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


def _compact_issue(issue: dict) -> dict:
    item = {"rule": issue["rule"], "severity": issue["severity"], "message": issue["message"][:240]}
    if issue.get("file"):
        item["file"] = issue["file"]
    if issue.get("line"):
        item["line"] = issue["line"]
    return item


def compact_document(document: dict, limit: int = 5) -> dict:
    summary = document["summary"]
    if "gate" in document:
        gate = document["gate"]
        actionable = [record for record in gate["issues"] if record["classification"] != "existing"]
        return {
            "schema_version": SCHEMA_VERSION, "status": gate["status"], "complete": gate["complete"],
            "scan_status": summary["status"], "scan_complete": summary["complete"],
            "files": summary["files"], "checks": summary["checks"],
            "gate": {"counts": gate["counts"], "baseline_commit": gate["baseline_commit"], "snapshot": gate["snapshot"], "required_failures": gate["required_failures"]},
            "findings": [{"check": record["check"], "classification": record["classification"], **_compact_issue(record["issue"]), "reason": record["reason"]} for record in actionable[:limit]],
            "issues_omitted": max(0, len(actionable) - limit),
            "gap_summary": gate["gaps"][:3], "gaps_omitted": max(0, len(gate["gaps"]) - 3),
            "report": document["report_path"],
        }
    result = {"schema_version": SCHEMA_VERSION, **summary}
    if summary["issues"] == 0 and summary["gaps"] == 0 and summary["status"] == PASS and summary["complete"]:
        result = {"schema_version": SCHEMA_VERSION, "status": PASS, "complete": True, "files": summary["files"], "checks": summary["checks"]}
        if summary["skipped"]:
            result["skipped"] = summary["skipped"]
        if summary["suppressed"]:
            result["suppressed"] = summary["suppressed"]
        return result
    relevant = [check for check in document["checks"] if check["status"] in (FAIL, ERROR) or check["issues"]]
    remaining = summary["issues"]
    failures = []
    for check in relevant:
        issues = check["issues"][:limit]
        remaining -= len(issues)
        failures.append({
            "id": check["id"],
            "status": check["status"],
            **({"reason": check["reason"][:240]} if check.get("reason") else {}),
            **({"issues": [_compact_issue(issue) for issue in issues]} if issues else {}),
        })
        if len(failures) >= limit:
            break
    if failures:
        result["findings"] = failures
        if remaining > 0:
            result["issues_omitted"] = remaining
    incomplete = [
        {"id": check["id"], "status": check["status"], "reason": (check.get("reason") or "not executed")[:240]}
        for check in document["checks"]
        if check["status"] in (ERROR, SKIP) and (check["required"] or check["status"] == ERROR)
    ]
    if incomplete:
        result["not_completed"] = incomplete[:limit]
        if len(incomplete) > limit:
            result["not_completed_omitted"] = len(incomplete) - limit
    gaps = [gap for check in document["checks"] for gap in check["gaps"]]
    if gaps:
        result["gap_summary"] = gaps[:3]
        if len(gaps) > 3:
            result["gaps_omitted"] = len(gaps) - 3
    if document.get("report_path") and summary["status"] != PASS:
        result["report"] = document["report_path"]
    return result


def _text_issue(issue: dict) -> str:
    location = f"{issue.get('file') or '<global>'}:{issue.get('line') or 0}"
    return f"{location}: {issue['message']} [{issue['rule']}]"


def render_text(document: dict, detail: bool) -> str:
    gate_lines = []
    if "gate" in document:
        gate = document["gate"]
        gate_lines = [f"Gate {gate['status']} | complete={gate['complete']} | scan={document['summary']['status']}", "counts: " + json.dumps(gate["counts"])]
        if not detail:
            compact = compact_document(document)
            gate_lines.extend(record["classification"] + ": " + _text_issue(record) for record in compact["findings"])
            gate_lines.extend("gap: " + gap for gap in compact["gap_summary"])
            gate_lines.extend("failed: " + identifier for identifier in gate["required_failures"])
            gate_lines.append("detail: " + document["report_path"])
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
    compact = compact_document(document)
    summary = document["summary"]
    if summary["issues"] == 0 and summary["gaps"] == 0 and summary["status"] == PASS and summary["complete"]:
        return f"PASS | complete=true | files={summary['files']} checks={summary['checks']} skipped={summary['skipped']} suppressed={summary['suppressed']}"
    lines = [
        f"{summary['status']} | complete={str(summary['complete']).lower()} | files={summary['files']} "
        f"checks={summary['checks']} issues={summary['issues']} gaps={summary['gaps']} skipped={summary['skipped']} suppressed={summary['suppressed']}"
    ]
    for finding in compact.get("findings", []):
        lines.append(f"{finding['id']}: {finding['status']}" + (f" — {finding['reason']}" if finding.get("reason") else ""))
        lines.extend("  " + _text_issue(issue) for issue in finding.get("issues", []))
    for check in compact.get("not_completed", []):
        lines.append(f"{check['id']}: {check['status']} — {check['reason']}")
    lines.extend("gap: " + gap for gap in compact.get("gap_summary", []))
    if compact.get("report"):
        lines.append("detail: " + compact["report"])
    return "\n".join(lines)


def render(document: dict, output_format: str, detail: bool) -> str:
    value = document if detail else compact_document(document)
    if output_format == "json":
        return json.dumps(value, ensure_ascii=False, indent=2 if detail else None, separators=None if detail else (",", ":"))
    if output_format == "text":
        return render_text(document, detail)
    if output_format == "markdown":
        text = render_text(document, detail)
        return "# C-Verify\n\n```text\n" + text + "\n```"
    raise ValueError(f"unknown output format: {output_format}")
