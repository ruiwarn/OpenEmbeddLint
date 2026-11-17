"""Command-line orchestration for project-independent C/C++ verification."""

from __future__ import annotations

import argparse
import copy
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from . import __version__

from .checks import (
    clang_tidy_config,
    run_clang_format,
    run_clang_tidy,
    run_cppcheck,
    run_coverage,
    run_cyclomatic,
    run_project_command,
)
from .compilation import HEADER_SUFFIXES, SOURCE_SUFFIXES, locate_compdb
from .execution import ERROR, PASS, SKIP, CheckResult, ToolUnavailable, Toolchain, run_process
from .report import SCHEMA_VERSION, exit_code, full_document, render
from .incremental.runner import run_incremental


SKILL_DIR = Path(__file__).resolve().parents[2]
SUFFIXES = SOURCE_SUFFIXES | HEADER_SUFFIXES
TOOL_NAMES = ("cppcheck", "clang-tidy", "clang-format", "clang", "clang++", "llvm-cov", "llvm-profdata", "llvm-symbolizer", "cmake", "ninja")


class CLIError(RuntimeError):
    pass


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="OpenEmbeddLint: deterministic C/C++ verification with a private toolchain")
    result.add_argument("--version", action="version", version="OpenEmbeddLint " + __version__)
    result.add_argument("--project-root", help="project root; required unless --self-test is used")
    result.add_argument("-f", "--file", action="append", default=[], help="source/header file; repeatable")
    result.add_argument("-d", "--directory", help="recursively check a directory")
    git_selection = result.add_mutually_exclusive_group()
    git_selection.add_argument("--git-staged", action="store_true", help="verify the index against the baseline (default HEAD)")
    git_selection.add_argument("--git-modified", action="store_true", help="verify tracked worktree changes against the baseline")
    git_selection.add_argument("--git-all", action="store_true", help="also select untracked C/C++ changes")
    result.add_argument("--baseline", help="compare full analysis against this Git commit; Git selectors default to HEAD")
    result.add_argument("--allow-empty", action="store_true", help="allow a valid empty pre-commit selection")
    result.add_argument("--config", help="project configuration; defaults to <root>/.c-verify.json")
    result.add_argument("-p", "--compdb", help="compile_commands.json or its directory; overrides project configuration")
    result.add_argument("--profile", choices=("quick", "full", "fuzz"), default="quick")
    result.add_argument("--format", choices=("json", "text", "markdown"), default="json")
    result.add_argument("--detail", action="store_true", help="include every diagnostic, command, version and artifact")
    result.add_argument("--output-dir", help="artifact parent; a unique run directory is always created")
    result.add_argument("--timeout", type=float, default=180.0, help="default timeout per process in seconds")
    result.add_argument("--checks", help="append native clang-tidy check globs; use -* to replace selection")
    result.add_argument("--header-filter")
    result.add_argument("--severity", choices=("error", "warning", "all"), default="all")
    result.add_argument("--skip-cppcheck", action="store_true")
    result.add_argument("--misra-c2012", action="store_true", help="enable partial MISRA-C:2012 checking through the bundled Cppcheck addon")
    result.add_argument("--skip-clang-tidy", action="store_true")
    result.add_argument("--skip-format", action="store_true")
    result.add_argument("--skip-cyclomatic", action="store_true")
    result.add_argument("--doctor", action="store_true", help="verify private tools without checking source")
    result.add_argument("--self-test", action="store_true", help="run the skill's own regression tests")
    result.add_argument("--summary", action="store_true", help="legacy alias for the default compact output")
    result.add_argument("-q", "--quiet", action="store_true")
    return result


