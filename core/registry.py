"""
注册中心：管理多项目的上下文、数据库连接器和钉钉群绑定。

所有业务相关信息通过 HTTP API 注册到此模块。
每个调用方是一个"项目"(project)，注册项按项目隔离，钉钉群通过 conversation_id 映射到项目。

持久化层：注册数据同时写入 MySQL（通过 core.models），启动时从 DB 全量加载到内存。
"""
import threading
from typing import Any, Literal, Optional

from loguru import logger
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine


# ============================================================
# 注册项数据模型
# ============================================================

class K8sWorkloadRef(BaseModel):
    """项目在 K8s 中的 workload 引用，用于「image → commit」真相源解析。

    仅在启用代码自省（project.git_url 非空）时有意义。
    """
    namespace: str
    kind: str = "Deployment"          # Deployment / StatefulSet
    name: str
    container: Optional[str] = None   # 多容器 Pod 时指定；None 表示取第一个


class ProjectItem(BaseModel):
    """项目定义。

    代码自省相关字段均为可选，缺省即「不启用代码自省」，老项目零影响：
    - git_url        : 项目代码仓库（http(s) 或 git@ 均可），None 表示不启用
    - default_branch : image 无 commit 标识时的兜底分支
    - k8s_workload   : 从哪个 Deployment 读线上 image 反查 commit；None 时退化为 default_branch
    """
    id: str
    name: str
    description: str = ""
    git_url: Optional[str] = None
    default_branch: str = "master"
    k8s_workload: Optional["K8sWorkloadRef"] = None


class RepositoryConnectorItem(BaseModel):
    """Repository Connector：项目关联的 Git 仓库。"""
    id: str
    project_id: str
    display_name: str = ""
    # 仓库职责描述：供 issue 自动路由器判断「什么需求该进这个仓库」。
    description: str = ""
    git_url: str
    default_branch: str = "master"
    k8s_workload: Optional["K8sWorkloadRef"] = None
    sort_order: int = 0
    # 是否为该仓库建 venv 并装依赖。False 时 warmup 只 clone 不建 venv。
    build_venv: bool = True
    # 多语言测试流程（B 层）。language 为空时由 detect_language 兜底；
    # test_command/lint_command 为空时回退到该语言内置默认（见 core.language_defaults）。
    language: str = ""
    test_command: str = ""
    lint_command: str = ""
    # 该仓库固定维护开发的钉钉手机号：MR 创建后通知 @ 此人。
    maintainer_mobile: str = ""


class ContextItem(BaseModel):
    """业务上下文片段，将注入到 system prompt 中。"""
    id: str
    project_id: str
    priority: int = 0
    content: str


class DatabaseConnectorItem(BaseModel):
    """数据库连接器连接配置。

    ssh_tunnel 字段语义：
    - None（默认）：直连该数据库连接器，不经 SSH 跳板机（适用于同集群 / 同 VPC / 公网直达）
    - 结构体：开启 SSH 隧道，字段缺省时从 settings.ssh_tunnel_config 回退默认值
    """
    id: str
    project_id: str
    type: str = "mysql"
    host: str
    port: int = 3306
    username: str
    password: str
    database: str
    readonly: bool = True
    charset: str = "utf8mb4"
    ssh_tunnel: Optional["SSHTunnelSpec"] = None


class LogConnectorItem(BaseModel):
    """项目级 Log Connector。

    Viktor 的 SLS Endpoint/AK/SK 走全局配置；这里存业务侧由用户指定的 project/logstore。
    一个 Viktor 项目可以注册多个日志连接器，用 id 区分服务或组件。
    """
    id: str
    project_id: str
    sls_project: str
    logstore: str
    display_name: str = ""
    description: str = ""
    enabled: bool = True


ExternalConnectorType = Literal[
    "redis",
    "object_storage",
    "queue",
    "vector_store",
    "http_service",
    "dingtalk_doc",
]


class ExternalConnectorItem(BaseModel):
    """外部证据连接器。

    用 connector_type 区分 Redis / OSS / RabbitMQ / Milvus-Zilliz / HTTP Service。
    config 存非敏感连接参数，secrets 存密码、token、AK/SK 等敏感项。
    """
    id: str
    project_id: str
    connector_type: ExternalConnectorType
    display_name: str = ""
    description: str = ""
    config: dict[str, Any] = Field(default_factory=dict)
    secrets: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class RuntimeContextItem(BaseModel):
    """Runtime Context：项目在生产环境中的运行态索引。

    它不是外部连接器，而是把 repo、K8s workload、cluster、SLS 日志、
    Service/Ingress 暴露入口与启动命令串起来的诊断地图。
    """
    id: str
    project_id: str
    environment: str = "prod"
    source_type: str = "kubevela"
    source_repo: str = ""
    source_path: str = ""
    app_name: str = ""
    namespace: str = ""
    workload_type: str = "Deployment"
    workload_name: str = ""
    service_name: str = ""
    clusters: list[str] = Field(default_factory=list)
    selector: dict[str, Any] = Field(default_factory=dict)
    labels: dict[str, Any] = Field(default_factory=dict)
    replicas: Optional[int] = None
    image: str = ""
    command: list[str] = Field(default_factory=list)
    ports: list[dict[str, Any]] = Field(default_factory=list)
    resources: dict[str, Any] = Field(default_factory=dict)
    probes: dict[str, Any] = Field(default_factory=dict)
    log_bindings: list[dict[str, Any]] = Field(default_factory=list)
    exposures: list[dict[str, Any]] = Field(default_factory=list)
    scheduling: dict[str, Any] = Field(default_factory=dict)
    config: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


