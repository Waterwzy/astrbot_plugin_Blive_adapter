import asyncio
import copy
import json
import os
import re
import traceback

from astrbot.api import AstrBotConfig, ToolSet, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

from .blive_platform_event import BLivePlatformEvent
from .core.context_parser import ContextParser
from .core.tools.vts_emotion import VTSEmotionTool
from .core.vts_manager import VTSManager

PLUGIN_NAME = "astrbot_plugin_Blive_adapter"
PLUGIN_DATA_DIR = os.path.join(get_astrbot_plugin_data_path(), PLUGIN_NAME)
LIVE_EVENTS_JSON = os.path.join(PLUGIN_DATA_DIR, "live_events.json")
LAST_PROCESSED_FILE = os.path.join(PLUGIN_DATA_DIR, "last_processed.json")

_EMOTION_PROMPT = "**任务**：根据以下的文字，选择最适合文字发送者心情的表情"


def _get_new_events(last_timestamp: float, max_count: int) -> list:
    """Get new events with timestamp > last_timestamp, up to max_count."""
    if not os.path.exists(LIVE_EVENTS_JSON):
        return []

    try:
        with open(LIVE_EVENTS_JSON, encoding="utf-8") as f:
            all_events = json.load(f)
    except Exception as e:
        logger.error(f"[Bilibili] failed to read live_events.json: {e}")
        return []

    # Filter new events (timestamp > last_timestamp)
    new_events = [e for e in all_events if e.get("timestamp", 0) > last_timestamp]

    # Take only the newest max_count events
    new_events.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
    new_events = new_events[:max_count]

    # Reverse to maintain chronological order (oldest first)
    return list(reversed(new_events))


def _get_last_processed_timestamp() -> float:
    """Get the timestamp of the last processed event."""
    if not os.path.exists(LAST_PROCESSED_FILE):
        return 0.0

    try:
        with open(LAST_PROCESSED_FILE, encoding="utf-8") as f:
            data = json.load(f)
            return data.get("last_timestamp", 0.0)
    except Exception as e:
        logger.error(f"[Bilibili] failed to read last_processed.json: {e}")
        return 0.0


def _update_last_processed_timestamp(timestamp: float):
    """Update the timestamp of the last processed event."""
    try:
        with open(LAST_PROCESSED_FILE, "w", encoding="utf-8") as f:
            json.dump({"last_timestamp": timestamp}, f)
    except Exception as e:
        logger.error(f"[Bilibili] failed to write last_processed.json: {e}")


