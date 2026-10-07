from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
from pathlib import Path

from PIL import Image, features

from .process import Runner

# Container constraints, intersected with discovered executable capabilities.
# This is a deliberately conservative compatibility policy, not a claim that all muxers convert.
VIDEO = {
    "mp4": ("mp4", ["libx264", "libx265", "libaom-av1"], "aac"),
    "mov": ("mov", ["libx264", "libx265"], "aac"),
    "mkv": ("matroska", ["libx264", "libx265", "libvpx-vp9", "libaom-av1"], "aac"),
    "webm": ("webm", ["libvpx-vp9", "libvpx", "libaom-av1"], "libopus"),
    "avi": ("avi", ["mpeg4", "libx264"], "libmp3lame"),
    "flv": ("flv", ["libx264", "flv"], "aac"),
    "vob": ("vob", ["mpeg2video"], "ac3"),
    "ogv": ("ogg", ["libtheora"], "libvorbis"),
    "mpg": ("mpeg", ["mpeg2video"], "mp2"),
    "mpeg": ("mpeg", ["mpeg2video"], "mp2"),
    "ts": ("mpegts", ["libx264", "libx265", "mpeg2video"], "aac"),
    "mts": ("mpegts", ["libx264"], "ac3"),
    "m2ts": ("mpegts", ["libx264"], "ac3"),
    "m4v": ("mp4", ["libx264"], "aac"),
    "3gp": ("3gp", ["libx264", "mpeg4"], "aac"),
    "gif": ("gif", ["gif"], ""),
}
AUDIO = {
    "mp3": ("mp3", ["libmp3lame"]),
    "m4a": ("ipod", ["aac", "alac"]),
    "aac": ("adts", ["aac"]),
    "flac": ("flac", ["flac"]),
    "aiff": ("aiff", ["pcm_s16be"]),
    "aif": ("aiff", ["pcm_s16be"]),
    "ogg": ("ogg", ["libvorbis", "libopus", "flac"]),
    "oga": ("ogg", ["libvorbis", "flac"]),
    "opus": ("opus", ["libopus"]),
    "wav": ("wav", ["pcm_s16le", "pcm_s24le", "pcm_f32le"]),
    "wma": ("asf", ["wmav2"]),
    "ac3": ("ac3", ["ac3"]),
}
LOSSLESS = {"flac", "alac", "pcm_s16le", "pcm_s24le", "pcm_f32le", "pcm_s16be"}
VIDEO_AUDIO = {
    "mp4": ["aac", "alac", "ac3", "libmp3lame"],
    "m4v": ["aac", "alac", "ac3", "libmp3lame"],
    "mov": ["aac", "alac", "pcm_s16le", "libmp3lame"],
    "mkv": ["aac", "libopus", "flac", "libvorbis", "ac3", "libmp3lame", "pcm_s16le"],
    "webm": ["libopus", "libvorbis"],
    "avi": ["libmp3lame", "ac3", "pcm_s16le"],
    "flv": ["aac", "libmp3lame"],
    "vob": ["ac3", "mp2"],
    "ogv": ["libvorbis"],
    "mpg": ["mp2"],
    "mpeg": ["mp2"],
    "ts": ["aac", "ac3", "mp2", "libmp3lame"],
    "mts": ["ac3", "aac"],
    "m2ts": ["ac3", "aac"],
    "3gp": ["aac"],
    "gif": [],
}
IMAGE_FORMATS = {
    "png": "PNG",
    "jpg": "JPEG",
    "jpeg": "JPEG",
    "webp": "WEBP",
    "gif": "GIF",
    "bmp": "BMP",
    "tiff": "TIFF",
    "ico": "ICO",
    "avif": "AVIF",
}
DOCUMENTS = {"docx", "odt", "rtf", "txt", "md", "html", "csv", "xlsx", "pdf"}


