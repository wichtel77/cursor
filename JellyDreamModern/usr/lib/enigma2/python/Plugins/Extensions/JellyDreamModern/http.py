# -*- coding: utf-8 -*-

import hashlib
import json
import os
import socket
import ssl
import threading

import urllib.request
import urllib.parse
import urllib.error

from Screens.MessageBox import MessageBox

from . import _

from .constants import (
    JELLYDREAM_USER_AGENT,
    JELLYDREAM_VERSION,
    JOB_PRIORITY_API_INTERACTIVE,
    TIMEOUT_LONG,
    TIMEOUT_NORMAL,
    TIMEOUT_SHORT
)
from .log import jd_log
from .workers import jellydream_dispatch_to_gui, jellydream_submit_api
from .config import (
    jellydream_device_id,
    jellydream_login_credentials,
    jellydream_store_server_profile,
    read_config,
    write_config
)
from .uiutil import jellydream_safe_set_status

_SSL_CONTEXT_LOCK = threading.RLock()

_SSL_CONTEXT_CACHE_KEY = None

_SSL_CONTEXT_CACHE_VALUE = None


def jellydream_is_timeout_error(error):
    try:
        reason = getattr(error, "reason", error)

        if isinstance(
            reason,
            (
                socket.timeout,
                TimeoutError
            )
        ):
            return True

        error_text = str(
            reason or error or ""
        ).lower()

        return (
            "timed out" in error_text
            or "timeout" in error_text
            or "zeitüberschreitung" in error_text
        )

    except Exception:
        return False

        
def jellyfin_http_result(ok=False, status=0, data=None, error=""):
    return {
        "ok": bool(ok),
        "status": int(status or 0),
        "data": data,
        "error": str(error or "")
    }


class JellyfinRequestError(Exception):
    """Kontrollierter Fehler für Jellyfin-HTTP-Anfragen."""

    def __init__(self, message, status=0, technical_error=""):
        Exception.__init__(self, message)
        self.status = int(status or 0)
        self.technical_error = str(technical_error or "")


def jellydream_translate_network_error(
    technical_error,
    fallback="Jellyfin-Server ist nicht erreichbar."
):
    """
    Übersetzt typische Python-, SSL- und Netzwerkfehler in eine
    verständliche deutsche Meldung.
    """
    technical_error = str(
        technical_error or ""
    ).strip()

    error_lower = technical_error.lower()

    if (
        "certificate verify failed" in error_lower
        or "unable to get local issuer certificate" in error_lower
        or "self signed certificate" in error_lower
        or "hostname mismatch" in error_lower
        or "certificate has expired" in error_lower
    ):
        return _(
            "Das SSL-Zertifikat des Jellyfin-Servers konnte "
            "nicht überprüft werden. Bitte die CA-Zertifikate "
            "der Box und die Zertifikatskette des Servers prüfen."
        )

    if (
        "timed out" in error_lower
        or "timeout" in error_lower
        or "zeitüberschreitung" in error_lower
    ):
        return _(
            "Der Jellyfin-Server antwortet nicht rechtzeitig."
        )

    if (
        "connection refused" in error_lower
        or "connection reset" in error_lower
        or "connection aborted" in error_lower
    ):
        return _(
            "Die Verbindung zum Jellyfin-Server wurde "
            "abgelehnt oder unterbrochen."
        )

    if (
        "network is unreachable" in error_lower
        or "no route to host" in error_lower
    ):
        return _(
            "Das Netzwerk oder der Jellyfin-Server ist "
            "nicht erreichbar."
        )

    if (
        "name or service not known" in error_lower
        or "temporary failure in name resolution" in error_lower
        or "nodename nor servname provided" in error_lower
        or "getaddrinfo failed" in error_lower
    ):
        return _(
            "Der Servername konnte nicht aufgelöst werden. "
            "Bitte die Jellyfin-Adresse und die DNS-Einstellungen prüfen."
        )

    if (
        "remote end closed connection" in error_lower
        or "remote disconnected" in error_lower
    ):
        return _(
            "Der Jellyfin-Server hat die Verbindung unerwartet beendet."
        )

    if (
        "wrong version number" in error_lower
        or "unknown protocol" in error_lower
    ):
        return _(
            "Die HTTPS-Verbindung konnte nicht aufgebaut werden. "
            "Bitte prüfen, ob HTTP und HTTPS korrekt konfiguriert sind."
        )

    text = str(fallback or "").strip()
    if not text:
        text = "Jellyfin-Server ist nicht erreichbar."
    return _(text)

    
