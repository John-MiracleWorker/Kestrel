#!/usr/bin/env python3
"""Fail when Kestrel release metadata or compatibility identities drift."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PYTHON_DISTRIBUTION = "nested-memvid-agent"
WEB_PACKAGE = "kestrel-web"
PUBLISHED_RELEASE = "0.4.0"


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"expected an object in {path.relative_to(ROOT)}")
    return payload


def main() -> int:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    uv_lock = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    web_package = _load_json(ROOT / "web" / "package.json")
    web_lock = _load_json(ROOT / "web" / "package-lock.json")
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    deployment = (ROOT / "docs" / "DEPLOYMENT.md").read_text(encoding="utf-8")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    security_policy = (ROOT / "SECURITY.md").read_text(encoding="utf-8")

    project = pyproject["project"]
    version = str(project["version"])
    locked_python = {
        str(package["name"]): str(package["version"])
        for package in uv_lock["package"]
        if isinstance(package, dict) and "name" in package and "version" in package
    }
    web_root = web_lock.get("packages", {}).get("", {})
    release_series = ".".join(PUBLISHED_RELEASE.split(".")[:2]) + ".x"
    security_support_row = f"| Latest `{release_series}` patch | Yes |"
    stable_tag = f"v{PUBLISHED_RELEASE}"
    stable_installer = (
        "https://github.com/John-MiracleWorker/Kestrel/releases/download/"
        f"{stable_tag}/install.sh"
    )
    development_installer = (
        "https://github.com/John-MiracleWorker/Kestrel/releases/download/"
        f"v{version}/install.sh"
    )
    is_current_release = version == PUBLISHED_RELEASE
    release_marker = f"`v{version}` is the current stable release"
    unreleased_marker = f"unreleased `v{version}` development line"
    expected_release_marker = release_marker if is_current_release else unreleased_marker
    unreleased_compare = f"[Unreleased]: https://github.com/John-MiracleWorker/Kestrel/compare/{stable_tag}...HEAD"

    expected = {
        "Python distribution name": (str(project["name"]), PYTHON_DISTRIBUTION),
        "Python lock version": (locked_python.get(PYTHON_DISTRIBUTION, "<missing>"), version),
        "web package name": (str(web_package.get("name", "<missing>")), WEB_PACKAGE),
        "web package version": (str(web_package.get("version", "<missing>")), version),
        "web lock name": (str(web_lock.get("name", "<missing>")), WEB_PACKAGE),
        "web lock version": (str(web_lock.get("version", "<missing>")), version),
        "web lock root name": (str(web_root.get("name", "<missing>")), WEB_PACKAGE),
        "web lock root version": (str(web_root.get("version", "<missing>")), version),
        "security policy release line": (
            security_support_row if security_support_row in security_policy else "<missing>",
            security_support_row,
        ),
        "README release-state marker": (
            expected_release_marker if expected_release_marker in readme else "<missing>",
            expected_release_marker,
        ),
        "deployment release-state marker": (
            expected_release_marker if expected_release_marker in deployment else "<missing>",
            expected_release_marker,
        ),
        "README stable installer": (
            stable_installer if stable_installer in readme else "<missing>",
            stable_installer,
        ),
        "deployment stable installer": (
            stable_installer if stable_installer in deployment else "<missing>",
            stable_installer,
        ),
        "changelog unreleased base": (
            unreleased_compare if unreleased_compare in changelog else "<missing>",
            unreleased_compare,
        ),
    }
    errors = [f"{label}: found {actual!r}, expected {wanted!r}" for label, (actual, wanted) in expected.items() if actual != wanted]
    if not is_current_release:
        for label, document in (("README", readme), ("deployment", deployment)):
            if development_installer in document:
                errors.append(
                    f"{label} advertises unavailable development installer {development_installer!r}"
                )
    if errors:
        raise SystemExit("Kestrel project metadata drift:\n- " + "\n- ".join(errors))

    release_state = "release" if is_current_release else "development"
    print(
        f"Kestrel metadata aligned: {PYTHON_DISTRIBUTION} {version} {release_state}; "
        f"published release {stable_tag}; private web package {WEB_PACKAGE} {version}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
