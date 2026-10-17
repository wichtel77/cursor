# -*- coding: utf-8 -*-

import errno
import os
import threading
import time

import urllib.request
import urllib.parse
import urllib.error
try:
    from enigma import ePicLoad
except ImportError:
    ePicLoad = None

from Tools.LoadPixmap import LoadPixmap
from enigma import getDesktop

from . import constants as jd_constants
from .constants import (
    BACKGROUND_FILE,
    IMAGE_NEGATIVE_CACHE_TTL_404,
    IMAGE_NEGATIVE_CACHE_TTL_410,
    JOB_PRIORITY_IMAGE_NORMAL,
    JellyDreamLRUCache,
    JellyDreamScopedCacheKey,
    SYSTEM_SPINNER_COLLECTION_DIRECTORIES,
    SYSTEM_SPINNER_DIRECTORIES,
    TIMEOUT_IMAGE
)
from .log import jd_log
from .workers import (
    jellydream_dispatch_to_gui,
    jellydream_submit_backdrop,
    jellydream_submit_image
)
from .config import (
    jellydream_cache_dir,
    jellydream_dynamic_backdrops_enabled,
    jellydream_scoped_cache_key
)
from .http import (
    jellydream_auth_snapshot,
    jellydream_auth_snapshot_key,
    jellydream_auth_snapshot_matches_current,
    jellyfin_api_read_ex
)


try:
    from enigma import ePicLoad
except ImportError:
    ePicLoad = None

_IMAGE_DOWNLOAD_LOCK = threading.Lock()

_IMAGE_DOWNLOAD_IN_PROGRESS = set()


_GLOBAL_SPINNER_FRAME_CACHE = {}

_GLOBAL_SPINNER_FRAME_CACHE_LOCK = threading.Lock()

# Dekodierte Pixmaps benötigen viel RAM.
# Gerade auf OpenPLi ist ein kleiner Cache stabiler als 80 Bilder.
_GLOBAL_PIXMAP_CACHE = JellyDreamLRUCache(max_size=24)

_GLOBAL_BACKDROP_PIXMAP_CACHE = JellyDreamLRUCache(max_size=4)

# Verhindert wiederholtes Öffnen derselben JPEG-Datei für die Prüfung
# der FF-D8-/FF-D9-Marker. Der Dateistatus ist Teil des Cache-Keys,
# damit eine geänderte oder neu geschriebene Datei automatisch erneut
# geprüft wird.
_GLOBAL_IMAGE_VALIDATION_CACHE = JellyDreamLRUCache(max_size=512)

_GLOBAL_IMAGE_PATH_CACHE = JellyDreamLRUCache(max_size=256)


_GLOBAL_IMAGE_NEGATIVE_CACHE = JellyDreamLRUCache(
    max_size=512
)


_ASYNC_IMAGE_JOB_LOCK = threading.Lock()

_ASYNC_IMAGE_JOBS = set()

_ASYNC_IMAGE_JOB_CALLBACKS = {}

    
def jellydream_get_system_spinner_directory(choice=""):
    try:
        spinner_choice = os.path.basename(
            str(choice or "").strip()
        )

        if spinner_choice:
            for collection_directory in (
                SYSTEM_SPINNER_COLLECTION_DIRECTORIES
            ):
                selected_directory = os.path.join(
                    collection_directory,
                    spinner_choice
                )

                first_frame = os.path.join(
                    selected_directory,
                    "wait1.png"
                )

                if jellydream_valid_file(first_frame, 1):
                    jd_log(
                        "Selected system spinner found: %s"                        % selected_directory,
                        level="DEBUG"
                    )
                    return selected_directory

        for spinner_directory in SYSTEM_SPINNER_DIRECTORIES:
            first_frame = os.path.join(
                spinner_directory,
                "wait1.png"
            )

            if jellydream_valid_file(first_frame, 1):
                jd_log(
                    "System spinner found: %s"
                    % spinner_directory,
                    level="DEBUG"
                )
                return spinner_directory

        jd_log(
            "No compatible system spinner directory found",
            level="WARNING"
        )

    except Exception as e:
        jd_log(
            "System spinner directory could not be resolved: %s" % e,
            level="WARNING"
        )

    return ""


def jellydream_get_spinner_frames(choice=""):
    try:
        spinner_choice = os.path.basename(
            str(choice or "").strip()
        )

        cache_key = spinner_choice or "__default__"

        with _GLOBAL_SPINNER_FRAME_CACHE_LOCK:
            if cache_key in _GLOBAL_SPINNER_FRAME_CACHE:
                return _GLOBAL_SPINNER_FRAME_CACHE[cache_key]

            spinner_directory = (
                jellydream_get_system_spinner_directory(
                    spinner_choice
                )
            )

            if not spinner_directory:
                _GLOBAL_SPINNER_FRAME_CACHE[cache_key] = ()
                return ()

            frames = []

            for index in range(1, 65):
                frame_path = os.path.join(
                    spinner_directory,
                    "wait%d.png" % index
                )

                if jellydream_valid_file(frame_path, 1):
                    frames.append(frame_path)

            # Alle Frames einmalig dekodieren. Dadurch muss die Animation
            # später nicht mehr bei jedem Bild setPixmapFromFile() aufrufen.
            preloaded_frames = []

            for frame_path in frames:
                try:
                    try:
                        pixmap = LoadPixmap(
                            path=frame_path,
                            cached=True
                        )
                    except TypeError:
                        # Kompatibilität mit älteren Enigma2-Versionen.
                        pixmap = LoadPixmap(frame_path)

                    if pixmap is not None:
                        preloaded_frames.append(pixmap)

                except Exception as e:
                    jd_log(
                        "Spinner frame could not be preloaded: %s"
                        % e,
                        level="DEBUG"
                    )

            frames = tuple(preloaded_frames)

            _GLOBAL_SPINNER_FRAME_CACHE[cache_key] = frames

            if frames:
                jd_log(
                    "%d spinner frames from '%s' preloaded once"
                    % (
                        len(frames),
                        spinner_directory
                    ),
                    level="DEBUG"
                )
            else:
                jd_log(
                    "No frames found in the spinner directory: %s"
                    % spinner_directory,
                    level="WARNING"
                )

            return frames

    except Exception as e:
        jd_log(
            "System spinner could not be loaded: %s"
            % e,
            level="WARNING"
        )
        return ()


def jellydream_load_pixmap_cached(path):
    """
    Lädt ein Bild als Enigma-Pixmap.

    Backdrops erhalten einen kleinen separaten Cache. Dadurch verdrängen
    große Fullscreen-JPEGs nicht alle Poster aus dem normalen Bildcache
    und belegen nicht unnötig viel RAM.
    """
    try:
        path = os.path.abspath(
            str(path or "").strip()
        )

        if not path:
            return None

        extension = os.path.splitext(path)[1].lower()

        if extension not in (".png", ".jpg", ".jpeg", ".svg"):
            return None

        if not jellydream_valid_image_file(path, 500):
            return None

        filename = os.path.basename(path).lower()

        is_backdrop = filename.startswith("backdrop_")

        pixmap_cache = (
            _GLOBAL_BACKDROP_PIXMAP_CACHE
            if is_backdrop
            else _GLOBAL_PIXMAP_CACHE
        )

        cached_pixmap = pixmap_cache.get(path)

        if cached_pixmap is not None:
            return cached_pixmap

        try:
            # Backdrops werden bereits im kleinen Python-LRU gehalten.
            # Daher keinen zusätzlichen großen Enigma-internen Cache nutzen.
            pixmap = LoadPixmap(
                path=path,
                cached=not is_backdrop
            )
        except TypeError:
            # Kompatibilität mit älteren Enigma2-Versionen.
            pixmap = LoadPixmap(path)

        if pixmap is not None:
            pixmap_cache.set(path, pixmap)

        return pixmap

    except Exception as e:
        jd_log(
            "Pixmap could not be loaded: %s | path: %s"
            % (
                e,
                path if "path" in locals() else "<unbekannt>"
            ),
            level="DEBUG"
        )

    return None



