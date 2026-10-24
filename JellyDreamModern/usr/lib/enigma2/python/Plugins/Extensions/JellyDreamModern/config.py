# -*- coding: utf-8 -*-

import copy
import hashlib
import json
import os
import re
import threading
import time
import uuid

from . import _

from .constants import (
    CACHE_DIRS,
    CONFIG_FILE,
    DEVICE_ID_FILE,
    JELLYDREAM_CONFIG_DEFAULTS,
    JellyDreamScopedCacheKey,
    LIBRARY_CACHE_FILE_PREFIX,
    MAX_JELLYFIN_SERVER_PROFILES,
    NAMESPACE_CACHE_MAX_AGE_SECONDS,
    SEARCH_HISTORY_FILE,
    SEARCH_HISTORY_MAX,
    SERVICE_REFERENCE,
    jellydream_get_json_lock
)
from .log import jd_log, jellydream_set_log_level_from_config

        
_CONFIG_CACHE = None

_CONFIG_LOCK = threading.RLock()

_CACHE_NAMESPACE_LOCK = threading.RLock()

_CACHE_NAMESPACE_VALUE = None

_DEVICE_ID_LOCK = threading.RLock()


def jellydream_player_service_choices():
    choices = [
        ("4097", _("Enigma2 IPTV (4097)"))
    ]

    if os.path.exists("/usr/bin/gstplayer"):
        choices.append(
            ("5001", _("GStreamer / gstplayer (5001)"))
        )

    if os.path.exists("/usr/bin/exteplayer3"):
        choices.append(
            ("5002", _("ExtePlayer3 (5002)"))
        )

    return choices


def jellydream_get_service_reference():

    try:
        configured_value = str(
            read_config().get("player_service_reference", "4097")
        ).strip()

        available_values = [
            value for value, label in jellydream_player_service_choices()
        ]

        if configured_value in available_values:
            service_reference = int(configured_value)

            jd_log(
                "JellyDream player selected: service reference %s"                % service_reference,
                level="DEBUG"
            )

            return service_reference

        jd_log(
            "Selected service reference %s is not available -> using 4097"           % configured_value,
            level="WARNING"
        )

    except Exception as e:
        jd_log(
            "Player service reference could not be read: %s" % e,
            level="WARNING"
        )

    return SERVICE_REFERENCE


def jellydream_library_cache_file():
    return "%s_%s.json" % (
        LIBRARY_CACHE_FILE_PREFIX,
        jellydream_cache_namespace()
    )


_SEARCH_CACHE_LOCK = threading.RLock()

_SEARCH_CACHE_BY_SCOPE = {}

        
def jellydream_set_search_cache(cache_type, items):
    scope = jellydream_cache_namespace()

    with _SEARCH_CACHE_LOCK:
        scope_cache = _SEARCH_CACHE_BY_SCOPE.setdefault(
            scope,
            {
                "movies": [],
                "series": []
            }
        )

        scope_cache[cache_type] = (
            list(items)
            if isinstance(items, list)
            else []
        )


def jellydream_patch_search_cache_userdata(item_id, user_data):
    item_id = str(item_id or "").strip()

    if not item_id or not isinstance(user_data, dict):
        return

    scope = jellydream_cache_namespace()

    with _SEARCH_CACHE_LOCK:
        scope_cache = _SEARCH_CACHE_BY_SCOPE.get(scope)

        if not isinstance(scope_cache, dict):
            return

        for cache_type in ("movies", "series"):
            for cached in scope_cache.get(cache_type) or []:
                if not isinstance(cached, dict):
                    continue

                cached_id = str(
                    cached.get("Id") or cached.get("id") or ""
                ).strip()

                if cached_id != item_id:
                    continue

                existing = cached.get("UserData")

                if not isinstance(existing, dict):
                    existing = {}
                    cached["UserData"] = existing

                existing.update(user_data)


def jellydream_get_search_cache_items():
    scope = jellydream_cache_namespace()

    with _SEARCH_CACHE_LOCK:
        scope_cache = _SEARCH_CACHE_BY_SCOPE.get(
            scope,
            {}
        )

        movies = list(
            scope_cache.get("movies") or []
        )

        series = list(
            scope_cache.get("series") or []
        )

    return movies + series


def add_search_history(term):
    term = str(term or "").strip()

    if not term:
        return False

    history_lock = jellydream_get_json_lock(
        SEARCH_HISTORY_FILE
    )

    with history_lock:
        history = read_search_history()
        lowered = term.lower()

        history = [
            entry
            for entry in history
            if entry.lower() != lowered
        ]

        history.insert(
            0,
            term
        )

        return write_search_history(
            history
        )

    
def write_search_history(history_list):
    try:
        if not isinstance(history_list, list):
            return False
        cleaned = [str(x).strip() for x in history_list if str(x).strip()][:SEARCH_HISTORY_MAX]
        return jellydream_write_json_atomic(SEARCH_HISTORY_FILE, cleaned)
    except Exception as e:
        jd_log(f"Error while writing search history: {e}", level="WARNING")
        return False

        
def read_search_history():
    try:
        data = jellydream_read_json_file(
            SEARCH_HISTORY_FILE,
            []
        )

        if not isinstance(data, list):
            return []

        return [
            str(entry).strip()
            for entry in data
            if str(entry).strip()
        ][:SEARCH_HISTORY_MAX]

    except Exception as e:
        jd_log(
            "Error while reading search history: %s" % e,
            level="WARNING"
        )
        return []


