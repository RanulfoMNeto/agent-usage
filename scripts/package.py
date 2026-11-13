"""Build a portable directory bundle using PyInstaller's native Qt hooks."""

import importlib.metadata
import platform
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Qt bindings the app never imports, on any platform (QtDBus serves GNOME, so it stays).
# Their PyInstaller hooks would bundle every QML plugin and the Qt libraries those load,
# none of which the widget opens; the Qt libraries Chromium needs are still collected
# from the binaries that link them. A test keeps this list apart from the app's imports.
UNUSED_MODULES = ("QtOpenGL", "QtPositioning", "QtQml", "QtQuick", "QtQuickWidgets")
# Qt WebEngine data the widget never reads: debug-build resources, the DevTools UI
# (remote debugging still works), Qt's UI translations and Chromium's non-English
# strings. The widget is English; Chromium falls back to en-US, which stays.
UNUSED_DATA = (
    "*.debug.pak",
    "qtwebengine_devtools_resources.pak",
    "translations/*.qm",
    "qtwebengine_locales/*.pak",
)
KEPT_DATA = ("qtwebengine_locales/en-US.pak",)


def prune(bundle):
    """Delete unused Qt data from a built bundle; returns the removed files' paths."""
    kept = {path for pattern in KEPT_DATA for path in bundle.rglob(pattern)}
    removed = sorted(
        {path for pattern in UNUSED_DATA for path in bundle.rglob(pattern) if path not in kept}
    )
    for path in removed:
        path.unlink()
    return removed


def main():
    options = []
    if sys.platform == "win32":
        icon = ROOT / "usage/static/assets/widget.ico"
        options = ["--hide-console", "hide-early", "--icon", str(icon)]
    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--name",
            "agent-usage",
            "--paths",
            str(ROOT),
            "--collect-data",
            "usage",
            *(arg for name in UNUSED_MODULES for arg in ("--exclude-module", f"PySide6.{name}")),
            *options,
            "usage/__main__.py",
        ],
        cwd=ROOT,
        check=True,
    )
    bundle = ROOT / "dist/agent-usage"
    removed = prune(bundle)
    print(f"Pruned {len(removed)} unused Qt files")
    for name in ("LICENSE", "README.md"):
        shutil.copy2(ROOT / name, bundle / name)
    shutil.copytree(ROOT / ".github/assets", bundle / ".github/assets", dirs_exist_ok=True)
    licenses = bundle / "licenses"
    licenses.mkdir(exist_ok=True)
    python_license = Path(sysconfig.get_path("stdlib")) / "LICENSE.txt"
    if python_license.exists():
        shutil.copy2(python_license, licenses / "Python.txt")
    for name in (
        "pyinstaller",
        "shiboken6",
        "PySide6-Essentials",
        "PySide6-Addons",
        "PySide6-WebEngine",
    ):
        distribution = importlib.metadata.distribution(name)
        for path in distribution.files or []:
            if "licenses" in path.parts:
                target = licenses / name / path.name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(distribution.locate_file(path), target)
    system = "windows" if sys.platform == "win32" else "linux"
    architecture = platform.machine().lower().replace("amd64", "x86_64")
    archive = ROOT / "dist" / f"agent-usage-{system}-{architecture}"
    archive_format = "zip" if sys.platform == "win32" else "gztar"
    print(shutil.make_archive(str(archive), archive_format, bundle.parent, bundle.name))


if __name__ == "__main__":
    main()
