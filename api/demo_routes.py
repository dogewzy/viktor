"""Local demo data reset endpoints for repeatable product recordings."""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import or_

from core.auth import hash_password
from core.database import SessionLocal
from core.models import (
    CodingArtifactModel,
    CodingAttemptModel,
    CodingEventModel,
    CodingTaskModel,
    IssueIntakeConfigModel,
    IssueIntakeEventModel,
    IssueIntakeLinkModel,
    IssueIntakeTargetModel,
    ProjectModel,
    RepositoryConnectorModel,
    UserModel,
)
from core.registry import ProjectItem, RepositoryConnectorItem, registry
from core.report_store import build_report_url, save_report

router = APIRouter(prefix="/api/v1/demo", tags=["Demo"])

DEMO_PROJECT_ID = "viktor-demo"
DEMO_TASK_ID = "ct_demo_coding_full_flow"
DEMO_DEBUG_TASK_ID = "ct_demo_debug_skill_flow"
DEMO_DEBUG_LINK_ID = "il_demo_debug_skill_flow"
DEMO_USERNAME = "viktor_demo_video"
DEMO_PASSWORD = "change-me"
DEMO_MOBILE = "19900000000"
DEMO_TOKEN = "change-me"
DEMO_SUBMIT_TOKEN = "change-me"
DEMO_GITLAB_BASE_URL = "https://gitlab.example.com"
DEMO_BACKEND_GITLAB_PROJECT = "vdnaserver/viktor_demo"
DEMO_FRONTEND_GITLAB_PROJECT = "vdnaserver/viktor_demo_front"
DEMO_DEBUG_ISSUE_URL = f"{DEMO_GITLAB_BASE_URL}/{DEMO_BACKEND_GITLAB_PROJECT}/-/issues/128"
DEMO_DEBUG_MR_URL = f"{DEMO_GITLAB_BASE_URL}/{DEMO_BACKEND_GITLAB_PROJECT}/-/merge_requests/37"


class DemoResetRequest(BaseModel):
    scene: str = "coding-full-flow"


def _is_demo_reset_enabled() -> bool:
    explicit = os.environ.get("VIKTOR_ENABLE_DEMO_RESET", "").strip().lower()
    if explicit in {"1", "true", "yes", "on"}:
        return True
    if explicit in {"0", "false", "no", "off"}:
        return False
    run_mode = os.environ.get("RUN_MODE", "local-test").strip().lower()
    return run_mode in {"local", "local-test", "dev", "development", "test"}


def _check_demo_access(token: str) -> None:
    if not _is_demo_reset_enabled():
        raise HTTPException(status_code=403, detail="demo reset is disabled")
    expected = os.environ.get("VIKTOR_DEMO_RESET_TOKEN", DEMO_TOKEN)
    if token != expected:
        raise HTTPException(status_code=401, detail="invalid demo reset token")


def _repo_path(dirname: str) -> str:
    path = Path("/path/to/projects") / dirname
    return str(path)


def _upsert_demo_user(db: Any) -> None:
    username_user = db.query(UserModel).filter(UserModel.username == DEMO_USERNAME).first()
    mobile_user = db.query(UserModel).filter(UserModel.mobile == DEMO_MOBILE).first()
    if username_user and mobile_user and username_user.id != mobile_user.id:
        db.delete(mobile_user)
        db.flush()
    user = username_user or mobile_user
    if not user:
        user = UserModel(username=DEMO_USERNAME)
        db.add(user)
    user.username = DEMO_USERNAME
    user.password_hash = hash_password(DEMO_PASSWORD)
    user.password_set = 1
    user.role = "developer"
    user.display_name = "Viktor 演示账号"
    user.mobile = DEMO_MOBILE
    user.profile_key = "developer"
    user.auth_source = "local"
    user.is_active = 1


