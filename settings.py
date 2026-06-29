"""
Viktor 全局配置加载模块。
从 config.yaml 加载配置，支持 ${ENV_VAR:default} 格式的环境变量替换。
"""
import os
import re
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import yaml
from loguru import logger
from pydantic import BaseModel, Field, model_validator


BASE_DIR = Path(__file__).resolve().parent

_ENV_VAR_PATTERN = re.compile(r"\$\{([^}:]+)(?::([^}]*))?\}")


def _normalize_gitlab_base_url(raw: str) -> str:
    """Normalize a GitLab web/API root to scheme://host[:port]."""
    s = (raw or "").strip().rstrip("/")
    if not s:
        return ""
    parsed = urlparse(s)
    if parsed.scheme in ("http", "https") and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
    host_and_maybe_port = s.split("/", 1)[0]
    if "://" not in host_and_maybe_port:
        return f"http://{host_and_maybe_port}".rstrip("/")
    return s


def _gitlab_base_url_from_repo_url(repo_url: str) -> str:
    """Best-effort GitLab API root from an http(s) clone URL."""
    raw = (repo_url or "").strip()
    if raw.startswith("git@"):
        return ""
    parsed = urlparse(raw)
    if parsed.scheme in ("http", "https") and parsed.netloc:
        return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
    return ""


def _load_dotenv() -> None:
    """本地开发时自动加载项目根下的 .env（不覆盖已存在的环境变量）。

    生产环境通过 K8s Secret/ConfigMap 注入环境变量，不依赖 .env 文件。
    """
    env_file = BASE_DIR / ".env"
    if not env_file.exists():
        return
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv()


