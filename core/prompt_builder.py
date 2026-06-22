"""
运行时 Prompt 组装器。

根据指定项目的已注册上下文片段动态构建 system prompt，
支持子系统级别路由，让 LLM 更专注于相关上下文。
"""
import json

from langchain_core.messages import HumanMessage
from loguru import logger

from core.llm_client import create_llm
from core.registry import registry
from settings import code_inspection_config

BASE_SYSTEM_PROMPT = """你是 Viktor，一个通用运维诊断助手。

## 规则

1. **信息信任层级**（从高到低）：
   - 本轮 Project Intent / 术语检索结果 = project-scoped glossary/knowledge topK → 优先用于解释用户业务词
   - 业务知识库 field_semantics = 已验证业务事实 → 直接用于 SQL 条件，不需额外工具验证
   - 业务知识库 metric_definition = 权威指标口径 → 必须按其公式写 SQL，不自造定义
   - 业务知识库 schema_convention / pitfall = 约束清单 → 写 SQL 前逐条核对
   - 业务术语表 = 中英映射参考 → 用于推断库表/字段名
   - 工具探索 = 用于知识库未覆盖的表、字段、枚举值
2. 对知识库**未覆盖**的表、字段、枚举值，必须先通过工具拿到证据，不凭空猜测。
3. 多个互不依赖的证据查询应在同一轮并行发起多个 tool call，不串行等待。
4. 禁止任何写操作（INSERT/UPDATE/DELETE）；禁止多语句拼接。
5. 给出明确诊断结论（当前状态、可能原因、关键证据、建议操作），不让用户自己查。
6. 信息不足时主动调用更多工具获取补充，直到能给出结论。
7. 回复使用中文，格式 Markdown。

## 数据库查询工作流

0. 写 SQL 前，先查阅下方「本轮 Project Intent / 术语检索结果」与「业务知识库」：
   - 若 field_semantics 已给出字段枚举/映射 → 直接采用，跳到步骤 3
   - 核对 schema_convention / pitfall（时区、软删除、状态语义）
   - 若有 metric_definition 匹配用户问题 → 按其口径写，不自造指标
   - 若 missing_terms 非空，或中文业务词未被 glossary/knowledge 明确映射，先澄清或用 code_grep/code_read 验证代码语义，再写聚合 SQL
1. 用业务上下文与术语表锁定数据库连接器与候选表名；不确定时 list_database_connectors → list_tables 收敛。
2. 对最终用到的每张表：必须 describe_table 核对字段与索引，必要时 sample_rows 观察形态。
3. 写最终 SQL 前，从索引与过滤条件预估代价；大查询先 explain_sql。
4. 区分意图：用户要精确总数 → execute_sql COUNT(*)；仅你自己摸底 → 不要用 COUNT(*) 当探针。
5. 探索形态 → probe_sql（合理 WHERE + 小 LIMIT）；回答用户问题 → execute_sql。
6. 一旦 SQL 或工具超时，不要无止境重复大范围试错；基于已有证据给出部分结论，或明确向用户索要更窄时间、平台、状态等强过滤条件。（需要实跑代码复现动态逻辑时，按下方「脚本执行与复现」工作流，而不是盲目重试。）

## 运行时与外部证据工作流

- 线上排障先调 list_runtime_contexts；多 cluster 必须明确证据来源，不混淆。
- 用 command / workload_name 判断相关运行单元，log_bindings 选 Log Connector，selector 选 K8s 查询对象。
- 如果 runtime 中只有日志绑定但还没注册 Log Connector，在回答中指出缺口，不假装已查过。
- 样本无结果类按 DB → OSS → Queue → logs 顺序多源收集，不只看单一源。
- 外部连接器（Redis/OSS/Queue/HTTP/DingTalk Doc）均只读；先 list_external_connectors 确认可用。

## Skill 使用原则

1. Skill 是沉淀方法，不是事实源；必须用工具核验证据。
2. 用户问题命中 Skill 触发样例 → 优先按其步骤推进。
3. Skill 的 required_contexts/tools 是规划线索；缺失时在回答中说明缺口。

"""

