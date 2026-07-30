from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DESKTOP = ROOT / "desktop"


def test_developer_directory_builder_is_exactly_pinned_and_source_gated() -> None:
    package = json.loads((DESKTOP / "package.json").read_text(encoding="utf-8"))
    lock = json.loads((DESKTOP / "package-lock.json").read_text(encoding="utf-8"))

    assert package["devDependencies"]["electron-builder"] == "26.15.3"
    assert package["scripts"]["build:dir"] == "node scripts/build-dir.mjs"
    assert package["scripts"]["test:build-dir"] == (
        "vitest run --config vitest.build-dir.config.ts"
    )
    assert package["scripts"]["audit:reviewed"] == (
        "node scripts/audit-reviewed.mjs"
    )
    assert "overrides" not in package
    assert lock["packages"][""]["devDependencies"]["electron-builder"] == "26.15.3"
    assert lock["packages"]["node_modules/electron-builder"]["version"] == "26.15.3"
    assert (DESKTOP / "scripts" / "build-dir.mjs").is_file()
    assert (DESKTOP / "scripts" / "build-dir.test.mjs").is_file()
    assert (DESKTOP / "scripts" / "audit-reviewed.mjs").is_file()
    assert (DESKTOP / "scripts" / "audit-reviewed.test.mjs").is_file()
    assert (DESKTOP / "electron-builder.developer.yml").is_file()

    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    assert "Validate developer directory bundle contracts" in workflow
    assert "npm run test:build-dir" in workflow
    assert "npm run audit:reviewed" in workflow
    assert "actions/upload-artifact" not in workflow[
        workflow.index("Validate developer directory bundle contracts") :
    ].split("  codeql:", 1)[0]


def test_developer_builder_config_has_no_release_or_publication_targets() -> None:
    config = json.loads(
        (DESKTOP / "electron-builder.developer.yml").read_text(encoding="utf-8")
    )

    assert config == {
        "appId": "dev.kestrel.desktop",
        "productName": "Kestrel Developer",
        "asar": False,
        "npmRebuild": False,
        "removePackageKeywords": False,
        "removePackageScripts": False,
        "directories": {
            "output": "__VERIFIED_DIRECTORY_OUTPUT__",
        },
        "electronVersion": "43.2.0",
        "files": [
            "dist/**/*",
            "package.json",
            "config/desktop-developer-public-key.pem",
        ],
        "extraResources": [
            {
                "from": "__VERIFIED_STAGE_RESOURCE_ROOT__",
                "to": "kestrel",
            },
        ],
        "mac": {
            "target": ["dir"],
            "identity": None,
            "hardenedRuntime": False,
            "gatekeeperAssess": False,
        },
        "win": {
            "target": ["dir"],
            "signAndEditExecutable": False,
        },
        "linux": {"target": ["dir"]},
    }
