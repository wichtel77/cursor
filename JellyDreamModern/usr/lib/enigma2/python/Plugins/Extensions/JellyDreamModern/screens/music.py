# -*- coding: utf-8 -*-

import random

from Components.ActionMap import ActionMap
from Components.Label import Label
from Components.Pixmap import Pixmap
from enigma import ePoint, eSize
from skin import parseColor

from .. import _

from ..config import (
    jellydream_cache_dir,
    jellydream_music_played_filter,
    jellydream_music_sort,
    jellydream_update_refresh_time
)
from ..images import (
    jellydream_apply_item_logo,
    jellydream_async_download_image,
    jellydream_get_cached_image_path,
    jellydream_valid_file
)
from ..library import (
    jellydream_build_genres,
    jellydream_filter_by_genre,
    jellydream_filter_by_played,
    jellydream_genrebar_text,
    jellydream_item_genre_line,
    jellydream_item_overview,
    jellydream_merge_item_details,
    jellyfin_get_album_tracks,
    jellyfin_get_music,
    _GLOBAL_DETAILS_CACHE
)
from ..log import jd_log
from ..ui import (
    JellyDreamGridScreen,
    jellydream_apply_title_icon,
    jellydream_menu_icon,
    jellydream_render_grid,
    jellydream_reset_screen_background
)
from ..uiutil import (
    jellydream_apply_letter_filter,
    jellydream_confirm_alphabet,
    jellydream_finish_letter_choice,
    jellydream_focus_alphabet,
    jellydream_handle_navigation,
    jellydream_move_alphabet,
    jellydream_reset_letter_filter,
    jellydream_setup_letter_bar,
    jellydream_with_letter_label,
    jellydream_music_queue_clear,
    jellydream_music_queue_has_items,
    jellydream_music_queue_set,
    jellydream_music_view_label,
    jellydream_open_music_view,
    jellydream_restart_single_timer,
    jellydream_safe_current_item,
    jellydream_safe_open,
    jellydream_safe_set_pixmap,
    jellydream_safe_set_status,
    jellydream_with_sort_status,
    jellydream_with_yellow_status,
    jellydream_wrap,
    ticks_to_minutes,
    ticks_to_seconds
)


TRACK_ROWS = 10


def jellydream_album_artist(item):
    if not isinstance(item, dict):
        return ""

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


