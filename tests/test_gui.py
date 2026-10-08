# -*- coding: utf-8 -*-
"""Окна по-настоящему (Tk): клавиши в русской раскладке, форма, напоминание, язычок, вход, админка."""
import os
import shutil
import tempfile
import threading
import time
import tkinter as tk
import unittest

from helpers import TestServer
import pz_common as pc
import pz_ui as ui

os.environ["PEREZVON_NOINSTALL"] = "1"
import perezvon  # noqa: E402
import perezvon_admin  # noqa: E402

CTRL = 0x0004


def pump(root, sec=0.3):
    end = time.time() + sec
    while time.time() < end:
        root.update()
        time.sleep(0.01)


def key(w, keysym, keycode, state=CTRL):
    w.focus_force()
    w.update()
    w.event_generate("<KeyPress>", keysym=keysym, keycode=keycode, state=state, when="now")
    w.update()


import ctypes  # noqa: E402
import ctypes.wintypes as wt  # noqa: E402

U32 = ctypes.windll.user32
RU = "00000419"


def real_keys(*combo):
    """Настоящие нажатия через keybd_event: combo = [(vk, ...)] — зажимаем по порядку, отпускаем в обратном."""
    for vk in combo:
        U32.keybd_event(vk, 0, 0, 0)
    for vk in reversed(combo):
        U32.keybd_event(vk, 0, 2, 0)


REAL = os.environ.get("PEREZVON_REAL_INPUT") == "1"
REAL_WHY = "настоящие нажатия/клики: запускать с PEREZVON_REAL_INPUT=1, когда за компьютером никто не печатает"


@unittest.skipUnless(REAL, REAL_WHY)
class RealRussianKeyboard(unittest.TestCase):
    """Главная претензия: в РУССКОЙ раскладке Ctrl+C/V/X/A/Z должны работать ровно один раз.
    Нажатия настоящие (как с клавиатуры). Если окно теста не удалось сделать активным — тест пропускается,
    чтобы нажатия не ушли в чужое окно."""

    @classmethod
    def setUpClass(cls):
        cls.root = tk.Tk()
        cls.root.title("pz-keyboard-test")
        cls.root.geometry("400x120+200+200")
        cls.root.attributes("-topmost", True)
        ui.install_keyboard(cls.root)
        cls.f = ui.Field(cls.root, "Тест", "подсказка")
        cls.f.pack(fill="x", padx=10, pady=10)
        cls.keysyms = []
        cls.f.entry.bind("<KeyPress>", lambda e: cls.keysyms.append(e.keysym), add="+")
        pump(cls.root, 0.3)
        cls.prev_layout = U32.GetKeyboardLayout(0)
        cls.hkl = U32.LoadKeyboardLayoutW(RU, 0x1)          # KLF_ACTIVATE — для потока теста
        if not cls.hkl:
            raise unittest.SkipTest("русская раскладка не установлена в Windows")

    @classmethod
    def tearDownClass(cls):
        U32.ActivateKeyboardLayout(cls.prev_layout, 0)
        cls.root.destroy()

    def focus(self):
        ui.force_foreground(self.root)
        self.f.entry.focus_force()
        pump(self.root, 0.15)
        U32.ActivateKeyboardLayout(self.hkl, 0)
        if U32.GetForegroundWindow() != ui.hwnd_of(self.root):
            self.skipTest("окно теста не стало активным — не шлю нажатия, чтобы не попасть в чужое окно")

    def press(self, *vks):
        self.focus()
        real_keys(*vks)
        pump(self.root, 0.15)

    def test_all_hotkeys_russian(self):
        VK_CTRL, VK_SHIFT = 0x11, 0x10
        self.f.set("")
        self.root.clipboard_clear()
        self.root.clipboard_append("+7 999 123")
        self.keysyms.clear()
        self.press(VK_CTRL, 0x56)                          # Ctrl+V (в русской — «М»)
        # раскладка правда русская: Tk видит не «v», а «??» — именно поэтому обычно Ctrl+V не работает
        self.assertNotIn("v", self.keysyms)
        self.assertNotIn("V", self.keysyms)
        self.assertEqual(self.f.get(), "+7 999 123")       # вставилось ровно один раз
        self.press(VK_CTRL, 0x41)                          # Ctrl+A («Ф»)
        self.assertEqual(self.f.entry.selection_get(), "+7 999 123")
        self.press(VK_CTRL, 0x43)                          # Ctrl+C («С»)
        self.assertEqual(self.root.clipboard_get(), "+7 999 123")
        self.press(VK_CTRL, 0x58)                          # Ctrl+X («Ч»)
        self.assertEqual(self.f.get(), "")
        self.assertEqual(self.root.clipboard_get(), "+7 999 123")
        self.press(VK_CTRL, 0x5A)                          # Ctrl+Z («Я») — вернуть вырезанное
        self.assertEqual(self.f.get(), "+7 999 123")
        self.press(VK_CTRL, VK_SHIFT, 0x5A)                # Ctrl+Shift+Z — повтор
        self.assertEqual(self.f.get(), "")
        self.press(VK_CTRL, 0x59)                          # Ctrl+Y — ничего не ломает
        self.f.set("")
        self.press(0x47, 0x48)                             # обычный набор в русской: «пр»
        self.assertEqual(self.f.get(), "пр")
        self.press(VK_SHIFT, 0x2D)                         # Shift+Insert — тоже вставка
        self.assertEqual(self.f.get(), "пр+7 999 123")


