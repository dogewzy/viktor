"""
GitLab 上下文自动导入服务。

通过 GitLab API 拉取代码仓库文件，用 LLM 分析提取业务概要和 API 契约，
写入 viktor_contexts 表供人工 review。
"""
import os
import re
import uuid
import threading
from typing import Any, Optional
from urllib.parse import quote_plus, urlparse

import httpx
from loguru import logger
from langchain_core.messages import SystemMessage, HumanMessage

from core.database import SessionLocal
from core.llm_client import create_llm
from core.models import ContextModel, GitLabTaskModel
from core.registry import ContextItem, registry
from settings import gitlab_config, llm_config


# ============================================================
# GitLab API 客户端
# ============================================================

class GitLabClient:
    """通过 GitLab REST API v4 读取仓库文件，无需 git clone。"""

    @staticmethod
    def normalize_base_url(raw: str) -> str:
        """补全 `base_url`：无 `http(s)://` 时默认加 `http://`（内网 GitLab 常见写法）。

        若已带协议则只保留 ``scheme://host[:port]``，丢弃误写的 path，避免拼出错误 API 根。
        """
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

    def __init__(
        self,
        base_url: str,
        private_token: str,
        timeout: float = 30.0,
    ) -> None:
        self._base_url = GitLabClient.normalize_base_url(base_url).rstrip("/")
        self._headers = {"PRIVATE-TOKEN": private_token}
        self._timeout = timeout

    @staticmethod
    def base_url_from_clone_url(repo_url: str) -> Optional[str]:
        """从 http(s) 克隆地址解析 GitLab API 根地址（scheme + host[:port]）。

        git@ 形式无法可靠推断 Web/API 协议与端口，返回 None，由配置决定。
        """
        raw = repo_url.strip()
        if raw.startswith("git@"):
            return None
        parsed = urlparse(raw)
        if parsed.scheme in ("http", "https") and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
        return None

    @staticmethod
    def extract_project_path(repo_url: str) -> str:
        """从各种格式的 GitLab URL 中提取项目路径。

        支持:
          - https://gitlab.example.com/group/project.git
          - https://gitlab.example.com/group/project
          - git@gitlab.example.com:group/project.git
          - group/project
        """
        url = repo_url.strip()
        if url.startswith("git@"):
            url = url.split(":", 1)[1]
        url = re.sub(r"\.git$", "", url)
        url = re.sub(r"^https?://[^/]+/", "", url)
        if "/-/" in url:
            url = url.split("/-/", 1)[0]
        return url.strip("/")

    def _api(self, path: str) -> str:
        return f"{self._base_url}/api/v4{path}"

    def get_json(self, path: str, params: Optional[dict[str, Any]] = None) -> Any:
        """通用 GET JSON。path 以 `/` 开头，不含 `/api/v4` 前缀。"""
        with httpx.Client(headers=self._headers, timeout=self._timeout) as client:
            resp = client.get(self._api(path), params=params or {})
            resp.raise_for_status()
            return resp.json()

    def get_project(self, project_path: str) -> dict:
        """GET /projects/:id，返回项目元数据（含 default_branch 等）。"""
        encoded = quote_plus(project_path)
        with httpx.Client(headers=self._headers, timeout=self._timeout) as client:
            resp = client.get(self._api(f"/projects/{encoded}"))
            resp.raise_for_status()
            return resp.json()

    def get_file_tree(
        self,
        project_path: str,
        ref: str = "master",
        recursive: bool = True,
    ) -> list[dict]:
        """获取仓库完整文件树。"""
        encoded = quote_plus(project_path)
        all_items: list[dict] = []
        page = 1
        per_page = 100

        with httpx.Client(headers=self._headers, timeout=self._timeout) as client:
            while True:
                resp = client.get(
                    self._api(f"/projects/{encoded}/repository/tree"),
                    params={
                        "ref": ref,
                        "recursive": str(recursive).lower(),
                        "per_page": per_page,
                        "page": page,
                    },
                )
                resp.raise_for_status()
                items = resp.json()
                if not items:
                    break
                all_items.extend(items)
                page += 1

        return all_items

    def get_file_content(
        self,
        project_path: str,
        file_path: str,
        ref: str = "master",
    ) -> str:
        """获取单个文件的原始内容。"""
        encoded_project = quote_plus(project_path)
        encoded_file = quote_plus(file_path)

        with httpx.Client(headers=self._headers, timeout=self._timeout) as client:
            resp = client.get(
                self._api(
                    f"/projects/{encoded_project}/repository/files"
                    f"/{encoded_file}/raw"
                ),
                params={"ref": ref},
            )
            resp.raise_for_status()
            return resp.text