def merge(base: dict, override: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _schema_type(value, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "null":
        return value is None
    raise CLIError(f"unsupported schema type: {expected}")


def validate_schema(value, schema: dict, path: str, root: dict) -> None:
    """Validate the JSON-Schema features used by config.schema.json without dependencies."""
    if "$ref" in schema:
        reference = schema["$ref"]
        if not reference.startswith("#/"):
            raise CLIError(f"unsupported schema reference: {reference}")
        target = root
        for part in reference[2:].split("/"):
            target = target[part]
        return validate_schema(value, target, path, root)
    if "const" in schema and value != schema["const"]:
        raise CLIError(f"config error at {path}: expected {schema['const']!r}, got {value!r}")
    if "enum" in schema and value not in schema["enum"]:
        raise CLIError(f"config error at {path}: expected one of {schema['enum']!r}, got {value!r}")
    if "type" in schema:
        expected_types = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_schema_type(value, expected) for expected in expected_types):
            raise CLIError(f"config error at {path}: expected {expected_types}, got {type(value).__name__}")
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                raise CLIError(f"config error at {path}: missing required key {key}")
        properties = schema.get("properties", {})
        additional = schema.get("additionalProperties", True)
        for key, item in value.items():
            if key in properties:
                validate_schema(item, properties[key], f"{path}.{key}", root)
            elif additional is False:
                raise CLIError(f"config error at {path}: unknown key {key}")
            elif isinstance(additional, dict):
                validate_schema(item, additional, f"{path}.{key}", root)
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            raise CLIError(f"config error at {path}: expected at least {schema['minItems']} items")
        if isinstance(schema.get("items"), dict):
            for index, item in enumerate(value):
                validate_schema(item, schema["items"], f"{path}[{index}]", root)
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            raise CLIError(f"config error at {path}: string is too short")
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            raise CLIError(f"config error at {path}: {value!r} does not match {schema['pattern']}")
    if _schema_type(value, "number"):
        if "minimum" in schema and value < schema["minimum"]:
            raise CLIError(f"config error at {path}: expected >= {schema['minimum']}, got {value}")
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            raise CLIError(f"config error at {path}: expected > {schema['exclusiveMinimum']}, got {value}")


def load_config(root: Path, explicit: str | None) -> tuple[dict, list[str]]:
    default_path = SKILL_DIR / "defaults" / "config.json"
    try:
        config = json.loads(default_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise CLIError(f"cannot load generic defaults: {error}") from error
    sources = [str(default_path)]
    config_paths = []
    project_path = root / ".c-verify.json"
    if project_path.is_file():
        config_paths.append(project_path)
    if explicit:
        explicit_path = Path(explicit).resolve()
        if not explicit_path.is_file():
            raise CLIError(f"project config not found: {explicit_path}")
        config_paths.append(explicit_path)
    unset = object()
    cyclomatic_global = unset
    explicit_metric_policies = {}
    for path in config_paths:
        try:
            override = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise CLIError(f"cannot load project config {path}: {error}") from error
        if not isinstance(override, dict) or override.get("schema_version") != 2:
            raise CLIError("project config must be an object with schema_version 2")
        if isinstance(override.get("cyclomatic"), dict):
            section = override["cyclomatic"]
            if "policy" in section:
                cyclomatic_global = section["policy"]
            metric_overrides = section.get("metric_policies", {})
            if not isinstance(metric_overrides, dict):
                raise CLIError(f"config error at {path}.cyclomatic.metric_policies: expected object")
            explicit_metric_policies.update(metric_overrides)
        config = merge(config, override)
        sources.append(str(path))
    if cyclomatic_global is not unset:
        config["cyclomatic"]["metric_policies"] = explicit_metric_policies
    schema = json.loads((SKILL_DIR / "config.schema.json").read_text(encoding="utf-8"))
    validate_schema(config, schema, "config", schema)
    return config, sources


def inside_root(path: Path, root: Path) -> Path:
    resolved = path.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise CLIError(f"selected path escapes project root: {path}")
    return resolved


def excluded(path: Path, root: Path, patterns: list[str]) -> bool:
    relative = path.relative_to(root).as_posix()
    return any(relative == pattern.rstrip("/") or relative.startswith(pattern.rstrip("/") + "/") for pattern in patterns)


def select_files(args: argparse.Namespace, root: Path, config: dict) -> list[Path]:
    files: set[Path] = set()
    for value in args.file:
        path = inside_root(Path(value) if Path(value).is_absolute() else root / value, root)
        if not path.is_file():
            raise CLIError(f"selected file not found: {path}")
        if path.suffix.lower() in SUFFIXES:
            files.add(path)
    if args.directory:
        directory = inside_root(Path(args.directory) if Path(args.directory).is_absolute() else root / args.directory, root)
        if not directory.is_dir():
            raise CLIError(f"selected directory not found: {directory}")
        files.update(path for path in directory.rglob("*") if path.is_file() and path.suffix.lower() in SUFFIXES and not excluded(path, root, config["exclude_directories"]))
    selected = sorted(
        (inside_root(path, root) for path in files if path.is_file() and path.suffix.lower() in SUFFIXES),
        key=lambda path: path.as_posix(),
    )
    if not selected and not args.allow_empty:
        raise CLIError("no C/C++ files selected; use --allow-empty only for a legitimate empty pre-commit scope")
    return selected


def make_output_dir(value: str | None, root: Path | None = None) -> Path:
    parent = Path(value or tempfile.gettempdir()).resolve()
    if root is not None:
        repository = next((path for path in (root, *root.parents) if (path / ".git").exists()), root)
        if parent.is_relative_to(repository):
            raise CLIError("verification artifacts must be outside the source repository; omit --output-dir or choose an external directory")
    parent.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix="run-" if value else "c-verify-", dir=parent))


