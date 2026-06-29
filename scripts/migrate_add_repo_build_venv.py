#!/usr/bin/env python3
"""迁移：viktor_repository_connectors 增加 build_venv 列。

build_venv 控制 warmup 是否为该仓库建 venv 并安装依赖：
  1 = 加载依赖（可跑脚本的主代码仓库，如 order-api）
  0 = 只 clone 不建 venv（无需跑脚本的 worker 仓库，如解析/抽帧/DNA worker）

新列默认 1，老数据零影响（保持原「全部建 venv」行为）。需要瘦身的仓库
另行通过注册 API（build_venv=false）或 SQL 显式置 0。

用法:
    python scripts/migrate_add_repo_build_venv.py

幂等：列已存在时跳过。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import inspect, text

from core.database import engine

TABLE = "viktor_repository_connectors"
COLUMN = "build_venv"
DDL = f"ADD COLUMN {COLUMN} TINYINT NOT NULL DEFAULT 1 AFTER sort_order"


def main() -> None:
    inspector = inspect(engine)
    existing = {c["name"] for c in inspector.get_columns(TABLE)}
    if COLUMN in existing:
        print(f"✅ {TABLE}.{COLUMN} 已存在，跳过")
        return
    sql = f"ALTER TABLE {TABLE} {DDL}"
    print(f"执行: {sql}")
    with engine.begin() as conn:
        conn.execute(text(sql))
    print(f"✅ {TABLE}.{COLUMN} 添加完成")


if __name__ == "__main__":
    main()
