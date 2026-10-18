-- The human hunter's level 10 pet chain (RestedXP Forever "6-11 Elwynn Forest"): Taming the Beast 94792 -> 94863 ->
-- 94864 -> Training the Beast 94793. The quests are in tcf_world but their Goldshire NPCs are not (user approved adding
-- them 2026-10-18, like nb/sql_tordrin.sql):
--   Josephine Carson (251507), hunter trainer: copied from the night elf Forever one, Quel'ana Quickgale (252389,
--     trainer 20, gossip 41123); model from Lyria Du Lac (913). The guide's spot (-9472.4, 107.2).
--   Isaac Chan (258930), pet trainer: copied from Quel'dora Quickgale (254411, trainer 273, gossip 4783); model from
--     Brother Wilhelm (927). The guide's spot (-9475.8, 85.0).
-- Spawn rows are copied from Goldshire spawns (map / zone / phase right), z from the NPCs beside them.
CREATE TEMPORARY TABLE t_ct AS SELECT * FROM creature_template WHERE entry=252389;
UPDATE t_ct SET entry=251507, name='Josephine Carson', subname='Hunter Trainer';
INSERT INTO creature_template SELECT * FROM t_ct;
CREATE TEMPORARY TABLE t_ct2 AS SELECT * FROM creature_template WHERE entry=254411;
UPDATE t_ct2 SET entry=258930, name='Isaac Chan', subname='Pet Trainer';
INSERT INTO creature_template SELECT * FROM t_ct2;

CREATE TEMPORARY TABLE t_cd AS SELECT * FROM creature_template_difficulty WHERE Entry=252389;
UPDATE t_cd SET Entry=251507;
INSERT INTO creature_template_difficulty SELECT * FROM t_cd;
CREATE TEMPORARY TABLE t_cd2 AS SELECT * FROM creature_template_difficulty WHERE Entry=254411;
UPDATE t_cd2 SET Entry=258930;
INSERT INTO creature_template_difficulty SELECT * FROM t_cd2;

INSERT INTO creature_template_model (CreatureID, Idx, CreatureDisplayID, DisplayScale, Probability, VerifiedBuild)
  SELECT 251507, Idx, CreatureDisplayID, DisplayScale, Probability, VerifiedBuild FROM creature_template_model WHERE CreatureID=913;
INSERT INTO creature_template_model (CreatureID, Idx, CreatureDisplayID, DisplayScale, Probability, VerifiedBuild)
  SELECT 258930, Idx, CreatureDisplayID, DisplayScale, Probability, VerifiedBuild FROM creature_template_model WHERE CreatureID=927;

INSERT INTO creature_template_gossip SELECT 251507, MenuID, VerifiedBuild FROM creature_template_gossip WHERE CreatureID=252389;
INSERT INTO creature_template_gossip SELECT 258930, MenuID, VerifiedBuild FROM creature_template_gossip WHERE CreatureID=254411;
INSERT INTO creature_trainer SELECT 251507, TrainerID, MenuID, OptionID FROM creature_trainer WHERE CreatureID=252389;
INSERT INTO creature_trainer SELECT 258930, TrainerID, MenuID, OptionID FROM creature_trainer WHERE CreatureID=254411;

CREATE TEMPORARY TABLE t_c AS SELECT * FROM creature WHERE guid=20080333;
UPDATE t_c SET guid=26209146, id=251507, position_x=-9472.4, position_y=107.2, position_z=57.7, orientation=4.7, spawntimesecs=300;
INSERT INTO creature SELECT * FROM t_c;
CREATE TEMPORARY TABLE t_c2 AS SELECT * FROM creature WHERE guid=20080325;
UPDATE t_c2 SET guid=26209147, id=258930, position_x=-9475.8, position_y=85.0, position_z=57.7, orientation=0.0, spawntimesecs=300;
INSERT INTO creature SELECT * FROM t_c2;

INSERT INTO creature_queststarter (id, quest) VALUES (251507, 94792), (251507, 94863), (251507, 94864), (251507, 94793);
INSERT INTO creature_questender (id, quest) VALUES (251507, 94792), (251507, 94863), (251507, 94864), (258930, 94793);
