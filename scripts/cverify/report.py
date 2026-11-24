"""Versioned detailed report and token-bounded compact renderers."""

from __future__ import annotations

import json
import platform
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from .execution import ERROR, FAIL, PASS, SKIP, CheckResult


SCHEMA_VERSION = 1


def summarize(checks: Sequence[CheckResult]) -> dict:
    required = [check for check in checks if check.required]
    if any(check.status == FAIL for check in checks):
        status = FAIL
    elif any(check.status == ERROR for check in checks):
        status = ERROR
    elif any(check.status == SKIP for check in required):
        status = "INCOMPLETE"
    else:
        status = PASS
    complete = not any(check.status == ERROR or (check.required and check.status == SKIP) for check in checks)
    gaps = [gap for check in checks for gap in check.gaps]
    return {
        "status": status,
        "complete": complete,
        "files": len({file for check in checks for file in check.files}),
        "checks": len(checks),
        "issues": sum(len(check.issues) for check in checks),
        "errors": sum(issue.severity == "error" for check in checks for issue in check.issues),
        "warnings": sum(issue.severity == "warning" for check in checks for issue in check.issues),
        "gaps": len(gaps),
        "skipped": sum(check.status == SKIP for check in checks),
        "suppressed": sum(len(check.metrics.get("suppressed", [])) for check in checks),
    }


def exit_code(summary: dict) -> int:
    if summary["status"] == FAIL:
        return 1
    if summary["status"] == ERROR:
        return 2
    if summary["status"] == "INCOMPLETE":
        return 3
    return 0


def dump_report_json(document: dict) -> str:
    total_issues = sum(len(check.get("issues", [])) for check in document.get("checks", []))
    if total_issues > 1000:
        return json.dumps(document, ensure_ascii=False, separators=(",", ":"))
    return json.dumps(document, ensure_ascii=False, indent=2)


def full_document(context: dict, checks: Sequence[CheckResult], requirements: list[dict]) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "host": {"system": platform.system(), "machine": platform.machine(), "python": platform.python_version()},
        **context,
        "summary": summarize(checks),
        "checks": [check.to_dict() for check in checks],
        "requirements": requirements,
    }


_DEFAULT_GATE_REASONS = {
    "new": "no matching diagnostic under the same rule configuration",
    "worsened": "compared the same function's metric evidence",
    "required": "this check always applies to the current snapshot",
}


def _rule_counts(issues: Sequence[dict], top: int = 10) -> dict[str, int]:
    counts: dict[str, int] = {}
    for issue in issues:
        rule = issue.get("rule") or "unknown"
        counts[rule] = counts.get(rule, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:top])


def _compact_issue(issue: dict) -> dict:
    item = {"rule": issue["rule"], "severity": issue["severity"], "message": issue["message"][:240]}
    if issue.get("file"):
        item["file"] = issue["file"]
    if issue.get("line"):
        item["line"] = issue["line"]
    return item