# ============================================================
# 文件筛选
# ============================================================

def filter_relevant_files(
    tree: list[dict],
    extensions: list[str],
    exclude_dirs: list[str],
    max_files: int,
    low_priority_dirs: list[str] | None = None,
) -> list[str]:
    """从文件树中筛选出值得分析的代码文件路径。

    hard exclude (exclude_dirs) 的文件完全不出现；
    soft exclude (low_priority_dirs) 的文件降低优先级排到末尾，但不丢弃。
    """
    result: list[str] = []
    exclude_set = {d.lower() for d in exclude_dirs}
    low_set = {d.lower() for d in (low_priority_dirs or [])}

    for item in tree:
        if item.get("type") != "blob":
            continue
        path: str = item["path"]

        parts = path.split("/")
        if any(p.lower() in exclude_set for p in parts[:-1]):
            continue

        _, ext = os.path.splitext(path)
        if ext.lower() not in extensions:
            continue

        result.append(path)

    def _sort_key(p: str) -> tuple[int, int]:
        dirs = p.split("/")[:-1]
        is_low = 1 if any(d.lower() in low_set for d in dirs) else 0
        return (is_low, _file_priority(p))

    result.sort(key=_sort_key)
    return result[:max_files]


def _file_priority(path: str) -> int:
    """按文件路径推断重要性，数字越小越优先。"""
    lower = path.lower()
    high_priority_keywords = [
        "controller", "router", "route", "handler", "api",
        "model", "schema", "entity",
        "service", "usecase",
        "config", "settings",
        "main", "app",
        "proto", "thrift", "graphql",
    ]
    for i, kw in enumerate(high_priority_keywords):
        if kw in lower:
            return i
    return 100


# ============================================================
# LLM 代码分析器
# ============================================================

BUSINESS_SUMMARY_PROMPT = """你是一个资深的软件架构师，擅长快速阅读代码并提炼业务知识。

请根据以下代码内容，用中文撰写一份简洁的**业务概要**，格式为 Markdown，包含：

1. **项目简介**：一句话说明这个项目做什么
2. **技术栈**：使用的编程语言、框架、数据库等
3. **核心业务功能**：列出主要功能模块及其作用
4. **关键领域模型**：核心实体及其关系（如 用户-订单-商品）
5. **核心业务流程**：最重要的 2-3 个业务流程的简要描述

要求：
- 聚焦业务含义，不要复述代码细节
- 内容面向运维诊断场景，帮助排查问题时理解业务背景
- 如果信息不足以确定某个部分，标注"待补充"
- 总字数控制在 500-1500 字
"""

API_CONTRACTS_PROMPT = """你是一个资深的软件架构师，擅长快速阅读代码并提炼 API 接口信息。

请根据以下代码内容，用中文撰写一份**API 接口契约文档**，格式为 Markdown，包含：

对每个接口列出：
- **路径**：HTTP 方法 + URL
- **功能**：一句话描述
- **关键入参**：参数名、类型、含义
- **关键出参/响应**：返回的主要字段
- **错误码**：如果代码中有定义的话

要求：
- 按功能模块分组
- 只列出业务 API，跳过健康检查等基础设施接口
- 内容面向运维诊断场景，帮助理解接口行为以排查问题
- 如果信息不足以确定某个部分，标注"待补充"
"""