def jellydream_widget_pixel_size(widget):
    try:
        instance = getattr(widget, "instance", None)

        if not instance:
            return 0, 0

        widget_size = instance.size()
        width = int(widget_size.width())
        height = int(widget_size.height())

        if width > 0 and height > 0:
            return width, height

    except Exception:
        pass

    return 0, 0



def jellydream_prepared_pixmap_cache_key(path, width, height):
    return "|".join((
        os.path.abspath(str(path or "").strip()),
        str(int(width or 0)),
        str(int(height or 0))
    ))



class JellyDreamBackdropDecoder:
    """
    Dekodiert Fullscreen-Hintergründe fertig skaliert, bevor sie
    sichtbar werden.

    OpenPLi skaliert JPEGs sonst erst beim OSD-Blit. Zusammen mit
    alphatest=blend entsteht der zeilenweise Aufbau und die GUI bremst.
    ePicLoad erledigt Decode und Scale unsichtbar; setPixmap() bekommt
    danach ein fertiges Bild in Widgetgröße.
    """

    def __init__(self):
        self.picload = None
        self.busy = False
        self.current_job = None
        self.pending_job = None

        if ePicLoad is None:
            return

        try:
            self.picload = ePicLoad()
            self.picload.PictureData.get().append(
                self._on_decoded
            )
        except Exception as e:
            jd_log(
                "ePicLoad is not available for backgrounds: %s" % e,
                level="DEBUG"
            )
            self.picload = None

    def request(self, path, width, height, callback):
        job = {
            "path": os.path.abspath(str(path or "").strip()),
            "width": int(width or 0),
            "height": int(height or 0),
            "callback": callback
        }

        if (
            not job["path"]
            or job["width"] <= 0
            or job["height"] <= 0
            or not callable(callback)
        ):
            return False

        if self.picload is None:
            return False

        if self.busy:
            self.pending_job = job
            return True

        return self._start(job)

    def _start(self, job):
        try:
            # Vierter Wert 0: Seitenverhältnis nicht halten, sonst bleiben
            # links und rechts transparente Ränder, weil das Fenster breiter als 16:9 ist.
            para = (
                int(job["width"]),
                int(job["height"]),
                0,
                0,
                0,
                1,
                "#ff000000"
            )

            try:
                self.picload.setPara(para)
            except TypeError:
                self.picload.setPara(*para)

            self.busy = True
            self.current_job = job
            result = self.picload.startDecode(job["path"])

            try:
                decode_failed = int(result) != 0
            except Exception:
                decode_failed = False

            if decode_failed:
                self.busy = False
                self.current_job = None
                return False

            return True

        except Exception as e:
            self.busy = False
            self.current_job = None
            jd_log(
                "Background decode could not start: %s" % e,
                level="DEBUG"
            )
            return False

    def _on_decoded(self, *args):
        job = self.current_job
        pixmap = None

        try:
            pixmap = self.picload.getData()
        except Exception:
            pixmap = None

        self.busy = False
        self.current_job = None

        if job and pixmap is not None:
            cache_key = jellydream_prepared_pixmap_cache_key(
                job["path"],
                job["width"],
                job["height"]
            )

            try:
                _GLOBAL_BACKDROP_PIXMAP_CACHE.set(
                    cache_key,
                    pixmap
                )
            except Exception:
                pass

            try:
                job["callback"](pixmap, job)
            except Exception as e:
                jd_log(
                    "Finished background could not be set: %s" % e,
                    level="DEBUG"
                )

        pending = self.pending_job
        self.pending_job = None

        if pending:
            self._start(pending)

    def fallback_pixmap(self, path):
        return jellydream_load_pixmap_cached(path)



_JELLYDREAM_BACKDROP_DECODER = None



def jellydream_backdrop_decoder():
    global _JELLYDREAM_BACKDROP_DECODER

    if _JELLYDREAM_BACKDROP_DECODER is None:
        _JELLYDREAM_BACKDROP_DECODER = JellyDreamBackdropDecoder()

    return _JELLYDREAM_BACKDROP_DECODER



def jellydream_assign_background_pixmap(widget, pixmap, path, screen):
    if pixmap is None or not getattr(widget, "instance", None):
        return False

    instance = widget.instance

    try:
        instance.setBlitFlags(0)
    except Exception:
        pass

    try:
        # Fertig skalierte Pixmap 1:1 blitten, nicht nochmal scale=1.
        instance.setScale(0)
    except Exception:
        pass

    instance.setPixmap(pixmap)

    try:
        instance.show()
    except Exception:
        pass

    try:
        widget.show()
    except Exception:
        pass

    if screen is not None:
        screen._jellydream_active_background_path = path

    widget._jellydream_pixmap_state = (
        id(instance),
        path
    )

    return True



def jellydream_set_pixmap_if_changed(
    widget,
    image_path,
    hide_if_missing=False
):
    """
    Setzt eine Pixmap nur, wenn sich Datei oder native Widget-Instanz
    tatsächlich geändert haben.

    Das ist für OpenPLi wichtig: Wiederholte setPixmap()-Aufrufe mit
    derselben Pixmap verursachen dort trotzdem aufwendige Repaints.
    """
    try:
        if not widget or not getattr(widget, "instance", None):
            return False

        instance = widget.instance
        instance_id = id(instance)

        image_path = os.path.abspath(
            str(image_path or "").strip()
        )

        previous_state = getattr(
            widget,
            "_jellydream_pixmap_state",
            None
        )

        target_state = (
            instance_id,
            image_path
        )

        if image_path and jellydream_valid_image_file(
            image_path,
            500
        ):
            # Das Bild ist bereits exakt dieses Bild auf exakt dieser
            # nativen Enigma2-Widget-Instanz. Nicht erneut setzen.
            if previous_state == target_state:
                try:
                    instance.show()
                except Exception:
                    pass

                return True

            pixmap = jellydream_load_pixmap_cached(
                image_path
            )

            if pixmap is None:
                return False

            instance.setPixmap(pixmap)

            try:
                instance.show()
            except Exception:
                pass

            try:
                widget.show()
            except Exception:
                pass

            widget._jellydream_pixmap_state = target_state

            # Kein invalidate(): setPixmap() stößt die Aktualisierung an.
            return True

        if hide_if_missing:
            empty_state = (
                instance_id,
                ""
            )

            if previous_state != empty_state:
                try:
                    instance.setPixmap(None)
                except Exception:
                    pass

                widget._jellydream_pixmap_state = empty_state

            try:
                instance.hide()
            except Exception:
                pass

            try:
                widget.hide()
            except Exception:
                pass

        return False

    except Exception as e:
        jd_log(
            "Pixmap could not be set: %s" % e,
            level="DEBUG"
        )
        return False

        
def jellydream_cache_get(cache, key, default=None):
    try:
        if cache is None:
            return default

        # Bild- und Async-Keys sind bereits sauber getrennt. Dadurch wird
        # jellydream_cache_namespace() nicht ein zweites Mal aufgerufen.
        if isinstance(key, JellyDreamScopedCacheKey):
            scoped_key = key
        else:
            scoped_key = jellydream_scoped_cache_key(
                key
            )

        return cache.get(
            scoped_key,
            default
        )

    except Exception:
        return default


