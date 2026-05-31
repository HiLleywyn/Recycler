"""
core/framework/agent_tools/validation.py -- strict input coercion and validation.

The AI produces raw dicts that can include wrong types, extra keys, or
hallucinated shapes. validate_args() is the single choke point that every
tool call passes through before the handler runs.
"""
from __future__ import annotations

import math
from typing import Any

from .core import ParamSpec, ToolSpec


class ToolValidationError(ValueError):
    """Raised when the AI's arguments do not match a tool's schema."""


def validate_args(spec: ToolSpec, raw: dict | None) -> dict:
    """Coerce + validate raw args against a tool spec.

    - Rejects unknown keys so the AI can't smuggle in ``__import__`` etc.
    - Coerces primitives from strings (the Ollama tool path emits all JSON).
    - Enforces choices / min / max / required.
    """
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ToolValidationError(f"expected object args, got {type(raw).__name__}")

    out: dict[str, Any] = {}
    known = {p.name for p in spec.params}

    for p in spec.params:
        if p.name not in raw:
            if p.required and p.default is None:
                raise ToolValidationError(f"missing required param {p.name!r}")
            out[p.name] = p.default
            continue
        try:
            out[p.name] = _coerce(raw[p.name], p)
        except (TypeError, ValueError) as exc:
            raise ToolValidationError(f"param {p.name!r}: {exc}") from None
        if p.choices and out[p.name] not in p.choices:
            raise ToolValidationError(
                f"param {p.name!r}: {out[p.name]!r} not in {list(p.choices)}"
            )
        if p.type in ("int", "float") and out[p.name] is not None:
            v = out[p.name]
            if p.min is not None and v < p.min:
                raise ToolValidationError(f"param {p.name!r}: {v} < {p.min}")
            if p.max is not None and v > p.max:
                raise ToolValidationError(f"param {p.name!r}: {v} > {p.max}")

    extras = [k for k in raw.keys() if k not in known and not k.startswith("_")]
    if extras:
        raise ToolValidationError(f"unknown params: {sorted(extras)}")

    # Preserve leading-underscore metadata keys (used by triggers to pass
    # firing context through without counting as user input).
    for k, v in raw.items():
        if k.startswith("_") and k not in out:
            out[k] = v

    return out


def _coerce(val: Any, p: ParamSpec) -> Any:
    t = p.type
    if val is None:
        if p.required and p.default is None:
            raise ValueError("null not allowed")
        return p.default

    if t in ("str", "symbol", "network", "uid"):
        s = str(val).strip()
        if not s:
            raise ValueError("empty string")
        if t == "symbol":
            s = s.upper()
            if len(s) > 12 or not s.replace("_", "").isalnum():
                raise ValueError(f"invalid symbol {s!r}")
        elif t == "network":
            s = s.lower()
            if s not in ("dsc", "arc", "mta", "sun"):
                raise ValueError(f"unknown network {s!r}")
        elif t == "uid":
            try:
                s = str(int(s))
            except (TypeError, ValueError):
                raise ValueError("uid must be numeric")
        else:
            if len(s) > 2000:
                raise ValueError("string too long")
        return s

    if t == "int":
        if isinstance(val, bool):
            raise ValueError("bool is not int")
        if isinstance(val, float) and not math.isfinite(val):
            raise ValueError("nan/inf not allowed")
        i = int(val)
        return i

    if t == "float":
        if isinstance(val, bool):
            raise ValueError("bool is not float")
        f = float(val)
        if not math.isfinite(f):
            raise ValueError("nan/inf not allowed")
        return f

    if t == "bool":
        if isinstance(val, bool):
            return val
        s = str(val).strip().lower()
        if s in ("1", "true", "yes", "y", "on"):
            return True
        if s in ("0", "false", "no", "n", "off"):
            return False
        raise ValueError("not a bool")

    if t == "json":
        if isinstance(val, (dict, list)):
            return val
        raise ValueError("expected json object or array")

    raise ValueError(f"unknown type {t!r}")
