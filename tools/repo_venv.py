"""仓库虚拟环境管理器。

`run_repo_debug_script` 默认使用 Viktor 自己的解释器，只有 Viktor 的依赖，没有
目标项目 requirements 里的三方包；agent 写的复现脚本一旦 `import` 项目三方依赖就
会 ModuleNotFoundError。本模块为每个仓库懒加载一个隔离 venv 并安装其依赖，供脚本
执行链路复用。

关键设计：
- **跨 commit 复用**：venv 放在 per-sha workspace 的父目录（`_repo_dir`/`_project_dir`），
  同一仓库所有 commit 共享一个 venv，按依赖文件指纹决定是否重装；LRU 只清理带 `.git`
  的 sha 目录，不会误删 venv。
- **显式触发**：依赖安装可能很慢（分钟级），由独立工具 `setup_repo_venv` 触发并带长
  超时；脚本/命令执行只复用已建好的 venv，绝不在脚本执行的短超时里隐式安装。
- **指纹去重**：对 requirements*.txt / pyproject.toml 等内容算 sha256，未变更则跳过安装。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from core.code_sync import _project_dir, _repo_dir, ensure_workspace
from settings import repo_venv_config

# 依赖文件探测顺序（top-level）。requirements*.txt 全部安装；pyproject/setup 仅在
# install_project=True 时做 editable 安装（源码已在 PYTHONPATH，import 项目包通常无需安装）。
_REQUIREMENTS_GLOB = "requirements*.txt"
_PROJECT_DEP_FILES = ("pyproject.toml", "setup.py", "setup.cfg", "Pipfile")
_MARKER_NAME = ".viktor_venv.json"


def repo_venv_policy_summary() -> str:
    return (
        f"enabled={repo_venv_config.enabled}, "
        f"auto_install={repo_venv_config.auto_install}, "
        f"install_project={repo_venv_config.install_project}, "
        f"timeout={repo_venv_config.install_timeout_sec}/{repo_venv_config.max_install_timeout_sec}s, "
        f"index_url={repo_venv_config.index_url or 'pip-default'}"
    )


def _resolve_connector_id(connector_id: Optional[str], repo_connector_id: Optional[str]) -> str:
    connector_id = (connector_id or "").strip()
    repo_connector_id = (repo_connector_id or "").strip()
    if connector_id and repo_connector_id and connector_id != repo_connector_id:
        raise ValueError(
            f"connector_id({connector_id!r}) 与 repo_connector_id({repo_connector_id!r}) 不一致"
        )
    return connector_id or repo_connector_id


def _venv_dir(workspace: Path) -> Path:
    """venv 目录：per-sha workspace 的父目录下的 dir_name，跨 commit 复用。"""
    return workspace.parent / (repo_venv_config.dir_name or ".venv")


def _venv_python(venv_dir: Path) -> Path:
    # 部署目标（mac 本机 + 阿里云 Linux Pod）均为 posix，bin 布局固定。
    return venv_dir / "bin" / "python"


def _is_valid_venv(venv_dir: Path) -> bool:
    return _venv_python(venv_dir).exists()


def _is_legacy_isolated_venv(venv_dir: Path) -> bool:
    """旧 venv 是否为完全隔离（include-system-site-packages = false）。

    我们已改为 --system-site-packages 复用镜像层（pytest/ruff/fastapi 等）。存量隔离
    venv 永远看不到镜像层，需一次性 rmtree 重建。读 pyvenv.cfg 判断。
    """
    cfg = venv_dir / "pyvenv.cfg"
    try:
        for line in cfg.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            if key.strip().lower() == "include-system-site-packages":
                return value.strip().lower() != "true"
    except (OSError, ValueError):
        return False
    # 没有该键的极旧环境也按隔离处理，重建以统一。
    return True


def venv_python_for_workspace(workspace: Path) -> Optional[Path]:
    """供脚本/命令执行链路查询：返回该 workspace 可复用的 venv 解释器，没有则 None。"""
    if not repo_venv_config.enabled:
        return None
    venv_dir = _venv_dir(workspace)
    py = _venv_python(venv_dir)
    return py if py.exists() else None


def venv_bin_for_repo(project_id: str, connector_id: Optional[str] = None) -> Optional[Path]:
    """返回某仓库预热 venv 的 bin 目录（仅当已建好），否则 None。

    venv 建在 per-sha workspace 的父目录（`_repo_dir`/`_project_dir`），位置只取决于
    project_id + connector_id，与具体 commit、与 coding task 的独立 clone 位置都无关；
    因此这里直接按目录约定推算，不触发 clone。coding 执行链路用它把项目依赖注入 PATH，
    让白名单里的裸 `pytest` / `python` 直接用上项目三方依赖（无需 pip install / .venv 前缀）。
    """
    if not repo_venv_config.enabled:
        return None
    cid = (connector_id or "").strip()
    repo_parent = _repo_dir(project_id, cid) if cid else _project_dir(project_id)
    venv_dir = repo_parent / (repo_venv_config.dir_name or ".venv")
    bin_dir = venv_dir / "bin"
    return bin_dir if _venv_python(venv_dir).exists() else None


def _base_python() -> str:
    return (repo_venv_config.base_python or "").strip() or sys.executable


def _clamp_timeout(raw: int) -> int:
    try:
        value = int(raw or repo_venv_config.install_timeout_sec)
    except (TypeError, ValueError):
        value = repo_venv_config.install_timeout_sec
    return min(max(value, 1), repo_venv_config.max_install_timeout_sec)


def _truncate(text: str, max_chars: int) -> tuple[str, bool]:
    if text is None:
        return "", False
    if len(text) <= max_chars:
        return text, False
    return text[:max_chars] + "\n...(truncated)", True


def _detect_dep_files(workspace: Path) -> tuple[list[str], list[str]]:
    """返回 (requirements 文件相对路径列表, 项目声明文件相对路径列表)。"""
    requirements = sorted(p.name for p in workspace.glob(_REQUIREMENTS_GLOB) if p.is_file())
    project_files = [name for name in _PROJECT_DEP_FILES if (workspace / name).is_file()]
    return requirements, project_files


def _fingerprint(workspace: Path, dep_files: list[str], extra_packages: list[str]) -> str:
    h = hashlib.sha256()
    h.update(f"base={_base_python_version()}\n".encode())
    h.update(f"install_project={repo_venv_config.install_project}\n".encode())
    for rel in dep_files:
        h.update(f"--file:{rel}--\n".encode())
        try:
            h.update((workspace / rel).read_bytes())
        except OSError:
            h.update(b"<unreadable>")
        h.update(b"\n")
    for pkg in sorted(extra_packages):
        h.update(f"--pkg:{pkg}--\n".encode())
    return h.hexdigest()


def _base_python_version() -> str:
    try:
        out = subprocess.run(
            [_base_python(), "-c", "import sys;print('.'.join(map(str, sys.version_info[:3])))"],
            capture_output=True, text=True, timeout=15,
        )
        return out.stdout.strip() or "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def _pip_index_args() -> list[str]:
    args: list[str] = []
    if repo_venv_config.index_url:
        args += ["--index-url", repo_venv_config.index_url]
    if repo_venv_config.extra_index_url:
        args += ["--extra-index-url", repo_venv_config.extra_index_url]
    if repo_venv_config.trusted_host:
        args += ["--trusted-host", repo_venv_config.trusted_host]
    return args


def _pip_env(venv_dir: Path) -> dict[str, str]:
    env = os.environ.copy()
    bin_dir = str(venv_dir / "bin")
    env["PATH"] = bin_dir + os.pathsep + env.get("PATH", "")
    env["VIRTUAL_ENV"] = str(venv_dir)
    env.pop("PYTHONHOME", None)
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    env["PIP_NO_INPUT"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _read_marker(venv_dir: Path) -> dict:
    try:
        return json.loads((venv_dir / _MARKER_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_marker(venv_dir: Path, data: dict) -> None:
    try:
        (venv_dir / _MARKER_NAME).write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass


def ensure_repo_venv(
    project_id: str,
    install: bool = True,
    force: bool = False,
    extra_packages: Optional[list[str]] = None,
    timeout_sec: int = 0,
    max_chars: int = 0,
    connector_id: Optional[str] = None,
    repo_connector_id: Optional[str] = None,
) -> dict:
    """确保仓库 venv 就绪。

    - install=True：按依赖文件指纹安装/刷新依赖（指纹未变则跳过）。
    - force=True：忽略指纹强制重装。
    - install=False：仅确保 venv 存在（创建空环境），不装依赖。
    """
    if not repo_venv_config.enabled:
        return {"ok": False, "error": "repo_venv 已关闭"}

    # 运维侧可通过 auto_install=false 关掉自动装依赖，此时只建空 venv，依赖需人工介入。
    if install and not repo_venv_config.auto_install:
        install = False

    extra_packages = [p.strip() for p in (extra_packages or []) if isinstance(p, str) and p.strip()]
    if not isinstance(extra_packages, list):
        return {"ok": False, "error": "extra_packages 必须是字符串数组"}

    try:
        effective_connector_id = _resolve_connector_id(connector_id, repo_connector_id)
        workspace = ensure_workspace(project_id, connector_id=effective_connector_id or None)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e)}

    venv_dir = _venv_dir(workspace)
    timeout = _clamp_timeout(timeout_sec)
    log_limit = max_chars if max_chars and max_chars > 0 else repo_venv_config.pip_log_chars
    started = time.monotonic()
    logs: list[str] = []
    steps: list[dict] = []

    def _run(cmd: list[str], label: str) -> subprocess.CompletedProcess:
        proc = subprocess.run(
            cmd, cwd=str(workspace), env=_pip_env(venv_dir),
            capture_output=True, text=True, timeout=timeout,
        )
        combined = (proc.stdout or "") + (proc.stderr or "")
        logs.append(f"$ {' '.join(cmd)}\n{combined.strip()}")
        steps.append({"label": label, "exit_code": proc.returncode})
        return proc

    created = False
    try:
        # 一次性迁移：存量完全隔离 venv 看不到镜像层 pytest/ruff，rmtree 后按新参数重建。
        if _is_valid_venv(venv_dir) and _is_legacy_isolated_venv(venv_dir):
            shutil.rmtree(venv_dir, ignore_errors=True)
            steps.append({"label": "rebuild_legacy_isolated_venv", "exit_code": 0})
        if not _is_valid_venv(venv_dir):
            venv_dir.mkdir(parents=True, exist_ok=True)
            # --system-site-packages：venv 复用镜像层依赖（pytest/ruff/fastapi 等），
            # 省去重装、让白名单裸命令直接可用（决策已确认全部可见镜像包）。
            proc = _run([_base_python(), "-m", "venv", "--system-site-packages", str(venv_dir)], "create_venv")
            if proc.returncode != 0 or not _is_valid_venv(venv_dir):
                log, truncated = _truncate("\n\n".join(logs), log_limit)
                return {
                    "ok": False, "error": "创建 venv 失败（base python 可能缺少 venv/ensurepip）",
                    "venv_dir": str(venv_dir), "base_python": _base_python(),
                    "log": log, "log_truncated": truncated, "steps": steps,
                }
            created = True
            # 新建环境才升级 pip 工具链，复用时跳过以省时。
            _run([str(_venv_python(venv_dir)), "-m", "pip", "install", "--upgrade",
                  "pip", "setuptools", "wheel", *_pip_index_args()], "upgrade_pip")
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"创建 venv 超时（>{timeout}s）", "venv_dir": str(venv_dir)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": str(e), "venv_dir": str(venv_dir)}

    requirements, project_files = _detect_dep_files(workspace)
    dep_files = requirements + (project_files if repo_venv_config.install_project else [])
    fingerprint = _fingerprint(workspace, dep_files, extra_packages)
    marker = _read_marker(venv_dir)
    py = _venv_python(venv_dir)

    base_result = {
        "ok": True,
        "venv_dir": str(venv_dir),
        "python": str(py),
        "base_python": _base_python(),
        "repo_connector_id": effective_connector_id,
        "created": created,
        "requirements_files": requirements,
        "project_files": project_files,
        "extra_packages": extra_packages,
        "fingerprint": fingerprint,
    }

    if not install:
        return {**base_result, "installed": False, "skipped_reason": "install=false（仅确保 venv 存在）"}

    if not dep_files and not extra_packages:
        _write_marker(venv_dir, {"fingerprint": fingerprint, "dep_files": [], "packages": []})
        return {**base_result, "installed": False,
                "skipped_reason": "未探测到依赖文件，且未指定 extra_packages"}

    if not force and marker.get("fingerprint") == fingerprint:
        return {**base_result, "installed": False, "changed": False,
                "skipped_reason": "依赖指纹未变更，复用已安装环境",
                "installed_at": marker.get("installed_at")}

    # 执行安装：requirements*.txt → 项目声明（可选）→ 额外包。
    install_ok = True
    try:
        for rel in requirements:
            proc = _run([str(py), "-m", "pip", "install", "-r", rel, *_pip_index_args()],
                        f"install -r {rel}")
            install_ok = install_ok and proc.returncode == 0
        if repo_venv_config.install_project and project_files:
            proc = _run([str(py), "-m", "pip", "install", "-e", ".", *_pip_index_args()],
                        "install -e .")
            install_ok = install_ok and proc.returncode == 0
        if extra_packages:
            proc = _run([str(py), "-m", "pip", "install", *extra_packages, *_pip_index_args()],
                        "install extra_packages")
            install_ok = install_ok and proc.returncode == 0
    except subprocess.TimeoutExpired:
        log, truncated = _truncate("\n\n".join(logs), log_limit)
        return {**base_result, "ok": False, "installed": False,
                "error": f"依赖安装超时（>{timeout}s），可调高 timeout_sec 或换更快的 pip 源",
                "log": log, "log_truncated": truncated, "steps": steps,
                "elapsed_sec": round(time.monotonic() - started, 1)}
    except Exception as e:  # noqa: BLE001
        log, truncated = _truncate("\n\n".join(logs), log_limit)
        return {**base_result, "ok": False, "installed": False, "error": str(e),
                "log": log, "log_truncated": truncated, "steps": steps}

    if install_ok:
        _write_marker(venv_dir, {
            "fingerprint": fingerprint,
            "dep_files": dep_files,
            "packages": extra_packages,
            "installed_at": int(time.time()),
            "python_version": _base_python_version(),
        })

    log, truncated = _truncate("\n\n".join(logs), log_limit)
    return {
        **base_result,
        "ok": install_ok,
        "installed": install_ok,
        "changed": True,
        "error": None if install_ok else "部分依赖安装失败，详见 log",
        "steps": steps,
        "log": log,
        "log_truncated": truncated,
        "elapsed_sec": round(time.monotonic() - started, 1),
    }