def _upsert_demo_project(db: Any) -> None:
    project = db.get(ProjectModel, DEMO_PROJECT_ID)
    if not project:
        project = ProjectModel(project_id=DEMO_PROJECT_ID)
        db.add(project)
    project.name = "viktor演示用"
    project.description = "猫超商城演示项目，由 FastAPI 后端和 React/Vite 前端组成，用于 Viktor 演示项目接入、代码自省和 Coding Agent 工作流。"
    project.git_url = _repo_path("viktor_demo")
    project.default_branch = "main"
    project.k8s_workload = None

    repos = [
        RepositoryConnectorModel(
            project_id=DEMO_PROJECT_ID,
            connector_id="backend",
            display_name="猫超商城后端",
            description="FastAPI 后端，负责商品、订单和用户相关 API。Coding Agent 演示默认修改该仓库。",
            git_url=_repo_path("viktor_demo"),
            default_branch="main",
            sort_order=10,
            build_venv=1,
            language="python",
            test_command="python -m py_compile maochao/modules/products/schema.py maochao/modules/products/service.py maochao/modules/products/repository.py",
            lint_command="",
            maintainer_mobile=DEMO_MOBILE,
        ),
        RepositoryConnectorModel(
            project_id=DEMO_PROJECT_ID,
            connector_id="frontend",
            display_name="猫超商城前端",
            description="React/Vite 前端，负责商品列表、购物车和运营配置页面。",
            git_url=_repo_path("viktor_demo_front"),
            default_branch="main",
            sort_order=20,
            build_venv=0,
            language="typescript",
            test_command="pnpm build",
            lint_command="pnpm lint",
            maintainer_mobile=DEMO_MOBILE,
        ),
    ]
    for repo in repos:
        db.merge(repo)


def _demo_plan_markdown() -> str:
    return """## 商品列表补充库存筛选能力

### 执行方案
1. 在商品查询 schema 中增加 `stock_status` 参数，支持 `in_stock` / `out_of_stock`。
2. 在 service 层统一校验筛选值，并向 repository 传递结构化条件。
3. 在 repository 查询中追加库存条件，保持既有分页和排序逻辑。

### 预计改动文件
- `maochao/modules/products/schema.py`
- `maochao/modules/products/service.py`
- `maochao/modules/products/repository.py`

### 验证方式
- 执行 Python 语法检查。
- 通过 diff 和报告确认仅影响商品查询链路。

### 风险
- 需要确认前端下拉枚举和后端枚举保持一致。
"""


def _demo_diff_markdown() -> str:
    return """diff --git a/maochao/modules/products/schema.py b/maochao/modules/products/schema.py
@@
+    stock_status: str | None = None

diff --git a/maochao/modules/products/service.py b/maochao/modules/products/service.py
@@
+    if params.stock_status not in {None, "in_stock", "out_of_stock"}:
+        raise ValueError("stock_status must be in_stock or out_of_stock")

diff --git a/maochao/modules/products/repository.py b/maochao/modules/products/repository.py
@@
+    if stock_status == "in_stock":
+        query = query.filter(Product.stock > 0)
+    elif stock_status == "out_of_stock":
+        query = query.filter(Product.stock <= 0)
"""


def _demo_report_markdown() -> str:
    return """# Viktor Coding Task 演示报告

## 需求
为猫超商城商品列表补充库存筛选能力，让运营可以快速查看有库存和无库存商品。

## 本次实际改动
- schema 增加库存筛选入参。
- service 统一校验枚举值。
- repository 根据库存状态追加查询条件。

## 修改文件
- `maochao/modules/products/repository.py`
- `maochao/modules/products/schema.py`
- `maochao/modules/products/service.py`

## 自动校验
- `python -m py_compile ...` 通过。
- 未创建 MR，任务停在 Code Review gate。

## Review 建议
正式合并前建议补充接口测试，覆盖 `in_stock`、`out_of_stock` 和空筛选三种路径。
"""


def _demo_debug_report_markdown() -> str:
    return """# 测试 Bug Skill 接入演示报告

## 问题
测试同学在猫超商城商品列表验证时发现：选择“有库存”筛选后，接口仍返回库存为 0 的商品。

## Viktor 处理结果
- 本地 Agent 使用测试 Bug Skill 提交问题。
- Viktor 创建 GitLab Issue，并自动路由到后端仓库。
- Coding Agent 补充库存筛选条件，并生成 MR。

## 修改文件
- `maochao/modules/products/repository.py`
- `maochao/modules/products/service.py`

## 验证
- 复现脚本覆盖 `in_stock`、`out_of_stock` 和空筛选。
- `python -m py_compile ...` 通过。

## 给测试同学看的闭环信号
- GitLab Issue 已关联 Coding Task。
- MR 已创建，可从需求接入页直接跳转查看。
"""


