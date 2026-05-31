-- 0086: agent_task_queue.approval_id
--
-- Closes a privilege-escalation gap in the agent task queue. Previously the
-- queue worker set ctx.approved=True unconditionally on every drained task,
-- which meant any DANGER-risk tool (economy.mint, economy.burn, ...) would
-- bypass run_tool()'s approval gate as soon as it was enqueued.
--
-- Tasks now optionally reference an agent_approvals row. The queue worker
-- only marks the task as approved if that row is in status='approved' and
-- not expired. Without an approval_id, queued execution always runs through
-- run_tool() with approved=False, so DANGER tools cannot bypass the gate
-- just by being enqueued.

ALTER TABLE agent_task_queue
    ADD COLUMN IF NOT EXISTS approval_id BIGINT;
