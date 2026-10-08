# -*- coding: utf-8 -*-
"""Общая логика «Перезвона» без tkinter: время, номера, хранилище, сеть, синхронизация.

Всё, что здесь, покрыто автотестами (tests/), поэтому GUI-модули только вызывают эти функции.
"""
import datetime as dt
import json
import os
import re
import shutil
import ssl
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

APP_NAME = "Перезвон"
APP_VERSION = "1.0.1"
ADMIN_ROLES = ("admin", "superadmin")
DEFAULT_SERVER = "https://89.124.122.158/perezvon"

LIMITS = {"fio": 200, "phone": 64, "note": 500}
MAX_AHEAD = 7 * 24 * 3600          # дальше недели вперёд напоминать не даём
MISSED_GRACE = 10 * 60             # «пропущен» = просрочен больше чем на 10 минут


# ───────────────────────────── время ─────────────────────────────

class WhenError(ValueError):
    pass


_NO_NUM = r"(?<![\d.])(?<![\d.]\s)"      # слово-число, только если перед ним нет цифры («1 час» ≠ «1 1ч»)
_WORDS = [
    (_NO_NUM + r"\bполтора\s+часа\b", "90м"), (_NO_NUM + r"\bполтора\b", "90м"),
    (_NO_NUM + r"\bпол\s*часа\b", "30м"), (_NO_NUM + r"\bчетверть\s+часа\b", "15м"),
    (_NO_NUM + r"\bпару\s+минут\b", "2м"),
    (_NO_NUM + r"\bчас\b", "1ч"), (_NO_NUM + r"\bминут[уку]*\b", "1м"),
]
_UNITS = {
    3600: ("ч", "час", "часа", "часов", "h", "hr", "hrs", "hour", "hours"),
    60: ("м", "мин", "минут", "минуты", "минута", "минуту", "минутки", "m", "min", "mins"),
    1: ("с", "сек", "секунд", "секунды", "секунду", "s", "sec"),
    86400: ("д", "дн", "день", "дня", "дней", "d", "day", "days"),
}

_EXAMPLES = "Примеры: 15 · 1ч 30м · 14:30 · завтра в 10"


def _unit_seconds(unit):
    u = unit.strip(".")
    if u == "":
        return 60          # без единиц — минуты: «15», и «1ч 30» = 1 ч 30 мин
    for k, names in _UNITS.items():
        if u in names:
            return k
    return None


def parse_when(text, now=None):
    """Разбирает «через сколько» / «во сколько».

    Возвращает (due: datetime, kind), kind = "in" (через N) или "at" (к часам).
    Бросает WhenError с понятным русским текстом.
    """
    now = now or dt.datetime.now()
    s = (text or "").strip().lower().replace("ё", "е").replace(",", ".")
    s = re.sub(r"\s+", " ", s)
    if not s:
        raise WhenError("Укажите, через сколько перезвонить")
    s = re.sub(r"^через\s*", "", s).strip()

    # ── к определённому времени: «14:30», «в 14», «в 9.15», «завтра в 10»
    tomorrow = False
    m_tom = re.match(r"^завтра\s*", s)
    if m_tom:
        tomorrow = True
        s = s[m_tom.end():]
    m = (re.fullmatch(r"в\s*(\d{1,2})(?:[:.](\d{2}))?(?:\s*ч(?:ас\w*)?)?", s)
         or re.fullmatch(r"(\d{1,2}):(\d{2})", s)
         or (tomorrow and re.fullmatch(r"(\d{1,2})(?:[:.](\d{2}))?", s)))
    if m:
        h, mi = int(m.group(1)), int(m.group(2) or 0)
        if h > 23 or mi > 59:
            raise WhenError("Такого времени нет: %02d:%02d" % (h, mi))
        due = now.replace(hour=h, minute=mi, second=0, microsecond=0)
        if tomorrow:
            due += dt.timedelta(days=1)
        elif due <= now:
            due += dt.timedelta(days=1)
        return due, "at"
    if tomorrow:
        raise WhenError("Во сколько завтра? " + _EXAMPLES)

    # ── через сколько: «15», «1.5ч», «1ч 30м», «полчаса», «90 мин»
    for pat, rep in _WORDS:
        s = re.sub(pat, rep, s)
    s = s.replace(" ", "")
    if not re.fullmatch(r"(?:\d+(?:\.\d+)?[a-zа-я.]*)+", s):
        raise WhenError("Не понял «%s». %s" % (text.strip(), _EXAMPLES))
    total = 0.0
    for num, unit in re.findall(r"(\d+(?:\.\d+)?)([a-zа-я.]*)", s):
        k = _unit_seconds(unit)
        if k is None:
            raise WhenError("Не понял единицу «%s». %s" % (unit, _EXAMPLES))
        total += float(num) * k
    total = round(total)
    if total <= 0:
        raise WhenError("Время должно быть больше нуля")
    if total > MAX_AHEAD:
        raise WhenError("Слишком далеко: не больше 7 дней")
    return now + dt.timedelta(seconds=total), "in"


