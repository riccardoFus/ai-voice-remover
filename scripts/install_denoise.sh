#!/usr/bin/env bash
# Prepara l'ambiente isolato per il denoise AI (DeepFilterNet3).
#
# Perché una virtualenv separata?
#   DeepFilterNet richiede Python 3.11 + una versione di torch/torchaudio
#   compatibile, mentre Demucs usa il Python e il PyTorch di sistema (con CUDA).
#   Tenere i due ambienti separati evita downgrade e conflitti.
set -euo pipefail
cd "$(dirname "$0")/.."

VENV=".denoise-venv"
PY="$VENV/bin/python"
MARKER="$VENV/.ready"
UV="${UV:-$(command -v uv || true)}"

say() { printf '\n\033[1;36m▸ %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m⚠ %s\033[0m\n' "$*"; }

if [ -x "$PY" ] && "$PY" -c "import df, torch, torchaudio, soundfile" >/dev/null 2>&1; then
  if [ ! -f "$MARKER" ]; then
    # Ambiente già funzionante (es. creato prima del marker): adottalo.
    touch "$MARKER"
  fi
  say "Motore denoise già pronto"
  exit 0
fi

# ── Trova Python 3.11 ─────────────────────────────────────────────────────────
PY311="${PYTHON311:-}"
if [ -z "$PY311" ]; then
  for candidate in \
    "$(command -v python3.11 2>/dev/null || true)" \
    /usr/bin/python3.11 \
    "$HOME/miniconda3/bin/python3.11" \
    "$HOME"/miniconda3/envs/*/bin/python3.11 \
    "$HOME"/anaconda3/envs/*/bin/python3.11
  do
    if [ -n "$candidate" ] && [ -x "$candidate" ] && \
       "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 11) else 1)' 2>/dev/null; then
      PY311="$candidate"
      break
    fi
  done
fi

# ── Crea la virtualenv ────────────────────────────────────────────────────────
if [ -n "$UV" ]; then
  say "Creo l'ambiente denoise con uv (Python 3.11)"
  rm -rf "$VENV"
  "$UV" venv --python 3.11 "$VENV"
elif [ -n "$PY311" ]; then
  say "Creo l'ambiente denoise con $PY311"
  rm -rf "$VENV"
  "$PY311" -m venv "$VENV"
else
  warn "Python 3.11 o uv non trovati: il denoise AI non può essere installato."
  warn "Installa uv (https://astral.sh/uv) oppure python3.11 + python3.11-venv,"
  warn "poi rilancia: ./install.sh --denoise"
  exit 1
fi

if [ ! -x "$PY" ]; then
  warn "Creazione della virtualenv non riuscita."
  exit 1
fi

# ── Dipendenze ────────────────────────────────────────────────────────────────
# torch/torchaudio 2.4.x sono compatibili con l'API usata da DeepFilterNet 0.5.6.
# La build CPU è sufficiente: il denoise è molto più leggero della separazione.
say "Installo PyTorch CPU per il denoise"
if [ -n "$UV" ]; then
  "$UV" pip install --python "$PY" "torch==2.4.1" "torchaudio==2.4.1" \
    --index-url https://download.pytorch.org/whl/cpu
  "$UV" pip install --python "$PY" "deepfilternet==0.5.6" "soundfile>=0.12"
else
  "$VENV/bin/pip" install --upgrade pip >/dev/null
  "$VENV/bin/pip" install "torch==2.4.1" "torchaudio==2.4.1" \
    --index-url https://download.pytorch.org/whl/cpu
  "$VENV/bin/pip" install "deepfilternet==0.5.6" "soundfile>=0.12"
fi

# ── Verifica + download del modello ──────────────────────────────────────────
say "Scarico e verifico DeepFilterNet3 (pochi MB)"
if "$PY" - <<'PY'
import numpy as np
import torch
from df.enhance import enhance, init_df

model, state, _suffix = init_df(
    "DeepFilterNet3", post_filter=False, log_level="error", log_file=None
)
probe = torch.zeros(1, 48000, dtype=torch.float32)
with torch.inference_mode():
    enhance(model, state, probe, pad=True, atten_lim_db=8)
print("DeepFilterNet3 operativo")
PY
then
  touch "$MARKER"
  say "Motore denoise pronto ✓"
  exit 0
fi

warn "Verifica del motore denoise non riuscita."
exit 1
