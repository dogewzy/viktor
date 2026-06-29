"""SQLAlchemy ORM 模型，对应 viktor_* 系列表。"""
from datetime import datetime

from sqlalchemy import (
    Column, String, Integer, Text, JSON, SmallInteger, DateTime, BigInteger, Float, Index,
)
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class ProjectModel(Base):
    __tablename__ = "viktor_projects"

    project_id = Column(String(128), primary_key=True)
    name = Column(String(255), nullable=False, default="")
    description = Column(String(2048), nullable=False, default="")
    # 代码自省相关字段（可选，缺省即不启用）
    git_url = Column(String(512), nullable=True, default=None)
    default_branch = Column(String(128), nullable=True, default="master")
    # K8s workload 引用：{namespace, kind, name, container}，用于 image→commit 反查
    k8s_workload = Column(JSON, nullable=True, default=None)
    created_at = Column(DateTime(timezone=True), default=datetime.now)
    updated_at = Column(DateTime(timezone=True), default=datetime.now, onupdate=datetime.now)

    def __repr__(self) -> str:
        return f"Project({self.project_id})"


class RepositoryConnectorModel(Base):
    """项目 Repository Connector（1:N）：一个项目可关联多个 Git 仓库（微服务链）。"""
    __tablename__ = "viktor_repository_connectors"

    project_id = Column(String(128), primary_key=True)
    connector_id = Column(String(128), primary_key=True)
    display_name = Column(String(255), nullable=False, default="")
    # 仓库职责描述：供 issue 自动路由器判断「什么需求该进这个仓库」。
    description = Column(Text, nullable=False, default="", server_default="")
    git_url = Column(String(512), nullable=False)
    default_branch = Column(String(128), nullable=False, default="master")
    k8s_workload = Column(JSON, nullable=True, default=None)
    sort_order = Column(Integer, nullable=False, default=0)
    # 是否为该仓库建 venv 并安装依赖（warmup 预热里最慢的一步）。
    # 1=加载依赖（如可跑脚本的主代码仓库），0=只 clone 不建 venv（无需跑脚本的 worker 仓库）。
    build_venv = Column(SmallInteger, nullable=False, default=1, server_default="1")
    # 多语言测试流程（B 层）：语言 + 用户覆盖的测试/lint 命令。空串走内置默认。
    language = Column(String(32), nullable=False, default="", server_default="")
    test_command = Column(String(512), nullable=False, default="", server_default="")
    lint_command = Column(String(512), nullable=False, default="", server_default="")
    # 该仓库固定维护开发的钉钉手机号：MR 创建后通知里 @ 此人（每仓配一次复用）。
    maintainer_mobile = Column(String(32), nullable=False, default="", server_default="")
    created_at = Column(DateTime(timezone=True), default=datetime.now)
    updated_at = Column(DateTime(timezone=True), default=datetime.now, onupdate=datetime.now)

    def __repr__(self) -> str:
        return f"RepositoryConnector({self.project_id}/{self.connector_id})"


class ContextModel(Base):
    __tablename__ = "viktor_contexts"

    project_id = Column(String(128), primary_key=True)
    context_id = Column(String(128), primary_key=True)
    priority = Column(Integer, nullable=False, default=0)
    content = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), default=datetime.now)
    updated_at = Column(DateTime(timezone=True), default=datetime.now, onupdate=datetime.now)

    def __repr__(self) -> str:
        return f"Context({self.project_id}/{self.context_id})"


class DatabaseConnectorModel(Base):
    __tablename__ = "viktor_database_connectors"

    project_id = Column(String(128), primary_key=True)
    connector_id = Column(String(128), primary_key=True)
    type = Column(String(32), nullable=False, default="mysql")
    host = Column(String(255), nullable=False)
    port = Column(Integer, nullable=False, default=3306)
    username = Column(String(255), nullable=False)
    password = Column(String(512), nullable=False)
    database_name = Column(String(255), nullable=False)
    readonly_flag = Column(SmallInteger, nullable=False, default=1)
    charset_name = Column(String(64), nullable=False, default="utf8mb4")
    # 数据库连接器级 SSH 隧道配置：NULL 表示直连；JSON 表示开启隧道，结构参见 SSHTunnelSpec
    ssh_tunnel = Column(JSON, nullable=True, default=None)
    created_at = Column(DateTime(timezone=True), default=datetime.now)
    updated_at = Column(DateTime(timezone=True), default=datetime.now, onupdate=datetime.now)

    def __repr__(self) -> str:
        return f"DatabaseConnector({self.project_id}/{self.connector_id})"


class LogConnectorModel(Base):
    """项目级 Log Connector：存储用户指定的 SLS project/logstore。"""
    __tablename__ = "viktor_log_connectors"

    project_id = Column(String(128), primary_key=True)
    connector_id = Column(String(128), primary_key=True)
    display_name = Column(String(255), nullable=False, default="")
    sls_project = Column(String(128), nullable=False)
    logstore = Column(String(128), nullable=False)
    description = Column(String(2048), nullable=False, default="")
    enabled = Column(SmallInteger, nullable=False, default=1)
    created_at = Column(DateTime(timezone=True), default=datetime.now)
    updated_at = Column(DateTime(timezone=True), default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        Index("ix_log_connector_project_enabled", "project_id", "enabled"),
    )

    def __repr__(self) -> str:
        return f"LogConnector({self.project_id}/{self.connector_id} -> {self.sls_project}/{self.logstore})"


