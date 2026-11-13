"""Persistent desktop widget and local-only UI, without a network port."""

import ctypes
import hashlib
import json
import logging
import math
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PySide6.QtCore import (
    SLOT,
    QByteArray,
    QCoreApplication,
    QEvent,
    QIODevice,
    QLockFile,
    QObject,
    QSaveFile,
    QSettings,
    Qt,
    QTimer,
    QUrl,
    Signal,
    Slot,
)
from PySide6.QtGui import QColor, QDesktopServices, QIcon, QScreen
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import (
    QWebEnginePage,
    QWebEngineProfile,
    QWebEngineSettings,
    QWebEngineUrlRequestInterceptor,
)
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

from .agents import AGENTS
from .agents.base import validate_login_url
from .config import user_directory
from .history import MAX_IMPORT, Usage
from .integration import (
    ConnectionUnavailable,
    gnome_call,
    gnome_commands,
    gnome_session,
    open_terminal,
    terminal_command,
)
from .limits import Limits
from .scheduler import Scheduler
from .tray import Tray

STATIC = Path(__file__).with_name("static").resolve()
# Account queries keep their own cache; this only bounds how soon the top bar follows.
TOP_BAR_SECONDS = 5
# Unchanged limits are republished so the extension can tell an exit from silence.
TOP_BAR_HEARTBEAT = 60
# Zoom preference bounds; the renderer offers fixed steps within them.
ZOOM_RANGE = (0.5, 2.0)


def zoom_level(value):
    """The saved zoom factor when valid, otherwise the natural size."""
    low, high = ZOOM_RANGE
    return float(value) if type(value) in (int, float) and low <= value <= high else 1.0


# A later launch sends the only request the running widget accepts, and waits for the answer.
REVEAL = b"reveal\n"
REVEALED = b"shown\n"
# A launch that never closes its end after the answer is dropped after this long.
ANSWER_SECONDS = 5


def instance_name(state):
    """Per-user channel between launches; Unix sockets live in the private state directory."""
    if sys.platform == "win32":
        # Named pipes share one machine-wide namespace and cannot contain path separators.
        digest = hashlib.sha256(str(state).casefold().encode()).hexdigest()[:16]
        return f"agent-usage-{digest}"
    return str(state / "widget.sock")


def reveal_running(name, timeout=2000):
    """Ask the running widget to show itself; False when nothing answers."""
    if sys.platform == "win32":
        # The user started this launch, so it may hand the foreground to the widget (ASFW_ANY).
        ctypes.windll.user32.AllowSetForegroundWindow(-1)
    socket = QLocalSocket()
    socket.connectToServer(name)
    try:
        if not socket.waitForConnected(timeout):
            return False
        socket.write(REVEAL)
        # Only the answer proves the request arrived: on Windows a write can finish
        # before this wait starts, which then reports that nothing was written.
        socket.waitForBytesWritten(timeout)
        while not socket.canReadLine() and socket.waitForReadyRead(timeout):
            pass
        return socket.canReadLine() and bytes(socket.readLine()) == REVEALED
    finally:
        # This side closes: the widget's side closing first could drop the answer unread.
        socket.abort()


class Launches(QObject):
    """Let a later launch reveal this widget, which × only hides, instead of starting again."""

    def __init__(self, widget, name):
        super().__init__(widget)
        self.widget = widget
        self.server = QLocalServer(self)
        # INVARIANT: only the same user can connect, and only REVEAL does anything.
        self.server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        self.server.newConnection.connect(self.accept)
        # Holding the widget lock proves that a leftover socket belongs to a crashed instance.
        QLocalServer.removeServer(name)
        if not self.server.listen(name):
            logging.warning("Later launches cannot reveal the widget.")

    @Slot()
    def accept(self):
        while (socket := self.server.nextPendingConnection()) is not None:
            socket.readyRead.connect(lambda socket=socket: self.answer(socket))
            socket.disconnected.connect(socket.deleteLater)

    def answer(self, socket):
        if socket.bytesAvailable() > len(REVEAL):
            socket.abort()
            return
        if not socket.canReadLine():
            return
        if bytes(socket.readLine()) != REVEAL:
            socket.disconnectFromServer()
            return
        # Answer before showing the window, which can be slow, and leave closing to the
        # launch: on Windows, disconnecting a pipe discards an answer not yet read.
        socket.write(REVEALED)
        socket.flush()
        QTimer.singleShot(ANSWER_SECONDS * 1000, socket, socket.abort)
        self.widget.reveal()


