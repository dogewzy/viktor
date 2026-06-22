"""多语言测试流程默认值（B 层）。

设计：「到底用什么测试」由 Viktor 决定 —— 每语言写死内置默认命令作为默认值，
同时允许用户在 Repository Connector 上覆盖（connector.test_command / lint_command）。

- 用户用默认配置 → 测试阶段直接跑内置命令，依赖已在镜像 / 预热 venv 就绪，很快。
- 用户配了 Viktor 没有的依赖 → 声明自定义 test_command，policy 才开窄口子允许执行阶段安装。

决策层（coding_service）用 `resolve_test_command` / `resolve_lint_command` 把项目实际命令
注入 prompt 与 allowed_commands，让 LLM「对症下药」而非在循环里盲目现装依赖。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

# 每语言内置默认测试 / lint 命令。venv 已在 PATH（见 coding_runtime._command_env），
# 故一律用裸命令，不加 .venv/ 前缀、不加 pip install。
LANGUAGE_TEST_DEFAULTS: dict[str, dict[str, str]] = {
    "python": {"test_command": "pytest", "lint_command": "ruff check ."},
    "javascript": {"test_command": "npm test", "lint_command": ""},
    "typescript": {"test_command": "npm test", "lint_command": ""},
    # java / go 预留占位，待有实际项目再填默认值。
    "java": {"test_command": "", "lint_command": ""},
    "go": {"test_command": "", "lint_command": ""},
}

# 语言别名 → 归一化名。键统一小写。
_LANGUAGE_ALIASES: dict[str, str] = {
    "py": "python",
    "python3": "python",
    "js": "javascript",
    "node": "javascript",
    "nodejs": "javascript",
    "ts": "typescript",
    "tsx": "typescript",
    "golang": "go",
}


def normalize_language(language: Optional[str]) -> str:
    """归一化语言名；无法识别返回空字符串。"""
    name = (language or "").strip().lower()
    if not name:
        return ""
    name = _LANGUAGE_ALIASES.get(name, name)
    return name if name in LANGUAGE_TEST_DEFAULTS else ""


def _connector_attr(connector: Any, attr: str) -> str:
    """安全读取 connector 的字符串属性，兼容旧对象（滚动重启期间无新字段）。"""
    return (getattr(connector, attr, "") or "").strip()


def resolve_language(connector: Any = None, language: Optional[str] = None) -> str:
    """回退链：显式 language > connector.language > ""。"""
    explicit = normalize_language(language)
    if explicit:
        return explicit
    if connector is not None:
        return normalize_language(_connector_attr(connector, "language"))
    return ""


def resolve_test_command(connector: Any = None, language: Optional[str] = None) -> str:
    """回退链：connector.test_command（用户覆盖）> 该语言内置默认 > ""。"""
    if connector is not None:
        override = _connector_attr(connector, "test_command")
        if override:
            return override
    lang = resolve_language(connector, language)
    if lang:
        return LANGUAGE_TEST_DEFAULTS.get(lang, {}).get("test_command", "")
    return ""


def resolve_lint_command(connector: Any = None, language: Optional[str] = None) -> str:
    """回退链：connector.lint_command（用户覆盖）> 该语言内置默认 > ""。"""
    if connector is not None:
        override = _connector_attr(connector, "lint_command")
        if override:
            return override
    lang = resolve_language(connector, language)
    if lang:
        return LANGUAGE_TEST_DEFAULTS.get(lang, {}).get("lint_command", "")
    return ""


def has_custom_test_command(connector: Any) -> bool:
    """connector 是否声明了自定义 test_command（用于判断是否要开依赖安装窄口子）。"""
    return bool(_connector_attr(connector, "test_command"))


def detect_language(workspace: Path) -> str:
    """按标志文件嗅探 workspace 语言；仅当 connector 未声明 language 时兜底。"""
    try:
        ws = Path(workspace)
    except TypeError:
        return ""
    if (ws / "go.mod").is_file():
        return "go"
    if (ws / "pom.xml").is_file() or (ws / "build.gradle").is_file() or (ws / "build.gradle.kts").is_file():
        return "java"
    if (ws / "package.json").is_file():
        # 有 tsconfig.json 视为 TS，否则 JS（两者默认命令相同，区分仅为语义/未来扩展）。
        return "typescript" if (ws / "tsconfig.json").is_file() else "javascript"
    if (
        (ws / "pyproject.toml").is_file()
        or (ws / "setup.py").is_file()
        or any(ws.glob("requirements*.txt"))
    ):
        return "python"
    return ""
