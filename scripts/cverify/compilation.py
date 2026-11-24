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
_COMPILER_TRIPLE_RE = re.compile(r"(.+)-(?:gcc|g\+\+|clang(?:\+\+)?|cc|c\+\+)(?:-\d[\d.]*)?$")
_GNU_COMPILER_RE = re.compile(r"^(?:.+-)?(?:gcc|g\+\+|clang(?:\+\+)?|cc|c\+\+)(?:-\d[\d.]*)?$")
_KEEP_PAIRED_FLAGS = {
    "-I", "-isystem", "-iquote", "-idirafter", "-include", "-imacros",
    "-D", "-U", "-F", "-iframework", "--sysroot", "-isysroot",
    "--target", "-target", "-Xpreprocessor",
}
_STRIP_PAIRED_FLAGS = {"-o", "-MF", "-MT", "-MQ", "-MJ", "--param"}
_STRIP_SINGLE_FLAGS = {
    "-c", "-S", "-E", "-M", "-MM", "-MD", "-MMD", "-MP",
    "-Werror", "-Wall", "-Wextra", "-Wpedantic", "-pipe", "-save-temps",
}
_STRIP_PREFIXES = ("-o", "-MF", "-MT", "-MQ", "-MJ", "--param=", "-specs=", "--specs=", "-Wa,", "-Wl,", "-Werror=")
_STRIP_CODEGEN_PREFIXES = (
    "-flto", "-fno-lto", "-ffunction-sections", "-fdata-sections",
    "-fomit-frame-pointer", "-fno-omit-frame-pointer", "-fstack-protector",
    "-fstrict-aliasing", "-fno-strict-aliasing", "-falign-", "-finline-functions", "-fno-inline",
)
_OPT_FLAG_RE = re.compile(r"^-O(?:[0-3sgz]|fast)?$")
_DEBUG_PREFIXES = ("-gdwarf", "-ggdb", "-gstabs", "-g0", "-g1", "-g2", "-g3", "-gz")


def _compiler_stem(executable: str) -> str:
    return re.sub(r"(?i)\.exe$", "", Path(executable.replace("\\", "/")).name)


def long_path_name(executable: str) -> str:
    """Expand Windows 8.3 short paths without resolving POSIX symlinks."""
    if os.name != "nt" or not executable:
        return executable
    try:
        if not Path(executable).exists():
            return executable
    except OSError:
        return executable
    get_long = ctypes.windll.kernel32.GetLongPathNameW
    get_long.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint]
    get_long.restype = ctypes.c_uint
    size = 260
    while True:
        buffer = ctypes.create_unicode_buffer(size)
        copied = get_long(executable, buffer, size)
        if copied == 0:
            return executable
        if copied < size:
            return buffer.value
        size = copied + 1


def compiler_name_arguments(executable: str) -> list[str]:
    match = _COMPILER_TRIPLE_RE.fullmatch(_compiler_stem(executable))
    if not match and os.name == "nt":
        expanded = long_path_name(executable)
        if expanded != executable:
            match = _COMPILER_TRIPLE_RE.fullmatch(_compiler_stem(expanded))
    return ["--target=" + match[1]] if match else []


def is_gnu_like_compiler(executable: str) -> bool:
    if _GNU_COMPILER_RE.fullmatch(_compiler_stem(executable)):
        return True
    if os.name == "nt":
        expanded = long_path_name(executable)
        if expanded != executable:
            return bool(_GNU_COMPILER_RE.fullmatch(_compiler_stem(expanded)))
    return False


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


def find_compdb_entry(compdb: Path, source: Path) -> dict | None:
    try:
        entries = compilation_entries(compdb)
        resolved_source = source.resolve()
        for entry in entries:
            directory = Path(entry["directory"])
            candidate = Path(entry["file"])
            candidate = candidate if candidate.is_absolute() else directory / candidate
            if candidate.resolve() == resolved_source:
                return entry
        return None
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ValueError(f"cannot parse compilation context for {source}: {error}") from error


def target_driver_flags(arguments: Sequence[str], *, include_defines: bool = True) -> list[str]:
    forwarded = []
    separate = {"--target", "-target", "--sysroot", "-isysroot", "--gcc-toolchain"}
    prefixes = (
        "--target=", "-target=", "--sysroot=", "--gcc-toolchain=", "-mcpu=", "-march=",
        "-mabi=", "-mfloat-abi=", "-mfpu=", "-mthumb", "-marm", "-fshort-enums",
        "-fshort-wchar", "-stdlib=", "-std=", "--specs=",
    )
    if include_defines:
        separate = separate | {"-D", "-U"}
        prefixes = prefixes + ("-D", "-U")
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