class LocalOnly(QWebEngineUrlRequestInterceptor):
    def interceptRequest(self, info):
        url = info.requestUrl()
        allowed = url.toString() == "qrc:///qtwebchannel/qwebchannel.js"
        if url.isLocalFile():
            allowed = Path(url.toLocalFile()).resolve().is_relative_to(STATIC)
        info.block(not allowed)


class LocalPage(QWebEnginePage):
    def acceptNavigationRequest(self, url, navigation_type, is_main_frame):
        return url.isLocalFile() and Path(url.toLocalFile()).resolve() == STATIC / "index.html"


class Bridge(QObject):
    reply = Signal(str, str)
    layout_changed = Signal(int, float)
    login_url = Signal(str, str)

    def __init__(self, usage, limits, settings, window, agents=AGENTS, scheduler=None):
        super().__init__(window)
        self.usage, self.limits, self.settings, self.window = usage, limits, settings, window
        self.agents = agents
        self.scheduler = scheduler or Scheduler(usage, limits, agents=agents)
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="agent-usage")
        self.terminal = None
        self.login_url.connect(self.open_login)

    @property
    def demo(self):
        return getattr(self.window, "config", {}).get("demo", False)

    def agent(self, value):
        if value not in self.agents:
            raise ValueError("Unknown agent")
        return self.agents[value]

    @Slot(str, str)
    def open_login(self, agent, url):
        try:
            url = validate_login_url(url, self.agent(agent).login_hosts)
            if not self.demo and QDesktopServices.openUrl(QUrl(url)):
                return
        except ValueError:
            pass
        self.limits.cancel_login()

    def respond(self, identifier, value):
        self.reply.emit(identifier, json.dumps(value, ensure_ascii=False, allow_nan=False))

    def finish(self, identifier, task):
        try:
            self.respond(identifier, task.result())
        except (ValueError, TypeError, KeyError):
            self.respond(identifier, {"error": "Invalid data or directory."})
        except Exception as exc:
            logging.warning("Local operation failed (%s)", type(exc).__name__)
            self.respond(identifier, {"error": "Could not complete the operation."})

    @Slot(str, str, str)
    def request(self, identifier, method, payload):
        try:
            if len(payload) > 16384:
                raise ValueError("Oversized request")
            args = json.loads(payload)
            if method == "agents":
                self.respond(
                    identifier,
                    [
                        dict(
                            id=agent.id,
                            name=agent.name,
                            colors=list(agent.colors),
                            monochrome=agent.monochrome,
                            icon=f"assets/agents/{agent.id}.svg",
                        )
                        for agent in self.agents.values()
                    ],
                )
                return
            if method in ("connect", "disconnect", "cancel_login"):
                if self.demo:
                    raise ValueError("Connections are disabled in the demo")
                agent = self.agent(args)
                if method == "cancel_login":
                    self.limits.cancel_login()
                    self.respond(identifier, {})
                    return
                if method == "connect" and agent.login == "terminal":
                    try:
                        command = self.limits.command(agent)
                    except FileNotFoundError:
                        raise ConnectionUnavailable(
                            f"Install {agent.name} in this environment."
                        ) from None
                    self.terminal = open_terminal(terminal_command(command))
            if method == "preferences":
                values = json.loads(self.settings.value("appearance", "{}"))
                if self.demo:
                    values["demo"] = True
                self.respond(identifier, values)
                return
            if method == "save_preferences":
                if not isinstance(args, dict) or len(payload) > 4096:
                    raise ValueError("Invalid preferences")
                self.settings.setValue("appearance", payload)
                self.settings.sync()
                if self.settings.status() != QSettings.Status.NoError:
                    raise OSError("Preferences unavailable")
                self.window.apply_preferences(args)
                self.respond(identifier, {})
                return
            if method == "move":
                handle = self.window.windowHandle()
                if handle and QApplication.mouseButtons() & Qt.MouseButton.LeftButton:
                    gnome_call("PrepareMove")
                    handle.startSystemMove()
                self.respond(identifier, {})
                return
            if method == "layout":
                height = args.get("height") if isinstance(args, dict) else None
                if type(height) is not int or not 0 < height <= 10000:
                    raise ValueError("Invalid content layout")
                self.window.content_height = height
                self.window.apply_size()
                self.respond(identifier, {})
                return
            if method in ("choose_directory", "export", "import"):
                if method == "choose_directory":
                    path = QFileDialog.getExistingDirectory(
                        self.window,
                        f"{self.agent(args).name} history directory",
                        self.usage.sources()[args]["path"],
                    )
                elif method == "export":
                    path, _ = QFileDialog.getSaveFileName(
                        self.window,
                        "Export history",
                        str(Path.home() / "agent-usage-history.json"),
                        "JSON (*.json)",
                    )
                else:
                    path, _ = QFileDialog.getOpenFileName(
                        self.window, "Import history", str(Path.home()), "JSON (*.json)"
                    )
                if not path:
                    self.respond(identifier, {"cancelled": True})
                    return
                args = {"agent": args, "path": path}
            if method == "hide":
                self.settings.setValue("hidden", True)
                self.window.hide()
                self.respond(identifier, {})
                return
            if method == "quit":
                QApplication.instance().quit()
                return
            task = self.pool.submit(self.execute, method, args)
            task.add_done_callback(lambda future: self.finish(identifier, future))
        except ConnectionUnavailable as exc:
            self.respond(identifier, {"error": str(exc)})
        except (ValueError, TypeError, KeyError, OSError):
            self.respond(identifier, {"error": "Could not update settings."})

    def execute(self, method, args):
        refresh = self.usage.config["collection"]["refresh_seconds"]
        if method in ("connect", "disconnect"):
            agent = self.agent(args)
            if method == "connect" and agent.login == "browser":
                result = self.limits.connect(agent, lambda url: self.login_url.emit(agent.id, url))
                if result.get("issue"):
                    return {"issue": result["issue"]}
            elif method == "connect":
                self.limits.invalidate(agent)
            self.usage.set_connection(agent.id, method == "connect")
            return self.limits.snapshot(self.usage.connections)
        if method == "usage":
            return dict(self.usage.snapshot(), refresh_seconds=refresh)
        if method == "limits":
            # Rendering follows collection cadence; account queries keep their own cache.
            self.scheduler.limits = self.limits
            snap = self.limits.snapshot(self.usage.connections)
            try:
                scheduled = self.scheduler.snapshot(limits_snapshot=snap)
            except Exception as exc:
                # The scheduler never blocks limits from rendering.
                logging.warning("Scheduler snapshot failed (%s)", type(exc).__name__)
                scheduled = None
            # Limits the shell already shows, in the GNOME top bar or the Windows
            # notification area, are left out of the widget rather than shown twice.
            top_bar = getattr(getattr(self.window, "top_bar", None), "attached", False)
            tray = getattr(getattr(self.window, "tray", None), "running", False)
            return dict(
                snap,
                refresh_seconds=refresh,
                shell_limits=top_bar or tray,
                scheduler=scheduled,
            )
        if method == "scheduler":
            self.scheduler.limits = self.limits
            return self.scheduler.snapshot()
        if method == "save_schedule":
            if not isinstance(args, dict):
                raise ValueError("Invalid schedule request")
            agent = self.agent(args.get("agent"))
            self.scheduler.limits = self.limits
            return self.scheduler.set_schedule(agent.id, args.get("schedule"))
        if method == "sources":
            return self.usage.sources()
        if method in ("choose_directory", "reset_source"):
            agent = self.agent(args["agent"] if method == "choose_directory" else args)
            values = {key: value["override"] for key, value in self.usage.sources().items()}
            values[agent.id] = args["path"] if method == "choose_directory" else ""
            return self.usage.set_sources(values)
        if method == "export":
            body = json.dumps(self.usage.export(), ensure_ascii=False, allow_nan=False).encode()
            output = QSaveFile(args["path"])
            if (
                not output.open(QIODevice.OpenModeFlag.WriteOnly)
                or output.write(body) != len(body)
                or not output.commit()
            ):
                raise OSError("Export failed")
            return {}
        if method == "import":
            with Path(args["path"]).open("rb") as stream:
                body = stream.read(MAX_IMPORT + 1)
            if len(body) > MAX_IMPORT:
                raise ValueError("Oversized archive")
            return self.usage.import_data(json.loads(body))
        raise ValueError("Unknown operation")

    def close(self):
        self.scheduler.close()
        if hasattr(self.limits, "cancel_login"):
            self.limits.cancel_login(close=True)
        self.pool.shutdown(wait=True, cancel_futures=True)