def dependency(name):
    roots = [Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[2])) / "vendor"]
    if getattr(sys, "frozen", False):
        roots.insert(0, Path(sys.executable).parent / "vendor")
    filename = name + (".exe" if os.name == "nt" else "")
    for root in roots:
        candidate = root / filename
        if candidate.is_file():
            return str(candidate)
    return shutil.which(name) or ""


class Capabilities:
    def __init__(self, runner: Runner, store=None):
        self.runner = runner
        self.store = store
        self.ffmpeg = dependency("ffmpeg")
        self.ffprobe = dependency("ffprobe")
        self.office = dependency("soffice")
        if not self.office and os.name == "nt":
            for root in (
                os.environ.get("PROGRAMFILES", ""),
                os.environ.get("PROGRAMFILES(X86)", ""),
            ):
                path = Path(root) / "LibreOffice/program/soffice.exe"
                if path.is_file():
                    self.office = str(path)
        self.encoders: set[str] = set()
        self.decoders: set[str] = set()
        self.muxers: set[str] = set()
        self.demuxers: set[str] = set()
        self.filters: set[str] = set()
        self.hardware: list[str] = []
        self.versions: dict[str, str] = {}
        self.errors: list[str] = []
        self.refresh()

    def refresh(self):
        self.errors.clear()
        self.ffmpeg, self.ffprobe, self.office = (
            dependency("ffmpeg"),
            dependency("ffprobe"),
            dependency("soffice"),
        )
        if not self.office and os.name == "nt":
            for root in (
                os.environ.get("PROGRAMFILES", ""),
                os.environ.get("PROGRAMFILES(X86)", ""),
            ):
                candidate = Path(root) / "LibreOffice/program/soffice.exe"
                if candidate.is_file():
                    self.office = str(candidate)
        self.versions.clear()
        if self.office and os.name == "nt" and Path(self.office).with_suffix(".com").is_file():
            self.office = str(Path(self.office).with_suffix(".com"))
        for field in ("encoders", "decoders", "muxers", "demuxers", "filters"):
            getattr(self, field).clear()
        self.hardware = []
        self.encoder_properties: dict[str, dict] = {}
        for name, executable in (
            ("FFmpeg", self.ffmpeg),
            ("ffprobe", self.ffprobe),
            ("LibreOffice", self.office),
        ):
            if executable:
                try:
                    self.versions[name] = self.runner.run(
                        [executable, "--version" if name == "LibreOffice" else "-version"],
                        timeout=30,
                    ).splitlines()[0]
                except Exception as error:
                    self.errors.append(f"{name}: {error}")
        signatures = {
            path: [Path(path).stat().st_size, Path(path).stat().st_mtime_ns]
            for path in (self.ffmpeg, self.ffprobe, self.office)
            if path and Path(path).is_file()
        }
        key = hashlib.sha256(
            json.dumps([self.versions, signatures], sort_keys=True).encode()
        ).hexdigest()
        cached = self.store.get("capabilities", key) if self.store else None
        if cached:
            for name in ("encoders", "decoders", "muxers", "demuxers", "filters"):
                setattr(self, name, set(cached[name]))
            self.hardware = cached["hardware"]
        elif self.ffmpeg:
            try:
                for name in ("encoders", "decoders", "muxers", "demuxers", "filters"):
                    output = self.runner.run([self.ffmpeg, "-hide_banner", "-" + name], timeout=30)
                    pattern = (
                        r"^\s+[A-Z.]{6}\s+(\S+)"
                        if name in ("encoders", "decoders")
                        else r"^\s+[A-Z.]{1,3}\s+(\S+)"
                    )
                    entries: set[str] = set()
                    for match in re.finditer(pattern, output, re.MULTILINE):
                        entries.update(match.group(1).split(","))
                    setattr(self, name, entries)
                self.hardware = self.runner.run(
                    [self.ffmpeg, "-hide_banner", "-hwaccels"], timeout=30
                ).splitlines()[1:]
                self.hardware = [name.strip() for name in self.hardware if name.strip()]
                if self.store:
                    self.store.put(
                        "capabilities",
                        key,
                        {
                            name: sorted(getattr(self, name))
                            for name in (
                                "encoders",
                                "decoders",
                                "muxers",
                                "demuxers",
                                "filters",
                                "hardware",
                            )
                        },
                    )
            except Exception as error:
                self.errors.append(str(error))
        Image.init()
        self.images = {ext: fmt for ext, fmt in IMAGE_FORMATS.items() if fmt in Image.SAVE}
        if not features.check("webp"):
            self.images.pop("webp", None)
        codecs = {
            codec for entry in list(VIDEO.values()) + list(AUDIO.values()) for codec in entry[1]
        }
        codecs.update(entry[2] for entry in VIDEO.values() if entry[2])
        codecs.update(
            codec for codec in self.encoders if codec.endswith(("_nvenc", "_qsv", "_amf"))
        )
        for codec in sorted(codecs & self.encoders):
            self.encoder_details(codec)

    def decoding_arguments(self, info):
        """Select CPU AV1 decoding explicitly; the native av1 decoder needs hardware."""
        args = []
        video_index = 0
        for stream in info.streams:
            if stream.get("codec_type") != "video":
                continue
            if stream.get("codec_name") == "av1":
                decoder = next(
                    (name for name in ("libdav1d", "libaom-av1") if name in self.decoders), None
                )
                if not decoder:
                    raise ValueError(
                        "This FFmpeg build lacks a software AV1 decoder. Update FileConverter "
                        "or use an FFmpeg build with libdav1d or libaom-av1."
                    )
                args += [f"-c:v:{video_index}", decoder]
            video_index += 1
        return args

    def codecs(self, fmt, category):
        policy = VIDEO if category == "video" else AUDIO
        if fmt not in policy:
            return []
        muxer, encoders = policy[fmt][:2]
        if not self.ffprobe or muxer not in self.muxers:
            return []
        return [codec for codec in encoders if codec in self.encoders]

    def audio_codecs(self, fmt):
        return [codec for codec in VIDEO_AUDIO.get(fmt, []) if codec in self.encoders]

    def encoder_details(self, codec):
        if codec not in self.encoder_properties:
            output = self.runner.run(
                [self.ffmpeg, "-hide_banner", "-h", "encoder=" + codec], timeout=30
            )
            details = {}
            for field, label in (
                ("pixel_formats", "Supported pixel formats"),
                ("sample_rates", "Supported sample rates"),
            ):
                match = re.search(label + r":\s*([^\n]+)", output)
                details[field] = match.group(1).split() if match else []
            self.encoder_properties[codec] = details
        return self.encoder_properties[codec]

    def outputs(self, category, source_format=""):
        if category in ("video", "audio"):
            result = [
                fmt
                for fmt in (VIDEO if category == "video" else AUDIO)
                if self.codecs(fmt, category)
                and (category != "video" or not VIDEO[fmt][2] or VIDEO[fmt][2] in self.encoders)
            ]
            if category == "video":
                result += [fmt for fmt in AUDIO if self.codecs(fmt, "audio")]
            return result
        if category == "image":
            return list(self.images) + ["pdf"]
        if category == "document":
            if source_format == "pdf":
                return ["png", "jpg", "txt", "pdf"]
            if source_format in ("txt", "md", "html"):
                result = ["txt", "html", "pdf"]
                if self.office:
                    result += ["docx", "odt", "rtf"]
                return result
            if source_format in ("csv", "xlsx"):
                return (
                    ["csv", "xlsx", "pdf"]
                    if self.office
                    else (["csv"] if source_format == "csv" else [])
                )
            return ["pdf", "docx", "odt", "rtf", "txt"] if self.office else []
        return []

    def diagnostics(self):
        return dict(
            versions=self.versions,
            ffmpeg=self.ffmpeg,
            ffprobe=self.ffprobe,
            office=self.office,
            images=self.images,
            hardware=self.hardware,
            hardware_encoders=sorted(
                c for c in self.encoders if any(s in c for s in ("nvenc", "qsv", "amf"))
            ),
            errors=self.errors,
        )