def jellyfin_raise_request_error(result, context="Daten"):
    """
    Wandelt ein Ergebnis von jellyfin_api_*_ex() in eine verständliche
    Exception um. Diese Exception wird von runAsyncLoad() an den
    finished_callback als error_message weitergereicht.
    """
    if result and result.get("ok"):
        return

    result = result or {}

    try:
        status = int(result.get("status") or 0)
    except Exception:
        status = 0

    technical_error = str(
        result.get("error") or ""
    ).strip()

    error_lower = technical_error.lower()

    if status == 401:
        message = _(
            "Jellyfin hat diese Anfrage nicht autorisiert "
            "(HTTP 401). Die gespeicherte Sitzung wurde nicht "
            "automatisch gelöscht."
        )
    elif status == 403:
        message = _("Zugriff auf Jellyfin wurde verweigert.")
    elif status == 404:
        message = _("Jellyfin-Endpunkt wurde nicht gefunden.")
    elif status >= 500:
        message = _("Jellyfin-Serverfehler (HTTP %s).") % status
    elif (
        "timed out" in error_lower
        or "timeout" in error_lower
        or "zeitüberschreitung" in error_lower
    ):
        message = _("Jellyfin-Server antwortet nicht rechtzeitig.")

    elif (
        "certificate verify failed" in error_lower
        or "unable to get local issuer certificate" in error_lower
        or "self signed certificate" in error_lower
        or "hostname mismatch" in error_lower
    ):
        message = _(
            "Das SSL-Zertifikat des Jellyfin-Servers konnte "
            "nicht überprüft werden. Bitte die CA-Zertifikate "
            "der Box oder die Zertifikatskette des Servers prüfen."
        )
    elif (
        "connection refused" in error_lower
        or "connection reset" in error_lower
        or "network is unreachable" in error_lower
        or "name or service not known" in error_lower
        or "temporary failure" in error_lower
    ):
        message = _("Jellyfin-Server ist nicht erreichbar.")
    elif status:
        message = _("Jellyfin-Anfrage fehlgeschlagen (HTTP %s).") % status
    else:
        message = _("Jellyfin-Server ist nicht erreichbar.")

    jd_log(
        "%s failed: %s | technical: %s"
        % (
            context,
            message,
            technical_error or "Unbekannter Netzwerkfehler"
        ),
        level="WARNING"
    )

    raise JellyfinRequestError(
        message,
        status=status,
        technical_error=technical_error
    )


def jellydream_ssl_context(url):
    """
    Erstellt und cached den SSL-Kontext für HTTPS-Verbindungen.

    Der Kontext wird nur neu erzeugt, wenn sich `verify_ssl` oder
    `custom_ca_file` ändern.
    """
    global _SSL_CONTEXT_CACHE_KEY
    global _SSL_CONTEXT_CACHE_VALUE

    url = str(url or "").strip()

    if not url.lower().startswith("https://"):
        return None

    try:
        config_data = read_config() or {}
    except Exception:
        config_data = {}

    verify_ssl = str(
        config_data.get("verify_ssl", "1")
    ).strip() != "0"

    custom_ca_file = str(
        config_data.get("custom_ca_file", "") or ""
    ).strip()

    cache_key = (
        bool(verify_ssl),
        custom_ca_file
    )

    with _SSL_CONTEXT_LOCK:
        if (
            _SSL_CONTEXT_CACHE_KEY == cache_key
            and _SSL_CONTEXT_CACHE_VALUE is not None
        ):
            return _SSL_CONTEXT_CACHE_VALUE

        if not verify_ssl:
            jd_log(
                "WARNING: SSL certificate verification is disabled",
                level="WARNING"
            )

            context = ssl._create_unverified_context()

            _SSL_CONTEXT_CACHE_KEY = cache_key
            _SSL_CONTEXT_CACHE_VALUE = context

            return context

        ca_candidates = [
            custom_ca_file,
            os.environ.get("SSL_CERT_FILE", ""),
            "/etc/ssl/certs/ca-certificates.crt",
            "/etc/ssl/cert.pem",
            "/etc/pki/tls/certs/ca-bundle.crt",
            "/etc/ssl/ca-bundle.pem",
        ]

        for ca_file in ca_candidates:
            ca_file = str(ca_file or "").strip()

            if not ca_file or not os.path.isfile(ca_file):
                continue

            try:
                context = ssl.create_default_context(
                    cafile=ca_file
                )

                jd_log(
                    "Using SSL CA file: %s"
                    % ca_file,
                    level="DEBUG"
                )

                _SSL_CONTEXT_CACHE_KEY = cache_key
                _SSL_CONTEXT_CACHE_VALUE = context

                return context

            except Exception as e:
                jd_log(
                    "SSL CA file could not be loaded: %s | %s"
                    % (
                        ca_file,
                        e
                    ),
                    level="WARNING"
                )

        context = ssl.create_default_context()

        _SSL_CONTEXT_CACHE_KEY = cache_key
        _SSL_CONTEXT_CACHE_VALUE = context

        return context


