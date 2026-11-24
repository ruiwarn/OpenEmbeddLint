"""Built-in C/C++ checks and adapters for external tool output."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import csv
import fnmatch
import glob
import json
import re
import shutil
import sys
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape as xml_escape
from pathlib import Path
from typing import Sequence

from .compilation import (
    SOURCE_SUFFIXES,
    compdb_sources,
    compdb_system_includes,
    compilation_entries,
    expand_response_arguments,
    find_compdb_entry,
    private_driver_arguments,
    private_driver_compile_args,
    resolve_entry_driver_arguments,
    split_compiler_command,
)
from .installation import misra_rule_texts
from .execution import (
    ERROR,
    FAIL,
    PASS,
    SKIP,
    CheckResult,
    Issue,
    ProcessResult,
    Toolchain,
    ToolUnavailable,
    run_process,
)


DIAGNOSTIC = re.compile(
    r"^(?P<file>.*?):(?P<line>\d+):(?P<column>\d+):\s+"
    r"(?P<severity>warning|error|note):\s+(?P<message>.*?)(?:\s+\[(?P<rule>[^\]]+)\])?$"
)
SANITIZER_ERROR = re.compile(r"(?:ERROR:\s*(?:Address|Leak|Memory)Sanitizer|runtime error:)")


def relative(path: str | Path, root: Path) -> str:
    try:
        return Path(path).resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        return str(path)


def unavailable(check_id: str, dimension: str, required: bool, reason: str) -> CheckResult:
    return CheckResult(check_id, dimension, ERROR if required else SKIP, required=required, reason=reason)


def is_context_error(issue: Issue) -> bool:
    """Compilation-context failures remain incomplete even when their text is suppressed."""
    if issue.severity != "error":
        return False
    if issue.rule in ("preprocessorErrorDirective", "clang-diagnostic-error", "clang-tidy-config", "internalError", "syntaxError"):
        return True
    message = issue.message.lower()
    patterns = (
        "file not found", "no such file or directory", "unable to open", "cannot open",
        "could not open", "failed to include", "compilation database", "unsupported target",
        "unknown argument", "invalid compilation", "cannot find",
    )
    return any(pattern in message for pattern in patterns)


def _source_line(root: Path | None, file_path: str | None, line_no: int | None, cache: dict[str, list[str] | None]) -> str | None:
    if root is None or not file_path or line_no is None or line_no < 1:
        return None
    if file_path not in cache:
        candidate = Path(file_path)
        resolved = candidate if candidate.is_absolute() else root / candidate
        try:
            cache[file_path] = resolved.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            cache[file_path] = None
    lines = cache[file_path]
    return lines[line_no - 1] if lines is not None and line_no <= len(lines) else None


def append_issue(
    result: CheckResult,
    issue: Issue,
    suppressions: Sequence[dict],
    root: Path | None = None,
    line_cache: dict[str, list[str] | None] | None = None,
) -> None:
    """Suppress only an exact rule/path/message/symbol/line scope and retain an audit record."""
    cache = {} if line_cache is None else line_cache
    for suppression in suppressions:
        rule = suppression.get("check", "")
        path = suppression.get("path")
        message = suppression.get("message")
        symbol = suppression.get("symbol")
        line_pattern = suppression.get("line_pattern")
        reason = suppression.get("reason", "")
        if not rule or not reason or not fnmatch.fnmatchcase(issue.rule, rule):
            continue
        if path and (not issue.file or not fnmatch.fnmatchcase(issue.file.replace("\\", "/"), path.replace("\\", "/"))):
            continue
        if message and message not in issue.message:
            continue
        if line_pattern or symbol:
            src_line = _source_line(root, issue.file, issue.line, cache)
            if line_pattern and (src_line is None or re.search(line_pattern, src_line) is None):
                continue
            if symbol:
                sym_re = rf"(?<!\w){re.escape(symbol)}(?!\w)"
                if not ((src_line and re.search(sym_re, src_line)) or re.search(sym_re, issue.message)):
                    continue
        result.metrics.setdefault("suppressed", []).append({
            "issue": issue.__dict__.copy(),
            "reason": reason,
            "needs_manual_confirmation": suppression.get("needs_manual_confirmation", False),
        })
        return
    result.issues.append(issue)


def effective_exclude_dirs(exclude_directories: Sequence[str], files: Sequence[Path], root: Path) -> list[str]:
    selected_rel = [relative(file, root).replace("\\", "/") for file in files]
    patterns: list[str] = []
    for raw in exclude_directories:
        pattern = raw.strip().replace("\\", "/").strip("/")
        if not pattern or pattern == ".git":
            continue
        if any(item == pattern or item.startswith(pattern + "/") for item in selected_rel):
            continue
        if pattern not in patterns:
            patterns.append(pattern)
    return patterns


def is_excluded_header_issue(issue: Issue, root: Path, exclude_dirs: Sequence[str], selected_rel: set[str]) -> bool:
    if is_context_error(issue) or not issue.file:
        return False
    rel = issue.file.replace("\\", "/")
    if rel in selected_rel:
        return False
    path = Path(rel)
    if path.is_absolute():
        try:
            resolved_root = root.resolve()
            resolved_path = path.resolve()
            if not resolved_path.is_relative_to(resolved_root):
                return True
            rel = resolved_path.relative_to(resolved_root).as_posix()
        except OSError:
            return True
        if rel in selected_rel:
            return False
    return any(rel == pattern or rel.startswith(pattern + "/") for pattern in exclude_dirs)


def parse_clang_tidy(output: str, root: Path, dimension: str) -> tuple[list[Issue], list[str]]:
    issues: list[Issue] = []
    unparsed: list[str] = []
    for line in output.splitlines():
        match = DIAGNOSTIC.match(line)
        if not match:
            if re.match(r"^(warning|error):.*\[clang-tidy-config\]$", line):
                issues.append(Issue(rule="clang-tidy-config", message=line, severity="error", dimension="environment"))
            if "Error while processing" in line or "error generated" in line:
                unparsed.append(line)
            continue
        data = match.groupdict()
        if data["severity"] == "note":
            if issues:
                issues[-1].notes.append({"file": relative(data["file"], root), "line": int(data["line"]), "column": int(data["column"]), "message": data["message"]})
            continue
        rule = (data["rule"] or "clang-diagnostic").split(",")[0]
        issue_dimension = dimension
        if rule == "clang-tidy-config":
            data["severity"] = "error"
            issue_dimension = "environment"
        elif rule.startswith("custom-"):
            issue_dimension = "project_rules"
        elif rule.startswith("readability-") or rule == "bugprone-branch-clone":
            issue_dimension = "readability"
        issues.append(
            Issue(
                file=relative(data["file"], root),
                line=int(data["line"]),
                column=int(data["column"]),
                severity=data["severity"],
                message=data["message"],
                rule=rule,
                dimension=issue_dimension,
            )
        )
    return issues, unparsed


def _run_logs(result: ProcessResult, root: Path) -> dict:
    return {
        "command": result.command,
        "exit_code": result.exit_code,
        "duration_s": round(result.duration_s, 3),
        "log": relative(result.log, root) if result.log else None,
        "timed_out": result.timed_out,
        "start_error": result.start_error,
    }


def misra_arguments(toolchain, logs):
    candidates = [toolchain.root / path / "misra.py" for path in ("Library/bin/addons", "bin/addons", "share/Cppcheck/addons")]
    addon = next((path.resolve() for path in candidates if path.is_file()), None)
    if addon is None or not addon.is_relative_to(toolchain.root):
        raise ValueError("bundled MISRA-C:2012 addon not found in the private toolchain")
    logs.mkdir(parents=True, exist_ok=True)
    texts = misra_rule_texts()
    arguments = ["--severity=error"]
    if texts.is_file():
        staged_texts = logs / "misra-rule-texts.txt"
        staged_texts.write_bytes(texts.read_bytes())
        arguments.append("--rule-texts=" + staged_texts.name)
    specification = logs / "misra-addon.json"
    specification.write_text(json.dumps({"script": str(addon), "args": arguments}), encoding="utf-8")
    return ["--addon=" + str(specification), "--addon-python=" + sys.executable]


def _write_cppcheck_suppressions(
    logs: Path, root: Path, exclude_dirs: Sequence[str], external_includes: Sequence[Path],
) -> Path | None:
    patterns: list[str] = []
    resolved_root = root.resolve()
    for excl in exclude_dirs:
        candidate = resolved_root / excl
        if candidate.exists():
            patterns.append(candidate.resolve().as_posix() + "/*")
        patterns.append(excl + "/*")
    for inc in sorted(external_includes, key=lambda p: p.as_posix()):
        if not inc.is_relative_to(resolved_root):
            patterns.append(inc.as_posix() + "/*")
    if not patterns:
        return None
    seen: list[str] = []
    for pattern in patterns:
        if pattern not in seen:
            seen.append(pattern)
    logs.mkdir(parents=True, exist_ok=True)
    path = logs / "cppcheck-suppressions.xml"
    body = "".join(f"  <suppress><id>*</id><fileName>{xml_escape(pattern)}</fileName></suppress>\n" for pattern in seen)
    path.write_text(f'<?xml version="1.0" encoding="UTF-8"?>\n<suppressions>\n{body}</suppressions>\n', encoding="utf-8")
    return path


def record_cppcheck_output(
    result, process, root, file, suppressions,
    *, exclude_dirs: Sequence[str] = (), selected_rel: set[str] | None = None, line_cache: dict | None = None,
):
    try:
        document = ET.fromstring(process.stderr or "<errors></errors>")
    except ET.ParseError as error:
        result.status = ERROR
        result.reason = f"cannot parse cppcheck XML: {error}"
        return
    excluded = 0
    selected = selected_rel or set()
    for item in document.iter("error"):
        rule = item.get("id", "cppcheck")
        severity = item.get("severity", "warning")
        locations = list(item.iter("location"))
        location = locations[0] if locations else None
        issue = Issue(
            file=relative(location.get("file", str(file)), root) if location is not None else relative(file, root),
            line=int(location.get("line", "0")) if location is not None else None,
            column=int(location.get("column", "0")) if location is not None else None,
            severity="error" if severity == "error" else "warning" if severity != "information" else "info",
            message=item.get("msg", ""),
            rule=rule,
        )
        if is_context_error(issue):
            result.status = ERROR
            result.reason = f"compilation context is incomplete: {issue.message}"
        elif is_excluded_header_issue(issue, root, exclude_dirs, selected):
            excluded += 1
            continue
        append_issue(result, issue, suppressions, root=root, line_cache=line_cache)
    if excluded:
        result.metrics["excluded_header_diagnostics"] = result.metrics.get("excluded_header_diagnostics", 0) + excluded
    if process.exit_code != 0:
        result.status = ERROR
        result.reason = f"cppcheck exited {process.exit_code}"


def configured_misra(result, cases, toolchain, config, root, logs):
    if not config.get("misra_c2012"):
        return []
    c_files = [relative(file, root) for file, _ in cases if file.suffix.lower() == ".c"]
    texts = misra_rule_texts()
    result.metrics["misra_c2012"] = {"coverage": "partial", "c_translation_units": c_files, "rule_texts": str(texts) if texts.is_file() else None}
    result.gaps.append("MISRA-C:2012 addon provides partial automatic checking; whole-program, manual and compliance acceptance remain separate")
    if not c_files:
        result.gaps.append("MISRA-C:2012 not executed: no C translation units selected")
        return []
    return misra_arguments(toolchain, logs)


def cppcheck_context(compdb, config, root, logs):
    arguments = [f"-D{value}" for value in config.get("cppcheck_defines", [])]
    arguments.extend(f"-I{(root / value).resolve()}" for value in config.get("cppcheck_includes", []))
    if compdb is None or not arguments:
        return compdb, arguments
    entries = compilation_entries(compdb)
    for entry in entries:
        command = entry.get("arguments") or split_compiler_command(entry["command"])
        entry["arguments"] = [*expand_response_arguments(command, Path(entry["directory"])), *arguments]
        entry.pop("command", None)
    logs.mkdir(parents=True, exist_ok=True)
    database = logs / "cppcheck-compile_commands.json"
    database.write_text(json.dumps(entries), encoding="utf-8")
    return database, []


def run_cppcheck(
    files: Sequence[Path], root: Path, toolchain: Toolchain, config: dict, timeout_s: float, logs: Path, required: bool,
    compdb: Path | None = None, suppressions: Sequence[dict] = (),
    exclude_directories: Sequence[str] = (), jobs: int = 1, driver_cache: dict | None = None,
) -> CheckResult:
    original_compdb = compdb
    try:
        executable = toolchain.require("cppcheck")
        compdb, extra_arguments = cppcheck_context(compdb, config, root, logs)
    except (ToolUnavailable, OSError, ValueError) as error:
        return unavailable("cppcheck", "correctness", required, str(error))
    result = CheckResult("cppcheck", "correctness", PASS, required=required, tool=str(executable))
    if compdb:
        try:
            sources = compdb_sources(compdb)
        except ValueError as error:
            return unavailable("cppcheck", "correctness", required, str(error))
        selected_sources = [file for file in files if file.suffix.lower() in SOURCE_SUFFIXES]
        cases = [(file, alias) for file in selected_sources if file.resolve() in sources for alias in sorted(sources[file.resolve()])]
        missing = [f"no compilation database entry: {file}" for file in selected_sources if file.resolve() not in sources]
        result.gaps.extend(missing)
        if missing:
            result.status = ERROR
            result.reason = "; ".join(missing)
        result.gaps.extend(f"header is checked through an including translation unit: {file}" for file in files if file.suffix.lower() not in SOURCE_SUFFIXES)
        if not cases:
            result.status = ERROR if required else SKIP
            result.reason = "no C/C++ translation units selected with compilation context"
            return result
    else:
        cases = [(file, None) for file in files]
    try:
        misra = configured_misra(result, cases, toolchain, config, root, logs)
    except (OSError, ValueError) as error:
        result.status, result.reason = ERROR, str(error)
        return result

    exclude_dirs = effective_exclude_dirs(exclude_directories, files, root)
    selected_rel = {relative(file, root).replace("\\", "/") for file in files}
    probed_driver_args: list[str] = []
    if original_compdb is not None:
        cache = {} if driver_cache is None else driver_cache
        try:
            for file, _ in cases:
                entry = find_compdb_entry(original_compdb, file)
                if entry is not None:
                    d_args, _ = resolve_entry_driver_arguments(entry, toolchain, file.suffix.lower() != ".c", Path(entry["directory"]), cache=cache)
                    probed_driver_args.extend(d_args)
        except (ToolUnavailable, ValueError) as error:
            result.status = ERROR
            result.reason = str(error)
            return result
    external_includes = compdb_system_includes(original_compdb, probed_driver_args) if (original_compdb or probed_driver_args) else set()
    suppress_xml = _write_cppcheck_suppressions(logs, root, exclude_dirs, sorted(external_includes)) if (exclude_dirs or external_includes) else None
    environment = toolchain.environment()

    tasks = []
    for index, (file, project_file) in enumerate(cases):
        argv = [
            executable,
            f"--enable={config.get('cppcheck_enable', 'warning,performance,portability,style')}",
            "--inline-suppr",
            "--suppress=missingIncludeSystem",
            "--xml",
            "--xml-version=2",
        ]
        if suppress_xml is not None:
            argv.append(f"--suppress-xml={suppress_xml}")
        argv.extend(extra_arguments)
        case_build = None
        if misra and file.suffix.lower() == ".c":
            case_build = logs / f"cppcheck-build-{index}"
            case_build.mkdir(parents=True, exist_ok=True)
            argv.extend(misra)
            argv.append("--cppcheck-build-dir=" + str(case_build))
        if project_file is not None:
            # Cppcheck filters its original compdb spelling (e.g. /var, not /private/var).
            argv.extend([f"--project={compdb}", f"--file-filter={project_file}"])
        else:
            argv.append(file)
        tasks.append((index, file, argv, case_build))

    def _run_case(item):
        index, file, argv, case_build = item
        try:
            process = run_process(
                argv,
                cwd=logs if misra else root,
                timeout_s=timeout_s,
                log_path=logs / f"cppcheck-{index}.log",
                extra_env=environment,
            )
            return index, file, process
        finally:
            if case_build is not None:
                shutil.rmtree(case_build, ignore_errors=True)

    if jobs > 1 and len(tasks) > 1:
        with ThreadPoolExecutor(max_workers=min(jobs, len(tasks))) as pool:
            completed = list(pool.map(_run_case, tasks))
    else:
        completed = [_run_case(item) for item in tasks]

    line_cache: dict[str, list[str] | None] = {}
    for _, file, process in completed:
        result.metrics.setdefault("runs", []).append(_run_logs(process, root))
        result.duration_s += process.duration_s
        result.files.append(relative(file, root))
        if process.timed_out or process.start_error:
            result.status = ERROR
            result.reason = process.start_error or f"timeout after {timeout_s}s"
            continue
        record_cppcheck_output(
            result, process, root, file, suppressions,
            exclude_dirs=exclude_dirs, selected_rel=selected_rel, line_cache=line_cache,
        )
    if result.status == PASS and any(issue.severity == "error" for issue in result.issues):
        result.status = FAIL
    result.command = [str(executable), "<selected files>"]
    return result


def _tidy_files(files: Sequence[Path], compdb: Path | None, allow_without_compdb: bool) -> tuple[list[Path], list[str]]:
    if compdb is None:
        if allow_without_compdb:
            return [file for file in files if file.suffix.lower() in SOURCE_SUFFIXES], []
        return [], ["compilation database not found"]
    sources = compdb_sources(compdb)
    selected = [file for file in files if file.suffix.lower() in SOURCE_SUFFIXES]
    present = [file for file in selected if file.resolve() in sources]
    gaps = [f"no compilation database entry: {file}" for file in selected if file.resolve() not in sources]
    gaps.extend(f"header requires an including translation unit: {file}" for file in files if file.suffix.lower() not in SOURCE_SUFFIXES)
    return present, gaps


def clang_tidy_config(file: Path, root: Path, skill_dir: Path) -> Path:
    for directory in file.parents:
        candidate = directory / ".clang-tidy"
        if candidate.is_file():
            return candidate
        if directory == root:
            break
    return skill_dir / "defaults" / ".clang-tidy"


def record_tidy_output(
    result: CheckResult, process: ProcessResult, root: Path, suppressions: Sequence[dict], timeout_s: float,
    *, exclude_dirs: Sequence[str] = (), selected_rel: set[str] | None = None, line_cache: dict | None = None,
) -> None:
    output = process.stdout + "\n" + process.stderr
    issues, unparsed = parse_clang_tidy(output, root, "correctness")
    raw_error = any(issue.severity == "error" for issue in issues)
    context_errors = [issue for issue in issues if is_context_error(issue)]
    if context_errors:
        result.status = ERROR
        result.reason = "compilation context is incomplete: " + context_errors[0].message
    excluded = 0
    selected = selected_rel or set()
    for issue in issues:
        if not is_context_error(issue) and is_excluded_header_issue(issue, root, exclude_dirs, selected):
            excluded += 1
            continue
        append_issue(result, issue, suppressions, root=root, line_cache=line_cache)
    if excluded:
        result.metrics["excluded_header_diagnostics"] = result.metrics.get("excluded_header_diagnostics", 0) + excluded
    if process.timed_out or process.start_error or (unparsed and not raw_error) or (process.exit_code != 0 and not raw_error):
        result.status = ERROR
        result.reason = process.start_error or "; ".join(unparsed) or (f"timeout after {timeout_s}s" if process.timed_out else f"clang-tidy exited {process.exit_code}")


def run_clang_tidy(
    files: Sequence[Path],
    root: Path,
    toolchain: Toolchain,
    config: dict,
    timeout_s: float,
    logs: Path,
    required: bool,
    compdb: Path | None,
    suppressions: Sequence[dict] = (),
    checks_override: str | None = None,
    header_filter: str | None = None,
    exclude_directories: Sequence[str] = (),
    jobs: int = 1,
    driver_cache: dict | None = None,
) -> CheckResult:
    check_id = "clang-tidy"
    try:
        executable = toolchain.require("clang-tidy")
    except ToolUnavailable as error:
        return unavailable(check_id, "correctness", required, str(error))
    result = CheckResult(check_id, "correctness", PASS, required=required, tool=str(executable))
    if compdb is None:
        result.gaps.append("compilation database not found")
    try:
        selected, gaps = _tidy_files(files, compdb, config.get("allow_without_compdb", False))
    except ValueError as error:
        return unavailable(check_id, "correctness", required, str(error))
    result.gaps.extend(gaps)
    missing_context = [gap for gap in gaps if gap.startswith("no compilation database entry:")]
    if missing_context:
        result.status = ERROR
        result.reason = "; ".join(missing_context)
    if not selected:
        result.status = ERROR if required else SKIP
        result.reason = "; ".join(gaps) or "no C/C++ translation units selected"
        return result

    cache = {} if driver_cache is None else driver_cache
    prepared = []
    for file in selected:
        driver_args: list[str] = []
        is_cpp = file.suffix.lower() != ".c"
        try:
            if compdb is None:
                key = ("private", is_cpp, ())
                if key not in cache:
                    cache[key] = private_driver_arguments(toolchain, is_cpp, root, [])
                driver_args, query = cache[key]
                result.metrics.setdefault("driver_queries", []).append(query)
            else:
                entry = find_compdb_entry(compdb, file)
                if entry is not None:
                    driver_args, query = resolve_entry_driver_arguments(entry, toolchain, is_cpp, Path(entry["directory"]), cache=cache)
                    if query is not None:
                        result.metrics.setdefault("driver_queries", []).append(query)
        except (ToolUnavailable, ValueError) as error:
            result.status = ERROR
            result.reason = str(error)
            continue
        native_config = clang_tidy_config(file, root, toolchain.skill_dir)
        result.metrics.setdefault("config_files", {})[relative(file, root)] = str(native_config)
        argv = [executable, "--experimental-custom-checks", f"--config-file={native_config}", *[f"--extra-arg={arg}" for arg in [*driver_args, *config.get("extra_args", [])]], file]
        if checks_override:
            argv.insert(1, f"--checks={checks_override}")
        if compdb:
            argv[1:1] = ["-p", str(compdb.parent)]
        else:
            argv.extend(["--", "-std=c++17" if is_cpp else "-std=c11", f"-I{root}", *driver_args])
        if header_filter is not None:
            argv.insert(1, f"--header-filter={header_filter}")
        prepared.append((len(prepared), file, argv))

    environment = toolchain.environment()

    def _run_tidy(item):
        index, file, argv = item
        process = run_process(
            argv,
            cwd=root,
            timeout_s=timeout_s,
            log_path=logs / f"clang-tidy-{index}.log",
            extra_env=environment,
        )
        return index, file, process

    if jobs > 1 and len(prepared) > 1:
        with ThreadPoolExecutor(max_workers=min(jobs, len(prepared))) as pool:
            completed = list(pool.map(_run_tidy, prepared))
    else:
        completed = [_run_tidy(item) for item in prepared]

    exclude_dirs = effective_exclude_dirs(exclude_directories, files, root)
    selected_rel = {relative(file, root).replace("\\", "/") for file in files}
    line_cache: dict[str, list[str] | None] = {}
    for _, file, process in completed:
        result.metrics.setdefault("runs", []).append(_run_logs(process, root))
        result.duration_s += process.duration_s
        result.files.append(relative(file, root))
        record_tidy_output(
            result, process, root, suppressions, timeout_s,
            exclude_dirs=exclude_dirs, selected_rel=selected_rel, line_cache=line_cache,
        )
    if result.status != ERROR and any(issue.severity == "error" for issue in result.issues):
        result.status = FAIL
    result.command = [str(executable), "<selected translation units>"]
    return result


def run_cyclomatic(
    files: Sequence[Path], root: Path, toolchain: Toolchain, config: dict, timeout_s: float, logs: Path
) -> CheckResult:
    """Report McCabe CCN separately from clang-tidy's cognitive complexity."""
    required = config.get("required", False)
    private_root = toolchain.skill_dir / "tools" / "python" / "lizard-1.25.0"
    module = private_root / "lizard.py"
    if not module.is_file():
        return unavailable("cyclomatic-complexity", "readability", required, f"private Lizard module missing: {module}")
    bootstrap = (
        "import sys;"
        f"sys.path=[{json.dumps(str(private_root))}]+sys.path[1:];"
        "sys.argv[0]='lizard';"
        "import runpy;"
        "runpy.run_module('lizard',run_name='__main__')"
    )
    argv = [Path(sys.executable), "-c", bootstrap, "--csv", *files]
    process = run_process(argv, cwd=root, timeout_s=timeout_s, log_path=logs / "cyclomatic-complexity.log", extra_env=toolchain.environment())
    policy = config.get("policy", "blocking")
    metric_policies = config.get("metric_policies", {})
    result = CheckResult(
        "cyclomatic-complexity", "readability", PASS, required=required,
        tool=str(module), command=process.command, exit_code=process.exit_code,
        duration_s=process.duration_s, log=process.log, policy=policy,
        files=[relative(file, root) for file in files], metrics={"functions": []},
    )
    if process.timed_out or process.start_error or process.exit_code != 0:
        result.status = ERROR
        result.reason = process.start_error or (f"timeout after {timeout_s}s" if process.timed_out else process.stderr[-1000:] or f"lizard exited {process.exit_code}")
        return result
    try:
        threshold = int(config.get("ccn_threshold", 30))
        nloc_threshold = int(config.get("nloc_threshold", 100))
        length_threshold = int(config.get("length_threshold", 1000))
        for row in csv.reader(process.stdout.splitlines()):
            if not row:
                continue
            if len(row) != 11:
                raise ValueError(f"unexpected Lizard CSV row: {row}")
            function = {
                "file": relative(row[6], root), "function": row[7], "line": int(row[9]),
                "ccn": int(row[1]), "nloc": int(row[0]), "parameters": int(row[3]), "length": int(row[4]),
            }
            result.metrics["functions"].append(function)
            if function["ccn"] > threshold:
                result.issues.append(Issue(
                    file=function["file"], line=function["line"], rule="cyclomatic-complexity",
                    message=f"function '{function['function']}' has McCabe CCN {function['ccn']} (threshold {threshold})",
                    severity="warning", dimension="readability",
                ))
            if function["nloc"] > nloc_threshold:
                result.issues.append(Issue(
                    file=function["file"], line=function["line"], rule="function-nloc",
                    message=f"function '{function['function']}' has NLOC {function['nloc']} (threshold {nloc_threshold})",
                    severity="warning", dimension="readability",
                ))
            if function["length"] > length_threshold:
                result.issues.append(Issue(
                    file=function["file"], line=function["line"], rule="function-length",
                    message=f"function '{function['function']}' has total length {function['length']} lines (threshold {length_threshold})",
                    severity="warning", dimension="readability",
                ))
    except (ValueError, IndexError) as error:
        result.status = ERROR
        result.reason = f"cannot parse Lizard CSV: {error}"
        return result
    metric_for_rule = {"cyclomatic-complexity": "ccn", "function-nloc": "nloc", "function-length": "length"}
    for issue in result.issues:
        if metric_policies.get(metric_for_rule[issue.rule], policy) == "blocking":
            issue.severity = "error"
    if any(metric_policies.get(metric_for_rule[issue.rule], policy) == "blocking" for issue in result.issues):
        result.status = FAIL
    result.metrics["metric_policies"] = {metric: metric_policies.get(metric, policy) for metric in ("ccn", "nloc", "length")}
    return result


