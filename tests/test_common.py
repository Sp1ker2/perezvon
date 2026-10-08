# -*- coding: utf-8 -*-
"""Время, номера, раскладка, хранилище — без сети и без окон."""
import datetime as dt
import json
import os
import shutil
import tempfile
import time
import unittest

import helpers  # noqa: F401  (пути)
import pz_common as pc

NOW = dt.datetime(2026, 10, 8, 14, 30, 0)


def mins(text, now=NOW):
    due, kind = pc.parse_when(text, now)
    return (due - now).total_seconds() / 60, kind


class ParseWhen(unittest.TestCase):
    def test_minutes(self):
        for text, m in [("15", 15), ("5", 5), (" 15 ", 15), ("15 мин", 15), ("15мин", 15), ("15 минут", 15),
                        ("15m", 15), ("через 15 минут", 15), ("1.5", 1.5), ("1,5", 1.5), ("90 мин", 90),
                        ("2 м", 2), ("45 МИН", 45)]:
            self.assertAlmostEqual(mins(text)[0], m, msg=text)
            self.assertEqual(mins(text)[1], "in")

    def test_hours(self):
        for text, m in [("1ч", 60), ("1 ч", 60), ("1 час", 60), ("2 часа", 120), ("5 часов", 300), ("1.5ч", 90),
                        ("1,5 часа", 90), ("1ч 30м", 90), ("1ч30", 90), ("1 ч 30 мин", 90), ("2h", 120),
                        ("час", 60), ("полчаса", 30), ("пол часа", 30), ("полтора часа", 90),
                        ("через час", 60), ("четверть часа", 15), ("30 сек", 0.5), ("1д", 1440)]:
            self.assertAlmostEqual(mins(text)[0], m, msg=text)

    def test_clock(self):
        due, kind = pc.parse_when("16:00", NOW)
        self.assertEqual((due, kind), (dt.datetime(2026, 10, 8, 16, 0), "at"))
        due, _ = pc.parse_when("в 16", NOW)
        self.assertEqual(due, dt.datetime(2026, 10, 8, 16, 0))
        due, _ = pc.parse_when("в 9.15", NOW)                  # уже прошло → завтра
        self.assertEqual(due, dt.datetime(2026, 10, 9, 9, 15))
        due, _ = pc.parse_when("14:30", NOW)                   # ровно сейчас → завтра, не «0 минут»
        self.assertEqual(due, dt.datetime(2026, 10, 9, 14, 30))
        due, _ = pc.parse_when("завтра в 10", NOW)
        self.assertEqual(due, dt.datetime(2026, 10, 9, 10, 0))
        due, _ = pc.parse_when("завтра 18:45", NOW)
        self.assertEqual(due, dt.datetime(2026, 10, 9, 18, 45))
        due, _ = pc.parse_when("в 23 часа", NOW)
        self.assertEqual(due, dt.datetime(2026, 10, 8, 23, 0))

    def test_errors(self):
        for text in ["", "   ", "abc", "завтра", "25:00", "в 12:61", "0", "0 мин", "15 лет", "-5", "8 дней",
                     "1ч ч", "через", "15:", "::"]:
            with self.assertRaises(pc.WhenError, msg=text):
                pc.parse_when(text, NOW)

    def test_error_texts_are_russian(self):
        try:
            pc.parse_when("абв", NOW)
        except pc.WhenError as e:
            self.assertIn("Примеры", str(e))

    def test_max(self):
        self.assertAlmostEqual(mins("7д")[0], 7 * 1440)
        with self.assertRaises(pc.WhenError):
            pc.parse_when("7д 1м", NOW)


