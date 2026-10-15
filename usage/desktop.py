"""Persistent desktop widget and local-only UI, without a listening port."""

import json
import logging
import math
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PySide6.QtCore import (
    QByteArray,
    QCoreApplication,
    QEvent,
    QIODevice,
    QLockFile,
    QObject,
    QSaveFile,
    QSettings,
    Qt,
    QUrl,
    Signal,
    Slot,
)
from PySide6.QtGui import QColor, QDesktopServices, QIcon, QScreen
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import (
    QWebEnginePage,
    QWebEngineProfile,
    QWebEngineSettings,
    QWebEngineUrlRequestInterceptor,
)
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

from .config import user_directory
from .history import MAX_IMPORT, Usage
from .integration import gnome_call, install_claude_statusline
from .readers import project_path
from .subscriptions import Subscriptions, validate_login_url

STATIC = Path(__file__).with_name("static").resolve()


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
    layout_changed = Signal(int, float, bool)
    login_url = Signal(str)

    def __init__(self, usage, subscriptions, settings, window):
        super().__init__(window)
        self.usage, self.subscriptions, self.settings, self.window = (
            usage,
            subscriptions,
            settings,
            window,
        )
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="agent-usage")
        self.login_url.connect(self.open_login)

    @Slot(str)
    def open_login(self, url):
        try:
            url = validate_login_url(url)
            if not getattr(self.window, "config", {}).get("demo") and QDesktopServices.openUrl(
                QUrl(url)
            ):
                return
        except ValueError:
            pass
        self.subscriptions.cancel_login()

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
            if method in ("connect", "cancel_login"):
                if getattr(self.window, "config", {}).get("demo"):
                    raise ValueError("Connections are disabled in the demo")
                if args not in ("codex", "claude"):
                    raise ValueError("Unknown connection")
                if method == "cancel_login":
                    self.subscriptions.cancel_login()
                    self.respond(identifier, {})
                    return
                if args == "claude":
                    config = self.subscriptions.config_path
                    issue = install_claude_statusline(config)
                    if issue == "claude_custom_statusline":
                        answer = QMessageBox.question(
                            self.window,
                            "Claude terminal capture",
                            "Replace your current Claude terminal status line with Agent Usage?\n\n"
                            "A backup will be kept. Limits require Claude terminal use; "
                            "the VS Code chat panel alone does not supply them.",
                            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                            QMessageBox.StandardButton.No,
                        )
                        if answer != QMessageBox.StandardButton.Yes:
                            self.respond(identifier, {"cancelled": True})
                            return
                        issue = install_claude_statusline(config, replace=True)
                    self.respond(identifier, {"issue": issue})
                    return
            if method == "preferences":
                values = json.loads(self.settings.value("appearance", "{}"))
                if getattr(self.window, "config", {}).get("demo"):
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
                if (
                    not isinstance(args, dict)
                    or type(args.get("height")) is not int
                    or not 0 < args["height"] <= 10000
                    or type(args.get("compact")) is not bool
                ):
                    raise ValueError("Invalid content layout")
                self.window.content_height = args["height"]
                self.window.compact = args["compact"]
                self.window.apply_size()
                self.respond(identifier, {})
                return
            if method in ("choose_directory", "export", "import"):
                if method == "choose_directory":
                    if args not in ("codex", "claude", "project"):
                        raise ValueError("Unknown directory type")
                    project = args == "project"
                    current = (
                        json.loads(self.settings.value("appearance", "{}")).get("project")
                        if project
                        else self.usage.sources()[args]["path"]
                    )
                    path = QFileDialog.getExistingDirectory(
                        self.window,
                        "Project directory" if project else "Session directory",
                        current
                        if isinstance(current, str) and Path(current).is_dir()
                        else str(Path.home()),
                    )
                    if path and project:
                        self.respond(identifier, {"path": project_path(path)})
                        return
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
            if method == "quit":
                QApplication.instance().quit()
                return
            task = self.pool.submit(self.execute, method, args)
            task.add_done_callback(lambda future: self.finish(identifier, future))
        except (ValueError, TypeError, KeyError, OSError):
            self.respond(identifier, {"error": "Could not update settings."})

    def execute(self, method, args):
        if method == "connect" and args == "codex":
            return self.subscriptions.connect_codex(self.login_url.emit)
        if method == "usage":
            return self.usage.snapshot()
        if method == "subscriptions":
            return self.subscriptions.snapshot()
        if method == "sources":
            return self.usage.sources()
        if method in ("choose_directory", "reset_source"):
            name = args["agent"] if method == "choose_directory" else args
            if name not in ("codex", "claude"):
                raise ValueError("Unknown source")
            values = {
                key: value["override"]
                for key, value in self.usage.sources().items()
                if key in ("codex", "claude")
            }
            values[name] = args["path"] if method == "choose_directory" else ""
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
        if isinstance(self.subscriptions, Subscriptions):
            self.subscriptions.cancel_login(close=True)
        self.pool.shutdown(wait=True, cancel_futures=True)


class Widget(QWebEngineView):
    def __init__(self, config, usage, subscriptions):
        super().__init__()
        self.config = config
        self.content_height = 120
        self.layout_width = config["width"]
        self.layout_scale = 1
        self.compact = False
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
        self.bridge = Bridge(usage, subscriptions, self.preferences, self)
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
        self.compact = values.get("view") == "compact"
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
        self.windowHandle().screenChanged.connect(
            self.watch_screen, Qt.ConnectionType.UniqueConnection
        )
        self.watch_screen(self.screen())

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
        scale = min(
            self.layout_scale, self.width() / self.layout_width, self.height() / self.content_height
        )
        self.bridge.layout_changed.emit(self.layout_width, scale, self.compact)

    @Slot()
    def apply_size(self):
        screen = self.screen().availableGeometry()
        width = self.config["width"]
        if self.compact:
            width = min(width, self.config["compact_width"])
        self.layout_width = width
        height = max(120, self.content_height)
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


def run(config, *, subscriptions=None):
    app = QApplication.instance() or QApplication([])
    app.setApplicationName("Agent Usage")
    app.setDesktopFileName("agent-usage")
    app.setOrganizationName("AgentUsage")
    app.setQuitOnLastWindowClosed(True)
    state = user_directory("state")
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = QLockFile(str(state / "widget.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(0):
        QMessageBox.information(None, "Agent Usage", "The widget is already running.")
        return
    usage = Usage(config)
    widget = Widget(
        config["widget"], usage, subscriptions or Subscriptions(config["subscriptions"])
    )
    usage.start()
    widget.show()
    try:
        app.exec()
    finally:
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
