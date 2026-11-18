# OpenEmbeddLint

English | [简体中文](README.zh-CN.md)

Deterministic C/C++ verification and incremental quality gates for embedded projects.
Run clang-tidy, Cppcheck, optional MISRA-C:2012 checks, clang-format and Lizard
through one CLI. Developers, CI and AI coding agents share the same engine.

## Features

- Preserves real compilation database context: targets, ABI, macros, includes and response files.
- Compares full baseline/current scans to distinguish new, worsened and unchanged findings.
- Separates findings, execution errors, skipped checks and missing evidence.
- Supports project-declared host builds/tests, ASan/UBSan, LLVM coverage and explicit fuzzing.
- Uses private platform tools; missing analyzers never silently fall back to PATH.

Built-in scans execute locally. Tool installation downloads upstream packages;
ordinary scans never download or update tools. Project commands are executable
trust input: review them before running an unfamiliar repository.

## Install

Requires Python 3.10+. Incremental gates also require Git. Install
[pipx](https://pipx.pypa.io/stable/installation/), then:

```sh
pipx install https://github.com/ruiwarn/OpenEmbeddLint/releases/download/v3.0.0/openembeddlint-3.0.0-py3-none-any.whl
openembeddlint --version
openembeddlint-tools install
openembeddlint --doctor
```

The CLI wheel is small. The separate native-tool installer uses exact upstream
package URLs and SHA-256 values from the shipped manifest, installs the native
platform, and verifies the core executables. Existing installations are reused
and verified without downloading them again. An incomplete installation fails
explicitly instead of being overwritten. Public packages are distributed through
GitHub Releases; this project has not been published to PyPI.

Source installation:

```sh
git clone https://github.com/ruiwarn/OpenEmbeddLint.git
cd OpenEmbeddLint
pipx install --editable .
openembeddlint-tools install
openembeddlint --doctor
```

This source installation links the commands to the checkout, so `git pull --ff-only`
updates their implementation. Reinstall when dependencies or package metadata change.

Without pipx, install the wheel or source in a Python virtual environment and
use that environment's commands. Do not replace the system Python.

## Tool downloads

| Component | Pinned version | Upstream |
|---|---|---|
| LLVM / clang-tidy / clang-format / coverage | 23.1.4 | [LLVM](https://github.com/llvm/llvm-project/releases), [conda-forge](https://anaconda.org/conda-forge/clang-tools/files) |
| Cppcheck and MISRA addon | 2.22.0 | [Cppcheck](https://github.com/cppcheck-opensource/cppcheck/releases), [conda-forge](https://anaconda.org/conda-forge/cppcheck/files) |
| Windows host compiler and sanitizer/fuzzer runtimes | llvm-mingw 20261020 | [Pinned UCRT x64 archive](https://github.com/mstorsjo/llvm-mingw/releases/download/20261020/llvm-mingw-20261020-ucrt-x86_64.zip) |
| CMake / Ninja | 4.4.5 / 1.13.2 | [CMake](https://cmake.org/download/), [Ninja](https://github.com/ninja-build/ninja/releases) |
| Installer bootstrap | micromamba 2.10.0-0 | [micromamba](https://github.com/mamba-org/micromamba-releases/releases/tag/2.10.0-0) |
| Lizard | 1.25.0 | [PyPI](https://pypi.org/project/lizard/1.25.0/); small MIT Python distribution included |

[tools/manifest.json](tools/manifest.json) records per-platform package URLs,
hashes, binary layouts and historical verification evidence. Native binaries and
SDKs are excluded from Git and Python releases. Upstream components retain their
licenses; see [THIRD_PARTY.md](THIRD_PARTY.md).

Default installed-tool locations:

| OS | Directory |
|---|---|
| Windows | `%LOCALAPPDATA%/OpenEmbeddLint/tools/platforms/<platform>` |
| Linux | `${XDG_DATA_HOME:-~/.local/share}/openembeddlint/tools/platforms/<platform>` |
| macOS | `~/Library/Application Support/OpenEmbeddLint/tools/platforms/<platform>` |

Platform keys: `windows-x86_64`, `linux-x86_64`, `linux-arm64`,
`macos-x86_64`, `macos-arm64`. Linux targets a glibc 2.17 baseline;
macOS targets macOS 11. Only platforms with native runtime evidence are described
as runtime-tested; payload availability is not runtime verification.
macOS host compilation also needs Apple's Xcode Command Line Tools SDK.
Windows uses llvm-mingw and does not require Visual Studio.

To reuse separate tools, set `OPENEMBEDDLINT_TOOLS_DIR` to an absolute parent
containing platform directories. Source checkouts can reuse `tools/platforms/`.
Upgrading the Python CLI preserves user tool caches. Tool-version upgrades are
separate operations and must follow the release requirements.

## Quick start

```sh
openembeddlint --project-root . -f src/main.c --compdb build/compile_commands.json
openembeddlint --project-root . --git-modified
```

Selectors include repeated `-f`, `-d`, `--git-staged` and `--git-all`.
Git selectors compare against HEAD by default. `--baseline <commit>` selects
another baseline or adds incremental comparison to an explicit file selection.
Plain `-f`/`-d` without a baseline enforces the entire selected file.

Incremental mode analyzes captured source revisions completely, applying current
policy to both sides. It is not just changed-line filtering. Header edits expand
through real including units. Ambiguous attribution or incomplete analysis cannot
pass. Unchanged legacy findings remain visible even when the change gate passes.
The real source tree and Git index are not modified.

## Configuration and reports

- `.clang-tidy`: native checks, options and warning/error policy.
- `.c-verify.json` (schema 2): compilation context, Cppcheck, complexity limits,
  reviewed suppressions and project verification commands.

The established flow filename remains `.c-verify.json`; there is no second copy.
See [configuration](references/configuration.md) and the
[report contract](references/report-contract.md).

`quick` runs static checks and declared project-rule commands; `full` adds
declared builds/tests and coverage; `fuzz` runs declared fuzzing only.
Even `quick` can execute project commands: review untrusted configuration.

```sh
openembeddlint --project-root . --git-modified --format text
openembeddlint --project-root . --git-modified --detail
```

Default compact JSON uses `checks` as a count. `--detail` exposes the check
array, locations, rule IDs, severities and evidence. Full `report.json` and logs
are retained in a unique external run directory. `--output-dir` selects its
parent, which must remain outside the source repository.

| Exit | Meaning |
|---|---|
| 0 | Requested checks/gate passed |
| 1 | Blocking findings or verification failures |
| 2 | Tool, execution, configuration or compilation-context error |
| 3 | Required verification or attribution incomplete |

`complete` describes execution evidence, not business or hardware correctness.
Incremental reports keep the full scan status separate from the change gate.

## MISRA-C:2012

```sh
openembeddlint --project-root . --git-modified --misra-c2012
```

Alternatively enable `static.misra_c2012`. The open-source Cppcheck addon checks
selected C units and their headers. It provides partial automatic coverage,
not full compliance certification. Findings are blocking errors and participate
in incremental comparison. Scoped, reasoned suppressions remain auditable.

MISRA guideline text is not redistributed. Detection and rule IDs work without
it. If you have separately authorized text, put `misra-c2012-rule-texts.txt`
directly in the OS-specific OpenEmbeddLint data directory above `tools/`, or
set `OPENEMBEDDLINT_MISRA_RULE_TEXTS` to its absolute path. It loads automatically
for all projects, is copied into external artifacts when used, and is fingerprinted
during incremental verification. Projects do not configure rule-text paths.

## AI skills and development

[SKILL.md](SKILL.md) is the AI entry point. Install the repository in your client's
skills directory, normally `~/.agents/skills/openembeddlint`. Claude can link to
the same checkout. The skill and CLI share one implementation. Existing automation
can still invoke `python scripts/c_verify.py` using that same engine.

After tool installation, run:

```sh
openembeddlint --self-test --timeout 600
```

Source checkouts also support `python scripts/c_verify.py --self-test --timeout 600`.
Run native tests on every claimed platform. Installed-wheel validation is separate
from source-tree tests. Package CI does not establish analyzer, firmware or
hardware acceptance.

OpenEmbeddLint's own code is [MIT licensed](LICENSE).
