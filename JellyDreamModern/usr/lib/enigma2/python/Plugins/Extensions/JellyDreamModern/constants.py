# -*- coding: utf-8 -*-

import os
import threading

from Components.SystemInfo import BoxInfo


JELLYDREAM_VERSION = "1.0.0-r62"
JELLYDREAM_USER_AGENT = "JellyDreamModern/%s" % JELLYDREAM_VERSION

_JSON_LOCKS_GUARD = threading.Lock()
_JSON_PATH_LOCKS = {}


class JellyDreamLRUCache:
    def __init__(self, max_size=100):
        self.max_size = max(5, int(max_size))
        self.cache = {}
        self.order = []
        self.lock = threading.RLock()

    def get(self, key, default=None):
        with self.lock:
            if key not in self.cache:
                return default

            try:
                self.order.remove(key)
            except ValueError:
                pass

            self.order.append(key)
            return self.cache[key]

    def set(self, key, value):
        with self.lock:
            if key in self.cache:
                try:
                    self.order.remove(key)
                except ValueError:
                    pass
            else:
                while (
                    len(self.cache) >= self.max_size
                    and self.order
                ):
                    oldest_key = self.order.pop(0)
                    self.cache.pop(oldest_key, None)

            self.cache[key] = value
            self.order.append(key)

    def delete(self, key):
        with self.lock:
            self.cache.pop(key, None)

            try:
                self.order.remove(key)
            except ValueError:
                pass

    def clear(self):
        with self.lock:
            self.cache.clear()
            self.order = []


class JellyDreamScopedCacheKey(str):
    """
    Markiert einen Cache-Key als bereits mit dem aktuellen Namespace
    versehen.

    Dadurch müssen jellydream_cache_get() und jellydream_cache_set()
    den Namespace nicht noch einmal ermitteln.
    """
    pass


def jellydream_get_json_lock(path):
    normalized_path = os.path.abspath(
        str(path or "")
    )

    with _JSON_LOCKS_GUARD:
        lock = _JSON_PATH_LOCKS.get(
            normalized_path
        )

        if lock is None:
            lock = threading.RLock()
            _JSON_PATH_LOCKS[normalized_path] = lock

        return lock

        
# ==================== KONSTANTEN ====================
LOG_FILE = "/tmp/jellydream.log"

LOG_MAX_SIZE = 1024 * 1024  # 1MB

DEFAULT_LOG_LEVEL = "INFO"  # DEBUG, INFO, WARNING, ERROR


TICKS_PER_SECOND = 10000000

ENIGMA_SEEK_CLOCK = 90000

SERVICE_REFERENCE = 4097



def jellydream_get_lcd_size():
    display_type = BoxInfo.getItem("displaytype")

    sizes = {
        "bwlcd96": (96, 64),
        "bwlcd128": (128, 64),
        "bwlcd140": (140, 32),
        "bwlcd255": (255, 64),
        "colorlcd220": (220, 176),
        "colorlcd240": (240, 80),
        "colorlcd128": (128, 64),
        "colorlcd400": (400, 240),
        "colorlcd390": (390, 240),
        "colorlcd480": (480, 320),
        "colorlcd720": (720, 576),
        "colorlcd800": (800, 480),
    }

    return sizes.get(display_type)

    
# Timeouts (in Sekunden)
TIMEOUT_SHORT = 8

TIMEOUT_NORMAL = 12

TIMEOUT_LONG = 20

TIMEOUT_IMAGE = 25

TIMEOUT_MIDDLE = 10

TIMEOUT_POST = 8


# Jellyfin-TMDb-Plugin. RemoteSearch/Season gibt es nicht, und
# RemoteSearch/Episode lässt Overview absichtlich leer. Staffel- und
# Folgenbeschreibungen kommen deshalb direkt von TMDB, mit demselben
# Schlüssel den Jellyfin ohne Extra-Token im Client nutzt.
JELLYDREAM_TMDB_API_KEY = "4219e299c89411838049ab0dab19ebd5"

JELLYDREAM_TMDB_API_BASE = "https://api.themoviedb.org/3"

MAIN_CONNECTION_WATCHDOG_MS = 10000


# Vorgabe für die Servereingabe im Login-Screen.
DEFAULT_JELLYFIN_SERVER_INPUT = "https://"


# Es können maximal drei Jellyfin-Serverprofile gespeichert werden.
# Immer nur eines dieser Profile wird als aktive Sitzung verwendet.
MAX_JELLYFIN_SERVER_PROFILES = 3


# Retry-Konfiguration für fehlgeschlagene Detailabrufe.
# Nach jedem Fehler wird die Wartezeit verdoppelt, aber maximal
# bis DETAIL_RETRY_MAX_SECONDS.
DETAIL_RETRY_BASE_SECONDS = 2

