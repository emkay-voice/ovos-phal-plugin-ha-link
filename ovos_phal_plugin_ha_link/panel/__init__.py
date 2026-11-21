"""The Home Assistant page in ovos-control-panel.

The control panel finds this page through the ``opm.control_panel.page``
entry point (see pyproject.toml and the panel's docs/pages.md). It shows how
the link to Home Assistant is, sets its address, token and name, and lists
the HiveMind satellites that came along. Nothing here is loaded by the
plugin itself, so the plugin needs no web packages; the panel brings those.
"""


def routes(r):
    # Imported here, so reading PAGE (the panel's registry, this plugin's
    # tests) needs no web framework; only the panel calls this.
    from ovos_phal_plugin_ha_link.panel.api import routes as add
    add(r)


PAGE = {
    "api_version": 1,
    "id": "home-assistant",
    "path": "/home-assistant",
    "title": {"key": "haLink.title", "label": "Home Assistant"},
    # how the device is connected: with the everyday pages, after Network (50)
    "group": "everyday",
    "order": 52,
    "needs": ["ovos-phal-plugin-ha-link"],
    "html": "page.html",
    "icon": "icon.svg",
    "i18n": "i18n",
    "routes": routes,
}