# Optional project-specific subsystem router hints.
# Keep empty in the open-source tree; private deployments may populate this via
# their own bootstrap code or local patches.
SUBSYSTEM_ROUTER_CONFIG = {}

# 是否启用子系统路由（可通过配置控制）
ENABLE_SUBSYSTEM_ROUTING = True


# 按登录用户角色定制应答风格（仅作用于 Prompt 层，不改变可用工具）。
# key 与 core.auth.ROLES 对齐；空字符串/未知角色不注入（钉钉等旧调用方行为不变）。
ROLE_PROMPTS = {
    "operations": """当前用户是【运营】。应答风格要求：
- 结论先行、口语化，直接给"当前状态 / 原因 / 建议操作"，让运营能照着做。
- **严格使用业务术语表中的标准中文词，全程术语一致**；不要把同一概念在不同段落叫不同名字。
- **不要在回答里展示代码片段、堆栈、SQL、字段级技术细节或英文标识符**；你可以用工具读代码/查库求证，但只把业务结论讲出来。
- 涉及操作时给清晰的步骤；不确定时说明需要哪类同事（开发/产品）介入，而不是丢技术细节给运营。""",
    "product": """当前用户是【产品】。应答风格要求：
- 聚焦业务影响、指标口径、链路与流程；先讲清"发生了什么、影响范围、为什么"。
- 可以出现少量关键技术名词（队列、状态码、表名），但不要展开贴代码或长 SQL。
- 结构化输出（分点 / 表格）；保持业务术语一致，指标按权威口径解释。""",
    "qa": """当前用户是【测试】。应答风格要求：
- 聚焦复现条件、预期/实际结果、影响范围、边界场景与回归风险。
- 输出便于转成测试用例或缺陷单；需要技术细节时控制在定位和验证所必需的范围。""",
    "developer": """当前用户是【开发】。应答风格要求：
- 可以给出 file:line、相关代码片段、堆栈、SQL、技术根因与定位过程，越具体越好。
- 仍要给明确结论与建议，不要只堆证据；指出关键代码路径与可改动点。""",
    "admin": """当前用户是【管理员】。应答风格要求：
- 优先关注项目配置、连接器、权限身份、trace learning、成本与运行观测。
- 给出可审计的系统状态和治理建议，必要时指出哪些数据会影响工作流闭环。""",
}


_CODE_INSPECTION_GUIDE = """
## 代码自省能力（本项目已开启）
当需要了解「当前线上真实运行的代码逻辑」时，直接调用代码搜索工具，而不是仅依赖上面的静态业务上下文（上下文可能滞后）。

工具按成本顺序使用：
code_glob(pattern) → code_grep(pattern, fuzzy=True) → code_read(path, start, end)

搜索策略（重要）：
0. 用户询问业务“概念/口径/如何区分/字段映射”时，先看本轮 glossary/knowledge 命中；只有 missing_terms 或证据不足时，才 code_grep 定义、常量、请求参数、模型字段和定时任务逻辑，再决定 SQL 字段。
1. 调 code_grep 前先列 3-5 个候选关键词：
   - 同义词 + 中文业务词 ↔ 英文代码符号映射（优先查看下面的「业务术语」条目）
   - CamelCase / snake_case / kebab-case 变体
   - 可能的拼写错误
2. 一次性用 '(a|b|c)' 组合成正则调用，ignore_case=true；实在不确定就加 fuzzy=true。
3. 若 0 结果：去掉前缀/后缀放宽再试，换同义词，而不是直接放弃。
4. grep 命中后用 code_read 读定位行前后 50–200 行。前 200 行多为 import，注意从实际命中行读起。
退出条件：回答时明确标注「本分析基于 commit xxx 的代码」；如果仅根据上下文推断，也明确说明「未核对代码」。

## 脚本执行与复现能力（本项目已开启）
只读 SQL / 日志 / 代码无法定论的**动态逻辑**（如算法计算、序列化/反序列化、复杂数据变换、多步状态流转），可以在仓库 workspace 里实跑代码来复核结论。仓库已被预热：依赖一般已由 setup_repo_venv 装好，run_repo_debug_script / run_repo_command 默认 use_venv='auto' 会自动用项目 venv（含项目三方依赖）。

三类场景：
1. **自己写复现脚本**：用 write_repo_debug_file 生成 scripts/test_xxx.py，再 run_repo_debug_script 执行。这是你自己控制内容的只读复现，可直接跑；若 import 项目依赖报 ModuleNotFoundError，先 setup_repo_venv（可用 extra_packages 补缺包）再重试。
2. **调用仓库已有脚本**：很多业务仓库的 scripts/ 下有现成可执行脚本，用户诉求能对上时可调用。**必须先 code_read 读懂这个脚本：它读什么、改什么、有没有写库/调外部/发消息等副作用、需要哪些参数。**
3. **Skill 指路**：当 Skill 明确点名「用仓库某段代码/某脚本」时，按 Skill 走。

⚠️ 安全红线（必须遵守）：
- 你**自己刚写**的只读复现脚本，可以直接运行。
- 运行**仓库里已存在的脚本**、或任何**可能产生副作用**（写数据库 / 调用外部接口 / 发消息 / 触发任务 / 删改数据）的脚本或命令前，**必须先向用户说明：要跑哪个脚本、它做什么、用什么参数、可能的副作用，然后等用户明确确认后再执行**。用户没确认就只读不写、不要执行。
- 默认只做**只读复现 / 验证**，不对生产做写操作；这些脚本不会自动提交。一旦执行超时，按上面的退出条件给部分结论，不要盲目重试。
"""


