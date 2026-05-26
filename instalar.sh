#!/bin/bash
set -e
echo "🐾 PetFeeder IoT v4 — Instalando"
echo "=================================="

sudo apt update -qq
sudo apt install -y python3 python3-pip python3-venv -qq
sudo raspi-config nonint do_i2c 0 2>/dev/null || true

python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip -q
pip install flask pymysql pyserial -q
pip install adafruit-circuitpython-pca9685 adafruit-circuitpython-motor -q 2>/dev/null || true
pip install adafruit-circuitpython-dht -q 2>/dev/null || true

SCRIPT_DIR=$(pwd)
sudo bash -c "cat > /etc/systemd/system/prueba2.service << EOF
[Unit]
Description=PetFeeder IoT v4
After=network.target mariadb.service
[Service]
Type=simple
User=$USER
WorkingDirectory=$SCRIPT_DIR
ExecStart=$SCRIPT_DIR/venv/bin/python app.py
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
EOF"

sudo systemctl daemon-reload
sudo systemctl enable prueba2

echo ""
echo "✅ Instalación completa!"
echo "   sudo systemctl start prueba2"
echo "   http://$(hostname -I | awk '{print $1}'):5001"
echo "   demo@petfeeder.com / demo123"
echo ""
echo "☁️  PiTunnel: pitunnel --port=5001 --http --name=petfeeder --persist"