def private_driver_compile_args(compdb: Path, source: Path, toolchain: Toolchain) -> list[str] | None:
    """Return target/ABI flags when this entry uses our driver; None for foreign drivers."""
    entry = find_compdb_entry(compdb, source)
    return None if entry is None else private_driver_entry_args(entry, toolchain)


def private_driver_entry_args(entry, toolchain):
    directory = Path(entry["directory"])
    arguments = entry.get("arguments") or split_compiler_command(entry["command"])
    if not (directory / arguments[0]).resolve().is_relative_to(toolchain.root.resolve()):
        return None
    arguments = expand_response_arguments(arguments, directory)
    return target_driver_flags(arguments, include_defines=True)


_TARGET_IDENTITY_MACRO_RE = re.compile(
    r"^(?:__GNUC__|__GNUC_MINOR__|__GNUC_PATCHLEVEL__"
    r"|__clang__|__clang_major__|__clang_minor__|__clang_patchlevel__"
    r"|__ARM_ARCH(?:_[A-Z0-9_]+)?|__ARM_EABI__|__ARM_FEATURE_[A-Z0-9_]+"
    r"|__thumb2?__|__SOFTFP__|__VFP_FP__|__aarch64__"
    r"|__riscv(?:_[a-z0-9_]+)?|__AVR(?:_[A-Z0-9_]+)?|__XTENSA__"
    r"|__BYTE_ORDER__|__ORDER_(?:LITTLE|BIG)_ENDIAN__)$"
)
_DEFINE_LINE_RE = re.compile(r"^#define\s+([A-Za-z_]\w*)(?:\s+(.*?))?\s*$")


def _is_gcc_internal_include(path_text: str) -> bool:
    norm = path_text.replace("\\", "/")
    return bool(re.search(r"/lib(?:64)?/gcc/", norm, re.IGNORECASE)) or Path(norm).name.lower() == "include-fixed"


def _extract_identity_defines(stdout: str) -> list[str]:
    found: dict[str, str] = {}
    for line in stdout.splitlines():
        match = _DEFINE_LINE_RE.match(line)
        if not match or not _TARGET_IDENTITY_MACRO_RE.fullmatch(match[1]):
            continue
        val = (match[2] or "").strip()
        if any(ch in val for ch in (" ", "\t", "(", ")")):
            continue
        found[match[1]] = val
    return [f"{name}={val}" if val else name for name, val in sorted(found.items())]


def query_driver_arguments(compiler: Path, cpp: bool, cwd: Path, compile_args: Sequence[str] = ()) -> tuple[list[str], dict]:
    """Discover target, system headers, and compiler-identity defines from a GCC/Clang driver."""
    query_cwd = cwd if cwd.is_dir() else Path.cwd()
    process = run_process(
        [compiler, *compile_args, "-E", "-x", "c++" if cpp else "c", "-dM", "-v", "-"],
        cwd=query_cwd, timeout_s=30,
    )
    output = process.stdout + process.stderr
    if process.exit_code != 0 or process.start_error or process.timed_out:
        raise ValueError(process.start_error or output[-1000:] or "compiler query failed")
    target = re.search(r"(?m)^Target:\s*(.+?)\s*$", output)
    include_match = re.search(r"#include <\.\.\.> search starts here:\s*\n(.*?)\nEnd of search list\.", output, re.DOTALL)
    if not target or not include_match:
        raise ValueError("cannot determine target/system include paths from compiler")
    is_clang = "clang" in _compiler_stem(str(compiler)).lower()
    arguments = [f"--target={target.group(1)}"]
    for raw_line in include_match.group(1).splitlines():
        item = raw_line.strip()
        if not item:
            continue
        is_framework = item.endswith("(framework directory)")
        if is_framework:
            item = item[:-len("(framework directory)")].strip()
        try:
            resolved = Path(item).resolve()
            text = resolved.as_posix() if resolved.is_dir() else item
        except OSError:
            text = item
        flag = "-iframework" if is_framework else ("-idirafter" if not is_clang and _is_gcc_internal_include(text) else "-isystem")
        arguments.append(flag + text)
    defines = _extract_identity_defines(process.stdout)
    record = {"command": process.command, "exit_code": process.exit_code, "arguments": arguments, "defines": defines}
    return arguments, record


