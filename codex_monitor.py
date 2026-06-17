#!/usr/bin/env python3
"""CODEX WIRE — live telemetry for codex-exec agents driven by Claude Code.
Run:  python3 ~/codex_monitor.py   →   open http://localhost:8787
Sources (stdlib only, no deps):
  • `ps`                          → running codex exec jobs (pid, elapsed, cwd, sandbox)
  • ~/.codex/sessions/**/*.jsonl  → live activity: commands, file edits, agent messages, tokens
"""
import argparse, ctypes, datetime, glob, json, os, re, shlex, signal, struct, subprocess, sys, time, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8787
SESS = os.path.expanduser("~/.codex/sessions")
HOME = os.path.expanduser("~")

SESSION_LIMIT = int(os.environ.get("CODEX_MONITOR_SESSION_LIMIT", "80"))
RECENT_LIMIT = int(os.environ.get("CODEX_MONITOR_RECENT_LIMIT", "50"))
ACTIVE_STALE_SEC = int(os.environ.get("CODEX_MONITOR_STALE_SEC", "120"))
FEED_WINDOW_SEC = int(os.environ.get("CODEX_MONITOR_FEED_WINDOW_SEC", "1800"))
LONG_OP_GRACE_SEC = int(os.environ.get("CODEX_MONITOR_LONG_OP_GRACE_SEC", "180"))
TRACK_PATH = os.path.expanduser(os.environ.get("CODEX_MONITOR_TRACK_PATH", "~/.codex/codex_wire_jobs.json"))
TRACK_TTL_SEC = int(os.environ.get("CODEX_MONITOR_TRACK_TTL_SEC", str(7 * 24 * 3600)))

# Approximate gpt-5.5 placeholder pricing per 1M tokens; adjust if pricing changes.
COST_INPUT_PER_MTOK = float(os.environ.get("CODEX_MONITOR_COST_INPUT", "1.25"))
COST_CACHED_PER_MTOK = float(os.environ.get("CODEX_MONITOR_COST_CACHED", "0.125"))
COST_OUTPUT_PER_MTOK = float(os.environ.get("CODEX_MONITOR_COST_OUTPUT", "10.0"))

_TRACK_STATE = None


def _short(p):
    return (p or "").replace(HOME, "~")


def _ps_lines():
    try:
        return subprocess.run(["ps", "-axww", "-o", "pid=,etime=,stat=,args="],
                              capture_output=True, text=True, timeout=4).stdout.splitlines()
    except Exception:
        return []


def _is_monitor_server(pid, args):
    try:
        ipid = int(pid)
    except Exception:
        ipid = -1
    if ipid in (os.getpid(), os.getppid()):
        return True
    return bool(re.search(r"(^|\s)(python[0-9.]*|/.*/python[0-9.]*)\s+.*codex_monitor\.py(\s|$)", args))


def _pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False
    except Exception:
        return False


_VALUE_FLAGS = {
    "-C", "--cd", "--cwd", "--directory",
    "-s", "--sandbox",
    "-m", "--model",
    "-c", "--config",
    "--ask-for-approval", "--approval-policy",
    "--output-last-message",
    "--profile",
}

_BOOL_FLAGS = {
    "--json", "--help", "-h", "--version", "-V",
    "--dangerously-bypass-approvals-and-sandbox",
}


def _proc_argv(pid):
    """Return exact argv for a pid on macOS, preserving spaces inside args."""
    if sys.platform != "darwin":
        return None
    try:
        ipid = int(pid)
    except Exception:
        return None
    try:
        libc = ctypes.CDLL(None)
        mib = (ctypes.c_int * 3)(1, 49, ipid)  # CTL_KERN, KERN_PROCARGS2, pid
        size = ctypes.c_size_t(0)
        if libc.sysctl(mib, 3, None, ctypes.byref(size), None, 0) != 0 or size.value <= 0:
            return None
        buf = ctypes.create_string_buffer(size.value)
        if libc.sysctl(mib, 3, buf, ctypes.byref(size), None, 0) != 0:
            return None
        raw = buf.raw[:size.value]
        argc = struct.unpack_from("i", raw, 0)[0]
        if argc <= 0:
            return None
        off = 4
        while off < len(raw) and raw[off] != 0:
            off += 1
        while off < len(raw) and raw[off] == 0:
            off += 1
        argv = []
        for _ in range(argc):
            if off >= len(raw):
                break
            end = raw.find(b"\0", off)
            if end < 0:
                break
            if end > off:
                argv.append(raw[off:end].decode("utf-8", "replace"))
            off = end + 1
            while off < len(raw) and raw[off] == 0:
                off += 1
        return argv or None
    except Exception:
        return None


def _argv_from_ps(args):
    try:
        return shlex.split(args)
    except Exception:
        return args.split()


def _flag_value(argv, *flags):
    if not argv:
        return None
    for i, tok in enumerate(argv):
        for flag in flags:
            if tok == flag and i + 1 < len(argv):
                return argv[i + 1]
            if tok.startswith(flag + "="):
                return tok[len(flag) + 1:]
    return None


def _next_flag_index(text):
    matches = []
    for flag in _VALUE_FLAGS | _BOOL_FLAGS:
        m = re.search(r"(?<!\S)" + re.escape(flag) + r"(?:=|\s|$)", text)
        if m:
            matches.append(m.start())
    return min(matches) if matches else len(text)


def _raw_flag_value(args, *flags, want_dir=False):
    for flag in flags:
        m = re.search(r"(?<!\S)" + re.escape(flag) + r"(?:=|\s+)", args)
        if not m:
            continue
        value = args[m.end():_next_flag_index(args[m.end():]) + m.end()].strip()
        if not value:
            continue
        try:
            parts = shlex.split(value)
        except Exception:
            parts = value.split()
        if not parts:
            continue
        if want_dir:
            best = ""
            for i in range(1, len(parts) + 1):
                cand = " ".join(parts[:i])
                if os.path.isdir(cand):
                    best = cand
            if best:
                return best
        return parts[0] if len(parts) == 1 else " ".join(parts)
    return None


def _codex_exec_prompt(argv):
    if not argv or "exec" not in argv:
        return ""
    pos = []
    i = argv.index("exec") + 1
    while i < len(argv):
        tok = argv[i]
        if tok == "--":
            pos.extend(argv[i + 1:])
            break
        if tok in _VALUE_FLAGS:
            i += 2
            continue
        if any(tok.startswith(flag + "=") for flag in _VALUE_FLAGS):
            i += 1
            continue
        if tok in _BOOL_FLAGS:
            i += 1
            continue
        if tok.startswith("-"):
            i += 1
            continue
        pos.append(tok)
        i += 1
    return " ".join(pos).strip()[:240]


def _normalize_out(path, cwd=None):
    if not path:
        return None
    out = os.path.expanduser(str(path))
    if not os.path.isabs(out):
        base = cwd if cwd and os.path.isdir(cwd) else HOME
        out = os.path.join(base, out)
    return os.path.abspath(out)


def running_jobs():
    jobs, seen = [], set()
    for ln in _ps_lines():
        try:
            pid, etime, stat, args = ln.strip().split(None, 3)
        except ValueError:
            continue
        if "Z" in stat.upper():
            continue
        if _is_monitor_server(pid, args):
            continue
        if "codex" not in args or " exec" not in args:
            continue
        if "/codex" not in args and "@openai/codex" not in args and "codex-upstream" not in args:
            continue
        argv = _proc_argv(pid) or _argv_from_ps(args)
        cwd = (_flag_value(argv, "-C", "--cd", "--cwd", "--directory") or
               _raw_flag_value(args, "-C", "--cd", "--cwd", "--directory", want_dir=True) or "?")
        sandbox = (_flag_value(argv, "-s", "--sandbox") or
                   _raw_flag_value(args, "-s", "--sandbox") or "?")
        out = (_flag_value(argv, "--output-last-message") or
               _raw_flag_value(args, "--output-last-message"))
        out = _normalize_out(out, cwd)
        plabel = _codex_exec_prompt(argv)
        key = out or pid       # unique per dispatch: collapses wrapper/child, KEEPS parallel jobs
        if key in seen:
            continue
        seen.add(key)
        jobs.append({"pid": pid, "elapsed": etime, "cwd": _short(cwd), "cwd_raw": cwd,
                     "sandbox": sandbox, "out": out, "plabel": plabel, "alive": _pid_alive(pid)})
    return jobs


_PARSE_CACHE = {}            # path -> (mtime, size, summary) — skip re-parsing unchanged files
_BIG = 8 * 1024 * 1024       # over this: read head+tail only, skip the middle
_HEAD = 256 * 1024
_TAIL = 1024 * 1024

_LONG_CMD_RE = re.compile(
    r"\b(build|compile|pytest|test|lint|check|py_compile|tsc|vitest|jest|"
    r"npm\s+(install|ci|run|test)|pnpm\s+(install|test|build)|yarn\s+(install|test|build)|"
    r"cargo|go\s+test|mvn|gradle|docker\s+build|make)\b",
    re.I,
)


def _iter(path, size=None, parse_errors=None):
    """Yield json objects from a jsonl. For very large files, read only the
    head (session meta / prompt) + tail (recent events) so a huge rollout
    doesn't cost full-file parsing on every poll."""
    try:
        with open(path, "rb") as fh:
            if size is None:
                fh.seek(0, os.SEEK_END); size = fh.tell()
            if size > _BIG:
                fh.seek(0)
                for raw in fh.read(_HEAD).split(b"\n")[:-1]:
                    try:
                        if raw.strip():
                            yield json.loads(raw)
                    except Exception as e:
                        if parse_errors is not None and len(parse_errors) < 3:
                            parse_errors.append(str(e))
                fh.seek(size - _TAIL); fh.readline()   # drop the partial first line
                for raw in fh:
                    try:
                        if raw.strip():
                            yield json.loads(raw)
                    except Exception as e:
                        if parse_errors is not None and len(parse_errors) < 3:
                            parse_errors.append(str(e))
            else:
                fh.seek(0)
                for raw in fh:
                    try:
                        if raw.strip():
                            yield json.loads(raw)
                    except Exception as e:
                        if parse_errors is not None and len(parse_errors) < 3:
                            parse_errors.append(str(e))
    except Exception:
        return