def jellydream_cache_delete(cache, key):
    try:
        if cache is None:
            return False

        if isinstance(key, JellyDreamScopedCacheKey):
            scoped_key = key
        else:
            scoped_key = jellydream_scoped_cache_key(
                key
            )

        cache.delete(scoped_key)
        return True

    except Exception:
        return False


def jellydream_cache_drop_containing(cache, needle):
    needle = str(needle or "").strip()

    if cache is None or not needle:
        return

    try:
        with cache.lock:
            keys = [
                key
                for key in cache.cache.keys()
                if needle in str(key)
            ]
    except Exception:
        return

    for key in keys:
        try:
            cache.delete(key)
        except Exception:
            pass



def jellydream_cache_set(cache, key, value):
    try:
        if cache is None:
            return False

        # Bereits getrennte Schlüssel direkt verwenden.
        if isinstance(key, JellyDreamScopedCacheKey):
            scoped_key = key
        else:
            scoped_key = jellydream_scoped_cache_key(
                key
            )

        cache.set(
            scoped_key,
            value
        )

        return True

    except Exception:
        return False


def jellydream_make_image_cache_key(
    item_id,
    width,
    height,
    image_type="Primary"
):
    try:
        raw_key = "%s|%s|%s|%s" % (
            str(item_id or "").strip(),
            str(image_type or "Primary").strip().lower(),
            int(width or 0),
            int(height or 0)
        )

    except Exception:
        raw_key = "%s|%s|%s|%s" % (
            item_id,
            image_type,
            width,
            height
        )

    return jellydream_scoped_cache_key(
        raw_key
    )


def jellydream_is_image_negatively_cached(job_key):
    """
    Prüft, ob ein Bild kürzlich als nicht vorhanden erkannt wurde.

    Abgelaufene Einträge werden direkt entfernt. Der Rückgabewert ist nur
    für tatsächliche Bildfehler wie HTTP 404 / 410 gedacht, nicht für
    temporäre Netzwerkfehler.
    """
    try:
        entry = jellydream_cache_get(
            _GLOBAL_IMAGE_NEGATIVE_CACHE,
            job_key,
            None
        )

        if not isinstance(entry, dict):
            return False

        expires_at = float(
            entry.get("expires_at") or 0
        )

        if expires_at > time.time():
            return True

        _GLOBAL_IMAGE_NEGATIVE_CACHE.delete(
            jellydream_scoped_cache_key(job_key)
        )

    except Exception:
        pass

    return False


def jellydream_set_image_negative_cache(
    job_key,
    status=404,
    ttl_seconds=IMAGE_NEGATIVE_CACHE_TTL_404
):
    """
    Merkt sich ausschließlich zeitlich begrenzt, dass ein Bild nicht
    existiert. Ein erfolgreicher Download entfernt diesen Eintrag wieder.
    """
    try:
        ttl_seconds = max(60, int(ttl_seconds or 0))

        jellydream_cache_set(
            _GLOBAL_IMAGE_NEGATIVE_CACHE,
            job_key,
            {
                "status": int(status or 0),
                "expires_at": time.time() + ttl_seconds
            }
        )

        jd_log(
            "Missing image cached as negative: status=%s, valid for %s seconds"           % (
                int(status or 0),
                ttl_seconds
            ),
            level="DEBUG"
        )

        return True

    except Exception as e:
        jd_log(
            "Negative image cache could not be set: %s"
            % e,
            level="DEBUG"
        )

    return False


def jellydream_clear_image_negative_cache(job_key):
    try:
        _GLOBAL_IMAGE_NEGATIVE_CACHE.delete(
            jellydream_scoped_cache_key(job_key)
        )
    except Exception:
        pass

        
def jellydream_async_image_worker(item_id, out_dir, width, height, quality=90, image_type="Primary", cache_key=None, async_job_key=None, auth_snapshot=None):
    """
    Führt einen Bilddownload für exakt die Anmeldung aus, unter der
    der Job eingeplant wurde.

    Nach Logout/Login werden Resultate einer alten Sitzung weder in den
    aktuellen RAM-Cache geschrieben noch an die GUI weitergegeben.
    """
    if not isinstance(auth_snapshot, dict):
        auth_snapshot = jellydream_auth_snapshot()

    if cache_key is None:
        cache_key = jellydream_make_image_cache_key(
            item_id,
            width,
            height,
            image_type
        )

    if async_job_key is None:
        async_job_key = cache_key

    result_path = ""
    session_still_current = False

    try:
        result_path = jellydream_download_image(
            item_id,
            out_dir,
            width=width,
            height=height,
            quality=quality,
            image_type=image_type,
            auth_snapshot=auth_snapshot,
            cache_key=cache_key
        )

        # Resultate alter Sitzungen niemals in den Cache der aktuellen
        # Anmeldung übernehmen.
        session_still_current = (
            jellydream_auth_snapshot_matches_current(
                auth_snapshot
            )
        )

        if result_path and session_still_current:
            jellydream_cache_set(
                _GLOBAL_IMAGE_PATH_CACHE,
                cache_key,
                result_path
            )

        elif result_path and not session_still_current:
            jd_log(
                "Stale image-download result discarded: item=%s, type=%s"
                % (
                    item_id,
                    image_type
                ),
                level="DEBUG"
            )

            result_path = ""

    except Exception as e:
        jd_log(
            "Error in async image download: %s" % e,
            level="WARNING"
        )
        result_path = ""

    finally:
        callbacks = []

        try:
            with _ASYNC_IMAGE_JOB_LOCK:
                callbacks = list(
                    _ASYNC_IMAGE_JOB_CALLBACKS.pop(
                        async_job_key,
                        []
                    )
                )

                _ASYNC_IMAGE_JOBS.discard(
                    async_job_key
                )

        except Exception:
            callbacks = []

    # Alte Jobs dürfen keine GUI-Änderung mehr auslösen.
    if not jellydream_auth_snapshot_matches_current(
        auth_snapshot
    ):
        return

    for registered_callback in callbacks:
        jellydream_dispatch_to_gui(
            lambda cb=registered_callback,
            path=result_path or "",
            callback_item_id=item_id: cb(
                path,
                callback_item_id
            )
        )


