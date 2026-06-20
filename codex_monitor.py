#!/usr/bin/env python3
"""CODEX WIRE — live telemetry for codex-exec agents driven by Claude Code.
Run:  python3 ~/codex_monitor.py   →   open http://localhost:8787
Sources (stdlib only, no deps):
  • `ps`                          → running codex exec jobs (pid, elapsed, cwd, sandbox)
  • ~/.codex/sessions/**/*.jsonl  → live activity: commands, file edits, agent messages, tokens
"""
import argparse, ctypes, datetime, glob, hashlib, json, math, os, re, shlex, signal, struct, subprocess, sys, threading, time, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 8787
SESS = os.path.expanduser(os.environ.get("CODEX_MONITOR_SESS_DIR", "~/.codex/sessions"))
HOME = os.path.expanduser("~")

SESSION_LIMIT = int(os.environ.get("CODEX_MONITOR_SESSION_LIMIT", "80"))
RECENT_LIMIT = int(os.environ.get("CODEX_MONITOR_RECENT_LIMIT", "50"))
ACTIVE_STALE_SEC = int(os.environ.get("CODEX_MONITOR_STALE_SEC", "120"))
FEED_WINDOW_SEC = int(os.environ.get("CODEX_MONITOR_FEED_WINDOW_SEC", "1800"))
LONG_OP_GRACE_SEC = int(os.environ.get("CODEX_MONITOR_LONG_OP_GRACE_SEC", "180"))
TRACK_PATH = os.path.expanduser(os.environ.get("CODEX_MONITOR_TRACK_PATH", "~/.codex/codex_wire_jobs.json"))
TRACK_TTL_SEC = int(os.environ.get("CODEX_MONITOR_TRACK_TTL_SEC", str(7 * 24 * 3600)))

# Pricing is USD per 1M tokens.
DEFAULT_MODEL = "gpt-5.5"
PRICING = {
    "gpt-5.5": {"input": 5.0, "cached": 0.5, "output": 30.0},
}

_TRACK_STATE = None
_PS_STATE = {"jobs": [], "degraded": False, "error": "", "ts": 0.0}
_LAST_SNAPSHOT = None


def _short(p):
    return (p or "").replace(HOME, "~")


def _ps_lines():
    try:
        proc = subprocess.run(["ps", "-axww", "-o", "pid=,etime=,stat=,args="],
                              capture_output=True, text=True, timeout=4)
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or "").strip() or f"ps exited {proc.returncode}")
        return proc.stdout.splitlines()
    except Exception as e:
        _PS_STATE.update(degraded=True, error=str(e), ts=time.time())
        return None


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


def running_jobs(use_cache_on_failure=True):
    jobs, seen = [], set()
    lines = _ps_lines()
    if lines is None:
        if use_cache_on_failure:
            return [dict(j, ps_degraded=True) for j in (_PS_STATE.get("jobs") or [])]
        return []
    _PS_STATE.update(degraded=False, error="", ts=time.time())
    for ln in lines:
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
    _PS_STATE["jobs"] = [dict(j) for j in jobs]
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


def _add_parse_error(parse_errors, msg):
    if parse_errors is not None and len(parse_errors) < 3:
        parse_errors.append(str(msg))


def _jsonl_obj(raw, parse_errors=None, complete=None):
    if not raw or not raw.strip():
        return None
    if complete is None:
        complete = raw.endswith(b"\n")
    try:
        obj = json.loads(raw.strip())
    except Exception as e:
        if not complete:
            return None
        _add_parse_error(parse_errors, e)
        return None
    if not isinstance(obj, dict):
        _add_parse_error(parse_errors, "jsonl schema: top-level record is not an object")
        return None
    payload = obj.get("payload", {})
    if payload is not None and not isinstance(payload, dict):
        _add_parse_error(parse_errors, "jsonl schema: payload is not an object")
        return None
    return obj


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
                    obj = _jsonl_obj(raw, parse_errors, complete=True)
                    if obj is not None:
                        yield obj
                fh.seek(size - _TAIL); fh.readline()   # drop the partial first line
                for raw in fh:
                    obj = _jsonl_obj(raw, parse_errors)
                    if obj is not None:
                        yield obj
            else:
                fh.seek(0)
                for raw in fh:
                    obj = _jsonl_obj(raw, parse_errors)
                    if obj is not None:
                        yield obj
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


def _pricing_for_model(model):
    model = str(model or DEFAULT_MODEL).strip() or DEFAULT_MODEL
    key = model.lower()
    if key in PRICING:
        return key, PRICING[key], False
    return DEFAULT_MODEL, PRICING[DEFAULT_MODEL], True


def _usage_cost(tokens, pricing):
    uncached = max(0, tokens.get("input", 0) - tokens.get("cached", 0))
    cost = (uncached * pricing["input"] +
            tokens.get("cached", 0) * pricing["cached"] +
            tokens.get("output", 0) * pricing["output"]) / 1_000_000
    return float(cost)


def _api_tokens(tokens):
    out = dict(tokens or {})
    try:
        out["cost"] = round(float(out.get("cost") or 0.0), 4)
    except Exception:
        out["cost"] = 0.0
    return out


def _normalize_usage(info, model=None):
    usage = ((info or {}).get("total_token_usage") or
             (info or {}).get("last_token_usage") or
             (info or {}).get("usage") or {})
    model = ((info or {}).get("model") or (info or {}).get("model_name") or model or DEFAULT_MODEL)
    pricing_model, pricing, fallback = _pricing_for_model(model)
    tokens = {
        "input": int(usage.get("input_tokens") or 0),
        "cached": int(usage.get("cached_input_tokens") or 0),
        "output": int(usage.get("output_tokens") or 0),
        "reasoning": int(usage.get("reasoning_output_tokens") or 0),
        "total": int(usage.get("total_tokens") or 0),
        "model": str(model or DEFAULT_MODEL),
        "cost_model": pricing_model,
        "cost_estimate": fallback,
        "cost_note": "est:fallback" if fallback else "",
    }
    if not tokens["total"]:
        tokens["total"] = tokens["input"] + tokens["output"]
    tokens["cost"] = _usage_cost(tokens, pricing)
    return tokens


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _rate_sample(rl, ts, ets):
    pct = _num((rl or {}).get("used_percent"))
    if pct is None:
        return None
    return {
        "pct": pct,
        "window": (rl or {}).get("window_minutes"),
        "resets_at": (rl or {}).get("resets_at"),
        "ts": ets if ets is not None else _epoch(ts),
    }


def _latest_active_rate(sessions, key, now):
    latest = None
    for s in sessions:
        sample = (s or {}).get(key) or {}
        pct = _num(sample.get("pct"))
        resets_at = _num(sample.get("resets_at"))
        if pct is None or resets_at is None or resets_at <= now:
            continue
        ts = _num(sample.get("ts"))
        rank = ts if ts is not None else resets_at
        if latest is None or (rank, resets_at) > (latest[0], latest[1]):
            latest = (rank, resets_at, pct)
    return latest[2] if latest else None


