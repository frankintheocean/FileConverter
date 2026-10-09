from __future__ import annotations

import math
from pathlib import Path

import pymupdf
from PIL import Image

from .capabilities import DOCUMENTS, LOSSLESS
from .models import MediaInfo
from .process import ProcessFailure


def number(value, default=0):
    try:
        result = float(value)
        return result if math.isfinite(result) else default
    except (ValueError, TypeError):
        return default


def ratio(value):
    try:
        a, b = value.split("/")
        return number(a) / number(b) if number(b) else 0
    except (ValueError, AttributeError):
        return number(value)


class Detector:
    def __init__(self, capabilities, runner):
        self.cap = capabilities
        self.runner = runner

    def inspect(self, path, cancel=None):
        path = Path(path).resolve(strict=True)
        if not path.is_file():
            raise ValueError("The source is not a regular file")
        size = path.stat().st_size
        if not size:
            raise ValueError("The source file is empty")
        ext = path.suffix.lower().lstrip(".")
        try:
            with Image.open(path) as image:
                fmt = (image.format or "unknown").lower()
                if fmt not in {"png", "jpeg", "webp", "gif", "bmp", "tiff", "ico", "avif"}:
                    raise ValueError("Unsupported image reader")
                width, height = image.size
                metadata: dict = {
                    key: str(value)[:500]
                    for key, value in image.info.items()
                    if key not in ("icc_profile", "exif")
                }
                exif_count = len(image.getexif())
                if exif_count:
                    metadata["EXIF fields"] = exif_count
                animated = getattr(image, "n_frames", 1) > 1
                # EXIF access can load an image; verification requires a fresh reader.
                with Image.open(path) as verifier:
                    verifier.verify()
                if animated:
                    raise ValueError("Use FFmpeg for animated images")
                info = MediaInfo(
                    str(path), "image", fmt, size, width=width, height=height, metadata=metadata
                )
                if ext != fmt and {ext, fmt} != {"jpg", "jpeg"}:
                    info.warnings.append(
                        "Filename extension differs from the detected image format"
                    )
                return info
        except (OSError, ValueError):
            pass
        with path.open("rb") as source:
            signature = source.read(8)
        if signature.startswith(b"%PDF"):
            with pymupdf.open(path) as document:
                if document.is_encrypted:
                    raise ValueError("Encrypted PDFs must be unlocked before conversion")
                if not document.page_count:
                    raise ValueError("PDF has no readable pages")
                return MediaInfo(
                    str(path),
                    "document",
                    "pdf",
                    size,
                    metadata=dict(document.metadata or {}, pages=document.page_count),
                )
        if ext in DOCUMENTS - {"pdf"}:
            document_metadata = {}
            if ext in {"docx", "xlsx", "odt"}:
                import xml.etree.ElementTree as ET
                import zipfile

                with zipfile.ZipFile(path) as archive:
                    names = set(archive.namelist())
                    expected = {
                        "docx": "word/document.xml",
                        "xlsx": "xl/workbook.xml",
                        "odt": "content.xml",
                    }[ext]
                    if (
                        len(names) > 100000
                        or sum(i.file_size for i in archive.infolist()) > 2_000_000_000
                    ):
                        raise ValueError("Document archive exceeds safe inspection limits")
                    if expected not in names:
                        raise ValueError("Document container is corrupt or has the wrong extension")
                    metadata_path = "meta.xml" if ext == "odt" else "docProps/core.xml"
                    if metadata_path in names:
                        if archive.getinfo(metadata_path).file_size > 2_000_000:
                            raise ValueError("Document metadata is excessively large")
                        data = archive.read(metadata_path)
                        if b"<!DOCTYPE" in data:
                            raise ValueError("Document metadata contains an unsafe DTD")
                        root = ET.fromstring(data)
                        for field in root.iter():
                            name = field.tag.rsplit("}", 1)[-1]
                            if name in ("creator", "initial-creator", "lastModifiedBy"):
                                document_metadata["author"] = (field.text or "")[:1000]
                            elif name in (
                                "title",
                                "description",
                                "created",
                                "modified",
                                "creation-date",
                                "date",
                            ):
                                document_metadata[name] = (field.text or "")[:1000]
            elif ext == "rtf" and not signature.startswith(b"{\\rtf"):
                raise ValueError("The input is not an RTF document")
            return MediaInfo(str(path), "document", ext, size, metadata=document_metadata)
        if not self.cap.ffprobe:
            raise ValueError("FFmpeg/ffprobe is missing. Install it or repair the application.")
        try:
            payload = self.runner.json(
                [
                    self.cap.ffprobe,
                    "-v",
                    "error",
                    "-protocol_whitelist",
                    "file,pipe,crypto,data",
                    "-show_format",
                    "-show_streams",
                    "-of",
                    "json",
                    str(path),
                ],
                cancel,
            )
        except ProcessFailure as error:
            raise ProcessFailure(
                "Cannot read this file: unsupported format or corrupt input.", error.diagnostic
            ) from error
        streams = payload.get("streams", [])
        videos = [
            s
            for s in streams
            if s.get("codec_type") == "video" and not s.get("disposition", {}).get("attached_pic")
        ]
        audios = [s for s in streams if s.get("codec_type") == "audio"]
        if not videos and not audios:
            raise ValueError("No supported video or audio stream was detected")
        video = videos[0] if videos else {}
        audio = audios[0] if audios else {}
        fmt = payload.get("format", {})
        duration = number(fmt.get("duration")) or max(
            (number(s.get("duration")) for s in streams), default=0
        )
        bitrate = int(number(fmt.get("bit_rate")))
        audio_rate = int(number(audio.get("bit_rate")))
        if not videos and not audio_rate and duration:
            audio_rate = int(size * 8 / duration)
        info = MediaInfo(
            str(path),
            "video" if videos else "audio",
            fmt.get("format_name", "unknown"),
            size,
            duration=duration,
            width=int(video.get("width", 0)),
            height=int(video.get("height", 0)),
            fps=ratio(video.get("avg_frame_rate", "0/1")),
            bitrate=bitrate,
            audio_bitrate=audio_rate,
            channels=int(audio.get("channels", 0)),
            sample_rate=int(number(audio.get("sample_rate"))),
            video_codec=video.get("codec_name", ""),
            audio_codec=audio.get("codec_name", ""),
            lossless=audio.get("codec_name", "") in LOSSLESS,
            metadata=fmt.get("tags", {}),
            streams=streams,
        )
        if (
            video.get("codec_name") not in self.cap.decoders
            or audio.get("codec_name") not in self.cap.decoders
        ):
            for stream in videos + audios:
                if stream.get("codec_name") not in self.cap.decoders:
                    info.warnings.append(f"Decoder unavailable: {stream.get('codec_name')}")
        known = set(info.format.split(","))
        aliases = {
            "m4a": "mov",
            "mp4": "mov",
            "mkv": "matroska",
            "ts": "mpegts",
            "mts": "mpegts",
            "m2ts": "mpegts",
            "wma": "asf",
            "oga": "ogg",
            "opus": "ogg",
            "m4v": "mov",
            "aif": "aiff",
        }
        if aliases.get(ext, ext) not in known:
            info.warnings.append("Filename extension differs from the detected container")
        return info
