"""Watchdog 钉钉报警通知器。

通过自定义机器人 Webhook 向独立的报警群发送消息。
支持签名验证（sign_secret）、@指定人 / @所有人。
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger

from core.dingtalk_notifier import send_dingtalk_markdown

if TYPE_CHECKING:
    from core.registry import WatchdogNotificationTarget


def build_watchdog_markdown(
    *,
    watchdog_name: str,
    project_id: str,
    severity: str,
    conclusion: str,
    coding_task_id: str = "",
    coding_task_status: str = "",
    coding_task_stage: str = "",
    coding_task_message: str = "",
) -> dict:
    """构造钉钉 Markdown 格式报警消息体。"""
    severity_emoji = {"critical": "[CRITICAL]", "warning": "[WARNING]"}.get(severity, "[INFO]")
    title = f"{severity_emoji} {watchdog_name}"
    lines = [
        f"## {title}",
        "",
        f"**项目**: {project_id}",
        f"**严重程度**: {severity}",
        "",
        "---",
        "",
        conclusion,
    ]
    if coding_task_id:
        lines.append("")
        lines.append(f"> {_coding_task_notice(coding_task_id, coding_task_status, coding_task_stage, coding_task_message)}")
    return {"title": title, "text": "\n".join(lines)}


def _coding_task_notice(task_id: str, status: str = "", stage: str = "", message: str = "") -> str:
    detail = f"任务ID: `{task_id}`"
    if status == "waiting_plan_review":
        return f"已生成修复 Plan，等待人工审核，{detail}"
    if status == "waiting_clarification":
        return f"Coding Task 已创建，但需要先回答澄清问题后才能生成 Plan，{detail}"
    if status in {"failed", "cancelled"}:
        suffix = f"，原因: {message}" if message else ""
        return f"Coding Task 未能生成可审核 Plan（{status}）{suffix}，{detail}"
    if status == "timeout":
        stage_text = f"，当前阶段: {stage}" if stage else ""
        return f"Coding Task 已创建，但等待 Plan 生成超时{stage_text}，{detail}"
    if status:
        stage_text = f"/{stage}" if stage and stage != status else ""
        return f"Coding Task 已创建，当前状态: {status}{stage_text}，{detail}"
    return f"Coding Task 已创建，{detail}"


async def send_dingtalk_notification(
    target: "WatchdogNotificationTarget",
    *,
    watchdog_name: str,
    project_id: str,
    severity: str,
    conclusion: str,
    coding_task_id: str = "",
    coding_task_status: str = "",
    coding_task_stage: str = "",
    coding_task_message: str = "",
) -> None:
    """发送钉钉 Webhook 报警消息（Markdown 格式）。

    Raises:
        HTTPStatusError: 当钉钉 API 返回非 2xx 状态时。
        Exception: 其他网络错误。
    """
    md = build_watchdog_markdown(
        watchdog_name=watchdog_name,
        project_id=project_id,
        severity=severity,
        conclusion=conclusion,
        coding_task_id=coding_task_id,
        coding_task_status=coding_task_status,
        coding_task_stage=coding_task_stage,
        coding_task_message=coding_task_message,
    )
    await send_dingtalk_markdown(
        webhook_url=target.webhook_url,
        sign_secret=target.sign_secret,
        title=str(md["title"]),
        text=str(md["text"]),
        at_mobiles=target.at_mobiles,
        at_all=target.at_all,
    )
    logger.info("Watchdog 报警已发送: {} ({})", watchdog_name, severity)
