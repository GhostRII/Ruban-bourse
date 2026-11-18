#!/usr/bin/env bash
# Installe le bandeau boursier sur le Raspberry Pi.
# À lancer depuis le dossier pi/ du dépôt, avec ton utilisateur normal (pas root) :  ./install.sh
set -e
if [ "$(id -u)" = 0 ]; then echo "!! Lance ce script avec ton utilisateur normal, pas en root (tape exit)"; exit 1; fi
SRC="$(cd "$(dirname "$0")" && pwd)"
DEST="$HOME/ticker"

echo "==> 1/5 Paquets système (python3-venv, curl, chromium)"
sudo apt update
sudo apt install -y python3-venv curl chromium

echo "==> 2/5 Environnement Python + Flask + yfinance"
mkdir -p "$DEST"
[ -d "$DEST/venv" ] || python3 -m venv "$DEST/venv"
LC_ALL=C.UTF-8 "$DEST/venv/bin/pip" install --timeout 60 -q --upgrade flask yfinance

echo "==> 3/5 Fichiers du bandeau"
cp "$SRC/server.py" "$SRC/index.html" "$SRC/edit.html" "$DEST/"
[ -f "$DEST/tickers.json" ] || echo '["AAPL","MSFT","NVDA","TSLA","^FCHI","MC.PA","BTC-EUR","ETH-EUR"]' > "$DEST/tickers.json"

echo "==> 4/5 Service systemd (démarrage automatique du serveur)"
pkill -f "python server.py" 2>/dev/null || true      # coupe un éventuel serveur lancé à la main
sudo tee /etc/systemd/system/ticker.service > /dev/null <<SVC
[Unit]
Description=Ticker bourse
After=network-online.target
Wants=network-online.target

[Service]
User=$USER
WorkingDirectory=$DEST
ExecStart=$DEST/venv/bin/python $DEST/server.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SVC
sudo systemctl daemon-reload
sudo systemctl enable ticker
sudo systemctl restart ticker

echo "==> 5/5 Chromium plein écran à l'ouverture de session"
mkdir -p "$HOME/.config/autostart"
cat > "$HOME/.config/autostart/ticker-kiosk.desktop" <<'KIO'
[Desktop Entry]
Type=Application
Name=Ticker kiosk
Exec=sh -c "sleep 6; exec /usr/bin/chromium --kiosk --noerrdialogs --disable-infobars --no-first-run --password-store=basic --disable-session-crashed-bubble http://localhost:8050"
KIO

sleep 15
echo; curl -s localhost:8050/api/quotes | head -c 300; echo
IP=$(hostname -I | awk '{print $1}')
echo
echo "===== FINI ====="
echo "Bandeau          : http://$IP:8050"
echo "Contrôle mobile  : http://$IP:8050/edit"
echo "Redémarre le Pi (sudo reboot) pour lancer le plein écran."
