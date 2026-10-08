# -*- coding: utf-8 -*-
"""Общий интерфейс «Перезвона»: тема, кнопки, поля, клавиатура в русской раскладке, Win32."""
import ctypes
import ctypes.wintypes as wt
import os
import sys
import threading
import tkinter as tk
from tkinter import ttk

IS_WIN = sys.platform == "win32"

# ───────────────────────────── тема ─────────────────────────────

C = {
    "bg": "#15171C", "surface": "#1C1F26", "card": "#232731", "card_h": "#2A2F3B", "border": "#2F3441",
    "input": "#101218", "text": "#ECEEF2", "muted": "#9AA2B1", "faint": "#636B7A",
    "accent": "#3D8BFD", "accent_h": "#5B9DFF", "accent_d": "#2F6FD0",
    "green": "#22B573", "green_h": "#2CCB83", "amber": "#F5A524", "amber_h": "#FFB73D",
    "red": "#EF4E4B", "red_h": "#FF6461", "white": "#FFFFFF", "chip": "#262B36", "chip_h": "#303644",
}
URG = {"later": C["accent"], "soon": C["amber"], "due": C["red"], "missed": C["red"],
       "done": C["green"], "deleted": C["faint"]}

FONT = "Segoe UI"
FONT_SB = "Segoe UI Semibold"
ICON_FONT = "Segoe MDL2 Assets"
I = {  # глифы Segoe MDL2 Assets
    "phone": "", "copy": "", "check": "", "delete": "", "clock": "",
    "pin": "", "unpin": "", "settings": "", "close": "", "add": "",
    "contact": "", "more": "", "edit": "", "undo": "", "history": "",
    "sync": "", "offline": "", "ringer": "", "snooze": "", "back": "",
    "note": "", "missed": "", "people": "", "room": "", "search": "",
    "refresh": "", "warning": "", "keyboard": "", "signout": "",
}

_SCALE = 1.0


def setup_dpi():
    if not IS_WIN:
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def init_scale(root):
    global _SCALE
    try:
        _SCALE = max(1.0, root.winfo_fpixels("1i") / 96.0)
    except Exception:
        _SCALE = 1.0
    return _SCALE


def px(n):
    return int(round(n * _SCALE))


def font(size=10, bold=False):
    return (FONT_SB if bold else FONT, size)


def ifont(size=11):
    return (ICON_FONT, size)


def blend(c1, c2, t):
    a = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02X%02X%02X" % tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def apply_ttk_theme(root):
    st = ttk.Style(root)
    try:
        st.theme_use("clam")
    except tk.TclError:
        pass
    st.configure("Vertical.TScrollbar", background=C["card"], troughcolor=C["surface"],
                 bordercolor=C["surface"], arrowcolor=C["muted"], lightcolor=C["card"], darkcolor=C["card"],
                 gripcount=0, arrowsize=px(10))
    st.map("Vertical.TScrollbar", background=[("active", C["card_h"])])
    st.configure("TCombobox", fieldbackground=C["input"], background=C["card"], foreground=C["text"],
                 arrowcolor=C["muted"], bordercolor=C["border"], lightcolor=C["input"], darkcolor=C["input"],
                 selectbackground=C["accent_d"], selectforeground=C["white"], padding=px(6))
    st.map("TCombobox", fieldbackground=[("readonly", C["input"])], foreground=[("readonly", C["text"])],
           bordercolor=[("focus", C["accent"])])
    root.option_add("*TCombobox*Listbox.background", C["card"])
    root.option_add("*TCombobox*Listbox.foreground", C["text"])
    root.option_add("*TCombobox*Listbox.selectBackground", C["accent_d"])
    root.option_add("*TCombobox*Listbox.font", font(10))
    st.configure("Treeview", background=C["surface"], fieldbackground=C["surface"], foreground=C["text"],
                 bordercolor=C["border"], rowheight=px(30), font=font(10))
    st.map("Treeview", background=[("selected", C["accent_d"])], foreground=[("selected", C["white"])])
    st.configure("Treeview.Heading", background=C["card"], foreground=C["muted"], relief="flat",
                 font=font(9, True), bordercolor=C["border"], padding=(px(6), px(6)))
    st.map("Treeview.Heading", background=[("active", C["card_h"])])


