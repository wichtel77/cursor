# -*- coding: utf-8 -*-

import copy
import threading
import uuid

import urllib.request
import urllib.parse
import urllib.error

from Components.ActionMap import ActionMap
from Components.Label import Label
from Components.MenuList import MenuList
from Screens.MessageBox import MessageBox
from Components.Pixmap import Pixmap
from Components.ProgressBar import ProgressBar
from Screens.Screen import Screen
from Components.ServiceEventTracker import ServiceEventTracker
from enigma import ePoint
from enigma import eServiceReference
from enigma import eTimer
from enigma import iPlayableService
from enigma import iServiceInformation
from skin import parseColor
from time import strftime

from . import _

from .constants import (
    ENIGMA_SEEK_CLOCK,
    FALLBACK_HOME_COVER,
    INFOBAR_WIDGETS,
    JOB_PRIORITY_PLAYBACK,
    SERVICE_REFERENCE,
    TICKS_PER_SECOND,
    TIMEOUT_LONG,
    TIMEOUT_MIDDLE,
    TIMEOUT_POST,
    jellydream_get_lcd_size
)
from .log import get_log_level, jd_log
from .workers import (
    jellydream_dispatch_to_gui,
    jellydream_is_gui_thread,
    jellydream_submit_playback
)
from .config import (
    jellydream_allow_ac3_direct,
    jellydream_allow_dts_direct,
    jellydream_allow_eac3_direct,
    jellydream_allow_truehd_direct,
    jellydream_cache_dir,
    jellydream_default_audio_language,
    jellydream_device_id,
    jellydream_get_service_reference,
    jellydream_max_resolution,
    jellydream_next_episode_autoplay,
    jellydream_skip_intro_automatically,
    jellydream_preferred_audio_codec,
    jellydream_stream_mode,
    jellydream_stream_protocol,
    jellydream_video_bitrate,
    jellyfin_server,
    jellyfin_token
)
from .http import (
    JellyfinRequestError,
    jellydream_api_post_async,
    jellydream_auth_snapshot,
    jellydream_auth_snapshot_matches_current,
    jellydream_build_authenticated_player_url,
    jellyfin_api_get_ex,
    jellyfin_api_post_ex,
    jellyfin_raise_request_error
)
from .images import (
    jellydream_async_download_image,
    jellydream_cache_get,
    jellydream_cache_set,
    jellydream_get_cached_image_path,
    jellydream_valid_file
)
from .uiutil import (
    jellydream_int_or_default,
    jellydream_music_queue_clear,
    jellydream_music_queue_has_items,
    jellydream_music_queue_pop,
    jellydream_restart_single_timer,
    jellydream_apply_status_symbol_font,
    jellydream_bind_status_symbols,
    jellydream_safe_open,
    jellydream_safe_set_pixmap,
    jellydream_stop_timer,
    jellydream_wrap,
    ticks_to_seconds
)
from .library import (
    jellydream_item_overview,
    jellydream_load_item_details_async,
    jellyfin_get_episodes,
    jellyfin_get_item_details,
    _GLOBAL_DETAILS_CACHE
)
from .ui import JellyDreamPlayerBaseScreen

_PLAYBACK_PROGRESS_LOCK = threading.Lock()
_PLAYBACK_PROGRESS_IN_FLIGHT = False


def jellydream_build_playback_payload(
    item_id,
    position_seconds=0,
    user_id="",
    media_source_id="",
    play_session_id="",
    play_method="DirectStream",
    is_paused=False,
    can_seek=True,
    event_type=""
):
    """
    Erstellt einen einheitlichen Jellyfin-Payload für:

    - /Sessions/Playing
    - /Sessions/Playing/Progress
    - /Sessions/Playing/Stopped

    Alle drei Meldungen verwenden dieselben Basisfelder. Nur tatsächlich
    ereignisspezifische Felder werden zusätzlich ergänzt.
    """
    try:
        item_id = str(
            item_id or ""
        ).strip()

        user_id = str(
            user_id or ""
        ).strip()

        media_source_id = str(
            media_source_id or item_id
        ).strip()

        play_session_id = str(
            play_session_id or ""
        ).strip()

        play_method = str(
            play_method or "DirectStream"
        ).strip()

        if play_method not in (
            "DirectPlay",
            "DirectStream",
            "Transcode"
        ):
            play_method = "DirectStream"

        try:
            position_seconds = max(
                0,
                int(position_seconds or 0)
            )
        except Exception:
            position_seconds = 0

        payload = {
            "ItemId": item_id,
            "UserId": user_id,
            "MediaSourceId": media_source_id,
            "PositionTicks": int(
                position_seconds * TICKS_PER_SECOND
            ),
            "PlayMethod": play_method,
            "CanSeek": bool(can_seek),
            "IsPaused": bool(is_paused),
            "IsMuted": False
        }

        if play_session_id:
            payload["PlaySessionId"] = play_session_id

        event_type = str(
            event_type or ""
        ).strip().lower()

        if event_type == "progress":
            payload["EventName"] = "TimeUpdate"

        elif event_type == "stop":
            payload["Failed"] = False

        return payload

    except Exception as e:
        jd_log(
            "Playback payload could not be built: %s"
            % e,
            level="WARNING"
        )

        return {}


def jellyfin_report_progress(
    item_id,
    position_seconds,
    runtime_seconds=0,
    title="",
    is_paused=False,
    play_session_id="",
    media_source_id="",
    stream_url=""
):
    """
    Meldet die aktuelle Wiedergabeposition an Jellyfin.

    Start, Progress und Stop verwenden denselben Basis-Payload.
    Während eine Progress-Anfrage läuft, wird keine zweite parallele
    Progress-Meldung gestartet.
    """
    global _PLAYBACK_PROGRESS_IN_FLIGHT

    progress_lock_acquired = False

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

        item_id = str(
            item_id or ""
        ).strip()

        if not server or not user_id or not token or not item_id:
            return False

        position_seconds = max(
            0,
            int(position_seconds or 0)
        )

        runtime_seconds = max(
            0,
            int(runtime_seconds or 0)
        )

        play_method = jellydream_get_play_method_from_stream_url(
            stream_url
        )

        payload = jellydream_build_playback_payload(
            item_id=item_id,
            position_seconds=position_seconds,
            user_id=user_id,
            media_source_id=media_source_id,
            play_session_id=play_session_id,
            play_method=play_method,
            is_paused=is_paused,
            can_seek=True,
            event_type="progress"
        )

        if not payload:
            return False

        with _PLAYBACK_PROGRESS_LOCK:
            if _PLAYBACK_PROGRESS_IN_FLIGHT:
                jd_log(
                    "Progress report skipped: a previous report is still running",
                    level="DEBUG"
                )

                return False

            _PLAYBACK_PROGRESS_IN_FLIGHT = True
            progress_lock_acquired = True

        url = "%s/Sessions/Playing/Progress" % server

        jd_log(
            "Sending Jellyfin progress: '%s' at %ss / %ss (paused=%s, MediaSource=%s, PlayMethod=%s, PlaySession=%s)"
            % (
                title or item_id,
                position_seconds,
                runtime_seconds,
                bool(is_paused),
                payload.get("MediaSourceId") or "-",
                play_method,
                payload.get("PlaySessionId") or "-"
            ),
            level="DEBUG"
        )

        def worker():
            global _PLAYBACK_PROGRESS_IN_FLIGHT

            try:
                result = jellyfin_api_post_ex(
                    url,
                    payload,
                    timeout=TIMEOUT_MIDDLE,
                    auth_snapshot=auth_snapshot
                )

                if not result.get("ok"):
                    jd_log(
                        "Jellyfin progress report failed: %s | HTTP %s | %s"
                        % (
                            title or item_id,
                            result.get("status") or 0,
                            result.get("error")
                            or "Unbekannter Jellyfin-Fehler"
                        ),
                        level="WARNING"
                    )

            except Exception as e:
                jd_log(
                    "Progress report to Jellyfin failed: %s"
                    % e,
                    level="WARNING"
                )

            finally:
                with _PLAYBACK_PROGRESS_LOCK:
                    _PLAYBACK_PROGRESS_IN_FLIGHT = False

        started = jellydream_submit_playback(
            worker
        )

        if not started:
            with _PLAYBACK_PROGRESS_LOCK:
                _PLAYBACK_PROGRESS_IN_FLIGHT = False

        return started

    except Exception as e:
        jd_log(
            "Error in jellyfin_report_progress: %s" % e,
            level="WARNING"
        )

        if progress_lock_acquired:
            with _PLAYBACK_PROGRESS_LOCK:
                _PLAYBACK_PROGRESS_IN_FLIGHT = False

        return False


def jellydream_get_play_method_from_stream_url(stream_url):
    """
    Ermittelt den an Jellyfin zu meldenden PlayMethod-Wert anhand der
    tatsächlich erzeugten Stream-URL.

    Mögliche Jellyfin-Werte:
    - DirectPlay
    - DirectStream
    - Transcode
    """
    try:
        stream_url = str(stream_url or "").strip()

        if not stream_url:
            return "DirectStream"

        parsed_url = urllib.parse.urlparse(
            stream_url
        )

        query = urllib.parse.parse_qs(
            parsed_url.query
        )

        def get_param(name, default=""):
            values = query.get(name, [])

            if values:
                return str(values[0] or "").strip()

            return default

        static_mode = (
            get_param("Static", "").lower()
            == "true"
        )

        video_codec = get_param(
            "VideoCodec",
            ""
        ).lower()

        audio_codec = get_param(
            "AudioCodec",
            ""
        ).lower()

        # /stream.mkv?Static=true liefert die Originaldatei direkt.
        if static_mode:
            return "DirectPlay"

        # Copy + Copy über stream.ts / HLS bedeutet Remux / Direct Stream.
        if (
            video_codec == "copy"
            and audio_codec == "copy"
        ):
            return "DirectStream"

        # Video Copy, Audio wird umgewandelt.
        if video_codec == "copy" and audio_codec:
            return "Transcode"

        # Video wird umgewandelt, unabhängig davon ob Audio kopiert oder
        # ebenfalls transcodiert wird.
        if video_codec and video_codec != "copy":
            return "Transcode"

        if audio_codec and audio_codec != "copy":
            return "Transcode"

        # Sichere Voreinstellung für nicht eindeutig erkennbare URLs.
        return "DirectStream"

    except Exception as e:
        jd_log(
            "PlayMethod could not be read from the stream URL: %s" % e,
            level="DEBUG"
        )

        return "DirectStream"

        
def jellyfin_report_start(
    item_id,
    title="",
    position_seconds=0,
    play_session_id="",
    media_source_id="",
    stream_url=""
):
    """
    Meldet den Wiedergabestart mit demselben Basis-Payload wie Progress
    und Stop.
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

        item_id = str(
            item_id or ""
        ).strip()

        if not server or not user_id or not token or not item_id:
            return False

        play_method = jellydream_get_play_method_from_stream_url(
            stream_url
        )

        payload = jellydream_build_playback_payload(
            item_id=item_id,
            position_seconds=position_seconds,
            user_id=user_id,
            media_source_id=media_source_id,
            play_session_id=play_session_id,
            play_method=play_method,
            is_paused=False,
            can_seek=True,
            event_type="start"
        )

        if not payload:
            return False

        url = "%s/Sessions/Playing" % server

        jd_log(
            "Reporting playback start to Jellyfin: '%s' (item=%s, MediaSource=%s, position=%ss, PlayMethod=%s, PlaySession=%s)"
            % (
                title or item_id,
                item_id,
                payload.get("MediaSourceId") or "-",
                max(0, int(position_seconds or 0)),
                play_method,
                payload.get("PlaySessionId") or "-"
            ),
            level="DEBUG"
        )

        if jellydream_is_gui_thread():
            return jellydream_api_post_async(
                url,
                payload,
                timeout=TIMEOUT_POST,
                priority=JOB_PRIORITY_PLAYBACK
            )

        result = jellyfin_api_post_ex(
            url,
            payload,
            timeout=TIMEOUT_POST,
            auth_snapshot=auth_snapshot
        )

        return bool(
            result.get("ok")
        )

    except Exception as e:
        jd_log(
            "Error in jellyfin_report_start: %s" % e,
            level="WARNING"
        )

        return False


def jellyfin_report_stop(
    item_id,
    position_seconds,
    title="",
    media_source_id="",
    play_session_id="",
    is_paused=False,
    stream_url=""
):
    """
    Meldet das Wiedergabeende mit demselben Basis-Payload wie Start
    und Progress.
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

        item_id = str(
            item_id or ""
        ).strip()

        if not server or not user_id or not token or not item_id:
            return False

        position_seconds = max(
            0,
            int(position_seconds or 0)
        )

        play_method = jellydream_get_play_method_from_stream_url(
            stream_url
        )

        payload = jellydream_build_playback_payload(
            item_id=item_id,
            position_seconds=position_seconds,
            user_id=user_id,
            media_source_id=media_source_id,
            play_session_id=play_session_id,
            play_method=play_method,
            is_paused=is_paused,
            can_seek=True,
            event_type="stop"
        )

        if not payload:
            return False

        url = "%s/Sessions/Playing/Stopped" % server

        jd_log(
            "Reporting playback stop to Jellyfin: '%s' at %ss (item=%s, MediaSource=%s, PlayMethod=%s, PlaySession=%s)"
            % (
                title or item_id,
                position_seconds,
                item_id,
                payload.get("MediaSourceId") or "-",
                play_method,
                payload.get("PlaySessionId") or "-"
            ),
            level="DEBUG"
        )

        if jellydream_is_gui_thread():
            return jellydream_api_post_async(
                url,
                payload,
                timeout=TIMEOUT_POST,
                priority=JOB_PRIORITY_PLAYBACK
            )

        result = jellyfin_api_post_ex(
            url,
            payload,
            timeout=TIMEOUT_POST,
            auth_snapshot=auth_snapshot
        )

        return bool(
            result.get("ok")
        )

    except Exception as e:
        jd_log(
            "Playback stop could not be reported to Jellyfin: %s"
            % e,
            level="WARNING"
        )

        return False


def jellydream_stream_debug_text(stream_url, service_reference=SERVICE_REFERENCE):
    try:
        if str(get_log_level() or "").upper() != "DEBUG":
            return ""

        stream_url = str(stream_url or "").strip()

        if not stream_url:
            return "DEBUG: Stream-Methode wird ermittelt ..."

        parsed_url = urllib.parse.urlparse(stream_url)
        query = urllib.parse.parse_qs(parsed_url.query)

        stream_path = str(parsed_url.path or "").lower()

        if stream_path.endswith(".m3u8"):
            output_format = "HLS"
        elif stream_path.endswith(".ts"):
            output_format = "TS"
        elif stream_path.endswith(".mkv"):
            output_format = "MKV"
        else:
            output_format = "Unbekannt"
    
        def get_param(name, default=""):
            values = query.get(name, [])
            if values:
                return str(values[0] or "").strip()
            return default

        static_mode = get_param("Static", "").lower() == "true"
        video_codec = get_param("VideoCodec", "").strip().upper()
        audio_codec = get_param("AudioCodec", "").strip().upper()

        player_names = {
            4097: "Enigma2",
            5001: "GStreamer",
            5002: "ExtePlayer3"
        }

        try:
            service_reference = int(service_reference)
        except Exception:
            service_reference = SERVICE_REFERENCE

        player_name = player_names.get(
            service_reference,
            "Service %s" % service_reference
        )

        if static_mode:
            method = "DIRECT PLAY"
            video_text = "Original"
            audio_text = "Original"

        elif video_codec == "COPY" and audio_codec == "COPY":
            method = "DIRECT STREAM / REMUX"
            video_text = "Copy"
            audio_text = "Copy"

        elif video_codec == "COPY":
            method = "AUDIO-TRANSCODING"
            video_text = "Copy"
            audio_text = audio_codec or "Transcode"

        elif audio_codec == "COPY":
            method = "VIDEO-TRANSCODING"
            video_text = video_codec or "Transcode"
            audio_text = "Copy"

        else:
            method = "VIDEO + AUDIO-TRANSCODING"
            video_text = video_codec or "Transcode"
            audio_text = audio_codec or "Transcode"

        return (
            "DEBUG: %s  |  Ausgabe: %s  |  Player: %s (%s)  |  Video: %s  |  Audio: %s"
            % (
                method,
                output_format,
                player_name,
                service_reference,
                video_text,
                audio_text
            )
        )

    except Exception as e:
        jd_log(
            "Debug infobar could not be built: %s" % e,
            level="DEBUG"
        )
        return "DEBUG: Stream-Information nicht verfügbar"


def jellyfin_find_audio_stream_index_by_language(item_id, preferred_language, preloaded_details=None):
    try:
        preferred_language = str(preferred_language or "").strip().lower()
        if not item_id or not preferred_language:
            return None

        item = (
            preloaded_details
            if isinstance(preloaded_details, dict)
            else {}
        )

        if not item:
            if jellydream_is_gui_thread():
                jd_log(
                    "Audio metadata without preloaded details was blocked on the GUI thread: %s"
                    % item_id,
                    level="WARNING"
                )
                return None

            item = jellyfin_get_item_details(
                item_id
            ) or {}
            
        sources = item.get("MediaSources") or []
        if not sources or not isinstance(sources, list):
            return None

        first_source = sources[0] if sources else {}
        if not isinstance(first_source, dict):
            return None

        streams = first_source.get("MediaStreams") or []
        if not streams:
            return None

        language_aliases = {
            "de": ["de", "deu", "ger", "german", "deutsch"],
            "en": ["en", "eng", "english"],
            "fr": ["fr", "fra", "fre", "french", "francais", "français"],
            "it": ["it", "ita", "italian", "italiano"],
            "es": ["es", "spa", "spanish", "espanol", "español"],
            "nl": ["nl", "nld", "dut", "dutch", "nederlands"],
            "pl": ["pl", "pol", "polish", "polski"],
            "pt": ["pt", "por", "portuguese", "português", "portugues"],
            "ru": ["ru", "rus", "russian", "русский"],
            "tr": ["tr", "tur", "turkish", "türkçe", "turkce"],
            "cs": ["cs", "ces", "cze", "czech", "čeština", "cestina"],
            "sk": ["sk", "slk", "slo", "slovak", "slovenčina", "slovencina"],
            "hu": ["hu", "hun", "hungarian", "magyar"],
            "ro": ["ro", "ron", "rum", "romanian", "română", "romana"],
            "bg": ["bg", "bul", "bulgarian", "български"],
            "el": ["el", "ell", "gre", "greek", "ελληνικά"],
            "sv": ["sv", "swe", "swedish", "svenska"],
            "no": ["no", "nor", "norwegian", "norsk"],
            "da": ["da", "dan", "danish", "dansk"],
            "fi": ["fi", "fin", "finnish", "suomi"],
            "hr": ["hr", "hrv", "croatian", "hrvatski"],
            "sr": ["sr", "srp", "serbian", "srpski"],
            "sl": ["sl", "slv", "slovenian", "slovenščina", "slovenscina"],
            "uk": ["uk", "ukr", "ukrainian", "українська"],
            "ja": ["ja", "jpn", "japanese", "日本語"],
            "ko": ["ko", "kor", "korean", "한국어"],
            "zh": ["zh", "zho", "chi", "chinese", "中文"],
        }

        wanted_values = language_aliases.get(preferred_language, [preferred_language])

        def normalize_lang(value):
            return str(value or "").strip().lower()

        exact_matches = []
        partial_matches = []

        for stream in streams:
            if not isinstance(stream, dict):
                continue
            if stream.get("Type") != "Audio":
                continue

            stream_index = stream.get("Index", None)
            if stream_index is None:
                continue

            candidates = [
                normalize_lang(stream.get("Language")),
                normalize_lang(stream.get("DisplayLanguage")),
                normalize_lang(stream.get("Title")),
                normalize_lang(stream.get("DisplayTitle")),
            ]
            candidates = [c for c in candidates if c]

            try:
                stream_index = int(stream_index)
            except Exception:
                continue

            if any(c in wanted_values for c in candidates):
                exact_matches.append(stream_index)
                continue

            if any(any(c.startswith(w) or w in c for w in wanted_values) for c in candidates):
                partial_matches.append(stream_index)
                continue

        if exact_matches:
            return exact_matches[0]

        if partial_matches:
            return partial_matches[0]

        return None
    except Exception as e:
        jd_log(f"Error in jellyfin_find_audio_stream_index_by_language: {e}", level="WARNING")
        return None


def jellydream_select_media_source(
    sources,
    preferred_media_source_id=""
):
    """
    Wählt eine konkrete Jellyfin-MediaSource.

    Priorität:
    1. Explizit angeforderte MediaSource-ID.
    2. Erste gültige, nicht platzhalterhafte Quelle.
    3. Erste gültige Quelle als Fallback.
    """
    try:
        if not isinstance(sources, list):
            return {}

        preferred_media_source_id = str(
            preferred_media_source_id or ""
        ).strip()

        valid_sources = [
            source
            for source in sources
            if isinstance(source, dict)
        ]

        if not valid_sources:
            return {}

        if preferred_media_source_id:
            for source in valid_sources:
                source_id = str(
                    source.get("Id") or ""
                ).strip()

                if source_id == preferred_media_source_id:
                    jd_log(
                        "Requested MediaSource selected: %s"                       % source_id,
                        level="INFO"
                    )
                    return source

            jd_log(
                "Requested MediaSource '%s' was not found; using the Jellyfin default source"
                % preferred_media_source_id,
                level="WARNING"
            )

        # Placeholder-Quellen sind für Wiedergabe nicht sinnvoll.
        for source in valid_sources:
            if not bool(source.get("IsPlaceholder", False)):
                return source

        return valid_sources[0]

    except Exception as e:
        jd_log(
            "MediaSource selection failed: %s" % e,
            level="WARNING"
        )
        return {}



def jellyfin_get_playback_info(item_id, start_position_seconds=0, preferred_media_source_id="", raise_on_error=False, auth_snapshot=None):
    """
    Holt serverseitige Wiedergabeinformationen über /PlaybackInfo.

    Der Rückgabewert enthält:
    - item_details: Originaldaten mit der konkret gewählten MediaSource
    - media_source_id: ID der ausgewählten Quelle
    - play_session_id: von Jellyfin erzeugte PlaySessionId
    """
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

        if not server or not user_id or not token or not item_id:
            if raise_on_error:
                raise JellyfinRequestError(
                    "Jellyfin-Anmeldung oder Item-ID fehlt."
                )
            return {}

        start_position_seconds = max(
            0,
            int(start_position_seconds or 0)
        )

        url = "%s/Items/%s/PlaybackInfo" % (
            server,
            urllib.parse.quote(item_id, safe="")
        )

        payload = {
            "UserId": str(user_id),
            "StartTimeTicks": int(
                start_position_seconds * TICKS_PER_SECOND
            ),
            "AutoOpenLiveStream": False
        }

        result = jellyfin_api_post_ex(
            url,
            payload,
            timeout=TIMEOUT_LONG,
            auth_snapshot=auth_snapshot
        )

        if not result.get("ok"):
            if raise_on_error:
                jellyfin_raise_request_error(
                    result,
                    "Wiedergabeinformationen"
                )
            return {}

        playback_info = result.get("data")

        if not isinstance(playback_info, dict):
            if raise_on_error:
                raise JellyfinRequestError(
                    "Ungültige PlaybackInfo-Antwort von Jellyfin."
                )
            return {}

        sources = playback_info.get("MediaSources") or []

        selected_source = jellydream_select_media_source(
            sources,
            preferred_media_source_id
        )

        if not selected_source:
            if raise_on_error:
                raise JellyfinRequestError(
                    "Jellyfin lieferte keine verwendbare Medienquelle."
                )
            return {}

        media_source_id = str(
            selected_source.get("Id") or ""
        ).strip()

        play_session_id = str(
            playback_info.get("PlaySessionId") or ""
        ).strip()

        jd_log(
            "PlaybackInfo resolved: item=%s, MediaSource=%s, container=%s, protocol=%s, PlaySession=%s"
            % (
                item_id,
                media_source_id or "-",
                selected_source.get("Container") or "-",
                selected_source.get("Protocol") or "-",
                play_session_id or "-"
            ),
            level="INFO"
        )

        return {
            "playback_info": playback_info,
            "media_source": selected_source,
            "media_source_id": media_source_id,
            "play_session_id": play_session_id
        }

    except JellyfinRequestError:
        raise

    except Exception as e:
        jd_log(
            "PlaybackInfo could not be loaded for item '%s': %s"            % (
                item_id,
                e
            ),
            level="WARNING"
        )

        if raise_on_error:
            raise JellyfinRequestError(
                "Wiedergabeinformationen konnten nicht geladen werden.",
                technical_error=str(e)
            )

        return {}

        
