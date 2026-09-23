#!/bin/bash
# Record the sha256 of an already-built bwrap next to it (build-bwrap.sh does this for new builds).
set -euo pipefail
B="$HOME/.local/share/dryrun/bwrap/${BWRAP_VERSION:-0.11.0}/bwrap"
sha256sum "$B" | cut -d' ' -f1 > "$B.sha256"
echo "pinned $(cat "$B.sha256") for $B"
