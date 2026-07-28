from __future__ import annotations

import importlib.util
import json
import os
import plistlib
import shutil
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


def test_prepare_persists_only_supported_runtime_profile_values(
    tmp_path: Path,
) -> None:
    module = _module()
    home = _kestrel_home(tmp_path)
    executable = home / ".venv" / "bin" / "kestrel"
    executable.write_text(
        "#!/bin/bash\n"
        "printf '%s|%s|%s|%s\\n' "
        '"$KESTREL_HOME" "$KESTREL_PORT" "$NEST_AGENT_STATE_PATH" '
        '"$NEST_AGENT_MEMORY_DIR" > "$KESTREL_TEST_CAPTURE"\n',
        encoding="utf-8",
    )
    executable.chmod(0o755)
    user_home = tmp_path / "user"
    user_home.mkdir()
    bin_dir = user_home / "bin"
    state_path = home / ".nest" / "custom-state" / "agent.db"
    memory_dir = home / ".nest" / "custom-memory"
    manifest = home / ".nest" / "transactions" / "launchers.json"

    module.prepare_launchers(
        kestrel_home=home,
        user_home=user_home,
        manifest_path=manifest,
        bin_dir=bin_dir,
        platform="linux",
        port=19421,
        state_path=state_path,
        memory_dir=memory_dir,
        environ={
            "PATH": str(bin_dir),
            "OPENAI_API_KEY": "secret-not-for-shim",
            "KESTREL_SERVER_PID": "/secret/lifecycle/path",
        },
    )

    shim = bin_dir / "kestrel"
    text = shim.read_text(encoding="utf-8")
    assert "export KESTREL_PORT=19421" in text
    assert f"export NEST_AGENT_STATE_PATH={state_path}" in text
    assert f"export NEST_AGENT_MEMORY_DIR={memory_dir}" in text
    assert "secret-not-for-shim" not in text
    assert "KESTREL_SERVER_PID" not in text

    capture = tmp_path / "profile-capture.txt"
    completed = subprocess.run(
        [str(shim), "status"],
        check=False,
        env={
            "PATH": os.environ.get("PATH", ""),
            "KESTREL_TEST_CAPTURE": str(capture),
            "KESTREL_PORT": "9999",
            "NEST_AGENT_STATE_PATH": "/caller/state.db",
        },
    )

    assert completed.returncode == 0
    assert capture.read_text(encoding="utf-8").strip() == (
        f"{home.resolve()}|19421|{state_path}|{memory_dir}"
    )


def test_plan_is_non_mutating_and_uses_the_same_path_and_collision_rules(
    tmp_path: Path,
) -> None:
    module = _module()
    user_home = tmp_path / "user"
    user_home.mkdir()
    path_bin = user_home / "preferred-path-bin"
    path_bin.mkdir()
    intended_home = user_home / "future-kestrel-home"
    before = sorted(path.relative_to(user_home) for path in user_home.rglob("*"))

    result = module.plan_launchers(
        kestrel_home=intended_home,
        user_home=user_home,
        platform="darwin",
        environ={"PATH": str(path_bin)},
    )

    assert result["shim_path"] == str(path_bin / "kestrel")
    assert result["app_path"] == str(user_home / "Applications" / "Kestrel.app")
    assert result["bin_on_path"] is True
    assert sorted(path.relative_to(user_home) for path in user_home.rglob("*")) == before
    assert not (user_home / ".local").exists()
    assert not (user_home / "Applications").exists()
    assert not intended_home.exists()

    unrelated = path_bin / "kestrel"
    unrelated.write_text("#!/bin/sh\necho unrelated\n", encoding="utf-8")
    original = unrelated.read_bytes()
    with pytest.raises(module.LauncherArtifactError, match="unrelated"):
        module.plan_launchers(
            kestrel_home=intended_home,
            user_home=user_home,
            platform="linux",
            environ={"PATH": str(path_bin)},
        )
    assert unrelated.read_bytes() == original


def test_plan_fallback_does_not_create_the_fallback_directory(tmp_path: Path) -> None:
    module = _module()
    user_home = tmp_path / "user"
    user_home.mkdir()

    result = module.plan_launchers(
        kestrel_home=user_home / "future-kestrel-home",
        user_home=user_home,
        platform="linux",
        environ={"PATH": str(tmp_path / "missing")},
    )

    assert result["shim_path"] == str(user_home / ".local" / "bin" / "kestrel")
    assert result["bin_on_path"] is False
    assert not (user_home / ".local").exists()


