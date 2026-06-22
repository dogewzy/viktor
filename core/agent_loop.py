"""
Agent 主循环：基于 LangGraph ReAct Agent。

将数据库探索、代码自省、K8s 等内置能力转换为 LangChain StructuredTool，
通过 Viktor ToolExecutionManager 编排多轮 tool calling（SQL 由模型在 describe 后自行撰写）。
"""
import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Optional

from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.tools import StructuredTool
from loguru import logger
from pydantic import create_model, Field

from core.chat_commands import normalize_user_command_text
from core.clarification_gate import (
    format_clarification_text,
    format_term_mappings_for_prompt,
    run_clarification_gate,
)
from core.context_compaction import compact_messages
from core.audit.recorder import record_trace_event
from core.file_service import (
    create_downloadable_file,
    format_attachments_for_prompt,
    is_export_request,
    tool_result_markdown,
)
from core.intent import IntentRoute, prepare_intent_context
from core.llm_client import create_llm
from core.llm_metrics import llm_observation_context
from core.memory import (
    clear_agent_checkpoint,
    cleanup_interrupted_messages,
    load_agent_checkpoint,
    load_history,
    save_agent_checkpoint,
    save_compaction_summary,
    save_turn,
)
from core.prompt_builder import build_system_prompt
from core.registry import registry
from core.explorer_agent import run_explorer
from core.tool_execution_manager import ToolExecutionManager, ToolJob, ToolJobResult
from settings import agent_config, code_inspection_config, context_compaction_config, llm_config
from tools.code_inspector import code_glob, code_grep, code_read
from tools.repo_debug_runner import (
    repo_debug_runner_policy_summary,
    run_repo_command,
    run_repo_debug_script,
    write_repo_debug_file,
)
from tools.repo_venv import ensure_repo_venv, repo_venv_policy_summary
from settings import repo_debug_runner_config, repo_venv_config
from tools.k8s_tool import get_pod_logs, get_pod_status
from tools.log_connector import (
    list_log_connectors as log_list_log_connectors,
    query_logs as log_query_logs,
)
from tools.external_connectors import (
    dingtalk_doc_read,
    http_call,
    http_health_check,
    list_external_connectors as external_list_external_connectors,
    object_storage_head,
    object_storage_list,
    queue_overview,
    redis_exists,
    redis_get,
    vector_collection_info,
)
from tools.runtime_context import (
    discover_runtime_contexts as runtime_discover_runtime_contexts,
    get_runtime_context as runtime_get_runtime_context,
    list_runtime_contexts as runtime_list_runtime_contexts,
)
from tools.schema_inspector import (
    describe_table as schema_describe_table,
    list_tables as schema_list_tables,
    sample_rows as schema_sample_rows,
)
from tools.sql_executor import execute_free_sql, execute_probe_sql, explain_sql


_FILE_OUTPUT_INSTRUCTIONS = """

文件交付规则：
- 当用户要求“导出”、或提到 excel / word / xlsx / csv 等文件格式时，优先调用 create_downloadable_file 生成下载文件。
- 当回答包含长表格、长报告、CSV/JSON/Markdown 正文，或不适合直接塞进聊天气泡时，也应调用 create_downloadable_file。
- 生成文件后，面向用户的最终回复只给简要说明和下载链接，不要再完整粘贴大段文件内容。
"""


@dataclass
class AgentRunSetup:
    """一次 Agent turn 的共享运行上下文。"""

    user_message: str
    history: list[Any]
    messages: list[Any]
    trace_id: str = ""
    intent_route: IntentRoute | None = None
    retrieval_context: str = ""
    llm: Any | None = None
    llm_with_tools: Any | None = None
    tool_manager: ToolExecutionManager | None = None


@dataclass
class AgentPrepareResult:
    """准备阶段结果：要么可运行，要么有可直接返回/推送的文本。"""

    setup: AgentRunSetup | None = None
    error_text: str = ""
    clarification: dict[str, Any] | None = None


def _build_k8s_tools() -> list[StructuredTool]:
    """构建 K8s 内置工具列表。"""

    PodStatusArgs = create_model(
        "PodStatusArgs",
        app_label=(str, Field(description="Pod 的 app 标签值，如 order-worker")),
    )

    PodLogsArgs = create_model(
        "PodLogsArgs",
        app_label=(str, Field(description="Pod 的 app 标签值")),
        lines=(int, Field(default=100, description="获取最近多少行日志")),
        keyword=(
            Optional[str],
            Field(default=None, description="按关键字过滤日志行"),
        ),
    )

    return [
        StructuredTool.from_function(
            func=lambda app_label: get_pod_status(app_label),
            name="get_pod_status",
            description="查询 K8s 集群中指定应用的 Pod 运行状态、副本数、重启次数",
            args_schema=PodStatusArgs,
        ),
        StructuredTool.from_function(
            func=lambda app_label, lines=100, keyword=None: get_pod_logs(
                app_label, lines, keyword
            ),
            name="get_pod_logs",
            description="获取 K8s 集群中指定应用的 Pod 最近日志，可按关键字过滤",
            args_schema=PodLogsArgs,
        ),
    ]


def _build_db_explorer_tools(project_id: str) -> list[StructuredTool]:
    """构建通用数据库探索工具（所有项目共享）。

    含五个工具：
    - list_database_connectors  当前项目有哪些数据库连接器
    - list_tables       指定数据库连接器下的表列表 + 表注释
    - describe_table    某张表的字段/主键/索引
    - sample_rows       抽样返回前 N 行，N <= agent.sample_row_limit
    - explain_sql       对 SELECT 做 EXPLAIN 预估访问方式与扫描规模
    - probe_sql         探索性 SELECT，小心限制全表 count 探针
    - execute_sql       自由 SELECT（自动注入 LIMIT，受全局安全校验）

    这些工具不需要注册，每个项目启动时自动接入。
    """

    ListDatabaseConnectorsArgs = create_model(
        "ListDatabaseConnectorsArgs",
        placeholder=(
            Optional[str],
            Field(default=None, description="无需传参"),
        ),
    )
    ListTablesArgs = create_model(
        "ListTablesArgs",
        connector_id=(str, Field(description="目标数据库连接器 ID（先用 list_database_connectors 确认）")),
    )
    DescribeTableArgs = create_model(
        "DescribeTableArgs",
        connector_id=(str, Field(description="目标数据库连接器 ID")),
        table=(str, Field(description="表名（不带库名前缀）")),
    )
    SampleRowsArgs = create_model(
        "SampleRowsArgs",
        connector_id=(str, Field(description="目标数据库连接器 ID")),
        table=(str, Field(description="表名")),
        limit=(
            int,
            Field(default=5, description=f"抽样行数，上限 {agent_config.sample_row_limit}"),
        ),
    )
    ExecuteSqlArgs = create_model(
        "ExecuteSqlArgs",
        connector_id=(str, Field(description="目标数据库连接器 ID")),
        sql=(
            str,
            Field(
                description=(
                    "单条 SELECT 语句（允许 WITH ... SELECT）。禁止多语句与非查询操作。"
                    "若未写 LIMIT，系统自动注入默认上限。"
                )
            ),
        ),
    )
    ProbeSqlArgs = create_model(
        "ProbeSqlArgs",
        connector_id=(str, Field(description="目标数据库连接器 ID")),
        sql=(
            str,
            Field(
                description=(
                    "单条 SELECT 探针语句，用于摸索数据形态或验证过滤条件。"
                    "禁止把无过滤条件 COUNT(*) 当表大小探针。"
                )
            ),
        ),
    )

    def _list_database_connectors(placeholder: Optional[str] = None) -> str:  # noqa: ARG001
        items = []
        for ds in registry._database_connectors.get(project_id, {}).values():
            items.append(
                f"- {ds.id}  (type={ds.type}, database={ds.database}, "
                f"readonly={ds.readonly}, via_tunnel={ds.ssh_tunnel is not None})"
            )
        if not items:
            return f"项目 '{project_id}' 尚未注册任何数据库连接器"
        return "当前项目数据库连接器：\n" + "\n".join(items)

    return [
        StructuredTool.from_function(
            func=_list_database_connectors,
            name="list_database_connectors",
            description="列出当前项目已注册的数据库连接器 ID 与连接摘要。不确定查哪个库时先调这个。",
            args_schema=ListDatabaseConnectorsArgs,
        ),
        StructuredTool.from_function(
            func=lambda connector_id: schema_list_tables(project_id, connector_id),
            name="list_tables",
            description="列出指定数据库连接器的所有表名与表注释（走 information_schema，结果缓存）。",
            args_schema=ListTablesArgs,
        ),
        StructuredTool.from_function(
            func=lambda connector_id, table: schema_describe_table(project_id, connector_id, table),
            name="describe_table",
            description="查看指定表的字段定义、主键、索引、注释。写 SQL 前必查。",
            args_schema=DescribeTableArgs,
        ),
        StructuredTool.from_function(
            func=lambda connector_id, table, limit=5: schema_sample_rows(
                project_id, connector_id, table, limit
            ),
            name="sample_rows",
            description="从指定表抽样返回前 N 行，帮助判断数据形态与枚举值含义。",
            args_schema=SampleRowsArgs,
        ),
        StructuredTool.from_function(
            func=lambda connector_id, sql: execute_free_sql(project_id, connector_id, sql),
            name="execute_sql",
            description=(
                "执行一条自由 SELECT（只读）。在 describe_table 核实字段后编写；"
                "用于回答用户问题本身；如果用户明确要求精确总数，可以执行 COUNT(*)，但仍受 60 秒超时保护。"
                "遇到宽表、大表、LIKE '%...%' 或疑似全表扫描时，先用 explain_sql 预估。"
            ),
            args_schema=ExecuteSqlArgs,
        ),
        StructuredTool.from_function(
            func=lambda connector_id, sql: execute_probe_sql(project_id, connector_id, sql),
            name="probe_sql",
            description=(
                "执行一条探索性 SELECT 探针，用来摸索数据形态、验证过滤条件或查看少量样本。"
                "不要用它做无过滤 COUNT(*)；如果用户明确要精确总数，改用 execute_sql。"
            ),
            args_schema=ProbeSqlArgs,
        ),
        StructuredTool.from_function(
            func=lambda connector_id, sql: explain_sql(project_id, connector_id, sql),
            name="explain_sql",
            description=(
                "对一条 SELECT 先做 EXPLAIN，查看访问方式、预计扫描行数和是否全表扫描。"
                "在执行宽表查询、跨天统计、模糊匹配、COUNT(*) 前先调用它。"
            ),
            args_schema=ExecuteSqlArgs,
        ),
    ]


