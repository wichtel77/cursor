# -*- coding: utf-8 -*-

import html
import json
import threading
import time

import urllib.request
import urllib.parse
import urllib.error

from enigma import eTimer

from . import JELLYDREAM_UI_LANGUAGES
from . import _
from . import jellydream_resolve_ui_language
from . import localeInit

from .constants import (
    JELLYDREAM_TMDB_API_BASE,
    JELLYDREAM_TMDB_API_KEY,
    JELLYDREAM_USER_AGENT,
    JELLYDREAM_VERSION,
    JellyDreamLRUCache,
    JellyDreamScopedCacheKey,
    TIMEOUT_LONG,
    TIMEOUT_MIDDLE,
    TIMEOUT_NORMAL,
    TIMEOUT_SHORT,
    jellydream_get_json_lock
)
from .log import jd_log, jellydream_set_log_level_from_config
from .workers import (
    jellydream_dispatch_to_gui,
    jellydream_is_gui_thread,
    jellydream_submit_api,
    jellydream_submit_maintenance
)
from .config import (
    jellydream_cleanup_old_namespace_caches,
    jellydream_get_search_cache_items,
    jellydream_library_auto_update_enabled,
    jellydream_library_cache_file,
    jellydream_library_sort,
    jellydream_library_update_hour,
    jellydream_library_update_minute,
    jellydream_read_json_file,
    jellydream_scoped_cache_key,
    jellydream_set_cache_namespace_from_config,
    jellydream_patch_search_cache_userdata,
    jellydream_set_search_cache,
    jellydream_write_json_atomic,
    jellyfin_server,
    jellyfin_token,
    jellyfin_user_id,
    read_config
)
from .http import (
    JellyfinRequestError,
    jellydream_auth_snapshot,
    jellydream_auth_snapshot_key,
    jellydream_auth_snapshot_matches_current,
    jellydream_urlopen,
    jellyfin_api_delete_ex,
    jellyfin_api_get_ex,
    jellyfin_api_post_ex,
    jellyfin_is_logged_in,
    jellyfin_items_api_get_ex,
    jellyfin_raise_request_error
)
from .images import (
    jellydream_cache_delete,
    jellydream_cache_drop_containing,
    jellydream_cache_get,
    jellydream_cache_set
)
from .uiutil import jellydream_int_or_default

_LIBRARY_CACHE_LOCK = threading.Lock()

_LIBRARY_UPDATE_IN_PROGRESS = False

_LIBRARY_SCHEDULER = None


# ==================== GLOBALE CACHES / ASYNC-LOADER ====================

_GLOBAL_DETAILS_CACHE = JellyDreamLRUCache(max_size=256)

_GLOBAL_REMOTE_OVERVIEW_CACHE = JellyDreamLRUCache(max_size=256)

_GLOBAL_SERIES_SEASONS_CACHE = JellyDreamLRUCache(max_size=64)

_GLOBAL_EPISODES_CACHE = JellyDreamLRUCache(max_size=128)

_GLOBAL_BOXSET_CACHE = JellyDreamLRUCache(max_size=64)
_FAVORITE_ID_CACHE = None
_LIBRARY_SERIES_CACHE_STALE = False
_STALE_SEASON_SERIES_IDS = set()

_ASYNC_DETAIL_JOB_LOCK = threading.Lock()

_ASYNC_DETAIL_JOBS = set()

_ASYNC_DETAIL_JOB_CALLBACKS = {}


def jellydream_merge_item_details(base_item, details):
    merged = {}
    merged.update(base_item or {})
    merged.update(details or {})
    return merged


def jellydream_load_item_details_async(
    item_id,
    callback=None,
    callbacks=None,
    cache_callback=None
):
    """
    Lädt Detaildaten im Hintergrund und speichert sie im globalen Cache.

    Callback-Signaturen:
        callback(details, item_id)
            Wird sowohl bei einem Cache-Treffer als auch nach einem
            echten Hintergrundabruf aufgerufen. Bei Fehlern oder
            leeren Daten erhält der Callback {}.

        cache_callback(details, item_id)
            Optionaler zusätzlicher Kompatibilitäts-Callback, der bei
            einem Cache-Treffer ebenfalls aufgerufen wird.

    Mehrere parallele Requests für dieselbe Item-ID werden zusammengefasst.
    """
    item_id = str(item_id or "").strip()

    # Der Snapshot muss vor dem Einreihen in den Worker-Pool entstehen.
    auth_snapshot = jellydream_auth_snapshot()

    registered_callbacks = []

    if callable(callback):
        registered_callbacks.append(callback)

    if isinstance(callbacks, (list, tuple)):
        for current_callback in callbacks:
            if callable(current_callback):
                registered_callbacks.append(current_callback)

    def dispatch_detail_result(
        details,
        callback_item_id,
        include_cache_callback=False
    ):
        callbacks_to_dispatch = list(
            registered_callbacks
        )

        if (
            include_cache_callback
            and callable(cache_callback)
            and cache_callback not in callbacks_to_dispatch
        ):
            callbacks_to_dispatch.append(
                cache_callback
            )

        for current_callback in callbacks_to_dispatch:
            dispatched = jellydream_dispatch_to_gui(
                lambda cb=current_callback,
                loaded_details=details,
                loaded_item_id=callback_item_id: cb(
                    loaded_details,
                    loaded_item_id
                )
            )

            if not dispatched:
                jd_log(
                    "Detail callback could not be dispatched to the GUI thread: %s"                    % callback_item_id,
                    level="ERROR"
                )

    if not item_id:
        dispatch_detail_result(
            {},
            "",
            include_cache_callback=True
        )

        return False

    # Erster Cache-Check ohne Job-Lock.
    cached_details = jellydream_cache_get(
        _GLOBAL_DETAILS_CACHE,
        item_id,
        {}
    ) or {}

    if cached_details:
        jd_log(
            "Using cached item details: %s" % item_id,
            level="DEBUG"
        )

        # Auch der normale Callback muss bei einem Cache-Treffer
        # abgeschlossen werden. Das ist insbesondere für
        # _detail_requests_in_flight wichtig.
        dispatch_detail_result(
            cached_details,
            item_id,
            include_cache_callback=True
        )

        return True

    job_key = jellydream_scoped_cache_key(item_id)

    # Details derselben Item-ID dürfen bei einem Tokenwechsel nicht an
    # einen alten, noch wartenden Request angehängt werden.
    async_job_key = JellyDreamScopedCacheKey(
        "%s|auth=%s"
        % (
            str(job_key),
            jellydream_auth_snapshot_key(auth_snapshot)
        )
    )

    cache_hit_after_lock = {}

    with _ASYNC_DETAIL_JOB_LOCK:
        # Während des Wartens auf den Lock könnte ein anderer Worker
        # die Daten bereits geladen und im Cache abgelegt haben.
        cache_hit_after_lock = jellydream_cache_get(
            _GLOBAL_DETAILS_CACHE,
            item_id,
            {}
        ) or {}

        if cache_hit_after_lock:
            # Callback und Rückgabe erfolgen nach Verlassen des Locks.
            pass

        elif async_job_key in _ASYNC_DETAIL_JOBS:
            # Ein Abruf läuft bereits. Der normale Callback wird an den
            # laufenden Request angehängt.
            if registered_callbacks:
                _ASYNC_DETAIL_JOB_CALLBACKS.setdefault(
                    async_job_key,
                    []
                ).extend(registered_callbacks)

            return True

        else:
            _ASYNC_DETAIL_JOBS.add(async_job_key)
            _ASYNC_DETAIL_JOB_CALLBACKS[async_job_key] = list(
                registered_callbacks
            )

    # Wichtig: Nur bei einem Treffer des ZWEITEN Cache-Checks zurückkehren.
    if cache_hit_after_lock:
        jd_log(
            "Using cached item details after lock check: %s"            % item_id,
            level="DEBUG"
        )

        # Während auf den Lock gewartet wurde, konnte ein anderer
        # Worker die Daten laden. Trotzdem müssen alle Callbacks
        # des aktuellen Aufrufers abgeschlossen werden.
        dispatch_detail_result(
            cache_hit_after_lock,
            item_id,
            include_cache_callback=True
        )

        return True

    # Ab hier gab es keinen Cache-Treffer und keinen laufenden Job:
    # Der Netzwerk-Worker darf nun gestartet werden.
    def worker():
        details = {}

        try:
            details = jellyfin_get_item_details(
                item_id,
                raise_on_error=False,
                auth_snapshot=auth_snapshot
            ) or {}

            # Resultate aus einer alten Sitzung dürfen nicht im Cache der
            # nun aktiven Sitzung landen.
            if not jellydream_auth_snapshot_matches_current(
                auth_snapshot
            ):
                jd_log(
                    "Stale detail result discarded: %s"
                    % item_id,
                    level="DEBUG"
                )
                details = {}

            elif isinstance(details, dict) and details:
                jellydream_cache_set(
                    _GLOBAL_DETAILS_CACHE,
                    item_id,
                    details
                )

            else:
                details = {}

        except Exception as e:
            details = {}

            jd_log(
                "Async detail request failed for '%s': %s"% (item_id, e),
                level="DEBUG"
            )

        finally:
            pending_callbacks = []

            try:
                with _ASYNC_DETAIL_JOB_LOCK:
                    pending_callbacks = list(
                        _ASYNC_DETAIL_JOB_CALLBACKS.pop(
                            async_job_key,
                            []
                        )
                    )

                    _ASYNC_DETAIL_JOBS.discard(
                        async_job_key
                    )

            except Exception as e:
                jd_log(
                    "Detail callbacks could not be attached for '%s': %s"% (item_id, e),
                    level="DEBUG"
                )

        # Die Job-Registrierung wurde bereits bereinigt. Das Ergebnis
        # einer alten Sitzung wird aber nicht mehr in die GUI gegeben.
        if not jellydream_auth_snapshot_matches_current(
            auth_snapshot
        ):
            return

        for current_callback in pending_callbacks:
            jellydream_dispatch_to_gui(
                lambda cb=current_callback,
                loaded_details=details,
                loaded_item_id=item_id: cb(
                    loaded_details,
                    loaded_item_id
                )
            )

    started = jellydream_submit_api(worker)

    if not started:
        pending_callbacks = []

        with _ASYNC_DETAIL_JOB_LOCK:
            pending_callbacks = list(
                _ASYNC_DETAIL_JOB_CALLBACKS.pop(
                    async_job_key,
                    []
                )
            )

            _ASYNC_DETAIL_JOBS.discard(
                async_job_key
            )

        for current_callback in pending_callbacks:
            jellydream_dispatch_to_gui(
                lambda cb=current_callback,
                failed_item_id=item_id: cb(
                    {},
                    failed_item_id
                )
            )

    return started

        
def jellyfin_get_boxset_for_item(item_id, title="", item_type=""):
    if not item_id:
        return None, title

    if jellydream_is_gui_thread():
        jd_log(
            "Direct jellyfin_get_boxset_for_item() call blocked on the GUI thread: %s"
            % item_id,
            level="ERROR"
        )
        return None, title

    if item_type in ("BoxSet", "CollectionFolder"):
        return str(item_id), str(title)

    try:
        server = jellyfin_server()
        user_id = jellyfin_user_id()
        if server and user_id and jellyfin_token():
            auth_snapshot = jellydream_auth_snapshot()

            result = jellyfin_items_api_get_ex(
                route_suffix=urllib.parse.quote(
                    str(item_id),
                    safe=""
                ),
                query_params={
                    "Fields": "BoxSetConnections"
                },
                timeout=TIMEOUT_SHORT,
                auth_snapshot=auth_snapshot
            )

            details = (
                result.get("data")
                if result.get("ok")
                else None
            )
            
            if details and isinstance(details, dict):
                if details.get("Type") == "BoxSet":
                    return str(item_id), str(title)

                connections = details.get("BoxSetConnections") or []
                if connections and isinstance(connections, list) and len(connections) > 0:
                    first_conn = connections[0]
                    if isinstance(first_conn, dict):
                        b_id = first_conn.get("Id")
                        b_name = first_conn.get("Name") or f"{title} Reihe"
                        if b_id:
                            jd_log(f"Box set link resolved: {b_name} ({b_id}) for item: {item_id}", level="DEBUG")
                            return str(b_id), str(b_name)
    except Exception as e:
        jd_log(
            "Error while resolving the collection: %s" % e,
            level="WARNING"
        )
        
    return None, title


def jellydream_normalize_item(item, default_index=0):
    if isinstance(item, dict):
        item_id = item.get("id") or item.get("Id") or ""
        item_name = item.get("name") or item.get("Name") or ""

        item_idx = item.get("IndexNumber")
        if item_idx is None: item_idx = item.get("index")
        if item_idx is None: item_idx = item.get("ParentIndexNumber")
        if item_idx is None: item_idx = default_index
            
        try: item_idx = int(item_idx)
        except Exception: item_idx = default_index

        if not item_name:
            item_name = (_("Staffel %s") % item_idx) if item.get("Type") == "Season" else _("Eintrag")

        normalized = dict(item)
        normalized.update({
            "id": str(item_id),          "Id": str(item_id),
            "index": item_idx,           "IndexNumber": item_idx,
            "name": str(item_name),      "Name": str(item_name)
        })
        return normalized
    else:
        try: idx = int(item)
        except Exception: idx = default_index
        return {
            "id": "",                    "Id": "",
            "index": idx,                "IndexNumber": idx,
            "name": (_("Staffel %s") % idx), "Name": (_("Staffel %s") % idx)
        }


