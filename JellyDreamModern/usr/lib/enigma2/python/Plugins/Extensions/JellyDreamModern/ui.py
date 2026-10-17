# -*- coding: utf-8 -*-

import os
import threading
import time

from Components.Label import Label
from Components.Pixmap import Pixmap
from Tools.Directories import resolveFilename, SCOPE_PLUGINS
from Screens.Screen import Screen
from Components.config import config, ConfigSelection, ConfigNothing
from enigma import ePoint
from enigma import eSize
from enigma import eTimer
from enigma import getDesktop
from skin import parseColor

from . import _

from .constants import (
    DETAIL_RETRY_BASE_SECONDS,
    DETAIL_RETRY_MAX_SECONDS,
    FALLBACK_HOME_COVER,
    FALLBACK_RESUME_COVER,
    ITEMS_PER_PAGE_EPISODES,
    ITEMS_PER_PAGE_GRID,
    PLUGIN_DIR,
    SPINNER_FRAME_DELAY
)
from .log import jd_log
from .workers import jellydream_dispatch_to_gui, jellydream_submit_api
from .config import (
    jellydream_cache_dir,
    jellydream_dynamic_backdrops_enabled,
    read_config
)
from .http import jellydream_auth_snapshot, jellydream_auth_snapshot_matches_current
from .images import (
    jellydream_apply_background_and_shadow,
    jellydream_apply_resolved_background,
    jellydream_async_download_image,
    jellydream_cache_get,
    jellydream_cached_background_path,
    jellydream_expected_image_filename,
    jellydream_get_background_widget_size,
    jellydream_get_cached_image_path,
    jellydream_get_spinner_frames,
    jellydream_set_pixmap_if_changed,
    jellydream_valid_file,
    set_jellydream_background
)
from .uiutil import (
    jellydream_int_or_default,
    jellydream_music_queue_clear,
    jellydream_item_is_favorite,
    jellydream_item_poster_badge,
    jellydream_season_progress,
    jellydream_restart_single_timer,
    jellydream_safe_current_item,
    jellydream_safe_set_pixmap,
    jellydream_resume_seconds,
    jellydream_safe_open,
    jellydream_apply_status_symbol_font,
    jellydream_bind_status_symbols,
    jellydream_safe_set_status,
    jellydream_stop_timer
)
from .library import (
    jellydream_invalidate_playback_caches,
    jellydream_is_next_up_item,
    jellydream_item_has_image,
    jellydream_load_item_details_async,
    jellydream_season_display_name,
    jellyfin_get_boxset_for_item,
    jellyfin_hide_item_from_resume,
    jellyfin_set_item_favorite,
    jellyfin_set_item_played_state,
    _GLOBAL_DETAILS_CACHE
)


def jellydream_load_external_skin(screen_name):
    from . import constants as jd_constants
    try:
        try:
            active_skin = config.skin.primary_skin.value.lower()
        except Exception as e:
            jd_log(f"Active skin could not be read: {e}", level="WARNING")
            active_skin = ""

        try:
            width_px = getDesktop(0).size().width()
        except Exception as e:
            jd_log(f"Desktop width could not be read: {e}", level="WARNING")
            width_px = 1920

        plugin_root = resolveFilename(SCOPE_PLUGINS, "Extensions/JellyDreamModern")

        if "metrix" in active_skin:
            try:
                is_ehd = False
                if hasattr(config.plugins, "MyMetrixLiteOther") and hasattr(config.plugins.MyMetrixLiteOther, "EHDenabled"):
                    if str(config.plugins.MyMetrixLiteOther.EHDenabled.value) in ("1", "2", "3", "True"):
                        is_ehd = True
            except Exception:
                is_ehd = False

            if is_ehd:
                skin_directory = os.path.join(plugin_root, "skin/fhd/")
            else:
                skin_directory = os.path.join(plugin_root, "skin/hd/")
        else:
            if width_px >= 2560:
                skin_directory = os.path.join(plugin_root, "skin/uhd/")
            elif width_px > 1280:
                skin_directory = os.path.join(plugin_root, "skin/fhd/")
            else:
                skin_directory = os.path.join(plugin_root, "skin/hd/")

        jd_constants.CURRENT_SKIN_DIRECTORY = skin_directory

        skin_xml_file = os.path.join(skin_directory, f"{screen_name}.xml")
        if os.path.exists(skin_xml_file):
            with open(skin_xml_file, "r", encoding="utf-8") as f:
                return f.read()
                
        jd_log(f"[Skin engine] XML file is missing from the folder: {skin_xml_file}", level="ERROR")
    except Exception as e:
        jd_log(f"[Skin engine] Critical error while loading {screen_name}: {e}", level="ERROR")
        
    return "<screen></screen>"


def jellydream_reset_screen_background(screen, text_widget_name=""):
    try:
        if text_widget_name and text_widget_name in screen:
            screen[text_widget_name].setText("")
    except Exception:
        pass

    try:
        jellydream_apply_resolved_background(screen)
    except Exception:
        try:
            set_jellydream_background(screen)
        except Exception:
            pass

        
