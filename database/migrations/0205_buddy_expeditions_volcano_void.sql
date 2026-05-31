-- Widen buddy_expeditions destination CHECK to include 'volcano' and
-- 'void'. The two zones were added to expeditions_config.DESTINATIONS
-- in the May-3 expansion but the table-level constraint still only
-- allowed the original four (forest / reef / mine / ruins), so any
-- ,expedition send for a Volcano or Void run failed with a
-- CheckViolationError on insert.

ALTER TABLE buddy_expeditions
    DROP CONSTRAINT IF EXISTS buddy_expeditions_destination_chk;

ALTER TABLE buddy_expeditions
    ADD CONSTRAINT buddy_expeditions_destination_chk CHECK (
        destination IN ('forest', 'reef', 'mine', 'ruins', 'volcano', 'void')
    );
