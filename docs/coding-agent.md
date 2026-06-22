# Coding Agent 专项设计

本文描述 Viktor 从“诊断与业务答疑 Agent”扩展到“无人值守 MR 生产系统”的专项设计。目标不是做一个交互式本地 IDE 助手，而是在已注册项目拥有充分上下文后，让 Viktor 能接收开发需求、后台完成分析和代码修改、创建 Merge Request，并产出可审计的改动报告。

## 1. 目标定位

Viktor Coding Agent 的产品形态：

```text
用户提交开发需求
  -> Viktor 创建 coding task
  -> 后台分析需求、准备工作区、修改代码、运行校验
  -> 创建 Merge Request 和改动报告
  -> 人进入 MR review，决定合并、要求修改或重做
```

与 Codex / Claude Code 这类交互式 coding assistant 的区别：

- 交互式助手默认有人在电脑前观察、批准、打断。
- Viktor 默认是后台无人值守任务，在 MR 前不要求人工介入。
- Viktor 仍然提供 task 观察窗口，用户可以通过 task id 查看进度、打断、暂停、补充指令或取消。
- Viktor 可以拥有完成任务所需的 full access，但 agent 实际行动必须经过 task policy 约束，走高安全默认路径。

一句话原则：

> 无人值守是默认模式，可观测和可中断是安全阀，MR review 是正式人工验收点。

## 2. 能力边界

Viktor 负责总编排：

- 接收需求并创建 coding task。
- 加载项目注册上下文、知识库、术语表、Repository Connector、Runtime Context、历史问答等信息。
- 生成开发计划和风险判断。
- 准备隔离的可写代码工作区。
- 控制 coding tools 的权限、路径、命令和超时。
- 记录完整事件流和工具轨迹。
- 运行测试、lint、typecheck、安全检查和自审。
- push 分支、创建 MR、生成改动报告。
- 支持 review 后重做、追加修改或继续同一 MR。

Coding tools 负责具体工程动作：

- 搜索代码。
- 阅读文件。
- 修改文件。
- 运行命令。
- 查看 diff。
- 根据测试反馈继续修复。

这里的“tool”不是简单的 shell 命令包装。它必须是适合 LLM 后台使用的工程操作接口：结构化、可失败、可审计、可回滚、可被 policy 约束。

## 3. 核心状态机

每个 coding task 有独立 task id 和状态机。

```text
created
  -> planning
  -> waiting_plan_review
  -> plan_approved
  -> preparing_workspace
  -> exploring_code
  -> editing
  -> running_checks
  -> self_review
  -> preparing_mr / waiting_code_review
  -> completed

planning 内部包含：
  -> loading_context
  -> exploring_code       # 只读代码探索，产出 code_exploration artifact
  -> drafting_plan        # 基于已核对代码生成正式 Plan

任意阶段可进入：
  -> paused
  -> cancelled
  -> failed
  -> needs_attention
```

建议拆分 task 和 attempt：

```text
coding_task
  attempt 1 -> 生成 MR !123，review 后要求修改
  attempt 2 -> 继续 push 到同一 MR 或创建新 MR
  attempt 3 -> 完成
```

每次 attempt 都要保存：

- 输入需求和补充指令。
- 项目上下文快照。
- Plan 前置代码探索结果。
- 已审核 Plan。
- 工具调用日志。
- 修改文件列表。
- diff 摘要。
- 测试和检查结果。
- MR 链接。
- 失败原因或人工反馈。

## 4. 观察窗口

虽然默认后台执行，但用户必须能通过 task id 清晰看到 Viktor 正在做什么。

建议 API：

```text
POST /api/v1/coding/tasks
GET  /api/v1/coding/tasks
GET  /api/v1/coding/tasks/{task_id}
GET  /api/v1/coding/tasks/{task_id}/events
GET  /api/v1/coding/tasks/{task_id}/diff
POST /api/v1/coding/tasks/{task_id}/pause
POST /api/v1/coding/tasks/{task_id}/resume
POST /api/v1/coding/tasks/{task_id}/cancel
POST /api/v1/coding/tasks/{task_id}/interrupt
POST /api/v1/coding/tasks/{task_id}/message
```

`events` 建议支持 SSE。事件例子：

```text
stage_changed: planning -> exploring_code
code_exploration_completed: relevant_files=["cronjobs/sync_tracking_status.py"]
plan_generated: waiting_plan_review
plan_revision_requested: "旧业务知识已移除，请重新定位"
plan_approved: waiting for execution
tool_call_started: grep pattern="OrderStatus"
tool_call_finished: read_file path="src/order/service.ts"
command_started: npm test -- order
command_finished: exit_code=0 duration_ms=18320
policy_blocked: denied_path=".env"
diff_updated: changed_files=["src/order/service.ts", "tests/order.test.ts"]
```

