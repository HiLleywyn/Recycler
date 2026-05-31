-- Widen any remaining NUMERIC(20,8) columns to NUMERIC(28,8) so that
-- tokens with very large supplies (e.g. SATS max_supply = 2.1e15)
-- can be stored without a numeric field overflow error.
DO $$
DECLARE
    r RECORD;
BEGIN
    FOR r IN
        SELECT table_name, column_name
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND data_type = 'numeric'
          AND numeric_precision = 20
          AND numeric_scale = 8
    LOOP
        EXECUTE format(
            'ALTER TABLE %I ALTER COLUMN %I TYPE NUMERIC(28,8)',
            r.table_name, r.column_name
        );
    END LOOP;
END
$$;