SkillKind = Literal["business", "operational"]
SkillStatus = Literal["draft", "enabled", "disabled", "archived"]
SkillSourceType = Literal["manual", "anthropic_skill", "codex_skill", "repo_discovered", "import"]


class SkillTriggerExample(BaseModel):
    text: str
    source: str = "owner_text"
    confirmed: bool = False
    confidence: Optional[float] = None


class SkillContextRef(BaseModel):
    type: str
    id: str = ""
    name: str = ""
    required: bool = True
    purpose: str = ""


class SkillItem(BaseModel):
    """Skill：项目内可复用的方法与流程。

    Context 负责“能看什么”，Tool 负责“能做什么”，Skill 负责“什么时候按什么步骤组合它们”。
    trigger_examples 是语义检索样例，不是硬编码规则。
    """
    id: str
    project_id: str
    name: str
    kind: SkillKind = "business"
    scope: Literal["project", "global"] = "project"
    description: str = ""
    trigger_examples: list[SkillTriggerExample] = Field(default_factory=list)
    input_contract: dict[str, Any] = Field(default_factory=dict)
    required_contexts: list[SkillContextRef] = Field(default_factory=list)
    required_tools: list[str] = Field(default_factory=list)
    related_glossary_terms: list[str] = Field(default_factory=list)
    instructions: list[str] = Field(default_factory=list)
    output_contract: dict[str, Any] = Field(default_factory=dict)
    safety_policy: dict[str, Any] = Field(default_factory=dict)
    source_type: SkillSourceType = "manual"
    source_uri: str = ""
    raw_content: str = ""
    status: SkillStatus = "enabled"
    version: int = 1


class SSHTunnelSpec(BaseModel):
    """数据库连接器级 SSH 隧道配置（注册时指定，字段缺省即使用全局默认值）。"""
    jump_host: Optional[str] = None      # 缺省→ ssh_tunnel_config.jump_host
    jump_port: Optional[int] = None      # 缺省→ ssh_tunnel_config.jump_port
    username: Optional[str] = None       # 缺省→ ssh_tunnel_config.username
    private_key: Optional[str] = None    # 缺省→ ssh_tunnel_config.private_key


DatabaseConnectorItem.model_rebuild()


# ============================================================
# Watchdog 相关模型
# ============================================================

WatchdogProbeType = Literal["http", "sql_metric", "http_json_metric"]
WatchdogStatus = Literal["enabled", "disabled"]


class HttpProbeSpec(BaseModel):
    """HTTP 探针配置。"""
    url: str
    method: str = "GET"
    headers: dict[str, str] = Field(default_factory=dict)
    timeout_sec: int = 10
    expected_status: int = 200
    max_response_time_ms: int = 3000

    @model_validator(mode="before")
    @classmethod
    def _normalize_status_alias(cls, data: Any) -> Any:
        if isinstance(data, dict) and "expected_status" not in data and "expect_status" in data:
            data = dict(data)
            data["expected_status"] = data["expect_status"]
        return data


class SqlMetricProbeSpec(BaseModel):
    """SQL 指标探针配置。"""
    connector_id: str
    sql: str
    threshold: float
    operator: Literal["gt", "lt", "gte", "lte", "eq", "neq"]
    description: str = ""


class HttpJsonMetricProbeSpec(BaseModel):
    """HTTP JSON 指标探针配置。

    面向 crawler-console parser_working_summary 一类 JSON 摘要接口，同时保留通用
    metric_path + threshold/operator 的消费方式。
    """
    url: str
    fallback_urls: list[str] = Field(default_factory=list)
    method: str = "GET"
    headers: dict[str, str] = Field(default_factory=dict)
    timeout_sec: int = 10
    expected_status: int = 200
    summary_path: str = ""
    anomalous_parsers_path: str = ""
    parser_statistics_path: str = ""
    metric_path: str = ""
    threshold: Optional[float] = None
    operator: Optional[Literal["gt", "lt", "gte", "lte", "eq", "neq"]] = None
    failure_rate_threshold: Optional[float] = None
    failed_attempts_threshold: Optional[int] = None
    min_total_attempts: int = 0
    max_anomalous_parsers: int = 100
    description: str = ""

    @model_validator(mode="before")
    @classmethod
    def _normalize_status_alias(cls, data: Any) -> Any:
        if isinstance(data, dict) and "expected_status" not in data and "expect_status" in data:
            data = dict(data)
            data["expected_status"] = data["expect_status"]
        return data


