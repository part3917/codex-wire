# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
for 0.x releases.

## [Unreleased]

## [0.10.0] - 2026-06-26

### Performance

- Behavior-preserving optimizations (UI and `/api` output unchanged, verified via golden `/api` and incremental-parse self-tests):
  - Frontend: skip re-rendering the Stage donut, rate lines, and control selects when their values are unchanged; same-value write guards; build the stage tooltip skeleton once.
  - Backend snapshot: index running jobs by cwd (O(J·S)→O(S)); count `stage_counts`/`status_counts` in a single pass; lazy lowercasing in `_stage()`; precompiled JSONL regexes; reuse already-parsed session summaries for today's totals; combined output status/message file probe.
  - Rate RPC: `SimpleQueue` for the reader, removed redundant cache copies, reader-thread join hardening.
  - Incremental parser: reuse the head signature to avoid a redundant re-read.

## [0.9.1] - 2026-06-26

### Changed

- Stage distribution donut segments now animate smoothly (grow/shrink and shift) on value changes — segment nodes persist and update via `stroke-dasharray`/`stroke-dashoffset` with a CSS transition instead of being rebuilt each tick.

## [0.9.0] - 2026-06-26

### Added

- Added `stage_counts` to the `/api` snapshot, counting running jobs across `reading`, `analyzing`, `editing`, `verifying`, `starting`, and `idle` stages.
- Added a segmented Stage distribution donut with stage-specific colors, hover tooltips, and a persistent idle track when no jobs are running.

### Changed

- Replaced the top Wire feed stat card with the Stage distribution card while keeping the Live Telegraph feed stream panel unchanged.

## [0.8.0] - 2026-06-26

### Performance

- Added append-only incremental parsing for active session JSONL files, reusing cached parse state when file identity, size growth, and the head signature prove the log only grew.
- Preserved full reparse fallbacks for truncation, rewrite, head mismatch, malformed or incomplete records, identity changes, and platforms without reliable device/inode identity.

## [0.7.0] - 2026-06-26

### Performance

- Reused a persistent Codex `app-server` connection for live RPC rate-limit refreshes, initializing once per connection and repeating only `account/rateLimits/read` plus `account/read` until EOF, timeout, or failure forces a reconnect.
- Preserved the existing rate cache across transient RPC failures while retiring broken app-server process groups with `SIGTERM`, `SIGKILL`, and process kill fallback.

## [0.6.0] - 2026-06-26

### Performance

- Optimized `codex_monitor.py` backend hot paths while preserving dashboard output and `/api` values, including precompiled regexes, faster `ps` parsing, cached macOS process-argument handles, reduced session stat calls, fused snapshot counters, memoized pricing version, shared session discovery, single-pass cost bucketing, copy-on-write cost index handling, flatten memoization, shared cost grid data, and cached root HTML bytes.
- Reduced frontend redraw work with signature-based render skipping for today hours, cost graphs, recent dispatches, structured card patches, live plates, and stale tick cleanup.

### Changed

- Released the behavior-preserving performance pass as the `0.6.0` minor update, keeping the existing screen contract and `/api` snapshot semantics unchanged.

## [0.5.3] - 2026-06-26

### Fixed

- Hid the Live card agent stack when no agents are running so the idle plate remnant no longer appears beside the numeric count.

### Changed

- Standardized dashboard UI strings on English copy, including session-date tooltips, empty/offline states, expand/collapse labels, and server-local weekday abbreviations.

## [0.5.2] - 2026-06-26

### Fixed

- Hid the Wire feed donut ring entirely when the feed is empty so only the numeric count remains visible.

## [0.5.1] - 2026-06-26

### Changed

- Reduced the Live stack plate tilt so running-agent plates sit more upright.
- Made the Live stack visible plate count respond to available card width, keeping narrow layouts compact while showing more plates on wider cards.

## [0.5.0] - 2026-06-26

### Added

- Added the `today_hours` and `today_date` fields to the `/api` snapshot for server-local session timeline rendering.
- Added a Sessions today microbar with current-hour indicator, 4-hour scale labels, server-local date display, and per-hour hover tooltips.
- Added an 80-cap Wire feed donut gauge with warning color above 85% capacity.
- Added current Rate account and plan display in the Rate card.

### Changed

- Replaced the Live card's single on-air dot with a stacked frosted-glass plate visualization that scales with the number of running agents.
- Updated the Live stack layout so active-agent plates accumulate from the left and expand across the card area, with capped overflow labeling and an idle plate state.

## [0.4.0] - 2026-06-26

### Added

- Added live Codex JSON-RPC rate-limit reads through `account/rateLimits/read`, with a 45-second background cache and `account/read` account metadata.
- Added `rate_account`, `rate_plan`, `rate_source`, `rate_resets_at`, and `rate7d_resets_at` fields to the `/api` snapshot.
- Added simultaneous `5H` and `7D` rate-limit lines with reset times in the Rate card header.
- Added clone-local `.env` loading for `CODEX_MONITOR_*` settings and documented `CODEX_MONITOR_COST_INDEX_PATH`.
- Added localhost-only POST hardening with Host, Origin, Content-Type, body-size, and CSRF checks.

### Changed

- Replaced JSONL-first rate-limit reporting with live Codex RPC values so the dashboard matches Codex's authoritative account state.
- Improved JSONL rate fallback to prefer the maximum active `used_percent` within each live reset window, reducing spurious zero and stale-session readings.
- Improved dispatch output by printing machine-readable `STATUS`, `OUT`, and `LOG` headers.
- Improved large-session parsing, parser cache safety, storage error reporting, API fallback shape, polling timeouts, and frontend state normalization.
- Updated the rate source to show JSONL only as a fallback when live RPC data is unavailable.

### Fixed