def fmt_duration(sec):
    sec = int(round(sec))
    if sec < 60:
        return "%d с" % max(sec, 0)
    m = sec // 60
    if m < 60:
        return "%d мин" % m
    h, m = divmod(m, 60)
    if h < 24:
        return "%d ч" % h if m == 0 else "%d ч %02d мин" % (h, m)
    d, h = divmod(h, 24)
    return "%d дн" % d if h == 0 else "%d дн %d ч" % (d, h)


def fmt_clock(ts, now_ts=None):
    """«14:45», «завтра 09:00», «вчера 18:10», «12.10 09:00»."""
    now_ts = time.time() if now_ts is None else now_ts
    t = dt.datetime.fromtimestamp(ts)
    today = dt.datetime.fromtimestamp(now_ts).date()
    delta = (t.date() - today).days
    hm = t.strftime("%H:%M")
    if delta == 0:
        return hm
    if delta == 1:
        return "завтра " + hm
    if delta == -1:
        return "вчера " + hm
    return t.strftime("%d.%m ") + hm


def fmt_at(ts, ref_ts=None):
    """«в 13:17», «вчера в 13:17», «завтра в 9:00», «06.10 в 13:17» — относительно дня ref_ts."""
    ref_ts = time.time() if ref_ts is None else ref_ts
    t = dt.datetime.fromtimestamp(ts)
    delta = (t.date() - dt.datetime.fromtimestamp(ref_ts).date()).days
    hm = t.strftime("%H:%M")
    if delta == 0:
        return "в " + hm
    if delta == -1:
        return "вчера в " + hm
    if delta == 1:
        return "завтра в " + hm
    return t.strftime("%d.%m в ") + hm


