"""仓库调试执行工具。

当前 Viktor 处于快速迭代阶段，这里优先支持 agent 像工程师一样在浅 clone
workspace 中写临时复现脚本、运行验证命令、读取 stdout/stderr。它不是代码自省
能力，也不会自动提交任何变更；最小边界是所有路径必须留在 workspace 内。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Optional

from core.code_sync import ensure_workspace
from settings import repo_debug_runner_config
from tools.repo_venv import venv_python_for_workspace


def _resolve_venv(workspace: Path, use_venv) -> Optional[Path]:
    """根据 use_venv 解析要使用的 venv 解释器。

    - "off" / False        : 不使用 venv，返回 None（沿用 Viktor 自身解释器）
    - "on" / "require" / True : 必须使用 venv，未建好则抛错提示先 setup_repo_venv
    - "auto"（默认）/其它     : venv 存在则用，不存在退回 None
    """
    mode = use_venv
    if isinstance(mode, bool):
        mode = "on" if mode else "off"
    mode = str(mode or "auto").strip().lower()

    if mode in ("off", "false", "no", "none"):
        return None
    venv_python = venv_python_for_workspace(workspace)
    if mode in ("on", "true", "yes", "require", "required"):
        if not venv_python:
            raise ValueError("该仓库尚未建立虚拟环境，请先调用 setup_repo_venv 安装依赖")
        return venv_python
    # auto
    return venv_python


def repo_debug_runner_policy_summary() -> str:
    return (
        f"enabled={repo_debug_runner_config.enabled}, "
        f"allow_write={repo_debug_runner_config.allow_write}, "
        f"timeout={repo_debug_runner_config.timeout_sec}/{repo_debug_runner_config.max_timeout_sec}s"
    )


def _resolve_connector_id(connector_id: Optional[str] = None, repo_connector_id: Optional[str] = None) -> str:
    connector_id = (connector_id or "").strip()
    repo_connector_id = (repo_connector_id or "").strip()
    if connector_id and repo_connector_id and connector_id != repo_connector_id:
        raise ValueError(
            f"connector_id({connector_id!r}) 与 repo_connector_id({repo_connector_id!r}) 不一致"
        )
    return connector_id or repo_connector_id


def _safe_resolve(workspace: Path, user_path: str, *, default: str = ".") -> Path:
    raw = (user_path or default).strip() or default
    raw_path = Path(raw)
    if raw_path.is_absolute():
        raise ValueError("路径必须是 workspace 内相对路径，不能是绝对路径")
    if ".." in raw_path.parts:
        raise ValueError("路径不能包含 '..'")

    candidate = (workspace / raw).resolve()
    try:
        candidate.relative_to(workspace.resolve())
    except ValueError:
        raise ValueError(f"路径 {user_path!r} 越出 workspace 范围") from None
    return candidate


def _to_relative(workspace: Path, abs_path: Path) -> str:
    try:
        return str(abs_path.relative_to(workspace))
    except ValueError:
        return str(abs_path)


def _workspace(project_id: str, connector_id: Optional[str], repo_connector_id: Optional[str]) -> tuple[Path, str]:
    effective_connector_id = _resolve_connector_id(connector_id, repo_connector_id)
    ws = ensure_workspace(project_id, connector_id=effective_connector_id or None)
    return ws, effective_connector_id


def _runner_env(
    workspace: Path,
    extra_env: Optional[dict[str, str]] = None,
    venv_python: Optional[Path] = None,
) -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    existing_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(workspace) if not existing_pythonpath else f"{workspace}{os.pathsep}{existing_pythonpath}"
    env.setdefault("NO_COLOR", "1")
    if venv_python is not None:
        # 把 venv/bin 前插 PATH，让 python/pip/pytest 等解析到 venv；标准 venv 约定。
        bin_dir = str(Path(venv_python).parent)
        env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")
        env["VIRTUAL_ENV"] = str(Path(venv_python).parent.parent)
        env.pop("PYTHONHOME", None)
    for key, value in (extra_env or {}).items():
        if isinstance(key, str) and isinstance(value, str):
            env[key] = value
    return env


def _clamp_int(raw: int, default: int, lower: int, upper: int) -> int:
    try:
        value = int(raw or default)
    except (TypeError, ValueError):
        value = default
    return min(max(value, lower), upper)


def _truncate_output(text: str | bytes | None, max_chars: int) -> tuple[str, bool]:
    if text is None:
        return "", False
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars] + "\n...(truncated)", True


def _limits(timeout_sec: int, max_chars: int) -> tuple[int, int]:
    timeout = _clamp_int(
        timeout_sec,
        repo_debug_runner_config.timeout_sec,
        1,
        repo_debug_runner_config.max_timeout_sec,
    )
    output_limit = _clamp_int(
        max_chars,
        repo_debug_runner_config.output_chars,
        1000,
        repo_debug_runner_config.max_output_chars,
    )
    return timeout, output_limit


def write_repo_debug_file(
    project_id: str,
    path: str,
    content: str,
    overwrite: bool = True,
    connector_id: Optional[str] = None,
    repo_connector_id: Optional[str] = None,
) -> dict:
    """在仓库 workspace 中写入临时复现/验证文件。"""
    if not repo_debug_runner_config.enabled:
        return {"error": "repo_debug_runner 已关闭", "path": path}
    if not repo_debug_runner_config.allow_write:
        return {"error": "repo_debug_runner 写入能力已关闭", "path": path}
    if not isinstance(content, str):
        return {"error": "content 必须是字符串", "path": path}

    try:
        ws, effective_connector_id = _workspace(project_id, connector_id, repo_connector_id)
        target = _safe_resolve(ws, path)
        existed = target.exists()
        if existed and not overwrite:
            return {
                "error": f"文件已存在且 overwrite=false: {_to_relative(ws, target)}",
                "workspace": str(ws),
                "repo_connector_id": effective_connector_id,
                "path": _to_relative(ws, target),
            }
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return {
            "workspace": str(ws),
            "repo_connector_id": effective_connector_id,
            "path": _to_relative(ws, target),
            "bytes": len(content.encode("utf-8")),
            "created": not existed,
            "overwritten": existed,
        }
    except Exception as e:  # noqa: BLE001
        return {"error": str(e), "path": path}


def run_repo_command(
    project_id: str,
    command: list[str],
    cwd: str = "",
    timeout_sec: int = 0,
    max_chars: int = 0,
    extra_env: Optional[dict[str, str]] = None,
    connector_id: Optional[str] = None,
    repo_connector_id: Optional[str] = None,
    use_venv="auto",
) -> dict:
    """在仓库 workspace 内执行命令 argv。需要 shell 时可显式传 `bash -lc ...`。

    use_venv: "auto"(默认，venv 存在则把 venv/bin 前插 PATH) / "on"(强制，未建报错) / "off"。
    """
    if not repo_debug_runner_config.enabled:
        return {"error": "repo_debug_runner 已关闭", "command": command}
    if not isinstance(command, list) or not command or any(not isinstance(arg, str) for arg in command):
        return {"error": "command 必须是非空字符串数组", "command": command}

    try:
        ws, effective_connector_id = _workspace(project_id, connector_id, repo_connector_id)
        venv_python = _resolve_venv(ws, use_venv)
        workdir = _safe_resolve(ws, cwd or ".")
        if not workdir.is_dir():
            return {
                "error": f"cwd 不是目录: {_to_relative(ws, workdir)}",
                "workspace": str(ws),
                "repo_connector_id": effective_connector_id,
                "cwd": _to_relative(ws, workdir),
                "command": command,
            }
        timeout, output_limit = _limits(timeout_sec, max_chars)
        result = subprocess.run(
            command,
            cwd=workdir,
            env=_runner_env(ws, extra_env, venv_python),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        stdout, stdout_truncated = _truncate_output(result.stdout, output_limit)
        stderr, stderr_truncated = _truncate_output(result.stderr, output_limit)
        return {
            "workspace": str(ws),
            "repo_connector_id": effective_connector_id,
            "cwd": _to_relative(ws, workdir),
            "command": command,
            "venv": str(venv_python.parent.parent) if venv_python else None,
            "exit_code": result.returncode,
            "timed_out": False,
            "timeout_sec": timeout,
            "stdout": stdout,
            "stderr": stderr,
            "truncated": {
                "stdout": stdout_truncated,
                "stderr": stderr_truncated,
            },
        }
    except subprocess.TimeoutExpired as e:
        stdout, stdout_truncated = _truncate_output(e.stdout, max_chars or repo_debug_runner_config.output_chars)
        stderr, stderr_truncated = _truncate_output(e.stderr, max_chars or repo_debug_runner_config.output_chars)
        return {
            "workspace": str(ws) if "ws" in locals() else "",
            "repo_connector_id": effective_connector_id if "effective_connector_id" in locals() else "",
            "cwd": _to_relative(ws, workdir) if "ws" in locals() and "workdir" in locals() else cwd,
            "command": command,
            "exit_code": None,
            "timed_out": True,
            "timeout_sec": timeout if "timeout" in locals() else timeout_sec,
            "stdout": stdout,
            "stderr": stderr,
            "truncated": {
                "stdout": stdout_truncated,
                "stderr": stderr_truncated,
            },
        }
    except Exception as e:  # noqa: BLE001
        return {"error": str(e), "command": command}


def run_repo_debug_script(
    project_id: str,
    script_path: str,
    args: Optional[list[str]] = None,
    timeout_sec: int = 0,
    max_chars: int = 0,
    connector_id: Optional[str] = None,
    repo_connector_id: Optional[str] = None,
    use_venv="auto",
) -> dict:
    """运行仓库 workspace 内的 Python 脚本。

    use_venv="auto"(默认)：仓库已建好 venv 则用其解释器（含项目三方依赖），否则退回
    Viktor 自身解释器；"on" 强制要求 venv（未建报错）；"off" 始终用 Viktor 解释器。
    """
    argv = args or []
    if not isinstance(argv, list) or not all(isinstance(arg, str) for arg in argv):
        return {"error": "args 必须是字符串数组", "script_path": script_path}

    try:
        ws, effective_connector_id = _workspace(project_id, connector_id, repo_connector_id)
        venv_python = _resolve_venv(ws, use_venv)
        target = _safe_resolve(ws, script_path)
        if not target.is_file():
            return {
                "error": f"脚本不存在: {_to_relative(ws, target)}",
                "workspace": str(ws),
                "repo_connector_id": effective_connector_id,
                "script_path": _to_relative(ws, target),
            }
        rel_script = _to_relative(ws, target)
    except Exception as e:  # noqa: BLE001
        return {"error": str(e), "script_path": script_path}

    interpreter = str(venv_python) if venv_python else sys.executable
    result = run_repo_command(
        project_id,
        [interpreter, rel_script, *argv],
        timeout_sec=timeout_sec,
        max_chars=max_chars,
        connector_id=connector_id,
        repo_connector_id=repo_connector_id,
        use_venv=use_venv,
    )
    if "script_path" not in result:
        result["script_path"] = rel_script
    if not venv_python and not result.get("error"):
        # 退回 Viktor 解释器：提示 agent 若遇到 ModuleNotFoundError 可先建 venv。
        result["venv_hint"] = (
            "当前用 Viktor 自身解释器执行（无项目三方依赖）。若脚本 import 项目依赖失败，"
            "请先调用 setup_repo_venv 安装该仓库依赖后重试。"
        )
    return result
