"""core/framework/ai/github_pr.py  -  Open a single-file PR on the configured
GitHub repo, used by ``core/framework/ai/auto_fix.py`` to ship Tier-A AI patches.

Authenticates with ``Config.GITHUB_TOKEN``, targets
``Config.AUTOFIX_REPO_OWNER`` / ``AUTOFIX_REPO_NAME``, and bases the
branch on ``Config.AUTOFIX_BASE_BRANCH`` (default "main"). Every helper
returns ``None`` instead of raising on failure so the caller can degrade
gracefully -- a missing token or a 422 from GitHub means the human DM
still goes out, just without a PR link.

The flow uses the REST v3 contents API (one PUT per file) instead of the
git-data API. That's intentional: contents API takes the new file body
verbatim and creates the commit + tree + blob in one round trip, which
matches our "exactly one file changes per PR" guardrail set in auto_fix.

If you ever need multi-file PRs, switch to the git-data flow (refs +
trees + blobs) -- but you should also raise ``MAX_DIFF_LINES`` and
re-think the validators; multi-file changes are out of scope for the
Tier-A safety model.
"""
from __future__ import annotations

import base64
import logging
from typing import Any

import aiohttp

from core.config import Config

log = logging.getLogger("discoin.github_pr")

_API_ROOT = "https://api.github.com"
_HTTP_TIMEOUT = aiohttp.ClientTimeout(total=20.0)


def is_configured() -> bool:
    """True only if every piece needed to open a PR is set in env."""
    return bool(
        (Config.GITHUB_TOKEN or "").strip()
        and (Config.AUTOFIX_REPO_OWNER or "").strip()
        and (Config.AUTOFIX_REPO_NAME or "").strip()
    )


async def check_auth() -> dict:
    """Hit ``GET /repos/{owner}/{repo}`` to verify the token is wired up.

    No side effects -- the endpoint is read-only. Returns a dict the
    cog can render verbatim:
        {
          "ok":      bool,
          "status":  int,
          "private": bool | None,
          "scopes":  list[str] | None,   # from X-OAuth-Scopes if present
          "reason":  str,                # short human-readable
        }

    Useful permission states:
      * 200, ``private: True``                  -- token works, repo is private
      * 200, ``private: False``                 -- token works, repo is public
      * 401                                     -- token is invalid / expired
      * 403                                     -- token is valid but lacks scope
                                                  (or repo blocks app access)
      * 404                                     -- repo doesn't exist OR token
                                                  can't see it (private + no
                                                  read perms looks identical
                                                  to "doesn't exist" by design)
    """
    if not is_configured():
        return {
            "ok": False,
            "status": 0,
            "private": None,
            "scopes": None,
            "reason": (
                "GITHUB_TOKEN / AUTOFIX_REPO_OWNER / AUTOFIX_REPO_NAME "
                "is missing in env."
            ),
        }
    owner = Config.AUTOFIX_REPO_OWNER
    repo  = Config.AUTOFIX_REPO_NAME
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(
                f"{_API_ROOT}/repos/{owner}/{repo}",
                headers=_headers(),
                timeout=_HTTP_TIMEOUT,
            ) as resp:
                status = int(resp.status)
                # X-OAuth-Scopes is only set for classic PATs; fine-
                # grained tokens leave it absent (the scope set is
                # immutable on the token itself, not exposed via header).
                scope_hdr = resp.headers.get("X-OAuth-Scopes") or ""
                scopes = [s.strip() for s in scope_hdr.split(",") if s.strip()] or None
                try:
                    body = await resp.json()
                except aiohttp.ContentTypeError:
                    body = {}
                if status == 200 and isinstance(body, dict):
                    return {
                        "ok": True,
                        "status": status,
                        "private": bool(body.get("private")),
                        "scopes": scopes,
                        "reason": (
                            f"OK -- repo is "
                            f"{'private' if body.get('private') else 'public'}, "
                            f"default branch `{body.get('default_branch') or '?'}`."
                        ),
                    }
                msg_map = {
                    401: "Token is invalid or expired (HTTP 401).",
                    403: (
                        "Token rejected (HTTP 403). The token is valid but "
                        "lacks the scope to see this repo, or repo settings "
                        "block app access."
                    ),
                    404: (
                        "Repo not found (HTTP 404). Either the owner/name "
                        "is wrong or the token can't see the repo (private "
                        "repos return 404 to unauthenticated tokens to "
                        "avoid leaking existence)."
                    ),
                }
                reason = msg_map.get(
                    status, f"Unexpected HTTP {status}: {body!r}",
                )
                return {
                    "ok": False, "status": status,
                    "private": None, "scopes": scopes, "reason": reason,
                }
        except Exception as exc:
            return {
                "ok": False, "status": 0,
                "private": None, "scopes": None,
                "reason": f"Network / runtime error: {exc!r}",
            }


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {Config.GITHUB_TOKEN}",
        "Accept":        "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent":    "discoin-autofix/1.0",
    }


