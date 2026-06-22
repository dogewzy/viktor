"""健康检查路由。"""
import asyncio

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from loguru import logger

router = APIRouter(tags=["健康检查"])

# Temporal 连接探测超时（秒）：避免就绪检查被慢连接拖死
_TEMPORAL_PROBE_TIMEOUT_SEC = 3.0


@router.get("/health", summary="健康检查")
def health_check() -> dict:
    """进程存活检查（与业务项目注册无关）。K8s livenessProbe 使用。"""
    return {"status": "ok"}


def _check_db() -> dict:
    """DB ping：执行 SELECT 1。关键项，失败必须 503。"""
    from sqlalchemy import text

    from core.database import SessionLocal

    db = SessionLocal()
    try:
        db.execute(text("SELECT 1"))
        return {"ok": True}
    except Exception as e:  # noqa: BLE001 — 探测兜底，任何异常都记为不健康
        logger.warning("就绪检查 DB ping 失败: {}: {}", e.__class__.__name__, e)
        return {"ok": False, "error": e.__class__.__name__}
    finally:
        try:
            db.close()
        except Exception:  # noqa: BLE001
            pass


async def _check_temporal() -> dict:
    """Temporal：仅在 enabled 时探测连接。未启用则 skipped，不影响就绪。"""
    from settings import temporal_config

    if not temporal_config.enabled:
        return {"ok": True, "skipped": True}

    from core.temporal.client import get_temporal_client

    try:
        client = await asyncio.wait_for(
            get_temporal_client(), timeout=_TEMPORAL_PROBE_TIMEOUT_SEC
        )
        # Client.connect 成功即视为可达；连接对象无需显式关闭
        del client
        return {"ok": True, "skipped": False}
    except Exception as e:  # noqa: BLE001
        logger.warning("就绪检查 Temporal 连接失败: {}: {}", e.__class__.__name__, e)
        return {"ok": False, "skipped": False, "error": e.__class__.__name__}


def _check_llm() -> dict:
    """LLM provider：至少一个 provider 非全冷却。关键项。"""
    from core.llm_metrics import provider_health

    try:
        rows = provider_health()
    except Exception as e:  # noqa: BLE001
        logger.warning("就绪检查 LLM provider_health 失败: {}: {}", e.__class__.__name__, e)
        return {"ok": False, "error": e.__class__.__name__}

    if not rows:
        # 没有配置任何 provider：无法判定冷却，降级为非关键（不阻塞就绪）
        return {"ok": True, "note": "no providers configured"}

    # provider_health 行含 status: "cooldown"|"ready"
    available = [r for r in rows if r.get("status") != "cooldown"]
    if available:
        return {"ok": True}
    return {"ok": False, "error": "all providers in cooldown"}


@router.get("/ready", summary="就绪检查")
async def readiness_check() -> JSONResponse:
    """
    HTTP 服务就绪：真实探测关键依赖（DB / Temporal / LLM provider）。
    K8s readinessProbe 应使用本接口；业务是否「可诊断」见 GET /api/v1/register/status。

    任一关键项 ok=false → HTTP 503 且 ready=false。即便探测代码自身抛异常，
    也返回 503 而非 500。
    """
    try:
        db = _check_db()
        temporal = await _check_temporal()
        llm = _check_llm()

        # 关键项：db 与 llm 必须 ok；temporal 仅在未 skipped 时纳入判定
        critical_ok = db.get("ok", False) and llm.get("ok", False)
        if not temporal.get("skipped", False):
            critical_ok = critical_ok and temporal.get("ok", False)

        body = {
            "status": "ok" if critical_ok else "degraded",
            "ready": critical_ok,
            "checks": {"db": db, "temporal": temporal, "llm": llm},
        }
        return JSONResponse(status_code=200 if critical_ok else 503, content=body)
    except Exception as e:  # noqa: BLE001 — 顶层兜底：探测自身崩溃也返回 503
        logger.error("就绪检查整体异常: {}: {}", e.__class__.__name__, e)
        return JSONResponse(
            status_code=503,
            content={
                "status": "degraded",
                "ready": False,
                "checks": {"error": e.__class__.__name__},
            },
        )
