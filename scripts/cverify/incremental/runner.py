import copy
import hashlib
import json
import re
import tempfile
from pathlib import Path

from ..installation import misra_rule_texts
from ..compilation import HEADER_SUFFIXES, SOURCE_SUFFIXES, locate_compdb
from .attribution import compare_reports
from .context import including_units, input_digest, remap_config, remap_database, validate_compilation_scope
from .snapshots import POLICY_FILES, capture, file_digest, git, safe_path, synchronize_policy


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


def context_gaps(changed, before, after):
    gaps = []
    for name in sorted(changed):
        path = Path(name)
        if path.suffix.lower() in SOURCE_SUFFIXES | HEADER_SUFFIXES | {".md", ".rst"} or path.name in POLICY_FILES:
            continue
        if path.name == "compile_commands.json":
            gaps.append("tracked compilation database changed; verify each revision with its own build context")
        elif file_digest(before / name) != file_digest(after / name):
            gaps.append("changed non-source input requires separate verification: " + name)
    return gaps


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


def expand_headers(names, origin, before, after, databases, configs, toolchain, output, timeout, inputs):
    gaps, external = [], set()
    for index, tree in enumerate((before, after)):
        headers = {(tree / name).resolve() for name in names if Path(name).suffix.lower() in HEADER_SUFFIXES and (tree / name).is_file()}
        units, missing, dependencies = including_units(tree, headers, databases[index], configs[index], toolchain, output / ("baseline" if index == 0 else "current"), timeout, None if headers else names)
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
    gaps = context_gaps(changed, before, after)
    if args.git_staged:
        gaps.extend(context_gaps(snapshot_manifest(after), after, root))
    inheritance = r'(?i)["\x27]?InheritParentConfig["\x27]?\s*:\s*(true|yes|1)\b'
    if any(re.search(inheritance, path.read_text(encoding="utf-8")) for path in after.rglob(".clang-tidy")):
        if any((parent / ".clang-tidy").is_file() for tree in (root, after) for parent in tree.parents):
            gaps.append("clang-tidy inheritance outside the repository is not frozen; use self-contained project policies")
    names = selected_names(args, root, after, changed, config)
    if not names and not args.allow_empty:
        raise ValueError("no C/C++ files selected; use --allow-empty only for a legitimate empty scope")
    synchronize_policy(before, after)
    return before, after, names, renames, identity, config, sources, gaps


def verify_origin(root, identity, manifest, staged, timeout):
    gaps = []
    current_index = hashlib.sha256(git(root, "ls-files", "--stage", "-z", timeout=timeout)).hexdigest()
    if current_index != identity["index_digest"] or git(root, "rev-parse", "HEAD", timeout=timeout).decode().strip() != identity["current_commit"]:
        gaps.append("repository index or HEAD changed during verification")
    if not staged and any(file_digest(root / name) != digest for name, digest in manifest.items()):
        gaps.append("worktree inputs changed after the verification snapshot was captured")
    return gaps


def run_incremental(args, root, toolchain, output, load_config, scan):
    if args.profile == "fuzz":
        raise ValueError("incremental gates apply to static analysis; run fuzz verification separately")
    if output.is_relative_to(root):
        raise ValueError("incremental artifacts must be outside the source repository")
    with tempfile.TemporaryDirectory(prefix="snapshots-", dir=output) as temporary:
        before, after, names, renames, identity, config, sources, gaps = snapshot_scope(args, root, Path(temporary), load_config)
        databases, configs, digest = prepare_context(args, root, before, after, config, output, renames)
        if args.misra_c2012 or config["static"].get("misra_c2012"):
            digest.update(input_digest([misra_rule_texts()]))
        digest.update(input_digest(Path(source) for source in sources))
        gaps.extend(expand_headers(names, root, before, after, databases, configs, toolchain, output, args.timeout, digest))
        manifests = {"baseline": snapshot_manifest(before), "current": snapshot_manifest(after)}
        current_args = copy.copy(args)
        current_args.header_filter = args.header_filter if args.header_filter is not None else ".*"
        baseline_args = copy.copy(current_args)
        baseline_args.profile = "quick"
        configs[0]["commands"] = []
        documents = []
        for index, tree in enumerate((before, after)):
            side_names = {renames.get(name, name) if index == 0 else name for name in names}
            validate_compilation_scope(databases[index], tree, side_names)
            files = sorted(tree / name for name in side_names if (tree / name).is_file())
            document = scan(baseline_args if index == 0 else current_args, tree, toolchain, configs[index], files, databases[index], output / ("baseline" if index == 0 else "current"), sources)
            documents.append(document)
        if input_digest(digest) != digest:
            gaps.append("compilation database, response file or external dependency changed during verification")
        if any(file_digest(tree / name) != digest for label, tree in (("baseline", before), ("current", after)) for name, digest in manifests[label].items()):
            gaps.append("a verification command modified snapshot inputs")
        gaps.extend(verify_origin(root, identity, manifests["current"], args.git_staged, args.timeout))
        baseline, current = documents
        gate = compare_reports(baseline, current, before, after, renames, gaps)
        manifest_path = output / "snapshots.json"
        manifest_path.write_text(json.dumps(manifests, ensure_ascii=False, indent=2), encoding="utf-8")
        identity["source_manifest"] = str(manifest_path)
        identity["source_digest"] = hashlib.sha256(json.dumps(manifests, sort_keys=True).encode()).hexdigest()
        identity["compilation_digest"] = digest
        current.update(project_root=str(root), report_path=str(output / "report.json"), gate={**gate, **identity, "baseline_report": baseline["report_path"], "scope": sorted(names), "policy": "current rules on both snapshots"})
        return current