DOCUMENTATION_ANALYSIS_PROMPT = """你是一个资深软件架构师，正在为运维诊断助手做项目接入。

请优先依据 README、docs、AGENTS、业务知识等文档内容，提炼这个项目的“官方语义”。输出 Markdown，必须包含：

1. **项目定位**：项目解决什么业务问题，服务哪些调用方
2. **核心业务概念**：列出重要术语、别名、代码关键词
3. **关键流程**：文档中明确描述的主链路/状态流转/异步流程
4. **外部依赖**：数据库、消息队列、缓存、对象存储、第三方服务等
5. **运维诊断关注点**：排障时必须先知道的状态、错误码、队列、表、配置
6. **不确定信息**：文档缺失或互相矛盾的地方

要求：
- 不要编造文档中没有的业务结论
- 每个重要结论尽量标注来源文件路径
- 输出面向后续代码分析，不要写成对用户宣传文案
"""

DIRECTORY_ANALYSIS_PROMPT = """你是一个目录级代码调研 Agent。你的任务是只分析指定目录/模块，不要泛化到整个项目。

请结合“文档初步结论”和当前目录代码，输出 Markdown，必须包含：

1. **目录职责**：这个目录在系统中负责什么
2. **关键文件**：列出最重要的文件及原因
3. **主要入口/接口/任务**：HTTP 路由、消费者、定时任务、CLI、后台任务等
4. **核心数据模型与状态**：实体、表、字段、枚举、状态流转
5. **外部依赖与调用方向**：调用哪些数据库、MQ、Redis、OSS、HTTP 服务等
6. **运维诊断线索**：排查这个模块时应该看什么日志、状态、字段、队列、错误码
7. **风险与待确认**：代码中看不全或需要人工确认的点

要求：
- 只基于给定目录代码和文档结论
- 每条重要结论尽量引用文件路径
- 如果目录只是工具/配置/测试，也要明确说明
- 保持高信息密度，避免长篇铺陈；建议控制在 1200 字以内
- 不要输出 JSON，只输出 Markdown
"""

PROJECT_SYNTHESIS_PROMPT = """你是项目接入负责人，需要合并多个目录调研 Agent 的结果，产出可供 Viktor 运维诊断使用的项目综合上下文。

请基于：
- 项目文件树
- 文档优先分析结果
- 各目录调研结果

输出 Markdown，必须包含：

1. **项目一句话定位**
2. **技术栈与运行形态**
3. **模块职责地图**：按目录/模块说明职责
4. **核心业务流程**：按时序描述主链路，包含异步队列/任务/外部系统
5. **关键业务概念与术语**：中文词、英文/代码关键词、业务含义
6. **数据与状态语义**：核心表/实体/状态字段/错误码/时间字段
7. **运维排障入口**：常见问题应该先看哪些模块、表、队列、日志或接口
8. **已读证据与覆盖范围**：说明本次分析读了哪些文档、哪些目录，哪些目录覆盖不足
9. **待人工确认问题**

要求：
- 不要把目录摘要简单拼接，要消除重复并形成整体视角
- 对不确定内容明确标注“待确认”
- 面向 Viktor system prompt，可直接作为业务上下文候选
- 保持可审核、可注入，全文建议控制在 8000~10000 字以内；优先保留排障相关事实
"""


GLOSSARY_EXTRACTION_PROMPT = """你是一个资深的业务知识工程师。请从以下项目综合分析中，提取结构化的业务术语和知识笔记。

请输出 **严格合法的 JSON**（不要包裹在 markdown 代码块中），格式如下：

{
  "glossary": [
    {
      "id": "term-id",
      "term": "中文业务术语",
      "aliases": ["同义词1", "同义词2"],
      "code_keywords": ["codeSymbol1", "code_symbol_2"],
      "description": "简短业务含义说明（不超过200字）"
    }
  ],
  "knowledge_notes": [
    {
      "id": "note-id",
      "kind": "schema_convention|field_semantics|pitfall|metric_definition",
      "scope": "作用域（如 db-name.table.field 或模块名）",
      "title": "一行摘要",
      "content": "详细规则/说明（markdown）",
      "tags": ["tag1", "tag2"]
    }
  ]
}

要求：
- glossary 提取 5~20 个最重要的业务术语，覆盖核心实体、状态、操作
- knowledge_notes 提取 3~10 条最重要的业务知识，尤其是：
  - schema_convention：库/表/字段命名约定、时区、软删除规则
  - field_semantics：关键字段含义（如 status 各值代表什么）
  - pitfall：容易踩坑的点（如某些字段需联合判断）
  - metric_definition：关键业务指标的口径定义
- id 用小写字母和连字符，简短有意义
- 只提取分析中已明确提到的信息，不要编造
- 如果分析内容不足以提取某类信息，对应数组留空即可
"""


