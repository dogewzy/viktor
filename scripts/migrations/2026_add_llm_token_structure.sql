-- 为 viktor_llm_calls 增加 token 结构字段：缓存命中/未命中、思考(reasoning)、可见输出。
-- 仅需手动执行一次（MySQL，无 IF NOT EXISTS，重复执行会报错）。
ALTER TABLE viktor_llm_calls ADD COLUMN cache_hit_tokens INT NULL;
ALTER TABLE viktor_llm_calls ADD COLUMN cache_miss_tokens INT NULL;
ALTER TABLE viktor_llm_calls ADD COLUMN reasoning_tokens INT NULL;
ALTER TABLE viktor_llm_calls ADD COLUMN output_tokens INT NULL;