def build_jellyfin_stream(
    item_id,
    audio_stream_index=None,
    subtitle_stream_index=None,
    start_position_seconds=0,
    preloaded_details=None,
    play_session_id=None,
    preferred_media_source_id=""
):
    token = jellyfin_token()
    server = jellyfin_server()
    if not token or not server or not item_id:
        return ""

    item_id = str(item_id).strip()
    item = (
        preloaded_details
        if isinstance(preloaded_details, dict)
        else {}
    )

    if not item:
        if jellydream_is_gui_thread():
            jd_log(
                "Stream setup without preloaded details was blocked on the GUI thread: %s"
                % item_id,
                level="ERROR"
            )
            return ""

        item = jellyfin_get_item_details(
            item_id
        ) or {}

    video_codec = ""
    audio_codec = ""
    audio_title = ""
    audio_profile = ""
    audio_is_advanced_dts = False
    media_source_id = item_id
    orig_width = 1920
    orig_height = 1080

    try:
        sources = item.get("MediaSources") or []

        streams = []
        selected_source = jellydream_select_media_source(
            sources,
            preferred_media_source_id
        )

        if isinstance(selected_source, dict) and selected_source:
            extracted_source_id = selected_source.get("Id")

            if extracted_source_id:
                media_source_id = str(extracted_source_id)

                jd_log(
                    "MediaSource selected for stream setup: item=%s, MediaSource=%s, container=%s"
                    % (
                        item_id,
                        media_source_id,
                        selected_source.get("Container") or "-"
                    ),
                    level="INFO"
                )

            orig_width = (
                selected_source.get("Width")
                or orig_width
            )

            orig_height = (
                selected_source.get("Height")
                or orig_height
            )

            streams = selected_source.get(
                "MediaStreams"
            ) or []

        audio_streams = []
        selected_audio_stream = None
        audio_stream_ordinal = None

        for stream in streams:
            if not isinstance(stream, dict):
                continue

            stream_type = str(stream.get("Type") or "").strip().lower()

            if stream_type == "video" and not video_codec:
                video_codec = (
                    str(stream.get("Codec") or "").strip().lower()
                )

                try:
                    stream_width = int(
                        stream.get("Width") or 0
                    )

                    if stream_width > 0:
                        orig_width = stream_width
                except Exception:
                    pass

                try:
                    stream_height = int(
                        stream.get("Height") or 0
                    )

                    if stream_height > 0:
                        orig_height = stream_height
                except Exception:
                    pass

            elif stream_type == "audio":
                audio_streams.append(stream)

        for ordinal, stream in enumerate(audio_streams):
            try:
                jd_log(
                    "Audio URL candidate: ordinal=%s | MediaStream.Index=%s | language=%s | title=%s | codec=%s"
                    % (
                        ordinal,
                        stream.get("Index"),
                        stream.get("Language")
                        or stream.get("DisplayLanguage")
                        or "-",
                        stream.get("Title")
                        or stream.get("DisplayTitle")
                        or "-",
                        stream.get("Codec") or "-"
                    ),
                    level="DEBUG"
                )
            except Exception:
                pass

        if audio_stream_index is not None:
            try:
                wanted_media_index = int(audio_stream_index)

                for ordinal, stream in enumerate(audio_streams):
                    try:
                        stream_media_index = int(stream.get("Index"))
                    except Exception:
                        continue

                    if stream_media_index == wanted_media_index:
                        selected_audio_stream = stream
                        audio_stream_ordinal = ordinal
                        break

                if selected_audio_stream is not None:
                    jd_log(
                        "Audio track mapped for item %s | MediaStream.Index=%s | AudioStreamIndex=%s | language=%s"
                        % (
                            item_id,
                            wanted_media_index,
                            audio_stream_ordinal,
                            selected_audio_stream.get("Language")
                            or selected_audio_stream.get("DisplayLanguage")
                            or "-"
                        ),
                        level="DEBUG"
                    )
                else:
                    jd_log(
                        "MediaStream.Index %s was not found for item %s"                        % (wanted_media_index, item_id),
                        level="WARNING"
                    )

            except Exception as e:
                jd_log(
                    "Audio index could not be evaluated for item %s: %s"% (item_id, e),
                    level="WARNING"
                )

        # Eine über die Standardsprache ausgewählte TrueHD-Spur darf
        # bei deaktivierter TrueHD-Direktausgabe nicht die E-AC3-Spur
        # verdrängen. Falls verfügbar, wird deshalb E-AC3 bevorzugt.
        #
        # Wichtig:
        # `audio_stream_index` wird ebenfalls auf den echten
        # Jellyfin-MediaStream.Index der E-AC3-Spur geändert. Sonst
        # hätte die spätere URL-Erzeugung weiterhin die ursprünglich
        # gewählte TrueHD-Spur verwendet.
        if (
            selected_audio_stream is not None
            and not jellydream_allow_truehd_direct()
            and jellydream_allow_eac3_direct()
        ):
            selected_codec = str(
                selected_audio_stream.get("Codec") or ""
            ).strip().lower()

            if selected_codec in ("truehd", "mlp", "mlpfba"):
                for ordinal, stream in enumerate(audio_streams):
                    codec = str(
                        stream.get("Codec") or ""
                    ).strip().lower()

                    if codec not in ("eac3", "ec3"):
                        continue

                    eac3_media_stream_index = stream.get("Index")

                    try:
                        eac3_media_stream_index = int(
                            eac3_media_stream_index
                        )
                    except Exception:
                        continue

                    selected_audio_stream = stream
                    audio_stream_ordinal = ordinal
                    audio_stream_index = eac3_media_stream_index

                    jd_log(
                        "TrueHD selection replaced by E-AC3: ordinal=%s | MediaStream.Index=%s | language=%s | title=%s"
                        % (
                            ordinal,
                            eac3_media_stream_index,
                            stream.get("Language")
                            or stream.get("DisplayLanguage")
                            or "-",
                            stream.get("Title")
                            or stream.get("DisplayTitle")
                            or "-"
                        ),
                        level="INFO"
                    )

                    break

        if selected_audio_stream is None and audio_streams:
            # Standard: erste Tonspur wie von Jellyfin geliefert.
            selected_audio_stream = audio_streams[0]
            audio_stream_ordinal = 0

            # TrueHD ist auf vielen Enigma2-Boxen bzw. in ExtePlayer3 nicht
            # direkt abspielbar. Gibt es zusätzlich eine E-AC3-Spur, wird diese
            # bevorzugt. E-AC3 kann Dolby-Atmos-Metadaten als JOC enthalten und
            # ist über HDMI/TS deutlich realistischer als TrueHD.
            #
            # Die Logik greift nur, wenn:
            # - keine Spur ausdrücklich gewählt wurde,
            # - TrueHD-Direktausgabe deaktiviert ist,
            # - E-AC3-Direktausgabe erlaubt ist.
            if (
                not jellydream_allow_truehd_direct()
                and jellydream_allow_eac3_direct()
            ):
                for ordinal, stream in enumerate(audio_streams):
                    codec = str(
                        stream.get("Codec") or ""
                    ).strip().lower()

                    if codec in ("eac3", "ec3"):
                        selected_audio_stream = stream
                        audio_stream_ordinal = ordinal

                        jd_log(
                            "Preferring E-AC3 audio track over TrueHD: ordinal=%s | MediaStream.Index=%s | title=%s"
                            % (
                                ordinal,
                                stream.get("Index"),
                                stream.get("Title")
                                or stream.get("DisplayTitle")
                                or "-"
                            ),
                            level="INFO"
                        )

                        break

        if selected_audio_stream:
            audio_codec = (
                str(selected_audio_stream.get("Codec") or "")
                .strip()
                .lower()
            )

            audio_title = str(
                selected_audio_stream.get("Title")
                or selected_audio_stream.get("DisplayTitle")
                or ""
            ).strip().lower()

            audio_profile = str(
                selected_audio_stream.get("Profile")
                or ""
            ).strip().lower()

            dts_description = "%s %s" % (
                audio_title,
                audio_profile
            )

            audio_is_advanced_dts = (
                audio_codec == "dts"
                and any(
                    marker in dts_description
                    for marker in (
                        "dts-hd",
                        "dts hd",
                        "dts:x",
                        "dts x",
                        "master audio",
                        "ma +",
                        "hra"
                    )
                )
            )

            if audio_is_advanced_dts:
                jd_log(
                    "DTS-HD/DTS:X detected -> direct audio output is blocked for compatibility: %s"                   % (
                        audio_title
                        or audio_profile
                        or "DTS-Profil"
                    ),
                    level="INFO"
                )
        
        jd_log(
            "Selected audio track for item %s | Jellyfin.MediaStream.Index=%s | AudioStreamIndex=%s | AudioCodec=%s | available audio tracks=%s"            % (
                item_id,
                str(audio_stream_index),
                str(audio_stream_ordinal),
                audio_codec or "-",
                str(len(audio_streams))
            ),
            level="DEBUG"
        )

    except Exception as e:
        jd_log(
            "Exception while evaluating media sources for item %s: %s"            % (item_id, e),
            level="WARNING"
        )
        
    quoted_item_id = urllib.parse.quote(str(item_id), safe="")
    quoted_source_id = urllib.parse.quote(str(media_source_id), safe="")
    quoted_token = urllib.parse.quote(str(token), safe="")
    quoted_device_id = urllib.parse.quote(
        str(jellydream_device_id()),
        safe=""
    )
    play_session_id = str(
        play_session_id or uuid.uuid4()
    ).strip()

    quoted_play_session_id = urllib.parse.quote(
        play_session_id,
        safe=""
    )

    subtitle_stream_param = ""
    normalized_subtitle_stream_index = None
    subtitle_requested = False

    try:
        if subtitle_stream_index is not None:
            normalized_subtitle_stream_index = int(
                subtitle_stream_index
            )

            if normalized_subtitle_stream_index >= 0:
                subtitle_requested = True

                # `SubtitleMethod=Encode` brennt den Untertitel serverseitig
                # ins Bild ein. Das ist für Enigma2, TS und HLS am stabilsten.
                subtitle_stream_param = (
                    "&SubtitleStreamIndex=%d"
                    "&SubtitleMethod=Encode"
                    % normalized_subtitle_stream_index
                )

                jd_log(
                    "Subtitles requested for stream: SubtitleStreamIndex=%s, method=Encode"
                    % normalized_subtitle_stream_index,
                    level="INFO"
                )

    except Exception as e:
        normalized_subtitle_stream_index = None
        subtitle_requested = False
        subtitle_stream_param = ""

        jd_log(
            "Subtitle index could not be processed: %s"
            % e,
            level="WARNING"
        )
    
    chosen_res = jellydream_max_resolution()
    limit_w, limit_h = 1920, 1080

    if chosen_res == "4K":
        limit_w, limit_h = 3840, 2160
    elif chosen_res == "720p":
        limit_w, limit_h = 1280, 720
    elif chosen_res == "SD":
        limit_w, limit_h = 720, 576

    try:
        max_w = min(limit_w, int(orig_width))
    except Exception:
        max_w = limit_w

    try:
        max_h = min(limit_h, int(orig_height))
    except Exception:
        max_h = limit_h

    try:
        source_width = int(orig_width or 0)
    except Exception:
        source_width = 0

    try:
        source_height = int(orig_height or 0)
    except Exception:
        source_height = 0

    # Viele Enigma2-Receiver unterstützen UHD nur mit HEVC/H.265.
    # 4K-H.264 führt bei diesen Geräten häufig direkt zu evEOF,
    # während die Wiedergabeposition bei 0 Sekunden bleibt.
    force_h264_uhd_transcode = (
        video_codec in ("h264", "avc")
        and (
            source_width > 1920
            or source_height > 1080
        )
    )

    if force_h264_uhd_transcode:
        max_w = min(max_w, 1920)
        max_h = min(max_h, 1080)

        jd_log(
            "4K H.264 detected -> forcing compatible 1080p video transcoding: source=%sx%s, target=%sx%s"
            % (
                source_width,
                source_height,
                max_w,
                max_h
            ),
            level="INFO"
        )
        
    user_bitrate = jellydream_video_bitrate()
    stream_mode = str(jellydream_stream_mode() or "hybrid").strip().lower()

    stream_protocol = jellydream_stream_protocol()

    if stream_protocol == "hls":
        stream_endpoint = "master.m3u8"

        # Jellyfin 12 / neues FFmpeg:
        # Keine aggressiven Segment-Vorgaben erzwingen.
        #
        # `BreakOnNonKeyFrames=true` kann bei einzelnen Quellen dazu führen,
        # dass ExtePlayer3 sofort EOF meldet, bevor ein verwertbares erstes
        # Segment verfügbar ist.
        stream_protocol_params = (
            "&SegmentContainer=ts"
            "&MinSegments=1"
        )
    else:
        stream_endpoint = "stream.ts"
        stream_protocol_params = ""

    jd_log(
        "Stream output format: %s | endpoint: %s"
        % (
            stream_protocol.upper(),
            stream_endpoint
        ),
        level="DEBUG"
    )

    stable_video_codecs = ("h264", "avc", "mpeg2video")
    stable_audio_codecs = ["mp2", "mp3", "aac"]

    direct_video_codecs = ("h264", "avc", "mpeg2video", "hevc", "h265", "mpeg4")
    direct_audio_codecs = ["mp2", "mp3", "aac"]

    if jellydream_allow_ac3_direct():
        direct_audio_codecs.append("ac3")
    
    if jellydream_allow_eac3_direct():
        # Dolby Digital Plus, inklusive E-AC3 JOC / Dolby Atmos.
        direct_audio_codecs.extend((
            "eac3",
            "ec3"
        ))

    if jellydream_allow_truehd_direct():
        # Nur aktivieren, wenn die Box, der verwendete Player und der AVR
        # TrueHD per HDMI tatsächlich verarbeiten können.
        direct_audio_codecs.extend((
            "truehd",
            "mlp",
            "mlpfba"
        ))

    if jellydream_allow_dts_direct():
        direct_audio_codecs.append("dts")

    stable_audio_codecs = tuple(stable_audio_codecs)
    direct_audio_codecs = tuple(direct_audio_codecs)

    if stream_mode == "stable":
        # Stabilitätsmodus: bewusst nur sichere Videoformate.
        allowed_direct_video = stable_video_codecs
        allowed_direct_audio = stable_audio_codecs

    elif stream_mode == "hybrid":
        # Automatisch:
        # HEVC darf direkt laufen, wenn die eingestellte Maximalauflösung
        # dies erlaubt. Audio bleibt nach den kompatibleren Regeln behandelt.
        allowed_direct_video = direct_video_codecs
        allowed_direct_audio = direct_audio_codecs

    else:
        # Direktstream bevorzugen.
        allowed_direct_video = direct_video_codecs
        allowed_direct_audio = direct_audio_codecs

    if (not video_codec and not audio_codec and not subtitle_requested):
        jd_log(
            "No codec info found for item %s -> using direct-stream fallback"
            % item_id,
            level="DEBUG"
        )

        url = (
            "%s/Videos/%s/stream.mkv?"
            "Static=true"
            "&MediaSourceId=%s"
            "&DeviceId=%s"
            "&PlaySessionId=%s"
            "&api_key=%s"
            % (
                server,
                quoted_item_id,
                quoted_source_id,
                quoted_device_id,
                quoted_play_session_id,
                quoted_token
            )
        )

        if audio_stream_index is not None:
            try:
                fallback_audio_index = int(audio_stream_index)

                if fallback_audio_index >= 0:
                    url += "&AudioStreamIndex=%d" % fallback_audio_index

                    jd_log(
                        "Fallback audio selection for item %s: AudioStreamIndex=%s (MediaStream.Index=%s, AudioOrdinal=%s)"
                        % (
                            item_id,
                            fallback_audio_index,
                            audio_stream_index,
                            audio_stream_ordinal
                        ),
                        level="DEBUG"
                    )

            except Exception as e:
                jd_log(
                    "Fallback audio index could not be set for item %s: %s"                    % (item_id, e),
                    level="WARNING"
                )

        return url

    direct_video = video_codec in allowed_direct_video
    direct_audio = audio_codec in allowed_direct_audio

    # Eine Überschreitung der eingestellten Maximalauflösung benötigt
    # zwingend Video-Transcoding. VideoCodec=copy kann keine Auflösung
    # verkleinern.
    resolution_exceeds_limit = (
        (
            source_width > 0
            and source_width > limit_w
        )
        or (
            source_height > 0
            and source_height > limit_h
        )
    )

    if resolution_exceeds_limit:
        direct_video = False

        jd_log(
            "Source resolution exceeds the configured maximum -> video transcoding required: source=%sx%s, limit=%sx%s"
            % (
                source_width,
                source_height,
                limit_w,
                limit_h
            ),
            level="INFO"
        )

    if force_h264_uhd_transcode:
        direct_video = False

    # Eingebrannte Untertitel benötigen zwingend Video-Transcoding.
    if subtitle_requested:
        direct_video = False

        jd_log(
            "Subtitles are active -> forcing video transcoding so they can be burned in",
            level="INFO"
        )        
        
    # `Codec=dts` kann bei Jellyfin ebenfalls DTS-HD MA oder DTS:X bedeuten.
    # Diese Formate nicht ungeprüft als normales Core-DTS direkt ausgeben.
    if audio_is_advanced_dts:
        direct_audio = False

    jd_log(
        "Audio-Direktregeln | allow_ac3_direct=%s | allow_eac3_direct=%s | allow_truehd_direct=%s | allow_dts_direct=%s | preferred_audio_codec=%s" % (
            str(jellydream_allow_ac3_direct()),
            str(jellydream_allow_eac3_direct()),
            str(jellydream_allow_truehd_direct()),            
            str(jellydream_allow_dts_direct()),
            str(jellydream_preferred_audio_codec())
        ),
        level="DEBUG"
    )

    audio_needs_transcode = (
        bool(audio_codec)
        and not direct_audio
    )

    video_needs_transcode = (
        not direct_video
        or subtitle_requested
    )

    jd_log(
        "Stream decision for item %s | mode=%s | video=%s | audio=%s | audio title=%s | audio profile=%s | AdvancedDTS=%s | direct_video=%s | direct_audio=%s | JellyfinMediaStreamIndex=%s"
        % (
            item_id,
            stream_mode,
            video_codec or "-",
            audio_codec or "-",
            audio_title or "-",
            audio_profile or "-",
            str(audio_is_advanced_dts),
            str(direct_video),
            str(direct_audio),
            str(audio_stream_index)
        ),
        level="DEBUG"
    )

    audio_stream_param = ""
    start_time_param = ""

    try:
        start_position_seconds = max(
            0,
            int(start_position_seconds or 0)
        )

        if start_position_seconds > 0:
            jd_log(
                "Resume position %ss is not written into the stream URL; Enigma2 seeks after the player starts"
                % start_position_seconds,
                level="DEBUG"
            )

    except Exception as e:
        jd_log(
            "Invalid start position '%s' for item %s: %s"           % (
                start_position_seconds,
                item_id,
                e
            ),
            level="WARNING"
        )

        start_position_seconds = 0

    # Die Stream-URL benötigt einen echten Jellyfin-MediaStream.Index,
    # nicht den lokalen Audio-Ordinal.
    #
    # Beispiel dieser Datei:
    #   Audio-Ordinal 0 -> MediaStream.Index 1 -> TrueHD
    #   Audio-Ordinal 1 -> MediaStream.Index 2 -> AC3
    #   Audio-Ordinal 2 -> MediaStream.Index 3 -> E-AC3 Atmos
    audio_stream_param = ""
    url_audio_stream_index = None

    try:
        # Eine ausdrücklich vom Nutzer bzw. über die Standardsprache
        # ausgewählte Spur hat immer Vorrang.
        if audio_stream_index is not None:
            url_audio_stream_index = int(audio_stream_index)

        # Wurde automatisch eine andere als die erste Spur gewählt
        # (z. B. E-AC3 Atmos statt TrueHD), muss diese Auswahl ebenfalls
        # in die URL. Andernfalls liefert Static=true wieder die
        # Original-Standardspur der Datei.
        elif (
            selected_audio_stream is not None
            and audio_stream_ordinal is not None
            and int(audio_stream_ordinal) > 0
        ):
            selected_media_stream_index = (
                selected_audio_stream.get("Index")
            )

            if selected_media_stream_index is not None:
                url_audio_stream_index = int(
                    selected_media_stream_index
                )

        if (
            url_audio_stream_index is not None
            and url_audio_stream_index >= 0
        ):
            audio_stream_param = (
                "&AudioStreamIndex=%d"
                % url_audio_stream_index
            )

            jd_log(
                "URL audio selection for item %s: AudioStreamIndex=%s | AudioOrdinal=%s | Codec=%s"
                % (
                    item_id,
                    url_audio_stream_index,
                    audio_stream_ordinal,
                    audio_codec or "-"
                ),
                level="INFO"
            )

    except Exception as e:
        jd_log(
            "Audio index for the stream URL could not be set for item %s: %s"            % (
                item_id,
                e
            ),
            level="WARNING"
        )

        audio_stream_param = ""
        url_audio_stream_index = None

    if not video_needs_transcode and not audio_needs_transcode:
        must_remux_for_audio = (
            bool(audio_stream_param)
            and len(audio_streams) > 1
            and audio_stream_ordinal is not None
            and int(audio_stream_ordinal) > 0
        )

        if must_remux_for_audio:
            # Auch bei einer gezielt gewählten späteren Audiospur HLS beibehalten.
            #
            # Jellyfin erstellt dann eine HLS-Playlist mit genau der angeforderten
            # AudioStreamIndex-Spur. Ein erzwungenes stream.ts würde die globale
            # HLS-Einstellung unbemerkt außer Kraft setzen.
            audio_remux_endpoint = stream_endpoint
            audio_remux_protocol_params = stream_protocol_params

            jd_log(
                "Audio direct stream with selected audio track | JellyfinMediaStreamIndex=%s | AudioOrdinal=%s | audio tracks=%s | Video=copy | Audio=copy | Endpoint=%s"
                % (
                    url_audio_stream_index,
                    audio_stream_ordinal,
                    len(audio_streams),
                    audio_remux_endpoint
                ),
                level="INFO"
            )

            return (
                "%s/Videos/%s/%s"
                "?api_key=%s"
                "&MediaSourceId=%s"
                "&DeviceId=%s"
                "&PlaySessionId=%s"
                "&VideoCodec=copy"
                "&AudioCodec=copy"
                "&VideoBitrate=%s"
                "&AudioBitrate=320000"
                "&MaxWidth=%s"
                "&MaxHeight=%s"
                "%s%s%s%s"
            ) % (
                server,
                quoted_item_id,
                audio_remux_endpoint,
                quoted_token,
                quoted_source_id,
                quoted_device_id,
                quoted_play_session_id,
                int(user_bitrate),
                int(max_w),
                int(max_h),
                audio_remux_protocol_params,
                subtitle_stream_param,
                audio_stream_param,
                start_time_param
            )
            
        jd_log(
            "Direct play with Static=true | AudioOrdinal=%s | audio tracks=%s"
            % (
                audio_stream_ordinal,
                len(audio_streams)
            ),
            level="DEBUG"
        )

        return (
            "%s/Videos/%s/stream.mkv"
            "?Static=true"
            "&MediaSourceId=%s"
            "&DeviceId=%s"
            "&PlaySessionId=%s"
            "&api_key=%s"
            "%s%s%s"
        ) % (
            server,
            quoted_item_id,
            quoted_source_id,
            quoted_device_id,
            quoted_play_session_id,
            quoted_token,
            audio_stream_param,
            subtitle_stream_param,
            start_time_param
        )

    target_audio_codec = jellydream_preferred_audio_codec()

    if not video_needs_transcode and audio_needs_transcode:
        return (
            "%s/Videos/%s/%s"
            "?api_key=%s"
            "&MediaSourceId=%s"
            "&DeviceId=%s"
            "&PlaySessionId=%s"
            "&VideoCodec=copy"
            "&AudioCodec=%s"
            "&VideoBitrate=%s"
            "&AudioBitrate=320000"
            "&MaxWidth=%s"
            "&MaxHeight=%s"
            "%s%s%s%s"
        ) % (
            server,
            quoted_item_id,
            stream_endpoint,
            quoted_token,
            quoted_source_id,
            quoted_device_id,
            quoted_play_session_id,
            target_audio_codec,
            int(user_bitrate),
            int(max_w),
            int(max_h),
            stream_protocol_params,
            audio_stream_param,
            subtitle_stream_param,
            start_time_param
        )

    target_video_codec = "h264"

    # Wenn die gewählte Tonspur direkt unterstützt wird, soll Jellyfin sie
    # auch bei einem notwendigen Video-Transcoding unverändert durchreichen.
    #
    # Das ist wichtig für:
    # - E-AC3 JOC / Dolby Digital Plus + Atmos
    # - Dolby TrueHD + Atmos
    #
    # Im Stabilitätsmodus bleibt die bestehende Sicherheitslogik aktiv:
    # Dort wird nicht freigegebener bzw. problematischer Ton transcodiert.
    if not audio_needs_transcode:
        target_audio_for_video_transcode = "copy"
    else:
        target_audio_for_video_transcode = target_audio_codec

    return (
        "%s/Videos/%s/%s"
        "?api_key=%s"
        "&MediaSourceId=%s"
        "&DeviceId=%s"
        "&PlaySessionId=%s"
        "&VideoCodec=%s"
        "&AudioCodec=%s"
        "&VideoBitrate=%s"
        "&AudioBitrate=320000"
        "&MaxWidth=%s"
        "&MaxHeight=%s"
        "%s%s%s%s"
    ) % (
        server,
        quoted_item_id,
        stream_endpoint,
        quoted_token,
        quoted_source_id,
        quoted_device_id,
        quoted_play_session_id,
        target_video_codec,
        target_audio_for_video_transcode,
        int(user_bitrate),
        int(max_w),
        int(max_h),
        stream_protocol_params,
        audio_stream_param,
        subtitle_stream_param,
        start_time_param
    )


def jellydream_find_next_episode(playback_result):
    """
    Ermittelt die nächste Episode synchron.

    Diese Funktion darf nur in einem Hintergrund-Worker aufgerufen werden,
    weil jellyfin_get_episodes() bei einem Cache-Miss eine Netzwerkanfrage
    ausführen kann.
    """
    if jellydream_is_gui_thread():
        jd_log(
            "Synchronous next-episode search was blocked on the GUI thread",
            level="ERROR"
        )
        return None

    try:
        if not isinstance(playback_result, dict):
            return None

        if not playback_result.get("playback_finished", False):
            return None

        item_type = str(
            playback_result.get("item_type") or ""
        ).strip()

        if item_type != "Episode":
            return None

        current_item_id = str(
            playback_result.get("item_id") or ""
        ).strip()

        series_id = str(
            playback_result.get("series_id") or ""
        ).strip()

        season_number = playback_result.get("season_number")
        episode_number = playback_result.get("episode_number")

        if not current_item_id or not series_id:
            return None

        try:
            season_number = int(season_number)
        except Exception:
            return None

        try:
            episode_number = int(episode_number)
        except Exception:
            episode_number = None

        episodes = jellyfin_get_episodes(
            series_id,
            season_number
        ) or []

        if not episodes:
            return None

        season_episodes = []

        for episode in episodes:
            if not isinstance(episode, dict):
                continue

            episode_season = episode.get("ParentIndexNumber")

            try:
                if episode_season is not None:
                    if int(episode_season) != season_number:
                        continue
            except Exception:
                continue

            season_episodes.append(episode)

        if not season_episodes:
            return None

        def episode_sort_key(episode):
            try:
                return int(episode.get("IndexNumber") or 0)
            except Exception:
                return 0

        season_episodes.sort(key=episode_sort_key)

        for position, episode in enumerate(season_episodes):
            episode_id = str(
                episode.get("Id") or ""
            ).strip()

            if episode_id != current_item_id:
                continue

            next_position = position + 1

            if next_position >= len(season_episodes):
                return None

            return season_episodes[next_position]

        if episode_number is not None:
            for episode in season_episodes:
                try:
                    candidate_number = int(
                        episode.get("IndexNumber") or 0
                    )
                except Exception:
                    continue

                if candidate_number > episode_number:
                    return episode

        return None

    except Exception as e:
        jd_log(
            "Next series episode could not be resolved: %s"% e,
            level="WARNING"
        )
        return None


def jellydream_media_segments(item_id, auth_snapshot=None):
    """
    Liest Intro- und Outro-Abschnitte von /MediaSegments/{itemId}.

    Andere Segmentarten bleiben unberührt. Ein fehlender Endpunkt oder
    eine leere Liste ergibt einfach keine Sprünge.
    """
    item_id = str(item_id or "").strip()

    if not item_id:
        return []

    url = "%s/MediaSegments/%s" % (
        str(jellyfin_server() or "").rstrip("/"),
        urllib.parse.quote(item_id)
    )

    result = jellyfin_api_get_ex(
        url,
        timeout=TIMEOUT_MIDDLE,
        auth_snapshot=auth_snapshot
    )

    if not result.get("ok"):
        return []

    data = result.get("data") or {}
    items = data.get("Items") if isinstance(data, dict) else data

    if not isinstance(items, list):
        return []

    segments = []

    for item in items:
        if not isinstance(item, dict):
            continue

        kind = str(item.get("Type") or "").strip()

        if kind not in ("Intro", "Outro"):
            continue

        try:
            start = int(item.get("StartTicks") or 0) // TICKS_PER_SECOND
            end = int(item.get("EndTicks") or 0) // TICKS_PER_SECOND
        except Exception:
            continue

        if end <= start + 2:
            continue

        segments.append({
            "id": str(
                item.get("Id")
                or "%s-%s-%s" % (kind, start, end)
            ),
            "type": kind,
            "start": start,
            "end": end
        })

    segments.sort(key=lambda segment: segment["start"])
    return segments


def jellydream_load_media_segments_async(item_id, finished_callback=None):
    if not callable(finished_callback):
        return False

    def deliver_result(segments):
        return jellydream_dispatch_to_gui(
            lambda cb=finished_callback, loaded=segments: cb(loaded)
        )

    def worker():
        segments = []

        try:
            segments = jellydream_media_segments(item_id)
        except Exception as e:
            jd_log(
                "Media segments could not be loaded: %s" % e,
                level="WARNING"
            )

        deliver_result(segments)

    started = jellydream_submit_playback(worker)

    if not started:
        deliver_result([])
        return False

    return True


def jellydream_find_next_episode_async(
    playback_result,
    finished_callback=None
):
    """
    Ermittelt die nächste Episode ausschließlich im Worker-Pool.

    finished_callback(next_episode, error_message)

    Bei einem vollen Worker-Pool wird der Callback ebenfalls aufgerufen,
    damit der Aufrufer nicht auf ein Ergebnis wartet, das nie kommen wird.
    """
    if not callable(finished_callback):
        return False

    def deliver_result(next_episode, error_message=""):
        dispatched = jellydream_dispatch_to_gui(
            lambda cb=finished_callback,
            episode=next_episode,
            message=str(error_message or ""): cb(
                episode,
                message
            )
        )

        if not dispatched:
            jd_log(
                "Next-episode search result could not be dispatched to the GUI thread",
                level="ERROR"
            )

        return dispatched

    def worker():
        next_episode = None
        error_message = ""

        try:
            next_episode = jellydream_find_next_episode(
                playback_result
            )

        except Exception as e:
            error_message = _(
                "Die nächste Episode konnte nicht ermittelt werden."
            )

            jd_log(
                "Async next-episode search failed: %s" % e,
                level="WARNING"
            )

        deliver_result(
            next_episode,
            error_message
        )

    started = jellydream_submit_playback(worker)

    if not started:
        error_message = _(
            "Die nächste Episode konnte nicht gesucht werden, "
            "weil der Hintergrund-Worker-Pool ausgelastet ist."
        )

        jd_log(
            "Next episode could not be searched because the "
            "background worker pool is busy",
            level="WARNING"
        )

        deliver_result(
            None,
            error_message
        )

        return False

    return True