class ExternalConnectorModel(Base):
    """项目级外部证据连接器：Redis / OSS / Queue / Vector Store / HTTP Service 等。"""
    __tablename__ = "viktor_external_connectors"

    project_id = Column(String(128), primary_key=True)
    connector_id = Column(String(128), primary_key=True)
    connector_type = Column(String(32), nullable=False, index=True)
    display_name = Column(String(255), nullable=False, default="")
    description = Column(String(2048), nullable=False, default="")
    config = Column(JSON, nullable=False, default=dict)
    secrets = Column(JSON, nullable=False, default=dict)
    enabled = Column(SmallInteger, nullable=False, default=1)
    created_at = Column(DateTime(timezone=True), default=datetime.now)
    updated_at = Column(DateTime(timezone=True), default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        Index("ix_external_connector_project_type_enabled", "project_id", "connector_type", "enabled"),
    )

    def __repr__(self) -> str:
        return f"ExternalConnector({self.project_id}/{self.connector_id} {self.connector_type})"


class RuntimeContextModel(Base):
    """项目级运行时上下文：描述线上 workload、集群、入口命令、日志绑定和暴露入口。"""
    __tablename__ = "viktor_runtime_contexts"

    project_id = Column(String(128), primary_key=True)
    runtime_id = Column(String(128), primary_key=True)
    environment = Column(String(64), nullable=False, default="prod")
    source_type = Column(String(64), nullable=False, default="kubevela")
    source_repo = Column(String(512), nullable=False, default="")
    source_path = Column(String(1024), nullable=False, default="")
    app_name = Column(String(255), nullable=False, default="")
    namespace = Column(String(255), nullable=False, default="")
    workload_type = Column(String(64), nullable=False, default="Deployment")
    workload_name = Column(String(255), nullable=False, default="")
    service_name = Column(String(255), nullable=False, default="")
    clusters = Column(JSON, nullable=False, default=list)
    selector = Column(JSON, nullable=False, default=dict)
    labels = Column(JSON, nullable=False, default=dict)
    replicas = Column(Integer, nullable=True)
    image = Column(String(1024), nullable=False, default="")
    command = Column(JSON, nullable=False, default=list)
    ports = Column(JSON, nullable=False, default=list)
    resources = Column(JSON, nullable=False, default=dict)
    probes = Column(JSON, nullable=False, default=dict)
    log_bindings = Column(JSON, nullable=False, default=list)
    exposures = Column(JSON, nullable=False, default=list)
    scheduling = Column(JSON, nullable=False, default=dict)
    config = Column(JSON, nullable=False, default=dict)
    enabled = Column(SmallInteger, nullable=False, default=1)
    created_at = Column(DateTime(timezone=True), default=datetime.now)
    updated_at = Column(DateTime(timezone=True), default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        Index("ix_runtime_context_project_env_enabled", "project_id", "environment", "enabled"),
        Index("ix_runtime_context_project_workload", "project_id", "workload_name"),
    )

    def __repr__(self) -> str:
        return f"RuntimeContext({self.project_id}/{self.runtime_id} {self.workload_name})"


class SkillModel(Base):
    """项目级 Skill：把 owner 的经验沉淀为可检索、可编排的执行方法。"""
    __tablename__ = "viktor_skills"

    project_id = Column(String(128), primary_key=True)
    skill_id = Column(String(128), primary_key=True)
    name = Column(String(255), nullable=False, default="")
    kind = Column(String(32), nullable=False, default="business")
    description = Column(String(2048), nullable=False, default="")
    trigger_examples = Column(JSON, nullable=False, default=list)
    input_contract = Column(JSON, nullable=False, default=dict)
    required_contexts = Column(JSON, nullable=False, default=list)
    required_tools = Column(JSON, nullable=False, default=list)
    related_glossary_terms = Column(JSON, nullable=False, default=list)
    instructions = Column(JSON, nullable=False, default=list)
    output_contract = Column(JSON, nullable=False, default=dict)
    safety_policy = Column(JSON, nullable=False, default=dict)
    source_type = Column(String(64), nullable=False, default="manual")
    source_uri = Column(String(1024), nullable=False, default="")
    raw_content = Column(Text, nullable=False, default="")
    status = Column(String(32), nullable=False, default="enabled")
    version = Column(Integer, nullable=False, default=1)
    scope = Column(String(16), nullable=False, default="project")  # project / global
    created_at = Column(DateTime(timezone=True), default=datetime.now)
    updated_at = Column(DateTime(timezone=True), default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        Index("ix_skill_project_kind_status", "project_id", "kind", "status"),
        Index("ix_skill_project_name", "project_id", "name"),
    )

    def __repr__(self) -> str:
        return f"Skill({self.project_id}/{self.skill_id} {self.name!r})"


class GroupBindingModel(Base):
    __tablename__ = "viktor_group_bindings"

    conversation_id = Column(String(256), primary_key=True)
    project_id = Column(String(128), nullable=False)
    group_name = Column(String(255), nullable=False, default="")
    created_at = Column(DateTime(timezone=True), default=datetime.now)
    updated_at = Column(DateTime(timezone=True), default=datetime.now, onupdate=datetime.now)

    def __repr__(self) -> str:
        return f"GroupBinding({self.conversation_id} [{self.group_name!r}] -> {self.project_id})"


