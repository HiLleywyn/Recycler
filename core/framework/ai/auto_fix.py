"""core/framework/ai/auto_fix.py  -  AI-authored bug-fix proposals.

Tier A of the auto-fix pipeline: given a bug report (or a doctor anomaly
description), ask the LLM to localise the bug, draft a single-file patch,
and validate it before handing the result to ``core/framework/ai/github_pr.py``
to open a real GitHub pull request.

EVERY decision in this module is paranoid by design. The whole feature
exists at the intersection of prompt-injectable user input (player bug
reports) and a bot that can write Python files in a financial-economy
codebase, so the validators here are the firewall:

  * PATH_DENYLIST blocks the high-risk surface area (token economy,
    migrations, infra, this module itself) at the file level. The AI
    can only touch presentation / utility code.
  * MAX_DIFF_LINES caps how much can change per patch. A "fix" that
    rewrites 400 lines is treated as a hallucination, not a fix.
  * DENY_PATTERN_REGEX rejects diffs that introduce obvious foot-guns
    even inside an allowed file (eval, exec, os.system, raw wallet
    credits, etc).
  * compile-with-ast must succeed on the candidate file. A patch that
    breaks Python syntax never reaches the PR step.

If any check fails, ``propose_fix`` returns None and the caller surfaces
a "couldn't auto-fix" note. The bot never silently writes broken code.

Public API:
    propose_fix(report_text, signals, config, repo_root)  -> PatchProposal | None
    PatchProposal -- dataclass: rel_path, original_text, new_text, summary
"""
from __future__ import annotations

import ast
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

log = logging.getLogger("discoin.auto_fix")


# ── Guardrail constants ────────────────────────────────────────────────────

# Files (relative to repo root, POSIX separators) the AI is NEVER allowed to
# modify via auto-fix. Anything finance-adjacent, anything that touches the
# token mint / burn path, every migration (irreversible by definition),
# infrastructure, and the auto-fix machinery itself.
PATH_DENYLIST: tuple[str, ...] = (
    # The auto-fix subsystem itself. Letting AI patch its own guardrails
    # is the most direct path to a confused-deputy escalation.
    "core/framework/ai/auto_fix.py",
    "core/framework/ai/github_pr.py",
    "core/framework/ai/heal_ai.py",
    "core/framework/ai/diagnose_ai.py",
    "core/framework/ai/report_ai.py",
    "core/framework/ai/client.py",
    # Token / economy / financial mutation paths. Any change here can
    # mint money, drain pools, or skirt slippage. Out of scope for
    # auto-fix, full stop.
    "services/trade.py",
    "services/swap.py",
    "services/buddy_economy.py",
    "services/buddy_market.py",
    "services/fishing.py",
    "services/dungeon.py",
    "services/farming.py",
    "services/crafting.py",
    "services/stake.py",
    "services/savings.py",
    "services/vault.py",
    "services/net_worth.py",
    "services/lp_yield.py",
    "services/liquidity.py",
    "cogs/trade.py",
    "cogs/eat_the_rich.py",
    "cogs/admin.py",
    "cogs/crypto.py",
    "cogs/stake.py",
    "cogs/validators.py",
    "cogs/moons.py",
    "cogs/nfts.py",
    "cogs/contracts.py",
    "cogs/groups.py",
    "cogs/shop.py",
    "cogs/bank.py",
    "cogs/faucet.py",
    # DB layer + schema. Auto-modifying DB code or schema is a non-starter.
    "core/database.py",
    "database/users.py",
    "database/guilds.py",
    "database/mining.py",
    "database/pools.py",
    "database/reports.py",
    "database/schema.sql",
    # Core framework. Anything here can break every cog at once.
    "core/framework/bot.py",
    "core/framework/context.py",
    "core/framework/scale.py",
    "core/framework/network.py",
    "core/framework/middleware.py",
    "core/framework/log.py",
    "core/framework/embed.py",
    "core/framework/ui.py",
    "core/framework/cooldowns.py",
    # Config + secrets + infra. Auto-modifying these is how you ship
    # production credentials to the wrong place.
    "core/config.py",
    "main.py",
    ".env",
    ".env.example",
    "Dockerfile",
    "docker-compose.yml",
    "docker-entrypoint.sh",
    "railway.toml",
    "requirements.txt",
    "pyproject.toml",
    "uv.lock",
)

# Path prefixes that are *always* off-limits regardless of file. Migrations
# are immutable history; the entire .github / .claude tree is meta.
PATH_DENY_PREFIXES: tuple[str, ...] = (
    "database/migrations/",
    ".github/",
    ".claude/",
    ".pytest_cache/",
    ".venv/",
    "venv/",
    "scripts/",  # release / deploy helpers, never auto-edit
    "security/",  # manual review only
    "tests/",     # tests are the gate, not the patch
    "frontend/",  # not the bot
    "ai/",        # ML training / agent runtime, manual review
    # Public REST surface. The whole tree includes auth, rate limiting,
    # idempotency, and every wallet/trade/staking endpoint. Financial
    # mutation, manual-review only.
    "api/",
    # Plugins + models + tools are extension points + ML helpers --
    # auto-fix would create more bugs than it solves here.
    "plugins/",
    "models/",
)


