import asyncio
import json
import os
import time
from datetime import datetime

from astrbot import logger
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain
from astrbot.api.platform import (
    AstrBotMessage,
    MessageMember,
    MessageType,
    Platform,
    PlatformMetadata,
    register_platform_adapter,
)
from astrbot.core.platform.astr_message_event import MessageSesion
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

from .blive_platform_event import BLivePlatformEvent
from .client import BLiveClient

PLUGIN_NAME = "astrbot_plugin_Blive_adapter"
PLUGIN_DATA_DIR = os.path.join(get_astrbot_plugin_data_path(), PLUGIN_NAME)
EVENTS_FILE = os.path.join(PLUGIN_DATA_DIR, "events.txt")  # Human readable format
LIVE_EVENTS_JSON = os.path.join(
    PLUGIN_DATA_DIR, "live_events.json"
)  # Machine readable format
SENT_MESSAGES_FILE = os.path.join(PLUGIN_DATA_DIR, "sent_messages.json")

os.makedirs(PLUGIN_DATA_DIR, exist_ok=True)


@register_platform_adapter(
    "bilibili",
    "bilibili 直播适配器",
    default_config_tmpl={
        "id_code": "your_id_code",
        "appid": "your_app_id",
        "public_key": "your_access_key",
        "secret_key": "your_access_secret",
        "host": "https://live-open.biliapi.com",
    },
)
class BilibiliLiveAdapter(Platform):
    def __init__(
        self,
        platform_config: dict,
        platform_settings: dict,
        event_queue: asyncio.Queue,
    ) -> None:
        super().__init__(platform_config, event_queue)
        self.settings = platform_settings
        self.client: BLiveClient = None

    async def send_by_session(
        self, session: MessageSesion, message_chain: MessageChain
    ):
        text = ""
        for comp in message_chain.chain:
            if isinstance(comp, Plain):
                text += comp.text

        if text and self.client:
            success = await self.client.send_message(text)
            self._log_sent_message(text, success)

        await super().send_by_session(session, message_chain)

    @staticmethod
    def _log_event(text: str):
        """Synchronously append event to both human-readable and machine-readable files."""
        # Human readable format with timestamp
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        human_line = f"{text}\n"
        with open(EVENTS_FILE, "a", encoding="utf-8") as f:
            f.write(human_line)

        # Machine readable format (JSON)
        event_record = {"timestamp": time.time(), "time_str": now, "content": text}
        try:
            if os.path.exists(LIVE_EVENTS_JSON):
                with open(LIVE_EVENTS_JSON, encoding="utf-8") as f:
                    events = json.load(f)
            else:
                events = []
            events.append(event_record)
            with open(LIVE_EVENTS_JSON, "w", encoding="utf-8") as f:
                json.dump(events, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[Bilibili] failed to write live_events.json: {e}")

    @staticmethod
    def _log_sent_message(text: str, success: bool):
        """Synchronously log sent message to sent_messages.json."""
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        message_record = {
            "timestamp": now,
            "text": text,
            "success": success,
        }

        try:
            if os.path.exists(SENT_MESSAGES_FILE):
                with open(SENT_MESSAGES_FILE, encoding="utf-8") as f:
                    messages = json.load(f)
            else:
                messages = []

            messages.append(message_record)

            with open(SENT_MESSAGES_FILE, "w", encoding="utf-8") as f:
                json.dump(messages, f, ensure_ascii=False, indent=2)

            logger.info(f"[Bilibili] logged sent message: {text}")
        except Exception as e:
            logger.error(f"[Bilibili] failed to log sent message: {e}")

    def meta(self) -> PlatformMetadata:
        id_ = self.config.get("id") or "bilibili"
        return PlatformMetadata(
            name="bilibili",
            description="bilibili 直播适配器",
            id=id_,
        )

    async def terminate(self):
        """Called by PlatformManager when the adapter is being terminated."""
        logger.info("[Bilibili] adapter terminating...")
        if self.client:
            try:
                await self.client.close()
            except Exception as e:
                logger.error(f"[Bilibili] close() error: {e}")
        logger.info("[Bilibili] adapter terminated")

    async def run(self):
        id_code = str(self.config.get("id_code", ""))
        app_id = int(self.config.get("appid", 0))
        access_key = str(self.config.get("public_key", ""))
        access_secret = str(self.config.get("secret_key", ""))
        host = str(self.config.get("host", "https://live-open.biliapi.com"))

        self.client = BLiveClient(
            id_code=id_code,
            app_id=app_id,
            access_key=access_key,
            access_secret=access_secret,
            host=host,
        )

        async def on_received(data: dict):
            logger.info(f"[Bilibili] received: {data}")
            abm = await self.convert_message(data)
            if abm:
                await self.handle_msg(abm)

        self.client.on_message = on_received
        await self.client.run()

    async def convert_message(self, data: dict) -> AstrBotMessage:
        abm = AstrBotMessage()
        abm.raw_message = data
        abm.self_id = self.client.game_id

        # Bilibili open platform sends various message types
        # Common commands: LIVE_OPEN_PLATFORM_DM (danmu), LIVE_OPEN_PLATFORM_SEND_GIFT (gift)
        cmd = data.get("cmd", "")

        if cmd == "LIVE_OPEN_PLATFORM_DM" or cmd == "DANMU_MSG":
            # Danmu message
            dm_data = data.get("data", data)
            user_id = str(dm_data.get("open_id", ""))
            user_name = dm_data.get("uname", dm_data.get("username", ""))
            content = dm_data.get("msg", dm_data.get("content", ""))
            room_id = str(dm_data.get("room_id", self.client.room_id))
            msg_id = dm_data.get("msg_id", "")

            abm.type = MessageType.GROUP_MESSAGE
            abm.group_id = room_id
            abm.message_str = content
            abm.sender = MessageMember(user_id=user_id, nickname=user_name)
            abm.message = [Plain(text=content)]
            abm.session_id = f"{user_id}_{room_id}"
            abm.message_id = msg_id

        elif cmd == "LIVE_OPEN_PLATFORM_SEND_GIFT" or cmd == "SEND_GIFT":
            # Gift message — treat as an event notification
            gift_data = data.get("data", data)
            user_id = str(gift_data.get("open_id", ""))
            user_name = gift_data.get("uname", gift_data.get("username", ""))
            gift_name = gift_data.get("gift_name", "")
            gift_num = gift_data.get("gift_num", 1)
            room_id = str(gift_data.get("room_id", self.client.room_id))
            content = f"[礼物] {user_name} 赠送 {gift_name} x{gift_num}"

            self._log_event(f"礼物 {user_name} 赠送 {gift_name} x{gift_num}")

            abm.type = MessageType.GROUP_MESSAGE
            abm.group_id = room_id
            abm.message_str = content
            abm.sender = MessageMember(user_id=user_id, nickname=user_name)
            abm.message = [Plain(text=content)]
            abm.session_id = f"{user_id}_{room_id}"
            abm.message_id = ""

        elif cmd == "LIVE_OPEN_PLATFORM_SUPER_CHAT" or cmd == "SUPER_CHAT_MESSAGE":
            # Super chat message
            sc_data = data.get("data", data)
            user_id = str(sc_data.get("open_id", ""))
            user_name = sc_data.get("uname", sc_data.get("username", ""))
            content = sc_data.get("message", "")
            price = sc_data.get("price", 0)
            room_id = str(sc_data.get("room_id", self.client.room_id))
            text = f"[SC ¥{price}] {user_name}: {content}"

            abm.type = MessageType.GROUP_MESSAGE
            abm.group_id = room_id
            abm.message_str = text
            abm.sender = MessageMember(user_id=user_id, nickname=user_name)
            abm.message = [Plain(text=text)]
            abm.session_id = f"{user_id}_{room_id}"
            abm.message_id = ""

        elif cmd == "LIVE_OPEN_PLATFORM_GUARD":
            # Guard / 大航海 purchase
            guard_data = data.get("data", data)
            user_id = str(guard_data.get("open_id", ""))
            user_name = guard_data.get("uname", guard_data.get("username", ""))
            guard_level = guard_data.get("guard_level", 0)
            guard_name = {1: "总督", 2: "提督", 3: "舰长"}.get(
                guard_level, f"guard_{guard_level}"
            )
            room_id = str(guard_data.get("room_id", self.client.room_id))
            content = f"[大航海] {user_name} 开通了 {guard_name}"

            self._log_event(f"{user_name}开通了 {guard_name}")

            abm.type = MessageType.GROUP_MESSAGE
            abm.group_id = room_id
            abm.message_str = content
            abm.sender = MessageMember(user_id=user_id, nickname=user_name)
            abm.message = [Plain(text=content)]
            abm.session_id = f"{user_id}_{room_id}"
            abm.message_id = ""

        elif cmd == "LIVE_OPEN_PLATFORM_LIVE_ROOM_ENTER":
            # User enters the live room
            enter_data = data.get("data", data)
            user_id = str(enter_data.get("open_id"))
            user_name = enter_data.get("uname", "")
            room_id = str(enter_data.get("room_id", self.client.room_id))
            content = f"[进入] {user_name} 进入了直播间"

            self._log_event(f"{user_name}进入了直播间")

            abm.type = MessageType.GROUP_MESSAGE
            abm.group_id = room_id
            abm.message_str = content
            abm.sender = MessageMember(user_id=user_id, nickname=user_name)
            abm.message = [Plain(text=content)]
            abm.session_id = f"{user_id}_{room_id}"
            abm.message_id = ""

        elif cmd == "LIVE_OPEN_PLATFORM_LIKE":
            # Like event
            like_data = data.get("data", data)
            user_id = str(like_data.get("open_id"))
            user_name = like_data.get("uname", "")
            like_count = like_data.get("like_count", 1)
            room_id = str(like_data.get("room_id", self.client.room_id))
            content = f"[点赞] {user_name} 点赞 x{like_count}"

            self._log_event(f"{user_name} 点赞 x{like_count}")

            abm.type = MessageType.GROUP_MESSAGE
            abm.group_id = room_id
            abm.message_str = content
            abm.sender = MessageMember(user_id=user_id, nickname=user_name)
            abm.message = [Plain(text=content)]
            abm.session_id = f"{user_id}_{room_id}"
            abm.message_id = ""

        else:
            # Unknown message type — try generic extraction
            dm_data = data.get("data", data)
            if isinstance(dm_data, dict):
                user_id = str(dm_data.get("open_id"))
                user_name = dm_data.get("uname", dm_data.get("user_name", ""))
                content = dm_data.get(
                    "msg", dm_data.get("content", dm_data.get("message", str(data)))
                )
                room_id = str(dm_data.get("room_id", self.client.room_id))
            else:
                user_id = ""
                user_name = ""
                content = str(data)
                room_id = self.client.room_id or ""

            if not content.strip():
                return None

            abm.type = MessageType.GROUP_MESSAGE
            abm.group_id = room_id
            abm.message_str = content
            abm.sender = MessageMember(user_id=user_id, nickname=user_name)
            abm.message = [Plain(text=content)]
            abm.session_id = f"{user_id}_{room_id}"
            abm.message_id = ""

        return abm

    async def handle_msg(self, message: AstrBotMessage):
        cmd = message.raw_message.get("cmd", "")
        # Only danmu messages are sent to LLM; other events are logged only.
        if cmd not in ("LIVE_OPEN_PLATFORM_DM", "DANMU_MSG"):
            return

        message_event = BLivePlatformEvent(
            message_str=message.message_str,
            message_obj=message,
            platform_meta=self.meta(),
            session_id=message.session_id,
            client=self.client,
        )
        message_event.is_wake = True
        message_event.is_at_or_wake_command = True
        self.commit_event(message_event)