class JellyDreamBaseScreen(Screen):
    SCREEN_NAME = ""
    AUTO_BACKGROUND = True
    AUTO_STATUS = False
    AUTO_SHADOW = True

    def __init__(self, session):
        screen_name = self.SCREEN_NAME or self.__class__.__name__
        self.skin = jellydream_load_external_skin(screen_name)
        Screen.__init__(self, session)

        if self.AUTO_SHADOW:
            self["background_shadow"] = Pixmap()

        if self.AUTO_BACKGROUND:
            self["background"] = Pixmap()

        if self.AUTO_STATUS:
            self["status"] = Label("")
            jellydream_bind_status_symbols(self["status"])

        self["spinner"] = Pixmap()
        self["spinner_text"] = Label("")

        self._spinner_timer = eTimer()
        self._spinner_frames = ()
        self._spinner_index = 0
        self._spinner_active = False
        self._async_load_token = 0
        self._async_loading = False
        self._manual_library_update_lock = threading.RLock()
        self._manual_library_update_running = False
        self._widget_geometry = {}

        try:
            self._spinner_timer.callback.append(
                self._animateSpinner
            )
        except Exception:
            try:
                self._spinner_timer.timeout.get().append(
                    self._animateSpinner
                )
            except Exception:
                pass

        #self._loadSpinnerFrames()

        self.onLayoutFinish.append(self._baseOnLayoutFinish)
        self.onLayoutFinish.append(self._hideSpinnerInitially)
        self.onClose.append(self.cleanupTimers)

    def _baseOnLayoutFinish(self):
        if "status" in self:
            jellydream_apply_status_symbol_font(self)

        if self.AUTO_BACKGROUND:
            self.loadBackground()

    def loadBackground(self):
        jellydream_apply_background_and_shadow(self)

    def _setSpinnerVisible(self, visible):
        try:
            if "spinner" not in self:
                return

            spinner_widget = self["spinner"]

            if visible:
                try:
                    spinner_widget.show()
                except Exception:
                    pass

                try:
                    if getattr(spinner_widget, "instance", None):
                        spinner_widget.instance.show()
                except Exception:
                    pass

            else:
                try:
                    spinner_widget.hide()
                except Exception:
                    pass

                try:
                    if getattr(spinner_widget, "instance", None):
                        spinner_widget.instance.hide()
                except Exception:
                    pass

        except Exception as e:
            jd_log(
                "Spinner visibility could not be changed: %s"% e,
                level="DEBUG"
            )
        
    def _loadSpinnerFrames(self):
        try:
            spinner_choice = str(
                read_config().get(
                    "spinner_choice",
                    ""
                )
            ).strip()

            self._spinner_frames = (
                jellydream_get_spinner_frames(
                    spinner_choice
                )
            )

        except Exception as e:
            self._spinner_frames = ()

            jd_log(
                "Spinner frames could not be applied: %s"                % e,
                level="WARNING"
            )

    def _hideSpinnerInitially(self):
        self._spinner_active = False

        try:
            self._spinner_timer.stop()
        except Exception:
            pass

        self._setSpinnerVisible(False)

        try:
            self["spinner_text"].setText("")
            self["spinner_text"].hide()

            if getattr(self["spinner_text"], "instance", None):
                self["spinner_text"].instance.hide()
        except Exception:
            pass

    def _getSpinnerBackdropId(self):
        try:
            active_id = str(
                getattr(self, "_active_backdrop_id", "") or ""
            ).strip()

            if active_id:
                return active_id

            series_id = str(
                getattr(self, "series_id", "") or ""
            ).strip()

            if series_id:
                return series_id

            boxset_id = str(
                getattr(self, "boxset_id", "") or ""
            ).strip()

            if boxset_id:
                return boxset_id

            item_id = str(
                getattr(self, "item_id", "") or ""
            ).strip()

            if item_id:
                return item_id

        except Exception as e:
            jd_log(
                "Spinner backdrop id could not be resolved: %s"
                % e,
                level="DEBUG"
            )

        return ""

    def _applyCachedBackdropForSpinner(self):
        try:
            if not jellydream_dynamic_backdrops_enabled():
                # Falls vorher noch ein dynamischer Hintergrund sichtbar war,
                # diesen beim Start des Spinners sofort ersetzen.
                set_jellydream_background(self)
                return False

            target_id = self._getSpinnerBackdropId()

            if not target_id:
                return False

            background_width, background_height = (
                jellydream_get_background_widget_size(self)
            )

            backdrop_path = jellydream_cached_background_path(
                target_id,
                width=background_width,
                height=background_height,
                quality=90,
                image_type="Backdrop"
            )

            if not jellydream_valid_file(backdrop_path, 500):
                return False

            set_jellydream_background(self, backdrop_path)

            jd_log(
                "Using cached backdrop for spinner: %s"                % target_id,
                level="DEBUG"
            )

            return True

        except Exception as e:
            jd_log(
                "Spinner backdrop could not be set: %s"
                % e,
                level="DEBUG"
            )

        return False
        
    def startSpinner(self, message="Daten werden geladen ..."):
        try:
            if not self._spinner_frames:
                self._loadSpinnerFrames()
            self._applyCachedBackdropForSpinner()            
            self._spinner_active = True
            self._spinner_index = 0

            if "spinner_text" in self:
                self["spinner_text"].setText(
                    _(str(message or "Daten werden geladen ..."))
                )

                try:
                    self["spinner_text"].show()
                except Exception:
                    pass

            self._animateSpinner()

        except Exception as e:
            jd_log(
                "Spinner could not be started: %s" % e,
                level="DEBUG"
            )

    def _animateSpinner(self):
        try:
            if not self._spinner_active:
                return

            if not self._spinner_frames:
                return

            if not getattr(self, "instance", None):
                return

            # Die Pixmaps wurden bereits in _loadSpinnerFrames() vorgeladen.
            frame_pixmap = self._spinner_frames[
                self._spinner_index % len(self._spinner_frames)
            ]

            spinner_widget = self["spinner"]

            if (
                frame_pixmap is not None
                and getattr(spinner_widget, "instance", None)
            ):
                spinner_widget.instance.setPixmap(frame_pixmap)

                # Komponentenstatus und native Instanz synchron halten.
                self._setSpinnerVisible(True)

                # Kein zusätzliches invalidate(): setPixmap() löst den
                # erforderlichen Repaint selbst aus.

            self._spinner_index = (
                self._spinner_index + 1
            ) % len(self._spinner_frames)

            # Nur neu starten, wenn der Spinner weiterhin aktiv ist.
            if self._spinner_active:
                self._spinner_timer.start(
                    SPINNER_FRAME_DELAY,
                    True
                )

        except Exception as e:
            jd_log(
                "Spinner animation failed: %s" % e,
                level="DEBUG"
            )

    def stopSpinner(self):
        self._spinner_active = False

        try:
            self._spinner_timer.stop()
        except Exception:
            pass

        self._setSpinnerVisible(False)

        try:
            spinner_widget = self["spinner"]

            if getattr(spinner_widget, "instance", None):
                try:
                    spinner_widget.instance.setPixmap(None)
                except Exception:
                    pass

                try:
                    spinner_widget.instance.invalidate()
                except Exception:
                    pass
        except Exception:
            pass

        try:
            self["spinner_text"].setText("")
            self["spinner_text"].hide()

            if getattr(self["spinner_text"], "instance", None):
                self["spinner_text"].instance.hide()
        except Exception:
            pass

        jd_log(
            "Spinner stopped and hidden",
            level="DEBUG"
        )

    def runAsyncLoad(
        self,
        loader,
        finished_callback,
        message="Daten werden geladen ...",
        validate_auth_session=True,
        job_submitter=None
    ):

        try:
            if not callable(loader):
                return False

            if not callable(finished_callback):
                return False

            self._async_load_token += 1
            current_token = self._async_load_token

            # Sitzung zum Zeitpunkt der Job-Erstellung festhalten.
            #
            # Login- und Logout-Vorgänge verändern die Sitzung absichtlich.
            # Bei diesen Jobs darf das Ergebnis deshalb nicht aufgrund eines
            # geänderten Auth-Snapshots verworfen werden.
            auth_snapshot = (
                jellydream_auth_snapshot()
                if validate_auth_session
                else None
            )

            self._async_loading = True

            self.startSpinner(message)

            def worker():
                result_data = None
                error_message = ""

                try:
                    result_data = loader()
                except Exception as e:
                    error_message = str(e)

                    jd_log(
                        "Async data request failed: %s" % e,
                        level="WARNING"
                    )

                def apply_result():
                    try:
                        if not getattr(self, "instance", None):
                            return

                        if current_token != self._async_load_token:
                            return

                        # Der Screen ist zwar noch offen, aber die
                        # Anmeldung wurde geändert. Alte Listen-, Such- oder
                        # Startseitenresultate nicht mehr übernehmen.
                        if (
                            validate_auth_session
                            and not jellydream_auth_snapshot_matches_current(
                                auth_snapshot
                            )
                        ):
                            self._async_loading = False
                            self.stopSpinner()

                            jd_log(
                                "Stale async screen result discarded after a session change",
                                level="DEBUG"
                            )
                            return

                        self._async_loading = False
                        self.stopSpinner()

                        finished_callback(
                            result_data,
                            error_message
                        )

                    except Exception as e:
                        self._async_loading = False
                        self.stopSpinner()

                        jd_log(
                            "Async result could not be applied: %s" % e,
                            level="WARNING"
                        )

                jellydream_dispatch_to_gui(apply_result)

            submit_function = (
                job_submitter
                if callable(job_submitter)
                else jellydream_submit_api
            )

            if not submit_function(worker):
                rejection_message = _(
                    "Der Hintergrund-Worker-Pool ist ausgelastet. "
                    "Der Ladevorgang konnte nicht gestartet werden."
                )

                self._async_loading = False
                self.stopSpinner()

                jd_log(
                    "Async load could not be submitted to the worker pool",
                    level="WARNING"
                )

                # Auch bei einer abgelehnten Aufgabe muss der normale
                # Abschluss-Callback ausgeführt werden. Andernfalls
                # warten einzelne Screens dauerhaft auf ein Ergebnis.
                try:
                    finished_callback(
                        None,
                        rejection_message
                    )
                except Exception as callback_error:
                    jd_log(
                        "Callback of a rejected load failed: %s"
                        % callback_error,
                        level="WARNING"
                    )

                return False

            return True

        except Exception as e:
            self._async_loading = False
            self.stopSpinner()

            error_message = _(
                "Asynchroner Ladevorgang konnte nicht "
                "gestartet werden: %s"
            ) % e

            jd_log(
                "Async load could not be started: %s" % e,
                level="WARNING"
            )

            try:
                if callable(finished_callback):
                    finished_callback(
                        None,
                        error_message
                    )
            except Exception as callback_error:
                jd_log(
                    "Error callback of the load failed: %s"
                    % callback_error,
                    level="WARNING"
                )

            return False

    def _itemStateUsesMusicWords(self, item):
        screen_name = str(getattr(self, "SCREEN_NAME", "") or "")

        if screen_name in (
            "JellyDreamMusicListScreen",
            "JellyDreamMusicAlbumScreen"
        ):
            return True

        item_type = ""

        if isinstance(item, dict):
            item_type = str(item.get("Type") or "")

        return item_type in ("Audio", "MusicAlbum")

    def openItemStateMenu(self, item, refresh_callback=None):
        if self._async_loading:
            return False

        if not isinstance(item, dict):
            return False

        item_id = str(
            item.get("Id") or item.get("id") or ""
        ).strip()

        if not item_id:
            return False

        from Screens.ChoiceBox import ChoiceBox

        def chosen(answer):
            if not getattr(self, "instance", None):
                return

            if not answer:
                return

            try:
                action = str(answer[1])
            except Exception:
                return

            def loader():
                if action == "played":
                    return jellyfin_set_item_played_state(item_id, True)

                if action == "unplayed":
                    return jellyfin_set_item_played_state(item_id, False)

                if action == "favorite":
                    return jellyfin_set_item_favorite(item_id, True)

                if action == "unfavorite":
                    return jellyfin_set_item_favorite(item_id, False)

                return jellyfin_hide_item_from_resume(item_id)

            def finished(result, error_message=""):
                ok = (
                    isinstance(result, dict)
                    and result.get("ok")
                    and not error_message
                )

                if not ok:
                    self.setStatus(
                        _("Der Status konnte nicht geändert werden."),
                        level="ERROR"
                    )
                    return

                user_data = item.get("UserData")

                if not isinstance(user_data, dict):
                    user_data = {}
                    item["UserData"] = user_data

                if action == "played":
                    user_data["Played"] = True
                    user_data["PlaybackPositionTicks"] = 0
                    user_data["UnplayedItemCount"] = 0
                    user_data["PlayedPercentage"] = 100
                    self.setStatus(
                        _("Eintrag wurde als gehört markiert.")
                        if music
                        else _("Eintrag wurde als gesehen markiert.")
                    )
                elif action == "unplayed":
                    user_data["Played"] = False
                    user_data["PlayedPercentage"] = 0
                    user_data["PlaybackPositionTicks"] = 0

                    try:
                        child_count = int(item.get("ChildCount") or 0)
                    except Exception:
                        child_count = 0

                    if child_count > 0:
                        user_data["UnplayedItemCount"] = child_count

                    self.setStatus(
                        _("Eintrag wurde als ungehört markiert.")
                        if music
                        else _("Eintrag wurde als ungesehen markiert.")
                    )
                elif action == "favorite":
                    user_data["IsFavorite"] = True
                    self.setStatus(
                        _("Eintrag wurde zu den Favoriten hinzugefügt.")
                    )
                elif action == "unfavorite":
                    user_data["IsFavorite"] = False
                    self.setStatus(
                        _("Eintrag wurde aus den Favoriten entfernt.")
                    )
                else:
                    user_data["PlaybackPositionTicks"] = 0
                    self.setStatus(
                        _("Eintrag wurde aus Weiterhören entfernt.")
                        if music
                        else _("Eintrag wurde aus Weiterschauen entfernt.")
                    )

                response_data = (
                    result.get("data")
                    if isinstance(result, dict)
                    else None
                )

                if isinstance(response_data, dict):
                    for key in (
                        "Played",
                        "PlayedPercentage",
                        "UnplayedItemCount",
                        "PlaybackPositionTicks",
                        "IsFavorite",
                        "PlayCount"
                    ):
                        if key in response_data and response_data[key] is not None:
                            user_data[key] = response_data[key]

                jellydream_invalidate_playback_caches(
                    item,
                    action
                )

                if callable(refresh_callback):
                    refresh_callback()

            self.runAsyncLoad(
                loader=loader,
                finished_callback=finished,
                message=_("Status wird gespeichert ...")
            )

        user_data = item.get("UserData") or {}
        is_favorite = bool(
            user_data.get("IsFavorite")
        ) if isinstance(user_data, dict) else False
        music = self._itemStateUsesMusicWords(item)

        if music:
            menu_entries = [
                (_("Als gehört markieren"), "played"),
                (_("Als ungehört markieren"), "unplayed"),
                (_("Aus Weiterhören entfernen"), "hide_resume")
            ]
        else:
            menu_entries = [
                (_("Als gesehen markieren"), "played"),
                (_("Als ungesehen markieren"), "unplayed"),
                (_("Aus Weiterschauen entfernen"), "hide_resume")
            ]

        if is_favorite:
            menu_entries.append(
                (_("Aus Favoriten entfernen"), "unfavorite")
            )
        else:
            menu_entries.append(
                (_("Zu Favoriten hinzufügen"), "favorite")
            )

        opened, error_message = jellydream_safe_open(
            self.session,
            chosen,
            ChoiceBox,
            title=_("Wiedergabestatus"),
            list=menu_entries
        )

        if not opened:
            self.setStatus(error_message, level="ERROR")

        return opened

    def playItemAsync(
        self,
        title,
        item_id,
        runtime_seconds=0,
        return_screen=None,
        callback=None,
        start_position_seconds=0,
        audio_stream_index=None,
        message="Streamdaten werden geladen ...",
        item=None
    ):
        if self._async_loading:
            return False

        item_id = str(item_id or "").strip()

        if not item_id:
            self.setStatus(
                _("Fehler: Item-ID fehlt."),
                level="WARNING"
            )
            return False

        position = jellydream_resume_seconds(
            item,
            runtime_seconds
        )

        try:
            given_position = max(0, int(start_position_seconds or 0))
        except Exception:
            given_position = 0

        if given_position >= 15:
            runtime = 0

            try:
                runtime = max(0, int(runtime_seconds or 0))
            except Exception:
                runtime = 0

            if runtime <= 0 or given_position < runtime - 20:
                position = given_position

        if position > 0:
            return self._ask_resume_then_play(
                title=title,
                item_id=item_id,
                runtime_seconds=runtime_seconds,
                return_screen=return_screen,
                callback=callback,
                start_position_seconds=position,
                audio_stream_index=audio_stream_index,
                message=message
            )

        return self._start_playback(
            title=title,
            item_id=item_id,
            runtime_seconds=runtime_seconds,
            return_screen=return_screen,
            callback=callback,
            start_position_seconds=0,
            audio_stream_index=audio_stream_index,
            message=message
        )

    def _ask_resume_then_play(
        self,
        title,
        item_id,
        runtime_seconds,
        return_screen,
        callback,
        start_position_seconds,
        audio_stream_index,
        message
    ):
        from Screens.MessageBox import MessageBox

        def chosen(answer):
            if not getattr(self, "instance", None):
                return

            if answer is None:
                return

            if isinstance(answer, (tuple, list)):
                try:
                    resume = bool(answer[1])
                except Exception:
                    resume = True
            else:
                resume = bool(answer)

            self._start_playback(
                title=title,
                item_id=item_id,
                runtime_seconds=runtime_seconds,
                return_screen=return_screen,
                callback=callback,
                start_position_seconds=(
                    start_position_seconds if resume else 0
                ),
                audio_stream_index=audio_stream_index,
                message=message
            )

        opened, error_message = jellydream_safe_open(
            self.session,
            chosen,
            MessageBox,
            _("Von vorn oder fortsetzen?"),
            MessageBox.TYPE_YESNO,
            timeout=10,
            default=True,
            list=[
                (_("Fortsetzen"), True),
                (_("Von vorn"), False)
            ],
            timeout_default=True
        )

        if opened:
            return True

        jd_log(
            "Resume prompt could not be opened, playback resumes: %s"
            % error_message,
            level="WARNING"
        )
        return self._start_playback(
            title=title,
            item_id=item_id,
            runtime_seconds=runtime_seconds,
            return_screen=return_screen,
            callback=callback,
            start_position_seconds=start_position_seconds,
            audio_stream_index=audio_stream_index,
            message=message
        )

    def _start_playback(
        self,
        title,
        item_id,
        runtime_seconds,
        return_screen,
        callback,
        start_position_seconds,
        audio_stream_index,
        message
    ):
        self._async_loading = True
        self.startSpinner(message)

        def play_finished(ok, error_message):
            self._async_loading = False
            self.stopSpinner()

            if not ok:
                jellydream_music_queue_clear()

            if not getattr(self, "instance", None):
                return

            if not ok:
                self.setStatus(
                    error_message
                    or _("Wiedergabe konnte nicht gestartet werden."),
                    level="ERROR"
                )

        from .playback import jellydream_safe_play_async

        started = jellydream_safe_play_async(
            session=self.session,
            title=title,
            item_id=item_id,
            runtime_seconds=runtime_seconds,
            return_screen=(
                return_screen
                if return_screen is not None
                else self
            ),
            callback=callback,
            start_position_seconds=start_position_seconds,
            audio_stream_index=audio_stream_index,
            finished_callback=play_finished
        )

        if not started:
            self._async_loading = False
            self.stopSpinner()

        return started

    def resolveBoxsetAsync(
        self,
        item_id,
        title,
        item_type,
        finished_callback
    ):
        if self._async_loading:
            return False

        item_id = str(item_id or "").strip()

        if not item_id:
            return False

        return self.runAsyncLoad(
            loader=lambda: jellyfin_get_boxset_for_item(
                item_id,
                title,
                item_type
            ),
            finished_callback=finished_callback,
            message="Sammlungszuordnung wird geprüft ..."
        )

    def _beginManualLibraryUpdate(self):
        """
        Setzt die lokale Update-Sperre atomar.

        Rückgabe:
            True  = Update darf gestartet werden.
            False = Auf diesem Screen läuft bereits ein manuelles Update.
        """
        try:
            with self._manual_library_update_lock:
                if self._manual_library_update_running:
                    return False

                self._manual_library_update_running = True
                return True

        except Exception as e:
            jd_log(
                "Local library-update lock could not be set: %s" % e,
                level="WARNING"
            )
            return False


    def _finishManualLibraryUpdate(self):
        """
        Gibt die lokale Update-Sperre zuverlässig wieder frei.
        """
        try:
            with self._manual_library_update_lock:
                self._manual_library_update_running = False

            return True

        except Exception as e:
            jd_log(
                "Local library-update lock could not be released: %s" % e,
                level="WARNING"
            )
            return False
        
    def cleanupTimers(self):
        self.stopSpinner()
        self._async_load_token += 1

        for timer_name in [
            "_spinner_timer",
            "_startup_login_timer",
            "_updateinfo_timer",
            "_backdrop_apply_timer",
            "_kaltstart_bg_timer",
            "_live_bg_timer",
            "_audio_restart_verify_timer",
            "_audio_restart_seek_timer",
            "_audio_refresh_timer",
            "_initial_audio_apply_timer",
            "_eof_close_timer",
            "_audio_eof_guard_timer",
            "_next_episode_timer",
            "keyboardTimer",
            "clockTimer",
            "async_cover_timer",
            "hide_timer",
            "refresh_timer",
            "progress_report_timer",
            "resume_timer",
            "audioRestartClearTimer",
            "audioApplyTimer",
            "audioApplyTimer2",
            "_boxset_detail_retry_timer",
            "_home_background_timer",
            "_main_connection_watchdog_timer",
            "_cover_drift_timer",
            "_media_segment_timer",
        ]:
            jellydream_stop_timer(self, timer_name)

    def setStatus(self, message, level="INFO"):
        jellydream_safe_set_status(self, message, level)

    def restartTimer(
        self,
        attr_name,
        callback,
        delay_ms=120
    ):
        return jellydream_restart_single_timer(
            self,
            attr_name,
            callback,
            delay_ms
        )
    def setWidgetGeometry(
        self,
        widget_name,
        x,
        y,
        width,
        height
    ):
        """
        Setzt Position und Größe eines Widgets zur Laufzeit.
        """
        try:
            if widget_name not in self:
                return False

            widget = self[widget_name]

            if not getattr(widget, "instance", None):
                return False

            widget.instance.move(
                ePoint(
                    int(x),
                    int(y)
                )
            )

            widget.instance.resize(
                eSize(
                    int(width),
                    int(height)
                )
            )

            try:
                widget.instance.invalidate()
            except Exception:
                pass

            return True

        except Exception as e:
            jd_log(
                "Widget geometry could not be set for '%s': %s"                % (
                    widget_name,
                    e
                ),
                level="DEBUG"
            )

        return False


    def getOriginalWidgetGeometry(self, widget_name):
        """
        Liest die originale Geometrie direkt aus dem aktuell geladenen Skin.

        Das funktioniert automatisch mit:
        - skin/hd/
        - skin/fhd/
        - skin/uhd/
        """
        try:
            if widget_name in self._widget_geometry:
                return self._widget_geometry[widget_name]

            if widget_name not in self:
                return None

            widget = self[widget_name]

            if not getattr(widget, "instance", None):
                return None

            position = widget.instance.position()
            size = widget.instance.size()

            geometry = (
                int(position.x()),
                int(position.y()),
                int(size.width()),
                int(size.height())
            )

            self._widget_geometry[widget_name] = geometry

            return geometry

        except Exception as e:
            jd_log(
                "Original geometry could not be read for '%s': %s"                % (
                    widget_name,
                    e
                ),
                level="DEBUG"
            )

        return None


    def resetWidgetGeometry(self, widget_name):
        """
        Stellt Position und Größe wieder auf die XML-Ausgangswerte zurück.
        """
        geometry = self.getOriginalWidgetGeometry(
            widget_name
        )

        if not geometry:
            return False

        x, y, width, height = geometry

        return self.setWidgetGeometry(
            widget_name,
            x,
            y,
            width,
            height
        )


    def zoomWidgetFromOriginalGeometry(
        self,
        widget_name,
        zoom_factor=1.075,
        anchor_bottom=True
    ):
        """
        Vergrößert ein Widget relativ zu seiner originalen XML-Größe.

        anchor_bottom=True:
            Das Bild wächst nach oben, die Unterkante bleibt an derselben
            Position. Das entspricht dem Verhalten auf der Hauptseite.
        """
        geometry = self.getOriginalWidgetGeometry(
            widget_name
        )

        if not geometry:
            return False

        x, y, width, height = geometry

        new_width = int(width * float(zoom_factor))
        new_height = int(height * float(zoom_factor))

        # Horizontal mittig vergrößern.
        new_x = x - int((new_width - width) / 2)

        if anchor_bottom:
            # Unterkante bleibt stehen, Bild wächst nach oben.
            new_y = y - int(new_height - height)
        else:
            # Bild wächst gleichmäßig nach oben und unten.
            new_y = y - int((new_height - height) / 2)

        return self.setWidgetGeometry(
            widget_name,
            new_x,
            new_y,
            new_width,
            new_height
        )

        
