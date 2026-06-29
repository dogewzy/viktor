#!/usr/bin/env python3
"""迁移脚本：新增项目级 Log Connector表。

用法:
    python scripts/migrate_add_log_connectors.py

幂等：重复运行不会重复建表。
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import text

from core.database import engine
from core.models import LogConnectorModel


TABLE_NAME = "viktor_log_connectors"


def main() -> None:
    print("=== 迁移：新增 viktor_log_connectors 表 ===")
    with engine.connect() as conn:
        result = conn.execute(text(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema = DATABASE() AND table_name = :table_name"
        ), {"table_name": TABLE_NAME})
        exists = result.scalar() > 0

    if exists:
        print(f"表 {TABLE_NAME} 已存在，跳过建表。")
        return

    LogConnectorModel.__table__.create(engine)
    print(f"表 {TABLE_NAME} 创建成功。")


if __name__ == "__main__":
    main()