class GitLabTaskModel(Base):
    __tablename__ = "viktor_gitlab_tasks"

    task_id = Column(String(64), primary_key=True)
    project_id = Column(String(128), nullable=False)
    repo_url = Column(String(512), nullable=False)
    branch = Column(String(128), nullable=False, default="master")
    status = Column(String(32), nullable=False, default="pending")
    message = Column(Text, nullable=False, default="")
    contexts_generated = Column(JSON, nullable=False, default=list)
    created_at = Column(DateTime(timezone=True), default=datetime.now)
    updated_at = Column(DateTime(timezone=True), default=datetime.now, onupdate=datetime.now)

    def __repr__(self) -> str:
        return f"GitLabTask({self.task_id} [{self.status}] -> {self.project_id})"


class IssueIntakeConfigModel(Base):
    """项目级 GitLab Issue Intake 配置。"""

    __tablename__ = "viktor_issue_intake_configs"

    project_id = Column(String(128), primary_key=True)
    issue_project_url = Column(String(512), nullable=False, default="")
    default_repo_connector_id = Column(String(128), nullable=False, default="")
    default_labels = Column(JSON, nullable=False, default=list)
    submit_token = Column(String(128), nullable=False, default="")
    notification = Column(JSON, nullable=False, default=dict)
    assignee_mobiles = Column(JSON, nullable=False, default=dict)
    scan_interval_sec = Column(Integer, nullable=False, default=300)
    enabled = Column(SmallInteger, nullable=False, default=1)
    created_at = Column(DateTime(timezone=True), default=datetime.now)
    updated_at = Column(DateTime(timezone=True), default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        Index("ix_issue_intake_config_enabled", "enabled"),
    )

    def __repr__(self) -> str:
        return f"IssueIntakeConfig({self.project_id})"


class IssueIntakeTargetModel(Base):
    """项目内某个 Repository Connector 对应的 GitLab issue 扫描目标。"""

    __tablename__ = "viktor_issue_intake_targets"

    project_id = Column(String(128), primary_key=True)
    repo_connector_id = Column(String(128), primary_key=True)
    issue_project_url = Column(String(512), nullable=False, default="")
    labels = Column(JSON, nullable=False, default=list)
    enabled = Column(SmallInteger, nullable=False, default=1)
    created_at = Column(DateTime(timezone=True), default=datetime.now)
    updated_at = Column(DateTime(timezone=True), default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        Index("ix_issue_intake_target_enabled", "project_id", "enabled"),
    )

    def __repr__(self) -> str:
        return f"IssueIntakeTarget({self.project_id}/{self.repo_connector_id})"


class IssueIntakeLinkModel(Base):
    """GitLab issue 与 Viktor Coding Task/MR 的闭环映射。"""

    __tablename__ = "viktor_issue_intake_links"

    link_id = Column(String(64), primary_key=True)
    project_id = Column(String(128), nullable=False, index=True)
    repo_connector_id = Column(String(128), nullable=False, default="")
    source = Column(String(32), nullable=False, default="scan")
    kind = Column(String(32), nullable=False, default="bug")
    reporter = Column(String(255), nullable=False, default="")
    title = Column(String(512), nullable=False, default="")
    description = Column(Text, nullable=False, default="")
    status = Column(String(32), nullable=False, default="created", index=True)
    stage = Column(String(64), nullable=False, default="created")
    message = Column(Text, nullable=False, default="")
    gitlab_base_url = Column(String(255), nullable=False, default="")
    gitlab_project_path = Column(String(512), nullable=False, default="")
    gitlab_project_id = Column(String(64), nullable=False, default="")
    issue_id = Column(String(64), nullable=False, default="")
    issue_iid = Column(String(64), nullable=False, default="")
    issue_url = Column(String(1024), nullable=False, default="")
    issue_state = Column(String(32), nullable=False, default="")
    issue_labels = Column(JSON, nullable=False, default=list)
    issue_payload = Column(JSON, nullable=False, default=dict)
    assignees = Column(JSON, nullable=False, default=list)
    coding_task_id = Column(String(64), nullable=False, default="", index=True)
    mr_url = Column(String(1024), nullable=False, default="")
    report_id = Column(String(16), nullable=False, default="")
    dedupe_key = Column(String(255), nullable=False, default="", index=True)
    last_error = Column(Text, nullable=False, default="")
    result = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), default=datetime.now, index=True)
    updated_at = Column(DateTime(timezone=True), default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        Index("ix_issue_intake_project_status", "project_id", "status"),
        # gitlab_project_path 用前缀索引：三列全长在 utf8mb4 下超过 InnoDB 3072 字节上限。
        Index(
            "ux_issue_intake_gitlab_issue",
            "gitlab_base_url", "gitlab_project_path", "issue_iid",
            unique=True,
            mysql_length={"gitlab_project_path": 400},
        ),
    )

    def __repr__(self) -> str:
        return f"IssueIntakeLink({self.link_id} {self.status})"


class IssueIntakeEventModel(Base):
    """Issue Intake 事件流：供前端观察与审计使用。"""

    __tablename__ = "viktor_issue_intake_events"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    link_id = Column(String(64), nullable=False, index=True)
    seq = Column(Integer, nullable=False, default=0)
    event_type = Column(String(64), nullable=False, index=True)
    stage = Column(String(64), nullable=False, default="")
    message = Column(Text, nullable=False, default="")
    payload = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), default=datetime.now, index=True)

    __table_args__ = (
        Index("ix_issue_intake_event_link_seq", "link_id", "seq"),
    )

    def __repr__(self) -> str:
        return f"IssueIntakeEvent({self.link_id}#{self.seq} {self.event_type})"