def jellydream_deduplicate_items(
    items,
    context=""
):
    """
    Entfernt doppelte Jellyfin-Einträge anhand ihrer Item-ID.

    Die Reihenfolge der ersten Vorkommen bleibt erhalten.
    Einträge ohne ID bleiben ebenfalls erhalten, weil sie nicht
    zuverlässig als Dublette erkannt werden können.
    """
    try:
        if not isinstance(items, list):
            return []

        unique_items = []
        seen_ids = set()
        duplicate_ids = []

        for item in items:
            if not isinstance(item, dict):
                continue

            item_id = str(
                item.get("Id")
                or item.get("id")
                or ""
            ).strip()

            if item_id:
                if item_id in seen_ids:
                    duplicate_ids.append(item_id)
                    continue

                seen_ids.add(item_id)

            unique_items.append(item)

        removed_count = len(items) - len(unique_items)

        if removed_count > 0:
            jd_log(
                "Duplicate GetItems results removed%s: server=%s, unique=%s, removed=%s"
                % (
                    " für %s" % context
                    if context
                    else "",
                    len(items),
                    len(unique_items),
                    removed_count
                ),
                level="WARNING"
            )
        else:
            jd_log(
                "GetItems set checked%s: %s entries, no duplicate ids"
                % (
                    " für %s" % context
                    if context
                    else "",
                    len(unique_items)
                ),
                level="DEBUG"
            )

        return unique_items

    except Exception as e:
        jd_log(
            "GetItems deduplication failed%s: %s"
            % (
                " für %s" % context
                if context
                else "",
                e
            ),
            level="WARNING"
        )

        return (
            list(items)
            if isinstance(items, list)
            else []
        )

        
def jellydream_series_library_is_stale():
    return bool(_LIBRARY_SERIES_CACHE_STALE)


def jellydream_consume_season_list_stale(series_id):
    series_id = str(series_id or "").strip()

    if series_id and series_id in _STALE_SEASON_SERIES_IDS:
        _STALE_SEASON_SERIES_IDS.discard(series_id)
        return True

    return False


def jellydream_patch_library_item_userdata(item):
    """
    Schreibt den neuen Gesehen- und Favoritenstatus in die gespeicherte
    Film- und Serienliste. Sonst lädt die nächste Ansicht den alten Stand.
    """
    if not isinstance(item, dict):
        return

    item_id = str(
        item.get("Id") or item.get("id") or ""
    ).strip()
    user_data = item.get("UserData")

    if not item_id or not isinstance(user_data, dict):
        return

    jellydream_patch_search_cache_userdata(
        item_id,
        user_data
    )

    cache_file_path = jellydream_library_cache_file()
    cache_lock = jellydream_get_json_lock(
        cache_file_path
    )

    with cache_lock:
        cache_data = jellydream_read_library_cache()

        if not isinstance(cache_data, dict):
            return

        changed = False

        for cache_name in ("movies", "series"):
            rows = cache_data.get(cache_name)

            if not isinstance(rows, list):
                continue

            for cached in rows:
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
                changed = True

        if changed:
            jellydream_write_library_cache(cache_data)


def jellydream_invalidate_playback_caches(item, action=""):
    """
    Vergisst zwischengespeicherte Listen, nachdem Gesehen, Ungesehen
    oder Favorit geändert wurde.
    """
    global _LIBRARY_SERIES_CACHE_STALE

    if not isinstance(item, dict):
        return

    needles = []

    for key in ("Id", "id", "SeriesId", "SeasonId", "ParentId"):
        value = str(item.get(key) or "").strip()

        if value and value not in needles:
            needles.append(value)

    for needle in needles:
        jellydream_cache_delete(_GLOBAL_DETAILS_CACHE, needle)
        jellydream_cache_drop_containing(
            _GLOBAL_SERIES_SEASONS_CACHE,
            needle
        )
        jellydream_cache_drop_containing(
            _GLOBAL_EPISODES_CACHE,
            needle
        )

    _GLOBAL_BOXSET_CACHE.clear()
    jellydream_patch_library_item_userdata(item)

    if action not in ("played", "unplayed", "hide_resume"):
        return

    item_type = str(item.get("Type") or "").strip()
    series_id = str(item.get("SeriesId") or "").strip()
    item_id = str(
        item.get("Id") or item.get("id") or ""
    ).strip()

    if item_type == "Episode" and series_id:
        _STALE_SEASON_SERIES_IDS.add(series_id)

    if item_type in ("Season", "Episode") or (
        series_id and series_id != item_id
    ):
        _LIBRARY_SERIES_CACHE_STALE = True


def jellyfin_get_series_seasons(series_id, raise_on_error=False):
    try:
        
        series_id = str(series_id or "").strip()

        if not series_id:
            if raise_on_error:
                raise JellyfinRequestError(
                    "Serien-ID fehlt."
                )
            return []

        server = jellyfin_server()
        user_id = jellyfin_user_id()
        token = jellyfin_token()

        if not server or not user_id or not token:
            if raise_on_error:
                raise JellyfinRequestError(
                    "Jellyfin-Anmeldung ist unvollständig."
                )
            return []

        cache_key = "%s|%s|%s" % (
            server,
            user_id,
            series_id
        )

        cached = jellydream_cache_get(
            _GLOBAL_SERIES_SEASONS_CACHE,
            cache_key,
            None
        )

        if cached is not None:
            if (
                cached
                and isinstance(cached[0], dict)
                and "ChildCount" not in cached[0]
            ):
                cached = None
            else:
                return jellydream_apply_favorite_flags(cached)

        url = (
            f"{server}/Shows/"
            f"{urllib.parse.quote(series_id, safe='')}/Seasons?"
            f"userId={urllib.parse.quote(str(user_id), safe='')}"
            f"&Fields=IndexNumber,ProductionYear,Overview,"
            f"OfficialRating,CommunityRating,SeriesId,SeriesName,"
            f"ImageTags,ProviderIds,UserData,ChildCount"
        )

        result = jellyfin_api_get_ex(
            url,
            timeout=TIMEOUT_SHORT
        )

        if not result.get("ok"):
            if raise_on_error:
                jellyfin_raise_request_error(
                    result,
                    "Staffelliste"
                )
            return []

        data = result.get("data")

        if not isinstance(data, dict):
            if raise_on_error:
                raise JellyfinRequestError(
                    "Ungültige Antwort vom Jellyfin-Server."
                )
            return []

        raw_items = data.get("Items", [])

        if not isinstance(raw_items, list):
            raw_items = []

        seasons = [
            jellydream_normalize_item(
                item,
                item.get("IndexNumber", index + 1)
            )
            for index, item in enumerate(raw_items)
            if isinstance(item, dict)
        ]

        jellydream_cache_set(
            _GLOBAL_SERIES_SEASONS_CACHE,
            cache_key,
            seasons
        )

        return jellydream_apply_favorite_flags(seasons)

    except JellyfinRequestError:
        raise

    except Exception as e:
        jd_log(
            "Seasons for series '%s' could not be loaded: %s"            % (series_id, e),
            level="ERROR"
        )

        if raise_on_error:
            raise JellyfinRequestError(
                "Staffeln konnten nicht geladen werden.",
                technical_error=str(e)
            )

        return []


def jellydream_is_next_up_item(item):
    return (
        isinstance(item, dict)
        and bool(item.get("_jellydream_next_up"))
    )



def jellydream_next_up_episode_code(item):
    if not isinstance(item, dict):
        return ""

    season_number = jellydream_int_or_default(
        item.get("ParentIndexNumber"),
        0
    )
    episode_number = jellydream_int_or_default(
        item.get("IndexNumber"),
        0
    )

    if season_number < 0 or episode_number <= 0:
        return ""

    return "S%02dE%02d" % (
        season_number,
        episode_number
    )



def jellydream_prepare_next_up_item(item):
    if not isinstance(item, dict):
        return {}

    next_up_item = dict(item)
    item_id = str(
        next_up_item.get("Id")
        or next_up_item.get("id")
        or ""
    ).strip()

    if not item_id:
        return {}

    next_up_item["Id"] = item_id
    next_up_item["id"] = item_id
    next_up_item["Type"] = "Episode"
    next_up_item["_jellydream_next_up"] = True

    episode_code = jellydream_next_up_episode_code(
        next_up_item
    )

    if episode_code:
        next_up_item["_jellydream_grid_title"] = "%s\n%s" % (
            _("Als Nächstes"),
            episode_code
        )
    else:
        next_up_item["_jellydream_grid_title"] = _("Als Nächstes")

    jellydream_apply_favorite_flags([next_up_item])
    return next_up_item



def jellyfin_get_series_next_up(series_id, auth_snapshot=None):
    """
    Nächste Folge einer Serie über Jellyfin /Shows/NextUp.

    Das ist dieselbe Quelle wie „Als Nächstes“ in der
    Jellyfin-Staffelübersicht. Bei Fehlern bleibt die normale
    Staffelliste unverändert.
    """
    try:
        if jellydream_is_gui_thread():
            return {}

        series_id = str(series_id or "").strip()

        if not series_id:
            return {}

        if not isinstance(auth_snapshot, dict):
            auth_snapshot = jellydream_auth_snapshot()

        server = str(
            auth_snapshot.get("server") or ""
        ).strip().rstrip("/")

        user_id = str(
            auth_snapshot.get("user_id") or ""
        ).strip()

        token = str(
            auth_snapshot.get("token") or ""
        ).strip()

        if not server or not user_id or not token:
            return {}

        query_params = {
            "userId": user_id,
            "seriesId": series_id,
            "Limit": 1,
            "EnableResumable": "true",
            "EnableRewatching": "false",
            "Fields": (
                "RunTimeTicks,UserData,Overview,SeriesId,"
                "SeriesName,SeasonId,ParentId,ParentIndexNumber,"
                "IndexNumber,PremiereDate,ProductionYear,"
                "OfficialRating,ImageTags,ProviderIds"
            )
        }

        url = "%s/Shows/NextUp?%s" % (
            server,
            urllib.parse.urlencode(query_params)
        )

        result = jellyfin_api_get_ex(
            url,
            timeout=TIMEOUT_SHORT,
            auth_snapshot=auth_snapshot
        )

        if not result.get("ok"):
            jd_log(
                "NextUp for series '%s' is not available: HTTP %s"               % (
                    series_id,
                    result.get("status") or 0
                ),
                level="DEBUG"
            )
            return {}

        data = result.get("data")
        items = []

        if isinstance(data, dict):
            items = data.get("Items") or []
        elif isinstance(data, list):
            items = data

        if not isinstance(items, list) or not items:
            return {}

        next_item = items[0]

        if not isinstance(next_item, dict):
            return {}

        next_up_item = jellydream_prepare_next_up_item(
            next_item
        )

        if next_up_item:
            jellydream_apply_localized_overview(
                next_up_item,
                auth_snapshot
            )

        return next_up_item

    except Exception as e:
        jd_log(
            "NextUp for series '%s' failed: %s"            % (series_id, e),
            level="DEBUG"
        )
        return {}


def jellydream_metadata_language():
    language_code = str(
        jellydream_resolve_ui_language() or ""
    ).strip().lower()

    if language_code in JELLYDREAM_UI_LANGUAGES:
        return language_code

    return "en"



def jellydream_item_tmdb_id(item):
    if not isinstance(item, dict):
        return ""

    provider_ids = item.get("ProviderIds")

    if not isinstance(provider_ids, dict):
        return ""

    for key in (
        "Tmdb",
        "TmdbId",
        "TheMovieDb",
        "tmdb",
        "TmdbCollection",
        "TmdbCollectionId",
    ):
        value = str(provider_ids.get(key) or "").strip()

        if value:
            return value

    return ""



def jellydream_item_year(item):
    if not isinstance(item, dict):
        return None

    try:
        year = int(item.get("ProductionYear"))

        if year > 0:
            return year
    except Exception:
        pass

    premiere = str(item.get("PremiereDate") or "").strip()

    if len(premiere) >= 4 and premiere[:4].isdigit():
        try:
            year = int(premiere[:4])

            if year > 0:
                return year
        except Exception:
            pass

    return None



def jellydream_remote_search_kind(item_type):
    item_type = str(item_type or "").strip()

    if item_type in ("Movie", "Series"):
        return item_type

    return ""



def jellydream_season_display_name(item, fallback_index=1):
    season_index = jellydream_int_or_default(
        (item or {}).get("IndexNumber")
        if isinstance(item, dict)
        else fallback_index,
        fallback_index
    )

    return _("Staffel %s") % season_index



def jellydream_remote_overview_cache_key(item):
    if not isinstance(item, dict):
        return ""

    item_id = str(item.get("Id") or "").strip()
    tmdb_id = jellydream_item_tmdb_id(item)
    item_type = str(item.get("Type") or "").strip()

    if not item_id and not tmdb_id:
        return ""

    return "|".join((
        jellydream_metadata_language(),
        item_type,
        (
            "tv" if item_type in ("Season", "Episode")
            else "col" if item_type in ("BoxSet", "CollectionFolder")
            else "rs"
        ),
        tmdb_id or item_id,
        str(item.get("ParentIndexNumber") or ""),
        str(item.get("IndexNumber") or "")
    ))



def jellydream_item_cached_remote_overview(item):
    cache_key = jellydream_remote_overview_cache_key(item)

    if not cache_key:
        return ""

    cached = jellydream_cache_get(
        _GLOBAL_REMOTE_OVERVIEW_CACHE,
        cache_key,
        None
    )

    if cached:
        return str(cached).strip()

    return ""



def jellydream_unescape_overview(text):
    try:
        return html.unescape(str(text or "")).strip()
    except Exception:
        return str(text or "").strip()



def jellydream_item_overview(item):
    if not isinstance(item, dict):
        return ""

    remote = jellydream_item_cached_remote_overview(item)

    if remote:
        return jellydream_unescape_overview(remote)

    return jellydream_unescape_overview(item.get("Overview"))



def jellydream_imdb_rating_number(value):
    try:
        if value in (None, ""):
            return ""

        rating = float(value)
    except Exception:
        return ""

    if rating <= 0:
        return ""

    return "%.1f" % rating



def jellydream_format_imdb_rating(value):
    number = jellydream_imdb_rating_number(value)

    if not number:
        return ""

    return _("IMDb %s") % number



def jellydream_item_imdb_rating_number(item, fallback_item=None):
    sources = []

    if isinstance(item, dict):
        sources.append(item)

    if isinstance(fallback_item, dict):
        sources.append(fallback_item)

    for source in sources:
        number = jellydream_imdb_rating_number(
            source.get("CommunityRating")
        )

        if number:
            return number

    if not isinstance(item, dict):
        return ""

    series_id = str(item.get("SeriesId") or "").strip()

    if not series_id:
        return ""

    series_details = jellydream_cache_get(
        _GLOBAL_DETAILS_CACHE,
        series_id,
        {}
    ) or {}

    return jellydream_imdb_rating_number(
        series_details.get("CommunityRating")
        if isinstance(series_details, dict)
        else None
    )