# Top-level files that look like documentation / project meta and aren't
# really auto-fix territory. Listed explicitly so a "fix the README"
# bug report doesn't get an AI rewriting it.
TOP_LEVEL_DOC_DENY: tuple[str, ...] = (
    "CLAUDE.md",
    "README.md",
    "CONTRIBUTING.md",
    "PUBLISHING.md",
    "CHANGELOG.md",
    "pyproject.toml",
    "uv.lock",
    "mkdocs.yml",
    "pytest.ini",
    "diff.txt",
    "tools.json",
)

# Soft cap on lines changed per patch. Real bug fixes can be 200+ lines
# (renames, signature changes, follow-ups across a single file). Above
# this, the human review step ALWAYS happens before the PR opens, so
# the cap is a sanity check against hallucinated full-file rewrites
# rather than an automatic reject.
MAX_DIFF_LINES: int = 250

# Patterns that must NOT appear ADDED in the new content (case-insensitive
# substring). These are foot-guns that can mint money, run shell, or
# bypass safety regardless of which file the AI is editing.
DENY_PATTERN_REGEX: tuple[str, ...] = (
    r"\bos\.system\b",
    r"\bsubprocess\b",
    r"\beval\s*\(",
    r"\bexec\s*\(",
    r"\b__import__\s*\(",
    r"update_wallet\s*\(",          # direct USD mint
    r"update_wallet_holding\s*\(",   # direct token mint
    r"update_price\s*\(",            # oracle write
    r"DROP\s+TABLE",
    r"TRUNCATE\s+",
    r"DELETE\s+FROM\s+\w+\b(?!\s+WHERE)",  # unbounded DELETE
)

# Output cap so a model that ignores the system prompt can't return a 1MB
# rewrite that we then have to validate.
MAX_OUTPUT_BYTES: int = 64_000


@dataclass(slots=True)
class TraceStep:
    """Per-AI-call observability captured during ``propose_fix``.

    The Tier-A pipeline makes up to two LLM calls (locate + generate).
    Each populates one of these so the admin DM can show what was
    asked, what came back, and how much it cost. None of the fields
    are required -- a step might be partially populated if the call
    crashed.
    """
    stage: str               # 'locate' | 'generate'
    backend: str = ""        # 'openrouter' | 'ollama'
    model: str = ""
    prompt_chars: int = 0    # length of the assembled user prompt body
    max_tokens: int = 0      # cap requested from the model
    prompt_tokens: int = 0
    completion_tokens: int = 0
    elapsed_ms: int = 0
    raw_output: str = ""     # truncated copy of the AI's reply (debug)
    error: str = ""          # exception text if the call crashed


@dataclass(slots=True)
class PatchProposal:
    rel_path: str           # POSIX path relative to repo root
    original_text: str      # full file content before
    new_text: str           # full file content after
    summary: str            # one-line commit-message-ready summary
    rationale: str          # short reasoning the AI returned (for the PR body)
    lines_changed: int      # crude diff-line count for display
    trace: list[TraceStep] = field(default_factory=list)


@dataclass(slots=True)
class PatchRejection:
    """Result returned by ``propose_fix`` when the pipeline declines.

    ``stage`` is one of: locate / path_denied / file_missing /
    file_too_large / generate / validate. ``reason`` is a short
    human-readable string the cog surfaces to the admin so they know
    exactly what the AI / validator pushed back on rather than the
    old generic 'AI returned UNKNOWN'.
    """
    stage: str
    reason: str
    rel_path: str = ""
    trace: list[TraceStep] = field(default_factory=list)


# ── Path validation ────────────────────────────────────────────────────────

def _normalise(rel_path: str) -> str:
    """Strip a leading ``./`` and normalise separators.

    Returns "" if the path is structurally suspicious (absolute, parent
    refs, empty). Callers should treat "" as a hard reject. Anything
    starting with ``/`` is rejected outright (an absolute path is not a
    valid in-repo location regardless of what comes after).
    """
    s = (rel_path or "").strip()
    if not s:
        return ""
    # Reject absolute paths and parent-traversal up-front; do not strip
    # them and silently rewrite. ./foo is the only relative-prefix we
    # accept, and we strip it explicitly.
    if s.startswith("/"):
        return ""
    if s.startswith("./"):
        s = s[2:]
    if ".." in Path(s).parts:
        return ""
    return Path(s).as_posix()


def is_path_allowed(rel_path: str) -> tuple[bool, str]:
    """Return (allowed, reason). reason is empty when allowed."""
    p = _normalise(rel_path)
    if not p:
        return (False, "path is empty / absolute / contains ..")
    if p in PATH_DENYLIST:
        return (False, f"`{p}` is on the denylist")
    if p in TOP_LEVEL_DOC_DENY:
        return (False, f"`{p}` is project meta / docs (off-limits)")
    for prefix in PATH_DENY_PREFIXES:
        if p.startswith(prefix):
            return (False, f"`{p}` lives under `{prefix}` (off-limits)")
    if not (p.endswith(".py") or p.endswith(".md")):
        return (False, f"only .py / .md files are auto-fixable (got `{p}`)")
    return (True, "")


def _denied_patterns_in(added_lines: "Iterable[str]") -> list[str]:
    """Return the list of pattern strings that appear in any added line."""
    hits: list[str] = []
    for line in added_lines:
        for pat in DENY_PATTERN_REGEX:
            if re.search(pat, line, flags=re.IGNORECASE):
                hits.append(pat)
                break
    return hits


