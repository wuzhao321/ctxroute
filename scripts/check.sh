#!/bin/sh
set -eu
cd "$(dirname "$0")/.."
export UV_CACHE_DIR="$PWD/.cache/uv"
export UV_PYTHON_DOWNLOADS=never
# Optionally use a Node installation outside PATH for the type checker.
ctx_node_dir="${CTXROUTE_NODE_DIR:-}"
if ! command -v node >/dev/null 2>&1 && [ -n "$ctx_node_dir" ] && [ -x "$ctx_node_dir/node" ]; then
  export PATH="$ctx_node_dir:$PATH"
fi
ctx_uv="${CTXROUTE_UV:-$PWD/.tools/uv-package/bin/uv}"
if [ ! -x "$ctx_uv" ]; then ctx_uv=uv; fi
"$ctx_uv" run --frozen --offline --no-sync ruff check .
"$ctx_uv" run --frozen --offline --no-sync ruff format --check .
"$ctx_uv" run --frozen --offline --no-sync pyright
"$ctx_uv" run --frozen --offline --no-sync pytest
