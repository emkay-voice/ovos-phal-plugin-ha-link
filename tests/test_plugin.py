"""The plugin's messages for the control panel's page, and the page itself:
its description, its strings in every language, and its routes."""
import json
import re
from pathlib import Path

import pytest
from ovos_bus_client.message import Message
from ovos_utils.fakebus import FakeBus

import ovos_phal_plugin_ha_link as plugin_module
from ovos_phal_plugin_ha_link import panel

PANEL = Path(panel.__file__).parent
LANGS = {"ca", "da", "de", "en", "es", "eu", "fr", "gl", "it", "nl", "pt"}


class FakeLink:
    """HALink without the network: it is 'ready' as soon as it starts."""
    started = []

    def __init__(self, url, token, device, **kw):
        self.url, self.token, self.device = url, token, device
        self.ready, self.satellites = False, {}
        FakeLink.started.append(self)

    def start(self):
        self.ready = True

    def stop(self):
        self.ready = False

    def status(self):
        return {"state": "ready" if self.ready else "connecting", "error": "", "since": 1.0,
                "ha_version": "2026.11.0", "forward": 12, "satellites": []}

    def send(self, message):
        pass


@pytest.fixture
def bus(monkeypatch, tmp_path):
    monkeypatch.setattr(plugin_module, "HALink", FakeLink)
    monkeypatch.setattr(plugin_module, "device_id", lambda: "ovos-test")
    FakeLink.started.clear()
    return FakeBus()


def answer(bus, msg_type, **data):
    got = []
    bus.on(f"{msg_type}.response", lambda m: got.append(Message.deserialize(m) if isinstance(m, str) else m))
    bus.emit(Message(msg_type, data))
    return got[-1].data


def test_not_set_up_it_says_so_and_never_shows_a_token(bus):
    p = plugin_module.HALinkPlugin(bus=bus, config={})
    status = answer(bus, "ovos.ha_link.status")
    assert status["configured"] is False and status["state"] == "off" and p.link is None
    p.shutdown()


def test_the_status_of_a_link(bus):
    p = plugin_module.HALinkPlugin(bus=bus, config={"url": "http://ha:8123", "token": "secret", "name": "Kitchen"})
    status = answer(bus, "ovos.ha_link.status")
    assert status["state"] == "ready" and status["token_set"] and status["name"] == "Kitchen"
    assert "secret" not in json.dumps(status)
    p.shutdown()


def test_new_settings_take_effect_at_once(bus):
    p = plugin_module.HALinkPlugin(bus=bus, config={})
    status = answer(bus, "ovos.ha_link.reload", config={"url": "http://ha:8123", "token": "t", "name": "Hall"})
    assert status["state"] == "ready" and status["name"] == "Hall"
    first = p.link
    answer(bus, "ovos.ha_link.reload", config={"url": "http://other:8123", "token": "t"})
    assert first.ready is False, "the old link was stopped"
    assert p.link.url == "http://other:8123" and len(FakeLink.started) == 2
    p.shutdown()


def test_assist_without_a_link_answers_no(bus):
    p = plugin_module.HALinkPlugin(bus=bus, config={})
    assert answer(bus, "ovos.assist.recognize", utterance="turn on the light") == {"match": False, "intent": None}
    p.shutdown()


# ── the page ──────────────────────────────────────────────────────────────────

def test_the_page_description_names_files_that_exist():
    page = panel.PAGE
    assert page["api_version"] == 1 and page["id"] == "home-assistant"
    for key in ("html", "icon"):
        assert (PANEL / page[key]).is_file(), key


def test_the_page_speaks_every_language():
    files = {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in (PANEL / "i18n").glob("*.json")}
    assert set(files) == LANGS
    keys = set(files["en"])
    for lang, table in files.items():
        assert set(table) == keys, lang
        assert all(text.strip() for text in table.values()), lang
        assert "{v}" in table["haLink.detail.ready"] and "{n}" in table["haLink.detail.ready"], lang
    html = (PANEL / "page.html").read_text(encoding="utf-8")
    used = set(re.findall(r'data-i18n="([^"]+)"', html)) | set(re.findall(r'"(haLink\.[\w.]+)":', html))
    assert used <= keys and keys - used <= {"haLink.title"}


pytest.importorskip("fastapi")
pytest.importorskip("httpx")


@pytest.fixture
def client(bus, tmp_path):
    from fastapi import APIRouter, FastAPI
    from fastapi.testclient import TestClient

    p = plugin_module.HALinkPlugin(bus=bus, config={})
    user = {"PHAL": {"ovos-phal-plugin-ha-link": {"token": "old-token", "name": "Old"}}}

    class Config:
        @staticmethod
        def mutate(change, bus=None):
            change(user)

        @staticmethod
        def section(name):
            return json.loads(json.dumps(user.get(name) or {}))

    class R:
        api = APIRouter(prefix="/api")
        privileged = APIRouter(prefix="/api")
        config = Config

        @staticmethod
        def need_bus():
            return bus

        @staticmethod
        def bus():
            return bus

    panel.routes(R)
    app = FastAPI()
    app.include_router(R.api)
    app.include_router(R.privileged)
    with TestClient(app) as c:
        yield c, p, user
    p.shutdown()


def test_saving_keeps_the_token_and_connects(client):
    c, p, user = client
    r = c.post("/api/ha-link", json={"url": "http://ha.local:8123/", "token": None, "name": "Kitchen",
                                     "satellites": False})
    assert r.status_code == 200, r.text
    mine = user["PHAL"]["ovos-phal-plugin-ha-link"]
    assert mine == {"url": "http://ha.local:8123", "token": "old-token", "name": "Kitchen", "satellites": False}
    assert r.json()["state"] == "ready" and p.link.token == "old-token"
    assert "old-token" not in r.text and "old-token" not in c.get("/api/ha-link").text


@pytest.mark.parametrize("body", [{"url": "ha.local"}, {"url": "http://ha", "panel_public_url": "ftp://x"}])
def test_bad_addresses_are_refused(client, body):
    c, _, _ = client
    assert c.post("/api/ha-link", json=body).status_code == 400


def test_forgetting_a_satellite(client):
    c, _, _ = client
    seen = []
    c.app  # noqa: B018
    client[1].bus.on("ovos.ha_link.satellite.forget", lambda m: seen.append(m))
    assert c.post("/api/ha-link/forget", json={"device_id": "hm-0123456789abcdef"}).json() == {"ok": True}
    assert c.post("/api/ha-link/forget", json={"device_id": "../x"}).status_code == 422
    assert seen
