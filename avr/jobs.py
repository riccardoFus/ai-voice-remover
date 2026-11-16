"""Gestione dei job di separazione: coda, processi, persistenza e pulizia."""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from pathlib import Path

from .config import (
    DENOISE_MODEL_LABEL,
    DENOISE_OUTPUT_FORMATS,
    DENOISE_PRESETS,
    MODELS,
    POLISH_PRESETS,
    QUALITY_PRESETS,
    ROOT,
    OUTPUT_FORMATS,
    STEM_LABELS,
    Settings,
)

logger = logging.getLogger("avr.jobs")

ACTIVE_STATUSES = {"queued", "processing"}
JOB_ID_LENGTH = 10
MAX_LOG_TAIL = 4000


class JobNotFound(Exception):
    pass


class JobBusy(Exception):
    pass


class MissingInput(Exception):
    pass


class KeyNotFound(Exception):
    pass


def new_job_id() -> str:
    return uuid.uuid4().hex[:JOB_ID_LENGTH]


def sanitize_name(name: str, fallback: str = "audio") -> str:
    """Rende un nome file sicuro per il download, mantenendo la leggibilità."""
    name = (name or "").replace("\\", "/").split("/")[-1]
    cleaned = "".join(
        ch for ch in name
        if ch.isprintable() and ch not in '<>:"/\\|?*\x00' and ch not in "\r\n\t"
    ).strip(" .")
    cleaned = " ".join(cleaned.split())
    if not cleaned:
        cleaned = fallback
    if len(cleaned) > 120:
        stem, dot, ext = cleaned.rpartition(".")
        if dot and len(ext) <= 6:
            cleaned = stem[: 120 - len(ext) - 1] + "." + ext
        else:
            cleaned = cleaned[:120]
    return cleaned


def _tail(path: Path, max_bytes: int = 8000, lines: int = 14) -> str:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            data = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""
    chunks = [line.strip() for line in data.splitlines() if line.strip()]
    return " | ".join(chunks[-lines:])[:MAX_LOG_TAIL]


