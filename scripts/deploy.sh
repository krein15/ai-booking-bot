#!/usr/bin/env bash
# Выкладка бота на VPS по SSH (ключом, без паролей).
#
#   bash scripts/deploy.sh            # залить код и перезапустить бота
#   bash scripts/deploy.sh --https    # то же + HTTPS через Caddy (нужна запись DNS)
#
# Настройки — переменные окружения:
#   VPS=user@host  SSH_KEY=~/.ssh/id_ed25519  REMOTE_DIR=booking-bot
#   BOT_DOMAIN=bot.example.ru  BOT_HTTPS_PORT=8444
#
# На сервере нужен Docker, а пользователь — в группе docker.
# .env уходит на сервер только при первой выкладке; дальше правьте его там.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# адрес сервера хранится вне git: .deploy.env (VPS=user@host) или переменная окружения
[[ -f "$ROOT/.deploy.env" ]] && source "$ROOT/.deploy.env"
VPS="${VPS:?укажите VPS=user@host в .deploy.env или в переменной окружения}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_ed25519}"
KNOWN_HOSTS="${KNOWN_HOSTS:-$HOME/.ssh/known_hosts}"
REMOTE_DIR="${REMOTE_DIR:-booking-bot}"
BOT_DOMAIN="${BOT_DOMAIN:-bot.example.ru}"
BOT_HTTPS_PORT="${BOT_HTTPS_PORT:-8444}"
PROFILE=""
[[ "${1:-}" == "--https" ]] && PROFILE="--profile https"

ssh_vps() {
  ssh -i "$SSH_KEY" -o IdentitiesOnly=yes -o UserKnownHostsFile="$KNOWN_HOSTS" \
      -o StrictHostKeyChecking=yes -o BatchMode=yes "$VPS" "$@"
}

echo "→ Загружаю код на $VPS:~/$REMOTE_DIR"
tar -C "$ROOT" -czf - \
  --exclude=.venv --exclude=.git --exclude=.claude --exclude=data --exclude=certs \
  --exclude=assets --exclude=review --exclude=.pytest_cache --exclude='__pycache__' \
  --exclude=.env --exclude=.deploy.env --exclude=docs \
  . | ssh_vps "mkdir -p ~/$REMOTE_DIR && tar -xzf - -C ~/$REMOTE_DIR"

if ! ssh_vps "test -f ~/$REMOTE_DIR/.env"; then
  echo "→ Первая выкладка: копирую .env"
  ssh_vps "umask 077 && cat > ~/$REMOTE_DIR/.env" < "$ROOT/.env"
fi
if [[ -d "$ROOT/secrets" ]]; then
  echo "→ Обновляю secrets/"
  tar -C "$ROOT" -czf - secrets | ssh_vps "cd ~/$REMOTE_DIR && umask 077 && tar -xzf -"
fi

echo "→ Настраиваю .env на сервере и запускаю"
ssh_vps "bash -s" <<EOF
set -euo pipefail
cd ~/$REMOTE_DIR
chmod 600 .env
mkdir -p secrets && chmod 755 secrets && find secrets -type f -exec chmod 644 {} +
set_env() { grep -q "^\$1=" .env && sed -i "s|^\$1=.*|\$1=\$2|" .env || echo "\$1=\$2" >> .env; }
grep -q '^IP_HASH_SALT=change-me' .env && set_env IP_HASH_SALT "\$(openssl rand -hex 16)"
set_env CORS_ORIGINS "https://kreinstudio.ru,https://www.kreinstudio.ru,https://$BOT_DOMAIN:$BOT_HTTPS_PORT"
set_env BOT_DOMAIN "$BOT_DOMAIN"
set_env BOT_HTTPS_PORT "$BOT_HTTPS_PORT"
docker compose $PROFILE up -d --build --remove-orphans
sleep 5
docker compose $PROFILE ps
echo "--- логи бота ---"
docker compose logs --tail 15 bot
EOF
