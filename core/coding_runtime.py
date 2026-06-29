"""Coding tools runtime：LLM 可调用的受控读写与命令执行工具。"""
from __future__ import annotations

import difflib
import os
import py_compile
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Callable, Optional

from langchain_core.tools import StructuredTool
from pydantic import Field, create_model

from core.coding_policy import CodingPolicy
from core.coding_workspace import git_diff, git_status


EventSink = Callable[[str, str, dict], None]

SUPPORTED_SYNTAX_LANGUAGES = {"python", "java", "javascript", "typescript", "tsx", "jsx"}

# 走 esbuild 单文件语法检查的语言（前端 TS/JSX 家族）。
# esbuild 只解析不做类型/模块解析，所以在没有 node_modules / tsconfig 的
# coding workspace 里也能可靠抓真实语法错，且不会因缺依赖误报 "Cannot find module"。
ESBUILD_SYNTAX_LANGUAGES = {"typescript", "tsx", "jsx"}


def _detect_syntax_language(path: str, language: str = "auto") -> str:
    lang = _normalize_syntax_language(language)
    if lang and lang != "auto":
        return lang

    suffix_lang = _detect_syntax_language_from_path(path)
    if suffix_lang:
        return suffix_lang

    if lang == "auto":
        raise ValueError(f"无法从文件后缀识别语法检查语言: {path}")
    raise ValueError(f"不支持的语法检查语言: {language!r}")


def _normalize_syntax_language(language: str = "auto") -> str | None:
    raw = str(language or "auto").strip().lower().strip("`'\"")
    compact = re.sub(r"[^a-z0-9]+", "", raw)
    aliases = {
        "": "auto",
        "auto": "auto",
        "py": "python",
        "python": "python",
        "python3": "python",
        "java": "java",
        "js": "javascript",
        "node": "javascript",
        "javascript": "javascript",
        "ecmascript": "javascript",
        "ts": "typescript",
        "typescript": "typescript",
        "tsx": "tsx",
        "typescriptreact": "tsx",
        "jsx": "jsx",
        "javascriptreact": "jsx",
    }
    return aliases.get(compact)


def _detect_syntax_language_from_path(path: str) -> str | None:
    suffix = Path(path).suffix.lower()
    if suffix == ".py":
        return "python"
    if suffix == ".java":
        return "java"
    if suffix in {".js", ".mjs", ".cjs"}:
        return "javascript"
    if suffix in {".ts", ".mts", ".cts"}:
        return "typescript"
    if suffix == ".tsx":
        return "tsx"
    if suffix == ".jsx":
        return "jsx"
    return None


def _python_diagnostic(error: py_compile.PyCompileError) -> dict:
    exc = getattr(error, "exc_value", None)
    diagnostic = {
        "message": str(error).strip(),
        "severity": "error",
    }
    if isinstance(exc, SyntaxError):
        diagnostic.update({
            "line": exc.lineno,
            "column": exc.offset,
            "text": (exc.text or "").strip(),
            "message": exc.msg,
        })
    return diagnostic


