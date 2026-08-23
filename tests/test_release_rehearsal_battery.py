from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.run_release_rehearsal import _NAMESPACE_RE
from scripts.run_release_rehearsal_battery import (
    BATTERY_SCHEMA,
    recompute_aggregate_digest,
    rehearsal_namespaces,
    run_release_rehearsal_battery,
)


def _candidate_repository(root: Path) -> tuple[Path, str]:
    source = root / "source"
    package = source / "src" / "nested_memvid_agent"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text('__version__ = "1.2.3"\n', encoding="utf-8")
    (source / "README.md").write_text("# battery fixture\n", encoding="utf-8")
    (source / "pyproject.toml").write_text(
        """
[build-system]
requires = ["setuptools==83.0.0"]
build-backend = "setuptools.build_meta"

[project]
name = "nested-memvid-agent"
version = "1.2.3"
description = "Release rehearsal battery fixture"
readme = "README.md"
requires-python = ">=3.11"

[tool.setuptools.packages.find]
where = ["src"]
""".lstrip(),
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q", source], check=True)
    subprocess.run(
        ["git", "-C", source, "config", "user.email", "battery@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", source, "config", "user.name", "Kestrel Battery"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", source, "config", "core.autocrlf", "false"],
        check=True,
    )
    subprocess.run(["git", "-C", source, "add", "."], check=True)
    subprocess.run(
        ["git", "-C", source, "commit", "-q", "-m", "fixture"],
        check=True,
    )
    commit = subprocess.run(
        ["git", "-C", source, "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return source, commit


def test_rehearsal_namespaces_are_valid_and_unique() -> None:
    namespaces = rehearsal_namespaces("a" * 40, 20)

    assert len(namespaces) == 20
    assert len(set(namespaces)) == 20
    for namespace in namespaces:
        assert _NAMESPACE_RE.fullmatch(namespace), namespace
        assert "a" * 12 in namespace


def test_battery_rejects_nonpositive_repeat_count() -> None:
    with pytest.raises(ValueError, match="repeats"):
        rehearsal_namespaces("a" * 40, 0)
    with pytest.raises(ValueError, match="repeats"):
        rehearsal_namespaces("a" * 40, -1)


def test_aggregate_digest_recomputation_is_deterministic() -> None:
    receipt = {
        "schema": BATTERY_SCHEMA,
        "source": {
            "commit": "b" * 40,
            "distribution": "nested-memvid-agent",
            "version": "1.2.3",
        },
        "repeats": 2,
        "namespaces": ["kestrel-rehearsal-bb-001", "kestrel-rehearsal-bb-002"],
        "rehearsal_reports": [
            {
                "index": 1,
                "namespace": "kestrel-rehearsal-bb-001",
                "report_file": "rehearsal-001.json",
                "report_sha256": "c" * 64,
            },
            {
                "index": 2,
                "namespace": "kestrel-rehearsal-bb-002",
                "report_file": "rehearsal-002.json",
                "report_sha256": "d" * 64,
            },
        ],
        "zero_flaky_failures": True,
        "aggregate_digest": "0" * 64,
    }

    digest = recompute_aggregate_digest(receipt)

    assert digest == hashlib.sha256(
        json.dumps(
            {key: value for key, value in receipt.items() if key != "aggregate_digest"},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    ).hexdigest()
    assert recompute_aggregate_digest(receipt) == digest


def test_battery_runs_consecutive_unique_namespace_rehearsals_and_seals_digest(
    tmp_path: Path,
) -> None:
    source, commit = _candidate_repository(tmp_path)
    sandbox_root = tmp_path / "battery-sandboxes"
    output_dir = tmp_path / "battery-output"

    receipt = run_release_rehearsal_battery(
        source_root=source,
        sandbox_root=sandbox_root,
        commit=commit,
        repeats=3,
        output_dir=output_dir,
    )

    assert receipt["schema"] == BATTERY_SCHEMA
    assert receipt["repeats"] == 3
    assert receipt["source"]["commit"] == commit
    assert receipt["zero_flaky_failures"] is True
    assert len(receipt["namespaces"]) == 3
    assert len(set(receipt["namespaces"])) == 3
    assert recompute_aggregate_digest(receipt) == receipt["aggregate_digest"]

    written = json.loads(
        (output_dir / "aggregate-receipt.json").read_text(encoding="utf-8")
    )
    assert written == receipt
    for index in (1, 2, 3):
        report = json.loads(
            (output_dir / f"rehearsal-{index:03d}.json").read_text(encoding="utf-8")
        )
        assert report["passed"] is True
        assert report["source"]["commit"] == commit
        assert report["namespace"] == receipt["namespaces"][index - 1]


def test_battery_fails_closed_on_first_failure_without_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, commit = _candidate_repository(tmp_path)
    calls: list[str] = []

    def _flaky_rehearsal(*, source_root: Path, sandbox_root: Path, namespace: str, commit: str) -> dict[str, object]:
        calls.append(namespace)
        if len(calls) >= 2:
            raise ValueError("injected rehearsal failure")
        return {"passed": True, "namespace": namespace}

    monkeypatch.setattr(
        "scripts.run_release_rehearsal_battery.run_release_rehearsal",
        _flaky_rehearsal,
    )

    with pytest.raises(ValueError, match="rehearsal 2"):
        run_release_rehearsal_battery(
            source_root=source,
            sandbox_root=tmp_path / "battery-sandboxes",
            commit=commit,
            repeats=3,
            output_dir=tmp_path / "battery-output",
        )

    assert len(calls) == 2


def test_battery_rejects_missing_source_commit(tmp_path: Path) -> None:
    source, _ = _candidate_repository(tmp_path)
    with pytest.raises(ValueError, match="source commit"):
        run_release_rehearsal_battery(
            source_root=source,
            sandbox_root=tmp_path / "battery-sandboxes",
            commit="e" * 40,
            repeats=1,
            output_dir=tmp_path / "battery-output",
        )


def test_battery_cli_help_works() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "scripts.run_release_rehearsal_battery", "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "--repeats" in completed.stdout
