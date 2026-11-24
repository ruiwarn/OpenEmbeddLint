from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from cverify.checks import append_issue, is_excluded_header_issue, parse_clang_tidy, run_cppcheck, run_project_command
from cverify.cli import load_config
from cverify.compilation import compiler_name_arguments, is_gnu_like_compiler, long_path_name, sanitize_dependency_arguments
from cverify.execution import ERROR, FAIL, PASS, SKIP, CheckResult, Issue, Toolchain, platform_key
from cverify.report import compact_document, exit_code, full_document


SKILL_DIR = Path(__file__).resolve().parents[1]


class PlatformTests(unittest.TestCase):
    def test_supported_platform_names(self):
        self.assertEqual(platform_key("Windows", "AMD64"), "windows-x86_64")
        self.assertEqual(platform_key("Linux", "x86_64"), "linux-x86_64")
        self.assertEqual(platform_key("Linux", "aarch64"), "linux-arm64")
        self.assertEqual(platform_key("Darwin", "arm64"), "macos-arm64")
        self.assertEqual(platform_key("Darwin", "x86_64"), "macos-x86_64")

    def test_unsupported_platform_is_explicit(self):
        with self.assertRaisesRegex(RuntimeError, "unsupported platform"):
            platform_key("FreeBSD", "x86_64")

    def test_tool_path_stays_inside_private_platform_directory(self):
        toolchain = Toolchain(SKILL_DIR)
        executable = toolchain.require("clang-tidy")
        self.assertTrue(executable.is_absolute())
        self.assertTrue(executable.is_relative_to(toolchain.root))
        self.assertEqual(executable.suffix.lower(), ".exe" if toolchain.is_windows else "")


class ReportTests(unittest.TestCase):
    def document(self, check):
        return full_document({"profile": "quick", "report_path": "report.json"}, [check], [])

    def test_success_is_minimal(self):
        document = self.document(CheckResult("cppcheck", "correctness", PASS, files=["a.c"]))
        compact = compact_document(document)
        self.assertEqual(compact, {"schema_version": 1, "status": "PASS", "complete": True, "files": 1, "checks": 1})
        self.assertEqual(exit_code(document["summary"]), 0)

    def test_optional_skip_does_not_create_a_wall_of_text(self):
        checks = [CheckResult("cppcheck", "correctness", PASS), CheckResult("review", "maintainability", SKIP, required=False, reason="agent review")]
        document = full_document({}, checks, [])
        compact = compact_document(document)
        self.assertNotIn("not_completed", compact)
        self.assertEqual(compact["skipped"], 1)

    def test_failure_contains_location_and_is_bounded(self):
        issues = [Issue(file="a.c", line=index, rule="rule", message="x" * 500) for index in range(1, 9)]
        document = self.document(CheckResult("clang-tidy", "correctness", FAIL, issues=issues))
        compact = compact_document(document)
        self.assertEqual(compact["findings"][0]["issues"][0]["line"], 1)
        self.assertEqual(len(compact["findings"][0]["issues"][0]["message"]), 240)
        self.assertEqual(exit_code(document["summary"]), 1)

    def test_required_skip_is_incomplete_not_pass(self):
        document = self.document(CheckResult("tests", "correctness", SKIP, required=True, reason="not configured"))
        self.assertEqual(document["summary"]["status"], "INCOMPLETE")
        self.assertFalse(document["summary"]["complete"])
        self.assertEqual(exit_code(document["summary"]), 3)

    def test_tool_error_is_not_a_clean_pass(self):
        document = self.document(CheckResult("clang-tidy", "correctness", ERROR, reason="missing binary"))
        self.assertEqual(exit_code(document["summary"]), 2)


class SuppressionTests(unittest.TestCase):
    def test_suppression_is_scoped_and_auditable(self):
        result = CheckResult("cppcheck", "correctness", PASS)
        suppression = {"check": "nullPointer", "path": "drivers/cmsis/core_cm0.h", "message": "vectors", "reason": "memory-mapped vector table", "needs_manual_confirmation": True}
        append_issue(result, Issue(file="drivers/cmsis/core_cm0.h", line=833, rule="nullPointer", message="Null pointer dereference: vectors"), [suppression])
        append_issue(result, Issue(file="src/other.c", line=9, rule="nullPointer", message="Null pointer dereference: vectors"), [suppression])
        self.assertEqual(len(result.issues), 1)
        self.assertEqual(result.issues[0].file, "src/other.c")
        self.assertEqual(len(result.metrics["suppressed"]), 1)
        self.assertTrue(result.metrics["suppressed"][0]["needs_manual_confirmation"])
        document = full_document({}, [result], [])
        self.assertEqual(document["summary"]["suppressed"], 1)