def _build_log_tools(project_id: str) -> list[StructuredTool]:
    ListLogConnectorsArgs = create_model(
        "ListLogConnectorsArgs",
        placeholder=(Optional[str], Field(default=None, description="无需传参")),
    )
    QueryLogsArgs = create_model(
        "QueryLogsArgs",
        connector_id=(str, Field(description="Log Connector ID")),
        query=(str, Field(description="SLS 查询语句，例：'143243265' 或 '* | where message like ...'")),
        minutes=(int, Field(default=30, description="查询最近 N 分钟")),
        limit=(int, Field(default=20, description="最多返回条数，最大 100")),
    )

    return [
        StructuredTool.from_function(
            func=lambda placeholder=None: log_list_log_connectors(project_id),
            name="list_log_connectors",
            description="列出当前项目已注册的日志连接器。不确定查哪个 logstore 时先调这个。",
            args_schema=ListLogConnectorsArgs,
        ),
        StructuredTool.from_function(
            func=lambda connector_id, query, minutes=30, limit=20: log_query_logs(
                project_id, connector_id, query, minutes=minutes, limit=limit
            ),
            name="query_logs",
            description="查询指定 Log Connector 的最近日志，用于按 sample_id、task_id、错误码、trace 关键字定位运行期证据。",
            args_schema=QueryLogsArgs,
        ),
    ]


def _build_external_connector_tools(project_id: str) -> list[StructuredTool]:
    ListExternalConnectorsArgs = create_model(
        "ListExternalConnectorsArgs",
        connector_type=(
            Optional[str],
            Field(default=None, description="可选：redis/object_storage/queue/vector_store/http_service/dingtalk_doc"),
        ),
    )
    RedisKeyArgs = create_model(
        "RedisKeyArgs",
        connector_id=(str, Field(description="Redis Connector ID")),
        key=(str, Field(description="Redis key")),
    )
    ObjectHeadArgs = create_model(
        "ObjectHeadArgs",
        connector_id=(str, Field(description="Object Storage Connector ID")),
        object_key=(str, Field(description="对象 key，不含 bucket，例：sample_feats/143243265.npy")),
    )
    ObjectListArgs = create_model(
        "ObjectListArgs",
        connector_id=(str, Field(description="Object Storage Connector ID")),
        prefix=(str, Field(description="对象前缀，例：sample_images/143243265_")),
        max_keys=(int, Field(default=50, description="最多返回对象数")),
    )
    QueueOverviewArgs = create_model(
        "QueueOverviewArgs",
        connector_id=(str, Field(description="Queue Connector ID")),
        name_filter=(str, Field(default="", description="按队列名包含文本过滤")),
    )
    VectorInfoArgs = create_model(
        "VectorInfoArgs",
        connector_id=(str, Field(description="Vector Store Connector ID")),
    )
    HttpHealthArgs = create_model(
        "HttpHealthArgs",
        connector_id=(str, Field(description="HTTP Service Connector ID")),
        path=(str, Field(default="", description="相对路径，留空访问 base_url")),
    )
    HttpCallArgs = create_model(
        "HttpCallArgs",
        connector_id=(str, Field(description="HTTP Service Connector ID")),
        method=(str, Field(default="GET", description="HTTP 方法，例如 GET/POST。必须在 connector config.allowed_methods 中启用")),
        path=(str, Field(description="相对路径，不允许完整 URL，例如 /v4/parser/parse")),
        query=(dict[str, Any], Field(default_factory=dict, description="Query 参数对象")),
        body=(Optional[Any], Field(default=None, description="JSON 请求体；GET 请求通常留空")),
        headers=(dict[str, Any], Field(default_factory=dict, description="额外请求头；敏感鉴权信息应放在 connector secrets 中")),
        max_chars=(int, Field(default=4000, description="最多返回响应正文字符数，范围 200-20000")),
    )
    DingtalkDocReadArgs = create_model(
        "DingtalkDocReadArgs",
        connector_id=(str, Field(description="DingTalk Doc Connector ID")),
        doc_url=(str, Field(description="钉钉文档链接，例如 https://alidocs.dingtalk.com/i/nodes/...")),
        max_chars=(int, Field(default=20000, description="最多返回正文字符数")),
    )

    return [
        StructuredTool.from_function(
            func=lambda connector_type=None: external_list_external_connectors(project_id, connector_type),
            name="list_external_connectors",
            description="列出当前项目 Redis/OSS/Queue/Vector/HTTP 等外部证据连接器。不确定有哪些外部证据源时先调这个。",
            args_schema=ListExternalConnectorsArgs,
        ),
        StructuredTool.from_function(
            func=lambda connector_id, key: redis_exists(project_id, connector_id, key),
            name="redis_exists",
            description="检查 Redis key 是否存在并返回 TTL。",
            args_schema=RedisKeyArgs,
        ),
        StructuredTool.from_function(
            func=lambda connector_id, key: redis_get(project_id, connector_id, key),
            name="redis_get",
            description="读取 Redis 字符串 key 的值和 TTL。只用于明确 key 的只读探查。",
            args_schema=RedisKeyArgs,
        ),
        StructuredTool.from_function(
            func=lambda connector_id, object_key: object_storage_head(project_id, connector_id, object_key),
            name="object_storage_head",
            description="检查 OSS/Object Storage 对象是否存在，返回 size、etag、last_modified 等元数据。",
            args_schema=ObjectHeadArgs,
        ),
        StructuredTool.from_function(
            func=lambda connector_id, prefix, max_keys=50: object_storage_list(project_id, connector_id, prefix, max_keys),
            name="object_storage_list",
            description="按前缀列出 OSS/Object Storage 对象，用于确认样本图片、特征文件等产物是否存在。",
            args_schema=ObjectListArgs,
        ),
        StructuredTool.from_function(
            func=lambda connector_id, name_filter="": queue_overview(project_id, connector_id, name_filter),
            name="queue_overview",
            description="读取 RabbitMQ 管理 API 的队列深度、unacked、consumer 数等摘要。",
            args_schema=QueueOverviewArgs,
        ),
        StructuredTool.from_function(
            func=lambda connector_id: vector_collection_info(project_id, connector_id),
            name="vector_collection_info",
            description="读取 Milvus/Zilliz collection schema 与统计信息，用于确认向量库连接和 collection 状态。",
            args_schema=VectorInfoArgs,
        ),
        StructuredTool.from_function(
            func=lambda connector_id, path="": http_health_check(project_id, connector_id, path),
            name="http_health_check",
            description="对已注册 HTTP Service 执行只读 GET 健康检查或轻量 JSON/文本探查。",
            args_schema=HttpHealthArgs,
        ),
        StructuredTool.from_function(
            func=lambda connector_id, method="GET", path="", query=None, body=None, headers=None, max_chars=4000: http_call(
                project_id,
                connector_id,
                method,
                path,
                query=query,
                body=body,
                headers=headers,
                max_chars=max_chars,
            ),
            name="http_call",
            description=(
                "调用已注册 HTTP Service 的接口获取信息。先用 list_external_connectors(connector_type='http_service') "
                "确认 connector；path 必须是相对路径；method 必须在 connector config.allowed_methods 中启用；"
                "响应会自动截断。"
            ),
            args_schema=HttpCallArgs,
        ),
        StructuredTool.from_function(
            func=lambda connector_id, doc_url, max_chars=20000: dingtalk_doc_read(
                project_id, connector_id, doc_url, max_chars
            ),
            name="dingtalk_doc_read",
            description=(
                "读取已授权的钉钉文档链接正文。先用 list_external_connectors(connector_type='dingtalk_doc') "
                "确认连接器，再传入用户给出的 alidocs/docs 链接。"
            ),
            args_schema=DingtalkDocReadArgs,
        ),
    ]


def _build_file_artifact_tools(project_id: str, topic_thread_id: str = "") -> list[StructuredTool]:
    CreateDownloadableFileArgs = create_model(
        "CreateDownloadableFileArgs",
        filename=(
            str,
            Field(description="下载文件名，需包含扩展名，例如 report.md / export.csv / report.docx / data.xlsx"),
        ),
        content=(str, Field(description="文件正文。CSV/XLSX 建议传 CSV 文本；DOCX/MD/HTML 传报告正文。")),
        content_type=(
            str,
            Field(default="text/markdown; charset=utf-8", description="MIME 类型，可留默认值"),
        ),
    )

    def _create_downloadable_file(
        filename: str,
        content: str,
        content_type: str = "text/markdown; charset=utf-8",
    ) -> str:
        result = create_downloadable_file(
            project_id=project_id,
            topic_thread_id=topic_thread_id or "generated",
            filename=filename,
            content=content,
            content_type=content_type,
        )
        return tool_result_markdown(result)

    return [
        StructuredTool.from_function(
            func=_create_downloadable_file,
            name="create_downloadable_file",
            description=(
                "把本轮分析结果生成可下载文件并上传到 OSS。用户要求导出、excel、word、xlsx、csv，"
                "或结果是长报告/长表格/结构化数据时使用。返回 oss_uri 和 download_url。"
            ),
            args_schema=CreateDownloadableFileArgs,
        )
    ]