def _format_config(file: Path, root: Path, explicit: str | None) -> Path | None:
    if explicit:
        path = Path(explicit)
        return (path if path.is_absolute() else root / path).resolve()
    current = file.resolve().parent
    root = root.resolve()
    while current.is_relative_to(root):
        for name in (".clang-format", "_clang-format"):
            candidate = current / name
            if candidate.is_file():
                return candidate
        if current == root:
            break
        current = current.parent
    return None


def run_clang_format(
    files: Sequence[Path], root: Path, toolchain: Toolchain, config: dict, timeout_s: float, logs: Path, jobs: int = 1,
) -> CheckResult:
    required = config.get("required", False)
    try:
        executable = toolchain.require("clang-format")
    except ToolUnavailable as error:
        return unavailable("clang-format", "style", required, str(error))
    result = CheckResult("clang-format", "style", PASS, required=required, tool=str(executable))
    missing = []
    tasks = []
    for file in files:
        format_config = _format_config(file, root, config.get("config"))
        if format_config is None or not format_config.is_file():
            missing.append(relative(file, root))
            continue
        style = f"file:{format_config}"
        tasks.append((len(tasks), file, style))

    environment = toolchain.environment()

    def _run_fmt(item):
        index, file, style = item
        process = run_process(
            [executable, "--dry-run", "--Werror", f"--style={style}", file],
            cwd=root, timeout_s=timeout_s,
            log_path=logs / f"clang-format-{index}.log",
            extra_env=environment,
        )
        return index, file, process

    if jobs > 1 and len(tasks) > 1:
        with ThreadPoolExecutor(max_workers=min(jobs, len(tasks))) as pool:
            completed = list(pool.map(_run_fmt, tasks))
    else:
        completed = [_run_fmt(item) for item in tasks]

    for _, file, process in completed:
        result.metrics.setdefault("runs", []).append(_run_logs(process, root))
        result.duration_s += process.duration_s
        result.files.append(relative(file, root))
        if process.timed_out or process.start_error:
            result.status = ERROR
            result.reason = process.start_error or f"timeout after {timeout_s}s"
        elif process.exit_code == 1:
            result.status = FAIL
            result.issues.append(Issue(file=relative(file, root), rule="clang-format", message=process.stderr.strip() or "formatting differs"))
        elif process.exit_code != 0:
            result.status = ERROR
            result.reason = process.stderr.strip() or f"clang-format exited {process.exit_code}"
    if missing:
        result.gaps.append("no .clang-format for: " + ", ".join(missing))
        if not result.files:
            result.status = ERROR if required else SKIP
            result.reason = result.gaps[-1]
    result.command = [str(executable), "--dry-run", "--Werror"]
    return result