async def _api(
    session: aiohttp.ClientSession,
    method: str,
    path: str,
    *,
    params: dict | None = None,
    json: dict | None = None,
) -> tuple[int, Any]:
    """Single-shot API call. Returns (status, parsed_json_or_text).

    Errors return their HTTP status and the parsed body so the caller
    can log a useful failure message; we never raise to the cog.
    """
    url = f"{_API_ROOT}{path}"
    try:
        async with session.request(
            method, url,
            headers=_headers(),
            params=params,
            json=json,
            timeout=_HTTP_TIMEOUT,
        ) as resp:
            try:
                body = await resp.json()
            except aiohttp.ContentTypeError:
                body = await resp.text()
            return (resp.status, body)
    except Exception as exc:
        log.warning("github_pr: %s %s failed: %s", method, path, exc)
        return (0, None)


async def _get_base_sha(
    session: aiohttp.ClientSession, owner: str, repo: str, base_branch: str,
) -> str | None:
    status, body = await _api(
        session, "GET", f"/repos/{owner}/{repo}/git/ref/heads/{base_branch}",
    )
    if status != 200 or not isinstance(body, dict):
        log.warning(
            "github_pr: get base ref failed status=%s body=%r", status, body,
        )
        return None
    return str(((body or {}).get("object") or {}).get("sha") or "") or None


async def _create_branch(
    session: aiohttp.ClientSession, owner: str, repo: str,
    branch: str, base_sha: str,
) -> bool:
    status, body = await _api(
        session, "POST", f"/repos/{owner}/{repo}/git/refs",
        json={"ref": f"refs/heads/{branch}", "sha": base_sha},
    )
    if status in (200, 201):
        return True
    # 422 = branch already exists. Treat as soft-success so a retry on the
    # same report id reuses the branch instead of crashing.
    if status == 422:
        log.info("github_pr: branch %s already exists, reusing", branch)
        return True
    log.warning(
        "github_pr: create branch %s failed status=%s body=%r",
        branch, status, body,
    )
    return False


async def _get_existing_blob_sha(
    session: aiohttp.ClientSession, owner: str, repo: str,
    rel_path: str, branch: str,
) -> str | None:
    """Contents API requires the existing blob's sha to update a file.
    Returns the sha or None if the file is new on this branch.
    """
    status, body = await _api(
        session, "GET", f"/repos/{owner}/{repo}/contents/{rel_path}",
        params={"ref": branch},
    )
    if status == 200 and isinstance(body, dict):
        sha = body.get("sha")
        return str(sha) if sha else None
    return None


async def _put_file(
    session: aiohttp.ClientSession, owner: str, repo: str,
    rel_path: str, branch: str, new_text: str, commit_msg: str,
    existing_sha: str | None,
) -> bool:
    payload: dict[str, Any] = {
        "message": commit_msg,
        "content": base64.b64encode(new_text.encode("utf-8")).decode("ascii"),
        "branch":  branch,
        "committer": {
            "name":  "discoin-autofix",
            "email": "lleywyn@proton.me",
        },
    }
    if existing_sha:
        payload["sha"] = existing_sha
    status, body = await _api(
        session, "PUT", f"/repos/{owner}/{repo}/contents/{rel_path}",
        json=payload,
    )
    if status in (200, 201):
        return True
    log.warning(
        "github_pr: PUT %s on %s failed status=%s body=%r",
        rel_path, branch, status, body,
    )
    return False


async def _open_pull_request(
    session: aiohttp.ClientSession, owner: str, repo: str,
    branch: str, base: str, title: str, body: str,
) -> tuple[str, int] | None:
    status, resp = await _api(
        session, "POST", f"/repos/{owner}/{repo}/pulls",
        json={
            "title": title,
            "head":  branch,
            "base":  base,
            "body":  body,
            # ALWAYS as draft. The whole point of Tier A is that a human
            # reviews + merges; draft state makes the gate explicit and
            # prevents Railway from auto-deploying if the repo has merge
            # automation rules elsewhere.
            "draft": True,
        },
    )
    if status in (200, 201) and isinstance(resp, dict):
        url = str(resp.get("html_url") or "")
        num = resp.get("number")
        if url and isinstance(num, int):
            return (url, int(num))
    log.warning(
        "github_pr: open PR failed status=%s body=%r", status, resp,
    )
    return None


