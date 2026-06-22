"""Shared `/week_report` command execution for DingTalk and Web chat."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from core.report_store import build_report_url, save_report
from gitlab.service import GitLabClient
from gitlab.weekly_stats import compute_weekly_user_line_stats, format_weekly_stats_markdown
from settings import gitlab_config


@dataclass(frozen=True)
class WeekReportCommandResult:
    title: str
    body: str


async def run_week_report_command(
    username: str | None,
    *,
    project_id: str = "gitlab",
    thread_id: str = "week_report",
) -> WeekReportCommandResult:
    """Build the response for `/week_report <GitLab用户名>`."""
    if not username:
        return WeekReportCommandResult(
            title="Viktor · 用法",
            body="**`/week_report <GitLab用户名>`**\n\n示例：`/week_report zhangsan`",
        )

    base_url = gitlab_config.base_url.rstrip("/")
    token = gitlab_config.token_for_base_url(base_url)
    if not token:
        return WeekReportCommandResult(
            title="Viktor · GitLab",
            body=(
                "未配置默认 GitLab Token，无法查询 GitLab 提交统计。\n\n"
                "请在 Viktor `config.yaml` / 环境变量中配置后再试。"
            ),
        )

    client = GitLabClient(
        base_url=base_url,
        private_token=token,
        timeout=60.0,
    )
    stats_result = await asyncio.to_thread(
        compute_weekly_user_line_stats,
        client,
        username,
    )
    markdown_body = format_weekly_stats_markdown(stats_result)
    report_title = f"{stats_result.username} 最近七天提交统计"
    report_id, summary, _title = save_report(
        markdown_text=markdown_body,
        project_id=project_id,
        thread_id=thread_id,
        title=report_title,
        copy_markdown=True,
    )
    url = build_report_url(report_id)
    return WeekReportCommandResult(
        title="Viktor · GitLab 最近七天提交",
        body=(
            f"{summary}\n\n"
            f"---\n\n"
            f"已生成 HTML 周报：[{url}]({url})\n\n"
            "报告页内可一键复制 Markdown 内容。"
        ),
    )
