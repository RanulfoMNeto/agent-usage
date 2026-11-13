"""Windows notification area: one icon and a limits panel, like the GNOME top bar."""

import ctypes
import json
import logging
import math
import re
import sys
import time
from pathlib import Path

from PySide6.QtCore import QObject, QPoint, QRect, QRectF, QSize, Qt, QTimer, Signal, Slot
from PySide6.QtGui import (
    QColor,
    QCursor,
    QFont,
    QFontMetricsF,
    QGuiApplication,
    QIcon,
    QPainter,
    QPalette,
    QPixmap,
)
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QRadioButton,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

ASSETS = Path(__file__).with_name("static") / "assets"
# Account queries keep their own cache; this only bounds how soon the icon follows.
REFRESH_SECONDS = 5
# Icon sizes the shell asks for between 100% and 300% display scaling.
ICON_SIZES = (16, 20, 24, 32, 40, 48)
# Windows cuts notification area tooltips beyond this length.
TOOLTIP_LIMIT = 127
# The limits panel: its width, and its gap to the taskbar and screen edges.
FLYOUT_WIDTH = 320
FLYOUT_MARGIN = 12
# A click on the icon that just closed the panel arrives this soon after.
REOPEN_SECONDS = 0.3
# DwmSetWindowAttribute: ask Windows 11 for its rounded corners.
DWMWA_WINDOW_CORNER_PREFERENCE, DWMWCP_ROUND = 33, 2

# Presentation rules mirror usage/gnome/limits.js; a test runs both on the same payload.
ISSUES = {
    "disconnected": lambda name: "Disconnected",
    "login_required": lambda name: "Not connected",
    "unsupported_account": lambda name: "Subscription required",
    "unsupported_version": lambda name: f"Update {name}",
    "timeout": lambda name: "Connection timed out",
}
AGENT_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
COLOR = re.compile(r"#[0-9a-fA-F]{6}")
LEVELS = {"warning": "#f8e45c", "critical": "#ff7b63"}


def _finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def _number(value):
    return str(int(value)) if float(value).is_integer() else str(value)


def limit_label(minutes):
    if minutes == 10080:
        return "Weekly"
    if minutes == 1440:
        return "Daily"
    return f"{_number(minutes / 60)}-hour" if minutes % 60 == 0 else f"{_number(minutes)}-minute"


def countdown(seconds):
    minutes = max(0, math.ceil(seconds / 60))
    if minutes >= 1440:
        return f"{minutes // 1440}d {minutes % 1440 // 60}h"
    return f"{minutes // 60}h {minutes % 60}m" if minutes >= 60 else f"{minutes}m"


def level(used, warning):
    if used >= 100:
        return "critical"
    return "warning" if warning > 0 and used >= warning else "normal"


