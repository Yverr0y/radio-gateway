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
# Requires: git, gcc, autotools (autoconf automake libtool), make, and wget —
# autogen.sh shells out to download_model.sh for the model weights and that
# script is hardcoded to wget, so curl alone is not enough. scripts/install.sh
# installs all of these before calling this script.
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
# Resolve the real shared object rather than globbing librnnoise.so.*: libtool
# leaves BOTH a versioned file and an unversioned symlink to it in .libs
# (librnnoise.so -> librnnoise.so.0 -> librnnoise.so.0.4.1), so the glob
# expands to two paths and cp then demands a directory target and dies with
# "No such file or directory". readlink -f gives the one regular file and does
# not care what upstream's soname version happens to be.
SO=$(readlink -f .libs/librnnoise.so 2>/dev/null || true)
if [ -z "$SO" ] || [ ! -f "$SO" ]; then
    # Fall back to the newest versioned regular file if the dev symlink is absent.
    SO=$(find .libs -maxdepth 1 -type f -name 'librnnoise.so.*' \
         -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -1 | cut -d' ' -f2-)
fi
if [ -z "$SO" ] || [ ! -f "$SO" ]; then
    echo "ERROR: no built librnnoise shared object found in .libs/" >&2
    ls -la .libs/ >&2 || true
    exit 1
fi
echo "   using $SO"
# tmp + mv so a running gateway never ctypes-loads a half-written file.
cp "$SO" "$DEST/librnnoise.so.tmp"
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
