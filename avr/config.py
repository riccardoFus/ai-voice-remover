"""Configurazione condivisa tra server, job manager e worker di separazione."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

APP_NAME = "AI Voice Remover"
APP_VERSION = "1.1.0"

# ── Modelli Demucs ────────────────────────────────────────────────────────────
# `hf_repo` serve solo per capire se i pesi sono già nella cache di HuggingFace.
MODELS: dict[str, dict] = {
    "htdemucs_ft": {
        "label": "HTDemucs FT",
        "description": "Massima qualità · 4 stem · consigliato",
        "hf_repo": "HTDemucs-ft",
        "sources": ("vocals", "drums", "bass", "other"),
        "recommended": True,
    },
    "htdemucs": {
        "label": "HTDemucs",
        "description": "Veloce · 4 stem · ottimo compromesso",
        "hf_repo": "HTDemucs",
        "sources": ("vocals", "drums", "bass", "other"),
    },
    "htdemucs_6s": {
        "label": "HTDemucs 6S",
        "description": "6 stem · separa anche chitarra e piano",
        "hf_repo": "HTDemucs-6s",
        "sources": ("vocals", "drums", "bass", "other", "guitar", "piano"),
    },
    "mdx_extra_q": {
        "label": "MDX Extra Q",
        "description": "Qualità alta · meno memoria GPU",
        "hf_repo": "Demucs-mdx_extra_q",
        "sources": ("vocals", "drums", "bass", "other"),
    },
}

DEFAULT_MODEL = "htdemucs_ft"

# ── Preset di qualità ────────────────────────────────────────────────────────
QUALITY_PRESETS: dict[str, dict] = {
    "turbo": {
        "label": "Turbo",
        "description": "Il più veloce · shifts 0",
        "shifts": 0,
        "overlap": 0.10,
    },
    "balanced": {
        "label": "Bilanciata",
        "description": "Buona qualità · shifts 1",
        "shifts": 1,
        "overlap": 0.25,
    },
    "best": {
        "label": "Massima",
        "description": "Qualità top · più lenta · shifts 2",
        "shifts": 2,
        "overlap": 0.25,
    },
}

DEFAULT_QUALITY = "balanced"

# ── Formati di output ────────────────────────────────────────────────────────
OUTPUT_FORMATS: dict[str, dict] = {
    "mp3": {
        "label": "MP3 320 kbps",
        "description": "Leggero e universale",
        "ext": "mp3",
        "mimetype": "audio/mpeg",
    },
    "wav": {
        "label": "WAV 24-bit",
        "description": "Lossless per il mixing",
        "ext": "wav",
        "mimetype": "audio/wav",
    },
    "flac": {
        "label": "FLAC 24-bit",
        "description": "Lossless compresso",
        "ext": "flac",
        "mimetype": "audio/flac",
    },
}

DEFAULT_FORMAT = "mp3"

# ── Denoise AI (DeepFilterNet) ───────────────────────────────────────────────
DENOISE_MODEL = "DeepFilterNet3"
DENOISE_MODEL_LABEL = "DeepFilterNet3"

# L'attenuazione è volutamente limitata: il modello non deve "impoverire" il
# timbro. Valori bassi = intervento più delicato.
DENOISE_PRESETS: dict[str, dict] = {
    "delicate": {
        "label": "Delicato",
        "description": "Riduce solo il rumore evidente · massima naturalezza",
        "atten_db": 4,
    },
    "natural": {
        "label": "Naturale",
        "description": "Equilibrio tra pulizia e timbro · consigliato",
        "atten_db": 8,
    },
    "strong": {
        "label": "Intenso",
        "description": "Più incisivo, sempre senza effetto robotico",
        "atten_db": 14,
    },
}

DEFAULT_DENOISE_STRENGTH = "natural"

# ── Pulizia post-stem: residui vocali nella strumentale ──────────────────────
# Un secondo passaggio Demucs stima la voce ancora presente nello stem; la
# sottrazione è parziale (percentuale controllata) per non impoverire il mix.
POLISH_MODEL = "htdemucs_ft"

POLISH_PRESETS: dict[str, dict] = {
    "light": {
        "label": "Leggero",
        "description": "Tocca solo i residui più evidenti · massima prudenza",
        "residual": 0.35,
    },
    "medium": {
        "label": "Equilibrato",
        "description": "Buon compromesso tra pulizia e naturalezza · consigliato",
        "residual": 0.60,
    },
    "strong": {
        "label": "Deciso",
        "description": "Riduce più voce residua, con qualche rischio in più",
        "residual": 0.85,
    },
}

DEFAULT_POLISH_STRENGTH = "medium"

DENOISE_OUTPUT_FORMATS: dict[str, dict] = {
    "auto": {
        "label": "Automatico",
        "description": "Video → video · audio → MP3 320",
        "ext": "mp3",
        "mimetype": "audio/mpeg",
    },
    "video": {
        "label": "Video (audio pulito)",
        "description": "Video invariato, traccia audio sostituita",
        "ext": "mp4",
        "mimetype": "video/mp4",
        "video": True,
    },
    "mp3": {
        "label": "MP3 320 kbps",
        "description": "Solo audio · leggero",
        "ext": "mp3",
        "mimetype": "audio/mpeg",
    },
    "wav": {
        "label": "WAV 24-bit",
        "description": "Solo audio · lossless",
        "ext": "wav",
        "mimetype": "audio/wav",
    },
    "flac": {
        "label": "FLAC 24-bit",
        "description": "Solo audio · lossless compresso",
        "ext": "flac",
        "mimetype": "audio/flac",
    },
}

VIDEO_EXTENSIONS = {"mp4", "m4v", "mov", "mkv", "webm", "avi"}

# ── Stem ─────────────────────────────────────────────────────────────────────
STEM_LABELS: dict[str, dict] = {
    "instrumental": {"label": "Strumentale", "file_label": "strumentale", "icon": "instrumental"},
    "vocals": {"label": "Voce", "file_label": "voce", "icon": "vocals"},
    "drums": {"label": "Batteria", "file_label": "batteria", "icon": "drums"},
    "bass": {"label": "Basso", "file_label": "basso", "icon": "bass"},
    "other": {"label": "Altro", "file_label": "altro", "icon": "other"},
    "guitar": {"label": "Chitarra", "file_label": "chitarra", "icon": "guitar"},
    "piano": {"label": "Piano", "file_label": "piano", "icon": "piano"},
}

ALLOWED_AUDIO_EXTENSIONS = {
    "mp3", "wav", "flac", "ogg", "oga", "m4a", "aac", "wma", "opus",
    "aiff", "aif", "alac", "mp4", "webm",
}

ALLOWED_MEDIA_EXTENSIONS = ALLOWED_AUDIO_EXTENSIONS | VIDEO_EXTENSIONS

AUDIO_MIMETYPES = {
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "flac": "audio/flac",
    "ogg": "audio/ogg",
    "oga": "audio/ogg",
    "m4a": "audio/mp4",
    "aac": "audio/aac",
    "wma": "audio/x-ms-wma",
    "opus": "audio/ogg",
    "aiff": "audio/aiff",
    "aif": "audio/aiff",
    "alac": "audio/mp4",
    "mp4": "audio/mp4",
    "webm": "audio/webm",
}

MEDIA_MIMETYPES = {
    **AUDIO_MIMETYPES,
    "mp4": "video/mp4",
    "m4v": "video/mp4",
    "mov": "video/quicktime",
    "mkv": "video/x-matroska",
    "webm": "video/webm",
    "avi": "video/x-msvideo",
}


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_path(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    if not value:
        return default
    path = Path(value).expanduser()
    return path if path.is_absolute() else (ROOT / path)


def detect_device(preference: str = "auto") -> tuple[str, str, str]:
    """Ritorna (device, etichetta, nome hardware) senza far fallire l'avvio."""
    pref = (preference or "auto").strip().lower()
    if pref not in {"auto", "cuda", "mps", "cpu"}:
        pref = "auto"

    try:
        import torch
    except Exception:
        return "cpu", "CPU", ""

    if pref in {"auto", "cuda"}:
        try:
            if torch.cuda.is_available():
                name = torch.cuda.get_device_name(0)
                return "cuda", f"GPU · {name}", name
        except Exception:
            pass
        if pref == "cuda":
            return "cpu", "CPU", ""

    if pref in {"auto", "mps"}:
        try:
            mps = getattr(torch.backends, "mps", None)
            if mps is not None and mps.is_available():
                return "mps", "GPU · Apple Silicon", ""
        except Exception:
            pass

    return "cpu", "CPU", ""