def _line_diff_count(before: str, after: str) -> int:
    """Crude unified-diff line count: total added + removed.

    Doesn't try to be ``difflib`` accurate -- a small overcount is fine
    because the only consumer is MAX_DIFF_LINES (a safety cap, not a UI).
    """
    a = before.splitlines()
    b = after.splitlines()
    return abs(len(a) - len(b)) + sum(
        1 for x, y in zip(a, b) if x != y
    )


def _added_lines(before: str, after: str) -> list[str]:
    """Return only the lines present in ``after`` but not in ``before``.

    Mirrors what a ``+`` line in a unified diff would be. Used by the
    ``DENY_PATTERN_REGEX`` scan so we only flag patterns the AI actually
    introduced -- a pre-existing ``eval`` call already in the file
    doesn't trip the guardrail.
    """
    before_set = set(before.splitlines())
    return [ln for ln in after.splitlines() if ln not in before_set]


# ── LLM prompts ────────────────────────────────────────────────────────────

_LOCATE_SYSTEM_PROMPT = (
    "You are a senior engineer triaging a Discoin (Discord economy bot) "
    "bug report. Your job: pick the SINGLE most likely buggy file from "
    "the supplied annotated list. The list is in the format\n"
    "  `<path>  --  <one-line description>`\n"
    "Reply with EXACTLY one line: the path copied verbatim, no prose, "
    "no markdown, no quotes.\n\n"
    "Method:\n"
    "  1. Read the bug report. Extract concrete keywords: command names "
    "     (`,fish`, `,delve`, `,buddy`, etc.), feature names (crafting, "
    "     dungeon, fishing, market, achievements, status), error text, "
    "     UI elements (embeds, buttons, modals).\n"
    "  2. Scan the file descriptions for matches. A `,X` command almost "
    "     always lives in `cogs/X.py`. A status / dashboard / panel is "
    "     usually in cogs/*. Display / formatting / helper bugs live "
    "     under core/framework/* or cogs/*. Achievements / quests / "
    "     challenges live in their own cogs / services / *_config.py.\n"
    "  3. Pick the one file with the strongest match. Prefer cogs over "
    "     services when the report is about a Discord-facing command. "
    "     Prefer the most specific file (`cogs/fishing.py`) over a "
    "     general one (`cogs/play.py`) when both could plausibly fit.\n"
    "  4. Many reports MENTION money/tokens but the bug is in display/"
    "     help/embed code that happens to reference them. 'Mentions "
    "     money' is NOT the same as 'lives in financial logic, return "
    "     UNKNOWN'. Try to find a real candidate FIRST.\n\n"
    "UNKNOWN is the LAST resort -- only return it when:\n"
    "  * the report is genuinely too vague (no command names, no error "
    "    text, no feature names), OR\n"
    "  * every plausible file is missing from the supplied list (it "
    "    lives in a denylisted financial / migration / infra path).\n\n"
    "If you can plausibly nominate any file from the list, do so even "
    "if you're not certain -- the system will validate the patch and "
    "reject it later if you guessed wrong. A wrong guess that gets "
    "rejected at the validate step is strictly better than UNKNOWN."
)


# Trace-side state populated by _keyword_filter so the cog's trace DM
# can show the admin which keywords were extracted and how the
# candidate list shrank. Per-call, not cached.

@dataclass(slots=True)
class FilterTrace:
    keywords:       list[str] = field(default_factory=list)
    full_count:     int = 0
    filtered_count: int = 0
    shortcut_path:  str = ""   # set when filter narrowed to 1 file


# Stop-words and command-shaped tokens we DON'T want to treat as
# keywords. Common English plus Discord/economy chatter that would
# match every file otherwise.
_KEYWORD_STOPWORDS: frozenset[str] = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "for",
    "from", "have", "in", "into", "is", "it", "its", "of", "on", "or",
    "so", "that", "the", "this", "to", "was", "were", "with", "when",
    "where", "you", "your", "i", "me", "my", "we", "our", "they", "them",
    "what", "why", "how", "do", "does", "did", "doing", "done", "can",
    "could", "would", "should", "will", "just", "not", "no", "yes",
    "if", "then", "than", "any", "all", "some", "more", "less", "most",
    "very", "really", "also", "only", "even", "still", "again", "back",
    "down", "up", "out", "off", "on", "over", "under", "now", "here",
    "there", "good", "bad", "great", "ok", "okay", "thanks", "thank",
    "please", "lol", "lmao", "lmfao", "wtf", "tbh", "ngl",
    "bug", "bugs", "issue", "issues", "report", "reports", "broken",
    "broke", "doesnt", "doesn", "isnt", "isn", "cant", "cannot",
    "wont", "didnt", "havent",
    "discoin", "bot", "discord",
    "command", "commands", "page", "pages", "thing", "things",
    # Catch-all economy words that match nearly every file.
    "money", "token", "tokens", "coin", "coins", "balance", "wallet",
    "user", "users", "player", "players", "server", "servers", "guild",
})

# Stem map -- collapses simple plurals / verb forms so 'crashes' and
# 'crashed' both map to 'crash'. Cheap, hand-rolled, no NLP dep.
_STEM_SUFFIXES: tuple[str, ...] = (
    "ing", "ies", "ied", "ed", "es", "s",
)