def jellydream_offer_next_episode(session, original_callback, return_screen, playback_result, next_episode, auto_start=False):
    """
    Zeigt den Dialog für die bereits im Hintergrund ermittelte Episode an.

    Diese Funktion führt selbst keine Netzwerkanfrage aus und wird nur im
    GUI-Thread aufgerufen.
    """
    try:
        if not isinstance(next_episode, dict):
            return

        playback_result = (
            playback_result
            if isinstance(playback_result, dict)
            else {}
        )

        next_item_id = str(
            next_episode.get("Id") or ""
        ).strip()

        if not next_item_id:
            return

        season_number = jellydream_int_or_default(
            next_episode.get("ParentIndexNumber"),
            1
        )

        episode_number = jellydream_int_or_default(
            next_episode.get("IndexNumber"),
            1
        )

        episode_name = str(
            next_episode.get("Name") or "Folge"
        ).strip()

        series_name = str(
            next_episode.get("SeriesName")
            or playback_result.get("series_name")
            or ""
        ).strip()

        next_title = (
            "S%02dE%02d - %s"
            % (
                season_number,
                episode_number,
                episode_name
            )
        )

        next_runtime = ticks_to_seconds(
            next_episode.get("RunTimeTicks")
        )

        if series_name:
            episode_display = "%s\n%s" % (
                series_name,
                next_title
            )
        else:
            episode_display = next_title

        question_text = (
            _("Möchtest du die nächste Folge abspielen?\n\n%s\n\n")
            % episode_display
        )

        def next_episode_answer(answer):
            try:
                if answer is not True:
                    jd_log(
                        "Next episode was not started",
                        level="INFO"
                    )
                    return

                jd_log(
                    "Starting next episode: %s"% next_title,
                    level="INFO"
                )

                def next_play_finished(ok, error):
                    if ok:
                        return

                    error = str(
                        error
                        or _("Nächste Folge konnte nicht gestartet werden.")
                    )

                    jd_log(
                        "Next episode could not be started: %s"                        % error,
                        level="ERROR"
                    )

                    try:
                        session.open(
                            MessageBox,
                            error,
                            MessageBox.TYPE_ERROR,
                            timeout=5
                        )
                    except Exception:
                        pass

                jellydream_safe_play_async(
                    session=session,
                    title=next_title,
                    item_id=next_item_id,
                    runtime_seconds=next_runtime,
                    return_screen=return_screen,
                    callback=original_callback,
                    finished_callback=next_play_finished
                )

            except Exception as e:
                jd_log(
                    "Error while starting the next episode: %s"% e,
                    level="ERROR"
                )

        # Die Frage wurde bereits zehn Sekunden vor dem Ende mit Ja
        # beantwortet. Deshalb die vorbereitete Episode jetzt direkt starten,
        # ohne nach dem EOF eine zweite Frage anzuzeigen.
        if auto_start:
            next_episode_answer(True)
            return

        session.openWithCallback(
            next_episode_answer,
            MessageBox,
            question_text,
            MessageBox.TYPE_YESNO,
            timeout=15,
            default=False
        )

    except Exception as e:
        jd_log(
            "Next-episode dialog could not be shown: %s" % e,
            level="WARNING"
        )

        
def jellydream_run_player_callback(callback, result=None):
    try:
        if callable(callback):
            callback(result)
    except Exception as e:
        jd_log(
            "Player callback could not be run: %s"% e,
            level="WARNING"
        )


def jellydream_handle_player_closed(
    session,
    original_callback,
    return_screen,
    playback_result=None
):
    """
    Verarbeitet das Schließen des Players.

    Der ursprüngliche Screen-Callback läuft im GUI-Thread. Die Suche nach
    der nächsten Episode wird dagegen vollständig in den Worker-Pool
    ausgelagert.

    Eine Musikliste wird nur fortgesetzt, wenn die Wiedergabe normal
    zu Ende war und noch Titel in der Warteschlange liegen.
    """
    try:
        continue_music = (
            isinstance(playback_result, dict)
            and bool(playback_result.get("playback_finished", False))
            and jellydream_music_queue_has_items()
        )

        if not continue_music:
            jellydream_music_queue_clear()

        jellydream_run_player_callback(
            original_callback,
            playback_result
        )

        if continue_music:
            jellydream_play_next_music_track(
                session,
                original_callback,
                return_screen
            )
            return

        if not isinstance(playback_result, dict):
            return

        if not playback_result.get(
            "playback_finished",
            False
        ):
            return

        if str(
            playback_result.get("item_type") or ""
        ).strip() != "Episode":
            return

        prompt_was_shown = bool(
            playback_result.get(
                "next_episode_prompt_shown",
                False
            )
        )

        preloaded_next_episode = playback_result.get(
            "next_episode"
        )

        next_episode_decision = playback_result.get(
            "next_episode_decision",
            None
        )

        # Die Frage wurde bereits innerhalb der letzten zehn Sekunden
        # angezeigt. Deshalb nach dem EOF niemals ein zweites Mal fragen.
        if prompt_was_shown:
            if (
                next_episode_decision is True
                and isinstance(preloaded_next_episode, dict)
            ):
                jellydream_offer_next_episode(
                    session=session,
                    original_callback=original_callback,
                    return_screen=return_screen,
                    playback_result=playback_result,
                    next_episode=preloaded_next_episode,
                    auto_start=True
                )

            else:
                jd_log(
                    "No next episode started after EOF: the prefetch was declined or not confirmed",
                    level="INFO"
                )

            return

        def next_episode_resolved(
            next_episode,
            error_message=""
        ):
            try:
                if error_message:
                    jd_log(
                        "Next-episode lookup failed",
                        level="WARNING"
                    )
                    return

                if not next_episode:
                    jd_log(
                        "No further episode found in this season",
                        level="DEBUG"
                    )
                    return

                jellydream_offer_next_episode(
                    session=session,
                    original_callback=original_callback,
                    return_screen=return_screen,
                    playback_result=playback_result,
                    next_episode=next_episode
                )

            except Exception as e:
                jd_log(
                    "Next-episode search result could not be processed: %s" % e,
                    level="WARNING"
                )

        jellydream_find_next_episode_async(
            playback_result,
            finished_callback=next_episode_resolved
        )

    except Exception as e:
        jd_log(
            "Error in jellydream_handle_player_closed: %s" % e,
            level="WARNING"
        )


def jellydream_play_next_music_track(session, original_callback, return_screen):
    track = jellydream_music_queue_pop()

    if not isinstance(track, dict):
        jellydream_music_queue_clear()
        return

    item_id = str(track.get("Id") or "").strip()

    if not item_id:
        jellydream_music_queue_clear()
        return

    title = str(track.get("Name") or "").strip() or "Music"
    runtime_seconds = ticks_to_seconds(track.get("RunTimeTicks"))

    def next_track_finished(ok, error_message=""):
        if ok:
            return

        jellydream_music_queue_clear()
        jd_log(
            "Next music track could not be started: %s" % (
                error_message or item_id
            ),
            level="WARNING"
        )

    started = jellydream_safe_play_async(
        session=session,
        title=title,
        item_id=item_id,
        runtime_seconds=runtime_seconds,
        return_screen=return_screen,
        callback=original_callback,
        start_position_seconds=0,
        finished_callback=next_track_finished
    )

    if not started:
        jellydream_music_queue_clear()
        jd_log(
            "Next music track was not accepted: %s" % item_id,
            level="WARNING"
        )


def jellydream_safe_play_async(session, title, item_id, runtime_seconds=0, return_screen=None, callback=None, start_position_seconds=0, audio_stream_index=None, finished_callback=None):
    """
    Lädt die Streamdetails im Worker-Pool und startet den Player anschließend
    im GUI-Thread.

    Rückgabewert:
        True:
            Der Hintergrundjob wurde erfolgreich angenommen.

        False:
            Ungültige Item-ID oder Worker-Pool ist ausgelastet.

    finished_callback(ok, error_message) wird auch dann aufgerufen, wenn
    der Worker-Pool die Aufgabe nicht annehmen kann.
    """
    item_id = str(item_id or "").strip()

    def notify_finished(ok, error_message=""):
        if not callable(finished_callback):
            return True

        dispatched = jellydream_dispatch_to_gui(
            lambda cb=finished_callback,
            success=bool(ok),
            message=str(error_message or ""): cb(
                success,
                message
            )
        )

        if not dispatched:
            jd_log(
                "Playback result could not be dispatched to the GUI thread",
                level="ERROR"
            )

        return dispatched

    if not item_id:
        notify_finished(
            False,
            "Fehler: Leere Item-ID"
        )
        return False

    # Wiedergabe darf nur mit derselben Anmeldung vorbereitet und gestartet
    # werden, mit der der Nutzer OK gedrückt hat.
    auth_snapshot = jellydream_auth_snapshot()

    def worker():
        try:
            details = jellyfin_get_item_details(
                item_id,
                raise_on_error=True,
                auth_snapshot=auth_snapshot
            )

            playback_context = jellyfin_get_playback_info(
                item_id,
                start_position_seconds=start_position_seconds,
                raise_on_error=True,
                auth_snapshot=auth_snapshot
            )

            selected_source = playback_context.get(
                "media_source"
            ) or {}

            selected_source_id = str(
                playback_context.get("media_source_id") or ""
            ).strip()

            server_play_session_id = str(
                playback_context.get("play_session_id") or ""
            ).strip()

            # Die vollständigen Item-Details bleiben erhalten. Ausschließlich die
            # durch PlaybackInfo verbindlich gewählte Medienquelle wird ersetzt.
            prepared_details = dict(details or {})

            if selected_source:
                prepared_details["MediaSources"] = [
                    selected_source
                ]

            if selected_source_id:
                prepared_details["_jellydream_media_source_id"] = (
                    selected_source_id
                )

            if server_play_session_id:
                prepared_details["_jellydream_play_session_id"] = (
                    server_play_session_id
                )

            # Die bevorzugte Audiosprache bereits im Worker auflösen.
            # Dadurch wird exakt dieselbe Stream-URL getestet, die später
            # an Enigma2 übergeben wird.
            effective_audio_stream_index = audio_stream_index

            if effective_audio_stream_index is None:
                preferred_language = jellydream_default_audio_language()

                if preferred_language:
                    effective_audio_stream_index = (
                        jellyfin_find_audio_stream_index_by_language(
                            item_id,
                            preferred_language,
                            preloaded_details=prepared_details
                        )
                    )

            probe_stream_url = build_jellyfin_stream(
                item_id,
                audio_stream_index=effective_audio_stream_index,
                start_position_seconds=start_position_seconds,
                preloaded_details=prepared_details,
                play_session_id=server_play_session_id,
                preferred_media_source_id=selected_source_id
            )

            if not probe_stream_url:
                raise JellyfinRequestError(
                    _(
                        "Die Jellyfin-Stream-URL konnte nicht erzeugt werden."
                    )
                )

            # Kein separater Stream-Abruf vor dem Playerstart:
            #
            # Für maximale Kompatibilität bleibt `api_key` in der
            # Stream-URL enthalten. Beim tatsächlichen Playerstart wird
            # der Token zusätzlich über `Authorization`, `X-Emby-Token`
            # und `X-MediaBrowser-Token` übergeben.
            #
            # Damit funktionieren ältere Jellyfin-Versionen weiterhin
            # über den Query-Parameter, während neuere Versionen den
            # Token auch als HTTP-Header erhalten.
            jd_log(
                "Stream URL prepared; query token and player headers are used together.",
                level="DEBUG"
            )

            prepared_details[
                "_jellydream_prebuilt_stream_url"
            ] = probe_stream_url

            prepared_details[
                "_jellydream_effective_audio_stream_index"
            ] = effective_audio_stream_index

            def apply_result():
                try:
                    if not jellydream_auth_snapshot_matches_current(
                        auth_snapshot
                    ):
                        notify_finished(
                            False,
                            "Die Anmeldung wurde während der "
                            "Wiedergabevorbereitung geändert."
                        )

                        jd_log(
                            "Prepared playback discarded after a session change: %s"
                            % item_id,
                            level="INFO"
                        )
                        return

                    ok, error = jellydream_safe_play(
                        session=session,
                        title=title,
                        item_id=item_id,
                        runtime_seconds=runtime_seconds,
                        return_screen=return_screen,
                        callback=callback,
                        start_position_seconds=start_position_seconds,
                        audio_stream_index=effective_audio_stream_index,
                        preloaded_stream_data=prepared_details,
                        play_session_id=server_play_session_id,
                        prebuilt_stream_url=probe_stream_url
                    )

                    if callable(finished_callback):
                        finished_callback(
                            ok,
                            error
                        )

                except Exception as e:
                    jd_log(
                        "Prepared stream could not be started on the GUI thread: %s" % e,
                        level="ERROR"
                    )

                    if callable(finished_callback):
                        finished_callback(
                            False,
                            _("Wiedergabe konnte nicht gestartet werden.")
                        )

            if not jellydream_dispatch_to_gui(
                apply_result
            ):
                jd_log(
                    "Prepared stream could not be dispatched to the GUI thread",
                    level="ERROR"
                )

        except JellyfinRequestError as e:
            error_message = str(e)

            notify_finished(
                False,
                error_message
            )

        except Exception as e:
            jd_log(
                "Stream data could not be loaded for item '%s': %s"                % (
                    item_id,
                    e
                ),
                level="WARNING"
            )

            notify_finished(
                False,
                "Streamdaten konnten nicht geladen werden."
            )

    started = jellydream_submit_playback(
        worker
    )

    if not started:
        error_message = (
            "Wiedergabe konnte nicht vorbereitet werden, "
            "weil der Hintergrund-Worker-Pool ausgelastet ist."
        )

        jd_log(
            "Playback could not be prepared because the "
            "background worker pool is busy",
            level="WARNING"
        )

        notify_finished(
            False,
            error_message
        )

        return False

    return True

    
def jellydream_safe_play(
    session,
    title,
    item_id,
    runtime_seconds=0,
    return_screen=None,
    callback=None,
    start_position_seconds=0,
    audio_stream_index=None,
    preloaded_stream_data=None,
    play_session_id="",
    prebuilt_stream_url=""
):
    try:
        title = str(title or "Jellyfin Video")
        item_id = str(item_id or "").strip()

        preloaded_stream_data = (
            dict(preloaded_stream_data)
            if isinstance(preloaded_stream_data, dict)
            else {}
        )

        if not item_id:
            return False, _("Fehler: Item-ID fehlt.")

        # Die Listen-/Suchdaten enthalten nicht immer eine vollständige
        # Laufzeit. Die beim Streamstart geladenen Item-Details haben Vorrang.
        details_runtime_seconds = ticks_to_seconds(
            preloaded_stream_data.get("RunTimeTicks")
        )

        if details_runtime_seconds > 0:
            if int(runtime_seconds or 0) != details_runtime_seconds:
                jd_log(
                    "Runtime taken from full item details: %ss -> %ss for item %s"                    % (
                        int(runtime_seconds or 0),
                        details_runtime_seconds,
                        item_id
                    ),
                    level="DEBUG"
                )

            runtime_seconds = details_runtime_seconds

            jellydream_cache_set(
                _GLOBAL_DETAILS_CACHE,
                item_id,
                preloaded_stream_data
            )

        if audio_stream_index is None:
            preferred_language = jellydream_default_audio_language()
            if preferred_language:
                resolved_audio_index = jellyfin_find_audio_stream_index_by_language(
                    item_id,
                    preferred_language,
                    preloaded_details=preloaded_stream_data
                )
                if resolved_audio_index is not None:
                    audio_stream_index = resolved_audio_index
                    jd_log(
                        f"Default audio language '{preferred_language}' found -> Jellyfin MediaStream.Index {audio_stream_index} for item {item_id}",
                        level="INFO"
                    )
                else:
                    jd_log(
                        f"Default audio language '{preferred_language}' not found for item {item_id} -> using the default track",
                        level="DEBUG"
                    )

        play_session_id = str(
            play_session_id
            or preloaded_stream_data.get(
                "_jellydream_play_session_id",
                ""
            )
            or uuid.uuid4()
        ).strip()

        selected_media_source_id = str(
            preloaded_stream_data.get(
                "_jellydream_media_source_id",
                ""
            ) or ""
        ).strip()

        url = str(
            prebuilt_stream_url
            or preloaded_stream_data.get(
                "_jellydream_prebuilt_stream_url",
                ""
            )
            or ""
        ).strip()

        if not url:
            url = build_jellyfin_stream(
                item_id,
                audio_stream_index=audio_stream_index,
                start_position_seconds=start_position_seconds,
                preloaded_details=preloaded_stream_data,
                play_session_id=play_session_id,
                preferred_media_source_id=selected_media_source_id
            )
        if not url:
            jd_log(f"Empty stream URL for item {item_id}", level="ERROR")
            return False, _(
                "Stream-URL konnte nicht erzeugt werden. Bitte zuerst einloggen."
            )

        # Falls die Audiospur automatisch im Streamaufbau gewählt wurde,
        # den tatsächlich in der URL verwendeten Jellyfin-Audioindex für
        # Infobar und Audio-Menü übernehmen.
        effective_audio_stream_index = audio_stream_index

        try:
            if effective_audio_stream_index is None:
                parsed_stream_url = urllib.parse.urlparse(url)
                stream_query = urllib.parse.parse_qs(
                    parsed_stream_url.query
                )

                audio_values = stream_query.get(
                    "AudioStreamIndex",
                    []
                )

                if audio_values:
                    effective_audio_stream_index = int(
                        audio_values[0]
                    )

        except Exception as e:
            jd_log(
                "Effective AudioStreamIndex could not be read from the stream URL: %s"
                % e,
                level="DEBUG"
            )
    
        service_reference = jellydream_get_service_reference()

        jd_log(
            "Starting stream with service reference %s" % service_reference,
            level="DEBUG"
        )

        #jd_log(
        #    "STREAM-URL ZUM TESTEN: %s"
        #    % url,
        #    level="DEBUG"
        #)

        # `url` bleibt die normale Jellyfin-URL einschließlich `api_key`.
        #
        # Nur die an Enigma2 übergebene Player-URL bekommt zusätzlich
        # Authorization und die Jellyfin-Token-Header.
        player_auth_snapshot = jellydream_auth_snapshot()

        player_url = jellydream_build_authenticated_player_url(
            url,
            auth_snapshot=player_auth_snapshot
        )

        ref = eServiceReference(
            service_reference,
            0,
            player_url
        )

        ref.setName(title)

        old_ref = None
        try:
            old_ref = session.nav.getCurrentlyPlayingServiceReference()
        except Exception:
            old_ref = None

        try:
            if return_screen and getattr(return_screen, "instance", None):
                return_screen.instance.hide()
        except Exception:
            pass

        try:
            session.nav.playService(ref)
        except Exception as e:
            msg = _(
                "Wiedergabe konnte nicht gestartet werden: %s"
            ) % e
            jd_log(f"Playback could not be started: {e}", level="ERROR")
            return False, msg

        def player_closed_callback(playback_result=None):
            jellydream_handle_player_closed(
                session=session,
                original_callback=callback,
                return_screen=return_screen,
                playback_result=playback_result
            )

        ok, err = jellydream_safe_open(
            session,
            player_closed_callback,
            JellyDreamStreamScreen,
            title,
            old_ref,
            return_screen,
            item_id,
            int(runtime_seconds or 0),
            int(start_position_seconds or 0),
            "",
            "",
            "",
            effective_audio_stream_index,
            preloaded_stream_data,
            url,
            service_reference,
            play_session_id
        )
        
        if not ok:
            try:
                if old_ref:
                    session.nav.playService(old_ref)
                else:
                    session.nav.stopService()
            except Exception:
                pass
            return False, err

        return True, ""
    except Exception as e:
        msg = _(
            "Kritischer Fehler beim Starten des Players: %s"
        ) % e
        jd_log(f"Critical error while starting the player: {e}", level="ERROR")
        return False, msg


class JellyDreamAudioMenu(Screen):
    skin = """
        <screen name="JellyDreamAudioMenu" position="center,center" size="900,620" title="Ton &amp; Untertitel" backgroundColor="#101010">
            <widget name="title" position="30,20" size="840,40" font="Regular;30" foregroundColor="#ffffff" backgroundColor="#101010" />
            <widget name="list" position="30,80" size="840,440" font="Regular;24" scrollbarMode="showOnDemand" />
            <widget name="status" position="30,540" size="840,45" font="Regular;24" foregroundColor="#ffffff" backgroundColor="#101010" />
        </screen>
    """

    def __init__(
        self,
        session,
        tracks=None,
        current_index=-1,
        on_select_callback=None
    ):
        Screen.__init__(self, session)

        self.tracks = (
            list(tracks)
            if isinstance(tracks, list)
            else []
        )

        try:
            self.current_index = int(
                current_index
                if current_index is not None
                else -1
            )
        except Exception:
            self.current_index = -1

        self.pending_index = self.current_index
        self.on_select_callback = on_select_callback

        self["title"] = Label(_("Ton & Untertitel"))
        self["list"] = MenuList([])
        self["status"] = Label("")
        jellydream_bind_status_symbols(self["status"])
        self["status"].setText(
            _("OK = Auswählen   HOCH/RUNTER = Navigieren   EXIT = Schließen")
        )

        self["actions"] = ActionMap(
            [
                "OkCancelActions",
                "DirectionActions"
            ],
            {
                "ok": self.okPressed,
                "cancel": self.cancel,
                "up": self.moveUp,
                "down": self.moveDown,
                "left": self.pageUp,
                "right": self.pageDown,
            },
            -1
        )

        self.onLayoutFinish.append(
            self.buildList
        )
        self.onLayoutFinish.append(
            self.applyStatusSymbolFont
        )

    def applyStatusSymbolFont(self):
        jellydream_apply_status_symbol_font(self)

    def buildList(self):
        entries = []
        selected_pos = 0
        first_selected_pos = None

        for pos, track in enumerate(self.tracks):
            if not isinstance(track, dict):
                continue

            track_kind = str(
                track.get("kind")
                or "audio"
            ).strip().lower()

            is_selected = bool(
                track.get("selected", False)
            )

            if track_kind == "subtitle":
                if track.get("disabled", False):
                    label = _("[Untertitel] Aus")
                else:
                    lang = str(
                        track.get("language_display")
                        or track.get("language")
                        or ""
                    ).strip()

                    codec = str(
                        track.get("codec_display")
                        or track.get("codec")
                        or ""
                    ).strip()

                    desc = str(
                        track.get("description")
                        or ""
                    ).strip()

                    flags = []

                    if track.get("forced", False):
                        flags.append(_("Erzwungen"))

                    if track.get("hearing_impaired", False):
                        flags.append(_("Hörgeschädigte"))

                    if track.get("external", False):
                        flags.append(_("Extern"))

                    parts = []

                    if lang:
                        parts.append(lang)

                    if codec:
                        parts.append(codec)

                    if desc:
                        existing_text = " ".join(parts).lower()

                        if desc.lower() not in existing_text:
                            parts.append(desc)

                    if flags:
                        parts.append(", ".join(flags))

                    subtitle_text = " | ".join(
                        part
                        for part in parts
                        if part
                    )

                    if not subtitle_text:
                        subtitle_text = _("Untertitelspur")

                    label = _("[Untertitel] %s") % subtitle_text

            else:
                lang = str(
                    track.get("language_display")
                    or track.get("language")
                    or ""
                ).strip()

                codec = str(
                    track.get("codec_display")
                    or track.get("codec")
                    or ""
                ).strip()

                desc = str(
                    track.get("description")
                    or ""
                ).strip()

                channels = str(
                    track.get("channels_display")
                    or ""
                ).strip()

                parts = []

                if lang:
                    parts.append(lang)

                if codec:
                    parts.append(codec)

                if channels:
                    parts.append(channels)

                if desc:
                    existing_text = " ".join(parts).lower()

                    if desc.lower() not in existing_text:
                        parts.append(desc)

                audio_text = " | ".join(
                    part
                    for part in parts
                    if part
                )

                if not audio_text:
                    audio_text = _("Audiospur")

                label = _("[Ton] %s") % audio_text

            marker = "✓ " if is_selected else "  "

            entries.append((
                "%s%s" % (
                    marker,
                    label
                ),
                pos
            ))

            if is_selected and first_selected_pos is None:
                first_selected_pos = len(entries) - 1

        self["list"].setList(entries)

        if first_selected_pos is not None:
            selected_pos = first_selected_pos

        try:
            self["list"].moveToIndex(
                selected_pos
            )
        except Exception:
            pass

    def moveUp(self):
        try:
            self["list"].up()
        except Exception:
            pass

    def moveDown(self):
        try:
            self["list"].down()
        except Exception:
            pass

    def pageUp(self):
        try:
            self["list"].pageUp()
        except Exception:
            pass

    def pageDown(self):
        try:
            self["list"].pageDown()
        except Exception:
            pass

    def okPressed(self):
        try:
            current = self["list"].getCurrent()

            if not current:
                return

            original_position = int(
                current[1]
            )

            if (
                original_position < 0
                or original_position >= len(self.tracks)
            ):
                return

            track = self.tracks[
                original_position
            ]

            if not isinstance(track, dict):
                return

            if callable(self.on_select_callback):
                self.on_select_callback(track)

            self.close(track)

        except Exception as e:
            jd_log(
                "Error in JellyDreamAudioMenu.okPressed: %s"
                % e,
                level="WARNING"
            )

    def cancel(self):
        self.close(None)


