# -*- coding: utf-8 -*-

from Components.ActionMap import ActionMap
from Components.Label import Label
from Components.ProgressBar import ProgressBar

from .. import _

from ..constants import ITEMS_PER_PAGE_EPISODES
from ..log import jd_log
from ..config import jellydream_update_refresh_time
from ..images import (
    jellydream_apply_item_backdrop,
    jellydream_apply_item_logo,
    set_jellydream_background
)
from ..uiutil import (
    jellydream_handle_navigation,
    jellydream_int_or_default,
    jellydream_restart_single_timer,
    jellydream_safe_current_item,
    jellydream_safe_set_status,
    jellydream_show_poster_progress,
    jellydream_with_yellow_status,
    jellydream_wrap,
    ticks_to_minutes,
    ticks_to_seconds
)
from ..library import (
    jellydream_item_overview,
    jellydream_merge_item_details,
    jellyfin_get_episodes,
    jellyfin_get_series_seasons,
    _GLOBAL_DETAILS_CACHE
)
from ..ui import (
    JellyDreamGridScreen,
    jellydream_render_grid,
    jellydream_reset_screen_background
)


class JellyDreamEpisodeListScreen(JellyDreamGridScreen):
    SCREEN_NAME = "JellyDreamEpisodeListScreen"
    GRID_COUNT = ITEMS_PER_PAGE_EPISODES

    def __init__(self, session, series_id="", series_title="Serie", chosen_season=1):
        JellyDreamGridScreen.__init__(self, session)
        self.detail_cache = _GLOBAL_DETAILS_CACHE

        self.series_id = series_id
        self.series_title = series_title or "Serie"
        self.seasons = []

        self.season = chosen_season
        self.episodes = []
        self.mode = "episodes"

        self["title"] = Label("")
        self["seasonbar"] = Label("")
        self["episodeinfo"] = Label("")
        self["grid_progress"] = ProgressBar()

        self["actions"] = ActionMap(
            ["OkCancelActions", "DirectionActions", "ColorActions"],
            {
                "cancel": self.close,
                "ok": self.okPressed,
                "yellow": self.yellowPressed,
                "left": lambda: self.navigateEpisodes("left"),
                "right": lambda: self.navigateEpisodes("right"),
                "up": lambda: self.navigateEpisodes("up"),
                "down": lambda: self.navigateEpisodes("down"),
            },
            -1
        )

        self.onLayoutFinish.append(self.loadSeason)      
        
    def loadSeason(self):
        if self._async_loading:
            return

        self.episodes = []
        self.index = 0

        self["title"].setText(
            _("%s - Staffel %s")
            % (
                self.series_title or _("Serie"),
                self.season
            )
        )

        self["episodeinfo"].setText("")
        self["status"].setText(
            _("Folgen der Staffel %s werden geladen ...")
            % self.season
        )

        jellydream_render_grid(
            self,
            [],
            0,
            items_per_page=ITEMS_PER_PAGE_EPISODES
        )

        requested_season = int(self.season)

        def loader():
            seasons = self.seasons

            if not seasons:
                seasons = jellyfin_get_series_seasons(
                    self.series_id,
                    raise_on_error=True
                )

            episodes = jellyfin_get_episodes(
                self.series_id,
                requested_season,
                raise_on_error=True
            )

            return {
                "season": requested_season,
                "seasons": seasons,
                "episodes": episodes
            }

        self.runAsyncLoad(
            loader=loader,
            finished_callback=self.episodesLoaded,
            message=(
                _("Folgen der Staffel %s werden geladen ...")
                % requested_season
            )
        )

    def episodesLoaded(self, result, error_message=""):
        if error_message:
            self.episodes = []

            self["status"].setText(
                _("Folgen konnten nicht geladen werden: %s")
                % error_message
            )

            jellydream_render_grid(
                self,
                [],
                0,
                items_per_page=ITEMS_PER_PAGE_EPISODES
            )
            jellydream_show_poster_progress(
                self,
                [],
                0,
                ITEMS_PER_PAGE_EPISODES
            )
            return

        if not isinstance(result, dict):
            result = {}

        loaded_season = jellydream_int_or_default(
            result.get("season"),
            self.season
        )

        if loaded_season != int(self.season):
            return

        seasons = result.get("seasons")
        episodes = result.get("episodes")

        self.seasons = (
            list(seasons)
            if isinstance(seasons, list)
            else []
        )

        self.episodes = (
            list(episodes)
            if isinstance(episodes, list)
            else []
        )

        self.index = 0
        self.mode = "episodes"
        self.updateList()

    def navigateEpisodes(self, direction):
        if self.mode == "seasonbar":
            if direction == "left":
                if self.seasons and len(self.seasons) > 1:
                    try:
                        season_numbers = [int(s.get("IndexNumber") if s.get("IndexNumber") is not None else s.get("index", i + 1)) for i, s in enumerate(self.seasons)]
                        if int(self.season) in season_numbers:
                            pos = season_numbers.index(int(self.season))
                            self.season = season_numbers[(pos - 1) % len(season_numbers)]
                            self.loadSeason()
                    except Exception as e:
                        jd_log(
                            "Error while switching to the previous season: %s" % e,
                            level="WARNING"
                        )
                return

            elif direction == "right":
                if self.seasons and len(self.seasons) > 1:
                    try:
                        season_numbers = [int(s.get("IndexNumber") if s.get("IndexNumber") is not None else s.get("index", i + 1)) for i, s in enumerate(self.seasons)]
                        if int(self.season) in season_numbers:
                            pos = season_numbers.index(int(self.season))
                            self.season = season_numbers[(pos + 1) % len(season_numbers)]
                            self.loadSeason()
                    except Exception as e:
                        jd_log(
                            "Error while switching to the next season: %s"% e,
                            level="WARNING"
                        )
                return

            elif direction == "down":
                self.mode = "episodes"
                self.updateList()
                return

            elif direction == "up":
                return

        if not self.episodes:
            return

        episode_columns = 4 if ITEMS_PER_PAGE_EPISODES == 8 else 3

        if direction == "up" and self.index < episode_columns:
            if self.seasons and len(self.seasons) > 1:
                self.mode = "seasonbar"
                self.updateList()
                return

        try:
            self.mode = "episodes"
            self.index = jellydream_handle_navigation(
                self,
                direction,
                self.episodes,
                self.index,
                items_per_page=ITEMS_PER_PAGE_EPISODES,
                columns=4 if ITEMS_PER_PAGE_EPISODES == 8 else 3
            )
        except Exception:
            pass

    def updateInfo(self):
        try:
            if not self.episodes:
                jellydream_reset_screen_background(
                    self,
                    "episodeinfo"
                )
                return

            ep = jellydream_safe_current_item(
                self.episodes,
                self.index
            )

            if not isinstance(ep, dict):
                jellydream_reset_screen_background(
                    self,
                    "episodeinfo"
                )
                return

            item_id = str(
                ep.get("Id") or ""
            ).strip()

            details = self.requestSelectedItemDetails(
                self.episodes,
                item_id,
                refresh_callback=self.updateInfo
            )

            if details:
                ep = jellydream_merge_item_details(
                    ep,
                    details
                )

            title = str(
                ep.get("Name") or _("Ohne Titel")
            ).strip()

            season_number = ep.get("ParentIndexNumber")

            if season_number is None:
                season_number = self.season

            episode_number = ep.get("IndexNumber")

            if episode_number is None:
                episode_number = self.index + 1

            season_number = jellydream_int_or_default(
                season_number,
                self.season
            )

            try:
                episode_number = int(episode_number)
            except Exception:
                episode_number = self.index + 1

            minutes = ticks_to_minutes(
                ep.get("RunTimeTicks")
            )

            runtime_text = (
                (_("%s Min") % minutes)
                if minutes > 0
                else _("- Min")
            )

            premiered = str(
                ep.get("PremiereDate") or ""
            ).strip()

            if premiered:
                try:
                    year, month, day = premiered[:10].split("-")

                    date_text = "%s.%s.%s" % (
                        day,
                        month,
                        year
                    )

                except Exception:
                    date_text = premiered[:10]
            else:
                date_text = "-"

            rating = str(
                ep.get("OfficialRating") or ""
            ).strip()

            meta_text = (
                _("Laufzeit: %s   Erstausstrahlung: %s")
                % (
                    runtime_text,
                    date_text
                )
            )

            if rating:
                meta_text += "   %s" % rating

            info_text = (
                _("Folge: S%02dE%02d - %s\n%s")
                % (
                    season_number,
                    episode_number,
                    title,
                    meta_text
                )
            )

            overview = jellydream_item_overview(ep)

            if overview:
                wrapped_overview = jellydream_wrap(
                    overview,
                    110,
                    5
                )

                info_text += "\n\n%s" % wrapped_overview

            self["episodeinfo"].setText(
                info_text.strip()
            )

            current_episode_id = item_id

            current_target_id = str(
                ep.get("SeriesId")
                or self.series_id
                or item_id
            ).strip()

            jellydream_apply_item_logo(
                self,
                ep,
                text_widget_name="title",
                hide_text_when_shown=True,
                fallback_id=self.series_id,
                selection_check=lambda: (
                    bool(self.episodes)
                    and 0 <= int(self.index) < len(self.episodes)
                    and str(self.episodes[self.index].get("Id", "")) == item_id
                )
            )
            jellydream_apply_item_backdrop(
                self,
                target_id=current_target_id,
                selection_check=lambda: (
                    bool(self.episodes)
                    and 0 <= int(self.index) < len(self.episodes)
                    and str(
                        self.episodes[self.index].get("Id") or ""
                    ).strip() == current_episode_id
                )
            )

        except Exception as e:
            jd_log(
                "Episode info could not be shown: %s" % e,
                level="WARNING"
            )

    def updateList(self, *args):
        try:
            if getattr(self, "instance", None):
                self.instance.show()
        except Exception:
            pass

        try:
            active_color = "\\c00f1c40f"
            normal_color = "\\c00ffffff"

            if len(self.seasons) > 1:
                season_numbers = [int(s.get("IndexNumber") if s.get("IndexNumber") is not None else s.get("index", i + 1)) for i, s in enumerate(self.seasons)]
                pos = season_numbers.index(int(self.season)) if int(self.season) in season_numbers else 0

                prev_season = season_numbers[(pos - 1) % len(season_numbers)]
                next_season = season_numbers[(pos + 1) % len(season_numbers)]

                if self.mode == "seasonbar":
                    self["seasonbar"].setText(
                        "%s<  %s %s     %s[ %s %s %s %s ]%s     %s %s  >"
                        % (
                            normal_color,
                            _("Staffel"),
                            prev_season,
                            active_color,
                            _("Staffel"),
                            self.season,
                            _("von"),
                            len(self.seasons),
                            normal_color,
                            _("Staffel"),
                            next_season
                        )
                    )
                else:
                    self["seasonbar"].setText(
                        "%s<  %s %s     [ %s %s %s %s ]     %s %s  >"
                        % (
                            normal_color,
                            _("Staffel"),
                            prev_season,
                            _("Staffel"),
                            self.season,
                            _("von"),
                            len(self.seasons),
                            _("Staffel"),
                            next_season
                        )
                    )
            else:
                if self.mode == "seasonbar":
                    self["seasonbar"].setText(
                        "%s<     %s[ %s %s ]%s     >"
                        % (
                            normal_color,
                            active_color,
                            _("Staffel"),
                            self.season,
                            normal_color
                        )
                    )
                else:
                    self["seasonbar"].setText(
                        "[ %s %s ]" % (_("Staffel"), self.season)
                    )
        except Exception:
            self["seasonbar"].setText("")

        if not self.episodes:
            self["status"].setText(_("Keine Folgen in dieser Staffel gefunden."))
            self["episodeinfo"].setText("")
            jellydream_render_grid(self, [], 0, items_per_page=ITEMS_PER_PAGE_EPISODES)
            jellydream_show_poster_progress(self, [], 0, ITEMS_PER_PAGE_EPISODES)
            set_jellydream_background(self)
            try:
                if hasattr(self, "_updateinfo_timer") and self._updateinfo_timer:
                    self._updateinfo_timer.stop()
            except Exception:
                pass
            return

        jellydream_render_grid(self, self.episodes, self.index, items_per_page=ITEMS_PER_PAGE_EPISODES)
        jellydream_show_poster_progress(
            self,
            self.episodes,
            self.index,
            ITEMS_PER_PAGE_EPISODES
        )

        if self.mode == "seasonbar":
            self["status"].setText(
                _("Staffel %s | LINKS/RECHTS = Staffel wechseln | RUNTER = Zur Folgenliste") % self.season
            )
        else:
            self["status"].setText(jellydream_with_yellow_status(
                _("Folge %s / %s | HOCH oben = Staffelauswahl | OK = Abspielen") % (self.index + 1, len(self.episodes))
            ))

        # Details und Serien-Backdrop erst nach der Navigationspause laden.
        jellydream_restart_single_timer(
            self,
            "_updateinfo_timer",
            self.updateInfo,
            jellydream_update_refresh_time()
        )

    def yellowPressed(self):
        self.openItemStateMenu(
            jellydream_safe_current_item(self.episodes, self.index),
            self.loadSeason
        )

    def okPressed(self):
        ep = jellydream_safe_current_item(self.episodes, self.index)
        if not ep:
            jellydream_safe_set_status(self, _("Keine gültige Folge ausgewählt"), level="WARNING")
            return

        item_id = str(ep.get("Id", "")).strip()
        if not item_id:
            jellydream_safe_set_status(self, _("Fehler: Episoden-ID fehlt"), level="WARNING")
            return

        s = jellydream_int_or_default(
            ep.get("ParentIndexNumber"),
            self.season
        )

        e = jellydream_int_or_default(
            ep.get("IndexNumber"),
            self.index + 1
        )

        ep_title = ep.get("Name", "Folge")

        title = "S%02dE%02d - %s" % (
            s,
            e,
            ep_title
        )
        runtime = ticks_to_seconds(ep.get("RunTimeTicks"))

        self.playItemAsync(
            title=title,
            item_id=item_id,
            runtime_seconds=runtime,
            return_screen=self,
            callback=self.updateList,
            item=ep
        )
