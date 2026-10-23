# -*- coding: utf-8 -*-

from .. import _

from ..log import jd_log
from ..library import jellyfin_get_favorites
from ..uiutil import (
    jellydream_episode_service_title,
    jellydream_safe_current_item,
    jellydream_safe_open,
    jellydream_safe_set_status,
    jellydream_set_imdb_badge,
    jellydream_with_yellow_status,
    ticks_to_seconds
)
from ..ui import jellydream_render_grid
from .boxsets import JellyDreamBoxSetMoviesScreen
from .movies import JellyDreamMovieListScreen
from .seasons import JellyDreamSeasonListScreen


class JellyDreamFavoriteListScreen(JellyDreamMovieListScreen):
    SCREEN_NAME = "JellyDreamFavoriteListScreen"

    def __init__(self, session):
        JellyDreamMovieListScreen.__init__(self, session)
        self["title"].setText(_("Favoriten"))

    def greenPressed(self):
        return

    def itemKindLabel(self, item, is_boxset=False):
        item_type = str(
            (item or {}).get("Type") or ""
        ).strip()

        if item_type == "Series":
            return _("Serie")

        if item_type == "Episode":
            return _("Folge")

        if is_boxset or item_type == "BoxSet":
            return _("Sammlung")

        return _("Film")

    def loadMoviesAsync(self):
        if self._async_loading:
            return

        self.movies = []
        self.index = 0
        self["title"].setText(_("Favoriten"))
        self["genrebar"].setText("")
        self["movieinfo"].setText("")
        jellydream_set_imdb_badge(self, None)
        self["status"].setText(_("Favoriten werden geladen ..."))
        jellydream_render_grid(self, [], 0)

        self.runAsyncLoad(
            loader=lambda: jellyfin_get_favorites(
                2000,
                raise_on_error=True
            ),
            finished_callback=self.moviesLoaded,
            message="Favorites are loading ..."
        )

    def moviesLoaded(self, movies, error_message=""):
        try:
            if error_message:
                self.all_movies = []
                self.movies = []
                self.genres = ["Alle"]
                self["status"].setText(
                    _("Favoriten konnten nicht geladen werden: %s")
                    % error_message
                )
                jellydream_render_grid(self, [], 0)
                return

            self.all_movies = (
                list(movies)
                if isinstance(movies, list)
                else []
            )
            self.genres = self._favorite_genres()
            self.genreIndex = 0
            self.filterMoviesBySelectedGenre()

        except Exception as e:
            self["status"].setText(
                _("Favoriten konnten nicht angezeigt werden.")
            )
            jd_log(
                "Loaded favorites could not be processed: %s" % e,
                level="WARNING"
            )

    def _favorite_genres(self):
        from ..library import jellydream_build_genres

        return jellydream_build_genres(self.all_movies)

    def updateList(self, *args):
        try:
            if getattr(self, "instance", None):
                self.instance.show()
        except Exception:
            pass

        self["title"].setText(_("Favoriten"))
        from ..library import jellydream_genrebar_text

        self["genrebar"].setText(
            jellydream_genrebar_text(self.genres, self.genreIndex)
        )
        jellydream_render_grid(self, self.movies, self.index)

        if self.movies:
            self["status"].setText(jellydream_with_yellow_status(
                _(
                    "Genre wechseln: In der obersten Reihe HOCH drücken | "
                    "Favorit %s / %s | OK = Öffnen"
                ) % (self.index + 1, len(self.movies))
            ))
        else:
            self["status"].setText(
                _(
                    "Genre wechseln: In der obersten Reihe HOCH drücken | "
                    "Keine Favoriten"
                )
            )

        from ..config import jellydream_update_refresh_time
        from ..uiutil import jellydream_restart_single_timer

        jellydream_restart_single_timer(
            self,
            "_updateinfo_timer",
            self.updateInfo,
            jellydream_update_refresh_time()
        )

    def okPressed(self):
        if self._async_loading:
            self["status"].setText(_("Favoriten werden geladen ..."))
            return

        item = jellydream_safe_current_item(self.movies, self.index)

        if not item:
            jellydream_safe_set_status(
                self,
                _("Kein gültiger Favorit an der aktuellen Position"),
                level="WARNING"
            )
            return

        item_id = str(item.get("Id") or "").strip()
        title = item.get("Name") or _("Favorit")
        item_type = str(item.get("Type") or "").strip()

        if not item_id:
            jellydream_safe_set_status(
                self,
                _("Fehler: Favoriten-ID fehlt"),
                level="WARNING"
            )
            return

        if item_type == "Series":
            ok, err = jellydream_safe_open(
                self.session,
                self.updateList,
                JellyDreamSeasonListScreen,
                item_id,
                title
            )

            if not ok:
                jellydream_safe_set_status(self, err, level="ERROR")

            return

        if item_type == "BoxSet":
            ok, err = jellydream_safe_open(
                self.session,
                self.updateList,
                JellyDreamBoxSetMoviesScreen,
                item_id,
                title
            )

            if not ok:
                jellydream_safe_set_status(self, err, level="ERROR")

            return

        if item_type == "Episode":
            title = jellydream_episode_service_title(
                series_name=item.get("SeriesName"),
                season_number=item.get("ParentIndexNumber"),
                episode_number=item.get("IndexNumber"),
                episode_name=title
            )
            self.playItemAsync(
                title=title,
                item_id=item_id,
                runtime_seconds=ticks_to_seconds(item.get("RunTimeTicks")),
                return_screen=self,
                callback=self.updateList,
                item=item
            )
            return

        if item_type != "Movie":
            jellydream_safe_set_status(
                self,
                _("Unbekannter Treffertyp: %s") % item_type,
                level="WARNING"
            )
            return

        runtime = ticks_to_seconds(item.get("RunTimeTicks"))

        def boxset_resolved(result, error_message=""):
            if error_message:
                self.setStatus(error_message, level="ERROR")
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
                    self.setStatus(err, level="ERROR")

                return

            self.playItemAsync(
                title=title,
                item_id=item_id,
                runtime_seconds=runtime,
                return_screen=self,
                callback=self.updateList,
                item=item
            )

        self.resolveBoxsetAsync(
            item_id,
            title,
            item_type or "Movie",
            boxset_resolved
        )