def model_is_cached(model_name: str) -> bool:
    info = MODELS.get(model_name)
    if not info:
        return False
    explicit = os.environ.get("HUGGINGFACE_HUB_CACHE")
    hf_home = os.environ.get("HF_HOME")
    if explicit:
        hub = Path(explicit).expanduser()
    elif hf_home:
        hub = Path(hf_home).expanduser() / "hub"
    else:
        hub = Path.home() / ".cache" / "huggingface" / "hub"
    return (hub / f"models--adefossez--{info['hf_repo']}").is_dir()


def denoise_model_cached() -> bool:
    base = Path(
        os.environ.get("AVR_DENOISE_CACHE", Path.home() / ".cache" / "DeepFilterNet")
    ).expanduser()
    return (base / DENOISE_MODEL).is_dir()


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    device: str
    device_label: str
    device_name: str
    max_upload_mb: int
    retention_hours: float
    concurrency: int
    job_timeout: int
    uploads_dir: Path
    outputs_dir: Path
    data_dir: Path
    logs_dir: Path
    default_model: str
    default_quality: str
    default_format: str
    denoise_python: Path
    denoise_ready: bool
    denoise_max_minutes: int
    version: str = APP_VERSION

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def models_cached(self) -> dict[str, bool]:
        return {name: model_is_cached(name) for name in MODELS}

    @property
    def denoise_available(self) -> bool:
        return bool(self.denoise_ready and self.denoise_python.is_file())

    def ensure_dirs(self) -> None:
        for path in (
            self.uploads_dir,
            self.outputs_dir,
            self.data_dir,
            self.logs_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)


