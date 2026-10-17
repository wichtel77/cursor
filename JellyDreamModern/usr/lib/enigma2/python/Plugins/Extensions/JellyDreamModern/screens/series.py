# -*- coding: utf-8 -*-

from Components.ActionMap import ActionMap
from Components.Label import Label
from Components.Pixmap import Pixmap

from .. import _

from ..constants import ITEMS_PER_PAGE_GRID
from ..log import jd_log
from ..config import (
    jellydream_library_played_filter,
    jellydream_library_sort,
    jellydream_update_refresh_time
)
from ..images import jellydream_apply_item_backdrop, jellydream_apply_item_logo
from ..uiutil import (
    jellydream_apply_letter_filter,
    jellydream_confirm_alphabet,
    jellydream_finish_letter_choice,
    jellydream_focus_alphabet,
    jellydream_handle_navigation,
    jellydream_move_alphabet,
    jellydream_reset_letter_filter,
    jellydream_restart_single_timer,
    jellydream_setup_letter_bar,
    jellydream_with_letter_label,
    jellydream_safe_current_item,
    jellydream_safe_open,
    jellydream_safe_set_status,
    jellydream_set_imdb_badge,
    jellydream_open_library_sort,
    jellydream_view_filter_label,
    jellydream_with_sort_status,
    jellydream_wrap
)
from ..library import (
    jellydream_build_genres,
    jellydream_filter_by_genre,
    jellydream_filter_by_played,
    jellydream_genrebar_text,
    jellydream_item_genre_line,
    jellydream_item_overview,
    jellydream_merge_item_details,
    jellydream_series_library_is_stale,
    jellyfin_get_series,
    _GLOBAL_DETAILS_CACHE
)
from ..ui import (
    JellyDreamGridScreen,
    jellydream_apply_title_icon,
    jellydream_render_grid,
    jellydream_reset_screen_background
)
from .seasons import JellyDreamSeasonListScreen


