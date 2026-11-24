import copy
import hashlib
import json
import os
import re
import tempfile
import types
from pathlib import Path

from .. import __version__
from ..compilation import HEADER_SUFFIXES, SOURCE_SUFFIXES, compilation_entries, locate_compdb
from ..installation import cache_directory, misra_rule_texts
from ..report import dump_report_json
from .attribution import compare_reports
from .context import including_units, input_digest, remap_config, remap_database, validate_compilation_scope
from .snapshots import POLICY_FILES, capture, file_digest, git, safe_path, synchronize_policy

BUILD_INPUT_NAMES = {
    "compile_commands.json", "CMakeLists.txt", "CMakeCache.txt", "CMakePresets.json", "CMakeUserPresets.json",
    "Makefile", "GNUmakefile", "makefile", "Makefile.am", "Makefile.in",
    "meson.build", "meson_options.txt", "build.ninja", "rules.ninja",
    "Kconfig", "SConstruct", "SConscript", "conanfile.txt", "conanfile.py",
    "configure", "configure.ac", "configure.in",
}
BUILD_INPUT_SUFFIXES = {
    ".cmake", ".mk", ".mak", ".ninja", ".config", ".kconfig", ".defconfig",
    ".ld", ".lds", ".sct", ".icf", ".rsp", ".props", ".targets", ".vcxproj",
    ".sln", ".cbp",
}


def selected_names(args, origin, current, changed, config):
    names = set(changed) if args.git_staged or args.git_modified or args.git_all or not (args.file or args.directory) else set()
    for value in args.file:
        path = Path(value)
        relative = path.resolve().relative_to(origin) if path.is_absolute() else path
        if not safe_path(current, relative.as_posix()).is_file():
            raise ValueError("selected snapshot file not found: " + str(relative))
        names.add(relative.as_posix())
    if args.directory:
        path = Path(args.directory)
        relative = path.resolve().relative_to(origin) if path.is_absolute() else path
        directory = safe_path(current, relative.as_posix())
        if not directory.is_dir():
            raise ValueError("selected snapshot directory not found: " + str(relative))
        names.update(path.relative_to(current).as_posix() for path in directory.rglob("*") if path.is_file())
    patterns = [pattern.rstrip("/") for pattern in config["exclude_directories"]]
    return {name for name in names if Path(name).suffix.lower() in SOURCE_SUFFIXES | HEADER_SUFFIXES and not any(name == pattern or name.startswith(pattern + "/") for pattern in patterns)}


def _is_build_input(path: Path) -> bool:
    return path.name in BUILD_INPUT_NAMES or path.suffix.lower() in BUILD_INPUT_SUFFIXES


def context_gaps(changed, before, after):
    gaps, advisory = [], []
    for name in sorted(changed):
        path = Path(name)
        if path.suffix.lower() in SOURCE_SUFFIXES | HEADER_SUFFIXES | {".md", ".rst"} or path.name in POLICY_FILES:
            continue
        if path.name == "compile_commands.json":
            gaps.append("tracked compilation database changed; verify each revision with its own build context")
        elif file_digest(before / name) != file_digest(after / name):
            if _is_build_input(path):
                gaps.append("changed non-source input requires separate verification: " + name)
            else:
                advisory.append(name)
    return gaps, advisory


def prepare_context(args, root, before, after, config, output, renames):
    database = locate_compdb(root, args.compdb, config["static"])
    if database is None:
        raise ValueError("incremental analysis requires a current compilation database")
    digest = input_digest([database])
    databases, configs = [], []
    for label, tree in (("baseline", before), ("current", after)):
        directory = output / label
        directory.mkdir(parents=True)
        databases.append(remap_database(database, root, tree, directory / "compile_commands.json", after, renames if label == "baseline" else None, digest))
        configs.append(remap_config(config, root, tree, after))
    if input_digest(digest) != digest:
        raise ValueError("compilation database changed while preparing analysis")
    return databases, configs, digest


def expand_headers(names, origin, before, after, databases, configs, toolchain, output, timeout, inputs, jobs=1, driver_cache=None):
    gaps, external = [], set()
    for index, tree in enumerate((before, after)):
        headers = {(tree / name).resolve() for name in names if Path(name).suffix.lower() in HEADER_SUFFIXES and (tree / name).is_file()}
        units, missing, dependencies = including_units(tree, headers, databases[index], configs[index], toolchain, output / ("baseline" if index == 0 else "current"), timeout, None if headers else names, jobs=jobs, driver_cache=driver_cache)
        names.update(units)
        gaps.extend(missing)
        external.update(dependencies)
    gaps.extend("dependency bypassed the source snapshot: " + str(path) for path in external if path.is_relative_to(origin) and (after / path.relative_to(origin)).is_file())
    inputs.update(input_digest(external))
    return gaps


def snapshot_manifest(tree):
    return {path.relative_to(tree).as_posix(): file_digest(path) for path in tree.rglob("*") if path.is_file() or path.is_symlink()}


