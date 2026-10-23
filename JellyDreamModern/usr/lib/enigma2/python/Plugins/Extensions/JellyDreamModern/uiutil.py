# -*- coding: utf-8 -*-

import os
import re

from enigma import eTimer

from . import _

from .constants import ITEMS_PER_PAGE_EPISODES, PLUGIN_DIR, TICKS_PER_SECOND
from .log import jd_log
from .workers import jellydream_dispatch_to_gui, jellydream_is_gui_thread

        
_STATUS_COLOR_RESET = "\\c00d8dde5"
_ARROW_LEFT = "◀"
_ARROW_RIGHT = "▶"
_ARROW_UP = "▲"
_ARROW_DOWN = "▼"
_ARROWS_HORIZONTAL = "%s %s" % (_ARROW_LEFT, _ARROW_RIGHT)
_ARROWS_VERTICAL = "%s %s" % (_ARROW_UP, _ARROW_DOWN)
_ARROWS_DPAD = "%s %s" % (_ARROWS_HORIZONTAL, _ARROWS_VERTICAL)


def jellydream_episode_service_title(
    series_name="",
    season_number=None,
    episode_number=None,
    episode_name=""
):
    """
    Name, den Enigma2 für eine Folge anzeigt.

    Format: Serienname - S01E13 - Episodentitel
    """
    series_name = str(series_name or "").strip()
    episode_name = str(episode_name or "").strip() or _("Folge")
    episode_code = ""

    try:
        if season_number is not None and episode_number is not None:
            episode_code = "S%02dE%02d" % (
                int(season_number),
                int(episode_number)
            )
    except Exception:
        episode_code = ""

    if series_name and episode_code:
        return "%s - %s - %s" % (
            series_name,
            episode_code,
            episode_name
        )

    if episode_code:
        return "%s - %s" % (episode_code, episode_name)

    if series_name:
        return "%s - %s" % (series_name, episode_name)

    return episode_name


def jellydream_item_enigma_names(item, fallback_title=""):
    """
    Aufteilung für die Enigma2-Infobar.

    Sendername: Serienname
    Sendung: S01E13 - Episodentitel
    """
    item = item if isinstance(item, dict) else {}
    fallback_title = str(
        fallback_title
        or item.get("Name")
        or ""
    ).strip()
    item_type = str(item.get("Type") or "").strip()

    if item_type != "Episode":
        return fallback_title, fallback_title

    series_name = str(item.get("SeriesName") or "").strip()
    episode_name = str(item.get("Name") or fallback_title).strip()
    event_name = jellydream_episode_service_title(
        season_number=item.get("ParentIndexNumber"),
        episode_number=item.get("IndexNumber"),
        episode_name=episode_name
    )
    service_name = series_name or event_name or fallback_title
    return service_name, event_name or fallback_title


def _jellydream_status_box(rgb):
    return "\\c00%s■%s" % (rgb, _STATUS_COLOR_RESET)


_STATUS_YELLOW = _jellydream_status_box("f1c40f")
_STATUS_GREEN = _jellydream_status_box("39d353")
_STATUS_BLUE = _jellydream_status_box("4aa3ff")
_STATUS_RED = _jellydream_status_box("ff4d4d")