def jellydream_urlopen(request, timeout):
    """
    Öffnet HTTP- und HTTPS-Anfragen zentral mit dem passenden SSL-Kontext.
    """
    url = str(
        getattr(request, "full_url", "") or ""
    ).strip()

    ssl_context = jellydream_ssl_context(url)

    if ssl_context is None:
        return urllib.request.urlopen(
            request,
            timeout=timeout
        )

    return urllib.request.urlopen(
        request,
        timeout=timeout,
        context=ssl_context
    )

    
def jellyfin_api_get_ex(url, timeout=TIMEOUT_NORMAL, auth_snapshot=None):
    if not isinstance(auth_snapshot, dict):
        auth_snapshot = jellydream_auth_snapshot()

    try:
        req = urllib.request.Request(
            url,
            headers=jellyfin_headers(auth_snapshot)
        )

        with jellydream_urlopen(req, timeout) as r:
            raw = r.read().decode("utf-8")
            data = json.loads(raw) if raw else {}

            return jellyfin_http_result(
                ok=True,
                status=getattr(r, "status", 200),
                data=data,
                error=""
            )

    except urllib.error.HTTPError as e:
        try:
            error_body = e.read().decode(
                "utf-8",
                errors="ignore"
            )
        except Exception:
            error_body = str(e)

        jd_log(
            "API GET HTTP error %s for URL: %s | %s"            % (e.code, url, error_body),
            level="ERROR"
        )

        jellydream_handle_auth_failure(
            e.code,
            "jellyfin_api_get_ex",
            auth_snapshot
        )

        return jellyfin_http_result(
            ok=False,
            status=e.code,
            data=None,
            error=error_body or str(e)
        )

    except (socket.timeout, TimeoutError) as e:
        jd_log(
            "API GET timed out after %ss for URL: %s | %s"           % (
                timeout,
                url,
                e
            ),
            level="ERROR"
        )

        return jellyfin_http_result(
            ok=False,
            status=0,
            data=None,
            error="HTTPS-Zeitüberschreitung nach %s Sekunden: %s"
            % (
                timeout,
                e
            )
        )
    
    except urllib.error.URLError as e:
        if jellydream_is_timeout_error(e):
            jd_log(
                "API GET timed out after %ss for URL: %s | %s"                % (
                    timeout,
                    url,
                    e
                ),
                level="WARNING"
            )

            return jellyfin_http_result(
                ok=False,
                status=0,
                data=None,
                error=(
                    "HTTPS-Zeitüberschreitung nach "
                    "%s Sekunden: %s"
                    % (
                        timeout,
                        e
                    )
                )
            )

        jd_log(
            "API GET network error for URL: %s | %s"            % (
                url,
                e
            ),
            level="WARNING"
        )

        return jellyfin_http_result(
            ok=False,
            status=0,
            data=None,
            error=str(e)
        )

    except Exception as e:
        jd_log(
            "API GET error for URL: %s | exception: %s"            % (url, e),
            level="ERROR"
        )

        return jellyfin_http_result(
            ok=False,
            status=0,
            data=None,
            error=str(e)
        )


def jellyfin_items_api_get_ex(
    route_suffix="",
    query_params=None,
    timeout=TIMEOUT_NORMAL,
    auth_snapshot=None
):
    """
    Ruft moderne Jellyfin-/Items-Routen auf.

    Reihenfolge:
    1. /Items...
    2. Fallback auf /Users/{user_id}/Items...

    Der Fallback erfolgt ausschließlich, wenn die moderne Route vom
    Server nicht unterstützt wird. Netzwerk-, Authentifizierungs- und
    Serverfehler lösen keinen zweiten Request aus.
    """
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
        return jellyfin_http_result(
            ok=False,
            status=0,
            data=None,
            error="Jellyfin-Anmeldung ist unvollständig."
        )

    route_suffix = str(
        route_suffix or ""
    ).strip().strip("/")

    if route_suffix:
        route_suffix = "/%s" % route_suffix
    else:
        route_suffix = ""

    modern_query = dict(
        query_params
        if isinstance(query_params, dict)
        else {}
    )

    # Moderne /Items-Routen benötigen den Benutzer als Query-Parameter.
    if not modern_query.get("userId"):
        modern_query["userId"] = user_id

    legacy_query = dict(modern_query)

    # Bei der alten Route steckt die Benutzer-ID bereits im Pfad.
    legacy_query.pop("userId", None)
    legacy_query.pop("UserId", None)

    def build_url(base_url, params):
        cleaned_params = {}

        for key, value in params.items():
            if value is None:
                continue

            cleaned_params[str(key)] = value

        query_string = urllib.parse.urlencode(
            cleaned_params,
            doseq=True
        )

        if query_string:
            return "%s?%s" % (
                base_url,
                query_string
            )

        return base_url

    modern_url = build_url(
        "%s/Items%s" % (
            server,
            route_suffix
        ),
        modern_query
    )

    modern_result = jellyfin_api_get_ex(
        modern_url,
        timeout=timeout,
        auth_snapshot=auth_snapshot
    )

    if modern_result.get("ok"):
        return modern_result

    try:
        modern_status = int(
            modern_result.get("status") or 0
        )
    except Exception:
        modern_status = 0

    # Keine doppelten Requests bei Token-, Netzwerk- oder Serverfehlern.
    if modern_status not in (404, 405, 501):
        return modern_result

    legacy_url = build_url(
        "%s/Users/%s/Items%s" % (
            server,
            urllib.parse.quote(
                user_id,
                safe=""
            ),
            route_suffix
        ),
        legacy_query
    )

    jd_log(
        "Modern Jellyfin /Items route is not available (HTTP %s) -> using legacy fallback: %s"
        % (
            modern_status,
            legacy_url
        ),
        level="INFO"
    )

    return jellyfin_api_get_ex(
        legacy_url,
        timeout=timeout,
        auth_snapshot=auth_snapshot
    )

    