class ChatMessageModel(Base):
    """多轮对话记忆。

    - thread_id：钉钉侧会话键 conversation_id:sender_staff_id（下称 session_id）。
    - topic_thread_id：同一 session 下的**议题段**（/clear 新开一段；旧段保留）。
    - turn_id：议题内一轮问答（含 tool 消息）。

    role 取值：human | ai | tool | system_note（后者仅用于 /clear 切话题锚点，不参与 Agent 历史）。
    """

    __tablename__ = "viktor_chat_messages"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    thread_id = Column(String(256), nullable=False, index=True)
    topic_thread_id = Column(String(128), nullable=False, index=True)
    project_id = Column(String(128), nullable=False, index=True)
    turn_id = Column(String(64), nullable=False, index=True)
    role = Column(String(16), nullable=False)
    content = Column(Text, nullable=False, default="")
    tool_calls = Column(JSON, nullable=True)
    reasoning_content = Column(Text, nullable=True)
    tool_call_id = Column(String(128), nullable=True)
    tool_name = Column(String(128), nullable=True)
    truncated = Column(SmallInteger, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), default=datetime.now, index=True)

    __table_args__ = (
        Index("ix_chat_thread_created", "thread_id", "created_at"),
        Index("ix_chat_session_topic", "thread_id", "topic_thread_id", "created_at"),
    )

    def __repr__(self) -> str:
        return f"ChatMessage({self.thread_id}/{self.topic_thread_id}/{self.turn_id} {self.role})"


class LLMCallModel(Base):
    """LLM 调用观测记录：用于看板统计首字、TPS、429 与 fallback。"""

    __tablename__ = "viktor_llm_calls"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    request_id = Column(String(64), nullable=False, index=True)
    feature = Column(String(64), nullable=False, default="unknown", index=True)
    # 使用场景：coding（Coding Agent）/ webchat / dingtalk / system（系统后台），用于看板按场景拆分 token 用量
    scene = Column(String(32), nullable=False, default="system", server_default="system", index=True)
    provider = Column(String(128), nullable=False, index=True)
    model = Column(String(255), nullable=False, default="")
    attempt_index = Column(Integer, nullable=False, default=1)
    fallback_from = Column(String(128), nullable=True)
    status = Column(String(32), nullable=False, default="success", index=True)
    streaming = Column(SmallInteger, nullable=False, default=0)
    started_at = Column(DateTime(timezone=True), default=datetime.now, index=True)
    first_token_ms = Column(Float, nullable=True)
    duration_ms = Column(Float, nullable=True)
    prompt_tokens = Column(Integer, nullable=True)
    completion_tokens = Column(Integer, nullable=True)
    total_tokens = Column(Integer, nullable=True)
    cache_hit_tokens = Column(Integer, nullable=True)
    cache_miss_tokens = Column(Integer, nullable=True)
    reasoning_tokens = Column(Integer, nullable=True)
    output_tokens = Column(Integer, nullable=True)
    output_chars = Column(Integer, nullable=False, default=0)
    tokens_per_second = Column(Float, nullable=True)
    error_type = Column(String(255), nullable=False, default="")
    error_message = Column(Text, nullable=False, default="")
    meta = Column(JSON, nullable=False, default=dict)

    __table_args__ = (
        Index("ix_llm_calls_started_provider", "started_at", "provider"),
        Index("ix_llm_calls_feature_status", "feature", "status"),
    )

    def __repr__(self) -> str:
        return f"LLMCall({self.provider}/{self.model} {self.status})"


class AgentTraceEventModel(Base):
    """Agent 运行审计事件：记录意图检索、路由、LLM、工具与最终答复。"""

    __tablename__ = "viktor_agent_trace_events"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    trace_id = Column(String(64), nullable=False, index=True)
    event_seq = Column(Integer, nullable=False, default=0)
    event_type = Column(String(64), nullable=False, index=True)
    project_id = Column(String(128), nullable=False, default="", index=True)
    session_id = Column(String(256), nullable=False, default="", index=True)
    topic_thread_id = Column(String(128), nullable=False, default="", index=True)
    payload = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), default=datetime.now, index=True)

    __table_args__ = (
        Index("ix_agent_trace_trace_seq", "trace_id", "event_seq"),
        Index("ix_agent_trace_project_created", "project_id", "created_at"),
        Index("ix_agent_trace_session_topic", "session_id", "topic_thread_id", "created_at"),
    )

    def __repr__(self) -> str:
        return f"AgentTraceEvent({self.trace_id}#{self.event_seq} {self.event_type})"


