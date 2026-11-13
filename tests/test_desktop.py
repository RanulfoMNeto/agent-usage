import json
import math
import os
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import Future
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import (
    SLOT,
    QCoreApplication,
    QEvent,
    QPoint,
    QPointF,
    QRect,
    QSettings,
    Qt,
    QUrl,
)
from PySide6.QtGui import QWheelEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QLabel,
    QPushButton,
    QRadioButton,
    QSystemTrayIcon,
    QWidget,
)

from usage.agents import AGENTS
from usage.agents.claude import Claude
from usage.config import load_config, user_directory
from usage.desktop import (
    REVEAL,
    STATIC,
    TOP_BAR_HEARTBEAT,
    Bridge,
    Launches,
    LocalOnly,
    LocalPage,
    TopBar,
    Widget,
    instance_name,
    reveal_running,
    run,
)
from usage.history import MAX_IMPORT
from usage.limits import Limits
from usage.tray import FLYOUT_MARGIN, REOPEN_SECONDS

WIDTH = 344
# No overflow is not enough: shrinking also waits for the native resize.
PANEL_FITS = """(()=>{
    const p=$('settings').open?$('settings'):document.querySelector('.widget');
    const r=p.getBoundingClientRect();
    return r.width>0 && r.height>0 && r.left>=0 && r.top>=0 &&
        r.right<=innerWidth && r.bottom<=innerHeight &&
        Math.abs(innerHeight-(r.bottom+r.top))<=2 &&
        p.scrollHeight<=p.clientHeight+1 &&
        document.documentElement.scrollHeight<=innerHeight+1 &&
        document.documentElement.scrollWidth<=innerWidth+1;
})()"""
# Like a real second launch, the client owns an application and its own process.
LAUNCH_CLIENT = """
import json, sys
from PySide6.QtCore import QCoreApplication
from PySide6.QtNetwork import QLocalSocket
from usage.desktop import reveal_running
app = QCoreApplication([])
name, requests = sys.argv[1], json.loads(sys.argv[2])
def send(request):
    socket = QLocalSocket()
    socket.connectToServer(name)
    socket.waitForConnected(2000)
    socket.write(request.encode())
    socket.waitForBytesWritten(2000)
    socket.waitForReadyRead(500)
    reply = bytes(socket.readAll()).decode()
    socket.abort()
    return reply
print(json.dumps([reveal_running(name) if r is None else send(r) for r in requests]))
"""


def account(agent, limits, now=None, status="available", **extra):
    """One agent's limits as the backend reports them: (minutes, used percent, reset)."""
    now = now or time.time()
    return dict(
        id=agent,
        status=status,
        updated_at=now,
        connected=True,
        windows=[
            dict(
                id=f"{agent}::{minutes}",
                bucket=None,
                minutes=minutes,
                used_percent=used,
                resets_at=resets,
            )
            for minutes, used, resets in limits
        ],
        **extra,
    )


def reporting(*accounts):
    limits = Mock()
    limits.snapshot.return_value = {"agents": list(accounts)}
    return limits