def jellyfin_api_read_ex(url, timeout=TIMEOUT_LONG, headers=None, auth_snapshot=None):
    if not isinstance(auth_snapshot, dict):
        auth_snapshot = jellydream_auth_snapshot()

    try:
        req_headers = jellyfin_headers(auth_snapshot)

        if headers and isinstance(headers, dict):
            req_headers.update(headers)

        req = urllib.request.Request(
            url,
            headers=req_headers
        )

        with jellydream_urlopen(req, timeout) as r:
            return jellyfin_http_result(
                ok=True,
                status=getattr(r, "status", 200),
                data=r.read(),
                error=""
            )

    except urllib.error.HTTPError as e:
        try:
            error_body = e.read()
        except Exception:
            error_body = b""

        jd_log(
            "API READ HTTP error %s for URL: %s"            % (e.code, url),
            level="ERROR"
        )

        jellydream_handle_auth_failure(
            e.code,
            "jellyfin_api_read_ex",
            auth_snapshot
        )

        return jellyfin_http_result(
            ok=False,
            status=e.code,
            data=error_body,
            error=str(e)
        )

    except (socket.timeout, TimeoutError) as e:
        jd_log(
            "API READ timed out after %ss for URL: %s | %s"           % (
                timeout,
                url,
                e
            ),
            level="ERROR"
        )

        return jellyfin_http_result(
            ok=False,
            status=0,
            data=None,
            error="HTTPS-Zeitüberschreitung nach %s Sekunden: %s"
            % (
                timeout,
                e
            )
        )
    
    except urllib.error.URLError as e:
        if jellydream_is_timeout_error(e):
            jd_log(
                "API READ timed out after %ss for URL: %s | %s"                % (
                    timeout,
                    url,
                    e
                ),
                level="WARNING"
            )

            return jellyfin_http_result(
                ok=False,
                status=0,
                data=None,
                error=(
                    "HTTPS-Zeitüberschreitung nach "
                    "%s Sekunden: %s"
                    % (
                        timeout,
                        e
                    )
                )
            )

        jd_log(
            "API READ network error for URL: %s | %s"            % (
                url,
                e
            ),
            level="WARNING"
        )

        return jellyfin_http_result(
            ok=False,
            status=0,
            data=None,
            error=str(e)
        )

    except Exception as e:
        jd_log(
            "API READ error for URL: %s | %s"            % (url, e),
            level="ERROR"
        )

        return jellyfin_http_result(
            ok=False,
            status=0,
            data=None,
            error=str(e)
        )