def jellydream_async_download_image(item_id, out_dir, width, height, quality=90, image_type="Primary", callback=None, priority=JOB_PRIORITY_IMAGE_NORMAL):

    try:
        # Der Snapshot wird beim EINPLANEN erfasst, nicht erst wenn der
        # Worker später ausgeführt wird.
        auth_snapshot = jellydream_auth_snapshot()

        job_key = jellydream_make_image_cache_key(
            item_id,
            width,
            height,
            image_type
        )

        # Gleiche Bildanfrage mit anderer Anmeldung darf nicht mit einem
        # bereits wartenden Alt-Job zusammengelegt werden.
        async_job_key = JellyDreamScopedCacheKey(
            "%s|auth=%s"
            % (
                str(job_key),
                jellydream_auth_snapshot_key(auth_snapshot)
            )
        )
        
        cached_path = jellydream_cache_get(
            _GLOBAL_IMAGE_PATH_CACHE,
            job_key,
            ""
        )

        if cached_path and jellydream_valid_image_file(
            cached_path,
            500
        ):
            if callback:
                jellydream_dispatch_to_gui(
                    lambda cb=callback,
                    path=cached_path,
                    callback_item_id=item_id: cb(
                        path,
                        callback_item_id
                    )
                )

            return cached_path

        # RAM-Eintrag darf nicht auf eine defekte Bilddatei zeigen.
        if cached_path:
            _GLOBAL_IMAGE_PATH_CACHE.delete(job_key)

        if jellydream_is_image_negatively_cached(job_key):
            jd_log(
                "Image download skipped because of negative cache: item=%s, type=%s"
                % (
                    item_id,
                    image_type
                ),
                level="DEBUG"
            )

            # Wichtig: Der Callback muss trotzdem erfolgen. So kann
            # jellydream_render_grid() auf ein Serien-/Fallback-Bild
            # ausweichen.
            if callback:
                jellydream_dispatch_to_gui(
                    lambda cb=callback,
                    callback_item_id=item_id: cb(
                        "",
                        callback_item_id
                    )
                )

            return ""
            
        with _ASYNC_IMAGE_JOB_LOCK:
            if async_job_key in _ASYNC_IMAGE_JOBS:
                if callback:
                    _ASYNC_IMAGE_JOB_CALLBACKS.setdefault(
                        async_job_key,
                        []
                    ).append(callback)

                return ""

            _ASYNC_IMAGE_JOBS.add(async_job_key)
            _ASYNC_IMAGE_JOB_CALLBACKS[async_job_key] = []

            if callback:
                _ASYNC_IMAGE_JOB_CALLBACKS[
                    async_job_key
                ].append(callback)

        worker_job = lambda: jellydream_async_image_worker(
            item_id,
            out_dir,
            width,
            height,
            quality,
            image_type,
            cache_key=job_key,
            async_job_key=async_job_key,
            auth_snapshot=auth_snapshot
        )

        if str(image_type or "").strip().lower() == "backdrop":
            started = jellydream_submit_backdrop(
                worker_job,
                priority=priority
            )
        else:
            started = jellydream_submit_image(
                worker_job,
                priority=priority
            )

        if not started:
            rejected_callbacks = []

            with _ASYNC_IMAGE_JOB_LOCK:
                rejected_callbacks = list(
                    _ASYNC_IMAGE_JOB_CALLBACKS.pop(
                        async_job_key,
                        []
                    )
                )

                _ASYNC_IMAGE_JOBS.discard(
                    async_job_key
                )

            jd_log(
                "Image job was rejected by the worker pool: item=%s, type=%s"
                % (
                    item_id,
                    image_type
                ),
                level="WARNING"
            )

            # Leerer Pfad signalisiert einen abgeschlossenen, aber
            # fehlgeschlagenen Bildabruf. Fallback-Logik und nachfolgende
            # Cover-Tasks können dadurch regulär fortgesetzt werden.
            for registered_callback in rejected_callbacks:
                jellydream_dispatch_to_gui(
                    lambda cb=registered_callback,
                    callback_item_id=item_id: cb(
                        "",
                        callback_item_id
                    )
                )

        return ""

    except Exception as e:
        jd_log(
            "Error in jellydream_async_download_image: %s" % e,
            level="WARNING"
        )
        return ""

        
def jellydream_get_cached_image_path(item_id, width, height, image_type="Primary"):
    try:
        job_key = jellydream_make_image_cache_key(
            item_id,
            width,
            height,
            image_type
        )

        cached = jellydream_cache_get(
            _GLOBAL_IMAGE_PATH_CACHE,
            job_key,
            ""
        )

        if cached and jellydream_valid_image_file(
            cached,
            500
        ):
            return cached

        # RAM-Referenz auf eine beschädigte Datei entfernen.
        if cached:
            _GLOBAL_IMAGE_PATH_CACHE.delete(job_key)

        return ""

    except Exception:
        return ""


def jellydream_safe_image_id(item_id):
    try:
        return (
            str(item_id or "")
            .replace("/", "_")
            .replace("\\", "_")
            .replace(":", "_")
            .strip()
        )
    except Exception:
        return str(item_id or "").strip()


def jellydream_expected_image_filename(
    item_id,
    width,
    height,
    quality=90,
    image_type="Primary"
):
    try:
        safe_item_id = jellydream_safe_image_id(item_id)
        safe_type = (
            str(image_type or "Primary")
            .lower()
            .replace("/", "_")
            .replace("\\", "_")
        )

        extension = (
            "png"
            if safe_type == "logo"
            else "jpg"
        )

        return "%s_%s_%sx%s_q%s.%s" % (
            safe_type,
            safe_item_id,
            int(width or 0),
            int(height or 0),
            int(quality or 90),
            extension
        )
    except Exception:
        return ""

        