CONNECTOR_CONFIG_ANALYSIS_PROMPT = """你是一个资深运维平台接入工程师。用户公司的项目没有配置中心，数据库、Redis、OSS、MQ、Milvus/Zilliz、HTTP 服务等连接信息会直接放在项目配置文件中。

请只基于用户提供的配置文件内容，提取可供 Viktor 注册审核的连接器候选。输出 **严格合法的 JSON**（不要包裹 markdown 代码块），格式如下：

{
  "database_connectors": [
    {
      "id": "稳定、简短的连接器 id",
      "display_name": "可读名称",
      "type": "mysql",
      "host": "数据库 host",
      "port": 3306,
      "username": "用户名",
      "password": "密码或占位符",
      "database": "库名",
      "readonly": true,
      "charset": "utf8mb4",
      "environment": "配置所属环境，如 prod/local-test",
      "source_file": "来源文件路径",
      "confidence": 0.0,
      "notes": "需要人工确认的事项"
    }
  ],
  "log_connectors": [
    {
      "id": "稳定、简短的连接器 id",
      "sls_project": "业务 SLS project",
      "logstore": "业务 logstore",
      "display_name": "可读名称",
      "description": "用途说明",
      "enabled": true,
      "environment": "配置所属环境",
      "source_file": "来源文件路径",
      "confidence": 0.0,
      "notes": "需要人工确认的事项"
    }
  ],
  "external_connectors": [
    {
      "id": "稳定、简短的连接器 id",
      "connector_type": "redis|object_storage|queue|vector_store|http_service",
      "display_name": "可读名称",
      "description": "用途说明",
      "config": {"非敏感连接参数": "如 host/port/db/endpoint/bucket/uri/base_url"},
      "secrets": {"敏感参数": "如 password/token/access_key_secret"},
      "enabled": true,
      "environment": "配置所属环境",
      "source_file": "来源文件路径",
      "confidence": 0.0,
      "notes": "需要人工确认的事项"
    }
  ]
}

字段映射规则：
- MySQL/PostgreSQL 等数据库输出到 database_connectors；如果配置字段叫 database_name，也映射到 database。
- RabbitMQ/AMQP 输出 connector_type=queue；host/port/management_port/vhost 放 config，username/password 放 secrets。
- Redis 输出 connector_type=redis；host/port/db/key 放 config，password/username 放 secrets。
- OSS/Object Storage 输出 connector_type=object_storage；endpoint/end_point/bucket 放 config.endpoint/config.bucket，access_key/access_key_id/secret_key/access_key_secret 放 secrets。
- Milvus/Zilliz/向量库输出 connector_type=vector_store；uri/db_name/collection_name/cloud_endpoint/cluster_id 放 config，token 放 secrets。
- 内部 HTTP 服务 URL、server_url、*_url、*_server 输出 connector_type=http_service，base_url 放 config。
- 阿里云 SLS 业务 project/logstore 输出到 log_connectors；全局 AK/SK 不要输出成 log connector。

要求：
- 不要编造配置文件中没有的 host、库名、bucket、collection 或 secret。
- 如果一个字段使用 ${ENV:default}，保留原表达式；如能从 default 看出值，可在 notes 里说明。
- id 使用小写字母、数字、短横线，尽量包含环境和连接器名，避免重复。
- secrets 必须和 config 分开；不要把 password/token/secret_key/access_key_secret 放进 config。
- confidence 取 0~1；无法确定用途但结构像连接器时可输出较低 confidence，并在 notes 中说明。
- 没有识别到的类别返回空数组。
"""