def _expand(value: str, root: Path, output_dir: Path, toolchain: Toolchain) -> str:
    replacements = {"project_root": str(root), "output_dir": str(output_dir), "platform": toolchain.platform, "python": sys.executable, "skill_dir": str(toolchain.skill_dir), "exe_suffix": ".exe" if toolchain.is_windows else ""}
    for key, replacement in replacements.items():
        value = value.replace("{" + key + "}", replacement)
    for name in re.findall(r"\{tool:([A-Za-z0-9_+.-]+)\}", value):
        value = value.replace("{tool:" + name + "}", str(toolchain.require(name)))
    return value


def parse_project_diagnostics(output: str, root: Path) -> list[Issue]:
    document = json.loads(output)
    if not isinstance(document, dict) or set(document) != {"issues"} or not isinstance(document["issues"], list):
        raise ValueError("project rule output must be an object containing an issues array")
    issues = []
    for item in document["issues"]:
        if not isinstance(item, dict) or set(item) - {"rule", "message", "severity", "file", "line", "column"}:
            raise ValueError("invalid project rule diagnostic fields")
        if any(not isinstance(item.get(key), str) or not item[key].strip() for key in ("rule", "message")):
            raise ValueError("project rule diagnostic requires non-empty rule and message")
        if item.get("severity", "error") not in ("error", "warning", "info"):
            raise ValueError("invalid project rule diagnostic severity")
        if item.get("file") is not None and not isinstance(item["file"], str):
            raise ValueError("project rule diagnostic file must be a string")
        if any(item.get(key) is not None and (type(item[key]) is not int or item[key] < 1) for key in ("line", "column")):
            raise ValueError("project rule diagnostic line/column must be positive integers")
        issue = Issue(**{"severity": "error", **item}, dimension="project_rules")
        if issue.file:
            issue.file = relative(root / issue.file, root)
        issues.append(issue)
    return issues


