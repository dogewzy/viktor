"""单测试环境 staging 验收服务。

DB 是读模型和恢复锚点；Temporal workflow 是唯一编排者。这里的函数保持幂等，
供 activities / webhook / API 复用。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from loguru import logger
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from core.database import SessionLocal
from core.models import (
    CodingTaskModel,
    IssueIntakeLinkModel,
    StagingEventModel,
    StagingLockModel,
    StagingRunModel,
)
from settings import coding_agent_config, report_config, staging_acceptance_config


RUN_FINAL = {"passed", "test_failed", "infra_failed", "cancelled", "superseded"}
RUN_ACTIVE = {"queued", "lock_waiting", "locked", "deploying", "testing", "feedback_created", "fixing"}
LOCK_MANUAL_STATUS = "manual_intervention"


class StagingBusinessFailure(RuntimeError):
    """业务/集成层失败，应反馈给原始 MR 分支继续修复。"""


class StagingInfraFailure(RuntimeError):
    """基础设施失败，不应回炉给 Agent。"""


@dataclass
class Candidate:
    task_id: str
    repo_connector_id: str
    repo_url: str
    source_branch: str
    target_branch: str
    mr_url: str
    mr_iid: str
    head_sha: str


def _new_id(prefix: str = "sr") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _now() -> datetime:
    return datetime.now()


def _run_to_dict(row: StagingRunModel) -> dict[str, Any]:
    return {
        "run_id": row.run_id,
        "env_id": row.env_id,
        "link_id": row.link_id,
        "project_id": row.project_id,
        "status": row.status,
        "stage": row.stage,
        "message": row.message,
        "commit_fingerprint": row.commit_fingerprint,
        "dev_base_sha": row.dev_base_sha,
        "dev_deploy_sha": row.dev_deploy_sha,
        "candidate_shas": row.candidate_shas or {},
        "task_ids": row.task_ids or [],
        "mr_urls": row.mr_urls or [],
        "branches": row.branches or {},
        "test_plan": row.test_plan or {},
        "test_result": row.test_result or {},
        "deploy_payload": row.deploy_payload or {},
        "feedback_issue_url": row.feedback_issue_url,
        "report_url": row.report_url,
        "last_error": row.last_error,
        "retry_count": row.retry_count,
        "created_at": row.created_at.isoformat() if row.created_at else "",
        "updated_at": row.updated_at.isoformat() if row.updated_at else "",
        "started_at": row.started_at.isoformat() if row.started_at else "",
        "finished_at": row.finished_at.isoformat() if row.finished_at else "",
    }


def emit_staging_event(run_id: str, event_type: str, message: str, payload: dict[str, Any] | None = None, *, stage: str = "") -> None:
    db = SessionLocal()
    try:
        seq = (
            db.query(func.max(StagingEventModel.seq))
            .filter(StagingEventModel.run_id == run_id)
            .scalar()
            or 0
        ) + 1
        db.add(StagingEventModel(
            run_id=run_id,
            seq=seq,
            event_type=event_type,
            stage=stage,
            message=message,
            payload=payload or {},
        ))
        db.commit()
    finally:
        db.close()


def _set_run(
    run_id: str,
    *,
    status: str | None = None,
    stage: str | None = None,
    message: str | None = None,
    last_error: str | None = None,
    test_result: dict[str, Any] | None = None,
    deploy_payload: dict[str, Any] | None = None,
    feedback_issue_url: str | None = None,
    report_url: str | None = None,
    dev_base_sha: str | None = None,
    dev_deploy_sha: str | None = None,
    finished: bool = False,
) -> dict[str, Any]:
    db = SessionLocal()
    try:
        row = db.get(StagingRunModel, run_id)
        if not row:
            raise ValueError("staging run 不存在")
        if status is not None:
            row.status = status
        if stage is not None:
            row.stage = stage
        if message is not None:
            row.message = message
        if last_error is not None:
            row.last_error = last_error
        if test_result is not None:
            row.test_result = test_result
        if deploy_payload is not None:
            row.deploy_payload = deploy_payload
        if feedback_issue_url is not None:
            row.feedback_issue_url = feedback_issue_url
        if report_url is not None:
            row.report_url = report_url
        if dev_base_sha is not None:
            row.dev_base_sha = dev_base_sha
        if dev_deploy_sha is not None:
            row.dev_deploy_sha = dev_deploy_sha
        if status in {"locked", "deploying", "testing"} and not row.started_at:
            row.started_at = _now()
        if finished:
            row.finished_at = _now()
        row.updated_at = _now()
        db.commit()
        data = _run_to_dict(row)
    finally:
        db.close()
    return data


def list_staging_runs(
    *,
    project_id: str | None = None,
    link_id: str | None = None,
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> dict[str, Any]:
    db = SessionLocal()
    try:
        query = db.query(StagingRunModel)
        if project_id:
            query = query.filter(StagingRunModel.project_id == project_id)
        if link_id:
            query = query.filter(StagingRunModel.link_id == link_id)
        if status:
            query = query.filter(StagingRunModel.status == status)
        total = query.count()
        rows = query.order_by(StagingRunModel.created_at.desc()).offset(offset).limit(limit).all()
        return {"items": [_run_to_dict(r) for r in rows], "total": total, "limit": limit, "offset": offset}
    finally:
        db.close()


def get_staging_run(run_id: str) -> dict[str, Any] | None:
    db = SessionLocal()
    try:
        row = db.get(StagingRunModel, run_id)
        return _run_to_dict(row) if row else None
    finally:
        db.close()


def list_staging_events(run_id: str, *, after_seq: int = 0, limit: int = 200) -> dict[str, Any]:
    db = SessionLocal()
    try:
        rows = (
            db.query(StagingEventModel)
            .filter(StagingEventModel.run_id == run_id)
            .filter(StagingEventModel.seq > after_seq)
            .order_by(StagingEventModel.seq.asc())
            .limit(limit)
            .all()
        )
        return {
            "items": [
                {
                    "id": r.id,
                    "run_id": r.run_id,
                    "seq": r.seq,
                    "event_type": r.event_type,
                    "stage": r.stage,
                    "message": r.message,
                    "payload": r.payload or {},
                    "created_at": r.created_at.isoformat() if r.created_at else "",
                }
                for r in rows
            ]
        }
    finally:
        db.close()


def _task_candidates(link: IssueIntakeLinkModel) -> list[Candidate]:
    from core.coding_service import _resolve_repo
    from gitlab.merge_request_service import get_merge_request

    result = _as_dict(link.result)
    tasks = _as_list(result.get("coding_tasks"))
    candidates: list[Candidate] = []
    db = SessionLocal()
    try:
        for item in tasks:
            if not isinstance(item, dict):
                continue
            task_id = str(item.get("coding_task_id") or "").strip()
            if not task_id:
                continue
            task = db.get(CodingTaskModel, task_id)
            if not task:
                continue
            task_result = _as_dict(task.result)
            mr = _as_dict(task_result.get("mr"))
            mr_iid = str(mr.get("iid") or "").strip()
            mr_url = str(task.mr_url or item.get("mr_url") or task_result.get("mr_url") or "").strip()
            source_branch = str(task.work_branch or task_result.get("branch") or "").strip()
            head_sha = str(task_result.get("head_commit") or "").strip()
            repo_url, default_branch, resolved_repo = _resolve_repo(task.project_id, task.repo_connector_id)
            if mr_iid:
                try:
                    remote = get_merge_request(repo_url=repo_url, merge_request_iid=mr_iid)
                    source_branch = str(remote.get("source_branch") or source_branch)
                    head_sha = str(remote.get("sha") or remote.get("diff_refs", {}).get("head_sha") or head_sha)
                    mr_url = str(remote.get("web_url") or mr_url)
                except Exception as e:  # noqa: BLE001
                    logger.warning("[staging] 查询 MR head 失败 task={}: {}", task_id, e)
            if not (mr_iid and source_branch and head_sha):
                continue
            candidates.append(Candidate(
                task_id=task_id,
                repo_connector_id=resolved_repo or task.repo_connector_id,
                repo_url=repo_url,
                source_branch=source_branch,
                target_branch=task.target_branch or default_branch,
                mr_url=mr_url,
                mr_iid=mr_iid,
                head_sha=head_sha,
            ))
    finally:
        db.close()
    return candidates


def _fingerprint(candidates: list[Candidate]) -> str:
    material = [
        {
            "task_id": c.task_id,
            "mr_iid": c.mr_iid,
            "source_branch": c.source_branch,
            "head_sha": c.head_sha,
        }
        for c in sorted(candidates, key=lambda x: x.task_id)
    ]
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode("utf-8")).hexdigest()


def enqueue_staging_for_link(link_id: str) -> dict[str, Any] | None:
    """当 link 下全部 MR ready 后入 staging 队列；同 commit 指纹幂等复用。"""
    if not staging_acceptance_config.enabled:
        return None
    db = SessionLocal()
    try:
        link = db.get(IssueIntakeLinkModel, link_id)
        if not link:
            return None
        project_id = link.project_id
        result = _as_dict(link.result)
        tasks = _as_list(result.get("coding_tasks"))
        if not tasks:
            return None
        if any(not str(t.get("mr_url") or "").strip() for t in tasks if isinstance(t, dict)):
            return None
        test_plan = _as_dict(result.get("test_plan"))
        if not test_plan:
            test_plan = {
                "version": 1,
                "review_status": "implicit",
                "summary": "未声明测试计划；仅执行配置的 Playwright 验收命令。",
                "cases": [],
            }
        candidates = _task_candidates(link)
    finally:
        db.close()

    if not candidates:
        raise StagingInfraFailure("MR 信息不完整，无法创建 staging run")
    fp = _fingerprint(candidates)
    db = SessionLocal()
    try:
        existing = (
            db.query(StagingRunModel)
            .filter(StagingRunModel.link_id == link_id)
            .filter(StagingRunModel.commit_fingerprint == fp)
            .filter(StagingRunModel.status.in_(list(RUN_ACTIVE | {"passed"})))
            .order_by(StagingRunModel.created_at.desc())
            .first()
        )
        if existing:
            return _run_to_dict(existing)
        run_id = _new_id("sr")
        run = StagingRunModel(
            run_id=run_id,
            env_id=staging_acceptance_config.env_id,
            link_id=link_id,
            project_id=project_id,
            status="queued",
            stage="queued",
            message="Staging 验收已排队",
            commit_fingerprint=fp,
            candidate_shas={c.task_id: c.head_sha for c in candidates},
            task_ids=[c.task_id for c in candidates],
            mr_urls=[c.mr_url for c in candidates],
            branches={c.task_id: c.source_branch for c in candidates},
            test_plan=test_plan,
        )
        db.add(run)
        link_row = db.get(IssueIntakeLinkModel, link_id)
        if link_row:
            link_result = _as_dict(link_row.result)
            link_result["staging"] = {
                **_as_dict(link_result.get("staging")),
                "latest_run_id": run_id,
                "latest_status": "queued",
                "passed_commit_fingerprint": "",
            }
            link_row.result = link_result
        for c in candidates:
            task = db.get(CodingTaskModel, c.task_id)
            if task:
                tr = _as_dict(task.result)
                tr["staging"] = {
                    **_as_dict(tr.get("staging")),
                    "required": True,
                    "latest_run_id": run_id,
                    "latest_status": "queued",
                    "passed_commit_sha": "",
                }
                task.result = tr
        db.commit()
        data = _run_to_dict(run)
    finally:
        db.close()
    emit_staging_event(run_id, "queued", "Staging 验收已排队", {"link_id": link_id}, stage="queued")
    _notify_link(link_id, "Viktor Staging 已排队", f"- Run: `{run_id}`\n- 环境: `{staging_acceptance_config.env_id}`")
    try:
        from core.temporal import trigger
        if not trigger.start_staging_coordinator_sync(staging_acceptance_config.env_id):
            _set_run(
                run_id,
                status="infra_failed",
                stage="temporal_disabled",
                message="Staging 验收需要启用 Temporal 编排",
                last_error="TEMPORAL_ENABLED=false",
                finished=True,
            )
            emit_staging_event(run_id, "infra_failed", "Staging 验收需要启用 Temporal 编排", {}, stage="temporal_disabled")
    except Exception as e:  # noqa: BLE001
        logger.warning("[staging] 启动 coordinator 失败 run={}: {}", run_id, e)
    return data


def next_queued_run(env_id: str) -> dict[str, Any] | None:
    db = SessionLocal()
    try:
        row = (
            db.query(StagingRunModel)
            .filter(StagingRunModel.env_id == env_id)
            .filter(StagingRunModel.status == "queued")
            .order_by(StagingRunModel.created_at.asc())
            .first()
        )
        return _run_to_dict(row) if row else None
    finally:
        db.close()


def acquire_lock(run_id: str, env_id: str, lease_owner: str) -> bool:
    expires = _now() + timedelta(seconds=staging_acceptance_config.lock.lease_sec)
    db = SessionLocal()
    try:
        lock = db.get(StagingLockModel, env_id)
        if not lock:
            db.add(StagingLockModel(
                env_id=env_id,
                run_id=run_id,
                lease_owner=lease_owner,
                status="locked",
                heartbeat_at=_now(),
                expires_at=expires,
            ))
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                return False
            _set_run(run_id, status="locked", stage="locked", message="已锁定测试环境")
            emit_staging_event(run_id, "lock_acquired", "已锁定测试环境", {"env_id": env_id}, stage="locked")
            return True
        if lock.status == LOCK_MANUAL_STATUS:
            _set_run(run_id, status="lock_waiting", stage="lock_waiting", message=f"测试环境需要人工释放锁，当前占用 run={lock.run_id}")
            return False
        if lock.run_id == run_id:
            lock.lease_owner = lease_owner
            lock.heartbeat_at = _now()
            lock.expires_at = expires
            lock.updated_at = _now()
            db.commit()
            return True
        if lock.expires_at and lock.expires_at < _now():
            old_run = lock.run_id
            lock.run_id = run_id
            lock.lease_owner = lease_owner
            lock.status = "locked"
            lock.heartbeat_at = _now()
            lock.expires_at = expires
            lock.updated_at = _now()
            db.commit()
            if old_run:
                _set_run(old_run, status="infra_failed", stage="lock_expired", message="Staging 锁过期后被新 run 接管", finished=True)
                emit_staging_event(old_run, "lock_expired", "Staging 锁过期后被新 run 接管", {}, stage="lock_expired")
            _set_run(run_id, status="locked", stage="locked", message="已锁定测试环境")
            emit_staging_event(run_id, "lock_acquired", "已锁定测试环境", {"env_id": env_id}, stage="locked")
            return True
        _set_run(run_id, status="lock_waiting", stage="lock_waiting", message=f"测试环境被 {lock.run_id} 占用，等待释放")
        return False
    finally:
        db.close()


def heartbeat_lock(run_id: str, env_id: str) -> bool:
    db = SessionLocal()
    try:
        lock = db.get(StagingLockModel, env_id)
        if not lock or lock.run_id != run_id:
            return False
        lock.heartbeat_at = _now()
        lock.expires_at = _now() + timedelta(seconds=staging_acceptance_config.lock.lease_sec)
        lock.updated_at = _now()
        db.commit()
        return True
    finally:
        db.close()


def release_lock(run_id: str, env_id: str, *, force: bool = False) -> bool:
    db = SessionLocal()
    try:
        lock = db.get(StagingLockModel, env_id)
        if not lock:
            return False
        if lock.run_id != run_id and not force:
            return False
        db.delete(lock)
        db.commit()
    finally:
        db.close()
    emit_staging_event(run_id, "lock_released", "已释放测试环境锁", {"env_id": env_id, "force": force}, stage="lock_released")
    return True


def hold_lock_for_manual_intervention(run_id: str, env_id: str, reason: str) -> bool:
    db = SessionLocal()
    try:
        lock = db.get(StagingLockModel, env_id)
        if not lock or lock.run_id != run_id:
            return False
        lock.status = LOCK_MANUAL_STATUS
        lock.heartbeat_at = _now()
        lock.expires_at = None
        lock.updated_at = _now()
        db.commit()
    finally:
        db.close()
    _set_run(run_id, stage=LOCK_MANUAL_STATUS, message=reason, last_error=reason)
    emit_staging_event(
        run_id,
        LOCK_MANUAL_STATUS,
        "测试环境锁已转入人工介入状态",
        {"env_id": env_id, "reason": reason},
        stage=LOCK_MANUAL_STATUS,
    )
    run = get_staging_run(run_id)
    if run:
        _notify_link(
            str(run.get("link_id") or ""),
            "Viktor Staging 需要人工介入",
            f"- Run: `{run_id}`\n- 环境: `{env_id}`\n- 原因: {reason}",
        )
    return True


def reconcile_stale_staging_locks() -> dict[str, Any]:
    db = SessionLocal()
    released = 0
    try:
        rows = (
            db.query(StagingLockModel)
            .filter(StagingLockModel.status != LOCK_MANUAL_STATUS)
            .filter(StagingLockModel.expires_at < _now())
            .all()
        )
        for lock in rows:
            run_id = lock.run_id
            db.delete(lock)
            released += 1
            if run_id:
                row = db.get(StagingRunModel, run_id)
                if row and row.status not in RUN_FINAL:
                    row.status = "infra_failed"
                    row.stage = "lock_expired"
                    row.message = "Staging 锁租约过期，已释放"
                    row.finished_at = _now()
        db.commit()
    finally:
        db.close()
    return {"released": released}


def _notify_link(link_id: str, title: str, body: str, *, extra_mobiles: list[str] | None = None) -> None:
    try:
        from core.issue_intake_service import _notify_for_link
        _notify_for_link(link_id, title, body, extra_mobiles=extra_mobiles)
    except Exception as e:  # noqa: BLE001
        logger.warning("[staging] notify failed link={}: {}", link_id, e)


def _git(args: list[str], cwd: Path | None = None, timeout: int = 300, *, check: bool = True) -> str:
    env = os.environ.copy()
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    env.setdefault("GIT_ASKPASS", "/bin/true")
    env.setdefault("GIT_AUTHOR_NAME", coding_agent_config.git_author_name)
    env.setdefault("GIT_AUTHOR_EMAIL", coding_agent_config.git_author_email)
    env.setdefault("GIT_COMMITTER_NAME", coding_agent_config.git_author_name)
    env.setdefault("GIT_COMMITTER_EMAIL", coding_agent_config.git_author_email)
    res = subprocess.run(
        [coding_agent_config.git_binary, *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    if check and res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or res.stdout.strip() or f"git {args[0]} failed")
    return res.stdout.strip()


def _safe_path(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "-", value).strip("-") or "repo"


def _integration_workspace(run_id: str, candidate: Candidate) -> Path:
    root = Path(os.path.expanduser(staging_acceptance_config.workspace_root)).resolve()
    return root / _safe_path(run_id) / _safe_path(candidate.repo_connector_id)


def refresh_run_candidates(run_id: str) -> dict[str, Any]:
    run = get_staging_run(run_id)
    if not run:
        raise ValueError("staging run 不存在")
    db = SessionLocal()
    try:
        link = db.get(IssueIntakeLinkModel, run["link_id"])
        if not link:
            raise ValueError("Issue link 不存在")
    finally:
        db.close()
    latest = _task_candidates(link)
    latest_fp = _fingerprint(latest)
    if latest_fp != run["commit_fingerprint"]:
        _set_run(run_id, status="superseded", stage="superseded", message="MR head 已更新，本轮 staging run 已废弃", finished=True)
        emit_staging_event(run_id, "superseded", "MR head 已更新，本轮 staging run 已废弃", {
            "old": run["commit_fingerprint"],
            "new": latest_fp,
        }, stage="superseded")
        enqueue_staging_for_link(run["link_id"])
        return {"fresh": False, "superseded": True}
    return {"fresh": True, "superseded": False}


def integrate_candidates_to_dev(run_id: str) -> dict[str, Any]:
    """把候选 MR 分支 merge 到各仓库 dev 并 push，触发现有 CD。"""
    run = get_staging_run(run_id)
    if not run:
        raise ValueError("staging run 不存在")
    db = SessionLocal()
    try:
        link = db.get(IssueIntakeLinkModel, run["link_id"])
        if not link:
            raise ValueError("Issue link 不存在")
    finally:
        db.close()
    candidates = _task_candidates(link)
    deploy_branch = staging_acceptance_config.deploy_branch
    _set_run(run_id, status="deploying", stage="integrating_dev", message=f"正在把候选分支集成到 {deploy_branch}")
    emit_staging_event(run_id, "dev_integration_started", f"开始集成到 {deploy_branch}", {}, stage="integrating_dev")
    payload: dict[str, Any] = {"repos": []}
    primary_base = ""
    primary_deploy = ""
    try:
        from core.coding_workspace import inject_git_credentials
        for c in candidates:
            ws = _integration_workspace(run_id, c)
            if ws.exists():
                shutil.rmtree(ws, ignore_errors=True)
            ws.parent.mkdir(parents=True, exist_ok=True)
            _git(["clone", "--branch", deploy_branch, inject_git_credentials(c.repo_url), str(ws)], timeout=600)
            _git(["fetch", "origin", c.source_branch], cwd=ws, timeout=300)
            dev_base = _git(["rev-parse", "HEAD"], cwd=ws, timeout=30)
            merge_out = subprocess.run(
                [
                    coding_agent_config.git_binary,
                    "-c",
                    f"user.name={coding_agent_config.git_author_name}",
                    "-c",
                    f"user.email={coding_agent_config.git_author_email}",
                    "merge",
                    "--no-ff",
                    f"origin/{c.source_branch}",
                    "-m",
                    f"Viktor staging {run_id}: merge {c.source_branch}",
                ],
                cwd=str(ws),
                capture_output=True,
                text=True,
                timeout=300,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "/bin/true"},
            )
            if merge_out.returncode != 0:
                _git(["merge", "--abort"], cwd=ws, timeout=60, check=False)
                raise StagingBusinessFailure(
                    f"{c.repo_connector_id} 合并 {c.source_branch} 到 {deploy_branch} 冲突："
                    f"{(merge_out.stderr or merge_out.stdout).strip()[:1000]}"
                )
            dev_deploy = _git(["rev-parse", "HEAD"], cwd=ws, timeout=30)
            _git(["push", "origin", f"HEAD:{deploy_branch}"], cwd=ws, timeout=600)
            payload["repos"].append({
                "task_id": c.task_id,
                "repo_connector_id": c.repo_connector_id,
                "repo_url": c.repo_url,
                "source_branch": c.source_branch,
                "head_sha": c.head_sha,
                "dev_base_sha": dev_base,
                "dev_deploy_sha": dev_deploy,
            })
            primary_base = primary_base or dev_base
            primary_deploy = primary_deploy or dev_deploy
            _set_run(
                run_id,
                status="deploying",
                stage="dev_partial_pushed",
                message=f"已推送 {c.repo_connector_id} 到 {deploy_branch}",
                deploy_payload=payload,
                dev_base_sha=primary_base,
                dev_deploy_sha=primary_deploy,
            )
            emit_staging_event(run_id, "dev_partial_pushed", f"已推送 {c.repo_connector_id} 到 {deploy_branch}", payload, stage="dev_partial_pushed")
    except StagingBusinessFailure:
        raise
    except Exception as e:  # noqa: BLE001
        raise StagingInfraFailure(f"集成 dev 失败：{e}") from e
    _set_run(
        run_id,
        status="deploying",
        stage="dev_pushed",
        message=f"已推送 {deploy_branch}，等待测试环境部署",
        deploy_payload=payload,
        dev_base_sha=primary_base,
        dev_deploy_sha=primary_deploy,
    )
    emit_staging_event(run_id, "dev_pushed", f"已推送 {deploy_branch}", payload, stage="dev_pushed")
    return payload


def wait_for_deployment(run_id: str) -> dict[str, Any]:
    wait_sec = max(0, int(staging_acceptance_config.deploy.deploy_wait_sec))
    _set_run(run_id, status="deploying", stage="waiting_deploy", message=f"等待测试环境部署完成（{wait_sec}s）")
    emit_staging_event(run_id, "deploy_wait_started", "等待现有 dev CD 完成", {"wait_sec": wait_sec}, stage="waiting_deploy")
    if wait_sec:
        time.sleep(wait_sec)
    return {"ok": True, "wait_sec": wait_sec, "staging_url": staging_acceptance_config.staging_url}


def run_playwright_acceptance(run_id: str) -> dict[str, Any]:
    command = staging_acceptance_config.playwright.command.strip()
    run = get_staging_run(run_id)
    if not run:
        raise ValueError("staging run 不存在")
    _set_run(run_id, status="testing", stage="testing", message="正在执行 Playwright staging 验收")
    emit_staging_event(run_id, "playwright_started", "开始执行 Playwright staging 验收", {}, stage="testing")
    if not command:
        raise StagingInfraFailure("未配置 STAGING_ACCEPTANCE_PLAYWRIGHT_COMMAND，无法执行 staging 验收")
    artifacts_dir = Path(os.path.expanduser(staging_acceptance_config.workspace_root)).resolve() / _safe_path(run_id) / "artifacts"
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update({
        "VIKTOR_STAGING_RUN_ID": run_id,
        "VIKTOR_STAGING_URL": staging_acceptance_config.staging_url,
        "VIKTOR_STAGING_ARTIFACTS_DIR": str(artifacts_dir),
        "VIKTOR_STAGING_TEST_PLAN": json.dumps(run.get("test_plan") or {}, ensure_ascii=False),
    })
    res = subprocess.run(
        command,
        shell=True,
        cwd=str(artifacts_dir),
        env=env,
        capture_output=True,
        text=True,
        timeout=staging_acceptance_config.playwright.timeout_sec,
    )
    summary_path = artifacts_dir / "summary.json"
    summary: dict[str, Any] = {}
    if summary_path.exists():
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            summary = {"summary_parse_error": str(e)}
    status = str(summary.get("status") or ("passed" if res.returncode == 0 else "failed"))
    result = {
        "status": status,
        "returncode": res.returncode,
        "stdout": res.stdout[-8000:],
        "stderr": res.stderr[-8000:],
        "cases": summary.get("cases") or [],
        "manual_cases": summary.get("manual_cases") or [],
        "artifacts_dir": str(artifacts_dir),
        "report_url": summary.get("report_url") or "",
    }
    _set_run(run_id, test_result=result, report_url=str(result.get("report_url") or ""))
    emit_staging_event(run_id, "playwright_finished", f"Playwright 验收完成：{status}", result, stage="testing")
    return result


def _feedback_body(run: dict[str, Any], reason: str, test_result: dict[str, Any] | None = None) -> str:
    marker = f"<!-- viktor:staging-feedback:{run['link_id']} -->"
    cases = _as_list((test_result or {}).get("cases"))
    failed_cases = [c for c in cases if isinstance(c, dict) and str(c.get("status")) == "failed"]
    failed_lines = "\n".join(
        f"- [ ] `{c.get('id') or '-'}` {c.get('title') or ''}: {c.get('error') or ''}"
        for c in failed_cases
    ) or f"- [ ] {reason}"
    return f"""{marker}
