#!/bin/sh
# Project-owned Lima instance store; does not change global shell or Docker settings.
set -eu
CTXROUTE_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
export LIMA_HOME="$CTXROUTE_ROOT/.tools/vm"
exec "$CTXROUTE_ROOT/.tools/lima/bin/limactl" "$@"