def jellyfin_api_post_ex(url, payload, timeout=TIMEOUT_LONG, headers=None, authenticated=True, auth_snapshot=None):
    if authenticated and not isinstance(auth_snapshot, dict):
        auth_snapshot = jellydream_auth_snapshot()

    try:
        data = json.dumps(payload).encode("utf-8")

        if authenticated:
            req_headers = jellyfin_headers(auth_snapshot)
        else:
            req_headers = {
                "Accept": "application/json"
            }

        req_headers["Content-Type"] = "application/json"

        if headers and isinstance(headers, dict):
            req_headers.update(headers)

        req = urllib.request.Request(
            url,
            data=data,
            headers=req_headers,
            method="POST"
        )

        with jellydream_urlopen(req, timeout) as r:
            response_raw = r.read().decode("utf-8")
            response_data = (
                json.loads(response_raw)
                if response_raw
                else True
            )

            return jellyfin_http_result(
                ok=True,
                status=getattr(r, "status", 200),
                data=response_data,
                error=""
            )

    except urllib.error.HTTPError as e:
        try:
            error_body = e.read().decode(
                "utf-8",
                errors="ignore"
            )
        except Exception:
            error_body = str(e)

        jd_log(
            "API POST HTTP error %s for URL: %s | %s"            % (e.code, url, error_body),
            level="ERROR"
        )

        if authenticated:
            jellydream_handle_auth_failure(
                e.code,
                "jellyfin_api_post_ex",
                auth_snapshot
            )

        return jellyfin_http_result(
            ok=False,
            status=e.code,
            data=None,
            error=error_body or str(e)
        )

    except (socket.timeout, TimeoutError) as e:
        jd_log(
            "API POST timed out after %ss for URL: %s | %s"           % (
                timeout,
                url,
                e
            ),
            level="ERROR"
        )

        return jellyfin_http_result(
            ok=False,
            status=0,
            data=None,
            error="HTTPS-Zeitüberschreitung nach %s Sekunden: %s"
            % (
                timeout,
                e
            )
        )
    
    except urllib.error.URLError as e:
        if jellydream_is_timeout_error(e):
            jd_log(
                "API POST timed out after %ss for URL: %s | %s"                % (
                    timeout,
                    url,
                    e
                ),
                level="WARNING"
            )

            return jellyfin_http_result(
                ok=False,
                status=0,
                data=None,
                error=(
                    "HTTPS-Zeitüberschreitung nach "
                    "%s Sekunden: %s"
                    % (
                        timeout,
                        e
                    )
                )
            )

        jd_log(
            "API POST network error for URL: %s | %s"            % (
                url,
                e
            ),
            level="WARNING"
        )

        return jellyfin_http_result(
            ok=False,
            status=0,
            data=None,
            error=str(e)
        )

    except Exception as e:
        jd_log(
            "API POST error for URL: %s | exception: %s"            % (url, e),
            level="ERROR"
        )

        return jellyfin_http_result(
            ok=False,
            status=0,
            data=None,
            error=str(e)
        )

        
def jellyfin_api_delete_ex(url, timeout=TIMEOUT_LONG, headers=None, authenticated=True, auth_snapshot=None):
    if authenticated and not isinstance(auth_snapshot, dict):
        auth_snapshot = jellydream_auth_snapshot()

    try:
        if authenticated:
            req_headers = jellyfin_headers(auth_snapshot)
        else:
            req_headers = {
                "Accept": "application/json"
            }

        if headers and isinstance(headers, dict):
            req_headers.update(headers)

        req = urllib.request.Request(
            url,
            data=None,
            headers=req_headers,
            method="DELETE"
        )

        with jellydream_urlopen(req, timeout) as response:
            response_raw = response.read().decode("utf-8")
            response_data = (
                json.loads(response_raw)
                if response_raw
                else True
            )

            return jellyfin_http_result(
                ok=True,
                status=getattr(response, "status", 200),
                data=response_data,
                error=""
            )

    except urllib.error.HTTPError as e:
        try:
            error_body = e.read().decode("utf-8", errors="ignore")
        except Exception:
            error_body = str(e)

        jd_log(
            "API DELETE HTTP error %s for URL: %s | %s"
            % (e.code, url, error_body),
            level="ERROR"
        )

        return jellyfin_http_result(
            ok=False,
            status=e.code,
            data=None,
            error=error_body or str(e)
        )

    except Exception as e:
        jd_log(
            "API DELETE error for URL: %s | exception: %s"
            % (url, e),
            level="ERROR"
        )

        return jellyfin_http_result(
            ok=False,
            status=0,
            data=None,
            error=str(e)
        )


def jellydream_api_post_async(
    url,
    payload,
    timeout=TIMEOUT_LONG,
    finished_callback=None,
    priority=JOB_PRIORITY_API_INTERACTIVE
):
    def deliver_result(result, error_message=""):
        if not callable(finished_callback):
            return True

        return jellydream_dispatch_to_gui(
            lambda cb=finished_callback,
            callback_result=result,
            callback_error=str(error_message or ""): cb(
                callback_result,
                callback_error
            )
        )

    def worker():
        result = False
        error_message = ""

        try:
            post_result = jellyfin_api_post_ex(
                url,
                payload,
                timeout=timeout
            )

            if post_result.get("ok"):
                result = post_result.get("data")
            else:
                result = False
                error_message = str(
                    post_result.get("error") or ""
                )

        except Exception as e:
            error_message = str(e)

            jd_log(
                "Async Jellyfin POST failed: %s"
                % e,
                level="WARNING"
            )

        deliver_result(
            result,
            error_message
        )

    started = jellydream_submit_api(
        worker,
        priority=priority
    )

    if not started:
        error_message = _(
            "Jellyfin-POST konnte nicht gestartet werden, "
            "weil der Hintergrund-Worker-Pool ausgelastet ist."
        )

        jd_log(
            "Jellyfin POST could not be started because the "
            "background worker pool is busy",
            level="WARNING"
        )

        deliver_result(
            False,
            error_message
        )

    return started


