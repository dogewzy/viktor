"""
钉钉 Stream 消息处理模块。

使用钉钉 Stream 模式（长连接）接收群内 @机器人的消息，
根据群绑定的项目加载对应的上下文和工具，调用 Agent Loop 处理后回复。
"""
import re

import dingtalk_stream
from dingtalk_stream import AckMessage
from loguru import logger

from core.agent_loop import run_agent
from core.chat_commands import (
    match_magic_command,
    normalize_user_command_text,
    parse_week_report_username,
    strip_new_topic_prefix,
)
from core.memory import (
    get_latest_topic_thread_id,
    new_topic_thread_id,
    record_topic_switch,
)
from core.registry import registry
from core.report_store import build_report_url, save_report
from dingtalk.message_formatter import format_for_dingtalk
from gitlab.week_report_command import run_week_report_command
from settings import dingtalk_config, report_config

_UNBOUND_GROUP_MARKDOWN = """当前群**尚未绑定**到任何项目，无法进行诊断。

**本群 conversation_id**（复制后调用 bindgroup，无需查 Viktor 日志）：
`{conversation_id}`

**闭环操作建议：**
1. 服务 A 先在 Viktor 上完成 **Project → Context → Connector → Tool** 注册（**不需要**事先知道本群 ID）
2. 再对内网 Viktor 调用 `POST /api/v1/register/bindgroup`，将上面的 `conversation_id` 与 `project_id` 关联
3. 绑定成功后，再次 @ 机器人即可提问

（基址为运维配置的 Viktor HTTP 地址，例如集群内 `http://<service>:<port>`）"""

# /report 前缀：强制生成 HTML 报告，忽略长度阈值。
# 支持中英文、全角斜杠、并允许后接空格 / 全角冒号。
_REPORT_PREFIX_PATTERN = re.compile(r"^\s*[/／](report|报告)\b[\s：:]*", re.IGNORECASE)

_HELP_MARKDOWN = """**Viktor 可用命令**

- `/clear`、`/new`、`清空会话`、`开启新对话` 等：开启**新议题段**（旧对话仍保留在后台，只影响当前上下文）
- 可与正文同条发送，例如：`/clear 今天失败任务有多少`（先切议题再提问）
- `/report 你的问题` ：强制以 HTML 报告形式返回，不看长度阈值（适合需要记录 / 分享的诊断）
- `/week_report <GitLab用户名>` ：生成该用户**最近七天**提交统计 HTML 周报，报告页可一键复制 Markdown（需配置默认 GitLab Token）
- `/help` 或 `帮助` ：显示本帮助

**记忆说明：**
机器人按 `群 + 发问人` 连续议题记忆；同一议题内可追问。`/clear` 开始新议题段。空闲超过 30 分钟当前议题段上下文会失效。"""


def _strip_report_prefix(text: str) -> tuple[bool, str]:
    """识别并剥离 /report 前缀。返回 (是否强制报告, 剥离后的正文)。"""
    if not text:
        return False, text
    m = _REPORT_PREFIX_PATTERN.match(text)
    if not m:
        return False, text
    return True, text[m.end():].strip()


