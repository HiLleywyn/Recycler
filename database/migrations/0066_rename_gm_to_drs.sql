-- Rename the gm_commands beta feature key to drs_commands to match the .drs command rename.
UPDATE beta_features SET feature_name = 'drs_commands' WHERE feature_name = 'gm_commands';