class ParserTests(unittest.TestCase):
    def test_windows_drive_and_rule_are_parsed(self):
        output = r"C:\work\a.c:12:7: warning: suspicious value [bugprone-example]"
        issues, unparsed = parse_clang_tidy(output, Path(r"C:\work"), "correctness")
        self.assertEqual(unparsed, [])
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].line, 12)
        self.assertEqual(issues[0].rule, "bugprone-example")

    def test_upgraded_rule_keeps_its_identity_and_dimension(self):
        issues, _ = parse_clang_tidy("a.c:2:3: error: forbidden call [custom-no-delay,-warnings-as-errors]", Path.cwd(), "correctness")
        self.assertEqual(issues[0].rule, "custom-no-delay")
        self.assertEqual(issues[0].severity, "error")
        self.assertEqual(issues[0].dimension, "project_rules")

    def test_locationless_config_warning_is_an_error(self):
        issues, _ = parse_clang_tidy("warning: 1:1: Matcher not found [clang-tidy-config]", Path.cwd(), "correctness")
        self.assertEqual(issues[0].rule, "clang-tidy-config")
        self.assertEqual(issues[0].severity, "error")


class ProjectRuleTests(unittest.TestCase):
    def run_rule(self, payload, code=0, **overrides):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            spec = {"id": "domain-lint", "kind": "project_rule", "argv": ["{python}", "-c", f"import sys; print({payload!r}); sys.exit({code})"], **overrides}
            return run_project_command(spec, root, root / "out", Toolchain(SKILL_DIR), 10)

    def test_structured_error_keeps_location_and_fails_even_with_zero_exit(self):
        result = self.run_rule(json.dumps({"issues": [{"rule": "domain-no-private-access", "message": "Use public API", "severity": "error", "file": "src/app.c", "line": 9, "column": 2}]}))
        self.assertEqual(result.status, FAIL)
        self.assertEqual(result.issues[0].rule, "domain-no-private-access")
        self.assertEqual(result.issues[0].file, "src/app.c")
        self.assertEqual(result.issues[0].line, 9)
        self.assertEqual(result.issues[0].dimension, "project_rules")

    def test_clean_and_advisory_rules_pass(self):
        for issues in ([], [{"rule": "domain-advice", "message": "Consider public API", "severity": "warning"}]):
            with self.subTest(issues=issues):
                self.assertEqual(self.run_rule(json.dumps({"issues": issues})).status, PASS)

    def test_invalid_results_are_execution_errors(self):
        for payload in ("not json", "[]", '{"issues": "bad"}', '{"issues":[{"rule":"x","message":"x","line":true}]}', '{"issues":[{"rule":"x","message":"x","severity":"fatal"}]}'):
            with self.subTest(payload=payload):
                self.assertEqual(self.run_rule(payload).status, ERROR)

    def test_checker_failure_is_not_a_code_violation(self):
        result = self.run_rule('{"issues": []}', code=2)
        self.assertEqual(result.status, ERROR)
        self.assertEqual(result.exit_code, 2)

    def test_sanitizer_failure_is_not_a_clean_checker_result(self):
        result = self.run_rule("", argv=["{python}", "-c", 'import sys; print(\'{"issues":[]}\'); sys.stderr.write("runtime error: invalid checker state")'])
        self.assertEqual(result.status, ERROR)

    def test_exit_one_requires_a_real_error_diagnostic(self):
        self.assertEqual(self.run_rule('{"issues":[]}', code=1).status, ERROR)
        result = self.run_rule('{"issues":[{"rule":"domain-error","message":"Use public API"}]}', code=1)
        self.assertEqual(result.status, FAIL)
        self.assertEqual(result.issues[0].severity, "error")

    def test_non_json_violation_is_an_invalid_checker_result(self):
        result = self.run_rule("Use the public API", code=1)
        self.assertEqual(result.status, ERROR)

    def test_empty_scope_cannot_remove_the_command_executable(self):
        self.assertEqual(self.run_rule("", argv=["{files}"]).status, ERROR)


