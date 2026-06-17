#!/usr/bin/env bash
# ~/.codex/dispatch.sh — run ONE codex exec job, detect completion via its unique
# --output-last-message file, then REAP the lingering codex process.
# Solves codex's "work done != process exits" zombie pattern. Parallel-safe:
# only kills the process whose args carry THIS job's unique output path.
#
# Usage (run via Claude Bash with run_in_background: true):
#   ~/.codex/dispatch.sh <read-only|workspace-write> <cwd> "<prompt>" [max_minutes]
#   CODEX_WIRE_OUTDIR overrides the output directory.
# On completion prints:  OUT=<summary path>  +  the codex summary body.
set -u
SANDBOX="${1:?sandbox: read-only|workspace-write}"
CWD="${2:?cwd}"
PROMPT="${3:?prompt}"
MAXMIN="${4:-90}"
OUTDIR="${CODEX_WIRE_OUTDIR:-${TMPDIR:-/tmp}/codex-wire}"
OUT="$OUTDIR/codex_$(date +%s%N).md"
mkdir -p "$(dirname "$OUT")"

codex exec -C "$CWD" --skip-git-repo-check -s "$SANDBOX" \
  -c approval_policy=never --output-last-message "$OUT" "$PROMPT" \
  < /dev/null > "${OUT}.log" 2>&1 &
CPID=$!

deadline=$(( $(date +%s) + MAXMIN * 60 ))
while :; do
  [ -s "$OUT" ] && break                       # summary written = work complete
  kill -0 "$CPID" 2>/dev/null || break          # codex exited on its own
  [ "$(date +%s)" -ge "$deadline" ] && { echo "[dispatch] TIMEOUT ${MAXMIN}m — reaping" >&2; break; }
  sleep 2
done

sleep 1                                          # let final flush settle
# reap the lingering codex for THIS job (unique output path; never matches this wrapper)
kill "$CPID" 2>/dev/null
sleep 1; kill -0 "$CPID" 2>/dev/null && kill -9 "$CPID" 2>/dev/null
pkill -f "output-last-message $OUT" 2>/dev/null

echo "OUT=$OUT"
if [ -s "$OUT" ]; then
  echo "----- codex summary -----"; cat "$OUT"
else
  echo "[dispatch] no summary — see ${OUT}.log"
fi