class TraceEvaluationModel(Base):
    """Ragas shadow evaluation result for one completed Agent trace."""

    __tablename__ = "viktor_trace_evaluations"

    evaluation_id = Column(String(64), primary_key=True)
    trace_id = Column(String(64), nullable=False, index=True)
    project_id = Column(String(128), nullable=False, default="", index=True)
    status = Column(String(32), nullable=False, default="queued", index=True)
    sample_type = Column(String(32), nullable=False, default="single_turn", index=True)
    evaluator_version = Column(String(64), nullable=False, default="")
    metrics = Column(JSON, nullable=False, default=list)
    scores = Column(JSON, nullable=False, default=dict)
    sample_preview = Column(JSON, nullable=False, default=dict)
    diagnostics = Column(JSON, nullable=False, default=dict)
    error = Column(Text, nullable=False, default="")
    created_at = Column(DateTime(timezone=True), default=datetime.now, index=True)
    updated_at = Column(DateTime(timezone=True), default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        Index("ix_trace_eval_trace_status", "trace_id", "status"),
        Index("ix_trace_eval_project_created", "project_id", "created_at"),
        Index("ix_trace_eval_status_created", "status", "created_at"),
    )

    def __repr__(self) -> str:
        return f"TraceEvaluation({self.evaluation_id} {self.status})"


class AgentCheckpointModel(Base):
    """Agent 选择性 checkpoint：澄清门通过后保存，支持 /resume 恢复执行。"""

    __tablename__ = "viktor_agent_checkpoints"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    session_id = Column(String(256), nullable=False)
    topic_thread_id = Column(String(128), nullable=False)
    project_id = Column(String(128), nullable=False)
    user_message = Column(Text, nullable=False)
    system_prompt = Column(Text, nullable=False)
    intent_route_json = Column(JSON, nullable=True)
    retrieval_context = Column(Text, nullable=False, default="")
    llm_feature = Column(String(64), nullable=False, default="sse_agent")
    provider_order_json = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), default=datetime.now)
    expires_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        Index("ix_checkpoint_session_topic", "session_id", "topic_thread_id"),
    )

    def __repr__(self) -> str:
        return f"AgentCheckpoint({self.session_id}/{self.topic_thread_id} {self.project_id})"


class LearningCandidateModel(Base):
    """Trace 自动复盘生成的长期知识候选，默认 pending，人工审核后才写入正式 Registry。"""

    __tablename__ = "viktor_learning_candidates"

    candidate_id = Column(String(64), primary_key=True)
    project_id = Column(String(128), nullable=False, index=True)
    source_trace_id = Column(String(64), nullable=False, index=True)
    target_type = Column(String(32), nullable=False, index=True)
    target_id = Column(String(128), nullable=False, default="", index=True)
    title = Column(String(512), nullable=False, default="")
    content = Column(Text, nullable=False, default="")
    payload = Column(JSON, nullable=False, default=dict)
    confidence = Column(Float, nullable=False, default=0.0)
    risk_level = Column(String(16), nullable=False, default="medium", index=True)
    status = Column(String(32), nullable=False, default="pending", index=True)
    created_at = Column(DateTime(timezone=True), default=datetime.now, index=True)
    updated_at = Column(DateTime(timezone=True), default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        Index("ix_learning_project_status", "project_id", "status"),
        Index("ix_learning_trace_type", "source_trace_id", "target_type"),
        Index("ix_learning_project_target", "project_id", "target_type", "target_id"),
    )

    def __repr__(self) -> str:
        return f"LearningCandidate({self.candidate_id} {self.target_type} {self.status})"


class GlossaryModel(Base):
    """业务术语表：中文业务词 ↔ 代码关键词的映射，供 LLM 搜索代码时做 query 扩展。"""

    __tablename__ = "viktor_glossaries"

    project_id = Column(String(128), primary_key=True)
    glossary_id = Column(String(128), primary_key=True)
    term = Column(String(255), nullable=False)
    aliases = Column(JSON, nullable=False, default=list)
    code_keywords = Column(JSON, nullable=False, default=list)
    description = Column(String(2048), nullable=False, default="")
    enabled = Column(SmallInteger, nullable=False, default=1)
    created_at = Column(DateTime(timezone=True), default=datetime.now)
    updated_at = Column(DateTime(timezone=True), default=datetime.now, onupdate=datetime.now)

    def __repr__(self) -> str:
        return f"Glossary({self.project_id}/{self.glossary_id} {self.term!r})"


class KnowledgeNoteModel(Base):
    """业务知识笔记：沉淀字段级约定 / 字段语义 / 易错点 / 指标定义。

    与 Glossary 分工：Glossary 是词法映射，本表是「写 SQL 前必须知道的业务潜规则」，
    按 kind 分节注入到 system prompt，为 LLM 提供查询推理所需的潜规则。
    """

    __tablename__ = "viktor_knowledge_notes"

    project_id = Column(String(128), primary_key=True)
    note_id = Column(String(128), primary_key=True)
    # kind 枚举：schema_convention / field_semantics / pitfall / metric_definition
    kind = Column(String(32), nullable=False, index=True)
    scope = Column(String(255), nullable=False, default="")    # 自由文本，如 vt-db.video.hide_flag
    title = Column(String(512), nullable=False)                # 一行摘要
    content = Column(Text, nullable=False, default="")         # 详细规则/反例（markdown）
    tags = Column(JSON, nullable=False, default=list)
    enabled = Column(SmallInteger, nullable=False, default=1)
    source = Column(String(16), nullable=False, default="api")  # admin / api / import
    created_at = Column(DateTime(timezone=True), default=datetime.now)
    updated_at = Column(DateTime(timezone=True), default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        Index("ix_knowledge_project_kind", "project_id", "kind"),
    )

    def __repr__(self) -> str:
        return f"KnowledgeNote({self.project_id}/{self.note_id} {self.kind} {self.title!r})"