def snapshot_scope(args, root, destination, load_config):
    extras = [Path(value).resolve().relative_to(root).as_posix() if Path(value).is_absolute() else value for value in args.file]
    before, after, changed, renames, identity = capture(root, args.baseline or "HEAD", args.git_staged, destination, extras, args.timeout)
    if args.git_all or args.baseline and not (args.file or args.directory or args.git_staged or args.git_modified):
        changed.update(path.relative_to(after).as_posix() for path in after.rglob("*") if path.is_file() and path.suffix.lower() in SOURCE_SUFFIXES | HEADER_SUFFIXES and not (before / path.relative_to(after)).exists())
    explicit = args.config
    if explicit and Path(explicit).resolve().is_relative_to(root):
        explicit = str(after / Path(explicit).resolve().relative_to(root))
    config, sources = load_config(after, explicit)
    gaps, advisory = context_gaps(changed, before, after)
    if args.git_staged:
        staged_gaps, staged_advisory = context_gaps(snapshot_manifest(after), after, root)
        gaps.extend(staged_gaps)
        advisory.extend(staged_advisory)
    inheritance = r'(?i)["\x27]?InheritParentConfig["\x27]?\s*:\s*(true|yes|1)\b'
    if any(re.search(inheritance, path.read_text(encoding="utf-8")) for path in after.rglob(".clang-tidy")):
        if any((parent / ".clang-tidy").is_file() for tree in (root, after) for parent in tree.parents):
            gaps.append("clang-tidy inheritance outside the repository is not frozen; use self-contained project policies")
    names = selected_names(args, root, after, changed, config)
    if not names and not args.allow_empty:
        raise ValueError("no C/C++ files selected; use --allow-empty only for a legitimate empty scope")
    synchronize_policy(before, after)
    return before, after, names, renames, identity, config, sources, gaps, sorted(set(advisory))


def verify_origin(root, identity, manifest, staged, timeout):
    gaps = []
    current_index = hashlib.sha256(git(root, "ls-files", "--stage", "-z", timeout=timeout)).hexdigest()
    if current_index != identity["index_digest"] or git(root, "rev-parse", "HEAD", timeout=timeout).decode().strip() != identity["current_commit"]:
        gaps.append("repository index or HEAD changed during verification")
    if not staged and any(file_digest(root / name) != digest for name, digest in manifest.items()):
        gaps.append("worktree inputs changed after the verification snapshot was captured")
    return gaps


def _normalize_text(value: str, before: Path, after: Path) -> str:
    for prefix, token in ((str(before), "<before>"), (before.as_posix(), "<before>"), (str(after), "<after>"), (after.as_posix(), "<after>")):
        value = value.replace(prefix, token)
    return value


