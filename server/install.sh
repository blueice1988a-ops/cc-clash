#!/usr/bin/env bash
# Cc clash 中转服务器一键安装脚本（Ubuntu / Debian）
# 用法：curl -fsSL https://raw.githubusercontent.com/blueice1988a-ops/cc-clash/smart_core/server/install.sh | sudo bash
set -euo pipefail

MIHOMO_VERSION="v1.19.32"
REPO_RAW="https://raw.githubusercontent.com/blueice1988a-ops/cc-clash/smart_core/server"
HOME_DIR="/opt/ccrelay"

if [ "$(id -u)" -ne 0 ]; then
  echo "请用 sudo 运行：curl -fsSL $REPO_RAW/install.sh | sudo bash"
  exit 1
fi

echo "==> 安装依赖"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 curl nano ca-certificates >/dev/null

echo "==> 下载 mihomo $MIHOMO_VERSION"
case "$(uname -m)" in
  x86_64) arch="amd64-compatible" ;;
  aarch64 | arm64) arch="arm64" ;;
  *) echo "不支持的 CPU 架构：$(uname -m)"; exit 1 ;;
esac
mkdir -p "$HOME_DIR/bin" "$HOME_DIR/data" "$HOME_DIR/core"
chmod 700 "$HOME_DIR/data" "$HOME_DIR/core"
curl -fsSL "https://github.com/MetaCubeX/mihomo/releases/download/$MIHOMO_VERSION/mihomo-linux-$arch-$MIHOMO_VERSION.gz" \
  | gunzip > "$HOME_DIR/bin/mihomo.new"
chmod +x "$HOME_DIR/bin/mihomo.new"
mv "$HOME_DIR/bin/mihomo.new" "$HOME_DIR/bin/mihomo"

echo "==> 下载管理工具"
curl -fsSL "$REPO_RAW/ccrelay.py" -o "$HOME_DIR/ccrelay.py.new"
mv "$HOME_DIR/ccrelay.py.new" "$HOME_DIR/ccrelay.py"
cat > /usr/local/bin/ccrelay <<EOF
#!/bin/sh
exec python3 $HOME_DIR/ccrelay.py "\$@"
EOF
chmod +x /usr/local/bin/ccrelay

echo "==> 生成服务器密钥"
public_ip="$(curl -4 -fsS --max-time 10 https://api.ipify.org || curl -4 -fsS --max-time 10 https://ifconfig.me)"
keys="$("$HOME_DIR/bin/mihomo" generate reality-keypair)"
private_key="$(echo "$keys" | awk '/PrivateKey/ {print $2}')"
public_key="$(echo "$keys" | awk '/PublicKey/ {print $2}')"
# 已经安装过时只会更新 IP，不会改动已有的朋友和密钥
python3 "$HOME_DIR/ccrelay.py" init "$public_ip" "$private_key" "$public_key"

echo "==> 开启 BBR 加速"
cat > /etc/sysctl.d/99-ccrelay.conf <<EOF
net.core.default_qdisc=fq
net.ipv4.tcp_congestion_control=bbr
EOF
sysctl --system >/dev/null 2>&1 || true

echo "==> 设置开机自动运行"
cat > /etc/systemd/system/ccrelay-core.service <<EOF
[Unit]
Description=Cc clash relay core (mihomo)
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=$HOME_DIR/bin/mihomo -d $HOME_DIR/core
Restart=always
RestartSec=3
LimitNOFILE=1048576

[Install]
WantedBy=multi-user.target
EOF
cat > /etc/systemd/system/ccrelay-web.service <<EOF
[Unit]
Description=Cc clash relay subscription and usage service
After=ccrelay-core.service

[Service]
ExecStart=/usr/bin/python3 $HOME_DIR/ccrelay.py serve
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable ccrelay-core ccrelay-web >/dev/null 2>&1
systemctl restart ccrelay-core
sleep 2
systemctl restart ccrelay-web
sleep 1

# Ubuntu 自带的防火墙如果开着，放行 80 和 443
if command -v ufw >/dev/null 2>&1 && ufw status | grep -q "Status: active"; then
  ufw allow 80/tcp >/dev/null
  ufw allow 443/tcp >/dev/null
fi

if systemctl is-active --quiet ccrelay-core && systemctl is-active --quiet ccrelay-web; then
  echo
  echo "✅ 安装完成！服务器 IP：$public_ip"
  echo
  echo "下一步："
  echo "  1. 在云服务器网页后台的“防火墙”里，确认 TCP 80 和 TCP 443 端口已放行"
  echo "  2. 添加朋友：ccrelay add 名字 订阅链接"
  echo "  3. 修改分流规则：ccrelay rules"
  echo "  4. 查看所有命令：ccrelay"
else
  echo "❌ 服务没有正常启动，请把下面的内容发给 Claude："
  systemctl status ccrelay-core ccrelay-web --no-pager | tail -20
  exit 1
fi
