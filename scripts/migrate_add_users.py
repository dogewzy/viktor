#!/usr/bin/env python3
"""迁移：新建 viktor_users 表（网页控制台登录用户）。

表结构见 core.models.UserModel：用户名密码 + 手机号 + 角色。
连接走 core.database.engine，复用 settings 里的 ${VIKTOR_DB_*} 环境变量。

用法:
    python scripts/migrate_add_users.py

幂等：表已存在时跳过。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import inspect

from core.database import engine
from core.models import UserModel

TABLE = "viktor_users"


def main() -> None:
    inspector = inspect(engine)
    if TABLE in inspector.get_table_names():
        print(f"✅ {TABLE} 已存在，跳过")
        return
    print(f"创建表: {TABLE}")
    UserModel.__table__.create(bind=engine)
    print(f"✅ {TABLE} 创建完成")


if __name__ == "__main__":
    main()
