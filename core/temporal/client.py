"""Temporal client 连接与命名约定。

workflow id 方案（保证 signal 能精确路由、且天然幂等去重）：
- CodingTaskWorkflow.id == coding task_id（如 `ct_xxx`）
- IssueLinkWorkflow.id   == issue link_id（如 `il_xxx`）
"""
from __future__ import annotations

from temporalio.client import Client

from settings import temporal_config

CODING_TASK_WORKFLOW = "CodingTaskWorkflow"
ISSUE_LINK_WORKFLOW = "IssueLinkWorkflow"
STAGING_COORDINATOR_WORKFLOW = "StagingCoordinatorWorkflow"
STAGING_RUN_WORKFLOW = "StagingRunWorkflow"


def task_workflow_id(task_id: str) -> str:
    return str(task_id)


def link_workflow_id(link_id: str) -> str:
    return str(link_id)


def staging_coordinator_workflow_id(env_id: str) -> str:
    return f"staging-coordinator:{env_id}"


def staging_run_workflow_id(run_id: str) -> str:
    return f"staging-run:{run_id}"


def temporal_target() -> str:
    return f"{temporal_config.host}:{temporal_config.port}"


async def get_temporal_client() -> Client:
    """连接 Temporal frontend。调用方负责复用/缓存（连接较重）。"""
    return await Client.connect(
        temporal_target(),
        namespace=temporal_config.namespace,
    )
