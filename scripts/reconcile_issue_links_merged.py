#!/usr/bin/env python3
"""手动触发 link 侧合并对账：把"所有 MR 已合并但 issue 未关闭"的 link 收尾。

背景：reconciler 只扫 status==waiting_code_review 的 coding task；一旦 task 被对账成
completed 而 issue 联动那一步漏掉，task 已 completed 后 reconciler 不再触碰，link 永久卡
mr_created、issue 不关、发起人收不到通知。core.issue_intake_service.reconcile_issue_links_merged
以 link 为锚兜底收尾，已挂到 600s 定时对账；本脚本用于部署后立即手动跑一次，不等定时。

用法:
    python scripts/reconcile_issue_links_merged.py

注意：会真实关闭 GitLab issue 并发钉钉通知（@ 发起人）。只对"所有关联 coding task 都已
确认合并（task completed 且 code_review.status==merged）"的 link 生效，幂等：收尾后 link
置 issue_closed，再跑不会重复处理。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.issue_intake_service import reconcile_issue_links_merged  # noqa: E402


def main() -> None:
    result = reconcile_issue_links_merged()
    print(f"link 侧合并对账完成：candidates={result.get('checked')} closed={result.get('closed')}")


if __name__ == "__main__":
    main()