def jellydream_config_value_is_empty(value):
    """
    Prüft, ob ein gespeicherter Konfigurationswert als leer gilt.

    Zahlen und boolesche Werte gelten nicht als leer. Dadurch bleibt
    beispielsweise der numerische Wert 0 erhalten.
    """
    if value is None:
        return True

    if isinstance(value, str):
        return not value.strip()

    return False



def jellydream_merge_config_defaults(config_data):
    """
    Ergänzt fehlende oder leere Einstellungswerte.

    Unbekannte Schlüssel und Jellyfin-Zugangsdaten bleiben unverändert
    erhalten.

    Rückgabe:
        merged_config, changed
    """
    if isinstance(config_data, dict):
        merged_config = copy.deepcopy(config_data)
    else:
        merged_config = {}

    changed = not isinstance(config_data, dict)

    for setting_name, default_value in (
        JELLYDREAM_CONFIG_DEFAULTS.items()
    ):
        if (
            setting_name not in merged_config
            or jellydream_config_value_is_empty(
                merged_config.get(setting_name)
            )
        ):
            new_value = copy.deepcopy(default_value)

            if (
                setting_name not in merged_config
                or merged_config.get(setting_name) != new_value
            ):
                changed = True

            merged_config[setting_name] = new_value

    return merged_config, changed



def jellydream_ensure_config_file():
    """
    Legt die Konfigurationsdatei beim ersten Start vollständig an.

    Bestehende Konfigurationen werden automatisch ergänzt:
    - fehlende Einstellungen erhalten ihren Standardwert,
    - leere Einstellungen erhalten ihren Standardwert,
    - Zugangsdaten und unbekannte Schlüssel bleiben erhalten.
    """
    with _CONFIG_LOCK:
        config_file_exists = os.path.isfile(
            CONFIG_FILE
        )

        config_data = jellydream_read_json_file(
            CONFIG_FILE,
            {}
        )

        merged_config, config_changed = (
            jellydream_merge_config_defaults(
                config_data
            )
        )

        if not config_file_exists or config_changed:
            if not jellydream_write_json_atomic(
                CONFIG_FILE,
                merged_config
            ):
                jd_log(
                    "Configuration file could not be fully initialized",
                    level="ERROR"
                )

                return merged_config

            try:
                os.chmod(
                    CONFIG_FILE,
                    0o600
                )
            except Exception:
                pass

            if config_file_exists:
                jd_log(
                    "Missing configuration settings were added",
                    level="INFO"
                )
            else:
                jd_log(
                    "Configuration file was created with default values",
                    level="INFO"
                )

        return merged_config



def read_config():
    global _CONFIG_CACHE

    with _CONFIG_LOCK:
        if _CONFIG_CACHE is None:
            try:
                # Beim ersten Aufruf wird die Datei vollständig angelegt.
                # Bestehende unvollständige Dateien werden migriert.
                data = jellydream_ensure_config_file()

                if not isinstance(data, dict):
                    data = copy.deepcopy(
                        JELLYDREAM_CONFIG_DEFAULTS
                    )

                # Zusätzliche Absicherung für den RAM-Cache.
                data, config_changed = (
                    jellydream_merge_config_defaults(
                        data
                    )
                )

                if config_changed:
                    jellydream_write_json_atomic(
                        CONFIG_FILE,
                        data
                    )

                # Der interne Cache bekommt eine eigene Kopie.
                _CONFIG_CACHE = copy.deepcopy(data)

                # Log-Level direkt aus der vollständigen Konfiguration
                # in den RAM übernehmen.
                jellydream_set_log_level_from_config(
                    _CONFIG_CACHE
                )

            except Exception as e:
                try:
                    print(
                        "[JellyDream][WARNING] "
                        "Fehler beim Initialisieren der "
                        "Konfigurationsdatei: %s"
                        % e
                    )
                except Exception:
                    pass

                # Auch bei einem unerwarteten Lesefehler mit einer
                # vollständigen Standardkonfiguration weiterarbeiten.
                _CONFIG_CACHE = copy.deepcopy(
                    JELLYDREAM_CONFIG_DEFAULTS
                )

                jellydream_set_log_level_from_config(
                    _CONFIG_CACHE
                )

        # Niemals die interne Cache-Referenz zurückgeben.
        return copy.deepcopy(_CONFIG_CACHE)


