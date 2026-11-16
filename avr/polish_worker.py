"""Pulizia post-stem: residui vocali e ronzio elettrico.

Modalità:
  * `voice` — secondo passaggio Demucs, sottrazione controllata della voce residua
  * `hum`   — solo filtri notch stretti su 50/60 Hz e armoniche
  * `both`  — entrambi

Per il ronzio non si usa un denoiser vocale: sarebbe troppo aggressivo sulla
musica. Si usano peaking filter stretti (Q alto, -20 dB) applicati in modo
zero-phase, con rilevamento automatico della frequenza di rete. L'obiettivo è
togliere solo le righe tonali del ronzio, non il contenuto musicale vicino.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import traceback
from pathlib import Path

import numpy as np
import soundfile as sf
from scipy.signal import filtfilt, welch

from .config import MODELS, OUTPUT_FORMATS, QUALITY_PRESETS
from .worker import _install_signal_handlers, compute_peaks, emit, status

HUM_MAX_HZ = 400
HUM_GAIN_DB = -16.0
HUM_Q = 55.0
HUM_THRESHOLD_DB = 6.0
HUM_HARMONICS_FOR_SCORE = 12


def _decode_to_array(src: Path, temp_dir: Path) -> tuple[np.ndarray, int]:
    """Decodifica qualsiasi input in float32 PCM tramite ffmpeg."""
    temp = temp_dir / "_polish_input.wav"
    cmd = [
        "ffmpeg",
        "-y",
        "-v",
        "error",
        "-i",
        str(src),
        "-vn",
        "-c:a",
        "pcm_f32le",
        str(temp),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0 or not temp.is_file():
        tail = " | ".join((proc.stderr or "").splitlines()[-6:])
        raise RuntimeError(f"Impossibile decodificare lo stem. {tail}")
    data, sr = sf.read(str(temp), always_2d=True, dtype="float32")
    try:
        temp.unlink()
    except OSError:
        pass
    return np.ascontiguousarray(data.T), int(sr)


def _peak_score(freqs: np.ndarray, psd: np.ndarray, f0: float) -> tuple[float, int]:
    scores: list[float] = []
    nyquist = float(freqs[-1])
    for k in range(1, HUM_HARMONICS_FOR_SCORE + 1):
        f = f0 * k
        if f > min(HUM_MAX_HZ, nyquist * 0.92):
            break
        peak_mask = (freqs >= f - 1.2) & (freqs <= f + 1.2)
        floor_mask = ((freqs >= f - 10.0) & (freqs <= f - 2.5)) | (
            (freqs >= f + 2.5) & (freqs <= f + 10.0)
        )
        if not peak_mask.any() or not floor_mask.any():
            continue
        peak = float(psd[peak_mask].max())
        floor = float(np.median(psd[floor_mask]))
        if peak > 0 and floor > 0:
            scores.append(10.0 * np.log10(peak / floor))
    if not scores:
        return 0.0, 0
    return float(max(scores)), len(scores)


def detect_hum(audio: np.ndarray, sr: int) -> tuple[float | None, float]:
    """Ritorna (frequenza rilevata, punteggio in dB) oppure (None, score)."""
    mono = audio.mean(axis=0) if audio.ndim > 1 else audio
    if mono.size < 4096 or sr <= 0:
        return None, 0.0
    nperseg = min(65536, int(mono.size))
    try:
        freqs, psd = welch(
            mono.astype(np.float64),
            fs=sr,
            nperseg=nperseg,
            noverlap=nperseg // 2,
            scaling="density",
        )
    except ValueError:
        return None, 0.0

    score50, count50 = _peak_score(freqs, psd, 50.0)
    score60, count60 = _peak_score(freqs, psd, 60.0)
    if count50 < 4 and count60 < 4:
        return None, max(score50, score60)
    if score50 >= score60 and score50 >= HUM_THRESHOLD_DB:
        return 50.0, score50
    if score60 > score50 and score60 >= HUM_THRESHOLD_DB:
        return 60.0, score60
    return None, max(score50, score60)


def _peaking_biquad(f0: float, sr: int, gain_db: float = HUM_GAIN_DB, q: float = HUM_Q):
    """Biquad peaking con guadagno finito (non un notch infinito)."""
    a_amp = 10.0 ** (gain_db / 40.0)
    w0 = 2.0 * np.pi * f0 / sr
    alpha = np.sin(w0) / (2.0 * q)
    cos_w0 = np.cos(w0)
    b = np.array([1.0 + alpha * a_amp, -2.0 * cos_w0, 1.0 - alpha * a_amp])
    a = np.array([1.0 + alpha / a_amp, -2.0 * cos_w0, 1.0 - alpha / a_amp])
    return b / a[0], a / a[0]


def remove_hum(audio: np.ndarray, sr: int, f0: float) -> tuple[np.ndarray, int]:
    """Applica peaking zero-phase sulle prime armoniche del ronzio."""
    out = np.ascontiguousarray(audio.astype(np.float32, copy=True))
    applied = 0
    nyquist = sr / 2.0
    for k in range(1, 100):
        frequency = f0 * k
        if frequency >= min(HUM_MAX_HZ, nyquist * 0.92):
            break
        b, a = _peaking_biquad(frequency, sr)
        padlen = min(3 * max(len(a), len(b)), max(0, out.shape[-1] - 1))
        try:
            out = filtfilt(b, a, out, axis=-1, padlen=padlen).astype(np.float32)
        except ValueError:
            continue
        applied += 1
    return np.clip(out, -1.0, 1.0), applied


def _run_ffmpeg(args: list[str], timeout: int = 600) -> None:
    proc = subprocess.run(args, capture_output=True, text=True, check=False, timeout=timeout)
    if proc.returncode != 0:
        tail = " | ".join((proc.stderr or "").splitlines()[-6:])
        raise RuntimeError(f"FFmpeg non riuscito. {tail}")


def run_polish(args: argparse.Namespace) -> int:
    started = time.time()
    src = Path(args.input).resolve()
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    if not src.is_file():
        raise RuntimeError("Stem di input non trovato.")

    mode = args.mode
    residual = 0.0 if mode == "hum" else max(0.0, min(1.0, float(args.residual)))
    hum_mode = "off" if mode == "voice" else args.hum_mode
    if mode == "hum" and hum_mode == "off":
        raise RuntimeError("Nessuna pulizia selezionata.")

    out_format = OUTPUT_FORMATS[args.format]
    need_model = residual > 0
    original_np: np.ndarray | None = None
    cleaned_np: np.ndarray | None = None
    samplerate = 0

    if need_model:
        model_info = MODELS[args.model]
        preset = QUALITY_PRESETS[args.quality]
        status(
            4,
            f"Caricamento modello {model_info['label']} per il secondo passaggio…",
            "loading_model",
        )

        import torch
        from demucs.api import Separator, save_audio

        torch.set_grad_enabled(False)
        if args.device == "cpu":
            try:
                torch.set_num_threads(max(1, os.cpu_count() or 4))
            except Exception:
                pass

        shifts = max(1, int(preset["shifts"]))
        progress_state = {"last": 7.0, "last_emit": 0.0}

        def on_progress(data: dict) -> None:
            models = max(1, int(data.get("models", 1)))
            model_idx = int(data.get("model_idx_in_bag", 0))
            shift_idx = int(data.get("shift_idx", 0))
            audio_length = max(1, int(data.get("audio_length", 1)))
            offset = min(int(data.get("segment_offset", 0)), audio_length)
            units = models * shifts
            done = model_idx * shifts + shift_idx
            frac = offset / audio_length
            value = 8.0 + 66.0 * min(1.0, (done + frac) / units)
            now = time.time()
            if value - progress_state["last"] >= 0.5 and now - progress_state["last_emit"] >= 0.35:
                progress_state["last"] = value
                progress_state["last_emit"] = now
                pass_no = min(units, done + 1)
                emit(
                    event="progress",
                    progress=round(value, 1),
                    message=f"Analisi dei residui · passaggio {pass_no}/{units}",
                    stage="separating",
                )

        separator = Separator(
            model=args.model,
            device=args.device,
            shifts=preset["shifts"],
            overlap=preset["overlap"],
            split=True,
            progress=False,
            callback=on_progress,
        )

        status(10, "Analisi della strumentale…", "separating")
        origin, stems = separator.separate_audio_file(src)
        samplerate = separator.samplerate
        vocals = stems.get("vocals")
        if vocals is None:
            raise RuntimeError("Il modello non ha prodotto una traccia vocale residua.")

        status(78, "Riduzione controllata dei residui vocali…", "cleaning")
        cleaned_tensor = torch.clamp(origin - residual * vocals, -1.0, 1.0)
        original_np = origin.detach().cpu().numpy()
        cleaned_np = cleaned_tensor.detach().cpu().numpy()
    else:
        status(10, "Decodifica dello stem…", "loading")
        original_np, samplerate = _decode_to_array(src, out_dir)
        cleaned_np = original_np.copy()

    # ── Ronzio ───────────────────────────────────────────────────────────────
    hum_hz: float | None = None
    hum_score = 0.0
    hum_applied = False
    hum_bands = 0

    if hum_mode != "off":
        status(82, "Analisi del ronzio di fondo…", "hum")
        if hum_mode == "auto":
            hum_hz, hum_score = detect_hum(cleaned_np, samplerate)
            hum_applied = hum_hz is not None
        else:
            hum_hz = float(hum_mode)
            _, hum_score = detect_hum(cleaned_np, samplerate)
            hum_applied = True

        if hum_applied and hum_hz is not None:
            status(86, f"Rimozione ronzio a {int(hum_hz)} Hz…", "hum")
            cleaned_np, hum_bands = remove_hum(cleaned_np, samplerate, hum_hz)

    status(90, "Salvataggio dello stem pulito…", "exporting")
    if cleaned_np.ndim == 1:
        cleaned_np = cleaned_np[None, :]

    import torch
    from demucs.api import save_audio

    filename = f"clean.{out_format['ext']}"
    target = out_dir / filename
    save_audio(
        torch.from_numpy(np.ascontiguousarray(cleaned_np)),
        target,
        samplerate=samplerate,
        bitrate=320,
        preset=2,
        clip="rescale",
        bits_per_sample=24 if out_format["ext"] in {"wav", "flac"} else 16,
        as_float=False,
    )

    status(96, "Analisi della forma d'onda…", "analyzing")
    peaks = {
        "clean": compute_peaks(torch.from_numpy(np.ascontiguousarray(cleaned_np))),
        "original": compute_peaks(torch.from_numpy(np.ascontiguousarray(original_np))),
    }

    if mode == "hum":
        model_label = "Filtri notch · 50/60 Hz"
    else:
        model_label = f"{MODELS[args.model]['label']} · 2° passaggio"

    elapsed = time.time() - started
    manifest = {
        "version": 1,
        "kind": "polish",
        "mode": mode,
        "model": args.model,
        "model_label": model_label,
        "quality": args.quality,
        "target": args.target,
        "residual": round(residual, 3),
        "hum_mode": hum_mode,
        "hum_hz": hum_hz,
        "hum_score_db": round(hum_score, 1),
        "hum_applied": hum_applied,
        "hum_bands": hum_bands,
        "hum_gain_db": HUM_GAIN_DB,
        "format": args.format,
        "output_media": "audio",
        "duration": round(float(cleaned_np.shape[-1]) / samplerate, 2),
        "samplerate": samplerate,
        "files": {
            "clean": {
                "file": filename,
                "size": target.stat().st_size if target.exists() else 0,
                "key": "clean",
                "label": "Stem pulito",
                "media": "audio",
            }
        },
        "peaks": peaks,
        "elapsed": round(elapsed, 1),
        "device": args.device,
    }
    tmp = out_dir / "result.json.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, out_dir / "result.json")

    status(100, "Pulizia completata", "done")
    emit(event="done", manifest={k: v for k, v in manifest.items() if k != "peaks"})
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AI Voice Remover — polish worker")
    parser.add_argument("--input", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--model", required=True, choices=sorted(MODELS))
    parser.add_argument("--quality", required=True, choices=sorted(QUALITY_PRESETS))
    parser.add_argument("--format", required=True, choices=sorted(OUTPUT_FORMATS))
    parser.add_argument("--residual", type=float, required=True)
    parser.add_argument("--mode", default="both", choices=["both", "voice", "hum"])
    parser.add_argument("--hum-mode", default="auto", choices=["auto", "off", "50", "60"])
    parser.add_argument("--target", default="instrumental")
    parser.add_argument("--device", default="cpu")
    args = parser.parse_args(argv)

    _install_signal_handlers()
    try:
        return run_polish(args)
    except KeyboardInterrupt:
        emit(event="cancelled", message="Pulizia annullata")
        return 130
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
        message = str(exc).strip() or exc.__class__.__name__
        lowered = message.lower()
        if "out of memory" in lowered or ("cuda" in lowered and "memory" in lowered):
            message = (
                "Memoria GPU esaurita durante il secondo passaggio. Prova il "
                "preset «Turbo» o il modello «MDX Extra Q»."
            )
        elif "could not load" in lowered or "failed to load" in lowered:
            message = "Impossibile caricare il modello AI. Controlla la connessione e riprova."
        elif isinstance(exc, FileNotFoundError) or "ffmpeg" in lowered:
            message = "FFmpeg non disponibile: installalo e riprova."
        emit(event="error", message=message[:1200])
        return 1


if __name__ == "__main__":
    sys.exit(main())
