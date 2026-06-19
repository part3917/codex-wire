#!/usr/bin/env bash
# ~/.codex/dispatch.sh - run ONE codex exec job, detect completion via its unique
# --output-last-message file, then reap the whole codex process group.
#
# Usage (run via Claude Bash with run_in_background: true):
#   ~/.codex/dispatch.sh <read-only|workspace-write> <cwd> "<prompt>" [max_minutes]
#   CODEX_WIRE_OUTDIR overrides the output directory.
# On completion prints:  OUT=<summary path>  +  the codex summary body.
set -u

die() {
  echo "[dispatch] ERROR: $*" >&2
  exit 1
}

usage() {
  die "usage: $0 <read-only|workspace-write> <cwd> <prompt> [max_minutes]"
}

[ "$#" -ge 3 ] && [ "$#" -le 4 ] || usage

SANDBOX="$1"
CWD="$2"
PROMPT="$3"
MAXMIN="${4:-90}"
OUTDIR="${CODEX_WIRE_OUTDIR:-${TMPDIR:-/tmp}/codex-wire}"

case "$SANDBOX" in
  read-only|workspace-write) ;;
  *) die "sandbox must be read-only or workspace-write" ;;
esac

[ -d "$CWD" ] || die "cwd is not a directory: $CWD"

case "$MAXMIN" in
  ''|*[!0-9]*) die "max_minutes must be a positive integer" ;;
esac
[ "$MAXMIN" -gt 0 ] || die "max_minutes must be a positive integer"

CODEX_BIN="$(command -v codex 2>/dev/null)" || die "codex command not found"

if command -v setsid >/dev/null 2>&1; then
  SETSID_RUNNER="setsid"
elif command -v perl >/dev/null 2>&1; then
  SETSID_RUNNER="perl"
else
  die "setsid command not found and perl POSIX::setsid fallback is unavailable"
fi

mkdir -p "$OUTDIR" || die "failed to create output directory: $OUTDIR"

make_outfile() {
  out="$(mktemp "$OUTDIR/codex_XXXXXX.md" 2>/dev/null)" || return 1

  if [ "$(basename "$out")" != "codex_XXXXXX.md" ]; then
    echo "$out"
    return 0
  fi

  # BSD mktemp only replaces trailing Xs. Keep the requested form on GNU, but
  # fall back to a mktemp-derived name that still ends in .md on macOS.
  rm -f "$out"
  tries=0
  while [ "$tries" -lt 20 ]; do
    base="$(mktemp "$OUTDIR/codex_XXXXXX" 2>/dev/null)" || return 1
    candidate="${base}.md"
    if ( set -C; : > "$candidate" ) 2>/dev/null; then
      rm -f "$base"
      echo "$candidate"
      return 0
    fi
    rm -f "$base"
    tries=$((tries + 1))
  done

  return 1
}

OUT="$(make_outfile)" || die "failed to create output file in: $OUTDIR"
LOG="${OUT}.log"

CPID=""
PGID=""
REAPED=0
STATUS=""
TIMED_OUT=0
STOP_REASON=""

tail_log() {
  if [ -f "$LOG" ]; then
    echo "[dispatch] ${LOG} tail:" >&2
    tail -n 80 "$LOG" >&2
  else
    echo "[dispatch] log file missing: $LOG" >&2
  fi
}

parent_is_zombie() {
  [ -n "${CPID:-}" ] || return 1
  state="$(ps -o stat= -p "$CPID" 2>/dev/null || true)"
  case "$state" in
    *Z*) return 0 ;;
    *) return 1 ;;
  esac
}

reap_zombie_parent() {
  [ -n "${CPID:-}" ] || return 0
  [ "$REAPED" -eq 0 ] || return 0

  if parent_is_zombie; then
    wait_for_codex
  fi
}

cleanup() {
  [ -n "${PGID:-}" ] || return 0
  cleanup_pgid="$PGID"
  PGID=""

  if kill -0 "-$cleanup_pgid" 2>/dev/null; then
    kill -TERM "-$cleanup_pgid" 2>/dev/null || true

    grace_until=$(( $(date +%s) + 2 ))
    while kill -0 "-$cleanup_pgid" 2>/dev/null; do
      reap_zombie_parent
      kill -0 "-$cleanup_pgid" 2>/dev/null || break
      [ "$(date +%s)" -ge "$grace_until" ] && break
      sleep 1
    done

    if kill -0 "-$cleanup_pgid" 2>/dev/null; then
      kill -KILL "-$cleanup_pgid" 2>/dev/null || true
    fi
  fi
}

wait_for_codex() {
  [ -n "${CPID:-}" ] || return 1
  if [ "$REAPED" -eq 0 ]; then
    wait "$CPID"
    STATUS=$?
    REAPED=1
  fi
  return 0
}

on_int() {
  cleanup
  wait_for_codex >/dev/null 2>&1 || true
  exit 130
}

on_term() {
  cleanup
  wait_for_codex >/dev/null 2>&1 || true
  exit 143
}

trap cleanup EXIT
trap on_int INT
trap on_term TERM

if [ "$SETSID_RUNNER" = "setsid" ]; then
  setsid "$CODEX_BIN" exec -C "$CWD" --skip-git-repo-check -s "$SANDBOX" \
    -c approval_policy=never --output-last-message "$OUT" "$PROMPT" \
    < /dev/null > "$LOG" 2>&1 &
else
  perl -MPOSIX=setsid -e '
    setsid() or die "setsid failed: $!\n";
    exec @ARGV or die "exec failed: $!\n";
  ' -- "$CODEX_BIN" exec -C "$CWD" --skip-git-repo-check -s "$SANDBOX" \
    -c approval_policy=never --output-last-message "$OUT" "$PROMPT" \
    < /dev/null > "$LOG" 2>&1 &
fi
CPID=$!
PGID=$CPID

deadline=$(( $(date +%s) + MAXMIN * 60 ))
while :; do
  if [ -s "$OUT" ]; then
    STOP_REASON="summary"
    break
  fi

  if parent_is_zombie; then
    STOP_REASON="exit"
    wait_for_codex
    break
  fi

  if ! kill -0 "$CPID" 2>/dev/null; then
    STOP_REASON="exit"
    wait_for_codex
    break
  fi

  if [ "$(date +%s)" -ge "$deadline" ]; then
    STOP_REASON="timeout"
    TIMED_OUT=1
    echo "[dispatch] TIMEOUT ${MAXMIN}m - reaping process group $PGID" >&2
    break
  fi

  sleep 2
done

sleep 1

case "$STOP_REASON" in
  summary)
    cleanup
    wait_for_codex >/dev/null 2>&1 || true
    ;;
  timeout)
    cleanup
    wait_for_codex >/dev/null 2>&1 || true
    ;;
  exit)
    cleanup
    ;;
  *)
    cleanup
    wait_for_codex >/dev/null 2>&1 || true
    ;;
esac

echo "OUT=$OUT"
if [ -s "$OUT" ]; then
  echo "----- codex summary -----"
  cat "$OUT"
  exit 0
fi

if [ "$TIMED_OUT" -eq 1 ]; then
  echo "[dispatch] no summary after timeout - see $LOG" >&2
  tail_log
  exit 124
fi

if [ -n "${STATUS:-}" ] && [ "$STATUS" -ne 0 ]; then
  echo "[dispatch] codex exited nonzero ($STATUS) without summary - see $LOG" >&2
else
  echo "[dispatch] no summary - see $LOG" >&2
fi
tail_log
exit 1
