"""Tests for the dynamic help model (clanklib.help)."""
from __future__ import annotations

from clanklib import help as H


class _FakeCmd:
    def __init__(self, name, short_doc="", subs=None, hidden=False):
        self.name = name
        self.short_doc = short_doc
        self.hidden = hidden
        self.commands = subs or []


class _FakeBot:
    def __init__(self, cmds):
        self._cmds = {c.name: c for c in cmds}

    def get_command(self, name):
        return self._cmds.get(name)


def test_sections_have_stable_unique_keys():
    keys = [s.key for s in H.SECTIONS]
    assert len(keys) == len(set(keys))
    assert "settings" in keys and "backups" in keys


def test_command_lines_expands_group_subcommands():
    backup = _FakeCmd("backup", subs=[
        _FakeCmd("create", "Snapshot the server."),
        _FakeCmd("load", "Restore a backup."),
    ])
    bot = _FakeBot([backup])
    sec = H.SECTIONS_BY_KEY["backups"]
    lines = H.command_lines(bot, sec, ".")
    assert lines == ["`.backup create` -- Snapshot the server.",
                     "`.backup load` -- Restore a backup."]


def test_command_lines_skips_hidden_and_missing():
    grp = _FakeCmd("backup", subs=[
        _FakeCmd("create", "ok"),
        _FakeCmd("secret", "hidden one", hidden=True),
    ])
    bot = _FakeBot([grp])  # 'template' etc. are absent -> skipped, no crash
    sec = H.SECTIONS_BY_KEY["backups"]
    lines = H.command_lines(bot, sec, ".")
    assert lines == ["`.backup create` -- ok"]


def test_plain_command_lists_itself():
    exp = _FakeCmd("export", "Download a backup as JSON.")
    bot = _FakeBot([exp])
    sec = H.SECTIONS_BY_KEY["importexport"]
    lines = H.command_lines(bot, sec, ".")
    assert "`.export` -- Download a backup as JSON." in lines


def test_selected_sections_preserves_order_and_filters():
    chosen = H.selected_sections({"settings", "backups", "bogus"})
    keys = [s.key for s in chosen]
    # SECTIONS order preserved (backups before settings), unknown dropped
    assert keys == ["backups", "settings"]