class ConfigTests(unittest.TestCase):
    def test_portable_paths_and_packaged_defaults(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.dict(
                os.environ,
                {"XDG_STATE_HOME": folder, "XDG_CONFIG_HOME": folder, "LOCALAPPDATA": folder},
            ):
                with patch("sys.platform", "linux"):
                    config = load_config()
                    self.assertEqual(
                        config["history"]["database"],
                        str(Path(folder) / "agent-usage/history.sqlite3"),
                    )
                    self.assertEqual((config["agents"], config["widget"]["width"]), ({}, WIDTH))
                with patch("sys.platform", "win32"):
                    self.assertEqual(user_directory("state"), Path(folder) / "AgentUsage")


class DesktopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.window = QWidget()
        self.window.apply_preferences = Mock()
        self.settings = QSettings(str(self.root / "settings.ini"), QSettings.Format.IniFormat)
        self.usage = Mock()
        self.usage.config = {"collection": {"refresh_seconds": 1}}
        self.usage.sources.return_value = {
            agent: {"path": str(self.root), "override": "", "exists": True} for agent in AGENTS
        }
        self.bridge = Bridge(self.usage, Mock(), self.settings, self.window)
        self.responses = []
        self.bridge.reply.connect(lambda _, body: self.responses.append(json.loads(body)))

    def tearDown(self):
        self.bridge.close()
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def widget(self, limits):
        with (
            patch("usage.desktop.user_directory", return_value=self.root),
            patch.object(Widget, "load"),
        ):
            return Widget({"width": WIDTH}, self.usage, limits)

    def dispose(self, window):
        window.tray.stop()
        window.top_bar.stop()
        window.bridge.close()
        window.page().deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def launch(self, name, *requests):
        """A later launch, in its own process, while this one keeps serving the widget.

        None asks through reveal_running; text is sent raw and its reply returned.
        """
        process = subprocess.Popen(
            [sys.executable, "-c", LAUNCH_CLIENT, name, json.dumps(requests)],
            cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        try:
            deadline = time.monotonic() + 20
            while process.poll() is None and time.monotonic() < deadline:
                QTest.qWait(10)
            output = process.communicate(timeout=1)[0]
        finally:
            process.kill()
        return json.loads(output.splitlines()[-1])

    @contextmanager
    def renderer(self, limits):
        with patch("usage.desktop.user_directory", return_value=self.root):
            window = Widget({"width": WIDTH}, self.usage, limits)

        def evaluate(expression):
            result = []
            window.page().runJavaScript("JSON.stringify(" + expression + ")", result.append)
            deadline = time.monotonic() + 10
            while not result and time.monotonic() < deadline:
                QTest.qWait(10)
            self.assertTrue(result and result[0], "Renderer did not respond: " + expression)
            return json.loads(result[0])

        def wait_for(expression):
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if evaluate(expression):
                    return
                QTest.qWait(20)
            self.fail("Renderer did not settle: " + expression)

        try:
            window.show()
            wait_for("typeof data!=='undefined' && !!data && limitsLoaded && agents.size>0")
            yield window, evaluate, wait_for
        finally:
            window.close()
            self.dispose(window)

    def test_limit_preferences_preserve_collection_and_survive_restart(self):
        now = time.time()
        self.usage.snapshot.return_value = {
            "rows": [{"id": "prompt", "agent": "codex", "start": now, "tokens": 123}],
            "warnings": [],
        }
        limits = reporting(
            account("codex", [(10080, 56, now + 3600)], now),
            account("claude", [(300, 0, now + 3600), (10080, 3, now + 3600)], now),
        )
        with self.renderer(limits) as (window, evaluate, wait_for):
            self.assertEqual(evaluate("preferences.indicator"), "bar")
            self.assertEqual(evaluate("document.querySelectorAll('.limit-card').length"), 2)
            self.assertEqual(evaluate("document.querySelectorAll('[role=meter]').length"), 3)
            self.assertEqual(
                evaluate("[...$('limit-options').querySelectorAll('input')].map(n=>n.value)"),
                ["10080", "300"],
            )
            window.page().runJavaScript(
                "$('limit-options').querySelector('[value=\"300\"]').click()"
            )
            wait_for("document.querySelectorAll('[role=meter]').length===2")
            self.assertEqual(evaluate("preferences.hiddenLimits"), [300])
            self.assertEqual(evaluate("accounts[1].windows.length"), 2)
            self.assertEqual(evaluate("selected().length"), 1)
            window.page().runJavaScript(
                "$('indicator-setting').value='ring';"
                "$('indicator-setting').dispatchEvent(new Event('change'))"
            )
            wait_for("document.documentElement.dataset.indicator==='ring'")
            self.assertNotEqual(
                evaluate("getComputedStyle(document.querySelector('.limit-gauge svg')).display"),
                "none",
            )
            # Temporary unavailability must not discard a display preference.
            claude = limits.snapshot.return_value["agents"][1]
            claude["windows"] = []
            wait_for("!$('limit-options').querySelector('[value=\"300\"]')")
            self.assertEqual(evaluate("preferences.hiddenLimits"), [300])
            claude["windows"] = account("claude", [(300, 0, now + 3600)])["windows"]
            wait_for("!!$('limit-options').querySelector('[value=\"300\"]')")
            self.assertFalse(
                evaluate("$('limit-options').querySelector('[value=\"300\"]').checked")
            )
            self.assertEqual(evaluate("document.querySelectorAll('.limit-card').length"), 1)
            window.page().runJavaScript(
                "$('limit-options').querySelector('[value=\"10080\"]').click()"
            )
            wait_for("$('limits-section').hidden")
            self.assertFalse(evaluate("$('activity').hidden"))
            window.page().runJavaScript(
                "$('limit-options').querySelector('[value=\"10080\"]').click()"
            )
            wait_for("!$('limits-section').hidden")
            wait_for("pending.size===0")
        with self.renderer(limits) as (_, evaluate, __):
            self.assertEqual(evaluate("preferences.indicator"), "ring")
            self.assertEqual(evaluate("preferences.hiddenLimits"), [300])
            self.assertEqual(evaluate("document.querySelectorAll('.limit-card').length"), 1)

    def test_weekly_cycles_select_agent_days_and_keep_history_combined(self):
        now = int(time.time())
        day = 86400
        rows = [
            {
                "id": identifier,
                "agent": agent,
                "start": start,
                "tokens": tokens,
                "cost": tokens / 1e4,
            }
            for identifier, agent, start, tokens in (
                ("codex-before", "codex", now - 4 * day - 1, 9000),
                ("codex-boundary", "codex", now - 4 * day, 100),
                ("claude-before", "claude", now - 2 * day - 1, 8000),
                ("claude-boundary", "claude", now - 2 * day, 200),
                ("codex-today", "codex", now, 300),
                ("claude-today", "claude", now, 400),
                ("codex-future", "codex", now + 3 * day, 7000),
            )
        ]
        self.usage.snapshot.return_value = {
            "rows": sorted(rows, key=lambda row: row["start"], reverse=True),
            "warnings": [],
        }
        limits = reporting(
            account("codex", [(10080, 30, now + 3 * day)], now),
            account("claude", [(10080, 30, now + 5 * day)], now),
        )
        with self.renderer(limits) as (window, evaluate, wait_for):
            window.page().runJavaScript("changePeriod('weekly')")
            wait_for("document.querySelectorAll('.heatmap').length===2")
            self.assertEqual(evaluate("selected().length"), 4)
            self.assertEqual(evaluate("$('total-tokens').textContent"), "1K")
            self.assertTrue(
                evaluate("""[...document.querySelectorAll('.heatmap-grid')]
                .every(grid=>grid.children.length>=7 && grid.children.length<=8)""")
            )
            self.assertEqual(
                evaluate("""new Set([...document.querySelectorAll('.heatmap-range')]
                .map(node=>node.textContent)).size"""),
                2,
            )
            # The same date has separate selections, totals and charts for each agent.
            for agent, expected in (("codex", "300"), ("claude", "400")):
                window.page().runJavaScript(
                    "[...document.querySelectorAll('.heat-cell')].find(cell=>"
                    f"cell.dataset.key===bucketKey({now},'{agent}')).click()"
                )
                wait_for(f"$('bucket-tokens').textContent==='{expected}'")
                self.assertEqual(evaluate("$('bucket-prompts').textContent"), "1")
                name = AGENTS[agent].name
                self.assertEqual(evaluate("charts.usage.data.datasets.map(d=>d.label)"), [name])
                self.assertTrue(evaluate("$('bucket-title').textContent").startswith(name + " · "))
                self.assertEqual(
                    evaluate("document.querySelectorAll('.heat-cell[aria-pressed=true]').length"), 1
                )
            selection = evaluate("bucketId")
            window.page().runJavaScript(
                "[...document.querySelectorAll('.heat-cell')].find(cell=>"
                f"cell.dataset.key===bucketKey({now},'codex'))"
                ".dispatchEvent(new MouseEvent('mouseover',{bubbles:true}))"
            )
            wait_for("!$('heatmap-tooltip').hidden")
            self.assertEqual(evaluate("bucketId"), selection)
            self.assertEqual(evaluate("$('bucket-tokens').textContent"), "400")
            self.assertIn("Codex", evaluate("$('heatmap-tooltip').textContent"))
            # Include the exact boundary; exclude even one second before it.
            window.page().runJavaScript(
                "[...document.querySelectorAll('.heat-cell')].find(cell=>"
                f"cell.dataset.key===bucketKey({now - 4 * day},'codex')).click()"
            )
            wait_for("$('bucket-tokens').textContent==='100'")
            window.page().runJavaScript(
                "$('limit-options').querySelector('[value=\"10080\"]').click()"
            )
            wait_for("$('limits-section').hidden")
            self.assertEqual(evaluate("document.querySelectorAll('.heatmap').length"), 2)
            self.assertEqual(evaluate("selected().length"), 4)
            # A reset replaces the old cycle and clears a day outside the new window.
            limits.snapshot.return_value["agents"][0]["windows"][0]["resets_at"] = (
                now + 7 * day - 10
            )
            wait_for("selected().length===3 && $('bucket-tokens').textContent==='300'")
            self.assertEqual(evaluate("$('total-tokens').textContent"), "900")
            # A missing cycle must not be invented from the other agent's reset.
            limits.snapshot.return_value["agents"][1]["windows"] = []
            wait_for("document.querySelectorAll('.heatmap').length===1")
            window.page().runJavaScript("changePeriod('all')")
            wait_for("$('heatmaps').dataset.period==='all'")
            self.assertEqual(evaluate("selected().length"), 6)
            self.assertEqual(evaluate("$('bucket-tokens').textContent"), "700")
            self.assertEqual(evaluate("charts.usage.data.datasets.length"), 2)
            self.assertEqual(
                evaluate("""getComputedStyle(document.querySelector('.heatmap'))
                .getPropertyValue('--heat').trim()"""),
                evaluate("color('neutral')"),
            )
            window.page().runJavaScript(
                "document.querySelector('.heat-cell[aria-pressed=true]')"
                ".dispatchEvent(new MouseEvent('mouseover',{bubbles:true}))"
            )
            wait_for("$('heatmap-tooltip').querySelectorAll('.tooltip-agent').length===2")
            self.assertIn("Codex · 300 tokens", evaluate("$('heatmap-tooltip').textContent"))
            self.assertIn("Claude Code · 400 tokens", evaluate("$('heatmap-tooltip').textContent"))
            # An agent filter only affects activity, in that agent's color.
            window.page().runJavaScript("$('agents').querySelector('[data-agent=claude]').click()")
            wait_for("selected().length===3")
            self.assertEqual(
                evaluate("""getComputedStyle(document.querySelector('.heatmap'))
                .getPropertyValue('--heat').trim()"""),
                AGENTS["claude"].colors[1],
            )
            self.assertEqual(evaluate("accounts[0].windows[0].used_percent"), 30)

    def test_only_packaged_assets_and_channel_are_accessible(self):
        interceptor = LocalOnly()
        for url, allowed in [
            (QUrl.fromLocalFile(str(STATIC / "index.html")), True),
            (QUrl.fromLocalFile(str(STATIC / "assets/app.js")), True),
            (QUrl("qrc:///qtwebchannel/qwebchannel.js"), True),
            (QUrl("https://example.com/"), False),
            (QUrl.fromLocalFile(str(self.root / "credentials.json")), False),
            (QUrl("qrc:///other.js"), False),
        ]:
            info = Mock()
            info.requestUrl.return_value = url
            interceptor.interceptRequest(info)
            info.block.assert_called_once_with(not allowed)
        self.assertTrue(
            LocalPage.acceptNavigationRequest(
                None, QUrl.fromLocalFile(str(STATIC / "index.html")), None, True
            )
        )
        self.assertFalse(
            LocalPage.acceptNavigationRequest(None, QUrl("https://example.com/"), None, True)
        )
        # The renderer learns agents from the registry, with packaged icons.
        self.bridge.request("1", "agents", "null")
        self.assertEqual([agent["id"] for agent in self.responses[-1]], list(AGENTS))
        for agent in self.responses[-1]:
            self.assertTrue((STATIC / agent["icon"]).is_file())
            self.assertEqual(agent["colors"], list(AGENTS[agent["id"]].colors))

    def test_preferences_and_directories_belong_to_agents(self):
        value = {"style": "glass", "pinned": True}
        self.bridge.request("1", "save_preferences", json.dumps(value))
        self.bridge.request("2", "preferences", "null")
        self.assertEqual(self.responses[-1], value)
        self.window.apply_preferences.assert_called_once_with(value)
        with patch("usage.desktop.QFileDialog.getExistingDirectory", return_value=""):
            self.bridge.request("3", "choose_directory", '"codex"')
        self.assertEqual(self.responses[-1], {"cancelled": True})
        self.usage.set_sources.assert_not_called()
        with patch(
            "usage.desktop.QFileDialog.getExistingDirectory", return_value=str(self.root)
        ) as choose:
            self.bridge.request("4", "choose_directory", '"claude"')
            QTest.qWait(40)
            choose.assert_called_once_with(
                self.window, "Claude Code history directory", str(self.root)
            )
        self.usage.set_sources.assert_called_once_with({"codex": "", "claude": str(self.root)})
        for request in ('"project"', '"../codex"', "null"):
            self.bridge.request("5", "choose_directory", request)
            self.assertIn("error", self.responses[-1])

    def test_connections_follow_each_agents_sign_in_and_reject_untrusted_requests(self):
        limits = self.bridge.limits
        limits.command.return_value = ["agent"]
        limits.snapshot.return_value = {"agents": []}
        limits.connect.return_value = {"windows": []}
        with (
            patch("usage.desktop.terminal_command", return_value=["terminal", "agent"]) as wrap,
            patch("usage.desktop.open_terminal") as terminal,
        ):
            self.bridge.request("1", "connect", '"claude"')
            QTest.qWait(40)
            self.assertEqual(self.responses[-1], {"agents": []})
            wrap.assert_called_once_with(["agent"])
            terminal.assert_called_once_with(["terminal", "agent"])
            limits.invalidate.assert_called_once_with(AGENTS["claude"])
            self.usage.set_connection.assert_called_once_with("claude", True)
            # Browser sign-ins stay inside the agent's native client.
            self.bridge.request("2", "connect", '"codex"')
            QTest.qWait(40)
            self.assertEqual(limits.connect.call_args.args[0], AGENTS["codex"])
            terminal.assert_called_once()
            limits.command.side_effect = FileNotFoundError
            self.bridge.request("3", "connect", '"claude"')
            self.assertEqual(
                self.responses[-1], {"error": "Install Claude Code in this environment."}
            )
            limits.command.side_effect = None
            self.window.config = {"demo": True}
            self.bridge.request("4", "connect", '"claude"')
            self.assertIn("error", self.responses[-1])
            self.window.config = {}
            self.bridge.request("5", "connect", '"arbitrary-command"')
            self.assertIn("error", self.responses[-1])
            self.assertEqual(terminal.call_count, 1)
            self.bridge.request("6", "cancel_login", '"codex"')
            limits.cancel_login.assert_called_once_with()

        with patch("usage.desktop.QDesktopServices.openUrl", return_value=True) as open_url:
            self.bridge.open_login("codex", "https://auth.openai.com/oauth/authorize")
            open_url.assert_called_once()
            open_url.reset_mock()
            for agent, url in (
                ("codex", "file:///tmp/command"),
                ("codex", "https://auth.openai.com@evil.example"),
                ("codex", "https://example.com"),
                ("claude", "https://auth.openai.com/oauth/authorize"),
            ):
                self.bridge.open_login(agent, url)
            open_url.assert_not_called()
            self.window.config = {"demo": True}
            self.bridge.open_login("codex", "https://auth.openai.com/oauth/authorize")
            open_url.assert_not_called()

    def test_window_stays_visible_without_focus_and_after_pinning(self):
        window = self.widget(Mock())
        try:
            window.show()
            QCoreApplication.sendEvent(window, QEvent(QEvent.Type.WindowDeactivate))
            self.assertTrue(window.isVisible())
            for pinned in (True, False):
                window.apply_preferences({"pinned": pinned, "scale": 1.1})
                self.assertTrue(window.isVisible())
                self.assertEqual(
                    bool(window.windowFlags() & Qt.WindowType.WindowStaysOnTopHint), pinned
                )
                self.assertEqual(window.zoomFactor(), 1)
            self.assertTrue(window.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground))
            self.assertEqual(window.page().backgroundColor().alpha(), 0)
            window.bridge.request("layout", "layout", '{"height": 300}')
            self.assertEqual((window.width(), window.height()), (WIDTH, 300))
            layouts = []
            window.bridge.layout_changed.connect(lambda w, s: layouts.append((w, s)))
            screen = Mock()
            with patch.object(window, "screen", return_value=screen):
                screen.availableGeometry.return_value = QRect(0, 0, 1920, 1080)
                window.bridge.request("layout", "layout", '{"height": 825}')
                self.assertEqual((window.width(), window.height()), (WIDTH, 825))
                self.assertEqual(layouts[-1], (WIDTH, 1))
                # Smaller monitors fit the whole panel; larger ones restore its natural size.
                for area in (
                    QRect(0, 0, 1280, 600),
                    QRect(0, 0, 250, 900),
                    QRect(0, 0, 1920, 1080),
                ):
                    screen.availableGeometry.return_value = area
                    window.apply_size()
                    width, scale = layouts[-1]
                    self.assertLessEqual(width * scale, window.width())
                    self.assertLessEqual(825 * scale, window.height())
                    self.assertTrue(area.contains(window.geometry()))
                self.assertEqual(layouts[-1], (WIDTH, 1))
                window.bridge.request("layout", "layout", '{"height": 9000}')
                self.assertLessEqual(9000 * layouts[-1][1], window.height())
            for invalid in ({"height": 0}, {"height": -1}, {"height": 10001}, {"height": True},
                            {"height": "300"}, {}, None):  # fmt: skip
                window.bridge.request("layout", "layout", json.dumps(invalid))
                self.assertEqual(window.content_height, 9000)
            window.close()
            self.assertFalse(window.isVisible())
        finally:
            self.dispose(window)

    def test_zoom_scales_the_window_and_survives_restart(self):
        self.settings.setValue("appearance", json.dumps({"theme": "light", "zoom": 1.25}))
        self.settings.sync()
        window = self.widget(Mock())
        layouts = []
        window.bridge.layout_changed.connect(lambda w, s: layouts.append((w, s)))
        screen = Mock()
        screen.availableGeometry.return_value = QRect(0, 0, 1920, 1080)
        try:
            self.assertEqual(window.zoomFactor(), 1.25)
            with patch.object(window, "screen", return_value=screen):
                window.bridge.request("layout", "layout", '{"height": 400}')
                self.assertEqual((window.width(), window.height()), (430, 500))
                for zoom in (2, 0.5, 1.1, 1):
                    window.apply_preferences({"zoom": zoom})
                    self.assertEqual(window.zoomFactor(), zoom)
                    self.assertEqual(
                        (window.width(), window.height()),
                        (math.ceil(WIDTH * zoom), math.ceil(400 * zoom)),
                    )
                    self.assertEqual(layouts[-1], (WIDTH, 1))
                # A zoomed panel still fits a smaller screen, as the natural size does.
                screen.availableGeometry.return_value = QRect(0, 0, 1280, 600)
                window.apply_preferences({"zoom": 2})
                width, scale = layouts[-1]
                self.assertLess(scale, 1)
                self.assertLessEqual(width * scale * 2, window.width())
                self.assertLessEqual(400 * scale * 2, window.height())
                self.assertTrue(screen.availableGeometry().contains(window.geometry()))
                # Unknown or out-of-range values restore the natural size.
                screen.availableGeometry.return_value = QRect(0, 0, 1920, 1080)
                for invalid in (0.25, 2.5, True, "1.5", None, float("nan")):
                    window.apply_preferences({"zoom": 1.5})
                    window.apply_preferences({"zoom": invalid})
                    self.assertEqual(window.zoomFactor(), 1)
                    self.assertEqual((window.width(), window.height()), (WIDTH, 400))
        finally:
            self.dispose(window)
        for damaged in ("not json", json.dumps([1.5]), json.dumps({"zoom": 9})):
            self.settings.setValue("appearance", damaged)
            self.settings.sync()
            window = self.widget(Mock())
            try:
                self.assertEqual(window.zoomFactor(), 1)
            finally:
                self.dispose(window)

    def test_keys_wheel_and_settings_zoom_the_whole_window(self):
        now = time.time()
        self.usage.snapshot.return_value = {
            "rows": [
                {"id": agent, "agent": agent, "start": now, "tokens": 100, "cost": 0.1}
                for agent in AGENTS
            ],
            "warnings": [],
        }
        limits = reporting(*[account(agent, [(300, 50, now + 3600)], now) for agent in AGENTS])
        screen = Mock()
        screen.availableGeometry.return_value = QRect(0, 0, 1920, 1080)
        with self.renderer(limits) as (window, evaluate, wait_for):
            with patch.object(window, "screen", return_value=screen):
                window.apply_size()
                wait_for(PANEL_FITS)
                natural = (window.width(), window.height())
                page = window.focusProxy()

                def press(key):
                    QTest.keyClick(page, key, Qt.KeyboardModifier.ControlModifier)

                def scroll(notches, modifiers=Qt.KeyboardModifier.ControlModifier):
                    position = QPointF(100, 100)
                    event = QWheelEvent(
                        position,
                        window.mapToGlobal(position),
                        QPoint(),
                        QPoint(0, 120 * notches),
                        Qt.MouseButton.NoButton,
                        modifiers,
                        Qt.ScrollPhase.NoScrollPhase,
                        False,
                    )
                    QApplication.sendEvent(page, event)

                def zoomed(factor):
                    wait_for(f"preferences.zoom==={factor}")
                    deadline = time.monotonic() + 10
                    while time.monotonic() < deadline and not math.isclose(
                        window.zoomFactor(), factor, abs_tol=1e-3
                    ):
                        QTest.qWait(20)
                    self.assertAlmostEqual(window.zoomFactor(), factor, places=3)
                    wait_for(PANEL_FITS)
                    self.assertEqual(window.width(), math.ceil(WIDTH * factor))

                for key, factor in (
                    (Qt.Key.Key_Equal, 1.1),
                    (Qt.Key.Key_Plus, 1.25),
                    (Qt.Key.Key_Minus, 1.1),
                ):
                    press(key)
                    zoomed(factor)
                self.assertGreater(window.height(), natural[1])
                # Each notch is one step at every zoom, though the page sees smaller deltas.
                for notches, factor in ((1, 1.25), (1, 1.5), (1, 1.75), (1, 2), (1, 2), (-1, 1.75)):
                    scroll(notches)
                    zoomed(factor)
                for _ in range(3):
                    scroll(-1)
                zoomed(1.1)
                # Plain scrolling never zooms.
                scroll(1, Qt.KeyboardModifier.NoModifier)
                QTest.qWait(200)
                zoomed(1.1)
                press(Qt.Key.Key_0)
                zoomed(1)
                self.assertEqual((window.width(), window.height()), natural)
                # Settings offers the same steps and keeps the open dialog inside the window.
                window.page().runJavaScript(
                    "openSettings();$('zoom-setting').value='1.5';"
                    "$('zoom-setting').dispatchEvent(new Event('change'))"
                )
                zoomed(1.5)
                self.assertEqual(evaluate("$('zoom-setting').value"), "1.5")
                self.settings.sync()
                self.assertEqual(json.loads(self.settings.value("appearance"))["zoom"], 1.5)
                window.page().runJavaScript("$('settings').close()")
                zoomed(1.5)
                # Chromium's own zoom is reverted, so the window never falls out of step.
                window.page().setZoomFactor(0.8)
                QTest.qWait(300)
                zoomed(1.5)

    def test_later_launch_reveals_the_hidden_widget(self):
        name = instance_name(self.root)
        window = self.widget(Mock())
        try:
            window.launches = Launches(window, name)
            window.show()
            window.bridge.request("hide", "hide", "null")
            self.assertFalse(window.isVisible())
            self.assertTrue(window.preferences.value("hidden", type=bool))
            self.assertEqual(self.launch(name, None), [True])
            self.assertTrue(window.isVisible())
            self.assertFalse(window.preferences.value("hidden", type=bool))
            # A visible widget is only brought forward.
            self.assertEqual(self.launch(name, None), [True])
            self.assertTrue(window.isVisible())
            # Anything but the one request is dropped without an answer.
            window.hide()
            strangers = ("quit\n", REVEAL.decode().strip(), REVEAL.decode() * 2, "x" * 4096)
            self.assertEqual(self.launch(name, *strangers), [""] * len(strangers))
            self.assertFalse(window.isVisible())
        finally:
            self.dispose(window)
        # With nothing listening, the caller falls back to its message.
        self.assertFalse(reveal_running(name, timeout=200))
        self.assertNotEqual(instance_name(self.root / "other"), name)

    def test_second_launch_reveals_the_first_or_explains_why_it_cannot(self):
        with (
            patch("usage.desktop.user_directory", return_value=self.root),
            patch("usage.desktop.QLockFile") as lock,
            patch("usage.desktop.QMessageBox.information") as message,
            patch("usage.desktop.Usage") as usage,
        ):
            lock.return_value.tryLock.return_value = False
            for answered in (True, False):
                with patch("usage.desktop.reveal_running", return_value=answered) as reveal:
                    run({"widget": {"width": WIDTH}})
                    reveal.assert_called_once_with(instance_name(self.root))
                self.assertEqual(message.call_count, 0 if answered else 1)
            usage.assert_not_called()

    def test_tray_mirrors_limits_and_controls_the_widget(self):
        now = time.time()
        limits = reporting(
            account("claude", [(300, 14, now + 7200), (10080, 42, now + 345600)], now),
            account("codex", [(10080, 86, now + 172800)], now),
        )
        window = self.widget(limits)
        tray = window.tray
        available = "usage.tray.QSystemTrayIcon.isSystemTrayAvailable"
        try:
            window.preferences.setValue("appearance", json.dumps({"limitAlert": 80}))
            # Only Windows with a notification area gets icons, and never the demo.
            for platform, tray_available, demo in (
                ("linux", True, False),
                ("win32", False, False),
                ("win32", True, True),
            ):
                with (
                    patch("usage.tray.sys.platform", platform),
                    patch(available, return_value=tray_available),
                    patch.dict(window.config, {"demo": demo}),
                ):
                    self.assertFalse(tray.start())
            self.assertFalse(tray.timer.isActive())
            with patch("usage.tray.sys.platform", "win32"), patch(available, return_value=True):
                self.assertTrue(tray.start())
            self.assertTrue(tray.timer.isActive())
            # Before limits arrive, the icon is the app's own.
            self.assertIsNone(tray.face)
            self.assertEqual(tray.icon.toolTip(), "Agent Usage")
            deadline = time.monotonic() + 10
            while tray.payload is None and time.monotonic() < deadline:
                QTest.qWait(10)
            # Nothing chosen: the app's own icon; hovering lists every limit.
            self.assertIsNone(tray.face)
            self.assertEqual(
                tray.icon.toolTip().splitlines(),
                ["Agent Usage", "Claude Code: 5-hour 14%, Weekly 42%", "Codex: Weekly 86%"],
            )
            # A click opens the panel with every limit and its reset.
            flyout = tray.flyout

            def shown(kind):
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                return flyout.content.findChildren(kind)

            def chosen():
                return [button.isChecked() for button in shown(QRadioButton)]

            tray.activated(QSystemTrayIcon.ActivationReason.Trigger)
            self.assertTrue(flyout.isVisible())
            area = flyout.screen().availableGeometry()
            self.assertTrue(area.contains(flyout.geometry()))
            texts = [label.text() for label in shown(QLabel)]
            for text in ("Claude Code", "Codex", "5-hour limit", "14%", "Weekly limit", "42%"):
                self.assertIn(text, texts)
            self.assertIn("Resets in 2h 0m", texts)
            self.assertEqual(chosen(), [False, False, False])
            # The icon shows the one chosen limit; another replaces it, the same one clears it.
            claude = AGENTS["claude"].colors[1]
            for index, face, key, state in (
                (2, ("86", "#f8e45c", False), "codex::10080", [False, False, True]),
                (0, ("14", claude, False), "claude::300", [True, False, False]),
                (0, None, "", [False, False, False]),
                (2, ("86", "#f8e45c", False), "codex::10080", [False, False, True]),
            ):
                QTest.mouseClick(shown(QRadioButton)[index], Qt.MouseButton.LeftButton)
                self.assertTrue(flyout.isVisible())
                self.assertEqual(tray.face, face)
                self.assertEqual(window.preferences.value("tray_limit"), key)
                self.assertEqual(chosen(), state)

            # Opened from an icon on a bottom taskbar, the panel sits just above it, and
            # stays there while new limits rebuild it or a reopen anchors it elsewhere.
            def placed(anchor):
                QTest.qWait(50)
                self.assertEqual(flyout.geometry().bottom() + 1 + FLYOUT_MARGIN, anchor.top())
                self.assertEqual(flyout.height(), flyout.sizeHint().height())

            for left in (area.center().x() - 12, area.center().x() + 100):
                anchor = QRect(left, area.bottom() - 24, 24, 24)
                flyout.open(anchor)
                placed(anchor)
            codex = account("codex", [(10080, 86, now + 172800)], now)
            full = account("claude", [(300, 100, now + 600), (10080, 42, now + 345600)], now)
            stale = account("codex", [(10080, 86, now + 172800)], now, status="stale")
            # The chosen limit stays on the icon even when another one is higher; while it
            # is not reported, the app's icon stands in.
            for agents, face in (
                ([full, codex], ("86", "#f8e45c", False)),
                ([stale], ("86", AGENTS["codex"].colors[1], True)),
                ([full], None),
                ([codex], ("86", "#f8e45c", False)),
            ):
                tray.receive({"agents": agents})
                placed(anchor)
                self.assertEqual(tray.face, face)
            # The choice survives a restart.
            restarted = self.widget(limits)
            try:
                restarted.tray.receive(limits.snapshot())
                self.assertEqual(restarted.tray.face, ("86", "#f8e45c", False))
            finally:
                self.dispose(restarted)
            # Escape closes the panel; the click that closes it must not reopen it at once.
            QTest.keyClick(flyout, Qt.Key.Key_Escape)
            self.assertFalse(flyout.isVisible())
            tray.activated(QSystemTrayIcon.ActivationReason.Trigger)
            self.assertFalse(flyout.isVisible())
            QTest.qWait(int(REOPEN_SECONDS * 1000) + 50)
            tray.activated(QSystemTrayIcon.ActivationReason.Trigger)
            self.assertTrue(flyout.isVisible())
            tray.activated(QSystemTrayIcon.ActivationReason.Trigger)
            self.assertFalse(flyout.isVisible())
            # Without limits the icon is the app's own again.
            tray.receive({"agents": []})
            self.assertIsNone(tray.face)
            self.assertEqual(window.preferences.value("tray_limit"), "codex::10080")
            # A long summary is cut at whole lines, within what Windows displays.
            tray.receive(
                {
                    "agents": [
                        account(agent, [(300, 50, now + 3600), (10080, 50, now + 3600)], now)
                        for agent in AGENTS
                    ]
                    * 4
                }
            )
            self.assertLessEqual(len(tray.icon.toolTip()), 127)
            # The panel's footer and the right-click menu show or hide the widget, and quit.
            window.show()
            QTest.qWait(int(REOPEN_SECONDS * 1000) + 50)
            tray.activated(QSystemTrayIcon.ActivationReason.Trigger)
            buttons = {button.text(): button for button in shown(QPushButton)}
            self.assertEqual(sorted(buttons), ["Hide widget", "Quit"])
            buttons["Hide widget"].click()
            self.assertFalse(flyout.isVisible())
            self.assertFalse(window.isVisible())
            self.assertTrue(window.preferences.value("hidden", type=bool))
            tray.activated(QSystemTrayIcon.ActivationReason.Context)
            self.assertFalse(flyout.isVisible())
            tray.fill_menu()
            labels = [action.text() for action in tray.menu.actions()]
            self.assertEqual(labels, ["Show widget", "Quit"])
            tray.menu.actions()[0].trigger()
            self.assertTrue(window.isVisible())
            self.assertFalse(window.preferences.value("hidden", type=bool))
            with patch.object(QApplication, "quit") as quit:
                tray.fill_menu()
                self.assertEqual(tray.menu.actions()[0].text(), "Hide widget")
                tray.menu.actions()[-1].trigger()
                QTest.qWait(int(REOPEN_SECONDS * 1000) + 50)
                flyout.signature = None  # rebuild, so the button reaches the patched quit
                tray.activated(QSystemTrayIcon.ActivationReason.Trigger)
                {button.text(): button for button in shown(QPushButton)}["Quit"].click()
                self.assertEqual(quit.call_count, 2)
            tray.stop()
            self.assertFalse(tray.timer.isActive())
            self.assertFalse(flyout.isVisible())
        finally:
            self.dispose(window)

    def test_notification_area_leaves_limits_out_of_the_widget(self):
        now = time.time()
        self.usage.snapshot.return_value = {"rows": [], "warnings": []}
        limits = reporting(account("claude", [(300, 10, now + 3600)], now))
        rows = "['visible-limits','indicator-row','limit-setting'].map(id=>$(id).hidden)"
        with self.renderer(limits) as (window, evaluate, wait_for):
            wait_for("!$('limits-section').hidden")
            self.assertEqual(evaluate(rows), [False, False, False])
            # As with the GNOME top bar, limits the icon shows are not repeated; only the
            # warning, which colors the icon, still configures them.
            with (
                patch("usage.tray.sys.platform", "win32"),
                patch("usage.tray.QSystemTrayIcon.isSystemTrayAvailable", return_value=True),
            ):
                self.assertTrue(window.tray.start())
            wait_for("$('limits-section').hidden")
            self.assertEqual(evaluate(rows), [True, True, False])
            window.tray.stop()
            wait_for("!$('limits-section').hidden")
            self.assertEqual(evaluate(rows), [False, False, False])

    def test_top_bar_leaves_limits_out_and_day_summary_expands_on_request(self):
        now = time.time()
        self.usage.snapshot.return_value = {
            "rows": [
                {"id": "a", "agent": "claude", "start": now, "tokens": 1500, "cost": 0.25},
                {"id": "b", "agent": "claude", "start": now - 1, "tokens": 500},
                {"id": "c", "agent": "claude", "start": now - 86400, "tokens": None},
            ],
            "warnings": [],
        }
        limits = reporting(account("claude", [(300, 10, now + 3600)], now))

        def grows_from(height):
            deadline = time.monotonic() + 10
            while window.height() <= height and time.monotonic() < deadline:
                QTest.qWait(20)
            return window.height() > height

        with self.renderer(limits) as (window, evaluate, wait_for):
            wait_for("!$('limits-section').hidden")
            # Limits move to the top bar; only the warning still configures it.
            window.top_bar.attached = True
            wait_for("$('limits-section').hidden")
            self.assertEqual(
                evaluate(
                    "['visible-limits','indicator-row','limit-setting'].map(id=>$(id).hidden)"
                ),
                [True, True, False],
            )
            window.page().runJavaScript(
                "$('theme-setting').value='light';"
                "$('theme-setting').dispatchEvent(new Event('change'))"
            )
            wait_for("document.documentElement.dataset.theme==='light'")
            # Explanations live behind Settings' help button, not on the widget.
            window.page().runJavaScript("openSettings();$('help-toggle').click()")
            wait_for("!$('help').hidden")
            self.assertEqual(evaluate("$('help-toggle').getAttribute('aria-expanded')"), "true")
            window.page().runJavaScript("$('settings').close()")
            # Today's totals are its summary line, instead of a second toolbar total.
            window.page().runJavaScript("changePeriod('today')")
            wait_for("$('bucket-title').textContent==='Today'")
            self.assertTrue(evaluate("$('totals').hidden && $('detail-body').hidden"))
            self.assertEqual(
                evaluate(
                    "['bucket-prompts','bucket-prompt-unit','bucket-tokens','bucket-cost']"
                    ".map(id=>$(id).textContent)"
                ),
                ["2", "prompts", "2K", "$0.25"],
            )
            # Measure only once the window has shrunk from Settings, or growth goes unseen.
            wait_for(PANEL_FITS)
            height = window.height()
            window.page().runJavaScript("$('detail-toggle').click()")
            wait_for("!$('detail-body').hidden && !$('prompt-inspector').hidden")
            self.assertEqual(evaluate("$('detail-toggle').getAttribute('aria-expanded')"), "true")
            self.assertTrue(grows_from(height))
            # Unknown measurements leave the summary instead of showing placeholders.
            window.page().runJavaScript("$('period').querySelector('[data-period=all]').click()")
            wait_for("preferences.period==='all' && !$('heatmaps').hidden")
            self.assertEqual(
                evaluate("[...$('period').children].map(b=>b.getAttribute('aria-pressed'))"),
                ["false", "false", "true"],
            )
            window.page().runJavaScript(
                "[...document.querySelectorAll('.heat-cell')].find(cell=>"
                f"cell.dataset.key===bucketKey({now - 86400})).click()"
            )
            wait_for("$('bucket-prompt-unit').textContent==='prompt'")
            self.assertEqual(
                evaluate("[$('bucket-tokens'),$('bucket-cost')].map(n=>n.parentElement.hidden)"),
                [True, True],
            )
            wait_for("pending.size===0")
        with self.renderer(limits) as (_, evaluate, wait_for):
            wait_for("!$('detail-body').hidden")
            self.assertEqual(evaluate("[preferences.details,preferences.theme]"), [True, "light"])

    def test_top_bar_mirrors_limits_and_controls_widget_visibility(self):
        now = time.time()
        limits = reporting(
            account("claude", [(300, 14, now + 3600), (10080, 42, now + 3600)], now),
            dict(id="codex", status="unavailable", issue="not_found", windows=[], connected=False),
        )
        self.usage.connections = {}
        calls, answer = [], [True]

        def gnome_call(method, *args):
            calls.append((method, *args))
            return answer[0]

        def sent():
            return [args[0] for method, *args in calls if method == "SetLimits"]

        def published():
            return json.loads(sent()[-1])

        def wait_for(condition):
            deadline = time.monotonic() + 10
            while not condition() and time.monotonic() < deadline:
                QTest.qWait(10)
            self.assertTrue(condition())

        with (
            patch("usage.desktop.gnome_session", return_value=True),
            patch("usage.desktop.gnome_commands") as commands,
            patch("usage.desktop.gnome_call", side_effect=gnome_call),
        ):
            window = self.widget(limits)
            try:
                with patch("usage.desktop.gnome_session", return_value=False):
                    self.assertFalse(TopBar(window).start() or calls)
                window.preferences.setValue(
                    "appearance", json.dumps({"limitAlert": 80, "hiddenLimits": [300]})
                )
                window.preferences.setValue("hidden", True)
                pid = os.getpid()
                # An answering top bar keeps a hidden widget hidden from the first launch.
                self.assertTrue(window.top_bar.start())
                self.assertEqual(published()["agents"], [])
                wait_for(lambda: published()["agents"])
                self.assertEqual(commands.call_args.args[1], SLOT("command(int,QString)"))
                self.assertGreaterEqual(
                    window.top_bar.metaObject().indexOfSlot("command(int,QString)"), 0
                )
                payload = published()
                self.assertEqual((payload["warning"], payload["visible"]), (80, False))
                # The bar offers every limit; the widget's own filter does not apply to it.
                self.assertEqual(
                    payload["agents"][0],
                    {
                        "id": "claude",
                        "name": "Claude Code",
                        "color": AGENTS["claude"].colors[1],
                        "issue": None,
                        "status": "available",
                        "limits": [
                            {
                                "id": f"claude::{minutes}",
                                "bucket": None,
                                "minutes": minutes,
                                "used_percent": used,
                                "resets_at": now + 3600,
                            }
                            for minutes, used in ((300, 14), (10080, 42))
                        ],
                    },
                )
                self.assertEqual(payload["agents"][1]["issue"], "not_found")
                count = len(sent())
                window.top_bar.refresh()
                wait_for(lambda: window.top_bar.task.done())
                QTest.qWait(20)
                self.assertEqual(len(sent()), count)
                window.top_bar.command(pid + 1, "toggle")
                self.assertFalse(window.isVisible())
                window.top_bar.command(pid, "toggle")
                self.assertTrue(window.isVisible())
                self.assertFalse(window.preferences.value("hidden", type=bool))
                self.assertTrue(published()["visible"])
                window.bridge.request("1", "save_preferences", json.dumps({"limitAlert": 90}))
                self.assertEqual(published()["warning"], 90)
                window.bridge.request("hide", "hide", "null")
                self.assertFalse(window.isVisible() or published()["visible"])
                self.assertTrue(window.preferences.value("hidden", type=bool))
                window.top_bar.command(pid, "toggle")
                self.assertTrue(window.isVisible() and published()["visible"])
                window.top_bar.command(pid, "toggle")
                self.assertFalse(window.isVisible() or published()["visible"])
                self.assertTrue(window.preferences.value("hidden", type=bool))
                # A locked screen unexports the extension; unchanged limits are retried.
                answer[0] = False
                window.top_bar.sent -= TOP_BAR_HEARTBEAT
                count = len(sent())
                window.top_bar.publish()
                window.top_bar.publish()
                self.assertEqual(len(sent()), count + 2)
                self.assertTrue(window.top_bar.attached)
                answer[0] = True
                window.top_bar.publish()
                window.top_bar.publish()
                self.assertEqual(len(sent()), count + 3)
                window.top_bar.sent -= TOP_BAR_HEARTBEAT
                window.top_bar.publish()
                self.assertEqual(len(sent()), count + 4)
                # Disabling the extension restores the widget without changing the preference.
                answer[0] = False
                window.top_bar.command(pid, "detach")
                self.assertTrue(window.isVisible())
                self.assertFalse(window.top_bar.attached)
                self.assertTrue(window.preferences.value("hidden", type=bool))
                with patch("usage.desktop.QApplication") as application:
                    window.top_bar.command(pid, "quit")
                application.instance.return_value.quit.assert_called_once_with()
                window.top_bar.stop()
                window.top_bar.stop()
                self.assertEqual(sent()[-1], "")
                self.assertEqual(sent().count(""), 1)
                window.config["demo"] = True
                count = len(calls)
                self.assertFalse(TopBar(window).start())
                self.assertEqual(len(calls), count)
            finally:
                self.dispose(window)

    def test_visible_content_controls_height_without_overflow(self):
        now = time.time()
        self.usage.snapshot.return_value = {
            "rows": [
                {"id": agent, "agent": agent, "start": now, "tokens": 100, "cost": 0.1}
                for agent in AGENTS
            ],
            "warnings": [],
        }
        limits = reporting(
            *[
                account(agent, [(300, 50, now + 3600), (10080, 50, now + 3600)], now)
                for agent in AGENTS
            ]
        )
        bounds = PANEL_FITS
        screen = Mock()
        screen.availableGeometry.return_value = QRect(0, 0, 1920, 1080)
        with self.renderer(limits) as (window, evaluate, wait_for):
            with patch.object(window, "screen", return_value=screen):
                window.apply_size()
                geometry = """(()=>{
                    const box = selector => {
                        const r=document.querySelector(selector).getBoundingClientRect();
                        return [r.x,r.y,r.width,r.height];
                    };
                    return [innerWidth,innerHeight,
                        box($('settings').open?'dialog':'.widget'),
                        box($('settings').open?'#settings-close':'#widget-close')];
                })()"""
                window.page().runJavaScript("changePeriod('all')")
                QTest.qWait(100)
                for style in ("minimal", "glass", "brutalist", "neumorphic", "swiss"):
                    for theme in ("light", "dark"):
                        window.page().runJavaScript(
                            f"preferences.style='{style}';preferences.theme='{theme}';"
                            "bucketId=null;applyTheme();render();reportSize()"
                        )
                        QTest.qWait(80)
                        wait_for(bounds)
                        baseline = evaluate(geometry)
                        claude = AGENTS["claude"].colors[theme == "dark"]
                        self.assertEqual(
                            evaluate("""getComputedStyle(document.querySelector(
                                '.limit-card[data-agent="claude"]')).getPropertyValue('--accent')"""),
                            claude,
                        )
                        self.assertEqual(
                            evaluate("""charts.usage.data.datasets.find(
                                d=>d.label==='Claude Code').borderColor"""),
                            claude,
                        )
                        self.assertEqual(
                            evaluate("""new Set([...document.querySelectorAll(
                                '.limit-card,.detail-card')].map(n=>
                                getComputedStyle(n).backgroundColor)).size"""),
                            1,
                        )
                        # Empty days keep the summary's size; hidden Settings reserve none.
                        window.page().runJavaScript(
                            "[...document.querySelectorAll('.heat-cell')].find(c=>!c.disabled&&"
                            "c.dataset.level==='0').click()"
                        )
                        QTest.qWait(80)
                        wait_for(bounds)
                        self.assertEqual(evaluate(geometry), baseline)
                        window.page().runJavaScript("openSettings()")
                        QTest.qWait(80)
                        wait_for(bounds)
                        settings_geometry = evaluate(geometry)
                        self.assertNotEqual(settings_geometry[1], baseline[1])
                        self.assertEqual(settings_geometry[-1], baseline[-1])
                        for agent in AGENTS:
                            window.page().runJavaScript(f"$('source-details-{agent}').open=true")
                            QTest.qWait(80)
                            wait_for(bounds)
                            self.assertGreater(window.height(), settings_geometry[1])
                            self.assertEqual(evaluate(geometry)[-1], settings_geometry[-1])
                            window.page().runJavaScript(f"$('source-details-{agent}').open=false")
                            QTest.qWait(80)
                            wait_for(bounds)
                            self.assertEqual(evaluate(geometry), settings_geometry)
                        window.page().runJavaScript("$('settings').close()")
                        QTest.qWait(80)
                        wait_for(bounds)
                        self.assertEqual(evaluate(geometry), baseline)
                        self.assertEqual(evaluate("$('bucket-prompts').textContent"), "0")
                        self.assertTrue(evaluate("$('bucket-tokens').parentElement.hidden"))
                # Period changes shrink and grow in both directions without retaining a maximum.
                heights = {}
                for period in ("all", "weekly", "today", "weekly", "all"):
                    window.page().runJavaScript(f"changePeriod('{period}')")
                    QTest.qWait(100)
                    wait_for(bounds)
                    if period in heights:
                        self.assertEqual(window.height(), heights[period])
                    heights[period] = window.height()
                self.assertLess(heights["today"], heights["weekly"])
                self.assertLess(heights["today"], heights["all"])
                # A smaller monitor still fits every active panel without a vertical scrollbar.
                screen.availableGeometry.return_value = QRect(0, 0, 1280, 600)
                window.apply_size()
                for script in (
                    "changePeriod('all')",
                    "openSettings();$('source-details-codex').open=true",
                    "$('source-details-codex').open=false",
                    "$('settings').close()",
                    "changePeriod('today')",
                ):
                    window.page().runJavaScript(script)
                    QTest.qWait(100)
                    wait_for(bounds)
                    self.assertTrue(screen.availableGeometry().contains(window.geometry()))
                # Restoring a larger screen also restores the natural size.
                screen.availableGeometry.return_value = QRect(0, 0, 1920, 1080)
                window.apply_size()
                QTest.qWait(100)
                wait_for(bounds)
                self.assertEqual(window.width(), WIDTH)
                self.assertEqual(
                    evaluate(
                        "Number(getComputedStyle(document.documentElement).getPropertyValue('--layout-scale'))"
                    ),
                    1,
                )
                # Diagnostics name each agent and update while it still reports no limits.
                for agent, issue, label in (
                    ("codex", "not_found", "Not installed"),
                    ("codex", "login_required", "Not connected"),
                    ("codex", "unsupported_account", "Subscription required"),
                    ("claude", "login_required", "Not connected"),
                    ("claude", "unsupported_version", "Update Claude Code"),
                ):
                    window.page().runJavaScript(
                        f"accounts=[{{id:'{agent}',windows:[],issue:'{issue}'}}];render()"
                    )
                    wait_for(
                        "[...document.querySelectorAll('.limit-unavailable .caption')]"
                        f".some(node=>node.textContent==='{label}')"
                    )
                # Account updates reach Settings and the limits without any terminal.
                source = Limits(load_config())
                source.query = Mock(
                    return_value={"windows": [], "updated_at": None, "issue": "login_required"}
                )
                self.usage.connections = {"codex": False, "claude": True}
                window.bridge.limits = source
                window.page().runJavaScript("openSettings()")
                wait_for("$('connection-claude').textContent==='Not connected'")
                limits = {
                    "five_hour": {"utilization": 0, "resets_at": "2099-01-01T00:00:00Z"},
                    "seven_day": {"utilization": 3, "resets_at": "2099-01-07T00:00:00Z"},
                }
                source.query.return_value = {
                    "windows": Claude.windows({"rate_limits": limits}),
                    "updated_at": time.time(),
                }
                source.checked.clear()
                wait_for("$('connection-claude').textContent==='Connected'")
                self.assertEqual(
                    evaluate("""[...document.querySelectorAll(
                    '.limit-card[data-agent="claude"] .limit-percent')]
                    .map(node=>node.textContent)"""),
                    ["0%", "3%"],
                )
                self.assertEqual(source.query.call_count, 2)

    def test_backup_limits_sources_and_failure_redaction(self):
        archive = {"format": "agent-usage", "version": 3, "records": []}
        self.usage.export.return_value = archive
        path = self.root / "backup.json"
        self.bridge.execute("export", {"path": str(path)})
        self.assertEqual(json.loads(path.read_text()), archive)
        self.bridge.execute("import", {"path": str(path)})
        self.usage.import_data.assert_called_once_with(archive)
        with path.open("wb") as stream:
            stream.truncate(MAX_IMPORT + 1)
        with self.assertRaises(ValueError):
            self.bridge.execute("import", {"path": str(path)})
        self.bridge.execute("choose_directory", {"agent": "codex", "path": str(self.root)})
        self.usage.set_sources.assert_called_with({"codex": str(self.root), "claude": ""})
        for method in ("shell", "read_file", "save_sources"):
            with self.assertRaises(ValueError):
                self.bridge.execute(method, {})
        future = Future()
        future.set_exception(RuntimeError("PRIVATE CONTENT"))
        with self.assertLogs(level="WARNING") as logs:
            self.bridge.finish("4", future)
        self.assertNotIn("PRIVATE", str(self.responses) + str(logs.output))

    def test_window_scheduler_is_one_switch_and_an_editable_chart(self):
        now = time.time()
        today = datetime.fromtimestamp(now).date()
        rows = []
        for age in range(1, 36):
            day = today - timedelta(days=age)
            for minute in range(9 * 60, 17 * 60, 20) if day.weekday() < 5 else ():
                moment = datetime(day.year, day.month, day.day, minute // 60, minute % 60)
                rows.append(
                    {
                        "id": f"p{age}-{minute}",
                        "agent": "claude",
                        "start": moment.astimezone().timestamp(),
                        "tokens": 30000,
                    }
                )
        self.usage.snapshot.return_value = {"rows": rows, "warnings": []}
        self.usage.connections = {"claude": True, "codex": True}
        limits = reporting(
            account("claude", [(10080, 25, now + 400000)], now),
            account("codex", [(300, 10, now + 3600), (10080, 40, now + 400000)], now),
        )
        with self.renderer(limits) as (window, evaluate, wait_for):
            run = window.page().runJavaScript
            run("openSettings()")
            wait_for("$('settings').open && !!scheduler?.agents?.claude")
            self.assertEqual(evaluate("$('scheduler-summary-claude').textContent"), "Off")
            for removed in ("mode", "guard", "sim", "history", "status"):
                self.assertIsNone(evaluate(f"$('scheduler-{removed}-claude')"))
            self.assertIsNone(evaluate("$('activate-claude') || $('simulate-claude')"))

            run("$('scheduler-details-claude').open=true")
            wait_for("!!charts['scheduler-claude']?.$scheduler")
            self.assertTrue(
                evaluate("""(()=>{
                    const card=$('scheduler-details-claude').closest('.agent-card').getBoundingClientRect();
                    const box=$('scheduler-chart-claude').getBoundingClientRect();
                    return box.width>0 && box.left>=card.left && box.right<=card.right;
                })()""")
            )
            self.assertEqual(
                evaluate("$('scheduler-source-claude').textContent"), "Learned · drag to adjust"
            )
            self.assertTrue(evaluate("$('scheduler-reset-claude').hidden"))
            learned = evaluate("scheduler.agents.claude.plan.starts")
            self.assertEqual(len(learned), 1)
            # Half-hour steps: the drawn use starts exactly at the first slot with prompts.
            self.assertEqual(
                evaluate("charts['scheduler-claude'].data.datasets[0].stepped"), "middle"
            )
            self.assertEqual(
                evaluate("""(()=>{
                    const act=scheduler.agents.claude.plan.activity, first=act.findIndex(v=>v>0.02);
                    const points=charts['scheduler-claude'].data.datasets[0].data;
                    return points.findIndex(p=>p.y>0.02)-1===first && points[first+1].x-0.25===first/2;
                })()"""),
                True,
            )

            # The switch alone turns it on, and the summary names the next start.
            run("$('scheduler-enabled-claude').click()")
            wait_for("scheduler.agents.claude.schedule.enabled===true")
            self.assertEqual(evaluate("scheduler.agents.claude.status"), "scheduled")
            self.assertRegex(
                evaluate("$('scheduler-summary-claude').textContent"),
                r"^(Next|Tomorrow|[A-Z][a-z]{2}) \d\d:\d\d$",
            )

            # One start begins the chain: press the chart to set it, drag it, click it to go back.
            run("""window.pointer = (type, hour) => {
                const chart = charts['scheduler-claude'], canvas = chart.canvas;
                const rect = canvas.getBoundingClientRect(), ratio = rect.width / canvas.offsetWidth;
                canvas.dispatchEvent(new PointerEvent(type, {
                    bubbles: true, pointerId: 1, button: 0, buttons: type === 'pointerup' ? 0 : 1,
                    clientX: rect.left + chart.scales.x.getPixelForValue(hour) * ratio,
                    clientY: rect.top + 30,
                }));
            }""")
            run("pointer('pointerdown', 15); pointer('pointerup', 15)")
            wait_for("JSON.stringify(scheduler.agents.claude.schedule.times)==='[\"15:00\"]'")
            self.assertEqual(evaluate("$('scheduler-source-claude').textContent"), "Your times")
            self.assertFalse(evaluate("$('scheduler-reset-claude').hidden"))
            # The planned chain runs from the next start, window after window, until midnight.
            self.assertTrue(
                evaluate("""(()=>{
                    const overlay=charts['scheduler-claude'].$scheduler, chain=overlay.chain;
                    return chain.length>0 && chain[0].start===overlay.next
                        && chain[0].start>=Math.min(900, overlay.now ?? 900)
                        && chain.at(-1).end>=1440
                        && chain.every((w,i)=>i===0 || w.start===chain[i-1].end);
                })()""")
            )
            self.assertEqual(
                evaluate("[...$('scheduler-legend-claude').children].map(e=>e.textContent).join()"),
                "Open,Ended,Planned",
            )
            run("pointer('pointerdown', 15); pointer('pointermove', 16); pointer('pointerup', 16)")
            wait_for("JSON.stringify(scheduler.agents.claude.schedule.times)==='[\"16:00\"]'")
            run("pointer('pointerdown', 16); pointer('pointerup', 16)")
            wait_for("scheduler.agents.claude.schedule.times===null")
            self.assertEqual(
                evaluate("JSON.stringify(scheduler.agents.claude.plan.starts)"), json.dumps(learned)
            )
            run("pointer('pointerdown', 15); pointer('pointerup', 15)")
            wait_for("scheduler.agents.claude.schedule.times?.length===1")
            run("$('scheduler-reset-claude').click()")
            wait_for("scheduler.agents.claude.schedule.times===null")
            self.assertTrue(evaluate("scheduler.agents.claude.schedule.enabled"))

            run("$('settings').close()")
            wait_for("!$('settings').open")
            self.assertEqual(evaluate("document.querySelectorAll('.limit-scheduler').length"), 0)
