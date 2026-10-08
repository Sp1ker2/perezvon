#!/bin/sh
# Установка сервера «Перезвона» на 89.124.122.158. Только ДОБАВЛЯЕТ, ничего существующего не удаляет:
#   пользователь perezvon, /opt/perezvon, /var/lib/perezvon, служба perezvon,
#   location /perezvon/ в /etc/nginx/sites-available/sidekick-https (с резервной копией и nginx -t).
# Ожидает в /root/: perezvon_server.py, perezvon.service, config.json (кладётся scp заранее).
set -e
id perezvon >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin perezvon
install -d -m 755 /opt/perezvon
install -m 644 /root/perezvon_server.py /opt/perezvon/perezvon_server.py
install -d -m 700 -o perezvon -g perezvon /var/lib/perezvon
[ -f /var/lib/perezvon/config.json ] || install -m 600 -o perezvon -g perezvon /root/config.json /var/lib/perezvon/config.json
install -m 644 /root/perezvon.service /etc/systemd/system/perezvon.service
rm -f /root/config.json /root/perezvon.service /root/perezvon_server.py

F=/etc/nginx/sites-available/sidekick-https
if ! grep -q "location /perezvon/" "$F"; then
  B=/root/nginx-sidekick-https.bak-perezvon-$(date +%Y%m%d-%H%M%S)
  cp -a "$F" "$B"; echo "резервная копия nginx: $B"
  python3 - "$F" <<'PY'
import sys
p = sys.argv[1]
s = open(p, encoding="utf-8").read()
block = """	# «Перезвон»: API программ (служба perezvon, 127.0.0.1:8767)
	location /perezvon/ {
		proxy_pass http://127.0.0.1:8767;
		proxy_set_header Host $host;
		proxy_set_header X-Real-IP $remote_addr;
		proxy_set_header X-Forwarded-Proto https;
		client_max_body_size 3m;
		proxy_read_timeout 60s;
		add_header Cache-Control "no-store";
	}
"""
anchor = "\tlocation / {\n\t\treturn 404;"
assert s.count(anchor) == 1, "не нашёл location / — ничего не меняю"
open(p, "w", encoding="utf-8").write(s.replace(anchor, block + anchor))
PY
  if ! nginx -t; then cp -a "$B" "$F"; echo "nginx -t не прошёл — вернул как было"; exit 1; fi
  nginx -s reload
fi
systemctl daemon-reload
systemctl enable --now perezvon
sleep 3
systemctl is-active perezvon
curl -s http://127.0.0.1:8767/api/ping; echo
