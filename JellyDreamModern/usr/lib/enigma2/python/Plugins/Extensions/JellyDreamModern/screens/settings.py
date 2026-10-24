# -*- coding: utf-8 -*-

import os

from Components.ActionMap import ActionMap
from Components.ConfigList import ConfigListScreen
from Components.config import config, ConfigSelection, ConfigNothing
from Components.Label import Label
from Components.Pixmap import Pixmap
from enigma import ePoint, eSize, fontRenderClass, gFont
from skin import parseColor

from .. import _
from .. import jellydream_resolve_ui_language
from .. import localeInit

from ..constants import JELLYDREAM_VERSION, MAX_JELLYFIN_SERVER_PROFILES, PLUGIN_DIR
from ..log import jd_log
from ..workers import jellydream_dispatch_to_gui, jellydream_submit_maintenance
from ..config import (
    jellydream_normalize_server_profiles,
    jellydream_player_service_choices,
    jellyfin_server,
    jellyfin_username,
    read_config,
    write_config
)
from ..http import jellyfin_is_logged_in
from ..uiutil import jellydream_safe_config_selection_value, jellydream_safe_open
from .. import library as jd_library
from ..library import (
    jellydream_clear_metadata_caches,
    jellydream_library_update_log_text,
    jellydream_refresh_library_cache,
    jellydream_restart_library_scheduler,
    jellyfin_get_database_counts
)
from ..ui import JellyDreamBaseScreen, jellydream_apply_title_icon
from .login import JellyDreamLoginScreen


