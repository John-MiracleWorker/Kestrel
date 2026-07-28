from __future__ import annotations

import shutil
from pathlib import Path

from nested_memvid_agent.config import AgentConfig
from nested_memvid_agent.orchestrator import build_memory_system
from nested_memvid_agent.repo_index import RepositoryIndex
from nested_memvid_agent.runtime_models import ToolCall
from nested_memvid_agent.tools.base import ToolContext
from nested_memvid_agent.tools.builtin import build_default_tools

FIXTURE = Path(__file__).parent / "fixtures" / "repo_index"
PROJECT_ID = "project.tools"


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    shutil.copytree(FIXTURE, repository)
    return repository


def _context(
    tmp_path: Path,
    repository: Path,
    *,
    project_id: str | None = PROJECT_ID,
    allowed_paths: tuple[str, ...] = (".",),
    baseline_index_digest: str | None = None,
    project_revision: int = 1,
) -> ToolContext:
    memory = build_memory_system("memory", tmp_path / "memory")
    context = ToolContext(
        memory=memory,
        config=AgentConfig(
            workspace=repository,
            project_id=project_id,
            project_allowed_paths=allowed_paths,
        ),
        workspace=repository,
        project_id=project_id,
        allowed_paths=allowed_paths,
    )
    if baseline_index_digest is None and project_id is not None:
        sidecar = repository / ".nest" / "repo-index" / f"{project_id}.sqlite"
        if sidecar.is_file():
            baseline_index_digest = RepositoryIndex(
                project_id=project_id,
                repository_root=repository,
                create=False,
            ).status().aggregate_digest
    context.project_revision = project_revision
    context.project_baseline_index_digest = baseline_index_digest
    return context


