import copy
import difflib
import re
from collections import Counter
from pathlib import Path


METRIC_RULES = {"readability-function-cognitive-complexity", "readability-function-size", "cyclomatic-complexity", "function-nloc", "function-length"}
UNSTABLE_CROSS_DECLARATION_RULES = {"misra-c2012-5.8", "misra-c2012-5.9"}


def normalize_message(message, root_strings):
    for root_string in root_strings:
        if root_string in message:
            message = message.replace(root_string, "<project>")
    return message


def metric_values(issue):
    if issue["rule"] not in METRIC_RULES:
        return None
    if issue["rule"] == "readability-function-size":
        values = {}
        for note in issue.get("notes", []):
            if "threshold" not in note["message"]:
                continue
            match = re.match(r"(?:nesting level )?(\d+) (.+?) \(threshold (\d+)\)", note["message"])
            if not match:
                return None
            metric_name = "nesting level" if "nesting level" in note["message"] else match[2]
            values[metric_name] = (int(match[1]), int(match[3]))
        return values or None
    match = re.search(r"(?:complexity of|McCabe CCN|NLOC|total length) (\d+).*?\(threshold (\d+)\)", issue["message"])
    return {issue["rule"]: (int(match[1]), int(match[2]))} if match else None


def metric_change(before, after):
    old, new = metric_values(before), metric_values(after)
    if not old or not new:
        return "unattributed"
    if any(key in old and old[key][1] != value[1] for key, value in new.items()):
        return "unattributed"
    return "worsened" if any(key not in old or value[0] > old[key][0] for key, value in new.items()) else "existing"


def source_path(root, name):
    if not name:
        return None
    path = (root / name).resolve()
    return path if path.is_relative_to(root) and path.is_file() else None


def issue_key(check, issue, root):
    message = issue["message"].replace(str(root), "<project>").replace(root.as_posix(), "<project>")
    return check, issue["rule"], message


def report_issues(document, root):
    records, seen = [], set()
    for check in document["checks"]:
        for issue in check.get("issues", []):
            record = {"check": check["id"], "issue": issue}
            key = (issue_key(check["id"], issue, root), issue.get("file"), issue.get("line"), issue.get("column"), issue["severity"], repr(issue.get("notes", [])))
            if key not in seen:
                seen.add(key)
                records.append(record)
    return records


def unchanged_lines(before, after):
    old, new = before.read_bytes().splitlines(), after.read_bytes().splitlines()
    prefix = 0
    limit = min(len(old), len(new))
    while prefix < limit and old[prefix] == new[prefix]:
        prefix += 1
    suffix = 0
    while suffix < limit - prefix and old[-suffix - 1] == new[-suffix - 1]:
        suffix += 1
    mapping = {line + 1: line + 1 for line in range(prefix)}
    mapping.update({len(new) - offset: len(old) - offset for offset in range(suffix)})
    blocks = difflib.SequenceMatcher(None, old[prefix:len(old) - suffix], new[prefix:len(new) - suffix], autojunk=False).get_matching_blocks()
    mapping.update({prefix + b + offset + 1: prefix + a + offset + 1 for a, b, size in blocks for offset in range(size)})
    return mapping


def matching_metric(records, current, old_name, line_map, used):
    issue = current["issue"]
    function = re.search(r"function '(.+?)'", issue["message"])
    if not function:
        return []
    candidates = []
    for index, record in enumerate(records):
        other = record["issue"]
        name = re.search(r"function '(.+?)'", other["message"])
        if index not in used and record["check"] == current["check"] and other["rule"] == issue["rule"] and other.get("file") == old_name and name and name[1] == function[1]:
            candidates.append(index)
    mapped = [index for index in candidates if records[index]["issue"].get("line") == line_map.get(issue.get("line"))]
    return mapped or candidates