def _baseline_cache_key(args, toolchain, identity, side_names, before, after, database, config, digest, compdb_source):
    resolved_compdb = str(compdb_source.resolve()) if compdb_source else None
    before_resolved = before.resolve()
    entries = []
    for entry in compilation_entries(database):
        source = Path(entry["file"])
        source = source if source.is_absolute() else Path(entry["directory"]) / source
        try:
            rel = source.resolve().relative_to(before_resolved).as_posix()
        except ValueError:
            continue
        if rel not in side_names:
            continue
        entries.append({
            "directory": _normalize_text(entry["directory"], before, after),
            "file": rel,
            "arguments": [_normalize_text(arg, before, after) for arg in entry["arguments"]],
        })
    policies = sorted(
        (path.relative_to(before).as_posix(), file_digest(path))
        for pattern in (".clang-tidy", ".clang-format", "_clang-format")
        for path in before.rglob(pattern) if path.is_file()
    )
    ext_digests = sorted(
        (_normalize_text(k, before, after), v)
        for k, v in digest.items()
        if resolved_compdb is None or str(Path(k).resolve()) != resolved_compdb
    )
    payload = {
        "version": __version__,
        "platform": toolchain.platform,
        "tool_root": str(toolchain.root),
        "commit": identity["baseline_commit"],
        "files": [(rel, file_digest(before / rel)) for rel in sorted(side_names)],
        "policies": policies,
        "compdb": entries,
        "config": _normalize_text(json.dumps(config, sort_keys=True), before, after),
        "external": ext_digests,
        "misra_texts": file_digest(misra_rule_texts()) if (args.misra_c2012 or config["static"].get("misra_c2012")) else None,
        "flags": {
            "checks": args.checks, "header_filter": args.header_filter, "severity": args.severity,
            "skip_cppcheck": args.skip_cppcheck, "skip_clang_tidy": args.skip_clang_tidy,
            "skip_format": args.skip_format, "skip_cyclomatic": args.skip_cyclomatic,
            "misra_c2012": bool(args.misra_c2012),
        },
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _prune_cache(directory: Path, max_entries: int = 64) -> None:
    try:
        items = sorted((p for p in directory.glob("*.json") if p.is_file()), key=lambda p: p.stat().st_mtime, reverse=True)
        for stale in items[max_entries:]:
            stale.unlink(missing_ok=True)
    except OSError:
        pass


def run_incremental(args, root, toolchain, output, load_config, scan):
    if args.profile == "fuzz":
        raise ValueError("incremental gates apply to static analysis; run fuzz verification separately")
    if output.is_relative_to(root):
        raise ValueError("incremental artifacts must be outside the source repository")
    jobs = getattr(args, "jobs", None)
    if jobs is None:
        jobs = min(4, os.cpu_count() or 1)
    driver_cache = {}
    with tempfile.TemporaryDirectory(prefix="snapshots-", dir=output) as temporary:
        before, after, names, renames, identity, config, sources, gaps, advisory = snapshot_scope(args, root, Path(temporary), load_config)
        databases, configs, digest = prepare_context(args, root, before, after, config, output, renames)
        if args.misra_c2012 or config["static"].get("misra_c2012"):
            digest.update(input_digest([misra_rule_texts()]))
        digest.update(input_digest(Path(source) for source in sources))
        gaps.extend(expand_headers(names, root, before, after, databases, configs, toolchain, output, args.timeout, digest, jobs=jobs, driver_cache=driver_cache))
        manifests = {"baseline": snapshot_manifest(before), "current": snapshot_manifest(after)}
        current_args = copy.copy(args)
        current_args.header_filter = args.header_filter if args.header_filter is not None else ".*"
        current_args.jobs = jobs
        current_args._driver_cache = driver_cache
        baseline_args = copy.copy(current_args)
        baseline_args.profile = "quick"
        configs[0]["commands"] = []

        cache_allowed = (
            not getattr(args, "no_cache", False)
            and isinstance(scan, types.FunctionType)
            and scan.__module__.endswith("cverify.cli")
            and scan.__name__ == "project_report"
        )
        baseline_cached = False
        cache_path = None
        baseline = None

        baseline_names = {renames.get(name, name) for name in names}
        validate_compilation_scope(databases[0], before, baseline_names)
        validate_compilation_scope(databases[1], after, names)
        if cache_allowed:
            try:
                compdb_src = locate_compdb(root, args.compdb, config["static"])
                key = _baseline_cache_key(baseline_args, toolchain, identity, baseline_names, before, after, databases[0], configs[0], digest, compdb_src)
                cache_dir = cache_directory() / "baseline-v1"
                cache_path = cache_dir / f"{key}.json"
                if cache_path.is_file():
                    baseline = json.loads(cache_path.read_text(encoding="utf-8"))
                    base_out = output / "baseline"
                    base_out.mkdir(parents=True, exist_ok=True)
                    base_report = base_out / "report.json"
                    baseline["project_root"] = str(before)
                    baseline["report_path"] = str(base_report)
                    base_report.write_text(dump_report_json(baseline), encoding="utf-8")
                    baseline_cached = True
            except (OSError, ValueError, TypeError, KeyError):
                baseline = None
                cache_path = None

        if baseline is None:
            files = sorted(before / name for name in baseline_names if (before / name).is_file())
            baseline = scan(baseline_args, before, toolchain, configs[0], files, databases[0], output / "baseline", sources)

        current_files = sorted(after / name for name in names if (after / name).is_file())
        current = scan(current_args, after, toolchain, configs[1], current_files, databases[1], output / "current", sources)

        if input_digest(digest) != digest:
            gaps.append("compilation database, response file or external dependency changed during verification")
        if any(file_digest(tree / name) != file_hash for label, tree in (("baseline", before), ("current", after)) for name, file_hash in manifests[label].items()):
            gaps.append("a verification command modified snapshot inputs")
        gaps.extend(verify_origin(root, identity, manifests["current"], args.git_staged, args.timeout))

        if cache_path is not None and not baseline_cached and not gaps and baseline["summary"]["complete"]:
            try:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                tmp = cache_path.with_suffix(f".tmp.{os.getpid()}")
                tmp.write_text(json.dumps(baseline, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
                os.replace(tmp, cache_path)
                _prune_cache(cache_path.parent)
            except OSError:
                pass

        gate = compare_reports(baseline, current, before, after, renames, gaps)
        if advisory:
            gate["advisory_non_source_changes"] = advisory
        if baseline_cached:
            gate["baseline_cached"] = True
        manifest_path = output / "snapshots.json"
        manifest_path.write_text(json.dumps(manifests, ensure_ascii=False, indent=2), encoding="utf-8")
        identity["source_manifest"] = str(manifest_path)
        identity["source_digest"] = hashlib.sha256(json.dumps(manifests, sort_keys=True).encode()).hexdigest()
        identity["compilation_digest"] = digest
        current.update(project_root=str(root), report_path=str(output / "report.json"), gate={**gate, **identity, "baseline_report": baseline["report_path"], "scope": sorted(names), "policy": "current rules on both snapshots"})
        return current
