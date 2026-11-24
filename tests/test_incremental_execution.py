import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from cverify.compilation import expand_response_arguments, split_compiler_command
from cverify.cli import project_report
from cverify.execution import Toolchain, run_process
from cverify.incremental.context import remap_database
from cverify.incremental.snapshots import capture, git, index_entries
from cverify.report import render
from test_integration import invoke


SKILL_DIR = Path(__file__).resolve().parents[1]
SOURCE = "int f(void)\n{\n    return 23;\n}\n"


class IncrementalExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cverify gate ")
        environment = patch.dict(os.environ, {"GIT_TRACE2_EVENT": "0", "OPENEMBEDDLINT_CACHE_DIR": str(Path(self.temp.name) / "cverify-cache")})
        environment.start()
        self.addCleanup(environment.stop)
        self.root = Path(self.temp.name) / "project space"
        self.root.mkdir()
        self.output = Path(self.temp.name) / "reports"
        git(self.root, "init", "--quiet")
        (self.root / "a.c").write_text(SOURCE, encoding="utf-8")
        (self.root / ".gitignore").write_text("compile_commands.json\nbuild/\n", encoding="utf-8")
        self.policy = {"Checks": "-*,readability-magic-numbers", "WarningsAsErrors": "*", "CheckOptions": {"readability-magic-numbers.IgnoredIntegerValues": "0;1", "readability-magic-numbers.IgnorePowersOf2IntegerValues": "false"}}
        (self.root / ".clang-tidy").write_text(json.dumps(self.policy), encoding="utf-8")
        (self.root / ".c-verify.json").write_text(json.dumps({"schema_version": 2, "format": {"enabled": False}, "cyclomatic": {"enabled": False}}), encoding="utf-8")
        self.commit()
        self.database()

    def tearDown(self):
        self.temp.cleanup()

    def commit(self):
        git(self.root, "add", ".")
        git(self.root, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--quiet", "-m", "fixture")

    def database(self, names=("a.c",)):
        compiler = Toolchain(SKILL_DIR).require("clang")
        entries = [{"directory": str(self.root), "file": name, "arguments": [str(compiler), "-std=c11", "-c", name]} for name in names]
        (self.root / "compile_commands.json").write_text(json.dumps(entries), encoding="utf-8")

    def run_gate(self, *arguments):
        return invoke(["--project-root", str(self.root), "--output-dir", str(self.output), "--skip-cppcheck", *arguments])

    def test_existing_error_passes_gate_but_remains_in_full_report(self):
        code, document = self.run_gate("-f", "a.c", "--baseline", "HEAD", "--detail")
        self.assertEqual(code, 0, document)
        self.assertEqual(document["summary"]["status"], "FAIL")
        self.assertEqual(document["gate"]["counts"]["existing"], 1)
        self.assertEqual(document["gate"]["status"], "PASS")
        self.assertTrue(Path(document["gate"]["baseline_report"]).is_file())
        self.assertTrue(Path(document["gate"]["source_manifest"]).is_file())
        compact = json.loads(render(document, "json", False))
        self.assertEqual((compact["status"], compact["scan_status"]), ("PASS", "FAIL"))
        self.assertEqual(compact["findings"], [])
        self.assertIn("Gate PASS", render(document, "text", False))
        self.assertIn("Gate PASS", render(document, "markdown", True))

    def test_internal_output_is_rejected_before_allocation_or_index_changes(self):
        destination = self.root / "reports"
        before = (self.root / ".git" / "index").read_bytes()
        code, document = self.run_gate("--baseline", "HEAD", "-f", "a.c", "--output-dir", str(destination))
        self.assertEqual(code, 2, document)
        self.assertIn("outside", document["reason"])
        self.assertFalse(destination.exists())
        self.assertEqual((self.root / ".git" / "index").read_bytes(), before)

    def test_new_error_is_not_hidden_by_existing_errors(self):
        (self.root / "a.c").write_text(SOURCE + "int g(void) { return 24; }\n", encoding="utf-8")
        code, document = self.run_gate("--git-modified")
        self.assertEqual(code, 1, document)
        self.assertEqual(document["gate"]["counts"]["new"], 1)
        self.assertEqual(document["gate"]["counts"]["existing"], 1)
        self.assertEqual(len(document["findings"]), 1)
        self.assertIn("24", document["findings"][0]["message"])

    def test_staged_scan_uses_index_content_and_does_not_modify_repository(self):
        source = self.root / "a.c"
        source.write_text(SOURCE.replace("23", "24"), encoding="utf-8")
        git(self.root, "add", "a.c")
        source.write_text(SOURCE.replace("23", "25"), encoding="utf-8")
        original_index = (self.root / ".git" / "index").read_bytes()
        original_source = source.read_bytes()
        code, document = self.run_gate("--git-staged")
        self.assertEqual(code, 1, document)
        self.assertEqual(document["gate"]["snapshot"], "index")
        self.assertTrue(any("24" in issue["message"] for issue in document["findings"]))
        self.assertFalse(any("25" in issue["message"] for issue in document["findings"]))
        self.assertEqual((self.root / ".git" / "index").read_bytes(), original_index)
        self.assertEqual(source.read_bytes(), original_source)

    def test_staged_policy_is_not_replaced_by_unstaged_policy(self):
        (self.root / "a.c").write_text(SOURCE.replace("23", "24"), encoding="utf-8")
        git(self.root, "add", "a.c")
        (self.root / ".clang-tidy").write_text('{"Checks":"-*,clang-diagnostic-*"}', encoding="utf-8")
        code, document = self.run_gate("--git-staged")
        self.assertEqual(code, 1, document)
        self.assertIn("readability-magic-numbers", str(document["findings"]))

    def test_baseline_flow_schema_is_not_executed_or_required_for_adoption(self):
        path = self.root / ".c-verify.json"
        current = path.read_text(encoding="utf-8")
        path.write_text('{"schema_version":1,"readability":{"removed":true}}', encoding="utf-8")
        self.commit()
        path.write_text(current, encoding="utf-8")
        code, document = self.run_gate("--baseline", "HEAD", "-f", "a.c")
        self.assertEqual(code, 0, document)
        self.assertEqual(document["gate"]["counts"]["existing"], 1)

    def test_header_only_change_is_checked_through_including_unit(self):
        (self.root / "domain.h").write_text("static inline int value(void) { return 1; }\n", encoding="utf-8")
        (self.root / "a.c").write_text('#include "domain.h"\nint f(void) { return value(); }\n', encoding="utf-8")
        self.commit()
        (self.root / "domain.h").write_text("static inline int value(void) { return 24; }\n", encoding="utf-8")
        code, document = self.run_gate("--git-modified", "--detail")
        self.assertEqual(code, 1, document)
        self.assertIn("a.c", document["gate"]["scope"])
        self.assertTrue(any(record["issue"]["file"] == "domain.h" for record in document["gate"]["issues"]))

    def test_header_without_including_unit_is_incomplete(self):
        (self.root / "orphan.h").write_text("enum { ITEM = 23 };\n", encoding="utf-8")
        code, document = self.run_gate("--git-all")
        self.assertEqual(code, 3, document)
        self.assertEqual(document["status"], "INCOMPLETE")

    def test_missing_compilation_context_cannot_pass(self):
        (self.root / "compile_commands.json").unlink()
        code, document = self.run_gate("-f", "a.c", "--baseline", "HEAD")
        self.assertEqual((code, document["status"]), (2, "ERROR"))

    def test_changed_build_input_is_not_assumed_equivalent(self):
        (self.root / "CMakeLists.txt").write_text("project(sample C)\n", encoding="utf-8")
        self.commit()
        (self.root / "CMakeLists.txt").write_text("project(changed C)\n", encoding="utf-8")
        code, document = self.run_gate("-f", "a.c", "--baseline", "HEAD")
        self.assertEqual(code, 3, document)
        self.assertTrue(any("non-source input" in gap for gap in document["gap_summary"]))

    def test_pure_rename_keeps_existing_violation(self):
        (self.root / "a.c").rename(self.root / "renamed.c")
        git(self.root, "add", "-A")
        self.database(("renamed.c",))
        code, document = self.run_gate("--git-staged")
        self.assertEqual(code, 0, document)
        self.assertEqual(document["gate"]["counts"]["existing"], 1)

    def test_new_untracked_source_is_selected_only_by_git_all(self):
        (self.root / "new.c").write_text("int g(void) { return 24; }\n", encoding="utf-8")
        self.database(("a.c", "new.c"))
        code, document = self.run_gate("--git-modified", "--allow-empty")
        self.assertEqual(code, 0, document)
        self.assertEqual(document["gate"]["counts"]["new"], 0)
        code, document = self.run_gate("--git-all")
        self.assertEqual(code, 1, document)
        self.assertEqual(document["gate"]["counts"]["new"], 1)

    def test_cognitive_increase_at_unchanged_declaration_fails(self):
        source = "int f(int x)\n{\n    if (x) return x;\n    if (x < 0) return -x;\n    return 0;\n}\n"
        (self.root / "a.c").write_text(source, encoding="utf-8")
        self.commit()
        policy = {"Checks": "-*,readability-function-cognitive-complexity", "WarningsAsErrors": "*", "CheckOptions": {"readability-function-cognitive-complexity.Threshold": "1"}}
        (self.root / ".clang-tidy").write_text(json.dumps(policy), encoding="utf-8")
        (self.root / "a.c").write_text(source.replace("if (x) return x;", "if (x) { if (x > 0) return x; }"), encoding="utf-8")
        code, document = self.run_gate("--git-modified")
        self.assertEqual(code, 1, document)
        self.assertEqual(document["gate"]["counts"]["worsened"], 1)

    def test_function_size_compares_real_numeric_notes(self):
        source = "int f(void)\n{\n    int x = 0;\n    x++;\n    return x;\n}\n"
        (self.root / "a.c").write_text(source, encoding="utf-8")
        self.commit()
        policy = {"Checks": "-*,readability-function-size", "WarningsAsErrors": "*", "CheckOptions": {"readability-function-size.StatementThreshold": "1"}}
        (self.root / ".clang-tidy").write_text(json.dumps(policy), encoding="utf-8")
        (self.root / "a.c").write_text(source.replace("    x++;", "    x++;\n    x++;"), encoding="utf-8")
        code, document = self.run_gate("--git-modified", "--detail")
        self.assertEqual(code, 1, document)
        self.assertEqual(document["gate"]["counts"]["worsened"], 1)
        self.assertTrue(document["gate"]["issues"][0]["issue"]["notes"])

    def test_concurrent_worktree_change_invalidates_the_gate(self):
        def changing_scan(*args):
            result = project_report(*args)
            (self.root / "a.c").write_text(SOURCE.replace("23", "25"), encoding="utf-8")
            return result
        with patch("cverify.cli.project_report", side_effect=changing_scan):
            code, document = self.run_gate("--baseline", "HEAD", "-f", "a.c")
        self.assertEqual(code, 3, document)
        self.assertTrue(any("worktree inputs changed" in gap for gap in document["gap_summary"]))

    def test_project_rule_failure_is_never_waived(self):
        (self.root / "checker.py").write_text('import json\nprint(json.dumps({"issues":[{"file":"a.c","line":3,"rule":"project-example","message":"current violation","severity":"error"}]}))\n', encoding="utf-8")
        configuration = json.loads((self.root / ".c-verify.json").read_text())
        configuration["commands"] = [{"id": "project-lint", "kind": "project_rule", "argv": ["{python}", "{project_root}/checker.py"]}]
        (self.root / ".c-verify.json").write_text(json.dumps(configuration), encoding="utf-8")
        self.commit()
        code, document = self.run_gate("--baseline", "HEAD", "-f", "a.c")
        self.assertEqual(code, 1, document)
        self.assertEqual(document["gate"]["required_failures"], ["project-lint"])

    def test_full_profile_does_not_waive_missing_runtime_evidence(self):
        code, document = self.run_gate("--baseline", "HEAD", "-f", "a.c", "--profile", "full")
        self.assertEqual(code, 3, document)
        self.assertFalse(document["complete"])

    def test_directory_root_and_absolute_file_selectors_work(self):
        for selection in (("-d", "."), ("-f", str(self.root / "a.c"))):
            code, document = self.run_gate("--baseline", "HEAD", *selection)
            self.assertEqual(code, 0, document)

    def test_invalid_database_is_a_structured_error(self):
        (self.root / "compile_commands.json").write_text('{"bad":"shape"}', encoding="utf-8")
        code, document = self.run_gate("--baseline", "HEAD", "-f", "a.c")
        self.assertEqual((code, document["status"]), (2, "ERROR"))
        self.assertIn("compilation database", document["reason"])

    def test_cppcheck_partial_context_cannot_pass_when_tidy_is_disabled(self):
        (self.root / "b.c").write_text("int g(void) { return 0; }\n", encoding="utf-8")
        code, document = invoke(["--project-root", str(self.root), "-f", "a.c", "-f", "b.c", "--skip-clang-tidy", "--detail", "--output-dir", str(self.output)])
        check = next(check for check in document["checks"] if check["id"] == "cppcheck")
        self.assertEqual((code, check["status"], document["summary"]["complete"]), (2, "ERROR", False))

    def test_cross_compiler_name_preserves_target_in_dependency_queries(self):
        source = '#if !defined(__arm__) || !defined(__ARM_ARCH_7EM__)\n#error wrong compilation target\n#endif\n' + SOURCE
        (self.root / "a.c").write_text(source, encoding="utf-8")
        self.commit()
        entries = [{"directory": str(self.root), "file": "a.c", "arguments": ["arm-none-eabi-gcc", "-mcpu=cortex-m4", "-mthumb", "-c", "a.c"]}]
        (self.root / "compile_commands.json").write_text(json.dumps(entries), encoding="utf-8")
        code, document = self.run_gate("--baseline", "HEAD", "-f", "a.c")
        self.assertEqual(code, 0, document)
        self.assertEqual(document["gate"]["counts"]["existing"], 1)

    def test_all_analyzers_use_the_remapped_compilation_context(self):
        source = "int values[1];\nint f(void) { return values[0]; }\n"
        (self.root / "a.c").write_text(source, encoding="utf-8")
        configuration = json.loads((self.root / ".c-verify.json").read_text())
        configuration["cyclomatic"]["enabled"] = True
        (self.root / ".c-verify.json").write_text(json.dumps(configuration), encoding="utf-8")
        self.commit()
        (self.root / "a.c").write_text(source.replace("values[0]", "values[2]"), encoding="utf-8")
        code, document = invoke(["--project-root", str(self.root), "--git-modified", "--detail", "--output-dir", str(self.output)])
        self.assertEqual(code, 1, document)
        statuses = {check["id"]: check["status"] for check in document["checks"]}
        self.assertEqual(statuses["cyclomatic-complexity"], "PASS")
        self.assertEqual(statuses["cppcheck"], "FAIL")
        self.assertTrue(any(record["issue"]["rule"] == "arrayIndexOutOfBounds" and record["classification"] == "new" for record in document["gate"]["issues"]))

    def test_json_cli_and_child_output_remain_utf8_under_a_legacy_locale(self):
        process = run_process([sys.executable, str(SKILL_DIR / "scripts/c_verify.py"), "--project-root", str(self.root), "-f", "missing-中文-😀.c"], cwd=self.root, timeout_s=30, extra_env={"PYTHONIOENCODING": "gbk"})
        self.assertEqual(process.exit_code, 2, process.stderr)
        self.assertIn("中文-😀", json.loads(process.stdout)["reason"])
        self.assertNotIn("\ufffd", process.stdout)
        process = run_process([sys.executable, "-c", 'print("中文😀")'], cwd=self.root, timeout_s=30, extra_env={"PYTHONIOENCODING": "gbk"})
        self.assertEqual(process.stdout.strip(), "中文😀")

    def test_distinct_compilation_configurations_are_not_silently_merged(self):
        database = self.root / "compile_commands.json"
        entries = json.loads(database.read_text())
        other = {**entries[0], "arguments": [*entries[0]["arguments"], "-DMODE=1"]}
        database.write_text(json.dumps([*entries, other]), encoding="utf-8")
        code, document = self.run_gate("--baseline", "HEAD", "-f", "a.c")
        self.assertEqual(code, 2, document)
        self.assertIn("multiple compilation configurations", document["reason"])
        self.assertIn("--compdb", document["reason"])
        self.assertIn("preserve distinct target configurations", document["reason"])

    def test_unselected_multi_target_sources_do_not_block_the_selected_scope(self):
        (self.root / "other.c").write_text("int other(void) { return 1; }\n", encoding="utf-8")
        self.commit()
        self.database(("a.c", "other.c"))
        database = self.root / "compile_commands.json"
        entries = json.loads(database.read_text())
        entries.append({**entries[1], "arguments": [*entries[1]["arguments"], "-DOTHER_TARGET=1"]})
        database.write_text(json.dumps(entries), encoding="utf-8")
        code, document = self.run_gate("--baseline", "HEAD", "-f", "a.c", "--detail")
        self.assertEqual(code, 0, document)
        remapped = json.loads(Path(document["compdb"]).read_text())
        self.assertEqual(sum(Path(entry["file"]).name == "other.c" for entry in remapped), 2)

    def test_header_expansion_keeps_selected_multi_target_contexts_explicit(self):
        header = self.root / "domain.h"
        header.write_text("#define DOMAIN_VALUE 1\n", encoding="utf-8")
        (self.root / "other.c").write_text('#include "domain.h"\nint other(void) { return DOMAIN_VALUE; }\n', encoding="utf-8")
        self.commit()
        self.database(("a.c", "other.c"))
        database = self.root / "compile_commands.json"
        entries = json.loads(database.read_text())
        entries.append({**entries[1], "arguments": [*entries[1]["arguments"], "-DOTHER_TARGET=1"]})
        database.write_text(json.dumps(entries), encoding="utf-8")
        header.write_text("#define DOMAIN_VALUE 23\n", encoding="utf-8")
        code, document = self.run_gate("--git-modified")
        self.assertEqual(code, 2, document)
        self.assertIn("selected sources", document["reason"])

    def test_response_change_during_analysis_invalidates_the_gate(self):
        response = self.root / "build" / "options.rsp"
        response.parent.mkdir()
        response.write_text("-std=c11 -c a.c", encoding="utf-8")
        database = self.root / "compile_commands.json"
        entries = json.loads(database.read_text())
        entries[0]["arguments"] = [entries[0]["arguments"][0], "@build/options.rsp"]
        database.write_text(json.dumps(entries), encoding="utf-8")
        def changing_scan(*args):
            result = project_report(*args)
            response.write_text("-std=c11 -DCHANGED=1 -c a.c", encoding="utf-8")
            return result
        with patch("cverify.cli.project_report", side_effect=changing_scan):
            code, document = self.run_gate("--baseline", "HEAD", "-f", "a.c")
        self.assertEqual(code, 3, document)
        self.assertTrue(any("response file" in gap for gap in document["gap_summary"]))

    def test_snapshot_mutation_invalidates_its_static_evidence(self):
        def changing_scan(*args):
            result = project_report(*args)
            (args[1] / "a.c").write_text(SOURCE.replace("23", "25"), encoding="utf-8")
            return result
        with patch("cverify.cli.project_report", side_effect=changing_scan):
            code, document = self.run_gate("--baseline", "HEAD", "-f", "a.c")
        self.assertEqual(code, 3, document)
        self.assertTrue(any("modified snapshot" in gap for gap in document["gap_summary"]))
        self.assertEqual((self.root / "a.c").read_text(), SOURCE)

    def test_unstaged_build_inputs_cannot_supply_the_staged_context(self):
        build = self.root / "CMakeLists.txt"
        build.write_text("project(sample C)\n", encoding="utf-8")
        self.commit()
        (self.root / "a.c").write_text(SOURCE.replace("23", "24"), encoding="utf-8")
        git(self.root, "add", "a.c")
        build.write_text("project(unstaged C)\n", encoding="utf-8")
        code, document = self.run_gate("--git-staged")
        self.assertEqual(code, 3, document)
        self.assertTrue(any("non-source input" in gap for gap in document["gap_summary"]))

    def test_external_policy_inheritance_is_not_silently_lost(self):
        (self.root.parent / ".clang-tidy").write_text('{"Checks":"-*,readability-magic-numbers"}', encoding="utf-8")
        policy = {**self.policy, "InheritParentConfig": True}
        (self.root / ".clang-tidy").write_text(json.dumps(policy), encoding="utf-8")
        code, document = self.run_gate("--baseline", "HEAD", "-f", "a.c")
        self.assertEqual(code, 3, document)
        self.assertTrue(any("inheritance outside" in gap for gap in document["gap_summary"]))

    def test_snapshots_reject_unmerged_and_submodule_entries(self):
        for entry in (b"100644 123 1\ta.c\0", b"160000 123 0\tmodule\0"):
            with self.assertRaises(ValueError):
                index_entries(entry)

    def test_snapshot_uses_isolated_index(self):
        before = (self.root / ".git" / "index").read_bytes()
        destination = Path(self.temp.name) / "snapshot"
        destination.mkdir()
        old, new, _, _, identity = capture(self.root, "HEAD", True, destination)
        self.assertEqual((old / "a.c").read_text(), (new / "a.c").read_text())
        self.assertEqual(identity["snapshot"], "index")
        self.assertEqual((self.root / ".git" / "index").read_bytes(), before)

    def test_response_paths_and_quoted_defines_survive_snapshot_remapping(self):
        response = self.root / "options.rsp"
        response.write_text('-DNAME="two words" -I. -c a.c', encoding="utf-8")
        snapshot = Path(self.temp.name) / "snapshot"
        snapshot.mkdir()
        shutil.copyfile(self.root / "a.c", snapshot / "a.c")
        database = self.root / "compile_commands.json"
        database.write_text(json.dumps([{"directory": str(self.root), "file": "a.c", "arguments": ["compiler", "@options.rsp"]}]), encoding="utf-8")
        mapped = remap_database(database, self.root, snapshot, Path(self.temp.name) / "mapped.json", snapshot)
        arguments = json.loads(mapped.read_text())[0]["arguments"]
        self.assertIn("-DNAME=two words", arguments)
        self.assertIn("-I" + snapshot.as_posix(), arguments)
        self.assertIn((snapshot / "a.c").as_posix(), arguments)
        self.assertEqual(split_compiler_command('"path with space/compiler" -DNAME="two words"')[-1], "-DNAME=two words")
        self.assertEqual(expand_response_arguments(["@options.rsp"], self.root)[0], "-DNAME=two words")

    def test_dependency_discovery_ignores_werror_and_gcc_codegen_flags(self):
        (self.root / "domain.h").write_text("static inline int val(void) { return 1; }\n", encoding="utf-8")
        (self.root / "a.c").write_text('#include "domain.h"\nint f(void) { return val(); }\n', encoding="utf-8")
        self.commit()
        compiler = Toolchain(SKILL_DIR).require("clang")
        entries = [{
            "directory": str(self.root),
            "file": "a.c",
            "arguments": [str(compiler), "--target=arm-none-eabi", "-mcpu=cortex-m4", "-mthumb", "-Werror", "-Wall", "-O2", "-flto", "-c", "a.c"],
        }]
        (self.root / "compile_commands.json").write_text(json.dumps(entries), encoding="utf-8")
        (self.root / "domain.h").write_text("static inline int val(void) { return 24; }\n", encoding="utf-8")
        code, document = self.run_gate("--git-modified", "--detail")
        self.assertEqual(code, 1, document)
        self.assertEqual(document["gate"]["counts"]["new"], 1)

    def test_non_build_non_source_change_is_advisory_while_build_inputs_block(self):
        (self.root / "tools").mkdir()
        (self.root / "tools" / "check.py").write_text("print('v1')\n", encoding="utf-8")
        self.commit()
        (self.root / "tools" / "check.py").write_text("print('v2')\n", encoding="utf-8")
        code, document = self.run_gate("-f", "a.c", "--baseline", "HEAD", "--detail")
        self.assertEqual(code, 0, document)
        self.assertIn("tools/check.py", document["gate"]["advisory_non_source_changes"])

    def test_baseline_cache_reuses_unchanged_baseline_and_respects_no_cache(self):
        cache_dir = Path(self.temp.name) / "cache"
        with patch.dict(os.environ, {"OPENEMBEDDLINT_CACHE_DIR": str(cache_dir)}):
            code1, doc1 = self.run_gate("-f", "a.c", "--baseline", "HEAD", "--detail")
            self.assertEqual(code1, 0, doc1)
            self.assertNotIn("baseline_cached", doc1["gate"])
            code2, doc2 = self.run_gate("-f", "a.c", "--baseline", "HEAD", "--detail")
            self.assertEqual(code2, 0, doc2)
            self.assertTrue(doc2["gate"].get("baseline_cached"))
            self.assertTrue(Path(doc2["gate"]["baseline_report"]).is_file())
            code3, doc3 = self.run_gate("-f", "a.c", "--baseline", "HEAD", "--no-cache", "--detail")
            self.assertEqual(code3, 0, doc3)
            self.assertNotIn("baseline_cached", doc3["gate"])

    def test_multifile_selector_and_parallel_jobs_are_deterministic(self):
        (self.root / "b.c").write_text("int g(void) { return 0; }\n", encoding="utf-8")
        self.commit()
        self.database(("a.c", "b.c"))
        code1, doc1 = self.run_gate("-f", "a.c", "b.c", "--baseline", "HEAD", "--jobs", "1", "--no-cache", "--detail")
        code2, doc2 = self.run_gate("-f", "a.c", "b.c", "--baseline", "HEAD", "--jobs", "2", "--no-cache", "--detail")
        self.assertEqual((code1, code2), (0, 0))
        self.assertEqual(doc1["selected_files"], ["a.c", "b.c"])
        self.assertEqual(doc1["gate"]["counts"], doc2["gate"]["counts"])


if __name__ == "__main__":
    unittest.main()