def record_project_rule_output(result: CheckResult, process: ProcessResult, root: Path, suppressions: Sequence[dict]) -> None:
    if process.exit_code not in (0, 1) or SANITIZER_ERROR.search(process.stdout + "\n" + process.stderr):
        result.status = ERROR
        result.reason = f"project rule checker failed (exit {process.exit_code}); see log"
        return
    try:
        issues = parse_project_diagnostics(process.stdout, root)
        if process.exit_code == 1 and not any(issue.severity == "error" for issue in issues):
            raise ValueError("project rule exit 1 requires an error diagnostic")
        line_cache: dict[str, list[str] | None] = {}
        for issue in issues:
            append_issue(result, issue, suppressions, root=root, line_cache=line_cache)
        if any(issue.severity == "error" for issue in result.issues):
            result.status = FAIL
    except (TypeError, ValueError) as error:
        result.status = ERROR
        result.reason = f"invalid project rule output: {error}"


def run_project_command(
    spec: dict, root: Path, output_dir: Path, toolchain: Toolchain, default_timeout: float,
    files: Sequence[Path] = (), suppressions: Sequence[dict] = (),
) -> CheckResult:
    check_id = spec.get("id", "command")
    kind = spec.get("kind", "test")
    dimension = "project_rules" if kind == "project_rule" else "correctness"
    required = spec.get("required", True)
    try:
        raw_argv = spec["argv"]
        if not isinstance(raw_argv, list) or not raw_argv:
            raise ValueError("argv must be a non-empty array")
        argv = []
        for part in raw_argv:
            if part == "{files}":
                argv.extend(str(file) for file in files)
            else:
                argv.append(_expand(str(part), root, output_dir, toolchain))
        if not argv or not Path(argv[0]).is_absolute():
            raise ValueError(f"command executable must be absolute or use {{tool:name}}/{{python}}: {argv[:1]}")
        cwd = (_expand(str(spec.get("cwd", ".")), root, output_dir, toolchain))
        cwd_path = (root / cwd).resolve() if not Path(cwd).is_absolute() else Path(cwd).resolve()
        if not cwd_path.is_relative_to(root.resolve()) and not cwd_path.is_relative_to(output_dir.resolve()):
            raise ValueError("command cwd must stay inside project root or its dedicated output directory")
        environment = toolchain.environment()
        for key, value in spec.get("env", {}).items():
            environment[key] = _expand(str(value), root, output_dir, toolchain)
    except (KeyError, ValueError, ToolUnavailable) as error:
        return unavailable(check_id, dimension, required, str(error))
    timeout_s = float(spec.get("timeout_s", default_timeout))
    process = run_process(argv, cwd=cwd_path, timeout_s=timeout_s, log_path=output_dir / "logs" / f"{check_id}.log", extra_env=environment)
    result = CheckResult(
        check_id, dimension, PASS, required=required, command=process.command,
        exit_code=process.exit_code, duration_s=process.duration_s, log=process.log,
        metrics={"kind": kind, "evidence": spec.get("evidence", []), "requirements": spec.get("requirements", [])},
    )
    output = process.stdout + "\n" + process.stderr
    if process.timed_out or process.start_error:
        result.status = ERROR
        result.reason = process.start_error or f"timeout after {timeout_s}s"
    elif kind == "project_rule":
        record_project_rule_output(result, process, root, suppressions)
    elif process.exit_code != 0 or SANITIZER_ERROR.search(output):
        result.status = FAIL
        result.reason = f"command exited {process.exit_code}" if process.exit_code else "sanitizer diagnostic with zero exit code"
        result.issues.append(Issue(rule=kind, message=output[-2000:].strip() or result.reason))
    return result


