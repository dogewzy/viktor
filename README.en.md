# Viktor

Language: [中文](README.md) | English

Viktor is an Agent Harness / Agent Ops platform for small software teams.

It helps a team connect AI agents to real projects, production evidence, coding workflows, and human approval gates. Instead of being another chat box or a standalone coding assistant, Viktor provides the shared backend layer an agent needs to work inside a real engineering environment: project context, tool access, workflow state, audit trails, review gates, and long-term knowledge.

This repository contains the backend and agent orchestration core. The web console is maintained as a separate frontend project.

## Demo

[![Coding full flow demo](docs/assets/coding-full-flow-preview.gif)](docs/assets/coding-full-flow.mp4)

The demo shows a coding task moving through project context loading, code exploration, plan/review state, execution artifacts, changed files, event history, and the final review gate.

## What Viktor Does

Viktor organizes agent work around a `Project`. Each project can register:

- source repositories and repository metadata
- databases, log stores, runtime contexts, and external services
- business context, glossary entries, knowledge notes, and reusable skills
- watchdogs, issue intake configuration, and coding task policies

With those assets in place, Viktor supports several connected workflows:

| Area | Capability |
| --- | --- |
| Chat diagnostics | Project-aware agent answers through DingTalk or the web console, backed by registered tools and trace records. |
| Project onboarding | Analyze a GitLab repository, generate candidate context/glossary/knowledge/connector artifacts, then let a human review and apply them. |
| Evidence tools | Read-only SQL, schema exploration, logs, Kubernetes runtime status/logs, code search/read/explore, Redis, object storage, queues, vector stores, HTTP services, and documents. |
| Issue intake | Turn GitLab issues, local agent submissions, or web-submitted requirements into routed engineering work. |
| Coding Agent | Create background coding tasks with clarification, plan review, isolated workspace execution, diff/report generation, branch/MR handling, and review follow-up. |
| Watchdog | Run scheduled probes, analyze anomalies, notify humans, and optionally open coding work. |
| Trace learning | Convert completed traces into human-reviewable long-term knowledge candidates. |

## Architecture

```text
DingTalk / Web Console / GitLab Issue / Watchdog / Coding Task
        |
        v
FastAPI API layer
        |
        v
Project Registry + MySQL persistence
        |
        v
Agent orchestration
  - diagnostics agent
  - onboarding analyzer
  - issue router
  - coding agent
  - watchdog agent
  - trace learning
        |
        v
Workflow and execution layer
  - optional Temporal workflows
  - Kubernetes coding jobs
  - GitLab MR/webhook/polling integration
        |
        v
Evidence tools
  DB / Code / Logs / K8s / Redis / OSS / Queue / Vector / HTTP / Docs
```

The core design principle is simple: keep durable project knowledge in the registry, fetch fresh evidence through tools, and make risky changes pass through explicit workflow state and human gates.

## Core Workflows

### Project Onboarding

```text
submit project + repositories
  -> analyze repository tree, docs, code, and API entry points
  -> generate candidate artifacts
  -> human reviews, edits, accepts, or rejects artifacts
  -> accepted artifacts become project registry records
```

Candidate artifacts can include project context, directory summaries, API contracts, glossary entries, knowledge notes, database/log/external connectors, and runtime contexts.

### Coding Task

```text
create coding task
  -> load project context and repository connector
  -> explore code before planning
  -> ask clarification when needed
  -> generate a plan and wait for review
  -> execute in an isolated workspace
  -> run allowed checks
  -> produce diff, report, branch, and optional MR
  -> wait for human or automated review follow-up
```

Coding execution is policy-bound. A task policy can limit writable paths, deny secret or production config edits, restrict commands, and control whether branches or merge requests may be created.

### Issue Intake

```text
requirement or bug enters Viktor
  -> create or scan GitLab issue
  -> route to one or more repository connectors
  -> optionally generate a multi-repo change blueprint
  -> create coding tasks
  -> aggregate MR status and notify maintainers
  -> close the issue when all work is merged
```

This turns "someone reported a bug" into a traceable engineering workflow with project routing, coding tasks, merge requests, and notifications.

## Quick Start

Requirements:

- Python 3.12+
- MySQL 8 compatible metadata database
- optional: Kubernetes, GitLab, DingTalk, Temporal, object storage, and log service integrations

Install and run locally:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Create your local environment file from the public example, then fill secrets.
cp .env.example .env

python main.py
```

For local demo data:

```bash
VIKTOR_AUTH_SECRET=change-me-secret \
VIKTOR_ENABLE_DEMO_RESET=1 \
VIKTOR_DEMO_RESET_TOKEN=change-me \
REPO_WARMUP_ENABLED=false \
WATCHDOG_ENABLED=false \
python main.py
```

Then call:

```bash
curl -X POST http://127.0.0.1:8080/api/v1/demo/reset \
  -H "Content-Type: application/json" \
  -H "X-Viktor-Demo-Token: change-me" \
  -d '{"scene":"coding-full-flow"}'
```

## Configuration

Most integrations are optional. A minimal local setup needs:

- `VIKTOR_DB_*` for the metadata database
- `VIKTOR_AUTH_SECRET` for web authentication tokens
- at least one LLM provider key for agent workflows

Common optional integrations:

- `GITLAB_BASE_URL`, `GITLAB_PRIVATE_TOKEN`, `GITLAB_WEBHOOK_SECRET`
- `K8S_NAMESPACE`, `K8S_CONTEXT`, `K8S_API_SERVER`, `K8S_TOKEN`, `K8S_CA_DATA`
- DingTalk app credentials
- Aliyun SLS / object storage credentials
- Temporal endpoint and namespace
- SSH tunnel defaults for local access to private databases

Secrets should come from environment variables or a deployment secret manager. Do not commit `.env`, private tokens, database passwords, internal domains, or production cluster names.

## Repository Layout

```text
api/          FastAPI route modules
core/         registry, agent orchestration, coding tasks, issue intake, workflows
tools/        agent tools for DB, logs, runtime, code, storage, HTTP, and docs
gitlab/       GitLab API helpers and repository analysis
workflows/    optional Temporal workflow definitions
activities/   workflow activity implementations
scripts/      local maintenance, tests, and open-source snapshot tooling
docs/         public docs and demo assets
tests/        focused backend tests
```

## Safety Model

Viktor is built for evidence-driven agent work, so safety is part of the workflow shape:

- Tools are project-scoped through the registry.
- SQL tools are read-only and add limits/guards.
- Coding tasks run under explicit policies and human gates.
- Risky file paths and commands can be denied.
- Long-running code edits can be isolated in Kubernetes jobs.
- Traces record intent, LLM calls, tool calls, artifacts, and final answers.
- Long-term knowledge is created through reviewable candidates, not silent self-modification.

## Open-Source Snapshot

The private development repository may contain deployment-specific configuration, internal project names, or private operational notes. The public GitHub version should be generated with:

```bash
python scripts/sanitize_open_source.py \
  --output ../viktor-public-snapshot \
  --force \
  --init-git \
  --branch main
```

The sanitizer exports a history-free tree, removes internal-only files, applies public templates, runs a sensitive scan, and can push the snapshot to GitHub when configured.

## License

See [LICENSE](LICENSE).
