# Report contract

Report `schema_version` remains `1`; project flow configuration independently uses version `2`. Compact and detailed output describe the same run. Full scan status and incremental gate status are separate results with separate meanings.

## Top-level detailed fields

- `generated_at`, `host`, `profile`, `project_root`, `tool_platform`, `tool_root`.
- `config_sources`, `compdb`, `selected_files`, `report_path`.
- `summary`: status, execution completeness, file/check/issue/gap/SKIP counts.
- `checks[]`: `id`, `dimension`, `status`, `required`, `policy`, actual private `tool`, `command`, raw `exit_code`, duration, checked files, issues, reason, log, metrics and evidence gaps.
- `requirements[]`: acceptance requirements and their declared evidence status.
- `gate`, when incremental execution was requested: gate status/completeness, categorized issues/counts, gaps, strict current-check failures, baseline/current commit identities, snapshot kind, selected scope, baseline report and source/input fingerprints. The full current scan remains in `summary` and `checks[]`.

Dimensions are `correctness`, `style`, `readability`, `maintainability`, `sufficiency`, `project_rules`, and `environment`. Complexity findings are `blocking` by default, with clang-tidy severity controlled by native `WarningsAsErrors` and Lizard severity by per-metric policy. Semantic maintainability findings are produced by the agent review described in `quality-review.md`, not fabricated by the runner. `summary.suppressed` counts narrow suppressions; their reasons and unverified premises remain in each check's `metrics.suppressed`.

Blocking readability and Lizard metric violations have issue `severity: error` and check status `FAIL` (exit 1). This is distinct from check status `ERROR`, which means execution/configuration failed. Advisory native checks and explicit Lizard `report_only` policies keep warning diagnostics. Compact findings include each issue's severity so errors are not hidden behind a generic warning or an error-only filter.

Clang-tidy issues retain `notes[]` with message and source coordinates. Incremental function-size comparisons depend on these numeric notes, not only the identical headline diagnostic.

## Summary semantics

Any executed `FAIL` makes the summary `FAIL`. Otherwise any `ERROR` makes it `ERROR`. Otherwise a required `SKIP` makes it `INCOMPLETE`; otherwise it is `PASS`.

`complete=false` means an execution error occurred or a required check was skipped. It does not measure test adequacy. `gaps` records unexecuted branches, missing compilation contexts, missing acceptance evidence and similar limits even when the summary is `PASS`.

## Compact mode

Compact JSON contains the summary and at most five relevant findings, five incomplete checks and three gap summaries. Messages are bounded to 240 characters. Omitted counts are explicit. Optional SKIP reasons collapse to a count; required incompleteness remains visible.

A clean run with no issues or evidence gaps uses only `schema_version`, `status`, `complete`, `files`, `checks`, and optional `skipped`. Use `--detail` or the saved `report.json` for all diagnostics and artifacts. Logs are never inlined wholesale merely because they exist.

Compact `checks` is an integer count; detailed `checks` is an array. Do not iterate compact `checks` or infer the report shape from a failing status. Request `--detail` when a consumer needs per-check records.

## Incremental outcomes

With `gate` present, its status/completeness controls the process exit code. `summary.status` continues to describe all current scan findings, including legacy errors. Compact top-level `status`/`complete` describe the gate, and `scan_status`/`scan_complete` describe the underlying scan. Compact findings contain `classification` and `check`; unchanged legacy findings stay in the detailed report.

Gate classifications are `new`, `worsened`, `existing`, `unattributed` and `required`; `resolved` is a count of unmatched old errors only when attribution completed. `required` identifies strict current-check diagnostics, not a claim that those diagnostics were introduced by this change. New/worsened errors or strict current failures yield `FAIL`; otherwise missing execution/context evidence or unattributed errors yield `INCOMPLETE`; otherwise the gate is `PASS`. A failing gate can still have `complete=false`.

Consumers of detailed reports should choose `document.get("gate", document["summary"])` for the requested outcome. Inspect the full scan separately. Never translate `gate.status: PASS` into “the whole file has no lint errors.”

## Exit codes

- `0`: the requested scan or incremental gate passed.
- `1`: the scan found a policy failure, or the gate found a new/worsened error or strict current-check failure.
- `2`: execution, configuration, tool, parsing or timeout error.
- `3`: a required check was not executed, or incremental execution/attribution evidence is incomplete.

Do not infer runtime/hardware success from static analysis or a host build. Do not infer business correctness from coverage or from an empty issue list.

## Native and project rules

When requested, MISRA-C:2012 addon findings belong to the existing `cppcheck` check with stable `misra-c2012-*` IDs and blocking severity. `metrics.misra_c2012` lists the checked C translation units, the optional user-level `rule_texts` path (null when absent), and partial coverage. Its evidence gaps distinguish selected-unit automatic checking from full compliance. Explicitly configured missing text files or addon failures make execution incomplete even when the tool process returned zero. Default text absence does not disable detection.

Correctness and readability share one `clang-tidy` check. Its issues retain their own `correctness`, `readability`, `project_rules` or `environment` dimension; query-rule IDs use the native `custom-` prefix. Upgrade markers such as `,-warnings-as-errors` are removed from the stable issue rule ID. `metrics.config_files` maps each scanned translation unit to the native configuration selected for it. A locationless `[clang-tidy-config]` warning is an execution `ERROR`, even with tool exit 0 or a matching presentation suppression.

Project-rule commands execute once in quick/full and return structured issues with file/line/column, rule ID, severity and `project_rules` dimension. Exit 2 or another abnormal status, malformed JSON and invalid issue fields are `ERROR`; actual unsuppressed error findings are `FAIL`. A project rule does not count as a full build/test command. See [configuration.md](configuration.md) for the checker protocol.
