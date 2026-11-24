---
name: openembeddlint
description: Verify C/C++ changes with a bundled private LLVM/Cppcheck/Lizard toolchain on Windows, Linux and macOS. Use for static analysis, bug detection, clang-format, McCabe cyclomatic complexity, cognitive complexity, readability and maintainability review, pre-commit checks, build/test verification, ASan/UBSan, llvm-cov branch coverage, libFuzzer, and explicit evidence about checks that were not executed.
---

# OpenEmbeddLint

Run deterministic C/C++ checks through the OpenEmbeddLint CLI and its managed private toolchain. Never substitute an analyzer found on `PATH`. Treat passing checks, failed checks, execution errors and missing evidence as different outcomes.

Python 3.10+ is the host-side runtime prerequisite. Managed toolchain targets are Windows x64, Linux x64/ARM64 (glibc 2.17 baseline), and macOS x64/ARM64 (macOS 11 baseline). Windows host builds use the bundled MinGW/UCRT environment and do not require Visual Studio. macOS analysis tools are installed separately, but compiling host programs still requires the system Xcode/Command Line Tools SDK because Apple SDKs are not redistributed here. Check `tools/manifest.json` for actual per-platform runtime verification; package integrity is not a runtime claim.

## Quick start

```bash
openembeddlint --project-root /path/to/project -f path/to/file.c
```

Use the installed `openembeddlint` command for AI checks. First locate it with `Get-Command openembeddlint` in PowerShell or `command -v openembeddlint` in a POSIX shell. If the current process has a stale PATH, use the verified absolute installed command and fix the user PATH. The private-tool requirement applies to backend analyzers, not to this CLI entry point.

