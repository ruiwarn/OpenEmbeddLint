import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from cverify.checks import run_cppcheck
from cverify.cli import project_report
from cverify.execution import Toolchain
from cverify.incremental.snapshots import git
from test_integration import invoke


SKILL_DIR = Path(__file__).resolve().parents[1]
BAD = "int main(void)\n{\n    goto done;\ndone:\n    return 0;\n}\n"
GOOD = "int main(void)\n{\n    return 0;\n}\n"


class MisraTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="cverify misra ")
        self.root = Path(self.temporary.name) / "project"
        self.root.mkdir()
        self.toolchain = Toolchain(SKILL_DIR)
        self.user_data = patch("cverify.installation.user_data_directory", return_value=Path(self.temporary.name) / "user-data")
        self.user_data.start()
        self.addCleanup(self.user_data.stop)
        self.environment = patch.dict(os.environ, {"OPENEMBEDDLINT_MISRA_RULE_TEXTS": "", "OPENEMBEDDLINT_TOOLS_DIR": str(self.toolchain.root.parent)})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.output = Path(self.temporary.name) / "reports"
        (self.root / "a.c").write_text(BAD, encoding="utf-8")
        compiler = self.toolchain.require("clang")
        database = [{"directory": str(self.root), "file": "a.c", "arguments": [str(compiler), "-std=c11", "-c", "a.c"]}]
        (self.root / "compile_commands.json").write_text(json.dumps(database), encoding="utf-8")
        self.config = {"schema_version": 2, "format": {"enabled": False}, "cyclomatic": {"enabled": False}}
        self.save_config()

    def tearDown(self):
        self.temporary.cleanup()

    def save_config(self):
        (self.root / ".c-verify.json").write_text(json.dumps(self.config), encoding="utf-8")

    def scan(self, *arguments):
        return invoke(["--project-root", str(self.root), "--output-dir", str(self.output), "-f", "a.c", "--skip-clang-tidy", "--detail", *arguments])

    def test_opt_in_detects_a_real_misra_violation(self):
        code, document = self.scan("--misra-c2012")
        self.assertEqual(code, 1, document)
        check = next(check for check in document["checks"] if check["id"] == "cppcheck")
        self.assertTrue(any(issue["rule"] == "misra-c2012-15.1" and issue["severity"] == "error" and issue["file"] == "a.c" and issue["line"] == 3 for issue in check["issues"]), check)
        self.assertEqual(check["metrics"]["misra_c2012"]["coverage"], "partial")
        self.assertIsNone(check["metrics"]["misra_c2012"]["rule_texts"])
        self.assertFalse(list(self.root.rglob("*.dump")))

    def test_default_does_not_enable_misra(self):
        code, document = self.scan()
        self.assertEqual(code, 0, document)
        self.assertFalse(any(issue["rule"].startswith("misra-") for check in document["checks"] for issue in check["issues"]))

    def test_project_configuration_can_enable_misra(self):
        self.config["static"] = {"misra_c2012": True}
        self.save_config()
        self.assertEqual(self.scan()[0], 1)

    def test_valid_case_runs_the_addon_without_findings(self):
        (self.root / "a.c").write_text(GOOD, encoding="utf-8")
        code, document = self.scan("--misra-c2012")
        self.assertEqual(code, 0, document)
        check = next(check for check in document["checks"] if check["id"] == "cppcheck")
        self.assertEqual(check["metrics"]["misra_c2012"]["c_translation_units"], ["a.c"])
        self.assertFalse(check["issues"])

    def test_explicit_missing_rule_texts_are_an_execution_error(self):
        with patch.dict(os.environ, {"OPENEMBEDDLINT_MISRA_RULE_TEXTS": str(self.root / "missing.txt")}):
            code, document = self.scan("--misra-c2012")
        self.assertEqual(code, 2, document)
        self.assertFalse(document["summary"]["complete"])
        self.assertTrue(any("OPENEMBEDDLINT_MISRA_RULE_TEXTS" in check.get("reason", "") for check in document["checks"]))

    def test_private_texts_support_unicode_and_spaces(self):
        directory = Path(self.temporary.name) / "技能副本 with spaces"
        texts = directory / "rule-texts.txt"
        texts.parent.mkdir(parents=True)
        texts.write_text("Appendix A Summary of guidelines\nRule 15.1 Advisory\nPrefer structured control flow instead of goto.\n", encoding="utf-8")
        with patch.dict(os.environ, {"OPENEMBEDDLINT_MISRA_RULE_TEXTS": str(texts)}):
            code, document = self.scan("--misra-c2012")
        self.assertEqual(code, 1, document)
        issues = [issue for check in document["checks"] for issue in check["issues"] if issue["rule"] == "misra-c2012-15.1"]
        self.assertEqual(len(issues), 1)
        self.assertIn("structured control flow", issues[0]["message"])
        self.assertEqual(issues[0]["severity"], "error")
        staged = Path(document["report_path"]).parent / "logs/misra-rule-texts.txt"
        self.assertEqual(staged.read_bytes(), texts.read_bytes())

    def test_private_text_changes_invalidate_incremental_verification(self):
        directory = Path(self.temporary.name) / "skill copy"
        texts = directory / "rule-texts.txt"
        texts.parent.mkdir(parents=True)
        texts.write_text("Appendix A Summary of guidelines\nRule 15.1 Advisory\nPrefer structured control flow instead of goto.\n", encoding="utf-8")
        (self.root / "a.c").write_text(GOOD, encoding="utf-8")
        (self.root / ".gitignore").write_text("compile_commands.json\n", encoding="utf-8")
        with patch.dict(os.environ, {"GIT_TRACE2_EVENT": "0"}):
            git(self.root, "init", "--quiet")
            git(self.root, "add", ".")
            git(self.root, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--quiet", "-m", "fixture")
            def changing_scan(*args):
                result = project_report(*args)
                texts.write_bytes(texts.read_bytes() + b"\n")
                return result
            with patch.dict(os.environ, {"OPENEMBEDDLINT_MISRA_RULE_TEXTS": str(texts)}), patch("cverify.cli.project_report", side_effect=changing_scan):
                code, document = self.scan("--misra-c2012", "--baseline", "HEAD")
        self.assertEqual(code, 3, document)
        self.assertFalse(document["gate"]["complete"])
        self.assertTrue(any("changed during verification" in gap for gap in document["gate"]["gaps"]), document)

    def test_project_suppression_is_scoped_and_auditable(self):
        self.config["static"] = {"misra_c2012": True}
        self.config["suppressions"] = [{"check": "misra-c2012-15.1", "path": "a.c", "reason": "Isolated regression for an explicit project suppression"}]
        self.save_config()
        code, document = self.scan()
        self.assertEqual(code, 0, document)
        self.assertEqual(document["summary"]["suppressed"], 1)
        self.config["suppressions"][0]["path"] = "other.c"
        self.save_config()
        self.assertEqual(self.scan()[0], 1)

    def test_relative_include_paths_keep_the_project_context(self):
        include = self.root / "include"
        include.mkdir()
        (include / "project_header.h").write_text("#ifndef PROJECT_HEADER_H\n#define PROJECT_HEADER_H\n#ifndef PROJECT_TARGET\n#error missing project target\n#endif\nstatic inline int value(void)\n{\n    goto done;\ndone:\n    return 0;\n}\n#endif\n", encoding="utf-8")
        (self.root / "a.c").write_text('#include "project_header.h"\nint main(void) { return value(); }\n', encoding="utf-8")
        self.config["static"] = {"misra_c2012": True, "cppcheck_defines": ["PROJECT_TARGET=1"], "cppcheck_includes": ["include"]}
        self.save_config()
        code, document = self.scan()
        self.assertEqual(code, 1, document)
        self.assertTrue(any(issue["rule"] == "misra-c2012-15.1" and issue["file"] == "include/project_header.h" for check in document["checks"] for issue in check["issues"]), document)

    def test_requested_misra_cannot_be_skipped(self):
        code, document = self.scan("--misra-c2012", "--skip-cppcheck")
        self.assertEqual((code, document["status"]), (2, "ERROR"))

    def test_misra_is_not_silently_ignored_by_fuzz_profile(self):
        code, document = self.scan("--misra-c2012", "--profile", "fuzz")
        self.assertEqual((code, document["status"]), (2, "ERROR"))

    def test_missing_addon_is_not_a_clean_result(self):
        executable = self.toolchain.require("cppcheck")
        with patch.object(self.toolchain, "root", Path(self.temporary.name) / "empty-tools"), patch.object(self.toolchain, "require", return_value=executable):
            result = run_cppcheck([self.root / "a.c"], self.root, self.toolchain, {"misra_c2012": True}, 30, self.output, True)
        self.assertEqual(result.status, "ERROR")
        self.assertIn("addon not found", result.reason)

    def test_addon_crash_is_an_execution_error(self):
        executable = self.toolchain.require("cppcheck")
        environment = self.toolchain.environment()
        private = (Path(self.temporary.name) / "broken-tools").resolve()
        addon = private / "Library/bin/addons/misra.py"
        addon.parent.mkdir(parents=True)
        addon.write_text('raise RuntimeError("broken addon")\n', encoding="utf-8")
        with patch.object(self.toolchain, "root", private), patch.object(self.toolchain, "require", return_value=executable), patch.object(self.toolchain, "environment", return_value=environment):
            result = run_cppcheck([self.root / "a.c"], self.root, self.toolchain, {"misra_c2012": True}, 30, self.output, True)
        self.assertEqual(result.status, "ERROR", result.to_dict())
        self.assertTrue(any(issue.rule == "internalError" for issue in result.issues), result.to_dict())

    def test_incremental_gate_distinguishes_legacy_and_new_misra(self):
        with patch.dict(os.environ, {"GIT_TRACE2_EVENT": "0"}):
            (self.root / ".gitignore").write_text("compile_commands.json\n", encoding="utf-8")
            git(self.root, "init", "--quiet")
            git(self.root, "add", ".")
            git(self.root, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--quiet", "-m", "fixture")
            code, document = self.scan("--misra-c2012", "--baseline", "HEAD")
            self.assertEqual(code, 0, document)
            self.assertEqual(document["gate"]["counts"]["existing"], 1)
            (self.root / "a.c").write_text(GOOD, encoding="utf-8")
            git(self.root, "add", "a.c")
            git(self.root, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--quiet", "-m", "clean baseline")
            (self.root / "a.c").write_text(BAD, encoding="utf-8")
            code, document = self.scan("--misra-c2012", "--baseline", "HEAD")
            self.assertEqual(code, 1, document)
            self.assertTrue(any(record["issue"]["rule"] == "misra-c2012-15.1" and record["classification"] == "new" for record in document["gate"]["issues"]))


if __name__ == "__main__":
    unittest.main()
