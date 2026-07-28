from __future__ import annotations

import importlib.util
import json
import os
import plistlib
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


def _module() -> ModuleType:
    path = Path(__file__).parents[1] / "scripts" / "manage_user_launchers.py"
    spec = importlib.util.spec_from_file_location(
        "manage_user_launchers",
        path,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _kestrel_home(tmp_path: Path) -> Path:
    home = tmp_path / "install"
    executable = home / ".venv" / "bin" / "kestrel"
    executable.parent.mkdir(parents=True)
    executable.write_text(
        "#!/usr/bin/env bash\n"
        "printf '%s|%s\\n' \"$KESTREL_HOME\" \"$*\" > \"$KESTREL_TEST_CAPTURE\"\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return home


def test_bin_directory_selection_honors_explicit_path_then_path_then_fallback(
    tmp_path: Path,
) -> None:
    module = _module()
    user_home = tmp_path / "user"
    user_home.mkdir()
    explicit = user_home / "explicit-bin"
    path_bin = user_home / "path-bin"
    path_bin.mkdir()

    selected = module.select_bin_directory(
        explicit=explicit,
        user_home=user_home,
        environ={"PATH": str(path_bin)},
    )
    assert selected == explicit.resolve()
    assert explicit.is_dir()

    selected = module.select_bin_directory(
        explicit=None,
        user_home=user_home,
        environ={"PATH": str(path_bin)},
    )
    assert selected == path_bin.resolve()

    selected = module.select_bin_directory(
        explicit=None,
        user_home=user_home,
        environ={"PATH": str(tmp_path / "missing")},
    )
    assert selected == (user_home / ".local" / "bin").resolve()
    assert selected.is_dir()


@pytest.mark.parametrize("unsafe", ["relative", "symlink", "virtualenv", "app"])
def test_bin_directory_selection_rejects_unsafe_targets(
    tmp_path: Path,
    unsafe: str,
) -> None:
    module = _module()
    user_home = tmp_path / "user"
    user_home.mkdir()
    if unsafe == "relative":
        target = Path("relative-bin")
    elif unsafe == "symlink":
        real = user_home / "real-bin"
        real.mkdir()
        target = user_home / "linked-bin"
        target.symlink_to(real, target_is_directory=True)
    elif unsafe == "virtualenv":
        target = user_home / ".venv" / "bin"
    else:
        target = user_home / "Applications" / "Other.app" / "Contents" / "MacOS"

    with pytest.raises(module.LauncherArtifactError):
        module.select_bin_directory(
            explicit=target,
            user_home=user_home,
            environ={"PATH": ""},
        )


def test_prepare_creates_executable_managed_shim_that_forwards_exactly(
    tmp_path: Path,
) -> None:
    module = _module()
    home = _kestrel_home(tmp_path)
    user_home = tmp_path / "user"
    user_home.mkdir()
    bin_dir = user_home / "bin"
    manifest = home / ".nest" / "transactions" / "launchers.json"

    result = module.prepare_launchers(
        kestrel_home=home,
        user_home=user_home,
        manifest_path=manifest,
        bin_dir=bin_dir,
        platform="linux",
        environ={"PATH": str(bin_dir), "OPENAI_API_KEY": "secret-not-for-shim"},
    )

    shim = bin_dir / "kestrel"
    text = shim.read_text(encoding="utf-8")
    assert result["shim_path"] == str(shim.resolve())
    assert module.SHIM_MARKER in text
    assert str(home.resolve()) in text
    assert str(home.resolve() / ".venv" / "bin" / "kestrel") in text
    assert '"$@"' in text
    assert "secret-not-for-shim" not in text
    assert shim.stat().st_mode & 0o777 == 0o755
    assert manifest.stat().st_mode & 0o777 == 0o600

    capture = tmp_path / "capture.txt"
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "KESTREL_TEST_CAPTURE": str(capture),
    }
    completed = subprocess.run(
        [str(shim), "open", "--no-browser"],
        check=False,
        env=environment,
    )

    assert completed.returncode == 0
    assert capture.read_text(encoding="utf-8").strip() == (
        f"{home.resolve()}|open --no-browser"
    )


def test_prepare_refuses_unrelated_existing_shim_before_mutation(
    tmp_path: Path,
) -> None:
    module = _module()
    home = _kestrel_home(tmp_path)
    user_home = tmp_path / "user"
    bin_dir = user_home / "bin"
    bin_dir.mkdir(parents=True)
    unrelated = bin_dir / "kestrel"
    unrelated.write_text("#!/bin/sh\necho unrelated\n", encoding="utf-8")
    before = unrelated.read_bytes()
    manifest = home / ".nest" / "transactions" / "launchers.json"

    with pytest.raises(module.LauncherArtifactError, match="unrelated"):
        module.prepare_launchers(
            kestrel_home=home,
            user_home=user_home,
            manifest_path=manifest,
            bin_dir=bin_dir,
            platform="linux",
            environ={"PATH": str(bin_dir)},
        )

    assert unrelated.read_bytes() == before
    assert not manifest.exists()


def test_prepare_refuses_dangling_launcher_symlink_without_replacing_it(
    tmp_path: Path,
) -> None:
    module = _module()
    home = _kestrel_home(tmp_path)
    user_home = tmp_path / "user"
    bin_dir = user_home / "bin"
    bin_dir.mkdir(parents=True)
    shim = bin_dir / "kestrel"
    missing_target = tmp_path / "missing-unrelated-target"
    shim.symlink_to(missing_target)
    manifest = home / ".nest" / "transactions" / "launchers.json"

    with pytest.raises(module.LauncherArtifactError, match="symbolic-link"):
        module.prepare_launchers(
            kestrel_home=home,
            user_home=user_home,
            manifest_path=manifest,
            bin_dir=bin_dir,
            platform="linux",
            environ={"PATH": str(bin_dir)},
        )

    assert shim.is_symlink()
    assert shim.readlink() == missing_target
    assert not manifest.exists()


def test_darwin_prepare_creates_owned_parseable_app_with_static_recovery(
    tmp_path: Path,
) -> None:
    module = _module()
    home = _kestrel_home(tmp_path)
    user_home = tmp_path / "user"
    user_home.mkdir()
    bin_dir = user_home / "bin"
    manifest = home / ".nest" / "transactions" / "launchers.json"

    result = module.prepare_launchers(
        kestrel_home=home,
        user_home=user_home,
        manifest_path=manifest,
        bin_dir=bin_dir,
        platform="darwin",
        environ={"PATH": str(bin_dir)},
        which=lambda _name: None,
    )

    app = user_home / "Applications" / "Kestrel.app"
    plist_path = app / "Contents" / "Info.plist"
    executable = app / "Contents" / "MacOS" / "Kestrel"
    marker = app / "Contents" / "Resources" / module.APP_MARKER_FILENAME
    with plist_path.open("rb") as handle:
        plist = plistlib.load(handle)
    executable_text = executable.read_text(encoding="utf-8")

    assert result["app_path"] == str(app.resolve())
    assert result["codesign"] == "unavailable"
    assert plist["CFBundleIdentifier"] == "com.kestrel.local-launcher"
    assert plist["CFBundleExecutable"] == "Kestrel"
    assert marker.read_text(encoding="utf-8").strip() == module.APP_MARKER
    assert str((bin_dir / "kestrel").resolve()) in executable_text
    assert " open" in executable_text
    assert "osascript" in executable_text
    assert str(home.resolve() / ".nest" / "server.log") in executable_text
    assert "kestrel doctor" in executable_text
    assert executable.stat().st_mode & 0o777 == 0o755


def test_rollback_restores_prior_managed_artifacts_byte_for_byte(
    tmp_path: Path,
) -> None:
    module = _module()
    home = _kestrel_home(tmp_path)
    user_home = tmp_path / "user"
    user_home.mkdir()
    bin_dir = user_home / "bin"
    first_manifest = home / ".nest" / "transactions" / "first.json"
    module.prepare_launchers(
        kestrel_home=home,
        user_home=user_home,
        manifest_path=first_manifest,
        bin_dir=bin_dir,
        platform="darwin",
        environ={"PATH": str(bin_dir)},
        which=lambda _name: None,
    )
    module.commit_launchers(first_manifest)
    shim = bin_dir / "kestrel"
    app_executable = (
        user_home
        / "Applications"
        / "Kestrel.app"
        / "Contents"
        / "MacOS"
        / "Kestrel"
    )
    old_shim = shim.read_bytes()
    old_app_executable = app_executable.read_bytes()

    replacement_home = _kestrel_home(tmp_path / "replacement")
    second_manifest = home / ".nest" / "transactions" / "second.json"
    module.prepare_launchers(
        kestrel_home=replacement_home,
        user_home=user_home,
        manifest_path=second_manifest,
        bin_dir=bin_dir,
        platform="darwin",
        environ={"PATH": str(bin_dir)},
        which=lambda _name: None,
    )
    assert replacement_home.resolve().as_posix() in shim.read_text(encoding="utf-8")

    module.rollback_launchers(second_manifest)

    assert shim.read_bytes() == old_shim
    assert app_executable.read_bytes() == old_app_executable
    assert not second_manifest.exists()


def test_commit_removes_backups_and_manifest_but_keeps_new_artifacts(
    tmp_path: Path,
) -> None:
    module = _module()
    home = _kestrel_home(tmp_path)
    user_home = tmp_path / "user"
    user_home.mkdir()
    bin_dir = user_home / "bin"
    manifest = home / ".nest" / "transactions" / "launchers.json"
    module.prepare_launchers(
        kestrel_home=home,
        user_home=user_home,
        manifest_path=manifest,
        bin_dir=bin_dir,
        platform="linux",
        environ={"PATH": str(bin_dir)},
    )
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    backup_dir = Path(payload["backup_dir"])

    module.commit_launchers(manifest)

    assert (bin_dir / "kestrel").is_file()
    assert not backup_dir.exists()
    assert not manifest.exists()


def test_prepare_rolls_back_if_app_install_fails_after_shim_install(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    home = _kestrel_home(tmp_path)
    user_home = tmp_path / "user"
    user_home.mkdir()
    bin_dir = user_home / "bin"
    manifest = home / ".nest" / "transactions" / "launchers.json"
    original = module._install_prepared_artifact
    calls = 0

    def fail_second(*args: Any, **kwargs: Any) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated app installation failure")
        original(*args, **kwargs)

    monkeypatch.setattr(module, "_install_prepared_artifact", fail_second)

    with pytest.raises(module.LauncherArtifactError, match="rolled back"):
        module.prepare_launchers(
            kestrel_home=home,
            user_home=user_home,
            manifest_path=manifest,
            bin_dir=bin_dir,
            platform="darwin",
            environ={"PATH": str(bin_dir)},
            which=lambda _name: None,
        )

    assert not (bin_dir / "kestrel").exists()
    assert not (user_home / "Applications" / "Kestrel.app").exists()
    assert not manifest.exists()
