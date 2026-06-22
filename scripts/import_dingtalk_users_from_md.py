#!/usr/bin/env python3
"""Import DingTalk phonebook Markdown into viktor_users.

The importer is intentionally destructive when run with --execute:
it clears viktor_users, ensures the DingTalk profile columns exist, then inserts
one active user per Markdown row. Imported users are marked password_set=0 so a
later activation flow can require password setup.
"""
from __future__ import annotations

import argparse
import glob
import re
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import inspect, text

from core.database import SessionLocal, engine
from core.models import UserModel

TABLE = "viktor_users"
NO_PASSWORD_HASH = "!dingtalk-activation-required!"


@dataclass(frozen=True)
class ImportedUser:
    name: str
    mobile: str
    userid: str
    departments: list[str]
    profile_key: str
    legacy_role: str


def _split_md_cells(line: str) -> list[str]:
    line = line.strip()
    if not line.startswith("|") or not line.endswith("|"):
        return []
    text = line[1:-1]
    cells: list[str] = []
    buf: list[str] = []
    escaped = False
    for ch in text:
        if escaped:
            buf.append(ch)
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == "|":
            cells.append("".join(buf).strip())
            buf = []
            continue
        buf.append(ch)
    cells.append("".join(buf).strip())
    return cells


def _is_separator(cells: list[str]) -> bool:
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell.strip()) for cell in cells)


def _normalize_mobile(value: str) -> str:
    raw = str(value or "").strip()
    raw = re.sub(r"[\s\-()]+", "", raw)
    if raw.startswith("+"):
        return "+" + re.sub(r"\D", "", raw[1:])
    return re.sub(r"\D", "", raw)


def _split_departments(value: str) -> list[str]:
    parts = [p.strip() for p in re.split(r"\s*;\s*", str(value or "").strip()) if p.strip()]
    return list(dict.fromkeys(parts))


def _profile_from_departments(departments: list[str]) -> str:
    text_value = " ".join(departments).lower()
    if any(key in text_value for key in ("qa", "test", "testing", "quality", "测试", "质控", "质量")):
        return "qa"
    if any(key in text_value for key in ("product", "pm", "产品")):
        return "product"
    if any(
        key in text_value
        for key in (
            "dev",
            "developer",
            "development",
            "engineering",
            "engineer",
            "backend",
            "frontend",
            "platform",
            "研发",
            "开发",
            "工程",
            "技术",
            "架构",
            "算法",
            "数据",
            "平台",
        )
    ):
        return "developer"
    if any(key in text_value for key in ("operation", "operations", "ops", "运营", "客服", "销售", "support")):
        return "operations"
    return "operations"


def _latest_default_markdown() -> Path | None:
    root = Path(__file__).resolve().parent.parent.parent
    matches = sorted(glob.glob(str(root / "dingtalk_users_phonebook_with_department_*.md")))
    return Path(matches[-1]) if matches else None


def parse_markdown(path: Path) -> list[ImportedUser]:
    rows: list[ImportedUser] = []
    header: list[str] | None = None
    index: dict[str, int] = {}

    for line in path.read_text(encoding="utf-8").splitlines():
        cells = _split_md_cells(line)
        if not cells or _is_separator(cells):
            continue
        if header is None:
            if {"姓名", "手机号", "userid"}.issubset(set(cells)):
                header = cells
                index = {name: i for i, name in enumerate(header)}
            continue
        if len(cells) < len(header):
            continue

        name = cells[index["姓名"]].strip()
        mobile = _normalize_mobile(cells[index["手机号"]])
        userid = cells[index["userid"]].strip()
        department_text = cells[index["部门"]].strip() if "部门" in index else ""
        departments = _split_departments(department_text)
        profile = _profile_from_departments(departments)
        rows.append(
            ImportedUser(
                name=name,
                mobile=mobile,
                userid=userid,
                departments=departments,
                profile_key=profile,
                legacy_role=profile,
            )
        )

    if header is None:
        raise ValueError("未找到包含 姓名 / 手机号 / userid 的 Markdown 表头")
    if not rows:
        raise ValueError("Markdown 表格中没有可导入用户")
    return rows


def validate_users(users: list[ImportedUser]) -> None:
    missing_mobile = sum(1 for user in users if not user.mobile)
    missing_name = sum(1 for user in users if not user.name)
    if missing_mobile or missing_name:
        raise ValueError(f"存在缺少姓名或手机号的行：missing_name={missing_name}, missing_mobile={missing_mobile}")

    mobiles: dict[str, int] = {}
    userids: dict[str, int] = {}
    for user in users:
        mobiles[user.mobile] = mobiles.get(user.mobile, 0) + 1
        if user.userid:
            userids[user.userid] = userids.get(user.userid, 0) + 1

    duplicate_mobiles = sum(1 for count in mobiles.values() if count > 1)
    duplicate_userids = sum(1 for count in userids.values() if count > 1)
    if duplicate_mobiles or duplicate_userids:
        raise ValueError(f"存在重复身份字段：duplicate_mobiles={duplicate_mobiles}, duplicate_userids={duplicate_userids}")


