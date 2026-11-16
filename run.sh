#!/usr/bin/env bash
# Avvia AI Voice Remover. Se l'ambiente non è pronto, lo prepara automaticamente.
set -euo pipefail
cd "$(dirname "$0")"

OPEN_BROWSER=1
HOST="${AVR_HOST:-127.0.0.1}"
PORT="${AVR_PORT:-8791}"
MODE="run"
PID_FILE="data/server.pid"

usage() {
  cat <<'EOF'
Uso: ./run.sh [opzioni]

  --background      Avvia il server in background (resta attivo anche chiudendo il terminale)
  --stop            Ferma l'istanza in esecuzione
  --status          Mostra se il server è attivo
  --no-browser      Non aprire il browser
  --port PORTA      Porta HTTP (default 8791)
  --host INDIRIZZO  Indirizzo di ascolto (default 127.0.0.1)
  --setup           Esegui solo installazione/riparazione
  -h, --help        Mostra questo aiuto

Variabili: AVR_PORT, AVR_HOST, AVR_DEVICE=cuda|cpu|auto,
           AVR_RETENTION_HOURS, AVR_MODEL, AVR_FORMAT.
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --background) MODE="background"; shift ;;
    --stop) MODE="stop"; shift ;;
    --status) MODE="status"; shift ;;
    --no-browser) OPEN_BROWSER=0; shift ;;
    --port) PORT="${2:?Numero di porta mancante}"; shift 2 ;;
    --port=*) PORT="${1#*=}"; shift ;;
    --host) HOST="${2:?Indirizzo mancante}"; shift 2 ;;
    --host=*) HOST="${1#*=}"; shift ;;
    --setup) MODE="setup"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Opzione non riconosciuta: $1" >&2; usage; exit 2 ;;
  esac
done

if [ "$MODE" = "setup" ]; then
  exec ./install.sh
fi

export AVR_HOST="$HOST"
export AVR_PORT="$PORT"

BROWSER_HOST="$HOST"
if [ "$BROWSER_HOST" = "0.0.0.0" ] || [ "$BROWSER_HOST" = "::" ]; then
  BROWSER_HOST="127.0.0.1"
fi
URL="http://${BROWSER_HOST}:${PORT}"

open_browser() {
  local target="$1"
  if [ "${AVR_NO_BROWSER:-0}" = "1" ] || [ "$OPEN_BROWSER" = "0" ]; then
    return 0
  fi
  if [ -n "${BROWSER:-}" ]; then
    "$BROWSER" "$target" >/dev/null 2>&1 || true
  elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$target" >/dev/null 2>&1 || true
  elif command -v sensible-browser >/dev/null 2>&1; then
    sensible-browser "$target" >/dev/null 2>&1 || true
  fi
}

health_check() {
  if command -v curl >/dev/null 2>&1; then
    curl -fsS --max-time 1.5 "${URL}/api/health" 2>/dev/null || true
  elif [ -x ".venv/bin/python" ]; then
    .venv/bin/python - "${URL}" <<'PY' 2>/dev/null || true
import sys
import urllib.request
try:
    with urllib.request.urlopen(sys.argv[1] + "/api/health", timeout=1.5) as response:
        sys.stdout.write(response.read().decode("utf-8", "replace"))
except Exception:
    pass
PY
  fi
}

server_pid() {
  if [ -f "$PID_FILE" ]; then
    local pid cmd
    pid="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
      if [ -r "/proc/$pid/cmdline" ]; then
        cmd="$(tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null || true)"
        case "$cmd" in
          *app.py*|*run.sh*) printf '%s' "$pid"; return 0 ;;
          *) return 1 ;;
        esac
      fi
      printf '%s' "$pid"
      return 0
    fi
  fi
  return 1
}