For installation, follow [AI-assisted installation](README.md#ai-assisted-installation). A missing CLI is an installation issue; do not routinely bypass its isolated environment with the system Python. Invoke `scripts/c_verify.py` only for an explicitly required existing source integration, or after confirming the CLI is unavailable and installation cannot be completed within the authorized scope. Record the fallback reason and use an interpreter with the dependencies declared in `pyproject.toml`. A `FAIL`, `ERROR` or `INCOMPLETE` result is never a reason to switch entry points.

Other selectors: repeat `-f` or pass multiple paths (`-f a.c b.c`), `-d <directory>`, `--git-staged`, `--git-modified`, or `--git-all`. Use `--allow-empty` only when an empty pre-commit scope is legitimate. Use `--jobs N` to control static analyzer concurrency, `--no-cache` to bypass the incremental baseline cache, and `--prune-temp` to remove stale default temp directories.

Git selectors run an incremental gate against `HEAD` by default. `--git-staged` analyzes the actual index contents; the other Git selectors analyze a worktree snapshot. Use `--baseline <commit>` to choose another baseline or add comparison to an explicit `-f`/`-d` selection. Without a Git selector or baseline, explicit file/directory scans retain whole-file enforcement. Read [references/configuration.md](references/configuration.md#incremental-gates) before configuring incremental checks or interpreting legacy findings. Incremental mode requires Git and respects repository VCS access rules; it does not stage, commit, switch branches or alter the real index.

```bash
openembeddlint --project-root /path/to/project --git-modified --profile full
```

- `quick`: Cppcheck, one native-configured clang-tidy pass for correctness/readability, clang-format, Lizard metrics, and project-rule commands.
- `full`: quick checks plus project-declared build/test commands and llvm-cov. Missing required configuration makes the result incomplete.
- `fuzz`: only project-declared fuzz commands; never run fuzzing implicitly.

## Control output size

Default JSON is compact. A clean run is normally one line and omits optional SKIP details:

```json
{"schema_version":1,"status":"PASS","complete":true,"files":2,"checks":5,"skipped":1}
```

Use `--detail` for every diagnostic, absolute private tool path, command, exit code, log and coverage metric. Use `--actionable` or `--limit N` (`0` for unlimited) to view all actionable findings in compact mode without dumping legacy errors. Use `--format text` or `--format markdown` for people. Full logs and `report.json` are always written to a unique run directory; pass `--output-dir` to choose its parent.

Runner-managed artifacts must stay outside the source repository in every project profile. The default is the system temporary directory; an explicit `--output-dir` inside the repository is rejected before allocation. Keep temporary database copies and adapter settings with the external run artifacts. To select another compilation database, use `--compdb`; do not clone `.c-verify.json` or discard distinct target configurations to bypass an error.

Do not paste raw tool logs into the conversation when the compact result identifies the issue. Read the detailed report or named log when more evidence is needed.

## Interpret outcomes

| Status | Meaning |
|---|---|
| `PASS` | The check actually ran and met its policy. |
| `FAIL` | It ran and found a correctness, style, test or assertion failure. |
| `ERROR` | Tool, configuration, compilation context, parsing, startup or timeout failure. |
| `SKIP` | Not configured, not requested or not applicable; the reason is recorded. |
| `INCOMPLETE` | Required execution or incremental attribution evidence is missing. |

Exit codes: `0` pass, `1` findings, `2` execution/configuration error, `3` required check incomplete. `complete` describes execution completeness, not business correctness. Coverage gaps and unreviewed behavior remain visible separately.

Run `--doctor` to test the registered core executables. Unsupported OS/architecture or a missing private binary is an error, never a PATH fallback.

## Project configuration

Read [references/configuration.md](references/configuration.md) when adding/configuring a project lint rule, persisting a user correction, creating `.c-verify.json`, or configuring build/test/coverage/fuzz commands. `.clang-tidy` owns clang-tidy rules; `.c-verify.json` schema version 2 owns verification orchestration and other tools. Old flow fields and the separate readability scan are removed. The generic skill must not acquire project names, business protocols, chip rules or absolute project paths.

Fix ordinary scanner findings without recording duplicate rules. Persist a rule only for a user correction or a concrete constraint established in discussion, after checking existing rule coverage. Use regressions or existing applicable instructions when lint cannot establish the behavior. Keep the detailed workflow in the reference; do not create an incident document for every correction.

Use `--misra-c2012` or `static.misra_c2012: true` for the bundled Cppcheck MISRA-C:2012 addon on selected C translation units. Rule IDs and detection work without MISRA guideline text. Optional, separately authorized text is loaded automatically from `~/misra-c2012-rule-texts.txt` in the user's home directory or `OPENEMBEDDLINT_MISRA_RULE_TEXTS`; projects do not configure rule-text paths. During installation, tell users the exact home-file destination if the optional text is absent; continue without it. It is off by default, uses blocking diagnostics, and participates in the same incremental comparison. Read the configuration reference for coverage limitations and reviewed suppressions; passing this partial check never establishes full MISRA compliance.

Use `{tool:name}`, `{python}`, `{skill_dir}`, `{project_root}` and `{output_dir}` placeholders. Built-in tools and project commands must resolve to absolute executables; shell strings and implicit PATH lookup are rejected.

## Review maintainability and correctness

For modified code, read [references/quality-review.md](references/quality-review.md). Inspect the implementation, public contracts and affected callers. Report semantic review findings separately from deterministic tool diagnostics. Do not auto-split a cohesive state machine merely to satisfy a metric. McCabe CCN and cognitive complexity are different measurements. Complexity rules block by default: CCN 30, NLOC 100, total function length 1000, and cognitive complexity 25. Configure clang-tidy thresholds and blocking diagnostics in native `.clang-tidy`; configure Lizard metrics through `cyclomatic.metric_policies`. The skill fallback `defaults/.clang-tidy` supplies the generic rule baseline when the project has no native file.

A test deriving its expectations only from the implementation does not independently establish correctness. Map acceptance requirements to tests and inspect the assertions. Coverage does not prove assertion quality, hardware timing or target behavior.

Blocking readability rules and Lizard metrics emit `severity: error`, make the check `FAIL`, and return exit code 1. An AI completing a code change must fix these quality errors and rerun the affected checks before claiming completion. Do not lower thresholds, switch to `report_only`, suppress or skip a check merely to obtain a pass without explicit user authorization. Ordinary clang-tidy warnings remain advisory; real compiler/context/tool errors stay visible. Explicitly authorized advisory clang-tidy rules and `report_only` Lizard metrics retain warning diagnostics.

In incremental mode, fix new or worsened blocking findings. Unchanged legacy errors remain in the full scan report and may coexist with `gate.status: PASS`; this establishes only the selected change gate, never a clean whole file. Unattributed findings or incomplete baseline/current analysis prevent a passing gate. Current format, project-checker, build, test and coverage failures remain strict. Never dismiss an error as legacy based on surrounding style, a previous successful submission or a similar diagnostic count; use the paired report evidence.

Read [references/report-contract.md](references/report-contract.md) when consuming JSON programmatically.

## Maintainer operations

Source updates use Git; see [README.md](README.md) for installation and `git pull --ff-only`. Large installed tool directories remain ignored and are transferred only for an initial installation or explicit tool repair/update. Preserve working tools when updating source.

Use `openembeddlint-tools install` for initial native tools; reuse existing tools. `scripts/manage_tools.py` also supports maintainer rebuild, prune and inventory operations. Normal checks never download or install anything. Use `scripts/build_fuzzer.py` through a project command to build a fuzz target with the private compiler and runtime.

Run the skill's own tests with `--self-test`, then build the Python wheel/source distribution and validate an installed copy separately. Claim runtime support only on platforms where those tests actually ran; foreign-platform package integrity alone is not runtime verification.

After relocating macOS binaries or libraries, validate their code signatures on macOS and repair invalid signatures before runtime validation. Signing changes executable bytes: refresh manifest hashes and distribute the repaired payload, rather than disabling platform security or using unrelated analyzers.
