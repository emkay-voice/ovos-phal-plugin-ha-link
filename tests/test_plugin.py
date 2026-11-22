"""The plugin's messages for the control panel's page (controlpanel.json)."""
import json

import pytest
from ovos_bus_client.message import Message
from ovos_utils.fakebus import FakeBus

import ovos_phal_plugin_ha_link as plugin_module


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

    def drop_satellite(self, device_id, forget=False):
        self.dropped = (device_id, forget)


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


def test_the_page_forgets_a_satellite_by_its_device_id(bus):
    p = plugin_module.HALinkPlugin(bus=bus, config={"url": "http://ha:8123", "token": "t"})
    bus.emit(Message("ovos.ha_link.satellite.forget", {"device_id": "hm-0123456789abcdef"}))
    assert FakeLink.started[-1].dropped == ("hm-0123456789abcdef", True)
    p.shutdown()
