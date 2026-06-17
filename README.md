# codex-wire

English · [한국어](README.ko.md)

Live telemetry + dispatch wrapper for the OpenAI Codex CLI, driven by Claude Code. by 3917

![codex-wire dashboard](assets/00-dashboard.png)

## What It Does

- `dispatch.sh` runs one `codex exec` job, detects completion through its unique `--output-last-message` file, then reaps the lingering process that can remain after Codex has finished.
- `codex_monitor.py` is a stdlib-only web dashboard at `http://localhost:8787`. It reads `ps` plus `~/.codex/sessions` to show live jobs, recent sessions, activity, tokens, and dispatch state.
- `codex-instructions` is an optional launcher that points Codex at an instructions file via `CODEX_INSTRUCTIONS_FILE`.

## The Dashboard

A stdlib-only web UI at `http://localhost:8787` — no database, no dependencies. It polls `ps` and `~/.codex/sessions` on an interval and renders every delegated Codex job as it happens. Here is what each section does, top to bottom.

### Masthead & Stats

![Masthead and stat tiles](assets/01-masthead-stats.png)

The masthead shows the dashboard identity, the current date/time, and refresh freshness. The broadcast light reads **`ON AIR`** whenever at least one job is running, and **`STANDBY`** when nothing is live.

Below it, five at-a-glance tiles:

| Tile | Meaning |
|------|---------|
| **RUNNING** | Number of currently detected running `codex exec` jobs. |
| **TODAY** | Codex sessions started today (JSONL files under `~/.codex/sessions/YYYY/MM/DD/`). |
| **RATE 5H** | Highest rate-limit usage (`primary.used_percent`) seen across scanned recent sessions, as a percent + gauge. |
| **COST** | Total scanned tokens plus estimated cost, using the configured token pricing. |
| **WIRE LINES** | Number of event rows currently in the live feed. |

### Controls & Alerts

![Controls and toggles bar](assets/02-controls.png)

Filter, sort, and tune the view. Filters, sort, and polling apply to the running-job cards below.

| Control | What it does |
|---------|--------------|
| **DIR** | Filter cards by working directory. |
| **SANDBOX** | Filter by sandbox mode (`read-only` / `workspace-write`). |
| **STATUS** | Filter by status: running, zombie, error, done, killed, interrupted. |
| **SORT** | Order cards by elapsed, edits, tokens, or last activity (pinned cards stay first). |
| **search** | Text match across pid, cwd, sandbox, status, stage, prompt, commands, activity, errors, and file names. |
| **POLL** | Auto-refresh interval: 1s / 2s / 5s / 10s. |
| **REFRESH** | Fetch a fresh snapshot immediately. |
| **COMPACT** | Collapse card bodies into condensed rows. |
| **CLEAR ALL** | Reset filters, pins, compact state, alerts, and polling back to defaults. |

The alert toggles fire desktop notifications:

| Toggle | Fires when… |
|--------|-------------|
| **ALERTS** (master) | Master switch — when off, the toggles below are ignored. |
| **ZOMBIE** | A running job newly enters `zombie` status. |
| **ERROR** | A running job records new error-log output. |
| **RATE** (`AT __%`) | Max observed rate usage crosses your percent threshold. |
| **IDLE** (`AFTER __ M`) | A still-running job has been quiet for that many minutes. |

### On the Wire — running jobs

![On the Wire running cards](assets/03-on-the-wire.png)

A live grid of the Codex jobs running right now. Each card shows the status pill + pid, working directory, prompt, sandbox badge, stage, activity signal, last-event age, and a running elapsed timer — plus telemetry for `cmds`, `edits`, `tok`, estimated cost, and rate percent. The body streams recent command / edit / message / error / output events and the last agent message, with expandable detail. Card actions: **pin**, **copy cmd** (last or pending command), **retry** (relaunch the cwd + prompt as a new `codex exec`), and **kill**.

Empty state: *Idle — no Codex jobs match the filter.*

### Live Telegraph

![Live Telegraph and Recent Dispatches](assets/04-telegraph-dispatches.png)

A scrolling, real-time feed of session events across all jobs — commands, edits, messages, errors, and outputs as they land. Empty state: *Quiet wire.*

### Recent Dispatches

The logbook at the bottom of the same view (shown above): recently finished, non-running sessions with their status, age, source directory, prompt, command / edit counts, tokens, estimated cost, and rate percent. Empty state: *No history.*

## Prerequisites

- [Claude Code](https://claude.com/claude-code) — the intended driver that delegates jobs to Codex (`dispatch.sh` can also be run on its own).
- OpenAI Codex CLI installed.
- `codex login` completed.
- Python 3.

## Install

```bash
git clone https://github.com/part3917/codex-wire.git codex-wire
cd codex-wire
./install.sh
```

Manual setup:

```bash
mkdir -p ~/.codex
ln -sf "$PWD/dispatch.sh" ~/.codex/dispatch.sh
cp .env.example .env
```

Edit `.env` for your local paths and defaults.

## Usage

Run one delegated Codex job:

```bash
./dispatch.sh <read-only|workspace-write> <cwd> "<prompt>" [max_minutes]
```

Example:

```bash
./dispatch.sh workspace-write "$PWD" "Run the tests and fix any failures" 30
```

Run the monitor:

```bash
python3 codex_monitor.py
```

Then open `http://localhost:8787`.

Optional instructions launcher:

```bash
CODEX_INSTRUCTIONS_FILE=/path/to/your/AGENTS.md ./codex-instructions exec -C "$PWD" "Summarize this repo"
```

If `CODEX_INSTRUCTIONS_FILE` is unset or empty, `codex-instructions` runs `codex` without adding instructions.

## Claude Code Integration

`dispatch.sh` is meant to be called from Claude Code in the background to delegate coding work to Codex while Claude Code continues monitoring or coordinating other tasks.

A ready-made **`/codex` skill** — an orchestration doctrine (Claude plans, Codex codes, and you **check in mid-run** via this dashboard) — is in [`examples/codex.md`](examples/codex.md). Copy it to `~/.claude/commands/codex.md` to use it as a `/codex` slash command. Its core idea: dispatching and waiting is delegation; watching a job and steering it mid-flight is orchestration.

## Configuration

Copy `.env.example` to `.env` and edit values as needed. The main knobs are:

- `CODEX_INSTRUCTIONS_FILE`: optional instructions file for `codex-instructions`.
- `CODEX_WIRE_OUTDIR`: output directory for dispatch summaries and logs.
- `CODEX_MONITOR_*`: dashboard scan limits, stale thresholds, token cost estimates, bind host/port, and retry command.

## Attribution

Please retain the `by 3917` credit in the monitor UI and project materials.