# Längere Begriffe zuerst, damit LINKS/RECHTS nicht zu zwei einzelnen Pfeilen
# und BLUE nicht zu BLU wird. Die Wörter stammen aus den zehn UI-Sprachen.
_STATUS_SYMBOLS = (
    ("Yön tuşları", _ARROWS_DPAD),
    ("Tasti freccia", _ARROWS_DPAD),
    ("Steuerkreuz", _ARROWS_DPAD),
    ("LINKS/RECHTS", _ARROWS_HORIZONTAL),
    ("LEFT/RIGHT", _ARROWS_HORIZONTAL),
    ("LEWO/PRAWO", _ARROWS_HORIZONTAL),
    ("IZQUIERDA/DERECHA", _ARROWS_HORIZONTAL),
    ("SINISTRA/DESTRA", _ARROWS_HORIZONTAL),
    ("GAUCHE/DROITE", _ARROWS_HORIZONTAL),
    ("SOL/SAĞ", _ARROWS_HORIZONTAL),
    ("ВЛЕВО/ВПРАВО", _ARROWS_HORIZONTAL),
    ("يسار/يمين", _ARROWS_HORIZONTAL),
    ("HOCH/RUNTER", _ARROWS_VERTICAL),
    ("UP/DOWN", _ARROWS_VERTICAL),
    ("OMHOOG/OMLAAG", _ARROWS_VERTICAL),
    ("GÓRA/DÓŁ", _ARROWS_VERTICAL),
    ("ARRIBA/ABAJO", _ARROWS_VERTICAL),
    ("SU/GIÙ", _ARROWS_VERTICAL),
    ("HAUT/BAS", _ARROWS_VERTICAL),
    ("YUKARI/AŞAĞI", _ARROWS_VERTICAL),
    ("ВВЕРХ/ВНИЗ", _ARROWS_VERTICAL),
    ("أعلى/أسفل", _ARROWS_VERTICAL),
    ("Pijltjes", _ARROWS_DPAD),
    ("Krzyżak", _ARROWS_DPAD),
    ("Cruceta", _ARROWS_DPAD),
    ("D-pad", _ARROWS_DPAD),
    ("الأسهم", _ARROWS_DPAD),
    ("Крест", _ARROWS_DPAD),
    ("Croix", _ARROWS_DPAD),
    ("IZQUIERDA", _ARROW_LEFT),
    ("SINISTRA", _ARROW_LEFT),
    ("GAUCHE", _ARROW_LEFT),
    ("NIEBIESKI", _STATUS_BLUE),
    ("AMARILLO", _STATUS_YELLOW),
    ("ZIELONY", _STATUS_GREEN),
    ("CZERWONY", _STATUS_RED),
    ("OMHOOG", _ARROW_UP),
    ("OMLAAG", _ARROW_DOWN),
    ("RECHTS", _ARROW_RIGHT),
    ("LINKS", _ARROW_LEFT),
    ("YELLOW", _STATUS_YELLOW),
    ("GREEN", _STATUS_GREEN),
    ("RIGHT", _ARROW_RIGHT),
    ("BLAUW", _STATUS_BLUE),
    ("DERECHA", _ARROW_RIGHT),
    ("DESTRA", _ARROW_RIGHT),
    ("DROITE", _ARROW_RIGHT),
    ("ARRIBA", _ARROW_UP),
    ("ABAJO", _ARROW_DOWN),
    ("YUKARI", _ARROW_UP),
    ("AŞAĞI", _ARROW_DOWN),
    ("ВЛЕВО", _ARROW_LEFT),
    ("ВПРАВО", _ARROW_RIGHT),
    ("ВВЕРХ", _ARROW_UP),
    ("ВНИЗ", _ARROW_DOWN),
    ("ЖЕЛТАЯ", _STATUS_YELLOW),
    ("ЗЕЛЕНАЯ", _STATUS_GREEN),
    ("КРАСНАЯ", _STATUS_RED),
    ("СИНИЙ", _STATUS_BLUE),
    ("GIALLO", _STATUS_YELLOW),
    ("ROSSO", _STATUS_RED),
    ("ROUGE", _STATUS_RED),
    ("JAUNE", _STATUS_YELLOW),
    ("VERDE", _STATUS_GREEN),
    ("AZUL", _STATUS_BLUE),
    ("ROJO", _STATUS_RED),
    ("GROEN", _STATUS_GREEN),
    ("GEEL", _STATUS_YELLOW),
    ("ROOD", _STATUS_RED),
    ("YEŞİL", _STATUS_GREEN),
    ("KIRMIZI", _STATUS_RED),
    ("MAVİ", _STATUS_BLUE),
    ("SARI", _STATUS_YELLOW),
    ("ŻÓŁTY", _STATUS_YELLOW),
    ("GÓRA", _ARROW_UP),
    ("PRAWO", _ARROW_RIGHT),
    ("LEWO", _ARROW_LEFT),
    ("HAUT", _ARROW_UP),
    ("BLEU", _STATUS_BLUE),
    ("VERT", _STATUS_GREEN),
    ("BLUE", _STATUS_BLUE),
    ("LEFT", _ARROW_LEFT),
    ("DOWN", _ARROW_DOWN),
    ("RUNTER", _ARROW_DOWN),
    ("HOCH", _ARROW_UP),
    ("GELB", _STATUS_YELLOW),
    ("GRÜN", _STATUS_GREEN),
    ("BLAU", _STATUS_BLUE),
    ("ROT", _STATUS_RED),
    ("SOL", _ARROW_LEFT),
    ("SAĞ", _ARROW_RIGHT),
    ("BLU", _STATUS_BLUE),
    ("RED", _STATUS_RED),
    ("DÓŁ", _ARROW_DOWN),
    ("GIÙ", _ARROW_DOWN),
    ("BAS", _ARROW_DOWN),
    ("UP", _ARROW_UP),
    ("SU", _ARROW_UP),
    ("يسار", _ARROW_LEFT),
    ("يمين", _ARROW_RIGHT),
    ("أعلى", _ARROW_UP),
    ("أسفل", _ARROW_DOWN),
    ("أصفر", _STATUS_YELLOW),
    ("أخضر", _STATUS_GREEN),
    ("أزرق", _STATUS_BLUE),
    ("أحمر", _STATUS_RED),
)

_STATUS_SYMBOL_PATTERN = re.compile(
    "(?<![\\w-])(%s)(?![\\w-])" % "|".join(
        re.escape(token)
        for token, _symbol in sorted(
            _STATUS_SYMBOLS,
            key=lambda item: len(item[0]),
            reverse=True
        )
    )
)
_STATUS_SYMBOL_MAP = dict(_STATUS_SYMBOLS)


def jellydream_status_symbols(text):
    """
    Ersetzt Tastenwörter in der Statuszeile durch Pfeile und farbige Kästchen.
    OK, EXIT und BACK bleiben Text. Die Ersetzung gilt für alle UI-Sprachen.
    """
    if text is None:
        return ""

    return _STATUS_SYMBOL_PATTERN.sub(
        lambda match: _STATUS_SYMBOL_MAP[match.group(1)],
        str(text)
    )


_STATUS_FONT_NAME = "JDStatus"
_STATUS_FONT_READY = False


