from __future__ import annotations

import dataclasses
import json
import os
import platform
import sys
import threading
import time
import uuid
from pathlib import Path

from PySide6.QtCore import (
    QAbstractTableModel,
    QItemSelectionModel,
    QModelIndex,
    QObject,
    Qt,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QDesktopServices,
    QIcon,
    QKeySequence,
    QPalette,
    QPixmap,
    QShortcut,
)
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QSystemTrayIcon,
    QTableView,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from . import __version__
from .capabilities import AUDIO, LOSSLESS
from .models import TERMINAL, Job, Options, State
from .process import Cancelled
from .watchers import Watcher, scan_files


def asset(name):
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2])) / "assets" / name


def size(value):
    if value is None:
        return "—"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1000:
            return f"{value:.1f} {unit}"
        value /= 1000
    return f"{value:.1f} PB"


def button(text, callback, primary=False):
    widget = QPushButton(text)
    widget.setCursor(Qt.CursorShape.PointingHandCursor)
    widget.setAccessibleName(text)
    widget.setToolTip(text)
    if primary:
        widget.setProperty("primary", True)

    def guarded():
        try:
            callback()
        except Exception as error:
            QMessageBox.warning(
                widget.window(), "FileConverter", str(error) or type(error).__name__
            )

    widget.clicked.connect(guarded)
    return widget


def combo(items):
    widget = QComboBox()
    widget.addItems(items)
    return widget


def spin(low=0, high=32768, value=0):
    widget = QSpinBox()
    widget.setRange(low, high)
    widget.setValue(value)
    return widget


def decimal(low=0, high=10000000, value=0, decimals=2):
    widget = QDoubleSpinBox()
    widget.setRange(low, high)
    widget.setDecimals(decimals)
    widget.setValue(value)
    return widget


def folder_field(value=""):
    widget = QWidget()
    layout = QHBoxLayout(widget)
    layout.setContentsMargins(0, 0, 0, 0)
    edit = QLineEdit(value)
    edit.setAccessibleName("Folder path")
    layout.addWidget(edit)

    def browse():
        chosen = QFileDialog.getExistingDirectory(widget, "Choose folder", edit.text())
        if chosen:
            edit.setText(chosen)

    layout.addWidget(button("Browse…", browse))
    return widget, edit


class Bridge(QObject):
    changed = Signal(object)
    terminal = Signal(object)
    done = Signal(str, object)
    error = Signal(str)


class JobTable(QAbstractTableModel):
    columns = (
        "File",
        "Output",
        "State / stage",
        "Progress",
        "Size",
        "Elapsed",
        "Remaining",
        "Speed",
    )

    def __init__(self):
        super().__init__()
        self.rows = []

    def set_rows(self, rows):
        self.beginResetModel()
        self.rows = list(rows)
        self.endResetModel()

    def rowCount(self, parent=None):
        parent = parent or QModelIndex()
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=None):
        return len(self.columns)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.columns[section]

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        job = self.rows[index.row()]
        if role == Qt.ItemDataRole.DisplayRole:
            elapsed = job.result.get("elapsed", job.result.get("seconds", 0))
            remaining = job.result.get("remaining")
            return (
                Path(job.source).name,
                job.options.format.upper(),
                job.error if job.state == State.FAILED else job.stage,
                f"{job.progress:.1f}%",
                size(job.result.get("size", job.info.get("size"))),
                f"{elapsed:.1f}s",
                f"{remaining:.0f}s" if remaining else "—",
                job.result.get("speed", "—"),
            )[index.column()]
        if role == Qt.ItemDataRole.ToolTipRole:
            return job.error or job.output or job.source
        if role == Qt.ItemDataRole.ForegroundRole:
            app = QApplication.instance()
            dark = (
                isinstance(app, QApplication)
                and app.palette().color(QPalette.ColorRole.Base).lightness() < 128
            )
            if job.state == State.FAILED:
                return QColor("#ff929d" if dark else "#b23b45")
            if job.state == State.COMPLETED:
                return QColor("#6ee0b7" if dark else "#17755f")


