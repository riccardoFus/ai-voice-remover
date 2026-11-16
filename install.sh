#!/usr/bin/env bash
# Installa (o ripara) l'ambiente di AI Voice Remover.
set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"
PREFETCH=1
DESKTOP=0
DENOISE=1

usage() {
  cat <<'EOF'
Uso: ./install.sh [opzioni]

  --no-prefetch     Non scaricare subito i pesi del modello (verranno presi al primo uso)
  --no-denoise      Non installare il motore AI di denoise (DeepFilterNet3)
  --denoise         Installa/ripara solo il motore denoise
  --desktop         Crea anche la voce nel menu applicazioni Linux
  -h, --help        Mostra questo aiuto

Variabili utili:
  PYTHON=python3.12         interprete da usare
  TORCH_INDEX_URL=...       indice per l'installazione CPU di PyTorch
  AVR_SKIP_PREFETCH=1       equivalente a --no-prefetch
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --no-prefetch) PREFETCH=0; shift ;;
    --no-denoise) DENOISE=0; shift ;;
    --denoise) DENOISE=2; shift ;;
    --desktop) DESKTOP=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Opzione non riconosciuta: $1" >&2; usage; exit 2 ;;
  esac
done

say() { printf '\n\033[1;35m▸ %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m⚠ %s\033[0m\n' "$*"; }

if [ "$DENOISE" = "2" ]; then
  exec bash scripts/install_denoise.sh
fi

# ── Python minimo ─────────────────────────────────────────────────────────────
if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "Python non trovato: installa Python 3.10+ oppure imposta PYTHON=/percorso/python3" >&2
  exit 1
fi

if ! "$PYTHON" - <<'PY'
import sys
if sys.version_info < (3, 10):
    sys.exit(f"Python {sys.version.split()[0]} troppo vecchio: serve 3.10 o successivo.")
PY
then
  exit 1
fi

say "Python: $("$PYTHON" -c 'import sys; print(sys.version.split()[0])')"

# ── Virtualenv ────────────────────────────────────────────────────────────────
if [ ! -x ".venv/bin/python" ]; then
  if "$PYTHON" -c "import torch" >/dev/null 2>&1; then
    say "Creo la virtualenv (riuso PyTorch di sistema, utile per la GPU)"
    "$PYTHON" -m venv --system-site-packages .venv
  else
    say "Creo la virtualenv"
    "$PYTHON" -m venv .venv
  fi
fi

VENV_PY=".venv/bin/python"

# ── PyTorch ───────────────────────────────────────────────────────────────────
if ! "$VENV_PY" -c "import torch" >/dev/null 2>&1; then
  warn "PyTorch non è visibile nella virtualenv: installo la build CPU."
  .venv/bin/pip install --upgrade pip >/dev/null
  .venv/bin/pip install --index-url "${TORCH_INDEX_URL:-https://download.pytorch.org/whl/cpu}" torch
fi

# ── Dipendenze della web app ─────────────────────────────────────────────────
say "Installo le dipendenze (Flask, Demucs, Waitress…)"
.venv/bin/pip install -r requirements.txt

# ── FFmpeg ────────────────────────────────────────────────────────────────────
if command -v ffmpeg >/dev/null 2>&1 && command -v ffprobe >/dev/null 2>&1; then
  say "FFmpeg trovato: $(ffmpeg -version 2>/dev/null | head -n1)"
else
  warn "FFmpeg/ffprobe non trovati. Su Debian/Ubuntu: sudo apt install ffmpeg"
  warn "Senza FFmpeg alcuni formati audio non potranno essere letti o convertiti."
fi

# ── Verifica ──────────────────────────────────────────────────────────────────
if ! "$VENV_PY" -c "import flask, demucs, waitress, soundfile, numpy, scipy" >/dev/null 2>&1; then
  echo "La verifica delle dipendenze è fallita." >&2
  exit 1
fi
say "Dipendenze verificate ✓"

# ── Pesi del modello (best effort) ────────────────────────────────────────────
if [ "$PREFETCH" = "1" ] && [ "${AVR_SKIP_PREFETCH:-0}" != "1" ]; then
  say "Preparo il modello AI (il primo download può richiedere qualche minuto)"
  if ! "$VENV_PY" scripts/prefetch_models.py --model "${AVR_PREFETCH_MODEL:-htdemucs_ft}"; then
    warn "Download non completato. Nessun problema: verrà ripreso al primo utilizzo."
  fi
fi

# ── Motore denoise AI (opzionale, ambiente separato) ─────────────────────────
if [ "$DENOISE" = "1" ]; then
  say "Preparo il motore denoise AI (DeepFilterNet3, ambiente separato)"
  if ! bash scripts/install_denoise.sh; then
    warn "Denoise AI non disponibile. La separazione stem funziona comunque."
    warn "Puoi riprovare con: ./install.sh --denoise"
  fi
fi

# ── Voce nel menu applicazioni ────────────────────────────────────────────────
if [ "$DESKTOP" = "1" ]; then
  say "Creo la voce nel menu applicazioni"
  scripts/install_desktop_entry.sh || warn "Impossibile creare la voce nel menu."
fi

cat <<'EOF'

┌──────────────────────────────────────────────────────────────┐
│  Installazione completata.                                   │
│                                                              │
│  Avvio:   ./run.sh                                           │
│  Browser: http://127.0.0.1:8791                              │
│                                                              │
│  Ctrl+C ferma il server.                                     │
└──────────────────────────────────────────────────────────────┘

EOF