class ReportModel(Base):
    """超长 Agent 回复的 HTML 报告：钉钉里仅给简述 + 链接，完整内容在此。

    渲染策略：入库时把 markdown 一次性转为 HTML 片段存 html_body，访问时直接套外壳模板返回。
    过期清理：启动时按 expires_at < now 删除。
    """

    __tablename__ = "viktor_reports"

    id = Column(String(16), primary_key=True)        # 短 hash，URL 友好
    thread_id = Column(String(256), nullable=False, index=True)
    project_id = Column(String(128), nullable=False, index=True)
    title = Column(String(255), nullable=False, default="")
    summary = Column(Text, nullable=False, default="")  # 钉钉里展示的简述
    html_body = Column(Text().with_variant(MEDIUMTEXT, "mysql"), nullable=False)  # 渲染好的 HTML 片段
    created_at = Column(DateTime(timezone=True), default=datetime.now, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)

    __table_args__ = (
        Index("ix_report_expires", "expires_at"),
    )

    def __repr__(self) -> str:
        return f"Report({self.id} {self.project_id})"


class OnboardingTaskModel(Base):
    """项目接入审核任务：仓库分析 → 候选知识审核 → 采纳落地正式项目。"""

    __tablename__ = "viktor_onboarding_tasks"

    task_id = Column(String(64), primary_key=True)
    project_id = Column(String(128), nullable=False, index=True)
    repo_url = Column(String(512), nullable=False)
    branch = Column(String(128), nullable=False, default="master")
    status = Column(String(32), nullable=False, default="created")
    stage = Column(String(64), nullable=False, default="created")
    message = Column(Text, nullable=False, default="")
    analysis_level = Column(String(32), nullable=False, default="standard")
    profile = Column(JSON, nullable=False, default=dict)
    stats = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), default=datetime.now, index=True)
    updated_at = Column(DateTime(timezone=True), default=datetime.now, onupdate=datetime.now)

    def __repr__(self) -> str:
        return f"OnboardingTask({self.task_id} [{self.status}] -> {self.project_id})"


class OnboardingArtifactModel(Base):
    """项目接入候选产物：等待用户采纳后才写入正式知识库。"""

    __tablename__ = "viktor_onboarding_artifacts"

    artifact_id = Column(String(64), primary_key=True)
    task_id = Column(String(64), nullable=False, index=True)
    project_id = Column(String(128), nullable=False, index=True)
    artifact_type = Column(String(32), nullable=False, index=True)  # context / glossary / knowledge_note / *_connector
    target_id = Column(String(128), nullable=False)
    title = Column(String(512), nullable=False, default="")
    content = Column(Text, nullable=False, default="")
    payload = Column(JSON, nullable=False, default=dict)
    status = Column(String(32), nullable=False, default="pending")  # pending / accepted / rejected / applied
    created_at = Column(DateTime(timezone=True), default=datetime.now, index=True)
    updated_at = Column(DateTime(timezone=True), default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        Index("ix_onboarding_artifact_task_status", "task_id", "status"),
    )

    def __repr__(self) -> str:
        return f"OnboardingArtifact({self.artifact_id} {self.artifact_type} {self.status})"


class CodingTaskModel(Base):
    """Coding Agent 后台任务：需求 -> 可写 workspace -> 校验 -> MR/报告。"""

    __tablename__ = "viktor_coding_tasks"

    task_id = Column(String(64), primary_key=True)
    project_id = Column(String(128), nullable=False, index=True)
    requirement = Column(Text, nullable=False, default="")
    status = Column(String(32), nullable=False, default="created")
    stage = Column(String(64), nullable=False, default="created")
    message = Column(Text, nullable=False, default="")
    repo_connector_id = Column(String(128), nullable=False, default="")
    target_branch = Column(String(128), nullable=False, default="")
    work_branch = Column(String(255), nullable=False, default="")
    mr_url = Column(String(1024), nullable=False, default="")
    report_id = Column(String(16), nullable=False, default="")
    policy = Column(JSON, nullable=False, default=dict)
    control = Column(JSON, nullable=False, default=dict)
    result = Column(JSON, nullable=False, default=dict)
    created_by = Column(String(255), nullable=False, default="")
    created_by_mobile = Column(String(32), nullable=False, default="", server_default="")
    # 当前人工 gate 的处理人。空手机号表示公共队列或暂未能定位到具体人。
    pending_gate = Column(String(64), nullable=False, default="", server_default="", index=True)
    pending_owner_mobile = Column(String(32), nullable=False, default="", server_default="", index=True)
    pending_owner_label = Column(String(128), nullable=False, default="", server_default="")
    created_at = Column(DateTime(timezone=True), default=datetime.now, index=True)
    updated_at = Column(DateTime(timezone=True), default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        Index("ix_coding_task_project_status", "project_id", "status"),
        Index("ix_coding_task_pending_owner", "pending_owner_mobile", "status"),
    )

    def __repr__(self) -> str:
        return f"CodingTask({self.task_id} [{self.status}] -> {self.project_id})"


