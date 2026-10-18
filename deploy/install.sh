#!/usr/bin/env bash
# 织序 WeaveOrder · 公网演示部署脚本（Ubuntu / Debian / Alibaba Cloud Linux / CentOS）
# 用法（在服务器上，代码已放到 /opt/weaveorder）：
#   sudo bash deploy/install.sh
# 作用：
#   • 安装 Python 依赖（venv）
#   • 注册并启动两个 systemd 服务：
#       weaveorder-web  :8000  Web 控制台（WEAVE_PUBLIC_DEMO=1，强制 Mock、限流）
#       weaveorder-a2a  :8001  A2A 智能体服务（默认 Mock）
#   • 如启用了 ufw，放行 8000/8001
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${PYTHON_BIN:-python3}"
DEMO_MODE="${DEMO_MODE:-mock}"   # mock | live（live 需要在 .env 配置 INTERN_API_KEY）

if [[ "$(id -u)" -ne 0 ]]; then
  echo "请用 root 或 sudo 运行：sudo bash deploy/install.sh" >&2
  exit 1
fi

echo "==> 应用目录：$APP_DIR"
if [[ ! -f "$APP_DIR/requirements.txt" ]]; then
  echo "未找到 requirements.txt，请先把代码放到 $APP_DIR" >&2
  exit 1
fi

echo "==> 安装系统依赖"
if command -v apt-get >/dev/null 2>&1; then
  apt-get update -y
  apt-get install -y python3 python3-venv python3-pip curl
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y python3 python3-pip curl
elif command -v yum >/dev/null 2>&1; then
  yum install -y python3 python3-pip curl
else
  echo "未识别的包管理器；请手动安装 python3 (>=3.9) 与 venv" >&2
fi

if ! command -v "$PY" >/dev/null 2>&1; then
  echo "找不到 $PY" >&2
  exit 1
fi
"$PY" - <<'PYCHK'
import sys
if sys.version_info < (3, 9):
    raise SystemExit(f"需要 Python >= 3.9，当前 {sys.version.split()[0]}")
print("Python", sys.version.split()[0])
PYCHK

echo "==> 创建虚拟环境并安装依赖"
"$PY" -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"

echo "==> 写入 systemd 服务（单 worker：运行注册表在进程内）"
cat > /etc/systemd/system/weaveorder-web.service <<EOF
[Unit]
Description=WeaveOrder public demo (mock only)
After=network.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
Environment=WEAVE_DEMO_MODE=$DEMO_MODE
Environment=WEAVE_APPROVAL_TIMEOUT=150
Environment=PYTHONUNBUFFERED=1
ExecStart=$APP_DIR/.venv/bin/uvicorn app.server:app --host 0.0.0.0 --port 8000 --workers 1 --proxy-headers
Restart=always
RestartSec=3
User=root

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/weaveorder-a2a.service <<EOF
[Unit]
Description=WeaveOrder A2A server (mock)
After=network.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
Environment=PYTHONUNBUFFERED=1
ExecStart=$APP_DIR/.venv/bin/python a2a_server.py --host 0.0.0.0 --port 8001
Restart=always
RestartSec=3
User=root

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now weaveorder-web.service
systemctl enable --now weaveorder-a2a.service

echo "==> 防火墙"
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -qi active; then
  ufw allow 8000/tcp || true
  ufw allow 8001/tcp || true
fi
if command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
  firewall-cmd --permanent --add-port=8000/tcp || true
  firewall-cmd --permanent --add-port=8001/tcp || true
  firewall-cmd --reload || true
fi

PUBLIC_IP="$(curl -fsS --max-time 8 https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')"
echo
echo "==> 完成"
systemctl --no-pager --lines=0 status weaveorder-web.service weaveorder-a2a.service || true
echo
echo "  控制台： http://$PUBLIC_IP:8000"
echo "  A2A   ： http://$PUBLIC_IP:8001/.well-known/agent-card.json"
echo
echo "注意：阿里云轻量应用服务器还需在「防火墙」里放行 TCP 8000 / 8001。"