class CodingRuntime:
    def __init__(
        self,
        workspace: Path,
        policy: CodingPolicy,
        emit: EventSink | None = None,
        *,
        project_id: str = "",
        repo_connector_id: str = "",
    ) -> None:
        self.workspace = workspace.resolve()
        self.policy = policy
        self.emit = emit or (lambda event_type, message, payload: None)
        self.project_id = project_id
        self.repo_connector_id = repo_connector_id

    def _command_env(self) -> dict:
        """命令/测试子进程环境：把仓库预热 venv 的 bin 注入 PATH，
        让白名单里的裸 `pytest` / `python` 直接用上项目三方依赖，
        agent 无需 pip install、也无需 .venv/bin 前缀。venv 未建好则原样返回。
        """
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        if not self.project_id:
            return env
        try:
            from tools.repo_venv import venv_bin_for_repo

            bin_dir = venv_bin_for_repo(self.project_id, self.repo_connector_id or None)
        except Exception:  # noqa: BLE001
            bin_dir = None
        if bin_dir:
            env["PATH"] = str(bin_dir) + os.pathsep + env.get("PATH", "")
            env["VIRTUAL_ENV"] = str(bin_dir.parent)
            env.pop("PYTHONHOME", None)
        return env

    def _workspace_relative_path(self, user_path: str, *, for_write: bool = False) -> str:
        raw = (user_path or "").strip()
        if not raw:
            return raw
        path = Path(raw).expanduser()
        if not path.is_absolute():
            return raw

        resolved = path.resolve(strict=False)
        try:
            return resolved.relative_to(self.workspace).as_posix()
        except ValueError:
            pass

        parts = path.parts
        for index in range(1, len(parts)):
            suffix = Path(*parts[index:])
            candidate = self.workspace / suffix
            has_glob = any(char in suffix.as_posix() for char in "*?[")
            if candidate.exists() or (for_write and candidate.parent.exists()) or (has_glob and candidate.parent.exists()):
                return suffix.as_posix()
        return raw

    def _tool_error(self, name: str, error: Exception) -> dict:
        message = str(error)
        self.emit("tool_call_failed", name, {"error": message})
        return {"error": message, "type": error.__class__.__name__}

    def resolve(self, user_path: str) -> Path:
        raw = self._workspace_relative_path(user_path)
        raw = (raw or "").strip().lstrip("/")
        candidate = (self.workspace / raw).resolve()
        try:
            candidate.relative_to(self.workspace)
        except ValueError as e:
            raise PermissionError(f"路径越出 workspace: {user_path}") from e
        return candidate

    def list_files(self, pattern: str = "**/*", max_results: int = 200) -> dict:
        self.emit("tool_call_started", "list_files", {"pattern": pattern})
        try:
            pat = self._workspace_relative_path(pattern).strip() or "**/*"
            files: list[str] = []
            for p in self.workspace.glob(pat):
                if not p.is_file() or ".git" in p.parts:
                    continue
                rel = str(p.relative_to(self.workspace))
                files.append(rel)
                if len(files) >= max_results:
                    break
            files.sort()
            out = {"count": len(files), "truncated": len(files) >= max_results, "files": files}
            self.emit("tool_call_finished", "list_files", out)
            return out
        except Exception as e:  # noqa: BLE001
            return self._tool_error("list_files", e)

    def grep(self, pattern: str, path: str = "", ignore_case: bool = True, max_results: int = 80) -> dict:
        self.emit("tool_call_started", "grep", {"pattern": pattern, "path": path})
        try:
            norm_path = self._workspace_relative_path(path) if path else ""
            root = self.resolve(norm_path) if norm_path else self.workspace
            flags = re.IGNORECASE if ignore_case else 0
            regex = re.compile(pattern, flags)
            hits: list[dict] = []
            files = [root] if root.is_file() else [p for p in root.rglob("*") if p.is_file()]
            for file in files:
                if ".git" in file.parts:
                    continue
                try:
                    rel = str(file.relative_to(self.workspace))
                    self.policy.check_read_path(rel)
                    with open(file, "r", encoding="utf-8", errors="ignore") as f:
                        for line_no, line in enumerate(f, 1):
                            if regex.search(line):
                                hits.append({"file": rel, "line": line_no, "content": line.rstrip("\n")[:500]})
                                if len(hits) >= max_results:
                                    out = {"count": len(hits), "truncated": True, "hits": hits}
                                    self.emit("tool_call_finished", "grep", out)
                                    return out
                except (OSError, UnicodeDecodeError, PermissionError):
                    continue
            out = {"count": len(hits), "truncated": False, "hits": hits}
            self.emit("tool_call_finished", "grep", out)
            return out
        except Exception as e:  # noqa: BLE001
            return self._tool_error("grep", e)

    def read_file(self, path: str, start_line: int = 1, end_line: Optional[int] = None) -> dict:
        try:
            norm_path = self._workspace_relative_path(path)
            self.policy.check_read_path(norm_path)
            file = self.resolve(norm_path)
            self.emit("tool_call_started", "read_file", {"path": norm_path, "start_line": start_line, "end_line": end_line})
            if not file.is_file():
                raise FileNotFoundError(norm_path)
            lines = file.read_text(encoding="utf-8", errors="ignore").splitlines()
            start = max(start_line, 1)
            end = min(end_line or start + 300 - 1, len(lines), start + 500 - 1)
            body = "\n".join(f"{idx}: {lines[idx - 1]}" for idx in range(start, end + 1))
            out = {"path": norm_path, "start_line": start, "end_line": end, "total_lines": len(lines), "content": body}
            self.emit("tool_call_finished", "read_file", {"path": norm_path, "lines": f"{start}-{end}"})
            return out
        except Exception as e:  # noqa: BLE001
            return self._tool_error("read_file", e)

    def write_file(self, path: str, content: str) -> dict:
        try:
            norm_path = self._workspace_relative_path(path, for_write=True)
            self.policy.check_write_path(norm_path)
            file = self.resolve(norm_path)
            file.parent.mkdir(parents=True, exist_ok=True)
            before = file.read_text(encoding="utf-8", errors="ignore") if file.exists() else ""
            file.write_text(content, encoding="utf-8")
            changed = before != content
            self.emit("tool_call_finished", "write_file", {"path": norm_path, "changed": changed})
            return {"path": norm_path, "changed": changed, "chars": len(content)}
        except Exception as e:  # noqa: BLE001
            return self._tool_error("write_file", e)

    def apply_patch(self, path: str, old: str, new: str, occurrence: int = 1) -> dict:
        try:
            norm_path = self._workspace_relative_path(path, for_write=True)
            self.policy.check_write_path(norm_path)
            file = self.resolve(norm_path)
            if not file.is_file():
                raise FileNotFoundError(norm_path)
            text = file.read_text(encoding="utf-8", errors="ignore")
            if not old:
                raise ValueError("old 不能为空")
            matches = [m.start() for m in re.finditer(re.escape(old), text)]
            if len(matches) < occurrence:
                raise ValueError(f"未找到第 {occurrence} 处 old 内容，实际匹配 {len(matches)} 处")
            index = matches[occurrence - 1]
            updated = text[:index] + new + text[index + len(old):]
            file.write_text(updated, encoding="utf-8")
            preview = "\n".join(difflib.unified_diff(
                old.splitlines(), new.splitlines(), fromfile=f"{norm_path}:old", tofile=f"{norm_path}:new", lineterm=""
            ))[:4000]
            self.emit("tool_call_finished", "apply_patch", {"path": norm_path, "changed": True})
            return {"path": norm_path, "changed": True, "preview": preview}
        except Exception as e:  # noqa: BLE001
            return self._tool_error("apply_patch", e)

    def check_syntax(self, path: str, language: str = "auto", timeout_sec: int | None = None) -> dict:
        """Check one source file with a fixed, language-specific compiler command."""
        try:
            norm_path = self._workspace_relative_path(path)
            self.policy.check_read_path(norm_path)
            file = self.resolve(norm_path)
            if not file.is_file():
                raise FileNotFoundError(norm_path)
            lang = _detect_syntax_language(norm_path, language)
            timeout = min(timeout_sec or self.policy.command_timeout_sec, self.policy.command_timeout_sec)
            self.emit("tool_call_started", "check_syntax", {"path": norm_path, "language": lang, "timeout_sec": timeout})
            if lang == "python":
                out = self._check_python_syntax(file, norm_path)
            elif lang == "java":
                out = self._check_subprocess_syntax(["javac", "-proc:none"], file, norm_path, lang, timeout)
            elif lang == "javascript":
                out = self._check_subprocess_syntax(["node", "--check"], file, norm_path, lang, timeout)
            elif lang in ESBUILD_SYNTAX_LANGUAGES:
                out = self._check_esbuild_syntax(file, norm_path, lang, timeout)
            else:
                raise ValueError(f"不支持的语法检查语言: {lang}")
            self.emit("tool_call_finished", "check_syntax", {"path": norm_path, "language": lang, "ok": out.get("ok")})
            return out
        except Exception as e:  # noqa: BLE001
            return self._tool_error("check_syntax", e)

    def _check_python_syntax(self, file: Path, norm_path: str) -> dict:
        try:
            with tempfile.TemporaryDirectory(prefix="viktor-pycompile-") as tmp:
                py_compile.compile(str(file), cfile=str(Path(tmp) / "out.pyc"), doraise=True)
            return {"path": norm_path, "language": "python", "ok": True, "diagnostics": []}
        except py_compile.PyCompileError as e:
            diagnostic = _python_diagnostic(e)
            return {
                "path": norm_path,
                "language": "python",
                "ok": False,
                "diagnostics": [diagnostic],
                "stderr": str(e)[-8000:],
            }

    def _check_subprocess_syntax(self, base_command: list[str], file: Path, norm_path: str, language: str, timeout: int) -> dict:
        executable = shutil.which(base_command[0])
        if not executable:
            return {
                "path": norm_path,
                "language": language,
                "ok": False,
                "diagnostics": [{
                    "message": f"语法检查器不可用: {base_command[0]} 未安装或不在 PATH 中",
                    "severity": "error",
                }],
                "missing_tool": base_command[0],
            }
        with tempfile.TemporaryDirectory(prefix=f"viktor-{language}-syntax-") as tmp:
            command = [executable, *base_command[1:]]
            if language == "java":
                command.extend(["-d", tmp])
            command.append(str(file))
            res = subprocess.run(
                command,
                cwd=str(self.workspace),
                shell=False,
                capture_output=True,
                text=True,
                timeout=timeout,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            )
        stderr = (res.stderr or "")[-8000:]
        stdout = (res.stdout or "")[-8000:]
        return {
            "path": norm_path,
            "language": language,
            "ok": res.returncode == 0,
            "exit_code": res.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "diagnostics": [] if res.returncode == 0 else [{"message": (stderr or stdout or "语法检查失败").strip()[:2000], "severity": "error"}],
        }

    def _check_esbuild_syntax(self, file: Path, norm_path: str, language: str, timeout: int) -> dict:
        """用 esbuild 对单个 TS/TSX/JSX 文件做纯语法检查。

        esbuild 只解析、不做类型检查和模块解析，因此即使 workspace 没有
        node_modules / tsconfig 也能可靠抓真实语法错，不会误报缺依赖。
        """
        executable = shutil.which("esbuild")
        if not executable:
            return {
                "path": norm_path,
                "language": language,
                "ok": False,
                "diagnostics": [{
                    "message": "语法检查器不可用: esbuild 未安装或不在 PATH 中",
                    "severity": "error",
                }],
                "missing_tool": "esbuild",
            }
        with tempfile.TemporaryDirectory(prefix=f"viktor-{language}-syntax-") as tmp:
            # 直接传文件时 esbuild 按后缀（.ts/.tsx/.jsx）自动选 loader，不能再传
            # 不带扩展名的 --loader（那只对 stdin 生效，会报错）。
            # --bundle 默认关闭：只解析+生成单文件，不解析 import，故无 node_modules 也不误报缺依赖。
            command = [
                executable,
                "--log-level=warning",
                "--outfile=" + str(Path(tmp) / "out.js"),
                str(file),
            ]
            res = subprocess.run(
                command,
                cwd=str(self.workspace),
                shell=False,
                capture_output=True,
                text=True,
                timeout=timeout,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            )
        stderr = (res.stderr or "")[-8000:]
        stdout = (res.stdout or "")[-8000:]
        return {
            "path": norm_path,
            "language": language,
            "ok": res.returncode == 0,
            "exit_code": res.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "diagnostics": [] if res.returncode == 0 else [{"message": (stderr or stdout or "语法检查失败").strip()[:2000], "severity": "error"}],
        }

    def run_command(self, command: str, timeout_sec: int | None = None) -> dict:
        try:
            self.policy.check_command(command)
            timeout = min(timeout_sec or self.policy.command_timeout_sec, self.policy.command_timeout_sec)
            self.emit("command_started", command, {"timeout_sec": timeout})
            res = subprocess.run(
                command,
                cwd=str(self.workspace),
                shell=True,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=self._command_env(),
            )
            out = {
                "command": command,
                "exit_code": res.returncode,
                "stdout": (res.stdout or "")[-8000:],
                "stderr": (res.stderr or "")[-8000:],
            }
            self.emit("command_finished", command, {"exit_code": res.returncode})
            return out
        except Exception as e:  # noqa: BLE001
            return self._tool_error("run_command", e)

    def git_status(self) -> dict:
        try:
            return {"status": git_status(self.workspace)}
        except Exception as e:  # noqa: BLE001
            return self._tool_error("git_status", e)

    def git_diff(self) -> dict:
        try:
            diff = git_diff(self.workspace)
            return {"diff": diff}
        except Exception as e:  # noqa: BLE001
            return self._tool_error("git_diff", e)

    def tools(self) -> list[StructuredTool]:
        ListFilesArgs = create_model("CodingListFilesArgs", pattern=(str, Field(default="**/*")), max_results=(int, Field(default=200)))
        GrepArgs = create_model("CodingGrepArgs", pattern=(str, Field(description="正则或关键词")), path=(str, Field(default="")), ignore_case=(bool, Field(default=True)), max_results=(int, Field(default=80)))
        ReadArgs = create_model("CodingReadFileArgs", path=(str, Field(description="workspace 内相对路径")), start_line=(int, Field(default=1)), end_line=(Optional[int], Field(default=None)))
        WriteArgs = create_model("CodingWriteFileArgs", path=(str, Field(description="workspace 内相对路径")), content=(str, Field(description="完整文件内容")))
        PatchArgs = create_model("CodingApplyPatchArgs", path=(str, Field(description="workspace 内相对路径")), old=(str, Field(description="要替换的原文，必须精确匹配")), new=(str, Field(description="替换后的文本")), occurrence=(int, Field(default=1)))
        SyntaxArgs = create_model("CodingCheckSyntaxArgs", path=(str, Field(description="workspace 内单个源码文件相对路径")), language=(str, Field(default="auto", description="auto/python/java/javascript/typescript/tsx/jsx；通常用 auto 按后缀自动识别")), timeout_sec=(Optional[int], Field(default=None)))
        CommandArgs = create_model("CodingRunCommandArgs", command=(str, Field(description="要执行的命令，必须符合 policy")), timeout_sec=(Optional[int], Field(default=None)))
        EmptyArgs = create_model("CodingEmptyArgs", placeholder=(Optional[str], Field(default=None)))
        return [
            StructuredTool.from_function(func=self.list_files, name="list_files", description="列出 workspace 内文件", args_schema=ListFilesArgs),
            StructuredTool.from_function(func=self.grep, name="grep", description="在 workspace 内搜索代码", args_schema=GrepArgs),
            StructuredTool.from_function(func=self.read_file, name="read_file", description="读取文件片段", args_schema=ReadArgs),
            StructuredTool.from_function(func=self.write_file, name="write_file", description="写入完整文件；优先用于新文件", args_schema=WriteArgs),
            StructuredTool.from_function(func=self.apply_patch, name="apply_patch", description="用精确 old/new 替换修改文件；上下文不匹配会失败", args_schema=PatchArgs),
            StructuredTool.from_function(func=self.check_syntax, name="check_syntax", description="固定检查单个 Python/Java/JavaScript/TypeScript/TSX/JSX 文件语法或编译错误；优先于拼接 run_command", args_schema=SyntaxArgs),
            StructuredTool.from_function(func=self.run_command, name="run_command", description="执行 policy 允许的测试/lint/build 命令", args_schema=CommandArgs),
            StructuredTool.from_function(func=lambda placeholder=None: self.git_status(), name="git_status", description="查看 git status --short", args_schema=EmptyArgs),
            StructuredTool.from_function(func=lambda placeholder=None: self.git_diff(), name="git_diff", description="查看当前 diff", args_schema=EmptyArgs),
        ]