# ───────────────────────────── клавиатура ─────────────────────────────
#
# В Tk горячие клавиши привязаны к латинским символам: при русской раскладке Ctrl+С/М/Ч/Ф
# приходят как Cyrillic_es и т.п. и не работают. Ловим по виртуальному коду клавиши (keycode
# на Windows = VK_*), он от раскладки не зависит. Латиницу Tk обрабатывает сам — её не трогаем,
# иначе вставка сработала бы дважды.

VK_A, VK_C, VK_V, VK_X, VK_Y, VK_Z = 65, 67, 86, 88, 89, 90
_LATIN = {VK_A: "a", VK_C: "c", VK_V: "v", VK_X: "x", VK_Y: "y", VK_Z: "z"}


def _is_entry(w):
    return isinstance(w, (tk.Entry, ttk.Entry)) or (hasattr(w, "winfo_class") and w.winfo_class() in ("Entry", "TEntry", "TCombobox"))


def entry_select_all(w):
    try:
        w.selection_range(0, "end")
        w.icursor("end")
    except tk.TclError:
        pass


def _ctrl_key(e):
    w = e.widget
    if not _is_entry(w):
        return None
    vk = e.keycode
    latin = (e.keysym or "").lower() == _LATIN.get(vk)
    if vk == VK_A:                       # Ctrl+A — выделить всё (в Tk по умолчанию «в начало строки»)
        entry_select_all(w)
        return "break"
    if vk == VK_Z:
        undo_entry(w, redo=bool(e.state & 0x0001))
        return "break"
    if vk == VK_Y:
        undo_entry(w, redo=True)
        return "break"
    if latin:
        return None
    ev = {VK_C: "<<Copy>>", VK_V: "<<Paste>>", VK_X: "<<Cut>>"}.get(vk)
    if ev:
        w.event_generate(ev)
        return "break"
    return None


def _ctrl_backspace(e):
    w = e.widget
    if not _is_entry(w):
        return None
    try:
        if w.selection_present():
            w.delete("sel.first", "sel.last")
            return "break"
        pos = w.index("insert")
        s = w.get()[:pos]
        i = len(s.rstrip())
        while i > 0 and not s[i - 1].isspace():
            i -= 1
        w.delete(i, pos)
    except tk.TclError:
        pass
    return "break"


# простая история правок для полей (Ctrl+Z / Ctrl+Y)
_HIST = {}


def track_undo(entry, var):
    h = {"stack": [var.get()], "pos": 0, "lock": False}
    _HIST[str(entry)] = (h, var)

    def changed(*_):
        if h["lock"]:
            return
        v = var.get()
        if v == h["stack"][h["pos"]]:
            return
        del h["stack"][h["pos"] + 1:]
        h["stack"].append(v)
        if len(h["stack"]) > 100:
            h["stack"].pop(0)
        h["pos"] = len(h["stack"]) - 1
    var.trace_add("write", changed)


def undo_entry(w, redo=False):
    rec = _HIST.get(str(w))
    if not rec:
        return
    h, var = rec
    np = h["pos"] + (1 if redo else -1)
    if not 0 <= np < len(h["stack"]):
        return
    h["pos"] = np
    h["lock"] = True
    var.set(h["stack"][np])
    h["lock"] = False
    try:
        w.icursor("end")
    except tk.TclError:
        pass


def install_keyboard(root):
    root.bind_all("<Control-KeyPress>", _ctrl_key, add="+")
    root.bind_class("Entry", "<Control-BackSpace>", _ctrl_backspace)
    root.bind_class("Entry", "<Button-3>", _entry_menu, add="+")


_MENU = None


def _entry_menu(e):
    global _MENU
    w = e.widget
    if _MENU is None:
        _MENU = tk.Menu(w, tearoff=0, bg=C["card"], fg=C["text"], activebackground=C["accent_d"],
                        activeforeground=C["white"], bd=0, font=font(10))
    m = _MENU
    m.delete(0, "end")
    try:
        has_sel = w.selection_present()
    except tk.TclError:
        has_sel = False
    try:
        has_clip = bool(w.clipboard_get())
    except tk.TclError:
        has_clip = False
    st = lambda ok: "normal" if ok else "disabled"
    m.add_command(label="Вырезать        Ctrl+X", state=st(has_sel), command=lambda: w.event_generate("<<Cut>>"))
    m.add_command(label="Копировать     Ctrl+C", state=st(has_sel), command=lambda: w.event_generate("<<Copy>>"))
    m.add_command(label="Вставить         Ctrl+V", state=st(has_clip), command=lambda: w.event_generate("<<Paste>>"))
    m.add_separator()
    m.add_command(label="Выделить всё  Ctrl+A", command=lambda: (w.focus_set(), entry_select_all(w)))
    m.add_command(label="Очистить", command=lambda: w.delete(0, "end"))
    w.focus_set()
    m.tk_popup(e.x_root, e.y_root)
    return "break"


