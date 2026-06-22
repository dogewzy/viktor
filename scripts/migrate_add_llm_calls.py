#!/usr/bin/env python3
"""迁移：新增 LLM 调用观测表。

用法:
    python scripts/migrate_add_llm_calls.py

幂等：表已存在时跳过。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import inspect

from core.database import engine
from core.models import LLMCallModel


TABLE = "viktor_llm_calls"


def main() -> None:
    inspector = inspect(engine)
    if TABLE in inspector.get_table_names():
        print(f"{TABLE} 已存在，跳过")
        return
    print(f"创建表: {TABLE}")
    LLMCallModel.__table__.create(bind=engine)
    print(f"{TABLE} 创建完成")


if __name__ == "__main__":
    main()
