#!/usr/bin/env python3
"""迁移脚本：新增 viktor_repository_connectors 表，并将现有项目的 git_url 迁移为默认 Repository Connector。

用法:
    python scripts/migrate_add_repository_connectors.py

幂等：重复运行不会报错或产生重复数据。
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import text
from core.database import engine, SessionLocal
from core.models import Base, RepositoryConnectorModel


def main():
    print("=== 迁移：新增 viktor_repository_connectors 表 ===")

    with engine.connect() as conn:
        result = conn.execute(text(
            "SELECT COUNT(*) FROM information_schema.tables "
            "WHERE table_schema = DATABASE() AND table_name = 'viktor_repository_connectors'"
        ))
        exists = result.scalar() > 0

    if exists:
        print("表 viktor_repository_connectors 已存在，跳过建表。")
    else:
        RepositoryConnectorModel.__table__.create(engine)
        print("表 viktor_repository_connectors 创建成功。")

    # 将现有项目的 git_url 迁移为 default Repository Connector
    session = SessionLocal()
    try:
        rows = session.execute(text(
            "SELECT project_id, git_url, default_branch, k8s_workload "
            "FROM viktor_projects WHERE git_url IS NOT NULL AND git_url != ''"
        )).fetchall()

        migrated = 0
        for row in rows:
            project_id, git_url, default_branch, k8s_workload = row
            existing = session.execute(text(
                "SELECT 1 FROM viktor_repository_connectors "
                "WHERE project_id = :pid AND connector_id = 'default'"
            ), {"pid": project_id}).fetchone()

            if existing:
                continue

            session.execute(text(
                "INSERT INTO viktor_repository_connectors "
                "(project_id, connector_id, display_name, git_url, default_branch, k8s_workload, sort_order) "
                "VALUES (:pid, 'default', :name, :url, :branch, :workload, 0)"
            ), {
                "pid": project_id,
                "name": f"{project_id} (default)",
                "url": git_url,
                "branch": default_branch or "master",
                "workload": k8s_workload,
            })
            migrated += 1

        session.commit()
        print(f"迁移完成：{migrated} 个项目的 git_url 已转为 default Repository Connector。")
        print(f"（共 {len(rows)} 个项目有 git_url，{len(rows) - migrated} 个已有 default Repository Connector 跳过）")
    finally:
        session.close()


if __name__ == "__main__":
    main()
