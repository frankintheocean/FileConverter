"""Reuse our previously verified native FFmpeg build, pinned by complete release SHA256."""

import hashlib
import os
import shutil
import stat
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

from build import ROOT, verify_vendor

URL = "https://github.com/frankintheocean/FileConverter/releases/download/v1.0.1/FileConverter-1.0.1-Windows-x64.zip"
SHA256 = "8f00b14cb26952cb842e3514058333170cfa27c2d37d2a0e9dea2a5cbf2b3a78"


def main():
    if os.name != "nt":
        raise SystemExit("Vendor restoration is for the native Windows build")
    build = ROOT / "build"
    build.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="vendor-cache-", dir=build) as directory:
        folder = Path(directory)
        archive_path = folder / "verified-release.zip"
        try:
            with (
                urllib.request.urlopen(URL, timeout=120) as response,
                archive_path.open("wb") as output,
            ):
                shutil.copyfileobj(response, output, length=1024 * 1024)
        except (urllib.error.URLError, TimeoutError) as error:
            print(f"Previous verified build unavailable; compiling pinned source: {error}")
            output_file = os.environ.get("GITHUB_OUTPUT")
            if output_file:
                with open(output_file, "a", encoding="utf-8") as output:
                    output.write("restored=false\n")
            return
        with archive_path.open("rb") as file:
            if hashlib.file_digest(file, "sha256").hexdigest() != SHA256:
                raise ValueError("Prior release checksum mismatch; refusing binary reuse")
        vendor = folder / "vendor"
        total = 0
        with zipfile.ZipFile(archive_path) as archive:
            for entry in archive.infolist():
                if not entry.filename.startswith("Application/vendor/") or entry.is_dir():
                    continue
                relative = PurePosixPath(entry.filename.removeprefix("Application/vendor/"))
                if (
                    relative.is_absolute()
                    or ".." in relative.parts
                    or "\\" in str(relative)
                    or stat.S_ISLNK(entry.external_attr >> 16)
                ):
                    raise ValueError("Unsafe path in verified vendor archive")
                total += entry.file_size
                if total > 512 * 1024 * 1024:
                    raise ValueError("Vendor extraction limit exceeded")
                destination = vendor.joinpath(*relative.parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(entry) as source, destination.open("wb") as output:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
            previous = "FileConverter-1.0.1-Windows-x64-Setup.exe"
            with (
                archive.open(previous) as source,
                (build / "previous-Setup.exe").open("wb") as output,
            ):
                shutil.copyfileobj(source, output, length=1024 * 1024)
        verify_vendor(vendor)
        shutil.copytree(vendor, ROOT / "vendor/windows-x64", dirs_exist_ok=True)
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
            output.write("restored=true\n")
    print(
        "Restored source-built FFmpeg and prior installer after pinned release and per-file verification."
    )


if __name__ == "__main__":
    main()
