#!/usr/bin/env python3
"""
迁移：viktor_database_connectors 增加 ssh_tunnel 列（JSON NULL）。

语义：NULL=直连（默认），JSON={jump_host,jump_port,username,private_key} 开启 SSH 隧道。
对应代码改动：DatabaseConnectorItem.ssh_tunnel / DatabaseConnectorModel.ssh_tunnel。

用法:
    python scripts/migrate_add_database_connector_ssh_tunnel.py

幂等：列已存在时跳过。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import inspect, text

from core.database import engine


TABLE = "viktor_database_connectors"
COLUMN = "ssh_tunnel"


def main() -> None:
    inspector = inspect(engine)
    cols = {c["name"] for c in inspector.get_columns(TABLE)}
    if COLUMN in cols:
        print(f"✅ {TABLE}.{COLUMN} 已存在，跳过")
        return

    ddl = f"ALTER TABLE {TABLE} ADD COLUMN {COLUMN} JSON NULL AFTER charset_name"
    print(f"执行: {ddl}")
    with engine.begin() as conn:
        conn.execute(text(ddl))
    print(f"✅ {TABLE}.{COLUMN} 添加完成")


if __name__ == "__main__":
    main()
