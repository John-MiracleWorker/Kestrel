#!/usr/bin/env python3
"""Enforce Kestrel's reviewed container vulnerability policy over Trivy JSON."""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path
from typing import Any

GATED_SEVERITIES = {"HIGH", "CRITICAL"}


def _load_object(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected a JSON object in {path}")
    return payload


def evaluate_policy(
    report: dict[str, Any],
    policy: dict[str, Any],
    *,
    today: date,
) -> tuple[int, list[str]]:
    errors: list[str] = []
    if policy.get("schema_version") != 1:
        return 0, ["exception policy schema_version must be 1"]
    raw_exceptions = policy.get("exceptions")
    if not isinstance(raw_exceptions, list):
        return 0, ["exception policy must contain an exceptions array"]

    exceptions: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(raw_exceptions):
        if not isinstance(raw, dict):
            errors.append(f"exception {index} must be an object")
            continue
        vulnerability_id = str(raw.get("id", "")).strip()
        if not vulnerability_id:
            errors.append(f"exception {index} has no id")
            continue
        if vulnerability_id in exceptions:
            errors.append(f"duplicate exception for {vulnerability_id}")
            continue
        try:
            expiry = date.fromisoformat(str(raw.get("expires_on", "")))
        except ValueError:
            errors.append(f"{vulnerability_id} has an invalid expires_on date")
            continue
        if expiry < today:
            errors.append(f"{vulnerability_id} exception expired on {expiry.isoformat()}")
        severities = raw.get("severities")
        packages = raw.get("packages")
        versions = raw.get("installed_versions")
        rationale = str(raw.get("rationale", "")).strip()
        controls = raw.get("controls")
        if not isinstance(severities, list) or not all(isinstance(item, str) for item in severities):
            errors.append(f"{vulnerability_id} severities must be a string array")
        if not isinstance(packages, list) or not all(isinstance(item, str) for item in packages):
            errors.append(f"{vulnerability_id} packages must be a string array")
        if not isinstance(versions, list) or not all(isinstance(item, str) for item in versions):
            errors.append(f"{vulnerability_id} installed_versions must be a string array")
        if len(rationale) < 30:
            errors.append(f"{vulnerability_id} rationale is too short")
        if not isinstance(controls, list) or not controls or not all(
            isinstance(item, str) and item.strip() for item in controls
        ):
            errors.append(f"{vulnerability_id} controls must be a non-empty string array")
        exceptions[vulnerability_id] = raw

    results = report.get("Results")
    if not isinstance(results, list):
        return 0, [*errors, "Trivy report has no Results array"]
    observed: set[str] = set()
    gated_count = 0
    for result in results:
        if not isinstance(result, dict):
            continue
        target = str(result.get("Target", "<unknown>"))
        vulnerabilities = result.get("Vulnerabilities") or []
        if not isinstance(vulnerabilities, list):
            errors.append(f"Trivy result {target!r} has invalid Vulnerabilities")
            continue
        for finding in vulnerabilities:
            if not isinstance(finding, dict):
                continue
            vulnerability_id = str(finding.get("VulnerabilityID", "")).strip()
            severity = str(finding.get("Severity", "UNKNOWN")).upper()
            if severity not in GATED_SEVERITIES and vulnerability_id not in exceptions:
                continue
            gated_count += 1
            package = str(finding.get("PkgName", "")).strip()
            installed = str(finding.get("InstalledVersion", "")).strip()
            fixed = str(finding.get("FixedVersion", "")).strip()
            if fixed:
                errors.append(
                    f"{vulnerability_id} in {package} has fixed version {fixed}; rebuild instead of excepting it"
                )
                continue
            rule = exceptions.get(vulnerability_id)
            if rule is None:
                errors.append(
                    f"unreviewed {severity} finding {vulnerability_id} in {package} ({target})"
                )
                continue
            observed.add(vulnerability_id)
            if severity not in rule.get("severities", []):
                errors.append(f"{vulnerability_id} severity drifted to {severity}")
            if package not in rule.get("packages", []):
                errors.append(f"{vulnerability_id} appeared in unreviewed package {package}")
            if installed not in rule.get("installed_versions", []):
                errors.append(
                    f"{vulnerability_id} package {package} drifted to version {installed or '<missing>'}"
                )

    for vulnerability_id in sorted(exceptions.keys() - observed):
        errors.append(f"stale exception {vulnerability_id} was not observed; remove or re-review it")
    return gated_count, errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("report", type=Path)
    parser.add_argument("--exceptions", type=Path, required=True)
    args = parser.parse_args()
    report = _load_object(args.report)
    policy = _load_object(args.exceptions)
    gated_count, errors = evaluate_policy(report, policy, today=date.today())
    if errors:
        raise SystemExit("Container vulnerability policy failed:\n- " + "\n- ".join(errors))
    print(
        f"Container vulnerability policy passed: {gated_count} gated occurrences; "
        f"{len(policy['exceptions'])} reviewed CVEs."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
