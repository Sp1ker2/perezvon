# -*- coding: utf-8 -*-
"""Снимки окон для визуальной проверки → tests/screens/*.png. Фокус у пользователя не отбирает."""
import os
import sys
import threading
import time

import helpers
from helpers import TestServer
import pz_common as pc
import pz_ui as ui

os.environ["PEREZVON_NOINSTALL"] = "1"
ui.force_foreground = lambda win: None          # не отбирать фокус
import perezvon  # noqa: E402
import perezvon_admin  # noqa: E402

OUT = os.path.join(os.path.dirname(__file__), "screens")
os.makedirs(OUT, exist_ok=True)
import tempfile  # noqa: E402


def pump(root, sec):
    end = time.time() + sec
    while time.time() < end:
        root.update()
        time.sleep(0.01)


def shot(win, name, pad=0):
    """Снимок ТОЛЬКО этого окна (PrintWindow): даже если его что-то перекрывает, чужое в кадр не попадёт."""
    import ctypes
    import ctypes.wintypes as wt
    from PIL import Image
    win.update()
    u32, g32 = ctypes.windll.user32, ctypes.windll.gdi32
    hwnd = ui.hwnd_of(win)
    r = wt.RECT()
    u32.GetWindowRect(hwnd, ctypes.byref(r))
    w, h = r.right - r.left, r.bottom - r.top
    hdc = u32.GetWindowDC(hwnd)
    mem = g32.CreateCompatibleDC(hdc)
    bmp = g32.CreateCompatibleBitmap(hdc, w, h)
    g32.SelectObject(mem, bmp)
    u32.PrintWindow(hwnd, mem, 2)                       # PW_RENDERFULLCONTENT

    class BIH(ctypes.Structure):
        _fields_ = [("biSize", wt.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long),
                    ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
                    ("biSizeImage", wt.DWORD), ("a", ctypes.c_long), ("b", ctypes.c_long), ("c", wt.DWORD),
                    ("d", wt.DWORD)]
    bih = BIH(ctypes.sizeof(BIH), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
    buf = ctypes.create_string_buffer(w * h * 4)
    g32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bih), 0)
    g32.DeleteObject(bmp)
    g32.DeleteDC(mem)
    u32.ReleaseDC(hwnd, hdc)
    Image.frombuffer("RGB", (w, h), buf.raw, "raw", "BGRX", 0, 1).save(os.path.join(OUT, name + ".png"))
    print("saved", name)


def show_noactivate(win):
    """Показать окно, не отбирая фокус у того, кто печатает (SW_SHOWNOACTIVATE)."""
    import ctypes
    win.update_idletasks()
    ctypes.windll.user32.ShowWindow(ui.hwnd_of(win), 4)
    win.update()


