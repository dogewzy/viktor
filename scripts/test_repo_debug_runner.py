#!/usr/bin/env python3
"""仓库调试脚本执行工具自测。

覆盖 repo_debug_runner 的快速验证能力：
- 允许在 workspace 内写临时 Python 复现脚本
- 允许执行 workspace 内 Python 脚本和通用命令
- 拒绝路径穿越
"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from core.registry import ProjectItem, registry


FAKE_PROJECT_ID = "repo-debug-runner-self"


def _seed_fake_project() -> None:
    registry.register_project(ProjectItem(
        id=FAKE_PROJECT_ID,
        name="Repo Debug Runner Self (Test)",
        description="仅用于 repo_debug_runner 自测，不落库",
        git_url="https://example.com/fake.git",
        default_branch="master",
        k8s_workload=None,
    ))


def _patch_ensure_workspace_to_self() -> None:
    import core.code_sync as code_sync
    import tools.repo_debug_runner as runner

    def _fake_ensure(project_id: str, commit_sha=None, connector_id=None, repo_connector_id=None):  # noqa: ARG001
        return PROJECT_ROOT

    code_sync.ensure_workspace = _fake_ensure
    runner.ensure_workspace = _fake_ensure


def test_run_repo_debug_script() -> None:
    from tools.repo_debug_runner import run_repo_command, run_repo_debug_script, write_repo_debug_file

    probe_path = "scripts/tmp_repo_debug_runner_probe.py"
    probe = write_repo_debug_file(
        FAKE_PROJECT_ID,
        probe_path,
        "import sys\nprint('probe-ok:' + '|'.join(sys.argv[1:]))\n",
    )
    assert "error" not in probe, probe
    assert probe["path"] == probe_path, probe

    ok = run_repo_debug_script(FAKE_PROJECT_ID, probe_path, args=["a", "b"], timeout_sec=10)
    assert "error" not in ok, ok
    assert ok["exit_code"] == 0, ok
    assert "probe-ok:a|b" in ok["stdout"], ok["stdout"]

    cmd = run_repo_command(FAKE_PROJECT_ID, [sys.executable, "-c", "print('cmd-ok')"], timeout_sec=10)
    assert "error" not in cmd, cmd
    assert cmd["exit_code"] == 0 and "cmd-ok" in cmd["stdout"], cmd

    traversal = run_repo_debug_script(FAKE_PROJECT_ID, "../scripts/test_repo_debug_runner.py")
    assert "error" in traversal and ".." in traversal["error"], traversal

    (PROJECT_ROOT / probe_path).unlink(missing_ok=True)


def main() -> int:
    _seed_fake_project()
    _patch_ensure_workspace_to_self()
    try:
        test_run_repo_debug_script()
        print("PASS: repo_debug_runner 临时写入与命令执行")
        return 0
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return 1
    finally:
        (PROJECT_ROOT / "scripts/tmp_repo_debug_runner_probe.py").unlink(missing_ok=True)
        registry.unregister_project(FAKE_PROJECT_ID)


if __name__ == "__main__":
    raise SystemExit(main())
