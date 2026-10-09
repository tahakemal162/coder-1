#!/bin/sh
cd /app
python3 serve/server.py --ckpt /app/c1art --tokenizer /app/c1art/tok --port 9100 &
sleep 4
exec python3 agent/main.py
