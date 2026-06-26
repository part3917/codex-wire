#!/usr/bin/env bash
# ~/.codex/dispatch.sh - run ONE codex exec job, detect completion via its unique
# --output-last-message file, then reap the whole codex process group.
#
# Usage (run via Claude Bash with run_in_background: true):
#   ~/.codex/dispatch.sh <read-only|workspace-write> <cwd> "<prompt>" [max_minutes]
#   CODEX_WIRE_OUTDIR overrides the output directory.
# On completion prints machine-readable STATUS/OUT/LOG headers plus any summary.
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
CWD_INPUT="$2"
PROMPT="$3"
MAXMIN="${4:-90}"
OUTDIR="${CODEX_WIRE_OUTDIR:-${TMPDIR:-/tmp}/codex-wire}"

case "$SANDBOX" in
  read-only|workspace-write) ;;
  *) die "sandbox must be read-only or workspace-write" ;;
esac

CWD="$(cd "$CWD_INPUT" 2>/dev/null && pwd -P)" || die "cwd is not a directory: $CWD_INPUT"
[ -d "$CWD" ] || die "cwd is not a directory: $CWD"
[ -r "$CWD" ] || die "cwd is not readable: $CWD"
[ -x "$CWD" ] || die "cwd is not searchable: $CWD"

case "$MAXMIN" in
  ''|*[!0-9]*) die "max_minutes must be a positive integer" ;;
esac
[ "$MAXMIN" -gt 0 ] || die "max_minutes must be a positive integer"
[ "$MAXMIN" -le 10080 ] || die "max_minutes must be between 1 and 10080"

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
  base="$(mktemp "$OUTDIR/codex_XXXXXX" 2>/dev/null)" || return 1
  out="${base}.md"
  mv "$base" "$out" || return 1
  echo "$out"
}

OUT="$(make_outfile)" || die "failed to create output file in: $OUTDIR"
LOG="${OUT}.log"

CPID=""
PGID=""
REAPED=0
STATUS=""
TIMED_OUT=0
STOP_REASON=""
CLEANING=0
TERMINATED_GROUP=0
TERMINATED_CHILD=0
SUMMARY_PARENT_RUNNING=0
LAST_OUT_SIG=""
STABLE_HITS=0

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

get_pgid() {
  target_pid="$1"
  [ -n "$target_pid" ] || return 1
  pgid="$(ps -o pgid= -p "$target_pid" 2>/dev/null | tr -d '[:space:]' || true)"
  case "$pgid" in
    ''|*[!0-9]*) return 1 ;;
    *) echo "$pgid" ;;
  esac
}

cleanup() {
  [ "$CLEANING" -eq 0 ] || return 0
  CLEANING=1

  cleanup_pgid="${PGID:-}"
  [ -n "$cleanup_pgid" ] || cleanup_pgid="${CPID:-}"

  # Group-kill the codex session group, but NEVER our own process group
  # (cleanup_pgid == SELF_PGID would make `kill -<pgid>` suicide this script).
  if [ -n "$cleanup_pgid" ] \
     && [ -n "${SELF_PGID:-}" ] \
     && [ "$cleanup_pgid" != "$SELF_PGID" ] \
     && kill -0 "-$cleanup_pgid" 2>/dev/null; then
    TERMINATED_GROUP=1
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

  # Always also reap the direct child (covers setsid-failed / shared-group case,
  # where the codex group could not be targeted without hitting our own group).
  if [ -n "${CPID:-}" ] && kill -0 "$CPID" 2>/dev/null; then
    TERMINATED_CHILD=1
    kill -TERM "$CPID" 2>/dev/null || true

    grace_until=$(( $(date +%s) + 2 ))
    while kill -0 "$CPID" 2>/dev/null; do
      reap_zombie_parent
      kill -0 "$CPID" 2>/dev/null || break
      [ "$(date +%s)" -ge "$grace_until" ] && break
      sleep 1
    done

    if kill -0 "$CPID" 2>/dev/null; then
      kill -KILL "$CPID" 2>/dev/null || true
    fi
  fi

  PGID=""
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
  [ "$CLEANING" -eq 0 ] || return 0
  cleanup
  wait_for_codex >/dev/null 2>&1 || true
  exit 130
}