def _build_glossary_block(project_id: str) -> str:
    """拼装业务术语表（只输出 enabled=True），用于引导 LLM 做 query 扩展。"""
    try:
        items = registry.get_glossaries(project_id, only_enabled=True)
    except Exception:  # noqa: BLE001
        items = []
    if not items:
        return ""
    lines = ["\n## 业务术语表（推断库表/字段名与代码符号时的中英映射参考）"]
    for g in items:
        alias_part = ""
        if g.aliases:
            alias_part = " / " + " / ".join(g.aliases)
        kw_part = ""
        if g.code_keywords:
            kw_part = " → " + ", ".join(g.code_keywords)
        lines.append(f"- {g.term}{alias_part}{kw_part}")
    return "\n".join(lines) + "\n"


def _build_skills_block(project_id: str, user_message: str = "") -> str:
    """拼装项目 Skill candidates，作为方法层注入 prompt。"""
    try:
        items = registry.get_skills(project_id, only_enabled=True)
    except Exception:  # noqa: BLE001
        items = []
    if not items:
        return ""

    scored: list[tuple[int, object]] = []
    msg = (user_message or "").lower()
    for item in items:
        haystacks = [item.name, item.description, item.raw_content]
        haystacks.extend(ex.text for ex in item.trigger_examples)
        score = 0
        for text in haystacks:
            raw = (text or "").lower()
            if raw and raw in msg:
                score += 4
            for token in re_split_tokens(raw):
                if len(token) >= 2 and token in msg:
                    score += 1
        scored.append((score, item))

    scored.sort(key=lambda pair: (pair[0], pair[1].name), reverse=True)
    selected = [item for _, item in scored[:8]]

    lines = ["\n## 项目技能 Skill（方法层：什么时候按什么流程调用上下文和工具）"]
    for skill in selected:
        lines.append(f"\n### {skill.name} ({skill.id}, kind={skill.kind})")
        if skill.description:
            lines.append(f"- 描述：{skill.description}")
        if skill.trigger_examples:
            examples = "；".join(ex.text for ex in skill.trigger_examples[:5] if ex.text)
            if examples:
                lines.append(f"- 触发样例：{examples}")
        if skill.required_contexts:
            ctx = []
            for ref in skill.required_contexts[:10]:
                label = ref.id or ref.name or ref.type
                ctx.append(f"{ref.type}:{label}" + (f"({ref.purpose})" if ref.purpose else ""))
            lines.append(f"- 依赖上下文：{'; '.join(ctx)}")
        if skill.required_tools:
            lines.append(f"- 推荐工具：{', '.join(skill.required_tools[:16])}")
        if skill.instructions:
            lines.append("- 执行步骤：")
            for idx, step in enumerate(skill.instructions[:10], 1):
                lines.append(f"  {idx}. {step}")
        if skill.output_contract:
            lines.append(f"- 输出要求：{json.dumps(skill.output_contract, ensure_ascii=False)}")
    return "\n".join(lines) + "\n"


