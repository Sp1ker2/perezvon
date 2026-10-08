# -*- coding: utf-8 -*-
"""«Перезвон» — программа оператора.

Окно: вкладка «Сегодня» (новый перезвон + все сегодняшние) и «История» (по датам).
Крестик/«свернуть» убирают окно в маленькую иконку у края экрана (клик — открыть).
В срок всплывает понятное напоминание со звуком.
Перезвоны синхронизируются с сервером (админ видит все комнаты), без сети всё работает локально.
"""
import datetime as dt
import hashlib
import os
import queue
import shutil
import socket
import subprocess
import sys
import threading
import time
import tkinter as tk

import pz_common as pc
import pz_ui as ui
import pz_update
from pz_login import LoginForm
from pz_ui import C, I, px, font, ifont

HOTKEY_VK = 0x50          # Ctrl+Alt+P (в русской раскладке — та же клавиша «З»)
HOTKEY_TEXT = "Ctrl+Alt+P"
SYNC_EVERY = 10
QUICK = [("5 мин", "5"), ("10 мин", "10"), ("15 мин", "15"), ("30 мин", "30"), ("1 ч", "1ч"), ("2 ч", "2ч")]
COPY_FORMATS = [("plus7", "+7…"), ("8", "8…"), ("raw", "как ввели")]
INSTANCE = os.environ.get("PEREZVON_INSTANCE") or "Perezvon.Client"   # переопределяют только тесты


def now():
    return time.time()


# ═══════════════════════════════ настройки ═══════════════════════════════

class Settings:
    """Настройки — в settings.json; данные входа (token, user) — в зашифрованной сессии ui.Session."""
    DEFAULTS = {"server": pc.DEFAULT_SERVER, "local_mode": False,
                "geom": None, "copy_fmt": "plus7", "sound": True, "bot": None, "form_open": True,
                "dock": {"edge": "right", "pos": 0.32, "mx": None, "my": None}}

    def __init__(self, path, session):
        self.path = path
        self.session = session
        d = pc.load_json(path, {})
        self.d = dict(self.DEFAULTS)
        self.d.update({k: v for k, v in d.items() if k in self.DEFAULTS})
        if d.get("token_dp") or d.get("user"):          # 1.0.0 держал вход в settings.json — переносим
            if not session.get("token"):
                session.set(token=ui.secret_unpack(d.get("token_dp")), user=d.get("user"))
            self.save()

    def __getitem__(self, k):
        if k in ("token", "user"):
            return self.session.get(k)
        return self.d.get(k)

    def __setitem__(self, k, v):
        self.set(k, v)
        self.save()

    def set(self, k, v):
        if k in ("token", "user"):
            self.session.set(**{k: v})
        else:
            self.d[k] = v

    def save(self):
        pc.save_json(self.path, self.d)


# ═══════════════════════════════ синхронизация ═══════════════════════════════

class Sync:
    """HTTP в отдельном потоке; результат применяется в главном потоке Tk (через очередь)."""

    def __init__(self, app):
        self.app = app
        self.q = queue.Queue()
        self.busy = False
        self.next_at = 0
        self.fail_streak = 0
        self.last_ok = None
        self.error = None
        self.need_login = False

    def kick(self, delay=0.4):
        self.next_at = min(self.next_at, now() + delay)

    def tick(self):
        s = self.app.settings
        while True:
            try:
                kind, pushed, res = self.q.get_nowait()
            except queue.Empty:
                break
            self.busy = False
            self._apply(kind, pushed, res)
        if self.busy or not s["token"] or now() < self.next_at:
            return
        body, pushed = self.app.store.build_push()
        body.update(version=pc.APP_VERSION, pc=socket.gethostname())
        api = pc.Api(s["server"], s["token"])
        self.busy = True
        self.next_at = now() + SYNC_EVERY

        def run():
            try:
                self.q.put(("ok", pushed, api.call("POST", "/api/sync", body)))
            except pc.ApiError as e:
                self.q.put(("err", pushed, e))
            except Exception as e:      # на всякий случай — поток не должен умирать молча
                self.q.put(("err", pushed, pc.ApiError(0, str(e))))
        threading.Thread(target=run, daemon=True).start()

    def _apply(self, kind, pushed, res):
        app = self.app
        if kind == "ok":
            self.fail_streak = 0
            self.error = None
            self.need_login = False
            self.last_ok = now()
            u = res.get("user") or {}
            if u.get("id") is not None and app.store.owner != u["id"]:
                app.adopt_user(u["id"])
                self.kick(0.1)                 # сразу скачать список этого пользователя
                app.refresh_status()
                return
            ext = app.store.apply_sync(pushed, res)
            if res.get("bot") and res["bot"] != app.settings["bot"]:
                app.settings["bot"] = res["bot"]
            u = res.get("user")
            if u and u != app.settings["user"]:
                app.settings["user"] = u
                app.refresh_header()
            if res.get("reset") or app.store.dirty_count():
                self.kick(0.3)
            if ext:
                app.on_external_change(ext)
        else:
            e = res
            if e.status == 401:
                self.need_login = True
                self.error = "Нужно войти заново"
                app.settings["token"] = None
            else:
                self.fail_streak += 1
                self.error = e.message
                self.next_at = now() + min(SYNC_EVERY * (2 ** min(self.fail_streak, 3)), 90)
        app.refresh_status()


# ═══════════════════════════════ главное окно ═══════════════════════════════

MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
          "ноября", "декабря"]
WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]


def day_title(d, today=None):
    today = today or dt.date.today()
    base = "%d %s" % (d.day, MONTHS[d.month - 1])
    rel = {0: "Сегодня", -1: "Вчера", 1: "Завтра"}.get((d - today).days)
    return "%s · %s" % (rel, base) if rel else "%s, %s" % (base, WEEKDAYS[d.weekday()])


