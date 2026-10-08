# -*- coding: utf-8 -*-
"""«Перезвон Админ» — все перезвоны за день по комнатам: предстоящие, пора звонить, пропущенные, выполненные.

Пользователи и комнаты создаются в Telegram-боте; здесь — наблюдение и действия с перезвонами.
"""
import datetime as dt
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk

import pz_common as pc
import pz_ui as ui
from pz_login import LoginForm
from pz_ui import C, I, px, font, ifont

REFRESH = 4
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
          "ноября", "декабря"]
WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
STATUS_TITLE = {"later": "Предстоит", "soon": "Скоро", "due": "Пора звонить", "missed": "Пропущен",
                "done": "Выполнен", "deleted": "Удалён"}
FILTERS = [("upcoming", "Предстоит", "accent"), ("due", "Пора звонить", "amber"),
           ("missed", "Пропущено", "red"), ("done", "Выполнено", "green")]


def bucket(it, t, grace):
    if it["status"] == "done":
        return "done"
    if it["status"] == "deleted":
        return "deleted"
    if it["due"] < t - grace:
        return "missed"
    if it["due"] <= t:
        return "due"
    return "upcoming"


class Settings:
    def __init__(self, path, session):
        self.path = path
        self.d = {"server": pc.DEFAULT_SERVER, "copy_fmt": "plus7"}
        old = pc.load_json(path, {})
        self.d.update({k: v for k, v in old.items() if k in self.d})
        self.session = session
        if old.get("token_dp") or old.get("user"):     # 1.0.0 держал вход в settings.json — переносим
            if not session.get("token"):
                session.set(token=ui.secret_unpack(old.get("token_dp")), user=old.get("user"))
            self.save()

    @property
    def token(self):
        return self.session.get("token")

    @token.setter
    def token(self, v):
        self.session.set(token=v)

    @property
    def user(self):
        return self.session.get("user") or {}

    @user.setter
    def user(self, v):
        self.session.set(user=v)

    def save(self):
        pc.save_json(self.path, self.d)


