"""Regenerate the Windows icon (shortcut and portable executable) from the app's SVG icon.

Run after changing usage/static/assets/widget.svg; a test fails while the two differ.
"""

import struct
import sys
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QRectF, Qt
from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

ROOT = Path(__file__).resolve().parents[1]
SVG = ROOT / "usage/static/assets/widget.svg"
ICO = ROOT / "usage/static/assets/widget.ico"
# Every size Windows picks from, 100% to 300% scaling, up to large Start menu tiles.
SIZES = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)


def render(svg, size):
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    QSvgRenderer(str(svg)).render(painter, QRectF(0, 0, size, size))
    painter.end()
    return image


def ico(svg=SVG):
    """An .ico holding a PNG of the SVG at each size, as Windows Vista and later read."""
    images = []
    for size in SIZES:
        data = QByteArray()
        buffer = QBuffer(data)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        render(svg, size).save(buffer, "PNG")
        images.append((size, bytes(data)))
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = len(header) + 16 * len(images)
    entries, payload = b"", b""
    for size, png in images:
        # Width and height 0 stand for 256.
        entries += struct.pack(
            "<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(png), offset + len(payload)
        )
        payload += png
    return header + entries + payload


def main():
    _app = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])
    ICO.write_bytes(ico())
    print(ICO.relative_to(ROOT))


if __name__ == "__main__":
    main()