# ── Stato ────────────────────────────────────────────────────────────────────
if [ "$MODE" = "status" ]; then
  HEALTH="$(health_check)"
  if printf '%s' "$HEALTH" | grep -q "AI Voice Remover"; then
    PID="$(server_pid || true)"
    echo "AI Voice Remover è attivo su ${URL}${PID:+ (PID $PID)}"
    exit 0
  fi
  echo "AI Voice Remover non è in esecuzione (${URL})."
  exit 1
fi

# ── Stop ─────────────────────────────────────────────────────────────────────
if [ "$MODE" = "stop" ]; then
  PID="$(server_pid || true)"
  if [ -z "$PID" ]; then
    if ! health_check | grep -q "AI Voice Remover"; then
      rm -f "$PID_FILE"
      echo "Nessuna istanza di AI Voice Remover in esecuzione."
      exit 0
    fi
    if command -v ss >/dev/null 2>&1; then
      PID="$(ss -ltnp 2>/dev/null | grep -E "[:.]${PORT}\b" | grep -oP 'pid=\K[0-9]+' | head -n1 || true)"
    fi
  fi
  if [ -z "$PID" ]; then
    echo "Il server risponde ma non riesco a trovare il PID. Usa Ctrl+C nel terminale di avvio." >&2
    exit 1
  fi
  kill "$PID" 2>/dev/null || true
  for _ in $(seq 1 30); do
    if ! kill -0 "$PID" 2>/dev/null; then break; fi
    sleep 0.2
  done
  if kill -0 "$PID" 2>/dev/null; then
    kill -9 "$PID" 2>/dev/null || true
  fi
  rm -f "$PID_FILE"
  echo "AI Voice Remover fermato."
  exit 0
fi

# ── Ambiente pronto? ─────────────────────────────────────────────────────────
if [ ! -x ".venv/bin/python" ] || ! .venv/bin/python -c "import flask, demucs, waitress, soundfile, scipy" >/dev/null 2>&1; then
  echo "Prima configurazione necessaria: preparo l'ambiente…"
  ./install.sh
fi

# ── Istanza già attiva? ──────────────────────────────────────────────────────
HEALTH="$(health_check)"
if printf '%s' "$HEALTH" | grep -q "AI Voice Remover"; then
  echo "AI Voice Remover è già in esecuzione: ${URL}"
  open_browser "$URL"
  exit 0
fi

# ── Porta occupata da un altro servizio ──────────────────────────────────────
if command -v ss >/dev/null 2>&1 && ss -ltn 2>/dev/null | awk '{print $4}' | grep -Eq "[:.]${PORT}$"; then
  echo "La porta ${PORT} è già occupata da un altro servizio." >&2
  echo "Avvia con una porta diversa:  ./run.sh --port 8799" >&2
  exit 1
fi

# ── Modalità background ──────────────────────────────────────────────────────
if [ "$MODE" = "background" ]; then
  mkdir -p logs data
  nohup "$0" --no-browser --host "$HOST" --port "$PORT" >>logs/server.log 2>&1 &
  CHILD="$!"
  printf '%s' "$CHILD" > "$PID_FILE"

  for _ in $(seq 1 50); do
    sleep 0.4
    if health_check | grep -q "AI Voice Remover"; then
      echo "AI Voice Remover è attivo su ${URL} (PID ${CHILD})."
      echo "Log: logs/server.log — per fermarlo: ./run.sh --stop"
      open_browser "$URL"
      exit 0
    fi
    if ! kill -0 "$CHILD" 2>/dev/null; then
      echo "Avvio in background non riuscito. Ultime righe del log:" >&2
      tail -n 20 logs/server.log >&2 || true
      exit 1
    fi
  done

  echo "Il server non ha risposto entro 20 secondi. Controlla logs/server.log" >&2
  exit 1
fi

# ── Avvio in primo piano ─────────────────────────────────────────────────────
if [ "$OPEN_BROWSER" = "1" ] && [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ] && [ "${AVR_NO_BROWSER:-0}" != "1" ]; then
  ( sleep 1.8; open_browser "$URL" ) &
fi

echo "Avvio AI Voice Remover su ${URL} …"
exec .venv/bin/python app.py
