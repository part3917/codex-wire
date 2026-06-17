#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# 1. dispatch wrapper
mkdir -p "$HOME/.codex"
ln -sf "$ROOT/dispatch.sh" "$HOME/.codex/dispatch.sh"
echo "Installed dispatch wrapper at $HOME/.codex/dispatch.sh"

# 2. .env (never overwrite)
if [ ! -e "$ROOT/.env" ]; then
  cp "$ROOT/.env.example" "$ROOT/.env"
  echo "Created .env from .env.example"
else
  echo "Keeping existing .env"
fi

# 3. /codex skill for Claude Code (never overwrite)
CMD_DIR="$HOME/.claude/commands"
if [ -e "$CMD_DIR/codex.md" ]; then
  echo "Keeping existing $CMD_DIR/codex.md"
else
  mkdir -p "$CMD_DIR"
  cp "$ROOT/examples/codex.md" "$CMD_DIR/codex.md"
  echo "Installed /codex skill at $CMD_DIR/codex.md"
fi

echo
echo "Next steps:"
echo "  1. Edit $ROOT/.env"
echo "  2. (optional) Put codex-instructions on your PATH, e.g.:"
echo "       ln -sf \"$ROOT/codex-instructions\" /usr/local/bin/codex-instructions"
echo "  3. Run the monitor:  python3 $ROOT/codex_monitor.py"
echo "  4. Open: http://localhost:8787"
echo "  5. In Claude Code, start orchestrating:  /codex"