def load_settings() -> Settings:
    device, device_label, device_name = detect_device(
        os.environ.get("AVR_DEVICE", "auto")
    )
    model = os.environ.get("AVR_MODEL", DEFAULT_MODEL)
    quality = os.environ.get("AVR_QUALITY", DEFAULT_QUALITY)
    output_format = os.environ.get("AVR_FORMAT", DEFAULT_FORMAT)
    denoise_venv = _env_path("AVR_DENOISE_VENV", ROOT / ".denoise-venv")
    denoise_python = Path(
        os.environ.get("AVR_DENOISE_PYTHON", str(denoise_venv / "bin" / "python"))
    ).expanduser()
    denoise_ready = denoise_python.is_file() and (
        (denoise_venv / ".ready").is_file()
        or os.environ.get("AVR_DENOISE_PYTHON") is not None
    )

    return Settings(
        host=os.environ.get("AVR_HOST", "127.0.0.1"),
        port=_env_int("AVR_PORT", 8791),
        device=device,
        device_label=device_label,
        device_name=device_name,
        max_upload_mb=_env_int("AVR_MAX_UPLOAD_MB", 250),
        retention_hours=_env_float("AVR_RETENTION_HOURS", 24.0),
        concurrency=max(1, _env_int("AVR_CONCURRENCY", 1)),
        job_timeout=_env_int("AVR_JOB_TIMEOUT", 0),
        uploads_dir=_env_path("AVR_UPLOADS_DIR", ROOT / "uploads"),
        outputs_dir=_env_path("AVR_OUTPUTS_DIR", ROOT / "outputs"),
        data_dir=_env_path("AVR_DATA_DIR", ROOT / "data"),
        logs_dir=_env_path("AVR_LOGS_DIR", ROOT / "logs"),
        default_model=model if model in MODELS else DEFAULT_MODEL,
        default_quality=quality if quality in QUALITY_PRESETS else DEFAULT_QUALITY,
        default_format=output_format if output_format in OUTPUT_FORMATS else DEFAULT_FORMAT,
        denoise_python=denoise_python,
        denoise_ready=denoise_ready,
        denoise_max_minutes=_env_int("AVR_DENOISE_MAX_MINUTES", 15),
    )


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def ffprobe_available() -> bool:
    return shutil.which("ffprobe") is not None


def disk_free_gb(path: Path) -> float:
    try:
        return round(shutil.disk_usage(path).free / (1024 ** 3), 1)
    except OSError:
        return 0.0