def compact_document(document: dict, limit: int = 5) -> dict:
    summary = document["summary"]
    cap = None if limit <= 0 else limit
    if "gate" in document:
        gate = document["gate"]
        actionable = [record for record in gate["issues"] if record["classification"] != "existing"]
        shown = actionable if cap is None else actionable[:cap]
        omitted_records = [] if cap is None else actionable[cap:]
        findings = []
        for record in shown:
            item = {"check": record["check"], "classification": record["classification"], **_compact_issue(record["issue"])}
            if record["reason"] != _DEFAULT_GATE_REASONS.get(record["classification"]):
                item["reason"] = record["reason"]
            if record.get("matched_baseline"):
                item["matched_baseline"] = record["matched_baseline"]
            findings.append(item)
        compact_gate = {"counts": gate["counts"], "baseline_commit": gate["baseline_commit"], "snapshot": gate["snapshot"]}
        if gate["required_failures"]:
            compact_gate["required_failures"] = gate["required_failures"]
        if gate.get("baseline_cached"):
            compact_gate["baseline_cached"] = True
        if gate.get("advisory_non_source_changes"):
            compact_gate["advisory_non_source_changes"] = gate["advisory_non_source_changes"][:5]
        result = {
            "schema_version": SCHEMA_VERSION, "status": gate["status"], "complete": gate["complete"],
            "scan_status": summary["status"], "scan_complete": summary["complete"],
            "files": summary["files"], "checks": summary["checks"],
            "gate": compact_gate,
            "findings": findings,
        }
        if omitted_records:
            result["issues_omitted"] = len(omitted_records)
            result["omitted_by_rule"] = _rule_counts([record["issue"] for record in omitted_records])
        if gate["gaps"]:
            result["gap_summary"] = gate["gaps"][:3] if cap is not None else gate["gaps"]
            if cap is not None and len(gate["gaps"]) > 3:
                result["gaps_omitted"] = len(gate["gaps"]) - 3
        if not (gate["status"] == PASS and gate["complete"] and not actionable and not gate["gaps"]):
            result["report"] = document["report_path"]
        return result
    if summary["issues"] == 0 and summary["gaps"] == 0 and summary["status"] == PASS and summary["complete"]:
        result = {"schema_version": SCHEMA_VERSION, "status": PASS, "complete": True, "files": summary["files"], "checks": summary["checks"]}
        if summary["skipped"]:
            result["skipped"] = summary["skipped"]
        if summary["suppressed"]:
            result["suppressed"] = summary["suppressed"]
        return result
    result = {
        "schema_version": SCHEMA_VERSION,
        "status": summary["status"],
        "complete": summary["complete"],
        "files": summary["files"],
        "checks": summary["checks"],
        "issues": summary["issues"],
        "errors": summary["errors"],
    }
    for key in ("warnings", "gaps", "skipped", "suppressed"):
        if summary[key]:
            result[key] = summary[key]
    relevant = [check for check in document["checks"] if check["status"] in (FAIL, ERROR) or check["issues"]]
    omitted_list: list[dict] = []
    failures = []
    for check in relevant:
        if cap is not None and len(failures) >= cap:
            omitted_list.extend(check["issues"])
            continue
        issues = check["issues"] if cap is None else check["issues"][:cap]
        if cap is not None:
            omitted_list.extend(check["issues"][cap:])
        failures.append({
            "id": check["id"],
            "status": check["status"],
            **({"reason": check["reason"][:240]} if check.get("reason") else {}),
            **({"issues": [_compact_issue(issue) for issue in issues]} if issues else {}),
        })
    if failures:
        result["findings"] = failures
    if omitted_list:
        result["issues_omitted"] = len(omitted_list)
        result["omitted_by_rule"] = _rule_counts(omitted_list)
    incomplete = [
        {"id": check["id"], "status": check["status"], "reason": (check.get("reason") or "not executed")[:240]}
        for check in document["checks"]
        if check["status"] in (ERROR, SKIP) and (check["required"] or check["status"] == ERROR)
    ]
    if incomplete:
        result["not_completed"] = incomplete if cap is None else incomplete[:cap]
        if cap is not None and len(incomplete) > cap:
            result["not_completed_omitted"] = len(incomplete) - cap
    gaps = [gap for check in document["checks"] for gap in check["gaps"]]
    if gaps:
        result["gap_summary"] = gaps if cap is None else gaps[:3]
        if cap is not None and len(gaps) > 3:
            result["gaps_omitted"] = len(gaps) - 3
    if document.get("report_path") and summary["status"] != PASS:
        result["report"] = document["report_path"]
    return result


def _text_issue(issue: dict) -> str:
    location = f"{issue.get('file') or '<global>'}:{issue.get('line') or 0}"
    return f"{location}: {issue['message']} [{issue['rule']}]"


def _omitted_line(compact: dict) -> str:
    breakdown = ", ".join(f"{rule}:{cnt}" for rule, cnt in compact.get("omitted_by_rule", {}).items())
    return f"omitted: {compact['issues_omitted']}" + (f" ({breakdown})" if breakdown else "")