def same_trace(before, after, before_root, after_root, renames, maps, bytes_equal=None):
    if len(before) != len(after):
        return False
    for old, new in zip(before, after):
        if old.get("message") != new.get("message") or old.get("column") != new.get("column"):
            return False
        name = new.get("file")
        old_name = renames.get(name, name)
        if old.get("file") != old_name:
            return False
        before_source, after_source = source_path(before_root, old_name), source_path(after_root, name)
        if before_source and after_source:
            pair = (old_name, name)
            equal = bytes_equal.get(pair) if bytes_equal is not None else None
            if equal is None:
                equal = before_source.read_bytes() == after_source.read_bytes()
                if bytes_equal is not None:
                    bytes_equal[pair] = equal
            if equal:
                if new.get("line") != old.get("line"):
                    return False
                continue
            if name not in maps:
                maps[name] = unchanged_lines(before_source, after_source)
            if maps[name].get(new.get("line")) != old.get("line"):
                return False
        elif old != new:
            return False
    return True


def _baseline_coords(issue):
    return {"file": issue.get("file"), "line": issue.get("line"), "column": issue.get("column")}


def classify_issue(records, current, before_root, after_root, renames, used, maps, index=None):
    issue = current["issue"]
    name = issue.get("file")
    after = source_path(after_root, name)
    if not after or type(issue.get("line")) is not int or issue["line"] < 1:
        return "unattributed", [], "diagnostic has no versioned source location", None
    old_name = renames.get(name, name)
    before = source_path(before_root, old_name)
    if before is None:
        return "new", [], "source did not exist in the baseline", None
    bytes_equal = index["bytes_equal"] if index else {}
    pair = (old_name, name)
    same_bytes = bytes_equal.get(pair)
    if same_bytes is None:
        same_bytes = before.read_bytes() == after.read_bytes()
        bytes_equal[pair] = same_bytes
    if same_bytes:
        line_map = None
    else:
        if name not in maps:
            maps[name] = unchanged_lines(before, after)
        line_map = maps[name]

    def _old_line(line_no):
        return line_no if line_map is None else line_map.get(line_no)

    if issue["rule"] in METRIC_RULES:
        if index is not None:
            function = re.search(r"function '(.+?)'", issue["message"])
            if function:
                pool = [idx for idx in index["by_metric"].get((old_name, current["check"], issue["rule"], function[1]), ()) if idx not in used]
                expected = _old_line(issue.get("line"))
                mapped = [idx for idx in pool if records[idx]["issue"].get("line") == expected]
                candidates = mapped or pool
            else:
                candidates = []
        else:
            candidates = matching_metric(records, current, old_name, line_map or {}, used)
        if len(candidates) == 1:
            other = records[candidates[0]]["issue"]
            classification = metric_change(other, issue)
            if other["severity"] != "error":
                classification = "worsened"
            return classification, candidates, "compared the same function's metric evidence", None
        if candidates:
            return "unattributed", candidates, "function identity is ambiguous", _baseline_coords(records[candidates[0]]["issue"])

    if index is not None:
        norm = normalize_message(issue["message"], index["after_strs"])
        candidates = [idx for idx in index["by_key"].get((old_name, current["check"], issue["rule"], norm), ()) if idx not in used]
    else:
        candidates = [idx for idx, record in enumerate(records) if idx not in used and record["issue"].get("file") == old_name and issue_key(record["check"], record["issue"], before_root) == issue_key(current["check"], issue, after_root)]
    expected_line = _old_line(issue["line"])
    same_line = [idx for idx in candidates if records[idx]["issue"].get("line") == expected_line]
    exact = [idx for idx in same_line if records[idx]["issue"].get("column") == issue.get("column")]
    if not exact and len(same_line) == 1 and same_bytes:
        exact = same_line
    if len(exact) == 1:
        other = records[exact[0]]["issue"]
        if not same_trace(other.get("notes", []), issue.get("notes", []), before_root, after_root, renames, maps, bytes_equal):
            return "unattributed", exact, "diagnostic trace changed", _baseline_coords(other)
        return ("existing" if other["severity"] == "error" else "worsened"), exact, "same diagnostic on an unchanged source line", None
    if candidates:
        target_line = issue["line"] if line_map is None else line_map.get(issue["line"], issue["line"])
        best = min(candidates, key=lambda idx: abs((records[idx]["issue"].get("line") or 0) - target_line))
        matched_issue = records[best]["issue"]
        if issue["rule"] in UNSTABLE_CROSS_DECLARATION_RULES and same_bytes and not issue.get("notes") and not matched_issue.get("notes"):
            classification = "existing" if matched_issue["severity"] == "error" else "worsened"
            return classification, [best], "same cross-declaration diagnostic in an unchanged file", None
        return "unattributed", [best], "similar old diagnostic moved or its source line changed", _baseline_coords(matched_issue)
    return "new", [], "no matching diagnostic under the same rule configuration", None


