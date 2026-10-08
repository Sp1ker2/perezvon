#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Сервер «Перезвона»: API для программ + Telegram-бот управления.

Только стандартная библиотека Python 3.9+. Один файл — так проще выкладывать.

  python3 perezvon_server.py                       — запустить (API + бот + оповещения)
  python3 perezvon_server.py --create-admin "Имя" [--room vn2] [--tg ник]  — первый админ, печатает код
  python3 perezvon_server.py --list-users

Каталог данных: $PEREZVON_DATA (по умолчанию /var/lib/perezvon):
  config.json  — {"bot_token": "...", "tz": "Europe/Moscow", "port": 8767, "bind": "127.0.0.1",
                  "missed_grace_min": 10}
  perezvon.db  — SQLite
"""
import argparse
import datetime as dt
import hashlib
import html
import json
import math
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

VERSION = "1.0.0"
LIMITS = {"fio": 200, "phone": 64, "note": 500, "name": 80, "room": 40, "pc": 80}
STATUSES = ("active", "done", "deleted")
ROLE_TITLE = {"operator": "Оператор", "admin": "Админ", "superadmin": "Супер-админ"}
ROLE_ICON = {"operator": "", "admin": "🛡 ", "superadmin": "👑 "}
ADMIN_ROLES = ("admin", "superadmin")


def is_admin(u):
    """Админ и супер-админ: видят все перезвоны всех комнат."""
    return bool(u) and u.get("role") in ADMIN_ROLES


def is_super(u):
    """Супер-админ: ещё и управляет пользователями, комнатами и ролями."""
    return bool(u) and u.get("role") == "superadmin"


def sees_room(u, room):
    """Админ видит только свою комнату, супер-админ — все."""
    if is_super(u):
        return True
    return is_admin(u) and (room or "").casefold() == (u.get("room") or "").casefold()
ONLINE_SEC = 60


def log(*a):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *a, flush=True)


def clean(s, limit):
    s = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", str(s if s is not None else ""))
    return re.sub(r"\s+", " ", s).strip()[:limit]


def sha(s):
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def norm_code(s):
    return re.sub(r"\D", "", s or "")


_RU2EN = dict(zip("йцукенгшщзхъфывапролджэячсмитьбюё", "qwertyuiop[]asdfghjkl;'zxcvbnm,.`"))


def norm_username(s):
    """@Anna_S, anna_s, «фттф_ы» (набрано в русской раскладке) → anna_s. None, если не похоже на ник."""
    s = (s or "").strip().lower()
    s = "".join(_RU2EN.get(ch, ch) for ch in s)
    for pre in ("https://t.me/", "http://t.me/", "t.me/"):
        if s.startswith(pre):
            s = s[len(pre):]
    s = s.lstrip("@\"'").strip()
    return s if re.fullmatch(r"[a-z][a-z0-9_]{3,31}", s) else None


def new_code():
    d = "".join(secrets.choice("0123456789") for _ in range(12))
    return "%s-%s-%s" % (d[:4], d[4:8], d[8:])


def fnum(v, default=None):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return default
    return f if math.isfinite(f) else default


def phone_copy(s):
    d = re.sub(r"\D", "", s or "")
    if len(d) == 11 and d[0] in "78":
        return "+7" + d[1:]
    if len(d) == 10 and d[0] == "9" and not (s or "").strip().startswith("+"):
        return "+7" + d
    return ("+" if (s or "").strip().startswith("+") else "") + d if d else (s or "")


# ═══════════════════════════════ база ═══════════════════════════════

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS rooms(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE COLLATE NOCASE,
    created REAL);
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, room TEXT, role TEXT,
    code_hash TEXT UNIQUE, tg_chat INTEGER, tg_name TEXT, tg_notify INTEGER DEFAULT 1,
    blocked INTEGER DEFAULT 0, created REAL, tg_username TEXT);
CREATE TABLE IF NOT EXISTS devices(token_hash TEXT PRIMARY KEY, user_id INTEGER, app TEXT, pc TEXT,
    version TEXT, last_seen REAL, skew REAL DEFAULT 0, created REAL);
CREATE TABLE IF NOT EXISTS items(id TEXT PRIMARY KEY, user_id INTEGER, operator TEXT, room TEXT,
    fio TEXT, phone TEXT, note TEXT, created REAL, due REAL, status TEXT, attempts INTEGER DEFAULT 0,
    snoozes INTEGER DEFAULT 0, done_at REAL, done_by TEXT, updated REAL, rev INTEGER,
    tg_reminded INTEGER DEFAULT 0, missed_notified INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS items_user_rev ON items(user_id, rev);
CREATE INDEX IF NOT EXISTS items_due ON items(due);
CREATE INDEX IF NOT EXISTS items_status ON items(status);
"""


