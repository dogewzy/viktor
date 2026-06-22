#!/usr/bin/env python3
"""
迁移：为 viktor_chat_messages 增加 reasoning_content 列。

DeepSeek 思考模式下，带 tool_calls 的 assistant 消息在后续请求中必须原样
回传 reasoning_content。该列用于跨轮会话历史重放。

用法:
    python scripts/migrate_add_chat_reasoning_content.py

幂等：列已存在时跳过。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import inspect, text

from core.database import engine


TABLE = "viktor_chat_messages"
COLUMN = "reasoning_content"


def main() -> None:
    inspector = inspect(engine)
    if TABLE not in inspector.get_table_names():
        print(f"❌ 表 {TABLE} 不存在，请先运行 migrate_add_chat_messages.py")
        sys.exit(1)

    cols = {c["name"] for c in inspector.get_columns(TABLE)}
    if COLUMN in cols:
        print(f"✅ {TABLE}.{COLUMN} 已存在，跳过")
        return

    dialect = engine.dialect.name
    if dialect == "mysql":
        ddl = f"ALTER TABLE {TABLE} ADD COLUMN {COLUMN} LONGTEXT NULL AFTER tool_calls"
    else:
        ddl = f"ALTER TABLE {TABLE} ADD COLUMN {COLUMN} TEXT NULL"

    print(f"执行: {ddl}")
    with engine.begin() as conn:
        conn.execute(text(ddl))
    print(f"✅ {TABLE}.{COLUMN} 添加完成")


if __name__ == "__main__":
    main()