def client_shots(S):
    home = tempfile.mkdtemp()
    pc.save_json(os.path.join(home, "settings.json"), {"server": S.url})
    tok = pc.login_flow(pc.Api(S.url), "@anna_smirnova", "client", "PC", threading.Event())["token"]
    sess = ui.Session(ui.session_dir("Perezvon", home))
    sess.set(token=tok, user={"id": 1, "name": "Анна Смирнова", "room": "vn2", "role": "operator",
                              "username": "anna_smirnova"})
    app = perezvon.App(home, minimized=True)
    st = app.store
    now = time.time()
    st.add("Иванов Иван Петрович", "89991234567", "договор поставки, просил после обеда", now + 47 * 60)
    st.add("Ковалёва Мария", "+7 912 000-11-22", "", now + 4 * 60 + 30)
    st.add("", "+44 7529 622562", "международный", now + 3 * 3600)
    late = st.add("Петров Сергей", "8 (495) 765-43-21", "уточнить счёт №2481", now - 2 * 60)
    st.update(late["id"], attempts=1)
    for fio, ph, back, note in (("Сидоров Павел", "89990000000", 3600, ""), ("Белова Ирина", "89161234455", 2 * 3600, "")):
        d = st.add(fio, ph, note, now - back, now=now - back - 900)
        st.done(d["id"], now=now - back + 120)
    for days, fio, done in ((1, "Громов Алексей", True), (1, "ООО «Ромашка»", False), (3, "Зайцева Ольга", True)):
        x = st.add(fio, "8916%07d" % days, "", now - days * 86400, now=now - days * 86400 - 600)
        if done:
            st.done(x["id"], now=now - days * 86400 + 300)
    app.start(hotkey=False)
    l, t, r, b = ui.work_area(960, 500)
    app.root.geometry("%dx%d+%d+%d" % (ui.px(460), min(ui.px(860), b - t - 20), r - ui.px(460) - 30, t + 10))
    show_noactivate(app.root)
    app.main.f_fio.set("Смирнов Олег")
    app.main.f_phone.set("+7 903 555-12-34")
    app.main.pick("15")
    app.main.refresh(force=True)
    pump(app.root, 1.5)
    shot(app.root, "main_today")
    app.main.show_tab("history", focus=False)
    pump(app.root, 0.6)
    shot(app.root, "main_history")
    app.main.show_tab("today", focus=False)
    app.main.open_form(False, focus=False)
    pump(app.root, 0.4)
    shot(app.root, "main_collapsed")
    app.collapse()
    for edge in ("right", "top", "br"):
        app.settings["dock"] = {"edge": edge, "pos": 0.4, "mx": None, "my": None}
        app.tab.show()
        pump(app.root, 0.4)
        shot(app.tab.win, "tab_" + edge)
    rem = app.reminders.open.get(late["id"])
    if rem:
        shot(rem.win, "client_reminder")
    app.root.destroy()


def admin_shots(S):
    home = tempfile.mkdtemp()
    pc.save_json(os.path.join(home, "settings.json"), {"server": S.url})
    a = perezvon_admin.AdminApp(home)
    a.form.f.set("@the_chief")
    a.form.go()
    end = time.time() + 8
    while time.time() < end and not (hasattr(a, "tree") and a.data):
        pump(a.root, 0.1)
    pump(a.root, 1.0)
    shot(a.root, "admin_main")
    a.set_filter("missed")
    pump(a.root, 0.5)
    shot(a.root, "admin_missed")
    a.root.destroy()


def seed(S):
    A = S.app
    A.create_user("Анна Смирнова", "vn2", "operator", "anna_smirnova")
    A.create_user("Олег Ким", "vn2", "operator", "oleg_kim")
    A.create_user("Ирина Белова", "vn3", "operator", "irina_b")
    A.create_user("Шеф", "vn2", "superadmin", "the_chief")
    A.create_user("Дежурный админ", "vn3", "admin", "duty_admin")
    data = {"@oleg_kim": [("Морозов Андрей", "89161112233", "по возврату", 25 * 60),
                          ("ООО «Ромашка», бухгалтерия", "84957778899", "", -40 * 60),
                          ("Лебедева Анна", "89035556677", "", 2 * 3600)],
            "@irina_b": [("Гусев Павел", "89263334455", "просил в 15:00", -30),
                         ("Тихонова Елена", "89851112200", "", 70 * 60)]}
    for nick, items in data.items():
        tok = pc.login_flow(pc.Api(S.url), nick, "client", "PC", threading.Event())["token"]
        st = pc.Store(os.path.join(S.dir, nick + ".json"))
        for fio, ph, note, d in items:
            st.add(fio, ph, note, time.time() + d)
        x = st.add("Фёдоров", "89160000001", "", time.time() - 7200)
        st.done(x["id"])
        body, pushed = st.build_push()
        st.apply_sync(pushed, pc.Api(S.url, tok).call("POST", "/api/sync", body))


if __name__ == "__main__":
    S = TestServer()
    seed(S)
    try:
        if "admin" not in sys.argv:
            client_shots(S)
        if "client" not in sys.argv:
            admin_shots(S)
    finally:
        S.close()