class JellyDreamGridScreen(JellyDreamBaseScreen):
    GRID_COUNT = 6
    AUTO_STATUS = True

    def __init__(self, session):
        JellyDreamBaseScreen.__init__(self, session)
        self.index = 0
        self.detail_cache = _GLOBAL_DETAILS_CACHE

        # IDs, für die aktuell ein Detailabruf läuft.
        self._detail_requests_in_flight = set()

        # Retry-Status pro Item-ID:
        # {
        #     "item-id": {
        #         "failures": 2,
        #         "next_retry": 1234567890.0
        #     }
        # }
        self._detail_retry_state = {}

        # Merkt sich den zuletzt vergrößerten Grid-Slot.
        #
        # Beispiel:
        # 1 = grid_poster1
        # 2 = grid_poster2
        # ...
        # Dadurch muss beim Navigieren nur das alte Poster zurückgesetzt
        # und das neue Poster vergrößert werden.
        self._last_zoomed_grid_slot = None

        jellydream_create_grid_widgets(self, self.GRID_COUNT)
        self["item_logo"] = Pixmap()

    def updateGridPosterZoom(
        self,
        items_list=None,
        current_index=None,
        items_per_page=None
    ):
        """
        Aktualisiert nur zwei Poster:

        1. Das vorher ausgewählte Poster wird auf Originalgröße gesetzt.
        2. Das neu ausgewählte Poster wird vergrößert.

        Vorher wurden bei jedem Tastendruck alle sichtbaren Poster mit
        move(), resize() und invalidate() bearbeitet. Das ist besonders
        auf langsameren OpenPLi-Receivern unnötig teuer.
        """
        try:
            if items_per_page is None:
                items_per_page = self.GRID_COUNT

            try:
                items_per_page = max(
                    1,
                    int(items_per_page)
                )
            except Exception:
                items_per_page = self.GRID_COUNT

            previous_slot = getattr(
                self,
                "_last_zoomed_grid_slot",
                None
            )

            # Wenn kein Inhalt sichtbar ist, das zuletzt vergrößerte Poster
            # zurücksetzen und den Zustand löschen.
            if not items_list:
                if previous_slot:
                    self.resetWidgetGeometry(
                        "grid_poster%d" % previous_slot
                    )

                self._last_zoomed_grid_slot = None
                return

            if current_index is None:
                current_index = getattr(
                    self,
                    "index",
                    0
                )

            try:
                current_index = int(current_index)
            except Exception:
                current_index = 0

            if (
                current_index < 0
                or current_index >= len(items_list)
            ):
                return

            # Bei sechs Postern:
            # Index 0 -> Slot 1
            # Index 1 -> Slot 2
            # ...
            # Index 5 -> Slot 6
            # Index 6 -> wieder Slot 1 auf der nächsten Seite.
            selected_slot = (
                current_index % items_per_page
            ) + 1

            page_start = (
                current_index // items_per_page
            ) * items_per_page

            if (
                previous_slot
                and previous_slot != selected_slot
            ):
                previous_index = (
                    page_start + previous_slot - 1
                )
                previous_item = None

                if (
                    0 <= previous_index < len(items_list)
                ):
                    previous_item = items_list[previous_index]

                if jellydream_is_next_up_item(previous_item):
                    jellydream_apply_grid_poster_item_geometry(
                        self,
                        "grid_poster%d" % previous_slot,
                        previous_item,
                        zoomed=False
                    )
                else:
                    self.resetWidgetGeometry(
                        "grid_poster%d" % previous_slot
                    )

            selected_item = items_list[current_index]

            if jellydream_is_next_up_item(selected_item):
                jellydream_apply_grid_poster_item_geometry(
                    self,
                    "grid_poster%d" % selected_slot,
                    selected_item,
                    zoomed=True,
                    zoom_factor=1.075
                )
            else:
                self.zoomWidgetFromOriginalGeometry(
                    "grid_poster%d" % selected_slot,
                    zoom_factor=1.075,
                    anchor_bottom=True
                )

            self._last_zoomed_grid_slot = selected_slot

        except Exception as e:
            jd_log(
                "Error in updateGridPosterZoom: %s" % e,
                level="DEBUG"
            )
            
    def requestSelectedItemDetails(
        self,
        items,
        item_id,
        refresh_callback=None
    ):
        """
        Liefert gecachte Details sofort zurück oder lädt sie asynchron.

        Fehlgeschlagene Abrufe werden nicht dauerhaft gesperrt. Stattdessen
        wird ein begrenztes exponentielles Backoff verwendet:

            1. Fehler:  2 Sekunden
            2. Fehler:  4 Sekunden
            3. Fehler:  8 Sekunden
            4. Fehler: 16 Sekunden
            5. Fehler: 32 Sekunden
            danach:    maximal 60 Sekunden

        Der refresh_callback wird nur aufgerufen, wenn:
        - der Screen noch existiert,
        - dieselbe Item-ID noch ausgewählt ist,
        - tatsächlich Detaildaten geladen wurden.
        """
        item_id = str(item_id or "").strip()

        if not item_id:
            return {}

        cached_details = jellydream_cache_get(
            self.detail_cache,
            item_id,
            {}
        ) or {}

        if cached_details:
            self._detail_retry_state.pop(
                item_id,
                None
            )

            self._detail_requests_in_flight.discard(
                item_id
            )

            return cached_details

        # Ein Request für dieselbe ID läuft bereits.
        if item_id in self._detail_requests_in_flight:
            return {}

        now_timestamp = time.time()

        retry_state = self._detail_retry_state.get(
            item_id,
            {}
        )

        try:
            next_retry = float(
                retry_state.get("next_retry") or 0
            )
        except Exception:
            next_retry = 0

        # Der letzte Abruf ist fehlgeschlagen und die Backoff-Zeit
        # ist noch nicht abgelaufen.
        if next_retry > now_timestamp:
            return {}

        self._detail_requests_in_flight.add(
            item_id
        )

        def details_loaded(loaded_details, loaded_item_id):
            loaded_item_id = str(
                loaded_item_id or ""
            ).strip()

            # Ein abgeschlossener Request darf niemals dauerhaft als
            # "laufend" markiert bleiben.
            self._detail_requests_in_flight.discard(
                loaded_item_id
            )

            if loaded_details:
                # Erfolgreicher Abruf: Fehlerzähler vollständig zurücksetzen.
                self._detail_retry_state.pop(
                    loaded_item_id,
                    None
                )

            else:
                previous_state = self._detail_retry_state.get(
                    loaded_item_id,
                    {}
                )

                try:
                    failure_count = int(
                        previous_state.get("failures") or 0
                    ) + 1
                except Exception:
                    failure_count = 1

                retry_delay = min(
                    DETAIL_RETRY_MAX_SECONDS,
                    DETAIL_RETRY_BASE_SECONDS
                    * (2 ** max(0, failure_count - 1))
                )

                self._detail_retry_state[
                    loaded_item_id
                ] = {
                    "failures": failure_count,
                    "next_retry": time.time() + retry_delay
                }

                jd_log(
                    "Detail request for '%s' failed. Retrying in %s seconds (failure streak: %s)"
                    % (
                        loaded_item_id,
                        retry_delay,
                        failure_count
                    ),
                    level="DEBUG"
                )

            if not getattr(self, "instance", None):
                return

            current_item = jellydream_safe_current_item(
                items,
                self.index
            )

            if not isinstance(current_item, dict):
                return

            current_item_id = str(
                current_item.get("Id") or ""
            ).strip()

            # Nutzer hat inzwischen weiter navigiert.
            if current_item_id != loaded_item_id:
                return

            if loaded_details and callable(refresh_callback):
                refresh_callback()

        started = jellydream_load_item_details_async(
            item_id,
            callback=details_loaded
        )

        if not started:
            # Normalerweise ruft jellydream_load_item_details_async()
            # den Callback auch bei einem vollen Worker-Pool auf. Diese
            # zusätzliche Absicherung verhindert dennoch eine Sperre.
            self._detail_requests_in_flight.discard(
                item_id
            )

        return {}

        