def summarize(payload, now, selection=()):
    """Agents with their limits for the menu, and the chosen limits that get an icon."""
    payload = payload if isinstance(payload, dict) else {}
    warning = payload.get("warning")
    warning = warning if _finite(warning) and float(warning).is_integer() else 0
    agents = []
    for agent in payload.get("agents") if isinstance(payload.get("agents"), list) else []:
        if (
            not isinstance(agent, dict)
            or not isinstance(agent.get("id"), str)
            or not AGENT_ID.fullmatch(agent["id"])
            or not isinstance(agent.get("name"), str)
        ):
            continue
        windows = sorted(
            (
                item
                for item in (agent.get("limits") if isinstance(agent.get("limits"), list) else [])
                if isinstance(item, dict)
                and isinstance(item.get("id"), str)
                and all(_finite(item.get(key)) for key in ("minutes", "used_percent", "resets_at"))
                and item["resets_at"] > now
            ),
            key=lambda item: item["minutes"],
        )
        if not windows and agent.get("issue") == "not_found":
            continue
        available = agent.get("status") == "available"
        color = agent.get("color")
        color = color if isinstance(color, str) and COLOR.fullmatch(color) else "#9a9a9a"
        buckets = len({item.get("bucket") for item in windows}) > 1
        limits = []
        for item in windows:
            state = level(item["used_percent"], warning) if available else "normal"
            bucket = item.get("bucket")
            prefix = f"{bucket} · " if buckets and bucket is not None else ""
            limits.append(
                dict(
                    id=item["id"],
                    agent=agent["id"],
                    label=f"{prefix}{limit_label(item['minutes'])} limit",
                    used=item["used_percent"],
                    value=f"{math.floor(item['used_percent'])}%",
                    level=state,
                    fill=LEVELS.get(state, color),
                    stale=agent.get("status") == "stale",
                    resets_at=item["resets_at"],
                    reset=f"Resets in {countdown(item['resets_at'] - now)}",
                    selected=item["id"] in selection,
                )
            )
        issue = ISSUES.get(agent.get("issue"), lambda name: "Limits unavailable")
        note = (
            "Outdated" if agent.get("status") == "stale" else "" if limits else issue(agent["name"])
        )
        agents.append(dict(id=agent["id"], name=agent["name"], note=note, limits=limits))
    agents.sort(key=lambda agent: agent["name"].casefold())
    shown = [limit for agent in agents for limit in agent["limits"] if limit["selected"]]
    return dict(agents=agents, shown=shown)


def text_color(fill):
    """Black or white, whichever contrasts more with the fill (WCAG relative luminance)."""

    def channel(value):
        value /= 255
        return value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4

    color = QColor(fill)
    luminance = sum(
        weight * channel(value)
        for weight, value in zip(
            (0.2126, 0.7152, 0.0722), (color.red(), color.green(), color.blue())
        )
    )
    dark = (luminance + 0.05) / 0.05
    light = 1.05 / (luminance + 0.05)
    return QColor("#000000") if dark >= light else QColor("#ffffff")


def badge(value, fill, faded=False):
    """A limit's icon: its used percentage on its color, legible on light and dark taskbars."""
    icon = QIcon()
    for size in ICON_SIZES:
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        if faded:
            painter.setOpacity(0.45)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(fill))
        painter.drawRoundedRect(QRectF(0, 0, size, size), size * 0.22, size * 0.22)
        font = QFont(QApplication.font())
        font.setBold(True)
        # The largest font whose digits still fit inside the badge.
        for pixels in range(round(size * 0.75), 4, -1):
            font.setPixelSize(pixels)
            if QFontMetricsF(font).horizontalAdvance(value) <= size * 0.9:
                break
        painter.setFont(font)
        painter.setPen(text_color(fill))
        painter.drawText(QRectF(0, 0, size, size), Qt.AlignmentFlag.AlignCenter, value)
        painter.end()
        icon.addPixmap(pixmap)
    return icon


def placement(anchor, area, size, margin=FLYOUT_MARGIN):
    """Top-left corner for a panel beside the clicked icon, inside the screen's work area.

    The panel opens away from the taskbar, wherever it is docked: above an icon in the
    lower half of the screen and below one in the upper half, centered on the icon.
    """
    x = anchor.center().x() - size.width() // 2
    if anchor.center().y() >= area.center().y():
        y = anchor.top() - margin - size.height()
    else:
        y = anchor.bottom() + 1 + margin
    x = max(area.left() + margin, min(x, area.right() + 1 - margin - size.width()))
    y = max(area.top() + margin, min(y, area.bottom() + 1 - margin - size.height()))
    return QPoint(x, y)


