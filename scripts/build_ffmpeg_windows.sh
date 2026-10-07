#!/usr/bin/env bash
# Native MSYS2/MinGW64 build from pinned upstream sources, with corresponding source.
set -euo pipefail
cd "$(dirname "$0")/.."
project_root="$PWD"
work="$project_root/build/ffmpeg-source"
prefix="$project_root/build/ffmpeg-prefix"
vendor="$project_root/vendor/windows-x64"
mkdir -p "$work" "$prefix" "$vendor"
fetch_source() {
  local name="$1" url="$2" commit="$3"
  if [ ! -d "$work/$name/.git" ]; then git -c core.autocrlf=false clone "$url" "$work/$name"; fi
  git -C "$work/$name" checkout --detach "$commit"
  test "$(git -C "$work/$name" rev-parse HEAD)" = "$commit"
}
fetch_source ffmpeg https://github.com/FFmpeg/FFmpeg.git 3a0867c2bfda4a4d4309ca1a8cbdc6175e67f587
fetch_source x264 https://github.com/mirror/x264.git c24e06c2e184345ceb33eb20a15d1024d9fd3497
fetch_source lame https://github.com/rbrito/lame.git af984672a95bb0eedc9f3193604e269c97cca162
fetch_source zlib https://github.com/madler/zlib.git da607da739fa6047df13e66a2af6b8bec7c2a498
export PKG_CONFIG_PATH="$prefix/lib/pkgconfig"
workers="$(nproc)"
cd "$work/zlib"
./configure --static --prefix="$prefix"
make -j"$workers"
make install
cd "$work/x264"
./configure --host=x86_64-w64-mingw32 --prefix="$prefix" --enable-static --disable-cli --disable-opencl
make -j"$workers"
make install
cd "$work/lame"
./configure --host=x86_64-w64-mingw32 --prefix="$prefix" --enable-static --disable-shared --disable-frontend --disable-decoder
make -j"$workers"
make install
cd "$work/ffmpeg"
./configure --prefix="$prefix" --target-os=mingw32 --arch=x86_64 --disable-autodetect --disable-debug --disable-doc --disable-ffplay --disable-network --enable-gpl --enable-version3 --enable-libx264 --enable-libmp3lame --enable-zlib --pkg-config-flags=--static --enable-static --disable-shared --extra-cflags="-I$prefix/include" --extra-ldflags="-L$prefix/lib -static"
make -j"$workers"
cp ffmpeg.exe ffprobe.exe "$vendor/"
cp COPYING.GPLv3 "$vendor/LICENSE"
cp "$work/x264/COPYING" "$vendor/x264-COPYING"
cp "$work/lame/COPYING" "$vendor/LAME-COPYING"
cp "$work/zlib/LICENSE" "$vendor/zlib-LICENSE"
if [ -d /mingw64/share/licenses ]; then cp -rL /mingw64/share/licenses "$vendor/toolchain-licenses"; fi
# git archive retains pristine complete sources instead of generated objects/caches.
source_dir="$project_root/build/ffmpeg-corresponding-source"
mkdir -p "$source_dir"
for component in ffmpeg x264 lame zlib; do
  git -C "$work/$component" archive --format=tar --prefix="$component/" HEAD | tar -xf - -C "$source_dir"
done
cp "$project_root/scripts/build_ffmpeg_windows.sh" "$source_dir/BUILD.sh"
tar -cJf "$vendor/SOURCE.tar.xz" -C "$source_dir" .
{
  echo 'Native Windows x64 FFmpeg 7.1.5 with static x264 and LAME.'
  echo 'Complete source and exact build recipe are in SOURCE.tar.xz/BUILD.sh.'
  echo 'Build with MSYS2 MinGW64 GCC, make, pkg-config, nasm and diffutils.'
  echo 'Upstream: https://github.com/FFmpeg/FFmpeg, https://github.com/mirror/x264, https://github.com/rbrito/lame'
  "$vendor/ffmpeg.exe" -version
} > "$vendor/BUILD.txt"
cd "$project_root"
echo 'Native FFmpeg vendor bundle built from pinned sources.'