前端观察窗口至少展示：

- 当前阶段和当前动作。
- 当前计划。
- 最近事件。
- 已读关键文件。
- 已修改文件。
- 当前 diff 摘要。
- 正在执行或最近执行的命令。
- 测试结果。
- policy 拦截记录。
- MR 链接和报告链接。

## 5. 打断与恢复

Viktor 支持用户在 task 运行中介入，但不依赖用户介入才能完成。

中断类型：

- `pause`：在安全点暂停，保留 workspace 和当前 diff。
- `resume`：从暂停点继续。
- `cancel`：终止任务，保留日志和当前产物，不创建 MR。
- `interrupt/message`：追加新指令，让 agent 在当前或下一个安全点重规划。

安全点包括：

- 一轮 LLM tool call 完成后。
- 单次 patch 成功或失败后。
- 命令执行结束后。
- 测试阶段结束后。
- commit 前。
- push/MR 前。

如果必须强制终止正在运行的命令，需要记录为 `interrupted_during_command`，并让下一轮恢复逻辑明确处理半完成状态。

## 6. Coding Tools 设计

第一版建议内置以下工具。

只读工具：

- `list_files(pattern, max_results)`
- `grep(pattern, path, max_results)`
- `read_file(path, start_line, end_line)`
- `git_status()`
- `git_diff(path=None)`

可写工具：

- `apply_patch(patch)`：首选编辑工具，要求上下文匹配，失败则不落地。
- `write_file(path, content)`：用于新文件或小型整体生成，默认限制大小。
- `multi_edit(path, edits)`：单文件多处原子修改。
- `delete_file(path)`：默认高风险，需要 policy 允许。

执行工具：

- `check_syntax(path, language=auto, timeout_sec)`：固定检查单个 Python / Java / JavaScript 文件语法或编译错误，不经过自由 shell。Python 使用标准库；Java / JavaScript 依赖环境中存在 `javac` / `node`。
- `run_command(command, cwd, timeout_sec)`：只允许 policy 里的命令模板或安全命令。
- `run_tests(selector=None)`
- `run_lint()`
- `run_typecheck()`
- `run_build()`

Git / MR 工具建议由编排层调用，不直接暴露给 LLM 自由调用：

- `create_branch`
- `commit_changes`
- `push_branch`
- `create_merge_request`
- `query_pipeline`

判断工具好坏的标准：

- 是否限制在 workspace 内，拒绝路径穿越。
- 是否能按 policy 限制读写路径、命令、网络和耗时。
- 是否有明确失败信号，避免悄悄改错。
- 是否产生清晰 diff。
- 是否完整记录输入、输出、耗时、退出码和错误。
- 是否支持超长输出截断。
- 是否支持 checkpoint 和恢复。
- 是否方便 verifier 独立复查。

## 7. Task Policy

后台无人值守不能依赖过程中反复问人，权限必须在任务启动前前置为 policy。

示例：

```json
{
  "write_paths": ["src/**", "tests/**"],
  "deny_paths": [".env*", "config/prod/**", "secrets/**"],
  "allowed_commands": ["npm test", "npm run lint", "npm run typecheck"],
  "allow_dependency_change": false,
  "allow_schema_change": false,
  "allow_ci_change": false,
  "allow_delete_files": false,
  "allow_push_branch": true,
  "allow_create_mr": true,
  "max_runtime_minutes": 60,
  "max_changed_files": 30,
  "max_diff_lines": 3000
}
```

默认高安全路径：

- 允许修改业务代码和测试。
- 允许运行项目注册的测试/lint/build 命令。
- 允许 push feature branch 和创建 MR。
- 默认禁止改 `.env`、生产配置、密钥文件、CI/CD、数据库迁移、默认分支。
- 默认禁止 merge MR、force push、删除大量文件。
- 默认禁止访问与 task 无关的仓库和路径。

## 8. 自动校验

创建 MR 前必须运行 verifier。建议检查项：

- `git diff` 非空且符合任务目标。
- 修改文件在 policy 允许范围内。
- 没有提交 secrets、token、password、私钥。
- 没有大文件或二进制误提交。
- 没有越权修改 CI/CD、生产配置、数据库迁移。
- lint/typecheck/test/build 按项目配置执行。
- 依赖文件变更需要显式说明。
- 删除文件、新增外部依赖、schema 变更需要标为高风险。
- reviewer agent 做一次自审，输出风险和重点 review 文件。

检查失败时：

- 可修复失败交给 agent 继续修。
- policy 失败直接进入 `needs_attention` 或 `failed`。
- 测试环境不可用要如实写入报告，不允许伪造通过。

## 9. MR 与改动报告

MR 是第一个正式人工验收点。MR body 和报告必须足够完整。

