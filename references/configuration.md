# Project configuration

## Configuration ownership

`.clang-tidy` owns clang-tidy rule selection, `CheckOptions`, `CustomChecks`, header filtering and `WarningsAsErrors`. `.c-verify.json` owns verification orchestration, compilation context, Cppcheck, Lizard, project commands and report suppressions. Scan findings belong in the generated report, not either configuration file.

The flow configuration uses `schema_version: 2`; reports independently retain schema version 1. Version 1 flow files and removed `readability`, `static.clang_tidy_checks`, `static.header_filter` and `static.ignore_checks` fields are rejected. There is no compatibility adapter or automatic migration. The removed `--skip-readability` and `--ignore` switches are not accepted.

Create `.c-verify.json` only when defaults are insufficient. Dictionaries merge by key; arrays replace. Load order is skill defaults, project `.c-verify.json`, explicit `--config`, then command-line arguments. Invalid nested types, unknown fields and invalid commands return structured `ERROR`.

Keep genuine project policy in the existing flow file. Select a temporary or target-specific compilation database with `--compdb <path>` rather than copying the flow configuration. Runner-managed reports, logs, snapshots and derived databases belong in a unique external run directory. All project profiles reject an output parent inside the project repository before creating any directory, including symlink/junction aliases and sibling paths inside the repository of a selected subproject. The default uses the system temporary directory; if that location is project-local, choose an external `--output-dir`. Project-declared build/test commands remain responsible for their own configured outputs.

```json
{
  "schema_version": 2,
  "static": {"compile_commands": "build/compile_commands.json"},
  "exclude_directories": [".git/", "build/", "third_party/"]
}
```

## Native clang-tidy rules

For each translation unit, the runner uses the nearest `.clang-tidy` between its source directory and the project root. If none exists, it uses the skill's `defaults/.clang-tidy`. The default file is JSON, which clang-tidy accepts as YAML; project files may use either syntax. Project configurations use clang-tidy's native inheritance and replace the skill fallback, rather than merging with it. To add a first project rule while retaining the baseline, copy `defaults/.clang-tidy` into the project and extend it. Do not reduce existing quality policies merely to obtain a pass.

The skill fallback retains cognitive complexity 25, function statement count 100, line count 200, branch count 30, parameter count 8, nesting 5 and variable count 30. Cognitive complexity, function size and branch-clone diagnostics are errors through `WarningsAsErrors`; other clang-tidy warnings remain advisory. Lizard separately reports McCabe CCN/NLOC/total length. Correctness and readability now run in one clang-tidy invocation per selected translation unit. Each issue retains its own dimension; there is no separate `readability` check.

`--checks` appends native check globs (include `-*` to replace the selection); `--header-filter` overrides the native header filter. The detailed clang-tidy check records the selected native configuration in `metrics.config_files`. Query checks are enabled by the runner with `--experimental-custom-checks`; do not put that analyzer switch into `static.extra_args`, which contains compiler arguments. A malformed query's `[clang-tidy-config]` warning is an execution error even when clang-tidy returns zero. Query rules remain experimental and need regression checks after tool upgrades.

A project that declares native rules requires compilation context: missing translation units or a missing compilation database must not silently establish a pass. Without project rules, an optional quick clang-tidy scan can still be skipped with its reason and gaps. Headers must be exercised through real including translation units and applicable shipped configurations.

## Incremental gates

Rules remain in `.clang-tidy`; incremental execution is selected at the command line. There is no persistent exemption ledger, baseline JSON configuration or second rule-definition file.

```bash
python3 "$SKILL_DIR/scripts/c_verify.py" --project-root /repo --git-modified
python3 "$SKILL_DIR/scripts/c_verify.py" --project-root /repo --git-staged
python3 "$SKILL_DIR/scripts/c_verify.py" --project-root /repo --baseline HEAD -f src/controller.c
```

Git selectors are mutually exclusive and default to baseline `HEAD`. `--git-modified` selects tracked differences from that baseline; `--git-all` also selects untracked C/C++ files. `--git-staged` uses index contents and index-native policies, including when the worktree has a different version. `--baseline <commit>` without another selector selects baseline-to-worktree differences plus untracked C/C++ files. For committed/MR changes, supply the intended base commit explicitly; default `HEAD` does not review already committed changes. The project root must be the Git repository root; use `-f`/`-d` for subprojects. Explicit selections without `--baseline` retain full enforcement.