class MainWindow:
    """Обычное окно с кнопкой на панели задач: вкладки «Сегодня» (форма + перезвоны) и «История»."""
    W, H = 460, 860

    def __init__(self, app):
        self.app = app
        self.win = app.root
        self.win.configure(bg=C["surface"])
        self.win.minsize(px(400), px(520))
        self.edit_id = None
        self.edit_preset = None
        self.tab = "today"            # today | history
        self.cards = {}
        self.sig = None
        self.hist_sig = None
        self.chips = []
        self.build()
        self.win.protocol("WM_DELETE_WINDOW", self.minimize)
        self.win.bind("<Configure>", self.on_configure, add="+")
        self._geom_job = None

    # ── сборка
    def build(self):
        root = tk.Frame(self.win, bg=C["surface"])
        root.pack(fill="both", expand=True)
        self.body = root
        head = tk.Frame(root, bg=C["surface"])
        head.pack(fill="x", padx=px(18), pady=(px(14), px(4)))
        ui.IconBtn(head, "settings", self.app.open_settings, tooltip="Настройки").pack(side="right")
        self.acc_btn = ui.Btn(head, "Выйти", icon="signout", command=self.app.account_click, bg=C["chip"],
                              fg=C["text"], size=9, padx=10, pady=4, bold=False,
                              tooltip=lambda: "Выйти из аккаунта — перезвоны останутся на сервере"
                              if self.app.settings["token"] else "Войти по нику Telegram")
        self.acc_btn.pack(side="right", padx=(0, px(6)))
        logo = tk.Label(head, text=I["phone"], font=ifont(13), fg=C["white"], bg=C["accent"], width=2, pady=px(4))
        logo.pack(side="left")
        tt = tk.Frame(head, bg=C["surface"])
        tt.pack(side="left", padx=(px(10), 0), fill="x", expand=True)
        tk.Label(tt, text="Перезвон", font=font(13, True), fg=C["text"], bg=C["surface"]).pack(anchor="w")
        self.sub = tk.Label(tt, text="", font=font(8), fg=C["muted"], bg=C["surface"], anchor="w")
        self.sub.pack(anchor="w", fill="x")

        self.banner = tk.Frame(root, bg=C["surface"])
        self.banner.pack(fill="x")

        # вкладки
        tabs = tk.Frame(root, bg=C["surface"])
        tabs.pack(fill="x", padx=px(18), pady=(px(8), 0))
        self.tab_btns = {}
        for key, title in (("today", "Сегодня"), ("history", "История")):
            f = tk.Frame(tabs, bg=C["surface"], cursor="hand2")
            f.pack(side="left", padx=(0, px(18)))
            lbl = tk.Label(f, text=title, font=font(11, True), bg=C["surface"], cursor="hand2")
            lbl.pack(pady=(0, px(6)))
            line = tk.Frame(f, height=px(3), bg=C["surface"])
            line.pack(fill="x")
            for w in (f, lbl, line):
                w.bind("<Button-1>", lambda e, k=key: self.show_tab(k))
            self.tab_btns[key] = (lbl, line)
        tk.Frame(root, bg=C["border"], height=1).pack(fill="x", padx=px(18))

        # подвал (пакуем раньше содержимого, чтобы он всегда был виден)
        foot = tk.Frame(root, bg=C["bg"])
        foot.pack(fill="x", side="bottom")
        self.status_dot = tk.Label(foot, text="●", font=font(9), fg=C["faint"], bg=C["bg"])
        self.status_dot.pack(side="left", padx=(px(14), px(4)), pady=px(7))
        self.status = tk.Label(foot, text="", font=font(8), fg=C["muted"], bg=C["bg"], cursor="hand2")
        self.status.pack(side="left")
        self.status.bind("<Button-1>", lambda e: self.app.status_click())
        tk.Label(foot, text="%s — открыть из любого окна" % HOTKEY_TEXT, font=font(8), fg=C["faint"],
                 bg=C["bg"]).pack(side="right", padx=px(12))

        self.pages = tk.Frame(root, bg=C["surface"])
        self.pages.pack(fill="both", expand=True)
        self.page_today = tk.Frame(self.pages, bg=C["surface"])
        self.page_hist = tk.Frame(self.pages, bg=C["surface"])
        self.build_today(self.page_today)
        self.build_history(self.page_hist)
        self.win.bind("<Escape>", lambda e: self.escape())
        self.show_tab("today", focus=False)

    def build_today(self, page):
        outer = tk.Frame(page, bg=C["surface"])
        outer.pack(fill="x", padx=px(18), pady=(px(10), 0))
        fh = tk.Frame(outer, bg=C["surface"])
        fh.pack(fill="x")
        self.form_head = fh
        self.form_title = tk.Label(fh, text="Новый перезвон", font=font(10, True), fg=C["muted"], bg=C["surface"])
        self.form_title.pack(side="left")
        self.fold_btn = ui.Btn(fh, "Свернуть", icon="\uE70E", command=self.toggle_form, bg=C["surface"],
                               fg=C["muted"], size=9, padx=6, pady=2, bold=False,
                               tooltip="Свернуть форму — будет виден весь список")
        self.fold_btn.pack(side="right")
        self.cancel_edit_btn = ui.Btn(fh, "Отмена", icon="close", command=self.cancel_edit, bg=C["surface"],
                                      fg=C["muted"], size=9, padx=6, pady=2, bold=False)
        self.new_btn = ui.Btn(outer, "Новый перезвон", icon="add", command=lambda: self.open_form(True),
                              bg=C["accent"], hover=C["accent_h"], fg=C["white"], size=11, pady=9)
        form = tk.Frame(outer, bg=C["surface"])
        self.form = form

        two = tk.Frame(form, bg=C["surface"])
        two.pack(fill="x", pady=(px(6), 0))
        two.columnconfigure(0, weight=1, uniform="fp")
        two.columnconfigure(1, weight=1, uniform="fp")
        self.f_fio = ui.Field(two, "ФИО", "Иванов Иван", icon="contact", width=8)
        self.f_fio.grid(row=0, column=0, sticky="new", padx=(0, px(5)))
        self.f_phone = ui.Field(two, "Входящий номер", "+7 999 123-45-67", icon="phone", width=8)
        self.f_phone.grid(row=0, column=1, sticky="new", padx=(px(5), 0))
        self.clip_chip = tk.Label(form, text="", font=font(8, True), fg=C["accent_h"],
                                  bg=C["surface"], cursor="hand2", anchor="e")
        self.clip_chip.bind("<Button-1>", lambda e: self.paste_clip())
        self.clip_anchor = two
        self.f_note = ui.Field(form, "Заметка", "о чём договорились", icon="note", optional=True)
        self.f_note.pack(fill="x", pady=(px(8), 0))

        tk.Label(form, text="Перезвонить через", font=font(9, True), fg=C["muted"], bg=C["surface"]).pack(
            anchor="w", pady=(px(8), px(4)))
        chips = tk.Frame(form, bg=C["surface"])
        chips.pack(fill="x")
        for label, val in QUICK:
            ch = ui.Chip(chips, label, lambda v=val: self.pick(v))
            ch.value = val
            ch.pack(side="left", padx=(0, px(5)))
            self.chips.append(ch)
        self.f_when = ui.Field(form, "или своё", "25 · 1ч 30м · 14:30 · завтра в 10", icon="clock")
        self.f_when.pack(fill="x", pady=(px(8), 0))
        self.preview = tk.Label(form, text="", font=font(9, True), fg=C["muted"], bg=C["surface"], anchor="w",
                                justify="left", wraplength=px(400))
        self.preview.pack(fill="x", pady=(px(6), 0))
        self.add_btn = ui.Btn(form, "Добавить перезвон", icon="add", command=self.submit, bg=C["accent"],
                              hover=C["accent_h"], fg=C["white"], size=11, pady=9)
        self.add_btn.pack(fill="x", pady=(px(8), 0))
        self.toast = tk.Label(form, text="", font=font(9, True), fg=C["green"], bg=C["surface"])
        self.toast.pack(fill="x")

        for f in (self.f_fio, self.f_phone, self.f_note, self.f_when):
            f.entry.bind("<Return>", lambda e: self.submit())
            f.entry.bind("<KP_Enter>", lambda e: self.submit())
        self.f_when.var.trace_add("write", lambda *_: self.on_when())
        self.f_phone.var.trace_add("write", lambda *_: (self.f_phone.error(), self.update_clip()))
        self.f_phone.entry.bind("<FocusIn>", lambda e: self.update_clip(), add="+")
        self.open_form(self.app.settings["form_open"] is not False, focus=False)

        self.area = ui.ScrollArea(page)
        self.area.pack(fill="both", expand=True, padx=(px(18), px(8)), pady=(px(4), 0))

    def build_history(self, page):
        top = tk.Frame(page, bg=C["surface"])
        top.pack(fill="x", padx=px(18), pady=(px(12), px(8)))
        self.h_search = ui.Field(top, "", "Поиск: ФИО, номер, заметка", icon="search")
        self.h_search.winfo_children()[0].pack_forget()
        self.h_search.box.pack_configure(pady=0)
        self.h_search.pack(fill="x")
        self.h_search.var.trace_add("write", lambda *_: self.refresh_history(force=True))
        self.h_area = ui.ScrollArea(page)
        self.h_area.pack(fill="both", expand=True, padx=(px(18), px(8)))

    # ── окно
    def show_tab(self, key, focus=True):
        self.tab = key
        for k, (lbl, line) in self.tab_btns.items():
            on = k == key
            lbl.configure(fg=C["text"] if on else C["muted"])
            line.configure(bg=C["accent"] if on else C["surface"])
        self.page_today.pack_forget()
        self.page_hist.pack_forget()
        if key == "today":
            self.page_today.pack(fill="both", expand=True)
            self.refresh(force=True)
        else:
            self.page_hist.pack(fill="both", expand=True)
            self.refresh_history(force=True)
            self.h_area.top()
            if focus:
                self.h_search.entry.focus_set()

    def visible(self):
        try:
            return self.win.state() == "normal"
        except tk.TclError:
            return False

    def show(self, focus=True):
        self.win.deiconify()
        if self.win.state() == "iconic":
            self.win.state("normal")
        self.refresh(force=True)
        self.update_clip()
        if focus:
            ui.force_foreground(self.win)
            if self.tab == "today" and not self.form_open:
                self.open_form(True, focus=False)
            if self.tab == "today":
                target = self.f_phone.entry if (self.f_fio.get() and not self.f_phone.get()) else self.f_fio.entry
                self.win.after(30, lambda: (target.focus_force(), target.icursor("end")))

    def minimize(self):
        """Крестик, «свернуть» и Esc — окно прячется в иконку у края экрана; программа продолжает напоминать."""
        self.app.collapse()

    def escape(self):
        if self.edit_id:
            self.cancel_edit()
        else:
            self.minimize()

    def place_initial(self):
        g = self.app.settings["geom"]
        l, t, r, b = ui.work_area(self.win.winfo_screenwidth() // 2, self.win.winfo_screenheight() // 2)
        if g and len(g) == 4:
            x, y, w, h = g
            wa = ui.work_area(x + w // 2, y + 20)
            if wa[0] - 50 <= x <= wa[2] - 100 and wa[1] - 10 <= y <= wa[3] - 100:
                self.win.geometry("%dx%d+%d+%d" % (w, min(h, wa[3] - wa[1]), x, y))
                return
        W, H = px(self.W), min(px(self.H), b - t - px(20))
        self.win.geometry("%dx%d+%d+%d" % (W, H, r - W - px(24), t + (b - t - H) // 2))

    def on_configure(self, e):
        if e.widget is not self.win or self.win.state() != "normal":
            return
        if self._geom_job:
            self.win.after_cancel(self._geom_job)
        self._geom_job = self.win.after(800, self.save_geom)

    def save_geom(self):
        self._geom_job = None
        try:
            if self.win.state() != "normal" or not self.win.winfo_ismapped():
                return                          # скрытое/свёрнутое окно — его координаты не настоящие
            g = [self.win.winfo_x(), self.win.winfo_y(), self.win.winfo_width(), self.win.winfo_height()]
        except tk.TclError:
            return
        if g != self.app.settings["geom"]:
            self.app.settings["geom"] = g

    # ── форма
    def open_form(self, on, focus=True):
        """Форма развёрнута — поля; свёрнута — одна кнопка «Новый перезвон», весь список на виду."""
        self.form_open = on
        if on:
            self.new_btn.pack_forget()
            self.form_head.pack(fill="x")
            self.form.pack(fill="x", after=self.form_head)
            if focus:
                self.f_fio.entry.focus_set()
        else:
            self.form.pack_forget()
            self.form_head.pack_forget()        # пустая рамка в Tk сохраняет высоту — убираем целиком
            self.new_btn.pack(fill="x", pady=(px(4), 0))
        if self.app.settings["form_open"] != on:
            self.app.settings["form_open"] = on

    def toggle_form(self):
        self.open_form(not self.form_open)

    def pick(self, val):
        self.f_when.set(val)
        self.f_when.error()

    def on_when(self):
        txt = self.f_when.get()
        for ch in self.chips:
            ch.select(txt.strip() == ch.value)
        self.f_when.error()
        self.update_preview()

    def parse_due(self):
        txt = self.f_when.get()
        if self.edit_id and self.edit_preset is not None and txt.strip() == self.edit_preset:
            return self.app.store.items[self.edit_id]["due"]
        due, _ = pc.parse_when(txt)
        return due.timestamp()

    def update_preview(self):
        txt = self.f_when.get().strip()
        if not txt:
            self.preview.configure(text="", fg=C["muted"])
            return
        try:
            due = self.parse_due()
        except pc.WhenError as e:
            self.preview.configure(text=str(e), fg=C["amber"])
            return
        self.preview.configure(text=pc.describe_due(due, now()), fg=C["accent_h"])

    def update_clip(self):
        clip = ui.clipboard_text(self.app.root).strip()
        if not self.f_phone.get() and pc.looks_like_phone(clip):
            self.clip_chip.configure(text="Вставить номер из буфера:  " + pc.phone_display(clip))
            self.clip_chip.clip = clip
            if not self.clip_chip.winfo_ismapped():
                self.clip_chip.pack(after=self.clip_anchor, fill="x", pady=(px(3), 0))
        else:
            self.clip_chip.pack_forget()

    def paste_clip(self):
        self.f_phone.set(getattr(self.clip_chip, "clip", ""))
        self.f_phone.entry.focus_set()

    def submit(self):
        ok = True
        phone = self.f_phone.get().strip()
        if not phone:
            self.f_phone.error("Укажите номер, с которого звонили")
            ok = False
        elif not pc.phone_valid(phone):
            self.f_phone.error("В номере должно быть хотя бы 3 цифры")
            ok = False
        due = None
        if not self.f_when.get().strip():
            self.f_when.error("Выберите, через сколько перезвонить — кнопкой выше или впишите")
            ok = False
        else:
            try:
                due = self.parse_due()
            except pc.WhenError as e:
                self.f_when.error(str(e))
                self.preview.configure(text="")          # не дублировать ту же ошибку ниже
                ok = False
        if not ok:
            for f in (self.f_phone, self.f_when):
                if f.err.winfo_ismapped():
                    f.entry.focus_set()
                    break
            return "break"
        st = self.app.store
        if self.edit_id and self.edit_id in st.items:
            it = st.items[self.edit_id]
            ch = {"fio": self.f_fio.get(), "phone": phone, "note": self.f_note.get(), "due": due}
            if abs(it["due"] - due) > 1:
                ch["status"] = "active"
            st.update(self.edit_id, **ch)
            msg = "Сохранено · " + pc.describe_due(due, now()).replace("Напомню ", "напомню ")
            self.app.reminders.close(self.edit_id)
        else:
            u = self.app.settings["user"] or {}
            st.add(self.f_fio.get(), phone, self.f_note.get(), due, operator=u.get("name", ""), room=u.get("room", ""))
            msg = "Добавлено · " + pc.describe_due(due, now()).replace("Напомню ", "напомню ")
        self.clear_form()
        self.flash(msg)
        self.app.changed()
        self.f_fio.entry.focus_set()
        return "break"

    def clear_form(self):
        for f in (self.f_fio, self.f_phone, self.f_note, self.f_when):
            f.set("")
            f.error()
        self.edit_id = None
        self.edit_preset = None
        self.form_title.configure(text="Новый перезвон")
        self.add_btn.set_text("Добавить перезвон")
        self.cancel_edit_btn.pack_forget()

    def flash(self, msg, color=None):
        self.toast.configure(text="✓  " + msg, fg=color or C["green"], cursor="")
        self.toast.unbind("<Button-1>")
        if getattr(self, "_toast_job", None):
            self.win.after_cancel(self._toast_job)
        self._toast_job = self.win.after(5000, lambda: self.toast.configure(text="", cursor=""))

    def flash_undo(self, msg, undo):
        self.undo_cb = undo
        self.flash(msg + "   ↶ вернуть", C["muted"])
        self.toast.configure(cursor="hand2")
        self.toast.bind("<Button-1>", lambda e: (undo(), self.toast.configure(text="", cursor=""),
                                                 self.toast.unbind("<Button-1>")))

    def start_edit(self, iid):
        it = self.app.store.items.get(iid)
        if not it:
            return
        self.show_tab("today", focus=False)
        self.open_form(True, focus=False)
        self.clear_form()
        self.edit_id = iid
        self.f_fio.set(it["fio"])
        self.f_phone.set(it["phone"])
        self.f_note.set(it.get("note") or "")
        if it["due"] > now() and it["due"] - now() < 20 * 3600:
            self.edit_preset = dt.datetime.fromtimestamp(it["due"]).strftime("%H:%M")
            self.f_when.set(self.edit_preset)
        self.form_title.configure(text="Изменить перезвон", fg=C["text"])
        self.add_btn.set_text("Сохранить")
        self.cancel_edit_btn.pack(side="right")
        self.show(focus=False)
        self.f_when.entry.focus_set()
        self.f_when.entry.select_range(0, "end")

    def cancel_edit(self):
        self.clear_form()
        self.form_title.configure(fg=C["muted"])

    # ── «Сегодня»: ждут + выполнено сегодня
    def refresh(self, force=False):
        if self.tab != "today":
            if self.tab == "history":
                self.refresh_history()
            return
        st = self.app.store
        t = now()
        active, done = st.active(), st.done_today()
        sig = tuple((i["id"], i["fio"], i["phone"], i.get("note"), i["due"], i.get("attempts"), i["status"])
                    for i in active + done)
        if force or sig != self.sig:
            self.sig = sig
            self.rebuild(active, done)
        self.tick_cards(t)

    def section(self, parent, title, count, color=None):
        h = tk.Frame(parent, bg=C["surface"])
        h.pack(fill="x", pady=(px(10), px(6)), padx=(0, px(8)))
        tk.Label(h, text=title, font=font(11, True), fg=C["text"], bg=C["surface"]).pack(side="left")
        if count:
            tk.Label(h, text=" %d " % count, font=font(9, True), fg=C["white"], bg=color or C["chip"]).pack(
                side="left", padx=px(8))

    def rebuild(self, active, done):
        inner = self.area.inner
        for w in inner.winfo_children():
            w.destroy()
        self.cards = {}
        t = now()
        due_n = sum(1 for i in active if i["due"] <= t)
        self.section(inner, "Ждут перезвона", len(active), C["red"] if due_n else C["accent"])
        if not active:
            box = tk.Frame(inner, bg=C["surface"])
            box.pack(fill="x", pady=px(14))
            tk.Label(box, text=I["phone"], font=ifont(22), fg=C["faint"], bg=C["surface"]).pack()
            tk.Label(box, text="Пока никого не нужно набирать", font=font(10, True), fg=C["muted"],
                     bg=C["surface"]).pack(pady=(px(6), 0))
            tk.Label(box, text="Заполните форму выше — напомню вовремя", font=font(9), fg=C["faint"],
                     bg=C["surface"]).pack()
        for it in active:
            self.cards[it["id"]] = self.card(inner, it, hist=False)
        self.section(inner, "Перезвонили сегодня", len(done), C["green"])
        if not done:
            tk.Label(inner, text="Пока никого", font=font(9), fg=C["faint"], bg=C["surface"]).pack(anchor="w")
        for it in done:
            self.cards[it["id"]] = self.card(inner, it, hist=True)
        tk.Frame(inner, bg=C["surface"], height=px(10)).pack()

    def card(self, parent, it, hist):
        wrap = tk.Frame(parent, bg=C["surface"])
        wrap.pack(fill="x", pady=(0, px(8)), padx=(0, px(8)))
        bar = tk.Frame(wrap, bg=C["accent"], width=px(4))
        bar.pack(side="left", fill="y")
        c = tk.Frame(wrap, bg=C["card"])
        c.pack(side="left", fill="both", expand=True)
        r1 = tk.Frame(c, bg=C["card"])
        r1.pack(fill="x", padx=px(12), pady=(px(9), 0))
        tk.Label(r1, text=it["fio"] or "Без имени", font=font(10, True), fg=C["text"] if it["fio"] else C["muted"],
                 bg=C["card"], anchor="w").pack(side="left")
        clock = tk.Label(r1, text="", font=font(11, True), fg=C["text"], bg=C["card"])
        clock.pack(side="right")
        r2 = tk.Frame(c, bg=C["card"])
        r2.pack(fill="x", padx=px(12), pady=(px(1), 0))
        ph = tk.Label(r2, text=pc.phone_display(it["phone"]), font=font(11), fg=C["accent_h"], bg=C["card"],
                      cursor="hand2")
        ph.pack(side="left")
        ph.bind("<Button-1>", lambda e, i=it: self.copy_phone(i))
        ui.Tooltip(ph, "Нажмите — номер скопируется")
        left = tk.Label(r2, text="", font=font(9), fg=C["muted"], bg=C["card"])
        left.pack(side="right")
        if it.get("note"):
            tk.Label(c, text=it["note"], font=font(9), fg=C["muted"], bg=C["card"], anchor="w", justify="left",
                     wraplength=px(340)).pack(fill="x", padx=px(12), pady=(px(3), 0))
        r3 = tk.Frame(c, bg=C["card"])
        r3.pack(fill="x", padx=px(6), pady=(px(4), px(6)))
        meta = "Звонок %s" % pc.fmt_at(it["created"], now())
        if it.get("attempts"):
            meta += " · не дозвонились: %d" % it["attempts"]
        tk.Label(r3, text=meta, font=font(8), fg=C["faint"], bg=C["card"]).pack(side="left", padx=px(6))
        mk = lambda icon, cmd, tip, fg=None: ui.IconBtn(r3, icon, cmd, bg=C["card"], fg=fg, size=10, tooltip=tip, pad=6)
        if hist:
            mk("undo", lambda: self.restore(it["id"]), "Вернуть в ожидающие").pack(side="right")
            mk("copy", lambda: self.copy_phone(it), "Скопировать номер").pack(side="right")
        else:
            mk("check", lambda: self.done(it["id"]), "Перезвонил", C["green"]).pack(side="right")
            mk("delete", lambda: self.delete(it["id"]), "Удалить").pack(side="right")
            mk("edit", lambda: self.start_edit(it["id"]), "Изменить").pack(side="right")
            sn = mk("snooze", None, "Отложить")
            sn.command = lambda w=sn: self.snooze_menu(it["id"], w)
            sn.pack(side="right")
            mk("copy", lambda: self.copy_phone(it), "Скопировать номер").pack(side="right")
        return {"bar": bar, "clock": clock, "left": left, "it": it}

    def tick_cards(self, t):
        for iid, w in self.cards.items():
            it = self.app.store.items.get(iid)
            if not it:
                continue
            try:
                if it["status"] == "done":
                    w["bar"].configure(bg=C["green"])
                    w["clock"].configure(text=pc.fmt_clock(it.get("done_at") or t, t), fg=C["green"])
                    w["left"].configure(text="перезвонили", fg=C["green"])
                    continue
                u = pc.urgency(it["due"], t)
                col = ui.URG[u]
                w["bar"].configure(bg=col)
                w["clock"].configure(text=pc.fmt_clock(it["due"], t), fg=C["text"] if u == "later" else col)
                w["left"].configure(text=pc.fmt_left(it["due"], t), fg=C["muted"] if u == "later" else col)
            except tk.TclError:
                pass

    # ── «История»: по датам и времени
    def history_items(self):
        q = self.h_search.get().strip().lower()
        qd = pc.phone_digits(q)
        res = []
        for it in self.app.store.items.values():
            if it.get("status") == "deleted":
                continue
            if q:
                hay = " ".join(str(it.get(k) or "") for k in ("fio", "phone", "note")).lower()
                if q not in hay and not (len(qd) >= 3 and qd in pc.phone_digits(it.get("phone"))):
                    continue
            res.append(it)
        res.sort(key=lambda i: -i["due"])
        return res

    def refresh_history(self, force=False):
        items = self.history_items()
        sig = (self.h_search.get(), dt.date.today(), tuple(
            (i["id"], i["fio"], i["phone"], i.get("note"), i["due"], i["status"], i.get("done_at"),
             pc.urgency(i["due"], now(), i["status"]) == "missed") for i in items))
        if not force and sig == self.hist_sig:
            return
        self.hist_sig = sig
        inner = self.h_area.inner
        for w in inner.winfo_children():
            w.destroy()
        if not items:
            tk.Label(inner, text="Ничего не найдено" if self.h_search.get().strip() else "История пока пустая",
                     font=font(10, True), fg=C["muted"], bg=C["surface"]).pack(pady=px(30))
            return
        t = now()
        today = dt.date.today()
        cur = None
        shown = 0
        for it in items:
            d = dt.date.fromtimestamp(it["due"])
            if d != cur:
                cur = d
                day = [i for i in items if dt.date.fromtimestamp(i["due"]) == d]
                done_n = sum(1 for i in day if i["status"] == "done")
                h = tk.Frame(inner, bg=C["surface"])
                h.pack(fill="x", pady=(px(12), px(4)), padx=(0, px(8)))
                tk.Label(h, text=day_title(d, today), font=font(10, True), fg=C["text"], bg=C["surface"]).pack(side="left")
                tk.Label(h, text="%d · перезвонили %d" % (len(day), done_n), font=font(8), fg=C["faint"],
                         bg=C["surface"]).pack(side="right")
            self.hist_row(inner, it, t)
            shown += 1
            if shown >= 400:
                tk.Label(inner, text="Показаны последние 400 — уточните поиск", font=font(9), fg=C["faint"],
                         bg=C["surface"]).pack(pady=px(10))
                break

    def hist_row(self, parent, it, t):
        u = pc.urgency(it["due"], t, it["status"])
        if it["status"] == "done":
            st, col = "перезвонили %s" % pc.fmt_at(it.get("done_at") or it["due"], it["due"]), C["green"]
        elif u == "missed":
            st, col = "пропущен", C["red"]
        elif u == "due":
            st, col = "пора звонить", C["amber"]
        else:
            st, col = "ждёт", C["accent_h"]
        row = tk.Frame(parent, bg=C["card"])
        row.pack(fill="x", pady=(0, px(4)), padx=(0, px(8)))
        tk.Frame(row, bg=col, width=px(3)).pack(side="left", fill="y")
        tk.Label(row, text=dt.datetime.fromtimestamp(it["due"]).strftime("%H:%M"), font=font(11, True),
                 fg=C["text"], bg=C["card"], width=5).pack(side="left", padx=(px(8), px(4)), pady=px(6), anchor="n")
        mid = tk.Frame(row, bg=C["card"])
        mid.pack(side="left", fill="x", expand=True, pady=px(5))
        top = tk.Frame(mid, bg=C["card"])
        top.pack(fill="x")
        tk.Label(top, text=it["fio"] or "Без имени", font=font(10, True), fg=C["text"] if it["fio"] else C["muted"],
                 bg=C["card"]).pack(side="left")
        tk.Label(top, text=st, font=font(9), fg=col, bg=C["card"]).pack(side="right", padx=px(10))
        line = tk.Frame(mid, bg=C["card"])
        line.pack(fill="x")
        ph = tk.Label(line, text=pc.phone_display(it["phone"]), font=font(10), fg=C["accent_h"], bg=C["card"],
                      cursor="hand2")
        ph.pack(side="left")
        ph.bind("<Button-1>", lambda e, i=it: self.copy_phone(i))
        meta = "звонок %s" % pc.fmt_at(it["created"], it["due"])
        if it.get("attempts"):
            meta += " · не дозвонились %d" % it["attempts"]
        tk.Label(line, text="  " + meta, font=font(8), fg=C["faint"], bg=C["card"]).pack(side="left")
        if it.get("note"):
            tk.Label(mid, text=it["note"], font=font(9), fg=C["muted"], bg=C["card"], anchor="w", justify="left",
                     wraplength=px(330)).pack(fill="x")

    # ── действия
    def copy_phone(self, it):
        num = pc.phone_for_copy(it["phone"], self.app.settings["copy_fmt"])
        ui.copy_text(self.app.root, num)
        if self.tab == "today":
            self.flash("Номер скопирован: " + num, C["accent_h"])
        else:
            self.app.set_status_note("Скопировано: " + num)

    def snooze_menu(self, iid, w):
        m = tk.Menu(self.win, tearoff=0, bg=C["card"], fg=C["text"], activebackground=C["accent_d"],
                    activeforeground=C["white"], bd=0, font=font(10))
        for label, sec in (("на 5 минут", 300), ("на 15 минут", 900), ("на 30 минут", 1800), ("на 1 час", 3600)):
            m.add_command(label="Отложить " + label, command=lambda s=sec: self.app.snooze(iid, s))
        m.tk_popup(w.winfo_rootx(), w.winfo_rooty() + w.winfo_height())

    def done(self, iid):
        self.app.done(iid)

    def delete(self, iid):
        it = self.app.store.items.get(iid)
        self.app.store.delete(iid)
        self.app.reminders.close(iid)
        self.app.changed()
        self.flash_undo("Удалено: %s" % (it["fio"] or pc.phone_display(it["phone"])),
                        lambda: (self.app.store.restore(iid), self.app.changed()))

    def restore(self, iid):
        self.app.store.restore(iid)
        self.app.changed()

    def set_banner(self, text, button=None, cmd=None, color=None):
        for w in self.banner.winfo_children():
            w.destroy()
        if not text:
            return
        b = tk.Frame(self.banner, bg=color or C["card"])
        b.pack(fill="x", padx=px(18), pady=(px(4), px(4)))
        tk.Label(b, text=I["warning"], font=ifont(11), fg=C["amber"], bg=b["bg"]).pack(side="left", padx=(px(10), 0))
        tk.Label(b, text=text, font=font(9), fg=C["text"], bg=b["bg"], wraplength=px(270), justify="left").pack(
            side="left", padx=px(8), pady=px(8))
        if button:
            ui.Btn(b, button, command=cmd, bg=C["accent"], fg=C["white"], size=9, padx=10, pady=4).pack(
                side="right", padx=px(8))


# ═══════════════════════════════ иконка у края экрана ═══════════════════════════════

class Tab:
    """Маленькая иконка у края или в углу экрана — в неё сворачивается окно. Клик — открыть окно.
    Перетаскивается и прилипает к ближайшему краю/углу. Показывает, сколько ждут и сколько до ближайшего."""

    def __init__(self, app):
        self.app = app
        self.win = tk.Toplevel(app.root)
        self.win.withdraw()
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.configure(bg=C["accent"])
        self.cv = tk.Canvas(self.win, highlightthickness=0, bd=0, bg=C["accent"], cursor="hand2")
        self.cv.pack(fill="both", expand=True)
        self.hover = False
        self.drag = None
        self.pulse = False
        self.shown = False
        for ev, fn in (("<ButtonPress-1>", self.press), ("<B1-Motion>", self.motion),
                       ("<ButtonRelease-1>", self.release), ("<Enter>", self.enter), ("<Leave>", self.leave),
                       ("<Button-3>", self.menu)):
            self.cv.bind(ev, fn)
        self.win.update_idletasks()
        ui.tool_window(self.win)
        ui.no_activate(self.win)
        ui.Tooltip(self.cv, lambda: "Перезвон — нажмите, чтобы открыть\nПеретащите к любому краю или углу\n"
                                    "%s — открыть с клавиатуры" % HOTKEY_TEXT)

    # ── показ
    def show(self):
        self.place()
        self.win.deiconify()
        ui.raise_top(self.win)
        self.shown = True
        self.redraw()

    def hide(self):
        self.win.withdraw()
        self.shown = False

    # ── геометрия
    def size(self, edge):
        if edge in ("left", "right"):
            return px(46), px(128)
        if edge in ("top", "bottom"):
            return px(160), px(44)
        return px(62), px(62)

    def area(self):
        d = self.app.settings["dock"] or {}
        if d.get("mx") is None:
            return ui.work_area(self.win.winfo_screenwidth() // 2, self.win.winfo_screenheight() // 2)
        return ui.work_area(d["mx"], d["my"])

    def geometry_for(self, edge, pos, area):
        l, t, r, b = area
        w, h = self.size(edge)
        pos = max(0.0, min(1.0, pos))
        if edge == "left":
            x, y = l, t + pos * (b - t - h)
        elif edge == "right":
            x, y = r - w, t + pos * (b - t - h)
        elif edge == "top":
            x, y = l + pos * (r - l - w), t
        elif edge == "bottom":
            x, y = l + pos * (r - l - w), b - h
        else:
            x = l if edge[1] == "l" else r - w
            y = t if edge[0] == "t" else b - h
        return int(w), int(h), int(x), int(y)

    def place(self):
        d = self.app.settings["dock"] or {}
        w, h, x, y = self.geometry_for(d.get("edge", "right"), d.get("pos", 0.32), self.area())
        self.win.geometry("%dx%d+%d+%d" % (w, h, x, y))
        self.win.update_idletasks()

    @staticmethod
    def snap(cx, cy, area, thr):
        """Куда прилипнуть: ближайший край, а если рядом два края — угол. Возвращает (edge, pos)."""
        l, t, r, b = area
        dl, dr, dtop, db = cx - l, r - cx, cy - t, b - cy
        if min(dl, dr) < thr and min(dtop, db) < thr:
            return ("t" if dtop < db else "b") + ("l" if dl < dr else "r"), 0.0
        m = min(dl, dr, dtop, db)
        if m in (dl, dr):
            return ("left" if m == dl else "right"), (cy - t) / max(1, b - t)
        return ("top" if m == dtop else "bottom"), (cx - l) / max(1, r - l)

    # ── мышь
    def press(self, e):
        self.drag = {"x": e.x_root, "y": e.y_root, "wx": self.win.winfo_x(), "wy": self.win.winfo_y(), "moved": False}

    def motion(self, e):
        if not self.drag:
            return
        dx, dy = e.x_root - self.drag["x"], e.y_root - self.drag["y"]
        if not self.drag["moved"] and abs(dx) + abs(dy) < px(6):
            return
        if not self.drag["moved"]:
            self.drag["moved"] = True
            w, h = px(62), px(62)
            self.win.geometry("%dx%d" % (w, h))
            self.drag["wx"], self.drag["wy"] = e.x_root - w // 2, e.y_root - h // 2
            self.drag["x"], self.drag["y"] = e.x_root, e.y_root
            self.cv.delete("all")
            self.cv.configure(bg=C["accent_h"])
            self.cv.create_text(w // 2, h // 2, text=I["phone"], font=ifont(18), fill=C["white"])
        self.win.geometry("+%d+%d" % (self.drag["wx"] + dx, self.drag["wy"] + dy))

    def release(self, e):
        d = self.drag
        self.drag = None
        if not d:
            return
        if not d["moved"]:
            self.app.show_main()
            return
        area = ui.work_area(e.x_root, e.y_root)
        edge, pos = self.snap(e.x_root, e.y_root, area, px(140))
        self.app.settings["dock"] = {"edge": edge, "pos": pos, "mx": e.x_root, "my": e.y_root}
        self.place()
        self.redraw()

    def enter(self, _e):
        self.hover = True
        self.redraw()

    def leave(self, _e):
        self.hover = False
        self.redraw()

    def menu(self, e):
        m = tk.Menu(self.win, tearoff=0, bg=C["card"], fg=C["text"], activebackground=C["accent_d"],
                    activeforeground=C["white"], bd=0, font=font(10))
        m.add_command(label="Открыть      " + HOTKEY_TEXT, command=self.app.show_main)
        m.add_command(label="Настройки", command=self.app.open_settings)
        m.add_separator()
        m.add_command(label="Закрыть программу", command=self.app.quit)
        m.tk_popup(e.x_root, e.y_root)

    # ── рисование
    def redraw(self):
        if not self.shown or (self.drag and self.drag.get("moved")):
            return
        app = self.app
        act = app.store.active()
        t = now()
        nxt = act[0] if act else None
        urg = pc.urgency(nxt["due"], t) if nxt else "none"
        overdue = urg in ("due", "missed")
        base = {"due": C["red"], "missed": C["red"], "soon": C["amber"]}.get(urg, C["accent"])
        if overdue and self.pulse:
            base = C["red_h"]
        if self.hover:
            base = ui.blend(base, "#FFFFFF", 0.12)
        cv = self.cv
        cv.delete("all")
        cv.configure(bg=base)
        self.win.configure(bg=base)
        edge = (app.settings["dock"] or {}).get("edge", "right")
        w, h = self.size(edge)
        n = len(act)
        icon = I["ringer"] if overdue else I["phone"]
        left = ""
        if nxt:
            sec = nxt["due"] - t
            if sec <= 0:
                left = "сейчас"
            elif sec < 3600:
                left = "%d:%02d" % (sec // 60, sec % 60)
            else:
                left = "%dч" % (sec // 3600) if sec < 86400 else "%dд" % (sec // 86400)
        fg = C["white"]
        shade = ui.blend(base, "#000000", 0.25)
        if edge in ("left", "right"):
            cv.create_text(w // 2, px(22), text=icon, font=ifont(15), fill=fg)
            if n:
                cv.create_text(w // 2, px(58), text=str(n), font=font(16, True), fill=fg)
                cv.create_text(w // 2, px(84), text="ждут" if not overdue else "пора!", font=font(7, True), fill=fg)
                cv.create_text(w // 2, px(108), text=left, font=font(8, True), fill=fg)
            else:
                cv.create_text(w // 2, px(78), text="ПЕРЕЗВОН", font=font(7, True), fill=fg, angle=90)
            x = 0 if edge == "right" else w - 1
            cv.create_line(x, 0, x, h, fill=shade)
        elif edge in ("top", "bottom"):
            cv.create_text(px(22), h // 2, text=icon, font=ifont(14), fill=fg)
            if n:
                cv.create_text(px(44), h // 2, text=str(n), font=font(14, True), fill=fg, anchor="w")
                cv.create_text(w - px(12), h // 2, text=left, font=font(9, True), fill=fg, anchor="e")
            else:
                cv.create_text(px(44), h // 2, text="Перезвон", font=font(10, True), fill=fg, anchor="w")
        else:
            cv.create_text(w // 2, px(24) if n else h // 2, text=icon, font=ifont(17), fill=fg)
            if n:
                cv.create_text(w // 2, px(46), text="%d · %s" % (n, left) if left and len(left) <= 5 else str(n),
                               font=font(8, True), fill=fg)
        if app.sync.need_login or (app.sync.error and app.settings["token"]):
            cv.create_oval(px(4), px(4), px(10), px(10), fill=C["amber"], outline="")

    def keep_on_top(self):
        if self.shown and not self.drag:
            ui.raise_top(self.win)


# ═══════════════════════════════ напоминания ═══════════════════════════════

class Reminder:
    W = 420

    def __init__(self, mgr, it):
        self.mgr = mgr
        self.app = mgr.app
        self.id = it["id"]
        self.win = tk.Toplevel(self.app.root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.configure(bg=C["red"])
        self.pulse = False
        self.build(it)
        self.win.update_idletasks()
        ui.tool_window(self.win)
        ui.no_activate(self.win)
        ui.fade_in(self.win)
        self.last_chime = 0

    def build(self, it):
        self.bar = tk.Frame(self.win, bg=C["red"], height=px(5))
        self.bar.pack(fill="x")
        b = tk.Frame(self.win, bg=C["surface"])
        b.pack(fill="both", expand=True, padx=1, pady=(0, 1))
        p = tk.Frame(b, bg=C["surface"])
        p.pack(fill="both", expand=True, padx=px(18), pady=(px(12), px(14)))
        h = tk.Frame(p, bg=C["surface"])
        h.pack(fill="x")
        tk.Label(h, text=I["ringer"], font=ifont(13), fg=C["red"], bg=C["surface"]).pack(side="left")
        tk.Label(h, text="ПОРА ПЕРЕЗВОНИТЬ", font=font(10, True), fg=C["red"], bg=C["surface"]).pack(
            side="left", padx=(px(8), 0))
        self.when = tk.Label(h, text="", font=font(9), fg=C["muted"], bg=C["surface"])
        self.when.pack(side="right")

        tk.Label(p, text=it["fio"] or "Без имени", font=font(16, True), fg=C["text"] if it["fio"] else C["muted"],
                 bg=C["surface"], anchor="w", wraplength=px(380), justify="left").pack(fill="x", pady=(px(10), 0))
        ph = tk.Frame(p, bg=C["surface"])
        ph.pack(fill="x", pady=(px(4), 0))
        num = tk.Label(ph, text=pc.phone_display(it["phone"]), font=font(19, True), fg=C["accent_h"], bg=C["surface"],
                       cursor="hand2")
        num.pack(side="left")
        num.bind("<Button-1>", lambda e: self.copy())
        self.copy_btn = ui.Btn(ph, "Копировать", icon="copy", command=self.copy, bg=C["card"], size=9, padx=10, pady=6)
        self.copy_btn.pack(side="right")
        if it.get("note"):
            n = tk.Frame(p, bg=C["card"])
            n.pack(fill="x", pady=(px(8), 0))
            tk.Label(n, text=I["note"], font=ifont(10), fg=C["muted"], bg=C["card"]).pack(side="left", anchor="n",
                                                                                     padx=(px(10), 0), pady=px(8))
            tk.Label(n, text=it["note"], font=font(10), fg=C["text"], bg=C["card"], wraplength=px(330),
                     justify="left", anchor="w").pack(side="left", fill="x", padx=px(8), pady=px(7))
        meta = "Звонок был %s · обещали перезвонить %s" % (pc.fmt_at(it["created"]), pc.fmt_at(it["due"]))
        if it.get("attempts"):
            meta += "\nУже не дозвонились: %d раз" % it["attempts"]
        tk.Label(p, text=meta, font=font(8), fg=C["faint"], bg=C["surface"], anchor="w", justify="left").pack(
            fill="x", pady=(px(8), 0))

        acts = tk.Frame(p, bg=C["surface"])
        acts.pack(fill="x", pady=(px(12), 0))
        ui.Btn(acts, "Перезвонил", icon="check", command=self.done, bg=C["green"], hover=C["green_h"],
               fg=C["white"], size=11, pady=9).pack(side="left", fill="x", expand=True)
        ui.Btn(acts, "Не дозвонился", icon="close", command=self.no_answer, bg=C["card"], size=10, pady=9,
               tooltip="Напомню ещё раз через 15 минут").pack(side="left", padx=(px(8), 0))
        sn = tk.Frame(p, bg=C["surface"])
        sn.pack(fill="x", pady=(px(10), 0))
        tk.Label(sn, text="Отложить:", font=font(9), fg=C["muted"], bg=C["surface"]).pack(side="left")
        for label, sec in (("5 мин", 300), ("15 мин", 900), ("30 мин", 1800), ("1 ч", 3600)):
            ui.Chip(sn, label, lambda s=sec: self.snooze(s)).pack(side="left", padx=(px(6), 0))

    def copy(self):
        it = self.app.store.items.get(self.id)
        if not it:
            return
        ui.copy_text(self.app.root, pc.phone_for_copy(it["phone"], self.app.settings["copy_fmt"]))
        self.copy_btn.set_text("Скопировано")
        self.copy_btn.set_colors(C["green"], fg=C["white"])
        self.win.after(1600, lambda: (self.copy_btn.set_text("Копировать"), self.copy_btn.set_colors(C["card"], fg=C["text"])))

    def done(self):
        self.app.done(self.id)

    def no_answer(self):
        self.app.snooze(self.id, 900, attempt=True)

    def snooze(self, sec):
        self.app.snooze(self.id, sec)

    def tick(self, t):
        it = self.app.store.items.get(self.id)
        if not it:
            return
        late = t - it["due"]
        self.when.configure(text="обещали в %s" % pc.fmt_clock(it["due"], t) if late < 60 else
                            "опоздание %s" % pc.fmt_duration(late), fg=C["muted"] if late < 60 else C["red"])
        self.pulse = not self.pulse
        col = C["red"] if self.pulse else C["amber"]
        self.bar.configure(bg=col)
        self.win.configure(bg=col)


class Reminders:
    MAX = 4

    def __init__(self, app):
        self.app = app
        self.open = {}

    def tick(self, t):
        due = self.app.store.due_items(t)
        ids = {i["id"] for i in due}
        for iid in list(self.open):
            if iid not in ids:
                self.close(iid)
        new = False
        for it in due:
            if it["id"] not in self.open and len(self.open) < self.MAX:
                self.open[it["id"]] = Reminder(self, it)
                new = True
        if new:
            self.layout()
            ui.flash_taskbar(self.app.root)
        chime_due = False
        for r in self.open.values():
            r.tick(t)
            if t - r.last_chime > 120:
                chime_due = True
                r.last_chime = t
        if chime_due and self.app.settings["sound"]:
            ui.play_chime()

    def layout(self):
        """Снизу справа над панелью задач, стопкой вверх; не влезло по высоте — следующий столбец левее."""
        root = self.app.root
        if root.state() == "normal":
            cx, cy = root.winfo_rootx() + root.winfo_width() // 2, root.winfo_rooty() + 10
        else:                                       # свёрнуто в иконку — тот монитор, где иконка
            l, top, r, b = self.app.tab.area()
            cx, cy = (l + r) // 2, (top + b) // 2
        l, top, r, b = ui.work_area(cx, cy)
        gap = px(10)
        w = px(Reminder.W)
        x = r - w - px(12)
        y = b - px(12)
        for rem in self.open.values():
            rem.win.update_idletasks()
            h = rem.win.winfo_reqheight()
            if y - h < top + gap and y < b - px(12):    # столбец заполнен
                x -= w + gap
                y = b - px(12)
            y -= h
            # «-topmost» здесь не трогаем: в Tk он, вызванный до применения geometry, сбрасывает окно в (0,0)
            rem.win.geometry("%dx%d+%d+%d" % (w, h, max(l, x), max(top, y)))
            rem.win.update_idletasks()
            y -= gap

    def close(self, iid):
        r = self.open.pop(iid, None)
        if r:
            try:
                r.win.destroy()
            except tk.TclError:
                pass
            self.layout()

    def keep_on_top(self):
        for r in self.open.values():
            ui.raise_top(r.win)


# ═══════════════════════════════ окна входа и настроек ═══════════════════════════════

class Dialog(tk.Toplevel):
    def __init__(self, app, title, width=440):
        super().__init__(app.root)
        self.app = app
        self.title(title)
        self.configure(bg=C["surface"])
        self.resizable(False, False)
        self.attributes("-topmost", True)
        ui.set_icon(self)
        self.body = tk.Frame(self, bg=C["surface"])
        self.body.pack(fill="both", expand=True, padx=px(26), pady=px(22))
        self.width = width
        self.protocol("WM_DELETE_WINDOW", self.destroy)

    def center(self):
        self.update_idletasks()
        w = max(px(self.width), self.winfo_reqwidth())
        h = self.winfo_reqheight()
        l, t, r, b = ui.work_area(self.winfo_screenwidth() // 2, self.winfo_screenheight() // 2)
        self.geometry("%dx%d+%d+%d" % (w, h, l + (r - l - w) // 2, t + (b - t - h) // 3))
        ui.dark_titlebar(self)
        ui.force_foreground(self)


class LoginDialog(Dialog):
    def __init__(self, app, reason=None):
        super().__init__(app, "Вход — Перезвон")
        b = self.body
        top = tk.Frame(b, bg=C["surface"])
        top.pack(fill="x")
        tk.Label(top, text=I["phone"], font=ifont(20), fg=C["white"], bg=C["accent"], width=2, pady=px(6)).pack(side="left")
        tt = tk.Frame(top, bg=C["surface"])
        tt.pack(side="left", padx=px(12))
        tk.Label(tt, text="Перезвон", font=font(16, True), fg=C["text"], bg=C["surface"]).pack(anchor="w")
        tk.Label(tt, text="напоминания перезвонить клиенту", font=font(9), fg=C["muted"], bg=C["surface"]).pack(anchor="w")
        if reason:
            tk.Label(b, text=reason, font=font(9, True), fg=C["amber"], bg=C["surface"], wraplength=px(380),
                     justify="left").pack(anchor="w", pady=(px(16), 0))
        self.form = LoginForm(b, "client", lambda: self.app.settings["server"], self.done)
        self.form.pack(fill="x", pady=(px(18), 0))
        later = tk.Label(b, text="Пока без входа — работать только на этом компьютере",
                         font=font(9, True), fg=C["accent_h"], bg=C["surface"], cursor="hand2")
        later.pack(anchor="w", pady=(px(12), 0))
        later.bind("<Button-1>", lambda e: self.skip())
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.center()
        self.form.focus()

    def done(self, res):
        s = self.app.settings
        s.set("token", res["token"])
        s.set("user", res["user"])
        s.set("local_mode", False)
        s.save()
        self.app.after_login()
        self.destroy()

    def close(self):
        if self.form.cancel:
            self.form.cancel.set()
        if not self.app.settings["token"]:
            self.app.settings["local_mode"] = True
        self.destroy()

    def skip(self):
        self.app.settings["local_mode"] = True
        self.app.after_login()
        self.close()


class SettingsDialog(Dialog):
    def __init__(self, app):
        super().__init__(app, "Настройки — Перезвон", width=480)
        self.build()
        self.center()

    def section(self, title):
        tk.Label(self.body, text=title, font=font(9, True), fg=C["muted"], bg=C["surface"]).pack(
            anchor="w", pady=(px(16), px(6)))
        f = tk.Frame(self.body, bg=C["card"])
        f.pack(fill="x")
        return f

    def row(self, parent, title, sub=None):
        r = tk.Frame(parent, bg=C["card"])
        r.pack(fill="x", padx=px(14), pady=px(10))
        t = tk.Frame(r, bg=C["card"])
        t.pack(side="left", fill="x", expand=True)
        tk.Label(t, text=title, font=font(10, True), fg=C["text"], bg=C["card"], anchor="w").pack(anchor="w")
        if sub:
            tk.Label(t, text=sub, font=font(8), fg=C["muted"], bg=C["card"], anchor="w", justify="left",
                     wraplength=px(270)).pack(anchor="w")
        return r

    def toggle(self, parent, value, on_change):
        state = {"v": value}
        b = ui.Btn(parent, "Выкл", command=None, bg=C["chip"], fg=C["white"], size=9, padx=10, pady=4)

        def paint():
            b.set_text("Вкл" if state["v"] else "Выкл")
            b.set_colors(C["green"] if state["v"] else C["chip"], fg=C["white"] if state["v"] else C["muted"])

        def click():
            state["v"] = not state["v"]
            paint()
            on_change(state["v"])
        b.command = click
        paint()
        return b

    def build(self):
        app, s = self.app, self.app.settings
        tk.Label(self.body, text="Настройки", font=font(15, True), fg=C["text"], bg=C["surface"]).pack(anchor="w")
        sec = self.section("УЧЁТНАЯ ЗАПИСЬ")
        u = s["user"]
        if s["token"] and u:
            r = self.row(sec, u["name"], "Комната %s · %s" % (u["room"], {"admin": "администратор", "superadmin": "супер-админ"}.get(u["role"], "оператор")))
            ui.Btn(r, "Выйти", icon="signout", command=lambda: (self.destroy(), self.app.logout()), bg=C["chip"],
                   size=9, padx=10, pady=4).pack(side="right")
            bot = s["bot"]
            self.row(sec, "Напоминания в Telegram",
                     "Приходят от бота%s, если вы нажали в нём «Старт». Выключить — в самом боте."
                     % ((" @" + bot) if bot else ""))
        else:
            r = self.row(sec, "Вход не выполнен", "Перезвоны хранятся только на этом компьютере.")
            ui.Btn(r, "Войти", command=lambda: (self.destroy(), app.open_login()), bg=C["accent"],
                   fg=C["white"], size=9, padx=10, pady=4).pack(side="right")

        sec = self.section("НАПОМИНАНИЯ")
        r = self.row(sec, "Звук", "Короткий сигнал, когда пора перезвонить (повтор раз в 2 минуты)")
        ui.Btn(r, "", icon="ringer", command=ui.play_chime, bg=C["chip"], size=9, padx=8, pady=4,
               tooltip="Прослушать").pack(side="right", padx=(px(6), 0))
        self.toggle(r, s["sound"], lambda v: s.__setitem__("sound", v)).pack(side="right")
        r = self.row(sec, "Копировать номер как", "Как вставлять в телефонную программу")
        chips = []
        for key, label in reversed(COPY_FORMATS):
            ch = ui.Chip(r, label, None)
            ch.key = key
            ch.command = lambda c=ch: (s.__setitem__("copy_fmt", c.key), [x.select(x.key == c.key) for x in chips])
            ch.select(s["copy_fmt"] == key)
            ch.pack(side="right", padx=(px(4), 0))
            chips.append(ch)

        sec = self.section("СИСТЕМА")
        r = self.row(sec, "Запускать вместе с Windows", "Перезвон сам запустится иконкой у края экрана")
        self.toggle(r, bool(ui.autostart_get(pc.APP_NAME)), app.set_autostart).pack(side="right")
        hk = "работает" if app.hotkey and app.hotkey.ok else ("занята другой программой" if app.hotkey else "выключена")
        self.row(sec, "Горячая клавиша: " + HOTKEY_TEXT, "Открыть окно из любой программы (%s). "
                 "В полях работают Ctrl+C, V, X, A, Z, Y в любой раскладке" % hk)
        r = self.row(sec, "Сервер", None)
        self.srv = tk.Entry(r, font=font(9), bg=C["input"], fg=C["text"], insertbackground=C["text"], relief="flat",
                            width=30)
        self.srv.insert(0, s["server"])
        self.srv.pack(side="right", ipady=px(4))
        self.srv.bind("<FocusOut>", lambda e: self.save_server())
        self.srv.bind("<Return>", lambda e: self.save_server())
        r = self.row(sec, "Закрыть программу", "Крестик окна только сворачивает его в иконку у края экрана, чтобы "
                                              "напоминания приходили. Если закрыть — напоминаний не будет до "
                                              "следующего запуска.")
        ui.Btn(r, "Закрыть", command=self.app.quit, bg=C["chip"], fg=C["red"], size=9, padx=10, pady=4).pack(side="right")
        up = self.app.updater
        r = self.row(sec, "Версия %s" % pc.APP_VERSION, "Обновления: " + up.status)
        self.up_sub = r.winfo_children()[0].winfo_children()[1]
        self.up_btn = ui.Btn(r, "Проверить", icon="refresh", command=self.check_updates, bg=C["chip"], size=9,
                             padx=10, pady=4)
        self.up_btn.pack(side="right")
        self.after(500, self.poll_updates)

    def check_updates(self):
        up = self.app.updater
        if up.ready:
            self.app.apply_update(show_after=True)
            return
        if not up.enabled():
            up.status = "работают только в собранной программе (exe)"
        else:
            up.status = "проверяю…"
            up.check_now()

    def poll_updates(self):
        if not self.winfo_exists():
            return
        up = self.app.updater
        self.up_sub.configure(text="Обновления: " + up.status)
        if up.ready:
            self.up_btn.set_text("Обновить сейчас")
        self.after(500, self.poll_updates)

    def save_server(self):
        v = self.srv.get().strip().rstrip("/")
        if v and v != self.app.settings["server"]:
            self.app.settings["server"] = v
            self.app.sync.kick()


def _safe(fn):
    try:
        fn()
    except Exception:
        pass


# ═══════════════════════════════ приложение ═══════════════════════════════

class App:
    def __init__(self, home=None, minimized=False):
        self.home = home or pc.app_dir("Perezvon")
        sandbox = home or os.environ.get("PEREZVON_HOME")      # тесты: всё только в своей папке
        self.settings = Settings(os.path.join(self.home, "settings.json"),
                                 ui.Session(ui.session_dir("Perezvon", sandbox)))
        self.store = pc.Store(os.path.join(self.home, "items.json"))
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title("Перезвон")
        ui.init_scale(self.root)
        ui.apply_ttk_theme(self.root)
        ui.install_keyboard(self.root)
        ui.set_icon(self.root)
        self.root.report_callback_exception = self.on_error
        self.sync = Sync(self)
        self.reminders = Reminders(self)
        self.main = MainWindow(self)
        self.tab = Tab(self)
        self.root.bind("<Unmap>", self.on_unmap, add="+")
        self.updater = pz_update.Updater("client", pc.APP_VERSION, lambda: self.settings["server"],
                                         self.home if sandbox else install_dir(), log=_install_log)
        self.hotkey = None
        self.settings_win = None
        self.login_win = None
        self.status_note = None
        self.last_title = None
        self.refresh_header()
        self.refresh_status()
        u = self.settings["user"] or {}
        if self.settings["token"] and u.get("id") is not None:
            self.store.adopt(u["id"])          # починка списков, оставшихся от прошлого пользователя
        self.main.place_initial()
        if minimized:
            self.collapse()                    # автозапуск: сразу иконкой у края, окно не выскакивает
        else:
            self.root.deiconify()
        ui.dark_titlebar(self.root)

    # ── жизненный цикл
    def start(self, hotkey=True):
        if hotkey:
            self.hotkey = ui.GlobalHotkey(HOTKEY_VK, lambda: self.root.after(0, self.hotkey_pressed))
        if not self.settings["token"] and not self.settings["local_mode"]:
            self.root.after(300, self.open_login)
        self.loop_fast()
        self.loop_second()
        self.loop_slow()

    def on_error(self, exc, val, tb):
        import traceback
        try:
            with open(os.path.join(self.home, "errors.log"), "a", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + "".join(traceback.format_exception(exc, val, tb)) + "\n")
        except OSError:
            pass

    def loop_fast(self):
        self.sync.tick()
        self.root.after(150, self.loop_fast)

    def loop_second(self):
        t = now()
        self.reminders.tick(t)
        self.tab.pulse = not self.tab.pulse
        self.tab.redraw()
        if self.main.visible():
            self.main.refresh()
            if self.sync.last_ok:
                self.refresh_status()
        self.update_title(t)
        self.root.after(1000 - int((time.time() % 1) * 1000) + 5, self.loop_second)

    def loop_slow(self):
        self.tab.keep_on_top()
        self.reminders.keep_on_top()
        self.updater.tick()
        if self.updater.ready and self.update_is_safe():
            self.apply_update()
            return
        self.root.after(3000, self.loop_slow)

    def update_is_safe(self):
        """Ставим обновление незаметно: окно свёрнуто в иконку, напоминаний нет, форма пустая, диалоги закрыты."""
        m = self.main
        busy_form = any(f.get().strip() for f in (m.f_fio, m.f_phone, m.f_note)) or m.edit_id
        dialogs = any(w is not None and w.winfo_exists() for w in (self.settings_win, self.login_win))
        return not m.visible() and not self.reminders.open and not busy_form and not dialogs

    def apply_update(self, show_after=False):
        self.store.save()
        self.settings.save()
        args = ["--updated"] + ([] if show_after else ["--autostart"])
        if self.updater.apply(sys.executable, args):
            self.quit()

    def update_title(self, t=None):
        """Подпись кнопки на панели задач: сразу видно, сколько ждут и есть ли «пора»."""
        t = now() if t is None else t
        act = self.store.active()
        due = sum(1 for i in act if i["due"] <= t)
        if due:
            title = "Перезвон — пора звонить: %d" % due
        elif act:
            title = "Перезвон — ждут: %d · ближайший в %s" % (len(act), pc.fmt_clock(act[0]["due"], t))
        else:
            title = "Перезвон"
        if title != self.last_title:
            self.last_title = title
            self.root.title(title)

    def quit(self):
        try:
            self.store.save()
            self.main.save_geom()
            self.settings.save()
        finally:
            self.root.quit()
            self.root.destroy()

    # ── действия
    def hotkey_pressed(self):
        if self.main.visible() and ui.foreground_is_ours():
            self.collapse()
        else:
            self.show_main()

    def show_main(self):
        self.tab.hide()
        self.main.show(focus=True)

    def collapse(self):
        """Окно → маленькая иконка у края экрана (без кнопки на панели задач)."""
        self.main.save_geom()
        self.root.withdraw()
        self.tab.show()

    def on_unmap(self, e):
        # кнопка «свернуть» в заголовке окна тоже сворачивает в иконку, а не в панель задач
        if e.widget is self.root and self.root.state() == "iconic":
            self.root.after_idle(self.collapse)

    def changed(self):
        self.sync.kick()
        self.main.refresh(force=True)
        self.reminders.tick(now())
        self.update_title()
        self.tab.redraw()

    def done(self, iid):
        it = self.store.items.get(iid)
        self.store.done(iid)
        self.reminders.close(iid)
        self.changed()
        if it:
            self.main.flash_undo("Перезвонили: %s" % (it["fio"] or pc.phone_display(it["phone"])),
                                 lambda: (self.store.restore(iid), self.changed()))

    def snooze(self, iid, sec, attempt=False):
        self.store.snooze(iid, sec, attempt=attempt)
        self.reminders.close(iid)
        self.changed()
        it = self.store.items.get(iid)
        if it:
            self.main.flash("Напомню в %s" % pc.fmt_clock(it["due"]), C["accent_h"])

    def on_external_change(self, ids):
        for iid in ids:
            it = self.store.items.get(iid)
            if not it or it["status"] != "active":
                self.reminders.close(iid)
        self.main.refresh(force=True)
        self.update_title()

    def set_autostart(self, on):
        if not on:
            ui.autostart_set(pc.APP_NAME, None)
        elif getattr(sys, "frozen", False):
            ui.autostart_set(pc.APP_NAME, '"%s" --autostart' % sys.executable)
        else:
            ui.autostart_set(pc.APP_NAME, '"%s" "%s" --autostart' % (
                sys.executable.replace("python.exe", "pythonw.exe"), os.path.abspath(__file__)))

    def open_settings(self):
        if self.settings_win and self.settings_win.winfo_exists():
            ui.force_foreground(self.settings_win)
            return
        self.settings_win = SettingsDialog(self)

    def open_login(self, reason=None):
        if self.login_win and self.login_win.winfo_exists():
            ui.force_foreground(self.login_win)
            return
        self.login_win = LoginDialog(self, reason)

    def adopt_user(self, user_id):
        if self.store.adopt(user_id):
            for iid in list(self.reminders.open):
                self.reminders.close(iid)
            self.main.refresh(force=True)
            self.update_title()

    def after_login(self):
        u = self.settings["user"] or {}
        self.adopt_user(u.get("id"))
        self.sync.need_login = False
        self.sync.error = None
        self.sync.next_at = 0
        self.refresh_header()
        self.refresh_status()

    def status_click(self):
        if not self.settings["token"]:
            self.open_login()
        else:
            self.sync.kick(0)

    def account_click(self):
        if self.settings["token"]:
            self.logout()
        else:
            self.open_login()

    def logout(self, ask=True):
        """Выход: дослать неотправленное, разлогинить устройство на сервере, убрать перезвоны с этого ПК."""
        from tkinter import messagebox
        s = self.settings
        tok = s["token"]
        u = s["user"] or {}
        if ask and not messagebox.askyesno(
                "Выйти — Перезвон", "Выйти из аккаунта %s?\n\nВаши перезвоны останутся на сервере и появятся снова, "
                "когда вы войдёте — на этом или любом другом компьютере.\nС этого компьютера они будут убраны."
                % (u.get("name") or ""), parent=self.root):
            return False
        if tok and self.store.dirty_count():
            self.root.configure(cursor="watch")
            self.root.update()
            try:
                api = pc.Api(s["server"], tok, timeout=6)
                body, pushed = self.store.build_push()
                self.store.apply_sync(pushed, api.call("POST", "/api/sync", body))
            except pc.ApiError:
                pass
            finally:
                self.root.configure(cursor="")
            n = self.store.dirty_count()
            if n and ask and not messagebox.askyesno(
                    "Нет связи — Перезвон", "Нет связи с сервером: %d перезвон(ов) ещё не отправлено и пропадёт, "
                    "если выйти сейчас.\n\nВсё равно выйти?" % n, icon="warning", default="no", parent=self.root):
                return False
        if tok:
            api = pc.Api(s["server"], tok, timeout=4)
            threading.Thread(target=lambda: _safe(lambda: api.call("POST", "/api/logout", {})), daemon=True).start()
        s.session.clear()
        s["local_mode"] = False
        self.store.clear()
        for iid in list(self.reminders.open):
            self.reminders.close(iid)
        self.sync = Sync(self)
        self.main.clear_form()
        self.refresh_header()
        self.refresh_status()
        self.main.refresh(force=True)
        self.update_title()
        self.open_login()
        return True

    def set_status_note(self, text):
        self.status_note = (text, now())
        self.refresh_status()

    # ── отрисовка статуса
    def refresh_header(self):
        u = self.settings["user"]
        if self.settings["token"] and u:
            txt = "%s · %s%s" % (u["name"], u["room"], " · админ" if u["role"] in pc.ADMIN_ROLES else "")
        else:
            txt = "без входа — только этот компьютер"
        self.main.sub.configure(text=txt)
        self.main.acc_btn.set_text("Выйти" if self.settings["token"] else "Войти")

    def refresh_status(self):
        s, sy, p = self.settings, self.sync, self.main
        if self.status_note and now() - self.status_note[1] < 3:
            p.status_dot.configure(fg=C["accent_h"])
            p.status.configure(text=self.status_note[0])
            return
        if not s["token"]:
            if sy.need_login:
                p.set_banner("Сервер просит войти заново. Перезвоны на этом ПК сохранены.", "Войти",
                             self.open_login)
            else:
                p.set_banner(None)
            p.status_dot.configure(fg=C["faint"])
            p.status.configure(text="Без сервера · войти")
            return
        p.set_banner(None)
        if sy.error:
            n = self.store.dirty_count()
            p.status_dot.configure(fg=C["amber"])
            p.status.configure(text="Нет связи с сервером%s — всё сохранено на этом ПК" %
                               (" (%d не отправлено)" % n if n else ""))
        elif sy.last_ok:
            p.status_dot.configure(fg=C["green"])
            ago = int(now() - sy.last_ok)
            p.status.configure(text="Синхронизировано" + (" %d с назад" % ago if ago > 15 else ""))
        else:
            p.status_dot.configure(fg=C["faint"])
            p.status.configure(text="Подключаюсь…")


# ═══════════════════════════════ установка и запуск ═══════════════════════════════

def install_dir():
    return os.path.join(os.environ.get("LOCALAPPDATA") or pc.app_dir(), "Perezvon")


def _sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _mutex_exists(name):
    import ctypes
    k32 = ctypes.windll.kernel32
    h = k32.OpenMutexW(0x00100000, False, "Local\\%s.mutex" % name)
    if h:
        k32.CloseHandle(h)
        return True
    return False


def _shortcut(lnk, target):
    ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%s');$s.TargetPath='%s';"
          "$s.WorkingDirectory='%s';$s.Description='Перезвон — напоминания перезвонить';$s.Save()"
          % (lnk.replace("'", "''"), target.replace("'", "''"), os.path.dirname(target).replace("'", "''")))
    try:
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                       creationflags=0x08000000, timeout=20)
    except Exception:
        pass


def _install_log(msg):
    try:
        folder = os.environ.get("PEREZVON_HOME") or install_dir()
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "install.log"), "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except OSError:
        pass


def self_install():
    """exe запущен не из папки установки → копируем себя в %LOCALAPPDATA%\\Perezvon, ярлыки, автозапуск.

    Возвращает True, если текущий процесс должен завершиться (запущена установленная копия).
    """
    if not getattr(sys, "frozen", False) or os.environ.get("PEREZVON_NOINSTALL"):
        return False
    target_dir = install_dir()
    target = os.path.join(target_dir, "Перезвон.exe")
    if os.path.normcase(os.path.abspath(sys.executable)) == os.path.normcase(os.path.abspath(target)):
        return False
    os.makedirs(target_dir, exist_ok=True)
    same = os.path.exists(target) and _sha(target) == _sha(sys.executable)
    if not same:
        if _mutex_exists(INSTANCE):                       # старая версия запущена — просим выйти
            ui.signal_instance(INSTANCE, "quit")
            for _ in range(50):
                time.sleep(0.2)
                if not _mutex_exists(INSTANCE):
                    break
            time.sleep(0.5)
        for attempt in range(20):
            try:
                shutil.copy2(sys.executable, target + ".new")
                os.replace(target + ".new", target)
                break
            except OSError:
                time.sleep(0.3)
        else:
            _install_log("не удалось заменить %s — работаю из %s" % (target, sys.executable))
            return False                                  # не вышло — работаем из текущего места
    want = '"%s" --autostart' % target
    if ui.autostart_get(pc.APP_NAME) != want:
        ui.autostart_set(pc.APP_NAME, want)
    programs = os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs")
    if os.path.isdir(programs):
        _shortcut(os.path.join(programs, "Перезвон.lnk"), target)
    desk = os.path.join(os.environ.get("USERPROFILE", ""), "Desktop")
    if os.path.isdir(desk):
        _shortcut(os.path.join(desk, "Перезвон.lnk"), target)
    if _mutex_exists(INSTANCE):
        ui.signal_instance(INSTANCE, "show")
        _install_log("уже запущена — показал окно")
        return True
    for attempt in range(3):                              # запускаем и убеждаемся, что поднялась
        pz_update.launch_clean(target)
        for _ in range(60):
            time.sleep(0.25)
            if _mutex_exists(INSTANCE):
                _install_log("установлено и запущено: %s (попытка %d)" % (target, attempt + 1))
                return True
        _install_log("новая копия не поднялась за 15 с — пробую ещё раз")
    _install_log("не удалось запустить установленную копию — работаю из %s" % sys.executable)
    return False


def main():
    if "--replace" in sys.argv:                      # это новая версия ставит себя на место старой
        return pz_update.run_replace(sys.argv, lambda: pz_update.mutex_exists(INSTANCE), _install_log)
    ui.setup_dpi()
    if self_install():
        return 0
    inst = ui.SingleInstance(INSTANCE)
    if not inst.ok:
        inst.signal_show()
        return 0
    selftest = bool(os.environ.get("PEREZVON_SELFTEST"))
    app = App(minimized="--autostart" in sys.argv or selftest)
    if selftest:                                     # проверка собранного exe: окна создаются, модули на месте
        app.root.withdraw()
        app.root.update()
        with open(os.path.join(app.home, "selftest.ok"), "w") as f:
            f.write(pc.APP_VERSION)
        return 0
    inst.listen(lambda: app.root.after(0, app.show_main), lambda: app.root.after(0, app.quit))
    if "--updated" in sys.argv:
        app.set_status_note("Обновлено до версии %s" % pc.APP_VERSION)
        _install_log("запущена версия %s после обновления" % pc.APP_VERSION)
    app.start()
    app.root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
