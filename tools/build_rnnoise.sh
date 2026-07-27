#!/usr/bin/env bash
# Build an optimized librnnoise for this machine and install it where
# audio_util._load_rnnoise() prefers it (~/.local/lib/radio-gateway/).
#
# Why: the pyrnnoise wheel ships a mostly-scalar librnnoise that measured
# ~0.9 ms/frame on the gateway (Haswell i5); a -O3 -march=native build of
# xiph/rnnoise runs the same API at ~0.22 ms/frame (4x faster, measured
# 2026-07-27). The result is machine-specific, so it is NOT committed to
# the repo — run this script per machine. Without it the gateway falls
# back to the wheel's bundled lib automatically.
#
# Requires: git, gcc, autotools (autoconf automake libtool), make.
set -euo pipefail

WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT
DEST="$HOME/.local/lib/radio-gateway"

echo "== Cloning xiph/rnnoise =="
git clone --depth 1 https://github.com/xiph/rnnoise.git "$WORK/rnnoise"
cd "$WORK/rnnoise"

echo "== Building (-O3 -march=native) =="
./autogen.sh            # also downloads the model weights
./configure CFLAGS="-O3 -march=native" --disable-examples --disable-doc
make -j"$(nproc)"

echo "== Installing to $DEST =="
mkdir -p "$DEST"
cp .libs/librnnoise.so.* "$DEST/librnnoise.so.tmp"
mv "$DEST/librnnoise.so.tmp" "$DEST/librnnoise.so"

echo "== Verifying =="
python3 - <<'EOF'
import ctypes, os
lib = ctypes.CDLL(os.path.expanduser('~/.local/lib/radio-gateway/librnnoise.so'))
lib.rnnoise_get_frame_size.restype = ctypes.c_int
fs = lib.rnnoise_get_frame_size()
assert fs == 480, f"unexpected frame size {fs}"
print(f"OK: librnnoise loads, frame size {fs}")
EOF
echo "Done. Restart the gateway to pick it up."