def jellydream_ensure_status_font():
    """
    Statusschrift auf Basis von DejaVu. Pfeile und Kästchen sind darin
    etwas größer als die Buchstaben, der übrige Text bleibt gleich.
    """
    global _STATUS_FONT_READY

    if _STATUS_FONT_READY:
        return True

    try:
        from enigma import addFont

        font_path = os.path.join(PLUGIN_DIR, "fonts", "JDStatus.ttf")

        if not os.path.isfile(font_path):
            font_path = "/usr/share/fonts/DejaVuSans.ttf"

        # Gleiche Skalierung wie die Skin-Schrift Regular (95).
        addFont(
            font_path,
            _STATUS_FONT_NAME,
            95,
            0
        )
        _STATUS_FONT_READY = True
    except Exception as e:
        jd_log(
            "Status symbol font could not be registered: %s" % e,
            level="WARNING"
        )

    return _STATUS_FONT_READY


def _jellydream_status_font_size(screen):
    skin = getattr(screen, "skin", None) or ""
    tag = re.search(
        r'<widget\b[^>]*\bname="status"[^>]*>',
        skin
    )

    if not tag:
        return 0

    font = re.search(
        r'\bfont="[^"]*;(\d+)"',
        tag.group(0)
    )

    if not font:
        return 0

    try:
        return int(font.group(1))
    except Exception:
        return 0


def jellydream_apply_status_symbol_font(screen):
    if not jellydream_ensure_status_font():
        return

    try:
        label = screen["status"]
    except Exception:
        return

    size = _jellydream_status_font_size(screen)

    if size <= 0 or getattr(label, "instance", None) is None:
        return

    try:
        from enigma import gFont

        label.instance.setFont(gFont(_STATUS_FONT_NAME, size))
    except Exception as e:
        jd_log(
            "Status symbol font could not be applied: %s" % e,
            level="WARNING"
        )
        return

    try:
        label.setText(label.getText())
    except Exception:
        pass


def jellydream_bind_status_symbols(label):
    """Leitet künftige setText-Aufrufe des Status-Labels durch die Symbole."""
    if label is None or getattr(label, "_jellydream_status_symbols", False):
        return

    original_set = label.setText
    original_get = getattr(label, "getText", None)
    state = {"raw": None}

    def set_text(text):
        state["raw"] = text
        original_set(jellydream_status_symbols(text))

    def get_text():
        if state["raw"] is not None:
            return state["raw"]
        if original_get is not None:
            return original_get()
        return ""

    label.setText = set_text
    if original_get is not None:
        label.getText = get_text
    label._jellydream_status_symbols = True


def jellydream_with_yellow_status(text):
    return "%s   %s" % (
        text,
        _("GELB = Wiedergabestatus")
    )


def jellydream_view_filter_label(sort_mode=None):
    from .config import (
        jellydream_library_played_filter,
        jellydream_library_sort
    )

    if sort_mode not in ("name", "added"):
        sort_mode = jellydream_library_sort()

    if sort_mode == "added":
        sort_label = _("Zuletzt hinzugefügt")
    else:
        sort_label = _("Alphabetisch")

    played_filter = jellydream_library_played_filter()

    if played_filter == "unplayed":
        played_label = _("Ungesehene")
    elif played_filter == "played":
        played_label = _("Gesehene")
    else:
        played_label = _("Alle")

    return "%s   ·   %s" % (sort_label, played_label)


def jellydream_with_sort_status(text):
    return jellydream_with_yellow_status(
        "%s   %s" % (
            text,
            _("GRÜN = Ansicht")
        )
    )


def jellydream_open_library_sort(screen, reload_callback, filter_callback=None):
    from Screens.ChoiceBox import ChoiceBox

    from .config import (
        jellydream_library_played_filter,
        jellydream_library_sort,
        jellydream_set_library_played_filter,
        jellydream_set_library_sort
    )

    current_sort = jellydream_library_sort()

    if hasattr(screen, "sortMode"):
        try:
            screen_sort = screen.sortMode()
        except Exception:
            screen_sort = None

        if screen_sort in ("name", "added"):
            current_sort = screen_sort
    current_filter = jellydream_library_played_filter()
    entries = [
        (_("Alle"), "all"),
        (_("Ungesehene"), "unplayed"),
        (_("Gesehene"), "played"),
        (_("Alphabetisch"), "name"),
        (_("Zuletzt hinzugefügt"), "added"),
        (_("Filter zurücksetzen"), "reset_letter")
    ]

    def chosen(answer):
        if answer is None:
            return

        try:
            mode = answer[1]
        except Exception:
            return

        if _jellydream_menu_reset_letter(screen, mode):
            return

        if mode in ("name", "added"):
            if mode == current_sort:
                return

            jellydream_set_library_sort(mode)

            if callable(reload_callback):
                reload_callback()

            return

        if mode not in ("all", "unplayed", "played") or mode == current_filter:
            return

        jellydream_set_library_played_filter(mode)

        if callable(filter_callback):
            filter_callback()
        elif callable(reload_callback):
            reload_callback()

    jellydream_safe_open(
        screen.session,
        chosen,
        ChoiceBox,
        title=_("Ansicht"),
        list=entries
    )


def jellydream_music_view_label():
    from .config import (
        jellydream_music_played_filter,
        jellydream_music_sort
    )

    sort_mode = jellydream_music_sort()

    if sort_mode == "added":
        sort_label = _("Zuletzt hinzugefügt")
    elif sort_mode == "artist":
        sort_label = _("Künstler")
    else:
        sort_label = _("Alphabetisch")

    played_filter = jellydream_music_played_filter()

    if played_filter == "unplayed":
        played_label = _("Ungehörte")
    elif played_filter == "played":
        played_label = _("Gehörte")
    else:
        played_label = _("Alle")

    return "%s   ·   %s" % (sort_label, played_label)