# Staging 验收失败

- 原始 issue: {run.get('link_id')}
- Staging run: `{run.get('run_id')}`
- dev_base_sha: `{run.get('dev_base_sha') or '-'}`
- dev_deploy_sha: `{run.get('dev_deploy_sha') or '-'}`
- candidate_shas: `{json.dumps(run.get('candidate_shas') or {}, ensure_ascii=False)}`
- MR: {', '.join(run.get('mr_urls') or []) or '-'}
- Report: {run.get('report_url') or (test_result or {}).get('report_url') or '-'}

## 失败项
{failed_lines}

## 原因
{reason}
"""


def create_feedback_and_continue(run_id: str, reason: str, test_result: dict[str, Any] | None = None) -> dict[str, Any]:
    from gitlab.issue_service import create_or_update_issue_by_marker

    run = get_staging_run(run_id)
    if not run:
        raise ValueError("staging run 不存在")
    db = SessionLocal()
    try:
        link = db.get(IssueIntakeLinkModel, run["link_id"])
        if not link:
            raise ValueError("Issue link 不存在")
        project_url = f"{link.gitlab_base_url.rstrip('/')}/{link.gitlab_project_path.strip('/')}"
        issue_iid = link.issue_iid
        reporter_mobile = str(_as_dict(link.result).get("reporter_mobile") or "")
    finally:
        db.close()
    marker = f"<!-- viktor:staging-feedback:{run['link_id']} -->"
    body = _feedback_body(run, reason, test_result)
    title = f"Staging 验收失败：{run['link_id']}"
    labels = ["viktor:staging-feedback", "staging:failed", f"source:{issue_iid}"]
    feedback = create_or_update_issue_by_marker(
        project_url=project_url,
        marker=marker,
        title=title,
        description=body,
        labels=labels,
        confidential=True,
    )
    feedback_url = str(feedback.get("web_url") or "")
    _set_run(run_id, status="feedback_created", stage="feedback_created", message="已创建 staging 批量反馈", feedback_issue_url=feedback_url)
    emit_staging_event(run_id, "feedback_created", "已创建 staging 批量反馈", {"feedback_issue_url": feedback_url}, stage="feedback_created")
    for task_id in run.get("task_ids") or []:
        try:
            from core.temporal import trigger
            if not trigger.signal_coding_task_sync(str(task_id), "execution_continue", body):
                from core.coding_service import continue_execution
                continue_execution(str(task_id), body)
        except Exception as e:  # noqa: BLE001
            logger.warning("[staging] 回炉 CodingTask 失败 task={} run={}: {}", task_id, run_id, e)
    _notify_link(
        run["link_id"],
        "Viktor Staging 验收失败",
        f"- Run: `{run_id}`\n- Feedback: {feedback_url or '-'}\n- 原因: {reason}",
        extra_mobiles=[reporter_mobile] if reporter_mobile else None,
    )
    return {"feedback_issue_url": feedback_url, "body": body}


def restore_dev_after_failure(run_id: str) -> dict[str, Any]:
    run = get_staging_run(run_id)
    if not run:
        raise ValueError("staging run 不存在")
    payload = _as_dict(run.get("deploy_payload"))
    repos = _as_list(payload.get("repos"))
    if not repos:
        return {"ok": True, "skipped": True}
    deploy_branch = staging_acceptance_config.deploy_branch
    restored: list[dict[str, Any]] = []
    try:
        from core.coding_workspace import inject_git_credentials
        for repo in repos:
            repo_url = str(repo.get("repo_url") or "")
            rid = str(repo.get("repo_connector_id") or "repo")
            base = str(repo.get("dev_base_sha") or "")
            deploy = str(repo.get("dev_deploy_sha") or "")
            if not (repo_url and base and deploy):
                raise RuntimeError(f"{rid} 缺少 repo_url/dev_base_sha/dev_deploy_sha")
            ws = Path(os.path.expanduser(staging_acceptance_config.workspace_root)).resolve() / _safe_path(run_id) / f"restore-{_safe_path(rid)}"
            if ws.exists():
                shutil.rmtree(ws, ignore_errors=True)
            ws.parent.mkdir(parents=True, exist_ok=True)
            _git(["clone", "--branch", deploy_branch, inject_git_credentials(repo_url), str(ws)], timeout=600)
            if staging_acceptance_config.restore_strategy == "force_with_lease":
                _git(["reset", "--hard", base], cwd=ws, timeout=120)
                _git(["push", "--force-with-lease", "origin", f"HEAD:{deploy_branch}"], cwd=ws, timeout=600)
            elif deploy == base:
                restored.append({"repo_connector_id": rid, "dev_base_sha": base, "dev_deploy_sha": deploy, "skipped": True})
                continue
            else:
                parents = _git(["rev-list", "--parents", "-n", "1", deploy], cwd=ws, timeout=30).split()
                if len(parents) > 2:
                    _git(["revert", "-m", "1", "--no-edit", deploy], cwd=ws, timeout=300)
                else:
                    _git(["revert", "--no-edit", deploy], cwd=ws, timeout=300)
                _git(["push", "origin", f"HEAD:{deploy_branch}"], cwd=ws, timeout=600)
            restored.append({"repo_connector_id": rid, "dev_base_sha": base, "dev_deploy_sha": deploy})
    except Exception as e:  # noqa: BLE001
        raise StagingInfraFailure(f"恢复 {deploy_branch} 失败：{e}") from e
    emit_staging_event(run_id, "dev_restored", f"已恢复 {deploy_branch}", {"repos": restored}, stage="dev_restored")
    return {"ok": True, "repos": restored}


def mark_staging_failed(run_id: str, reason: str, *, business: bool, test_result: dict[str, Any] | None = None) -> dict[str, Any]:
    status = "test_failed" if business else "infra_failed"
    run = get_staging_run(run_id)
    if run:
        try:
            _mark_gitlab_status(run, "failed", reason[:255], target_url=run.get("feedback_issue_url") or run.get("report_url") or "")
        except Exception as e:  # noqa: BLE001
            logger.warning("[staging] 标记 GitLab failed 失败 run={}: {}", run_id, e)
    data = _set_run(run_id, status=status, stage=status, message=reason, last_error=reason, test_result=test_result, finished=True)
    emit_staging_event(run_id, status, reason, test_result or {}, stage=status)
    return data


def mark_staging_passed(run_id: str, test_result: dict[str, Any] | None = None) -> dict[str, Any]:
    run = get_staging_run(run_id)
    if not run:
        raise ValueError("staging run 不存在")
    _mark_gitlab_status(run, "success", "Staging 验收通过")
    db = SessionLocal()
    try:
        link = db.get(IssueIntakeLinkModel, run["link_id"])
        if link:
            result = _as_dict(link.result)
            result["staging"] = {
                **_as_dict(result.get("staging")),
                "latest_run_id": run_id,
                "latest_status": "passed",
                "passed_commit_fingerprint": run["commit_fingerprint"],
            }
            link.result = result
        for task_id in run.get("task_ids") or []:
            task = db.get(CodingTaskModel, str(task_id))
            if task:
                tr = _as_dict(task.result)
                tr["staging"] = {
                    **_as_dict(tr.get("staging")),
                    "required": True,
                    "latest_run_id": run_id,
                    "latest_status": "passed",
                    "passed_commit_sha": run.get("candidate_shas", {}).get(str(task_id), ""),
                }
                task.result = tr
        db.commit()
    finally:
        db.close()
    data = _set_run(run_id, status="passed", stage="passed", message="Staging 验收通过，MR 可合并", test_result=test_result, finished=True)
    emit_staging_event(run_id, "passed", "Staging 验收通过，MR 可合并", test_result or {}, stage="passed")
    _notify_link(run["link_id"], "Viktor Staging 验收通过", f"- Run: `{run_id}`\n- MR 可合并")
    return data


def _mark_gitlab_status(run: dict[str, Any], state: str, description: str, target_url: str = "") -> None:
    from core.coding_service import _resolve_repo
    from gitlab.merge_request_service import (
        create_or_update_merge_request_note,
        get_merge_request,
        set_commit_status,
        update_merge_request,
    )

    db = SessionLocal()
    try:
        for task_id in run.get("task_ids") or []:
            task = db.get(CodingTaskModel, str(task_id))
            if not task:
                continue
            task_result = _as_dict(task.result)
            mr = _as_dict(task_result.get("mr"))
            iid = mr.get("iid")
            sha = str((run.get("candidate_shas") or {}).get(str(task_id)) or task_result.get("head_commit") or "")
            if not iid or not sha:
                continue
            repo_url, _default_branch, _repo_id = _resolve_repo(task.project_id, task.repo_connector_id)
            remote = get_merge_request(repo_url=repo_url, merge_request_iid=iid)
            set_commit_status(
                repo_url=repo_url,
                sha=sha,
                state=state,
                context=staging_acceptance_config.gitlab.status_context,
                target_url=target_url or run.get("report_url") or "",
                description=description,
            )
            marker = f"<!-- viktor:staging:{run['run_id']} -->"
            body = f"{marker}\n### Viktor Staging\n\n- Run: `{run['run_id']}`\n- 状态: `{state}`\n- 说明: {description}\n"
            create_or_update_merge_request_note(repo_url=repo_url, merge_request_iid=iid, marker=marker, body=body)
            labels = _staging_labels(list(remote.get("labels") or []), state)
            update_merge_request(repo_url=repo_url, merge_request_iid=iid, labels=labels)
            if state == "success":
                title = str(remote.get("title") or "")
                if title.lower().startswith("draft:"):
                    update_merge_request(repo_url=repo_url, merge_request_iid=iid, title=title.split(":", 1)[1].strip())
    finally:
        db.close()


def mark_gitlab_pending_for_task(task_id: str) -> None:
    if not staging_acceptance_config.enabled:
        return
    from core.coding_service import _resolve_repo
    from gitlab.merge_request_service import get_merge_request, set_commit_status, update_merge_request

    db = SessionLocal()
    try:
        task = db.get(CodingTaskModel, task_id)
        if not task:
            return
        result = _as_dict(task.result)
        mr = _as_dict(result.get("mr"))
        iid = mr.get("iid")
        sha = str(result.get("head_commit") or "")
        if not iid:
            return
        repo_url, _default_branch, _repo_id = _resolve_repo(task.project_id, task.repo_connector_id)
        remote = get_merge_request(repo_url=repo_url, merge_request_iid=iid)
        sha = str(remote.get("sha") or remote.get("diff_refs", {}).get("head_sha") or sha)
        title = str(remote.get("title") or "")
        labels = _staging_labels(list(remote.get("labels") or []), "pending")
        if title and not title.lower().startswith("draft:"):
            update_merge_request(repo_url=repo_url, merge_request_iid=iid, title=f"Draft: {title}", labels=labels)
        else:
            update_merge_request(repo_url=repo_url, merge_request_iid=iid, labels=labels)
        if sha:
            set_commit_status(
                repo_url=repo_url,
                sha=sha,
                state="pending",
                context=staging_acceptance_config.gitlab.status_context,
                description="等待 Viktor staging 验收",
            )
        result["staging"] = {
            **_as_dict(result.get("staging")),
            "required": True,
            "latest_status": "pending",
            "passed_commit_sha": "",
        }
        task.result = result
        db.commit()
    finally:
        db.close()


def handle_task_mr_updated(task_id: str) -> dict[str, Any]:
    """MR source branch 更新后清空 staging pass 并重新排队。"""
    mark_gitlab_pending_for_task(task_id)
    db = SessionLocal()
    link_id = ""
    try:
        task = db.get(CodingTaskModel, task_id)
        if task:
            src = _as_dict(_as_dict(task.result).get("source_issue"))
            link_id = str(src.get("link_id") or "")
    finally:
        db.close()
    if link_id:
        run = enqueue_staging_for_link(link_id)
        return {"ok": True, "link_id": link_id, "run": run}
    return {"ok": True, "link_id": "", "run": None}


def _staging_labels(current: list[str], state: str) -> list[str]:
    blocked = {"viktor:staging-pending", "viktor:staging-failed", "viktor:staging-passed"}
    labels = [str(x) for x in current if str(x) not in blocked]
    if state == "success":
        labels.append("viktor:staging-passed")
    elif state == "failed":
        labels.append("viktor:staging-failed")
    else:
        labels.append("viktor:staging-pending")
    return list(dict.fromkeys(labels))
