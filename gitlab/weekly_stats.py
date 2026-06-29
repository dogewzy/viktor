"""
基于 GitLab REST API v4，汇总指定用户在「最近七天（Asia/Shanghai）」内的推送所产生的提交行数。

依赖 GET /users、GET /user、GET /events 或 GET /users/:id/events、repository/compare、repository/commits/:sha（stats）。

说明：查询「当前 PAT 对应用户本人」时优先走 GET /events；部分自建 GitLab 上 GET /users/:id/events
对本人会恒为空，与 Profile 页不一致。查询他人仍用 /users/:id/events（需与对方有共同项目或是管理员）。
仅能统计 Push 事件可达的提交。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo

from loguru import logger

from gitlab.service import GitLabClient

_ZERO_SHA = "0" * 40


def _truncate_title(text: str, max_len: int = 120) -> str:
    s = text.strip().replace("\r", " ")
    if len(s) <= max_len:
        return s
    return s[: max_len - 1] + "…"


def week_bounds_local(
    tz_name: str = "Asia/Shanghai",
    *,
    now: datetime | None = None,
) -> tuple[datetime, datetime]:
    """汇报窗口：当前时刻往前滚动七天。

    返回二者对应的 UTC aware datetime。
    """
    tz = ZoneInfo(tz_name)
    if now is None:
        now_local = datetime.now(tz)
    elif now.tzinfo is None:
        now_local = now.replace(tzinfo=tz)
    else:
        now_local = now.astimezone(tz)
    start_local = now_local - timedelta(days=7)
    start_utc = start_local.astimezone(timezone.utc)
    end_utc = now_local.astimezone(timezone.utc)
    return start_utc, end_utc


def _parse_gitlab_time(value: str) -> datetime:
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return datetime.fromisoformat(value).astimezone(timezone.utc)


def resolve_gitlab_username(client: GitLabClient, username: str) -> dict[str, Any]:
    """解析 GitLab 登录名对应的用户 JSON。

    先按 username 精确查；部分自建 GitLab/PAT 权限下会返回空列表，
    再退回 search 并在结果中做 username 精确匹配。
    """
    u = username.strip()
    if not u:
        raise ValueError("GitLab 用户名为空")

    rows = client.get_json("/users", params={"username": u})
    if isinstance(rows, list) and rows:
        first = rows[0]
        if isinstance(first, dict):
            return first

    logger.info("[gitlab_weekly_stats] username 精确查询无结果，改用 search: {}", u)
    search_rows = client.get_json("/users", params={"search": u, "per_page": 100})
    if isinstance(search_rows, list):
        u_lower = u.lower()
        for row in search_rows:
            if not isinstance(row, dict):
                continue
            if str(row.get("username") or "").lower() == u_lower:
                return row

    raise ValueError(f"未找到 GitLab 用户: {username}")


def authenticated_user_id(client: GitLabClient) -> int | None:
    """返回当前 PAT 对应用户 id（GET /user）。Deploy Token 等无用户上下文时返回 None。"""
    try:
        row = client.get_json("/user")
        if isinstance(row, dict) and row.get("id") is not None:
            return int(row["id"])
    except Exception as e:
        logger.warning("[gitlab_weekly_stats] GET /user 失败，退回 /users/:id/events: {}", e)
    return None


def _is_branch_push_ref(ref: Any) -> bool:
    """判断是否分支推送（排除 tag 等）。兼容 ref 为 refs/heads/x 或仅为分支短名。"""
    s = str(ref or "").strip()
    if not s:
        return False
    if s.startswith("refs/heads/"):
        return True
    if s.startswith("refs/tags/") or s.startswith("refs/remotes/"):
        return False
    if s.startswith("refs/"):
        return False
    return True


def iter_user_push_events_since(
    client: GitLabClient,
    user_id: int,
    since_utc: datetime,
) -> list[dict[str, Any]]:
    """分页拉取用户 Push 事件（分支），时间在 since 之后。

    若目标用户即 PAT 持有人，使用 GET /events；否则 GET /users/:id/events。
    假定列表按 created_at **从新到旧**，一旦出现早于 since 的记录即停止翻页。
    """
    auth_id = authenticated_user_id(client)
    if auth_id is not None and auth_id == user_id:
        events_path = "/events"
        logger.info("[gitlab_weekly_stats] 使用 GET /events（与 PAT 用户 id={} 一致）", user_id)
    else:
        events_path = f"/users/{user_id}/events"
        logger.info("[gitlab_weekly_stats] 使用 GET {}", events_path)

    result: list[dict[str, Any]] = []
    page = 1
    per_page = 100
    while True:
        batch = client.get_json(
            events_path,
            params={"page": page, "per_page": per_page},
        )
        if not isinstance(batch, list) or not batch:
            break
        reached_old = False
        for ev in batch:
            created_raw = ev.get("created_at")
            if not created_raw:
                continue
            created = _parse_gitlab_time(str(created_raw))
            if created < since_utc:
                reached_old = True
                break
            pd = ev.get("push_data")
            if not pd or not isinstance(pd, dict):
                continue
            ref = pd.get("ref")
            if not _is_branch_push_ref(ref):
                continue
            result.append(ev)
        if reached_old or len(batch) < per_page:
            break
        page += 1
    return result


def commit_shas_for_push_event(client: GitLabClient, project_id: int, push_data: dict[str, Any]) -> list[str]:
    commit_to = str(push_data.get("commit_to") or "").strip()
    commit_from = str(push_data.get("commit_from") or "").strip()
    if not commit_to:
        return []
    if not commit_from or commit_from == _ZERO_SHA:
        return [commit_to]
    if commit_from == commit_to:
        return [commit_to]
    try:
        data = client.get_json(
            f"/projects/{project_id}/repository/compare",
            params={"from": commit_from, "to": commit_to},
        )
    except Exception as e:
        logger.warning(
            "[gitlab_weekly_stats] compare 失败 project={} {}..{} : {}",
            project_id,
            commit_from[:8],
            commit_to[:8],
            e,
        )
        return [commit_to]
    commits = data.get("commits") if isinstance(data, dict) else None
    if not commits or not isinstance(commits, list):
        return [commit_to]
    shas = [str(c["id"]) for c in commits if isinstance(c, dict) and c.get("id")]
    return shas if shas else [commit_to]


def _project_web_url_cached(
    client: GitLabClient,
    project_id: int,
    cache: dict[int, str],
) -> str | None:
    if project_id in cache:
        return cache[project_id]
    try:
        row = client.get_json(f"/projects/{project_id}")
        if isinstance(row, dict):
            raw = row.get("web_url")
            if isinstance(raw, str) and raw.strip():
                cache[project_id] = raw.strip().rstrip("/")
                return cache[project_id]
    except Exception as e:
        logger.warning("[gitlab_weekly_stats] 拉取 project web_url 失败 id={}: {}", project_id, e)
    return None


def fetch_commit_stats_detail(
    client: GitLabClient,
    project_id: int,
    sha: str,
    project_web_cache: dict[int, str],
) -> tuple[int, int, str | None, str, str]:
    """返回 (additions, deletions, commit 页 URL 或 None, 首行标题, committed_date)。"""
    enc = quote(sha, safe="")
    data = client.get_json(
        f"/projects/{project_id}/repository/commits/{enc}",
        params={"stats": "true"},
    )
    if not isinstance(data, dict):
        return 0, 0, None, "", ""
    st = data.get("stats") or {}
    if not isinstance(st, dict):
        st = {}
    additions = int(st.get("additions") or 0)
    deletions = int(st.get("deletions") or 0)

    raw_title = data.get("title")
    if not isinstance(raw_title, str) or not raw_title.strip():
        msg = data.get("message")
        raw_title = str(msg).split("\n", 1)[0] if msg else ""
    title = _truncate_title(raw_title)

    web_url: str | None = None
    wu = data.get("web_url")
    if isinstance(wu, str) and wu.strip():
        web_url = wu.strip()
    else:
        base = _project_web_url_cached(client, project_id, project_web_cache)
        if base:
            web_url = f"{base}/-/commit/{sha}"

    committed_date = ""
    raw_date = data.get("committed_date") or data.get("created_at") or ""
    if isinstance(raw_date, str) and raw_date.strip():
        try:
            dt = _parse_gitlab_time(raw_date)
            committed_date = dt.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d")
        except Exception:
            committed_date = raw_date[:10] if len(raw_date) >= 10 else ""

    return additions, deletions, web_url, title, committed_date


@dataclass(frozen=True)
class WeeklyCommitLink:
    """单条提交在报告中的展示项。"""

    project_id: int
    sha: str
    title: str
    web_url: str | None
    additions: int = 0
    deletions: int = 0
    committed_date: str = ""

    @property
    def short_sha(self) -> str:
        return self.sha[:8] if len(self.sha) > 8 else self.sha


@dataclass
class WeeklyLineStatsResult:
    username: str
    user_id: int
    week_start_utc: datetime
    week_end_utc: datetime
    commit_count: int = 0
    additions: int = 0
    deletions: int = 0
    push_events_used: int = 0
    commits: list[WeeklyCommitLink] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    project_names: dict[int, str] = field(default_factory=dict)


def compute_weekly_user_line_stats(
    client: GitLabClient,
    username: str,
    *,
    tz_name: str = "Asia/Shanghai",
) -> WeeklyLineStatsResult:
    start_utc, end_utc = week_bounds_local(tz_name)
    user = resolve_gitlab_username(client, username)
    uid = int(user["id"])
    display = str(user.get("username") or username)

    events = iter_user_push_events_since(client, uid, start_utc)
    seen: set[tuple[int, str]] = set()
    additions = 0
    deletions = 0
    warnings: list[str] = []
    commits_out: list[WeeklyCommitLink] = []
    project_web_cache: dict[int, str] = {}
    project_names: dict[int, str] = {}

    for ev in events:
        project_id = ev.get("project_id")
        pd = ev.get("push_data")
        if project_id is None or not isinstance(pd, dict):
            continue
        pid = int(project_id)
        # 缓存项目名称
        if pid not in project_names:
            try:
                proj_data = client.get_json(f"/projects/{pid}")
                if isinstance(proj_data, dict):
                    project_names[pid] = str(proj_data.get("path") or proj_data.get("name") or f"project-{pid}")
            except Exception:
                project_names[pid] = f"project-{pid}"
        shas = commit_shas_for_push_event(client, pid, pd)
        for sha in shas:
            key = (pid, sha)
            if key in seen:
                continue
            seen.add(key)
            try:
                add_n, del_n, link_url, title, committed_date = fetch_commit_stats_detail(
                    client,
                    pid,
                    sha,
                    project_web_cache,
                )
            except Exception as e:
                msg = f"提交 {sha[:8]} (project {pid}) 拉取 stats 失败: {e}"
                logger.warning("[gitlab_weekly_stats] {}", msg)
                warnings.append(msg)
                continue
            additions += add_n
            deletions += del_n
            commits_out.append(
                WeeklyCommitLink(
                    project_id=pid,
                    sha=sha,
                    title=title,
                    web_url=link_url,
                    additions=add_n,
                    deletions=del_n,
                    committed_date=committed_date,
                )
            )

    return WeeklyLineStatsResult(
        username=display,
        user_id=uid,
        week_start_utc=start_utc,
        week_end_utc=end_utc,
        commit_count=len(seen),
        additions=additions,
        deletions=deletions,
        push_events_used=len(events),
        commits=commits_out,
        warnings=warnings,
        project_names=project_names,
    )


_CN_NUM = {1: "一", 2: "两", 3: "三", 4: "四", 5: "五", 6: "六", 7: "七", 8: "八", 9: "九", 10: "十"}


def _cn_count(n: int) -> str:
    """小数量用中文量词（一/两/三…），超过 10 用阿拉伯数字。"""
    return _CN_NUM[n] if 1 <= n <= 10 else str(n)


def _signed(n: int) -> str:
    return f"+{n}" if n >= 0 else str(n)


def _group_by_project(
    result: WeeklyLineStatsResult,
) -> tuple[
    dict[int, list[WeeklyCommitLink]],
    list[tuple[int, str, int, int, int]],
]:
    """按 project_id 分组并计算每个项目的小计。

    返回:
        grouped: {project_id: [commits...]}
        summaries: [(project_id, name, count, additions, deletions)]
    """
    from collections import defaultdict

    grouped: dict[int, list[WeeklyCommitLink]] = defaultdict(list)
    for c in result.commits:
        grouped[c.project_id].append(c)

    summaries: list[tuple[int, str, int, int, int]] = []
    for pid, commits in grouped.items():
        name = result.project_names.get(pid, f"project-{pid}")
        p_add = sum(c.additions for c in commits)
        p_del = sum(c.deletions for c in commits)
        summaries.append((pid, name, len(commits), p_add, p_del))
    return grouped, summaries


def format_weekly_stats_markdown(result: WeeklyLineStatsResult, *, tz_name: str = "Asia/Shanghai") -> str:
    """按图示样式输出 Markdown：上方汇总 + 每个项目一张明细表。

    可直接复制到飞书 / 语雀 / GitLab Issue 等支持 Markdown 表格的地方。
    """
    tz = ZoneInfo(tz_name)
    start_local = result.week_start_utc.astimezone(tz).strftime("%Y-%m-%d %H:%M")
    end_local = result.week_end_utc.astimezone(tz).strftime("%Y-%m-%d %H:%M")
    grouped, summaries = _group_by_project(result)

    lines: list[str] = []
    lines.append("**最近七天工作完成情况:**")
    lines.append("")
    lines.append(f"统计窗口：{start_local} ~ {end_local}（{tz_name}）")
    lines.append("")
    lines.append("汇总")
    lines.append("")

    for _pid, name, count, p_add, p_del in summaries:
        net = p_add - p_del
        lines.append(
            f"- {name}: {count} 个 commit，+{p_add} / -{p_del}，净增 {_signed(net)}"
        )
    if len(summaries) > 1:
        total_net = result.additions - result.deletions
        lines.append(
            f"- {_cn_count(len(summaries))}个项目合计: {result.commit_count} 个 commit，"
            f"+{result.additions} / -{result.deletions}，净增 {_signed(total_net)}"
        )
    lines.append("")

    for pid, commits in grouped.items():
        name = result.project_names.get(pid, f"project-{pid}")
        lines.append(f"**{name}**")
        lines.append("")
        lines.append("| 日期 | commit | URL | + | - | 净增 | 说明 |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        for c in commits:
            date_str = c.committed_date or "-"
            url_cell = f"[link]({c.web_url})" if c.web_url else "-"
            net = c.additions - c.deletions
            # 标题里若含 `|` 会破坏表格，转义一下
            title_cell = (c.title or "-").replace("|", "\\|")
            lines.append(
                f"| {date_str} | {c.sha} | {url_cell} | {c.additions} | {c.deletions} | {net} | {title_cell} |"
            )
        lines.append("")

    if result.warnings:
        lines.append("**部分警告**（已跳过对应提交）：")
        for w in result.warnings[:8]:
            lines.append(f"- {w}")
        if len(result.warnings) > 8:
            lines.append(f"- ... 另有 {len(result.warnings) - 8} 条")
    return "\n".join(lines)


def format_weekly_stats_html(
    result: WeeklyLineStatsResult,
    *,
    tz_name: str = "Asia/Shanghai",
) -> str:
    """渲染独立可分享的 HTML 报告，整体排版与 Markdown 版本对齐。"""
    from html import escape

    tz = ZoneInfo(tz_name)
    start_local = result.week_start_utc.astimezone(tz).strftime("%Y-%m-%d %H:%M")
    end_local = result.week_end_utc.astimezone(tz).strftime("%Y-%m-%d %H:%M")
    grouped, summaries = _group_by_project(result)

    parts: list[str] = []
    parts.append(
        "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        f"<title>{escape(result.username)} 最近七天提交统计</title>"
        "<style>"
        "body{font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',sans-serif;"
        "max-width:1080px;margin:32px auto;padding:0 24px;color:#1f2328;line-height:1.55;}"
        "h1{font-size:22px;margin:0 0 8px;}h2{font-size:18px;margin:24px 0 8px;}h3{font-size:16px;margin:24px 0 8px;}"
        ".meta{color:#6b7280;font-size:13px;margin-bottom:16px;}"
        "ul{padding-left:20px;}li{margin:4px 0;}"
        "table{border-collapse:collapse;width:100%;margin-bottom:16px;font-size:13px;}"
        "th,td{border:1px solid #d0d7de;padding:6px 10px;vertical-align:top;text-align:left;word-break:break-all;}"
        "th{background:#f6f8fa;}"
        "td.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap;}"
        "code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px;}"
        ".warn{color:#9a3412;}"
        "</style></head><body>"
    )
    parts.append("<h1>最近七天工作完成情况:</h1>")
    parts.append(
        f"<div class=\"meta\">用户 <b>{escape(result.username)}</b>（id={result.user_id}）"
        f"&nbsp;·&nbsp;窗口 {start_local} ~ {end_local}（{escape(tz_name)}）"
        f"&nbsp;·&nbsp;Push 事件 {result.push_events_used} 条</div>"
    )

    parts.append("<h2>汇总</h2><ul>")
    for _pid, name, count, p_add, p_del in summaries:
        net = p_add - p_del
        parts.append(
            f"<li>{escape(name)}: {count} 个 commit，+{p_add} / -{p_del}，净增 {_signed(net)}</li>"
        )
    if len(summaries) > 1:
        total_net = result.additions - result.deletions
        parts.append(
            f"<li>{_cn_count(len(summaries))}个项目合计: {result.commit_count} 个 commit，"
            f"+{result.additions} / -{result.deletions}，净增 {_signed(total_net)}</li>"
        )
    parts.append("</ul>")

    for pid, commits in grouped.items():
        name = result.project_names.get(pid, f"project-{pid}")
        parts.append(f"<h3>{escape(name)}</h3>")
        parts.append(
            "<table><thead><tr>"
            "<th>日期</th><th>commit</th><th>URL</th>"
            "<th>+</th><th>-</th><th>净增</th><th>说明</th>"
            "</tr></thead><tbody>"
        )
        for c in commits:
            date_str = escape(c.committed_date or "-")
            url_cell = (
                f"<a href=\"{escape(c.web_url, quote=True)}\" target=\"_blank\" rel=\"noreferrer\">link</a>"
                if c.web_url
                else "-"
            )
            net = c.additions - c.deletions
            parts.append(
                "<tr>"
                f"<td>{date_str}</td>"
                f"<td><code>{escape(c.sha)}</code></td>"
                f"<td>{url_cell}</td>"
                f"<td class=\"num\">{c.additions}</td>"
                f"<td class=\"num\">{c.deletions}</td>"
                f"<td class=\"num\">{net}</td>"
                f"<td>{escape(c.title or '-')}</td>"
                "</tr>"
            )
        parts.append("</tbody></table>")

    if result.warnings:
        parts.append("<h3 class=\"warn\">部分警告（已跳过对应提交）</h3><ul class=\"warn\">")
        for w in result.warnings[:20]:
            parts.append(f"<li>{escape(w)}</li>")
        if len(result.warnings) > 20:
            parts.append(f"<li>... 另有 {len(result.warnings) - 20} 条</li>")
        parts.append("</ul>")

    parts.append("</body></html>")
    return "".join(parts)
