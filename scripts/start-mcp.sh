#!/usr/bin/env bash
set -euo pipefail

PLUGIN_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PLUGIN_DIR"

if ! command -v node >/dev/null 2>&1; then
  echo "Session Recall requires Node.js 18 or newer, but node was not found." >&2
  exit 1
fi

NODE_MAJOR="$(node -p "Number(process.versions.node.split('.')[0])")"
if [ "$NODE_MAJOR" -lt 18 ]; then
  echo "Session Recall requires Node.js 18 or newer. Current version: $(node --version)" >&2
  exit 1
fi

exec node "$PLUGIN_DIR/mcp/server.mjs"