def _stem(word: str) -> str:
    w = word.lower()
    for suf in _STEM_SUFFIXES:
        if len(w) > len(suf) + 2 and w.endswith(suf):
            return w[: -len(suf)]
    return w


def _extract_keywords(report_text: str) -> list[str]:
    """Pull a small set of meaningful keywords from the bug report.

    Strategy:
      1. Pull every ``,token`` / ``.token`` / ``/token`` occurrence
         verbatim (these are command invocations -- strong signal of
         which cog owns the bug).
      2. Tokenize the rest on non-alphanumerics, lowercase, drop
         stop-words, drop tokens shorter than 4 chars (almost always
         noise), stem.
      3. Dedupe preserving order so the first mentions weight higher.
      4. Cap at 12 keywords to keep the downstream filter fast.
    """
    import re
    text = (report_text or "")
    keywords: list[str] = []
    seen: set[str] = set()

    def _push(k: str) -> None:
        if k and k not in seen:
            seen.add(k)
            keywords.append(k)

    # Command invocations: ',fish' '.delve' '/buddy' etc.
    for m in re.finditer(r"[,./]([a-zA-Z][a-zA-Z0-9_]{2,})", text):
        _push(m.group(1).lower())

    # Bare words.
    for raw in re.findall(r"[A-Za-z][A-Za-z0-9_]{3,}", text):
        w = raw.lower()
        if w in _KEYWORD_STOPWORDS:
            continue
        _push(_stem(w))

    return keywords[:12]


def _keyword_filter(
    file_list: list[str], keywords: list[str], repo_root: Path,
    *, max_filtered: int = 30,
) -> list[str]:
    """Score each file by keyword matches against (a) its path and
    (b) its module-purpose docstring; return the highest-scoring
    files (up to ``max_filtered``). Files with zero matches are
    dropped entirely.

    A ``,fish`` report → ``cogs/fishing.py`` scores high because both
    the path and purpose mention ``fish``. ``crafting_config.py``
    scores too because the purpose mentions crafting. Files about
    unrelated systems (``cogs/help.py``, ``cogs/diagnose.py``) score
    zero and get filtered out.

    Returns the empty list if no keywords were extracted -- the
    caller falls back to the full file_list when that happens (so
    the existing path is never blocked by an empty filter).
    """
    if not keywords or not file_list:
        return []
    scores: list[tuple[int, str]] = []
    for rel in file_list:
        path_lower = rel.lower()
        purpose = _file_purpose(repo_root / rel).lower()
        score = 0
        for kw in keywords:
            kw_lower = kw.lower()
            if kw_lower in path_lower:
                score += 5  # path match is a strong signal
            if kw_lower in purpose:
                score += 2
            # Loose stem match against the path's basename. Catches
            # 'fishing' vs 'fish' and similar.
            base = path_lower.rsplit("/", 1)[-1].rsplit(".", 1)[0]
            if _stem(base) == _stem(kw_lower):
                score += 3
        if score > 0:
            scores.append((score, rel))
    scores.sort(key=lambda t: (-t[0], t[1]))
    return [rel for _, rel in scores[:max_filtered]]


_ALLOWED_FILES_CACHE: tuple[float, list[str]] | None = None
# Per-file 'one-line purpose' lines extracted from module docstrings.
# Cached by mtime so re-edits invalidate the entry on next read.
_FILE_PURPOSE_CACHE: dict[str, tuple[float, str]] = {}