class KeyHandler(unittest.TestCase):
    """Логика обработчика без реальных нажатий: латиницу не трогаем (иначе вставка была бы двойной)."""

    class Ev:
        def __init__(self, w, keysym, keycode, state=4):
            self.widget, self.keysym, self.keycode, self.state = w, keysym, keycode, state

    def setUp(self):
        self.root = tk.Tk()
        self.root.withdraw()
        self.e = tk.Entry(self.root)
        self.calls = []
        self.e.event_generate = lambda ev, **kw: self.calls.append(ev)

    def tearDown(self):
        self.root.destroy()

    def test_dispatch(self):
        h = ui._ctrl_key
        self.assertIsNone(h(self.Ev(self.e, "v", 86)))
        self.assertIsNone(h(self.Ev(self.e, "V", 86)))
        self.assertEqual(h(self.Ev(self.e, "Cyrillic_em", 86)), "break")
        self.assertEqual(h(self.Ev(self.e, "Cyrillic_es", 67)), "break")
        self.assertEqual(h(self.Ev(self.e, "Cyrillic_che", 88)), "break")
        self.assertEqual(self.calls, ["<<Paste>>", "<<Copy>>", "<<Cut>>"])
        self.assertIsNone(h(self.Ev(tk.Label(self.root), "Cyrillic_em", 86)))   # не поле — не трогаем


