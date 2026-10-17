# -*- coding: utf-8 -*-

import os
import re
import threading

from time import strftime

from .constants import DEFAULT_LOG_LEVEL, LOG_FILE, LOG_MAX_SIZE

_LOG_LEVEL_LOCK = threading.RLock()

_LOG_LEVEL_VALUE = "INFO"


def jellydream_redact_secrets(value):

    try:
        text = str(value or "")

        # URL-Parameter:
        # ?api_key=xxx
        # &token=xxx
        # &access_token=xxx
        text = re.sub(
            r'(?i)([?&](?:api_key|access_token|token)=)[^&\s\'"]+',
            r'\1<REDACTED>',
            text
        )

        # Jellyfin Authorization:
        # Token="xxx"
        text = re.sub(
            r'(?i)(\bToken\s*=\s*")[^"]*(")',
            r'\1<REDACTED>\2',
            text
        )

        # HTTP-Header:
        # X-Emby-Token: xxx
        # X-MediaBrowser-Token: xxx
        text = re.sub(
            r'(?i)((?:X-Emby-Token|X-MediaBrowser-Token)'
            r'\s*[:=]\s*)[^\s,;\'"]+',
            r'\1<REDACTED>',
            text
        )

        # JSON-Werte:
        # "AccessToken": "xxx"
        # "access_token": "xxx"
        # "Password": "xxx"
        # "Pw": "xxx"
        text = re.sub(
            r'(?i)(["\']'
            r'(?:AccessToken|access_token|token|api_key|Password|Pw)'
            r'["\']\s*[:=]\s*["\'])'
            r'[^"\']*'
            r'(["\'])',
            r'\1<REDACTED>\2',
            text
        )

        return text

    except Exception:
        return "<LOG-REDACTION-ERROR>"


def jellydream_normalize_log_level(value):
    """
    Normalisiert einen Log-Level auf einen unterstützten Wert.
    """
    try:
        normalized_level = str(
            value or DEFAULT_LOG_LEVEL
        ).strip().upper()

        if normalized_level not in (
            "DEBUG",
            "INFO",
            "WARNING",
            "ERROR"
        ):
            normalized_level = DEFAULT_LOG_LEVEL

        return normalized_level

    except Exception:
        return DEFAULT_LOG_LEVEL



def jellydream_set_log_level_from_config(config_data=None):
    """
    Übernimmt den Log-Level aus einem bereits vorhandenen Config-Dict
    in den RAM.

    Diese Funktion führt selbst keinen read_config()-Aufruf aus.
    """
    global _LOG_LEVEL_VALUE

    try:
        if isinstance(config_data, dict):
            configured_level = config_data.get(
                "log_level",
                DEFAULT_LOG_LEVEL
            )
        else:
            configured_level = DEFAULT_LOG_LEVEL

        normalized_level = jellydream_normalize_log_level(
            configured_level
        )

        with _LOG_LEVEL_LOCK:
            _LOG_LEVEL_VALUE = normalized_level

        return normalized_level

    except Exception:
        with _LOG_LEVEL_LOCK:
            _LOG_LEVEL_VALUE = DEFAULT_LOG_LEVEL

        return DEFAULT_LOG_LEVEL

        
def jd_log(msg, level="INFO"):
    try:
        levels = {
            "DEBUG": 0,
            "INFO": 1,
            "WARNING": 2,
            "ERROR": 3
        }

        current_level = levels.get(get_log_level(), 1)
        msg_level = levels.get(str(level).upper(), 1)

        if msg_level < current_level:
            return

        clean_msg = jellydream_redact_secrets(msg)
        for duplicate in ["ERROR IN ", "ERROR: ", "ERROR ", "FEHLER IN ", "FEHLER: ", "FEHLER ", "ERROR IN", "FEHLER IN"]:
            if clean_msg.upper().startswith(duplicate):
                clean_msg = clean_msg[len(duplicate):].strip()
                break

        if clean_msg and clean_msg[0].islower():
            clean_msg = clean_msg[0].upper() + clean_msg[1:]

        timestamp = strftime("%Y-%m-%d %H:%M:%S")
        level_prefix = f"[{level}]" if level != "INFO" else ""

        line = f"{timestamp} [JellyDream]{level_prefix} {clean_msg}"

        if msg_level >= 1:
            print(line)

        try:
            if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > LOG_MAX_SIZE:
                with open(LOG_FILE, "w") as f:
                    f.write(f"{timestamp} [JellyDream] Logfile rotated due to size limit.\n")
        except Exception:
            pass

        with open(LOG_FILE, "a") as f:
            f.write(f"{line}\n")
            
    except Exception:
        pass

    
def get_log_level():
    """
    Liefert den aktuellen Log-Level direkt aus dem RAM.

    Dadurch verursacht jd_log() keine read_config()- und
    deepcopy()-Aufrufe mehr.
    """
    try:
        with _LOG_LEVEL_LOCK:
            return _LOG_LEVEL_VALUE

    except Exception:
        return DEFAULT_LOG_LEVEL
