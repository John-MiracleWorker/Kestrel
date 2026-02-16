"""
Tests for Hands service permission checking and audit logging.
"""

import os
import json
from unittest.mock import patch, MagicMock, AsyncMock
import pytest


# ── Test PermissionChecker / Allowlist ────────────────────────────────

def test_allowlist_loads_default_permissions():
    """Allowlist should have default allowed modules/commands."""
    from security.allowlist import PermissionChecker
    checker = PermissionChecker()
    # Default allowlist should exist
    assert checker is not None


def test_allowlist_blocks_dangerous_module():
    """Modules like 'os.system' and 'subprocess' should be blocked by default."""
    from security.allowlist import PermissionChecker
    checker = PermissionChecker()

    # These should be blocked in the default configuration
    assert checker.is_module_allowed("subprocess") is False
    assert checker.is_module_allowed("os") is False


def test_allowlist_allows_safe_modules():
    """Safe standard library modules should be allowed."""
    from security.allowlist import PermissionChecker
    checker = PermissionChecker()

    assert checker.is_module_allowed("json") is True
    assert checker.is_module_allowed("math") is True
    assert checker.is_module_allowed("datetime") is True


def test_allowlist_validates_network_access():
    """Network access should be denied by default."""
    from security.allowlist import PermissionChecker
    checker = PermissionChecker()

    assert checker.is_network_allowed() is False


# ── Test AuditLogger ─────────────────────────────────────────────────

def test_audit_logger_creates_entry():
    """AuditLogger should create structured log entries."""
    from security.audit import AuditLogger
    logger = AuditLogger()

    entry = logger.create_entry(
        skill_name="web_search",
        user_id="user-1",
        workspace_id="ws-1",
        action="execute",
        status="success",
    )

    assert entry["skill_name"] == "web_search"
    assert entry["user_id"] == "user-1"
    assert entry["status"] == "success"
    assert "timestamp" in entry


def test_audit_logger_captures_error():
    """AuditLogger should record error details."""
    from security.audit import AuditLogger
    logger = AuditLogger()

    entry = logger.create_entry(
        skill_name="file_write",
        user_id="user-2",
        workspace_id="ws-2",
        action="execute",
        status="denied",
        error="Permission denied: filesystem write not allowed",
    )

    assert entry["status"] == "denied"
    assert "Permission denied" in entry["error"]
