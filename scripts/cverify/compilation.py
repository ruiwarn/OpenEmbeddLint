from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import shlex
from pathlib import Path
from typing import Sequence

from .execution import Toolchain, run_process


SOURCE_SUFFIXES = {".c", ".cc", ".cpp", ".cxx"}
HEADER_SUFFIXES = {".h", ".hh", ".hpp", ".hxx"}


def compiler_name_arguments(executable):
    name = re.sub(r"(?i)\.exe$", "", Path(executable.replace("\\", "/")).name)
    match = re.fullmatch(r"(.+)-(?:gcc|g\+\+|clang(?:\+\+)?|cc|c\+\+)(?:-\d[\d.]*)?", name)
    return ["--target=" + match[1]] if match else []


def split_compiler_command(command: str) -> list[str]:
    if os.name != "nt":
        return shlex.split(command)
    count = ctypes.c_int()
    parse = ctypes.windll.shell32.CommandLineToArgvW
    parse.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
    parse.restype = ctypes.POINTER(ctypes.c_wchar_p)
    arguments = parse("cverify-driver " + command, ctypes.byref(count))
    if not arguments:
        raise ValueError("cannot parse compiler command line")
    try:
        return [arguments[index] for index in range(1, count.value)]
    finally:
        release = ctypes.windll.kernel32.LocalFree
        release.argtypes = [ctypes.c_void_p]
        release(arguments)


def locate_compdb(root: Path, explicit: str | None, config: dict) -> Path | None:
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    elif config.get("compile_commands"):
        candidates.append(root / config["compile_commands"])
    else:
        candidates.extend((root / "compile_commands.json", root / "build" / "compile_commands.json"))
    for candidate in candidates:
        candidate = candidate.resolve()
        if candidate.is_dir():
            candidate = candidate / "compile_commands.json"
        if candidate.is_file():
            return candidate
    return None


def compdb_sources(compdb: Path) -> dict[Path, set[Path]]:
    try:
        entries = compilation_entries(compdb)
        sources: dict[Path, set[Path]] = {}
        for entry in entries:
            directory = Path(entry["directory"])
            source = Path(entry["file"])
            source = source if source.is_absolute() else directory / source
            sources.setdefault(source.resolve(), set()).add(source)
        return sources
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ValueError(f"invalid compilation database {compdb}: {error}") from error


def compilation_entries(compdb: Path) -> list[dict]:
    try:
        entries = json.loads(compdb.read_text(encoding="utf-8"))
        if not isinstance(entries, list):
            raise ValueError("expected an array of compiler entries")
        for entry in entries:
            if not isinstance(entry, dict) or not all(isinstance(entry.get(key), str) and entry[key] for key in ("directory", "file")):
                raise ValueError("each compiler entry requires file and directory strings")
            if not Path(entry["directory"]).is_absolute():
                raise ValueError("compilation directory must be absolute")
            arguments = entry.get("arguments")
            if arguments is not None:
                if not isinstance(arguments, list) or not arguments or not all(isinstance(value, str) for value in arguments):
                    raise ValueError("compiler arguments must be a nonempty string array")
            elif not isinstance(entry.get("command"), str) or not entry["command"].strip():
                raise ValueError("compiler entry requires arguments or command")
        return entries
    except (OSError, ValueError) as error:
        raise ValueError(f"invalid compilation database {compdb}: {error}") from error


def expand_response_arguments(arguments: Sequence[str], base_dir: Path, seen: frozenset[Path] = frozenset(), inputs=None) -> list[str]:
    """Expand compiler response files without executing their contents."""
    expanded = []
    for argument in arguments:
        if not argument.startswith("@") or len(argument) == 1:
            expanded.append(argument)
            continue
        response = Path(argument[1:].strip('"'))
        response = response if response.is_absolute() else base_dir / response
        response = response.resolve()
        if response in seen:
            raise ValueError(f"response file cycle detected: {response}")
        if len(seen) >= 16:
            raise ValueError("response file nesting exceeds 16 levels")
        try:
            if response.stat().st_size > 1_000_000:
                raise ValueError(f"response file exceeds 1 MB: {response}")
            raw = response.read_bytes()
            content = raw.decode("utf-8-sig")
            if inputs is not None:
                digest = hashlib.sha256(raw).hexdigest()
                if str(response) in inputs and inputs[str(response)] != digest:
                    raise ValueError("response file changed during verification: " + str(response))
                inputs[str(response)] = digest
        except OSError as error:
            raise ValueError(f"cannot read response file {response}: {error}") from error
        nested = split_compiler_command(content)
        expanded.extend(expand_response_arguments(nested, response.parent, seen | {response}, inputs))
    return expanded


def private_driver_compile_args(compdb: Path, source: Path, toolchain: Toolchain) -> list[str] | None:
    """Return target/ABI flags when this entry uses our driver; None for foreign drivers."""
    try:
        entries = compilation_entries(compdb)
        for entry in entries:
            directory = Path(entry["directory"])
            candidate = Path(entry["file"])
            candidate = candidate if candidate.is_absolute() else directory / candidate
            if candidate.resolve() != source.resolve():
                continue
            return private_driver_entry_args(entry, toolchain)
        return None
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ValueError(f"cannot parse compilation context for {source}: {error}") from error


def private_driver_entry_args(entry, toolchain):
    directory = Path(entry["directory"])
    arguments = entry.get("arguments") or split_compiler_command(entry["command"])
    if not (directory / arguments[0]).resolve().is_relative_to(toolchain.root.resolve()):
        return None
    arguments = expand_response_arguments(arguments, directory)
    forwarded = []
    separate = {"--target", "-target", "--sysroot", "-isysroot", "--gcc-toolchain", "-D", "-U"}
    prefixes = (
        "--target=", "-target=", "--sysroot=", "--gcc-toolchain=", "-mcpu=", "-march=",
        "-mabi=", "-mfloat-abi=", "-mfpu=", "-mthumb", "-marm", "-fshort-enums",
        "-fshort-wchar", "-stdlib=", "-std=", "-D", "-U",
    )
    index = 0
    while index < len(arguments):
        argument = arguments[index]
        if argument in separate and index + 1 < len(arguments):
            forwarded.extend((argument, arguments[index + 1]))
            index += 2
            continue
        if argument.startswith(prefixes):
            forwarded.append(argument)
        index += 1
    return forwarded


def private_driver_arguments(toolchain: Toolchain, cpp: bool, cwd: Path, compile_args: Sequence[str] = ()) -> tuple[list[str], dict]:
    """Discover target and system headers using the actual compilation target context."""
    compiler = toolchain.require("clang++" if cpp else "clang")
    process = run_process(
        [compiler, *compile_args, "-E", "-x", "c++" if cpp else "c", "-v", "-"],
        cwd=cwd, timeout_s=30,
    )
    output = process.stdout + process.stderr
    record = {"command": process.command, "exit_code": process.exit_code, "output": output}
    if process.exit_code != 0 or process.start_error or process.timed_out:
        raise ValueError(process.start_error or output[-1000:] or "private compiler query failed")
    target = re.search(r"(?m)^Target:\s*(.+?)\s*$", output)
    include_match = re.search(r"#include <\.\.\.> search starts here:\s*\n(.*?)\nEnd of search list\.", output, re.DOTALL)
    if not target or not include_match:
        raise ValueError("cannot determine target/system include paths from private compiler")
    includes = [line.strip() for line in include_match.group(1).splitlines() if line.strip()]
    arguments = [f"--target={target.group(1)}", *[f"-isystem{include}" for include in includes]]
    record["arguments"] = arguments
    return arguments, record