def copy_text(root, text):
    root.clipboard_clear()
    root.clipboard_append(text)
    root.update_idletasks()


def clipboard_text(root):
    try:
        return root.clipboard_get()
    except tk.TclError:
        return ""


# ───────────────────────────── виджеты ─────────────────────────────

class Btn(tk.Frame):
    """Плоская кнопка: иконка + текст, наведение, нажатие, выключение."""

    def __init__(self, master, text="", icon=None, command=None, bg=None, hover=None, fg=None,
                 size=10, bold=True, padx=14, pady=8, icon_size=None, tooltip=None, anchor="center", width=None):
        self.base_bg = bg or C["card"]
        self.hover_bg = hover or blend(self.base_bg, "#FFFFFF", 0.08)
        self.fg = fg or C["text"]
        super().__init__(master, bg=self.base_bg, cursor="hand2")
        self.command = command
        self.enabled = True
        inner = tk.Frame(self, bg=self.base_bg)
        inner.pack(padx=px(padx), pady=px(pady), anchor=anchor, expand=True)
        self.parts = [self, inner]
        self.icon_lbl = None
        if icon:
            self.icon_lbl = tk.Label(inner, text=I.get(icon, icon), font=ifont(icon_size or size), bg=self.base_bg,
                                     fg=self.fg)
            self.icon_lbl.pack(side="left")
            self.parts.append(self.icon_lbl)
        self.lbl = None
        if text:
            self.lbl = tk.Label(inner, text=text, font=font(size, bold), bg=self.base_bg, fg=self.fg)
            self.lbl.pack(side="left", padx=(px(7) if icon else 0, 0))
            self.parts.append(self.lbl)
        if width:
            self.configure(width=px(width))
        for p in self.parts:
            p.bind("<Enter>", self._enter)
            p.bind("<Leave>", self._leave)
            p.bind("<ButtonRelease-1>", self._click)
        if tooltip:
            Tooltip(self, tooltip)

    def _paint(self, color):
        for p in self.parts:
            p.configure(bg=color)

    def _enter(self, _e=None):
        if self.enabled:
            self._paint(self.hover_bg)

    def _leave(self, _e=None):
        self._paint(self.base_bg)

    def _click(self, e):
        if not self.enabled:
            return
        x, y = e.x_root - self.winfo_rootx(), e.y_root - self.winfo_rooty()
        if 0 <= x <= self.winfo_width() and 0 <= y <= self.winfo_height() and self.command:
            self.command()

    def set_text(self, text):
        if self.lbl:
            self.lbl.configure(text=text)

    def set_colors(self, bg, hover=None, fg=None):
        self.base_bg, self.hover_bg = bg, hover or blend(bg, "#FFFFFF", 0.08)
        if fg:
            self.fg = fg
            for p in (self.icon_lbl, self.lbl):
                if p:
                    p.configure(fg=fg)
        self._paint(self.base_bg)

    def set_enabled(self, ok):
        self.enabled = ok
        fg = self.fg if ok else C["faint"]
        for p in (self.icon_lbl, self.lbl):
            if p:
                p.configure(fg=fg)
        self.configure(cursor="hand2" if ok else "arrow")


class IconBtn(Btn):
    def __init__(self, master, icon, command=None, bg=None, fg=None, size=11, tooltip=None, hover=None, pad=7):
        super().__init__(master, icon=icon, command=command, bg=bg or C["surface"], fg=fg or C["muted"],
                         hover=hover, size=size, padx=pad, pady=pad - 1, tooltip=tooltip)