class Format(unittest.TestCase):
    def test_duration(self):
        self.assertEqual(pc.fmt_duration(30), "30 с")
        self.assertEqual(pc.fmt_duration(60), "1 мин")
        self.assertEqual(pc.fmt_duration(3599), "59 мин")
        self.assertEqual(pc.fmt_duration(3600), "1 ч")
        self.assertEqual(pc.fmt_duration(3900), "1 ч 05 мин")
        self.assertEqual(pc.fmt_duration(86400 + 7200), "1 дн 2 ч")

    def test_left(self):
        t = 1_000_000.0
        self.assertEqual(pc.fmt_left(t + 299, t), "через 4:59")
        self.assertEqual(pc.fmt_left(t + 3900, t), "через 1 ч 05 мин")
        self.assertEqual(pc.fmt_left(t - 10, t), "пора звонить")
        self.assertEqual(pc.fmt_left(t - 180, t), "просрочено 3 мин")

    def test_clock_and_describe(self):
        now = NOW.timestamp()
        self.assertEqual(pc.fmt_clock(now + 900, now), "14:45")
        self.assertEqual(pc.fmt_clock(now + 86400, now), "завтра 14:30")
        self.assertEqual(pc.fmt_clock(now - 86400, now), "вчера 14:30")
        self.assertEqual(pc.describe_due(now + 900, now), "Напомню сегодня в 14:45 — через 15 мин")
        self.assertEqual(pc.describe_due(now + 86400 - 3600, now), "Напомню завтра в 13:30 — через 23 ч")

    def test_urgency(self):
        t = 1000.0
        self.assertEqual(pc.urgency(t + 600, t), "later")
        self.assertEqual(pc.urgency(t + 200, t), "soon")
        self.assertEqual(pc.urgency(t, t), "due")
        self.assertEqual(pc.urgency(t - 599, t), "due")
        self.assertEqual(pc.urgency(t - 601, t), "missed")
        self.assertEqual(pc.urgency(t - 601, t, "done"), "done")


class Phones(unittest.TestCase):
    def test_display(self):
        for raw in ["89991234567", "+7 999 123-45-67", "+7(999)1234567", "8 (999) 123 45 67", "9991234567",
                    "7-999-123-45-67"]:
            self.assertEqual(pc.phone_display(raw), "+7 (999) 123-45-67", raw)
        self.assertEqual(pc.phone_display("+44 7529 622562"), "+44 7529 622562")
        self.assertEqual(pc.phone_display("  1234 "), "1234")

    def test_copy(self):
        self.assertEqual(pc.phone_for_copy("8 (999) 123-45-67", "plus7"), "+79991234567")
        self.assertEqual(pc.phone_for_copy("+7 999 123-45-67", "8"), "89991234567")
        self.assertEqual(pc.phone_for_copy("8 (999) 123-45-67", "raw"), "8 (999) 123-45-67")
        self.assertEqual(pc.phone_for_copy("+44 7529 622562", "plus7"), "+447529622562")
        self.assertEqual(pc.phone_for_copy("доб. 123", "plus7"), "123")
        self.assertEqual(pc.phone_for_copy("+9991234567", "plus7"), "+9991234567")  # «+» → не российский

    def test_valid_and_clipboard(self):
        self.assertTrue(pc.phone_valid("123"))
        self.assertFalse(pc.phone_valid("12"))
        self.assertFalse(pc.phone_valid("абв"))
        self.assertTrue(pc.looks_like_phone("+7 (999) 123-45-67"))
        self.assertFalse(pc.looks_like_phone("Привет 89991234567"))
        self.assertFalse(pc.looks_like_phone("1234"))
        self.assertFalse(pc.looks_like_phone("8999\n1234567"))


class Layout(unittest.TestCase):
    def test_fix_layout(self):
        self.assertEqual(pc.fix_layout('"фттф_ыьшктщмф'), "@anna_smirnova")
        self.assertEqual(pc.fix_layout("ФТТФ"), "anna")
        self.assertEqual(pc.fix_layout("@anna"), "@anna")
        self.assertEqual(pc.fix_layout("1234-5678"), "1234-5678")

    def test_server_username_norm(self):
        import perezvon_server as s
        for raw in ["@Anna_Smirnova", "anna_smirnova", " https://t.me/anna_smirnova ", "фттф_ыьшктщмф",
                    '"фттф_ыьшктщмф']:
            self.assertEqual(s.norm_username(raw), "anna_smirnova", raw)
        for bad in ["", "@", "ann", "анна!", "1anna", "a" * 40, "@anna smirnova"]:
            self.assertIsNone(s.norm_username(bad), bad)

    def test_code(self):
        self.assertTrue(pc.is_code("1234-5678-9012"))
        self.assertTrue(pc.is_code("1234 5678 9012"))
        self.assertFalse(pc.is_code("1234-5678-901"))
        self.assertFalse(pc.is_code("@anna"))


