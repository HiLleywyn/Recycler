-- 0051: Rename staked_sun -> staked_amount on all stone tables.
-- "staked_sun" was a legacy name from when the shop used SUN tokens.
-- The column stores the stablecoin amount staked in the stone.
-- Guarded: entrypoint hotfix may have already renamed the column.
DO $$
BEGIN
    IF EXISTS (SELECT FROM information_schema.columns WHERE table_name='hashstones'  AND column_name='staked_sun') THEN ALTER TABLE hashstones  RENAME COLUMN staked_sun TO staked_amount; END IF;
    IF EXISTS (SELECT FROM information_schema.columns WHERE table_name='lockstones'  AND column_name='staked_sun') THEN ALTER TABLE lockstones  RENAME COLUMN staked_sun TO staked_amount; END IF;
    IF EXISTS (SELECT FROM information_schema.columns WHERE table_name='vaultstones' AND column_name='staked_sun') THEN ALTER TABLE vaultstones RENAME COLUMN staked_sun TO staked_amount; END IF;
    IF EXISTS (SELECT FROM information_schema.columns WHERE table_name='liqstones'   AND column_name='staked_sun') THEN ALTER TABLE liqstones   RENAME COLUMN staked_sun TO staked_amount; END IF;
    IF EXISTS (SELECT FROM information_schema.columns WHERE table_name='gambastones' AND column_name='staked_sun') THEN ALTER TABLE gambastones RENAME COLUMN staked_sun TO staked_amount; END IF;
END $$;
