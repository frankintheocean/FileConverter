"""Render the original vector identity with alpha, then write a multi-frame Windows ICO."""

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PIL import Image
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parents[1]
app = QApplication.instance() or QApplication([])
renderer = QSvgRenderer(str(ROOT / "assets/app.svg"))
image = QImage(1024, 1024, QImage.Format.Format_ARGB32)
image.fill(Qt.GlobalColor.transparent)
painter = QPainter(image)
painter.setRenderHint(QPainter.RenderHint.Antialiasing)
renderer.render(painter)
painter.end()
image.save(str(ROOT / "assets/app.png"))
with Image.open(ROOT / "assets/app.png") as source:
    source.save(
        ROOT / "assets/app.ico", sizes=[(n, n) for n in (16, 20, 24, 32, 40, 48, 64, 128, 256)]
    )
print("Rendered 1024px PNG and nine-size alpha ICO")