on_term() {
  [ "$CLEANING" -eq 0 ] || return 0
  cleanup
  wait_for_codex >/dev/null 2>&1 || true
  exit 143
}

out_signature() {
  [ -s "$OUT" ] || return 1
  if sig="$(stat -f '%z:%m' "$OUT" 2>/dev/null)"; then
    echo "$sig"
  elif sig="$(stat -c '%s:%Y' "$OUT" 2>/dev/null)"; then
    echo "$sig"
  else
    size="$(wc -c < "$OUT" | tr -d '[:space:]')"
    mtime="$(date -r "$OUT" +%s 2>/dev/null || echo 0)"
    echo "${size}:${mtime}"
  fi
}

summary_is_stable() {
  sig="$(out_signature)" || {
    LAST_OUT_SIG=""
    STABLE_HITS=0
    return 1
  }

  if [ "$sig" = "$LAST_OUT_SIG" ]; then
    STABLE_HITS=$((STABLE_HITS + 1))
  else
    LAST_OUT_SIG="$sig"
    STABLE_HITS=1
  fi

  [ "$STABLE_HITS" -ge 2 ]
}

# our own process group — cleanup must never signal this, or it kills the script
SELF_PGID="$(get_pgid "$$" 2>/dev/null || true)"

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
# codex is placed in its own session/group (pgid == CPID) by setsid, so target the
# child PID as the group id. Do NOT read the child's *current* pgid here: before
# setsid completes it is still THIS script's group, and group-killing that = suicide.
PGID="$CPID"

deadline=$(( $(date +%s) + MAXMIN * 60 ))
while :; do
  now="$(date +%s)"

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

  if [ "$now" -ge "$deadline" ]; then
    STOP_REASON="timeout"
    TIMED_OUT=1
    echo "[dispatch] TIMEOUT ${MAXMIN}m - reaping process group $PGID" >&2
    break
  fi

  if summary_is_stable; then
    STOP_REASON="summary"
    break
  fi

  sleep 2
done

sleep 1

case "$STOP_REASON" in
  summary)
    reap_zombie_parent
    if [ "$REAPED" -eq 0 ] && ! kill -0 "$CPID" 2>/dev/null; then
      wait_for_codex >/dev/null 2>&1 || true
    fi
    if [ "$REAPED" -eq 0 ]; then
      SUMMARY_PARENT_RUNNING=1
    fi
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

print_headers() {
  echo "STATUS=$1"
  echo "OUT=$OUT"
  echo "LOG=$LOG"
}

has_summary() {
  [ -s "$OUT" ]
}

status_is_cleanup_summary() {
  [ "$STOP_REASON" = "summary" ] && \
    [ "$SUMMARY_PARENT_RUNNING" -eq 1 ] && \
    { [ "$TERMINATED_GROUP" -eq 1 ] || [ "$TERMINATED_CHILD" -eq 1 ]; }
}

if [ "$TIMED_OUT" -eq 1 ]; then
  print_headers "timeout"
  if has_summary; then
    echo "----- codex partial/late summary -----"
    cat "$OUT"
  else
    echo "[dispatch] no summary after timeout - see $LOG" >&2
    tail_log
  fi
  exit 124
fi

if [ -n "${STATUS:-}" ] && [ "$STATUS" -ne 0 ] && ! status_is_cleanup_summary; then
  print_headers "error"
  if has_summary; then
    echo "----- codex partial/late summary -----"
    cat "$OUT"
  else
    echo "[dispatch] codex exited nonzero ($STATUS) without summary - see $LOG" >&2
    tail_log
  fi
  exit "$STATUS"
fi

if has_summary; then
  print_headers "ok"
  echo "----- codex summary -----"
  cat "$OUT"
  exit 0
fi

print_headers "error"
if [ -n "${STATUS:-}" ] && [ "$STATUS" -ne 0 ]; then
  echo "[dispatch] codex exited nonzero ($STATUS) without summary - see $LOG" >&2
else
  echo "[dispatch] no summary - see $LOG" >&2
fi
tail_log
exit 1
