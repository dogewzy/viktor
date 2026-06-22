"""网页控制台接口：在钉钉之外提供浏览器内对话与流式输出。"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncIterator

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from core.agent_loop import run_agent_resume_sse_events, run_agent_sse_events
from core.auth import CurrentUser, get_current_user
from core.chat_commands import (
    RepoScriptCommand,
    match_magic_command,
    normalize_user_command_text,    parse_report_bug_command,
    parse_repo_script_command,
    parse_week_report_username,
)
from core.file_service import upload_user_file
from core.issue_intake_service import submit_local_agent_issue
from gitlab.week_report_command import run_week_report_command
from settings import llm_config
from tools.repo_debug_runner import run_repo_debug_script

router = APIRouter(prefix="/api/v1/ui", tags=["网页控制台"])

DEFAULT_WEBCHAT_PROVIDER = llm_config.default or next(iter(llm_config.providers.keys()), "")
ALLOWED_WEBCHAT_PROVIDERS = set(llm_config.providers.keys())
SSE_HEARTBEAT_INTERVAL_SEC = 15


class FileAttachment(BaseModel):
    file_id: str = ""
    filename: str = ""
    size: int = 0
    content_type: str = ""
    oss_uri: str = ""
    object_key: str = ""
    download_url: str = ""
    extract_status: str = ""
    extracted_text: str = ""
    extracted_preview: str = ""
    truncated: bool = False


class ChatStreamRequest(BaseModel):
    project_id: str = Field(..., min_length=1, description="目标项目 ID")
    message: str = Field(..., min_length=1, description="用户消息")
    session_id: str = Field(
        ...,
        min_length=4,
        description="浏览器会话键，建议 web:<uuid>，用于多轮记忆",
    )
    topic_thread_id: str = Field(
        ...,
        min_length=8,
        description="议题段 ID；新开对话时在客户端生成新的 32 位 hex",
    )
    llm_provider: str = Field(default=DEFAULT_WEBCHAT_PROVIDER, description="网页聊天模型 provider id")
    attachments: list[FileAttachment] = Field(default_factory=list, description="本轮消息已上传附件")


async def _sse_json_lines(events: AsyncIterator[dict[str, Any]]) -> AsyncIterator[str]:
    queue: asyncio.Queue[tuple[str, dict[str, Any] | Exception | None]] = asyncio.Queue()

    async def _produce() -> None:
        try:
            async for event in events:
                await queue.put(("event", event))
        except Exception as e:  # noqa: BLE001
            await queue.put(("error", e))
        finally:
            await queue.put(("done", None))

    producer = asyncio.create_task(_produce())
    try:
        while True:
            try:
                kind, payload = await asyncio.wait_for(
                    queue.get(),
                    timeout=SSE_HEARTBEAT_INTERVAL_SEC,
                )
            except asyncio.TimeoutError:
                heartbeat = json.dumps({"type": "heartbeat", "active_tools": 0}, ensure_ascii=False)
                yield f"data: {heartbeat}\n\n"
                continue
            if kind == "done":
                break
            if kind == "error":
                err = json.dumps({"type": "error", "message": str(payload)}, ensure_ascii=False)
                yield f"data: {err}\n\n"
                continue
            event = payload
            line = json.dumps(event, ensure_ascii=False)
            yield f"data: {line}\n\n"
    except Exception as e:  # noqa: BLE001
        err = json.dumps({"type": "error", "message": str(e)}, ensure_ascii=False)
        yield f"data: {err}\n\n"
    finally:
        if not producer.done():
            producer.cancel()
            try:
                await producer
            except asyncio.CancelledError:
                pass
    yield "data: [DONE]\n\n"


async def _week_report_sse_events(
    username: str | None,
    *,
    project_id: str,
    thread_id: str,
) -> AsyncIterator[dict[str, Any]]:
    if username:
        yield {"type": "status", "text": "正在汇总最近七天提交统计并生成 HTML 报告，请稍候..."}
    try:
        result = await run_week_report_command(
            username,
            project_id=project_id,
            thread_id=thread_id,
        )
        text = result.body
    except ValueError as e:
        text = str(e)
    except Exception as e:  # noqa: BLE001
        text = f"GitLab 统计失败：{e}"
    yield {"type": "delta", "text": text}
    yield {"type": "done", "full_text": text}


async def _single_error_sse_events(text: str) -> AsyncIterator[dict[str, Any]]:
    yield {"type": "error_text", "text": text}
    yield {"type": "done", "full_text": text}


async def _single_text_sse_events(text: str, *, is_error: bool = False) -> AsyncIterator[dict[str, Any]]:
    if is_error:
        yield {"type": "error_text", "text": text}
    else:
        yield {"type": "delta", "text": text}
    yield {"type": "done", "full_text": text}


def _bug_title(description: str) -> str:
    for line in description.splitlines():
        cleaned = line.strip().lstrip("#").strip()
        if cleaned:
            return cleaned[:120]
    return "网页聊天上报 Bug"


async def _report_bug_sse_events(
    *,
    project_id: str,
    command_description: str,
    repo_connector_id: str,
    target_branch: str,
    reporter: str,
    reporter_mobile: str,
    attachments: list[FileAttachment],
    create_coding_task: bool,
) -> AsyncIterator[dict[str, Any]]:
    yield {"type": "status", "text": "正在创建 GitLab Bug issue 并交给 Viktor Intake..."}
    issue_attachments = [item.model_dump() for item in attachments]
    try:
        link = await asyncio.to_thread(
            submit_local_agent_issue,
            project_id=project_id,
            submit_token="",
            kind="bug",
            title=_bug_title(command_description),
            description=command_description,
            repo_connector_id=repo_connector_id,
            target_branch=target_branch,
            reporter_display_name=reporter,
            reporter_mobile=reporter_mobile,
            labels=[],
            attachments=issue_attachments,
            source="web_chat",
            require_token=False,
            create_coding_task=create_coding_task,
        )
        text = (
            "**Bug 已提交到 GitLab Issue Intake**\n\n"
            f"- Issue: {link.get('issue_url') or '-'}\n"
            f"- Viktor tracking ID: `{link.get('coding_task_id') or link.get('link_id')}`\n"
            f"- 状态: `{link.get('status')}`\n"
            f"- Coding Task: `{link.get('coding_task_id') or '-'}`\n\n"
            "后续 MR 创建和合并状态会通过钉钉通知。"
        )
    except ValueError as e:
        text = f"Bug 上报失败：{e}"
        yield {"type": "error_text", "text": text}
        yield {"type": "done", "full_text": text}
        return
    except Exception as e:  # noqa: BLE001
        text = f"Bug 上报失败：{e}"
        yield {"type": "error_text", "text": text}
        yield {"type": "done", "full_text": text}
        return
    yield {"type": "delta", "text": text}
    yield {"type": "done", "full_text": text}


def _extract_json_summary(stdout: str) -> dict[str, Any] | None:
    text = (stdout or "").strip()
    if not text:
        return None
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else None
    except Exception:
        pass

    start = text.rfind("\n{")
    if start >= 0:
        candidate = text[start + 1:]
    else:
        start = text.find("{")
        candidate = text[start:] if start >= 0 else ""
    if not candidate:
        return None
    try:
        value = json.loads(candidate)
        return value if isinstance(value, dict) else None
    except Exception:
        return None


def _format_repo_script_result(command: RepoScriptCommand, result: dict[str, Any]) -> str:
    exit_code = result.get("exit_code")
    stdout = str(result.get("stdout") or "")
    stderr = str(result.get("stderr") or "")
    error = str(result.get("error") or "")
    summary = _extract_json_summary(stdout)

    ok = not error and exit_code == 0
    lines = [f"**{command.name}执行{'完成' if ok else '失败'}**", ""]
    lines.append(f"- sample_id：{', '.join(str(i) for i in command.sample_ids)}")
    lines.append(f"- 脚本：`{command.script_path}`")
    lines.append(f"- 退出码：`{exit_code}`")

    if summary:
        lines.append(f"- 总数：{summary.get('total', '-')}")
        lines.append(f"- 成功：{summary.get('success', '-')}")
        lines.append(f"- 失败：{summary.get('failed', '-')}")
        failed_items = [
            item for item in summary.get("items", [])
            if isinstance(item, dict) and item.get("error")
        ]
        if failed_items:
            lines.extend(["", "**失败样本**"])
            for item in failed_items[:20]:
                lines.append(f"- `{item.get('sample_id')}`：{item.get('error')}")
    elif error:
        lines.extend(["", f"错误：{error}"])

    preview_parts = []
    if stdout and not summary:
        preview_parts.append(f"stdout:\n{stdout[-3000:]}")
    if stderr:
        preview_parts.append(f"stderr:\n{stderr[-3000:]}")
    if preview_parts:
        lines.extend(["", "```text", "\n\n".join(preview_parts), "```"])

    return "\n".join(lines).strip()


async def _repo_script_command_sse_events(
    command: RepoScriptCommand,
    *,
    project_id: str,
) -> AsyncIterator[dict[str, Any]]:
    if project_id != "order-service":
        text = "该快捷命令只支持在 `order-service` 项目下执行，请先切换项目。"
        yield {"type": "error_text", "text": text}
        yield {"type": "done", "full_text": text}
        return

    args = [*command.execute_args, *[str(sample_id) for sample_id in command.sample_ids]]
    tool_input = {
        "repo_connector_id": command.repo_connector_id,
        "script_path": command.script_path,
        "args": args,
    }
    yield {"type": "status", "text": f"正在执行快捷命令：{command.name}..."}
    yield {
        "type": "tool_start",
        "seq": 1,
        "tool": "run_repo_debug_script",
        "input": json.dumps(tool_input, ensure_ascii=False),
    }
    result = await asyncio.to_thread(
        run_repo_debug_script,
        project_id,
        command.script_path,
        args,
        300,
        20000,
        None,
        command.repo_connector_id,
    )
    output_preview = ""
    if result.get("error"):
        output_preview = str(result.get("error"))
    else:
        stdout = str(result.get("stdout") or "")
        stderr = str(result.get("stderr") or "")
        output_preview = (stdout or stderr)[-1200:]

    ok = not result.get("error") and result.get("exit_code") == 0
    yield {
        "type": "tool_end",
        "seq": 1,
        "tool": "run_repo_debug_script",
        "ok": ok,
        "output_preview": output_preview,
    }
    text = _format_repo_script_result(command, result)
    yield {"type": "delta", "text": text}
    yield {"type": "done", "full_text": text}


@router.post("/chat/stream")
async def chat_stream(
    body: ChatStreamRequest,
    user: CurrentUser = Depends(get_current_user),
) -> StreamingResponse:
    """Server-Sent Events：流式输出 LLM/Agent 文本增量，结束前下发 done 事件。"""
    project_id = body.project_id.strip()
    message = body.message.strip()
    session_id = body.session_id.strip()
    topic_thread_id = body.topic_thread_id.strip()
    llm_provider = body.llm_provider.strip() or DEFAULT_WEBCHAT_PROVIDER
    if not project_id or not message or not session_id or not topic_thread_id:
        raise HTTPException(status_code=400, detail="project_id / message / session_id / topic_thread_id 均不能为空")
    if llm_provider not in ALLOWED_WEBCHAT_PROVIDERS:
        supported = " / ".join(sorted(ALLOWED_WEBCHAT_PROVIDERS))
        raise HTTPException(status_code=400, detail=f"llm_provider 仅支持 {supported}")

    normalized_message = normalize_user_command_text(message)

    # /resume 命令：从 checkpoint 恢复执行
    if match_magic_command(normalized_message) == "resume":
        events = run_agent_resume_sse_events(
            project_id,
            session_id=session_id,
            topic_thread_id=topic_thread_id,
            user_role=user.role,
        )
        return StreamingResponse(
            _sse_json_lines(events),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    report_bug_command, report_bug_error = parse_report_bug_command(normalized_message)
    if report_bug_error:
        return StreamingResponse(
            _sse_json_lines(_single_error_sse_events(report_bug_error)),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
    if report_bug_command is not None:
        return StreamingResponse(
            _sse_json_lines(
                _report_bug_sse_events(
                    project_id=project_id,
                    command_description=report_bug_command.description,
                    repo_connector_id=report_bug_command.repo_connector_id,
                    target_branch=report_bug_command.target_branch,
                    reporter=user.display_name or user.username,
                    reporter_mobile=user.mobile,
                    attachments=body.attachments,
                    create_coding_task=not report_bug_command.no_coding,
                )
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
    is_week_report, week_report_username = parse_week_report_username(normalized_message)
    if is_week_report:
        return StreamingResponse(
            _sse_json_lines(
                _week_report_sse_events(
                    week_report_username,
                    project_id=project_id,
                    thread_id=topic_thread_id,
                )
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    repo_script_command, repo_script_error = parse_repo_script_command(message)
    if repo_script_error:
        return StreamingResponse(
            _sse_json_lines(
                _single_text_sse_events(repo_script_error, is_error=True)
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
    if repo_script_command is not None:
        return StreamingResponse(
            _sse_json_lines(
                _repo_script_command_sse_events(
                    repo_script_command,
                    project_id=project_id,
                )
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    events = run_agent_sse_events(
        message,
        project_id,
        session_id=session_id,
        topic_thread_id=topic_thread_id,
        llm_provider=llm_provider,
        attachments=[item.model_dump() for item in body.attachments],
        user_role=user.role,  # role 取自登录用户，忽略请求体任何自报角色
    )
    return StreamingResponse(
        _sse_json_lines(events),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/files/upload")
async def upload_chat_file(
    project_id: str = Form(...),
    session_id: str = Form(...),  # noqa: ARG001 - preserved for request shape/audit parity
    topic_thread_id: str = Form(...),
    file: UploadFile = File(...),
) -> dict[str, Any]:
    """上传网页对话附件到 OSS，并尽量提取可读内容供 Agent 使用。"""
    project_id = project_id.strip()
    topic_thread_id = topic_thread_id.strip()
    if not project_id or not topic_thread_id:
        raise HTTPException(status_code=400, detail="project_id / topic_thread_id 不能为空")
    try:
        data = await file.read()
        return upload_user_file(
            project_id=project_id,
            topic_thread_id=topic_thread_id,
            filename=file.filename or "upload",
            content_type=file.content_type or "",
            data=data,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"文件上传失败：{e}") from e
