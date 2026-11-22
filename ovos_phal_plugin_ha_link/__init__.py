"""ovos-phal-plugin-ha-link: links an OpenVoiceOS device to Home Assistant.

The device connects to Home Assistant itself (Home Assistant's WebSocket
API, the OpenVoiceOS integration's ovos/connect) and carries its bus both
ways, so it shows up in Home Assistant as a device: volume, listening on/off,
state, last question and answer, its sensors, "say" and "ask". No port is
opened on the device, and it works behind NAT.

Configuration, in mycroft.conf:

    "PHAL": {
      "ovos-phal-plugin-ha-link": {
        "url": "http://homeassistant.local:8123",
        "token": "<a long-lived access token>",
        "name": "Kitchen"
      }
    }

The first time, Home Assistant shows the device under Settings -> Devices &
services -> Discovered; add it there.

The control panel's page (panel/) asks how the link is, and starts it again
with new settings, on the bus:

    ovos.ha_link.status   {}                 -> .response: see status()
    ovos.ha_link.reload   {"config": {...}}  -> .response: the same
"""
from __future__ import annotations

import hashlib
from importlib.metadata import PackageNotFoundError, version
import json
from pathlib import Path
import socket
from typing import Any
import uuid

from ovos_bus_client.message import Message
from ovos_plugin_manager.phal import PHALPlugin
from ovos_utils.log import LOG
from ovos_utils.xdg_utils import xdg_state_home

from .link import HALink

PLUGIN = "ovos-phal-plugin-ha-link"
# context key on what the link puts on the bus for a satellite, so it isn't
# taken for the satellite's own answer
SENT_BY_LINK = "ha_link"


def _version(package: str) -> str | None:
    try:
        return version(package)
    except PackageNotFoundError:
        return None


def device_id() -> str:
    """Stable per device: made once, kept in the XDG state folder."""
    path = Path(xdg_state_home()) / PLUGIN / "device_id"
    if path.exists():
        return path.read_text().strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    new = f"ovos-{uuid.uuid4()}"
    path.write_text(new)
    return new