class CodingAttemptModel(Base):
    """Coding Task 的一次执行尝试。review 后重做会产生新的 attempt。"""

    __tablename__ = "viktor_coding_attempts"

    attempt_id = Column(String(64), primary_key=True)
    task_id = Column(String(64), nullable=False, index=True)
    project_id = Column(String(128), nullable=False, index=True)
    repo_connector_id = Column(String(128), nullable=False, default="")
    status = Column(String(32), nullable=False, default="created")
    stage = Column(String(64), nullable=False, default="created")
    workspace_path = Column(String(1024), nullable=False, default="")
    branch_name = Column(String(255), nullable=False, default="")
    base_commit = Column(String(64), nullable=False, default="")
    head_commit = Column(String(64), nullable=False, default="")
    plan = Column(Text, nullable=False, default="")
    summary = Column(Text, nullable=False, default="")
    test_results = Column(JSON, nullable=False, default=dict)
    risk_flags = Column(JSON, nullable=False, default=list)
    created_at = Column(DateTime(timezone=True), default=datetime.now, index=True)
    updated_at = Column(DateTime(timezone=True), default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        Index("ix_coding_attempt_task_status", "task_id", "status"),
    )

    def __repr__(self) -> str:
        return f"CodingAttempt({self.attempt_id} [{self.status}] -> {self.task_id})"


class CodingEventModel(Base):
    """Coding Task 事件流：供前端观察窗口和审计使用。"""

    __tablename__ = "viktor_coding_events"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    task_id = Column(String(64), nullable=False, index=True)
    attempt_id = Column(String(64), nullable=False, default="", index=True)
    seq = Column(Integer, nullable=False, default=0)
    event_type = Column(String(64), nullable=False, index=True)
    stage = Column(String(64), nullable=False, default="")
    message = Column(Text, nullable=False, default="")
    payload = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), default=datetime.now, index=True)

    __table_args__ = (
        Index("ix_coding_event_task_seq", "task_id", "seq"),
    )

    def __repr__(self) -> str:
        return f"CodingEvent({self.task_id}#{self.seq} {self.event_type})"


class CodingArtifactModel(Base):
    """Coding Task 产物：计划、diff 摘要、报告草稿等结构化记录。"""

    __tablename__ = "viktor_coding_artifacts"

    artifact_id = Column(String(64), primary_key=True)
    task_id = Column(String(64), nullable=False, index=True)
    attempt_id = Column(String(64), nullable=False, default="", index=True)
    project_id = Column(String(128), nullable=False, index=True)
    artifact_type = Column(String(32), nullable=False, index=True)
    title = Column(String(512), nullable=False, default="")
    content = Column(Text, nullable=False, default="")
    payload = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), default=datetime.now, index=True)
    updated_at = Column(DateTime(timezone=True), default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        Index("ix_coding_artifact_task_type", "task_id", "artifact_type"),
    )

    def __repr__(self) -> str:
        return f"CodingArtifact({self.artifact_id} {self.artifact_type})"


class StagingRunModel(Base):
    """单测试环境 staging 验收批次。"""

    __tablename__ = "viktor_staging_runs"

    run_id = Column(String(64), primary_key=True)
    env_id = Column(String(128), nullable=False, default="default-staging", index=True)
    link_id = Column(String(64), nullable=False, default="", index=True)
    project_id = Column(String(128), nullable=False, default="", index=True)
    status = Column(String(32), nullable=False, default="queued", index=True)
    stage = Column(String(64), nullable=False, default="queued")
    message = Column(Text, nullable=False, default="")
    commit_fingerprint = Column(String(128), nullable=False, default="", index=True)
    dev_base_sha = Column(String(64), nullable=False, default="")
    dev_deploy_sha = Column(String(64), nullable=False, default="")
    candidate_shas = Column(JSON, nullable=False, default=dict)
    task_ids = Column(JSON, nullable=False, default=list)
    mr_urls = Column(JSON, nullable=False, default=list)
    branches = Column(JSON, nullable=False, default=dict)
    test_plan = Column(JSON, nullable=False, default=dict)
    test_result = Column(JSON, nullable=False, default=dict)
    deploy_payload = Column(JSON, nullable=False, default=dict)
    feedback_issue_url = Column(String(1024), nullable=False, default="")
    report_url = Column(String(1024), nullable=False, default="")
    last_error = Column(Text, nullable=False, default="")
    retry_count = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), default=datetime.now, index=True)
    updated_at = Column(DateTime(timezone=True), default=datetime.now, onupdate=datetime.now)
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_staging_run_env_status", "env_id", "status"),
        Index("ix_staging_run_link_status", "link_id", "status"),
    )

    def __repr__(self) -> str:
        return f"StagingRun({self.run_id} [{self.status}] -> {self.link_id})"


class StagingLockModel(Base):
    """单测试环境互斥锁。"""

    __tablename__ = "viktor_staging_locks"

    env_id = Column(String(128), primary_key=True)
    run_id = Column(String(64), nullable=False, default="", index=True)
    lease_owner = Column(String(128), nullable=False, default="")
    status = Column(String(32), nullable=False, default="locked")
    heartbeat_at = Column(DateTime(timezone=True), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), default=datetime.now)
    updated_at = Column(DateTime(timezone=True), default=datetime.now, onupdate=datetime.now)

    def __repr__(self) -> str:
        return f"StagingLock({self.env_id} -> {self.run_id})"


class StagingEventModel(Base):
    """Staging run 事件流。"""

    __tablename__ = "viktor_staging_events"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    run_id = Column(String(64), nullable=False, index=True)
    seq = Column(Integer, nullable=False, default=0)
    event_type = Column(String(64), nullable=False, index=True)
    stage = Column(String(64), nullable=False, default="")
    message = Column(Text, nullable=False, default="")
    payload = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), default=datetime.now, index=True)

    __table_args__ = (
        Index("ix_staging_event_run_seq", "run_id", "seq"),
    )

    def __repr__(self) -> str:
        return f"StagingEvent({self.run_id}#{self.seq} {self.event_type})"