def top_bar_limits(snapshot, agents, warning, visible):
    """Limits for the top bar, without history, paths or account details."""
    return dict(
        agents=[
            dict(
                id=entry["id"],
                name=agents[entry["id"]].name,
                # The shell's menus are dark, so the dark-theme accent applies.
                color=agents[entry["id"]].colors[1],
                status=entry["status"],
                issue=entry.get("issue"),
                limits=[
                    {
                        key: item[key]
                        for key in ("id", "bucket", "minutes", "used_percent", "resets_at")
                    }
                    for item in entry["windows"]
                ],
            )
            for entry in snapshot["agents"]
        ],
        warning=warning if type(warning) is int and 0 <= warning <= 100 else 0,
        visible=visible,
    )


class TopBar(QObject):
    """Mirror limits into the GNOME top bar, whose menu can then hide the widget."""

    collected = Signal(object)

    def __init__(self, widget):
        super().__init__(widget)
        self.widget = widget
        self.snapshot = {"agents": []}
        self.body = None
        self.sent = 0.0
        # Available follows each call; attached survives a locked screen until detached.
        self.available = self.attached = False
        self.task = None
        self.collected.connect(self.receive)
        self.timer = QTimer(self)
        self.timer.setInterval(TOP_BAR_SECONDS * 1000)
        self.timer.timeout.connect(self.refresh)

    def start(self):
        """Return whether the widget should stay hidden behind an answering top bar."""
        if self.widget.config.get("demo") or not gnome_session():
            return False
        gnome_commands(self, SLOT("command(int,QString)"))
        self.timer.start()
        self.refresh()
        self.publish()
        return self.available and self.widget.preferences.value("hidden", False, type=bool)

    def stop(self):
        if self.timer.isActive():
            self.timer.stop()
            gnome_call("SetLimits", "")

    @Slot()
    def refresh(self):
        if self.task is None or self.task.done():
            self.task = self.widget.bridge.pool.submit(self.collect)

    def collect(self):
        bridge = self.widget.bridge
        try:
            self.collected.emit(bridge.limits.snapshot(bridge.usage.connections))
        except Exception as exc:
            logging.warning("Top bar limits unavailable (%s)", type(exc).__name__)

    @Slot(object)
    def receive(self, snapshot):
        self.snapshot = snapshot
        self.publish()

    def publish(self):
        if not self.timer.isActive():
            return
        try:
            preferences = json.loads(self.widget.preferences.value("appearance", "{}"))
            warning = preferences.get("limitAlert") if isinstance(preferences, dict) else None
            body = json.dumps(
                top_bar_limits(
                    self.snapshot, self.widget.bridge.agents, warning, self.widget.isVisible()
                ),
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
            )
        except (ValueError, TypeError, KeyError):
            logging.warning("Top bar limits could not be prepared.")
            return
        # Retry after a failure: the shell disables extensions while the screen is locked.
        if (
            self.available
            and body == self.body
            and time.monotonic() - self.sent < TOP_BAR_HEARTBEAT
        ):
            return
        self.available = gnome_call("SetLimits", body)
        self.attached = self.attached or self.available
        self.body, self.sent = body, time.monotonic()

    @Slot(int, str)
    def command(self, pid, action):
        if pid != os.getpid():
            return
        if action == "toggle":
            visible = not self.widget.isVisible()
            self.widget.preferences.setValue("hidden", not visible)
            self.widget.setVisible(visible)
        elif action == "detach":
            # The extension is going away; never leave the widget unreachable.
            self.available = self.attached = False
            self.widget.show()
        elif action == "quit":
            QApplication.instance().quit()