DETAIL_RETRY_MAX_SECONDS = 60


# UI-Konstanten
ITEMS_PER_PAGE_GRID = 6

ITEMS_PER_PAGE_EPISODES = 8

SEARCH_HISTORY_MAX = 20

# Der Backdrop der Hauptseite wird frühestens 180 ms nach der
# letzten Navigation aktualisiert.
MAIN_BACKGROUND_DEBOUNCE_MS = 180


# Dateipfade
SEARCH_HISTORY_FILE = "/etc/enigma2/jellydreammodern_search_history.json"

CONFIG_FILE = "/etc/enigma2/jellydreammodern.json"

DEVICE_ID_FILE = "/etc/enigma2/jellydreammodern_device_id.txt"



# Vollständige Standardkonfiguration.
#
# Diese Werte werden:
# - beim ersten Pluginstart angelegt,
# - bei bestehenden unvollständigen Dateien ergänzt,
# - bei leeren Einstellungswerten eingesetzt.
#
# Zugangsdaten sind absichtlich nicht enthalten. Sie werden erst nach
# erfolgreicher Jellyfin-Anmeldung gespeichert.
JELLYDREAM_CONFIG_DEFAULTS = {
    "max_res": "4K",
    "stream_mode": "direct",
    "player_service_reference": "4097",
    "stream_protocol": "ts",

    "allow_ac3_direct": "0",
    "allow_eac3_direct": "1",
    "allow_truehd_direct": "0",
    "allow_dts_direct": "0",
    "preferred_audio_codec": "aac",
    "default_audio_language": "de",

    "video_bitrate": "8000000",
    "update_refresh": "150",

    "dynamic_backdrops": "1",
    "image_worker_count": "2",

    "library_auto_update": "1",
    "library_update_hour": "04",
    "library_update_minute": "00",
    "library_sort": "name",
    "library_played_filter": "all",
    "music_sort": "name",
    "music_played_filter": "all",

    "show_in_mainmenu": "0",
    "ui_language": "auto",
    "log_level": "INFO",

    "verify_ssl": "1",
    "custom_ca_file": "",
    "spinner_choice": "",
    # Nummer des aktuell aktiven Serverprofils:
    # 0 = Server 1
    # 1 = Server 2
    # 2 = Server 3
    "active_server_profile": 0
}

PLUGIN_DIR = os.path.dirname(__file__)

BACKGROUND_FILE = os.path.join(PLUGIN_DIR, "images", "covers", "default_background.jpg")

CURRENT_SKIN_DIRECTORY = ""

LIBRARY_CACHE_FILE_PREFIX = (
    "/etc/enigma2/jellydreammodern_library_cache"
)

# Alte Cache-Namespaces bleiben ausreichend lange erhalten, damit ein
# kurzfristiger Server-/Benutzerwechsel nicht unnötig alles neu lädt.
# Danach werden nicht mehr verwendete Bibliotheks- und Bildcaches täglich
# durch den Scheduler entfernt.
NAMESPACE_CACHE_MAX_AGE_SECONDS = 30 * 24 * 60 * 60

    
SYSTEM_SPINNER_DIRECTORIES = (
    "/usr/share/enigma2/spinner",
    "/usr/share/enigma2/skin_default/spinner",
    "/usr/share/enigma2/Spinner",
)


SYSTEM_SPINNER_COLLECTION_DIRECTORIES = (
    "/usr/share/enigma2/spinner",
    "/usr/share/enigma2/Spinner",
)


SPINNER_FRAME_DELAY = 90

FALLBACK_RESUME_COVER = os.path.join(PLUGIN_DIR, "images", "covers", "no_image_310.png")

FALLBACK_HOME_COVER = os.path.join(PLUGIN_DIR, "images", "covers", "no_image_200.png")

# ==================== ENDE KONSTANTEN ====================

INFOBAR_WIDGETS = [
    "infobar_background",
    "infobar_cover",
    "jelly_video_title",
    "time_text",
    "remaining_time_text",
    "clock",
    "date",
    "codec_info",
    "stream_debug",
    "progress"
]


CACHE_DIRS = {
    "cover": "/tmp/jellydream_cache"
}



JOB_PRIORITY_PLAYBACK = 0

JOB_PRIORITY_API_INTERACTIVE = 10

JOB_PRIORITY_IMAGE_NORMAL = 10

JOB_PRIORITY_MAINTENANCE = 50

IMAGE_NEGATIVE_CACHE_TTL_404 = 6 * 60 * 60

IMAGE_NEGATIVE_CACHE_TTL_410 = 6 * 60 * 60