class CodeAnalyzer:
    """使用 LLM 分析代码内容，生成结构化上下文。"""

    def __init__(self) -> None:
        self._llm = create_llm(thinking=False, feature="onboarding")

    def analyze_business_summary(self, file_tree_text: str, code_content: str) -> str:
        """提取业务概要。"""
        user_content = (
            f"## 项目文件结构\n```\n{file_tree_text}\n```\n\n"
            f"## 核心代码\n{code_content}"
        )
        messages = [
            SystemMessage(content=BUSINESS_SUMMARY_PROMPT),
            HumanMessage(content=user_content),
        ]
        resp = self._llm.invoke(messages)
        return resp.content

    def analyze_api_contracts(self, code_content: str) -> str:
        """提取 API 契约。"""
        user_content = f"## API 相关代码\n{code_content}"
        messages = [
            SystemMessage(content=API_CONTRACTS_PROMPT),
            HumanMessage(content=user_content),
        ]
        resp = self._llm.invoke(messages)
        return resp.content

    def analyze_documentation(self, docs_content: str, project_description: str = "") -> str:
        """优先分析项目文档，建立官方语义基线。"""
        prior = ""
        if project_description:
            prior = f"## 项目描述（用户提供的先验信息，请优先参考）\n{project_description}\n\n"
        user_content = f"{prior}## 文档内容\n{docs_content}"
        messages = [
            SystemMessage(content=DOCUMENTATION_ANALYSIS_PROMPT),
            HumanMessage(content=user_content),
        ]
        resp = self._llm.invoke(messages)
        return resp.content

    def analyze_directory(self, directory: str, docs_summary: str, code_content: str) -> str:
        """分析单个目录/模块，模拟目录级调研 Agent。"""
        user_content = (
            f"## 目标目录\n{directory}\n\n"
            f"## 文档初步结论\n{docs_summary}\n\n"
            f"## 目录代码\n{code_content}"
        )
        messages = [
            SystemMessage(content=DIRECTORY_ANALYSIS_PROMPT),
            HumanMessage(content=user_content),
        ]
        resp = self._llm.invoke(messages)
        return resp.content

    def synthesize_project_analysis(
        self,
        file_tree_text: str,
        docs_summary: str,
        directory_summaries: str,
        project_description: str = "",
    ) -> str:
        """合并文档分析与目录调研结果，生成项目综合上下文。"""
        prior = ""
        if project_description:
            prior = f"## 项目描述（用户提供的先验信息）\n{project_description}\n\n"
        user_content = (
            f"{prior}"
            f"## 项目文件树\n```\n{file_tree_text}\n```\n\n"
            f"## 文档优先分析结果\n{docs_summary}\n\n"
            f"## 目录调研结果\n{directory_summaries}"
        )
        messages = [
            SystemMessage(content=PROJECT_SYNTHESIS_PROMPT),
            HumanMessage(content=user_content),
        ]
        resp = self._llm.invoke(messages)
        return resp.content

    def extract_glossary_and_notes(self, comprehensive_summary: str, project_description: str = "") -> str:
        """从综合分析中提取结构化术语和知识笔记。返回 JSON 字符串。"""
        prior = ""
        if project_description:
            prior = f"项目描述：{project_description}\n\n"
        user_content = f"{prior}## 项目综合分析\n{comprehensive_summary}"
        messages = [
            SystemMessage(content=GLOSSARY_EXTRACTION_PROMPT),
            HumanMessage(content=user_content),
        ]
        resp = self._llm.invoke(messages)
        return resp.content

    def analyze_connector_configs(self, config_content: str, project_description: str = "") -> str:
        """从用户指定的配置文件内容中提取连接器候选。返回 JSON 字符串。"""
        prior = ""
        if project_description:
            prior = f"项目描述：{project_description}\n\n"
        user_content = f"{prior}## 配置文件内容\n{config_content}"
        messages = [
            SystemMessage(content=CONNECTOR_CONFIG_ANALYSIS_PROMPT),
            HumanMessage(content=user_content),
        ]
        resp = self._llm.invoke(messages)
        return resp.content


