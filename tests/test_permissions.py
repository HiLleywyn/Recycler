"""Tests for the permission model: minimal invite, audit, mod gating."""
from __future__ import annotations

import discord  # noqa: F401
import pytest

# permissions.py imports the framework (core.framework.cogs); skip cleanly where
# the framework isn't installed (the dependency-light CI job), matching the
# convention used by the other framework-dependent tests.
pytest.importorskip("core.framework.cogs")

from clanklib import permissions as perms  # noqa: E402


def test_invite_requests_minimal_not_administrator():
    p = perms.required_bot_permissions()
    assert not p.administrator
    # the features it does need: recreate roles/channels for backups, webhooks
    # for chatlog/sync, ban for sync ban-propagation.
    assert p.manage_roles and p.manage_channels and p.manage_webhooks
    assert p.ban_members
    url = perms.invite_url(123456789012345678)
    assert "permissions=8&" not in url           # not Administrator
    assert f"permissions={p.value}" in url


def test_audit_flags_missing_and_passes_on_admin():
    class _Perms:
        def __init__(self, **kw):
            self._kw = kw
            self.administrator = kw.get("administrator", False)
        def __getattr__(self, name):
            return self._kw.get(name, False)

    class _Me:
        def __init__(self, gp):
            self.guild_permissions = gp

    # nothing granted -> every non-core feature reports missing perms
    res = perms.audit_permissions(_Me(_Perms()))
    assert any(not r.ok for r in res)

    # administrator -> all features OK
    res_admin = perms.audit_permissions(_Me(_Perms(administrator=True)))
    assert all(r.ok for r in res_admin)


def test_pretty_perm_label():
    assert perms.pretty_perm("manage_roles") == "Manage Roles"


def test_manifest_permissions_match_required():
    """auren.json's declared invite permissions (which the Auren platform's
    Invite button reads) must equal the code's required set, so the two can
    never drift apart."""
    import json
    from pathlib import Path

    manifest = json.loads(
        (Path(__file__).resolve().parent.parent / "auren.json").read_text()
    )
    declared = int(manifest["discord"]["permissions"])
    assert declared == perms.required_bot_permissions().value
    assert declared != 8  # never Administrator
