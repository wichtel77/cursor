# -*- coding: utf-8 -*-

import heapq
import json
import threading

try:
    from enigma import ePythonMessagePump
except ImportError:
    ePythonMessagePump = None

from .constants import (
    CONFIG_FILE,
    JOB_PRIORITY_API_INTERACTIVE,
    JOB_PRIORITY_IMAGE_NORMAL,
    JOB_PRIORITY_MAINTENANCE,
    JOB_PRIORITY_PLAYBACK
)
from .log import jd_log


# ==================== GETRENNTE HINTERGRUND-WORKER-POOLS ====================

def jellydream_initial_image_worker_count():
    """
    Liest die Image-Worker-Anzahl bereits beim Plugin-Import.

    read_config() steht an dieser Position noch nicht zur Verfügung,
    weil die Funktion weiter unten in der Datei definiert wird.
    Deshalb wird die JSON-Datei hier bewusst direkt und fehlertolerant
    gelesen.
    """
    try:
        with open(
            CONFIG_FILE,
            "r",
            encoding="utf-8"
        ) as config_file:
            config_data = json.load(config_file)

        if not isinstance(config_data, dict):
            return 2

        worker_count = int(
            config_data.get("image_worker_count", 2)
        )

        return 1 if worker_count <= 1 else 2

    except Exception:
        # Bestehendes Verhalten und sichere Voreinstellung.
        return 2



class JellyDreamWorkerPool:
    """
    Begrenzter Worker-Pool mit Prioritätswarteschlange.

    Kleine Prioritätswerte werden zuerst ausgeführt. Die laufende Aufgabe
    wird nicht unterbrochen; die Priorität gilt für wartende Aufgaben.

    Ist die Hauptwarteschlange voll, verdrängt ein wichtigerer Job den
    unwichtigsten wartenden Job. Der verdrängte Job und gleich- oder
    niedriger priorisierte neue Jobs landen in einer Overflow-Liste und
    werden nachgezogen, sobald wieder Platz ist. Erst wenn auch der
    Overflow voll ist, wird der unwichtigste Job wirklich verworfen.
    """

    def __init__(
        self,
        pool_name,
        worker_count=2,
        queue_size=64
    ):
        self.pool_name = str(
            pool_name or "Worker"
        )

        self.worker_count = max(
            1,
            int(worker_count or 1)
        )

        self.max_jobs = max(
            1,
            int(queue_size or 1)
        )

        # Gleich groß wie die Hauptwarteschlange, damit Lastspitzen
        # abgefangen werden, der Speicher aber begrenzt bleibt.
        self.max_overflow = self.max_jobs

        self._lock = threading.Lock()
        self._not_empty = threading.Condition(
            self._lock
        )
        self._pending = []
        self._overflow = []

        self._started = False
        self._start_lock = threading.Lock()
        self._sequence_lock = threading.Lock()
        self._sequence = 0

    def _next_sequence(self):
        with self._sequence_lock:
            self._sequence += 1
            return self._sequence

    def _job_sort_key(self, item):
        # Unwichtig zuerst: hohe Prioritätszahl, bei Gleichstand älter
        # (kleine Sequence), weil neuere Cover eher noch sichtbar sind.
        priority, sequence, _job = item
        return (
            int(priority),
            -int(sequence)
        )

    def _remove_worst_item(self, items):
        if not items:
            return None

        worst_item = max(
            items,
            key=self._job_sort_key
        )

        try:
            items.remove(worst_item)
        except ValueError:
            return None

        heapq.heapify(items)
        return worst_item

    def _promote_overflow_unlocked(self):
        while (
            self._overflow
            and len(self._pending) < self.max_jobs
        ):
            overflow_item = heapq.heappop(
                self._overflow
            )
            heapq.heappush(
                self._pending,
                overflow_item
            )

    def _park_in_overflow_unlocked(self, item):
        if len(self._overflow) < self.max_overflow:
            heapq.heappush(
                self._overflow,
                item
            )
            return True

        worst_overflow_item = max(
            self._overflow,
            key=self._job_sort_key
        )

        if self._job_sort_key(item) < self._job_sort_key(
            worst_overflow_item
        ):
            self._remove_worst_item(self._overflow)
            heapq.heappush(
                self._overflow,
                item
            )

            jd_log(
                "JellyDream %s pool: a less important overflow job was replaced by a more important job"
                % self.pool_name,
                level="DEBUG"
            )

            return True

        jd_log(
            "JellyDream %s pool is busy; task was dropped"
            % self.pool_name,
            level="WARNING"
        )

        return False

    def _get_job(self):
        with self._not_empty:
            while True:
                self._promote_overflow_unlocked()

                if self._pending:
                    item = heapq.heappop(
                        self._pending
                    )
                    self._promote_overflow_unlocked()
                    return item

                self._not_empty.wait()

    def _start_workers(self):
        with self._start_lock:
            if self._started:
                return

            self._started = True

            for index in range(self.worker_count):
                worker = threading.Thread(
                    target=self._worker_loop,
                    name="JellyDream%s-%d" % (
                        self.pool_name,
                        index + 1
                    )
                )

                worker.daemon = True
                worker.start()

            jd_log(
                "JellyDream %s pool started: %d workers"
                % (
                    self.pool_name,
                    self.worker_count
                ),
                level="DEBUG"
            )

    def _worker_loop(self):
        while True:
            _priority, _sequence, job = self._get_job()

            try:
                if callable(job):
                    job()

            except Exception as e:
                jd_log(
                    "Error in the JellyDream %s pool: %s"
                    % (
                        self.pool_name,
                        e
                    ),
                    level="WARNING"
                )

    def submit(
        self,
        job,
        priority=JOB_PRIORITY_API_INTERACTIVE
    ):
        if not callable(job):
            return False

        self._start_workers()

        item = (
            int(priority),
            self._next_sequence(),
            job
        )

        with self._not_empty:
            if len(self._pending) < self.max_jobs:
                heapq.heappush(
                    self._pending,
                    item
                )
                self._not_empty.notify()
                return True

            worst_pending_item = max(
                self._pending,
                key=self._job_sort_key
            )

            if item[0] < worst_pending_item[0]:
                self._remove_worst_item(self._pending)
                heapq.heappush(
                    self._pending,
                    item
                )
                self._not_empty.notify()

                parked = self._park_in_overflow_unlocked(
                    worst_pending_item
                )

                if parked:
                    jd_log(
                        "JellyDream %s pool: a more important job replaced a waiting job"                        % self.pool_name,
                        level="DEBUG"
                    )

                return True

            return self._park_in_overflow_unlocked(item)