class Field(tk.Frame):
    """Поле ввода: подпись сверху, подсказка внутри, рамка подсвечивается при фокусе, текст ошибки."""

    def __init__(self, master, label, placeholder="", icon=None, bg=None, width=28, optional=False):
        bg = bg or C["surface"]
        super().__init__(master, bg=bg)
        head = tk.Frame(self, bg=bg)
        head.pack(fill="x")
        tk.Label(head, text=label, font=font(9, True), fg=C["muted"], bg=bg).pack(side="left")
        if optional:
            tk.Label(head, text="  необязательно", font=font(8), fg=C["faint"], bg=bg).pack(side="left")
        self.right = tk.Frame(head, bg=bg)
        self.right.pack(side="right")
        self.box = tk.Frame(self, bg=C["input"], highlightthickness=1, highlightbackground=C["border"],
                            highlightcolor=C["accent"])
        self.box.pack(fill="x", pady=(px(4), 0))
        if icon:
            tk.Label(self.box, text=I[icon], font=ifont(10), fg=C["faint"], bg=C["input"]).pack(side="left", padx=(px(9), 0))
        self.var = tk.StringVar()
        self.entry = tk.Entry(self.box, textvariable=self.var, font=font(11), bg=C["input"], fg=C["text"],
                              insertbackground=C["text"], relief="flat", bd=0, width=width,
                              selectbackground=C["accent_d"], selectforeground=C["white"],
                              disabledbackground=C["input"])
        self.entry.pack(side="left", fill="x", expand=True, padx=px(9), pady=px(7))
        self.ph = tk.Label(self.box, text=placeholder, font=font(11), fg=C["faint"], bg=C["input"], cursor="xterm")
        self.ph.bind("<Button-1>", lambda e: self.entry.focus_set())
        self.err = tk.Label(self, text="", font=font(8), fg=C["red"], bg=bg, anchor="w", justify="left",
                            wraplength=px(350))
        self.var.trace_add("write", lambda *_: self._sync_ph())
        self.entry.bind("<FocusIn>", lambda e: self.box.configure(highlightbackground=C["accent"]), add="+")
        self.entry.bind("<FocusOut>", lambda e: self.box.configure(
            highlightbackground=C["red"] if self.err.winfo_ismapped() else C["border"]), add="+")
        track_undo(self.entry, self.var)
        self.after_idle(self._sync_ph)

    def _sync_ph(self):
        if self.var.get():
            self.ph.place_forget()
        else:
            self.ph.place(in_=self.entry, x=0, rely=0.5, anchor="w")

    def get(self):
        return self.var.get()

    def set(self, v):
        self.var.set(v)
        self.entry.icursor("end")

    def error(self, text=None):
        if text:
            self.err.configure(text=text)
            self.err.pack(fill="x", pady=(px(3), 0))
            self.box.configure(highlightbackground=C["red"])
        else:
            self.err.pack_forget()
            self.box.configure(highlightbackground=C["accent"] if self.focus_get() == self.entry else C["border"])


class Chip(tk.Label):
    def __init__(self, master, text, command, bg=None):
        self.bg0 = C["chip"]
        super().__init__(master, text=text, font=font(9, True), fg=C["text"], bg=self.bg0, padx=px(10),
                         pady=px(5), cursor="hand2")
        self.selected = False
        self.command = command
        self.bind("<Enter>", lambda e: self.configure(bg=C["accent_h"] if self.selected else C["chip_h"]))
        self.bind("<Leave>", lambda e: self._paint())
        self.bind("<Button-1>", lambda e: self.command())

    def _paint(self):
        self.configure(bg=C["accent"] if self.selected else self.bg0, fg=C["white"] if self.selected else C["text"])

    def select(self, on):
        self.selected = on
        self._paint()