def run_coverage(config: dict, root: Path, output_dir: Path, toolchain: Toolchain, timeout_s: float) -> CheckResult:
    required = config.get("required", True)
    check_id = config.get("id", "llvm-cov")
    try:
        profdata_tool = toolchain.require("llvm-profdata")
        cov_tool = toolchain.require("llvm-cov")
        binary = Path(_expand(config["binary"], root, output_dir, toolchain))
        binary = binary if binary.is_absolute() else root / binary
        patterns = config.get("profraw", [])
        inputs = []
        for pattern in patterns:
            expanded = _expand(pattern, root, output_dir, toolchain)
            path = Path(expanded)
            inputs.extend(glob.glob(expanded, recursive=True) if path.is_absolute() else root.glob(expanded))
        inputs = sorted(set(Path(item).resolve() for item in inputs if Path(item).is_file()))
        if not inputs:
            raise ValueError("no .profraw files matched")
        if not binary.is_file():
            raise ValueError(f"coverage executable not found: {binary}")
        profdata = Path(_expand(config.get("profdata", str(output_dir / "default.profdata")), root, output_dir, toolchain))
        profdata = profdata if profdata.is_absolute() else root / profdata
    except (KeyError, ValueError, ToolUnavailable) as error:
        return unavailable(check_id, "sufficiency", required, str(error))
    merge = run_process([profdata_tool, "merge", "-sparse", "-o", profdata, *inputs], cwd=root, timeout_s=timeout_s, log_path=output_dir / "logs" / "llvm-profdata.log", extra_env=toolchain.environment())
    if merge.exit_code != 0 or merge.start_error or merge.timed_out:
        result = unavailable(check_id, "sufficiency", required, merge.start_error or merge.stderr.strip() or "profile merge failed")
        result.command = merge.command
        result.log = merge.log
        return result
    sources = [_expand(source, root, output_dir, toolchain) for source in config.get("sources", [])]
    export_path = output_dir / "coverage-export.json"
    export = run_process([cov_tool, "export", "--summary-only", "-instr-profile", str(profdata), str(binary), *sources], cwd=root, timeout_s=timeout_s, log_path=output_dir / "logs" / "llvm-cov-export.log", extra_env=toolchain.environment())
    if export.exit_code != 0 or export.start_error or export.timed_out:
        result = unavailable(check_id, "sufficiency", required, export.start_error or export.stderr.strip() or "coverage export failed")
        result.command = export.command
        result.log = export.log
        return result
    export_path.write_text(export.stdout, encoding="utf-8")
    show = run_process([cov_tool, "show", "--show-branches=count", "--show-line-counts-or-regions", "-instr-profile", str(profdata), str(binary)], cwd=root, timeout_s=timeout_s, log_path=output_dir / "logs" / "llvm-cov-show.log", extra_env=toolchain.environment())
    show_path = output_dir / "coverage-show.txt"
    show_path.write_text(show.stdout + show.stderr, encoding="utf-8")
    show_diagnostic = re.search(r"(?i)(?:\berror\b|file not found|no such file or directory)", show.stderr)
    if show.timed_out or show.start_error or show.exit_code != 0 or not show.stdout.strip() or show_diagnostic:
        reason = show.start_error or (f"timeout after {timeout_s}s" if show.timed_out else show.stderr.strip() or "llvm-cov show produced no detailed report")
        return CheckResult(
            check_id, "sufficiency", ERROR, required=required, tool=str(cov_tool),
            command=show.command, exit_code=show.exit_code, duration_s=show.duration_s,
            log=show.log, reason=reason,
            gaps=[f"detailed llvm-cov show report unavailable: {reason}"],
        )
    result = CheckResult(check_id, "sufficiency", PASS, required=required, tool=str(cov_tool), command=export.command, log=str(show_path), metrics={"profile": str(profdata), "export": str(export_path), "requested_sources": sources, "files": []})
    try:
        document = json.loads(export.stdout)
        data = document.get("data", [])
        files = data[0].get("files", []) if data else []
        if not files and data:
            files = [{"filename": "totals", "summary": data[0].get("totals", {})}]
        for file in files:
            summary = file.get("summary", {})
            lines = summary.get("lines", {})
            branches = summary.get("branches", {})
            item = {"file": relative(file.get("filename", "totals"), root), "lines": lines, "branches": branches}
            result.metrics["files"].append(item)
            for name, values in (("line", lines), ("branch", branches)):
                count = values.get("count", 0)
                covered = values.get("covered", 0)
                if count and covered < count:
                    result.gaps.append(f"{item['file']}: {covered}/{count} {name}s covered; see {show_path}")
    except (ValueError, KeyError, IndexError, TypeError) as error:
        return unavailable(check_id, "sufficiency", required, f"cannot parse coverage JSON: {error}")
    return result
