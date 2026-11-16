#!/usr/bin/env python3
"""Worker di denoise AI (DeepFilterNet3).

Gira nella virtualenv dedicata `.denoise-venv` (Python 3.11 + torch CPU), così
da non interferire con Demucs né con la GPU. Comunica col server tramite righe
JSON su stdout, come il worker di separazione.

Filosofia: riduzione del rumore *conservativa*. Il modello DeepFilterNet3 è
specializzato in voce/parlato e viene usato con un limite di attenuazione
(`atten_lim_db`) per non impoverire il timbro. Il post-filter, che tende a
"mangiare" le code e le respirazioni, resta disattivato.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

import numpy as np
import soundfile as sf

MODEL_NAME = "DeepFilterNet3"
PEAK_BUCKETS = 720
SR = 48000

_emit_lock = threading.Lock()


def emit(**payload) -> None:
    payload.setdefault("t", round(time.time(), 3))
    with _emit_lock:
        try:
            sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
            sys.stdout.flush()
        except (BrokenPipeError, OSError):
            os._exit(0)


def status(progress: float, message: str, stage: str = "") -> None:
    emit(event="status", progress=round(progress, 1), message=message, stage=stage)


def run(cmd: list[str], timeout: int | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout,
        check=False,
    )


def tail_process(proc: subprocess.CompletedProcess, lines: int = 8) -> str:
    raw = (proc.stderr or "") + "\n" + (proc.stdout or "")
    chunks = [line.strip() for line in raw.splitlines() if line.strip()]
    return " | ".join(chunks[-lines:])[:1200]


def probe(path: Path) -> dict:
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "stream=codec_type,channels,sample_rate:stream_disposition=attached_pic:format=duration",
        "-of",
        "json",
        str(path),
    ]
    proc = run(cmd, timeout=30)
    if proc.returncode != 0:
        raise RuntimeError("Il file non sembra contenere una traccia audio valida.")
    try:
        payload = json.loads(proc.stdout or "{}")
    except ValueError as exc:
        raise RuntimeError("Impossibile analizzare il file audio.") from exc

    streams = payload.get("streams") or []
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    video = [
        s
        for s in streams
        if s.get("codec_type") == "video"
        and not (s.get("disposition") or {}).get("attached_pic")
    ]
    if not audio:
        raise RuntimeError("La clip non contiene alcuna traccia audio.")

    duration = None
    try:
        raw = (payload.get("format") or {}).get("duration")
        if raw is not None:
            duration = float(raw)
    except (TypeError, ValueError):
        duration = None

    return {
        "duration": duration,
        "channels": int(audio[0].get("channels") or 2),
        "has_video": bool(video),
    }


def extract_audio(src: Path, dst: Path, channels: int) -> None:
    cmd = [
        "ffmpeg",
        "-y",
        "-v",
        "error",
        "-i",
        str(src),
        "-vn",
        "-ac",
        str(max(1, min(2, channels))),
        "-ar",
        str(SR),
        "-c:a",
        "pcm_f32le",
        str(dst),
    ]
    proc = run(cmd, timeout=30 * 60)
    if proc.returncode != 0 or not dst.is_file():
        raise RuntimeError(
            "Impossibile estrarre l'audio dalla clip. " + tail_process(proc)
        )


def compute_peaks(audio: np.ndarray, buckets: int = PEAK_BUCKETS) -> list[float]:
    """audio: float32 [channels, samples] -> forma d'onda normalizzata 0..1."""
    if audio.ndim == 2:
        mono = np.abs(audio).mean(axis=0)
    else:
        mono = np.abs(audio)
    if mono.size == 0:
        return [0.0] * buckets
    edges = np.linspace(0, mono.size, buckets + 1, dtype=np.int64)
    values = np.zeros(buckets, dtype=np.float32)
    for i in range(buckets):
        segment = mono[edges[i] : edges[i + 1]]
        values[i] = float(segment.max()) if segment.size else 0.0
    peak = float(values.max())
    if peak > 0:
        values /= peak
    return [round(float(v), 3) for v in values]