def _file_purpose(abs_path: Path) -> str:
    """Pull a one-line summary of what ``abs_path`` does, for the locate
    prompt. Three checks, in order:

      1. Module docstring -- file starts with a triple-quoted string.
         Take the first meaningful non-blank line inside it. If that
         line just repeats the filename (``cogs/foo.py -- ...``), strip
         the repeated prefix and use the rest.
      2. Top comment block -- consecutive ``#`` lines starting at line 1
         or 2 (skipping shebang / encoding declarations).
      3. Markdown heading -- for .md files, the first ``#`` heading.

    Anything past the first 30 lines is ignored so we don't surface
    random helper docstrings further down the file. Files with no
    detectable header return ``""`` and the caller falls back to the
    bare path.

    Cached by mtime so re-edits invalidate on next read.
    """
    try:
        st = abs_path.stat()
    except OSError:
        return ""
    cached = _FILE_PURPOSE_CACHE.get(str(abs_path))
    if cached and cached[0] == st.st_mtime:
        return cached[1]
    try:
        text = abs_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""

    summary = ""
    head = text[:6000]  # cap for cheap docstring lookup

    # 1) Module docstring: file starts with a triple-quoted string after
    # optional leading whitespace / shebang. We only check the very top
    # -- random docstrings deeper in the file are not the module purpose.
    stripped = head.lstrip()
    # Skip past a single shebang line if present.
    if stripped.startswith("#!"):
        nl = stripped.find("\n")
        if nl >= 0:
            stripped = stripped[nl + 1:].lstrip()
    if stripped.startswith("# -*-"):
        nl = stripped.find("\n")
        if nl >= 0:
            stripped = stripped[nl + 1:].lstrip()
    # Skip past `from __future__ import ...` since that's the one
    # legal import-before-docstring per PEP 8.
    if stripped.startswith("from __future__"):
        nl = stripped.find("\n")
        if nl >= 0:
            stripped = stripped[nl + 1:].lstrip()
    for q in ('"""', "'''"):
        if stripped.startswith(q):
            after = stripped[3:]
            end = after.find(q)
            if end >= 0:
                doc = after[:end]
                for line in doc.splitlines():
                    s = line.strip()
                    if not s:
                        continue
                    # Strip a 'cogs/foo.py -- desc' prefix that just
                    # echoes the path; the desc is what we want.
                    if s.lower().startswith(abs_path.name.lower()):
                        rest = s[len(abs_path.name):].lstrip(" -:")
                        if rest:
                            summary = rest
                            break
                        continue
                    summary = s
                    break
            break

    # 2) Top comment block -- only the first 8 lines, only consecutive
    # comment lines starting at line 1 or 2.
    if not summary:
        comment_lines: list[str] = []
        for i, line in enumerate(text.splitlines()[:8]):
            s = line.strip()
            if i == 0 and (s.startswith("#!") or s.startswith("# -*-")):
                continue
            if s.startswith("#"):
                comment_lines.append(s.lstrip("# ").strip())
            elif comment_lines:
                break
            elif s:
                # Non-comment, non-blank before any comment -- abort.
                break
        if comment_lines:
            summary = " ".join(c for c in comment_lines[:2] if c)

    # 3) Markdown heading.
    if not summary and abs_path.suffix.lower() == ".md":
        for line in text.splitlines()[:30]:
            s = line.strip()
            if not s:
                continue
            if s.startswith("#"):
                summary = s.lstrip("# ").strip()
                break
            # First non-blank non-heading line is OK too.
            summary = s
            break

    summary = summary.replace("\t", " ").strip()
    if len(summary) > 120:
        summary = summary[:117] + "..."
    _FILE_PURPOSE_CACHE[str(abs_path)] = (st.st_mtime, summary)
    return summary


def _allowed_files_index(repo_root: Path, *, max_files: int = 400) -> list[str]:
    """Walk ``repo_root`` and return a sorted list of relative POSIX
    paths that pass :func:`is_path_allowed`. Cached for 5 minutes so
    repeated proposals don't re-walk the tree.

    The cap (``max_files``) is a safety net for a future refactor
    that adds a thousand new cog files; the prompt budget can't fit
    them all anyway and the AI does better with a focused surface.
    """
    global _ALLOWED_FILES_CACHE
    import time as _time
    now = _time.time()
    if _ALLOWED_FILES_CACHE and (now - _ALLOWED_FILES_CACHE[0] < 300):
        return _ALLOWED_FILES_CACHE[1]
    out: list[str] = []
    for path in repo_root.rglob("*"):
        if not path.is_file():
            continue
        try:
            rel = path.relative_to(repo_root).as_posix()
        except ValueError:
            continue
        # Cheap fast-rejects before the (slightly more expensive)
        # is_path_allowed call.
        if not (rel.endswith(".py") or rel.endswith(".md")):
            continue
        ok, _ = is_path_allowed(rel)
        if ok:
            out.append(rel)
        if len(out) >= max_files:
            break
    out.sort()
    _ALLOWED_FILES_CACHE = (now, out)
    return out

_PATCH_SYSTEM_PROMPT = (
    "You are a senior engineer applying a SMALL, SURGICAL fix to a Discoin "
    "Python file. You will receive the bug report and the full current file "
    "contents. Return ONLY the complete new file contents. No markdown "
    "fences, no explanation, no leading/trailing prose -- just the raw "
    "file as it should look after your fix.\n\n"
    "Hard rules:\n"
    "  * Change as few lines as possible. A 5-line patch is correct; a "
    "    50-line patch is suspicious; anything bigger will be rejected.\n"
    "  * Preserve every existing import, class, function, and decorator "
    "    that you don't need to change.\n"
    "  * Do not add new dependencies. Do not import new third-party libs.\n"
    "  * Do not touch authentication, permissions, slippage, oracle "
    "    writes, or wallet credit/debit calls.\n"
    "  * If the bug is unfixable from this file alone, return the file "
    "    UNCHANGED -- the system will detect zero diff and decline.\n"
    "  * The output must be valid Python (or Markdown for .md files).\n"
    "After the file body, on a NEW line, append exactly:\n"
    "  ###RATIONALE### <one-sentence summary of what you changed and why>\n"
    "Anything you write after ###RATIONALE### will be used as the PR body."
)