def re_split_tokens(text: str) -> list[str]:
    import re
    return re.findall(r"[a-zA-Z0-9_]{2,}|[\u4e00-\u9fff]{2,}", text or "")


def _context_id_set(contexts: list) -> set[str]:
    return {str(getattr(ctx, "id", "") or "") for ctx in contexts if getattr(ctx, "id", "")}


def _subsystem_context_ids(subsystem: dict) -> set[str]:
    return {str(cid) for cid in subsystem.get("context_ids", []) if str(cid)}


def _available_subsystems(project_id: str, contexts: list | None = None) -> list[dict]:
    """Return router subsystems that are backed by currently registered contexts.

    The static router config is only a routing hint. The registry is the source
    of truth, so stale hard-coded subsystem entries must not be shown to the LLM
    or accepted from its classification output. A subsystem is usable when it is
    backed by either a current context or a current repository connector.
    """
    config = SUBSYSTEM_ROUTER_CONFIG.get(project_id)
    if not config:
        return []

    project_contexts = registry.get_contexts(project_id) if contexts is None else contexts
    available_context_ids = _context_id_set(project_contexts)
    repo_ids = {repo.id for repo in registry.get_repository_connectors(project_id)}
    if not available_context_ids and not repo_ids:
        return []

    available = []
    for sub in config.get("subsystems", []):
        sub_id = str(sub.get("id") or "")
        context_ids = _subsystem_context_ids(sub)
        if sub_id in repo_ids:
            available.append(sub)
            continue
        if context_ids & available_context_ids:
            available.append(sub)
            continue
        if not context_ids:
            continue
        logger.info(
            "跳过未注册上下文/仓库支撑的子系统路由项: project={}, subsystem={}, context_ids={}",
            project_id,
            sub.get("id"),
            sorted(context_ids),
        )
    return available


def _valid_subsystem_ids(project_id: str, contexts: list | None = None) -> set[str]:
    return {str(sub.get("id")) for sub in _available_subsystems(project_id, contexts) if sub.get("id")}


# Knowledge Note 分节配置：顺序按对 SQL 正确性的影响程度排列，schema_convention 优先级最高
_KNOWLEDGE_SECTIONS: list[tuple[str, str]] = [
    ("schema_convention", "库/表/字段级约定（写 SQL 前必须核对的约束清单）"),
    ("field_semantics", "已验证字段映射与枚举值（可直接用于 SQL 条件，无需工具再验证）"),
    ("pitfall", "常见坑位（反例与正确写法）"),
    ("metric_definition", "权威指标口径（必须按此公式写 SQL）"),
]

# 注入时的约束：每条 content 截断长度 + 每类最多条数，避免 prompt 超长
_NOTE_CONTENT_MAX_CHARS = 500
_NOTES_PER_KIND_CAP = 30


def _build_knowledge_notes_block(project_id: str) -> str:
    """拼装业务知识库（按 kind 分节），仅输出 enabled=True，注入在 Glossary 之前。"""
    try:
        items = registry.get_knowledge_notes(project_id, only_enabled=True)
    except Exception:  # noqa: BLE001
        items = []
    if not items:
        return ""

    by_kind: dict[str, list] = {}
    for it in items:
        by_kind.setdefault(it.kind, []).append(it)

    lines = ["\n## 业务知识库（规则第1条信任层级适用于以下内容）"]
    has_any = False
    for kind, section_title in _KNOWLEDGE_SECTIONS:
        group = by_kind.get(kind) or []
        if not group:
            continue
        has_any = True
        if len(group) > _NOTES_PER_KIND_CAP:
            logger.warning(
                "知识笔记 kind={} 条数 {} 超出注入上限 {}，已截断",
                kind, len(group), _NOTES_PER_KIND_CAP,
            )
            group = group[:_NOTES_PER_KIND_CAP]
        lines.append(f"\n### {section_title}")
        for n in group:
            scope_part = f"[{n.scope}] " if n.scope else ""
            lines.append(f"- {scope_part}{n.title}")
            if n.content:
                body = n.content.strip()
                if len(body) > _NOTE_CONTENT_MAX_CHARS:
                    body = body[:_NOTE_CONTENT_MAX_CHARS] + "…"
                # 内容以缩进子句形式追在标题下，保持 markdown 结构易读
                lines.append(f"  {body}")
    if not has_any:
        return ""
    return "\n".join(lines) + "\n"