def write_config(data_dict):
    global _CONFIG_CACHE

    if not isinstance(data_dict, dict):
        return False

    with _CONFIG_LOCK:
        try:
            # Der Aufrufer behält seine eigene Datenstruktur.
            config_copy = copy.deepcopy(data_dict)

            # Vor jedem Speichern fehlende oder leere Einstellungswerte ergänzen.
            # Zugangsdaten und unbekannte Schlüssel bleiben dabei erhalten.
            config_copy, config_changed = (
                jellydream_merge_config_defaults(
                    config_copy
                )
            )

            if not jellydream_write_json_atomic(
                CONFIG_FILE,
                config_copy
            ):
                return False

            try:
                os.chmod(
                    CONFIG_FILE,
                    0o600
                )
            except Exception:
                pass

            # Auch der RAM-Cache erhält seine eigene Referenz.
            _CONFIG_CACHE = copy.deepcopy(config_copy)

            # Änderungen am Log-Level werden sofort in den separaten
            # RAM-Wert übernommen. jd_log() muss dadurch weder read_config()
            # noch deepcopy() aufrufen.
            jellydream_set_log_level_from_config(
                config_copy
            )

            # Server oder Benutzer können sich durch Login / Logout ändern.
            # Deshalb wird der Namespace exakt hier aktualisiert – und nicht
            # bei jedem Bild-, Detail- oder Cache-Zugriff neu berechnet.
            jellydream_set_cache_namespace_from_config(
                config_copy
            )

            jd_log(
                "Configuration file written, RAM cache and cache namespace updated.",
                level="INFO"
            )

            return True

        except Exception as e:
            jd_log(
                "Error while writing the configuration file: %s" % e,
                level="ERROR"
            )
            return False


def clear_config():
    """
    Löscht ausschließlich das aktuell aktive Serverprofil.

    Andere gespeicherte Serverplätze bleiben erhalten und können später
    erneut über den Login-Screen aktiviert werden.
    """
    try:
        current_data = copy.deepcopy(
            read_config() or {}
        )

        profiles, active_index = (
            jellydream_normalize_server_profiles(
                current_data
            )
        )

        if (
            0 <= active_index
            < len(profiles)
        ):
            profiles[active_index] = {}

        current_data["server_profiles"] = profiles
        current_data["active_server_profile"] = (
            active_index
        )

        # Die bisherigen aktiven Zugangsschlüssel vollständig entfernen.
        for key in (
            "access_token",
            "token",
            "user_id",
            "server_url",
            "server",
            "username"
        ):
            current_data.pop(key, None)

        if not write_config(current_data):
            jd_log(
                "Logout failed: configuration file could not be written.",
                level="ERROR"
            )
            return False

        jd_log(
            "Logout succeeded: server profile %s was removed. Other server profiles are kept."
            % (
                active_index + 1
            ),
            level="INFO"
        )

        return True

    except Exception as e:
        jd_log(
            "Error while deleting the active server profile: %s" % e,
            level="ERROR"
        )
        return False

            
def jellydream_write_json_atomic(path, data):
    path = os.path.abspath(
        str(path or "")
    )

    if not path:
        return False

    path_lock = jellydream_get_json_lock(path)

    tmp_path = "%s.tmp.%s.%s" % (
        path,
        threading.current_thread().ident,
        uuid.uuid4().hex
    )

    with path_lock:
        try:
            parent_directory = os.path.dirname(path)

            if parent_directory and not os.path.exists(
                parent_directory
            ):
                os.makedirs(
                    parent_directory,
                    exist_ok=True
                )

            with open(
                tmp_path,
                "w",
                encoding="utf-8"
            ) as json_file:
                json.dump(
                    data,
                    json_file,
                    indent=4,
                    ensure_ascii=False
                )

                json_file.flush()

                try:
                    os.fsync(json_file.fileno())
                except Exception:
                    pass

            os.replace(
                tmp_path,
                path
            )

            return True

        except Exception as e:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass

            jd_log(
                "Atomic JSON write failed for %s: %s"                % (
                    path,
                    e
                ),
                level="ERROR"
            )

            return False

            
def jellydream_read_json_file(path, default=None):
    path = os.path.abspath(
        str(path or "")
    )

    if not path:
        return copy.deepcopy(default)

    path_lock = jellydream_get_json_lock(path)

    with path_lock:
        try:
            if not os.path.exists(path):
                return copy.deepcopy(default)

            with open(
                path,
                "r",
                encoding="utf-8"
            ) as json_file:
                return json.load(json_file)

        except Exception as e:
            jd_log(
                "JSON file could not be read: %s | %s"
                % (
                    path,
                    e
                ),
                level="WARNING"
            )

            return copy.deepcopy(default)

            
def jellydream_device_id():
    """
    Liefert die dauerhafte Jellyfin-Device-ID dieser Box.

    Der komplette Lese-/Erzeuge-/Schreibvorgang ist durch ein Lock
    geschützt. Dadurch können parallele API- und Bild-Worker nicht
    unterschiedliche Device-IDs erzeugen oder die Datei überschreiben.
    """
    with _DEVICE_ID_LOCK:
        try:
            if os.path.exists(DEVICE_ID_FILE):
                with open(
                    DEVICE_ID_FILE,
                    "r",
                    encoding="utf-8"
                ) as device_file:
                    value = (
                        device_file.read() or ""
                    ).strip()

                    if value:
                        return value

        except Exception as e:
            jd_log(
                "Device-ID file could not be read: %s"
                % e,
                level="WARNING"
            )

        value = uuid.uuid4().hex

        try:
            parent_directory = os.path.dirname(
                DEVICE_ID_FILE
            )

            if (
                parent_directory
                and not os.path.exists(parent_directory)
            ):
                os.makedirs(
                    parent_directory,
                    exist_ok=True
                )

            # Die Datei wird erst nach vollständigem Schreiben atomar
            # an ihre endgültige Position verschoben.
            tmp_path = "%s.tmp.%s.%s" % (
                DEVICE_ID_FILE,
                threading.current_thread().ident,
                uuid.uuid4().hex
            )

            with open(
                tmp_path,
                "w",
                encoding="utf-8"
            ) as device_file:
                device_file.write(value)
                device_file.flush()

                try:
                    os.fsync(device_file.fileno())
                except Exception:
                    pass

            os.replace(
                tmp_path,
                DEVICE_ID_FILE
            )

            try:
                os.chmod(
                    DEVICE_ID_FILE,
                    0o600
                )
            except Exception:
                pass

            return value

        except Exception as e:
            try:
                if "tmp_path" in locals() and os.path.exists(
                    tmp_path
                ):
                    os.remove(tmp_path)
            except Exception:
                pass

            jd_log(
                "Device-ID file could not be written: %s"
                % e,
                level="WARNING"
            )

            # Auch wenn der permanente Schreibzugriff scheitert, erhält
            # der laufende Prozess eine konsistente ID.
            return value

    
