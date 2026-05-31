"""
core/framework/agent_tools -- Discoin agent tool framework.

Public API:

    from core.framework.agent_tools import (
        AgentTools,         # service container attached to the bot
        ToolContext,        # what every tool handler gets
        ToolResult,         # structured result envelope
        RiskLevel,          # READ / SAFE / MUTATE / DANGER
        ToolRegistry,       # discover registered tools
        run_tool,           # the single execution choke point
        ChainStep, ChainExecutor, parse_chain_plan,
        enqueue_task, TaskQueueWorker,
        TriggerEngine, create_trigger, list_triggers, delete_trigger,
        registry_state,     # enable/disable state for tools/plugins/hooks/agents
        disrepo,            # installer for the disrepo catalog
    )

Design rules (see CLAUDE.md for the repo-wide rules):

  1. Fewer, more powerful tools with strict schemas -- never one-per-tiny-action.
  2. Every tool returns a ToolResult (ok/data/error/meta), never a raw blob.
  3. Inputs go through a validation layer before the handler runs.
  4. Risk level drives an explicit approval step for dangerous actions.
  5. Automation state (queue/triggers/chain runs) persists across restarts.
  6. All execution flows through the same run_tool choke point, so audit,
     cooldowns, and the approval policy can never be bypassed.
  7. Every callable thing (built-in tool, Lua plugin, chat hook, installed
     agent) is gated by registry_state.is_enabled so operators can flip any
     item on/off via ,ai <group> enable|disable <name>.

Load once during bot startup:

    from core.framework.agent_tools import AgentTools
    bot.agent_tools = AgentTools(bot)
    bot.agent_tools.start()
"""
from __future__ import annotations

import logging

from .core import (
    ParamSpec,
    RiskLevel,
    ToolContext,
    ToolRegistry,
    ToolResult,
    ToolSpec,
    tool,
)
from .executor import decide_approval, request_approval, run_tool
from .validation import ToolValidationError, validate_args
from .chain import ChainExecutor, ChainRun, ChainStep, parse_chain_plan
from .queue import (
    TaskQueueWorker,
    cancel_task,
    enqueue_task,
    list_user_tasks,
)
from .triggers import (
    TriggerEngine,
    create_trigger,
    delete_trigger,
    list_triggers,
)
from .ai_bridge import complete_with_agent_tools, complete_with_agent_tools_stream
from . import disrepo, lua_plugins, registry_state

log = logging.getLogger("discoin.agent_tools")

__all__ = [
    "AgentTools",
    "ChainExecutor",
    "ChainRun",
    "ChainStep",
    "ParamSpec",
    "RiskLevel",
    "TaskQueueWorker",
    "ToolContext",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
    "ToolValidationError",
    "TriggerEngine",
    "cancel_task",
    "complete_with_agent_tools",
    "complete_with_agent_tools_stream",
    "create_trigger",
    "decide_approval",
    "delete_trigger",
    "disrepo",
    "enqueue_task",
    "list_triggers",
    "list_user_tasks",
    "lua_plugins",
    "parse_chain_plan",
    "registry_state",
    "request_approval",
    "run_tool",
    "tool",
    "validate_args",
]


class AgentTools:
    """Lightweight service container. Attach to the bot during setup_hook.

    Provides:
      - registry access (self.registry)
      - chain executor (self.chain)
      - background queue worker (self.queue_worker)
      - event trigger engine (self.triggers)

    ``start()`` loads the built-in tools and spins up the background
    subsystems. ``stop()`` drains them back out on shutdown.
    """

    def __init__(self, bot) -> None:
        self.bot = bot
        self.registry = ToolRegistry
        self.chain = ChainExecutor(bot)
        self.queue_worker = TaskQueueWorker(bot)
        self.triggers = TriggerEngine(bot)

    def start(self) -> None:
        # Import the built-in tools package so every @tool decorator fires.
        from .tools import load_builtin_tools
        load_builtin_tools()
        # Load Lua plugins from plugins/*.lua (optional; errors are non-fatal).
        lua_plugins.load_lua_plugins()
        self.queue_worker.start()
        self.triggers.start()
        log.info(
            "[agent_tools] framework started -- %d tools registered",
            len(ToolRegistry.all()),
        )

    def stop(self) -> None:
        try:
            self.queue_worker.stop()
        except Exception:
            log.exception("[agent_tools] queue worker stop failed")
        try:
            self.triggers.stop()
        except Exception:
            log.exception("[agent_tools] trigger engine stop failed")
