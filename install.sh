#!/bin/bash
# Klartext - Installer (macOS, Apple Silicon empfohlen)
# Richtet whisper.cpp, ffmpeg, Ollama, das Python-venv und den
# Menu-Bar-Agenten (LaunchAgent) ein. Alles lokal, nichts verlaesst den Mac.
set -e

HERE="$(cd "$(dirname "$0")" && pwd)"
INSTALL="$HOME/.wa-transcribe"
MODELDIR="$HOME/.whisper-models"
MODEL="$MODELDIR/ggml-large-v3-turbo.bin"
PLIST="$HOME/Library/LaunchAgents/com.local.klartext.plist"

echo "==> Klartext installieren"

# 1. Abhaengigkeiten
if ! command -v brew >/dev/null 2>&1; then
  echo "Homebrew wird benoetigt: https://brew.sh"; exit 1
fi
for pkg in whisper-cpp ffmpeg ollama; do
  brew list "$pkg" >/dev/null 2>&1 || brew install "$pkg"
done

# 2. Whisper-Modell (~1.6 GB)
mkdir -p "$MODELDIR"
if [ ! -f "$MODEL" ]; then
  echo "==> Lade Whisper-Modell large-v3-turbo (~1.6 GB)"
  curl -L --fail -o "$MODEL" \
    "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo.bin"
fi

# 3. Ollama-Modell fuer Zusammenfassung/Uebersetzung (optional)
brew services start ollama >/dev/null 2>&1 || true
sleep 2
ollama pull qwen2.5:7b || echo "  (qwen2.5 uebersprungen - Zusammenfassung/Uebersetzung erst nach 'ollama pull qwen2.5:7b')"

# 4. App + venv
mkdir -p "$INSTALL"
cp "$HERE/app.py" "$INSTALL/app.py"
if [ ! -d "$INSTALL/venv" ]; then
  python3 -m venv "$INSTALL/venv"
fi
"$INSTALL/venv/bin/pip" install --quiet --upgrade pip
"$INSTALL/venv/bin/pip" install --quiet -r "$HERE/requirements.txt"

# 5. LaunchAgent (Autostart + Hintergrund)
sed "s|__HOME__|$HOME|g" "$HERE/com.local.klartext.plist.template" > "$PLIST"
launchctl bootout "gui/$(id -u)/com.local.klartext" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"

echo ""
echo "==> Fertig. Das Waveform-Icon erscheint oben in der Menueleiste."
echo ""
echo "WICHTIG - Vollzugriff auf Festplatte erlauben, sonst bleibt die Liste leer:"
echo "  Systemeinstellungen -> Datenschutz & Sicherheit -> Vollzugriff auf Festplatte"
echo "  -> dort das Python-Binary aus $INSTALL/venv/bin/ hinzufuegen und aktivieren."
echo ""
echo "Stoppen:      launchctl bootout gui/\$(id -u)/com.local.klartext"
echo "Deinstallieren: rm -rf $INSTALL \"$PLIST\""
