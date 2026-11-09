#!/usr/bin/env bash
# Install / upgrade the PC Power agent (CachyOS / Arch; also works on Ubuntu / Pop!_OS).
# Optional: ddcutil (monitors). Set allow_power_control = true in /etc/pc-power/config.toml for power buttons.
#   sudo ./install.sh            install or upgrade (keeps existing token)
#   sudo ./install.sh --uninstall
set -euo pipefail
[[ $EUID -eq 0 ]] || { echo "run with sudo"; exit 1; }
HERE="$(cd "$(dirname "$0")" && pwd)"
LIB=/usr/local/lib/pc-power CONF=/etc/pc-power/config.toml UNIT=/etc/systemd/system/pc-power-agent.service
AVAHI=/etc/avahi/services/pc-power.service

if [[ ${1:-} == --uninstall ]]; then
  systemctl disable --now pc-power-agent 2>/dev/null || true
  rm -rf "$LIB" "$UNIT" "$AVAHI"; systemctl daemon-reload
  echo "removed (config kept in /etc/pc-power)"; exit 0
fi

python3 -c 'import sys; assert sys.version_info >= (3, 11)' || { echo "need python >= 3.11"; exit 1; }
install -Dm755 "$HERE/pc_power_agent.py" "$LIB/pc_power_agent.py"
install -Dm644 "$HERE/pc-power-agent.service" "$UNIT"

if [[ ! -f $CONF ]]; then
  TOKEN=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')
  install -Dm600 /dev/null "$CONF"
  cat > "$CONF" <<CFG
listen = "0.0.0.0"
port = 8765
token = "$TOKEN"
sample_interval = 1.0
# gpu_fallback_w = 5            # for NVIDIA GPUs that report no power (e.g. some GTX 10xx)
allow_power_control = false     # true = HA gets Suspend / Shut down / Restart buttons
allow_monitor_control = true    # HA can set monitor brightness over DDC/CI
allow_session_lock = true       # HA gets a Lock screen button
CFG
fi
PORT=$(sed -n 's/^port *= *\([0-9]*\).*/\1/p' "$CONF"); PORT=${PORT:-8765}

if [[ -d /etc/avahi/services ]]; then
  MID=$(cat /etc/machine-id)
  cat > "$AVAHI" <<XML
<?xml version="1.0" standalone='no'?>
<!DOCTYPE service-group SYSTEM "avahi-service.dtd">
<service-group>
  <name replace-wildcards="yes">PC Power on %h</name>
  <service>
    <type>_pc-power._tcp</type>
    <port>$PORT</port>
    <txt-record>machine_id=$MID</txt-record>
    <txt-record>hostname=$(hostname)</txt-record>
    <txt-record>api=1</txt-record>
  </service>
</service-group>
XML
  systemctl is-active -q avahi-daemon || echo "note: avahi-daemon not running; HA auto-discovery won't work (sudo systemctl enable --now avahi-daemon)"
fi

if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  ufw allow "$PORT"/tcp comment "pc-power-agent" >/dev/null
elif command -v firewall-cmd >/dev/null && firewall-cmd --state >/dev/null 2>&1; then
  firewall-cmd --permanent --add-port="$PORT"/tcp >/dev/null && firewall-cmd --reload >/dev/null
fi

if command -v ddcutil >/dev/null; then
  echo i2c-dev > /etc/modules-load.d/pc-power.conf; modprobe i2c-dev 2>/dev/null || true
else
  echo "note: install ddcutil for monitor power/brightness (CachyOS/Arch: sudo pacman -S ddcutil)"
fi

systemctl daemon-reload
systemctl enable -q pc-power-agent
systemctl restart pc-power-agent
sleep 3
python3 "$LIB/pc_power_agent.py" --config "$CONF" --summary 2>/dev/null || true
echo
echo "PC Power agent running on port $PORT"
echo "Token: $(sed -n 's/^token *= *"\(.*\)"/\1/p' "$CONF")"