class ProbeSpec(BaseModel):
    """探针规格（按 type 区分具体配置）。"""
    type: WatchdogProbeType
    http: Optional[HttpProbeSpec] = None
    sql_metric: Optional[SqlMetricProbeSpec] = None
    http_json_metric: Optional[HttpJsonMetricProbeSpec] = None


class WatchdogNotificationTarget(BaseModel):
    """钉钉报警群通知目标（自定义机器人 Webhook）。"""
    webhook_url: str
    sign_secret: str = ""
    at_mobiles: list[str] = Field(default_factory=list)
    at_all: bool = False


class WatchdogItem(BaseModel):
    """Watchdog 注册项：探针 + 调度 + 分析 + 通知 的完整定义。"""
    id: str
    project_id: str
    name: str
    description: str = ""
    probe: ProbeSpec
    schedule: str
    skill_ids: list[str] = Field(default_factory=list)
    notification: WatchdogNotificationTarget
    severity_filter: list[str] = Field(default_factory=lambda: ["critical"])
    auto_coding_plan: bool = False
    coding_repo_connector_id: str = ""
    cooldown_minutes: int = 30
    max_execution_sec: int = 300
    status: WatchdogStatus = "enabled"


class GroupBinding(BaseModel):
    """钉钉群与项目的绑定关系。"""
    conversation_id: str
    project_id: str
    group_name: str = ""


class GlossaryItem(BaseModel):
    """项目业务术语表条目。

    业务中文词 ↔ 代码关键词的映射，用于 LLM 搜索代码时做 query 扩展，
    尤其缓解「中文业务黑话 vs 英文代码符号」的命名鸿沟。
    """
    id: str
    project_id: str
    term: str                              # 主词，例: "下单"
    aliases: list[str] = Field(default_factory=list)         # 业务同义词，例: ["生单","出单"]
    code_keywords: list[str] = Field(default_factory=list)   # 代码侧关键词，例: ["createOrder","place_order"]
    description: str = ""                   # 可选：业务含义说明（<200 字）
    enabled: bool = True


KnowledgeNoteKind = Literal[
    "schema_convention",   # 库/表/字段级约定：时区、软删除、命名规律等
    "field_semantics",     # 单字段语义与联合判定：如 hide_flag=0 需结合 error_code
    "pitfall",             # 易错点/反模式/反例：写 SQL 前先看
    "metric_definition",   # 指标口径：日进量 / GMV 等如何定义
]

KnowledgeNoteSource = Literal["admin", "api", "import", "manual"]


class KnowledgeNoteItem(BaseModel):
    """业务知识笔记：在术语表之外沉淀字段语义 / 约定 / 坑位 / 指标定义。

    与 GlossaryItem 的分工：
    - Glossary 解决「中文 ↔ 代码符号」的词法映射，喂给 code_grep；
    - KnowledgeNote 解决「写 SQL 前必须知道的业务潜规则」，喂给 LLM 做查询推理。
    """
    id: str
    project_id: str
    kind: KnowledgeNoteKind
    scope: str = ""                         # 自由文本，例: "vt-db.video.hide_flag"，帮助 LLM 定位
    title: str                              # 一行摘要，必填
    content: str = ""                       # 详细规则/反例，markdown 友好（注入时会截断）
    tags: list[str] = Field(default_factory=list)
    enabled: bool = True
    source: KnowledgeNoteSource = "api"


def normalize_conversation_id(conversation_id: str) -> str:
    """钉钉 conversation_id 与内存/DB 映射使用同一规范化规则，避免首尾空白导致「已绑定仍提示未绑定」。"""
    return (conversation_id or "").strip()


# ============================================================
# 注册中心
# ============================================================