async def _ai_pick_path(
    report_text: str, signals: dict, config: dict, repo_root: Path,
    *,
    trace: TraceStep | None = None,
) -> str | None:
    """Round 1: ask the AI which file is most likely to contain the bug.
    Returns a normalised POSIX path or None on UNKNOWN / error.

    The user message includes the repository's actual list of allowed
    files so the AI picks from a real surface rather than hallucinating
    a path into the denylist (which used to drop most legit reports
    into the ``locate`` rejection bucket).

    ``trace`` (when supplied) is populated with backend / model / token
    usage / elapsed / raw output so the cog's debug DM can surface what
    actually happened.
    """
    full_file_list = _allowed_files_index(repo_root)
    # Pre-filter: extract keywords from the report and grep them against
    # paths + purposes. Cuts the LLM's prompt from 200+ paths to the
    # 5-30 that actually match the report's vocabulary, which is the
    # difference between 'AI returns UNKNOWN every time' and 'AI picks
    # the right cog 80% of the time' on flash-tier models.
    keywords = _extract_keywords(report_text)
    filtered = _keyword_filter(
        full_file_list, keywords, repo_root, max_filtered=30,
    )
    if trace is not None:
        trace.error = ""  # cleared so the filter trace below isn't masked
    # Single-match shortcut: if exactly one file matched the keywords,
    # use it directly without burning an LLM call. The validate step
    # will catch a bad pick at the generate stage anyway.
    if len(filtered) == 1:
        only = filtered[0]
        if trace is not None:
            trace.backend = "(skipped)"
            trace.model = "(keyword shortcut)"
            trace.elapsed_ms = 0
            trace.raw_output = (
                f"keyword filter matched exactly one file from "
                f"{len(keywords)} keyword(s): {', '.join(keywords[:8])}"
            )
        log.info(
            "auto_fix: locate keyword-shortcut to %s (kw=%s)", only, keywords,
        )
        return only
    # If the filter produced something, use the narrowed list; otherwise
    # fall back to the full list so the locate AI still has a shot.
    file_list = filtered or full_file_list
    # Annotate each path with the file's module-docstring purpose so the
    # AI has something to match the report against. Falls back to a bare
    # path if no docstring is parseable.
    if file_list:
        annotated_lines: list[str] = []
        for rel in file_list:
            purpose = _file_purpose(repo_root / rel)
            if purpose:
                annotated_lines.append(f"{rel}  --  {purpose}")
            else:
                annotated_lines.append(rel)
        files_block = (
            "Allowed files (pick the path -- everything before the "
            "'--' is the relative POSIX path, after is what that file "
            "does). Reply with the path verbatim or UNKNOWN:\n"
            + "\n".join(annotated_lines)
        )
    else:
        files_block = "Allowed files: (none -- return UNKNOWN)"
    kw_block = (
        f"Keyword filter extracted: {', '.join(keywords) or '(none)'}\n"
        f"Filter narrowed candidate list: "
        f"{len(full_file_list)} -> {len(file_list)} files."
    )
    body = (
        f"Bug report:\n{report_text}\n\n"
        f"Submitter signals:\n"
        + "\n".join(f"  - {k}: {v}" for k, v in (signals or {}).items())
        + f"\n\n{kw_block}\n\n{files_block}"
    )
    messages = [
        {"role": "system", "content": _LOCATE_SYSTEM_PROMPT},
        {"role": "user",   "content": body},
    ]
    backend = (config.get("backend") or "openrouter").lower()
    model   = config.get("model") or None
    base_url = (config.get("base_url") or "").strip()
    locate_max_tokens = 256  # was 64 -- too tight for "flash" models that
                              # like to emit a sentence before the path
    if trace is not None:
        trace.backend = backend
        trace.model = model or ""
        trace.prompt_chars = len(body)
        trace.max_tokens = locate_max_tokens
    raw: str | None = None
    usage: list[dict] = []
    err_out: list[str] = []
    import time as _t
    started = _t.monotonic()
    try:
        if backend == "ollama":
            from core.framework.ai.client import complete_ollama
            import os
            if base_url:
                old = os.environ.get("OLLAMA_BASE_URL", "")
                os.environ["OLLAMA_BASE_URL"] = base_url
                try:
                    raw = await complete_ollama(
                        messages, model=model or "llama3.2",
                        max_tokens=locate_max_tokens, temperature=0.0,
                        _error_out=err_out,
                    )
                finally:
                    os.environ["OLLAMA_BASE_URL"] = old
            else:
                raw = await complete_ollama(
                    messages, model=model or "llama3.2",
                    max_tokens=locate_max_tokens, temperature=0.0,
                    _error_out=err_out,
                )
        else:
            from core.framework.ai.client import complete
            raw = await complete(
                messages, model=model or None,
                max_tokens=locate_max_tokens, temperature=0.0,
                _usage_out=usage,
            )
    except Exception as exc:
        log.exception("auto_fix: locate-AI call failed")
        if trace is not None:
            trace.error = repr(exc)[:300]
            trace.elapsed_ms = int((_t.monotonic() - started) * 1000)
        return None
    if trace is not None:
        trace.elapsed_ms = int((_t.monotonic() - started) * 1000)
        trace.raw_output = (raw or "")[:400]
        if usage:
            u = usage[-1]
            trace.prompt_tokens = int(u.get("prompt_tokens") or 0)
            trace.completion_tokens = int(u.get("completion_tokens") or 0)
        if not raw and err_out:
            trace.error = err_out[-1][:300]
    if not raw:
        return None

    # Forgiving parser. Try, in order:
    #   1. The first non-empty, non-fence line (the strict format the
    #      prompt asks for).
    #   2. Any line in the response that, after normalisation, matches
    #      a path in the allowed-file index. Models that wrap the
    #      answer in markdown / preamble still produce a usable hit.
    #   3. UNKNOWN anywhere in the response -> bail out.
    body_lines = [ln.strip().strip("`'\"") for ln in raw.strip().splitlines()]
    body_lines = [ln for ln in body_lines if ln]
    if any("UNKNOWN" in ln for ln in body_lines):
        return None
    candidate: str | None = None
    if body_lines:
        first_norm = _normalise(body_lines[0]) or None
        if first_norm and first_norm in file_list:
            candidate = first_norm
        else:
            for ln in body_lines:
                # Strip common prefixes models emit ("Path:", "File:", "1.").
                stripped = ln
                for prefix in ("path:", "file:", "answer:", "- ", "* ", "1.", "1)"):
                    if stripped.lower().startswith(prefix):
                        stripped = stripped[len(prefix):].strip()
                stripped = stripped.strip("`'\"")
                norm = _normalise(stripped) or ""
                if norm and norm in file_list:
                    candidate = norm
                    break
    if not candidate:
        log.info(
            "auto_fix: AI output has no allowed path. raw=%r", raw[:300],
        )
        if trace is not None:
            trace.error = (
                "model output didn't contain any path from the allowed "
                "list -- see `output =` for the raw reply"
            )
        return None
    return candidate


