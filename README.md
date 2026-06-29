# Viktor

语言：中文 | [English](README.en.md)

Viktor 是面向小型研发团队的 Agent Harness / Agent Ops 平台。

它帮助团队把 AI Agent 接入真实项目、生产证据、研发流程和人工审批 gate。Viktor 不是另一个聊天框，也不是单点 coding assistant；它更像团队共享的 Agent 后端层，负责项目上下文、工具访问、工作流状态、审计记录、Review gate 和长期知识沉淀。

本仓库包含 Viktor 后端和 Agent 编排核心。Web 控制台由独立前端项目维护。

## 演示

[![Coding full flow demo](docs/assets/coding-full-flow-preview.gif)](docs/assets/coding-full-flow.mp4)

这个演示展示了一个 Coding Task 从项目上下文加载、代码探索、Plan/Review 状态、执行产物、变更文件、事件历史，到最终 Review gate 的完整链路。点击动图可打开 MP4。

## Viktor 能做什么

Viktor 以 `Project` 为核心组织 Agent 工作。每个项目可以注册：

- 源代码仓库和仓库元信息
- 数据库、日志、运行时上下文和外部服务
- 业务上下文、术语、知识笔记和可复用 Skill
- Watchdog、需求接入配置和 Coding Task 策略

注册这些资产之后，Viktor 可以支持几类相互打通的工作流：

| 能力域 | 当前能力 |
| --- | --- |
| 对话诊断 | 通过钉钉或 Web 控制台进行项目感知的 Agent 问答，并保留工具调用和 trace。 |
| 项目接入 | 分析 GitLab 仓库，生成上下文、术语、知识、连接器候选产物，由人工审核后落地。 |
| 证据工具 | 只读 SQL、schema 探索、日志、Kubernetes 状态/日志、代码搜索/读取/探索、Redis、对象存储、队列、向量库、HTTP 服务和文档。 |
| 需求接入 | 将 GitLab issue、本地 Agent 提交或 Web 录入的需求/Bug 路由为研发工作。 |
| Coding Agent | 后台 Coding Task，支持澄清、Plan 审核、隔离 workspace 执行、diff/report、分支/MR 和 Review 后续修复。 |
| Watchdog | 定时探针、异常分析、通知人工，并可选生成 Coding 工作。 |
| Trace Learning | 将已完成 trace 转成可人工审核的长期知识候选。 |

## 架构

```text
钉钉 / Web 控制台 / GitLab Issue / Watchdog / Coding Task
        |
        v
FastAPI API 层
        |
        v
项目注册中心 Registry + MySQL 持久化
        |
        v
Agent 编排层
  - 诊断 Agent
  - 项目接入分析器
  - Issue Router
  - Coding Agent
  - Watchdog Agent
  - Trace Learning
        |
        v
工作流与执行层
  - 可选 Temporal workflow
  - Kubernetes Coding Job
  - GitLab MR / webhook / polling 集成
        |
        v
证据工具层
  DB / Code / Logs / K8s / Redis / OSS / Queue / Vector / HTTP / Docs
```

Viktor 的核心原则是：长期项目知识放在 registry 里，实时事实通过工具获取，高风险改动必须经过明确工作流状态和人工 gate。

## 核心工作流

### 项目接入

```text
提交项目和仓库
  -> 分析仓库文件树、文档、代码和 API 入口
  -> 生成候选产物
  -> 人工审核、编辑、接受或拒绝
  -> 接受的产物写入项目注册中心
```

候选产物可以包括项目上下文、目录摘要、API 契约、术语、知识笔记、数据库/日志/外部连接器和运行时上下文。

### Coding Task

```text
创建 Coding Task
  -> 加载项目上下文和仓库连接器
  -> Plan 前只读探索代码
  -> 必要时向用户澄清
  -> 生成 Plan 并等待审核
  -> 在隔离 workspace 中执行
  -> 运行策略允许的检查命令
  -> 产出 diff、报告、分支和可选 MR
  -> 等待人工或自动 Review 后续处理
```