class DB:
    def __init__(self, path):
        self.lock = threading.RLock()
        self.c = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.c.row_factory = sqlite3.Row
        self.c.execute("PRAGMA journal_mode=WAL")
        self.c.execute("PRAGMA synchronous=NORMAL")
        self.c.executescript(SCHEMA)
        cols = {r[1] for r in self.c.execute("PRAGMA table_info(users)")}
        if "tg_username" not in cols:
            self.c.execute("ALTER TABLE users ADD COLUMN tg_username TEXT")
        self.c.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_tg_username ON users(tg_username)")
        with self.lock:
            if self.meta("epoch") is None:
                self.set_meta("epoch", secrets.token_hex(8))
                self.set_meta("rev", "0")
            if not self.c.execute("SELECT 1 FROM rooms").fetchone():
                self.c.execute("INSERT INTO rooms(name, created) VALUES('vn2', ?)", (time.time(),))

    def meta(self, k, default=None):
        r = self.one("SELECT v FROM meta WHERE k=?", (k,))
        return r[0] if r else default

    def set_meta(self, k, v):
        self.x("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                       (k, str(v)))

    def next_rev(self):
        r = int(self.meta("rev", "0")) + 1
        self.set_meta("rev", r)
        return r

    # одно соединение на все потоки → каждое обращение под общим (реентерабельным) замком
    def q(self, sql, args=()):
        with self.lock:
            return self.c.execute(sql, args).fetchall()

    def one(self, sql, args=()):
        with self.lock:
            return self.c.execute(sql, args).fetchone()

    def x(self, sql, args=()):
        with self.lock:
            return self.c.execute(sql, args)

    # транзакция поверх общего замка
    def tx(self):
        db = self

        class _T:
            def __enter__(s):
                db.lock.acquire()
                db.c.execute("BEGIN IMMEDIATE")
                return db

            def __exit__(s, et, ev, tb):
                try:
                    db.c.execute("COMMIT" if et is None else "ROLLBACK")
                finally:
                    db.lock.release()
                return False
        return _T()


class App:
    """Вся предметная логика: и HTTP, и бот ходят сюда."""

    def __init__(self, data_dir):
        os.makedirs(data_dir, exist_ok=True)
        self.dir = data_dir
        self.cfg_path = os.path.join(data_dir, "config.json")
        self.cfg = {}
        if os.path.exists(self.cfg_path):
            with open(self.cfg_path, encoding="utf-8") as f:
                self.cfg = json.load(f)
        self.db = DB(os.path.join(data_dir, "perezvon.db"))
        self.grace = int(self.cfg.get("missed_grace_min", 10)) * 60
        self.tz = None
        if ZoneInfo is not None:
            try:
                self.tz = ZoneInfo(self.cfg.get("tz", "Europe/Moscow"))
            except Exception:
                self.tz = None
        # вход просто по нику; "login_confirm": true в config.json — дополнительно кнопка «Да, это я» в Telegram
        self.confirm = bool(self.cfg.get("login_confirm", False))
        # владельцы — Telegram ID; всегда супер-админы, их нельзя понизить, отключить или удалить
        self.owners = {int(x) for x in self.cfg.get("owners", [])}
        self.pending = {}          # id запроса входа → {...}
        self.fails = {}            # ключ → [время неудачных попыток]
        self.fail_lock = threading.Lock()
        self.bot = None

    # ── время в часовом поясе офиса (для бота)
    def local(self, ts):
        return dt.datetime.fromtimestamp(ts, self.tz) if self.tz else dt.datetime.fromtimestamp(ts)

    def hm(self, ts, now=None):
        now = time.time() if now is None else now
        t, n = self.local(ts), self.local(now)
        d = (t.date() - n.date()).days
        s = t.strftime("%H:%M")
        return s if d == 0 else ("завтра " + s if d == 1 else ("вчера " + s if d == -1 else t.strftime("%d.%m ") + s))

    def day_bounds(self, now=None):
        n = self.local(time.time() if now is None else now)
        start = n.replace(hour=0, minute=0, second=0, microsecond=0)
        return start.timestamp(), (start + dt.timedelta(days=1)).timestamp()

    # ── ограничение перебора кодов
    def too_many(self, key, limit=10, window=600):
        now = time.time()
        with self.fail_lock:
            arr = [t for t in self.fails.get(key, []) if t > now - window]
            self.fails[key] = arr
            return len(arr) >= limit

    def add_fail(self, key):
        with self.fail_lock:
            self.fails.setdefault(key, []).append(time.time())

    # ── комнаты
    def rooms(self):
        return [dict(r) for r in self.db.q("SELECT id, name FROM rooms ORDER BY name COLLATE NOCASE")]

    def room_by_id(self, rid):
        r = self.db.one("SELECT id, name FROM rooms WHERE id=?", (rid,))
        return dict(r) if r else None

    def create_room(self, name):
        name = clean(name, LIMITS["room"])
        if not name:
            raise ValueError("Пустое название комнаты")
        with self.db.tx() as db:
            ex = [r for r in db.q("SELECT id, name FROM rooms") if r["name"].casefold() == name.casefold()]
            if ex:
                return dict(ex[0]), False
            cur = db.x("INSERT INTO rooms(name, created) VALUES(?,?)", (name, time.time()))
            return {"id": cur.lastrowid, "name": name}, True

    def rename_room(self, rid, new):
        new = clean(new, LIMITS["room"])
        if not new:
            raise ValueError("Пустое название комнаты")
        with self.db.tx() as db:
            r = db.one("SELECT name FROM rooms WHERE id=?", (rid,))
            if not r:
                raise ValueError("Комната не найдена")
            ex = [x for x in db.q("SELECT id, name FROM rooms WHERE id<>?", (rid,))
                  if x["name"].casefold() == new.casefold()]
            if ex:
                raise ValueError("Комната «%s» уже есть" % new)
            old = r["name"]
            db.x("UPDATE rooms SET name=? WHERE id=?", (new, rid))
            db.x("UPDATE users SET room=? WHERE room=?", (new, old))
            for it in db.q("SELECT id FROM items WHERE room=?", (old,)):
                db.x("UPDATE items SET room=?, rev=? WHERE id=?", (new, db.next_rev(), it["id"]))
            return old

    def delete_room(self, rid):
        with self.db.tx() as db:
            r = db.one("SELECT name FROM rooms WHERE id=?", (rid,))
            if not r:
                raise ValueError("Комната не найдена")
            n = db.one("SELECT COUNT(*) FROM users WHERE room=?", (r["name"],))[0]
            if n:
                raise ValueError("В комнате %d польз. — сначала переведите их в другую" % n)
            db.x("DELETE FROM rooms WHERE id=?", (rid,))
            return r["name"]

    # ── пользователи
    def user(self, uid):
        r = self.db.one("SELECT * FROM users WHERE id=?", (uid,))
        return dict(r) if r else None

    def users(self):
        return [dict(r) for r in self.db.q("SELECT * FROM users ORDER BY room COLLATE NOCASE, name COLLATE NOCASE")]

    def user_by_chat(self, chat):
        r = self.db.one("SELECT * FROM users WHERE tg_chat=? AND blocked=0", (chat,))
        return dict(r) if r else None

    def user_by_username(self, uname):
        r = self.db.one("SELECT * FROM users WHERE tg_username=?", (uname,)) if uname else None
        return dict(r) if r else None

    def check_username(self, raw, exclude=None):
        uname = norm_username(raw)
        if not uname:
            raise ValueError("Ник Telegram — латиница, цифры и _, от 4 символов (например @anna_smirnova)")
        ex = self.user_by_username(uname)
        if ex and ex["id"] != exclude:
            raise ValueError("Ник @%s уже занят: %s" % (uname, ex["name"]))
        return uname

    def create_user(self, name, room, role, username=None):
        name = clean(name, LIMITS["name"])
        if not name:
            raise ValueError("Пустое имя")
        if role not in ROLE_TITLE:
            raise ValueError("Роль: оператор или админ")
        uname = self.check_username(username) if username else None
        room_row, _ = self.create_room(room)
        code = new_code()
        with self.db.tx() as db:
            cur = db.x("INSERT INTO users(name, room, role, code_hash, created, tg_username) VALUES(?,?,?,?,?,?)",
                       (name, room_row["name"], role, sha(norm_code(code)), time.time(), uname))
            uid = cur.lastrowid
        return self.user(uid), code

    def supers_count(self, exclude=None):
        return self.db.one("SELECT COUNT(*) FROM users WHERE role='superadmin' AND blocked=0 AND id<>?",
                           (exclude or -1,))[0]

    def is_owner(self, u):
        return bool(u) and u.get("tg_chat") is not None and int(u["tg_chat"]) in self.owners

    def ensure_owner(self, chat, frm):
        """Владелец (по Telegram ID из config.json) — всегда супер-админ, создаётся при первом «Старт»."""
        if chat not in self.owners:
            return None
        r = self.db.one("SELECT * FROM users WHERE tg_chat=?", (chat,))
        if r:
            if r["role"] != "superadmin" or r["blocked"]:
                with self.db.tx() as db:
                    db.x("UPDATE users SET role='superadmin', blocked=0 WHERE id=?", (r["id"],))
            return self.user(r["id"])
        uname = norm_username(frm.get("username") or "")
        name = clean(" ".join(x for x in (frm.get("first_name"), frm.get("last_name")) if x), LIMITS["name"]) \
            or "Владелец"
        ex = self.user_by_username(uname) if uname else None
        if ex:
            uid = ex["id"]
        else:
            uid = self.create_user(name, "vn2", "superadmin", uname)[0]["id"]
        with self.db.tx() as db:
            db.x("UPDATE users SET tg_chat=NULL WHERE tg_chat=?", (chat,))
            db.x("UPDATE users SET tg_chat=?, tg_name=?, role='superadmin', blocked=0 WHERE id=?",
                 (chat, clean(name + ((" @" + uname) if uname else ""), 80), uid))
        return self.user(uid)

    def update_user(self, uid, **ch):
        u = self.user(uid)
        if not u:
            raise ValueError("Пользователь не найден")
        if "name" in ch:
            ch["name"] = clean(ch["name"], LIMITS["name"])
            if not ch["name"]:
                raise ValueError("Пустое имя")
        if "room" in ch:
            ch["room"] = self.create_room(ch["room"])[0]["name"]
        if "role" in ch and ch["role"] not in ROLE_TITLE:
            raise ValueError("Неизвестная роль")
        if "tg_username" in ch:
            ch["tg_username"] = self.check_username(ch["tg_username"], exclude=uid) if ch["tg_username"] else None
        demote = ("role" in ch and ch["role"] != "superadmin") or ch.get("blocked") == 1
        if demote and self.is_owner(u):
            raise ValueError("Это владелец — его нельзя понизить или отключить")
        if demote and u["role"] == "superadmin" and self.supers_count(exclude=uid) == 0:
            raise ValueError("Это последний супер-админ — так нельзя, иначе управлять будет некому")
        with self.db.tx() as db:
            for k, v in ch.items():
                db.x("UPDATE users SET %s=? WHERE id=?" % k, (v, uid))   # k — только из кода
            if ch.get("blocked") == 1:
                db.x("DELETE FROM devices WHERE user_id=?", (uid,))
            if "name" in ch:
                for it in db.q("SELECT id FROM items WHERE user_id=? AND status='active'", (uid,)):
                    db.x("UPDATE items SET operator=?, rev=? WHERE id=?", (ch["name"], db.next_rev(), it["id"]))
        return self.user(uid)

    def regen_code(self, uid):
        code = new_code()
        with self.db.tx() as db:
            db.x("UPDATE users SET code_hash=? WHERE id=?", (sha(norm_code(code)), uid))
        return code

    def delete_user(self, uid):
        u = self.user(uid)
        if not u:
            raise ValueError("Пользователь не найден")
        if self.is_owner(u):
            raise ValueError("Это владелец — удалить нельзя")
        if u["role"] == "superadmin" and self.supers_count(exclude=uid) == 0:
            raise ValueError("Это последний супер-админ — удалить нельзя")
        with self.db.tx() as db:
            db.x("DELETE FROM devices WHERE user_id=?", (uid,))
            db.x("DELETE FROM users WHERE id=?", (uid,))
        return u

    def link_tg(self, code, chat, tg_name, tg_username=None):
        h = sha(norm_code(code))
        with self.db.tx() as db:
            u = db.one("SELECT * FROM users WHERE code_hash=?", (h,))
            if not u:
                return None, "bad"
            if u["blocked"]:
                return None, "blocked"
            db.x("UPDATE users SET tg_chat=NULL WHERE tg_chat=?", (chat,))
            db.x("UPDATE users SET tg_chat=?, tg_name=? WHERE id=?", (chat, clean(tg_name, 80), u["id"]))
            if not u["tg_username"] and tg_username:
                if not db.one("SELECT 1 FROM users WHERE tg_username=?", (tg_username,)):
                    db.x("UPDATE users SET tg_username=? WHERE id=?", (tg_username, u["id"]))
        return self.user(u["id"]), "ok"

    def link_by_username(self, uname, chat, tg_name):
        """Человек нажал «Старт» в боте: если админ добавил его ник — привязываем чат."""
        u = self.user_by_username(uname)
        if not u:
            return None
        if u["blocked"]:
            return "blocked"
        with self.db.tx() as db:
            db.x("UPDATE users SET tg_chat=NULL WHERE tg_chat=? AND id<>?", (chat, u["id"]))
            db.x("UPDATE users SET tg_chat=?, tg_name=? WHERE id=?", (chat, clean(tg_name, 80), u["id"]))
        return self.user(u["id"])

    def last_seen(self, uid):
        r = self.db.one("SELECT MAX(last_seen) FROM devices WHERE user_id=?", (uid,))
        return r[0] if r and r[0] else None

    # ── устройства
    def bot_name(self):
        return ("@" + self.bot.username) if self.bot and self.bot.username else "бота «Перезвон»"

    def issue_token(self, u, app, pc, version):
        token = secrets.token_urlsafe(32)
        with self.db.tx() as db:
            db.x("INSERT INTO devices(token_hash,user_id,app,pc,version,last_seen,created) VALUES(?,?,?,?,?,?,?)",
                 (sha(token), u["id"], app, clean(pc, LIMITS["pc"]), clean(version, 20), time.time(), time.time()))
        return token

    def login(self, login, app, pc, version, ip):
        """Вход по нику Telegram (с подтверждением в боте) или по 12-значному коду."""
        login = (login or "").strip()
        if self.too_many("ip:" + ip):
            return 429, {"error": "Слишком много неудачных попыток. Подождите 10 минут"}
        is_code = bool(re.fullmatch(r"[\d\s-]+", login)) and len(norm_code(login)) == 12
        if is_code:
            u = self.db.one("SELECT * FROM users WHERE code_hash=?", (sha(norm_code(login)),))
            if not u:
                self.add_fail("ip:" + ip)
                return 403, {"error": "Неверный код"}
            u = dict(u)
        else:
            uname = norm_username(login)
            if not uname:
                return 400, {"error": "Введите ваш ник в Telegram, например @anna_smirnova"}
            u = self.user_by_username(uname)
            if not u:
                self.add_fail("ip:" + ip)
                return 403, {"error": "@%s нет в списке. Попросите администратора добавить вас в боте" % uname}
        if u["blocked"]:
            return 403, {"error": "Доступ отключён администратором"}
        if app == "admin" and u["role"] not in ADMIN_ROLES:
            return 403, {"error": "У вас роль «оператор» — админка недоступна. Войдите в программу «Перезвон»"}
        if app != "admin" and u["role"] in ADMIN_ROLES:
            return 403, {"error": "Вы администратор: перезвоны создают операторы. Для вас — программа «Перезвон Админ»"}
        if is_code or not self.confirm:
            return 200, {"token": self.issue_token(u, app, pc, version), "user": self.user_public(u)}
        # по нику — подтверждение одной кнопкой в Telegram
        if not u["tg_chat"]:
            return 403, {"error": "Сначала откройте %s в Telegram и нажмите «Старт», затем войдите снова"
                                  % self.bot_name(), "need_start": True,
                         "bot": self.bot.username if self.bot else None}
        if self.bot is None:
            return 503, {"error": "Telegram-бот сейчас недоступен — попросите у админа код входа"}
        key = "req:%d" % u["id"]
        if self.too_many(key, limit=6):
            return 429, {"error": "Слишком много запросов входа. Подождите 10 минут"}
        self.add_fail(key)
        rid = secrets.token_urlsafe(18)
        with self.fail_lock:
            now = time.time()
            for k in [k for k, v in self.pending.items() if v["created"] < now - 600]:
                del self.pending[k]
            self.pending[rid] = {"user_id": u["id"], "app": app, "pc": clean(pc, LIMITS["pc"]),
                                 "version": version, "created": now, "status": "pending", "token": None,
                                 "chat": u["tg_chat"]}
        threading.Thread(target=self.bot.ask_login, args=(rid, u, app, pc), daemon=True).start()
        return 202, {"pending": rid, "expires": 180, "bot": self.bot.username}

    def login_poll(self, rid):
        with self.fail_lock:
            p = self.pending.get(rid or "")
            if not p:
                return 404, {"error": "Запрос входа не найден — нажмите «Войти» ещё раз"}
            if p["status"] == "pending" and time.time() - p["created"] > 180:
                p["status"] = "expired"
            st = p["status"]
            if st in ("ok", "denied", "expired"):
                self.pending.pop(rid, None)
        if st == "pending":
            return 200, {"status": "pending"}
        if st == "denied":
            return 403, {"error": "Вход отклонён в Telegram"}
        if st == "expired":
            return 410, {"error": "Время на подтверждение вышло — нажмите «Войти» ещё раз"}
        return 200, {"status": "ok", "token": p["token"], "user": self.user_public(self.user(p["user_id"]))}

    def login_answer(self, rid, chat, yes):
        """Нажата кнопка в Telegram. Возвращает текст для сообщения."""
        with self.fail_lock:
            p = self.pending.get(rid)
            if not p or p["chat"] != chat:
                return "Запрос входа устарел"
            if p["status"] != "pending" or time.time() - p["created"] > 180:
                return "Запрос входа устарел — войдите в программе ещё раз"
            if not yes:
                p["status"] = "denied"
                return "⛔ Вход отклонён. Если это были не вы — сообщите администратору."
            p["status"] = "issuing"
        u = self.user(p["user_id"])
        if not u or u["blocked"]:
            with self.fail_lock:
                p["status"] = "denied"
            return "Доступ отключён"
        token = self.issue_token(u, p["app"], p["pc"], p["version"])
        with self.fail_lock:
            p["token"], p["status"] = token, "ok"
        return "✅ Вход подтверждён · %s" % esc(p["pc"] or "компьютер")

    def device(self, token):
        if not token:
            return None
        r = self.db.one("SELECT d.*, u.name, u.room, u.role, u.blocked FROM devices d JOIN users u ON u.id=d.user_id "
                        "WHERE d.token_hash=?", (sha(token),))
        if not r or r["blocked"]:
            return None
        if r["app"] != "admin" and r["role"] in ADMIN_ROLES:
            return None                    # стал админом — программа оператора больше не для него
        return dict(r)

    @staticmethod
    def user_public(u):
        return {"id": u["id"], "name": u["name"], "room": u["room"], "role": u["role"],
                "tg": bool(u.get("tg_chat")), "username": u.get("tg_username")}

    # ── перезвоны
    def item_out(self, r, skew=0.0):
        d = dict(r)
        out = {k: d.get(k) for k in ("id", "fio", "phone", "note", "status", "attempts", "snoozes",
                                     "operator", "room", "rev", "done_by", "user_id")}
        for k in ("created", "due", "done_at"):
            out[k] = (d[k] - skew) if d.get(k) is not None else None
        return out

    def sync(self, dev, body):
        now = time.time()
        epoch = self.db.meta("epoch")
        if body.get("epoch") and body.get("epoch") != epoch:
            return {"reset": True, "epoch": epoch, "server_now": now}
        cnow = fnum(body.get("now"), now)
        skew = now - cnow
        if abs(skew) < 2:
            skew = 0.0                     # задержку сети за сдвиг часов не считаем
        since = int(fnum(body.get("since"), 0) or 0)
        items = body.get("items") or []
        if not isinstance(items, list) or len(items) > 500:
            raise ValueError("items")
        accepted, rejected, conflicts = [], [], []
        uid = dev["user_id"]
        with self.db.tx() as db:
            db.x("UPDATE devices SET last_seen=?, skew=?, version=?, pc=? WHERE token_hash=?",
                 (now, skew, clean(body.get("version"), 20), clean(body.get("pc") or dev.get("pc"), LIMITS["pc"]),
                  dev["token_hash"]))
            for p in items:
                if not isinstance(p, dict):
                    continue
                iid = str(p.get("id") or "")
                if not re.fullmatch(r"[0-9a-f]{32}", iid):
                    continue
                status = p.get("status") if p.get("status") in STATUSES else "active"
                due = fnum(p.get("due"))
                if due is None:
                    continue
                due += skew
                created = fnum(p.get("created"), cnow) + skew
                done_at = fnum(p.get("done_at"))
                done_at = done_at + skew if done_at is not None else None
                if status == "done" and done_at is None:
                    done_at = now
                fields = dict(fio=clean(p.get("fio"), LIMITS["fio"]), phone=clean(p.get("phone"), LIMITS["phone"]),
                              note=clean(p.get("note"), LIMITS["note"]), due=due, status=status,
                              attempts=max(0, min(int(fnum(p.get("attempts"), 0)), 1000)),
                              snoozes=max(0, min(int(fnum(p.get("snoozes"), 0)), 1000)),
                              done_at=done_at if status == "done" else None, updated=now)
                row = db.one("SELECT * FROM items WHERE id=?", (iid,))
                base = int(fnum(p.get("base"), 0) or 0)
                if row is None:
                    rev = db.next_rev()
                    db.x("INSERT INTO items(id,user_id,operator,room,fio,phone,note,created,due,status,attempts,"
                         "snoozes,done_at,done_by,updated,rev) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                         (iid, uid, dev["name"], dev["room"], fields["fio"], fields["phone"], fields["note"],
                          created, due, status, fields["attempts"], fields["snoozes"], fields["done_at"],
                          "operator" if status == "done" else None, now, rev))
                    accepted.append(iid)
                elif row["user_id"] != uid:
                    rejected.append(iid)
                elif row["rev"] == base:
                    rev = db.next_rev()
                    reset = abs((row["due"] or 0) - due) > 1 or (row["status"] != "active" and status == "active")
                    done_by = row["done_by"]
                    if status == "done" and row["status"] != "done":
                        done_by = "operator"
                    db.x("UPDATE items SET fio=?,phone=?,note=?,due=?,status=?,attempts=?,snoozes=?,done_at=?,"
                         "done_by=?,updated=?,rev=?%s WHERE id=?" % (",tg_reminded=0,missed_notified=0" if reset else ""),
                         (fields["fio"], fields["phone"], fields["note"], due, status, fields["attempts"],
                          fields["snoozes"], fields["done_at"], done_by, now, rev, iid))
                    accepted.append(iid)
                else:
                    conflicts.append(iid)
            rev_now = int(db.meta("rev"))
            ids = accepted + conflicts
            rows = db.q("SELECT * FROM items WHERE user_id=? AND rev>?", (uid, since))
            seen = {r["id"] for r in rows}
            if ids:
                extra = [i for i in ids if i not in seen]
                if extra:
                    rows += db.q("SELECT * FROM items WHERE id IN (%s)" % ",".join("?" * len(extra)), extra)
            u = db.one("SELECT * FROM users WHERE id=?", (uid,))
        return {"epoch": epoch, "rev": rev_now, "server_now": now, "accepted": accepted,
                "rejected": rejected, "items": [self.item_out(r, skew) for r in rows],
                "user": self.user_public(dict(u)), "bot": self.bot.username if self.bot else None}

    def item(self, iid):
        r = self.db.one("SELECT * FROM items WHERE id=?", (iid,))
        return dict(r) if r else None

    def item_action(self, iid, action, by):
        """Действие админа или из Telegram: done / restore / delete / snooze15 / noanswer."""
        now = time.time()
        with self.db.tx() as db:
            r = db.one("SELECT * FROM items WHERE id=?", (iid,))
            if not r:
                raise ValueError("Перезвон не найден")
            rev = db.next_rev()
            if action == "done":
                if r["status"] == "done":
                    raise ValueError("Уже отмечено как выполненное")
                db.x("UPDATE items SET status='done', done_at=?, done_by=?, updated=?, rev=? WHERE id=?",
                     (now, by, now, rev, iid))
            elif action == "restore":
                db.x("UPDATE items SET status='active', done_at=NULL, done_by=NULL, updated=?, rev=?, "
                     "tg_reminded=0, missed_notified=0 WHERE id=?", (now, rev, iid))
            elif action == "delete":
                db.x("UPDATE items SET status='deleted', updated=?, rev=? WHERE id=?", (now, rev, iid))
            elif action in ("snooze15", "noanswer"):
                if r["status"] != "active":
                    raise ValueError("Перезвон уже закрыт")
                db.x("UPDATE items SET due=?, snoozes=snoozes+1, attempts=attempts+?, updated=?, rev=?, "
                     "tg_reminded=0, missed_notified=0 WHERE id=?",
                     (now + 15 * 60, 1 if action == "noanswer" else 0, now, rev, iid))
            else:
                raise ValueError("Неизвестное действие")
        return self.item(iid)

    def day_view(self, t_from, t_to, carry=True, viewer=None):
        now = time.time()
        rows = self.db.q("SELECT * FROM items WHERE (due>=? AND due<?) OR (? AND status='active' AND due<?) "
                         "ORDER BY due", (t_from, t_to, 1 if carry else 0, t_from))
        if viewer is not None:
            rows = [r for r in rows if sees_room(viewer, r["room"])]
        rooms = [r["name"] for r in self.rooms()]
        if viewer is not None and not is_super(viewer):
            rooms = [viewer["room"]]
        users = []
        for u in self.users():
            if viewer is not None and not sees_room(viewer, u["room"]):
                continue
            ls = self.last_seen(u["id"])
            users.append({"id": u["id"], "name": u["name"], "room": u["room"], "role": u["role"],
                          "blocked": bool(u["blocked"]), "tg": bool(u["tg_chat"]), "last_seen": ls,
                          "online": bool(ls and now - ls < ONLINE_SEC)})
        return {"server_now": now, "grace": self.grace, "items": [self.item_out(r) for r in rows],
                "users": users, "rooms": rooms, "bot": self.bot.username if self.bot else None,
                "can_act": viewer is None or is_super(viewer)}

    def day_stats(self, now=None, viewer=None):
        now = time.time() if now is None else now
        a, b = self.day_bounds(now)
        rows = self.db.q("SELECT * FROM items WHERE ((due>=? AND due<?) OR (status='active' AND due<?)) "
                         "AND status<>'deleted'", (a, b, a))
        if viewer is not None:
            rows = [r for r in rows if sees_room(viewer, r["room"])]
        per = {}
        for r in rows:
            st = per.setdefault(r["room"] or "—", {"upcoming": 0, "missed": 0, "due": 0, "done": 0})
            if r["status"] == "done":
                st["done"] += 1
            elif r["due"] < now - self.grace:
                st["missed"] += 1
            elif r["due"] <= now:
                st["due"] += 1
            else:
                st["upcoming"] += 1
        return per, [dict(r) for r in rows]

    def purge_old(self):
        now = time.time()
        with self.db.tx() as db:
            db.x("DELETE FROM items WHERE status='deleted' AND updated<?", (now - 90 * 86400,))
            db.x("DELETE FROM items WHERE status='done' AND updated<?", (now - 180 * 86400,))
            db.x("DELETE FROM devices WHERE last_seen<?", (now - 120 * 86400,))


