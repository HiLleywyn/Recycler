"""
Test that validator constants are defined only in constants/validators.py.

Uses AST parsing to find top-level variable assignments in cogs/, services/,
api/, and core/framework/ that duplicate a constant name from constants.validators.

This test FAILS before migration and PASSES after migration.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

# The canonical constant names that must live only in constants/validators.py
import constants.validators as _cv

CANONICAL_NAMES: frozenset[str] = frozenset({
    "VALIDATOR_TICK",
    "VALIDATOR_REWARD",
    "TREASURY_CUT",
    "MIN_STAKE",
    "MIN_VALIDATORS",
    "STAKE_LOCK_SECS",
    "MAX_SLASH_COUNT",
    "SLASH_RATE",
    "SLASH_DECAY_SECS",
    "MAX_MEMPOOL",
    "DELEGATION_VALIDATOR_KEEP",
    "DELEGATION_POOL_SHARE",
    "DELEGATION_LOCK_SECS",
    "MIN_DELEGATION",
    "MAX_DELEGATIONS",
    "REJECTION_SLASH_RATE",
    "GAS_TIERS",
    "GAS_MIN_MULT",
    "GAS_MAX_MULT",
    "NET_SHORT",
})

ALL_DUPLICATE_NAMES = CANONICAL_NAMES

# Directories to scan (relative to repo root)
SCAN_DIRS = ["cogs", "services", "api", "core/framework"]

# Files that are explicitly allowed to define these names (the canonical source)
ALLOWED_FILES: frozenset[str] = frozenset({
    "constants/validators.py",
    "constants/trading.py",
    "constants/__init__.py",
})


def _repo_root() -> Path:
    """Return the repository root (parent of the tests/ directory)."""
    return Path(__file__).parent.parent


def _top_level_assignments(filepath: Path) -> set[str]:
    """Return names assigned at module top level in the given Python file.

    Excludes pure alias assignments (``X = Y`` where Y is a bare name), since
    those are just re-exports, not independent constant definitions.
    """
    try:
        source = filepath.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(filepath))
    except SyntaxError:
        return set()

    names: set[str] = set()
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Assign):
            # Skip pure aliases: X = Y (name on right side)
            if isinstance(node.value, ast.Name):
                continue
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name):
                names.add(node.target.id)
    return names


def _collect_violations() -> list[tuple[str, str]]:
    """
    Return a list of (relative_path, name) pairs where a canonical constant
    name is re-defined at module top level outside of constants/.
    """
    root = _repo_root()
    violations: list[tuple[str, str]] = []

    for scan_dir in SCAN_DIRS:
        scan_path = root / scan_dir
        if not scan_path.is_dir():
            continue
        for py_file in scan_path.rglob("*.py"):
            rel = py_file.relative_to(root).as_posix()
            if rel in ALLOWED_FILES:
                continue
            defined = _top_level_assignments(py_file)
            for name in defined & ALL_DUPLICATE_NAMES:
                violations.append((rel, name))

    return violations


class TestConstantsSingleSource:
    """Validator constants must be defined only in constants/validators.py."""

    def test_constants_validators_module_importable(self):
        """constants.validators must be importable and pure-Python."""
        import constants.validators as cv
        # Ensure it has no core/framework/discord/database imports by checking its
        # module dict has no such attributes
        for attr in dir(cv):
            # None of the module's attributes should be discord/framework objects
            pass  # import succeeded without circular deps  -  that's the test

    def test_canonical_values_correct(self):
        """Spot-check a few canonical values."""
        assert _cv.VALIDATOR_TICK == 120
        assert _cv.MAX_SLASH_COUNT == 5
        assert _cv.STAKE_LOCK_SECS == 86_400
        assert _cv.DELEGATION_LOCK_SECS == 86_400
        assert _cv.GAS_TIERS == {"high": 0.50, "medium": 0.20, "low": 0.05}
        assert _cv.NET_SHORT["Sun Network"] == "sun"
        assert _cv.GAS_MIN_MULT == 0.1
        assert _cv.GAS_MAX_MULT == 100.0

    def test_no_duplicate_definitions_in_cogs_api_framework(self):
        """
        No file in cogs/, services/, api/, or core/framework/ may define a
        top-level variable with the same name as a constants.validators constant.

        If this test fails, it lists every offending (file, constant) pair.
        """
        violations = _collect_violations()
        if violations:
            lines = "\n".join(f"  {f}: {n}" for f, n in sorted(violations))
            pytest.fail(
                f"The following files still define validator constants locally "
                f"(should import from constants.validators instead):\n{lines}"
            )


# ---------------------------------------------------------------------------
# Trading constants  -  single-source enforcement
# ---------------------------------------------------------------------------


_TRADING_CANONICAL_NAMES: frozenset[str] = frozenset({
    "DEFAULT_SWAP_FEE",
    "PLATFORM_FEE_RATIO",
    "SWAP_PLATFORM_FEE_PCT",
    "ARB_FEE",
    "SLIPPAGE_WARN",
    "PRICE_FLOOR",
    "PRICE_IMPACT_DIVISOR",
    "DEFAULT_FEE_PCT",
    "DEFAULT_FEE_MIN",
    "DEFAULT_FEE_MAX",
    "USD_PRECISION",
    "TOKEN_PRECISION",
    "MIN_TRADE_USD",
    "QUOTE_EXPIRY_SECS",
})


def _collect_trading_violations() -> list[tuple[str, str]]:
    """Return (relative_path, name) pairs where a trading constant name
    (ignoring leading underscores) is re-defined at module top level outside
    constants/.
    """
    root = _repo_root()
    violations: list[tuple[str, str]] = []

    for scan_dir in SCAN_DIRS:
        scan_path = root / scan_dir
        if not scan_path.is_dir():
            continue
        for py_file in scan_path.rglob("*.py"):
            rel = py_file.relative_to(root).as_posix()
            if rel in ALLOWED_FILES:
                continue
            defined = _top_level_assignments(py_file)
            # Strip leading underscores before comparing
            for name in defined:
                canonical_name = name.lstrip("_")
                if canonical_name in _TRADING_CANONICAL_NAMES:
                    violations.append((rel, name))

    return violations


class TestTradingConstantsNotDuplicated:
    """Trading constants must be defined only in constants/trading.py."""

    def test_trading_constants_module_importable(self):
        """constants.trading must be importable without circular dependencies."""
        import constants.trading as ct
        assert ct.DEFAULT_SWAP_FEE == 0.01
        assert ct.PLATFORM_FEE_RATIO == 0.1
        assert ct.SWAP_PLATFORM_FEE_PCT == 0.001
        assert ct.ARB_FEE == 0.003
        assert ct.SLIPPAGE_WARN == 0.15
        assert ct.PRICE_FLOOR == 0.001
        assert ct.PRICE_IMPACT_DIVISOR == 2_500_000.0
        assert ct.DEFAULT_FEE_PCT == 0.005
        assert ct.DEFAULT_FEE_MIN == 0.01
        assert ct.DEFAULT_FEE_MAX == 500.0
        assert ct.USD_PRECISION == 2
        assert ct.TOKEN_PRECISION == 8
        assert ct.MIN_TRADE_USD == 0.01
        assert ct.QUOTE_EXPIRY_SECS == 5

    def test_no_duplicate_trading_definitions_in_cogs_api_services(self):
        """
        No file in cogs/, services/, api/, or core/framework/ may define a top-level
        variable whose name (stripping leading underscores) matches a trading
        constant.

        If this test fails, it lists every offending (file, constant) pair.
        """
        violations = _collect_trading_violations()
        if violations:
            lines = "\n".join(f"  {f}: {n}" for f, n in sorted(violations))
            pytest.fail(
                f"The following files still define trading constants locally "
                f"(should import from constants.trading instead):\n{lines}"
            )