class JobStore:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.settings.ensure_dirs()

        self._lock = threading.RLock()
        self._wake = threading.Condition(self._lock)
        self._jobs: dict[str, dict] = {}
        self._queue: deque[str] = deque()
        self._procs: dict[str, subprocess.Popen] = {}
        self._stop = threading.Event()
        self._stopping = False
        self._last_save = 0.0
        self._workers: list[threading.Thread] = []
        self._cleaner: threading.Thread | None = None

        self._state_file = self.settings.data_dir / "jobs.json"
        self._load()
        self._start_workers()
        self._start_cleaner()

    # ── Persistenza ──────────────────────────────────────────────────────────
    def _load(self) -> None:
        if not self._state_file.exists():
            return
        try:
            raw = json.loads(self._state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("Impossibile leggere %s: %s", self._state_file, exc)
            return

        restored = 0
        for item in raw.get("jobs", []):
            if not isinstance(item, dict) or not item.get("id"):
                continue
            job = item
            job.setdefault("status", "error")
            job.setdefault("progress", 0)
            job.setdefault("message", "")
            job.setdefault("files", {})
            job.setdefault("error", None)
            job.setdefault("kind", "separate")
            job.setdefault("media", "audio")
            job.setdefault("quality", self.settings.default_quality)
            job.setdefault("format", self.settings.default_format)
            job.setdefault("model", self.settings.default_model)
            job.setdefault("created_at", time.time())

            if job["status"] in ACTIVE_STATUSES:
                if self._input_path(job).exists():
                    job.update(
                        status="queued",
                        progress=0,
                        stage="queued",
                        message="Ripristinato dopo il riavvio · in coda",
                        error=None,
                        started_at=None,
                        finished_at=None,
                    )
                    self._queue.append(job["id"])
                    restored += 1
                else:
                    job.update(
                        status="error",
                        stage="error",
                        message="Il file di input non è più disponibile dopo il riavvio.",
                        error="File di input mancante",
                        finished_at=time.time(),
                    )
            elif job["status"] == "completed":
                missing = [
                    info
                    for info in job.get("files", {}).values()
                    if not (self._job_dir(job["id"]) / info.get("file", "")).is_file()
                ]
                if missing:
                    job.update(
                        status="error",
                        stage="error",
                        message="I file di output non sono più disponibili.",
                        error="File di output mancanti",
                    )

            self._jobs[job["id"]] = job

        if restored:
            logger.info("%s job ripristinati nella coda", restored)
            self._save(force=True)

    def _save(self, force: bool = False) -> None:
        with self._lock:
            now = time.time()
            if not force and now - self._last_save < 2.5:
                return
            self._last_save = now
            payload = {
                "version": 1,
                "saved_at": now,
                "jobs": list(self._jobs.values()),
            }
            tmp = self._state_file.with_suffix(".json.tmp")
            try:
                self.settings.data_dir.mkdir(parents=True, exist_ok=True)
                with open(tmp, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, ensure_ascii=False)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, self._state_file)
            except OSError as exc:
                logger.warning("Salvataggio stato job fallito: %s", exc)

    # ── Percorsi ─────────────────────────────────────────────────────────────
    def _job_dir(self, job_id: str) -> Path:
        return self.settings.outputs_dir / job_id

    def _input_path(self, job: dict) -> Path:
        return self.settings.uploads_dir / job.get("input", "")

    def _log_path(self, job_id: str) -> Path:
        return self.settings.logs_dir / f"{job_id}.log"

    # ── API interna ──────────────────────────────────────────────────────────
    def submit(
        self,
        tmp_path: Path,
        original_name: str,
        options: dict,
        size: int,
        duration: float | None,
        kind: str = "separate",
        media: str = "audio",
    ) -> dict:
        job_id = new_job_id()
        safe_original = sanitize_name(original_name)
        ext = Path(safe_original).suffix.lower().lstrip(".") or "audio"
        input_name = f"{job_id}.{ext}"
        input_path = self.settings.uploads_dir / input_name
        os.replace(tmp_path, input_path)

        job = {
            "id": job_id,
            "name": safe_original,
            "input": input_name,
            "size": int(size),
            "duration": duration,
            "kind": kind,
            "media": media,
            "model": options.get("model"),
            "quality": options.get("quality"),
            "format": options.get("format"),
            "strength": options.get("strength"),
            "strength_db": options.get("strength_db"),
            "residual": options.get("residual"),
            "source_job": options.get("source_job"),
            "target": options.get("target"),
            "mode": options.get("mode"),
            "hum_mode": options.get("hum_mode"),
            "status": "queued",
            "stage": "queued",
            "progress": 0.0,
            "message": "In coda · in attesa dello slot libero",
            "error": None,
            "files": {},
            "created_at": time.time(),
            "started_at": None,
            "finished_at": None,
            "eta_seconds": None,
            "device": "cpu" if kind == "denoise" else self.settings.device,
        }

        with self._lock:
            self._jobs[job_id] = job
            self._queue.append(job_id)
            self._save(force=True)
            self._wake.notify_all()

        logger.info("Job %s accodato: %s (%s)", job_id, safe_original, options)
        return self._public(job)

    def _start_workers(self) -> None:
        for index in range(max(1, self.settings.concurrency)):
            thread = threading.Thread(
                target=self._worker_loop,
                name=f"avr-worker-{index + 1}",
                daemon=True,
            )
            thread.start()
            self._workers.append(thread)

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            job: dict | None = None
            with self._lock:
                while not self._queue and not self._stop.is_set():
                    self._wake.wait(timeout=1.0)
                if self._stop.is_set():
                    return
                while self._queue:
                    job_id = self._queue.popleft()
                    candidate = self._jobs.get(job_id)
                    if candidate and candidate.get("status") == "queued":
                        job = candidate
                        break
                if job is None:
                    continue
                job.update(
                    status="processing",
                    stage="preparing",
                    progress=1.0,
                    message=(
                        "Avvio della pulizia AI…"
                        if job.get("kind") == "denoise"
                        else "Avvio della pulizia dei residui…"
                        if job.get("kind") == "polish"
                        else "Avvio della separazione…"
                    ),
                    started_at=time.time(),
                    error=None,
                    eta_seconds=None,
                )
                self._save(force=True)

            try:
                self._execute(job)
            except Exception as exc:  # noqa: BLE001
                logger.exception("Errore nel worker per il job %s", job.get("id"))
                with self._lock:
                    if job.get("status") not in {"cancelled", "completed"}:
                        job.update(
                            status="error",
                            stage="error",
                            message="Errore interno del server durante l'elaborazione.",
                            error=str(exc)[:MAX_LOG_TAIL],
                            finished_at=time.time(),
                            eta_seconds=None,
                        )
                        self._save(force=True)

    def _execute(self, job: dict) -> None:
        job_id = job["id"]
        job_dir = self._job_dir(job_id)
        shutil.rmtree(job_dir, ignore_errors=True)
        job_dir.mkdir(parents=True, exist_ok=True)

        input_path = self._input_path(job)
        log_path = self._log_path(job_id)
        manifest_path = job_dir / "result.json"

        if job.get("kind") == "denoise":
            if not self.settings.denoise_available:
                self._fail(
                    job,
                    "Motore denoise non installato. Esegui ./install.sh --denoise e riprova.",
                    None,
                )
                return
            cmd = [
                str(self.settings.denoise_python),
                str(ROOT / "scripts" / "denoise_worker.py"),
                "--input",
                str(input_path),
                "--out",
                str(job_dir),
                "--format",
                job["format"],
                "--strength-db",
                str(job.get("strength_db") or 8.0),
                "--media",
                job.get("media", "audio"),
                "--max-minutes",
                str(self.settings.denoise_max_minutes),
            ]
        elif job.get("kind") == "polish":
            cmd = [
                sys.executable,
                "-m",
                "avr.polish_worker",
                "--input",
                str(input_path),
                "--out",
                str(job_dir),
                "--model",
                job["model"],
                "--quality",
                job["quality"],
                "--format",
                job["format"],
                "--residual",
                str(job.get("residual") or 0.6),
                "--mode",
                job.get("mode", "both"),
                "--hum-mode",
                job.get("hum_mode", "auto"),
                "--target",
                job.get("target", "instrumental"),
                "--device",
                self.settings.device,
            ]
        else:
            cmd = [
                sys.executable,
                "-m",
                "avr.worker",
                "--input",
                str(input_path),
                "--out",
                str(job_dir),
                "--model",
                job["model"],
                "--quality",
                job["quality"],
                "--format",
                job["format"],
                "--device",
                self.settings.device,
            ]
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONUTF8"] = "1"
        env.setdefault("TOKENIZERS_PARALLELISM", "false")

        logger.info("Avvio worker per %s: %s", job_id, " ".join(cmd))
        log_handle = open(log_path, "wb", buffering=0)
        timer: threading.Timer | None = None
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(ROOT),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=log_handle,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                start_new_session=True,
            )
            with self._lock:
                self._procs[job_id] = proc

            if self.settings.job_timeout > 0:
                timer = threading.Timer(
                    self.settings.job_timeout,
                    self._on_timeout,
                    args=(job_id,),
                )
                timer.daemon = True
                timer.start()

            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                self._handle_event(job, event)

            returncode = proc.wait()
        finally:
            if timer is not None:
                timer.cancel()
            with self._lock:
                self._procs.pop(job_id, None)
            try:
                log_handle.close()
            except OSError:
                pass

        with self._lock:
            cancelled = job.get("status") == "cancelled"

        if cancelled:
            shutil.rmtree(job_dir, ignore_errors=True)
            with self._lock:
                job.update(
                    progress=0,
                    eta_seconds=None,
                    message="Elaborazione annullata",
                    stage="cancelled",
                    finished_at=time.time(),
                )
                self._save(force=True)
            return

        if returncode == 0 and manifest_path.is_file():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                self._fail(job, f"Impossibile leggere il risultato: {exc}", log_path)
                return
            self._complete(job, manifest)
            return

        message = job.get("error") or ""
        if not message or message == "Elaborazione annullata":
            message = (
                "Elaborazione non riuscita. Controlla il log per i dettagli."
                if returncode != 0
                else "Il worker non ha prodotto alcun risultato."
            )
            tail = _tail(log_path, lines=6)
            if tail:
                logger.error("Job %s fallito (codice %s): %s", job_id, returncode, tail)
        self._fail(job, message, log_path)

    def _handle_event(self, job: dict, event: dict) -> None:
        if not isinstance(event, dict):
            return
        with self._lock:
            if job.get("status") != "processing":
                return
            kind = event.get("event")
            now = time.time()

            if kind in {"status", "progress"}:
                try:
                    value = float(event.get("progress", job.get("progress", 0)))
                except (TypeError, ValueError):
                    value = job.get("progress", 0)
                value = max(value, float(job.get("progress", 0)))
                job["progress"] = round(min(value, 100.0), 1)
                if event.get("message"):
                    job["message"] = str(event["message"])[:300]
                if event.get("stage"):
                    job["stage"] = str(event["stage"])[:40]

                started = job.get("started_at") or now
                elapsed = max(0.0, now - started)
                if job["progress"] >= 5 and elapsed > 3:
                    estimate = elapsed * (100.0 - job["progress"]) / max(job["progress"], 1)
                    previous = job.get("eta_seconds")
                    if previous:
                        estimate = previous * 0.65 + estimate * 0.35
                    job["eta_seconds"] = round(max(1.0, min(estimate, 86400.0)))
                self._save()
            elif kind == "error":
                job["error"] = str(event.get("message") or "Errore sconosciuto")[:MAX_LOG_TAIL]
            elif kind == "cancelled":
                job["error"] = "Elaborazione annullata"

    def _complete(self, job: dict, manifest: dict) -> None:
        files = manifest.get("files", {})
        is_denoise = job.get("kind") == "denoise"
        is_polish = job.get("kind") == "polish"
        if not files:
            message = (
                "La pulizia non ha prodotto alcun file."
                if is_denoise or is_polish
                else "La separazione non ha prodotto file."
            )
            self._fail(job, message, self._log_path(job["id"]))
            return
        with self._lock:
            job.update(
                status="completed",
                stage="done",
                progress=100.0,
                message=(
                    "Ronzio rimosso"
                    if is_polish and job.get("mode") == "hum"
                    else "Pulizia residui completata"
                    if is_polish
                    else "Pulizia completata"
                    if is_denoise
                    else "Separazione completata"
                ),
                error=None,
                finished_at=time.time(),
                eta_seconds=0,
                files=files,
                duration=manifest.get("duration", job.get("duration")),
                samplerate=manifest.get("samplerate"),
                result={
                    "kind": manifest.get("kind", job.get("kind", "separate")),
                    "duration": manifest.get("duration"),
                    "samplerate": manifest.get("samplerate"),
                    "elapsed": manifest.get("elapsed"),
                    "model_label": manifest.get("model_label"),
                    "device": manifest.get("device"),
                    "sources": manifest.get("sources", []),
                    "output_media": manifest.get("output_media", "audio"),
                    "strength_db": manifest.get("strength_db"),
                    "video_reencoded": manifest.get("video_reencoded", False),
                    "target": manifest.get("target"),
                    "residual": manifest.get("residual"),
                    "source_job": job.get("source_job"),
                    "mode": manifest.get("mode"),
                    "hum_mode": manifest.get("hum_mode"),
                    "hum_hz": manifest.get("hum_hz"),
                    "hum_applied": manifest.get("hum_applied"),
                    "hum_score_db": manifest.get("hum_score_db"),
                    "hum_bands": manifest.get("hum_bands"),
                    "hum_gain_db": manifest.get("hum_gain_db"),
                },
            )
            self._save(force=True)
        logger.info(
            "Job %s completato in %.1fs (%s file)",
            job["id"],
            manifest.get("elapsed") or 0,
            len(files),
        )

    def _fail(self, job: dict, message: str, log_path: Path | None) -> None:
        with self._lock:
            if job.get("status") == "cancelled":
                return
            tail = _tail(log_path) if log_path else ""
            final_message = message or "Elaborazione non riuscita."
            if job.get("status") == "error" and job.get("message"):
                # Messaggio già impostato da un evento più preciso (es. timeout).
                final_message = job["message"]
            job.update(
                status="error",
                stage="error",
                message=final_message,
                error=(job.get("error") or message or tail or "")[:MAX_LOG_TAIL],
                finished_at=time.time(),
                eta_seconds=None,
            )
            self._save(force=True)

    def _on_timeout(self, job_id: str) -> None:
        with self._lock:
            proc = self._procs.get(job_id)
            job = self._jobs.get(job_id)
            if job and job.get("status") == "processing":
                job.update(
                    status="error",
                    stage="error",
                    message="Elaborazione interrotta: tempo massimo superato.",
                    error="Timeout del job",
                    finished_at=time.time(),
                    eta_seconds=None,
                )
                self._save(force=True)
        if proc and proc.poll() is None:
            self._terminate(proc)

    @staticmethod
    def _terminate(proc: subprocess.Popen, grace: float = 4.0) -> None:
        def _signal_group(sig: int) -> None:
            if os.name == "nt":
                if sig == signal.SIGKILL:
                    proc.kill()
                else:
                    proc.terminate()
                return
            try:
                os.killpg(os.getpgid(proc.pid), sig)
            except (ProcessLookupError, PermissionError, OSError):
                try:
                    if sig == signal.SIGKILL:
                        proc.kill()
                    else:
                        proc.terminate()
                except OSError:
                    pass

        _signal_group(signal.SIGTERM)

        def _kill_later() -> None:
            try:
                proc.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                _signal_group(signal.SIGKILL)

        killer = threading.Thread(target=_kill_later, daemon=True)
        killer.start()

    # ── Operazioni pubbliche ─────────────────────────────────────────────────
    def get(self, job_id: str) -> dict:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                raise JobNotFound(job_id)
            return job

    def list_public(self) -> list[dict]:
        with self._lock:
            positions = {
                job_id: index + 1
                for index, job_id in enumerate(self._queue)
                if self._jobs.get(job_id, {}).get("status") == "queued"
            }
            jobs = [self._public(job, positions.get(job["id"])) for job in self._jobs.values()]

        jobs.sort(key=lambda j: (j["status"] not in ACTIVE_STATUSES, -(j["created_at"] or 0)))
        return jobs

    def get_public(self, job_id: str) -> dict:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                raise JobNotFound(job_id)
            position = None
            if job["id"] in self._queue:
                position = self._queue.index(job["id"]) + 1
            return self._public(job, position)

    def _public(self, job: dict, queue_position: int | None = None) -> dict:
        status = job.get("status", "error")
        is_active = status in ACTIVE_STATUSES
        kind = job.get("kind", "separate")
        is_denoise = kind == "denoise"
        is_polish = kind == "polish"
        files = {}
        if status == "completed":
            base = Path(job.get("name", "audio")).stem or "audio"
            for key, info in (job.get("files") or {}).items():
                filename = info.get("file") or ""
                ext = Path(filename).suffix.lstrip(".") or job.get("format", "mp3")
                if is_denoise:
                    label = "pulito"
                    display_label = "Clip pulita"
                elif is_polish:
                    label = "pulito"
                    display_label = "Stem pulito"
                else:
                    label = STEM_LABELS.get(key, {}).get("file_label", key)
                    display_label = STEM_LABELS.get(key, {}).get("label", key)
                files[key] = {
                    "key": key,
                    "label": display_label,
                    "name": f"{base}_{label}.{ext}",
                    "size": info.get("size", 0),
                    "ext": ext,
                    "media": info.get("media", "video" if ext in {"mp4", "webm", "mkv", "mov"} else "audio"),
                }

        if is_denoise:
            model_label = DENOISE_MODEL_LABEL
            quality_label = DENOISE_PRESETS.get(job.get("strength", ""), {}).get("label")
            format_label = DENOISE_OUTPUT_FORMATS.get(job.get("format", ""), {}).get("label")
        elif is_polish:
            base_model = MODELS.get(job.get("model", ""), {}).get("label", job.get("model"))
            result_obj = job.get("result") or {}
            mode = job.get("mode") or ("hum" if not job.get("residual") else "voice")
            hum_mode = job.get("hum_mode") or "off"
            hum_labels = {"auto": "Auto", "off": "off", "50": "50 Hz", "60": "60 Hz"}
            hum_label = hum_labels.get(hum_mode, hum_mode)
            if mode == "hum":
                model_label = result_obj.get("model_label") or "Filtri notch · 50/60 Hz"
                quality_label = f"Solo ronzio · {hum_label}"
            else:
                model_label = result_obj.get("model_label") or f"{base_model} · 2° passaggio"
                preset = POLISH_PRESETS.get(job.get("strength", ""), {})
                pct = round(float(job.get("residual") or 0) * 100)
                quality_label = f"{preset.get('label', job.get('strength'))} · {pct}%"
                if hum_mode != "off":
                    quality_label += f" + ronzio {hum_label}"
            format_label = OUTPUT_FORMATS.get(job.get("format", ""), {}).get(
                "label", job.get("format")
            )
        else:
            model_label = MODELS.get(job.get("model", ""), {}).get("label", job.get("model"))
            quality_label = QUALITY_PRESETS.get(job.get("quality", ""), {}).get(
                "label", job.get("quality")
            )
            format_label = OUTPUT_FORMATS.get(job.get("format", ""), {}).get(
                "label", job.get("format")
            )

        input_exists = self._input_path(job).is_file()
        result = job.get("result")
        output_media = (result or {}).get("output_media") or (
            "video" if job.get("format") == "video" else "audio"
        )
        return {
            "id": job["id"],
            "name": job.get("name", "audio"),
            "kind": kind,
            "media": job.get("media", "audio"),
            "output_media": output_media,
            "status": status,
            "stage": job.get("stage", ""),
            "progress": job.get("progress", 0),
            "message": job.get("message", ""),
            "error": job.get("error"),
            "model": job.get("model"),
            "model_label": model_label,
            "quality": (
                job.get("strength") if (is_denoise or is_polish) else job.get("quality")
            ),
            "quality_label": quality_label,
            "format": job.get("format"),
            "format_label": format_label,
            "strength": job.get("strength"),
            "strength_db": job.get("strength_db"),
            "residual": job.get("residual"),
            "target": job.get("target"),
            "source_job": job.get("source_job"),
            "mode": job.get("mode"),
            "hum_mode": job.get("hum_mode"),
            "size": job.get("size"),
            "duration": job.get("duration"),
            "created_at": job.get("created_at"),
            "started_at": job.get("started_at"),
            "finished_at": job.get("finished_at"),
            "elapsed": round((job.get("finished_at") or time.time()) - job["started_at"], 1)
            if job.get("started_at")
            else None,
            "eta_seconds": job.get("eta_seconds"),
            "queue_position": queue_position if status == "queued" else None,
            "files": files,
            "result": result,
            "has_source": input_exists,
            "retryable": status in {"error", "cancelled"} and input_exists,
            "active": is_active,
            "device": job.get("device"),
        }

    def cancel(self, job_id: str) -> dict:
        proc = None
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                raise JobNotFound(job_id)
            status = job.get("status")
            if status not in ACTIVE_STATUSES:
                return self._public(job)

            if status == "queued":
                try:
                    self._queue.remove(job_id)
                except ValueError:
                    pass
                job.update(
                    status="cancelled",
                    stage="cancelled",
                    progress=0,
                    message="Elaborazione annullata",
                    eta_seconds=None,
                    finished_at=time.time(),
                )
            else:
                job.update(
                    status="cancelled",
                    stage="cancelled",
                    message="Annullamento in corso…",
                    eta_seconds=None,
                )
                proc = self._procs.get(job_id)
            self._save(force=True)

        if proc is not None:
            self._terminate(proc)
        return self.get_public(job_id)

    def retry(self, job_id: str, overrides: dict | None = None) -> dict:
        for _attempt in range(2):
            with self._lock:
                job = self._jobs.get(job_id)
                if not job:
                    raise JobNotFound(job_id)
                if job.get("status") in ACTIVE_STATUSES:
                    raise JobBusy(job_id)
                if job_id in self._procs:
                    busy = True
                else:
                    busy = False
                    if not self._input_path(job).is_file():
                        raise MissingInput(job_id)

                    overrides = overrides or {}
                    for key in ("model", "quality", "format", "strength", "mode", "hum_mode"):
                        if overrides.get(key):
                            job[key] = overrides[key]
                    if overrides.get("strength") in DENOISE_PRESETS:
                        job["strength_db"] = DENOISE_PRESETS[overrides["strength"]]["atten_db"]
                    if overrides.get("strength") in POLISH_PRESETS:
                        job["residual"] = POLISH_PRESETS[overrides["strength"]]["residual"]
                    if job.get("kind") == "polish" and job.get("mode") == "hum":
                        job["residual"] = 0.0

                    shutil.rmtree(self._job_dir(job_id), ignore_errors=True)
                    job.update(
                        status="queued",
                        stage="queued",
                        progress=0,
                        message="In coda · nuovo tentativo",
                        error=None,
                        files={},
                        result=None,
                        eta_seconds=None,
                        started_at=None,
                        finished_at=None,
                        created_at=time.time(),
                    )
                    self._queue.append(job_id)
                    self._save(force=True)
                    self._wake.notify_all()
                    return self.get_public(job_id)

            # Un annullamento è ancora in corso: aspetta che il processo esca.
            if busy and not self._wait_terminal(job_id, timeout=6.0):
                raise JobBusy(job_id)

        raise JobBusy(job_id)

    def delete(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                raise JobNotFound(job_id)
            active = job.get("status") in ACTIVE_STATUSES
        if active:
            self.cancel(job_id)
            if not self._wait_terminal(job_id, timeout=8.0):
                raise JobBusy(job_id)
        with self._lock:
            job = self._jobs.pop(job_id, None)
            try:
                self._queue.remove(job_id)
            except ValueError:
                pass
            self._save(force=True)
        if job:
            self._remove_files(job)
        return True

    def _wait_terminal(self, job_id: str, timeout: float = 8.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                job = self._jobs.get(job_id)
                if not job:
                    return True
                if job.get("status") not in ACTIVE_STATUSES and job_id not in self._procs:
                    return True
            time.sleep(0.15)
        return False

    def _remove_files(self, job: dict) -> None:
        shutil.rmtree(self._job_dir(job["id"]), ignore_errors=True)
        try:
            self._input_path(job).unlink()
        except OSError:
            pass
        try:
            self._log_path(job["id"]).unlink()
        except OSError:
            pass

    # ── File dei risultati ───────────────────────────────────────────────────
    def file_path(self, job_id: str, key: str) -> tuple[Path, str]:
        """Ritorna (percorso assoluto sicuro, nome di download) per uno stem."""
        job = self.get(job_id)
        if job.get("status") != "completed":
            raise KeyNotFound(key)
        info = (job.get("files") or {}).get(key)
        if not info:
            raise KeyNotFound(key)
        job_dir = self._job_dir(job_id).resolve()
        path = (job_dir / info.get("file", "")).resolve()
        if job_dir not in path.parents:
            raise KeyNotFound(key)
        base = Path(job.get("name", "audio")).stem or "audio"
        if job.get("kind") in {"denoise", "polish"}:
            label = "pulito"
        else:
            label = STEM_LABELS.get(key, {}).get("file_label", key)
        ext = path.suffix.lstrip(".") or job.get("format", "mp3")
        return path, f"{base}_{label}.{ext}"

    def source_path(self, job_id: str) -> tuple[Path, str]:
        job = self.get(job_id)
        path = self._input_path(job)
        if not path.is_file():
            raise KeyNotFound("source")
        return path, job.get("name", path.name)

    def bundle_path(self, job_id: str) -> tuple[Path, str]:
        import tempfile
        import zipfile

        job = self.get(job_id)
        if job.get("status") != "completed" or not job.get("files"):
            raise KeyNotFound("bundle")
        job_dir = self._job_dir(job_id)
        base = Path(job.get("name", "audio")).stem or "audio"
        bundle = job_dir / "bundle.zip"
        if not bundle.is_file():
            handle = tempfile.NamedTemporaryFile(
                dir=str(job_dir), prefix=".bundle-", suffix=".zip", delete=False
            )
            tmp_name = handle.name
            handle.close()
            try:
                with zipfile.ZipFile(tmp_name, "w", compression=zipfile.ZIP_STORED) as archive:
                    for key in job["files"]:
                        try:
                            path, name = self.file_path(job_id, key)
                        except KeyNotFound:
                            continue
                        if path.is_file():
                            archive.write(path, arcname=name)
                os.replace(tmp_name, bundle)
            finally:
                if os.path.exists(tmp_name):
                    try:
                        os.unlink(tmp_name)
                    except OSError:
                        pass
        return bundle, f"{base}_stems.zip"

    # ── Manifest / peaks ─────────────────────────────────────────────────────
    def manifest(self, job_id: str) -> dict:
        job = self.get(job_id)
        path = self._job_dir(job_id) / "result.json"
        if not path.is_file():
            raise JobNotFound(job_id)
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise JobNotFound(job_id) from exc

    def peaks(self, job_id: str, key: str) -> dict:
        manifest = self.manifest(job_id)
        peaks = manifest.get("peaks", {})
        if key not in peaks:
            raise KeyNotFound(key)
        return {
            "key": key,
            "peaks": peaks[key],
            "duration": manifest.get("duration"),
        }

    # ── Pulizia automatica ───────────────────────────────────────────────────
    def _start_cleaner(self) -> None:
        self._cleaner = threading.Thread(
            target=self._cleanup_loop, name="avr-cleaner", daemon=True
        )
        self._cleaner.start()

    def _cleanup_loop(self) -> None:
        while not self._stop.wait(600):
            try:
                self.cleanup()
            except Exception:  # noqa: BLE001
                logger.exception("Errore nella pulizia automatica")

    def cleanup(self) -> int:
        retention = max(0.0, self.settings.retention_hours) * 3600
        if retention <= 0:
            return 0

        cutoff = time.time() - retention
        to_delete: list[dict] = []
        protected_names: set[str] = set()
        with self._lock:
            for job in list(self._jobs.values()):
                if job.get("status") in ACTIVE_STATUSES:
                    protected_names.add(self._input_path(job).name)
                    protected_names.add(self._log_path(job["id"]).name)
                    continue
                reference = job.get("finished_at") or job.get("created_at") or 0
                if reference and reference < cutoff:
                    to_delete.append(job)
            for job in to_delete:
                self._jobs.pop(job["id"], None)

        for job in to_delete:
            self._remove_files(job)
        if to_delete:
            logger.info("%s job scaduti rimossi", len(to_delete))
            self._save(force=True)

        # Orfani: cartelle di output, upload e log non più collegati a un job.
        with self._lock:
            valid_ids = set(self._jobs)
        for directory in (
            self.settings.outputs_dir.iterdir() if self.settings.outputs_dir.exists() else []
        ):
            if directory.is_dir() and directory.name not in valid_ids:
                try:
                    if directory.stat().st_mtime < cutoff:
                        shutil.rmtree(directory, ignore_errors=True)
                except OSError:
                    pass
        for folder, suffixes in (
            (self.settings.uploads_dir, None),
            (self.settings.logs_dir, {".log"}),
        ):
            if not folder.exists():
                continue
            for entry in folder.iterdir():
                if not entry.is_file() or entry.name in protected_names:
                    continue
                if suffixes and entry.suffix not in suffixes:
                    continue
                try:
                    if entry.stat().st_mtime < cutoff:
                        entry.unlink()
                except OSError:
                    pass
        return len(to_delete)

    # ── Chiusura ─────────────────────────────────────────────────────────────
    def shutdown(self) -> None:
        self._stopping = True
        self._stop.set()
        with self._lock:
            self._wake.notify_all()
            procs = list(self._procs.values())
        for proc in procs:
            self._terminate(proc)
        self._save(force=True)