class JellyDreamPlayerBaseScreen(JellyDreamBaseScreen):
    AUTO_BACKGROUND = False
    AUTO_STATUS = False
    AUTO_SHADOW = False

    def __init__(self, session):
        JellyDreamBaseScreen.__init__(self, session)


_MENU_ICON_PIXMAPS = {}


def jellydream_menu_icon(name, tone="white"):
    key = (str(name or ""), "gold" if tone == "gold" else "white")
    cached = _MENU_ICON_PIXMAPS.get(key)

    if cached is not None:
        return cached

    path = os.path.join(
        PLUGIN_DIR,
        "images",
        "menu",
        "%s_%s.png" % key
    )

    if not os.path.isfile(path):
        return None

    from Tools.LoadPixmap import LoadPixmap

    pixmap = LoadPixmap(path)

    if pixmap is not None:
        _MENU_ICON_PIXMAPS[key] = pixmap

    return pixmap


def jellydream_apply_title_icon(screen):
    key = {
        "JellyDreamMovieListScreen": "movies",
        "JellyDreamSeriesListScreen": "series",
        "JellyDreamBoxSetListScreen": "boxsets",
        "JellyDreamFavoriteListScreen": "favorites",
        "JellyDreamSearchScreen": "search",
        "JellyDreamSettingsScreen": "settings",
        "JellyDreamMusicListScreen": "music",
        "JellyDreamMusicAlbumScreen": "music"
    }.get(getattr(screen, "SCREEN_NAME", ""))

    if not key or "title_icon" not in screen:
        return

    pixmap = jellydream_menu_icon(key, "white")

    if pixmap is None:
        return

    try:
        screen["title_icon"].setPixmap(pixmap)
        screen["title_icon"].show()
    except Exception:
        pass