class JellyDreamAudioController:
    def __init__(self, player):
        self.player = player

        self.audio_tracks_cache = []
        self.audio_track_map_by_service_index = {}

        self.selected_jellyfin_audio_stream_index = None
        self.initial_audio_stream_index = None

        # `None` bedeutet: Untertitel deaktiviert.
        self.selected_jellyfin_subtitle_stream_index = None

        self._forced_audio_track_index = -1
        self._pending_audio_track_index = -1
        self._pending_restart_jellyfin_audio_index = None
        self._audio_track_before_menu = -1
        self._audio_refresh_retry = 0

        # Verhindert parallele Audio-Neustarts, beispielsweise bei mehrfach
        # schnellem Drücken auf OK im Audio-Menü.
        self._audio_restart_in_progress = False
        self._audio_restart_token = 0

    def applyInitialAudioSelection(self):
        try:
            if self.player._initial_audio_applied:
                return

            wanted_jellyfin_index = self.initial_audio_stream_index

            if wanted_jellyfin_index is None:
                wanted_jellyfin_index = (
                    self.selected_jellyfin_audio_stream_index
                )

            if wanted_jellyfin_index is None:
                jd_log(
                    "No initial Jellyfin audio track available to apply",
                    level="DEBUG"
                )
                self.player._initial_audio_applied = True
                return

            try:
                wanted_jellyfin_index = int(wanted_jellyfin_index)
            except Exception:
                jd_log(
                    f"Invalid initial Jellyfin audio index: {wanted_jellyfin_index}",
                    level="WARNING"
                )
                self.player._initial_audio_applied = True
                return

            tracks = self.buildAudioTrackMenuData()

            target_track = None

            for track in tracks:
                try:
                    track_jellyfin_index = track.get(
                        "jellyfin_index",
                        None
                    )

                    if track_jellyfin_index is None:
                        continue

                    if int(track_jellyfin_index) == wanted_jellyfin_index:
                        target_track = track
                        break

                except Exception:
                    continue

            if not target_track:
                self.player._initial_audio_apply_retries += 1

                jd_log(
                    f"Initial audio track is not available yet: JellyfinIndex={wanted_jellyfin_index}, attempt={self.player._initial_audio_apply_retries}",
                    level="DEBUG"
                )

                if self.player._initial_audio_apply_retries < 8:
                    jellydream_restart_single_timer(
                        self.player,
                        "_initial_audio_apply_timer",
                        self.applyInitialAudioSelection,
                        500
                    )
                else:
                    jd_log(
                        "Initial audio track could not be set after several attempts",
                        level="WARNING"
                    )
                    self.player._initial_audio_applied = True

                return

            service_index = int(
                target_track.get("service_index", -1)
            )

            if service_index < 0:
                self.selected_jellyfin_audio_stream_index = (
                    wanted_jellyfin_index
                )

                self.player._initial_audio_applied = True

                jd_log(
                    "Initial Jellyfin audio track was already selected via the stream URL: JellyfinIndex=%s; the player does not expose a switchable service track"
                    % wanted_jellyfin_index,
                    level="DEBUG"
                )

                self.delayedAudioInfoRefresh()
                return

            current_service_index = self.getCurrentAudioTrackIndex()

            jd_log(
                f"Initial audio selection: JellyfinIndex={wanted_jellyfin_index}, "
                f"ServiceIndex={service_index}, current service index={current_service_index}",
                level="DEBUG"
            )

            if current_service_index != service_index:
                if self.applyAudioTrackByIndex(service_index):
                    self._forced_audio_track_index = service_index
                    self._pending_audio_track_index = -1
                    self.selected_jellyfin_audio_stream_index = (
                        wanted_jellyfin_index
                    )

                    self.player._initial_audio_applied = True

                    jd_log(
                        f"Initial audio track set: ServiceIndex={service_index}, "
                        f"JellyfinIndex={wanted_jellyfin_index}",
                        level="INFO"
                    )

                    self.delayedAudioInfoRefresh()
                    return

                jd_log(
                    f"Initial audio track could not be set: ServiceIndex={service_index}",
                    level="WARNING"
                )

            else:
                self._forced_audio_track_index = service_index
                self.player._initial_audio_applied = True

                jd_log(
                    f"Initial audio track was already active: ServiceIndex={service_index}",
                    level="DEBUG"
                )

                self.delayedAudioInfoRefresh()

        except Exception as e:
            jd_log(
                f"Error in applyInitialAudioSelection: {e}",
                level="WARNING"
            )
        
    def setInitialAudioStreamIndex(self, audio_stream_index):
        if audio_stream_index is None:
            self.selected_jellyfin_audio_stream_index = None
            self.initial_audio_stream_index = None
            return

        try:
            audio_stream_index = int(audio_stream_index)
        except Exception:
            audio_stream_index = None

        self.selected_jellyfin_audio_stream_index = audio_stream_index
        self.initial_audio_stream_index = audio_stream_index

    def normalizeAudioLanguage(self, lang):
        try:
            raw = str(lang or "").strip()
            if not raw:
                return ""

            mapping = {
                "de": "Deutsch", "deu": "Deutsch", "ger": "Deutsch", "deutsch": "Deutsch",
                "en": "English", "eng": "English", "english": "English",
                "fr": "Français", "fre": "Français", "fra": "Français", "francais": "Français", "français": "Français",
                "it": "Italiano", "ita": "Italiano", "italiano": "Italiano",
                "es": "Español", "spa": "Español", "espanol": "Español", "español": "Español",
                "nl": "Nederlands", "dut": "Nederlands", "nld": "Nederlands",
                "pl": "Polski", "pol": "Polski",
                "pt": "Português", "por": "Português",
                "ru": "Русский", "rus": "Русский",
                "tr": "Türkçe", "tur": "Türkçe",
                "cs": "Čeština", "cze": "Čeština", "ces": "Čeština",
                "sk": "Slovenčina", "slo": "Slovenčina", "slk": "Slovenčina",
                "hu": "Magyar", "hun": "Magyar",
                "ro": "Română", "rum": "Română", "ron": "Română",
                "bg": "Български", "bul": "Български",
                "el": "Ελληνικά", "gre": "Ελληνικά", "ell": "Ελληνικά",
                "sv": "Svenska", "swe": "Svenska",
                "no": "Norsk", "nor": "Norsk",
                "da": "Dansk", "dan": "Dansk",
                "fi": "Suomi", "fin": "Suomi",
                "hr": "Hrvatski", "hrv": "Hrvatski",
                "sr": "Srpski", "srp": "Srpski",
                "sl": "Slovenščina", "slv": "Slovenščina",
                "uk": "Українська", "ukr": "Українська",
                "ja": "Japanese", "jpn": "Japanese",
                "ko": "Korean", "kor": "Korean",
                "zh": "Chinese", "chi": "Chinese", "zho": "Chinese",
            }

            invalid_values = {"und", "undefined", "unknown", "mis", "mul", "null", "none", "n/a"}

            def normalize_single_token(token):
                t = str(token or "").strip()
                if not t:
                    return ""
                lower_t = t.lower()
                if lower_t in invalid_values:
                    return ""
                return mapping.get(lower_t, t)

            def split_with_separator(text):
                candidates = [
                    (" / ", " / "),
                    ("/", " / "),
                    (", ", ", "),
                    (",", ", "),
                    ("; ", " / "),
                    (";", " / "),
                    (" | ", " | "),
                    ("|", " | "),
                ]
                for needle, output_sep in candidates:
                    if needle in text:
                        return text.split(needle), output_sep
                return [text], None

            def normalize_compound(text):
                text = str(text or "").strip()
                if not text:
                    return ""
                parts, output_sep = split_with_separator(text)
                if output_sep is None:
                    return normalize_single_token(text)
                normalized_parts = [normalize_single_token(x) for x in parts]
                normalized_parts = [p for p in normalized_parts if p]
                if not normalized_parts:
                    return ""
                return output_sep.join(normalized_parts)

            def normalize_segment(segment):
                segment = str(segment or "").strip()
                if not segment:
                    return ""
                if "(" in segment and ")" in segment:
                    start = segment.find("(")
                    prefix = segment[:start].strip()
                    suffix = segment[start:].strip()
                    normalized_prefix = normalize_compound(prefix)
                    if normalized_prefix:
                        return f"{normalized_prefix} {suffix}".strip()
                    return suffix
                return normalize_compound(segment)

            return normalize_segment(raw).strip()
        except Exception as e:
            jd_log(f"Error in normalizeAudioLanguage: {e}", level="WARNING")
            try:
                return str(lang or "").strip()
            except Exception:
                return ""

    def getDetailedAudioCodecFromMeta(self, meta):
        """
        Ermittelt die genaue Audiobezeichnung aus den Jellyfin-Metadaten.

        Erkennt unter anderem:
        - Dolby TrueHD + Dolby Atmos
        - Dolby Digital Plus + Dolby Atmos
        - Dolby TrueHD
        - Dolby Digital Plus
        - Dolby Digital
        - DTS-HD MA + DTS:X
        """
        try:
            if not isinstance(meta, dict):
                return ""

            codec = str(
                meta.get("Codec")
                or ""
            ).strip().lower()

            codec_key = (
                codec
                .replace("-", "")
                .replace("_", "")
                .replace(" ", "")
            )

            description_parts = []

            for field_name in (
                "Title",
                "DisplayTitle",
                "Profile",
                "CodecTag",
                "CodecTagString",
                "ChannelLayout"
            ):
                field_value = str(
                    meta.get(field_name)
                    or ""
                ).strip()

                if field_value:
                    description_parts.append(field_value)

            description = " ".join(description_parts)
            description_upper = description.upper()

            has_atmos = any(
                marker in description_upper
                for marker in (
                    "DOLBY ATMOS",
                    "ATMOS",
                    "E-AC-3 JOC",
                    "E-AC3 JOC",
                    "EAC3 JOC",
                    "DD+ JOC",
                    "JOC"
                )
            )

            # --------------------------------------------------------------
            # Dolby TrueHD / Dolby Atmos
            # --------------------------------------------------------------
            if codec_key in (
                "truehd",
                "mlp",
                "mlpfba"
            ):
                if has_atmos:
                    return "Dolby TrueHD + Dolby Atmos"

                return "Dolby TrueHD"

            # --------------------------------------------------------------
            # Dolby Digital Plus / Dolby Atmos
            #
            # Atmos in E-AC3 wird üblicherweise über JOC signalisiert.
            # --------------------------------------------------------------
            if codec_key in (
                "eac3",
                "ec3",
                "ddp",
                "ddplus"
            ):
                if has_atmos:
                    return "Dolby Digital Plus + Dolby Atmos"

                return "Dolby Digital Plus"

            # --------------------------------------------------------------
            # Dolby Digital
            # --------------------------------------------------------------
            if codec_key in (
                "ac3",
                "ac3fixed"
            ):
                return "Dolby Digital"

            # --------------------------------------------------------------
            # DTS-Varianten
            # --------------------------------------------------------------
            if codec_key == "dts":
                has_dts_x = (
                    "DTS:X" in description_upper
                    or "DTS X" in description_upper
                )

                has_dts_hd_ma = (
                    "DTS-HD MA" in description_upper
                    or "DTS HD MA" in description_upper
                    or "MASTER AUDIO" in description_upper
                )

                has_dts_hd_hra = (
                    "DTS-HD HRA" in description_upper
                    or "DTS HD HRA" in description_upper
                    or "HIGH RESOLUTION AUDIO" in description_upper
                )

                has_dts_hd = (
                    "DTS-HD" in description_upper
                    or "DTS HD" in description_upper
                )

                if has_dts_x and has_dts_hd_ma:
                    return "DTS-HD MA + DTS:X"

                if has_dts_x:
                    return "DTS:X"

                if has_dts_hd_ma:
                    return "DTS-HD MA"

                if has_dts_hd_hra:
                    return "DTS-HD HRA"

                if has_dts_hd:
                    return "DTS-HD"

                return "DTS"

            # Falls `Codec` leer oder unbekannt ist, zusätzlich den gesamten
            # Beschreibungstext auswerten.
            guessed_codec = self.guessCodecFromText(
                "%s %s" % (
                    codec,
                    description
                )
            )

            if guessed_codec:
                return guessed_codec

            codec_names = {
                "aac": "AAC",
                "mp3": "MP3",
                "mp2": "MP2",
                "flac": "FLAC",
                "pcm": "PCM",
                "opus": "OPUS",
                "vorbis": "VORBIS"
            }

            return codec_names.get(
                codec,
                codec.upper()
            )

        except Exception as e:
            jd_log(
                "Detailed audio codec could not be resolved: %s" % e,
                level="DEBUG"
            )

            return ""
            
    def guessCodecFromText(self, text):
        """
        Erkennt Audiocodecs aus der Beschreibung des aktiven
        Enigma2-Service-Tracks.
        """
        try:
            value = str(
                text or ""
            ).strip().upper()

            if not value:
                return ""

            has_atmos = any(
                marker in value
                for marker in (
                    "DOLBY ATMOS",
                    "ATMOS",
                    "E-AC-3 JOC",
                    "E-AC3 JOC",
                    "EAC3 JOC",
                    "DD+ JOC",
                    "JOC"
                )
            )

            has_truehd = any(
                marker in value
                for marker in (
                    "TRUEHD",
                    "TRUE HD",
                    "DOLBY TRUEHD",
                    "MLP FBA"
                )
            )

            has_eac3 = any(
                marker in value
                for marker in (
                    "EAC3",
                    "E-AC3",
                    "E-AC-3",
                    "EC-3",
                    "DOLBY DIGITAL PLUS",
                    "DD+",
                    "DDP"
                )
            )

            # --------------------------------------------------------------
            # Dolby Atmos
            # --------------------------------------------------------------
            if has_truehd and has_atmos:
                return "Dolby TrueHD + Dolby Atmos"

            if has_eac3 and has_atmos:
                return "Dolby Digital Plus + Dolby Atmos"

            # --------------------------------------------------------------
            # DTS
            # --------------------------------------------------------------
            has_dts_x = (
                "DTS:X" in value
                or "DTS X" in value
            )

            has_dts_hd_ma = (
                "DTS-HD MA" in value
                or "DTS HD MA" in value
                or "MASTER AUDIO" in value
            )

            has_dts_hd_hra = (
                "DTS-HD HRA" in value
                or "DTS HD HRA" in value
                or "HIGH RESOLUTION AUDIO" in value
            )

            if has_dts_x and has_dts_hd_ma:
                return "DTS-HD MA + DTS:X"

            if has_dts_x:
                return "DTS:X"

            if has_dts_hd_ma:
                return "DTS-HD MA"

            if has_dts_hd_hra:
                return "DTS-HD HRA"

            if "DTS-HD" in value or "DTS HD" in value:
                return "DTS-HD"

            # --------------------------------------------------------------
            # Dolby ohne Atmos
            # --------------------------------------------------------------
            if has_truehd:
                return "Dolby TrueHD"

            if has_eac3:
                return "Dolby Digital Plus"

            if (
                "DOLBY DIGITAL" in value
                or "AC3" in value
                or "AC-3" in value
            ):
                return "Dolby Digital"

            if "DTS" in value:
                return "DTS"

            if "AAC" in value:
                return "AAC"

            if "MP3" in value:
                return "MP3"

            if "MP2" in value or "MPEG AUDIO" in value:
                return "MP2"

            if "PCM" in value:
                return "PCM"

            if "FLAC" in value:
                return "FLAC"

            if "OPUS" in value:
                return "OPUS"

            if "VORBIS" in value:
                return "VORBIS"

            return ""

        except Exception:
            return ""

    def getCurrentAudioTrackIndex(self):
        try:
            service = self.player.session.nav.getCurrentService()
            if not service:
                return -1
            audio = service.audioTracks()
            if not audio:
                return -1
            idx = audio.getCurrentTrack()
            if idx is None:
                return -1
            return int(idx)
        except Exception as e:
            jd_log(f"Error while reading the current audio track index: {e}", level="WARNING")
            return -1

    def getServiceAudioTracks(self):
        tracks = []
        try:
            service = self.player.session.nav.getCurrentService()
            if not service:
                return tracks

            audio = service.audioTracks()
            if not audio:
                return tracks

            count = audio.getNumberOfTracks()
            for i in range(count):
                try:
                    info = audio.getTrackInfo(i)
                    if not info:
                        continue

                    lang = ""
                    desc = ""

                    try:
                        lang = info.getLanguage() or ""
                    except Exception:
                        pass

                    try:
                        desc = info.getDescription() or ""
                    except Exception:
                        pass

                    tracks.append({
                        "service_index": int(i),
                        "language": str(lang or "").strip(),
                        "description": str(desc or "").strip(),
                    })
                except Exception:
                    pass
        except Exception as e:
            jd_log(f"Error in getServiceAudioTracks: {e}", level="WARNING")

        return tracks

    def _audioMetaToMenuItem(self, meta, service_index=-1, selected=False):
        try:
            if not isinstance(meta, dict):
                return None

            raw_lang = (
                meta.get("DisplayLanguage")
                or meta.get("Language")
                or ""
            )

            description = (
                meta.get("Title")
                or meta.get("DisplayTitle")
                or ""
            )

            codec = self.getDetailedAudioCodecFromMeta(
                meta
            )

            channels_display = ""

            try:
                channels = int(meta.get("Channels") or 0)

                if channels == 6:
                    channels_display = "5.1"
                elif channels == 8:
                    channels_display = "7.1"
                elif channels > 0:
                    channels_display = f"{channels}ch"
            except Exception:
                channels_display = ""

            jellyfin_index = meta.get(
                "JellyfinIndex",
                meta.get("Index")
            )

            try:
                jellyfin_index = int(jellyfin_index)
            except Exception:
                jellyfin_index = None

            language_display = self.normalizeAudioLanguage(raw_lang)

            return {
                "service_index": int(service_index),
                "jellyfin_index": jellyfin_index,
                "language": str(raw_lang or "").strip(),
                "language_display": str(language_display or "").strip(),
                "description": str(description or "").strip(),
                "codec": codec,
                "codec_display": codec,
                "channels_display": channels_display,
                "selected": bool(selected),
            }

        except Exception as e:
            jd_log(
                f"Error in _audioMetaToMenuItem: {e}",
                level="WARNING"
            )
            return None

    def _findServiceIndexForAudioMeta(
        self,
        meta,
        service_tracks,
        used_service_indexes
    ):
        try:
            if not isinstance(meta, dict):
                return -1

            meta_language = self.normalizeAudioLanguage(
                meta.get("DisplayLanguage")
                or meta.get("Language")
                or ""
            ).strip().lower()

            meta_codec = str(
                meta.get("Codec")
                or ""
            ).strip().lower()

            meta_title = str(
                meta.get("Title")
                or meta.get("DisplayTitle")
                or ""
            ).strip().lower()

            for service_track in service_tracks:
                service_index = int(
                    service_track.get("service_index", -1)
                )

                if service_index in used_service_indexes:
                    continue

                service_language = self.normalizeAudioLanguage(
                    service_track.get("language")
                    or ""
                ).strip().lower()

                service_description = str(
                    service_track.get("description")
                    or ""
                ).strip().lower()

                service_codec = self.guessCodecFromText(
                    service_description
                ).strip().lower()

                language_match = (
                    meta_language
                    and service_language
                    and meta_language == service_language
                )

                codec_match = (
                    meta_codec
                    and service_codec
                    and meta_codec == service_codec
                )

                title_match = (
                    meta_title
                    and (
                        meta_title in service_description
                        or service_description in meta_title
                    )
                )

                if language_match and (codec_match or title_match):
                    return service_index

            for service_track in service_tracks:
                service_index = int(
                    service_track.get("service_index", -1)
                )

                if service_index in used_service_indexes:
                    continue

                service_language = self.normalizeAudioLanguage(
                    service_track.get("language")
                    or ""
                ).strip().lower()

                if (
                    meta_language
                    and service_language
                    and meta_language == service_language
                ):
                    return service_index

            for service_track in service_tracks:
                service_index = int(
                    service_track.get("service_index", -1)
                )

                if service_index not in used_service_indexes:
                    return service_index

        except Exception as e:
            jd_log(
                f"Error while mapping an audio service track: {e}",
                level="WARNING"
            )

        return -1

    def getTargetJellyfinAudioIndex(self):
        try:
            wanted = getattr(
                self,
                "selected_jellyfin_audio_stream_index",
                None
            )

            if wanted is None:
                wanted = getattr(
                    self,
                    "initial_audio_stream_index",
                    None
                )

            if wanted is None:
                return None

            return int(wanted)

        except Exception:
            return None
            
    def buildAudioTrackMenuData(self):
        result = []

        try:
            service_tracks = self.getServiceAudioTracks()
            media_audio = self.audio_tracks_cache or []

            current_service_index = self.getCurrentAudioTrackIndex()

            pending_index = int(
                getattr(
                    self,
                    "_pending_audio_track_index",
                    -1
                )
            )

            forced_index = int(
                getattr(
                    self,
                    "_forced_audio_track_index",
                    -1
                )
            )

            selected_jellyfin_index = getattr(
                self,
                "selected_jellyfin_audio_stream_index",
                None
            )

            if media_audio:
                used_service_indexes = set()
                target_jellyfin_index = self.getTargetJellyfinAudioIndex()

                single_remux_service = (
                    len(service_tracks) == 1
                    and len(media_audio) > 1
                    and target_jellyfin_index is not None
                )

                if single_remux_service:
                    jd_log(
                        "Single-remux service detected: ServiceIndex=0 is mapped to JellyfinIndex=%s"
                        % target_jellyfin_index,
                        level="DEBUG"
                    )

                for pos, meta in enumerate(media_audio):
                    if not isinstance(meta, dict):
                        continue

                    jellyfin_index = meta.get(
                        "JellyfinIndex",
                        meta.get("Index")
                    )

                    try:
                        jellyfin_index = int(jellyfin_index)
                    except Exception:
                        jellyfin_index = None

                    if (
                        single_remux_service
                        and jellyfin_index == target_jellyfin_index
                    ):
                        service_index = int(
                            service_tracks[0].get("service_index", 0)
                        )
                        used_service_indexes.add(service_index)

                        jd_log(
                            "Remux-Audio-Mapping: "
                            "service_index=%s -> "
                            "jellyfin_index=%s"
                            % (
                                service_index,
                                jellyfin_index
                            ),
                            level="DEBUG"
                        )

                    else:
                        if single_remux_service:
                            service_index = -1
                        else:
                            service_index = self._findServiceIndexForAudioMeta(
                                meta,
                                service_tracks,
                                used_service_indexes
                            )

                            if service_index >= 0:
                                used_service_indexes.add(service_index)

                    is_selected = False

                    if single_remux_service:
                        # Bei einem Single-Track-Remux existiert nur ein lokaler
                        # Service-Track. Hier entscheidet ausschließlich die aktuell
                        # über Jellyfin angeforderte Spur.
                        if (
                            target_jellyfin_index is not None
                            and jellyfin_index is not None
                        ):
                            is_selected = (
                                int(jellyfin_index)
                                == int(target_jellyfin_index)
                            )

                    else:
                        # Bei mehreren echten Service-Tracks ist Enigma2 die maßgebliche
                        # Quelle. Genau eine Spur darf ausgewählt sein.
                        #
                        # Priorität:
                        # 1. gerade angeforderter Wechsel,
                        # 2. bereits bestätigter/erzwungener Track,
                        # 3. aktuell vom Service gemeldeter Track,
                        # 4. Jellyfin-Index nur als Fallback, falls kein Mapping existiert.
                        active_service_index = -1

                        if pending_index >= 0:
                            active_service_index = pending_index
                        elif forced_index >= 0:
                            active_service_index = forced_index
                        elif current_service_index >= 0:
                            active_service_index = current_service_index

                        if service_index >= 0 and active_service_index >= 0:
                            is_selected = (
                                service_index == active_service_index
                            )

                        elif (
                            service_index < 0
                            and target_jellyfin_index is not None
                            and jellyfin_index is not None
                        ):
                            # Kein lokales Track-Mapping vorhanden: nur dann darf
                            # Jellyfin den Haken bestimmen.
                            is_selected = (
                                int(jellyfin_index)
                                == int(target_jellyfin_index)
                            )

                    item = self._audioMetaToMenuItem(
                        meta,
                        service_index=service_index,
                        selected=is_selected
                    )

                    if item:
                        result.append(item)

            elif service_tracks:
                for service_track in service_tracks:
                    service_index = int(
                        service_track.get("service_index", -1)
                    )

                    raw_language = str(
                        service_track.get("language")
                        or ""
                    ).strip()

                    description = str(
                        service_track.get("description")
                        or ""
                    ).strip()

                    codec = self.guessCodecFromText(
                        description
                    )

                    language_display = self.normalizeAudioLanguage(
                        raw_language
                    )

                    result.append({
                        "service_index": service_index,
                        "jellyfin_index": None,
                        "language": raw_language,
                        "language_display": language_display,
                        "description": description,
                        "codec": codec,
                        "codec_display": codec,
                        "channels_display": "",
                        "selected": (
                            service_index == current_service_index
                            or service_index == pending_index
                            or service_index == forced_index
                        ),
                    })

            self.audio_track_map_by_service_index = {}

            for item in result:
                try:
                    service_index = int(
                        item.get("service_index", -1)
                    )

                    if service_index >= 0:
                        self.audio_track_map_by_service_index[
                            service_index
                        ] = item
                except Exception:
                    pass

            for track in result:
                jd_log(
                    f"Audio mapping: service_index={track.get('service_index')}, "
                    f"jellyfin_index={track.get('jellyfin_index')}, language={track.get('language_display')}, description={track.get('description')}, "
                    f"codec={track.get('codec_display')}",
                    level="DEBUG"
                )
    
            jd_log(
                f"Audio menu built: service tracks={len(service_tracks)}, Jellyfin meta={len(media_audio)}, menu entries={len(result)}, Jellyfin metadata takes priority",
                level="DEBUG"
            )

        except Exception as e:
            jd_log(
                f"Error in buildAudioTrackMenuData: {e}",
                level="WARNING"
            )

        return result

    def getPreferredAudioTrackDataByJellyfinIndex(self):
        try:
            wanted = getattr(self, "selected_jellyfin_audio_stream_index", None)
            if wanted is None:
                wanted = getattr(self, "initial_audio_stream_index", None)
            if wanted is None:
                return None

            try:
                wanted = int(wanted)
            except Exception:
                return None

            track_map = getattr(self, "audio_track_map_by_service_index", {}) or {}
            for _, track in track_map.items():
                try:
                    jf_index = track.get("jellyfin_index", None)
                    if jf_index is None:
                        continue
                    if int(jf_index) == wanted:
                        return track
                except Exception:
                    pass

            for meta in (self.audio_tracks_cache or []):
                try:
                    jf_index = meta.get("JellyfinIndex", meta.get("Index"))
                    if jf_index is None:
                        continue
                    if int(jf_index) == wanted:
                        raw_lang = meta.get("DisplayLanguage") or meta.get("Language") or ""
                        desc = meta.get("Title") or meta.get("DisplayTitle") or ""
                        codec = self.getDetailedAudioCodecFromMeta(
                            meta
                        )

                        channels_display = ""
                        try:
                            ch = int(meta.get("Channels") or 0)
                            if ch == 6:
                                channels_display = "5.1"
                            elif ch == 8:
                                channels_display = "7.1"
                            elif ch > 0:
                                channels_display = f"{ch}ch"
                        except Exception:
                            pass

                        return {
                            "service_index": -1,
                            "jellyfin_index": int(jf_index),
                            "language": str(raw_lang or "").strip(),
                            "language_display": str(self.normalizeAudioLanguage(raw_lang) or "").strip(),
                            "description": str(desc or "").strip(),
                            "codec": str(codec or "").strip(),
                            "codec_display": str(codec or "").strip(),
                            "channels_display": str(channels_display or "").strip(),
                            "selected": True,
                        }
                except Exception:
                    pass

            return None
        except Exception as e:
            jd_log(f"Error in getPreferredAudioTrackDataByJellyfinIndex: {e}", level="WARNING")
            return None

    def getPreferredAudioTrackData(self):
        try:
            current_idx = self.getCurrentAudioTrackIndex()
            track_map = getattr(
                self,
                "audio_track_map_by_service_index",
                {}
            ) or {}

            forced_idx = int(
                getattr(self, "_forced_audio_track_index", -1)
            )

            pending_idx = int(
                getattr(self, "_pending_audio_track_index", -1)
            )

            if forced_idx >= 0 and forced_idx in track_map:
                return track_map[forced_idx]

            if pending_idx >= 0 and pending_idx in track_map:
                return track_map[pending_idx]

            if current_idx >= 0 and current_idx in track_map:
                return track_map[current_idx]

            by_jellyfin = self.getPreferredAudioTrackDataByJellyfinIndex()
            if by_jellyfin:
                return by_jellyfin

            return None

        except Exception as e:
            jd_log(
                f"Error in getPreferredAudioTrackData: {e}",
                level="WARNING"
            )
            return None

    def getPreferredAudioLanguageDisplay(self):
        try:
            current_language = self.getCurrentAudioLanguage()
            current_language = self.normalizeAudioLanguage(current_language)

            if current_language:
                return current_language

            preferred = self.getPreferredAudioTrackData()
            if preferred:
                lang = str(
                    preferred.get("language_display")
                    or preferred.get("language")
                    or ""
                ).strip()

                lang = self.normalizeAudioLanguage(lang)
                if lang:
                    return lang

                desc = str(
                    preferred.get("description")
                    or ""
                ).strip()

                desc = self.normalizeAudioLanguage(desc)
                if desc:
                    return desc

            return ""

        except Exception as e:
            jd_log(
                f"Error in getPreferredAudioLanguageDisplay: {e}",
                level="WARNING"
            )
            return ""

    def getCurrentAudioLanguage(self):
        try:
            current_service_index = self.getCurrentAudioTrackIndex()

            target_jellyfin_index = self.getTargetJellyfinAudioIndex()

            if (
                current_service_index >= 0
                and len(self.getServiceAudioTracks()) == 1
                and len(self.audio_tracks_cache or []) > 1
                and target_jellyfin_index is not None
            ):
                for meta in self.audio_tracks_cache or []:
                    try:
                        meta_index = meta.get(
                            "JellyfinIndex",
                            meta.get("Index")
                        )

                        if meta_index is None:
                            continue

                        if int(meta_index) != int(target_jellyfin_index):
                            continue

                        raw_language = (
                            meta.get("DisplayLanguage")
                            or meta.get("Language")
                            or ""
                        )

                        language_display = self.normalizeAudioLanguage(
                            raw_language
                        )

                        if language_display:
                            jd_log(
                                "Infobar language resolved from the active Jellyfin remux track: service_index=%s, jellyfin_index=%s, language=%s"
                                % (
                                    current_service_index,
                                    target_jellyfin_index,
                                    language_display
                                ),
                                level="DEBUG"
                            )
                            return language_display

                    except Exception:
                        continue

            if current_service_index >= 0:

                tracks = self.buildAudioTrackMenuData()

                for track in tracks:
                    try:
                        track_service_index = int(
                            track.get("service_index", -1)
                        )

                        if track_service_index != current_service_index:
                            continue

                        mapped_language = str(
                            track.get("language_display")
                            or track.get("language")
                            or ""
                        ).strip()

                        if mapped_language:
                            jd_log(
                                f"Infobar language resolved from audio mapping: service_index={current_service_index}, language={mapped_language}, "
                                f"jellyfin_index={track.get('jellyfin_index')}",
                                level="DEBUG"
                            )
                            return self.normalizeAudioLanguage(
                                mapped_language
                            )

                    except Exception:
                        continue

            service = self.player.session.nav.getCurrentService()

            if service:
                audio = service.audioTracks()

                if audio:
                    current_index = audio.getCurrentTrack()

                    if current_index is not None and current_index >= 0:
                        track_info = audio.getTrackInfo(current_index)

                        if track_info:
                            lang = ""
                            desc = ""

                            try:
                                lang = track_info.getLanguage() or ""
                            except Exception:
                                pass

                            try:
                                desc = track_info.getDescription() or ""
                            except Exception:
                                pass

                            normalized_lang = self.normalizeAudioLanguage(lang)

                            if normalized_lang:
                                jd_log(
                                    f"Infobar language resolved from receiver fallback: service_index={current_index}, language={normalized_lang}, raw={lang}",
                                    level="DEBUG"
                                )
                                return normalized_lang

                            if desc:
                                normalized_desc = self.normalizeAudioLanguage(desc)

                                if normalized_desc:
                                    return normalized_desc

            return ""

        except Exception as e:
            jd_log(
                f"Error while reading the audio language: {e}",
                level="WARNING"
            )
            return ""

    def getActiveAudioServiceCodec(self):
        try:
            service = self.player.session.nav.getCurrentService()
            if not service:
                return ""

            audio = service.audioTracks()
            if not audio:
                return ""

            current_index = audio.getCurrentTrack()
            if current_index is None or current_index < 0:
                return ""

            track_info = audio.getTrackInfo(current_index)
            if not track_info:
                return ""

            desc = ""
            try:
                desc = track_info.getDescription() or ""
            except Exception:
                pass

            return self.guessCodecFromText(desc)
        except Exception as e:
            jd_log(f"Error in getActiveAudioServiceCodec: {e}", level="WARNING")
            return ""

    def getPreferredAudioMetaCodec(self):
        try:
            preferred = self.getPreferredAudioTrackData()

            if preferred:
                codec = str(
                    preferred.get("codec_display")
                    or preferred.get("codec")
                    or ""
                ).strip()

                if codec:
                    return codec

        except Exception as e:
            jd_log(
                "Error in getPreferredAudioMetaCodec: %s" % e,
                level="WARNING"
            )

        return ""

    def getAudioCodecDisplayText(self):
        try:
            meta_codec = str(
                self.getPreferredAudioMetaCodec()
                or ""
            ).strip()

            service_codec = str(
                self.getActiveAudioServiceCodec()
                or ""
            ).strip()

            def codec_family(codec_text):
                value = str(
                    codec_text or ""
                ).strip().upper()

                if not value:
                    return ""

                if (
                    "TRUEHD" in value
                    or "TRUE HD" in value
                    or "MLP FBA" in value
                ):
                    return "truehd"

                if (
                    "DOLBY DIGITAL PLUS" in value
                    or "EAC3" in value
                    or "E-AC3" in value
                    or "E-AC-3" in value
                    or "DD+" in value
                    or "DDP" in value
                ):
                    return "eac3"

                if (
                    "DOLBY DIGITAL" in value
                    or "AC3" in value
                    or "AC-3" in value
                ):
                    return "ac3"

                if "DTS" in value:
                    return "dts"

                if "AAC" in value:
                    return "aac"

                if "MP3" in value:
                    return "mp3"

                if (
                    "MP2" in value
                    or "MPEG AUDIO" in value
                ):
                    return "mp2"

                if "FLAC" in value:
                    return "flac"

                if "PCM" in value:
                    return "pcm"

                if "OPUS" in value:
                    return "opus"

                if "VORBIS" in value:
                    return "vorbis"

                return value.lower()

            if meta_codec and service_codec:
                meta_family = codec_family(meta_codec)
                service_family = codec_family(service_codec)

                # Enigma2 meldet häufig nur den Basiskodec, während Jellyfin
                # die vollständige Variante inklusive Atmos oder DTS:X kennt.
                if (
                    meta_codec.lower() == service_codec.lower()
                    or (
                        meta_family
                        and meta_family == service_family
                    )
                ):
                    return meta_codec

                # Unterschiedliche Codec-Familien bedeuten normalerweise,
                # dass Audio tatsächlich transcodiert wurde.
                return "%s -> %s" % (
                    meta_codec,
                    service_codec
                )

            if meta_codec:
                return meta_codec

            if service_codec:
                return service_codec

            return str(
                getattr(
                    self.player,
                    "a_codec",
                    ""
                )
                or ""
            ).strip()

        except Exception as e:
            jd_log(
                "Error in getAudioCodecDisplayText: %s" % e,
                level="WARNING"
            )

            return str(
                getattr(
                    self.player,
                    "a_codec",
                    ""
                )
                or ""
            ).strip()

    def updateActiveAudioCodec(self):
        try:
            service_codec = self.getActiveAudioServiceCodec()
            if service_codec:
                self.player.a_codec = service_codec
                return

            meta_codec = self.getPreferredAudioMetaCodec()
            if meta_codec:
                self.player.a_codec = meta_codec
                return

        except Exception as e:
            jd_log(
                f"Error while updating the active audio codec: {e}",
                level="WARNING"
            )

    def delayedAudioInfoRefresh(self):
        try:
            self.updateActiveAudioCodec()
            self.player.updateCodecAndLanguageText()
            self.player.refreshInfobarData()
        except Exception as e:
            jd_log(f"Error in delayedAudioInfoRefresh: {e}", level="WARNING")

    def applyAudioTrackByIndex(self, index):
        try:
            index = int(index)
            service = self.player.session.nav.getCurrentService()
            if not service:
                return False

            audio = service.audioTracks()
            if not audio:
                return False

            audio.selectTrack(index)
            return True
        except Exception as e:
            jd_log(f"Error in applyAudioTrackByIndex({index}): {e}", level="WARNING")
            return False

    def shouldRestartStreamForAudioSelection(
        self,
        selected_track=None
    ):
        """
        Ein Neustart ist nur nötig, wenn der aktuell laufende Enigma2-Service
        die gewünschte Spur nicht selbst umschalten kann.

        Bei DirectPlay stellt Enigma2 / ExtePlayer3 häufig mehrere echte
        Service-Tracks bereit. Dann muss unbedingt audio.selectTrack() genutzt
        werden, weil ein Wechsel auf stream.ts den Resume-Seek verlieren kann.
        """
        try:
            if not isinstance(selected_track, dict):
                return False

            service_tracks = self.getServiceAudioTracks()
            meta_tracks = self.audio_tracks_cache or []

            try:
                selected_service_index = int(
                    selected_track.get("service_index", -1)
                )
            except Exception:
                selected_service_index = -1

            selected_jellyfin_index = selected_track.get(
                "jellyfin_index",
                None
            )

            current_service_index = self.getCurrentAudioTrackIndex()

            service_count = len(service_tracks)
            meta_count = len(meta_tracks)

            jd_log(
                "Audio mode check: "
                "service tracks=%s, Jellyfin meta tracks=%s, "
                "current_service_index=%s, selected_service_index=%s, "
                "selected_jellyfin_index=%s"
                % (
                    service_count,
                    meta_count,
                    current_service_index,
                    selected_service_index,
                    selected_jellyfin_index
                ),
                level="DEBUG"
            )

            # Wichtigster Fall:
            #
            # Der laufende Service besitzt mehrere echte Audiospuren und die
            # gewünschte Jellyfin-Spur wurde einem davon zugeordnet.
            #
            # Beispiel aus deinem Log:
            # Deutsch -> ServiceIndex 0
            # English -> ServiceIndex 1
            #
            # Hier darf KEIN Stream-Neustart erfolgen. selectTrack(1) erhält
            # die vorhandene Wiedergabeposition.
            if service_count > 1 and selected_service_index >= 0:
                jd_log(
                    "Audio change without stream restart: local service track %s is available"                    % selected_service_index,
                    level="DEBUG"
                )
                return False

            # Auch bei genau passender Anzahl von Meta- und Service-Tracks ist
            # die lokale Auswahl sicherer als ein Wechsel zu stream.ts.
            if (
                service_count > 0
                and meta_count > 0
                and service_count >= meta_count
                and selected_service_index >= 0
            ):
                return False

            # Nur ein Streamtrack trotz mehrerer Jellyfin-Audiospuren bedeutet:
            # Jellyfin liefert bereits einen Single-Track-Remux. Hier muss für
            # einen Spurwechsel ein neuer Jellyfin-Stream aufgebaut werden.
            if meta_count > 1 and service_count <= 1:
                return True

            # Keine lokale Zuordnung vorhanden: Neustart erforderlich, sofern
            # die Jellyfin-Spur überhaupt bekannt ist.
            if (
                selected_jellyfin_index is not None
                and selected_service_index < 0
            ):
                return True

            # Kann der Service die gewünschte Spur nicht darstellen, ist ein
            # Jellyfin-Neustart der letzte Fallback.
            if service_count == 0 and selected_jellyfin_index is not None:
                return True

            return False

        except Exception as e:
            jd_log(
                "Error in shouldRestartStreamForAudioSelection: %s" % e,
                level="WARNING"
            )
            return True

    def audioTrackNeedsRestart(self, track):
        try:
            if not track or not isinstance(track, dict):
                return False

            jellyfin_index = track.get("jellyfin_index", None)
            codec = str(track.get("codec") or track.get("codec_display") or "").strip().lower()

            if jellyfin_index is None:
                return False

            if self.shouldRestartStreamForAudioSelection(track):
                return True

            if not codec:
                return True

            return False
        except Exception as e:
            jd_log(f"Error in audioTrackNeedsRestart: {e}", level="WARNING")
            return True

    def verifyAudioRestartPosition(self):
        """
        Prüft nach dem Audio-Neustart die tatsächlich vom Player gemeldete
        Position.

        Wichtig: Hier darf nicht estimatedPositionSeconds() verwendet werden,
        weil diese Methode während eines Resume-Seeks absichtlich auf die
        gemerkte Zielposition zurückfallen kann.
        """
        try:
            target = int(
                getattr(
                    self.player,
                    "_resume_target_seconds",
                    0
                ) or 0
            )

            if target <= 0:
                return

            raw_current = self.player.playback.get_raw_position_seconds()

            jd_log(
                "Audio-restart position check (raw): current=%ss, "
                "target=%ss"
                % (
                    raw_current
                    if raw_current is not None
                    else "unknown",
                    target
                ),
                level="DEBUG"
            )

            # Nur eine echte, rohe Playerposition darf den Resume-Seek
            # bestätigen.
            if (
                raw_current is not None
                and abs(int(raw_current) - target) <= 20
            ):
                self.player.internal_ram_seconds = int(raw_current)
                self.player.last_known_position_seconds = int(raw_current)
                self.player._resume_target_seconds = 0
                self.player._audio_restart_position_confirmed = True

                jd_log(
                    "Audio-restart resume seek confirmed: position=%ss, target=%ss"
                    % (
                        raw_current,
                        target
                    ),
                    level="INFO"
                )

                self.player.updateCodecAndLanguageText()
                self.player.refreshInfobarData()
                return

            retry_count = int(
                getattr(
                    self.player,
                    "_audio_restart_reseek_attempts",
                    0
                ) or 0
            ) + 1

            self.player._audio_restart_reseek_attempts = retry_count
            self.player._audio_restart_position_confirmed = False

            if retry_count <= 8:
                jd_log(
                    "Audio-restart resume seek is not effective yet -> seeking again: attempt=%s, raw_current=%s, target=%s"
                    % (
                        retry_count,
                        raw_current,
                        target
                    ),
                    level="WARNING"
                )

                self.player.internal_ram_seconds = target
                self.player.last_known_position_seconds = target
                self.player.seekToPosition(target)

                jellydream_restart_single_timer(
                    self.player,
                    "_audio_restart_verify_timer",
                    self.verifyAudioRestartPosition,
                    900
                )

                return

            jd_log(
                "Audio-restart resume seek could not be confirmed from the raw position after %s attempts. The target position is kept."                % retry_count,
                level="ERROR"
            )

        except Exception as e:
            jd_log(
                "Error in verifyAudioRestartPosition: %s" % e,
                level="WARNING"
            )

    def clearResumeTargetSeconds(self):
        """
        Löscht die Schutzposition erst, wenn der Player die Zielposition
        tatsächlich erreicht hat.

        Vorher wurde das Ziel nach fünf Sekunden blind gelöscht. Wenn
        ExtePlayer3 bzw. stream.ts den ersten Seek ignoriert, fällt die
        Wiedergabe anschließend sichtbar auf Position 0 zurück.
        """
        try:
            target = int(
                getattr(
                    self.player,
                    "_resume_target_seconds",
                    0
                ) or 0
            )

            if target <= 0:
                return

            raw_current = self.player.playback.get_raw_position_seconds()

            if (
                raw_current is not None
                and abs(int(raw_current) - target) <= 20
            ):
                self.player._resume_target_seconds = 0
                self.player._audio_restart_position_confirmed = True

                jd_log(
                    "Audio-restart guard position cleared after raw check: position=%ss, target=%ss"                    % (
                        raw_current,
                        target
                    ),
                    level="DEBUG"
                )

                return

            retry_count = int(
                getattr(
                    self.player,
                    "_audio_restart_reseek_attempts",
                    0
                ) or 0
            )

            if retry_count < 8:
                jd_log(
                    "Audio-restart guard position stays active: raw_current=%s, target=%s"
                    % (
                        raw_current,
                        target
                    ),
                    level="DEBUG"
                )

                self.player.seekToPosition(target)

                try:
                    self.player.audioRestartClearTimer.start(
                        1200,
                        True
                    )
                except Exception:
                    pass

                return

            jd_log(
                "Audio-restart guard position cleared after failed reseek attempts",
                level="WARNING"
            )

            self.player._resume_target_seconds = 0

        except Exception as e:
            jd_log(
                "Error in clearResumeTargetSeconds: %s" % e,
                level="WARNING"
            )

    def applyPendingAudioSelection(self):
        try:
            pending_jellyfin_index = getattr(self, "_pending_restart_jellyfin_audio_index", None)
            pending_service_index = int(getattr(self, "_pending_audio_track_index", -1))

            jd_log(
                f"Delayed audio apply: jellyfin_index={pending_jellyfin_index}, service_index={pending_service_index}",
                level="DEBUG"
            )

            tracks = self.buildAudioTrackMenuData()

            if pending_jellyfin_index is not None:
                for track in tracks:
                    try:
                        if int(track.get("jellyfin_index", -9999)) == int(pending_jellyfin_index):
                            service_index = int(track.get("service_index", -1))
                            if service_index >= 0:
                                if self.applyAudioTrackByIndex(service_index):
                                    self._forced_audio_track_index = service_index
                                    self._pending_audio_track_index = service_index
                                    self._pending_restart_jellyfin_audio_index = None

                                    codec = str(track.get("codec_display") or track.get("codec") or "").strip().upper()
                                    if codec:
                                        self.player.a_codec = codec

                                    jd_log(
                                        f"Delayed audio track set: service_index={service_index}, jellyfin_index={pending_jellyfin_index}",
                                        level="DEBUG"
                                    )
                                    self.delayedAudioInfoRefresh()
                                    return
                    except Exception:
                        pass

            if pending_service_index >= 0:
                if self.applyAudioTrackByIndex(pending_service_index):
                    self._forced_audio_track_index = pending_service_index
                    self._pending_restart_jellyfin_audio_index = None
                    jd_log(
                        f"Delayed audio track set via service_index: {pending_service_index}",
                        level="DEBUG"
                    )
                    self.delayedAudioInfoRefresh()
                    return

        except Exception as e:
            jd_log(f"Error in applyPendingAudioSelection: {e}", level="WARNING")

    def restartStreamWithSelectedAudio(
        self,
        jellyfin_audio_stream_index,
        selected_track=None,
        subtitle_stream_index=None,
        subtitle_selection_changed=False
    ):
        """
        Startet den Stream mit einer neuen Audiospur erneut.

        Wichtig:
        Der Neustart verwendet nicht mehr die beim ursprünglichen Start
        zwischengespeicherte MediaSource und PlaySessionId. Stattdessen wird
        vor jedem Audio-Neustart erneut /Items/{itemId}/PlaybackInfo abgefragt.

        Dadurch kann Jellyfin für die gewählte Audiospur erneut entscheiden,
        ob Direct Play, Direct Stream / Remux oder Transcoding nötig ist.
        """
        try:
            if jellyfin_audio_stream_index is not None:
                try:
                    jellyfin_audio_stream_index = int(
                        jellyfin_audio_stream_index
                    )
                except Exception:
                    jellyfin_audio_stream_index = None

            if subtitle_selection_changed:
                try:
                    effective_subtitle_stream_index = int(
                        subtitle_stream_index
                    )
                except Exception:
                    effective_subtitle_stream_index = -1
            else:
                current_subtitle_index = getattr(
                    self,
                    "selected_jellyfin_subtitle_stream_index",
                    None
                )

                if current_subtitle_index is None:
                    effective_subtitle_stream_index = -1
                else:
                    try:
                        effective_subtitle_stream_index = int(
                            current_subtitle_index
                        )
                    except Exception:
                        effective_subtitle_stream_index = -1

            if self._audio_restart_in_progress:
                jd_log(
                    "Audio restart ignored: a previous restart is still being prepared",
                    level="WARNING"
                )
                return False

            if not self.player.item_id:
                jd_log(
                    "Audio restart not possible: item id is missing",
                    level="ERROR"
                )
                return False

            # --------------------------------------------------------------
            # Diese Werte müssen im GUI-Thread erfasst werden.
            # --------------------------------------------------------------
            current_pos = self.player.estimatedPositionSeconds()

            if current_pos <= 0:
                current_pos = int(
                    getattr(
                        self.player,
                        "internal_ram_seconds",
                        0
                    ) or 0
                )

            if current_pos <= 0:
                current_pos = int(
                    getattr(
                        self.player,
                        "last_known_position_seconds",
                        0
                    ) or 0
                )

            current_pos = max(0, int(current_pos))

            old_play_session_id = str(
                getattr(
                    self.player,
                    "play_session_id",
                    ""
                ) or ""
            ).strip()

            old_media_source_id = str(
                getattr(
                    self.player,
                    "media_source_id",
                    self.player.item_id
                ) or self.player.item_id
            ).strip()

            old_stream_url = str(
                getattr(
                    self.player,
                    "current_stream_url",
                    ""
                ) or ""
            ).strip()

            # Die vollständigen Detaildaten bleiben erhalten. PlaybackInfo
            # ersetzt später nur die verbindlich gewählte MediaSource.
            previous_item_details = copy.deepcopy(
                getattr(
                    self.player,
                    "item_details",
                    {}
                ) or {}
            )

            auth_snapshot = jellydream_auth_snapshot()

            self._audio_restart_in_progress = True
            self._audio_restart_token += 1

            restart_token = self._audio_restart_token

            self.player.internal_ram_seconds = current_pos
            self.player.last_known_position_seconds = current_pos
            self.player._resume_target_seconds = current_pos
            self.player._resume_retry_count = 0

            # Separater Status für den Audio-Neustart. Dieser darf nicht von
            # vorherigen Resume-/Audio-Wechseln übernommen werden.
            self.player._audio_restart_reseek_attempts = 0
            self.player._audio_restart_position_confirmed = False

            jd_log(
                "Audio restart is being prepared via PlaybackInfo: item=%s, requested AudioStreamIndex=%s, position=%ss"                % (
                    self.player.item_id,
                    jellyfin_audio_stream_index,
                    current_pos
                ),
                level="INFO"
            )

            def finish_with_error(error_message):
                self._audio_restart_in_progress = False

                error_message = str(
                    error_message
                    or _(
                        "Audio-Neustart konnte nicht vorbereitet werden."
                    )
                )

                jd_log(
                    "Audio restart via PlaybackInfo failed: %s"                    % error_message,
                    level="ERROR"
                )

                try:
                    if getattr(self.player, "instance", None):
                        self.player.session.open(
                            MessageBox,
                            error_message,
                            MessageBox.TYPE_ERROR,
                            timeout=5
                        )
                except Exception:
                    pass

            def worker():
                try:
                    # ------------------------------------------------------
                    # Frische serverseitige Wiedergabeentscheidung abrufen.
                    # ------------------------------------------------------
                    playback_context = jellyfin_get_playback_info(
                        self.player.item_id,
                        start_position_seconds=current_pos,
                        preferred_media_source_id=old_media_source_id,
                        raise_on_error=True,
                        auth_snapshot=auth_snapshot
                    )

                    selected_source = playback_context.get(
                        "media_source"
                    ) or {}

                    selected_source_id = str(
                        playback_context.get(
                            "media_source_id"
                        ) or ""
                    ).strip()

                    new_play_session_id = str(
                        playback_context.get(
                            "play_session_id"
                        ) or ""
                    ).strip()

                    if not selected_source:
                        raise JellyfinRequestError(
                            "Jellyfin lieferte beim Audio-Neustart "
                            "keine verwendbare Medienquelle."
                        )

                    if not selected_source_id:
                        raise JellyfinRequestError(
                            "Jellyfin lieferte beim Audio-Neustart "
                            "keine MediaSource-ID."
                        )

                    if not new_play_session_id:
                        raise JellyfinRequestError(
                            "Jellyfin lieferte beim Audio-Neustart "
                            "keine PlaySessionId."
                        )

                    # Die ursprünglichen Metadaten wie Titel, Laufzeit,
                    # Serieninformationen und Overview bleiben erhalten.
                    # Ausschließlich die aktuelle PlaybackInfo-MediaSource
                    # wird ersetzt.
                    prepared_details = dict(
                        previous_item_details or {}
                    )

                    prepared_details["MediaSources"] = [
                        selected_source
                    ]

                    prepared_details["_jellydream_media_source_id"] = (
                        selected_source_id
                    )

                    prepared_details["_jellydream_play_session_id"] = (
                        new_play_session_id
                    )

                    # Die neue URL wird mit der FRISCHEN MediaSource und
                    # FRISCHEN PlaySessionId gebaut.
                    new_url = build_jellyfin_stream(
                        self.player.item_id,
                        audio_stream_index=jellyfin_audio_stream_index,
                        subtitle_stream_index=effective_subtitle_stream_index,
                        start_position_seconds=current_pos,
                        preloaded_details=prepared_details,
                        play_session_id=new_play_session_id,
                        preferred_media_source_id=selected_source_id
                    )

                    if not new_url:
                        raise JellyfinRequestError(
                            _(
                                "Die neue Jellyfin-Stream-URL konnte nicht "
                                "erzeugt werden."
                            )
                        )

                    # Metadaten für Audio-Menü und Infobar aus der neuen,
                    # serverseitig über PlaybackInfo gelieferten MediaSource
                    # vorbereiten.
                    refreshed_stream_data = JellyDreamStreamInfoLoader(
                        self.player.item_id,
                        preloaded_details=prepared_details
                    ).load()

                    def apply_restart():
                        try:
                            # Ein späteres Ergebnis eines alten Jobs darf keinen
                            # inzwischen neueren Audio-Neustart überschreiben.
                            if restart_token != self._audio_restart_token:
                                return

                            self._audio_restart_in_progress = False

                            if not getattr(self.player, "instance", None):
                                return

                            if not jellydream_auth_snapshot_matches_current(
                                auth_snapshot
                            ):
                                finish_with_error(
                                    "Die Jellyfin-Anmeldung wurde während "
                                    "des Audio-Neustarts geändert."
                                )
                                return

                            # Den bisherigen Jellyfin-Playback-Lauf sauber
                            # beenden, bevor mit der neuen PlaySession gestartet
                            # wird.
                            if old_play_session_id:
                                jellyfin_report_stop(
                                    item_id=self.player.item_id,
                                    position_seconds=current_pos,
                                    title=self.player.title,
                                    media_source_id=old_media_source_id,
                                    play_session_id=old_play_session_id,
                                    is_paused=self.player.paused,
                                    stream_url=old_stream_url
                                )

                            # --------------------------------------------------
                            # Neue PlaybackInfo-Daten verbindlich übernehmen.
                            # --------------------------------------------------
                            self.player.item_details = prepared_details
                            self.player.stream_data = refreshed_stream_data
                            self.player.media_source_id = selected_source_id
                            self.player.play_session_id = new_play_session_id
                            self.player.current_stream_url = new_url

                            video_meta = refreshed_stream_data.get(
                                "video_meta",
                                {}
                            ) or {}

                            if video_meta.get("codec"):
                                self.player.v_codec = str(
                                    video_meta.get("codec")
                                ).upper()

                            if video_meta.get("resolution"):
                                self.player.res = str(
                                    video_meta.get("resolution")
                                )

                            audio_meta = refreshed_stream_data.get(
                                "audio_meta",
                                {}
                            ) or {}

                            self.audio_tracks_cache = audio_meta.get(
                                "streams",
                                []
                            ) or []

                            subtitle_meta = refreshed_stream_data.get(
                                "subtitle_meta",
                                {}
                            ) or {}

                            self.subtitle_tracks_cache = subtitle_meta.get(
                                "streams",
                                []
                            ) or []

                            if effective_subtitle_stream_index >= 0:
                                self.selected_jellyfin_subtitle_stream_index = (
                                    effective_subtitle_stream_index
                                )
                            else:
                                self.selected_jellyfin_subtitle_stream_index = None
    
                            if jellyfin_audio_stream_index is not None:
                                self.selected_jellyfin_audio_stream_index = (
                                    jellyfin_audio_stream_index
                                )

                            self._pending_audio_track_index = -1
                            self._forced_audio_track_index = -1

                            self._pending_restart_jellyfin_audio_index = (
                                jellyfin_audio_stream_index
                            )

                            if (
                                selected_track
                                and isinstance(selected_track, dict)
                            ):
                                codec = str(
                                    selected_track.get(
                                        "codec_display"
                                    )
                                    or selected_track.get(
                                        "codec"
                                    )
                                    or ""
                                ).strip().upper()

                                if codec:
                                    self.player.a_codec = codec

                            service_reference = (
                                jellydream_get_service_reference()
                            )

                            self.player.current_service_reference = (
                                service_reference
                            )

                            # Der Audio-Neustart benötigt dieselben
                            # Authentifizierungsheader wie der erste Start.
                            #
                            # `new_url` bleibt intern ohne Enigma2-Headersuffix
                            # gespeichert.
                            player_url = jellydream_build_authenticated_player_url(
                                new_url,
                                auth_snapshot=auth_snapshot
                            )

                            ref = eServiceReference(
                                service_reference,
                                0,
                                player_url
                            )

                            ref.setName(self.player.title)

                            jd_log(
                                "Stream restart starts a new PlaybackInfo session: "
                                "item=%s, MediaSource=%s, PlaySession=%s, "
                                "AudioStreamIndex=%s, SubtitleStreamIndex=%s"
                                % (
                                    self.player.item_id,
                                    selected_source_id,
                                    new_play_session_id,
                                    jellyfin_audio_stream_index,
                                    (
                                        effective_subtitle_stream_index
                                        if effective_subtitle_stream_index >= 0
                                        else "aus"
                                    )
                                ),
                                level="INFO"
                            )

                            try:
                                self.player._ignore_eof = True

                                try:
                                    self.player.session.nav.stopService()

                                    jd_log(
                                        "Previous service stopped before the audio restart",
                                        level="DEBUG"
                                    )

                                except Exception as stop_error:
                                    jd_log(
                                        "Previous service could not be stopped before the audio restart: %s"
                                        % stop_error,
                                        level="WARNING"
                                    )

                                self.player.session.nav.playService(ref)

                                jellydream_restart_single_timer(
                                    self.player,
                                    "_audio_eof_guard_timer",
                                    self.player.clearAudioEOFGuard,
                                    2000
                                )

                            except Exception as start_error:
                                self.player._ignore_eof = False

                                finish_with_error(
                                    _(
                                        "Der neue Audio-Stream konnte nicht "
                                        "gestartet werden: %s"
                                    )
                                    % start_error
                                )
                                return

                            # Der neue Lauf erhält seine neue PlaySessionId.
                            jellyfin_report_start(
                                item_id=self.player.item_id,
                                title=self.player.title,
                                position_seconds=current_pos,
                                play_session_id=new_play_session_id,
                                media_source_id=selected_source_id,
                                stream_url=new_url
                            )

                            def delayed_reseek():
                                try:
                                    target = int(
                                        getattr(
                                            self.player,
                                            "_resume_target_seconds",
                                            0
                                        ) or 0
                                    )

                                    if target > 0:
                                        self.player.internal_ram_seconds = (
                                            target
                                        )
                                        self.player.last_known_position_seconds = (
                                            target
                                        )

                                    current_after = (
                                        self.player.estimatedPositionSeconds()
                                    )

                                    if (
                                        target > 0
                                        and (
                                            current_after <= 0
                                            or abs(
                                                int(current_after) - target
                                            ) > 15
                                        )
                                    ):
                                        self.player.seekToPosition(target)

                                    self.buildAudioTrackMenuData()

                                    self.player.updateCodecAndLanguageText()
                                    self.player.refreshInfobarData()
                                    self.player.showInfobar()

                                    jellydream_restart_single_timer(
                                        self.player,
                                        "_audio_restart_verify_timer",
                                        self.verifyAudioRestartPosition,
                                        self.player.AUDIO_RESTART_VERIFY_DELAY_MS
                                    )

                                except Exception as reseek_error:
                                    jd_log(
                                        "Error during reseek after audio restart: %s"
                                        % reseek_error,
                                        level="WARNING"
                                    )

                            jellydream_restart_single_timer(
                                self.player,
                                "_audio_restart_seek_timer",
                                delayed_reseek,
                                self.player.AUDIO_RESTART_RESEEK_DELAY_MS
                            )

                            try:
                                self.player.audioRestartClearTimer.stop()
                            except Exception:
                                pass

                            try:
                                self.player.audioApplyTimer.stop()
                            except Exception:
                                pass

                            try:
                                self.player.audioApplyTimer2.stop()
                            except Exception:
                                pass

                            self.player.audioApplyTimer.start(
                                self.player.AUDIO_REAPPLY_DELAY_1_MS,
                                True
                            )

                            self.player.audioApplyTimer2.start(
                                self.player.AUDIO_REAPPLY_DELAY_2_MS,
                                True
                            )

                            self.player.audioRestartClearTimer.start(
                                self.player.AUDIO_CLEAR_RESUME_DELAY_MS,
                                True
                            )

                            jd_log(
                                "Audio restart via PlaybackInfo succeeded: AudioStreamIndex=%s, position=%ss"
                                % (
                                    jellyfin_audio_stream_index,
                                    current_pos
                                ),
                                level="INFO"
                            )

                        except Exception as gui_error:
                            finish_with_error(
                                _(
                                    "Audio-Neustart konnte nicht angewendet "
                                    "werden: %s"
                                )
                                % gui_error
                            )

                    if not jellydream_dispatch_to_gui(apply_restart):
                        raise JellyfinRequestError(
                            _(
                                "Der vorbereitete Audio-Neustart konnte nicht "
                                "an die Benutzeroberfläche übergeben werden."
                            )
                        )

                except JellyfinRequestError as error:
                    jellydream_dispatch_to_gui(
                        lambda message=str(error): finish_with_error(
                            message
                        )
                    )

                except Exception as error:
                    jd_log(
                        "Background audio restart failed: %s"
                        % error,
                        level="ERROR"
                    )

                    jellydream_dispatch_to_gui(
                        lambda: finish_with_error(
                            "Wiedergabeinformationen für die neue "
                            "Audiospur konnten nicht geladen werden."
                        )
                    )

            started = jellydream_submit_playback(worker)

            if not started:
                self._audio_restart_in_progress = False

                jd_log(
                    "Audio restart was rejected by the playback worker pool",
                    level="WARNING"
                )

                return False

            return True

        except Exception as e:
            self._audio_restart_in_progress = False

            jd_log(
                "Error in restartStreamWithSelectedAudio: %s" % e,
                level="ERROR"
            )

            return False

    def buildSubtitleTrackMenuData(self):
        result = []

        try:
            selected_index = getattr(
                self,
                "selected_jellyfin_subtitle_stream_index",
                None
            )

            # Erster Eintrag schaltet Untertitel vollständig aus.
            result.append({
                "kind": "subtitle",
                "service_index": -1,
                "jellyfin_index": None,
                "language": "",
                "language_display": "",
                "description": "",
                "codec": "",
                "codec_display": "",
                "disabled": True,
                "forced": False,
                "hearing_impaired": False,
                "external": False,
                "selected": selected_index is None
            })

            for meta in self.subtitle_tracks_cache or []:
                if not isinstance(meta, dict):
                    continue

                jellyfin_index = meta.get(
                    "JellyfinIndex",
                    meta.get("Index")
                )

                try:
                    jellyfin_index = int(
                        jellyfin_index
                )
                except Exception:
                    continue

                raw_language = str(
                    meta.get("DisplayLanguage")
                    or meta.get("Language")
                    or ""
                ).strip()

                language_display = (
                    self.normalizeAudioLanguage(
                        raw_language
                    )
                )

                description = str(
                    meta.get("Title")
                    or meta.get("DisplayTitle")
                    or ""
                ).strip()

                codec = str(
                    meta.get("Codec")
                    or ""
                ).strip().upper()

                is_selected = False

                if selected_index is not None:
                    try:
                        is_selected = (
                            int(selected_index)
                            == jellyfin_index
                        )
                    except Exception:
                        is_selected = False

                result.append({
                    "kind": "subtitle",
                    "service_index": -1,
                    "jellyfin_index": jellyfin_index,
                    "language": raw_language,
                    "language_display": language_display,
                    "description": description,
                    "codec": codec,
                    "codec_display": codec,
                    "disabled": False,
                    "forced": bool(
                        meta.get("IsForced", False)
                    ),
                    "hearing_impaired": bool(
                        meta.get("IsHearingImpaired", False)
                    ),
                    "external": bool(
                        meta.get("IsExternal", False)
                    ),
                    "selected": is_selected
                })

            jd_log(
                "Subtitle menu built: %s track(s)"                % len(self.subtitle_tracks_cache or []),
                level="DEBUG"
            )

        except Exception as e:
            jd_log(
                "Subtitle menu could not be built: %s"                % e,
                level="WARNING"
            )

        return result


    def buildAudioAndSubtitleMenuData(self):
        result = []

        try:
            audio_tracks = self.buildAudioTrackMenuData()

            for track in audio_tracks:
                if not isinstance(track, dict):
                    continue

                menu_track = dict(track)
                menu_track["kind"] = "audio"

                result.append(
                    menu_track
                )

            result.extend(
                self.buildSubtitleTrackMenuData()
            )

        except Exception as e:
            jd_log(
                "Audio/subtitle menu could not be built: %s"                % e,
                level="WARNING"
            )

        return result


    def getAudioIndexForStreamRestart(self):
        try:
            audio_index = self.getTargetJellyfinAudioIndex()

            if audio_index is not None:
                return int(audio_index)

            preferred = self.getPreferredAudioTrackData()

            if isinstance(preferred, dict):
                preferred_index = preferred.get(
                    "jellyfin_index"
                )

                if preferred_index is not None:
                    return int(preferred_index)

            for meta in self.audio_tracks_cache or []:
                if not isinstance(meta, dict):
                    continue

                meta_index = meta.get(
                    "JellyfinIndex",
                    meta.get("Index")
                )

                if meta_index is not None:
                    return int(meta_index)

        except Exception as e:
            jd_log(
                "Audio index for the subtitle restart could not be resolved: %s"
                % e,
                level="WARNING"
            )

        return None

    def onJellyDreamMediaTrackSelected(self, track):
        try:
            if not isinstance(track, dict):
                return

            track_kind = str(
                track.get("kind")
                or "audio"
            ).strip().lower()

            if track_kind == "subtitle":
                self.onJellyDreamSubtitleTrackSelected(
                    track
                )
                return

            self.onJellyDreamAudioTrackSelected(
                track
            )

        except Exception as e:
            jd_log(
                "Audio/subtitle selection failed: %s"
                % e,
                level="WARNING"
            )

    def onJellyDreamSubtitleTrackSelected(self, track):
        try:
            if not isinstance(track, dict):
                return

            subtitle_disabled = bool(
                track.get("disabled", False)
            )

            if subtitle_disabled:
                subtitle_index = -1
            else:
                subtitle_index = track.get(
                    "jellyfin_index"
                )

                if subtitle_index is None:
                    return

                subtitle_index = int(
                    subtitle_index
                )

            current_subtitle_index = getattr(
                self,
                "selected_jellyfin_subtitle_stream_index",
                None
            )

            if subtitle_disabled and current_subtitle_index is None:
                jd_log(
                    "Subtitles are already disabled",
                    level="DEBUG"
                )
                return

            if (
                not subtitle_disabled
                and current_subtitle_index is not None
                and int(current_subtitle_index) == subtitle_index
            ):
                jd_log(
                    "Selected subtitle track is already active: %s"                    % subtitle_index,
                    level="DEBUG"
                )
                return

            audio_index = self.getAudioIndexForStreamRestart()

            jd_log(
                "Untertitelauswahl: SubtitleStreamIndex=%s, "
                "AudioStreamIndex=%s"
                % (
                    subtitle_index,
                    audio_index
                ),
                level="INFO"
            )

            success = self.restartStreamWithSelectedAudio(
                audio_index,
                selected_track=None,
                subtitle_stream_index=subtitle_index,
                subtitle_selection_changed=True
            )

            if not success:
                self.player.session.open(
                    MessageBox,
                    _("Die Untertitelspur konnte nicht aktiviert werden."),
                    MessageBox.TYPE_ERROR,
                    timeout=4
                )

        except Exception as e:
            jd_log(
                "Subtitles could not be selected: %s"                % e,
                level="WARNING"
            )
        
    def openJellyfinAudioMenu(self):
        try:
            self.player.showInfobar()

            self._audio_track_before_menu = (
                self.getCurrentAudioTrackIndex()
            )

            self._audio_refresh_retry = 0

            try:
                self.player.hide_timer.stop()
            except Exception:
                pass

            tracks = self.buildAudioAndSubtitleMenuData()

            preferred = self.getPreferredAudioTrackData()
            current_index = -1

            if preferred:
                try:
                    current_index = int(
                        preferred.get(
                            "service_index",
                            -1
                        )
                    )
                except Exception:
                    current_index = -1

            if current_index < 0:
                current_index = (
                    self.getCurrentAudioTrackIndex()
                )

            if not tracks:
                self.player.session.open(
                    MessageBox,
                    _("Keine Ton- oder Untertitelspuren gefunden."),
                    MessageBox.TYPE_INFO,
                    timeout=3
                )

                try:
                    self.player.hide_timer.start(
                        5000,
                        True
                    )
                except Exception:
                    pass

                return

            self.player.session.openWithCallback(
                self.audioSelectionClosed,
                JellyDreamAudioMenu,
                tracks,
                current_index,
                self.onJellyDreamMediaTrackSelected
            )

        except Exception as e:
            jd_log(
                "Audio/subtitle menu could not be opened: %s"               % e,
                level="WARNING"
            )

            try:
                self.player.hide_timer.start(
                    5000,
                    True
                )
            except Exception:
                pass

    def onJellyDreamAudioTrackSelected(self, track):
        try:
            if not track or not isinstance(track, dict):
                return

            service_index = int(track.get("service_index", -1))
            jellyfin_index = track.get("jellyfin_index", None)
            if jellyfin_index is not None:
                try:
                    jellyfin_index = int(jellyfin_index)
                except Exception:
                    jellyfin_index = None
                    
            restart_required = self.audioTrackNeedsRestart(track)

            # Wenn Enigma2 die Zielspur direkt anbietet, niemals auf stream.ts
            # umschalten. Der lokale selectTrack()-Wechsel erhält die Position.
            if service_index >= 0 and len(self.getServiceAudioTracks()) > 1:
                restart_required = False
    
            jd_log(
                f"Audio selection: service_index={service_index}, jellyfin_index={jellyfin_index}, restart_required={restart_required}",
                level="DEBUG"
            )

            if restart_required:
                if jellyfin_index is None:
                    jd_log("Audio restart not possible: jellyfin_index is missing", level="WARNING")
                    return

                success = self.restartStreamWithSelectedAudio(jellyfin_index, selected_track=track)
                if not success:
                    jd_log("Audio restart with the selected track failed", level="WARNING")
                    return

                return

            if service_index < 0:
                return

            success = self.applyAudioTrackByIndex(service_index)
            if not success:
                return

            self._pending_audio_track_index = service_index
            self._forced_audio_track_index = -1

            # Die lokale Enigma2-Auswahl ist erfolgreich angestoßen worden.
            # Den Jellyfin-Status sofort synchronisieren, damit das Audio-Menü
            # nicht weiterhin die vorherige Sprache als ausgewählt markiert.
            if jellyfin_index is not None:
                self.selected_jellyfin_audio_stream_index = jellyfin_index

            codec = str(track.get("codec_display") or track.get("codec") or "").strip().upper()
            if codec:
                self.player.a_codec = codec

            self.player.updateCodecAndLanguageText()
            self.player.refreshInfobarData()

        except Exception as e:
            jd_log(f"Error in onJellyDreamAudioTrackSelected: {e}", level="WARNING")

    def audioSelectionClosed(self, selected_track=None):
        try:
            self.player.showInfobar()
            self._audio_refresh_retry = 0

            if int(getattr(self, "_pending_audio_track_index", -1)) >= 0:
                jellydream_restart_single_timer(
                    self.player,
                    "_audio_refresh_timer",
                    self.pollAudioTrackChange,
                    self.player.AUDIO_POLL_DELAY_MS
                )

            self.player.hide_timer.start(5000, True)
        except Exception as e:
            jd_log(f"Error in audioSelectionClosed: {e}", level="WARNING")

    def pollAudioTrackChange(self):
        try:
            old_idx = int(getattr(self, "_audio_track_before_menu", -1))
            pending_idx = int(getattr(self, "_pending_audio_track_index", -1))
            new_idx = self.getCurrentAudioTrackIndex()
            self._audio_refresh_retry = int(getattr(self, "_audio_refresh_retry", 0)) + 1

            jd_log(
                f"Audio track check: previous={old_idx}, pending={pending_idx}, current={new_idx}, attempt={self._audio_refresh_retry}",
                level="DEBUG"
            )

            if new_idx >= 0 and pending_idx >= 0 and new_idx == pending_idx:
                jd_log("Audio track change confirmed by the service", level="DEBUG")
                self._forced_audio_track_index = new_idx
                self._pending_audio_track_index = -1
                self.buildAudioTrackMenuData()
                self.delayedAudioInfoRefresh()
                self._audio_track_before_menu = new_idx
                return

            if new_idx >= 0 and pending_idx < 0 and new_idx != old_idx:
                jd_log("Audio track change detected", level="DEBUG")
                self._forced_audio_track_index = new_idx
                self.buildAudioTrackMenuData()
                self.delayedAudioInfoRefresh()
                self._audio_track_before_menu = new_idx
                return

            if self._audio_refresh_retry < self.player.AUDIO_POLL_RETRY_MAX:
                jellydream_restart_single_timer(
                    self.player,
                    "_audio_refresh_timer",
                    self.pollAudioTrackChange,
                    self.player.AUDIO_POLL_DELAY_MS
                )
                return

            jd_log("Service did not report the track change in time -> UI stays on the selected target track", level="DEBUG")
            if pending_idx >= 0:
                self._forced_audio_track_index = pending_idx
            self._pending_audio_track_index = -1
            self.buildAudioTrackMenuData()
            self.delayedAudioInfoRefresh()

        except Exception as e:
            jd_log(f"Error in pollAudioTrackChange: {e}", level="WARNING")
            try:
                self.delayedAudioInfoRefresh()
            except Exception:
                pass