def encode_audio(audio: np.ndarray, fmt: str, target: Path, tmp_wav: Path) -> None:
    samples = np.ascontiguousarray(audio.T)
    if fmt == "wav":
        sf.write(target, samples, SR, subtype="PCM_24")
        return
    if fmt == "flac":
        sf.write(target, samples, SR, subtype="PCM_24")
        return

    sf.write(tmp_wav, samples, SR, subtype="PCM_24")
    cmd = [
        "ffmpeg",
        "-y",
        "-v",
        "error",
        "-i",
        str(tmp_wav),
        "-c:a",
        "libmp3lame",
        "-b:a",
        "320k",
        str(target),
    ]
    proc = run(cmd, timeout=30 * 60)
    if proc.returncode != 0 or not target.is_file():
        raise RuntimeError("Codifica MP3 non riuscita. " + tail_process(proc))


def mux_video(
    src: Path,
    clean_wav: Path,
    target: Path,
    container: str,
) -> bool:
    """Rimuxa l'audio pulito nel video originale copiando lo stream video.

    Ritorna True se il video è stato copiato, False se è stato ricodificato.
    """
    base = [
        "ffmpeg",
        "-y",
        "-v",
        "error",
        "-i",
        str(src),
        "-i",
        str(clean_wav),
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-map_metadata",
        "0",
        "-shortest",
    ]
    if container == "webm":
        audio_args = ["-c:a", "libopus", "-b:a", "192k"]
    else:
        audio_args = ["-c:a", "aac", "-b:a", "256k", "-ar", str(SR)]

    copy_cmd = base + ["-c:v", "copy"] + audio_args + [str(target)]
    proc = run(copy_cmd, timeout=60 * 60)
    if proc.returncode == 0 and target.is_file():
        return True

    # Alcuni container/codec non sono compatibili con la copia: ricodifica in
    # alta qualità (CRF basso) per non perdere definizione.
    if container == "webm":
        video_args = ["-c:v", "libvpx-vp9", "-crf", "28", "-b:v", "0", "-row-mt", "1"]
    else:
        video_args = [
            "-c:v",
            "libx264",
            "-preset",
            "medium",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
        ]
    fallback_cmd = base + video_args + audio_args + [str(target)]
    proc = run(fallback_cmd, timeout=120 * 60)
    if proc.returncode != 0 or not target.is_file():
        raise RuntimeError("Impossibile creare il video pulito. " + tail_process(proc))
    return False