def execution_gaps(document, label):
    gaps = []
    if not document["summary"]["complete"]:
        gaps.append(label + " analysis did not complete")
    for check in document["checks"]:
        gaps.extend(label + ": " + check["id"] + ": " + gap for gap in check.get("gaps", []) if gap.startswith(("no compilation database entry:", "compilation database not found")))
        if check["status"] == "ERROR" or check["status"] == "SKIP" and check.get("required"):
            gaps.append(label + ": " + check["id"] + ": " + (check.get("reason") or check["status"]))
    return gaps


def _build_index(records, before_root, after_root):
    before_strs = (str(before_root), before_root.as_posix())
    after_strs = (str(after_root), after_root.as_posix())
    by_key = {}
    by_metric = {}
    for idx, record in enumerate(records):
        other = record["issue"]
        file_name = other.get("file")
        norm = normalize_message(other["message"], before_strs)
        by_key.setdefault((file_name, record["check"], other["rule"], norm), []).append(idx)
        if other["rule"] in METRIC_RULES:
            func = re.search(r"function '(.+?)'", other["message"])
            if func:
                by_metric.setdefault((file_name, record["check"], other["rule"], func[1]), []).append(idx)
    return {"by_key": by_key, "by_metric": by_metric, "after_strs": after_strs, "bytes_equal": {}}


def compare_reports(before, after, before_root, after_root, renames=None, gaps=()):
    before_root, after_root = Path(before_root).resolve(), Path(after_root).resolve()
    old = report_issues(before, before_root)
    index = _build_index(old, before_root, after_root)
    used, maps, classified = set(), {}, []
    missing = [*gaps, *execution_gaps(before, "baseline"), *execution_gaps(after, "current")]
    required_failures = []
    for check in after["checks"]:
        if check["status"] == "FAIL" and (check.get("metrics", {}).get("kind") in {"build", "test", "fuzz", "project_rule"} or check["id"] in {"clang-format", "llvm-cov"}):
            required_failures.append(check["id"])
        elif check["status"] == "FAIL" and not any(i["severity"] == "error" for i in check.get("issues", [])):
            missing.append(check["id"] + ": failed without a structured error diagnostic")
    new = [record for record in report_issues(after, after_root) if record["issue"]["severity"] == "error" or record["check"] in required_failures]
    for current in new:
        matched_baseline = None
        if current["check"] in required_failures:
            classification, matches, reason = "required", [], "this check always applies to the current snapshot"
        elif missing:
            classification, matches, reason = "unattributed", [], "analysis evidence is incomplete"
        else:
            classification, matches, reason, matched_baseline = classify_issue(old, current, before_root, after_root, renames or {}, used, maps, index=index)
        used.update(matches)
        entry = {**copy.deepcopy(current), "classification": classification, "reason": reason}
        if matched_baseline is not None:
            entry["matched_baseline"] = matched_baseline
        classified.append(entry)
    counts = dict.fromkeys(("new", "worsened", "existing", "unattributed", "resolved", "required"), 0)
    counts.update(Counter(record["classification"] for record in classified))
    if not missing and not counts["unattributed"]:
        counts["resolved"] = sum(idx not in used and record["issue"]["severity"] == "error" for idx, record in enumerate(old))
    complete = not missing and not counts["unattributed"]
    status = "FAIL" if counts["new"] or counts["worsened"] or required_failures else "PASS" if complete else "INCOMPLETE"
    return {"status": status, "complete": complete, "counts": counts, "issues": classified, "gaps": missing, "required_failures": required_failures}