def jellydream_open_music_view(screen, reload_callback, filter_callback=None):
    from Screens.ChoiceBox import ChoiceBox

    from .config import (
        jellydream_music_played_filter,
        jellydream_music_sort,
        jellydream_set_music_played_filter,
        jellydream_set_music_sort
    )

    current_sort = jellydream_music_sort()
    current_filter = jellydream_music_played_filter()
    entries = [
        (_("Alle"), "all"),
        (_("Ungehörte"), "unplayed"),
        (_("Gehörte"), "played"),
        (_("Alphabetisch"), "name"),
        (_("Zuletzt hinzugefügt"), "added"),
        (_("Künstler"), "artist"),
        (_("Filter zurücksetzen"), "reset_letter")
    ]

    def chosen(answer):
        if answer is None:
            return

        try:
            mode = answer[1]
        except Exception:
            return

        if _jellydream_menu_reset_letter(screen, mode):
            return

        if mode in ("name", "added", "artist"):
            if mode == current_sort:
                return

            jellydream_set_music_sort(mode)

            if callable(reload_callback):
                reload_callback()

            return

        if mode not in ("all", "unplayed", "played") or mode == current_filter:
            return

        jellydream_set_music_played_filter(mode)

        if callable(filter_callback):
            filter_callback()
        elif callable(reload_callback):
            reload_callback()

    jellydream_safe_open(
        screen.session,
        chosen,
        ChoiceBox,
        title=_("Ansicht"),
        list=entries
    )


JELLYDREAM_ALPHABET = ["#"] + [
    chr(code)
    for code in range(ord("A"), ord("Z") + 1)
]


def jellydream_letter_of_name(name):
    text = str(name or "").strip().upper()

    if not text:
        return "#"

    letter = text[0]

    if letter == "Ä":
        return "A"

    if letter == "Ö":
        return "O"

    if letter == "Ü":
        return "U"

    if "A" <= letter <= "Z":
        return letter

    return "#"


def jellydream_letter_item_name(item, by_artist=False):
    if not isinstance(item, dict):
        return ""

    if by_artist:
        artist = item.get("AlbumArtist")

        if isinstance(artist, list):
            artist = artist[0] if artist else ""

        artist = str(artist or "").strip()

        if not artist:
            artists = item.get("Artists") or item.get("AlbumArtists") or []

            if isinstance(artists, list):
                artist = artists[0] if artists else ""
            else:
                artist = artists

            artist = str(artist or "").strip()

        if artist:
            return artist

    sort_name = str(item.get("SortName") or "").strip()

    if sort_name:
        return sort_name

    return str(item.get("Name") or "").strip()


def jellydream_apply_letter_filter(items, letter, by_artist=False):
    letter = str(letter or "").strip().upper()

    if letter not in JELLYDREAM_ALPHABET:
        return list(items or [])

    matched = []

    for item in items or []:
        if jellydream_letter_of_name(
            jellydream_letter_item_name(item, by_artist=by_artist)
        ) == letter:
            matched.append(item)

    return matched


def jellydream_alphabet_line(cursor_index, active_letter):
    active_letter = str(active_letter or "").strip().upper()
    parts = []

    for index, key in enumerate(JELLYDREAM_ALPHABET):
        selected = (
            cursor_index is not None and index == cursor_index
        ) or (
            cursor_index is None and key == active_letter
        )

        if selected:
            parts.append("\\c00f1c40f%s\\c00ffffff" % key)
        else:
            parts.append(key)

    return "\\c00ffffff" + "  ".join(parts)


def jellydream_setup_letter_bar(screen):
    from Components.Label import Label

    screen._letter_filter = ""
    screen._alphabet_open = False
    screen._alphabet_cursor = 0
    screen["alphabet"] = Label("")
    screen["empty_message"] = Label("")
    jellydream_refresh_alphabet(screen)


def jellydream_refresh_alphabet(screen):
    if "alphabet" not in screen:
        return

    cursor = (
        int(getattr(screen, "_alphabet_cursor", 0) or 0)
        if getattr(screen, "_alphabet_open", False)
        else None
    )

    screen["alphabet"].setText(
        jellydream_alphabet_line(
            cursor,
            getattr(screen, "_letter_filter", "")
        )
    )


def jellydream_with_letter_label(screen, text):
    letter = str(getattr(screen, "_letter_filter", "") or "").strip()

    if not letter:
        return text

    return "%s   ·   %s" % (text, letter)


def jellydream_focus_alphabet(screen):
    screen._alphabet_open = True
    letter = str(getattr(screen, "_letter_filter", "") or "")

    if letter in JELLYDREAM_ALPHABET:
        screen._alphabet_cursor = JELLYDREAM_ALPHABET.index(letter)

    jellydream_refresh_alphabet(screen)

    if "status" in screen:
        screen["status"].setText(
            jellydream_with_sort_status(
                _("LINKS/RECHTS = Buchstabe | OK = filtern | HOCH = zurück")
            )
        )


