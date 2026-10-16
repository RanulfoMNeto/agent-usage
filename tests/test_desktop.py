import json
import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import Future
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, QRect, QSettings, Qt, QUrl
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from usage.config import load_config, user_directory
from usage.desktop import STATIC, Bridge, LocalOnly, LocalPage, Widget
from usage.history import MAX_IMPORT
from usage.readers import project_path
from usage.subscriptions import Subscriptions, claude_snapshot


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
                    self.assertEqual(config["transcripts"]["codex"], "~/.codex")
                with patch("sys.platform", "win32"):
                    self.assertEqual(user_directory("state"), Path(folder) / "AgentUsage")
                path = Path(folder) / "config.toml"
                path.write_text("[widget]\nwidth=350\n", encoding="utf-8")
                self.assertEqual(load_config(path)["widget"]["width"], 350)
                path.write_text("[widget]\nwidth=0\n", encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_config(path)


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
            "codex": {"path": str(self.root), "override": "", "exists": True},
            "claude": {"path": str(self.root), "override": "", "exists": True},
        }
        self.bridge = Bridge(self.usage, Mock(), self.settings, self.window)
        self.responses = []
        self.bridge.reply.connect(lambda _, body: self.responses.append(json.loads(body)))

    def tearDown(self):
        self.bridge.close()
        self.window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    @contextmanager
    def renderer(self, subscriptions):
        with patch("usage.desktop.user_directory", return_value=self.root):
            window = Widget({"width": 400, "compact_width": 300}, self.usage, subscriptions)

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
            wait_for("typeof data!=='undefined' && !!data && subscriptionsLoaded")
            yield window, evaluate, wait_for
        finally:
            window.close()
            window.bridge.close()
            window.page().deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            window.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_limit_preferences_preserve_collection_and_survive_restart(self):
        now = time.time()
        self.usage.snapshot.return_value = {
            "rows": [{"id": "prompt", "agent": "Codex", "start": now, "tokens": 123}],
            "warnings": [],
        }
        subscriptions = Mock()
        subscriptions.snapshot.return_value = {
            "providers": [
                {
                    "agent": agent,
                    "status": "available",
                    "updated_at": now,
                    "windows": [
                        {
                            "name": agent,
                            "minutes": minutes,
                            "resets_at": now + 3600,
                            "used_percent": used,
                        }
                        for minutes, used in limits
                    ],
                }
                for agent, limits in (
                    ("Codex", [(10080, 56)]),
                    ("Claude Code", [(300, 0), (10080, 3)]),
                )
            ]
        }
        with self.renderer(subscriptions) as (window, evaluate, wait_for):
            self.assertEqual(evaluate("preferences.indicator"), "bar")
            self.assertEqual(evaluate("document.querySelectorAll('.quota-card').length"), 2)
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
            self.assertEqual(evaluate("providers[1].windows.length"), 2)
            self.assertEqual(evaluate("selected().length"), 1)
            window.page().runJavaScript(
                "$('indicator-setting').value='ring';"
                "$('indicator-setting').dispatchEvent(new Event('change'))"
            )
            wait_for("document.documentElement.dataset.indicator==='ring'")
            self.assertNotEqual(
                evaluate("getComputedStyle(document.querySelector('.quota-gauge svg')).display"),
                "none",
            )
            # Temporary unavailability must not discard a display preference.
            subscriptions.snapshot.return_value["providers"][1]["windows"] = []
            wait_for("!$('limit-options').querySelector('[value=\"300\"]')")
            self.assertEqual(evaluate("preferences.hiddenLimits"), [300])
            subscriptions.snapshot.return_value["providers"][1]["windows"] = [
                {"name": "Claude", "minutes": 300, "resets_at": now + 3600, "used_percent": 0}
            ]
            wait_for("!!$('limit-options').querySelector('[value=\"300\"]')")
            self.assertFalse(
                evaluate("$('limit-options').querySelector('[value=\"300\"]').checked")
            )
            self.assertEqual(evaluate("document.querySelectorAll('.quota-card').length"), 1)
            window.page().runJavaScript(
                "$('limit-options').querySelector('[value=\"10080\"]').click()"
            )
            wait_for("$('subscriptions-section').hidden")
            self.assertFalse(evaluate("$('activity').hidden"))
            window.page().runJavaScript(
                "$('limit-options').querySelector('[value=\"10080\"]').click()"
            )
            wait_for("!$('subscriptions-section').hidden")
            wait_for("pending.size===0")
        with self.renderer(subscriptions) as (_, evaluate, __):
            self.assertEqual(evaluate("preferences.indicator"), "ring")
            self.assertEqual(evaluate("preferences.hiddenLimits"), [300])
            self.assertEqual(evaluate("document.querySelectorAll('.quota-card').length"), 1)

    def test_weekly_cycles_select_agent_days_and_keep_history_combined(self):
        now = int(time.time())
        day = 86400
        rows = [
            {
                "id": identifier,
                "agent": agent,
                "start": start,
                "tokens": tokens,
                "cost": tokens / 10000,
                "project": "/example",
            }
            for identifier, agent, start, tokens in (
                ("codex-before", "Codex", now - 4 * day - 1, 9000),
                ("codex-boundary", "Codex", now - 4 * day, 100),
                ("claude-before", "Claude Code", now - 2 * day - 1, 8000),
                ("claude-boundary", "Claude Code", now - 2 * day, 200),
                ("codex-today", "Codex", now, 300),
                ("claude-today", "Claude Code", now, 400),
                ("codex-future", "Codex", now + 3 * day, 7000),
            )
        ]
        self.usage.snapshot.return_value = {
            "rows": sorted(rows, key=lambda row: row["start"], reverse=True),
            "warnings": [],
        }
        subscriptions = Mock()
        subscriptions.snapshot.return_value = {
            "providers": [
                {
                    "agent": agent,
                    "status": "available",
                    "updated_at": now,
                    "windows": [
                        {
                            "name": agent,
                            "minutes": 10080,
                            "resets_at": now + days * day,
                            "used_percent": 30,
                        }
                    ],
                }
                for agent, days in (("Codex", 3), ("Claude Code", 5))
            ]
        }
        with self.renderer(subscriptions) as (window, evaluate, wait_for):
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
            for agent, expected in (("Codex", "300"), ("Claude Code", "400")):
                window.page().runJavaScript(
                    "[...document.querySelectorAll('.heat-cell')].find(cell=>"
                    f"cell.dataset.key===bucketKey({now},'{agent}')).click()"
                )
                wait_for(f"$('bucket-tokens').textContent==='{expected}'")
                self.assertEqual(evaluate("$('bucket-prompts').textContent"), "1")
                self.assertEqual(evaluate("charts.usage.data.datasets.map(d=>d.label)"), [agent])
                self.assertTrue(
                    evaluate("$('bucket-title').textContent").startswith(
                        agent.replace(" Code", "") + " · "
                    )
                )
                self.assertEqual(
                    evaluate("document.querySelectorAll('.heat-cell[aria-pressed=true]').length"), 1
                )
            selection = evaluate("bucketId")
            window.page().runJavaScript(
                "[...document.querySelectorAll('.heat-cell')].find(cell=>"
                f"cell.dataset.key===bucketKey({now},'Codex'))"
                ".dispatchEvent(new MouseEvent('mouseover',{bubbles:true}))"
            )
            wait_for("!$('heatmap-tooltip').hidden")
            self.assertEqual(evaluate("bucketId"), selection)
            self.assertEqual(evaluate("$('bucket-tokens').textContent"), "400")
            self.assertIn("Codex", evaluate("$('heatmap-tooltip').textContent"))
            # Include the exact boundary; exclude even one second before it.
            window.page().runJavaScript(
                "[...document.querySelectorAll('.heat-cell')].find(cell=>"
                f"cell.dataset.key===bucketKey({now - 4 * day},'Codex')).click()"
            )
            wait_for("$('bucket-tokens').textContent==='100'")
            window.page().runJavaScript(
                "$('limit-options').querySelector('[value=\"10080\"]').click()"
            )
            wait_for("$('subscriptions-section').hidden")
            self.assertEqual(evaluate("document.querySelectorAll('.heatmap').length"), 2)
            self.assertEqual(evaluate("selected().length"), 4)
            # A reset replaces the old cycle and clears a day outside the new window.
            subscriptions.snapshot.return_value["providers"][0]["windows"][0]["resets_at"] = (
                now + 7 * day - 10
            )
            wait_for("selected().length===3 && $('bucket-tokens').textContent==='300'")
            self.assertEqual(evaluate("$('total-tokens').textContent"), "900")
            # A missing cycle must not be invented from the other agent's reset.
            subscriptions.snapshot.return_value["providers"][1]["windows"] = []
            wait_for("document.querySelectorAll('.heatmap').length===1")
            self.assertEqual(
                evaluate("$('chart-coverage').textContent"), "Claude: weekly cycle unavailable."
            )
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
            self.assertIn("Claude · 400 tokens", evaluate("$('heatmap-tooltip').textContent"))
            # Project filtering only affects activity, and empty days stay selectable.
            window.page().runJavaScript("preferences.project='/missing';render()")
            wait_for("$('bucket-prompts').textContent==='0'")
            self.assertEqual(evaluate("$('usage-empty').textContent"), "No recorded prompts")
            self.assertEqual(evaluate("providers[0].windows[0].used_percent"), 30)

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

    def test_preferences_and_file_dialog_cancellation(self):
        value = {"view": "compact", "style": "glass", "pinned": True}
        self.bridge.request("1", "save_preferences", json.dumps(value))
        self.bridge.request("2", "preferences", "null")
        self.assertEqual(self.responses[-1], value)
        self.window.apply_preferences.assert_called_once_with(value)
        with patch("usage.desktop.QFileDialog.getExistingDirectory", return_value=""):
            self.bridge.request("3", "choose_directory", '"codex"')
        self.assertEqual(self.responses[-1], {"cancelled": True})
        self.usage.set_sources.assert_not_called()

    def test_connections_open_native_terminal_and_reject_untrusted_requests(self):
        self.bridge.subscriptions.config = {"claude_command": ["claude"]}
        self.bridge.subscriptions.lock = threading.Lock()
        self.bridge.subscriptions.checked = {"claude": 123}
        self.bridge.subscriptions.snapshot.return_value = {"providers": []}
        with (
            patch("usage.desktop.claude_terminal_command", return_value=["claude"]),
            patch("usage.desktop.open_claude_terminal") as terminal,
        ):
            self.bridge.request("1", "connect", '"claude"')
            QTest.qWait(40)
            self.assertEqual(self.responses[-1], {"providers": []})
            terminal.assert_called_once_with(["claude"])
            self.usage.set_connection.assert_called_once_with("claude", True)
            self.assertNotIn("claude", self.bridge.subscriptions.checked)
            terminal.reset_mock()
            self.window.config = {"demo": True}
            self.bridge.request("2", "connect", '"claude"')
            self.assertIn("error", self.responses[-1])
            terminal.assert_not_called()
            self.window.config = {}
            self.bridge.request("3", "connect", '"arbitrary-command"')
            self.assertIn("error", self.responses[-1])
            terminal.assert_not_called()
            self.bridge.request("4", "cancel_login", '"codex"')
            self.bridge.subscriptions.cancel_login.assert_called_once_with()

        with patch("usage.desktop.QDesktopServices.openUrl", return_value=True) as open_url:
            self.bridge.open_login("https://auth.openai.com/oauth/authorize")
            open_url.assert_called_once()
            open_url.reset_mock()
            for url in (
                "file:///tmp/command",
                "https://auth.openai.com@evil.example",
                "https://example.com",
            ):
                self.bridge.open_login(url)
            open_url.assert_not_called()
            self.window.config = {"demo": True}
            self.bridge.open_login("https://auth.openai.com/oauth/authorize")
            open_url.assert_not_called()

    def test_project_picker_preserves_sources_and_cancelled_selection(self):
        self.settings.setValue("appearance", json.dumps({"project": str(self.root)}))
        with patch(
            "usage.desktop.QFileDialog.getExistingDirectory", return_value=str(self.root)
        ) as choose:
            self.bridge.request("1", "choose_directory", '"project"')
            choose.assert_called_once_with(self.window, "Project directory", str(self.root))
        self.assertEqual(self.responses[-1], {"path": project_path(str(self.root))})
        self.usage.set_sources.assert_not_called()
        with patch("usage.desktop.QFileDialog.getExistingDirectory", return_value=""):
            self.bridge.request("2", "choose_directory", '"project"')
        self.assertEqual(self.responses[-1], {"cancelled": True})
        self.assertEqual(json.loads(self.settings.value("appearance"))["project"], str(self.root))

    def test_window_stays_visible_without_focus_and_after_pinning(self):
        with (
            patch("usage.desktop.user_directory", return_value=self.root),
            patch.object(Widget, "load"),
        ):
            window = Widget({"width": 400, "compact_width": 300}, self.usage, Mock())
        try:
            window.show()
            QCoreApplication.sendEvent(window, QEvent(QEvent.Type.WindowDeactivate))
            self.assertTrue(window.isVisible())
            for pinned in (True, False):
                window.apply_preferences({"pinned": pinned, "view": "compact", "scale": 1.1})
                self.assertTrue(window.isVisible())
                self.assertEqual(
                    bool(window.windowFlags() & Qt.WindowType.WindowStaysOnTopHint), pinned
                )
                self.assertEqual(window.zoomFactor(), 1)
            self.assertTrue(window.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground))
            self.assertEqual(window.page().backgroundColor().alpha(), 0)
            window.bridge.request("layout", "layout", '{"height": 300, "compact": false}')
            self.assertEqual(window.height(), 300)
            self.assertEqual(window.width(), 400)
            window.bridge.request("layout", "layout", '{"height": 180, "compact": true}')
            self.assertEqual(window.height(), 180)
            self.assertEqual(window.width(), 300)
            self.assertEqual(window.zoomFactor(), 1)
            layouts = []
            window.bridge.layout_changed.connect(lambda w, s, _: layouts.append((w, s)))
            screen = Mock()
            with patch.object(window, "screen", return_value=screen):
                screen.availableGeometry.return_value = QRect(0, 0, 1920, 1080)
                window.bridge.request("layout", "layout", '{"height": 825, "compact": false}')
                self.assertEqual((window.width(), window.height()), (400, 825))
                self.assertEqual(layouts[-1], (400, 1))
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
                self.assertEqual(layouts[-1], (400, 1))
                window.bridge.request("layout", "layout", '{"height": 9000, "compact": false}')
                self.assertLessEqual(9000 * layouts[-1][1], window.height())
            for invalid in (
                {"height": 0, "compact": True},
                {"height": -1, "compact": True},
                {"height": 10001, "compact": True},
                {"height": True, "compact": True},
                {"height": "300", "compact": True},
                {"height": 300, "compact": "true"},
                {"height": 300},
                None,
            ):
                window.bridge.request("layout", "layout", json.dumps(invalid))
                self.assertEqual(window.content_height, 9000)
                self.assertFalse(window.compact)
            window.close()
            self.assertFalse(window.isVisible())
        finally:
            window.bridge.close()
            window.page().deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            window.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_visible_content_controls_height_without_overflow(self):
        now = time.time()
        self.usage.snapshot.return_value = {
            "rows": [
                {"id": agent, "agent": agent, "start": now, "tokens": 100, "cost": 0.1}
                for agent in ("Codex", "Claude Code")
            ],
            "warnings": [],
        }
        subscriptions = Mock()
        subscriptions.snapshot.return_value = {
            "providers": [
                {
                    "agent": agent,
                    "status": "available",
                    "updated_at": now,
                    "windows": [
                        {
                            "name": agent,
                            "minutes": minutes,
                            "resets_at": now + 3600,
                            "used_percent": 50,
                        }
                        for minutes in (300, 10080)
                    ],
                }
                for agent in ("Codex", "Claude Code")
            ]
        }
        bounds = """(()=>{
            const p=$('settings').open?$('settings'):document.querySelector('.widget');
            const r=p.getBoundingClientRect();
            return r.width>0 && r.height>0 && r.left>=0 && r.top>=0 &&
                r.right<=innerWidth && r.bottom<=innerHeight &&
                p.scrollHeight<=p.clientHeight+1 &&
                document.documentElement.scrollHeight<=innerHeight+1 &&
                document.documentElement.scrollWidth<=innerWidth+1;
        })()"""
        screen = Mock()
        screen.availableGeometry.return_value = QRect(0, 0, 1920, 1080)
        with self.renderer(subscriptions) as (window, evaluate, wait_for):
            with patch.object(window, "screen", return_value=screen):
                window.apply_size()
                geometry = """(()=>{
                    const box = selector => {
                        const r=document.querySelector(selector).getBoundingClientRect();
                        return [r.x,r.y,r.width,r.height];
                    };
                    return [innerWidth,innerHeight,
                        box($('settings').open?'dialog':'.widget'),
                        box($('settings').open?'dialog .icon-button':'#widget-close')];
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
                        self.assertEqual(evaluate("color('claude')"), "#d97757")
                        self.assertNotEqual(evaluate("color('codex')"), "#d97757")
                        self.assertEqual(
                            evaluate("""getComputedStyle(document.querySelector(
                                '.quota-card[data-agent="Claude Code"] .fill')).stroke"""),
                            "rgb(217, 119, 87)",
                        )
                        self.assertEqual(
                            evaluate("""charts.usage.data.datasets.find(
                                d=>d.label==='Claude Code').borderColor"""),
                            "#d97757",
                        )
                        self.assertEqual(
                            evaluate("""new Set([...document.querySelectorAll(
                                '.quota-card,.detail-card')].map(n=>
                                getComputedStyle(n).backgroundColor)).size"""),
                            1,
                        )
                        # Empty days retain the chart's space; hidden Settings reserve none.
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
                        self.assertLess(settings_geometry[1], baseline[1])
                        self.assertEqual(settings_geometry[-1], baseline[-1])
                        window.page().runJavaScript("$('source-details-codex').open=true")
                        QTest.qWait(80)
                        wait_for(bounds)
                        self.assertGreater(window.height(), settings_geometry[1])
                        window.page().runJavaScript("$('source-details-codex').open=false")
                        QTest.qWait(80)
                        wait_for(bounds)
                        self.assertEqual(evaluate(geometry), settings_geometry)
                        window.page().runJavaScript("$('source-details-claude').open=true")
                        QTest.qWait(80)
                        wait_for(bounds)
                        self.assertGreater(window.height(), settings_geometry[1])
                        self.assertEqual(evaluate(geometry)[-1], settings_geometry[-1])
                        window.page().runJavaScript("$('source-details-claude').open=false")
                        QTest.qWait(80)
                        wait_for(bounds)
                        self.assertEqual(evaluate(geometry), settings_geometry)
                        window.page().runJavaScript("$('settings').close()")
                        QTest.qWait(80)
                        wait_for(bounds)
                        self.assertEqual(evaluate(geometry), baseline)
                        self.assertEqual(evaluate("$('bucket-prompts').textContent"), "0")
                        self.assertEqual(evaluate("$('bucket-tokens').textContent"), "—")
                        self.assertTrue(evaluate("!$('usage-empty').hidden"))
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
                # Use the actual preference control to exercise the native width handshake.
                window.page().runJavaScript(
                    "$('view-setting').value='compact';"
                    "$('view-setting').dispatchEvent(new Event('change'))"
                )
                QTest.qWait(100)
                wait_for(bounds)
                self.assertTrue(window.compact)
                compact_height = window.height()
                compact_close = evaluate(geometry)[-1]
                window.page().runJavaScript("openSettings()")
                QTest.qWait(100)
                wait_for(bounds)
                self.assertNotEqual(window.height(), compact_height)
                self.assertEqual(evaluate(geometry)[-1], compact_close)
                window.page().runJavaScript("$('settings').close()")
                QTest.qWait(100)
                wait_for(bounds)
                self.assertEqual(window.height(), compact_height)
                window.page().runJavaScript(
                    "$('view-setting').value='normal';"
                    "$('view-setting').dispatchEvent(new Event('change'));$('settings').close()"
                )
                QTest.qWait(100)
                wait_for(bounds)
                self.assertFalse(window.compact)
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
                self.assertEqual(window.width(), 400)
                self.assertEqual(
                    evaluate(
                        "Number(getComputedStyle(document.documentElement).getPropertyValue('--layout-scale'))"
                    ),
                    1,
                )
                # Diagnostics must update even when the same agent still has no quota windows.
                for agent, issue, label in (
                    ("Codex", "not_found", "Not installed"),
                    ("Codex", "login_required", "Not connected"),
                    ("Codex", "unsupported_account", "Subscription required"),
                    ("Claude Code", "login_required", "Not connected"),
                    ("Claude Code", "unsupported_version", "Update Claude Code"),
                ):
                    window.page().runJavaScript(
                        f"providers=[{{agent:'{agent}',windows:[],issue:'{issue}'}}];render()"
                    )
                    wait_for(
                        "[...document.querySelectorAll('.quota-unavailable .caption')]"
                        f".some(node=>node.textContent==='{label}')"
                    )
                # Native account updates must reach Settings and gauges without terminal capture.
                source = Subscriptions(load_config()["subscriptions"])
                source.query = Mock(
                    return_value={
                        "agent": "Claude Code",
                        "windows": [],
                        "updated_at": None,
                        "issue": "login_required",
                    }
                )
                self.usage.connections = {"codex": False, "claude": True}
                window.bridge.subscriptions = source
                window.page().runJavaScript("openSettings()")
                wait_for("$('claude-connection').textContent==='Not connected'")
                source.query.return_value = claude_snapshot(
                    {
                        "rate_limits": {
                            "five_hour": {"utilization": 0, "resets_at": "2099-01-01T00:00:00Z"},
                            "seven_day": {"utilization": 3, "resets_at": "2099-01-07T00:00:00Z"},
                        }
                    },
                    time.time(),
                )
                source.checked.clear()
                wait_for("$('claude-connection').textContent==='Connected'")
                self.assertEqual(
                    evaluate("""[...document.querySelectorAll(
                    '.quota-card[data-agent="Claude Code"] .quota-percent')]
                    .map(node=>node.textContent)"""),
                    ["0%", "3%"],
                )
                self.assertEqual(source.query.call_count, 2)

    def test_backup_limits_sources_and_failure_redaction(self):
        archive = {"format": "agent-usage", "version": 1, "records": []}
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