class Storage(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.p = os.path.join(self.d, "x.json")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_roundtrip_and_bak(self):
        pc.save_json(self.p, {"a": 1})
        pc.save_json(self.p, {"a": 2})
        self.assertEqual(pc.load_json(self.p, {}), {"a": 2})
        self.assertEqual(json.load(open(self.p + ".bak", encoding="utf-8")), {"a": 1})

    def test_corrupt_falls_back_to_bak(self):
        pc.save_json(self.p, {"a": 1})
        pc.save_json(self.p, {"a": 2})
        with open(self.p, "w", encoding="utf-8") as f:
            f.write('{"a": 3,,,')          # обрыв записи
        self.assertEqual(pc.load_json(self.p, {}), {"a": 1})
        self.assertTrue(any(n.startswith("x.json.corrupt-") for n in os.listdir(self.d)))

    def test_missing_and_wrong_type(self):
        self.assertEqual(pc.load_json(self.p, {"d": 1}), {"d": 1})
        with open(self.p, "w", encoding="utf-8") as f:
            f.write("[1,2,3]")
        self.assertEqual(pc.load_json(self.p, {}), {})

    def test_store_persists_unicode(self):
        st = pc.Store(self.p)
        it = st.add("Ёжиков Пётр «Тест» 🙂", "8 999 123-45-67", "перезвонить\nпро\tдоговор", time.time() + 60)
        st2 = pc.Store(self.p)
        self.assertEqual(st2.items[it["id"]]["fio"], "Ёжиков Пётр «Тест» 🙂")
        self.assertEqual(st2.items[it["id"]]["note"], "перезвонить про договор")

    def test_store_limits(self):
        st = pc.Store(self.p)
        it = st.add("я" * 1000, "1" * 500, "з" * 5000, time.time())
        self.assertEqual(len(it["fio"]), pc.LIMITS["fio"])
        self.assertEqual(len(it["phone"]), pc.LIMITS["phone"])
        self.assertEqual(len(it["note"]), pc.LIMITS["note"])

    def test_store_flow(self):
        st = pc.Store(self.p)
        t = 1_000_000.0
        a = st.add("А", "111", "", t + 100, now=t)
        b = st.add("Б", "222", "", t + 50, now=t)
        self.assertEqual([i["id"] for i in st.active()], [b["id"], a["id"]])
        self.assertEqual(st.due_items(t + 60), [st.items[b["id"]]])
        st.snooze(b["id"], 300, now=t + 60, attempt=True)
        self.assertEqual(st.items[b["id"]]["due"], t + 360)
        self.assertEqual(st.items[b["id"]]["attempts"], 1)
        self.assertEqual(st.items[b["id"]]["snoozes"], 1)
        st.done(a["id"], now=time.time())
        self.assertEqual(len(st.done_today()), 1)
        st.restore(a["id"])
        self.assertEqual(st.items[a["id"]]["status"], "active")
        self.assertIsNone(st.items[a["id"]]["done_at"])
        lv = st.items[a["id"]]["lver"]
        st.delete(a["id"])
        self.assertEqual(st.items[a["id"]]["lver"], lv + 1)
        self.assertNotIn(a["id"], [i["id"] for i in st.active()])

    def test_adopt_owner(self):
        st = pc.Store(self.p)
        local = st.add("Без входа", "111", "", time.time() + 60)          # создан до входа
        synced = st.add("С сервера", "222", "", time.time() + 60)
        st.items[synced["id"]].update(srv_rev=5, dirty=False)
        st.since = 9
        self.assertTrue(st.adopt(1))                  # старый список без владельца
        self.assertIn(local["id"], st.items)          # неотправленное своё — остаётся
        self.assertNotIn(synced["id"], st.items)      # пришедшее с сервера — скачаем заново
        self.assertEqual((st.owner, st.since), (1, 0))
        self.assertFalse(st.adopt(1))                 # тот же человек — ничего не трогаем
        self.assertIn(local["id"], st.items)
        self.assertTrue(st.adopt(2))                  # другой человек — чужого на ПК не остаётся
        self.assertEqual(st.items, {})
        self.assertEqual(pc.Store(self.p).owner, 2)   # запомнено на диске
        st.clear()
        self.assertIsNone(st.owner)

    def test_purge_keeps_unsent(self):
        st = pc.Store(self.p)
        a = st.add("А", "111", "", time.time())
        st.delete(a["id"])
        st.purge()
        self.assertIn(a["id"], st.items)            # ещё не ушло на сервер — не теряем
        st.items[a["id"]]["dirty"] = False
        st.purge()
        self.assertNotIn(a["id"], st.items)


if __name__ == "__main__":
    unittest.main()
