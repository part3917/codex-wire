# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
for 0.x releases.

## [Unreleased]

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

[Unreleased]: https://github.com/part3917/codex-wire/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/part3917/codex-wire/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/part3917/codex-wire/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/part3917/codex-wire/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/part3917/codex-wire/releases/tag/v0.1.0
