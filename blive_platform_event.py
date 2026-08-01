import os
import threading

from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import Image, Plain, Record
from astrbot.api.platform import AstrBotMessage, PlatformMetadata
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path
from astrbot.core.utils.io import download_image_by_url

from .client import BLiveClient

PLUGIN_NAME = "astrbot_plugin_Blive_adapter"
PLUGIN_DATA_DIR = os.path.join(get_astrbot_plugin_data_path(), PLUGIN_NAME)
SENT_MESSAGES_FILE = os.path.join(PLUGIN_DATA_DIR, "sent_messages.txt")

os.makedirs(PLUGIN_DATA_DIR, exist_ok=True)


class BLivePlatformEvent(AstrMessageEvent):
    def __init__(
        self,
        message_str: str,
        message_obj: AstrBotMessage,
        platform_meta: PlatformMetadata,
        session_id: str,
        client: BLiveClient,
    ):
        super().__init__(message_str, message_obj, platform_meta, session_id)
        self.client = client

    async def send(self, message: MessageChain):
        for comp in message.chain:
            if isinstance(comp, Plain):
                self._log_sent_message(comp.text)
            elif isinstance(comp, Image):
                img_url = comp.file
                if img_url.startswith("file:///"):
                    img_url = img_url[8:]
                elif comp.file and comp.file.startswith("http"):
                    img_url = await download_image_by_url(comp.file)
                text = f"[Image] {img_url}"
                self._log_sent_message(text)
            elif isinstance(comp, Record):
                try:
                    file_path = await comp.convert_to_file_path()
                    self._play_audio(file_path)
                    self._log_sent_message(comp.text or "[Audio] played")
                except Exception as e:
                    from astrbot import logger

                    logger.error(f"[Bilibili] failed to play audio: {e}")

        await super().send(message)

    @staticmethod
    def _play_audio(file_path: str):
        """Play audio file through speaker in a separate thread."""

        def _play_thread():
            import shutil
            import time
            import uuid
            import winsound

            try:
                ext = os.path.splitext(file_path)[1].lower() or ".wav"
                dest_path = os.path.join(
                    PLUGIN_DATA_DIR, f"audio_playback_{uuid.uuid4().hex}{ext}"
                )
                shutil.copy2(file_path, dest_path)
                time.sleep(0.1)

                if ext == ".wav":
                    winsound.PlaySound(dest_path, winsound.SND_FILENAME)
                else:
                    from astrbot import logger

                    logger.error(
                        f"[Bilibili] unsupported audio format: {ext}. Only WAV is supported."
                    )
            except Exception as e:
                from astrbot import logger

                logger.error(f"[Bilibili] audio playback error: {e}")
            finally:
                try:
                    if "dest_path" in dir() and os.path.exists(dest_path):
                        os.remove(dest_path)
                except Exception:
                    pass

        thread = threading.Thread(target=_play_thread, daemon=True)
        thread.start()

    @staticmethod
    def _log_sent_message(text: str):
        """Synchronously log sent message to sent_messages.txt."""
        line = f"{text}\n"
        try:
            with open(SENT_MESSAGES_FILE, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception as e:
            from astrbot import logger

            logger.error(f"[Bilibili] failed to log sent message: {e}")
