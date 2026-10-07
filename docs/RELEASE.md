Windows 10/11 x64 local file converter and compressor.

Version 1.0.6 bundles dav1d 1.5.1 for software AV1 decoding and explicitly selects
it for conversion, preview and output validation. AV1 inputs no longer require an
AV1-capable GPU. Real AV1-to-H.264 MP4 and AV1 remux regression tests are included.
The Windows output-finalization fix and M4V regression coverage are retained.

Download the Windows-x64 ZIP, extract it, and run the included Setup executable.
The ZIP includes the installer, application runtime, FFmpeg conversion engines,
complete project source, documentation and dependency notices. End users do not
need Python, Node.js or a compiler.

Run the same installer again for Repair / Uninstall / Cancel. Repair restores shipped
files and shortcuts while preserving settings, history and outputs. Installed Apps
also supports normal uninstall. Original converted files are always preserved.

Output location explicitly supports each file's own source folder or any chosen
folder. Originals remain protected by suffixes, validation and collision handling.

The release workflow publishes only after native Windows packaging, install,
repair, uninstall, reinstall, real conversions and target-size tests pass. The
bundle includes the corresponding sources for its pinned software H.264/MP3/AAC
FFmpeg build. Optional codec availability is detected rather than assumed.

This build is unsigned; no publisher certificate is configured. Native visual shell,
accessibility/DPI, upgrade and interrupted-install checks remain interactive release
checks. LibreOffice is an optional separately installed document backend. See the
bundled README and BUILD documentation for capabilities and limitations.