def _build_runtime_context_tools(project_id: str) -> list[StructuredTool]:
    ListRuntimeContextsArgs = create_model(
        "ListRuntimeContextsArgs",
        environment=(Optional[str], Field(default="prod", description="环境，默认 prod")),
        cluster=(Optional[str], Field(default=None, description="可选：按集群过滤，例如 prod-cluster")),
        keyword=(str, Field(default="", description="按 workload/command/logstore/cluster 关键词过滤")),
    )
    GetRuntimeContextArgs = create_model(
        "GetRuntimeContextArgs",
        runtime_id=(str, Field(description="Runtime Context ID")),
    )
    DiscoverRuntimeContextsArgs = create_model(
        "DiscoverRuntimeContextsArgs",
        repo_url=(str, Field(description="业务代码仓库 URL，用于推断 app 名并定位 KubeVela 配置")),
        k8s_config_root=(
            Optional[str],
            Field(default=None, description="本地 deploy-config 路径；留空使用工作区默认路径"),
        ),
        app_name=(Optional[str], Field(default=None, description="可选：部署应用名，留空从 repo_url 推断")),
        environment=(str, Field(default="prod", description="环境，默认 prod")),
    )

    return [
        StructuredTool.from_function(
            func=lambda environment="prod", cluster=None, keyword="": runtime_list_runtime_contexts(
                project_id, environment=environment, cluster=cluster, keyword=keyword
            ),
            name="list_runtime_contexts",
            description=(
                "列出当前项目已注册的运行时上下文，包括 workload、cluster、namespace、selector、replicas、"
                "image、command、SLS logstore、Service/Ingress。排查线上问题时先用它确认应该切到哪个集群、查哪个日志入口。"
            ),
            args_schema=ListRuntimeContextsArgs,
        ),
        StructuredTool.from_function(
            func=lambda runtime_id: runtime_get_runtime_context(project_id, runtime_id),
            name="get_runtime_context",
            description="查看单个 Runtime Context 的完整结构化详情。",
            args_schema=GetRuntimeContextArgs,
        ),
        StructuredTool.from_function(
            func=lambda repo_url, k8s_config_root=None, app_name=None, environment="prod": runtime_discover_runtime_contexts(
                project_id,
                repo_url,
                k8s_config_root=k8s_config_root,
                app_name=app_name,
                environment=environment,
            ),
            name="discover_runtime_contexts",
            description=(
                "从 repo_url 自动定位本地 deploy-config 中对应的 KubeVela/Flux 配置并生成 Runtime Context 候选。"
                "这是接入/校验工具；诊断已接入项目时优先用 list_runtime_contexts。"
            ),
            args_schema=DiscoverRuntimeContextsArgs,
        ),
    ]