The runner checks out the baseline through a temporary index and captures the current index/worktree independently. The real index, branch and worktree are not changed. Both sides receive current native policies and validated current flow settings. Historical flow files are neither loaded nor executed; an old schema in the baseline does not require a compatibility adapter or prevent adoption of current checks. Full translation units are analyzed on both sides; this is not a changed-line filter. Modified headers trigger compiler dependency queries on both snapshots and expand the scope to including translation units. Incremental clang-tidy defaults to header filter `.*`; an explicit override narrows that coverage and must be justified. Header discovery or compilation failures cannot establish a passing gate.

The gate compares rule identity, message, source location and unchanged source lines, accounting for line shifts and detected renames. Duplicate diagnostics from multiple including units do not become duplicate gate violations. Existing occurrences cannot offset new occurrences. Recognized cognitive/Lizard metrics compare the same function's numeric values; clang-tidy function size also compares numeric notes, so a larger body is caught even when the declaration and headline stay unchanged. Equal or lower over-threshold metrics remain visible legacy findings. Ambiguous identity, changed diagnostic traces and indistinguishable moved/changed violations are `unattributed`, not automatic waivers.

The saved report retains the complete current scan and a separate `gate`, with `new`, `worsened`, `existing`, `unattributed`, `resolved` and `required` counts. A passing gate can coexist with a failing full scan. Compact output leads with the gate outcome, includes `scan_status`, and shows actionable findings instead of flooding the output with legacy errors. `gate.complete=false` prevents a pass; `FAIL` still takes precedence when a strict check fails. See [report-contract.md](report-contract.md) before consuming either shape.

Limits are explicit:

- A current compilation database is required, with one compiler configuration per selected source per run; identical duplicate entries are collapsed. Unselected sources may retain distinct configurations. Header expansion also enforces this selected-source requirement. Response files, source paths and include paths are remapped while target/ABI/defines and external/generated dependencies are preserved. Distinct selected configurations must be verified separately. Use `--compdb <path>` to select another database without duplicating `.c-verify.json`; do not discard real target configurations just to obtain a pass. Actual build-input changes and other tracked non-source inputs outside native policies and Markdown/reStructuredText require separate verification; the runner reports incomplete attribution rather than assuming the current database describes a different build configuration. Changing analyzer-only flow settings applies the same current settings to both source revisions.
- Database/response files, loaded flow settings and discovered external dependencies are fingerprinted. Original source/index changes during a scan or snapshot mutations invalidate its gate. These fingerprints establish which inputs were checked; they do not prove that an externally supplied database was freshly generated or that ignored generated headers match every target configuration. Prepare the real build context first.
- Precompiled headers/modules, VFS/compiler configuration overlays, unmerged indexes and submodules are rejected. Detected native-policy inheritance outside the repository produces an incomplete gate. Dependency paths that escape back into captured worktree sources cannot silently substitute current code into baseline analysis. No guessed host context replaces a missing target context.
- Formatting, arbitrary project-rule commands, builds/tests and coverage remain strict on the current snapshot; their failures are never excused as legacy. Only built-in Cppcheck, clang-tidy (including native custom checks) and Lizard diagnostics use paired attribution. Project commands run in the snapshot and must tolerate relocation and absence of ignored build products/Git metadata. `full` still requires its normal runtime/coverage evidence. Run `fuzz` separately.
- Artifacts must be outside the repository. Baseline/current reports, logs and source manifests are retained; temporary trees are removed. Allow for two full analyses plus dependency discovery. This gate does not establish runtime, hardware or business correctness.

## MISRA-C:2012

The bundled open-source Cppcheck addon provides partial automatic MISRA-C:2012 checking. Enable it with `--misra-c2012` for one run, or in the existing flow file:

```json
{
  "schema_version": 2,
  "static": {"misra_c2012": true}
}
```

It runs through the existing Cppcheck pass on selected `.c` translation units, including their headers. C++ units retain normal Cppcheck analysis and are not claimed as MISRA-C checks. MISRA findings retain native IDs such as `misra-c2012-15.1`, source coordinates and blocking severity. They participate in the existing paired incremental attribution. The default remains disabled; enabling it in the generic skill does not automatically enable it in every project. Skipping Cppcheck while requesting this check is an error.

The runner uses the private addon and the current absolute Python interpreter; no implicit `python` lookup is used. Generated addon settings, dumps and cross-unit data go to the run artifact directory. A missing addon, explicitly configured missing rule-text file, malformed output or addon failure is an execution error, never a clean check.

