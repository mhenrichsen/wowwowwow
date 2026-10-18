-- classic exploration quests (Flags 0x4 + areatrigger_involvedrelation) with no QUEST_OBJECTIVE_AREATRIGGER (10)
-- objective: the server completed them at accept, the client never showed them ready (62 The Fargodeep Mine
-- could not be turned in, Hunt 2026-10-18). ObjectID -1 = any of the quest's triggers (MiscHandler.cpp:563).
DELETE FROM quest_objectives WHERE ID BETWEEN 90097001 AND 90097099;
INSERT INTO quest_objectives (ID, QuestID, Type, `Order`, StorageIndex, ObjectID, Amount, ConditionalAmount, Flags, Flags2, ProgressBarWeight, ParentObjectiveID, Visible, Description, VerifiedBuild) VALUES
(90097001, 62, 10, 0, 0, -1, 0, 0, 0, 0, 0, 0, 1, '', 0),
(90097002, 76, 10, 0, 0, -1, 0, 0, 0, 0, 0, 0, 1, '', 0),
(90097003, 201, 10, 0, 0, -1, 0, 0, 0, 0, 0, 0, 1, '', 0),
(90097004, 273, 10, 0, 0, -1, 0, 0, 0, 0, 0, 0, 1, '', 0),
(90097005, 578, 10, 0, 0, -1, 0, 0, 0, 0, 0, 0, 1, '', 0),
(90097006, 870, 10, 0, 0, -1, 0, 0, 0, 0, 0, 0, 1, '', 0),
(90097007, 944, 10, 0, 0, -1, 0, 0, 0, 0, 0, 0, 1, '', 0),
(90097008, 984, 10, 0, 0, -1, 0, 0, 0, 0, 0, 0, 1, '', 0),
(90097009, 1149, 10, 0, 0, -1, 0, 0, 0, 0, 0, 0, 1, '', 0),
(90097010, 1265, 10, 0, 0, -1, 0, 0, 0, 0, 0, 0, 1, '', 0),
(90097011, 1448, 10, 0, 0, -1, 0, 0, 0, 0, 0, 0, 1, '', 0),
(90097012, 1658, 10, 0, 0, -1, 0, 0, 0, 0, 0, 0, 1, '', 0),
(90097013, 1699, 10, 0, 0, -1, 0, 0, 0, 0, 0, 0, 1, '', 0),
(90097014, 2240, 10, 0, 0, -1, 0, 0, 0, 0, 0, 0, 1, '', 0),
(90097015, 2989, 10, 0, 0, -1, 0, 0, 0, 0, 0, 0, 1, '', 0),
(90097016, 4811, 10, 0, 0, -1, 0, 0, 0, 0, 0, 0, 1, '', 0),
(90097017, 4842, 10, 0, 0, -1, 0, 0, 0, 0, 0, 0, 1, '', 0),
(90097018, 6025, 10, 0, 0, -1, 0, 0, 0, 0, 0, 0, 1, '', 0),
(90097019, 8286, 10, 0, 0, -1, 0, 0, 0, 0, 0, 0, 1, '', 0);
