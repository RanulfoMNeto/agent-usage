import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.package import UNUSED_MODULES, prune

ROOT = Path(__file__).resolve().parents[1]


class PackageTests(unittest.TestCase):
    def test_excluded_qt_modules_are_never_imported_by_the_app(self):
        excluded = {f"PySide6.{name}" for name in UNUSED_MODULES}
        # Every import in the source, including platform-only ones inside functions.
        written = {
            match
            for path in (ROOT / "usage").rglob("*.py")
            for match in re.findall(
                r"(?:from|import)\s+(PySide6\.Qt\w+)", path.read_text(encoding="utf-8")
            )
        }
        self.assertTrue(written)
        self.assertEqual(written & excluded, set())
        # Qt bindings also import each other when loaded, which the source cannot show.
        script = (
            "import importlib, json, pkgutil, sys, usage;"
            "[importlib.import_module(m.name) for m in pkgutil.walk_packages("
            "usage.__path__, 'usage.')];"
            "print(json.dumps(sorted(m for m in sys.modules if m.startswith('PySide6.'))))"
        )
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT,
            env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
        loaded = set(json.loads(result.stdout.splitlines()[-1]))
        self.assertIn("PySide6.QtWebEngineWidgets", loaded)
        self.assertEqual(loaded & excluded, set())

    def test_pruning_keeps_what_qt_webengine_reads_on_either_layout(self):
        kept = [
            "agent-usage.exe",
            "_internal/usage/static/assets/app.js",
            # Windows wheel layout.
            "_internal/PySide6/resources/qtwebengine_resources.pak",
            "_internal/PySide6/resources/icudtl.dat",
            "_internal/PySide6/translations/qtwebengine_locales/en-US.pak",
            "_internal/PySide6/Qt6WebEngineCore.dll",
            # Linux wheel layout.
            "_internal/PySide6/Qt/resources/qtwebengine_resources.pak",
            "_internal/PySide6/Qt/translations/qtwebengine_locales/en-US.pak",
            "_internal/PySide6/Qt/libexec/QtWebEngineProcess",
        ]
        removed = [
            "_internal/PySide6/resources/qtwebengine_resources.debug.pak",
            "_internal/PySide6/resources/qtwebengine_devtools_resources.pak",
            "_internal/PySide6/resources/qtwebengine_devtools_resources.debug.pak",
            "_internal/PySide6/translations/qt_pt_BR.qm",
            "_internal/PySide6/translations/qtwebengine_locales/pt-BR.pak",
            "_internal/PySide6/Qt/resources/qtwebengine_resources.debug.pak",
            "_internal/PySide6/Qt/translations/qtbase_de.qm",
            "_internal/PySide6/Qt/translations/qtwebengine_locales/de.pak",
        ]
        with tempfile.TemporaryDirectory() as folder:
            bundle = Path(folder)
            for name in kept + removed:
                (bundle / name).parent.mkdir(parents=True, exist_ok=True)
                (bundle / name).write_bytes(b"x")
            self.assertEqual(
                sorted(path.relative_to(bundle).as_posix() for path in prune(bundle)),
                sorted(removed),
            )
            remaining = sorted(
                path.relative_to(bundle).as_posix() for path in bundle.rglob("*") if path.is_file()
            )
            self.assertEqual(remaining, sorted(kept))