def private_driver_arguments(toolchain: Toolchain, cpp: bool, cwd: Path, compile_args: Sequence[str] = ()) -> tuple[list[str], dict]:
    """Discover target and system headers using the actual compilation target context."""
    compiler = toolchain.require("clang++" if cpp else "clang")
    return query_driver_arguments(compiler, cpp, cwd, compile_args)


def resolve_entry_driver_arguments(
    entry: dict, toolchain: Toolchain, cpp: bool, cwd: Path, cache: dict | None = None,
) -> tuple[list[str], dict | None]:
    """Resolve target/system-header args for private or explicit external GCC/Clang drivers."""
    directory = Path(entry["directory"])
    arguments = entry.get("arguments") or split_compiler_command(entry["command"])
    compiler_raw = arguments[0]
    candidate = Path(compiler_raw)
    if not candidate.is_absolute():
        candidate = directory / candidate
    if candidate.resolve().is_relative_to(toolchain.root.resolve()):
        forwarded = target_driver_flags(expand_response_arguments(arguments, directory), include_defines=True)
        key = ("private", cpp, tuple(forwarded))
        if cache is not None and key in cache:
            return cache[key]
        result = private_driver_arguments(toolchain, cpp, cwd, forwarded)
        if cache is not None:
            cache[key] = result
        return result
    if candidate.is_file() and is_gnu_like_compiler(str(candidate)):
        compiler_path = Path(os.path.abspath(long_path_name(str(candidate))))
        forwarded = target_driver_flags(expand_response_arguments(arguments, directory), include_defines=False)
        key = ("external", str(compiler_path), cpp, tuple(forwarded))
        if cache is not None and key in cache:
            return cache[key]
        try:
            result = query_driver_arguments(compiler_path, cpp, cwd, forwarded)
        except (ValueError, OSError) as error:
            fallback = compiler_name_arguments(str(compiler_path))
            result = (fallback, {"command": [str(compiler_path), *forwarded], "exit_code": None, "fallback": True, "error": str(error), "arguments": fallback, "defines": []})
        if cache is not None:
            cache[key] = result
        return result
    return compiler_name_arguments(compiler_raw), None


def compdb_system_includes(compdb: Path | None, driver_args: Sequence[str] = ()) -> set[Path]:
    """Collect existing system include directories declared in compdb entries or driver args."""
    includes: set[Path] = set()

    def scan_args(args: Sequence[str], base_dir: Path) -> None:
        flags = ("-isystem", "-iframework", "-idirafter")
        index = 0
        while index < len(args):
            value = args[index]
            raw_path = None
            if value in flags and index + 1 < len(args):
                raw_path = args[index + 1]
                index += 2
            else:
                for prefix in flags:
                    if value.startswith(prefix) and len(value) > len(prefix):
                        raw_path = value[len(prefix) + (1 if value[len(prefix)] == "=" else 0):]
                        break
                index += 1
            if raw_path:
                try:
                    path = Path(raw_path)
                    resolved = (path if path.is_absolute() else base_dir / path).resolve()
                    if resolved.is_dir():
                        includes.add(resolved)
                except OSError:
                    pass

    if driver_args:
        scan_args(driver_args, Path.cwd())
    if compdb is not None:
        try:
            for entry in compilation_entries(compdb):
                directory = Path(entry["directory"])
                args = entry.get("arguments") or split_compiler_command(entry["command"])
                scan_args(args, directory)
        except ValueError:
            pass
    return includes


def sanitize_dependency_arguments(arguments: Sequence[str]) -> list[str]:
    """Keep target, macro and include flags for clang -M; strip warnings and codegen flags."""
    kept: list[str] = []
    index = 0
    while index < len(arguments):
        value = arguments[index]
        if value in _KEEP_PAIRED_FLAGS and index + 1 < len(arguments):
            kept.extend((value, arguments[index + 1]))
            index += 2
            continue
        if value in _STRIP_PAIRED_FLAGS:
            index += 2
            continue
        if value in _STRIP_SINGLE_FLAGS or value.startswith(_STRIP_PREFIXES) or value.startswith(_STRIP_CODEGEN_PREFIXES):
            index += 1
            continue
        if value == "-W" or (value.startswith("-W") and not value.startswith("-Wp,")):
            index += 1
            continue
        if _OPT_FLAG_RE.fullmatch(value) or value == "-g" or value.startswith(_DEBUG_PREFIXES):
            index += 1
            continue
        kept.append(value)
        index += 1
    return kept


