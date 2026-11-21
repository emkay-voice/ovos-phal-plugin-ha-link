"""The Home Assistant page's API. The settings are saved in mycroft.conf
(PHAL -> ovos-phal-plugin-ha-link) through the panel, with its backups, and
then handed to the plugin, which connects again with them at once."""
import re
from typing import Any, Optional

from fastapi import HTTPException
from pydantic import BaseModel, Field

PLUGIN = "ovos-phal-plugin-ha-link"
STATUS = "ovos.ha_link.status"
RELOAD = "ovos.ha_link.reload"
FORGET = "ovos.ha_link.satellite.forget"
TIMEOUT = 5.0


class Settings(BaseModel):
    url: str = Field(max_length=500)
    #: None keeps the token there is; the page never sees it
    token: Optional[str] = Field(None, max_length=2000)
    name: str = Field("", max_length=100)
    model: str = Field("", max_length=100)
    panel_public_url: str = Field("", max_length=500)
    satellites: bool = True


class Satellite(BaseModel):
    device_id: str = Field(pattern=r"^hm-[0-9a-f]{16}$")


def ask(r, msg_type: str, data: dict | None = None, timeout: float = TIMEOUT) -> dict[str, Any] | None:
    from ovos_bus_client.message import Message
    bus = r.need_bus()
    message = Message(msg_type, data or {})
    try:
        from ovos_webui import buswait
        reply = buswait.wait_for_response(bus, message, timeout=timeout)
    except ImportError:  # an older panel: the bus client's own wait
        reply = bus.wait_for_response(message, timeout=timeout)
    return None if reply is None else reply.data


def tell(r, msg_type: str, data: dict) -> None:
    from ovos_bus_client.message import Message
    r.need_bus().emit(Message(msg_type, data))


def clean(body: Settings) -> Settings:
    url = body.url.strip().rstrip("/")
    if not re.match(r"^https?://[^\s/]+", url):
        raise HTTPException(400, "Home Assistant's address starts with http:// or https://")
    public = body.panel_public_url.strip()
    if public and not re.match(r"^https?://\S+$", public):
        raise HTTPException(400, "the panel's address starts with http:// or https://")
    token = body.token.strip() if body.token is not None else None
    return body.model_copy(update={"url": url, "panel_public_url": public, "token": token,
                                   "name": body.name.strip(), "model": body.model.strip()})


def save(r, body: Settings) -> dict[str, Any]:
    """Save the settings in the user's mycroft.conf; the section as it is now."""
    section: dict[str, Any] = {}

    def change(user: dict) -> None:
        phal = user.setdefault("PHAL", {})
        mine = dict(phal.get(PLUGIN) or {})
        mine["url"] = body.url
        if body.token is not None:
            mine["token"] = body.token
        for key in ("name", "model", "panel_public_url"):
            value = getattr(body, key)
            if value:
                mine[key] = value
            else:
                mine.pop(key, None)
        if body.satellites:
            mine.pop("satellites", None)
        else:
            mine["satellites"] = False
        phal[PLUGIN] = mine

    r.config.mutate(change, bus=r.bus())
    # what the plugin reads: the merged configuration, with this user layer on top
    section.update((r.config.section("PHAL") or {}).get(PLUGIN) or {})
    return section


def routes(r) -> None:
    @r.api.get("/ha-link")
    def api_ha_link() -> dict[str, Any]:
        answer = ask(r, STATUS)
        if answer is None:
            raise HTTPException(504, "the Home Assistant link did not answer; is OVOS' PHAL service running?")
        return answer

    # The address and token decide where the device connects and as whom:
    # they need the access token.
    @r.privileged.post("/ha-link")
    def api_ha_link_save(body: Settings) -> dict[str, Any]:
        body = clean(body)
        section = save(r, body)
        if not section.get("token"):
            raise HTTPException(400, "a long-lived access token from Home Assistant is needed")
        answer = ask(r, RELOAD, {"config": section})
        if answer is None:
            return {"saved": True, "restart": True}
        return {"saved": True, "restart": False, **answer}

    @r.privileged.post("/ha-link/forget")
    def api_ha_link_forget(body: Satellite) -> dict[str, Any]:
        tell(r, FORGET, {"id": body.device_id[3:]})
        return {"ok": True}
