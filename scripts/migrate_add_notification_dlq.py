#!/usr/bin/env python3
"""
迁移：新建 viktor_notification_dlq 表（失败的钉钉通知死信队列）。

表结构参见 core.models.NotificationDLQModel。
幂等：表已存在时跳过。

用法:
    python scripts/migrate_add_notification_dlq.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import inspect

from core.database import engine
from core.models import NotificationDLQModel


TABLE = "viktor_notification_dlq"


def main() -> None:
    inspector = inspect(engine)
    if TABLE in inspector.get_table_names():
        print(f"✅ {TABLE} 已存在，跳过")
        return

    print(f"创建表: {TABLE}")
    NotificationDLQModel.__table__.create(bind=engine)
    print(f"✅ {TABLE} 创建完成")


if __name__ == "__main__":
    main()