class HALinkPlugin(PHALPlugin):
    def __init__(self, bus=None, config=None):
        super().__init__(bus=bus, name=PLUGIN, config=config)
        self.link: HALink | None = None
        # HiveMind satellites, when this OVOS is a hivemind-core master (Emkay
        # Home, a hub): each one becomes a device in HA, connected via this one
        self._peers: dict[str, str] = {}        # satellite device_id -> its peer
        self._clients: dict[str, str] = {}      # connected clients: peer without session -> device_id
        self.bus.on("hive.client.connect", self._hive_connect)
        self.bus.on("hive.client.disconnect", self._hive_disconnect)
        self.bus.on("ovos.ha_link.satellite.forget", self._hive_forget)
        # Home Assistant's Assist for ovos-assist-pipeline-plugin (the pipeline
        # plugin only sends bus messages; this link does the talking)
        self.bus.on("ovos.assist.ping", self._assist_ping)
        self.bus.on("ovos.assist.recognize", self._assist_recognize)
        self.bus.on("ovos.assist.process", self._assist_process)
        # the control panel's page: how the link is, and new settings at once
        self.bus.on("ovos.ha_link.status", self._status)
        self.bus.on("ovos.ha_link.reload", self._reload)
        # every bus message; the link sends HA only the types it asked for
        self.bus.on("message", self._from_bus)
        self._start()

    def _start(self) -> None:
        url, token = self.config.get("url"), self.config.get("token")
        if not url or not token:
            LOG.warning(f"{PLUGIN}: set url and token in mycroft.conf (PHAL -> {PLUGIN}) "
                        "to link this device to Home Assistant")
            return
        lang = None
        try:
            from ovos_config import Configuration
            lang = Configuration().get("lang")
        except Exception:  # noqa: BLE001 - the language is nice to have
            pass
        info = {
            "device_id": self.config.get("device_id") or device_id(),
            "name": self.config.get("name") or socket.gethostname(),
            "model": self.config.get("model") or "OpenVoiceOS",
            "manufacturer": "OpenVoiceOS",
            "sw_version": _version("ovos-core") or _version(PLUGIN),
        }
        if lang:
            info["lang"] = lang
        # the control panel on this device, offered to HA through the link
        panel_url = self.config.get("panel_url", "http://127.0.0.1:8500")
        panel_token = self.config.get("panel_token")
        if not panel_token:
            try:
                from ovos_config import Configuration
                panel_token = (Configuration().get("webui") or {}).get("access_token")
            except Exception:  # noqa: BLE001
                panel_token = None
        if self.config.get("panel_public_url"):
            info["panel_url"] = self.config["panel_public_url"]
        self.link = HALink(url, token, info, on_bus=self._from_ha, on_event=self._on_link_event,
                           panel_url=panel_url or None, panel_token=panel_token,
                           on_satellite_bus=self._from_ha_for_satellite)
        self.link.start()

    def _stop(self) -> None:
        if self.link is not None:
            self.link.stop()
            self.link = None
        self._peers.clear()

    def status(self) -> dict[str, Any]:
        """For the control panel's page: the settings (never the token) and
        how the link is."""
        out = {"configured": bool(self.config.get("url") and self.config.get("token")),
               "url": self.config.get("url") or "", "token_set": bool(self.config.get("token")),
               "name": self.config.get("name") or "", "default_name": socket.gethostname(),
               "model": self.config.get("model") or "",
               "panel_public_url": self.config.get("panel_public_url") or "",
               "satellites_on": bool(self.config.get("satellites", True)),
               "state": "off", "error": "", "since": None, "ha_version": "", "forward": 0,
               "satellites": []}
        if self.link is not None:
            out.update(self.link.status())
        return out

    def _status(self, message: Message) -> None:
        self.bus.emit(message.response(self.status()))

    def _reload(self, message: Message) -> None:
        """New settings from the control panel (already saved in mycroft.conf):
        {"config": this plugin's section}. The link starts again with them."""
        new = message.data.get("config")
        if isinstance(new, dict):
            self.config = dict(new)
        self._stop()
        self._start()
        self.bus.emit(message.response(self.status()))

    def _from_bus(self, raw) -> None:
        if self.link is None:
            return
        try:
            message = json.loads(raw) if isinstance(raw, str) else raw
        except ValueError:
            return
        if not isinstance(message, dict) or not message.get("type"):
            return
        context = message.get("context") or {}
        if context.get(SENT_BY_LINK):
            return              # what this link put on the bus for a satellite
        if self._clients:
            self._learn_peer(context.get("source"))
        satellite = self._satellite_of(context)
        if satellite:
            self.link.send_satellite(satellite, message)
        else:
            self.link.send(message)

    def _from_ha(self, message: dict) -> None:
        """What Home Assistant asks of the device, put on its bus as it is: no
        session, so OVOS treats it as the device's own."""
        if message.get("type"):
            self.bus.emit(Message(message["type"], message.get("data") or {},
                                  message.get("context") or {}))

    # --- HiveMind satellites (ha-ovos-integration#12) ------------------------------
    # hivemind-core puts what a satellite sends on this bus with its peer as
    # context["source"], and sends a satellite whatever has its peer in
    # context["destination"]. So the link only has to tell them apart.

    @staticmethod
    def satellite_id(key: str) -> str:
        """A satellite's device id: stable across connections (its session
        isn't), and not its secret access key. The same 16 characters Emkay
        Home shows for a paired Emkay."""
        return "hm-" + hashlib.sha256(key.encode()).hexdigest()[:16]

    def _satellite_of(self, context: dict) -> str | None:
        if not self._peers:
            return None
        targets = context.get("destination") or []
        targets = targets if isinstance(targets, list) else [targets]
        for device_id, peer in self._peers.items():
            if context.get("source") == peer or peer in targets:
                return device_id
        return None

    # hivemind-core's peer is "<useragent>::<client id>::<name>::<session>".
    # hive.client.connect comes before the client's HELLO, while the session is
    # still "default"; the real one is only known from the satellite's first
    # message. So a satellite is added to HA when it first says something
    # (Emkay sends its volume and mic state right after connecting).

    @staticmethod
    def _client_of(peer: str) -> str:
        return peer.rsplit("::", 1)[0]

    def _hive_connect(self, message: Message) -> None:
        if self.link is None or not self.config.get("satellites", True):
            return
        peer, key = message.context.get("source"), message.data.get("key")
        if peer and key:
            self._clients[self._client_of(peer)] = self.satellite_id(key)

    def _learn_peer(self, source: Any) -> None:
        if not isinstance(source, str) or "::" not in source:
            return
        device_id = self._clients.get(self._client_of(source))
        if device_id is None or self._peers.get(device_id) == source:
            return
        new = device_id not in self._peers
        self._peers[device_id] = source
        if new:
            parts = source.split("::")
            self.link.add_satellite({"device_id": device_id,
                                     "name": (parts[2] if len(parts) >= 4 else parts[0]) or "Satellite",
                                     "model": "HiveMind satellite",
                                     "manufacturer": "OpenVoiceOS"})

    def _hive_disconnect(self, message: Message) -> None:
        key = message.data.get("key")
        if not key or self.link is None:
            return
        device_id = self.satellite_id(key)
        self._clients = {c: d for c, d in self._clients.items() if d != device_id}
        if self._peers.pop(device_id, None):
            self.link.drop_satellite(device_id)

    def _hive_forget(self, message: Message) -> None:
        """A satellite was removed from the hub (unpaired): its device goes
        from HA too. data: {"key"}, {"id"} (the id as in satellite_id without
        "hm-", Emkay Home's client id), or {"device_id"} (the control panel)."""
        sid = message.data.get("id")
        device_id = (self.satellite_id(message.data["key"]) if message.data.get("key")
                     else f"hm-{sid}" if sid else message.data.get("device_id"))
        if device_id and self.link is not None:
            self._peers.pop(device_id, None)
            self._clients = {c: d for c, d in self._clients.items() if d != device_id}
            self.link.drop_satellite(device_id, forget=True)

    def _from_ha_for_satellite(self, device_id: str, message: dict) -> None:
        """What HA asks of a satellite, handed to hivemind-core for that one
        peer (hive.send.downstream). Not as the message itself on this bus:
        then this device's own PHAL would take "mycroft.volume.set" as meant
        for it too."""
        peer = self._peers.get(device_id)
        if peer and message.get("type"):
            payload = {"type": message["type"], "data": message.get("data") or {},
                       "context": {**(message.get("context") or {}), "source": PLUGIN,
                                   "destination": [peer], SENT_BY_LINK: True}}
            self.bus.emit(Message("hive.send.downstream",
                                  {"msg_type": "bus", "peer": peer, "payload": payload},
                                  {SENT_BY_LINK: True}))

    # --- Home Assistant's Assist (ovos-assist-pipeline-plugin) --------------------

    def _on_link_event(self, event: dict) -> None:
        if event.get("type") in ("ready", "lost", "displaced", "removed", "pending"):
            self.bus.emit(Message("ovos.assist.available",
                                  {"available": bool(self.link and self.link.ready)}))

    def _assist_ping(self, message: Message) -> None:
        self.bus.emit(message.reply("ovos.assist.available",
                                    {"available": bool(self.link and self.link.ready)}))

    @staticmethod
    def _ha_lang(lang: str | None) -> str | None:
        """OVOS' da-dk -> HA's da (HA keeps a region only where it matters)."""
        if not lang:
            return None
        base, _, region = lang.replace("_", "-").partition("-")
        return f"{base}-{region.upper()}" if base in ("pt", "zh") and region else base.lower()

    def _assist_recognize(self, message: Message) -> None:
        """Does HA's own sentence matching know this one? Recognising only."""
        data = {"match": False, "intent": None}
        if self.link is None:
            return self.bus.emit(message.response(data))
        try:
            result = self.link.call({
                "type": "conversation/agent/homeassistant/debug",
                "sentences": [message.data.get("utterance", "")],
                "language": self._ha_lang(message.data.get("lang")),
                "device_id": self.link.ha_device_id,
            }, timeout=3)
            found = (result or {}).get("results", [None])[0]
            if found and found.get("match"):
                data = {"match": True, "intent": (found.get("intent") or {}).get("name")}
        except Exception as e:  # noqa: BLE001 - no match is the safe answer
            LOG.debug(f"{PLUGIN}: assist recognize: {e}")
        self.bus.emit(message.response(data))

    def _assist_process(self, message: Message) -> None:
        """HA carries it out, with its own agent (the one that recognised it)."""
        data = {"speech": None}
        if self.link is None:
            return self.bus.emit(message.response(data))
        try:
            result = self.link.call({
                "type": "conversation/process",
                "text": message.data.get("utterance", ""),
                "language": self._ha_lang(message.data.get("lang")),
                "agent_id": "conversation.home_assistant",
                "device_id": self.link.ha_device_id,
            }, timeout=15)
            response = (result or {}).get("response") or {}
            data = {"speech": ((response.get("speech") or {}).get("plain") or {}).get("speech"),
                    "response_type": response.get("response_type"),
                    "conversation_id": (result or {}).get("conversation_id")}
        except Exception as e:  # noqa: BLE001
            LOG.warning(f"{PLUGIN}: assist process: {e}")
        self.bus.emit(message.response(data))

    def shutdown(self):
        self.bus.remove("message", self._from_bus)
        self._stop()
        super().shutdown()