def jellydream_move_alphabet(screen, direction):
    if not getattr(screen, "_alphabet_open", False):
        return False

    cursor = int(getattr(screen, "_alphabet_cursor", 0) or 0)
    count = len(JELLYDREAM_ALPHABET)

    if direction == "left":
        screen._alphabet_cursor = (cursor - 1) % count
        jellydream_refresh_alphabet(screen)
        return True

    if direction == "right":
        screen._alphabet_cursor = (cursor + 1) % count
        jellydream_refresh_alphabet(screen)
        return True

    if direction == "up":
        screen._alphabet_open = False
        jellydream_refresh_alphabet(screen)

        if hasattr(screen, "updateList"):
            screen.updateList()

        return True

    if direction == "down":
        return True

    return False


def jellydream_confirm_alphabet(screen, refilter):
    if not getattr(screen, "_alphabet_open", False):
        return False

    cursor = int(getattr(screen, "_alphabet_cursor", 0) or 0)
    cursor = max(0, min(cursor, len(JELLYDREAM_ALPHABET) - 1))
    screen._letter_filter = JELLYDREAM_ALPHABET[cursor]
    screen._alphabet_open = False
    screen._alphabet_stay = True

    if callable(refilter):
        refilter()

    return True


def jellydream_reset_letter_filter(screen, refilter):
    screen._letter_filter = ""
    screen._alphabet_open = False
    screen._alphabet_cursor = 0
    jellydream_refresh_alphabet(screen)

    if "empty_message" in screen:
        screen["empty_message"].setText("")

    if callable(refilter):
        refilter()


def jellydream_show_letter_empty(screen, has_items):
    if "empty_message" not in screen:
        return False

    letter = str(getattr(screen, "_letter_filter", "") or "").strip()

    if letter and not has_items:
        screen["empty_message"].setText(
            _("Nichts hier.\nEs sind aktuell keine Einträge verfügbar.")
        )
        return True

    screen["empty_message"].setText("")
    return False


def jellydream_finish_letter_choice(screen, has_items):
    if getattr(screen, "_alphabet_stay", False) and not has_items:
        screen._alphabet_open = True

    screen._alphabet_stay = False
    jellydream_refresh_alphabet(screen)
    empty = jellydream_show_letter_empty(screen, has_items)

    if getattr(screen, "_alphabet_open", False) and "status" in screen:
        screen["status"].setText(
            jellydream_with_sort_status(
                _("LINKS/RECHTS = Buchstabe | OK = filtern | HOCH = zurück")
            )
        )
    elif empty and "status" in screen:
        screen["status"].setText(
            jellydream_with_sort_status(_("RUNTER = Buchstabe"))
        )

    return empty


def _jellydream_menu_reset_letter(screen, chosen_mode):
    if chosen_mode != "reset_letter":
        return False

    if hasattr(screen, "resetLetterFilter"):
        screen.resetLetterFilter()

    return True


_MUSIC_QUEUE = []


def jellydream_music_queue_set(items):
    global _MUSIC_QUEUE

    queue = []

    for item in items or []:
        if not isinstance(item, dict):
            continue

        item_id = str(item.get("Id") or "").strip()

        if not item_id:
            continue

        queue.append({
            "Id": item_id,
            "Name": str(item.get("Name") or ""),
            "RunTimeTicks": item.get("RunTimeTicks") or 0
        })

    _MUSIC_QUEUE = queue


def jellydream_music_queue_clear():
    global _MUSIC_QUEUE
    _MUSIC_QUEUE = []


def jellydream_music_queue_has_items():
    return bool(_MUSIC_QUEUE)


def jellydream_music_queue_pop():
    global _MUSIC_QUEUE

    if not _MUSIC_QUEUE:
        return None

    return _MUSIC_QUEUE.pop(0)


def jellydream_item_is_favorite(item):
    try:
        if not isinstance(item, dict):
            return False

        user_data = item.get("UserData") or {}

        if not isinstance(user_data, dict):
            return False

        return bool(user_data.get("IsFavorite"))

    except Exception:
        return False


def jellydream_item_poster_badge(item):
    """
    Gibt den Poster-Hinweis zurück: Haken für gesehen, sonst die Zahl
    ungesehener Folgen. Leerer Text bedeutet kein Hinweis.
    """
    try:
        if not isinstance(item, dict):
            return ""

        user_data = item.get("UserData") or {}

        if not isinstance(user_data, dict):
            user_data = {}

        item_type = str(item.get("Type") or "").strip()

        try:
            unplayed = int(user_data.get("UnplayedItemCount") or 0)
        except Exception:
            unplayed = 0

        if unplayed < 0:
            unplayed = 0

        if bool(user_data.get("Played")):
            return "\u2713"

        if item_type in ("Series", "Season", "BoxSet") and unplayed > 0:
            return "99+" if unplayed > 99 else str(unplayed)

        if unplayed > 0:
            return "99+" if unplayed > 99 else str(unplayed)

        return ""

    except Exception:
        return ""


