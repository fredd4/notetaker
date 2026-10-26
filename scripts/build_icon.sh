#!/bin/bash
# Renders the app icon and packs it into an .icns for py2app.
# Output: build/Notetaker.icns
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SET="$ROOT/build/Notetaker.iconset"
MASTER="$ROOT/build/icon-1024.png"

mkdir -p "$SET"
swift "$ROOT/scripts/make_icon.swift" "$MASTER"

for S in 16 32 128 256 512; do
    sips -z "$S" "$S" "$MASTER" --out "$SET/icon_${S}x${S}.png" >/dev/null
    D=$((S * 2))
    sips -z "$D" "$D" "$MASTER" --out "$SET/icon_${S}x${S}@2x.png" >/dev/null
done

iconutil -c icns -o "$ROOT/build/Notetaker.icns" "$SET"
rm -rf "$SET"
echo "Done: $ROOT/build/Notetaker.icns"
