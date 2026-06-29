#!/usr/bin/env python3
"""
DeepSeek 思考模式 × LangChain 兼容性实测。

验证两个问题：
    A. 用 langchain-openai 的 ChatOpenAI 打开 DeepSeek 思考模式，能否拿到 reasoning_content
    B. 多轮 tool call（有 reasoning 的 assistant 消息再次回传）能否跑通，不被 API 返回 400

用法:
    source .venv/bin/activate
    export DEEPSEEK_API_KEY=sk-xxx
    python scripts/test_thinking_mode.py
    python scripts/test_thinking_mode.py --model deepseek-v4-flash
    python scripts/test_thinking_mode.py --disable-thinking   # 对照组：关闭思考

退出码:
    0 = 全部通过
    1 = 至少一个用例失败
"""
from __future__ import annotations

import argparse
import json
import sys
import traceback
from datetime import datetime
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

try:
    from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
    from langchain_core.tools import tool
    from langchain_deepseek import ChatDeepSeek
    from loguru import logger
except ImportError as e:
    print(f"❌ 缺少依赖: {e}")
    print("请在项目根目录执行: source .venv/bin/activate && pip install -r requirements.txt")
    sys.exit(2)

from core.llm_client import _ChatDeepSeekThinking
from settings import llm_config


# ----------- mock 工具 -----------
@tool
def get_date() -> str:
    """返回今天的日期（YYYY-MM-DD）。"""
    return datetime.now().strftime("%Y-%m-%d")


@tool
def get_weather(location: str, date: str) -> str:
    """根据城市和日期查天气。"""
    return f"{location} {date}: Cloudy 7~13°C"


TOOL_MAP = {"get_date": get_date, "get_weather": get_weather}


# ----------- 辅助打印 -----------
def _banner(title: str) -> None:
    print("\n" + "=" * 70)
    print(f" {title}")
    print("=" * 70)


def _show_ai_message(prefix: str, msg: AIMessage) -> None:
    rc = msg.additional_kwargs.get("reasoning_content")
    tc = msg.tool_calls or []
    print(f"[{prefix}] content={msg.content!r}")
    print(f"[{prefix}] reasoning_content={(rc[:120] + '...') if rc and len(rc) > 120 else rc!r}")
    print(f"[{prefix}] tool_calls={[{'name': t['name'], 'args': t['args']} for t in tc]}")


# ----------- 构造 LLM -----------
def build_llm(model: str, enable_thinking: bool, effort: str = "high") -> ChatDeepSeek:
    extra_body = {"thinking": {"type": "enabled" if enable_thinking else "disabled"}}
    kwargs: dict = {
        "model": model,
        "api_key": llm_config.api_key,
        "api_base": llm_config.base_url,
        "max_tokens": 2048,
        "extra_body": extra_body,
    }
    # 思考模式不支持 temperature；非思考模式才传
    if not enable_thinking and llm_config.temperature is not None:
        kwargs["temperature"] = llm_config.temperature
    if enable_thinking:
        kwargs["reasoning_effort"] = effort
    logger.info(
        "构造 LLM: model={}, thinking={}, effort={}",
        model,
        "enabled" if enable_thinking else "disabled",
        effort if enable_thinking else "-",
    )
    # 使用带 reasoning_content 回传补丁的子类
    return _ChatDeepSeekThinking(**kwargs)


# ----------- 用例 A：单轮推理 -----------
def case_single_turn(llm: ChatDeepSeek) -> bool:
    _banner("用例 A：单轮推理，观察 reasoning_content")
    messages = [
        SystemMessage(content="你是一个严谨的助手，请用中文回答。"),
        HumanMessage(content="9.11 和 9.8 哪个大？给出结论和一步推导。"),
    ]
    try:
        resp = llm.invoke(messages)
    except Exception as e:
        print(f"❌ 调用失败: {e}")
        traceback.print_exc()
        return False
    _show_ai_message("A", resp)
    ok = bool(resp.content)
    print(f"结果: {'✅ 通过' if ok else '❌ content 为空'}")
    return ok


# ----------- 用例 B：多轮 tool call -----------
def case_multi_tool_call(llm: ChatDeepSeek) -> bool:
    _banner("用例 B：多轮 tool call（考验 reasoning_content 回传）")
    llm_with_tools = llm.bind_tools([get_date, get_weather])
    messages: list = [
        SystemMessage(content="你是个天气助手。若需要今天的日期请调用 get_date。"),
        HumanMessage(content="明天杭州天气怎么样？"),
    ]
    max_sub_turns = 6
    for step in range(1, max_sub_turns + 1):
        print(f"\n--- sub-turn {step} ---")
        try:
            ai_msg: AIMessage = llm_with_tools.invoke(messages)
        except Exception as e:
            print(f"❌ 第 {step} 次调用失败（这通常意味着思考模式下 reasoning_content 未被回传，API 返回 400）")
            print(f"错误: {e}")
            return False
        _show_ai_message(f"B.{step}", ai_msg)
        # 关键：直接把 AIMessage 原样 append，让 langchain-openai 决定如何序列化
        messages.append(ai_msg)

        if not ai_msg.tool_calls:
            print("✅ 模型给出最终回答，循环结束")
            return True

        # 执行工具
        for call in ai_msg.tool_calls:
            fn = TOOL_MAP.get(call["name"])
            if fn is None:
                print(f"⚠️ 未知工具: {call['name']}")
                return False
            try:
                result = fn.invoke(call["args"])
            except Exception as e:
                result = f"工具执行错误: {e}"
            print(f"[tool] {call['name']}({call['args']}) -> {result}")
            messages.append(
                ToolMessage(content=str(result), tool_call_id=call["id"])
            )

    print("❌ 超过最大子轮次仍未收敛")
    return False


# ----------- 主流程 -----------
def main() -> int:
    parser = argparse.ArgumentParser(description="DeepSeek 思考模式兼容性测试")
    parser.add_argument("--model", default="deepseek-v4-pro",
                        help="模型名，默认 deepseek-v4-pro，可改 deepseek-v4-flash")
    parser.add_argument("--disable-thinking", action="store_true",
                        help="对照组：关闭思考模式")
    parser.add_argument("--effort", default="high", choices=["high", "max"],
                        help="思考强度，默认 high")
    args = parser.parse_args()

    if not llm_config.api_key:
        print("❌ DEEPSEEK_API_KEY 未设置（config.yaml 的 api_key 解析为空）")
        return 2

    enable_thinking = not args.disable_thinking
    llm = build_llm(args.model, enable_thinking, effort=args.effort)

    results = {
        "A. single_turn": case_single_turn(llm),
        "B. multi_tool_call": case_multi_tool_call(llm),
    }

    _banner("汇总")
    for name, ok in results.items():
        print(f"  {name}: {'✅' if ok else '❌'}")
    all_pass = all(results.values())
    print(f"\n总结果: {'✅ 全部通过，思考模式在 LangChain 下可用' if all_pass else '❌ 存在失败，需关闭思考模式或升级适配'}")
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