def fmt_left(due_ts, now_ts=None):
    """Для списков: «через 4:59», «через 1 ч 05 мин», «просрочено 3 мин»."""
    now_ts = time.time() if now_ts is None else now_ts
    left = due_ts - now_ts
    if left >= 0:
        if left < 3600:
            s = int(left)
            return "через %d:%02d" % (s // 60, s % 60)
        return "через " + fmt_duration(left)
    late = -left
    if late < 60:
        return "пора звонить"
    return "просрочено " + fmt_duration(late)


def describe_due(due_ts, now_ts=None):
    """Строка-подтверждение под формой: «Напомню сегодня в 14:45 — через 15 мин»."""
    now_ts = time.time() if now_ts is None else now_ts
    clock = fmt_clock(due_ts, now_ts)
    day = "сегодня в " if ":" in clock and " " not in clock else ""
    if clock.startswith("завтра "):
        clock = "завтра в " + clock[7:]
    return "Напомню %s%s — через %s" % (day, clock, fmt_duration(max(due_ts - now_ts, 0)))


def urgency(due_ts, now_ts=None, status="active"):
    """'done' | 'deleted' | 'missed' | 'due' | 'soon' | 'later'."""
    if status in ("done", "deleted"):
        return status
    now_ts = time.time() if now_ts is None else now_ts
    left = due_ts - now_ts
    if left < -MISSED_GRACE:
        return "missed"
    if left <= 0:
        return "due"
    if left <= 5 * 60:
        return "soon"
    return "later"


# ───────────────────────────── номера ─────────────────────────────

def phone_digits(s):
    return re.sub(r"\D", "", s or "")


def _ru10(s):
    d = phone_digits(s)
    if len(d) == 11 and d[0] in "78":
        return d[1:]
    if len(d) == 10 and d[0] == "9" and not (s or "").strip().startswith("+"):
        return d
    return None


def phone_display(s):
    ru = _ru10(s)
    if ru:
        return "+7 (%s) %s-%s-%s" % (ru[:3], ru[3:6], ru[6:8], ru[8:])
    return re.sub(r"\s+", " ", (s or "").strip())


def phone_for_copy(s, fmt="plus7"):
    """fmt: plus7 → +79991234567, 8 → 89991234567, raw → как ввели."""
    raw = (s or "").strip()
    if fmt == "raw":
        return raw
    ru = _ru10(s)
    if ru:
        return ("8" if fmt == "8" else "+7") + ru
    d = phone_digits(raw)
    if not d:
        return raw
    return ("+" if raw.startswith("+") else "") + d


def phone_valid(s):
    return len(phone_digits(s)) >= 3


def looks_like_phone(s):
    """Для подсказки «вставить из буфера»: в буфере только номер и ничего лишнего."""
    s = (s or "").strip()
    if not s or len(s) > 30 or "\n" in s:
        return False
    if not re.fullmatch(r"[+\d\s()\-.]+", s):
        return False
    return 5 <= len(phone_digits(s)) <= 15


def clean_text(s, limit):
    s = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", str(s or ""))
    s = re.sub(r"\s+", " ", s).strip()
    return s[:limit]


_RU2EN = dict(zip("йцукенгшщзхъфывапролджэячсмитьбюё", "qwertyuiop[]asdfghjkl;'zxcvbnm,.`"))


def fix_layout(s):
    """Ник, набранный в русской раскладке, → латиница по тем же клавишам: «фттф» → «anna», «"» → «@»."""
    out = []
    for ch in s or "":
        low = ch.lower()
        if low in _RU2EN:
            out.append(_RU2EN[low])
        elif ch == '"':
            out.append("@")
        else:
            out.append(ch)
    return "".join(out)


def is_code(s):
    return bool(re.fullmatch(r"[\d\s-]+", (s or "").strip())) and len(phone_digits(s)) == 12


def login_flow(api, login, app, pc_name, cancel, on_wait=None, poll_every=1.5, max_wait=200):
    """Вход: по коду — сразу токен; по нику — ждём «Да, это я» в Telegram. Бросает ApiError."""
    res = api.call("POST", "/api/login", {"login": login, "app": app, "pc": pc_name, "version": APP_VERSION})
    if res.get("token"):
        return res
    rid = res.get("pending")
    if not rid:
        raise ApiError(502, "Сервер ответил непонятно")
    if on_wait:
        on_wait(res)
    deadline = time.time() + max_wait
    while time.time() < deadline:
        if cancel.wait(poll_every):
            raise ApiError(-1, "Отменено")
        try:
            r = api.call("POST", "/api/login/poll", {"id": rid})
        except ApiError as e:
            if e.status == 0:
                continue                     # сеть мигнула — ждём дальше
            raise
        if r.get("status") == "ok" and r.get("token"):
            return r
    raise ApiError(410, "Время на подтверждение вышло — нажмите «Войти» ещё раз")


# ───────────────────────────── хранилище ─────────────────────────────

def app_dir(name="Perezvon"):
    base = os.environ.get("PEREZVON_HOME")
    if not base:
        base = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), name)
    os.makedirs(base, exist_ok=True)
    return base


def load_json(path, default):
    """Читает JSON; битый файл откладывает в .corrupt-*, берёт резервную копию .bak."""
    for p in (path, path + ".bak"):
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(default, dict) and not isinstance(data, dict):
                raise ValueError("не тот тип")
            return data
        except FileNotFoundError:
            continue
        except (ValueError, OSError, UnicodeDecodeError):
            try:
                os.replace(p, "%s.corrupt-%d" % (p, int(time.time())))
            except OSError:
                pass
            continue
    return default


