#!/bin/sh
# Mimics `npm run dev`: a shell process that spawns the real server as a child.
python3 "$(dirname "$0")/slow_server.py" "$1" 1
