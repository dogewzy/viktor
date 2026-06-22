"""
超长 Agent 回复 → HTML 报告存储。

钉钉群里 @ 机器人时，若 Agent 回复过长（超过 report.threshold_chars），
不再把全文塞进钉钉消息（钉钉对长 markdown 渲染体验差），而是：

  1. 把完整 markdown 一次性渲染为 HTML 片段，连同摘要写入 viktor_reports；
  2. 钉钉里改发：简述 + 一条短链 `{base_url}/reports/{id}`；
  3. /reports/{id} 路由直接 send 渲染好的 HTML，不再二次渲染。

设计要点：
- id 用 8 位 base32（去掉易混淆字符），URL 友好且足够防遍历；
- 入库即定型，访问性能稳定；
- 过期清理由调用方在启动时触发。
"""
from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta

import markdown as md
from loguru import logger
from sqlalchemy import delete

from core.database import SessionLocal
from core.models import ReportModel
from settings import report_config

# URL 友好的 base32 字母（去掉易混淆的 0/O/1/I/L）
_ID_ALPHABET = "23456789abcdefghijkmnpqrstuvwxyz"
_ID_LEN = 8

_MD_EXTENSIONS = [
    "fenced_code",   # ```code``` 代码块
    "tables",        # markdown 表格
    "toc",           # # 标题自动生成锚点
    "nl2br",         # 单换行也变 <br>，更接近钉钉显示习惯
    "sane_lists",    # 修复有序/无序列表的边界
]


def _new_report_id() -> str:
    """生成 URL 友好的短 id；冲突概率极低，万一冲突由调用方重试。"""
    return "".join(secrets.choice(_ID_ALPHABET) for _ in range(_ID_LEN))


def _extract_summary(text: str, max_chars: int) -> str:
    """从 markdown 提取一段适合钉钉展示的纯文本简述。

    规则：取首个非空段落（按空行切分），剥掉常见 markdown 装饰字符，再按 max_chars 截断。
    若首段过短（小于 80 字），尝试拼接下一段，避免摘要太单薄。
    """
    if not text:
        return ""
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    if not paragraphs:
        return text.strip()[:max_chars]

    summary = paragraphs[0]
    if len(summary) < 80 and len(paragraphs) > 1:
        summary = summary + "\n\n" + paragraphs[1]

    # 剥离 markdown 装饰：标题井号、列表符号、加粗/斜体、行内代码
    summary = re.sub(r"^\s*#{1,6}\s+", "", summary, flags=re.MULTILINE)
    summary = re.sub(r"^\s*[-*+]\s+", "• ", summary, flags=re.MULTILINE)
    summary = re.sub(r"`([^`]+)`", r"\1", summary)
    summary = re.sub(r"\*\*([^*]+)\*\*", r"\1", summary)
    summary = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"\1", summary)
    summary = summary.strip()

    if len(summary) <= max_chars:
        return summary
    # 截断时尽量在标点处断开，更自然
    cut = summary[:max_chars]
    for punct in ("。", "；", "！", "？", "\n", "，", " "):
        idx = cut.rfind(punct)
        if idx >= max_chars * 0.6:
            return cut[: idx + 1].rstrip() + "…"
    return cut.rstrip() + "…"


def _extract_title(text: str) -> str:
    """优先取首个 '# 标题' 作为 title；没有就用首句的前 60 字。"""
    m = re.search(r"^\s*#{1,3}\s+(.+?)\s*$", text, flags=re.MULTILINE)
    if m:
        return m.group(1).strip()[:255]
    first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    return first_line[:60]


def render_markdown(text: str) -> str:
    """把 markdown 文本渲染为 HTML 片段（不带 <html><body> 外壳）。"""
    return md.markdown(text or "", extensions=_MD_EXTENSIONS, output_format="html5")


