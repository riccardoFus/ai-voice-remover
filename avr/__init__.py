"""AI Voice Remover — applicazione Flask."""

from __future__ import annotations

import atexit
import importlib.util
import json
import logging
import mimetypes
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from flask import (
    Flask,
    jsonify,
    render_template,
    request,
    send_file,
)

from .config import (
    ALLOWED_AUDIO_EXTENSIONS,
    ALLOWED_MEDIA_EXTENSIONS,
    APP_NAME,
    APP_VERSION,
    DEFAULT_DENOISE_STRENGTH,
    DENOISE_MODEL_LABEL,
    DENOISE_OUTPUT_FORMATS,
    DENOISE_PRESETS,
    MEDIA_MIMETYPES,
    MODELS,
    OUTPUT_FORMATS,
    POLISH_MODEL,
    POLISH_PRESETS,
    QUALITY_PRESETS,
    ROOT,
    STEM_LABELS,
    Settings,
    VIDEO_EXTENSIONS,
    DEFAULT_POLISH_STRENGTH,
    denoise_model_cached,
    disk_free_gb,
    ffmpeg_available,
    ffprobe_available,
    load_settings,
)
from .jobs import (
    JobBusy,
    JobNotFound,
    JobStore,
    KeyNotFound,
    MissingInput,
    sanitize_name,
)

logger = logging.getLogger("avr")


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def probe_audio(path: Path) -> tuple[float | None, str | None]:
    """Legge la durata con ffprobe. Ritorna (durata, errore)."""
    if not ffprobe_available():
        return None, None
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=codec_type:format=duration",
        "-of",
        "json",
        str(path),
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=25, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None, "Impossibile analizzare il file audio."

    if result.returncode != 0:
        return None, "Il file non sembra contenere una traccia audio valida."

    try:
        payload = json.loads(result.stdout or "{}")
    except ValueError:
        return None, None

    if not payload.get("streams"):
        return None, "Il file non contiene alcun stream audio."

    duration = None
    try:
        raw = payload.get("format", {}).get("duration")
        if raw is not None:
            duration = round(float(raw), 2)
    except (TypeError, ValueError):
        duration = None
    return duration, None


def probe_media(path: Path) -> tuple[dict | None, str | None]:
    """Analizza audio/video e ritorna durata, media e presenza traccia audio."""
    if not ffprobe_available():
        return None, None
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "stream=codec_type,codec_name,channels,sample_rate:stream_disposition=attached_pic:format=duration",
        "-of",
        "json",
        str(path),
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None, "Impossibile analizzare il file."

    if result.returncode != 0:
        return None, "Il file non sembra contenere una traccia audio valida."

    try:
        payload = json.loads(result.stdout or "{}")
    except ValueError:
        return None, None

    streams = payload.get("streams") or []
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    video = [
        s
        for s in streams
        if s.get("codec_type") == "video"
        and not (s.get("disposition") or {}).get("attached_pic")
    ]
    if not audio:
        return None, "La clip non contiene alcuna traccia audio."

    duration = None
    try:
        raw = (payload.get("format") or {}).get("duration")
        if raw is not None:
            duration = round(float(raw), 2)
    except (TypeError, ValueError):
        duration = None

    return {
        "duration": duration,
        "media": "video" if video else "audio",
        "channels": int(audio[0].get("channels") or 2),
    }, None