def run_denoise(args: argparse.Namespace) -> int:
    started = time.time()
    src = Path(args.input).resolve()
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    if not src.is_file():
        raise RuntimeError("File di input non trovato.")
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise RuntimeError("FFmpeg/ffprobe non disponibili: installa FFmpeg.")

    status(3, "Preparazione della clip…", "preparing")
    info = probe(src)
    duration = info["duration"]
    if duration and duration > args.max_minutes * 60:
        raise RuntimeError(
            f"La clip dura {duration / 60:.1f} minuti; il limite per il denoise è "
            f"{args.max_minutes} minuti."
        )

    source_wav = out_dir / "_source_48k.wav"
    clean_wav = out_dir / "_clean_48k.wav"
    status(7, "Estrazione della traccia audio…", "extracting")
    extract_audio(src, source_wav, info["channels"])

    import torch

    threads = int(os.environ.get("AVR_DENOISE_THREADS", "0") or 0)
    if threads <= 0:
        threads = max(1, min(8, os.cpu_count() or 4))
    try:
        torch.set_num_threads(threads)
    except Exception:
        pass

    status(18, "Caricamento del modello AI (DeepFilterNet3)…", "loading_model")
    from df.enhance import enhance, init_df

    model, df_state, _suffix = init_df(
        MODEL_NAME,
        post_filter=False,
        log_level="error",
        log_file=None,
        config_allow_defaults=True,
    )

    data, sr = sf.read(str(source_wav), always_2d=True, dtype="float32")
    if sr != SR:
        raise RuntimeError(f"Frequenza di campionamento inattesa: {sr} Hz.")
    audio = torch.from_numpy(np.ascontiguousarray(data.T))  # [C, T]
    original = audio.numpy().copy()

    stop_ticker = threading.Event()

    def ticker() -> None:
        value = 32.0
        while not stop_ticker.wait(1.2):
            value = min(82.0, value + 0.8)
            status(value, "Pulizia AI in corso…", "denoising")

    ticker_thread = threading.Thread(target=ticker, daemon=True)
    ticker_thread.start()
    status(30, "Pulizia AI in corso…", "denoising")
    try:
        with torch.inference_mode():
            enhanced = enhance(
                model,
                df_state,
                audio,
                pad=True,
                atten_lim_db=float(args.strength_db),
            )
    finally:
        stop_ticker.set()
        ticker_thread.join(timeout=2)

    clean = enhanced.detach().cpu().numpy().astype(np.float32)
    clean = np.clip(clean, -1.0, 1.0)
    status(85, "Preparazione del file finale…", "encoding")

    fmt = args.format
    if fmt == "video" and not info["has_video"]:
        raise RuntimeError("Il formato video richiede una clip video in ingresso.")

    files: dict[str, dict] = {}
    output_media = "audio"
    video_reencoded = False

    if fmt == "video":
        container = "webm" if src.suffix.lower() == ".webm" else "mp4"
        target = out_dir / f"clean.{container}"
        sf.write(str(clean_wav), np.ascontiguousarray(clean.T), SR, subtype="PCM_24")
        status(90, "Unione con il video originale…", "encoding")
        video_reencoded = not mux_video(src, clean_wav, target, container)
        output_media = "video"
        key = "clean"
        files[key] = {
            "file": target.name,
            "size": target.stat().st_size,
            "key": key,
            "label": "Clip pulita",
            "media": "video",
        }
    else:
        ext = {"mp3": "mp3", "wav": "wav", "flac": "flac"}[fmt]
        target = out_dir / f"clean.{ext}"
        if ext == "mp3":
            encode_audio(clean, "mp3", target, clean_wav)
        else:
            encode_audio(clean, ext, target, clean_wav)
        output_media = "audio"
        key = "clean"
        files[key] = {
            "file": target.name,
            "size": target.stat().st_size,
            "key": key,
            "label": "Clip pulita",
            "media": "audio",
        }

    status(96, "Analisi della forma d'onda…", "analyzing")
    peaks: dict[str, list[float]] = {}
    if output_media == "audio":
        peaks["clean"] = compute_peaks(clean)
        peaks["original"] = compute_peaks(original)

    elapsed = time.time() - started
    manifest = {
        "version": 1,
        "kind": "denoise",
        "model": MODEL_NAME,
        "model_label": MODEL_NAME,
        "strength_db": round(float(args.strength_db), 1),
        "format": fmt,
        "output_media": output_media,
        "video_reencoded": video_reencoded,
        "duration": round(float(clean.shape[-1]) / SR, 2),
        "samplerate": SR,
        "files": files,
        "peaks": peaks,
        "elapsed": round(elapsed, 1),
    }
    tmp = out_dir / "result.json.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, out_dir / "result.json")

    for temp in (source_wav, clean_wav):
        try:
            temp.unlink()
        except OSError:
            pass

    status(100, "Pulizia completata", "done")
    emit(event="done", manifest={k: v for k, v in manifest.items() if k != "peaks"})
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AI Voice Remover — denoise worker")
    parser.add_argument("--input", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--format", required=True, choices=["mp3", "wav", "flac", "video"])
    parser.add_argument("--strength-db", type=float, default=8.0)
    parser.add_argument("--media", default="audio", choices=["audio", "video"])
    parser.add_argument("--max-minutes", type=int, default=15)
    args = parser.parse_args(argv)

    try:
        return run_denoise(args)
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
        message = str(exc).strip() or exc.__class__.__name__
        lowered = message.lower()
        if "ffmpeg" in lowered or isinstance(exc, FileNotFoundError):
            message = "FFmpeg non disponibile: installalo e riprova."
        elif "memoria" in lowered or "memory" in lowered:
            message = "Memoria insufficiente per questa clip. Prova una clip più corta."
        emit(event="error", message=message[:1200])
        return 1


if __name__ == "__main__":
    sys.exit(main())
