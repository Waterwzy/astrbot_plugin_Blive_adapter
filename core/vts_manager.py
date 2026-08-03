from dataclasses import dataclass
from pathlib import Path

import pyvts
from astrbot.api import logger
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

PLUGIN_NAME = "astrbot_plugin_Blive_adapter"

# Store the VTS token inside the AstrBot plugin data dir so it does not
# depend on the process working directory.
_TOKEN_PATH = Path(get_astrbot_plugin_data_path()) / PLUGIN_NAME / "vts_token.txt"
# Legacy location used by older versions of this plugin (relative to CWD).
_LEGACY_TOKEN_PATH = Path("./vts_token.txt")

PLUGIN_INFO = {
    "plugin_name": "vts_emotion",
    "developer": "Waterwzy",
    "authentication_token_path": str(_TOKEN_PATH),
}


@dataclass
class VTSManager:
    vts = None

    async def _vts_connect(self):
        try:
            self.vts = pyvts.vts(plugin_info=PLUGIN_INFO)
            await self.vts.connect()

            # Make sure the target directory exists before pyvts writes the
            # token file, otherwise the token would silently fail to be saved.
            _TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
            self._migrate_legacy_token()

            # Load the token from file (or request a new one if missing),
            # then try to authenticate with it.
            await self.vts.request_authenticate_token()
            authenticated = await self.vts.request_authenticate()
            if authenticated:
                logger.info(f"[VTS] authenticated with existing token at {_TOKEN_PATH}")
                return

            # The stored token is missing or no longer valid: force a new one.
            # Note: this requires the user to approve the plugin in VTube Studio.
            logger.warning(
                "[VTS] token invalid, requesting a new token "
                "(please approve the plugin in VTube Studio)"
            )
            await self.vts.request_authenticate_token(force=True)
            authenticated = await self.vts.request_authenticate()
            if authenticated:
                logger.info("[VTS] authenticated with a new token")
            else:
                logger.error(
                    "[VTS] authentication failed, VTS emotion features are disabled"
                )

        except Exception as e:
            logger.error(f"[VTS] failed to connect: {e}")

    def _migrate_legacy_token(self):
        """Migrate the token file from the old CWD-relative location, if any."""
        if _TOKEN_PATH.exists() or not _LEGACY_TOKEN_PATH.exists():
            return
        try:
            token = _LEGACY_TOKEN_PATH.read_text(encoding="utf-8")
            _TOKEN_PATH.write_text(token, encoding="utf-8")
            logger.info(
                f"[VTS] migrated token from {_LEGACY_TOKEN_PATH} to {_TOKEN_PATH}"
            )
        except Exception as e:
            logger.error(f"[VTS] failed to migrate token: {e}")

    async def get_emotions(self):
        if not self.vts:
            await self._vts_connect()
        try:
            possible_emotions = await self.vts.request(
                self.vts.vts_request.requestHotKeyList()
            )
        except Exception as e:
            logger.error(f"[VTS] failed to request hotkey list: {e}")
            return []
        data = possible_emotions.get("data", {})
        if "availableHotkeys" not in data:
            # VTube Studio returns error responses without `availableHotkeys`
            # (e.g. errorID/message when the plugin is not authenticated).
            logger.error(f"[VTS] unexpected response from VTube Studio: {data}")
            return []
        format_keys = []
        for items in data["availableHotkeys"]:
            format_keys.append({"name": items["name"], "id": items["hotkeyID"]})
        return format_keys

    async def send_emotion(self, emotion_id: str):
        if not self.vts:
            await self._vts_connect()
        try:
            response = await self.vts.request(
                self.vts.vts_request.requestTriggerHotKey(emotion_id)
            )
            data = response.get("data", {})
            if "errorID" in data:
                logger.error(
                    f"[VTS] failed to trigger hotkey {emotion_id}: {data.get('message')}"
                )
                return False
            logger.info(f"[VTS] sent emotion {emotion_id}")
            return True
        except Exception as e:
            logger.error(f"[VTS] failed to send emotion {emotion_id}: {e}")
            return False
