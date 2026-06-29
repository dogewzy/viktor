#!/usr/bin/env python3
"""
迁移：将 viktor_reports.html_body 升级为 MEDIUMTEXT。

大报告的渲染后 HTML 可能超过 MySQL TEXT 的 64KB 上限，导致保存时报
Data too long for column 'html_body'。本迁移只放宽字段容量，幂等可重复执行。

用法:
    ./.venv/bin/python scripts/migrate_reports_mediumtext.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import inspect, text

from core.database import engine


TABLE = "viktor_reports"
COLUMN = "html_body"


def main() -> None:
    inspector = inspect(engine)
    if TABLE not in inspector.get_table_names():
        print(f"{TABLE} 不存在，跳过")
        return

    columns = {col["name"]: col for col in inspector.get_columns(TABLE)}
    if COLUMN not in columns:
        print(f"{TABLE}.{COLUMN} 不存在，跳过")
        return

    current_type = str(columns[COLUMN]["type"]).lower()
    if "mediumtext" in current_type or "longtext" in current_type:
        print(f"{TABLE}.{COLUMN} 已是 {current_type}，跳过")
        return

    if engine.dialect.name != "mysql":
        print(f"当前数据库方言为 {engine.dialect.name}，无需执行 MySQL MEDIUMTEXT 迁移")
        return

    print(f"修改 {TABLE}.{COLUMN}: {current_type} -> MEDIUMTEXT")
    with engine.begin() as conn:
        conn.execute(text(f"ALTER TABLE {TABLE} MODIFY {COLUMN} MEDIUMTEXT NOT NULL"))
    print("完成")


if __name__ == "__main__":
    main()
