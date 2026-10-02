#!/usr/bin/env bash
# agent-memory 安装脚本（macOS / Linux）
# 用法：
#   ./install.sh                  # 基础安装（venv + 依赖 + config 初始化 + CLI 软链）
#   ./install.sh --with-launchd   # macOS：安装 launchd 常驻服务
#   ./install.sh --with-systemd   # Linux：安装 systemd user 常驻服务
set -euo pipefail
cd "$(dirname "$0")"
MODE="${1:-}"

echo "==> 1/4 创建虚拟环境并安装依赖"
python3 -m venv .venv
./.venv/bin/pip install --quiet --disable-pip-version-check -r requirements.txt

echo "==> 2/4 初始化配置"
if [ ! -f config.json ]; then
	cp config.example.json config.json
	chmod 600 config.json
	echo "    已生成 config.json（0600）—— 请填写 bridge_token / mem0_* / llm 后继续"
else
	echo "    config.json 已存在，跳过"
fi

echo "==> 3/4 CLI 与 hooks"
chmod +x cli/mem hooks/*.py
mkdir -p ~/.local/bin logs state
ln -sf "$(pwd)/cli/mem" ~/.local/bin/mem
ln -sf "$(pwd)/cli/mem" ~/.local/bin/memory
case ":$PATH:" in
*":$HOME/.local/bin:"*) ;;
*) echo "    提示：请把 ~/.local/bin 加入 PATH" ;;
esac

echo "==> 4/4 完成"
echo "    前台启动: .venv/bin/uvicorn server:app --host 127.0.0.1 --port 8765 --app-dir \"$PWD/bridge\""
echo "    管理台  : http://127.0.0.1:8765/"

case "$MODE" in
--with-launchd)
	[ "$(uname)" = "Darwin" ] || {
		echo "launchd 仅限 macOS"
		exit 1
	}
	sed "s|__ROOT__|$PWD|g" deploy/launchd.dev.agent-memory.bridge.plist.tmpl \
		>~/Library/LaunchAgents/dev.agent-memory.bridge.plist
	launchctl bootout gui/$(id -u)/dev.agent-memory.bridge 2>/dev/null || true
	launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/dev.agent-memory.bridge.plist
	echo "    launchd 已安装并启动（dev.agent-memory.bridge）"
	;;
--with-systemd)
	[ "$(uname)" = "Linux" ] || {
		echo "systemd 仅限 Linux"
		exit 1
	}
	mkdir -p ~/.config/systemd/user
	sed "s|__ROOT__|$PWD|g" deploy/agent-memory-bridge.service.tmpl \
		>~/.config/systemd/user/agent-memory-bridge.service
	systemctl --user daemon-reload
	systemctl --user enable --now agent-memory-bridge.service
	echo "    systemd user 服务已安装并启动（loginctl enable-linger 可开机自启）"
	;;
esac
