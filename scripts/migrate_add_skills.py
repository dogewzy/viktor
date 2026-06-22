"""Create viktor_skills table.

Usage:
    python scripts/migrate_add_skills.py
"""
from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy import inspect

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.database import engine  # noqa: E402
from core.models import SkillModel  # noqa: E402


def main() -> None:
    table_name = SkillModel.__tablename__
    inspector = inspect(engine)
    if table_name in inspector.get_table_names():
        print(f"{table_name} already exists")
        return
    SkillModel.__table__.create(bind=engine)
    print(f"created {table_name}")


if __name__ == "__main__":
    main()
