"""The connection to Home Assistant: its WebSocket API, with the OpenVoiceOS
integration's ovos/* commands (andlo/ha-ovos-integration, docs/protocol.md).

Runs its own asyncio loop in a thread; the plugin hands it bus messages with
send() (thread-safe) and gets HA's with on_bus. Connects again by itself
when HA restarts or the network drops.
"""
from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable
import itertools
import json
import re
import threading
import time
from typing import Any

import aiohttp
from ovos_utils.log import LOG

PROTOCOL = 1
RETRY_SECONDS = 10
# a file for HA (the panel's backup) goes in one WebSocket message: HA takes
# 4 MB, and base64 makes it a third bigger
MAX_FILE = 3 * 1024 * 1024 - 64 * 1024


class HALink:
    def __init__(self, url: str, token: str, device: dict[str, Any],
                 on_bus: Callable[[dict], None],
                 on_event: Callable[[dict], None] | None = None,
                 panel_url: str | None = None, panel_token: str | None = None,
                 on_satellite_bus: Callable[[str, dict], None] | None = None) -> None:
        self.url = url.rstrip("/").replace("http", "ws", 1) + "/api/websocket"
        # this device's HiveMind satellites, each its own device in HA "via"
        # this one, on this same connection (ha-ovos-integration#12):
        # device_id -> {"info", "sub" (its ovos/connect id), "ready", "forward"}
        self.satellites: dict[str, dict[str, Any]] = {}
        self.on_satellite_bus = on_satellite_bus or (lambda device_id, m: None)
        self.token = token
        self.device = device            # what ovos/connect says about the device
        self.on_bus = on_bus            # a bus message from HA, for the device
        self.on_event = on_event or (lambda e: None)
        self.forward: set[str] = set()  # the types HA wants (ready event)
        self.ready = False
        self.ha_device_id: str | None = None   # HA's own id for this device
        # how the connection is, for the control panel's page (status()):
        # connecting, unreachable, refused, pending, ready, displaced or removed
        self.state = "connecting"
        self.error = ""
        self.ha_version = ""
        self.since = time.time()
        self._results: dict[int, asyncio.Future] = {}
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._ids = itertools.count(1)
        self._connect_id: int | None = None
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="ha-link", daemon=True)
        self._stopping = False
        # the device's ovos-control-panel, which HA reaches through this link
        self.panel_url = panel_url.rstrip("/") if panel_url else None
        self.panel_token = panel_token
        self._session: aiohttp.ClientSession | None = None

    # --- the plugin's side ------------------------------------------------

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stopping = True
        if self._ws is not None:
            asyncio.run_coroutine_threadsafe(self._ws.close(), self._loop)
        self._loop.call_soon_threadsafe(self._loop.stop)

    def _set(self, state: str, error: str = "") -> None:
        if state != self.state:
            self.since = time.time()
        self.state, self.error = state, error

    def status(self) -> dict[str, Any]:
        """How the connection is: never the token."""
        return {"state": self.state, "error": self.error, "since": self.since,
                "ha_version": self.ha_version, "forward": len(self.forward),
                "satellites": [{"device_id": d, "name": (s.get("info") or {}).get("name", ""),
                                "ready": bool(s.get("ready"))} for d, s in self.satellites.items()]}

    def send(self, message: dict[str, Any]) -> None:
        """A bus message for HA, if HA asked for this type (thread-safe)."""
        if self.ready and message.get("type") in self.forward:
            self._loop.call_soon_threadsafe(self._send_bus, message)

    # --- satellites (thread-safe) -------------------------------------------

    def add_satellite(self, info: dict[str, Any]) -> None:
        """A satellite connected to this device: it shows up in HA too."""
        self._loop.call_soon_threadsafe(self._add_satellite, info)

    def drop_satellite(self, device_id: str, forget: bool = False) -> None:
        """It disconnected (unavailable in HA), or was removed (forget: its
        device goes from HA)."""
        self._loop.call_soon_threadsafe(self._drop_satellite, device_id, forget)

    def send_satellite(self, device_id: str, message: dict[str, Any]) -> None:
        sat = self.satellites.get(device_id)
        if sat and sat["ready"] and message.get("type") in sat["forward"]:
            self._loop.call_soon_threadsafe(self._send, {
                "type": "ovos/bus", "device_id": device_id,
                "message": {"type": message["type"], "data": message.get("data") or {},
                            "context": message.get("context") or {}}})

    def _add_satellite(self, info: dict[str, Any]) -> None:
        sat = self.satellites.setdefault(info["device_id"], {"sub": None, "ready": False,
                                                            "forward": set()})
        sat["info"] = {**info, "via_device": self.device["device_id"]}
        if self.ready and sat["sub"] is None:
            self._connect_satellite(info["device_id"])

    def _connect_satellite(self, device_id: str) -> None:
        sat = self.satellites[device_id]
        sat["sub"], sat["ready"] = next(self._ids), False
        self._send_raw({"id": sat["sub"], "type": "ovos/connect", "protocol": PROTOCOL,
                        **sat["info"]})

    def _drop_satellite(self, device_id: str, forget: bool) -> None:
        sat = self.satellites.pop(device_id, None)
        if sat is None or self._ws is None or self._ws.closed:
            return
        if sat["sub"] is not None:
            self._send_raw({"id": next(self._ids), "type": "unsubscribe_events",
                            "subscription": sat["sub"]})
        if forget:
            self._send({"type": "ovos/forget", "device_id": device_id})

    def _satellite_event(self, device_id: str, event: dict[str, Any]) -> None:
        sat = self.satellites.get(device_id)
        if sat is None:
            return
        match event.get("type"):
            case "ready":
                sat["forward"], sat["ready"] = set(event.get("forward", [])), True
                LOG.info(f"ha-link: satellite {sat['info'].get('name')} is in Home Assistant")
            case "bus":
                self.on_satellite_bus(device_id, event.get("message") or {})
            case "pending" | "displaced" | "removed":
                sat["ready"] = False

    def command(self, msg: dict[str, Any]) -> None:
        """Any other command for HA (thread-safe), e.g. the control panel's answers."""
        self._loop.call_soon_threadsafe(self._send, msg)

    def call(self, msg: dict[str, Any], timeout: float = 10) -> Any:
        """A command for HA, waiting for its result (thread-safe, blocking).
        Raises on an error or when HA isn't there."""
        if self._ws is None or self._ws.closed:
            raise ConnectionError("not connected to Home Assistant")
        return asyncio.run_coroutine_threadsafe(self._call(msg), self._loop).result(timeout)

    async def _call(self, msg: dict[str, Any]) -> Any:
        msg_id = next(self._ids)
        fut = self._loop.create_future()
        self._results[msg_id] = fut
        try:
            await self._ws.send_json({"id": msg_id, **msg})
            return await fut
        finally:
            self._results.pop(msg_id, None)

    # --- the loop ---------------------------------------------------------

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.create_task(self._keep_connected())
        self._loop.run_forever()

    async def _keep_connected(self) -> None:
        while not self._stopping:
            try:
                await self._connect_once()
                if not self._stopping:
                    LOG.warning("ha-link: Home Assistant closed the connection")
            except PermissionError as e:
                LOG.error(f"ha-link: {e}")
                self._set("refused", str(e))
            except (aiohttp.ClientError, OSError) as e:
                LOG.warning(f"ha-link: can't reach Home Assistant: {e}")
                self._set("unreachable", str(e) or type(e).__name__)
            else:
                if not self._stopping:
                    self._set("unreachable", "Home Assistant closed the connection")
            self.ready = False
            for sat in self.satellites.values():   # connected again after the hub
                sat["sub"], sat["ready"] = None, False
            self.on_event({"type": "lost"})
            await asyncio.sleep(RETRY_SECONDS)

    async def _connect_once(self) -> None:
        async with aiohttp.ClientSession() as session, \
                session.ws_connect(self.url, heartbeat=30) as ws:
            self._ws, self._session = ws, session
            device = dict(self.device)
            if await self._panel_answers():
                device["panel"] = True
            hello = await ws.receive_json()
            if hello.get("type") != "auth_required":
                raise aiohttp.ClientError(f"not Home Assistant: {hello}")
            await ws.send_json({"type": "auth", "access_token": self.token})
            auth = await ws.receive_json()
            if auth.get("type") != "auth_ok":
                raise PermissionError(f"Home Assistant refused the token: {auth.get('message')}")
            LOG.info(f"ha-link: logged in to Home Assistant {auth.get('ha_version')}")
            self.ha_version = auth.get("ha_version") or ""
            self._connect_id = next(self._ids)
            await ws.send_json({"id": self._connect_id, "type": "ovos/connect",
                                "protocol": PROTOCOL, **device})
            async for raw in ws:
                if raw.type != aiohttp.WSMsgType.TEXT:
                    continue
                msg = json.loads(raw.data)
                if msg.get("type") == "result" and msg.get("id") in self._results:
                    fut = self._results[msg["id"]]
                    if not fut.done():
                        if msg.get("success"):
                            fut.set_result(msg.get("result"))
                        else:
                            fut.set_exception(RuntimeError((msg.get("error") or {}).get("message", "error")))
                    continue
                if msg.get("id") != self._connect_id:
                    sat_id = next((d for d, s in self.satellites.items()
                                   if s["sub"] == msg.get("id")), None)
                    if sat_id is not None:
                        if msg.get("type") == "event":
                            self._satellite_event(sat_id, msg["event"])
                        elif msg.get("type") == "result" and not msg.get("success"):
                            LOG.warning(f"ha-link: HA refused satellite {sat_id}: {msg.get('error')}")
                    elif msg.get("type") == "result" and not msg.get("success"):
                        LOG.debug(f"ha-link: HA refused a command: {msg.get('error')}")
                    continue
                if msg.get("type") == "result" and not msg.get("success"):
                    raise PermissionError(f"Home Assistant refused the device: {msg.get('error')}")
                if msg.get("type") == "event":
                    self._on_connect_event(msg["event"])
            self._ws = None

    def _on_connect_event(self, event: dict[str, Any]) -> None:
        match event.get("type"):
            case "pending":
                self.ready = False
                self._set("pending")
                LOG.info("ha-link: waiting to be added in Home Assistant "
                         "(Settings -> Devices & services -> Discovered)")
            case "ready":
                self.forward = set(event.get("forward", []))
                self.ha_device_id = event.get("device_id")
                self.ready = True
                self._set("ready")
                LOG.info(f"ha-link: ready, {len(self.forward)} message types go to Home Assistant")
                # the satellites come in once their hub is there (again)
                for device_id, sat in self.satellites.items():
                    if sat["sub"] is None:
                        self._connect_satellite(device_id)
            case "bus":
                self.on_bus(event.get("message") or {})
            case "panel":
                self._loop.create_task(self._panel(event))
            case "displaced" | "removed":
                self.ready = False
                self._set(event["type"])
                LOG.warning(f"ha-link: device {event['type']} in Home Assistant")
        self.on_event(event)

    # --- the control panel (ha-ovos-integration#15) ----------------------------

    async def _panel_answers(self) -> bool:
        """Is there a control panel on this device to offer HA?"""
        if not self.panel_url:
            return False
        try:
            async with self._session.get(f"{self.panel_url}/healthz",
                                         timeout=aiohttp.ClientTimeout(total=5)) as r:
                return r.status == 200
        except (aiohttp.ClientError, OSError, TimeoutError):
            LOG.info(f"ha-link: no control panel at {self.panel_url}")
            return False

    async def _panel(self, event: dict[str, Any]) -> None:
        """One request from HA to the panel's API, made here on the device."""
        method = str(event.get("method", "GET")).upper()
        path = str(event.get("path", ""))
        status, body = 400, {"detail": "bad request"}
        if method in ("GET", "POST", "PUT", "DELETE") and path.startswith("/") \
                and ".." not in path and "://" not in path:
            headers = {"Authorization": f"Bearer {self.panel_token}"} if self.panel_token else {}
            try:
                async with self._session.request(
                        method, f"{self.panel_url}/api{path}", headers=headers,
                        json=event.get("body") if method != "GET" else None,
                        timeout=aiohttp.ClientTimeout(total=80 if event.get("binary") else 25)) as r:
                    status = r.status
                    if event.get("binary") and status < 400:
                        body = await self._file(r)
                        if body is None:
                            status, body = 413, {"detail": "the file is too big to send to Home Assistant"}
                    else:
                        try:
                            body = await r.json(content_type=None)
                        except ValueError:
                            body = {"detail": (await r.text())[:500]}
            except (aiohttp.ClientError, OSError, TimeoutError) as e:
                status, body = 502, {"detail": f"the control panel did not answer: {e}"}
        self._send({"type": "ovos/panel/response", "device_id": self.device["device_id"],
                    "request_id": event.get("id"), "status": status, "body": body})

    @staticmethod
    async def _file(r: aiohttp.ClientResponse) -> dict[str, Any] | None:
        """A file answer (the panel's backup) as JSON: base64, with its
        name. None when it wouldn't fit in one of HA's WebSocket messages
        (4 MB)."""
        data = await r.read()
        if len(data) > MAX_FILE:
            return None
        disposition = r.headers.get("Content-Disposition", "")
        match = re.search(r'filename="?([^";]+)"?', disposition)
        return {"content_type": r.headers.get("Content-Type", "application/octet-stream"),
                "filename": match.group(1) if match else None,
                "data": base64.b64encode(data).decode()}

    def _send(self, msg: dict[str, Any]) -> None:
        self._send_raw({"id": next(self._ids), **msg})

    def _send_raw(self, msg: dict[str, Any]) -> None:
        if self._ws is not None and not self._ws.closed:
            self._loop.create_task(self._ws.send_json(msg))

    def _send_bus(self, message: dict[str, Any]) -> None:
        self._send({"type": "ovos/bus", "device_id": self.device["device_id"],
                    "message": {"type": message["type"],
                                "data": message.get("data") or {},
                                "context": message.get("context") or {}}})
