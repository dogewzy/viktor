#!/usr/bin/env python3
"""止血：回填 Issue Intake link 中丢失的 coding_tasks[].mr_url，并补发卡住的钉钉聚合通知。

背景：多仓路由下，watcher 时序错乱 / 多副本整列写回会让某些 task 的 mr_url 在
link.result.coding_tasks 里空着（task 表其实已有真实 MR），导致
`_maybe_notify_all_mr_ready` 的 “任一仓库没出 MR 就不发” 永久成立，聚合通知发不出去。

本脚本遍历所有 active link，按 coding_task_id 从 viktor_coding_tasks 回查、只填不清；
全部 task 都有 mr_url 后清掉 mr_ready_notified、修正 status 并补发一次聚合通知。

用法:
    python scripts/heal_issue_intake_mr_urls.py            # dry-run，只打印将做什么
    python scripts/heal_issue_intake_mr_urls.py --apply    # 实际回填 + 补发
    python scripts/heal_issue_intake_mr_urls.py --apply --link il_xxx   # 只处理指定 link

幂等：已发过通知（mr_ready_notified=True）的 link 不会重复发。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.database import SessionLocal  # noqa: E402
from core.models import CodingTaskModel, IssueIntakeLinkModel  # noqa: E402
from core.issue_intake_service import (  # noqa: E402
    ACTIVE_LINK_STATUSES,
    _maybe_notify_all_mr_ready,
)

# task 已到这些状态说明 MR 已产出，其 mr_url 可信。
_MR_READY_TASK_STATUSES = {"waiting_code_review", "completed"}


def _task_mr_url(db, task_id: str) -> str:
    task = db.get(CodingTaskModel, task_id)
    if not task:
        return ""
    status = str(getattr(task, "status", "") or "")
    if status not in _MR_READY_TASK_STATUSES:
        return ""
    mr = str(getattr(task, "mr_url", "") or "").strip()
    if mr:
        return mr
    result = task.result if isinstance(task.result, dict) else {}
    return str(result.get("mr_url") or "").strip()


def heal(apply: bool, only_link: str = "") -> None:
    db = SessionLocal()
    try:
        query = db.query(IssueIntakeLinkModel).filter(
            IssueIntakeLinkModel.status.in_(sorted(ACTIVE_LINK_STATUSES))
        )
        if only_link:
            query = query.filter(IssueIntakeLinkModel.link_id == only_link)
        rows = query.limit(2000).all()

        backfilled_links: list[str] = []
        notify_links: list[str] = []
        for row in rows:
            result = dict(row.result or {})
            tasks = list(result.get("coding_tasks") or [])
            if not tasks:
                continue
            changed = False
            for t in tasks:
                if str(t.get("mr_url") or "").strip():
                    continue
                tid = str(t.get("coding_task_id") or "").strip()
                if not tid:
                    continue
                real_mr = _task_mr_url(db, tid)
                if real_mr:
                    print(
                        f"  [link {row.link_id}] repo={t.get('repo_connector_id')} "
                        f"task={tid} 回填 mr_url -> {real_mr}"
                    )
                    t["mr_url"] = real_mr
                    changed = True

            all_have_mr = bool(tasks) and all(
                str(t.get("mr_url") or "").strip() for t in tasks
            )
            if changed:
                backfilled_links.append(row.link_id)
                if apply:
                    result["coding_tasks"] = tasks
                    # 全部到齐则清掉通知幂等标记 + 修正顶层 status，让补发与展示一致。
                    if all_have_mr:
                        result.pop("mr_ready_notified", None)
                        if row.status not in {"issue_closed", "completed"}:
                            row.status = "mr_created"
                            row.stage = "waiting_code_review"
                            row.message = "多仓 MR 均已创建，等待开发合并"
                    row.result = result
            if all_have_mr and not result.get("mr_ready_notified"):
                notify_links.append(row.link_id)

        if apply:
            db.commit()
    finally:
        db.close()

    print()
    print(f"扫描 active link: {len(rows)}")
    print(f"需要回填 mr_url 的 link: {len(backfilled_links)} -> {backfilled_links}")
    print(f"齐活待补发通知的 link: {len(notify_links)} -> {notify_links}")

    if not apply:
        print("\n[dry-run] 未写库、未发通知。加 --apply 实际执行。")
        return

    # 回填提交后再补发，确保 _maybe_notify_all_mr_ready 读到最新 result。
    for link_id in notify_links:
        try:
            print(f"补发聚合通知: link={link_id}")
            _maybe_notify_all_mr_ready(link_id)
        except Exception as e:  # noqa: BLE001
            print(f"  补发失败 link={link_id}: {e}")
    print("\n完成。")


def main() -> None:
    parser = argparse.ArgumentParser(description="回填 Issue Intake mr_url 并补发卡住的通知")
    parser.add_argument("--apply", action="store_true", help="实际写库并补发通知（默认 dry-run）")
    parser.add_argument("--link", default="", help="只处理指定 link_id")
    args = parser.parse_args()
    heal(apply=args.apply, only_link=args.link.strip())


if __name__ == "__main__":
    main()
