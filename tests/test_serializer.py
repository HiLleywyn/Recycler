"""Unit tests for the serializer's pure logic (no live Discord objects)."""
from __future__ import annotations

import discord  # noqa: F401  -- ensures the runtime has Components V2 era discord.py

from clanklib import serializer


def test_restore_options_default_to_full_package() -> None:
    opts = serializer.RestoreOptions()
    assert opts.delete_roles is True
    assert opts.delete_channels is True
    assert opts.restore_roles is True
    assert opts.restore_channels is True
    assert opts.restore_settings is True
    # A backup restore is the whole package by default: roles, permissions,
    # channels, settings, and the archived messages. Callers opt out with a
    # structure-only flag.
    assert opts.restore_messages is True


def test_restore_stats_summary_mentions_counts() -> None:
    stats = serializer.RestoreStats(roles_created=3, channels_created=5, messages_sent=12)
    summary = stats.summary()
    assert "3 roles" in summary
    assert "5 channels" in summary
    assert "12 messages" in summary


def test_message_limits_are_sane() -> None:
    assert serializer.DEFAULT_MESSAGE_LIMIT >= 1
    assert serializer.MAX_MESSAGE_LIMIT >= serializer.DEFAULT_MESSAGE_LIMIT
    assert serializer.SCHEMA_VERSION >= 1