# ═══════════════════════════════ HTTP ═══════════════════════════════

class Handler(BaseHTTPRequestHandler):
    app = None
    server_version = "Perezvon/" + VERSION
    sys_version = ""

    def log_message(self, fmt, *a):
        pass

    def ip(self):
        ip = self.client_address[0]
        if ip in ("127.0.0.1", "::1"):
            ip = self.headers.get("X-Real-IP") or ip
        return ip

    def send(self, code, obj):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def read_raw(self):
        """Тело читаем ВСЕГДА и сразу: если ответить (например 401), не дочитав, Windows рвёт соединение (RST)."""
        n = int(self.headers.get("Content-Length") or 0)
        if n > 2 * 1024 * 1024:
            self.close_connection = True
            self._raw = None
            return
        self._raw = self.rfile.read(n) if n else b""

    def body(self):
        raw = getattr(self, "_raw", b"")
        if raw is None:
            raise ValueError("too big")
        obj = json.loads((raw or b"{}").decode("utf-8"))
        if not isinstance(obj, dict):
            raise ValueError("not object")
        return obj

    def route(self):
        u = urlparse(self.path)
        path = u.path
        if path.startswith("/perezvon/"):
            path = path[len("/perezvon"):]
        return path, {k: v[-1] for k, v in parse_qs(u.query).items()}

    def auth(self):
        h = self.headers.get("Authorization") or ""
        return self.app.device(h[7:].strip() if h.startswith("Bearer ") else "")

    def do_GET(self):
        self.handle_any("GET")

    def do_POST(self):
        self.handle_any("POST")

    def handle_any(self, method):
        try:
            self.read_raw()
            path, qs = self.route()
            a = self.app
            if path == "/api/ping":
                return self.send(200, {"ok": True, "version": VERSION, "server_now": time.time()})
            if method == "POST" and path == "/api/login":
                b = self.body()
                code, obj = a.login(str(b.get("login") or b.get("code") or ""),
                                    "admin" if b.get("app") == "admin" else "client",
                                    b.get("pc"), b.get("version"), self.ip())
                return self.send(code, obj)
            if method == "POST" and path == "/api/login/poll":
                code, obj = a.login_poll(str(self.body().get("id") or ""))
                return self.send(code, obj)
            dev = self.auth()
            if dev is None:
                return self.send(401, {"error": "Нужно войти заново (код из Telegram-бота)"})
            if method == "POST" and path == "/api/logout":
                with a.db.tx() as db:
                    db.x("DELETE FROM devices WHERE token_hash=?", (dev["token_hash"],))
                return self.send(200, {"ok": True})
            if method == "POST" and path == "/api/sync":
                return self.send(200, a.sync(dev, self.body()))
            if path.startswith("/api/admin/"):
                if dev["role"] not in ADMIN_ROLES:
                    return self.send(403, {"error": "Нет прав администратора"})
                with a.db.tx() as db:
                    db.x("UPDATE devices SET last_seen=? WHERE token_hash=?", (time.time(), dev["token_hash"]))
                if method == "GET" and path == "/api/admin/day":
                    t_from = fnum(qs.get("from"))
                    t_to = fnum(qs.get("to"))
                    if t_from is None or t_to is None or not (0 < t_to - t_from <= 40 * 86400):
                        return self.send(400, {"error": "Неверный период"})
                    viewer = {"role": dev["role"], "room": dev["room"]}
                    return self.send(200, a.day_view(t_from, t_to, qs.get("carry", "1") == "1", viewer))
                if method == "POST" and path == "/api/admin/item":
                    if dev["role"] != "superadmin":
                        return self.send(403, {"error": "Админ только смотрит — менять перезвоны может супер-админ"})
                    b = self.body()
                    try:
                        it = a.item_action(str(b.get("id") or ""), str(b.get("action") or ""),
                                           "admin:" + dev["name"])
                    except ValueError as e:
                        return self.send(409, {"error": str(e)})
                    return self.send(200, {"item": a.item_out(it)})
            return self.send(404, {"error": "Не найдено"})
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
            return self.send(400, {"error": "Неверный запрос"})
        except Exception:
            log("HTTP error", traceback.format_exc())
            return self.send(500, {"error": "Внутренняя ошибка сервера"})