async def get_pr_state(pr_number: int) -> dict | None:
    """Return a small subset of the PR row used by the auto-close loop.

    Keys returned (subset of GitHub's PR object):
        merged    -- bool
        state     -- "open" | "closed"
        merged_at -- ISO8601 string or None
        html_url  -- str
    Returns None if the API call fails or the PR doesn't exist. The
    auto-close loop treats None as "no decision yet, try again next tick".
    """
    if not is_configured():
        return None
    owner = Config.AUTOFIX_REPO_OWNER
    repo  = Config.AUTOFIX_REPO_NAME
    async with aiohttp.ClientSession() as session:
        status, body = await _api(
            session, "GET", f"/repos/{owner}/{repo}/pulls/{int(pr_number)}",
        )
        if status != 200 or not isinstance(body, dict):
            log.debug(
                "github_pr: get_pr_state #%s status=%s", pr_number, status,
            )
            return None
        return {
            "merged":    bool(body.get("merged")),
            "state":     str(body.get("state") or "unknown"),
            "merged_at": body.get("merged_at"),
            "html_url":  str(body.get("html_url") or ""),
        }


async def open_issue(
    *,
    title: str,
    body: str,
    labels: list[str] | None = None,
    _error_out: list | None = None,
) -> tuple[str, int] | None:
    """Open a GitHub issue on the configured repo. Returns
    ``(html_url, number)`` on success, ``None`` on any failure.

    ``labels`` is intentionally NOT sent to the GitHub API by default
    -- the previous implementation passed ``["auto-fix", "bug-report",
    "ai-triage"]`` which 422'd silently on repos that didn't have
    those labels pre-created, so every issue creation looked like a
    network failure. Pass labels explicitly only when you've ensured
    they exist on the target repo.

    ``_error_out`` accumulates a short reason string on failure so the
    caller can surface it. Mirrors the pattern in
    :func:`core.framework.ai.client.complete_ollama`.
    """
    def _err(msg: str) -> None:
        if _error_out is not None:
            _error_out.append(msg)
    if not is_configured():
        log.info("github_pr: open_issue skipped, not configured")
        _err("not configured (GITHUB_TOKEN / AUTOFIX_REPO_OWNER / AUTOFIX_REPO_NAME)")
        return None
    owner = Config.AUTOFIX_REPO_OWNER
    repo  = Config.AUTOFIX_REPO_NAME
    payload: dict[str, Any] = {"title": title, "body": body}
    if labels:
        payload["labels"] = list(labels)
    async with aiohttp.ClientSession() as session:
        status, resp = await _api(
            session, "POST", f"/repos/{owner}/{repo}/issues",
            json=payload,
        )
        if status in (200, 201) and isinstance(resp, dict):
            url = str(resp.get("html_url") or "") or None
            num = resp.get("number")
            if url and isinstance(num, int):
                return (url, int(num))
            _err(f"unexpected response shape: {str(resp)[:200]}")
            return None
        log.warning(
            "github_pr: open_issue failed status=%s body=%r", status, resp,
        )
        # Surface the actual GitHub message so the admin can fix it.
        # 422 = bad payload (often unknown label).
        # 401 = bad token. 403 = scope. 404 = repo missing / no read.
        body_excerpt = ""
        if isinstance(resp, dict):
            body_excerpt = str(resp.get("message") or resp)[:200]
        else:
            body_excerpt = str(resp)[:200]
        _err(f"HTTP {status}: {body_excerpt}")
        return None


async def open_single_file_pr(
    *,
    branch: str,
    rel_path: str,
    new_text: str,
    commit_msg: str,
    pr_title: str,
    pr_body: str,
    _error_out: list | None = None,
) -> tuple[str, int] | None:
    """Single-call helper used by the auto-fix path.

    Creates ``branch`` from ``Config.AUTOFIX_BASE_BRANCH``, commits the
    new file body via the Contents API, and opens a draft PR back to
    the base. Returns ``(html_url, pr_number)`` on success, ``None``
    on any failure. ``_error_out`` (when supplied) is appended with a
    short reason so the caller can surface it instead of just logging.
    """
    def _err(msg: str) -> None:
        if _error_out is not None:
            _error_out.append(msg)
    if not is_configured():
        log.info("github_pr: not configured (token / owner / name missing)")
        _err("not configured (token / owner / name missing)")
        return None
    owner = Config.AUTOFIX_REPO_OWNER
    repo  = Config.AUTOFIX_REPO_NAME
    base  = Config.AUTOFIX_BASE_BRANCH or "main"

    async with aiohttp.ClientSession() as session:
        base_sha = await _get_base_sha(session, owner, repo, base)
        if not base_sha:
            _err(f"failed to read base ref refs/heads/{base}")
            return None
        if not await _create_branch(session, owner, repo, branch, base_sha):
            _err(f"failed to create branch {branch}")
            return None
        existing_sha = await _get_existing_blob_sha(
            session, owner, repo, rel_path, branch,
        )
        ok = await _put_file(
            session, owner, repo,
            rel_path, branch, new_text, commit_msg, existing_sha,
        )
        if not ok:
            _err(f"PUT /contents/{rel_path} failed")
            return None
        result = await _open_pull_request(
            session, owner, repo, branch, base, pr_title, pr_body,
        )
        if not result:
            _err(f"POST /pulls failed for branch {branch}")
        return result
