"""Central bounded-worker download queue for PEAK."""
import queue, threading, time


class DownloadQueue:
    """Fixed-pool job executor. Jobs are callables; statuses tracked per id.
    Thread-safe. Cancel is global (matches PEAK's existing cancel model)."""

    def __init__(self, workers=3):
        self.q = queue.Queue()
        self.workers = []
        self.lock = threading.Lock()
        self.active = {}     # jid -> {"status","label","progress","error"}
        self.results = []    # terminal-state snapshots (newest last)
        self.cancel = threading.Event()
        self._start(workers)

    def _start(self, n):
        for i in range(n):
            t = threading.Thread(target=self._worker, daemon=True,
                                 name=f"peak-w{len(self.workers)}")
            t.start()
            self.workers.append(t)

    def resize(self, n):
        """Grow pool instantly; shrink happens lazily as idle workers exit."""
        n = max(1, min(10, int(n)))
        cur = len(self.workers)
        if n > cur:
            self._start(n - cur)
        elif n < cur:
            for _ in range(cur - n):
                self.q.put(None)   # poison pill: idle worker exits

    def submit(self, fn, *args, job_id=None, label="", on_done=None, **kwargs):
        jid = job_id or f"job-{int(time.time() * 1000)}-{id(fn) % 9999}"
        with self.lock:
            self.active[jid] = {"status": "queued", "label": label,
                                "progress": 0.0, "error": None,
                                "fn": fn, "args": args, "kwargs": kwargs}
        self.q.put((jid, fn, args, kwargs, on_done))
        return jid

    def cancel_all(self):
        self.cancel.set()

    def reset_cancel(self):
        self.cancel.clear()

    def pending(self):
        with self.lock:
            return sum(1 for s in self.active.values()
                       if s["status"] in ("queued", "running"))

    def snapshot(self):
        with self.lock:
            return {k: dict(v) for k, v in self.active.items()}

    def _worker(self):
        while True:
            item = self.q.get()
            if item is None:
                break
            jid, fn, args, kwargs, on_done = item
            if self.cancel.is_set():
                with self.lock:
                    st = self.active.get(jid)
                    if st and st["status"] == "queued":
                        st["status"] = "cancelled"
                        self.results.append(dict(st))
                continue
            with self.lock:
                st = self.active.get(jid)
                if st:
                    st["status"] = "running"
            err = None
            try:
                fn(*args, **kwargs)
            except Exception as e:
                err = f"{type(e).__name__}: {e}"[:160]
            with self.lock:
                st = self.active.get(jid)
                if st:
                    st["status"] = ("error" if err else
                                    "cancelled" if self.cancel.is_set() else "done")
                    if err:
                        st["error"] = err
                    self.results.append(dict(st))
            if on_done:
                try:
                    on_done(jid, err)
                except Exception:
                    pass
