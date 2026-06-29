-- 为 viktor_llm_calls 增加使用场景维度（coding / webchat / dingtalk / system）
-- 用于看板按使用场景拆分 token 用量，而非混在一个总数里。
ALTER TABLE viktor_llm_calls ADD COLUMN scene VARCHAR(32) NOT NULL DEFAULT 'system';
ALTER TABLE viktor_llm_calls ADD INDEX ix_llm_calls_scene (scene);
