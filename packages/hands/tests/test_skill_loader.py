
import json
import tempfile
from pathlib import Path
import pytest
import server


class _AllowAllPermissions:
    def check(self, *_args, **_kwargs):
        return True


class _NoopExecutor:
    active_sandboxes = 0
    max_concurrent = 1


class _NoopAudit:
    pass

@pytest.fixture(autouse=True)
def clean_skills():
    """Clear the global skills registry before each test."""
    server._skills.clear()
    yield
    server._skills.clear()

def test_load_skills_from_directory():
    """load_skills should discover skills from skill.json."""
    # Create temp skill directory with a skill manifest
    with tempfile.TemporaryDirectory() as tmpdir:
        skill_dir = Path(tmpdir) / "hello_skill"
        skill_dir.mkdir()

        manifest = {
            "name": "hello",
            "version": "1.0.0",
            "description": "A test skill",
            "functions": [
                {"name": "say_hello", "description": "Say hello", "parameters": {}}
            ],
            "capabilities": {"network": True, "filesystem": False},
        }
        (skill_dir / "skill.json").write_text(json.dumps(manifest))
        (skill_dir / "main.py").write_text("print('hello')")

        skills = server.load_skills(tmpdir)

        assert len(skills) >= 1
        # server.py uses directory name as skill name
        hello = next((s for s in skills if s["name"] == "hello_skill"), None)
        assert hello is not None
        assert hello["manifest"]["name"] == "hello"


def test_list_skills_maps_skill_json_fields():
    """ListSkills should expose functions and capability flags from skill.json."""
    with tempfile.TemporaryDirectory() as tmpdir:
        skill_dir = Path(tmpdir) / "json_skill"
        skill_dir.mkdir()

        manifest = {
            "name": "json_skill",
            "version": "2.0.0",
            "description": "Skill loaded from skill.json",
            "functions": [
                {
                    "name": "do_it",
                    "description": "Do the thing",
                    "parameters": {"type": "object"},
                }
            ],
            "capabilities": {"network": True, "filesystem": True},
        }
        (skill_dir / "skill.json").write_text(json.dumps(manifest))

        server.load_skills(tmpdir)

        servicer = server.HandsServicer(
            _NoopExecutor(),
            _AllowAllPermissions(),
            _NoopAudit(),
        )
        import asyncio
        response = asyncio.run(servicer.ListSkills(type("Req", (), {"workspace_id": "ws-1"})(), None))

        assert len(response["skills"]) == 1
        skill = response["skills"][0]
        assert skill["description"] == manifest["description"]
        assert skill["version"] == manifest["version"]
        assert skill["functions"][0]["name"] == "do_it"
        assert skill["requires_network"] is True
        assert skill["requires_filesystem"] is True


def test_load_skills_falls_back_to_manifest_json_when_missing_skill_json():
    """load_skills should keep supporting legacy manifest.json files."""
    with tempfile.TemporaryDirectory() as tmpdir:
        skill_dir = Path(tmpdir) / "legacy_skill"
        skill_dir.mkdir()
        (skill_dir / "manifest.json").write_text(json.dumps({"name": "legacy"}))

        skills = server.load_skills(tmpdir)

        assert len(skills) == 1
        assert skills[0]["manifest"]["name"] == "legacy"

def test_load_skills_skips_invalid():
    """load_skills should skip directories without valid manifests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create a directory without a manifest
        (Path(tmpdir) / "broken_skill").mkdir()
        (Path(tmpdir) / "broken_skill" / "README.md").write_text("not a skill")

        skills = server.load_skills(tmpdir)

        # Should not crash, just skip
        assert isinstance(skills, list)
        assert len(skills) == 0

def test_load_skills_empty_directory():
    """load_skills should return empty list for empty directory."""
    with tempfile.TemporaryDirectory() as tmpdir:
        skills = server.load_skills(tmpdir)
        assert skills == []