class Tooltip:
    def __init__(self, widget, text):
        self.w, self.text, self.tip, self.job = widget, text, None, None
        widget.bind("<Enter>", self._sched, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _sched(self, _e=None):
        self._cancel()
        self.job = self.w.after(550, self._show)

    def _cancel(self):
        if self.job:
            self.w.after_cancel(self.job)
            self.job = None

    def _show(self):
        if self.tip or not self.w.winfo_exists():
            return
        t = self.text() if callable(self.text) else self.text
        if not t:
            return
        self.tip = tk.Toplevel(self.w)
        self.tip.overrideredirect(True)
        self.tip.attributes("-topmost", True)
        tk.Label(self.tip, text=t, font=font(9), bg="#0B0C10", fg=C["text"], padx=px(8), pady=px(4),
                 justify="left").pack()
        self.tip.update_idletasks()
        x = self.w.winfo_rootx() + self.w.winfo_width() // 2 - self.tip.winfo_width() // 2
        y = self.w.winfo_rooty() + self.w.winfo_height() + px(4)
        sw = self.w.winfo_screenwidth()
        x = max(0, min(x, sw - self.tip.winfo_width()))
        self.tip.geometry("+%d+%d" % (x, y))

    def _hide(self, _e=None):
        self._cancel()
        if self.tip:
            self.tip.destroy()
            self.tip = None


class ScrollArea(tk.Frame):
    """Прокручиваемая область с колесом мыши (только когда курсор над ней)."""

    def __init__(self, master, bg=None):
        bg = bg or C["surface"]
        super().__init__(master, bg=bg)
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0)
        self.sb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview, style="Vertical.TScrollbar")
        self.inner = tk.Frame(self.canvas, bg=bg)
        self.win = self.canvas.create_window(0, 0, window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self._on_scroll)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner.bind("<Configure>", lambda e: self._update())
        self.canvas.bind("<Configure>", lambda e: (self.canvas.itemconfigure(self.win, width=e.width), self._update()))
        self.canvas.bind_all("<MouseWheel>", self._wheel, add="+")

    def _on_scroll(self, a, b):
        self.sb.set(a, b)
        if float(a) <= 0 and float(b) >= 1:
            self.sb.pack_forget()
        elif not self.sb.winfo_ismapped():
            self.sb.pack(side="right", fill="y")

    def _update(self):
        self.canvas.configure(scrollregion=(0, 0, self.inner.winfo_reqwidth(), self.inner.winfo_reqheight()))

    def _wheel(self, e):
        # колесо глобальное: крутим, только если курсор над этой областью
        try:
            w = self.winfo_containing(e.x_root, e.y_root)
        except (tk.TclError, KeyError):
            return
        if w is None or not str(w).startswith(str(self)):
            return
        if self.inner.winfo_reqheight() > self.canvas.winfo_height():
            self.canvas.yview_scroll(int(-e.delta / 120) * 3, "units")

    def top(self):
        self.canvas.yview_moveto(0)


def separator(master, bg=None, pady=8):
    f = tk.Frame(master, bg=C["border"], height=1)
    f.pack(fill="x", pady=px(pady))
    return f


def fade_in(win, target=1.0, step=0.12, delay=12):
    try:
        win.attributes("-alpha", 0.0)
    except tk.TclError:
        return

    def tick(a=0.0):
        a = min(target, a + step)
        try:
            win.attributes("-alpha", a)
        except tk.TclError:
            return
        if a < target:
            win.after(delay, tick, a)
    tick()


# ───────────────────────────── Win32 ─────────────────────────────

class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]


def work_area(x, y):
    """Рабочая область (без панели задач) монитора, на котором точка (x, y)."""
    if IS_WIN:
        try:
            u32 = ctypes.windll.user32
            u32.MonitorFromPoint.restype = wt.HMONITOR
            u32.MonitorFromPoint.argtypes = [wt.POINT, wt.DWORD]
            hm = u32.MonitorFromPoint(wt.POINT(int(x), int(y)), 2)  # MONITOR_DEFAULTTONEAREST
            mi = _MONITORINFO()
            mi.cbSize = ctypes.sizeof(_MONITORINFO)
            if u32.GetMonitorInfoW(hm, ctypes.byref(mi)):
                r = mi.rcWork
                return r.left, r.top, r.right, r.bottom
        except Exception:
            pass
    return 0, 0, 1920, 1040


def hwnd_of(win):
    try:
        return ctypes.windll.user32.GetParent(win.winfo_id()) or win.winfo_id()
    except Exception:
        return 0


def no_activate(win):
    """Окно-напоминание не отбирает фокус у того, где оператор печатает (WS_EX_NOACTIVATE)."""
    if not IS_WIN:
        return
    try:
        h = hwnd_of(win)
        u32 = ctypes.windll.user32
        GWL_EXSTYLE = -20
        style = u32.GetWindowLongW(h, GWL_EXSTYLE)
        u32.SetWindowLongW(h, GWL_EXSTYLE, style | 0x08000000 | 0x00000080)  # NOACTIVATE | TOOLWINDOW
    except Exception:
        pass


def tool_window(win):
    """Без кнопки на панели задач и в Alt+Tab."""
    if not IS_WIN:
        return
    try:
        h = hwnd_of(win)
        u32 = ctypes.windll.user32
        style = u32.GetWindowLongW(h, -20)
        u32.SetWindowLongW(h, -20, (style | 0x00000080) & ~0x00040000)
    except Exception:
        pass