def jellydream_season_progress(item):
    """
    Fortschritt einer Staffel: gesehene Folgen, Gesamtzahl und Prozent.
    Ohne belastbare Angabe kommt None zurück.
    """
    try:
        if not isinstance(item, dict):
            return None

        if str(item.get("Type") or "").strip() != "Season":
            return None

        user_data = item.get("UserData") or {}

        if not isinstance(user_data, dict):
            user_data = {}

        try:
            reported = float(user_data.get("PlayedPercentage") or 0)
        except Exception:
            reported = 0

        try:
            total = int(item.get("ChildCount") or 0)
        except Exception:
            total = 0

        try:
            unplayed = int(user_data.get("UnplayedItemCount") or 0)
        except Exception:
            unplayed = 0

        if total < 0:
            total = 0

        if unplayed < 0:
            unplayed = 0

        has_unplayed = "UnplayedItemCount" in user_data
        played = None
        percent = reported

        # Die Staffelliste liefert bei ChildCount oft die doppelte
        # Folgenzahl. PlayedPercentage 0 heißt: nichts gesehen.
        # Die Gesamtzahl ist dann die Zahl der ungesehenen Folgen.
        if bool(user_data.get("Played")) or reported >= 99.5:
            percent = 100

            if has_unplayed and unplayed > 0 and reported <= 0:
                total = unplayed
                played = 0
                percent = 0
            elif total > 0:
                played = total
        elif reported <= 0:
            percent = 0
            played = 0

            if has_unplayed and unplayed > 0:
                total = unplayed
        elif has_unplayed:
            remaining = 1.0 - (reported / 100.0)

            if unplayed <= 0 or remaining <= 0.001:
                percent = 100
                played = total if total > 0 else None
            else:
                total = int(round(unplayed / remaining))

                if total < unplayed:
                    total = unplayed

                played = max(0, total - unplayed)
                percent = reported

        if percent < 0:
            percent = 0

        if percent > 100:
            percent = 100

        if played is None and percent <= 0:
            return None

        return {
            "played": played,
            "total": total if total > 0 else None,
            "percent": percent
        }

    except Exception:
        return None


def jellydream_season_progress_text(item):
    progress = jellydream_season_progress(item)

    if not progress:
        return ""

    played = progress.get("played")
    total = progress.get("total")

    if played is not None and total:
        return _("Staffelfortschritt: %s von %s") % (
            int(played),
            int(total)
        )

    return _("Staffelfortschritt: %s %%") % int(
        round(progress.get("percent") or 0)
    )


def jellydream_int_or_default(value, default=0):
    try:
        if value is None or str(value).strip() == "":
            return int(default)

        return int(value)

    except Exception:
        return int(default)


def jellydream_safe_config_selection_value(
    value,
    choices,
    fallback,
    setting_name=""
):
    """
    Liefert ausschließlich einen Wert zurück, der in den Choices
    von ConfigSelection vorhanden ist.

    Damit können alte, manuell geänderte oder nicht mehr unterstützte
    Werte aus der gespeicherten JSON-Konfiguration die Settings-GUI
    nicht beschädigen.
    """
    try:
        valid_values = [
            str(choice[0])
            for choice in (choices or [])
            if isinstance(choice, (tuple, list)) and choice
        ]

        candidate = str(
            value if value is not None else ""
        ).strip()

        fallback = str(
            fallback if fallback is not None else ""
        ).strip()

        # Direkte Übereinstimmung, z. B. "1080p" oder "8000000".
        if candidate in valid_values:
            return candidate

        # Kompatibilität für alte numerische Werte:
        # JSON 4 wird zum Choice-Wert "04".
        if candidate:
            try:
                candidate_number = int(candidate)

                for valid_value in valid_values:
                    try:
                        if int(valid_value) == candidate_number:
                            return valid_value
                    except (TypeError, ValueError):
                        pass

            except (TypeError, ValueError):
                pass

        # Nur vor einem tatsächlich vorhandenen, aber ungültigen Wert warnen.
        # Fehlende oder leere Werte verwenden still den Standardwert.
        if setting_name and candidate:
            jd_log(
                "Invalid stored setting for '%s': '%s' -> using default '%s'"
                % (
                    setting_name,
                    candidate,
                    fallback
                ),
                level="WARNING"
            )

        if fallback in valid_values:
            return fallback

        return valid_values[0] if valid_values else ""

    except Exception as e:
        jd_log(
            "Setting value could not be validated%s: %s"
            % (
                " für '%s'" % setting_name
                if setting_name else "",
                e
            ),
            level="WARNING"
        )

        return str(fallback or "")

    
def jellydream_stop_timer(owner, attr_name):
    if not jellydream_is_gui_thread():
        return jellydream_dispatch_to_gui(
            lambda: jellydream_stop_timer(
                owner,
                attr_name
            )
        )

    try:
        timer = getattr(owner, attr_name, None)

        if timer:
            try:
                timer.stop()
            except Exception as e:
                jd_log(
                    "Timer '%s' could not be stopped: %s"
                    % (
                        attr_name,
                        e
                    ),
                    level="WARNING"
                )

            setattr(owner, attr_name, None)

        return True

    except Exception as e:
        jd_log(
            "Error in jellydream_stop_timer for '%s': %s"            % (
                attr_name,
                e
            ),
            level="WARNING"
        )
        return False