class WatchdogModel(Base):
    """Watchdog 注册项持久化：探针定义 + 调度 + 分析 Skill + 通知目标。"""
    __tablename__ = "viktor_watchdogs"

    project_id = Column(String(128), primary_key=True)
    watchdog_id = Column(String(128), primary_key=True)
    name = Column(String(255), nullable=False, default="")
    description = Column(String(2048), nullable=False, default="")
    probe = Column(JSON, nullable=False)
    schedule = Column(String(128), nullable=False)
    skill_ids = Column(JSON, nullable=False, default=list)
    notification = Column(JSON, nullable=False)
    severity_filter = Column(JSON, nullable=False, default=list)
    auto_coding_plan = Column(SmallInteger, nullable=False, default=0)
    coding_repo_connector_id = Column(String(128), nullable=False, default="")
    cooldown_minutes = Column(Integer, nullable=False, default=30)
    max_execution_sec = Column(Integer, nullable=False, default=300)
    enabled = Column(SmallInteger, nullable=False, default=1)
    created_at = Column(DateTime(timezone=True), default=datetime.now)
    updated_at = Column(DateTime(timezone=True), default=datetime.now, onupdate=datetime.now)

    __table_args__ = (
        Index("ix_watchdog_project_enabled", "project_id", "enabled"),
    )

    def __repr__(self) -> str:
        return f"Watchdog({self.project_id}/{self.watchdog_id} {self.name!r})"


class WatchdogEventModel(Base):
    """Watchdog 执行记录：每次探针触发产生一条，含探针结果、AI 分析、通知状态。"""
    __tablename__ = "viktor_watchdog_events"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    watchdog_id = Column(String(128), nullable=False, index=True)
    project_id = Column(String(128), nullable=False, index=True)
    status = Column(String(32), nullable=False, default="started")
    # status 枚举：started / probing / analyzing / plan_waiting / notifying / completed / failed
    probe_result = Column(JSON, nullable=False, default=dict)
    is_anomaly = Column(SmallInteger, nullable=False, default=0)
    severity = Column(String(32), nullable=False, default="")
    conclusion = Column(Text, nullable=False, default="")
    evidence = Column(JSON, nullable=False, default=list)
    action_type = Column(String(32), nullable=False, default="none")
    coding_task_id = Column(String(64), nullable=False, default="")
    notification_sent = Column(SmallInteger, nullable=False, default=0)
    notification_error = Column(Text, nullable=False, default="")
    duration_ms = Column(Float, nullable=True)
    started_at = Column(DateTime(timezone=True), default=datetime.now)
    completed_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_watchdog_event_watchdog_started", "watchdog_id", "started_at"),
        Index("ix_watchdog_event_project_started", "project_id", "started_at"),
    )

    def __repr__(self) -> str:
        return f"WatchdogEvent({self.watchdog_id} [{self.status}])"


class UserModel(Base):
    """网页控制台用户（手机号为长期身份凭证）。

    role 为旧兼容字段，当前仍决定 Agent 应答时暴露多少代码/工具细节与语气。
    钉钉融合后，真实用户画像由手机号、钉钉 userid 与部门路径共同决定。
    """
    __tablename__ = "viktor_users"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    username = Column(String(64), nullable=False, unique=True, index=True)
    password_hash = Column(String(128), nullable=False)
    password_set = Column(SmallInteger, nullable=False, default=1, server_default="1")
    role = Column(String(16), nullable=False, default="operations")
    display_name = Column(String(64), nullable=False, default="")
    mobile = Column(String(32), nullable=False, default="", server_default="", unique=True, index=True)
    dingtalk_userid = Column(String(128), nullable=False, default="", server_default="", index=True)
    department_paths = Column(JSON, nullable=True, default=list)
    primary_department = Column(String(512), nullable=False, default="", server_default="")
    profile_key = Column(String(32), nullable=False, default="", server_default="")
    auth_source = Column(String(32), nullable=False, default="local", server_default="local")
    is_active = Column(SmallInteger, nullable=False, default=1)
    created_at = Column(DateTime(timezone=True), default=datetime.now)
    updated_at = Column(DateTime(timezone=True), default=datetime.now, onupdate=datetime.now)

    def __repr__(self) -> str:
        return f"User({self.username} [{self.role}])"


class NotificationDLQModel(Base):
    """失败的钉钉通知死信队列。"""
    __tablename__ = "viktor_notification_dlq"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    target = Column(String(1024), nullable=False, default="")      # webhook_url
    payload = Column(JSON, nullable=False, default=dict)           # 钉钉 markdown payload(title/text/sign_secret/at_mobiles/at_all/timeout)
    last_error = Column(Text, nullable=False, default="")
    retry_count = Column(Integer, nullable=False, default=0)
    status = Column(String(32), nullable=False, default="pending")  # pending/sent/dead
    created_at = Column(DateTime(timezone=True), default=datetime.now, index=True)
    updated_at = Column(DateTime(timezone=True), default=datetime.now, onupdate=datetime.now)

    __table_args__ = (Index("ix_notification_dlq_status_created", "status", "created_at"),)

    def __repr__(self) -> str:
        return f"NotificationDLQ({self.id} [{self.status}] retry={self.retry_count})"
