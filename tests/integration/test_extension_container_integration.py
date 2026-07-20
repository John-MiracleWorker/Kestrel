from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from nested_memvid_agent.extension_policy import (
    ExtensionPolicyError,
    extension_tree_digest,
    parse_extension_scopes,
)
from nested_memvid_agent.extension_runner import ContainerExecutionRequest, OCIContainerRunner

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_EXTENSION_SANDBOX_INTEGRATION") != "1",
    reason="set RUN_EXTENSION_SANDBOX_INTEGRATION=1 with a preloaded digest-pinned test image",
)


def test_real_container_denies_host_paths_network_and_root_identity(tmp_path: Path) -> None:
    image = _test_image()
    source = tmp_path / "skill"
    source.mkdir()
    (source / "probe.py").write_text(
        "\n".join(
            [
                "import json, os, socket",
                "from pathlib import Path",
                "host_visible = Path('/workspace/private-sentinel').exists()",
                "network_blocked = False",
                "try:",
                "    socket.create_connection(('1.1.1.1', 443), timeout=0.25)",
                "except OSError:",
                "    network_blocked = True",
                "print(json.dumps({",
                "    'host_visible': host_visible,",
                "    'network_blocked': network_blocked,",
                "    'uid': os.getuid(),",
                "    'home': os.environ.get('HOME'),",
                "}))",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "private-sentinel").write_text("private", encoding="utf-8")

    result = OCIContainerRunner().run(
        ContainerExecutionRequest(
            extension_id="integration-probe",
            source_dir=source,
            expected_tree_digest=extension_tree_digest(source),
            workspace=workspace,
            scopes=parse_extension_scopes({}),
            image=image,
            command=("python", "/extension/probe.py"),
            stdin="{}",
            timeout_seconds=5,
        )
    )

    assert result.success is True, result.stderr
    payload = json.loads(result.stdout)
    assert payload == {
        "host_visible": False,
        "network_blocked": True,
        "uid": _expected_non_root_uid(),
        "home": "/tmp",
    }


def test_real_container_read_scope_is_snapshotted_and_read_only(tmp_path: Path) -> None:
    image = _test_image()
    source = tmp_path / "skill"
    source.mkdir()
    (source / "read.py").write_text(
        "\n".join(
            [
                "import json",
                "from pathlib import Path",
                "value = Path('/workspace/input/value.txt').read_text(encoding='utf-8')",
                "scope_write_blocked = False",
                "try:",
                "    Path('/workspace/input/result.txt').write_text('no', encoding='utf-8')",
                "except OSError:",
                "    scope_write_blocked = True",
                "rootfs_blocked = False",
                "try:",
                "    Path('/forbidden').write_text('no', encoding='utf-8')",
                "except OSError:",
                "    rootfs_blocked = True",
                "print(json.dumps({",
                "    'value': value,",
                "    'scope_write_blocked': scope_write_blocked,",
                "    'rootfs_blocked': rootfs_blocked,",
                "}))",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    inputs = workspace / "input"
    inputs.mkdir(parents=True)
    (inputs / "value.txt").write_text("original", encoding="utf-8")
    with pytest.raises(ExtensionPolicyError, match="extension_write_scope_unsupported"):
        parse_extension_scopes(
            {
                "filesystem": [
                    {"root": "workspace", "path": "input", "access": "write"}
                ]
            }
        )
    scopes = parse_extension_scopes(
        {"filesystem": [{"root": "workspace", "path": "input", "access": "read"}]}
    )

    result = OCIContainerRunner().run(
        ContainerExecutionRequest(
            extension_id="integration-read",
            source_dir=source,
            expected_tree_digest=extension_tree_digest(source),
            workspace=workspace,
            scopes=scopes,
            image=image,
            command=("python", "/extension/read.py"),
            stdin="{}",
            timeout_seconds=5,
        )
    )

    assert result.success is True, result.stderr
    assert json.loads(result.stdout) == {
        "value": "original",
        "scope_write_blocked": True,
        "rootfs_blocked": True,
    }
    assert (inputs / "value.txt").read_text(encoding="utf-8") == "original"
    assert not (inputs / "result.txt").exists()
    assert not (workspace / "forbidden").exists()


def test_real_container_timeout_leaves_no_orphan(tmp_path: Path) -> None:
    image = _test_image()
    source = tmp_path / "skill"
    source.mkdir()
    (source / "hang.py").write_text(
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "print('started', flush=True)\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = OCIContainerRunner().run(
        ContainerExecutionRequest(
            extension_id="integration-timeout",
            source_dir=source,
            expected_tree_digest=extension_tree_digest(source),
            workspace=workspace,
            scopes=parse_extension_scopes({}),
            image=image,
            command=("python", "/extension/hang.py"),
            stdin="{}",
            timeout_seconds=0.2,
        )
    )
    time.sleep(0.5)

    assert result.error == "extension_timeout"
    probe = subprocess.run(  # noqa: S603  # nosec B603
        [
            "docker",
            "ps",
            "-a",
            "--filter",
            "name=kestrel-skill-integration-timeout-",
            "--format={{.Names}}",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert probe.returncode == 0
    assert probe.stdout.strip() == ""


def _test_image() -> str:
    image = os.getenv("KESTREL_EXTENSION_TEST_IMAGE", "")
    if "@sha256:" not in image:
        pytest.fail(
            "RUN_EXTENSION_SANDBOX_INTEGRATION=1 requires a preloaded digest-pinned "
            "KESTREL_EXTENSION_TEST_IMAGE",
            pytrace=False,
        )
    if shutil.which("docker") is None:
        pytest.fail(
            "RUN_EXTENSION_SANDBOX_INTEGRATION=1 requires the docker executable",
            pytrace=False,
        )
    return image


def _expected_non_root_uid() -> int:
    uid = os.getuid() if hasattr(os, "getuid") else 65532
    return 65532 if uid == 0 else uid