def _copy_markdown_toolbar() -> str:
    """生成报告页内的一键复制工具条。"""
    return f"""
<div class="report-copy-markdown" data-report-copy-markdown>
  <style>
    .report-copy-markdown {{
      display: flex;
      align-items: center;
      flex-wrap: wrap;
      gap: 10px;
      margin: 0 0 18px;
      padding: 12px 14px;
      border: 1px solid #d0d7de;
      border-radius: 8px;
      background: #f6f8fa;
    }}
    .report-copy-markdown button {{
      appearance: none;
      border: 0;
      border-radius: 6px;
      background: #2563eb;
      color: #fff;
      cursor: pointer;
      font: inherit;
      font-size: 14px;
      line-height: 1;
      padding: 9px 12px;
      white-space: nowrap;
    }}
    .report-copy-markdown button:hover {{ background: #1d4ed8; }}
    .report-copy-markdown button[data-copy-kind="dingtalk"] {{
      background: #0f766e;
    }}
    .report-copy-markdown button[data-copy-kind="dingtalk"]:hover {{
      background: #0d5f59;
    }}
    .report-copy-markdown button:disabled {{ cursor: default; opacity: .72; }}
    .report-copy-markdown .copy-status {{
      color: #656d76;
      font-size: 13px;
      min-width: 7em;
    }}
    .report-copy-markdown textarea {{
      position: absolute;
      left: -9999px;
      top: 0;
      width: 1px;
      height: 1px;
      opacity: 0;
    }}
    @media (prefers-color-scheme: dark) {{
      .report-copy-markdown {{
        border-color: #30363d;
        background: #1e242c;
      }}
      .report-copy-markdown button {{ background: #1f6feb; }}
      .report-copy-markdown button:hover {{ background: #388bfd; }}
      .report-copy-markdown button[data-copy-kind="dingtalk"] {{ background: #238277; }}
      .report-copy-markdown button[data-copy-kind="dingtalk"]:hover {{ background: #2aa596; }}
      .report-copy-markdown .copy-status {{ color: #8b949e; }}
    }}
  </style>
  <button type="button" data-copy-button data-copy-kind="markdown" data-copy-label="Markdown">复制 Markdown</button>
  <button type="button" data-copy-button data-copy-kind="dingtalk" data-copy-label="钉钉文本">复制钉钉文本</button>
  <span class="copy-status" data-copy-status></span>
</div>
<script>
(function () {{
  var script = document.currentScript;
  var root = script && script.previousElementSibling;
  if (!root) return;
  var buttons = root.querySelectorAll("[data-copy-button]");
  var status = root.querySelector("[data-copy-status]");
  if (!buttons.length) return;

  function normalizedText(node) {{
    if (!node) return "";
    var clone = node.cloneNode(true);
    clone.querySelectorAll("a").forEach(function (a) {{
      var label = (a.textContent || "").trim();
      var href = a.getAttribute("href") || "";
      var text = href && (!label || label.toLowerCase() === "link")
        ? href
        : (href ? label + "（" + href + "）" : label);
      a.replaceWith(document.createTextNode(text));
    }});
    return (clone.textContent || "").replace(/\\s+/g, " ").trim();
  }}

  function markdownText(node) {{
    var text = normalizedText(node);
    return text.replace(/\\|/g, "\\\\|");
  }}

  function getContentClone() {{
    var article = root.closest("article");
    if (!article) return null;
    var clone = article.cloneNode(true);
    var toolbar = clone.querySelector("[data-report-copy-markdown]");
    if (toolbar) toolbar.remove();
    return clone;
  }}

  function tableToMarkdown(table) {{
    var rows = Array.prototype.slice.call(table.querySelectorAll("tr"));
    var lines = [];
    rows.forEach(function (row, index) {{
      var cells = Array.prototype.slice.call(row.children).map(markdownText);
      if (!cells.length) return;
      lines.push("| " + cells.join(" | ") + " |");
      if (index === 0) {{
        lines.push("| " + cells.map(function () {{ return "---"; }}).join(" | ") + " |");
      }}
    }});
    return lines.join("\\n");
  }}

  function tableToDingTalk(table) {{
    var rows = Array.prototype.slice.call(table.querySelectorAll("tr"));
    if (!rows.length) return "";
    var headers = Array.prototype.slice.call(rows[0].children).map(normalizedText);
    return rows.slice(1).map(function (row) {{
      var cells = Array.prototype.slice.call(row.children);
      var parts = [];
      cells.forEach(function (cell, index) {{
        var header = headers[index] || "";
        var value = normalizedText(cell);
        if (value) parts.push((header ? header + ": " : "") + value);
      }});
      return parts.length ? "- " + parts.join("；") : "";
    }}).filter(Boolean).join("\\n");
  }}

  function blocksToText(container, kind) {{
    var parts = [];
    Array.prototype.slice.call(container.children).forEach(function (child) {{
      var tag = child.tagName ? child.tagName.toLowerCase() : "";
      if (!tag) return;
      if (/^h[1-6]$/.test(tag)) {{
        var level = Number(tag.slice(1));
        var heading = normalizedText(child);
        if (heading) {{
          parts.push(kind === "markdown" ? "#".repeat(level) + " " + heading : heading);
        }}
      }} else if (tag === "p") {{
        var paragraph = normalizedText(child);
        if (paragraph) parts.push(paragraph);
      }} else if (tag === "ul" || tag === "ol") {{
        var lines = [];
        Array.prototype.slice.call(child.children).forEach(function (li, index) {{
          var text = normalizedText(li);
          if (!text) return;
          lines.push((tag === "ol" ? (index + 1) + ". " : "- ") + text);
        }});
        if (lines.length) parts.push(lines.join("\\n"));
      }} else if (tag === "table") {{
        var tableText = kind === "markdown" ? tableToMarkdown(child) : tableToDingTalk(child);
        if (tableText) parts.push(tableText);
      }} else if (tag === "pre") {{
        var code = child.textContent || "";
        if (code.trim()) {{
          parts.push(kind === "markdown" ? "```\\n" + code.trim() + "\\n```" : code.trim());
        }}
      }} else {{
        var nested = blocksToText(child, kind);
        if (nested) parts.push(nested);
      }}
    }});
    return parts.join("\\n\\n");
  }}

  function buildCopyText(kind) {{
    var content = getContentClone();
    if (!content) return "";
    return blocksToText(content, kind).trim();
  }}

  function setStatus(text) {{
    if (!status) return;
    status.textContent = text;
    if (text) {{
      window.setTimeout(function () {{
        if (status.textContent === text) status.textContent = "";
      }}, 1800);
    }}
  }}

  function fallbackCopy(source, text) {{
    if (!source) {{
      source = document.createElement("textarea");
      source.setAttribute("aria-hidden", "true");
      source.style.position = "fixed";
      source.style.left = "-9999px";
      source.style.top = "0";
      document.body.appendChild(source);
    }}
    source.value = text;
    source.focus();
    source.select();
    source.setSelectionRange(0, source.value.length);
    var ok = document.execCommand("copy");
    if (source.parentNode === document.body) source.remove();
    return ok;
  }}

  buttons.forEach(function (button) {{
    button.addEventListener("click", async function () {{
      var kind = button.getAttribute("data-copy-kind") || "markdown";
      var label = button.getAttribute("data-copy-label") || "";
      var text = buildCopyText(kind);
      if (!text) return;
      button.disabled = true;
      try {{
        if (navigator.clipboard && window.isSecureContext) {{
          await navigator.clipboard.writeText(text);
        }} else if (!fallbackCopy(null, text)) {{
          throw new Error("copy failed");
        }}
        setStatus(label ? "已复制" + label : "已复制");
      }} catch (err) {{
        try {{
          if (fallbackCopy(null, text)) {{
            setStatus(label ? "已复制" + label : "已复制");
          }} else {{
            setStatus("复制失败，请手动选择");
          }}
        }} catch (fallbackErr) {{
          setStatus("复制失败，请手动选择");
        }}
      }} finally {{
        button.disabled = false;
      }}
    }});
  }});
}})();
</script>
"""


