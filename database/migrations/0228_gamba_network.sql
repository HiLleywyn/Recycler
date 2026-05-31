-- 0228_gamba_network.sql
--
-- Gamba Network rollout:
--   * 1 network coin (GBC) + 8 game-themed earn-only tokens (GAMBIT, CROWN,
--     VEIN, PIP, EDGE, ACE, NOIR, CHERRY) declared in config.py.
--   * Per-user staking position table for each of the eight game tokens
--     (gamba_stakes). Mirrors discfun_stakes -- lazy accrual, pending GBC
--     yield carried in the row, last_accrue on the DB clock so the position
--     keeps earning across bot restarts.
--   * Per-user PvE/PvP records for chess and checkers (gamba_chess_stats,
--     gamba_checkers_stats) so the leaderboards can rank players by wins,
--     win-rate, and ELO without scanning the match log.
--   * Active match state for chess + checkers (gamba_chess_matches,
--     gamba_checkers_matches). One row per active or recently finished
--     match; the cog reads/updates the position string after each move.
--
-- Lazy accrual mirrors services/safety_module.py + services/discfun.py:
-- every stake / unstake / claim event re-computes elapsed yield since
-- last_accrue and adds it to pending_gbc. No background tick needed.

CREATE TABLE IF NOT EXISTS gamba_stakes (
    user_id           BIGINT          NOT NULL,
    guild_id          BIGINT          NOT NULL,
    symbol            TEXT            NOT NULL,    -- GAMBIT / CROWN / VEIN / PIP / EDGE / ACE / NOIR / CHERRY
    amount            NUMERIC(36, 0)  NOT NULL DEFAULT 0,
    pending_gbc       NUMERIC(36, 0)  NOT NULL DEFAULT 0,    -- accrued + unclaimed
    total_claimed     NUMERIC(36, 0)  NOT NULL DEFAULT 0,    -- lifetime GBC claimed
    auto_compound     BOOLEAN         NOT NULL DEFAULT FALSE,
    total_compounded  NUMERIC(36, 0)  NOT NULL DEFAULT 0,    -- lifetime SYM auto-restaked
    last_accrue       TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    staked_at         TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, guild_id, symbol)
);

CREATE INDEX IF NOT EXISTS idx_gamba_stakes_user
    ON gamba_stakes (guild_id, user_id)
    WHERE amount > 0;

CREATE INDEX IF NOT EXISTS idx_gamba_stakes_symbol
    ON gamba_stakes (guild_id, symbol)
    WHERE amount > 0;


-- Per-user chess record. ELO seeded at 1200 on first match. vs_ai_*
-- columns track AI-bot games so leaderboards can filter "PvP only" if
-- the guild prefers a strict ranking.
CREATE TABLE IF NOT EXISTS gamba_chess_stats (
    user_id           BIGINT          NOT NULL,
    guild_id          BIGINT          NOT NULL,
    wins              INTEGER         NOT NULL DEFAULT 0,
    losses            INTEGER         NOT NULL DEFAULT 0,
    draws             INTEGER         NOT NULL DEFAULT 0,
    vs_ai_wins        INTEGER         NOT NULL DEFAULT 0,
    vs_ai_losses      INTEGER         NOT NULL DEFAULT 0,
    vs_ai_draws       INTEGER         NOT NULL DEFAULT 0,
    elo_rating        INTEGER         NOT NULL DEFAULT 1200,
    total_wagered_raw NUMERIC(36, 0)  NOT NULL DEFAULT 0,
    total_won_raw     NUMERIC(36, 0)  NOT NULL DEFAULT 0,
    last_played       TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, guild_id)
);

CREATE INDEX IF NOT EXISTS idx_gamba_chess_leaderboard
    ON gamba_chess_stats (guild_id, elo_rating DESC, wins DESC);


CREATE TABLE IF NOT EXISTS gamba_checkers_stats (
    user_id           BIGINT          NOT NULL,
    guild_id          BIGINT          NOT NULL,
    wins              INTEGER         NOT NULL DEFAULT 0,
    losses            INTEGER         NOT NULL DEFAULT 0,
    draws             INTEGER         NOT NULL DEFAULT 0,
    vs_ai_wins        INTEGER         NOT NULL DEFAULT 0,
    vs_ai_losses      INTEGER         NOT NULL DEFAULT 0,
    vs_ai_draws       INTEGER         NOT NULL DEFAULT 0,
    elo_rating        INTEGER         NOT NULL DEFAULT 1200,
    total_wagered_raw NUMERIC(36, 0)  NOT NULL DEFAULT 0,
    total_won_raw     NUMERIC(36, 0)  NOT NULL DEFAULT 0,
    last_played       TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    PRIMARY KEY (user_id, guild_id)
);

