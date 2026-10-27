#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="$(cd "$(dirname "$(dirname "${BASH_SOURCE[0]}")")" && pwd)"
ENV_FILE="${DOGRAH_ENV_FILE:-$BASE_DIR/api/.env}"
if [[ -f "$ENV_FILE" ]]; then
  set -a && . "$ENV_FILE" && set +a
fi
cd "$BASE_DIR"
# on_startup verifies the source build and real app-server handshake before
# dispatching the durable outbox or consuming any workbench jobs.
exec python -m api.tasks.agent_workbench
