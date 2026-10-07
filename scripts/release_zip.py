"""Create complete source ZIP and, only with actual Windows artifacts, a release ZIP."""

import argparse
import hashlib
import json
import zipfile

from build import ROOT, source_files
from fileconverter import __version__


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-only", action="store_true")
    args = parser.parse_args()
    artifacts = ROOT / "artifacts"
    artifacts.mkdir(exist_ok=True)
    source = artifacts / f"FileConverter-{__version__}-Source.zip"
    with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in source_files():
            archive.write(path, "FileConverter/" + path.relative_to(ROOT).as_posix())
    paths = [source]
    if not args.source_only:
        installer = artifacts / f"FileConverter-{__version__}-Windows-x64-Setup.exe"
        application = ROOT / "dist/FileConverter/FileConverter.exe"
        if not installer.is_file() or not application.is_file():
            parser.error(
                "Build the real Windows application and installer before creating a release ZIP"
            )
        bundle = artifacts / f"FileConverter-{__version__}-Windows-x64.zip"
        with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.write(installer, installer.name)
            archive.write(source, source.name)
            for path in (ROOT / "dist/FileConverter").rglob("*"):
                if path.is_file():
                    archive.write(
                        path,
                        "Application/" + path.relative_to(ROOT / "dist/FileConverter").as_posix(),
                    )
            for name in (
                "README.md",
                "LICENSE",
                "docs/BUILD.md",
                "docs/VERIFICATION.md",
                "docs/ATTRIBUTIONS.md",
            ):
                archive.write(ROOT / name, name)
            verification = artifacts / "windows-verification.json"
            if verification.is_file():
                archive.write(verification, "verification/windows-verification.json")
        paths.append(bundle)
    checksums = {}
    for path in paths:
        with zipfile.ZipFile(path) as archive:
            if archive.testzip() is not None:
                raise RuntimeError("ZIP integrity validation failed")
        with path.open("rb") as file:
            checksums[path.name] = hashlib.file_digest(file, "sha256").hexdigest()
        print(path)
    (artifacts / f"FileConverter-{__version__}-SHA256.json").write_text(
        json.dumps(checksums, indent=2) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