# ============================================================
# 分析流程编排
# ============================================================

def _build_file_tree_text(tree: list[dict]) -> str:
    """将文件树转为可读的纯文本表示。"""
    lines = []
    for item in tree:
        if item.get("type") == "blob":
            lines.append(item["path"])
    lines.sort()
    return "\n".join(lines)


def _truncate_for_llm(text: str, max_chars: int = 120_000) -> str:
    """截断过长内容，防止超出 LLM 上下文窗口。"""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n\n... (内容过长，已截断)"


def _collect_code_content(
    client: GitLabClient,
    project_path: str,
    file_paths: list[str],
    ref: str,
    max_file_size_kb: int,
) -> str:
    """批量拉取文件内容并拼接。"""
    parts: list[str] = []
    for fp in file_paths:
        try:
            content = client.get_file_content(project_path, fp, ref=ref)
            if len(content) > max_file_size_kb * 1024:
                content = content[: max_file_size_kb * 1024] + "\n... (文件过大，已截断)"
            parts.append(f"### {fp}\n```\n{content}\n```\n")
        except Exception as e:
            logger.warning("读取文件 {} 失败: {}", fp, e)
    return "\n".join(parts)


def _upsert_context(
    session,
    project_id: str,
    context_id: str,
    content: str,
    priority: int,
) -> None:
    """写入或覆盖一条 context 记录，同时同步内存 Registry。"""
    existing = session.query(ContextModel).filter_by(
        project_id=project_id, context_id=context_id,
    ).first()

    if existing:
        existing.content = content
        existing.priority = priority
    else:
        row = ContextModel(
            project_id=project_id,
            context_id=context_id,
            priority=priority,
            content=content,
        )
        session.add(row)

    session.flush()

    registry.register_context(ContextItem(
        id=context_id,
        project_id=project_id,
        priority=priority,
        content=content,
    ))


def _update_task(session, task_id: str, **kwargs) -> None:
    """更新任务记录的字段。"""
    task = session.query(GitLabTaskModel).filter_by(task_id=task_id).first()
    if task:
        for k, v in kwargs.items():
            setattr(task, k, v)
        session.commit()