# 路由分类的 LLM Prompt
SUBSYSTEM_ROUTER_PROMPT = """你是系统架构分析专家。请分析用户问题，判断涉及哪些子系统。

项目名称：{project_name}
项目描述：{project_description}

可选子系统：
{subsystems_desc}

用户问题：{user_message}

请分析：
1. 这个问题涉及哪些子系统？
2. 是否需要了解系统整体流程？
3. 是否需要跨系统协作？

请严格按以下 JSON 格式输出（不要添加其他说明文字）：
```json
{{
  "subsystems": ["子系统id1", "子系统id2"],
  "include_overview": true/false,
  "reason": "简要说明选择理由"
}}
```
"""


def _get_subsystem_description(project_id: str) -> str:
    """获取子系统描述文本。"""
    subsystems = _available_subsystems(project_id)
    if not subsystems:
        return ""
    
    lines = []
    for sub in subsystems:
        lines.append(f"- {sub['id']}: {sub['name']}")
        lines.append(f"  职责：{sub['description']}")
        lines.append(f"  关键词：{', '.join(sub['keywords'][:5])}...")
    return "\n".join(lines)


def _keyword_based_classify(project_id: str, user_message: str) -> tuple[list[str], bool]:
    """基于关键词的启发式分类（作为LLM分类的备选）。"""
    subsystems = _available_subsystems(project_id)
    if not subsystems:
        return [], True
    
    user_msg_lower = user_message.lower()
    matched_subsystems = set()
    
    for sub in subsystems:
        # 关键词匹配
        for keyword in sub["keywords"]:
            if keyword.lower() in user_msg_lower:
                matched_subsystems.add(sub["id"])
                break
    
    # 如果没有匹配到，默认包含所有
    if not matched_subsystems:
        return [], True
    
    # 判断是否跨系统问题（匹配多个子系统）
    is_cross_system = len(matched_subsystems) > 1
    
    # 单系统问题不包含 overview
    include_overview = is_cross_system
    
    return list(matched_subsystems), include_overview


async def classify_subsystems(project_id: str, user_message: str) -> tuple[list[str], bool]:
    """
    使用 LLM 判断问题涉及哪些子系统。
    
    Returns:
        (子系统ID列表, 是否包含系统概述)
    """
    project = registry.get_project(project_id)
    if not project:
        return [], True
    
    # 如果项目没有配置子系统路由，使用关键词匹配
    if project_id not in SUBSYSTEM_ROUTER_CONFIG:
        return _keyword_based_classify(project_id, user_message)
    
    subsystems_desc = _get_subsystem_description(project_id)
    if not subsystems_desc:
        return [], True
    valid_subsystem_ids = _valid_subsystem_ids(project_id)
    
    prompt = SUBSYSTEM_ROUTER_PROMPT.format(
        project_name=project.name,
        project_description=project.description,
        subsystems_desc=subsystems_desc,
        user_message=user_message,
    )
    
    try:
        llm = create_llm(feature="router")
        response = await llm.ainvoke([HumanMessage(content=prompt)])
        content = response.content.strip()
        
        # 提取 JSON（处理 markdown 代码块）
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0].strip()
        elif "```" in content:
            content = content.split("```")[1].split("```")[0].strip()
        
        result = json.loads(content)
        raw_subsystems = result.get("subsystems", [])
        subsystems = [sub_id for sub_id in raw_subsystems if sub_id in valid_subsystem_ids]
        include_overview = result.get("include_overview", True)
        reason = result.get("reason", "")

        dropped = [sub_id for sub_id in raw_subsystems if sub_id not in valid_subsystem_ids]
        if dropped:
            logger.warning(
                "LLM 返回了未注册/不可用的子系统，已丢弃: project={}, dropped={}, valid={}",
                project_id,
                dropped,
                sorted(valid_subsystem_ids),
            )
        
        logger.info(
            "子系统分类结果: project={}, subsystems={}, overview={}, reason={}",
            project_id, subsystems, include_overview, reason
        )
        
        return subsystems, include_overview
        
    except Exception as e:
        logger.warning("LLM 子系统分类失败，回退到关键词匹配: {}", e)
        return _keyword_based_classify(project_id, user_message)