def _event_rows(base_time: datetime) -> list[tuple[str, str, str, dict[str, Any]]]:
    return [
        ("created", "created", "Coding task 已创建，开始生成 plan", {}),
        ("stage_changed", "loading_context", "正在加载项目上下文", {}),
        ("stage_changed", "code_exploration", "正在只读探索代码并压缩关键上下文", {}),
        ("code_exploration_completed", "code_exploration", "Plan 前置代码探索完成", {"files": 3}),
        ("stage_changed", "drafting_plan", "正在基于已核对代码生成正式 Plan", {}),
        ("plan_generated", "waiting_plan_review", "Plan 已生成，等待人工审核", {}),
        ("plan_approved", "plan_approved", "Plan 已通过，等待启动执行", {"reviewer": "Viktor 演示账号"}),
        ("execution_start_requested", "queued", "执行已启动", {}),
        ("stage_changed", "preparing_workspace", "正在准备隔离 workspace", {}),
        ("stage_changed", "agent_running", "Coding Agent 正在分析并修改代码（第 1 轮）", {}),
        ("tool_call_started", "agent_running", "list_files", {"tool": "list_files"}),
        ("tool_call_finished", "agent_running", "list_files", {"tool": "list_files", "ok": True}),
        ("tool_call_started", "agent_running", "grep", {"tool": "grep", "query": "stock"}),
        ("tool_call_finished", "agent_running", "grep", {"tool": "grep", "ok": True}),
        ("tool_call_started", "agent_running", "read_file", {"tool": "read_file", "path": "maochao/modules/products/service.py"}),
        ("tool_call_finished", "agent_running", "read_file", {"tool": "read_file", "ok": True}),
        ("tool_call_started", "agent_running", "write_file", {"tool": "write_file", "path": "maochao/modules/products/schema.py"}),
        ("tool_call_finished", "agent_running", "write_file", {"tool": "write_file", "ok": True}),
        ("tool_call_started", "agent_running", "check_syntax", {"tool": "check_syntax"}),
        ("tool_call_finished", "agent_running", "check_syntax", {"tool": "check_syntax", "ok": True}),
        ("diff_updated", "summarizing", "Diff 已生成", {"changed_files": 3}),
        ("report_generated", "reporting", "长报告已生成", {}),
        ("mr_skipped", "waiting_code_review", "policy 未开启自动 push/MR，已保留本地 workspace 和报告", {}),
        ("stage_changed", "waiting_code_review", "执行完成，等待用户处理 Kimi Review", {}),
    ]


def _issue_event_rows(base_time: datetime) -> list[tuple[str, str, str, dict[str, Any]]]:
    return [
        ("local_agent_submitted", "created", "本地 Agent 已通过测试 Bug Skill 提交问题", {"source": "codex_skill"}),
        ("gitlab_issue_created", "issue_created", "GitLab Issue 已创建", {"issue_url": DEMO_DEBUG_ISSUE_URL}),
        ("issue_routed", "routed", "Viktor 已判断该问题归属后端仓库", {"repo_connector_id": "backend"}),
        ("coding_task_created", "coding_task_created", "Coding Task 已创建并进入执行", {"task_id": DEMO_DEBUG_TASK_ID}),
        ("agent_plan_generated", "planning", "Agent 已生成修复计划", {"files": ["maochao/modules/products/repository.py"]}),
        ("agent_execution_done", "summarizing", "Agent 已完成修复和验证", {"syntax_check": "passed"}),
        ("merge_request_created", "mr_created", "MR 已创建，等待研发/测试确认", {"mr_url": DEMO_DEBUG_MR_URL}),
    ]


