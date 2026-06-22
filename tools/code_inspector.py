"""
代码自省（一期）· 三件套只读工具：code_glob / code_grep / code_read。

设计原则（免索引 agentic search 路线）：
- 只读：不写入、不执行、不访问 workspace 外的路径
- 路径安全：所有参数解析到 workspace 绝对路径后，必须是 workspace 的子路径
- 输出对 LLM 友好：返回结构化 dict，便于 tool-calling 后直接消费

每个工具的第一个参数是 project_id（由 agent_loop 闭包注入），对 LLM 透明。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from loguru import logger

from core.code_sync import ensure_workspace


# ============================================================
# 常见忽略目录（fallback 用；有 .gitignore 时优先走 .gitignore）
# ============================================================

_DEFAULT_IGNORES = {
    ".git", "node_modules", "vendor", "__pycache__", ".mypy_cache",
    ".pytest_cache", ".tox", ".venv", "venv", "dist", "build",
    ".idea", ".vscode", ".next", "target",
}

_MAX_READ_LINES = 500
_MAX_GREP_LINE_LEN = 500


def _resolve_connector_id(connector_id: Optional[str] = None, repo_connector_id: Optional[str] = None) -> str:
    """兼容 connector_id / repo_connector_id 两个入参名。"""
    connector_id = (connector_id or "").strip()
    repo_connector_id = (repo_connector_id or "").strip()
    if connector_id and repo_connector_id and connector_id != repo_connector_id:
        raise ValueError(
            f"connector_id({connector_id!r}) 与 repo_connector_id({repo_connector_id!r}) 不一致"
        )
    return connector_id or repo_connector_id


# ============================================================
# 路径安全
# ============================================================

def _safe_resolve(workspace: Path, user_path: str) -> Path:
    """把用户传入的相对路径解析为 workspace 内部的绝对路径，拒绝目录穿越。"""
    if not user_path or user_path.strip() in ("", "."):
        return workspace
    raw = user_path.strip().lstrip("/")
    candidate = (workspace / raw).resolve()
    try:
        candidate.relative_to(workspace.resolve())
    except ValueError:
        raise ValueError(f"路径 {user_path!r} 越出 workspace 范围")
    return candidate


def _to_relative(workspace: Path, abs_path: Path) -> str:
    try:
        return str(abs_path.relative_to(workspace))
    except ValueError:
        return str(abs_path)


def _iter_gitignore(workspace: Path):
    """加载 workspace 根下的 .gitignore，返回 PathSpec 或 None。"""
    gi = workspace / ".gitignore"
    if not gi.exists():
        return None
    try:
        import pathspec
    except ImportError:
        logger.debug("[code_inspector] pathspec 未安装，忽略 .gitignore")
        return None
    try:
        lines = gi.read_text(encoding="utf-8", errors="ignore").splitlines()
        return pathspec.PathSpec.from_lines("gitwildmatch", lines)
    except Exception as e:  # noqa: BLE001
        logger.debug("[code_inspector] 解析 .gitignore 失败: {}", e)
        return None


def _is_ignored(workspace: Path, rel_path: str, spec) -> bool:
    parts = Path(rel_path).parts
    if any(p in _DEFAULT_IGNORES for p in parts):
        return True
    if spec is not None and spec.match_file(rel_path):
        return True
    return False


# ============================================================
# code_glob
# ============================================================

def code_glob(
    project_id: str,
    pattern: str,
    max_results: int = 200,
    connector_id: Optional[str] = None,
    repo_connector_id: Optional[str] = None,
) -> dict:
    """按 glob 匹配 workspace 内的文件路径。

    - pattern 例：`**/*.py`、`services/**/*.go`、`order*.java`
    - 自动遵循 `.gitignore` + 通用忽略目录
    - 返回相对路径（相对 workspace 根）
    """
    try:
        effective_connector_id = _resolve_connector_id(connector_id, repo_connector_id)
    except ValueError as e:
        return {"error": str(e), "files": []}
    ws = ensure_workspace(project_id, connector_id=effective_connector_id or None)
    spec = _iter_gitignore(ws)

    pat = pattern.strip()
    if not pat:
        return {"error": "pattern 不能为空", "files": []}
    # 单独支持 `*.py` 自动扩展为 `**/*.py`
    if "/" not in pat and "**" not in pat and pat.startswith("*"):
        pat = f"**/{pat}"

    matches: list[str] = []
    for p in ws.glob(pat):
        if not p.is_file():
            continue
        rel = _to_relative(ws, p)
        if _is_ignored(ws, rel, spec):
            continue
        matches.append(rel)
        if len(matches) >= max_results:
            break
    matches.sort()
    return {
        "workspace": str(ws),
        "repo_connector_id": effective_connector_id,
        "pattern": pattern,
        "count": len(matches),
        "truncated": len(matches) >= max_results,
        "files": matches,
    }


# ============================================================
# code_grep
# ============================================================

def _camelcase_to_alt(pattern: str) -> str:
    """把 CamelCase / snake_case 的 token 拆成 | 组合，扩大召回。

    例：`createOrder` → `(createOrder|create|Order)`
        `create_order` → `(create_order|create|order)`
    非 identifier 的 pattern 原样返回。
    """
    token = pattern.strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", token):
        return pattern
    parts = re.findall(r"[A-Z][a-z0-9]+|[A-Z]+(?=[A-Z]|$)|[a-z0-9]+", token.replace("_", " ")) \
        if not token.islower() and "_" not in token else token.split("_")
    parts = [p for p in parts if p]
    cand = {token, *parts}
    if len(cand) <= 1:
        return pattern
    return "(" + "|".join(re.escape(x) for x in cand) + ")"


def _rg_available() -> Optional[str]:
    return shutil.which("rg")


def _parse_rg_json(line: str) -> Optional[dict]:
    import json
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    if obj.get("type") != "match":
        return None
    data = obj.get("data", {})
    submatches = data.get("submatches", []) or []
    lines_obj = data.get("lines", {}) or {}
    content = (lines_obj.get("text") or "").rstrip("\n")
    if len(content) > _MAX_GREP_LINE_LEN:
        content = content[:_MAX_GREP_LINE_LEN] + "...(truncated)"
    return {
        "file": data.get("path", {}).get("text", ""),
        "line": data.get("line_number", 0),
        "content": content,
        "match": submatches[0]["match"]["text"] if submatches else "",
    }


def _grep_with_rg(ws: Path, pattern: str, path: str, ignore_case: bool, max_results: int) -> list[dict]:
    rg = _rg_available()
    if not rg:
        return []
    search_dir = _safe_resolve(ws, path) if path else ws
    cmd = [
        rg, "--json", "--no-heading", "--line-number", "--color", "never",
        "--max-count", str(max_results),
    ]
    if ignore_case:
        cmd.append("-i")
    cmd.extend(["--", pattern, str(search_dir)])

    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        logger.warning("[code_inspector] rg 超时, pattern={}", pattern)
        return []

    hits: list[dict] = []
    for line in (res.stdout or "").splitlines():
        hit = _parse_rg_json(line)
        if hit:
            # 把 file 路径转为 workspace 相对路径
            try:
                hit["file"] = _to_relative(ws, Path(hit["file"]))
            except Exception:  # noqa: BLE001
                pass
            hits.append(hit)
            if len(hits) >= max_results:
                break
    return hits


def _grep_fallback(
    ws: Path, pattern: str, path: str, ignore_case: bool, max_results: int,
) -> list[dict]:
    """无 rg 时的 Python 回退实现（性能较差，仅作兜底）。"""
    flags = re.IGNORECASE if ignore_case else 0
    try:
        regex = re.compile(pattern, flags)
    except re.error as e:
        raise ValueError(f"非法正则 pattern={pattern!r}: {e}") from e

    root = _safe_resolve(ws, path) if path else ws
    spec = _iter_gitignore(ws)
    hits: list[dict] = []
    for dirpath, dirnames, filenames in os.walk(root):
        # 原地剪枝
        dirnames[:] = [d for d in dirnames if d not in _DEFAULT_IGNORES]
        for fn in filenames:
            abs_p = Path(dirpath) / fn
            rel = _to_relative(ws, abs_p)
            if _is_ignored(ws, rel, spec):
                continue
            try:
                with open(abs_p, "r", encoding="utf-8", errors="ignore") as f:
                    for i, raw in enumerate(f, start=1):
                        if regex.search(raw):
                            content = raw.rstrip("\n")
                            if len(content) > _MAX_GREP_LINE_LEN:
                                content = content[:_MAX_GREP_LINE_LEN] + "...(truncated)"
                            hits.append({
                                "file": rel,
                                "line": i,
                                "content": content,
                                "match": pattern,
                            })
                            if len(hits) >= max_results:
                                return hits
            except (OSError, UnicodeDecodeError):
                continue
    return hits


def code_grep(
    project_id: str,
    pattern: str,
    path: str = "",
    ignore_case: bool = True,
    fuzzy: bool = False,
    max_results: int = 50,
    connector_id: Optional[str] = None,
    repo_connector_id: Optional[str] = None,
) -> dict:
    """在 workspace 内 grep。

    - 优先系统 ripgrep；无 rg 时 Python 兜底
    - fuzzy=True 时自动把 camelCase/snake_case 拆分成 | 组合（扩大召回）
    - 0 结果会记录日志，后续可用于反哺术语表
    """
    if not pattern or not pattern.strip():
        return {"error": "pattern 不能为空", "hits": []}
    try:
        effective_connector_id = _resolve_connector_id(connector_id, repo_connector_id)
    except ValueError as e:
        return {"error": str(e), "hits": []}
    ws = ensure_workspace(project_id, connector_id=effective_connector_id or None)
    effective_pattern = _camelcase_to_alt(pattern) if fuzzy else pattern

    hits: list[dict] = []
    try:
        if _rg_available():
            hits = _grep_with_rg(ws, effective_pattern, path, ignore_case, max_results)
        else:
            hits = _grep_fallback(ws, effective_pattern, path, ignore_case, max_results)
    except ValueError as e:
        return {"error": str(e), "hits": []}

    if not hits:
        logger.info(
            "[code_inspector] grep 0 hits: project={} pattern={!r} fuzzy={}",
            project_id, pattern, fuzzy,
        )

    return {
        "workspace": str(ws),
        "repo_connector_id": effective_connector_id,
        "pattern": pattern,
        "effective_pattern": effective_pattern,
        "path": path or "",
        "count": len(hits),
        "truncated": len(hits) >= max_results,
        "hits": hits,
    }


# ============================================================
# code_read
# ============================================================

def code_read(
    project_id: str,
    path: str,
    start_line: int = 1,
    end_line: Optional[int] = None,
    connector_id: Optional[str] = None,
    repo_connector_id: Optional[str] = None,
) -> dict:
    """读取 workspace 内某文件指定行范围。

    - 单次最多返回 _MAX_READ_LINES 行，超过要求分段读
    - 返回内容带真实行号
    """
    try:
        effective_connector_id = _resolve_connector_id(connector_id, repo_connector_id)
    except ValueError as e:
        return {"error": str(e), "path": path}
    ws = ensure_workspace(project_id, connector_id=effective_connector_id or None)
    target = _safe_resolve(ws, path)
    if not target.is_file():
        return {"error": f"文件不存在: {path}", "path": path}

    start = max(int(start_line or 1), 1)
    cap = start + _MAX_READ_LINES - 1
    if end_line is None:
        end = cap
    else:
        end = min(int(end_line), cap)
    if end < start:
        return {"error": f"end_line({end}) 必须 >= start_line({start})", "path": path}

    lines: list[str] = []
    total = 0
    try:
        with open(target, "r", encoding="utf-8", errors="ignore") as f:
            for i, raw in enumerate(f, start=1):
                total = i
                if i < start:
                    continue
                if i > end:
                    # 继续计数以得到 total，但不再追加内容
                    for _ in f:
                        total += 1
                    break
                lines.append(f"{i:>6}\u2502{raw.rstrip(chr(10))}")
    except OSError as e:
        return {"error": f"读取失败: {e}", "path": path}

    return {
        "repo_connector_id": effective_connector_id,
        "path": _to_relative(ws, target),
        "start_line": start,
        "end_line": end,
        "total_lines": total,
        "truncated": (end_line is None and total > end) or (end_line is not None and end_line > end),
        "content": "\n".join(lines),
    }