def _resolve_env_vars(value: Any) -> Any:
    """递归解析配置值中的 ${ENV_VAR:default} 占位符。"""
    if isinstance(value, str):
        def _replace(match: re.Match) -> str:
            env_name = match.group(1)
            default = match.group(2) if match.group(2) is not None else ""
            return os.environ.get(env_name, default)
        return _ENV_VAR_PATTERN.sub(_replace, value)
    if isinstance(value, dict):
        return {k: _resolve_env_vars(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_env_vars(item) for item in value]
    return value


def _load_config() -> dict:
    """加载并解析 config.yaml。"""
    config_path = BASE_DIR / "config.yaml"
    with open(config_path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    resolved = _resolve_env_vars(raw)
    logger.info("配置加载完成")
    return resolved


class LLMProviderConfig(BaseModel):
    """单个 LLM 供应商配置（优先支持 OpenAI 兼容接口）。"""

    provider: str = "deepseek"
    model: str = "deepseek-chat"
    api_key: str = ""
    base_url: str = "https://api.deepseek.com/v1"
    temperature: Optional[float] = 0.1
    max_tokens: int = 4096
    supports_tools: bool = True
    supports_stream: bool = True
    supports_thinking: bool = False


class LLMConfig(BaseModel):
    """LLM 配置，兼容旧的单供应商写法和新的多供应商写法。"""

    provider: str = "deepseek"
    model: str = "deepseek-chat"
    api_key: str = ""
    base_url: str = "https://api.deepseek.com/v1"
    temperature: Optional[float] = 0.1
    max_tokens: int = 4096
    default: str = ""
    fallback_order: list[str] = Field(default_factory=list)
    feature_provider_order: dict[str, list[str]] = Field(default_factory=dict)
    cooldown_sec: int = 60
    providers: dict[str, LLMProviderConfig] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _normalize_providers(self) -> "LLMConfig":
        if not self.providers:
            provider_id = self.default or self.provider or "default"
            self.default = provider_id
            self.providers = {
                provider_id: LLMProviderConfig(
                    provider=self.provider,
                    model=self.model,
                    api_key=self.api_key,
                    base_url=self.base_url,
                    temperature=self.temperature,
                    max_tokens=self.max_tokens,
                    supports_thinking=self.provider == "deepseek",
                )
            }
        else:
            if not self.default:
                self.default = next(iter(self.providers.keys()))
            primary = self.providers.get(self.default) or next(iter(self.providers.values()))
            self.provider = primary.provider
            self.model = primary.model
            self.api_key = primary.api_key
            self.base_url = primary.base_url
            self.temperature = primary.temperature
            self.max_tokens = primary.max_tokens
        if not self.fallback_order:
            self.fallback_order = [self.default, *[k for k in self.providers.keys() if k != self.default]]
        return self


class DingtalkConfig(BaseModel):
    """钉钉配置。"""
    app_key: str = ""
    app_secret: str = ""


class ServerConfig(BaseModel):
    """HTTP 服务配置。"""
    host: str = "0.0.0.0"
    port: int = 8080


class AuthConfig(BaseModel):
    """网页控制台登录鉴权配置（内网用户名密码 + 长效 JWT）。"""
    jwt_secret: str = ""            # ${VIKTOR_AUTH_SECRET}，prod 必须设置
    token_ttl_days: int = 365       # 长效凭证：本机登录一次即可
    allow_registration: bool = True  # 开放自助注册（内网）


class KubernetesConfig(BaseModel):
    """K8s 配置。

    鉴权优先级：in_cluster > token 直连（api_server + token）> kubeconfig 文件。
    """
    namespace: str = "default"
    in_cluster: bool = False
    kubeconfig_path: str = "~/.kube/config"
    context: str = ""
    # token 直连模式（本地开发/调试专用）
    api_server: str = ""            # https://x.x.x.x:6443
    token: str = ""                 # 用户 Bearer Token
    ca_data: str = ""               # base64(PEM CA 证书)，留空且 insecure=True 时跳过验证
    insecure_skip_tls_verify: bool = False


class DatabaseConfig(BaseModel):
    """Viktor 自身数据库配置（存储注册项）。"""
    url: str = "mysql+pymysql://viktor:@127.0.0.1:3306/viktor?charset=utf8mb4"


class AliyunSLSConfig(BaseModel):
    """阿里云 SLS 全局连接配置。

    SLS project/logstore 属于业务注册项，不放在这里；这里仅存 Viktor 访问 SLS API 所需的全局凭证。
    """
    endpoint: str = ""
    access_key_id: str = ""
    access_key_secret: str = ""
    security_token: str = ""


class OSSConfig(BaseModel):
    """Aliyun OSS config shared by user uploads and generated artifacts."""

    access_key: str = ""
    secret_key: str = ""
    end_point: str = ""
    end_point_public: str = ""
    bucket: str = ""
    upload_prefix: str = "viktor"
    signed_url_ttl_seconds: int = 30 * 24 * 60 * 60

    @model_validator(mode="after")
    def _normalize(self) -> "OSSConfig":
        self.upload_prefix = (self.upload_prefix or "viktor").strip().strip("/") or "viktor"
        if not self.end_point_public and self.end_point:
            self.end_point_public = self.end_point.replace("-internal", "")
        return self

    @property
    def enabled(self) -> bool:
        return bool(self.access_key and self.secret_key and self.end_point and self.bucket)


class FileUploadConfig(BaseModel):
    """Web chat file upload and extraction limits."""

    max_file_size_mb: int = 20
    extracted_max_chars: int = 20000
    preview_max_chars: int = 2000
    max_excel_rows: int = 200
    max_excel_cols: int = 30
    allowed_extensions: list[str] = Field(
        default_factory=lambda: [
            ".txt", ".md", ".markdown", ".html", ".htm", ".log", ".json", ".csv", ".tsv", ".yaml", ".yml",
            ".sql", ".xml", ".ini", ".cfg", ".properties", ".py", ".java", ".go", ".ts",
            ".js", ".pdf", ".docx", ".xlsx",
        ]
    )

    @property
    def max_file_size_bytes(self) -> int:
        return max(self.max_file_size_mb, 1) * 1024 * 1024


class SSHTunnelConfig(BaseModel):
    """SSH 隧道全局默认值。

    是否走隧道由每个 Database Connector 自带的 ssh_tunnel 字段决定（见 DatabaseConnectorItem）：
    - ssh_tunnel 为 None  -> 直连（默认）
    - ssh_tunnel 有值   -> 走隧道，未填字段从本类的默认值回退
    """
    jump_host: str = ""
    jump_port: int = 20140
    username: str = "viewer"
    private_key: str = "~/.ssh/id_vb1"


class AgentConfig(BaseModel):
    """Agent 运行配置。"""
    max_iterations: int = 30
    # 同一轮 LLM 产生多个独立 tool call 时，最多并行执行多少个
    tool_max_concurrency: int = 6
    # 非 SQL 工具的统一执行超时；SQL 自身还有数据库级 sql_timeout_sec
    tool_timeout_sec: int = 75
    # 同一轮会话累计工具超时达到该阈值后，停止继续调用工具并做无工具收口
    max_tool_timeouts_per_turn: int = 2
    query_result_limit: int = 50
    # Free-SQL 安全上限：单条 SQL 字符数上限，超过拒绝执行
    max_sql_length: int = 4000
    # Free-SQL 数据库级执行超时；MySQL 通过 MAX_EXECUTION_TIME 尝试中止慢查询
    sql_timeout_sec: int = 60
    # Free-SQL EXPLAIN 预估扫描行数超过该值且命中全表扫描时拒绝执行
    sql_explain_max_estimated_rows: int = 100000
    # schema 自省缓存 TTL（秒），过期后重新查 information_schema
    schema_cache_ttl: int = 300
    # sample_rows 抽样工具允许的 LIMIT 最大值
    sample_row_limit: int = 20


class IntentConfig(BaseModel):
    """Project-scoped glossary-first intent routing."""

    enabled: bool = True
    glossary_top_k: int = 10
    knowledge_top_k: int = 8


class AgentAuditConfig(BaseModel):
    """Agent trace audit configuration."""

    enabled: bool = True
    retention_days: int = 7
    max_payload_string_length: int = 12000


class TraceEvaluationConfig(BaseModel):
    """Shadow evaluation for completed Agent traces."""

    enabled: bool = False
    auto_sample_rate: float = 0.0
    metrics: list[str] = Field(default_factory=lambda: ["faithfulness"])
    max_contexts: int = 12
    max_context_chars: int = 12000
    low_score_threshold: float = 0.65
    timeout_sec: float = 120.0

    @model_validator(mode="after")
    def normalize_values(self) -> "TraceEvaluationConfig":
        self.auto_sample_rate = max(0.0, min(1.0, float(self.auto_sample_rate)))
        self.max_contexts = max(1, int(self.max_contexts))
        self.max_context_chars = max(1000, int(self.max_context_chars))
        self.low_score_threshold = max(0.0, min(1.0, float(self.low_score_threshold)))
        self.timeout_sec = max(1.0, float(self.timeout_sec))
        normalized: list[str] = []
        for metric in self.metrics or []:
            text = str(metric).strip().lower()
            if text and text not in normalized:
                normalized.append(text)
        self.metrics = normalized or ["faithfulness"]
        return self


class ContextCompactionConfig(BaseModel):
    """Shared token-budget context compaction."""
    enabled: bool = True
    threshold_tokens: int = 30000
    target_tokens: int = 12000
    keep_recent_turns: int = 3


class GitLabCredentialConfig(BaseModel):
    """单个 GitLab 实例的访问凭证。"""

    base_url: str = ""
    aliases: list[str] = Field(default_factory=list)
    private_token: str = ""


class GitLabConfig(BaseModel):
    """GitLab 集成配置。"""
    base_url: str = "https://gitlab.example.com"
    private_token: str = ""
    credentials: list[GitLabCredentialConfig] = Field(default_factory=list)
    webhook_secret: str = ""
    file_extensions: list[str] = [
        ".py", ".java", ".go", ".ts", ".js", ".yaml", ".yml",
        ".sql", ".proto", ".thrift", ".graphql", ".gql",
        ".xml", ".properties", ".toml", ".cfg", ".ini",
    ]
    exclude_dirs: list[str] = [
        "vendor", "node_modules", ".git",
        "__pycache__", ".idea", ".vscode",
        ".gradle", "build", "dist", "target/classes",
    ]
    low_priority_dirs: list[str] = [
        "test", "tests", "testdata", "fixtures",
        "mock", "mocks", "examples", "example",
    ]
    max_file_size_kb: int = 100
    max_total_files: int = 200

    @model_validator(mode="after")
    def _normalize_credentials(self) -> "GitLabConfig":
        normalized: list[GitLabCredentialConfig] = []
        seen: set[str] = set()
        for item in self.credentials:
            base = _normalize_gitlab_base_url(item.base_url)
            token = (item.private_token or "").strip()
            if not base:
                continue
            if base in seen:
                continue
            aliases: list[str] = []
            alias_seen: set[str] = {base}
            for alias in item.aliases:
                normalized_alias = _normalize_gitlab_base_url(alias)
                if not normalized_alias or normalized_alias in alias_seen:
                    continue
                aliases.append(normalized_alias)
                alias_seen.add(normalized_alias)
            normalized.append(GitLabCredentialConfig(base_url=base, aliases=aliases, private_token=token))
            seen.add(base)

        legacy_base = _normalize_gitlab_base_url(self.base_url)
        legacy_token = (self.private_token or "").strip()
        self.base_url = legacy_base
        self.private_token = legacy_token
        if legacy_base and legacy_token and legacy_base not in seen:
            normalized.insert(0, GitLabCredentialConfig(base_url=legacy_base, private_token=legacy_token))

        self.credentials = normalized
        return self

    def base_url_from_repo_url(self, repo_url: str) -> str:
        return _gitlab_base_url_from_repo_url(repo_url)

    def resolve_base_url(self, repo_url: str = "") -> str:
        base_url = self.base_url_from_repo_url(repo_url) or self.base_url
        return self.access_base_url_for_base_url(base_url)

    def access_base_url_for_base_url(self, base_url: str) -> str:
        target = _normalize_gitlab_base_url(base_url)
        if not target:
            return ""
        for item in self.credentials:
            item_base = _normalize_gitlab_base_url(item.base_url)
            if item_base == target:
                return item_base
            if target in {_normalize_gitlab_base_url(alias) for alias in item.aliases}:
                return item_base
        return target

    def access_url_for_repo_url(self, repo_url: str) -> str:
        raw = (repo_url or "").strip()
        parsed = urlparse(raw)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return raw
        access_base = self.access_base_url_for_base_url(f"{parsed.scheme}://{parsed.netloc}")
        access = urlparse(access_base)
        if access.scheme not in ("http", "https") or not access.netloc:
            return raw
        return parsed._replace(scheme=access.scheme, netloc=access.netloc).geturl()

    def token_for_base_url(self, base_url: str) -> str:
        target = _normalize_gitlab_base_url(base_url)
        if target:
            for item in self.credentials:
                if _normalize_gitlab_base_url(item.base_url) == target:
                    return (item.private_token or "").strip()
                if target in {_normalize_gitlab_base_url(alias) for alias in item.aliases}:
                    return (item.private_token or "").strip()
            if not self.credentials:
                return self.private_token
        return ""

    def token_for_repo_url(self, repo_url: str) -> str:
        return self.token_for_base_url(self.resolve_base_url(repo_url))


class ExplorerConfig(BaseModel):
    """Explore sub-agent 运行配置。"""
    max_steps: int = 200
    token_budget: int = 40000
    timeout_sec: int = 120


class ReportConfig(BaseModel):
    """超长 Agent 回复转 HTML 报告配置。

    - base_url           : 钉钉里点击跳转用的对外基址，例如 https://viktor.example.com（无尾斜杠）
    - threshold_chars    : 钉钉消息字符阈值，超过则改发简述 + 报告链接
    - summary_max_chars  : 钉钉里简述部分的字符上限
    - ttl_days           : 报告保留天数，启动时清理过期
    """
    base_url: str = "https://viktor.example.com"
    threshold_chars: int = 800
    summary_max_chars: int = 600
    ttl_days: int = 30


class CodeInspectionConfig(BaseModel):
    """代码自省能力（一期）配置。

    - enabled             : 总开关（即使关闭，项目仍需 git_url 非空才会启用）
    - cache_root          : 本地 / Pod 上的仓库缓存根目录
    - max_commits_per_repo: 每个项目保留的 commit workspace 个数（LRU 清理）
    - git_binary          : git 可执行文件
    - clone_timeout_sec   : 单次 clone/checkout 的硬超时
    """
    enabled: bool = True
    cache_root: str = "/var/cache/viktor/repositories"
    max_commits_per_repo: int = 3
    git_binary: str = "git"
    clone_timeout_sec: int = 300
    explorer: ExplorerConfig = ExplorerConfig()


class RepoDebugRunnerConfig(BaseModel):
    """仓库调试脚本执行配置。

    - enabled          : 是否启用仓库调试脚本执行工具
    - allow_write      : 是否允许 agent 在仓库缓存中写临时复现/验证脚本
    - timeout_sec      : 默认执行超时
    - max_timeout_sec  : 单次执行超时硬上限
    - output_chars     : 默认 stdout/stderr 返回字符数
    - max_output_chars : stdout/stderr 返回字符数硬上限
    """
    enabled: bool = True
    allow_write: bool = True
    timeout_sec: int = 60
    max_timeout_sec: int = 120
    output_chars: int = 12000
    max_output_chars: int = 20000


class RepoVenvConfig(BaseModel):
    """仓库虚拟环境配置。

    为每个仓库懒加载一个隔离 venv 并安装其依赖，让 agent 在 web 对话里写的复现脚本
    能 import 项目自己的三方依赖。venv 按 repo 跨 commit 复用（放在 per-sha workspace
    的父目录），按依赖文件指纹决定是否重装。

    - enabled              : 总开关
    - auto_install         : setup_repo_venv 默认是否安装依赖（关掉则只建空环境）
    - base_python          : 创建 venv 的基础解释器；留空用 Viktor 当前 sys.executable
    - dir_name             : venv 目录名
    - install_timeout_sec  : 单次 pip 安装默认超时
    - max_install_timeout_sec : 安装超时硬上限
    - index_url            : pip 主源（默认阿里云镜像，部署在阿里云内网更快）
    - extra_index_url      : pip 备用源
    - trusted_host         : 免 TLS 校验主机（与 index_url 配套）
    - install_project      : 是否对 pyproject/setup.py 做 `pip install -e .`
                             （源码已在 PYTHONPATH，import 项目包通常无需安装，默认关）
    - pip_log_chars        : 返回给 agent 的 pip 日志字符上限
    """
    enabled: bool = True
    auto_install: bool = True
    base_python: str = ""
    dir_name: str = ".venv"
    install_timeout_sec: int = 600
    max_install_timeout_sec: int = 1800
    index_url: str = "https://mirrors.aliyun.com/pypi/simple/"
    extra_index_url: str = ""
    trusted_host: str = "mirrors.aliyun.com"
    install_project: bool = False
    pip_log_chars: int = 8000


class RepoWarmupConfig(BaseModel):
    """仓库预热配置。

    启动期与注册期在后台并行把已注册仓库 clone 好、venv 建好装好依赖，避免用户在
    对话里等待几分钟的首次懒加载。预热幂等（已 clone 秒回、依赖指纹未变跳过）。

    - enabled      : 总开关（关掉则退回懒加载，仅在 agent 用到时才建）
    - concurrency  : 并行预热的仓库数上限（git clone / pip install 较重，别开太大）
    - build_venv   : 预热是否同时建 venv 装依赖（关掉则只 clone 仓库，venv 按需）
    """
    enabled: bool = True
    concurrency: int = 3
    build_venv: bool = True


class CodingAgentConfig(BaseModel):
    """Coding Agent 后台任务配置。"""
    enabled: bool = True
    workspace_root: str = "/var/cache/viktor/coding"
    max_steps: int = 1000
    command_timeout_sec: int = 120
    task_timeout_sec: int = 3600
    git_binary: str = "git"
    default_create_mr: bool = False
    git_author_name: str = "Viktor Coding Agent"
    git_author_email: str = "viktor-coding-agent@example.com"

    # --- Job 执行器：coding task 不再跑在 web pod 线程里，而是每任务一个 K8s Job ---
    # web pod 滚动重启不再杀任务；状态/control 全走 DB，Job 自治。
    executor: str = "job"                  # "job"（默认）| "thread"（逃生开关，本地无 K8s 时用）
    job_image: str = ""                    # 空→读 VIKTOR_JOB_IMAGE 环境变量，再空→自查本 pod 镜像
    job_namespace: str = ""                # 空→回退 k8s_config.namespace（prod: video-tracker）
    job_service_account: str = "viktor-coding-job"
    job_pvc_name: str = "viktor-cache-nas"
    job_concurrency_limit: int = 5         # 同时活跃的 coding Job 上限
    job_ttl_seconds: int = 1800            # Job 完成后的 ttlSecondsAfterFinished
    job_resources: dict = Field(default_factory=lambda: {
        "requests": {"cpu": "500m", "memory": "1Gi"},
        "limits": {"cpu": "2", "memory": "4Gi"},
    })
    # 孤儿回收：Job 丢失（pod 崩/被删/超 activeDeadline）后把卡住的任务翻成 failed（可 resume）。
    reconcile_enabled: bool = True
    reconcile_interval_seconds: int = 60
    reconcile_grace_seconds: int = 120     # 任务 updated_at 早于此窗口才考虑回收，避免误杀刚派发的
    cancel_force_seconds: int = 120        # cancelling 超此时长且 Job 仍活跃 → 强删 Job


class WatchdogConfig(BaseModel):
    """Watchdog 调度器全局配置。"""
    enabled: bool = True
    max_concurrent_runs: int = 3           # 最大同时执行的 watchdog 数
    default_cooldown_minutes: int = 30     # 默认冷却时间（分钟）
    default_max_execution_sec: int = 300   # 单次执行超时（秒）
    agent_max_iterations: int = 15         # Agent 分析最大迭代轮数
    plan_wait_timeout_sec: int = 600       # 等待 CodingTask Plan 生成的超时（秒）


class TemporalConfig(BaseModel):
    """Temporal 编排配置：issue-intake → coding-task 的 durable workflow。

    Temporal = 编排大脑 + 唯一写者；现有 DB 表 = 读模型投影。enabled=false 时
    保留旧 watcher/reconciler 链路（迁移期逃生开关）。
    """
    enabled: bool = False                  # 默认关；切换时由 worker/web 显式打开
    host: str = "127.0.0.1"                # Temporal frontend host
    port: int = 7233                       # Temporal frontend gRPC 端口
    namespace: str = "default"
    task_queue: str = "viktor-coding"
    # blueprint 阶段：fan-out 前先收敛路由 + 定跨仓契约，过一道人审 gate。
    # 默认关 → IssueLinkWorkflow 走原 fan-out（灰度开关，开后只影响新 link）。
    blueprint_enabled: bool = False
    blueprint_review_timeout_sec: int = 86400  # blueprint 人审等待升级阈值
    # 人审 gate 的默认超时（秒），超时发钉钉升级，不强制推进
    clarification_timeout_sec: int = 86400  # 澄清等待升级阈值（1 天）
    plan_review_timeout_sec: int = 86400    # plan 审批等待升级阈值
    # 轮询 coding Job 终态的间隔（秒）：durable，崩溃可恢复
    job_poll_interval_sec: int = 15
    # await MR 合并的轮询兜底间隔（秒）：webhook 漏发时主动查 GitLab
    merge_poll_interval_sec: int = 300
    activity_schedule_to_close_sec: int = 1800   # 普通 activity 重试封顶，耗尽落 failed
    dispatch_schedule_to_close_sec: int = 7200   # dispatch_job 专用，远大于普通（并发满合法等待）
    gate_escalation_interval_sec: int = 86400    # 人审 gate 周期性重发升级间隔
    # LLM 限流类失败（429/TPM 打满）的编排级重试：可恢复，不当作终态失败。
    rate_limit_max_retries: int = 5              # 超过则落 failed 终态
    rate_limit_backoff_sec: int = 90             # 首次退避（秒）；与每分钟 TPM 窗口对齐，之后指数退避
    rate_limit_backoff_max_sec: int = 600        # 指数退避上限（秒）


class StagingAcceptanceGitLabConfig(BaseModel):
    """Staging 验收在 GitLab 上使用的状态上下文。"""

    status_context: str = "viktor/staging"


class StagingAcceptanceLockConfig(BaseModel):
    """单测试环境锁租约配置。"""

    lease_sec: int = 7200
    heartbeat_sec: int = 30


class StagingAcceptanceDeployConfig(BaseModel):
    """现有 dev 分支 CD 的等待配置。

    provider=dev_branch 表示 Viktor 只负责把候选分支集成到 deploy_branch，真实部署由
    公司已有「push dev 自动部署」链路触发；Viktor 用 deploy_wait_sec 做保守等待。
    """

    provider: str = "dev_branch"
    deploy_wait_sec: int = 300
    timeout_sec: int = 1800
    poll_interval_sec: int = 15


class StagingAcceptancePlaywrightConfig(BaseModel):
    """Playwright 验收 runner 配置。"""

    command: str = ""
    timeout_sec: int = 1800
    report_base_url: str = ""


class StagingAcceptanceConfig(BaseModel):
    """单测试环境 staging 验收配置。

    默认关闭；开启后 MR ready 会进入 staging 队列，Viktor 串行把候选分支集成到 dev。
    """

    enabled: bool = False
    env_id: str = "default-staging"
    deploy_branch: str = "dev"
    staging_url: str = ""
    workspace_root: str = "/var/cache/viktor/staging"
    restore_strategy: str = "revert"  # revert | force_with_lease
    gitlab: StagingAcceptanceGitLabConfig = StagingAcceptanceGitLabConfig()
    lock: StagingAcceptanceLockConfig = StagingAcceptanceLockConfig()
    deploy: StagingAcceptanceDeployConfig = StagingAcceptanceDeployConfig()
    playwright: StagingAcceptancePlaywrightConfig = StagingAcceptancePlaywrightConfig()


def _load_external_oss_config() -> dict[str, Any]:
    """Best-effort load of an external config.yaml::oss section for local compatibility."""
    configured_path = os.environ.get("VIKTOR_OSS_CONFIG_PATH", "").strip()
    if not configured_path:
        return {}
    path = Path(configured_path)
    if not path.exists():
        return {}
    run_mode = (
        os.environ.get("VIKTOR_OSS_RUN_MODE", "").strip()
        or os.environ.get("RUN_MODE", "local-test")
    ).lower()
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        resolved = _resolve_env_vars(raw)
        env_config = resolved.get(run_mode) if isinstance(resolved, dict) else None
        if isinstance(env_config, dict) and isinstance(env_config.get("oss"), dict):
            return dict(env_config["oss"])
    except Exception as e:  # noqa: BLE001
        logger.warning("加载外部 OSS 配置失败: path={}, error={}", path, e)
    return {}


def _non_empty_config(data: dict[str, Any] | None) -> dict[str, Any]:
    if not data:
        return {}
    return {k: v for k, v in data.items() if v not in (None, "")}


_config = _load_config()

llm_config = LLMConfig(**_config.get("llm", {}))
dingtalk_config = DingtalkConfig(**_config.get("dingtalk", {}))
server_config = ServerConfig(**_config.get("server", {}))
auth_config = AuthConfig(**_config.get("auth", {}))
k8s_config = KubernetesConfig(**_config.get("kubernetes", {}))
database_config = DatabaseConfig(**_config.get("database", {}))
aliyun_sls_config = AliyunSLSConfig(**_config.get("aliyun_sls", {}))
_oss_raw = {**_load_external_oss_config(), **_non_empty_config(_config.get("oss", {}))}
oss_config = OSSConfig(**_oss_raw)
file_upload_config = FileUploadConfig(**_config.get("file_upload", {}))
agent_config = AgentConfig(**_config.get("agent", {}))
intent_config = IntentConfig(**_config.get("intent", {}))
agent_audit_config = AgentAuditConfig(**_config.get("agent_audit", {}))
trace_evaluation_config = TraceEvaluationConfig(**_config.get("trace_evaluation", {}))
context_compaction_config = ContextCompactionConfig(**_config.get("context_compaction", {}))
gitlab_config = GitLabConfig(**_config.get("gitlab", {}))
ssh_tunnel_config = SSHTunnelConfig(**_config.get("ssh_tunnel", {}))
code_inspection_config = CodeInspectionConfig(**_config.get("code_inspection", {}))
repo_debug_runner_config = RepoDebugRunnerConfig(**_config.get("repo_debug_runner", {}))
repo_venv_config = RepoVenvConfig(**_config.get("repo_venv", {}))
repo_warmup_config = RepoWarmupConfig(**_config.get("repo_warmup", {}))
report_config = ReportConfig(**_config.get("report", {}))
coding_agent_config = CodingAgentConfig(**_config.get("coding_agent", {}))
watchdog_config = WatchdogConfig(**_config.get("watchdog", {}))
temporal_config = TemporalConfig(**_config.get("temporal", {}))
staging_acceptance_config = StagingAcceptanceConfig(**_config.get("staging_acceptance", {}))
