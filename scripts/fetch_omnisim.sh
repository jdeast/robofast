#!/usr/bin/env bash
# Download, verify and unpack the ASCOM Alpaca OmniSimulators (Linux x64).
#
#   scripts/fetch_omnisim.sh [DEST]      (default DEST: ~/.cache/robofast/omnisim)
#
# Prints the path of the simulator executable on its last line, e.g.
#   ROBOFAST_OMNISIM_BIN=$(scripts/fetch_omnisim.sh | tail -1)
#
# Run it headless with  "$BIN" --urls=http://127.0.0.1:32323  (the tests pick
# a free port and a throw-away HOME themselves). Settings and logs go under
# $HOME/ascom.
set -euo pipefail

VERSION="v0.4.0"
ASSET="ascom.alpaca.simulators.linux-x64.tar.xz"
SHA256="c004a13c869473f4e2f110a231c2bcadbaa43ccabc324deb47bb7f2e79a5d6a6"
URL="https://github.com/ASCOMInitiative/ASCOM.Alpaca.Simulators/releases/download/${VERSION}/${ASSET}"

DEST="${1:-${HOME}/.cache/robofast/omnisim}/${VERSION}"
BIN="${DEST}/ascom.alpaca.simulators.linux-x64/ascom.alpaca.simulators"

if [ ! -x "$BIN" ]; then
    mkdir -p "$DEST"
    tarball="${DEST}/${ASSET}"
    if [ ! -f "$tarball" ]; then
        echo "downloading $URL" >&2
        python3 - "$URL" "$tarball.part" <<'EOF'
import sys, urllib.request
urllib.request.urlretrieve(sys.argv[1], sys.argv[2])
EOF
        mv "$tarball.part" "$tarball"
    fi
    echo "${SHA256}  ${tarball}" | sha256sum -c - >&2
    tar -xJf "$tarball" -C "$DEST"
fi
test -x "$BIN"
echo "$BIN"