def raise_top(win):
    """Поверх всех окон. Сначала применяем отложенную geometry: «-topmost» до неё сбрасывает окно в (0,0)."""
    try:
        win.update_idletasks()
        win.attributes("-topmost", True)
        win.lift()
    except tk.TclError:
        pass


class _FLASHWINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.UINT), ("hwnd", wt.HWND), ("dwFlags", wt.DWORD), ("uCount", wt.UINT),
                ("dwTimeout", wt.DWORD)]


def flash_taskbar(win):
    """Кнопка на панели задач мигает оранжевым, пока окно не откроют (если оно не активно)."""
    if not IS_WIN or foreground_is_ours():
        return
    try:
        fi = _FLASHWINFO(ctypes.sizeof(_FLASHWINFO), hwnd_of(win), 0x2 | 0xC, 0, 0)  # TRAY | TIMERNOFG
        ctypes.windll.user32.FlashWindowEx(ctypes.byref(fi))
    except Exception:
        pass


def force_foreground(win):
    try:
        win.deiconify()
        win.lift()
        win.focus_force()
        if IS_WIN:
            ctypes.windll.user32.SetForegroundWindow(hwnd_of(win))
    except Exception:
        pass


class SingleInstance:
    """Именованный мьютекс + события «покажись» и «выйди» для второго запуска."""

    def __init__(self, name):
        self.ok = True
        self.handles = []
        if not IS_WIN:
            return
        k32 = ctypes.windll.kernel32
        k32.CreateMutexW.restype = wt.HANDLE
        k32.CreateEventW.restype = wt.HANDLE
        self.mutex = k32.CreateMutexW(None, False, "Local\\%s.mutex" % name)
        self.ok = k32.GetLastError() != 183  # ERROR_ALREADY_EXISTS
        self.ev_show = k32.CreateEventW(None, False, False, "Local\\%s.show" % name)
        self.ev_quit = k32.CreateEventW(None, False, False, "Local\\%s.quit" % name)

    def signal_show(self):
        if IS_WIN:
            ctypes.windll.kernel32.SetEvent(self.ev_show)

    def signal_quit(self):
        if IS_WIN:
            ctypes.windll.kernel32.SetEvent(self.ev_quit)

    def listen(self, on_show, on_quit):
        if not IS_WIN:
            return
        k32 = ctypes.windll.kernel32
        arr = (wt.HANDLE * 2)(self.ev_show, self.ev_quit)

        def loop():
            while True:
                r = k32.WaitForMultipleObjects(2, arr, False, 0xFFFFFFFF)
                if r == 0:
                    on_show()
                elif r == 1:
                    on_quit()
                    return
                else:
                    return
        threading.Thread(target=loop, daemon=True).start()


class GlobalHotkey:
    """Ctrl+Alt+<клавиша> из любого окна. Код клавиши — VK, от раскладки не зависит."""

    def __init__(self, vk, callback, mods=0x0002 | 0x0001 | 0x4000):  # CTRL | ALT | NOREPEAT
        self.ok = False
        if not IS_WIN:
            return
        self.vk, self.mods, self.cb = vk, mods, callback
        ready = threading.Event()

        def loop():
            u32 = ctypes.windll.user32
            self.ok = bool(u32.RegisterHotKey(None, 0x5051, self.mods, self.vk))
            ready.set()
            if not self.ok:
                return
            msg = wt.MSG()
            while u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == 0x0312:  # WM_HOTKEY
                    self.cb()
        threading.Thread(target=loop, daemon=True).start()
        ready.wait(2)


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def autostart_get(name):
    if not IS_WIN:
        return None
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            return winreg.QueryValueEx(k, name)[0]
    except OSError:
        return None


def autostart_set(name, cmd):
    if not IS_WIN:
        return
    import winreg
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
        if cmd:
            winreg.SetValueEx(k, name, 0, winreg.REG_SZ, cmd)
        else:
            try:
                winreg.DeleteValue(k, name)
            except OSError:
                pass


def resource(name):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def dark_titlebar(win):
    """Тёмный заголовок у обычных окон Windows 10/11."""
    if not IS_WIN:
        return
    try:
        win.update_idletasks()
        h = hwnd_of(win)
        v = ctypes.c_int(1)
        for attr in (20, 19):
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(h, attr, ctypes.byref(v), 4) == 0:
                break
    except Exception:
        pass