OpenEmbeddLint does not redistribute MISRA guideline text. Detection and rule IDs work without it. Optional, separately authorized text is discovered from `misra-c2012-rule-texts.txt` in the user data directory, or from the absolute file named by `OPENEMBEDDLINT_MISRA_RULE_TEXTS`. See the README for OS-specific data directories. Projects only enable the check and configure reviewed suppressions; the removed `static.misra_rule_texts` option remains rejected. When present, the adapter copies the file into external run artifacts and uses a simple relative filename to avoid addon argument encoding issues. Incremental verification fingerprints the selected user resource, including an absent default file, so creation or mutation during analysis prevents a pass. The text remains private and is excluded from Git, wheels and source releases.

Use the existing top-level `suppressions` for reviewed MISRA deviations, for example `check: "misra-c2012-15.1"` with a narrow `path` and a mandatory `reason`. Suppressed findings remain auditable. All enabled MISRA findings use `severity: error`, including rules categorized as Advisory by MISRA; standard classification and project gate severity are different fields. Incremental mode can accept unchanged existing violations while blocking newly introduced ones. Tool and context failures cannot be suppressed into a pass.

`metrics.misra_c2012` records the checked C units and explicitly labels coverage as partial. Whole-program analysis, all shipped configurations, directives, manual review and deviation/compliance acceptance are not established by this selected-unit addon pass. Do not relabel it as complete MISRA-C:2012 compliance or as C:2023 support. See the [upstream addon description](https://github.com/cppcheck-opensource/cppcheck/blob/main/addons/README.md).

## Persist a user-confirmed lint rule

Ordinary scanner findings are fixed using the existing diagnostic; do not duplicate their rules or create an incident ledger. Consider persisting a rule when the user explicitly corrects a mistake, asks to prevent recurrence, or discussion establishes a concrete project constraint. Use the actual agreement as evidence, not an inferred preference or one unusual example.

1. Identify the forbidden behavior, allowed cases and affected domain. Check the existing enabled and available rules first. Reuse an existing rule or enable/configure it when sufficient; add a custom rule only for a demonstrated gap.
2. If the constraint is statically detectable, add it to the project's `.clang-tidy`; use a small project checker only when the native checks cannot express it. Keep business names and exceptions in the project, never the generic skill. Give each rule a stable ID and an actionable diagnostic naming the approved alternative. Do not create another rule-definition file or a Markdown entry for every correction.
3. Add the smallest meaningful violation, valid-use and likely-false-positive regression cases in the project's existing `.test/` harness. The skill's own tests use its existing `tests/` harness. For constraints requiring runtime, protocol or hardware evidence, use regression tests or the existing applicable agent instructions; do not invent a text lint that claims to establish behavior.
4. Verify through the actual c-verify entry point that the violation produces `FAIL` with rule ID/location, allowed cases pass, and malformed rule configuration produces `ERROR`. A saved rule, standalone tool success or zero child exit alone is not execution evidence. Reuse an established user decision; do not repeatedly request confirmation for the same rule.

For a first native rule, copy the fallback and append the new `custom-` ID to both `Checks` and `WarningsAsErrors` when the agreed constraint is blocking. For example, this `CustomChecks` entry detects direct `vTaskDelay` calls inside functions named `*_IRQHandler`:

```yaml
CustomChecks:
  - Name: no-delay-in-irq
    Query: |
      match callExpr(
        callee(functionDecl(hasName("vTaskDelay"))),
        hasAncestor(functionDecl(matchesName("_IRQHandler$")))
      ).bind("call")
    Diagnostic:
      - BindName: call
        Message: 'IRQ handlers must notify a task instead of calling vTaskDelay.'
        Level: Warning
```

Its enabled name is `custom-no-delay-in-irq`. Valid task calls must still pass. The matcher covers only direct calls under that naming convention; it does not establish that indirect call chains or differently named interrupt entries are safe. Use real project entry points when defining the production rule. This is a demonstration, not a default rule for every project.

## Compilation context and other checks

Prefer an explicit compilation database. Without one, clang-tidy is not run unless `allow_without_compdb` is deliberately enabled; that fallback is not evidence for target configuration. Bundled compiler entries preserve response-file, target, sysroot, ABI and preprocessor options when querying system includes. External compiler entries do not inherit the host driver environment. Invalid response files are errors.

`static.extra_args` supplies compiler driver arguments. `static.cppcheck_defines` and `static.cppcheck_includes` supplement target definitions obtained from the real toolchain. Include paths resolve against the project root. With a compilation database, these supplements are written into an artifact copy of its compiler arguments because Cppcheck project imports do not apply the command-line supplements. The original database is unchanged. The generic skill does not invent vendor macros, SDK paths or host substitutes. Canonical source membership and Cppcheck's original compilation-database spellings must both be preserved.

Formatting is skipped when no `.clang-format` exists. Configure naming and other project-specific style rules in native `.clang-tidy` only when they are actual project policy.

Lizard's `cyclomatic` configuration independently controls `ccn_threshold=30`, `nloc_threshold=100` and `length_threshold=1000`. Policies default to `blocking`; explicit per-metric policies override the latest global policy. `report_only` retains a warning. Changing thresholds or making a blocking rule advisory to bypass a finding requires explicit user authorization.

```json
{
  "schema_version": 2,
  "cyclomatic": {"metric_policies": {"length": "report_only"}}
}
```

## Scoped suppressions

Use top-level `suppressions` only for a reviewed false positive or target-specific rule that cannot apply. Each entry requires a reason and can match a rule, project-relative path and message substring. Other diagnostics remain active, and suppressed records stay visible in the detailed report.

```json
{
  "schema_version": 2,
  "suppressions": [
    {
      "check": "nullPointer",
      "path": "drivers/cmsis/core_cm0.h",
      "message": "vectors",
      "reason": "Memory-mapped vector table; host null-pointer model does not apply.",
      "needs_manual_confirmation": true
    }
  ]
}
```

Do not globally disable a useful rule when a narrow suppression or a corrected compilation definition would be more accurate. `needs_manual_confirmation` records an unverified target/business premise; it is not proof that the suppression is valid. Suppression changes presentation only: missing headers, files that cannot be opened, any `clang-diagnostic-error` (including arbitrary-text `#error`), Cppcheck `preprocessorErrorDirective` and other compilation-context failures remain `ERROR` with `complete=false` even when their diagnostic text matches a suppression.

## Commands

Every command uses an argv array, never a shell string. The executable must be absolute after placeholder expansion. Project configuration is executable trust input: review it before running `full` or `fuzz` on an untrusted repository, just as you would review its build scripts.

| Placeholder | Value |
|---|---|
| `{tool:clang}` | Private host Clang for the current OS/architecture. |
| `{tool:clang++}` | Private host Clang++. |
| `{tool:cmake}` / `{tool:ninja}` / `{tool:clang-tidy}` | Other private executables. |
| `{python}` | Absolute Python running the verifier. |
| `{skill_dir}` | Absolute skill root. |
| `{project_root}` / `{output_dir}` | Project and unique artifact directories. |
| `{platform}` | Current platform key. |
| `{exe_suffix}` | `.exe` on Windows, empty on Linux/macOS. |

Available command fields: `id`, `kind` (`build`, `test`, `fuzz`, `project_rule`), `argv`, optional `cwd`, `env`, `timeout_s`, `required`, `evidence`, and `requirements`. `cwd` must stay inside the project or its dedicated output directory. Environment values are used but not copied into reports.

`project_rule` commands execute once in both `quick` and `full`, before full-only build/test commands. Fuzz commands remain explicit. A standalone `{files}` argument expands into the selected absolute source/header paths, with no shell splitting. A project rule does not substitute for full-profile build/test or coverage evidence.

```json
{
  "schema_version": 2,
  "commands": [
    {
      "id": "domain-lint",
      "kind": "project_rule",
      "argv": ["{python}", "{project_root}/env/domain_lint.py", "{files}"]
    }
  ]
}
```

Project rule checkers write exactly one JSON object to stdout; send progress and tracebacks to stderr. `issues` is required, including for a clean run:

```json
{"issues":[{"rule":"domain-no-private-access","message":"Use the public domain API.","severity":"error","file":"src/app.c","line":9,"column":2}]}
```

Each issue requires non-empty `rule` and `message`; severity defaults to `error` and may be `error`, `warning` or `info`. Optional `file` is a path; line/column are positive integers when present. Unknown fields and malformed JSON are execution errors. Exit 0 reports results, exit 1 requires an error diagnostic, and any other exit or process failure is `ERROR`. An error diagnostic fails even with exit 0; a warning with exit 0 is advisory. Reviewed suppressions remain auditable. No plain-text compatibility protocol is retained. Build/test/fuzz commands retain their ordinary exit-status semantics.

## ASan/UBSan and coverage example

Adapt target names to the project; do not let the verifier guess them. This example assumes a CMake host-test target called `host_tests`.

```json
{
  "schema_version": 2,
  "commands": [
    {
      "id": "sanitize-build",
      "kind": "build",
      "argv": ["{tool:cmake}", "-S", ".", "-B", "{output_dir}/sanitize-build", "-G", "Ninja", "-DCMAKE_MAKE_PROGRAM={tool:ninja}", "-DCMAKE_C_COMPILER={tool:clang}", "-DCMAKE_CXX_COMPILER={tool:clang++}", "-DCMAKE_C_FLAGS=-fsanitize=address,undefined;-fno-omit-frame-pointer", "-DCMAKE_CXX_FLAGS=-fsanitize=address,undefined;-fno-omit-frame-pointer", "-DCMAKE_EXE_LINKER_FLAGS=-fsanitize=address,undefined"]
    },
    {
      "id": "sanitize-test",
      "kind": "test",
      "argv": ["{output_dir}/sanitize-build/host_tests{exe_suffix}"],
      "env": {
        "ASAN_OPTIONS": "detect_leaks=1:halt_on_error=1:symbolize=1",
        "UBSAN_OPTIONS": "print_stacktrace=1:halt_on_error=1"
      },
      "evidence": ["ASan", "UBSan"]
    },
    {
      "id": "coverage-build",
      "kind": "build",
      "argv": ["{tool:cmake}", "-S", ".", "-B", "{output_dir}/coverage-build", "-G", "Ninja", "-DCMAKE_MAKE_PROGRAM={tool:ninja}", "-DCMAKE_C_COMPILER={tool:clang}", "-DCMAKE_CXX_COMPILER={tool:clang++}", "-DCMAKE_C_FLAGS=-fprofile-instr-generate;-fcoverage-mapping", "-DCMAKE_CXX_FLAGS=-fprofile-instr-generate;-fcoverage-mapping", "-DCMAKE_EXE_LINKER_FLAGS=-fprofile-instr-generate"]
    },
    {
      "id": "coverage-test",
      "kind": "test",
      "argv": ["{output_dir}/coverage-build/host_tests{exe_suffix}"],
      "env": {"LLVM_PROFILE_FILE": "{output_dir}/host-%p.profraw"}
    }
  ],
  "coverage": {
    "binary": "{output_dir}/coverage-build/host_tests{exe_suffix}",
    "profraw": ["{output_dir}/host-*.profraw"],
    "profdata": "{output_dir}/host.profdata",
    "sources": ["src/parser.c"],
    "required": true
  }
}
```

Use `{exe_suffix}` for cross-platform executable paths and separate sanitizer/coverage build directories. The runner merges profiles and stores `coverage-export.json` and `coverage-show.txt`. A branch summary identifies evidence gaps; inspect the show file for exact source branches. The detailed show report is generated without a source-filter argument so Windows paths are not misinterpreted as filter expressions. Startup failures, timeouts, missing sources, tool errors and empty detailed reports are `ERROR`, never a clean pass.

Host SDK availability is checked by actually compiling the self-test fixtures. Tool version output alone does not establish that host executables can be linked.

## libFuzzer

Build and run are separate commands so a compile/link failure cannot be mistaken for a fuzz finding.

```json
{
  "schema_version": 2,
  "fuzz": [
    {
      "id": "parser-fuzz-build",
      "kind": "build",
      "argv": ["{python}", "{skill_dir}/scripts/build_fuzzer.py", "--source", "src/parser_fuzz.c", "--output", "{output_dir}/parser-fuzz{exe_suffix}"]
    },
    {
      "id": "parser-fuzz-run",
      "kind": "fuzz",
      "argv": ["{output_dir}/parser-fuzz{exe_suffix}", "test/corpus/parser", "-max_total_time=60", "-artifact_prefix={output_dir}/fuzz-artifacts/"],
      "timeout_s": 90,
      "env": {"ASAN_OPTIONS": "detect_leaks=1:symbolize=1"}
    }
  ]
}
```

The helper compiles C and C++ sources separately with coverage instrumentation: `fuzzer-no-link` on Linux/macOS and explicit `inline-8bit-counters,pc-table,trace-cmp` on MinGW. On MinGW it links the packaged Fuzzer runtime explicitly because the driver rejects `-fsanitize=fuzzer`; on Linux/macOS it uses the driver option. Preserve every crash artifact as a fixed regression input. Tests should check both `__sanitizer_cov` symbols and real `cov:` feedback, not only a pre-seeded crash.

## Acceptance evidence

```json
{
  "schema_version": 2,
  "requirements": [
    {
      "id": "malformed-input-rejected",
      "description": "Malformed frames return the documented error without changing output state.",
      "tests": ["sanitize-test"]
    },
    {
      "id": "target-timing",
      "tests": [],
      "manual": "Measure on the target; host stubs are not timing evidence."
    }
  ]
}
```

The verifier checks whether declared evidence ran. An agent must still inspect whether assertions express the requirement. Missing or misleading requirements cannot be repaired by coverage percentages.
