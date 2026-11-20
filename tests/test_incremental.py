import copy
import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from cverify.incremental.attribution import compare_reports


def diagnostic(line, message="23 is a magic number", rule="readability-magic-numbers", **extra):
    return {"file": "a.c", "line": line, "column": 5, "rule": rule, "message": message, "severity": "error", "notes": [], **extra}


def report(issues, status=None, **extra):
    return {"summary": {"status": status or ("FAIL" if issues else "PASS"), "complete": status != "ERROR"}, "checks": [{"id": "clang-tidy", "status": status or ("FAIL" if issues else "PASS"), "required": True, "issues": issues, "metrics": {}, **extra}]}


class AttributionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name) / "base"
        self.head = Path(self.temp.name) / "head"
        self.base.mkdir()
        self.head.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def compare(self, old, new, old_source="int f(void) { return 23; }\n", new_source=None, **kwargs):
        (self.base / "a.c").write_text(old_source)
        (self.head / "a.c").write_text(new_source if new_source is not None else old_source)
        return compare_reports(old, new, self.base, self.head, **kwargs)

    def test_unchanged_legacy_error_does_not_fail_the_gate(self):
        gate = self.compare(report([diagnostic(1)]), report([diagnostic(1)]))
        self.assertEqual((gate["status"], gate["counts"]["existing"]), ("PASS", 1))

    def test_inserted_lines_do_not_turn_old_errors_into_new_errors(self):
        gate = self.compare(report([diagnostic(1)]), report([diagnostic(2)]), new_source="\nint f(void) { return 23; }\n")
        self.assertEqual((gate["status"], gate["counts"]["existing"]), ("PASS", 1))

    def test_copying_an_old_violation_adds_a_new_violation(self):
        gate = self.compare(report([diagnostic(1)]), report([diagnostic(1), diagnostic(2)]), new_source="int f(void) { return 23; }\nint g(void) { return 23; }\n")
        self.assertEqual((gate["status"], gate["counts"]["new"]), ("FAIL", 1))

    def test_equal_total_count_does_not_hide_a_replacement(self):
        gate = self.compare(report([diagnostic(1)]), report([diagnostic(1, "24 is a magic number")]), new_source="int f(void) { return 24; }\n")
        self.assertEqual((gate["status"], gate["counts"]["new"]), ("FAIL", 1))

    def test_changed_line_with_indistinguishable_old_error_is_unattributed(self):
        gate = self.compare(report([diagnostic(1)]), report([diagnostic(1)]), new_source="int f(int x) { return x + 23; }\n")
        self.assertEqual((gate["status"], gate["counts"]["unattributed"]), ("INCOMPLETE", 1))

    def test_cognitive_complexity_is_compared_even_at_unchanged_declaration(self):
        rule = "readability-function-cognitive-complexity"
        old = report([diagnostic(1, "function 'f' has cognitive complexity of 29 (threshold 25)", rule)])
        new = report([diagnostic(1, "function 'f' has cognitive complexity of 35 (threshold 25)", rule)])
        gate = self.compare(old, new, "int f(int x) {\n return x;\n}\n", "int f(int x) {\n if (x) return x;\n return 0;\n}\n")
        self.assertEqual((gate["status"], gate["counts"]["worsened"]), ("FAIL", 1))

    def test_lower_complexity_remains_a_visible_existing_error(self):
        rule = "readability-function-cognitive-complexity"
        old = report([diagnostic(1, "function 'f' has cognitive complexity of 35 (threshold 25)", rule)])
        new = report([diagnostic(1, "function 'f' has cognitive complexity of 29 (threshold 25)", rule)])
        self.assertEqual(self.compare(old, new)["status"], "PASS")

    def test_function_size_uses_notes_instead_of_identical_headline(self):
        old_issue = diagnostic(1, "function 'f' exceeds recommended size/complexity thresholds", "readability-function-size", notes=[{"message": "101 statements (threshold 100)"}])
        new_issue = copy.deepcopy(old_issue)
        new_issue["notes"][0]["message"] = "110 statements (threshold 100)"
        gate = self.compare(report([old_issue]), report([new_issue]))
        self.assertEqual(gate["counts"]["worsened"], 1)

    def test_function_size_compares_nesting_and_ignores_context_notes(self):
        old = diagnostic(1, "function 'f' exceeds recommended size/complexity thresholds", "readability-function-size", notes=[
            {"message": "nesting level 6 starts here (threshold 5)"},
            {"message": "related context"},
        ])
        new = copy.deepcopy(old)
        new["notes"][0]["message"] = "nesting level 7 starts here (threshold 5)"
        gate = self.compare(report([old]), report([new]))
        self.assertEqual((gate["status"], gate["counts"]["worsened"]), ("FAIL", 1))

    def test_execution_error_never_becomes_a_passing_gate(self):
        self.assertEqual(self.compare(report([], "ERROR"), report([]))["status"], "INCOMPLETE")

    def test_unlocated_error_requires_review(self):
        issue = diagnostic(None, file=None)
        self.assertEqual(self.compare(report([issue]), report([issue]))["status"], "INCOMPLETE")

    def test_report_comparison_does_not_mutate_raw_evidence(self):
        old = report([diagnostic(1)])
        new = copy.deepcopy(old)
        self.compare(old, new)
        self.assertEqual(old, new)

    def test_shifted_diagnostic_trace_does_not_look_like_a_new_error(self):
        old = diagnostic(1, notes=[{"file": "a.c", "line": 1, "column": 5, "message": "value originated here"}])
        new = diagnostic(2, notes=[{"file": "a.c", "line": 2, "column": 5, "message": "value originated here"}])
        gate = self.compare(report([old]), report([new]), new_source="\nint f(void) { return 23; }\n")
        self.assertEqual(gate["status"], "PASS")

    def test_column_drift_requires_same_line_and_trace_on_unchanged_source(self):
        old = diagnostic(1, notes=[{"file": "a.c", "line": 1, "column": 8, "message": "origin"}])
        for line, message, status in ((1, "origin", "PASS"), (1, "changed origin", "INCOMPLETE"), (2, "origin", "INCOMPLETE")):
            with self.subTest(line=line, message=message):
                new = copy.deepcopy(old)
                new.update(line=line, column=6)
                new["notes"][0]["message"] = message
                gate = self.compare(report([old]), report([new]), "int x;\nint y;\n")
                self.assertEqual(gate["status"], status)

    def test_duplicate_translation_unit_diagnostics_are_not_new_occurrences(self):
        gate = self.compare(report([diagnostic(1)]), report([diagnostic(1), diagnostic(1)]))
        self.assertEqual((gate["status"], gate["counts"]["existing"]), ("PASS", 1))

    def test_resolved_and_new_errors_do_not_cancel_each_other(self):
        gate = self.compare(report([diagnostic(1)]), report([diagnostic(1, "24 is a magic number")]), new_source="int f(void) { return 24; }\n")
        self.assertEqual((gate["counts"]["resolved"], gate["counts"]["new"]), (1, 1))

    def test_format_failure_remains_strict_and_keeps_its_location(self):
        old = report([diagnostic(1, rule="clang-format", severity="warning")])
        old["checks"][0]["id"] = "clang-format"
        gate = self.compare(old, old)
        self.assertEqual(gate["status"], "FAIL")
        self.assertEqual(gate["counts"]["required"], 1)
        self.assertEqual(gate["issues"][0]["issue"]["file"], "a.c")

    def test_missing_compilation_entry_gap_cannot_establish_a_pass(self):
        current = report([], gaps=["no compilation database entry: b.c"])
        gate = self.compare(report([]), current)
        self.assertEqual(gate["status"], "INCOMPLETE")

    def test_repeated_legacy_lines_keep_their_identity_after_a_small_insertion(self):
        old_source = "int value;\n" * 5000 + "int f(void) { return 23; }\n"
        new_source = "int value;\n" * 5001 + "int f(void) { return 23; }\n"
        gate = self.compare(report([diagnostic(5001)]), report([diagnostic(5002)]), old_source, new_source)
        self.assertEqual((gate["status"], gate["counts"]["existing"]), ("PASS", 1))


if __name__ == "__main__":
    unittest.main()
