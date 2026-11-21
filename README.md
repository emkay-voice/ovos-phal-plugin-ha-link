# ovos-phal-plugin-ha-link

An [OpenVoiceOS](https://openvoiceos.org) PHAL plugin that links the device to
[Home Assistant](https://www.home-assistant.io).

It is part of [Emkay](https://github.com/emkay-voice/emkay), and works on any
OpenVoiceOS.

The device connects to Home Assistant itself and carries its message bus both
ways to the [OpenVoiceOS integration](https://github.com/andlo/ha-ovos-integration).
In Home Assistant it shows up as a device, with what the bus can see and set:

- volume, mute and listening on/off: automations can turn listening off at night
- its state (idle, listening, thinking, speaking), the last question and the last answer
- **Say** (`notify.send_message`) and **Ask** (`ovos.ask`)
- the device's own sensors, when a plugin reports them (`ovos.phal.sensor`)

No port is opened on the device and nothing has to reach it from outside, so it
also works behind NAT. Any OVOS install can use it: ovos-installer, raspOVOS,
a Mark II, an OVOS in a VM.

## Install

```bash
pip install --pre ovos-phal-plugin-ha-link
```

In Home Assistant, install the [OpenVoiceOS integration](https://github.com/andlo/ha-ovos-integration)
(0.3.0 or newer), add it, and choose **An OpenVoiceOS device**. That lets
devices connect. Then make a long-lived access token: your profile → Security.

## Configure

In `mycroft.conf`:

```json
{
  "PHAL": {
    "ovos-phal-plugin-ha-link": {
      "url": "http://homeassistant.local:8123",
      "token": "<the long-lived access token>",
      "name": "Kitchen"
    }
  }
}
```

| Setting | |
|---|---|
| `url` | Home Assistant's address |
| `token` | a long-lived access token |
| `name` | the device's name in Home Assistant (default: the host name) |
| `model` | shown on the device page (default: `OpenVoiceOS`) |
| `panel_url` | the device's [ovos-control-panel](https://github.com/OpenVoiceOS/ovos-control-panel), reached here on the device (default `http://127.0.0.1:8500`; `false` for none) |
| `panel_token` | its access token (default: `webui.access_token` from `mycroft.conf`) |
| `panel_public_url` | where a browser can open the panel, for "Open control panel" in Home Assistant (optional) |

Restart OVOS' PHAL service. The device then appears in Home Assistant under
**Settings → Devices & services → Discovered**. Add it there, once. The plugin
keeps the device's id in `~/.local/state/ovos-phal-plugin-ha-link/device_id`,
so it stays the same device.

## In the control panel

With [ovos-control-panel](https://github.com/OpenVoiceOS/ovos-control-panel) on the device, the plugin brings
its own page, **Home Assistant**, with the everyday pages. It shows how the link is (connected, waiting to be
added in Home Assistant, cannot reach it, token refused …) and sets the address, the token, the name and the rest
above. They are saved in `mycroft.conf` with the panel's backups, and the link connects again with them at once,
without a restart. The token is never shown again: leave the field empty to keep it. HiveMind satellites are
listed, and one that is gone for good can be removed from Home Assistant there. Saving needs the panel's access
token. The page is in Catalan, Danish, Dutch, English, French, Galician, German, Italian, Portuguese, Spanish and
Basque.

Behind it, on the bus:

| Message | Data | Answer (`<message>.response`) |
|---|---|---|
| `ovos.ha_link.status` | | the settings (`url`, `name`, `model`, `panel_public_url`, `satellites_on`, `token_set`; never the token), `state` (`off`, `connecting`, `unreachable`, `refused`, `pending`, `ready`, `displaced`, `removed`), `error`, `ha_version`, `forward`, `satellites` |
| `ovos.ha_link.reload` | `config`: the plugin's section | the same, with the link started again |

## How it works

It uses the integration's protocol ([docs/protocol.md](https://github.com/andlo/ha-ovos-integration/blob/master/docs/protocol.md)):
`ovos/connect` over Home Assistant's WebSocket API, then bus messages both
ways. What Home Assistant sends goes on the bus as it is. Of the device's own
messages, only the types Home Assistant asks for (the `forward` list) are
sent. It connects again by itself when Home Assistant restarts.

## The control panel

When [ovos-control-panel](https://github.com/OpenVoiceOS/ovos-control-panel)
runs on the device, Home Assistant also gets what it can set: language, units,
voice, speech to text, wake word, skills on and off, updates, restart and
reboot ([ha-ovos-integration#15](https://github.com/andlo/ha-ovos-integration/issues/15)).
Home Assistant's requests come through this link, and the plugin calls the
panel here on the device, so the panel never has to be opened on the network.
Restart and reboot need the panel's access token (`webui.access_token`).
The panel's backup goes to Home Assistant too (its **Take backup** button):
as a file in one message, base64, up to about 3 MB.

## A hub: HiveMind satellites in Home Assistant too

When this OVOS is a HiveMind master (hivemind-core, e.g.
[Emkay Home](https://github.com/emkay-voice/emkay-home)), its satellites
come along: each one becomes its own device in Home Assistant, connected via
this one, with its own volume, listening on/off, state and "say"
([ha-ovos-integration#12](https://github.com/andlo/ha-ovos-integration/issues/12)).
Nothing to set up on the satellites, and they don't log in to Home Assistant.

- A satellite is added when it first sends something after connecting (Emkay
  sends its volume and listening state right away), named as in hivemind-core.
- It is unavailable while disconnected. Send `ovos.ha_link.satellite.forget`
  with `{"key": "<its access key>"}` (or Emkay Home's `{"id": …}`) when it is
  removed, and its device goes from Home Assistant.
- What Home Assistant asks of a satellite goes through hivemind-core
  (`hive.send.downstream`), so this device's own PHAL never takes it as
  meant for itself.
- The satellite must be allowed to answer: hivemind-core only lets the message
  types a client is allowed onto the bus, by default little more than
  utterances. Allow `mycroft.volume.get.response` and
  `mycroft.mic.get_status.response` (`hivemind-core allow-msg`). Emkay Home
  does it for its Emkays.

Turn it off with `"satellites": false`.

## Home Assistant's Assist

With [ovos-assist-pipeline-plugin](https://github.com/emkay-voice/ovos-assist-pipeline-plugin)
in OVOS' intent pipeline, what Home Assistant's Assist recognizes ("turn on
the light in the kitchen") goes to Home Assistant through this link. The
link answers that plugin's `ovos.assist.*` messages. Home Assistant knows
which device asked, so it knows the room. You can use it instead of
skill-homeassistant, with no token to set there.

## Coming

- What the device sends out on the network ([#16](https://github.com/andlo/ha-ovos-integration/issues/16)).
- The device as an Assist satellite ([#13](https://github.com/andlo/ha-ovos-integration/issues/13)).

## License

Apache-2.0