class JellyDreamSettingsScreen(JellyDreamBaseScreen, ConfigListScreen):
    SCREEN_NAME = "JellyDreamSettingsScreen"
    AUTO_STATUS = True

    def __init__(self, session):
        JellyDreamBaseScreen.__init__(self, session)
        
        self["config_shield"] = Pixmap()
        self["language_flag"] = Pixmap()
        self["title"] = Label(_("Einstellungen"))
        self["title_icon"] = Pixmap()

        self["settings_title"] = Label(_("Server"))
        self["settings_box"] = Label("")
        self["login_title"] = Label(_("Login"))
        self["login_box"] = Label("")
        self["info_title"] = Label(_("Datenbank"))
        self["info_box"] = Label(_("Filme: -\nSerien: -\nEpisoden: -\nSammlungen: -"))
        self["help_box"] = Label("")
        self["version"] = Label(_("Version %s") % JELLYDREAM_VERSION)
        current_cfg = read_config() or {}

        self.help_texts = {
            _("Server bearbeiten/hinzufügen:"): _(
                "Öffnet die Serververwaltung. Dort können bis zu drei "
                "Jellyfin-Server eingerichtet, vorhandene Server bearbeitet "
                "oder bereits gespeicherte Serverprofile aktiviert werden. "
                "Passwörter werden nicht gespeichert."
            ),
            _("Maximale Video-Auflösung:"): _("Legt fest, in welcher maximalen Auflösung JellyDream Inhalte streamen darf. Höhere Auflösungen liefern bessere Bildqualität, benötigen aber mehr Bandbreite."),            
            _("Streaming-Modus:"): _("Legt fest, wie JellyDream Streams abspielt. 'Direktstream bevorzugen' nutzt die Originalformate des Servers und funktioniert oft am besten, wenn dein Receiver HEVC/H.265 und EAC3 direkt unterstützt. 'Stabilität bevorzugen' erzwingt eher kompatible Formate per Transcoding. 'Automatisch' mischt beide Verfahren, kann aber je nach Receiver bei einzelnen Codec-Kombinationen wie HEVC + EAC3 zu Problemen führen."),
            _("Transcoding Video-Bitrate:"): _("Hier stellst du die maximale Datenrate für umgewandelte Streams ein. Niedrigere Werte sind besser für langsame Netzwerke, höhere Werte liefern bessere Bildqualität."),
            _("Cover-/Info-Refresh-Verzögerung:"): _(
                "Steuert, wie schnell Poster, Backdrops und Detailinformationen beim Navigieren aktualisiert werden. Kleinere Werte reagieren schneller, können aber die Box stärker belasten."
            ),
            _("Image-Worker für Cover und Backdrops:"): _("Legt fest, wie viele Bilder JellyDream gleichzeitig im Hintergrund laden darf. Zwei Worker laden Poster schneller. Auf schwachen oder älteren Boxen reduziert ein Worker die gleichzeitige CPU-, RAM- und Netzwerkbelastung. Nach einer Änderung wird ein GUI-Neustart benötigt."),
            _("Im Receiver-Hauptmenü anzeigen:"): _("Legt fest, ob JellyDream zusätzlich direkt im Hauptmenü des Receivers angezeigt werden soll."),
            _("Plugin-Sprache:"): _(
                "Legt die Sprache der JellyDream-Oberfläche fest. "
                "'Automatisch' verwendet die Receiversprache, "
                "sofern Deutsch, English, Français, Italiano, Español, "
                "Polski, Nederlands, Türkçe, العربية oder Русский "
                "verfügbar ist. "
                "Andernfalls wird English verwendet."
            ),
            _("Plugin Log-Level (Debugging):"): _("Bestimmt, wie viele technische Meldungen ins Log geschrieben werden. 'INFO' ist für den normalen Betrieb empfohlen, 'DEBUG' nur zur Fehlersuche."),
            _("Dynamische Hintergründe:"): _(
                "Legt fest, ob beim Navigieren passende Jellyfin-Backdrops "
                "als Vollbild-Hintergrund geladen und angezeigt werden. "
                "Bei 'Nein' verwendet JellyDream immer den Standardhintergrund. "
                "Das reduziert auf älteren oder langsameren OpenPLi-Boxen "
                "CPU-, RAM-, Netzwerk- und Repaint-Belastung."
            ),
            _("LCD-Anzeige:"): _(
                "Zeigt während der Wiedergabe eine eigene Ansicht mit Titel, "
                "Fortschritt und Zeit. Bei 'Nein' verwendet das Display die "
                "normale Wiedergabeansicht mit Titel, Sendung und "
                "Fortschrittsbalken."
            ),
            _("AC3 direkt ausgeben:"): _("Legt fest, ob AC3-Tonspuren ohne Umwandlung direkt an den Receiver bzw. Fernseher gesendet werden dürfen. Wenn dein TV bei AC3 'Audioformat nicht unterstützt' meldet, stelle hier 'Nein' ein."),
            _("EAC3 direkt ausgeben:"): _("Legt fest, ob EAC3-/Dolby-Digital-Plus-Tonspuren direkt ausgegeben werden dürfen. Falls dein Gerät damit Probleme hat, stelle hier 'Nein' ein, damit JellyDream in ein kompatibleres Format umwandelt."),
            _("Dolby TrueHD / Atmos direkt ausgeben:"): _(
                "Legt fest, ob Dolby TrueHD inklusive Dolby Atmos unverändert "
                "über HDMI ausgegeben werden darf. Dies benötigt einen "
                "AV-Receiver, einen HDMI-Passthrough-fähigen Receiver und "
                "einen Enigma2-Player, der TrueHD unterstützt. Falls kein "
                "Ton kommt, diese Einstellung deaktivieren."
            ),
            _("DTS direkt ausgeben:"): _("Legt fest, ob DTS-Tonspuren ohne Umwandlung direkt ausgegeben werden dürfen. Wenn dein TV oder Receiver DTS nicht unterstützt, stelle hier 'Nein' ein, damit JellyDream die Tonspur in ein kompatibleres Format umwandelt."),
            _("Nächste Folge automatisch abspielen:"): _(
                "Spielt die nächste Folge ohne Nachfrage, sobald der Abspann "
                "bis zum Dateiende läuft oder die letzten 15 Sekunden "
                "erreicht sind. Bei 'Nein' bleibt die Frage."
            ),
            _("Vorspann automatisch überspringen:"): _(
                "Überspringt den Vorspann ohne Nachfrage. "
                "Bei 'Nein' erscheint jedes Mal die Frage."
            ),
            _("Standard-Audiosprache:"): _("Wenn vorhanden, versucht JellyDream einen Film oder eine Folge direkt mit dieser Audiosprache zu starten. Wird keine passende Spur gefunden, bleibt automatisch die Standardspur der Datei bzw. des Servers aktiv."),
            _("Bevorzugter Audio-Transcode-Codec:"): _("Bestimmt, in welches Audioformat inkompatible Tonspuren umgewandelt werden. 'AAC' ist meist modern und kompatibel, 'MP2' funktioniert oft besonders gut mit älteren Enigma2-Boxen, 'AC3' nur wenn dein TV/AV-Receiver AC3 sicher unterstützt."),    
            _("Transcoding-/Remux-Ausgabe:"): _(
                "Legt fest, wie umgewandelte oder neu verpackte Streams übertragen "
                "werden. 'TS' verwendet einen fortlaufenden MPEG-Transportstream und "
                "ist für Enigma2 meistens die kompatibelste Einstellung. "
                "'HLS' verwendet eine M3U8-Playlist mit einzelnen TS-Segmenten und "
                "kann bei instabilen Netzwerkverbindungen oder bestimmten externen "
                "Playern besser funktionieren. Direct Play bleibt von dieser "
                "Einstellung unberührt."
            ),
            _("Player für Jellyfin-Streams:"): _(
                "Legt fest, mit welchem Enigma2-Service-Typ JellyDream "
                "Filme und Serien abspielt. "
                "'Enigma2 IPTV (4097)' ist der Standard. "
                "'GStreamer / gstplayer (5001)' verwendet gstplayer. "
                "'ExtePlayer3 (5002)' verwendet exteplayer3. "
                "Die zusätzlichen Auswahlmöglichkeiten erscheinen nur, "
                "wenn der jeweilige Player auf der Box installiert ist."
            ),
            _("Bibliothek automatisch aktualisieren:"): _(
                "Legt fest, ob die vollständigen Film- und Serienlisten "
                "einmal täglich automatisch vom Jellyfin-Server geladen "
                "und lokal gespeichert werden."
            ),
            _("Bibliotheksaktualisierung Stunde:"): _(
                "Legt die Stunde fest, zu der JellyDream die Film- und "
                "Serienlisten automatisch aktualisiert."
            ),
            _("Bibliotheksaktualisierung Minute:"): _(
                "Legt die Minute der automatischen "
                "Bibliotheksaktualisierung fest."
            ),
        }
        
        # Alle Choices zentral definieren. Gespeicherte Werte werden vor
        # ConfigSelection validiert, damit alte/ungültige JSON-Werte die
        # Einstellungen nicht beschädigen.

        res_choices = [
            ("4K", _("4K (UHD)")),
            ("1080p", _("1080p (Full-HD)")),
            ("720p", _("720p (HD)")),
            ("SD", _("576p (Standard SD)"))
        ]

        bitrate_choices = [
            ("16000000", _("16 Mbps (Sehr hoch)")),
            ("12000000", _("12 Mbps (Hoch)")),
            ("8000000", _("8 Mbps (Standard)")),
            ("4000000", _("4 Mbps (Mittel / WLAN)")),
            ("2000000", _("2 Mbps (Niedrig / VPN)"))
        ]

        stream_mode_choices = [
            ("stable", _("Stabilität bevorzugen")),
            ("hybrid", _("Automatisch")),
            ("direct", _("Direktstream bevorzugen"))
        ]

        player_choices = jellydream_player_service_choices()

        stream_protocol_choices = [
            ("ts", _("TS (Standard / empfohlen)")),
            ("hls", _("HLS (.m3u8)"))
        ]

        yes_no_choices = [
            ("1", _("Ja")),
            ("0", _("Nein"))
        ]

        audio_codec_choices = [
            ("aac", _("AAC (empfohlen)")),
            ("mp2", _("MP2 (maximale Kompatibilität)")),
            ("ac3", "AC3")
        ]

        audio_language_choices = [
            ("de", "Deutsch"),
            ("en", "English"),
            ("fr", "Français"),
            ("it", "Italiano"),
            ("es", "Español"),
            ("nl", "Nederlands"),
            ("pl", "Polski"),
            ("pt", "Português"),
            ("ru", "Русский"),
            ("ar", "العربية"),
            ("tr", "Türkçe"),
            ("ja", "Japanese"),
            ("ko", "Korean"),
            ("zh", "Chinese"),
        ]

        refresh_choices = [
            ("150", _("150 ms (Schnell)")),
            ("200", "200 ms"),
            ("250", "250 ms"),
            ("300", _("300 ms (empfohlen für OpenPLi)")),
            ("350", _("350 ms (langsame Receiver)"))
        ]

        image_worker_choices = [
            ("2", _("2 Worker (Standard / schnellere Bildanzeige)")),
            ("1", _("1 Worker (schwache oder ältere Box)"))
        ]

        log_choices = [
            ("DEBUG", _("DEBUG (Alles loggen)")),
            ("INFO", _("INFO (Standard)")),
            ("WARNING", _("WARNING (Nur Warnungen)")),
            ("ERROR", _("ERROR (Nur Fehler)"))
        ]

        library_hour_choices = [
            ("%02d" % hour, _("%02d Uhr") % hour)
            for hour in range(24)
        ]

        library_minute_choices = [
            ("%02d" % minute, _("%02d Minuten") % minute)
            for minute in range(0, 60, 5)
        ]

        # Reiner Aktionspunkt ohne eigenen Konfigurationswert.
        # Mit OK wird die vorhandene erweiterte Serververwaltung geöffnet.
        self.config_server_editor = ConfigNothing()

        self.config_res = ConfigSelection(
            default=jellydream_safe_config_selection_value(
                current_cfg.get("max_res"),
                res_choices,
                "1080p",
                "max_res"
            ),
            choices=res_choices
        )

        self.config_bitrate = ConfigSelection(
            default=jellydream_safe_config_selection_value(
                current_cfg.get("video_bitrate"),
                bitrate_choices,
                "8000000",
                "video_bitrate"
            ),
            choices=bitrate_choices
        )

        self.config_stream_mode = ConfigSelection(
            default=jellydream_safe_config_selection_value(
                current_cfg.get("stream_mode"),
                stream_mode_choices,
                "direct",
                "stream_mode"
            ),
            choices=stream_mode_choices
        )

        self.config_player_service_reference = ConfigSelection(
            default=jellydream_safe_config_selection_value(
                current_cfg.get("player_service_reference"),
                player_choices,
                "4097",
                "player_service_reference"
            ),
            choices=player_choices
        )

        self.config_stream_protocol = ConfigSelection(
            default=jellydream_safe_config_selection_value(
                current_cfg.get("stream_protocol"),
                stream_protocol_choices,
                "ts",
                "stream_protocol"
            ),
            choices=stream_protocol_choices
        )

        self.config_allow_ac3_direct = ConfigSelection(
            default=jellydream_safe_config_selection_value(
                current_cfg.get("allow_ac3_direct"),
                yes_no_choices,
                "0",
                "allow_ac3_direct"
            ),
            choices=yes_no_choices
        )

        self.config_allow_eac3_direct = ConfigSelection(
            default=jellydream_safe_config_selection_value(
                current_cfg.get("allow_eac3_direct"),
                yes_no_choices,
                "1",
                "allow_eac3_direct"
            ),
            choices=yes_no_choices
        )

        self.config_allow_truehd_direct = ConfigSelection(
            default=jellydream_safe_config_selection_value(
                current_cfg.get("allow_truehd_direct"),
                yes_no_choices,
                "0",
                "allow_truehd_direct"
            ),
            choices=yes_no_choices
        )
        
        self.config_allow_dts_direct = ConfigSelection(
            default=jellydream_safe_config_selection_value(
                current_cfg.get("allow_dts_direct"),
                yes_no_choices,
                "0",
                "allow_dts_direct"
            ),
            choices=yes_no_choices
        )

        self.config_preferred_audio_codec = ConfigSelection(
            default=jellydream_safe_config_selection_value(
                current_cfg.get("preferred_audio_codec"),
                audio_codec_choices,
                "aac",
                "preferred_audio_codec"
            ),
            choices=audio_codec_choices
        )

        self.config_skip_intro_automatically = ConfigSelection(
            default=jellydream_safe_config_selection_value(
                current_cfg.get("skip_intro_automatically"),
                yes_no_choices,
                "0",
                "skip_intro_automatically"
            ),
            choices=yes_no_choices
        )

        self.config_next_episode_autoplay = ConfigSelection(
            default=jellydream_safe_config_selection_value(
                current_cfg.get("next_episode_autoplay"),
                yes_no_choices,
                "0",
                "next_episode_autoplay"
            ),
            choices=yes_no_choices
        )

        self.config_default_audio_language = ConfigSelection(
            default=jellydream_safe_config_selection_value(
                current_cfg.get("default_audio_language"),
                audio_language_choices,
                "de",
                "default_audio_language"
            ),
            choices=audio_language_choices
        )

        self.config_update_refresh = ConfigSelection(
            default=jellydream_safe_config_selection_value(
                current_cfg.get("update_refresh"),
                refresh_choices,
                "150",
                "update_refresh"
            ),
            choices=refresh_choices
        )

        self.config_dynamic_backdrops = ConfigSelection(
            default=jellydream_safe_config_selection_value(
                current_cfg.get("dynamic_backdrops"),
                yes_no_choices,
                "1",
                "dynamic_backdrops"
            ),
            choices=yes_no_choices
        )

        self.config_lcd_playback_display = ConfigSelection(
            default=jellydream_safe_config_selection_value(
                current_cfg.get("lcd_playback_display"),
                yes_no_choices,
                "0",
                "lcd_playback_display"
            ),
            choices=yes_no_choices
        )
        
        self.config_image_worker_count = ConfigSelection(
            default=jellydream_safe_config_selection_value(
                current_cfg.get("image_worker_count"),
                image_worker_choices,
                "2",
                "image_worker_count"
            ),
            choices=image_worker_choices
        )

        self.config_mainmenu = ConfigSelection(
            default=jellydream_safe_config_selection_value(
                current_cfg.get("show_in_mainmenu"),
                yes_no_choices,
                "0",
                "show_in_mainmenu"
            ),
            choices=yes_no_choices
        )

        ui_language_choices = [
            (
                "auto",
                _("Automatisch (Receiver, sonst English)")
            ),
            ("en", "English"),
            ("de", "Deutsch"),
            ("fr", "Français"),
            ("it", "Italiano"),
            ("es", "Español"),
            ("pl", "Polski"),
            ("nl", "Nederlands"),
            ("tr", "Türkçe"),
            ("ar", "العربية"),
            ("ru", "Русский"),
        ]

        self.config_ui_language = ConfigSelection(
            default=jellydream_safe_config_selection_value(
                current_cfg.get("ui_language"),
                ui_language_choices,
                "auto",
                "ui_language"
            ),
            choices=ui_language_choices
        )

        self.config_log = ConfigSelection(
            default=jellydream_safe_config_selection_value(
                current_cfg.get("log_level"),
                log_choices,
                "INFO",
                "log_level"
            ),
            choices=log_choices
        )

        self.config_library_auto_update = ConfigSelection(
            default=jellydream_safe_config_selection_value(
                current_cfg.get("library_auto_update"),
                yes_no_choices,
                "1",
                "library_auto_update"
            ),
            choices=yes_no_choices
        )

        self.config_library_update_hour = ConfigSelection(
            default=jellydream_safe_config_selection_value(
                current_cfg.get("library_update_hour"),
                library_hour_choices,
                "04",
                "library_update_hour"
            ),
            choices=library_hour_choices
        )

        self.config_library_update_minute = ConfigSelection(
            default=jellydream_safe_config_selection_value(
                current_cfg.get("library_update_minute"),
                library_minute_choices,
                "00",
                "library_update_minute"
            ),
            choices=library_minute_choices
        )

        self.list = [
            (
                _("Server bearbeiten/hinzufügen:"),
                self.config_server_editor
            ),
            (_("Maximale Video-Auflösung:"), self.config_res),
            (_("Streaming-Modus:"), self.config_stream_mode),
            (_("Player für Jellyfin-Streams:"), self.config_player_service_reference),
            (_("Transcoding-/Remux-Ausgabe:"), self.config_stream_protocol),
            (_("AC3 direkt ausgeben:"), self.config_allow_ac3_direct),
            (_("EAC3 direkt ausgeben:"), self.config_allow_eac3_direct),
            (
                _("Dolby TrueHD / Atmos direkt ausgeben:"),
                self.config_allow_truehd_direct
            ),            
            (_("DTS direkt ausgeben:"), self.config_allow_dts_direct),
            (_("Bevorzugter Audio-Transcode-Codec:"), self.config_preferred_audio_codec),
            (_("Standard-Audiosprache:"), self.config_default_audio_language),
            (
                _("Vorspann automatisch überspringen:"),
                self.config_skip_intro_automatically
            ),
            (
                _("Nächste Folge automatisch abspielen:"),
                self.config_next_episode_autoplay
            ),            
            (_("Transcoding Video-Bitrate:"), self.config_bitrate),
            (
                _("Bibliothek automatisch aktualisieren:"),
                self.config_library_auto_update
            ),
            (
                _("Bibliotheksaktualisierung Stunde:"),
                self.config_library_update_hour
            ),
            (
                _("Bibliotheksaktualisierung Minute:"),
                self.config_library_update_minute
            ),
            (_("Dynamische Hintergründe:"), self.config_dynamic_backdrops),
            (_("LCD-Anzeige:"), self.config_lcd_playback_display),
            (_("Cover-/Info-Refresh-Verzögerung:"), self.config_update_refresh),
            (
                _("Image-Worker für Cover und Backdrops:"),
                self.config_image_worker_count
            ),
            (_("Im Receiver-Hauptmenü anzeigen:"), self.config_mainmenu),
            (_("Plugin-Sprache:"), self.config_ui_language),
            (_("Plugin Log-Level (Debugging):"), self.config_log)
        ]

        ConfigListScreen.__init__(self, self.list, session=session)
        self["config"].onSelectionChanged.append(self.updateHelpText)

        self["actions"] = ActionMap(
            ["OkCancelActions", "ColorActions"],
            {
                "cancel": self.close,
                "green": self.saveSettings,
                "ok": self.okPressed,
                "blue": self.manualLibraryUpdate
            },
            -1
        )

        self.onLayoutFinish.append(self.applyTitleIcon)
        self.onLayoutFinish.append(self.updateConnectionLabels)
        self.onLayoutFinish.append(self.refreshDatabase)
        self.onLayoutFinish.append(self.updateHelpText)
        self.onLayoutFinish.append(self.updateSelection)
        self.config_ui_language.addNotifier(
            self.updateLanguageFlag,
            initial_call=False
        )

    def okPressed(self):
        """
        Öffnet beim Eintrag 'Server bearbeiten/hinzufügen' die
        Serververwaltung.

        Bei allen anderen Einstellungseinträgen bleibt das bisherige
        Verhalten erhalten und die Einstellungen werden gespeichert.
        """
        try:
            current_entry = self["config"].getCurrent()

            if (
                current_entry
                and len(current_entry) > 1
                and current_entry[1] is self.config_server_editor
            ):
                self.openServerEditor()
                return

            self.saveSettings()

        except Exception as e:
            jd_log(
                "OK action in settings failed: %s"
                % e,
                level="ERROR"
            )

            if "status" in self:
                self["status"].setText(
                    _("Aktion konnte nicht ausgeführt werden: %s")
                    % e
                )


    def openServerEditor(self):
        """
        Öffnet den vorhandenen erweiterten Login-Screen zur Verwaltung
        der drei Jellyfin-Serverprofile.
        """
        try:
            ok, error_message = jellydream_safe_open(
                self.session,
                self.serverEditorClosed,
                JellyDreamLoginScreen
            )

            if not ok:
                self["status"].setText(
                    error_message
                    or _("Serververwaltung konnte nicht geöffnet werden.")
                )

        except Exception as e:
            jd_log(
                "Server manager could not be opened: %s"                % e,
                level="ERROR"
            )

            self["status"].setText(
                _("Serververwaltung konnte nicht geöffnet werden: %s")
                % e
            )


    def serverEditorClosed(self, result=None):
        """
        Aktualisiert die Server-, Benutzer- und Datenbankanzeige, nachdem
        die Serververwaltung geschlossen wurde.
        """
        try:
            if not getattr(self, "instance", None):
                return

            self.updateConnectionLabels()

            if jellyfin_is_logged_in():
                self["status"].setText(
                    _("Serververwaltung geschlossen. GRÜN = Einstellungen speichern")
                )

                self.refreshDatabase()
            else:
                self["info_box"].setText(
                    _("Filme: -\nSerien: -\nEpisoden: -\nSammlungen: -")
                )

                self["status"].setText(
                    _("Nicht bei Jellyfin angemeldet. GRÜN = Einstellungen speichern")
                )

            self.updateHelpText()

        except Exception as e:
            jd_log(
                "Display could not be updated after the server manager: %s"
                % e,
                level="WARNING"
            )

    def manualLibraryUpdate(self):
        try:
            if self._async_loading:
                self["status"].setText(_("Bitte warten, es werden bereits Daten geladen ..."))
                return

            if not jellyfin_is_logged_in():
                self["status"].setText(_("Bitte zuerst bei Jellyfin anmelden."))
                return

            # Globalen Update-Status geschützt lesen.
            with jd_library._LIBRARY_CACHE_LOCK:
                update_running = bool(
                    jd_library._LIBRARY_UPDATE_IN_PROGRESS
                )

            if update_running:
                self["status"].setText(
                    _("Bibliotheksaktualisierung läuft bereits ...")
                )
                return

            # Lokale Sperre atomar setzen. Diese verhindert insbesondere
            # mehrfaches schnelles Drücken auf die blaue Taste.
            if not self._beginManualLibraryUpdate():
                self["status"].setText(
                    _("Bibliotheksaktualisierung läuft bereits ...")
                )
                return

            self["status"].setText(
                _("Film- und Serienlisten werden aktualisiert ...")
            )

            self.startSpinner(
                _("Film- und Serienlisten werden aktualisiert ...")
            )

            def worker():
                try:
                    result = jellydream_refresh_library_cache()

                    if not isinstance(result, dict):
                        result = {
                            "ok": False,
                            "busy": False,
                            "message": _(
                                "Die Aktualisierung lieferte "
                                "kein gültiges Ergebnis."
                            ),
                            "log_message": (
                                "The update did not return a valid result"
                            )
                        }

                except Exception as e:
                    result = {
                        "ok": False,
                        "busy": False,
                        "message": str(e),
                        "log_message": str(e)
                    }

                    jd_log(
                        "Manual library refresh in settings failed: %s" % e,
                        level="ERROR"
                    )

                def finished():
                    # Lokale Sperre unabhängig vom Zustand des Screens lösen.
                    self._finishManualLibraryUpdate()

                    try:
                        if not getattr(self, "instance", None):
                            return

                        self.stopSpinner()

                        message = str(
                            result.get("message")
                            or _("Aktualisierung beendet")
                        )
                        log_message = jellydream_library_update_log_text(result)

                        if result.get("ok"):
                            self["status"].setText(
                                _("%s | GRÜN = Speichern")
                                % message
                            )

                            jd_log(
                                "Manual library refresh in settings succeeded: %s"
                                % log_message,
                                level="INFO"
                            )

                            self.refreshDatabase()

                        elif result.get("busy"):
                            self["status"].setText(
                                _("Bibliotheksaktualisierung läuft bereits ...")
                            )

                        else:
                            self["status"].setText(
                                _("Aktualisierung fehlgeschlagen: %s")
                                % message
                            )

                            jd_log(
                                "Manual library refresh in settings was not successful: %s"
                                % log_message,
                                level="WARNING"
                            )

                    except Exception as e:
                        try:
                            self.stopSpinner()
                        except Exception:
                            pass

                        jd_log(
                            "Manual update result could not be shown in settings: %s"
                            % e,
                            level="WARNING"
                        )

                if not jellydream_dispatch_to_gui(finished):
                    self._finishManualLibraryUpdate()

                    jd_log(
                        "Manual library refresh result could not be dispatched to the GUI thread",
                        level="ERROR"
                    )

            started = jellydream_submit_maintenance(
                worker
            )

            if not started:
                self._finishManualLibraryUpdate()
                self.stopSpinner()

                self["status"].setText(
                    _(
                        "Aktualisierung konnte nicht gestartet werden: "
                        "Der Hintergrund-Worker-Pool ist ausgelastet."
                    )
                )

                jd_log(
                    "Manual library refresh in settings was rejected by the worker pool",
                    level="WARNING"
                )

        except Exception as e:
            self._finishManualLibraryUpdate()

            try:
                self.stopSpinner()
            except Exception:
                pass

            try:
                self["status"].setText(
                    _("Aktualisierung konnte nicht gestartet werden: %s")
                    % e
                )
            except Exception:
                pass

            jd_log(
                "Manual library refresh in settings could not be started: %s"
                % e,
                level="ERROR"
            )
        
    def updateHelpText(self):
        try:
            current = self["config"].getCurrent()
            if not current:
                self["help_box"].setText("")
                self.updateLanguageFlag()
                return

            entry_name = current[0]
            help_text = self.help_texts.get(entry_name, _("Keine Beschreibung verfügbar."))
            self["help_box"].setText(help_text)
            self.updateLanguageFlag()
        except Exception as e:
            jd_log(f"Error in updateHelpText: {e}", level="WARNING")

    def updateLanguageFlag(self, config_element=None):
        widget = self["language_flag"] if "language_flag" in self else None

        if widget is None or not getattr(widget, "instance", None):
            return

        flag_file = ""

        try:
            current = self["config"].getCurrent()
            selected = (
                current
                and len(current) > 1
                and current[1] is self.config_ui_language
            )
            code = str(self.config_ui_language.value or "")
            names = {
                "en": "us.png",
                "de": "de.png",
                "fr": "fr.png",
                "it": "it.png",
                "es": "es.png",
                "pl": "pl.png",
                "nl": "nl.png",
                "tr": "tr.png",
                "ar": "eg.png",
                "ru": "ru.png",
            }

            if selected:
                if code == "auto":
                    code = jellydream_resolve_ui_language("auto")
                flag_file = names.get(code, names["en"])
        except Exception:
            flag_file = ""

        instance = widget.instance

        if not flag_file:
            try:
                instance.hide()
            except Exception:
                pass
            return

        path = os.path.join(
            PLUGIN_DIR,
            "images",
            "flags",
            flag_file
        )

        try:
            box = self["config"].instance
            item_height = int(box.getItemHeight())
            top = int(box.getTopIndex())
            row = max(0, int(self["config"].getCurrentIndex()) - top)

            native = {
                "us.png": (40, 21),
                "de.png": (40, 24),
                "pl.png": (40, 25),
            }.get(flag_file, (40, 27))

            if item_height >= 90:
                flag_width = 80
            elif item_height >= 65:
                flag_width = 60
            else:
                flag_width = 40

            flag_height = max(
                16,
                int(round(native[1] * flag_width / float(native[0])))
            )
            if item_height >= 90:
                font_size = 34
            elif item_height >= 65:
                font_size = 26
            else:
                font_size = 20

            label = str(self.config_ui_language.getText() or "").strip()
            text_width = int(len(label) * font_size * 0.56)

            try:
                boundary = fontRenderClass.getInstance().getTextBoundarySize(
                    self.instance,
                    gFont("Regular", font_size),
                    eSize(box.size().width(), item_height),
                    label
                )
                measured = int(boundary.width())
                if measured > 0:
                    text_width = measured
            except Exception:
                pass

            origin = box.position()
            content_right = origin.x() + box.size().width() - max(36, int(item_height * 0.62))
            x = content_right - text_width - 12 - flag_width
            y = origin.y() + row * item_height + max(0, (item_height - flag_height) // 2)
            instance.resize(eSize(flag_width, flag_height))
            instance.move(ePoint(x, y))
            instance.setPixmapFromFile(path)
            instance.show()
        except Exception as e:
            jd_log(
                "Language flag could not be shown: %s" % e,
                level="WARNING"
            )

    def refreshDatabase(self, *args):
        if not getattr(self, "instance", None):
            return

        if self._async_loading:
            return

        if not jellyfin_is_logged_in():
            self["info_box"].setText(
                _("Filme: -\nSerien: -\nEpisoden: -\nSammlungen: -")
            )
            return

        self.runAsyncLoad(
            loader=jellyfin_get_database_counts,
            finished_callback=self.databaseCountsLoaded,
            message=_("Datenbankinformationen werden geladen ...")
        )

    def databaseCountsLoaded(self, counts, error_message=""):
        if error_message:
            self["info_box"].setText(
                _("Filme: -\nSerien: -\nEpisoden: -\nSammlungen: -")
            )

            jd_log(
                "Settings library counts could not be loaded: %s"                % error_message,
                level="WARNING"
            )
            return

        counts = (
            counts
            if isinstance(counts, dict)
            else {}
        )

        m_count = int(counts.get("movies") or 0)
        b_count = int(counts.get("boxsets") or 0)
        s_count = int(counts.get("series") or 0)
        e_count = int(counts.get("episodes") or 0)

        self["info_box"].setText(
            _("Filme: %s   Sammlungen: %s\nSerien: %s   Episoden: %s")
            % (
                m_count,
                b_count,
                s_count,
                e_count
            )
        )

    def applyTitleIcon(self):
        jellydream_apply_title_icon(self)

    def updateConnectionLabels(self):
        try:
            server = jellyfin_server() or _("nicht gesetzt")

            if len(server) > 43:
                server = "%s..." % server[:40]

            user = jellyfin_username() or "-"

            profiles, active_profile_index = (
                jellydream_normalize_server_profiles(
                    read_config() or {}
                )
            )

            status = (
                _("verbunden")
                if jellyfin_is_logged_in()
                else _("nicht angemeldet")
            )

            self["settings_box"].setText(
                _("Server %s von %s\n%s")
                % (
                    active_profile_index + 1,
                    MAX_JELLYFIN_SERVER_PROFILES,
                    server
                )
            )

            self["login_box"].setText(
                _("Benutzer: %s\nStatus: %s")
                % (
                    user,
                    status
                )
            )

        except Exception as e:
            jd_log(
                "Error in JellyDreamSettingsScreen.updateConnectionLabels: %s"
                % e,
                level="WARNING"
            )
        
    def loadBackground(self):
        super().loadBackground()

        try:
            if self["config_shield"].instance:
                self["config_shield"].instance.setPixmap(None)
        except Exception:
            pass

    def updateSelection(self):
        try:
            if "status" in self:
                self["status"].setText(
                    _(
                        "OK = Öffnen / Übernehmen   GRÜN = Speichern   "
                        "BLAU = Bibliothek aktualisieren   EXIT = Abbrechen"
                    )
                )

                if self["status"].instance:
                    self["status"].instance.setForegroundColor(
                        parseColor("#d8dde5")
                    )

        except Exception:
            pass

    def saveSettings(self):
        try:
            full_cfg = read_config() or {}

            old_mainmenu_value = str(
                full_cfg.get("show_in_mainmenu", "0")
            )

            old_image_worker_count = str(
                full_cfg.get("image_worker_count", "2")
            )

            old_dynamic_backdrops = str(
                full_cfg.get("dynamic_backdrops", "1")
            )

            old_ui_language = str(
                full_cfg.get("ui_language", "auto")
            )

            full_cfg["max_res"] = self.config_res.value
            full_cfg["stream_mode"] = self.config_stream_mode.value
            full_cfg["player_service_reference"] = (
                self.config_player_service_reference.value
            )
            full_cfg["stream_protocol"] = self.config_stream_protocol.value
            full_cfg["allow_ac3_direct"] = self.config_allow_ac3_direct.value
            full_cfg["allow_eac3_direct"] = self.config_allow_eac3_direct.value
            full_cfg["allow_truehd_direct"] = (
                self.config_allow_truehd_direct.value
            )
            full_cfg["allow_dts_direct"] = self.config_allow_dts_direct.value
            full_cfg["preferred_audio_codec"] = self.config_preferred_audio_codec.value    
            full_cfg["default_audio_language"] = self.config_default_audio_language.value
            full_cfg["next_episode_autoplay"] = (
                self.config_next_episode_autoplay.value
            )
            full_cfg["skip_intro_automatically"] = (
                self.config_skip_intro_automatically.value
            )            
            full_cfg["video_bitrate"] = int(self.config_bitrate.value)
            full_cfg["library_auto_update"] = (
                self.config_library_auto_update.value
            )

            full_cfg["library_update_hour"] = str(
                self.config_library_update_hour.value
            )

            full_cfg["library_update_minute"] = str(
                self.config_library_update_minute.value
            )
            full_cfg["dynamic_backdrops"] = str(
                self.config_dynamic_backdrops.value
            )
            full_cfg["lcd_playback_display"] = str(
                self.config_lcd_playback_display.value
            )            
            full_cfg["update_refresh"] = str(self.config_update_refresh.value)

            full_cfg["image_worker_count"] = str(
                self.config_image_worker_count.value
            )

            full_cfg["log_level"] = self.config_log.value
            full_cfg["show_in_mainmenu"] = self.config_mainmenu.value
            full_cfg["ui_language"] = str(
                self.config_ui_language.value
            )

            if not write_config(full_cfg):
                if "status" in self:
                    self["status"].setText(_("Fehler beim Speichern der Einstellungen"))
                jd_log("Settings could not be saved", level="ERROR")
                return

            localeInit(full_cfg["ui_language"])

            if str(full_cfg["ui_language"]) != old_ui_language:
                jellydream_clear_metadata_caches(
                    clear_details=True
                )

            jellydream_restart_library_scheduler()

            mainmenu_changed = (
                str(self.config_mainmenu.value)
                != old_mainmenu_value
            )

            image_worker_changed = (
                str(self.config_image_worker_count.value)
                != old_image_worker_count
            )

            dynamic_backdrops_changed = (
                str(self.config_dynamic_backdrops.value)
                != old_dynamic_backdrops
            )

            if mainmenu_changed:
                self.close("trigger_mainmenu_restart")

            elif image_worker_changed:
                self.close("trigger_image_worker_restart")

            elif dynamic_backdrops_changed:
                self.close("dynamic_backdrops_changed")

            else:
                self.close()

        except Exception as e:
            if "status" in self:
                self["status"].setText(_("Fehler beim Speichern: %s") % e)
            jd_log(f"Error in saveSettings: {e}", level="ERROR")