class JellyDreamStreamInfoLoader:
    def __init__(self, item_id, preloaded_details=None):
        self.item_id = str(item_id or "").strip()
        self.preloaded_details = preloaded_details if isinstance(preloaded_details, dict) else {}
        self.item_details = {}
        self.cover_id = ""
        self.video_meta = {
            "codec": "",
            "width": 0,
            "height": 0,
            "resolution": "",
        }
        self.audio_meta = {
            "streams": [],
            "codec": "",
            "selected_index": None,
            "jellyfin_audio_stream_index": None,
        }

        self.subtitle_meta = {
            "streams": [],
            "selected_index": None,
            "jellyfin_subtitle_stream_index": None,
        }

    def load(self):
        try:
            if not self.item_id:
                return self.get_result()

            details = self.preloaded_details or {}

            if not details:
                if jellydream_is_gui_thread():
                    details = jellydream_cache_get(
                        _GLOBAL_DETAILS_CACHE,
                        self.item_id,
                        {}
                    ) or {}

                    if not details:
                        jellydream_load_item_details_async(
                            self.item_id
                        )
                else:
                    details = jellyfin_get_item_details(
                        self.item_id
                    ) or {}
            if not details or not isinstance(details, dict):
                return self.get_result()

            self.item_details = details
            self._map_cover_id()
            self._map_media_meta()
            return self.get_result()
        except Exception as e:
            jd_log(f"Error in JellyDreamStreamInfoLoader.load: {e}", level="WARNING")
            return self.get_result()

    def _map_cover_id(self):
        try:
            item_type = self.item_details.get("Type") or ""
            if item_type == "Episode":
                self.cover_id = str(
                    self.item_details.get("SeriesId")
                    or self.item_details.get("SeasonId")
                    or self.item_details.get("ParentId")
                    or self.item_id
                ).strip()
            elif item_type == "Audio":
                self.cover_id = str(
                    self.item_details.get("AlbumId")
                    or self.item_details.get("ParentId")
                    or self.item_id
                ).strip()
            else:
                self.cover_id = self.item_id
        except Exception:
            self.cover_id = self.item_id

    def _map_media_meta(self):
        try:
            sources = self.item_details.get("MediaSources") or []
            if not isinstance(sources, list) or not sources:
                return

            first_source = sources[0] if isinstance(sources[0], dict) else {}
            streams = first_source.get("MediaStreams") or []

            self._map_video_meta(
                first_source,
                streams
            )

            self._map_audio_meta(
                streams
            )

            self._map_subtitle_meta(
                streams
            )
        except Exception as e:
            jd_log(f"Error in JellyDreamStreamInfoLoader._map_media_meta: {e}", level="WARNING")

    def _map_video_meta(self, source, streams):
        try:
            video_stream = {}

            for stream in streams:
                if not isinstance(stream, dict):
                    continue

                if str(stream.get("Type") or "").strip().lower() == "video":
                    video_stream = stream

                    self.video_meta["codec"] = str(
                        stream.get("Codec") or ""
                    ).upper().strip()

                    break

            def get_int_value(*values):
                for value in values:
                    try:
                        parsed = int(value or 0)

                        if parsed > 0:
                            return parsed
                    except Exception:
                        pass

                return 0

            # Jellyfin liefert die Werte je nach Server/Datei entweder
            # im MediaSource-Objekt oder direkt im Video-MediaStream.
            width = get_int_value(
                source.get("Width"),
                source.get("width"),
                video_stream.get("Width"),
                video_stream.get("width")
            )

            height = get_int_value(
                source.get("Height"),
                source.get("height"),
                video_stream.get("Height"),
                video_stream.get("height")
            )

            self.video_meta["width"] = width
            self.video_meta["height"] = height

            resolution_label = ""

            if width >= 3840 or height >= 2160:
                resolution_label = "4K"
            elif width >= 2560 or height >= 1440:
                resolution_label = "1440p"
            elif width >= 1920 or height >= 1080:
                resolution_label = "1080p"
            elif width >= 1280 or height >= 720:
                resolution_label = "720p"
            elif width >= 720 or height >= 576:
                resolution_label = "576p"
            elif height > 0:
                resolution_label = "%sp" % height

            if width > 0 and height > 0:
                if resolution_label:
                    self.video_meta["resolution"] = "%sx%s / %s" % (
                        width,
                        height,
                        resolution_label
                    )
                else:
                    self.video_meta["resolution"] = "%sx%s" % (
                        width,
                        height
                    )
            else:
                self.video_meta["resolution"] = resolution_label

            jd_log(
                "Video metadata: codec=%s | source=%sx%s | display=%s"
                % (
                    self.video_meta["codec"] or "-",
                    width,
                    height,
                    self.video_meta["resolution"] or "-"
                ),
                level="DEBUG"
            )

        except Exception as e:
            jd_log(
                "Error in JellyDreamStreamInfoLoader._map_video_meta: %s" % e,
                level="WARNING"
            )

    def _map_audio_meta(self, streams):
        try:
            audio_streams = []

            for stream in streams:
                if not isinstance(stream, dict):
                    continue
                if stream.get("Type") != "Audio":
                    continue

                stream_copy = dict(stream)
                try:
                    stream_copy["JellyfinIndex"] = int(stream.get("Index"))
                except Exception:
                    stream_copy["JellyfinIndex"] = len(audio_streams)

                audio_streams.append(stream_copy)

            self.audio_meta["streams"] = audio_streams

            if audio_streams:
                self.audio_meta["codec"] = str(audio_streams[0].get("Codec") or "").upper()
                self.audio_meta["jellyfin_audio_stream_index"] = audio_streams[0].get("JellyfinIndex")
        except Exception as e:
            jd_log(f"Error in JellyDreamStreamInfoLoader._map_audio_meta: {e}", level="WARNING")

    def _map_subtitle_meta(self, streams):
        try:
            subtitle_streams = []

            for stream in streams:
                if not isinstance(stream, dict):
                    continue

                stream_type = str(
                    stream.get("Type") or ""
                ).strip().lower()

                if stream_type != "subtitle":
                    continue

                stream_copy = dict(stream)

                try:
                    stream_copy["JellyfinIndex"] = int(
                        stream.get("Index")
                    )
                except Exception:
                    stream_copy["JellyfinIndex"] = len(
                        subtitle_streams
                    )

                subtitle_streams.append(
                    stream_copy
                )

            self.subtitle_meta["streams"] = (
                subtitle_streams
            )

            for subtitle_stream in subtitle_streams:
                if bool(
                    subtitle_stream.get("IsDefault", False)
                ):
                    self.subtitle_meta[
                        "jellyfin_subtitle_stream_index"
                    ] = subtitle_stream.get(
                        "JellyfinIndex"
                    )

                    break

            jd_log(
                "Subtitle metadata loaded: %s track(s)"
                % len(subtitle_streams),
                level="DEBUG"
            )

        except Exception as e:
            jd_log(
                "Error in JellyDreamStreamInfoLoader._map_subtitle_meta: %s"
                % e,
                level="WARNING"
            )
        
    def get_result(self):
        return {
            "item_details": self.item_details,
            "cover_id": self.cover_id,
            "video_meta": self.video_meta,
            "audio_meta": self.audio_meta,
            "subtitle_meta": self.subtitle_meta,
        }

		
