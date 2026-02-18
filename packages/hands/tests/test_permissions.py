
import pytest
from security.allowlist import PermissionChecker

def test_allowlist_default_allow_all():
    """By default, no policy means everything is allowed."""
    checker = PermissionChecker()
    assert checker.check("ws-1", "any_skill") is True

def test_allowlist_global_block():
    """Globally blocked skills should be denied."""
    checker = PermissionChecker()
    checker._default_blocked.add("dangerous_skill")
    assert checker.check("ws-1", "dangerous_skill") is False

def test_allowlist_workspace_allow_mode():
    """Workspace in allowlist mode should only allow listed skills."""
    checker = PermissionChecker()
    checker.set_policy("ws-1", {
        "mode": "allowlist",
        "skills": ["safe_skill"]
    })
    
    assert checker.check("ws-1", "safe_skill") is True
    assert checker.check("ws-1", "other_skill") is False

def test_allowlist_workspace_block_mode():
    """Workspace in blocklist mode should deny listed skills."""
    checker = PermissionChecker()
    checker.set_policy("ws-1", {
        "mode": "blocklist",
        "skills": ["bad_skill"]
    })
    
    assert checker.check("ws-1", "good_skill") is True
    assert checker.check("ws-1", "bad_skill") is False
