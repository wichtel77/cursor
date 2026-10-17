# -*- coding: utf-8 -*-

import gettext
import json
import os

try:
    from Components.Language import language
except ImportError:
    language = None

PLUGIN_LANGUAGE_DOMAIN = "JellyDreamModern"
PLUGIN_LANGUAGE_PATH = os.path.join(
    os.path.dirname(os.path.realpath(__file__)),
    "locale"
)
JELLYDREAM_CONFIG_FILE = "/etc/enigma2/jellydreammodern.json"
JELLYDREAM_UI_LANGUAGES = (
    "de", "en", "fr", "it", "es", "pl", "nl", "tr", "ar", "ru"
)
JELLYDREAM_UI_LANGUAGE_FALLBACK = "en"

_translation = gettext.NullTranslations()
_ORIGINAL_LANGUAGE_ENV = os.environ.get("LANGUAGE")
_HAD_LANGUAGE_ENV = "LANGUAGE" in os.environ


def jellydream_box_language_code():
    try:
        if language is None:
            return ""

        language_code = str(language.getLanguage() or "").strip()
    except Exception:
        return ""

    if not language_code:
        return ""

    return language_code.replace("-", "_").split("_")[0].lower()


def jellydream_read_ui_language_setting():
    try:
        with open(
            JELLYDREAM_CONFIG_FILE,
            "r",
            encoding="utf-8"
        ) as config_file:
            config_data = json.load(config_file)

        if not isinstance(config_data, dict):
            return "auto"

        return str(
            config_data.get("ui_language", "auto")
            or "auto"
        ).strip().lower()

    except Exception:
        return "auto"


def jellydream_resolve_ui_language(setting=None):
    if setting is None:
        setting = jellydream_read_ui_language_setting()

    setting = str(setting or "auto").strip().lower()

    if setting in JELLYDREAM_UI_LANGUAGES:
        return setting

    box_language = jellydream_box_language_code()

    if box_language in JELLYDREAM_UI_LANGUAGES:
        return box_language

    return JELLYDREAM_UI_LANGUAGE_FALLBACK


def localeInit(setting=None):
    global _translation

    lang = jellydream_resolve_ui_language(setting)
    languages = [lang]

    if lang != JELLYDREAM_UI_LANGUAGE_FALLBACK:
        languages.append(JELLYDREAM_UI_LANGUAGE_FALLBACK)

    try:
        if _HAD_LANGUAGE_ENV:
            os.environ["LANGUAGE"] = _ORIGINAL_LANGUAGE_ENV
        elif "LANGUAGE" in os.environ:
            del os.environ["LANGUAGE"]
    except Exception:
        pass

    try:
        _translation = gettext.translation(
            PLUGIN_LANGUAGE_DOMAIN,
            localedir=PLUGIN_LANGUAGE_PATH,
            languages=languages,
            fallback=True
        )
    except Exception:
        _translation = gettext.NullTranslations()

    try:
        gettext.bindtextdomain(
            PLUGIN_LANGUAGE_DOMAIN,
            PLUGIN_LANGUAGE_PATH
        )
    except Exception:
        pass

    return lang


def _(txt):
    if txt is None:
        return ""

    text = str(txt)
    if not text:
        return ""

    try:
        translated = _translation.gettext(text)
    except Exception:
        translated = text

    if translated:
        return translated

    return text


localeInit()

if language is not None:
    try:
        language.addCallback(localeInit)
    except Exception:
        pass
