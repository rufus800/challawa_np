#!/bin/bash
# Challawa Pump Dashboard - Setup Script for IoT2050
# Run this script on your IoT2050 device

echo "========================================"
echo "  Challawa Pump Dashboard Setup"
echo "========================================"

# Update system
echo "Updating system..."
apt-get update

# Install dependencies
echo "Installing Python dependencies..."
pip3 install flask flask-socketio python-snap7 eventlet

# Install cloudflared
echo "Installing Cloudflare Tunnel..."
if ! command -v cloudflared &> /dev/null; then
    curl -L --output cloudflared.deb https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64.deb
    dpkg -i cloudflared.deb
    rm cloudflared.deb
fi

# Setup systemd service for the app
echo "Setting up auto-start service..."
cp challawa.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable challawa.service
systemctl start challawa.service

echo ""
echo "========================================"
echo "  App Service Setup Complete!"
echo "========================================"
echo ""
echo "Check status: systemctl status challawa"
echo "View logs: journalctl -u challawa -f"
echo ""
echo "========================================"
echo "  Now setting up Cloudflare Tunnel"
echo "========================================"
echo ""
echo "Run these commands manually:"
echo ""
echo "1. Login to Cloudflare:"
echo "   cloudflared tunnel login"
echo ""
echo "2. Create tunnel:"
echo "   cloudflared tunnel create challawanp"
echo ""
echo "3. Route DNS (replace with your tunnel ID):"
echo "   cloudflared tunnel route dns challawanp challawanp.akfotekengineering.com"
echo ""
echo "4. Create config file:"
echo "   nano ~/.cloudflared/config.yml"
echo ""
echo "   Add this content:"
echo "   ---"
echo "   tunnel: <YOUR-TUNNEL-ID>"
echo "   credentials-file: /root/.cloudflared/<YOUR-TUNNEL-ID>.json"
echo "   ingress:"
echo "     - hostname: challawanp.akfotekengineering.com"
echo "       service: http://localhost:5050"
echo "     - service: http_status:404"
echo ""
echo "5. Install tunnel as service:"
echo "   cloudflared service install"
echo "   systemctl start cloudflared"
echo "   systemctl enable cloudflared"
echo ""
echo "========================================"