class Meter(QWidget):
    """A limit's use as a thin bar in the limit's color, like the GNOME menu's."""

    def __init__(self, used, fill, faded):
        super().__init__()
        self.used = max(0.0, min(float(used), 100.0))
        self.fill = QColor(fill)
        self.faded = faded
        self.setFixedHeight(4)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        rect = QRectF(self.rect())
        radius = rect.height() / 2
        track = QColor(self.palette().color(QPalette.ColorRole.WindowText))
        track.setAlphaF(0.16)
        painter.setBrush(track)
        painter.drawRoundedRect(rect, radius, radius)
        if self.used:
            if self.faded:
                painter.setOpacity(0.45)
            painter.setBrush(self.fill)
            width = max(rect.height(), rect.width() * self.used / 100)
            painter.drawRoundedRect(QRectF(0, 0, width, rect.height()), radius, radius)


class Flyout(QWidget):
    """The panel a click on a tray icon opens, beside it, as the shell's own flyouts do."""

    def __init__(self, tray):
        # A popup closes on a click elsewhere and on Escape, like the shell's flyouts.
        super().__init__(None, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.tray = tray
        self.anchor = QRect()
        self.closed_at = 0.0
        self.signature = None
        self.content = None
        self.setFixedWidth(FLYOUT_WIDTH)
        self.setAutoFillBackground(True)
        self.setAccessibleName("Agent Usage limits")
        self.frame = QVBoxLayout(self)
        self.frame.setContentsMargins(0, 0, 0, 0)

    def open(self, anchor):
        self.anchor = QRect(anchor)
        self.update_content()
        self.place()
        self.show()
        self.activateWindow()

    def area(self):
        screen = QGuiApplication.screenAt(self.anchor.center()) or QGuiApplication.primaryScreen()
        return screen.availableGeometry()

    def place(self):
        self.frame.activate()
        self.adjustSize()
        self.move(placement(self.anchor, self.area(), self.size()))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # INVARIANT: the panel's edge facing the taskbar stays put while its content changes.
        if self.isVisible() and self.anchor.isValid():
            self.move(placement(self.anchor, self.area(), self.size()))

    def showEvent(self, event):
        super().showEvent(event)
        if sys.platform == "win32":
            # Windows 11 rounds and shadows the panel as it does its own; earlier versions ignore it.
            preference = ctypes.c_int(DWMWCP_ROUND)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                ctypes.c_void_p(int(self.winId())),
                DWMWA_WINDOW_CORNER_PREFERENCE,
                ctypes.byref(preference),
                ctypes.sizeof(preference),
            )

    def hideEvent(self, event):
        super().hideEvent(event)
        self.closed_at = time.monotonic()

    def label(self, text, muted=False, bold=False, size=None):
        label = QLabel(text)
        font = label.font()
        font.setBold(bold)
        if size:
            font.setPixelSize(size)
        label.setFont(font)
        if muted:
            palette = label.palette()
            role = QPalette.ColorRole.WindowText
            palette.setColor(role, self.palette().color(QPalette.ColorRole.PlaceholderText))
            label.setPalette(palette)
        return label

    def choice(self, agent, limit):
        # One limit at most: choosing another replaces it, choosing it again clears it.
        button = QRadioButton()
        button.setAutoExclusive(False)
        button.setChecked(limit["selected"])
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setToolTip(
            "Shown on the tray icon" if limit["selected"] else "Show on the tray icon"
        )
        button.setAccessibleName(f"Show {agent['name']} {limit['label']} on the tray icon")
        button.toggled.connect(
            lambda chosen, key=limit["id"]: self.tray.choose(key if chosen else None)
        )
        return button

    def update_content(self):
        """Rebuild for new limits; identical content is left alone, so nothing flickers."""
        summary = self.tray.summary
        loaded = self.tray.payload is not None
        visible = self.tray.widget.isVisible()
        signature = json.dumps([summary["agents"], loaded, visible], sort_keys=True)
        if signature == self.signature and self.content is not None:
            return
        self.signature = signature
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(16, 14, 16, 8)
        layout.setSpacing(4)
        if not summary["agents"]:
            layout.addWidget(self.label("Loading…" if not loaded else "No agent limits", True))
        for index, agent in enumerate(summary["agents"]):
            if index:
                layout.addSpacing(10)
            heading = QHBoxLayout()
            heading.setSpacing(8)
            icon = QLabel()
            icon.setPixmap(
                self.tray.agent_icon(agent["id"], self.palette()).pixmap(
                    QSize(16, 16), self.devicePixelRatioF()
                )
            )
            heading.addWidget(icon)
            heading.addWidget(self.label(agent["name"], bold=True))
            heading.addStretch()
            if agent["note"]:
                heading.addWidget(self.label(agent["note"], muted=True))
            layout.addLayout(heading)
            for limit in agent["limits"]:
                layout.addSpacing(6)
                title = QHBoxLayout()
                title.addWidget(self.label(limit["label"]))
                title.addStretch()
                title.addWidget(self.label(limit["value"], bold=True, size=16))
                layout.addLayout(title)
                layout.addWidget(Meter(limit["used"], limit["fill"], limit["stale"]))
                footer = QHBoxLayout()
                footer.addWidget(self.label(limit["reset"], muted=True))
                footer.addStretch()
                footer.addWidget(self.choice(agent, limit))
                layout.addLayout(footer)
        content = QWidget()
        column = QVBoxLayout(content)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(0)
        column.addWidget(body)
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Plain)
        column.addWidget(line)
        actions = QHBoxLayout()
        actions.setContentsMargins(8, 4, 8, 8)
        show = QPushButton("Hide widget" if visible else "Show widget")
        quit_app = QPushButton("Quit")
        for button in (show, quit_app):
            button.setFlat(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
        show.clicked.connect(self.toggle_widget)
        quit_app.clicked.connect(QApplication.instance().quit)
        actions.addWidget(show)
        actions.addStretch()
        actions.addWidget(quit_app)
        column.addLayout(actions)
        if self.content is not None:
            self.frame.removeWidget(self.content)
            self.content.hide()
            self.content.deleteLater()
        self.content = content
        self.frame.addWidget(content)
        # Children added to a shown window appear only later; sized while hidden, the
        # panel would be measured empty and then grow over the taskbar.
        content.show()
        self.place()

    def toggle_widget(self):
        self.hide()
        self.tray.toggle()


class Tray(QObject):
    """The notification area icon: the chosen limit, or the app's own; it opens the panel."""

    collected = Signal(object)

    def __init__(self, widget, payload):
        super().__init__(widget)
        self.widget = widget
        # Turns a limits snapshot into the top bar payload, without history or paths.
        self.payload_for = payload
        self.payload = None
        self.summary = dict(agents=[], shown=[])
        self.running = False
        self.task = None
        self.flyout = Flyout(self)
        # Right-click keeps the shell's convention: a short menu of app commands.
        self.menu = QMenu()
        self.menu.aboutToShow.connect(self.fill_menu)
        # One icon whose place on the taskbar never moves; until limits arrive, the app's.
        self.icon = QSystemTrayIcon(self)
        self.icon.setIcon(QIcon(str(ASSETS / "widget.svg")))
        self.icon.setToolTip("Agent Usage")
        self.icon.setContextMenu(self.menu)
        self.icon.activated.connect(self.activated)
        self.face = None
        self.collected.connect(self.receive)
        self.timer = QTimer(self)
        self.timer.setInterval(REFRESH_SECONDS * 1000)
        self.timer.timeout.connect(self.refresh)

    def start(self):
        """Show the icon where Windows offers a notification area; the demo stays out."""
        if (
            sys.platform != "win32"
            or self.widget.config.get("demo")
            or not QSystemTrayIcon.isSystemTrayAvailable()
        ):
            return False
        self.running = True
        self.render()
        self.timer.start()
        self.refresh()
        return True

    def stop(self):
        self.running = False
        self.timer.stop()
        self.flyout.hide()
        self.icon.hide()

    @Slot()
    def refresh(self):
        if self.task is None or self.task.done():
            self.task = self.widget.bridge.pool.submit(self.collect)

    def collect(self):
        bridge = self.widget.bridge
        try:
            self.collected.emit(bridge.limits.snapshot(bridge.usage.connections))
        except Exception as exc:
            logging.warning("Tray limits unavailable (%s)", type(exc).__name__)

    @Slot(object)
    def receive(self, snapshot):
        try:
            self.payload = self.payload_for(snapshot)
        except (ValueError, TypeError, KeyError):
            logging.warning("Tray limits could not be prepared.")
            return
        self.render()

    def selected(self):
        value = self.widget.preferences.value("tray_limit", "")
        return value if isinstance(value, str) else ""

    def choose(self, key):
        """Show this limit on the icon, or the app's own icon for None."""
        self.widget.preferences.setValue("tray_limit", key or "")
        self.render()

    def overview(self):
        """The icon's tooltip: every agent's limits on one line each."""
        lines = ["Agent Usage"]
        for agent in self.summary["agents"]:
            values = ", ".join(
                f"{limit['label'].removesuffix(' limit')} {limit['value']}"
                for limit in agent["limits"]
            )
            note = f" ({agent['note']})" if values and agent["note"] else agent["note"]
            lines.append(f"{agent['name']}: {values}{note}")
        while len("\n".join(lines)) > TOOLTIP_LIMIT and len(lines) > 1:
            lines.pop()
        return "\n".join(lines)[:TOOLTIP_LIMIT]

    def render(self):
        selected = self.selected()
        self.summary = summarize(self.payload, time.time(), [selected] if selected else [])
        # A chosen limit that is not reported right now leaves the app's icon until it is.
        limit = next(iter(self.summary["shown"]), None)
        face = (limit["value"].removesuffix("%"), limit["fill"], limit["stale"]) if limit else None
        if face != self.face:
            self.icon.setIcon(badge(*face) if face else QIcon(str(ASSETS / "widget.svg")))
            self.face = face
        tooltip = self.overview()
        if self.icon.toolTip() != tooltip:
            self.icon.setToolTip(tooltip)
        if self.running and not self.icon.isVisible():
            self.icon.show()
        if self.flyout.isVisible():
            self.flyout.update_content()

    def agent_icon(self, agent_id, palette):
        icon = QIcon(str(ASSETS / "agents" / f"{agent_id}.svg"))
        agent = self.widget.bridge.agents.get(agent_id)
        if not agent or not agent.monochrome:
            return icon
        # Monochrome marks take the text color, as the widget inverts them when dark.
        tinted = QIcon()
        for size in ICON_SIZES:
            pixmap = icon.pixmap(size, size)
            painter = QPainter(pixmap)
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
            painter.fillRect(pixmap.rect(), palette.color(QPalette.ColorRole.WindowText))
            painter.end()
            tinted.addPixmap(pixmap)
        return tinted

    @Slot()
    def fill_menu(self):
        self.menu.clear()
        visible = self.widget.isVisible()
        self.menu.addAction("Hide widget" if visible else "Show widget").triggered.connect(
            self.toggle
        )
        self.menu.addAction("Quit").triggered.connect(QApplication.instance().quit)

    @Slot(QSystemTrayIcon.ActivationReason)
    def activated(self, reason):
        if reason != QSystemTrayIcon.ActivationReason.Trigger:
            return
        if self.flyout.isVisible():
            self.flyout.hide()
            return
        # A click on the icon first closes an open panel as a click elsewhere; it must not
        # reopen it at once.
        if time.monotonic() - self.flyout.closed_at < REOPEN_SECONDS:
            return
        anchor = self.icon.geometry()
        if not anchor.isValid() or anchor.isEmpty():
            # Windows may not place an icon kept in the overflow; the click happened here.
            anchor = QRect(QCursor.pos(), QSize(1, 1))
        self.flyout.open(anchor)

    @Slot()
    def toggle(self):
        if self.widget.isVisible():
            self.widget.preferences.setValue("hidden", True)
            self.widget.hide()
        else:
            self.widget.reveal()
