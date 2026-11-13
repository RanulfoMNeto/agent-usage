import json
import os
import shutil
import struct
import subprocess
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRect, QSize
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from scripts import icon
from usage.tray import (
    FLYOUT_MARGIN,
    FLYOUT_WIDTH,
    ICON_SIZES,
    LEVELS,
    badge,
    placement,
    summarize,
    text_color,
)

NOW = 1_800_000_000
# The fields both desktops present; the menu and icons are built from these alone.
FIELDS = ("id", "agent", "label", "value", "level", "fill", "stale", "reset", "selected")


def limit(agent, minutes, used, hours, bucket=None):
    return dict(
        id=f"{agent}:{bucket or ''}:{minutes}",
        bucket=bucket,
        minutes=minutes,
        used_percent=used,
        resets_at=NOW + hours * 3600,
    )


def agent(id, name, limits, **extra):
    return {
        "id": id,
        "name": name,
        "color": "#123456",
        "status": "available",
        "limits": limits,
    } | extra


PAYLOAD = dict(
    warning=80,
    visible=True,
    agents=[
        agent("beta", "Beta", [limit("beta", 10080, 100, 48)]),
        agent("alpha", "Alpha", [limit("alpha", 10080, 42, 96), limit("alpha", 300, 86.7, 1.2)]),
        agent("stale", "Stale", [limit("stale", 300, 95, 2)], status="stale"),
        agent("gone", "Gone", [limit("gone", 300, 10, -1)], issue="timeout"),
        agent("login", "Login", [], issue="login_required"),
        agent("old", "Old", [], issue="unsupported_version"),
        agent("odd", "Odd", [], issue="something_new"),
        agent("absent", "Absent", [], issue="not_found"),
        agent(
            "pools",
            "Pools",
            [limit("pools", 300, 5, 3, "spark"), limit("pools", 300, 7, 3, "codex")],
            color="not a color",
        ),
        agent("Bad ID", "Bad", [limit("bad", 300, 1, 1)]),
        agent(
            "partial",
            "Partial",
            [dict(id="partial::300", minutes=300, used_percent=None, resets_at=NOW + 60), 4],
        ),
        agent("x", "Daily", [limit("x", 1440, 0, 0.01), limit("x", 90, 12.5, 30)]),
    ],
)
SELECTION = ["beta::10080", "alpha::300", "stale::300", "pools:codex:300", "missing::300"]


class PresentationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    @unittest.skipUnless(shutil.which("node"), "Node runs the top bar module outside the shell")
    def test_tray_presents_limits_exactly_as_the_gnome_top_bar(self):
        module = (Path(__file__).parents[1] / "usage/gnome/limits.js").as_uri()
        script = (
            f"import {{summarize}} from {json.dumps(module)};"
            "const [payload, now, selection] = process.argv.slice(1).map(JSON.parse);"
            "console.log(JSON.stringify(summarize(payload, now, selection)));"
        )
        result = subprocess.run(
            ["node", "--input-type=module", "-e", script]
            + [json.dumps(value) for value in (PAYLOAD, NOW, SELECTION)],
            capture_output=True,
            encoding="utf-8",
            check=True,
            timeout=30,
        )

        def view(summary):
            return dict(
                agents=[
                    [a["id"], a["name"], a["note"], [[x[k] for k in FIELDS] for x in a["limits"]]]
                    for a in summary["agents"]
                ],
                shown=[x["id"] for x in summary["shown"]],
            )

        gnome = view(json.loads(result.stdout))
        tray = view(summarize(PAYLOAD, NOW, SELECTION))
        self.assertEqual(tray, gnome)
        # The payload exercises every rule, so agreement is not vacuous.
        notes = {name: note for _, name, note, _ in tray["agents"]}
        self.assertEqual(notes["Stale"], "Outdated")
        self.assertEqual(notes["Gone"], "Connection timed out")
        self.assertEqual(notes["Old"], "Update Old")
        self.assertEqual(notes["Odd"], "Limits unavailable")
        self.assertNotIn("Absent", notes)
        self.assertNotIn("Bad", notes)
        levels = {row[0]: (row[2], row[3], row[4]) for a in tray["agents"] for row in a[3]}
        self.assertEqual(levels["alpha::300"], ("5-hour limit", "86%", "warning"))
        self.assertEqual(levels["beta::10080"], ("Weekly limit", "100%", "critical"))
        self.assertEqual(levels["stale::300"][2], "normal")
        self.assertEqual(levels["pools:spark:300"][0], "spark · 5-hour limit")
        self.assertEqual(levels["x::90"][0], "90-minute limit")
        self.assertEqual(
            tray["shown"], ["alpha::300", "beta::10080", "pools:codex:300", "stale::300"]
        )

    def test_panel_opens_beside_the_icon_away_from_the_taskbar(self):
        size, gap = QSize(FLYOUT_WIDTH, 300), FLYOUT_MARGIN

        def panel(icon, area):
            rect = QRect(placement(icon, area, size), size)
            self.assertTrue(area.contains(rect))
            return rect

        # Taskbar at the bottom: just above it, centered on the icon.
        area = QRect(0, 0, 1920, 1032)
        icon = QRect(1500, 1040, 24, 24)
        rect = panel(icon, area)
        self.assertEqual(rect.bottom() + 1 + gap, area.bottom() + 1)
        self.assertLessEqual(abs(rect.center().x() - icon.center().x()), 1)
        # Near the screen's corner it stays a margin inside the work area.
        rect = panel(QRect(1890, 1040, 24, 24), area)
        self.assertEqual(rect.right() + 1 + gap, area.right() + 1)
        # Taskbar at the top: just below it.
        rect = panel(QRect(1500, 12, 24, 24), QRect(0, 48, 1920, 1032))
        self.assertEqual(rect.top(), 48 + gap)
        # Taskbar on the left with the clock at its foot: beside it, above the icon.
        area = QRect(64, 0, 1856, 1080)
        icon = QRect(20, 1000, 24, 24)
        rect = panel(icon, area)
        self.assertEqual(rect.left(), area.left() + gap)
        self.assertEqual(rect.bottom() + 1 + gap, icon.top())

    def test_app_icon_is_black_and_white_and_the_windows_icon_matches_it(self):
        def pixels(image):
            image = image.convertToFormat(QImage.Format.Format_RGBA8888)
            return bytes(image.constBits())[: image.sizeInBytes()]

        data = icon.ICO.read_bytes()
        self.assertEqual(struct.unpack_from("<HHH", data), (0, 1, len(icon.SIZES)))
        for index, size in enumerate(icon.SIZES):
            width, height, _, _, _, depth, length, offset = struct.unpack_from(
                "<BBBBHHII", data, 6 + 16 * index
            )
            self.assertEqual((width or 256, height or 256, depth), (size, size, 32))
            stored = pixels(QImage.fromData(data[offset : offset + length], "PNG"))
            expected = pixels(icon.render(icon.SVG, size))
            # Rasterizers may round a blended edge differently; shapes must not differ.
            self.assertEqual(len(stored), len(expected))
            self.assertLessEqual(max(abs(a - b) for a, b in zip(stored, expected)), 4)
            # Black and white only: every opaque pixel is a gray.
            for red, green, blue, _ in zip(*[iter(expected)] * 4):
                self.assertTrue(red == green == blue, f"colored pixel at {size} px")

    def test_badges_stay_legible_at_every_size(self):
        self.assertEqual(text_color(LEVELS["warning"]).name(), "#000000")
        self.assertEqual(text_color("#123456").name(), "#ffffff")
        for value in ("0", "86", "100"):
            icon = badge(value, LEVELS["critical"])
            sizes = sorted(size.width() for size in icon.availableSizes())
            self.assertEqual(sizes, list(ICON_SIZES))
            for size in ICON_SIZES:
                image = icon.pixmap(size, size).toImage()
                corner, center = image.pixelColor(0, 0), image.pixelColor(size // 2, size // 2)
                self.assertEqual(corner.alpha(), 0)
                self.assertGreater(center.alpha(), 0)

        def opacity(icon):
            image = icon.pixmap(32, 32).toImage()
            return max(image.pixelColor(x, y).alpha() for x in range(32) for y in range(32))

        # Outdated badges are translucent throughout, digits included, whatever the font.
        self.assertEqual(opacity(badge("42", "#d97757")), 255)
        self.assertLess(opacity(badge("42", "#d97757", faded=True)), 200)
