"""Staging acceptance workflows 用的 activities。"""
from __future__ import annotations

import asyncio
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError


@activity.defn
async def get_next_staging_run(env_id: str) -> dict[str, Any] | None:
    def _run() -> dict[str, Any] | None:
        from core.staging_acceptance_service import next_queued_run
        return next_queued_run(env_id)
    return await asyncio.to_thread(_run)


@activity.defn
async def get_staging_run_activity(run_id: str) -> dict[str, Any] | None:
    def _run() -> dict[str, Any] | None:
        from core.staging_acceptance_service import get_staging_run
        return get_staging_run(run_id)
    return await asyncio.to_thread(_run)


@activity.defn
async def acquire_staging_lock(run_id: str, env_id: str, lease_owner: str) -> bool:
    def _run() -> bool:
        from core.staging_acceptance_service import acquire_lock
        return acquire_lock(run_id, env_id, lease_owner)
    return await asyncio.to_thread(_run)


@activity.defn
async def heartbeat_staging_lock(run_id: str, env_id: str) -> bool:
    def _run() -> bool:
        from core.staging_acceptance_service import heartbeat_lock
        return heartbeat_lock(run_id, env_id)
    return await asyncio.to_thread(_run)


@activity.defn
async def release_staging_lock(run_id: str, env_id: str) -> bool:
    def _run() -> bool:
        from core.staging_acceptance_service import release_lock
        return release_lock(run_id, env_id)
    return await asyncio.to_thread(_run)


@activity.defn
async def hold_staging_lock_for_manual_intervention(run_id: str, env_id: str, reason: str) -> bool:
    def _run() -> bool:
        from core.staging_acceptance_service import hold_lock_for_manual_intervention
        return hold_lock_for_manual_intervention(run_id, env_id, reason)
    return await asyncio.to_thread(_run)


@activity.defn
async def refresh_staging_candidates(run_id: str) -> dict[str, Any]:
    def _run() -> dict[str, Any]:
        from core.staging_acceptance_service import refresh_run_candidates
        return refresh_run_candidates(run_id)
    return await asyncio.to_thread(_run)


@activity.defn
async def integrate_staging_to_dev(run_id: str) -> dict[str, Any]:
    def _run() -> dict[str, Any]:
        from core.staging_acceptance_service import (
            StagingBusinessFailure,
            StagingInfraFailure,
            integrate_candidates_to_dev,
        )
        try:
            return integrate_candidates_to_dev(run_id)
        except StagingBusinessFailure as e:
            raise ApplicationError(str(e), type="StagingBusinessFailure", non_retryable=True)
        except StagingInfraFailure as e:
            raise ApplicationError(str(e), type="StagingInfraFailure")
    return await asyncio.to_thread(_run)


@activity.defn
async def wait_staging_deploy(run_id: str) -> dict[str, Any]:
    def _run() -> dict[str, Any]:
        from core.staging_acceptance_service import StagingInfraFailure, wait_for_deployment
        try:
            return wait_for_deployment(run_id)
        except StagingInfraFailure as e:
            raise ApplicationError(str(e), type="StagingInfraFailure")
    return await asyncio.to_thread(_run)


@activity.defn
async def run_staging_playwright(run_id: str) -> dict[str, Any]:
    def _run() -> dict[str, Any]:
        from core.staging_acceptance_service import StagingInfraFailure, run_playwright_acceptance
        try:
            return run_playwright_acceptance(run_id)
        except StagingInfraFailure as e:
            raise ApplicationError(str(e), type="StagingInfraFailure", non_retryable=True)
    return await asyncio.to_thread(_run)


@activity.defn
async def create_staging_feedback(run_id: str, reason: str, test_result: dict[str, Any] | None = None) -> dict[str, Any]:
    def _run() -> dict[str, Any]:
        from core.staging_acceptance_service import create_feedback_and_continue
        return create_feedback_and_continue(run_id, reason, test_result or {})
    return await asyncio.to_thread(_run)


@activity.defn
async def restore_staging_dev(run_id: str) -> dict[str, Any]:
    def _run() -> dict[str, Any]:
        from core.staging_acceptance_service import StagingInfraFailure, restore_dev_after_failure
        try:
            return restore_dev_after_failure(run_id)
        except StagingInfraFailure as e:
            raise ApplicationError(str(e), type="StagingInfraFailure", non_retryable=True)
    return await asyncio.to_thread(_run)


@activity.defn
async def mark_staging_failed_activity(run_id: str, reason: str, business: bool, test_result: dict[str, Any] | None = None) -> dict[str, Any]:
    def _run() -> dict[str, Any]:
        from core.staging_acceptance_service import mark_staging_failed
        return mark_staging_failed(run_id, reason, business=business, test_result=test_result or {})
    return await asyncio.to_thread(_run)


@activity.defn
async def mark_staging_passed_activity(run_id: str, test_result: dict[str, Any] | None = None) -> dict[str, Any]:
    def _run() -> dict[str, Any]:
        from core.staging_acceptance_service import mark_staging_passed
        return mark_staging_passed(run_id, test_result or {})
    return await asyncio.to_thread(_run)
