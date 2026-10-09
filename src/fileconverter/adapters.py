from __future__ import annotations

import html
import io
import threading
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pymupdf
from PIL import Image, ImageOps, PngImagePlugin

from .capabilities import IMAGE_FORMATS
from .process import Cancelled


def check_cancel(cancel):
    if cancel.is_set():
        raise Cancelled("Cancelled")


class ImageAdapter:
    def convert(self, info, options, plan, output, cancel, progress):
        check_cancel(cancel)
        with Image.open(info.path) as original:
            original.seek(0)
            image = ImageOps.exif_transpose(original)
            if options.crop:
                width, height, x, y = map(int, options.crop.split(":"))
                image = image.crop((x, y, x + width, y + height))
            if options.rotation:
                image = image.rotate(-options.rotation, expand=True)
            if image.size != (plan["width"], plan["height"]):
                image = image.resize((plan["width"], plan["height"]), Image.Resampling.LANCZOS)
            fmt = IMAGE_FORMATS[options.format]
            if fmt in ("JPEG", "BMP") or image.mode not in ("RGB", "RGBA", "L", "P"):
                background = Image.new("RGB", image.size, options.background)
                if "A" in image.getbands():
                    background.paste(image, mask=image.getchannel("A"))
                else:
                    background.paste(image.convert("RGB"))
                image = background
            metadata: dict = {}
            if options.metadata != "strip":
                exif = original.getexif()
                exif.pop(274, None)
                if options.metadata == "minimal":
                    for key in list(exif):
                        if key not in (270, 315, 33432):
                            del exif[key]
                if exif and fmt in ("JPEG", "WEBP", "PNG", "TIFF", "AVIF"):
                    metadata["exif"] = exif.tobytes()
                if original.info.get("icc_profile"):
                    metadata["icc_profile"] = original.info["icc_profile"]
                if fmt == "PNG":
                    text_info = PngImagePlugin.PngInfo()
                    for text_key, value in original.info.items():
                        if isinstance(value, str) and text_key not in (
                            "exif",
                            "icc_profile",
                            "xmp",
                        ):
                            if options.metadata == "preserve" or str(text_key).lower() in (
                                "title",
                                "author",
                                "description",
                                "copyright",
                            ):
                                text_info.add_text(str(text_key), value)
                    metadata["pnginfo"] = text_info
                if (
                    options.metadata == "preserve"
                    and fmt in ("JPEG", "WEBP")
                    and original.info.get("xmp")
                ):
                    metadata["xmp"] = original.info["xmp"]
                if (
                    options.metadata == "preserve"
                    and fmt == "JPEG"
                    and original.info.get("comment")
                ):
                    metadata["comment"] = original.info["comment"]
            if options.metadata != "preserve":
                if image.mode == "P" and "transparency" in image.info:
                    image = image.convert("RGBA")
                image.info.clear()
            # Animated inputs are not silently flattened.
            if getattr(original, "n_frames", 1) > 1:
                raise ValueError(
                    "Animated images require the FFmpeg video workflow; static-image conversion would discard frames"
                )
            target = plan["target_bytes"]
            low, high = 1, options.image_quality
            best = None
            attempts = 1 if not target else 8
            for index in range(attempts):
                check_cancel(cancel)
                quality = high if not target else (low + high) // 2
                kwargs = dict(metadata)
                if fmt in ("JPEG", "WEBP", "AVIF"):
                    kwargs["quality"] = quality
                if fmt == "JPEG":
                    kwargs["optimize"] = True
                if fmt in ("WEBP", "AVIF"):
                    kwargs["lossless"] = options.lossless
                if fmt == "PNG":
                    kwargs["optimize"] = True
                if fmt == "ICO":
                    side = min(256, max(image.size))
                    image = ImageOps.contain(image.convert("RGBA"), (side, side))
                    kwargs["sizes"] = [
                        (n, n) for n in (16, 20, 24, 32, 40, 48, 64, 128, 256) if n <= side
                    ]
                # Encode to disk; no unbounded encoded-file buffer.
                image.save(output, format=fmt, **kwargs)
                size = Path(output).stat().st_size
                progress((index + 1) / attempts * 0.95, {"quality": str(quality)})
                if not target:
                    break
                if size <= target:
                    best = quality
                    low = quality + 1
                else:
                    high = quality - 1
                if low > high:
                    break
            if target:
                if best is None:
                    raise ValueError(
                        "This size target cannot be reached at the selected dimensions. Resize or increase the maximum."
                    )
                kwargs["quality"] = best
                image.save(output, format=fmt, **kwargs)
            check_cancel(cancel)


# MuPDF has process-global state; serialize its calls independently of the media queue.
PDF_LOCK = threading.RLock()


