--[[
  plugins/_example.lua -- template Lua plugin for Discoin AI agent tools.

  This file is prefixed with _ so it is SKIPPED by the plugin loader.
  Copy it without the underscore to activate it:
      cp plugins/_example.lua plugins/mytools.lua

  ============================================================
  AGENT TOOLS  (tools the AI can call via function-calling)
  ============================================================

  tool_api.register(config_table)
      Register a new agent tool. Keys:
          name        (string, required)  Dot-namespaced id, e.g. "greet.hello"
          summary     (string)            One-line description shown to the AI
          risk        (string)            "read" | "safe" | "mutate"  (default "read")
          category    (string)            Grouping label (default "lua_plugin")
          cooldown_s  (number)            Per-user cooldown in seconds (default 0)
          params      (table)             Array of parameter tables (see below)
          handler     (function)          function(args) -> tool_api.ok()/fail()

  Parameter table keys:
      name        (string, required)
      type        (string)  "str" | "int" | "float" | "bool" | "symbol" | "uid"
      required    (boolean, default true)
      default     (any)
      description (string)
      choices     (table)   Array of allowed values
      min / max   (number)  Numeric bounds

  Return values from handler:
      tool_api.ok({ key = value, ... })   -- success, data table sent to AI
      tool_api.fail("reason string")      -- failure, error forwarded to AI

  ============================================================
  REAL-TIME CHAT HOOKS
  ============================================================

  Hooks intercept the AI chat pipeline. Each runs in order. Return the
  (optionally modified) string, or nil to pass the value through unchanged.

  Context fields (ctx table):
      ctx.guild_id, ctx.user_id, ctx.display_name, ctx.channel_id

  tool_api.on_system_prompt(fn)
      Called after the system prompt is assembled, before the AI call.
      fn(prompt, ctx) -> string | nil

  tool_api.on_user_message(fn)
      Called after user input is sanitized, before the AI call.
      fn(msg, ctx) -> string | nil

  tool_api.on_ai_reply(fn)
      Called after the AI produces its reply, before it is sent to Discord.
      fn(reply, ctx) -> string | nil

  ============================================================
  SANDBOX NOTES
  ============================================================
  - No file I/O, network, or OS access (io/os/package/require are removed).
  - Use tool_api.log("msg") for debug output (goes to the bot logger).
  - math, string, table, pcall, error, assert, tostring, tonumber are safe.
--]]


-- ── Example agent tool: greet.hello ──────────────────────────────────────────
tool_api.register({
    name    = "greet.hello",
    summary = "Return a greeting for a given name. Demo tool -- remove or replace.",
    risk    = "read",
    params  = {
        {
            name        = "name",
            type        = "str",
            required    = false,
            default     = "world",
            description = "Name to greet.",
        },
    },
    handler = function(args)
        local who = args.name or "world"
        if type(who) ~= "string" or #who == 0 then
            return tool_api.fail("name must be a non-empty string")
        end
        if #who > 64 then
            return tool_api.fail("name too long (max 64 chars)")
        end
        tool_api.log("greet.hello called for: " .. who)
        return tool_api.ok({ greeting = "Hello, " .. who .. "!" })
    end,
})


-- ── Example chat hook: append a suffix to every AI reply ─────────────────────
--
-- Uncomment to activate:
--
-- tool_api.on_ai_reply(function(reply, ctx)
--     return reply .. " ~lua"
-- end)


-- ── Example chat hook: inject extra instructions into the system prompt ───────
--
-- tool_api.on_system_prompt(function(prompt, ctx)
--     return prompt .. "\n\nAlways end your response with a fun emoji."
-- end)


-- ── Example chat hook: prepend the user's display name to their message ───────
--
-- tool_api.on_user_message(function(msg, ctx)
--     return "[" .. (ctx.display_name or "user") .. "] " .. msg
-- end)
