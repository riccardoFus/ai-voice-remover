"""Worker di separazione.

Viene eseguito in un processo separato dal server web (uno per job) così che:
  * un crash di PyTorch/Demucs non abbatta la web app;
  * l'annullamento possa terminare il processo e liberare subito la GPU;
  * la memoria GPU non resti occupata tra un job e l'altro.

Comunicazione con il processo padre: una riga JSON per evento su stdout.
Tutto ciò che non è JSON (warning di torch, log di demucs) finisce su stderr.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
import traceback
from pathlib import Path

from .config import MODELS, OUTPUT_FORMATS, QUALITY_PRESETS, STEM_LABELS

PEAK_BUCKETS = 720
_CANCELLED = False


def emit(**payload) -> None:
    payload.setdefault("t", round(time.time(), 3))
    try:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    except (BrokenPipeError, OSError):
        # Il server è stato chiuso: nessun motivo per continuare.
        os._exit(0)


def status(progress: float, message: str, stage: str = "") -> None:
    emit(event="status", progress=round(progress, 1), message=message, stage=stage)


def _install_signal_handlers() -> None:
    def _handler(signum, frame):  # noqa: ARG001
        global _CANCELLED
        _CANCELLED = True
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


def compute_peaks(tensor, buckets: int = PEAK_BUCKETS) -> list[float]:
    """Forma d'onda compatta (0..1) per disegnarla nel browser."""
    import torch
    import torch.nn.functional as F

    if tensor.dim() == 2:
        mono = tensor.mean(dim=0)
    else:
        mono = tensor
    mono = mono.float().abs().reshape(1, 1, -1)
    if mono.shape[-1] == 0:
        return [0.0] * buckets
    pooled = F.adaptive_max_pool1d(mono, buckets).flatten()
    peak = float(pooled.max().item())
    if peak > 0:
        pooled = pooled / peak
    return [round(float(v), 3) for v in pooled]


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


