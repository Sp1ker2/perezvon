# -*- coding: utf-8 -*-
"""Форма входа для обеих программ: ник Telegram (+ «Да, это я» в боте) или код из 12 цифр."""
import queue
import socket
import threading
import tkinter as tk
import webbrowser

import pz_common as pc
import pz_ui as ui
from pz_ui import C, px, font


class LoginForm(tk.Frame):
    def __init__(self, master, app_kind, get_server, on_success, bg=None):
        bg = bg or C["surface"]
        super().__init__(master, bg=bg)
        self.app_kind = app_kind
        self.get_server = get_server
        self.on_success = on_success
        self.q = queue.Queue()
        self.cancel = None
        self.bot = None
        tk.Label(self, text="Ваш ник в Telegram", font=font(11, True), fg=C["text"], bg=bg).pack(anchor="w")
        tk.Label(self, text="Тот, что указал администратор. Раскладка не важна.\n"
                            "Если у вас нет ника и админ выдал код из 12 цифр — введите код.",
                 font=font(9), fg=C["muted"], bg=bg, justify="left").pack(anchor="w", pady=(px(2), 0))
        self.f = ui.Field(self, "Ник или код", "@anna_smirnova", icon="contact", width=32)
        self.f.pack(fill="x", pady=(px(12), 0))
        self.f.var.trace_add("write", lambda *_: self._layout())
        self.f.entry.bind("<Return>", lambda e: self.go())
        self.btn = ui.Btn(self, "Войти", icon="signout", command=self.go, bg=C["accent"], hover=C["accent_h"],
                          fg=C["white"], size=11, pady=10)
        self.btn.pack(fill="x", pady=(px(14), 0))
        self.wait = tk.Frame(self, bg=C["card"])
        self.msg = tk.Label(self, text="", font=font(9), fg=C["muted"], bg=bg, wraplength=px(380), justify="left")
        self.msg.pack(anchor="w", pady=(px(8), 0))
        self.extra = tk.Frame(self, bg=bg)
        self.extra.pack(fill="x")
        self._busy_layout = False
        self.after(100, self._poll)

    def focus(self):
        self.f.entry.focus_force()

    def _layout(self):
        """Ник в русской раскладке сразу превращаем в латиницу (код из цифр не трогаем)."""
        if self._busy_layout:
            return
        v = self.f.get()
        fixed = pc.fix_layout(v)
        if fixed != v:
            self._busy_layout = True
            pos = self.f.entry.index("insert")
            self.f.var.set(fixed)
            self.f.entry.icursor(pos)
            self._busy_layout = False
        self.f.error()

    def go(self):
        if self.cancel:                      # уже ждём подтверждения
            return
        v = self.f.get().strip()
        if not v:
            self.f.error("Введите ник, например @anna_smirnova")
            return
        if not pc.is_code(v) and not v.lstrip("@"):
            self.f.error("Введите ник")
            return
        self.set_extra()
        self.btn.set_enabled(False)
        self.msg.configure(text="Проверяю…", fg=C["muted"])
        cancel = threading.Event()
        self.cancel = cancel
        api = pc.Api(self.get_server())

        def run():
            try:
                res = pc.login_flow(api, v, self.app_kind, socket.gethostname(), cancel,
                                    on_wait=lambda r: self.q.put(("wait", r)))
                self.q.put(("ok", res))
            except pc.ApiError as e:
                self.q.put(("err", e))
        threading.Thread(target=run, daemon=True).start()

    def stop(self):
        if self.cancel:
            self.cancel.set()
        self.cancel = None
        self.btn.set_enabled(True)
        self.msg.configure(text="Отменено", fg=C["muted"])
        self.set_extra()

    def set_extra(self, *buttons):
        for w in self.extra.winfo_children():
            w.destroy()
        for text, cmd, primary in buttons:
            ui.Btn(self.extra, text, command=cmd, bg=C["accent"] if primary else C["chip"],
                   fg=C["white"] if primary else C["text"], size=9, padx=12, pady=6).pack(
                side="left", padx=(0, px(8)), pady=(px(8), 0))

    def open_bot(self):
        if self.bot:
            webbrowser.open("https://t.me/" + self.bot)

    def _poll(self):
        try:
            while True:
                kind, res = self.q.get_nowait()
                if kind == "wait":
                    self.bot = res.get("bot") or self.bot
                    self.msg.configure(text="Отправил запрос в Telegram. Откройте бота%s и нажмите «✅ Да, это я».\n"
                                            "Жду подтверждения…" % ((" @" + self.bot) if self.bot else ""),
                                       fg=C["accent_h"])
                    btns = [("Отмена", self.stop, False)]
                    if self.bot:
                        btns.insert(0, ("Открыть Telegram", self.open_bot, True))
                    self.set_extra(*btns)
                elif kind == "ok":
                    self.cancel = None
                    self.btn.set_enabled(True)
                    self.msg.configure(text="Готово!", fg=C["green"])
                    self.set_extra()
                    self.on_success(res)
                    return
                else:
                    e = res
                    if e.status == -1:
                        continue
                    self.cancel = None
                    self.btn.set_enabled(True)
                    self.msg.configure(text=e.message, fg=C["red"] if e.status != 0 else C["amber"])
                    self.set_extra()
                    if e.data.get("need_start") and e.data.get("bot"):
                        self.bot = e.data["bot"]
                        self.set_extra(("Открыть бота @" + self.bot, self.open_bot, True))
        except queue.Empty:
            pass
        if self.winfo_exists():
            self.after(150, self._poll)