def jellyfin_assert_server_online(timeout=TIMEOUT_SHORT):
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
        raise JellyfinRequestError(
            "Jellyfin-Anmeldung ist unvollständig."
        )

    # Moderner Endpunkt
    url = "%s/Users/Me" % server

    result = jellyfin_api_get_ex(
        url,
        timeout=timeout,
        auth_snapshot=auth_snapshot
    )

    try:
        status = int(result.get("status") or 0)
    except Exception:
        status = 0

    # Legacy-Fallback für ältere Jellyfin-Versionen
    if not result.get("ok") and status in (404, 405, 501):
        legacy_url = "%s/Users/%s" % (
            server,
            urllib.parse.quote(
                user_id,
                safe=""
            )
        )

        jd_log(
            "/Users/Me is not supported by the server (HTTP %s) -> using legacy endpoint /Users/{userId}"
            % status,
            level="INFO"
        )

        result = jellyfin_api_get_ex(
            legacy_url,
            timeout=timeout,
            auth_snapshot=auth_snapshot
        )

    if not result.get("ok"):
        jellyfin_raise_request_error(
            result,
            "Jellyfin-Health-Check"
        )

    data = result.get("data")

    if not isinstance(data, dict):
        raise JellyfinRequestError(
            "Der Jellyfin-Health-Check lieferte "
            "keine gültigen Benutzerdaten."
        )

    returned_user_id = str(
        data.get("Id") or ""
    ).strip()

    if not returned_user_id:
        raise JellyfinRequestError(
            "Der Jellyfin-Health-Check lieferte "
            "keine Benutzer-ID."
        )

    if returned_user_id != user_id:
        raise JellyfinRequestError(
            "Der gespeicherte Jellyfin-Token gehört zu "
            "einem anderen Benutzer."
        )

    jd_log(
        "Jellyfin health check succeeded: user=%s"
        % returned_user_id,
        level="DEBUG"
    )

    return True


def jellydream_auth_snapshot():
    """
    Erstellt einen unveränderlichen Stand der aktuell verwendeten Anmeldung.

    Ein verspätetes HTTP-401 darf nur genau diese Sitzung löschen. Falls
    inzwischen ein neuer Login durchgeführt wurde, unterscheiden sich Token,
    Benutzer-ID oder Server und das alte 401 wird ignoriert.
    """
    credentials = jellydream_login_credentials()

    return {
        "server": credentials.get("server", ""),
        "token": credentials.get("token", ""),
        "user_id": credentials.get("user_id", "")
    }

    
def jellydream_auth_snapshot_matches_current(snapshot):
    """
    Prüft, ob die gespeicherte Sitzung noch exakt der Sitzung entspricht,
    mit der die HTTP-Anfrage gestartet wurde.
    """
    try:
        if not isinstance(snapshot, dict):
            return False

        current = jellydream_auth_snapshot()

        return (
            str(snapshot.get("server") or "").rstrip("/")
            == str(current.get("server") or "").rstrip("/")
            and str(snapshot.get("user_id") or "")
            == str(current.get("user_id") or "")
            and str(snapshot.get("token") or "")
            == str(current.get("token") or "")
        )

    except Exception:
        return False


def jellydream_auth_snapshot_key(snapshot):
    """
    Erzeugt einen nicht sensiblen Schlüssel für eine konkrete Anmeldung.

    Der Schlüssel wird ausschließlich zur Trennung paralleler Async-Jobs
    verwendet. Der Token selbst wird nicht in RAM-Job-Keys oder Logs
    gespeichert.
    """
    try:
        if not isinstance(snapshot, dict):
            return ""

        identity = "%s\n%s\n%s" % (
            str(snapshot.get("server") or "").strip().rstrip("/"),
            str(snapshot.get("user_id") or "").strip(),
            str(snapshot.get("token") or "").strip()
        )

        return hashlib.sha256(
            identity.encode("utf-8")
        ).hexdigest()[:20]

    except Exception:
        return ""

        