class Registry:
    """
    线程安全的注册中心单例。

    所有注册项按 project_id 隔离：
    - projects: 项目定义
    - contexts: 业务上下文片段 (project_id -> {id -> item})
    - database_connectors: 数据库连接 (project_id -> {id -> item})
    - group_bindings: 钉钉群绑定 (conversation_id -> project_id)
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._projects: dict[str, ProjectItem] = {}
        self._repository_connectors: dict[str, dict[str, RepositoryConnectorItem]] = {}  # project_id -> {id -> item}
        self._contexts: dict[str, dict[str, ContextItem]] = {}
        self._database_connectors: dict[str, dict[str, DatabaseConnectorItem]] = {}
        self._log_connectors: dict[str, dict[str, LogConnectorItem]] = {}
        self._external_connectors: dict[str, dict[str, ExternalConnectorItem]] = {}
        self._runtime_contexts: dict[str, dict[str, RuntimeContextItem]] = {}
        self._skills: dict[str, dict[str, SkillItem]] = {}
        self._engines: dict[str, dict[str, Engine]] = {}
        self._group_bindings: dict[str, str] = {}  # conversation_id -> project_id
        self._glossaries: dict[str, dict[str, GlossaryItem]] = {}  # project_id -> {glossary_id -> item}
        self._knowledge_notes: dict[str, dict[str, KnowledgeNoteItem]] = {}  # project_id -> {note_id -> item}
        self._watchdogs: dict[str, dict[str, WatchdogItem]] = {}  # project_id -> {watchdog_id -> item}

    # ---------- Project ----------

    def register_project(self, item: ProjectItem) -> None:
        with self._lock:
            self._projects[item.id] = item
            self._repository_connectors.setdefault(item.id, {})
            self._contexts.setdefault(item.id, {})
            self._database_connectors.setdefault(item.id, {})
            self._log_connectors.setdefault(item.id, {})
            self._external_connectors.setdefault(item.id, {})
            self._runtime_contexts.setdefault(item.id, {})
            self._skills.setdefault(item.id, {})
            self._engines.setdefault(item.id, {})
            self._glossaries.setdefault(item.id, {})
            self._knowledge_notes.setdefault(item.id, {})
            self._watchdogs.setdefault(item.id, {})
        logger.info("注册项目: id={}, name={}", item.id, item.name)

    def unregister_project(self, project_id: str) -> bool:
        with self._lock:
            removed = self._projects.pop(project_id, None)
            if not removed:
                return False
            self._repository_connectors.pop(project_id, None)
            self._contexts.pop(project_id, None)
            self._log_connectors.pop(project_id, None)
            self._external_connectors.pop(project_id, None)
            self._runtime_contexts.pop(project_id, None)
            self._skills.pop(project_id, None)
            ds_items = self._database_connectors.pop(project_id, {})
            engines = self._engines.pop(project_id, {})
            self._glossaries.pop(project_id, None)
            self._knowledge_notes.pop(project_id, None)
            self._watchdogs.pop(project_id, None)
            bindings_to_remove = [
                cid for cid, pid in self._group_bindings.items()
                if pid == project_id
            ]
            for cid in bindings_to_remove:
                del self._group_bindings[cid]
        for engine in engines.values():
            engine.dispose()
        logger.info("注销项目: id={} (含 {} 个数据库连接器连接)", project_id, len(ds_items))
        return True

    def get_project(self, project_id: str) -> Optional[ProjectItem]:
        with self._lock:
            return self._projects.get(project_id)

    def list_projects(self) -> list[ProjectItem]:
        """返回所有已注册项目（供仓库预热等全量遍历使用）。"""
        with self._lock:
            return list(self._projects.values())

    def _assert_project_exists(self, project_id: str) -> None:
        if project_id not in self._projects:
            raise ValueError(f"项目 '{project_id}' 不存在，请先注册项目")

    # ---------- Repository Connector ----------

    def register_repository_connector(self, item: RepositoryConnectorItem) -> None:
        with self._lock:
            self._assert_project_exists(item.project_id)
            self._repository_connectors[item.project_id][item.id] = item
        logger.info("注册 Repository Connector: project={}, id={}, url={}", item.project_id, item.id, item.git_url)

    def unregister_repository_connector(self, project_id: str, connector_id: str) -> bool:
        with self._lock:
            bucket = self._repository_connectors.get(project_id, {})
            removed = bucket.pop(connector_id, None)
        if removed:
            logger.info("注销 Repository Connector: project={}, id={}", project_id, connector_id)
        return removed is not None

    def get_repository_connectors(self, project_id: str) -> list[RepositoryConnectorItem]:
        with self._lock:
            items = list(self._repository_connectors.get(project_id, {}).values())
        return sorted(items, key=lambda r: r.sort_order)

    def get_repository_connector(self, project_id: str, connector_id: str) -> Optional[RepositoryConnectorItem]:
        with self._lock:
            return self._repository_connectors.get(project_id, {}).get(connector_id)

    # ---------- Group Binding ----------

    def bind_group(self, conversation_id: str, project_id: str) -> None:
        conversation_id = normalize_conversation_id(conversation_id)
        with self._lock:
            self._assert_project_exists(project_id)
            self._group_bindings[conversation_id] = project_id
        logger.info("绑定群: conversation_id={} -> project_id={}", conversation_id, project_id)

    def unbind_group(self, conversation_id: str) -> bool:
        conversation_id = normalize_conversation_id(conversation_id)
        with self._lock:
            removed = self._group_bindings.pop(conversation_id, None)
        if removed:
            logger.info("解绑群: conversation_id={}", conversation_id)
        return removed is not None

    def get_project_by_conversation(self, conversation_id: str) -> Optional[str]:
        """根据钉钉群 conversation_id 查找绑定的 project_id。"""
        conversation_id = normalize_conversation_id(conversation_id)
        with self._lock:
            return self._group_bindings.get(conversation_id)

    # ---------- Context ----------

    def register_context(self, item: ContextItem) -> None:
        with self._lock:
            self._assert_project_exists(item.project_id)
            self._contexts[item.project_id][item.id] = item
        logger.info("注册上下文: project={}, id={}", item.project_id, item.id)

    def unregister_context(self, project_id: str, context_id: str) -> bool:
        with self._lock:
            bucket = self._contexts.get(project_id, {})
            removed = bucket.pop(context_id, None)
        if removed:
            logger.info("注销上下文: project={}, id={}", project_id, context_id)
        return removed is not None

    def get_contexts(self, project_id: str) -> list[ContextItem]:
        """获取指定项目的所有上下文，按 priority 升序排列。"""
        with self._lock:
            items = list(self._contexts.get(project_id, {}).values())
        return sorted(items, key=lambda c: c.priority)

    # ---------- Database Connector ----------

    def register_database_connector(self, item: DatabaseConnectorItem) -> None:
        url = (
            f"mysql+pymysql://{item.username}:{item.password}"
            f"@{item.host}:{item.port}/{item.database}"
            f"?charset={item.charset}"
        )
        engine = create_engine(
            url,
            pool_size=3,
            max_overflow=2,
            pool_recycle=1800,
            pool_pre_ping=True,
            echo=False,
        )
        with self._lock:
            self._assert_project_exists(item.project_id)
            old_engine = self._engines.get(item.project_id, {}).get(item.id)
            if old_engine:
                old_engine.dispose()
            self._database_connectors[item.project_id][item.id] = item
            self._engines[item.project_id][item.id] = engine
        logger.info("注册数据库连接器: project={}, id={}, database={}", item.project_id, item.id, item.database)

    def unregister_database_connector(self, project_id: str, connector_id: str) -> bool:
        with self._lock:
            ds_bucket = self._database_connectors.get(project_id, {})
            eng_bucket = self._engines.get(project_id, {})
            removed = ds_bucket.pop(connector_id, None)
            engine = eng_bucket.pop(connector_id, None)
        if engine:
            engine.dispose()
        if removed:
            logger.info("注销数据库连接器: project={}, id={}", project_id, connector_id)
        return removed is not None

    def get_engine(self, project_id: str, connector_id: str) -> Optional[Engine]:
        with self._lock:
            return self._engines.get(project_id, {}).get(connector_id)

    # ---------- Log Connector ----------

    def register_log_connector(self, item: LogConnectorItem) -> None:
        with self._lock:
            self._assert_project_exists(item.project_id)
            self._log_connectors[item.project_id][item.id] = item
        logger.info(
            "注册 Log Connector: project={}, id={}, sls_project={}, logstore={}",
            item.project_id, item.id, item.sls_project, item.logstore,
        )

    def unregister_log_connector(self, project_id: str, connector_id: str) -> bool:
        with self._lock:
            bucket = self._log_connectors.get(project_id, {})
            removed = bucket.pop(connector_id, None)
        if removed:
            logger.info("注销 Log Connector: project={}, id={}", project_id, connector_id)
        return removed is not None

    def get_log_connectors(
        self,
        project_id: str,
        only_enabled: bool = True,
    ) -> list[LogConnectorItem]:
        with self._lock:
            items = list(self._log_connectors.get(project_id, {}).values())
        if only_enabled:
            items = [item for item in items if item.enabled]
        return items

    def get_log_connector(self, project_id: str, connector_id: str) -> Optional[LogConnectorItem]:
        with self._lock:
            return self._log_connectors.get(project_id, {}).get(connector_id)

    # ---------- External Connector ----------

    def register_external_connector(self, item: ExternalConnectorItem) -> None:
        with self._lock:
            self._assert_project_exists(item.project_id)
            self._external_connectors[item.project_id][item.id] = item
        logger.info(
            "注册 External Connector: project={}, id={}, type={}",
            item.project_id, item.id, item.connector_type,
        )

    def unregister_external_connector(self, project_id: str, connector_id: str) -> bool:
        with self._lock:
            bucket = self._external_connectors.get(project_id, {})
            removed = bucket.pop(connector_id, None)
        if removed:
            logger.info("注销 External Connector: project={}, id={}", project_id, connector_id)
        return removed is not None

    def get_external_connectors(
        self,
        project_id: str,
        connector_type: Optional[str] = None,
        only_enabled: bool = True,
    ) -> list[ExternalConnectorItem]:
        with self._lock:
            items = list(self._external_connectors.get(project_id, {}).values())
        if connector_type:
            items = [item for item in items if item.connector_type == connector_type]
        if only_enabled:
            items = [item for item in items if item.enabled]
        return sorted(items, key=lambda item: (item.connector_type, item.id))

    def get_external_connector(self, project_id: str, connector_id: str) -> Optional[ExternalConnectorItem]:
        with self._lock:
            return self._external_connectors.get(project_id, {}).get(connector_id)

    # ---------- Runtime Context ----------

    def register_runtime_context(self, item: RuntimeContextItem) -> None:
        with self._lock:
            self._assert_project_exists(item.project_id)
            self._runtime_contexts[item.project_id][item.id] = item
        logger.info(
            "注册 Runtime Context: project={}, id={}, env={}, clusters={}, workload={}",
            item.project_id, item.id, item.environment, item.clusters, item.workload_name,
        )

    def unregister_runtime_context(self, project_id: str, runtime_id: str) -> bool:
        with self._lock:
            bucket = self._runtime_contexts.get(project_id, {})
            removed = bucket.pop(runtime_id, None)
        if removed:
            logger.info("注销 Runtime Context: project={}, id={}", project_id, runtime_id)
        return removed is not None

    def get_runtime_contexts(
        self,
        project_id: str,
        environment: Optional[str] = None,
        cluster: Optional[str] = None,
        only_enabled: bool = True,
    ) -> list[RuntimeContextItem]:
        with self._lock:
            items = list(self._runtime_contexts.get(project_id, {}).values())
        if only_enabled:
            items = [item for item in items if item.enabled]
        if environment:
            items = [item for item in items if item.environment == environment]
        if cluster:
            items = [item for item in items if cluster in item.clusters]
        return sorted(items, key=lambda item: (item.environment, item.workload_name or item.id))

    def get_runtime_context(self, project_id: str, runtime_id: str) -> Optional[RuntimeContextItem]:
        with self._lock:
            return self._runtime_contexts.get(project_id, {}).get(runtime_id)

    # ---------- Skill ----------

    def register_skill(self, item: SkillItem) -> None:
        with self._lock:
            self._assert_project_exists(item.project_id)
            self._skills[item.project_id][item.id] = item
        logger.info(
            "注册 Skill: project={}, id={}, kind={}, status={}",
            item.project_id, item.id, item.kind, item.status,
        )

    def unregister_skill(self, project_id: str, skill_id: str) -> bool:
        with self._lock:
            bucket = self._skills.get(project_id, {})
            removed = bucket.pop(skill_id, None)
        if removed:
            logger.info("注销 Skill: project={}, id={}", project_id, skill_id)
        return removed is not None

    def get_skills(
        self,
        project_id: str,
        kind: Optional[str] = None,
        only_enabled: bool = True,
    ) -> list[SkillItem]:
        with self._lock:
            items = list(self._skills.get(project_id, {}).values())
        if only_enabled:
            items = [item for item in items if item.status == "enabled"]
        if kind:
            items = [item for item in items if item.kind == kind]
        return sorted(items, key=lambda item: (item.kind, item.name or item.id))

    def get_skill(self, project_id: str, skill_id: str) -> Optional[SkillItem]:
        with self._lock:
            return self._skills.get(project_id, {}).get(skill_id)

    # ---------- Glossary ----------

    def register_glossary(self, item: GlossaryItem) -> None:
        with self._lock:
            self._assert_project_exists(item.project_id)
            self._glossaries[item.project_id][item.id] = item
        logger.info("注册术语: project={}, id={}, term={}", item.project_id, item.id, item.term)

    def unregister_glossary(self, project_id: str, glossary_id: str) -> bool:
        with self._lock:
            bucket = self._glossaries.get(project_id, {})
            removed = bucket.pop(glossary_id, None)
        if removed:
            logger.info("注销术语: project={}, id={}", project_id, glossary_id)
        return removed is not None

    def get_glossaries(self, project_id: str, only_enabled: bool = True) -> list[GlossaryItem]:
        """获取指定项目的业务术语表。"""
        with self._lock:
            items = list(self._glossaries.get(project_id, {}).values())
        if only_enabled:
            items = [it for it in items if it.enabled]
        return items

    def get_glossary(self, project_id: str, glossary_id: str) -> Optional[GlossaryItem]:
        with self._lock:
            return self._glossaries.get(project_id, {}).get(glossary_id)

    # ---------- Knowledge Note ----------

    def register_knowledge_note(self, item: KnowledgeNoteItem) -> None:
        with self._lock:
            self._assert_project_exists(item.project_id)
            self._knowledge_notes[item.project_id][item.id] = item
        logger.info(
            "注册知识笔记: project={}, id={}, kind={}, title={}",
            item.project_id, item.id, item.kind, item.title,
        )

    def unregister_knowledge_note(self, project_id: str, note_id: str) -> bool:
        with self._lock:
            bucket = self._knowledge_notes.get(project_id, {})
            removed = bucket.pop(note_id, None)
        if removed:
            logger.info("注销知识笔记: project={}, id={}", project_id, note_id)
        return removed is not None

    def get_knowledge_notes(
        self,
        project_id: str,
        kind: Optional[str] = None,
        only_enabled: bool = True,
    ) -> list[KnowledgeNoteItem]:
        """获取指定项目的知识笔记，支持按 kind 过滤。"""
        with self._lock:
            items = list(self._knowledge_notes.get(project_id, {}).values())
        if only_enabled:
            items = [it for it in items if it.enabled]
        if kind:
            items = [it for it in items if it.kind == kind]
        return items

    def get_knowledge_note(self, project_id: str, note_id: str) -> Optional[KnowledgeNoteItem]:
        with self._lock:
            return self._knowledge_notes.get(project_id, {}).get(note_id)

    # ---------- Watchdog ----------

    def register_watchdog(self, item: WatchdogItem) -> None:
        with self._lock:
            self._assert_project_exists(item.project_id)
            self._watchdogs[item.project_id][item.id] = item
        logger.info(
            "注册 Watchdog: project={}, id={}, schedule={}",
            item.project_id, item.id, item.schedule,
        )

    def unregister_watchdog(self, project_id: str, watchdog_id: str) -> bool:
        with self._lock:
            bucket = self._watchdogs.get(project_id, {})
            removed = bucket.pop(watchdog_id, None)
        if removed:
            logger.info("注销 Watchdog: project={}, id={}", project_id, watchdog_id)
        return removed is not None

    def get_watchdogs(self, project_id: str, only_enabled: bool = True) -> list[WatchdogItem]:
        with self._lock:
            items = list(self._watchdogs.get(project_id, {}).values())
        if only_enabled:
            items = [item for item in items if item.status == "enabled"]
        return sorted(items, key=lambda item: item.name or item.id)

    def get_watchdog(self, project_id: str, watchdog_id: str) -> Optional[WatchdogItem]:
        with self._lock:
            return self._watchdogs.get(project_id, {}).get(watchdog_id)

    def get_all_enabled_watchdogs(self) -> list[WatchdogItem]:
        """跨项目获取所有已启用的 watchdog（供调度器使用）。"""
        with self._lock:
            result: list[WatchdogItem] = []
            for project_watchdogs in self._watchdogs.values():
                for item in project_watchdogs.values():
                    if item.status == "enabled":
                        result.append(item)
        return result

    def resolve_skills_for_watchdog(self, project_id: str, skill_ids: list[str]) -> list[SkillItem]:
        """解析 watchdog 绑定的 skill 列表：先从本项目找，再从 global scope 找。"""
        with self._lock:
            result: list[SkillItem] = []
            project_skills = self._skills.get(project_id, {})
            for skill_id in skill_ids:
                # 先在本项目中找
                skill = project_skills.get(skill_id)
                if skill and skill.status == "enabled":
                    result.append(skill)
                    continue
                # 再在所有项目中找 scope=global 的
                for pid, skills_map in self._skills.items():
                    s = skills_map.get(skill_id)
                    if s and s.scope == "global" and s.status == "enabled":
                        result.append(s)
                        break
        return result

    # ---------- Status ----------

    def is_ready(self, project_id: str) -> bool:
        """至少具备一条业务上下文即可启用 Agent（SQL 由模型通过 schema 工具自拟）。"""
        with self._lock:
            contexts = self._contexts.get(project_id, {})
            return len(contexts) > 0

    def get_status(self) -> dict:
        with self._lock:
            projects_status = {}
            for pid, project in self._projects.items():
                repository_connectors = self._repository_connectors.get(pid, {})
                contexts = self._contexts.get(pid, {})
                database_connectors = self._database_connectors.get(pid, {})
                log_connectors = self._log_connectors.get(pid, {})
                external_connectors = self._external_connectors.get(pid, {})
                runtime_contexts = self._runtime_contexts.get(pid, {})
                skills = self._skills.get(pid, {})
                watchdogs = self._watchdogs.get(pid, {})
                bound_groups = [
                    cid for cid, p in self._group_bindings.items() if p == pid
                ]
                projects_status[pid] = {
                    "name": project.name,
                    "ready": len(contexts) > 0,
                    "repository_connectors": list(repository_connectors.keys()),
                    "contexts": list(contexts.keys()),
                    "database_connectors": list(database_connectors.keys()),
                    "log_connectors": list(log_connectors.keys()),
                    "external_connectors": list(external_connectors.keys()),
                    "runtime_contexts": list(runtime_contexts.keys()),
                    "skills": list(skills.keys()),
                    "watchdogs": list(watchdogs.keys()),
                    "bound_groups": bound_groups,
                }
            return {
                "project_count": len(self._projects),
                "projects": projects_status,
            }

    # ---------- 从 DB 全量加载 ----------

    def load_from_db(self) -> None:
        """启动时从 MySQL 全量加载注册数据到内存，重建连接池。"""
        from core.database import SessionLocal
        from core.models import (
            ProjectModel,
            RepositoryConnectorModel,
            ContextModel,
            DatabaseConnectorModel,
            LogConnectorModel,
            ExternalConnectorModel,
            RuntimeContextModel,
            SkillModel,
            GroupBindingModel,
            GlossaryModel,
            KnowledgeNoteModel,
            WatchdogModel,
        )
        from core.registry_persistence import (
            database_connector_item_from_model,
            external_connector_item_from_model,
            glossary_item_from_model,
            knowledge_note_item_from_model,
            log_connector_item_from_model,
            runtime_context_item_from_model,
            skill_item_from_model,
        )

        session = SessionLocal()
        try:
            projects = session.query(ProjectModel).all()
            for row in projects:
                workload_raw = getattr(row, "k8s_workload", None)
                workload_spec = (
                    K8sWorkloadRef(**workload_raw)
                    if isinstance(workload_raw, dict)
                    else None
                )
                self.register_project(ProjectItem(
                    id=row.project_id, name=row.name, description=row.description or "",
                    git_url=getattr(row, "git_url", None) or None,
                    default_branch=getattr(row, "default_branch", None) or "master",
                    k8s_workload=workload_spec,
                ))

            # 加载Repository Connector
            repository_connectors = session.query(RepositoryConnectorModel).all()
            for row in repository_connectors:
                wl_raw = getattr(row, "k8s_workload", None)
                wl_spec = K8sWorkloadRef(**wl_raw) if isinstance(wl_raw, dict) else None
                try:
                    self.register_repository_connector(RepositoryConnectorItem(
                        id=row.connector_id, project_id=row.project_id,
                        display_name=row.display_name or "",
                        description=getattr(row, "description", "") or "",
                        git_url=row.git_url,
                        default_branch=row.default_branch or "master",
                        k8s_workload=wl_spec,
                        sort_order=row.sort_order or 0,
                        build_venv=bool(getattr(row, "build_venv", 1)),
                        language=getattr(row, "language", "") or "",
                        test_command=getattr(row, "test_command", "") or "",
                        lint_command=getattr(row, "lint_command", "") or "",
                        maintainer_mobile=getattr(row, "maintainer_mobile", "") or "",
                    ))
                except ValueError:
                    logger.warning("跳过仓库 {}/{}: 项目不存在", row.project_id, row.connector_id)

            database_connectors = session.query(DatabaseConnectorModel).all()
            for row in database_connectors:
                self.register_database_connector(database_connector_item_from_model(row))

            log_connectors = session.query(LogConnectorModel).all()
            for row in log_connectors:
                try:
                    self.register_log_connector(log_connector_item_from_model(row))
                except ValueError:
                    logger.warning("跳过 Log Connector {}/{}: 项目不存在", row.project_id, row.connector_id)

            external_connectors = session.query(ExternalConnectorModel).all()
            for row in external_connectors:
                try:
                    self.register_external_connector(external_connector_item_from_model(row))
                except ValueError:
                    logger.warning("跳过 External Connector {}/{}: 项目不存在", row.project_id, row.connector_id)

            runtime_contexts = session.query(RuntimeContextModel).all()
            for row in runtime_contexts:
                try:
                    self.register_runtime_context(runtime_context_item_from_model(row))
                except ValueError:
                    logger.warning("跳过 Runtime Context {}/{}: 项目不存在", row.project_id, row.runtime_id)

            skills = session.query(SkillModel).all()
            for row in skills:
                try:
                    self.register_skill(skill_item_from_model(row))
                except ValueError:
                    logger.warning("跳过 Skill {}/{}: 项目不存在", row.project_id, row.skill_id)

            contexts = session.query(ContextModel).all()
            for row in contexts:
                self.register_context(ContextItem(
                    id=row.context_id, project_id=row.project_id,
                    priority=row.priority, content=row.content,
                ))

            bindings = session.query(GroupBindingModel).all()
            for row in bindings:
                self.bind_group(row.conversation_id, row.project_id)

            # 加载业务术语表
            glossaries = session.query(GlossaryModel).all()
            for row in glossaries:
                self.register_glossary(glossary_item_from_model(row))

            # 加载知识笔记
            notes = session.query(KnowledgeNoteModel).all()
            for row in notes:
                self.register_knowledge_note(knowledge_note_item_from_model(row))

            # 加载 Watchdog
            watchdogs = session.query(WatchdogModel).filter(WatchdogModel.enabled == 1).all()
            for row in watchdogs:
                try:
                    self.register_watchdog(WatchdogItem(
                        id=row.watchdog_id,
                        project_id=row.project_id,
                        name=row.name or "",
                        description=row.description or "",
                        probe=row.probe,
                        schedule=row.schedule,
                        skill_ids=list(row.skill_ids or []),
                        notification=row.notification,
                        severity_filter=list(row.severity_filter or []),
                        auto_coding_plan=bool(row.auto_coding_plan),
                        coding_repo_connector_id=row.coding_repo_connector_id or "",
                        cooldown_minutes=row.cooldown_minutes or 30,
                        max_execution_sec=row.max_execution_sec or 300,
                        status="enabled",
                    ))
                except ValueError:
                    logger.warning("跳过 Watchdog {}/{}: 项目不存在", row.project_id, row.watchdog_id)

            logger.info(
                "从 DB 加载完成: {} 个项目, {} 个仓库, {} 个数据库连接器, {} 个 Log Connector, {} 个 External Connector, {} 个 Runtime Context, {} 个 Skill, {} 个上下文, {} 个群绑定, {} 个术语, {} 条知识笔记, {} 个 Watchdog",
                len(projects),
                len(repository_connectors),
                len(database_connectors),
                len(log_connectors),
                len(external_connectors),
                len(runtime_contexts),
                len(skills),
                len(contexts),
                len(bindings),
                len(glossaries),
                len(notes),
                len(watchdogs),
            )
        except Exception:
            logger.exception("从 DB 加载注册数据失败")
            raise
        finally:
            session.close()


# 全局单例
registry = Registry()
