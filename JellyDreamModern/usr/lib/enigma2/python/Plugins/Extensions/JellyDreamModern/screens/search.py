# -*- coding: utf-8 -*-

import os

from Components.ActionMap import ActionMap
from Components.Label import Label
from Components.Pixmap import Pixmap
from Tools.Directories import resolveFilename, SCOPE_PLUGINS
from Screens.VirtualKeyBoard import VirtualKeyBoard
from enigma import eTimer
from skin import parseColor

from .. import _

from ..constants import ITEMS_PER_PAGE_GRID
from ..log import jd_log
from ..config import (
    add_search_history,
    jellydream_update_refresh_time,
    read_search_history
)
from ..images import (
    jellydream_apply_item_backdrop,
    jellydream_apply_item_logo,
    set_jellydream_background
)
from ..uiutil import (
    jellydream_handle_navigation,
    jellydream_restart_single_timer,
    jellydream_safe_current_item,
    jellydream_with_yellow_status,
    jellydream_safe_open,
    jellydream_safe_set_status,
    jellydream_wrap,
    ticks_to_minutes,
    ticks_to_seconds
)
from ..library import (
    jellydream_item_genre_line,
    jellydream_item_overview,
    jellydream_merge_item_details,
    jellyfin_search_items,
    _GLOBAL_DETAILS_CACHE
)
from ..ui import (
    JellyDreamGridScreen,
    jellydream_apply_title_icon,
    jellydream_render_grid
)
from .seasons import JellyDreamSeasonListScreen
from .boxsets import JellyDreamBoxSetMoviesScreen