# Bilder können API- und Playbackjobs nicht mehr blockieren.
#
# Die Anzahl wird beim GUI-/Pluginstart aus der Konfiguration gelesen.
# Eine spätere Änderung in den Einstellungen benötigt daher einen
# GUI-Neustart, damit der Pool mit der neuen Anzahl erzeugt wird.
_JELLYDREAM_IMAGE_POOL = JellyDreamWorkerPool(
    pool_name="Image",
    worker_count=jellydream_initial_image_worker_count(),
    queue_size=96
)


# Backdrops dürfen nicht hinter vielen Poster-Downloads warten.
# Ein einzelner Worker begrenzt gleichzeitig die Netz- und CPU-Last.
_JELLYDREAM_BACKDROP_POOL = JellyDreamWorkerPool(
    pool_name="Backdrop",
    worker_count=1,
    queue_size=24
)


# Interaktive API-Jobs und Playback-Vorbereitung teilen sich einen Pool.
# Playback erhält darin die höchste Priorität.
_JELLYDREAM_API_POOL = JellyDreamWorkerPool(
    pool_name="API",
    worker_count=2,
    queue_size=64
)


# Verbindungsprüfungen dürfen nicht hinter langen Detail-, Startseiten-
# oder Bibliotheksjobs warten.
_JELLYDREAM_HEALTH_POOL = JellyDreamWorkerPool(
    pool_name="Health",
    worker_count=1,
    queue_size=4
)


def jellydream_submit_health(job):
    return _JELLYDREAM_HEALTH_POOL.submit(
        job,
        priority=JOB_PRIORITY_PLAYBACK
    )

    
def jellydream_submit_image(
    job,
    priority=JOB_PRIORITY_IMAGE_NORMAL
):
    return _JELLYDREAM_IMAGE_POOL.submit(
        job,
        priority=priority
    )


def jellydream_submit_backdrop(
    job,
    priority=JOB_PRIORITY_IMAGE_NORMAL
):
    return _JELLYDREAM_BACKDROP_POOL.submit(
        job,
        priority=priority
    )

    
def jellydream_submit_api(
    job,
    priority=JOB_PRIORITY_API_INTERACTIVE
):
    return _JELLYDREAM_API_POOL.submit(
        job,
        priority=priority
    )



def jellydream_submit_playback(job):
    return jellydream_submit_api(
        job,
        priority=JOB_PRIORITY_PLAYBACK
    )



def jellydream_submit_maintenance(job):
    return jellydream_submit_api(
        job,
        priority=JOB_PRIORITY_MAINTENANCE
    )

_ASYNC_GUI_LOCK = threading.Lock()

_ASYNC_GUI_QUEUE = []

_GUI_THREAD_ID = threading.current_thread().ident


try:
    _ASYNC_GUI_PUMP = ePythonMessagePump()
except Exception:
    _ASYNC_GUI_PUMP = None

        
def _jellydream_process_gui_queue(*args):
    callbacks = []

    try:
        with _ASYNC_GUI_LOCK:
            callbacks = list(_ASYNC_GUI_QUEUE)
            del _ASYNC_GUI_QUEUE[:]
    except Exception:
        callbacks = []

    for callback in callbacks:
        try:
            callback()
        except Exception as e:
            jd_log(
                "Error in the GUI callback for an image: %s"% e,
                level="DEBUG"
            )


try:
    if _ASYNC_GUI_PUMP:
        _ASYNC_GUI_PUMP.recv_msg.get().append(
            _jellydream_process_gui_queue
        )
except Exception as e:
    jd_log(
        "ePythonMessagePump could not be set up: %s" % e,
        level="WARNING"
    )


def jellydream_is_gui_thread():
    try:
        return threading.current_thread().ident == _GUI_THREAD_ID
    except Exception:
        return False


def jellydream_dispatch_to_gui(callback):
    if not callable(callback):
        return False

    try:
        if jellydream_is_gui_thread():
            callback()
            return True

        if not _ASYNC_GUI_PUMP:
            jd_log(
                "GUI callback dropped: ePythonMessagePump is not available",
                level="ERROR"
            )
            return False

        with _ASYNC_GUI_LOCK:
            _ASYNC_GUI_QUEUE.append(callback)

        _ASYNC_GUI_PUMP.send(0)
        return True

    except Exception as e:
        jd_log(
            "GUI dispatch failed: %s" % e,
            level="WARNING"
        )
        return False