class AdminApp:
    def __init__(self, home=None):
        self.home = home or pc.app_dir("PerezvonAdmin")
        self.s = Settings(os.path.join(self.home, "settings.json"),
                          ui.Session(ui.session_dir("PerezvonAdmin", home)))
        self.root = tk.Tk()
        if os.environ.get("PEREZVON_SELFTEST"):
            self.root.withdraw()
        self.root.title("Перезвон · Админ")
        self.root.configure(bg=C["bg"])
        ui.init_scale(self.root)
        ui.apply_ttk_theme(self.root)
        ui.install_keyboard(self.root)
        ui.set_icon(self.root)
        self.root.minsize(px(1040), px(620))
        l, t, r, b = ui.work_area(self.root.winfo_screenwidth() // 2, self.root.winfo_screenheight() // 2)
        W, H = min(px(1360), r - l - px(40)), min(px(820), b - t - px(40))
        self.root.geometry("%dx%d+%d+%d" % (W, H, l + (r - l - W) // 2, t + (b - t - H) // 2))
        ui.dark_titlebar(self.root)
        self.q = queue.Queue()
        self.busy = False
        self.offset = 0.0              # серверное время − локальное
        self.data = None
        self.day = dt.date.today()
        self.room = None               # None = все комнаты
        self.operator = None
        self.filter = None             # None = все кроме удалённых
        self.show_deleted = False
        self.last_ok = None
        self.error = None
        self.next_at = 0
        self.frame = None
        self.root.report_callback_exception = self.on_error
        if self.s.token:
            self.build_main()
        else:
            self.build_login()
        self.loop()

    def on_error(self, exc, val, tb):
        import traceback
        try:
            with open(os.path.join(self.home, "errors.log"), "a", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + "".join(traceback.format_exception(exc, val, tb)) + "\n")
        except OSError:
            pass

    def clear(self):
        if self.frame:
            self.frame.destroy()
        self.frame = tk.Frame(self.root, bg=C["bg"])
        self.frame.pack(fill="both", expand=True)
        return self.frame

    # ═══════════════ вход ═══════════════
    def build_login(self, reason=None):
        f = self.clear()
        box = tk.Frame(f, bg=C["surface"])
        box.place(relx=0.5, rely=0.42, anchor="center")
        b = tk.Frame(box, bg=C["surface"])
        b.pack(padx=px(36), pady=px(32))
        top = tk.Frame(b, bg=C["surface"])
        top.pack(fill="x")
        tk.Label(top, text=I["people"], font=ifont(20), fg=C["white"], bg=C["accent"], width=2, pady=px(6)).pack(side="left")
        tt = tk.Frame(top, bg=C["surface"])
        tt.pack(side="left", padx=px(12))
        tk.Label(tt, text="Перезвон · Админ", font=font(16, True), fg=C["text"], bg=C["surface"]).pack(anchor="w")
        tk.Label(tt, text="все перезвоны всех комнат за день", font=font(9), fg=C["muted"], bg=C["surface"]).pack(anchor="w")
        if reason:
            tk.Label(b, text=reason, font=font(9, True), fg=C["amber"], bg=C["surface"]).pack(anchor="w", pady=(px(14), 0))
        self.form = LoginForm(b, "admin", self.server_from_field, self.logged_in)
        self.form.pack(fill="x", pady=(px(18), 0))
        adv = tk.Frame(b, bg=C["surface"])
        adv.pack(fill="x", pady=(px(14), 0))
        tk.Label(adv, text="Сервер:", font=font(8), fg=C["faint"], bg=C["surface"]).pack(side="left")
        self.srv = tk.Entry(adv, font=font(8), bg=C["input"], fg=C["muted"], relief="flat", insertbackground=C["text"])
        self.srv.insert(0, self.s.d["server"])
        self.srv.pack(side="left", fill="x", expand=True, padx=(px(6), 0), ipady=px(3))
        self.root.after(50, self.form.focus)

    def server_from_field(self):
        self.s.d["server"] = self.srv.get().strip().rstrip("/") or pc.DEFAULT_SERVER
        return self.s.d["server"]

    def logged_in(self, res):
        if res.get("user", {}).get("role") not in pc.ADMIN_ROLES:
            self.form.msg.configure(text="Это не администратор", fg=C["red"])
            return
        self.s.user = res["user"]
        self.s.token = res["token"]
        self.build_main()

    def logout(self):
        tok = self.s.token
        if tok:
            api = pc.Api(self.s.d["server"], tok, timeout=4)
            threading.Thread(target=lambda: _safe(lambda: api.call("POST", "/api/logout", {})), daemon=True).start()
        self.s.session.clear()
        self.data = None
        self.build_login()

    # ═══════════════ главный экран ═══════════════
    def build_main(self):
        f = self.clear()
        self.root.bind("<F5>", lambda e: self.refresh_now())
        self.root.bind("<Control-KeyPress>", self.ctrl_key, add="+")
        # верхняя полоса
        top = tk.Frame(f, bg=C["surface"])
        top.pack(fill="x")
        tk.Label(top, text=I["phone"], font=ifont(14), fg=C["white"], bg=C["accent"], width=2, pady=px(5)).pack(
            side="left", padx=(px(16), px(10)), pady=px(10))
        tk.Label(top, text="Перезвон", font=font(13, True), fg=C["text"], bg=C["surface"]).pack(side="left")
        tk.Label(top, text="  админ", font=font(10), fg=C["muted"], bg=C["surface"]).pack(side="left")
        nav = tk.Frame(top, bg=C["surface"])
        nav.pack(side="left", padx=px(30))
        ui.IconBtn(nav, "", lambda: self.shift_day(-1), tooltip="Предыдущий день (Ctrl+←)").pack(side="left")
        self.day_lbl = tk.Label(nav, text="", font=font(11, True), fg=C["text"], bg=C["surface"], width=26)
        self.day_lbl.pack(side="left")
        ui.IconBtn(nav, "", lambda: self.shift_day(1), tooltip="Следующий день (Ctrl+→)").pack(side="left")
        self.today_btn = ui.Btn(nav, "Сегодня", command=lambda: self.set_day(dt.date.today()), bg=C["chip"],
                                size=9, padx=10, pady=4)
        self.today_btn.pack(side="left", padx=(px(8), 0))
        right = tk.Frame(top, bg=C["surface"])
        right.pack(side="right", padx=px(12))
        ui.Btn(right, "Выйти", icon="signout", command=self.logout, bg=C["chip"], size=9, padx=10, pady=4,
               bold=False, tooltip="Выйти из аккаунта").pack(side="right", padx=(px(6), 0))
        ui.IconBtn(right, "refresh", self.refresh_now, tooltip="Обновить (F5)").pack(side="right")
        u = self.s.user
        tk.Label(right, text=u.get("name", ""), font=font(9), fg=C["muted"], bg=C["surface"]).pack(side="right", padx=px(8))
        self.search = ui.Field(right, "", "Поиск: ФИО, номер, заметка, оператор", icon="search", width=34)
        self.search.pack(side="right", padx=px(10))
        self.search.winfo_children()[0].pack_forget()       # без подписи над полем
        self.search.box.pack_configure(pady=0)
        self.search.var.trace_add("write", lambda *_: self.render())
        tk.Frame(f, bg=C["border"], height=1).pack(fill="x")

        body = tk.Frame(f, bg=C["bg"])
        body.pack(fill="both", expand=True)
        # боковая панель
        side = tk.Frame(body, bg=C["surface"], width=px(270))
        side.pack(side="left", fill="y")
        side.pack_propagate(False)
        tk.Label(side, text="КОМНАТЫ", font=font(8, True), fg=C["faint"], bg=C["surface"]).pack(
            anchor="w", padx=px(18), pady=(px(16), px(6)))
        self.rooms_box = tk.Frame(side, bg=C["surface"])
        self.rooms_box.pack(fill="x")
        tk.Label(side, text="ОПЕРАТОРЫ", font=font(8, True), fg=C["faint"], bg=C["surface"]).pack(
            anchor="w", padx=px(18), pady=(px(18), px(6)))
        self.ops_area = ui.ScrollArea(side)
        self.ops_area.pack(fill="both", expand=True)
        self.bot_hint = tk.Label(side, text="", font=font(8), fg=C["faint"], bg=C["surface"], justify="left",
                                 wraplength=px(236))
        self.bot_hint.pack(side="bottom", anchor="w", padx=px(18), pady=px(12))
        tk.Frame(body, bg=C["border"], width=1).pack(side="left", fill="y")

        main = tk.Frame(body, bg=C["bg"])
        main.pack(side="left", fill="both", expand=True, padx=px(20), pady=px(16))
        # карточки-счётчики
        cards = tk.Frame(main, bg=C["bg"])
        cards.pack(fill="x")
        self.stat = {}
        for i, (key, title, col) in enumerate(FILTERS):
            c = tk.Frame(cards, bg=C["surface"], cursor="hand2", highlightthickness=2,
                         highlightbackground=C["surface"], highlightcolor=C["surface"])
            c.grid(row=0, column=i, sticky="nsew", padx=(0 if i == 0 else px(12), 0))
            cards.columnconfigure(i, weight=1, uniform="c")
            stripe = tk.Frame(c, bg=C[col], height=px(3))
            stripe.pack(fill="x")
            n = tk.Label(c, text="0", font=font(24, True), fg=C[col], bg=C["surface"])
            n.pack(anchor="w", padx=px(16), pady=(px(8), 0))
            t = tk.Label(c, text=title, font=font(10), fg=C["muted"], bg=C["surface"])
            t.pack(anchor="w", padx=px(16), pady=(0, px(12)))
            for w in (c, stripe, n, t):
                w.bind("<Button-1>", lambda e, k=key: self.set_filter(k))
            self.stat[key] = (c, n)
        # строка фильтра
        fl = tk.Frame(main, bg=C["bg"])
        fl.pack(fill="x", pady=(px(14), px(8)))
        self.list_title = tk.Label(fl, text="", font=font(12, True), fg=C["text"], bg=C["bg"])
        self.list_title.pack(side="left")
        self.reset_btn = ui.Btn(fl, "Показать все", icon="close", command=self.reset_filters, bg=C["chip"], size=9,
                                padx=10, pady=3, bold=False)
        self.del_btn = ui.Btn(fl, "Удалённые", icon="delete", command=self.toggle_deleted, bg=C["bg"], fg=C["faint"],
                              size=9, padx=8, pady=3, bold=False, tooltip="Показать и удалённые операторами записи")
        self.del_btn.pack(side="right")
        # таблица
        tbl = tk.Frame(main, bg=C["surface"])
        tbl.pack(fill="both", expand=True)
        cols = [("time", "Перезвонить", 88), ("left", "Осталось", 128), ("status", "Статус", 118),
                ("fio", "ФИО", 190), ("phone", "Номер", 138), ("note", "Заметка", 150),
                ("operator", "Оператор", 128), ("room", "Комната", 64), ("created", "Звонил в", 72)]
        self.tree = ttk.Treeview(tbl, columns=[c[0] for c in cols], show="headings", selectmode="browse")
        for key, title, w in cols:
            self.tree.heading(key, text=title, anchor="w")
            self.tree.column(key, width=px(w), minwidth=px(60 if key in ("fio", "note") else w),
                             anchor="w", stretch=key in ("fio", "note"))
        self.fixed_cols = {k: px(w) for k, _, w in cols if k not in ("fio", "note")}
        self.tree.bind("<Configure>", self.fit_columns, add="+")
        sb = ttk.Scrollbar(tbl, orient="vertical", command=self.tree.yview, style="Vertical.TScrollbar")
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        for tag, col in (("missed", C["red"]), ("due", C["amber"]), ("upcoming", C["text"]),
                         ("done", C["green"]), ("deleted", C["faint"])):
            self.tree.tag_configure(tag, foreground=col)
        self.tree.tag_configure("odd", background=ui.blend(C["surface"], "#FFFFFF", 0.02))
        self.tree.bind("<Button-3>", self.row_menu)
        self.tree.bind("<Double-1>", lambda e: self.copy_selected())
        self.tree.bind("<Return>", lambda e: self.copy_selected())
        self.empty = tk.Label(tbl, text="", font=font(11), fg=C["faint"], bg=C["surface"])
        # статус
        foot = tk.Frame(f, bg=C["surface"])
        foot.pack(fill="x", side="bottom")
        self.dot = tk.Label(foot, text="●", font=font(9), fg=C["faint"], bg=C["surface"])
        self.dot.pack(side="left", padx=(px(16), px(4)), pady=px(6))
        self.status = tk.Label(foot, text="Загрузка…", font=font(9), fg=C["muted"], bg=C["surface"])
        self.status.pack(side="left")
        tk.Label(foot, text="Двойной щелчок — скопировать номер · правая кнопка — действия · F5 — обновить",
                 font=font(8), fg=C["faint"], bg=C["surface"]).pack(side="right", padx=px(16))
        self.update_day_label()
        self.next_at = 0

    def fit_columns(self, e=None):
        """ФИО и заметка делят то, что осталось от фиксированных столбцов — ничего не обрезается справа."""
        free = self.tree.winfo_width() - sum(self.fixed_cols.values()) - px(4)
        if free < px(160):
            return
        self.tree.column("fio", width=int(free * 0.58))
        self.tree.column("note", width=free - int(free * 0.58))

    def ctrl_key(self, e):
        if e.keycode == 37:
            self.shift_day(-1)
        elif e.keycode == 39:
            self.shift_day(1)
        elif e.keycode == 70:            # Ctrl+F (и «А» в русской раскладке)
            self.search.entry.focus_set()
            ui.entry_select_all(self.search.entry)

    # ═══════════════ данные ═══════════════
    def day_range(self):
        start = dt.datetime.combine(self.day, dt.time.min).timestamp()
        end = dt.datetime.combine(self.day + dt.timedelta(days=1), dt.time.min).timestamp()
        return start + self.offset, end + self.offset

    def refresh_now(self):
        self.next_at = 0

    def fetch(self):
        tok = self.s.token
        if not tok or self.busy:
            return
        self.busy = True
        a, b = self.day_range()
        api = pc.Api(self.s.d["server"], tok)
        params = {"from": "%.0f" % a, "to": "%.0f" % b, "carry": "1" if self.day == dt.date.today() else "0"}
        sent = time.time()

        def run():
            try:
                res = api.call("GET", "/api/admin/day", params=params)
                res["_sent"], res["_recv"] = sent, time.time()
                self.q.put(("day", res))
            except pc.ApiError as e:
                self.q.put(("day_err", e))
        threading.Thread(target=run, daemon=True).start()

    def loop(self):
        while True:
            try:
                kind, res = self.q.get_nowait()
            except queue.Empty:
                break
            self.handle(kind, res)
        if self.s.token and hasattr(self, "tree") and self.tree.winfo_exists():
            if time.time() >= self.next_at and not self.busy:
                self.next_at = time.time() + REFRESH
                self.fetch()
            self.tick()
        self.root.after(250, self.loop)

    def handle(self, kind, res):
        if kind == "day":
            self.busy = False
            self.offset = res["server_now"] - (res["_sent"] + res["_recv"]) / 2
            if abs(self.offset) < 2:
                self.offset = 0.0
            self.data = res
            self.last_ok = time.time()
            self.error = None
            self.render()
        elif kind == "day_err":
            self.busy = False
            if res.status in (401, 403):
                self.s.token = None
                self.build_login("Сессия закончилась или доступ отключён — войдите заново")
                return
            self.error = res.message
            self.paint_status()
        elif kind == "action":
            self.next_at = 0
        elif kind == "action_err":
            self.status.configure(text="Не получилось: " + res.message, fg=C["red"])

    def items_local(self):
        """Элементы со временем, переведённым в часы этого компьютера."""
        out = []
        for it in (self.data or {}).get("items", []):
            d = dict(it)
            for k in ("due", "created", "done_at"):
                if d.get(k) is not None:
                    d[k] = d[k] - self.offset
            out.append(d)
        return out

    def render(self):
        if not self.data or not hasattr(self, "tree"):
            return
        t = time.time()
        grace = self.data.get("grace", pc.MISSED_GRACE)
        items = self.items_local()
        users = self.data.get("users", [])
        rooms = list(self.data.get("rooms", []))
        for it in items:
            if it.get("room") and it["room"] not in rooms:
                rooms.append(it["room"])
        for x in users:
            if x["room"] not in rooms:
                rooms.append(x["room"])
        if self.room and self.room not in rooms:
            self.room = None
        # подсчёты
        per_room = {}
        per_op = {}
        for it in items:
            bk = bucket(it, t, grace)
            per_room.setdefault(it.get("room"), {}).setdefault(bk, 0)
            per_room[it.get("room")][bk] += 1
            per_op.setdefault(it.get("user_id"), {}).setdefault(bk, 0)
            per_op[it.get("user_id")][bk] += 1
        scope = [it for it in items if (self.room is None or it.get("room") == self.room)
                 and (self.operator is None or it.get("user_id") == self.operator)]
        counts = {k: 0 for k, _, _ in FILTERS}
        for it in scope:
            bk = bucket(it, t, grace)
            if bk in counts:
                counts[bk] += 1
        for k, (card, n) in self.stat.items():
            n.configure(text=str(counts[k]))
            card.configure(highlightbackground=C[dict((a, c) for a, _, c in FILTERS)[k]] if self.filter == k else C["surface"])
        self.render_rooms(rooms, per_room, items)
        self.render_ops(users, per_op)
        # таблица
        q = self.search.get().strip().lower()
        qd = pc.phone_digits(q)
        rows = []
        for it in scope:
            bk = bucket(it, t, grace)
            if bk == "deleted" and not self.show_deleted:
                continue
            if self.filter and bk != self.filter:
                continue
            if q:
                hay = " ".join(str(it.get(k) or "") for k in ("fio", "phone", "note", "operator", "room")).lower()
                if q not in hay and not (len(qd) >= 3 and qd in pc.phone_digits(it.get("phone"))):
                    continue
            rows.append((bk, it))
        order = {"missed": 0, "due": 1, "upcoming": 2, "done": 3, "deleted": 4}
        rows.sort(key=lambda r: (order[r[0]], r[1]["due"] if r[0] != "done" else -(r[1].get("done_at") or 0)))
        self.rows = {it["id"]: it for _, it in rows}
        sel = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        for i, (bk, it) in enumerate(rows):
            tags = (bk,) + (("odd",) if i % 2 else ())
            self.tree.insert("", "end", iid=it["id"], values=self.row_values(it, bk, t), tags=tags)
        if sel and self.tree.exists(sel[0]):
            self.tree.selection_set(sel[0])
        if rows:
            self.empty.place_forget()
        else:
            self.empty.configure(text="Здесь пусто" if (self.filter or q or self.room or self.operator)
                                 else "На этот день перезвонов нет")
            self.empty.place(relx=0.5, rely=0.4, anchor="center")
        title = ("Все комнаты" if self.room is None else "Комната " + self.room)
        if len(rooms) == 1 and not self.data.get("can_act", True):
            title = "Комната " + rooms[0] + " · только просмотр"
        if self.operator is not None:
            title += " · " + next((x["name"] for x in users if x["id"] == self.operator), "?")
        if self.filter:
            title += " · " + dict((a, b) for a, b, _ in FILTERS)[self.filter].lower()
        self.list_title.configure(text="%s  —  %d" % (title, len(rows)))
        if self.filter or self.room or self.operator is not None or q:
            self.reset_btn.pack(side="left", padx=px(12))
        else:
            self.reset_btn.pack_forget()
        bot = self.data.get("bot")
        self.bot_hint.configure(text="Пользователи, комнаты и роли создаются в Telegram-боте%s. "
                                     "Туда же приходят пропущенные перезвоны.%s" % (
                                         " @" + bot if bot else "",
                                         "" if self.data.get("can_act", True) else
                                         "\nВы админ: видна только ваша комната."))
        self.paint_status()

    def row_values(self, it, bk, t):
        if bk == "done":
            left = "в " + pc.fmt_clock(it.get("done_at") or it["due"], t)
            if it.get("done_at") and it["done_at"] - it["due"] > 60:
                left += " · +" + pc.fmt_duration(it["done_at"] - it["due"])
        elif bk == "deleted":
            left = "—"
        else:
            left = pc.fmt_left(it["due"], t)
        st = {"upcoming": "Предстоит", "due": "Пора звонить", "missed": "Пропущен", "done": "Выполнен",
              "deleted": "Удалён"}[bk]
        if bk == "done" and (it.get("done_by") or "").startswith("admin"):
            st += " (админ)"
        elif bk == "done" and (it.get("done_by") or "").startswith("tg"):
            st += " (Telegram)"
        elif it.get("attempts") and bk != "deleted":
            st += " · недозвон %d" % it["attempts"]
        return (pc.fmt_clock(it["due"], t), left, st, it.get("fio") or "без имени", pc.phone_display(it.get("phone")),
                it.get("note") or "", it.get("operator") or "", it.get("room") or "",
                pc.fmt_clock(it["created"], t) if it.get("created") else "")

    def tick(self):
        """Раз в секунду пересчитываем «осталось» без запроса к серверу."""
        if not self.data or not getattr(self, "rows", None):
            return
        if getattr(self, "_last_tick", 0) > time.time() - 1:
            return
        self._last_tick = time.time()
        t = time.time()
        grace = self.data.get("grace", pc.MISSED_GRACE)
        for iid, it in self.rows.items():
            if self.tree.exists(iid):
                bk = bucket(it, t, grace)
                old = self.tree.item(iid, "tags")
                if old and old[0] != bk:
                    self.render()
                    return
                self.tree.set(iid, "left", self.row_values(it, bk, t)[1])
        self.paint_status()

    def render_rooms(self, rooms, per_room, items):
        for w in self.rooms_box.winfo_children():
            w.destroy()
        allc = {}
        for st in per_room.values():
            for k, v in st.items():
                allc[k] = allc.get(k, 0) + v
        self.room_row(None, "Все комнаты", allc)
        for r in sorted(rooms, key=lambda s: (s or "").lower()):
            self.room_row(r, r, per_room.get(r, {}))

    def room_row(self, key, title, st):
        sel = self.room == key
        bg = C["card_h"] if sel else C["surface"]
        row = tk.Frame(self.rooms_box, bg=bg, cursor="hand2")
        row.pack(fill="x", padx=px(10), pady=1)
        bar = tk.Frame(row, bg=C["accent"] if sel else bg, width=px(3))
        bar.pack(side="left", fill="y")
        lbl = tk.Label(row, text=title, font=font(10, sel), fg=C["text"], bg=bg)
        lbl.pack(side="left", padx=px(10), pady=px(7))
        parts = [row, lbl]
        if st.get("missed"):
            b = tk.Label(row, text=" %d " % st["missed"], font=font(8, True), fg=C["white"], bg=C["red"])
            b.pack(side="right", padx=(px(4), px(10)))
            parts.append(b)
        waiting = st.get("upcoming", 0) + st.get("due", 0)
        w = tk.Label(row, text=str(waiting) if waiting else "", font=font(9), fg=C["muted"], bg=bg)
        w.pack(side="right", padx=(0, px(4) if st.get("missed") else px(10)))
        parts.append(w)
        for p in parts:
            p.bind("<Button-1>", lambda e, k=key: self.set_room(k))
        ui.Tooltip(row, "ждут: %d · пропущено: %d · выполнено: %d" % (waiting, st.get("missed", 0), st.get("done", 0)))

    def render_ops(self, users, per_op):
        inner = self.ops_area.inner
        for w in inner.winfo_children():
            w.destroy()
        shown = [x for x in users if self.room is None or x["room"] == self.room]
        shown.sort(key=lambda x: (not x["online"], x["name"].lower()))
        if not shown:
            tk.Label(inner, text="Нет пользователей", font=font(9), fg=C["faint"], bg=C["surface"]).pack(
                anchor="w", padx=px(18))
        for x in shown:
            sel = self.operator == x["id"]
            bg = C["card_h"] if sel else C["surface"]
            row = tk.Frame(inner, bg=bg, cursor="hand2")
            row.pack(fill="x", padx=px(10), pady=1)
            dot = tk.Label(row, text="●", font=font(9), bg=bg,
                           fg=C["faint"] if x["blocked"] else (C["green"] if x["online"] else C["faint"]))
            dot.pack(side="left", padx=(px(8), px(6)), pady=px(6))
            t = tk.Frame(row, bg=bg)
            t.pack(side="left", fill="x", expand=True)
            name = x["name"] + {"admin": "  · админ", "superadmin": "  · супер-админ"}.get(x["role"], "") + \
                ("  · отключён" if x["blocked"] else "")
            n = tk.Label(t, text=name, font=font(9, sel), fg=C["text"] if not x["blocked"] else C["faint"], bg=bg, anchor="w")
            n.pack(anchor="w")
            st = per_op.get(x["id"], {})
            sub = "%s · ждут %d" % (x["room"], st.get("upcoming", 0) + st.get("due", 0))
            if st.get("missed"):
                sub += " · пропущено %d" % st["missed"]
            if not x["online"]:
                sub += " · " + ("был(а) " + pc.fmt_clock(x["last_seen"] - self.offset) if x.get("last_seen") else "не входил(а)")
            s = tk.Label(t, text=sub, font=font(8), fg=C["red"] if st.get("missed") else C["muted"], bg=bg, anchor="w")
            s.pack(anchor="w")
            for p in (row, dot, t, n, s):
                p.bind("<Button-1>", lambda e, k=x["id"]: self.set_operator(k))

    def paint_status(self):
        if not hasattr(self, "status"):
            return
        if self.error:
            self.dot.configure(fg=C["amber"])
            self.status.configure(text="Нет связи с сервером: %s — показаны последние данные" % self.error, fg=C["amber"])
        elif self.last_ok:
            self.dot.configure(fg=C["green"])
            self.status.configure(text="Обновлено в %s · обновляется каждые %d с" % (
                time.strftime("%H:%M:%S", time.localtime(self.last_ok)), REFRESH), fg=C["muted"])

    # ═══════════════ фильтры и действия ═══════════════
    def update_day_label(self):
        d = self.day
        rel = {0: "Сегодня", -1: "Вчера", 1: "Завтра"}.get((d - dt.date.today()).days)
        txt = "%d %s, %s" % (d.day, MONTHS[d.month - 1], WEEKDAYS[d.weekday()])
        self.day_lbl.configure(text=(rel + ", " + txt) if rel else txt)

    def set_day(self, d):
        self.day = d
        self.update_day_label()
        self.data = None
        self.tree.delete(*self.tree.get_children())
        self.refresh_now()

    def shift_day(self, n):
        self.set_day(self.day + dt.timedelta(days=n))

    def set_filter(self, k):
        self.filter = None if self.filter == k else k
        self.render()

    def set_room(self, r):
        self.room = r
        self.operator = None
        self.render()

    def set_operator(self, uid):
        self.operator = None if self.operator == uid else uid
        self.render()

    def reset_filters(self):
        self.filter = self.room = self.operator = None
        self.search.set("")
        self.render()

    def toggle_deleted(self):
        self.show_deleted = not self.show_deleted
        self.del_btn.set_colors(C["chip"] if self.show_deleted else C["bg"],
                                fg=C["text"] if self.show_deleted else C["faint"])
        self.render()

    def selected(self):
        sel = self.tree.selection()
        return self.rows.get(sel[0]) if sel else None

    def copy_selected(self):
        it = self.selected()
        if it:
            num = pc.phone_for_copy(it["phone"], self.s.d.get("copy_fmt", "plus7"))
            ui.copy_text(self.root, num)
            self.status.configure(text="Скопировано: " + num, fg=C["accent_h"])

    def row_menu(self, e):
        iid = self.tree.identify_row(e.y)
        if not iid:
            return
        self.tree.selection_set(iid)
        it = self.rows.get(iid)
        m = tk.Menu(self.root, tearoff=0, bg=C["card"], fg=C["text"], activebackground=C["accent_d"],
                    activeforeground=C["white"], bd=0, font=font(10))
        m.add_command(label="Скопировать номер", command=self.copy_selected)
        if not (self.data or {}).get("can_act", True):
            m.add_separator()
            m.add_command(label="Админ только смотрит — менять может супер-админ", state="disabled")
            m.tk_popup(e.x_root, e.y_root)
            return
        if it["status"] == "active":
            m.add_command(label="Отметить: перезвонили", command=lambda: self.action(iid, "done"))
            m.add_command(label="Отложить на 15 минут", command=lambda: self.action(iid, "snooze15"))
        else:
            m.add_command(label="Вернуть в работу", command=lambda: self.action(iid, "restore"))
        if it["status"] != "deleted":
            m.add_separator()
            m.add_command(label="Удалить", command=lambda: self.action(iid, "delete"))
        m.tk_popup(e.x_root, e.y_root)

    def action(self, iid, act):
        api = pc.Api(self.s.d["server"], self.s.token)

        def run():
            try:
                self.q.put(("action", api.call("POST", "/api/admin/item", {"id": iid, "action": act})))
            except pc.ApiError as e:
                self.q.put(("action_err", e))
        threading.Thread(target=run, daemon=True).start()


def _safe(fn):
    try:
        fn()
    except Exception:
        pass


def main():
    ui.setup_dpi()
    inst = ui.SingleInstance("Perezvon.Admin")
    if not inst.ok:
        return 0
    app = AdminApp()
    if os.environ.get("PEREZVON_SELFTEST"):
        app.root.withdraw()
        app.root.update()
        with open(os.path.join(app.home, "selftest.ok"), "w") as f:
            f.write(pc.APP_VERSION)
        return 0
    app.root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