class JellyDreamSearchScreen(JellyDreamGridScreen):
    SCREEN_NAME = "JellyDreamSearchScreen"
    GRID_COUNT = 6

    def __init__(self, session):
        JellyDreamGridScreen.__init__(self, session)
        self.detail_cache = _GLOBAL_DETAILS_CACHE

        self.query = ""
        self.results = []
        self.history = read_search_history() or []
        self.mode = "history"
        self.historyIndex = 0

        self._search_request_token = 0

        self["search_icon"] = Pixmap()
        self["title"] = Label(_("Suche"))
        self["title_icon"] = Pixmap()
        self["hint"] = Label("")
        self["info"] = Label("")

        self.keyboardTimer = eTimer()
        try:
            self.keyboardTimer.callback.append(self.startSearchLogic)
        except Exception:
            try:
                self.keyboardTimer.timeout.get().append(self.startSearchLogic)
            except Exception:
                pass

        self["actions"] = ActionMap(
            ["OkCancelActions", "DirectionActions", "ColorActions"],
            {
                "cancel": self.close,
                "ok": self.okPressed,
                "yellow": self.yellowPressed,
                "left": lambda: self.navigateSearch("left"),
                "right": lambda: self.navigateSearch("right"),
                "up": lambda: self.navigateSearch("up"),
                "down": lambda: self.navigateSearch("down"),
            },
            -1
        )

        self.onLayoutFinish.append(self.startKeyboardTimer)
        self.onLayoutFinish.append(self.applyTitleIcon)

    def applyTitleIcon(self):
        jellydream_apply_title_icon(self)
    
    def loadBackground(self):
        super().loadBackground()

        try:
            icon_path = resolveFilename(SCOPE_PLUGINS, "Extensions/JellyDreamModern/images/search.png")
            if not os.path.exists(icon_path):
                icon_path = resolveFilename(SCOPE_PLUGINS, "Extensions/JellyDream/images/search.png")
        
            if "search_icon" in self and self["search_icon"].instance and os.path.exists(icon_path):
                self["search_icon"].instance.setPixmapFromFile(icon_path)
                self["search_icon"].instance.show()
        except Exception as e:
            try:
                jd_log(
                    "Search icon could not be loaded: %s" % e,
                    level="WARNING"
                )
            except Exception:
                pass

    def startKeyboardTimer(self):
        try: self.keyboardTimer.start(300, True)
        except Exception: pass

    def startSearchLogic(self):
        self.historyIndex = 0
        self.query = ""
        self.mode = "history"
        self.history = read_search_history() or []
        self.triggerLiveSearch()

    def openKeyboard(self):
        try:
            self.session.openWithCallback(self.keyboardDone, VirtualKeyBoard, title=_("Filme / Serien suchen"), text=self.query or "")
        except Exception as e:
            self["status"].setText(_("Tastaturfehler: %s") % e)

    def triggerLiveSearch(self):
        query = str(self.query or "").strip()

        # Jede Änderung des Suchbegriffs bzw. Verlaufs macht ältere
        # Suchergebnisse ungültig.
        self._search_request_token += 1
        request_token = self._search_request_token

        if not query:
            # Auch eine leere Suche muss eine alte laufende Suche ungültig
            # machen, damit deren Ergebnis nicht später wieder erscheint.
            self._async_load_token += 1
            self._async_loading = False
            self.stopSpinner()

            self.results = []
            self.mode = "history"
            self.index = 0
            self.updateList()
            return

        # Wichtig: Keine Rückgabe bei self._async_loading.
        # Eine neue Suche darf eine noch laufende alte Suche überholen.
        self.runAsyncLoad(
            loader=lambda current_query=query: jellyfin_search_items(
                current_query,
                100
            ),
            finished_callback=lambda result, error_message: (
                self.searchFinished(
                    result,
                    error_message,
                    request_token,
                    query
                )
            ),
            message="Suche läuft ..."
        )

    def searchFinished(
        self,
        result,
        error_message="",
        request_token=None,
        requested_query=""
    ):
        # Ein Resultat darf ausschließlich übernommen werden, wenn es zur
        # letzten gestarteten Suche und zum aktuell sichtbaren Suchbegriff
        # gehört.
        if request_token != self._search_request_token:
            jd_log(
                "Stale search result discarded: token=%s, current=%s, query='%s'"
                % (
                    request_token,
                    self._search_request_token,
                    requested_query
                ),
                level="DEBUG"
            )
            return

        current_query = str(self.query or "").strip()

        if str(requested_query or "").strip() != current_query:
            jd_log(
                "Stale search result discarded because the query changed: '%s' != '%s'"
                % (
                    requested_query,
                    current_query
                ),
                level="DEBUG"
            )
            return

        if error_message:
            self.results = []
            self.index = 0

            self["status"].setText(
                _("Suche fehlgeschlagen: %s") % error_message
            )

            self.updateList()
            return

        self.results = (
            list(result)
            if isinstance(result, list)
            else []
        )

        self.index = 0
        self.mode = "grid"
        self.updateList()

    def keyboardDone(self, text):
        text = (text or "").strip()
        if not text:
            self.mode = "history"
            self.historyIndex = 0
            self.query = ""
            self.history = read_search_history() or []
            self.triggerLiveSearch()
            return
            
        self.query = text
        add_search_history(text)
        self.history = read_search_history() or []

        self.mode = "grid"
        self.historyIndex = 1  
        
        self.triggerLiveSearch()

    def navigateSearch(self, direction):
        if self.mode == "history":
            if direction in ("left", "right") and self.history:
                try:
                    total_slots = len(self.history) + 1
                    if direction == "left":
                        self.historyIndex = (self.historyIndex - 1) % total_slots
                    else:
                        self.historyIndex = (self.historyIndex + 1) % total_slots
                        
                    if self.historyIndex == 0:
                        self.query = ""
                    else:
                        self.query = self.history[self.historyIndex - 1]
                        
                    self.triggerLiveSearch()
                except Exception as e:
                    jd_log(
                        "Error while changing the search history: %s" % e,
                        level="WARNING"
                    )
                    return
                
            if direction == "down" and self.results and self.historyIndex != 0:
                self.mode = "grid"
                self.index = 0
                self.updateList()
                return

        elif self.mode == "grid":
            if direction == "up" and self.index < 3:
                self.mode = "history"
                self.updateList()
                return

            try:
                self.index = jellydream_handle_navigation(self, direction, self.results, self.index, items_per_page=ITEMS_PER_PAGE_GRID)
            except Exception:
                pass

    def itemTypeName(self, item):
        item_type = item.get("Type") or ""
        if item_type == "Movie": return "Film"
        if item_type == "Series": return "Serie"
        if item_type == "BoxSet": return _("Sammlung")
        return "Treffer"

    def updateInfo(self):
        try:
            if self.historyIndex == 0:
                self["info"].setText(_("Tippe ein Wort ein, um deine Jellyfin-Mediathek blitzschnell nach Filmen, Serien oder Sammlungen zu durchsuchen."))
                set_jellydream_background(self)
                return
                
            if not self.results:
                self["info"].setText(_("Keine passenden Einträge in deiner Mediathek gefunden."))
                set_jellydream_background(self)
                return

            item = self.results[self.index]
            item_id = item.get("Id", "")
            
            details = self.requestSelectedItemDetails(
                self.results,
                item_id,
                refresh_callback=self.updateInfo
            )

            if details:
                item = jellydream_merge_item_details(
                    item,
                    details
                )

            title = item.get("Name", "Ohne Titel")
            year = item.get("ProductionYear") or "-"
            genres = jellydream_item_genre_line(item)

            minutes = ticks_to_minutes(item.get("RunTimeTicks"))
            runtime_text = f"{minutes} Min" if minutes else ""

            rating = str(item.get("OfficialRating") or "").strip()

            label_type = self.itemTypeName(item)

            meta_text = _("Jahr: %s") % year

            if runtime_text:
                meta_text += "   " + (_("Laufzeit: %s") % runtime_text)

            if rating:
                meta_text += f"   {rating}"

            info_text = (
                f"{label_type}: {title}\n"
                f"{meta_text}\n"
                f"Genre: {genres}"
            )
            
            overview = jellydream_item_overview(item)
            if overview:
                wrapped_overview = jellydream_wrap(overview, 110, 4)
                info_text = f"{info_text}\n\n{wrapped_overview}"
                
            self["info"].setText(info_text.strip())

            media_type = item.get("Type", "")
            current_item_id = str(item_id or "")
            target_id = str((item.get("SeriesId") or item_id) if media_type == "Episode" else item_id or "")

            jellydream_apply_item_backdrop(
                self,
                target_id=target_id,
                selection_check=lambda: (
                    bool(self.results)
                    and 0 <= int(self.index) < len(self.results)
                    and str(self.results[self.index].get("Id", "")) == current_item_id
                )
            )
            jellydream_apply_item_logo(
                self,
                item,
                fallback_id=(
                    item.get("SeriesId")
                    if media_type == "Episode"
                    else ""
                ),
                selection_check=lambda: (
                    bool(self.results)
                    and 0 <= int(self.index) < len(self.results)
                    and str(self.results[self.index].get("Id", "")) == current_item_id
                )
            )

        except Exception:
            pass

    def updateList(self, *args):
        self["title"].setText(_("Suche"))

        virtual_history = [_("Neue Suche...")] + (self.history or [])
        count = len(virtual_history)

        history_color = "\\c00f1c40f" if self.mode == "history" else "\\c00ffffff"
        
        if count == 1:
            self["hint"].setText("%s     %s[ %s ]%s     " % ("\\c00ffffff", history_color, _("Neue Suche..."), "\\c00ffffff"))
        else:
            prev_term = virtual_history[(self.historyIndex - 1) % count]
            current_term = virtual_history[self.historyIndex]
            next_term = virtual_history[(self.historyIndex + 1) % count]

            if self.historyIndex == 0:
                self["hint"].setText(f"\\c00ffffff      {prev_term}     {history_color}[ {current_term} ]\\c00ffffff     {next_term}  >")
            elif self.historyIndex == count - 1:
                self["hint"].setText(f"\\c00ffffff<  {prev_term}     {history_color}[ {current_term} ]\\c00ffffff     {next_term}      ")
            else:
                self["hint"].setText(f"\\c00ffffff<  {prev_term}     {history_color}[ {current_term} ]\\c00ffffff     {next_term}  >")

        if self.historyIndex == 0:
            for i in range(1, 7):
                if self[f"grid_poster{i}"].instance: self[f"grid_poster{i}"].instance.hide()
                self[f"grid_title{i}"].setText("")

            if self["search_icon"].instance:
                self["search_icon"].instance.show()
                
            self["status"].setText(_("OK = Neue Suche starten | LINKS/RECHTS = Alten Verlauf durchblättern"))
            self["info"].setText(_("Tippe ein Wort ein, um deine Jellyfin-Mediathek blitzschnell nach Filmen, Serien oder Sammlungen zu durchsuchen."))
            set_jellydream_background(self)
            return

        if not self.results:
            for i in range(1, 7):
                if self[f"grid_poster{i}"].instance: self[f"grid_poster{i}"].instance.hide()
                self[f"grid_title{i}"].setText("")

            if self["search_icon"].instance:
                self["search_icon"].instance.show()
                
            self["status"].setText(_("Keine Treffer für '%s' | LINKS/RECHTS = Verlauf wechseln") % self.query)
            self.updateInfo()
            return

        if self["search_icon"].instance:
            self["search_icon"].instance.hide()

        jellydream_render_grid(self, self.results, self.index, items_per_page=ITEMS_PER_PAGE_GRID, is_search=True)

        if self.mode == "history":
            for i in range(1, 7):
                title_widget = self[f"grid_title{i}"]
                if title_widget.instance:
                    try:
                        title_widget.instance.setForegroundColor(parseColor("#ffffff"))
                    except Exception: pass

        if self.mode == "history":
            self["status"].setText(_("LINKS/RECHTS = Verlauf blättern | RUNTER = In die Film-Auswahl wechseln"))
        else:
            self["status"].setText(jellydream_with_yellow_status(
                _("Eintrag %s / %s | Steuerkreuz = Navigieren | HOCH am Anfang = Zum Verlauf") % (self.index + 1, len(self.results))
            ))
            
        # Suchdetails und Backdrop nicht sofort bei jeder Navigation laden.
        jellydream_restart_single_timer(
            self,
            "_updateinfo_timer",
            self.updateInfo,
            jellydream_update_refresh_time()
        )

    def yellowPressed(self):
        if self.mode != "grid":
            return

        self.openItemStateMenu(
            jellydream_safe_current_item(self.results, self.index),
            self.updateList
        )

    def okPressed(self):
        if self.historyIndex == 0:
            self.openKeyboard()
            return

        if self.mode == "history":
            if self.results:
                self.mode = "grid"
                self.index = 0
                self.updateList()
            return

        if not self.results:
            self.openKeyboard()
            return

        item = jellydream_safe_current_item(self.results, self.index)
        if not item:
            jellydream_safe_set_status(self, _("Kein gültiger Suchtreffer ausgewählt"), level="WARNING")
            return

        item_type = item.get("Type")
        item_id = str(item.get("Id", "")).strip()
        title = item.get("Name", "Jellyfin Eintrag")

        if not item_id:
            jellydream_safe_set_status(self, _("Fehler: Treffer-ID fehlt"), level="WARNING")
            return

        if item_type == "Movie":
            runtime = ticks_to_seconds(
                item.get("RunTimeTicks")
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
                    item=item
                )

            self.resolveBoxsetAsync(
                item_id,
                title,
                item_type,
                boxset_resolved
            )
            return

        elif item_type == "Series":
            ok, err = jellydream_safe_open(
                self.session,
                self.updateList,
                JellyDreamSeasonListScreen,
                item_id,
                title
            )
            if not ok:
                jellydream_safe_set_status(self, err, level="ERROR")

        elif item_type == "BoxSet":
            ok, err = jellydream_safe_open(
                self.session,
                self.updateList,
                JellyDreamBoxSetMoviesScreen,
                item_id,
                title
            )
            if not ok:
                jellydream_safe_set_status(self, err, level="ERROR")
        else:
            jellydream_safe_set_status(self, _("Unbekannter Treffertyp: %s") % item_type, level="WARNING")