def _reset_demo_coding_task(db: Any, report_id: str) -> None:
    task_ids = [
        row.task_id
        for row in db.query(CodingTaskModel.task_id)
        .filter(CodingTaskModel.project_id == DEMO_PROJECT_ID)
        .all()
    ]
    if task_ids:
        db.query(CodingEventModel).filter(CodingEventModel.task_id.in_(task_ids)).delete(synchronize_session=False)
        db.query(CodingArtifactModel).filter(CodingArtifactModel.task_id.in_(task_ids)).delete(synchronize_session=False)
        db.query(CodingAttemptModel).filter(CodingAttemptModel.task_id.in_(task_ids)).delete(synchronize_session=False)
        db.query(CodingTaskModel).filter(CodingTaskModel.task_id.in_(task_ids)).delete(synchronize_session=False)

    now = datetime.now()
    base_time = datetime(2026, 6, 20, 22, 53, 33)
    changed_files = [
        "maochao/modules/products/repository.py",
        "maochao/modules/products/schema.py",
        "maochao/modules/products/service.py",
    ]
    plan_markdown = _demo_plan_markdown()
    result = {
        "plan": {
            "summary": "商品列表补充库存筛选能力",
            "steps": [
                "定位商品查询 schema / service / repository",
                "增加 stock_status 参数与枚举校验",
                "追加库存查询条件并保持分页逻辑",
                "执行语法检查并生成 diff/report",
            ],
            "files": changed_files,
            "risks": ["需要前端枚举与后端保持一致"],
            "acceptance_criteria": ["语法检查通过", "diff 仅包含商品查询链路"],
        },
        "plan_markdown": plan_markdown,
        "approved_plan_markdown": plan_markdown,
        "approved_at": base_time.replace(hour=22, minute=54, second=20).isoformat(),
        "plan_review": {
            "decision": "approved",
            "comment": "演示流程自动通过 Plan，进入执行阶段。",
            "reviewer": "Viktor 演示账号",
            "reviewed_at": base_time.replace(hour=22, minute=54, second=20).isoformat(),
        },
        "changed_files": changed_files,
        "risk_flags": [],
        "report_id": report_id,
        "report_url": build_report_url(report_id),
        "workspace_path": f"/tmp/viktor/coding/{DEMO_TASK_ID}/repo",
        "branch": f"viktor/{DEMO_TASK_ID}",
        "base_commit": "b6deaa75c615d31f445eeb6dc71dc45d80743eb9",
        "head_commit": "",
        "automated_review": {"status": "pending", "items": []},
        "code_review": {"status": "pending"},
    }
    db.add(CodingTaskModel(
        task_id=DEMO_TASK_ID,
        project_id=DEMO_PROJECT_ID,
        requirement="项目：viktor-demo\n\n为商品列表补充库存筛选能力，并输出可 review 的 diff/report。",
        status="waiting_code_review",
        stage="waiting_code_review",
        message="执行完成，等待用户处理 Kimi Review",
        repo_connector_id="backend",
        target_branch="main",
        work_branch=f"viktor/{DEMO_TASK_ID}",
        mr_url="",
        report_id=report_id,
        policy={"create_mr": False, "demo": True},
        control={},
        result=result,
        created_by="Viktor 演示账号",
        created_by_mobile=DEMO_MOBILE,
        pending_gate="code_review",
        pending_owner_mobile=DEMO_MOBILE,
        pending_owner_label="backend maintainer",
        created_at=base_time,
        updated_at=now,
    ))
    db.add(CodingAttemptModel(
        attempt_id=f"cat_{DEMO_TASK_ID.removeprefix('ct_')}",
        task_id=DEMO_TASK_ID,
        project_id=DEMO_PROJECT_ID,
        repo_connector_id="backend",
        status="waiting_code_review",
        stage="waiting_code_review",
        workspace_path="",
        branch_name=f"viktor/{DEMO_TASK_ID}",
        base_commit="b6deaa75c615d31f445eeb6dc71dc45d80743eb9",
        head_commit="",
        plan=plan_markdown,
        summary="为商品列表补充库存筛选参数，更新 schema、service 和 repository。",
        test_results={
            "changed_files": changed_files,
            "edit_round": 1,
            "syntax_check": "passed",
        },
        risk_flags=[],
        created_at=base_time + timedelta(seconds=75),
        updated_at=now,
    ))
    db.add(CodingArtifactModel(
        artifact_id="cart_demo_plan",
        task_id=DEMO_TASK_ID,
        attempt_id="",
        project_id=DEMO_PROJECT_ID,
        artifact_type="plan",
        title="Coding Plan",
        content=plan_markdown,
        payload={"demo": True},
        created_at=base_time + timedelta(seconds=15),
    ))
    db.add(CodingArtifactModel(
        artifact_id="cart_demo_diff",
        task_id=DEMO_TASK_ID,
        attempt_id=f"cat_{DEMO_TASK_ID.removeprefix('ct_')}",
        project_id=DEMO_PROJECT_ID,
        artifact_type="diff",
        title="Demo diff",
        content=_demo_diff_markdown(),
        payload={"changed_files": changed_files},
        created_at=base_time + timedelta(seconds=83),
    ))
    for seq, (event_type, stage, message, payload) in enumerate(_event_rows(base_time), start=1):
        event_payload = dict(payload)
        if event_type == "report_generated":
            event_payload["report_id"] = report_id
        db.add(CodingEventModel(
            task_id=DEMO_TASK_ID,
            attempt_id=f"cat_{DEMO_TASK_ID.removeprefix('ct_')}" if seq >= 8 else "",
            seq=seq,
            event_type=event_type,
            stage=stage,
            message=message,
            payload=event_payload,
            created_at=base_time + timedelta(seconds=seq * 4),
        ))


