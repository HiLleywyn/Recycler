"""Tests for services/chat_threads.py -- the deterministic, pure helpers
behind thread-based AI chat (intent pre-parser, titles, history keys)."""
from __future__ import annotations

from services.chat_threads import (
    detect_thread_intent,
    history_key_for,
    make_thread_title,
)


class TestDetectThreadIntent:
    def test_save_natural(self):
        assert detect_thread_intent("hey can u save this thread for me please") == ("save", None)

    def test_save_chat_synonym(self):
        assert detect_thread_intent("bookmark this conversation") == ("save", None)

    def test_save_needs_object_word(self):
        # "save this idea" must NOT trigger a save -- no thread/chat/convo word.
        assert detect_thread_intent("i should save this idea") == (None, None)

    def test_recall_pull(self):
        assert detect_thread_intent("pull thread a81n5jkh") == ("recall", "a81n5jkh")

    def test_recall_show_me(self):
        assert detect_thread_intent("show me thread hf7j5jkh please") == ("recall", "hf7j5jkh")

    def test_recall_code_is_lowercased(self):
        assert detect_thread_intent("recall thread A81N5JKH") == ("recall", "a81n5jkh")

    def test_recall_find(self):
        # "find" must resolve to the recall intent so it never spawns a thread.
        assert detect_thread_intent("find thread d6yzrhov") == ("recall", "d6yzrhov")

    def test_recall_link(self):
        assert detect_thread_intent("link thread d6yzrhov") == ("recall", "d6yzrhov")

    def test_list(self):
        assert detect_thread_intent("show me my saved threads") == ("list", None)

    def test_list_plain(self):
        assert detect_thread_intent("list my chats") == ("list", None)

    def test_plain_chat_is_not_an_intent(self):
        assert detect_thread_intent("what is the price of mta right now") == (None, None)

    def test_empty(self):
        assert detect_thread_intent("") == (None, None)
        assert detect_thread_intent("   ") == (None, None)


class TestThreadTitle:
    def test_collapses_whitespace(self):
        assert make_thread_title("  write   me a shitpost  ") == "write me a shitpost"

    def test_empty_falls_back(self):
        assert make_thread_title("") == "Disco chat"
        assert make_thread_title("    ") == "Disco chat"

    def test_truncates_long_titles(self):
        title = make_thread_title("x" * 200)
        assert len(title) <= 80
        assert title.endswith("...")


class TestHistoryKey:
    def test_format(self):
        assert history_key_for(123456789) == "thread:123456789"

    def test_accepts_str_id(self):
        assert history_key_for("987") == "thread:987"
