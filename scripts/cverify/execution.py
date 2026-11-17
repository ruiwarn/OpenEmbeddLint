"""Private tool lookup and process execution. No analyzer is resolved through PATH."""

from __future__ import annotations

import os
import platform
import signal
import stat
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from .installation import tool_directory


PASS = "PASS"
FAIL = "FAIL"
ERROR = "ERROR"
SKIP = "SKIP"


@dataclass
class Issue:
    message: str
    rule: str
    severity: str = "warning"
    file: str | None = None
    line: int | None = None
    column: int | None = None
    dimension: str = "correctness"
    notes: list[dict] = field(default_factory=list)


@dataclass
class CheckResult:
    id: str
    dimension: str
    status: str
    required: bool = True
    tool: str | None = None
    command: list[str] = field(default_factory=list)
    exit_code: int | None = None
    duration_s: float = 0.0
    files: list[str] = field(default_factory=list)
    issues: list[Issue] = field(default_factory=list)
    reason: str | None = None
    log: str | None = None
    metrics: dict = field(default_factory=dict)
    gaps: list[str] = field(default_factory=list)
    policy: str = "blocking"

    def to_dict(self) -> dict:
        result = asdict(self)
        result["duration_s"] = round(self.duration_s, 3)
        return result


@dataclass
class ProcessResult:
    command: list[str]
    exit_code: int | None
    stdout: str
    stderr: str
    duration_s: float
    log: str | None = None
    timed_out: bool = False
    start_error: str | None = None


class ToolUnavailable(RuntimeError):
    pass


def platform_key(system: str | None = None, machine: str | None = None) -> str:
    system = (system or platform.system()).lower()
    machine = (machine or platform.machine()).lower()
    architectures = {
        "amd64": "x86_64",
        "x86_64": "x86_64",
        "x64": "x86_64",
        "arm64": "arm64",
        "aarch64": "arm64",
    }
    architecture = architectures.get(machine)
    systems = {"windows": "windows", "linux": "linux", "darwin": "macos"}
    if system not in systems or architecture is None:
        raise ToolUnavailable(f"unsupported platform: {system}/{machine}")
    return f"{systems[system]}-{architecture}"


class Toolchain:
    """Resolve executables only inside this skill's platform-specific directory."""

    def __init__(self, skill_dir: Path, target: str | None = None):
        self.skill_dir = skill_dir.resolve()
        self.platform = target or platform_key()
        self.root = tool_directory(self.skill_dir, self.platform).resolve()
        self.is_windows = self.platform.startswith("windows-")

    def candidates(self, name: str) -> Iterable[Path]:
        filename = f"{name}.exe" if self.is_windows else name
        if self.is_windows and name in ("clang", "clang++"):
            yield self.root / "host" / "bin" / filename
        directories = ("Library/bin", "Scripts", "bin") if self.is_windows else ("bin",)
        for directory in directories:
            yield self.root / directory / filename

    def find(self, name: str) -> Path | None:
        for candidate in self.candidates(name):
            if not candidate.is_file():
                continue
            resolved = candidate.resolve()
            if not resolved.is_relative_to(self.root):
                raise ToolUnavailable(f"private tool escapes its platform directory: {candidate}")
            if not self.is_windows and not os.access(resolved, os.X_OK):
                try:
                    # Zip installers do not all restore the executable bit stored in the archive.
                    resolved.chmod(resolved.stat().st_mode | stat.S_IXUSR)
                except OSError as error:
                    raise ToolUnavailable(f"cannot restore executable bit for private tool: {resolved}: {error}") from error
            if not self.is_windows and not os.access(resolved, os.X_OK):
                raise ToolUnavailable(f"private tool is not executable: {resolved}")
            return resolved
        return None

    def require(self, name: str) -> Path:
        executable = self.find(name)
        if executable is None:
            expected = ", ".join(str(path) for path in self.candidates(name))
            raise ToolUnavailable(f"private tool '{name}' missing for {self.platform}; expected: {expected}; setup: https://github.com/ruiwarn/OpenEmbeddLint#tool-downloads")
        return executable

    def environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        binary_dirs = [self.root / "bin"]
        if self.is_windows:
            binary_dirs.insert(0, self.root / "Library/bin")
            binary_dirs.insert(0, self.root / "host" / "bin")
        private_path = os.pathsep.join(str(path) for path in binary_dirs if path.is_dir())
        if private_path:
            # DLLs and helper tools may use PATH; every built-in entry point remains absolute.
            environment["PATH"] = private_path + os.pathsep + environment.get("PATH", "")
        library_path = self.root / "lib"
        if library_path.is_dir() and not self.is_windows:
            variable = "DYLD_LIBRARY_PATH" if self.platform.startswith("macos-") else "LD_LIBRARY_PATH"
            environment[variable] = str(library_path) + os.pathsep + environment.get(variable, "")
        symbolizer = self.find("llvm-symbolizer")
        if symbolizer is not None:
            environment["LLVM_SYMBOLIZER_PATH"] = str(symbolizer)
        return environment


def _kill_process_tree(process: subprocess.Popen) -> None:
    if os.name == "nt":
        system_root = Path(os.environ.get("SystemRoot", r"C:\Windows"))
        taskkill = system_root / "System32" / "taskkill.exe"
        if taskkill.is_file():
            subprocess.run(
                [str(taskkill), "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=10,
            )
            return
    elif process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError):
            pass
    if process.poll() is None:
        process.kill()


def run_process(
    argv: Sequence[str | Path],
    *,
    cwd: Path,
    timeout_s: float,
    log_path: Path | None = None,
    extra_env: Mapping[str, str] | None = None,
) -> ProcessResult:
    """Run an argv without a shell and preserve output, exit status and timeout."""

    command = [str(part) for part in argv]
    if not command or not Path(command[0]).is_absolute():
        raise ValueError(f"executable must be an absolute path: {command[:1]}")
    environment = os.environ.copy()
    if extra_env:
        environment.update(extra_env)
    environment["PYTHONIOENCODING"] = "utf-8"
    started = time.monotonic()
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    try:
        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            creationflags=creationflags,
            start_new_session=os.name != "nt",
        )
    except (OSError, ValueError) as error:
        return ProcessResult(command, None, "", "", time.monotonic() - started, start_error=str(error))

    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired as error:
        timed_out = True
        _kill_process_tree(process)
        stdout, stderr = process.communicate()
        if isinstance(error.stdout, str):
            stdout = error.stdout + (stdout or "")
        if isinstance(error.stderr, str):
            stderr = error.stderr + (stderr or "")
    duration = time.monotonic() - started
    log = None
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            "$ " + subprocess.list2cmdline(command) + "\n\n[stdout]\n" + stdout + "\n[stderr]\n" + stderr,
            encoding="utf-8",
        )
        log = str(log_path)
    return ProcessResult(command, process.returncode, stdout, stderr, duration, log, timed_out)