class JellyDreamSeriesListScreen(JellyDreamGridScreen):
    SCREEN_NAME = "JellyDreamSeriesListScreen"
    GRID_COUNT = 6

    def __init__(self, session, sort_override=None):
        JellyDreamGridScreen.__init__(self, session)
        self.detail_cache = _GLOBAL_DETAILS_CACHE
        self._sort_override = (
            sort_override
            if sort_override in ("name", "added")
            else None
        )

        self.all_series = []
        self.genres = ["Alle"]
        self.genreIndex = 0
        self.mode = "grid"
        self.series = []

        self["title"] = Label(_("Serien"))
        self["title_icon"] = Pixmap()
        self["viewfilter"] = Label("")
        self["genrebar"] = Label("")
        self["seriesinfo"] = Label("")
        self["imdb_logo"] = Pixmap()
        self["imdb_rating"] = Label("")
        jellydream_setup_letter_bar(self)

        self["actions"] = ActionMap(
            ["OkCancelActions", "DirectionActions", "ColorActions"],
            {
                "cancel": self.close,
                "ok": self.okPressed,
                "green": self.greenPressed,
                "yellow": self.yellowPressed,
                "left": lambda: self.navigateGrid("left"),
                "right": lambda: self.navigateGrid("right"),
                "up": lambda: self.navigateGrid("up"),
                "down": lambda: self.navigateGrid("down"),
            },
            -1
        )

        self.onLayoutFinish.append(
            self.loadSeriesAsync
        )
        self.onLayoutFinish.append(
            self.applyTitleIcon
        )

    def sortMode(self):
        mode = getattr(self, "_sort_override", None)

        if mode in ("name", "added"):
            return mode

        return jellydream_library_sort()

    def viewFilterText(self):
        return jellydream_with_letter_label(
            self,
            jellydream_view_filter_label(self.sortMode())
        )

    def resetLetterFilter(self):
        jellydream_reset_letter_filter(
            self,
            self.filterSeriesBySelectedGenre
        )

    def reloadFromSavedSort(self):
        self._sort_override = None
        self.loadSeriesAsync()

    def applyTitleIcon(self):
        jellydream_apply_title_icon(self)

    def loadSeriesAsync(self):
        if self._async_loading:
            return

        self.series = []
        self.index = 0

        self["title"].setText(_("Serien"))
        self["viewfilter"].setText(self.viewFilterText())
        self["genrebar"].setText("")
        self["seriesinfo"].setText("")
        jellydream_set_imdb_badge(self, None)
        self["status"].setText(
            _("Serienliste wird von Jellyfin geladen ...")
        )

        jellydream_render_grid(
            self,
            [],
            0
        )

        self.runAsyncLoad(
            loader=lambda: jellyfin_get_series(
                2000,
                raise_on_error=True,
                sort_mode=self.sortMode()
            ),
            finished_callback=self.seriesLoaded,
            message=_("Serienliste wird geladen ...")
        )

    def seriesLoaded(self, series, error_message=""):
        try:
            if error_message:
                self.all_series = []
                self.series = []
                self.genres = ["Alle"]

                self["status"].setText(
                    _("Serienliste konnte nicht geladen werden: %s")
                    % error_message
                )

                jellydream_render_grid(
                    self,
                    [],
                    0
                )
                return

            self.all_series = (
                list(series)
                if isinstance(series, list)
                else []
            )

            self.genres = jellydream_build_genres(
                self.all_series
            )

            keep_genre = getattr(self, "_keep_genre_index", None)
            keep_id = str(getattr(self, "_keep_item_id", "") or "")
            self._keep_genre_index = None
            self._keep_item_id = ""

            if isinstance(keep_genre, int) and self.genres:
                self.genreIndex = min(
                    keep_genre,
                    len(self.genres) - 1
                )
            else:
                self.genreIndex = 0

            self.filterSeriesBySelectedGenre()

            if keep_id:
                for index, show in enumerate(self.series):
                    if (
                        isinstance(show, dict)
                        and str(show.get("Id") or show.get("id") or "") == keep_id
                    ):
                        self.index = index
                        self.updateList()
                        break

        except Exception as e:
            self["status"].setText(
                _("Serienliste konnte nicht angezeigt werden.")
            )

            jd_log(
                "Loaded series list could not be processed: %s" % e,
                level="WARNING"
            )
        
    def currentGenre(self):
        try: return self.genres[self.genreIndex]
        except Exception: return "Alle"

    def filterSeriesBySelectedGenre(self):
        series = jellydream_filter_by_genre(
            self.all_series,
            self.currentGenre()
        )
        series = jellydream_filter_by_played(
            series,
            jellydream_library_played_filter()
        )
        self.series = jellydream_apply_letter_filter(
            series,
            self._letter_filter
        )
        self.index = 0
        self.updateList()

    def navigateGrid(self, direction):
        if self._async_loading:
            return

        if jellydream_move_alphabet(self, direction):
            return

        if direction == "up" and self.index < 3 and self.mode == "grid":
            self.mode = "genre"
            if self.genres:
                try:
                    self.genreIndex = (self.genreIndex - 1) % len(self.genres)
                    self.filterSeriesBySelectedGenre()
                except Exception as e:
                    jd_log(
                        "Error while changing the series genre: %s" % e,
                        level="WARNING"
                    )
            return

        if self.mode == "genre" and direction in ("left", "right"):
            if self.genres:
                step = 1 if direction == "right" else -1
                self.genreIndex = (self.genreIndex + step) % len(self.genres)
                self.filterSeriesBySelectedGenre()
            return

        if self.mode == "genre" and direction == "down":
            self.mode = "grid"
            self.index = 0
            self.updateList()
            return

        if direction == "down" and self.mode == "grid":
            jellydream_focus_alphabet(self)
            return

        try:
            self.mode = "grid"
            self.index = jellydream_handle_navigation(self, direction, self.series, self.index, items_per_page=ITEMS_PER_PAGE_GRID)
        except Exception:
            pass

    def updateInfo(self):
        try:
            if not self.series:
                jellydream_reset_screen_background(self, "seriesinfo")
                jellydream_set_imdb_badge(self, None)
                jellydream_apply_item_logo(self, None)
                return

            show = self.series[self.index]
            item_id = show.get("Id", "")

            details = self.requestSelectedItemDetails(
                self.series,
                item_id,
                refresh_callback=self.updateInfo
            )

            if details:
                show = jellydream_merge_item_details(
                    show,
                    details
                )

            title = show.get("Name", _("Ohne Titel"))
            year = show.get("ProductionYear") or "-"
            genres = jellydream_item_genre_line(show)
            rating = str(show.get("OfficialRating") or "").strip()
            fsk_text = "   %s" % rating if rating else ""
            imdb_text = jellydream_set_imdb_badge(self, show)

            if imdb_text:
                fsk_text += "   %s" % imdb_text

            info_text = (
                _("Serie: %s%s\nJahr: %s\nGenre: %s")
                % (title, fsk_text, year, genres)
            )

            overview = jellydream_item_overview(show)
            if overview:
                wrapped_overview = jellydream_wrap(overview, 110, 4)
                info_text = f"{info_text}\n\n{wrapped_overview}"

            self["seriesinfo"].setText(info_text.strip())

            current_id = str(item_id or "")

            jellydream_apply_item_backdrop(
                self,
                target_id=current_id,
                selection_check=lambda: (
                    bool(self.series)
                    and 0 <= int(self.index) < len(self.series)
                    and str(self.series[self.index].get("Id", "")) == current_id
                )
            )
            jellydream_apply_item_logo(
                self,
                show,
                selection_check=lambda: (
                    bool(self.series)
                    and 0 <= int(self.index) < len(self.series)
                    and str(self.series[self.index].get("Id", "")) == current_id
                )
            )

        except Exception:
            pass

    def updateList(self, *args):
        try:
            if getattr(self, "instance", None):
                self.instance.show()
        except Exception:
            pass

        self["title"].setText(_("Serien"))
        self["viewfilter"].setText(self.viewFilterText())
        self["genrebar"].setText(jellydream_genrebar_text(self.genres, self.genreIndex))

        jellydream_render_grid(self, self.series, self.index)
        letter_empty = jellydream_finish_letter_choice(
            self,
            bool(self.series)
        )

        if letter_empty:
            self["seriesinfo"].setText("")
            jellydream_set_imdb_badge(self, None)
        elif self.series:
            self["status"].setText(jellydream_with_sort_status(
                _("Genre wechseln: In der obersten Reihe HOCH drücken | Serie %s / %s | OK = Staffeln anzeigen | RUNTER = Buchstabe") % (self.index + 1, len(self.series))
            ))
        else:
            self["status"].setText(jellydream_with_sort_status(_("Genre wechseln: In der obersten Reihe HOCH drücken | Keine Serien in dieser Kategorie | RUNTER = Buchstabe")))
            
        # Kein sofortiger Detail-/Backdrop-Refresh bei jeder Taste.
        jellydream_restart_single_timer(
            self,
            "_updateinfo_timer",
            self.updateInfo,
            jellydream_update_refresh_time()
        )

    def greenPressed(self):
        if self._async_loading:
            return

        jellydream_open_library_sort(
            self,
            self.reloadFromSavedSort,
            self.filterSeriesBySelectedGenre
        )

    def seasonsClosed(self, *args):
        if not jellydream_series_library_is_stale():
            self.updateList()
            return

        current = jellydream_safe_current_item(
            self.series,
            self.index
        )
        self._keep_item_id = (
            str(current.get("Id") or current.get("id") or "")
            if isinstance(current, dict)
            else ""
        )
        self._keep_genre_index = self.genreIndex
        self.loadSeriesAsync()

    def yellowPressed(self):
        self.openItemStateMenu(
            jellydream_safe_current_item(self.series, self.index),
            self.loadSeriesAsync
        )

    def okPressed(self):
        if self._async_loading:
            self["status"].setText(_("Bitte warten, die Serienliste wird noch geladen ..."))
            return

        if jellydream_confirm_alphabet(
            self,
            self.filterSeriesBySelectedGenre
        ):
            return
            
        show = jellydream_safe_current_item(self.series, self.index)
        if not show:
            jellydream_safe_set_status(self, _("Keine gültige Serie ausgewählt"), level="WARNING")
            return

        item_id = str(show.get("Id", "")).strip()
        title = show.get("Name", "Jellyfin Serie")

        if not item_id:
            jellydream_safe_set_status(self, _("Fehler: Serien-ID fehlt"), level="WARNING")
            return

        ok, err = jellydream_safe_open(
            self.session,
            self.seasonsClosed,
            JellyDreamSeasonListScreen,
            item_id,
            title
        )
        if not ok:
            jellydream_safe_set_status(self, err, level="ERROR")