def jellydream_create_grid_widgets(screen, count=6):
    for i in range(1, count + 1):
        screen[f"grid_poster{i}"] = Pixmap()
        screen[f"grid_title{i}"] = Label("")
        screen[f"grid_badge{i}"] = Label("")


_BADGE_PLATE_PIXMAPS = {}


def jellydream_write_badge_png(path, width, height):
    """Schreibt eine blaue Kapsel mit weichen Kanten. Quadratisch ist sie ein Kreis."""
    import struct
    import zlib

    width = int(width)
    height = int(height)
    radius = height / 2.0
    rows = bytearray()

    for y in range(height):
        rows.append(0)
        py = y + 0.5

        for x in range(width):
            px = x + 0.5
            center_x = radius if px < radius else (
                width - radius if px > width - radius else px
            )
            dist = (
                (px - center_x) ** 2
                + (py - height / 2.0) ** 2
            ) ** 0.5

            if dist <= radius - 1.0:
                alpha = 255
            elif dist >= radius:
                alpha = 0
            else:
                alpha = int(255 * (radius - dist))

            rows += bytes((0x3a, 0x66, 0xe3, alpha))

    def chunk(tag, data):
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff)
        )

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(rows), 9))
        + chunk(b"IEND", b"")
    )
    temporary = path + ".part"

    with open(temporary, "wb") as handle:
        handle.write(png)

    os.replace(temporary, path)


def jellydream_badge_plate_pixmap(width, height):
    key = (int(width), int(height))
    cached = _BADGE_PLATE_PIXMAPS.get(key)

    if cached is not None:
        return cached

    directory = "/tmp/jellydream_cache/badge"

    try:
        os.makedirs(directory, exist_ok=True)
    except Exception:
        return None

    path = os.path.join(
        directory,
        "badge_%sx%s.png" % key
    )

    try:
        if not os.path.isfile(path) or os.path.getsize(path) < 40:
            jellydream_write_badge_png(path, width, height)
    except Exception:
        return None

    from Tools.LoadPixmap import LoadPixmap

    pixmap = LoadPixmap(path)

    if pixmap is not None:
        _BADGE_PLATE_PIXMAPS[key] = pixmap

    return pixmap


def jellydream_badge_plate(screen, badge_name):
    plates = getattr(screen, "_jellydream_badge_plates", None)

    if plates is None:
        plates = {}
        screen._jellydream_badge_plates = plates

    plate = plates.get(badge_name)

    if plate is not None:
        return plate

    parent = getattr(screen, "instance", None)

    if parent is None:
        return None

    from enigma import ePixmap

    plate = ePixmap(parent)

    try:
        plate.setAlphatest(2)
    except Exception:
        pass

    try:
        plate.setZPosition(5)
    except Exception:
        pass

    plates[badge_name] = plate
    return plate