def parse_session(path):
    """Extract a rich summary + event stream from one rollout jsonl."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    s = {"file": path, "cwd": "", "prompt": "", "n_cmds": 0, "n_edits": 0,
         "files": [], "last_cmd": "", "last_msg": "", "rate_pct": None,
         "rate_primary": None, "rate_secondary": None,
         "ctx_window": None, "started": None, "last_ts": st.st_mtime,
         "last_event_ts": "", "last_event_epoch": None, "pending_cmd": "",
         "pending_cmds": [], "pending_long": False,
         "events": [], "model": "", "outputs": [], "errors": [],
         "tokens": {"input": 0, "cached": 0, "output": 0, "reasoning": 0, "total": 0, "cost": 0.0}}
    files, exec_calls, parse_errors = set(), {}, []
    for o in _iter(path, st.st_size, parse_errors):
        try:
            if not isinstance(o, dict):
                continue
            p = o.get("payload", {}) or {}
            if not isinstance(p, dict):
                _add_parse_error(parse_errors, "jsonl schema: payload is not an object")
                continue
            ts = o.get("timestamp", "")
            ets = _epoch(ts)
            if ets:
                s["last_ts"] = max(s["last_ts"], ets)
                if s["last_event_epoch"] is None or ets > s["last_event_epoch"]:
                    s["last_event_epoch"] = ets
                    s["last_event_ts"] = ts
            otype, ptype = o.get("type"), p.get("type", "")
            if otype == "session_meta" or ("cwd" in p and not s["cwd"]):
                s["cwd"] = p.get("cwd", s["cwd"]); s["model"] = (p.get("model") or o.get("model") or s["model"])
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
                rate_limits = p.get("rate_limits") or {}
                primary = _rate_sample(rate_limits.get("primary") or {}, ts, ets)
                if primary is not None:
                    s["rate_primary"] = primary
                    s["rate_pct"] = primary["pct"]
                secondary = _rate_sample(rate_limits.get("secondary") or {}, ts, ets)
                if secondary is not None:
                    s["rate_secondary"] = secondary
                info = p.get("info") or {}
                if info:
                    s["model"] = (p.get("model") or info.get("model") or info.get("model_name") or s.get("model") or DEFAULT_MODEL)
                    s["tokens"] = _normalize_usage(info, s["model"])
                    if info.get("model_context_window"):
                        s["ctx_window"] = info.get("model_context_window")
        except Exception as e:
            _add_parse_error(parse_errors, f"jsonl schema: {e}")
    if parse_errors:
        err = _error_entry("", "jsonl parser", None, "JSONL parse damage: " + "; ".join(parse_errors))
        s["errors"].append(err)
        _event(s["events"], s.get("last_event_ts") or "", "err", f"{err['label']}: session log damaged", True)
    pending = [v for v in exec_calls.values() if isinstance(v, dict) and v.get("cmd")]
    s["pending_cmds"] = [v["cmd"] for v in pending][-3:]
    s["pending_cmd"] = s["pending_cmds"][-1] if s["pending_cmds"] else ""
    s["pending_long"] = bool(s["pending_cmd"] and _LONG_CMD_RE.search(s["pending_cmd"]))
    s["files"] = sorted(files)[:12]
    s["events"] = s["events"][-12:]
    s["outputs"] = s["outputs"][-5:]
    s["errors"] = s["errors"][-5:]
    return s


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0


def _parse_session_files(files):
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
    return out


def all_sessions(limit=SESSION_LIMIT):
    try:
        files = glob.glob(os.path.join(SESS, "**", "*.jsonl"), recursive=True)
    except Exception:
        files = []
    files = sorted(files, key=_mtime, reverse=True)[:limit]
    out = _parse_session_files(files)
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
    long_op = bool(s and pending_cmd and (s.get("pending_long") or _LONG_CMD_RE.search(pending_cmd)))
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
        meta.update(confidence=35, label="quiet long cmd", reason="PID is alive and the pending command looks long-running")
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
    tokens = _api_tokens((s or {}).get("tokens", {}))
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
        "cost_estimate": tokens.get("cost_estimate", False), "cost_note": tokens.get("cost_note", ""),
    }


# ── Cost history ─────────────────────────────────────────────────────────────
# Cost graph points are derived from token_count cumulative deltas. This parser
# intentionally does not use _iter(): large files must not skip middle deltas.
_COST_INDEX_PATH = os.path.expanduser(os.environ.get("CODEX_MONITOR_COST_INDEX_PATH", "~/.codex/codex_wire_cost_index.json"))
_COST_INDEX_SCHEMA = 1
_COST_INDEX = None
_COST_INDEX_LOCK = threading.Lock()
_COST_INDEX_STATS = {"reused": 0, "parsed": 0, "rescanned": 0, "saved": False, "generation": 0}
_SERIES_CACHE = {"ts": 0.0, "data": None, "sig": None, "generation": None, "pricing_version": None}
_COST_RANGES = [                     # name, span seconds, bucket count
    ("5h",    5 * 3600,        30),
    ("day",   24 * 3600,       24),
    ("week",  7 * 24 * 3600,   7),
    ("month", 30 * 24 * 3600,  30),
]
_COST_LABEL = {"5h": "5h session", "day": "last 24h", "week": "last 7 days",
               "month": "last 30 days", "year": "last 12 months"}


def _pricing_version():
    payload = {"default_model": DEFAULT_MODEL, "pricing": PRICING}
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def _empty_cost_index():
    return {
        "version": _COST_INDEX_SCHEMA,
        "pricing_version": _pricing_version(),
        "generation": 0,
        "updated_at": 0,
        "files": {},
    }


def _coerce_int(v):
    if isinstance(v, bool):
        raise ValueError("bool is not an int")
    return int(v)


def _coerce_float(v):
    if isinstance(v, bool):
        raise ValueError("bool is not a float")
    out = float(v)
    if not math.isfinite(out):
        raise ValueError("non-finite float")
    return out


def _coerce_usage_record(value):
    if not isinstance(value, dict):
        raise ValueError("last_usage is not an object")
    out = {}
    for k in ("input", "cached", "output", "total"):
        out[k] = max(0, _coerce_int(value.get(k, 0)))
    if not out["total"]:
        out["total"] = out["input"] + out["output"]
    return out


def _coerce_cost_points(points):
    if not isinstance(points, list):
        raise ValueError("points is not a list")
    out = []
    for pt in points:
        if not isinstance(pt, dict):
            continue
        try:
            ts = _coerce_float(pt.get("ts"))
            cost = _coerce_float(pt.get("cost"))
            total = _coerce_int(pt.get("total"))
        except Exception:
            continue
        out.append({"ts": ts, "cost": cost, "total": max(0, total)})
    return out


def _coerce_cost_record(rec):
    if not isinstance(rec, dict):
        raise ValueError("record is not an object")
    return {
        "dev": _coerce_int(rec.get("dev")),
        "ino": _coerce_int(rec.get("ino")),
        "size": max(0, _coerce_int(rec.get("size"))),
        "mtime": _coerce_float(rec.get("mtime", 0.0)),
        "offset": max(0, _coerce_int(rec.get("offset", 0))),
        "last_usage": _coerce_usage_record(rec.get("last_usage")),
        "last_model": str(rec.get("last_model") or DEFAULT_MODEL),
        "last_ts": str(rec.get("last_ts") or ""),
        "points": _coerce_cost_points(rec.get("points", [])),
    }


def _load_cost_index():
    global _COST_INDEX
    if _COST_INDEX is not None:
        return _COST_INDEX
    try:
        with open(_COST_INDEX_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    if (data.get("version") != _COST_INDEX_SCHEMA or
            data.get("pricing_version") != _pricing_version() or
            not isinstance(data.get("files"), dict)):
        data = _empty_cost_index()
    try:
        data["generation"] = max(0, _coerce_int(data.get("generation", 0)))
    except Exception:
        data["generation"] = 0
    try:
        data["updated_at"] = max(0.0, _coerce_float(data.get("updated_at", 0.0)))
    except Exception:
        data["updated_at"] = 0
    files = {}
    for path, rec in (data.get("files") or {}).items():
        if not isinstance(path, str) or not path:
            continue
        try:
            files[path] = _coerce_cost_record(rec)
        except Exception:
            continue
    data["files"] = files
    data["pricing_version"] = _pricing_version()
    _COST_INDEX = data
    return _COST_INDEX


def _save_cost_index(index):
    try:
        parent = os.path.dirname(_COST_INDEX_PATH)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = _COST_INDEX_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(index, fh, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, _COST_INDEX_PATH)
        return True
    except Exception:
        return False


def _file_sig(path, st):
    return (path, int(st.st_dev), int(st.st_ino), int(st.st_size), float(st.st_mtime))


def _cost_usage_from_info(info):
    usage = (info or {}).get("total_token_usage") or (info or {}).get("usage") or {}
    if not isinstance(usage, dict) or not usage:
        return None

    def iv(name):
        try:
            return max(0, int(usage.get(name) or 0))
        except Exception:
            return 0

    out = {
        "input": iv("input_tokens"),
        "cached": iv("cached_input_tokens"),
        "output": iv("output_tokens"),
        "total": iv("total_tokens"),
    }
    if not out["total"]:
        out["total"] = out["input"] + out["output"]
    return out


def _usage_decreased(cur, prev):
    if not prev:
        return False
    return int(cur.get("total", 0)) < int(prev.get("total", 0))


def _usage_delta(cur, prev):
    prev = prev or {}
    return {k: max(0, int(cur.get(k, 0)) - int(prev.get(k, 0))) for k in ("input", "cached", "output", "total")}


def _cost_file_record(path, st, old=None, reset=False):
    old = old if isinstance(old, dict) else {}
    points = [] if reset else list(old.get("points") or [])
    last_usage = None if reset else old.get("last_usage")
    last_model = "" if reset else str(old.get("last_model") or "")
    last_ts = "" if reset else str(old.get("last_ts") or "")
    offset = 0 if reset else int(old.get("offset") or 0)
    if offset < 0 or offset > st.st_size:
        offset = 0
        points, last_usage, last_model, last_ts = [], None, "", ""

    parse_errors, severe, parsed_any = [], False, False
    try:
        with open(path, "rb") as fh:
            fh.seek(offset)
            while True:
                pos = fh.tell()
                raw = fh.readline()
                if not raw:
                    break
                next_pos = fh.tell()
                stripped = raw.strip()
                if not stripped:
                    offset = next_pos
                    continue
                try:
                    obj = json.loads(stripped)
                except Exception as e:
                    if not raw.endswith(b"\n"):
                        offset = pos
                        break
                    parse_errors.append(str(e))
                    offset = next_pos
                    if len(parse_errors) >= 3:
                        severe = True
                        break
                    continue

                parsed_any = True
                offset = next_pos
                if not isinstance(obj, dict):
                    parse_errors.append("jsonl schema: top-level record is not an object")
                    if len(parse_errors) >= 3:
                        severe = True
                        break
                    continue
                payload = obj.get("payload", {}) or {}
                if not isinstance(payload, dict):
                    parse_errors.append("jsonl schema: payload is not an object")
                    if len(parse_errors) >= 3:
                        severe = True
                        break
                    continue
                model = payload.get("model") or obj.get("model") or last_model
                if model:
                    last_model = str(model)
                if payload.get("type") != "token_count":
                    continue
                info = payload.get("info") or {}
                usage = _cost_usage_from_info(info)
                if not usage:
                    continue
                model = payload.get("model") or info.get("model") or info.get("model_name") or last_model or DEFAULT_MODEL
                last_model = str(model)
                ep = _epoch(obj.get("timestamp", ""))
                if ep is None:
                    last_usage = usage
                    continue
                if _usage_decreased(usage, last_usage):
                    last_usage = usage
                    last_ts = obj.get("timestamp", "") or last_ts
                    continue
                delta = _usage_delta(usage, last_usage)
                last_usage = usage
                last_ts = obj.get("timestamp", "") or last_ts
                if not any(delta.get(k, 0) for k in ("input", "cached", "output", "total")):
                    continue
                _, pricing, _ = _pricing_for_model(model)
                cost = _usage_cost(delta, pricing)
                if cost or delta.get("total", 0):
                    points.append({"ts": ep, "cost": cost, "total": int(delta.get("total", 0))})
    except Exception:
        if not reset:
            return _cost_file_record(path, st, None, True), True, True, False

    if severe and not reset:
        rec, _, _, _ = _cost_file_record(path, st, None, True)
        return rec, True, True, True

    rec = {
        "dev": int(st.st_dev),
        "ino": int(st.st_ino),
        "size": int(st.st_size),
        "mtime": float(st.st_mtime),
        "offset": int(offset),
        "last_usage": last_usage or {"input": 0, "cached": 0, "output": 0, "total": 0},
        "last_model": last_model or DEFAULT_MODEL,
        "last_ts": last_ts,
        "points": points,
    }
    if parse_errors:
        rec["parse_errors"] = parse_errors[:3]
    meta_changed = (
        int(old.get("dev", -1)) != int(st.st_dev) or
        int(old.get("ino", -1)) != int(st.st_ino) or
        int(old.get("size", -1)) != int(st.st_size) or
        float(old.get("mtime") or 0) != float(st.st_mtime)
    )
    changed = bool(parsed_any or offset != int(old.get("offset") or 0) or reset or meta_changed)
    return rec, changed, bool(reset), bool(severe)


def _refresh_cost_index(entries):
    global _COST_INDEX_STATS
    with _COST_INDEX_LOCK:
        index = _load_cost_index()
        files = index.setdefault("files", {})
        seen = {path for path, _ in entries}
        dirty = False
        stats = {"reused": 0, "parsed": 0, "rescanned": 0, "saved": False, "generation": index.get("generation", 0)}

        for path in list(files):
            if path not in seen:
                files.pop(path, None)
                dirty = True

        for path, st in entries:
            old = files.get(path)
            reset = False
            if isinstance(old, dict):
                same_identity = int(old.get("dev", -1)) == int(st.st_dev) and int(old.get("ino", -1)) == int(st.st_ino)
                old_size = int(old.get("size") or 0)
                old_offset = int(old.get("offset") or 0)
                complete = old_offset >= int(st.st_size)
                if same_identity and old_size == int(st.st_size) and float(old.get("mtime") or 0) == float(st.st_mtime) and complete:
                    stats["reused"] += 1
                    continue
                if not same_identity or int(st.st_size) < old_size or int(st.st_size) < old_offset:
                    reset = True
            else:
                reset = True

            rec, changed, rescanned, _ = _cost_file_record(path, st, old, reset)
            files[path] = rec
            if changed:
                dirty = True
                stats["parsed"] += 1
            else:
                stats["reused"] += 1
            if rescanned:
                stats["rescanned"] += 1

        if dirty:
            index["generation"] = int(index.get("generation") or 0) + 1
            index["updated_at"] = time.time()
            stats["generation"] = index["generation"]
            stats["saved"] = _save_cost_index(index)
        _COST_INDEX_STATS = stats
        pts = []
        for rec in files.values():
            for pt in rec.get("points") or []:
                if pt and (pt.get("cost") or pt.get("total")):
                    pts.append(pt)
        return pts, int(index.get("generation") or 0), stats


def _year_buckets(pts, now):
    base = datetime.datetime.fromtimestamp(now)
    months, y, m = [], base.year, base.month
    for _ in range(12):
        months.append((y, m))
        m -= 1
        if m == 0:
            m, y = 12, y - 1
    months.reverse()
    index = {ym: i for i, ym in enumerate(months)}
    cost, toks = [0.0] * 12, 0
    for pt in pts:
        d = datetime.datetime.fromtimestamp(pt["ts"])
        i = index.get((d.year, d.month))
        if i is not None:
            cost[i] += pt["cost"]
            toks += pt["total"]
    return cost, toks


def _aligned_starts(now, unit, n):
    """Bucket starts aligned to clock boundaries, oldest to newest."""
    if unit == 86400:
        anchor = datetime.datetime.fromtimestamp(now).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).timestamp()
    else:
        anchor = (now // unit) * unit
    return [anchor - (n - 1 - k) * unit for k in range(n)]


def _cost_grid(name, now):
    ranges = {
        "5h": (600, 30),
        "day": (3600, 24),
        "week": (86400, 7),
        "month": (86400, 30),
    }
    spec = ranges.get(name)
    if not spec:
        return None
    unit, n = spec
    return unit, n, _aligned_starts(now, unit, n)


def _cost_axis(name, now):
    """Time anchors for a range's x-axis: normalized x (0=oldest, 1=now) + label."""
    base = datetime.datetime.fromtimestamp(now)
    grid = _cost_grid(name, now)
    if name == "5h":
        _, n, starts = grid
        out = []
        for k, ts in enumerate(starts[:-1]):
            t = datetime.datetime.fromtimestamp(ts)
            if t.minute == 0:
                out.append({"x": round(k / (n - 1), 4), "label": t.strftime("%H:%M")})
        out.append({"x": 1, "label": "now"})
        return out
    if name == "day":
        _, n, starts = grid
        out = [{"x": round(k / (n - 1), 4),
                "label": datetime.datetime.fromtimestamp(starts[k]).strftime("%H:%M")}
               for k in range(0, n, 2)]
        out.append({"x": 1, "label": "now"})
        return out
    if name == "week":
        _, n, starts = grid
        return [{"x": round(k / (n - 1), 4),
                 "label": datetime.datetime.fromtimestamp(starts[k]).strftime("%a")}
                for k in range(n)]
    if name == "month":
        _, n, starts = grid
        out = []
        for k in (0, 10, 20):
            t = datetime.datetime.fromtimestamp(starts[k])
            out.append({"x": round(k / (n - 1), 4), "label": f"{t.month}/{t.day}"})
        out.append({"x": 1, "label": "now"})
        return out
    if name == "year":
        seq, y, m = [], base.year, base.month
        for _ in range(12):
            seq.append((y, m)); m -= 1
            if m == 0:
                m, y = 12, y - 1
        seq.reverse()
        return [{"x": round(i / 11, 4), "label": datetime.date(yy, mm, 1).strftime("%b")}
                for i, (yy, mm) in enumerate(seq)]
    return []