def _build_code_inspection_tools(project_id: str) -> list[StructuredTool]:
    """构建代码自省三件套工具（仅当 project.git_url 非空且总开关打开时启用）。

    工具对 LLM 隐藏 project_id / workspace，调用时由闭包注入。
    第一次调用任一工具时会懒加载仓库到本地缓存（core.code_sync.ensure_workspace）。
    """
    import json

    project = registry.get_project(project_id)
    repo_connectors = registry.get_repository_connectors(project_id) if project else []
    if not project or (not project.git_url and not repo_connectors) or not code_inspection_config.enabled:
        return []

    ListRepositoryConnectorsArgs = create_model(
        "ListRepositoryConnectorsArgs",
        placeholder=(
            Optional[str],
            Field(default=None, description="无需传参"),
        ),
    )
    CodeGlobArgs = create_model(
        "CodeGlobArgs",
        pattern=(str, Field(description="glob 模式，例：'**/*.py' 或 'services/**/*.go'")),
        max_results=(int, Field(default=200, description="最多返回多少路径")),
        repo_connector_id=(
            Optional[str],
            Field(default=None, description="可选：Repository Connector ID；多仓库项目先用 list_repository_connectors 确认。不传保持默认仓库行为"),
        ),
        connector_id=(
            Optional[str],
            Field(default=None, description="repo_connector_id 的别名"),
        ),
    )
    CodeGrepArgs = create_model(
        "CodeGrepArgs",
        pattern=(str, Field(description="正则，可直接写关键词，例：'createOrder' 或 '(create|add)_order'")),
        path=(str, Field(default="", description="限定到 workspace 子目录/文件，缺省为整个仓库")),
        ignore_case=(bool, Field(default=True, description="是否忽略大小写")),
        fuzzy=(bool, Field(default=False, description="开启后自动拆分 CamelCase/snake_case，扩大召回")),
        max_results=(int, Field(default=50, description="命中上限")),
        repo_connector_id=(
            Optional[str],
            Field(default=None, description="可选：Repository Connector ID；多仓库项目先用 list_repository_connectors 确认。不传保持默认仓库行为"),
        ),
        connector_id=(
            Optional[str],
            Field(default=None, description="repo_connector_id 的别名"),
        ),
    )
    CodeReadArgs = create_model(
        "CodeReadArgs",
        path=(str, Field(description="workspace 内相对路径")),
        start_line=(int, Field(default=1, description="起始行（含，从 1 开始）")),
        end_line=(
            Optional[int],
            Field(default=None, description="结束行（含）；缺省最多读 500 行"),
        ),
        repo_connector_id=(
            Optional[str],
            Field(default=None, description="可选：Repository Connector ID；多仓库项目先用 list_repository_connectors 确认。不传保持默认仓库行为"),
        ),
        connector_id=(
            Optional[str],
            Field(default=None, description="repo_connector_id 的别名"),
        ),
    )
    RunRepoDebugScriptArgs = create_model(
        "RunRepoDebugScriptArgs",
        script_path=(
            str,
            Field(description="workspace 内 Python 脚本相对路径，例如 scripts/test_case.py"),
        ),
        args=(
            Optional[list[str]],
            Field(default=None, description="传给脚本的 argv 字符串数组；不要拼 shell 命令"),
        ),
        timeout_sec=(
            int,
            Field(default=60, description="执行超时秒数，系统上限 120 秒"),
        ),
        max_chars=(
            int,
            Field(default=12000, description="stdout/stderr 各自最多返回多少字符，系统上限 20000"),
        ),
        use_venv=(
            str,
            Field(default="auto", description="venv 模式：'auto'(默认，已建则用项目 venv)/'on'(强制 venv，未建报错)/'off'(用 Viktor 解释器)"),
        ),
        repo_connector_id=(
            Optional[str],
            Field(default=None, description="可选：Repository Connector ID；多仓库项目先用 list_repository_connectors 确认。不传保持默认仓库行为"),
        ),
        connector_id=(
            Optional[str],
            Field(default=None, description="repo_connector_id 的别名"),
        ),
    )
    WriteRepoDebugFileArgs = create_model(
        "WriteRepoDebugFileArgs",
        path=(str, Field(description="workspace 内相对路径，例如 scripts/test_goofish_case.py")),
        content=(str, Field(description="要写入的完整文件内容")),
        overwrite=(bool, Field(default=True, description="目标文件存在时是否覆盖")),
        repo_connector_id=(
            Optional[str],
            Field(default=None, description="可选：Repository Connector ID；多仓库项目先用 list_repository_connectors 确认。不传保持默认仓库行为"),
        ),
        connector_id=(
            Optional[str],
            Field(default=None, description="repo_connector_id 的别名"),
        ),
    )
    RunRepoCommandArgs = create_model(
        "RunRepoCommandArgs",
        command=(
            list[str],
            Field(description="命令 argv 字符串数组；需要 shell 时显式传 ['bash', '-lc', '...']"),
        ),
        cwd=(str, Field(default="", description="workspace 内相对工作目录；缺省为仓库根")),
        timeout_sec=(int, Field(default=60, description="执行超时秒数")),
        max_chars=(int, Field(default=12000, description="stdout/stderr 各自最多返回多少字符")),
        extra_env=(
            Optional[dict[str, str]],
            Field(default=None, description="可选附加环境变量"),
        ),
        use_venv=(
            str,
            Field(default="auto", description="venv 模式：'auto'(默认，已建则把项目 venv/bin 注入 PATH)/'on'(强制)/'off'"),
        ),
        repo_connector_id=(
            Optional[str],
            Field(default=None, description="可选：Repository Connector ID；多仓库项目先用 list_repository_connectors 确认。不传保持默认仓库行为"),
        ),
        connector_id=(
            Optional[str],
            Field(default=None, description="repo_connector_id 的别名"),
        ),
    )
    SetupRepoVenvArgs = create_model(
        "SetupRepoVenvArgs",
        install=(
            bool,
            Field(default=True, description="是否安装依赖；False 时只创建空 venv"),
        ),
        force=(
            bool,
            Field(default=False, description="忽略依赖指纹强制重装（依赖文件没变也重装）"),
        ),
        extra_packages=(
            Optional[list[str]],
            Field(default=None, description="requirements 之外要额外安装的包，例如 ['aliyun-log-python-sdk']"),
        ),
        timeout_sec=(
            int,
            Field(default=0, description=f"安装超时秒数；缺省 {repo_venv_config.install_timeout_sec}，上限 {repo_venv_config.max_install_timeout_sec}"),
        ),
        repo_connector_id=(
            Optional[str],
            Field(default=None, description="可选：Repository Connector ID；多仓库项目先用 list_repository_connectors 确认。不传保持默认仓库行为"),
        ),
        connector_id=(
            Optional[str],
            Field(default=None, description="repo_connector_id 的别名"),
        ),
    )

    def _json(data: Any) -> str:
        return json.dumps(data, ensure_ascii=False, default=str)

    def _list_repository_connectors(placeholder: Optional[str] = None) -> str:  # noqa: ARG001
        items: list[str] = []
        if project.git_url:
            items.append(
                f"- default  (project git_url, branch={project.default_branch or 'master'})"
            )
        for repo in repo_connectors:
            items.append(
                f"- {repo.id}  (name={repo.display_name or repo.id}, branch={repo.default_branch or 'master'}, url={repo.git_url})"
            )
        if not items:
            return f"项目 '{project_id}' 尚未注册任何代码仓库"
        return "当前项目代码仓库连接器：\n" + "\n".join(items)

    def _glob(
        pattern: str,
        max_results: int = 200,
        repo_connector_id: Optional[str] = None,
        connector_id: Optional[str] = None,
    ) -> str:
        return _json(
            code_glob(
                project_id, pattern, max_results=max_results,
                connector_id=connector_id, repo_connector_id=repo_connector_id,
            )
        )

    def _grep(
        pattern: str, path: str = "", ignore_case: bool = True,
        fuzzy: bool = False, max_results: int = 50,
        repo_connector_id: Optional[str] = None, connector_id: Optional[str] = None,
    ) -> str:
        return _json(code_grep(
            project_id, pattern, path=path, ignore_case=ignore_case,
            fuzzy=fuzzy, max_results=max_results,
            connector_id=connector_id, repo_connector_id=repo_connector_id,
        ))

    def _read(
        path: str,
        start_line: int = 1,
        end_line: Optional[int] = None,
        repo_connector_id: Optional[str] = None,
        connector_id: Optional[str] = None,
    ) -> str:
        return _json(
            code_read(
                project_id, path, start_line=start_line, end_line=end_line,
                connector_id=connector_id, repo_connector_id=repo_connector_id,
            )
        )

    def _run_debug_script(
        script_path: str,
        args: Optional[list[str]] = None,
        timeout_sec: int = 60,
        max_chars: int = 12000,
        use_venv: str = "auto",
        repo_connector_id: Optional[str] = None,
        connector_id: Optional[str] = None,
    ) -> str:
        return _json(
            run_repo_debug_script(
                project_id,
                script_path,
                args=args,
                timeout_sec=timeout_sec,
                max_chars=max_chars,
                connector_id=connector_id,
                repo_connector_id=repo_connector_id,
                use_venv=use_venv,
            )
        )

    def _setup_venv(
        install: bool = True,
        force: bool = False,
        extra_packages: Optional[list[str]] = None,
        timeout_sec: int = 0,
        repo_connector_id: Optional[str] = None,
        connector_id: Optional[str] = None,
    ) -> str:
        return _json(
            ensure_repo_venv(
                project_id,
                install=install,
                force=force,
                extra_packages=extra_packages,
                timeout_sec=timeout_sec,
                connector_id=connector_id,
                repo_connector_id=repo_connector_id,
            )
        )

    def _write_debug_file(
        path: str,
        content: str,
        overwrite: bool = True,
        repo_connector_id: Optional[str] = None,
        connector_id: Optional[str] = None,
    ) -> str:
        return _json(
            write_repo_debug_file(
                project_id,
                path,
                content,
                overwrite=overwrite,
                connector_id=connector_id,
                repo_connector_id=repo_connector_id,
            )
        )

    def _run_repo_command(
        command: list[str],
        cwd: str = "",
        timeout_sec: int = 60,
        max_chars: int = 12000,
        extra_env: Optional[dict[str, str]] = None,
        repo_connector_id: Optional[str] = None,
        connector_id: Optional[str] = None,
    ) -> str:
        return _json(
            run_repo_command(
                project_id,
                command,
                cwd=cwd,
                timeout_sec=timeout_sec,
                max_chars=max_chars,
                extra_env=extra_env,
                connector_id=connector_id,
                repo_connector_id=repo_connector_id,
            )
        )

    CodeExploreArgs = create_model(
        "CodeExploreArgs",
        task=(
            str,
            Field(description="子任务描述，越具体越好，例：'定位订单创建主流程的核心文件和关键函数'"),
        ),
        repo_connector_id=(
            Optional[str],
            Field(default=None, description="可选：Repository Connector ID；多仓库项目先用 list_repository_connectors 确认。不传保持默认仓库行为"),
        ),
        connector_id=(
            Optional[str],
            Field(default=None, description="repo_connector_id 的别名"),
        ),
    )

    async def _explore(
        task: str,
        repo_connector_id: Optional[str] = None,
        connector_id: Optional[str] = None,
    ) -> str:
        res = await run_explorer(
            project_id, task, connector_id=connector_id, repo_connector_id=repo_connector_id,
        )
        return _json(res)

    def _explore_sync(
        task: str,
        repo_connector_id: Optional[str] = None,
        connector_id: Optional[str] = None,
    ) -> str:
        # 同步占位：仅供同步调用配套。实践中主 agent 走 ainvoke，会用 coroutine。
        import asyncio
        return _json(
            asyncio.get_event_loop().run_until_complete(
                run_explorer(
                    project_id, task, connector_id=connector_id, repo_connector_id=repo_connector_id,
                )
            )
        )

    return [
        StructuredTool.from_function(
            func=_list_repository_connectors,
            name="list_repository_connectors",
            description="列出当前项目已注册的代码仓库连接器 ID。多仓库项目在代码读取、写临时调试脚本、运行验证命令前先调用它确认 repo_connector_id。",
            args_schema=ListRepositoryConnectorsArgs,
        ),
        StructuredTool.from_function(
            func=_glob,
            name="code_glob",
            description=(
                "在项目源码 workspace 中按 glob 找文件路径（自动遵循 .gitignore）。"
                "多仓库项目先用 list_repository_connectors 确认 repo_connector_id；不传则使用项目默认仓库。"
                "适合第一步缩小範围，例如 '**/order*.py'。"
            ),
            args_schema=CodeGlobArgs,
        ),
        StructuredTool.from_function(
            func=_grep,
            name="code_grep",
            description=(
                "在项目源码中全文搜索关键词/正则。"
                "多仓库项目先用 list_repository_connectors 确认 repo_connector_id；不传则使用项目默认仓库。"
                "用户询问业务概念、口径、字段含义或如何区分时，只有本轮 glossary/knowledge 证据不足或 missing_terms 非空，才用本工具补齐代码语义。"
                "策略：先列 3-5 个候选关键词（同义词/英文映射/命名风格变体），"
                "用 '(a|b|c)' 拼成正则一次调用；0 结果时放宽或换词。"
            ),
            args_schema=CodeGrepArgs,
        ),
        StructuredTool.from_function(
            func=_read,
            name="code_read",
            description=(
                "读取项目源码中的指定文件片段（带行号），"
                "多仓库项目先用 list_repository_connectors 确认 repo_connector_id；不传则使用项目默认仓库。"
                "适合 grep 定位到文件:行 后进一步查看上下文。单次最多 500 行。"
            ),
            args_schema=CodeReadArgs,
        ),
        StructuredTool.from_function(
            func=_write_debug_file,
            name="write_repo_debug_file",
            description=(
                "在项目源码 workspace 中写入临时复现/验证文件，例如生成 scripts/test_xxx.py。"
                "用于快速验证和升级能力，文件只写入 Viktor 的本地 repo cache，不会自动提交。"
                "多仓库项目先用 list_repository_connectors 确认 repo_connector_id。"
            ),
            args_schema=WriteRepoDebugFileArgs,
        ),
        StructuredTool.from_function(
            func=_run_repo_command,
            name="run_repo_command",
            description=(
                "在项目源码 workspace 内执行验证命令 argv。可直接跑 python/pytest/unittest/curl，也可显式使用 ['bash','-lc','...']。"
                "默认 use_venv='auto'：仓库已建 venv 则把 venv/bin 注入 PATH，python/pip/pytest 解析到项目 venv；需要项目依赖时先 setup_repo_venv。"
                f"当前策略：{repo_debug_runner_policy_summary()}。多仓库项目先用 list_repository_connectors 确认 repo_connector_id。"
            ),
            args_schema=RunRepoCommandArgs,
        ),
        StructuredTool.from_function(
            func=_setup_venv,
            name="setup_repo_venv",
            description=(
                "为该仓库创建隔离虚拟环境并安装其依赖（requirements*.txt 等），之后 run_repo_debug_script / "
                "run_repo_command 会自动复用这个 venv，让复现脚本能 import 项目自己的三方依赖。"
                "venv 按仓库跨 commit 复用、按依赖文件指纹去重，已装好时秒回。"
                "用法：脚本因 ModuleNotFoundError 失败、或预判脚本要 import 项目三方库时，先调用本工具；"
                "缺个别包可用 extra_packages 补装；依赖文件没变但想强制重装传 force=true。"
                "安装可能耗时（分钟级），本工具有独立长超时。"
                f"当前策略：{repo_venv_policy_summary()}。多仓库项目先用 list_repository_connectors 确认 repo_connector_id。"
            ),
            args_schema=SetupRepoVenvArgs,
        ),
        StructuredTool.from_function(
            func=_run_debug_script,
            name="run_repo_debug_script",
            description=(
                "运行项目源码 workspace 内的 Python 调试/验证脚本，用于复现指定 case 或核验修复。"
                "默认 use_venv='auto'：仓库已用 setup_repo_venv 建好 venv 则用其解释器（含项目三方依赖），否则退回 Viktor 自身解释器。"
                "脚本若 import 项目三方依赖失败，先调用 setup_repo_venv 再重试。"
                "如果脚本不存在，可先用 write_repo_debug_file 生成 scripts/test_xxx.py；"
                "多仓库项目先用 list_repository_connectors 确认 repo_connector_id；不传则使用项目默认仓库。"
            ),
            args_schema=RunRepoDebugScriptArgs,
        ),
        StructuredTool.from_function(
            func=_explore_sync,
            coroutine=_explore,
            name="code_explore",
            description=(
                "启动一个独立的代码探索 sub-agent，自主多轮调用 code_glob/code_grep/code_read 定位与 task 相关的文件和关键函数，"
                "返回结构化总结（relevant_files / key_symbols / searched_keywords）。"
                "多仓库项目先用 list_repository_connectors 确认 repo_connector_id；不传则使用项目默认仓库。"
                "适合问题模糊、需要跨文件追查的场景；不要用于单文件快查（那用 code_grep/code_read）。"
            ),
            args_schema=CodeExploreArgs,
        ),
    ]


