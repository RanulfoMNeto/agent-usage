import json
import os
import tempfile
import time
import unittest
from concurrent.futures import Future
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
        with patch("usage.desktop.user_directory", return_value=self.root):
            window = Widget({"width": 400, "compact_width": 300}, self.usage, subscriptions)

        def evaluate(expression):
            result = []
            window.page().runJavaScript("JSON.stringify(" + expression + ")", result.append)
            deadline = time.monotonic() + 10
            while not result and time.monotonic() < deadline:
                QTest.qWait(10)
            self.assertTrue(result, "Renderer did not respond")
            return json.loads(result[0])

        def wait_for(expression):
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if evaluate(expression):
                    return
                QTest.qWait(20)
            self.fail("Layout did not settle: " + expression)

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
        try:
            with patch.object(window, "screen", return_value=screen):
                window.show()
                wait_for("typeof data!=='undefined' && !!data && subscriptionsLoaded")
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
                            "[...$('heatmap-grid').children].find(c=>!c.disabled&&"
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
                        window.page().runJavaScript("$('source-settings').open=true")
                        QTest.qWait(80)
                        wait_for(bounds)
                        self.assertGreater(window.height(), settings_geometry[1])
                        window.page().runJavaScript("$('source-settings').open=false")
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
                self.assertLess(heights["weekly"], heights["all"])
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
                    "openSettings();$('source-settings').open=true",
                    "$('source-settings').open=false",
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
                for issue, label in (
                    ("not_found", "Codex not found"),
                    ("login_required", "Sign in required"),
                    ("unsupported_account", "ChatGPT login required"),
                ):
                    window.page().runJavaScript(
                        f"providers=[{{agent:'Codex',windows:[],issue:'{issue}'}}];render()"
                    )
                    wait_for(
                        "[...document.querySelectorAll('.quota-unavailable .caption')]"
                        f".some(node=>node.textContent==='{label}')"
                    )
        finally:
            window.close()
            window.bridge.close()
            window.page().deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            window.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

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