def jellydream_download_image(item_id, out_dir, width, height, quality=90, image_type="Primary", auth_snapshot=None, cache_key=None):
    try:
        # Wichtig: Nicht erst hier eine neue Sitzung lesen, wenn ein
        # Snapshot vom einplanenden Aufrufer vorliegt.
        if not isinstance(auth_snapshot, dict):
            auth_snapshot = jellydream_auth_snapshot()

        server = str(
            auth_snapshot.get("server") or ""
        ).strip().rstrip("/")

        token = str(
            auth_snapshot.get("token") or ""
        ).strip()

        if not server or not token or not item_id:
            return ""

        if not os.path.exists(out_dir):
            os.makedirs(out_dir, exist_ok=True)

        expected_filename = jellydream_expected_image_filename(
            item_id,
            width,
            height,
            quality=quality,
            image_type=image_type
        )

        out = os.path.join(out_dir, expected_filename)
        tmp_out = "%s.part%s" % os.path.splitext(out)
        logo_download = (
            str(image_type or "").strip().lower() == "logo"
        )
        min_image_bytes = 150 if logo_download else 500

        # Nur vollständig geprüfte Cache-Bilder verwenden.
        if jellydream_valid_image_file(out, min_image_bytes):
            return out

        # Eine alte, beschädigte oder nur halb geladene Bilddatei darf
        # niemals weiterverwendet werden.
        try:
            if os.path.exists(out):
                os.remove(out)

                jd_log(
                    "Damaged image removed from cache: %s"                    % out,
                    level="WARNING"
                )
        except Exception as e:
            jd_log(
                "Damaged image could not be removed: %s"                % e,
                level="DEBUG"
            )

        if cache_key is None:
            cache_key = jellydream_make_image_cache_key(
                item_id,
                width,
                height,
                image_type
            )

        job_key = cache_key

        cached_path = jellydream_cache_get(
            _GLOBAL_IMAGE_PATH_CACHE,
            job_key,
            ""
        )

        if cached_path and jellydream_valid_image_file(
            cached_path,
            500
        ):
            return cached_path

        # Auch einen alten RAM-Cache-Eintrag entfernen, wenn die Datei
        # inzwischen unvollständig, beschädigt oder gelöscht ist.
        if cached_path:
            _GLOBAL_IMAGE_PATH_CACHE.delete(job_key)

        if jellydream_is_image_negatively_cached(job_key):
            jd_log(
                "Image download skipped because of negative cache: item=%s, type=%s"
                % (
                    item_id,
                    image_type
                ),
                level="DEBUG"
            )
            return ""

        with _IMAGE_DOWNLOAD_LOCK:
            if job_key in _IMAGE_DOWNLOAD_IN_PROGRESS:
                return ""

            _IMAGE_DOWNLOAD_IN_PROGRESS.add(job_key)

        def _download():
            try:
                # Während ein alter .part-Download fehlgeschlagen ist,
                # darf diese Datei nie als fertiges Bild verwendet werden.
                try:
                    if os.path.exists(tmp_out):
                        os.remove(tmp_out)
                except Exception:
                    pass

                # Zwischen Aufruf und Worker-Start könnte ein anderer
                # Download die finale Datei bereits vollständig erzeugt
                # haben.
                if jellydream_valid_image_file(out, min_image_bytes):
                    jellydream_cache_set(
                        _GLOBAL_IMAGE_PATH_CACHE,
                        job_key,
                        out
                    )

                    jellydream_clear_image_negative_cache(
                        job_key
                    )

                    return out

                # Falls eine defekte Datei existiert, vor dem neuen
                # Download löschen.
                try:
                    if os.path.exists(out):
                        os.remove(out)
                except Exception:
                    pass

                url = (
                    "%s/Items/%s/Images/%s?"
                    "maxWidth=%s&maxHeight=%s&quality=%s"
                ) % (
                    server,
                    urllib.parse.quote(str(item_id)),
                    urllib.parse.quote(str(image_type)),
                    int(width),
                    int(height),
                    int(quality)
                )

                if logo_download:
                    url += "&format=Png"

                image_headers = {
                    "User-Agent": "Mozilla/5.0",
                    "X-Emby-Token": token,
                    "Accept": (
                        "image/png,image/*;q=0.5"
                        if logo_download
                        else "image/jpeg,image/*;q=0.8"
                    )
                }

                result = jellyfin_api_read_ex(
                    url,
                    timeout=TIMEOUT_IMAGE,
                    headers=image_headers,
                    auth_snapshot=auth_snapshot
                )

                status = result.get("status", 0)
                data = result.get("data")

                if (
                    logo_download
                    and data
                    and not bytes(data).startswith(b"\x89PNG")
                ):
                    jd_log(
                        "Logo image was not returned as PNG for item id %s"
                        % item_id,
                        level="DEBUG"
                    )
                    return ""

                if result.get("ok") and data and len(data) > min_image_bytes:
                    # Ausschließlich in die temporäre Datei schreiben.
                    # Die finale .jpg-Datei bleibt bis dahin unverändert
                    # bzw. existiert noch nicht.
                    with open(tmp_out, "wb") as image_file:
                        image_file.write(data)
                        image_file.flush()

                        try:
                            os.fsync(image_file.fileno())
                        except Exception:
                            pass

                    # Erst die fertige .part-Datei prüfen.
                    if not jellydream_valid_image_file(
                        tmp_out,
                        min_image_bytes
                    ):
                        try:
                            os.remove(tmp_out)
                        except Exception:
                            pass

                        jd_log(
                            "Incomplete JPEG download discarded: item=%s, type=%s"
                            % (
                                item_id,
                                image_type
                            ),
                            level="WARNING"
                        )

                        return ""

                    # Atomarer Wechsel:
                    # Erst ab hier existiert die finale Cachedatei.
                    os.replace(tmp_out, out)

                    # Zusätzliche Kontrolle nach dem Verschieben.
                    if not jellydream_valid_image_file(out, min_image_bytes):
                        try:
                            os.remove(out)
                        except Exception:
                            pass

                        return ""

                    jellydream_cache_set(
                        _GLOBAL_IMAGE_PATH_CACHE,
                        job_key,
                        out
                    )

                    jellydream_clear_image_negative_cache(
                        job_key
                    )

                    return out

                if status in (404, 410):
                    negative_ttl = (
                        IMAGE_NEGATIVE_CACHE_TTL_410
                        if status == 410
                        else IMAGE_NEGATIVE_CACHE_TTL_404
                    )

                    jellydream_set_image_negative_cache(
                        job_key,
                        status=status,
                        ttl_seconds=negative_ttl
                    )

                elif status == 401:
                    jd_log(
                        "Unauthorized image access (401) for item id %s"                        % item_id,
                        level="ERROR"
                    )

                elif status >= 500:
                    jd_log(
                        "Server error during image download (%s) for item id %s"                        % (
                            status,
                            item_id
                        ),
                        level="ERROR"
                    )

                return ""

            except Exception as e:
                jd_log(
                    "Exception during image download for item id %s: %s"                    % (
                        item_id,
                        e
                    ),
                    level="WARNING"
                )

                return ""

            finally:
                # Eine zurückgebliebene .part-Datei ist nie ein Cachetreffer
                # und wird nach Erfolg oder Fehler sicher entfernt.
                try:
                    if os.path.exists(tmp_out):
                        os.remove(tmp_out)
                except Exception:
                    pass

                with _IMAGE_DOWNLOAD_LOCK:
                    _IMAGE_DOWNLOAD_IN_PROGRESS.discard(job_key)

        return _download()

    except Exception as e:
        jd_log(
            "Exception during image download for item id %s: %s"            % (
                item_id,
                e
            ),
            level="WARNING"
        )

        return ""


def jellydream_valid_file(path, min_size=1):
    try:
        return bool(
            path
            and os.path.exists(path)
            and os.path.getsize(path) >= int(min_size)
        )
    except Exception:
        return False



def jellydream_valid_image_file(path, min_size=500):
    """
    Prüft, ob eine Bilddatei vorhanden und vollständig ist.

    Nicht vorhandene Dateien sind normale Cache-Misses und werden
    deshalb nicht protokolliert.

    JPEG-Dateien werden über FF-D8 am Anfang und FF-D9 am Ende geprüft.
    """
    try:
        path = os.path.abspath(
            str(path or "").strip()
        )

        if not path:
            return False

        try:
            min_size = max(
                0,
                int(min_size or 0)
            )
        except Exception:
            min_size = 0

        try:
            file_stat = os.stat(path)

        except OSError as e:
            # Beim ersten Bildabruf existiert die Cachedatei noch nicht.
            # Das ist kein Fehler, sondern ein normaler Cache-Miss.
            if getattr(e, "errno", None) in (
                errno.ENOENT,
                errno.ENOTDIR
            ):
                return False

            jd_log(
                "Image file status could not be read: %s | %s"
                % (
                    path,
                    e
                ),
                level="DEBUG"
            )

            return False

        if file_stat.st_size < min_size:
            return False

        extension = os.path.splitext(path)[1].lower()

        if extension not in (
            ".png",
            ".jpg",
            ".jpeg",
            ".svg"
        ):
            return False

        modified_ns = getattr(
            file_stat,
            "st_mtime_ns",
            int(file_stat.st_mtime * 1000000000)
        )

        validation_key = (
            "%s|%s|%s|%s"
            % (
                path,
                int(file_stat.st_size),
                int(modified_ns),
                min_size
            )
        )

        cached_result = _GLOBAL_IMAGE_VALIDATION_CACHE.get(
            validation_key,
            None
        )

        if cached_result is not None:
            return bool(cached_result)

        if extension in (
            ".png",
            ".svg"
        ):
            _GLOBAL_IMAGE_VALIDATION_CACHE.set(
                validation_key,
                True
            )
            return True

        with open(path, "rb") as image_file:
            start_marker = image_file.read(2)

            if start_marker != b"\xff\xd8":
                jd_log(
                    "Invalid JPEG start in image cache: %s"                    % path,
                    level="WARNING"
                )

                _GLOBAL_IMAGE_VALIDATION_CACHE.set(
                    validation_key,
                    False
                )
                return False

            image_file.seek(
                -2,
                os.SEEK_END
            )

            end_marker = image_file.read(2)

            if end_marker != b"\xff\xd9":
                jd_log(
                    "Incomplete JPEG detected in image cache: %s"                    % path,
                    level="WARNING"
                )

                _GLOBAL_IMAGE_VALIDATION_CACHE.set(
                    validation_key,
                    False
                )
                return False

        _GLOBAL_IMAGE_VALIDATION_CACHE.set(
            validation_key,
            True
        )

        return True

    except Exception as e:
        jd_log(
            "Image file could not be checked: %s | %s"            % (
                path if "path" in locals() else "<unbekannt>",
                e
            ),
            level="DEBUG"
        )

        return False


