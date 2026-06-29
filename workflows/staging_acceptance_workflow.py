"""Staging acceptance durable workflows.

Coordinator 是单测试环境的串行调度器；Run workflow 负责一次 dev 集成、部署等待、
Playwright 验收、反馈/恢复/放行。
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

with workflow.unsafe.imports_passed_through():
    from activities.staging_activities import (
        acquire_staging_lock,
        create_staging_feedback,
        get_next_staging_run,
        heartbeat_staging_lock,
        hold_staging_lock_for_manual_intervention,
        integrate_staging_to_dev,
        mark_staging_failed_activity,
        mark_staging_passed_activity,
        refresh_staging_candidates,
        release_staging_lock,
        restore_staging_dev,
        run_staging_playwright,
        wait_staging_deploy,
    )
    from settings import staging_acceptance_config, temporal_config


_SHORT = timedelta(seconds=30)
_DISPATCH = timedelta(seconds=60)
_RETRY = RetryPolicy(maximum_interval=timedelta(seconds=60))
_STC = timedelta(seconds=temporal_config.activity_schedule_to_close_sec)
_LONG = timedelta(seconds=max(staging_acceptance_config.deploy.timeout_sec, staging_acceptance_config.playwright.timeout_sec, 60))


def _activity_error_type(e: ActivityError) -> str:
    cause = getattr(e, "cause", None)
    return str(getattr(cause, "type", "") or "")


def _activity_error_message(e: ActivityError) -> str:
    cause = getattr(e, "cause", None)
    return str(getattr(cause, "message", "") or e)


@workflow.defn(name="StagingCoordinatorWorkflow")
class StagingCoordinatorWorkflow:
    def __init__(self) -> None:
        self._poke = 0
        self._stop = False

    @workflow.signal
    def run_queued(self) -> None:
        self._poke += 1

    @workflow.signal
    def stop(self) -> None:
        self._stop = True

    @workflow.run
    async def run(self, env_id: str) -> dict[str, Any]:
        processed = 0
        while not self._stop:
            item = await workflow.execute_activity(
                get_next_staging_run,
                env_id,
                start_to_close_timeout=_SHORT,
                schedule_to_close_timeout=_STC,
                retry_policy=_RETRY,
            )
            if not item:
                try:
                    await workflow.wait_condition(lambda: self._poke > 0 or self._stop, timeout=timedelta(seconds=60))
                except asyncio.TimeoutError:
                    continue
                self._poke = 0
                continue
            run_id = str(item.get("run_id") or "")
            if not run_id:
                continue
            await workflow.execute_child_workflow(
                StagingRunWorkflow.run,
                args=[run_id, env_id],
                id=f"staging-run:{run_id}",
            )
            processed += 1
        return {"env_id": env_id, "processed": processed}


@workflow.defn(name="StagingRunWorkflow")
class StagingRunWorkflow:
    def __init__(self) -> None:
        self._cancel = False

    @workflow.signal
    def cancel(self) -> None:
        self._cancel = True

    async def _restore_dev_or_hold_lock(
        self,
        run_id: str,
        env_id: str,
        reason: str,
        test_result: dict[str, Any] | None = None,
    ) -> bool:
        try:
            await workflow.execute_activity(
                restore_staging_dev,
                run_id,
                start_to_close_timeout=_LONG,
                schedule_to_close_timeout=_LONG,
                retry_policy=_RETRY,
            )
            return True
        except ActivityError as e:
            restore_reason = _activity_error_message(e)
            await workflow.execute_activity(
                mark_staging_failed_activity,
                args=[run_id, restore_reason, False, test_result or {}],
                start_to_close_timeout=_SHORT,
                schedule_to_close_timeout=_STC,
                retry_policy=_RETRY,
            )
            try:
                await workflow.execute_activity(
                    hold_staging_lock_for_manual_intervention,
                    args=[run_id, env_id, f"{reason}; {restore_reason}"],
                    start_to_close_timeout=_SHORT,
                    schedule_to_close_timeout=_STC,
                    retry_policy=_RETRY,
                )
            except ActivityError:
                pass
            return False

    @workflow.run
    async def run(self, run_id: str, env_id: str) -> dict[str, Any]:
        locked = False
        release_on_exit = True
        try:
            while not locked:
                locked = await workflow.execute_activity(
                    acquire_staging_lock,
                    args=[run_id, env_id, f"workflow:{workflow.info().workflow_id}"],
                    start_to_close_timeout=_SHORT,
                    schedule_to_close_timeout=_STC,
                    retry_policy=_RETRY,
                )
                if not locked:
                    await workflow.sleep(timedelta(seconds=staging_acceptance_config.lock.heartbeat_sec))
                if self._cancel:
                    await workflow.execute_activity(
                        mark_staging_failed_activity,
                        args=[run_id, "Staging run 已取消", False, {}],
                        start_to_close_timeout=_SHORT,
                        schedule_to_close_timeout=_STC,
                        retry_policy=_RETRY,
                    )
                    return {"run_id": run_id, "result": "cancelled"}

            fresh = await workflow.execute_activity(
                refresh_staging_candidates,
                run_id,
                start_to_close_timeout=_SHORT,
                schedule_to_close_timeout=_STC,
                retry_policy=_RETRY,
            )
            if not fresh.get("fresh"):
                return {"run_id": run_id, "result": "superseded"}

            await workflow.execute_activity(
                heartbeat_staging_lock,
                args=[run_id, env_id],
                start_to_close_timeout=_SHORT,
                schedule_to_close_timeout=_STC,
                retry_policy=_RETRY,
            )

            try:
                await workflow.execute_activity(
                    integrate_staging_to_dev,
                    run_id,
                    start_to_close_timeout=_LONG,
                    schedule_to_close_timeout=_LONG,
                    retry_policy=_RETRY,
                )
            except ActivityError as e:
                kind = _activity_error_type(e)
                reason = _activity_error_message(e)
                business = kind == "StagingBusinessFailure"
                if business:
                    await workflow.execute_activity(
                        create_staging_feedback,
                        args=[run_id, reason, {}],
                        start_to_close_timeout=_DISPATCH,
                        schedule_to_close_timeout=_STC,
                        retry_policy=_RETRY,
                    )
                if not business:
                    restored = await self._restore_dev_or_hold_lock(run_id, env_id, reason, {})
                    if not restored:
                        release_on_exit = False
                        return {"run_id": run_id, "result": "infra_failed"}
                await workflow.execute_activity(
                    mark_staging_failed_activity,
                    args=[run_id, reason, business, {}],
                    start_to_close_timeout=_SHORT,
                    schedule_to_close_timeout=_STC,
                    retry_policy=_RETRY,
                )
                return {"run_id": run_id, "result": "test_failed" if business else "infra_failed"}

            await workflow.execute_activity(
                heartbeat_staging_lock,
                args=[run_id, env_id],
                start_to_close_timeout=_SHORT,
                schedule_to_close_timeout=_STC,
                retry_policy=_RETRY,
            )
            try:
                await workflow.execute_activity(
                    wait_staging_deploy,
                    run_id,
                    start_to_close_timeout=_LONG,
                    schedule_to_close_timeout=_LONG,
                    retry_policy=_RETRY,
                )
                result = await workflow.execute_activity(
                    run_staging_playwright,
                    run_id,
                    start_to_close_timeout=_LONG,
                    schedule_to_close_timeout=_LONG,
                    retry_policy=_RETRY,
                )
            except ActivityError as e:
                reason = _activity_error_message(e)
                restored = await self._restore_dev_or_hold_lock(run_id, env_id, reason, {})
                if not restored:
                    release_on_exit = False
                    return {"run_id": run_id, "result": "infra_failed"}
                await workflow.execute_activity(
                    mark_staging_failed_activity,
                    args=[run_id, reason, False, {}],
                    start_to_close_timeout=_SHORT,
                    schedule_to_close_timeout=_STC,
                    retry_policy=_RETRY,
                )
                return {"run_id": run_id, "result": "infra_failed"}

            status = str(result.get("status") or "")
            if status == "passed":
                await workflow.execute_activity(
                    mark_staging_passed_activity,
                    args=[run_id, result],
                    start_to_close_timeout=_DISPATCH,
                    schedule_to_close_timeout=_STC,
                    retry_policy=_RETRY,
                )
                return {"run_id": run_id, "result": "passed"}

            reason = "Playwright staging 验收失败"
            await workflow.execute_activity(
                create_staging_feedback,
                args=[run_id, reason, result],
                start_to_close_timeout=_DISPATCH,
                schedule_to_close_timeout=_STC,
                retry_policy=_RETRY,
            )
            restored = await self._restore_dev_or_hold_lock(run_id, env_id, reason, result)
            if not restored:
                release_on_exit = False
                return {"run_id": run_id, "result": "infra_failed"}
            await workflow.execute_activity(
                mark_staging_failed_activity,
                args=[run_id, reason, True, result],
                start_to_close_timeout=_SHORT,
                schedule_to_close_timeout=_STC,
                retry_policy=_RETRY,
            )
            return {"run_id": run_id, "result": "test_failed"}
        finally:
            if locked and release_on_exit:
                await workflow.execute_activity(
                    release_staging_lock,
                    args=[run_id, env_id],
                    start_to_close_timeout=_SHORT,
                    schedule_to_close_timeout=_STC,
                    retry_policy=_RETRY,
                )