# ==============================
# ZENTRALE JELLYFIN-DATEN-ANKER 
# ==============================

def jellydream_normalize_server_profiles(config_data=None):
    """
    Liefert immer exakt drei Serverplätze zurück.

    Bestehende ältere Einzelserver-Konfigurationen werden automatisch
    als Serverplatz 1 bereitgestellt.

    Rückgabe:
        profiles, active_index
    """
    try:
        if not isinstance(config_data, dict):
            config_data = read_config() or {}

        profiles = [
            {}
            for _ in range(MAX_JELLYFIN_SERVER_PROFILES)
        ]

        raw_profiles = config_data.get(
            "server_profiles"
        )

        if isinstance(raw_profiles, list):
            for index in range(
                min(
                    len(raw_profiles),
                    MAX_JELLYFIN_SERVER_PROFILES
                )
            ):
                raw_profile = raw_profiles[index]

                if not isinstance(raw_profile, dict):
                    continue

                server = str(
                    raw_profile.get("server_url")
                    or raw_profile.get("server")
                    or ""
                ).strip().rstrip("/")

                username = str(
                    raw_profile.get("username")
                    or ""
                ).strip()

                user_id = str(
                    raw_profile.get("user_id")
                    or ""
                ).strip()

                token = str(
                    raw_profile.get("access_token")
                    or raw_profile.get("token")
                    or ""
                ).strip()

                if server or username or user_id or token:
                    profiles[index] = {
                        "server_url": server,
                        "username": username,
                        "user_id": user_id,
                        "access_token": token
                    }

        # Kompatibilität mit der bisherigen Einzelserver-Konfiguration.
        #
        # Nur wenn noch kein Profil existiert, werden die bisherigen
        # obersten Zugangsdaten als Serverplatz 1 übernommen.
        if not any(
            bool(profile)
            for profile in profiles
        ):
            legacy_server = str(
                config_data.get("server_url")
                or config_data.get("server")
                or ""
            ).strip().rstrip("/")

            legacy_username = str(
                config_data.get("username")
                or ""
            ).strip()

            legacy_user_id = str(
                config_data.get("user_id")
                or ""
            ).strip()

            legacy_token = str(
                config_data.get("access_token")
                or config_data.get("token")
                or ""
            ).strip()

            if (
                legacy_server
                or legacy_username
                or legacy_user_id
                or legacy_token
            ):
                profiles[0] = {
                    "server_url": legacy_server,
                    "username": legacy_username,
                    "user_id": legacy_user_id,
                    "access_token": legacy_token
                }

        try:
            active_index = int(
                config_data.get(
                    "active_server_profile",
                    0
                )
            )
        except Exception:
            active_index = 0

        active_index = max(
            0,
            min(
                MAX_JELLYFIN_SERVER_PROFILES - 1,
                active_index
            )
        )

        return profiles, active_index

    except Exception as e:
        jd_log(
            "Server profiles could not be normalized: %s"
            % e,
            level="WARNING"
        )

        return (
            [
                {}
                for _ in range(
                    MAX_JELLYFIN_SERVER_PROFILES
                )
            ],
            0
        )



def jellydream_server_profile_is_logged_in(profile):
    """
    Prüft, ob ein Serverprofil eine vollständige gespeicherte Sitzung
    enthält.
    """
    try:
        if not isinstance(profile, dict):
            return False

        return bool(
            str(
                profile.get("server_url")
                or profile.get("server")
                or ""
            ).strip()
            and str(
                profile.get("access_token")
                or profile.get("token")
                or ""
            ).strip()
            and str(
                profile.get("user_id")
                or ""
            ).strip()
        )

    except Exception:
        return False



def jellydream_count_saved_server_profiles(
    config_data=None
):
    """
    Zählt Serverplätze, bei denen mindestens eine Serveradresse
    gespeichert ist.
    """
    try:
        profiles, active_index = (
            jellydream_normalize_server_profiles(
                config_data
            )
        )

        return sum(
            1
            for profile in profiles
            if str(
                profile.get("server_url")
                or ""
            ).strip()
        )

    except Exception:
        return 0



