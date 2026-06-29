"""可写 Coding Workspace：为 coding task clone 仓库、创建分支并读取 diff。"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlparse

from settings import coding_agent_config, gitlab_config


@dataclass
class WorkspaceInfo:
    path: Path
    branch: str
    base_commit: str
    git_url: str
    target_branch: str


def _workspace_root() -> Path:
    root = Path(os.path.expanduser(coding_agent_config.workspace_root)).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _git_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    env.setdefault("GIT_ASKPASS", "/bin/true")
    env.setdefault("GIT_AUTHOR_NAME", coding_agent_config.git_author_name)
    env.setdefault("GIT_AUTHOR_EMAIL", coding_agent_config.git_author_email)
    env.setdefault("GIT_COMMITTER_NAME", coding_agent_config.git_author_name)
    env.setdefault("GIT_COMMITTER_EMAIL", coding_agent_config.git_author_email)
    return env


def _git_identity_args() -> list[str]:
    return [
        "-c",
        f"user.name={coding_agent_config.git_author_name}",
        "-c",
        f"user.email={coding_agent_config.git_author_email}",
    ]


def inject_git_credentials(git_url: str) -> str:
    token = gitlab_config.token_for_repo_url(git_url)
    if not token:
        return git_url
    raw = gitlab_config.access_url_for_repo_url(git_url)
    if raw.startswith("git@") or raw.startswith("ssh://"):
        return raw
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or "@" in parsed.netloc:
        return raw
    auth = f"oauth2:{quote(token, safe='')}"
    return parsed._replace(netloc=f"{auth}@{parsed.netloc}").geturl()


def run_git(args: list[str], cwd: Path | None = None, timeout: int | None = None) -> str:
    res = subprocess.run(
        [coding_agent_config.git_binary, *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=timeout or coding_agent_config.task_timeout_sec,
        env=_git_env(),
    )
    if res.returncode != 0:
        raise RuntimeError(res.stderr.strip() or res.stdout.strip() or f"git {args[0]} failed")
    return res.stdout.strip()


def _run_git_allow_exit_codes(
    args: list[str],
    *,
    cwd: Path,
    allowed_exit_codes: set[int],
    timeout: int | None = None,
) -> str:
    res = subprocess.run(
        [coding_agent_config.git_binary, *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout or coding_agent_config.task_timeout_sec,
        env=_git_env(),
    )
    if res.returncode not in allowed_exit_codes:
        raise RuntimeError(res.stderr.strip() or res.stdout.strip() or f"git {args[0]} failed")
    return res.stdout.strip()


def prepare_workspace(
    *,
    task_id: str,
    git_url: str,
    target_branch: str,
    work_branch: str = "",
) -> WorkspaceInfo:
    safe_task = re.sub(r"[^a-zA-Z0-9_.-]+", "-", task_id).strip("-") or task_id
    branch = work_branch or f"viktor/{safe_task}"
    dst = _workspace_root() / safe_task / "repo"
    if (dst / ".git").exists():
        current_branch = run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=dst, timeout=30)
        if current_branch != branch:
            run_git(["checkout", branch], cwd=dst, timeout=30)
        base_commit = run_git(["rev-parse", "HEAD"], cwd=dst, timeout=30)
        return WorkspaceInfo(
            path=dst,
            branch=branch,
            base_commit=base_commit,
            git_url=git_url,
            target_branch=target_branch,
        )
    if dst.exists():
        shutil.rmtree(dst, ignore_errors=True)
    dst.parent.mkdir(parents=True, exist_ok=True)

    url = inject_git_credentials(git_url)
    run_git(["clone", "--branch", target_branch, "--single-branch", url, str(dst)])
    base_commit = run_git(["rev-parse", "HEAD"], cwd=dst, timeout=30)
    run_git(["checkout", "-b", branch], cwd=dst, timeout=30)
    return WorkspaceInfo(
        path=dst,
        branch=branch,
        base_commit=base_commit,
        git_url=git_url,
        target_branch=target_branch,
    )


def git_status(workspace: Path) -> str:
    return run_git(["status", "--short"], cwd=workspace, timeout=30)


def git_diff(workspace: Path, *, max_chars: int = 80_000) -> str:
    parts: list[str] = []
    diff = run_git(["diff", "--no-ext-diff", "HEAD", "--"], cwd=workspace, timeout=60)
    if diff:
        parts.append(diff)

    untracked = run_git(["ls-files", "--others", "--exclude-standard", "-z"], cwd=workspace, timeout=30)
    for rel in [p for p in untracked.split("\0") if p]:
        file_diff = _run_git_allow_exit_codes(
            ["diff", "--no-ext-diff", "--no-index", "--", "/dev/null", rel],
            cwd=workspace,
            allowed_exit_codes={0, 1},
            timeout=30,
        )
        if file_diff:
            parts.append(file_diff)

    diff = "\n\n".join(parts)
    if len(diff) > max_chars:
        return diff[:max_chars] + f"\n... diff truncated, original chars={len(diff)}"
    return diff


def git_cumulative_diff(workspace: Path, target_branch: str, *, max_chars: int = 80_000) -> str:
    """Return the cumulative branch diff against the target branch."""
    ref = f"origin/{target_branch}...HEAD"
    diff = run_git(["diff", "--no-ext-diff", ref, "--"], cwd=workspace, timeout=60)
    if len(diff) > max_chars:
        return diff[:max_chars] + f"\n... diff truncated, original chars={len(diff)}"
    return diff


def changed_files(workspace: Path) -> list[str]:
    out = run_git(["status", "--short"], cwd=workspace, timeout=30)
    files: list[str] = []
    for line in out.splitlines():
        if not line.strip():
            continue
        path = line[3:].strip() if len(line) > 2 and line[2] == " " else line[2:].strip()
        if " -> " in path:
            path = path.split(" -> ", 1)[1].strip()
        files.append(path)
    return files


def commit_all(workspace: Path, message: str) -> str:
    run_git(["add", "--all"], cwd=workspace, timeout=60)
    if not git_status(workspace):
        return ""
    run_git([*_git_identity_args(), "commit", "-m", message], cwd=workspace, timeout=120)
    return run_git(["rev-parse", "HEAD"], cwd=workspace, timeout=30)


def push_branch(workspace: Path, branch: str) -> None:
    run_git(["push", "-u", "origin", branch], cwd=workspace, timeout=300)
