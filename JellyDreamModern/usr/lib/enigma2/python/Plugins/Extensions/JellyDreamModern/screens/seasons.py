# -*- coding: utf-8 -*-

from Components.ActionMap import ActionMap
from Components.Label import Label
from Components.Pixmap import Pixmap

from .. import _

from ..constants import ITEMS_PER_PAGE_GRID
from ..log import jd_log
from ..config import jellydream_update_refresh_time
from ..images import (
    jellydream_apply_item_backdrop,
    jellydream_apply_item_logo,
    jellydream_cache_get,
    jellydream_cache_set,
    set_jellydream_background
)
from ..uiutil import (
    jellydream_handle_navigation,
    jellydream_int_or_default,
    jellydream_restart_single_timer,
    jellydream_safe_current_item,
    jellydream_safe_open,
    jellydream_safe_set_status,
    jellydream_season_progress_text,
    jellydream_with_yellow_status,
    jellydream_set_imdb_badge,
    jellydream_wrap,
    ticks_to_minutes,
    ticks_to_seconds
)
from ..library import (
    jellydream_is_next_up_item,
    jellydream_item_overview,
    jellydream_merge_item_details,
    jellydream_season_display_name,
    jellyfin_get_item_details,
    jellyfin_get_series_next_up,
    jellydream_consume_season_list_stale,
    jellyfin_get_series_seasons,
    _GLOBAL_DETAILS_CACHE
)
from ..ui import (
    JellyDreamGridScreen,
    jellydream_render_grid,
    jellydream_reset_screen_background
)
from .episodes import JellyDreamEpisodeListScreen

            
class JellyDreamSeasonListScreen(JellyDreamGridScreen):
    SCREEN_NAME = "JellyDreamSeasonListScreen"
    GRID_COUNT = 6

    def __init__(self, session, series_id="", series_title="Serie"):
        JellyDreamGridScreen.__init__(self, session)

        self.series_id = series_id
        self.series_title = series_title
        self.seasons = []
        self.series_details = {}

        self["title"] = Label(self.series_title)
        self["genrebar"] = Label(_("Staffelauswahl"))
        self["seriesinfo"] = Label("")
        self["imdb_logo"] = Pixmap()
        self["imdb_rating"] = Label("")

        self["actions"] = ActionMap(
            ["OkCancelActions", "DirectionActions", "ColorActions"],
            {
                "cancel": self.close,
                "ok": self.okPressed,
                "yellow": self.yellowPressed,
                "left": lambda: self.navigateGrid("left"),
                "right": lambda: self.navigateGrid("right"),
                "up": lambda: self.navigateGrid("up"),
                "down": lambda: self.navigateGrid("down"),
            },
            -1
        )

        self.onLayoutFinish.append(
            self.loadSeasonsAsync
        )

    def loadSeasonsAsync(self):
        if self._async_loading:
            return

        current = jellydream_safe_current_item(
            self.seasons,
            self.index
        )
        self._keep_item_id = (
            str(current.get("Id") or current.get("id") or "")
            if isinstance(current, dict)
            else ""
        )

        self.seasons = []
        self.series_details = {}
        self.index = 0

        self["seriesinfo"].setText("")
        jellydream_set_imdb_badge(self, None)
        self["status"].setText(_("Staffeln werden geladen ..."))

        jellydream_render_grid(
            self,
            [],
            0
        )

        def loader():
            seasons = jellyfin_get_series_seasons(
                self.series_id,
                raise_on_error=True
            )

            if not isinstance(seasons, list):
                seasons = []

            series_details = jellydream_cache_get(
                _GLOBAL_DETAILS_CACHE,
                self.series_id,
                {}
            ) or {}

            if not series_details:
                series_details = jellyfin_get_item_details(
                    self.series_id
                ) or {}

                if series_details:
                    jellydream_cache_set(
                        _GLOBAL_DETAILS_CACHE,
                        self.series_id,
                        series_details
                    )

            if not isinstance(series_details, dict):
                series_details = {}

            next_up_item = jellyfin_get_series_next_up(
                self.series_id
            )

            if next_up_item:
                seasons = [next_up_item] + list(seasons)

            return {
                "seasons": seasons,
                "series_details": series_details
            }

        self.runAsyncLoad(
            loader=loader,
            finished_callback=self.seasonsLoaded,
            message=_("Staffeln werden geladen ...")
        )

    def seasonsLoaded(self, result, error_message=""):
        if error_message:
            self.seasons = []
            self.series_details = {}

            self["status"].setText(
                _("Staffeln konnten nicht geladen werden: %s")
                % error_message
            )

            jellydream_render_grid(
                self,
                [],
                0
            )
            return

        if not isinstance(result, dict):
            result = {}

        seasons = result.get("seasons")
        series_details = result.get("series_details")

        self.seasons = (
            list(seasons)
            if isinstance(seasons, list)
            else []
        )

        self.series_details = (
            dict(series_details)
            if isinstance(series_details, dict)
            else {}
        )

        keep_id = str(getattr(self, "_keep_item_id", "") or "")
        self._keep_item_id = ""
        self.index = 0

        if keep_id:
            for index, season in enumerate(self.seasons):
                if (
                    isinstance(season, dict)
                    and str(season.get("Id") or season.get("id") or "") == keep_id
                ):
                    self.index = index
                    break

        self.updateGrid()
    
    def navigateGrid(self, direction):
        try:
            self.index = jellydream_handle_navigation(self, direction, self.seasons, self.index, items_per_page=ITEMS_PER_PAGE_GRID)
        except Exception:
            pass

    def updateInfo(self):
        try:
            if not self.seasons:
                jellydream_reset_screen_background(self, "seriesinfo")
                jellydream_apply_item_logo(self, None, text_widget_name="title")
                return

            season_item = jellydream_safe_current_item(
                self.seasons,
                self.index
            )

            if not isinstance(season_item, dict):
                jellydream_reset_screen_background(self, "seriesinfo")
                return

            logo_details = (
                self.series_details
                if isinstance(self.series_details, dict) and self.series_details
                else None
            )
            jellydream_apply_item_logo(
                self,
                logo_details,
                text_widget_name="title",
                hide_text_when_shown=True,
                fallback_id="" if logo_details else self.series_id
            )

            season_id = str(season_item.get("Id") or "").strip()
            season_index = jellydream_int_or_default(
                season_item.get("IndexNumber"),
                self.index + 1
            )

            details = self.requestSelectedItemDetails(
                self.seasons,
                season_id,
                refresh_callback=self.updateInfo
            )

            if details:
                keep_next_up = jellydream_is_next_up_item(
                    season_item
                )
                grid_title = season_item.get(
                    "_jellydream_grid_title"
                )
                season_item = jellydream_merge_item_details(
                    season_item,
                    details
                )

                if keep_next_up:
                    season_item["_jellydream_next_up"] = True

                if grid_title:
                    season_item["_jellydream_grid_title"] = grid_title

            if jellydream_is_next_up_item(season_item):
                episode_number = jellydream_int_or_default(
                    season_item.get("IndexNumber"),
                    0
                )
                parent_index = jellydream_int_or_default(
                    season_item.get("ParentIndexNumber"),
                    season_index
                )
                episode_title = str(
                    season_item.get("Name") or _("Ohne Titel")
                ).strip()
                minutes = ticks_to_minutes(
                    season_item.get("RunTimeTicks")
                )
                runtime_text = (
                    (_("%s Min") % minutes)
                    if minutes
                    else ""
                )
                overview_text = jellydream_item_overview(
                    season_item
                )
                info_text = (
                    _("Als Nächstes: S%02dE%02d - %s")
                    % (
                        parent_index,
                        episode_number,
                        episode_title
                    )
                )

                if runtime_text:
                    info_text += "\n%s" % (
                        _("Laufzeit: %s") % runtime_text
                    )

                if overview_text:
                    info_text += "\n\n%s" % jellydream_wrap(
                        overview_text,
                        110,
                        5
                    )

                jellydream_set_imdb_badge(self, None)
                self["seriesinfo"].setText(info_text.strip())

                current_target_id = str(
                    season_item.get("SeriesId")
                    or self.series_id
                    or season_id
                    or ""
                ).strip()

                jellydream_apply_item_backdrop(
                    self,
                    target_id=current_target_id,
                    selection_check=lambda: (
                        bool(self.seasons)
                        and 0 <= int(self.index) < len(self.seasons)
                        and isinstance(self.seasons[self.index], dict)
                        and str(
                            self.seasons[self.index].get("Id") or ""
                        ).strip() == season_id
                    )
                )
                return

            title = jellydream_season_display_name(
                season_item,
                season_index
            )

            year = season_item.get("ProductionYear") or "-"
            rating = str(
                season_item.get("OfficialRating") or ""
            ).strip()

            overview_text = jellydream_item_overview(season_item)

            fsk_text = "   %s" % rating if rating else ""
            imdb_text = jellydream_set_imdb_badge(
                self,
                season_item,
                self.series_details
            )

            if imdb_text:
                fsk_text += "   %s" % imdb_text

            info_text = (
                _("Staffel-Details: %s%s\nJahr: %s")
                % (
                    title,
                    fsk_text,
                    year
                )
            )

            progress_text = jellydream_season_progress_text(
                season_item
            )

            if progress_text:
                info_text += "\n%s" % progress_text

            if overview_text:
                info_text += "\n\n%s" % jellydream_wrap(
                    overview_text,
                    110,
                    5
                )
            else:
                info_text += (
                    "\n\n"
                    + _(
                        "Keine Beschreibung für diese Staffel hinterlegt."
                    )
                )

            self["seriesinfo"].setText(info_text.strip())

            current_target_id = str(
                season_item.get("SeriesId")
                or self.series_id
                or season_id
                or ""
            ).strip()

            jellydream_apply_item_backdrop(
                self,
                target_id=current_target_id,
                selection_check=lambda: (
                    bool(self.seasons)
                    and 0 <= int(self.index) < len(self.seasons)
                    and isinstance(self.seasons[self.index], dict)
                    and str(
                        self.seasons[self.index].get("Id") or ""
                    ).strip() == season_id
                )
            )

        except Exception as e:
            jd_log(
                "Season info could not be shown: %s"
                % e,
                level="WARNING"
            )

    def updateList(self, *args):
        self.updateGrid()

    def updateGrid(self, *args):
        try:
            if getattr(self, "instance", None):
                self.instance.show()
        except Exception:
            pass

        if not self.seasons:
            self["status"].setText(_("Keine Staffeln gefunden."))
            self["seriesinfo"].setText("")
            jellydream_set_imdb_badge(self, None)
            jellydream_render_grid(self, [], 0)
            set_jellydream_background(self)
            try:
                if hasattr(self, "_updateinfo_timer") and self._updateinfo_timer:
                    self._updateinfo_timer.stop()
            except Exception:
                pass
            return

        jellydream_render_grid(self, self.seasons, self.index)

        selected_item = jellydream_safe_current_item(
            self.seasons,
            self.index
        )

        if jellydream_is_next_up_item(selected_item):
            self["status"].setText(jellydream_with_yellow_status(
                _("OK = Abspielen   Steuerkreuz = Navigieren   EXIT = Zurück")
            ))
        else:
            self["status"].setText(jellydream_with_yellow_status(
                _(
                    "Staffel %s / %s | Steuerkreuz = Navigieren / blättern | "
                    "OK = Folgen anzeigen | EXIT = Zurück"
                )
                % (
                    self.index + 1,
                    len(self.seasons)
                )
            ))

        # Kein doppelter Refresh: nur verzögert aktualisieren.
        jellydream_restart_single_timer(
            self,
            "_updateinfo_timer",
            self.updateInfo,
            jellydream_update_refresh_time()
        )

    def yellowPressed(self):
        self.openItemStateMenu(
            jellydream_safe_current_item(self.seasons, self.index),
            self.loadSeasonsAsync
        )

    def episodesClosed(self, *args):
        if not jellydream_consume_season_list_stale(self.series_id):
            self.updateGrid()
            return

        self.loadSeasonsAsync()

    def okPressed(self):
        selected_season = jellydream_safe_current_item(self.seasons, self.index)
        if selected_season is None:
            jellydream_safe_set_status(self, _("Keine gültige Staffel ausgewählt"), level="WARNING")
            return

        if jellydream_is_next_up_item(selected_season):
            item_id = str(
                selected_season.get("Id") or ""
            ).strip()

            if not item_id:
                jellydream_safe_set_status(
                    self,
                    _("Fehler: Episoden-ID fehlt"),
                    level="WARNING"
                )
                return

            season_number = jellydream_int_or_default(
                selected_season.get("ParentIndexNumber"),
                0
            )
            episode_number = jellydream_int_or_default(
                selected_season.get("IndexNumber"),
                1
            )
            episode_title = str(
                selected_season.get("Name") or _("Folge")
            ).strip()
            title = "S%02dE%02d - %s" % (
                season_number,
                episode_number,
                episode_title
            )
            runtime = ticks_to_seconds(
                selected_season.get("RunTimeTicks")
            )
            user_data = selected_season.get("UserData") or {}
            start_position = 0

            if isinstance(user_data, dict):
                start_position = ticks_to_seconds(
                    user_data.get("PlaybackPositionTicks")
                )

            self.playItemAsync(
                title=title,
                item_id=item_id,
                runtime_seconds=runtime,
                return_screen=self,
                callback=self.loadSeasonsAsync,
                start_position_seconds=start_position
            )
            return

        if isinstance(selected_season, dict):
            season_number = selected_season.get("IndexNumber")
            if season_number is None:
                season_number = selected_season.get("index", self.index + 1)
        else:
            season_number = selected_season

        try:
            season_number = int(season_number)
        except Exception:
            jellydream_safe_set_status(self, _("Staffelnummer ist ungültig"), level="WARNING")
            return

        ok, err = jellydream_safe_open(
            self.session,
            self.episodesClosed,
            JellyDreamEpisodeListScreen,
            self.series_id,
            self.series_title,
            season_number
        )
        if not ok:
            jellydream_safe_set_status(self, err, level="ERROR")
