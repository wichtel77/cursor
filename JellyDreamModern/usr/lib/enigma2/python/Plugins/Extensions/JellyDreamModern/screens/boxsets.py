# -*- coding: utf-8 -*-

from Components.ActionMap import ActionMap
from Components.Label import Label
from Components.Pixmap import Pixmap

from .. import _

from ..constants import ITEMS_PER_PAGE_GRID
from ..log import jd_log
from ..config import jellydream_library_played_filter, jellydream_update_refresh_time
from ..images import (
    jellydream_apply_item_backdrop,
    jellydream_apply_item_logo,
    jellydream_apply_resolved_background,
    set_jellydream_background
)
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
    jellydream_stop_timer,
    jellydream_open_library_sort,
    jellydream_view_filter_label,
    jellydream_with_sort_status,
    jellydream_with_yellow_status,
    jellydream_wrap,
    ticks_to_minutes,
    ticks_to_seconds
)
from ..library import (
    jellydream_build_genres,
    jellydream_filter_by_genre,
    jellydream_filter_by_played,
    jellydream_genrebar_text,
    jellydream_item_genre_line,
    jellydream_item_overview,
    jellydream_merge_item_details,
    jellyfin_get_boxset,
    jellyfin_get_boxset_items,
    _GLOBAL_DETAILS_CACHE
)
from ..ui import (
    JellyDreamGridScreen,
    jellydream_apply_title_icon,
    jellydream_render_grid,
    jellydream_reset_screen_background
)

        
class JellyDreamBoxSetListScreen(JellyDreamGridScreen):
    SCREEN_NAME = "JellyDreamBoxSetListScreen"
    GRID_COUNT = 6

    def __init__(self, session):
        JellyDreamGridScreen.__init__(self, session)
        self.detail_cache = _GLOBAL_DETAILS_CACHE

        self.all_boxsets = []
        self.boxsets = []
        self.genres = ["Alle"]

        self.genreIndex = 0
        self.mode = "grid"
        self._boxsets_loaded = False

        self["title"] = Label(_("Sammlungen"))
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
            self.loadBoxsetsAsync
        )
        self.onLayoutFinish.append(
            self.applyTitleIcon
        )

    def currentGenre(self):
        try:
            return self.genres[self.genreIndex]
        except Exception:
            return "Alle"

    def applyTitleIcon(self):
        jellydream_apply_title_icon(self)

    def loadBoxsetsAsync(self):

        if self._async_loading:
            return

        self.all_boxsets = []
        self.boxsets = []
        self.genres = ["Alle"]

        self.index = 0
        self.genreIndex = 0
        self.mode = "grid"
        self._boxsets_loaded = False

        self["title"].setText(_("Sammlungen"))
        self["viewfilter"].setText(jellydream_view_filter_label())
        self["genrebar"].setText("")
        self["seriesinfo"].setText("")
        jellydream_set_imdb_badge(self, None)
        self["status"].setText(
            _("Sammlungen werden von Jellyfin geladen ...")
        )

        jellydream_render_grid(
            self,
            [],
            0,
            items_per_page=ITEMS_PER_PAGE_GRID
        )

        jellydream_apply_resolved_background(self)

        started = self.runAsyncLoad(
            loader=lambda: jellyfin_get_boxset(
                100,
                raise_on_error=True
            ),
            finished_callback=self.boxsetsLoaded,
            message=_("Sammlungen werden geladen ...")
        )

        if not started:
            self["status"].setText(
                _("Sammlungen konnten nicht geladen werden.")
            )

    def boxsetsLoaded(self, boxsets, error_message=""):

        try:
            if error_message:
                self.all_boxsets = []
                self.boxsets = []
                self.genres = ["Alle"]
                self._boxsets_loaded = False

                self["genrebar"].setText("")
                self["seriesinfo"].setText("")
                jellydream_set_imdb_badge(self, None)

                self["status"].setText(
                    _("Sammlungen konnten nicht geladen werden: %s")
                    % error_message
                )

                jellydream_render_grid(
                    self,
                    [],
                    0,
                    items_per_page=ITEMS_PER_PAGE_GRID
                )

                jellydream_apply_resolved_background(self)
                return

            self.all_boxsets = (
                list(boxsets)
                if isinstance(boxsets, list)
                else []
            )

            self.genres = jellydream_build_genres(
                self.all_boxsets
            )

            if not self.genres:
                self.genres = ["Alle"]

            self.genreIndex = 0
            self.index = 0
            self.mode = "grid"
            self._boxsets_loaded = True

            self.filterBoxsetsBySelectedGenre()

        except Exception as e:
            self.all_boxsets = []
            self.boxsets = []
            self.genres = ["Alle"]
            self._boxsets_loaded = False

            self["genrebar"].setText("")
            self["seriesinfo"].setText("")
            jellydream_set_imdb_badge(self, None)
            self["status"].setText(
                _("Sammlungen konnten nicht angezeigt werden.")
            )

            jellydream_render_grid(
                self,
                [],
                0,
                items_per_page=ITEMS_PER_PAGE_GRID
            )

            jellydream_apply_resolved_background(self)

            jd_log(
                "Loaded collections could not be processed: %s" % e,
                level="WARNING"
            )

    def filterBoxsetsBySelectedGenre(self):
        if self._async_loading:
            return

        try:
            self.boxsets = jellydream_apply_letter_filter(
                jellydream_filter_by_played(
                    jellydream_filter_by_genre(
                        self.all_boxsets,
                        self.currentGenre()
                    ),
                    jellydream_library_played_filter()
                ),
                self._letter_filter
            )

            self.index = 0
            self.updateList()

        except Exception as e:
            self.boxsets = []

            self["status"].setText(
                _("Sammlungen konnten nicht gefiltert werden.")
            )

            jellydream_render_grid(
                self,
                [],
                0,
                items_per_page=ITEMS_PER_PAGE_GRID
            )

            jd_log(
                "Error while filtering collections: %s" % e,
                level="WARNING"
            )

    def resetLetterFilter(self):
        jellydream_reset_letter_filter(
            self,
            self.filterBoxsetsBySelectedGenre
        )

    def navigateGrid(self, direction):
        # Während des Ladens keine Navigation zulassen.
        if self._async_loading:
            return

        if not self._boxsets_loaded:
            return

        if jellydream_move_alphabet(self, direction):
            return

        if (
            direction == "up"
            and self.index < 3
            and self.mode == "grid"
        ):
            self.mode = "genre"

            if self.genres:
                try:
                    self.genreIndex = (
                        self.genreIndex - 1
                    ) % len(self.genres)

                    self.filterBoxsetsBySelectedGenre()

                except Exception as e:
                    jd_log(
                        "Error while changing the collection genre: %s" % e,
                        level="WARNING"
                    )

            return

        if (
            self.mode == "genre"
            and direction in ("left", "right")
        ):
            if self.genres:
                step = 1 if direction == "right" else -1

                self.genreIndex = (
                    self.genreIndex + step
                ) % len(self.genres)

                self.filterBoxsetsBySelectedGenre()

            return

        if (
            self.mode == "genre"
            and direction == "down"
        ):
            self.mode = "grid"
            self.index = 0
            self.updateList()
            return

        if direction == "down" and self.mode == "grid":
            jellydream_focus_alphabet(self)
            return

        try:
            self.mode = "grid"

            self.index = jellydream_handle_navigation(
                self,
                direction,
                self.boxsets,
                self.index,
                items_per_page=ITEMS_PER_PAGE_GRID
            )

        except Exception as e:
            jd_log(
                "Navigation in collections failed: %s"
                % e,
                level="WARNING"
            )

    def renderBoxsetInfo(self, boxset):

        try:
            if not isinstance(boxset, dict):
                return

            item_id = str(
                boxset.get("Id") or ""
            ).strip()

            title = str(
                boxset.get("Name") or _("Ohne Titel")
            ).strip()

            year = boxset.get("ProductionYear") or "-"

            genres = jellydream_item_genre_line(boxset)

            rating = str(
                boxset.get("OfficialRating") or ""
            ).strip()

            meta_text = _("Jahr: %s") % year

            if rating:
                meta_text += "   %s" % rating

            imdb_text = jellydream_set_imdb_badge(self, boxset)

            if imdb_text:
                meta_text += "   %s" % imdb_text

            info_text = (
                _("%s: %s\n%s\nGenre: %s")
                % (_("Sammlung"), title, meta_text, genres)
            )

            overview = jellydream_item_overview(boxset)

            if overview:
                wrapped_overview = jellydream_wrap(
                    overview,
                    110,
                    4
                )

                info_text += "\n\n%s" % wrapped_overview

            self["seriesinfo"].setText(
                info_text.strip()
            )

            current_id = item_id

            backdrop_item_id = str(
                boxset.get(
                    "_jellydream_backdrop_image_item_id"
                )
                or current_id
            ).strip()

            jellydream_apply_item_backdrop(
                self,
                target_id=backdrop_item_id,
                selection_check=lambda: (
                    bool(self.boxsets)
                    and 0 <= int(self.index) < len(self.boxsets)
                    and isinstance(
                        self.boxsets[self.index],
                        dict
                    )
                    and str(
                        self.boxsets[self.index].get("Id") or ""
                    ).strip() == current_id
                )
            )
            jellydream_apply_item_logo(
                self,
                boxset,
                selection_check=lambda: (
                    bool(self.boxsets)
                    and 0 <= int(self.index) < len(self.boxsets)
                    and isinstance(self.boxsets[self.index], dict)
                    and str(self.boxsets[self.index].get("Id") or "").strip() == current_id
                )
            )

        except Exception as e:
            jd_log(
                "Collection info could not be rendered: %s" % e,
                level="WARNING"
            )

    def updateInfo(self):
        try:
            if self._async_loading:
                return

            if not self.boxsets:
                jellydream_reset_screen_background(
                    self,
                    "seriesinfo"
                )
                return

            boxset = jellydream_safe_current_item(
                self.boxsets,
                self.index
            )

            if not isinstance(boxset, dict):
                jellydream_reset_screen_background(
                    self,
                    "seriesinfo"
                )
                return

            item_id = str(
                boxset.get("Id") or ""
            ).strip()

            if not item_id:
                self.renderBoxsetInfo(boxset)
                return

            details = self.requestSelectedItemDetails(
                self.boxsets,
                item_id,
                refresh_callback=self.updateInfo
            )

            if details:
                boxset = jellydream_merge_item_details(
                    boxset,
                    details
                )

            self.renderBoxsetInfo(boxset)

        except Exception as e:
            jd_log(
                "Collection info could not be shown: %s" % e,
                level="WARNING"
            )
            
    def updateList(self, *args):
        try:
            if getattr(self, "instance", None):
                self.instance.show()
        except Exception:
            pass

        if self._async_loading:
            return

        self["title"].setText(_("Sammlungen"))
        self["viewfilter"].setText(
            jellydream_with_letter_label(
                self,
                jellydream_view_filter_label()
            )
        )

        self["genrebar"].setText(
            jellydream_genrebar_text(
                self.genres,
                self.genreIndex
            )
        )

        if not self.boxsets:
            self["seriesinfo"].setText("")
            jellydream_set_imdb_badge(self, None)

            jellydream_render_grid(
                self,
                [],
                0,
                items_per_page=ITEMS_PER_PAGE_GRID
            )

            letter_empty = jellydream_finish_letter_choice(self, False)

            if letter_empty:
                pass
            elif self.all_boxsets:
                self["status"].setText(jellydream_with_sort_status(
                    _("Keine Sammlungen in dieser Kategorie. | RUNTER = Buchstabe")
                ))
            else:
                self["status"].setText(jellydream_with_sort_status(
                    _("Keine Sammlungen gefunden.")
                ))

            jellydream_apply_resolved_background(self)

            try:
                jellydream_stop_timer(
                    self,
                    "_updateinfo_timer"
                )
            except Exception:
                pass

            return

        jellydream_render_grid(
            self,
            self.boxsets,
            self.index,
            items_per_page=ITEMS_PER_PAGE_GRID
        )
        jellydream_finish_letter_choice(self, True)

        self["status"].setText(jellydream_with_sort_status(
            _(
                "Genre wechseln: In der obersten Reihe HOCH drücken | "
                "Sammlung %s / %s | OK = Öffnen | RUNTER = Buchstabe"
            )
            % (
                self.index + 1,
                len(self.boxsets)
            )
        ))

        # Nur der letzte Auswahlzustand nach kurzer Pause lädt Details.
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
            self.loadBoxsetsAsync,
            self.filterBoxsetsBySelectedGenre
        )

    def yellowPressed(self):
        self.openItemStateMenu(
            jellydream_safe_current_item(self.boxsets, self.index),
            self.loadBoxsetsAsync
        )

    def okPressed(self):
        if self._async_loading:
            self["status"].setText(_("Bitte warten, die Sammlungen werden noch geladen ..."))
            return

        if jellydream_confirm_alphabet(
            self,
            self.filterBoxsetsBySelectedGenre
        ):
            return

        boxset = jellydream_safe_current_item(
            self.boxsets,
            self.index
        )

        if not boxset:
            jellydream_safe_set_status(
                self,
                _("Keine gültige Sammlung ausgewählt"),
                level="WARNING"
            )
            return

        item_id = str(
            boxset.get("Id") or ""
        ).strip()

        title = str(
            boxset.get("Name") or _("Sammlung")
        ).strip()

        if not item_id:
            jellydream_safe_set_status(
                self,
                _("Fehler: Sammlungs-ID fehlt"),
                level="WARNING"
            )
            return

        ok, err = jellydream_safe_open(
            self.session,
            self.updateList,
            JellyDreamBoxSetMoviesScreen,
            item_id,
            title
        )

        if not ok:
            jellydream_safe_set_status(
                self,
                err,
                level="ERROR"
            )