CREATE INDEX IF NOT EXISTS idx_gamba_checkers_leaderboard
    ON gamba_checkers_stats (guild_id, elo_rating DESC, wins DESC);


-- Chess match state. ``status`` is one of:
--   'active' | 'white_won' | 'black_won' | 'draw' | 'resigned' | 'timeout'
-- ``fen`` carries the python-chess FEN string after the most recent move.
-- ``move_history`` is a JSONB array of UCI strings (e.g. ["e2e4", "e7e5"]).
-- ``ai_side`` is 'white' / 'black' / null when both seats are human.
CREATE TABLE IF NOT EXISTS gamba_chess_matches (
    match_id          BIGSERIAL       PRIMARY KEY,
    guild_id          BIGINT          NOT NULL,
    channel_id        BIGINT          NOT NULL,
    message_id        BIGINT,
    white_user_id     BIGINT          NOT NULL,
    black_user_id     BIGINT,
    ai_side           TEXT,
    bet_token         TEXT            NOT NULL DEFAULT 'USD',
    bet_amount_raw    NUMERIC(36, 0)  NOT NULL DEFAULT 0,
    fen               TEXT            NOT NULL DEFAULT 'rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1',
    move_history      JSONB           NOT NULL DEFAULT '[]'::jsonb,
    status            TEXT            NOT NULL DEFAULT 'active',
    turn_user_id      BIGINT          NOT NULL,
    last_move_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    started_at        TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    ended_at          TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_gamba_chess_active
    ON gamba_chess_matches (guild_id, status)
    WHERE status = 'active';

CREATE INDEX IF NOT EXISTS idx_gamba_chess_player_active
    ON gamba_chess_matches (guild_id, white_user_id, black_user_id)
    WHERE status = 'active';


-- Checkers match state. Board is stored as a 64-char string, one char per
-- square (a1..h8 ordering, file-major):
--   '.'  empty
--   'r'  red man (light) | 'R'  red king
--   'b'  black man       | 'B'  black king
-- ``turn`` is 'r' or 'b'. ``move_history`` is a JSONB array of move
-- notations (e.g. "a3-b4" or "c3xe5xg7" for jumps).
CREATE TABLE IF NOT EXISTS gamba_checkers_matches (
    match_id          BIGSERIAL       PRIMARY KEY,
    guild_id          BIGINT          NOT NULL,
    channel_id        BIGINT          NOT NULL,
    message_id        BIGINT,
    red_user_id       BIGINT          NOT NULL,
    black_user_id     BIGINT,
    ai_side           TEXT,
    bet_token         TEXT            NOT NULL DEFAULT 'USD',
    bet_amount_raw    NUMERIC(36, 0)  NOT NULL DEFAULT 0,
    board             TEXT            NOT NULL,
    turn              TEXT            NOT NULL DEFAULT 'r',
    move_history      JSONB           NOT NULL DEFAULT '[]'::jsonb,
    status            TEXT            NOT NULL DEFAULT 'active',
    turn_user_id      BIGINT          NOT NULL,
    last_move_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    started_at        TIMESTAMPTZ     NOT NULL DEFAULT NOW(),
    ended_at          TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_gamba_checkers_active
    ON gamba_checkers_matches (guild_id, status)
    WHERE status = 'active';

CREATE INDEX IF NOT EXISTS idx_gamba_checkers_player_active
    ON gamba_checkers_matches (guild_id, red_user_id, black_user_id)
    WHERE status = 'active';


-- Gamba Shop consumable inventory. Stackable single-use items priced in
-- GBC. Mirrors validator_guard_inventory / yield_guard_inventory but
-- one table per item-key to avoid one-table-per-item schema sprawl.
-- Items are auto-applied when the player has >=1 in stock and triggers
-- the matching event (win for Lucky Chip / Side Bet Slip, loss for
-- House Marker). Item definitions live in items_config.SHOP_ITEMS.
CREATE TABLE IF NOT EXISTS gamba_consumables (
    user_id    BIGINT      NOT NULL,
    guild_id   BIGINT      NOT NULL,
    item_key   TEXT        NOT NULL,
    count      INTEGER     NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, guild_id, item_key)
);

CREATE INDEX IF NOT EXISTS idx_gamba_consumables_user
    ON gamba_consumables (guild_id, user_id)
    WHERE count > 0;