class MissingToolTests(unittest.TestCase):
    def test_missing_required_private_tool_is_error(self):
        toolchain = Toolchain(SKILL_DIR, "windows-arm64")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "a.c"
            source.write_text("int main(void) { return 0; }\n", encoding="utf-8")
            result = run_cppcheck([source], root, toolchain, {}, 1, root, True)
        self.assertEqual(result.status, ERROR)
        self.assertIn("windows-arm64", result.reason)


class PortabilityAndSuppressionTests(unittest.TestCase):
    def test_compiler_triple_handles_posix_symlinks_and_windows_short_paths(self):
        self.assertEqual(compiler_name_arguments("/usr/bin/arm-none-eabi-gcc"), ["--target=arm-none-eabi"])
        self.assertTrue(is_gnu_like_compiler("/usr/bin/ccache-clang-18"))
        self.assertFalse(is_gnu_like_compiler("C:/iar/iccarm.exe"))
        self.assertEqual(long_path_name("/nonexistent/8dot3~1"), "/nonexistent/8dot3~1")
        with patch("cverify.compilation.os.name", "nt"), patch("cverify.compilation.long_path_name", return_value=r"C:\Toolchain\bin\arm-none-eabi-gcc.exe"):
            self.assertEqual(compiler_name_arguments(r"C:\PROGRA~2\TOOL~1\bin\AR19DD~1.EXE"), ["--target=arm-none-eabi"])
            self.assertTrue(is_gnu_like_compiler(r"C:\PROGRA~2\TOOL~1\bin\AR19DD~1.EXE"))

    def test_sanitize_dependency_arguments_keeps_macros_and_strips_codegen_warnings(self):
        cleaned = sanitize_dependency_arguments([
            "-Iinc", "-isystem", "/sys", "-D", "O2", "-include", "-g.h",
            "-mcpu=cortex-m4", "-mthumb", "-Wp,-DFROM_WP", "-Werror", "-Wall",
            "-O2", "-gdwarf-4", "-flto=auto", "--param", "max-inline=500",
            "-specs=nano.specs", "-Wl,--gc-sections", "-o", "out.o", "-c", "src.c",
        ])
        self.assertEqual(cleaned, ["-Iinc", "-isystem", "/sys", "-D", "O2", "-include", "-g.h", "-mcpu=cortex-m4", "-mthumb", "-Wp,-DFROM_WP", "src.c"])

    def test_symbol_and_line_pattern_suppressions_stay_scoped(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "a.c"
            source.write_text("void f(void) {\n    SPECIAL_MACRO();\n    return;\n}\n", encoding="utf-8")
            suppressions = [{"check": "misra-c2012-15.5", "path": "a.c", "symbol": "SPECIAL_MACRO", "line_pattern": r"SPECIAL_MACRO\(", "reason": "macro yields"}]
            result = CheckResult("cppcheck", "correctness", PASS)
            append_issue(result, Issue(file="a.c", line=2, rule="misra-c2012-15.5", message="Rule 15.5"), suppressions, root=root)
            append_issue(result, Issue(file="a.c", line=3, rule="misra-c2012-15.5", message="Rule 15.5"), suppressions, root=root)
            self.assertEqual(len(result.metrics.get("suppressed", [])), 1)
            self.assertEqual([i.line for i in result.issues], [3])
            (root / ".c-verify.json").write_text(json.dumps({"schema_version": 2, "suppressions": [{"check": "cppcheck", "line_pattern": "[unclosed", "reason": "bad"}]}), encoding="utf-8")
            with self.assertRaisesRegex(Exception, "line_pattern"):
                load_config(root, None)

    def test_compact_document_limit_zero_returns_all_findings(self):
        issues = [Issue(file="a.c", line=idx, rule="r", message="msg") for idx in range(1, 9)]
        doc = full_document({"profile": "quick", "report_path": "report.json"}, [CheckResult("clang-tidy", "correctness", FAIL, issues=issues)], [])
        compact = compact_document(doc, limit=0)
        self.assertEqual(len(compact["findings"][0]["issues"]), 8)
        self.assertNotIn("issues_omitted", compact)

    def test_external_header_filtered_even_with_empty_exclude_dirs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            root.mkdir()
            ext = Path(directory) / "external_sdk" / "inc.h"
            self.assertTrue(is_excluded_header_issue(Issue(file=str(ext), line=1, rule="r", message="w"), root, (), {"a.c"}))
            self.assertFalse(is_excluded_header_issue(Issue(file=str(ext), line=1, rule="clang-diagnostic-error", severity="error", message="fatal error: file not found"), root, (), {"a.c"}))


if __name__ == "__main__":
    unittest.main()