def jellydream_get_background_widget_size(
    screen,
    fallback_width=1280,
    fallback_height=720
):
    """
    Liefert die tatsächliche Größe des Background-Widgets.

    Dadurch werden Backdrops passend zum aktuell geladenen HD-, FHD-
    oder UHD-Skin angefordert und im Cache gespeichert.
    """
    try:
        if screen is not None and "background" in screen:
            background_widget = screen["background"]

            if getattr(background_widget, "instance", None):
                widget_size = background_widget.instance.size()

                width = int(widget_size.width())
                height = int(widget_size.height())

                width = min(width, 1600)
                height = min(height, 900)

                if width > 0 and height > 0:
                    return width, height

    except Exception as e:
        jd_log(
            "Background widget size could not be read: %s"           % e,
            level="DEBUG"
        )

    # Falls das Widget noch nicht vollständig aufgebaut wurde, möglichst
    # die aktuelle Desktopauflösung statt einer festen Auflösung verwenden.
    try:
        desktop_size = getDesktop(0).size()

        desktop_width = int(desktop_size.width())
        desktop_height = int(desktop_size.height())

        if desktop_width > 0 and desktop_height > 0:
            return desktop_width, desktop_height

    except Exception:
        pass

    return (
        max(1, int(fallback_width or 1280)),
        max(1, int(fallback_height or 720))
    )



def jellydream_cached_background_path(
    item_id,
    width=1280,
    height=720,
    quality=90,
    image_type="Backdrop"
):
    try:
        item_id = str(item_id or "").strip()

        if not item_id:
            return ""

        filename = jellydream_expected_image_filename(
            item_id,
            width,
            height,
            quality=quality,
            image_type=image_type
        )

        if not filename:
            return ""

        return os.path.join(
            jellydream_cache_dir("cover"),
            filename
        )

    except Exception:
        return ""


def jellydream_resolve_background_path(
    item_id="",
    fallback_path="",
    width=1280,
    height=720,
    image_type="Backdrop"
):
    try:
        cached_path = jellydream_cached_background_path(
            item_id,
            width=width,
            height=height,
            image_type=image_type
        )

        if jellydream_valid_image_file(cached_path, 500):
            return cached_path

        if jellydream_valid_file(fallback_path, 500):
            return fallback_path

        if jellydream_valid_file(BACKGROUND_FILE, 500):
            return BACKGROUND_FILE

        return ""

    except Exception as e:
        jd_log(
            "Error in jellydream_resolve_background_path: %s" % e,
            level="WARNING"
        )
        return ""


def jellydream_apply_resolved_background(screen, item_id="", fallback_path="", width=None, height=None, image_type="Backdrop"):
    try:
        widget_width, widget_height = (
            jellydream_get_background_widget_size(screen)
        )

        if width is None:
            width = widget_width

        if height is None:
            height = widget_height

        bg_path = jellydream_resolve_background_path(
            item_id=item_id,
            fallback_path=fallback_path,
            width=width,
            height=height,
            image_type=image_type
        )

        set_jellydream_background(
            screen,
            bg_path
        )

        return bool(bg_path)

    except Exception as e:
        jd_log(
            "Error in jellydream_apply_resolved_background: %s" % e,
            level="WARNING"
        )

        set_jellydream_background(screen)
        return False


def set_jellydream_background(screen, custom_path=None):
    """
    Setzt einen deckenden Fullscreen-Hintergrund mit LoadPixmap.
    """
    try:
        if "background" not in screen:
            return False

        background_widget = screen["background"]

        if not getattr(
            background_widget,
            "instance",
            None
        ):
            return False

        # Auf langsamen Boxen können dynamische Backdrops deaktiviert werden.
        # Dann wird immer BACKGROUND_FILE verwendet.
        if not jellydream_dynamic_backdrops_enabled():
            custom_path = None
            
        target_path = ""

        if jellydream_valid_image_file(
            custom_path,
            500
        ):
            target_path = os.path.abspath(
                str(custom_path)
            )

        elif jellydream_valid_image_file(
            BACKGROUND_FILE,
            500
        ):
            target_path = os.path.abspath(
                str(BACKGROUND_FILE)
            )

        previous_path = os.path.abspath(
            str(
                getattr(
                    screen,
                    "_jellydream_active_background_path",
                    ""
                ) or ""
            ).strip()
        ) if getattr(
            screen,
            "_jellydream_active_background_path",
            ""
        ) else ""

        screen._jellydream_pending_background_path = target_path

        if target_path and target_path == previous_path:
            try:
                background_widget.show()
            except Exception:
                pass

            try:
                background_widget.instance.show()
            except Exception:
                pass

            return True

        if not target_path:
            # Ohne Zielbild den vorhandenen Hintergrund nicht entfernen.
            # Dadurch bleibt das letzte gültige Bild sichtbar.
            return bool(previous_path)

        paint_width, paint_height = jellydream_widget_pixel_size(
            background_widget
        )

        if paint_width <= 0 or paint_height <= 0:
            paint_width, paint_height = (
                jellydream_get_background_widget_size(screen)
            )

        cache_key = jellydream_prepared_pixmap_cache_key(
            target_path,
            paint_width,
            paint_height
        )
        cached_pixmap = _GLOBAL_BACKDROP_PIXMAP_CACHE.get(
            cache_key
        )

        if cached_pixmap is not None:
            return jellydream_assign_background_pixmap(
                background_widget,
                cached_pixmap,
                target_path,
                screen
            )

        def background_ready(pixmap, job):
            if not getattr(screen, "instance", None):
                return

            if "background" not in screen:
                return

            pending_path = str(
                getattr(
                    screen,
                    "_jellydream_pending_background_path",
                    ""
                ) or ""
            ).strip()

            if pending_path and pending_path != job.get("path"):
                return

            jellydream_assign_background_pixmap(
                screen["background"],
                pixmap,
                job.get("path"),
                screen
            )

        decoder = jellydream_backdrop_decoder()

        if decoder.request(
            target_path,
            paint_width,
            paint_height,
            background_ready
        ):
            # Das bisherige Bild bleibt sichtbar, bis das neue fertig ist.
            return True

        pixmap = decoder.fallback_pixmap(target_path)

        if pixmap is None:
            return False

        assigned = jellydream_assign_background_pixmap(
            background_widget,
            pixmap,
            target_path,
            screen
        )

        try:
            # Unskaliertes JPEG muss das Widget selbst einpassen.
            background_widget.instance.setScale(1)
        except Exception:
            pass

        return assigned

    except Exception as e:
        jd_log(
            "Error in set_jellydream_background: %s"
            % e,
            level="WARNING"
        )

        return False


def jellydream_apply_background_and_shadow(screen, custom_path=None, shadow_name="full_kino_shadow.png"):
    try:
        set_jellydream_background(screen, custom_path)
    except Exception as e:
        try:
            jd_log(f"Error while setting the background: {e}", level="WARNING")
        except Exception:
            pass

    try:
        if "background_shadow" not in screen:
            return

        shadow_widget = screen["background_shadow"]
        if not getattr(shadow_widget, "instance", None):
            return

        if not jd_constants.CURRENT_SKIN_DIRECTORY:
            try:
                shadow_widget.instance.hide()
            except Exception:
                pass
            return

        shadow_file = os.path.join(jd_constants.CURRENT_SKIN_DIRECTORY, "images", shadow_name)

        if os.path.exists(shadow_file) and os.path.getsize(shadow_file) > 0:
            try:
                # PNG hat bereits die Fenstergröße. scale=1 lässt sonst einen Rand stehen.
                shadow_widget.instance.setScale(0)
            except Exception:
                pass
            shadow_widget.instance.setPixmapFromFile(shadow_file)
            shadow_widget.instance.show()
            try:
                shadow_widget.show()
            except Exception:
                pass
        else:
            try:
                shadow_widget.instance.hide()
            except Exception:
                pass

    except Exception as e:
        try:
            jd_log(f"Error while loading shadow image '{shadow_name}': {e}", level="WARNING")
        except Exception:
            pass


