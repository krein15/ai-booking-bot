#!/usr/bin/env bash
# Управление ботом на сервере (пользовательская служба systemd, без root).
#   bash scripts/server_ctl.sh start|stop|restart|status|logs [строк]
set -euo pipefail
cd "$(dirname "$0")/.."
UNIT=booking-bot

case "${1:-status}" in
  start | stop | restart)
    systemctl --user "$1" "$UNIT"
    echo "$1: готово"
    ;;
  status)
    echo "служба: $(systemctl --user is-active "$UNIT" 2>/dev/null || true), автозапуск: $(systemctl --user is-enabled "$UNIT" 2>/dev/null || true)"
    curl -s --max-time 5 http://127.0.0.1:8000/health || echo "API не отвечает"
    echo
    ;;
  logs)
    tail -n "${2:-50}" data/bot.log
    ;;
  *)
    echo "использование: $0 start|stop|restart|status|logs [строк]"
    exit 1
    ;;
esac