def jellydream_handle_navigation(screen_instance, direction, items_list, current_index, items_per_page=ITEMS_PER_PAGE_EPISODES, index_attr="index", columns=None):
    try:
        if not items_list:
            return current_index

        count = len(items_list)
        if count <= 0:
            return current_index

        try:
            current_index = int(current_index)
        except Exception:
            current_index = 0

        if current_index < 0:
            current_index = 0
        elif current_index >= count:
            current_index = count - 1

        new_index = current_index

        if columns is None:
            if items_per_page == 6:
                columns = 3
            elif items_per_page == 3:
                columns = 1
            else:
                columns = items_per_page if items_per_page > 0 else 1

        columns = max(1, int(columns))

        if direction == "left":
            new_index = (current_index - 1) % count

        elif direction == "right":
            new_index = (current_index + 1) % count

        elif direction == "up":
            candidate = current_index - columns
            if candidate >= 0:
                new_index = candidate
            else:
                col = current_index % columns
                last_row_start = ((count - 1) // columns) * columns
                candidate = last_row_start + col
                if candidate >= count:
                    candidate = count - 1
                new_index = candidate

        elif direction == "down":
            candidate = current_index + columns
            if candidate < count:
                new_index = candidate
            else:
                col = current_index % columns
                candidate = col
                if candidate >= count:
                    candidate = count - 1
                new_index = candidate

        if new_index != current_index:
            setattr(screen_instance, index_attr, new_index)

            if hasattr(screen_instance, "updateList"):
                screen_instance.updateList()
            elif hasattr(screen_instance, "updateGrid"):
                screen_instance.updateGrid()

        return new_index
    except Exception as e:
        jd_log(f"Error in central navigation: {e}", level="WARNING")
        return current_index

def ticks_to_seconds(ticks):
    try:
        return int((ticks or 0) / TICKS_PER_SECOND)
    except Exception as e:
        jd_log(
            "Error converting ticks to seconds for value %s: %s"            % (
                ticks,
                e
            ),
            level="WARNING"
        )
        return 0


def jellydream_resume_seconds(item, runtime_seconds=0):
    """Gespeicherte Position, wenn Fortsetzen sinnvoll ist."""
    try:
        if not isinstance(item, dict):
            return 0

        user_data = item.get("UserData") or {}

        if not isinstance(user_data, dict):
            return 0

        position = ticks_to_seconds(
            user_data.get("PlaybackPositionTicks")
        )

        if position < 15:
            return 0

        runtime = int(runtime_seconds or 0)

        if runtime <= 0:
            runtime = ticks_to_seconds(item.get("RunTimeTicks"))

        if runtime > 0 and position >= runtime - 20:
            return 0

        return position

    except Exception:
        return 0


def jellydream_playback_percent(item):
    """Fortschritt in Prozent, wenn der Film mittendrin fortsetzbar ist."""
    try:
        position = jellydream_resume_seconds(item)

        if position <= 0 or not isinstance(item, dict):
            return 0

        runtime = ticks_to_seconds(item.get("RunTimeTicks"))

        if runtime <= 0:
            return 0

        return max(1, min(100, int((position * 100) / runtime)))

    except Exception:
        return 0


def jellydream_show_poster_progress(screen, items, index, items_per_page):
    """Blauer Balken unten am ausgewählten Cover, wenn eine Position gespeichert ist."""
    if "grid_progress" not in screen:
        return

    bar = screen["grid_progress"]
    item = None
    slot = 0

    try:
        index = int(index)
    except Exception:
        index = -1

    try:
        items_per_page = max(1, int(items_per_page))
    except Exception:
        items_per_page = 1

    if items and 0 <= index < len(items):
        item = items[index]
        slot = (index % items_per_page) + 1

    percent = jellydream_playback_percent(item)
    poster_name = "grid_poster%d" % slot
    poster = screen[poster_name] if poster_name in screen else None

    if (
        percent <= 0
        or poster is None
        or not getattr(poster, "instance", None)
        or not getattr(bar, "instance", None)
    ):
        try:
            bar.hide()
        except Exception:
            pass
        return

    position = poster.instance.position()
    size = poster.instance.size()
    bar_height = getattr(screen, "_grid_progress_height", None)

    if not bar_height:
        bar_height = 6

        try:
            bar_height = int(bar.instance.size().height())
        except Exception:
            bar_height = 6

        if bar_height < 2:
            bar_height = 6

        screen._grid_progress_height = bar_height

    screen.setWidgetGeometry(
        "grid_progress",
        int(position.x()),
        int(position.y()) + int(size.height()) - bar_height,
        int(size.width()),
        bar_height
    )

    try:
        bar.setRange(100)
    except Exception:
        pass

    bar.setValue(percent)

    try:
        bar.show()
    except Exception:
        pass


def ticks_to_minutes(ticks):
    try:
        return int(ticks_to_seconds(ticks) / 60)
    except Exception as e:
        jd_log(
            "Error converting ticks to minutes for value %s: %s"            % (
                ticks,
                e
            ),
            level="WARNING"
        )
        return 0


def format_resume_text(title, episode_info="", item_type="", production_year="", original_title=""):
    try:
        title = str(title)

        if item_type == "Episode":
            if episode_info:
                episode_title = (
                    str(original_title)
                    if original_title
                    else _("Folge")
                )

                return "%s\n%s - %s" % (
                    title,
                    episode_info,
                    episode_title
                )

            return title

        year = (
            str(production_year)
            if production_year
            and str(production_year) != "-"
            else ""
        )

        if year:
            return "%s\n%s" % (
                title,
                year
            )

        return title

    except Exception as e:
        jd_log(
            "Error in format_resume_text: %s" % e,
            level="WARNING"
        )
        return str(title)


def jellydream_wrap(text, width, max_lines):
    try:
        words = (text or "").replace("\r", " ").replace("\n", " ").split()
        if not words:
            return ""
            
        lines = []
        line = ""
        has_more_words = False

        for i, word in enumerate(words):

            while len(word) > width:
                lines.append(word[:width])
                word = word[width:]
                if len(lines) >= max_lines:
                    has_more_words = (i < len(words) - 1) or (len(word) > width)
                    break
                continue

            candidate = (line + " " + word).strip()
            if len(candidate) <= width:
                line = candidate
            else:
                if line:
                    lines.append(line)
                if len(lines) >= max_lines:
                    has_more_words = True
                    break
                line = word

        if len(lines) < max_lines and line:
            lines.append(line)
        elif line:
            has_more_words = True
            
        result = "\n".join(lines)

        if has_more_words:
            result = f"{result.rstrip('. ')}..."
            
        return result
    except Exception:
        return text or ""


def jellydream_wrap_lines(text, width):
    try:
        words = (text or "").replace("\r", " ").replace("\n", " ").split()
        lines = []
        line = ""
        for word in words:
            if len(word) > width and not line:
                lines.append(word)
                continue

            candidate = (line + " " + word).strip()
            if len(candidate) <= width:
                line = candidate
            else:
                if line:
                    lines.append(line)
                line = word
        if line:
            lines.append(line)
        return lines
    except Exception:
        return []


def jellydream_two_line_title(text, width=23):
    try:

        lines = jellydream_wrap_lines(text or "", width)
        if not lines:
            return ""

        if len(lines) <= 2:
            return "\n".join(lines)

        second = lines[1]
        space_for_dots = max(3, width - 3)
        
        if len(second) > space_for_dots:
            second = second[:space_for_dots]

        return f"{lines[0]}\n{second.rstrip('. ')}..."
    except Exception:
        return text or ""


def jellydream_safe_set_status(screen, message, level="INFO"):
    try:
        clean_message = str(message or "").strip()
        if not clean_message:
            clean_message = _("Unbekannter Status")
        else:
            clean_message = _(clean_message)

        if "status" in screen:
            screen["status"].setText(clean_message)

        if level in ("WARNING", "ERROR"):
            jd_log(clean_message, level=level)
        else:
            jd_log(clean_message, level="INFO")
    except Exception as e:
        try:
            jd_log(f"Error while setting a status message: {e}", level="WARNING")
        except Exception:
            pass


def jellydream_safe_current_item(items, index):
    try:
        if not items or index is None:
            return None
        index = int(index)
        if index < 0 or index >= len(items):
            return None
        return items[index]
    except Exception:
        return None


def jellydream_set_imdb_badge(screen, item, fallback_item=None):
    """
    Zeigt Logo und Punktzahl. Der Rückgabewert ist der Text-Fallback,
    falls das Logo nicht gezeichnet werden kann.
    """
    from .library import jellydream_item_imdb_rating_number

    number = ""

    if isinstance(item, dict) or isinstance(fallback_item, dict):
        number = jellydream_item_imdb_rating_number(
            item,
            fallback_item
        )

    logo = screen["imdb_logo"] if "imdb_logo" in screen else None
    label = screen["imdb_rating"] if "imdb_rating" in screen else None

    if logo is None or label is None:
        return (_("IMDb %s") % number) if number else ""

    path = os.path.join(PLUGIN_DIR, "images", "imdb.png")

    if number and jellydream_safe_set_pixmap(logo, path):
        try:
            label.setText(number)
            label.show()
        except Exception:
            pass

        return ""

    try:
        if getattr(logo, "instance", None):
            logo.instance.hide()
    except Exception:
        pass

    try:
        label.setText("")
    except Exception:
        pass

    return (_("IMDb %s") % number) if number else ""


def jellydream_safe_set_pixmap(widget, image_path, hide_if_missing=False):
    from .images import jellydream_set_pixmap_if_changed
    return jellydream_set_pixmap_if_changed(
        widget,
        image_path,
        hide_if_missing=hide_if_missing
    )

        
def jellydream_safe_open(session, callback, screen_class, *args, **kwargs):
    try:
        if callback:
            session.openWithCallback(
                callback,
                screen_class,
                *args,
                **kwargs
            )
        else:
            session.open(
                screen_class,
                *args,
                **kwargs
            )

        return True, ""

    except Exception as e:
        msg = (
            _("Screen '%s' konnte nicht geöffnet werden: %s")
            % (
                getattr(screen_class, "__name__", str(screen_class)),
                e
            )
        )
        jd_log(
            "Screen '%s' could not be opened: %s" % (
                getattr(screen_class, "__name__", str(screen_class)),
                e
            ),
            level="ERROR"
        )
        return False, msg


def jellydream_restart_single_timer(
    owner,
    attr_name,
    callback,
    delay_ms=120
):
    if not jellydream_is_gui_thread():
        return jellydream_dispatch_to_gui(
            lambda: jellydream_restart_single_timer(
                owner,
                attr_name,
                callback,
                delay_ms
            )
        )

    try:
        old_timer = getattr(owner, attr_name, None)

        if old_timer:
            try:
                old_timer.stop()
            except Exception:
                pass

            setattr(owner, attr_name, None)

        timer = eTimer()

        try:
            timer.callback.append(callback)
        except Exception:
            try:
                timer.timeout.get().append(callback)
            except Exception as e:
                jd_log(
                    "Timer callback could not be registered: %s" % e,
                    level="WARNING"
                )
                return False

        setattr(owner, attr_name, timer)
        timer.start(int(delay_ms), True)
        return True

    except Exception as e:
        jd_log(
            "Error in jellydream_restart_single_timer: %s" % e,
            level="WARNING"
        )
        return False
