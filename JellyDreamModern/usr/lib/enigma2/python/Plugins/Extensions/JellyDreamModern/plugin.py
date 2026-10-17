# -*- coding: utf-8 -*-

import json
import os

from Plugins.Plugin import PluginDescriptor

from . import _
from .main import (
    JellyDreamMain,
    jellydream_initialize_plugin_services,
    jellydream_refresh_library_cache_if_stale_async,
    jellydream_stop_library_scheduler
)


CONFIG_FILE = "/etc/enigma2/jellydreammodern.json"

PLUGIN_NAME = "JellyDream Modern"
PLUGIN_DESCRIPTION = _("Jellyfin Client für Enigma2")
PLUGIN_ICON = "jellyfin.png"
PLUGIN_MENU_ID = "JellyDreamModern"


def jellydream_session_start(reason, session=None, **kwargs):
    try:
        if reason == 0:
            initialized = jellydream_initialize_plugin_services()

            if initialized:
                jellydream_refresh_library_cache_if_stale_async()

            return

        jellydream_stop_library_scheduler()

    except Exception as e:
        print(
            "[JellyDream] Fehler beim Starten oder Stoppen "
            "der Plugin-Dienste: %s" % e
        )


def get_mainmenu_setting():
    try:
        if not os.path.exists(CONFIG_FILE):
            return "0"

        with open(
            CONFIG_FILE,
            "r",
            encoding="utf-8"
        ) as config_file:
            data = json.load(config_file)

        if not isinstance(data, dict):
            return "0"

        return str(
            data.get(
                "show_in_mainmenu",
                "0"
            )
        )

    except Exception as e:
        print(
            "[JellyDream] Hauptmenü-Einstellung "
            "konnte nicht gelesen werden: %s" % e
        )
        return "0"


def main(session, **kwargs):
    if not session or isinstance(session, str):
        session = kwargs.get("session")

    if not session:
        print(
            "[JellyDream] Keine gültige "
            "Enigma2-Session vorhanden"
        )
        return

    try:
        session.open(JellyDreamMain)

    except Exception as e:
        print(
            "[JellyDream] Hauptbildschirm konnte "
            "nicht geöffnet werden: %s" % e
        )


def mainmenu(menu_id, **kwargs):
    if menu_id != "mainmenu":
        return []

    return [
        (
            PLUGIN_NAME,
            main,
            PLUGIN_MENU_ID,
            50
        )
    ]


def Plugins(**kwargs):
    descriptors = [
        PluginDescriptor(
            name=PLUGIN_NAME,
            description=PLUGIN_DESCRIPTION,
            where=PluginDescriptor.WHERE_PLUGINMENU,
            fnc=main,
            icon=PLUGIN_ICON
        ),
        PluginDescriptor(
            name=PLUGIN_NAME,
            description=PLUGIN_DESCRIPTION,
            where=PluginDescriptor.WHERE_EXTENSIONSMENU,
            fnc=main,
            icon=PLUGIN_ICON
        ),
        PluginDescriptor(
            where=PluginDescriptor.WHERE_SESSIONSTART,
            fnc=jellydream_session_start
        )
    ]

    if get_mainmenu_setting() == "1":
        descriptors.append(
            PluginDescriptor(
                name=PLUGIN_NAME,
                description=PLUGIN_DESCRIPTION,
                where=PluginDescriptor.WHERE_MENU,
                fnc=mainmenu,
                needsRestart=True
            )
        )

    return descriptors