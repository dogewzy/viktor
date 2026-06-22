"""
报告查看路由：钉钉里贴的 https://viktor.example.com/reports/{id} 由此渲染。

设计：
- 该路由对外公开（无鉴权），靠 8 位随机 id 作为弱凭证 + 30 天 TTL；
- 不依赖 admin 的 layout，自带最小化 HTML 外壳，便于在钉钉移动端 / 公网浏览器都能正常显示。
"""
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from loguru import logger

from core.report_store import get_report

router = APIRouter(tags=["Report"])

_TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"
_jinja = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    autoescape=select_autoescape(["html"]),
)


def _render(template_name: str, **ctx) -> str:
    return _jinja.get_template(template_name).render(**ctx)


@router.get("/reports/{report_id}", response_class=HTMLResponse, summary="查看 Agent 诊断报告")
def view_report(report_id: str) -> HTMLResponse:
    """渲染一份诊断报告。不存在或已过期均返回 404 友好页。"""
    report = get_report(report_id)
    if report is None:
        logger.info("访问不存在/已过期的报告: id={}", report_id)
        html = _render(
            "report_not_found.html",
            report_id=report_id,
        )
        return HTMLResponse(content=html, status_code=404)

    html = _render(
        "report.html",
        title=report.title or "Viktor 诊断报告",
        project_id=report.project_id,
        created_at=report.created_at.strftime("%Y-%m-%d %H:%M:%S") if report.created_at else "",
        expires_at=report.expires_at.strftime("%Y-%m-%d %H:%M:%S") if report.expires_at else "",
        # html_body 是 markdown 渲染产物，需作为安全 HTML 注入；
        # 内容来源于受控的 Agent 输出 + python-markdown，未引入用户原始 HTML。
        html_body=report.html_body,
    )
    return HTMLResponse(content=html)