class JellyDreamPlaybackController:
    def __init__(self, screen):
        self.screen = screen

    def get_raw_position_seconds(self):
        """
        Liest ausschließlich die echte Position des aktuell laufenden
        Enigma2-Service aus.

        Kein Fallback auf Resume-/RAM-Werte, weil diese Methode für die
        zuverlässige Kontrolle nach einem Stream- oder Audio-Neustart dient.
        """
        try:
            screen = self.screen
            service = screen.session.nav.getCurrentService()

            if not service:
                return None

            seekable = service.seek()

            if not seekable:
                return None

            pos = seekable.getPlayPosition()

            if pos is None:
                return None

            if isinstance(pos, (tuple, list)):
                if len(pos) > 1:
                    raw_ticks = pos[1]
                elif len(pos) == 1:
                    raw_ticks = pos[0]
                else:
                    return None
            else:
                raw_ticks = pos

            raw_ticks = int(raw_ticks or 0)

            if raw_ticks < 0:
                return None

            return max(
                0,
                int(raw_ticks / ENIGMA_SEEK_CLOCK)
            )

        except Exception:
            return None
        
    def get_position_seconds(self):
        try:
            screen = self.screen
            seconds = self.get_raw_position_seconds()

            if seconds is not None:
                remembered = int(
                    getattr(
                        screen,
                        "internal_ram_seconds",
                        0
                    ) or 0
                )

                target = int(
                    getattr(
                        screen,
                        "_resume_target_seconds",
                        0
                    ) or 0
                )

                # Während eines Resume-Seeks darf die Oberfläche kurzzeitig
                # die Zielposition anzeigen. Für technische Prüfungen wird
                # dagegen get_raw_position_seconds() verwendet.
                if seconds <= 2:
                    if target > 5:
                        return target

                    if remembered > 5:
                        return remembered

                screen.last_known_position_seconds = seconds
                return seconds

        except Exception:
            pass

        fallback = int(
            getattr(
                self.screen,
                "internal_ram_seconds",
                0
            ) or 0
        )

        self.screen.last_known_position_seconds = fallback
        return fallback

    def seek_to(self, seconds):
        try:
            screen = self.screen
            seconds = max(0, int(seconds or 0))
            screen.internal_ram_seconds = seconds
            screen.last_known_position_seconds = seconds

            service = screen.session.nav.getCurrentService()
            if not service:
                return False

            seekable = service.seek()
            if not seekable:
                return False

            seekable.seekTo(int(seconds * ENIGMA_SEEK_CLOCK))
            return True
        except Exception as e:
            jd_log(f"Error in JellyDreamPlaybackController.seek_to({seconds}): {e}", level="WARNING")
            return False

    def seek_relative(self, seconds):
        try:
            screen = self.screen
            delta = int(seconds or 0)
            current = self.get_position_seconds()
            if current <= 0:
                current = int(getattr(screen, "internal_ram_seconds", 0) or 0)

            new_pos = max(0, current + delta)

            total_seconds = int(getattr(screen, "runtime_seconds", 0) or 0)
            if total_seconds > 0:
                new_pos = min(new_pos, total_seconds)

            screen.internal_ram_seconds = new_pos
            screen.last_known_position_seconds = new_pos

            service = screen.session.nav.getCurrentService()
            if not service:
                return self.seek_to(new_pos)

            seekable = service.seek()
            if not seekable:
                return self.seek_to(new_pos)

            try:
                direction = 1
                amount = delta
                if amount < 0:
                    direction = -1
                    amount = -amount

                seekable.seekRelative(direction, int(amount * ENIGMA_SEEK_CLOCK))
                return True
            except Exception:
                return self.seek_to(new_pos)
        except Exception as e:
            jd_log(f"Error in JellyDreamPlaybackController.seek_relative({seconds}): {e}", level="WARNING")
            return False

    def toggle_pause(self):
        try:
            screen = self.screen
            service = screen.session.nav.getCurrentService()
            if not service:
                return

            pauseable = service.pause()
            if not pauseable:
                return

            pos = self.get_position_seconds()
            if pos <= 0:
                pos = int(getattr(screen, "internal_ram_seconds", 0) or 0)

            screen.internal_ram_seconds = int(pos)
            screen.last_known_position_seconds = int(pos)

            if screen.paused:
                pauseable.unpause()
                screen.paused = False
                screen.showInfobar()
                jellyfin_report_progress(
                    item_id=screen.item_id,
                    position_seconds=pos,
                    runtime_seconds=screen.runtime_seconds,
                    title=screen.title,
                    is_paused=False,
                    play_session_id=screen.play_session_id,
                    media_source_id=screen.media_source_id,
                    stream_url=screen.current_stream_url
                )
            else:
                pauseable.pause()
                screen.paused = True
                screen.showInfobar()
                try:
                    screen.hide_timer.stop()
                except Exception:
                    pass
                jellyfin_report_progress(
                    item_id=screen.item_id,
                    position_seconds=pos,
                    runtime_seconds=screen.runtime_seconds,
                    title=screen.title,
                    is_paused=True,
                    play_session_id=screen.play_session_id,
                    media_source_id=screen.media_source_id,
                    stream_url=screen.current_stream_url
                )
        except Exception as e:
            jd_log(f"Error in JellyDreamPlaybackController.toggle_pause: {e}", level="WARNING")

    def stop(self, playback_finished=False):
        screen = self.screen

        if getattr(screen, "stop_in_progress", False):
            return

        screen.stop_in_progress = True
        session = screen.session

        playback_finished = bool(playback_finished)

        if playback_finished:
            jd_log(
                "Playback ended normally -> returning to the previous screen",
                level="INFO"
            )

        try:
            screen.refresh_timer.stop()
        except Exception:
            pass

        try:
            screen.progress_report_timer.stop()
        except Exception:
            pass
    
        try:
            screen.hide_timer.stop()
        except Exception:
            pass

        try:
            jellydream_stop_timer(screen, "resume_timer")
            jellydream_stop_timer(screen, "_audio_refresh_timer")
            jellydream_stop_timer(screen, "_audio_restart_verify_timer")
            jellydream_stop_timer(screen, "_audio_restart_seek_timer")
        except Exception:
            pass

        try:
            screen.audioRestartClearTimer.stop()
        except Exception:
            pass

        try:
            screen.audioApplyTimer.stop()
        except Exception:
            pass

        try:
            screen.audioApplyTimer2.stop()
        except Exception:
            pass

        try:
            runtime = int(getattr(screen, "runtime_seconds", 0) or 0)

            if playback_finished and runtime > 0:

                pos = runtime
            else:
                pos = self.get_position_seconds()

                if pos <= 0:
                    pos = int(
                        getattr(
                            screen,
                            "internal_ram_seconds",
                            0
                        ) or 0
                    )

            screen.internal_ram_seconds = int(pos)
            screen.last_known_position_seconds = int(pos)

            if (
                not playback_finished
                and pos > 5
                and (
                    runtime == 0
                    or pos < (runtime - 10)
                )
            ):
                jellyfin_report_progress(
                    item_id=screen.item_id,
                    position_seconds=pos,
                    runtime_seconds=runtime,
                    title=screen.title,
                    is_paused=screen.paused,
                    play_session_id=screen.play_session_id,
                    media_source_id=screen.media_source_id,
                    stream_url=screen.current_stream_url
                )

            if pos > 0:
                jellyfin_report_stop(
                    item_id=screen.item_id,
                    position_seconds=pos,
                    title=screen.title,
                    media_source_id=getattr(
                        screen,
                       "media_source_id",
                        screen.item_id
                    ),
                    play_session_id=screen.play_session_id,
                    is_paused=screen.paused,
                    stream_url=screen.current_stream_url
                )

        except Exception as e:
            jd_log(
                f"Error while stopping playback: {e}",
                level="WARNING"
            )

        try:
            if screen.old_ref:
                session.nav.playService(screen.old_ref)
            else:
                session.nav.stopService()
        except Exception:
            pass

        try:
            item_details = getattr(
                screen,
                "item_details",
                {}
            ) or {}

            next_episode_candidate = getattr(
                screen,
                "_next_episode_candidate",
                None
            )

            if not isinstance(next_episode_candidate, dict):
                next_episode_candidate = None

            close_result = {
                "playback_finished": bool(playback_finished),
                "item_id": str(
                    getattr(screen, "item_id", "") or ""
                ),
                "title": str(
                    getattr(screen, "title", "") or ""
                ),
                "item_type": str(
                    item_details.get("Type") or ""
                ),
                "series_name": str(
                    item_details.get("SeriesName") or ""
                ),
                "series_id": str(
                    item_details.get("SeriesId") or ""
                ),
                "season_id": str(
                    item_details.get("SeasonId") or ""
                ),
                "season_number": item_details.get(
                    "ParentIndexNumber"
                ),
                "episode_number": item_details.get(
                    "IndexNumber"
                ),

                # Ergebnis der bereits zehn Sekunden vor dem Ende
                # angezeigten Frage.
                "next_episode_prompt_shown": bool(
                    getattr(
                        screen,
                        "_next_episode_prompt_shown",
                        False
                    )
                ),
                "next_episode_decision": getattr(
                    screen,
                    "_next_episode_decision",
                    None
                ),
                "next_episode": next_episode_candidate
            }

            screen.close(close_result)

        except Exception as e:
            jd_log(
                "Player result could not be returned: %s"% e,
                level="WARNING"
            )

            try:
                screen.close()
            except Exception:
                pass

        try:
            if screen.return_screen and getattr(screen.return_screen, "instance", None):
                screen.return_screen.instance.show()
        except Exception:
            pass

    def resume_jump(self):
        try:
            screen = self.screen

            target = int(
                getattr(
                    screen,
                    "_resume_target_seconds",
                    0
                ) or 0
            )

            if target <= 0:
                return

            screen._resume_retry_count = int(
                getattr(
                    screen,
                    "_resume_retry_count",
                    0
                ) or 0
            ) + 1

            screen.internal_ram_seconds = target
            screen.last_known_position_seconds = target

            seek_accepted = self.seek_to(target)

            if not seek_accepted:
                if screen._resume_retry_count < screen.RESUME_RETRY_MAX:
                    delay = 1200 + (
                        screen._resume_retry_count * 500
                    )

                    jellydream_restart_single_timer(
                        screen,
                        "resume_timer",
                        self.resume_jump,
                        delay
                    )

                return

            jd_log(
                "Resume seek accepted: target position %ss; position will still be verified"               % target,
                level="DEBUG"
            )

            def verify_resume_position():
                try:
                    current_position = int(
                        self.get_position_seconds() or 0
                    )

                    difference = abs(
                        current_position - target
                    )

                    if difference <= 15:
                        screen._resume_target_seconds = 0
                        screen._resume_retry_count = 0

                        jd_log(
                            "Resume seek confirmed: position=%ss, target=%ss"
                            % (
                                current_position,
                                target
                            ),
                            level="DEBUG"
                        )

                        return

                    jd_log(
                        "Resume seek not confirmed yet: position=%ss, target=%ss, delta=%ss"
                        % (
                            current_position,
                            target,
                            difference
                        ),
                        level="DEBUG"
                    )

                    if screen._resume_retry_count < screen.RESUME_RETRY_MAX:
                        jellydream_restart_single_timer(
                            screen,
                            "resume_timer",
                            self.resume_jump,
                            1000
                        )
                    else:
                        screen._resume_target_seconds = 0

                        jd_log(
                            "Resume seek could not be confirmed after %s attempts"                            % screen._resume_retry_count,
                            level="WARNING"
                        )

                except Exception as verify_error:
                    jd_log(
                        "Resume position could not be verified: %s"                       % verify_error,
                        level="WARNING"
                    )

            jellydream_restart_single_timer(
                screen,
                "resume_timer",
                verify_resume_position,
                1000
            )

        except Exception as e:
            jd_log(
                "Error in JellyDreamPlaybackController.resume_jump: %s"
                % e,
                level="WARNING"
            )


