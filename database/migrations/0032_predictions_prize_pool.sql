-- Add prize_pool column to prediction_markets so admins can seed the pool

ALTER TABLE prediction_markets
    ADD COLUMN IF NOT EXISTS prize_pool NUMERIC(20,2) NOT NULL DEFAULT 0;
