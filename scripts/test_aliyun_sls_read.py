#!/usr/bin/env python3
"""阿里云 SLS（日志服务）连通性 / 读日志测试脚本。

本地默认使用公网 Endpoint；在生产 Pod 内改用私网 Endpoint。
依赖: pip install aliyun-log-python-sdk

默认目标来自 deploy-config 中 order-api-tool 的 SLS 环境变量:
    project=order-api
    logstore=order-api-tool-prod

示例:

    pip install -q aliyun-log-python-sdk

    export ALIYUN_ACCESS_KEY_ID='...'
    export ALIYUN_ACCESS_KEY_SECRET='...'

    # 1) 本地公网测试：列出默认 Project 下 Logstore
    python scripts/test_aliyun_sls_read.py --list-logstores

    # 2) 本地公网测试：拉最近 15 分钟内最多 20 条（查询语句默认 *）
    python scripts/test_aliyun_sls_read.py

    # 3) 生产 Pod 私网测试
    python /tmp/test_aliyun_sls_read.py --endpoint intranet

环境变量（可选，与 --ak/--sk 等价，命令行优先）:
    ALIYUN_SLS_ACCESS_KEY_ID / ALIYUN_SLS_ACCESS_KEY_SECRET / ALIYUN_SLS_SECURITY_TOKEN
    兼容旧变量：ALIYUN_ACCESS_KEY_ID / ALIYUN_ACCESS_KEY_SECRET / ALIYUN_SECURITY_TOKEN
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any


PUBLIC_ENDPOINT = "cn-hangzhou.log.aliyuncs.com"
INTRANET_ENDPOINT = "cn-hangzhou-intranet.log.aliyuncs.com"
DEFAULT_PROJECT = "order-api"
DEFAULT_LOGSTORE = "order-api-tool-prod"


def _require_sdk():
    try:
        from aliyun.log import GetLogsRequest, ListLogstoresRequest, LogClient
        from aliyun.log.logexception import LogException
    except ImportError:
        print(
            "缺少依赖：请执行 pip install aliyun-log-python-sdk",
            file=sys.stderr,
        )
        sys.exit(2)
    return GetLogsRequest, ListLogstoresRequest, LogClient, LogException


def _client(endpoint: str, ak: str, sk: str, sts: str | None):
    _, _, LogClient, _ = _require_sdk()
    return LogClient(endpoint, accessKeyId=ak, accessKey=sk, securityToken=sts)


def _print_json(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def _resolve_endpoint(value: str) -> str:
    aliases = {
        "public": PUBLIC_ENDPOINT,
        "internet": PUBLIC_ENDPOINT,
        "intranet": INTRANET_ENDPOINT,
        "private": INTRANET_ENDPOINT,
    }
    return aliases.get(value, value)


def _masked(value: str) -> str:
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}...{value[-4:]}"


def cmd_list_projects(client) -> None:
    resp = client.list_project(offset=0, size=500)
    projects = resp.get_projects()
    print(
        f"total={resp.get_total()} count_this_page={resp.get_count()}",
        file=sys.stderr,
    )
    rows = []
    for p in projects:
        if isinstance(p, dict):
            rows.append(p)
        else:
            rows.append({"projectName": p})
    _print_json(rows)


def cmd_list_logstores(client, project: str) -> None:
    _, ListLogstoresRequest, _, _ = _require_sdk()
    resp = client.list_logstores(ListLogstoresRequest(project=project))
    names = resp.get_logstores()
    _print_json(
        {
            "project": project,
            "total": resp.get_total(),
            "count": resp.get_count(),
            "logstores": names,
        }
    )


def cmd_get_logs(
    client,
    project: str,
    logstore: str,
    minutes: int,
    query: str,
    line: int,
) -> None:
    GetLogsRequest, _, _, _ = _require_sdk()
    now = int(time.time())
    from_time = now - minutes * 60
    req = GetLogsRequest(
        project=project,
        logstore=logstore,
        fromTime=from_time,
        toTime=now,
        query=query,
        line=line,
        offset=0,
        reverse=True,
    )
    resp = client.get_logs(req)
    out = []
    for log in resp.get_logs():
        row = {
            "__time__": log.get_time(),
            "__source__": log.get_source(),
        }
        row.update(log.get_contents() or {})
        out.append(row)
    _print_json(
        {
            "meta": {
                "count": resp.get_count(),
                "is_completed": resp.is_completed(),
            },
            "logs": out,
        }
    )


def main() -> None:
    default_endpoint = os.environ.get("ALIYUN_SLS_ENDPOINT", PUBLIC_ENDPOINT)

    parser = argparse.ArgumentParser(
        description="测试阿里云 SLS 读权限（list project / logstore / get_logs）"
    )
    parser.add_argument(
        "--endpoint",
        default=default_endpoint,
        help=(
            "SLS Endpoint，支持 public/intranet 别名；"
            f"默认: {default_endpoint}，可用 ALIYUN_SLS_ENDPOINT 覆盖"
        ),
    )
    parser.add_argument(
        "--ak",
        "--access-key-id",
        dest="ak",
        default=os.environ.get("ALIYUN_SLS_ACCESS_KEY_ID") or os.environ.get("ALIYUN_ACCESS_KEY_ID", ""),
        help="AccessKey Id（或环境变量 ALIYUN_SLS_ACCESS_KEY_ID / ALIYUN_ACCESS_KEY_ID）",
    )
    parser.add_argument(
        "--sk",
        "--access-key-secret",
        dest="sk",
        default=os.environ.get("ALIYUN_SLS_ACCESS_KEY_SECRET") or os.environ.get("ALIYUN_ACCESS_KEY_SECRET", ""),
        help="AccessKey Secret（或环境变量 ALIYUN_SLS_ACCESS_KEY_SECRET / ALIYUN_ACCESS_KEY_SECRET）",
    )
    parser.add_argument(
        "--sts-token",
        dest="sts",
        default=os.environ.get("ALIYUN_SLS_SECURITY_TOKEN") or os.environ.get("ALIYUN_SECURITY_TOKEN") or None,
        help="STS SecurityToken（可选；环境变量 ALIYUN_SLS_SECURITY_TOKEN / ALIYUN_SECURITY_TOKEN）",
    )
    parser.add_argument(
        "--project",
        default=os.environ.get("ALIYUN_SLS_PROJECT", DEFAULT_PROJECT),
        help=f"Project 名；默认 {DEFAULT_PROJECT}（或环境变量 ALIYUN_SLS_PROJECT）",
    )
    parser.add_argument(
        "--logstore",
        default=os.environ.get("ALIYUN_SLS_LOGSTORE", DEFAULT_LOGSTORE),
        help=f"Logstore 名；默认 {DEFAULT_LOGSTORE}（或环境变量 ALIYUN_SLS_LOGSTORE）",
    )
    parser.add_argument(
        "--list-projects",
        action="store_true",
        help="仅列出可见 Project，不需要 --project",
    )
    parser.add_argument(
        "--list-logstores",
        action="store_true",
        help="列出 --project 下 Logstore，需要 --project",
    )
    parser.add_argument(
        "--minutes",
        type=int,
        default=15,
        help="拉日志时的时间窗口：最近 N 分钟（默认 15）",
    )
    parser.add_argument(
        "--query",
        default="*",
        help="SLS 查询语句（默认 *，需索引支持 SQL 时才能写 SQL）",
    )
    parser.add_argument(
        "--line",
        type=int,
        default=20,
        help="最多返回条数（默认 20）",
    )
    args = parser.parse_args()

    _require_sdk()  # fail fast on missing dependency

    if not args.ak or not args.sk:
        print(
            "必须提供 AccessKey：--ak / --sk（或环境变量 ALIYUN_SLS_ACCESS_KEY_ID / ALIYUN_SLS_ACCESS_KEY_SECRET）",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.list_projects and args.list_logstores:
        print("不能同时指定 --list-projects 与 --list-logstores", file=sys.stderr)
        sys.exit(1)

    endpoint = _resolve_endpoint(args.endpoint)
    print(
        "SLS config: "
        f"endpoint={endpoint}, project={args.project or '-'}, "
        f"logstore={args.logstore or '-'}, ak={_masked(args.ak)}",
        file=sys.stderr,
    )
    client = _client(endpoint, args.ak, args.sk, args.sts)
    _, _, _, LogException = _require_sdk()

    try:
        if args.list_projects:
            cmd_list_projects(client)
            return
        if args.list_logstores:
            if not args.project:
                print("请添加 --project", file=sys.stderr)
                sys.exit(1)
            cmd_list_logstores(client, args.project)
            return
        if not args.project or not args.logstore:
            print(
                "拉取日志需要 --project 与 --logstore；若尚未知 Project，请先执行:\n"
                f"  python {sys.argv[0]} --ak ... --sk ... --list-projects",
                file=sys.stderr,
            )
            sys.exit(1)
        cmd_get_logs(
            client,
            args.project,
            args.logstore,
            minutes=args.minutes,
            query=args.query,
            line=args.line,
        )
    except LogException as e:
        print(f"SLS API 错误: {e}", file=sys.stderr)
        if getattr(e, "get_error_code", lambda: "")() == "SignatureNotMatch":
            print(
                "诊断提示: SignatureNotMatch 通常表示 AccessKeyId 与 "
                "AccessKeySecret 不匹配，或 Secret 在 shell 中被特殊字符转义/截断。"
                "请用单引号设置 ALIYUN_ACCESS_KEY_SECRET，并确认 endpoint 不带 "
                "http:// 或 https://。",
                file=sys.stderr,
            )
        sys.exit(1)


if __name__ == "__main__":
    main()