def jellydream_get_media_source_id(
    item_details,
    fallback_item_id=""
):
    """
    Liefert die ID aus MediaSources[0]['Id'].

    Jellyfin erwartet beim Stop-Report die MediaSource-ID des gestarteten
    Streams und nicht zwingend die Item-ID.
    """
    try:
        if isinstance(item_details, dict):
            sources = item_details.get("MediaSources") or []

            if (
                isinstance(sources, list)
                and sources
                and isinstance(sources[0], dict)
            ):
                media_source_id = str(
                    sources[0].get("Id") or ""
                ).strip()

                if media_source_id:
                    return media_source_id

    except Exception as e:
        jd_log(
            "MediaSource id could not be read: %s" % e,
            level="DEBUG"
        )

    return str(fallback_item_id or "").strip()


class JellyDreamStreamSummary(Screen):
    def __init__(self, session, parent):
        width, height = jellydream_get_lcd_size() or (132, 64)

        margin = max(1, int(width * 0.02))
        title_y = max(1, int(height * 0.02))
        title_height = max(1, int(height * 0.50))
        progress_y = int(height * 0.58)
        progress_height = max(2, int(height * 0.09))
        time_y = int(height * 0.73)
        time_height = max(1, height - time_y - 1)

        title_font = max(8, int(height * 0.22))
        time_font = max(6, int(height * 0.19))

        self.skin = """
        <screen name="JellyDreamStreamSummary"
                position="0,0" size="{width},{height}" id="1">
            <widget name="title"
                    position="{margin},{title_y}"
                    size="{content_width},{title_height}"
                    font="Regular;{title_font}"
                    halign="center" valign="center" />
            <widget name="progress"
                    position="{margin},{progress_y}"
                    size="{content_width},{progress_height}"
                    borderWidth="1" />
            <widget name="time"
                    position="{margin},{time_y}"
                    size="{content_width},{time_height}"
                    font="Regular;{time_font}"
                    halign="center" valign="center" />
        </screen>
        """.format(
            width=width,
            height=height,
            margin=margin,
            content_width=width - 2 * margin,
            title_y=title_y,
            title_height=title_height,
            title_font=title_font,
            progress_y=progress_y,
            progress_height=progress_height,
            time_y=time_y,
            time_height=time_height,
            time_font=time_font
        )

        Screen.__init__(self, session)

        # Ab hier deinen vorhandenen Code beibehalten:
        self.player = parent
        self._lcd_active = False

        self["title"] = Label("")
        self["progress"] = ProgressBar()
        self["time"] = Label("")

        self._lcd_timer = eTimer()
        parent._bind_timer(
            self._lcd_timer,
            self.updateDisplay
        )

        self.onShow.append(self.startDisplay)
        self.onHide.append(self.stopDisplay)
        self.onClose.append(self.stopDisplay)

    def startDisplay(self):
        self._lcd_active = True
        self.updateDisplay()

    def stopDisplay(self):
        self._lcd_active = False
        self._lcd_timer.stop()

    def updateDisplay(self):
        if not self._lcd_active:
            return

        if self.player.stop_in_progress:
            self.stopDisplay()
            return

        try:
            position = max(
                0,
                int(self.player.estimatedPositionSeconds() or 0)
            )
            runtime = max(
                0,
                int(self.player.runtime_seconds or 0)
            )

            if runtime > 0:
                position = min(position, runtime)
                percent = min(
                    100,
                    int(position * 100 / runtime)
                )
            else:
                percent = 0

            self["title"].setText(self.player.title)
            self["progress"].setValue(percent)
            self["time"].setText(
                "%s / %s" % (
                    self.player._formatInfobarTime(position),
                    self.player._formatInfobarTime(runtime)
                )
            )

        except Exception as e:
            jd_log(
                "LCD display could not be updated: %s"
                % e,
                level="DEBUG"
            )

        finally:
            if self._lcd_active:
                self._lcd_timer.start(1000, True)

                