def jellyfin_headers(auth_snapshot=None):
    """
    Erstellt die Authentifizierungsheader für Jellyfin.

    Der Zugriffstoken wird auf drei kompatiblen Wegen übertragen:

    1. Im Authorization-Header als Token="..."
    2. Über X-Emby-Token
    3. Zusätzlich über X-MediaBrowser-Token für ältere Systeme
    """
    if not isinstance(auth_snapshot, dict):
        auth_snapshot = jellydream_auth_snapshot()

    token = str(
        auth_snapshot.get("token") or ""
    ).strip()

    device_id = jellydream_device_id()

    authorization = (
        'MediaBrowser '
        'Client="JellyDreamModern", '
        'Device="Enigma2 Box", '
        'DeviceId="%s", '
        'Version="%s"'
        % (device_id, JELLYDREAM_VERSION)
    )

    if token:
        authorization += (
            ', Token="%s"'
            % token
        )

    headers = {
        "Accept": "application/json",
        "Authorization": authorization,
        "X-Emby-Authorization": authorization,
        "User-Agent": JELLYDREAM_USER_AGENT
    }

    if token:
        # Von Jellyfin verwendeter Token-Header.
        headers["X-Emby-Token"] = token

        # Zusätzliche Kompatibilität mit älteren MediaBrowser-Systemen.
        headers["X-MediaBrowser-Token"] = token

    return headers


def jellydream_build_authenticated_player_url(
    stream_url,
    auth_snapshot=None
):
    """
    Ergänzt die Jellyfin-Stream-URL um Enigma2-HTTP-Header.

    Der api_key bleibt aus Kompatibilitätsgründen zusätzlich in der
    normalen Stream-URL erhalten.

    Enigma2 interpretiert den Teil hinter `#` als HTTP-Header für den
    verwendeten IPTV-Player. Dadurch erhalten auch HLS-Folgeanfragen
    den Jellyfin-Token.
    """
    try:
        stream_url = str(
            stream_url or ""
        ).strip()

        if not stream_url:
            return ""

        if not isinstance(auth_snapshot, dict):
            auth_snapshot = jellydream_auth_snapshot()

        token = str(
            auth_snapshot.get("token") or ""
        ).strip()

        if not token:
            jd_log(
                "Player URL could not be authenticated: Jellyfin token is missing",
                level="WARNING"
            )
            return stream_url

        device_id = jellydream_device_id()

        authorization = (
            'MediaBrowser '
            'Client="JellyDreamModern", '
            'Device="Enigma2 Box", '
            'DeviceId="%s", '
            'Version="%s", '
            'Token="%s"'
            % (
                device_id,
                JELLYDREAM_VERSION,
                token
            )
        )

        # Als Liste definieren, damit die Reihenfolge auch auf älteren
        # Python-Versionen eindeutig bleibt.
        player_headers = [
            (
                "Authorization",
                authorization
            ),
            (
                "X-Emby-Authorization",
                authorization
            ),
            (
                "X-Emby-Token",
                token
            ),
            (
                "X-MediaBrowser-Token",
                token
            ),
            (
                "User-Agent",
                JELLYDREAM_USER_AGENT
            )
        ]

        encoded_headers = urllib.parse.urlencode(
            player_headers,
            quote_via=urllib.parse.quote
        )

        player_url = "%s#%s" % (
            stream_url,
            encoded_headers
        )

        jd_log(
            "Jellyfin authentication headers were attached to the Enigma2 player URL",
            level="DEBUG"
        )

        return player_url

    except Exception as e:
        jd_log(
            "Authenticated player URL could not be built: %s" % e,
            level="WARNING"
        )

        return str(stream_url or "").strip()


def jellyfin_is_logged_in():
    """
    Prüft ausschließlich über dieselbe zentrale Auflösung wie alle
    Jellyfin-Anfragen und HTTP-Auth-Snapshots.
    """
    credentials = jellydream_login_credentials()

    return bool(
        credentials.get("server")
        and credentials.get("token")
        and credentials.get("user_id")
    )

    
def jellydream_require_login(screen, message="Bitte zuerst einloggen."):
    try:
        if jellyfin_is_logged_in():
            return True

        message = _(message)
        jellydream_safe_set_status(screen, message, level="WARNING")

        if hasattr(screen, "session") and getattr(screen, "session", None):
            try:
                screen.session.open(
                    MessageBox,
                    message,
                    MessageBox.TYPE_INFO,
                    timeout=3
                )
            except Exception:
                pass

        return False
    except Exception as e:
        try:
            jd_log(f"Error in jellydream_require_login: {e}", level="WARNING")
        except Exception:
            pass
        return False


