"""Tests for core/framework/ai/auto_fix.py guardrails.

These tests are the firewall for the AI auto-fix path -- a regression
here turns a bug-report-triggered AI into a code-modification system
that can mint money, drop tables, or push to financial logic. Every
denylist entry, every regex pattern, every cap exists for a specific
exploit class; the tests document the "why" by name.

The two LLM-calling helpers (_ai_pick_path / _ai_generate_patch) and
the public propose_fix coroutine are NOT exercised here -- they need
network access. The validators are pure-Python and that's where the
safety lives.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


# ── Module loader ──────────────────────────────────────────────────────────
#
# core/framework/__init__.py imports `discord` (and a chain of deps) at module
# load time. The auto_fix module itself has no Discord deps, so we load it
# directly via importlib without going through the package __init__. This
# keeps the test suite runnable in a venv without the bot's full deps.

_MODULE_PATH = Path(__file__).resolve().parent.parent / "core" / "framework" / "ai" / "auto_fix.py"


@pytest.fixture(scope="module")
def af():
    spec = importlib.util.spec_from_file_location("auto_fix_under_test", _MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["auto_fix_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


# ── Path allowlist ──────────────────────────────────────────────────────────
#
# Every denial below corresponds to a real attack surface. The test name
# explains which one.


class TestPathAllowed:
    def test_normal_cog_is_allowed(self, af):
        ok, _ = af.is_path_allowed("cogs/help.py")
        assert ok

    def test_normal_md_doc_is_allowed(self, af):
        ok, _ = af.is_path_allowed("docs/quickstart.md")
        assert ok

    def test_finance_service_is_blocked(self, af):
        # services/trade.py is the canonical example: a "fix" here can
        # short-circuit slippage or burn / mint tokens directly.
        ok, why = af.is_path_allowed("services/trade.py")
        assert not ok and "denylist" in why

    @pytest.mark.parametrize("path", [
        "services/buddy_economy.py",
        "services/fishing.py",
        "services/dungeon.py",
        "services/farming.py",
        "services/crafting.py",
        "services/swap.py",
        "services/stake.py",
        "services/savings.py",
        "services/vault.py",
        "services/net_worth.py",
        "cogs/trade.py",
        "cogs/eat_the_rich.py",
        "cogs/admin.py",
        "cogs/crypto.py",
        "cogs/stake.py",
        "cogs/bank.py",
        "cogs/shop.py",
        "cogs/groups.py",
    ])
    def test_every_finance_path_blocked(self, af, path: str):
        ok, _ = af.is_path_allowed(path)
        assert not ok, f"{path} should be on the denylist"

    def test_self_referential_paths_blocked(self, af):
        # Auto-fix patching itself is the most direct guardrail-evasion
        # path. Block all of core/framework/ai/.
        ok, _ = af.is_path_allowed("core/framework/ai/auto_fix.py")
        assert not ok
        ok, _ = af.is_path_allowed("core/framework/ai/github_pr.py")
        assert not ok
        ok, _ = af.is_path_allowed("core/framework/ai/heal_ai.py")
        assert not ok

    def test_db_layer_and_schema_blocked(self, af):
        ok, _ = af.is_path_allowed("database/users.py")
        assert not ok
        ok, _ = af.is_path_allowed("database/schema.sql")
        assert not ok

    def test_migrations_directory_blocked(self, af):
        # Migrations are immutable history -- editing one in place is a
        # data integrity disaster.
        ok, why = af.is_path_allowed("database/migrations/0099_anything.sql")
        assert not ok and "off-limits" in why

    def test_github_dir_blocked(self, af):
        ok, _ = af.is_path_allowed(".github/workflows/ci.yml")
        assert not ok

    def test_scripts_dir_blocked(self, af):
        ok, _ = af.is_path_allowed("scripts/deploy.sh")
        assert not ok

    def test_security_dir_blocked(self, af):
        ok, _ = af.is_path_allowed("security/engine.py")
        assert not ok

    def test_tests_dir_blocked(self, af):
        # Tests are the gate, not the patch surface.
        ok, _ = af.is_path_allowed("tests/test_anything.py")
        assert not ok

    def test_infra_files_blocked(self, af):
        for path in ("core/config.py", "main.py", "Dockerfile",
                     "docker-compose.yml", "docker-entrypoint.sh",
                     "railway.toml", "requirements.txt",
                     ".env", ".env.example"):
            ok, _ = af.is_path_allowed(path)
            assert not ok, f"{path} should be denied"

    def test_path_traversal_blocked(self, af):
        ok, why = af.is_path_allowed("../../etc/passwd")
        assert not ok

    def test_absolute_paths_blocked(self, af):
        ok, _ = af.is_path_allowed("/etc/passwd")
        assert not ok

    def test_non_python_non_md_blocked(self, af):
        ok, why = af.is_path_allowed("random/binary.bin")
        assert not ok and ".py" in why

    def test_empty_path_blocked(self, af):
        ok, _ = af.is_path_allowed("")
        assert not ok
        ok, _ = af.is_path_allowed(None)  # type: ignore[arg-type]
        assert not ok

    def test_normalisation_strips_dot_slash(self, af):
        ok, _ = af.is_path_allowed("./cogs/help.py")
        assert ok

    @pytest.mark.parametrize("path", [
        # api/ is the entire FastAPI surface incl. financial mutation
        # endpoints. Out of scope for auto-fix.
        "api/v2/main.py",
        "api/v2/routers/trading.py",
        # Plugins + models = extension points / ML helpers
        "plugins/community/foo.py",
        "models/bar.py",
        # Test cache + venvs are never source we want to patch
        ".pytest_cache/README.md",
        ".venv/lib/foo.py",
        "venv/lib/foo.py",
    ])
    def test_new_deny_prefixes(self, af, path: str):
        ok, _ = af.is_path_allowed(path)
        assert not ok, f"{path} should be denied via PATH_DENY_PREFIXES"

    @pytest.mark.parametrize("path", [
        # Project docs / meta. Auto-fix shouldn't be rewriting CLAUDE.md
        # or the README -- those are human-curated.
        "CLAUDE.md",
        "README.md",
        "CONTRIBUTING.md",
        "PUBLISHING.md",
        "CHANGELOG.md",
        "pyproject.toml",
        "uv.lock",
        "mkdocs.yml",
    ])
    def test_top_level_doc_deny(self, af, path: str):
        ok, _ = af.is_path_allowed(path)
        assert not ok, f"{path} should be in TOP_LEVEL_DOC_DENY"


class TestKeywordFilter:
    """The keyword filter is the difference between locate-UNKNOWN every
    time and the AI getting a focused 5-file shortlist. Lock the
    extraction + ranking behaviour so it doesn't quietly regress.
    """

    def test_extracts_command_invocations(self, af):
        kw = af._extract_keywords("the ,fish command crashes")
        assert "fish" in kw

    def test_extracts_dot_and_slash_commands(self, af):
        kw = af._extract_keywords("/buddy hatch and .delve start both fail")
        assert "buddy" in kw
        assert "delve" in kw

    def test_drops_stopwords_and_short_words(self, af):
        kw = af._extract_keywords("the bug is in the buddy panel ok")
        # 'the', 'is', 'in', 'ok' all stopwords; 'bug' too.
        assert "the" not in kw
        assert "is" not in kw
        assert "bug" not in kw
        assert "buddy" in kw

    def test_stems_simple_plurals(self, af):
        kw = af._extract_keywords("the achievements crashes when paginated")
        assert "achievement" in kw or "achievements" in kw  # stem accepts either
        assert "crash" in kw  # 'crashes' -> 'crash'

    def test_caps_at_12(self, af):
        text = " ".join(f"keyword{i}" for i in range(50))
        kw = af._extract_keywords(text)
        assert len(kw) <= 12

    def test_filter_returns_empty_for_no_keywords(self, af, tmp_path):
        (tmp_path / "cogs").mkdir()
        (tmp_path / "cogs" / "x.py").write_text("# x\n")
        af._ALLOWED_FILES_CACHE = None
        af._FILE_PURPOSE_CACHE.clear()
        files = af._allowed_files_index(tmp_path)
        assert af._keyword_filter(files, [], tmp_path) == []

    def test_filter_ranks_path_match_above_purpose_match(self, af, tmp_path):
        (tmp_path / "cogs").mkdir()
        (tmp_path / "cogs" / "fishing.py").write_text("# unrelated\n")
        (tmp_path / "cogs" / "other.py").write_text(
            '"""other -- mentions fishing in passing."""\n'
        )
        af._ALLOWED_FILES_CACHE = None
        af._FILE_PURPOSE_CACHE.clear()
        files = af._allowed_files_index(tmp_path)
        ranked = af._keyword_filter(files, ["fishing"], tmp_path)
        # Path match should beat purpose match.
        assert ranked[0] == "cogs/fishing.py"

    def test_filter_drops_files_with_zero_match(self, af, tmp_path):
        (tmp_path / "cogs").mkdir()
        (tmp_path / "cogs" / "fishing.py").write_text(
            '"""fishing -- fish stuff."""\n'
        )
        (tmp_path / "cogs" / "irrelevant.py").write_text(
            '"""irrelevant -- nothing to see."""\n'
        )
        af._ALLOWED_FILES_CACHE = None
        af._FILE_PURPOSE_CACHE.clear()
        files = af._allowed_files_index(tmp_path)
        ranked = af._keyword_filter(files, ["fish"], tmp_path)
        assert "cogs/irrelevant.py" not in ranked

    def test_filter_caps_at_max_filtered(self, af, tmp_path):
        (tmp_path / "cogs").mkdir()
        for i in range(40):
            (tmp_path / "cogs" / f"fishing{i}.py").write_text("# fishing\n")
        af._ALLOWED_FILES_CACHE = None
        af._FILE_PURPOSE_CACHE.clear()
        files = af._allowed_files_index(tmp_path)
        ranked = af._keyword_filter(files, ["fishing"], tmp_path, max_filtered=10)
        assert len(ranked) <= 10


class TestAllowedFilesIndex:
    """The locate prompt now feeds a list of real paths into the AI so
    it picks from a known surface instead of hallucinating into the
    denylist. These tests lock the behaviour of the index builder.
    """

    def test_index_excludes_denylisted(self, af, tmp_path):
        # Build a tiny synthetic repo with a denied + allowed file.
        (tmp_path / "cogs").mkdir()
        (tmp_path / "services").mkdir()
        (tmp_path / "cogs" / "help.py").write_text("# allowed\n")
        (tmp_path / "services" / "trade.py").write_text("# denied\n")
        # Bust the module-level cache between calls or we get stale results.
        af._ALLOWED_FILES_CACHE = None
        files = af._allowed_files_index(tmp_path)
        assert "cogs/help.py" in files
        assert "services/trade.py" not in files

    def test_index_ignores_non_py_md(self, af, tmp_path):
        (tmp_path / "stray.bin").write_text("x")
        (tmp_path / "cogs").mkdir()
        (tmp_path / "cogs" / "x.py").write_text("# allowed\n")
        af._ALLOWED_FILES_CACHE = None
        files = af._allowed_files_index(tmp_path)
        assert "cogs/x.py" in files
        assert "stray.bin" not in files

    def test_index_caps_at_max_files(self, af, tmp_path):
        (tmp_path / "cogs").mkdir()
        for i in range(50):
            (tmp_path / "cogs" / f"f{i}.py").write_text("# x\n")
        af._ALLOWED_FILES_CACHE = None
        files = af._allowed_files_index(tmp_path, max_files=10)
        assert len(files) <= 10


# ── Patch validator ────────────────────────────────────────────────────────


class TestValidatePatch:
    def test_unchanged_rejected(self, af):
        original = "def x():\n    return 1\n"
        ok, why = af._validate_patch("cogs/help.py", original, original)
        assert not ok and "unchanged" in why

    def test_empty_rejected(self, af):
        ok, why = af._validate_patch("cogs/help.py", "x = 1\n", "")
        assert not ok and "empty" in why

    def test_small_fix_accepted(self, af):
        before = "def x():\n    return 1\n"
        after  = "def x():\n    return 2\n"
        ok, _ = af._validate_patch("cogs/help.py", before, after)
        assert ok

    def test_oversized_diff_rejected(self, af):
        before = "x = 1\n"
        # MAX_DIFF_LINES is the sanity cap (250 today). Add a comfortable
        # margin past it so the test stays meaningful if the cap is
        # tweaked again later.
        added = af.MAX_DIFF_LINES + 100
        after = before + "\n".join(f"y{i} = {i}" for i in range(added)) + "\n"
        ok, why = af._validate_patch("cogs/help.py", before, after)
        assert not ok and "too large" in why

    def test_syntax_error_rejected(self, af):
        before = "def x():\n    return 1\n"
        after  = "def x()\n    return 2\n"  # missing colon
        ok, why = af._validate_patch("cogs/help.py", before, after)
        assert not ok and "SyntaxError" in why

    def test_md_files_skip_ast(self, af):
        # SyntaxError check is .py-only; an .md "fix" should validate
        # without ast.parse blowing up on Markdown.
        ok, _ = af._validate_patch(
            "docs/notes.md", "# old\n", "# new with prose\n",
        )
        assert ok

    @pytest.mark.parametrize("snippet", [
        "    import os; os.system('rm -rf /')",
        "    import subprocess",
        "    eval('1 + 1')",
        "    exec('x = 1')",
        "    __import__('os')",
        "    update_wallet(uid, gid, 9999)",
        "    update_wallet_holding(uid, gid, 'dsc', 'DSC', 999)",
        "    update_price('MTA', gid, 1.0)",
        # SQL strings -- realistic attack vector lives inside .execute()
        '    await db.execute("DROP TABLE users")',
        '    await db.execute("DELETE FROM reports")',  # unbounded DELETE
    ])
    def test_each_denied_pattern_blocks(self, af, snippet: str):
        before = "def x():\n    return 1\n"
        after  = f"def x():\n{snippet}\n    return 2\n"
        ok, why = af._validate_patch("cogs/help.py", before, after)
        assert not ok, f"`{snippet}` should be blocked but passed"
        assert "denied pattern" in why

    def test_pre_existing_pattern_in_unchanged_lines_does_not_trip(self, af):
        # The denylist scans ADDED lines only, not unchanged content. A
        # file that already imports subprocess shouldn't refuse all
        # patches forever.
        before = "import subprocess\n\ndef x():\n    return 1\n"
        after  = "import subprocess\n\ndef x():\n    return 2\n"
        ok, _ = af._validate_patch("cogs/help.py", before, after)
        assert ok

    def test_delete_with_where_clause_passes(self, af):
        # Bounded DELETE is fine; only unbounded DELETE FROM <table> is
        # rejected.
        before = "QUERY = 'SELECT 1'\n"
        after  = "QUERY = 'DELETE FROM reports WHERE id = 1'\n"
        ok, _ = af._validate_patch("cogs/help.py", before, after)
        assert ok


# ── Other helpers ──────────────────────────────────────────────────────────


class TestNormalisation:
    def test_strips_leading_slash(self, af):
        assert af._normalise("/cogs/x.py") == ""  # absolute path rejected

    def test_strips_dot_slash(self, af):
        assert af._normalise("./cogs/x.py") == "cogs/x.py"

    def test_parent_refs_rejected(self, af):
        assert af._normalise("../cogs/x.py") == ""
        assert af._normalise("cogs/../etc/passwd") == ""

    def test_empty_returns_empty(self, af):
        assert af._normalise("") == ""
        assert af._normalise(None) == ""  # type: ignore[arg-type]


class TestLineDiffCount:
    def test_identical_returns_zero(self, af):
        assert af._line_diff_count("a\nb\n", "a\nb\n") == 0

    def test_one_line_changed(self, af):
        assert af._line_diff_count("a\nb\n", "a\nc\n") == 1

    def test_added_lines_count(self, af):
        assert af._line_diff_count("a\n", "a\nb\n") >= 1

    def test_removed_lines_count(self, af):
        assert af._line_diff_count("a\nb\n", "a\n") >= 1


class TestVerdictParsing:
    """Quick sanity check that the verdict-actionable helper in
    cogs/report.py is robust to model output variations.

    Imported lazily so the rest of this file can run without the cogs/
    deps; if discord isn't available the test is skipped.
    """

    def _import(self):
        try:
            spec = importlib.util.spec_from_file_location(
                "report_under_test",
                Path(__file__).resolve().parent.parent / "cogs" / "report.py",
            )
            mod = importlib.util.module_from_spec(spec)
            sys.modules["report_under_test"] = mod
            spec.loader.exec_module(mod)
            return mod
        except Exception:
            pytest.skip("cogs/report.py not importable in this env")

    def test_real_high_is_actionable(self):
        mod = self._import()
        v = "Verdict: real\nConfidence: high\nReasoning: ...\nRecommended action: investigate"
        assert mod._verdict_is_actionable(v) is True

    def test_real_low_blocks_autofix(self):
        mod = self._import()
        v = "Verdict: real\nConfidence: low\nReasoning: ..."
        assert mod._verdict_is_actionable(v) is False

    def test_likely_real_medium_is_actionable(self):
        mod = self._import()
        v = "Verdict: likely_real\nConfidence: medium\nReasoning: ..."
        assert mod._verdict_is_actionable(v) is True

    def test_suspicious_blocks_autofix(self):
        mod = self._import()
        v = "Verdict: suspicious\nConfidence: high\nReasoning: ..."
        assert mod._verdict_is_actionable(v) is False

    def test_likely_fake_blocks_autofix(self):
        mod = self._import()
        v = "Verdict: likely_fake\nConfidence: high\nReasoning: ..."
        assert mod._verdict_is_actionable(v) is False

    def test_spam_blocks_autofix(self):
        mod = self._import()
        v = "Verdict: spam\nConfidence: high\nReasoning: ..."
        assert mod._verdict_is_actionable(v) is False

    def test_empty_blocks_autofix(self):
        mod = self._import()
        assert mod._verdict_is_actionable("") is False
        assert mod._verdict_is_actionable(None) is False  # type: ignore[arg-type]
