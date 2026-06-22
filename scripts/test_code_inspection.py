#!/usr/bin/env python3
"""代码自省（一期）· 端到端自测脚本。

覆盖:
  A. Registry：ProjectItem 新字段 + GlossaryItem CRUD
  B. Prompt：代码自省指南 + 业务术语表注入
  C. 三件套：code_glob / code_grep / code_read（含路径穿越防护）
  D. 代码同步：resolve_live_commit + ensure_workspace（可选，online 模式）
  E. Explorer Sub-Agent：code_explore（可选，需要 DEEPSEEK_API_KEY）

用法:
    # 离线模式（默认）：用 Viktor 自身仓库作 workspace，不动网络
    python scripts/test_code_inspection.py

    # 在线模式：真跑 K8s image→commit→clone（需要 ali-prod 可达 + git_url）
    python scripts/test_code_inspection.py --online --project video-tracker

    # 探索子 agent（需要 DEEPSEEK_API_KEY）
    python scripts/test_code_inspection.py --explorer --task "聊天消息如何持久化"

退出码:
    0 = 全部通过
    1 = 至少一项失败
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import traceback
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from loguru import logger

from core.registry import ContextItem, GlossaryItem, K8sWorkloadRef, ProjectItem, RepositoryConnectorItem, registry

# ============================================================
# 彩色输出
# ============================================================

GREEN, RED, YELLOW, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[0m"
_results: list[tuple[str, bool, str]] = []


def _case(name: str):
    def deco(fn):
        def wrapper(*args, **kwargs):
            print(f"\n{'='*60}\n[CASE] {name}\n{'='*60}")
            try:
                fn(*args, **kwargs)
                _results.append((name, True, ""))
                print(f"{GREEN}✓ PASS{RESET}: {name}")
            except AssertionError as e:
                _results.append((name, False, str(e)))
                print(f"{RED}✗ FAIL{RESET}: {name}\n  {e}")
            except Exception as e:  # noqa: BLE001
                _results.append((name, False, f"{type(e).__name__}: {e}"))
                print(f"{RED}✗ ERROR{RESET}: {name}")
                traceback.print_exc()
        return wrapper
    return deco


# ============================================================
# 准备假项目
# ============================================================

FAKE_PROJECT_ID = "viktor-self"


def _seed_fake_project() -> None:
    """把 Viktor 自身注册为假项目，用于离线测试三件套。
    注意：不写 DB，仅写内存 Registry。"""
    registry.register_project(ProjectItem(
        id=FAKE_PROJECT_ID,
        name="Viktor Self (Test)",
        description="仅用于 code_inspection 自测，不落库",
        git_url="https://example.com/fake.git",       # 保证 is_enabled 判定通过
        default_branch="master",
        k8s_workload=None,
    ))


def _patch_ensure_workspace_to_self() -> None:
    """离线模式：把 ensure_workspace 重定向到 Viktor 仓库本身，跳过 K8s/git。"""
    import core.code_sync as code_sync
    import tools.code_inspector as ci

    def _fake_ensure(project_id: str, commit_sha=None, connector_id=None, repo_connector_id=None):
        return PROJECT_ROOT

    code_sync.ensure_workspace = _fake_ensure   # noqa: SLF001
    ci.ensure_workspace = _fake_ensure           # 已经在 import 时绑定过，替换模块内引用


# ============================================================
# A. Registry + Glossary
# ============================================================

@_case("Registry: 项目新字段 + Glossary CRUD")
def test_registry_and_glossary():
    p = registry.get_project(FAKE_PROJECT_ID)
    assert p is not None, "假项目未注册"
    assert p.git_url and p.default_branch == "master"

    g1 = GlossaryItem(
        id="order-create", project_id=FAKE_PROJECT_ID,
        term="下单", aliases=["生单", "出单"],
        code_keywords=["create_order", "OrderService.create"],
        description="订单创建主流程", enabled=True,
    )
    g2 = GlossaryItem(
        id="disabled-one", project_id=FAKE_PROJECT_ID,
        term="废弃术语", enabled=False,
    )
    registry.register_glossary(g1)
    registry.register_glossary(g2)

    all_ = registry.get_glossaries(FAKE_PROJECT_ID, only_enabled=False)
    enabled = registry.get_glossaries(FAKE_PROJECT_ID, only_enabled=True)
    assert len(all_) == 2 and len(enabled) == 1, f"all={len(all_)} enabled={len(enabled)}"
    assert enabled[0].id == "order-create"

    assert registry.unregister_glossary(FAKE_PROJECT_ID, "disabled-one")
    remain = registry.get_glossaries(FAKE_PROJECT_ID, only_enabled=False)
    assert len(remain) == 1 and remain[0].id == "order-create"


@_case("RegistryPersistence: upsert/load 字段不丢")
def test_registry_persistence_roundtrip():
    from core.models import DatabaseConnectorModel, ExternalConnectorModel
    from core.registry import DatabaseConnectorItem, ExternalConnectorItem, SSHTunnelSpec
    from core.registry_persistence import (
        database_connector_item_from_model,
        database_connector_values,
        external_connector_item_from_model,
        external_connector_values,
    )

    db_item = DatabaseConnectorItem(
        id="db1",
        project_id=FAKE_PROJECT_ID,
        host="mysql.internal",
        port=3307,
        username="readonly",
        password="secret",
        database="biz",
        readonly=True,
        ssh_tunnel=SSHTunnelSpec(),
    )
    loaded_db = database_connector_item_from_model(DatabaseConnectorModel(**database_connector_values(db_item)))
    assert loaded_db.ssh_tunnel is not None, "ssh_tunnel={} 应保留为开启默认隧道"
    assert loaded_db.ssh_tunnel.model_dump(exclude_none=True) == {}, loaded_db.ssh_tunnel
    assert loaded_db.host == db_item.host and loaded_db.database == db_item.database

    ext_item = ExternalConnectorItem(
        id="redis1",
        project_id=FAKE_PROJECT_ID,
        connector_type="redis",
        config={"host": "redis.internal", "db": 3},
        secrets={"password": "redis-secret"},
    )
    loaded_ext = external_connector_item_from_model(ExternalConnectorModel(**external_connector_values(ext_item)))
    assert loaded_ext.config == ext_item.config
    assert loaded_ext.secrets == ext_item.secrets
    print("  DatabaseConnector ssh_tunnel 与 ExternalConnector secrets roundtrip OK")


# ============================================================
# B. Prompt 注入
# ============================================================

@_case("Prompt: 代码自省指南 + 术语表注入")
def test_prompt_injection():
    from core.prompt_builder import build_system_prompt

    # 不走子系统路由：enable_routing=False
    prompt = asyncio.run(build_system_prompt(FAKE_PROJECT_ID, "", enable_routing=False))
    assert "代码自省能力" in prompt, "代码自省指南未注入"
    assert "业务术语表" in prompt, "术语表块未注入"
    assert "下单" in prompt and "create_order" in prompt, "术语条目内容丢失"
    assert "废弃术语" not in prompt, "disabled 术语不应出现"


@_case("Prompt: 子系统路由不暴露未注册上下文")
def test_subsystem_router_uses_registered_contexts():
    from core import prompt_builder

    project_id = "router-stale-context-test"
    registry.register_project(ProjectItem(
        id=project_id,
        name="Router Stale Context Test",
        description="验证子系统路由只使用当前 registry 中存在的上下文",
    ))
    registry.register_context(ContextItem(
        id="active-context",
        project_id=project_id,
        priority=1,
        content="当前有效上下文",
    ))
    registry.register_repository_connector(RepositoryConnectorItem(
        id="active-repo",
        project_id=project_id,
        display_name="active-repo 当前仓库",
        git_url="https://example.com/active-repo.git",
    ))

    original = prompt_builder.SUBSYSTEM_ROUTER_CONFIG.get(project_id)
    prompt_builder.SUBSYSTEM_ROUTER_CONFIG[project_id] = {
        "description": "测试路由配置",
        "subsystems": [
            {
                "id": "active-repo",
                "name": "active-repo 当前仓库",
                "description": "当前仍在 registry 中的仓库",
                "keywords": ["仓库"],
                "context_ids": ["repo-context-not-required"],
            },
            {
                "id": "active-context-service",
                "name": "active-context-service 当前上下文",
                "description": "当前仍在 registry 中的服务",
                "keywords": ["当前"],
                "context_ids": ["active-context"],
            },
            {
                "id": "removed-service",
                "name": "removed-service 已移除服务",
                "description": "旧业务知识，不应暴露给分类模型",
                "keywords": ["旧服务"],
                "context_ids": ["removed-context"],
            },
        ],
    }
    try:
        desc = prompt_builder._get_subsystem_description(project_id)  # noqa: SLF001
        assert "active-repo" in desc, desc
        assert "active-context-service" in desc, desc
        assert "removed-service" not in desc, desc

        subsystems, include_overview = prompt_builder._keyword_based_classify(project_id, "旧服务问题")  # noqa: SLF001
        assert subsystems == [], f"不应命中过期 subsystem: {subsystems}"
        assert include_overview is True

        contexts = registry.get_contexts(project_id)
        filtered = prompt_builder._filter_contexts(project_id, contexts, ["removed-service"], False)  # noqa: SLF001
        assert [ctx.id for ctx in filtered] == ["active-context"], [ctx.id for ctx in filtered]
    finally:
        if original is None:
            prompt_builder.SUBSYSTEM_ROUTER_CONFIG.pop(project_id, None)
        else:
            prompt_builder.SUBSYSTEM_ROUTER_CONFIG[project_id] = original
        registry.unregister_project(project_id)


@_case("Coding: Plan 修正意见会重新进入 planning")
def test_coding_plan_revision_request():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from core import coding_service
    from core.models import Base, CodingArtifactModel, CodingTaskModel

    engine = create_engine("sqlite:///:memory:")
    TestingSessionLocal = sessionmaker(bind=engine)
    Base.metadata.create_all(bind=engine)

    original_session = coding_service.SessionLocal
    original_emit = coding_service.emit_event
    original_thread = coding_service.threading.Thread
    emitted_events = []

    class _NoopThread:
        started = False

        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            type(self).started = True

    coding_service.SessionLocal = TestingSessionLocal
    coding_service.emit_event = lambda *args, **kwargs: emitted_events.append((args, kwargs))
    coding_service.threading.Thread = _NoopThread
    task_id = "ct_revision_test"
    try:
        db = TestingSessionLocal()
        try:
            db.add(CodingTaskModel(
                task_id=task_id,
                project_id=FAKE_PROJECT_ID,
                requirement="修正 Plan workflow",
                status="waiting_plan_review",
                stage="waiting_plan_review",
                message="Plan 已生成，等待人工审核",
                repo_connector_id="",
                target_branch="master",
                work_branch="viktor/ct_revision_test",
                policy={},
                control={},
                result={
                    "plan_markdown": "## 旧 Plan\n- 旧假设",
                    "approved_plan_markdown": "不应保留",
                },
            ))
            db.commit()
        finally:
            db.close()

        task = coding_service.request_plan_revision(task_id, comment="旧服务已移除，请重新定位", reviewer="tester")
        assert task["status"] == "planning", task
        assert _NoopThread.started is True, "应启动重新规划线程"

        db = TestingSessionLocal()
        try:
            row = db.get(CodingTaskModel, task_id)
            assert row is not None
            assert row.status == "planning"
            result = row.result or {}
            assert "approved_plan_markdown" not in result, result
            revisions = result.get("plan_revisions") or []
            assert revisions and revisions[-1]["comment"] == "旧服务已移除，请重新定位"
            artifact = db.query(CodingArtifactModel).filter_by(task_id=task_id, artifact_type="plan_review").first()
            assert artifact is not None and "旧服务已移除" in artifact.content
            assert any(event[0][1] == "plan_revision_requested" for event in emitted_events), "应记录 plan_revision_requested 事件"
        finally:
            db.close()
    finally:
        coding_service.SessionLocal = original_session
        coding_service.emit_event = original_emit
        coding_service.threading.Thread = original_thread


@_case("Coding: Planning 会先探索代码再生成正式 Plan")
def test_coding_planning_uses_code_exploration():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from core import coding_service
    from core.models import Base, CodingArtifactModel, CodingTaskModel

    engine = create_engine("sqlite:///:memory:")
    TestingSessionLocal = sessionmaker(bind=engine)
    Base.metadata.create_all(bind=engine)

    original_session = coding_service.SessionLocal
    original_emit = coding_service.emit_event
    original_build_system_prompt = coding_service.build_system_prompt
    original_run_explorer = coding_service.run_explorer
    original_run_coding_clarification = coding_service.run_coding_clarification
    original_run_coding_plan = coding_service.run_coding_plan
    emitted_events = []
    captured_plan_args = {}

    async def _fake_build_system_prompt(
        project_id: str,
        user_message: str = "",
        enable_routing: bool = True,
        retrieval_context: str | None = None,
    ):
        assert project_id == FAKE_PROJECT_ID
        assert "母本停止录入缓冲" in user_message
        return "项目上下文：order-api 当前仓库"

    async def _fake_run_explorer(project_id: str, task: str):
        assert project_id == FAKE_PROJECT_ID
        assert "不是修改代码" in task
        assert "母本停止录入缓冲" in task
        return {
            "summary": "14 天来自 sync_tracking_status.py 中两个串行 7 天门槛。",
            "relevant_files": [
                {
                    "path": "cronjobs/sync_tracking_status.py",
                    "why": "takedown 降档与 Milvus 清理都在这里",
                    "key_lines": "40-180",
                }
            ],
            "key_symbols": [
                {
                    "file": "cronjobs/sync_tracking_status.py",
                    "symbol": "_cleanup_milvus_for_stop_tracking",
                    "lines": "130-180",
                }
            ],
            "searched_keywords": ["UNTRACK_STALE_DAYS", "stop_tracking_at", "delete_by_bid"],
            "dead_ends": ["未发现 hongguo-meta-ingest"],
            "_meta": {"commit": "abc1234"},
        }

    async def _fake_run_coding_plan(*, requirement: str, project_context: str, code_exploration: str = ""):
        captured_plan_args["requirement"] = requirement
        captured_plan_args["project_context"] = project_context
        captured_plan_args["code_exploration"] = code_exploration
        return """## Summary
