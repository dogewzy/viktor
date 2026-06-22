#!/usr/bin/env python3
"""
迁移：新建 viktor_knowledge_notes 表（业务知识笔记：字段约定/语义/坑位/指标定义）。

表结构参见 core.models.KnowledgeNoteModel。
幂等：表已存在时跳过。

用法:
    python scripts/migrate_add_knowledge_notes.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import inspect

from core.database import engine
from core.models import KnowledgeNoteModel


TABLE = "viktor_knowledge_notes"


def main() -> None:
    inspector = inspect(engine)
    if TABLE in inspector.get_table_names():
        print(f"✅ {TABLE} 已存在，跳过")
        return

    print(f"创建表: {TABLE}")
    KnowledgeNoteModel.__table__.create(bind=engine)
    print(f"✅ {TABLE} 创建完成")


if __name__ == "__main__":
    main()