def jellydream_store_server_profile(
    config_data,
    profile_index,
    server,
    username,
    user_id,
    access_token
):
    """
    Speichert eine erfolgreiche Anmeldung in einem der drei Serverplätze
    und macht diesen Platz gleichzeitig zum aktiven Profil.

    Zusätzlich werden die bisherigen obersten Zugangsschlüssel gesetzt,
    damit der restliche bestehende Plugin-Code unverändert weiterarbeitet.
    """
    if not isinstance(config_data, dict):
        config_data = {}

    profiles, current_active_index = (
        jellydream_normalize_server_profiles(
            config_data
        )
    )

    try:
        profile_index = int(profile_index)
    except Exception:
        profile_index = 0

    profile_index = max(
        0,
        min(
            MAX_JELLYFIN_SERVER_PROFILES - 1,
            profile_index
        )
    )

    server = str(
        server or ""
    ).strip().rstrip("/")

    username = str(
        username or ""
    ).strip()

    user_id = str(
        user_id or ""
    ).strip()

    access_token = str(
        access_token or ""
    ).strip()

    profiles[profile_index] = {
        "server_url": server,
        "username": username,
        "user_id": user_id,
        "access_token": access_token
    }

    config_data["server_profiles"] = profiles
    config_data["active_server_profile"] = profile_index

    # Legacy-/Aktivschlüssel zuerst vollständig bereinigen.
    for key in (
        "server_url",
        "server",
        "username",
        "user_id",
        "access_token",
        "token"
    ):
        config_data.pop(key, None)

    # Das aktive Profil wieder in die bisherigen Schlüssel spiegeln.
    config_data["server_url"] = server
    config_data["username"] = username
    config_data["user_id"] = user_id
    config_data["access_token"] = access_token

    return config_data



def jellydream_activate_server_profile(profile_index):
    """
    Aktiviert ein bereits gespeichertes Serverprofil.

    Es wird kein paralleler Login erzeugt. Die bisherigen aktiven
    Zugangsschlüssel werden vollständig durch das gewählte Profil ersetzt.

    Rückgabe:
        success, message
    """
    try:
        config_data = read_config() or {}

        profiles, active_index = (
            jellydream_normalize_server_profiles(
                config_data
            )
        )

        try:
            profile_index = int(profile_index)
        except Exception:
            return False, _("Ungültiger Serverplatz.")

        if not (
            0 <= profile_index
            < MAX_JELLYFIN_SERVER_PROFILES
        ):
            return False, _("Ungültiger Serverplatz.")

        profile = profiles[profile_index]

        if not jellydream_server_profile_is_logged_in(
            profile
        ):
            return (
                False,
                _(
                    "Für Server %s ist keine vollständige "
                    "Anmeldung gespeichert."
                )
                % (
                    profile_index + 1
                )
            )

        server = str(
            profile.get("server_url")
            or ""
        ).strip().rstrip("/")

        username = str(
            profile.get("username")
            or ""
        ).strip()

        user_id = str(
            profile.get("user_id")
            or ""
        ).strip()

        access_token = str(
            profile.get("access_token")
            or ""
        ).strip()

        # Vorherige aktive Sitzung vollständig aus den Aktivschlüsseln
        # entfernen.
        for key in (
            "server_url",
            "server",
            "username",
            "user_id",
            "access_token",
            "token"
        ):
            config_data.pop(key, None)

        config_data["server_profiles"] = profiles
        config_data["active_server_profile"] = profile_index

        config_data["server_url"] = server
        config_data["username"] = username
        config_data["user_id"] = user_id
        config_data["access_token"] = access_token

        if not write_config(config_data):
            return (
                False,
                _("Serverprofil konnte nicht aktiviert werden.")
            )

        jd_log(
            "Server profile %s activated: %s"
            % (
                profile_index + 1,
                server
            ),
            level="INFO"
        )

        return (
            True,
            _(
                "Server %s wurde aktiviert."
            )
            % (
                profile_index + 1
            )
        )

    except Exception as e:
        jd_log(
            "Server profile could not be activated: %s"
            % e,
            level="ERROR"
        )

        return (
            False,
            "Serverprofil konnte nicht aktiviert werden."
        )

        
def jellydream_login_credentials(config_data=None):
    """
    Liefert die Login-Daten einheitlich aus der Konfiguration.

    Unterstützt aus Kompatibilitätsgründen sowohl die aktuellen Schlüssel
    `server_url` und `access_token` als auch die älteren Schlüssel
    `server` und `token`.

    Wichtig: Ein leerer aktueller Wert blockiert den Legacy-Fallback nicht.
    """
    try:
        if not isinstance(config_data, dict):
            config_data = read_config() or {}

        server = str(
            config_data.get("server_url")
            or config_data.get("server")
            or ""
        ).strip().rstrip("/")

        token = str(
            config_data.get("access_token")
            or config_data.get("token")
            or ""
        ).strip()

        user_id = str(
            config_data.get("user_id")
            or ""
        ).strip()

        username = str(
            config_data.get("username")
            or ""
        ).strip()

        return {
            "server": server,
            "token": token,
            "user_id": user_id,
            "username": username
        }

    except Exception:
        return {
            "server": "",
            "token": "",
            "user_id": "",
            "username": ""
        }



def jellyfin_server():
    return jellydream_login_credentials().get(
        "server",
        ""
    )



def jellyfin_token():
    return jellydream_login_credentials().get(
        "token",
        ""
    )



def jellyfin_user_id():
    return jellydream_login_credentials().get(
        "user_id",
        ""
    )



def jellyfin_username():
    return jellydream_login_credentials().get(
        "username",
        ""
    )


def jellydream_max_resolution():
    # Standardmäßig "1080p", Optionen: "4K", "1080p", "720p", "SD"
    return read_config().get("max_res", "4K")


