# -*- coding: utf-8 -*-
"""Общее для тестов: сервер «Перезвона» в отдельном потоке + поддельный Telegram API."""
import json
import os
import shutil
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "server"))

import perezvon_server as srv  # noqa: E402


class FakeTelegram:
    """Отвечает как api.telegram.org и записывает все вызовы."""

    def __init__(self):
        self.calls = []
        self.lock = threading.Lock()
        self.msg_id = 100
        owner = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
                method = self.path.rsplit("/", 1)[-1]
                with owner.lock:
                    owner.calls.append((method, body))
                    owner.msg_id += 1
                    mid = owner.msg_id
                if method == "getMe":
                    res = {"id": 1, "is_bot": True, "username": "perezvon_test_bot"}
                elif method == "getUpdates":
                    res = []
                elif method in ("sendMessage", "editMessageText"):
                    res = {"message_id": mid, "chat": {"id": body.get("chat_id")}, "text": body.get("text")}
                else:
                    res = True
                raw = json.dumps({"ok": True, "result": res}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.httpd.server_port
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def sent(self, chat=None, method="sendMessage"):
        with self.lock:
            return [b for m, b in self.calls if m == method and (chat is None or b.get("chat_id") == chat)]

    def last_text(self, chat):
        msgs = [b for m, b in self.calls if m in ("sendMessage", "editMessageText") and b.get("chat_id") == chat]
        return msgs[-1]["text"] if msgs else ""

    def last_buttons(self, chat):
        msgs = [b for m, b in self.calls if m in ("sendMessage", "editMessageText") and b.get("chat_id") == chat
                and b.get("reply_markup")]
        if not msgs:
            return []
        return [btn for row in msgs[-1]["reply_markup"]["inline_keyboard"] for btn in row]

    def clear(self):
        with self.lock:
            self.calls.clear()

    def close(self):
        self.httpd.shutdown()


class TestServer:
    """App + HTTP на случайном порту, данные во временной папке. bot=True — с поддельным Telegram."""

    def __init__(self, bot=False, cfg=None):
        self.dir = tempfile.mkdtemp(prefix="pz-srv-")
        if cfg:
            with open(os.path.join(self.dir, "config.json"), "w", encoding="utf-8") as f:
                json.dump(cfg, f)
        self.app = srv.App(self.dir)
        self.tg = None
        if bot:
            self.tg = FakeTelegram()
            self.app.bot = srv.Bot(self.app, "TEST:TOKEN", api_base=self.tg.url)
            self.app.bot.username = "perezvon_test_bot"
        handler = type("H", (srv.Handler,), {"app": self.app})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.httpd.daemon_threads = True
        self.url = "http://127.0.0.1:%d/perezvon" % self.httpd.server_port
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        if self.tg:
            self.tg.close()
        self.app.db.c.close()
        shutil.rmtree(self.dir, ignore_errors=True)


def tg_msg(chat, text, username=None, first="Тест"):
    frm = {"id": chat, "first_name": first}
    if username:
        frm["username"] = username
    return {"update_id": 1, "message": {"message_id": 1, "chat": {"id": chat, "type": "private"},
                                        "from": frm, "text": text}}


def tg_cb(chat, data, mid=555):
    return {"update_id": 2, "callback_query": {"id": "cb1", "data": data, "from": {"id": chat},
                                               "message": {"message_id": mid, "chat": {"id": chat}}}}
