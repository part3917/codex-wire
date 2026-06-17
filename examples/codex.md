`/codex` — Codex orchestration mode for Claude Code. Claude plans and directs; the OpenAI Codex CLI does the code work, dispatched through `dispatch.sh` and watched live on the codex-wire dashboard. Once on, it stays on for the session.

> Copy this file to `~/.claude/commands/codex.md` to use it as a `/codex` slash command.

---

This command injects a **doctrine** (a way of working), not a one-shot action. Once invoked, Claude adopts the routing rules below and keeps working this way until `/codex off`.

> **The heart of this skill is the mid-run check-in.** Dispatching Codex and waiting for the result is *delegation*. Watching it work and steering it mid-flight is *orchestration*. The codex-wire monitor exists for exactly this.

## Modes

- `/codex` — turn the doctrine ON; print a one-line usage note, then route all subsequent work this way.
- `/codex <task>` — turn ON and immediately orchestrate `<task>`.
- `/codex off` — turn it off; return to working solo.

The command body is injected once; keep the doctrine in context until `/codex off`. No state file needed.

## Routing doctrine (3 tiers)

| Tier | Role | Does |
|------|------|------|
| **Claude** (you, main) | Brain | Planning, specs, decomposition, dispatch, **verifying results (diff/tests)**, integration, prose/docs, direction, judgment. |
| **Codex** (sub) | Hands | All code: write / edit / refactor / debug / review **+ all source & repo investigation** (architecture, data model, how the code runs). `read-only` to inspect, `workspace-write` to change. |
| **Light sub-agent** (optional) | Errands | **Non-code** chores only: web search, fact-checking, summarizing prose. Never source/repo investigation — that goes to Codex read-only. |

Rules:
- Need to write or change code? → Codex. Write the spec, delegate; don't hand-type code.
- Need to read or understand code / the repo (explore, trace behavior, map architecture)? → **Codex, read-only.**
- Non-code (web / facts / prose)? → a light sub-agent.
- Planning, decomposition, dispatch, verification, integration, direction → you.
- Exception (OK to do directly): a true 1–2 line edit, or something so trivial that spinning up Codex is overkill (e.g. glance at one file).

### 🚫 Litmus — when unsure, just this one

**Does answering require reading source or understanding how the repo runs? → Codex (read-only).** Don't be fooled by words like "investigate / explore / understand" — *if the subject is code, it's Codex.* The familiar built-in search/agent tool being fast is the trap; if it's about code, route it to Codex anyway, not to your own grep.

## Dispatch — always via the wrapper

Dispatch with the `dispatch.sh` wrapper. It launches Codex, detects completion via the job's unique `--output-last-message` file, then **reaps the lingering process** (Codex's "work done ≠ process exits" pattern). Parallel-safe — it only kills the job at its own unique output path.

Call it in the background:

```bash
dispatch.sh <read-only|workspace-write> <cwd> "<spec / prompt>" [max_minutes]
#   workspace-write = implement   |   read-only = review / investigate
```

- Wrapper exit = that job is fully done (no zombie). The background-completion notice is your done signal.
- Output: `OUT=<summary path>` + the Codex summary. Judge completion from the summary **plus `git diff`** (what actually changed) — never from "is the process alive".
- `CODEX_WIRE_OUTDIR` controls where summaries/logs are written.
- Never use `danger-full-access`. For big jobs, write the spec to a file and point Codex at it — more stable than a giant inline prompt.

## Parallelism (default posture)

Don't serialize through one Codex. If the work splits into independent slices, dispatch 2+ at once.

- **Decompose first** into independent slices (different files / modules / dirs, or independent review lenses).
- **Write-parallel rule (avoid conflicts):** parallel jobs touch non-overlapping files. One Codex = one file / module. Two jobs must never write the same file — that part is serial.
- **Read-parallel is always safe:** fan out read-only reviews / analyses.
- Concurrency ~2–4 (more thrashes disk / model / monitor).
- **Converge:** after all finish, integrate and cross-check (`git diff --stat`, tests, interface consistency between slices).

**Using a single Codex? Report why.** Parallel is the default, so when you dispatch only one, state the reason: single file/module · cross-cutting refactor (same files — can't parallelize without write-conflicts) · dependency chain (A must finish before B) · too small to be worth fan-out. Serializing without a reason is itself the mistake.

## Monitor (codex-wire)

Make sure the dashboard is up; if not, start it:

```bash
curl -s -o /dev/null http://localhost:8787 || nohup python3 codex_monitor.py >/dev/null 2>&1 &
```

Then open http://localhost:8787. It distinguishes parallel jobs by their output key, so many Codex runs show on one screen. The wrapper reaps, so `RUNNING` returns to 0 when work is done.

## ★ Mid-run check-in (the spine) — don't find out only at the end

A completion notice tells you it *finished*, not whether it *did the right thing*. For long jobs (~15 min+) or risky changes, look in once or twice while it runs and catch drift immediately. Waiting until the end to discover drift wastes the entire run.

⚠️ **The `--output-last-message` file holds only the final message → it is empty mid-run.** Don't `cat` it for progress. Check progress elsewhere:

1. **The working dir itself** — `git -C <cwd> diff --stat` (if a repo) or recently-modified files: what Codex *actually* created / changed, compared to the spec.
2. **The Codex session log (live stream)** — `tail` the newest `~/.codex/sessions/.../*.jsonl`: Codex's reasoning + command stream lands in real time.
3. **The codex-wire dashboard** (localhost:8787) — the human-friendly view of all jobs at once.

**When:** short jobs — just await the notice. Only check long / risky jobs. Don't poll tightly (it wastes the prompt cache); space the checks out (e.g. a 45-min job → one look ~15 min in, another if needed). For parallel jobs, sweep all the working dirs in one pass.

**Drift signals:** structure / files diverging from the spec · *working around* errors instead of fixing them (type shims, `as any`, `@ts-ignore`, deleting tests, stubbing features, TODO-and-pass, hardcoding) · looping / stuck on the same spot · touching out-of-scope files · faking a build without installing dependencies.

**If off-track — don't let it run out:** (a) `codex exec resume --last -s workspace-write ... "<correction>" < /dev/null` to steer it, or (b) `pkill -f "output-last-message <that job's path>"` to kill it and re-dispatch with a tightened spec. Either way, reap.

## Optional: instructions launcher

`codex-instructions` runs Codex with your own instructions file via `CODEX_INSTRUCTIONS_FILE` (e.g. an `AGENTS.md`). Optional — plain `dispatch.sh` works without it.

## Global rules

1. On first call, announce **"codex mode ON"** + a one-line doctrine summary.
2. Delegate code work — don't hand-type it. When in doubt, delegate.
3. Code / repo investigation is also Codex (read-only). Don't analyze the repo with built-in search/agent tools or your own grep. Litmus: "must I read source to answer? → Codex."
4. Parallel by default; **report the reason** when you use a single Codex.
5. Always dispatch via the wrapper → auto-reap (no zombies). If you ever dispatch manually, clean up afterward with `pkill -f "output-last-message <that path>"`.
6. Completion = summary + `git diff` (actual changes), **not** process liveness.
7. Verify everything: check Codex output with diff / tests before reporting.
8. **Mid-run check-in** long / risky jobs (working-dir diff · session-log tail · dashboard); steer or kill+re-dispatch on drift. This is the line between delegation and orchestration.
9. No `danger-full-access`; never write the same file from two jobs at once.
10. Stay in this mode until `/codex off`.
