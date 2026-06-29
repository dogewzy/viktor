"""钉钉自定义机器人通知工具。"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import time
import urllib.parse
from typing import Any

import httpx
from loguru import logger

# 重试参数：最多 MAX_RETRIES 次重试（即最多 MAX_RETRIES+1 次尝试）。
MAX_RETRIES = 3
# 钉钉限流相关 errcode（非 HTTP 层，errcode!=0 时由 validate_dingtalk_response 抛出）。
DINGTALK_RATELIMIT_ERRCODES = {130101, 130102, 130103}


def build_signed_url(webhook_url: str, sign_secret: str = "") -> str:
    """如果配置了签名密钥，则在 URL 上追加 timestamp + sign 参数。"""
    if not sign_secret:
        return webhook_url
    timestamp = str(int(time.time() * 1000))
    string_to_sign = f"{timestamp}\n{sign_secret}"
    hmac_code = hmac.new(
        sign_secret.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        digestmod=hashlib.sha256,
    ).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(hmac_code).decode("utf-8"))
    sep = "&" if "?" in webhook_url else "?"
    return f"{webhook_url}{sep}timestamp={timestamp}&sign={sign}"


def build_at_block(at_mobiles: list[str] | None = None, *, at_all: bool = False) -> dict[str, Any]:
    if at_all:
        return {"isAtAll": True}
    mobiles = [str(item).strip() for item in (at_mobiles or []) if str(item).strip()]
    if mobiles:
        return {"atMobiles": list(dict.fromkeys(mobiles)), "isAtAll": False}
    return {"isAtAll": False}


def _inject_at_mentions(text: str, at_mobiles: list[str] | None, at_all: bool) -> str:
    """钉钉 markdown：仅传 atMobiles 只发提醒红点，正文里要出现 `@手机号` 才会显示并高亮 @。

    把 atMobiles 中正文尚未出现的号码以 `@号码` 追加到末尾，确保被 @ 的人真正看到。"""
    if at_all:
        return text
    mobiles = [str(item).strip() for item in (at_mobiles or []) if str(item).strip()]
    missing = [m for m in dict.fromkeys(mobiles) if m not in text]
    if not missing:
        return text
    mentions = " ".join(f"@{m}" for m in missing)
    return f"{text}\n\n{mentions}" if text else mentions


def build_markdown_payload(
    *,
    title: str,
    text: str,
    at_mobiles: list[str] | None = None,
    at_all: bool = False,
) -> dict[str, Any]:
    return {
        "msgtype": "markdown",
        "markdown": {"title": title, "text": _inject_at_mentions(text, at_mobiles, at_all)},
        "at": build_at_block(at_mobiles, at_all=at_all),
    }


class DingTalkResponseError(RuntimeError):
    """钉钉返回 errcode!=0。errcode 用于判断是否可重试（限流码可重试）。"""

    def __init__(self, errcode: int, errmsg: str) -> None:
        self.errcode = errcode
        self.errmsg = errmsg
        super().__init__(f"DingTalk webhook error: errcode={errcode} errmsg={errmsg}")


def validate_dingtalk_response(body: Any) -> None:
    if not isinstance(body, dict):
        return
    errcode = body.get("errcode", 0)
    if errcode != 0:
        raise DingTalkResponseError(int(errcode), str(body.get("errmsg")))


def _is_retryable_http_status(status_code: int) -> bool:
    """429（限流）或 5xx（服务端错误）可重试。"""
    return status_code == 429 or 500 <= status_code < 600


def _backoff_seconds(attempt: int) -> float:
    """指数退避：attempt 从 1 起 → 1s / 2s / 4s。

    subagent 环境可能禁用 random，故不引入随机 jitter；用 attempt 序号派生固定退避。
    """
    return float(2 ** (attempt - 1))


def _classify_error(exc: Exception) -> tuple[bool, float | None]:
    """判断异常是否可重试，并给出建议等待秒数（None 表示用默认退避）。

    返回 (retryable, retry_after_seconds)。
    """
    # 网络层错误（连接失败 / 超时）→ 可重试。
    if isinstance(exc, (httpx.TimeoutException, httpx.RequestError)):
        return True, None
    # HTTP 状态错误：429 或 5xx 可重试，429 读 Retry-After。
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if not _is_retryable_http_status(status):
            return False, None
        retry_after = None
        if status == 429:
            raw = exc.response.headers.get("Retry-After")
            if raw:
                try:
                    retry_after = float(raw)
                except (TypeError, ValueError):
                    retry_after = None
        return True, retry_after
    # 钉钉业务错误：仅限流码可重试，签名错 / 被禁言等不可重试。
    if isinstance(exc, DingTalkResponseError):
        return exc.errcode in DINGTALK_RATELIMIT_ERRCODES, None
    return False, None


def _build_dlq_payload(
    *,
    title: str,
    text: str,
    sign_secret: str,
    at_mobiles: list[str] | None,
    at_all: bool,
    timeout: float,
) -> dict[str, Any]:
    """落 DLQ 的完整参数，可由 redrive 还原成 send 调用。"""
    return {
        "title": title,
        "text": text,
        "sign_secret": sign_secret,
        "at_mobiles": list(at_mobiles or []),
        "at_all": bool(at_all),
        "timeout": float(timeout),
    }


def _write_dlq(webhook_url: str, payload: dict[str, Any], last_error: str) -> None:
    """同步写一条 pending 死信。失败仅记日志，不让写 DLQ 的异常掩盖原始错误。"""
    from core.database import SessionLocal
    from core.models import NotificationDLQModel

    db = SessionLocal()
    try:
        db.add(
            NotificationDLQModel(
                target=webhook_url or "",
                payload=payload,
                last_error=last_error or "",
                retry_count=0,
                status="pending",
            )
        )
        db.commit()
    except Exception as e:  # noqa: BLE001
        logger.error("[dingtalk] 写 DLQ 失败 target={}: {}", webhook_url, e)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
    finally:
        db.close()


async def send_dingtalk_markdown(
    *,
    webhook_url: str,
    title: str,
    text: str,
    sign_secret: str = "",
    at_mobiles: list[str] | None = None,
    at_all: bool = False,
    timeout: float = 15.0,
) -> None:
    """发送钉钉 markdown（异步）。

    带重试：429/5xx/网络错误/钉钉限流码可重试，指数退避；其它（签名错等）不可重试。
    彻底失败（重试耗尽 / 不可重试）→ 写一条 pending DLQ 后 re-raise，便于调用方区分成败。
    """
    url = build_signed_url(webhook_url, sign_secret)
    payload = build_markdown_payload(title=title, text=text, at_mobiles=at_mobiles, at_all=at_all)
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 2):  # 1 次首发 + MAX_RETRIES 次重试
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
                validate_dingtalk_response(resp.json())
            return
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            retryable, retry_after = _classify_error(exc)
            if not retryable or attempt > MAX_RETRIES:
                break
            wait = retry_after if retry_after is not None else _backoff_seconds(attempt)
            logger.warning(
                "[dingtalk] 发送失败将重试 attempt={}/{} wait={}s: {}",
                attempt, MAX_RETRIES, wait, exc,
            )
            await asyncio.sleep(wait)
    # 彻底失败：异步环境下用线程写 DB，避免阻塞事件循环。
    dlq_payload = _build_dlq_payload(
        title=title, text=text, sign_secret=sign_secret,
        at_mobiles=at_mobiles, at_all=at_all, timeout=timeout,
    )
    await asyncio.to_thread(_write_dlq, webhook_url, dlq_payload, str(last_exc))
    assert last_exc is not None
    raise last_exc


def _send_dingtalk_markdown_sync_raw(
    *,
    webhook_url: str,
    title: str,
    text: str,
    sign_secret: str = "",
    at_mobiles: list[str] | None = None,
    at_all: bool = False,
    timeout: float = 15.0,
) -> None:
    """同步发送 + 重试，但**不写 DLQ**（彻底失败直接 raise）。

    供 redrive 复用：redrive 要在既有 DLQ 行上原地更新，不能再插新行。
    """
    url = build_signed_url(webhook_url, sign_secret)
    payload = build_markdown_payload(title=title, text=text, at_mobiles=at_mobiles, at_all=at_all)
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 2):
        try:
            with httpx.Client(timeout=timeout) as client:
                resp = client.post(url, json=payload)
                resp.raise_for_status()
                validate_dingtalk_response(resp.json())
            return
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            retryable, retry_after = _classify_error(exc)
            if not retryable or attempt > MAX_RETRIES:
                break
            wait = retry_after if retry_after is not None else _backoff_seconds(attempt)
            logger.warning(
                "[dingtalk] 发送失败将重试 attempt={}/{} wait={}s: {}",
                attempt, MAX_RETRIES, wait, exc,
            )
            time.sleep(wait)
    assert last_exc is not None
    raise last_exc


def send_dingtalk_markdown_sync(
    *,
    webhook_url: str,
    title: str,
    text: str,
    sign_secret: str = "",
    at_mobiles: list[str] | None = None,
    at_all: bool = False,
    timeout: float = 15.0,
) -> None:
    """发送钉钉 markdown（同步）。语义同 async 版：彻底失败入 DLQ 后 re-raise。"""
    try:
        _send_dingtalk_markdown_sync_raw(
            webhook_url=webhook_url, title=title, text=text, sign_secret=sign_secret,
            at_mobiles=at_mobiles, at_all=at_all, timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001
        dlq_payload = _build_dlq_payload(
            title=title, text=text, sign_secret=sign_secret,
            at_mobiles=at_mobiles, at_all=at_all, timeout=timeout,
        )
        _write_dlq(webhook_url, dlq_payload, str(exc))
        raise


def redrive_notification_dlq(max_retry: int = 5, batch: int = 50) -> dict:
    """周期重发 pending 死信。供调度器调用。

    - 取 status=='pending' 的死信（按 created_at，limit batch）逐条重发；
    - 重发走 _send_dingtalk_markdown_sync_raw（绕过自动入 DLQ），避免制造重复 DLQ 行；
    - 成功 → status='sent'；
    - 失败 → 在**当前行**上 retry_count+1、记 last_error；若 retry_count>=max_retry → status='dead'
      并发一条元告警（仅 logger.error，绝不走会再入 DLQ 的发送路径，避免死循环）。
    返回统计 {"checked":n,"sent":n,"dead":n}。
    """
    from core.database import SessionLocal
    from core.models import NotificationDLQModel

    stats = {"checked": 0, "sent": 0, "dead": 0}
    db = SessionLocal()
    try:
        rows = (
            db.query(NotificationDLQModel)
            .filter(NotificationDLQModel.status == "pending")
            .order_by(NotificationDLQModel.created_at.asc())
            .limit(batch)
            .all()
        )
        for row in rows:
            stats["checked"] += 1
            payload = dict(row.payload or {})
            try:
                _send_dingtalk_markdown_sync_raw(
                    webhook_url=row.target or "",
                    title=str(payload.get("title") or ""),
                    text=str(payload.get("text") or ""),
                    sign_secret=str(payload.get("sign_secret") or ""),
                    at_mobiles=list(payload.get("at_mobiles") or []),
                    at_all=bool(payload.get("at_all")),
                    timeout=float(payload.get("timeout") or 15.0),
                )
                row.status = "sent"
                stats["sent"] += 1
            except Exception as exc:  # noqa: BLE001
                row.retry_count = (row.retry_count or 0) + 1
                row.last_error = str(exc)
                if row.retry_count >= max_retry:
                    row.status = "dead"
                    stats["dead"] += 1
                    # 元告警：仅 log，绝不再走入 DLQ 的发送路径，避免死循环。
                    logger.error(
                        "[dingtalk] 通知彻底失败转 dead, id={} target={} retry_count={} last_error={}",
                        row.id, row.target, row.retry_count, row.last_error,
                    )
            db.commit()
    finally:
        db.close()
    return stats