def render_text(document: dict, detail: bool, limit: int = 5) -> str:
    gate_lines = []
    if "gate" in document:
        gate = document["gate"]
        if detail:
            gate_lines = [f"Gate {gate['status']} | complete={gate['complete']} | scan={document['summary']['status']}", "counts: " + json.dumps(gate["counts"])]
        else:
            compact = compact_document(document, limit=limit)
            counts = {k: v for k, v in gate["counts"].items() if v}
            count_text = " ".join(f"{k}={v}" for k, v in counts.items()) or "clean"
            if gate.get("baseline_cached"):
                count_text += " (cached)"
            gate_lines = [f"Gate {gate['status']} | complete={gate['complete']} | scan={document['summary']['status']} | {count_text}"]
            for record in compact["findings"]:
                loc = f"{record.get('file') or '<global>'}:{record.get('line') or 0}"
                line = f"{record['classification']} [{record['check']}/{record['severity']}] {loc} [{record['rule']}] {record['message']}"
                if record.get("matched_baseline"):
                    mb = record["matched_baseline"]
                    line += f" (baseline {mb.get('file')}:{mb.get('line')})"
                if record.get("reason"):
                    line += f" — {record['reason']}"
                gate_lines.append(line)
            if compact.get("issues_omitted"):
                gate_lines.append(_omitted_line(compact))
            gate_lines.extend("gap: " + gap for gap in compact.get("gap_summary", []))
            gate_lines.extend("failed: " + identifier for identifier in gate.get("required_failures", []))
            if compact.get("report"):
                gate_lines.append("detail: " + compact["report"])
            return "\n".join(gate_lines)
    if detail:
        lines = [*gate_lines, f"C-Verify scan {document['summary']['status']} | complete={document['summary']['complete']}"]
        for check in document["checks"]:
            lines.append(f"\n[{check['id']}] {check['status']} required={check['required']} exit={check['exit_code']}")
            if check.get("reason"):
                lines.append(f"reason: {check['reason']}")
            if check.get("tool"):
                lines.append(f"tool: {check['tool']}")
            if check.get("command"):
                lines.append("command: " + " ".join(check["command"]))
            if check.get("log"):
                lines.append(f"log: {check['log']}")
            if check.get("metrics"):
                lines.append("metrics: " + json.dumps(check["metrics"], ensure_ascii=False))
            lines.extend(_text_issue(issue) for issue in check["issues"])
            lines.extend("gap: " + gap for gap in check["gaps"])
        return "\n".join(lines)
    compact = compact_document(document, limit=limit)
    summary = document["summary"]
    if summary["issues"] == 0 and summary["gaps"] == 0 and summary["status"] == PASS and summary["complete"]:
        parts = ["PASS", f"files={summary['files']} checks={summary['checks']}"]
        for key in ("skipped", "suppressed"):
            if summary[key]:
                parts.append(f"{key}={summary[key]}")
        return " | ".join(parts)
    header = [f"files={summary['files']}", f"checks={summary['checks']}", f"issues={summary['issues']}"]
    for key in ("gaps", "skipped", "suppressed"):
        if summary[key]:
            header.append(f"{key}={summary[key]}")
    lines = [f"{summary['status']} | complete={str(summary['complete']).lower()} | " + " ".join(header)]
    for finding in compact.get("findings", []):
        lines.append(f"{finding['id']}: {finding['status']}" + (f" — {finding['reason']}" if finding.get("reason") else ""))
        lines.extend("  " + _text_issue(issue) for issue in finding.get("issues", []))
    if compact.get("issues_omitted"):
        lines.append(_omitted_line(compact))
    for check in compact.get("not_completed", []):
        lines.append(f"{check['id']}: {check['status']} — {check['reason']}")
    lines.extend("gap: " + gap for gap in compact.get("gap_summary", []))
    if compact.get("report"):
        lines.append("detail: " + compact["report"])
    return "\n".join(lines)


def render(document: dict, output_format: str, detail: bool, limit: int = 5) -> str:
    value = document if detail else compact_document(document, limit=limit)
    if output_format == "json":
        return json.dumps(value, ensure_ascii=False, indent=2 if detail else None, separators=None if detail else (",", ":"))
    if output_format == "text":
        return render_text(document, detail, limit=limit)
    if output_format == "markdown":
        text = render_text(document, detail, limit=limit)
        return "# C-Verify\n\n```text\n" + text + "\n```"
    raise ValueError(f"unknown output format: {output_format}")