def jellydream_update_refresh_time():
    # Standardmäßig "150", Optionen: "150", "200", "250"
    try:
        return int(read_config().get("update_refresh", 150))
    except Exception:
        return 150


def jellydream_dynamic_backdrops_enabled():
    """
    True: Jellyfin-Backdrops dürfen dynamisch geladen und angezeigt werden.
    False: Es wird immer ausschließlich BACKGROUND_FILE verwendet.
    """
    return str(
        read_config().get(
            "dynamic_backdrops",
            "1"
        )
    ) == "1"


def jellydream_lcd_playback_enabled():
    """
    True: Die Wiedergabe zeigt Titel, Fortschritt und Zeit auf dem LCD/VFD.
    """
    return str(
        read_config().get(
            "lcd_playback_display",
            "0"
        )
    ) == "1"

    
def jellydream_library_auto_update_enabled():
    return str(
        read_config().get(
            "library_auto_update",
            "1"
        )
    ) == "1"


def jellydream_library_update_hour():
    try:
        hour = int(
            read_config().get(
                "library_update_hour",
                4
            )
        )
        return max(0, min(23, hour))
    except Exception:
        return 4


def jellydream_library_sort():
    value = str(
        read_config().get("library_sort") or "name"
    ).strip()

    if value not in ("name", "added"):
        return "name"

    return value


def jellydream_set_library_sort(mode):
    mode = "added" if str(mode or "") == "added" else "name"
    config_data = read_config() or {}

    if str(config_data.get("library_sort") or "name") == mode:
        return True

    config_data["library_sort"] = mode
    return write_config(config_data)


def jellydream_library_played_filter():
    value = str(
        read_config().get("library_played_filter") or "all"
    ).strip()

    if value not in ("all", "unplayed", "played"):
        return "all"

    return value


def jellydream_set_library_played_filter(mode):
    mode = str(mode or "all").strip()

    if mode not in ("all", "unplayed", "played"):
        mode = "all"

    config_data = read_config() or {}

    if str(config_data.get("library_played_filter") or "all") == mode:
        return True

    config_data["library_played_filter"] = mode
    return write_config(config_data)


def jellydream_music_sort():
    value = str(
        read_config().get("music_sort") or "name"
    ).strip()

    if value not in ("name", "added", "artist"):
        return "name"

    return value


def jellydream_set_music_sort(mode):
    mode = str(mode or "name").strip()

    if mode not in ("name", "added", "artist"):
        mode = "name"

    config_data = read_config() or {}

    if str(config_data.get("music_sort") or "name") == mode:
        return True

    config_data["music_sort"] = mode
    return write_config(config_data)


def jellydream_music_played_filter():
    value = str(
        read_config().get("music_played_filter") or "all"
    ).strip()

    if value not in ("all", "unplayed", "played"):
        return "all"

    return value


def jellydream_set_music_played_filter(mode):
    mode = str(mode or "all").strip()

    if mode not in ("all", "unplayed", "played"):
        mode = "all"

    config_data = read_config() or {}

    if str(config_data.get("music_played_filter") or "all") == mode:
        return True

    config_data["music_played_filter"] = mode
    return write_config(config_data)


def jellydream_library_update_minute():
    try:
        minute = int(
            read_config().get(
                "library_update_minute",
                0
            )
        )
        return max(0, min(59, minute))
    except Exception:
        return 0

        
def jellydream_video_bitrate():
    # Standardmäßig 8 Mbit/s (8000000)
    try: return int(read_config().get("video_bitrate", 8000000))
    except Exception: return 8000000


def jellydream_stream_mode():
    # Standardmäßig "direct", Optionen: "stable", "hybrid", "direct"
    return read_config().get("stream_mode", "direct")


def jellydream_stream_protocol():
    value = str(
        read_config().get("stream_protocol", "ts")
    ).strip().lower()

    if value not in ("ts", "hls"):
        value = "ts"

    return value

    
def jellydream_allow_ac3_direct():
    return str(read_config().get("allow_ac3_direct", "0")) == "1"


def jellydream_allow_eac3_direct():
    return str(read_config().get("allow_eac3_direct", "1")) == "1"

    
def jellydream_allow_truehd_direct():
    return str(
        read_config().get(
            "allow_truehd_direct",
            "0"
        )
    ) == "1"

    
def jellydream_allow_dts_direct():
    return str(read_config().get("allow_dts_direct", "0")) == "1"


def jellydream_next_episode_autoplay():
    return str(read_config().get("next_episode_autoplay", "0")) == "1"


def jellydream_skip_intro_automatically():
    return str(read_config().get("skip_intro_automatically", "0")) == "1"


def jellydream_preferred_audio_codec():
    value = str(read_config().get("preferred_audio_codec", "aac")).strip().lower()
    if value not in ("aac", "mp2", "ac3"):
        value = "aac"
    return value


def jellydream_default_audio_language():
    value = str(read_config().get("default_audio_language", "de")).strip().lower()
    if value not in ("", "de", "en", "fr", "it", "es", "nl", "pl", "pt", "ru", "tr", "ja", "ko", "zh"):
        value = "de"
    return value