async def _ai_generate_patch(
    rel_path: str, original_text: str,
    report_text: str, config: dict,
    *,
    trace: TraceStep | None = None,
) -> tuple[str, str] | None:
    """Round 2: ask the AI to rewrite the file with the fix.

    Returns ``(new_text, rationale)`` or None on error / no change /
    over-budget output. ``trace`` is populated the same way as in
    :func:`_ai_pick_path`.
    """
    body = (
        f"File path: {rel_path}\n\n"
        f"Bug report:\n{report_text}\n\n"
        f"Current file contents:\n{original_text}"
    )
    messages = [
        {"role": "system", "content": _PATCH_SYSTEM_PROMPT},
        {"role": "user",   "content": body},
    ]
    backend = (config.get("backend") or "openrouter").lower()
    model   = config.get("model") or None
    base_url = (config.get("base_url") or "").strip()
    generate_max_tokens = 4096
    if trace is not None:
        trace.backend = backend
        trace.model = model or ""
        trace.prompt_chars = len(body)
        trace.max_tokens = generate_max_tokens
    raw: str | None = None
    usage: list[dict] = []
    err_out: list[str] = []
    import time as _t
    started = _t.monotonic()
    try:
        if backend == "ollama":
            from core.framework.ai.client import complete_ollama
            import os
            if base_url:
                old = os.environ.get("OLLAMA_BASE_URL", "")
                os.environ["OLLAMA_BASE_URL"] = base_url
                try:
                    raw = await complete_ollama(
                        messages, model=model or "llama3.2",
                        max_tokens=generate_max_tokens, temperature=0.1,
                        _error_out=err_out,
                    )
                finally:
                    os.environ["OLLAMA_BASE_URL"] = old
            else:
                raw = await complete_ollama(
                    messages, model=model or "llama3.2",
                    max_tokens=generate_max_tokens, temperature=0.1,
                    _error_out=err_out,
                )
        else:
            from core.framework.ai.client import complete
            raw = await complete(
                messages, model=model or None,
                max_tokens=generate_max_tokens, temperature=0.1,
                _usage_out=usage,
            )
    except Exception as exc:
        log.exception("auto_fix: patch-AI call failed for %s", rel_path)
        if trace is not None:
            trace.error = repr(exc)[:300]
            trace.elapsed_ms = int((_t.monotonic() - started) * 1000)
        return None
    if trace is not None:
        trace.elapsed_ms = int((_t.monotonic() - started) * 1000)
        trace.raw_output = (raw or "")[:600]
        if usage:
            u = usage[-1]
            trace.prompt_tokens = int(u.get("prompt_tokens") or 0)
            trace.completion_tokens = int(u.get("completion_tokens") or 0)
        if not raw and err_out:
            trace.error = err_out[-1][:300]
    if not raw:
        return None
    if len(raw.encode("utf-8", errors="ignore")) > MAX_OUTPUT_BYTES:
        log.warning(
            "auto_fix: AI output too large for %s (%d bytes)",
            rel_path, len(raw),
        )
        if trace is not None:
            trace.error = (
                f"AI output too large ({len(raw):,} bytes > "
                f"{MAX_OUTPUT_BYTES:,} cap)"
            )
        return None
    # Split rationale off the back. If the marker is missing, fall back
    # to a generic summary -- the bot still validates the file body, it
    # just won't have a curated PR description.
    if "###RATIONALE###" in raw:
        body_part, _, rationale = raw.partition("###RATIONALE###")
        rationale = rationale.strip().splitlines()[0].strip() if rationale.strip() else ""
        new_text = body_part.rstrip() + "\n"
    else:
        rationale = ""
        new_text = raw.rstrip() + "\n"
    # Strip a leading triple-backtick fence in case the model ignored
    # the "no markdown" rule.
    if new_text.startswith("```"):
        first_nl = new_text.find("\n")
        if first_nl > 0:
            new_text = new_text[first_nl + 1:]
        if new_text.rstrip().endswith("```"):
            new_text = new_text.rstrip()[:-3].rstrip() + "\n"
    return (new_text, rationale or "AI-authored auto-fix")


# ── Validation ─────────────────────────────────────────────────────────────

