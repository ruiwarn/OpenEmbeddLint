import copy
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import json
import shlex
from pathlib import Path

from ..compilation import SOURCE_SUFFIXES, compilation_entries, expand_response_arguments, resolve_entry_driver_arguments, sanitize_dependency_arguments, split_compiler_command
from ..execution import run_process
from .snapshots import file_digest


PATH_OPTIONS = {"-I", "-isystem", "-iquote", "-idirafter", "-include", "-imacros", "-isysroot", "--sysroot", "-F", "-iframework", "-resource-dir", "--gcc-toolchain", "--gcc-install-dir", "-B"}
PATH_PREFIXES = ("--gcc-install-dir=", "--gcc-toolchain=", "--sysroot=", "-fmodule-map-file=", "-resource-dir=", "-isystem=", "-isystem", "-iquote", "-idirafter", "-isysroot", "-include", "-imacros", "-I", "-F", "-B")


def source_directories(root):
    directories = {Path(".")}
    files = set()
    for path in root.rglob("*"):
        if path.is_file():
            relative = path.relative_to(root)
            files.add(relative)
            directories.update(relative.parents)
    return files, directories


def mapped_path(value, directory, origin, snapshot, files, directories, force=False):
    path = Path(value)
    path = (directory / path).resolve() if not path.is_absolute() else path.resolve()
    if path.is_relative_to(origin):
        relative = path.relative_to(origin)
        if force or relative in files or relative in directories:
            return (snapshot / relative).as_posix()
    return path.as_posix()


def mapped_arguments(arguments, directory, origin, snapshot, files, directories, source=None):
    result, index = [], 0
    while index < len(arguments):
        value = arguments[index]
        if value.startswith(("-include-pch", "-include-pth", "-fmodule", "-ivfsoverlay", "--config", "-working-directory")):
            raise ValueError("incremental comparison cannot remap cached or overlaid compiler inputs: " + value)
        if value in PATH_OPTIONS:
            if index + 1 == len(arguments):
                raise ValueError("missing path after " + value)
            result.extend([value, mapped_path(arguments[index + 1], directory, origin, snapshot, files, directories)])
            index += 2
            continue
        prefix = next((prefix for prefix in PATH_PREFIXES if value.startswith(prefix) and value != prefix), None)
        if prefix:
            result.append(prefix + mapped_path(value[len(prefix):], directory, origin, snapshot, files, directories))
        elif source and not value.startswith("-") and (directory / value).resolve() == source:
            result.append(mapped_path(value, directory, origin, snapshot, files, directories, True))
        else:
            result.append(value)
        index += 1
    return result


def remap_database(database, origin, snapshot, destination, current_tree, renames=None, inputs=None):
    origin = origin.resolve()
    entries = compilation_entries(database)
    files, directories = source_directories(current_tree)
    mapped, contexts = [], set()
    for entry in entries:
        directory = Path(entry["directory"])
        source = (directory / entry["file"]).resolve()
        if not source.is_relative_to(origin):
            continue
        relative = source.relative_to(origin)
        relative = Path((renames or {}).get(relative.as_posix(), relative.as_posix()))
        if not (snapshot / relative).is_file():
            continue
        arguments = entry.get("arguments") or split_compiler_command(entry["command"])
        arguments = expand_response_arguments(arguments, directory, inputs=inputs)
        argv = [arguments[0], *mapped_arguments(arguments[1:], directory, origin, snapshot, files, directories, source)]
        current_source = (snapshot / source.relative_to(origin)).as_posix()
        argv = [(snapshot / relative).as_posix() if value == current_source else value for value in argv]
        key = (relative, argv[0], tuple(dependency_arguments(argv)))
        if key in contexts:
            continue
        contexts.add(key)
        cwd = mapped_path(str(directory), directory, origin, snapshot, files, directories)
        mapped.append({"directory": cwd, "file": (snapshot / relative).as_posix(), "arguments": argv})
    destination.write_text(json.dumps(mapped), encoding="utf-8")
    return destination