def jellydream_build_cache_namespace(config_data=None):
    """
    Berechnet den Namespace aus einem bereits vorhandenen Config-Dict.

    Wichtig: Diese Funktion ruft weder jellyfin_server() noch
    jellyfin_user_id() auf. Dadurch werden keine mehrfachen read_config()
    und deepcopy()-Aufrufe ausgelöst.
    """
    try:
        if not isinstance(config_data, dict):
            config_data = read_config() or {}

        server = str(
            config_data.get("server_url")
            or config_data.get("server")
            or ""
        ).strip().rstrip("/").lower()

        user_id = str(
            config_data.get("user_id", "") or ""
        ).strip().lower()

        identity = "%s\n%s" % (
            server,
            user_id
        )

        return hashlib.sha256(
            identity.encode("utf-8")
        ).hexdigest()[:20]

    except Exception:
        return "anonymous"



def jellydream_set_cache_namespace_from_config(config_data=None):
    """
    Aktualisiert den RAM-Namespace nach einer Konfigurationsänderung.

    Wird gezielt nach Login, Logout oder Speichern der Einstellungen
    über write_config() aufgerufen.
    """
    global _CACHE_NAMESPACE_VALUE

    namespace = jellydream_build_cache_namespace(
        config_data
    )

    with _CACHE_NAMESPACE_LOCK:
        _CACHE_NAMESPACE_VALUE = namespace

    return namespace



def jellydream_cache_namespace():
    """
    Liefert den aktuellen Cache-Namespace aus dem RAM.

    Die aufwendige Ermittlung aus der Konfiguration und der SHA-256-Hash
    erfolgen nur beim ersten Aufruf sowie nach write_config().
    """
    global _CACHE_NAMESPACE_VALUE

    with _CACHE_NAMESPACE_LOCK:
        if _CACHE_NAMESPACE_VALUE is not None:
            return _CACHE_NAMESPACE_VALUE

    # Beim ersten Aufruf einmalig aus der aktuellen Konfiguration bilden.
    return jellydream_set_cache_namespace_from_config(
        read_config() or {}
    )



def jellydream_scoped_cache_key(key):
    """
    Fügt einem Schlüssel den Namespace hinzu.

    Bereits markierte JellyDreamScopedCacheKey-Objekte werden unverändert
    zurückgegeben. Das ist besonders für Bild-Downloads wichtig, weil
    dort ein job_key sehr oft durch mehrere Cache-Funktionen läuft.
    """
    if isinstance(key, JellyDreamScopedCacheKey):
        return key

    namespace = jellydream_cache_namespace()
    key = str(key or "")

    prefix = "%s|" % namespace

    # Kompatibilität mit bereits als normaler String übergebenen,
    # aktuellen Namespace-Schlüsseln.
    if key.startswith(prefix):
        return JellyDreamScopedCacheKey(key)

    return JellyDreamScopedCacheKey(
        "%s%s" % (
            prefix,
            key
        )
    )


def jellydream_cache_dir(cache_name="cover"):
    base_directory = CACHE_DIRS.get(
        cache_name,
        "/tmp/jellydream_cache"
    )

    scoped_directory = os.path.join(
        base_directory,
        jellydream_cache_namespace()
    )

    try:
        os.makedirs(
            scoped_directory,
            exist_ok=True
        )
    except Exception:
        pass

    return scoped_directory


def jellydream_is_known_cache_namespace(namespace):
    """
    Akzeptiert ausschließlich Namespace-Namen, die JellyDream selbst
    erzeugt. Dadurch werden beim Aufräumen keine fremden Verzeichnisse
    oder Dateien entfernt.
    """
    try:
        namespace = str(namespace or "").strip().lower()

        return bool(
            namespace == "anonymous"
            or re.match(
                r"^[a-f0-9]{20}$",
                namespace
            )
        )

    except Exception:
        return False



def jellydream_namespace_directory_last_modified(
    directory_path
):
    """
    Ermittelt die jüngste Änderungszeit eines Namespace-Verzeichnisses.

    Die Verzeichniszeit allein reicht nicht aus: Das Anlegen neuer Dateien
    ändert sie zwar, spätere Änderungen an bereits vorhandenen Bilddateien
    aber nicht zwingend. Deshalb werden auch enthaltene Dateien geprüft.
    """
    try:
        newest_timestamp = os.path.getmtime(
            directory_path
        )

        for root_directory, directory_names, file_names in os.walk(
            directory_path
        ):
            for name in directory_names + file_names:
                path = os.path.join(
                    root_directory,
                    name
                )

                try:
                    newest_timestamp = max(
                        newest_timestamp,
                        os.path.getmtime(path)
                    )
                except Exception:
                    pass

        return newest_timestamp

    except Exception:
        return None



def jellydream_remove_cache_directory(directory_path):
    """
    Entfernt ein Verzeichnis rekursiv ohne zusätzliche Abhängigkeit auf
    shutil. Rückgabe: True nur bei vollständigem Erfolg.
    """
    try:
        for root_directory, directory_names, file_names in os.walk(
            directory_path,
            topdown=False
        ):
            for file_name in file_names:
                try:
                    os.remove(
                        os.path.join(
                            root_directory,
                            file_name
                        )
                    )
                except Exception:
                    return False

            for directory_name in directory_names:
                try:
                    os.rmdir(
                        os.path.join(
                            root_directory,
                            directory_name
                        )
                    )
                except Exception:
                    return False

        os.rmdir(directory_path)
        return True

    except Exception:
        return False