def jellydream_hide_badge_plate(screen, badge_name):
    plates = getattr(screen, "_jellydream_badge_plates", None) or {}
    plate = plates.get(badge_name)

    if plate is not None:
        plate.hide()


_HEART_PIXMAPS = {}


def jellydream_write_heart_png(path, size):
    """Schreibt ein rotes Herz mit weißem Rand auf transparentem Grund."""
    import struct
    import zlib

    size = int(size)
    samples = 4
    rows = bytearray()

    for y in range(size):
        rows.append(0)

        for x in range(size):
            red = 0
            white = 0

            for sy in range(samples):
                for sx in range(samples):
                    px = x + (sx + 0.5) / samples
                    py = y + (sy + 0.5) / samples
                    nx = (px / size - 0.5) * 2.55
                    ny = (0.58 - py / size) * 2.55
                    area = nx * nx + ny * ny - 1.0
                    inside = (
                        area * area * area
                        - nx * nx * ny * ny * ny
                    ) <= 0
                    nx2 = nx * 1.12
                    ny2 = ny * 1.12
                    core_area = nx2 * nx2 + ny2 * ny2 - 1.0
                    core = (
                        core_area * core_area * core_area
                        - nx2 * nx2 * ny2 * ny2 * ny2
                    ) <= 0

                    if core:
                        red += 1
                    elif inside:
                        white += 1

            total = samples * samples

            if red >= white and red > 0:
                rows += bytes((225, 22, 48, int(255 * red / total)))
            elif white > 0:
                rows += bytes((
                    255,
                    255,
                    255,
                    int(255 * (red + white) / total)
                ))
            else:
                rows += bytes((0, 0, 0, 0))

    def chunk(tag, data):
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff)
        )

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(rows), 9))
        + chunk(b"IEND", b"")
    )
    temporary = path + ".part"

    with open(temporary, "wb") as handle:
        handle.write(png)

    os.replace(temporary, path)


def jellydream_heart_pixmap(size):
    key = int(size)
    cached = _HEART_PIXMAPS.get(key)

    if cached is not None:
        return cached

    directory = "/tmp/jellydream_cache/badge"

    try:
        os.makedirs(directory, exist_ok=True)
    except Exception:
        return None

    path = os.path.join(directory, "heart_%s.png" % key)

    try:
        if not os.path.isfile(path) or os.path.getsize(path) < 40:
            jellydream_write_heart_png(path, key)
    except Exception:
        return None

    from Tools.LoadPixmap import LoadPixmap

    pixmap = LoadPixmap(path)

    if pixmap is not None:
        _HEART_PIXMAPS[key] = pixmap

    return pixmap


def jellydream_favorite_heart(screen, heart_name):
    hearts = getattr(screen, "_jellydream_hearts", None)

    if hearts is None:
        hearts = {}
        screen._jellydream_hearts = hearts

    heart = hearts.get(heart_name)

    if heart is not None:
        return heart

    parent = getattr(screen, "instance", None)

    if parent is None:
        return None

    from enigma import ePixmap

    heart = ePixmap(parent)

    try:
        heart.setAlphatest(2)
    except Exception:
        pass

    try:
        heart.setZPosition(8)
    except Exception:
        pass

    hearts[heart_name] = heart
    return heart


def jellydream_hide_favorite_heart(screen, heart_name):
    hearts = getattr(screen, "_jellydream_hearts", None) or {}
    heart = hearts.get(heart_name)

    if heart is not None:
        heart.hide()


_PROGRESS_PIXMAPS = {}


def jellydream_write_solid_png(path, red, green, blue, alpha):
    import struct
    import zlib

    rows = bytearray()

    for _y in range(4):
        rows.append(0)
        rows += bytes((red, green, blue, alpha)) * 4

    def chunk(tag, data):
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff)
        )

    png = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 4, 4, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(bytes(rows), 9))
        + chunk(b"IEND", b"")
    )
    temporary = path + ".part"

    with open(temporary, "wb") as handle:
        handle.write(png)

    os.replace(temporary, path)


def jellydream_progress_pixmap(name, color):
    cached = _PROGRESS_PIXMAPS.get(name)

    if cached is not None:
        return cached

    directory = "/tmp/jellydream_cache/badge"

    try:
        os.makedirs(directory, exist_ok=True)
    except Exception:
        return None

    path = os.path.join(directory, "progress_%s.png" % name)

    try:
        if not os.path.isfile(path) or os.path.getsize(path) < 40:
            jellydream_write_solid_png(path, *color)
    except Exception:
        return None

    from Tools.LoadPixmap import LoadPixmap

    pixmap = LoadPixmap(path)

    if pixmap is not None:
        _PROGRESS_PIXMAPS[name] = pixmap

    return pixmap


def jellydream_progress_bar(screen, bar_name):
    bars = getattr(screen, "_jellydream_progress_bars", None)

    if bars is None:
        bars = {}
        screen._jellydream_progress_bars = bars

    bar = bars.get(bar_name)

    if bar is not None:
        return bar

    parent = getattr(screen, "instance", None)

    if parent is None:
        return None

    from enigma import ePixmap

    bar = ePixmap(parent)

    try:
        bar.setAlphatest(2)
    except Exception:
        pass

    try:
        bar.setScale(1)
    except Exception:
        pass

    try:
        bar.setZPosition(6)
    except Exception:
        pass

    bars[bar_name] = bar
    return bar


def jellydream_hide_season_progress(screen, key):
    bars = getattr(screen, "_jellydream_progress_bars", None) or {}

    for suffix in ("track", "fill"):
        bar = bars.get("%s_%s" % (key, suffix))

        if bar is not None:
            bar.hide()


def jellydream_set_season_progress(screen, key, poster_name, item):
    try:
        progress = jellydream_season_progress(item)
        percent = 0 if not progress else float(progress.get("percent") or 0)

        if percent <= 0 or percent >= 99.5:
            jellydream_hide_season_progress(screen, key)
            return

        poster = screen[poster_name] if poster_name in screen else None
        poster_instance = getattr(poster, "instance", None) if poster else None

        if poster_instance is None:
            jellydream_hide_season_progress(screen, key)
            return

        size = poster_instance.size()
        pos = poster_instance.position()

        if size.width() <= 20 or size.height() <= 20:
            jellydream_hide_season_progress(screen, key)
            return

        bar_h = 8 if size.height() >= 220 else 6
        bar_w = int(size.width())
        fill_w = max(4, int(bar_w * percent / 100.0))
        bar_y = pos.y() + size.height() - bar_h
        track = jellydream_progress_bar(screen, "%s_track" % key)
        fill = jellydream_progress_bar(screen, "%s_fill" % key)
        track_pixmap = jellydream_progress_pixmap("track", (0, 0, 0, 150))
        fill_pixmap = jellydream_progress_pixmap("fill", (74, 163, 255, 255))

        if (
            track is None
            or fill is None
            or track_pixmap is None
            or fill_pixmap is None
        ):
            return

        track.setPixmap(track_pixmap)
        track.resize(eSize(bar_w, bar_h))
        track.move(ePoint(pos.x(), bar_y))
        track.show()
        fill.setPixmap(fill_pixmap)
        fill.resize(eSize(fill_w, bar_h))
        fill.move(ePoint(pos.x(), bar_y))
        fill.show()

    except Exception:
        pass


def jellydream_set_favorite_heart(screen, heart_name, poster_name, item):
    try:
        if not jellydream_item_is_favorite(item):
            jellydream_hide_favorite_heart(screen, heart_name)
            return

        poster = screen[poster_name] if poster_name in screen else None
        poster_instance = getattr(poster, "instance", None) if poster else None

        if poster_instance is None:
            jellydream_hide_favorite_heart(screen, heart_name)
            return

        size = poster_instance.size()
        pos = poster_instance.position()

        if size.width() <= 20 or size.height() <= 20:
            jellydream_hide_favorite_heart(screen, heart_name)
            return

        if size.height() >= 400:
            heart_size = 42
        elif size.height() < 220:
            heart_size = 24
        else:
            heart_size = 32

        pixmap = jellydream_heart_pixmap(heart_size)
        heart = jellydream_favorite_heart(screen, heart_name)

        if pixmap is None or heart is None:
            return

        heart.setPixmap(pixmap)
        heart.resize(eSize(heart_size, heart_size))
        heart.move(ePoint(
            pos.x() + 6,
            pos.y() + size.height() - heart_size - 6
        ))
        heart.show()

    except Exception:
        pass


