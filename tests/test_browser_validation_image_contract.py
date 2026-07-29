from __future__ import annotations

import json
import re
from pathlib import Path


def test_browser_validation_image_is_immutable_and_version_aligned() -> None:
    root = Path(__file__).resolve().parents[1] / "containers" / "browser-validation"
    package = json.loads((root / "package.json").read_text(encoding="utf-8"))
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    runner = (root / "browser-validate.mjs").read_text(encoding="utf-8")

    playwright_version = package["dependencies"]["playwright"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", playwright_version)
    assert package["dependencies"]["axe-core"] == "4.12.1"
    assert (
        f"mcr.microsoft.com/playwright:v{playwright_version}-noble@sha256:"
        in dockerfile
    )
    assert re.search(r"@sha256:[0-9a-f]{64}", dockerfile)
    assert "npm ci --omit=dev --ignore-scripts" in dockerfile
    assert "WORKDIR /opt/kestrel\nCOPY package.json package-lock.json ./" in dockerfile
    assert "/opt/kestrel/browser-validate" in dockerfile
    assert "kestrel.browser_validation.v1" in runner
    assert '"--no-sandbox"' in runner
    assert "network_fixtures" in runner
    assert "Page.captureScreenshot" in runner