def _upsert_demo_issue_intake_config(db: Any) -> None:
    config = db.get(IssueIntakeConfigModel, DEMO_PROJECT_ID)
    if not config:
        config = IssueIntakeConfigModel(project_id=DEMO_PROJECT_ID)
        db.add(config)
    config.issue_project_url = f"{DEMO_GITLAB_BASE_URL}/{DEMO_BACKEND_GITLAB_PROJECT}"
    config.default_repo_connector_id = "backend"
    config.default_labels = ["viktor:auto", "demo", "qa"]
    config.submit_token = DEMO_SUBMIT_TOKEN
    config.notification = {"dingtalk_group": "demo-recording", "enabled": False}
    config.assignee_mobiles = {"backend": DEMO_MOBILE, "frontend": DEMO_MOBILE}
    config.scan_interval_sec = 300
    config.enabled = 1

    db.query(IssueIntakeTargetModel).filter(IssueIntakeTargetModel.project_id == DEMO_PROJECT_ID).delete()
    db.add(IssueIntakeTargetModel(
        project_id=DEMO_PROJECT_ID,
        repo_connector_id="backend",
        issue_project_url=f"{DEMO_GITLAB_BASE_URL}/{DEMO_BACKEND_GITLAB_PROJECT}",
        labels=["viktor:auto", "demo", "qa", "backend"],
        enabled=1,
    ))
    db.add(IssueIntakeTargetModel(
        project_id=DEMO_PROJECT_ID,
        repo_connector_id="frontend",
        issue_project_url=f"{DEMO_GITLAB_BASE_URL}/{DEMO_FRONTEND_GITLAB_PROJECT}",
        labels=["viktor:auto", "demo", "qa", "frontend"],
        enabled=1,
    ))