class JellyDreamStreamScreen(JellyDreamPlayerBaseScreen):
    SCREEN_NAME = "JellyDreamStreamScreen"

    AUDIO_RESTART_VERIFY_DELAY_MS = 1200
    AUDIO_RESTART_RESEEK_DELAY_MS = 1400
    AUDIO_REAPPLY_DELAY_1_MS = 1200
    AUDIO_REAPPLY_DELAY_2_MS = 2500
    AUDIO_CLEAR_RESUME_DELAY_MS = 5000
    AUDIO_POLL_DELAY_MS = 250
    AUDIO_POLL_RETRY_MAX = 10
    RESUME_RETRY_MAX = 5
    PROGRESS_REPORT_INTERVAL_MS = 30000

    # EOF wird nicht sofort als reguläres Ende akzeptiert. Manche Player
    # melden bei Stream-/Audio-Wechseln ebenfalls evEOF.
    EOF_VERIFY_DELAY_MS = 1200

    # EOF nur akzeptieren, wenn die Wiedergabe tatsächlich sehr nah am
    # Ende steht. Das verhindert „nächste Folge“-Angebote bei Stream-,
    # Decoder- oder Playerfehlern.
    EOF_END_TOLERANCE_SECONDS = 10
    EOF_END_MIN_PERCENT = 98

    # Während der Wiedergabe wird einmal pro Sekunde geprüft, ob die
    # vorhandene EOF-Endtoleranz erreicht wurde.
    NEXT_EPISODE_CHECK_INTERVAL_MS = 1000

    # Wie viele Sekunden vor dem Ende die Frage angezeigt wird.
    NEXT_EPISODE_PROMPT_BEFORE_END_SECONDS = 15
    
    # Die Frage verschwindet kurz vor dem tatsächlichen Ende automatisch.
    # Dadurch bleibt kein Dialog über einem bereits geschlossenen Player.
    NEXT_EPISODE_PROMPT_TIMEOUT_SECONDS = 8
    
    def __init__(self, session, title, old_ref=None, return_screen=None,
                item_id="", runtime_seconds=0, start_time=0,
                v_codec="", a_codec="", res="", initial_audio_stream_index=None,
                preloaded_stream_data=None, stream_url="",
                service_reference=SERVICE_REFERENCE,
                play_session_id=""):
        JellyDreamPlayerBaseScreen.__init__(self, session)

        self.title = str(title or "Jellyfin Video")
        self.old_ref = old_ref
        self.return_screen = return_screen
        self.item_id = str(item_id or "").strip()
        self.play_session_id = str(
            play_session_id or uuid.uuid4()
        ).strip()
        self.runtime_seconds = int(runtime_seconds or 0)
        self.start_time = int(start_time or 0)

        self.paused = False
        self.infobar_visible = False
        self.stop_in_progress = False

        self._ignore_eof = False

        # Status der Vorabfrage für die nächste Episode.
        self._next_episode_lookup_started = False
        self._next_episode_lookup_finished = False
        self._next_episode_candidate = None
        self._next_episode_prompt_shown = False
        self._next_episode_decision = None
        self._next_episode_dialog_open = False
        self._segment_prompt_open = False

        self._service_event_tracker = ServiceEventTracker(
            screen=self,
            eventmap={
                iPlayableService.evEOF: self.onPlaybackEOF
            }
        )

        self.internal_ram_seconds = int(self.start_time or 0)
        self.last_known_position_seconds = int(self.start_time or 0)

        self._resume_retry_count = 0
        self._resume_target_seconds = int(self.start_time or 0)
        self.current_stream_url = str(stream_url or "")
        self.current_service_reference = int(
            service_reference or SERVICE_REFERENCE
        )
        self._initial_audio_apply_retries = 0
        self._initial_audio_applied = False

        self["infobar_background"] = Label("")
        self["infobar_cover"] = Pixmap()
        self["playback_cover"] = Pixmap()
        self["jelly_video_title"] = Label(self.title)
        self["time_text"] = Label("")
        self["remaining_time_text"] = Label("")
        self["clock"] = Label("")
        self["date"] = Label("")
        self["codec_info"] = Label("")
        self["stream_debug"] = Label("")
        self["progress"] = ProgressBar()

        self.hide_timer = eTimer()
        self.refresh_timer = eTimer()
        self.progress_report_timer = eTimer()

        # Eigener Timer, weil refresh_timer nur bei sichtbarer Infobar läuft
        # und der 30-Sekunden-Progress-Timer für eine 10-Sekunden-Grenze
        # zu ungenau wäre.
        self._next_episode_timer = eTimer()
        self._cover_drift_timer = eTimer()
        self._media_segment_timer = eTimer()
        self._media_segments = []
        self._skipped_segment_ids = set()
        self._cover_drift = None

        self.audioRestartClearTimer = eTimer()
        self.audioApplyTimer = eTimer()
        self.audioApplyTimer2 = eTimer()

        self.stream_info = JellyDreamStreamInfoLoader(self.item_id, preloaded_details=preloaded_stream_data)
        self.stream_data = self.stream_info.load()

        self.item_details = self.stream_data.get("item_details", {}) or {}

        self.media_source_id = jellydream_get_media_source_id(
            self.item_details,
            self.item_id
        )

        self.cover_id = self.stream_data.get("cover_id", "") or ""

        jd_log(
            "Player MediaSource id stored: %s"
            % self.media_source_id,
            level="DEBUG"
        )

        video_meta = self.stream_data.get(
            "video_meta",
            {}
        ) or {}

        audio_meta = self.stream_data.get(
            "audio_meta",
            {}
        ) or {}

        subtitle_meta = self.stream_data.get(
            "subtitle_meta",
            {}
        ) or {}

        self.v_codec = (v_codec or video_meta.get("codec", "") or "").upper()
        self.a_codec = (a_codec or audio_meta.get("codec", "") or "").upper()
        self.res = str(res or video_meta.get("resolution", "") or "")

        self.audioController = JellyDreamAudioController(self)
        initial_jellyfin_audio_index = initial_audio_stream_index

        if initial_jellyfin_audio_index is None:
            initial_jellyfin_audio_index = audio_meta.get(
                "jellyfin_audio_stream_index"
            )

        self.audioController.setInitialAudioStreamIndex(
            initial_jellyfin_audio_index
        )
        self.audioController.audio_tracks_cache = audio_meta.get("streams", []) or []
        self.audioController.subtitle_tracks_cache = (
            subtitle_meta.get(
                "streams",
                []
            ) or []
        )

        # Untertitel starten standardmäßig deaktiviert.
        # Sie werden bewusst erst über das Menü eingeschaltet.
        self.audioController.selected_jellyfin_subtitle_stream_index = None        
        self.audioController.buildAudioTrackMenuData()

        self.playback = JellyDreamPlaybackController(self)

        self["actions"] = ActionMap(
            ["OkCancelActions", "DirectionActions", "ColorActions", "InfobarActions", "InfobarSeekActions", "MediaPlayerActions", "NumberActions"],
            {
                "cancel": self.closeInfobarOrStop,
                "red": self.stopStream,
                "stop": self.stopStream,
                "ok": self.toggleInfobar,
                "play": self.togglePause,
                "playpause": self.togglePause,
                "playpauseService": self.togglePause,
                "pauseService": self.togglePause,
                "yellow": self.audioController.openJellyfinAudioMenu,
                "audio": self.audioController.openJellyfinAudioMenu,
                "subtitle": self.audioController.openJellyfinAudioMenu,
                "left": self.seekBackSmall,
                "right": self.seekForwardSmall,
                "seekBack": self.seekBackSmall,
                "seekFwd": self.seekForwardSmall,
                "rewind": self.seekBackBig,
                "fastForward": self.seekForwardBig,
                "1": self.seekBackSmall,
                "3": self.seekForwardSmall,
                "4": self.seekBackBig,
                "6": self.seekForwardBig,
                "7": self.seekVeryBackBig,
                "9": self.seekVeryForwardBig,
            },
            -1
        )

        self.onLayoutFinish.append(self.startScreenLayout)

        try:
            if session and not hasattr(session, "enableSubtitle"):
                def safe_enable_subtitle(sub_idx=None):
                    try:
                        cur_service = session.nav.getCurrentService()
                        if cur_service and hasattr(cur_service, "subtitle"):
                            sub = cur_service.subtitle()
                            if sub and sub_idx is not None:
                                sub.enableSubtitle(sub_idx)
                    except Exception:
                        pass
                session.enableSubtitle = safe_enable_subtitle
        except Exception:
            pass

        if self.start_time > 0:
            self.onLayoutFinish.append(self.jumpToResumePosition)

    def createSummary(self):
        return JellyDreamStreamSummary
        
    def startScreenLayout(self):
        self._connectTimers()
        self._applyLoadedMeta()
        self._loadCover()
        self.updateCodecAndLanguageText()
        self.showInfobar()
        jellydream_restart_single_timer(
            self,
            "_initial_audio_apply_timer",
            self.audioController.applyInitialAudioSelection,
            2200
        )
        jellydream_restart_single_timer(
            self,
            "_audio_refresh_timer",
            self.audioController.delayedAudioInfoRefresh,
            1200
        )

        if self.item_id:
            jellyfin_report_start(
                item_id=self.item_id,
                title=self.title,
                position_seconds=self.start_time,
                play_session_id=self.play_session_id,
                media_source_id=self.media_source_id,
                stream_url=self.current_stream_url
            )

            jellyfin_report_progress(
                item_id=self.item_id,
               position_seconds=self.start_time,
                runtime_seconds=self.runtime_seconds,
                title=self.title,
                is_paused=False,
                play_session_id=self.play_session_id,
                media_source_id=self.media_source_id,
                stream_url=self.current_stream_url
            )

            self.progress_report_timer.start(
                self.PROGRESS_REPORT_INTERVAL_MS,
                True
            )

        # Bei Episoden die nächste Folge frühzeitig im Hintergrund suchen.
        # Der Dialog selbst wird trotzdem erst innerhalb der letzten
        # EOF_END_TOLERANCE_SECONDS angezeigt.
        self.prepareNextEpisodePrompt()
        self.loadMediaSegments()

    def _connectTimers(self):
        self._bind_timer(self.hide_timer, self.hideInfobar)
        self._bind_timer(self.refresh_timer, self.refreshInfobarData)
        self._bind_timer(
            self.progress_report_timer,
            self.reportPlaybackProgress
        )
        self._bind_timer(
            self._next_episode_timer,
            self.checkNextEpisodePrompt
        )
        self._bind_timer(
            self._cover_drift_timer,
            self._stepCoverDrift
        )
        self._bind_timer(
            self._media_segment_timer,
            self.checkMediaSegments
        )
        self._bind_timer(
            self.audioRestartClearTimer,
            self.audioController.clearResumeTargetSeconds
        )
        self._bind_timer(
            self.audioApplyTimer,
            self.audioController.applyPendingAudioSelection
        )
        self._bind_timer(
            self.audioApplyTimer2,
            self.audioController.applyPendingAudioSelection
        )

    def _bind_timer(self, timer, callback):
        try:
            timer.callback.append(callback)
        except Exception:
            try:
                timer.timeout.get().append(callback)
            except Exception:
                pass

    def _applyLoadedMeta(self):
        try:
            details = self.item_details or {}

            if details:
                real_name = str(
                    details.get("Name")
                    or self.title
                    or "Jellyfin Video"
                ).strip()

                item_type = str(
                    details.get("Type")
                    or ""
                ).strip()

                series_name = str(
                    details.get("SeriesName")
                    or ""
                ).strip()

                current_title = real_name

                # Bei Serienfolgen zusätzlich Staffel und Episode anzeigen:
                # Beispiel:
                # Serienname - S01E02 - Episodentitel
                if item_type == "Episode":
                    season_number = details.get(
                        "ParentIndexNumber"
                    )

                    episode_number = details.get(
                        "IndexNumber"
                    )

                    episode_code = ""

                    if (
                        season_number is not None
                        and episode_number is not None
                    ):
                        try:
                            episode_code = "S%02dE%02d" % (
                                int(season_number),
                                int(episode_number)
                            )
                        except Exception:
                            episode_code = ""

                    if series_name and episode_code:
                        current_title = "%s - %s - %s" % (
                            series_name,
                            episode_code,
                            real_name
                        )

                    elif episode_code:
                        current_title = "%s - %s" % (
                            episode_code,
                            real_name
                        )

                    elif series_name:
                        current_title = "%s - %s" % (
                            series_name,
                            real_name
                        )

                self.title = current_title

                max_title_length = 80

                display_title = current_title

                if len(display_title) > max_title_length:
                    display_title = (
                        display_title[:max_title_length - 3].rstrip()
                        + "..."
                    )

                self["jelly_video_title"].setText(
                    display_title
                )

                try:
                    self.setTitle(current_title)
                except Exception:
                    pass

            if not self.v_codec:
                self.v_codec = str(
                    self.stream_data.get(
                        "video_meta",
                        {}
                    ).get(
                        "codec",
                        ""
                    )
                    or ""
                ).upper()

            if not self.res:
                self.res = str(
                    self.stream_data.get(
                        "video_meta",
                        {}
                    ).get(
                        "resolution",
                        ""
                    )
                    or ""
                )

            if not self.a_codec:
                self.a_codec = str(
                    self.stream_data.get(
                        "audio_meta",
                        {}
                    ).get(
                        "codec",
                        ""
                    )
                    or ""
                ).upper()

        except Exception as e:
            jd_log(
                "Error while applying stream metadata: %s"
                % e,
                level="WARNING"
            )

    def _setCoverWidget(self, widget, item_id, width, height, fallback_path=""):
        if widget is None:
            return

        item_id = str(item_id or "").strip()

        if not item_id:
            jellydream_safe_set_pixmap(
                widget,
                fallback_path,
                hide_if_missing=True
            )
            return

        try:
            widget.show()
        except Exception:
            pass

        cover_dir = jellydream_cache_dir("cover")
        cached_path = jellydream_get_cached_image_path(
            item_id,
            width,
            height,
            "Primary"
        )

        if jellydream_valid_file(cached_path, 500):
            jellydream_safe_set_pixmap(
                widget,
                cached_path,
                hide_if_missing=False
            )
            return

        if fallback_path:
            jellydream_safe_set_pixmap(
                widget,
                fallback_path,
                hide_if_missing=True
            )

        def cover_finished(path, loaded_item_id, target_widget=widget):
            if not getattr(self, "instance", None):
                return

            if str(loaded_item_id) != item_id:
                return

            if jellydream_valid_file(path, 500):
                jellydream_safe_set_pixmap(
                    target_widget,
                    path,
                    hide_if_missing=False
                )

        jellydream_async_download_image(
            item_id,
            cover_dir,
            width=width,
            height=height,
            image_type="Primary",
            callback=cover_finished
        )

    def _stopCoverDrift(self):
        self._cover_drift = None
        timer = getattr(self, "_cover_drift_timer", None)

        if not timer:
            return

        try:
            timer.stop()
        except Exception:
            pass

    def _scheduleCoverDrift(self):
        if not getattr(self, "_cover_drift", None):
            return

        if not getattr(self, "instance", None):
            return

        if getattr(self, "stop_in_progress", False):
            self._stopCoverDrift()
            return

        timer = getattr(self, "_cover_drift_timer", None)

        if not timer:
            timer = eTimer()
            self._cover_drift_timer = timer
            self._bind_timer(timer, self._stepCoverDrift)

        try:
            timer.start(100, True)
        except Exception as e:
            self._stopCoverDrift()
            jd_log(
                "Music cover drift timer could not be armed: %s" % e,
                level="WARNING"
            )

    def _coverDriftBounds(self):
        widget = self["playback_cover"]
        instance = getattr(widget, "instance", None)
        screen = getattr(self, "instance", None)

        if not instance or not screen:
            return None

        position = instance.position()
        size = instance.size()
        screen_size = screen.size()
        cover_w = int(size.width())
        cover_h = int(size.height())
        screen_w = int(screen_size.width())
        screen_h = int(screen_size.height())

        if cover_w < 40 or cover_h < 40 or screen_w < 80 or screen_h < 80:
            return None

        margin = max(12, screen_w // 60)
        floor = screen_h - margin

        if "infobar_background" in self:
            bar = getattr(self["infobar_background"], "instance", None)

            if bar is not None:
                try:
                    floor = int(bar.position().y()) - margin
                except Exception:
                    pass

        min_x = margin
        min_y = margin
        max_x = screen_w - cover_w - margin
        max_y = floor - cover_h

        if max_x - min_x < 40 and max_y - min_y < 40:
            return None

        if max_x < min_x:
            min_x = max_x = int(position.x())

        if max_y < min_y:
            min_y = max_y = int(position.y())

        return {
            "x": max(min_x, min(int(position.x()), max_x if max_x > min_x else min_x)),
            "y": max(min_y, min(int(position.y()), max_y if max_y > min_y else min_y)),
            "vx": 3 if max_x > min_x + 3 else 0,
            "vy": 2 if max_y > min_y + 3 else 0,
            "min_x": min_x,
            "min_y": min_y,
            "max_x": max_x,
            "max_y": max_y,
            "w": cover_w,
            "h": cover_h
        }

    def _startCoverDrift(self):
        self._stopCoverDrift()
        self._cover_drift = {"pending": True, "tries": 0}
        jd_log(
            "Music cover drift timer started",
            level="INFO"
        )
        self._scheduleCoverDrift()

    def _stepCoverDrift(self):
        state = getattr(self, "_cover_drift", None)

        if not state or not getattr(self, "instance", None):
            return

        try:
            if state.get("pending"):
                measured = self._coverDriftBounds()
                tries = int(state.get("tries") or 0) + 1

                if not measured or (
                    measured["vx"] == 0 and measured["vy"] == 0
                ):
                    if tries >= 40:
                        self._stopCoverDrift()
                        jd_log(
                            "Music cover drift gave up: the cover has no room to move",
                            level="INFO"
                        )
                        return

                    self._cover_drift = {
                        "pending": True,
                        "tries": tries
                    }
                    self._scheduleCoverDrift()
                    return

                state = measured
                self._cover_drift = state
                jd_log(
                    "Music cover drift moving inside x=%s..%s y=%s..%s size=%sx%s"
                    % (
                        state["min_x"],
                        state["max_x"],
                        state["min_y"],
                        state["max_y"],
                        state["w"],
                        state["h"]
                    ),
                    level="INFO"
                )

            instance = self["playback_cover"].instance

            if not instance:
                self._scheduleCoverDrift()
                return

            x = state["x"] + state["vx"]
            y = state["y"] + state["vy"]

            if state["vx"] and (x < state["min_x"] or x > state["max_x"]):
                state["vx"] = -state["vx"]
                x = state["x"] + state["vx"]

            if state["vy"] and (y < state["min_y"] or y > state["max_y"]):
                state["vy"] = -state["vy"]
                y = state["y"] + state["vy"]

            state["x"] = int(x)
            state["y"] = int(y)
            instance.move(ePoint(state["x"], state["y"]))

            try:
                instance.invalidate()
            except Exception:
                pass

            try:
                self.instance.invalidate()
            except Exception:
                pass

        except Exception as e:
            self._stopCoverDrift()
            jd_log(
                "Cover movement stopped: %s" % e,
                level="WARNING"
            )
            return

        self._scheduleCoverDrift()

    def _loadCover(self):
        try:
            target_cover_id = self.cover_id or self.item_id
            item_type = str(
                (self.item_details or {}).get("Type") or ""
            ).strip()
            playback_cover = (
                self["playback_cover"]
                if "playback_cover" in self
                else None
            )

            self._setCoverWidget(
                self["infobar_cover"],
                target_cover_id,
                90,
                130,
                FALLBACK_HOME_COVER
            )

            if item_type == "Audio" and playback_cover is not None:
                self._setCoverWidget(
                    playback_cover,
                    target_cover_id,
                    600,
                    600
                )
                self._startCoverDrift()
            elif playback_cover is not None:
                self._stopCoverDrift()
                try:
                    playback_cover.hide()
                except Exception:
                    pass

        except Exception as e:
            jd_log(
                "Error while loading the player cover: %s" % e,
                level="WARNING"
            )

            try:
                jellydream_safe_set_pixmap(
                    self["infobar_cover"],
                    FALLBACK_HOME_COVER,
                    hide_if_missing=True
                )
            except Exception:
                pass

    def updateCodecAndLanguageText(self):
        try:
            info_parts = []

            if self.v_codec:
                if self.res:
                    info_parts.append(f"VIDEO: {self.v_codec} ({self.res})")
                else:
                    info_parts.append(f"VIDEO: {self.v_codec}")
            elif self.res:
                info_parts.append(
                    "AUFLÖSUNG: %s" % self.res
                )

            audio_text = self.audioController.getAudioCodecDisplayText()
            if audio_text:
                info_parts.append(f"AUDIO: {audio_text}")

            current_language = self.audioController.getPreferredAudioLanguageDisplay()
            if current_language:
                info_parts.append(f"SPRACHE: {current_language}")

            subtitle_index = getattr(
                self.audioController,
                "selected_jellyfin_subtitle_stream_index",
                None
            )

            if subtitle_index is not None:
                subtitle_language = ""

                for subtitle_meta in (
                    self.audioController.subtitle_tracks_cache
                    or []
                ):
                    if not isinstance(subtitle_meta, dict):
                        continue

                    meta_index = subtitle_meta.get(
                        "JellyfinIndex",
                        subtitle_meta.get("Index")
                    )

                    try:
                        if int(meta_index) != int(subtitle_index):
                            continue
                    except Exception:
                        continue

                    subtitle_language = (
                        self.audioController.normalizeAudioLanguage(
                            subtitle_meta.get("DisplayLanguage")
                            or subtitle_meta.get("Language")
                            or ""
                        )
                    )

                    if not subtitle_language:
                        subtitle_language = str(
                            subtitle_meta.get("Title")
                            or subtitle_meta.get("DisplayTitle")
                            or ""
                        ).strip()

                    break

                if subtitle_language:
                    info_parts.append(
                        "UNTERTITEL: %s"
                        % subtitle_language
                    )
                else:
                    info_parts.append(
                        "UNTERTITEL: AN"
                    )
            self["codec_info"].setText("   |   ".join(info_parts) if info_parts else "")
        except Exception as e:
            jd_log(f"Error in updateCodecAndLanguageText: {e}", level="WARNING")

    def jumpToResumePosition(self):
        self._resume_retry_count = 0
        self._resume_target_seconds = int(self.start_time or 0)
        jellydream_restart_single_timer(self, "resume_timer", self.doJump, 1200)

    def doJump(self):
        self.playback.resume_jump()

    def closeInfobarOrStop(self):
        """
        EXIT/BACK schließt zuerst nur die sichtbare Infobar.

        Erst wenn die Infobar bereits ausgeblendet ist, wird die
        Wiedergabe beendet.
        """
        if self.infobar_visible:
            self.hideInfobar()
            return

        self.stopStream()

    def toggleInfobar(self):
        if self.infobar_visible:
            self.hideInfobar()
        else:
            self.showInfobar()

    def showInfobar(self):
        self.infobar_visible = True
        self.updateCodecAndLanguageText()

        for name in INFOBAR_WIDGETS:
            if name in self and getattr(self[name], "instance", None):
                self[name].instance.show()

        self.refreshInfobarData()
        try:
            self.refresh_timer.start(1000, False)
        except Exception:
            pass
        try:
            self.hide_timer.start(5000, True)
        except Exception:
            pass

    def hideInfobar(self):
        self.infobar_visible = False
        try:
            self.refresh_timer.stop()
        except Exception:
            pass
        try:
            self.hide_timer.stop()
        except Exception:
            pass

        for name in INFOBAR_WIDGETS:
            if name in self and getattr(self[name], "instance", None):
                self[name].instance.hide()

    def _formatInfobarTime(self, seconds):
        try:
            seconds = max(0, int(seconds or 0))
            hours = seconds // 3600
            minutes = (seconds % 3600) // 60
            secs = seconds % 60

            if hours > 0:
                return "%d:%02d:%02d" % (hours, minutes, secs)

            return "%02d:%02d" % (minutes, secs)
        except Exception:
            return "00:00"

    def updateStreamDebugText(self):
        """
        Zeigt bei aktivem DEBUG-Level die technischen Streaminformationen
        in Gelb an.

        Bei allen anderen Log-Levels wird die Jellyfin-Kurzbeschreibung
        aus `Overview` in Weiß angezeigt.
        """
        try:
            if "stream_debug" not in self:
                return

            stream_widget = self["stream_debug"]

            debug_text = jellydream_stream_debug_text(
                self.current_stream_url,
                self.current_service_reference
            )

            if debug_text:
                # Technische DEBUG-Anzeige weiterhin gelb darstellen.
                try:
                    if getattr(stream_widget, "instance", None):
                        stream_widget.instance.setForegroundColor(
                            parseColor("#f1c40f")
                        )
                except Exception:
                    pass

                stream_widget.setText(debug_text)
                return

            # Kurzbeschreibung immer weiß darstellen.
            try:
                if getattr(stream_widget, "instance", None):
                    stream_widget.instance.setForegroundColor(
                        parseColor("#ffffff")
                    )
            except Exception:
                pass

            details = (
                self.item_details
                if isinstance(self.item_details, dict)
                else {}
            )

            item_type = str(
                details.get("Type") or ""
            ).strip()

            overview = jellydream_item_overview(details)

            # Nur Filme und Episoden erhalten eine Kurzbeschreibung.
            if item_type not in ("Movie", "Episode"):
                stream_widget.setText("")
                return

            if not overview:
                stream_widget.setText(
                    _("Keine Kurzbeschreibung vorhanden.")
                )
                return

            short_overview = jellydream_wrap(
                overview,
                125,
                3
            )

            stream_widget.setText(short_overview)

        except Exception as e:
            jd_log(
                "Error while updating the stream/short description: %s"
                % e,
                level="DEBUG"
            )

            try:
                self["stream_debug"].setText("")
            except Exception:
                pass

    def _formatVideoResolution(self, width, height):
        try:
            width = int(width or 0)
            height = int(height or 0)

            if width <= 0 or height <= 0:
                return ""

            if width >= 3840 or height >= 2160:
                label = "4K"
            elif width >= 2560 or height >= 1440:
                label = "1440p"
            elif width >= 1920 or height >= 1080:
                label = "1080p"
            elif width >= 1280 or height >= 720:
                label = "720p"
            elif width >= 720 or height >= 576:
                label = "576p"
            else:
                label = "%sp" % height

            return "%sx%s / %s" % (
                width,
                height,
                label
            )

        except Exception:
            return ""

    def updateActiveVideoResolution(self):
        try:
            width_info_id = getattr(
                iServiceInformation,
                "sVideoWidth",
                None
            )

            height_info_id = getattr(
                iServiceInformation,
                "sVideoHeight",
                None
            )

            if width_info_id is None or height_info_id is None:
                return False

            service = self.session.nav.getCurrentService()

            if not service:
                return False

            info = service.info()

            if not info:
                return False

            width = int(
                info.getInfo(width_info_id) or 0
            )

            height = int(
                info.getInfo(height_info_id) or 0
            )

            if width <= 0 or height <= 0:
                return False

            new_resolution = self._formatVideoResolution(
                width,
                height
            )

            if not new_resolution:
                return False

            if self.res != new_resolution:
                self.res = new_resolution

                jd_log(
                    "Active video resolution reported by the player: %s"                    % self.res,
                    level="DEBUG"
                )

                self.updateCodecAndLanguageText()

            return True

        except Exception as e:
            jd_log(
                "Active video resolution could not be read: %s"                % e,
                level="DEBUG"
            )

            return False

    def reportPlaybackProgress(self):
        """
        Periodische Positionsmeldung an Jellyfin.

        Der Timer wird am Ende erneut als One-Shot gestartet.
        Dadurch entsteht kein dauerhafter Timerlauf nach dem Schließen
        des Players.
        """
        try:
            if self.stop_in_progress:
                return

            if not self.item_id:
                return

            position = int(
                self.estimatedPositionSeconds() or 0
            )

            runtime = int(
                self.runtime_seconds or 0
            )

            if runtime > 0:
                position = min(position, runtime)

            jellyfin_report_progress(
                item_id=self.item_id,
                position_seconds=position,
                runtime_seconds=runtime,
                title=self.title,
                is_paused=self.paused,
                play_session_id=self.play_session_id,
                media_source_id=self.media_source_id,
                stream_url=self.current_stream_url
            )

        except Exception as e:
            jd_log(
                "Periodic progress report failed: %s"
                % e,
                level="WARNING"
            )

        finally:
            try:
                if not self.stop_in_progress:
                    self.progress_report_timer.start(
                        self.PROGRESS_REPORT_INTERVAL_MS,
                        True
                    )
            except Exception:
                pass
            
    def refreshInfobarData(self):
        try:
            if not self.infobar_visible:
                return

            self.updateActiveVideoResolution()
            self.updateStreamDebugText()
            
            position = int(self.estimatedPositionSeconds() or 0)
            runtime = int(self.runtime_seconds or 0)

            if runtime > 0:
                position = min(position, runtime)
                remaining = max(0, runtime - position)
                percent = int((position * 100) / runtime)
                percent = max(0, min(100, percent))
            else:
                remaining = 0
                percent = 0

            self["time_text"].setText(
                "%s / %s" % (
                    self._formatInfobarTime(position),
                    self._formatInfobarTime(runtime)
                )
            )

            if runtime > 0:
                self["remaining_time_text"].setText(
                    "-%s" % self._formatInfobarTime(remaining)
                )
            else:
                self["remaining_time_text"].setText("")

            try:
                self["progress"].setValue(percent)
            except Exception:
                pass

            self["clock"].setText(strftime("%H:%M"))
            self["date"].setText(strftime("%d.%m.%Y"))

        except Exception as e:
            jd_log(
                "Error in JellyDreamStreamScreen.refreshInfobarData: %s" % e,
                level="WARNING"
            )
            
    def estimatedPositionSeconds(self):
        return self.playback.get_position_seconds()

    def seekToPosition(self, seconds):
        return self.playback.seek_to(seconds)

    def seekRelativeSeconds(self, seconds):
        return self.playback.seek_relative(seconds)

    def togglePause(self):
        return self.playback.toggle_pause()

    def buildCurrentEpisodePlaybackResult(self):
        """
        Erstellt dieselben Episodeninformationen, die normalerweise erst
        beim Schließen des Players zurückgegeben werden.

        playback_finished=True wird hier ausschließlich verwendet, damit
        die vorhandene Suche nach der nächsten Episode bereits vor dem
        tatsächlichen EOF genutzt werden kann.
        """
        try:
            item_details = (
                self.item_details
                if isinstance(self.item_details, dict)
                else {}
            )

            return {
                "playback_finished": True,
                "item_id": str(
                    getattr(self, "item_id", "") or ""
                ),
                "title": str(
                    getattr(self, "title", "") or ""
                ),
                "item_type": str(
                    item_details.get("Type") or ""
                ),
                "series_name": str(
                    item_details.get("SeriesName") or ""
                ),
                "series_id": str(
                    item_details.get("SeriesId") or ""
                ),
                "season_id": str(
                    item_details.get("SeasonId") or ""
                ),
                "season_number": item_details.get(
                    "ParentIndexNumber"
                ),
                "episode_number": item_details.get(
                    "IndexNumber"
                )
            }

        except Exception as e:
            jd_log(
                "Episode info for the prefetch could not be built: %s" % e,
                level="WARNING"
            )

            return {}


    def loadMediaSegments(self):
        item_type = str(
            (self.item_details or {}).get("Type") or ""
        ).strip()

        if item_type == "Audio" or not self.item_id:
            return

        def segments_loaded(segments):
            try:
                if not getattr(self, "instance", None):
                    return

                self._media_segments = (
                    list(segments)
                    if isinstance(segments, list)
                    else []
                )

                if not self._media_segments:
                    return

                jd_log(
                    "Media segments loaded for skip: %s"
                    % ", ".join(
                        "%s %s-%ss" % (
                            segment.get("type"),
                            segment.get("start"),
                            segment.get("end")
                        )
                        for segment in self._media_segments
                    )
                )

                if not self.stop_in_progress:
                    self._media_segment_timer.start(1000, True)
            except Exception as e:
                jd_log(
                    "Media segments could not be applied: %s" % e,
                    level="WARNING"
                )

        jellydream_load_media_segments_async(
            self.item_id,
            finished_callback=segments_loaded
        )

    def _following_episode_ready(self):
        episode = self._next_episode_candidate

        if not isinstance(episode, dict):
            return False

        return bool(str(episode.get("Id") or "").strip())

    def _prompt_skip_intro(self, segment, position):
        segment_id = segment.get("id")
        end = int(segment.get("end") or 0)
        self._skipped_segment_ids.add(segment_id)

        if jellydream_skip_intro_automatically():
            jd_log(
                "Skipping intro automatically: %ss -> %ss" % (
                    position,
                    end
                )
            )

            if not self.seekToPosition(end):
                self._skipped_segment_ids.discard(segment_id)

            return

        self._segment_prompt_open = True

        jd_log(
            "Asking to skip intro at %ss" % position
        )

        def intro_answer(answer):
            try:
                self._segment_prompt_open = False

                if self.stop_in_progress or answer is not True:
                    jd_log(
                        "Intro skip declined"
                    )
                    return

                if self.seekToPosition(end):
                    jd_log(
                        "Skipping intro: %ss -> %ss" % (
                            position,
                            end
                        )
                    )
                else:
                    self._skipped_segment_ids.discard(segment_id)
            except Exception as e:
                self._segment_prompt_open = False
                jd_log(
                    "Intro skip answer failed: %s" % e,
                    level="WARNING"
                )

        self.session.openWithCallback(
            intro_answer,
            MessageBox,
            _("Vorspann überspringen?"),
            MessageBox.TYPE_YESNO,
            timeout=self.NEXT_EPISODE_PROMPT_TIMEOUT_SECONDS,
            default=False
        )

    def checkMediaSegments(self):
        try:
            if (
                self.stop_in_progress
                or self.paused
                or self._segment_prompt_open
                or self._next_episode_dialog_open
            ):
                return

            segments = getattr(self, "_media_segments", None) or []

            if not segments:
                return

            raw_position = self.playback.get_raw_position_seconds()

            if raw_position is None:
                return

            position = int(raw_position)

            if position < 1:
                return

            resume_target = int(
                getattr(self, "_resume_target_seconds", 0) or 0
            )

            if resume_target > 5 and position + 8 < resume_target:
                return

            runtime = int(self.runtime_seconds or 0)
            skipped = self._skipped_segment_ids
            item_type = str(
                (self.item_details or {}).get("Type") or ""
            )

            for segment in segments:
                segment_id = segment.get("id")

                if segment_id in skipped:
                    continue

                start = int(segment.get("start") or 0)
                end = int(segment.get("end") or 0)

                if position < start or position >= end - 1:
                    continue

                kind = segment.get("type")

                if kind == "Intro":
                    self._prompt_skip_intro(segment, position)
                    break

                reaches_end = runtime > 0 and end >= runtime - 2

                if reaches_end and item_type == "Episode":
                    if not self._next_episode_lookup_started:
                        self.prepareNextEpisodePrompt()

                    lookup_pending = (
                        self._next_episode_lookup_started
                        and not self._next_episode_lookup_finished
                    )

                    if lookup_pending and not self._next_episode_prompt_shown:
                        break

                    if (
                        self._following_episode_ready()
                        or self._next_episode_prompt_shown
                    ):
                        skipped.add(segment_id)

                        if not self._next_episode_prompt_shown:
                            self.checkNextEpisodePrompt(force=True)

                        break

                target = end - 1

                if reaches_end and runtime > 0:
                    target = runtime - 1

                if target <= position + 1:
                    skipped.add(segment_id)
                    continue

                skipped.add(segment_id)

                if self.seekToPosition(target):
                    jd_log(
                        "Skipping credits: %ss -> %ss" % (
                            position,
                            target
                        )
                    )
                else:
                    skipped.discard(segment_id)

                break
        except Exception as e:
            jd_log(
                "Media segment skip failed: %s" % e,
                level="WARNING"
            )
        finally:
            try:
                pending = []

                if not self.stop_in_progress:
                    pending = [
                        segment
                        for segment in (getattr(self, "_media_segments", None) or [])
                        if segment.get("id") not in self._skipped_segment_ids
                    ]

                if pending:
                    self._media_segment_timer.start(1000, True)
            except Exception:
                pass

    def prepareNextEpisodePrompt(self):
        """
        Sucht die nächste Folge frühzeitig im Hintergrund.

        Die Suche selbst zeigt noch keinen Dialog. Die Frage wird erst von
        checkNextEpisodePrompt() innerhalb der letzten zehn Sekunden
        eingeblendet.
        """
        try:
            if self._next_episode_lookup_started:
                return

            item_details = (
                self.item_details
                if isinstance(self.item_details, dict)
                else {}
            )

            if str(item_details.get("Type") or "").strip() != "Episode":
                return

            if int(self.runtime_seconds or 0) <= 0:
                jd_log(
                    "Next-episode prefetch skipped: runtime is unknown",
                    level="DEBUG"
                )
                return

            playback_result = (
                self.buildCurrentEpisodePlaybackResult()
            )

            if not playback_result:
                return

            self._next_episode_lookup_started = True

            def next_episode_resolved(
                next_episode,
                error_message=""
            ):
                try:
                    if not getattr(self, "instance", None):
                        return

                    self._next_episode_lookup_finished = True

                    if error_message:
                        jd_log(
                            "Next-episode prefetch failed: %s" % error_message,
                            level="WARNING"
                        )

                        self._next_episode_candidate = None
                        return

                    self._next_episode_candidate = (
                        next_episode
                        if isinstance(next_episode, dict)
                        else None
                    )

                    if self._next_episode_candidate:
                        jd_log(
                            "Next episode for the 10-second prompt was prepared: %s"
                            % (
                                self._next_episode_candidate.get("Name")
                                or self._next_episode_candidate.get("Id")
                                or "Folge"
                            ),
                            level="DEBUG"
                        )

                    # Falls die Suche erst innerhalb der letzten zehn
                    # Sekunden fertig wurde, den Dialog sofort prüfen.
                    self.checkNextEpisodePrompt()

                except Exception as e:
                    jd_log(
                        "Episode prefetch result could not be processed: %s" % e,
                        level="WARNING"
                    )

            jellydream_find_next_episode_async(
                playback_result,
                finished_callback=next_episode_resolved
            )

            self._next_episode_timer.start(
                self.NEXT_EPISODE_CHECK_INTERVAL_MS,
                True
            )

        except Exception as e:
            jd_log(
                "Prefetch for the next episode could not be started: %s" % e,
                level="WARNING"
            )


    def checkNextEpisodePrompt(self, force=False):
        """
        Zeigt die nächste Folge einmalig.

        Ohne force gilt das in den letzten
        NEXT_EPISODE_PROMPT_BEFORE_END_SECONDS. force kommt vom Abspann,
        wenn der Abschnitt bis zum Dateiende reicht.
        """
        try:
            if self.stop_in_progress:
                return

            if self._next_episode_prompt_shown:
                return

            if self._segment_prompt_open or self._next_episode_dialog_open:
                if not force:
                    self._next_episode_timer.start(
                        self.NEXT_EPISODE_CHECK_INTERVAL_MS,
                        True
                    )
                return

            item_details = (
                self.item_details
                if isinstance(self.item_details, dict)
                else {}
            )

            if str(item_details.get("Type") or "").strip() != "Episode":
                return

            runtime = int(
                getattr(self, "runtime_seconds", 0) or 0
            )

            if runtime <= 0:
                return

            position = int(
                self.estimatedPositionSeconds() or 0
            )

            remembered_position = int(
                getattr(
                    self,
                    "last_known_position_seconds",
                    0
                ) or 0
            )

            if remembered_position > position:
                position = remembered_position

            position = max(
                0,
                min(position, runtime)
            )

            remaining_seconds = max(
                0,
                runtime - position
            )

            prompt_threshold = int(
                self.NEXT_EPISODE_PROMPT_BEFORE_END_SECONDS
            )

            if not force and remaining_seconds > prompt_threshold:
                self._next_episode_timer.start(
                    self.NEXT_EPISODE_CHECK_INTERVAL_MS,
                    True
                )
                return

            next_episode = self._next_episode_candidate

            # Die Zehn-Sekunden-Grenze ist erreicht, aber der Hintergrundjob
            # ist noch nicht fertig. In kurzen Abständen weiter prüfen.
            if not isinstance(next_episode, dict):
                if not self._next_episode_lookup_finished:
                    self._next_episode_timer.start(
                        500,
                        True
                    )

                return

            next_item_id = str(
                next_episode.get("Id") or ""
            ).strip()

            if not next_item_id:
                return

            self._next_episode_prompt_shown = True

            season_number = jellydream_int_or_default(
                next_episode.get("ParentIndexNumber"),
                1
            )

            episode_number = jellydream_int_or_default(
                next_episode.get("IndexNumber"),
                1
            )

            episode_name = str(
                next_episode.get("Name") or "Folge"
            ).strip()

            series_name = str(
                next_episode.get("SeriesName")
                or item_details.get("SeriesName")
                or ""
            ).strip()

            next_title = (
                "S%02dE%02d - %s"
                % (
                    season_number,
                    episode_number,
                    episode_name
                )
            )

            if series_name:
                episode_display = "%s\n%s" % (
                    series_name,
                    next_title
                )
            else:
                episode_display = next_title

            if remaining_seconds > int(
                self.NEXT_EPISODE_PROMPT_BEFORE_END_SECONDS
            ):
                question_text = _(
                    "Der Abspann läuft.\n\n"
                    "Möchtest du die nächste Folge abspielen?\n\n"
                    "%s"
                ) % episode_display
            else:
                question_text = (
                    "Die aktuelle Folge endet in wenigen Sekunden.\n\n"
                    "Möchtest du danach die nächste Folge abspielen?\n\n"
                    "%s"
                    % episode_display
                )

            jd_log(
                "Showing next-episode prompt at %ss remaining"                % remaining_seconds,
                level="INFO"
            )

            def next_episode_answer(answer):
                try:
                    self._next_episode_dialog_open = False

                    if self.stop_in_progress:
                        return

                    self._next_episode_decision = (
                        answer is True
                    )

                    if self._next_episode_decision:
                        jd_log(
                            "Next episode confirmed -> reporting the current episode as stopped and starting the next episode",
                            level="INFO"
                        )

                        # Sofortiges Beenden mit playback_finished=True:
                        #
                        # - Jellyfin erhält PositionTicks = komplette Laufzeit
                        # - die aktuelle Folge wird als regulär beendet behandelt
                        # - jellydream_handle_player_closed() startet anschließend
                        #   die bereits vorbereitete nächste Folge
                        self.playback.stop(
                            playback_finished=True
                        )

                    else:
                        jd_log(
                            "Next episode was declined or the prompt expired",
                            level="INFO"
                        )

                except Exception as e:
                    jd_log(
                        "Answer to the next-episode prompt could not be processed: %s" % e,
                        level="WARNING"
                    )

            if jellydream_next_episode_autoplay():
                jd_log(
                    "Next episode autoplay at %ss remaining"
                    % remaining_seconds,
                    level="INFO"
                )
                next_episode_answer(True)
                return

            self._next_episode_dialog_open = True

            self.session.openWithCallback(
                next_episode_answer,
                MessageBox,
                question_text,
                MessageBox.TYPE_YESNO,
                timeout=self.NEXT_EPISODE_PROMPT_TIMEOUT_SECONDS,
                default=False
            )

        except Exception as e:
            jd_log(
                "Next-episode prompt check failed: %s"               % e,
                level="WARNING"
            )
            
    def onPlaybackEOF(self):
        """
        EOF bedeutet nicht automatisch, dass ein Film oder eine Folge
        vollständig abgespielt wurde.

        Einige Enigma2-Player melden `evEOF` auch bei einem Streamproblem,
        bei einem Decoderwechsel oder nach einem Audio-Neustart. Deshalb
        erfolgt nach 1200 ms eine Positionsprüfung.
        """
        try:
            if self.stop_in_progress:
                return

            if getattr(self, "_ignore_eof", False):
                jd_log(
                    "EOF during a service restart ignored",
                    level="DEBUG"
                )
                return

            self._ignore_eof = True

            jd_log(
                "EOF detected; checking playback position in %d ms"                % self.EOF_VERIFY_DELAY_MS,
                level="DEBUG"
            )

            jellydream_restart_single_timer(
                self,
                "_eof_close_timer",
                self.closeAfterPlaybackEOF,
                self.EOF_VERIFY_DELAY_MS
            )

        except Exception as e:
            jd_log(
                "Error while handling the stream end: %s" % e,
                level="WARNING"
            )

    def closeAfterPlaybackEOF(self):
        """
        Prüft nach dem EOF, ob die Wiedergabe wirklich am Ende war.

        Nur dann wird `playback_finished=True` gesetzt. Das ist wichtig,
        damit bei einem defekten oder zu frühen EOF keine nächste Episode
        angeboten wird.
        """
        try:
            if self.stop_in_progress:
                return

            runtime = int(
                getattr(self, "runtime_seconds", 0) or 0
            )

            position = int(
                self.estimatedPositionSeconds() or 0
            )

            remembered_position = int(
                getattr(self, "last_known_position_seconds", 0) or 0
            )

            if remembered_position > position:
                position = remembered_position

            if runtime <= 0:
                jd_log(
                    "EOF was not confirmed as a normal end: runtime is unknown",
                    level="WARNING"
                )

                self.playback.stop(playback_finished=False)
                return

            # Beide Bedingungen müssen erfüllt sein:
            #
            # 1. Die Position liegt mindestens bei EOF_END_MIN_PERCENT.
            # 2. Es verbleiben höchstens EOF_END_TOLERANCE_SECONDS.
            #
            # Dadurch genügt bei langen Filmen nicht mehr allein ein großer
            # prozentualer Restbereich. Beispiel: Bei 3 Stunden wären 5 %
            # sonst noch neun Minuten – zu großzügig für ein echtes EOF.
            minimum_position_by_percent = int(
                runtime * int(self.EOF_END_MIN_PERCENT) / 100
            )

            end_threshold = max(
                minimum_position_by_percent,
                runtime - int(self.EOF_END_TOLERANCE_SECONDS)
            )

            playback_finished = position >= end_threshold

            jd_log(
                "EOF check: position=%ss, runtime=%ss, threshold=%ss, normal end=%s"                % (
                    position,
                    runtime,
                    end_threshold,
                    playback_finished
                ),
                level="INFO"
            )

            if playback_finished:
                jd_log(
                    "Playback ended normally -> closing the player",
                    level="INFO"
                )
            else:
                jd_log(
                    "EOF was too early or not conclusive. Not offering the next episode.",
                    level="WARNING"
                )

            self.playback.stop(
                playback_finished=playback_finished
            )

        except Exception as e:
            jd_log(
                "EOF check failed: %s"% e,
                level="ERROR"
            )

            try:
                self.playback.stop(playback_finished=False)
            except Exception:
                pass

    def clearAudioEOFGuard(self):
        try:
            self._ignore_eof = False

            jd_log(
                "EOF guard after audio restart cleared",
                level="DEBUG"
            )

        except Exception:
            pass

    def stopStream(self):
        return self.playback.stop(playback_finished=False)

    def seekBackSmall(self):
        self.seekRelativeSeconds(-30)

    def seekForwardSmall(self):
        self.seekRelativeSeconds(30)

    def seekBackBig(self):
        self.seekRelativeSeconds(-120)

    def seekForwardBig(self):
        self.seekRelativeSeconds(120)

    def seekVeryBackBig(self):
        self.seekRelativeSeconds(-300)

    def seekVeryForwardBig(self):
        self.seekRelativeSeconds(300)
