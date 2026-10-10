"""Single-process persistent job queue for the video generation API."""
from __future__ import annotations

import json
import re
import threading
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from pathlib import Path

from .modules.pipeline import run_pipeline
from .modules.utils import log

JOB_ID_RE = re.compile(r"^[\w-]{6,64}$")


@dataclass
class Job:
    id: str
    params: dict
    batch_id: str = ""
    batch_index: int = 1
    batch_size: int = 1
    status: str = "queued"  # queued | running | done | error
    step: str = "queued"
    progress: float = 0.0
    message: str = ""
    result: dict | None = None
    error: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    def to_dict(self) -> dict:
        return asdict(self)


class JobStore:
    def __init__(self, cfg):
        self.cfg = cfg
        self.root = Path(cfg.OUTPUT_DIR)
        self.jobs_dir = self.root / ".jobs"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self._jobs: dict[str, Job] = {}
        self._lock = threading.RLock()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="video-job")
        self._load_jobs()

    def _job_path(self, job_id: str) -> Path:
        return self.jobs_dir / f"{job_id}.json"

    def _save(self, job: Job) -> None:
        job.updated_at = datetime.now().isoformat(timespec="seconds")
        path = self._job_path(job.id)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(job.to_dict(), ensure_ascii=False), encoding="utf-8")
        temp.replace(path)

    def _load_jobs(self) -> None:
        for path in self.jobs_dir.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                job = Job(**data)
                if job.status in {"queued", "running"}:
                    job.status = "error"
                    job.step = "interrupted"
                    job.error = "The service restarted before this video job completed. Submit it again."
                    self._save(job)
                self._jobs[job.id] = job
            except (OSError, ValueError, TypeError):
                log.exception("Could not restore job record %s", path)

    def _usage_path(self) -> Path:
        return self.root / "usage.json"

    def used_today(self) -> int:
        try:
            return int(json.loads(self._usage_path().read_text(encoding="utf-8")).get(str(date.today()), 0))
        except (OSError, ValueError, TypeError):
            return 0

    def submit_batch(self, params: dict, count: int, daily_limit: int) -> tuple[str, list[Job]]:
        batch_id = uuid.uuid4().hex
        jobs = [
            Job(
                id=f"{datetime.now():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}",
                params=dict(params), batch_id=batch_id, batch_index=i + 1, batch_size=count,
            )
            for i in range(count)
        ]
        with self._lock:
            usage = {}
            try:
                usage = json.loads(self._usage_path().read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                pass
            today = str(date.today())
            used = int(usage.get(today, 0))
            if used + count > daily_limit:
                raise ValueError(f"Daily video limit of {daily_limit} reached; {used} video(s) have been submitted today.")
            usage[today] = used + count
            temp = self._usage_path().with_suffix(".tmp")
            temp.write_text(json.dumps(usage), encoding="utf-8")
            temp.replace(self._usage_path())
            for job in jobs:
                self._jobs[job.id] = job
                self._save(job)
        for job in jobs:
            self._pool.submit(self._run, job)
        return batch_id, jobs

    def _run(self, job: Job) -> None:
        with self._lock:
            job.status, job.step = "running", "starting"
            self._save(job)

        def progress(step: str, frac: float, msg: str = "") -> None:
            with self._lock:
                job.step = step
                job.progress = round(max(0.0, min(1.0, frac)), 3)
                job.message = msg
                self._save(job)

        try:
            result = run_pipeline(job.params, self.cfg, self.root / job.id, progress)
            with self._lock:
                job.result = result
                job.status, job.step, job.progress = "done", "done", 1.0
                self._save(job)
        except Exception as exc:
            log.error("job %s failed: %s\n%s", job.id, exc, traceback.format_exc())
            with self._lock:
                job.status, job.step, job.error = "error", "error", str(exc)
                self._save(job)

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            job = self._jobs.get(job_id)
            return Job(**job.to_dict()) if job else None

    def batch(self, batch_id: str) -> dict | None:
        with self._lock:
            jobs = sorted((j for j in self._jobs.values() if j.batch_id == batch_id), key=lambda j: j.batch_index)
            if not jobs:
                return None
            counts = {state: sum(j.status == state for j in jobs) for state in ("queued", "running", "done", "error")}
            return {"batch_id": batch_id, "count": len(jobs), "counts": counts,
                    "completed": counts["done"] + counts["error"],
                    "progress": round(sum(j.progress for j in jobs) / len(jobs), 3),
                    "status": "running" if counts["queued"] or counts["running"] else ("error" if counts["error"] else "done"),
                    "jobs": [j.to_dict() for j in jobs]}

    def counts(self) -> dict:
        with self._lock:
            return {state: sum(j.status == state for j in self._jobs.values())
                    for state in ("queued", "running", "done", "error")}