def _write_manifest(path: Path, manifest: dict) -> None:
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def run(args: argparse.Namespace) -> int:
    started_at = time.time()

    model_info = MODELS[args.model]
    preset = QUALITY_PRESETS[args.quality]
    out_format = OUTPUT_FORMATS[args.format]
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    status(2, "Preparazione dell'ambiente…", "preparing")

    import torch

    torch.set_grad_enabled(False)
    torch.manual_seed(0)

    if args.device == "cpu":
        threads = int(os.environ.get("AVR_CPU_THREADS", "0") or 0)
        if threads <= 0:
            threads = max(1, (os.cpu_count() or 4))
        try:
            torch.set_num_threads(threads)
        except Exception:
            pass

    device_label = {
        "cuda": "GPU CUDA",
        "mps": "GPU Apple",
        "cpu": "CPU",
    }.get(args.device, args.device)

    status(
        4,
        f"Caricamento modello {model_info['label']} su {device_label}…",
        "loading_model",
    )

    from demucs.api import Separator

    # ── Progress callback ────────────────────────────────────────────────────
    shifts = max(1, int(preset["shifts"]))
    state = {"last_emit": 0.0, "last_value": 4.0}

    def on_progress(data: dict) -> None:
        global _CANCELLED
        if _CANCELLED:
            raise KeyboardInterrupt

        models = max(1, int(data.get("models", 1)))
        model_idx = int(data.get("model_idx_in_bag", 0))
        shift_idx = int(data.get("shift_idx", 0))
        audio_length = max(1, int(data.get("audio_length", 1)))
        offset = min(int(data.get("segment_offset", 0)), audio_length)
        units = models * shifts
        done = model_idx * shifts + shift_idx
        frac = offset / audio_length
        value = 8.0 + 70.0 * min(1.0, (done + frac) / units)

        now = time.time()
        if value - state["last_value"] >= 0.5 and now - state["last_emit"] >= 0.35:
            state["last_value"] = value
            state["last_emit"] = now
            pass_no = min(units, done + 1)
            emit(
                event="progress",
                progress=round(value, 1),
                message=f"Separazione in corso · passaggio {pass_no}/{units}",
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

    status(8, "Analisi del brano…", "separating")
    origin, stems = separator.separate_audio_file(Path(args.input))

    if _CANCELLED:
        raise KeyboardInterrupt

    samplerate = separator.samplerate
    duration = float(origin.shape[-1]) / float(samplerate)

    source_names = list(stems.keys())
    status(78, f"Unione degli stem strumentali ({len(source_names) - 1} tracce)…", "merging")

    # ── Strumentale = somma di tutti gli stem tranne la voce ─────────────────
    import torch

    accompaniment = None
    for name, tensor in stems.items():
        if name == "vocals":
            continue
        accompaniment = tensor.clone() if accompaniment is None else accompaniment + tensor
    if accompaniment is None:
        # Modello inatteso: usa la traccia originale meno la voce.
        accompaniment = origin - stems.get("vocals", torch.zeros_like(origin))
    accompaniment = torch.clamp(accompaniment, -1.0, 1.0)

    outputs = {"instrumental": accompaniment}
    for name, tensor in stems.items():
        outputs[name] = tensor

    # ── Salvataggio dei file ─────────────────────────────────────────────────
    from demucs.api import save_audio

    save_kwargs = {
        "samplerate": samplerate,
        "clip": "rescale",
        "bits_per_sample": 24 if out_format["ext"] in {"wav", "flac"} else 16,
        "as_float": False,
        "bitrate": 320,
        "preset": 2,
    }

    ordered = ["instrumental"] + [k for k in STEM_LABELS if k in outputs and k != "instrumental"]
    files: dict[str, dict] = {}
    total = max(1, len(ordered))
    for index, key in enumerate(ordered):
        if _CANCELLED:
            raise KeyboardInterrupt
        label = STEM_LABELS.get(key, {}).get("label", key)
        progress = 80.0 + 15.0 * (index / total)
        status(progress, f"Salvataggio {label.lower()}…", "exporting")

        filename = f"{key}.{out_format['ext']}"
        target = out_dir / filename
        save_audio(outputs[key], target, **save_kwargs)
        files[key] = {
            "file": filename,
            "size": target.stat().st_size if target.exists() else 0,
            "key": key,
            "label": STEM_LABELS.get(key, {}).get("label", key),
        }

    # ── Forme d'onda per il player del browser ───────────────────────────────
    status(96, "Generazione delle forme d'onda…", "analyzing")
    peaks: dict[str, list[float]] = {}
    for key in ["instrumental", "vocals", "original"] + [k for k in ordered if k not in {"instrumental", "vocals"}]:
        if _CANCELLED:
            raise KeyboardInterrupt
        tensor = origin if key == "original" else outputs.get(key)
        if tensor is None:
            continue
        peaks[key] = compute_peaks(tensor)

    elapsed = time.time() - started_at
    manifest = {
        "version": 1,
        "model": args.model,
        "model_label": model_info["label"],
        "quality": args.quality,
        "format": args.format,
        "samplerate": samplerate,
        "duration": round(duration, 2),
        "sources": source_names,
        "files": files,
        "peaks": peaks,
        "elapsed": round(elapsed, 1),
        "device": args.device,
    }
    _write_manifest(out_dir / "result.json", manifest)
    status(100, "Separazione completata", "done")
    emit(event="done", manifest={k: v for k, v in manifest.items() if k != "peaks"})
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AI Voice Remover — separation worker")
    parser.add_argument("--input", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--model", required=True, choices=sorted(MODELS))
    parser.add_argument("--quality", required=True, choices=sorted(QUALITY_PRESETS))
    parser.add_argument("--format", required=True, choices=sorted(OUTPUT_FORMATS))
    parser.add_argument("--device", default="cpu")
    args = parser.parse_args(argv)

    _install_signal_handlers()

    try:
        return run(args)
    except KeyboardInterrupt:
        emit(event="cancelled", message="Elaborazione annullata")
        return 130
    except Exception as exc:  # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
        message = str(exc).strip() or exc.__class__.__name__
        lowered = message.lower()
        if "out of memory" in lowered or ("cuda error" in lowered and "memory" in lowered):
            message = (
                "Memoria GPU esaurita. Prova il preset «Turbo» oppure il modello "
                "«MDX Extra Q», che richiede meno memoria."
            )
        elif isinstance(exc, ModuleNotFoundError) and "demucs" in lowered:
            message = "Demucs non è installato. Esegui ./install.sh e riprova."
        elif isinstance(exc, FileNotFoundError) or "ffmpeg" in lowered:
            message = (
                "FFmpeg non è disponibile o non riesce a leggere il file. "
                "Installa FFmpeg e riprova."
            )
        elif "loadaudio" in lowered or "invalid audio" in lowered or "sphn" in lowered or "could not read the file" in lowered:
            message = (
                "Impossibile leggere il file audio. Verifica che non sia corrotto "
                "o prova a convertirlo in WAV/MP3."
            )
        elif (
            "could not load" in lowered
            or "failed to load" in lowered
            or "connection" in lowered
            or "download" in lowered
            or "huggingface" in lowered
            or "timed out" in lowered
        ):
            message = (
                "Impossibile scaricare il modello AI. Controlla la connessione "
                "a internet e riprova: il download riprenderà da dove si era fermato."
            )
        emit(event="error", message=message)
        return 1


if __name__ == "__main__":
    sys.exit(main())
