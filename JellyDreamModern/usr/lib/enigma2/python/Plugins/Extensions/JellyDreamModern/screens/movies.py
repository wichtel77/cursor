# -*- coding: utf-8 -*-

from Components.ActionMap import ActionMap
from Components.Label import Label
from Components.Pixmap import Pixmap
from Components.ProgressBar import ProgressBar

from .. import _

from ..log import jd_log
from ..config import (
    jellydream_library_played_filter,
    jellydream_library_sort,
    jellydream_update_refresh_time
)
from ..images import jellydream_apply_item_backdrop, jellydream_apply_item_logo
from ..uiutil import (
    jellydream_handle_navigation,
    jellydream_show_poster_progress,
    jellydream_restart_single_timer,
    jellydream_safe_current_item,
    jellydream_safe_open,
    jellydream_safe_set_status,
    jellydream_set_imdb_badge,
    jellydream_apply_letter_filter,
    jellydream_confirm_alphabet,
    jellydream_finish_letter_choice,
    jellydream_focus_alphabet,
    jellydream_move_alphabet,
    jellydream_open_library_sort,
    jellydream_reset_letter_filter,
    jellydream_setup_letter_bar,
    jellydream_view_filter_label,
    jellydream_with_letter_label,
    jellydream_with_sort_status,
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
    jellyfin_get_movies,
    _GLOBAL_DETAILS_CACHE
)
from ..ui import (
    JellyDreamGridScreen,
    jellydream_apply_title_icon,
    jellydream_render_grid,
    jellydream_reset_screen_background
)
from .boxsets import JellyDreamBoxSetMoviesScreen

        
class JellyDreamMovieListScreen(JellyDreamGridScreen):
    SCREEN_NAME = "JellyDreamMovieListScreen"
    GRID_COUNT = 6

    def __init__(self, session, sort_override=None):
        JellyDreamGridScreen.__init__(self, session)
        self.detail_cache = _GLOBAL_DETAILS_CACHE
        self._sort_override = (
            sort_override
            if sort_override in ("name", "added")
            else None
        )

        self.all_movies = []
        self.genres = ["Alle"]
        self.genreIndex = 0
        self.mode = "grid"
        self.movies = []

        self["title"] = Label(_("Filme"))
        self["title_icon"] = Pixmap()
        self["viewfilter"] = Label("")
        self["genrebar"] = Label("")
        self["movieinfo"] = Label("")
        self["imdb_logo"] = Pixmap()
        self["imdb_rating"] = Label("")
        jellydream_setup_letter_bar(self)

        if self.SCREEN_NAME == "JellyDreamMovieListScreen":
            self["grid_progress"] = ProgressBar()

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
            self.loadMoviesAsync
        )
        self.onLayoutFinish.append(
            self.applyTitleIcon
        )

    def applyTitleIcon(self):
        jellydream_apply_title_icon(self)

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
            self.filterMoviesBySelectedGenre
        )

    def reloadFromSavedSort(self):
        self._sort_override = None
        self.loadMoviesAsync()

    def currentGenre(self):
        try: return self.genres[self.genreIndex]
        except Exception: return "Alle"

    def filterMoviesBySelectedGenre(self):
        movies = jellydream_filter_by_genre(
            self.all_movies,
            self.currentGenre()
        )

        if self.SCREEN_NAME == "JellyDreamMovieListScreen":
            movies = jellydream_filter_by_played(
                movies,
                jellydream_library_played_filter()
            )

        self.movies = jellydream_apply_letter_filter(
            movies,
            self._letter_filter
        )
        self.index = 0
        self.updateList()

    def loadMoviesAsync(self):
        if self._async_loading:
            return

        self.movies = []
        self.index = 0

        self["title"].setText(_("Filme"))
        self["viewfilter"].setText(self.viewFilterText())
        self["genrebar"].setText("")
        self["movieinfo"].setText("")
        jellydream_set_imdb_badge(self, None)
        self["status"].setText(_("Filmliste wird von Jellyfin geladen ..."))

        jellydream_render_grid(
            self,
            [],
            0
        )

        self.runAsyncLoad(
            loader=lambda: jellyfin_get_movies(
                2000,
                raise_on_error=True,
                sort_mode=self.sortMode()
            ),
            finished_callback=self.moviesLoaded,
            message="Filmliste wird geladen ..."
        )

    def moviesLoaded(self, movies, error_message=""):
        try:
            if error_message:
                self.all_movies = []
                self.movies = []
                self.genres = ["Alle"]

                self["status"].setText(
                    _("Filmliste konnte nicht geladen werden: %s")
                    % error_message
                )

                jellydream_render_grid(
                    self,
                    [],
                    0
                )
                return

            self.all_movies = (
                list(movies)
                if isinstance(movies, list)
                else []
            )

            self.genres = jellydream_build_genres(
                self.all_movies
            )

            self.genreIndex = 0
            self.filterMoviesBySelectedGenre()

        except Exception as e:
            self["status"].setText(_("Filmliste konnte nicht angezeigt werden."))

            jd_log(
                "Loaded movie list could not be processed: %s" % e,
                level="WARNING"
            )
        
    def navigateGrid(self, direction):
        if self._async_loading:
            return

        if jellydream_move_alphabet(self, direction):
            return

        if direction == "down":
            jellydream_focus_alphabet(self)
            return

        if direction == "up" and self.index < 3:
            if self.genres:
                try:
                    self.genreIndex = (
                        self.genreIndex - 1
                    ) % len(self.genres)

                    self.filterMoviesBySelectedGenre()

                except Exception as e:
                    jd_log(
                        "Error while changing the movie genre: %s" % e,
                        level="WARNING"
                    )
            return

        try:
            self.index = jellydream_handle_navigation(
                self,
                direction,
                self.movies,
                self.index
            )
        except Exception:
            pass
            
    def updateInfo(self):
        try:
            if not self.movies:
                jellydream_reset_screen_background(self, "movieinfo")
                jellydream_set_imdb_badge(self, None)
                jellydream_apply_item_logo(self, None)
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
            
            item_type = str(movie.get("Type") or "").strip()
            is_boxset = item_type in ("BoxSet", "CollectionFolder")

            rating = str(movie.get("OfficialRating") or "").strip()

            meta_text = _("Jahr: %s") % year

            if not is_boxset:
                minutes = ticks_to_minutes(movie.get("RunTimeTicks"))
                runtime_text = (_("%s Min") % minutes) if minutes else _("- Min")
                meta_text += "   " + (_("Laufzeit: %s") % runtime_text)

            if rating:
                meta_text += "   %s" % rating

            imdb_text = jellydream_set_imdb_badge(self, movie)

            if imdb_text:
                meta_text += "   %s" % imdb_text

            item_label = self.itemKindLabel(movie, is_boxset)

            info_text = (
                _("%s: %s\n%s\nGenre: %s")
                % (item_label, title, meta_text, genres)
            )
            
            overview = jellydream_item_overview(movie)
            if overview:
                wrapped_overview = jellydream_wrap(overview, 110, 4)
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

        except Exception as e:
            try: jd_log(f"Error in updateInfo backdrop engine: {e}", level="ERROR")
            except Exception: pass

    def itemKindLabel(self, item, is_boxset=False):
        if is_boxset:
            return _("Sammlung")

        return _("Film")

    def updateList(self, *args):
        try:
            if getattr(self, "instance", None):
                self.instance.show()
        except Exception:
            pass

        self["title"].setText(_("Filme"))
        self["viewfilter"].setText(self.viewFilterText())
        self["genrebar"].setText(jellydream_genrebar_text(self.genres, self.genreIndex))

        jellydream_render_grid(self, self.movies, self.index)
        jellydream_show_poster_progress(
            self,
            self.movies,
            self.index,
            self.GRID_COUNT
        )
        letter_empty = jellydream_finish_letter_choice(
            self,
            bool(self.movies)
        )

        if letter_empty:
            self["movieinfo"].setText("")
            jellydream_set_imdb_badge(self, None)
        elif self.movies:
            self["status"].setText(jellydream_with_sort_status(
                _("Genre wechseln: In der obersten Reihe HOCH drücken | Film %s / %s | OK = Abspielen | RUNTER = Buchstabe") % (self.index + 1, len(self.movies))
            ))
        else:
            self["status"].setText(jellydream_with_sort_status(_("Genre wechseln: In der obersten Reihe HOCH drücken | Keine Filme vorhanden | RUNTER = Buchstabe")))
            
        # Info, Details und Backdrop erst aktualisieren, wenn der Benutzer
        # kurz auf dem aktuell gewählten Eintrag stehen bleibt.
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
            self.filterMoviesBySelectedGenre
        )

    def yellowPressed(self):
        self.openItemStateMenu(
            jellydream_safe_current_item(self.movies, self.index),
            self.loadMoviesAsync
        )

    def okPressed(self):
        if self._async_loading:
            self["status"].setText(_("Bitte warten, die Filmliste wird noch geladen ..."))
            return

        if jellydream_confirm_alphabet(
            self,
            self.filterMoviesBySelectedGenre
        ):
            return

        movie = jellydream_safe_current_item(self.movies, self.index)
        if not movie:
            jellydream_safe_set_status(self, _("Kein gültiger Film an der aktuellen Position"), level="WARNING")
            return

        item_id = str(movie.get("Id", "")).strip()
        title = movie.get("Name", "Jellyfin Film")

        if not item_id:
            jellydream_safe_set_status(self, _("Fehler: Film-ID fehlt"), level="WARNING")
            return

        runtime = ticks_to_seconds(
            movie.get("RunTimeTicks")
        )

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
                    self.updateList,
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

            self.playItemAsync(
                title=title,
                item_id=item_id,
                runtime_seconds=runtime,
                return_screen=self,
                callback=self.updateList,
                item=movie
            )

        self.resolveBoxsetAsync(
            item_id,
            title,
            movie.get("Type", ""),
            boxset_resolved
        )
        return
