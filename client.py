import asyncio
import hashlib
import hmac
import json
import logging
import os
import random
import ssl
import time

import requests
import websockets

from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

from .proto import Proto

logger = logging.getLogger("astrbot")

PLUGIN_NAME = "astrbot_plugin_Blive_adapter"
GAME_ID_FILE = os.path.join(
    get_astrbot_plugin_data_path(), PLUGIN_NAME, "session_cache.json"
)


class BLiveClient:
    def __init__(
        self,
        id_code: str,
        app_id: int,
        access_key: str,
        access_secret: str,
        host: str = "https://live-open.biliapi.com",
    ):
        self.id_code = id_code
        self.app_id = app_id
        self.access_key = access_key
        self.access_secret = access_secret
        self.host = host
        self.game_id = ""
        self.room_id = ""
        self._ws = None
        self._running = False
        self._loop = None
        self.on_message: callable = None
        self._end_app_fail_count = 0
        self._heartbeat_interval = 15
        self._heartbeat_timeout = 120
        self._app_heartbeat_interval = 30
        self._connection_timeout = 30

    def _sign(self, params: str) -> dict:
        md5 = hashlib.md5()
        md5.update(params.encode())
        ts = time.time()
        nonce = random.randint(1, 100000) + time.time()
        md5data = md5.hexdigest()

        headers = {
            "x-bili-timestamp": str(int(ts)),
            "x-bili-signature-method": "HMAC-SHA256",
            "x-bili-signature-nonce": str(nonce),
            "x-bili-accesskeyid": self.access_key,
            "x-bili-signature-version": "1.0",
            "x-bili-content-md5": md5data,
        }

        sorted_headers = sorted(headers)
        header_str = ""
        for key in sorted_headers:
            header_str = header_str + key + ":" + str(headers[key]) + "\n"
        header_str = header_str.rstrip("\n")

        signature = hmac.new(
            self.access_secret.encode(),
            header_str.encode(),
            digestmod=hashlib.sha256,
        ).hexdigest()
        headers["Authorization"] = signature
        headers["Content-Type"] = "application/json"
        headers["Accept"] = "application/json"
        return headers

    async def _http_post_async(self, url: str, params: str) -> dict:
        """Non-blocking HTTP POST via executor."""
        headers = self._sign(params)
        resp = await asyncio.to_thread(
            requests.post, url=url, headers=headers, data=params, verify=False
        )
        return json.loads(resp.text)

    async def _get_websocket_info(self) -> tuple:
        """Start app with retry for rate-limit and duplicate-room errors."""
        url = f"{self.host}/v2/app/start"
        params = json.dumps({"code": self.id_code, "app_id": self.app_id})

        last_error = None
        for attempt in range(20):
            data = await self._http_post_async(url, params)
            code = data.get("code", 0)
            msg = data.get("message", "")
            logger.info(
                f"[Bilibili] app/start attempt={attempt + 1} -> code={code} msg={msg}"
            )
            if code == 0:
                ws_info = data["data"]["websocket_info"]
                game_info = data["data"]["game_info"]
                self.game_id = str(game_info["game_id"])
                self.room_id = str(game_info.get("room_id", ""))
                try:
                    os.makedirs(os.path.dirname(GAME_ID_FILE), exist_ok=True)
                    with open(GAME_ID_FILE, "w", encoding="utf-8") as f:
                        json.dump(
                            {
                                "game_id": self.game_id,
                                "room_id": self.room_id,
                                "ts": time.time(),
                            },
                            f,
                        )
                    logger.info(
                        f"[Bilibili] saved session to {GAME_ID_FILE} game_id={self.game_id}"
                    )
                except Exception as e:
                    logger.warning(f"[Bilibili] save session failed: {e}")
                return ws_info["wss_link"][0], ws_info["auth_body"]

            last_error = data

            if code == 7001:
                logger.warning(
                    f"[Bilibili] start app failed ({code} {msg}), "
                    f"retry {attempt + 1}/20 in 10s..."
                )
                await asyncio.sleep(10)

            elif code == 7002:
                # Try to end the stale session via persisted game_id
                end_ok = await self._end_app()
                if end_ok:
                    # Session ended successfully, wait a bit for server cleanup
                    wait = 10
                else:
                    # No game_id available; server must time out on its own.
                    # Increase wait progressively to reduce useless requests.
                    self._end_app_fail_count += 1
                    if self._end_app_fail_count == 1:
                        wait = 10
                    elif self._end_app_fail_count == 2:
                        wait = 30
                    elif self._end_app_fail_count == 3:
                        wait = 60
                    else:
                        wait = 120
                logger.warning(
                    f"[Bilibili] start app failed ({code} {msg}), "
                    f"retry {attempt + 1}/20 in {wait}s..."
                )
                await asyncio.sleep(wait)
            else:
                raise Exception(f"Failed to start app: {data}")

        raise Exception(f"Failed to start app after 20 retries: {last_error}")

    async def _auth(self, ws, auth_body: str):
        req = Proto()
        req.body = auth_body
        req.op = 7
        await ws.send(req.pack())
        buf = await ws.recv()
        resp = Proto()
        if not resp.unpack(buf):
            raise Exception("Failed to unpack auth response")
        resp_body = json.loads(resp.body)
        if resp_body.get("code") != 0:
            raise Exception(f"Auth failed: {resp_body}")

    async def _heartbeat(self):
        reconnect_needed = False
        consecutive_failures = 0
        max_consecutive_failures = 3

        while self._running:
            await asyncio.sleep(self._heartbeat_interval)
            if not self._running:
                break
            try:
                if self._ws:
                    req = Proto()
                    req.op = 2
                    await self._ws.send(req.pack())
                    consecutive_failures = 0
                    logger.debug("[Bilibili] sent WebSocket heartbeat")
            except Exception as e:
                consecutive_failures += 1
                logger.warning(
                    f"[Bilibili] WebSocket heartbeat failed ({consecutive_failures}/{max_consecutive_failures}): {e}"
                )
                if consecutive_failures >= max_consecutive_failures:
                    logger.error("[Bilibili] too many heartbeat failures, reconnecting")
                    reconnect_needed = True
                    break
        return reconnect_needed

    async def _app_heartbeat(self):
        reconnect_needed = False
        consecutive_failures = 0
        max_consecutive_failures = 3

        while self._running:
            await asyncio.sleep(self._app_heartbeat_interval)
            if not self._running:
                break
            try:
                if self.game_id:
                    url = f"{self.host}/v2/app/heartbeat"
                    params = json.dumps({"game_id": self.game_id})
                    await self._http_post_async(url, params)
                    consecutive_failures = 0
                    logger.debug("[Bilibili] sent app heartbeat")
            except Exception as e:
                consecutive_failures += 1
                logger.warning(
                    f"[Bilibili] app heartbeat failed ({consecutive_failures}/{max_consecutive_failures}): {e}"
                )
                if consecutive_failures >= max_consecutive_failures:
                    logger.error(
                        "[Bilibili] too many app heartbeat failures, reconnecting"
                    )
                    reconnect_needed = True
                    break
        return reconnect_needed

    async def _recv_loop(self):
        reconnect_needed = False
        consecutive_timeouts = 0
        max_consecutive_timeouts = 5

        while self._running:
            try:
                buf = await asyncio.wait_for(
                    self._ws.recv(), timeout=self._heartbeat_timeout
                )
                consecutive_timeouts = 0
                logger.debug(f"[Bilibili] received {len(buf)} bytes")
            except asyncio.TimeoutError:
                consecutive_timeouts += 1
                logger.info(
                    f"[Bilibili] recv timeout ({consecutive_timeouts}/{max_consecutive_timeouts}), heartbeat_timeout={self._heartbeat_timeout}s"
                )
                if consecutive_timeouts >= max_consecutive_timeouts:
                    logger.error(
                        f"[Bilibili] too many recv timeouts ({max_consecutive_timeouts}), reconnecting"
                    )
                    reconnect_needed = True
                    break
                continue
            except Exception as e:
                logger.warning(f"[Bilibili] recv loop error: {type(e).__name__}: {e}")
                reconnect_needed = True
                break

            resp = Proto()
            if not resp.unpack(buf):
                logger.warning("[Bilibili] failed to unpack message")
                continue
            if resp.op == 3:
                logger.debug("[Bilibili] received heartbeat response")
                continue
            if self.on_message and resp.body:
                try:
                    data = json.loads(resp.body)
                    asyncio.create_task(self._dispatch(data))
                except json.JSONDecodeError as e:
                    logger.warning(f"[Bilibili] JSON decode error: {e}")
        return reconnect_needed

    async def _dispatch(self, data: dict):
        try:
            await self.on_message(data)
        except Exception as e:
            logger.error(f"[Bilibili] message dispatch error: {e}")

    async def connect(self):
        addr, auth_body = await self._get_websocket_info()
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
        self._ws = await websockets.connect(
            addr,
            ssl=ssl_context,
            ping_interval=None,
            ping_timeout=None,
            close_timeout=10,
            max_size=2**20,
            open_timeout=self._connection_timeout,
        )
        await self._auth(self._ws, auth_body)
        logger.info("[Bilibili] WebSocket connected and authenticated")

    async def _disconnect(self):
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

    async def run(self):
        self._loop = asyncio.get_running_loop()
        self._running = True
        self._end_app_fail_count = 0
        self._reconnect_delay = 5
        self._max_reconnect_delay = 60

        while self._running:
            try:
                await self.connect()
                self._reconnect_delay = 5
            except Exception as e:
                logger.error(
                    f"[Bilibili] connection failed: {e}, retrying in {self._reconnect_delay}s..."
                )
                await asyncio.sleep(self._reconnect_delay)
                self._reconnect_delay = min(
                    self._reconnect_delay * 2, self._max_reconnect_delay
                )
                continue

            self._end_app_fail_count = 0

            tasks = [
                asyncio.ensure_future(self._recv_loop()),
                asyncio.ensure_future(self._heartbeat()),
                asyncio.ensure_future(self._app_heartbeat()),
            ]

            done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_COMPLETED
            )

            # Cancel remaining tasks
            for task in pending:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

            await self._disconnect()

            # End the current game session before reconnecting
            await self._end_app()

            if not self._running:
                break

            logger.warning("[Bilibili] disconnected, reconnecting in 5s...")
            await asyncio.sleep(5)

    async def _end_app(self) -> bool:
        """Call /v2/app/end and remove local cache.

        Returns True if the API was called (regardless of server response),
        False when no game_id is available at all.
        """
        game_id = self.game_id
        source = "self.game_id"
        if not game_id:
            source = "none"
            try:
                if os.path.exists(GAME_ID_FILE):
                    with open(GAME_ID_FILE, encoding="utf-8") as f:
                        cache = json.load(f)
                    game_id = str(cache.get("game_id", ""))
                    # Also restore room_id so convert_message keeps working
                    if not self.room_id:
                        self.room_id = str(cache.get("room_id", ""))
                    source = "file"
            except Exception as e:
                logger.warning(f"[Bilibili] _end_app read cache error: {e}")

        logger.info(f"[Bilibili] _end_app: game_id={game_id!r} (source={source})")

        if game_id:
            try:
                url = f"{self.host}/v2/app/end"
                params = json.dumps({"game_id": game_id, "app_id": self.app_id})
                data = await self._http_post_async(url, params)
                logger.info(
                    f"[Bilibili] app/end -> code={data.get('code')} msg={data.get('message')}"
                )
            except Exception as ex:
                logger.error(f"[Bilibili] app/end error: {ex}")
            finally:
                # Clear cached session so we don't retry with a stale id
                try:
                    if os.path.exists(GAME_ID_FILE):
                        os.remove(GAME_ID_FILE)
                        logger.info(f"[Bilibili] removed session cache {GAME_ID_FILE}")
                except Exception as e:
                    logger.warning(f"[Bilibili] remove session cache failed: {e}")
            return True
        else:
            logger.warning("[Bilibili] _end_app: no game_id, cannot call /v2/app/end")
            return False

    async def close(self):
        self._running = False
        await self._disconnect()
        await self._end_app()
