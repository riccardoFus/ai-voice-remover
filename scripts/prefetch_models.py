#!/usr/bin/env python3
"""Scarica in anticipo i pesi Demucs (best effort).

Uso:
    python scripts/prefetch_models.py [--model htdemucs_ft]
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from avr.config import MODELS, model_is_cached  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="htdemucs_ft", choices=sorted(MODELS))
    args = parser.parse_args()

    if model_is_cached(args.model):
        print(f"Modello {args.model} già presente nella cache HuggingFace.")
        return 0

    label = MODELS[args.model]["label"]
    print(f"Download del modello {label} ({args.model})…")
    started = time.time()
    try:
        from demucs.pretrained import get_model

        model = get_model(args.model)
        sources = len(getattr(model, "sources", []))
        print(
            f"Modello pronto: {sources} stem, scaricato in "
            f"{time.time() - started:.1f}s."
        )
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"Download non riuscito: {exc}", file=sys.stderr)
        print(
            "Puoi riprovare più tardi: il modello verrà scaricato al primo job.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