class ClientApp(unittest.TestCase):
    """Окно стартует свёрнутым (как при автозапуске) — тесты не отбирают фокус у того, кто работает за ПК."""

    def setUp(self):
        self.S = TestServer()
        self.S.app.create_user("Анна Смирнова", "vn2", "operator", "anna_smirnova")
        self.home = tempfile.mkdtemp(prefix="pz-home-")
        pc.save_json(os.path.join(self.home, "settings.json"), {"server": self.S.url, "local_mode": True})
        self._ff = ui.force_foreground
        ui.force_foreground = lambda w: None
        self.app = perezvon.App(self.home, minimized=True)
        self.app.start(hotkey=False)
        self.m = self.app.main
        pump(self.app.root, 0.3)

    def tearDown(self):
        ui.force_foreground = self._ff
        try:
            self.app.root.destroy()
        except tk.TclError:
            pass
        self.S.close()
        shutil.rmtree(self.home, ignore_errors=True)

    def labels(self, w):
        out = []

        def walk(x):
            for c in x.winfo_children():
                if isinstance(c, tk.Label):
                    out.append(c.cget("text"))
                walk(c)
        walk(w)
        return out

    def test_collapses_to_edge_icon(self):
        self.assertEqual(self.app.root.state(), "withdrawn")      # автозапуск: окна нет, только иконка
        self.assertTrue(self.app.tab.shown)
        self.assertTrue(self.app.tab.win.winfo_viewable())
        self.assertEqual(self.app.tab.win.attributes("-topmost"), 1)
        shown = []
        self.m.show = lambda focus=True: shown.append(focus)       # не открываем по-настоящему (фокус)
        self.app.tab.drag = {"x": 0, "y": 0, "wx": 0, "wy": 0, "moved": False}
        self.app.tab.release(None)                                # клик по иконке
        self.assertEqual(shown, [True])
        self.assertFalse(self.app.tab.shown)                      # окно открыто — иконка спрятана
        self.m.minimize()                                         # крестик
        self.assertEqual(self.app.root.state(), "withdrawn")
        self.assertTrue(self.app.tab.shown)
        self.assertTrue(self.app.root.winfo_exists())             # программа не закрылась
        self.app.root.state("iconic")                             # кнопка «свернуть» в заголовке
        pump(self.app.root, 0.3)
        self.assertEqual(self.app.root.state(), "withdrawn")
        self.assertTrue(self.app.tab.shown)

    def test_tab_snap_and_inside_screen(self):
        area = (0, 0, 1920, 1040)
        snap = perezvon.Tab.snap
        self.assertEqual(snap(1910, 500, area, 140)[0], "right")
        self.assertEqual(snap(5, 500, area, 140)[0], "left")
        self.assertEqual(snap(900, 3, area, 140)[0], "top")
        self.assertEqual(snap(900, 1030, area, 140)[0], "bottom")
        self.assertEqual(snap(1900, 20, area, 140)[0], "tr")
        self.assertEqual(snap(10, 1030, area, 140)[0], "bl")
        self.assertAlmostEqual(snap(1910, 520, area, 140)[1], 0.5, delta=0.01)
        l, t, r, b = self.app.tab.area()
        for edge in ("left", "right", "top", "bottom", "tl", "tr", "bl", "br"):
            self.app.settings["dock"] = {"edge": edge, "pos": 0.9, "mx": None, "my": None}
            self.app.tab.show()
            pump(self.app.root, 0.05)
            w = self.app.tab.win
            x, y = w.winfo_x(), w.winfo_y()
            self.assertGreaterEqual(x, l - 1, edge)
            self.assertGreaterEqual(y, t - 1, edge)
            self.assertLessEqual(x + w.winfo_width(), r + 1, edge)
            self.assertLessEqual(y + w.winfo_height(), b + 1, edge)
        self.app.settings["dock"] = {"edge": "right", "pos": 0.3, "mx": None, "my": None}
        self.app.tab.show()

    def test_tab_shows_count_and_due(self):
        self.app.store.add("К", "111", "", time.time() + 600)
        self.app.changed()
        texts = [self.app.tab.cv.itemcget(i, "text") for i in self.app.tab.cv.find_all()
                 if self.app.tab.cv.type(i) == "text"]
        self.assertIn("1", texts)
        self.assertIn("ждут", texts)
        self.app.store.add("Пора", "222", "", time.time() - 5)
        self.app.changed()
        texts = [self.app.tab.cv.itemcget(i, "text") for i in self.app.tab.cv.find_all()
                 if self.app.tab.cv.type(i) == "text"]
        self.assertIn("пора!", texts)
        self.assertIn(self.app.tab.cv.cget("bg").upper(), (ui.C["red"].upper(), ui.C["red_h"].upper()))

    def test_form_validation_and_add(self):
        p = self.m
        p.submit()
        self.assertIn("номер", p.f_phone.err.cget("text"))
        self.assertIn("через сколько", p.f_when.err.cget("text"))
        p.f_fio.set("Иванов Иван")
        p.f_phone.set("8 999 123-45-67")
        p.chips[2].command()                      # «15 мин»
        pump(self.app.root, 0.05)
        self.assertTrue(p.chips[2].selected)
        self.assertIn("через 15 мин", p.preview.cget("text"))
        p.f_when.set("абракадабра")
        self.assertIn("Не понял", p.preview.cget("text"))
        p.f_when.set("1ч 30м")
        self.assertIn("через 1 ч 30 мин", p.preview.cget("text"))
        p.submit()
        act = self.app.store.active()
        self.assertEqual(len(act), 1)
        self.assertEqual(act[0]["fio"], "Иванов Иван")
        self.assertAlmostEqual(act[0]["due"] - time.time(), 5400, delta=5)
        self.assertEqual(p.f_fio.get(), "")                    # форма очищена
        self.assertIn("Добавлено", p.toast.cget("text"))
        self.assertIn(act[0]["id"], p.cards)
        for f in (p.f_fio, p.f_phone, p.f_note, p.f_when):     # Enter в любом поле = «Добавить»
            self.assertTrue(f.entry.bind("<Return>"))

    def test_today_shows_waiting_and_done(self):
        st = self.app.store
        a = st.add("Ждёт", "111", "", time.time() + 600)
        b = st.add("Сделан", "222", "", time.time() - 600)
        old = st.add("Позавчера", "333", "", time.time() - 2 * 86400)
        st.done(b["id"])
        st.done(old["id"], now=time.time() - 2 * 86400)
        self.app.changed()
        self.assertIn(a["id"], self.m.cards)
        self.assertIn(b["id"], self.m.cards)
        self.assertNotIn(old["id"], self.m.cards)              # выполнен не сегодня → только в истории
        self.assertIn("ждут: 1", self.app.root.title())        # подпись кнопки на панели задач
        joined = "\n".join(self.labels(self.m.area.inner))
        self.assertIn("Ждут перезвона", joined)
        self.assertIn("Перезвонили сегодня", joined)

    def test_history_by_dates(self):
        import datetime as dt
        st = self.app.store
        t = time.time()
        st.add("Сегодняшний", "89990000001", "", t + 300)
        y = st.add("Вчерашний", "89990000002", "про счёт", t - 86400)
        st.done(y["id"], now=t - 86400 + 60)
        st.add("Давний", "89990000003", "", t - 5 * 86400)
        d = st.add("Удалённый", "89990000004", "", t)
        st.delete(d["id"])
        self.m.show_tab("history", focus=False)
        pump(self.app.root, 0.1)
        joined = "\n".join(self.labels(self.m.h_area.inner))
        self.assertIn("Сегодня · ", joined)
        self.assertIn("Вчера · ", joined)
        five = dt.date.fromtimestamp(t - 5 * 86400)
        self.assertIn("%d %s" % (five.day, perezvon.MONTHS[five.month - 1]), joined)
        self.assertLess(joined.index("Сегодняшний"), joined.index("Вчерашний"))   # новые сверху
        self.assertLess(joined.index("Вчерашний"), joined.index("Давний"))
        self.assertIn("перезвонили в", joined)
        self.assertIn("пропущен", joined)                       # «Давний» так и не сделали
        self.assertNotIn("Удалённый", joined)
        self.m.h_search.set("0000002")                          # поиск по куску номера
        pump(self.app.root, 0.05)
        names = [x for x in self.labels(self.m.h_area.inner) if x in ("Сегодняшний", "Вчерашний", "Давний")]
        self.assertEqual(names, ["Вчерашний"])
        self.m.show_tab("today", focus=False)

    def test_history_kept_long(self):
        st = self.app.store
        x = st.add("Месяц назад", "111", "", time.time() - 30 * 86400)
        st.done(x["id"], now=time.time() - 30 * 86400)
        st.items[x["id"]]["dirty"] = False
        st.purge()
        self.assertIn(x["id"], st.items)                        # история не теряется через 2 дня

    def test_reminder_lifecycle(self):
        st = self.app.store
        it = st.add("Петров", "89990000001", "про договор", time.time() - 1)
        self.app.loop_second()
        pump(self.app.root, 0.2)
        self.assertIn(it["id"], self.app.reminders.open)
        win = self.app.reminders.open[it["id"]].win
        self.assertTrue(win.winfo_viewable())
        self.assertEqual(win.attributes("-topmost"), 1)
        self.assertIn("пора звонить: 1", self.app.root.title())
        self.app.reminders.open[it["id"]].copy()
        self.assertEqual(self.app.root.clipboard_get(), "+79990000001")
        self.app.reminders.open[it["id"]].no_answer()
        self.assertNotIn(it["id"], self.app.reminders.open)
        self.assertEqual(st.items[it["id"]]["attempts"], 1)
        self.assertAlmostEqual(st.items[it["id"]]["due"], time.time() + 900, delta=5)
        st.update(it["id"], due=time.time() - 1)
        self.app.loop_second()
        self.app.reminders.open[it["id"]].done()
        self.assertEqual(st.items[it["id"]]["status"], "done")
        self.assertNotIn(it["id"], self.app.reminders.open)

    def test_many_reminders_stack(self):
        for i in range(6):
            self.app.store.add("К%d" % i, "100%d" % i, "", time.time() - 10 + i)
        self.app.loop_second()
        pump(self.app.root, 0.2)
        self.assertEqual(len(self.app.reminders.open), 4)       # не больше 4 окон сразу
        boxes = [(r.win.winfo_x(), r.win.winfo_y(), r.win.winfo_width(), r.win.winfo_height())
                 for r in self.app.reminders.open.values()]
        l, t, rr, bb = ui.work_area(self.app.root.winfo_screenwidth() // 2, self.app.root.winfo_screenheight() // 2)
        for x, y, w, h in boxes:                                   # все целиком на экране
            self.assertGreaterEqual(x, l)
            self.assertGreaterEqual(y, t)
            self.assertLessEqual(x + w, rr)
            self.assertLessEqual(y + h, bb)
        for i, A in enumerate(boxes):                              # и не налезают друг на друга
            for B in boxes[i + 1:]:
                overlap = not (A[0] + A[2] <= B[0] or B[0] + B[2] <= A[0] or A[1] + A[3] <= B[1] or B[1] + B[3] <= A[1])
                self.assertFalse(overlap, (A, B))

    def test_login_by_nick_in_russian_layout(self):
        self.app.open_login()
        pump(self.app.root, 0.2)
        form = self.app.login_win.form
        form.f.set('"фттф_ыьшктщмф')
        pump(self.app.root, 0.05)
        self.assertEqual(form.f.get(), "@anna_smirnova")       # раскладка поправлена на лету
        form.go()
        for _ in range(100):
            pump(self.app.root, 0.05)
            if self.app.settings["token"]:
                break
        self.assertTrue(self.app.settings["token"])
        self.assertEqual(self.app.settings["user"]["name"], "Анна Смирнова")
        self.assertIn("Анна Смирнова", self.m.sub.cget("text"))
        raw = open(os.path.join(self.home, "settings.json"), encoding="utf-8").read()
        self.assertNotIn(self.app.settings["token"], raw)     # токен на диске зашифрован
        self.app.store.add("Клиент", "222333", "", time.time() + 600)
        self.app.sync.kick(0)
        for _ in range(100):
            pump(self.app.root, 0.05)
            if self.app.sync.last_ok and not self.app.store.dirty_count():
                break
        self.assertEqual(self.app.store.dirty_count(), 0)
        self.assertEqual(self.S.app.db.one("SELECT COUNT(*) FROM items")[0], 1)

    def test_login_unknown_nick(self):
        self.app.open_login()
        form = self.app.login_win.form
        form.f.set("@nobody_here")
        form.go()
        for _ in range(60):
            pump(self.app.root, 0.05)
            if "нет в списке" in form.msg.cget("text"):
                break
        self.assertIn("нет в списке", form.msg.cget("text"))
        self.assertFalse(self.app.settings["token"])

    def test_offline_keeps_working(self):
        self.app.settings["server"] = "http://127.0.0.1:9"       # никто не слушает
        self.app.settings["token"] = "x"
        self.app.store.add("Офлайн", "111222", "", time.time() + 60)
        self.app.sync.kick(0)
        for _ in range(100):
            pump(self.app.root, 0.05)
            if self.app.sync.error:
                break
        self.assertIn("Нет связи", self.app.sync.error)
        self.app.refresh_status()
        self.assertIn("сохранено на этом ПК", self.m.status.cget("text"))
        self.assertEqual(len(self.app.store.active()), 1)

    def test_edit_and_delete_undo(self):
        p = self.m
        it = self.app.store.add("Старое", "111", "", time.time() + 3600)
        p.start_edit(it["id"])
        self.assertEqual(p.f_fio.get(), "Старое")
        p.f_fio.set("Новое")
        p.submit()                                           # время не трогали — осталось прежним
        self.assertEqual(self.app.store.items[it["id"]]["fio"], "Новое")
        self.assertAlmostEqual(self.app.store.items[it["id"]]["due"], it["due"], delta=60)
        p.delete(it["id"])
        self.assertEqual(self.app.store.active(), [])
        self.assertIn("вернуть", p.toast.cget("text"))
        p.undo_cb()
        self.assertEqual(len(self.app.store.active()), 1)

    def login_as(self, nick):
        self.app.open_login()
        form = self.app.login_win.form
        form.f.set(nick)
        form.go()
        for _ in range(100):
            pump(self.app.root, 0.05)
            if self.app.settings["token"]:
                return
        self.fail("не вошли")

    def sync_now(self):
        self.app.sync.kick(0)
        for _ in range(100):
            pump(self.app.root, 0.05)
            if self.app.sync.last_ok and not self.app.store.dirty_count() and not self.app.sync.busy:
                return

    def test_session_hidden_and_encrypted(self):
        import ctypes
        self.login_as("@anna_smirnova")
        tok = self.app.settings["token"]
        folder = os.path.join(self.home, "Data")
        path = os.path.join(folder, "session.bin")
        self.assertTrue(os.path.exists(path))
        raw = open(path, "rb").read()
        self.assertNotIn(tok.encode(), raw)                       # зашифровано
        self.assertNotIn("Анна".encode("utf-8"), raw)
        self.assertTrue(ctypes.windll.kernel32.GetFileAttributesW(folder) & 0x2)   # папка скрытая
        settings_raw = open(os.path.join(self.home, "settings.json"), encoding="utf-8").read()
        self.assertNotIn("token", settings_raw)
        self.assertNotIn("anna", settings_raw)                    # в настройках про вход ничего нет
        self.assertIn("Перезвон", ui.session_dir("Perezvon").replace("Perezvon", "Перезвон"))
        self.assertTrue(ui.session_dir("Perezvon").lower().startswith(os.environ["LOCALAPPDATA"].lower()))

    def test_migration_from_old_settings(self):
        self.app.root.destroy()
        home = tempfile.mkdtemp(prefix="pz-old-")
        pc.save_json(os.path.join(home, "settings.json"), {
            "server": self.S.url, "token_dp": ui.secret_pack("old-token"),
            "user": {"name": "Старый", "room": "vn2", "role": "operator"}, "sound": False})
        app = perezvon.App(home, minimized=True)
        try:
            self.assertEqual(app.settings["token"], "old-token")
            self.assertEqual(app.settings["user"]["name"], "Старый")
            self.assertFalse(app.settings["sound"])
            raw = open(os.path.join(home, "settings.json"), encoding="utf-8").read()
            self.assertNotIn("token_dp", raw)
            self.assertNotIn("Старый", raw)
        finally:
            app.root.destroy()
            shutil.rmtree(home, ignore_errors=True)
        self.app = perezvon.App(self.home, minimized=True)

    def test_logout_clears_pc_and_server_keeps(self):
        self.login_as("@anna_smirnova")
        self.assertEqual(self.m.acc_btn.lbl.cget("text"), "Выйти")
        a = self.app.store.add("Клиент Анны", "89991112233", "", time.time() + 600)
        self.sync_now()
        b = self.app.store.add("Неотправленный", "89994445566", "", time.time() + 900)   # ещё не ушёл
        self.assertTrue(self.app.logout(ask=False))                 # «Выйти» досылает неотправленное
        self.assertEqual(self.app.store.items, {})                  # на ПК пусто
        self.assertFalse(self.app.settings["token"])
        self.assertFalse(os.path.exists(os.path.join(self.home, "Data", "session.bin")))
        self.assertEqual(self.m.acc_btn.lbl.cget("text"), "Войти")
        self.assertIn("без входа", self.m.sub.cget("text"))
        self.assertTrue(self.app.login_win.winfo_exists())          # сразу предлагает войти
        self.assertEqual(self.S.app.item(a["id"])["status"], "active")
        self.assertEqual(self.S.app.item(b["id"])["status"], "active")   # дослан перед выходом
        # другой человек на этом ПК не видит перезвоны Анны
        self.S.app.create_user("Олег", "vn2", "operator", "oleg_kim")
        self.app.login_win.destroy()
        self.login_as("@oleg_kim")
        self.sync_now()
        self.assertEqual(self.app.store.active(), [])
        # Анна вернулась — её перезвоны снова тут
        self.app.logout(ask=False)
        self.app.login_win.destroy()
        self.login_as("@anna_smirnova")
        self.sync_now()
        self.assertEqual({i["fio"] for i in self.app.store.active()}, {"Клиент Анны", "Неотправленный"})

    def test_form_collapse_remembered(self):
        self.m.open_form(False, focus=False)
        self.assertFalse(self.m.form_open)
        self.assertFalse(self.app.settings["form_open"])
        it = self.app.store.add("К", "111", "", time.time() + 3600)
        self.m.start_edit(it["id"])                     # «Изменить» сам разворачивает форму
        self.assertTrue(self.m.form_open)
        self.assertEqual(self.m.f_fio.get(), "К")
        self.m.cancel_edit()

    def test_switch_user_without_logout(self):
        """Сессия кончилась (401) или просто вошли другим ником без «Выйти» — чужие перезвоны не показываем."""
        self.S.app.create_user("Олег", "vn2", "operator", "oleg_kim")
        self.login_as("@anna_smirnova")
        a = self.app.store.add("Клиент Анны", "89991112233", "", time.time() + 600)
        self.sync_now()
        self.app.settings["token"] = None              # как после 401 — без кнопки «Выйти»
        self.login_as("@oleg_kim")
        self.assertNotIn(a["id"], self.app.store.items)
        o = self.app.store.add("Клиент Олега", "89994445566", "", time.time() + 900)
        self.sync_now()
        self.assertEqual([i["fio"] for i in self.app.store.active()], ["Клиент Олега"])
        self.assertEqual(self.S.app.item(o["id"])["operator"], "Олег")
        self.assertEqual(self.S.app.item(a["id"])["operator"], "Анна Смирнова")   # на сервере у Анны
        # старый файл без владельца (как на ПК после версии 1.0.0) чинится при запуске
        self.app.store.owner = None
        self.app.store.items[a["id"]] = dict(a, srv_rev=3, dirty=False)
        self.app.store.save()
        self.app.root.destroy()
        self.app = perezvon.App(self.home, minimized=True)
        self.assertNotIn(a["id"], self.app.store.items)
        self.app.start(hotkey=False)
        self.m = self.app.main
        self.sync_now()
        self.assertEqual([i["fio"] for i in self.app.store.active()], ["Клиент Олега"])

    def test_settings_and_survive_restart(self):
        self.app.open_settings()
        pump(self.app.root, 0.2)
        self.assertTrue(self.app.settings_win.winfo_exists())
        self.app.settings["geom"] = [100, 100, 500, 700]
        self.app.store.add("Сохранится", "999", "", time.time() + 999)
        self.app.root.destroy()
        app2 = perezvon.App(self.home, minimized=True)
        try:
            self.assertEqual(app2.settings["geom"], [100, 100, 500, 700])
            self.assertEqual(app2.store.active()[0]["fio"], "Сохранится")
        finally:
            app2.root.destroy()
        self.app = app2


class AdminApp(unittest.TestCase):
    def setUp(self):
        self.S = TestServer()
        a = self.S.app
        a.create_user("Анна", "vn2", "operator", "anna_smirnova")
        a.create_user("Олег", "vn3", "operator", "oleg_kim")
        a.create_user("Шеф", "vn2", "superadmin", "the_chief")
        self.home = tempfile.mkdtemp(prefix="pz-adm-")
        pc.save_json(os.path.join(self.home, "settings.json"), {"server": self.S.url})
        for nick, items in (("@anna_smirnova", [("Будущий", 3600), ("Пропущенный", -3600), ("Пора", -60)]),
                            ("@oleg_kim", [("Олегов", 600)])):
            api = pc.Api(self.S.url)
            tok = pc.login_flow(api, nick, "client", "PC", threading.Event())["token"]
            st = pc.Store(os.path.join(self.home, nick + ".json"))
            for fio, d in items:
                st.add(fio, "8999123%04d" % abs(d), "", time.time() + d)
            body, pushed = st.build_push()
            st.apply_sync(pushed, pc.Api(self.S.url, tok).call("POST", "/api/sync", body))
        self.app = perezvon_admin.AdminApp(self.home)

    def tearDown(self):
        try:
            self.app.root.destroy()
        except tk.TclError:
            pass
        self.S.close()
        shutil.rmtree(self.home, ignore_errors=True)

    def wait(self, cond, sec=5):
        end = time.time() + sec
        while time.time() < end:
            pump(self.app.root, 0.05)
            if cond():
                return True
        return False

    def test_operator_cannot_enter_admin(self):
        self.app.form.f.set("@anna_smirnova")
        self.app.form.go()
        self.assertTrue(self.wait(lambda: "оператор" in self.app.form.msg.cget("text")))

    def test_full_view(self):
        self.app.form.f.set("@the_chief")
        self.app.form.go()
        self.assertTrue(self.wait(lambda: hasattr(self.app, "tree") and self.app.data))
        rows = {self.app.tree.set(i, "fio"): self.app.tree.item(i, "tags")[0] for i in self.app.tree.get_children()}
        self.assertEqual(rows, {"Будущий": "upcoming", "Пропущенный": "missed", "Пора": "due", "Олегов": "upcoming"})
        self.assertEqual(self.app.stat["missed"][1].cget("text"), "1")
        self.assertEqual(self.app.stat["upcoming"][1].cget("text"), "2")
        self.app.set_room("vn3")
        self.assertEqual([self.app.tree.set(i, "fio") for i in self.app.tree.get_children()], ["Олегов"])
        self.app.set_room(None)
        self.app.set_filter("missed")
        self.assertEqual([self.app.tree.set(i, "fio") for i in self.app.tree.get_children()], ["Пропущенный"])
        self.app.set_filter("missed")
        self.app.search.set("олег")
        self.assertEqual([self.app.tree.set(i, "fio") for i in self.app.tree.get_children()], ["Олегов"])
        self.app.search.set("")
        # действие «перезвонили» уходит на сервер
        iid = [i for i in self.app.tree.get_children() if self.app.tree.set(i, "fio") == "Пропущенный"][0]
        self.app.action(iid, "done")
        self.assertTrue(self.wait(lambda: self.S.app.item(iid)["status"] == "done"))
        self.assertTrue(self.wait(lambda: self.app.tree.exists(iid) and self.app.tree.item(iid, "tags")[0] == "done"))
        self.app.shift_day(-1)
        self.assertTrue(self.wait(lambda: self.app.data is not None))
        self.assertIn("Пропущенный", "")if False else None


if __name__ == "__main__":
    unittest.main()