def _safe_sse_value(obj: Any, *, max_depth: int = 4, max_str: int = 260) -> Any:
    """把任意对象压成可 JSON 序列化、体积可控的结构（供 SSE 展示）。"""
    if max_depth <= 0:
        return "…"
    if obj is None or isinstance(obj, (bool, int, float)):
        return obj
    if isinstance(obj, str):
        return obj if len(obj) <= max_str else obj[:max_str] + "…"
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for i, (k, v) in enumerate(obj.items()):
            if i >= 24:
                out["…"] = f"共 {len(obj)} 项，已截断"
                break
            key = str(k)[:72]
            out[key] = _safe_sse_value(v, max_depth=max_depth - 1, max_str=max_str)
        return out
    if isinstance(obj, (list, tuple)):
        return [_safe_sse_value(x, max_depth=max_depth - 1, max_str=max_str) for x in obj[:40]]
    return str(obj)[:max_str]


def _tool_calls_from_task_input(task_input: Any) -> list[dict[str, Any]]:
    """从 ToolNode 任务的 input state 里取出本轮 AIMessage.tool_calls。"""
    if not isinstance(task_input, dict):
        return []
    msgs = task_input.get("messages")
    if not isinstance(msgs, list):
        return []
    for m in reversed(msgs):
        if isinstance(m, AIMessage):
            calls = m.tool_calls
            if calls:
                cleaned: list[dict[str, Any]] = []
                for c in calls:
                    if isinstance(c, dict):
                        cleaned.append(
                            {
                                "name": c.get("name") or "",
                                "args": c.get("args"),
                                "id": c.get("id"),
                            }
                        )
                return cleaned
    return []


def _tool_results_for_round(msgs: list[Any], n_calls: int) -> list[ToolMessage | None]:
    """从 state.messages 末尾连续回溯，取本轮 n 条 ToolMessage（与 tool_calls 顺序对齐）。"""
    if n_calls <= 0:
        return []
    acc: list[ToolMessage] = []
    for m in reversed(msgs):
        if isinstance(m, ToolMessage):
            acc.append(m)
            if len(acc) >= n_calls:
                break
        elif acc:
            break
    acc.reverse()
    out: list[ToolMessage | None] = []
    for i in range(n_calls):
        out.append(acc[i] if i < len(acc) else None)
    return out


def _message_chunk_visible_text(chunk: Any) -> str:
    """从 LLM 流式 chunk 提取面向用户的正文（不含 tool_calls 结构）。"""
    if not isinstance(chunk, AIMessageChunk):
        return ""
    if chunk.tool_calls or chunk.tool_call_chunks:
        return ""
    content = chunk.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(str(block.get("text") or ""))
        return "".join(parts)
    return ""


def _ai_message_from_chunk(chunk: AIMessageChunk) -> AIMessage:
    """把累计后的 AIMessageChunk 转成可追加到 history 的 AIMessage。"""
    return AIMessage(
        content=chunk.content,
        additional_kwargs=dict(getattr(chunk, "additional_kwargs", None) or {}),
        tool_calls=list(getattr(chunk, "tool_calls", None) or []),
        invalid_tool_calls=list(getattr(chunk, "invalid_tool_calls", None) or []),
    )


def _tool_jobs_from_ai_message(message: AIMessage, start_seq: int = 0) -> list[ToolJob]:
    jobs: list[ToolJob] = []
    for offset, call in enumerate(message.tool_calls or [], start=1):
        if not isinstance(call, dict):
            continue
        name = str(call.get("name") or "")
        call_id = str(call.get("id") or f"tool-call-{start_seq + offset}")
        raw_args = call.get("args") or {}
        args = raw_args if isinstance(raw_args, dict) else {"input": raw_args}
        jobs.append(ToolJob(seq=start_seq + offset, call_id=call_id, name=name, args=args))
    return jobs


def _tool_result_preview(result: ToolJobResult, max_len: int = 480) -> str:
    preview = result.content or ""
    if result.elapsed_ms:
        preview = f"[{result.elapsed_ms}ms] {preview}"
    if len(preview) > max_len:
        return preview[:max_len] + "…"
    return preview


def _interrupted_ai_message(visible_text: str) -> AIMessage:
    content = "本轮回复被用户中断。"
    if visible_text.strip():
        content += "\n\n中断前已输出内容：\n" + visible_text.strip()
    return AIMessage(content=content)


def _safe_interrupted_messages(
    new_messages: list[Any],
    *,
    visible_text: str,
) -> list[Any]:
    """
    中断时只持久化可安全重放的上下文。

    LangChain/OpenAI-style tool calling 要求带 tool_calls 的 AIMessage 后面必须紧跟
    对应 ToolMessage。用户中断可能发生在工具执行中间，此时直接落库会让下一轮历史失效。
    """
    safe: list[Any] = []
    i = 0
    wrote_interruption_note = False

    while i < len(new_messages):
        msg = new_messages[i]
        if isinstance(msg, HumanMessage):
            safe.append(msg)
            i += 1
            continue

        if isinstance(msg, AIMessage):
            tool_calls = list(getattr(msg, "tool_calls", None) or [])
            if not tool_calls:
                if msg.content:
                    safe.append(msg)
                i += 1
                continue

            expected_ids = {str(c.get("id") or "") for c in tool_calls if isinstance(c, dict)}
            following = new_messages[i + 1 : i + 1 + len(expected_ids)]
            actual_ids = {
                str(getattr(t, "tool_call_id", "") or "")
                for t in following
                if isinstance(t, ToolMessage)
            }
            if expected_ids and expected_ids.issubset(actual_ids):
                safe.append(msg)
                safe.extend(following)
                i += 1 + len(following)
                continue

            if not wrote_interruption_note:
                safe.append(_interrupted_ai_message(visible_text))
                wrote_interruption_note = True
            break

        if isinstance(msg, ToolMessage):
            if not wrote_interruption_note:
                safe.append(_interrupted_ai_message(visible_text))
                wrote_interruption_note = True
            i += 1
            continue

        i += 1

    if not wrote_interruption_note and visible_text.strip():
        has_visible_ai = any(isinstance(m, AIMessage) and m.content for m in safe)
        if not has_visible_ai:
            safe.append(_interrupted_ai_message(visible_text))

    return safe


def _build_project_tools(project_id: str, topic_thread_id: str = "") -> list[StructuredTool]:
    """构建指定项目的所有可用工具。

    顺序：文件导出 -> 运行时上下文 -> 数据库探索工具 -> 日志 -> 外部连接器 -> 代码自省 -> K8s 内置工具。
    """
    tools: list[StructuredTool] = []

    tools.extend(_build_file_artifact_tools(project_id, topic_thread_id=topic_thread_id))
    tools.extend(_build_runtime_context_tools(project_id))
    tools.extend(_build_db_explorer_tools(project_id))
    tools.extend(_build_log_tools(project_id))
    tools.extend(_build_external_connector_tools(project_id))
    tools.extend(_build_code_inspection_tools(project_id))
    tools.extend(_build_k8s_tools())
    return tools


def _project_not_ready_text(project_id: str) -> str:
    status = registry.get_status()
    project_status = status.get("projects", {}).get(project_id, {})
    return (
        f"项目 '{project_id}' 尚未完成注册，无法进行诊断。\n\n"
        f"当前状态：上下文 {len(project_status.get('contexts', []))} 个，"
        f"数据库连接器 {len(project_status.get('database_connectors', []))} 个。\n\n"
        "请先至少注册一条业务上下文；若需查库，请注册只读数据库连接器。"
    )


def _max_iterations_text() -> str:
    return (
        f"⚠️ 诊断步数超过上限（max_iterations={agent_config.max_iterations}），任务尚未完成。\n\n"
        f"建议：\n"
        f"- 把问题拆成更小粒度分多次提问\n"
        f"- 使用 /clear 清空上下文后重试，避免历史消息挤占预算\n"
        f"- 若确为复杂分析，可联系管理员提高 agent.max_iterations 或 AGENT_MAX_ITERATIONS"
    )


def _long_running_tool_timeouts() -> dict[str, int]:
    """长任务工具的外层超时豁免表。

    这些工具（建 venv、跑/装依赖、跑复现脚本）天然需要远超通用 tool_timeout_sec(75s)
    的预算，否则通用上限会先于工具内部超时触发，使它们永远「超时」而无法完成
    （正是「安装依赖超时」死循环的根因）。给每个工具的外层预算取其内部硬上限再加
    一点 margin，让内部更细的超时先生效、返回更可读的错误。
    """
    return {
        # venv 安装：内部硬上限 max_install_timeout_sec（默认 1800）
        "setup_repo_venv": repo_venv_config.max_install_timeout_sec + 60,
        # 跑复现脚本 / 仓库命令：内部硬上限 max_timeout_sec（默认 120）
        "run_repo_debug_script": repo_debug_runner_config.max_timeout_sec + 30,
        "run_repo_command": repo_debug_runner_config.max_timeout_sec + 30,
    }


