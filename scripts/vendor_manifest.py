"""Record checksums after building trusted FFmpeg sources; never download executables."""

import hashlib
import json
import sys
from pathlib import Path

folder = Path(sys.argv[1]).resolve()
digests = {}
for path in sorted(folder.rglob("*")):
    if path.is_file() and path.name != "manifest.json":
        with path.open("rb") as file:
            digests[path.relative_to(folder).as_posix()] = hashlib.file_digest(
                file, "sha256"
            ).hexdigest()
(folder / "manifest.json").write_text(
    json.dumps({"origin": "https://github.com/FFmpeg/FFmpeg", "sha256": digests}, indent=2),
    encoding="utf-8",
)