def save_json(path, data):
    """Атомарная запись: tmp → fsync → replace; предыдущая версия остаётся в .bak."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
        f.flush()
        os.fsync(f.fileno())
    if os.path.exists(path):
        try:
            shutil.copy2(path, path + ".bak")
        except OSError:
            pass
    for attempt in range(5):           # антивирус/индексатор иногда держит файл
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.1)


# ───────────────────────────── сеть ─────────────────────────────

class ApiError(Exception):
    def __init__(self, status, message, data=None):
        super().__init__(message)
        self.status = status
        self.message = message
        self.data = data or {}


class Api:
    def __init__(self, base_url, token=None, timeout=10):
        self.base = (base_url or DEFAULT_SERVER).rstrip("/")
        self.token = token
        self.timeout = timeout
        self.ctx = ssl.create_default_context()

    def call(self, method, path, body=None, params=None):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data = None
        headers = {"Accept": "application/json", "User-Agent": "Perezvon/" + APP_VERSION}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout,
                                        context=self.ctx if url.startswith("https") else None) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            msg = "Ошибка сервера %d" % e.code
            data = {}
            try:
                data = json.loads(e.read().decode("utf-8"))
                msg = data.get("error") or msg
            except Exception:
                pass
            raise ApiError(e.code, msg, data)
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError):
            raise ApiError(0, "Нет связи с сервером")
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError:
            raise ApiError(502, "Сервер ответил непонятно")



# ───────────────────────────── локальные перезвоны оператора ─────────────────────────────

ITEM_FIELDS = ("id", "fio", "phone", "note", "created", "due", "status",
               "attempts", "snoozes", "done_at", "operator", "room")


class Store:
    """Список перезвонов на этом ПК + синхронизация с сервером.

    Правила синхронизации (проверены тестами):
      * каждое локальное изменение → lver+1, dirty=True;
      * на сервер уходит base = последняя виденная серверная ревизия; сервер примет,
        только если у него та же ревизия (иначе изменение сделал админ/бот — побеждает сервер);
      * если пока шёл запрос пользователь снова изменил запись — она остаётся dirty;
      * у сервера своя «эпоха»: если база на сервере пересоздана, всё отправляется заново.
    """

    KEEP_DONE = 180 * 86400            # для вкладки «История» (на сервере столько же)

    def __init__(self, path):
        self.path = path
        d = load_json(path, {})
        self.items = {}
        for it in d.get("items", []):
            if isinstance(it, dict) and it.get("id"):
                self.items[it["id"]] = it
        self.since = int(d.get("since", 0) or 0)
        self.epoch = d.get("epoch")
        self.owner = d.get("owner")          # id пользователя, чей это список (None — ещё не входили)

    def save(self):
        save_json(self.path, {"items": list(self.items.values()), "since": self.since,
                              "epoch": self.epoch, "owner": self.owner})

    def adopt(self, user_id):
        """Список на ПК принадлежит user_id. Вошёл другой человек — чужие перезвоны с ПК убираем,
        его собственные придут с сервера. Возвращает True, если список поменялся."""
        if user_id is None or self.owner == user_id:
            return False
        if self.owner is None:
            # версия без владельца: всё, что уже приходило с сервера, могло быть чужим — скачаем заново;
            # созданное на этом ПК и ещё не отправленное (srv_rev=0) — оставляем, уйдёт новому владельцу
            self.items = {k: i for k, i in self.items.items() if not i.get("srv_rev")}
        else:
            self.items = {}
        self.since = 0
        self.owner = user_id
        self.save()
        return True

    def clear(self):
        """Выход из аккаунта: перезвоны остаются на сервере, с этого ПК убираются."""
        self.items = {}
        self.since = 0
        self.epoch = None
        self.owner = None
        self.save()

    # ── изменения от пользователя
    def add(self, fio, phone, note, due, now=None, operator="", room=""):
        now = time.time() if now is None else now
        it = {"id": uuid.uuid4().hex, "fio": clean_text(fio, LIMITS["fio"]),
              "phone": clean_text(phone, LIMITS["phone"]), "note": clean_text(note, LIMITS["note"]),
              "created": now, "due": float(due), "status": "active", "attempts": 0,
              "snoozes": 0, "done_at": None, "operator": operator, "room": room,
              "lver": 1, "srv_rev": 0, "dirty": True}
        self.items[it["id"]] = it
        self.save()
        return it

    def update(self, item_id, **changes):
        it = self.items.get(item_id)
        if not it:
            return None
        for k, v in changes.items():
            if k in ("fio", "phone", "note"):
                v = clean_text(v, LIMITS[k])
            it[k] = v
        it["lver"] = it.get("lver", 0) + 1
        it["dirty"] = True
        self.save()
        return it

    def done(self, item_id, now=None):
        return self.update(item_id, status="done", done_at=time.time() if now is None else now)

    def snooze(self, item_id, seconds, now=None, attempt=False):
        it = self.items.get(item_id)
        if not it:
            return None
        now = time.time() if now is None else now
        ch = {"due": now + seconds, "status": "active", "snoozes": it.get("snoozes", 0) + 1}
        if attempt:
            ch["attempts"] = it.get("attempts", 0) + 1
        return self.update(item_id, **ch)

    def delete(self, item_id):
        return self.update(item_id, status="deleted")

    def restore(self, item_id):
        return self.update(item_id, status="active", done_at=None)

    # ── выборки
    def active(self):
        return sorted((i for i in self.items.values() if i.get("status") == "active"),
                      key=lambda i: i["due"])

    def done_today(self, now=None):
        now = time.time() if now is None else now
        day = dt.datetime.fromtimestamp(now).date()
        res = [i for i in self.items.values() if i.get("status") == "done" and i.get("done_at")
               and dt.datetime.fromtimestamp(i["done_at"]).date() == day]
        return sorted(res, key=lambda i: -i["done_at"])

    def due_items(self, now=None):
        now = time.time() if now is None else now
        return [i for i in self.active() if i["due"] <= now]

    def dirty_count(self):
        return sum(1 for i in self.items.values() if i.get("dirty"))

    def purge(self, now=None):
        """Убирает отправленные удалённые и старые выполненные (на сервере они остаются)."""
        now = time.time() if now is None else now
        drop = [k for k, i in self.items.items() if not i.get("dirty") and (
            i.get("status") == "deleted" or
            (i.get("status") == "done" and (i.get("done_at") or 0) < now - self.KEEP_DONE))]
        for k in drop:
            del self.items[k]
        return len(drop)

    # ── синхронизация
    def build_push(self, now=None):
        pushed = {}
        out = []
        for it in self.items.values():
            if not it.get("dirty"):
                continue
            pushed[it["id"]] = it.get("lver", 0)
            p = {k: it.get(k) for k in ITEM_FIELDS}
            p["base"] = it.get("srv_rev", 0)
            out.append(p)
            if len(out) >= 300:
                break
        body = {"now": time.time() if now is None else now, "since": self.since,
                "epoch": self.epoch, "items": out}
        return body, pushed

    def apply_sync(self, pushed, resp):
        """Применяет ответ /api/sync. Возвращает список id, изменённых «снаружи» (админ/бот)."""
        if resp.get("reset"):
            # на сервере другая база — отправляем всё заново, начиная с нуля
            self.epoch = resp.get("epoch")
            self.since = 0
            for it in self.items.values():
                it["srv_rev"] = 0
                it["dirty"] = True
            self.save()
            return []
        accepted = set(resp.get("accepted", []))
        external = []
        for srv in resp.get("items", []):
            iid = srv.get("id")
            if not iid:
                continue
            loc = self.items.get(iid)
            if iid in accepted and loc is not None:
                loc["srv_rev"] = srv["rev"]
                if loc.get("lver") == pushed.get(iid):
                    loc["dirty"] = False
                    for k in ("operator", "room"):        # их проставляет сервер
                        if srv.get(k) is not None:
                            loc[k] = srv[k]
                continue
            if loc is not None and srv.get("rev", 0) <= loc.get("srv_rev", 0):
                continue                                   # устаревший ответ
            new = {k: srv.get(k) for k in ITEM_FIELDS}
            new["lver"] = (loc or {}).get("lver", 0) + 1
            new["srv_rev"] = srv.get("rev", 0)
            new["dirty"] = False
            if loc is None and new.get("status") in ("deleted",):
                continue
            self.items[iid] = new
            external.append(iid)
        self.since = max(self.since, int(resp.get("rev", self.since)))
        if resp.get("epoch"):
            self.epoch = resp["epoch"]
        self.purge()
        self.save()
        return external