def save_report(
    *,
    markdown_text: str,
    project_id: str,
    thread_id: str,
    title: str | None = None,
    copy_markdown: bool = False,
) -> tuple[str, str, str]:
    """渲染并入库一份报告。

    返回 (report_id, summary, title)：
      - report_id 用于拼访问链接：`{base_url}/reports/{report_id}`
      - summary  钉钉消息里展示的简述
      - title    报告页 <title>
    """
    title = title or _extract_title(markdown_text) or "Viktor 诊断报告"
    summary = _extract_summary(markdown_text, report_config.summary_max_chars)
    html_body = render_markdown(markdown_text)
    if copy_markdown:
        html_body = _copy_markdown_toolbar() + html_body

    now = datetime.now()
    expires = now + timedelta(days=report_config.ttl_days)

    db = SessionLocal()
    try:
        # 极小概率主键冲突，最多重试 3 次
        for _ in range(3):
            report_id = _new_report_id()
            try:
                report = ReportModel(
                    id=report_id,
                    thread_id=thread_id,
                    project_id=project_id,
                    title=title,
                    summary=summary,
                    html_body=html_body,
                    created_at=now,
                    expires_at=expires,
                )
                db.add(report)
                db.commit()
                logger.info(
                    "报告已入库: id={}, project={}, html_len={}",
                    report_id, project_id, len(html_body),
                )
                return report_id, summary, title
            except Exception as e:
                db.rollback()
                logger.warning("报告写入失败（可能 id 冲突），将重试: {}", e)
        raise RuntimeError("生成报告 id 连续冲突，超过重试上限")
    finally:
        db.close()


def get_report(report_id: str) -> ReportModel | None:
    """读取一份未过期的报告；不存在或已过期返回 None。"""
    db = SessionLocal()
    try:
        report = db.query(ReportModel).filter_by(id=report_id).first()
        if report is None:
            return None
        if report.expires_at and report.expires_at < datetime.now():
            return None
        return report
    finally:
        db.close()


def cleanup_expired() -> int:
    """删除已过期的报告，返回删除条数。供启动钩子调用。"""
    db = SessionLocal()
    try:
        result = db.execute(
            delete(ReportModel).where(ReportModel.expires_at < datetime.now())
        )
        db.commit()
        deleted = result.rowcount or 0
        if deleted > 0:
            logger.info("清理过期报告: {} 条", deleted)
        return deleted
    except Exception as e:
        db.rollback()
        logger.warning("清理过期报告失败: {}", e)
        return 0
    finally:
        db.close()


def build_report_url(report_id: str) -> str:
    """拼出钉钉里发出去的报告完整 URL。"""
    base = report_config.base_url.rstrip("/")
    return f"{base}/reports/{report_id}"
