#!/usr/bin/env python3
"""
LLM 调试脚本。

直接运行 Agent 对话，观察 LLM 对特定问题的回答。
支持打印完整的 prompt 和工具调用过程。

用法:
    python scripts/test_llm.py -p <project_id> -Q "你的问题"
    python scripts/test_llm.py -p order-service -Q "查询订单 10001 的状态"
    python scripts/test_llm.py -p order-service -Q "查询订单" --prompt-only  # 只打印 prompt

注意: 必须在虚拟环境中运行!
    source .venv/bin/activate
"""
import argparse
import asyncio
import sys
from pathlib import Path

# 将项目根目录添加到路径
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

# 检查虚拟环境：尝试导入关键依赖
try:
    from loguru import logger
    import langchain_core
except ImportError as e:
    venv_path = project_root / ".venv"
    print("=" * 60)
    print("❌ 错误: 缺少依赖模块!")
    print("=" * 60)
    print()
    print(f"导入错误: {e}")
    print()
    print("当前 Python:", sys.executable)
    print("项目虚拟环境:", venv_path)
    print()
    print("请在项目目录下激活虚拟环境后运行:")
    print(f"    cd {project_root}")
    print(f"    source .venv/bin/activate")
    print(f"    python scripts/test_llm.py -p <project> -Q <question>")
    print()
    print("或者直接使用虚拟环境的 Python:")
    print(f"    {venv_path}/bin/python scripts/test_llm.py -p <project> -Q <question>")
    print("=" * 60)
    sys.exit(1)

# 配置日志格式
logger.remove()
logger.add(
    sys.stderr,
    format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
    level="INFO",
)

from core.agent_loop import run_agent
from core.prompt_builder import build_system_prompt
from core.registry import registry


def print_header(title: str) -> None:
    """打印带分隔线的标题。"""
    print(f"\n{'=' * 60}")
    print(f" {title}")
    print(f"{'=' * 60}")


def print_section(title: str, content: str) -> None:
    """打印带标题的内容区块。"""
    print(f"\n{'─' * 40}")
    print(f"【{title}】")
    print(f"{'─' * 40}")
    print(content)


async def debug_query(
    project_id: str,
    question: str,
    prompt_only: bool = False,
    verbose: bool = False,
) -> None:
    """
    执行调试查询。

    Args:
        project_id: 项目 ID
        question: 用户问题
        prompt_only: 是否只打印 prompt，不执行 Agent
        verbose: 是否打印详细信息
    """
    print_header("Viktor LLM 调试工具")

    # 1. 检查项目是否存在
    project = registry.get_project(project_id)
    if not project:
        print(f"❌ 错误: 项目 '{project_id}' 不存在")
        print(f"\n提示: 请先注册项目")
        print(f"  curl -X POST http://localhost:8080/api/v1/register/project \\")
        print(f"    -H 'Content-Type: application/json' \\")
        print(f"    -d '{{\"id\": \"{project_id}\", \"name\": \"项目名\"}}'")
        return

    print(f"项目 ID: {project_id}")
    print(f"项目名称: {project.name}")

    # 2. 检查项目状态
    is_ready = registry.is_ready(project_id)
    status = registry.get_status()["projects"].get(project_id, {})

    contexts = status.get("contexts", [])
    database_connectors = status.get("database_connectors", [])

    print(f"\n项目状态:")
    print(f"  - 上下文: {len(contexts)} 个 {contexts}")
    print(f"  - 数据库连接器: {len(database_connectors)} 个 {database_connectors}")
    print(f"  - 就绪: {'✅' if is_ready else '❌'}")

    if not is_ready:
        print("\n⚠️ 警告: 项目尚未就绪（至少需要一条业务上下文）")
        print("Agent 可能无法正常工作\n")

    # 3. 打印 System Prompt
    system_prompt = build_system_prompt(project_id)
    print_section("System Prompt", system_prompt)

    if prompt_only:
        print("\n⚙️ --prompt-only 模式，跳过 Agent 执行")
        return

    # 4. 执行 Agent
    print_header("Agent 执行")
    print(f"问题: {question}")
    print("\n⏳ 正在执行，请稍候...\n")

    try:
        response = await run_agent(question, project_id)
        print_section("Agent 回答", response)
    except Exception as e:
        print(f"\n❌ Agent 执行失败: {e}")
        logger.exception("Agent 执行异常")
        return

    print_header("调试完成")


def main() -> None:
    """主函数。"""
    parser = argparse.ArgumentParser(
        description="Viktor LLM 调试工具",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 基本用法
  python scripts/test_llm.py -p order-service -Q "查询订单 10001"

  # 只查看 prompt，不执行
  python scripts/test_llm.py -p order-service -Q "查询订单" --prompt-only

  # 详细输出
  python scripts/test_llm.py -p order-service -Q "查询订单" -v
        """,
    )

    parser.add_argument(
        "-p", "--project",
        required=True,
        help="项目 ID",
    )
    parser.add_argument(
        "-Q", "--question",
        required=True,
        help="要问的问题",
    )
    parser.add_argument(
        "--prompt-only",
        action="store_true",
        help="只打印 prompt，不执行 Agent",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="详细输出",
    )

    args = parser.parse_args()

    # 加载注册数据
    logger.info("加载注册数据...")
    try:
        registry.load_from_db()
    except Exception as e:
        logger.warning("从 DB 加载注册数据失败: {}", e)
        logger.info("使用空注册表继续...")

    # 执行调试
    asyncio.run(debug_query(
        project_id=args.project,
        question=args.question,
        prompt_only=args.prompt_only,
        verbose=args.verbose,
    ))


if __name__ == "__main__":
    main()
