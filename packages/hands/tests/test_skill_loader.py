
import json
import tempfile
from pathlib import Path
import pytest
import server

@pytest.fixture(autouse=True)
def clean_skills():
    """Clear the global skills registry before each test."""
    server._skills.clear()
    yield
    server._skills.clear()

def test_load_skills_from_directory():
    """load_skills should discover skill manifests from a directory."""
    # Create temp skill directory with a manifest
    with tempfile.TemporaryDirectory() as tmpdir:
        skill_dir = Path(tmpdir) / "hello_skill"
        skill_dir.mkdir()

        manifest = {
            "name": "hello",
            "version": "1.0.0",
            "description": "A test skill",
            "entrypoint": "main.py",
            "permissions": ["network"],
        }
        (skill_dir / "manifest.json").write_text(json.dumps(manifest))
        (skill_dir / "main.py").write_text("print('hello')")

        skills = server.load_skills(tmpdir)

        assert len(skills) >= 1
        # server.py uses directory name as skill name
        hello = next((s for s in skills if s["name"] == "hello_skill"), None)
        assert hello is not None
        assert hello["manifest"]["name"] == "hello"

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
