-- 0188_buddy_craft_cooldown.sql
--
-- Per-buddy cooldown stamp for ``,craft apply <food>`` -- player feedback
-- was that crafted food (Buddy Treat / Toy / Tonic / Training Brew /
-- Buddy Feast / Harvest Pie / etc.) granted too much XP and could be
-- spammed back-to-back, trivialising buddy levelling.
--
-- ``services/crafting.py:_apply_buddy_effect`` reads this column on
-- every buddy/<effect> apply, refuses if the elapsed time is below
-- the cooldown threshold, and stamps it again on success. Comparison
-- runs DB-side via ``EXTRACT(EPOCH FROM (NOW() - col))`` so container-
-- vs-DB clock skew doesn't affect the gate.
--
-- Idempotent.

ALTER TABLE cc_buddies
    ADD COLUMN IF NOT EXISTS last_buddy_craft_apply_at TIMESTAMPTZ;
