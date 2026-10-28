#!/usr/bin/env bash
set -euo pipefail

# 必须 root
if [[ $EUID -ne 0 ]]; then
    exec sudo "$0" "$@"
fi

# 安装 microsocks 和 curl
if ! command -v microsocks >/dev/null 2>&1; then
    echo "[+] Installing microsocks..."
    apt-get update -qq
    apt-get install -y microsocks curl
elif ! command -v curl >/dev/null 2>&1; then
    apt-get update -qq
    apt-get install -y curl
fi

# 生成随机字符串
rand_hex() {
    od -An -N"$1" -tx1 /dev/urandom | tr -d ' \n'
}

metadata_get() {
    curl -fsS \
        -H "Metadata-Flavor: Google" \
        "http://metadata.google.internal/computeMetadata/v1/instance/attributes/$1" \
        2>/dev/null || true
}

PORT="${SOCKS5_PORT:-${1:-}}"
USERNAME="${SOCKS5_USERNAME:-${2:-}}"
PASSWORD="${SOCKS5_PASSWORD:-${3:-}}"

if [[ -z "$PORT" ]]; then
    PORT="$(metadata_get socks5-port)"
fi

if [[ -z "$USERNAME" ]]; then
    USERNAME="$(metadata_get socks5-username)"
fi

if [[ -z "$PASSWORD" ]]; then
    PASSWORD="$(metadata_get socks5-password)"
fi

if [[ -z "$USERNAME" ]]; then
    USERNAME="u$(rand_hex 4)"
fi

if [[ -z "$PASSWORD" ]]; then
    PASSWORD="$(rand_hex 12)"
fi

if [[ ! "$PORT" =~ ^[0-9]+$ ]] || [[ "$PORT" -lt 1 ]] || [[ "$PORT" -gt 65535 ]]; then
    # 随机找一个未占用的高位端口
    while true; do
        N=$(od -An -N2 -tu2 /dev/urandom | tr -d ' ')
        PORT=$((20000 + N % 40000))

        if ! ss -ltn 2>/dev/null | awk '{print $4}' | grep -qE ":${PORT}$"; then
            break
        fi
    done
fi

UNIT="quick-socks5-${PORT}"

echo "[+] Starting SOCKS5..."

systemd-run \
    --quiet \
    --unit="$UNIT" \
    --property=Restart=always \
    --property=RestartSec=1 \
    -- \
    "$(command -v microsocks)" \
    -i 0.0.0.0 \
    -p "$PORT" \
    -u "$USERNAME" \
    -P "$PASSWORD"

# 如果 UFW 开着，就顺便开放端口
if command -v ufw >/dev/null 2>&1; then
    if ufw status 2>/dev/null | grep -q "^Status: active"; then
        ufw allow "${PORT}/tcp" >/dev/null
        UFW_OPENED=yes
    else
        UFW_OPENED=no
    fi
else
    UFW_OPENED=no
fi

# 尝试获取 IP
PUBLIC_IP=""

if command -v curl >/dev/null 2>&1; then
    PUBLIC_IP="$(curl -4 -fsS --max-time 3 https://api.ipify.org 2>/dev/null || true)"
fi

if [[ -z "$PUBLIC_IP" ]]; then
    PUBLIC_IP="$(ip route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([^ ]*\).*/\1/p' | head -n1)"
fi

echo
echo "========================================"
echo "        SOCKS5 started successfully"
echo "========================================"
echo
echo "IP       : ${PUBLIC_IP:-<YOUR_SERVER_IP>}"
echo "Port     : $PORT"
echo "Username : $USERNAME"
echo "Password : $PASSWORD"
echo
echo "SOCKS5:"
echo "socks5://${USERNAME}:${PASSWORD}@${PUBLIC_IP:-YOUR_SERVER_IP}:${PORT}"
echo
echo "Test:"
echo "curl --proxy 'socks5h://${USERNAME}:${PASSWORD}@${PUBLIC_IP:-YOUR_SERVER_IP}:${PORT}' https://api.ipify.org"
echo
echo "Status:"
echo "systemctl status ${UNIT}.service"
echo
echo "Stop:"
echo "sudo systemctl stop ${UNIT}.service"

if [[ "$UFW_OPENED" == "yes" ]]; then
    echo
    echo "Remove firewall rule:"
    echo "sudo ufw delete allow ${PORT}/tcp"
fi

echo
echo "========================================"