def _cost_blabels(name, now):
    """Per-bucket hover labels for cost ranges, aligned to bucket starts."""
    grid = _cost_grid(name, now)
    if name == "5h":
        return [datetime.datetime.fromtimestamp(ts).strftime("%H:%M") for ts in grid[2]]
    if name == "day":
        return [datetime.datetime.fromtimestamp(ts).strftime("%H:%M") for ts in grid[2]]
    if name == "week":
        return [f"{dt.strftime('%a')} {dt.month}/{dt.day}"
                for dt in (datetime.datetime.fromtimestamp(ts) for ts in grid[2])]
    if name == "month":
        return [f"{dt.month}/{dt.day}"
                for dt in (datetime.datetime.fromtimestamp(ts) for ts in grid[2])]
    if name == "year":
        base = datetime.datetime.fromtimestamp(now)
        seq, y, m = [], base.year, base.month
        for _ in range(12):
            seq.append((y, m)); m -= 1
            if m == 0:
                m, y = 12, y - 1
        seq.reverse()
        return [datetime.date(yy, mm, 1).strftime("%b") for yy, mm in seq]
    return []


def cost_series(now):
    """Bucket token_count delta spend into rolling windows."""
    pricing_version = _pricing_version()
    if (_SERIES_CACHE["data"] is not None and (now - _SERIES_CACHE["ts"]) < 15 and
            _SERIES_CACHE.get("pricing_version") == pricing_version):
        return _SERIES_CACHE["data"]

    try:
        files = sorted(glob.glob(os.path.join(SESS, "**", "*.jsonl"), recursive=True))
    except Exception:
        files = []
    entries, sig = [], []
    for f in files:
        try:
            st = os.stat(f)
        except OSError:
            continue
        entries.append((f, st))
        sig.append(_file_sig(f, st))

    pts, generation, _ = _refresh_cost_index(entries)
    if (_SERIES_CACHE["data"] is not None and (now - _SERIES_CACHE["ts"]) < 15 and
            _SERIES_CACHE.get("sig") == sig and
            _SERIES_CACHE.get("generation") == generation and
            _SERIES_CACHE.get("pricing_version") == pricing_version):
        return _SERIES_CACHE["data"]

    out = {}
    for name, _, _ in _COST_RANGES:
        unit, nb, starts = _cost_grid(name, now)
        cost, toks = [0.0] * nb, 0
        for pt in pts:
            if pt["ts"] < starts[0] or pt["ts"] > now:
                continue
            i = int((pt["ts"] - starts[0]) // unit)
            if 0 <= i < nb:
                cost[i] += pt["cost"]
                toks += pt["total"]
        out[name] = {"label": _COST_LABEL[name], "points": [round(c, 4) for c in cost],
                     "total": round(sum(cost), 4), "tokens": toks, "axis": _cost_axis(name, now),
                     "blabels": _cost_blabels(name, now)}
    ycost, ytok = _year_buckets(pts, now)
    out["year"] = {"label": _COST_LABEL["year"], "points": [round(c, 4) for c in ycost],
                   "total": round(sum(ycost), 4), "tokens": ytok, "axis": _cost_axis("year", now),
                   "blabels": _cost_blabels("year", now)}
    _SERIES_CACHE["ts"] = now
    _SERIES_CACHE["data"] = out
    _SERIES_CACHE["sig"] = sig
    _SERIES_CACHE["generation"] = generation
    _SERIES_CACHE["pricing_version"] = pricing_version
    return out


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
            "rate_pct": s["rate_pct"], "tokens": _api_tokens(s["tokens"]), "token_total": s["tokens"].get("total", 0),
            "cost": round(float(s["tokens"].get("cost", 0.0) or 0.0), 4), "cost_estimate": s["tokens"].get("cost_estimate", False),
            "cost_note": s["tokens"].get("cost_note", ""), "errors": s["errors"][:2],
        })
    recent = recent[:RECENT_LIMIT]

    today = time.strftime("%Y/%m/%d")
    try:
        today_files = glob.glob(os.path.join(SESS, today, "*.jsonl"))
    except Exception:
        today_files = []
    today_n = len(today_files)
    today_sessions = _parse_session_files(sorted(today_files, key=_mtime, reverse=True))
    rate = _latest_active_rate(sessions, "rate_primary", now)
    rate7d = _latest_active_rate(sessions, "rate_secondary", now)
    token_total_recent = sum((s.get("tokens") or {}).get("total", 0) for s in sessions)
    cost_total_recent = round(sum((s.get("tokens") or {}).get("cost", 0.0) for s in sessions), 4)
    token_total = sum((s.get("tokens") or {}).get("total", 0) for s in today_sessions)
    cost_total = round(sum((s.get("tokens") or {}).get("cost", 0.0) for s in today_sessions), 4)
    cost_estimate = any((s.get("tokens") or {}).get("cost_estimate") for s in today_sessions)
    # Counts only live rows derived from the current ps codex process list.
    status_counts = {k: sum(1 for j in running if j["status"] == k) for k in ("running", "zombie", "error", "done", "killed", "interrupted")}
    return {"ts": time.strftime("%H:%M:%S"), "date": time.strftime("%a %d %b %Y").upper(),
            "count": len(running), "today": today_n, "rate": rate, "rate7d": rate7d,
            "stale_sec": ACTIVE_STALE_SEC, "status_counts": status_counts,
            "token_total": token_total, "cost_total": cost_total, "cost_estimate": cost_estimate,
            "token_total_recent": token_total_recent, "cost_total_recent": cost_total_recent,
            "cost_series": cost_series(now),
            "source_degraded": {"ps": bool(_PS_STATE.get("degraded")), "ps_error": _PS_STATE.get("error", "")},
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
    for j in running_jobs(use_cache_on_failure=False):
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
body::before,body::after{content:"";position:fixed;inset:0;pointer-events:none;z-index:0}
body::before{
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='180' height='180' viewBox='0 0 180 180'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='.72' numOctaves='3' stitchTiles='stitch'/%3E%3CfeColorMatrix type='saturate' values='0'/%3E%3C/filter%3E%3Crect width='180' height='180' filter='url(%23n)' opacity='.72'/%3E%3C/svg%3E");
  opacity:.04;mix-blend-mode:multiply;
}
body::after{background:radial-gradient(ellipse at center,rgba(51,39,26,0) 54%,rgba(91,61,29,.08) 78%,rgba(51,39,26,.18) 100%)}
body>*{position:relative;z-index:1}
body.offline{filter:saturate(.4)}

.mast{display:flex;align-items:flex-end;justify-content:space-between;gap:18px;flex-wrap:wrap;
  border-bottom:3px double #b79b6a;padding-bottom:14px;position:relative}
.mast:after{content:"";position:absolute;left:0;right:0;bottom:-6px;height:1px;background:#cbb588}
.mastmeta{display:flex;align-items:center;justify-content:center;gap:12px;flex-wrap:wrap;
  margin:9px 0 0;padding:5px 0 4px;border-top:1px solid #d4bf92;border-bottom:1px solid #d4bf92;
  color:var(--dim);font:600 10px "Saira Condensed",sans-serif;letter-spacing:.2em;text-transform:uppercase}
.mastmeta .dot{color:var(--faint);letter-spacing:0}
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
.bulletin{display:inline-block;font-family:"Saira Condensed",sans-serif;font-weight:700;
  letter-spacing:.18em;text-transform:uppercase;color:var(--faint)}
.bulletin .stale{color:var(--warn)}.bulletin .err{color:var(--bad)}
.wirebanner{display:none;margin:12px 0 8px;border:1px solid #c9b382;border-left:3px solid var(--bad);
  background:linear-gradient(180deg,#e5d5b6,#dfcba5);color:var(--bad);
  font:700 12px "Saira Condensed",sans-serif;letter-spacing:.2em;text-transform:uppercase;
  padding:8px 12px}

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
.lamp.bad{background:var(--bad);animation:badpulse 1.15s infinite}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(207,89,21,.55)}70%{box-shadow:0 0 0 11px rgba(207,89,21,0)}100%{box-shadow:0 0 0 0 rgba(207,89,21,0)}}
@keyframes badpulse{0%{box-shadow:0 0 0 0 rgba(169,54,34,.5)}70%{box-shadow:0 0 0 11px rgba(169,54,34,0)}100%{box-shadow:0 0 0 0 rgba(169,54,34,0)}}
.ratetog{display:inline-flex;margin-left:8px;border:1px solid #c2aa78;background:#fbf3e1;vertical-align:middle}
.ratetog button{appearance:none;cursor:pointer;border:0;border-left:1px solid #ddcaa0;background:transparent;
  font:700 9px "Saira Condensed",sans-serif;letter-spacing:.1em;text-transform:uppercase;color:#6b5638;
  padding:2px 6px;line-height:1.35;transition:background .15s,color .15s}
.ratetog button:first-child{border-left:0}
.ratetog button.on{background:#33271a;color:#f1e7d2}
.ratetog button:hover:not(.on){background:#f1e3c6;color:#2a1d10}
.gauge{height:5px;background:#ddcba8;margin-top:9px;overflow:hidden}
.gauge i{display:block;height:100%;background:var(--wire);transition:width .6s,background-color .2s}
.gauge i.rate-ok{background:var(--wire)}
.gauge i.rate-warn{background:var(--warn)}
.gauge i.rate-bad{background:var(--bad);animation:ratepulse 1.2s infinite}
@keyframes ratepulse{0%,100%{opacity:1}50%{opacity:.66}}

.costwrap{position:relative;margin:14px 0 6px;border:1px solid #cbb588;
  background:linear-gradient(180deg,#f3e8cf 0%,#ece0c6 100%);overflow:hidden;height:138px}
.costwrap:after{content:"";position:absolute;left:0;right:0;bottom:0;height:1px;background:#d8c7a4;z-index:1}
.costgraph{position:absolute;inset:0;width:100%;height:100%;display:block;
  transition:opacity .28s ease;animation:costin .55s ease both}
@keyframes costin{from{opacity:0}to{opacity:1}}
.costline{stroke:var(--ember);stroke-width:2.2;fill:none;stroke-linejoin:miter;stroke-linecap:butt}
#costreveal.reveal-once{animation:costreveal .9s ease forwards}
@keyframes costreveal{from{clip-path:inset(0 100% 0 0)}to{clip-path:inset(0 0 0 0)}}
.costarea{fill:url(#costfill);stroke:none}
.costbase{stroke:#cbb588;stroke-width:1;stroke-dasharray:2 5;vector-effect:non-scaling-stroke;opacity:.6}
.costguide{stroke:#b79b6a;stroke-width:1;vector-effect:non-scaling-stroke;opacity:.26}
.costguide.now{stroke:var(--ember2);opacity:.5;stroke-dasharray:2.5 2.5}
.costhover{stroke:var(--ember2);stroke-width:1;vector-effect:non-scaling-stroke;opacity:.65;pointer-events:none}
.costdot{position:absolute;width:8px;height:8px;border-radius:50%;background:var(--ember);
  border:1px solid #fff0d0;box-shadow:0 0 0 2px rgba(207,89,21,.18),0 2px 8px rgba(42,29,16,.24);
  transform:translate(-50%,-50%);z-index:4;pointer-events:none;display:none}
.costtip{position:absolute;min-width:72px;padding:6px 8px;border:1px solid rgba(255,232,184,.28);
  background:#261d14;color:#f5ead4;box-shadow:0 8px 18px rgba(42,29,16,.28);
  font:10px/1.35 "JetBrains Mono",monospace;letter-spacing:.01em;z-index:5;pointer-events:none;display:none}
.costtip b{display:block;color:#fff5dd;font-weight:600}
.costtip span{display:block;color:#e99a55}
.costaxis{position:absolute;left:0;right:0;bottom:4px;height:13px;z-index:2;pointer-events:none}
.costaxis span{position:absolute;transform:translateX(-50%);font:9px "Saira Condensed",sans-serif;
  letter-spacing:.1em;text-transform:uppercase;color:var(--faint);white-space:nowrap}
.costaxis span.now{color:var(--ember2);font-weight:700}
.costgrid{stroke:#b79b6a;stroke-width:1;vector-effect:non-scaling-stroke;opacity:.16}
.costgridlab{position:absolute;inset:0;z-index:2;pointer-events:none}
.costgridlab span{position:absolute;right:7px;transform:translateY(-50%);font:9px "JetBrains Mono",monospace;color:var(--faint);opacity:.8}
.costface{position:relative;z-index:2;display:flex;align-items:flex-start;justify-content:space-between;
  gap:14px;padding:14px 20px 22px;height:100%;pointer-events:none}
.costface .costtoggle{pointer-events:auto}
.costmeta{display:flex;flex-direction:column;gap:4px}
.costmeta .l{font-family:"Saira Condensed",sans-serif;text-transform:uppercase;letter-spacing:.26em;
  font-size:10px;color:var(--dim)}
.costmeta .l span{color:var(--ember2)}
.costnum{font-family:"Bodoni Moda",serif;font-weight:600;font-variant-numeric:tabular-nums;
  font-size:clamp(40px,6.2vw,58px);line-height:.9;color:#2a1d10;text-shadow:0 1px 0 rgba(255,250,238,.6)}
.costnum .cur{font-size:.46em;color:var(--dim);vertical-align:.42em;margin-right:3px;font-weight:600}
.costsub{font:11px "JetBrains Mono",monospace;color:var(--dim);letter-spacing:.02em}
.costsub b{color:#4a3925;font-weight:600}
.costtoggle{display:inline-flex;border:1px solid #c2aa78;background:#fbf3e1;align-self:flex-start;
  box-shadow:0 1px 0 rgba(255,250,238,.5)}
.costtoggle button{appearance:none;cursor:pointer;border:0;border-left:1px solid #ddcaa0;background:transparent;
  font:700 11px "Saira Condensed",sans-serif;letter-spacing:.16em;text-transform:uppercase;color:#6b5638;
  padding:6px 11px;transition:background .15s,color .15s}
.costtoggle button:first-child{border-left:0}
.costtoggle button.on{background:#33271a;color:#f1e7d2}
.costtoggle button:hover:not(.on){background:#f1e3c6;color:#2a1d10}
@media(max-width:640px){.costwrap{height:124px}.costnum{font-size:36px}.costtoggle button{padding:6px 8px;letter-spacing:.1em}.costface{padding:12px 14px}}

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
.sec .ko{font-family:"Saira Condensed",sans-serif;letter-spacing:.26em;text-transform:uppercase;font-size:10.5px;color:var(--dim)}
.sec .rule{flex:1;height:1px;position:relative;
  background:repeating-linear-gradient(90deg,#bfa779 0 2px,transparent 2px 7px,#d8c7a4 7px 8px,transparent 8px 13px)}
.sec .rule:after{content:"※";position:absolute;right:0;top:50%;transform:translateY(-52%);
  color:var(--faint);background:var(--ink);padding-left:9px;font:12px "Bodoni Moda",serif}

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
.chip{font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:#5f513d;border:1px solid #d8c7a4;padding:2px 7px;background:#f8efd9}
.stage{font-size:10px;letter-spacing:.1em;text-transform:uppercase;color:#6b5638;border:1px solid #d8c7a4;padding:2px 7px;background:#f5ead3}
.signal{font-size:10px;letter-spacing:.08em;text-transform:uppercase;color:#5f513d;border:1px solid #d4c19b;padding:2px 7px;background:#f8efd9}
.signal.med{color:#7d5b12;border-color:#d8bb72;background:#fbf0cb}.signal.high{color:#8f2b1e;border-color:#d99b8d;background:#fae4dc}
.errbadges{display:flex;gap:5px;flex-wrap:wrap}.errbadge{font-size:10px;letter-spacing:.09em;text-transform:uppercase;border:1px solid #d8c7a4;padding:2px 6px;background:#fff8e9;color:#5a4631}
.errbadge.exit{color:var(--bad);border-color:#d9a193;background:#fae4dc}.errbadge.timeout{color:#805100;border-color:#d6b65e;background:#fbefc3}
.errbadge.permission,.errbadge.sandbox{color:#6b5638;border-color:#d8c7a4;background:#f5ead3}.errbadge.network{color:#5f513d;border-color:#d8c7a4;background:#f8efd9}.errbadge.jsonl{color:#8d3f13;border-color:#dfa16c;background:#f7e5d4}
.age.zombie,.age.error,.age.killed,.age.interrupted{color:var(--bad);font-weight:700}
.el{margin-left:auto;font-variant-numeric:tabular-nums;color:var(--ember2);font-weight:700;font-size:15px}
.el .el-last.flash{display:inline-block;animation:elapsedflash .24s ease}
@keyframes elapsedflash{0%{color:var(--paper);opacity:.52}45%{color:var(--ember);opacity:1}100%{color:var(--ember2);opacity:1}}
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
.ic.cmd{color:var(--dim)} .ic.edit{color:var(--ember2)} .ic.msg{color:var(--dim)} .ic.err{color:var(--bad)} .ic.out{color:var(--faint)}
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
.wline.new{position:relative;overflow:hidden;animation:rise .4s ease,wirestamp .8s ease-out}
.wline.new:before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--ember);
  transform-origin:top;animation:wirewipe .75s ease-out forwards}
.wline.new.err{animation:rise .4s ease,wirestampErr .9s ease-out}
.wline.new.err:before{width:4px;background:var(--bad)}
@keyframes wirestamp{0%{background:#f1e3c6}100%{background:transparent}}
@keyframes wirestampErr{0%{background:#f2b39f}55%{background:#fae5dc}100%{background:transparent}}
@keyframes wirewipe{0%{transform:scaleY(0);opacity:1}38%{transform:scaleY(1);opacity:1}100%{transform:scaleY(1);opacity:0}}
.wline .wt{color:var(--faint);font-size:10.5px;min-width:64px;font-variant-numeric:tabular-nums}
.wline .src{color:var(--ember);font-size:10px;text-transform:uppercase;letter-spacing:.1em;min-width:88px;
  overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.wline .wx{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#3a2c1c;font-size:11.5px}
.wline.cmd .wi{color:var(--dim)} .wline.edit .wi{color:var(--ember2)} .wline.msg .wi{color:var(--dim);font-style:italic}
.wline.err{background:#fae5dc}.wline.err .wi,.wline.err .wx{color:var(--bad);font-weight:700}.wline.out{opacity:.55}
.wline.clip{cursor:pointer}
.wline.clip:hover{background:#f1e3c6}
.wline.expanded{background:#f3ead6;align-items:flex-start}
.wline.expanded .wx{white-space:normal;overflow:visible;text-overflow:clip;word-break:break-word}
.wi{width:14px;flex:none;text-align:center}

.rec{position:relative;display:flex;gap:12px;align-items:baseline;padding:9px 12px 9px 0;border-bottom:1px solid var(--recent-rule,#ecddc1);
  transition:background .16s ease,box-shadow .16s ease}
.rec:nth-child(even){background:var(--recent-tint,#f8f1e2)}
.rec:hover{background:var(--recent-hover,#f1e3c6);box-shadow:inset 2px 0 0 var(--ember)}
.rec .idx{width:30px;flex:0 0 30px;text-align:right;color:var(--faint);font:500 10px "JetBrains Mono",monospace;
  font-variant-numeric:tabular-nums;opacity:.78}
.rec .ago{color:var(--faint);min-width:58px;text-align:right;font-variant-numeric:tabular-nums;font-size:11px}
.rec .src{color:var(--ember);font-size:10px;text-transform:uppercase;letter-spacing:.12em;min-width:84px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.rec .p{flex:1;min-width:0;color:#4a3a26;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px}
.rec .n{color:var(--dim);font-size:10.5px;white-space:nowrap;font-variant-numeric:tabular-nums}
.rec.error .p,.rec.error .n{color:var(--bad)}
.rec.killed .p,.rec.killed .n{color:var(--kill);font-weight:700}.rec.interrupted .p,.rec.interrupted .n{color:var(--interrupt);font-weight:700}
#recent{position:relative;--recent-rule:#ecddc1;--recent-tint:#f8f1e2;--recent-hover:#f1e3c6}
.recent-control{margin-left:auto;display:inline-flex;align-items:center;gap:7px}
.recent-control span{font:700 10px "Saira Condensed",sans-serif;letter-spacing:.18em;text-transform:uppercase;color:var(--dim)}
.recent-select{height:30px;border:1px solid #c9b382;background:#fff8e9;color:var(--paper);
  font:12px "JetBrains Mono",monospace;padding:0 9px;box-shadow:0 1px 0 rgba(255,250,238,.5)}
.recent-list{position:relative;overflow:hidden;transition:max-height .36s ease}
.rec.recent-new{animation:recentfade .34s ease both}
@keyframes recentfade{from{opacity:0;transform:translateY(-5px)}to{opacity:1;transform:translateY(0)}}
.recent-more{position:relative;margin-top:-36px;padding-top:44px;pointer-events:none}
.recent-fade{position:absolute;left:0;right:0;top:0;height:40px;
  background:linear-gradient(180deg,rgba(241,231,210,0) 0%,rgba(241,231,210,.78) 62%,var(--ink) 100%)}
.recent-collapse{display:flex;justify-content:center;padding:12px 0 2px}
.recent-continued{position:relative;z-index:1;width:100%;display:flex;align-items:center;justify-content:center;gap:10px;
  border:0;background:transparent;color:var(--dim);cursor:pointer;pointer-events:auto;padding:7px 0;
  font:700 11px "Saira Condensed",sans-serif;letter-spacing:.18em;text-transform:uppercase;
  transition:color .15s ease}
.recent-continued:before,.recent-continued:after{content:"";height:1px;flex:1;
  background:linear-gradient(90deg,transparent,var(--line) 26%,var(--line) 74%,transparent)}
.recent-continued:hover{color:var(--ember)}
.recent-continued:focus-visible{outline:2px solid var(--ember);outline-offset:2px}
.recent-chevron{display:inline-flex;align-items:center;justify-content:center;color:var(--ember)}
.recent-chevron svg{width:20px;height:20px}
.recent-continued .label{color:inherit}
.recent-continued .remain{color:var(--faint);font:500 10px "JetBrains Mono",monospace;letter-spacing:.04em;text-transform:none}
.empty{color:var(--faint);font-style:italic;font-family:"Bodoni Moda",serif;padding:22px 4px;font-size:15px;text-align:center;
  position:relative;letter-spacing:.02em}
.empty:before,.empty:after{content:"";display:block;height:1px;margin:0 0 12px;
  background:repeating-linear-gradient(90deg,#cbb588 0 2px,transparent 2px 7px);opacity:.72}
.empty:after{margin:12px 0 0}

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
::-webkit-scrollbar{width:7px;height:7px}::-webkit-scrollbar-thumb{background:#cf5915}::-webkit-scrollbar-track{background:transparent}
@media(max-width:640px){
  body{padding-inline:12px}.brand{display:block}.mastmeta{justify-content:flex-start}.dateline{text-align:left}.controls input{min-width:100%}.controls input.mini{min-width:54px;width:54px;flex:0}.controls .switch input{min-width:0;width:1px;height:1px;flex:0}.refresh-note{margin-left:0;width:100%}
  .stat{min-width:50%;border-bottom:1px solid #d3c096}.sec{gap:10px}.recent-control{margin-left:auto}.rec{gap:8px;padding-right:8px}.rec .idx{width:24px;flex-basis:24px}.rec .src{min-width:64px}.acts{width:100%}.el{margin-left:0}
}
</style></head><body>

<div class=mast>
  <div class=brand>
    <h1>Codex<b>·</b>Wire</h1>
    <span class=sub>Live Dispatch Ledger</span>
  </div>
  <div class=dateline><span class=onair><span id=lamp class=lamp></span> <b id=onair>OFF AIR</b></span><br><span id=date>—</span> · <b id=ts>—</b><br><span id=refresh_state>polling</span><br><span id=bulletin class=bulletin>ALL CLEAR</span></div>
</div>
<div class=mastmeta aria-label="publication metadata">
  <span>EST. ~/.codex</span><span class=dot>·</span>
  <span>VOL. <b id=meta_date>—</b></span><span class=dot>·</span>
  <span>NO. <b id=meta_no>—</b></span><span class=dot>·</span>
  <span>EX MACHINA · NUNTIUS</span>
</div>

<div id=wirebanner class=wirebanner></div>

<div class=strip>
  <div class=stat title="running codex processes from ps"><div class=l>Live</div><div class=v id=s_run>—</div></div>
  <div class=stat title="오늘 생성된 codex 세션 파일 수"><div class=l>Sessions today</div><div class=v id=s_today>—</div></div>
  <div class=stat><div class=l>Rate<span class=ratetog id=rate_toggle role=group aria-label="rate timeframe"><button type=button data-rw=5h class=on aria-pressed=true>5h</button><button type=button data-rw=7d aria-pressed=false>7d</button></span></div><div class=v id=s_rate>—<small>%</small></div><div class=gauge><i id=s_rate_bar style=width:0%></i></div></div>
  <div class=stat title="feed from sessions active in last 30m, capped at 80 lines"><div class=l>Wire feed</div><div class=v id=s_feed>—</div></div>
</div>

<div class=costwrap id=costwrap>
  <svg class=costgraph id=costgraph viewBox="0 0 100 100" preserveAspectRatio=none aria-hidden=true>
    <defs>
      <linearGradient id=costfill x1=0 y1=0 x2=0 y2=1>
        <stop offset="0%" stop-color="#cf5915" stop-opacity="0.34"/>
        <stop offset="55%" stop-color="#cf5915" stop-opacity="0.10"/>
        <stop offset="100%" stop-color="#cf5915" stop-opacity="0"/>
      </linearGradient>
    </defs>
    <g id=costgridh></g>
    <g id=costguides></g>
    <g id=costreveal>
      <path id=costarea class=costarea d=""/>
      <path id=costline class=costline d="" vector-effect=non-scaling-stroke pathLength="1"/>
    </g>
    <line id=costhover class=costhover x1="0" y1="0" x2="0" y2="100" visibility=hidden/>
  </svg>
  <div class=costdot id=costdot></div>
  <div class=costtip id=costtip></div>
  <div class=costaxis id=costaxis></div>
  <div class=costgridlab id=costgridlab></div>
  <div class=costface>
    <div class=costmeta>
      <div class=l>Codex spend · <span id=cost_range_label>5h session</span></div>
      <div class=costnum><span class=cur>$</span><span id=cost_amount>0.00</span></div>
      <div class=costsub><b id=cost_tokens>—</b> tok · peak <b id=cost_peak>$0.00</b><span id=cost_estflag></span></div>
    </div>
    <div class=costtoggle id=cost_toggle role=group aria-label="cost timeframe">
      <button type=button data-range=5h class=on aria-pressed=true>5H</button>
      <button type=button data-range=day aria-pressed=false>Day</button>
      <button type=button data-range=week aria-pressed=false>Wk</button>
      <button type=button data-range=month aria-pressed=false>Mo</button>
      <button type=button data-range=year aria-pressed=false>Yr</button>
    </div>
  </div>
</div>

<div class=controls>
  <label>dir</label><select id=f_dir><option value="">all</option></select>
  <label>sandbox</label><select id=f_sandbox><option value="">all</option></select>
  <label>status</label><select id=f_status><option value="">all</option><option>running</option><option value=zombie>stale</option><option>error</option><option>done</option><option>killed</option><option>interrupted</option></select>
  <label>sort</label><select id=f_sort><option value=elapsed>elapsed</option><option value=edits>edits</option><option value=tokens>tokens</option><option value=activity>last activity</option></select>
  <input id=f_q placeholder="prompt · file · pid search">
  <label>poll</label><select id=poll_ms><option value=1000>1s</option><option value=2000 selected>2s</option><option value=5000>5s</option><option value=10000>10s</option></select>
  <button id=refresh_btn>refresh</button><button id=compact_btn>compact</button><button id=clear_state_btn>clear all</button>
  <div class=notify-panel id=notify_panel aria-label="notification controls">
    <label class="switch master"><input id=n_master type=checkbox><span class=track><span class=thumb></span></span><span class=switch-text><b>alerts</b><small>master</small></span></label>
    <label class=switch title="alert when a live Codex process has a stale activity signal"><input id=n_zombie type=checkbox><span class=track><span class=thumb></span></span><span class=switch-text><b>stale</b><small>signal</small></span></label>
    <label class=switch><input id=n_error type=checkbox><span class=track><span class=thumb></span></span><span class=switch-text><b>error</b><small>logs</small></span></label>
    <label class=switch><input id=n_rate type=checkbox><span class=track><span class=thumb></span></span><span class=switch-text><b>rate</b><small>limit</small></span></label>
    <label class=threshold title="rate alert percent"><span>at</span><input id=n_rate_limit class=mini type=number min=50 max=100 value=80><span>%</span></label>
    <span class=notify-sep></span>
    <label class=switch><input id=n_idle type=checkbox><span class=track><span class=thumb></span></span><span class=switch-text><b>idle</b><small>quiet</small></span></label>
    <label class=threshold title="idle alert minutes"><span>after</span><input id=n_idle_min class=mini type=number min=1 max=180 value=10><span>m</span></label>
  </div>
  <span class=refresh-note id=refresh_note>—</span>
</div>

<div class=sec><span class=ko title="running codex processes from ps">● Live processes</span><h2>On the Wire</h2><div class=rule></div></div>
<div class=grid id=running></div>

<div class=sec><span class=ko>Wire feed</span><h2>Live Telegraph</h2><div class=rule></div></div>
<div class=wire id=wire></div>

<div class=sec><span class=ko>Logbook</span><h2>Recent Dispatches</h2><div class=rule></div><label class=recent-control><span>Entries</span><select id=recent_limit class=recent-select aria-label="Recent dispatch count"><option value=10>10</option><option value=20>20</option><option value=30>30</option><option value=40>40</option><option value=50>50</option></select></label></div>
<div id=recent></div>

<footer><span>CODEX WIRE · ps + ~/.codex/sessions · controlled polling</span><span>by 3917</span></footer>
<div class=toast-stack id=toasts aria-live=polite aria-atomic=false></div>

<script>
const ICON={cmd:'●',edit:'✎',msg:'»',err:'!',out:'·'};
const STATUS_ICON={running:'●',zombie:'!',error:'×',done:'✓',killed:'■',interrupted:'!'};
function statusDisplay(j){return j&&j.status==='zombie'?'stale':((j&&j.status_label)||((j&&j.status)||''));}
function costNote(j){return j&&j.cost_estimate?' <small style=color:var(--dim)>est:fallback</small>':' <small style=color:var(--dim)>est</small>';}
function esc(s){return String(s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function hhmm(iso){try{const d=new Date(iso);return d.toLocaleTimeString('en-GB',{hour12:false});}catch(e){return '';}}
function fmtAge(s){if(s==null)return 'no events'; if(s<60)return s+'s ago'; const m=Math.floor(s/60); if(m<60)return m+'m ago'; return Math.floor(m/60)+'h '+(m%60)+'m ago';}
function fmtRecentAge(m){m=Math.floor(Number(m)||0); const mm=String(m%60).padStart(2,'0'); if(m>=1440)return Math.floor(m/1440)+'d '+Math.floor((m%1440)/60)+'h '+mm+'m'; if(m>=60)return Math.floor(m/60)+'h '+mm+'m'; return m+'m';}
function fmtTok(n){n=Number(n||0); if(n>=1000000)return (n/1000000).toFixed(1)+'M'; if(n>=1000)return Math.round(n/100)/10+'k'; return String(n);}
function setText(el,v){if(el&&el.textContent!==String(v??''))el.textContent=String(v??'');}
function setHTML(el,v){if(el&&el.innerHTML!==v)el.innerHTML=v;}
function syncPressedButtons(root, attr, value){
  const el=typeof root==='string'?document.querySelector(root):root;
  if(!el)return;
  [...el.querySelectorAll('button')].forEach(b=>{
    const on=b.dataset[attr]===value;
    b.classList.toggle('on',on);
    b.setAttribute('aria-pressed',on?'true':'false');
  });
}
function emptyHTML(text){return `<div class=empty>${esc(text)}</div>`;}
function formatElapsedHTML(value, flash=false){
  const s=esc(value||'');
  return s.replace(/(\d)$/,`<span class="el-last${flash?' flash':''}">$1</span>`);
}
function paintElapsed(el,value,flash=false){if(el)setHTML(el,formatElapsedHTML(value,flash));}
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
function renderRateStat(value, valueId, barId){
  const n=Number(value), ok=Number.isFinite(n);
  setHTML(document.getElementById(valueId),(ok?Math.round(n):'—')+'<small>%</small>');
  const bar=document.getElementById(barId);
  if(!bar)return;
  const pct=ok?Math.min(100,Math.max(0,n)):0;
  bar.style.width=pct+'%';
  bar.classList.remove('rate-ok','rate-warn','rate-bad');
  if(ok)bar.classList.add(pct>85?'rate-bad':(pct>=70?'rate-warn':'rate-ok'));
}
let rateWin='5h';
try{const rw=localStorage.getItem('codex-wire-rate-win'); if(rw==='5h'||rw==='7d')rateWin=rw;}catch(e){}
function renderRate(d){renderRateStat(rateWin==='7d'?d.rate7d:d.rate,'s_rate','s_rate_bar');}
function setWireBanner(msg){
  const el=document.getElementById('wirebanner'); if(!el)return;
  if(msg){setText(el,msg);el.style.display='block';}
  else{setText(el,'');el.style.display='none';}
}
function renderBulletin(d){
  const c=(d&&d.status_counts)||{};
  const zombie=Number(c.zombie||0), error=Number(c.error||0);
  const el=document.getElementById('bulletin'); if(!el)return {zombie,error};
  if(zombie>0||error>0){
    setHTML(el,`<span class=stale>● ${zombie} STALE</span> · <span class=err>✕ ${error} ERROR</span>`);
  }else{
    setText(el,'ALL CLEAR');
  }
  return {zombie,error};
}
function setRateWin(w){
  if(w!=='5h'&&w!=='7d')return;
  rateWin=w;
  try{localStorage.setItem('codex-wire-rate-win',w);}catch(e){}
  syncPressedButtons('#rate_toggle','rw',w);
  if(latest)renderRate(latest);
}
const COST_LABEL={'5h':'5h session','day':'last 24h','week':'last 7 days','month':'last 30 days','year':'last 12 months'};
let costRange='5h';
try{const cr=localStorage.getItem('codex-wire-cost-range'); if(cr&&COST_LABEL[cr])costRange=cr;}catch(e){}
let _costHover={points:[],peak:1,blabels:[]}, costLineDrawn=false;
// Sharp (no smoothing) area path in a 0..100 viewBox; non-scaling stroke keeps
// the line crisp while the area stretches to the panel.
function costPath(points){
  const n=points.length;
  if(!n)return {line:'',area:''};
  const max=Math.max.apply(null,points.concat(0))||1;
  const W=100,H=100,top=14;                 // headroom so the peak isn't clipped
  const dx=n>1?W/(n-1):0;
  const pt=points.map((v,i)=>{
    const x=n>1?i*dx:W/2;
    const y=H-(Math.max(0,v)/max)*(H-top);
    return [Math.round(x*100)/100,Math.round(y*1000)/1000];
  });
  const line=pt.map((p,i)=>(i?'L':'M')+p[0]+' '+p[1]).join(' ');
  const area=(n>1?line:('M0 '+pt[0][1]+' L100 '+pt[0][1]))+
    ' L'+(n>1?W:100)+' '+H+' L'+(n>1?0:0)+' '+H+' Z';
  return {line,area};
}
function renderCost(d){
  const series=d&&d.cost_series; if(!series)return;
  const r=series[costRange]||series['5h']||{points:[],total:0,tokens:0};
  const pts=r.points||[];
  setText(document.getElementById('cost_amount'),Number(r.total||0).toFixed(2));
  setText(document.getElementById('cost_range_label'),COST_LABEL[costRange]||r.label||'');
  setText(document.getElementById('cost_tokens'),fmtTok(r.tokens||0));
  const peak=pts.length?Math.max.apply(null,pts):0;
  setText(document.getElementById('cost_peak'),'$'+Number(peak).toFixed(2));
  setText(document.getElementById('cost_estflag'),d.cost_estimate?' · est':'');
  _costHover={points:pts,peak:(pts.length?Math.max.apply(null,pts.concat(0)):0)||1,blabels:r.blabels||[]};
  const p=costPath(pts);
  const lEl=document.getElementById('costline'),aEl=document.getElementById('costarea'),rEl=document.getElementById('costreveal');
  if(lEl){
    lEl.setAttribute('d',p.line);
    if(p.line&&!costLineDrawn&&rEl){
      costLineDrawn=true; rEl.classList.add('reveal-once');
      setTimeout(()=>rEl.classList.remove('reveal-once'),950);
    }
  }
  if(aEl)aEl.setAttribute('d',p.area);
  renderCostAxis(r.axis);
  renderCostGrid(peak);
}
// horizontal cost gridlines at "nice" values below the peak (same y-map as costPath)
function costGrid(peak){
  if(!(peak>0))return [];
  const limit=peak*0.98;
  const mag=Math.pow(10,Math.floor(Math.log10(peak)))||1;
  const mult=[0.1,0.2,0.25,0.5,1,2,2.5,5,10];
  for(const m of mult){
    const step=m*mag;
    const n=Math.floor(limit/step);
    if(n>=2 && n<=5){
      const out=[];
      for(let v=step; v<limit && out.length<5; v+=step)out.push(v);
      return out;
    }
  }
  const rough=peak/3, fmag=Math.pow(10,Math.floor(Math.log10(rough)))||1, norm=rough/fmag;
  const step=(norm>=5?5:norm>=2?2:1)*fmag, out=[];
  for(let v=step; v<peak*0.94 && out.length<4; v+=step)out.push(v);
  return out;
}
function fmtCostShort(v){
  const one=Math.round(v*10)/10;
  if(v>=1)return Math.abs(one-Math.round(one))<0.001?String(Math.round(one)):one.toFixed(1);
  return v.toFixed(2);
}
function renderCostGrid(peak){
  const g=document.getElementById('costgridh'),lab=document.getElementById('costgridlab');
  const vals=costGrid(peak),H=100,top=14;
  const y=v=>H-(v/peak)*(H-top);
  if(g)g.innerHTML=vals.map(v=>`<line class=costgrid x1="0" y1="${y(v).toFixed(2)}" x2="100" y2="${y(v).toFixed(2)}"/>`).join('');
  if(lab)lab.innerHTML=vals.map(v=>`<span style="top:${y(v).toFixed(2)}%">$${fmtCostShort(v)}</span>`).join('');
}
function renderCostAxis(axis){
  const g=document.getElementById('costguides'),lab=document.getElementById('costaxis');
  if(!Array.isArray(axis)){if(g)g.innerHTML='';if(lab)lab.innerHTML='';return;}
  if(g)g.innerHTML=axis.map(a=>{const x=(a.x*100).toFixed(2);const now=a.label==='now';
    return `<line class="costguide${now?' now':''}" x1="${x}" y1="0" x2="${x}" y2="100"/>`;}).join('');
  if(lab)lab.innerHTML=axis.map(a=>{const now=a.label==='now';let style;
    if(a.x<=0.02)style='left:4px';else if(a.x>=0.98)style='left:auto;right:4px;transform:none';
    else style='left:'+(a.x*100).toFixed(2)+'%';
    return `<span class="${now?'now':''}" style="${style}">${esc(a.label)}</span>`;}).join('');
}
function hideCostHover(){
  const h=document.getElementById('costhover'),dot=document.getElementById('costdot'),tip=document.getElementById('costtip');
  if(h)h.setAttribute('visibility','hidden');
  if(dot)dot.style.display='none';
  if(tip)tip.style.display='none';
}
function moveCostHover(e){
  const wrap=document.getElementById('costwrap'),data=_costHover||{},pts=data.points||[];
  if(!wrap||!pts.length)return hideCostHover();
  const rect=wrap.getBoundingClientRect();
  if(!rect.width||!rect.height)return hideCostHover();
  const fx=Math.min(1,Math.max(0,(e.clientX-rect.left)/rect.width));
  const n=pts.length, i=Math.min(n-1,Math.max(0,Math.round(fx*(n-1))));
  const x=n>1?i/(n-1)*100:50,H=100,top=14,peak=data.peak||1;
  const y=H-(Math.max(0,Number(pts[i])||0)/peak)*(H-top);
  const h=document.getElementById('costhover'),dot=document.getElementById('costdot'),tip=document.getElementById('costtip');
  if(h){h.setAttribute('x1',x.toFixed(2));h.setAttribute('x2',x.toFixed(2));h.setAttribute('visibility','visible');}
  if(dot){dot.style.left=x.toFixed(2)+'%';dot.style.top=y.toFixed(2)+'%';dot.style.display='block';}
  if(tip){
    tip.innerHTML=`<b>${esc((data.blabels||[])[i]||'')}</b><span>$${Number(pts[i]||0).toFixed(2)}</span>`;
    tip.style.display='block';
    const tw=tip.offsetWidth||80, th=tip.offsetHeight||38, pad=8;
    const px=x/100*rect.width, py=y/100*rect.height;
    const left=Math.min(rect.width-tw-pad,Math.max(pad,px-tw/2));
    const above=py-th-12, topPx=above>=pad?above:Math.min(rect.height-th-pad,py+12);
    tip.style.left=left+'px';
    tip.style.top=Math.max(pad,topPx)+'px';
  }
}
function setCostRange(range){
  if(!COST_LABEL[range])return;
  costRange=range;
  try{localStorage.setItem('codex-wire-cost-range',range);}catch(e){}
  syncPressedButtons('#cost_toggle','range',range);
  hideCostHover();
  const g=document.getElementById('costgraph');
  if(g){g.style.opacity='0';setTimeout(()=>{if(latest)renderCost(latest);g.style.opacity='';},120);}
  else if(latest)renderCost(latest);
}
let ticks={}, seen=new Set(), latest=null, pollMs=2000, timer=null, lastOk=0;
let expanded=new Set(), promptOpen=new Set(), pins=new Set();
let orderSeed={}, orderSeq=0, controlState={}, statusSeen={}, errorSeen={}, idleSeen={}, alertsPrimed=false, rateHot=false;
const STORE='codex-wire-state-v2';
const NOTIFY_STORE='codex-wire-notify-v3';
const NOTIFY_DEFAULTS={master:false,zombie:false,error:false,rate:false,rateLimit:80,idle:false,idleMin:10};
const RECENT_OPTIONS=[10,20,30,40,50], RECENT_STEP=10, RECENT_MAX=50, RECENT_STORE='codex-wire-recent-n';
let recentN=10, recentRevealFrom=null;
try{const rn=Number(localStorage.getItem(RECENT_STORE)); if(RECENT_OPTIONS.includes(rn))recentN=rn;}catch(e){}

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
  return `<span class=m><b>${j.n_cmds||0}</b>cmds</span><span class=m><b>${j.n_edits||0}</b>edits</span><span class=m><b>${fmtTok(j.token_total)}</b>tok</span><span class=m><b>$${Number(j.cost||0).toFixed(3)}</b>${costNote(j)}</span>${j.rate_pct!=null?`<span class=m><b>${Math.round(j.rate_pct)}</b><small style=color:var(--dim)>% rate</small></span>`:''}`;
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
  return `<span class="signal ${cls}" title="${esc(a.reason||'stale signal risk')}">${esc(a.label||'live')} · signal ${c}% · ${esc(bits.join(' · '))}</span>`;
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
     <span class="pill ${j.status}" title="${esc(j.status_label||j.status)}"><span class=d></span>${STATUS_ICON[j.status]||'·'} ${esc(statusDisplay(j))} ${esc(j.pid)}</span>
     <span class=kv>dir <b>${esc(j.cwd)}</b></span>
     <span class="kv prompt-head">${esc((j.prompt||'').replace(/\s+/g,' ').slice(0,90))}</span>
     <span class=chip>${esc(j.sandbox)}</span><span class=stage>${esc(j.stage)}</span>${activityHTML(j)}${errBadges(j)}
     <span class="kv age ${j.status}">last ${fmtAge(j.last_age_sec)}</span>
     <span class=el id=el_${esc(j.pid)}>${formatElapsedHTML(j.elapsed)}</span>
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
    const msg=(latest&&latest.running&&latest.running.length>0)?'NO MATCH — 필터에 맞는 작업 없음':'── NO DISPATCHES ON THE WIRE ──';
    if(!empty){empty=document.createElement('div');empty.className='empty';R.appendChild(empty);}
    setText(empty,msg);
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
  if(!feed.length){ seen.clear(); setHTML(W,emptyHTML('── THE WIRE IS QUIET ──')); return; }
  const ph=W.querySelector('.empty'); if(ph) ph.remove();
  const fresh=feed.filter(e=>{const k=e.ts+'|'+e.k+'|'+e.t+'|'+e.src; if(seen.has(k))return false; seen.add(k); return true;});
  for(let i=fresh.length-1;i>=0;i--){const e=fresh[i];
    const div=document.createElement('div'); div.className='wline new '+e.k;
    div.innerHTML=`<span class=wt>${hhmm(e.ts)}</span><span class=wi>${ICON[e.k]||'·'}</span><span class=src>${esc(e.src)}</span><span class=wx>${esc(e.t)}</span>`;
    W.insertBefore(div, W.firstChild);
    setTimeout(()=>div.classList.remove('new'),1200);
    const wx=div.querySelector('.wx'); if(wx && wx.scrollWidth>wx.clientWidth+1) div.classList.add('clip');
  }
  while(W.children.length>80) W.removeChild(W.lastChild);
  if(seen.size>600){ seen=new Set(feed.map(e=>e.ts+'|'+e.k+'|'+e.t+'|'+e.src)); }
}
function recentChevron(dir){
  return dir==='up'
    ? '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m7 18 5-5 5 5"/><path d="m7 11 5-5 5 5"/></svg>'
    : '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m7 6 5 5 5-5"/><path d="m7 13 5 5 5-5"/></svg>';
}
function normalizeRecentN(n){
  n=Number(n)||RECENT_STEP;
  return RECENT_OPTIONS.reduce((best,v)=>Math.abs(v-n)<Math.abs(best-n)?v:best,RECENT_STEP);
}
function saveRecentN(){try{localStorage.setItem(RECENT_STORE,String(recentN));}catch(e){}}
function syncRecentSelect(total){
  const sel=document.getElementById('recent_limit'); if(!sel)return;
  sel.value=String(recentN);
  sel.disabled=!(total>0);
  sel.title=total>0?String(Math.min(total,RECENT_MAX))+' dispatches available':'No dispatches';
  sel.setAttribute('aria-label','Recent dispatch count');
}
function setRecentN(n,reveal=false){
  const old=recentN, next=normalizeRecentN(n);
  if(next===old)return;
  recentN=next; saveRecentN();
  if(reveal&&latest&&latest.recent)recentRevealFrom=Math.min(old,latest.recent.length,RECENT_MAX);
  if(latest)renderRecent(latest.recent);
}
function renderRecent(rows){
  const RE=document.getElementById('recent');
  const total=Math.min((rows||[]).length,RECENT_MAX), shown=Math.min(recentN,total);
  syncRecentSelect((rows||[]).length);
  if(!total){setHTML(RE,emptyHTML('── LOGBOOK EMPTY ──'));return;}
  const oldList=RE.querySelector('.recent-list'), oldHeight=oldList?oldList.getBoundingClientRect().height:0;
  const recs=(rows||[]).slice(0,shown).map((s,i)=>`<div class="rec ${s.status}">
     <span class=idx>${i+1}</span><span class=ago>${fmtRecentAge(s.age_min)}</span><span class=src>${esc(s.cwd)}</span>
     <span class=p>${esc(s.prompt)}</span>
     <span class=n>${esc(statusDisplay(s))} · ${s.n_cmds}c · ${s.n_edits}e · ${fmtTok(s.token_total)}t · $${Number(s.cost||0).toFixed(3)}${s.cost_estimate?' est:fallback':''}${s.rate_pct!=null?' · '+Math.round(s.rate_pct)+'%':''}</span></div>`).join('');
  const hasMore=shown<total&&shown<RECENT_MAX;
  const canCollapse=shown>RECENT_STEP&&!hasMore;
  const more=hasMore?`<div class=recent-more><div class=recent-fade></div><button class=recent-continued data-recent-more type=button aria-label="Show 10 more dispatches, ${total-shown} remaining" aria-expanded="false" title="show 10 more"><span class=recent-chevron>${recentChevron('down')}</span><span class=label>SHOW 10 MORE</span><span class=remain>· ${total-shown} more</span></button></div>`:'';
  const collapse=canCollapse?`<div class=recent-collapse><button class=recent-continued data-recent-collapse type=button aria-label="Collapse recent dispatches" aria-expanded="true" title="collapse to 10"><span class=recent-chevron>${recentChevron('up')}</span><span class=label>COLLAPSE</span></button></div>`:'';
  setHTML(RE,`<div class=recent-list>${recs}</div>${more}${collapse}`);
  const list=RE.querySelector('.recent-list'), newHeight=list?list.scrollHeight:0;
  if(list&&oldHeight&&Math.abs(newHeight-oldHeight)>1){
    list.style.maxHeight=oldHeight+'px';
    requestAnimationFrame(()=>{list.style.maxHeight=newHeight+'px';});
    setTimeout(()=>{if(list.isConnected)list.style.maxHeight='none';},420);
  }else if(list){
    list.style.maxHeight='none';
  }
  if(list&&recentRevealFrom!=null){
    [...list.querySelectorAll('.rec')].forEach((el,i)=>{
      if(i>=recentRevealFrom){el.classList.add('recent-new');el.style.animationDelay=Math.min((i-recentRevealFrom)*24,180)+'ms';}
    });
    recentRevealFrom=null;
  }
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
    if(o.zombie&&j.status==='zombie'&&statusSeen[k]!=='zombie')alertUser('zombie:'+k,'CODEX stale signal',name+' · '+((j.activity||{}).label||'silent'),'bad');
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
  document.body.classList.remove('offline');
  setWireBanner(d.source_degraded&&d.source_degraded.ps?'STOP PRESS — WIRE DEGRADED':'');
  const counts=renderBulletin(d);
  setText(document.getElementById('ts'),d.ts); setText(document.getElementById('date'),d.date);
  setText(document.getElementById('meta_date'),d.date); setText(document.getElementById('meta_no'),d.today);
  document.getElementById('lamp').className=counts.error>0?'lamp bad':('lamp'+(d.count>0?' on':''));
  setText(document.getElementById('onair'),d.count>0?'ON AIR':'STANDBY');
  document.getElementById('onair').style.color=counts.error>0?'var(--bad)':(d.count>0?'var(--ember)':'var(--dim)');
  setText(document.getElementById('s_run'),d.count); setText(document.getElementById('s_today'),d.today);
  renderRate(d);
  setText(document.getElementById('s_feed'),d.feed.length);
  renderCost(d);
  renderControls(d); renderCards(); renderWire(d.feed); renderRecent(d.recent); maybeAlerts(d);
  setText(document.getElementById('refresh_note'),(manual?'manual · ':'')+'updated now');
 }catch(e){
  document.body.classList.add('offline');setWireBanner('LINE DOWN — 연결 끊김');
  document.getElementById('lamp').className='lamp bad';
  setText(document.getElementById('ts'),'connection lost');setText(document.getElementById('onair'),'LINE DOWN');
  document.getElementById('onair').style.color='var(--bad)';
 }
}
function startPoll(){if(timer)clearInterval(timer);timer=setInterval(tick,pollMs);}
setInterval(()=>{for(const pid in ticks){const el=document.getElementById('el_'+pid);if(!el)continue;
  let p=ticks[pid].split(':').map(Number);let s=p.pop()+1;let m=p.pop()||0;let h=p.pop()||0;
  if(s>59){s=0;m++;}if(m>59){m=0;h++;}
  ticks[pid]=(h?String(h).padStart(2,'0')+':':'')+String(m).padStart(2,'0')+':'+String(s).padStart(2,'0');
  paintElapsed(el,ticks[pid],true);}
  if(lastOk){const age=Math.floor((Date.now()-lastOk)/1000);setText(document.getElementById('refresh_state'),age>pollMs/1000*3?'stale '+age+'s':'last refresh '+age+'s ago');}
},1000);
loadState();
['f_dir','f_sandbox','f_status','f_sort','f_q'].forEach(id=>document.getElementById(id).addEventListener('input',e=>{controlState[id]=e.target.value;saveState();renderCards();}));
document.getElementById('poll_ms').addEventListener('change',e=>{pollMs=Number(e.target.value);saveState();startPoll();tick(true);});
document.getElementById('refresh_btn').addEventListener('click',()=>tick(true));
document.getElementById('compact_btn').addEventListener('click',e=>{document.body.classList.toggle('compact');e.target.classList.toggle('on',document.body.classList.contains('compact'));saveState();});
document.getElementById('clear_state_btn').addEventListener('click',clearState);
document.getElementById('cost_toggle').addEventListener('click',e=>{const b=e.target.closest('button[data-range]');if(b)setCostRange(b.dataset.range);});
syncPressedButtons('#cost_toggle','range',costRange);
document.getElementById('costwrap').addEventListener('mousemove',moveCostHover);
document.getElementById('costwrap').addEventListener('mouseleave',hideCostHover);
document.getElementById('rate_toggle').addEventListener('click',e=>{const b=e.target.closest('button[data-rw]');if(b)setRateWin(b.dataset.rw);});
document.getElementById('recent_limit').addEventListener('change',e=>setRecentN(e.target.value,Number(e.target.value)>recentN));
document.getElementById('recent').addEventListener('click',e=>{
  const more=e.target.closest('[data-recent-more]'), collapse=e.target.closest('[data-recent-collapse]');
  if(more)setRecentN(Math.min(RECENT_MAX,recentN+RECENT_STEP),true);
  if(collapse)setRecentN(RECENT_STEP,false);
});
syncPressedButtons('#rate_toggle','rw',rateWin);
document.getElementById('wire').addEventListener('click',e=>{
  const line=e.target.closest('.wline'); if(!line)return;
  const wx=line.querySelector('.wx'); if(!wx)return;
  if(line.classList.contains('expanded')||wx.scrollWidth>wx.clientWidth+1){line.classList.add('clip');line.classList.toggle('expanded');}
});
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
        global _LAST_SNAPSHOT
        if self.path.startswith("/api"):
            try:
                data = snapshot()
                _LAST_SNAPSHOT = data
            except Exception as e:
                if _LAST_SNAPSHOT is not None:
                    data = dict(_LAST_SNAPSHOT)
                    data["degraded"] = True
                    data["api_error"] = str(e)
                else:
                    data = {"ok": False, "degraded": True, "api_error": str(e),
                            "ts": time.strftime("%H:%M:%S")}
            body = json.dumps(data, ensure_ascii=False).encode()
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