def _reset_demo_issue_debug_flow(db: Any, report_id: str) -> None:
    link_ids = [
        row.link_id
        for row in db.query(IssueIntakeLinkModel.link_id)
        .filter(IssueIntakeLinkModel.project_id == DEMO_PROJECT_ID)
        .all()
    ]
    if link_ids:
        db.query(IssueIntakeEventModel).filter(IssueIntakeEventModel.link_id.in_(link_ids)).delete(synchronize_session=False)
        db.query(IssueIntakeLinkModel).filter(IssueIntakeLinkModel.link_id.in_(link_ids)).delete(synchronize_session=False)

    task_ids = [
        row.task_id
        for row in db.query(CodingTaskModel.task_id)
        .filter(CodingTaskModel.project_id == DEMO_PROJECT_ID)
        .all()
    ]
    if task_ids:
        db.query(CodingEventModel).filter(CodingEventModel.task_id.in_(task_ids)).delete(synchronize_session=False)
        db.query(CodingArtifactModel).filter(CodingArtifactModel.task_id.in_(task_ids)).delete(synchronize_session=False)
        db.query(CodingAttemptModel).filter(CodingAttemptModel.task_id.in_(task_ids)).delete(synchronize_session=False)
        db.query(CodingTaskModel).filter(CodingTaskModel.task_id.in_(task_ids)).delete(synchronize_session=False)

    now = datetime.now()
    base_time = datetime(2026, 6, 20, 23, 18, 12)
    title = "商品列表库存筛选返回了无库存商品"
    description = """## 测试 Bug

### 环境
- 项目：猫超商城演示项目
- 页面：商品列表
- 分支：main

### 复现步骤
1. 打开商品列表。
2. 在库存筛选中选择“有库存”。
3. 查看接口返回结果和页面列表。

### 实际结果
列表中仍出现库存为 0 的商品。

### 期望结果
选择“有库存”时，仅返回库存大于 0 的商品。

### 影响
测试无法确认运营筛选结果，容易误判上架状态。
"""
    plan_markdown = """## 修复计划

1. 定位商品列表查询链路中的库存筛选参数。
2. 在 service 层补充枚举校验。
3. 在 repository 查询中追加 `stock > 0` / `stock <= 0` 条件。
4. 增加轻量复现脚本并执行语法检查。
"""
    changed_files = [
        "maochao/modules/products/repository.py",
        "maochao/modules/products/service.py",
    ]
    result = {
        "source": "local_agent",
        "reporter_mobile": DEMO_MOBILE,
        "skill_filename": "viktor-viktor-demo-bug-issue-skill.md",
        "routed": [{"repo_connector_id": "backend", "reason": "库存筛选由后端商品查询 API 控制"}],
        "coding_tasks": [{
            "repo_connector_id": "backend",
            "coding_task_id": DEMO_DEBUG_TASK_ID,
            "mr_url": DEMO_DEBUG_MR_URL,
            "role": "primary",
            "reason": "修复商品查询库存条件",
        }],
        "blueprint": {
            "status": "approved",
            "reviewer": "Viktor 演示账号",
            "repos": [{
                "repo_connector_id": "backend",
                "role": "primary",
                "reason": "库存筛选 Bug 只涉及商品查询后端逻辑",
            }],
            "contracts": [],
            "analysis": "测试 Bug 可由后端仓库独立修复；前端仅消费已有筛选参数，无需联动。",
        },
    }

    db.add(CodingTaskModel(
        task_id=DEMO_DEBUG_TASK_ID,
        project_id=DEMO_PROJECT_ID,
        requirement=description,
        status="mr_created",
        stage="mr_created",
        message="修复完成，MR 已创建，等待 Review / 合并。",
        repo_connector_id="backend",
        target_branch="main",
        work_branch=f"viktor/{DEMO_DEBUG_TASK_ID}",
        mr_url=DEMO_DEBUG_MR_URL,
        report_id=report_id,
        policy={"create_mr": True, "demo": True},
        control={},
        result={
            "plan_markdown": plan_markdown,
            "approved_plan_markdown": plan_markdown,
            "changed_files": changed_files,
            "mr_url": DEMO_DEBUG_MR_URL,
            "report_id": report_id,
            "report_url": build_report_url(report_id),
            "source_issue": {
                "link_id": DEMO_DEBUG_LINK_ID,
                "issue_url": DEMO_DEBUG_ISSUE_URL,
                "issue_iid": "128",
            },
        },
        created_by="测试同学",
        created_by_mobile=DEMO_MOBILE,
        pending_gate="",
        pending_owner_mobile="",
        pending_owner_label="",
        created_at=base_time + timedelta(seconds=18),
        updated_at=now,
    ))
    db.add(CodingArtifactModel(
        artifact_id="debug_skill_demo_plan",
        task_id=DEMO_DEBUG_TASK_ID,
        attempt_id="",
        project_id=DEMO_PROJECT_ID,
        artifact_type="plan",
        title="Debug Skill 修复计划",
        content=plan_markdown,
        payload={"demo": True},
        created_at=base_time + timedelta(seconds=26),
    ))
    db.add(IssueIntakeLinkModel(
        link_id=DEMO_DEBUG_LINK_ID,
        project_id=DEMO_PROJECT_ID,
        repo_connector_id="backend",
        source="local_agent",
        kind="bug",
        reporter="测试同学",
        title=title,
        description=description,
        status="mr_created",
        stage="mr_created",
        message="本地 Agent 提交后，Viktor 已接单、完成修复并创建 MR。",
        gitlab_base_url=DEMO_GITLAB_BASE_URL,
        gitlab_project_path=DEMO_BACKEND_GITLAB_PROJECT,
        gitlab_project_id="demo-viktor-backend",
        issue_id="900128",
        issue_iid="128",
        issue_url=DEMO_DEBUG_ISSUE_URL,
        issue_state="opened",
        issue_labels=["viktor:auto", "demo", "qa", "bug"],
        issue_payload={"demo": True, "title": title},
        assignees=[{"name": "Viktor Coding Agent"}],
        coding_task_id=DEMO_DEBUG_TASK_ID,
        mr_url=DEMO_DEBUG_MR_URL,
        report_id=report_id,
        dedupe_key=f"{DEMO_BACKEND_GITLAB_PROJECT}#128",
        last_error="",
        result=result,
        created_at=base_time,
        updated_at=now,
    ))
    for seq, (event_type, stage, message, payload) in enumerate(_issue_event_rows(base_time), start=1):
        db.add(IssueIntakeEventModel(
            link_id=DEMO_DEBUG_LINK_ID,
            seq=seq,
            event_type=event_type,
            stage=stage,
            message=message,
            payload=dict(payload),
            created_at=base_time + timedelta(seconds=seq * 6),
        ))


