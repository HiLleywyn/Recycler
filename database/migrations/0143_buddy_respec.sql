-- Buddy stat respec: per-buddy counter that tracks how many times the
-- player has paid USD to refund all spent stat points (hp_alloc /
-- atk_alloc / spd_alloc) back to "available". Price doubles each
-- respec on the same buddy:
--
--     respec #n -> RESPEC_BASE_PRICE_USD * 2 ** (n - 1)
--
-- Mirrors the swap_count column added in 0143_swap (kept as siblings
-- so the buddy panel can render both costs side by side without
-- branching schema reads).

ALTER TABLE cc_buddies
    ADD COLUMN IF NOT EXISTS respec_count BIGINT NOT NULL DEFAULT 0;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'cc_buddies_respec_nonneg_chk'
    ) THEN
        ALTER TABLE cc_buddies
            ADD CONSTRAINT cc_buddies_respec_nonneg_chk
            CHECK (respec_count >= 0) NOT VALID;
        ALTER TABLE cc_buddies VALIDATE CONSTRAINT cc_buddies_respec_nonneg_chk;
    END IF;
END$$;