def doctor(toolchain: Toolchain, output_dir: Path) -> list[CheckResult]:
    results = []
    for name in TOOL_NAMES:
        try:
            executable = toolchain.require(name)
        except ToolUnavailable as error:
            results.append(CheckResult(f"tool:{name}", "environment", ERROR, reason=str(error)))
            continue
        process = run_process([executable, "--version"], cwd=output_dir, timeout_s=20, log_path=output_dir / "logs" / f"doctor-{name}.log", extra_env=toolchain.environment())
        status = PASS if process.exit_code == 0 and not process.start_error and not process.timed_out else ERROR
        results.append(CheckResult(
            f"tool:{name}", "environment", status, tool=str(executable), command=process.command,
            exit_code=process.exit_code, duration_s=process.duration_s, log=process.log,
            reason=process.start_error or (process.stderr.strip() if status == ERROR else None),
            metrics={"version": (process.stdout + process.stderr).splitlines()[0] if (process.stdout + process.stderr).splitlines() else ""},
        ))
    return results


def requirement_evidence(config: dict, checks: list[CheckResult]) -> tuple[CheckResult | None, list[dict]]:
    if not config["requirements"]:
        return None, []
    by_id = {check.id: check for check in checks}
    evidence = []
    gaps = []
    for requirement in config["requirements"]:
        identifier = requirement.get("id", "requirement")
        command_ids = requirement.get("tests", [])
        statuses = {test: by_id[test].status if test in by_id else "MISSING" for test in command_ids}
        if not command_ids:
            gaps.append(f"{identifier}: no test or manual evidence declared")
        gaps.extend(f"{identifier}: {test}={status}" for test, status in statuses.items() if status != PASS)
        if requirement.get("manual"):
            gaps.append(f"{identifier}: manual/target verification remains: {requirement['manual']}")
        evidence.append({**requirement, "evidence_status": statuses})
    return CheckResult("requirements", "sufficiency", PASS, required=False, gaps=gaps), evidence


def execute_checks(args: argparse.Namespace, root: Path, toolchain: Toolchain, config: dict, files: list[Path], compdb: Path | None, output_dir: Path) -> list[CheckResult]:
    logs = output_dir / "logs"
    checks: list[CheckResult] = []
    if args.profile != "fuzz" and not files:
        checks.append(CheckResult("source-scope", "environment", SKIP, required=False, reason="no selected files exist in this snapshot"))
    if args.profile != "fuzz" and files:
        if args.skip_cppcheck:
            checks.append(CheckResult("cppcheck", "correctness", SKIP, required=False, reason="skipped by command line"))
        else:
            checks.append(run_cppcheck(files, root, toolchain, config["static"], args.timeout, logs, True, compdb, config["suppressions"]))
        tidy_required = args.profile == "full" or compdb is not None or any(clang_tidy_config(file, root, SKILL_DIR).is_relative_to(root) for file in files)
        if args.skip_clang_tidy:
            checks.append(CheckResult("clang-tidy", "correctness", SKIP, required=False, reason="skipped by command line"))
        else:
            checks.append(run_clang_tidy(files, root, toolchain, config["static"], args.timeout, logs, tidy_required, compdb, config["suppressions"], args.checks, args.header_filter))
        if config["format"]["enabled"] and not args.skip_format:
            checks.append(run_clang_format(files, root, toolchain, config["format"], args.timeout, logs))
        if config["cyclomatic"]["enabled"] and not args.skip_cyclomatic:
            checks.append(run_cyclomatic(files, root, toolchain, config["cyclomatic"], args.timeout, logs))
    if args.profile != "fuzz":
        checks.extend(run_project_command(spec, root, output_dir, toolchain, args.timeout, files, config["suppressions"]) for spec in config["commands"] if spec["kind"] == "project_rule")
    if args.profile == "full":
        commands = [spec for spec in config["commands"] if spec["kind"] != "project_rule"]
        checks.extend(run_project_command(spec, root, output_dir, toolchain, args.timeout) for spec in commands)
        if not any(spec["kind"] == "test" for spec in commands):
            checks.append(CheckResult("project-tests", "correctness", SKIP, required=True, reason="no test commands configured"))
        if config["coverage"]:
            checks.append(run_coverage(config["coverage"], root, output_dir, toolchain, args.timeout))
        else:
            checks.append(CheckResult("llvm-cov", "sufficiency", SKIP, required=True, reason="coverage not configured"))
    if args.profile == "fuzz":
        if config["fuzz"]:
            checks.extend(run_project_command(spec, root, output_dir, toolchain, args.timeout) for spec in config["fuzz"])
        else:
            checks.append(CheckResult("libFuzzer", "correctness", SKIP, required=True, reason="no fuzz targets configured"))
    return checks