def jellydream_cleanup_old_namespace_caches(
    max_age_seconds=NAMESPACE_CACHE_MAX_AGE_SECONDS
):
    """
    Entfernt alte, nicht mehr aktive Cache-Namespaces.

    Bereinigt werden:
    - jellydreammodern_library_cache_<namespace>.json
    - /tmp/jellydream_cache/<namespace>/

    Der aktuell aktive Namespace wird grundsätzlich übersprungen.
    Unbekannte Namen werden aus Sicherheitsgründen ebenfalls ignoriert.
    """
    result = {
        "library_files_removed": 0,
        "image_directories_removed": 0,
        "skipped_current": 0,
        "errors": 0
    }

    try:
        max_age_seconds = max(
            24 * 60 * 60,
            int(max_age_seconds or 0)
        )

        now_timestamp = time.time()
        current_namespace = jellydream_cache_namespace()

        library_directory = os.path.dirname(
            LIBRARY_CACHE_FILE_PREFIX
        )

        library_filename_prefix = (
            os.path.basename(
                LIBRARY_CACHE_FILE_PREFIX
            )
            + "_"
        )

        # Persistente Bibliothekscaches aufräumen.
        try:
            for file_name in os.listdir(
                library_directory
            ):
                if (
                    not file_name.startswith(
                        library_filename_prefix
                    )
                    or not file_name.endswith(".json")
                ):
                    continue

                namespace = file_name[
                    len(library_filename_prefix):-len(".json")
                ]

                if not jellydream_is_known_cache_namespace(
                    namespace
                ):
                    continue

                if namespace == current_namespace:
                    result["skipped_current"] += 1
                    continue

                cache_file_path = os.path.join(
                    library_directory,
                    file_name
                )

                try:
                    age_seconds = (
                        now_timestamp
                        - os.path.getmtime(cache_file_path)
                    )
                except Exception:
                    result["errors"] += 1
                    continue

                if age_seconds < max_age_seconds:
                    continue

                # Die Konfiguration kann während des Maintenance-Jobs
                # geändert worden sein. Vor dem Löschen daher erneut den
                # aktuell gültigen Namespace prüfen.
                if namespace == jellydream_cache_namespace():
                    result["skipped_current"] += 1
                    continue

                try:
                    os.remove(cache_file_path)

                    result["library_files_removed"] += 1

                    jd_log(
                        "Old library cache removed: %s"
                        % file_name,
                        level="INFO"
                    )

                except Exception as e:
                    result["errors"] += 1

                    jd_log(
                        "Old library cache could not be removed: %s | %s"
                        % (
                            cache_file_path,
                            e
                        ),
                        level="WARNING"
                    )

        except Exception as e:
            result["errors"] += 1

            jd_log(
                "Library cache directory could not be cleaned: %s" % e,
                level="WARNING"
            )

        # Namespace-Unterordner der Bildcaches aufräumen.
        processed_directories = set()

        for base_directory in CACHE_DIRS.values():
            base_directory = os.path.abspath(
                str(base_directory or "")
            )

            if (
                not base_directory
                or base_directory in processed_directories
            ):
                continue

            processed_directories.add(
                base_directory
            )

            try:
                if not os.path.isdir(base_directory):
                    continue

                for namespace in os.listdir(base_directory):
                    namespace_directory = os.path.join(
                        base_directory,
                        namespace
                    )

                    if not os.path.isdir(
                        namespace_directory
                    ):
                        continue

                    if not jellydream_is_known_cache_namespace(
                        namespace
                    ):
                        continue

                    if namespace == current_namespace:
                        result["skipped_current"] += 1
                        continue

                    last_modified = (
                        jellydream_namespace_directory_last_modified(
                            namespace_directory
                        )
                    )

                    # Bei einem nicht prüfbaren Verzeichnis wird bewusst
                    # nichts gelöscht.
                    if last_modified is None:
                        result["errors"] += 1
                        continue

                    age_seconds = (
                        now_timestamp
                        - last_modified
                    )

                    if age_seconds < max_age_seconds:
                        continue

                    if namespace == jellydream_cache_namespace():
                        result["skipped_current"] += 1
                        continue

                    if jellydream_remove_cache_directory(
                        namespace_directory
                    ):
                        result[
                            "image_directories_removed"
                        ] += 1

                        jd_log(
                            "Old image cache directory removed: %s"
                            % namespace_directory,
                            level="INFO"
                        )

                    else:
                        result["errors"] += 1

                        jd_log(
                            "Old image cache directory could not be fully removed: %s"                            % namespace_directory,
                            level="WARNING"
                        )

            except Exception as e:
                result["errors"] += 1

                jd_log(
                    "Image cache directory could not be cleaned: %s | %s"
                    % (
                        base_directory,
                        e
                    ),
                    level="WARNING"
                )

        jd_log(
            "Namespace cache cleanup finished: %s library file(s), %s image-cache director(ies) removed, %s error(s)"
            % (
                result["library_files_removed"],
                result["image_directories_removed"],
                result["errors"]
            ),
            level=(
                "WARNING"
                if result["errors"]
                else "INFO"
            )
        )

    except Exception as e:
        result["errors"] += 1

        jd_log(
            "Namespace cache cleanup failed: %s"
            % e,
            level="WARNING"
        )

    return result
