# Verification record

Verification host: Linux x64, Python 3.12, 2026-10-07. These results establish
Linux execution of the application and engines; they do not establish a verified
Windows production release.

## Automated checks

- Ruff lint and formatting checks passed.
- Mypy passed for all 14 application modules using the Windows target configuration.
- Version 1.0.1 full suite: **104 passed in 66.54 seconds**, with no skips or failures.
- Real Qt navigation, source inspection, queue conversion,
  history and custom icon assets were exercised with the offscreen Qt platform.
- Real filesystem notifications, reconciliation, stable-file checks, actual MP3 bitrate
  matching, recursive discovery, restart state, priorities, duplicate/loop prevention,
  dry runs and malformed persistence were exercised. Saved job source-handling settings
  are tested against later watcher edits and corrupt snapshots.
- Failures cover corrupt/empty inputs, Unicode and special filenames, collisions,
  unavailable dependencies, missing sources, insufficient disk space, cancellation,
  interrupted state and hardware-to-software fallback.

## Actual conversion execution

Tests generate actual media and invoke the installed conversion backends. They do
not substitute fake converters or fake progress. Representative successful paths:

- MP4 → MOV, MOV → MP4, MKV → MP4, MP4 → WebM and GIF.
- 1080p → 720p and 4K → 1080p, audio extraction, frame extraction and subtitles.
- WAV → MP3 320 kbps, FLAC → MP3 and MP3 → WAV.
- PNG → JPG, JPG → WebP, AVIF and the supported image writer formats.
- TXT → DOCX → PDF with detected LibreOffice, image → PDF and PDF → images.
- Multiple image/PDF merge and streaming ZIP through the persisted job queue.

Two-pass video compression used a generated 104,676,445-byte noisy FFV1 source:

| Maximum (decimal bytes) | Validated output bytes |
| --- | ---: |
| 10,000,000 | 9,067,528 |
| 25,000,000 | 22,883,065 |

The outputs reopened and decoded successfully, and both remained under their
requested maximum. Separate tests validate audio under 45,000 bytes and image output
under 5,000 bytes. Infeasible targets fail without publishing oversized output.

## Dependencies and packaged startup

FFmpeg/ffprobe 7.1.5 were detected with encoders, decoders, muxers, demuxers, filters
and acceleration capabilities. Pillow writers include AVIF. LibreOfficeDev
26.8.0.0.alpha0 was available and actually converted documents. Availability of an
encoder in FFmpeg is distinct from working GPU hardware; no usable GPU was established.
The NVENC failure test verified a real software fallback.

The initial 1.0.0 development verification produced `dist/FileConverter/FileConverter` with the shared Qt/Python
runtime and image/PDF libraries. This Linux developer bundle uses system FFmpeg.
Its diagnostics, actual offscreen desktop startup, focus IPC and graceful maintenance
shutdown were exercised. Reproducible setup dependencies and diagnostics were also run.
The build includes dependency license notices and a complete application source archive.

## Icon and branding

The original source is `assets/app.svg`, with a 1024-pixel PNG and a transparent ICO
containing 16, 20, 24, 32, 40, 48, 64, 128 and 256-pixel images. Automated tests check
these assets. Real application screenshots are in `docs/screenshots/`.
Executable, installer, shortcuts, taskbar identity and tray configuration reference
the same artwork. Actual Windows shell rendering remains unverified.

## Windows release gates still outstanding

The native GitHub Windows runner produced and verified the 1.0.1 executable and installer.
The downloaded 201,323,295-byte Windows ZIP matched its published SHA256 and passed CRC checks.
Inno Setup source,
PE metadata, vendor integrity checks and `scripts/windows_verify.ps1` are supplied;
the first automated native install/repair/uninstall/reinstall and real conversion/size checks passed.
Version 1.0.4 adds upgrade, damaged-executable/metadata repair, native UI and actual PE icon checks.
Interactive items still require a Windows 10/11 x64 runner:

- Native executable/installer build and actual installed launch.
- Clean install, upgrade, interrupted/locked-file installation and same-version maintenance.
- Repair of executable, dependency, shortcuts, metadata and icon resources.
- Installed Apps uninstall, installer maintenance uninstall and reinstall, with settings
  and user-created outputs preserved.
- Native process JobObject behavior, taskbar/Start Menu/Desktop icon rendering,
  notifications, system tray, high-DPI/accessibility and removable/network-drive behavior.

The project does not include a publisher signing identity or automatic update service.
Public distribution also requires completing third-party corresponding-source and
license obligations described in `ATTRIBUTIONS.md` and `BUILD.md`. Optional LibreOffice
conversion availability and codec capabilities depend on the actual installation.
Markdown conversion preserves literal text rather than claiming rich document layout.


## Windows-only 1.0.1 packaging and destination update

Production build scripts now refuse Linux application builds. Non-Windows UI execution
is restricted to the explicit `FILECONVERTER_DEVELOPMENT=1` test mode. Source ZIP
packaging remains platform independent. Output mode tests cover a chosen folder,
source folders across a mixed-location batch, mode switching and missing destinations.

The native Windows tag workflow compiles FFmpeg/x264/LAME/zlib from pinned sources,
creates an actual installer and ZIP, runs installed repair/uninstall/reinstall and
real conversion/size checks, and publishes only after those succeed. Its generated
`verification/windows-verification.json` in a published Windows ZIP records the native
runtime results. This host's Linux verification does not stand in for that report.


## Native Windows release evidence

The first native Windows workflow completed successfully and published a ZIP containing
Setup, bundled application/engines, complete source and a runtime verification report.
Its actual conversions produced MOV (172,743 bytes), GIF (405,574 bytes), extracted PNG
(51,182 bytes), MP3 (82,590 bytes), and validated 10 MB/25 MB maximum outputs of
9,067,528 / 22,883,065 bytes. Installed desktop startup, focus and safe shutdown passed.