def jellydream_handle_auth_failure(
    status=0,
    context="",
    auth_snapshot=None
):
    """
    Behandelt HTTP 401 nur als Fehler der konkreten Anfrage.

    Ein einzelnes 401 darf die global gespeicherte Sitzung nicht löschen.
    Ob der Token insgesamt noch gültig ist, wird ausschließlich durch den
    zentralen Health-Check über /Users/Me festgestellt.
    """
    try:
        if int(status or 0) != 401:
            return False

        if not isinstance(auth_snapshot, dict):
            auth_snapshot = jellydream_auth_snapshot()

        context_text = (
            " in %s" % context
            if context
            else ""
        )

        if not jellydream_auth_snapshot_matches_current(
            auth_snapshot
        ):
            jd_log(
                "Late HTTP 401%s ignored: the login has changed in the meantime."                % context_text,
                level="INFO"
            )

            return False

        jd_log(
            "HTTP 401%s for a single Jellyfin request. The saved session is kept and is not deleted automatically."            % context_text,
            level="WARNING"
        )

        return True

    except Exception as e:
        jd_log(
            "HTTP 401 could not be handled: %s"
            % e,
            level="WARNING"
        )

        return False

        
def jellyfin_login(
    server,
    username,
    password,
    profile_index=0
):
    try:
        server = str(
            server or ""
        ).strip().rstrip("/")

        username = str(
            username or ""
        ).strip()

        password = str(
            password or ""
        )

        if not server or not username:
            return False, _("Server oder Benutzer fehlt")

        if not server.lower().startswith(
            ("http://", "https://")
        ):
            if ":8096" in server:
                servers = [
                    "http://" + server
                ]
            else:
                servers = [
                    "https://" + server,
                    "http://" + server
                ]
        else:
            servers = [server]

        body = {
            "Username": username,
            "Pw": password
        }

        device_id = jellydream_device_id()

        authorization = (
            'MediaBrowser '
            'Client="JellyDreamModern", '
            'Device="Enigma2 Box", '
            'DeviceId="%s", '
            'Version="%s"'
            % (device_id, JELLYDREAM_VERSION)
        )

        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
            "Authorization": authorization,
            "X-Emby-Authorization": authorization,
            "User-Agent": JELLYDREAM_USER_AGENT
        }

        last_error = ""
        last_status = 0
        data = {}
        used_server = ""

        for server_url in servers:
            result = jellyfin_api_post_ex(
                "%s/Users/AuthenticateByName"
                % server_url,
                body,
                timeout=TIMEOUT_NORMAL,
                headers=headers,
                authenticated=False
            )

            if result.get("ok"):
                data = result.get("data") or {}
                used_server = server_url
                break

            last_status = int(
                result.get("status") or 0
            )

            technical_login_error = str(
                result.get("error") or ""
            ).strip()

            jd_log(
                "Login attempt for URL failed: %s | HTTP %s | %s"
                % (
                    server_url,
                    last_status,
                    technical_login_error
                    or "Keine Servermeldung"
                ),
                level="WARNING"
            )

            if last_status == 400:
                return (
                    False,
                    "Jellyfin hat die Login-Anfrage abgelehnt "
                    "(HTTP 400). Bitte Serverprotokoll, Geräte-ID "
                    "und Authentifizierungs-Plugin prüfen."
                )

            if last_status == 401:
                return (
                    False,
                    "Benutzername oder Passwort falsch"
                )

            if last_status == 403:
                return (
                    False,
                    "Die Anmeldung wurde vom Server verweigert"
                )

            if last_status == 404:
                last_error = (
                    "Login-Endpunkt nicht gefunden"
                )

            elif last_status >= 500:
                last_error = (
                    "Jellyfin-Serverfehler"
                )

        if not used_server:
            return (
                False,
                last_error or "Login fehlgeschlagen"
            )

        token = str(
            data.get("AccessToken") or ""
        ).strip()

        user = data.get("User") or {}

        user_id = str(
            user.get("Id") or ""
        ).strip()

        if not token or not user_id:
            return (
                False,
                _("Login ohne gültigen Token oder Benutzer-ID")
            )

        full_cfg = read_config() or {}

        full_cfg = jellydream_store_server_profile(
            full_cfg,
            profile_index,
            used_server,
            username,
            user_id,
            token
        )

        if not write_config(full_cfg):
            return (
                False,
                _(
                    "Login erfolgreich, aber das Serverprofil "
                    "konnte nicht gespeichert werden"
                )
            )

        jd_log(
            "Login succeeded for user '%s' on server slot %s: %s"
            % (
                username,
                int(profile_index) + 1,
                used_server
            ),
            level="INFO"
        )

        return True, username

    except Exception as e:
        jd_log(
            "Login for user '%s' failed: %s"
            % (
                username,
                e
            ),
            level="ERROR"
        )

        return False, jellydream_translate_network_error(
            str(e),
            fallback="Unbekannter Fehler bei der Anmeldung."
        )