def validate_compilation_scope(database, root, names):
    counts = Counter(Path(entry["file"]).relative_to(root).as_posix() for entry in compilation_entries(database))
    ambiguous = sorted(name for name in names if counts[name] > 1)
    if ambiguous:
        raise ValueError("multiple compilation configurations for selected sources; verify each configuration separately: " + ", ".join(ambiguous) + "; select a target-specific database with --compdb; keep temporary copies outside the source repository and preserve distinct target configurations")


def remap_config(config, origin, snapshot, current_tree):
    result = copy.deepcopy(config)
    files, directories = source_directories(current_tree)
    result["static"]["extra_args"] = mapped_arguments(config["static"]["extra_args"], origin, origin, snapshot, files, directories)
    result["static"]["cppcheck_includes"] = [mapped_path(value, origin, origin, snapshot, files, directories) for value in config["static"]["cppcheck_includes"]]
    return result


def dependency_arguments(arguments):
    filtered, index = [], 1
    pairs = {"-o", "-MF", "-MT", "-MQ", "-MJ"}
    flags = {"-c", "-S", "-E", "-M", "-MM", "-MD", "-MMD", "-MP"}
    while index < len(arguments):
        value = arguments[index]
        if value in pairs:
            index += 2
            continue
        if value not in flags and not any(value.startswith(prefix) for prefix in ("-o", "-MF", "-MT", "-MQ", "-MJ")):
            filtered.append(value)
        index += 1
    return filtered


def including_units(root, headers, database, config, toolchain, output, timeout, units=None, jobs=1, driver_cache=None):
    selected, covered, gaps, queries, external = set(), set(), [], {} if driver_cache is None else driver_cache, set()
    entries = compilation_entries(database)
    environment = toolchain.environment()
    tasks = []
    for index, entry in enumerate(entries):
        source = Path(entry["file"])
        if source.suffix.lower() not in SOURCE_SUFFIXES:
            continue
        source_rel = source.relative_to(root).as_posix()
        if units is not None and source_rel not in units:
            continue
        is_cpp = source.suffix.lower() != ".c"
        compiler = toolchain.require("clang++" if is_cpp else "clang")
        entry_dir = Path(entry["directory"])
        driver, _ = resolve_entry_driver_arguments(entry, toolchain, is_cpp, entry_dir, cache=queries)
        dep_flags = sanitize_dependency_arguments(entry["arguments"][1:])
        command = [compiler, *driver, *dep_flags, *config["static"]["extra_args"], "-Wno-unused-command-line-argument", "-M", "-MG", "-MT", "cverify", "-MF", "-"]
        tasks.append((index, source_rel, entry_dir, command))

    def _run_dep(item):
        index, source_rel, entry_dir, command = item
        return index, source_rel, entry_dir, run_process(command, cwd=entry_dir, timeout_s=timeout, log_path=output / "dependencies" / f"{index}.log", extra_env=environment)

    if jobs > 1 and len(tasks) > 1:
        with ThreadPoolExecutor(max_workers=min(jobs, len(tasks))) as pool:
            results = list(pool.map(_run_dep, tasks))
    else:
        results = [_run_dep(item) for item in tasks]

    for _, source_rel, entry_dir, process in results:
        if process.exit_code != 0 or process.start_error or process.timed_out or ":" not in process.stdout:
            gaps.append("dependency discovery failed: " + source_rel)
            continue
        names = shlex.split(process.stdout.split(":", 1)[1].replace("\\\n", " ").replace("\\\r\n", " "))
        dependencies = {(entry_dir / name.replace("$$", "$")).resolve() for name in names}
        external.update(path for path in dependencies if not path.is_relative_to(root))
        matches = dependencies & headers
        if matches:
            selected.add(source_rel)
            covered.update(matches)
    gaps.extend("no including translation unit found: " + path.relative_to(root).as_posix() for path in headers - covered)
    return selected, gaps, external


def input_digest(paths):
    return {str(path): file_digest(Path(path)) for path in paths}
