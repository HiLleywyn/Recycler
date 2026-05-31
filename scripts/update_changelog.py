#!/usr/bin/env python3
"""
Auto-generate changelog entries from git commits on main, dev, and nightly branches.

Runs daily via GitHub Actions (see .github/workflows/changelog.yml).
Scans the last 24 hours of commits on each branch and prepends entries
to CHANGELOG.md in the project's established format.
"""

import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

BRANCHES = ["main", "dev", "nightly"]
CHANGELOG_FILE = Path(__file__).parent.parent / "CHANGELOG.md"

# Conventional commit type → section heading
COMMIT_TYPE_MAP = {
    "feat": "New Features",
    "fix": "Bug Fixes",
    "perf": "Performance",
    "refactor": "Refactoring",
    "docs": "Documentation",
    "style": "Style",
    "test": "Tests",
    "chore": "Maintenance",
    "ci": "CI/CD",
    "security": "Security",
    "api": "API Changes",
    "db": "Database",
    "ui": "Frontend/UI",
    "build": "Build",
    "revert": "Reverts",
}

# File path prefix → section heading (evaluated in order, first match wins)
PATH_SECTION_MAP = [
    (r"^api/", "API Changes"),
    (r"^frontend/", "Frontend/UI"),
    (r"^cogs/", "Discord Bot"),
    (r"^database/|^migrations/", "Database"),
    (r"^docs/", "Documentation"),
    (r"^security/", "Security"),
    (r"^services/", "Services"),
    (r"^core/framework/", "Framework"),
    (r"^tests/", "Tests"),
    (r"^\.github/", "CI/CD"),
    (r"^scripts/", "Scripts"),
    (r"^config\.py$|^configs/", "Configuration"),
    (r"^Dockerfile|^docker", "Build"),
    (r"^requirements", "Dependencies"),
]


def run(cmd: list[str], check: bool = False) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True, check=check)
    return result.stdout.strip()


def branch_exists_on_remote(branch: str) -> bool:
    out = run(["git", "ls-remote", "--heads", "origin", branch])
    return bool(out.strip())


def fetch_branch(branch: str) -> bool:
    result = subprocess.run(
        ["git", "fetch", "origin", branch],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def get_commits_since(branch: str, since_iso: str) -> list[dict]:
    """Return commits on origin/<branch> in the last 24 hours (no merges)."""
    raw = run([
        "git", "log", f"origin/{branch}",
        f"--since={since_iso}",
        "--no-merges",
        "--format=%H%x00%s%x00%b%x00END",
    ])
    if not raw:
        return []

    commits = []
    # Split on the END sentinel we embedded in the format
    for block in raw.split("\x00END"):
        block = block.strip()
        if not block:
            continue
        parts = block.split("\x00", 2)
        if len(parts) < 2:
            continue
        sha = parts[0].strip()
        subject = parts[1].strip()
        body = parts[2].strip() if len(parts) > 2 else ""

        if not sha or not subject:
            continue

        # Skip our own auto-commits
        if re.search(r"auto-update changelog|\[skip ci\]", subject, re.IGNORECASE):
            continue

        commits.append({"sha": sha[:8], "subject": subject, "body": body})

    return commits


def get_changed_files(full_sha: str) -> list[str]:
    out = run(["git", "diff-tree", "--no-commit-id", "-r", "--name-only", full_sha])
    return out.splitlines() if out else []


def categorize(subject: str, files: list[str]) -> str:
    """Determine the section heading for a commit."""
    # Conventional commit prefix (feat:, fix(scope):, etc.)
    m = re.match(r"^(\w+)(\(.+?\))?!?:\s*", subject)
    if m:
        ctype = m.group(1).lower()
        if ctype in COMMIT_TYPE_MAP:
            return COMMIT_TYPE_MAP[ctype]

    # Fall back to changed-file heuristic
    for pattern, section in PATH_SECTION_MAP:
        for f in files:
            if re.match(pattern, f):
                return section

    return "Changes"


def format_bullet(commit: dict, files: list[str]) -> tuple[str, str]:
    """Return (section_heading, formatted_bullet)."""
    subject = commit["subject"]
    body = commit["body"]

    # Strip conventional prefix for the display text
    display = re.sub(r"^(\w+)(\(.+?\))?!?:\s*", "", subject).strip() or subject
    display = display[0].upper() + display[1:]

    section = categorize(subject, files)

    bullet = f"- **{display}**"
    if body:
        body_lines = [ln.strip() for ln in body.splitlines() if ln.strip()][:3]
        if body_lines:
            bullet += ": " + "; ".join(body_lines)
    bullet += f" (`{commit['sha']}`)"

    return section, bullet


def build_section(branch: str, commits: list[dict]) -> str:
    """Build the full changelog block for one branch."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    sections: dict[str, list[str]] = {}
    for commit in commits:
        # Use the full 40-char SHA for diff-tree
        full_sha = run(["git", "rev-parse", commit["sha"]])
        files = get_changed_files(full_sha or commit["sha"])
        heading, bullet = format_bullet(commit, files)
        sections.setdefault(heading, []).append(bullet)

    lines: list[str] = [f"## [{branch}] \u2014 {today}", ""]
    for heading, bullets in sections.items():
        lines.append(f"### {heading}")
        lines.extend(bullets)
        lines.append("")
    lines.append("---")
    lines.append("")

    return "\n".join(lines)


def main() -> None:
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    new_sections: list[str] = []
    for branch in BRANCHES:
        if not branch_exists_on_remote(branch):
            print(f"  [skip] {branch}  -  branch not found on remote")
            continue
        if not fetch_branch(branch):
            print(f"  [skip] {branch}  -  fetch failed")
            continue

        commits = get_commits_since(branch, since)
        if not commits:
            print(f"  [skip] {branch}  -  no new commits in the last 24h")
            continue

        print(f"  [ok]   {branch}  -  {len(commits)} commit(s)")
        new_sections.append(build_section(branch, commits))

    if not new_sections:
        print("Nothing to add to CHANGELOG.md.")
        return

    new_block = "\n".join(new_sections)

    if CHANGELOG_FILE.exists():
        existing = CHANGELOG_FILE.read_text(encoding="utf-8")
    else:
        existing = "# Changelog\n\n"

    # Preserve the `# Changelog` header, inject new entries right after it
    lines = existing.splitlines(keepends=True)
    if lines and lines[0].startswith("# "):
        header = lines[0]
        rest = "".join(lines[1:]).lstrip("\n")
        updated = header + "\n" + new_block + "\n" + rest
    else:
        updated = new_block + "\n" + existing

    CHANGELOG_FILE.write_text(updated, encoding="utf-8")
    print(f"CHANGELOG.md updated with {len(new_sections)} new section(s).")


if __name__ == "__main__":
    main()
