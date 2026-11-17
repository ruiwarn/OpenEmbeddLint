#!/usr/bin/env python3
"""Build a libFuzzer target with only this skill's private Clang binaries."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from cverify.execution import PASS, Toolchain, ToolUnavailable, run_process


SKILL_DIR = Path(__file__).resolve().parents[1]


def run(argv, cwd, environment, log):
    return run_process(argv, cwd=cwd, timeout_s=300, log_path=log, extra_env=environment)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", action="append", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--flag", action="append", default=[], help="additional compile/link flag; repeatable")
    parser.add_argument("--sanitizers", default="address,undefined")
    parser.add_argument("--detail", action="store_true")
    args = parser.parse_args()
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if not output.suffix and sys.platform == "win32":
        output = output.with_suffix(".exe")
    objects = output.parent / (output.name + ".objects")
    objects.mkdir(exist_ok=True)
    log = output.parent / "fuzzer-build.log"
    records = []
    try:
        toolchain = Toolchain(SKILL_DIR)
        environment = toolchain.environment()
        sanitizers = args.sanitizers
        if toolchain.is_windows:
            compile_flags = [f"-fsanitize={sanitizers}", "-fsanitize-coverage=inline-8bit-counters,pc-table,trace-cmp"]
        else:
            compile_flags = [f"-fsanitize=fuzzer-no-link,{sanitizers}"]
        compile_flags.extend(["-fno-omit-frame-pointer", "-g", "-O1", *args.flag])
        object_files = []
        for index, source_name in enumerate(args.source):
            source = Path(source_name).resolve()
            compiler = toolchain.require("clang++" if source.suffix.lower() in (".cc", ".cpp", ".cxx") else "clang")
            object_file = objects / f"{index}-{source.stem}.o"
            process = run([compiler, *compile_flags, "-c", source, "-o", object_file], output.parent, environment, log)
            records.append(process.__dict__)
            if process.exit_code != 0 or process.start_error or process.timed_out:
                raise RuntimeError(process.start_error or process.stderr[-2000:] or "fuzz target compilation failed")
            object_files.append(object_file)
        linker = toolchain.require("clang++")
        if toolchain.is_windows:
            resource = run([linker, "--print-resource-dir"], output.parent, environment, log)
            if resource.exit_code != 0:
                raise RuntimeError(resource.stderr.strip() or "cannot locate Clang resource directory")
            architecture = "x86_64" if toolchain.platform.endswith("x86_64") else "i386"
            fuzzer = Path(resource.stdout.strip()) / "lib" / "windows" / f"libclang_rt.fuzzer-{architecture}.a"
            if not fuzzer.is_file():
                raise RuntimeError(f"private MinGW libFuzzer runtime not found: {fuzzer}")
            link_flags = [f"-fsanitize={sanitizers}", str(fuzzer), "-lpsapi"]
        else:
            link_flags = [f"-fsanitize=fuzzer,{sanitizers}"]
        process = run([linker, *link_flags, *object_files, *args.flag, "-o", output], output.parent, environment, log)
        records.append(process.__dict__)
        if process.exit_code != 0 or process.start_error or process.timed_out:
            raise RuntimeError(process.start_error or process.stderr[-2000:] or "fuzz target link failed")
    except (ToolUnavailable, RuntimeError, ValueError) as error:
        print(json.dumps({"status": "ERROR", "reason": str(error), "log": str(log), "commands": records if args.detail else None}, ensure_ascii=False, separators=(",", ":")))
        return 2
    result = {"status": PASS, "binary": str(output)}
    if args.detail:
        result.update({"platform": toolchain.platform, "commands": records, "log": str(log)})
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
