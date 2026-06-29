"""StagingRunWorkflow 测试：单测试环境锁、Playwright 失败批量反馈、通过放行。"""
import uuid

import pytest
from temporalio import activity
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from workflows.staging_acceptance_workflow import StagingRunWorkflow


class FakeStaging:
    def __init__(self, playwright_status: str = "passed", restore_fails: bool = False) -> None:
        self.playwright_status = playwright_status
        self.restore_fails = restore_fails
        self.calls: list[str] = []


_B: FakeStaging


@activity.defn
async def acquire_staging_lock(run_id: str, env_id: str, lease_owner: str) -> bool:
    _B.calls.append("lock")
    return True


@activity.defn
async def heartbeat_staging_lock(run_id: str, env_id: str) -> bool:
    _B.calls.append("heartbeat")
    return True


@activity.defn
async def release_staging_lock(run_id: str, env_id: str) -> bool:
    _B.calls.append("release")
    return True


@activity.defn
async def hold_staging_lock_for_manual_intervention(run_id: str, env_id: str, reason: str) -> bool:
    _B.calls.append("hold")
    return True


@activity.defn
async def refresh_staging_candidates(run_id: str) -> dict:
    _B.calls.append("refresh")
    return {"fresh": True, "superseded": False}


@activity.defn
async def integrate_staging_to_dev(run_id: str) -> dict:
    _B.calls.append("integrate")
    return {"repos": [{"repo_connector_id": "api"}]}


@activity.defn
async def wait_staging_deploy(run_id: str) -> dict:
    _B.calls.append("deploy")
    return {"ok": True}


@activity.defn
async def run_staging_playwright(run_id: str) -> dict:
    _B.calls.append("playwright")
    return {"status": _B.playwright_status, "cases": [{"id": "case_1", "status": _B.playwright_status}]}


@activity.defn
async def create_staging_feedback(run_id: str, reason: str, test_result: dict | None = None) -> dict:
    _B.calls.append("feedback")
    return {"feedback_issue_url": "http://gitlab/issues/1"}


@activity.defn
async def restore_staging_dev(run_id: str) -> dict:
    _B.calls.append("restore")
    if _B.restore_fails:
        raise ApplicationError("restore failed", type="StagingInfraFailure", non_retryable=True)
    return {"ok": True}


@activity.defn
async def mark_staging_failed_activity(run_id: str, reason: str, business: bool, test_result: dict | None = None) -> dict:
    _B.calls.append(f"failed:{business}")
    return {"status": "test_failed" if business else "infra_failed"}


@activity.defn
async def mark_staging_passed_activity(run_id: str, test_result: dict | None = None) -> dict:
    _B.calls.append("passed")
    return {"status": "passed"}


_ACTIVITIES = [
    acquire_staging_lock,
    heartbeat_staging_lock,
    release_staging_lock,
    hold_staging_lock_for_manual_intervention,
    refresh_staging_candidates,
    integrate_staging_to_dev,
    wait_staging_deploy,
    run_staging_playwright,
    create_staging_feedback,
    restore_staging_dev,
    mark_staging_failed_activity,
    mark_staging_passed_activity,
]


async def _run(playwright_status: str, *, restore_fails: bool = False) -> tuple[dict, list[str]]:
    global _B
    _B = FakeStaging(playwright_status, restore_fails=restore_fails)
    tq = f"staging-test-{uuid.uuid4()}"
    async with await WorkflowEnvironment.start_time_skipping() as env:
        async with Worker(env.client, task_queue=tq, workflows=[StagingRunWorkflow], activities=_ACTIVITIES):
            result = await env.client.execute_workflow(
                StagingRunWorkflow.run,
                args=["sr_test", "default-staging"],
                id=f"sr_{uuid.uuid4()}",
                task_queue=tq,
            )
    return result, list(_B.calls)


@pytest.mark.asyncio
async def test_staging_run_passes_and_releases_lock():
    result, calls = await _run("passed")
    assert result["result"] == "passed"
    assert calls == ["lock", "refresh", "heartbeat", "integrate", "heartbeat", "deploy", "playwright", "passed", "release"]


@pytest.mark.asyncio
async def test_staging_run_failed_playwright_batches_feedback_and_restores_dev():
    result, calls = await _run("failed")
    assert result["result"] == "test_failed"
    assert "feedback" in calls
    assert "restore" in calls
    assert "failed:True" in calls
    assert calls[-1] == "release"


@pytest.mark.asyncio
async def test_staging_run_restore_failure_holds_lock_for_manual_intervention():
    result, calls = await _run("failed", restore_fails=True)
    assert result["result"] == "infra_failed"
    assert "feedback" in calls
    assert "restore" in calls
    assert "failed:False" in calls
    assert "hold" in calls
    assert "release" not in calls


def test_restore_dev_after_failure_reverts_merge_commit_with_mainline(monkeypatch, tmp_path):
    from core import coding_workspace
    from core import staging_acceptance_service as svc

    run = {
        "deploy_payload": {
            "repos": [{
                "repo_url": "https://gitlab.example.com/group/repo.git",
                "repo_connector_id": "api",
                "dev_base_sha": "base_sha",
                "dev_deploy_sha": "deploy_sha",
            }]
        }
    }
    calls: list[list[str]] = []

    def fake_git(args, cwd=None, timeout=300, check=True):
        calls.append(args)
        if args[:3] == ["rev-list", "--parents", "-n"]:
            return "deploy_sha parent_one parent_two"
        return ""

    monkeypatch.setattr(svc, "get_staging_run", lambda run_id: run)
    monkeypatch.setattr(svc, "emit_staging_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(svc, "_git", fake_git)
    monkeypatch.setattr(coding_workspace, "inject_git_credentials", lambda url: url)
    monkeypatch.setattr(svc.staging_acceptance_config, "workspace_root", str(tmp_path))
    monkeypatch.setattr(svc.staging_acceptance_config, "deploy_branch", "dev")
    monkeypatch.setattr(svc.staging_acceptance_config, "restore_strategy", "revert")

    result = svc.restore_dev_after_failure("sr_merge")

    assert result["ok"] is True
    assert ["revert", "-m", "1", "--no-edit", "deploy_sha"] in calls
    assert ["push", "origin", "HEAD:dev"] in calls