class Widget(QWebEngineView):
    def __init__(self, config, usage, limits):
        super().__init__()
        self.config = config
        self.content_height = 120
        self.layout_scale = 1
        self.setWindowTitle("Agent Usage")
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setWindowIcon(QIcon(str(STATIC / "assets/widget.svg")))
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.profile = QWebEngineProfile(self)
        self.interceptor = LocalOnly(self)
        self.profile.setUrlRequestInterceptor(self.interceptor)
        self.setPage(LocalPage(self.profile, self))
        self.page().setBackgroundColor(QColor(Qt.GlobalColor.transparent))
        self.settings().setAttribute(
            QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, False
        )
        self.settings().setAttribute(
            QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True
        )
        self.settings().setAttribute(
            QWebEngineSettings.WebAttribute.JavascriptCanOpenWindows, False
        )
        self.preferences = QSettings(
            str(user_directory("state") / "settings.ini"), QSettings.Format.IniFormat
        )
        if config.get("demo"):
            self.preferences.setValue("appearance", json.dumps({"period": "all"}))
        try:
            saved = json.loads(self.preferences.value("appearance", "{}"))
        except (TypeError, ValueError):
            saved = {}
        # Applied before the first layout so the window never opens at the wrong size.
        self.zoom = zoom_level(saved.get("zoom") if isinstance(saved, dict) else None)
        self.setZoomFactor(self.zoom)
        self.page().zoomFactorChanged.connect(self.keep_zoom)
        self.bridge = Bridge(usage, limits, self.preferences, self)
        self.top_bar = TopBar(self)
        self.tray = Tray(self, self.limits_payload)
        self.channel = QWebChannel(self.page())
        self.channel.registerObject("backend", self.bridge)
        self.page().setWebChannel(self.channel)
        self.apply_size()
        geometry = self.preferences.value("geometry")
        if isinstance(geometry, QByteArray) and not QApplication.platformName().startswith(
            "wayland"
        ):
            self.restoreGeometry(geometry)
        self.load(QUrl.fromLocalFile(str(STATIC / "index.html")))

    def apply_preferences(self, values):
        self.top_bar.publish()
        zoom = zoom_level(values.get("zoom"))
        if zoom != self.zoom:
            self.zoom = zoom
            self.setZoomFactor(zoom)
            self.apply_size()
        pinned = values.get("pinned") is True
        if gnome_call("SetPinned", pinned):
            return
        if bool(self.windowFlags() & Qt.WindowType.WindowStaysOnTopHint) != pinned:
            visible = self.isVisible()
            self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, pinned)
            if visible:
                self.show()

    def showEvent(self, event):
        super().showEvent(event)
        self.top_bar.publish()
        self.windowHandle().screenChanged.connect(
            self.watch_screen, Qt.ConnectionType.UniqueConnection
        )
        self.watch_screen(self.screen())

    def hideEvent(self, event):
        super().hideEvent(event)
        self.top_bar.publish()

    def limits_payload(self, snapshot):
        """Limits as the top bar receives them, for the Windows notification area."""
        try:
            preferences = json.loads(self.preferences.value("appearance", "{}"))
        except (TypeError, ValueError):
            preferences = {}
        warning = preferences.get("limitAlert") if isinstance(preferences, dict) else None
        return top_bar_limits(snapshot, self.bridge.agents, warning, self.isVisible())

    def reveal(self):
        """Show the widget again, in front, as a later launch of the app expects."""
        self.preferences.setValue("hidden", False)
        self.setWindowState(self.windowState() & ~Qt.WindowState.WindowMinimized)
        self.show()
        self.raise_()
        self.activateWindow()

    @Slot(float)
    def keep_zoom(self, factor):
        # INVARIANT: only the zoom preference scales the page, so the window can follow it.
        # Chromium's own gestures and navigation resets would leave the window mis-sized.
        if not math.isclose(factor, self.zoom, abs_tol=1e-3):
            self.setZoomFactor(self.zoom)

    @Slot(QScreen)
    def watch_screen(self, screen):
        if screen:
            screen.availableGeometryChanged.connect(
                self.apply_size, Qt.ConnectionType.UniqueConnection
            )
            self.apply_size()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "bridge"):
            self.fit_content()

    def fit_content(self):
        # Content is measured in CSS pixels; each one spans `zoom` window pixels.
        width = self.config["width"]
        scale = min(
            self.layout_scale,
            self.width() / (width * self.zoom),
            self.height() / (self.content_height * self.zoom),
        )
        self.bridge.layout_changed.emit(width, scale)

    @Slot()
    def apply_size(self):
        screen = self.screen().availableGeometry()
        width = self.config["width"] * self.zoom
        height = max(120, self.content_height) * self.zoom
        scale = min(1, screen.width() / width, screen.height() / height)
        self.layout_scale = scale
        self.resize(
            min(screen.width(), math.ceil(width * scale)),
            min(screen.height(), math.ceil(height * scale)),
        )
        self.fit_content()
        if not QApplication.platformName().startswith("wayland"):
            self.move(
                max(screen.x(), min(self.x(), screen.right() + 1 - self.width())),
                max(screen.y(), min(self.y(), screen.bottom() + 1 - self.height())),
            )


def run(config, *, limits=None):
    app = QApplication.instance() or QApplication([])
    app.setApplicationName("Agent Usage")
    app.setDesktopFileName("agent-usage")
    app.setOrganizationName("AgentUsage")
    app.setQuitOnLastWindowClosed(False)
    state = user_directory("state")
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = instance_name(state)
    lock = QLockFile(str(state / "widget.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(0):
        # An older version, or a busy one, cannot answer; say why nothing opens.
        if not reveal_running(name):
            QMessageBox.information(None, "Agent Usage", "Agent Usage is already running.")
        return
    usage = Usage(config)
    widget = Widget(config["widget"], usage, limits or Limits(config))
    widget.launches = Launches(widget, name)
    usage.start()
    widget.bridge.scheduler.start()
    if not widget.top_bar.start():
        widget.show()
    widget.tray.start()
    try:
        app.exec()
    finally:
        widget.tray.stop()
        widget.top_bar.stop()
        widget.preferences.setValue("geometry", widget.saveGeometry())
        widget.preferences.sync()
        usage.close()
        widget.bridge.close()
        widget.hide()
        widget.page().deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        lock.unlock()