class OptionsEditor(QWidget):
    def __init__(self, cap, parent=None):
        super().__init__(parent)
        self.cap = cap
        self.info = None
        self.base = Options()
        self.changed = lambda: None
        self.tabs = QTabWidget()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.tabs)
        output, form = self.page("Output")
        self.format = combo([])
        self.codec = combo(["Automatic"])
        self.audio_codec = combo(["Automatic"])
        self.quality = spin(0, 63, 23)
        self.image_quality = spin(1, 100, 85)
        self.lossless = QCheckBox("Lossless encoding")
        self.resolution = combo(
            ["Source", "360p", "480p", "720p", "1080p", "1440p", "2160p", "Custom"]
        )
        self.output_width = spin()
        self.output_height = spin()
        self.percent = decimal(1, 1000, 100)
        self.aspect = QCheckBox("Preserve aspect ratio")
        self.aspect.setChecked(True)
        self.fps = decimal(0, 240, 0)
        self.bitrate = spin(0, 1000000)
        self.max_bitrate = spin(0, 1000000)
        self.audio_rate = combo(["64", "96", "128", "160", "192", "256", "320"])
        self.audio_rate.setEditable(True)
        self.audio_rate.setCurrentText("192")
        self.sample_rate = spin(0, 384000)
        self.channels = combo(["Source", "Mono", "Stereo"])
        self.metadata = combo(
            [
                "Strip all removable metadata",
                "Strip timestamps/device/location",
                "Preserve metadata",
            ]
        )
        self.artwork = QCheckBox("Preserve embedded cover artwork")
        self.acceleration = combo(["Software only", "Auto", "Hardware preferred"])
        self.preset = combo(["ultrafast", "veryfast", "fast", "medium", "slow", "slower"])
        self.preset.setCurrentText("medium")
        self.pixel = combo(["yuv420p", "yuv420p10le", "yuv444p"])
        self.remux = QCheckBox("Copy compatible streams without transcoding")
        self.background = QLineEdit("#ffffff")
        self.fields = {}
        for label, widget in [
            ("Format", self.format),
            ("Video / audio codec", self.codec),
            ("Video audio codec", self.audio_codec),
            ("CRF (lower is higher quality)", self.quality),
            ("Image quality", self.image_quality),
            ("Lossless", self.lossless),
            ("Resolution", self.resolution),
            ("Width (0 = source)", self.output_width),
            ("Height (0 = source)", self.output_height),
            ("Scale percent", self.percent),
            ("Aspect ratio", self.aspect),
            ("FPS (0 = source)", self.fps),
            ("Video bitrate kbps (0 = quality)", self.bitrate),
            ("Maximum bitrate kbps", self.max_bitrate),
            ("Audio bitrate kbps", self.audio_rate),
            ("Sample rate Hz (0 = source)", self.sample_rate),
            ("Channels", self.channels),
            ("Metadata privacy", self.metadata),
            ("Cover artwork", self.artwork),
            ("Acceleration", self.acceleration),
            ("Encoder speed", self.preset),
            ("Pixel format", self.pixel),
            ("Remux", self.remux),
            ("Transparency background", self.background),
        ]:
            widget.setAccessibleName(label)
            widget.setToolTip(label)
            form.addRow(label, widget)
            self.fields[widget] = form.labelForField(widget)
        _, form = self.page("Size target")
        self.compression = combo(["Quality based", "Maximum output size", "Percentage reduction"])
        self.target = decimal(0.001, 100000, 10, 3)
        self.units = combo(["KB", "MB", "GB"])
        self.units.setCurrentText("MB")
        self.reduction = decimal(0, 99.9, 50)
        self.limit_preset = combo(
            [
                "Custom",
                "Discord 10 MB",
                "Discord 25 MB",
                "Email attachment 20 MB",
                "50 MB",
                "100 MB",
            ]
        )
        form.addRow("Compression", self.compression)
        form.addRow("Convenience limit (editable)", self.limit_preset)
        form.addRow("Maximum size", self.target)
        form.addRow("Decimal units", self.units)
        form.addRow("Reduction %", self.reduction)
        note = QLabel(
            "Maximum-size jobs publish only validated outputs within the requested limit. A tiny target may require lower resolution or trimming."
        )
        note.setWordWrap(True)
        form.addRow(note)
        _, form = self.page("Media tools")
        self.operation = combo(["convert", "frame", "frames", "subtitles"])
        self.start = decimal()
        self.end = decimal()
        self.crop = QLineEdit()
        self.crop.setPlaceholderText("width:height:x:y")
        self.rotation = combo(["0", "90", "180", "270"])
        self.remove_audio = QCheckBox("Remove audio")
        self.volume = decimal(0, 20, 1)
        self.normalize = QCheckBox("Normalize to −16 LUFS")
        self.video_stream = spin(-1, 1000, -1)
        self.audio_stream = spin(-1, 1000, -1)
        self.subtitle_stream = spin(-1, 1000, -1)
        self.subtitles = combo(["remove", "preserve", "burn"])
        self.frame_time = decimal()
        self.frame_interval = decimal(0.01, 3600, 1)
        self.tags = QPlainTextEdit("{}")
        self.tags.setMaximumHeight(80)
        self.tags.setAccessibleName("Metadata fields as JSON")
        self.media_fields = {}
        for label, widget in [
            ("Operation", self.operation),
            ("Trim start seconds", self.start),
            ("Trim end (0 = end)", self.end),
            ("Crop rectangle", self.crop),
            ("Clockwise rotation", self.rotation),
            ("Audio", self.remove_audio),
            ("Volume multiplier", self.volume),
            ("Normalize", self.normalize),
            ("Video stream (−1 = first)", self.video_stream),
            ("Audio stream (−1 = first)", self.audio_stream),
            ("Subtitle stream (−1 = all)", self.subtitle_stream),
            ("Subtitles", self.subtitles),
            ("Extract frame at seconds", self.frame_time),
            ("Frame interval seconds", self.frame_interval),
            ("Metadata fields (JSON)", self.tags),
        ]:
            form.addRow(label, widget)
            widget.setAccessibleName(label)
            self.media_fields[widget] = form.labelForField(widget)
        _, form = self.page("Output safety")
        self.collision = combo(["rename", "skip", "replace", "newer"])
        self.suffix = QLineEdit("-converted")
        self.filename_template = QLineEdit("{stem}{suffix}")
        form.addRow("Collision policy", self.collision)
        form.addRow("Filename suffix", self.suffix)
        form.addRow("Filename template ({stem}, {suffix}, {format})", self.filename_template)
        privacy = QLabel(
            "Stripping removes safely removable descriptive metadata. It does not redact visible content. Replacement requires explicit confirmation before starting."
        )
        privacy.setWordWrap(True)
        form.addRow(privacy)
        self.format.currentTextChanged.connect(self.update_formats)
        self.codec.currentTextChanged.connect(self.visibility)
        self.operation.currentTextChanged.connect(self.change_operation)
        self.resolution.currentIndexChanged.connect(self.set_resolution)
        self.limit_preset.currentIndexChanged.connect(self.set_limit)
        for widget in self.findChildren(QWidget):
            if isinstance(widget, QComboBox):
                widget.currentTextChanged.connect(self.notify)
            elif isinstance(widget, (QSpinBox, QDoubleSpinBox)):
                widget.valueChanged.connect(self.notify)
            elif isinstance(widget, QCheckBox):
                widget.toggled.connect(self.notify)
            elif isinstance(widget, QLineEdit):
                widget.textChanged.connect(self.notify)
        self.load(Options())

    def page(self, name):
        inner = QWidget()
        form = QFormLayout(inner)
        form.setSpacing(12)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(inner)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.tabs.addTab(scroll, name)
        return inner, form

    def notify(self, *args):
        self.changed()

    def set_resolution(self, index):
        if index == 0:
            self.output_width.setValue(0)
            self.output_height.setValue(0)
        elif index < 7:
            self.output_width.setValue(0)
            self.output_height.setValue([0, 360, 480, 720, 1080, 1440, 2160][index])

    def set_limit(self, index):
        if index:
            self.compression.setCurrentIndex(1)
            self.units.setCurrentText("MB")
            self.target.setValue([0, 10, 25, 20, 50, 100][index])

    def set_info(self, info):
        self.info = info
        if info.category != "video":
            self.operation.setCurrentText("convert")
        current = self.format.currentText()
        self.format.blockSignals(True)
        self.format.clear()
        outputs = self.cap.outputs(info.category, info.format)
        if info.category == "video" and self.operation.currentText() in (
            "frame",
            "frames",
            "subtitles",
        ):
            outputs = ["srt"] if self.operation.currentText() == "subtitles" else ["png"]
        self.format.addItems(outputs)
        if current in [self.format.itemText(i) for i in range(self.format.count())]:
            self.format.setCurrentText(current)
        self.format.blockSignals(False)
        self.update_formats()
        self.notify()

    def change_operation(self, *args):
        if self.info:
            self.set_info(self.info)
        elif self.operation.currentText() in ("frame", "frames", "subtitles"):
            self.format.clear()
            self.format.addItem("srt" if self.operation.currentText() == "subtitles" else "png")
        self.visibility()

    def update_formats(self, *args):
        current = self.codec.currentText()
        fmt = self.format.currentText()
        category = "audio" if fmt in AUDIO else "video"
        self.codec.blockSignals(True)
        self.codec.clear()
        self.codec.addItem("Automatic")
        self.codec.addItems(self.cap.codecs(fmt, category))
        if current in [self.codec.itemText(i) for i in range(self.codec.count())]:
            self.codec.setCurrentText(current)
        self.codec.blockSignals(False)
        self.audio_codec.clear()
        self.audio_codec.addItem("Automatic")
        self.audio_codec.addItems(self.cap.audio_codecs(fmt))
        self.visibility()

    def visibility(self, *args):
        category = self.info.category if self.info else "video"
        audio = self.format.currentText() in AUDIO
        video = (
            category == "video"
            and self.format.currentText() in self.cap.outputs("video")
            and not audio
            and self.operation.currentText() == "convert"
        )
        image = category == "image"
        has_audio = bool(self.cap.audio_codecs(self.format.currentText())) and (
            not self.info or bool(self.info.audio_codec)
        )
        codec = self.codec.currentText()
        if codec == "Automatic":
            codec = next(
                iter(self.cap.codecs(self.format.currentText(), "audio" if audio else "video")), ""
            )
        self.quality.setMaximum(51 if codec in ("libx264", "libx265") else 63)
        supported_pixels = self.cap.encoder_properties.get(codec, {}).get("pixel_formats", [])
        pixels = [
            p
            for p in ("yuv420p", "yuv420p10le", "yuv444p")
            if not supported_pixels or p in supported_pixels
        ]
        if pixels != [self.pixel.itemText(i) for i in range(self.pixel.count())]:
            previous = self.pixel.currentText()
            self.pixel.clear()
            self.pixel.addItems(pixels)
            if previous in pixels:
                self.pixel.setCurrentText(previous)
        groups = {
            self.codec: category in ("video", "audio")
            and self.operation.currentText() == "convert",
            self.audio_codec: video and has_audio,
            self.quality: video
            and codec in ("libx264", "libx265", "libvpx", "libvpx-vp9", "libaom-av1"),
            self.image_quality: image
            and self.format.currentText() in ("jpg", "jpeg", "webp", "avif"),
            self.lossless: (
                image and self.format.currentText() in ("png", "webp", "avif", "tiff", "bmp", "ico")
            )
            or (audio and codec in LOSSLESS)
            or (video and codec in ("libx264", "libx265", "libvpx-vp9", "libaom-av1")),
            self.resolution: video,
            self.output_width: image or video,
            self.output_height: image or video,
            self.percent: image or video,
            self.aspect: image or video,
            self.fps: video,
            self.bitrate: video,
            self.max_bitrate: video,
            self.audio_rate: (audio and codec not in LOSSLESS) or (video and has_audio),
            self.sample_rate: audio or (video and has_audio),
            self.channels: audio or (video and has_audio),
            self.artwork: audio,
            self.acceleration: video,
            self.preset: video and codec in ("libx264", "libx265"),
            self.pixel: video and self.format.currentText() != "gif",
            self.remux: category in ("audio", "video")
            and self.operation.currentText() == "convert",
            self.background: image and self.format.currentText() in ("jpg", "jpeg", "bmp"),
        }
        for widget, visible in groups.items():
            widget.setVisible(visible)
            self.fields[widget].setVisible(visible)
        if not groups[self.lossless]:
            self.lossless.setChecked(False)
        media = category in ("audio", "video")
        audio_tools = media and self.operation.currentText() == "convert"
        media_groups = {
            self.operation: category == "video",
            self.start: media,
            self.end: media,
            self.crop: image or video,
            self.rotation: image or video,
            self.remove_audio: video and has_audio,
            self.volume: audio_tools,
            self.normalize: audio_tools,
            self.video_stream: category == "video",
            self.audio_stream: audio_tools,
            self.subtitle_stream: category == "video",
            self.subtitles: video,
            self.frame_time: self.operation.currentText() == "frame",
            self.frame_interval: self.operation.currentText() == "frames",
            self.tags: audio_tools,
        }
        for widget, visible in media_groups.items():
            widget.setVisible(visible)
            self.media_fields[widget].setVisible(visible)

    def options(self):
        options = dataclasses.replace(self.base)
        options.format = self.format.currentText() or self.base.format
        options.codec = "" if self.codec.currentText() == "Automatic" else self.codec.currentText()
        options.audio_codec = (
            "" if self.audio_codec.currentText() == "Automatic" else self.audio_codec.currentText()
        )
        options.quality = self.quality.value()
        options.image_quality = self.image_quality.value()
        options.lossless = self.lossless.isChecked()
        options.width, options.height = self.output_width.value(), self.output_height.value()
        options.scale_percent = self.percent.value()
        options.aspect = self.aspect.isChecked()
        options.fps = self.fps.value()
        options.bitrate, options.max_bitrate = (
            self.bitrate.value() * 1000,
            self.max_bitrate.value() * 1000,
        )
        options.audio_bitrate = int(float(self.audio_rate.currentText()) * 1000)
        options.sample_rate = self.sample_rate.value()
        options.channels = self.channels.currentIndex()
        options.metadata = ["strip", "minimal", "preserve"][self.metadata.currentIndex()]
        options.artwork = self.artwork.isChecked()
        options.acceleration = ["software", "auto", "hardware"][self.acceleration.currentIndex()]
        options.encoder_preset = self.preset.currentText()
        options.pixel_format = self.pixel.currentText()
        options.remux = self.remux.isChecked()
        options.background = self.background.text()
        options.target_bytes = (
            int(self.target.value() * [1000, 1_000_000, 1_000_000_000][self.units.currentIndex()])
            if self.compression.currentIndex() == 1
            else 0
        )
        options.reduction = self.reduction.value() if self.compression.currentIndex() == 2 else 0
        options.operation = self.operation.currentText()
        options.trim_start, options.trim_end = self.start.value(), self.end.value()
        options.crop = self.crop.text().strip()
        options.rotation = int(self.rotation.currentText())
        options.remove_audio = self.remove_audio.isChecked()
        options.volume = self.volume.value()
        options.normalize = self.normalize.isChecked()
        options.video_stream, options.audio_stream = (
            self.video_stream.value(),
            self.audio_stream.value(),
        )
        options.subtitle_stream = self.subtitle_stream.value()
        options.subtitles = self.subtitles.currentText()
        options.frame_time, options.frame_interval = (
            self.frame_time.value(),
            self.frame_interval.value(),
        )
        options.tags = json.loads(self.tags.toPlainText())
        if not isinstance(options.tags, dict):
            raise ValueError("Metadata fields must be a JSON object")
        options.collision = self.collision.currentText()
        options.suffix = self.suffix.text()
        options.filename_template = self.filename_template.text()
        if options.operation != "convert":
            options.remux = False
            options.codec = ""
            options.audio_codec = ""
            options.subtitles = "remove"
            options.audio_stream = -1
            options.remove_audio = False
            options.normalize = False
            options.volume = 1
            options.artwork = False
        options.validate()
        return options

    def load(self, options):
        self.base = dataclasses.replace(options)
        self.operation.setCurrentText(options.operation)
        if not self.info:
            self.format.clear()
            self.format.addItems(
                sorted(
                    set(
                        self.cap.outputs("video")
                        + self.cap.outputs("audio")
                        + self.cap.outputs("image")
                        + self.cap.outputs("document", "txt")
                    )
                )
            )
        if self.format.findText(options.format) >= 0:
            self.format.setCurrentText(options.format)
        self.update_formats()
        self.codec.setCurrentText(options.codec or "Automatic")
        self.audio_codec.setCurrentText(options.audio_codec or "Automatic")
        self.quality.setValue(options.quality)
        self.image_quality.setValue(options.image_quality)
        self.lossless.setChecked(options.lossless)
        self.resolution.setCurrentText("Custom" if options.width or options.height else "Source")
        self.output_width.setValue(options.width)
        self.output_height.setValue(options.height)
        self.percent.setValue(options.scale_percent)
        self.aspect.setChecked(options.aspect)
        self.fps.setValue(options.fps)
        self.bitrate.setValue(options.bitrate // 1000)
        self.max_bitrate.setValue(options.max_bitrate // 1000)
        self.audio_rate.setCurrentText(str(options.audio_bitrate // 1000))
        self.sample_rate.setValue(options.sample_rate)
        self.channels.setCurrentIndex(options.channels)
        self.metadata.setCurrentIndex(["strip", "minimal", "preserve"].index(options.metadata))
        self.artwork.setChecked(options.artwork)
        self.acceleration.setCurrentIndex(
            ["software", "auto", "hardware"].index(options.acceleration)
        )
        self.preset.setCurrentText(options.encoder_preset)
        self.pixel.setCurrentText(options.pixel_format)
        self.remux.setChecked(options.remux)
        self.background.setText(options.background)
        self.compression.setCurrentIndex(
            1 if options.target_bytes else 2 if options.reduction else 0
        )
        self.units.setCurrentText("MB")
        if options.target_bytes:
            self.target.setValue(options.target_bytes / 1_000_000)
        self.reduction.setValue(options.reduction or 50)
        self.operation.setCurrentText(options.operation)
        self.start.setValue(options.trim_start)
        self.end.setValue(options.trim_end)
        self.crop.setText(options.crop)
        self.rotation.setCurrentText(str(options.rotation))
        self.remove_audio.setChecked(options.remove_audio)
        self.volume.setValue(options.volume)
        self.normalize.setChecked(options.normalize)
        self.video_stream.setValue(options.video_stream)
        self.audio_stream.setValue(options.audio_stream)
        self.subtitle_stream.setValue(options.subtitle_stream)
        self.subtitles.setCurrentText(options.subtitles)
        self.frame_time.setValue(options.frame_time)
        self.frame_interval.setValue(options.frame_interval)
        self.tags.setPlainText(json.dumps(options.tags, indent=2))
        self.collision.setCurrentText(options.collision)
        self.suffix.setText(options.suffix)
        self.filename_template.setText(options.filename_template)
        self.visibility()


class WatcherDialog(QDialog):
    def __init__(self, window, watcher=None):
        super().__init__(window)
        self.owner = window
        self.watcher = (
            dataclasses.replace(watcher) if watcher else Watcher("New watcher", "", "Balanced")
        )
        self.setWindowTitle("Configure watched folder")
        self.resize(740, 780)
        layout = QVBoxLayout(self)
        tabs = QTabWidget()
        layout.addWidget(tabs)
        basic = QWidget()
        form = QFormLayout(basic)
        self.name = QLineEdit(self.watcher.name)
        path_widget, self.path = folder_field(self.watcher.path)
        output_widget, self.output = folder_field(self.watcher.destination)
        archive_widget, self.archive = folder_field(self.watcher.archive)
        self.recursive = QCheckBox("Include subfolders")
        self.recursive.setChecked(self.watcher.recursive)
        self.enabled = QCheckBox("Enable automatic conversion")
        self.enabled.setChecked(self.watcher.enabled)
        self.preset = combo([p["id"] for p in window.store.records("preset")])
        self.preset.setCurrentText(self.watcher.preset_id)
        self.category = combo(["Any", "video", "audio", "image", "document"])
        self.extensions = QLineEdit()
        self.extensions.setPlaceholderText("mp3, flac, wav — empty means any")
        self.audio_max = spin(0, 10000)
        self.output_mode = combo(["same", "folder", "mirror", "preset"])
        self.output_mode.setCurrentText(self.watcher.output_mode)
        self.action = combo(["keep", "archive", "recycle", "delete"])
        self.action.setCurrentText(self.watcher.source_action)
        self.collision = combo(["rename", "skip", "replace", "newer"])
        self.collision.setCurrentText(self.watcher.collision)
        self.startup = combo(["scan", "new", "ask"])
        self.startup.setCurrentText(self.watcher.startup)
        self.stability = decimal(1, 86400, self.watcher.stability_seconds)
        self.reconcile = decimal(10, 86400, self.watcher.reconcile_seconds)
        self.priority = spin(-10000, 10000, self.watcher.priority)
        self.notify = QCheckBox("Allow completion notifications")
        self.notify.setChecked(self.watcher.notifications)
        for label, field in [
            ("Name", self.name),
            ("Watch location", path_widget),
            ("Recursion", self.recursive),
            ("Category", self.category),
            ("Extensions", self.extensions),
            ("Audio bitrate ≤ kbps (0 = any)", self.audio_max),
            ("Preset", self.preset),
            ("Output behavior", self.output_mode),
            ("Output folder", output_widget),
            ("Source action", self.action),
            ("Archive folder", archive_widget),
            ("Collision", self.collision),
            ("Startup", self.startup),
            ("Stable for seconds", self.stability),
            ("Reconcile every seconds", self.reconcile),
            ("Priority (higher wins)", self.priority),
            ("Notifications", self.notify),
            ("Enable", self.enabled),
        ]:
            form.addRow(label, field)
            field.setAccessibleName(label)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(basic)
        tabs.addTab(scroll, "Watcher")
        advanced = QWidget()
        av = QVBoxLayout(advanced)
        av.addWidget(
            QLabel(
                "Advanced conditions are ANDed with the basic fields. Use all/any groups and typed values."
            )
        )
        self.rules = QPlainTextEdit(json.dumps(self.watcher.rules, indent=2))
        self.rules.setAccessibleName("Advanced matching rules JSON")
        av.addWidget(self.rules)
        schema = QLabel(
            'Fields: category, format, extension, name, size, age, audio_bitrate, bitrate, width, height, fps, duration, sample_rate, channels, audio_codec, video_codec, lossless, metadata, hdr.\nOperators: eq, ne, lt, le, gt, ge, between, in, glob.\nExample: {"all": [{"field":"audio_bitrate","op":"le","value":128000}]}'
        )
        schema.setWordWrap(True)
        av.addWidget(schema)
        tabs.addTab(advanced, "Advanced rules")
        self.warning = QCheckBox(
            "I understand: increasing a lossy audio bitrate cannot restore lost source detail"
        )
        layout.addWidget(self.warning)
        controls = QHBoxLayout()
        controls.addWidget(button("Preview matches / Dry run", self.preview))
        controls.addStretch()
        controls.addWidget(button("Cancel", self.reject))
        controls.addWidget(button("Save watcher", self.save, True))
        layout.addLayout(controls)

    def value(self):
        w = dataclasses.replace(self.watcher)
        w.name = self.name.text().strip()
        w.path = self.path.text().strip()
        w.preset_id = self.preset.currentText()
        w.recursive, w.enabled = self.recursive.isChecked(), self.enabled.isChecked()
        basic = []
        if self.category.currentText() != "Any":
            basic.append(dict(field="category", op="eq", value=self.category.currentText()))
        extensions = [
            s.strip().lower().lstrip(".") for s in self.extensions.text().split(",") if s.strip()
        ]
        if extensions:
            basic.append(dict(field="extension", op="in", value=extensions))
        if self.audio_max.value():
            basic.append(dict(field="audio_bitrate", op="le", value=self.audio_max.value() * 1000))
        advanced = json.loads(self.rules.toPlainText())
        w.rules = {"all": [advanced, *basic]} if basic else advanced
        w.output_mode = self.output_mode.currentText()
        w.destination, w.archive = self.output.text().strip(), self.archive.text().strip()
        w.source_action, w.collision = self.action.currentText(), self.collision.currentText()
        w.startup = self.startup.currentText()
        w.stability_seconds, w.reconcile_seconds = self.stability.value(), self.reconcile.value()
        w.priority = self.priority.value()
        w.notifications = self.notify.isChecked()
        preset = self.owner.store.get("preset", w.preset_id)
        warning_key = json.dumps([w.rules, preset["options"]], sort_keys=True)
        if self.warning.isChecked():
            w.warning_ack = warning_key
        w.validate()
        return w

    def preview(self):
        try:
            watcher = self.value()
            self.owner.background(
                "watch-preview",
                lambda: self.owner.watchers.preview(watcher, self.owner.shutdown_event),
            )
        except Exception as error:
            self.owner.error(str(error))

    def save(self):
        try:
            watcher = self.value()
            if watcher.source_action in ("delete", "recycle", "archive"):
                if (
                    QMessageBox.warning(
                        self,
                        "Source file action",
                        f"After validated conversion, this watcher will {watcher.source_action} source files. Continue?",
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    )
                    != QMessageBox.StandardButton.Yes
                ):
                    return
            if watcher.collision in ("replace", "newer"):
                if (
                    QMessageBox.warning(
                        self,
                        "Replace outputs",
                        "This watcher may replace existing output files. Continue?",
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    )
                    != QMessageBox.StandardButton.Yes
                ):
                    return
            if not self.warning.isChecked() and watcher.enabled:
                preset = self.owner.store.get("preset", watcher.preset_id)
                if preset["options"]["audio_bitrate"] > 128000:
                    QMessageBox.information(
                        self,
                        "Lossy audio quality",
                        "Increasing a lossy audio bitrate does not restore lost detail. Matching upconversions wait for acknowledgement. Use the checkbox to acknowledge for this configuration.",
                    )
            self.owner.watchers.save(watcher)
            self.accept()
        except Exception as error:
            self.owner.error(str(error))


class Window(QMainWindow):
    def __init__(self, store, engine, queue, watchers, bridge):
        super().__init__()
        self.store, self.engine, self.queue, self.watchers = store, engine, queue, watchers
        self.bridge = bridge
        self.cap = engine.cap
        self.infos = {}
        self.imports = []
        self.shutdown_event = threading.Event()
        self.background_threads = set()
        self.force_exit = False
        self.closed = False
        self.preview_cancel = threading.Event()
        self.preview_generation = 0
        self.comparisons: dict[str, list[QLabel]] = {}
        self.history_offset = 0
        self.history_busy = False
        self.history_query = ("", 0, -1)
        self.history_last_request = None
        self.dirty = True
        self.setWindowTitle("FileConverter")
        self.setWindowIcon(QIcon(str(asset("app.ico"))))
        app = QApplication.instance()
        if isinstance(app, QApplication):
            app.setWindowIcon(self.windowIcon())
        self.resize(1280, 840)
        self.setMinimumSize(880, 640)
        self.setAcceptDrops(True)
        central = QWidget()
        self.setCentralWidget(central)
        shell = QHBoxLayout(central)
        shell.setContentsMargins(16, 16, 16, 16)
        shell.setSpacing(20)
        sidebar = QWidget()
        sidebar.setObjectName("sidebar")
        side = QVBoxLayout(sidebar)
        brand_row = QHBoxLayout()
        brand_icon = QLabel()
        brand_icon.setPixmap(self.windowIcon().pixmap(32, 32))
        brand_row.addWidget(brand_icon)
        brand = QLabel("FileConverter")
        brand.setObjectName("brand")
        brand_row.addWidget(brand)
        side.addLayout(brand_row)
        subtitle = QLabel("PRIVATE BY DESIGN")
        subtitle.setObjectName("eyebrow")
        side.addWidget(subtitle)
        self.navigation = QListWidget()
        self.navigation.addItems(
            [
                "Convert",
                "Compress",
                "Batch",
                "Watched Folders",
                "Media Tools",
                "Presets",
                "History",
                "Settings",
            ]
        )
        self.navigation.setAccessibleName("Primary navigation")
        self.navigation.setFixedWidth(190)
        side.addWidget(self.navigation, 1)
        side.addWidget(button("About & diagnostics", self.about))
        local = QLabel("Local files. Local processing.\nNo uploads. No telemetry.")
        local.setObjectName("muted")
        side.addWidget(local)
        shell.addWidget(sidebar)
        content = QVBoxLayout()
        self.title = QLabel("Convert")
        self.title.setObjectName("title")
        self.description = QLabel("Make your files work everywhere.")
        self.description.setObjectName("muted")
        content.addWidget(self.title)
        content.addWidget(self.description)
        self.pages = QStackedWidget()
        content.addWidget(self.pages, 1)
        shell.addLayout(content, 1)
        self.build_conversion()
        self.build_watchers()
        self.build_presets()
        self.build_history()
        self.build_settings()
        self.navigation.currentRowChanged.connect(self.navigate)
        self.navigation.setCurrentRow(0)
        self.bridge.changed.connect(self.job_changed)
        self.bridge.terminal.connect(self.job_terminal)
        self.bridge.error.connect(self.error)
        self.bridge.done.connect(self.background_done)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(700)
        self.estimate_timer = QTimer(self)
        self.estimate_timer.setSingleShot(True)
        self.estimate_timer.timeout.connect(self.estimate)
        self.editor.changed = lambda: self.estimate_timer.start(250)
        self.preview_timer = QTimer(self)
        self.preview_timer.setSingleShot(True)
        self.preview_timer.timeout.connect(self.render_preview)
        self.tray = QSystemTrayIcon(self.windowIcon(), self)
        menu = QMenu()
        for label, callback in [
            ("Open FileConverter", self.show_from_tray),
            ("Pause / resume folder watching", self.toggle_watching),
            ("Exit", self.exit_app),
        ]:
            action = menu.addAction(label)
            action.triggered.connect(callback)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda reason: (
                self.show_from_tray()
                if reason == QSystemTrayIcon.ActivationReason.DoubleClick
                else None
            )
        )
        self.tray.messageClicked.connect(self.show_from_tray)
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()
        for key, callback in [
            ("Ctrl+O", self.add_files),
            ("Ctrl+Shift+O", self.add_folder),
            ("Ctrl+Return", self.start),
            ("Ctrl+,", lambda: self.navigation.setCurrentRow(7)),
            ("Delete", self.remove_selected),
        ]:
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.activated.connect(callback)
        self.apply_theme()
        preferences = self.store.settings
        self.editor.load(
            Options(
                quality=preferences["default_quality"],
                image_quality=preferences["default_image_quality"],
                metadata=preferences["metadata"],
                acceleration=preferences["acceleration"],
                collision=preferences["collision"],
                suffix=preferences["suffix"],
            )
        )
        self.refresh()

    def build_conversion(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 12, 0, 0)
        actions = QHBoxLayout()
        actions.addWidget(button("Add files", self.add_files))
        actions.addWidget(button("Add folder", self.add_folder))
        actions.addWidget(button("Merge images / PDFs", self.merge))
        actions.addWidget(button("Create ZIP", self.archive))
        actions.addStretch()
        self.preset_select = combo([p["id"] for p in self.store.records("preset")])
        self.preset_select.setCurrentText("Balanced")
        actions.addWidget(self.preset_select)
        actions.addWidget(button("Apply preset", self.apply_preset))
        layout.addLayout(actions)
        split = QSplitter()
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        self.drop = QLabel("Drop files or folders here\nInspect → configure → validate → save")
        self.drop.setObjectName("dropzone")
        self.drop.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.drop.setMinimumHeight(88)
        self.drop.setAccessibleName("Drop files or folders here, or use Add files")
        ll.addWidget(self.drop)
        self.import_list = QListWidget()
        self.import_list.setSelectionMode(QListWidget.SelectionMode.ExtendedSelection)
        self.import_list.setAccessibleName("Inspected files")
        self.import_list.currentRowChanged.connect(self.select_import)
        ll.addWidget(self.import_list, 1)
        row = QHBoxLayout()
        row.addWidget(button("Exclude selected", self.exclude_imports))
        row.addWidget(button("Inspect source", self.inspect_source))
        row.addWidget(button("Queue selected", self.enqueue_selected, True))
        row.addWidget(button("Queue all", self.enqueue_all))
        ll.addLayout(row)
        self.preview_image = QLabel()
        self.preview_image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_image.setMaximumHeight(120)
        ll.addWidget(self.preview_image)
        self.source_details = QLabel(
            "Choose a file to inspect its actual format, codecs and metadata."
        )
        self.source_details.setWordWrap(True)
        self.source_details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        ll.addWidget(self.source_details)
        self.estimate_label = QLabel("")
        self.estimate_label.setWordWrap(True)
        self.estimate_label.setAccessibleName("Conversion estimate")
        ll.addWidget(self.estimate_label)
        split.addWidget(left)
        self.editor = OptionsEditor(self.cap)
        split.addWidget(self.editor)
        split.setSizes([480, 460])
        layout.addWidget(split, 3)
        self.destination_widget, self.destination = folder_field(self.store.settings["output"])
        self.destination_mode = combo(["Each file's source folder", "Choose output folder"])
        self.destination_mode.setAccessibleName("Output location")
        self.destination_mode.setToolTip(
            "Keep each output alongside its own source, or send all outputs to a chosen folder. Originals are protected."
        )
        self.destination_mode.setCurrentIndex(1 if self.destination.text().strip() else 0)
        self.destination_mode.currentIndexChanged.connect(self.update_destination_mode)
        self.destination.textChanged.connect(
            lambda text: self.destination_mode.setCurrentIndex(1 if text.strip() else 0)
        )
        row = QHBoxLayout()
        row.addWidget(QLabel("Save outputs to"))
        row.addWidget(self.destination_mode)
        row.addWidget(self.destination_widget, 1)
        layout.addLayout(row)
        self.update_destination_mode()
        self.job_model = JobTable()
        self.job_table = self.make_table(self.job_model)
        self.job_table.setMinimumHeight(150)
        self.job_table.doubleClicked.connect(
            lambda index: self.inspect_job(self.job_model.rows[index.row()])
        )
        self.job_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.job_table.customContextMenuRequested.connect(self.job_context)
        layout.addWidget(self.job_table, 2)
        self.aggregate = QProgressBar()
        self.aggregate.setAccessibleName("Aggregate queue progress")
        layout.addWidget(self.aggregate)
        controls = QHBoxLayout()
        for label, callback in [
            ("Start queue", self.start),
            ("Pause / resume queue", self.pause_queue),
            ("Cancel selected", self.cancel_selected),
            ("Retry failed", self.retry_failed),
            ("Move up", lambda: self.reorder(-1)),
            ("Move down", lambda: self.reorder(1)),
            ("Clear completed", self.clear_completed),
        ]:
            controls.addWidget(button(label, callback, label == "Start queue"))
        layout.addLayout(controls)
        self.pages.addWidget(page)

    def make_table(self, model):
        table = QTableView()
        table.setModel(model)
        table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        table.setSelectionMode(QTableView.SelectionMode.ExtendedSelection)
        table.setAlternatingRowColors(True)
        table.setWordWrap(False)
        table.verticalHeader().hide()
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        table.setAccessibleName("Conversion jobs")
        return table

    def build_watchers(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        actions = QHBoxLayout()
        for label, callback in [
            ("Add watcher", lambda: self.edit_watcher()),
            ("Edit", lambda: self.edit_watcher(self.selected_watcher())),
            ("Enable / pause", self.toggle_watcher),
            ("Scan now", self.scan_watcher),
            ("Dry run", self.preview_watcher),
            ("Duplicate", self.duplicate_watcher),
            ("Delete", self.delete_watcher),
            ("Pause / resume all", self.toggle_watching),
        ]:
            actions.addWidget(button(label, callback))
        layout.addLayout(actions)
        self.watcher_list = QListWidget()
        self.watcher_list.setAccessibleName("Watched folders")
        layout.addWidget(self.watcher_list, 1)
        filters = QHBoxLayout()
        self.activity_search = QLineEdit()
        self.activity_search.setPlaceholderText("Filter activity by filename, status, or date")
        filters.addWidget(self.activity_search)
        filters.addWidget(button("View activity", self.activity))
        layout.addLayout(filters)
        self.activity_text = QPlainTextEdit()
        self.activity_text.setReadOnly(True)
        self.activity_text.setAccessibleName("Watcher activity")
        layout.addWidget(self.activity_text, 1)
        self.pages.addWidget(page)

    def build_presets(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        self.preset_list = QListWidget()
        self.preset_list.setAccessibleName("Saved conversion presets")
        layout.addWidget(self.preset_list)
        row = QHBoxLayout()
        for label, callback in [
            ("Create from current settings", self.save_preset),
            ("Edit", self.edit_preset),
            ("Rename", self.rename_preset),
            ("Duplicate", self.duplicate_preset),
            ("Delete custom", self.delete_preset),
            ("Export", self.export_config),
            ("Import", self.import_config),
        ]:
            row.addWidget(button(label, callback))
        layout.addLayout(row)
        note = QLabel(
            "Service-size presets are editable convenience limits, not permanent claims about third-party services. Watchers follow preset edits and record the exact settings used."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        self.pages.addWidget(page)

    def build_history(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        self.history_search = QLineEdit()
        self.history_search.setPlaceholderText("Search all history by input, output or status")
        self.history_search.textChanged.connect(self.refresh_history)
        layout.addWidget(self.history_search)
        self.history_model = JobTable()
        self.history_table = self.make_table(self.history_model)
        self.history_table.doubleClicked.connect(
            lambda index: self.inspect_job(self.history_model.rows[index.row()])
        )
        layout.addWidget(self.history_table)
        actions = QHBoxLayout()
        for label, callback in [
            ("Open", lambda: self.history_action("open")),
            ("Reveal", lambda: self.history_action("reveal")),
            ("Repeat", lambda: self.history_action("repeat")),
            ("Copy path", lambda: self.history_action("copy")),
            ("Remove record", lambda: self.history_action("delete")),
            ("Clear history", self.clear_history),
            ("Previous", lambda: self.history_page(-1)),
            ("Next", lambda: self.history_page(1)),
        ]:
            actions.addWidget(button(label, callback))
        layout.addLayout(actions)
        self.pages.addWidget(page)

    def build_settings(self):
        inner = QWidget()
        form = QFormLayout(inner)
        settings = self.store.settings
        self.theme = combo(["system", "light", "dark"])
        self.theme.setCurrentText(settings["theme"])
        self.workers = spin(1, 4, settings["workers"])
        self.default_quality = spin(0, 51, settings["default_quality"])
        self.default_image_quality = spin(1, 100, settings["default_image_quality"])
        self.close_behavior = combo(["ask", "tray", "exit"])
        self.close_behavior.setCurrentText(settings["close_behavior"])
        self.completion = combo(["nothing", "open destination", "sleep", "shutdown"])
        self.completion.setCurrentText(settings["completion"])
        self.notifications = QCheckBox("Completion notifications")
        self.notifications.setChecked(settings["notifications"])
        self.failure_notifications = QCheckBox("Failure notifications")
        self.failure_notifications.setChecked(settings["failure_notifications"])
        self.default_output_widget, self.default_output = folder_field(settings["output"])
        self.default_metadata = combo(["strip", "minimal", "preserve"])
        self.default_metadata.setCurrentText(settings["metadata"])
        self.default_acceleration = combo(["software", "auto", "hardware"])
        self.default_acceleration.setCurrentText(settings["acceleration"])
        self.default_collision = combo(["rename", "skip", "replace", "newer"])
        self.default_collision.setCurrentText(settings["collision"])
        self.default_suffix = QLineEdit(settings["suffix"])
        for label, widget in [
            ("Appearance", self.theme),
            ("Concurrent encodes (maximum 4)", self.workers),
            ("Default video CRF", self.default_quality),
            ("Default image quality", self.default_image_quality),
            ("Default output folder", self.default_output_widget),
            ("Default metadata", self.default_metadata),
            ("Acceleration preference", self.default_acceleration),
            ("Collision policy", self.default_collision),
            ("Filename suffix", self.default_suffix),
            ("Close with active watchers", self.close_behavior),
            ("Queue completion action", self.completion),
            ("Notifications", self.notifications),
            ("Errors", self.failure_notifications),
        ]:
            form.addRow(label, widget)
            widget.setAccessibleName(label)
        form.addRow(button("Save settings", self.save_settings, True))
        form.addRow(button("Reset settings", self.reset_settings))
        self.dependencies = QPlainTextEdit()
        self.dependencies.setReadOnly(True)
        self.dependencies.setMinimumHeight(180)
        form.addRow("Dependencies", self.dependencies)
        form.addRow(
            button(
                "Refresh capabilities", lambda: self.background("capabilities", self.cap.refresh)
            )
        )
        form.addRow(button("Copy diagnostics", self.copy_diagnostics))
        form.addRow(button("Export diagnostic report", self.export_diagnostics))
        form.addRow(button("Export presets / appearance", self.export_config))
        form.addRow(button("Import presets / appearance", self.import_config))
        note = QLabel(
            "LibreOffice: install from libreoffice.org for Office conversion. FFmpeg: repair the packaged app or install a trusted build on PATH. Hardware encoder discovery does not prove device availability; failed hardware jobs fall back to software. Updates are manual: install a trusted new release over this installation."
        )
        note.setWordWrap(True)
        form.addRow(note)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(inner)
        self.pages.addWidget(scroll)

    def navigate(self, index):
        names = [
            "Convert",
            "Compress",
            "Batch",
            "Watched Folders",
            "Media Tools",
            "Presets",
            "History",
            "Settings",
        ]
        self.title.setText(names[index])
        descriptions = [
            "Make your files work everywhere.",
            "A maximum size, checked before your output is saved.",
            "One queue. Shared settings or individual choices.",
            "Stable files, real metadata, safe automation.",
            "Trim, resize, remux, extract and refine.",
            "Your workflow, saved.",
            "A local record of validated work.",
            "Make FileConverter yours.",
        ]
        self.description.setText(descriptions[index])
        self.pages.setCurrentIndex({0: 0, 1: 0, 2: 0, 3: 1, 4: 0, 5: 2, 6: 3, 7: 4}[index])
        if index == 1:
            self.editor.tabs.setCurrentIndex(1)
            self.editor.compression.setCurrentIndex(1)
        elif index == 4:
            self.editor.tabs.setCurrentIndex(2)
        elif index == 0:
            self.editor.tabs.setCurrentIndex(0)
        self.dirty = True
        self.refresh_history()
        self.refresh_watchers()
        self.refresh_presets()
        self.dependencies.setPlainText(json.dumps(self.cap.diagnostics(), indent=2))

    def background(self, tag, fn):
        if self.shutdown_event.is_set():
            return

        def run():
            try:
                result = fn()
                if not self.shutdown_event.is_set():
                    self.bridge.done.emit(tag, result)
            except Cancelled:
                pass
            except Exception as error:
                if not self.shutdown_event.is_set():
                    self.bridge.error.emit(str(error))
            finally:
                self.background_threads.discard(threading.current_thread())

        thread = threading.Thread(target=run, daemon=True)
        self.background_threads.add(thread)
        thread.start()

    def add_files(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Add files")
        if files:
            self.import_paths(files)

    def add_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Add folder recursively")
        if folder:
            self.import_paths([folder])

    def import_paths(self, paths):
        self.drop.setText("Inspecting files… You can keep working.")

        def inspect():
            count = skipped = 0
            for root in paths:
                files = (
                    scan_files(root, True, self.shutdown_event)
                    if Path(root).is_dir()
                    else [Path(root)]
                )
                for path in files:
                    if self.shutdown_event.is_set():
                        return
                    if count >= 10000:
                        raise ValueError(
                            "Import limit is 10,000 files; use watched folders for larger libraries"
                        )
                    try:
                        info = self.engine.detector.inspect(path, self.shutdown_event)
                        self.bridge.done.emit("import-file", info)
                        count += 1
                    except Exception:
                        skipped += 1
            return dict(added=count, skipped=skipped)

        self.background("import", inspect)

    def background_done(self, tag, result):
        if tag == "import-file":
            if result.path not in self.infos:
                self.infos[result.path] = result
                self.imports.append(result.path)
                self.import_list.addItem(
                    f"{Path(result.path).name}    {result.category} · {size(result.size)}"
                )
                if self.import_list.currentRow() < 0:
                    self.import_list.setCurrentRow(0)
        elif tag == "import":
            self.drop.setText(
                f"{result['added']} inspected · {result['skipped']} unreadable/unsupported skipped\nSelect files to configure or exclude before queuing."
            )
        elif tag == "watch-preview":
            self.text_dialog(
                "Watcher dry run — no files changed",
                json.dumps(result, indent=2, ensure_ascii=False),
            )
        elif tag == "capabilities":
            self.dependencies.setPlainText(json.dumps(self.cap.diagnostics(), indent=2))
            if self.editor.info:
                self.editor.set_info(self.editor.info)
        elif tag == "merge":
            QMessageBox.information(self, "PDF saved", str(result))
        elif tag == "archive":
            QMessageBox.information(
                self,
                "Archive validated",
                f"{result['files']} files · {size(result['size'])}\n{result['output']}",
            )
        elif tag == "archive-progress":
            self.statusBar().showMessage(
                f"ZIP compression: {result} files written; output will be validated before saving"
            )
        elif tag == f"thumbnail:{self.preview_generation}":
            pixmap = QPixmap()
            pixmap.loadFromData(result)
            self.preview_image.setPixmap(
                pixmap.scaled(
                    240,
                    120,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        elif tag.startswith("comparison:") and tag in self.comparisons:
            for label, payload in zip(self.comparisons[tag], result, strict=True):
                pixmap = QPixmap()
                pixmap.loadFromData(payload)
                label.setPixmap(pixmap)

        elif tag == "history":
            self.history_busy = False
            request, rows, error = result
            if error:
                self.error(error)
                return
            if request != self.history_query:
                self.refresh_history()
                return
            selected = {
                self.history_model.rows[i.row()].id
                for i in self.history_table.selectionModel().selectedRows()
            }
            self.history_model.set_rows(rows)
            for index, job in enumerate(rows):
                if job.id in selected:
                    self.history_table.selectionModel().select(
                        self.history_model.index(index, 0),
                        QItemSelectionModel.SelectionFlag.Select
                        | QItemSelectionModel.SelectionFlag.Rows,
                    )
        elif tag == "history-cleared":
            self.refresh_history()

    def select_import(self, index):
        if not 0 <= index < len(self.imports):
            return
        info = self.infos[self.imports[index]]
        self.editor.set_info(info)
        streams = ", ".join(
            f"#{s.get('index')} {s.get('codec_type')} {s.get('codec_name')}" for s in info.streams
        )
        self.source_details.setText(
            f"{info.format} · {size(info.size)} · {info.duration:.2f}s\n{info.width}×{info.height} · {info.fps:.3f} FPS · {info.bitrate / 1000:.0f} kbps\nAudio: {info.audio_codec or 'none'} · {info.audio_bitrate / 1000:.0f} kbps · {info.channels} ch · {info.sample_rate} Hz\n{streams}\n"
            + "\n".join(info.warnings)
        )
        self.preview_image.clear()
        self.preview_cancel.set()
        self.preview_cancel = threading.Event()
        self.preview_generation += 1
        self.preview_timer.start(250)
        self.estimate()

    def render_preview(self):
        if not self.editor.info:
            return
        info = self.editor.info
        cancel = self.preview_cancel
        self.background(
            f"thumbnail:{self.preview_generation}", lambda: self.engine.preview(info, cancel)
        )

    def estimate(self):
        if not self.editor.info:
            return
        try:
            plan = self.engine.planner.plan(self.editor.info, self.editor.options())
            self.estimate_label.setText(
                plan.get("estimate", "") + "\n" + "\n".join(plan["warnings"])
            )
        except Exception as error:
            self.estimate_label.setText(str(error))

    def exclude_imports(self):
        for index in sorted([i.row() for i in self.import_list.selectedIndexes()], reverse=True):
            path = self.imports.pop(index)
            self.infos.pop(path, None)
            self.import_list.takeItem(index)

    def inspect_source(self):
        if self.editor.info:
            self.text_dialog(
                "Detected source properties",
                json.dumps(dataclasses.asdict(self.editor.info), indent=2, ensure_ascii=False),
            )
        else:
            self.error("Import and select a file first")

    def apply_preset(self):
        preset = self.store.get("preset", self.preset_select.currentText())
        if preset:
            options = Options(**preset["options"])
            if self.editor.info and options.format not in self.cap.outputs(
                self.editor.info.category, self.editor.info.format
            ):
                self.error("This preset is incompatible with the selected input")
                return
            self.editor.load(options)

    def update_destination_mode(self):
        self.destination_widget.setVisible(self.destination_mode.currentIndex() == 1)

    def output_destination(self, source):
        if self.destination_mode.currentIndex() == 0:
            return str(Path(source).resolve().parent)
        destination = self.destination.text().strip()
        if not destination:
            raise ValueError(
                "Choose an output folder using Browse, or select each file's source folder"
            )
        if not Path(destination).is_absolute():
            raise ValueError("Choose an absolute output folder")
        return str(Path(destination).resolve())

    def enqueue_selected(self):
        self.enqueue([self.imports[i.row()] for i in self.import_list.selectedIndexes()])

    def enqueue_all(self):
        self.enqueue(self.imports)

    def enqueue(self, paths):
        try:
            if not paths:
                raise ValueError("Select or import files first")
            options = self.editor.options()
            if (
                options.collision in ("replace", "newer")
                and QMessageBox.warning(
                    self,
                    "Replace existing outputs",
                    "Existing output files may be replaced. Original input files are protected. Continue?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                )
                != QMessageBox.StandardButton.Yes
            ):
                return
            # Verify every plan first so incompatible mixed batches do not partially enqueue.
            for path in paths:
                self.engine.planner.plan(self.infos[path], options)
                self.output_destination(path)
            for path in paths:
                self.queue.submit(
                    Job(
                        path,
                        self.output_destination(path),
                        dataclasses.replace(options),
                        preset_id=self.preset_select.currentText(),
                    ),
                    start=False,
                )
            self.dirty = True
            self.refresh()
        except Exception as error:
            self.error(str(error))

    def start(self):
        self.queue.start()
        self.dirty = True

    def pause_queue(self):
        self.queue.pause(not self.queue.paused)
        self.statusBar().showMessage(
            "Queue paused; active encodes continue" if self.queue.paused else "Queue resumed"
        )

    def selected_jobs(self):
        return [
            self.job_model.rows[i.row()] for i in self.job_table.selectionModel().selectedRows()
        ]

    def cancel_selected(self):
        for job in self.selected_jobs():
            self.queue.cancel(job.id)

    def remove_selected(self):
        if self.import_list.hasFocus():
            self.exclude_imports()
            return
        for job in self.selected_jobs():
            try:
                self.queue.remove(job.id)
            except Exception as error:
                self.error(str(error))
        self.dirty = True

    def retry_failed(self):
        for job in list(self.queue.jobs):
            if job.state in (State.FAILED, State.INTERRUPTED, State.CANCELLED):
                self.queue.retry(job)
        self.dirty = True

    def reorder(self, delta):
        for job in self.selected_jobs():
            self.queue.reorder(job.id, delta)
        self.dirty = True

    def clear_completed(self):
        # Keep history; clear only the current session's visible queue.
        with self.queue.lock:
            self.queue.jobs = [j for j in self.queue.jobs if j.state != State.COMPLETED]
        self.dirty = True

    def job_changed(self, job):
        self.dirty = True

    def job_terminal(self, job):
        self.dirty = True
        self.statusBar().showMessage(f"{Path(job.source).name}: {job.state.value}", 15000)
        self.statusBar().setAccessibleName(
            f"Conversion {job.state.value.lower()}: {Path(job.source).name}"
        )
        prefs = self.store.settings
        watcher = self.watchers.watchers.get(job.watcher_id)
        notify = (
            prefs["notifications"]
            if job.state == State.COMPLETED
            else prefs["failure_notifications"]
        )
        if (
            notify
            and (not watcher or watcher.notifications)
            and time.monotonic() - getattr(self, "last_notification", 0) > 5
        ):
            self.last_notification = time.monotonic()
            self.tray.showMessage(
                "Conversion " + job.state.value.lower(),
                Path(job.source).name,
                QSystemTrayIcon.MessageIcon.Information
                if job.state == State.COMPLETED
                else QSystemTrayIcon.MessageIcon.Warning,
            )
        # All terminal signals are queued on the UI thread; check completion after workers release.
        QTimer.singleShot(500, self.completion_action)

    def completion_action(self):
        if self.queue.active or self.queue.pending:
            return
        generation = tuple(j.id for j in self.queue.jobs if j.state in TERMINAL)
        if generation == getattr(self, "completion_generation", None):
            return
        self.completion_generation = generation
        action = self.store.settings["completion"]
        if action == "open destination":
            completed = [j for j in self.queue.jobs if j.output]
            if completed:
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(completed[-1].output).parent)))
        elif action in ("sleep", "shutdown"):
            # Require immediate confirmation as well as explicit setting.
            if os.name != "nt":
                self.error("Sleep and shutdown completion actions require Windows")
                return
            if (
                QMessageBox.question(
                    self,
                    "Completion action",
                    f"Queue finished. {action.title()} this computer now?",
                )
                == QMessageBox.StandardButton.Yes
            ):
                import subprocess

                if action == "shutdown":
                    subprocess.Popen(["shutdown.exe", "/s", "/t", "30"])
                else:
                    import ctypes

                    ctypes.windll.powrprof.SetSuspendState(False, False, False)

    def refresh(self):
        if self.dirty:
            self.dirty = False
            selected = {j.id for j in self.selected_jobs()}
            self.job_model.set_rows(self.queue.jobs)
            for index, job in enumerate(self.job_model.rows):
                if job.id in selected:
                    self.job_table.selectionModel().select(
                        self.job_model.index(index, 0),
                        QItemSelectionModel.SelectionFlag.Select
                        | QItemSelectionModel.SelectionFlag.Rows,
                    )
            jobs = self.queue.jobs
            aggregate = sum(j.progress for j in jobs) / len(jobs) if jobs else 0
            self.aggregate.setValue(int(aggregate))
            self.aggregate.setFormat(
                f"{len(self.queue.active)} active · {len(self.queue.pending)} waiting · {aggregate:.0f}%"
            )
        if self.navigation.currentRow() == 3:
            self.refresh_watchers()
        if self.navigation.currentRow() == 6:
            self.refresh_history()

    def job_context(self, point):
        index = self.job_table.indexAt(point)
        if not index.isValid():
            return
        job = self.job_model.rows[index.row()]
        menu = QMenu(self)
        for label, callback in [
            ("Inspect plan & diagnostics", lambda: self.inspect_job(job)),
            ("Open output", lambda: self.open_job(job)),
            ("Reveal output", lambda: self.reveal_job(job)),
            ("Copy output path", lambda: QApplication.clipboard().setText(job.output)),
            ("Convert again", lambda: self.queue.retry(job)),
            ("Cancel", lambda: self.queue.cancel(job.id)),
            ("Remove from queue and history", lambda: self.remove_job(job)),
        ]:
            action = menu.addAction(label)
            action.triggered.connect(callback)
        menu.exec(self.job_table.viewport().mapToGlobal(point))

    def remove_job(self, job):
        try:
            self.queue.remove(job.id)
            self.dirty = True
        except Exception as error:
            self.error(str(error))

    def open_job(self, job):
        if not job.output or not Path(job.output).exists():
            self.error("No completed output is available")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(job.output))

    def reveal_job(self, job):
        if not job.output:
            self.error("No completed output is available")
            return
        if os.name == "nt":
            import subprocess

            subprocess.Popen(["explorer.exe", "/select,", str(Path(job.output))])
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(job.output).parent)))

    def inspect_job(self, job):
        dialog = QDialog(self)
        dialog.setWindowTitle("Conversion details")
        dialog.resize(760, 640)
        layout = QVBoxLayout(dialog)
        tabs = QTabWidget()
        summary = QWidget()
        form = QFormLayout(summary)
        result = job.result
        for label, value in [
            ("Source", job.source),
            ("Output", job.output or "No published output"),
            ("Status", job.state.value),
            ("Source format", job.info.get("format", "—")),
            ("Original size", size(result.get("original_size", job.info.get("size")))),
            ("Output size", size(result.get("size"))),
            ("Size difference", size(result.get("size", 0) - result.get("original_size", 0))),
            ("Compression", f"{result.get('saved_percent', 0):.1f}%"),
            ("Duration", f"{result.get('duration', 0):.2f}s"),
            ("Conversion time", f"{result.get('seconds', 0):.2f}s"),
            ("Validation", "Passed" if result.get("valid") else "Not completed"),
            ("Error", job.error or "None"),
        ]:
            field = QLabel(str(value))
            field.setTextFormat(Qt.TextFormat.PlainText)
            field.setWordWrap(True)
            field.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            form.addRow(label, field)
        tabs.addTab(summary, "Result")
        if (
            job.info.get("category") == "image"
            and job.output
            and job.options.format in self.cap.images
        ):
            comparison = QWidget()
            columns = QHBoxLayout(comparison)
            labels = []
            for title in ("Original", "Converted"):
                column = QVBoxLayout()
                column.addWidget(QLabel(title))
                image_label = QLabel("Rendering preview…")
                image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
                image_label.setAccessibleName(title + " image preview")
                column.addWidget(image_label, 1)
                columns.addLayout(column)
                labels.append(image_label)
            tag = "comparison:" + uuid.uuid4().hex
            self.comparisons[tag] = labels
            dialog.finished.connect(lambda result, key=tag: self.comparisons.pop(key, None))

            def previews():
                return [
                    self.engine.preview(
                        self.engine.detector.inspect(path, self.shutdown_event), self.shutdown_event
                    )
                    for path in (job.source, job.output)
                ]

            self.background(tag, previews)
            tabs.addTab(comparison, "Before / after")
        technical = QPlainTextEdit(json.dumps(job.to_dict(), indent=2, ensure_ascii=False))
        technical.setReadOnly(True)
        technical.setAccessibleName("Execution plan, backend arguments and diagnostics")
        tabs.addTab(technical, "Technical inspector")
        layout.addWidget(tabs)
        controls = QHBoxLayout()
        for label, callback in [
            ("Open", lambda: self.open_job(job)),
            ("Reveal", lambda: self.reveal_job(job)),
            ("Copy path", lambda: QApplication.clipboard().setText(job.output)),
            ("Convert again", lambda: self.queue.retry(job)),
            ("Copy diagnostics", lambda: QApplication.clipboard().setText(technical.toPlainText())),
        ]:
            controls.addWidget(button(label, callback))
        controls.addWidget(button("Close", dialog.accept))
        layout.addLayout(controls)
        dialog.exec()

    def text_dialog(self, title, text):
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        dialog.resize(820, 650)
        layout = QVBoxLayout(dialog)
        content = QPlainTextEdit(text)
        content.setReadOnly(True)
        content.setAccessibleName(title)
        layout.addWidget(content)
        row = QHBoxLayout()
        row.addWidget(button("Copy", lambda: QApplication.clipboard().setText(text)))
        row.addStretch()
        row.addWidget(button("Close", dialog.accept))
        layout.addLayout(row)
        dialog.exec()

    def selected_watcher(self):
        item = self.watcher_list.currentItem()
        return self.watchers.watchers.get(item.data(Qt.ItemDataRole.UserRole)) if item else None

    def edit_watcher(self, watcher=None):
        WatcherDialog(self, watcher).exec()
        self.refresh_watchers()

    def refresh_watchers(self):
        selected = self.selected_watcher()
        self.watcher_list.clear()
        for watcher in sorted(self.watchers.watchers.values(), key=lambda w: (-w.priority, w.name)):
            activities = self.store.activities(watcher.id, limit=100)
            counts = {
                status: sum(a["status"] == status for a in activities)
                for status in ("MATCHED", "COMPLETED", "SKIPPED", "FAILED")
            }
            state = (
                "Globally paused"
                if self.watchers.paused
                else self.watchers.states.get(
                    watcher.id, "Starting" if watcher.enabled else "Paused"
                )
            )
            self.watcher_list.addItem(
                f"{watcher.name} · {state}\n{watcher.path} · {'Recursive' if watcher.recursive else 'Root only'} · {watcher.preset_id}\nOutput: {watcher.output_mode} {watcher.destination} · source: {watcher.source_action}\nRecent activity: detected {counts['MATCHED']} · converted {counts['COMPLETED']} · skipped {counts['SKIPPED']} · failed {counts['FAILED']}"
            )
            item = self.watcher_list.item(self.watcher_list.count() - 1)
            item.setData(Qt.ItemDataRole.UserRole, watcher.id)
            if selected and watcher.id == selected.id:
                self.watcher_list.setCurrentItem(item)

    def toggle_watcher(self):
        watcher = self.selected_watcher()
        if watcher:
            watcher.enabled = not watcher.enabled
            self.watchers.save(watcher)
            self.refresh_watchers()

    def scan_watcher(self):
        watcher = self.selected_watcher()
        if watcher:
            self.watchers.request_scan(watcher.id)

    def preview_watcher(self):
        watcher = self.selected_watcher()
        if watcher:
            self.background(
                "watch-preview", lambda: self.watchers.preview(watcher, self.shutdown_event)
            )

    def duplicate_watcher(self):
        watcher = self.selected_watcher()
        if watcher:
            duplicate = dataclasses.replace(
                watcher, id=uuid.uuid4().hex, name=watcher.name + " copy", enabled=False
            )
            self.watchers.save(duplicate)
            self.refresh_watchers()

    def delete_watcher(self):
        watcher = self.selected_watcher()
        if (
            watcher
            and QMessageBox.question(
                self,
                "Delete watcher",
                "Delete this watcher configuration? Source/output files are kept.",
            )
            == QMessageBox.StandardButton.Yes
        ):
            self.watchers.delete(watcher.id)
            self.refresh_watchers()

    def toggle_watching(self):
        self.watchers.global_pause(not self.watchers.paused)
        self.refresh_watchers()

    def activity(self):
        watcher = self.selected_watcher()
        query = self.activity_search.text().lower()
        lines = []
        for record in self.store.activities(watcher.id if watcher else ""):
            timestamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(record["time"]))
            line = f"{timestamp} {record['watcher']} {record['status']} {record['source']} {json.dumps(record['details'], ensure_ascii=False)}"
            if query in line.lower():
                lines.append(line)
        self.activity_text.setPlainText("\n".join(lines))

    def refresh_presets(self):
        selected = self.preset_list.currentItem().text() if self.preset_list.currentItem() else ""
        self.preset_list.clear()
        names = [p["id"] for p in self.store.records("preset")]
        self.preset_list.addItems(names)
        items = self.preset_list.findItems(selected, Qt.MatchFlag.MatchExactly)
        if items:
            self.preset_list.setCurrentItem(items[0])
        current = self.preset_select.currentText()
        self.preset_select.clear()
        self.preset_select.addItems(names)
        self.preset_select.setCurrentText(current)

    def selected_preset(self):
        item = self.preset_list.currentItem()
        return self.store.get("preset", item.text()) if item else None

    def ask_name(self, title, initial=""):
        from PySide6.QtWidgets import QInputDialog

        name, ok = QInputDialog.getText(self, title, "Preset name", text=initial)
        return name.strip()[:100] if ok else ""

    def save_preset(self):
        try:
            name = self.ask_name("Create preset")
            if not name:
                return
            if self.store.get("preset", name):
                raise ValueError("A preset with that name already exists")
            options = self.editor.options()
            self.store.put(
                "preset",
                name,
                dict(
                    id=name,
                    name=name,
                    version=1,
                    builtin=False,
                    options=dataclasses.asdict(options),
                ),
            )
            self.refresh_presets()
        except Exception as error:
            self.error(str(error))

    def edit_preset(self):
        preset = self.selected_preset()
        if not preset:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("Edit " + preset["name"])
        dialog.resize(680, 760)
        layout = QVBoxLayout(dialog)
        editor = OptionsEditor(self.cap)
        editor.load(Options(**preset["options"]))
        layout.addWidget(editor)
        row = QHBoxLayout()
        row.addWidget(button("Cancel", dialog.reject))

        def save():
            try:
                preset["options"] = dataclasses.asdict(editor.options())
                preset["version"] += 1
                self.store.put("preset", preset["id"], preset)
                dialog.accept()
            except Exception as error:
                self.error(str(error))

        row.addWidget(button("Save", save, True))
        layout.addLayout(row)
        dialog.exec()
        self.refresh_presets()

    def duplicate_preset(self):
        preset = self.selected_preset()
        if preset:
            name = self.ask_name("Duplicate preset", preset["name"] + " copy")
            if name and not self.store.get("preset", name):
                self.store.put(
                    "preset", name, preset | dict(id=name, name=name, version=1, builtin=False)
                )
                self.refresh_presets()

    def rename_preset(self):
        preset = self.selected_preset()
        if not preset:
            return
        if preset["builtin"]:
            self.error(
                "Built-in presets retain their identifiers. Duplicate to create a renamed custom preset."
            )
            return
        name = self.ask_name("Rename preset", preset["name"])
        if name and name != preset["id"]:
            if self.store.get("preset", name):
                self.error("Preset name already exists")
                return
            old = preset["id"]
            self.store.put("preset", name, preset | dict(id=name, name=name))
            for watcher in list(self.watchers.watchers.values()):
                if watcher.preset_id == old:
                    watcher.preset_id = name
                    self.watchers.save(watcher)
            self.store.delete("preset", old)
            self.refresh_presets()

    def delete_preset(self):
        preset = self.selected_preset()
        if preset and not preset["builtin"]:
            self.store.delete("preset", preset["id"])
            self.refresh_presets()

    def export_config(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Export presets / appearance", "fileconverter-presets.json", "JSON (*.json)"
        )
        if path:
            try:
                self.store.export_configuration(path)
            except Exception as error:
                self.error(str(error))

    def import_config(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Import presets / appearance", "", "JSON (*.json)"
        )
        if path:
            try:
                self.store.import_configuration(path)
                self.refresh_presets()
                self.apply_theme()
            except Exception as error:
                self.error(str(error))

    def refresh_history(self, *args):
        if not hasattr(self, "history_model"):
            return
        request = (self.history_search.text(), self.history_offset, self.store.generation)
        self.history_query = request
        if self.history_busy or request == self.history_last_request:
            return
        self.history_busy = True
        self.history_last_request = request

        def read():
            try:
                rows = self.store.jobs(
                    200, request[1], request[0], self.shutdown_event, terminal_only=True
                )
                return request, rows, ""
            except Exception as error:
                return request, [], str(error)

        self.background("history", read)

    def history_page(self, direction):
        self.history_offset = max(0, self.history_offset + direction * 200)
        self.refresh_history()

    def history_action(self, action):
        for index in self.history_table.selectionModel().selectedRows():
            job = self.history_model.rows[index.row()]
            if action == "open":
                self.open_job(job)
            elif action == "reveal":
                self.reveal_job(job)
            elif action == "repeat":
                self.queue.retry(job)
            elif action == "copy":
                QApplication.clipboard().setText(job.output)
            elif action == "delete":
                self.store.delete_job(job.id)
        self.refresh_history()

    def clear_history(self):
        if (
            QMessageBox.question(
                self, "Clear history", "Remove terminal history records? Files are kept."
            )
            == QMessageBox.StandardButton.Yes
        ):
            self.background("history-cleared", self.store.clear_history)

    def save_settings(self):
        try:
            completion = self.completion.currentText()
            if (
                completion == "shutdown"
                and QMessageBox.warning(
                    self,
                    "Shutdown behavior",
                    "Shutdown may close other applications. Each queue completion will ask again. Enable?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                )
                != QMessageBox.StandardButton.Yes
            ):
                return
            options = Options(suffix=self.default_suffix.text())
            options.validate()
            prefs = self.store.settings | dict(
                theme=self.theme.currentText(),
                workers=self.workers.value(),
                default_quality=self.default_quality.value(),
                default_image_quality=self.default_image_quality.value(),
                output=self.default_output.text(),
                metadata=self.default_metadata.currentText(),
                acceleration=self.default_acceleration.currentText(),
                collision=self.default_collision.currentText(),
                suffix=self.default_suffix.text(),
                close_behavior=self.close_behavior.currentText(),
                completion=completion,
                notifications=self.notifications.isChecked(),
                failure_notifications=self.failure_notifications.isChecked(),
            )
            self.store.save_settings(prefs)
            self.queue.max_workers = prefs["workers"]
            self.destination.setText(prefs["output"])
            self.apply_theme()
        except Exception as error:
            self.error(str(error))

    def reset_settings(self):
        from .store import DEFAULT_SETTINGS

        if (
            QMessageBox.question(
                self,
                "Reset settings",
                "Restore application preferences? Presets and history are preserved.",
            )
            == QMessageBox.StandardButton.Yes
        ):
            self.store.save_settings(DEFAULT_SETTINGS)
            self.theme.setCurrentText("system")
            self.workers.setValue(2)
            self.default_quality.setValue(23)
            self.default_image_quality.setValue(85)
            self.default_output.clear()
            self.default_metadata.setCurrentText("strip")
            self.default_acceleration.setCurrentText("software")
            self.default_collision.setCurrentText("rename")
            self.default_suffix.setText("-converted")
            self.close_behavior.setCurrentText("ask")
            self.completion.setCurrentText("nothing")
            self.notifications.setChecked(True)
            self.failure_notifications.setChecked(True)
            self.queue.max_workers = 2
            self.apply_theme()

    def apply_theme(self):
        theme = self.store.settings["theme"]
        dark = theme == "dark" or (
            theme == "system" and QApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark
        )
        background, surface, text, muted, border = (
            ("#151923", "#202633", "#eef2fa", "#a4afc4", "#363f51")
            if dark
            else ("#f3f5fa", "#ffffff", "#202b40", "#64728a", "#dce2ec")
        )
        app = QApplication.instance()
        assert isinstance(app, QApplication)
        palette = QPalette()
        for role, color in [
            (QPalette.ColorRole.Window, background),
            (QPalette.ColorRole.Base, surface),
            (QPalette.ColorRole.AlternateBase, background),
            (QPalette.ColorRole.Text, text),
            (QPalette.ColorRole.WindowText, text),
            (QPalette.ColorRole.Button, surface),
            (QPalette.ColorRole.ButtonText, text),
            (QPalette.ColorRole.Highlight, "#416de0"),
            (QPalette.ColorRole.HighlightedText, "#ffffff"),
        ]:
            palette.setColor(role, QColor(color))
        app.setPalette(palette)
        app.setStyleSheet(f"""
        QWidget {{ font-family: 'SF Pro Text', 'Segoe UI', 'Noto Sans', sans-serif; font-size: 13px; color:{text}; }}
        QMainWindow {{ background:{background}; }}
        #sidebar {{ background:{surface}; border-radius:18px; }}
        #brand {{ font-size:20px; font-weight:700; padding:12px 0; }}
        #eyebrow {{ font-size:10px; color:{muted}; letter-spacing:1px; }}
        #title {{ font-size:30px; font-weight:700; }}
        #muted {{ color:{muted}; }}
        #dropzone {{ background:{surface}; border:2px dashed {border}; border-radius:16px; color:{muted}; padding:16px; }}
        QPushButton {{ background:{surface}; border:1px solid {border}; border-radius:9px; padding:9px 12px; }}
        QPushButton:hover {{ border-color:#6284dd; }}
        QPushButton:pressed {{ background:{border}; }}
        QPushButton[primary=true] {{ background:#416de0; color:white; border-color:#416de0; font-weight:600; }}
        QPushButton:focus, QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus, QListWidget:focus, QTableView:focus {{ border:2px solid #678df1; }}
        QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QPlainTextEdit {{ background:{surface}; border:1px solid {border}; border-radius:8px; padding:6px; }}
        QListWidget, QTableView {{ background:{surface}; border:1px solid {border}; border-radius:12px; padding:5px; }}
        QListWidget::item {{ padding:12px 8px; border-radius:8px; }}
        QListWidget::item:selected {{ background:#416de0; color:white; }}
        QHeaderView::section {{ background:{background}; border:0; padding:8px; font-weight:600; }}
        QTableView::item {{ padding:8px; }}
        QProgressBar {{ background:{border}; border:0; border-radius:6px; min-height:20px; text-align:center; }}
        QProgressBar::chunk {{ background:#416de0; border-radius:6px; }}
        QTabWidget::pane {{ border:0; }}
        QTabBar::tab {{ background:{surface}; padding:10px 12px; border-radius:8px; margin:2px; }}
        QTabBar::tab:selected {{ color:#416de0; border-bottom:2px solid #416de0; }}
        QScrollArea {{ border:0; background:transparent; }}
        QMenu {{ background:{surface}; border:1px solid {border}; padding:6px; }}
        QMenu::item {{ padding:8px 20px; }}
        QMenu::item:selected {{ background:#416de0; color:white; }}
        """)

    def diagnostics(self):
        return dict(
            application="FileConverter",
            version=__version__,
            os=platform.platform(),
            dependencies=self.cap.diagnostics(),
            active_workers=len(self.queue.active),
            log_location=str(self.store.root / "diagnostics.log"),
        )

    def copy_diagnostics(self):
        QApplication.clipboard().setText(json.dumps(self.diagnostics(), indent=2))

    def export_diagnostics(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "Export diagnostics", "fileconverter-diagnostics.json", "JSON (*.json)"
        )
        if path:
            try:
                Path(path).write_text(json.dumps(self.diagnostics(), indent=2), encoding="utf-8")
            except Exception as error:
                self.error(str(error))

    def about(self):
        QMessageBox.about(
            self,
            "About FileConverter",
            f"FileConverter {__version__}\nLocal conversion with validated outputs.\nGPL-3.0 · Qt, FFmpeg, Pillow, PyMuPDF, watchdog.\nManual updates: install a trusted release. No automatic executable downloads.\nDependency licenses are included with the application.",
        )

    def merge(self):
        sources, _ = QFileDialog.getOpenFileNames(
            self,
            "Merge images or PDFs",
            "",
            "Images and PDF (*.png *.jpg *.jpeg *.webp *.tiff *.bmp *.pdf)",
        )
        if not sources:
            return
        output, _ = QFileDialog.getSaveFileName(
            self, "Save merged PDF", "merged.pdf", "PDF (*.pdf)"
        )
        if not output:
            return
        final = Path(output)
        if final.suffix.lower() not in ("", ".pdf"):
            self.error("Choose a PDF filename")
            return
        options = Options(
            format="pdf",
            operation="merge",
            filename_template=final.stem,
            suffix="",
            collision="skip",
        )
        self.queue.submit(Job(sources[0], str(final.parent), options, sources=sources))
        self.dirty = True

    def archive(self):
        menu = QMenu(self)
        files_action = menu.addAction("Compress selected files…")
        folder_action = menu.addAction("Compress a folder recursively…")
        choice = menu.exec(self.mapToGlobal(self.rect().center()))
        if choice == files_action:
            sources, _ = QFileDialog.getOpenFileNames(self, "Choose files to compress into ZIP")
        elif choice == folder_action:
            folder = QFileDialog.getExistingDirectory(self, "Choose folder to compress recursively")
            sources = [folder] if folder else []
        else:
            return
        if not sources:
            return
        output, _ = QFileDialog.getSaveFileName(
            self, "Save ZIP archive", "compressed.zip", "ZIP (*.zip)"
        )
        if not output:
            return
        final = Path(output)
        if final.suffix.lower() not in ("", ".zip"):
            self.error("Choose a ZIP filename")
            return
        options = Options(
            format="zip",
            operation="archive",
            filename_template=final.stem,
            suffix="",
            collision="skip",
        )
        self.queue.submit(Job(sources[0], str(final.parent), options, sources=sources))
        self.dirty = True

    def error(self, message):
        QMessageBox.warning(self, "FileConverter", message)

    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() and all(url.isLocalFile() for url in event.mimeData().urls()):
            event.acceptProposedAction()

    def dropEvent(self, event):
        self.import_paths(
            [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        )
        event.acceptProposedAction()

    def show_from_tray(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def exit_app(self):
        if (
            self.queue.active
            and QMessageBox.question(
                self,
                "Exit FileConverter",
                "Cancel active conversions and exit? Unfinished queue entries are preserved.",
            )
            != QMessageBox.StandardButton.Yes
        ):
            return
        self.force_exit = True
        self.close()

    def closeEvent(self, event):
        active_watchers = any(w.enabled for w in self.watchers.watchers.values())
        behavior = self.store.settings["close_behavior"]
        if not self.force_exit and active_watchers:
            if behavior == "ask":
                response = QMessageBox.question(
                    self,
                    "Folder watching is active",
                    "Keep FileConverter running in the system tray? Choose No to stop watching and exit.",
                    QMessageBox.StandardButton.Yes
                    | QMessageBox.StandardButton.No
                    | QMessageBox.StandardButton.Cancel,
                )
                if response == QMessageBox.StandardButton.Cancel:
                    event.ignore()
                    return
                behavior = "tray" if response == QMessageBox.StandardButton.Yes else "exit"
            if behavior == "tray" and QSystemTrayIcon.isSystemTrayAvailable():
                self.hide()
                event.ignore()
                return
        if not self.force_exit and self.queue.active:
            if (
                QMessageBox.question(
                    self, "Conversion in progress", "Cancel active conversions and exit?"
                )
                != QMessageBox.StandardButton.Yes
            ):
                event.ignore()
                return
        self.shutdown_event.set()
        self.preview_cancel.set()
        self.preview_timer.stop()
        self.timer.stop()
        self.watchers.close()
        try:
            self.queue.close()
            for thread in list(self.background_threads):
                thread.join(timeout=3)
            if any(thread.is_alive() for thread in self.background_threads):
                raise RuntimeError(
                    "Background work is still stopping. Try exiting again in a moment."
                )
        except RuntimeError as error:
            self.error(str(error))
            event.ignore()
            return
        self.tray.hide()
        self.store.close()
        self.closed = True
        event.accept()
