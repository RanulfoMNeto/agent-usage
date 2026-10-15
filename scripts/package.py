"""Build a portable directory bundle using PyInstaller's native Qt hooks."""

import importlib.metadata
import platform
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    options = ["--hide-console", "hide-early"] if sys.platform == "win32" else []
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
            *options,
            "usage/__main__.py",
        ],
        cwd=ROOT,
        check=True,
    )
    bundle = ROOT / "dist/agent-usage"
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
