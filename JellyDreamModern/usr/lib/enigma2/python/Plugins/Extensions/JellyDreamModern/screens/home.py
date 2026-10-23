# -*- coding: utf-8 -*-

import os

import urllib.request
import urllib.parse
import urllib.error

from Components.ActionMap import ActionMap
from Components.Label import Label
from Screens.MessageBox import MessageBox
from Components.Pixmap import Pixmap
from Components.ProgressBar import ProgressBar
from enigma import eTimer
from skin import parseColor
from enigma import quitMainloop
from time import strftime

from .. import _

from ..constants import (
    FALLBACK_HOME_COVER,
    FALLBACK_RESUME_COVER,
    JOB_PRIORITY_IMAGE_NORMAL,
    MAIN_BACKGROUND_DEBOUNCE_MS,
    MAIN_CONNECTION_WATCHDOG_MS,
    PLUGIN_DIR,
    TIMEOUT_SHORT
)
from ..log import jd_log
from ..workers import (
    jellydream_dispatch_to_gui,
    jellydream_submit_health,
    jellydream_submit_maintenance
)
from ..config import (
    clear_config,
    jellydream_cache_dir,
    jellydream_count_saved_server_profiles,
    jellydream_dynamic_backdrops_enabled,
    jellydream_library_auto_update_enabled,
    jellydream_update_refresh_time
)
from ..http import (
    jellydream_require_login,
    jellyfin_assert_server_online,
    jellyfin_is_logged_in
)
from ..images import (
    jellydream_apply_home_background_from_movie,
    jellydream_apply_home_background_from_resume,
    jellydream_async_download_image,
    jellydream_expected_image_filename,
    jellydream_get_background_widget_size,
    jellydream_get_cached_image_path,
    jellydream_valid_file,
    set_jellydream_background
)
from ..uiutil import (
    format_resume_text,
    jellydream_episode_service_title,
    jellydream_safe_current_item,
    jellydream_safe_open,
    jellydream_safe_set_pixmap,
    jellydream_bind_status_symbols,
    jellydream_safe_set_status,
    jellydream_stop_timer,
    jellydream_two_line_title,
    jellydream_with_yellow_status,
    jellydream_wrap,
    ticks_to_seconds
)
from .. import library as jd_library
from ..library import (
    jellydream_library_cache_is_stale,
    jellydream_library_update_log_text,
    jellydream_refresh_library_cache,
    jellyfin_get_database_counts,
    jellyfin_get_home_latest,
    jellyfin_get_resume_items
)
from ..ui import JellyDreamBaseScreen, jellydream_menu_icon, jellydream_set_poster_badge
from .movies import JellyDreamMovieListScreen
from .favorites import JellyDreamFavoriteListScreen
from .seasons import JellyDreamSeasonListScreen
from .series import JellyDreamSeriesListScreen
from .search import JellyDreamSearchScreen
from .boxsets import JellyDreamBoxSetListScreen, JellyDreamBoxSetMoviesScreen
from .music import JellyDreamMusicListScreen
from .login import JellyDreamLoginScreen
from .home_info import JellyDreamHomeMovieInfoScreen
from .settings import JellyDreamSettingsScreen


