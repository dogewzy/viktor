#!/usr/bin/env python3
"""数据修订：设置各 Repository Connector 的 build_venv 开关。

build_venv=0 的仓库 warmup 只 clone 不建 venv（无需跑脚本的 worker 仓库）。
连接走 core.database.engine，复用 settings 里的 ${VIKTOR_DB_*} 环境变量。

用法:
    python scripts/set_repo_build_venv.py

幂等：重复执行结果一致。需新代码部署后开关才在 warmup 生效。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text

from core.database import engine

# (project_id, connector_id, build_venv)
TARGETS = [
    ("order-service", "order-api", 1),           # 主代码仓库：需要跑 retry/Milvus 等脚本
    ("order-service", "order-worker", 0),        # worker：只 clone
    ("order-service", "order-worker", 0),  # worker：只 clone
    ("order-service", "order-worker", 0),      # worker：只 clone
]


def _dump(conn) -> None:
    for r in conn.execute(text(
        "SELECT project_id, connector_id, build_venv FROM viktor_repository_connectors "
        "ORDER BY project_id, sort_order"
    )):
        print(f"  {r.project_id:14} {r.connector_id:20} build_venv={r.build_venv}")


def main() -> None:
    with engine.begin() as conn:
        print("=== 改前 ===")
        _dump(conn)
        for project_id, connector_id, value in TARGETS:
            res = conn.execute(text(
                "UPDATE viktor_repository_connectors SET build_venv=:v "
                "WHERE project_id=:p AND connector_id=:c"
            ), {"v": value, "p": project_id, "c": connector_id})
            print(f"  set {project_id}/{connector_id} -> {value} (matched {res.rowcount})")
        print("=== 改后 ===")
        _dump(conn)


if __name__ == "__main__":
    main()
