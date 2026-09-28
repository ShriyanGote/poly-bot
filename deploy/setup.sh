#!/bin/bash
# Run once on a fresh Ubuntu 24.04 box, as root.
#   ssh root@<host> 'bash -s' < deploy/setup.sh
set -euo pipefail

echo "== packages =="
apt-get update -qq
apt-get install -y -qq python3.12-venv python3-pip git ca-certificates

echo "== user =="
id -u poly &>/dev/null || adduser --disabled-password --gecos "" poly

echo "== clone =="
sudo -u poly bash <<'USERPART'
set -euo pipefail
cd /home/poly
[ -d Polymarket ] || git clone https://github.com/ShriyanGote/poly-bot.git Polymarket
cd Polymarket
mkdir -p data logs
python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
USERPART

echo "== service =="
cp /home/poly/Polymarket/deploy/polymarket.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable polymarket

echo "== log rotation =="
cat > /etc/logrotate.d/polymarket <<'LOGROT'
/home/poly/Polymarket/logs/*.log {
    daily
    rotate 14
    compress
    missingok
    notifempty
    copytruncate
    su poly poly
}
LOGROT

echo "== disk guard =="
cp /home/poly/Polymarket/deploy/diskguard.sh /usr/local/bin/poly-diskguard
chmod +x /usr/local/bin/poly-diskguard
cat > /etc/cron.d/poly-diskguard <<'CRON'
*/15 * * * * root /usr/local/bin/poly-diskguard >> /var/log/poly-diskguard.log 2>&1
CRON

echo
echo "DONE. Two things left, both need your secrets:"
echo "  1. scp your .env to /home/poly/Polymarket/.env  (chmod 600, chown poly)"
echo "  2. systemctl start polymarket"
