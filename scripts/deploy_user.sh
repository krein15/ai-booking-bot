#!/usr/bin/env bash
# Выкладка без Docker и root: бот работает от обычного пользователя в ~/booking-bot
# как пользовательская служба systemd (deploy/booking-bot.service).
# Подходит, пока нет прав root (Docker, HTTPS для сайта — scripts/deploy.sh).
#
#   bash scripts/deploy_user.sh
#
# Переменные: VPS=user@host  SSH_KEY=~/.ssh/id_ed25519  REMOTE_DIR=booking-bot
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# адрес сервера хранится вне git: .deploy.env (VPS=user@host) или переменная окружения
[[ -f "$ROOT/.deploy.env" ]] && source "$ROOT/.deploy.env"
VPS="${VPS:?укажите VPS=user@host в .deploy.env или в переменной окружения}"
SSH_KEY="${SSH_KEY:-$HOME/.ssh/id_ed25519}"
KNOWN_HOSTS="${KNOWN_HOSTS:-$HOME/.ssh/known_hosts}"
REMOTE_DIR="${REMOTE_DIR:-booking-bot}"
PYTHON="${PYTHON:-$ROOT/.venv/Scripts/python.exe}"
[[ -x "$PYTHON" ]] || PYTHON="$ROOT/.venv/bin/python"

ssh_vps() {
  ssh -i "$SSH_KEY" -o IdentitiesOnly=yes -o UserKnownHostsFile="$KNOWN_HOSTS" \
      -o StrictHostKeyChecking=yes -o BatchMode=yes "$VPS" "$@"
}

# На Ubuntu без пакета python3-venv нет ensurepip: окружение создаём без pip,
# а сам pip ставим из колеса, скачанного здесь с PyPI.
echo "→ Готовлю колесо pip"
mkdir -p "$ROOT/build/wheels"
"$PYTHON" -m pip download --quiet --disable-pip-version-check --no-deps -d "$ROOT/build/wheels" pip
[[ -f "$ROOT/certs/russian_trusted_ca.pem" ]] || sh "$ROOT/scripts/get_certs.sh" "$ROOT/certs/russian_trusted_ca.pem"

echo "→ Загружаю код на $VPS:~/$REMOTE_DIR"
tar -C "$ROOT" -czf - \
  --exclude=.venv --exclude=.git --exclude=.claude --exclude=data --exclude=assets --exclude=review \
  --exclude=.pytest_cache --exclude='__pycache__' --exclude=.env --exclude=.deploy.env --exclude=docs --exclude=tests \
  . | ssh_vps "mkdir -p ~/$REMOTE_DIR && tar -xzf - -C ~/$REMOTE_DIR"

if ! ssh_vps "test -f ~/$REMOTE_DIR/.env"; then
  echo "→ Первая выкладка: копирую .env"
  ssh_vps "umask 077 && cat > ~/$REMOTE_DIR/.env" < "$ROOT/.env"
fi

echo "→ Окружение, зависимости, запуск"
ssh_vps "bash -s" <<EOF
set -euo pipefail
cd ~/$REMOTE_DIR
chmod 600 .env
if [[ -d secrets ]]; then chmod 700 secrets; find secrets -type f -exec chmod 600 {} +; fi
set_env() { grep -q "^\$1=" .env && sed -i "s|^\$1=.*|\$1=\$2|" .env || echo "\$1=\$2" >> .env; }
grep -q '^IP_HASH_SALT=change-me' .env && set_env IP_HASH_SALT "\$(openssl rand -hex 16)" || true
set_env LLM_PROVIDER gigachat
set_env GIGACHAT_CA_BUNDLE_FILE certs/russian_trusted_ca.pem
set_env DATABASE_PATH data/bot.db

if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv --without-pip .venv
fi
if [[ ! -x .venv/bin/pip ]]; then
  PIP_WHEEL=\$(ls build/wheels/pip-*.whl | head -1)
  .venv/bin/python "\$PIP_WHEEL/pip" install --quiet --disable-pip-version-check --no-index "\$PIP_WHEEL"
fi
.venv/bin/pip install --quiet --disable-pip-version-check -r requirements.txt

# служба systemd пользователя: перезапуск при падении и автозапуск после перезагрузки
mkdir -p data ~/.config/systemd/user
cp deploy/booking-bot.service ~/.config/systemd/user/booking-bot.service
loginctl enable-linger "\$USER" 2>/dev/null || true
[[ "\$(loginctl show-user "\$USER" -p Linger --value 2>/dev/null)" == yes ]] \
  || echo "ВНИМАНИЕ: linger выключен — после перезагрузки сервера бот сам не запустится"
systemctl --user daemon-reload
systemctl --user enable booking-bot >/dev/null 2>&1
systemctl --user restart booking-bot
sleep 8
bash scripts/server_ctl.sh status
echo "--- лог ---"
bash scripts/server_ctl.sh logs 12
EOF
