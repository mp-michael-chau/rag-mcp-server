"""In-process FIFO job registry with a single worker thread.

Docling conversion is memory-heavy and both conversion and ingestion hammer the
local Ollama service, so jobs run one at a time in submission order.

The registry is in-memory only: it is lost when the process restarts. That is
acceptable because PDF conversion resumes from its `.progress` sidecar files and
ingestion is idempotent (deterministic uuid5 point IDs + per-file filter delete).

The registry also answers "is a conversion currently targeting this review
file?" so the API can reject concurrent edits (409) — jobs count as targeting a
file from the moment they are *queued*, not just once running, to avoid races
between "start conversion" and "save edit" clicks.
"""

import os
import queue
import threading
import time
import uuid

MAX_FINISHED_JOBS = 100  # prune finished jobs beyond this many
LOG_TAIL = 50  # lines exposed per job
SUBSCRIBER_MAXSIZE = 1000  # per-subscriber event backlog; overflow drops (reconnect snapshot reconciles)


class Job:
    def __init__(self, job_type, target, project_id=None, name=None):
        self.id = uuid.uuid4().hex[:12]
        self.type = job_type  # "convert" | "ingest" | "index_pages"
        self.target = target  # human-readable target description
        self.project_id = project_id
        self.name = name  # specific file name, or None for project-wide/all
        self.status = "queued"  # queued | running | done | error
        self.progress = ""  # e.g. "37/536 pages" | "512/1200 chunks"
        self.log = []
        self.error = None
        self.created_at = time.time()
        self.started_at = None
        self.finished_at = None
        self._run = None

    def to_dict(self):
        return {
            "id": self.id,
            "type": self.type,
            "target": self.target,
            "project_id": self.project_id,
            "name": self.name,
            "status": self.status,
            "progress": self.progress,
            "log": self.log[-LOG_TAIL:],
            "error": self.error,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


class JobContext:
    """Handle given to job run functions for progress/log updates (thread-safe)."""

    def __init__(self, manager, job):
        self._manager = manager
        self._job = job

    def set_progress(self, text):
        with self._manager._lock:
            self._job.progress = text
            self._manager._notify_locked(self._job)

    def log(self, text):
        with self._manager._lock:
            self._job.log.append(text)


class JobManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._jobs = {}
        self._order = []
        self._queue = queue.Queue()
        self._subscribers = []  # SSE subscriber queues, guarded by _lock
        self._worker = threading.Thread(target=self._worker_loop, name="rag-job-worker", daemon=True)
        self._worker.start()

    def subscribe(self) -> queue.Queue:
        """Register an SSE subscriber; returns a queue job snapshots are fanned out to.
        Events published between subscribe() and the endpoint's initial snapshot
        are retained in the queue, so the stream is at-least-once."""
        q = queue.Queue(maxsize=SUBSCRIBER_MAXSIZE)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q) -> None:
        with self._lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass

    def _notify_locked(self, job) -> None:
        """Fan a job snapshot out to every subscriber. Caller must hold _lock.
        put_nowait never blocks; a full queue means a slow/stuck consumer, and
        dropping is fine — its reconnect snapshot reconciles."""
        if not self._subscribers:
            return
        data = job.to_dict()
        for q in self._subscribers:
            try:
                q.put_nowait(data)
            except queue.Full:
                pass

    def submit(self, job_type, run_fn, target, project_id=None, name=None):
        """Register + enqueue a job. Registration happens synchronously under the lock,
        so immediately after submit() the job is already visible to blocks_review_edit()."""
        job = Job(job_type, target, project_id=project_id, name=name)
        job._run = run_fn
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            self._prune_locked()
            self._notify_locked(job)  # queued event: makes instant jobs visible without polling
        self._queue.put(job.id)
        return job

    def get(self, job_id):
        with self._lock:
            return self._jobs.get(job_id)

    def list(self, active_only=False):
        with self._lock:
            jobs = [self._jobs[jid] for jid in self._order]
        if active_only:
            jobs = [j for j in jobs if j.status in ("queued", "running")]
        return jobs

    def _active_jobs(self, job_type):
        with self._lock:
            return [j for j in self._jobs.values() if j.type == job_type and j.status in ("queued", "running")]

    def blocks_review_edit(self, project_id, review_name):
        """True if a queued or running convert job targets this review file.
        A project-wide convert job (name=None) blocks every review file of that project;
        a library-wide convert job (project_id=None) blocks every review file."""
        stem = os.path.splitext(review_name)[0]
        for job in self._active_jobs("convert"):
            if job.project_id is not None and job.project_id != project_id:
                continue
            if job.name is None or os.path.splitext(job.name)[0] == stem:
                return True
        return False

    def blocks_document_delete(self, project_id, name):
        """True if a queued or running ingest or index_pages job targets this approved document
        (exact name or a project-wide/library-wide job), so deleting it now would be undone by
        the job's upsert (or leave page points pointing at a deleted source)."""
        for job_type in ("ingest", "index_pages"):
            for job in self._active_jobs(job_type):
                if job.project_id is not None and job.project_id != project_id:
                    continue
                if job.name is None or job.name == name:
                    return True
        return False

    def _prune_locked(self):
        finished = [jid for jid in self._order if self._jobs[jid].status in ("done", "error")]
        excess = len(finished) - MAX_FINISHED_JOBS
        if excess > 0:
            doomed = set(finished[:excess])
            for jid in doomed:
                self._jobs.pop(jid, None)
            self._order = [jid for jid in self._order if jid not in doomed]

    def _worker_loop(self):
        while True:
            job_id = self._queue.get()
            try:
                with self._lock:
                    job = self._jobs.get(job_id)
                if job is None or job._run is None:
                    continue
                with self._lock:
                    job.status = "running"
                    job.started_at = time.time()
                    self._notify_locked(job)
                ctx = JobContext(self, job)
                try:
                    job._run(ctx)
                    with self._lock:
                        job.status = "done"
                        job.finished_at = time.time()
                        job.progress = ""
                        self._notify_locked(job)
                except Exception as e:  # noqa: BLE001 — job errors are reported, never kill the worker
                    with self._lock:
                        job.status = "error"
                        job.error = str(e)
                        job.finished_at = time.time()
                        job.log.append(f"Error: {e}")
                        self._notify_locked(job)
            finally:
                self._queue.task_done()
