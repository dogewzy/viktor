# Webchat Glossary-First Intent 与审计改造背景

本文记录一次关键设计转向的前因后果。它不是实现方案本身，而是后续实现
`core/intent/` 与 `core/audit/` 时必须保留的上下文。

## 触发事件

2026-05-26，webchat 会话：

- session: `web:e9e1fd85-e739-40d4-8515-31b4a83e09b2`
- project: `order-service`
- topic: `972ff0f1ab2144bf95bc858193a068a1`

用户问题大意：

1. 想看漫剧和真人剧的分布。
2. 想看每天新进量和待审核量。
3. 时间范围为最近 7 天。
4. 租户限定为 `company_id=1587`。
5. 用户补充“漫剧/真人剧是母本的概念”。

后端最终返回：

```text
⚠️ 诊断步数超过上限（max_iterations=30），任务尚未完成。
```

一开始的直觉是提高 `agent.max_iterations`，但复盘后确认这不是根因。

## 排查证据

生产日志显示：

- 第二轮 webchat 从 `2026-05-26 08:57:37` 跑到 `09:07:29`，约 9 分 52 秒。
- 前端 nginx 最终记录 `POST /api/v1/ui/chat/stream 200 31746`，不是 `499/504`。
- Ingress 已配置 `proxy-read-timeout=900`、`proxy-send-timeout=900`、`proxy-buffering=off`。
- 因此这次不是 HTTP 代理超时，而是后端 Agent 自己跑满 `max_iterations=30`。

会话消息轨迹中，第二轮保存了 71 条消息：

- `sse_agent` LLM 调用 30 次。
- `execute_sql` 17 次。
- `describe_table` 6 次。
- `probe_sql` 4 次。
- `code_grep` 3 次。
- `code_read` 2 次。
- 还写并执行了一次临时 repo debug script。

至少 4 次 SQL/工具卡满 60 秒：

- `orders` 最近 7 天按 `is_drama` 聚合超时。
- 同类查询再次超时。
- `COUNT(DISTINCT customer_ref_id)` 超时。
- 按天统计 `orders` 超时。

更重要的是，Agent 在“母本概念”这个语义点尚未解决时，直接围绕 DB 试聚合，
随后又漂移到 `crawler2.short_drama`、代码 grep/read、`vt_takedown.feishu_monitor_data_dt`
和临时脚本。这说明问题不是轮数不足，而是意图识别和工具策略错位。

## 早期误判

复盘过程中出现过一个不完整方向：把这类问题理解为 code-first。

这个方向有一部分合理性：当用户说“概念/口径/如何区分/字段映射”时，
确实不能直接进入大表 SQL，代码经常能提供真实业务语义。

但它仍然不够准确，因为 Viktor 与 Codex 有本质差异：

1. Viktor 显式暴露了 `Glossary`、`KnowledgeNote`、`Skill` 等业务一等概念。
2. Viktor 使用 `Project` 做业务隔离，同一个术语必须在当前 project 语境下解释。

因此 Viktor 不应该照搬 Codex 的 code-first 工作方式。正确抽象应是：

```text
project -> glossary retrieval -> knowledge note retrieval -> intent route -> tool strategy
```

代码搜索只应作为 glossary/knowledge 缺口后的验证工具，而不是默认第一步。

## 根因判断

当前实现的几个问题叠加导致了这次失败：

1. `prompt_builder` 全量注入 glossary，而不是按用户问题检索相关术语。
2. glossary 数量目前不大，但没有 BM25/规则检索，也没有命中分数和 missing terms。
3. clarification gate 主要判断“是否该问用户”，但没有先做 project-scoped 术语检索。
4. 主 Agent prompt 中 DB 查询工作流太强，容易诱导模型先查库。
5. chat history 只保留最终消息和工具结果，缺少完整 prompt、route decision、LLM raw response、provider 可见 reasoning，导致复盘困难。

## 设计结论

后续改造应遵守以下原则：

1. Project 是默认业务边界。glossary、knowledge note、skill 检索都必须先限定 project。
2. 意图识别从 glossary retrieval 开始，而不是从 code grep 或 DB schema 开始。
3. 单 project glossary 规模预计小于 1000 条，适合内存 BM25 + 规则 boost，不需要向量库。
4. exact term、alias、code keyword、description 应有不同权重。
5. 中文业务词需要 2-3 字符 ngram，英文需要 lower、snake_case、camelCase、字段名和状态码 token。
6. 每轮 prompt 只注入 topK glossary/knowledge note，不再全量注入。
7. missing terms 是一等结果：它决定后续是澄清、代码验证，还是禁止直接跑 SQL。
8. SQL 超时后的无工具收口只是保护机制，不是主修复点。主修复点是前置意图识别。

## 审计要求

这次事故暴露出当前审计能力不足。后续必须新增 agent trace 事件流，至少记录：

- glossary retrieval 输入、候选、命中、分数、missing terms。
- knowledge note retrieval 命中。
- intent route 结果。
- 完整 LLM request messages/prompt。
- LLM response、tool_calls、provider 可见 `reasoning_content` 或 reasoning summary。
- tool_start / tool_end，包括输入、输出摘要、耗时、错误。
- clarification decision。
- final answer。

生产默认开启全量 trace，但保留期默认 7 天。所有 payload 写入前必须脱敏：

- API key
- token
- password
- Authorization
- cookie
- private_key
- DB URL

完整 trace 只允许 admin 查看，普通 webchat 只展示简短状态，例如：

- 正在匹配项目业务术语...
- 已识别为业务概念映射 + 数据统计...
- 正在补齐缺失术语的代码证据...

## 后续实现方向

文件结构上不要继续把逻辑挤进 `core/agent_loop.py` 或 `core/prompt_builder.py`。

应新增：

```text
core/intent/
  models.py
  tokenizer.py
  bm25.py
  glossary_retriever.py
  knowledge_retriever.py
  resolver.py
  prompt.py

core/audit/
  models.py
  redaction.py
  recorder.py
  service.py
```

已有文件只做薄集成：

- `core/agent_loop.py` 负责调用 intent resolver、写 trace、串联主流程。
- `core/prompt_builder.py` 负责接受 intent 模块格式化后的检索上下文。
- `core/models.py` 增加 trace event ORM。
- `api/admin_routes.py` 增加 trace 查询 API。
- `config.yaml` / `settings.py` 增加 intent/audit 配置。

这份文档是后续实现和 review 的判断依据：如果实现再次滑向 code-first、DB-first
或把检索/审计逻辑集中塞回主 Agent loop，都应视为偏离本次设计结论。