报告内容：

- 需求原文。
- Viktor 对需求的理解。
- 实现方案摘要。
- 修改文件列表。
- 关键 diff 摘要。
- 执行过的命令和结果。
- 未运行或失败的检查。
- 风险点和人工 review 建议。
- 是否新增依赖、配置、迁移或行为兼容性变化。
- MR 链接。

报告应写入现有 report 系统，钉钉或前端只展示摘要和链接。

## 10. 数据模型草案

建议新增表：

- `viktor_coding_tasks`
- `viktor_coding_attempts`
- `viktor_coding_events`
- `viktor_coding_artifacts`

`viktor_coding_tasks` 核心字段：

- `task_id`
- `project_id`
- `requirement`
- `status`
- `stage`
- `target_branch`
- `mr_url`
- `report_id`
- `policy`
- `created_by`
- `created_at`
- `updated_at`

`viktor_coding_attempts` 核心字段：

- `attempt_id`
- `task_id`
- `repo_connector_id`
- `workspace_path`
- `branch_name`
- `base_commit`
- `head_commit`
- `status`
- `plan`
- `summary`
- `test_results`
- `risk_flags`

`viktor_coding_events` 核心字段：

- `id`
- `task_id`
- `attempt_id`
- `seq`
- `event_type`
- `stage`
- `message`
- `payload`
- `created_at`

`viktor_coding_artifacts` 核心字段：

- `artifact_id`
- `task_id`
- `attempt_id`
- `artifact_type`
- `title`
- `content`
- `payload`

## 11. 后端模块草案

建议新增：

- `api/coding_routes.py`
- `core/coding_service.py`
- `core/coding_agent_loop.py`
- `core/coding_runtime.py`
- `core/coding_tools.py`
- `core/coding_workspace.py`
- `core/coding_policy.py`
- `core/coding_verifier.py`
- `core/coding_report.py`
- `gitlab/merge_request_service.py`

职责划分：

- `coding_service`：任务生命周期、状态机、attempt 管理。
- `coding_workspace`：可写 clone、branch、commit、cleanup。
- `coding_runtime`：工具执行环境、路径限制、命令限制、日志。
- `coding_agent_loop`：LLM 多轮 tool calling。
- `coding_policy`：权限和安全策略判断。
- `coding_verifier`：MR 前检查。
- `coding_report`：报告生成。
- `merge_request_service`：GitLab push、MR、pipeline 查询。

## 12. 与现有能力的关系

当前已有能力可复用：

- 项目注册和 Repository Connector。
- onboarding 生成的项目上下文、术语和知识笔记。
- 只读代码探索思路和 explorer prompt。
- GitLab API 客户端的 base URL / project path 解析。
- report 存储和 HTML 展示。
- 前端项目详情页的二级导航结构。

需要注意：

- 现有 `code_sync.ensure_workspace` 是只读代码自省语义，不应直接复用为可写 coding workspace。
- 现有 `agent_loop` 主要服务诊断问答，不应直接混入高风险写文件工具。
- Coding Agent 应该是独立 task pipeline，但可以复用项目上下文构建能力。

## 13. MVP 路线

第一阶段：任务和观察窗口

- 新增 coding task 表。
- 新增创建/查询/事件 API。
- 前端增加 Coding Tasks tab。
- 支持 task 状态机和 SSE 事件。

第二阶段：可写 workspace 和内置 tools

- 支持 clone 目标仓库到 task workspace。
- 创建 feature branch。
- 实现 `read_file` / `grep` / `apply_patch` / `check_syntax` / `run_command` / `git_diff`。
- 实现 policy 拦截和事件日志。

第三阶段：内置 coding agent loop

- LLM 根据需求和上下文调用 coding tools。
- 支持测试失败后的修复循环。
- 支持 pause / cancel / interrupt。

第四阶段：MR 闭环

- GitLab push branch。
- 创建 MR。
- 生成改动报告。
- 查询 pipeline 状态。

第五阶段：review 后重做

- 支持基于人工反馈创建新 attempt。
- 支持继续同一 MR 或新 MR。
- 保存 attempt 间的差异和决策记录。

## 14. 开放问题

- 每个项目的测试命令从哪里配置：项目注册、Repository Connector，还是自动识别后人工确认？
- 后台任务队列选型：线程池、RQ、Celery、Arq、Temporal？
- task workspace 的保留周期和磁盘清理策略。
- 多仓库需求如何拆分 MR：单 task 多 MR，还是一个主 MR 加关联 MR？
- 是否需要在 MR 前强制 reviewer agent 给出 review 结论？
- policy 默认值是否按项目类型区分，例如前端、后端、数据任务、基础设施仓库？
- 用户中断时是否允许直接编辑当前计划，还是只能追加自然语言指令？