def _filter_contexts(
    project_id: str,
    contexts: list,
    subsystems: list[str],
    include_overview: bool,
) -> list:
    """根据子系统选择过滤上下文。"""
    if not subsystems:
        # 没有指定子系统，返回所有上下文
        return contexts
    
    valid_subsystems = _available_subsystems(project_id, contexts)
    subsystems_config = {s["id"]: s for s in valid_subsystems}
    
    # 收集需要包含的 context_ids
    target_context_ids = set()
    
    for sub_id in subsystems:
        if sub_id in subsystems_config:
            target_context_ids.update(subsystems_config[sub_id].get("context_ids", []))
    
    # 系统概述上下文
    if include_overview and "order-service" in project_id:
        target_context_ids.add("order-service-overview")
    
    # 过滤上下文
    filtered = []
    for ctx in contexts:
        if ctx.id in target_context_ids:
            filtered.append(ctx)
    
    # 如果过滤后为空，返回所有上下文（安全回退）
    if not filtered:
        logger.warning("子系统过滤后无上下文，回退到全部上下文")
        return contexts
    
    logger.info(
        "上下文过滤: 原始 {} 个 -> 筛选后 {} 个, 使用子系统: {}",
        len(contexts), len(filtered), subsystems
    )
    
    return filtered


async def build_system_prompt(
    project_id: str,
    user_message: str = "",
    enable_routing: bool = True,
    retrieval_context: str | None = None,
    user_role: str = "",
) -> str:
    """
    组装完整的 system prompt。

    Args:
        project_id: 项目 ID
        user_message: 用户问题（用于子系统路由）
        enable_routing: 是否启用子系统路由
        retrieval_context: intent 模块选出的 project-scoped glossary/knowledge topK 证据
    
    Returns:
        组装好的 system prompt
    """
    project = registry.get_project(project_id)
    all_contexts = registry.get_contexts(project_id)

    parts = [BASE_SYSTEM_PROMPT]

    if project and project.description:
        parts.append(f"当前项目：{project.name}\n{project.description}\n")

    if retrieval_context:
        parts.append(retrieval_context)
    else:
        # 兼容 coding/service 等旧调用方；主诊断 Agent 应传入 intent topK，避免全量 prompt 膨胀。
        knowledge_block = _build_knowledge_notes_block(project_id)
        if knowledge_block:
            parts.append(knowledge_block)

        glossary_block = _build_glossary_block(project_id)
        if glossary_block:
            parts.append(glossary_block)

    # 代码自省指南：仅当项目启用时才注入
    if project and project.git_url and code_inspection_config.enabled:
        parts.append(_CODE_INSPECTION_GUIDE)

    skills_block = _build_skills_block(project_id, user_message)
    if skills_block:
        parts.append(skills_block)

    role_block = ROLE_PROMPTS.get(user_role)

    if not all_contexts:
        if role_block:
            parts.append(f"## 当前用户角色与应答风格\n{role_block}")
        return "\n".join(parts)

    # 子系统路由：筛选相关上下文
    if enable_routing and ENABLE_SUBSYSTEM_ROUTING and user_message:
        subsystems, include_overview = await classify_subsystems(project_id, user_message)
        contexts = _filter_contexts(project_id, all_contexts, subsystems, include_overview)
        
        # 添加路由信息到 prompt（帮助 LLM 理解）
        if subsystems:
            parts.append(f"\n[诊断范围] 当前问题主要涉及：{', '.join(subsystems)}\n")
    else:
        contexts = all_contexts

    parts.append("以下是已注册的业务上下文信息：\n")
    for ctx in contexts:
        parts.append(f"--- {ctx.id} ---\n{ctx.content}\n")

    # 角色应答风格放在最后，利用 recency 强化对语气/细节暴露的约束。
    if role_block:
        parts.append(f"## 当前用户角色与应答风格\n{role_block}")

    return "\n".join(parts)
