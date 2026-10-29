#!/bin/bash
cd "$(dirname "$0")" || exit 1
echo "本地游戏服务端启动中…（Ctrl+C 停止）"
python3 server.py --port 3000
