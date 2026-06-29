"""
Viktor - 通用运维诊断 Agent。

启动 FastAPI HTTP 服务（注册 API + 健康检查）和钉钉 Stream 长连接。
管理界面由独立仓库 viktor-frontend 对接 /api/v1/admin 等 JSON API。
"""
import threading

import uvicorn
from fastapi import Depends, FastAPI
from loguru import logger

from api.health import router as health_router
from api.auth_routes import router as auth_router
from api.admin_routes import router as admin_api_router
from api.register_routes import router as register_router
from api.chat_routes import router as chat_router
from api.ui_routes import router as ui_router
from api.report_routes import router as report_router
from api.demo_routes import router as demo_router
from api.onboarding_routes import router as onboarding_router
from api.coding_routes import router as coding_router
from api.watchdog_routes import router as watchdog_router
from api.issue_intake_routes import router as issue_intake_router
from api.staging_routes import router as staging_router
from gitlab.routes import router as gitlab_router
from gitlab.webhook_routes import router as gitlab_webhook_router
from core.auth import require_auth
from core.registry import registry
from core.report_store import cleanup_expired as cleanup_expired_reports
from core.repo_warmup import start_background_warmup
from core.issue_intake_scheduler import issue_intake_scheduler
from core.watchdog import watchdog_scheduler
from core.coding_maintenance_scheduler import coding_maintenance_scheduler
from dingtalk.stream_handler import create_stream_client
from settings import dingtalk_config, server_config

app = FastAPI(
    title="Viktor",
    description="通用运维诊断 Agent — 通过注册 API 动态接入任何业务系统",
    version="0.1.0",
)

# 公开路由：健康检查、登录鉴权，以及供公开分享链接只读访问的 chat / report。
app.include_router(health_router)
app.include_router(auth_router)
app.include_router(chat_router)
app.include_router(report_router)
app.include_router(demo_router)
app.include_router(issue_intake_router)
app.include_router(gitlab_webhook_router)

# 受保护路由：需携带有效登录凭证（控制台与网页聊天）。
_protected = [Depends(require_auth)]
app.include_router(admin_api_router, dependencies=_protected)
app.include_router(register_router, dependencies=_protected)
app.include_router(ui_router, dependencies=_protected)
app.include_router(onboarding_router, dependencies=_protected)
app.include_router(coding_router, dependencies=_protected)
app.include_router(watchdog_router, dependencies=_protected)
app.include_router(gitlab_router, dependencies=_protected)
app.include_router(staging_router, dependencies=_protected)


def _start_dingtalk_stream() -> None:
    """在后台线程中启动钉钉 Stream 客户端。"""
    if not dingtalk_config.app_key or not dingtalk_config.app_secret:
        logger.warning("钉钉 appKey/appSecret 未配置，跳过 Stream 客户端启动")
        return

    try:
        client = create_stream_client()
        logger.info("启动钉钉 Stream 客户端...")
        client.start_forever()
    except Exception as e:
        logger.error("钉钉 Stream 客户端启动失败, error: {}", e)


@app.on_event("startup")
async def startup_event() -> None:
    """服务启动时初始化。"""
    logger.info("Viktor 启动中...")
    registry.load_from_db()
    try:
        cleanup_expired_reports()
    except Exception as e:
        logger.warning("起动时清理过期报告失败，忽略: {}", e)
    dingtalk_thread = threading.Thread(
        target=_start_dingtalk_stream,
        daemon=True,
        name="dingtalk-stream",
    )
    dingtalk_thread.start()
    # 启动 Watchdog 调度器
    watchdog_scheduler.start()
    issue_intake_scheduler.start()
    coding_maintenance_scheduler.start()
    # 后台并行预热已注册仓库（clone + venv），避免对话里首次懒加载等待
    try:
        start_background_warmup()
    except Exception as e:  # noqa: BLE001
        logger.warning("启动仓库预热失败，忽略（退回懒加载）: {}", e)
    logger.info("Viktor 启动完成")


@app.on_event("shutdown")
async def shutdown_event() -> None:
    """服务关停时停止后台调度器。"""
    try:
        coding_maintenance_scheduler.shutdown()
    except Exception as e:  # noqa: BLE001
        logger.warning("停止 Coding 自维护调度器失败，忽略: {}", e)
    try:
        watchdog_scheduler.shutdown()
    except Exception as e:  # noqa: BLE001
        logger.warning("停止 Watchdog 调度器失败，忽略: {}", e)


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=server_config.host,
        port=server_config.port,
        workers=1,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
