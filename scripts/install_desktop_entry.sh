#!/usr/bin/env bash
# Crea una voce "AI Voice Remover" nel menu applicazioni dell'utente.
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DESKTOP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ENTRY="$DESKTOP_DIR/ai-voice-remover.desktop"

mkdir -p "$DESKTOP_DIR"
chmod +x "$APP_DIR/run.sh" 2>/dev/null || true

cat > "$ENTRY" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=AI Voice Remover
GenericName=Voice & Stem Separator
Comment=Separa voce e strumentale dai tuoi brani con l'AI
Exec="${APP_DIR}/run.sh"
Path=${APP_DIR}
Icon=${APP_DIR}/static/favicon.svg
Terminal=false
Categories=AudioVideo;Audio;Utility;
Keywords=audio;voce;vocals;demucs;stem;karaoke;musica;
StartupNotify=true
EOF

chmod +x "$ENTRY"
if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$DESKTOP_DIR" >/dev/null 2>&1 || true
fi

echo "Voce creata: $ENTRY"
echo "Cerca «AI Voice Remover» nel menu applicazioni."
