from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from cverify.checks import run_clang_tidy, run_coverage, run_cppcheck, run_project_command
from cverify.cli import main
from cverify.compilation import query_driver_arguments, resolve_entry_driver_arguments
from cverify.execution import FAIL, PASS, ProcessResult, Toolchain, run_process


SKILL_DIR = Path(__file__).resolve().parents[1]
FIXTURES = SKILL_DIR / "tests" / "fixtures"


def invoke(arguments):
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        status = main(arguments)
    return status, json.loads(output.getvalue())


class StaticIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "project"
        self.root.mkdir()
        for name in ("good.c", "style_bad.c", ".clang-format"):
            shutil.copy2(FIXTURES / "basic" / name, self.root / name)
        self.output = Path(self.temporary.name) / "artifacts"

    def tearDown(self):
        for attempt in range(5):
            try:
                self.temporary.cleanup()
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.2)

    def compdb(self, filename="good.c"):
        source = self.root / filename
        compiler = Toolchain(SKILL_DIR).require("clang++" if source.suffix == ".cpp" else "clang")
        standard = "c++17" if source.suffix == ".cpp" else "c11"
        entries = [{"directory": str(self.root), "arguments": [str(compiler), f"-std={standard}", "-c", str(source)], "file": str(source)}]
        path = self.root / "compile_commands.json"
        path.write_text(json.dumps(entries), encoding="utf-8")
        return path

    def arguments(self, filename="good.c"):
        return ["--project-root", str(self.root), "-f", filename, "--compdb", str(self.compdb(filename)), "--output-dir", str(self.output)]

    def native_config(self, options=None, warnings=None, **overrides):
        config = json.loads((SKILL_DIR / "defaults" / ".clang-tidy").read_text(encoding="utf-8"))
        config["CheckOptions"].update(options or {})
        if warnings is not None:
            config["WarningsAsErrors"] = warnings
        config.update(overrides)
        config["Checks"] += "".join(",custom-" + check["Name"] for check in config.get("CustomChecks", []))
        (self.root / ".clang-tidy").write_text(json.dumps(config), encoding="utf-8")

    def test_good_c_is_compact_and_uses_private_tools(self):
        status, document = invoke(self.arguments())
        self.assertEqual(status, 0, document)
        self.assertEqual(document["status"], "PASS")
        self.assertLessEqual(len(json.dumps(document)), 180)
        self.assertNotIn("findings", document)

    def test_project_output_is_rejected_before_any_write_in_all_profiles(self):
        before = {path.relative_to(self.root): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        for profile in ("quick", "full", "fuzz"):
            for destination in (self.root, self.root / "reports" / "nested"):
                with self.subTest(profile=profile, destination=destination):
                    status, document = invoke(["--project-root", str(self.root), "-f", "good.c", "--profile", profile, "--output-dir", str(destination)])
                    self.assertEqual(status, 2, document)
                    self.assertIn("outside", document["reason"])
                    self.assertEqual({path.relative_to(self.root): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}, before)
                    self.assertFalse((self.root / "reports").exists())

    def test_subproject_cannot_write_artifacts_elsewhere_in_its_repository(self):
        (self.root / ".git").mkdir()
        subproject = self.root / "module"
        subproject.mkdir()
        destination = self.root / "reports"
        status, document = invoke(["--project-root", str(subproject), "--allow-empty", "--output-dir", str(destination)])
        self.assertEqual(status, 2, document)
        self.assertIn("outside", document["reason"])
        self.assertFalse(destination.exists())

    def test_worktree_git_file_also_protects_the_repository(self):
        (self.root / ".git").write_text("gitdir: external-control-directory\n", encoding="utf-8")
        subproject = self.root / "module"
        subproject.mkdir()
        destination = self.root / "reports"
        self.assertEqual(invoke(["--project-root", str(subproject), "--allow-empty", "--output-dir", str(destination)])[0], 2)
        self.assertFalse(destination.exists())

    def test_output_alias_into_project_is_rejected(self):
        alias = self.root.parent / "project alias"
        if os.name == "nt":
            subprocess.run(["cmd", "/c", "mklink", "/J", str(alias), str(self.root)], capture_output=True, check=True)
        else:
            alias.symlink_to(self.root, target_is_directory=True)
        try:
            status, document = invoke(["--project-root", str(self.root), "--allow-empty", "--output-dir", str(alias / "reports")])
            self.assertEqual(status, 2, document)
            self.assertIn("outside", document["reason"])
            self.assertFalse((self.root / "reports").exists())
        finally:
            alias.rmdir() if os.name == "nt" else alias.unlink()

    def test_project_local_system_temp_is_rejected_without_writes(self):
        before = set(self.root.iterdir())
        with patch("cverify.cli.tempfile.gettempdir", return_value=str(self.root)):
            status, document = invoke(["--project-root", str(self.root), "--allow-empty"])
        self.assertEqual(status, 2, document)
        self.assertIn("outside", document["reason"])
        self.assertEqual(set(self.root.iterdir()), before)

    def test_invalid_project_does_not_allocate_output(self):
        status, document = invoke(["--project-root", str(self.root / "missing"), "--allow-empty", "--output-dir", str(self.output)])
        self.assertEqual(status, 2, document)
        self.assertIn("project root", document["reason"])
        self.assertFalse(self.output.exists())

    def test_repeated_default_runs_use_separate_external_directories(self):
        self.output.mkdir()
        reports = []
        before = set(self.root.iterdir())
        with patch("cverify.cli.tempfile.gettempdir", return_value=str(self.output)):
            for _ in range(2):
                status, document = invoke(["--project-root", str(self.root), "--allow-empty", "--detail"])
                self.assertEqual(status, 0, document)
                reports.append(Path(document["report_path"]))
        self.assertNotEqual(*reports)
        self.assertTrue(all(path.is_file() and path.is_relative_to(self.output.resolve()) for path in reports))
        self.assertEqual(set(self.root.iterdir()), before)

    def test_external_scan_preserves_source_and_compilation_database(self):
        arguments = self.arguments()
        before = {path.relative_to(self.root): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        status, document = invoke([*arguments, "--detail"])
        self.assertEqual(status, 0, document)
        self.assertTrue(Path(document["report_path"]).is_relative_to(self.output.resolve()))
        self.assertEqual({path.relative_to(self.root): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}, before)

    def test_cpp_standard_headers_use_private_query_driver(self):
        shutil.copy2(FIXTURES / "basic" / "good.cpp", self.root / "good.cpp")
        status, document = invoke(self.arguments("good.cpp"))
        self.assertEqual(status, 0, document)
        self.assertEqual(document["status"], "PASS")

    def test_detailed_output_proves_private_absolute_tool_path(self):
        status, document = invoke([*self.arguments(), "--detail"])
        self.assertEqual(status, 0, document)
        self.assertEqual(Path(document["tool_root"]), Toolchain(SKILL_DIR).root)
        tidy = next(check for check in document["checks"] if check["id"] == "clang-tidy")
        self.assertTrue(Path(tidy["tool"]).is_absolute())
        self.assertTrue(Path(tidy["tool"]).is_relative_to(Path(document["tool_root"])))

    def test_format_failure_is_found_without_log_dump(self):
        status, document = invoke(self.arguments("style_bad.c"))
        self.assertEqual(status, 1, document)
        finding = next(item for item in document["findings"] if item["id"] == "clang-format")
        self.assertEqual(finding["status"], "FAIL")

    def test_native_rule_policy_can_make_one_rule_advisory(self):
        self.native_config({"readability-function-size.StatementThreshold": "1"}, warnings="readability-function-cognitive-complexity,bugprone-branch-clone")
        status, document = invoke(self.arguments())
        self.assertEqual(status, 0, document)
        finding = next(item for item in document["findings"] if item["id"] == "clang-tidy")
        self.assertEqual(finding["status"], "PASS")
        self.assertTrue(any(issue["rule"] == "readability-function-size" and issue["severity"] == "warning" for issue in finding["issues"]))

    def test_native_cognitive_complexity_is_blocking(self):
        self.native_config({"readability-function-cognitive-complexity.Threshold": "1"})
        status, document = invoke(self.arguments())
        self.assertEqual(status, 1, document)
        finding = next(item for item in document["findings"] if item["id"] == "clang-tidy")
        self.assertEqual(finding["status"], "FAIL")
        self.assertTrue(any(issue["rule"] == "readability-function-cognitive-complexity" and issue["severity"] == "error" for issue in finding["issues"]))

    def test_readability_honors_header_filter(self):
        """改动头文件的复杂度必须通过真实引用单元报告。"""
        (self.root / "calc.h").write_text("static inline int calc(int x) { if(x==1)return 1; if(x==2)return 2; if(x==3)return 3; return 0; }\n", encoding="utf-8")
        (self.root / "good.c").write_text('#include "calc.h"\nint use(int x) { return calc(x); }\n', encoding="utf-8")
        self.native_config({"readability-function-cognitive-complexity.Threshold": "1"}, HeaderFilterRegex=".*")
        status, document = invoke([*self.arguments(), "--skip-format", "--detail"])
        check = next(item for item in document["checks"] if item["id"] == "clang-tidy")
        self.assertEqual(check["status"], "FAIL", check)
        self.assertTrue(any(Path(issue["file"]).name == "calc.h" and issue["severity"] == "error" for issue in check["issues"]), check)
        self.assertEqual(status, 1)
        _, excluded = invoke([*self.arguments(), "--header-filter", "^does-not-match/", "--skip-format", "--detail"])
        check = next(item for item in excluded["checks"] if item["id"] == "clang-tidy")
        self.assertFalse(any(Path(issue["file"]).name == "calc.h" for issue in check["issues"]), check)

    def test_cyclomatic_reports_real_mccabe_ccn(self):
        config = self.root / ".c-verify.json"
        config.write_text(json.dumps({"schema_version": 2, "cyclomatic": {"ccn_threshold": 2, "metric_policies": {"ccn": "report_only"}}}), encoding="utf-8")
        status, document = invoke(self.arguments())
        self.assertEqual(status, 0, document)
        finding = next(item for item in document["findings"] if item["id"] == "cyclomatic-complexity")
        self.assertEqual(finding["status"], "PASS")
        self.assertTrue(any("classify" in issue["message"] and "CCN 3" in issue["message"] for issue in finding["issues"]))
        self.assertTrue(all(issue["severity"] == "warning" for issue in finding["issues"]))

    def test_cyclomatic_is_error_and_survives_error_only_filter(self):
        config = self.root / ".c-verify.json"
        config.write_text(json.dumps({"schema_version": 2, "cyclomatic": {"ccn_threshold": 2}}), encoding="utf-8")
        status, document = invoke([*self.arguments(), "--severity", "error"])
        self.assertEqual(status, 1, document)
        finding = next(item for item in document["findings"] if item["id"] == "cyclomatic-complexity")
        self.assertEqual(finding["status"], "FAIL")
        self.assertTrue(any(issue["rule"] == "cyclomatic-complexity" and issue["severity"] == "error" for issue in finding["issues"]))

    def test_personal_config_overrides_one_metric_without_replacing_project(self):
        project_config = self.root / ".c-verify.json"
        project_config.write_text(json.dumps({"schema_version": 2, "cyclomatic": {"ccn_threshold": 2, "metric_policies": {"ccn": "blocking"}}}), encoding="utf-8")
        personal_config = self.root / "personal.json"
        personal_config.write_text(json.dumps({"schema_version": 2, "cyclomatic": {"metric_policies": {"ccn": "report_only"}}}), encoding="utf-8")
        status, document = invoke([*self.arguments(), "--config", str(personal_config)])
        self.assertEqual(status, 0, document)
        finding = next(item for item in document["findings"] if item["id"] == "cyclomatic-complexity")
        self.assertEqual(finding["status"], "PASS")
        self.assertTrue(any("CCN 3" in issue["message"] for issue in finding["issues"]))

    def test_nloc_and_total_length_thresholds_are_independent(self):
        config = self.root / ".c-verify.json"
        config.write_text(json.dumps({"schema_version": 2, "cyclomatic": {"ccn_threshold": 30, "nloc_threshold": 5, "length_threshold": 8}}), encoding="utf-8")
        status, document = invoke(self.arguments())
        self.assertEqual(status, 1, document)
        finding = next(item for item in document["findings"] if item["id"] == "cyclomatic-complexity")
        self.assertEqual(finding["status"], "FAIL")
        rules = {issue["rule"] for issue in finding["issues"]}
        self.assertEqual(rules, {"function-nloc", "function-length"})
        self.assertTrue(all(issue["severity"] == "error" for issue in finding["issues"]))

    def test_private_driver_preserves_compilation_target(self):
        source = self.root / "arm.c"
        source.write_text('_Static_assert(sizeof(void *) == 4, "ARM target must be 32-bit");\nint main(void) { return 0; }\n', encoding="utf-8")
        arguments = self.arguments("arm.c")
        compiler = Toolchain(SKILL_DIR).require("clang")
        entries = [{"directory": str(self.root), "command": f'"{compiler}" --target=arm-none-eabi -std=c11 -c arm.c', "file": str(source)}]
        (self.root / "compile_commands.json").write_text(json.dumps(entries), encoding="utf-8")
        status, document = invoke([*arguments, "--skip-cppcheck", "--skip-format", "--skip-cyclomatic"])
        self.assertEqual(status, 0, document)

    def test_missing_include_cannot_be_suppressed_into_pass(self):
        source = self.root / "missing.c"
        source.write_text('#include "missing.h"\nint main(void) { return 0; }\n', encoding="utf-8")
        config = self.root / ".c-verify.json"
        config.write_text(json.dumps({"schema_version": 2, "suppressions": [{"check": "clang-diagnostic-error", "path": "missing.c", "message": "missing.h", "reason": "negative regression"}]}), encoding="utf-8")
        status, document = invoke([*self.arguments("missing.c"), "--skip-cppcheck", "--skip-format", "--skip-cyclomatic"])
        self.assertEqual(status, 2, document)
        self.assertEqual(document["status"], "ERROR")
        self.assertFalse(document["complete"])
        self.assertEqual(document["suppressed"], 1)

    def test_invalid_nested_policy_is_configuration_error(self):
        config = self.root / ".c-verify.json"
        config.write_text(json.dumps({"schema_version": 2, "cyclomatic": {"ccn_threshold": 1, "metric_policies": {"ccn": "blockng"}}}), encoding="utf-8")
        status, document = invoke(self.arguments())
        self.assertEqual(status, 2, document)
        self.assertEqual(document["status"], "ERROR")
        self.assertIn("blockng", document["reason"])

    def test_native_warnings_as_errors_is_the_only_tidy_severity_policy(self):
        self.native_config({"readability-function-cognitive-complexity.Threshold": "1"}, warnings="")
        status, document = invoke(self.arguments())
        self.assertEqual(status, 0, document)
        finding = next(item for item in document["findings"] if item["id"] == "clang-tidy")
        self.assertEqual(finding["status"], "PASS")
        self.assertTrue(any(issue["severity"] == "warning" for issue in finding["issues"]))

    def test_removed_rule_fields_are_rejected(self):
        for override in ({"readability": {}}, {"static": {"clang_tidy_checks": "bugprone-*"}}, {"static": {"header_filter": ".*"}}, {"static": {"ignore_checks": []}}, {"static": {"misra_rule_texts": "external.txt"}}):
            with self.subTest(override=override):
                (self.root / ".c-verify.json").write_text(json.dumps({"schema_version": 2, **override}), encoding="utf-8")
                status, document = invoke(self.arguments())
                self.assertEqual(status, 2, document)
                self.assertEqual(document["status"], "ERROR")
                self.assertIn("unknown key", document["reason"])
        (self.root / ".c-verify.json").write_text('{"schema_version":1}', encoding="utf-8")
        status, document = invoke(self.arguments())
        self.assertEqual(status, 2, document)
        self.assertIn("schema_version 2", document["reason"])

    def test_preprocessor_error_cannot_be_suppressed_into_pass(self):
        source = self.root / "invalid.c"
        source.write_text('#error TARGET_CONFIGURATION_INVALID\nint main(void) { return 0; }\n', encoding="utf-8")
        config = self.root / ".c-verify.json"
        config.write_text(json.dumps({"schema_version": 2, "suppressions": [{"check": "clang-diagnostic-error", "path": "invalid.c", "message": "TARGET_CONFIGURATION_INVALID", "reason": "negative regression"}]}), encoding="utf-8")
        status, document = invoke([*self.arguments("invalid.c"), "--skip-cppcheck", "--skip-format", "--skip-cyclomatic"])
        self.assertEqual(status, 2, document)
        self.assertEqual(document["status"], "ERROR")
        self.assertFalse(document["complete"])
        self.assertEqual(document["suppressed"], 1)

    def test_response_file_target_is_preserved(self):
        source = self.root / "arm-response.c"
        source.write_text('_Static_assert(sizeof(void *) == 4, "ARM context lost");\nint main(void) { return 0; }\n', encoding="utf-8")
        (self.root / "arm.rsp").write_text("--target=arm-none-eabi -ffreestanding\n", encoding="utf-8")
        arguments = self.arguments("arm-response.c")
        compiler = Toolchain(SKILL_DIR).require("clang")
        entries = [{"directory": str(self.root), "command": f'"{compiler}" @arm.rsp -std=c11 -c arm-response.c', "file": str(source)}]
        (self.root / "compile_commands.json").write_text(json.dumps(entries), encoding="utf-8")
        status, document = invoke([*arguments, "--skip-cppcheck", "--skip-format", "--skip-cyclomatic"])
        self.assertEqual(status, 0, document)

    def test_missing_response_file_is_an_execution_error(self):
        arguments = self.arguments("good.c")
        compiler = Toolchain(SKILL_DIR).require("clang")
        source = self.root / "good.c"
        entries = [{"directory": str(self.root), "command": f'"{compiler}" @missing.rsp -std=c11 -c good.c', "file": str(source)}]
        (self.root / "compile_commands.json").write_text(json.dumps(entries), encoding="utf-8")
        status, document = invoke([*arguments, "--skip-cppcheck", "--skip-format", "--skip-cyclomatic"])
        self.assertEqual(status, 2, document)
        self.assertEqual(document["status"], "ERROR")
        self.assertIn("response file", document["findings"][0]["reason"])

    def test_null_metric_policy_map_returns_error_json(self):
        (self.root / ".c-verify.json").write_text(json.dumps({"schema_version": 2, "cyclomatic": {"metric_policies": None}}), encoding="utf-8")
        status, document = invoke(self.arguments())
        self.assertEqual(status, 2, document)
        self.assertIn("expected object", document["reason"])

    def test_full_profile_cannot_pass_without_tests_and_coverage(self):
        status, document = invoke([*self.arguments(), "--profile", "full"])
        self.assertEqual(status, 3, document)
        identifiers = {item["id"] for item in document["not_completed"]}
        self.assertIn("project-tests", identifiers)
        self.assertIn("llvm-cov", identifiers)

    def irq_rule(self, query=None):
        self.native_config(warnings="custom-no-delay-in-irq", CustomChecks=[{
            "Name": "no-delay-in-irq",
            "Query": query or 'match callExpr(callee(functionDecl(hasName("vTaskDelay"))), hasAncestor(functionDecl(matchesName("_IRQHandler$")))).bind("call")',
            "Diagnostic": [{"BindName": "call", "Message": "Notify a task instead of blocking in IRQ.", "Level": "Warning"}],
        }])

    def test_custom_rule_runs_in_quick_and_keeps_identity(self):
        self.irq_rule()
        (self.root / "good.c").write_text("void vTaskDelay(unsigned);\nvoid UART_IRQHandler(void) { vTaskDelay(1U); }\n", encoding="utf-8")
        status, document = invoke([*self.arguments(), "--skip-format", "--severity", "error", "--detail"])
        self.assertEqual(status, 1, document)
        check = next(item for item in document["checks"] if item["id"] == "clang-tidy")
        issue = next(item for item in check["issues"] if item["rule"] == "custom-no-delay-in-irq")
        self.assertEqual((issue["file"], issue["line"], issue["severity"], issue["dimension"]), ("good.c", 2, "error", "project_rules"))
        self.assertEqual(len(check["metrics"]["runs"]), 1)
        self.assertNotIn("readability", {item["id"] for item in document["checks"]})
        self.assertEqual(Path(check["metrics"]["config_files"]["good.c"]).resolve(), (self.root / ".clang-tidy").resolve())

    def test_custom_rule_allows_task_delay_and_nonblocking_irq(self):
        self.irq_rule()
        for body in ("void WorkerTask(void) { vTaskDelay(1U); }", "void UART_IRQHandler(void) { NotifyTaskFromISR(); }"):
            with self.subTest(body=body):
                (self.root / "good.c").write_text("void vTaskDelay(unsigned);\nvoid NotifyTaskFromISR(void);\n" + body + "\n", encoding="utf-8")
                status, document = invoke([*self.arguments(), "--skip-format"])
                self.assertEqual(status, 0, document)

    def test_invalid_matcher_is_error_despite_tool_zero_exit(self):
        self.irq_rule("match missingMatcher()")
        (self.root / ".c-verify.json").write_text(json.dumps({"schema_version": 2, "suppressions": [{"check": "clang-tidy-config", "reason": "negative fixture"}]}), encoding="utf-8")
        status, document = invoke([*self.arguments(), "--detail"])
        self.assertEqual(status, 2, document)
        self.assertFalse(document["summary"]["complete"])
        check = next(item for item in document["checks"] if item["id"] == "clang-tidy")
        self.assertEqual(check["metrics"]["runs"][0]["exit_code"], 0)
        self.assertEqual(check["status"], "ERROR")

    def test_project_rules_run_once_in_quick_and_full_and_receive_scope(self):
        for profile in ("quick", "full"):
            with self.subTest(profile=profile):
                marker = self.root / (profile + ".txt")
                code = 'import json,sys; from pathlib import Path; p=Path(sys.argv[1]); p.write_text(p.read_text()+"x" if p.exists() else "x"); assert len(sys.argv)==3 and Path(sys.argv[2]).name=="good.c"; print(json.dumps({"issues":[]}))'
                commands = [{"id": "domain-lint", "kind": "project_rule", "argv": ["{python}", "-c", code, str(marker), "{files}"]}, {"id": "build-marker", "kind": "build", "argv": ["{python}", "-c", "pass"]}]
                (self.root / ".c-verify.json").write_text(json.dumps({"schema_version": 2, "commands": commands}), encoding="utf-8")
                status, document = invoke([*self.arguments(), "--profile", profile, "--detail"])
                identifiers = [check["id"] for check in document["checks"]]
                self.assertEqual(identifiers.count("domain-lint"), 1)
                self.assertEqual(marker.read_text(), "x")
                self.assertEqual("build-marker" in identifiers, profile == "full")
                self.assertEqual(status, 0 if profile == "quick" else 3, document)

    def test_native_rules_require_compilation_context(self):
        self.irq_rule()
        status, document = invoke(["--project-root", str(self.root), "-f", "good.c", "--skip-cppcheck", "--output-dir", str(self.output)])
        self.assertEqual(status, 2, document)
        self.assertFalse(document["complete"])

    def test_partial_compilation_context_does_not_establish_a_pass(self):
        (self.root / "unmapped.c").write_text("void unmapped(void) {}\n", encoding="utf-8")
        status, document = invoke([*self.arguments(), "-f", "unmapped.c", "--skip-cppcheck", "--skip-format", "--detail"])
        self.assertEqual(status, 2, document)
        self.assertFalse(document["summary"]["complete"])
        check = next(item for item in document["checks"] if item["id"] == "clang-tidy")
        self.assertIn("unmapped.c", check["reason"])

    def test_native_subdirectory_configuration_inherits_parent_rules(self):
        self.irq_rule()
        subdir = self.root / "sub"
        subdir.mkdir()
        (subdir / ".clang-tidy").write_text('{"InheritParentConfig":true}', encoding="utf-8")
        (subdir / "irq.c").write_text("void vTaskDelay(unsigned);\nvoid UART_IRQHandler(void) { vTaskDelay(1U); }\n", encoding="utf-8")
        status, document = invoke([*self.arguments("sub/irq.c"), "--skip-format"])
        self.assertEqual(status, 1, document)
        self.assertTrue(any(issue["rule"] == "custom-no-delay-in-irq" for check in document["findings"] for issue in check["issues"]))


class RuntimeIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.output = self.root / "out"
        self.output.mkdir()
        self.toolchain = Toolchain(SKILL_DIR)
        self.executable_suffix = ".exe" if self.toolchain.is_windows else ""

    def tearDown(self):
        for attempt in range(5):
            try:
                self.temporary.cleanup()
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.2)

    def command(self, check_id, kind, argv, env=None):
        result = run_project_command(
            {"id": check_id, "kind": kind, "argv": argv, "env": env or {}, "timeout_s": 120},
            self.root, self.output, self.toolchain, 120,
        )
        return result

    def build(self, name, source, flags):
        executable = self.output / (name + self.executable_suffix)
        result = self.command(name + "-build", "build", ["{tool:clang}", *flags, str(source), "-o", str(executable)])
        self.assertEqual(result.status, PASS, f"{result.reason}\n{result.issues[0].message if result.issues else ''}\n{result.log}")
        return executable

    def test_asan_detects_real_out_of_bounds_access(self):
        executable = self.build("asan", FIXTURES / "runtime" / "asan_oob.c", ["-fsanitize=address", "-fno-omit-frame-pointer", "-g", "-O0"])
        result = self.command("asan-run", "test", [str(executable)], {"ASAN_OPTIONS": "detect_leaks=0:halt_on_error=1:symbolize=1"})
        self.assertEqual(result.status, FAIL, result.reason)
        self.assertIn("AddressSanitizer", result.issues[0].message)

    def test_ubsan_detects_real_undefined_shift(self):
        executable = self.build("ubsan", FIXTURES / "runtime" / "ubsan_shift.c", ["-fsanitize=undefined", "-fno-sanitize-recover=undefined", "-g", "-O0"])
        result = self.command("ubsan-run", "test", [str(executable)], {"UBSAN_OPTIONS": "print_stacktrace=1:halt_on_error=1"})
        self.assertEqual(result.status, FAIL, result.reason)
        self.assertIn("runtime error", result.issues[0].message)

    def test_private_cmake_ninja_and_compiler_build_real_project(self):
        build_dir = self.output / "cmake-build"
        configure = self.command("cmake-configure", "build", ["{tool:cmake}", "-S", str(FIXTURES / "cmake"), "-B", str(build_dir), "-G", "Ninja", "-DCMAKE_MAKE_PROGRAM={tool:ninja}", "-DCMAKE_C_COMPILER={tool:clang}", "-DCMAKE_CXX_COMPILER={tool:clang++}"])
        self.assertEqual(configure.status, PASS, f"{configure.reason}\n{configure.issues[0].message if configure.issues else ''}")
        build = self.command("cmake-build", "build", ["{tool:cmake}", "--build", str(build_dir)])
        self.assertEqual(build.status, PASS, f"{build.reason}\n{build.issues[0].message if build.issues else ''}")
        test = self.command("ctest", "test", ["{tool:ctest}", "--test-dir", str(build_dir), "--output-on-failure"])
        self.assertEqual(test.status, PASS, f"{test.reason}\n{test.issues[0].message if test.issues else ''}")

    def test_coverage_reports_unexecuted_branches(self):
        profile = self.output / "good.profraw"
        executable = self.build("coverage", FIXTURES / "basic" / "good.c", [f"-fprofile-instr-generate={profile}", "-fcoverage-mapping", "-g", "-O0"])
        run = self.command("coverage-run", "test", [str(executable)])
        self.assertEqual(run.status, PASS, run.reason)
        result = run_coverage(
            {"binary": str(executable), "profraw": [str(self.output / "*.profraw")], "sources": [str(FIXTURES / "basic" / "good.c")], "required": True},
            self.root, self.output, self.toolchain, 120,
        )
        self.assertEqual(result.status, PASS, result.reason)
        self.assertTrue(any("branch" in gap for gap in result.gaps), result.metrics)

    def test_coverage_show_failure_is_not_a_clean_pass(self):
        source = self.root / "deleted-source.c"
        shutil.copy2(FIXTURES / "basic" / "good.c", source)
        profile = self.output / "deleted.profraw"
        executable = self.build("deleted-coverage", source, [f"-fprofile-instr-generate={profile}", "-fcoverage-mapping", "-g", "-O0"])
        run = self.command("deleted-coverage-run", "test", [str(executable)])
        self.assertEqual(run.status, PASS, run.reason)
        source.unlink()
        result = run_coverage(
            {"binary": str(executable), "profraw": [str(profile)], "sources": [str(source)], "required": True},
            self.root, self.output, self.toolchain, 120,
        )
        self.assertEqual(result.status, "ERROR")
        self.assertTrue(result.gaps)

    def test_libfuzzer_preserves_a_real_crash(self):
        corpus = self.root / "corpus"
        corpus.mkdir()
        (corpus / "seed").write_bytes(b"CVER")
        artifacts = self.output / "fuzz-artifacts"
        artifacts.mkdir()
        output = self.output / ("fuzz" + self.executable_suffix)
        build = self.command("fuzz-build", "build", ["{python}", "{skill_dir}/scripts/build_fuzzer.py", "--source", str(FIXTURES / "runtime" / "parser_fuzz.c"), "--output", str(output)])
        self.assertEqual(build.status, PASS, f"{build.reason}\n{build.issues[0].message if build.issues else ''}\n{build.log}")
        result = self.command("fuzz-run", "fuzz", [str(output), str(corpus), "-runs=1", f"-artifact_prefix={artifacts}/"], {"ASAN_OPTIONS": "detect_leaks=0:symbolize=1"})
        self.assertEqual(result.status, FAIL, result.reason)
        self.assertTrue(list(artifacts.glob("crash-*")), result.issues[0].message if result.issues else result.reason)
    def test_libfuzzer_has_real_coverage_feedback(self):
        output = self.output / ("fuzz-coverage" + self.executable_suffix)
        build = self.command("fuzz-coverage-build", "build", ["{python}", "{skill_dir}/scripts/build_fuzzer.py", "--source", str(FIXTURES / "runtime" / "parser_fuzz.c"), "--output", str(output)])
        self.assertEqual(build.status, PASS, f"{build.reason}\n{build.issues[0].message if build.issues else ''}\n{build.log}")
        objects = list((self.output / (output.name + ".objects")).glob("*.o"))
        self.assertTrue(objects)
        nm = run_process([self.toolchain.require("llvm-nm"), *objects], cwd=self.root, timeout_s=120, extra_env=self.toolchain.environment())
        self.assertEqual(nm.exit_code, 0, nm.stderr)
        self.assertIn("__sanitizer_cov", nm.stdout)
        corpus = self.root / "coverage-corpus"
        corpus.mkdir()
        (corpus / "seed").write_bytes(b"A")
        run = self.command("fuzz-coverage-run", "fuzz", [str(output), str(corpus), "-runs=100"])
        self.assertEqual(run.status, PASS, run.reason)
        self.assertIn("cov:", Path(run.log).read_text(encoding="utf-8"))

    def test_external_gnu_driver_probe_and_graceful_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inc = root / "sdk include" / "include"
            gcc_inc = root / "lib" / "gcc" / "arm-none-eabi" / "10.3.1" / "include"
            gcc_fixed = root / "lib" / "gcc" / "arm-none-eabi" / "10.3.1" / "include-fixed"
            fw = root / "Frameworks"
            for path in (inc, gcc_inc, gcc_fixed, fw):
                path.mkdir(parents=True)
            ext_gcc = root / "bin" / "arm-none-eabi-gcc.exe"
            ext_clang = root / "bin" / "clang"
            ext_gcc.parent.mkdir()
            ext_gcc.write_bytes(b"")
            fake_stderr = (
                "Target: arm-none-eabi\n"
                "#include <...> search starts here:\n"
                f" {inc}\n"
                f" {gcc_inc}\n"
                f" {gcc_fixed}\n"
                f" {fw} (framework directory)\n"
                "End of search list.\n"
            )
            with patch("cverify.compilation.run_process", return_value=ProcessResult(["arm-none-eabi-gcc"], 0, "#define __GNUC__ 10\n#define __GNUC_MINOR__ 3\n#define __GNUC_PATCHLEVEL__ 1\n#define IGNORED_MACRO 1\n", fake_stderr, 0.01)):
                args, rec = resolve_entry_driver_arguments({"directory": str(root), "file": "a.c", "arguments": [str(ext_gcc), "-mcpu=cortex-m0", "-DFOO=1", "-c", "a.c"]}, Toolchain(SKILL_DIR), False, root)
            self.assertEqual(args, [
                "--target=arm-none-eabi",
                f"-isystem{inc.resolve().as_posix()}",
                f"-idirafter{gcc_inc.resolve().as_posix()}",
                f"-idirafter{gcc_fixed.resolve().as_posix()}",
                f"-iframework{fw.resolve().as_posix()}",
            ])
            self.assertEqual(rec.get("defines"), ["__GNUC_MINOR__=3", "__GNUC_PATCHLEVEL__=1", "__GNUC__=10"])
            self.assertNotIn("output", rec)
            self.assertFalse(rec.get("fallback"))
            with patch("cverify.compilation.run_process", return_value=ProcessResult(["arm-none-eabi-gcc"], 1, "", "unsupported", 0.01)):
                fb_args, fb_rec = resolve_entry_driver_arguments({"directory": str(root), "file": "a.c", "arguments": [str(ext_gcc), "-c", "a.c"]}, Toolchain(SKILL_DIR), False, root)
            self.assertEqual(fb_args, ["--target=arm-none-eabi"])
            self.assertEqual(fb_rec.get("defines"), [])
            self.assertTrue(fb_rec.get("fallback"))
            with patch("cverify.compilation.run_process", return_value=ProcessResult(["clang"], 0, "", fake_stderr, 0.01)):
                q_args, _ = query_driver_arguments(ext_clang, True, root)
            self.assertEqual(q_args, [
                "--target=arm-none-eabi",
                f"-isystem{inc.resolve().as_posix()}",
                f"-isystem{gcc_inc.resolve().as_posix()}",
                f"-isystem{gcc_fixed.resolve().as_posix()}",
                f"-iframework{fw.resolve().as_posix()}",
            ])

    def test_cppcheck_injects_driver_identity_defines_and_tidy_dedups_queries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "proj"
            out = Path(directory) / "out"
            root.mkdir()
            src_a = root / "a.c"
            src_b = root / "b.c"
            code = "#ifndef __GNUC__\n#error Unknown compiler.\n#endif\nint fn(void) { return 0; }\n"
            src_a.write_text(code, encoding="utf-8")
            src_b.write_text(code, encoding="utf-8")
            compiler = Toolchain(SKILL_DIR).require("clang")
            compdb = root / "compile_commands.json"
            compdb.write_text(json.dumps([
                {"directory": str(root), "file": "a.c", "arguments": [str(compiler), "-std=c11", "-c", "a.c"]},
                {"directory": str(root), "file": "b.c", "arguments": [str(compiler), "-std=c11", "-c", "b.c"]},
            ]), encoding="utf-8")
            fake_query = {"command": ["arm-none-eabi-gcc", "-E"], "exit_code": 0, "arguments": [], "defines": ["__GNUC__=10", "__GNUC_MINOR__=3"]}
            with patch("cverify.checks.resolve_entry_driver_arguments", return_value=([], fake_query)):
                cpp_res = run_cppcheck([src_a], root, Toolchain(SKILL_DIR), {"cppcheck_defines": ["__GNUC__=9"]}, 60, out / "cpp", True, compdb)
            self.assertEqual(cpp_res.status, PASS, cpp_res.reason)
            log_text = Path(cpp_res.metrics["runs"][0]["log"]).read_text(encoding="utf-8")
            self.assertIn("-D__GNUC_MINOR__=3", log_text)
            self.assertNotIn("-D__GNUC__=10", log_text)

            (root / ".clang-tidy").write_text('{"Checks":"-*,readability-magic-numbers"}', encoding="utf-8")
            tidy_res = run_clang_tidy([src_a, src_b], root, Toolchain(SKILL_DIR), {}, 60, out / "tidy", True, compdb)
            self.assertEqual(tidy_res.status, PASS, tidy_res.reason)
            self.assertEqual(len(tidy_res.metrics.get("driver_queries", [])), 1)

    def test_actionable_and_prune_temp_cli_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory) / "tmp"
            stale = temp / "c-verify-old123"
            explicit = temp / "run-keep123"
            stale.mkdir(parents=True)
            (stale / "artifact.txt").write_text("stale", encoding="utf-8")
            explicit.mkdir(parents=True)
            with patch("cverify.cli.tempfile.gettempdir", return_value=str(temp)):
                status, doc = invoke(["--prune-temp"])
            self.assertEqual(status, 0, doc)
            self.assertEqual((doc["pruned_dirs"], doc["freed_bytes"] >= 5), (1, True))
            self.assertFalse(stale.exists())
            self.assertTrue(explicit.exists())

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "proj"
            root.mkdir()
            (root / "many.c").write_text("\n".join(f"int f{i}(void) {{ return {10 + i}; }}" for i in range(8)) + "\n", encoding="utf-8")
            compiler = Toolchain(SKILL_DIR).require("clang")
            (root / "compile_commands.json").write_text(json.dumps([{"directory": str(root), "file": "many.c", "arguments": [str(compiler), "-std=c11", "-c", "many.c"]}]), encoding="utf-8")
            (root / ".clang-tidy").write_text(json.dumps({"Checks": "-*,readability-magic-numbers", "WarningsAsErrors": "*"}), encoding="utf-8")
            status, compact = invoke(["--project-root", str(root), "-f", "many.c", "--skip-cppcheck", "--skip-format", "--skip-cyclomatic", "--actionable", "--output-dir", str(Path(directory) / "out")])
            self.assertEqual(status, 1, compact)
            self.assertEqual(len(compact["findings"][0]["issues"]), 8)
            self.assertNotIn("issues_omitted", compact)


if __name__ == "__main__":
    unittest.main()