def _tool_timeout_budget_text(timeout_count: int) -> str:
    return (
        f"⚠️ 已停止继续调用工具：本轮已有 {timeout_count} 次工具/SQL 查询超时。\n\n"
        "这通常说明当前查询范围过大，或缺少能命中索引的强过滤条件。"
        "继续让 Agent 自行试 SQL 容易放大耗时，也可能给数据库带来压力。\n\n"
        "建议先收窄口径后重试，例如限定更短时间窗口、平台、状态、剧集/母本范围；"
        "也可以让我基于已经查到的证据先给出部分结论。"
    )


def _tool_timeout_synthesis_instruction(timeout_count: int) -> str:
    return (
        f"本轮已经出现 {timeout_count} 次工具或 SQL 超时。现在必须停止调用工具，直接回复用户。\n"
        "请基于当前对话中已经拿到的 schema、样例、SQL 结果、EXPLAIN/拦截/超时信息做收口：\n"
        "1. 如果已有足够证据，给出部分结论并明确哪些数字是已验证的。\n"
        "2. 如果无法得到精确答案，说明阻塞点，不要编造数据。\n"
        "3. 给出下一步最小补充条件，例如更短时间窗口、平台、状态、母本/剧集范围，或需要离线聚合。\n"
        "4. 不要建议继续重复同类大范围 SQL。"
    )


async def _synthesize_after_tool_timeouts(
    setup: AgentRunSetup,
    timeout_count: int,
) -> AIMessage:
    if setup.llm is None:
        return AIMessage(content=_tool_timeout_budget_text(timeout_count))
    try:
        response = await _invoke_ai_message(
            setup.llm,
            [*setup.messages, SystemMessage(content=_tool_timeout_synthesis_instruction(timeout_count))],
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("工具超时后无工具收口失败: {}", e)
        return AIMessage(content=_tool_timeout_budget_text(timeout_count))
    if not str(response.content or "").strip():
        return AIMessage(content=_tool_timeout_budget_text(timeout_count))
    return response


def _webchat_large_table_clarification(
    *,
    project_id: str,
    user_message: str,
    final_content: str,
) -> dict[str, Any] | None:
    """Convert free-form large-table clarification text into structured cards.

    Open-source snapshot keeps this hook generic; private deployments can add
    project-specific card builders around this function.
    """
    return None


def _intent_status_text(route: IntentRoute) -> str:
    mapping = {
        "glossary_only": "已识别为业务概念映射。",
        "glossary_then_db": "已识别为业务概念映射 + 数据统计。",
        "glossary_then_code": "正在补齐缺失术语的代码证据...",
        "clarify_first": "已识别到缺失业务口径，准备先澄清。",
        "log_first": "已识别为线上运行/日志排查。",
        "direct_answer": "已识别为可直接回答的问题。",
    }
    return mapping.get(route.tool_strategy, "已完成项目业务术语匹配。")


def _new_turn_messages(messages: list[Any], history: list[Any]) -> list[Any]:
    # 跳过 SystemMessage 和历史消息，保留本轮 Human/AI/Tool 消息。
    return list(messages[1 + len(history):])


def _save_agent_turn(
    *,
    session_id: Optional[str],
    topic_thread_id: Optional[str],
    project_id: str,
    messages: list[Any],
    history: list[Any],
    interrupted: bool = False,
    visible_text: str = "",
    stream: bool = False,
) -> bool:
    if not session_id or not topic_thread_id or not messages:
        return False
    new_slice = _new_turn_messages(messages, history)
    if interrupted:
        new_slice = _safe_interrupted_messages(new_slice, visible_text=visible_text)
    if not new_slice:
        return False
    try:
        save_turn(session_id, topic_thread_id, project_id, new_slice)
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "会话历史落库失败{}, session={}, topic={}, interrupted={}, error={}",
            "(stream)" if stream else "",
            session_id,
            topic_thread_id,
            interrupted,
            e,
        )
        return False


def _provider_order_with_fallback(preferred_provider: str) -> list[str] | None:
    preferred = (preferred_provider or "").strip()
    if not preferred:
        return None

    ordered: list[str] = []
    for provider_id in [preferred, *llm_config.fallback_order]:
        if provider_id and provider_id in llm_config.providers and provider_id not in ordered:
            ordered.append(provider_id)
    return ordered or None


async def _prepare_agent_run(
    user_message: str,
    project_id: str,
    *,
    trace_id: str,
    session_id: Optional[str],
    topic_thread_id: Optional[str],
    llm_feature: str,
    compaction_title: str,
    webchat_clarification: bool = False,
    provider_order: list[str] | None = None,
    attachments: list[dict[str, Any]] | None = None,
    user_role: str = "",
) -> AgentPrepareResult:
    """共享 ready check、prompt/history、clarification 与 compaction 准备逻辑。"""
    if not registry.is_ready(project_id):
        return AgentPrepareResult(error_text=_project_not_ready_text(project_id))

    llm = create_llm(feature=llm_feature, provider_order=provider_order)
    tools = _build_project_tools(project_id, topic_thread_id=topic_thread_id or "")
    if not tools:
        return AgentPrepareResult(error_text="当前项目没有可用的内置工具，请联系管理员检查服务配置。")

    user_message = normalize_user_command_text(user_message)
    export_requested = is_export_request(user_message)
    attachment_block = format_attachments_for_prompt(attachments)
    if attachment_block:
        user_message = f"{user_message}{attachment_block}"
    history = (
        load_history(session_id, topic_thread_id)
        if session_id and topic_thread_id
        else []
    )
    recent_context = "\n\n".join(
        str(getattr(m, "content", "") or "")[:1000]
        for m in history[-6:]
        if str(getattr(m, "content", "") or "").strip()
    )
    intent_route: IntentRoute | None = None
    retrieval_context = ""
    intent_context = prepare_intent_context(
        project_id=project_id,
        user_message=user_message,
        trace_id=trace_id,
        session_id=session_id,
        topic_thread_id=topic_thread_id,
        recent_context=recent_context,
        trace_meta={"scope": "chat"},
    )
    if intent_context.route is not None:
        intent_route = intent_context.route
        retrieval_context = intent_context.retrieval_context
    system_prompt = await build_system_prompt(
        project_id,
        user_message,
        enable_routing=True,
        retrieval_context=retrieval_context,
        user_role=user_role,
    )
    if export_requested:
        system_prompt = f"{system_prompt}{_FILE_OUTPUT_INSTRUCTIONS}"

    if webchat_clarification:
        clarification = await run_clarification_gate(
            scenario="webchat",
            user_message=user_message,
            project_context=system_prompt,
            recent_context=recent_context,
            feature="webchat_clarification",
        )
        record_trace_event(
            trace_id=trace_id,
            event_type="clarification_decision",
            project_id=project_id,
            session_id=session_id,
            topic_thread_id=topic_thread_id,
            payload=clarification,
        )
        if clarification.get("needs_clarification") and clarification.get("questions"):
            text = format_clarification_text(clarification)
            messages: list[Any] = [
                SystemMessage(content=system_prompt),
                *history,
                HumanMessage(content=user_message),
                AIMessage(content=text),
            ]
            return AgentPrepareResult(
                setup=AgentRunSetup(
                    user_message=user_message,
                    history=history,
                    messages=messages,
                    trace_id=trace_id,
                    intent_route=intent_route,
                    retrieval_context=retrieval_context,
                ),
                clarification={
                    "message": text,
                    "questions": clarification.get("questions") or [],
                    "term_mappings": clarification.get("term_mappings") or [],
                },
            )

        term_mapping_block = format_term_mappings_for_prompt(clarification)
        if term_mapping_block:
            system_prompt = f"{system_prompt}\n\n{term_mapping_block}"

    if context_compaction_config.enabled and history and session_id and topic_thread_id:
        compacted_history, summary = await compact_messages(
            history,
            threshold_tokens=context_compaction_config.threshold_tokens,
            target_tokens=context_compaction_config.target_tokens,
            keep_recent_turns=context_compaction_config.keep_recent_turns,
            title=compaction_title,
        )
        if summary:
            save_compaction_summary(session_id, topic_thread_id, project_id, summary)
            history = compacted_history

    messages = [
        SystemMessage(content=system_prompt),
        *history,
        HumanMessage(content=user_message),
    ]
    return AgentPrepareResult(
        setup=AgentRunSetup(
            user_message=user_message,
            history=history,
            messages=messages,
            trace_id=trace_id,
            intent_route=intent_route,
            retrieval_context=retrieval_context,
            llm=llm,
            llm_with_tools=llm.bind_tools(tools),
            tool_manager=ToolExecutionManager(
                tools,
                max_concurrency=agent_config.tool_max_concurrency,
                timeout_sec=agent_config.tool_timeout_sec,
                tool_timeout_overrides=_long_running_tool_timeouts(),
            ),
        )
    )


async def _stream_ai_message(
    llm_with_tools: Any,
    messages: list[Any],
) -> AsyncIterator[dict[str, Any]]:
    full_chunk: AIMessageChunk | None = None
    llm_stream = llm_with_tools.astream(messages)
    pending_event = asyncio.create_task(llm_stream.__anext__())
    try:
        while True:
            done, _ = await asyncio.wait({pending_event}, timeout=15)
            if not done:
                yield {"type": "heartbeat", "active_tools": 0}
                continue

            try:
                token = pending_event.result()
            except StopAsyncIteration:
                break

            pending_event = asyncio.create_task(llm_stream.__anext__())
            if isinstance(token, AIMessageChunk):
                full_chunk = token if full_chunk is None else full_chunk + token
            delta = _message_chunk_visible_text(token)
            if delta:
                yield {"type": "delta", "text": delta}
    finally:
        pending_event.cancel()
        try:
            await pending_event
        except BaseException:
            pass
        try:
            await llm_stream.aclose()
        except Exception:
            pass

    if full_chunk is None:
        yield {"type": "_agent_error", "text": "Agent 未能生成有效回复，请重试。"}
    else:
        yield {"type": "_ai_message", "message": _ai_message_from_chunk(full_chunk)}