当前 14 天来自两个串行 7 天门槛。

## Code Findings
- cronjobs/sync_tracking_status.py (40-180): takedown 降档与 Milvus 清理都在这里。

## Key Changes
- 拆分 UNTRACK_STALE_DAYS 与 MILVUS_CLEANUP_STALE_DAYS。

## Impact
- Milvus 删除时间从 7+7 改为 7+0。

## Test Plan
- Mock stop_tracking_at 为当前时间，确认 cleanup 候选立即进入。

## Assumptions
- 只调整当前主链路。
""", []

    async def _fake_run_coding_clarification(*, requirement: str, project_context: str, code_exploration: str = ""):
        assert "母本停止录入缓冲" in requirement
        assert "sync_tracking_status.py" in code_exploration
        return {"needs_clarification": False, "questions": [], "term_mappings": []}

    coding_service.SessionLocal = TestingSessionLocal
    coding_service.emit_event = lambda *args, **kwargs: emitted_events.append((args, kwargs))
    coding_service.build_system_prompt = _fake_build_system_prompt
    coding_service.run_explorer = _fake_run_explorer
    coding_service.run_coding_clarification = _fake_run_coding_clarification
    coding_service.run_coding_plan = _fake_run_coding_plan
    task_id = "ct_planning_explore_test"
    try:
        db = TestingSessionLocal()
        try:
            db.add(CodingTaskModel(
                task_id=task_id,
                project_id=FAKE_PROJECT_ID,
                requirement="母本停止录入缓冲从 7+7 缩短到 7+0",
                status="created",
                stage="created",
                message="",
                repo_connector_id="",
                target_branch="master",
                work_branch="viktor/ct_planning_explore_test",
                policy={},
                control={},
                result={},
            ))
            db.commit()
        finally:
            db.close()

        asyncio.run(coding_service.run_coding_planning(task_id))

        db = TestingSessionLocal()
        try:
            row = db.get(CodingTaskModel, task_id)
            assert row is not None
            assert row.status == "waiting_plan_review"
            result = row.result or {}
            assert "当前 14 天来自两个串行 7 天门槛" in result.get("plan_markdown", "")
            assert "cronjobs/sync_tracking_status.py" in captured_plan_args.get("code_exploration", "")
            assert "hongguo-meta-ingest" in captured_plan_args.get("code_exploration", "")

            artifact = db.query(CodingArtifactModel).filter_by(task_id=task_id, artifact_type="code_exploration").first()
            assert artifact is not None
            assert "sync_tracking_status.py" in artifact.content
            assert any(event[0][1] == "code_exploration_completed" for event in emitted_events), "应记录代码探索完成事件"
            assert any(event[0][1] == "plan_generated" for event in emitted_events), "应生成 Plan 事件"
        finally:
            db.close()
    finally:
        coding_service.SessionLocal = original_session
        coding_service.emit_event = original_emit
        coding_service.build_system_prompt = original_build_system_prompt
        coding_service.run_explorer = original_run_explorer
        coding_service.run_coding_clarification = original_run_coding_clarification
        coding_service.run_coding_plan = original_run_coding_plan


@_case("CodingRuntime: check_syntax 固定语法检查工具")
def test_coding_runtime_check_syntax():
    from tempfile import TemporaryDirectory

    from core.coding_policy import CodingPolicy
    from core.coding_runtime import CodingRuntime, _detect_syntax_language

    with TemporaryDirectory() as tmp:
        ws = Path(tmp)
        (ws / "ok.py").write_text("value = 1\n", encoding="utf-8")
        (ws / "bad.py").write_text("def broken(:\n    pass\n", encoding="utf-8")

        runtime = CodingRuntime(ws, CodingPolicy())
        ok = runtime.check_syntax("ok.py")
        assert ok["ok"] is True, ok
        assert ok["language"] == "python"
        assert not (ws / "__pycache__").exists(), "check_syntax 不应在 workspace 写 __pycache__"

        bad = runtime.check_syntax("bad.py")
        assert bad["ok"] is False, bad
        assert bad["diagnostics"], bad
        assert bad["diagnostics"][0].get("line") == 1, bad

        blocked = runtime.run_command("python -m py_compile ok.py")
        assert "error" in blocked and "allowed_commands" in blocked["error"], blocked

        assert _detect_syntax_language("src/Main.java") == "java"
        assert _detect_syntax_language("src/app.js") == "javascript"
        assert _detect_syntax_language("src/app.mjs") == "javascript"
        assert _detect_syntax_language("ok.py", "python") == "python"
        assert _detect_syntax_language("ok.py", "Python") == "python"
        assert _detect_syntax_language("ok.py", "python\u200b") == "python"
        assert _detect_syntax_language("ok.py", "python.") == "python"
        assert _detect_syntax_language("ok.py", "not-real") == "python"
        assert "check_syntax" in [tool.name for tool in runtime.tools()]


@_case("Coding: MR description 包含审核摘要")
def test_coding_mr_description_summary():
    from core.coding_service import _build_mr_description

    body = _build_mr_description(
        task_id="ct_review_summary_test",
        project_id="demo",
        requirement="修复 waiting_code_review 页面仍展示 Plan 的流程缺陷",
        approved_plan="## Plan\n- 切换代码审核视图\n- 展示 MR 链接",
        summary="已新增 Code Review 面板，并让 MR description 说明本次改动原因和内容。",
        files=["src/pages/CodingPage.tsx", "core/coding_service.py"],
        test_results={"git_status": "M src/pages/CodingPage.tsx", "edit_round": 1},
        risks=[{"severity": "warning", "file": "src/pages/CodingPage.tsx", "message": "需要人工确认页面空态"}],
        report_url="/reports/coding/ct_review_summary_test",
    )

    assert "## 为什么改" in body, body
    assert "流程缺陷" in body, body
    assert "## 已审核 Plan 摘要" in body, body
    assert "切换代码审核视图" in body, body
    assert "## 本次实际改动" in body, body
    assert "MR description" in body, body
    assert "`src/pages/CodingPage.tsx`" in body, body
    assert "## 自动校验" in body, body
    assert "## 风险与 Review 建议" in body, body
    assert "/reports/coding/ct_review_summary_test" in body, body
    assert "/coding?task_id=ct_review_summary_test&project_id=demo" in body, body


@_case("Coding: commit_all 注入 git 作者身份")
def test_coding_commit_all_uses_configured_identity():
    import os
    from tempfile import TemporaryDirectory

    from core.coding_workspace import commit_all, run_git

    tracked_keys = [
        "HOME",
        "XDG_CONFIG_HOME",
        "GIT_CONFIG_GLOBAL",
        "GIT_CONFIG_NOSYSTEM",
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
    ]
    previous_env = {key: os.environ.get(key) for key in tracked_keys}
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        isolated_home = root / "home"
        isolated_config = root / "config"
        isolated_home.mkdir()
        isolated_config.mkdir()
        os.environ["HOME"] = str(isolated_home)
        os.environ["XDG_CONFIG_HOME"] = str(isolated_config)
        os.environ["GIT_CONFIG_GLOBAL"] = str(root / "missing-global-gitconfig")
        os.environ["GIT_CONFIG_NOSYSTEM"] = "1"
        for key in ["GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"]:
            os.environ.pop(key, None)
        try:
            repo = root / "repo"
            repo.mkdir()
            run_git(["init"], cwd=repo, timeout=30)
            (repo / "README.md").write_text("hello\n", encoding="utf-8")

            head = commit_all(repo, "test commit identity")

            assert head, "commit_all 应返回 head commit"
            author = run_git(["log", "-1", "--format=%an <%ae>"], cwd=repo, timeout=30)
            committer = run_git(["log", "-1", "--format=%cn <%ce>"], cwd=repo, timeout=30)
            expected = "Viktor Coding Agent <viktor-coding-agent@example.com>"
            assert author == expected, author
            assert committer == expected, committer
        finally:
            for key, value in previous_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value


@_case("Coding: execution result merge 不覆盖 Plan/澄清")
def test_coding_execution_result_merge_preserves_review_context():
    from core.coding_service import _approved_plan_markdown, _merge_execution_result

    existing = {
        "plan_markdown": "## Plan\n- keep this",
        "plan_questions": [{"id": "scope"}],
        "clarification": {"status": "answered", "answers": {"scope": "api"}},
        "plan_review": {"decision": "approved"},
        "execution": {"previous": True},
    }
    execution = {
        "changed_files": ["core/coding_service.py"],
        "report_url": "/reports/coding/ct_merge",
        "mr": {"url": "https://gitlab.example/mr/1"},
    }

    merged = _merge_execution_result(existing, execution)
    assert merged["plan_markdown"] == existing["plan_markdown"]
    assert merged["plan_questions"] == existing["plan_questions"]
    assert merged["clarification"] == existing["clarification"]
    assert merged["execution"]["previous"] is True
    assert merged["execution"]["changed_files"] == execution["changed_files"]
    assert merged["changed_files"] == execution["changed_files"]
    assert _approved_plan_markdown(merged) == existing["plan_markdown"]
    assert _approved_plan_markdown({"plan_markdown": "draft"}) == ""
    print("  execution nested merge / approved plan fallback OK")


@_case("Coding: continue_execution 可复用已审核 Plan")
def test_coding_continue_execution_uses_reviewed_plan():
    from core import coding_service

    calls = {"appended": [], "started": []}
    original_get_task = coding_service.get_task
    original_append_message = coding_service.append_message
    original_emit_event = coding_service.emit_event
    original_start_execution = coding_service.start_execution
    original_store_continuation = coding_service._store_continuation_instruction

    def _fake_get_task(task_id: str):
        return {
            "task_id": task_id,
            "status": "waiting_code_review",
            "result": {
                "plan_markdown": "## Plan\n- approved",
                "plan_review": {"decision": "approved"},
            },
        }

    def _fake_append_message(task_id: str, message: str):
        calls["appended"].append((task_id, message))

    def _fake_start_execution(task_id: str):
        calls["started"].append(task_id)
        return {"task_id": task_id, "status": "running"}

    try:
        coding_service.get_task = _fake_get_task
        coding_service.append_message = _fake_append_message
        coding_service.emit_event = lambda *args, **kwargs: None
        coding_service.start_execution = _fake_start_execution
        coding_service._store_continuation_instruction = lambda *args, **kwargs: None
        task = coding_service.continue_execution("ct_continue_reviewed_plan", "补一轮验证")
        assert task["status"] == "running"
        assert calls["appended"] == [("ct_continue_reviewed_plan", "补一轮验证")]
        assert calls["started"] == ["ct_continue_reviewed_plan"]
    finally:
        coding_service.get_task = original_get_task
        coding_service.append_message = original_append_message
        coding_service.emit_event = original_emit_event
        coding_service.start_execution = original_start_execution
        coding_service._store_continuation_instruction = original_store_continuation
    print("  waiting_code_review 可继续执行，先校验状态再追加消息")


@_case("Watchdog: CodingTask 通知文案按状态区分")
def test_watchdog_coding_task_notice_states():
    from core.watchdog_notifier import build_watchdog_markdown

    waiting_plan = build_watchdog_markdown(
        watchdog_name="订单异常",
        project_id=FAKE_PROJECT_ID,
        severity="warning",
        conclusion="发现异常",
        coding_task_id="ct_plan",
        coding_task_status="waiting_plan_review",
    )["text"]
    waiting_clarification = build_watchdog_markdown(
        watchdog_name="订单异常",
        project_id=FAKE_PROJECT_ID,
        severity="warning",
        conclusion="发现异常",
        coding_task_id="ct_clarify",
        coding_task_status="waiting_clarification",
    )["text"]
    failed = build_watchdog_markdown(
        watchdog_name="订单异常",
        project_id=FAKE_PROJECT_ID,
        severity="warning",
        conclusion="发现异常",
        coding_task_id="ct_failed",
        coding_task_status="failed",
        coding_task_message="Plan 生成失败",
    )["text"]
    timeout = build_watchdog_markdown(
        watchdog_name="订单异常",
        project_id=FAKE_PROJECT_ID,
        severity="warning",
        conclusion="发现异常",
        coding_task_id="ct_timeout",
        coding_task_status="timeout",
        coding_task_stage="drafting_plan",
    )["text"]

    assert "等待人工审核" in waiting_plan
    assert "需要先回答澄清问题" in waiting_clarification
    assert "未能生成可审核 Plan" in failed and "Plan 生成失败" in failed
    assert "等待 Plan 生成超时" in timeout and "drafting_plan" in timeout
    print("  waiting_plan_review / waiting_clarification / failed / timeout 文案 OK")


@_case("Issue Intake: /report_bug 指令解析")
def test_issue_intake_report_bug_command():
    from core.chat_commands import parse_report_bug_command

    cmd, err = parse_report_bug_command("/report_bug --repo order-api --branch release/2026-06 复现步骤：点击保存，实际结果 500，期望成功")
    assert err is None, err
    assert cmd is not None
    assert cmd.repo_connector_id == "order-api"
    assert cmd.target_branch == "release/2026-06"
    assert "实际结果" in cmd.description

    cmd, err = parse_report_bug_command("/report_bug")
    assert cmd is None
    assert err and "描述" in err
    print("  /report_bug repo 参数和缺描述错误 OK")


@_case("Issue Intake: label 与模板 metadata 生成")
def test_issue_intake_labels_and_description():
    from core.dingtalk_notifier import build_at_block, build_markdown_payload, build_signed_url
    from core.issue_intake_service import (
        _build_link_notification_text,
        _build_issue_description,
        _coding_task_markdown,
        _issue_labels,
        _metadata_from_issue,
        _normalize_target_payloads,
        _validate_issue,
    )
    from gitlab.service import GitLabClient

    registry.register_repository_connector(RepositoryConnectorItem(
        id="api",
        project_id=FAKE_PROJECT_ID,
        display_name="API",
        git_url="https://gitlab.example.com/demo/api.git",
    ))

    labels = _issue_labels(["viktor:auto"], [], "bug", "api", "web_chat")
    assert "viktor:auto" in labels
    assert "type:bug" in labels
    assert "repo:api" in labels
    assert "source:web_chat" in labels

    description = _build_issue_description(
        project_id="demo",
        repo_connector_id="api",
        kind="bug",
        reporter="Alice",
        source="web_chat",
        target_branch="release/2026-06",
        description="## 复现步骤\n1. 点击保存",
        attachments=[{"filename": "log.txt", "download_url": "https://example/log.txt", "extracted_preview": "stack"}],
    )
    assert "viktor-issue-intake" in description
    assert "log.txt" in description
    metadata = _metadata_from_issue({"description": description})
    assert metadata["submitter_display_name"] == "Alice"
    assert metadata["target_branch"] == "release/2026-06"
    assert GitLabClient.extract_project_path("https://gitlab.example.com/demo/api/-/issues/42") == "demo/api"
    assert "[ct_demo](" in _coding_task_markdown("ct_demo", "demo")
    assert "/coding?task_id=ct_demo&project_id=demo" in _coding_task_markdown("ct_demo", "demo")

    targets = _normalize_target_payloads(
        project_id=FAKE_PROJECT_ID,
        targets=[{"repo_connector_id": "api", "issue_project_url": "https://gitlab.example.com/demo/api", "labels": ["team:api"]}],
        default_repo_connector_id="api",
        legacy_issue_project_url="",
    )
    assert targets[0]["repo_connector_id"] == "api"
    assert targets[0]["issue_project_url"].endswith("/demo/api")
    assert targets[0]["labels"] == ["team:api"]

    signed = build_signed_url("https://oapi.dingtalk.com/robot/send?access_token=x", "secret")
    assert "timestamp=" in signed and "sign=" in signed
    assert build_at_block(["13800000000", "13800000000"], at_all=False)["atMobiles"] == ["13800000000"]
    payload = build_markdown_payload(title="T", text="body", at_mobiles=["13800000000"])
    assert payload["msgtype"] == "markdown" and payload["at"]["atMobiles"] == ["13800000000"]

    class Link:
        project_id = "demo"
        repo_connector_id = "api"
        coding_task_id = "ct_123"
        link_id = "iil_123"
        issue_url = "https://gitlab.example.com/demo/api/-/issues/42"

    notification_text = _build_link_notification_text(Link, "Viktor 已创建 MR", "- MR: https://example/mr/1")
    assert "/coding?task_id=ct_123&project_id=demo" in notification_text
    assert "- Issue: https://gitlab.example.com/demo/api/-/issues/42" in notification_text

    missing = _validate_issue({"title": "Bug", "description": "body", "labels": labels})
    assert missing == [], missing
    print("  labels / metadata / repo target / dingtalk payload 校验 OK")


# ============================================================
# C. 三件套只读工具
# ============================================================

@_case("code_glob: 通配符 + .gitignore 生效")
def test_code_glob():
    from tools.code_inspector import code_glob

    res = code_glob(FAKE_PROJECT_ID, "core/*.py", max_results=50)
    assert "error" not in res, res
    files = res["files"]
    assert any(p.endswith("code_sync.py") for p in files), f"未命中 code_sync.py: {files[:5]}"
    assert not any(".venv" in p or "__pycache__" in p for p in files), "未过滤忽略目录"
    print(f"  命中 {res['count']} 个文件")


@_case("code_grep: 正则 + fuzzy + 无结果处理")
def test_code_grep():
    from tools.code_inspector import code_grep

    # 1. 常规命中
    hit = code_grep(FAKE_PROJECT_ID, r"def ensure_workspace", path="core/",
                    ignore_case=False, max_results=5)
    assert "error" not in hit and hit["count"] >= 1, hit
    assert any("code_sync.py" in h["file"] for h in hit["hits"])
    print(f"  常规命中 {hit['count']} 条，首条: {hit['hits'][0]['file']}:{hit['hits'][0]['line']}")

    # 2. fuzzy=True 拆 CamelCase 命中 snake_case（ensureWorkspace → ensure|Workspace）
    fuz = code_grep(FAKE_PROJECT_ID, "ensureWorkspace", fuzzy=True, max_results=5)
    assert "error" not in fuz, fuz
    assert fuz["count"] >= 1, f"fuzzy 未命中: {fuz}"
    print(f"  fuzzy 展开后 pattern={fuz['effective_pattern']!r}, 命中 {fuz['count']} 条")

    # 3. 0 结果不 crash（用拼接避免测试脚本自己被 grep 命中）
    needle = "xq9k" + "pjvm" + "7z_no_match_" + "7pqb4"
    miss = code_grep(FAKE_PROJECT_ID, needle)
    assert "error" not in miss, f"miss 意外报错: {miss}"
    assert miss["count"] == 0, f"miss 应为 0 命中，实际 count={miss['count']} hits={miss.get('hits', [])[:2]}"
    print(f"  空结果测试通过，effective_pattern={miss.get('effective_pattern')!r}")


@_case("code_read: 正常读取 + 行数上限 + 路径穿越防护")
def test_code_read():
    from tools.code_inspector import code_read

    r = code_read(FAKE_PROJECT_ID, "core/code_sync.py", start_line=1, end_line=20)
    assert "error" not in r, r
    assert "代码自省" in r["content"], "头部内容对不上"
    assert r["end_line"] <= 20
    print(f"  读取 {r['path']} [{r['start_line']}-{r['end_line']}], 总行数 {r['total_lines']}")

    # 超量请求：应截断到 _MAX_READ_LINES，不报错
    big = code_read(FAKE_PROJECT_ID, "core/code_sync.py", start_line=1, end_line=99999)
    assert "error" not in big, big
    assert big["truncated"], "超量请求应标记 truncated"

    # 路径穿越：../ 必须被拦截（raise ValueError）
    try:
        code_read(FAKE_PROJECT_ID, "../etc/passwd")
        raise AssertionError("路径穿越未拦截！")
    except ValueError as e:
        assert "越出 workspace" in str(e), f"错误信息不对: {e}"
        print(f"  路径穿越已拦截: {e}")


# ============================================================
# D. 代码同步（online 可选）
# ============================================================

@_case("[online] resolve_live_commit + ensure_workspace 真实 clone")
def test_online_sync(project_id: str):
    from core.code_sync import ensure_workspace, resolve_live_commit

    proj = registry.get_project(project_id)
    assert proj, f"项目 {project_id} 未注册（先在 Admin 填好 git_url + k8s_workload）"
    assert proj.git_url, "git_url 为空"

    resolution = resolve_live_commit(proj)
    print(f"  解析结果 source={resolution.source} sha={resolution.short()} image={resolution.raw_image}")
    assert len(resolution.sha) >= 7

    ws = ensure_workspace(project_id)
    print(f"  workspace 路径: {ws}")
    assert ws.is_dir() and (ws / ".git").exists(), "workspace 不是 git 仓库"


# ============================================================
# E. Explorer sub-agent（可选）
# ============================================================

@_case("[explorer] code_explore 跑通一次")
def test_explorer(task: str):
    import os
    assert os.getenv("DEEPSEEK_API_KEY"), "未设置 DEEPSEEK_API_KEY"

    from core.explorer_agent import run_explorer
    result = run_explorer(FAKE_PROJECT_ID, task)
    print(f"  summary 长度: {len(result.summary)}")
    print(f"  相关文件: {len(result.relevant_files)} 条")
    print(f"  命中关键词: {result.searched_keywords[:5]}")
    assert result.summary, "summary 为空"


# ============================================================
# 入口
# ============================================================

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--online", action="store_true", help="启用 K8s→commit→clone 真实链路")
    parser.add_argument("--explorer", action="store_true", help="启用 explorer sub-agent 测试")
    parser.add_argument("--project", default=FAKE_PROJECT_ID, help="online/explorer 模式下的真实 project_id")
    parser.add_argument("--task", default="ensure_workspace 的实现位置和逻辑", help="explorer 任务描述")
    args = parser.parse_args()

    logger.remove()
    logger.add(sys.stderr, level="INFO",
               format="<dim>{time:HH:mm:ss}</dim> <level>{level: <5}</level> {message}")

    # 永远先跑离线套件
    _seed_fake_project()
    _patch_ensure_workspace_to_self()

    test_registry_and_glossary()
    test_registry_persistence_roundtrip()
    test_prompt_injection()
    test_subsystem_router_uses_registered_contexts()
    test_coding_plan_revision_request()
    test_coding_planning_uses_code_exploration()
    test_coding_runtime_check_syntax()
    test_coding_mr_description_summary()
    test_coding_commit_all_uses_configured_identity()
    test_coding_execution_result_merge_preserves_review_context()
    test_coding_continue_execution_uses_reviewed_plan()
    test_watchdog_coding_task_notice_states()
    test_issue_intake_report_bug_command()
    test_issue_intake_labels_and_description()
    test_code_glob()
    test_code_grep()
    test_code_read()

    # 可选在线链路：此时恢复真实 ensure_workspace
    if args.online:
        import importlib
        import core.code_sync as cs
        import tools.code_inspector as ci
        importlib.reload(cs)
        ci.ensure_workspace = cs.ensure_workspace
        test_online_sync(args.project)

    if args.explorer:
        test_explorer(args.task)

    # 清理
    registry.unregister_project(FAKE_PROJECT_ID)

    # 汇总
    print(f"\n{'='*60}\n汇总\n{'='*60}")
    passed = sum(1 for _, ok, _ in _results if ok)
    total = len(_results)
    for name, ok, err in _results:
        icon = f"{GREEN}✓{RESET}" if ok else f"{RED}✗{RESET}"
        line = f"  {icon} {name}"
        if err:
            line += f"  {YELLOW}[{err}]{RESET}"
        print(line)
    print(f"\n结果: {passed}/{total} 通过")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
