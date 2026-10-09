# AV1 regression fixture

`av1.mkv` is original generated test-pattern media, distributed under the project's
GPL-3.0 license. It contains three real 64×64 AV1 frames and needs no hardware decoder.
Reproduce using FFmpeg with a libaom encoder:

```sh
ffmpeg -v error -f lavfi -i testsrc2=size=64x64:rate=5 -t 0.6 -c:v libaom-av1 -cpu-used 8 -crf 35 -an av1.mkv
```

The fixture lets Windows verify actual AV1 decoding without bundling an AV1 encoder.