class JellyDreamMain(JellyDreamBaseScreen):
    SCREEN_NAME = "JellyDreamMain"
    AUTO_STATUS = False

    def __init__(self, session):
        JellyDreamBaseScreen.__init__(self, session)

        self.serverOnline = None
        self["logo"] = Pixmap()
        self["subtitle"] = Label(_("Media für die ganze Familie\nPowered by Jellyfin"))
        self["clock"] = Label(strftime("%H:%M"))
        self["date"] = Label(self.getGermanDate())
        self["menu"] = Label("")
        self["menu_gauge"] = Label("")
        self["status"] = Label("")
        jellydream_bind_status_symbols(self["status"])
        self["status"].setText(_("OK Auswählen    BACK Zurück"))

        self["section1"] = Label("   " + _("Weiterschauen"))
        self["section2"] = Label("   " + _("Neuerscheinungen Filme"))
        self["section3"] = Label("   " + _("Neuerscheinungen Serien"))

        for i in range(1, 6):
            self[f"continue{i}"] = Label("")
            self[f"continue_bar{i}"] = ProgressBar()
            self[f"continue_poster{i}"] = Pixmap()
            self[f"continue_badge{i}"] = Label("")
            
        for i in range(1, 7):
            self[f"poster{i}"] = Pixmap()
            self[f"poster_title{i}"] = Label("")
            self[f"poster_badge{i}"] = Label("")

        self.MENU_LABELS = {
            "movies": "Filme",
            "series": "Serien",
            "boxsets": "Sammlungen",
            "music": "Musik",
            "favorites": "Favoriten",
            "search": "Suche",
            "settings": "Einstellungen",
            "switch_server": "Server wechseln",
            "login": "Login",
            "logout": "Abmelden"
        }

        self.MENU_ICONS = {
            "movies",
            "series",
            "boxsets",
            "music",
            "favorites",
            "search",
            "settings",
            "logout"
        }

        self.MENU_PROTECTED = (
            "movies",
            "series",
            "boxsets",
            "music",
            "favorites",
            "search"
        )

        self.MENU_ACTIONS = {
            "movies": {
                "type": "screen",
                "target": JellyDreamMovieListScreen
            },
            "series": {
                "type": "screen",
                "target": JellyDreamSeriesListScreen
            },
            "boxsets": {
                "type": "screen",
                "target": JellyDreamBoxSetListScreen
            },
            "music": {
                "type": "screen",
                "target": JellyDreamMusicListScreen
            },
            "favorites": {
                "type": "screen",
                "target": JellyDreamFavoriteListScreen
            },
            "search": {
                "type": "screen",
                "target": JellyDreamSearchScreen
            },
            "settings": {
                "type": "callback",
                "target": "open_settings"
            },
            "switch_server": {
                "type": "callback",
                "target": "open_login"
            },
            "login": {
                "type": "callback",
                "target": "open_login"
            },
            "logout": {
                "type": "callback",
                "target": "do_logout"
            }
        }
        
        self.focusArea = "poster"
        self.latestSide = "movies"
        self.currentPoster = 0
        self.currentResume = 0 
        self.menuIndex = 0
        
        self.home_latest = []
        self.resume_items = []
        self.remaining_cover_tasks = []

        self._home_zoom_widget = ""
        self._home_background_snapshot = None
        # Merkt sich den zuletzt gerenderten Auswahlzustand der Hauptseite.
        #
        # Aufbau:
        # (
        #     focusArea,
        #     currentResume,
        #     currentPoster,
        #     menuIndex
        # )
        #
        # Damit werden bei Navigation nur noch die wirklich betroffenen
        # Auswahl-Widgets aktualisiert.
        self._home_selection_state = None

        # Verhindert wiederholte setText()-Aufrufe mit identischem Menütext.
        self._home_menu_display_text = None

        # Verhindert, dass der Logout-Bereich bei jedem Tastendruck erneut
        # komplett ausgeblendet wird.
        self._home_logged_out_ui_applied = False
        
        self.startupLoginChecked = False
        self.loginScreenOpen = False
        self.initialDataLoaded = False

        self.clockTimer = eTimer()
        self.async_cover_timer = eTimer()

        for timer_obj, callback_func in [
            (self.clockTimer, self.updateClock),
            (self.async_cover_timer, self.checkAsyncCovers)
        ]:
            try: timer_obj.callback.append(callback_func)
            except Exception:
                try: timer_obj.timeout.get().append(callback_func)
                except Exception: pass

        self.onLayoutFinish.append(self.loadHomeLogo)
        self.onLayoutFinish.append(self.updateMenuDisplay)
        self.onLayoutFinish.append(self.startClock)

        self.onLayoutFinish.append(self.scheduleFirstLoginCheck)
        
        self.onClose.append(self.stopClock)
        self.buildMenu()

        self["actions"] = ActionMap(
            [
                "OkCancelActions",
                "DirectionActions",
                "ColorActions",
                "MenuActions"
            ],
            {
                "cancel": self.close,
                "ok": self.okPressed,
                "menu": self.openSettings,
                "blue": self.manualLibraryUpdate,
                "yellow": self.yellowPressed,
                "left": lambda: self.navigateMainScreen("left"),
                "right": lambda: self.navigateMainScreen("right"),
                "up": lambda: self.navigateMainScreen("up"),
                "down": lambda: self.navigateMainScreen("down"),
            },
            -1
        )

    def prefetchHomeBackdrops(self):
        """
        Lädt Backdrops der Neuerscheinungen im Hintergrund vor.

        Der aktuell markierte Eintrag hat Vorrang. Dadurch ist sein Backdrop
        häufig bereits vollständig im Dateicache, bevor er sichtbar benötigt
        wird.
        """
        try:
            if not jellydream_dynamic_backdrops_enabled():
                return            
            if not self.home_latest:
                return

            width, height = jellydream_get_background_widget_size(
                self
            )

            selected_index = int(
                getattr(self, "currentPoster", 0) or 0
            )

            ordered_items = []

            current_item = jellydream_safe_current_item(
                self.home_latest,
                selected_index
            )

            if isinstance(current_item, dict):
                ordered_items.append(current_item)

            for item in self.home_latest:
                if item is not current_item:
                    ordered_items.append(item)

            seen_ids = set()
            prefetched_count = 0
            max_prefetch_backdrops = 2

            for item in ordered_items:
                # Höchstens zwei unterschiedliche Backdrops einplanen:
                # 1. aktuell markierter Eintrag
                # 2. nächster sinnvoller Eintrag
                if prefetched_count >= max_prefetch_backdrops:
                    break

                if not isinstance(item, dict):
                    continue

                item_id = str(
                    item.get("Id")
                    or item.get("id")
                    or ""
                ).strip()

                if not item_id or item_id in seen_ids:
                    continue

                seen_ids.add(item_id)

                jellydream_async_download_image(
                    item_id,
                    jellydream_cache_dir("cover"),
                    width=width,
                    height=height,
                    quality=90,
                    image_type="Backdrop",
                    priority=(
                        1
                        if prefetched_count == 0
                        else JOB_PRIORITY_IMAGE_NORMAL
                    )
                )

                prefetched_count += 1

        except Exception as e:
            jd_log(
                "Home screen backdrop preload failed: %s"
                % e,
                level="DEBUG"
            )

    def mainConnectionWatchdogExpired(self):
        """
        Beendet eine festhängende Verbindungsprüfung unabhängig davon,
        ob urllib, DNS oder der TLS-Handshake noch blockiert.

        Das Inkrementieren von `_async_load_token` sorgt dafür, dass ein
        verspätetes Ergebnis des alten Workers nicht mehr angewendet wird.
        """
        try:
            if not getattr(self, "_async_loading", False):
                return

            jd_log(
                "HTTPS connection check timed out; a late worker result is discarded",
                level="WARNING"
            )

            # Laufendes Ergebnis von runAsyncLoad() ungültig machen.
            self._async_load_token += 1

            self.showServerOfflineState(
                "Zeitüberschreitung bei der HTTPS-Verbindung "
                "zum Jellyfin-Server."
            )

        except Exception as e:
            jd_log(
                "Connection watchdog failed: %s" % e,
                level="ERROR"
            )
        
    def scheduleHomeBackgroundUpdate(self):
        """
        Verzögert den Backdrop-Wechsel auf der Hauptseite.

        Im Menü bleibt der zuletzt sichtbare Hintergrund erhalten.
        """
        try:
            area = str(
                getattr(self, "focusArea", "menu") or "menu"
            )

            # Im Menü keinen neuen Hintergrund setzen.
            if area == "menu":
                self._home_background_snapshot = None

                jellydream_stop_timer(
                    self,
                    "_home_background_timer"
                )

                # Verspätete Download-Callbacks ungültig machen.
                self._active_backdrop_id = "__menu_hold__"

                return

            index = 0
            item_id = ""

            if area == "resume":
                index = int(
                    getattr(self, "currentResume", 0) or 0
                )

                item = jellydream_safe_current_item(
                    self.resume_items,
                    index
                )

            elif area == "poster":
                index = int(
                    getattr(self, "currentPoster", 0) or 0
                )

                item = jellydream_safe_current_item(
                    self.home_latest,
                    index
                )

            else:
                return

            if isinstance(item, dict):
                item_id = str(
                    item.get("Id")
                    or item.get("id")
                    or ""
                ).strip()

            snapshot = (
                area,
                index,
                item_id
            )

            self._home_background_snapshot = snapshot

            delay_ms = max(
                MAIN_BACKGROUND_DEBOUNCE_MS,
                jellydream_update_refresh_time()
            )

            self.restartTimer(
                "_home_background_timer",
                lambda current_snapshot=snapshot:
                    self.applyHomeBackgroundSnapshot(
                        current_snapshot
                    ),
                delay_ms
            )

        except Exception as e:
            jd_log(
                "Home screen backdrop delay failed: %s" % e,
                level="WARNING"
            )

    def applyHomeBackgroundSnapshot(self, snapshot):
        """
        Übernimmt den Backdrop nur, wenn die gespeicherte Auswahl noch aktiv
        ist.
        """
        try:
            if snapshot != getattr(
                self,
                "_home_background_snapshot",
                None
            ):
                return

            if not jellyfin_is_logged_in():
                set_jellydream_background(self)
                return

            area, requested_index, requested_id = snapshot

            if area == "resume":
                current_index = int(
                    getattr(self, "currentResume", 0) or 0
                )

                if current_index != requested_index:
                    return

                item = jellydream_safe_current_item(
                    self.resume_items,
                    current_index
                )

                if not isinstance(item, dict):
                    return

                current_id = str(
                    item.get("Id")
                    or item.get("id")
                    or ""
                ).strip()

                if current_id != requested_id:
                    return

                jellydream_apply_home_background_from_resume(
                    self,
                    item,
                    async_if_missing=True
                )
                return

            if area == "poster":
                current_index = int(
                    getattr(self, "currentPoster", 0) or 0
                )

                if current_index != requested_index:
                    return

                item = jellydream_safe_current_item(
                    self.home_latest,
                    current_index
                )

                if not isinstance(item, dict):
                    return

                current_id = str(
                    item.get("Id")
                    or item.get("id")
                    or ""
                ).strip()

                if current_id != requested_id:
                    return

                jellydream_apply_home_background_from_movie(
                    self,
                    item,
                    async_if_missing=True
                )
                return

            # Im Menü wird der Hintergrund nicht verändert.
            # Der zuletzt sichtbare Backdrop bleibt erhalten.
            return

        except Exception as e:
            jd_log(
                "Delayed home screen backdrop could not be applied: %s" % e,
                level="WARNING"
            )
        
    def showServerOfflineState(self, error_message=""):
        try:
            jellydream_stop_timer(
                self,
                "_main_connection_watchdog_timer"
        )
        except Exception:
            pass

        self._async_loading = False
        self.stopSpinner()

        self.serverOnline = False

        self.home_latest = []
        self.resume_items = []
        self.boxset_count = 0
        self.music_count = 0

        self.currentPoster = 0
        self.currentResume = 0
        self.focusArea = "menu"

        self.remaining_cover_tasks = []

        try:
            self.async_cover_timer.stop()
        except Exception:
            pass

        try:
            self.clearHomeCovers()
        except Exception:
            pass

        try:
            self.hideLoggedOutContentAreas()
        except Exception:
            pass

        message = str(
            error_message
            or _("Jellyfin-Server ist nicht erreichbar.")
        ).strip()

        try:
            self["status"].setText(_(message))
        except Exception:
            pass

        self.buildMenu()
        self.updateSelection()
    
    def scheduleFirstLoginCheck(self):
        self.restartTimer("_startup_login_timer", self.startFirstLoginCheck, 100)
    
    def cleanupTimers(self):
        super().cleanupTimers()
        self.remaining_cover_tasks = []

    def buildMenu(self):
        try:
            items = ["movies", "series"]

            boxset_count = getattr(self, "boxset_count", 0)

            if boxset_count > 0:
                items.append("boxsets")

            if getattr(self, "music_count", 0) > 0:
                items.append("music")

            items.append("favorites")
            items.append("search")
            items.append("settings")

            if jellyfin_is_logged_in():
                # "Server wechseln" nur anzeigen, wenn tatsächlich
                # mehr als eine Serveradresse gespeichert ist.
                if jellydream_count_saved_server_profiles() > 1:
                    items.append("switch_server")
                items.append("logout")
            else:
                items.append("login")
                
            self.main_menu_items = items

            if self.menuIndex >= len(self.main_menu_items):
                self.menuIndex = len(self.main_menu_items) - 1
            if self.menuIndex < 0:
                self.menuIndex = 0

            self.updateMenuDisplay()
        except Exception as e:
            jd_log(
                "Main menu could not be built: %s"% e,
                level="WARNING"
            )

    def loadHomeLogo(self):
        path = os.path.join(PLUGIN_DIR, "images", "jellydream_logo.png")

        try:
            widget = self["logo"]

            if widget.instance and os.path.exists(path):
                widget.instance.setPixmapFromFile(path)
                widget.instance.show()
        except Exception as e:
            jd_log(
                "Home logo could not be loaded: %s" % e,
                level="WARNING"
            )

    def startClock(self):
        self.updateClock()

    def stopClock(self):
        try:
            self.clockTimer.stop()
        except Exception:
            pass

    def resetHomeState(self):
        self.home_latest = []
        self.resume_items = []
        self.boxset_count = 0
        self.music_count = 0
        self.currentPoster = 0
        self.currentResume = 0
        self.focusArea = "menu"
        # Nach einem neuen Login soll wieder die erste Neuerscheinung
        # als initialer Backdrop verwendet werden.
        self.initialDataLoaded = False
        self.remaining_cover_tasks = []
        self.hideLoggedOutContentAreas()

    def reloadHomeData(self):
        self.buildMenu()

        if jellyfin_is_logged_in():
            self.loadMainData()
        else:
            self.refreshDatabase()
            self.updateSelection()

    def updateClock(self):
        try:
            self["clock"].setText(strftime("%H:%M"))
            self["date"].setText(self.getGermanDate())
            self.clockTimer.start(30000, True)
        except Exception:
            pass

    def loadMainDataOnline(self):
        try:
            if not jellyfin_is_logged_in():
                self.refreshDatabase()
                self.updateSelection()
                return

            cache_needs_update = (
                jellydream_library_auto_update_enabled()
                and jellydream_library_cache_is_stale(
                    24 * 60 * 60
                )   
            )

            if not cache_needs_update:
                self.refreshDatabase()
                self.updateSelection()
                return

            jd_log(
                "Library cache is older than 24 hours -> refreshing from the home screen",
                level="INFO"
            )

            started = self.runAsyncLoad(
                loader=jellydream_refresh_library_cache,
                finished_callback=self.mainLibraryUpdateFinished,
                message=(
                    "Film- und Serienbibliothek wird "
                    "aktualisiert ..."
                )
            )

            if not started:
                self.refreshDatabase()
                self.updateSelection()

        except Exception as e:
            self.stopSpinner()

            jd_log(
                "Home screen library check failed: %s" % e,
                level="WARNING"
            )

            self.refreshDatabase()
            self.updateSelection()

    def loadMainData(self, *args):
        try:
            if self._async_loading:
                return

            if not jellyfin_is_logged_in():
                self.serverOnline = False
                self.resetHomeState()
                self.refreshDatabase()
                self.updateSelection()
                return

            self.serverOnline = None

            self["status"].setText(_("Jellyfin-Verbindung wird geprüft ..."))

            started = self.runAsyncLoad(
                loader=lambda: jellyfin_assert_server_online(
                    timeout=TIMEOUT_SHORT
                ),
                finished_callback=self.mainServerCheckFinished,
                message="Jellyfin-Verbindung wird geprüft ...",
                job_submitter=jellydream_submit_health
            )

            if started:
                self.restartTimer(
                    "_main_connection_watchdog_timer",
                    self.mainConnectionWatchdogExpired,
                    MAIN_CONNECTION_WATCHDOG_MS
                )
            else:
                self.showServerOfflineState(
                    "Jellyfin-Verbindung konnte nicht geprüft werden."
                )

        except Exception as e:
            jd_log(
                "Jellyfin connection check could not be started: %s" % e,
                level="WARNING"
            )

            self.showServerOfflineState(
                "Jellyfin-Server ist nicht erreichbar."
            )

    def mainServerCheckFinished(self, result, error_message=""):
        try:
            jellydream_stop_timer(
                self,
                "_main_connection_watchdog_timer"
            )

            if error_message:
                self.showServerOfflineState(
                    error_message
                )
                return

            if result is not True:
                self.showServerOfflineState(
                    "Jellyfin-Server ist nicht erreichbar."
                )
                return

            self.serverOnline = True

            self.showLoggedInContentAreas()
            self.loadMainDataOnline()

        except Exception as e:
            jd_log(
                "Jellyfin connection check result could not be processed: %s" % e,
                level="WARNING"
            )

            self.showServerOfflineState(
                "Jellyfin-Server ist nicht erreichbar."
            )
        
    def mainLibraryUpdateFinished(
        self,
        result,
        error_message=""
    ):
        try:
            if error_message:
                jd_log(
                    "Home screen library refresh failed: %s"
                    % error_message,
                    level="WARNING"
                )

                self["status"].setText(
                    _("Bibliothek konnte nicht aktualisiert werden.")
                )

            elif isinstance(result, dict):
                message = str(
                    result.get("message")
                    or _("Bibliotheksaktualisierung beendet")
                )
                log_message = jellydream_library_update_log_text(result)

                if result.get("ok"):
                    jd_log(
                        "Home screen library refresh finished: %s" % log_message,
                        level="INFO"
                    )

                    self["status"].setText(message)

                elif result.get("busy"):
                    jd_log(
                        "Library refresh is already running",
                        level="DEBUG"
                    )

                    self["status"].setText(
                        _("Bibliotheksaktualisierung läuft bereits.")
                    )

                else:
                    jd_log(
                        "Library refresh was not successful: %s" % log_message,
                        level="WARNING"
                    )

                    self["status"].setText(
                        _("Bibliothek konnte nicht aktualisiert werden.")
                    )

            self.refreshDatabase()
            self.updateSelection()

        except Exception as e:
            jd_log(
                "Library refresh result could not be applied: %s" % e,
                level="WARNING"
            )

            try:
                self.refreshDatabase()
                self.updateSelection()
            except Exception:
                pass
                
    def startFirstLoginCheck(self):
        try:
            if getattr(self, "startupLoginChecked", False):
                return

            self.startupLoginChecked = True

            if jellyfin_is_logged_in():
                self.loadMainData()
                return

            self.serverOnline = False
            self.resetHomeState()
            self.buildMenu()
            self.updateSelection()

            if not getattr(self, "loginScreenOpen", False):
                self.loginScreenOpen = True

                ok, err = jellydream_safe_open(
                    self.session,
                    self.afterLogin,
                    JellyDreamLoginScreen
                )

                if not ok:
                    self.loginScreenOpen = False

                    jellydream_safe_set_status(
                        self,
                        err,
                        level="ERROR"
                    )

        except Exception as e:
            jd_log(
                "Error during the initial login check: %s"% e,
                level="WARNING"
            )

    def updateHomeCovers(self):
        try:
            for i in range(1, 7):
                poster_name = f"poster{i}"
                title_name = f"poster_title{i}"

                poster_widget = self[poster_name] if poster_name in self else None
                title_widget = self[title_name] if title_name in self else None

                cover_path = ""
                title = ""

                try:
                    if self.home_latest and i <= len(self.home_latest):
                        latest_item = self.home_latest[i - 1]

                        if isinstance(latest_item, dict):
                            item_id = latest_item.get("Id") or latest_item.get("id") or ""
                            title = latest_item.get("Name") or latest_item.get("name") or _("Film")
                        else:
                            item_id = ""
                            title = ""

                        if item_id:
                            cover_dir = jellydream_cache_dir("cover")
                            cover_path = jellydream_get_cached_image_path(
                                item_id,
                                230,
                                318,
                                "Primary"
                            )

                            if not jellydream_valid_file(
                                cover_path,
                                500
                            ):
                                cover_path = (
                                    FALLBACK_HOME_COVER
                                    if jellydream_valid_file(
                                        FALLBACK_HOME_COVER,
                                        1
                                    )
                                    else ""
                                )

                                current_slot = i
                                current_item_id = str(item_id)
                                current_title = str(title)

                                def cover_finished(
                                    path,
                                    loaded_item_id,
                                    slot=current_slot,
                                    expected_id=current_item_id,
                                    loaded_title=current_title
                                ):
                                    if not getattr(
                                        self,
                                        "instance",
                                        None
                                    ):
                                        return

                                    if str(loaded_item_id) != expected_id:
                                        return

                                    current_index = slot - 1

                                    if not (
                                        0 <= current_index
                                        < len(self.home_latest)
                                    ):
                                        return

                                    current_item = self.home_latest[
                                        current_index
                                    ]

                                    if not isinstance(
                                        current_item,
                                        dict
                                    ):
                                        return

                                    visible_id = str(
                                        current_item.get("Id")
                                        or current_item.get("id")
                                        or ""
                                    )

                                    if visible_id != expected_id:
                                        return

                                    if jellydream_valid_file(path, 500):
                                        jellydream_safe_set_pixmap(
                                            self[
                                                "poster%d" % slot
                                            ],
                                            path,
                                            hide_if_missing=False
                                        )

                                        title_name = (
                                            "poster_title%d" % slot
                                        )

                                        is_selected = (
                                            self.focusArea == "poster"
                                            and self.currentPoster
                                            == (slot - 1)
                                        )

                                        display_title = (
                                            "> %s <" % loaded_title
                                            if is_selected
                                            else loaded_title
                                        )

                                        self._setHomeLabelTextIfChanged(
                                            title_name,
                                            jellydream_two_line_title(
                                                display_title,
                                                width=23
                                            )
                                        )

                                        self._setHomeLabelColorIfChanged(
                                            title_name,
                                            "#f1c40f"
                                            if is_selected
                                            else "#ffffff"
                                        )

                                jellydream_async_download_image(
                                    item_id,
                                    cover_dir,
                                    width=230,
                                    height=318,
                                    image_type="Primary",
                                    callback=cover_finished
                                )

                            plugin_cover = os.path.join(PLUGIN_DIR, "images", "covers", f"cover{i}.jpg")
                            if (not cover_path or not os.path.exists(cover_path) or os.path.getsize(cover_path) <= 500) and os.path.exists(plugin_cover):
                                cover_path = plugin_cover

                            if (not cover_path or not os.path.exists(cover_path) or os.path.getsize(cover_path) <= 0) and os.path.exists(FALLBACK_HOME_COVER):
                                cover_path = FALLBACK_HOME_COVER
                except Exception:
                    cover_path = FALLBACK_HOME_COVER if os.path.exists(FALLBACK_HOME_COVER) else ""

                if jellydream_safe_set_pixmap(poster_widget, cover_path, hide_if_missing=True):
                    if title_widget:
                        title_widget.setText(jellydream_two_line_title(title, width=23))
                else:
                    if title_widget:
                        title_widget.setText("")

                badge_item = None
                if (
                    self.home_latest
                    and i <= len(self.home_latest)
                    and isinstance(self.home_latest[i - 1], dict)
                    and title
                ):
                    badge_item = self.home_latest[i - 1]

                jellydream_set_poster_badge(
                    self,
                    "poster_badge%d" % i,
                    poster_name,
                    badge_item
                )

            # Kein Hintergrundaufruf an dieser Stelle.
            # Der Backdrop wird zentral und verzögert durch
            # updateSelection() -> scheduleHomeBackgroundUpdate() gesetzt.
                
        except Exception as e:
            try:
                jd_log(
                    "Exception while updating home screen covers: %s"
                    % e,
                    level="ERROR"
                )
            except Exception:
                pass

    def clearHomeCovers(self):
        for i in range(1, 7):
            try:
                if self[f"poster{i}"].instance: self[f"poster{i}"].instance.hide()
            except Exception:
                pass
        for i in range(1, 6):
            try:
                if self[f"continue_poster{i}"].instance: self[f"continue_poster{i}"].instance.hide()
            except Exception:
                pass
            jellydream_set_poster_badge(
                self,
                "continue_badge%d" % i,
                "continue_poster%d" % i,
                None
            )

    def hideLoggedOutContentAreas(self):
        self.focusArea = "menu"
        self.currentPoster = 0
        self.currentResume = 0

        try:
            self["section1"].setText("")
            self["section2"].setText("")
            self["section3"].setText("")
        except Exception:
            pass

        for i in range(1, 6):
            try:
                self[f"continue{i}"].setText("")
                if self[f"continue{i}"].instance:
                    self[f"continue{i}"].instance.hide()
            except Exception:
                pass

            try:
                self[f"continue_bar{i}"].setValue(0)
                if self[f"continue_bar{i}"].instance:
                    self[f"continue_bar{i}"].instance.hide()
            except Exception:
                pass

            try:
                if self[f"continue_poster{i}"].instance:
                    self[f"continue_poster{i}"].instance.hide()
            except Exception:
                pass

        for i in range(1, 7):
            try:
                self[f"poster_title{i}"].setText("")
                if self[f"poster_title{i}"].instance:
                    self[f"poster_title{i}"].instance.hide()
            except Exception:
                pass

            try:
                if self[f"poster{i}"].instance:
                    self[f"poster{i}"].instance.hide()
            except Exception:
                pass
            jellydream_set_poster_badge(
                self,
                "poster_badge%d" % i,
                "poster%d" % i,
                None
            )

    def showLoggedInContentAreas(self):
        try:
            if self["section1"].instance:
                self["section1"].instance.show()
        except Exception:
            pass

        try:
            if self["section2"].instance:
                self["section2"].instance.show()
        except Exception:
            pass

        try:
            if self["section3"].instance:
                self["section3"].instance.show()
        except Exception:
            pass

        for i in range(1, 6):
            try:
                if self[f"continue{i}"].instance:
                    self[f"continue{i}"].instance.show()
            except Exception:
                pass

            try:
                if self[f"continue_bar{i}"].instance:
                    self[f"continue_bar{i}"].instance.show()
            except Exception:
                pass

        for i in range(1, 7):
            try:
                if self[f"poster_title{i}"].instance:
                    self[f"poster_title{i}"].instance.show()
            except Exception:
                pass

            try:
                if self[f"poster{i}"].instance:
                    self[f"poster{i}"].instance.show()
            except Exception:
                pass
                
    def navigateMainScreen(self, direction):
        if not jellyfin_is_logged_in():
            if self.focusArea != "menu":
                self.focusArea = "menu"
                self.currentPoster = 0
                self.currentResume = 0

            if direction in ("left", "right"):
                if self.main_menu_items:
                    if direction == "left":
                        self.menuIndex = (self.menuIndex - 1) % len(self.main_menu_items)
                    else:
                        self.menuIndex = (self.menuIndex + 1) % len(self.main_menu_items)

            if hasattr(self, "updateSelection"):
                self.updateSelection()
            return
            
        try:
            if direction == "left":
                if self.focusArea == "menu" and self.main_menu_items:
                    self.menuIndex = (self.menuIndex - 1) % len(self.main_menu_items)
                elif self.focusArea == "latest":
                    self._stepLatestHeading(-1)
                elif self.focusArea == "poster":
                    self._stepHomePoster(-1)
                elif self.focusArea == "resume" and getattr(self, "currentResume", 0) > 0:
                    self.currentResume -= 1

            elif direction == "right":
                if self.focusArea == "menu" and self.main_menu_items:
                    self.menuIndex = (self.menuIndex + 1) % len(self.main_menu_items)
                elif self.focusArea == "latest":
                    self._stepLatestHeading(1)
                elif self.focusArea == "poster":
                    self._stepHomePoster(1)
                elif self.focusArea == "resume" and getattr(self, "currentResume", 0) < len(self.resume_items) - 1:
                    self.currentResume += 1

            elif direction == "up":
                if self.focusArea == "poster":
                    self.focusArea = "latest"
                    self.latestSide = (
                        "series"
                        if self.currentPoster >= 3
                        else "movies"
                    )
                elif self.focusArea == "latest":
                    if self.resume_items:
                        self.focusArea = "resume"
                        self.currentResume = 0
                    else:
                        self.focusArea = "menu"
                elif self.focusArea == "resume":
                    self.focusArea = "menu"

            elif direction == "down":
                if self.focusArea == "menu":
                    if self.resume_items:
                        self.focusArea = "resume"
                        self.currentResume = 0
                    elif self._homeLatestHasItems():
                        self.focusArea = "latest"
                        self.latestSide = self._firstLatestSide()
                    else:
                        self["status"].setText(_("Keine Inhalte zur Auswahl vorhanden"))
                elif self.focusArea == "resume" and self._homeLatestHasItems():
                    self.focusArea = "latest"
                    self.latestSide = self._firstLatestSide()
                elif (
                    self.focusArea == "latest"
                    and self._homeLatestSideHasItems(self.latestSide)
                ):
                    self.focusArea = "poster"
                    self.currentPoster = self._firstPosterOnSide(
                        self.latestSide
                    )

            if hasattr(self, "updateSelection"):
                self.updateSelection()

        except Exception as e:
            jd_log(
                "Error during home screen navigation: %s" % e,
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

            # Global prüfen, ob beispielsweise der Scheduler oder ein anderer
            # Screen bereits eine Aktualisierung gestartet hat.
            with jd_library._LIBRARY_CACHE_LOCK:
                update_running = bool(
                    jd_library._LIBRARY_UPDATE_IN_PROGRESS
                )

            if update_running:
                self["status"].setText(
                    _("Bibliotheksaktualisierung läuft bereits ...")
                )
                return

            # Lokale Sperre atomar setzen. Dadurch kann mehrfaches schnelles
            # Drücken auf BLAU nicht mehrere Jobs dieses Screens erzeugen.
            if not self._beginManualLibraryUpdate():
                self["status"].setText(
                    _("Bibliotheksaktualisierung läuft bereits ...")
                )
                return

            self["status"].setText(
                _("Film- und Serienbibliothek wird aktualisiert ...")
            )

            self.startSpinner(
                _("Film- und Serienbibliothek wird aktualisiert ...")
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
                        "Manual library update on the home screen failed: %s" % e,
                        level="ERROR"
                    )

                def finished():
                    # Sperre immer zuerst freigeben. Das funktioniert auch,
                    # wenn der Screen inzwischen geschlossen wurde.
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
                            self["status"].setText(message)

                            jd_log(
                                "Manual library update on the home screen succeeded: %s"
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
                                "Manual library update on the home screen was not successful: %s"
                                % log_message,
                                level="WARNING"
                            )

                    except Exception as e:
                        try:
                            self.stopSpinner()
                        except Exception:
                            pass

                        jd_log(
                            "Update result could not be shown on the home screen: %s"
                            % e,
                            level="WARNING"
                        )

                if not jellydream_dispatch_to_gui(finished):
                    self._finishManualLibraryUpdate()

                    jd_log(
                        "Manual library update result could not be dispatched to the GUI thread",
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
                    "Manual library update on the home screen was rejected by the worker pool",
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
                "Manual library update on the home screen could not be started: %s"
                % e,
                level="ERROR"
            )
        
    def openSettings(self):
        try:
            ok, err = jellydream_safe_open(
                self.session,
                self.handle_settings_close,
                JellyDreamSettingsScreen
            )

            if not ok:
                jellydream_safe_set_status(
                    self,
                    err,
                    level="ERROR"
                )

        except Exception as e:
            jellydream_safe_set_status(
                self,
                _("Einstellungen konnten nicht geöffnet werden: %s") % e,
                level="ERROR"
            )
        
    def yellowPressed(self):
        item = None

        if self.focusArea == "resume":
            item = jellydream_safe_current_item(
                self.resume_items,
                self.currentResume
            )
        elif self.focusArea == "poster":
            item = self._homePosterItem(self.currentPoster)

        self.openItemStateMenu(
            item,
            self.refreshDatabase
        )

    def okPressed(self):
        if self.focusArea == "resume":
            item = jellydream_safe_current_item(self.resume_items, self.currentResume)
            if not item:
                jellydream_safe_set_status(
                    self,
                    _("Kein gültiger Weiterschauen-Eintrag ausgewählt"),
                    level="WARNING"
                )
                return

            item_id = str(item.get("Id", "") or item.get("id", "")).strip()
            title = item.get("Name", "Jellyfin Video")
            if str(item.get("Type") or "") == "Episode":
                title = jellydream_episode_service_title(
                    series_name=item.get("SeriesName"),
                    season_number=item.get("ParentIndexNumber"),
                    episode_number=item.get("IndexNumber"),
                    episode_name=title
                )

            if not item_id:
                jellydream_safe_set_status(
                    self,
                    _("Fehler: Resume-ID fehlt"),
                    level="WARNING"
                )
                return

            user_data = item.get("UserData") or {}
            playback_ticks = user_data.get("PlaybackPositionTicks") or 0
            start_position_seconds = ticks_to_seconds(playback_ticks)
            runtime_seconds = ticks_to_seconds(item.get("RunTimeTicks"))

            callback_target = self.loadMainData

            self.playItemAsync(
                title=title,
                item_id=item_id,
                runtime_seconds=runtime_seconds,
                return_screen=self,
                callback=callback_target,
                start_position_seconds=start_position_seconds,
                message=_("Fortsetzungsdaten werden geladen ...")
            )
            return

        elif self.focusArea == "latest":
            self.openLatestOverview()
            return

        elif self.focusArea == "poster":
            if not jellyfin_is_logged_in():
                jellydream_safe_set_status(
                    self,
                    _("Bitte zuerst einloggen"),
                    level="WARNING"
                )
                return

            item = jellydream_safe_current_item(self.home_latest, self.currentPoster)
            if not item:
                jellydream_safe_set_status(
                    self,
                    _("Kein Eintrag an dieser Position"),
                    level="WARNING"
                )
                return

            if isinstance(item, (list, tuple)):
                title = item[0] if len(item) > 0 else "Jellyfin Eintrag"
                item_id = item[1] if len(item) > 1 else ""
                item_type = item[2] if len(item) > 2 else "Movie"
                item_data = {}
            else:
                title = item.get("Name", "Jellyfin Eintrag")
                item_id = item.get("Id", "")
                item_type = item.get("Type", "Movie")
                item_data = item

            item_id = str(item_id or "").strip()
            if not item_id:
                jellydream_safe_set_status(
                    self,
                    _("Fehler: ID fehlt"),
                    level="WARNING"
                )
                return

            callback_target = self.loadMainData

            if item_type == "Series":
                ok, err = jellydream_safe_open(
                    self.session,
                    callback_target,
                    JellyDreamSeasonListScreen,
                    item_id,
                    title
                )
                if not ok:
                    jellydream_safe_set_status(self, err, level="ERROR")
                return

            def boxset_resolved(result, error_message=""):
                if error_message:
                    self.setStatus(
                        error_message,
                        level="ERROR"
                    )
                    return

                try:
                    boxset_id, boxset_name = result
                except Exception:
                    boxset_id, boxset_name = None, title

                if boxset_id:
                    ok, err = jellydream_safe_open(
                        self.session,
                        callback_target,
                        JellyDreamBoxSetMoviesScreen,
                        boxset_id,
                        boxset_name
                    )

                    if not ok:
                        self.setStatus(
                            err,
                            level="ERROR"
                        )
                    return

                ok, err = jellydream_safe_open(
                    self.session,
                    callback_target,
                    JellyDreamHomeMovieInfoScreen,
                    title,
                    item_id,
                    item_data,
                    self,
                    preloaded_details=item_data
                )

                if not ok:
                    self.setStatus(
                        err,
                        level="ERROR"
                    )

            self.resolveBoxsetAsync(
                item_id,
                title,
                item_type,
                boxset_resolved
            )
            return

        else:
            try:
                action_id = self.main_menu_items[self.menuIndex]
            except Exception:
                jellydream_safe_set_status(
                    self,
                    _("Menüeintrag konnte nicht ermittelt werden"),
                    level="WARNING"
                )
                return

            action = self.MENU_ACTIONS.get(action_id)
            if not action:
                jellydream_safe_set_status(
                    self,
                    _("Unbekannte Menüaktion"),
                    level="WARNING"
                )
                return

            callback_target = self.loadMainData

            if action["type"] == "screen":
                if action_id in self.MENU_PROTECTED and not jellydream_require_login(
                    self,
                    _(
                        "Bitte zuerst einloggen, um auf diese "
                        "Inhalte zuzugreifen."
                    )
                ):
                    return

                ok, err = jellydream_safe_open(
                    self.session,
                    callback_target,
                    action["target"]
                )
                if not ok:
                    jellydream_safe_set_status(self, err, level="ERROR")

            elif action["type"] == "callback":
                target = action["target"]

                if target == "open_settings":
                    self.openSettings()

                elif target == "open_login":
                    self.loginScreenOpen = True
                    ok, err = jellydream_safe_open(
                        self.session,
                        self.afterLogin,
                        JellyDreamLoginScreen
                    )
                    if not ok:
                        self.loginScreenOpen = False
                        jellydream_safe_set_status(self, err, level="ERROR")

                elif target == "do_logout":
                    ok, err = jellydream_safe_open(
                        self.session,
                        self.confirmLogoutCallback,
                        MessageBox,
                        _(
                            "Möchtest du dich vom aktuell aktiven "
                            "Jellyfin-Server abmelden?\n\n"
                            "Nur dieses Serverprofil wird gelöscht. "
                            "Andere gespeicherte Server bleiben erhalten."
                        ),
                        MessageBox.TYPE_YESNO,
                        timeout=0,
                        default=False
                    )
                    if not ok:
                        jellydream_safe_set_status(self, err, level="ERROR")
                else:
                    jellydream_safe_set_status(
                        self,
                        _("Unbekannte Menüaktion"),
                        level="WARNING"
                    )
        
    def confirmLogoutCallback(self, answer):
        try:
            if answer:
                if clear_config():
                    self.resetHomeState()
                    self.loginScreenOpen = False
                    self.menuIndex = 0
                    self.reloadHomeData()
                    self["status"].setText(_("Erfolgreich abgemeldet."))
                else:
                    self["status"].setText(_("Abmeldung fehlgeschlagen."))
            else:
                if "status" in self:
                    self["status"].setText(_("Abmeldung abgebrochen."))
        except Exception as e:
            try:
                jd_log(f"Error in confirmLogoutCallback: {e}", level="ERROR")
            except Exception:
                pass

    def handle_settings_close(self, result=None):

        if result == "dynamic_backdrops_changed":
            if not jellydream_dynamic_backdrops_enabled():
                # Sofort den momentan sichtbaren Jellyfin-Backdrop durch
                # den normalen Standardhintergrund ersetzen.
                set_jellydream_background(self)

            else:
                # Bei erneuter Aktivierung muss updateSelection() den
                # aktuellen Auswahlzustand neu bewerten und den passenden
                # Backdrop wieder einplanen.
                self._home_selection_state = None

                try:
                    self.updateSelection()
                except Exception as e:
                    jd_log(
                        "Backdrop could not be reevaluated after activation: %s" % e,
                        level="DEBUG"
                    )

            return

        restart_messages = {
            "trigger_mainmenu_restart": _(
                "Damit die Änderung im Receiver-Hauptmenü "
                "wirksam wird, muss die Benutzeroberfläche "
                "(GUI) neu gestartet werden.\n\n"
                "JETZT neu starten?"
            ),
            "trigger_image_worker_restart": _(
                "Damit die neue Anzahl der Image-Worker für Cover "
                "und Backdrops wirksam wird, muss die "
                "Benutzeroberfläche (GUI) neu gestartet werden.\n\n"
                "JETZT neu starten?"
            )
        }

        restart_message = restart_messages.get(result)

        if restart_message:
            self.session.openWithCallback(
                self.execute_mainmenu_gui_restart,
                MessageBox,
                restart_message,
                MessageBox.TYPE_YESNO,
                timeout=0,
                default=False
            )
        else:
            self.reloadHomeData()

    def execute_mainmenu_gui_restart(self, answer):

        if answer:
            if hasattr(self, "session") and hasattr(self.session, "nav"):
                self.session.nav.stopService()
            
            quitMainloop(3)
        else:
            if hasattr(self, "refreshDatabase"):
                self.refreshDatabase()

    def afterLogin(self, answered=None):
        self.loginScreenOpen = False

        try:
            if getattr(self, "instance", None):
                self.instance.show()
        except Exception:
            pass

        if answered and jellyfin_is_logged_in():
            self["status"].setText(_("Erfolgreich angemeldet!"))
            self.reloadHomeData()
        else:
            self["status"].setText(_("Anmeldung fehlgeschlagen oder abgebrochen."))
            if not jellyfin_is_logged_in():
                self.resetHomeState()
            self.buildMenu()
            self.updateSelection()

    def loadHomeSnapshotWorker(self):
        return {
            "counts": jellyfin_get_database_counts() or {},
            "latest": jellyfin_get_home_latest(6) or [],
            "resume": jellyfin_get_resume_items(5) or []
        }
        
    def refreshDatabase(self, *args):
        if not jellyfin_is_logged_in():
            return

        if self._async_loading:
            return

        self.runAsyncLoad(
            loader=self.loadHomeSnapshotWorker,
            finished_callback=self.databaseCountsLoaded,
            message="Startseitendaten werden geladen ..."
        )
        
    def databaseCountsLoaded(
        self,
        snapshot,
        error_message=""
    ):
        if error_message:
            jd_log(
                "Home screen data could not be loaded: %s"
                % error_message,
                level="WARNING"
            )
            return

        snapshot = (
            snapshot
            if isinstance(snapshot, dict)
            else {}
        )

        counts = snapshot.get("counts") or {}

        self.home_latest = (
            list(snapshot.get("latest") or [])
        )

        self.resume_items = (
            list(snapshot.get("resume") or [])
        )

        # Beim ersten erfolgreichen Laden der Startseite einen initialen
        # Backdrop festlegen. Bevorzugt wird die erste Neuerscheinung.
        if not self.initialDataLoaded:
            if self._homeLatestHasItems():
                self.focusArea = "poster"
                self.currentPoster = self._firstHomePosterIndex()

            elif self.resume_items:
                self.focusArea = "resume"
                self.currentResume = 0

            else:
                self.focusArea = "menu"

            self.initialDataLoaded = True
    
        self.boxset_count = int(counts.get("boxsets") or 0)
        self.music_count = int(counts.get("music") or 0)

        self.showLoggedInContentAreas()

        # Neue Daten können andere Poster, Titel oder Resume-Zeilen
        # enthalten. Deshalb beim nächsten updateSelection() einmalig
        # alle Auswahlfarben und Titel sauber initialisieren.
        self._home_selection_state = None

        self.updateContinueRows()
        self.updateHomeCovers()
        # Backdrops parallel zu den Postern vorladen.
        self.prefetchHomeBackdrops()
        
        self.buildMenu()
        self.updateSelection()     
            
    def updateContinueRows(self):
        rows = []
        percents = []
        cover_tasks = []

        try:
            items_to_parse = self.resume_items[:5] if (hasattr(self, "resume_items") and self.resume_items) else []

            for i, item in enumerate(items_to_parse, start=1):
                title = item.get("Name", _("Fortsetzen"))
                item_id = item.get("Id") or item.get("id") or ""
                item_type = item.get("Type") or ""
                episode_info = ""

                if item_type == "Episode":
                    series = item.get("SeriesName") or ""
                    season = item.get("ParentIndexNumber")
                    episode = item.get("IndexNumber")

                    if series:
                        title = series
                        if season is not None and episode is not None:
                            episode_info = f"S{int(season):02d}E{int(episode):02d}"

                title = jellydream_wrap(title, 24, 1)
                runtime = ticks_to_seconds(item.get("RunTimeTicks"))
                user_data = item.get("UserData") or {}
                position = ticks_to_seconds(user_data.get("PlaybackPositionTicks"))

                percent = int((position * 100) / runtime) if runtime else 0
                percent = max(0, min(100, percent))

                rows.append(format_resume_text(
                    title=title,
                    episode_info=episode_info,
                    item_type=item_type,
                    production_year=item.get(
                        "ProductionYear",
                        "-"
                    ),
                    original_title=(
                        item.get("Name", "")
                        if item_type == "Episode"
                        else ""
                    )
                ))
                percents.append(percent)

                widget_name = f"continue_poster{i}"
                widget = self[widget_name] if widget_name in self else None

                if item_id:
                    if item_type == "Episode":
                        expected_path = os.path.join(
                            jellydream_cache_dir("cover"),
                            jellydream_expected_image_filename(
                                item_id,
                                310,
                                174,
                                quality=90,
                                image_type="Primary"
                            )
                        )
                    else:
                        expected_path = os.path.join(
                            jellydream_cache_dir("cover"),
                            jellydream_expected_image_filename(
                                item_id,
                                310,
                                174,
                                quality=90,
                                image_type="Backdrop"
                            )
                        )

                    if os.path.exists(expected_path) and os.path.getsize(expected_path) > 500:
                        jellydream_safe_set_pixmap(
                            widget,
                            expected_path,
                            hide_if_missing=True
                        )
                    else:
                        fallback_resume = FALLBACK_RESUME_COVER if os.path.exists(FALLBACK_RESUME_COVER) else ""
                        jellydream_safe_set_pixmap(widget, fallback_resume, hide_if_missing=True)
                        cover_tasks.append((item_id, expected_path, i))
                else:
                    fallback_resume = FALLBACK_RESUME_COVER if os.path.exists(FALLBACK_RESUME_COVER) else ""
                    jellydream_safe_set_pixmap(widget, fallback_resume, hide_if_missing=True)

        except Exception as e:
            try:
                print(f"[JellyDream] Fehler in updateContinueRows Schleife: {e}")
            except Exception:
                pass
            rows, percents = [], []

        for i in range(1, 6):
            try:
                row = rows[i - 1] if i - 1 < len(rows) else ""
                percent_value = percents[i - 1] if i - 1 < len(percents) else 0

                bar = self[f"continue_bar{i}"]
                text_widget = self[f"continue{i}"]

                text_widget.setText(row)
                bar.setValue(percent_value)
                resume_item = None
                if (
                    row
                    and isinstance(getattr(self, "resume_items", None), list)
                    and i - 1 < len(self.resume_items)
                    and isinstance(self.resume_items[i - 1], dict)
                ):
                    resume_item = self.resume_items[i - 1]

                jellydream_set_poster_badge(
                    self,
                    "continue_badge%d" % i,
                    "continue_poster%d" % i,
                    resume_item,
                    corner="top-right"
                )

                if row:
                    if text_widget.instance:
                        text_widget.instance.show()
                    if bar.instance:
                        bar.show()
                else:
                    if text_widget.instance:
                        text_widget.instance.hide()

                    if bar.instance:
                        bar.hide()

                    poster_name = "continue_poster%d" % i

                    poster_widget = (
                        self[poster_name]
                        if poster_name in self
                        else None
                    )
                    jellydream_safe_set_pixmap(
                        poster_widget,
                        "",
                        hide_if_missing=True
                    )
            except Exception:
                pass

        # Kein Hintergrundaufruf in updateContinueRows().
        # updateSelection() plant den einzigen sichtbaren Backdrop-Wechsel.

        self.remaining_cover_tasks = list(cover_tasks) if cover_tasks else []
        if self.remaining_cover_tasks:
            try:
                self.async_cover_timer.stop()
            except Exception:
                pass
            try:
                self.async_cover_timer.start(50, True)
            except Exception:
                pass

    def checkAsyncCovers(self):
        try:
            tasks = getattr(
                self,
                "remaining_cover_tasks",
                None
            )

            if not tasks:
                return

            try:
                self.async_cover_timer.stop()
            except Exception:
                pass

            task = tasks.pop(0)

            if not task or len(task) < 3:
                if tasks:
                    self.async_cover_timer.start(50, True)
                return

            item_id, expected_path, kachel_num = task

            try:
                kachel_num = int(kachel_num)
            except Exception:
                kachel_num = 0

            if not 1 <= kachel_num <= 5:
                if tasks:
                    self.async_cover_timer.start(50, True)
                return

            resume_index = kachel_num - 1

            current_item = {}

            if (
                isinstance(self.resume_items, list)
                and 0 <= resume_index < len(self.resume_items)
                and isinstance(
                    self.resume_items[resume_index],
                    dict
                )
            ):
                current_item = dict(
                    self.resume_items[resume_index]
                )

            item_type = current_item.get("Type") or ""

            if item_type == "Episode":
                target_img_id = str(
                    current_item.get("Id")
                    or item_id
                    or ""
                )

                bg_id = str(
                    current_item.get("SeriesId")
                    or current_item.get("Id")
                    or item_id
                    or ""
                )

                image_type = "Primary"
            else:
                target_img_id = str(
                    item_id
                    or current_item.get("Id")
                    or ""
                )

                bg_id = target_img_id
                image_type = "Backdrop"

            widget_name = (
                "continue_poster%d"
                % kachel_num
            )

            def image_finished(path, loaded_item_id):
                try:
                    if not getattr(
                        self,
                        "instance",
                        None
                    ):
                        return

                    if not (
                        0 <= resume_index
                        < len(self.resume_items)
                    ):
                        return

                    visible_item = self.resume_items[
                        resume_index
                    ]

                    if not isinstance(visible_item, dict):
                        return

                    visible_id = str(
                        visible_item.get("Id")
                        or visible_item.get("id")
                        or ""
                    )

                    expected_visible_id = str(
                        current_item.get("Id")
                        or current_item.get("id")
                        or item_id
                        or ""
                    )

                    if visible_id != expected_visible_id:
                        return

                    final_cover = ""

                    if jellydream_valid_file(path, 500):
                        final_cover = path
                    elif jellydream_valid_file(
                        expected_path,
                        500
                    ):
                        final_cover = expected_path
                    elif jellydream_valid_file(
                        FALLBACK_RESUME_COVER,
                        1
                    ):
                        final_cover = FALLBACK_RESUME_COVER

                    jellydream_safe_set_pixmap(
                        self[widget_name],
                        final_cover,
                        hide_if_missing=True
                    )

                    # Den großen Fullscreen-Backdrop nur laden, wenn
                    # dynamische Hintergründe aktiviert sind.
                    if (
                        bg_id
                        and jellydream_dynamic_backdrops_enabled()
                    ):
                        background_width, background_height = (
                            jellydream_get_background_widget_size(self)
                        )

                        jellydream_async_download_image(
                            bg_id,
                            jellydream_cache_dir("cover"),
                            width=background_width,
                            height=background_height,
                            image_type="Backdrop"
                        )

                finally:
                    if self.remaining_cover_tasks:
                        try:
                            self.async_cover_timer.start(
                                50,
                                True
                            )
                        except Exception:
                            pass

            if target_img_id:
                jellydream_async_download_image(
                    target_img_id,
                    jellydream_cache_dir("cover"),
                    width=310,
                    height=174,
                    image_type=image_type,
                    callback=image_finished
                )
            else:
                image_finished("", "")

        except Exception as e:
            jd_log(
                "Error in checkAsyncCovers: %s"
                % e,
                level="WARNING"
            )

            if self.remaining_cover_tasks:
                try:
                    self.async_cover_timer.start(
                        50,
                        True
                    )
                except Exception:
                    pass

    def updateHomePosterZoom(self):
        """
        Aktualisiert ausschließlich das vorherige und das neu ausgewählte
        Poster.

        Dadurch werden nicht mehr bei jedem Tastendruck elf Widgets
        verschoben, vergrößert und invalidiert.
        """
        try:
            target_widget = ""
            zoom_factor = 1.0

            if (
                self.focusArea == "resume"
                and self.resume_items
                and 0 <= self.currentResume < len(self.resume_items)
                and self.currentResume < 5
            ):
                target_widget = "continue_poster%d" % (
                    self.currentResume + 1
                )

                zoom_factor = 1.055

            elif (
                self.focusArea == "poster"
                and self._homePosterItem(self.currentPoster)
                and 0 <= self.currentPoster < len(self.home_latest)
                and self.currentPoster < 6
            ):
                target_widget = "poster%d" % (
                    self.currentPoster + 1
                )

                zoom_factor = 1.075

            previous_widget = str(
                getattr(
                    self,
                    "_home_zoom_widget",
                    ""
                ) or ""
            )

            if previous_widget == target_widget:
                return

            if previous_widget:
                self.resetWidgetGeometry(
                    previous_widget
                )

            self._home_zoom_widget = target_widget

            if not target_widget:
                return

            self.zoomWidgetFromOriginalGeometry(
                target_widget,
                zoom_factor=zoom_factor,
                anchor_bottom=True
            )

        except Exception as e:
            jd_log(
                "Error in updateHomePosterZoom: %s" % e,
                level="WARNING"
            )

    def _setHomeLabelTextIfChanged(
        self,
        widget_name,
        text
    ):
        """
        Setzt einen Label-Text nur, wenn der tatsächlich sichtbare Text
        abweicht.

        Der native Label-Inhalt wird zusätzlich geprüft, weil andere
        Funktionen den Text teilweise direkt mit `setText()` ändern.
        Dadurch kann der interne JellyDream-Textcache nicht mehr vom
        sichtbaren Widget-Inhalt abweichen.
        """
        try:
            if widget_name not in self:
                return False

            widget = self[widget_name]
            text = str(text or "")

            current_text = None

            # Zuerst den tatsächlich im Label gespeicherten Text lesen.
            try:
                current_text = str(
                    widget.getText() or ""
                )
            except Exception:
                # Kompatibilitätsfallback für ältere Enigma2-Versionen.
                current_text = getattr(
                    widget,
                    "_jellydream_home_text",
                    None
                )

            if current_text == text:
                # Cache trotzdem mit dem echten Widgetinhalt synchronisieren.
                widget._jellydream_home_text = text
                return False

            widget.setText(text)
            widget._jellydream_home_text = text

            return True

        except Exception as e:
            jd_log(
                "Label text could not be updated for '%s': %s"                % (
                    widget_name,
                    e
                ),
                level="DEBUG"
            )

            return False


    def _setHomeLabelColorIfChanged(
        self,
        widget_name,
        color
    ):
        """
        Setzt die Schriftfarbe nur bei einer tatsächlichen Änderung.

        Wiederholte setForegroundColor()-Aufrufe verursachen auf manchen
        OpenPLi-Boxen trotz gleicher Farbe unnötige Repaints.
        """
        try:
            if widget_name not in self:
                return False

            widget = self[widget_name]

            if not getattr(widget, "instance", None):
                return False

            color = str(color or "#ffffff")

            if getattr(
                widget,
                "_jellydream_home_color",
                None
            ) == color:
                return False

            widget.instance.setForegroundColor(
                parseColor(color)
            )

            widget._jellydream_home_color = color

            return True

        except Exception:
            return False


    def _homeLatestSideHasItems(self, side):
        start = 0 if side == "movies" else 3
        end = 3 if side == "movies" else 6

        return any(
            self._homePosterItem(index)
            for index in range(start, min(end, len(self.home_latest)))
        )

    def _firstLatestSide(self):
        if self._homeLatestSideHasItems("movies"):
            return "movies"

        return "series"

    def _stepLatestHeading(self, step):
        sides = [
            side
            for side in ("movies", "series")
            if self._homeLatestSideHasItems(side)
        ]

        if len(sides) < 2:
            return

        current = getattr(self, "latestSide", "movies")

        if current not in sides:
            self.latestSide = sides[0]
            return

        index = sides.index(current)
        self.latestSide = sides[(index + int(step)) % len(sides)]

    def _firstPosterOnSide(self, side):
        start = 0 if side == "movies" else 3
        end = 3 if side == "movies" else 6

        for index in range(start, min(end, len(self.home_latest))):
            if self._homePosterItem(index):
                return index

        return self._firstHomePosterIndex()

    def openLatestOverview(self):
        if not jellyfin_is_logged_in():
            jellydream_safe_set_status(
                self,
                _("Bitte zuerst einloggen"),
                level="WARNING"
            )
            return

        side = getattr(self, "latestSide", "movies")

        if side == "series":
            screen = JellyDreamSeriesListScreen
        else:
            screen = JellyDreamMovieListScreen

        ok, err = jellydream_safe_open(
            self.session,
            self.loadMainData,
            screen,
            "added"
        )

        if not ok:
            jellydream_safe_set_status(
                self,
                err,
                level="ERROR"
            )

    def _homePosterItem(self, index):
        try:
            index = int(index)
        except Exception:
            return None

        if index < 0 or index >= len(self.home_latest):
            return None

        item = self.home_latest[index]

        if isinstance(item, dict):
            return item

        return None

    def _homeLatestHasItems(self):
        return any(
            self._homePosterItem(index)
            for index in range(len(self.home_latest))
        )

    def _firstHomePosterIndex(self):
        for index in range(len(self.home_latest)):
            if self._homePosterItem(index):
                return index

        return 0

    def _stepHomePoster(self, step):
        index = int(self.currentPoster or 0) + int(step)

        while 0 <= index < len(self.home_latest):
            if self._homePosterItem(index):
                self.currentPoster = index
                return

            index += int(step)

    def _getHomePosterTitle(
        self,
        poster_index,
        selected=False
    ):
        """
        Liefert den formatierten Titel eines Neuerscheinungs-Posters.
        """
        try:
            poster_index = int(poster_index)

            if (
                poster_index < 0
                or poster_index >= len(self.home_latest)
            ):
                return ""

            item = self.home_latest[poster_index]

            if isinstance(item, dict):
                title = (
                    item.get("Name")
                    or item.get("name")
                    or ""
                )

            elif isinstance(item, (list, tuple)):
                title = item[0] if item else ""

            else:
                title = ""

            title = str(title or "").strip()

            if not title:
                return ""

            if selected:
                title = "> %s <" % title

            return jellydream_two_line_title(
                title,
                width=23
            )

        except Exception:
            return ""


    def _updateHomeSelectionWidgets(
        self,
        previous_state,
        current_state
    ):
        """
        Aktualisiert die Auswahl-UI der Hauptseite differenziell.

        Bei normaler Navigation werden nur geändert:

        - bisherige und neue Resume-Zeile,
        - bisheriger und neuer Poster-Titel,
        - die Bereichsüberschriften,
        - Menü und Statustext, sofern deren Inhalt abweicht.

        Nur bei einem vollständigen Neuladen der Startseite
        (`previous_state is None`) werden alle Auswahlfarben einmalig
        sauber initialisiert.
        """
        try:
            current_focus = current_state[0]
            current_resume = current_state[1]
            current_poster = current_state[2]
            latest_side = (
                current_state[4]
                if len(current_state) > 4
                else "movies"
            )
            movies_heading = (
                current_focus == "latest"
                and latest_side == "movies"
            )
            series_heading = (
                current_focus == "latest"
                and latest_side == "series"
            )

            force_full_refresh = previous_state is None

            if previous_state:
                previous_focus = previous_state[0]
                previous_resume = previous_state[1]
                previous_poster = previous_state[2]
            else:
                previous_focus = ""
                previous_resume = -1
                previous_poster = -1

            # Bereichsüberschriften nur bei Textänderung setzen.
            section1_text = (
                ">> " + _("Weiterschauen")
                if current_focus == "resume"
                else "   " + _("Weiterschauen")
            )

            section2_text = (
                ">> " + _("Neuerscheinungen Filme")
                if movies_heading
                else "   " + _("Neuerscheinungen Filme")
            )

            section3_text = (
                ">> " + _("Neuerscheinungen Serien")
                if series_heading
                else "   " + _("Neuerscheinungen Serien")
            )

            self._setHomeLabelTextIfChanged(
                "section1",
                section1_text
            )

            self._setHomeLabelTextIfChanged(
                "section2",
                section2_text
            )

            self._setHomeLabelTextIfChanged(
                "section3",
                section3_text
            )
            self._setHomeLabelColorIfChanged(
                "section2",
                "#f1c40f" if movies_heading else "#ffffff"
            )
            self._setHomeLabelColorIfChanged(
                "section3",
                "#f1c40f" if series_heading else "#ffffff"
            )

            # ----------------------------------------------------------
            # Weiterschauen: Nur alten und neuen Eintrag einfärben.
            # ----------------------------------------------------------
            if force_full_refresh:
                resume_slots = range(5)
            else:
                resume_slots = set()

                if previous_focus == "resume":
                    resume_slots.add(previous_resume)

                if current_focus == "resume":
                    resume_slots.add(current_resume)

            for resume_index in resume_slots:
                if resume_index < 0 or resume_index >= 5:
                    continue

                widget_name = "continue%d" % (
                    resume_index + 1
                )

                is_selected = (
                    current_focus == "resume"
                    and resume_index == current_resume
                )

                self._setHomeLabelColorIfChanged(
                    widget_name,
                    "#f1c40f"
                    if is_selected
                    else "#ffffff"
                )

            # ----------------------------------------------------------
            # Neuerscheinungen: Nur alten und neuen Poster-Titel ändern.
            # ----------------------------------------------------------
            if force_full_refresh:
                poster_slots = range(6)
            else:
                poster_slots = set()

                if previous_focus == "poster":
                    poster_slots.add(previous_poster)

                if current_focus == "poster":
                    poster_slots.add(current_poster)

            for poster_index in poster_slots:
                if poster_index < 0 or poster_index >= 6:
                    continue

                title_name = "poster_title%d" % (
                    poster_index + 1
                )

                is_selected = (
                    current_focus == "poster"
                    and poster_index == current_poster
                )

                self._setHomeLabelTextIfChanged(
                    title_name,
                    self._getHomePosterTitle(
                        poster_index,
                        selected=is_selected
                    )
                )

                self._setHomeLabelColorIfChanged(
                    title_name,
                    "#f1c40f"
                    if is_selected
                    else "#ffffff"
                )

        except Exception as e:
            jd_log(
                "Differential home screen selection could not be updated: %s" % e,
                level="DEBUG"
            )
            
    def updateSelection(self):
        """
        Aktualisiert die Auswahl auf der Hauptseite differenziell.

        Wichtig:
        - Kein pauschales Neufärben aller Continue-Zeilen.
        - Kein pauschales Neusetzen aller sechs Poster-Titel.
        - Nur alte und neue Auswahl werden bearbeitet.
        """
        try:
            # ----------------------------------------------------------
            # Nicht angemeldet: Einmalig die Inhaltsbereiche ausblenden.
            # ----------------------------------------------------------
            if not jellyfin_is_logged_in():
                self.focusArea = "menu"
                self.currentPoster = 0
                self.currentResume = 0

                if not self._home_logged_out_ui_applied:
                    self.hideLoggedOutContentAreas()
                    self._home_logged_out_ui_applied = True
                    self._home_selection_state = None

                self.updateMenuDisplay()

                self._setHomeLabelTextIfChanged(
                    "status",
                    _("LINKS/RECHTS = Menü wechseln     OK = Auswählen")
                )

                set_jellydream_background(self)
                return

            # Ab jetzt ist die Login-UI nicht mehr aktiv.
            self._home_logged_out_ui_applied = False

            # ----------------------------------------------------------
            # Ungültige Fokusbereiche bzw. leere Bereiche korrigieren.
            # ----------------------------------------------------------
            if self.focusArea not in (
                "menu",
                "resume",
                "latest",
                "poster"
            ):
                self.focusArea = "menu"

            if (
                self.focusArea == "latest"
                and not self._homeLatestHasItems()
            ):
                self.focusArea = (
                    "resume"
                    if self.resume_items
                    else "menu"
                )
            elif self.focusArea == "latest":
                if not self._homeLatestSideHasItems(
                    getattr(self, "latestSide", "movies")
                ):
                    self.latestSide = self._firstLatestSide()

            if (
                self.focusArea == "resume"
                and not self.resume_items
            ):
                self.focusArea = (
                    "poster"
                    if self._homeLatestHasItems()
                    else "menu"
                )

            if (
                self.focusArea == "poster"
                and not self._homeLatestHasItems()
            ):
                self.focusArea = (
                    "resume"
                    if self.resume_items
                    else "menu"
                )

            # Auswahlwerte absichern.
            if self.resume_items:
                self.currentResume = max(
                    0,
                    min(
                        int(self.currentResume or 0),
                        len(self.resume_items) - 1
                    )
                )
            else:
                self.currentResume = 0

            if self._homeLatestHasItems():
                self.currentPoster = max(
                    0,
                    min(
                        int(self.currentPoster or 0),
                        len(self.home_latest) - 1
                    )
                )

                if not self._homePosterItem(self.currentPoster):
                    self.currentPoster = self._firstHomePosterIndex()
            else:
                self.currentPoster = 0

            if self.main_menu_items:
                self.menuIndex = max(
                    0,
                    min(
                        int(self.menuIndex or 0),
                        len(self.main_menu_items) - 1
                    )
                )
            else:
                self.menuIndex = 0

            previous_state = getattr(
                self,
                "_home_selection_state",
                None
            )

            current_state = (
                self.focusArea,
                self.currentResume,
                self.currentPoster,
                self.menuIndex,
                getattr(self, "latestSide", "movies")
            )

            # Das Menü nutzt jetzt ebenfalls einen Textvergleich und führt
            # bei unverändertem Zustand kein setText() mehr aus.
            self.updateMenuDisplay()

            # Nur bei einer echten Auswahländerung die Auswahl-Widgets
            # bearbeiten. Beim ersten Rendern (`None`) werden alle Farben
            # einmalig sauber initialisiert.
            if previous_state != current_state:
                self._updateHomeSelectionWidgets(
                    previous_state,
                    current_state
                )

                self._home_selection_state = current_state

                # Zoom setzt schon bisher nur altes/neues Poster zurück bzw.
                # vergrößert es.
                self.updateHomePosterZoom()

            # Statustext nur setzen, falls er sich wirklich ändert.
            if self.focusArea == "menu":
                status_text = _(
                    "LINKS/RECHTS = Menü wechseln     "
                    "BLAU = Bibliothek aktualisieren\n"
                    "RUNTER = Weiterschauen / Neuerscheinungen     "
                    "OK = Auswählen"
                )

            elif self.focusArea == "resume":
                status_text = _(
                    "LINKS/RECHTS = Weiterschauen durchblättern\n"
                    "HOCH = Zum Hauptmenü | RUNTER = Neuerscheinungen   "
                    "OK = Fortsetzen   BLAU = Bibliothek aktualisieren"
                )

            elif self.focusArea == "latest":
                status_text = _(
                    "LINKS/RECHTS = Filme oder Serien\n"
                    "HOCH = Zurück | RUNTER = Poster | "
                    "OK = Zuletzt hinzugefügt"
                )

            elif self.focusArea == "poster" and self.currentPoster >= 3:
                status_text = _(
                    "LINKS/RECHTS = Serien durchblättern\n"
                    "HOCH = Neuerscheinungen Serien | "
                    "OK = Staffeln   BLAU = Bibliothek aktualisieren"
                )

            else:
                status_text = _(
                    "LINKS/RECHTS = Filme durchblättern\n"
                    "HOCH = Neuerscheinungen Filme | "
                    "OK = Film-Infos   BLAU = Bibliothek aktualisieren"
                )

            if self.focusArea != "menu":
                status_text = jellydream_with_yellow_status(status_text)

            self._setHomeLabelTextIfChanged(
                "status",
                status_text
            )

            # Der Backdrop muss nur bei Änderung des Fokusbereichs oder
            # des tatsächlich gewählten Inhalts neu geplant werden.
            previous_content_state = (
                previous_state[:3]
                if previous_state
                else None
            )

            current_content_state = current_state[:3]

            if previous_content_state != current_content_state:
                self.scheduleHomeBackgroundUpdate()

        except Exception as e:
            jd_log(
                "Critical exception in updateSelection: %s" % e,
                level="ERROR"
            )
            
    def updateMenuDisplay(self):
        """
        Zeichnet jeden Menüpunkt als eigenes Symbol plus Text,
        damit die Icons direkt vor dem Wort stehen.
        """
        try:
            entries = []

            for index, item in enumerate(self.main_menu_items):
                label = _(self.MENU_LABELS.get(item, item))
                focused = (
                    index == self.menuIndex
                    and self.focusArea == "menu"
                )
                selected = index == self.menuIndex
                body = "[ %s ]" % label if selected else label
                entries.append(
                    (item, body, item in self.MENU_ICONS, focused)
                )

            menu_instance = getattr(self["menu"], "instance", None)

            if menu_instance is None:
                self["menu"].setText(
                    "   ".join(
                        (
                            "\\c00f1c40f%s\\c00ffffff" % body
                        ) if focused else body
                        for _item, body, _has_icon, focused in entries
                    )
                )
                return

            key = tuple(
                (item, body, focused)
                for item, body, _has_icon, focused in entries
            )

            if key == getattr(self, "_home_menu_icon_key", None):
                return

            if not self._layout_menu_entries(entries):
                return

            self["menu"].setText("")
            self._home_menu_icon_key = key

        except Exception as e:
            jd_log(
                "Menu display could not be updated differentially: %s" % e,
                level="DEBUG"
            )

    def _menu_text_width(self, text):
        gauge = self["menu_gauge"] if "menu_gauge" in self else None

        if gauge is None:
            return 0

        try:
            gauge.setText(text or "")
        except Exception:
            return 0

        instance = getattr(gauge, "instance", None)

        if instance is None:
            return 0

        try:
            return max(0, int(instance.calculateSize().width()))
        except Exception:
            return 0

    def _menu_icon_slot(self, index):
        slots = getattr(self, "_menu_icon_slots", None)

        if slots is None:
            slots = []
            self._menu_icon_slots = slots

        while len(slots) <= index:
            from enigma import ePixmap

            pixmap = ePixmap(self.instance)

            try:
                pixmap.setAlphatest(2)
            except Exception:
                pass

            try:
                pixmap.setScale(1)
            except Exception:
                pass

            try:
                pixmap.setZPosition(5)
            except Exception:
                pass

            slots.append(pixmap)

        return slots[index]

    def _menu_label_slot(self, index):
        slots = getattr(self, "_menu_label_slots", None)

        if slots is None:
            slots = []
            self._menu_label_slots = slots

        while len(slots) <= index:
            from enigma import eLabel

            label = eLabel(self.instance)

            try:
                label.setTransparent(1)
            except Exception:
                pass

            try:
                label.setVAlign(1)
            except Exception:
                pass

            try:
                label.setZPosition(5)
            except Exception:
                pass

            slots.append(label)

        return slots[index]

    def _layout_menu_entries(self, entries):
        menu = getattr(self["menu"], "instance", None)

        if menu is None or getattr(self, "instance", None) is None:
            return False

        from enigma import ePoint, eSize
        from skin import parseColor

        row_height = int(menu.size().height())
        icon_size = max(16, int(row_height * 0.62))
        icon_gap = max(6, icon_size // 4)
        item_gap = max(16, int(row_height * 0.35))
        measured = []

        for _item, body, has_icon, _focused in entries:
            text_width = self._menu_text_width(body)

            if text_width < 4:
                return False

            piece = text_width

            if has_icon:
                piece += icon_size + icon_gap

            measured.append((text_width, piece))

        total = sum(piece for _text_width, piece in measured)
        total += item_gap * max(0, len(entries) - 1)
        origin = menu.position()
        cursor_x = origin.x() + max(
            0,
            (int(menu.size().width()) - total) // 2
        )
        row_y = origin.y()
        icon_y = row_y + max(0, (row_height - icon_size) // 2)
        font = None

        try:
            font = menu.getFont()
        except Exception:
            font = None

        used_icons = 0
        used_labels = 0

        for index, (item, body, has_icon, focused) in enumerate(entries):
            text_width, _piece = measured[index]

            if has_icon:
                slot = self._menu_icon_slot(used_icons)
                used_icons += 1
                pixmap = jellydream_menu_icon(
                    item,
                    "gold" if focused else "white"
                )

                if pixmap is not None:
                    slot.setPixmap(pixmap)
                    slot.resize(eSize(icon_size, icon_size))
                    slot.move(ePoint(int(cursor_x), int(icon_y)))
                    slot.show()
                else:
                    slot.hide()

                cursor_x += icon_size + icon_gap

            label = self._menu_label_slot(used_labels)
            used_labels += 1

            if font is not None:
                try:
                    label.setFont(font)
                except Exception:
                    pass

            try:
                label.setForegroundColor(
                    parseColor("#f1c40f" if focused else "#d8dde5")
                )
            except Exception:
                pass

            label.setText(body)
            label.resize(eSize(int(text_width + 6), row_height))
            label.move(ePoint(int(cursor_x), int(row_y)))
            label.show()
            cursor_x += text_width

            if index < len(entries) - 1:
                cursor_x += item_gap

        for slot in (getattr(self, "_menu_icon_slots", None) or [])[used_icons:]:
            slot.hide()

        for slot in (getattr(self, "_menu_label_slots", None) or [])[used_labels:]:
            slot.hide()

        if "menu_gauge" in self:
            self["menu_gauge"].setText("")

        return True

    def getGermanDate(self):
        days = [
            _("Montag"), _("Dienstag"), _("Mittwoch"), _("Donnerstag"),
            _("Freitag"), _("Samstag"), _("Sonntag")
        ]
        months = [
            _("Januar"), _("Februar"), _("März"), _("April"),
            _("Mai"), _("Juni"), _("Juli"), _("August"),
            _("September"), _("Oktober"), _("November"), _("Dezember")
        ]
        day_name = days[int(strftime("%u")) - 1]
        day = strftime("%d")
        month = months[int(strftime("%m")) - 1]
        year = strftime("%Y")

        return f"{day_name}\n{day}. {month} {year}"