Final 1.0.4 native results are carried in `verification/windows-verification.json`,
`verification/windows-installer-verification.json` and `verification/windows-ui-tests.xml`
in the actual Windows ZIP. Release publication is blocked unless each native command
succeeds; a later packaging command cannot mask failed verification. The cached backend
is our previously source-built release, verified against a pinned whole-ZIP checksum
and all vendor file checksums before executing it; its complete engine source is retained.


## Final verified Windows 1.0.4 release

Native Windows run: https://github.com/frankintheocean/FileConverter/actions/runs/37579066122

All release gates executed successfully: executable/installer build, clean install,
upgrade from 1.0.1, repair of missing and corrupt executable files, damaged FFmpeg,
missing shortcuts and missing application metadata, uninstall, reinstall and retention
of actual user preferences, custom presets, a queued job, source files and outputs.
The installed desktop started, focused and shut down cleanly. All three native Qt UI
tests passed, including explicit chosen/source-folder destination behavior. The
executable's first icon group matched all nine original ICO images byte for byte.

Actual native conversions: MOV 172,743 bytes; GIF 405,574 bytes; extracted PNG 51,182
bytes; MP3 82,590 bytes. Video outputs were 9,067,528 bytes under 10,000,000 and
22,883,065 bytes under 25,000,000. Reports are included in the Windows ZIP.
The local complete suite passed 104 tests; lint, format and type checks passed.

The downloaded Windows ZIP contains the real Setup executable, application/runtime,
conversion engines, complete project source, notices and native QA reports. It is
201,338,978 bytes; SHA256:
`aa157c9ae6c06cea6e5b1a73ff9607e65b3f4ddbcafa5e7eee9b40b362f65932`.
Published checksum and all ZIP CRCs were verified after download.

Remaining manual checks concern visual taskbar/DPI/accessibility/notification behavior
and interrupted installation. The installer is unsigned. Optional LibreOffice and
additional codec/hardware availability remain installation dependent.

## Verified Windows 1.0.5 regression release

Native Windows run: https://github.com/frankintheocean/FileConverter/actions/runs/37595632496

Windows output finalization now uses atomic, non-overwriting rename rather than
requiring hard links. This fixes `WinError 1` on filesystems that reject hard links.
Regression coverage includes a real M4V-to-MP4 conversion, an unsupported-hard-link
fault, preservation of the original and protection of existing destinations.
The local complete suite passed 107 tests; formatting, lint and type checks passed.
The native Windows suite passed three UI tests and four conversion/collision tests.

Native installer gates passed install, upgrade, repair of missing/corrupt application
files and damaged FFmpeg, shortcut/metadata repair, uninstall, reinstall and preservation
of user data and converted outputs. The actual executable icon matched all nine custom
ICO images. FFmpeg/ffprobe 7.1.5 and image backends were detected without errors;
the bundled software build correctly reported no hardware encoders.

Actual native M4V-to-MP4 output: 172,796 bytes, reopened and validated. MOV, GIF,
extracted PNG and MP3 conversions also passed. Maximum-size outputs remained
9,067,528 bytes under 10,000,000 and 22,883,065 under 25,000,000.

The published Windows ZIP includes installer, application, engines, full source,
licenses and all four native verification reports. Downloaded size: 201,328,075 bytes.
SHA256: `6e0ede54fc806e43ed7d5eb0408b2074aee065ed864fab55536e1a9d2197a6c6`.
The published checksum and every ZIP CRC were verified after download.
Physical FAT/exFAT/network-drive testing and the manual visual checks listed above
were not performed; unsupported-hard-link behavior was exercised through fault injection.

## Verified Windows 1.0.7 AV1 release

Native Windows run: https://github.com/frankintheocean/FileConverter/actions/runs/37614867567

FFmpeg/ffprobe 7.1.5 now include source-built dav1d 1.5.1, pinned to commit
`42b2b24fb8819f1ed3643aa9cf2a62f03868e3aa`. Conversion, preview and validation
explicitly select software AV1 decoding. Missing software decoders produce an
actionable planning error. Remux compatibility checks container support without
requiring an encoder for the copied codec.

The local complete suite passed 109 tests, plus lint, format and type checks.
The native Windows suite passed three UI tests and six conversion regressions,
with zero skips or failures. AV1-to-H.264 MP4 conversion, preview and validated AV1
remux passed with no AV1 encoder installed. A separate local AV1-in-M4V-to-MP4
check also passed using dav1d. The test media is original generated content.

Actual native AV1-to-MP4 output: 3,944 bytes, reopened and fully decoded. M4V-to-MP4,
MOV, GIF, frame extraction and MP3 conversion also passed. Maximum-size video outputs
were 9,067,528 bytes under 10,000,000 and 22,883,065 under 25,000,000.
Native clean install, upgrade, missing/corrupt-file and dependency repair,
shortcut/metadata repair, uninstall and reinstall passed. Settings, presets,
queued work, source files and outputs were preserved. All nine original executable
icon images matched. FFmpeg/ffprobe and image dependencies were detected without
errors; the software build correctly reported no hardware encoders.

The Windows ZIP includes Setup, runtime, complete application and engine sources,
dav1d's BSD license, other dependency notices and all native QA reports.
Downloaded size: 206,424,246 bytes. SHA256:
`ee7bf571c506bcb8dd0e487a1d882f4f3b400ef938a9e9d7e9e7a3cf623363d1`.
The published checksum and every ZIP CRC were verified after download.
The unsigned-installer and manual visual/platform limitations listed above remain.