def project_report(args, root, toolchain, config, files, compdb, output_dir, sources):
    if args.misra_c2012 and args.profile == "fuzz":
        raise CLIError("MISRA-C:2012 is a quick/full check; run fuzz verification separately")
    if args.misra_c2012:
        config = merge(config, {"static": {"misra_c2012": True}})
    if config["static"].get("misra_c2012") and args.skip_cppcheck:
        raise CLIError("MISRA-C:2012 requires Cppcheck; remove --skip-cppcheck")
    checks = execute_checks(args, root, toolchain, config, files, compdb, output_dir)
    requirement_check, requirements = requirement_evidence(config, checks)
    if requirement_check:
        checks.append(requirement_check)
    if args.profile == "full" and not requirements:
        checks.append(CheckResult("acceptance", "sufficiency", PASS, required=False, gaps=["no acceptance requirements declared; business correctness is not established"]))
    if not args.profile == "fuzz":
        checks.append(CheckResult("maintainability-review", "maintainability", SKIP, required=False, reason="agent must inspect implementations, public contracts and callers; see references/quality-review.md"))
    if args.severity != "all":
        for check in checks:
            check.issues = [issue for issue in check.issues if issue.severity == "error" or (args.severity == "warning" and issue.severity in ("error", "warning"))]
    context = {
        "profile": args.profile,
        "project_root": str(root),
        "tool_platform": toolchain.platform,
        "tool_root": str(toolchain.root),
        "config_sources": sources,
        "compdb": str(compdb) if compdb else None,
        "selected_files": [path.relative_to(root).as_posix() for path in files],
        "report_path": str(output_dir / "report.json"),
    }
    document = full_document(context, checks, requirements)
    output_dir.mkdir(parents=True, exist_ok=True)
    Path(document["report_path"]).write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    return document


def run(args: argparse.Namespace) -> tuple[dict, int]:
    root = Path(args.project_root).resolve() if args.project_root else None
    if not (args.doctor or args.self_test):
        if root is None:
            raise CLIError("--project-root is required")
        if not root.is_dir():
            raise CLIError(f"project root is not a directory: {root}")
    output_dir = make_output_dir(args.output_dir, root)
    toolchain = Toolchain(SKILL_DIR)
    if args.doctor:
        checks = doctor(toolchain, output_dir)
        context = {"profile": "doctor", "tool_platform": toolchain.platform, "tool_root": str(toolchain.root), "report_path": str(output_dir / "report.json")}
        document = full_document(context, checks, [])
    elif args.self_test:
        process = run_process([Path(sys.executable), "-m", "unittest", "discover", "-s", SKILL_DIR / "tests", "-v"], cwd=SKILL_DIR, timeout_s=max(args.timeout, 600), log_path=output_dir / "logs" / "self-test.log", extra_env=toolchain.environment())
        output = process.stdout + process.stderr
        ran_match = re.search(r"Ran (\d+) tests?", output)
        tests_run = int(ran_match.group(1)) if ran_match else 0
        skipped_match = re.search(r"skipped=(\d+)", output)
        tests_skipped = int(skipped_match.group(1)) if skipped_match else 0
        if process.exit_code != 0:
            status = "FAIL"
        elif tests_skipped:
            status = SKIP
        elif tests_run == 0 or process.start_error or process.timed_out:
            status = ERROR
        else:
            status = PASS
        check = CheckResult("self-test", "correctness", status, command=process.command, exit_code=process.exit_code, duration_s=process.duration_s, log=process.log, metrics={"tests_run": tests_run, "tests_skipped": tests_skipped, "output_tail": output[-1000:]})
        if process.exit_code != 0:
            check.reason = output[-2000:]
        elif tests_skipped:
            check.reason = f"{tests_skipped} required runtime/self checks were skipped"
        elif status == ERROR:
            check.reason = process.start_error or "no self-tests ran or the process timed out"
        document = full_document({"profile": "self-test", "tool_platform": toolchain.platform, "report_path": str(output_dir / "report.json")}, [check], [])
    else:
        if args.baseline or args.git_staged or args.git_modified or args.git_all:
            document = run_incremental(args, root, toolchain, output_dir, load_config, project_report)
        else:
            config, sources = load_config(root, args.config)
            files = select_files(args, root, config)
            compdb = locate_compdb(root, args.compdb, config["static"])
            document = project_report(args, root, toolchain, config, files, compdb, output_dir, sources)
    report_path = Path(document["report_path"])
    report_path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
    return document, exit_code(document.get("gate", document["summary"]))


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = parser().parse_args(argv)
    try:
        document, status = run(args)
    except (CLIError, ToolUnavailable, ValueError, OSError, subprocess.TimeoutExpired) as error:
        document = {"schema_version": SCHEMA_VERSION, "status": ERROR, "complete": False, "reason": str(error)}
        status = 2
        output = json.dumps(document, ensure_ascii=False, indent=2 if args.detail else None, separators=None if args.detail else (",", ":"))
        if not args.quiet:
            print(output)
        return status
    if not args.quiet:
        print(render(document, args.format, args.detail))
    return status