class ViktorBotHandler(dingtalk_stream.ChatbotHandler):
    """处理钉钉群内 @机器人 的消息。"""

    async def process(self, callback: dingtalk_stream.CallbackMessage) -> AckMessage:
        """
        处理收到的聊天消息。

        未绑定群：回复中含 conversation_id，便于业务方完成 bindgroup 闭环。
        已绑定群：先占位「正在分析」，再跑 Agent 并回复诊断结果。
        """
        try:
            incoming_message = dingtalk_stream.ChatbotMessage.from_dict(
                callback.data
            )
            user_message = normalize_user_command_text(
                incoming_message.text.content.strip()
            )
            sender_nick = incoming_message.sender_nick
            sender_staff_id = incoming_message.sender_staff_id or "anonymous"
            conversation_id = incoming_message.conversation_id
            session_id = f"{conversation_id}:{sender_staff_id}"

            logger.info(
                "收到钉钉消息: sender={}, conversation={}, content={}",
                sender_nick,
                conversation_id,
                user_message[:100],
            )

            project_id = registry.get_project_by_conversation(conversation_id)
            if not project_id:
                body = _UNBOUND_GROUP_MARKDOWN.format(conversation_id=conversation_id)
                self.reply_markdown("Viktor · 绑定本群", body, incoming_message)
                return AckMessage.STATUS_OK, "OK"

            if not user_message:
                self.reply_text("请输入你要咨询的问题。", incoming_message)
                return AckMessage.STATUS_OK, "OK"

            cmd = match_magic_command(user_message)
            if cmd == "help":
                self.reply_markdown("Viktor · 帮助", _HELP_MARKDOWN, incoming_message)
                return AckMessage.STATUS_OK, "OK"

            is_week_report, week_report_username = parse_week_report_username(user_message)
            if is_week_report:
                if not week_report_username:
                    result = await run_week_report_command(week_report_username)
                    self.reply_markdown(result.title, result.body, incoming_message)
                    return AckMessage.STATUS_OK, "OK"
                self.reply_markdown(
                    "Viktor · GitLab",
                    "正在汇总最近七天提交统计，请稍候…",
                    incoming_message,
                )
                try:
                    result = await run_week_report_command(
                        week_report_username,
                        project_id=project_id,
                        thread_id=session_id,
                    )
                    self.reply_markdown(
                        result.title,
                        result.body,
                        incoming_message,
                    )
                except ValueError as e:
                    self.reply_markdown(
                        "Viktor · GitLab",
                        str(e),
                        incoming_message,
                    )
                except Exception as e:
                    logger.exception("GitLab 最近七天统计失败: {}", e)
                    self.reply_text(
                        f"GitLab 统计失败：{e}",
                        incoming_message,
                    )
                return AckMessage.STATUS_OK, "OK"

            new_topic, remainder = strip_new_topic_prefix(user_message)
            user_message = remainder.strip()

            if new_topic and not user_message:
                topic_id = new_topic_thread_id()
                record_topic_switch(session_id, topic_id, project_id)
                self.reply_markdown(
                    "Viktor · 新议题",
                    "已开启新议题段，此前对话已保留。请直接提问。",
                    incoming_message,
                )
                logger.info("新开议题(仅指令): session={}, topic={}", session_id, topic_id)
                return AckMessage.STATUS_OK, "OK"

            # /report 前缀：强制生成 HTML 报告，忽略长度阈值
            force_report, user_message = _strip_report_prefix(user_message)
            if force_report and not user_message:
                self.reply_markdown(
                    "Viktor · 用法提示",
                    "`/report` 后面请接你要诊断的问题，例如：`/report 今天到现在为止 订单系统履约任务失败多少个`。",
                    incoming_message,
                )
                return AckMessage.STATUS_OK, "OK"

            if not user_message:
                self.reply_text("请输入你要咨询的问题。", incoming_message)
                return AckMessage.STATUS_OK, "OK"

            if new_topic:
                topic_thread_id = new_topic_thread_id()
            else:
                topic_thread_id = (
                    get_latest_topic_thread_id(session_id) or new_topic_thread_id()
                )

            self.reply_markdown(
                "Viktor 正在分析",
                "正在分析中，请稍候...",
                incoming_message,
            )

            agent_reply = await run_agent(
                user_message,
                project_id,
                session_id=session_id,
                topic_thread_id=topic_thread_id,
            )

            title, body = self._build_reply_message(
                agent_reply=agent_reply,
                project_id=project_id,
                topic_thread_id=topic_thread_id,
                force_report=force_report,
            )
            self.reply_markdown(title, body, incoming_message)

            logger.info("回复完成: sender={}, project={}", sender_nick, project_id)

        except Exception as e:
            logger.error("处理钉钉消息失败, error: {}", e)
            try:
                self.reply_text(
                    f"处理消息时出错：{e}\n请联系管理员。",
                    incoming_message,
                )
            except Exception:
                pass

        return AckMessage.STATUS_OK, "OK"

    def _build_reply_message(
        self,
        *,
        agent_reply: str,
        project_id: str,
        topic_thread_id: str,
        force_report: bool = False,
    ) -> tuple[str, str]:
        """根据回复长度选择钉钉交付策略。

        - force_report=True：无论长度都走 HTML 报告（/report 前缀触发）。
        - 未超阈值：按原逻辑发完整 markdown（仅做钉钉适配）。
        - 超阈值：生成 HTML 报告，钉钉里发 “简述 + 报告链接”。
        - 报告生成失败：降级为原逻辑，避免钉钉里什么都收不到。
        - Agent 错误提示（⚠️ 前缀）：直接短消息返回，绝不入库为报告。
        """
        # 兜底错误提示不入报告库：此类内容短且无分析价值，报告只会污染数据
        if agent_reply.startswith("⚠️ "):
            return "Viktor 诊断结果", format_for_dingtalk(agent_reply)

        if not force_report and len(agent_reply) <= report_config.threshold_chars:
            return "Viktor 诊断结果", format_for_dingtalk(agent_reply)

        try:
            report_id, summary, _title = save_report(
                markdown_text=agent_reply,
                project_id=project_id,
                thread_id=topic_thread_id,
            )
        except Exception as e:
            logger.error("生成 HTML 报告失败，降级为钉钉全文输出: {}", e)
            return "Viktor 诊断结果", format_for_dingtalk(agent_reply)

        url = build_report_url(report_id)
        hint = "已以报告形式返回" if force_report else "内容较长"
        body = (
            f"{summary}\n\n"
            f"---\n\n"
            f"{hint}，点击查看完整报告：[{url}]({url})"
        )
        logger.info(
            "走报告链接: report_id={}, raw_len={}, threshold={}, force={}",
            report_id, len(agent_reply), report_config.threshold_chars, force_report,
        )
        return "Viktor 诊断结果", body


def create_stream_client() -> dingtalk_stream.DingTalkStreamClient:
    """创建钉钉 Stream 客户端。"""
    credential = dingtalk_stream.Credential(
        dingtalk_config.app_key,
        dingtalk_config.app_secret,
    )
    client = dingtalk_stream.DingTalkStreamClient(credential)
    client.register_callback_handler(
        dingtalk_stream.ChatbotMessage.TOPIC,
        ViktorBotHandler(),
    )
    logger.info("钉钉 Stream 客户端已创建")
    return client