def jellydream_track_clock(ticks):
    seconds = ticks_to_seconds(ticks)

    if seconds <= 0:
        return "--:--"

    return "%d:%02d" % (seconds // 60, seconds % 60)


def jellydream_track_line(item, position):
    title = str(item.get("Name") or _("Ohne Titel")).strip() or _("Ohne Titel")
    artist = jellydream_album_artist(item)
    clock = jellydream_track_clock(item.get("RunTimeTicks"))

    try:
        number = int(item.get("IndexNumber") or 0)
    except Exception:
        number = 0

    if number <= 0:
        number = int(position)

    if artist:
        label = "%s  ·  %s" % (title, artist)
    else:
        label = title

    return "%3d   %s   %s" % (number, label, clock)


class JellyDreamMusicListScreen(JellyDreamGridScreen):
    SCREEN_NAME = "JellyDreamMusicListScreen"
    GRID_COUNT = 10
    COLUMNS = 5

    def __init__(self, session):
        JellyDreamGridScreen.__init__(self, session)
        self.detail_cache = _GLOBAL_DETAILS_CACHE
        self.all_albums = []
        self.albums = []
        self.genres = ["Alle"]
        self.genreIndex = 0
        self.mode = "grid"
        self.index = 0

        self["title"] = Label(_("Musik"))
        self["title_icon"] = Pixmap()
        self["viewfilter"] = Label("")
        self["genrebar"] = Label("")
        self["movieinfo"] = Label("")
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

        self.onLayoutFinish.append(self.loadMusicAsync)
        self.onLayoutFinish.append(self.applyTitleIcon)

    def applyTitleIcon(self):
        jellydream_apply_title_icon(self)

    def currentGenre(self):
        try:
            return self.genres[self.genreIndex]
        except Exception:
            return "Alle"

    def filterAlbums(self):
        albums = jellydream_filter_by_genre(
            self.all_albums,
            self.currentGenre()
        )
        albums = jellydream_filter_by_played(
            albums,
            jellydream_music_played_filter()
        )
        self.albums = jellydream_apply_letter_filter(
            albums,
            self._letter_filter,
            by_artist=jellydream_music_sort() == "artist"
        )
        self.index = 0
        self.updateList()

    def loadMusicAsync(self):
        if self._async_loading:
            return

        self.albums = []
        self.index = 0
        self["title"].setText(_("Musik"))
        self["viewfilter"].setText(jellydream_music_view_label())
        self["genrebar"].setText("")
        self["movieinfo"].setText("")
        self["status"].setText(_("Musikliste wird von Jellyfin geladen ..."))
        jellydream_render_grid(self, [], 0, items_per_page=self.GRID_COUNT)

        self.runAsyncLoad(
            loader=lambda: jellyfin_get_music(
                2000,
                raise_on_error=True
            ),
            finished_callback=self.musicLoaded,
            message="Music list is loading ..."
        )

    def musicLoaded(self, albums, error_message=""):
        try:
            if error_message:
                self.all_albums = []
                self.albums = []
                self.genres = ["Alle"]
                self["status"].setText(
                    _("Musikliste konnte nicht geladen werden: %s")
                    % error_message
                )
                jellydream_render_grid(self, [], 0, items_per_page=self.GRID_COUNT)
                return

            self.all_albums = list(albums) if isinstance(albums, list) else []
            self.genres = jellydream_build_genres(self.all_albums)
            self.genreIndex = 0
            self.filterAlbums()
        except Exception as e:
            self["status"].setText(_("Musikliste konnte nicht angezeigt werden."))
            jd_log(
                "Loaded music list could not be processed: %s" % e,
                level="WARNING"
            )

    def resetLetterFilter(self):
        jellydream_reset_letter_filter(self, self.filterAlbums)

    def navigateGrid(self, direction):
        if self._async_loading:
            return

        if jellydream_move_alphabet(self, direction):
            return

        if direction == "down":
            next_index = int(self.index) + self.COLUMNS
            page_end = (
                (int(self.index) // self.GRID_COUNT) + 1
            ) * self.GRID_COUNT

            if (
                not self.albums
                or next_index >= len(self.albums)
                or next_index >= page_end
            ):
                jellydream_focus_alphabet(self)
                return

        if (
            direction == "up"
            and (self.index % self.GRID_COUNT) < self.COLUMNS
        ):
            if self.genres:
                self.genreIndex = (self.genreIndex - 1) % len(self.genres)
                self.filterAlbums()
            return

        try:
            self.index = jellydream_handle_navigation(
                self,
                direction,
                self.albums,
                self.index,
                items_per_page=self.GRID_COUNT,
                columns=self.COLUMNS
            )
        except Exception:
            pass

    def updateList(self, *args):
        try:
            if getattr(self, "instance", None):
                self.instance.show()
        except Exception:
            pass

        self["title"].setText(_("Musik"))
        self["viewfilter"].setText(
            jellydream_with_letter_label(
                self,
                jellydream_music_view_label()
            )
        )
        self["genrebar"].setText(
            jellydream_genrebar_text(self.genres, self.genreIndex)
        )
        jellydream_render_grid(
            self,
            self.albums,
            self.index,
            items_per_page=self.GRID_COUNT
        )
        letter_empty = jellydream_finish_letter_choice(
            self,
            bool(self.albums)
        )

        if letter_empty:
            self["movieinfo"].setText("")
        elif self.albums:
            self["status"].setText(jellydream_with_sort_status(
                _(
                    "Genre wechseln: In der obersten Reihe HOCH drücken | "
                    "Album %s / %s | OK = Album öffnen | RUNTER = Buchstabe"
                ) % (self.index + 1, len(self.albums))
            ))
        else:
            self["status"].setText(jellydream_with_sort_status(
                _(
                    "Genre wechseln: In der obersten Reihe HOCH drücken | "
                    "Keine Alben vorhanden | RUNTER = Buchstabe"
                )
            ))

        jellydream_restart_single_timer(
            self,
            "_updateinfo_timer",
            self.updateInfo,
            jellydream_update_refresh_time()
        )

    def updateInfo(self):
        try:
            if not self.albums:
                jellydream_reset_screen_background(self, "movieinfo")
                jellydream_apply_item_logo(self, None)
                return

            album = self.albums[self.index]
            item_id = album.get("Id", "")
            details = self.requestSelectedItemDetails(
                self.albums,
                item_id,
                refresh_callback=self.updateInfo
            )

            if details:
                album = jellydream_merge_item_details(album, details)

            self["movieinfo"].setText("")
            current_id = str(item_id or "")
            jellydream_apply_item_logo(
                self,
                album,
                selection_check=lambda: (
                    bool(self.albums)
                    and 0 <= int(self.index) < len(self.albums)
                    and str(self.albums[self.index].get("Id", "")) == current_id
                )
            )
        except Exception as e:
            jd_log(
                "Music info could not be updated: %s" % e,
                level="ERROR"
            )

    def greenPressed(self):
        if self._async_loading:
            return

        jellydream_open_music_view(
            self,
            self.loadMusicAsync,
            self.filterAlbums
        )

    def yellowPressed(self):
        self.openItemStateMenu(
            jellydream_safe_current_item(self.albums, self.index),
            self.loadMusicAsync
        )

    def okPressed(self):
        if self._async_loading:
            self["status"].setText(_("Bitte warten, die Musikliste wird noch geladen ..."))
            return

        if jellydream_confirm_alphabet(self, self.filterAlbums):
            return

        album = jellydream_safe_current_item(self.albums, self.index)

        if not album:
            jellydream_safe_set_status(
                self,
                _("Kein gültiges Album an der aktuellen Position"),
                level="WARNING"
            )
            return

        item_id = str(album.get("Id") or "").strip()

        if not item_id:
            jellydream_safe_set_status(
                self,
                _("Kein gültiges Album an der aktuellen Position"),
                level="WARNING"
            )
            return

        if str(album.get("Type") or "") == "Audio":
            jellydream_music_queue_clear()
            self.playItemAsync(
                title=album.get("Name") or _("Musik"),
                item_id=item_id,
                runtime_seconds=ticks_to_seconds(album.get("RunTimeTicks")),
                return_screen=self,
                callback=self.loadMusicAsync,
                item=album,
                message=_("Musik wird geladen ...")
            )
            return

        opened, error_message = jellydream_safe_open(
            self.session,
            self.updateList,
            JellyDreamMusicAlbumScreen,
            album
        )

        if not opened:
            self["status"].setText(error_message)


class JellyDreamMusicAlbumScreen(JellyDreamGridScreen):
    SCREEN_NAME = "JellyDreamMusicAlbumScreen"
    GRID_COUNT = 6

    def __init__(self, session, album):
        JellyDreamGridScreen.__init__(self, session)
        self.detail_cache = _GLOBAL_DETAILS_CACHE
        self.album = album if isinstance(album, dict) else {}
        self.album_id = str(self.album.get("Id") or "").strip()
        self.album_name = str(self.album.get("Name") or _("Musik"))
        self.tracks = []
        self.index = 0

        self["title"] = Label(self.album_name)
        self["title_icon"] = Pixmap()
        self["viewfilter"] = Label(jellydream_album_artist(self.album))
        self["genrebar"] = Label(jellydream_item_genre_line(self.album))
        self["movieinfo"] = Label("")
        self["imdb_logo"] = Pixmap()
        self["imdb_rating"] = Label("")
        self["album_cover"] = Pixmap()
        self["shuffle_icon"] = Pixmap()

        for row in range(1, TRACK_ROWS + 1):
            self["track%d" % row] = Label("")

        self["actions"] = ActionMap(
            ["OkCancelActions", "DirectionActions", "ColorActions"],
            {
                "cancel": self.close,
                "ok": self.okPressed,
                "yellow": self.yellowPressed,
                "left": lambda: self.navigateList("left"),
                "right": lambda: self.navigateList("right"),
                "up": lambda: self.navigateList("up"),
                "down": lambda: self.navigateList("down"),
            },
            -1
        )

        self.onLayoutFinish.append(self.loadTracksAsync)
        self.onLayoutFinish.append(self.applyTitleIcon)

    def applyTitleIcon(self):
        jellydream_apply_title_icon(self)

    def loadTracksAsync(self):
        if self._async_loading:
            return

        if not self.album_id:
            self.tracks = []
            self["status"].setText(_("Kein gültiges Album an der aktuellen Position"))
            self.renderTracks()
            return

        self.tracks = []
        self.index = 0
        self["title"].setText(self.album_name)
        self["viewfilter"].setText(jellydream_album_artist(self.album))
        self["status"].setText(_("Album wird geladen ..."))
        self.renderTracks()
        self.showAlbumCover()

        self.runAsyncLoad(
            loader=lambda: jellyfin_get_album_tracks(
                self.album_id,
                raise_on_error=True
            ),
            finished_callback=self.tracksLoaded,
            message="Album tracks are loading ..."
        )

    def tracksLoaded(self, tracks, error_message=""):
        try:
            if error_message:
                self.tracks = []
                self["status"].setText(
                    _("Album konnte nicht geladen werden: %s") % error_message
                )
                self.renderTracks()
                return

            self.tracks = list(tracks) if isinstance(tracks, list) else []
            self.index = 0
            self.updateList()
        except Exception as e:
            self["status"].setText(_("Album konnte nicht geladen werden."))
            jd_log(
                "Loaded album tracks could not be processed: %s" % e,
                level="WARNING"
            )

    def showAlbumCover(self):
        widget = self["album_cover"]
        album_id = str(self.album_id or "").strip()

        if not album_id:
            jellydream_safe_set_pixmap(
                widget,
                "",
                hide_if_missing=True
            )
            return

        width = 600
        height = 600
        cached_path = jellydream_get_cached_image_path(
            album_id,
            width,
            height,
            "Primary"
        )

        if jellydream_valid_file(cached_path, 500):
            jellydream_safe_set_pixmap(
                widget,
                cached_path,
                hide_if_missing=False
            )
            return

        def cover_finished(path, loaded_item_id):
            if not getattr(self, "instance", None):
                return

            if str(loaded_item_id) != album_id:
                return

            if jellydream_valid_file(path, 500):
                jellydream_safe_set_pixmap(
                    self["album_cover"],
                    path,
                    hide_if_missing=False
                )

        jellydream_async_download_image(
            album_id,
            jellydream_cache_dir("cover"),
            width=width,
            height=height,
            image_type="Primary",
            callback=cover_finished
        )

    def entryCount(self):
        if not self.tracks:
            return 0

        return len(self.tracks) + 2

    def currentTrack(self):
        track_index = int(self.index) - 2

        if track_index < 0 or track_index >= len(self.tracks):
            return None

        return self.tracks[track_index]

    def navigateList(self, direction):
        total = self.entryCount()

        if self._async_loading or total <= 0:
            return

        if direction in ("up", "left"):
            if self.index <= 0:
                return

            self.index -= 1
        elif direction in ("down", "right"):
            if self.index >= total - 1:
                return

            self.index += 1
        else:
            return

        self.updateList()

    def renderTracks(self):
        total = self.entryCount()
        page_start = 0

        if total:
            page_start = (int(self.index) // TRACK_ROWS) * TRACK_ROWS

        for row in range(1, TRACK_ROWS + 1):
            widget = self["track%d" % row]
            position = page_start + row - 1
            selected = total and position == int(self.index)
            text = ""
            is_action = False

            if position == 0 and self.tracks:
                text = "\u25b6  %s" % _("Alle abspielen")
                is_action = True
            elif position == 1 and self.tracks:
                text = _("Zufällig")
                is_action = True
            elif position >= 2:
                track_index = position - 2

                if 0 <= track_index < len(self.tracks):
                    text = jellydream_track_line(
                        self.tracks[track_index],
                        track_index + 1
                    )

            if selected and text and not is_action:
                text = "> %s <" % text

            widget.setText(text)

            try:
                if widget.instance:
                    widget.instance.setForegroundColor(
                        parseColor("#f1c40f" if selected else "#ffffff")
                    )
            except Exception:
                pass

        self.placeShuffleIcon()

    def trackOrigin(self, name):
        origins = getattr(self, "_track_origins", None)

        if not isinstance(origins, dict):
            origins = {}
            self._track_origins = origins

        if name in origins:
            return origins[name]

        instance = getattr(self[name], "instance", None)

        if not instance:
            return None

        position = instance.position()
        size = instance.size()
        width = int(size.width())
        height = int(size.height())

        if width < 20 or height < 8:
            return None

        origins[name] = (
            int(position.x()),
            int(position.y()),
            width,
            height
        )
        return origins[name]

    def restoreTrackRow(self, name):
        origin = self.trackOrigin(name)
        instance = getattr(self[name], "instance", None)

        if not origin or not instance:
            return None

        instance.move(ePoint(origin[0], origin[1]))
        instance.resize(eSize(origin[2], origin[3]))
        return origin

    def placeShuffleIcon(self):
        icon = self["shuffle_icon"]
        visible_row = None
        selected = False
        page_start = 0
        total = self.entryCount()

        if total:
            page_start = (int(self.index) // TRACK_ROWS) * TRACK_ROWS

        for row in range(1, TRACK_ROWS + 1):
            name = "track%d" % row
            origin = self.restoreTrackRow(name)
            position = page_start + row - 1

            if position != 1 or not self.tracks or not origin:
                continue

            visible_row = (name, origin)
            selected = position == int(self.index)

        if not visible_row:
            try:
                icon.hide()
            except Exception:
                pass
            return

        name, origin = visible_row
        icon_instance = getattr(icon, "instance", None)
        row_instance = getattr(self[name], "instance", None)

        if not icon_instance or not row_instance:
            return

        icon_size = icon_instance.size()
        icon_w = int(icon_size.width())
        icon_h = int(icon_size.height())
        gap = max(6, origin[3] // 6)
        indent = icon_w + gap
        icon_x = origin[0]
        icon_y = origin[1] + max(0, (origin[3] - icon_h) // 2)
        icon_instance.move(ePoint(icon_x, icon_y))
        row_instance.move(ePoint(origin[0] + indent, origin[1]))
        row_instance.resize(eSize(max(40, origin[2] - indent), origin[3]))
        pixmap = jellydream_menu_icon(
            "shuffle",
            "gold" if selected else "white"
        )

        if pixmap is not None:
            try:
                icon.setPixmap(pixmap)
            except Exception:
                pass

        try:
            icon.show()
        except Exception:
            pass

    def updateList(self, *args):
        try:
            if getattr(self, "instance", None):
                self.instance.show()
        except Exception:
            pass

        self["title"].setText(self.album_name)
        self["viewfilter"].setText(jellydream_album_artist(self.album))
        self["genrebar"].setText(jellydream_item_genre_line(self.album))
        self.renderTracks()

        if self.index == 0 and self.tracks:
            self["status"].setText(_("OK = Alle abspielen"))
        elif self.index == 1 and self.tracks:
            self["status"].setText(_("OK = Zufällig"))
        elif self.currentTrack():
            track_index = int(self.index) - 2
            self["status"].setText(jellydream_with_yellow_status(
                _("Titel %s / %s | OK = Abspielen")
                % (track_index + 1, len(self.tracks))
            ))
        else:
            self["status"].setText(jellydream_with_yellow_status(
                _("Keine Titel vorhanden")
            ))

        jellydream_restart_single_timer(
            self,
            "_updateinfo_timer",
            self.updateInfo,
            jellydream_update_refresh_time()
        )

    def updateInfo(self):
        try:
            track = self.currentTrack()

            if not track:
                if not self.tracks:
                    jellydream_reset_screen_background(self, "movieinfo")
                    jellydream_apply_item_logo(self, None)
                    return

                self.showAlbumSummary()
                return

            song = track
            item_id = song.get("Id", "")
            details = self.requestSelectedItemDetails(
                self.tracks,
                item_id,
                refresh_callback=self.updateInfo
            )

            if details:
                song = jellydream_merge_item_details(song, details)

            title = song.get("Name", _("Ohne Titel"))
            artist = jellydream_album_artist(song) or jellydream_album_artist(self.album) or "-"
            album = str(song.get("Album") or self.album_name or "").strip() or "-"
            year = song.get("ProductionYear") or self.album.get("ProductionYear") or "-"
            genres = jellydream_item_genre_line(song) or jellydream_item_genre_line(self.album)
            minutes = ticks_to_minutes(song.get("RunTimeTicks"))
            runtime_text = (_("%s Min") % minutes) if minutes else _("- Min")
            info_text = _(
                "Titel: %s\nKünstler: %s\nAlbum: %s\n"
                "Jahr: %s   Laufzeit: %s\nGenre: %s"
            ) % (title, artist, album, year, runtime_text, genres)
            overview = jellydream_item_overview(song)

            if overview:
                info_text = "%s\n\n%s" % (
                    info_text,
                    jellydream_wrap(overview, 42, 4)
                )

            self["movieinfo"].setText(info_text.strip())
            current_id = str(item_id or "")

            def same_track():
                current = self.currentTrack()
                return (
                    isinstance(current, dict)
                    and str(current.get("Id") or "") == current_id
                )

            jellydream_apply_item_logo(
                self,
                self.album,
                selection_check=same_track
            )
        except Exception as e:
            jd_log(
                "Album info could not be updated: %s" % e,
                level="ERROR"
            )

    def showAlbumSummary(self):
        title = self.album_name or _("Musik")
        artist = jellydream_album_artist(self.album) or "-"
        year = self.album.get("ProductionYear") or "-"
        genres = jellydream_item_genre_line(self.album)
        minutes = ticks_to_minutes(self.album.get("RunTimeTicks"))
        runtime_text = (_("%s Min") % minutes) if minutes else _("- Min")
        self["movieinfo"].setText(_(
            "Album: %s\nKünstler: %s\nJahr: %s   Laufzeit: %s\nGenre: %s"
        ) % (title, artist, year, runtime_text, genres))
        jellydream_apply_item_logo(
            self,
            self.album,
            selection_check=lambda: self.currentTrack() is None and bool(self.tracks)
        )

    def yellowPressed(self):
        track = self.currentTrack()

        if not track:
            return

        self.openItemStateMenu(
            track,
            self.loadTracksAsync
        )

    def playbackClosed(self, result=None):
        if jellydream_music_queue_has_items():
            return

        if not getattr(self, "instance", None):
            return

        self.loadTracksAsync()

    def playVisibleSongs(self, shuffled=False):
        if self._async_loading:
            return

        rows = [
            item
            for item in self.tracks
            if isinstance(item, dict) and str(item.get("Id") or "").strip()
        ]

        if shuffled:
            rows = list(rows)
            random.shuffle(rows)

        if not rows:
            jellydream_safe_set_status(
                self,
                _("Keine Titel vorhanden"),
                level="WARNING"
            )
            return

        jellydream_music_queue_set(rows[1:])
        song = rows[0]
        started = self._start_playback(
            title=song.get("Name") or self.album_name,
            item_id=str(song.get("Id") or "").strip(),
            runtime_seconds=ticks_to_seconds(song.get("RunTimeTicks")),
            return_screen=self,
            callback=self.playbackClosed,
            start_position_seconds=0,
            audio_stream_index=None,
            message=_("Musik wird geladen ...")
        )

        if not started:
            jellydream_music_queue_clear()

    def okPressed(self):
        if self._async_loading:
            self["status"].setText(_("Bitte warten, das Album wird noch geladen ..."))
            return

        if self.index == 0 and self.tracks:
            self.playVisibleSongs(shuffled=False)
            return

        if self.index == 1 and self.tracks:
            self.playVisibleSongs(shuffled=True)
            return

        song = self.currentTrack()

        if not song:
            jellydream_safe_set_status(
                self,
                _("Kein Titel an der aktuellen Position"),
                level="WARNING"
            )
            return

        item_id = str(song.get("Id") or "").strip()

        if not item_id:
            jellydream_safe_set_status(
                self,
                _("Kein Titel an der aktuellen Position"),
                level="WARNING"
            )
            return

        jellydream_music_queue_clear()
        self.playItemAsync(
            title=song.get("Name") or self.album_name,
            item_id=item_id,
            runtime_seconds=ticks_to_seconds(song.get("RunTimeTicks")),
            return_screen=self,
            callback=self.playbackClosed,
            item=song,
            message=_("Musik wird geladen ...")
        )
