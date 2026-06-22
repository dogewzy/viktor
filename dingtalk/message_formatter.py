"""
钉钉消息格式化器。

将 Agent 输出的 Markdown 内容适配为钉钉支持的 Markdown 子集。
钉钉 Markdown 限制：
- 不支持表格
- 不支持代码块语法高亮
- 不支持内嵌 HTML
- 消息体上限约 2 万字符
"""
import re

MAX_DINGTALK_LENGTH = 18000


def format_for_dingtalk(content: str) -> str:
    """
    将标准 Markdown 适配为钉钉 Markdown。

    主要处理：
    - 将 Markdown 表格转为列表格式
    - 截断超长内容
    """
    result = _convert_tables_to_list(content)
    result = _truncate(result)
    return result


def _convert_tables_to_list(text: str) -> str:
    """将 Markdown 表格转为列表格式（钉钉不支持表格渲染）。"""
    lines = text.split("\n")
    output = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if "|" in line and i + 1 < len(lines) and re.match(r"^\s*\|[\s\-:|]+\|\s*$", lines[i + 1]):
            headers = [cell.strip() for cell in line.strip("|").split("|")]
            i += 2
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                values = [cell.strip() for cell in lines[i].strip("|").split("|")]
                row_parts = []
                for h, v in zip(headers, values):
                    if v:
                        row_parts.append(f"**{h}**: {v}")
                output.append("- " + " | ".join(row_parts))
                i += 1
        else:
            output.append(line)
            i += 1
    return "\n".join(output)


def _truncate(text: str) -> str:
    """截断超长内容。"""
    if len(text) <= MAX_DINGTALK_LENGTH:
        return text
    return text[:MAX_DINGTALK_LENGTH] + "\n\n... （内容过长，已截断）"
