#!/bin/sh
# Скачивает корневой и промежуточный сертификаты Минцифры (НУЦ) с сайта Госуслуг
# и складывает их в один файл. Нужен только для соединения с GigaChat:
# в систему он не устанавливается.
set -eu

OUT="${1:-certs/russian_trusted_ca.pem}"
BASE="https://gu-st.ru/content/lending"
mkdir -p "$(dirname "$OUT")"
TMP="$(mktemp)"

for name in russian_trusted_root_ca_pem.crt russian_trusted_sub_ca_pem.crt; do
  curl -fsSL --retry 3 "$BASE/$name" -o "$TMP"
  if ! grep -q "BEGIN CERTIFICATE" "$TMP"; then
    echo "Файл $name не похож на сертификат" >&2
    exit 1
  fi
  cat "$TMP"
  echo
done > "$OUT"

rm -f "$TMP"
echo "Сертификаты сохранены в $OUT"
