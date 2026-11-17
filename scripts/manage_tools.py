#!/usr/bin/env python3
"""Maintainer utility for reproducible private tool directories; normal checks never use it."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import struct
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cverify.cli import TOOL_NAMES
from cverify.execution import Toolchain, platform_key
from cverify.installation import tool_directory, user_data_directory


SKILL_DIR = Path(__file__).resolve().parents[1]
TOOLS = SKILL_DIR / "tools" if (SKILL_DIR / "pyproject.toml").is_file() else user_data_directory() / "tools"
SPECS = ["cppcheck=2.22.0", "clang-tools=23.1.4", "clang=23.1.4", "clangxx=23.1.4", "compiler-rt=23.1.4", "llvm-tools=23.1.4", "cmake=4.4.5", "ninja=1.13.2"]
TARGETS = {
    "windows-x86_64": "win-64",
    "linux-x86_64": "linux-64",
    "linux-arm64": "linux-aarch64",
    "macos-x86_64": "osx-64",
    "macos-arm64": "osx-arm64",
}
MICROMAMBA_SHA256 = {
    "windows-x86_64": "f8b4284bd4eb1134122eaf93b3ea6c7be567e596de5829e1a6f478e34e41974a",
    "linux-x86_64": "701f39c06f8e6a670a0e186c8479b9e5c0502a2f0cda6cef65d83bdd2875e8bd",
    "linux-arm64": "3bb56786e21b9b24024f599fe9185b4a59dc3f9f59937a42311ee583adbbb23c",
    "macos-x86_64": "8421462a1ef1df9f7acd114f9e2a24c1332ce4f4e29f44465b4acb44e2755172",
    "macos-arm64": "c547639f5caf742236641c362746abb4a3505774753ecd08fe341643670c4c01",
}
KEEP_BINARIES = set(TOOL_NAMES) | {
    "ctest", "cpack", "lld", "ld.lld", "lld-link", "ld64.lld", "wasm-ld",
    "llvm-ar", "llvm-ranlib", "llvm-strip", "llvm-objcopy", "llvm-objdump",
    "llvm-readobj", "llvm-nm", "llvm-dlltool", "llvm-lib", "llvm-mt", "llvm-rc", "llvm-windres",
}


def platform_directory(target):
    return tool_directory(SKILL_DIR, target).resolve() if os.environ.get("OPENEMBEDDLINT_TOOLS_DIR") else TOOLS / "platforms" / target


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def micromamba(target: str) -> Path:
    directory = TOOLS / "bootstrap" / f"micromamba-{TARGETS[target]}"
    name = "micromamba.exe" if target.startswith("windows-") else "micromamba"
    return directory / name


def bootstrap(targets: list[str]) -> None:
    for target in targets:
        destination = micromamba(target)
        if destination.exists():
            print(f"exists: {destination}")
            continue
        destination.parent.mkdir(parents=True)
        asset = "micromamba-win-64.exe" if target == "windows-x86_64" else f"micromamba-{TARGETS[target]}"
        url = f"https://github.com/mamba-org/micromamba-releases/releases/download/2.10.0-0/{asset}"
        temporary = destination.with_suffix(destination.suffix + ".download")
        urllib.request.urlretrieve(url, temporary)
        actual = sha256(temporary)
        if actual != MICROMAMBA_SHA256[target]:
            raise RuntimeError(f"micromamba checksum mismatch for {target}: {actual}")
        temporary.replace(destination)
        if not target.startswith("windows-"):
            destination.chmod(0o755)
        print(f"verified: {destination}")


def wsl_path(path: Path) -> str:
    text = str(path.resolve())
    drive = text[0].lower()
    return f"/mnt/{drive}/{text[2:].replace(chr(92), '/').lstrip('/')}"


def relocate(target: str) -> None:
    root = platform_directory(target)
    bin_dir = root / ("Library/bin" if target.startswith("windows-") else "bin")
    share = root / "share" / "Cppcheck"
    if not share.is_dir():
        share = root / "share" / "cppcheck"
    for name in ("cfg", "addons", "platforms"):
        source = share / name
        destination = bin_dir / name
        if source.is_dir() and not destination.exists():
            shutil.copytree(source, destination)
    (root / ".cverify-toolchain").write_text(json.dumps({"schema_version": 1, "target": target}), encoding="utf-8")
    print(f"relocated Cppcheck data: {target}")


def create(targets: list[str]) -> None:
    host = platform_key()
    for target in targets:
        prefix = platform_directory(target)
        if prefix.exists():
            raise RuntimeError(f"refusing to overwrite existing prefix: {prefix}")
        bootstrap_target = "linux-x86_64" if host.startswith("windows-") and not target.startswith("windows-") else host
        executable = micromamba(bootstrap_target)
        if not executable.is_file():
            raise RuntimeError(f"run bootstrap for {bootstrap_target} first: {executable}")
        environment = os.environ.copy()
        if target.startswith("linux-"):
            environment.update({"CONDA_OVERRIDE_GLIBC": "2.17", "CONDA_OVERRIDE_LINUX": "5.10"})
        if target.startswith("macos-"):
            environment["CONDA_OVERRIDE_OSX"] = "11.0"
        root_prefix = TOOLS / "bootstrap" / ("root" if target == host else "root-unix")
        manifest = json.loads((SKILL_DIR / "tools/manifest.json").read_text(encoding="utf-8"))
        packages = [package for package in manifest["platforms"][target]["packages"] if package["name"] != "llvm-mingw"]
        explicit = TOOLS / "bootstrap" / (target + ".explicit.txt")
        explicit.write_text("@EXPLICIT\n" + "".join(package["url"] + "#" + package["sha256"] + "\n" for package in packages), encoding="utf-8")
        command = [str(executable), "create", "-y", "--no-rc", "--always-copy", "--skip-run-link-scripts", "--no-pyc", "--root-prefix", str(root_prefix), "--prefix", str(prefix), "--platform", TARGETS[target], "--file", str(explicit)]
        if host.startswith("windows-") and not target.startswith("windows-"):
            overrides = [f"{key}={value}" for key, value in environment.items() if key.startswith("CONDA_OVERRIDE_")]
            command = ["C:/Windows/System32/wsl.exe", "--", "env", *overrides, wsl_path(executable), *command[1:]]
            for index in (command.index("--root-prefix") + 1, command.index("--prefix") + 1):
                command[index] = wsl_path(Path(command[index]))
            command[command.index("--file") + 1] = wsl_path(explicit)
            environment["MSYS_NO_PATHCONV"] = "1"
        subprocess.run(command, env=environment, check=True)
        relocate(target)


def install(targets):
    if targets != [platform_key()]:
        raise RuntimeError("install requires exactly the native platform; use create for cross-platform maintenance")
    target = targets[0]
    prefix = platform_directory(target)
    if not prefix.exists():
        bootstrap(targets)
        create(targets)
    if target == "windows-x86_64" and not (prefix / "host").exists():
        manifest = json.loads((SKILL_DIR / "tools/manifest.json").read_text(encoding="utf-8"))
        package = next(package for package in manifest["platforms"][target]["packages"] if package["name"] == "llvm-mingw")
        archive = TOOLS / "bootstrap" / Path(package["url"]).name
        archive.parent.mkdir(parents=True, exist_ok=True)
        if not archive.is_file():
            urllib.request.urlretrieve(package["url"], archive)
        if sha256(archive) != package["sha256"]:
            raise RuntimeError("llvm-mingw checksum mismatch: " + str(archive))
        with tempfile.TemporaryDirectory(dir=archive.parent) as temporary:
            directory = Path(temporary).resolve()
            with zipfile.ZipFile(archive) as bundle:
                if any(not (directory / name).resolve().is_relative_to(directory) for name in bundle.namelist()):
                    raise RuntimeError("llvm-mingw archive contains an escaping path")
                bundle.extractall(directory)
            roots = list(directory.iterdir())
            if len(roots) != 1 or not (roots[0] / "bin/clang.exe").is_file():
                raise RuntimeError("llvm-mingw archive has an unexpected layout")
            shutil.move(str(roots[0]), prefix / "host")
    if target.startswith("macos-"):
        for directory in (prefix / "bin", prefix / "lib"):
            for path in directory.rglob("*"):
                if not path.is_file() or path.is_symlink():
                    continue
                with path.open("rb") as stream:
                    header = stream.read(16)
                if len(header) < 16 or header[:4] != b"\xcf\xfa\xed\xfe" or struct.unpack_from("<I", header, 12)[0] not in (2, 6, 8):
                    continue
                process = subprocess.run(["/usr/bin/codesign", "--verify", "--strict", str(path)], capture_output=True)
                if process.returncode:
                    subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", str(path)], check=True)
    verify(targets, False)


def restore(targets: list[str]) -> None:
    """Restore pruned bin files from their verified package extraction directories."""
    for target in targets:
        if platform_key().startswith("windows-") and not target.startswith("windows-"):
            environment = os.environ.copy()
            environment["MSYS_NO_PATHCONV"] = "1"
            subprocess.run(["C:/Windows/System32/wsl.exe", "--", "python3", wsl_path(Path(__file__)), "restore", "--targets", target], env=environment, check=True)
            continue
        root = platform_directory(target)
        if not (root / ".cverify-toolchain").is_file():
            raise RuntimeError(f"refusing to restore an unmarked directory: {root}")
        restored = 0
        for record_path in (root / "conda-meta").glob("*.json"):
            record = json.loads(record_path.read_text(encoding="utf-8"))
            package_dir = None
            for name in record.get("files", []):
                if not name.startswith(("bin/", "Library/bin/")):
                    continue
                binary_name = Path(name).name
                binary_name = binary_name[:-4] if binary_name.lower().endswith(".exe") else binary_name
                llvm_major = SPECS[1].split("=", 1)[1].split(".", 1)[0]
                versioned = binary_name.endswith("-" + llvm_major) and binary_name[: -(len(llvm_major) + 1)] in KEEP_BINARIES
                if binary_name not in KEEP_BINARIES and not versioned:
                    continue
                destination = root / name
                if destination.exists() or destination.is_symlink():
                    continue
                if package_dir is None:
                    package_value = record.get("extracted_package_dir")
                    if not package_value:
                        raise RuntimeError(f"package cache unavailable for restoring {record['name']} ({destination})")
                    package_dir = Path(package_value)
                source = package_dir / name
                if source.is_symlink():
                    destination.symlink_to(os.readlink(source))
                elif source.is_file():
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, destination)
                else:
                    raise RuntimeError(f"package source missing: {source}")
                restored += 1
        print(f"restored {restored} packaged bin entries for {target}")


def prune(targets: list[str]) -> None:
    for target in targets:
        if platform_key().startswith("windows-") and not target.startswith("windows-"):
            environment = os.environ.copy()
            environment["MSYS_NO_PATHCONV"] = "1"
            subprocess.run(["C:/Windows/System32/wsl.exe", "--", "python3", wsl_path(Path(__file__)), "prune", "--targets", target], env=environment, check=True)
            continue
        root = platform_directory(target)
        marker = root / ".cverify-toolchain"
        if not marker.is_file():
            raise RuntimeError(f"refusing to prune an unmarked directory: {root}")
        removed = 0
        for bin_dir in (root / "Library/bin", root / "bin", root / "host" / "bin"):
            if not bin_dir.is_dir():
                continue
            keep_names = set(KEEP_BINARIES)
            if target == "windows-x86_64" and bin_dir == root / "Library" / "bin":
                keep_names.difference_update(("clang", "clang++", "clang-23", "clang++-23"))
            llvm_major = SPECS[1].split("=", 1)[1].split(".", 1)[0]
            for path in bin_dir.iterdir():
                name = path.name[:-4] if path.name.lower().endswith(".exe") else path.name
                if name.endswith("-" + llvm_major) and name[: -(len(llvm_major) + 1)] in KEEP_BINARIES:
                    keep_names.add(name)
                if name in KEEP_BINARIES and path.is_symlink():
                    target_name = Path(os.readlink(path)).name
                    keep_names.add(target_name[:-4] if target_name.lower().endswith(".exe") else target_name)
            if target == "windows-x86_64" and bin_dir == root / "Library" / "bin":
                keep_names.difference_update(("clang", "clang++", "clang-23", "clang++-23"))
            for path in bin_dir.iterdir():
                if not path.is_file() and not path.is_symlink():
                    continue
                name = path.name[:-4] if path.name.lower().endswith(".exe") else path.name
                executable_file = path.suffix.lower() == ".exe" or "." not in path.name
                if executable_file and name not in keep_names:
                    path.unlink()
                    removed += 1
        for path in root.rglob("__pycache__"):
            if path.is_dir():
                shutil.rmtree(path)
        for path in root.rglob("*.pyc"):
            path.unlink()
        if target == "windows-x86_64":
            for name in ("aarch64-w64-mingw32", "armv7-w64-mingw32", "i686-w64-mingw32", "python"):
                unused = root / "host" / name
                if unused.is_dir():
                    shutil.rmtree(unused)
            for base in (root / "Library" / "lib" / "clang", root / "lib" / "clang"):
                unused = base / "23" / "lib"
                if unused.is_dir():
                    shutil.rmtree(unused)
        print(f"pruned {removed} unused executables from {target}")


def binary(root: Path, target: str, name: str) -> Path | None:
    filename = name + (".exe" if target.startswith("windows-") else "")
    candidates = []
    if target.startswith("windows-") and name in ("clang", "clang++"):
        candidates.append(root / "host" / "bin" / filename)
    candidates.extend(root / directory / filename for directory in (("Library/bin", "bin", "Scripts") if target.startswith("windows-") else ("bin",)))
    return next((path for path in candidates if path.is_file()), None)


def collect_notices(targets: list[str]) -> None:
    """Preserve license texts and build recipes available in package caches."""
    if platform_key().startswith("windows-") and any(not target.startswith("windows-") for target in targets):
        environment = os.environ.copy()
        environment["MSYS_NO_PATHCONV"] = "1"
        foreign = [target for target in targets if not target.startswith("windows-")]
        subprocess.run(["C:/Windows/System32/wsl.exe", "--", "python3", wsl_path(Path(__file__)), "notices", "--targets", *foreign], env=environment, check=True)
        native = [target for target in targets if target.startswith("windows-")]
        if native:
            collect_notices(native)
        return
    index_path = TOOLS / "notices" / "index.json"
    index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.is_file() else {}
    for target in targets:
        root = platform_directory(target)
        for record_path in sorted((root / "conda-meta").glob("*.json")):
            record = json.loads(record_path.read_text(encoding="utf-8"))
            package = f"{record['name']}-{record['version']}-{record['build']}"
            source_value = record.get("extracted_package_dir")
            source = Path(source_value) / "info" if source_value else None
            destination = TOOLS / "notices" / target / package
            copied = [name for name in ("licenses", "recipe") if (destination / name).is_dir()]
            for name in ("licenses", "recipe"):
                candidate = source / name if source is not None else None
                if candidate is not None and candidate.is_dir():
                    shutil.copytree(candidate, destination / name, dirs_exist_ok=True)
                    if name not in copied:
                        copied.append(name)
            index[f"{target}/{package}"] = {"copied": copied, "source_package_url": record.get("url")}
    (TOOLS / "notices" / "index.json").write_text(json.dumps(index, indent=2, sort_keys=True), encoding="utf-8")
    print(f"preserved notices/recipes under {TOOLS / 'notices'}")


def lock(targets: list[str]) -> None:
    if platform_key().startswith("windows-") and any(not target.startswith("windows-") for target in targets):
        environment = os.environ.copy()
        environment["MSYS_NO_PATHCONV"] = "1"
        subprocess.run(["C:/Windows/System32/wsl.exe", "--", "python3", wsl_path(Path(__file__)), "lock", "--targets", *targets], env=environment, check=True)
        return
    manifest = {"schema_version": 1, "generated_at": datetime.now(timezone.utc).isoformat(), "specs": SPECS, "platforms": {}}
    lizard_record = TOOLS / "python" / "lizard-package.json"
    if lizard_record.is_file():
        manifest["python_tools"] = {"lizard": json.loads(lizard_record.read_text(encoding="utf-8"))}
    notices = {}
    for target in targets:
        root = platform_directory(target)
        packages = []
        for record_path in sorted((root / "conda-meta").glob("*.json")):
            record = json.loads(record_path.read_text(encoding="utf-8"))
            package = {key: record.get(key) for key in ("name", "version", "build", "channel", "url", "sha256", "license", "license_family")}
            packages.append(package)
            notices[f"{package['name']}-{package['version']}-{package['build']}"] = package
        binaries = {}
        for name in TOOL_NAMES:
            path = binary(root, target, name)
            if path is None:
                raise RuntimeError(f"cannot lock missing tool {target}/{name}")
            binaries[name] = {"path": path.relative_to(root).as_posix(), "sha256": sha256(path), "bytes": path.stat().st_size}
        verification_path = root / "verification.json"
        verification = json.loads(verification_path.read_text(encoding="utf-8")) if verification_path.is_file() else {"runtime_verified": False}
        if target == "windows-x86_64" and (root / "host").is_dir():
            packages.append({"name": "llvm-mingw", "version": "20261020", "clang_version": "23.1.4", "url": "https://github.com/mstorsjo/llvm-mingw/releases/download/20261020/llvm-mingw-20261020-ucrt-x86_64.zip", "sha256": "fd5462b7f5d93b2ff981fcd3f9c7e427e47f3203c64ea91c27bcc444eebd74cd", "license": "Apache-2.0 WITH LLVM-exception and bundled library licenses"})
        manifest["platforms"][target] = {"conda_subdir": TARGETS[target], "packages": packages, "binaries": binaries, "verification": verification}
    (TOOLS / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (TOOLS / "THIRD_PARTY_NOTICES.json").write_text(json.dumps(notices, indent=2, sort_keys=True), encoding="utf-8")
    print(f"wrote {TOOLS / 'manifest.json'} and third-party package notices")


def verify_binary_architecture(target: str, path: Path) -> None:
    """Reject a foreign binary whose container architecture does not match its target."""
    expected_arm = target.endswith("arm64")
    with path.open("rb") as stream:
        header = stream.read(64)
        if target.startswith("windows-"):
            if header[:2] != b"MZ" or len(header) < 64:
                raise RuntimeError(f"not a PE binary: {path}")
            offset = struct.unpack_from("<I", header, 60)[0]
            stream.seek(offset)
            signature = stream.read(6)
            if signature[:4] != b"PE\0\0":
                raise RuntimeError(f"invalid PE signature: {path}")
            machine = struct.unpack_from("<H", signature, 4)[0]
            expected = 0xAA64 if expected_arm else 0x8664
        elif target.startswith("linux-"):
            if header[:5] != b"\x7fELF\x02":
                raise RuntimeError(f"not a 64-bit ELF binary: {path}")
            machine = struct.unpack_from("<H", header, 18)[0]
            expected = 183 if expected_arm else 62
        else:
            if header[:4] in (b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca"):
                raise RuntimeError(f"universal Mach-O needs per-architecture inspection: {path}")
            if header[:4] != b"\xcf\xfa\xed\xfe":
                raise RuntimeError(f"not a 64-bit little-endian Mach-O binary: {path}")
            machine = struct.unpack_from("<I", header, 4)[0]
            expected = 0x0100000C if expected_arm else 0x01000007
        if machine != expected:
            raise RuntimeError(f"architecture mismatch for {target}: {path} has machine 0x{machine:x}, expected 0x{expected:x}")


def verify(targets: list[str], self_test: bool) -> None:
    native = platform_key()
    for target in targets:
        if native.startswith("windows-") and not target.startswith("windows-"):
            environment = os.environ.copy()
            environment["MSYS_NO_PATHCONV"] = "1"
            command = ["C:/Windows/System32/wsl.exe", "--", "python3", wsl_path(Path(__file__)), "verify", "--targets", target]
            if self_test:
                command.append("--self-test")
            subprocess.run(command, env=environment, check=True)
            continue
        root = platform_directory(target)
        binaries = {name: binary(root, target, name) for name in TOOL_NAMES}
        missing = [name for name, path in binaries.items() if path is None]
        if missing:
            raise RuntimeError(f"{target}: missing {', '.join(missing)}")
        for name, path in binaries.items():
            verify_binary_architecture(target, path)
        if target != native:
            print(f"{target}: package structure and CPU architecture verified; runtime not executed on {native}")
            continue
        for name, path in binaries.items():
            process = subprocess.run([path, "--version"], capture_output=True, text=True, encoding="utf-8", errors="replace", check=False, timeout=30)
            if process.returncode != 0:
                raise RuntimeError(f"{target}/{name}: {(process.stdout + process.stderr)[-500:]}")
        if self_test:
            process = subprocess.run([sys.executable, SKILL_DIR / "scripts" / "c_verify.py", "--self-test", "--format", "text"], check=False)
            if process.returncode != 0:
                raise RuntimeError(f"self-test failed for {target} with exit code {process.returncode}")
            (root / "verification.json").write_text(json.dumps({"runtime_verified": True, "suite": "all unit/static/ASan/UBSan/coverage/libFuzzer tests", "verified_at": datetime.now(timezone.utc).isoformat(), "host": platform.platform()}, indent=2), encoding="utf-8")
        print(f"{target}: native tool versions passed" + ("; full self-test passed" if self_test else ""))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("install", "bootstrap", "create", "relocate", "restore", "prune", "notices", "lock", "verify"))
    parser.add_argument("--targets", nargs="+", choices=list(TARGETS), default=[platform_key()])
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.action == "install":
        install(args.targets)
    elif args.action == "bootstrap":
        bootstrap(args.targets)
    elif args.action == "create":
        create(args.targets)
    elif args.action == "relocate":
        for target in args.targets:
            relocate(target)
    elif args.action == "restore":
        restore(args.targets)
    elif args.action == "prune":
        prune(args.targets)
    elif args.action == "notices":
        collect_notices(args.targets)
    elif args.action == "lock":
        lock(args.targets)
    else:
        verify(args.targets, args.self_test)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