def _refresh_registry() -> None:
    registry.register_project(ProjectItem(
        id=DEMO_PROJECT_ID,
        name="viktor演示用",
        description="猫超商城演示项目，由 FastAPI 后端和 React/Vite 前端组成。",
        git_url=_repo_path("viktor_demo"),
        default_branch="main",
    ))
    registry.register_repository_connector(RepositoryConnectorItem(
        id="backend",
        project_id=DEMO_PROJECT_ID,
        display_name="猫超商城后端",
        description="FastAPI 后端，负责商品、订单和用户相关 API。",
        git_url=_repo_path("viktor_demo"),
        default_branch="main",
        sort_order=10,
        build_venv=True,
        language="python",
        test_command="python -m py_compile maochao/modules/products/schema.py maochao/modules/products/service.py maochao/modules/products/repository.py",
        maintainer_mobile=DEMO_MOBILE,
    ))
    registry.register_repository_connector(RepositoryConnectorItem(
        id="frontend",
        project_id=DEMO_PROJECT_ID,
        display_name="猫超商城前端",
        description="React/Vite 前端，负责商品列表、购物车和运营配置页面。",
        git_url=_repo_path("viktor_demo_front"),
        default_branch="main",
        sort_order=20,
        build_venv=False,
        language="typescript",
        test_command="pnpm build",
        lint_command="pnpm lint",
        maintainer_mobile=DEMO_MOBILE,
    ))


@router.post("/reset", summary="重置本地演示数据")
def reset_demo(
    body: DemoResetRequest,
    x_viktor_demo_token: str = Header(default=""),
) -> dict:
    _check_demo_access(x_viktor_demo_token)
    if body.scene not in {"coding-full-flow", "debug-skill-flow"}:
        raise HTTPException(status_code=404, detail=f"unknown demo scene: {body.scene}")

    report_id, _, _ = save_report(
        markdown_text=_demo_report_markdown() if body.scene == "coding-full-flow" else _demo_debug_report_markdown(),
        project_id=DEMO_PROJECT_ID,
        thread_id=f"demo:{body.scene}",
        title="Viktor Coding Task 演示报告" if body.scene == "coding-full-flow" else "测试 Bug Skill 接入演示报告",
    )
    db = SessionLocal()
    try:
        _upsert_demo_user(db)
        _upsert_demo_project(db)
        if body.scene == "coding-full-flow":
            _reset_demo_coding_task(db, report_id)
            task_id = DEMO_TASK_ID
            path = f"/coding?project_id={DEMO_PROJECT_ID}&task_id={DEMO_TASK_ID}"
            extra: dict[str, str] = {}
        else:
            _upsert_demo_issue_intake_config(db)
            _reset_demo_issue_debug_flow(db, report_id)
            task_id = DEMO_DEBUG_TASK_ID
            path = f"/issue-intake?project_id={DEMO_PROJECT_ID}"
            extra = {
                "link_id": DEMO_DEBUG_LINK_ID,
                "result_path": f"/issue-intake?project_id={DEMO_PROJECT_ID}&link_id={DEMO_DEBUG_LINK_ID}",
                "issue_url": DEMO_DEBUG_ISSUE_URL,
                "mr_url": DEMO_DEBUG_MR_URL,
            }
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    _refresh_registry()
    return {
        "ok": True,
        "scene": body.scene,
        "project_id": DEMO_PROJECT_ID,
        "task_id": task_id,
        "path": path,
        "report_id": report_id,
        "report_url": build_report_url(report_id),
        "credentials": {
            "username": DEMO_USERNAME,
            "password": DEMO_PASSWORD,
        },
        **extra,
    }