Coding 执行受策略约束。任务策略可以限制可写路径、拒绝 secrets 或生产配置改动、限制可执行命令，并控制是否允许创建分支或 Merge Request。

### 需求接入

```text
需求或 Bug 进入 Viktor
  -> 创建或扫描 GitLab issue
  -> 路由到一个或多个仓库连接器
  -> 可选生成多仓改动蓝图
  -> 创建 Coding Task
  -> 聚合 MR 状态并通知维护人
  -> 所有工作合并后关闭 issue
```

这条链路把“有人提了一个 Bug/需求”变成可追踪的研发闭环：项目路由、Coding Task、MR 和通知都留在同一条记录里。

## 快速开始

依赖：

- Python 3.12+
- MySQL 8 兼容的元数据库
- 可选：Kubernetes、GitLab、钉钉、Temporal、对象存储和日志服务集成

本地安装和启动：

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 从公开模板创建本地环境变量文件，然后填入必要密钥。
cp .env.example .env

python main.py
```

启用本地 demo 数据：

```bash
VIKTOR_AUTH_SECRET=change-me-secret \
VIKTOR_ENABLE_DEMO_RESET=1 \
VIKTOR_DEMO_RESET_TOKEN=change-me \
REPO_WARMUP_ENABLED=false \
WATCHDOG_ENABLED=false \
python main.py
```

然后调用：

```bash
curl -X POST http://127.0.0.1:8080/api/v1/demo/reset \
  -H "Content-Type: application/json" \
  -H "X-Viktor-Demo-Token: change-me" \
  -d '{"scene":"coding-full-flow"}'
```

## 配置

大部分集成都可以按需启用。最小本地配置需要：

- `VIKTOR_DB_*`：Viktor 元数据库
- `VIKTOR_AUTH_SECRET`：Web 鉴权 token secret
- 至少一个 LLM provider key：用于 Agent 工作流

常用可选集成：

- `GITLAB_BASE_URL`、`GITLAB_PRIVATE_TOKEN`、`GITLAB_WEBHOOK_SECRET`
- `K8S_NAMESPACE`、`K8S_CONTEXT`、`K8S_API_SERVER`、`K8S_TOKEN`、`K8S_CA_DATA`
- 钉钉应用凭证
- Aliyun SLS / 对象存储凭证
- Temporal endpoint 和 namespace
- 本地访问私有数据库时使用的 SSH tunnel 默认值

密钥应来自环境变量或部署密钥系统。不要提交 `.env`、private token、数据库密码、内部域名或生产集群名称。

## 仓库结构

```text
api/          FastAPI 路由模块
core/         registry、Agent 编排、Coding Task、Issue Intake、workflow
tools/        DB、日志、运行时、代码、存储、HTTP、文档等 Agent 工具
gitlab/       GitLab API 辅助和仓库分析
workflows/    可选 Temporal workflow 定义
activities/   workflow activity 实现
scripts/      本地维护、测试和开源快照工具
docs/         公开文档和演示资产
tests/        后端测试
```

## 安全模型

Viktor 面向证据驱动的 Agent 工作，因此安全边界体现在工作流本身：

- 工具通过 registry 按项目隔离。
- SQL 工具只读，并带有限制和保护。
- Coding Task 受显式策略和人工 gate 控制。
- 高风险路径和命令可以被拒绝。
- 长耗时改代码任务可以隔离在 Kubernetes Job 中运行。
- Trace 记录 intent、LLM 调用、工具调用、产物和最终回答。
- 长期知识通过可审核候选沉淀，而不是让 Agent 静默自我修改。

## 开源快照

私有开发仓库可能包含部署特定配置、内部项目名或私有运维说明。公开 GitHub 版本应通过以下命令生成：

```bash
python scripts/sanitize_open_source.py \
  --output ../viktor-public-snapshot \
  --force \
  --init-git \
  --branch main
```

该脚本会导出无历史快照，移除内部文件，应用公开模板，运行敏感信息扫描，并可在配置后推送到 GitHub。

## License

See [LICENSE](LICENSE).
