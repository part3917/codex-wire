#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

mkdir -p "$HOME/.codex"
ln -sf "$ROOT/dispatch.sh" "$HOME/.codex/dispatch.sh"

if [ ! -e "$ROOT/.env" ]; then
  cp "$ROOT/.env.example" "$ROOT/.env"
  echo "Created .env from .env.example"
else
  echo "Keeping existing .env"
fi

echo "Installed dispatch wrapper at $HOME/.codex/dispatch.sh"
echo
echo "Next steps:"
echo "  1. Edit $ROOT/.env"
echo "  2. Run: python3 $ROOT/codex_monitor.py"
echo "  3. Open: http://localhost:8787"
