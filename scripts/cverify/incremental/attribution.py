import copy
import difflib
import re
from collections import Counter
from pathlib import Path


METRIC_RULES = {"readability-function-cognitive-complexity", "readability-function-size", "cyclomatic-complexity", "function-nloc", "function-length"}


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


def same_trace(before, after, before_root, after_root, renames, maps):
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
            if name not in maps:
                maps[name] = unchanged_lines(before_source, after_source)
            if maps[name].get(new.get("line")) != old.get("line"):
                return False
        elif old != new:
            return False
    return True


def classify_issue(records, current, before_root, after_root, renames, used, maps):
    issue = current["issue"]
    name = issue.get("file")
    after = source_path(after_root, name)
    if not after or type(issue.get("line")) is not int or issue["line"] < 1:
        return "unattributed", [], "diagnostic has no versioned source location"
    old_name = renames.get(name, name)
    before = source_path(before_root, old_name)
    if before is None:
        return "new", [], "source did not exist in the baseline"
    if name not in maps:
        maps[name] = unchanged_lines(before, after)
    line_map = maps[name]
    if issue["rule"] in METRIC_RULES:
        candidates = matching_metric(records, current, old_name, line_map, used)
        if len(candidates) == 1:
            other = records[candidates[0]]["issue"]
            classification = metric_change(other, issue)
            if other["severity"] != "error":
                classification = "worsened"
            return classification, candidates, "compared the same function's metric evidence"
        if candidates:
            return "unattributed", candidates, "function identity is ambiguous"
    candidates = [index for index, record in enumerate(records) if index not in used and record["issue"].get("file") == old_name and issue_key(record["check"], record["issue"], before_root) == issue_key(current["check"], issue, after_root)]
    same_line = [index for index in candidates if records[index]["issue"].get("line") == line_map.get(issue["line"])]
    exact = [index for index in same_line if records[index]["issue"].get("column") == issue.get("column")]
    if not exact and len(same_line) == 1 and before.read_bytes() == after.read_bytes():
        exact = same_line
    if len(exact) == 1:
        other = records[exact[0]]["issue"]
        if not same_trace(other.get("notes", []), issue.get("notes", []), before_root, after_root, renames, maps):
            return "unattributed", exact, "diagnostic trace changed"
        return "existing" if other["severity"] == "error" else "worsened", exact, "same diagnostic on an unchanged source line"
    if candidates:
        target_line = line_map.get(issue["line"], issue["line"])
        best = min(candidates, key=lambda idx: abs((records[idx]["issue"].get("line") or 0) - target_line))
        return "unattributed", [best], "similar old diagnostic moved or its source line changed"
    return "new", [], "no matching diagnostic under the same rule configuration"


def execution_gaps(document, label):
    gaps = []
    if not document["summary"]["complete"]:
        gaps.append(label + " analysis did not complete")
    for check in document["checks"]:
        gaps.extend(label + ": " + check["id"] + ": " + gap for gap in check.get("gaps", []) if gap.startswith(("no compilation database entry:", "compilation database not found")))
        if check["status"] == "ERROR" or check["status"] == "SKIP" and check.get("required"):
            gaps.append(label + ": " + check["id"] + ": " + (check.get("reason") or check["status"]))
    return gaps


def compare_reports(before, after, before_root, after_root, renames=None, gaps=()):
    before_root, after_root = Path(before_root).resolve(), Path(after_root).resolve()
    old = report_issues(before, before_root)
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
        if current["check"] in required_failures:
            classification, matches, reason = "required", [], "this check always applies to the current snapshot"
        elif missing:
            classification, matches, reason = "unattributed", [], "analysis evidence is incomplete"
        else:
            classification, matches, reason = classify_issue(old, current, before_root, after_root, renames or {}, used, maps)
        used.update(matches)
        classified.append({**copy.deepcopy(current), "classification": classification, "reason": reason})
    counts = dict.fromkeys(("new", "worsened", "existing", "unattributed", "resolved", "required"), 0)
    counts.update(Counter(record["classification"] for record in classified))
    if not missing and not counts["unattributed"]:
        counts["resolved"] = sum(index not in used and record["issue"]["severity"] == "error" for index, record in enumerate(old))
    complete = not missing and not counts["unattributed"]
    status = "FAIL" if counts["new"] or counts["worsened"] or required_failures else "PASS" if complete else "INCOMPLETE"
    return {"status": status, "complete": complete, "counts": counts, "issues": classified, "gaps": missing, "required_failures": required_failures}
