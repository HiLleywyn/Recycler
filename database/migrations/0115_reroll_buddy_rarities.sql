-- Decouple rarity from species: reroll every existing buddy's rarity_tier
-- exactly once so the new "rarity is independent of species" rule applies
-- retroactively. Before this change, rarity was baked into species (e.g.
-- zenny always Common, nimbus always Legendary). From now on rarity is
-- rolled at hatch / reroll time and stored on the row, so a one-off
-- rescramble is needed to make the transition fair for current owners.
--
-- Weights match buddies_config.RARITY_ROLL_WEIGHTS at the time this
-- migration was written:
--     Common    58
--     Uncommon  18
--     Rare      11
--     Epic       9
--     Legendary  4
-- Total 100, so `random() * 100` gives a clean sample space.
--
-- Touches every row (owned, shelter, whatever): the buddy still exists and
-- its rarity is a permanent attribute, so the shelter residents need the
-- fresh roll too.  Safe to re-run: idempotent because a second execution
-- just draws another random number and writes it over the previous one.
-- If you really need to pin current values, skip this migration.

UPDATE cc_buddies
SET rarity_tier = CASE
        WHEN r <  58 THEN 1   -- Common
        WHEN r <  76 THEN 2   -- Uncommon
        WHEN r <  87 THEN 3   -- Rare
        WHEN r <  96 THEN 4   -- Epic
        ELSE              5   -- Legendary
    END
FROM (
    SELECT id, random() * 100.0 AS r
    FROM   cc_buddies
) AS roll
WHERE cc_buddies.id = roll.id;