def _epoch(ts):
    if not ts:
        return None
    try:
        return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _event(events, ts, k, t, important=False):
    t = re.sub(r"\s+", " ", str(t or "")).strip()
    if t:
        events.append({"ts": ts, "k": k, "t": t[:300], "important": important})


def _trim_output(out, limit=2400):
    out = str(out or "").strip()
    if len(out) <= limit:
        return out
    head = out[:limit // 2]
    tail = out[-limit // 2:]
    return head + "\n…\n" + tail


def _exit_code(out):
    m = re.search(r"Process exited with code\s+(-?\d+)", str(out or ""))
    return int(m.group(1)) if m else None


def _classify_error(output, exit_code=None, cmd=""):
    text = (str(output or "") + "\n" + str(cmd or "")).lower()
    if "jsonl" in text or ("json" in text and re.search(r"parse|decode|unterminated|expecting value|extra data", text)):
        return {"kind": "jsonl", "label": "JSONL"}
    if re.search(r"\b(timed?\s*out|timeout|deadline exceeded|curl:\s*\(28\))\b", text):
        return {"kind": "timeout", "label": "TIMEOUT"}
    if re.search(r"\b(permission denied|operation not permitted|eacces|eperm|access is denied)\b", text):
        return {"kind": "permission", "label": "PERMISSION"}
    if re.search(r"\b(network|enotfound|econnreset|econnrefused|etimedout|dns|tls|ssl|fetch failed|curl:\s*\([567]\))\b", text):
        return {"kind": "network", "label": "NETWORK"}
    if re.search(r"\b(sandbox|sandboxed|rejected\(|approval|not allowed|outside the sandbox)\b", text):
        return {"kind": "sandbox", "label": "SANDBOX"}
    if exit_code not in (None, 0):
        return {"kind": "exit", "label": f"EXIT {exit_code}"}
    return {"kind": "error", "label": "ERROR"}


def _error_entry(ts, cmd, exit_code, output):
    err = {"ts": ts, "cmd": cmd, "exit_code": exit_code, "output": _trim_output(output)}
    err.update(_classify_error(err["output"], exit_code, cmd))
    return err


def _usage_cost(tokens):
    uncached = max(0, tokens.get("input", 0) - tokens.get("cached", 0))
    cost = (uncached * COST_INPUT_PER_MTOK +
            tokens.get("cached", 0) * COST_CACHED_PER_MTOK +
            tokens.get("output", 0) * COST_OUTPUT_PER_MTOK) / 1_000_000
    return round(cost, 4)


def _normalize_usage(info):
    usage = ((info or {}).get("total_token_usage") or
             (info or {}).get("last_token_usage") or
             (info or {}).get("usage") or {})
    tokens = {
        "input": int(usage.get("input_tokens") or 0),
        "cached": int(usage.get("cached_input_tokens") or 0),
        "output": int(usage.get("output_tokens") or 0),
        "reasoning": int(usage.get("reasoning_output_tokens") or 0),
        "total": int(usage.get("total_tokens") or 0),
    }
    if not tokens["total"]:
        tokens["total"] = tokens["input"] + tokens["output"]
    tokens["cost"] = _usage_cost(tokens)
    return tokens


def parse_session(path):
    """Extract a rich summary + event stream from one rollout jsonl."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    s = {"file": path, "cwd": "", "prompt": "", "n_cmds": 0, "n_edits": 0,
         "files": [], "last_cmd": "", "last_msg": "", "rate_pct": None,
         "ctx_window": None, "started": None, "last_ts": st.st_mtime,
         "last_event_ts": "", "last_event_epoch": None, "pending_cmd": "",
         "pending_cmds": [], "pending_long": False,
         "events": [], "model": "", "outputs": [], "errors": [],
         "tokens": {"input": 0, "cached": 0, "output": 0, "reasoning": 0, "total": 0, "cost": 0.0}}
    files, exec_calls, parse_errors = set(), {}, []
    for o in _iter(path, st.st_size, parse_errors):
        ts = o.get("timestamp", "")
        ets = _epoch(ts)
        if ets:
            s["last_ts"] = max(s["last_ts"], ets)
            if s["last_event_epoch"] is None or ets > s["last_event_epoch"]:
                s["last_event_epoch"] = ets
                s["last_event_ts"] = ts
        p = o.get("payload", {}) or {}
        otype, ptype = o.get("type"), p.get("type", "")
        if otype == "session_meta" or ("cwd" in p and not s["cwd"]):
            s["cwd"] = p.get("cwd", s["cwd"]); s["model"] = p.get("model", s["model"]) or s["model"]
        elif ptype == "task_started":
            s["started"] = p.get("started_at"); s["ctx_window"] = p.get("model_context_window")
        elif ptype == "user_message":
            s["prompt"] = (p.get("message") or "")[:5000]
        elif ptype == "agent_message":
            m = (p.get("message") or "").strip()
            if m:
                s["last_msg"] = m
                _event(s["events"], ts, "msg", m, True)
        elif ptype == "function_call" and p.get("name") == "exec_command":
            try:
                args = json.loads(p.get("arguments", "{}"))
                cmd = args.get("cmd", "")
            except Exception:
                cmd = p.get("arguments", "")
            cmd = re.sub(r"\s+", " ", str(cmd)).strip()
            if cmd:
                s["n_cmds"] += 1; s["last_cmd"] = cmd
                if p.get("call_id"):
                    exec_calls[p.get("call_id")] = {"cmd": cmd, "ts": ts, "epoch": ets}
                _event(s["events"], ts, "cmd", cmd[:240], False)
        elif ptype == "function_call_output":
            out = str(p.get("output", ""))
            call_id = p.get("call_id")
            call = exec_calls.pop(call_id, None) if call_id else None
            if call or out.startswith("Chunk ID:"):
                code = _exit_code(out)
                cmd = (call or {}).get("cmd") if isinstance(call, dict) else (call or s["last_cmd"])
                entry = {"ts": ts, "cmd": cmd, "exit_code": code, "output": _trim_output(out)}
                s["outputs"].append(entry)
                if code not in (None, 0):
                    err = _error_entry(ts, entry["cmd"], code, entry["output"])
                    s["errors"].append(err)
                    _event(s["events"], ts, "err", f"{err['label']}: {entry['cmd']}", True)
                else:
                    _event(s["events"], ts, "out", entry["cmd"], False)
        elif ptype == "custom_tool_call" and p.get("name") == "apply_patch":
            s["n_edits"] += 1
            _event(s["events"], ts, "edit", "apply_patch", True)
        elif ptype == "custom_tool_call_output":
            output = str(p.get("output", ""))
            for m in re.finditer(r"[AM]\s+(\S+)", output):
                files.add(os.path.basename(m.group(1)))
            if "error" in output.lower() or "failed" in output.lower():
                err = _error_entry(ts, "apply_patch", None, output)
                s["errors"].append(err)
                _event(s["events"], ts, "err", f"{err['label']}: apply_patch", True)
        elif ptype == "token_count":
            rl = (p.get("rate_limits") or {}).get("primary") or {}
            if rl.get("used_percent") is not None:
                s["rate_pct"] = rl["used_percent"]
            info = p.get("info") or {}
            if info:
                s["tokens"] = _normalize_usage(info)
                if info.get("model_context_window"):
                    s["ctx_window"] = info.get("model_context_window")
    if parse_errors:
        err = _error_entry("", "jsonl parser", None, "JSONL parse damage: " + "; ".join(parse_errors))
        s["errors"].append(err)
        _event(s["events"], s.get("last_event_ts") or "", "err", f"{err['label']}: session log damaged", True)
    pending = [v for v in exec_calls.values() if isinstance(v, dict) and v.get("cmd")]
    s["pending_cmds"] = [v["cmd"] for v in pending][-3:]
    s["pending_cmd"] = s["pending_cmds"][-1] if s["pending_cmds"] else ""
    s["pending_long"] = bool(_LONG_CMD_RE.search(s["pending_cmd"] or s["last_cmd"] or ""))
    s["files"] = sorted(files)[:12]
    s["events"] = s["events"][-12:]
    s["outputs"] = s["outputs"][-5:]
    s["errors"] = s["errors"][-5:]
    return s


def all_sessions(limit=SESSION_LIMIT):
    try:
        files = glob.glob(os.path.join(SESS, "**", "*.jsonl"), recursive=True)
    except Exception:
        files = []
    files = sorted(files, key=lambda f: os.path.getmtime(f), reverse=True)[:limit]
    out = []
    for f in files:
        try:
            st = os.stat(f)
        except OSError:
            continue
        ent = _PARSE_CACHE.get(f)
        if ent and ent[0] == st.st_mtime and ent[1] == st.st_size:
            out.append(ent[2]); continue          # unchanged → reuse cached summary
        s = parse_session(f)
        if s is None:
            continue
        _PARSE_CACHE[f] = (st.st_mtime, st.st_size, s)
        out.append(s)
    if len(_PARSE_CACHE) > 320:                    # evict entries no longer in window
        keep = set(files)
        for k in [k for k in _PARSE_CACHE if k not in keep]:
            _PARSE_CACHE.pop(k, None)
    return out


def _stage(s, age_sec):
    if not s:
        return "starting"
    if age_sec is not None and age_sec > ACTIVE_STALE_SEC and not s.get("pending_long"):
        return "idle"
    for e in reversed(s.get("events", [])):
        k, t = e.get("k"), (e.get("t") or "").lower()
        if k == "edit":
            return "editing"
        if k == "err":
            return "verifying"
        if k == "cmd":
            if re.search(r"\b(test|pytest|lint|build|check|py_compile|tsc|vitest)\b", t):
                return "verifying"
            if re.search(r"\b(rg|sed|cat|ls|find|git show|git status|nl|wc)\b", t):
                return "reading"
            return "analyzing"
        if k == "msg":
            return "analyzing"
    return "idle"


def _last_event_age(s, now):
    if not s:
        return None
    epoch = s.get("last_event_epoch")
    if not epoch:
        return None
    return max(0, int(now - epoch))


def _activity_meta(s, pid_alive, now):
    age_sec = _last_event_age(s, now)
    pending_cmd = (s or {}).get("pending_cmd", "") if s else ""
    last_cmd = (s or {}).get("last_cmd", "") if s else ""
    long_op = bool(s and (s.get("pending_long") or _LONG_CMD_RE.search(pending_cmd or last_cmd or "")))
    has_progress_signal = bool(long_op or pending_cmd)
    meta = {
        "status": "running", "confidence": 0, "label": "live",
        "reason": "recent session event", "pid_alive": bool(pid_alive),
        "last_event_age_sec": age_sec, "long_op": long_op,
        "pending_cmd": pending_cmd, "stale_sec": ACTIVE_STALE_SEC,
    }
    if not pid_alive:
        meta.update(status="zombie", confidence=95, label="pid gone", reason="process no longer responds")
        return meta
    if not s:
        meta.update(confidence=15, label="session pending", reason="process is alive but no session log is matched yet")
        return meta
    if age_sec is None:
        meta.update(confidence=25, label="no events", reason="session log has no timestamped events yet")
        return meta
    if age_sec <= ACTIVE_STALE_SEC:
        meta.update(confidence=5, label="live", reason="last event is fresh")
        return meta
    if long_op and age_sec <= LONG_OP_GRACE_SEC:
        meta.update(confidence=35, label="quiet long cmd", reason="PID is alive and the last/pending command looks long-running")
        return meta
    if long_op and age_sec <= LONG_OP_GRACE_SEC * 2:
        meta.update(confidence=55, label="long cmd watch", reason="long-running command is quiet beyond the first grace window")
        return meta
    if has_progress_signal:
        meta.update(confidence=45, label="idle", reason="PID is alive and a command appears to be in progress")
        return meta
    confidence = 85 if age_sec >= ACTIVE_STALE_SEC * 4 else 65
    meta.update(status="zombie", confidence=confidence, label="silent", reason="PID is alive but session events are stale")
    return meta


def _status_for_session(s, has_pid, pid_alive=True, now=None):
    now = now or time.time()
    if has_pid:
        meta = _activity_meta(s, pid_alive, now)
        return meta["status"], meta["last_event_age_sec"], meta
    age_sec = None if not s else max(0, int(now - s.get("last_ts", now)))
    meta = {"status": "done", "confidence": 0, "label": "done", "reason": "no live process",
            "pid_alive": False, "last_event_age_sec": _last_event_age(s, now), "long_op": False,
            "pending_cmd": "", "stale_sec": ACTIVE_STALE_SEC}
    if s and s.get("errors"):
        meta.update(status="error", label="error", confidence=100, reason="session recorded an error")
        return "error", age_sec, meta
    return "done", age_sec, meta


def _session_key(s):
    return os.path.basename(s.get("file", "")) or str(id(s))


def _load_track_state():
    global _TRACK_STATE
    if _TRACK_STATE is not None:
        return _TRACK_STATE
    try:
        with open(TRACK_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        data = {}
    jobs = data.get("jobs") if isinstance(data, dict) else None
    _TRACK_STATE = {"jobs": jobs if isinstance(jobs, dict) else {}}
    return _TRACK_STATE


def _save_track_state(state):
    try:
        os.makedirs(os.path.dirname(TRACK_PATH), exist_ok=True)
        tmp = TRACK_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, sort_keys=True)
        os.replace(tmp, TRACK_PATH)
    except Exception:
        pass


def _track_key(out):
    return _normalize_out(out) or ""


def _output_has_message(out):
    path = _normalize_out(out)
    if not path:
        return False
    try:
        return os.path.exists(path) and os.path.getsize(path) > 0
    except Exception:
        return False


def _remember_jobs(jobs, now, state=None):
    state = state or _load_track_state()
    changed = False
    for j in jobs:
        out = _track_key(j.get("out"))
        if not out:
            continue
        s = j.get("session")
        rec = dict(state["jobs"].get(out) or {})
        rec.update({
            "out": out, "pid": str(j.get("pid") or ""), "cwd": j.get("cwd_raw") or "",
            "cwd_short": j.get("cwd") or "", "sandbox": j.get("sandbox") or "",
            "prompt": (s or {}).get("prompt") or j.get("plabel") or rec.get("prompt", ""),
            "last_seen": now,
        })
        rec.setdefault("first_seen", now)
        if s:
            rec["session_file"] = s.get("file", "")
            rec["session_key"] = _session_key(s)
        state["jobs"][out] = rec
        changed = True
    cutoff = now - TRACK_TTL_SEC
    for key, rec in list(state["jobs"].items()):
        if float(rec.get("last_seen") or rec.get("kill_requested_at") or 0) < cutoff:
            state["jobs"].pop(key, None)
            changed = True
    if changed:
        _save_track_state(state)


def _track_for_session(s, state=None):
    if not s:
        return None
    state = state or _load_track_state()
    skey, sfile = _session_key(s), s.get("file", "")
    fallback = None
    sprompt = (s.get("prompt") or "").lower()
    for rec in state.get("jobs", {}).values():
        if rec.get("session_file") == sfile or rec.get("session_key") == skey:
            return rec
        rprompt = (rec.get("prompt") or "").lower()
        same_cwd = rec.get("cwd") and rec.get("cwd") == s.get("cwd")
        prompt_match = rprompt and (sprompt.startswith(rprompt[:60]) or rprompt[:60] in sprompt[:240])
        if same_cwd and prompt_match:
            fallback = rec
    return fallback


def _tracked_termination(rec):
    if not rec or not rec.get("out"):
        return None
    if rec.get("killed_by_dashboard"):
        return {"status": "killed", "status_label": "killed (dashboard)",
                "reason": "terminated by dashboard /api/kill", "out": rec.get("out"), "dashboard": True}
    if _output_has_message(rec.get("out")):
        return {"status": "done", "status_label": "completed",
                "reason": "output-last-message exists and is non-empty", "out": rec.get("out"), "dashboard": False}
    return {"status": "interrupted", "status_label": "killed/interrupted",
            "reason": "output-last-message is missing or empty", "out": rec.get("out"), "dashboard": False}


def _apply_tracked_termination(status, meta, rec):
    term = _tracked_termination(rec)
    if not term:
        meta.setdefault("status_label", meta.get("label") or status)
        return status, meta
    meta["termination"] = term
    if term["status"] in ("killed", "interrupted"):
        meta.update(status=term["status"], label=term["status_label"],
                    status_label=term["status_label"], confidence=100, reason=term["reason"])
        return term["status"], meta
    if status == "done":
        meta.update(label=term["status_label"], status_label=term["status_label"], reason=term["reason"])
    else:
        meta.setdefault("status_label", meta.get("label") or status)
    return status, meta


def _mark_dashboard_kill(pid, out, job=None):
    state = _load_track_state()
    now = time.time()
    key = _track_key(out)
    if not key and job:
        key = _track_key(job.get("out"))
    if not key:
        key = f"pid:{pid}"
    rec = dict(state["jobs"].get(key) or {})
    if job:
        rec.update({"out": job.get("out") or rec.get("out") or "", "cwd": job.get("cwd_raw") or rec.get("cwd", ""),
                    "cwd_short": job.get("cwd") or rec.get("cwd_short", ""), "sandbox": job.get("sandbox") or rec.get("sandbox", ""),
                    "prompt": job.get("plabel") or rec.get("prompt", "")})
    rec.update({"pid": str(pid or rec.get("pid") or ""), "killed_by_dashboard": True,
                "kill_requested_at": now, "last_seen": now})
    rec.setdefault("first_seen", now)
    state["jobs"][key] = rec
    _save_track_state(state)


def _job_payload(j, s, now):
    status, age_sec, activity = _status_for_session(s, True, j.get("alive", True), now)
    tokens = (s or {}).get("tokens", {})
    return {
        "key": _session_key(s) if s else f"pid-{j['pid']}",
        "pid": j["pid"], "elapsed": j["elapsed"], "cwd": j["cwd"], "cwd_raw": j["cwd_raw"],
        "sandbox": j["sandbox"], "out": j.get("out"), "status": status,
        "status_label": activity.get("status_label") or status,
        "pid_alive": j.get("alive", True), "activity": activity,
        "termination": activity.get("termination"),
        "stage": _stage(s, age_sec), "last_age_sec": age_sec,
        "prompt": (s or {}).get("prompt", j.get("plabel", "")),
        "n_cmds": (s or {}).get("n_cmds", 0), "n_edits": (s or {}).get("n_edits", 0),
        "files": (s or {}).get("files", []), "last_cmd": (s or {}).get("last_cmd", ""),
        "pending_cmd": (s or {}).get("pending_cmd", ""),
        "last_msg": (s or {}).get("last_msg", ""), "rate_pct": (s or {}).get("rate_pct"),
        "events": (s or {}).get("events", []), "outputs": (s or {}).get("outputs", []),
        "errors": (s or {}).get("errors", []), "tokens": tokens,
        "token_total": tokens.get("total", 0), "cost": tokens.get("cost", 0.0),
    }


def snapshot():
    now = time.time()
    jobs = running_jobs()
    sessions = all_sessions(SESSION_LIMIT)
    # enrich running jobs with their live session: match each job to the session whose
    # prompt matches this dispatch (so parallel agents in the same dir map to the RIGHT one)
    used = set()
    for j in jobs:
        cand = [s for s in sessions if s["cwd"] == j["cwd_raw"]]
        match = None
        pl = (j.get("plabel") or "")[:60].lower()
        if pl:
            for s in cand:
                if id(s) in used:
                    continue
                sp = (s.get("prompt") or "").lower()
                if sp[:60] == pl or sp.startswith(pl[:30]) or pl[:30] in sp[:240]:
                    match = s; break
        if match is None:
            match = next((s for s in cand if id(s) not in used), cand[0] if cand else None)
        if match is not None:
            used.add(id(match))
        j["session"] = match
    track_state = _load_track_state()
    _remember_jobs(jobs, now, track_state)
    running = [_job_payload(j, j.get("session"), now) for j in jobs]

    feed = []
    for s in sessions:
        age = now - s["last_ts"]
        if age < FEED_WINDOW_SEC:
            tag = _short(s["cwd"]).split("/")[-1] or "?"
            for e in s["events"]:
                feed.append({**e, "src": tag})
    feed.sort(key=lambda e: e.get("ts") or "", reverse=True)
    feed = feed[:80]

    recent = []
    running_session_ids = {id(j.get("session")) for j in jobs if j.get("session")}
    for s in sessions:
        if id(s) in running_session_ids:
            continue
        status, age_sec, activity = _status_for_session(s, False, False, now)
        status, activity = _apply_tracked_termination(status, activity, _track_for_session(s, track_state))
        recent.append({
            "key": _session_key(s), "status": status, "status_label": activity.get("status_label") or status,
            "termination": activity.get("termination"), "stage": _stage(s, age_sec),
            "age_min": round((age_sec or 0) / 60, 1), "last_age_sec": age_sec,
            "cwd": _short(s["cwd"]).split("/")[-1] or "session", "cwd_raw": s["cwd"],
            "prompt": (s["prompt"] or s["last_msg"] or "(session)")[:500],
            "n_cmds": s["n_cmds"], "n_edits": s["n_edits"], "files": s["files"][:4],
            "rate_pct": s["rate_pct"], "tokens": s["tokens"], "token_total": s["tokens"].get("total", 0),
            "cost": s["tokens"].get("cost", 0.0), "errors": s["errors"][:2],
        })
    recent = recent[:RECENT_LIMIT]

    today = time.strftime("%Y/%m/%d")
    try:
        today_n = len(glob.glob(os.path.join(SESS, today, "*.jsonl")))
    except Exception:
        today_n = len(sessions)
    max_rate = max([s["rate_pct"] for s in sessions if s["rate_pct"] is not None] + [0])
    token_total = sum((s.get("tokens") or {}).get("total", 0) for s in sessions)
    cost_total = round(sum((s.get("tokens") or {}).get("cost", 0.0) for s in sessions), 4)
    status_counts = {k: sum(1 for j in running if j["status"] == k) for k in ("running", "zombie", "error", "done", "killed", "interrupted")}
    return {"ts": time.strftime("%H:%M:%S"), "date": time.strftime("%a %d %b %Y").upper(),
            "count": len(running), "today": today_n, "rate": max_rate,
            "stale_sec": ACTIVE_STALE_SEC, "status_counts": status_counts,
            "token_total": token_total, "cost_total": cost_total,
            "running": running, "feed": feed, "recent": recent}


def _json_body(handler):
    try:
        n = int(handler.headers.get("Content-Length", "0"))
    except Exception:
        n = 0
    if n <= 0:
        return {}
    try:
        return json.loads(handler.rfile.read(n).decode("utf-8"))
    except Exception:
        return {}


def _local_client(handler):
    host = handler.client_address[0]
    return host in ("127.0.0.1", "::1", "::ffff:127.0.0.1")


def _find_running_job(pid=None, out=None):
    for j in running_jobs():
        if pid and str(j.get("pid")) == str(pid):
            return j
        if out and j.get("out") == out:
            return j
    return None


def post_kill(payload):
    pid, out = payload.get("pid"), payload.get("out")
    job = _find_running_job(pid=pid, out=out)
    if not job:
        return {"ok": False, "error": "matching codex exec job not found"}, 404
    target_pid = job.get("pid")
    confirm = _find_running_job(pid=target_pid)
    if not confirm:
        return {"ok": False, "error": "target pid is no longer a codex exec job"}, 409
    if job.get("out") and confirm.get("out") != job.get("out"):
        return {"ok": False, "error": "target pid output path changed; refusing to kill"}, 409
    try:
        os.kill(int(target_pid), signal.SIGTERM)
        _mark_dashboard_kill(target_pid, job.get("out"), job)
        return {"ok": True, "method": "pid", "pid": target_pid, "out": job.get("out")}
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500


def post_retry(payload):
    cwd = payload.get("cwd_raw") or payload.get("cwd")
    prompt = (payload.get("prompt") or "").strip()
    sandbox = payload.get("sandbox")
    if not cwd or not prompt:
        return {"ok": False, "error": "cwd and prompt are required"}, 400
    dispatch = shlex.split(os.environ.get("CODEX_MONITOR_DISPATCH", "codex"))
    cmd = dispatch + ["exec", "-C", cwd]
    if sandbox and sandbox != "?":
        cmd += ["-s", sandbox]
    cmd += [prompt]
    try:
        p = subprocess.Popen(cmd, cwd=cwd if os.path.isdir(cwd) else HOME,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        return {"ok": True, "pid": p.pid, "cmd": " ".join(shlex.quote(c) for c in cmd)}
    except Exception as e:
        return {"ok": False, "error": str(e)}, 500


PAGE = r"""<!doctype html><html lang=ko><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>CODEX WIRE</title>
<link rel=preconnect href=https://fonts.googleapis.com>
<link rel=preconnect href=https://fonts.gstatic.com crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Bodoni+Moda:ital,opsz,wght@0,6..96,500;0,6..96,600;1,6..96,500&family=JetBrains+Mono:wght@400;500;700&family=Saira+Condensed:wght@500;600;700&display=swap" rel=stylesheet>
<style>
:root{
  --ink:#f1e7d2; --ink2:#e9dcc1; --panel:#ece0c6;
  --paper:#33271a; --dim:#6b5638; --faint:#9c8765; --line:#d8c7a4;
  --ember:#cf5915; --ember2:#a8431a; --wire:#5c7a2b; --rust:#a8431a;
  --ok:#3f7a2c; --done:#756a5b; --warn:#b7831f; --bad:#a93622; --blue:#356d8f; --violet:#6d548b;
  --kill:#5c3f6f; --interrupt:#8a3d2b;
}
*{box-sizing:border-box;margin:0;padding:0}
html{scroll-behavior:smooth}
body{
  background:var(--ink); color:var(--paper);
  font-family:"JetBrains Mono",monospace; font-size:13px; line-height:1.5;
  padding:30px clamp(14px,4.5vw,70px) 80px; min-height:100vh; position:relative; overflow-x:hidden;
}

.mast{display:flex;align-items:flex-end;justify-content:space-between;gap:18px;flex-wrap:wrap;
  border-bottom:3px double #b79b6a;padding-bottom:16px;position:relative}
.mast:after{content:"";position:absolute;left:0;right:0;bottom:-6px;height:1px;background:#cbb588}
.brand{display:flex;align-items:baseline;gap:16px}
.brand h1{font-family:"Bodoni Moda",serif;font-style:italic;font-weight:600;
  font-size:clamp(34px,6vw,62px);letter-spacing:0;line-height:.9;color:#2a1d10;
  text-shadow:0 1px 0 rgba(255,250,238,.75)}
.brand h1 b{font-style:normal;color:var(--ember)}
.brand .sub{font-family:"Saira Condensed",sans-serif;font-weight:600;text-transform:uppercase;
  letter-spacing:.42em;font-size:11px;color:var(--dim);padding-bottom:6px}
.dateline{text-align:right;font-family:"Saira Condensed",sans-serif;letter-spacing:.18em;
  text-transform:uppercase;color:var(--dim);font-size:12px;line-height:1.7}
.dateline b{color:var(--paper)}

.strip{display:flex;flex-wrap:wrap;gap:0;margin:20px 0 8px;border:1px solid #cbb588;
  background:linear-gradient(180deg,#ede1c8,#e4d6b8)}
.stat{flex:1;min-width:120px;padding:12px 18px;border-right:1px solid #d3c096;position:relative}
.stat:last-child{border-right:0}
.stat .l{font-family:"Saira Condensed",sans-serif;text-transform:uppercase;letter-spacing:.24em;
  font-size:10px;color:var(--dim);margin-bottom:5px}
.stat .v{font-family:"Bodoni Moda",serif;font-size:30px;font-weight:600;font-variant-numeric:tabular-nums;line-height:1;color:#2a1d10}
.stat .v small{font-size:13px;color:var(--dim);font-family:"JetBrains Mono",monospace}
.onair{display:inline-flex;align-items:center;gap:9px}
.lamp{width:11px;height:11px;border-radius:50%;background:#c3ad81}
.lamp.on{background:var(--ember);animation:pulse 1.5s infinite}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(207,89,21,.55)}70%{box-shadow:0 0 0 11px rgba(207,89,21,0)}100%{box-shadow:0 0 0 0 rgba(207,89,21,0)}}
.gauge{height:5px;background:#ddcba8;margin-top:9px;overflow:hidden}
.gauge i{display:block;height:100%;background:linear-gradient(90deg,var(--ember),#e8893f);transition:width .6s}

.controls{display:flex;flex-wrap:wrap;gap:9px;align-items:center;margin:16px 0 8px;padding:10px;border:1px solid #cfbc91;background:#f8f1e2}
.controls label{font-family:"Saira Condensed",sans-serif;text-transform:uppercase;letter-spacing:.18em;color:var(--dim);font-size:10px}
.controls select,.controls input,.controls button{height:30px;border:1px solid #c9b382;background:#fff8e9;color:#33271a;font:12px "JetBrains Mono",monospace;padding:0 9px}
.controls input{min-width:220px;flex:1}
.controls input.mini{min-width:54px;width:54px;flex:0;padding:0 5px;text-align:center}
.controls button{cursor:pointer;text-transform:uppercase;font-family:"Saira Condensed",sans-serif;font-weight:700;letter-spacing:.12em}
.controls button.on{background:#33271a;color:#f1e7d2}
.notify-panel{display:inline-flex;align-items:center;gap:8px;flex-wrap:wrap;border:1px solid #decaa0;background:#fff8e9;padding:5px 7px}
.notify-panel.muted{opacity:.72}
.notify-sep{width:1px;height:26px;background:#decaa0;margin:0 1px}
.switch{position:relative;height:30px;display:inline-flex;align-items:center;gap:7px;padding:0 5px;cursor:pointer;user-select:none}
.switch input{position:absolute;opacity:0;pointer-events:none;min-width:0;width:1px;height:1px;flex:0}
.switch .track{width:34px;height:18px;border:1px solid #bda775;background:#e5d6b7;display:inline-flex;align-items:center;padding:2px;transition:background .18s,border-color .18s}
.switch .thumb{width:12px;height:12px;background:#8d7a55;box-shadow:0 1px 1px rgba(45,32,18,.22);transition:transform .18s,background .18s}
.switch input:checked+.track{background:#33271a;border-color:#33271a}
.switch input:checked+.track .thumb{transform:translateX(16px);background:var(--ember)}
.switch input:focus-visible+.track{outline:2px solid var(--ember);outline-offset:2px}
.switch-text{display:inline-flex;flex-direction:column;gap:1px;line-height:1}
.switch-text b{font-size:10px;color:#4a3925;letter-spacing:.12em}
.switch-text small{font:9px "JetBrains Mono",monospace;color:var(--faint);letter-spacing:.04em;text-transform:none}
.switch.master{border-right:1px solid #decaa0;padding-right:10px;margin-right:1px}
.switch.master .track{width:40px}
.switch.master input:checked+.track .thumb{transform:translateX(22px)}
.threshold{height:30px;display:inline-flex;align-items:center;gap:4px;color:var(--dim);font:10px "JetBrains Mono",monospace;letter-spacing:.04em;text-transform:none}
.refresh-note{margin-left:auto;color:var(--faint);font-size:10.5px}

.sec{display:flex;align-items:center;gap:14px;margin:34px 0 16px}
.sec h2{font-family:"Bodoni Moda",serif;font-style:italic;font-weight:500;font-size:21px;color:#2a1d10;white-space:nowrap}
.sec .ko{font-family:"Saira Condensed",sans-serif;letter-spacing:.3em;text-transform:uppercase;font-size:11px;color:var(--ember)}
.sec .rule{flex:1;height:1px;background:repeating-linear-gradient(90deg,#cbb588 0 6px,transparent 6px 10px)}

.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,380px),1fr));gap:16px}
.card{border:1px solid #cdb98f;background:linear-gradient(180deg,#fcf7ec,#f5eedc);position:relative;overflow:hidden;
  box-shadow:0 2px 12px rgba(120,84,38,.12)}
.card:before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--done)}
.card.running:before{background:var(--ok)} .card.zombie:before{background:var(--warn)} .card.error:before{background:var(--bad)}
.card.killed:before{background:var(--kill)} .card.interrupted:before{background:var(--interrupt)}
.card .hd{display:flex;align-items:center;gap:9px;flex-wrap:wrap;padding:13px 16px;border-bottom:1px solid #e6d6b2}
.pill{font-family:"Saira Condensed",sans-serif;font-weight:700;letter-spacing:.16em;font-size:11px;
  color:#fdf3df;background:var(--done);padding:3px 9px;display:inline-flex;align-items:center;gap:6px}
.pill.running{background:var(--ok)} .pill.zombie{background:var(--warn);color:#2a1d10} .pill.error{background:var(--bad)}
.pill.killed{background:var(--kill)} .pill.interrupted{background:var(--interrupt)}
.pill .d{width:6px;height:6px;border-radius:50%;background:#fdf3df}
.pill.running .d{animation:blink 1s infinite}.pill.zombie .d{background:#2a1d10;animation:blink .45s infinite}
.pill.killed .d,.pill.interrupted .d{animation:blink .75s infinite}
@keyframes blink{50%{opacity:.25}}
.kv{font-size:11px;color:var(--dim)} .kv b{color:var(--paper);font-weight:600}
.chip{font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:#4c6a1d;border:1px solid #bcd08e;padding:2px 7px;background:#edf2da}
.stage{font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:#2e5d78;border:1px solid #b6cfdb;padding:2px 7px;background:#e5f0f2}
.signal{font-size:10px;letter-spacing:.08em;text-transform:uppercase;color:#5f513d;border:1px solid #d4c19b;padding:2px 7px;background:#f8efd9}
.signal.med{color:#7d5b12;border-color:#d8bb72;background:#fbf0cb}.signal.high{color:#8f2b1e;border-color:#d99b8d;background:#fae4dc}
.errbadges{display:flex;gap:5px;flex-wrap:wrap}.errbadge{font-size:10px;letter-spacing:.09em;text-transform:uppercase;border:1px solid #d8c7a4;padding:2px 6px;background:#fff8e9;color:#5a4631}
.errbadge.exit{color:var(--bad);border-color:#d9a193;background:#fae4dc}.errbadge.timeout{color:#805100;border-color:#d6b65e;background:#fbefc3}
.errbadge.permission,.errbadge.sandbox{color:#683f84;border-color:#c4abd9;background:#efe6f7}.errbadge.network{color:#24627f;border-color:#9bc5d8;background:#e3f0f4}.errbadge.jsonl{color:#8d3f13;border-color:#dfa16c;background:#f7e5d4}
.age.zombie,.age.error,.age.killed,.age.interrupted{color:var(--bad);font-weight:700}
.el{margin-left:auto;font-variant-numeric:tabular-nums;color:var(--ember2);font-weight:700;font-size:15px}
.acts{display:flex;gap:5px}
.acts button,.toggle-more{border:1px solid #d8c7a4;background:#fff8e9;color:#5a4631;font:10px "JetBrains Mono",monospace;padding:3px 6px;cursor:pointer}
.acts button.kill{color:var(--bad);border-color:#d9a193}.acts button.pinbtn.on{background:#33271a;color:#f1e7d2;border-color:#33271a}
.card .bd{padding:13px 16px}
.prompt{color:#5a4631;white-space:pre-wrap;word-break:break-word;font-size:11.5px;
  border-left:2px solid var(--ember);padding:2px 0 2px 11px;margin-bottom:7px;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.prompt.open{display:block;max-height:none;overflow:visible}
.telem{display:flex;gap:16px;flex-wrap:wrap;align-items:center;margin:10px 0}
.telem .m{font-size:11px;color:var(--dim)} .telem .m b{font-family:"Bodoni Moda",serif;font-size:20px;color:#2a1d10;margin-right:4px;font-variant-numeric:tabular-nums}
.files{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:10px}
.fchip{font-size:10px;color:#9a3f12;background:#f6e6cb;border:1px solid #e6cda0;padding:2px 7px}
.minifeed{border-top:1px solid #e6d6b2;padding-top:9px;font-size:11px;color:var(--dim)}
.minifeed .ev{display:flex;gap:8px;padding:2px 0;align-items:baseline}
.ic{font-style:normal;width:13px;flex:none;text-align:center}
.ic.cmd{color:var(--wire)} .ic.edit{color:var(--ember2)} .ic.msg{color:var(--dim)} .ic.err{color:var(--bad)} .ic.out{color:var(--faint)}
.minifeed .tx{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#5a4631}
.msgbox{margin-top:10px;font-style:italic;font-family:"Bodoni Moda",serif;font-size:13px;color:#5a4028;
  border-top:1px dashed #d8c7a4;padding-top:9px;line-height:1.45}
.details{display:none;margin-top:10px;border-top:1px solid #e6d6b2;padding-top:10px}
.details.open{display:block}
.details h3{font:700 10px "Saira Condensed",sans-serif;letter-spacing:.18em;text-transform:uppercase;color:var(--dim);margin:8px 0 4px}
.details pre{white-space:pre-wrap;word-break:break-word;max-height:220px;overflow:auto;background:#2f2519;color:#f5ead2;padding:9px;border:1px solid #b99d6e;font-size:10.5px}
.details pre.err{border-color:#bd6d58;color:#ffe0d8}

.wire{border:1px solid #cdb98f;background:#fcf7ec;max-height:340px;overflow:auto;
  box-shadow:0 2px 12px rgba(120,84,38,.10)}
.wline{display:flex;gap:12px;align-items:baseline;padding:7px 16px;border-bottom:1px solid #ecddc1;
  animation:rise .4s ease}
@keyframes rise{from{opacity:0;transform:translateY(-4px)}to{opacity:1}}
.wline:nth-child(even){background:#f8f1e2}
.wline .wt{color:var(--faint);font-size:10.5px;min-width:64px;font-variant-numeric:tabular-nums}
.wline .src{color:var(--ember);font-size:10px;text-transform:uppercase;letter-spacing:.1em;min-width:88px;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.wline .wx{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#3a2c1c;font-size:11.5px}
.wline.cmd .wi{color:var(--wire)} .wline.edit .wi{color:var(--ember2)} .wline.msg .wi{color:var(--blue);font-style:italic}
.wline.err{background:#fae5dc}.wline.err .wi,.wline.err .wx{color:var(--bad);font-weight:700}.wline.out{opacity:.55}
.wi{width:14px;flex:none;text-align:center}

.rec{display:flex;gap:14px;align-items:baseline;padding:10px 0;border-bottom:1px solid #ddcba8}
.rec .ago{color:var(--faint);min-width:58px;text-align:right;font-variant-numeric:tabular-nums;font-size:11px}
.rec .src{color:var(--ember);font-size:10px;text-transform:uppercase;letter-spacing:.12em;min-width:84px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.rec .p{flex:1;color:#4a3a26;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px}
.rec .n{color:var(--dim);font-size:10.5px;white-space:nowrap}
.rec.error .p,.rec.error .n{color:var(--bad)}
.rec.killed .p,.rec.killed .n{color:var(--kill);font-weight:700}.rec.interrupted .p,.rec.interrupted .n{color:var(--interrupt);font-weight:700}
.empty{color:var(--faint);font-style:italic;font-family:"Bodoni Moda",serif;padding:22px 4px;font-size:15px}

body.compact .grid{display:flex;flex-direction:column;gap:7px}
body.compact .card .hd{padding:8px 12px}
body.compact .card .bd{display:none}
body.compact .pill{font-size:10px;padding:2px 7px}
body.compact .kv.prompt-head{display:inline;max-width:38vw;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

.toast-stack{position:fixed;right:18px;bottom:18px;z-index:50;display:flex;flex-direction:column;align-items:flex-end;gap:8px;pointer-events:none}
.toast{min-width:168px;max-width:min(340px,calc(100vw - 36px));padding:9px 12px;border:1px solid #5a4631;background:#261d14;color:#f1e7d2;
  box-shadow:0 10px 30px rgba(34,24,13,.28);font:700 11px "JetBrains Mono",monospace;letter-spacing:.02em;
  transform:translateY(8px);opacity:0;transition:opacity .18s ease,transform .18s ease;border-left:3px solid var(--ok)}
.toast.show{opacity:1;transform:translateY(0)}
.toast.hide{opacity:0;transform:translateY(8px)}
.toast.bad{border-left-color:var(--bad);color:#ffd9d0}
.toast .msg{display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

footer{margin-top:40px;color:var(--faint);font-size:10.5px;border-top:1px solid #cbb588;padding-top:14px;
  font-family:"Saira Condensed",sans-serif;letter-spacing:.16em;text-transform:uppercase;display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap}
::-webkit-scrollbar{width:7px;height:7px}::-webkit-scrollbar-thumb{background:#cdb98f}::-webkit-scrollbar-track{background:transparent}
@media(max-width:640px){
  body{padding-inline:12px}.brand{display:block}.dateline{text-align:left}.controls input{min-width:100%}.controls input.mini{min-width:54px;width:54px;flex:0}.controls .switch input{min-width:0;width:1px;height:1px;flex:0}.refresh-note{margin-left:0;width:100%}
  .stat{min-width:50%;border-bottom:1px solid #d3c096}.rec{gap:8px}.rec .src{min-width:64px}.acts{width:100%}.el{margin-left:0}
}
</style></head><body>

<div class=mast>
  <div class=brand>
    <h1>Codex<b>·</b>Wire</h1>
    <span class=sub>Subagent telemetry</span>
  </div>
  <div class=dateline><span class=onair><span id=lamp class=lamp></span> <b id=onair>OFF AIR</b></span><br><span id=date>—</span> · <b id=ts>—</b><br><span id=refresh_state>polling</span></div>
</div>

<div class=strip>
  <div class=stat><div class=l>Running</div><div class=v id=s_run>—</div></div>
  <div class=stat><div class=l>Today</div><div class=v id=s_today>—</div></div>
  <div class=stat><div class=l>Rate 5h</div><div class=v id=s_rate>—<small>%</small></div><div class=gauge><i id=s_rate_bar style=width:0%></i></div></div>
  <div class=stat><div class=l>Cost</div><div class=v id=s_tokens>—</div></div>
  <div class=stat><div class=l>Wire lines</div><div class=v id=s_feed>—</div></div>
</div>

<div class=controls>
  <label>dir</label><select id=f_dir><option value="">all</option></select>
  <label>sandbox</label><select id=f_sandbox><option value="">all</option></select>
  <label>status</label><select id=f_status><option value="">all</option><option>running</option><option>zombie</option><option>error</option><option>done</option><option>killed</option><option>interrupted</option></select>
  <label>sort</label><select id=f_sort><option value=elapsed>elapsed</option><option value=edits>edits</option><option value=tokens>tokens</option><option value=activity>last activity</option></select>
  <input id=f_q placeholder="prompt · file · pid search">
  <label>poll</label><select id=poll_ms><option value=1000>1s</option><option value=2000 selected>2s</option><option value=5000>5s</option><option value=10000>10s</option></select>
  <button id=refresh_btn>refresh</button><button id=compact_btn>compact</button><button id=clear_state_btn>clear all</button>
  <div class=notify-panel id=notify_panel aria-label="notification controls">
    <label class="switch master"><input id=n_master type=checkbox><span class=track><span class=thumb></span></span><span class=switch-text><b>alerts</b><small>master</small></span></label>
    <label class=switch><input id=n_zombie type=checkbox><span class=track><span class=thumb></span></span><span class=switch-text><b>zombie</b><small>status</small></span></label>
    <label class=switch><input id=n_error type=checkbox><span class=track><span class=thumb></span></span><span class=switch-text><b>error</b><small>logs</small></span></label>
    <label class=switch><input id=n_rate type=checkbox><span class=track><span class=thumb></span></span><span class=switch-text><b>rate</b><small>limit</small></span></label>
    <label class=threshold title="rate alert percent"><span>at</span><input id=n_rate_limit class=mini type=number min=50 max=100 value=80><span>%</span></label>
    <span class=notify-sep></span>
    <label class=switch><input id=n_idle type=checkbox><span class=track><span class=thumb></span></span><span class=switch-text><b>idle</b><small>quiet</small></span></label>
    <label class=threshold title="idle alert minutes"><span>after</span><input id=n_idle_min class=mini type=number min=1 max=180 value=10><span>m</span></label>
  </div>
  <span class=refresh-note id=refresh_note>—</span>
</div>

<div class=sec><span class=ko>● Running</span><h2>On the Wire</h2><div class=rule></div></div>
<div class=grid id=running></div>

<div class=sec><span class=ko>Wire feed</span><h2>Live Telegraph</h2><div class=rule></div></div>
<div class=wire id=wire></div>

<div class=sec><span class=ko>Logbook</span><h2>Recent Dispatches</h2><div class=rule></div></div>
<div id=recent></div>

<footer><span>CODEX WIRE · ps + ~/.codex/sessions · controlled polling</span><span>by 3917</span></footer>
<div class=toast-stack id=toasts aria-live=polite aria-atomic=false></div>

<script>
const ICON={cmd:'●',edit:'✎',msg:'»',err:'!',out:'·'};
const STATUS_ICON={running:'●',zombie:'!',error:'×',done:'✓',killed:'■',interrupted:'!'};
function esc(s){return String(s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function hhmm(iso){try{const d=new Date(iso);return d.toLocaleTimeString('en-GB',{hour12:false});}catch(e){return '';}}
function fmtAge(s){if(s==null)return 'no events'; if(s<60)return s+'s ago'; const m=Math.floor(s/60); if(m<60)return m+'m ago'; return Math.floor(m/60)+'h '+(m%60)+'m ago';}
function fmtRecentAge(m){m=Math.floor(Number(m)||0); const mm=String(m%60).padStart(2,'0'); if(m>=1440)return Math.floor(m/1440)+'d '+Math.floor((m%1440)/60)+'h '+mm+'m'; if(m>=60)return Math.floor(m/60)+'h '+mm+'m'; return m+'m';}
function fmtTok(n){n=Number(n||0); if(n>=1000000)return (n/1000000).toFixed(1)+'M'; if(n>=1000)return Math.round(n/100)/10+'k'; return String(n);}
function setText(el,v){if(el&&el.textContent!==String(v??''))el.textContent=String(v??'');}
function setHTML(el,v){if(el&&el.innerHTML!==v)el.innerHTML=v;}
function shortErr(e){
  return String((e&&e.message)||e||'unknown error').replace(/\s+/g,' ').slice(0,90);
}
function toast(msg, kind='ok'){
  const stack=document.getElementById('toasts'); if(!stack)return;
  const el=document.createElement('div'); el.className='toast '+(kind==='bad'?'bad':'ok'); el.setAttribute('role','status');
  el.innerHTML=`<span class=msg>${esc(msg)}</span>`; stack.appendChild(el);
  requestAnimationFrame(()=>el.classList.add('show'));
  setTimeout(()=>{el.classList.add('hide');el.classList.remove('show');},2100);
  setTimeout(()=>el.remove(),2450);
}
let ticks={}, seen=new Set(), latest=null, pollMs=2000, timer=null, lastOk=0;
let expanded=new Set(), promptOpen=new Set(), pins=new Set();
let orderSeed={}, orderSeq=0, controlState={}, statusSeen={}, errorSeen={}, idleSeen={}, alertsPrimed=false, rateHot=false;
const STORE='codex-wire-state-v2';
const NOTIFY_STORE='codex-wire-notify-v3';
const NOTIFY_DEFAULTS={master:false,zombie:false,error:false,rate:false,rateLimit:80,idle:false,idleMin:10};

function stableId(j){return String((j&&j.out)||(j&&j.key)||(j&&j.pid)||'');}
function cardKey(j){return 'job_'+stableId(j).replace(/[^a-zA-Z0-9_-]/g,'_');}
function orderOf(j){const k=stableId(j); if(orderSeed[k]==null)orderSeed[k]=++orderSeq; return orderSeed[k];}
function controlValue(id){return Object.prototype.hasOwnProperty.call(controlState,id)?controlState[id]:document.getElementById(id).value;}
function textIndex(j){return [j.pid,j.cwd,j.cwd_raw,j.sandbox,j.status,j.status_label,j.stage,j.prompt,j.last_cmd,j.pending_cmd,(j.activity||{}).label,(j.errors||[]).map(e=>e.label).join(' '),(j.files||[]).join(' ')].join(' ').toLowerCase();}
function sortScore(j,sort){
  if(sort==='edits')return -(j.n_edits||0);
  if(sort==='tokens')return -(j.token_total||0);
  if(sort==='activity')return (j.last_age_sec??1e9);
  return -parseElapsed(j.elapsed);
}
function filteredRunning(){
  if(!latest)return [];
  const dir=controlValue('f_dir'), sand=controlValue('f_sandbox');
  const st=controlValue('f_status'), q=controlValue('f_q').trim().toLowerCase();
  let out=latest.running.filter(j=>(!dir||j.cwd===dir)&&(!sand||j.sandbox===sand)&&(!st||j.status===st)&&(!q||textIndex(j).includes(q)));
  const sort=controlValue('f_sort'); out.forEach(orderOf);
  out.sort((a,b)=>{
    const pa=pins.has(stableId(a)), pb=pins.has(stableId(b));
    if(pa!==pb)return pa?-1:1;
    const c=sortScore(a,sort)-sortScore(b,sort);
    if(c)return c;
    return orderOf(a)-orderOf(b) || stableId(a).localeCompare(stableId(b));
  });
  return out;
}
function parseElapsed(s){let p=String(s||'0').split(/[:-]/).map(Number);let sec=0;for(const n of p){sec=sec*60+(n||0)}return sec}
function updateSelect(id, values){
  const el=document.getElementById(id), old=controlValue(id);
  if(old&&!values.includes(old))values=[old,...values];
  const html='<option value="">all</option>'+values.map(v=>`<option value="${esc(v)}">${esc(v)}</option>`).join('');
  setHTML(el,html); el.value=values.includes(old)?old:''; controlState[id]=el.value;
}
function renderControls(d){
  updateSelect('f_dir',[...new Set(d.running.map(j=>j.cwd).filter(Boolean))].sort());
  updateSelect('f_sandbox',[...new Set(d.running.map(j=>j.sandbox).filter(Boolean))].sort());
}
function promptBlock(j){
  const k=stableId(j), open=promptOpen.has(k), p=esc(j.prompt||'');
  return p?`<div class="prompt ${open?'open':''}" data-field=prompt>${p}</div><button class=toggle-more data-act=prompt>${open?'Collapse':'More'}</button>`:'';
}
function telem(j){
  return `<span class=m><b>${j.n_cmds||0}</b>cmds</span><span class=m><b>${j.n_edits||0}</b>edits</span><span class=m><b>${fmtTok(j.token_total)}</b>tok</span><span class=m><b>$${Number(j.cost||0).toFixed(3)}</b>est</span>${j.rate_pct!=null?`<span class=m><b>${Math.round(j.rate_pct)}</b><small style=color:var(--dim)>% rate</small></span>`:''}`;
}
function feedHTML(events){
  return (events||[]).slice().reverse().map(e=>`<div class=ev><span class="ic ${e.k}">${ICON[e.k]||'·'}</span><span class=tx>${esc(e.t)}</span></div>`).join('');
}
function errBadges(j){
  const seenKinds=new Set(), rows=[];
  for(const e of (j.errors||[]).slice().reverse()){
    const kind=e.kind||'error', label=e.label||'ERROR';
    const sig=kind+'|'+label; if(seenKinds.has(sig))continue; seenKinds.add(sig);
    rows.push(`<span class="errbadge ${esc(kind)}">${esc(label)}</span>`); if(rows.length>=3)break;
  }
  return rows.length?`<span class=errbadges>${rows.join('')}</span>`:'';
}
function activityHTML(j){
  const a=j.activity||{}, c=Number(a.confidence||0), cls=c>=80?'high':(c>=50?'med':'low');
  const bits=[`pid ${a.pid_alive?'alive':'gone'}`,`event ${fmtAge(a.last_event_age_sec)}`];
  if(a.long_op)bits.push('long cmd');
  return `<span class="signal ${cls}" title="${esc(a.reason||'')}">${esc(a.label||'live')} · z${c}% · ${esc(bits.join(' · '))}</span>`;
}
function detailsHTML(j){
  const outs=(j.outputs||[]).slice().reverse().map(o=>`<h3>output ${o.exit_code!=null?'exit '+o.exit_code:''}</h3><pre class="${o.exit_code?'err':''}">${esc(o.output)}</pre>`).join('');
  const errs=(j.errors||[]).slice().reverse().map(o=>`<h3><span class="errbadge ${esc(o.kind||'error')}">${esc(o.label||'ERROR')}</span> ${o.exit_code!=null?'exit '+o.exit_code:''}</h3><pre class=err>${esc(o.output)}</pre>`).join('');
  return `<div class="details ${expanded.has(stableId(j))?'open':''}" data-field=details>
    ${j.last_cmd?`<h3>last command</h3><pre>${esc(j.last_cmd)}</pre>`:''}
    ${j.pending_cmd?`<h3>pending command</h3><pre>${esc(j.pending_cmd)}</pre>`:''}
    ${j.last_msg?`<h3>last message</h3><pre>${esc(j.last_msg)}</pre>`:''}
    ${errs||outs||'<h3>detail</h3><pre>No output recorded yet.</pre>'}
  </div>`;
}
function latestJobForCard(el){
  const key=el.dataset.stable;
  return (latest&&latest.running||[]).find(x=>stableId(x)===String(key))||null;
}
function createCard(j){
  const el=document.createElement('div'); el.className='card'; el.id=cardKey(j); el.dataset.stable=stableId(j); el.dataset.key=j.key;
  el.addEventListener('click',ev=>{
    if(ev.target.closest('button'))return;
    const cur=latestJobForCard(el)||j, k=stableId(cur);
    expanded.has(k)?expanded.delete(k):expanded.add(k);
    patchCard(el,cur);
  });
  el.addEventListener('click',ev=>{
    const btn=ev.target.closest('button'); if(!btn)return;
    const cur=latestJobForCard(el)||j, k=stableId(cur), act=btn.dataset.act;
    if(act==='pin'){pins.has(k)?pins.delete(k):pins.add(k);saveState();renderCards();}
    if(act==='prompt'){promptOpen.has(k)?promptOpen.delete(k):promptOpen.add(k);patchCard(el,cur);}
    if(act==='copy')copyCmd(cur);
    if(act==='kill')killJob(cur);
    if(act==='retry')retryJob(cur);
  });
  patchCard(el,j); return el;
}
function patchCard(el,j){
  const k=stableId(j), pinned=pins.has(k);
  el.dataset.key=j.key; el.dataset.stable=k; el.dataset.pid=j.pid||''; el.dataset.out=j.out||'';
  el.className='card '+j.status; ticks[j.pid]=j.elapsed;
  const files=(j.files||[]).map(f=>`<span class=fchip>${esc(f)}</span>`).join('');
  setHTML(el,`<div class=hd>
     <span class="pill ${j.status}"><span class=d></span>${STATUS_ICON[j.status]||'·'} ${esc(j.status_label||j.status)} ${esc(j.pid)}</span>
     <span class=kv>dir <b>${esc(j.cwd)}</b></span>
     <span class="kv prompt-head">${esc((j.prompt||'').replace(/\s+/g,' ').slice(0,90))}</span>
     <span class=chip>${esc(j.sandbox)}</span><span class=stage>${esc(j.stage)}</span>${activityHTML(j)}${errBadges(j)}
     <span class="kv age ${j.status}">last ${fmtAge(j.last_age_sec)}</span>
     <span class=el id=el_${esc(j.pid)}>${esc(j.elapsed)}</span>
     <span class=acts><button class="pinbtn ${pinned?'on':''}" data-act=pin title="pin job">★</button><button data-act=copy>copy cmd</button><button data-act=retry>retry</button><button class=kill data-act=kill>kill</button></span></div>
    <div class=bd>${promptBlock(j)}
     <div class=telem>${telem(j)}</div>
     ${files?`<div class=files>${files}</div>`:''}
     ${(j.events||[]).length?`<div class=minifeed>${feedHTML(j.events)}</div>`:''}
     ${j.last_msg?`<div class=msgbox>“${esc(j.last_msg)}”</div>`:''}
     ${detailsHTML(j)}
    </div>`);
}
function renderCards(){
  const R=document.getElementById('running'), list=filteredRunning(), keep=new Set();
  let empty=R.querySelector('.empty'); if(list.length&&empty)empty.remove();
  if(!list.length){
    [...R.children].forEach(ch=>{if(ch.classList.contains('card'))ch.remove();});
    if(!empty){empty=document.createElement('div');empty.className='empty';empty.textContent='Idle — no Codex jobs match the filter.';R.appendChild(empty);}
    return;
  }
  for(const j of list){
    const id=cardKey(j); keep.add(id);
    let el=document.getElementById(id);
    if(!el){el=createCard(j);} else {patchCard(el,j);}
    R.appendChild(el);
  }
  [...R.children].forEach(ch=>{if(ch.classList.contains('card')&&!keep.has(ch.id))ch.remove();});
}
function renderWire(feed){
  const W=document.getElementById('wire');
  if(!feed.length){ seen.clear(); W.innerHTML='<div class=empty style=padding:16px>Quiet wire.</div>'; return; }
  const ph=W.querySelector('.empty'); if(ph) ph.remove();
  const fresh=feed.filter(e=>{const k=e.ts+'|'+e.k+'|'+e.t+'|'+e.src; if(seen.has(k))return false; seen.add(k); return true;});
  for(let i=fresh.length-1;i>=0;i--){const e=fresh[i];
    const div=document.createElement('div'); div.className='wline '+e.k;
    div.innerHTML=`<span class=wt>${hhmm(e.ts)}</span><span class=wi>${ICON[e.k]||'·'}</span><span class=src>${esc(e.src)}</span><span class=wx>${esc(e.t)}</span>`;
    W.insertBefore(div, W.firstChild);
  }
  while(W.children.length>80) W.removeChild(W.lastChild);
  if(seen.size>600){ seen=new Set(feed.map(e=>e.ts+'|'+e.k+'|'+e.t+'|'+e.src)); }
}
function renderRecent(rows){
  const RE=document.getElementById('recent');
  setHTML(RE,rows.map(s=>`<div class="rec ${s.status}">
     <span class=ago>${fmtRecentAge(s.age_min)}</span><span class=src>${esc(s.cwd)}</span>
     <span class=p>${esc(s.prompt)}</span>
     <span class=n>${esc(s.status_label||s.status)} · ${s.n_cmds}c · ${s.n_edits}e · ${fmtTok(s.token_total)}t · $${Number(s.cost||0).toFixed(3)}${s.rate_pct!=null?' · '+Math.round(s.rate_pct)+'%':''}</span></div>`).join('')||'<div class=empty>No history.</div>');
}
async function postJSON(url,payload){
  const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
  const d=await r.json().catch(()=>({ok:false,error:'bad response'}));
  if(!r.ok||!d.ok)throw new Error(d.error||r.statusText); return d;
}
function fallbackCopy(text){
  const ta=document.createElement('textarea');
  ta.value=text; ta.setAttribute('readonly',''); ta.style.position='fixed'; ta.style.left='-9999px';
  document.body.appendChild(ta); ta.select(); ta.setSelectionRange(0,ta.value.length);
  const ok=document.execCommand('copy'); document.body.removeChild(ta);
  if(!ok)throw new Error('copy command rejected');
}
async function writeClipboard(text){
  if(navigator.clipboard&&window.isSecureContext){await navigator.clipboard.writeText(text);return;}
  fallbackCopy(text);
}
async function copyCmd(j){
  const text=String((j&&j.last_cmd)||(j&&j.pending_cmd)||'').trim();
  if(!text){document.getElementById('refresh_note').textContent='no command to copy';toast('Copy failed: no command','bad');return;}
  try{await writeClipboard(text);document.getElementById('refresh_note').textContent='last command copied';toast('Copied');}
  catch(e){try{fallbackCopy(text);document.getElementById('refresh_note').textContent='last command copied';toast('Copied');}catch(e2){toast('Copy failed: '+shortErr(e2),'bad');}}
}
async function killJob(j){if(!confirm('Kill Codex job PID '+j.pid+'?'))return;try{await postJSON('/api/kill',{pid:j.pid,out:j.out});toast('Kill requested');await tick(true);}catch(e){toast('Kill failed: '+shortErr(e),'bad');}}
async function retryJob(j){if(!confirm('Retry this cwd/prompt as a new codex exec job?'))return;try{const d=await postJSON('/api/retry',{cwd_raw:j.cwd_raw,prompt:j.prompt,sandbox:j.sandbox});document.getElementById('refresh_note').textContent='retry started pid '+d.pid;toast('Retry started');await tick(true);}catch(e){toast('Retry failed: '+shortErr(e),'bad');}}
function readNotifyPrefs(){return {
  master:document.getElementById('n_master').checked,
  zombie:document.getElementById('n_zombie').checked,error:document.getElementById('n_error').checked,
  rate:document.getElementById('n_rate').checked,rateLimit:Number(document.getElementById('n_rate_limit').value||80),
  idle:document.getElementById('n_idle').checked,idleMin:Number(document.getElementById('n_idle_min').value||10)
};}
function notifyOptions(){
  const p=readNotifyPrefs();
  return {...p,zombie:p.master&&p.zombie,error:p.master&&p.error,rate:p.master&&p.rate,idle:p.master&&p.idle};
}
function setNotifyUiState(){
  const panel=document.getElementById('notify_panel'), master=document.getElementById('n_master').checked;
  if(panel){panel.classList.toggle('muted',!master);panel.title=master?'Notifications enabled':'Master alert switch is off; individual toggles are ignored';}
}
function applyNotifyPrefs(p={}){
  const n={...NOTIFY_DEFAULTS,...p};
  ['master','zombie','error','rate','idle'].forEach(k=>{const el=document.getElementById('n_'+k); if(el)el.checked=!!n[k];});
  document.getElementById('n_rate_limit').value=n.rateLimit||80; document.getElementById('n_idle_min').value=n.idleMin||10;
  setNotifyUiState();
}
function loadNotifyState(){
  let n={}; try{n=JSON.parse(localStorage.getItem(NOTIFY_STORE)||'{}')||{};}catch(e){n={};}
  applyNotifyPrefs(n);
}
function saveNotifyState(){try{localStorage.setItem(NOTIFY_STORE,JSON.stringify(readNotifyPrefs()));}catch(e){}}
function requestNotifyIfNeeded(){
  const o=notifyOptions();
  if((o.zombie||o.error||o.rate||o.idle)&&'Notification' in window&&Notification.permission==='default')Notification.requestPermission().catch(()=>{});
}
function alertUser(id,title,body,kind='bad'){
  const now=Date.now(); if(idleSeen[id]&&now-idleSeen[id]<60000)return; idleSeen[id]=now;
  toast(title+' · '+body,kind);
  if('Notification' in window&&Notification.permission==='granted')new Notification(title,{body});
}
function primeAlerts(d){
  for(const j of d.running||[]){const k=stableId(j);statusSeen[k]=j.status;errorSeen[k]=(j.errors||[]).map(e=>e.ts+'|'+e.label+'|'+e.output).join(';');}
  rateHot=Number(d.rate||0)>=notifyOptions().rateLimit; alertsPrimed=true;
}
function maybeAlerts(d){
  const o=notifyOptions(); if(!alertsPrimed){primeAlerts(d);return;}
  for(const j of d.running||[]){
    const k=stableId(j), name=(j.cwd||j.pid||'job');
    if(o.zombie&&j.status==='zombie'&&statusSeen[k]!=='zombie')alertUser('zombie:'+k,'CODEX zombie',name+' · '+((j.activity||{}).label||'silent'),'bad');
    statusSeen[k]=j.status;
    const esig=(j.errors||[]).map(e=>e.ts+'|'+e.label+'|'+e.output).join(';');
    if(o.error&&esig&&errorSeen[k]!==esig)alertUser('error:'+k+':'+esig,'CODEX error',name+' · '+((j.errors||[]).slice(-1)[0]||{}).label,'bad');
    errorSeen[k]=esig;
    if(o.idle&&j.status==='running'&&(j.last_age_sec||0)>=o.idleMin*60)alertUser('idle:'+k+':'+o.idleMin,'CODEX idle',name+' · '+fmtAge(j.last_age_sec),'ok');
    if((j.last_age_sec||0)<Math.max(30,o.idleMin*30))delete idleSeen['idle:'+k+':'+o.idleMin];
  }
  const high=Number(d.rate||0)>=o.rateLimit;
  if(o.rate&&high&&!rateHot)alertUser('rate:'+o.rateLimit,'CODEX rate high',Math.round(d.rate)+'% used','bad');
  rateHot=high;
}
function saveState(){
  const st={filters:{},pollMs,compact:document.body.classList.contains('compact'),pins:[...pins]};
  ['f_dir','f_sandbox','f_status','f_sort','f_q'].forEach(id=>st.filters[id]=document.getElementById(id).value);
  try{localStorage.setItem(STORE,JSON.stringify(st));}catch(e){}
}
function loadState(){
  let st={}; try{st=JSON.parse(localStorage.getItem(STORE)||'{}')||{};}catch(e){st={};}
  controlState={...(st.filters||{})}; pins=new Set(st.pins||[]);
  ['f_status','f_sort','f_q'].forEach(id=>{if(controlState[id]!=null)document.getElementById(id).value=controlState[id];});
  pollMs=Number(st.pollMs||document.getElementById('poll_ms').value||2000); document.getElementById('poll_ms').value=String(pollMs);
  if(st.compact){document.body.classList.add('compact');document.getElementById('compact_btn').classList.add('on');}
  loadNotifyState();
}
function clearState(){
  try{localStorage.removeItem(STORE);localStorage.removeItem(NOTIFY_STORE);}catch(e){}
  controlState={}; pins.clear(); orderSeed={}; orderSeq=0; alertsPrimed=false; statusSeen={}; errorSeen={}; idleSeen={}; rateHot=false;
  ['f_dir','f_sandbox','f_status'].forEach(id=>document.getElementById(id).value='');
  document.getElementById('f_sort').value='elapsed'; document.getElementById('f_q').value='';
  document.getElementById('poll_ms').value='2000'; pollMs=2000; startPoll();
  applyNotifyPrefs();
  document.body.classList.remove('compact'); document.getElementById('compact_btn').classList.remove('on');
  if(latest)renderControls(latest); renderCards(); toast('State cleared');
}
async function tick(manual=false){
 try{
  const d=await (await fetch('/api',{cache:'no-store'})).json(); latest=d; lastOk=Date.now();
  setText(document.getElementById('ts'),d.ts); setText(document.getElementById('date'),d.date);
  document.getElementById('lamp').className='lamp'+(d.count>0?' on':'');
  setText(document.getElementById('onair'),d.count>0?'ON AIR':'STANDBY');
  document.getElementById('onair').style.color=d.count>0?'var(--ember)':'var(--dim)';
  setText(document.getElementById('s_run'),d.count); setText(document.getElementById('s_today'),d.today);
  setHTML(document.getElementById('s_rate'),Math.round(d.rate)+'<small>%</small>');
  document.getElementById('s_rate_bar').style.width=Math.min(100,d.rate)+'%';
  setHTML(document.getElementById('s_tokens'),fmtTok(d.token_total)+'<small> $'+Number(d.cost_total||0).toFixed(2)+'</small>');
  setText(document.getElementById('s_feed'),d.feed.length);
  renderControls(d); renderCards(); renderWire(d.feed); renderRecent(d.recent); maybeAlerts(d);
  setText(document.getElementById('refresh_note'),(manual?'manual · ':'')+'updated now');
 }catch(e){setText(document.getElementById('ts'),'connection lost');setText(document.getElementById('onair'),'LINE DOWN');}
}
function startPoll(){if(timer)clearInterval(timer);timer=setInterval(tick,pollMs);}
setInterval(()=>{for(const pid in ticks){const el=document.getElementById('el_'+pid);if(!el)continue;
  let p=ticks[pid].split(':').map(Number);let s=p.pop()+1;let m=p.pop()||0;let h=p.pop()||0;
  if(s>59){s=0;m++;}if(m>59){m=0;h++;}
  ticks[pid]=(h?String(h).padStart(2,'0')+':':'')+String(m).padStart(2,'0')+':'+String(s).padStart(2,'0');
  el.textContent=ticks[pid];}
  if(lastOk){const age=Math.floor((Date.now()-lastOk)/1000);setText(document.getElementById('refresh_state'),age>pollMs/1000*3?'stale '+age+'s':'last refresh '+age+'s ago');}
},1000);
loadState();
['f_dir','f_sandbox','f_status','f_sort','f_q'].forEach(id=>document.getElementById(id).addEventListener('input',e=>{controlState[id]=e.target.value;saveState();renderCards();}));
document.getElementById('poll_ms').addEventListener('change',e=>{pollMs=Number(e.target.value);saveState();startPoll();tick(true);});
document.getElementById('refresh_btn').addEventListener('click',()=>tick(true));
document.getElementById('compact_btn').addEventListener('click',e=>{document.body.classList.toggle('compact');e.target.classList.toggle('on',document.body.classList.contains('compact'));saveState();});
document.getElementById('clear_state_btn').addEventListener('click',clearState);
['n_master','n_zombie','n_error','n_rate','n_idle','n_rate_limit','n_idle_min'].forEach(id=>document.getElementById(id).addEventListener('input',()=>{setNotifyUiState();requestNotifyIfNeeded();saveNotifyState();}));
tick();startPoll();
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, body, ctype):
        self.send_response(code); self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)
    def do_GET(self):
        if self.path.startswith("/api"):
            body = json.dumps(snapshot(), ensure_ascii=False).encode()
            self._send(200, body, "application/json; charset=utf-8")
        else:
            self._send(200, PAGE.encode(), "text/html; charset=utf-8")
    def do_POST(self):
        if not _local_client(self):
            body = json.dumps({"ok": False, "error": "POST actions are localhost-only"}).encode()
            self._send(403, body, "application/json; charset=utf-8"); return
        path = urllib.parse.urlparse(self.path).path
        payload = _json_body(self)
        if path == "/api/kill":
            data, code = post_kill(payload)
        elif path == "/api/retry":
            data, code = post_retry(payload)
        else:
            data, code = {"ok": False, "error": "unknown endpoint"}, 404
        body = json.dumps(data, ensure_ascii=False).encode()
        self._send(code, body, "application/json; charset=utf-8")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default=os.environ.get("CODEX_MONITOR_HOST", "127.0.0.1"),
                    help="bind host; use --host 0.0.0.0 or CODEX_MONITOR_HOST for LAN read-only viewing")
    ap.add_argument("--port", type=int, default=int(os.environ.get("CODEX_MONITOR_PORT", str(PORT))))
    args = ap.parse_args()
    print(f"CODEX WIRE → http://{args.host}:{args.port}")
    ThreadingHTTPServer((args.host, args.port), H).serve_forever()


if __name__ == "__main__":
    main()