async def _invoke_ai_message(llm_with_tools: Any, messages: list[Any]) -> AIMessage:
    response = await llm_with_tools.ainvoke(messages)
    if not isinstance(response, AIMessage):
        response = AIMessage(content=str(response))
    return response


async def _run_tool_jobs(
    tool_manager: ToolExecutionManager,
    jobs: list[ToolJob],
    *,
    stream: bool,
    trace_id: str = "",
    project_id: str = "",
    session_id: Optional[str] = None,
    topic_thread_id: Optional[str] = None,
) -> AsyncIterator[dict[str, Any]]:
    for job in jobs:
        record_trace_event(
            trace_id=trace_id,
            event_type="tool_start",
            project_id=project_id,
            session_id=session_id,
            topic_thread_id=topic_thread_id,
            payload={"seq": job.seq, "tool": job.name, "call_id": job.call_id, "input": job.args},
        )
    if stream:
        for job in jobs:
            yield {
                "type": "tool_start",
                "seq": job.seq,
                "tool": job.name,
                "input": _safe_sse_value(job.args),
            }

    results: dict[str, ToolJobResult] = {}
    if not stream:
        async for result in tool_manager.iter_results(jobs):
            results[result.job.call_id] = result
            record_trace_event(
                trace_id=trace_id,
                event_type="tool_end",
                project_id=project_id,
                session_id=session_id,
                topic_thread_id=topic_thread_id,
                payload={
                    "seq": result.job.seq,
                    "tool": result.job.name,
                    "call_id": result.job.call_id,
                    "ok": result.ok,
                    "elapsed_ms": result.elapsed_ms,
                    "error": result.error,
                    "output": result.content,
                },
            )
        yield {"type": "_tool_results", "results": results}
        return

    result_stream = tool_manager.iter_results(jobs)
    pending_result = asyncio.create_task(result_stream.__anext__())
    try:
        while True:
            done, _ = await asyncio.wait({pending_result}, timeout=15)
            if not done:
                pending_count = len(jobs) - len(results)
                yield {"type": "heartbeat", "active_tools": pending_count}
                continue

            try:
                result = pending_result.result()
            except StopAsyncIteration:
                break

            results[result.job.call_id] = result
            record_trace_event(
                trace_id=trace_id,
                event_type="tool_end",
                project_id=project_id,
                session_id=session_id,
                topic_thread_id=topic_thread_id,
                payload={
                    "seq": result.job.seq,
                    "tool": result.job.name,
                    "call_id": result.job.call_id,
                    "ok": result.ok,
                    "elapsed_ms": result.elapsed_ms,
                    "error": result.error,
                    "output": result.content,
                },
            )
            yield {
                "type": "tool_end",
                "seq": result.job.seq,
                "tool": result.job.name,
                "ok": result.ok,
                "output_preview": _tool_result_preview(result),
            }
            pending_result = asyncio.create_task(result_stream.__anext__())
    finally:
        pending_result.cancel()
        try:
            await pending_result
        except BaseException:
            pass
        try:
            await result_stream.aclose()
        except Exception:
            pass

    yield {"type": "_tool_results", "results": results}


async def _run_agent_core(
    setup: AgentRunSetup,
    *,
    project_id: str,
    session_id: Optional[str],
    topic_thread_id: Optional[str],
    stream: bool,
) -> AsyncIterator[dict[str, Any]]:
    """共享 LLM/tool 多轮循环；调用方只适配输出形态和持久化时机。"""
    if setup.llm_with_tools is None or setup.tool_manager is None:
        yield {"type": "_agent_error", "text": "Agent 未能生成有效回复，请重试。"}
        return

    messages = setup.messages
    last_messages: list[Any] = list(messages)
    tool_seq_counter = 0
    final_content = ""
    tool_timeout_count = 0

    for _step in range(agent_config.max_iterations):
        if stream:
            ai_message: AIMessage | None = None
            async for event in _stream_ai_message(setup.llm_with_tools, messages):
                if event["type"] == "_ai_message":
                    ai_message = event["message"]
                else:
                    yield event
            if ai_message is None:
                return
        else:
            ai_message = await _invoke_ai_message(setup.llm_with_tools, messages)

        messages.append(ai_message)
        last_messages = list(messages)

        jobs = _tool_jobs_from_ai_message(ai_message, tool_seq_counter)
        tool_seq_counter += len(jobs)
        if not jobs:
            final_content = ai_message.content if isinstance(ai_message.content, str) else str(ai_message.content)
            break

        tool_results: dict[str, ToolJobResult] = {}
        async for event in _run_tool_jobs(
            setup.tool_manager,
            jobs,
            stream=stream,
            trace_id=setup.trace_id,
            project_id=project_id,
            session_id=session_id,
            topic_thread_id=topic_thread_id,
        ):
            if event["type"] == "_tool_results":
                tool_results = event["results"]
            else:
                yield event
        messages.extend(setup.tool_manager.to_tool_messages(jobs, tool_results))
        last_messages = list(messages)

        round_timeouts = sum(
            1
            for result in tool_results.values()
            if result.error == "timeout" or "查询执行超时" in result.content or "工具执行超时" in result.content
        )
        tool_timeout_count += round_timeouts
        if tool_timeout_count >= agent_config.max_tool_timeouts_per_turn:
            logger.warning(
                "Agent 工具超时达到阈值{}, project={}, session={}, topic={}, timeout_count={}",
                " (stream)" if stream else "",
                project_id,
                session_id,
                topic_thread_id,
                tool_timeout_count,
            )
            final_message = await _synthesize_after_tool_timeouts(setup, tool_timeout_count)
            messages.append(final_message)
            last_messages = list(messages)
            final_content = final_message.content if isinstance(final_message.content, str) else str(final_message.content)
            if stream:
                yield {"type": "final_replace", "text": final_content}
            break
    else:
        logger.warning(
            "Agent 触达 max_iterations{}, project={}, session={}, topic={}, max_iterations={}",
            " (stream)" if stream else "",
            project_id,
            session_id,
            topic_thread_id,
            agent_config.max_iterations,
        )
        final_content = _max_iterations_text()
        if stream:
            yield {"type": "final_replace", "text": final_content}

    if not last_messages:
        yield {"type": "_agent_error", "text": "Agent 未能生成有效回复，请重试。"}
        return

    final_message = last_messages[-1]
    raw_final = (
        final_message.content if isinstance(final_message.content, str) else str(final_message.content)
    )
    if not final_content:
        final_content = raw_final

    if raw_final.strip() == "Sorry, need more steps to process this request.":
        logger.warning(
            "Agent 触达 recursion_limit{}, project={}, session={}, topic={}, max_iterations={}",
            " (stream)" if stream else "",
            project_id,
            session_id,
            topic_thread_id,
            agent_config.max_iterations,
        )
        final_content = _max_iterations_text()
        if stream:
            yield {"type": "final_replace", "text": final_content}

    record_trace_event(
        trace_id=setup.trace_id,
        event_type="final_answer",
        project_id=project_id,
        session_id=session_id,
        topic_thread_id=topic_thread_id,
        payload={"content": final_content, "stream": stream, "tool_timeout_count": tool_timeout_count},
    )
    yield {
        "type": "_agent_result",
        "final_content": final_content,
        "messages": messages,
    }


async def run_agent(
    user_message: str,
    project_id: str,
    *,
    session_id: Optional[str] = None,
    topic_thread_id: Optional[str] = None,
    user_role: str = "",
) -> str:
    """
    执行一次 Agent 对话。

    Args:
        user_message: 用户的提问内容。
        project_id: 项目 ID，用于加载对应的上下文和工具。
        session_id: 钉钉 session（conversation_id:sender_staff_id）。
        topic_thread_id: 当前议题段 id；与 session_id 同时传入时加载该段历史并落库。

    Returns:
        Agent 的最终回复文本。
    """
    trace_id = uuid.uuid4().hex
    try:
        with llm_observation_context(
            scope="chat",
            project_id=project_id,
            session_id=session_id,
            topic_thread_id=topic_thread_id,
            channel="dingtalk",
            trace_id=trace_id,
        ):
            prepared = await _prepare_agent_run(
                user_message,
                project_id,
                trace_id=trace_id,
                session_id=session_id,
                topic_thread_id=topic_thread_id,
                llm_feature="agent",
                compaction_title="Agent 历史上下文摘要",
                user_role=user_role,
            )
            if prepared.error_text:
                return prepared.error_text
            if prepared.setup is None:
                return "Agent 未能生成有效回复，请重试。"

            final_content = ""
            async for event in _run_agent_core(
                prepared.setup,
                project_id=project_id,
                session_id=session_id,
                topic_thread_id=topic_thread_id,
                stream=False,
            ):
                if event["type"] == "_agent_error":
                    return event["text"]
                if event["type"] == "_agent_result":
                    final_content = event["final_content"]

            _save_agent_turn(
                session_id=session_id,
                topic_thread_id=topic_thread_id,
                project_id=project_id,
                messages=prepared.setup.messages,
                history=prepared.setup.history,
            )
            return final_content or "Agent 未能生成有效回复，请重试。"

    except Exception as e:
        record_trace_event(
            trace_id=trace_id,
            event_type="error",
            project_id=project_id,
            session_id=session_id,
            topic_thread_id=topic_thread_id,
            payload={"where": "run_agent", "error_type": e.__class__.__name__, "error": str(e)},
        )
        logger.error("Agent 执行失败, project={}, error: {}", project_id, e)
        return f"诊断过程中遇到错误：{e}\n\n请联系管理员检查日志。"