def jellydream_set_poster_badge(
    screen,
    badge_name,
    poster_name,
    item,
    corner="bottom-right"
):
    try:
        jellydream_set_favorite_heart(
            screen,
            "%s_heart" % badge_name,
            poster_name,
            item
        )
        jellydream_set_season_progress(
            screen,
            "%s_progress" % badge_name,
            poster_name,
            item
        )

        if badge_name not in screen:
            return

        badge = screen[badge_name]
        text = jellydream_item_poster_badge(item)
        badge.setText(text)
        instance = getattr(badge, "instance", None)

        if not instance:
            return

        try:
            instance.setTransparent(1)
        except Exception:
            pass

        if not text:
            instance.hide()
            jellydream_hide_badge_plate(screen, badge_name)
            return

        poster = screen[poster_name] if poster_name in screen else None
        poster_instance = getattr(poster, "instance", None) if poster else None
        badge_w = 0
        badge_h = 0
        badge_x = 0
        badge_y = 0

        if poster_instance:
            size = poster_instance.size()
            pos = poster_instance.position()

            if size.width() > 20 and size.height() > 20:
                if size.height() >= 400:
                    diameter = 56
                elif size.height() >= 280:
                    diameter = 46
                elif size.width() <= 170:
                    diameter = 32
                else:
                    diameter = 36

                if diameter <= 32:
                    from enigma import gFont

                    instance.setFont(gFont("Regular", 14))

                extra = max(0, len(text) - 2)
                badge_w = diameter + extra * (diameter // 3)
                badge_h = diameter
                badge_y = (
                    pos.y() + 8
                    if corner == "top-right"
                    else pos.y() + size.height() - badge_h - 8
                )
                badge_x = pos.x() + size.width() - badge_w - 8

        if badge_w > 0 and badge_h > 0:
            instance.resize(eSize(badge_w, badge_h))
            instance.move(ePoint(badge_x, badge_y))
            plate = jellydream_badge_plate(screen, badge_name)
            pixmap = jellydream_badge_plate_pixmap(badge_w, badge_h)

            if plate is not None and pixmap is not None:
                plate.setPixmap(pixmap)
                plate.resize(eSize(badge_w, badge_h))
                plate.move(ePoint(badge_x, badge_y))
                plate.show()
        else:
            jellydream_hide_badge_plate(screen, badge_name)

        try:
            instance.setZPosition(7)
        except Exception:
            pass

        instance.show()

    except Exception:
        pass


def _jellydream_grid_album_artist(item):
    artist = item.get("AlbumArtist")

    if isinstance(artist, list):
        artist = artist[0] if artist else ""

    artist = str(artist or "").strip()

    if artist:
        return artist

    artists = item.get("Artists") or item.get("AlbumArtists") or []

    if isinstance(artists, list):
        artist = artists[0] if artists else ""
    else:
        artist = artists

    return str(artist or "").strip()


def jellydream_render_grid(screen_instance, items_list, current_index, items_per_page=ITEMS_PER_PAGE_GRID, is_search=False):
    try:
        page_start = (current_index // items_per_page) * items_per_page

        render_token = int(getattr(screen_instance, "_grid_render_token", 0)) + 1
        screen_instance._grid_render_token = render_token

        highlight_active = True
        try:
            if getattr(screen_instance, "mode", "") == "seasonbar":
                highlight_active = False
        except Exception:
            pass

        def _set_placeholder(widget, title_widget, badge_name="", keep_existing=True):
            try:
                if title_widget:
                    title_widget.setText("")
            except Exception:
                pass

            if badge_name:
                jellydream_set_poster_badge(
                    screen_instance,
                    badge_name,
                    "",
                    None
                )

            try:
                if not keep_existing:
                    jellydream_set_pixmap_if_changed(
                        widget,
                        "",
                        hide_if_missing=True
                    )
            except Exception:
                pass

        def _apply_cover_async(widget, item_id, fallback_item_id, width, height, image_type):
            def _apply_result(path, loaded_item_id):
                try:
                    if not jellydream_valid_file(path, 500):
                        return

                    loaded_item_id = str(loaded_item_id or "").strip()

                    expected_name = jellydream_expected_image_filename(
                        loaded_item_id,
                        width,
                        height,
                        quality=90,
                        image_type=image_type
                    )

                    actual_name = os.path.basename(path)

                    if (
                        expected_name
                        and actual_name.lower() != expected_name.lower()
                    ):
                        jd_log(
                            "Wrong cover discarded: expected %s, got %s"
                            % (expected_name, actual_name),
                            level="DEBUG"
                        )
                        return

                    # jellydream_async_download_image() übergibt seine
                    # Callbacks bereits im GUI-Thread. Kein zweiter
                    # ePythonMessagePump-Dispatch erforderlich.
                    if not getattr(screen_instance, "instance", None):
                        return

                    if int(getattr(
                        screen_instance,
                        "_grid_render_token",
                        0
                    )) != render_token:
                        return

                    if not widget or not getattr(widget, "instance", None):
                        return

                    jellydream_set_pixmap_if_changed(
                        widget,
                        path,
                        hide_if_missing=False
                    )

                except Exception as e:
                    jd_log(
                        "Error while applying a grid cover: %s" % e,
                        level="DEBUG"
                    )

            def _load_fallback_only_after_primary_failed():
                if not fallback_item_id:
                    return

                fallback_item_id_clean = str(fallback_item_id).strip()

                fallback_path = jellydream_get_cached_image_path(
                    fallback_item_id_clean,
                    width,
                    height,
                    image_type
                )

                if jellydream_valid_file(fallback_path, 500):
                    _apply_result(fallback_path, fallback_item_id_clean)
                    return

                jellydream_async_download_image(
                    fallback_item_id_clean,
                    jellydream_cache_dir("cover"),
                    width=width,
                    height=height,
                    image_type=image_type,
                    callback=lambda path, callback_item_id: _apply_result(
                        path,
                        callback_item_id
                    )
                )

            if not item_id:
                _load_fallback_only_after_primary_failed()
                return False

            primary_item_id = str(item_id).strip()

            primary_path = jellydream_get_cached_image_path(
                primary_item_id,
                width,
                height,
                image_type
            )

            if jellydream_valid_file(primary_path, 500):
                try:
                    if widget and getattr(widget, "instance", None):
                        jellydream_set_pixmap_if_changed(
                            widget,
                            primary_path,
                            hide_if_missing=False
                        )

                    return True

                except Exception as e:
                    jd_log(
                        "Cached cover could not be set directly: %s" % e,
                        level="DEBUG"
                    )

            def primary_finished(path, callback_item_id):
                if jellydream_valid_file(path, 500):
                    _apply_result(path, callback_item_id)
                else:
                    _load_fallback_only_after_primary_failed()

            jellydream_async_download_image(
                primary_item_id,
                jellydream_cache_dir("cover"),
                width=width,
                height=height,
                image_type=image_type,
                callback=primary_finished
            )

            return False

        for i in range(1, items_per_page + 1):
            poster_name = f"grid_poster{i}"
            title_name = f"grid_title{i}"
            badge_name = f"grid_badge{i}"

            poster_widget = screen_instance[poster_name] if poster_name in screen_instance else None
            title_widget = screen_instance[title_name] if title_name in screen_instance else None
            current_pos = page_start + (i - 1)

            if not poster_widget or not title_widget:
                continue

            if current_pos >= len(items_list):
                if hasattr(screen_instance, "resetWidgetGeometry"):
                    screen_instance.resetWidgetGeometry(
                        poster_name
                    )

                _set_placeholder(
                    poster_widget,
                    title_widget,
                    badge_name,
                    keep_existing=False
                )
                continue

            item = items_list[current_pos]

            item_name = "Eintrag"
            item_id = ""
            image_item_id = ""
            image_type = "Primary"
            width = 200
            height = 300
            fallback_image_item_id = ""
            is_next_up = False

            if isinstance(item, (list, tuple)):
                item_name = item[0] if len(item) > 0 else _("Eintrag")
                item_id = item[1] if len(item) > 1 else ""
                image_item_id = item_id

            elif isinstance(item, dict):
                item_name = item.get("Name")
                item_id = item.get("Id", "")

                if not item_name and "IndexNumber" in item:
                    season_index = jellydream_int_or_default(
                        item.get("IndexNumber"),
                        current_pos + 1
                    )

                    item_name = _("Staffel %s") % season_index

                item_type = item.get("Type") or ""
                is_next_up = jellydream_is_next_up_item(item)

                if item_type == "Season" and not is_next_up:
                    item_name = jellydream_season_display_name(
                        item,
                        current_pos + 1
                    )

                if is_next_up:
                    item_name = str(
                        item.get("_jellydream_grid_title")
                        or _("Als Nächstes")
                    )
                    image_type = "Primary"
                    image_item_id = item_id
                    fallback_image_item_id = (
                        item.get("SeriesId")
                        or getattr(
                            screen_instance,
                            "series_id",
                            ""
                        )
                    )
                    landscape_size = jellydream_apply_grid_poster_item_geometry(
                        screen_instance,
                        poster_name,
                        item,
                        zoomed=False
                    )

                    if landscape_size:
                        width, height = landscape_size
                    else:
                        width = 320
                        height = 180

                elif item_type == "Episode" or items_per_page == ITEMS_PER_PAGE_EPISODES:
                    width = 310
                    height = 174
                    image_type = "Primary"
                    image_item_id = item_id

                    fallback_image_item_id = ""
                elif item_type == "Season":
                    width = 200
                    height = 300
                    image_type = "Primary"
                    image_item_id = item_id
                    fallback_image_item_id = (
                        item.get("SeriesId")
                        or getattr(
                            screen_instance,
                            "series_id",
                            ""
                        )
                    )

                elif item_type in ("MusicAlbum", "Audio"):
                    width = 400
                    height = 400
                    image_type = "Primary"
                    image_item_id = item_id

                elif item_type in (
                    "BoxSet",
                    "CollectionFolder"
                ):
                    width = 200
                    height = 300
                    image_type = "Primary"

                    # Das BoxSet selbst nur anfragen, wenn Jellyfin dafür
                    # tatsächlich ein Primary-ImageTag geliefert hat.
                    if jellydream_item_has_image(
                        item,
                        "Primary"
                    ):
                        image_item_id = item_id
                        fallback_image_item_id = str(
                            item.get(
                                "_jellydream_primary_image_item_id"
                            )
                            or ""
                        ).strip()

                    else:
                        # Kein eigenes Sammlungsbild vorhanden:
                        # direkt das Cover eines enthaltenen Films nutzen.
                        image_item_id = str(
                            item.get(
                                "_jellydream_primary_image_item_id"
                            )
                            or ""
                        ).strip()

                        fallback_image_item_id = ""

                else:
                    image_item_id = item_id

            else:
                item_name = f"Staffel {item}"
                item_id = getattr(screen_instance, "series_id", "")
                image_item_id = item_id

            if (
                not is_next_up
                and hasattr(screen_instance, "resetWidgetGeometry")
            ):
                screen_instance.resetWidgetGeometry(
                    poster_name
                )

            if not item_name:
                item_name = "Eintrag"

            if items_per_page == 3 and isinstance(item, dict):
                s = jellydream_int_or_default(
                    item.get("ParentIndexNumber"),
                    getattr(screen_instance, "season", 1)
                )

                e = jellydream_int_or_default(
                    item.get("IndexNumber"),
                    current_pos + 1
                )

                display_name = "S%02dE%02d\n%s" % (
                    s,
                    e,
                    item_name
                )
                width = 320
                #alt height = 180
                height = 174
                image_type = "Primary"
                image_item_id = item.get("Id", "")
                fallback_image_item_id = (
                    item.get("SeriesId")
                    or item.get("SeasonId")
                    or item.get("ParentId")
                    or getattr(screen_instance, "series_id", "")
                )
            elif is_search and hasattr(screen_instance, "itemTypeName"):
                display_name = f"[{screen_instance.itemTypeName(item)}] {item_name}"
            else:
                display_name = item_name

            music_artist = ""
            if (
                isinstance(item, dict)
                and getattr(screen_instance, "SCREEN_NAME", "") == "JellyDreamMusicListScreen"
            ):
                music_artist = _jellydream_grid_album_artist(item)

            try:
                if highlight_active and current_pos == current_index:
                    if is_next_up:
                        title_widget.setText(display_name)
                    elif music_artist:
                        title_widget.setText("> %s <\n%s" % (display_name, music_artist))
                    else:
                        title_widget.setText(f"> {display_name} <")
                    if title_widget.instance:
                        try:
                            title_widget.instance.setForegroundColor(parseColor("#f1c40f"))
                        except Exception:
                            pass
                else:
                    if music_artist:
                        title_widget.setText("%s\n%s" % (display_name, music_artist))
                    else:
                        title_widget.setText(display_name)
                    if title_widget.instance:
                        try:
                            title_widget.instance.setForegroundColor(parseColor("#ffffff"))
                        except Exception:
                            pass
                try:
                    title_widget.show()
                except Exception:
                    pass
            except Exception:
                pass

            try:
                if image_item_id:

                    cover_is_ready = _apply_cover_async(
                        poster_widget,
                        image_item_id,
                        fallback_image_item_id,
                        width,
                        height,
                        image_type
                    )

                    if not cover_is_ready:
                        if width >= 300:
                            fallback_path = FALLBACK_RESUME_COVER
                        else:
                            fallback_path = FALLBACK_HOME_COVER

                        if jellydream_valid_file(fallback_path, 1):
                            jellydream_safe_set_pixmap(
                                poster_widget,
                                fallback_path,
                                hide_if_missing=False
                            )

                            try:
                                if poster_widget.instance:
                                    poster_widget.instance.invalidate()
                            except Exception:
                                pass
                        else:
                            try:
                                if poster_widget.instance:
                                    poster_widget.instance.setPixmap(None)
                                    poster_widget.instance.hide()
                            except Exception:
                                pass

                else:
                    fallback_path = (
                        FALLBACK_RESUME_COVER
                        if width >= 300
                        else FALLBACK_HOME_COVER
                    )

                    if jellydream_valid_file(
                        fallback_path,
                        1
                    ):
                        jellydream_safe_set_pixmap(
                            poster_widget,
                            fallback_path,
                            hide_if_missing=False
                        )
                    else:
                        jellydream_set_pixmap_if_changed(
                            poster_widget,
                            "",
                            hide_if_missing=True
                        )

            except Exception as e:
                jd_log(
                    "Error while rendering a grid poster: %s" % e,
                    level="DEBUG"
                )

        # Nach dem Rendern die Auswahl vergrößern.
        #
        # Diese Funktion existiert in JellyDreamGridScreen und betrifft
        # daher automatisch Filme, Serien, Staffeln, Folgen, Suche und
        # Sammlungen.
        if hasattr(screen_instance, "updateGridPosterZoom"):
            screen_instance.updateGridPosterZoom(
                items_list=items_list,
                current_index=current_index,
                items_per_page=items_per_page
            )

        for badge_index in range(1, items_per_page + 1):
            badge_pos = page_start + (badge_index - 1)
            badge_item = (
                items_list[badge_pos]
                if badge_pos < len(items_list)
                else None
            )
            jellydream_set_poster_badge(
                screen_instance,
                "grid_badge%d" % badge_index,
                "grid_poster%d" % badge_index,
                badge_item if isinstance(badge_item, dict) else None
            )

    except Exception as e:
        try:
            jd_log(
                "Error while rendering the grid: %s" % e,
                level="ERROR"
            )
        except Exception:
            pass



def jellydream_landscape_rect_in_slot(x, y, width, height):
    """
    16:9-Rechteck innerhalb eines Hochformat-Poster-Slots.
    """
    try:
        x = int(x)
        y = int(y)
        width = int(width)
        height = int(height)
    except Exception:
        return x, y, width, height

    if width <= 0 or height <= 0:
        return x, y, width, height

    target_height = int(round(width * 9.0 / 16.0))

    if target_height < 1:
        target_height = 1

    if target_height > height:
        target_width = int(round(height * 16.0 / 9.0))

        if target_width < 1:
            target_width = 1

        if target_width > width:
            target_width = width

        return (
            x + max(0, (width - target_width) // 2),
            y,
            target_width,
            height
        )

    return (
        x,
        y + max(0, (height - target_height) // 2),
        width,
        target_height
    )



def jellydream_apply_grid_poster_item_geometry(
    screen,
    poster_name,
    item=None,
    zoomed=False,
    zoom_factor=1.075
):
    if not hasattr(screen, "getOriginalWidgetGeometry"):
        return None

    geometry = screen.getOriginalWidgetGeometry(
        poster_name
    )

    if not geometry:
        return None

    x, y, width, height = geometry

    if jellydream_is_next_up_item(item):
        x, y, width, height = jellydream_landscape_rect_in_slot(
            x,
            y,
            width,
            height
        )

    if zoomed:
        new_width = int(width * float(zoom_factor))
        new_height = int(height * float(zoom_factor))
        x = x - int((new_width - width) / 2)
        y = y - int(new_height - height)
        width = new_width
        height = new_height

    if not screen.setWidgetGeometry(
        poster_name,
        x,
        y,
        width,
        height
    ):
        return None

    return (width, height)