def jellydream_item_imdb_rating_text(item, fallback_item=None):
    number = jellydream_item_imdb_rating_number(
        item,
        fallback_item
    )

    if not number:
        return ""

    return _("IMDb %s") % number



def jellydream_tmdb_api_key(auth_snapshot=None):
    return JELLYDREAM_TMDB_API_KEY



def jellydream_series_details_for_item(item, auth_snapshot=None):
    if not isinstance(item, dict):
        return {}

    if str(item.get("Type") or "").strip() == "Series":
        return item

    series_id = str(item.get("SeriesId") or "").strip()

    if not series_id:
        return {}

    series_details = jellydream_cache_get(
        _GLOBAL_DETAILS_CACHE,
        series_id,
        {}
    ) or {}

    if series_details:
        return series_details

    if jellydream_is_gui_thread():
        return {}

    series_details = jellyfin_get_item_details(
        series_id,
        auth_snapshot=auth_snapshot
    ) or {}

    if series_details:
        jellydream_cache_set(
            _GLOBAL_DETAILS_CACHE,
            series_id,
            series_details
        )

    return series_details if isinstance(series_details, dict) else {}



def jellydream_item_series_tmdb_id(item, auth_snapshot=None):
    series_details = jellydream_series_details_for_item(
        item,
        auth_snapshot
    )

    return jellydream_item_tmdb_id(series_details) or (
        jellydream_item_tmdb_id(item)
        if str(item.get("Type") or "").strip() == "Series"
        else ""
    )



def jellydream_tmdb_json_get(path, query):
    url = "%s/%s?%s" % (
        JELLYDREAM_TMDB_API_BASE,
        str(path or "").lstrip("/"),
        urllib.parse.urlencode(query or {})
    )

    try:
        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": JELLYDREAM_USER_AGENT
            }
        )

        with jellydream_urlopen(req, TIMEOUT_SHORT) as response:
            raw = response.read().decode("utf-8")
            data = json.loads(raw) if raw else {}

            if isinstance(data, dict):
                return data

    except Exception as e:
        jd_log(
            "TMDB request failed for '%s': %s"            % (path, e),
            level="DEBUG"
        )

    return {}



def jellydream_tmdb_localized_overview(path, api_key, metadata_language):
    languages = []
    metadata_language = str(metadata_language or "").strip() or "en"

    if metadata_language:
        languages.append(metadata_language)

    if metadata_language != "en":
        languages.append("en")

    for language in languages:
        data = jellydream_tmdb_json_get(
            path,
            {
                "api_key": api_key,
                "language": language
            }
        )
        overview = str(data.get("overview") or "").strip()

        if overview:
            return overview

    return ""



def jellydream_fetch_tmdb_tv_overview(item, auth_snapshot=None):
    if not isinstance(item, dict):
        return ""

    item_type = str(item.get("Type") or "").strip()

    if item_type not in ("Season", "Episode"):
        return ""

    api_key = jellydream_tmdb_api_key(auth_snapshot)

    if not api_key:
        return ""

    series_tmdb = jellydream_item_series_tmdb_id(
        item,
        auth_snapshot
    )

    if not series_tmdb:
        return ""

    metadata_language = jellydream_metadata_language()

    if item_type == "Season":
        season_number = jellydream_int_or_default(
            item.get("IndexNumber"),
            -1
        )

        if season_number < 0:
            return ""

        return jellydream_tmdb_localized_overview(
            "tv/%s/season/%s" % (
                urllib.parse.quote(str(series_tmdb), safe=""),
                season_number
            ),
            api_key,
            metadata_language
        )

    season_number = jellydream_int_or_default(
        item.get("ParentIndexNumber"),
        1
    )
    episode_number = jellydream_int_or_default(
        item.get("IndexNumber"),
        0
    )

    if episode_number <= 0:
        return ""

    return jellydream_tmdb_localized_overview(
        "tv/%s/season/%s/episode/%s" % (
            urllib.parse.quote(str(series_tmdb), safe=""),
            season_number,
            episode_number
        ),
        api_key,
        metadata_language
    )



def jellydream_fetch_tmdb_collection_overview(item, auth_snapshot=None):
    if not isinstance(item, dict):
        return ""

    item_type = str(item.get("Type") or "").strip()

    if item_type not in ("BoxSet", "CollectionFolder"):
        return ""

    api_key = jellydream_tmdb_api_key(auth_snapshot)

    if not api_key:
        return ""

    metadata_language = jellydream_metadata_language()
    collection_id = jellydream_item_tmdb_id(item)

    if collection_id:
        overview = jellydream_tmdb_localized_overview(
            "collection/%s" % urllib.parse.quote(
                str(collection_id),
                safe=""
            ),
            api_key,
            metadata_language
        )

        if overview:
            return overview

    name = str(item.get("Name") or "").strip()

    if not name:
        return ""

    languages = [metadata_language]

    if metadata_language != "en":
        languages.append("en")

    name_key = name.lower()

    for language in languages:
        data = jellydream_tmdb_json_get(
            "search/collection",
            {
                "api_key": api_key,
                "language": language,
                "query": name
            }
        )
        results = data.get("results") if isinstance(data, dict) else None

        if not isinstance(results, list):
            continue

        chosen = None

        for entry in results:
            if not isinstance(entry, dict):
                continue

            remote_name = str(entry.get("name") or "").strip().lower()

            if remote_name == name_key:
                chosen = entry
                break

        if chosen is None:
            first = results[0] if results else None
            chosen = first if isinstance(first, dict) else None

        if not chosen:
            continue

        overview = str(chosen.get("overview") or "").strip()

        if overview:
            return overview

        remote_id = chosen.get("id")

        if not remote_id:
            continue

        overview = jellydream_tmdb_localized_overview(
            "collection/%s" % remote_id,
            api_key,
            metadata_language
        )

        if overview:
            return overview

    return ""



def jellydream_build_remote_search_payload(item, metadata_language):
    search_info = {
        "Name": str(item.get("Name") or "").strip(),
        "MetadataLanguage": metadata_language
    }

    year = jellydream_item_year(item)

    if year:
        search_info["Year"] = year

    tmdb_id = jellydream_item_tmdb_id(item)

    if tmdb_id:
        search_info["ProviderIds"] = {
            "Tmdb": tmdb_id
        }

    if str(item.get("Type") or "").strip() == "Episode":
        series_name = str(item.get("SeriesName") or "").strip()

        if item.get("IndexNumber") not in (None, ""):
            search_info["IndexNumber"] = jellydream_int_or_default(
                item.get("IndexNumber"),
                0
            )

        if item.get("ParentIndexNumber") not in (None, ""):
            search_info["ParentIndexNumber"] = jellydream_int_or_default(
                item.get("ParentIndexNumber"),
                0
            )

        if series_name:
            search_info["SeriesName"] = series_name

        series_id = str(item.get("SeriesId") or "").strip()
        series_tmdb = ""

        if series_id:
            series_details = jellydream_cache_get(
                _GLOBAL_DETAILS_CACHE,
                series_id,
                {}
            ) or {}
            series_tmdb = jellydream_item_tmdb_id(
                series_details
            )

        if series_tmdb:
            search_info["SeriesProviderIds"] = {
                "Tmdb": series_tmdb
            }

    return {
        "SearchInfo": search_info,
        "IncludeDisabledProviders": False
    }



def jellydream_pick_remote_overview(results, tmdb_id=""):
    if not isinstance(results, list):
        return ""

    preferred = ""
    tmdb_id = str(tmdb_id or "").strip()

    for entry in results:
        if not isinstance(entry, dict):
            continue

        overview = str(entry.get("Overview") or "").strip()

        if not overview:
            continue

        provider_ids = entry.get("ProviderIds") or {}
        remote_tmdb = ""

        if isinstance(provider_ids, dict):
            remote_tmdb = str(
                provider_ids.get("Tmdb") or ""
            ).strip()

        if tmdb_id and remote_tmdb == tmdb_id:
            return overview

        if not preferred:
            preferred = overview

    return preferred



def jellydream_fetch_remote_overview(item, auth_snapshot=None):
    """
    Lokalisierte Beschreibung laden, ohne Metadaten auf dem Server
    zu überschreiben.

    Filme und Serien kommen über Jellyfin Remote Search. Staffeln und
    Folgen haben dort keine Overview, deshalb TMDB direkt anhand der
    Serien-TMDB-ID und der Staffel-/Folgennummer. Sammlungen haben
    keinen Remote-Search-Endpunkt und kommen über TMDB Collection.
    """
    if not isinstance(item, dict):
        return ""

    cache_key = jellydream_remote_overview_cache_key(item)

    if not cache_key:
        return ""

    cached = jellydream_cache_get(
        _GLOBAL_REMOTE_OVERVIEW_CACHE,
        cache_key,
        None
    )

    if cached is not None:
        return str(cached or "").strip()

    if jellydream_is_gui_thread():
        return ""

    item_type = str(item.get("Type") or "").strip()

    if item_type in ("Season", "Episode"):
        overview = jellydream_fetch_tmdb_tv_overview(
            item,
            auth_snapshot
        )
        jellydream_cache_set(
            _GLOBAL_REMOTE_OVERVIEW_CACHE,
            cache_key,
            overview
        )
        return overview

    if item_type in ("BoxSet", "CollectionFolder"):
        overview = jellydream_fetch_tmdb_collection_overview(
            item,
            auth_snapshot
        )
        jellydream_cache_set(
            _GLOBAL_REMOTE_OVERVIEW_CACHE,
            cache_key,
            overview
        )
        return overview

    search_kind = jellydream_remote_search_kind(item_type)

    if not search_kind:
        jellydream_cache_set(
            _GLOBAL_REMOTE_OVERVIEW_CACHE,
            cache_key,
            ""
        )
        return ""

    if not isinstance(auth_snapshot, dict):
        auth_snapshot = jellydream_auth_snapshot()

    server = str(
        auth_snapshot.get("server") or ""
    ).strip().rstrip("/")

    if not server:
        return ""

    metadata_language = jellydream_metadata_language()
    url = "%s/Items/RemoteSearch/%s" % (
        server,
        search_kind
    )

    result = jellyfin_api_post_ex(
        url,
        jellydream_build_remote_search_payload(
            item,
            metadata_language
        ),
        timeout=TIMEOUT_SHORT,
        auth_snapshot=auth_snapshot
    )

    overview = ""

    if result.get("ok"):
        overview = jellydream_pick_remote_overview(
            result.get("data"),
            jellydream_item_tmdb_id(item)
        )

    else:
        jd_log(
            "Remote-search overview not available for '%s': HTTP %s"            % (
                item.get("Id") or item.get("Name") or "?",
                result.get("status") or 0
            ),
            level="DEBUG"
        )

    jellydream_cache_set(
        _GLOBAL_REMOTE_OVERVIEW_CACHE,
        cache_key,
        overview
    )

    return overview



def jellydream_apply_localized_overview(item, auth_snapshot=None):
    if not isinstance(item, dict):
        return item

    remote = jellydream_item_cached_remote_overview(item)

    if not remote and not jellydream_is_gui_thread():
        remote = jellydream_fetch_remote_overview(
            item,
            auth_snapshot
        )

    if remote:
        item["Overview"] = remote

    return item


def jellyfin_get_item_details(item_id, raise_on_error=False, auth_snapshot=None):
    try:
        if not isinstance(auth_snapshot, dict):
            auth_snapshot = jellydream_auth_snapshot()

        server = str(
            auth_snapshot.get("server") or ""
        ).strip().rstrip("/")

        user_id = str(
            auth_snapshot.get("user_id") or ""
        ).strip()

        token = str(
            auth_snapshot.get("token") or ""
        ).strip()

        item_id = str(item_id or "").strip()

        if jellydream_is_gui_thread():
            cached_details = jellydream_cache_get(
                _GLOBAL_DETAILS_CACHE,
                item_id,
                {}
            ) or {}

            if cached_details:
                jellydream_apply_favorite_flags([cached_details])
                return cached_details

            jellydream_load_item_details_async(
                item_id
            )

            jd_log(
                "Direct jellyfin_get_item_details() call blocked on the GUI thread: %s"
                % item_id,
                level="WARNING"
            )

            if raise_on_error:
                raise JellyfinRequestError(
                    "Detaildaten müssen asynchron geladen werden."
                )

            return {}

        if not server or not user_id or not token or not item_id:
            if raise_on_error:
                raise JellyfinRequestError(
                    "Jellyfin-Anmeldung oder Item-ID fehlt."
                )
            return {}

        result = jellyfin_items_api_get_ex(
            route_suffix=urllib.parse.quote(
                item_id,
                safe=""
            ),
            query_params={
                "Fields": (
                    "MediaSources,RunTimeTicks,ProductionYear,Genres,"
                    "Overview,People,OfficialRating,CommunityRating,"
                    "SeriesId,SeriesName,SeasonId,ParentId,"
                    "ParentIndexNumber,IndexNumber,PremiereDate,"
                    "BoxSetConnections,ProviderIds,ImageTags,UserData"
                )
            },
            timeout=TIMEOUT_NORMAL,
            auth_snapshot=auth_snapshot
        )

        if not result.get("ok"):
            if raise_on_error:
                jellyfin_raise_request_error(
                    result,
                    "Detaildaten"
                )

            return {}

        details = result.get("data")

        if isinstance(details, dict):
            details = jellydream_apply_localized_overview(
                details,
                auth_snapshot
            )
            jellydream_apply_favorite_flags([details])
            return details

        if raise_on_error:
            raise JellyfinRequestError(
                "Ungültige Detailantwort vom Jellyfin-Server."
            )

        return {}

    except JellyfinRequestError:
        raise

    except Exception as e:
        jd_log(
            "Item details could not be loaded for '%s': %s"% (item_id, e),
            level="WARNING"
        )

        if raise_on_error:
            raise JellyfinRequestError(
                "Detaildaten konnten nicht geladen werden.",
                technical_error=str(e)
            )

        return {}


