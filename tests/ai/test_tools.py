"""ToolRegistry registration + format checks."""
from __future__ import annotations

import pytest

from ai.tools import ToolRegistry


@pytest.mark.asyncio
async def test_decorator_registers_tool():
    reg = ToolRegistry()

    @reg.tool(
        name="echo",
        description="Echo back",
        schema={"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"]},
    )
    async def _echo(args, ctx):
        return {"ok": True, "x": args["x"]}

    assert "echo" in reg.names()
    out = reg.as_openai_tools()
    schema, handler = out["echo"]
    assert "description" in schema
    assert "parameters" in schema
    assert schema["parameters"]["required"] == ["x"]

    result = await handler({"x": "hi"}, {})
    assert result == {"ok": True, "x": "hi"}


def test_merge_returns_combined_registry():
    a = ToolRegistry()
    b = ToolRegistry()
    a.register("a_only", "a", {}, _stub_handler)
    b.register("b_only", "b", {}, _stub_handler)
    b.register("a_only", "b's version", {}, _stub_handler)  # collision

    merged = a.merge(b)
    assert set(merged.names()) == {"a_only", "b_only"}
    # `b` wins on collision
    schema_a, _ = merged.as_openai_tools()["a_only"]
    assert schema_a["description"] == "b's version"


async def _stub_handler(args, ctx):
    return {"ok": True}