def test_plan_baseline_proves_no_public_mutation_without_manifest(
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
        platform="linux",
        environ={"PATH": str(bin_dir)},
    )
    module.commit_launchers(first_manifest)
    plan = module.plan_launchers(
        kestrel_home=home,
        user_home=user_home,
        bin_dir=bin_dir,
        platform="linux",
        environ={"PATH": str(bin_dir)},
    )

    assert module.verify_plan_unchanged(json.dumps(plan)) == {"unchanged": True}

    replacement_home = _kestrel_home(tmp_path / "replacement")
    replacement_manifest = home / ".nest" / "transactions" / "replacement.json"
    module.prepare_launchers(
        kestrel_home=replacement_home,
        user_home=user_home,
        manifest_path=replacement_manifest,
        bin_dir=bin_dir,
        platform="linux",
        environ={"PATH": str(bin_dir)},
    )
    replacement_manifest.unlink()
    with pytest.raises(module.LauncherArtifactError, match="identity changed"):
        module.verify_plan_unchanged(json.dumps(plan))


def test_prepare_persists_manifest_before_any_public_launcher_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    home = _kestrel_home(tmp_path)
    user_home = tmp_path / "user"
    user_home.mkdir()
    bin_dir = user_home / "bin"
    manifest = home / ".nest" / "transactions" / "launchers.json"

    def fail_manifest(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated durable manifest failure")

    monkeypatch.setattr(module, "_write_manifest", fail_manifest)
    with pytest.raises(OSError, match="durable manifest"):
        module.prepare_launchers(
            kestrel_home=home,
            user_home=user_home,
            manifest_path=manifest,
            bin_dir=bin_dir,
            platform="linux",
            environ={"PATH": str(bin_dir)},
        )

    assert not (bin_dir / "kestrel").exists()
    assert not manifest.exists()


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
    module.commit_launchers(manifest)

    assert (bin_dir / "kestrel").is_file()
    assert not list(bin_dir.glob(".kestrel-backup-*"))
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


def test_generated_launchers_use_fixed_bash_and_forward_exit_status(
    tmp_path: Path,
) -> None:
    module = _module()
    home = _kestrel_home(tmp_path)
    executable = home / ".venv" / "bin" / "kestrel"
    executable.write_text("#!/bin/bash\nexit 37\n", encoding="utf-8")
    executable.chmod(0o755)
    user_home = tmp_path / "user"
    user_home.mkdir()
    bin_dir = user_home / "bin"
    manifest = home / ".nest" / "transactions" / "launchers.json"

    module.prepare_launchers(
        kestrel_home=home, user_home=user_home, manifest_path=manifest,
        bin_dir=bin_dir, platform="darwin", environ={"PATH": str(bin_dir)},
        which=lambda _name: None,
    )

    shim = bin_dir / "kestrel"
    app_executable = user_home / "Applications" / "Kestrel.app" / "Contents" / "MacOS" / "Kestrel"
    assert shim.read_text(encoding="utf-8").splitlines()[0] == "#!/bin/bash"
    assert app_executable.read_text(encoding="utf-8").splitlines()[0] == "#!/bin/bash"
    assert subprocess.run([str(shim)], check=False).returncode == 37


def test_staging_and_backup_are_per_artifact_target_parent_and_replace_is_dirfd_relative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    home = _kestrel_home(tmp_path)
    user_home = tmp_path / "user"
    user_home.mkdir()
    bin_dir = user_home / "bin"
    manifest = home / ".nest" / "transactions" / "launchers.json"
    replacements: list[tuple[object, object]] = []
    original_replace = module.os.replace

    def record_replace(src: object, dst: object, **kwargs: object) -> None:
        replacements.append((kwargs.get("src_dir_fd"), kwargs.get("dst_dir_fd")))
        original_replace(src, dst, **kwargs)

    monkeypatch.setattr(module.os, "replace", record_replace)
    module.prepare_launchers(
        kestrel_home=home, user_home=user_home, manifest_path=manifest,
        bin_dir=bin_dir, platform="linux", environ={"PATH": str(bin_dir)},
    )
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    artifact = payload["artifacts"][0]
    assert Path(artifact["staged"]).parent == bin_dir
    assert Path(artifact["backup"]).parent == bin_dir
    assert any(left is not None and left == right for left, right in replacements)


def test_tampered_manifest_cannot_delete_marker_containing_arbitrary_file(
    tmp_path: Path,
) -> None:
    module = _module()
    home = _kestrel_home(tmp_path)
    user_home = tmp_path / "user"
    user_home.mkdir()
    bin_dir = user_home / "bin"
    manifest = home / ".nest" / "transactions" / "launchers.json"
    module.prepare_launchers(
        kestrel_home=home, user_home=user_home, manifest_path=manifest,
        bin_dir=bin_dir, platform="linux", environ={"PATH": str(bin_dir)},
    )
    victim = tmp_path / "victim"
    victim.write_text("# KESTREL_MANAGED_COMMAND_SHIM_V1\ndo not delete\n", encoding="utf-8")
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["artifacts"][0]["target"] = str(victim)
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    manifest.chmod(0o600)

    with pytest.raises(module.LauncherArtifactError, match="schema|manifest"):
        module.commit_launchers(manifest)
    assert victim.exists()
    assert victim.read_text(encoding="utf-8").endswith("do not delete\n")


def test_final_mutation_parent_replacement_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    home = _kestrel_home(tmp_path)
    user_home = tmp_path / "user"
    user_home.mkdir()
    bin_dir = user_home / "bin"
    manifest = home / ".nest" / "transactions" / "launchers.json"
    raced = False

    def race(parent: Path) -> None:
        nonlocal raced
        if raced:
            return
        raced = True
        moved = parent.with_name("bin-original")
        parent.rename(moved)
        parent.symlink_to(tmp_path / "attacker", target_is_directory=True)

    monkeypatch.setattr(module, "_before_final_mutation", race)
    with pytest.raises(module.LauncherArtifactError, match="changed"):
        module.prepare_launchers(
            kestrel_home=home, user_home=user_home, manifest_path=manifest,
            bin_dir=bin_dir, platform="linux", environ={"PATH": str(bin_dir)},
        )
    assert not (tmp_path / "attacker" / "kestrel").exists()


def test_var_compatibility_is_lexical_and_user_symlink_is_still_rejected(
    tmp_path: Path,
) -> None:
    module = _module()
    # macOS exposes this test directory through both /var and /private/var.
    assert str(tmp_path).startswith("/private/var/")
    user_home = tmp_path / "user"
    user_home.mkdir()
    alias_home = Path("/var") / user_home.relative_to("/private/var")
    assert module._canonical_path(alias_home) == user_home
    selected = module.select_bin_directory(
        explicit=Path("/var") / (user_home / "bin").relative_to("/private/var"),
        user_home=alias_home, environ={"PATH": ""},
    )
    assert selected == user_home / "bin"
    linked = tmp_path / "linked-user"
    linked.symlink_to(user_home, target_is_directory=True)
    with pytest.raises(module.LauncherArtifactError, match="symbolic links"):
        module.select_bin_directory(explicit=user_home / "other", user_home=linked, environ={"PATH": ""})


def test_bin_directory_rejects_regular_file_and_external_temp_target(tmp_path: Path) -> None:
    module = _module()
    user_home = tmp_path / "user"
    user_home.mkdir()
    not_a_directory = user_home / "not-bin"
    not_a_directory.write_text("no", encoding="utf-8")
    with pytest.raises(module.LauncherArtifactError, match="not a directory"):
        module.select_bin_directory(explicit=not_a_directory, user_home=user_home, environ={"PATH": ""})
    with pytest.raises(module.LauncherArtifactError, match="temporary"):
        module.select_bin_directory(explicit=tmp_path / "external-bin", user_home=user_home, environ={"PATH": ""})
    with pytest.raises(module.LauncherArtifactError, match="not owned"):
        module.select_bin_directory(explicit=user_home / "wrong-owner", user_home=user_home,
                                    environ={"PATH": ""}, current_uid=os.getuid() + 1)


def test_app_collision_and_static_recovery_never_accept_or_embed_a_secret(tmp_path: Path) -> None:
    module = _module()
    home = _kestrel_home(tmp_path)
    user_home = tmp_path / "user"
    collision = user_home / "Applications" / "Kestrel.app" / "Contents" / "Resources"
    collision.mkdir(parents=True)
    (collision / module.APP_MARKER_FILENAME).write_text(f"prefix {module.APP_MARKER} suffix\n", encoding="utf-8")
    manifest = home / ".nest" / "transactions" / "collision.json"
    with pytest.raises(module.LauncherArtifactError, match="unrelated"):
        module.prepare_launchers(kestrel_home=home, user_home=user_home, manifest_path=manifest,
                                 bin_dir=user_home / "bin", platform="darwin",
                                 environ={"PATH": "", "OPENAI_API_KEY": "do-not-embed"}, which=lambda _name: None)

    # A fresh app has only static recovery text; environment secrets must not leak into it.
    shutil.rmtree(user_home / "Applications" / "Kestrel.app")
    module.prepare_launchers(kestrel_home=home, user_home=user_home, manifest_path=manifest,
                             bin_dir=user_home / "bin", platform="darwin",
                             environ={"PATH": "", "OPENAI_API_KEY": "do-not-embed"}, which=lambda _name: None)
    app_text = "\n".join(path.read_text(encoding="utf-8", errors="ignore")
                         for path in (user_home / "Applications" / "Kestrel.app").rglob("*") if path.is_file())
    assert "do-not-embed" not in app_text


def test_codesign_reports_verified_and_refuses_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    calls: list[list[str]] = []

    def success(args: list[str], **_kwargs: object) -> None:
        calls.append(args)

    monkeypatch.setattr(module.subprocess, "run", success)
    assert module._codesign_app(tmp_path / "Kestrel.app", which=lambda _name: "/usr/bin/codesign") == "verified"
    assert calls[0][1:5] == ["--force", "--deep", "--sign", "-"]
    assert calls[1][1:4] == ["--verify", "--deep", "--strict"]

    def failure(*_args: object, **_kwargs: object) -> None:
        raise subprocess.CalledProcessError(1, "codesign")

    monkeypatch.setattr(module.subprocess, "run", failure)
    with pytest.raises(module.LauncherArtifactError, match="signing verification"):
        module._codesign_app(tmp_path / "Kestrel.app", which=lambda _name: "/usr/bin/codesign")


def test_commit_removes_real_same_parent_backup_after_replacement(tmp_path: Path) -> None:
    module = _module()
    first_home = _kestrel_home(tmp_path)
    user_home = tmp_path / "user"
    user_home.mkdir()
    bin_dir = user_home / "bin"
    first_manifest = first_home / ".nest" / "transactions" / "first.json"
    module.prepare_launchers(kestrel_home=first_home, user_home=user_home, manifest_path=first_manifest,
                             bin_dir=bin_dir, platform="linux", environ={"PATH": str(bin_dir)})
    module.commit_launchers(first_manifest)
    replacement_home = _kestrel_home(tmp_path / "replacement")
    second_manifest = first_home / ".nest" / "transactions" / "second.json"
    module.prepare_launchers(kestrel_home=replacement_home, user_home=user_home, manifest_path=second_manifest,
                             bin_dir=bin_dir, platform="linux", environ={"PATH": str(bin_dir)})
    payload = json.loads(second_manifest.read_text(encoding="utf-8"))
    backup = Path(payload["artifacts"][0]["backup"])
    assert backup.is_file() and backup.parent == bin_dir
    module.commit_launchers(second_manifest)
    assert not backup.exists()
    assert not list(bin_dir.glob(".kestrel-backup-*"))


def _interrupted_replacement(module: ModuleType, tmp_path: Path) -> tuple[dict[str, Any], Path, Path, Path]:
    original_home = _kestrel_home(tmp_path)
    user_home = tmp_path / "user"
    user_home.mkdir()
    bin_dir = user_home / "bin"
    initial_manifest = original_home / ".nest" / "transactions" / "initial.json"
    module.prepare_launchers(kestrel_home=original_home, user_home=user_home, manifest_path=initial_manifest,
                             bin_dir=bin_dir, platform="linux", environ={"PATH": str(bin_dir)})
    module.commit_launchers(initial_manifest)
    replacement_home = _kestrel_home(tmp_path / "replacement")
    manifest = original_home / ".nest" / "transactions" / "interrupted.json"
    transaction_id = "a" * 32
    artifacts = module._derived_artifacts(bin_dir, user_home, "linux", transaction_id)
    artifact = artifacts[0]
    artifact["had_previous"] = True
    previous = os.lstat(artifact["target"])
    artifact["previous_device"] = previous.st_dev
    artifact["previous_inode"] = previous.st_ino
    module._write_staged_shim(
        artifact, module._shim_text(replacement_home, transaction_id),
        uid=os.getuid(),
    )
    expected = os.lstat(artifact["staged"])
    artifact["expected_device"] = expected.st_dev
    artifact["expected_inode"] = expected.st_ino
    payload: dict[str, Any] = {
        "schema": module.MANIFEST_SCHEMA, "transaction_id": transaction_id,
        "kestrel_home": str(replacement_home), "user_home": str(user_home),
        "platform": "linux", "bin_dir": str(bin_dir), "artifacts": artifacts,
        "codesign": "not_applicable", "phase": "prepared",
    }
    module._write_manifest(manifest, payload)
    return payload, manifest, bin_dir / "kestrel", replacement_home


@pytest.mark.parametrize("boundary", ["initial", "after_backup", "after_install"])
def test_rollback_recovers_each_crash_boundary_without_losing_predecessor(
    tmp_path: Path, boundary: str,
) -> None:
    module = _module()
    payload, manifest, target, _replacement_home = _interrupted_replacement(module, tmp_path)
    old_bytes = target.read_bytes()
    artifact = payload["artifacts"][0]
    staged, backup = Path(artifact["staged"]), Path(artifact["backup"])
    if boundary in {"after_backup", "after_install"}:
        os.replace(target, backup)
    if boundary == "after_install":
        # The backup status reached disk, but the post-install manifest update did not.
        artifact["backed_up"] = True
        module._write_manifest(manifest, payload)
        os.replace(staged, target)

    module.rollback_launchers(manifest)

    assert target.read_bytes() == old_bytes
    assert not staged.exists()
    assert not backup.exists()
    assert not manifest.exists()


def test_commit_refuses_initial_crash_manifest_without_mutating_artifacts(tmp_path: Path) -> None:
    module = _module()
    payload, manifest, target, _replacement_home = _interrupted_replacement(module, tmp_path)
    old_bytes = target.read_bytes()
    stage = Path(payload["artifacts"][0]["staged"])
    with pytest.raises(module.LauncherArtifactError, match="incomplete"):
        module.commit_launchers(manifest)
    assert target.read_bytes() == old_bytes
    assert stage.exists()
    assert manifest.exists()


def _replace_quarantine_with_marker(parent: Path) -> None:
    quarantines = list(parent.glob(".kestrel-quarantine-*")) + list(parent.glob(".*.tombstone"))
    assert len(quarantines) == 1
    quarantine = quarantines[0]
    quarantine.rename(quarantine.with_name(quarantine.name + ".preserved"))
    quarantine.write_text("attacker marker must survive", encoding="utf-8")


def test_final_delete_race_preserves_swapped_shim_and_quarantine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    home = _kestrel_home(tmp_path)
    user_home = tmp_path / "user"
    user_home.mkdir()
    bin_dir = user_home / "bin"
    manifest = home / ".nest" / "transactions" / "launchers.json"
    module.prepare_launchers(kestrel_home=home, user_home=user_home, manifest_path=manifest,
                             bin_dir=bin_dir, platform="linux", environ={"PATH": str(bin_dir)})
    monkeypatch.setattr(module, "_before_quarantine_delete", _replace_quarantine_with_marker)
    with pytest.raises(module.LauncherArtifactError, match="quarantined"):
        module.rollback_launchers(manifest)
    quarantines = list(bin_dir.glob(".kestrel-quarantine-*"))
    attacker = next(
        path
        for path in quarantines
        if path.is_file()
        and path.read_text(encoding="utf-8") == "attacker marker must survive"
    )
    assert attacker.read_text(encoding="utf-8") == "attacker marker must survive"
    preserved = next(path for path in bin_dir.glob("*.preserved"))
    assert preserved.is_file()
    assert module.SHIM_MARKER in preserved.read_text(encoding="utf-8")


def test_final_delete_race_preserves_swapped_backup_and_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _module()
    first = _kestrel_home(tmp_path)
    user_home = tmp_path / "user"
    user_home.mkdir()
    bin_dir = user_home / "bin"
    initial = first / ".nest" / "transactions" / "initial.json"
    module.prepare_launchers(kestrel_home=first, user_home=user_home, manifest_path=initial,
                             bin_dir=bin_dir, platform="darwin", environ={"PATH": str(bin_dir)}, which=lambda _name: None)
    module.commit_launchers(initial)
    replacement = _kestrel_home(tmp_path / "replacement")
    manifest = first / ".nest" / "transactions" / "replacement.json"
    module.prepare_launchers(kestrel_home=replacement, user_home=user_home, manifest_path=manifest,
                             bin_dir=bin_dir, platform="darwin", environ={"PATH": str(bin_dir)}, which=lambda _name: None)
    app_parent = user_home / "Applications"

    def race_only_app(parent: Path) -> None:
        if parent == app_parent:
            _replace_quarantine_with_marker(parent)

    monkeypatch.setattr(module, "_before_quarantine_delete", race_only_app)
    with pytest.raises(module.LauncherArtifactError, match="quarantined"):
        module.commit_launchers(manifest)
    quarantines = list(app_parent.glob(".kestrel-quarantine-*"))
    attacker = next(
        path
        for path in quarantines
        if path.is_file()
        and path.read_text(encoding="utf-8") == "attacker marker must survive"
    )
    assert attacker.read_text(encoding="utf-8") == "attacker marker must survive"
    preserved = next(path for path in app_parent.glob("*.preserved"))
    assert preserved.is_dir()
    assert (
        preserved / "Contents" / "Resources" / module.APP_MARKER_FILENAME
    ).read_text(encoding="utf-8") == f"{module.APP_MARKER}\n"


def test_final_delete_race_preserves_swapped_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _module()
    home = _kestrel_home(tmp_path)
    user_home = tmp_path / "user"
    user_home.mkdir()
    bin_dir = user_home / "bin"
    manifest = home / ".nest" / "transactions" / "launchers.json"
    module.prepare_launchers(kestrel_home=home, user_home=user_home, manifest_path=manifest,
                             bin_dir=bin_dir, platform="linux", environ={"PATH": str(bin_dir)})
    monkeypatch.setattr(module, "_before_quarantine_delete", _replace_quarantine_with_marker)
    with pytest.raises(module.LauncherArtifactError, match="quarantined"):
        module.commit_launchers(manifest)
    quarantines = list(manifest.parent.glob(".*.tombstone"))
    attacker = next(
        path
        for path in quarantines
        if path.is_file()
        and path.read_text(encoding="utf-8") == "attacker marker must survive"
    )
    assert attacker.read_text(encoding="utf-8") == "attacker marker must survive"
    preserved = next(path for path in manifest.parent.glob("*.preserved"))
    assert preserved.is_file()
    assert (
        json.loads(preserved.read_text(encoding="utf-8"))["schema"]
        == module.MANIFEST_SCHEMA
    )


def test_commit_resume_after_each_artifact_boundary_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    module = _module()
    first = _kestrel_home(tmp_path)
    user_home = tmp_path / "user"
    user_home.mkdir()
    bin_dir = user_home / "bin"
    initial = first / ".nest" / "transactions" / "initial.json"
    module.prepare_launchers(kestrel_home=first, user_home=user_home, manifest_path=initial,
                             bin_dir=bin_dir, platform="darwin", environ={"PATH": str(bin_dir)}, which=lambda _name: None)
    module.commit_launchers(initial)
    replacement = _kestrel_home(tmp_path / "replacement")
    manifest = first / ".nest" / "transactions" / "replacement.json"
    module.prepare_launchers(kestrel_home=replacement, user_home=user_home, manifest_path=manifest,
                             bin_dir=bin_dir, platform="darwin", environ={"PATH": str(bin_dir)}, which=lambda _name: None)
    original_remove = module._remove_if_present
    calls = 0

    def crash_after_delete(*args: object, **kwargs: object) -> None:
        nonlocal calls
        original_remove(*args, **kwargs)
        calls += 1
        if calls == 2:
            raise OSError("simulated crash after second backup retirement")

    monkeypatch.setattr(module, "_remove_if_present", crash_after_delete)
    with pytest.raises(OSError, match="simulated crash"):
        module.commit_launchers(manifest)
    with pytest.raises(module.LauncherArtifactError, match="commit has started"):
        module.rollback_launchers(manifest)
    monkeypatch.setattr(module, "_remove_if_present", original_remove)
    assert module.commit_launchers(manifest)["committed"] is True
    assert not manifest.exists()
    assert (bin_dir / "kestrel").is_file()
    assert (user_home / "Applications" / "Kestrel.app").is_dir()


def test_racing_managed_destination_survives_failed_install_and_retry_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    home = _kestrel_home(tmp_path)
    racer_home = _kestrel_home(tmp_path / "racer")
    user_home = tmp_path / "user"
    user_home.mkdir()
    bin_dir = user_home / "bin"
    manifest = home / ".nest" / "transactions" / "launchers.json"
    target = bin_dir / "kestrel"
    racer_bytes = module._shim_text(racer_home).encode("utf-8")
    raced = False

    def create_destination(parent: Path) -> None:
        nonlocal raced
        if parent == bin_dir and not raced:
            raced = True
            target.write_bytes(racer_bytes)
            target.chmod(0o755)

    monkeypatch.setattr(module, "_before_final_mutation", create_destination)
    with pytest.raises(module.LauncherArtifactError, match="rolled back"):
        module.prepare_launchers(
            kestrel_home=home, user_home=user_home, manifest_path=manifest,
            bin_dir=bin_dir, platform="linux", environ={"PATH": str(bin_dir)},
        )

    assert target.read_bytes() == racer_bytes
    assert not manifest.exists()

    monkeypatch.setattr(module, "_before_final_mutation", lambda _parent: None)
    retry_manifest = home / ".nest" / "transactions" / "retry.json"
    module.prepare_launchers(
        kestrel_home=home, user_home=user_home, manifest_path=retry_manifest,
        bin_dir=bin_dir, platform="linux", environ={"PATH": str(bin_dir)},
    )
    module.rollback_launchers(retry_manifest)
    assert target.read_bytes() == racer_bytes


def test_substituted_staged_source_never_remains_public_and_is_preserved_as_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    home = _kestrel_home(tmp_path)
    racer_home = _kestrel_home(tmp_path / "racer")
    user_home = tmp_path / "user"
    user_home.mkdir()
    bin_dir = user_home / "bin"
    manifest = home / ".nest" / "transactions" / "launchers.json"
    target = bin_dir / "kestrel"
    substituted = module._shim_text(racer_home).encode("utf-8")
    preserved_stage = bin_dir / "preserved-original-stage"
    raced = False

    def substitute_source(parent: Path, _source: str, _destination: str) -> None:
        nonlocal raced
        if parent != bin_dir or raced:
            return
        staged = next(bin_dir.glob(".kestrel-stage-*-shim"))
        raced = True
        staged.rename(preserved_stage)
        staged.write_bytes(substituted)
        staged.chmod(0o755)

    monkeypatch.setattr(module, "_before_native_rename", substitute_source)
    with pytest.raises(module.LauncherArtifactError):
        module.prepare_launchers(
            kestrel_home=home, user_home=user_home, manifest_path=manifest,
            bin_dir=bin_dir, platform="linux", environ={"PATH": str(bin_dir)},
        )

    assert not target.exists()
    assert preserved_stage.is_file()
    evidence = list(bin_dir.glob(".kestrel-race-*.tombstone"))
    assert len(evidence) == 1
    assert evidence[0].read_bytes() == substituted


def test_staged_substitution_before_manifest_is_rejected_by_transaction_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _module()
    home = _kestrel_home(tmp_path)
    racer_home = _kestrel_home(tmp_path / "racer")
    user_home = tmp_path / "user"
    user_home.mkdir()
    bin_dir = user_home / "bin"
    manifest = home / ".nest" / "transactions" / "launchers.json"
    preserved_stage = bin_dir / "preserved-transaction-stage"
    original_write = module._write_staged_shim

    def substitute_after_write(
        artifact: dict[str, Any], text: str, *, uid: int,
    ) -> None:
        original_write(artifact, text, uid=uid)
        staged = Path(artifact["staged"])
        staged.rename(preserved_stage)
        staged.write_text(module._shim_text(racer_home), encoding="utf-8")
        staged.chmod(0o755)

    monkeypatch.setattr(module, "_write_staged_shim", substitute_after_write)
    with pytest.raises(
        module.LauncherArtifactError,
        match="identity changed before manifest",
    ):
        module.prepare_launchers(
            kestrel_home=home, user_home=user_home, manifest_path=manifest,
            bin_dir=bin_dir, platform="linux", environ={"PATH": str(bin_dir)},
        )

    assert preserved_stage.is_file()
    assert not (bin_dir / "kestrel").exists()
    assert not manifest.exists()