def jellyfin_fetch_items_ex(item_type, limit=5000, raise_on_error=False):
    try:
        server = jellyfin_server()
        user_id = jellyfin_user_id()
        token = jellyfin_token()

        if (
            not server
            or not user_id
            or not token
            or not item_type
        ):
            if raise_on_error:
                raise JellyfinRequestError(
                    "Jellyfin-Anmeldung ist unvollständig."
                )
            return False, []

        result = jellyfin_items_api_get_ex(
            query_params={
                "Recursive": "true",
                "IncludeItemTypes": str(item_type),
                "CollapseBoxSetItems": "false",
                "SortBy": "SortName",
                "SortOrder": "Ascending",
                "Limit": int(limit),
                "Fields": (
                    "RunTimeTicks,ProductionYear,Genres,"
                    "OfficialRating,CommunityRating,Overview,"
                    "ImageTags,BackdropImageTags,"
                    "ProviderIds,UserData,DateCreated"
                )
            },
            timeout=TIMEOUT_LONG
        )

        if not result.get("ok"):
            if raise_on_error:
                jellyfin_raise_request_error(
                    result,
                    "%s-Liste" % item_type
                )

            jd_log(
                "Library query for '%s' failed: %s"                % (
                    item_type,
                    result.get("error") or "Unbekannter Fehler"
                ),
                level="WARNING"
            )

            return False, []

        data = result.get("data")

        if not isinstance(data, dict):
            if raise_on_error:
                raise JellyfinRequestError(
                    "Ungültige Antwort vom Jellyfin-Server."
                )
            return False, []

        raw_items = data.get("Items")

        if not isinstance(raw_items, list):
            if raise_on_error:
                raise JellyfinRequestError(
                    "Ungültige Elementliste vom Jellyfin-Server."
                )
            return False, []

        normalized_items = [
            jellydream_normalize_item(item, index)
            for index, item in enumerate(raw_items)
            if isinstance(item, dict)
        ]

        normalized_items = jellydream_deduplicate_items(
            normalized_items,
            context="jellyfin_fetch_items_ex/%s" % item_type
        )

        return True, normalized_items

    except JellyfinRequestError:
        raise

    except Exception as e:
        jd_log(
            "Error in jellyfin_fetch_items_ex for '%s': %s"            % (item_type, e),
            level="ERROR"
        )

        if raise_on_error:
            raise JellyfinRequestError(
                "%s-Liste konnte nicht geladen werden."
                % item_type,
                technical_error=str(e)
            )

        return False, []


def jellydream_clear_metadata_caches(clear_details=True):
    global _FAVORITE_ID_CACHE
    global _LIBRARY_SERIES_CACHE_STALE

    try:
        _FAVORITE_ID_CACHE = None
        _LIBRARY_SERIES_CACHE_STALE = False
        _STALE_SEASON_SERIES_IDS.clear()
        _GLOBAL_SERIES_SEASONS_CACHE.clear()
        _GLOBAL_EPISODES_CACHE.clear()
        _GLOBAL_BOXSET_CACHE.clear()
        _GLOBAL_REMOTE_OVERVIEW_CACHE.clear()

        if clear_details:
            _GLOBAL_DETAILS_CACHE.clear()

        jd_log(
            "Temporary metadata caches were cleared",
            level="DEBUG"
        )

    except Exception as e:
        jd_log(
            "Metadata caches could not be cleared: %s" % e,
            level="WARNING"
        )

        
def jellydream_library_update_log_text(result):
    if not isinstance(result, dict):
        return "The update did not return a valid result"

    text = str(result.get("log_message") or "").strip()

    if text:
        return text

    return "No status message"


def jellydream_refresh_library_cache():
    global _LIBRARY_UPDATE_IN_PROGRESS

    with _LIBRARY_CACHE_LOCK:
        if _LIBRARY_UPDATE_IN_PROGRESS:
            return {
                "ok": False,
                "busy": True,
                "movies_ok": False,
                "series_ok": False,
                "movies": 0,
                "series": 0,
                "message": _("Aktualisierung läuft bereits"),
                "log_message": "Update already running"
            }

        _LIBRARY_UPDATE_IN_PROGRESS = True

    try:
        if not jellyfin_is_logged_in():
            return {
                "ok": False,
                "busy": False,
                "movies_ok": False,
                "series_ok": False,
                "movies": 0,
                "series": 0,
                "message": _("Nicht bei Jellyfin angemeldet"),
                "log_message": "Not signed in to Jellyfin"
            }

        jd_log(
            "Starting library cache refresh",
            level="INFO"
        )

        old_cache = jellydream_read_library_cache()

        if not isinstance(old_cache, dict):
            old_cache = jellydream_empty_library_cache()

        movies_ok, movies = jellyfin_fetch_items_ex(
            "Movie",
            5000
        )

        series_ok, series = jellyfin_fetch_items_ex(
            "Series",
            2000
        )

        now_timestamp = int(time.time())
        identity = jellydream_library_cache_identity()

        new_cache = dict(old_cache)

        new_cache["version"] = 1
        new_cache["server"] = identity["server"]
        new_cache["user_id"] = identity["user_id"]

        if movies_ok:
            new_cache["movies"] = movies
            new_cache["movies_updated"] = now_timestamp
            jellydream_set_search_cache(
                "movies",
                movies
            )

        if series_ok:
            new_cache["series"] = series
            new_cache["series_updated"] = now_timestamp
            jellydream_set_search_cache(
                "series",
                series
            )

        if movies_ok or series_ok:
            new_cache["updated"] = now_timestamp

        if not movies_ok and not series_ok:
            return {
                "ok": False,
                "busy": False,
                "movies_ok": False,
                "series_ok": False,
                "movies": len(
                    old_cache.get("movies") or []
                ),
                "series": len(
                    old_cache.get("series") or []
                ),
                "message": _(
                    "Film- und Serienlisten konnten "
                    "nicht aktualisiert werden"
                ),
                "log_message": "Movie and series lists could not be updated"
            }

        if not jellydream_write_library_cache(new_cache):
            return {
                "ok": False,
                "busy": False,
                "movies_ok": movies_ok,
                "series_ok": series_ok,
                "movies": len(
                    new_cache.get("movies") or []
                ),
                "series": len(
                    new_cache.get("series") or []
                ),
                "message": _(
                    "Bibliothekscache konnte nicht "
                    "gespeichert werden"
                ),
                "log_message": "Library cache could not be saved"
            }

        jellydream_clear_metadata_caches(clear_details=True)
        movie_count = len(
            new_cache.get("movies") or []
        )

        series_count = len(
            new_cache.get("series") or []
        )

        jd_log(
            "Library cache updated: %s movies, %s series"
            % (
                movie_count,
                series_count
            ),
            level="INFO"
        )

        return {
            "ok": True,
            "busy": False,
            "movies_ok": movies_ok,
            "series_ok": series_ok,
            "movies": movie_count,
            "series": series_count,
            "message": (
                _("%s Filme und %s Serien aktualisiert")
                % (
                    movie_count,
                    series_count
                )
            ),
            "log_message": (
                "%s movies and %s series updated"
                % (
                    movie_count,
                    series_count
                )
            )
        }

    except Exception as e:
        jd_log(
            "Library cache refresh failed: %s"
            % e,
            level="ERROR"
        )

        return {
            "ok": False,
            "busy": False,
            "movies_ok": False,
            "series_ok": False,
            "movies": 0,
            "series": 0,
            "message": str(e),
            "log_message": str(e)
        }

    finally:
        with _LIBRARY_CACHE_LOCK:
            _LIBRARY_UPDATE_IN_PROGRESS = False

            
def jellyfin_get_database_counts():
    try:
        server = jellyfin_server()
        user_id = jellyfin_user_id()
        if not server or not user_id or not jellyfin_token():
            return {"movies": 0, "series": 0, "episodes": 0, "boxsets": 0, "music": 0}

        url = f"{server}/Items/Counts?userId={user_id}"
        result = jellyfin_api_get_ex(url, timeout=TIMEOUT_SHORT)
        data = result.get("data") if result.get("ok") else None

        if data and isinstance(data, dict):
            return {
                "movies": int(data.get("MovieCount") or 0),
                "series": int(data.get("SeriesCount") or 0),
                "episodes": int(data.get("EpisodeCount") or 0),
                "boxsets": int(data.get("BoxSetCount") or 0),
                "music": int(data.get("AlbumCount") or 0) or int(data.get("SongCount") or 0),
            }
    except Exception as e:
        jd_log(f"Counts query for library totals failed: {e}", level="WARNING")
        
    return {
        "movies": jellyfin_count_items("Movie"),
        "series": jellyfin_count_items("Series"),
        "episodes": jellyfin_count_items("Episode"),
        "boxsets": jellyfin_count_items("BoxSet"),
        "music": jellyfin_count_items("MusicAlbum") or jellyfin_count_items("Audio"),
    }


def jellyfin_count_items(item_type):

    try:
        server = jellyfin_server()
        user_id = jellyfin_user_id()
        if not server or not user_id or not jellyfin_token() or not item_type:
            return 0

        result = jellyfin_items_api_get_ex(
            query_params={
                "Recursive": "true",
                "IncludeItemTypes": str(item_type),
                "Limit": 1,
                "CollapseBoxSetItems": "false"
            },
            timeout=TIMEOUT_MIDDLE
        )

        data = (
            result.get("data")
            if result.get("ok")
            else None
        )

        if isinstance(data, dict):
            return int(data.get("TotalRecordCount") or 0)
            
        return 0
    except Exception as e:
        jd_log(
            "Count of Jellyfin items of type '%s' could not be loaded: %s"
            % (
                item_type,
                e
            ),
            level="ERROR"
        )
        return 0


