---
name: openembeddlint
description: Verify C/C++ changes with a bundled private LLVM/Cppcheck/Lizard toolchain on Windows, Linux and macOS. Use for static analysis, bug detection, clang-format, McCabe cyclomatic complexity, cognitive complexity, readability and maintainability review, pre-commit checks, build/test verification, ASan/UBSan, llvm-cov branch coverage, libFuzzer, and explicit evidence about checks that were not executed.
---

# OpenEmbeddLint

Run deterministic C/C++ checks through the installed `openembeddlint` CLI and its private toolchain. Never substitute analyzers from `PATH`. Treat passing checks, failed checks, execution errors and missing evidence as distinct outcomes.

Locate the CLI first (`Get-Command openembeddlint` on Windows or `command -v openembeddlint` on POSIX); if stale PATH hides it, invoke the verified absolute CLI and fix user PATH. Invoke `scripts/c_verify.py` directly only for explicitly required source integrations or when CLI installation is blocked (record the reason and use `pyproject.toml` dependencies). Never switch entry points because a scan returned `FAIL`, `ERROR` or `INCOMPLETE`. See `README.md` for installation, supported OS/SDK notes and macOS binary signing; check `tools/manifest.json` for verified runtime status.

## Usage

```bash
openembeddlint --project-root /path/to/project [-f file1.c file2.c | -d dir | --git-staged | --git-modified | --git-all]
```

- **Selectors**: `-f` (repeatable or multi-arg), `-d`, `--git-staged` (checks index against `HEAD`), `--git-modified`, `--git-all`, `--baseline <commit>` (Git incremental comparison against snapshots without touching index/branches/commits; explicit `-f`/`-d` without `--baseline` checks whole files). Use `--allow-empty` only for valid empty pre-commit scopes.
- **Options**: `--profile quick|full|fuzz` (`quick`: Cppcheck, unified clang-tidy, clang-format, Lizard metrics, `project_rule`; `full`: + build/test and `llvm-cov`; `fuzz`: only declared fuzz targets), `--compdb <path|dir>`, `--config <path>`, `--jobs N`, `--timeout <s>`, `--no-cache` (bypass `baseline-v1` cache), `--prune-temp`, `--misra-c2012`.

## Token-Efficient Output & Artifacts

Default JSON is compact. **Prefer `--format text` in interactive AI sessions to save tokens** (`--format markdown` for humans; `--detail` for full diagnostics, commands, logs and metrics).

Use `--actionable` or `--limit N` (`0` for unlimited) to show actionable findings without dumping unchanged legacy errors. Truncated results include `issues_omitted` and `omitted_by_rule`; clean runs omit zero/empty fields and `report`. Artifacts (`report.json`, logs) always stay outside the source repo (default temp dir auto-prunes stale runs; `--output-dir` inside Git is rejected). Read `report.json` or named logs only when compact output needs follow-up detail.

## Outcomes & Exit Codes

| Status | Exit | Meaning |
|---|---|---|
| `PASS` | `0` | Check or gate ran and met policy. |
| `FAIL` | `1` | Policy, lint, complexity, format, test or coverage failure. |
| `ERROR` | `2` | Tool, config, compilation context, parsing, startup or timeout error. |
| `INCOMPLETE` | `3` | Required check skipped or incremental attribution/context evidence missing. |
| `SKIP` | — | Not configured or not applicable (non-blocking unless required). |

Run `--doctor` to verify private executables; missing binaries are always `ERROR`, never a `PATH` fallback. Run `--self-test` for framework regression tests.

## Configuration & Rules

`.clang-tidy` owns clang-tidy rules (`defaults/.clang-tidy` fallback); `.c-verify.json` (schema v2) owns orchestration, `exclude_directories`, suppressions, commands and Lizard metrics ([references/configuration.md](references/configuration.md), [references/report-contract.md](references/report-contract.md)). Project commands must use absolute executables or `{tool:name}`, `{python}`, `{skill_dir}`, `{project_root}`, `{output_dir}` placeholders (no shell strings).

`--misra-c2012` / `static.misra_c2012` enables partial MISRA-C:2012 checks on C units; optional guideline text loads automatically from `~/misra-c2012-rule-texts.txt` or `OPENEMBEDDLINT_MISRA_RULE_TEXTS` (never in project config; detection works without texts). Persist user corrections only as scoped lint/config/test rules after checking existing coverage ([references/configuration.md](references/configuration.md)).

## Quality & Incremental Review

Read [references/quality-review.md](references/quality-review.md) for semantic maintainability review and report semantic findings separately from tool output. Default blocking Lizard thresholds: CCN 30, NLOC 100, function length 1000, cognitive complexity 25. Blocking (`severity: error`, `FAIL`) violations must be fixed—never lower thresholds, switch to `report_only`, suppress or skip without explicit authorization.

Incremental gates block only on `new`, `worsened` and strict `required` failures. Unchanged `existing` errors may coexist with `gate.status: PASS` (never claim the whole file is clean). Never dismiss an error as legacy without paired baseline/current evidence; tests derived only from code do not prove requirements or hardware behavior.
