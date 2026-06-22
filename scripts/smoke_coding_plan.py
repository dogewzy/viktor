"""Smoke test coding task planning without using the browser UI.

Default behavior:
  - load the latest failed coding task from Viktor DB
  - reuse its project/repo/branch/requirement
  - build the same project context as a real coding task
  - ask the coding LLM for a planning-only response

Run in pod:
  python scripts/smoke_coding_plan.py
  python scripts/smoke_coding_plan.py --task-id ct_xxx
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _latest_failed_task() -> Any | None:
    from core.database import SessionLocal
    from core.models import CodingTaskModel

    db = SessionLocal()
    try:
        return (
            db.query(CodingTaskModel)
            .filter(CodingTaskModel.status == "failed")
            .order_by(CodingTaskModel.updated_at.desc(), CodingTaskModel.created_at.desc())
            .first()
        )
    finally:
        db.close()


def _get_task(task_id: str) -> Any | None:
    from core.database import SessionLocal
    from core.models import CodingTaskModel

    db = SessionLocal()
    try:
        return db.get(CodingTaskModel, task_id)
    finally:
        db.close()


async def _main() -> int:
    parser = argparse.ArgumentParser(description="Run a planning-only smoke test for a coding task.")
    parser.add_argument("--task-id", default="", help="Coding task id. Defaults to latest failed task.")
    parser.add_argument("--project-id", default="", help="Override project id.")
    parser.add_argument("--requirement", default="", help="Override requirement text.")
    args = parser.parse_args()

    task = _get_task(args.task_id) if args.task_id else _latest_failed_task()
    if not task and not (args.project_id and args.requirement):
        print("No failed coding task found. Pass --project-id and --requirement, or --task-id.", file=sys.stderr)
        return 2

    project_id = args.project_id or task.project_id
    requirement = args.requirement or task.requirement
    task_id = task.task_id if task else "<manual>"

    print(f"[smoke] task_id={task_id}")
    print(f"[smoke] project_id={project_id}")
    if task:
        print(f"[smoke] repo_connector_id={task.repo_connector_id}")
        print(f"[smoke] target_branch={task.target_branch}")
    print("[smoke] building project context...")
    from core.coding_agent_loop import run_coding_plan
    from core.prompt_builder import build_system_prompt

    project_context = await build_system_prompt(project_id, requirement, enable_routing=True)
    print(f"[smoke] context_chars={len(project_context)}")
    print("[smoke] requesting planning-only output...")
    plan = await run_coding_plan(requirement=requirement, project_context=project_context)
    if not plan.strip():
        print("[smoke] empty plan output", file=sys.stderr)
        return 1

    print("\n===== CODING PLAN SMOKE OUTPUT =====\n")
    print(plan)
    print("\n===== END =====")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