def _validate_patch(rel_path: str, original: str, candidate: str) -> tuple[bool, str]:
    """Return (ok, reason). reason is empty when ok."""
    if candidate == original:
        return (False, "AI returned the file unchanged")
    if not candidate.strip():
        return (False, "AI returned empty content")
    diff_lines = _line_diff_count(original, candidate)
    if diff_lines == 0:
        return (False, "no effective changes")
    if diff_lines > MAX_DIFF_LINES:
        return (False, f"diff too large ({diff_lines} > {MAX_DIFF_LINES})")
    # Syntax check on .py
    if rel_path.endswith(".py"):
        try:
            ast.parse(candidate)
        except SyntaxError as e:
            return (False, f"new file has SyntaxError: {e.msg} at line {e.lineno}")
    # Pattern denylist on added lines only.
    bad = _denied_patterns_in(_added_lines(original, candidate))
    if bad:
        return (False, f"denied pattern in added lines: {bad[0]}")
    return (True, "")


# ── Public entry point ────────────────────────────────────────────────────

async def propose_fix(
    report_text: str,
    signals: dict,
    config: dict,
    repo_root: Path,
) -> PatchProposal | PatchRejection:
    """Two-pass auto-fix proposer. Always returns a result -- either a
    ``PatchProposal`` to ship, or a ``PatchRejection`` describing
    exactly which stage refused. Callers that previously checked
    ``if not result`` should now check ``isinstance(result, PatchRejection)``.

    Pass 1: AI picks a single likely buggy file.
    Validate: file exists + path-allowed + within repo + reasonable size.
    Pass 2: AI rewrites the file.
    Validate: compiles + bounded diff + no denied patterns.
    """
    locate_trace = TraceStep(stage="locate")
    rel_path = await _ai_pick_path(
        report_text, signals, config, repo_root, trace=locate_trace,
    )
    if not rel_path:
        log.info("auto_fix: AI returned no/UNKNOWN file")
        index_size = len(_allowed_files_index(repo_root))
        kws = _extract_keywords(report_text)
        kw_note = (
            f"Keywords extracted: {', '.join(kws)}. "
            if kws else
            "No keywords could be extracted from the report -- "
            "the text was probably too short / too vague. "
        )
        return PatchRejection(
            stage="locate",
            reason=(
                f"{kw_note}AI saw {index_size} candidate file(s) total "
                f"and still returned UNKNOWN. Try re-asking after "
                f"adding the exact command name (e.g. `,fish`), the "
                f"error text, or steps to reproduce."
            ),
            trace=[locate_trace],
        )
    allowed, why = is_path_allowed(rel_path)
    if not allowed:
        log.info("auto_fix: rejected path `%s`: %s", rel_path, why)
        return PatchRejection(
            stage="path_denied", reason=why, rel_path=rel_path,
            trace=[locate_trace],
        )
    abs_path = (repo_root / rel_path).resolve()
    try:
        abs_path.relative_to(repo_root.resolve())
    except ValueError:
        log.warning("auto_fix: path escaped repo root: %s", rel_path)
        return PatchRejection(
            stage="path_denied",
            reason="resolved path escaped repo root",
            rel_path=rel_path,
            trace=[locate_trace],
        )
    if not abs_path.is_file():
        log.info("auto_fix: AI picked non-existent file `%s`", rel_path)
        return PatchRejection(
            stage="file_missing",
            reason=f"`{rel_path}` doesn't exist in this checkout",
            rel_path=rel_path,
            trace=[locate_trace],
        )
    try:
        original = abs_path.read_text(encoding="utf-8")
    except Exception as exc:
        log.exception("auto_fix: failed to read %s", rel_path)
        return PatchRejection(
            stage="file_missing",
            reason=f"failed to read `{rel_path}`: {exc!r}",
            rel_path=rel_path,
            trace=[locate_trace],
        )
    if len(original) > 200_000:
        log.info("auto_fix: target file too large to patch safely (%s)", rel_path)
        return PatchRejection(
            stage="file_too_large",
            reason=(
                f"`{rel_path}` is {len(original):,} bytes (>200KB cap). "
                f"Files this size need a manual diff -- the LLM context "
                f"window can't safely round-trip them."
            ),
            rel_path=rel_path,
            trace=[locate_trace],
        )
    generate_trace = TraceStep(stage="generate")
    out = await _ai_generate_patch(
        rel_path, original, report_text, config, trace=generate_trace,
    )
    if not out:
        return PatchRejection(
            stage="generate",
            reason=(
                f"AI didn't produce a usable patch for `{rel_path}` "
                f"(empty / oversized output / network error)."
            ),
            rel_path=rel_path,
            trace=[locate_trace, generate_trace],
        )
    new_text, rationale = out
    ok, why = _validate_patch(rel_path, original, new_text)
    if not ok:
        log.info("auto_fix: patch rejected for `%s`: %s", rel_path, why)
        return PatchRejection(
            stage="validate",
            reason=f"validation rejected the patch for `{rel_path}`: {why}",
            rel_path=rel_path,
            trace=[locate_trace, generate_trace],
        )
    summary = (rationale or "auto-fix").strip()
    if len(summary) > 72:
        summary = summary[:69] + "..."
    return PatchProposal(
        rel_path=rel_path,
        original_text=original,
        new_text=new_text,
        summary=summary,
        rationale=rationale,
        lines_changed=_line_diff_count(original, new_text),
        trace=[locate_trace, generate_trace],
    )
