#!/usr/bin/env bash
# Download, verify and unpack ASCOM Conform Universal (ConformU), Linux x64.
#
#   scripts/fetch_conformu.sh [DEST]      (default DEST: ~/.cache/robofast/conformu)
#
# Prints the path of the conformu executable on its last line, e.g.
#   CONFORMU=$(scripts/fetch_conformu.sh | tail -1)
#
# scripts/conformu_aqawan.py runs it against the Aqawan Dome server.
set -euo pipefail

VERSION="v4.5.3"
ASSET="conformu.4.5.3.linux-x64.tar.gz"
SHA256="47aa2a6da3e25bca77723efcaa5cd619f2b728c8e060bfeb098da6b5d0a64600"
URL="https://github.com/ASCOMInitiative/ConformU/releases/download/${VERSION}/${ASSET}"

DEST="${1:-${HOME}/.cache/robofast/conformu}/${VERSION}"
BIN="${DEST}/conformu"

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
    tar -xzf "$tarball" -C "$DEST"
fi
test -x "$BIN"
echo "$BIN"