class JellyDreamBoxSetMoviesScreen(JellyDreamGridScreen):
    SCREEN_NAME = "JellyDreamBoxSetMoviesScreen"
    GRID_COUNT = 6

    def __init__(self, session, boxset_id="", boxset_title=""):
        JellyDreamGridScreen.__init__(self, session)
        self.detail_cache = _GLOBAL_DETAILS_CACHE

        self.boxset_id = boxset_id
        self.boxset_title = boxset_title or _("Sammlung")
        self.movies = []
        self._movies_loaded = False

        self["title"] = Label(self.boxset_title)
        self["movieinfo"] = Label("")
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

        self.onLayoutFinish.append(self.loadMoviesAsync)      

    def loadMoviesAsync(self):
        if self._async_loading:
            return

        self.movies = []
        self.index = 0
        self._movies_loaded = False

        self["title"].setText(self.boxset_title)
        self["movieinfo"].setText("")
        jellydream_set_imdb_badge(self, None)
        self["status"].setText(_("Filme der Sammlung werden geladen ..."))

        jellydream_render_grid(
            self,
            [],
            0,
            items_per_page=ITEMS_PER_PAGE_GRID
        )

        jellydream_apply_resolved_background(self)
        self._active_backdrop_id = str(
            self.boxset_id or ""
        ).strip()

        self.runAsyncLoad(
            loader=lambda: jellyfin_get_boxset_items(
                self.boxset_id,
                raise_on_error=True
            ),
            finished_callback=self.moviesLoaded,
            message=_("Filme der Sammlung werden geladen ...")
        )

    def moviesLoaded(self, movies, error_message=""):
        try:
            if error_message:
                self.movies = []
                self._movies_loaded = False

                self["movieinfo"].setText("")
                jellydream_set_imdb_badge(self, None)
                self["status"].setText(
                    _("Filme der Sammlung konnten nicht geladen werden: %s")
                    % error_message
                )

                jellydream_render_grid(
                    self,
                    [],
                    0,
                    items_per_page=ITEMS_PER_PAGE_GRID
                )

                jellydream_apply_resolved_background(self)
                return

            self.movies = (
                list(movies)
                if isinstance(movies, list)
                else []
            )

            self.index = 0
            self._movies_loaded = True
            self.updateList()

        except Exception as e:
            self.movies = []
            self._movies_loaded = False

            self["movieinfo"].setText("")
            jellydream_set_imdb_badge(self, None)
            self["status"].setText(
                _("Filme der Sammlung konnten nicht angezeigt werden.")
            )

            jellydream_render_grid(
                self,
                [],
                0,
                items_per_page=ITEMS_PER_PAGE_GRID
            )

            jellydream_apply_resolved_background(self)

            jd_log(
                "Movies in the collection could not be processed: %s" % e,
                level="WARNING"
            )
            
    def navigateGrid(self, direction):
        if self._async_loading:
            return

        if not self._movies_loaded:
            return

        try:
            self.index = jellydream_handle_navigation(
                self,
                direction,
                self.movies,
                self.index,
                items_per_page=ITEMS_PER_PAGE_GRID
            )
        except Exception:
            pass

    def updateInfo(self):
        try:
            if not self.movies:
                jellydream_reset_screen_background(self, "movieinfo")
                return

            movie = self.movies[self.index]
            item_id = movie.get("Id", "")
            
            details = self.requestSelectedItemDetails(
                self.movies,
                item_id,
                refresh_callback=self.updateInfo
            )

            if details:
                movie = jellydream_merge_item_details(
                    movie,
                    details
                )

            title = movie.get("Name", _("Ohne Titel"))
            year = movie.get("ProductionYear") or "-"
            genres = jellydream_item_genre_line(movie)

            minutes = ticks_to_minutes(movie.get("RunTimeTicks"))
            runtime_text = (_("%s Min") % minutes) if minutes else _("- Min")

            rating = str(movie.get("OfficialRating") or "").strip()
            fsk_text = f"   {rating}" if rating else ""
            imdb_text = jellydream_set_imdb_badge(self, movie)

            if imdb_text:
                fsk_text += "   %s" % imdb_text

            info_text = (
                _("Film: %s\nJahr: %s   Laufzeit: %s%s\nGenre: %s")
                % (title, year, runtime_text, fsk_text, genres)
            )
            
            overview = jellydream_item_overview(movie)
            if overview:
                wrapped_overview = jellydream_wrap(overview, 110, 5)
                info_text = f"{info_text}\n\n{wrapped_overview}"
                
            self["movieinfo"].setText(info_text.strip())

            current_id = str(item_id or "")

            jellydream_apply_item_backdrop(
                self,
                target_id=current_id,
                selection_check=lambda: (
                    bool(self.movies)
                    and 0 <= int(self.index) < len(self.movies)
                    and str(self.movies[self.index].get("Id", "")) == current_id
                )
            )
            jellydream_apply_item_logo(
                self,
                movie,
                selection_check=lambda: (
                    bool(self.movies)
                    and 0 <= int(self.index) < len(self.movies)
                    and str(self.movies[self.index].get("Id", "")) == current_id
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

        if not self.movies:
            self["status"].setText(_("EXIT = Zurück"))
            self["movieinfo"].setText("")
            jellydream_set_imdb_badge(self, None)
            jellydream_render_grid(self, [], 0)
            set_jellydream_background(self)
            try:
                if hasattr(self, "_updateinfo_timer") and self._updateinfo_timer:
                    self._updateinfo_timer.stop()
            except Exception:
                pass
            return

        jellydream_render_grid(self, self.movies, self.index)

        self["status"].setText(jellydream_with_yellow_status(
            _("Film %s / %s | Steuerkreuz = Navigieren | OK = Abspielen")
            % (self.index + 1, len(self.movies))
        ))

        # Details und Backdrop erst nach Ende schneller Navigation setzen.
        jellydream_restart_single_timer(
            self,
            "_updateinfo_timer",
            self.updateInfo,
            jellydream_update_refresh_time()
        )

    def yellowPressed(self):
        self.openItemStateMenu(
            jellydream_safe_current_item(self.movies, self.index),
            self.loadMoviesAsync
        )

    def okPressed(self):
        if self._async_loading:
            self["status"].setText(_("Bitte warten, die Filme werden noch geladen ..."))
            return
            
        movie = jellydream_safe_current_item(self.movies, self.index)
        if not movie:
            jellydream_safe_set_status(self, _("Kein gültiger Film in dieser Sammlung ausgewählt"), level="WARNING")
            return

        item_id = str(movie.get("Id", "")).strip()
        title = movie.get("Name", self.boxset_title)
        runtime_seconds = ticks_to_seconds(movie.get("RunTimeTicks"))

        if not item_id:
            jellydream_safe_set_status(self, _("Fehler: Film-ID der Sammlung fehlt"), level="WARNING")
            return

        self.playItemAsync(
            title=title,
            item_id=item_id,
            runtime_seconds=runtime_seconds,
            return_screen=self,
            callback=self.updateList,
            item=movie
        )