def test_repo_intelligence_requires_project_context_and_never_creates_missing_index(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    registry = build_default_tools(("repo.symbols",))

    missing_project = registry.execute(
        ToolCall(name="repo.symbols", arguments={"query": "Widget"}),
        _context(tmp_path, repository, project_id=None),
    )
    missing_index = registry.execute(
        ToolCall(name="repo.symbols", arguments={"query": "Widget"}),
        _context(tmp_path, repository),
    )

    assert missing_project.error == "project_context_required"
    assert missing_index.error == "repo_index_missing"
    assert not (repository / ".nest").exists()


def test_repo_intelligence_returns_digest_bound_structural_evidence_with_path_ceiling(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    index = RepositoryIndex(project_id=PROJECT_ID, repository_root=repository)
    report = index.rebuild()
    registry = build_default_tools(
        (
            "repo.symbols",
            "repo.references",
            "repo.dependencies",
            "repo.tests_for",
            "repo.impact",
            "repo.context_pack",
        )
    )
    context = _context(tmp_path, repository, allowed_paths=("src",))

    symbols = registry.execute(
        ToolCall(name="repo.symbols", arguments={"query": "Widget"}),
        context,
    )
    references = registry.execute(
        ToolCall(name="repo.references", arguments={"name": "helper"}),
        context,
    )
    dependencies = registry.execute(
        ToolCall(name="repo.dependencies", arguments={"query": "deque"}),
        context,
    )
    context_pack = registry.execute(
        ToolCall(name="repo.context_pack", arguments={"query": "Widget"}),
        context,
    )

    assert symbols.success and symbols.data["authoritative"] is True
    assert symbols.data["index_digest"] == report.aggregate_digest
    assert ("Widget", "src/widget.py") in {
        (row["name"], row["path"]) for row in symbols.data["records"]
    }
    assert {row["path"] for row in symbols.data["records"]} == {"src/widget.py"}
    assert all(row["file_digest"] for row in symbols.data["records"])
    assert {(row["name"], row["path"]) for row in references.data["records"]} == {
        ("helper", "src/widget.py")
    }
    assert [(row["module"], row["path"]) for row in dependencies.data["records"]] == [
        ("collections.deque", "src/widget.py")
    ]
    assert context_pack.success and context_pack.data["authoritative"] is True
    assert "class Widget" in context_pack.data["context"]
    assert all(item["path"].startswith("src/") for item in context_pack.data["evidence"])

    full_context = _context(tmp_path, repository)
    tests_for = registry.execute(
        ToolCall(name="repo.tests_for", arguments={"symbol": "Widget"}),
        full_context,
    )
    impact = registry.execute(
        ToolCall(name="repo.impact", arguments={"symbol": "Widget"}),
        full_context,
    )
    assert tests_for.success
    assert tests_for.data["records"] == [
        {
            "symbol": "Widget",
            "symbol_path": "src/widget.py",
            "test_path": "tests/widget_checks.py",
            "relationship": "symbol_reference",
            "evidence_line": 1,
        }
    ]
    assert impact.success and impact.data["authoritative"] is True
    assert any(row["relation"] == "definition" for row in impact.data["records"])
    assert any(row["relation"] == "test" for row in impact.data["records"])


def test_repo_intelligence_hides_stale_rows_and_context(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    RepositoryIndex(project_id=PROJECT_ID, repository_root=repository).rebuild()
    registry = build_default_tools(("repo.symbols", "repo.context_pack"))
    context = _context(tmp_path, repository)
    (repository / "src" / "widget.py").write_text(
        "class Replacement:\n    pass\n",
        encoding="utf-8",
    )

    symbols = registry.execute(
        ToolCall(name="repo.symbols", arguments={"query": "Widget"}),
        context,
    )
    context_pack = registry.execute(
        ToolCall(name="repo.context_pack", arguments={"query": "Widget"}),
        context,
    )

    assert symbols.success
    assert symbols.data["freshness"] == "stale"
    assert symbols.data["authoritative"] is False
    assert symbols.data["records"] == []
    assert context_pack.success
    assert context_pack.data["authoritative"] is False
    assert context_pack.data["context"] == ""
    assert context_pack.data["evidence"] == []


def test_allowed_path_filter_does_not_leak_forbidden_matches(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    (repository / "public").mkdir(parents=True)
    (repository / "private").mkdir()
    (repository / "public" / "api.py").write_text(
        "class PublicApi: ...\n",
        encoding="utf-8",
    )
    (repository / "private" / "payroll.py").write_text(
        "class ConfidentialPayrollEngine: ...\n",
        encoding="utf-8",
    )
    report = RepositoryIndex(
        project_id=PROJECT_ID,
        repository_root=repository,
    ).rebuild()
    registry = build_default_tools(("repo.symbols",))
    context = _context(
        tmp_path,
        repository,
        allowed_paths=("public",),
        baseline_index_digest=report.aggregate_digest,
    )

    forbidden = registry.execute(
        ToolCall(
            name="repo.symbols",
            arguments={"query": "ConfidentialPayrollEngine"},
        ),
        context,
    )
    absent = registry.execute(
        ToolCall(name="repo.symbols", arguments={"query": "NameThatDoesNotExist"}),
        context,
    )

    assert forbidden.success and absent.success
    assert forbidden.data["records"] == absent.data["records"] == []
    assert forbidden.data["truncated"] == absent.data["truncated"] is False
    assert forbidden.data["next_offset"] == absent.data["next_offset"] is None


def test_repo_tools_require_project_bound_baseline_digest(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    report = RepositoryIndex(
        project_id=PROJECT_ID,
        repository_root=repository,
    ).rebuild()
    registry = build_default_tools(("repo.symbols",))

    missing = registry.execute(
        ToolCall(name="repo.symbols", arguments={"query": "Widget"}),
        _context(
            tmp_path,
            repository,
            baseline_index_digest="",
        ),
    )
    mismatched = registry.execute(
        ToolCall(name="repo.symbols", arguments={"query": "Widget"}),
        _context(
            tmp_path,
            repository,
            baseline_index_digest=f"{report.aggregate_digest}-unbound",
        ),
    )

    assert missing.error == "repo_index_rebuild_required"
    assert mismatched.error == "repo_index_rebuild_required"
