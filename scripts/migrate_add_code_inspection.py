#!/usr/bin/env python3
"""
迁移：代码自省能力（一期）所需的库表变更。

1) viktor_projects 增加 3 列（均可空，老项目零影响）：
   - git_url         VARCHAR(512) NULL
   - default_branch  VARCHAR(128) NULL DEFAULT 'master'
   - k8s_workload    JSON NULL     # {namespace, kind, name, container}

2) 新建 viktor_glossaries 表（业务术语表）：
   - 主键 (project_id, glossary_id)
   - term / aliases / code_keywords / description / enabled

用法:
    python scripts/migrate_add_code_inspection.py

幂等：列/表已存在时跳过。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import inspect, text

from core.database import engine


PROJECTS_TABLE = "viktor_projects"
GLOSSARIES_TABLE = "viktor_glossaries"

PROJECT_COLUMNS = [
    # (column_name, ddl_fragment)
    ("git_url",        "ADD COLUMN git_url VARCHAR(512) NULL AFTER description"),
    ("default_branch", "ADD COLUMN default_branch VARCHAR(128) NULL DEFAULT 'master' AFTER git_url"),
    ("k8s_workload",   "ADD COLUMN k8s_workload JSON NULL AFTER default_branch"),
]

GLOSSARY_DDL = f"""
CREATE TABLE IF NOT EXISTS {GLOSSARIES_TABLE} (
    project_id     VARCHAR(128) NOT NULL,
    glossary_id    VARCHAR(128) NOT NULL,
    term           VARCHAR(255) NOT NULL,
    aliases        JSON NOT NULL,
    code_keywords  JSON NOT NULL,
    description    VARCHAR(2048) NOT NULL DEFAULT '',
    enabled        TINYINT NOT NULL DEFAULT 1,
    created_at     DATETIME NULL,
    updated_at     DATETIME NULL,
    PRIMARY KEY (project_id, glossary_id),
    KEY idx_glossary_project (project_id),
    KEY idx_glossary_term (term)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
""".strip()


def alter_projects() -> None:
    inspector = inspect(engine)
    existing = {c["name"] for c in inspector.get_columns(PROJECTS_TABLE)}
    pending = [(col, ddl) for col, ddl in PROJECT_COLUMNS if col not in existing]
    if not pending:
        print(f"✅ {PROJECTS_TABLE}: 所有代码自省列已存在，跳过")
        return

    with engine.begin() as conn:
        for col, frag in pending:
            sql = f"ALTER TABLE {PROJECTS_TABLE} {frag}"
            print(f"执行: {sql}")
            conn.execute(text(sql))
            print(f"  ✅ {PROJECTS_TABLE}.{col} 添加完成")


def create_glossaries() -> None:
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    if GLOSSARIES_TABLE in tables:
        print(f"✅ {GLOSSARIES_TABLE} 已存在，跳过")
        return
    print(f"执行: CREATE TABLE {GLOSSARIES_TABLE}")
    with engine.begin() as conn:
        conn.execute(text(GLOSSARY_DDL))
    print(f"✅ {GLOSSARIES_TABLE} 创建完成")


def main() -> None:
    alter_projects()
    create_glossaries()
    print("🎉 代码自省一期迁移完成")


if __name__ == "__main__":
    main()