def jellyfin_get_home_latest(limit=6):
    """
    Lädt die neuesten Filme und Serien für die Startseite.

    Die ersten drei Plätze sind Filme, die letzten drei Serien.
    Leere Plätze bleiben frei, damit die Hälften nicht ineinander rutschen.

    Neue Episoden werden auf ihre übergeordnete Serie abgebildet.
    Dadurch erscheint eine bestehende Serie erneut unter
    „Neuerscheinungen“, wenn neue Folgen hinzugefügt wurden.

    Mehrere neue Episoden derselben Serie erzeugen nur einen Eintrag.
    """
    try:
        server = jellyfin_server()
        user_id = jellyfin_user_id()
        token = jellyfin_token()

        if not server or not user_id or not token:
            return []

        try:
            limit = max(1, int(limit or 6))
        except Exception:
            limit = 6

        # Es werden bewusst mehr Einträge geladen als angezeigt.
        #
        # Beispiel:
        # Die zehn neuesten Elemente können mehrere Episoden derselben
        # Serie sein. Nach dem Zusammenfassen soll die Startseite trotzdem
        # möglichst vollständig mit unterschiedlichen Einträgen gefüllt sein.
        fetch_limit = max(
            200,
            limit * 30
        )

        result = jellyfin_items_api_get_ex(
            query_params={
                "Recursive": "true",
                "IncludeItemTypes": "Movie,Series,Episode",
                "CollapseBoxSetItems": "false",
                "SortBy": "DateCreated",
                "SortOrder": "Descending",
                "Limit": fetch_limit,
                "Fields": (
                    "RunTimeTicks,ProductionYear,Genres,"
                    "PremiereDate,SeriesId,SeriesName,"
                    "SeasonId,ParentId,ParentIndexNumber,IndexNumber,"
                    "ImageTags,BackdropImageTags,ProviderIds,UserData"
                )
            },
            timeout=TIMEOUT_MIDDLE
        )

        if not result.get("ok"):
            jd_log(
                "Latest items could not be loaded: %s"
                % (
                    result.get("error")
                    or "Unbekannter Jellyfin-Fehler"
                ),
                level="ERROR"
            )
            return []

        data = result.get("data")

        if not isinstance(data, dict):
            return []

        raw_items = data.get("Items", [])

        if not isinstance(raw_items, list):
            return []

        movie_slots = max(0, limit // 2)
        series_slots = max(0, limit - movie_slots)
        movies = []
        series = []
        added_keys = set()

        for raw_index, raw_item in enumerate(raw_items):
            if not isinstance(raw_item, dict):
                continue

            item_type = str(
                raw_item.get("Type") or ""
            ).strip()

            # ----------------------------------------------------------
            # Neue Episode auf die übergeordnete Serie abbilden.
            # ----------------------------------------------------------
            if item_type == "Episode":
                series_id = str(
                    raw_item.get("SeriesId") or ""
                ).strip()

                series_name = str(
                    raw_item.get("SeriesName") or ""
                ).strip()

                if not series_id:
                    continue

                # Für die Startseite wird ein normaler Serien-Eintrag
                # erzeugt. Die Sortierung bleibt die der neuen Folge.
                home_item = dict(raw_item)

                home_item["Id"] = series_id
                home_item["id"] = series_id
                home_item["Name"] = (
                    series_name
                    or raw_item.get("Name")
                    or "Serie"
                )
                home_item["name"] = home_item["Name"]
                home_item["Type"] = "Series"

                unique_key = "Series:%s" % series_id

            # ----------------------------------------------------------
            # Film oder regulärer Serien-Eintrag.
            # ----------------------------------------------------------
            elif item_type in ("Movie", "Series"):
                item_id = str(
                    raw_item.get("Id") or ""
                ).strip()

                if not item_id:
                    continue

                home_item = dict(raw_item)
                unique_key = "%s:%s" % (
                    item_type,
                    item_id
                )

            else:
                continue

            # Eine Serie nur einmal anzeigen, auch wenn mehrere neue
            # Episoden derselben Serie geliefert wurden.
            if unique_key in added_keys:
                continue

            if (
                len(movies) >= movie_slots
                and len(series) >= series_slots
            ):
                break

            added_keys.add(unique_key)

            normalized_item = jellydream_normalize_item(
                home_item,
                raw_index
            )
            target_is_series = (
                str(home_item.get("Type") or "").strip() == "Series"
            )

            if target_is_series:
                if len(series) >= series_slots:
                    continue

                series.append(normalized_item)
            else:
                if len(movies) >= movie_slots:
                    continue

                movies.append(normalized_item)

            if (
                len(movies) >= movie_slots
                and len(series) >= series_slots
            ):
                break

        movies = jellydream_deduplicate_items(
            movies,
            context="latest movies"
        )
        series = jellydream_deduplicate_items(
            series,
            context="latest series"
        )

        slots = [None] * limit

        for index, item in enumerate(movies[:movie_slots]):
            slots[index] = item

        for index, item in enumerate(series[:series_slots]):
            slots[movie_slots + index] = item

        jd_log(
            "Latest items loaded: %s movies and %s series from %s newest API candidates"
            % (
                sum(1 for item in slots[:movie_slots] if isinstance(item, dict)),
                sum(1 for item in slots[movie_slots:] if isinstance(item, dict)),
                len(raw_items)
            ),
            level="DEBUG"
        )

        return jellydream_apply_favorite_flags(slots)

    except Exception as e:
        jd_log(
            "Latest items could not be loaded: %s"
            % e,
            level="ERROR"
        )

        return []


def _jellyfin_user_item_url(item_id, suffix):
    server = str(jellyfin_server() or "").strip().rstrip("/")
    user_id = str(jellyfin_user_id() or "").strip()
    item_id = str(item_id or "").strip()

    if not server or not user_id or not item_id:
        return ""

    return "%s/Users/%s/%s/%s" % (
        server,
        urllib.parse.quote(user_id, safe=""),
        suffix,
        urllib.parse.quote(item_id, safe="")
    )


def jellyfin_set_item_played_state(item_id, played):
    played = bool(played)
    url = _jellyfin_user_item_url(item_id, "PlayedItems")

    if not url:
        return {
            "ok": False,
            "status": 0,
            "data": None,
            "error": "Not signed in to Jellyfin"
        }

    if played:
        result = jellyfin_api_post_ex(url, {}, timeout=TIMEOUT_SHORT)
    else:
        result = jellyfin_api_delete_ex(url, timeout=TIMEOUT_SHORT)

    if result.get("ok"):
        jd_log(
            "Item marked %s: %s"
            % (
                "played" if played else "unplayed",
                item_id
            ),
            level="INFO"
        )
    else:
        jd_log(
            "Item played state could not be changed: %s | %s"
            % (
                item_id,
                result.get("error") or result.get("status")
            ),
            level="WARNING"
        )

    return result


def jellyfin_get_favorite_ids():
    global _FAVORITE_ID_CACHE

    if isinstance(_FAVORITE_ID_CACHE, set):
        return set(_FAVORITE_ID_CACHE)

    try:
        result = jellyfin_items_api_get_ex(
            query_params={
                "Recursive": "true",
                "IncludeItemTypes": "Movie,Series,Season,Episode,BoxSet",
                "Filters": "IsFavorite",
                "CollapseBoxSetItems": "false",
                "EnableImages": "false",
                "Limit": 3000,
                "Fields": "UserData"
            },
            timeout=TIMEOUT_MIDDLE
        )

        if not result.get("ok"):
            jd_log(
                "Favorite ids could not be loaded: %s"
                % (result.get("error") or "Unknown Jellyfin error"),
                level="WARNING"
            )
            return None

        data = result.get("data")
        raw_items = data.get("Items") if isinstance(data, dict) else []
        favorite_ids = set()

        if isinstance(raw_items, list):
            for item in raw_items:
                if not isinstance(item, dict):
                    continue

                item_id = str(item.get("Id") or "").strip()

                if item_id:
                    favorite_ids.add(item_id)

        _FAVORITE_ID_CACHE = favorite_ids
        jd_log(
            "Favorite ids loaded: %s" % len(favorite_ids),
            level="DEBUG"
        )
        return set(favorite_ids)

    except Exception as e:
        jd_log(
            "Favorite ids could not be loaded: %s" % e,
            level="WARNING"
        )
        return None


def jellydream_note_favorite_id(item_id, favorite):
    global _FAVORITE_ID_CACHE

    if not isinstance(_FAVORITE_ID_CACHE, set):
        return

    item_id = str(item_id or "").strip()

    if not item_id:
        return

    if favorite:
        _FAVORITE_ID_CACHE.add(item_id)
    else:
        _FAVORITE_ID_CACHE.discard(item_id)


def jellydream_apply_favorite_flags(items):
    if not isinstance(items, list):
        return []

    if (
        jellydream_is_gui_thread()
        and not isinstance(_FAVORITE_ID_CACHE, set)
    ):
        return items

    favorite_ids = jellyfin_get_favorite_ids()

    if favorite_ids is None:
        return items

    for item in items:
        if not isinstance(item, dict):
            continue

        item_id = str(
            item.get("Id") or item.get("id") or ""
        ).strip()
        user_data = item.get("UserData")

        if not isinstance(user_data, dict):
            user_data = {}
            item["UserData"] = user_data

        user_data["IsFavorite"] = item_id in favorite_ids

    return items


def jellydream_items_include_date_created(items):
    if not items:
        return True

    for item in list(items)[:8]:
        if isinstance(item, dict) and item.get("DateCreated"):
            return True

    return False


def jellydream_sort_items(items, mode="name"):
    rows = [
        item
        for item in (items or [])
        if isinstance(item, dict)
    ]

    def name_key(item):
        return str(
            item.get("SortName")
            or item.get("Name")
            or ""
        ).casefold()

    if mode == "added":
        dated = [
            item
            for item in rows
            if str(item.get("DateCreated") or "").strip()
        ]
        undated = [
            item
            for item in rows
            if not str(item.get("DateCreated") or "").strip()
        ]
        dated.sort(
            key=lambda item: str(item.get("DateCreated") or ""),
            reverse=True
        )
        undated.sort(key=name_key)
        return dated + undated

    if mode == "artist":
        def artist_key(item):
            artist = item.get("AlbumArtist")

            if isinstance(artist, list):
                artist = artist[0] if artist else ""

            if not str(artist or "").strip():
                artists = item.get("Artists") or []

                if isinstance(artists, list):
                    artist = artists[0] if artists else ""
                else:
                    artist = artists

            return (
                str(artist or "").casefold(),
                name_key(item)
            )

        rows.sort(key=artist_key)
        return rows

    rows.sort(key=name_key)
    return rows


def jellydream_prepare_library_items(items, limit, sort_mode=None):
    if sort_mode not in ("name", "added", "artist"):
        sort_mode = jellydream_library_sort()

    items = jellydream_sort_items(
        jellydream_apply_favorite_flags(items),
        sort_mode
    )

    try:
        limit = int(limit)
    except Exception:
        limit = len(items)

    if limit > 0:
        return items[:limit]

    return items


def jellyfin_set_item_favorite(item_id, favorite):
    favorite = bool(favorite)
    url = _jellyfin_user_item_url(item_id, "FavoriteItems")

    if not url:
        return {
            "ok": False,
            "status": 0,
            "data": None,
            "error": "Not signed in to Jellyfin"
        }

    if favorite:
        result = jellyfin_api_post_ex(url, {}, timeout=TIMEOUT_SHORT)
    else:
        result = jellyfin_api_delete_ex(url, timeout=TIMEOUT_SHORT)

    if result.get("ok"):
        jellydream_note_favorite_id(item_id, favorite)
        jd_log(
            "Item favorite flag set to %s: %s"
            % (favorite, item_id),
            level="INFO"
        )
    else:
        jd_log(
            "Item favorite flag could not be changed: %s | %s"
            % (
                item_id,
                result.get("error") or result.get("status")
            ),
            level="WARNING"
        )

    return result


def jellyfin_get_favorites(limit=2000, raise_on_error=False):
    try:
        result = jellyfin_items_api_get_ex(
            query_params={
                "Recursive": "true",
                "IncludeItemTypes": "Movie,Series,Episode,BoxSet",
                "Filters": "IsFavorite",
                "CollapseBoxSetItems": "false",
                "SortBy": "SortName",
                "SortOrder": "Ascending",
                "Limit": int(limit),
                "Fields": (
                    "RunTimeTicks,ProductionYear,Genres,"
                    "OfficialRating,CommunityRating,Overview,"
                    "ImageTags,BackdropImageTags,ProviderIds,"
                    "UserData,SeriesId,ParentIndexNumber,IndexNumber"
                )
            },
            timeout=TIMEOUT_LONG
        )

        if not result.get("ok"):
            if raise_on_error:
                jellyfin_raise_request_error(
                    result,
                    "Favoriten"
                )

            jd_log(
                "Favorites could not be loaded: %s"
                % (result.get("error") or "Unknown Jellyfin error"),
                level="WARNING"
            )
            return []

        data = result.get("data")
        raw_items = data.get("Items") if isinstance(data, dict) else []

        if not isinstance(raw_items, list):
            return []

        items = [
            jellydream_normalize_item(item, index)
            for index, item in enumerate(raw_items)
            if isinstance(item, dict)
        ]

        items = jellydream_deduplicate_items(
            items,
            context="favorites"
        )

        jd_log(
            "Favorites loaded: %s items" % len(items),
            level="DEBUG"
        )
        return jellydream_apply_favorite_flags(items)

    except JellyfinRequestError:
        raise

    except Exception as e:
        jd_log(
            "Favorites could not be loaded: %s" % e,
            level="ERROR"
        )

        if raise_on_error:
            raise

        return []


def jellyfin_hide_item_from_resume(item_id):
    """
    Nimmt einen Titel aus Weiterschauen, ohne ihn auf ungesehen zu setzen.

    Jellyfin zeigt einen Titel dort, solange PlaybackPositionTicks größer
    als 0 ist. HideFromResume ändert diese Position auf aktuellen Servern
    nicht. Deshalb wird die Fortsetzungsposition auf 0 gesetzt.
    """
    url = _jellyfin_user_item_url(item_id, "Items")

    if not url:
        return {
            "ok": False,
            "status": 0,
            "data": None,
            "error": "Not signed in to Jellyfin"
        }

    result = jellyfin_api_post_ex(
        "%s/UserData" % url,
        {"PlaybackPositionTicks": 0},
        timeout=TIMEOUT_SHORT
    )

    if not result.get("ok") and int(result.get("status") or 0) == 404:
        result = jellyfin_api_post_ex(
            "%s/HideFromResume?Hide=true" % url,
            {},
            timeout=TIMEOUT_SHORT
        )

    if result.get("ok"):
        jd_log(
            "Item hidden from continue watching: %s" % item_id,
            level="INFO"
        )
    else:
        jd_log(
            "Item could not be hidden from continue watching: %s | %s"
            % (
                item_id,
                result.get("error") or result.get("status")
            ),
            level="WARNING"
        )

    return result


def jellyfin_get_resume_items(limit=5):
    """
    Lädt die Weiterschauen-Einträge.

    Jellyfin stellt Resume nicht als moderne Route unter
    /Items/Resume bereit. Dieser Pfad würde als /Items/{itemId}
    interpretiert und mit itemId="Resume" zu HTTP 400 führen.

    Der korrekte Endpunkt lautet:
    /Users/{userId}/Items/Resume
    """
    try:
        auth_snapshot = jellydream_auth_snapshot()

        server = str(
            auth_snapshot.get("server") or ""
        ).strip().rstrip("/")

        user_id = str(
            auth_snapshot.get("user_id") or ""
        ).strip()

        token = str(
            auth_snapshot.get("token") or ""
        ).strip()

        if not server or not user_id or not token:
            return []

        try:
            limit = max(1, int(limit or 5))
        except Exception:
            limit = 5

        query_params = {
            "Recursive": "true",
            "MediaTypes": "Video",
            "Limit": limit,
            "Fields": (
                "RunTimeTicks,UserData,SeriesName,SeriesId,"
                "ParentId,ParentIndexNumber,"
                "IndexNumber,ProductionYear,ImageTags,"
                "BackdropImageTags,ProviderIds"
            )
        }

        url = "%s/Users/%s/Items/Resume?%s" % (
            server,
            urllib.parse.quote(
                user_id,
                safe=""
            ),
            urllib.parse.urlencode(
                query_params
            )
        )

        result = jellyfin_api_get_ex(
            url,
            timeout=TIMEOUT_MIDDLE,
            auth_snapshot=auth_snapshot
        )

        if not result.get("ok"):
            jd_log(
                "Resume items could not be loaded: HTTP %s | %s"
                % (
                    result.get("status") or 0,
                    result.get("error")
                    or "Unbekannter Jellyfin-Fehler"
                ),
                level="ERROR"
            )

            return []

        data = result.get("data")

        if not isinstance(data, dict):
            jd_log(
                "Resume request did not return a valid Jellyfin response",
                level="WARNING"
            )
            return []

        items = data.get("Items", [])

        if not isinstance(items, list):
            return []

        jd_log(
            "Resume items loaded: %s entries"            % len(items),
            level="DEBUG"
        )

        return jellydream_apply_favorite_flags(
            jellydream_deduplicate_items(
                items,
                context="Weiterschauen"
            )
        )

    except Exception as e:
        jd_log(
            "Resume items could not be loaded: %s"            % e,
            level="ERROR"
        )

        return []


def jellydream_library_cache_identity():
    return {
        "server": str(jellyfin_server() or ""),
        "user_id": str(jellyfin_user_id() or "")
    }


def jellydream_empty_library_cache():
    identity = jellydream_library_cache_identity()

    return {
        "version": 1,
        "server": identity["server"],
        "user_id": identity["user_id"],
        "updated": 0,
        "movies_updated": 0,
        "series_updated": 0,
        "movies": [],
        "series": []
    }


def jellydream_read_library_cache():
    try:
        cache_file_path = jellydream_library_cache_file()

        data = jellydream_read_json_file(
            cache_file_path,
            None
        )

        if data is None:
            return None

        if not isinstance(data, dict):
            return None

        identity = jellydream_library_cache_identity()

        cached_server = str(
            data.get("server") or ""
        ).rstrip("/")

        cached_user_id = str(
            data.get("user_id") or ""
        )

        current_server = str(
            identity.get("server") or ""
        ).rstrip("/")

        current_user_id = str(
            identity.get("user_id") or ""
        )

        if (
            cached_server != current_server
            or cached_user_id != current_user_id
        ):
            jd_log(
                "Library cache belongs to another server or user and is ignored",
                level="INFO"
            )
            return None

        movies = data.get("movies")
        series = data.get("series")

        if not isinstance(movies, list):
            data["movies"] = []

        if not isinstance(series, list):
            data["series"] = []

        return data

    except Exception as e:
        jd_log(
            "Library cache could not be read: %s"
            % e,
            level="WARNING"
        )
        return None


def jellydream_write_library_cache(cache_data):
    try:
        if not isinstance(cache_data, dict):
            return False

        return jellydream_write_json_atomic(
            jellydream_library_cache_file(),
            cache_data
        )

    except Exception as e:
        jd_log(
            "Library cache could not be written: %s"
            % e,
            level="ERROR"
        )
        return False


def jellydream_get_library_cache_items(cache_name):
    try:
        cache_data = jellydream_read_library_cache()

        if cache_data is None:
            return None

        items = cache_data.get(cache_name)

        if isinstance(items, list):
            return items

        return None

    except Exception as e:
        jd_log(
            "Library cache '%s' could not be read: %s"
            % (cache_name, e),
            level="WARNING"
        )
        return None


def jellydream_library_cache_is_stale(
    max_age_seconds=24 * 60 * 60
):
    try:
        cache_data = jellydream_read_library_cache()

        if not isinstance(cache_data, dict):
            return True

        now_timestamp = int(time.time())

        movies = cache_data.get("movies")
        series = cache_data.get("series")

        if not isinstance(movies, list):
            return True

        if not isinstance(series, list):
            return True

        movies_updated = int(
            cache_data.get("movies_updated")
            or cache_data.get("updated")
            or 0
        )

        series_updated = int(
            cache_data.get("series_updated")
            or cache_data.get("updated")
            or 0
        )

        if movies_updated <= 0 or series_updated <= 0:
            return True

        movies_age = now_timestamp - movies_updated
        series_age = now_timestamp - series_updated

        return (
            movies_age >= int(max_age_seconds)
            or series_age >= int(max_age_seconds)
        )

    except Exception as e:
        jd_log(
            "Cache age could not be checked: %s"% e,
            level="WARNING"
        )
        return True


def jellydream_refresh_library_cache_if_stale_async():
    global _LIBRARY_UPDATE_IN_PROGRESS

    try:
        if not jellydream_library_auto_update_enabled():
            jd_log(
                "Startup cache check skipped: automatic refresh is disabled",
                level="DEBUG"
            )
            return False

        if not jellyfin_is_logged_in():
            jd_log(
                "Startup cache check skipped: not signed in to Jellyfin",
                level="DEBUG"
            )
            return False

        if not jellydream_library_cache_is_stale():
            jd_log(
                "Library cache is younger than 24 hours",
                level="DEBUG"
            )
            return False

        with _LIBRARY_CACHE_LOCK:
            if _LIBRARY_UPDATE_IN_PROGRESS:
                jd_log(
                    "Startup cache check: a refresh is already running",
                    level="DEBUG"
                )
                return False

        jd_log(
            "Library cache is missing or older than 24 hours -> starting background refresh",
            level="INFO"
        )

        def worker():
            result = jellydream_refresh_library_cache()

            jd_log(
                "Startup cache refresh: %s"
                % jellydream_library_update_log_text(result),
                level=(
                    "INFO"
                    if result.get("ok")
                    else "WARNING"
                )
            )

        started = jellydream_submit_maintenance(worker)

        if not started:
            jd_log(
                "Startup cache refresh was rejected by the worker pool",
                level="WARNING"
            )
            return False

        return True

    except Exception as e:
        jd_log(
            "Startup cache refresh could not be started: %s"
            % e,
            level="WARNING"
        )
        return False


def jellyfin_get_movies(limit=5000, raise_on_error=False, sort_mode=None):

    try:
            
        cached_movies = jellydream_get_library_cache_items(
            "movies"
        )

        if cached_movies is not None:
            full_movies = list(cached_movies)

            mode = (
                sort_mode
                if sort_mode in ("name", "added")
                else jellydream_library_sort()
            )

            if (
                mode != "added"
                or jellydream_items_include_date_created(full_movies)
            ):
                movies = jellydream_prepare_library_items(
                    full_movies,
                    limit,
                    mode
                )
                jellydream_set_search_cache(
                    "movies",
                    movies
                )
                return movies

        fetch_limit = (
            5000
            if cached_movies is not None
            else limit
        )

        ok, movies = jellyfin_fetch_items_ex(
            "Movie",
            fetch_limit,
            raise_on_error=raise_on_error
        )

        if not ok:
            return []

        jellydream_update_library_cache_section(
            "movies",
            movies
        )

        jellydream_set_search_cache(
            "movies",
            movies
        )
        return jellydream_prepare_library_items(
            movies,
            limit,
            sort_mode
        )

    except JellyfinRequestError:
        raise

    except Exception as e:
        jd_log(
            "Error in jellyfin_get_movies: %s" % e,
            level="ERROR"
        )

        if raise_on_error:
            raise JellyfinRequestError(
                "Filmliste konnte nicht geladen werden.",
                technical_error=str(e)
            )

        return []


def jellyfin_get_series(limit=2000, raise_on_error=False, sort_mode=None):
    global _LIBRARY_SERIES_CACHE_STALE

    try:
        cached_series = None

        if not _LIBRARY_SERIES_CACHE_STALE:
            cached_series = jellydream_get_library_cache_items(
                "series"
            )

        if cached_series is not None:
            full_series = list(cached_series)

            mode = (
                sort_mode
                if sort_mode in ("name", "added")
                else jellydream_library_sort()
            )

            if (
                mode != "added"
                or jellydream_items_include_date_created(full_series)
            ):
                series = jellydream_prepare_library_items(
                    full_series,
                    limit,
                    mode
                )
                jellydream_set_search_cache(
                    "series",
                    series
                )
                return series

        ok, series = jellyfin_fetch_items_ex(
            "Series",
            limit,
            raise_on_error=raise_on_error
        )

        if not ok:
            return []

        _LIBRARY_SERIES_CACHE_STALE = False

        jellydream_update_library_cache_section(
            "series",
            series
        )

        jellydream_set_search_cache(
            "series",
            series
        )
        return jellydream_prepare_library_items(
            series,
            limit,
            sort_mode
        )

    except JellyfinRequestError:
        raise

    except Exception as e:
        jd_log(
            "Error in jellyfin_get_series: %s" % e,
            level="ERROR"
        )

        if raise_on_error:
            raise JellyfinRequestError(
                "Serienliste konnte nicht geladen werden.",
                technical_error=str(e)
            )

        return []


def jellyfin_get_music(limit=2000, raise_on_error=False, sort_mode=None):
    from .config import jellydream_music_sort

    mode = (
        sort_mode
        if sort_mode in ("name", "added", "artist")
        else jellydream_music_sort()
    )

    def load_type(item_type):
        result = jellyfin_items_api_get_ex(
            query_params={
                "Recursive": "true",
                "IncludeItemTypes": item_type,
                "SortBy": "SortName",
                "SortOrder": "Ascending",
                "Limit": int(limit),
                "Fields": (
                    "RunTimeTicks,ProductionYear,Genres,Overview,"
                    "ImageTags,BackdropImageTags,ProviderIds,UserData,"
                    "DateCreated,Album,AlbumArtist,AlbumArtists,Artists,"
                    "SortName,IndexNumber"
                )
            },
            timeout=TIMEOUT_LONG
        )

        if not result.get("ok"):
            if raise_on_error:
                jellyfin_raise_request_error(
                    result,
                    "Musikliste"
                )
            return []

        data = result.get("data")

        if not isinstance(data, dict):
            return []

        items = data.get("Items") or []

        if not isinstance(items, list):
            return []

        return [
            item
            for item in items
            if isinstance(item, dict)
        ]

    try:
        albums = load_type("MusicAlbum")

        if not albums:
            albums = load_type("Audio")

        return jellydream_prepare_library_items(
            albums,
            limit,
            mode
        )

    except JellyfinRequestError:
        raise

    except Exception as e:
        jd_log(
            "Music list could not be loaded: %s" % e,
            level="ERROR"
        )

        if raise_on_error:
            raise JellyfinRequestError(
                "Musikliste konnte nicht geladen werden.",
                technical_error=str(e)
            )

        return []


def jellyfin_get_album_first_song(album_id):
    album_id = str(album_id or "").strip()

    if not album_id:
        return None

    result = jellyfin_items_api_get_ex(
        query_params={
            "ParentId": album_id,
            "IncludeItemTypes": "Audio",
            "SortBy": "ParentIndexNumber,IndexNumber",
            "SortOrder": "Ascending",
            "Limit": 1,
            "Fields": "RunTimeTicks,UserData,Name,AlbumArtist"
        },
        timeout=TIMEOUT_MIDDLE
    )

    data = result.get("data") if result.get("ok") else None
    items = data.get("Items") if isinstance(data, dict) else None

    if isinstance(items, list) and items and isinstance(items[0], dict):
        return items[0]

    return None


def jellyfin_get_album_tracks(album_id, raise_on_error=False):
    album_id = str(album_id or "").strip()

    if not album_id:
        return []

    def track_key(item):
        try:
            disc = int(item.get("ParentIndexNumber") or 0)
        except Exception:
            disc = 0

        try:
            number = int(item.get("IndexNumber") or 0)
        except Exception:
            number = 0

        name = str(
            item.get("SortName")
            or item.get("Name")
            or ""
        ).casefold()
        return (disc, number, name)

    try:
        result = jellyfin_items_api_get_ex(
            query_params={
                "ParentId": album_id,
                "IncludeItemTypes": "Audio",
                "SortBy": "ParentIndexNumber,IndexNumber,SortName",
                "SortOrder": "Ascending",
                "Limit": 500,
                "Fields": (
                    "RunTimeTicks,ProductionYear,Genres,Overview,UserData,"
                    "Album,AlbumArtist,AlbumArtists,Artists,SortName,"
                    "IndexNumber,ParentIndexNumber,Name"
                )
            },
            timeout=TIMEOUT_MIDDLE
        )

        if not result.get("ok"):
            if raise_on_error:
                jellyfin_raise_request_error(
                    result,
                    "Album"
                )
            return []

        data = result.get("data")
        items = data.get("Items") if isinstance(data, dict) else None

        if not isinstance(items, list):
            return []

        tracks = [
            item
            for item in items
            if isinstance(item, dict)
        ]
        tracks.sort(key=track_key)
        return jellydream_apply_favorite_flags(tracks)

    except JellyfinRequestError:
        raise

    except Exception as e:
        jd_log(
            "Album tracks could not be loaded: %s" % e,
            level="ERROR"
        )

        if raise_on_error:
            raise JellyfinRequestError(
                "Album konnte nicht geladen werden.",
                technical_error=str(e)
            )

        return []


def jellydream_item_has_image(
    item,
    image_type="Primary"
):
    """
    Prüft anhand der Jellyfin-Metadaten, ob ein Item ein eigenes Bild
    des angeforderten Typs besitzt.

    Dadurch werden keine Bild-URLs aufgerufen, von denen bereits aus den
    Item-Metadaten bekannt ist, dass sie mit HTTP 404 antworten werden.
    """
    try:
        if not isinstance(item, dict):
            return False

        normalized_type = str(
            image_type or "Primary"
        ).strip().lower()

        if normalized_type == "primary":
            image_tags = item.get("ImageTags") or {}

            return bool(
                isinstance(image_tags, dict)
                and image_tags.get("Primary")
            )

        if normalized_type == "backdrop":
            backdrop_tags = (
                item.get("BackdropImageTags")
                or []
            )

            return bool(
                isinstance(backdrop_tags, list)
                and backdrop_tags
            )

        image_tags = item.get("ImageTags") or {}

        if not isinstance(image_tags, dict):
            return False

        for tag_name, tag_value in image_tags.items():
            if (
                str(tag_name or "").strip().lower()
                == normalized_type
                and tag_value
            ):
                return True

        return False

    except Exception:
        return False


def jellydream_enrich_boxset_image_sources(
    boxsets,
    raise_on_error=False
):
    """
    Ergänzt BoxSets ohne eigenes Bild um die Bild-ID eines enthaltenen
    Films.

    Dafür werden alle Filme samt BoxSetConnections in einer einzigen
    Jellyfin-Anfrage geladen. Es entsteht somit nicht für jedes BoxSet
    eine zusätzliche Anfrage.
    """
    try:
        if not isinstance(boxsets, list) or not boxsets:
            return []

        boxsets = [
            dict(boxset)
            for boxset in boxsets
            if isinstance(boxset, dict)
        ]

        missing_boxset_ids = set()

        for boxset in boxsets:
            boxset_id = str(
                boxset.get("Id")
                or boxset.get("id")
                or ""
            ).strip()

            if not boxset_id:
                continue

            if not jellydream_item_has_image(
                boxset,
                "Primary"
            ):
                missing_boxset_ids.add(boxset_id)

        if not missing_boxset_ids:
            return boxsets

        result = jellyfin_items_api_get_ex(
            query_params={
                "Recursive": "true",
                "IncludeItemTypes": "Movie",
                "CollapseBoxSetItems": "false",
                "SortBy": "SortName",
                "SortOrder": "Ascending",
                "Limit": 5000,
                "Fields": (
                    "BoxSetConnections,ImageTags,"
                    "BackdropImageTags"
                )
            },
            timeout=TIMEOUT_LONG
        )

        if not result.get("ok"):
            if raise_on_error:
                jellyfin_raise_request_error(
                    result,
                    "Bildquellen der Sammlungen"
                )

            jd_log(
                "Movie image sources for collections could not be loaded: HTTP %s | %s"
                % (
                    result.get("status") or 0,
                    result.get("error")
                    or "Unbekannter Jellyfin-Fehler"
                ),
                level="WARNING"
            )

            return boxsets

        data = result.get("data")

        if not isinstance(data, dict):
            return boxsets

        movies = data.get("Items") or []

        if not isinstance(movies, list):
            return boxsets

        primary_sources = {}
        backdrop_sources = {}

        # Merkt sich, für welche Sammlung der erste enthaltene Film
        # bereits ausgewertet wurde. Auch ein Film ohne Cover gilt
        # dabei als ausgewertet.
        first_movie_checked = set()

        for movie in movies:
            if not isinstance(movie, dict):
                continue

            movie_id = str(
                movie.get("Id")
                or movie.get("id")
                or ""
            ).strip()

            if not movie_id:
                continue

            connections = (
                movie.get("BoxSetConnections")
                or []
            )

            if not isinstance(connections, list):
                continue

            movie_has_primary = jellydream_item_has_image(
                movie,
                "Primary"
            )

            movie_has_backdrop = jellydream_item_has_image(
                movie,
                "Backdrop"
            )

            for connection in connections:
                if not isinstance(connection, dict):
                    continue

                boxset_id = str(
                    connection.get("Id")
                    or ""
                ).strip()

                if (
                    not boxset_id
                    or boxset_id not in missing_boxset_ids
                    or boxset_id in first_movie_checked
                ):
                    continue

                # Wichtig:
                # Dieser Film ist der erste Film der alphabetisch
                # sortierten Sammlung. Die Sammlung gilt damit als
                # ausgewertet – auch wenn der Film kein Cover besitzt.
                first_movie_checked.add(boxset_id)

                if movie_has_primary:
                    primary_sources[boxset_id] = movie_id

                if movie_has_backdrop:
                    backdrop_sources[boxset_id] = movie_id

        enriched_count = 0

        for boxset in boxsets:
            boxset_id = str(
                boxset.get("Id")
                or boxset.get("id")
                or ""
            ).strip()

            primary_source_id = primary_sources.get(
                boxset_id,
                ""
            )

            backdrop_source_id = backdrop_sources.get(
                boxset_id,
                ""
            )

            if primary_source_id:
                boxset[
                    "_jellydream_primary_image_item_id"
                ] = primary_source_id

                enriched_count += 1

            if backdrop_source_id:
                boxset[
                    "_jellydream_backdrop_image_item_id"
                ] = backdrop_source_id

        jd_log(
            "Collection image sources resolved: %s of %s boxsets use a movie cover"
            % (
                enriched_count,
                len(missing_boxset_ids)
            ),
            level="INFO"
        )

        return boxsets

    except JellyfinRequestError:
        raise

    except Exception as e:
        jd_log(
            "Collection image sources could not be resolved: %s"% e,
            level="WARNING"
        )

        if raise_on_error:
            raise JellyfinRequestError(
                "Bildquellen der Sammlungen konnten nicht "
                "geladen werden.",
                technical_error=str(e)
            )

        return (
            list(boxsets)
            if isinstance(boxsets, list)
            else []
        )

        
def jellyfin_get_boxset(
    limit=100,
    raise_on_error=False
):
    try:
        fetch_limit = max(int(limit or 100), 500)
    except Exception:
        fetch_limit = 500

    ok, boxsets = jellyfin_fetch_items_ex(
        "BoxSet",
        fetch_limit,
        raise_on_error=raise_on_error
    )

    if not ok:
        boxsets = []
    else:
        boxsets = jellydream_enrich_boxset_image_sources(
            boxsets,
            raise_on_error=raise_on_error
        )

    return jellydream_prepare_library_items(
        boxsets,
        limit
    )


def jellyfin_search_items(term, limit=80):
    try:
        term = (term or "").strip()
        if not term:
            return []
            
        server = jellyfin_server()
        user_id = jellyfin_user_id()
        if not server or not user_id or not jellyfin_token():
            return []

        query = urllib.parse.quote(term)
        result = jellyfin_items_api_get_ex(
            query_params={
                "Recursive": "true",
                "SearchTerm": term,
                "IncludeItemTypes": "Movie,Series,BoxSet",
                "CollapseBoxSetItems": "false",
                "SortBy": "SortName",
                "SortOrder": "Ascending",
                "Limit": int(limit),
                "Fields": (
                    "RunTimeTicks,ProductionYear,Genres,Overview,"
                    "People,OfficialRating,ProviderIds,UserData"
                )
            },
            timeout=TIMEOUT_MIDDLE
        )

        data = (
            result.get("data")
            if result.get("ok")
            else None
        )

        if isinstance(data, dict):
            items = data.get("Items")

            if isinstance(items, list) and items:
                normalized_items = [
                    jellydream_normalize_item(
                        item,
                        index
                    )
                    for index, item in enumerate(items)
                    if isinstance(item, dict)
                ]

                boxsets = [
                    item
                    for item in normalized_items
                    if item.get("Type") == "BoxSet"
                ]

                enriched_boxsets = (
                    jellydream_enrich_boxset_image_sources(boxsets)
                    if boxsets
                    else []
                )

                enriched_by_id = {
                    str(item.get("Id") or "").strip(): item
                    for item in enriched_boxsets
                    if isinstance(item, dict)
                }

                normalized_items = [
                    enriched_by_id.get(
                        str(item.get("Id") or "").strip(),
                        item
                    )
                    if item.get("Type") == "BoxSet"
                    else item
                    for item in normalized_items
                ]

                return jellydream_apply_favorite_flags(
                    jellydream_deduplicate_items(
                        normalized_items,
                        context="Jellyfin-Suche '%s'" % term
                    )
                )
                
    except Exception as e:
        jd_log(
            "Exception during primary search for '%s': %s"
            % (
                term,
                e
            ),
            level="ERROR"
        )

    return jellyfin_search_items_fallback(term, limit)


def jellydream_search_key(value):
    try:
        value = (value or "").lower()
        return "".join([ch for ch in value if ch.isalnum()])
    except Exception:
        return ""


def jellyfin_search_items_fallback(term, limit=80):
    result = []
    try:
        needle = jellydream_search_key(term)
        if not needle:
            return []

        items = jellydream_get_search_cache_items()

        if not items:
            server = jellyfin_server()
            user_id = jellyfin_user_id()
            if server and user_id and jellyfin_token():
                result = jellyfin_items_api_get_ex(
                    query_params={
                        "Recursive": "true",
                        "IncludeItemTypes": "Movie,Series,BoxSet",
                        "CollapseBoxSetItems": "false",
                        "Limit": 500,
                        "Fields": "IndexNumber"
                    },
                    timeout=TIMEOUT_MIDDLE
                )

                raw = (
                    result.get("data")
                    if result.get("ok")
                    else None
                )
                if raw and isinstance(raw, dict): 
                    items = raw.get("Items", [])

        for item in items:
            if not isinstance(item, dict):
                continue
            title = item.get("Name", "")
            hay = jellydream_search_key(title)
            if needle in hay or hay in needle:
                result.append(jellydream_normalize_item(item))
                if len(result) >= limit:
                    break
    except Exception as e:
        jd_log(
            "Error during fallback local search: %s" % e,
            level="WARNING"
        )
        
    return jellydream_apply_favorite_flags(
        jellydream_deduplicate_items(
            result,
            context="lokaler Such-Fallback '%s'" % term
        )
    )


def jellydream_update_library_cache_section(
    cache_name,
    items
):
    if cache_name not in ("movies", "series"):
        return False

    cache_file_path = jellydream_library_cache_file()
    cache_lock = jellydream_get_json_lock(
        cache_file_path
    )

    with cache_lock:
        cache_data = jellydream_read_library_cache()

        if not isinstance(cache_data, dict):
            cache_data = jellydream_empty_library_cache()

        now_timestamp = int(time.time())

        cache_data[cache_name] = (
            list(items)
            if isinstance(items, list)
            else []
        )

        cache_data[
            "%s_updated" % cache_name
        ] = now_timestamp

        cache_data["updated"] = now_timestamp

        return jellydream_write_library_cache(
            cache_data
        )

        
_GENRE_MSGID = {
    "action": "Action",
    "abenteuer": "Abenteuer",
    "adventure": "Abenteuer",
    "animation": "Animation",
    "komödie": "Komödie",
    "komodie": "Komödie",
    "comedy": "Komödie",
    "krimi": "Krimi",
    "crime": "Krimi",
    "dokumentation": "Dokumentation",
    "documentary": "Dokumentation",
    "drama": "Drama",
    "familie": "Familie",
    "family": "Familie",
    "fantasy": "Fantasy",
    "historie": "Historie",
    "history": "Historie",
    "geschichte": "Historie",
    "horror": "Horror",
    "musik": "Musik",
    "music": "Musik",
    "mystery": "Mystery",
    "liebesfilm": "Liebesfilm",
    "romance": "Liebesfilm",
    "romantik": "Liebesfilm",
    "romanze": "Liebesfilm",
    "science fiction": "Science-Fiction",
    "science-fiction": "Science-Fiction",
    "sci-fi": "Science-Fiction",
    "scifi": "Science-Fiction",
    "tv-film": "TV-Film",
    "tv film": "TV-Film",
    "tv movie": "TV-Film",
    "thriller": "Thriller",
    "krieg": "Krieg",
    "war": "Krieg",
    "western": "Western",
    "action & adventure": "Action & Abenteuer",
    "action & abenteuer": "Action & Abenteuer",
    "kinder": "Kinder",
    "kids": "Kinder",
    "children": "Kinder",
    "nachrichten": "Nachrichten",
    "news": "Nachrichten",
    "reality": "Reality",
    "reality-tv": "Reality",
    "reality tv": "Reality",
    "sci-fi & fantasy": "Science-Fiction & Fantasy",
    "science fiction & fantasy": "Science-Fiction & Fantasy",
    "science-fiction & fantasy": "Science-Fiction & Fantasy",
    "seifenoper": "Seifenoper",
    "soap": "Seifenoper",
    "soap opera": "Seifenoper",
    "talk": "Talk",
    "talkshow": "Talk",
    "talk show": "Talk",
    "talk-show": "Talk",
    "krieg & politik": "Krieg & Politik",
    "war & politics": "Krieg & Politik",
    "anime": "Anime",
    "sport": "Sport",
    "sports": "Sport",
    "musical": "Musical",
    "kurzfilm": "Kurzfilm",
    "short": "Kurzfilm",
    "biografie": "Biografie",
    "biography": "Biografie",
    "biographie": "Biografie",
    "rock": "Rock",
    "pop": "Pop",
    "jazz": "Jazz",
    "electronic": "Elektronik",
    "elektronik": "Elektronik",
    "electronica": "Elektronik",
    "electronic music": "Elektronik",
    "hip hop": "Hip-Hop",
    "hip-hop": "Hip-Hop",
    "hiphop": "Hip-Hop",
    "classical": "Klassik",
    "klassik": "Klassik",
    "metal": "Metal",
    "heavy metal": "Heavy Metal",
    "hard rock": "Hard Rock",
    "folk": "Folk",
    "blues": "Blues",
    "reggae": "Reggae",
    "soul": "Soul",
    "r&b": "R&B",
    "rnb": "R&B",
    "rhythm and blues": "R&B",
    "rhythm & blues": "R&B",
    "country": "Country",
    "punk": "Punk",
    "indie": "Indie",
    "indie rock": "Indie",
    "house": "House",
    "techno": "Techno",
    "ambient": "Ambient",
    "soundtrack": "Soundtrack",
    "soundtracks": "Soundtrack",
    "alternative": "Alternative",
    "alternative rock": "Alternative",
    "dance": "Dance",
    "disco": "Disco",
    "funk": "Funk",
    "latin": "Latin",
    "latin music": "Latin",
    "world": "Weltmusik",
    "world music": "Weltmusik",
    "weltmusik": "Weltmusik",
    "opera": "Oper",
    "oper": "Oper",
    "rap": "Rap",
    "schlager": "Schlager",
    "volksmusik": "Volksmusik",
    "gospel": "Gospel",
    "singer-songwriter": "Liedermacher",
    "singer/songwriter": "Liedermacher",
    "singer songwriter": "Liedermacher",
    "liedermacher": "Liedermacher",
    "instrumental": "Instrumental",
    "acoustic": "Akustik",
    "akustik": "Akustik",
    "akustisch": "Akustik",
    "new age": "New Age",
    "experimental": "Experimentell",
    "experimentell": "Experimentell",
    "industrial": "Industrial",
    "ska": "Ska",
    "grunge": "Grunge",
    "trance": "Trance",
    "dubstep": "Dubstep",
    "drum and bass": "Drum & Bass",
    "drum & bass": "Drum & Bass",
    "drum'n'bass": "Drum & Bass",
    "edm": "EDM",
    "electronic dance music": "EDM",
    "k-pop": "K-Pop",
    "kpop": "K-Pop",
    "j-pop": "J-Pop",
    "jpop": "J-Pop",
    "progressive rock": "Progressive Rock",
    "prog rock": "Progressive Rock",
    "synthpop": "Synthpop",
    "synth-pop": "Synthpop",
    "synth pop": "Synthpop",
    "easy listening": "Easy Listening",
    "children's music": "Kindermusik",
    "childrens music": "Kindermusik",
    "kindermusik": "Kindermusik",
    "holiday": "Feiertage",
    "holidays": "Feiertage",
}


def jellydream_genre_key(name):
    text = str(name or "").strip().casefold()
    text = text.replace("–", "-").replace("—", "-")
    text = text.replace(" und ", " & ").replace(" and ", " & ")
    return " ".join(text.split())


def jellydream_item_genres(item):
    try:
        genres = item.get("Genres") or []
        return [g for g in genres if g]
    except Exception:
        return []


def jellydream_build_genres(items):
    genres = []
    for item in items:
        for genre in jellydream_item_genres(item):
            if genre not in genres:
                genres.append(genre)
    genres.sort()
    return ["Alle"] + genres


def jellydream_filter_by_played(items, mode="all"):
    if mode not in ("unplayed", "played"):
        return list(items) if isinstance(items, list) else []

    want_played = mode == "played"
    selected = []

    for item in items or []:
        if not isinstance(item, dict):
            continue

        user_data = item.get("UserData") or {}
        played = (
            bool(user_data.get("Played"))
            if isinstance(user_data, dict)
            else False
        )

        if played == want_played:
            selected.append(item)

    return selected


def jellydream_filter_by_genre(items, genre):
    if not genre or genre == "Alle":
        return items
    result = []
    for item in items:
        if genre in jellydream_item_genres(item):
            result.append(item)
    return result


def jellydream_genre_label(genre):
    text = str(genre or "").strip()
    if not text:
        return ""
    if jellydream_genre_key(text) == "alle":
        return _("Alle")
    msgid = _GENRE_MSGID.get(jellydream_genre_key(text))
    if msgid:
        return _(msgid)
    return text


def jellydream_item_genre_line(item, limit=4):
    try:
        labels = [
            jellydream_genre_label(genre)
            for genre in jellydream_item_genres(item)[:limit]
        ]
        labels = [label for label in labels if label]
        return ", ".join(labels) or "-"
    except Exception:
        return "-"



def jellydream_genrebar_text(genres, index):
    try:
        if not genres:
            return "\\c00ffffff<  \\c00f1c40f%s\\c00ffffff  >" % _("Alle")
            
        count = len(genres)
        safe_index = index % count
        
        prev_genre = jellydream_genre_label(
            genres[(safe_index - 1) % count]
        )
        current = jellydream_genre_label(genres[safe_index])
        next_genre = jellydream_genre_label(
            genres[(safe_index + 1) % count]
        )

        return f"\\c00ffffff<  {prev_genre}     \\c00f1c40f[ {current} ]\\c00ffffff     {next_genre}  >"
    except Exception:
        return "\\c00ffffff<  \\c00f1c40f%s\\c00ffffff  >" % _("Alle")


class JellyDreamLibraryScheduler:
    def __init__(self):
        self.timer = eTimer()
        self.running = False

        try:
            self.timer.callback.append(
                self.onTimer
            )
        except Exception:
            try:
                self.timer.timeout.get().append(
                    self.onTimer
                )
            except Exception as e:
                jd_log(
                    "Library scheduler could not be set up: %s" % e,
                    level="ERROR"
                )

    def start(self):
        self.running = True
        self.scheduleNext()

    def stop(self):
        self.running = False

        try:
            self.timer.stop()
        except Exception:
            pass

    def secondsUntilNextUpdate(self):
        try:
            now = time.localtime()

            target_timestamp = time.mktime((
                now.tm_year,
                now.tm_mon,
                now.tm_mday,
                jellydream_library_update_hour(),
                jellydream_library_update_minute(),
                0,
                now.tm_wday,
                now.tm_yday,
                now.tm_isdst
            ))

            if target_timestamp <= time.time():
                target_timestamp += 24 * 60 * 60

            return max(
                30,
                int(target_timestamp - time.time())
            )

        except Exception as e:
            jd_log(
                "Next library update time could not be calculated: %s" % e,
                level="WARNING"
            )
            return 60 * 60

    def scheduleNext(self):
        try:
            self.timer.stop()
        except Exception:
            pass

        if not self.running:
            return

        delay_seconds = self.secondsUntilNextUpdate()

        if jellydream_library_auto_update_enabled():
            next_task_text = (
                "Next library update and namespace cache cleanup"
            )
        else:
            next_task_text = (
                "Next namespace cache cleanup "
                "(library update disabled)"
            )

        jd_log(
            "%s in %s seconds, scheduled for %02d:%02d"            % (
                next_task_text,
                delay_seconds,
                jellydream_library_update_hour(),
                jellydream_library_update_minute()
            ),
            level="INFO"
        )

        try:
            if hasattr(self.timer, "startLongTimer"):
                self.timer.startLongTimer(delay_seconds)
            else:
                self.timer.start(
                    int(delay_seconds * 1000),
                    True
                )
        except Exception as e:
            jd_log(
                "Library scheduler could not be started: %s" % e,
                level="ERROR"
            )

    def onTimer(self):
        if not self.running:
            return

        def worker():
            try:
                cleanup_result = (
                    jellydream_cleanup_old_namespace_caches()
                )

                jd_log(
                    "Scheduled namespace cache cleanup: %s library file(s), %s image-cache director(ies) removed"
                    % (
                        cleanup_result.get(
                            "library_files_removed",
                            0
                        ),
                        cleanup_result.get(
                            "image_directories_removed",
                            0
                        )
                    ),
                    level=(
                        "WARNING"
                        if cleanup_result.get("errors")
                        else "INFO"
                    )
                )

                # Die Bereinigung läuft unabhängig von der Einstellung.
                # Der Netzwerkabruf der Bibliothek bleibt dagegen optional.
                if not jellydream_library_auto_update_enabled():
                    jd_log(
                        "Scheduled library refresh skipped: automatic refresh is disabled",
                        level="INFO"
                    )
                    return

                result = jellydream_refresh_library_cache()

                if not isinstance(result, dict):
                    result = {
                        "ok": False,
                        "message": _(
                            "Die Aktualisierung lieferte "
                            "kein gültiges Ergebnis."
                        ),
                        "log_message": (
                            "The update did not return a valid result"
                        )
                    }

                jd_log(
                    "Scheduled library refresh: %s"
                    % jellydream_library_update_log_text(result),
                    level=(
                        "INFO"
                        if result.get("ok")
                        else "WARNING"
                    )
                )

            except Exception as e:
                jd_log(
                    "Scheduled cache maintenance failed unexpectedly: %s"
                    % e,
                    level="ERROR"
                )

            finally:
                # Auch nach einem Fehler muss ein Folgetermin entstehen.
                # scheduleNext() arbeitet mit eTimer und muss deshalb im
                # GUI-Thread ausgeführt werden.
                scheduled = jellydream_dispatch_to_gui(
                    self.scheduleNext
                )

                if not scheduled:
                    jd_log(
                        "Library scheduler could not be rescheduled on the GUI thread after the job finished",
                        level="ERROR"
                    )

        started = jellydream_submit_maintenance(
            worker
        )

        if not started:
            jd_log(
                "Scheduled cache maintenance was rejected by the worker pool; the next run will be rescheduled",
                level="WARNING"
            )

            # onTimer() läuft im GUI-Thread. Deshalb darf direkt neu
            # geplant werden.
            self.scheduleNext()


def jellydream_start_library_scheduler():
    global _LIBRARY_SCHEDULER

    try:
        if _LIBRARY_SCHEDULER is None:
            _LIBRARY_SCHEDULER = JellyDreamLibraryScheduler()

        _LIBRARY_SCHEDULER.start()

        return True

    except Exception as e:
        jd_log(
            "Library scheduler could not be started: %s" % e,
            level="ERROR"
        )
        return False


def jellydream_restart_library_scheduler():
    global _LIBRARY_SCHEDULER

    try:
        if _LIBRARY_SCHEDULER is None:
            return jellydream_start_library_scheduler()

        _LIBRARY_SCHEDULER.stop()
        _LIBRARY_SCHEDULER.start()

        return True

    except Exception as e:
        jd_log(
            "Library scheduler could not be restarted: %s" % e,
            level="WARNING"
        )
        return False

        
def jellydream_stop_library_scheduler():
    global _LIBRARY_SCHEDULER
    global _PLUGIN_SERVICES_INITIALIZED

    try:
        with _PLUGIN_SERVICES_LOCK:
            scheduler = _LIBRARY_SCHEDULER

            _LIBRARY_SCHEDULER = None
            _PLUGIN_SERVICES_INITIALIZED = False

        if scheduler is not None:
            scheduler.stop()

        jd_log(
            "Plugin services and library scheduler stopped",
            level="INFO"
        )

        return True

    except Exception as e:
        jd_log(
            "Library scheduler could not be stopped: %s"
            % e,
            level="WARNING"
        )
        return False


_PLUGIN_SERVICES_LOCK = threading.RLock()

_PLUGIN_SERVICES_INITIALIZED = False



def jellydream_initialize_plugin_services():
    """
    Wird beim Enigma2-Pluginstart aufgerufen.

    Der Aufruf ist absichtlich idempotent: Enigma2 kann Autostart-Hooks
    mehrfach ausführen, der Scheduler wird trotzdem nur einmal erzeugt.

    Das Initialisierungsflag wird erst nach einem erfolgreichen Start
    gesetzt. Bei einem Fehler bleibt ein späterer erneuter Startversuch
    möglich.
    """

    jd_log(
        "JellyDream Version: %s"
        % JELLYDREAM_VERSION,
        level="INFO"
    )    
    
    global _PLUGIN_SERVICES_INITIALIZED

    with _PLUGIN_SERVICES_LOCK:
        if _PLUGIN_SERVICES_INITIALIZED:
            return True

        try:
            # Konfiguration vor allen Plugin-Diensten vollständig anlegen.
            initial_config = read_config()
    
            jellydream_set_log_level_from_config(
                initial_config
            )

            localeInit(
                (initial_config or {}).get("ui_language", "auto")
            )

            jellydream_set_cache_namespace_from_config(
                initial_config
            )

            started = jellydream_start_library_scheduler()

            if not started:
                jd_log(
                    "Plugin services could not be fully initialized",
                    level="WARNING"
                )
                return False

            _PLUGIN_SERVICES_INITIALIZED = True

            jd_log(
                "Plugin services initialized: library scheduler was started",
                level="INFO"
            )

            return True

        except Exception as e:
            jd_log(
                "Plugin services could not be initialized: %s"
                % e,
                level="ERROR"
            )
            return False

        
def jellyfin_get_episodes(series_id, season=1, raise_on_error=False):
    try:
        series_id = str(series_id or "").strip()

        season = jellydream_int_or_default(
            season,
            1
        )

        server = jellyfin_server()
        user_id = jellyfin_user_id()
        token = jellyfin_token()

        if not series_id:
            if raise_on_error:
                raise JellyfinRequestError(
                    "Serien-ID fehlt."
                )
            return []

        if not server or not user_id or not token:
            if raise_on_error:
                raise JellyfinRequestError(
                    "Jellyfin-Anmeldung ist unvollständig."
                )
            return []

        cache_key = "%s|%s|%s|%s" % (
            server,
            user_id,
            series_id,
            season
        )

        cached = jellydream_cache_get(
            _GLOBAL_EPISODES_CACHE,
            cache_key,
            None
        )

        if cached is not None:
            return jellydream_apply_favorite_flags(cached)

        url = (
            f"{server}/Shows/"
            f"{urllib.parse.quote(series_id, safe='')}/Episodes?"
            f"Season={season}"
            f"&userId={urllib.parse.quote(str(user_id), safe='')}"
            f"&Fields=RunTimeTicks,ParentIndexNumber,IndexNumber,"
            f"SeriesId,SeriesName,SeasonId,ParentId,"
            f"Overview,PremiereDate,ProductionYear,"
            f"OfficialRating,ImageTags,ProviderIds,UserData"
        )

        result = jellyfin_api_get_ex(
            url,
            timeout=TIMEOUT_LONG
        )

        if not result.get("ok"):
            if raise_on_error:
                jellyfin_raise_request_error(
                    result,
                    "Folgenliste der Staffel %s" % season
                )
            return []

        data = result.get("data")

        if not isinstance(data, dict):
            if raise_on_error:
                raise JellyfinRequestError(
                    "Ungültige Antwort vom Jellyfin-Server."
                )
            return []

        raw_items = data.get("Items", [])

        if not isinstance(raw_items, list):
            raw_items = []

        episodes = [
            jellydream_normalize_item(item, index + 1)
            for index, item in enumerate(raw_items)
            if isinstance(item, dict)
        ]

        jellydream_cache_set(
            _GLOBAL_EPISODES_CACHE,
            cache_key,
            episodes
        )

        return jellydream_apply_favorite_flags(episodes)

    except JellyfinRequestError:
        raise

    except Exception as e:
        jd_log(
            "ERROR in jellyfin_get_episodes for series '%s' (season %s): %s"
            % (
                series_id,
                season,
                e
            ),
            level="ERROR"
        )

        if raise_on_error:
            raise JellyfinRequestError(
                "Folgen konnten nicht geladen werden.",
                technical_error=str(e)
            )

        return []

        
def jellyfin_get_boxset_items(boxset_id, raise_on_error=False):
    try:
        boxset_id = str(boxset_id or "").strip()

        if not boxset_id:
            if raise_on_error:
                raise JellyfinRequestError(
                    "Sammlungs-ID fehlt."
                )
            return []

        server = jellyfin_server()
        user_id = jellyfin_user_id()
        token = jellyfin_token()

        if not server or not user_id or not token:
            if raise_on_error:
                raise JellyfinRequestError(
                    "Jellyfin-Anmeldung ist unvollständig."
                )
            return []

        cache_key = "%s|%s|%s|userdata" % (
            server,
            user_id,
            boxset_id
        )

        cached = jellydream_cache_get(
            _GLOBAL_BOXSET_CACHE,
            cache_key,
            None
        )

        if cached is not None:
            return jellydream_apply_favorite_flags(cached)

        result = jellyfin_items_api_get_ex(
            query_params={
                "Recursive": "true",
                "ParentId": boxset_id,
                "SortBy": "SortName",
                "SortOrder": "Ascending",
                "Fields": (
                    "RunTimeTicks,ProductionYear,Overview,Genres,"
                    "OfficialRating,CommunityRating,"
                    "ImageTags,ProviderIds,UserData"
                )
            },
            timeout=TIMEOUT_LONG
        )

        if not result.get("ok"):
            if raise_on_error:
                jellyfin_raise_request_error(
                    result,
                    "Filme der Sammlung"
                )
            return []

        data = result.get("data")

        if not isinstance(data, dict):
            if raise_on_error:
                raise JellyfinRequestError(
                    "Ungültige Antwort vom Jellyfin-Server."
                )
            return []

        raw_items = data.get("Items", [])

        if not isinstance(raw_items, list):
            raw_items = []

        items = [
            jellydream_normalize_item(item, index)
            for index, item in enumerate(raw_items)
            if isinstance(item, dict)
        ]

        items = jellydream_deduplicate_items(
            items,
            context="Sammlung %s" % boxset_id
        )

        jellydream_cache_set(
            _GLOBAL_BOXSET_CACHE,
            cache_key,
            items
        )

        return jellydream_apply_favorite_flags(items)

    except JellyfinRequestError:
        raise

    except Exception as e:
        jd_log(
            "ERROR in jellyfin_get_boxset_items for collection '%s': %s"            % (boxset_id, e),
            level="ERROR"
        )

        if raise_on_error:
            raise JellyfinRequestError(
                "Filme der Sammlung konnten nicht geladen werden.",
                technical_error=str(e)
            )

        return []
