#!/bin/bash
# Build a pinned bubblewrap (>= 0.11, needed for --overlay) into
# ~/.local/share/dryrun/bwrap/<version>/bwrap and print its sha256.
# Needs: gcc, pkg-config, meson + ninja (pip), curl.
set -euo pipefail
VER="${BWRAP_VERSION:-0.11.0}"
PREFIX="$HOME/.local/share/dryrun/bwrap/$VER"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
URL="https://github.com/containers/bubblewrap/releases/download/v$VER/bubblewrap-$VER.tar.xz"
curl -fsSL "$URL" -o "$WORK/bwrap.tar.xz"
echo "tarball sha256: $(sha256sum "$WORK/bwrap.tar.xz" | cut -d' ' -f1)"
tar -C "$WORK" -xf "$WORK/bwrap.tar.xz"
cd "$WORK/bubblewrap-$VER"
meson setup _build --buildtype=release -Dselinux=disabled -Dman=disabled -Dtests=false \
  -Dbash_completion=disabled -Dzsh_completion=disabled 2>&1 | tail -25
ninja -C _build 2>&1 | tail -5
mkdir -p "$PREFIX"
install -m 0755 _build/bwrap "$PREFIX/bwrap"
"$PREFIX/bwrap" --version
echo "binary: $PREFIX/bwrap"
sha256sum "$PREFIX/bwrap" | cut -d' ' -f1 > "$PREFIX/bwrap.sha256"
echo "binary sha256: $(cat "$PREFIX/bwrap.sha256") (recorded in $PREFIX/bwrap.sha256)"