class DocumentAdapter:
    def __init__(self, cap, runner):
        self.cap = cap
        self.runner = runner

    def strip_office_metadata(self, output, cancel, mode="strip"):
        output = Path(output)
        if output.suffix in (".docx", ".xlsx", ".odt"):
            sanitized = output.with_name("metadata-clean.zip")
            metadata_path = "meta.xml" if output.suffix == ".odt" else "docProps/core.xml"
            with zipfile.ZipFile(output) as source, zipfile.ZipFile(sanitized, "w") as target:
                for entry in source.infolist():
                    check_cancel(cancel)
                    if entry.filename == metadata_path:
                        if entry.file_size > 2_000_000:
                            raise ValueError("Office metadata exceeds safe limits")
                        data = source.read(entry)
                        if b"<!DOCTYPE" in data:
                            raise ValueError("Office metadata contains an unsafe DTD")
                        root = ET.fromstring(data)
                        if output.suffix == ".odt":
                            for field in root:
                                if field.tag.endswith("}meta"):
                                    for child in list(field):
                                        if mode == "strip" or child.tag.rsplit("}", 1)[-1] not in (
                                            "title",
                                            "subject",
                                            "description",
                                            "creator",
                                            "initial-creator",
                                            "keyword",
                                        ):
                                            field.remove(child)
                        else:
                            for field in list(root):
                                if mode == "strip" or field.tag.rsplit("}", 1)[-1] not in (
                                    "title",
                                    "subject",
                                    "description",
                                    "creator",
                                    "lastModifiedBy",
                                    "keywords",
                                ):
                                    root.remove(field)
                        target.writestr(
                            entry, ET.tostring(root, encoding="utf-8", xml_declaration=True)
                        )
                    else:
                        with source.open(entry) as reader, target.open(entry, "w") as writer:
                            while chunk := reader.read(1024 * 1024):
                                check_cancel(cancel)
                                writer.write(chunk)
            sanitized.replace(output)
        elif output.suffix == ".rtf":
            if output.stat().st_size > 20_000_000:
                raise ValueError("RTF metadata stripping supports files up to 20 MB")
            data = output.read_bytes()
            start = data.find(b"{\\info")
            if start >= 0:
                level, escaped, end = 0, False, start
                for end in range(start, len(data)):
                    byte = data[end]
                    if escaped:
                        escaped = False
                        continue
                    if byte == 92:
                        escaped = True
                    elif byte == 123:
                        level += 1
                    elif byte == 125:
                        level -= 1
                        if level == 0:
                            break
                if level:
                    raise ValueError("Malformed RTF metadata group")
                output.write_bytes(data[:start] + data[end + 1 :])

    def convert(self, info, options, plan, output, work, cancel, progress):
        check_cancel(cancel)
        if info.category == "image":
            with PDF_LOCK, Image.open(info.path) as original, pymupdf.open() as doc:
                image = ImageOps.exif_transpose(original).convert("RGB")
                payload = io.BytesIO()
                image.save(payload, format="PNG")
                page = doc.new_page(width=image.width, height=image.height)
                page.insert_image(page.rect, stream=payload.getvalue())
                doc.save(output, deflate=True)
        elif info.format == "pdf":
            with PDF_LOCK, pymupdf.open(info.path) as doc:
                if options.format == "pdf":
                    if options.metadata != "preserve":
                        retained = (
                            {
                                key: value
                                for key, value in doc.metadata.items()
                                if key in ("title", "author", "subject", "keywords")
                            }
                            if options.metadata == "minimal"
                            else {}
                        )
                        doc.set_metadata(retained)
                        doc.del_xml_metadata()
                    doc.save(output, garbage=4, deflate=True, clean=True)
                elif options.format == "txt":
                    with Path(output).open("w", encoding="utf-8") as target:
                        for index, page in enumerate(doc):
                            check_cancel(cancel)
                            target.write(
                                page.get_text(
                                    flags=pymupdf.TEXTFLAGS_TEXT & ~pymupdf.TEXT_PRESERVE_LIGATURES
                                )
                            )
                            progress((index + 1) / len(doc) * 0.95, {})
                else:
                    folder = Path(output)
                    folder.mkdir()
                    for index, page in enumerate(doc):
                        check_cancel(cancel)
                        page.get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False).save(
                            folder / f"page-{index + 1:05d}.{options.format}"
                        )
                        progress((index + 1) / len(doc) * 0.95, {})
        elif info.format in ("txt", "md", "html") and options.format in ("txt", "html", "pdf"):
            source = Path(info.path)
            if source.stat().st_size > 20_000_000:
                raise ValueError("Text layout supports documents up to 20 MB")
            text = source.read_text(encoding="utf-8-sig")
            if info.format == "html":
                from html.parser import HTMLParser

                class TextParser(HTMLParser):
                    def __init__(self):
                        super().__init__()
                        self.parts = []
                        self.hidden = 0

                    def handle_starttag(self, tag, attrs):
                        if tag in ("script", "style"):
                            self.hidden += 1
                        if tag in ("p", "div", "br", "li", "h1", "h2", "tr"):
                            self.parts.append("\n")

                    def handle_endtag(self, tag):
                        if tag in ("script", "style"):
                            self.hidden = max(0, self.hidden - 1)

                    def handle_data(self, data):
                        if not self.hidden:
                            self.parts.append(data)

                parser = TextParser()
                parser.feed(text)
                text = "".join(parser.parts)
            if options.format == "txt":
                Path(output).write_text(text, encoding="utf-8")
            elif options.format == "html":
                # Escaped content cannot execute scripts or load external resources.
                Path(output).write_text(
                    "<!doctype html><meta charset='utf-8'><title>Converted document</title><pre>"
                    + html.escape(text)
                    + "</pre>",
                    encoding="utf-8",
                )
            else:
                with PDF_LOCK, pymupdf.open() as doc:
                    story = pymupdf.Story(
                        "<pre style='font-family:sans-serif;white-space:pre-wrap'>"
                        + html.escape(text)
                        + "</pre>"
                    )
                    writer = pymupdf.DocumentWriter(str(output))
                    try:
                        more = True
                        while more:
                            check_cancel(cancel)
                            rect = pymupdf.Rect(0, 0, 595, 842)
                            device = writer.begin_page(rect)
                            more, _ = story.place(rect + (40, 40, -40, -40))
                            story.draw(device)
                            writer.end_page()
                    finally:
                        writer.close()
        elif info.format == "csv" and options.format == "csv":
            import shutil

            shutil.copyfile(info.path, output)
        else:
            if not self.cap.office:
                raise ValueError(
                    "LibreOffice is required. Install LibreOffice and refresh Dependencies."
                )
            profile_path = work / "office-profile"
            (profile_path / "user").mkdir(parents=True)
            (profile_path / "user/registrymodifications.xcu").write_text(
                '<?xml version="1.0"?><oor:items xmlns:oor="http://openoffice.org/2001/registry">'
                '<item oor:path="/org.openoffice.Office.Common/Security/Scripting">'
                '<prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop></item>'
                '<item oor:path="/org.openoffice.Office.Common/Load">'
                '<prop oor:name="UpdateLinks" oor:op="fuse"><value>0</value></prop></item>'
                "</oor:items>",
                encoding="utf-8",
            )
            profile = profile_path.as_uri()
            outdir = work / "office-output"
            outdir.mkdir()
            input_path = Path(info.path)
            if info.format in ("txt", "md", "html"):
                # Copy into a safe local text file; never pass active HTML to office.
                input_path = work / "source.txt"
                input_path.write_text(
                    Path(info.path).read_text(encoding="utf-8-sig"), encoding="utf-8"
                )
            self.runner.run(
                [
                    self.cap.office,
                    f"-env:UserInstallation={profile}",
                    "--headless",
                    "--nologo",
                    "--nodefault",
                    "--nolockcheck",
                    "--convert-to",
                    options.format,
                    "--outdir",
                    str(outdir),
                    str(input_path),
                ],
                cancel=cancel,
                timeout=300,
            )
            expected = outdir / (input_path.stem + "." + options.format)
            if not expected.is_file():
                raise ValueError(
                    "LibreOffice did not produce the requested output. The input or conversion pair may be unsupported."
                )
            expected.replace(output)
            if options.metadata != "preserve":
                self.strip_office_metadata(output, cancel, options.metadata)
            if options.metadata != "preserve" and options.format == "pdf":
                with PDF_LOCK, pymupdf.open(output) as doc:
                    retained = (
                        {
                            key: value
                            for key, value in doc.metadata.items()
                            if key in ("title", "author", "subject", "keywords")
                        }
                        if options.metadata == "minimal"
                        else {}
                    )
                    doc.set_metadata(retained)
                    doc.del_xml_metadata()
                    doc.saveIncr()
        progress(0.95, {})
        check_cancel(cancel)

    def merge(self, sources, output, cancel):
        with PDF_LOCK, pymupdf.open() as doc:
            for source in sources:
                check_cancel(cancel)
                try:
                    with pymupdf.open(source) as part:
                        if part.is_pdf:
                            if part.is_encrypted:
                                raise ValueError("Cannot merge an encrypted PDF")
                            doc.insert_pdf(part)
                            continue
                except pymupdf.FileDataError:
                    pass
                with Image.open(source) as original:
                    image = ImageOps.exif_transpose(original).convert("RGB")
                    buffer = io.BytesIO()
                    image.save(buffer, "PNG")
                    page = doc.new_page(width=image.width, height=image.height)
                    page.insert_image(page.rect, stream=buffer.getvalue())
            if not len(doc):
                raise ValueError("No pages to merge")
            doc.save(output, deflate=True)