async def run_agent_sse_events(
    user_message: str,
    project_id: str,
    *,
    session_id: str,
    topic_thread_id: str,
    llm_provider: str = "",
    attachments: list[dict[str, Any]] | None = None,
    user_role: str = "",
) -> AsyncIterator[dict[str, Any]]:
    """
    以 SSE 友好的事件流运行 Agent：推送文本增量，并在末尾下发 full_text / done。

    与钉钉 run_agent 共用同一套工具、路由与多轮记忆（session + topic_thread_id）。
    """
    visible_text_parts: list[str] = []
    prepared_setup: AgentRunSetup | None = None
    saved = False
    trace_id = uuid.uuid4().hex

    def _persist_stream_turn(*, interrupted: bool) -> None:
        nonlocal saved
        if saved or prepared_setup is None:
            return
        saved = _save_agent_turn(
            session_id=session_id,
            topic_thread_id=topic_thread_id,
            project_id=project_id,
            messages=prepared_setup.messages,
            history=prepared_setup.history,
            interrupted=interrupted,
            visible_text="".join(visible_text_parts),
            stream=True,
        )

    try:
        with llm_observation_context(
            scope="chat",
            project_id=project_id,
            session_id=session_id,
            topic_thread_id=topic_thread_id,
            channel="webchat",
            trace_id=trace_id,
        ):
            yield {"type": "status", "text": "正在匹配项目业务术语..."}
            prepared = await _prepare_agent_run(
                user_message,
                project_id,
                trace_id=trace_id,
                session_id=session_id,
                topic_thread_id=topic_thread_id,
                llm_feature="sse_agent",
                compaction_title="Webchat 历史上下文摘要",
                webchat_clarification=True,
                provider_order=_provider_order_with_fallback(llm_provider),
                attachments=attachments,
                user_role=user_role,
            )
            if prepared.error_text:
                yield {"type": "error_text", "text": prepared.error_text}
                yield {"type": "done", "full_text": ""}
                return

            prepared_setup = prepared.setup
            if prepared_setup is None:
                yield {"type": "error_text", "text": "Agent 未能生成有效回复，请重试。"}
                yield {"type": "done", "full_text": ""}
                return
            if prepared_setup.intent_route is not None:
                yield {
                    "type": "status",
                    "text": _intent_status_text(prepared_setup.intent_route),
                }

            if prepared.clarification is not None:
                text = prepared.clarification["message"]
                yield {
                    "type": "clarification_request",
                    "message": text,
                    "questions": prepared.clarification["questions"],
                    "term_mappings": prepared.clarification["term_mappings"],
                }
                if text:
                    visible_text_parts.append(text)
                    yield {"type": "delta", "text": text}
                _persist_stream_turn(interrupted=False)
                yield {"type": "done", "full_text": text}
                return

            final_content = ""
            # --- checkpoint: 澄清通过，进入执行前保存 ---
            save_agent_checkpoint(
                session_id=session_id,
                topic_thread_id=topic_thread_id,
                project_id=project_id,
                user_message=user_message,
                system_prompt=prepared_setup.messages[0].content if prepared_setup.messages else "",
                intent_route=prepared_setup.intent_route,
                retrieval_context=prepared_setup.retrieval_context,
                llm_feature="sse_agent",
                provider_order=_provider_order_with_fallback(llm_provider),
            )
            async for event in _run_agent_core(
                prepared_setup,
                project_id=project_id,
                session_id=session_id,
                topic_thread_id=topic_thread_id,
                stream=True,
            ):
                event_type = event["type"]
                if event_type == "_agent_error":
                    yield {"type": "error_text", "text": event["text"]}
                    yield {"type": "done", "full_text": ""}
                    return
                if event_type == "_agent_result":
                    final_content = event["final_content"]
                    continue
                if event_type == "delta":
                    visible_text_parts.append(event["text"])
                yield event

            fallback_clarification = _webchat_large_table_clarification(
                project_id=project_id,
                user_message=user_message,
                final_content=final_content,
            )
            if fallback_clarification is not None:
                yield {
                    "type": "clarification_request",
                    "message": fallback_clarification["message"],
                    "questions": fallback_clarification["questions"],
                    "term_mappings": fallback_clarification["term_mappings"],
                }

            _persist_stream_turn(interrupted=False)
            clear_agent_checkpoint(session_id, topic_thread_id)
            yield {"type": "done", "full_text": final_content}

    except asyncio.CancelledError:
        logger.info(
            "Agent 流式请求被中断, project={}, session={}, topic={}",
            project_id,
            session_id,
            topic_thread_id,
        )
        raise
    except Exception as e:
        record_trace_event(
            trace_id=trace_id,
            event_type="error",
            project_id=project_id,
            session_id=session_id,
            topic_thread_id=topic_thread_id,
            payload={"where": "run_agent_sse_events", "error_type": e.__class__.__name__, "error": str(e)},
        )
        logger.error("Agent 流式执行失败, project={}, error: {}", project_id, e)
        yield {"type": "error_text", "text": f"诊断过程中遇到错误：{e}\n\n请联系管理员检查日志。"}
        yield {"type": "done", "full_text": ""}
    finally:
        if not saved:
            _persist_stream_turn(interrupted=True)


async def run_agent_resume_sse_events(
    project_id: str,
    *,
    session_id: str,
    topic_thread_id: str,
    user_role: str = "",
) -> AsyncIterator[dict[str, Any]]:
    """
    /resume 命令入口：从最近的 checkpoint 恢复 Agent 执行。

    跳过意图识别 + 澄清门，直接重建 AgentRunSetup 并进入 _run_agent_core。
    """
    checkpoint = load_agent_checkpoint(session_id, topic_thread_id)
    if checkpoint is None:
        yield {"type": "error_text", "text": "当前议题没有可恢复的执行状态（checkpoint 不存在或已过期）。"}
        yield {"type": "done", "full_text": ""}
        return

    cp_project_id = checkpoint["project_id"]
    cp_user_message = checkpoint["user_message"]
    cp_system_prompt = checkpoint["system_prompt"]
    cp_llm_feature = checkpoint["llm_feature"]
    cp_provider_order = checkpoint["provider_order_json"]
    cp_created_at = checkpoint["created_at"]

    # 清理中断保存的 partial messages
    cleanup_interrupted_messages(session_id, topic_thread_id, cp_created_at)

    # 重建执行环境
    if not registry.is_ready(cp_project_id):
        yield {"type": "error_text", "text": f"项目 {cp_project_id} 未就绪，无法恢复执行。"}
        yield {"type": "done", "full_text": ""}
        return

    llm = create_llm(feature=cp_llm_feature, provider_order=cp_provider_order)
    tools = _build_project_tools(cp_project_id, topic_thread_id=topic_thread_id)
    if not tools:
        yield {"type": "error_text", "text": "恢复失败：项目工具集不可用。"}
        yield {"type": "done", "full_text": ""}
        return

    # 加载干净的 history
    history = load_history(session_id, topic_thread_id)
    messages: list[Any] = [
        SystemMessage(content=cp_system_prompt),
        *history,
        HumanMessage(content=cp_user_message),
    ]

    setup = AgentRunSetup(
        user_message=cp_user_message,
        history=history,
        messages=messages,
        trace_id=uuid.uuid4().hex,
        llm=llm,
        llm_with_tools=llm.bind_tools(tools),
        tool_manager=ToolExecutionManager(
            tools,
            max_concurrency=agent_config.tool_max_concurrency,
            timeout_sec=agent_config.tool_timeout_sec,
            tool_timeout_overrides=_long_running_tool_timeouts(),
        ),
    )

    visible_text_parts: list[str] = []
    saved = False
    trace_id = setup.trace_id

    def _persist_resume_turn(*, interrupted: bool) -> None:
        nonlocal saved
        if saved:
            return
        saved = _save_agent_turn(
            session_id=session_id,
            topic_thread_id=topic_thread_id,
            project_id=cp_project_id,
            messages=setup.messages,
            history=setup.history,
            interrupted=interrupted,
            visible_text="".join(visible_text_parts),
            stream=True,
        )

    try:
        with llm_observation_context(
            scope="chat",
            project_id=cp_project_id,
            session_id=session_id,
            topic_thread_id=topic_thread_id,
            channel="webchat",
            trace_id=trace_id,
        ):
            yield {"type": "status", "text": "正在从 checkpoint 恢复执行..."}

            final_content = ""
            async for event in _run_agent_core(
                setup,
                project_id=cp_project_id,
                session_id=session_id,
                topic_thread_id=topic_thread_id,
                stream=True,
            ):
                event_type = event["type"]
                if event_type == "_agent_error":
                    yield {"type": "error_text", "text": event["text"]}
                    yield {"type": "done", "full_text": ""}
                    return
                if event_type == "_agent_result":
                    final_content = event["final_content"]
                    continue
                if event_type == "delta":
                    visible_text_parts.append(event["text"])
                yield event

            _persist_resume_turn(interrupted=False)
            clear_agent_checkpoint(session_id, topic_thread_id)
            yield {"type": "done", "full_text": final_content}

    except asyncio.CancelledError:
        logger.info(
            "Agent resume 流式请求被中断, project={}, session={}, topic={}",
            cp_project_id, session_id, topic_thread_id,
        )
        raise
    except Exception as e:
        record_trace_event(
            trace_id=trace_id,
            event_type="error",
            project_id=cp_project_id,
            session_id=session_id,
            topic_thread_id=topic_thread_id,
            payload={"where": "run_agent_resume_sse_events", "error_type": e.__class__.__name__, "error": str(e)},
        )
        logger.error("Agent resume 执行失败, project={}, error: {}", cp_project_id, e)
        yield {"type": "error_text", "text": f"恢复执行过程中遇到错误：{e}"}
        yield {"type": "done", "full_text": ""}
    finally:
        if not saved:
            _persist_resume_turn(interrupted=True)
