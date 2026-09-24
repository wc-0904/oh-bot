#!/bin/sh
set -eu
cd "$(dirname "$0")"

bot_pids() {
    pgrep -f '[p]ython oh_bot.py' || true
}

case "${1:-}" in
start)
    if [ -n "$(bot_pids)" ]; then
        echo "Already running."
        exit 0
    fi
    nohup ./venv/bin/python oh_bot.py >> oh_bot.log 2>&1 &
    echo "Started."
    ;;
stop)
    pids=$(bot_pids)
    if [ -z "$pids" ]; then
        echo "Not running."
        exit 0
    fi
    kill $pids || true
    i=0
    while [ "$i" -lt 10 ]; do
        if [ -z "$(bot_pids)" ]; then
            echo "Stopped."
            exit 0
        fi
        sleep 0.5
        i=$((i + 1))
    done
    kill -9 $(bot_pids) || true
    echo "Stopped."
    ;;
log)
    touch oh_bot.log
    exec tail -f oh_bot.log
    ;;
*)
    echo "Usage: ./bot.sh start|stop|log" >&2
    exit 1
    ;;
esac