# ═══════════════════════════════ Telegram ═══════════════════════════════

def esc(s):
    return html.escape(str(s if s is not None else ""), quote=False)


def kb(rows):
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in rows]}


class Bot:
    def __init__(self, app, token, api_base=None):
        self.app = app
        self.base = (api_base or os.environ.get("PEREZVON_TG_API") or "https://api.telegram.org") + "/bot" + token + "/"
        self.state = {}               # chat_id → {"step": ..., ...}
        self.stop = threading.Event()
        self.username = None

    # ── транспорт
    def call(self, method, http_timeout=15, **params):
        data = json.dumps(params, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.base + method, data=data,
                                     headers={"Content-Type": "application/json; charset=utf-8"})
        try:
            with urllib.request.urlopen(req, timeout=http_timeout) as r:
                res = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                res = json.loads(e.read().decode("utf-8"))
            except Exception:
                res = {"ok": False, "description": str(e)}
        if not res.get("ok"):
            raise RuntimeError("%s: %s" % (method, res.get("description")))
        return res.get("result")

    def send(self, chat, text, markup=None):
        p = {"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True}
        if markup:
            p["reply_markup"] = markup
        try:
            return self.call("sendMessage", **p)
        except Exception as e:
            log("tg send", chat, e)
            return None

    def edit(self, chat, msg_id, text, markup=None):
        p = {"chat_id": chat, "message_id": msg_id, "text": text, "parse_mode": "HTML",
             "disable_web_page_preview": True}
        if markup:
            p["reply_markup"] = markup
        try:
            return self.call("editMessageText", **p)
        except Exception as e:
            if "not modified" not in str(e):
                log("tg edit", e)
                self.send(chat, text, markup)

    def answer(self, cb_id, text=None, alert=False):
        try:
            p = {"callback_query_id": cb_id}
            if text:
                p["text"] = text
                p["show_alert"] = alert
            self.call("answerCallbackQuery", **p)
        except Exception as e:
            log("tg answer", e)

    def run(self):
        try:
            me = self.call("getMe")
            self.username = me.get("username")
            log("бот @%s запущен" % self.username)
            self.call("setMyCommands", commands=[
                {"command": "menu", "description": "Главное меню"},
                {"command": "today", "description": "Перезвоны на сегодня"},
                {"command": "missed", "description": "Пропущенные"},
                {"command": "adduser", "description": "Создать пользователя: имя; комната; роль"},
                {"command": "help", "description": "Как пользоваться"},
                {"command": "cancel", "description": "Отменить ввод"}])
        except Exception as e:
            log("бот: не удалось запуститься:", e)
        offset = int(self.app.db.meta("tg_offset", "0"))
        backoff = 1
        while not self.stop.is_set():
            try:
                ups = self.call("getUpdates", http_timeout=40, offset=offset, timeout=25,
                                allowed_updates=["message", "callback_query"])
                backoff = 1
            except Exception as e:
                log("бот getUpdates:", e)
                self.stop.wait(backoff)
                backoff = min(backoff * 2, 60)
                continue
            for up in ups or []:
                offset = max(offset, up["update_id"] + 1)
                with self.app.db.lock:
                    self.app.db.set_meta("tg_offset", offset)
                try:
                    self.handle(up)
                except Exception:
                    log("бот: ошибка обработки", traceback.format_exc())

    # ── маршрутизация
    def handle(self, up):
        if "callback_query" in up:
            cq = up["callback_query"]
            chat = cq["message"]["chat"]["id"]
            return self.on_callback(chat, cq["message"]["message_id"], cq["id"], cq.get("data") or "",
                                    cq.get("from") or {})
        msg = up.get("message")
        if not msg or "text" not in msg:
            return
        chat = msg["chat"]["id"]
        if msg["chat"].get("type") != "private":
            return
        text = msg["text"].strip()
        frm = msg.get("from") or {}
        self.on_text(chat, text, frm)

    def tg_name(self, frm):
        n = " ".join(x for x in (frm.get("first_name"), frm.get("last_name")) if x)
        if frm.get("username"):
            n += " @" + frm["username"]
        return n.strip()

    def try_code(self, chat, text, frm):
        if self.app.too_many("tg:%s" % chat, limit=6):
            self.send(chat, "⛔ Слишком много неверных кодов. Попробуйте через 10 минут.")
            return True
        u, st = self.app.link_tg(text, chat, self.tg_name(frm), norm_username(frm.get("username") or ""))
        if st == "bad":
            self.app.add_fail("tg:%s" % chat)
            self.send(chat, "❌ Код не подошёл. Проверьте цифры — код выдаёт администратор.")
            return True
        if st == "blocked":
            self.send(chat, "⛔ Доступ отключён администратором.")
            return True
        self.welcome(chat, u)
        return True

    def welcome(self, chat, u):
        how = ("в программе «Перезвон» введите <code>@%s</code>." % esc(u["tg_username"])
               if u.get("tg_username") else "в программе введите код, который дал администратор.")
        self.send(chat, "✅ <b>%s</b>, вы подключены · комната %s · %s\n\nЧтобы войти: %s\n\n%s" % (
            esc(u["name"]), esc(u["room"]), ROLE_TITLE[u["role"]].lower(), how,
            {"operator": "Сюда будут приходить напоминания о перезвонах.",
             "admin": "Вы админ своей комнаты: здесь видно её перезвоны, сюда приходят её пропущенные.",
             "superadmin": "Вы супер-админ: здесь создаются пользователи и комнаты, сюда приходят пропущенные."}
            [u["role"]]))
        self.menu(chat, u)

    def try_username(self, chat, frm):
        """«Старт» от человека, которого админ добавил по нику — привязываем без кодов."""
        uname = norm_username(frm.get("username") or "")
        if not uname:
            return False
        r = self.app.link_by_username(uname, chat, self.tg_name(frm))
        if r == "blocked":
            self.send(chat, "⛔ Доступ отключён администратором.")
            return True
        if r:
            self.welcome(chat, r)
            return True
        return False

    def not_listed(self, chat, frm):
        uname = norm_username(frm.get("username") or "")
        if uname:
            t = ("👋 Это бот программы <b>«Перезвон»</b>.\n\nВас пока нет в списке. Ваш ник: <code>@%s</code> — "
                 "передайте его администратору. Когда добавит — нажмите /start ещё раз." % esc(uname))
        else:
            t = ("👋 Это бот программы <b>«Перезвон»</b>.\n\nУ вас в Telegram не задан ник (username). Задайте его: "
                 "Настройки → Имя пользователя, и передайте администратору. Или отправьте сюда код из 12 цифр, "
                 "если администратор его выдал.")
        self.send(chat, t)

    def on_text(self, chat, text, frm):
        if chat in self.app.owners and not self.app.user_by_chat(chat):
            o = self.app.ensure_owner(chat, frm)
            self.send(chat, "👑 <b>%s</b>, вы владелец «Перезвона» — супер-админ: создаёте пользователей, комнаты "
                            "и назначаете роли, в том числе других супер-админов." % esc(o["name"]))
            return self.menu(chat, o)
        if chat in self.app.owners:
            self.app.ensure_owner(chat, frm)
        u = self.app.user_by_chat(chat)
        low = text.lower()
        if low.startswith("/start"):
            arg = text[6:].strip()
            if arg and len(re.sub(r"\D", "", arg)) == 12:
                return self.try_code(chat, arg, frm)
            if not u:
                return self.try_username(chat, frm) or self.not_listed(chat, frm)
            return self.menu(chat, u)
        if not u:
            if len(re.sub(r"\D", "", text)) == 12:
                return self.try_code(chat, text, frm)
            return self.try_username(chat, frm) or self.not_listed(chat, frm)
        if low in ("/cancel", "отмена"):
            self.state.pop(chat, None)
            self.send(chat, "Отменено.")
            return self.menu(chat, u)
        if low.startswith("/menu"):
            self.state.pop(chat, None)
            return self.menu(chat, u)
        if low.startswith("/help"):
            return self.help(chat, u)
        if low.startswith("/today"):
            return self.today(chat, u)
        if low.startswith("/missed"):
            return self.missed(chat, u) if is_admin(u) else self.today(chat, u)
        if len(re.sub(r"\D", "", text)) == 12 and re.fullmatch(r"[\d\s-]+", text):
            return self.try_code(chat, text, frm)
        if not is_super(u):
            return self.menu(chat, u)
        if low.startswith("/adduser"):
            return self.adduser_line(chat, text[8:].strip())
        if low.startswith("/rooms"):
            return self.rooms(chat)
        if low.startswith("/users"):
            return self.users(chat)
        st = self.state.get(chat)
        if st:
            return self.on_step(chat, u, st, text)
        if norm_username(text) and text.strip().startswith("@"):
            x = self.app.user_by_username(norm_username(text))
            if x:
                return self.user_card(chat, x["id"])
            self.state[chat] = {"step": "nu_name", "uname": norm_username(text)}
            return self.send(chat, "@%s ещё нет. Создаём? Введите имя (например: <b>Анна Смирнова</b>)\n"
                                   "/cancel — отмена" % esc(norm_username(text)))
        return self.menu(chat, u)

    # ── экраны
    def menu(self, chat, u, msg_id=None):
        if is_admin(u):
            text = "%s<b>Перезвон · %s</b>\n%s · комната %s" % (
                ROLE_ICON[u["role"]], ROLE_TITLE[u["role"]].lower(), esc(u["name"]), esc(u["room"]))
            rows = [[("📋 Сегодня", "today"), ("⚠️ Пропущенные", "missed")]]
            if is_super(u):
                rows += [[("👤 Пользователи", "users"), ("🏠 Комнаты", "rooms")],
                         [("➕ Создать пользователя", "nu")]]
            rows.append([("🔔 Оповещения о пропущенных: %s" % ("вкл" if u["tg_notify"] else "выкл"), "notify")])
            mk = kb(rows)
        else:
            text = "📞 <b>Перезвон</b>\n%s · комната %s" % (esc(u["name"]), esc(u["room"]))
            mk = kb([[("📋 Мои перезвоны на сегодня", "today")],
                     [("🔔 Напоминания сюда: %s" % ("вкл" if u["tg_notify"] else "выкл"), "notify")]])
        if msg_id:
            return self.edit(chat, msg_id, text, mk)
        return self.send(chat, text, mk)

    def help(self, chat, u):
        if is_admin(u) and not is_super(u):
            t = ("Вы <b>админ</b> комнаты <b>%s</b>: видите её перезвоны (📋 Сегодня, ⚠️ Пропущенные), сюда "
                 "приходят её пропущенные. Подробно — в программе «Перезвон Админ» (вход по вашему нику).\n"
                 "Менять перезвоны и создавать пользователей может супер-админ." % esc(u["room"]))
        elif is_super(u):
            t = ("<b>Как создать пользователя</b>\n"
                 "• Кнопка «➕ Создать пользователя» — бот спросит ник в Telegram, имя, комнату и роль.\n"
                 "• Или одной строкой:\n<code>/adduser @anna_smirnova; Анна Смирнова; vn2; оператор</code>\n"
                 "<code>/adduser @oleg_kim; Олег Ким; vn2; админ</code>\n"
                 "• Или просто пришлите ник: <code>@anna_smirnova</code>\n"
                 "Комнату, которой ещё нет, бот создаст сам.\n\n"
                 "<b>Как человеку войти</b>\nВ программе «Перезвон» (админ — в «Перезвон Админ») ввести свой "
                 "ник — и всё. На любом компьютере — те же перезвоны.\n"
                 "Нажать «Старт» в боте нужно, только чтобы напоминания приходили и в Telegram.\n"
                 "Если ника в Telegram нет — в карточке пользователя есть «🔑 Код входа».\n\n"
                 "<b>Роли</b>\n👤 Оператор — видит только свои перезвоны.\n"
                 "🛡 Админ — только смотрит свою комнату и получает её пропущенные.\n"
                 "👑 Супер-админ — всё, как у вас: пользователи, комнаты, роли.")
        else:
            t = ("Перезвоны создаются в программе «Перезвон» на компьютере. Сюда приходят напоминания: "
                 "под ними можно нажать «Перезвонил» или «+15 мин» — программа это увидит.")
        self.send(chat, t)

    def users(self, chat, msg_id=None):
        rows = []
        for x in self.app.users():
            ls = self.app.last_seen(x["id"])
            dot = "⛔" if x["blocked"] else ("🟢" if ls and time.time() - ls < ONLINE_SEC else "⚪️")
            rows.append([("%s %s%s · %s%s" % (dot, ROLE_ICON.get(x["role"], ""), x["name"], x["room"],
                                                (" · @" + x["tg_username"]) if x["tg_username"] else ""),
                          "u:%d" % x["id"])])
        rows = rows[:90]
        rows.append([("➕ Создать пользователя", "nu"), ("« Меню", "menu")])
        text = "👤 <b>Пользователи</b> (%d)\n🟢 в программе сейчас · ⚪️ нет · 🛡 админ · 👑 супер-админ · " \
               "⛔ отключён" % (len(rows) - 1)
        return self.edit(chat, msg_id, text, kb(rows)) if msg_id else self.send(chat, text, kb(rows))

    def user_card(self, chat, uid, msg_id=None, note=""):
        x = self.app.user(uid)
        if not x:
            return self.users(chat, msg_id)
        ls = self.app.last_seen(uid)
        per, rows = self.app.day_stats()
        mine = [r for r in rows if r["user_id"] == uid]
        now = time.time()
        act = sum(1 for r in mine if r["status"] == "active" and r["due"] >= now - self.app.grace)
        mis = sum(1 for r in mine if r["status"] == "active" and r["due"] < now - self.app.grace)
        dn = sum(1 for r in mine if r["status"] == "done")
        t = ("%s<b>%s</b>\nНик: %s\nКомната: <b>%s</b>\nРоль: <b>%s</b>%s\nTelegram: %s\nВ программе: %s\n"
             "Сегодня: ждут %d · пропущено %d · выполнено %d") % (
            (note + "\n\n") if note else "", esc(x["name"]),
            ("<code>@%s</code>" % esc(x["tg_username"])) if x["tg_username"] else "не задан (вход по коду)",
            esc(x["room"]), ROLE_TITLE[x["role"]] + (" · владелец" if self.app.is_owner(x) else ""),
            " · ⛔ отключён" if x["blocked"] else "",
            ("✅ " + esc(x["tg_name"])) if x["tg_chat"] else "ещё не нажал(а) «Старт» в боте",
            ("был(а) " + self.app.hm(ls)) if ls else "ещё не входил(а)", act, mis, dn)
        rows = [[("🏠 Комната", "ur:%d" % uid), ("🎭 Роль", "uro:%d" % uid)],
                [("✏️ Имя", "un:%d" % uid), ("📛 Ник", "ug:%d" % uid), ("🔑 Код входа", "uc:%d" % uid)],
                [("✅ Включить" if x["blocked"] else "⛔ Отключить", "ub:%d" % uid)]]
        if x["tg_chat"]:
            rows[-1].append(("🔗 Отвязать TG", "ut:%d" % uid))
        rows.append([("🗑 Удалить", "ud:%d" % uid), ("« К списку", "users")])
        return self.edit(chat, msg_id, t, kb(rows)) if msg_id else self.send(chat, t, kb(rows))

    def rooms(self, chat, msg_id=None):
        users = self.app.users()
        per, _ = self.app.day_stats()
        rows = []
        for r in self.app.rooms():
            n = sum(1 for x in users if x["room"].lower() == r["name"].lower())
            s = per.get(r["name"], {})
            rows.append([("🏠 %s — %d польз. · сегодня ждут %d, пропущено %d" % (
                r["name"], n, s.get("upcoming", 0) + s.get("due", 0), s.get("missed", 0)), "r:%d" % r["id"])])
        rows.append([("➕ Новая комната", "nr"), ("« Меню", "menu")])
        text = "🏠 <b>Комнаты</b>"
        return self.edit(chat, msg_id, text, kb(rows)) if msg_id else self.send(chat, text, kb(rows))

    def room_card(self, chat, rid, msg_id=None, note=""):
        r = self.app.room_by_id(rid)
        if not r:
            return self.rooms(chat, msg_id)
        people = [x for x in self.app.users() if x["room"].lower() == r["name"].lower()]
        t = "%s🏠 <b>%s</b>\n\n%s" % ((note + "\n\n") if note else "", esc(r["name"]),
                                     "\n".join("• %s%s" % (ROLE_ICON.get(x["role"], ""), esc(x["name"]))
                                               for x in people) or "Пока никого.")
        rows = [[("✏️ Переименовать", "rn:%d" % rid), ("🗑 Удалить", "rd:%d" % rid)],
                [("« Комнаты", "rooms")]]
        return self.edit(chat, msg_id, t, kb(rows)) if msg_id else self.send(chat, t, kb(rows))

    def fmt_item(self, r, now, show_op=True):
        late = now - r["due"]
        if r["status"] == "done":
            icon, when = "✅", "в %s" % self.app.hm(r["done_at"] or r["due"], now)
        elif late > self.app.grace:
            icon, when = "🔴", "%s, опоздание %d мин" % (self.app.hm(r["due"], now), late // 60)
        elif late >= 0:
            icon, when = "🟠", "%s — пора" % self.app.hm(r["due"], now)
        else:
            icon, when = "🕑", self.app.hm(r["due"], now)
        s = "%s %s — <b>%s</b> <code>%s</code>" % (icon, when, esc(r["fio"] or "без имени"),
                                                   esc(phone_copy(r["phone"])))
        if r.get("note"):
            s += "\n      📝 " + esc(r["note"])
        if show_op:
            s += "\n      👤 %s · %s" % (esc(r["operator"]), esc(r["room"]))
        return s

    def today(self, chat, u, msg_id=None):
        now = time.time()
        per, rows = self.app.day_stats(now, viewer=u if is_admin(u) else None)
        date = self.app.local(now).strftime("%d.%m")
        if is_admin(u):
            lines = ["📋 <b>Сегодня, %s</b>%s" % (date, "" if is_super(u) else " · комната " + esc(u["room"])), ""]
            tot = {"upcoming": 0, "missed": 0, "due": 0, "done": 0}
            for room in sorted(per, key=str.lower):
                s = per[room]
                for k in tot:
                    tot[k] += s[k]
                lines.append("🏠 <b>%s</b>: предстоит %d · пора %d · пропущено %d · выполнено %d" % (
                    esc(room), s["upcoming"], s["due"], s["missed"], s["done"]))
            if not per:
                lines.append("Сегодня перезвонов нет.")
            else:
                lines += ["", "<b>Всего</b>: предстоит %d · пора %d · пропущено %d · выполнено %d" % (
                    tot["upcoming"], tot["due"], tot["missed"], tot["done"])]
            soon = [r for r in rows if r["status"] == "active" and r["due"] >= now - self.app.grace]
            soon.sort(key=lambda r: r["due"])
            if soon:
                lines += ["", "<b>Ближайшие</b>:"] + [self.fmt_item(r, now) for r in soon[:15]]
            mk = kb([[("⚠️ Пропущенные", "missed"), ("🔄 Обновить", "today")], [("« Меню", "menu")]])
        else:
            mine = [r for r in rows if r["user_id"] == u["id"]]
            mine.sort(key=lambda r: (r["status"] == "done", r["due"]))
            lines = ["📋 <b>Мои перезвоны, %s</b>" % date, ""]
            lines += [self.fmt_item(r, now, show_op=False) for r in mine[:40]] or ["Сегодня перезвонов нет."]
            mk = kb([[("🔄 Обновить", "today"), ("« Меню", "menu")]])
        text = "\n".join(lines)[:4000]
        return self.edit(chat, msg_id, text, mk) if msg_id else self.send(chat, text, mk)

    def missed(self, chat, u, msg_id=None):
        now = time.time()
        _, rows = self.app.day_stats(now, viewer=u)
        ms = [r for r in rows if r["status"] == "active" and r["due"] < now - self.app.grace]
        ms.sort(key=lambda r: r["due"])
        lines = ["⚠️ <b>Пропущенные</b> (%d)" % len(ms), ""]
        lines += [self.fmt_item(r, now) for r in ms[:30]] or ["Пропущенных нет 👍"]
        text = "\n".join(lines)[:4000]
        mk = kb([[("📋 Сегодня", "today"), ("🔄 Обновить", "missed")], [("« Меню", "menu")]])
        return self.edit(chat, msg_id, text, mk) if msg_id else self.send(chat, text, mk)

    def created_text(self, x, code):
        app = "«Перезвон Админ»" if is_admin(x) else "«Перезвон»"
        bot = ("@" + self.username) if self.username else "этого бота"
        head = "✅ <b>Пользователь создан</b>\n\nИмя: <b>%s</b>\nКомната: <b>%s</b>\nРоль: <b>%s</b>\n" % (
            esc(x["name"]), esc(x["room"]), ROLE_TITLE[x["role"]])
        if x["tg_username"]:
            if self.app.confirm:
                steps = ("1. Открыть %s в Telegram и нажать «Старт» (один раз).\n2. Запустить %s, ввести "
                         "<code>@%s</code> и нажать «Да, это я» в Telegram.") % (bot, app, esc(x["tg_username"]))
            else:
                steps = ("Запустить %s и ввести <code>@%s</code> — всё.\nЧтобы напоминания приходили ещё и в "
                         "Telegram — открыть %s и нажать «Старт».") % (app, esc(x["tg_username"]), bot)
            return head + "Ник: <code>@%s</code>\n\nЧто сделать человеку:\n%s\n\nНа любом компьютере у него " \
                          "будут его перезвоны." % (esc(x["tg_username"]), steps)
        return head + ("\n🔑 Код входа: <code>%s</code>\n\nЧто сделать человеку:\n1. Запустить %s и ввести "
                       "этот код.\n2. По желанию отправить этот же код в %s — тогда напоминания придут и в "
                       "Telegram.") % (code, app, bot)

    def adduser_line(self, chat, arg):
        parts = [p.strip() for p in re.split(r"[;\n]", arg) if p.strip()]
        if len(parts) == 1 and "," in arg:
            parts = [p.strip() for p in arg.split(",") if p.strip()]
        uname = None
        if parts and parts[0].startswith("@"):
            uname = parts.pop(0)
        if len(parts) != 3:
            return self.send(chat, "Формат: <code>/adduser @ник; Имя Фамилия; комната; роль</code>\n"
                                   "Например: <code>/adduser @anna_smirnova; Анна Смирнова; vn2; оператор</code>\n"
                                   "Или нажмите «➕ Создать пользователя» в меню.")
        role = parse_role(parts[2])
        if not role:
            return self.send(chat, "Роль не понял: «%s». Напишите <b>оператор</b> или <b>админ</b>." % esc(parts[2]))
        try:
            x, code = self.app.create_user(parts[0], parts[1], role, uname)
        except ValueError as e:
            return self.send(chat, "❌ " + esc(e))
        self.send(chat, self.created_text(x, code), kb([[("👤 Открыть карточку", "u:%d" % x["id"]),
                                                         ("« Меню", "menu")]]))

    def room_picker(self, prefix, extra_new=True):
        rows = []
        line = []
        for r in self.app.rooms():
            line.append((r["name"], "%s:%d" % (prefix, r["id"])))
            if len(line) == 3:
                rows.append(line)
                line = []
        if line:
            rows.append(line)
        if extra_new:
            rows.append([("➕ Новая комната", prefix + ":new")])
        rows.append([("✖️ Отмена", "menu")])
        return kb(rows)

    def on_step(self, chat, u, st, text):
        step = st["step"]
        if step == "nu_tg":
            if text.strip() in ("-", "—", "нет"):
                st.update(step="nu_name", uname=None)
            else:
                try:
                    st.update(step="nu_name", uname=self.app.check_username(text))
                except ValueError as e:
                    return self.send(chat, "❌ %s\nВведите ник ещё раз или «-», если ника нет." % esc(e))
            return self.send(chat, "Имя%s? (например: <b>Анна Смирнова</b>)" % (
                (" для @" + esc(st["uname"])) if st.get("uname") else ""))
        if step == "ug_tg":
            try:
                self.app.update_user(st["uid"], tg_username=None if text.strip() in ("-", "—") else text)
            except ValueError as e:
                return self.send(chat, "❌ %s\nВведите ник ещё раз (или /cancel)." % esc(e))
            self.state.pop(chat, None)
            return self.user_card(chat, st["uid"], note="✅ Ник изменён")
        if step == "nu_name":
            name = clean(text, LIMITS["name"])
            if not name or name.startswith("/"):
                return self.send(chat, "Введите имя текстом (или /cancel).")
            st.update(step="nu_room", name=name)
            return self.send(chat, "Комната для <b>%s</b>?" % esc(name), self.room_picker("nur"))
        if step == "nu_newroom":
            try:
                room, _ = self.app.create_room(text)
            except ValueError as e:
                return self.send(chat, "❌ %s. Введите название ещё раз." % esc(e))
            st.update(step="nu_role", room=room["name"])
            return self.ask_role(chat, st)
        if step == "nr_name":
            try:
                room, created = self.app.create_room(text)
            except ValueError as e:
                return self.send(chat, "❌ %s. Введите название ещё раз." % esc(e))
            self.state.pop(chat, None)
            return self.room_card(chat, room["id"], note="✅ Комната создана" if created else "Такая комната уже есть")
        if step == "rn_name":
            try:
                old = self.app.rename_room(st["rid"], text)
            except ValueError as e:
                return self.send(chat, "❌ %s. Введите другое название." % esc(e))
            self.state.pop(chat, None)
            return self.room_card(chat, st["rid"], note="✅ «%s» переименована" % esc(old))
        if step == "un_name":
            try:
                self.app.update_user(st["uid"], name=text)
            except ValueError as e:
                return self.send(chat, "❌ " + esc(e))
            self.state.pop(chat, None)
            return self.user_card(chat, st["uid"], note="✅ Имя изменено")
        if step == "ur_newroom":
            try:
                self.app.update_user(st["uid"], room=text)
            except ValueError as e:
                return self.send(chat, "❌ " + esc(e))
            self.state.pop(chat, None)
            return self.user_card(chat, st["uid"], note="✅ Комната изменена")
        self.state.pop(chat, None)
        return self.menu(chat, u)

    def ask_role(self, chat, st, msg_id=None):
        t = "Роль для <b>%s</b> (комната %s)?\n\n%s" % (esc(st["name"]), esc(st["room"]), ROLES_HELP)
        mk = kb([[("👤 Оператор", "nuro:operator"), ("🛡 Админ", "nuro:admin")],
                 [("👑 Супер-админ", "nuro:superadmin")], [("✖️ Отмена", "menu")]])
        return self.edit(chat, msg_id, t, mk) if msg_id else self.send(chat, t, mk)

    def ask_login(self, rid, u, app, pc):
        t = ("🔐 <b>Вход в «%s»</b>\n\nКомпьютер: <b>%s</b>\nПользователь: %s\n\nЭто вы? Если нет — нажмите «Нет»."
             % ("Перезвон Админ" if app == "admin" else "Перезвон", esc(pc or "неизвестно"), esc(u["name"])))
        self.send(u["tg_chat"], t, kb([[("✅ Да, это я", "lg:y:" + rid), ("⛔ Нет", "lg:n:" + rid)]]))

    def on_callback(self, chat, mid, cid, data, frm):
        m = re.fullmatch(r"lg:([yn]):([\w-]{10,40})", data)
        if m:
            text = self.app.login_answer(m.group(2), chat, m.group(1) == "y")
            self.answer(cid, re.sub(r"<[^>]+>", "", text)[:150])
            return self.edit(chat, mid, text)
        u = self.app.user_by_chat(chat)
        if not u:
            return self.answer(cid, "Сначала нажмите /start", True)
        # ── кнопки под напоминаниями (и оператор, и админ)
        m = re.fullmatch(r"it:([dsn]):([0-9a-f]{32})", data)
        if m:
            act = {"d": "done", "s": "snooze15", "n": "noanswer"}[m.group(1)]
            it = self.app.item(m.group(2))
            if not it or (it["user_id"] != u["id"] and not is_super(u)):
                return self.answer(cid, "Перезвон не найден", True)
            try:
                it = self.app.item_action(it["id"], act, "tg:" + u["name"])
            except ValueError as e:
                self.answer(cid, str(e), True)
                return self.edit(chat, mid, self.reminder_text(self.app.item(m.group(2))))
            self.answer(cid, {"done": "Отмечено ✅", "snooze15": "Напомню через 15 минут",
                              "noanswer": "Ок, ещё раз через 15 минут"}[act])
            return self.edit(chat, mid, self.reminder_text(it))
        if data == "menu":
            self.state.pop(chat, None)
            self.answer(cid)
            return self.menu(chat, u, mid)
        if data == "today":
            self.answer(cid)
            return self.today(chat, u, mid)
        if data == "notify":
            self.app.update_user(u["id"], tg_notify=0 if u["tg_notify"] else 1)
            self.answer(cid, "Оповещения " + ("выключены" if u["tg_notify"] else "включены"))
            return self.menu(chat, self.app.user(u["id"]), mid)
        if not is_admin(u):
            return self.answer(cid, "Нужны права администратора", True)
        if data != "missed" and not is_super(u):
            return self.answer(cid, "Это может только супер-админ", True)
        self.answer(cid)
        try:
            return self.admin_callback(chat, mid, data, u)
        except ValueError as e:
            return self.send(chat, "❌ " + esc(e))

    def admin_callback(self, chat, mid, data, u):
        a = self.app
        if data == "missed":
            return self.missed(chat, u, mid)
        if data == "users":
            self.state.pop(chat, None)
            return self.users(chat, mid)
        if data == "rooms":
            self.state.pop(chat, None)
            return self.rooms(chat, mid)
        if data == "nu":
            self.state[chat] = {"step": "nu_tg"}
            return self.send(chat, "Ник нового пользователя в Telegram? (например: <code>@anna_smirnova</code>)\n"
                                   "По нику он будет входить в программу.\nЕсли ника нет — отправьте «-».\n"
                                   "/cancel — отмена")
        if data == "nr":
            self.state[chat] = {"step": "nr_name"}
            return self.send(chat, "Название новой комнаты? (например: <b>vn3</b>)\n/cancel — отмена")
        m = re.fullmatch(r"([a-z]+):(\w+)(?::(\w+))?", data)
        if not m:
            return
        cmd, arg, arg2 = m.group(1), m.group(2), m.group(3)
        st = self.state.get(chat) or {}
        if cmd == "nur":                      # выбор комнаты при создании
            if not st.get("name"):
                return self.menu(chat, u, mid)
            if arg == "new":
                st["step"] = "nu_newroom"
                return self.send(chat, "Название новой комнаты?")
            room = a.room_by_id(int(arg))
            if not room:
                return
            st.update(step="nu_role", room=room["name"])
            return self.ask_role(chat, st, mid)
        if cmd == "nuro":
            if not st.get("name") or not st.get("room") or arg not in ROLE_TITLE:
                return self.menu(chat, u, mid)
            x, code = a.create_user(st["name"], st["room"], arg, st.get("uname"))
            self.state.pop(chat, None)
            return self.edit(chat, mid, self.created_text(x, code),
                             kb([[("👤 Открыть карточку", "u:%d" % x["id"]), ("➕ Ещё одного", "nu")],
                                 [("« Меню", "menu")]]))
        if cmd == "u":
            return self.user_card(chat, int(arg), mid)
        if cmd == "r":
            return self.room_card(chat, int(arg), mid)
        if cmd == "rn":
            self.state[chat] = {"step": "rn_name", "rid": int(arg)}
            return self.send(chat, "Новое название комнаты?\n/cancel — отмена")
        if cmd == "rd":
            r = a.room_by_id(int(arg))
            if r and arg2 != "y":
                return self.edit(chat, mid, "Удалить комнату <b>%s</b>?" % esc(r["name"]),
                                 kb([[("🗑 Да, удалить", "rd:%s:y" % arg), ("« Назад", "r:%s" % arg)]]))
            a.delete_room(int(arg))
            return self.rooms(chat, mid)
        uid = int(arg) if arg.isdigit() else None
        x = a.user(uid) if uid else None
        if not x:
            return self.users(chat, mid)
        if cmd == "ur":                       # сменить комнату
            if arg2 == "new":
                self.state[chat] = {"step": "ur_newroom", "uid": uid}
                return self.send(chat, "Название новой комнаты для %s?" % esc(x["name"]))
            if arg2:
                room = a.room_by_id(int(arg2))
                a.update_user(uid, room=room["name"])
                return self.user_card(chat, uid, mid, "✅ Комната: " + esc(room["name"]))
            rows = []
            for r in a.rooms():
                rows.append([(("• " if r["name"] == x["room"] else "") + r["name"], "ur:%d:%d" % (uid, r["id"]))])
            rows.append([("➕ Новая комната", "ur:%d:new" % uid), ("« Назад", "u:%d" % uid)])
            return self.edit(chat, mid, "Комната для <b>%s</b>?" % esc(x["name"]), kb(rows))
        if cmd == "uro":
            if arg2 in ROLE_TITLE:
                a.update_user(uid, role=arg2)
                return self.user_card(chat, uid, mid, "✅ Роль: " + ROLE_TITLE[arg2])
            return self.edit(chat, mid, "Роль для <b>%s</b>?\n\n%s" % (esc(x["name"]), ROLES_HELP),
                             kb([[("👤 Оператор", "uro:%d:operator" % uid), ("🛡 Админ", "uro:%d:admin" % uid)],
                                 [("👑 Супер-админ", "uro:%d:superadmin" % uid)], [("« Назад", "u:%d" % uid)]]))
        if cmd == "ug":
            self.state[chat] = {"step": "ug_tg", "uid": uid}
            return self.send(chat, "Новый ник Telegram для <b>%s</b>? (например <code>@anna</code>; «-» — убрать)\n"
                                   "/cancel — отмена" % esc(x["name"]))
        if cmd == "un":
            self.state[chat] = {"step": "un_name", "uid": uid}
            return self.send(chat, "Новое имя для <b>%s</b>?\n/cancel — отмена" % esc(x["name"]))
        if cmd == "uc":
            if arg2 != "y":
                return self.edit(chat, mid, "Выдать <b>%s</b> код входа (12 цифр)? Нужен, если у человека нет "
                                            "ника в Telegram. Прежний код перестанет подходить (уже вошедшие "
                                            "программы продолжат работать)." % esc(x["name"]),
                                 kb([[("🔑 Да, выдать код", "uc:%d:y" % uid), ("« Назад", "u:%d" % uid)]]))
            code = a.regen_code(uid)
            return self.edit(chat, mid, "🔑 Код входа для <b>%s</b>: <code>%s</code>\nЕго вводят в программе вместо "
                                        "ника — без подтверждения в Telegram. Передавайте лично." % (esc(x["name"]), code),
                             kb([[("« К карточке", "u:%d" % uid)]]))
        if cmd == "ub":
            nb = 0 if x["blocked"] else 1
            a.update_user(uid, blocked=nb)
            return self.user_card(chat, uid, mid, "⛔ Отключён: программы разлогинены" if nb else "✅ Включён")
        if cmd == "ut":
            a.update_user(uid, tg_chat=None, tg_name=None)
            return self.user_card(chat, uid, mid, "🔗 Telegram отвязан")
        if cmd == "ud":
            if arg2 != "y":
                return self.edit(chat, mid, "Удалить <b>%s</b>? Его перезвоны останутся в истории." % esc(x["name"]),
                                 kb([[("🗑 Да, удалить", "ud:%d:y" % uid), ("« Назад", "u:%d" % uid)]]))
            a.delete_user(uid)
            return self.users(chat, mid)

    # ── оповещения
    def reminder_text(self, it):
        now = time.time()
        if it["status"] == "done":
            head = "✅ <b>Перезвонили</b> · %s" % self.app.hm(it["done_at"] or now, now)
        elif it["status"] == "deleted":
            head = "🗑 <b>Удалено</b>"
        elif it["due"] > now:
            head = "⏰ <b>Отложено до %s</b>" % self.app.hm(it["due"], now)
        else:
            head = "🔔 <b>Пора перезвонить</b>"
        s = "%s\n\n👤 <b>%s</b>\n📞 <code>%s</code>" % (head, esc(it["fio"] or "без имени"),
                                                       esc(phone_copy(it["phone"])))
        if it.get("note"):
            s += "\n📝 " + esc(it["note"])
        s += "\n🕑 Обещали в %s · звонок был в %s" % (self.app.hm(it["due"], now), self.app.hm(it["created"], now))
        if it.get("attempts"):
            s += "\n📵 Не дозвонились: %d раз" % it["attempts"]
        return s

    def notify_due(self, it, chat):
        self.send(chat, self.reminder_text(it), kb([[("✅ Перезвонил", "it:d:" + it["id"])],
                                                     [("⏰ +15 мин", "it:s:" + it["id"]),
                                                      ("📵 Не дозвонился", "it:n:" + it["id"])]]))

    def notify_missed(self, it, chat):
        now = time.time()
        self.send(chat, "⚠️ <b>Пропущен перезвон</b>\n\n👤 Оператор: <b>%s</b> · %s\n🙍 Клиент: <b>%s</b>\n"
                        "📞 <code>%s</code>%s\n🕑 Должны были в %s (опоздание %d мин)" % (
                            esc(it["operator"]), esc(it["room"]), esc(it["fio"] or "без имени"),
                            esc(phone_copy(it["phone"])), ("\n📝 " + esc(it["note"])) if it.get("note") else "",
                            self.app.hm(it["due"], now), (now - it["due"]) // 60))


ROLES_HELP = ("👤 Оператор — создаёт перезвоны, видит только свои.\n"
              "🛡 Админ — только смотрит перезвоны СВОЕЙ комнаты и получает её пропущенные.\n"
              "👑 Супер-админ — может всё: все комнаты, пользователи, роли.")


def parse_role(s):
    s = (s or "").strip().lower().replace("ё", "е")
    if s in ("супер", "суперадмин", "супер-админ", "супер админ", "superadmin", "super", "sa", "главный"):
        return "superadmin"
    if s in ("оператор", "опер", "юзер", "пользователь", "user", "operator", "o"):
        return "operator"
    if s in ("админ", "администратор", "admin", "a", "адм"):
        return "admin"
    return None


def notifier_loop(app, stop, period=15):
    last_purge = 0
    while not stop.is_set():
        try:
            notify_once(app)
            if time.time() - last_purge > 86400:
                app.purge_old()
                last_purge = time.time()
        except Exception:
            log("оповещения:", traceback.format_exc())
        stop.wait(period)


def notify_once(app):
    """Один проход: напоминания операторам (в момент «пора») и админам о пропущенных."""
    now = time.time()
    with app.db.lock:
        due = [dict(r) for r in app.db.q("SELECT * FROM items WHERE status='active' AND tg_reminded=0 AND due<=?",
                                         (now,))]
        missed = [dict(r) for r in app.db.q("SELECT * FROM items WHERE status='active' AND missed_notified=0 "
                                            "AND due<=?", (now - app.grace,))]
        for it in due:
            app.db.x("UPDATE items SET tg_reminded=1 WHERE id=?", (it["id"],))
        for it in missed:
            app.db.x("UPDATE items SET missed_notified=1 WHERE id=?", (it["id"],))
        admins = [dict(r) for r in app.db.q("SELECT * FROM users WHERE role IN ('admin','superadmin') AND blocked=0 "
                                            "AND tg_chat IS NOT NULL AND tg_notify=1")]
    sent = 0
    if app.bot is None:
        return 0
    for it in due:
        if now - it["due"] > 6 * 3600:
            continue                       # очень старые (сервер лежал) — не спамим
        u = app.user(it["user_id"])
        if u and u["tg_chat"] and u["tg_notify"] and not u["blocked"]:
            app.bot.notify_due(it, u["tg_chat"])
            sent += 1
    for it in missed:
        if now - it["due"] > 6 * 3600:
            continue
        for adm in admins:
            if sees_room(adm, it["room"]):
                app.bot.notify_missed(it, adm["tg_chat"])
                sent += 1
    return sent


# ═══════════════════════════════ запуск ═══════════════════════════════

def main(argv=None):
    ap = argparse.ArgumentParser(description="Сервер «Перезвона»")
    ap.add_argument("--data", default=os.environ.get("PEREZVON_DATA", "/var/lib/perezvon"))
    ap.add_argument("--create-admin", metavar="ИМЯ")
    ap.add_argument("--room", default="vn2")
    ap.add_argument("--tg", metavar="НИК", help="ник Telegram для --create-admin")
    ap.add_argument("--list-users", action="store_true")
    args = ap.parse_args(argv)
    app = App(args.data)
    if args.create_admin:
        u, code = app.create_user(args.create_admin, args.room, "superadmin", args.tg)
        print("Админ создан: %s (комната %s)%s\nКод (отправить боту или ввести в программе): %s" % (
            u["name"], u["room"], (", ник @" + u["tg_username"]) if u["tg_username"] else "", code))
        return 0
    if args.list_users:
        for u in app.users():
            print(u["id"], u["name"], "@%s" % u["tg_username"], u["room"], u["role"], "TG" if u["tg_chat"] else "-",
                  "ОТКЛ" if u["blocked"] else "")
        return 0
    stop = threading.Event()
    token = app.cfg.get("bot_token") or os.environ.get("PEREZVON_BOT_TOKEN")
    if token:
        app.bot = Bot(app, token)
        app.bot.stop = stop
        threading.Thread(target=app.bot.run, daemon=True, name="bot").start()
    else:
        log("bot_token не задан — бот выключен")
    threading.Thread(target=notifier_loop, args=(app, stop), daemon=True, name="notifier").start()
    Handler.app = app
    bind, port = app.cfg.get("bind", "127.0.0.1"), int(app.cfg.get("port", 8767))
    srv = ThreadingHTTPServer((bind, port), Handler)
    srv.daemon_threads = True
    log("Перезвон-сервер %s слушает %s:%d, данные в %s" % (VERSION, bind, port, args.data))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
    return 0


if __name__ == "__main__":
    sys.exit(main())
