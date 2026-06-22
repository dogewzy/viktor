#!/usr/bin/env python3
"""本地/Pod 验证：按 GitLab 用户名汇总「最近七天（默认 Asia/Shanghai）」分支推送的增删行数。

读取与主服务相同的 `config.yaml` + `.env`（`gitlab.credentials` 对应环境变量）。

用法:
  # 本地（注意激活 .venv，否则可能拿到系统 Python）
  cd /path/to/viktor && ./.venv/bin/python scripts/test_gitlab_weekly_stats.py -u <gitlab_username>

  # 生产 Pod 直连 GitLab，无需 SSH/数据库
  kubectl exec -n video-tracker -it $(kubectl get pod -n video-tracker -o name | grep viktor | head -1) -- \
      python scripts/test_gitlab_weekly_stats.py -u <gitlab_username>

  # 输出 HTML 报告，用浏览器直接打开
  ./.venv/bin/python scripts/test_gitlab_weekly_stats.py -u alice --format html -o /tmp/alice.html
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from gitlab.service import GitLabClient
from gitlab.weekly_stats import (
    compute_weekly_user_line_stats,
    format_weekly_stats_html,
    format_weekly_stats_markdown,
)
from settings import gitlab_config


def main() -> None:
    parser = argparse.ArgumentParser(description="测试 GitLab 用户最近七天提交行数统计")
    parser.add_argument("-u", "--username", required=True, help="GitLab username（登录名）")
    parser.add_argument(
        "--timezone",
        default="Asia/Shanghai",
        help="报告窗口时区（默认 Asia/Shanghai，窗口为当前时刻往前七天）",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="HTTP 超时秒数",
    )
    parser.add_argument(
        "--base-url",
        default=gitlab_config.base_url,
        help="GitLab base URL（默认使用 gitlab.base_url）",
    )
    parser.add_argument(
        "-f",
        "--format",
        choices=("markdown", "html"),
        default="markdown",
        help="输出格式：markdown（默认，可直接复制）/ html（独立报告页面）",
    )
    parser.add_argument(
        "-o",
        "--output",
        default=None,
        help="可选，写入到指定文件（不写则打印到 stdout）",
    )
    args = parser.parse_args()

    base = (args.base_url or "").strip().rstrip("/")
    token = gitlab_config.token_for_base_url(base)
    if not token:
        raise SystemExit(f"未配置 {base} 对应 GitLab Token，请在 gitlab.credentials / 环境变量中设置。")
    if not base or "example.com" in base:
        print("警告: GITLAB_BASE_URL 可能仍为占位，请确认指向实际实例。", file=sys.stderr)

    client = GitLabClient(base_url=base, private_token=token, timeout=args.timeout)
    result = compute_weekly_user_line_stats(client, args.username, tz_name=args.timezone)

    if args.format == "html":
        rendered = format_weekly_stats_html(result, tz_name=args.timezone)
    else:
        rendered = format_weekly_stats_markdown(result, tz_name=args.timezone)

    if args.output:
        out_path = Path(args.output).expanduser().resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(rendered, encoding="utf-8")
        print(f"已写入 {out_path}", file=sys.stderr)
    else:
        print(rendered)


if __name__ == "__main__":
    main()