def foreground_is_ours():
    if not IS_WIN:
        return True
    try:
        u32 = ctypes.windll.user32
        h = u32.GetForegroundWindow()
        pid = wt.DWORD()
        u32.GetWindowThreadProcessId(h, ctypes.byref(pid))
        return pid.value == os.getpid()
    except Exception:
        return True


def set_icon(win):
    try:
        p = resource("perezvon.ico")
        if os.path.exists(p):
            win.iconbitmap(p)
    except Exception:
        pass


def chime_wav():
    """Мягкий двойной «динь» — WAV в памяти, без файлов."""
    import io
    import math
    import struct
    import wave
    rate = 22050
    frames = bytearray()
    total = int(rate * 0.8)
    buf = [0.0] * total
    for f0, start, dur in ((880.0, 0.0, 0.45), (1318.5, 0.16, 0.62)):
        s0 = int(start * rate)
        for i in range(int(dur * rate)):
            t = i / rate
            env = math.exp(-t * 6.5) * min(1.0, t * 400)
            v = env * (math.sin(2 * math.pi * f0 * t) + 0.25 * math.sin(4 * math.pi * f0 * t))
            if s0 + i < total:
                buf[s0 + i] += v * 0.32
    for v in buf:
        frames += struct.pack("<h", int(max(-1.0, min(1.0, v)) * 32000))
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    return out.getvalue()


_CHIME = None


def play_chime():
    if not IS_WIN:
        return

    def run():
        global _CHIME
        try:
            import winsound
            if _CHIME is None:
                _CHIME = chime_wav()
            winsound.PlaySound(_CHIME, winsound.SND_MEMORY)
        except Exception:
            try:
                import winsound
                winsound.MessageBeep()
            except Exception:
                pass
    threading.Thread(target=run, daemon=True).start()


# ───────────────────────────── DPAPI: токен входа шифруется под учётку Windows ─────────────────────────────

class _BLOB(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data, protect):
    if not IS_WIN:
        return data
    buf = ctypes.create_string_buffer(data, len(data))
    src = _BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    dst = _BLOB()
    c32 = ctypes.windll.crypt32
    fn = c32.CryptProtectData if protect else c32.CryptUnprotectData
    if not fn(ctypes.byref(src), None, None, None, None, 0x01, ctypes.byref(dst)):  # UI_FORBIDDEN
        raise OSError("DPAPI")
    try:
        return ctypes.string_at(dst.pbData, dst.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(dst.pbData)


def secret_pack(text):
    import base64
    if not text:
        return None
    return base64.b64encode(_dpapi(text.encode("utf-8"), True)).decode("ascii")


def secret_unpack(blob):
    import base64
    if not blob:
        return None
    try:
        return _dpapi(base64.b64decode(blob), False).decode("utf-8")
    except Exception:
        return None


class Session:
    """Данные входа (токен, кто вошёл) — отдельно от настроек, в скрытой папке, файл целиком зашифрован DPAPI:
    прочитать его можно только под этой учётной записью Windows на этом компьютере."""

    def __init__(self, folder):
        self.folder = folder
        self.path = os.path.join(folder, "session.bin")
        self.data = self._load()

    def _load(self):
        import json
        try:
            with open(self.path, "rb") as f:
                raw = f.read()
            d = json.loads(_dpapi(raw, False).decode("utf-8"))
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def get(self, k):
        return self.data.get(k)

    def set(self, **kw):
        self.data.update(kw)
        self.data = {k: v for k, v in self.data.items() if v is not None}
        if self.data:
            self._save()
        else:
            self.clear()

    def clear(self):
        self.data = {}
        try:
            os.remove(self.path)
        except OSError:
            pass

    def _save(self):
        import json
        os.makedirs(self.folder, exist_ok=True)
        if IS_WIN:
            try:
                ctypes.windll.kernel32.SetFileAttributesW(self.folder, 0x2)   # скрытая папка
            except Exception:
                pass
        raw = _dpapi(json.dumps(self.data, ensure_ascii=False).encode("utf-8"), True)
        tmp = self.path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)


def session_dir(app_name, home=None):
    """Тесты передают свою папку; обычно — %LOCALAPPDATA%\\<app>\\Data (не роуминг, не рядом с настройками)."""
    if home:
        return os.path.join(home, "Data")
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, app_name, "Data")
