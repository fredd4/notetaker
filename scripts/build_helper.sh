#!/bin/bash
# Compiles the native Swift helper that captures system audio.
# Builds for both arm64 and x86_64 and merges them into a universal
# binary, so the packaged app works on both Apple Silicon and Intel Macs.
# Output: build/sysaudio (a single executable, no Xcode project needed).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SRC="$ROOT/native/sysaudio.swift"
OUT="$ROOT/build/sysaudio"

mkdir -p "$ROOT/build"

for ARCH in arm64 x86_64; do
    echo "Compiling for $ARCH..."
    swiftc -O \
        -target "$ARCH-apple-macosx13.0" \
        -framework ScreenCaptureKit \
        -framework AVFoundation \
        -framework CoreMedia \
        -o "$ROOT/build/sysaudio-$ARCH" \
        "$SRC"
done

echo "Merging into a universal binary -> $OUT"
lipo -create -output "$OUT" "$ROOT/build/sysaudio-arm64" "$ROOT/build/sysaudio-x86_64"
rm -f "$ROOT/build/sysaudio-arm64" "$ROOT/build/sysaudio-x86_64"

file "$OUT"
echo "Done: $OUT"