class BiliBiliLiveTool(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        from .Blive_adapter import BilibiliLiveAdapter  # noqa: F401

        self.config = config
        self.skip_config = []
        self._bla_lock = asyncio.Lock()

    async def initialize(self):
        """可选择实现异步的插件初始化方法，当实例化该插件类之后会自动调用该方法。"""
        for items in self.config["live_context_config"]["skip_config"]:
            if items["__template_key"] != "skip_template":
                continue
            if items["method"] == "regex":
                try:
                    self.skip_config.append(
                        {"method": "regex", "content": re.compile(items["content"])}
                    )
                except re.error:
                    logger.warning(
                        f"BiliBili直播适配器正则表达式编译失败，堆栈信息：\n{traceback.format_exc()}"
                    )
                    continue
            else:
                self.skip_config.append(
                    {"method": items["method"], "content": items["content"]}
                )

    def _is_skip(self, target: str) -> bool:
        for items in self.skip_config:
            if items["method"] == "full" and items["content"] == "target":
                return True
            if items["method"] == "prefix" and target.startswith(items["content"]):
                return True
            if items["method"] == "suffix" and target.endswith(items["content"]):
                return True
            if items["method"] == "regex" and items["content"].fullmatch(target):
                return True
        return False

    @filter.on_llm_request()
    async def inject_live_context(self, event: AstrMessageEvent, req: ProviderRequest):
        """Inject recent live room events into LLM context before sending."""
        sender_plat = event.platform_meta.name
        if sender_plat != "bilibili":
            return
        async with self._bla_lock:
            max_count = self.config.get("context_events_count", 10)
            if max_count <= 0:
                return

            # Get timestamp of last processed event
            last_timestamp = _get_last_processed_timestamp()

            # Get new events since last processing
            new_events = _get_new_events(last_timestamp, max_count)
            if not new_events:
                return

            # Update last processed timestamp to the newest event
            newest_timestamp = max(e.get("timestamp", 0) for e in new_events)
            _update_last_processed_timestamp(newest_timestamp)

            # Format events for LLM context
            events_text = "\n".join(e.get("content", "") for e in new_events)
            live_context = f"<直播间动态>\n{events_text}\n</直播间动态>"

            # Put danmu at top, events below
            for i in range(len(req.contexts) - 1, -1, -1):
                if req.contexts[i].get("role") == "user":
                    original = req.contexts[i].get("content", "")
                    req.contexts[i]["content"] = f"{original}\n\n{live_context}"
                    break

    @filter.on_llm_request()
    async def check_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """这是一个检查用户输入的函数
        Args:
            event(AstrMessageEvent):AstrBot消息事件
            req(ProviderRequest):AstrBot事件的llm请求详细信息
        """
        msg_str = event.get_message_str()
        sender_plat = event.platform_meta.name
        if sender_plat != "bilibili":
            return

        if (
            isinstance(event, BLivePlatformEvent)
            and event.is_emoji
            and self.config["live_context_config"]["skip_emoji"]
        ):
            event.stop_event()
            return

        if self._is_skip(msg_str):
            event.stop_event()
            return

        # 检查是否开启审核模型
        if not self.config["filter_config"]["filter_open"]:
            return
        system_prompt = (
            await self.context.persona_manager.get_persona(
                self.config["filter_config"]["filter_prompt"]
            )
        ).system_prompt
        # logger.debug(f"原始请求体：{req.contexts}")
        context_str = ContextParser(copy.deepcopy(req.contexts)).parse_context(
            self.config["filter_config"]["filter_roles"]
        )
        logger.debug(f"解析结果：\n{context_str}")
        if self.config["filter_config"]["filter_roles"] != 0:
            filter_content = (
                f"用户之前的输入内容：\n{context_str}\n最近一轮用户输入:{msg_str}"
            )
        else:
            filter_content = f"最近一轮用户输入:{msg_str}"
        msg = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": filter_content,
            },
        ]
        # logger.warning(f"获取personl类：{system_prompt}")
        try:
            filter_res = await self.context.llm_generate(
                chat_provider_id=self.config["filter_config"]["filter_provider"],
                contexts=msg,
            )
            if self.config["filter_config"]["filter_mode"]:
                if (
                    self.config["filter_config"]["filter_allow"]
                    in filter_res.completion_text
                ):
                    return
            else:
                if (
                    self.config["filter_config"]["filter_block"]
                    not in filter_res.completion_text
                ):
                    return
        except Exception:
            error_msg = traceback.format_exc()
            logger.error(error_msg)
            return
        # 这里就是stage1没通过的消息
        # chain = MessageChain().message(f"审核模型拒绝！")
        event.stop_event()

    @filter.after_message_sent()
    async def send_vts_emotion(self, event: AstrMessageEvent):
        """发送VTS情感"""
        if not self.config["vts_config"]["is_open"]:
            return
        VTS_manager = VTSManager()
        emotion_str = str(await VTS_manager.get_emotions())
        result = event.get_result()
        if not result or not result.chain:
            return
        logger.debug(f"文字内容{result.chain}")
        await self.context.tool_loop_agent(
            prompt=_EMOTION_PROMPT
            + emotion_str
            + f"\n文字内容：{result.get_plain_text(with_other_comps_mark=True)}",
            event=event,
            max_steps=1,
            tools=ToolSet([VTSEmotionTool()]),
            chat_provider_id=self.config["vts_config"]["provider"],
        )

    async def terminate(self):
        """可选择实现异步的插件销毁方法，当插件被卸载/停用时会调用。"""
