# -*- coding: utf-8 -*-

from Components.ActionMap import ActionMap
from Components.Label import Label
from Components.Pixmap import Pixmap

from .. import _

from ..constants import FALLBACK_HOME_COVER
from ..config import jellydream_cache_dir
from ..images import (
    jellydream_apply_item_backdrop,
    jellydream_apply_item_logo,
    jellydream_download_image,
    jellydream_valid_file
)
from ..uiutil import (
    jellydream_safe_set_pixmap,
    jellydream_safe_set_status,
    jellydream_set_imdb_badge,
    jellydream_with_yellow_status,
    jellydream_wrap,
    ticks_to_minutes,
    ticks_to_seconds
)
from ..library import (
    jellydream_item_genre_line,
    jellydream_item_overview,
    jellydream_merge_item_details,
    jellyfin_get_item_details,
    _GLOBAL_DETAILS_CACHE
)
from ..ui import JellyDreamBaseScreen, jellydream_set_favorite_heart


class JellyDreamHomeMovieInfoScreen(JellyDreamBaseScreen):
    SCREEN_NAME = "JellyDreamHomeMovieInfoScreen"
    AUTO_STATUS = True

    def __init__(self, session, title, item_id, item=None, return_screen=None, preloaded_details=None):
        JellyDreamBaseScreen.__init__(self, session)
        self.detail_cache = _GLOBAL_DETAILS_CACHE

        self.title_text = title or _("Film")
        self.item_id = item_id or ""
        self.item = item or {}
        self.return_screen = return_screen
        self.preloaded_details = preloaded_details if isinstance(preloaded_details, dict) else {}
        self.runtime_seconds = 0

        self["title"] = Label(self.title_text)
        self["item_logo"] = Pixmap()
        self["poster"] = Pixmap()
        self["meta"] = Label("")
        self["imdb_logo"] = Pixmap()
        self["imdb_rating"] = Label("")
        self["play_button"] = Label(_("OK Starten"))
        self["overview_title"] = Label(_("Beschreibung"))
        self["overview"] = Label("")
        self["status"].setText(jellydream_with_yellow_status(
            _("OK = Starten     EXIT = Zurück")
        ))

        self["actions"] = ActionMap(
            ["OkCancelActions", "ColorActions"],
            {
                "cancel": self.close,
                "ok": self.okPressed,
                "yellow": self.yellowPressed,
            },
            -1
        )

        self.onLayoutFinish.append(self.loadDetails)

    def loadDetails(self):
        if self._async_loading:
            return

        base_item = (
            dict(self.item)
            if isinstance(self.item, dict)
            else {}
        )

        preloaded = (
            dict(self.preloaded_details)
            if isinstance(self.preloaded_details, dict)
            else {}
        )

        def loader():
            details = jellyfin_get_item_details(
                self.item_id,
                raise_on_error=True
            )

            merged_item = jellydream_merge_item_details(
                base_item,
                preloaded
            )

            merged_item = jellydream_merge_item_details(
                merged_item,
                details
            )

            poster_path = ""

            if self.item_id:
                poster_path = jellydream_download_image(
                    self.item_id,
                    jellydream_cache_dir("cover"),
                    width=400,
                    height=600,
                    image_type="Primary"
                )

            return {
                "item": merged_item,
                "poster": poster_path
            }

        self.runAsyncLoad(
            loader=loader,
            finished_callback=self.detailsLoaded,
            message="Filminformationen werden geladen ..."
        )

    def detailsLoaded(self, result, error_message=""):
        if error_message:
            self["meta"].setText(
                "Filminfo konnte nicht geladen werden: %s"
                % error_message
            )
            jellydream_set_imdb_badge(self, None)
            jellydream_set_favorite_heart(self, "poster_heart", "poster", None)
            return

        if not isinstance(result, dict):
            result = {}

        loaded_item = result.get("item")

        if isinstance(loaded_item, dict):
            self.item = loaded_item

        poster = str(
            result.get("poster") or ""
        )

        if jellydream_valid_file(poster, 500):
            jellydream_safe_set_pixmap(
                self["poster"],
                poster,
                hide_if_missing=False
            )
        else:
            jellydream_safe_set_pixmap(
                self["poster"],
                FALLBACK_HOME_COVER,
                hide_if_missing=True
            )

        jellydream_set_favorite_heart(
            self,
            "poster_heart",
            "poster",
            self.item
        )

        title = self.item.get("Name") or self.title_text
        year = self.item.get("ProductionYear") or "-"

        minutes = ticks_to_minutes(
            self.item.get("RunTimeTicks")
        )

        self.runtime_seconds = ticks_to_seconds(
            self.item.get("RunTimeTicks")
        )

        genres = jellydream_item_genre_line(self.item)

        rating = self.item.get("OfficialRating") or ""

        directors = []

        for person in self.item.get("People") or []:
            if not isinstance(person, dict):
                continue

            person_type = str(
                person.get("Type") or ""
            ).lower()

            person_role = str(
                person.get("Role") or ""
            ).lower()

            if (
                person_type == "director"
                or "director" in person_role
                or "regie" in person_role
            ):
                directors.append(
                    person.get("Name", "")
                )

        director = ", ".join(
            value
            for value in directors
            if value
        ) or _("unbekannt")

        meta = (
            _("Jahr: %s   Laufzeit: %s Min")
            % (
                year,
                minutes
            )
        )

        if rating:
            meta += "   %s" % rating

        imdb_text = jellydream_set_imdb_badge(self, self.item)

        if imdb_text:
            meta += "   %s" % imdb_text

        meta += "\n" + (_("Regie: %s\nGenre: %s") % (
            director,
            genres
        ))

        self["title"].setText(title)
        self["meta"].setText(meta)
        jellydream_apply_item_logo(
            self,
            self.item,
            text_widget_name="title",
            hide_text_when_shown=True,
            fallback_id=(
                self.item.get("SeriesId")
                if str(self.item.get("Type") or "") == "Episode"
                else ""
            )
        )

        self["overview"].setText(
            jellydream_wrap(
                jellydream_item_overview(self.item),
                74,
                8
            )
            or _("Keine Beschreibung vorhanden.")
        )

        media_type = self.item.get("Type", "")

        target_id = str(
            (
                self.item.get("SeriesId")
                or self.item_id
            )
            if media_type == "Episode"
            else self.item_id
        )

        jellydream_apply_item_backdrop(
            self,
            target_id=target_id,
            selection_check=lambda: (
                str(getattr(self, "item_id", "") or "")
                == str(self.item_id)
            )
        )

    def yellowPressed(self):
        self.openItemStateMenu(
            getattr(self, "item", None),
            self.loadDetails
        )

    def okPressed(self):
        try:
            title = getattr(self, "title_text", "Jellyfin Film")
            item_id = getattr(self, "item_id", "")

            if not item_id and hasattr(self, "movie_id"):
                item_id = self.movie_id
            if not item_id and hasattr(self, "item") and isinstance(self.item, dict):
                item_id = self.item.get("Id", "")
                title = self.item.get("Name", title)

            item_id = str(item_id or "").strip()
            if not item_id:
                jellydream_safe_set_status(self, _("Fehler: Film-ID konnte nicht ermittelt werden"), level="WARNING")
                return

            runtime = self.runtime_seconds
            if not runtime and hasattr(self, "item") and isinstance(self.item, dict):
                runtime = ticks_to_seconds(self.item.get("RunTimeTicks"))

            self.playItemAsync(
                title=title,
                item_id=item_id,
                runtime_seconds=runtime,
                return_screen=self,
                callback=self.afterPlayerClosed,
                item=getattr(self, "item", None)
            )

        except Exception as e:
            jellydream_safe_set_status(self, _("Fehler beim Starten des Streams: %s") % e, level="ERROR")

    def afterPlayerClosed(self, *args):
        try:
            if getattr(self, "instance", None):
                self.instance.show()
            self.close()
        except Exception:
            self.close()
