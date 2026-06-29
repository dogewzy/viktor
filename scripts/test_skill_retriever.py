"""验证 skill 渐进式披露：BM25 路由 + 混合两层渲染 + expand_skill 工具。

用 viktor/.venv 实跑：./.venv/bin/python scripts/test_skill_retriever.py
不依赖 DB：直接往内存 registry 注册一个临时 project + 几条 skill。
"""
from core.registry import (
    ProjectItem,
    SkillItem,
    SkillTriggerExample,
    registry,
)
from core.intent import retrieve_skills
from core.prompt_builder import _build_skills_block
from core.agent_loop import _build_project_tools, _expand_skill

PID = "_skill_test_proj"


def _skill(sid, name, desc, triggers, steps):
    return SkillItem(
        id=sid,
        project_id=PID,
        name=name,
        description=desc,
        trigger_examples=[SkillTriggerExample(text=t) for t in triggers],
        instructions=steps,
        status="enabled",
    )


def setup():
    registry.register_project(ProjectItem(id=PID, name="技能测试项目", description="临时"))
    registry.register_skill(_skill(
        "sk_overpublish", "超发命中量统计",
        "统计超发样本命中量的标准流程",
        ["统计今天超发命中量", "超发样本有多少"],
        ["锁定 orders 表", "按 hide_flag 过滤", "COUNT(*) 聚合"],
    ))
    registry.register_skill(_skill(
        "sk_parser_fail", "解析失败排查",
        "parser 下载失败的排查方法",
        ["视频解析失败怎么查", "1102 报错"],
        ["查 runtime context", "查 parser 日志"],
    ))
    registry.register_skill(_skill(
        "sk_frame", "抽帧异常排查",
        "抽帧服务异常排查",
        ["抽帧没出图", "2103 错误"],
        ["查 OSS", "查 frameextract 日志"],
    ))


def main():
    setup()

    # 1. BM25 路由：触发样例整句命中的 skill 应排第一
    scored = retrieve_skills(PID, "帮我统计今天超发命中量")
    assert scored, "retrieve_skills 应有命中"
    top, top_score = scored[0]
    assert top.id == "sk_overpublish", f"期望 sk_overpublish 排第一，实际 {top.id}"
    print(f"[1] BM25 路由 OK：top={top.id} score={top_score}，命中 {len(scored)} 条")

    # 2a. 单命中：只全量展开第一名，不出现索引区/expand_skill 提示
    block, expanded_ids = _build_skills_block(PID, "帮我统计今天超发命中量")
    assert expanded_ids == ["sk_overpublish"], expanded_ids
    assert "执行步骤" in block, "第一名应全量展开含执行步骤"
    assert "expand_skill" not in block, "单命中时无其它 skill，不应出现 expand_skill 提示"
    print(f"[2a] 单命中全量展开 OK：展开={expanded_ids}，block {len(block)} 字符")

    # 2b. 多命中：恰好展开 _SKILL_FULL_EXPAND 条，其余只给索引行 + expand_skill 提示
    block2, expanded_ids2 = _build_skills_block(PID, "超发命中量怎么统计，另外视频解析失败和抽帧没出图怎么查")
    assert len(expanded_ids2) == 1, f"应只全量展开 1 条，实际 {expanded_ids2}"
    assert "执行步骤" in block2, "展开项应含执行步骤"
    assert "expand_skill" in block2, "多命中应提示用 expand_skill 拉取其它 skill"
    # 三条全部命中：1 条展开 + 2 条索引，索引项的 id 都应出现
    for sid in ("sk_overpublish", "sk_parser_fail", "sk_frame"):
        assert sid in block2, f"{sid} 应出现在 block（展开或索引）"
    print(f"[2b] 多命中混合两层 OK：展开={expanded_ids2}，block {len(block2)} 字符")

    # 3. expand_skill 工具：存在 + 能拉完整步骤 + 错误 id 友好提示
    tools = _build_project_tools(PID)
    names = {t.name for t in tools}
    assert "expand_skill" in names, f"工具应含 expand_skill，实际 {names}"
    full = _expand_skill(PID, "sk_parser_fail")
    assert "查 parser 日志" in full, "expand_skill 应返回完整步骤"
    bad = _expand_skill(PID, "sk_not_exist")
    assert "未找到" in bad, bad
    print("[3] expand_skill 工具 OK")

    print("\n全部通过 ✅")


if __name__ == "__main__":
    main()