def analyze_repo(
    task_id: str,
    project_id: str,
    repo_url: str,
    branch: str = "master",
    gitlab_token: Optional[str] = None,
) -> None:
    """
    执行完整的仓库分析流程。

    该函数在后台线程中运行：
    1. 通过 GitLab API 获取文件树
    2. 筛选关键代码文件
    3. 拉取文件内容
    4. LLM 分析生成业务概要和 API 契约
    5. 写入 context 表
    """
    session = SessionLocal()
    try:
        _update_task(session, task_id, status="running", message="正在连接 GitLab...")

        token = (gitlab_token or gitlab_config.token_for_repo_url(repo_url) or "").strip()
        if not token:
            _update_task(session, task_id, status="failed", message="缺少 GitLab Token")
            return

        if not (llm_config.api_key or "").strip():
            _update_task(
                session,
                task_id,
                status="failed",
                message="未配置 LLM API Key（如环境变量 MOONSHOT_API_KEY 或 config.yaml 中 llm.api_key）",
            )
            return

        derived = GitLabClient.base_url_from_clone_url(repo_url)
        base_url = gitlab_config.resolve_base_url(repo_url)
        logger.info("[GitLab] API base_url={} (from_clone_url={})", base_url, derived is not None)

        client = GitLabClient(
            base_url=base_url,
            private_token=token,
        )
        project_path = GitLabClient.extract_project_path(repo_url)
        logger.info("[GitLab] 开始分析: project={}, repo={}, branch={}", project_id, project_path, branch)

        # 1. 获取文件树
        _update_task(session, task_id, message="正在获取文件树...")
        tree = client.get_file_tree(project_path, ref=branch)
        file_tree_text = _build_file_tree_text(tree)
        logger.info("[GitLab] 文件树获取完成，共 {} 个文件", len(tree))

        # 2. 筛选关键文件
        relevant_files = filter_relevant_files(
            tree,
            extensions=gitlab_config.file_extensions,
            exclude_dirs=gitlab_config.exclude_dirs,
            max_files=gitlab_config.max_total_files,
            low_priority_dirs=gitlab_config.low_priority_dirs,
        )
        if not relevant_files:
            _update_task(session, task_id, status="failed", message="未找到可分析的代码文件")
            return

        logger.info("[GitLab] 筛选出 {} 个关键文件", len(relevant_files))

        # 3. 拉取文件内容
        _update_task(session, task_id, message=f"正在读取 {len(relevant_files)} 个代码文件...")
        all_code = _collect_code_content(
            client, project_path, relevant_files, ref=branch,
            max_file_size_kb=gitlab_config.max_file_size_kb,
        )

        # 4. LLM 分析
        analyzer = CodeAnalyzer()
        contexts_generated: list[str] = []

        # 4a. 业务概要
        _update_task(session, task_id, message="正在分析业务概要...")
        try:
            summary = analyzer.analyze_business_summary(
                _truncate_for_llm(file_tree_text, 10_000),
                _truncate_for_llm(all_code),
            )
            _upsert_context(session, project_id, "auto_business_summary", summary, priority=10)
            contexts_generated.append("auto_business_summary")
            logger.info("[GitLab] 业务概要生成完成")
        except Exception as e:
            logger.error("[GitLab] 业务概要分析失败: {}", e)

        # 4b. API 契约
        _update_task(session, task_id, message="正在分析 API 契约...")
        try:
            contracts = analyzer.analyze_api_contracts(_truncate_for_llm(all_code))
            _upsert_context(session, project_id, "auto_api_contracts", contracts, priority=11)
            contexts_generated.append("auto_api_contracts")
            logger.info("[GitLab] API 契约生成完成")
        except Exception as e:
            logger.error("[GitLab] API 契约分析失败: {}", e)

        # 5. 完成
        if not contexts_generated:
            _update_task(
                session, task_id,
                status="failed",
                message="LLM 分析全部失败",
                contexts_generated=[],
            )
        else:
            _update_task(
                session, task_id,
                status="completed",
                message=f"分析完成，生成了 {len(contexts_generated)} 条上下文",
                contexts_generated=contexts_generated,
            )

        session.commit()
        if contexts_generated:
            logger.info("[GitLab] 任务 {} 成功，上下文: {}", task_id, contexts_generated)
        else:
            logger.warning("[GitLab] 任务 {} 未生成任何上下文（多为 LLM 调用失败）", task_id)

    except Exception as e:
        logger.exception("[GitLab] 任务 {} 异常: {}", task_id, e)
        try:
            _update_task(session, task_id, status="failed", message=str(e))
        except Exception:
            pass
    finally:
        session.close()


def start_analyze_task(
    project_id: str,
    repo_url: str,
    branch: str = "master",
    gitlab_token: Optional[str] = None,
) -> str:
    """创建分析任务并在后台线程中启动，返回 task_id。"""
    task_id = uuid.uuid4().hex[:16]

    session = SessionLocal()
    try:
        task = GitLabTaskModel(
            task_id=task_id,
            project_id=project_id,
            repo_url=repo_url,
            branch=branch,
            status="pending",
            message="任务已创建，等待执行",
            contexts_generated=[],
        )
        session.add(task)
        session.commit()
    finally:
        session.close()

    thread = threading.Thread(
        target=analyze_repo,
        args=(task_id, project_id, repo_url, branch, gitlab_token),
        daemon=True,
        name=f"gitlab-analyze-{task_id}",
    )
    thread.start()

    logger.info("[GitLab] 分析任务已启动: task_id={}, project={}", task_id, project_id)
    return task_id