def create_app(settings: Settings | None = None) -> Flask:
    settings = settings or load_settings()
    settings.ensure_dirs()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(name)s · %(message)s",
        datefmt="%H:%M:%S",
    )

    app = Flask(
        __name__,
        template_folder=str(ROOT / "templates"),
        static_folder=str(ROOT / "static"),
        static_url_path="/static",
    )
    app.config.update(
        MAX_CONTENT_LENGTH=settings.max_upload_bytes + 32 * 1024 * 1024,
        JSON_SORT_KEYS=False,
        AVR_SETTINGS=settings,
    )
    app.config["TEMPLATES_AUTO_RELOAD"] = False

    store = JobStore(settings)
    app.extensions["avr"] = {"settings": settings, "store": store}

    # ── Sicurezza / header ───────────────────────────────────────────────────
    @app.after_request
    def add_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
        )
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        elif response.mimetype == "text/html":
            response.headers.setdefault(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self' data:; media-src 'self' blob:; "
                "style-src 'self'; script-src 'self'; connect-src 'self'; "
                "object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
            )
        return response

    @app.errorhandler(413)
    def too_large(_error):
        return (
            jsonify(
                error=(
                    f"File troppo grande. Il limite è {settings.max_upload_mb} MB "
                    "per brano."
                )
            ),
            413,
        )

    @app.errorhandler(404)
    def not_found(_error):
        if request.path.startswith("/api/"):
            return jsonify(error="Risorsa non trovata"), 404
        return ("Pagina non trovata", 404)

    # ── Helper ───────────────────────────────────────────────────────────────
    def server_info() -> dict:
        return {
            "app": APP_NAME,
            "version": APP_VERSION,
            "device": settings.device,
            "device_label": settings.device_label,
            "device_name": settings.device_name,
            "max_upload_mb": settings.max_upload_mb,
            "retention_hours": settings.retention_hours,
            "concurrency": settings.concurrency,
            "models_cached": settings.models_cached,
            "ffmpeg": ffmpeg_available(),
            "ffprobe": ffprobe_available(),
            "demucs": _module_available("demucs"),
            "torch": _module_available("torch"),
            "denoise": {
                "available": settings.denoise_available,
                "engine": DENOISE_MODEL_LABEL,
                "model_cached": denoise_model_cached(),
                "max_minutes": settings.denoise_max_minutes,
                "mode": "cpu",
            },
            "polish": {
                "available": True,
                "engine": f"{MODELS[POLISH_MODEL]['label']} · 2° passaggio",
            },
            "disk_free_gb": disk_free_gb(settings.outputs_dir),
            "time": time.time(),
        }

    def error(message: str, status: int = 400):
        return jsonify(error=message), status

    def parse_options(payload: dict) -> dict | None:
        model = payload.get("model") or settings.default_model
        quality = payload.get("quality") or settings.default_quality
        output_format = payload.get("format") or settings.default_format
        if model not in MODELS or quality not in QUALITY_PRESETS or output_format not in OUTPUT_FORMATS:
            return None
        return {"model": model, "quality": quality, "format": output_format}

    # ── Pagine ───────────────────────────────────────────────────────────────
    @app.get("/")
    def index():
        return render_template(
            "index.html",
            settings=settings,
            app_name=APP_NAME,
            version=APP_VERSION,
            models=MODELS,
            qualities=QUALITY_PRESETS,
            formats=OUTPUT_FORMATS,
            denoise_presets=DENOISE_PRESETS,
            denoise_formats=DENOISE_OUTPUT_FORMATS,
            denoise_available=settings.denoise_available,
            denoise_model=DENOISE_MODEL_LABEL,
            default_denoise_strength=DEFAULT_DENOISE_STRENGTH,
            polish_presets=POLISH_PRESETS,
            default_polish_strength=DEFAULT_POLISH_STRENGTH,
        )

    # ── API ──────────────────────────────────────────────────────────────────
    @app.get("/api/health")
    def health():
        info = server_info()
        info["ok"] = bool(info["demucs"] and info["torch"] and info["ffmpeg"])
        if not info["ffmpeg"]:
            info["warning"] = (
                "FFmpeg non trovato: la lettura di alcuni formati audio e la "
                "conversione potrebbero non funzionare."
            )
        elif not info["demucs"]:
            info["warning"] = "Demucs non installato: esegui ./install.sh."
        return jsonify(info)

    @app.get("/api/jobs")
    def list_jobs():
        jobs = store.list_public()
        return jsonify({"jobs": jobs, "server": server_info()})

    @app.get("/api/jobs/<job_id>")
    def get_job(job_id: str):
        try:
            return jsonify(store.get_public(job_id))
        except JobNotFound:
            return error("Job non trovato", 404)

    @app.post("/api/upload")
    def upload():
        upload_file = request.files.get("file")
        if upload_file is None or not upload_file.filename:
            return error("Nessun file ricevuto")

        kind = (request.form.get("kind") or "separate").strip().lower()
        if kind not in {"separate", "denoise"}:
            return error("Modalità di elaborazione non valida")

        original_name = sanitize_name(upload_file.filename)
        ext = Path(original_name).suffix.lower().lstrip(".")

        if kind == "denoise":
            if not settings.denoise_available:
                return error(
                    "Motore denoise non installato. Esegui ./install.sh --denoise "
                    "e riavvia l'app.",
                    503,
                )
            if ext not in ALLOWED_MEDIA_EXTENSIONS:
                return error(
                    "Formato non supportato. Accetto audio e clip video: "
                    + ", ".join(sorted(ALLOWED_MEDIA_EXTENSIONS))
                )
            strength = request.form.get("strength") or DEFAULT_DENOISE_STRENGTH
            if strength not in DENOISE_PRESETS:
                return error("Intensità di denoise non valida")
            output_format = request.form.get("format") or "auto"
            if output_format not in DENOISE_OUTPUT_FORMATS:
                return error("Formato di output non valido")
            media_hint = "video" if ext in VIDEO_EXTENSIONS else "audio"
        else:
            if ext not in ALLOWED_AUDIO_EXTENSIONS:
                return error(
                    "Formato non supportato. Formati accettati: "
                    + ", ".join(sorted(ALLOWED_AUDIO_EXTENSIONS))
                )
            options = parse_options(request.form)
            if options is None:
                return error("Opzioni di elaborazione non valide")

        tmp_path = settings.uploads_dir / f".incoming-{uuid.uuid4().hex}.{ext}"
        size = 0
        try:
            with open(tmp_path, "wb") as target:
                while True:
                    chunk = upload_file.stream.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > settings.max_upload_bytes:
                        raise ValueError("too-large")
                    target.write(chunk)
            if size == 0:
                return error("Il file ricevuto è vuoto")

            if kind == "denoise":
                info, probe_error = probe_media(tmp_path)
                if probe_error:
                    return error(probe_error)
                info = info or {}
                media = info.get("media") or media_hint
                if ext not in VIDEO_EXTENSIONS:
                    media = "audio"
                duration = info.get("duration")
                if output_format == "auto":
                    output_format = "video" if media == "video" else "mp3"
                if output_format == "video" and media != "video":
                    return error(
                        "Per ottenere un video devi caricare una clip video. "
                        "Per i file audio scegli MP3, WAV o FLAC."
                    )
                max_seconds = settings.denoise_max_minutes * 60
                if duration and duration > max_seconds:
                    return error(
                        f"La clip dura {duration / 60:.1f} minuti; il limite per "
                        f"il denoise è {settings.denoise_max_minutes} minuti.",
                        413,
                    )
                job = store.submit(
                    tmp_path=tmp_path,
                    original_name=original_name,
                    options={
                        "strength": strength,
                        "strength_db": DENOISE_PRESETS[strength]["atten_db"],
                        "format": output_format,
                    },
                    size=size,
                    duration=duration,
                    kind="denoise",
                    media=media,
                )
            else:
                duration, probe_error = probe_audio(tmp_path)
                if probe_error:
                    return error(probe_error)
                job = store.submit(
                    tmp_path=tmp_path,
                    original_name=original_name,
                    options=options,
                    size=size,
                    duration=duration,
                )
            return jsonify({"job": job}), 202
        except ValueError as exc:
            if str(exc) == "too-large":
                return error(
                    f"File troppo grande. Il limite è {settings.max_upload_mb} MB per file.",
                    413,
                )
            raise
        except OSError as exc:
            logger.exception("Errore durante il salvataggio dell'upload")
            return error(f"Impossibile salvare il file: {exc}", 500)
        finally:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass

    @app.post("/api/jobs/<job_id>/polish")
    def polish_job(job_id: str):
        try:
            source = store.get_public(job_id)
        except JobNotFound:
            return error("Job non trovato", 404)

        if source.get("kind") != "separate":
            return error("Solo i risultati di una separazione possono essere puliti.", 400)
        if source.get("status") != "completed":
            return error("Attendi il completamento della separazione.", 409)

        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            payload = {}
        target = payload.get("target") or "instrumental"
        if target not in source.get("files", {}) or target == "vocals":
            return error("Stem non disponibile per la pulizia dei residui.", 400)

        mode = payload.get("mode") or "both"
        if mode not in {"both", "voice", "hum"}:
            return error("Tipo di pulizia non valido", 400)

        strength = payload.get("strength") or DEFAULT_POLISH_STRENGTH
        if strength not in POLISH_PRESETS:
            return error("Intensità di pulizia non valida", 400)

        hum_mode = payload.get("hum") or "auto"
        if hum_mode not in {"auto", "off", "50", "60"}:
            return error("Modalità ronzio non valida", 400)
        if mode == "voice":
            hum_mode = "off"
        if mode == "hum" and hum_mode == "off":
            return error("Scegli Auto, 50 Hz o 60 Hz per rimuovere il ronzio.")

        residual = 0.0 if mode == "hum" else POLISH_PRESETS[strength]["residual"]

        output_format = payload.get("format") or source.get("format") or settings.default_format
        if output_format not in OUTPUT_FORMATS:
            return error("Formato di output non valido", 400)

        try:
            source_path, _ = store.file_path(job_id, target)
        except (JobNotFound, KeyNotFound):
            return error("File dello stem non disponibile", 404)
        if not source_path.is_file():
            return error("File dello stem non più disponibile", 409)

        base = Path(source.get("name", "stem")).stem or "stem"
        file_label = STEM_LABELS.get(target, {}).get("file_label", target)
        hum_labels = {"auto": "auto", "off": "off", "50": "50 Hz", "60": "60 Hz"}
        if mode == "hum":
            job_label = f"Ronzio {hum_labels[hum_mode]}"
        else:
            job_label = POLISH_PRESETS[strength]["label"]
            if hum_mode != "off":
                job_label += f" + ronzio {hum_labels[hum_mode]}"
        ext = source_path.suffix.lstrip(".") or "wav"
        tmp_path = settings.uploads_dir / f".incoming-polish-{uuid.uuid4().hex}.{ext}"
        try:
            shutil.copy2(source_path, tmp_path)
            job = store.submit(
                tmp_path=tmp_path,
                original_name=f"{base}_{file_label} ({job_label}).{ext}",
                options={
                    "model": POLISH_MODEL,
                    "quality": source.get("quality")
                    if source.get("quality") in QUALITY_PRESETS
                    else settings.default_quality,
                    "format": output_format,
                    "strength": strength,
                    "residual": residual,
                    "mode": mode,
                    "hum_mode": hum_mode,
                    "target": target,
                    "source_job": job_id,
                },
                size=source_path.stat().st_size,
                duration=source.get("duration"),
                kind="polish",
                media="audio",
            )
            return jsonify({"job": job}), 202
        except OSError as exc:
            logger.exception("Errore durante la preparazione della pulizia residui")
            return error(f"Impossibile preparare lo stem: {exc}", 500)
        finally:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass

    @app.post("/api/jobs/<job_id>/cancel")
    def cancel_job(job_id: str):
        try:
            return jsonify(store.cancel(job_id))
        except JobNotFound:
            return error("Job non trovato", 404)

    @app.post("/api/jobs/<job_id>/retry")
    def retry_job(job_id: str):
        try:
            job = store.get_public(job_id)
            payload = request.get_json(silent=True) or {}
            if not isinstance(payload, dict):
                payload = {}
            overrides = {}
            if job.get("kind") == "denoise":
                for key, table in (
                    ("strength", DENOISE_PRESETS),
                    ("format", DENOISE_OUTPUT_FORMATS),
                ):
                    value = payload.get(key)
                    if value:
                        if value not in table:
                            return error("Opzioni di elaborazione non valide")
                        overrides[key] = value
                if overrides.get("format") == "auto":
                    overrides["format"] = "video" if job.get("media") == "video" else "mp3"
                if overrides.get("format") == "video" and job.get("media") != "video":
                    return error("Il formato video richiede una clip video in ingresso.")
            elif job.get("kind") == "polish":
                for key, table in (
                    ("strength", POLISH_PRESETS),
                    ("format", OUTPUT_FORMATS),
                ):
                    value = payload.get(key)
                    if value:
                        if value not in table:
                            return error("Opzioni di elaborazione non valide")
                        overrides[key] = value
            else:
                for key, table in (
                    ("model", MODELS),
                    ("quality", QUALITY_PRESETS),
                    ("format", OUTPUT_FORMATS),
                ):
                    value = payload.get(key)
                    if value:
                        if value not in table:
                            return error("Opzioni di elaborazione non valide")
                        overrides[key] = value
            return jsonify(store.retry(job_id, overrides)), 202
        except JobNotFound:
            return error("Job non trovato", 404)
        except JobBusy:
            return error("Il job è ancora in elaborazione", 409)
        except MissingInput:
            return error(
                "Il file originale non è più disponibile: carica di nuovo il brano.", 409
            )

    @app.delete("/api/jobs/<job_id>")
    def delete_job(job_id: str):
        try:
            store.delete(job_id)
            return jsonify({"deleted": True, "id": job_id})
        except JobNotFound:
            return error("Job non trovato", 404)
        except JobBusy:
            return error(
                "Impossibile eliminare il job mentre è in elaborazione: riprova tra poco.",
                409,
            )

    @app.get("/api/jobs/<job_id>/files/<key>")
    def job_file(job_id: str, key: str):
        try:
            path, download_name = store.file_path(job_id, key)
        except (JobNotFound, KeyNotFound):
            return error("File non trovato", 404)
        if not path.is_file():
            return error("File non più disponibile", 404)
        mimetype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        as_attachment = request.args.get("download") == "1"
        return send_file(
            path,
            mimetype=mimetype,
            conditional=True,
            as_attachment=as_attachment,
            download_name=download_name,
            max_age=0,
        )

    @app.get("/api/jobs/<job_id>/source")
    def source_file(job_id: str):
        try:
            path, name = store.source_path(job_id)
        except (JobNotFound, KeyNotFound):
            return error("File originale non disponibile", 404)
        ext = path.suffix.lower().lstrip(".")
        mimetype = MEDIA_MIMETYPES.get(ext, "application/octet-stream")
        as_attachment = request.args.get("download") == "1"
        return send_file(
            path,
            mimetype=mimetype,
            conditional=True,
            as_attachment=as_attachment,
            download_name=name,
            max_age=0,
        )

    @app.get("/api/jobs/<job_id>/bundle.zip")
    def bundle(job_id: str):
        try:
            path, download_name = store.bundle_path(job_id)
        except (JobNotFound, KeyNotFound):
            return error("Archivio non disponibile", 404)
        return send_file(
            path,
            mimetype="application/zip",
            as_attachment=True,
            download_name=download_name,
            max_age=0,
        )

    @app.get("/api/jobs/<job_id>/peaks/<key>")
    def job_peaks(job_id: str, key: str):
        try:
            return jsonify(store.peaks(job_id, key))
        except (JobNotFound, KeyNotFound):
            return error("Forma d'onda non disponibile", 404)

    # ── Pulizia alla chiusura ────────────────────────────────────────────────
    def _shutdown() -> None:
        try:
            store.shutdown()
        except Exception:  # noqa: BLE001
            logger.exception("Errore durante la chiusura")

    app.extensions["avr_shutdown"] = _shutdown
    atexit.register(_shutdown)
    return app