def _columns() -> set[str]:
    return {c["name"] for c in inspect(engine).get_columns(TABLE)}


def _indexes() -> list[dict]:
    return inspect(engine).get_indexes(TABLE)


def _has_unique_index(columns: list[str]) -> bool:
    wanted = tuple(columns)
    for idx in _indexes():
        if tuple(idx.get("column_names") or []) == wanted and bool(idx.get("unique")):
            return True
    return False


def _has_index(name: str) -> bool:
    return any(idx.get("name") == name for idx in _indexes())


def _execute(sql: str) -> None:
    with engine.begin() as conn:
        conn.execute(text(sql))


def ensure_profile_columns() -> None:
    cols = _columns()
    ddl = [
        ("password_set", "ALTER TABLE viktor_users ADD COLUMN password_set TINYINT NOT NULL DEFAULT 1 AFTER password_hash"),
        ("dingtalk_userid", "ALTER TABLE viktor_users ADD COLUMN dingtalk_userid VARCHAR(128) NOT NULL DEFAULT '' AFTER mobile"),
        ("department_paths", "ALTER TABLE viktor_users ADD COLUMN department_paths JSON NULL AFTER dingtalk_userid"),
        ("primary_department", "ALTER TABLE viktor_users ADD COLUMN primary_department VARCHAR(512) NOT NULL DEFAULT '' AFTER department_paths"),
        ("profile_key", "ALTER TABLE viktor_users ADD COLUMN profile_key VARCHAR(32) NOT NULL DEFAULT '' AFTER primary_department"),
        ("auth_source", "ALTER TABLE viktor_users ADD COLUMN auth_source VARCHAR(32) NOT NULL DEFAULT 'local' AFTER profile_key"),
    ]
    for column, sql in ddl:
        if column not in cols:
            print(f"schema: add column {column}")
            _execute(sql)
            cols.add(column)

    if not _has_index("ix_viktor_users_dingtalk_userid"):
        print("schema: add index ix_viktor_users_dingtalk_userid")
        _execute("ALTER TABLE viktor_users ADD INDEX ix_viktor_users_dingtalk_userid (dingtalk_userid)")


def ensure_unique_mobile_index() -> None:
    if _has_unique_index(["mobile"]):
        return
    if not _has_index("ux_viktor_users_mobile"):
        print("schema: add unique index ux_viktor_users_mobile")
        _execute("ALTER TABLE viktor_users ADD UNIQUE INDEX ux_viktor_users_mobile (mobile)")


def import_users(users: list[ImportedUser]) -> dict[str, int]:
    db = SessionLocal()
    try:
        before = db.query(UserModel).count()
        db.query(UserModel).delete(synchronize_session=False)
        db.commit()
        ensure_unique_mobile_index()
        for user in users:
            primary_department = user.departments[0] if user.departments else ""
            db.add(
                UserModel(
                    username=user.mobile,
                    password_hash=NO_PASSWORD_HASH,
                    password_set=0,
                    role=user.legacy_role,
                    display_name=user.name,
                    mobile=user.mobile,
                    dingtalk_userid=user.userid,
                    department_paths=user.departments,
                    primary_department=primary_department,
                    profile_key=user.profile_key,
                    auth_source="dingtalk",
                    is_active=1,
                )
            )
        db.commit()
        after = db.query(UserModel).count()
        return {"before": before, "deleted": before, "inserted": len(users), "after": after}
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def summarize(users: list[ImportedUser]) -> dict[str, object]:
    profiles: dict[str, int] = {}
    with_departments = 0
    for user in users:
        profiles[user.profile_key] = profiles.get(user.profile_key, 0) + 1
        if user.departments:
            with_departments += 1
    return {
        "rows": len(users),
        "with_mobile": sum(1 for user in users if user.mobile),
        "with_department": with_departments,
        "profiles": dict(sorted(profiles.items())),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Import DingTalk phonebook Markdown into viktor_users")
    default_path = _latest_default_markdown()
    parser.add_argument("markdown", nargs="?", default=str(default_path) if default_path else "")
    parser.add_argument("--execute", action="store_true", help="清空并导入 viktor_users；不加时只做 dry-run")
    args = parser.parse_args()

    if not args.markdown:
        raise SystemExit("未指定 Markdown 文件，且未找到 dingtalk_users_phonebook_with_department_*.md")
    path = Path(args.markdown).expanduser().resolve()
    users = parse_markdown(path)
    validate_users(users)
    summary = summarize(users)
    print(
        "parsed: "
        f"rows={summary['rows']} "
        f"with_mobile={summary['with_mobile']} "
        f"with_department={summary['with_department']} "
        f"profiles={summary['profiles']}"
    )

    if not args.execute:
        print("dry-run: no database changes. Pass --execute to clear and import viktor_users.")
        return

    ensure_profile_columns()
    result = import_users(users)
    print(
        "imported: "
        f"before={result['before']} "
        f"deleted={result['deleted']} "
        f"inserted={result['inserted']} "
        f"after={result['after']}"
    )


if __name__ == "__main__":
    main()
