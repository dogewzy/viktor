#!/usr/bin/env python3
"""
迁移：为 viktor_chat_messages 增加 topic_thread_id（议题段）。

- DDL：ADD COLUMN（可空）→ 回填 → MODIFY NOT NULL
- 回填：按 thread_id（session）写入 legacy_topic_thread_id(session)
- 尝试创建复合索引 ix_chat_session_topic（已存在则忽略）

用法:
    python scripts/migrate_add_topic_thread_id.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import inspect, text

from core.database import engine
from core.memory import legacy_topic_thread_id


TABLE = "viktor_chat_messages"
COL = "topic_thread_id"
INDEX_NAME = "ix_chat_session_topic"


def _has_column(table: str, column: str) -> bool:
    insp = inspect(engine)
    if table not in insp.get_table_names():
        return False
    return any(c["name"] == column for c in insp.get_columns(table))


def main() -> None:
    if TABLE not in inspect(engine).get_table_names():
        print(f"❌ 表 {TABLE} 不存在，请先运行 migrate_add_chat_messages.py")
        sys.exit(1)

    dialect = engine.dialect.name

    with engine.begin() as conn:
        if not _has_column(TABLE, COL):
            print(f"添加列 {TABLE}.{COL} ...")
            if dialect == "mysql":
                conn.execute(
                    text(
                        f"ALTER TABLE {TABLE} ADD COLUMN {COL} VARCHAR(128) NULL"
                    )
                )
            else:
                conn.execute(
                    text(
                        f"ALTER TABLE {TABLE} ADD COLUMN {COL} VARCHAR(128) DEFAULT '' NOT NULL"
                    )
                )
            print("✅ 已添加列")
        else:
            print(f"✅ 列 {COL} 已存在")

    with engine.begin() as conn:
        n = conn.execute(
            text(
                f"SELECT COUNT(*) FROM {TABLE} WHERE {COL} IS NULL OR {COL} = ''"
            )
        ).scalar()
        if n and int(n) > 0:
            print(f"回填 {COL}（{int(n)} 行）...")
            rows = conn.execute(
                text(f"SELECT DISTINCT thread_id FROM {TABLE}")
            ).fetchall()
            for (sid,) in rows:
                tid = legacy_topic_thread_id(sid)
                conn.execute(
                    text(
                        f"UPDATE {TABLE} SET {COL} = :tid "
                        f"WHERE thread_id = :sid AND ({COL} IS NULL OR {COL} = '')"
                    ),
                    {"tid": tid, "sid": sid},
                )
            print("✅ 回填完成")
        else:
            print("✅ 无待回填行")

    if dialect == "mysql":
        with engine.begin() as conn:
            conn.execute(
                text(
                    f"ALTER TABLE {TABLE} MODIFY COLUMN {COL} VARCHAR(128) NOT NULL"
                )
            )
        print("✅ 已设 NOT NULL")
        with engine.begin() as conn:
            try:
                conn.execute(
                    text(
                        f"CREATE INDEX {INDEX_NAME} ON {TABLE} "
                        f"(thread_id, {COL}, created_at)"
                    )
                )
                print(f"✅ 已创建索引 {INDEX_NAME}")
            except Exception as e:  # noqa: BLE001
                msg = str(e).lower()
                if "duplicate" in msg or "already exists" in msg:
                    print(f"ℹ️ 索引 {INDEX_NAME} 已存在，跳过")
                else:
                    print(f"⚠️ 创建索引失败（可忽略）: {e}")


if __name__ == "__main__":
    main()
