-- ,start now offers a one-time starter pack on top of the existing
-- game-launcher hub. Track per-user redemption so the pack can only be
-- claimed once, and so the welcome message can flip from "claim your
-- starter pack" to "next steps" once the pack is gone.
--
-- The pack itself is granted in cogs/overview.py::_grant_starter_pack
-- inside an atomic transaction:
--   * starter_pack_claimed_at flipped from NULL -> NOW() (this column)
--   * USD wallet credit
--   * fishing bait inventory bumped
--   * farming seed_packets inventory bumped
--   * one free buddy hatch token (BUDDY_FREE_HATCH_GRANT) is consumed
--     transparently via the existing HATCH_FREE_COUNT path -- no
--     separate token DB column needed.
--
-- claimed_at instead of a bool so a future "starter pack v2" rollout
-- can re-grant by checking this column against a release timestamp.

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS starter_pack_claimed_at TIMESTAMPTZ;
