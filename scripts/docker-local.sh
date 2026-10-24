#!/bin/sh
# Always address this project's engine, including from the sanitized MCP environment.
set -eu
CTXROUTE_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
unset DOCKER_CONTEXT DOCKER_HOST DOCKER_TLS_VERIFY DOCKER_CERT_PATH DOCKER_API_VERSION
export DOCKER_CONFIG="$CTXROUTE_ROOT/.tools/docker-config"
exec "$CTXROUTE_ROOT/.tools/docker/docker" \
  --host "unix://$CTXROUTE_ROOT/.tools/vm/ctx/sock/docker.sock" "$@"
