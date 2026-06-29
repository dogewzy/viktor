#!/usr/bin/env python3
"""迁移脚本：为 viktor_repository_connectors 新增 description 列。

description 用于 issue 自动路由器（core/issue_router.py）判断「什么需求该进哪个仓库」。

用法:
    python scripts/migrate_add_repository_connector_description.py

幂等：列已存在时跳过。
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import text
from core.database import engine

TABLE = "viktor_repository_connectors"
COLUMN = "description"


def main() -> None:
    print(f"=== 迁移：{TABLE} 新增 {COLUMN} 列 ===")
    with engine.connect() as conn:
        exists = conn.execute(text(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = :t AND column_name = :c"
        ), {"t": TABLE, "c": COLUMN}).scalar() > 0

        if exists:
            print(f"列 {TABLE}.{COLUMN} 已存在，跳过。")
            return

        conn.execute(text(
            f"ALTER TABLE {TABLE} ADD COLUMN {COLUMN} TEXT NOT NULL AFTER display_name"
        ))
        conn.commit()
        print(f"列 {TABLE}.{COLUMN} 创建成功。")


if __name__ == "__main__":
    main()
