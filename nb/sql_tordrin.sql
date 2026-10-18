-- Tordrin Sternblade (248415), the RestedXP Forever guide's Northshire hunter trainer, absent from this server.
-- Template / trainer list copied from Einris Brightspear (5515, trainer 1005515), model from Llane Beshere (911).
CREATE TEMPORARY TABLE t_ct AS SELECT * FROM creature_template WHERE entry=5515;
UPDATE t_ct SET entry=248415, name='Tordrin Sternblade';
INSERT INTO creature_template SELECT * FROM t_ct;
CREATE TEMPORARY TABLE t_cd AS SELECT * FROM creature_template_difficulty WHERE Entry=5515;
UPDATE t_cd SET Entry=248415;
INSERT INTO creature_template_difficulty SELECT * FROM t_cd;
INSERT INTO creature_template_model (CreatureID, Idx, CreatureDisplayID, DisplayScale, Probability, VerifiedBuild)
  SELECT 248415, Idx, CreatureDisplayID, DisplayScale, Probability, VerifiedBuild FROM creature_template_model WHERE CreatureID=911;
INSERT INTO creature_template_gossip SELECT 248415, MenuID, VerifiedBuild FROM creature_template_gossip WHERE CreatureID=5515;
INSERT INTO creature_trainer SELECT 248415, TrainerID, MenuID, OptionID FROM creature_trainer WHERE CreatureID=5515;
CREATE TEMPORARY TABLE t_c AS SELECT * FROM creature WHERE guid=20037608;
UPDATE t_c SET guid=26209145, id=248415, position_x=-8884.2, position_y=-242.1, position_z=80.9, orientation=1.6, spawntimesecs=300;
INSERT INTO creature SELECT * FROM t_c;
INSERT INTO creature_questender (id, quest) VALUES (248415, 92479), (248415, 91758);
INSERT INTO creature_queststarter (id, quest) VALUES (248415, 91772);