def jellydream_item_logo_id(item, fallback_id=""):
    try:
        if isinstance(item, dict):
            image_tags = item.get("ImageTags") or {}

            if isinstance(image_tags, dict) and image_tags.get("Logo"):
                item_id = str(
                    item.get("Id") or item.get("id") or ""
                ).strip()

                if item_id:
                    return item_id

            parent_tag = str(
                item.get("ParentLogoImageTag") or ""
            ).strip()
            parent_id = str(
                item.get("ParentLogoItemId")
                or item.get("SeriesId")
                or ""
            ).strip()

            if parent_tag and parent_id:
                return parent_id

        return str(fallback_id or "").strip()

    except Exception:
        return ""


def jellydream_png_size(path):
    try:
        with open(path, "rb") as image_file:
            header = image_file.read(24)

        if header[:8] != b"\x89PNG\r\n\x1a\n" or len(header) < 24:
            return None

        image_width = int.from_bytes(header[16:20], "big")
        image_height = int.from_bytes(header[20:24], "big")

        if image_width < 1 or image_height < 1:
            return None

        return image_width, image_height

    except Exception:
        return None


def jellydream_place_logo_widget(screen, widget_name, path, align_right=False):
    """
    Passt das Logo in den Skin-Platz ein, ohne es hochzuskalieren.
    """
    try:
        slots = getattr(screen, "_jellydream_logo_slots", None) or {}
        slot = slots.get(widget_name)

        if not slot or not hasattr(screen, "setWidgetGeometry"):
            return False

        slot_x, slot_y, slot_w, slot_h = slot
        image_size = jellydream_png_size(path)

        if image_size:
            image_w, image_h = image_size
            scale = min(
                float(slot_w) / float(image_w),
                float(slot_h) / float(image_h),
                1.0
            )
            draw_w = max(1, int(image_w * scale))
            draw_h = max(1, int(image_h * scale))
        else:
            draw_w = slot_w
            draw_h = slot_h

        if align_right:
            draw_x = slot_x + slot_w - draw_w
        else:
            draw_x = slot_x

        draw_y = slot_y + max(0, (slot_h - draw_h) // 2)

        return screen.setWidgetGeometry(
            widget_name,
            draw_x,
            draw_y,
            draw_w,
            draw_h
        )

    except Exception:
        return False


def jellydream_apply_item_logo(
    screen,
    item=None,
    widget_name="item_logo",
    text_widget_name="",
    hide_text_when_shown=False,
    fallback_id="",
    selection_check=None
):
    """
    Zeigt das Jellyfin-Titellogo über dem Backdrop.
    Ohne Logo bleibt der Texttitel sichtbar.
    """
    try:
        if widget_name not in screen:
            return False

        logo_widget = screen[widget_name]
        logo_id = jellydream_item_logo_id(item, fallback_id)
        request_id = logo_id or ""
        screen._jellydream_logo_request = request_id

        def show_text(visible):
            if not text_widget_name or text_widget_name not in screen:
                return

            text_widget = screen[text_widget_name]
            instance = getattr(text_widget, "instance", None)

            if not instance:
                return

            try:
                if visible:
                    instance.show()
                else:
                    instance.hide()
            except Exception:
                pass

        if not logo_id:
            jellydream_set_pixmap_if_changed(
                logo_widget,
                "",
                hide_if_missing=True
            )
            show_text(True)
            return False

        if callable(selection_check) and not selection_check():
            return False

        width = 960
        height = 320

        def remember_slot():
            slots = getattr(screen, "_jellydream_logo_slots", None)

            if not isinstance(slots, dict):
                slots = {}
                screen._jellydream_logo_slots = slots

            if widget_name in slots:
                return slots[widget_name]

            slot_instance = getattr(logo_widget, "instance", None)

            if not slot_instance:
                return None

            try:
                pos = slot_instance.position()
                size = slot_instance.size()
                slot_w = int(size.width())
                slot_h = int(size.height())

                if slot_w < 40 or slot_h < 16:
                    return None

                slots[widget_name] = (
                    int(pos.x()),
                    int(pos.y()),
                    slot_w,
                    slot_h
                )
            except Exception:
                return None

            return slots[widget_name]

        remember_slot()

        def still_current():
            if str(getattr(screen, "_jellydream_logo_request", "")) != request_id:
                return False

            if not getattr(screen, "instance", None):
                return False

            if callable(selection_check) and not selection_check():
                return False

            return True

        def apply_path(path):
            if not still_current():
                return

            jellydream_place_logo_widget(
                screen,
                widget_name,
                path,
                align_right=not hide_text_when_shown
            )

            if jellydream_set_pixmap_if_changed(
                logo_widget,
                path,
                hide_if_missing=True
            ):
                if hide_text_when_shown:
                    show_text(False)
                return

            show_text(True)

        cached_path = jellydream_get_cached_image_path(
            logo_id,
            width,
            height,
            "Logo"
        )

        if jellydream_valid_image_file(cached_path, 150):
            apply_path(cached_path)
            return True

        show_text(True)

        def logo_finished(path, loaded_item_id):
            if str(loaded_item_id or "") != request_id:
                return

            apply_path(path)

        jellydream_async_download_image(
            logo_id,
            jellydream_cache_dir("logo"),
            width=width,
            height=height,
            quality=90,
            image_type="Logo",
            callback=logo_finished
        )
        return True

    except Exception as e:
        jd_log(
            "Title logo could not be applied: %s" % e,
            level="DEBUG"
        )
        return False


def jellydream_apply_item_backdrop(
    screen,
    target_id=None,
    selection_check=None,
    fallback_to_default=True,
    item=None,
    item_id=None
):
    """
    Setzt ein Item-Backdrop aus dem Cache oder lädt es über die zentrale
    asynchrone Bilddownload-Logik.

    Während eines laufenden Downloads bleibt der bisherige Hintergrund
    sichtbar. Der Standardhintergrund wird erst verwendet, wenn kein
    gültiger Backdrop geladen werden konnte.
    """
    try:
        # Keine Backdrop-Cache-Prüfung, keine Downloads und kein Wechsel
        # auf langsamen Boxen bzw. bei deaktivierter Funktion.
        if not jellydream_dynamic_backdrops_enabled():
            set_jellydream_background(screen)
            return False    
        if not target_id:
            if isinstance(item, dict):
                target_id = (
                    item.get("SeriesId")
                    or item.get("ParentId")
                    or item.get("Id")
                    or ""
                )
            elif item_id:
                target_id = item_id

        target_id = str(
            target_id or ""
        ).strip()

        if not target_id:
            if fallback_to_default:
                jellydream_apply_resolved_background(screen)

            return False

        if (
            callable(selection_check)
            and not selection_check()
        ):
            return False

        try:
            screen._active_backdrop_id = target_id
        except Exception:
            pass

        background_width, background_height = (
            jellydream_get_background_widget_size(screen)
        )

        # Zuerst den zentral registrierten Bildpfad prüfen.
        cached_path = jellydream_get_cached_image_path(
            target_id,
            background_width,
            background_height,
            "Backdrop"
        )

        # Nach einem GUI-/Plugin-Neustart kann die Datei bereits physisch
        # vorhanden sein, obwohl der RAM-Pfadcache noch leer ist.
        if not jellydream_valid_image_file(
            cached_path,
            500
        ):
            cached_path = jellydream_cached_background_path(
                target_id,
                width=background_width,
                height=background_height,
                quality=90,
                image_type="Backdrop"
            )

        # Bereits vorhandenen Backdrop sofort anzeigen.
        if jellydream_valid_image_file(
            cached_path,
            500
        ):
            set_jellydream_background(
                screen,
                cached_path
            )
            return True

        requested_target_id = target_id

        def backdrop_finished(path, loaded_item_id):
            try:
                if not getattr(screen, "instance", None):
                    return

                loaded_item_id = str(
                    loaded_item_id or ""
                ).strip()

                if loaded_item_id != requested_target_id:
                    return

                # Ein später gestarteter Backdrop-Request hat Vorrang.
                active_backdrop_id = str(
                    getattr(
                        screen,
                        "_active_backdrop_id",
                        ""
                    ) or ""
                ).strip()

                if active_backdrop_id != requested_target_id:
                    return

                # Prüfen, ob der Eintrag weiterhin ausgewählt ist.
                if (
                    callable(selection_check)
                    and not selection_check()
                ):
                    return

                # Der Download ist abgeschlossen, hat aber kein gültiges
                # Bild geliefert. Erst jetzt auf den Standardhintergrund
                # zurückfallen.
                if not jellydream_valid_image_file(
                    path,
                    500
                ):
                    if fallback_to_default:
                        jellydream_apply_resolved_background(
                            screen
                        )

                    return

                # Erst das vollständig geladene und geprüfte Bild ersetzt
                # den bisher sichtbaren Hintergrund.
                set_jellydream_background(
                    screen,
                    path
                )

            except Exception as e:
                jd_log(
                    "Error while applying a backdrop: %s"
                    % e,
                    level="WARNING"
                )

        # Wichtig:
        # Vor diesem Download wird der aktuelle Hintergrund nicht verändert.
        # Er bleibt sichtbar, bis backdrop_finished() ein gültiges Bild
        # erhält.
        jellydream_async_download_image(
            requested_target_id,
            jellydream_cache_dir("cover"),
            width=background_width,
            height=background_height,
            image_type="Backdrop",
            callback=backdrop_finished
        )

        return True

    except Exception as e:
        jd_log(
            "Error in jellydream_apply_item_backdrop: %s"
            % e,
            level="WARNING"
        )

        if fallback_to_default:
            jellydream_apply_resolved_background(screen)

        return False

        
def jellydream_home_resume_background_id(item):
    try:
        if not isinstance(item, dict):
            return ""

        item_id = str(
            item.get("Id")
            or item.get("id")
            or ""
        ).strip()

        if item.get("Type") == "Episode":
            return str(
                item.get("SeriesId")
                or item_id
            ).strip()

        return item_id

    except Exception:
        return ""


def jellydream_apply_home_background_from_resume(
    screen,
    item,
    async_if_missing=True
):
    """
    Verwendet als Fullscreen-Hintergrund ausschließlich einen echten
    Backdrop in der tatsächlichen Größe des Background-Widgets.

    Kleine Resume-Vorschaubilder werden nicht auf Bildschirmgröße
    skaliert.
    """
    try:
        bg_item_id = str(
            jellydream_home_resume_background_id(item)
            or ""
        ).strip()

        if not bg_item_id:
            jellydream_apply_resolved_background(screen)
            return False

        background_width, background_height = (
            jellydream_get_background_widget_size(screen)
        )

        high_res_bg = jellydream_cached_background_path(
            bg_item_id,
            width=background_width,
            height=background_height,
            quality=90,
            image_type="Backdrop"
        )

        if jellydream_valid_file(high_res_bg, 500):
            return set_jellydream_background(
                screen,
                high_res_bg
            )

        if not async_if_missing:
            jellydream_apply_resolved_background(screen)
            return False

        return jellydream_apply_item_backdrop(
            screen,
            target_id=bg_item_id,
            fallback_to_default=True
        )

    except Exception as e:
        jd_log(
            "Error in jellydream_apply_home_background_from_resume: %s"
            % e,
            level="WARNING"
        )

        jellydream_apply_resolved_background(screen)
        return False


def jellydream_apply_home_background_from_movie(
    screen,
    item,
    async_if_missing=True
):
    """
    Setzt auf der Startseite ausschließlich echte Jellyfin-Backdrops.

    Ein hochformatiges Primary-Cover darf nicht als Hintergrund-Fallback
    verwendet werden, weil es im Background-Widget auf Bildschirmgröße
    gestreckt würde. Solange der Backdrop noch fehlt, bleibt deshalb der
    Standardhintergrund sichtbar.
    """
    try:
        if not isinstance(item, dict):
            jellydream_apply_resolved_background(screen)
            return False

        item_id = str(
            item.get("Id") or item.get("id") or ""
        ).strip()

        if not item_id:
            jellydream_apply_resolved_background(screen)
            return False

        background_width, background_height = (
            jellydream_get_background_widget_size(screen)
        )

        high_res_bg = jellydream_cached_background_path(
            item_id,
            width=background_width,
            height=background_height,
            quality=90,
            image_type="Backdrop"
        )

        # Bereits vorhandenen echten Backdrop ohne Verzögerung anzeigen.
        if jellydream_valid_file(high_res_bg, 500):
            set_jellydream_background(
                screen,
                high_res_bg
            )
            return True

        # Wichtig:
        # Kein Primary-Cover mit 230x318 als Hintergrund setzen.
        # Das Cover würde im Fullscreen-Hintergrund stark verzerrt.
        if not async_if_missing:
            jellydream_apply_resolved_background(screen)
            return False

        # Zentrale Backdrop-Logik verwenden. Sie kümmert sich um:
        # - asynchronen Bilddownload,
        # - Cache,
        # - zusammengeführte parallele Downloads,
        # - korrekte GUI-Rückgabe,
        # - Schutz davor, dass ein älterer Download den Hintergrund einer
        #   inzwischen neu ausgewählten Neuerscheinung überschreibt.
        return jellydream_apply_item_backdrop(
            screen,
            target_id=item_id,
            selection_check=lambda: (
                getattr(screen, "focusArea", "") == "poster"
                and 0 <= int(
                    getattr(screen, "currentPoster", -1)
                ) < len(
                    getattr(screen, "home_latest", []) or []
                )
                and str(
                    (
                        getattr(screen, "home_latest", [])[
                            int(
                                getattr(
                                    screen,
                                    "currentPoster",
                                    -1
                                )
                            )
                        ].get("Id")
                        or getattr(screen, "home_latest", [])[
                            int(
                                getattr(
                                    screen,
                                    "currentPoster",
                                    -1
                                )
                            )
                        ].get("id")
                        or ""
                    )
                    if isinstance(
                        getattr(screen, "home_latest", [])[
                            int(
                                getattr(
                                    screen,
                                    "currentPoster",
                                    -1
                                )
                            )
                        ],
                        dict
                    )
                    else ""
                ).strip() == item_id
            ),
            fallback_to_default=True
        )

    except Exception as e:
        jd_log(
            "Error in jellydream_apply_home_background_from_movie: %s"
            % e,
            level="WARNING"
        )

        jellydream_apply_resolved_background(screen)
        return False