- Fixed Rate card staleness caused by new-session JSONL samples reporting misleading zero usage.
- Fixed retry and kill actions with stricter local-only request handling and CSRF-protected POSTs.
- Fixed dispatch cleanup so it avoids signaling its own process group, validates cwd and timeout inputs, and waits for stable summary output before stopping.
- Fixed Recent Dispatches and Live Telegraph rendering edge cases for malformed or overflowing data.

## [0.3.0] - 2026-06-20

### Added

- Added masthead bulletins for stale and error counts, including `ALL CLEAR`, stale, and error states.
- Added degraded-source states for monitor failures, including a `WIRE DEGRADED` / `LINE DOWN` banner and offline visual treatment.
- Added load-aware rate gauge colors with warning and error states.
- Added a ledger-style Recent Dispatches view with row indexes, collapsed defaults, "show more" controls, entry-count selection, and collapse behavior.
- Added ARIA state attributes for interactive toggles and expandable controls.

### Changed

- Refined the dashboard identity with paper grain, vignette, masthead metadata, section ornaments, and a more unified sepia palette.
- Improved motion feedback for new telegraph lines, cost graph reveal, and elapsed-time ticks.
- Clarified empty states so filtered-out results and truly idle states are easier to distinguish.

### Fixed

- Fixed the cost graph line reveal animation by replacing a broken dash-array draw effect.

## [0.2.0] - 2026-06-19

### Added

- Added a dedicated Cost panel with `5H`, `Day`, `Wk`, `Mo`, and `Yr` timeframes.
- Added a non-smoothed area graph for spend, with time-axis anchors and dynamic value gridlines.
- Added hover readouts for cost buckets, including a guide line, marker, timestamp, and amount.
- Added clock-aligned cost buckets so graph labels land on natural boundaries such as hours, local midnights, and calendar months.
- Added persistent cost history from `token_count` deltas, backed by an incremental index for restart-safe parsing.
- Added a `5h` / `7d` rate-limit toggle using primary and secondary rate-limit windows.
- Added fallback labeling for unknown pricing models.
- Added click-to-expand behavior for overflowing Live Telegraph lines.
- Added environment overrides for session and cost-index paths.

### Changed

- Switched Codex spend estimates to a model-keyed pricing table with `gpt-5.5` as the default model.
- Rounded cost values only at the API boundary so small deltas remain visible internally.
- Updated README and `.env.example` to match the current stats, rate, cost, install, and Python requirements.

### Removed

- Removed the old configurable `CODEX_MONITOR_COST_*` pricing knobs in favor of fixed monitor pricing.

### Fixed

- Fixed rate-limit reporting to use the latest active sample instead of stale expired windows.
- Corrected `gpt-5.5` pricing to `$5.00` input, `$0.50` cached input, and `$30.00` output per 1M tokens.
- Fixed cost graph gridlines so high buckets no longer cap labels too low.
- Fixed cost label rounding for values such as `$12.5`.
- Fixed long-operation detection so completed long commands are less likely to be misreported as active.
- Fixed partial trailing JSONL lines being treated as damaged session data.
- Fixed invalid cost-index records causing API failures.
- Fixed API snapshot failures so a single bad session record is less likely to take down the UI.
- Fixed `ps` failures being indistinguishable from "0 live jobs".
- Fixed dispatch cleanup by running Codex in its own process group and reaping that group instead of using broad `pkill -f` matching.
- Fixed dispatch exit handling so success, timeout, and failure surface clearer statuses and log tails.
- Fixed dispatch argument validation and output-file creation with safer `mktemp` handling.

## [0.1.0] - 2026-06-17

### Added

- Initial public release of codex-wire.
- Added `codex_monitor.py`, a Python-stdlib-only dashboard for live Codex CLI telemetry.
- Added live job detection from `ps` and Codex session parsing from `~/.codex/sessions`.
- Added dashboard sections for masthead stats, running job cards, Live Telegraph, and Recent Dispatches.
- Added filtering, sorting, polling controls, compact mode, search, desktop alerts, pinning, copy, retry, and kill actions.
- Added token, estimated cost, rate-limit, command, edit, output, error, and last-message telemetry.
- Added `dispatch.sh`, a wrapper for running one `codex exec` job and collecting its `--output-last-message` summary.
- Added `codex-instructions`, an optional launcher for passing an instructions file through `CODEX_INSTRUCTIONS_FILE`.
- Added `.env.example` for monitor, dispatch, and launcher configuration.
- Added `install.sh` to link the dispatch wrapper, create `.env`, and install the `/codex` Claude Code command when absent.
- Added a reusable `/codex` orchestration command example focused on mid-run check-ins.
- Added English and Korean READMEs with language navigation, public clone instructions, screenshots, usage, configuration, and attribution.

### Changed

- Renamed the example instructions launcher from a personal name to the generic `codex-with-instructions`.
- Updated README install instructions to use the public repository URL.
- Added a name-masked full-dashboard hero image to the English and Korean READMEs.

### Removed

- Removed internal improvement notes from the public tree.

[Unreleased]: https://github.com/part3917/codex-wire/compare/v0.6.0...HEAD
[0.6.0]: https://github.com/part3917/codex-wire/compare/v0.5.3...v0.6.0
[0.5.3]: https://github.com/part3917/codex-wire/compare/v0.5.2...v0.5.3
[0.5.2]: https://github.com/part3917/codex-wire/compare/v0.5.1...v0.5.2
[0.5.1]: https://github.com/part3917/codex-wire/compare/v0.5.0...v0.5.1
[0.5.0]: https://github.com/part3917/codex-wire/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/part3917/codex-wire/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/part3917/codex-wire/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/part3917/codex-wire/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/part3917/codex-wire/releases/tag/v0.1.0
