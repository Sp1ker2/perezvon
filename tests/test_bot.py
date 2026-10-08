# -*- coding: utf-8 -*-
"""Telegram-бот на поддельном API: владелец, создание пользователей и комнат, роли, права, напоминания."""
import re
import time
import unittest

from helpers import TestServer, tg_msg, tg_cb

OWNER = 8267218455


class BotBase(unittest.TestCase):
    def setUp(self):
        self.S = TestServer(bot=True, cfg={"owners": [OWNER]})
        self.app, self.bot, self.tg = self.S.app, self.S.app.bot, self.S.tg

    def tearDown(self):
        self.S.close()

    def say(self, chat, text, username=None, first="Тест"):
        self.bot.handle(tg_msg(chat, text, username, first))
        return self.tg.last_text(chat)

    def press(self, chat, data):
        self.bot.handle(tg_cb(chat, data))
        return self.tg.last_text(chat)

    def btn(self, chat, label_part):
        for b in self.tg.last_buttons(chat):
            if label_part in b["text"]:
                return b["callback_data"]
        self.fail("нет кнопки «%s» среди %s" % (label_part, [b["text"] for b in self.tg.last_buttons(chat)]))

    def owner(self):
        self.say(OWNER, "/start", "boss_owner", "Хозяин")
        return self.app.user_by_chat(OWNER)


class Owner(BotBase):
    def test_owner_bootstrap(self):
        t = self.say(OWNER, "/start", "boss_owner", "Хозяин")
        u = self.app.user_by_chat(OWNER)
        self.assertEqual(u["role"], "superadmin")
        self.assertEqual(u["tg_username"], "boss_owner")
        texts = "\n".join(m["text"] for m in self.tg.sent(OWNER))
        self.assertIn("владелец", texts)
        self.assertIn("Супер-админ".lower(), t.lower())
        labels = [b["text"] for b in self.tg.last_buttons(OWNER)]
        self.assertTrue(any("Пользователи" in x for x in labels))
        self.assertTrue(any("Создать пользователя" in x for x in labels))

    def test_stranger(self):
        t = self.say(42, "/start", "random_guy")
        self.assertIn("нет в списке", t)
        self.assertIn("@random_guy", t)
        t = self.say(43, "привет")
        self.assertIn("не задан ник", t)
        self.assertIsNone(self.app.user_by_chat(42))
        self.bot.handle(tg_cb(42, "users"))
        self.assertTrue(any(m == "answerCallbackQuery" for m, _ in self.tg.calls))
        self.assertNotIn("Пользователи", self.tg.last_text(42))


class CreateUser(BotBase):
    def test_wizard_full(self):
        self.owner()
        self.press(OWNER, "nu")
        self.assertIn("Ник нового пользователя", self.tg.last_text(OWNER))
        t = self.say(OWNER, "ня")                         # слишком короткий/кривой
        self.assertIn("❌", t)
        self.say(OWNER, "@Anna_Smirnova")
        t = self.say(OWNER, "Анна Смирнова")
        self.assertIn("Комната", t)
        self.press(OWNER, self.btn(OWNER, "Новая комната"))
        t = self.say(OWNER, "vn5")
        self.assertIn("Роль", t)
        self.assertIn("Супер-админ", t)
        t = self.press(OWNER, "nuro:operator")
        self.assertIn("Пользователь создан", t)
        self.assertIn("@anna_smirnova", t)
        self.assertIn("ввести <code>@anna_smirnova</code>", t)
        u = self.app.user_by_username("anna_smirnova")
        self.assertEqual((u["name"], u["room"], u["role"]), ("Анна Смирнова", "vn5", "operator"))

    def test_wizard_no_username_gives_code(self):
        self.owner()
        self.press(OWNER, "nu")
        self.say(OWNER, "-")
        self.say(OWNER, "Без Ника")
        self.press(OWNER, self.btn(OWNER, "vn2"))
        t = self.press(OWNER, "nuro:admin")
        m = re.search(r"<code>(\d{4}-\d{4}-\d{4})</code>", t)
        self.assertTrue(m, t)
        from helpers import srv  # noqa
        import pz_common as pc
        api = pc.Api(self.S.url)
        res = api.call("POST", "/api/login", {"login": m.group(1), "app": "admin"})
        self.assertEqual(res["user"]["name"], "Без Ника")

    def test_one_line(self):
        self.owner()
        t = self.say(OWNER, "/adduser @oleg_kim; Олег Ким; vn2; админ")
        self.assertIn("Пользователь создан", t)
        t = self.say(OWNER, "/adduser @super_two; Второй; vn9; супер-админ")
        self.assertEqual(self.app.user_by_username("super_two")["role"], "superadmin")
        self.assertIn("vn9", [r["name"] for r in self.app.rooms()])
        t = self.say(OWNER, "/adduser @oleg_kim; Двойник; vn2; оператор")
        self.assertIn("уже занят", t)
        t = self.say(OWNER, "/adduser @x_user_1; Кто; vn2; директор")
        self.assertIn("Роль не понял", t)
        t = self.say(OWNER, "/adduser мусор")
        self.assertIn("Формат", t)
        t = self.say(OWNER, "/adduser Без ника; vn2; оператор")
        self.assertIn("Код входа", t)

    def test_quick_by_nick(self):
        self.owner()
        t = self.say(OWNER, "@new_person")
        self.assertIn("ещё нет", t)
        self.say(OWNER, "Новый Человек")
        self.press(OWNER, self.btn(OWNER, "vn2"))
        self.press(OWNER, "nuro:operator")
        self.assertEqual(self.app.user_by_username("new_person")["name"], "Новый Человек")
        t = self.say(OWNER, "@new_person")                # существующий → карточка
        self.assertIn("Новый Человек", t)
        self.assertIn("Ник:", t)

    def test_cancel(self):
        self.owner()
        self.press(OWNER, "nu")
        self.say(OWNER, "/cancel")
        self.assertIsNone(self.bot.state.get(OWNER))
        n = len(self.app.users())
        self.say(OWNER, "Просто текст")
        self.assertEqual(len(self.app.users()), n)


class UserCard(BotBase):
    def setUp(self):
        super().setUp()
        self.owner()
        self.say(OWNER, "/adduser @anna_smirnova; Анна; vn2; оператор")
        self.uid = self.app.user_by_username("anna_smirnova")["id"]

    def test_card_actions(self):
        t = self.press(OWNER, "u:%d" % self.uid)
        self.assertIn("@anna_smirnova", t)
        self.assertIn("ещё не нажал(а) «Старт»", t)
        self.press(OWNER, "uro:%d" % self.uid)
        t = self.press(OWNER, "uro:%d:superadmin" % self.uid)
        self.assertEqual(self.app.user(self.uid)["role"], "superadmin")
        self.press(OWNER, "ur:%d" % self.uid)
        self.press(OWNER, "ur:%d:new" % self.uid)
        self.say(OWNER, "Зал 3")
        self.assertEqual(self.app.user(self.uid)["room"], "Зал 3")
        self.press(OWNER, "un:%d" % self.uid)
        self.say(OWNER, "Анна Петровна")
        self.assertEqual(self.app.user(self.uid)["name"], "Анна Петровна")
        self.press(OWNER, "ug:%d" % self.uid)
        self.say(OWNER, "@anna_p")
        self.assertEqual(self.app.user(self.uid)["tg_username"], "anna_p")
        self.press(OWNER, "uc:%d" % self.uid)
        t = self.press(OWNER, "uc:%d:y" % self.uid)
        self.assertRegex(t, r"\d{4}-\d{4}-\d{4}")
        self.press(OWNER, "ub:%d" % self.uid)
        self.assertEqual(self.app.user(self.uid)["blocked"], 1)
        self.press(OWNER, "ub:%d" % self.uid)
        self.assertEqual(self.app.user(self.uid)["blocked"], 0)
        self.press(OWNER, "ud:%d" % self.uid)
        self.assertIsNotNone(self.app.user(self.uid))           # без подтверждения не удаляет
        self.press(OWNER, "ud:%d:y" % self.uid)
        self.assertIsNone(self.app.user(self.uid))

    def test_owner_cannot_be_demoted_via_bot(self):
        oid = self.app.user_by_chat(OWNER)["id"]
        self.say(OWNER, "/adduser @second_super; Второй; vn2; супер")
        t = self.press(OWNER, "uro:%d:operator" % oid)
        self.assertIn("владелец", t)
        self.assertEqual(self.app.user(oid)["role"], "superadmin")

    def test_user_presses_start_and_gets_linked(self):
        self.say(700, "/start", "Anna_Smirnova", "Анна")
        t = "\n".join(m["text"] for m in self.tg.sent(700))
        self.assertIn("вы подключены", t)
        self.assertIn("@anna_smirnova", t)
        self.assertEqual(self.app.user(self.uid)["tg_chat"], 700)
        labels = [b["text"] for b in self.tg.last_buttons(700)]
        self.assertTrue(any("Мои перезвоны" in x for x in labels))
        self.assertFalse(any("Пользователи" in x for x in labels))
        # оператор не может управлять
        self.bot.handle(tg_cb(700, "users"))
        self.bot.handle(tg_cb(700, "u:%d" % self.uid))
        self.say(700, "/adduser @hack_er; Х; vn2; супер")
        self.assertIsNone(self.app.user_by_username("hack_er"))


class Rights(BotBase):
    def test_admin_sees_but_cannot_manage(self):
        self.owner()
        self.say(OWNER, "/adduser @plain_admin; Админ Простой; vn2; админ")
        self.say(800, "/start", "plain_admin")
        labels = [b["text"] for b in self.tg.last_buttons(800)]
        self.assertTrue(any("Сегодня" in x for x in labels))
        self.assertTrue(any("Пропущенные" in x for x in labels))
        self.assertFalse(any("Пользователи" in x for x in labels))
        t = self.press(800, "missed")
        self.assertIn("Пропущенные", t)
        # перезвоны двух комнат: админ vn2 видит только vn2
        import pz_common as pc
        import threading
        self.say(OWNER, "/adduser @op_vn2; Оп Два; vn2; оператор")
        self.say(OWNER, "/adduser @op_vn3; Оп Три; vn3; оператор")
        for nick, fio in (("@op_vn2", "Клиент двойки"), ("@op_vn3", "Клиент тройки")):
            tok = pc.login_flow(pc.Api(self.S.url), nick, "client", "PC", threading.Event())["token"]
            pc.Api(self.S.url, tok).call("POST", "/api/sync", {"now": time.time(), "items": [{
                "id": (("a" if "vn2" in nick else "b") * 32), "fio": fio, "phone": "123", "due": time.time() - 3600,
                "created": time.time() - 4000, "status": "active", "base": 0}]})
        t = self.press(800, "today")
        self.assertIn("комната vn2", t)
        self.assertNotIn("vn3", t)
        t = self.press(800, "missed")
        self.assertIn("Клиент двойки", t)
        self.assertNotIn("Клиент тройки", t)
        t = self.press(OWNER, "missed")                         # супер-админ — все комнаты
        self.assertIn("Клиент двойки", t)
        self.assertIn("Клиент тройки", t)
        self.bot.handle(tg_cb(800, "it:d:" + "a" * 32))          # админ не отмечает чужое
        self.assertEqual(self.app.item("a" * 32)["status"], "active")
        n = len(self.app.users())
        self.bot.handle(tg_cb(800, "nu"))
        self.say(800, "/adduser @hack_er; Х; vn2; супер")
        self.assertEqual(len(self.app.users()), n)

    def test_rooms(self):
        self.owner()
        self.press(OWNER, "rooms")
        self.press(OWNER, "nr")
        self.say(OWNER, "Колл-центр")
        rid = [r["id"] for r in self.app.rooms() if r["name"] == "Колл-центр"][0]
        self.press(OWNER, "rn:%d" % rid)
        self.say(OWNER, "КЦ-1")
        self.assertIn("КЦ-1", [r["name"] for r in self.app.rooms()])
        self.press(OWNER, "rd:%d" % rid)
        self.press(OWNER, "rd:%d:y" % rid)
        self.assertNotIn("КЦ-1", [r["name"] for r in self.app.rooms()])
        vn2 = [r["id"] for r in self.app.rooms() if r["name"] == "vn2"][0]
        t = self.press(OWNER, "rd:%d:y" % vn2)                # там владелец
        self.assertIn("сначала переведите", t)


class Reminders(BotBase):
    def test_buttons_under_reminder(self):
        self.owner()
        self.say(OWNER, "/adduser @anna_smirnova; Анна; vn2; оператор")
        self.say(700, "/start", "anna_smirnova")
        import pz_common as pc
        import threading
        api = pc.Api(self.S.url)
        tok = pc.login_flow(api, "@anna_smirnova", "client", "PC", threading.Event())["token"]
        a = pc.Api(self.S.url, tok)
        iid = "d" * 32
        a.call("POST", "/api/sync", {"now": time.time(), "items": [
            {"id": iid, "fio": "Клиент", "phone": "89990001122", "due": time.time() - 1, "created": time.time() - 600,
             "status": "active", "base": 0}]})
        import perezvon_server as srv
        srv.notify_once(self.app)
        self.assertIn("Пора перезвонить", self.tg.last_text(700))
        t = self.press(700, "it:s:" + iid)
        self.assertIn("Отложено", t)
        self.assertGreater(self.app.item(iid)["due"], time.time() + 800)
        t = self.press(700, "it:d:" + iid)
        self.assertIn("Перезвонили", t)
        self.assertEqual(self.app.item(iid)["done_by"], "tg:Анна")
        t = self.press(700, "it:d:" + iid)                       # повторное нажатие
        self.assertIn("Перезвонили", t)
        # чужой не может нажать
        self.say(OWNER, "/adduser @oleg_kim; Олег; vn2; оператор")
        self.say(701, "/start", "oleg_kim")
        self.app.item_action(iid, "restore", "x")
        self.bot.handle(tg_cb(701, "it:d:" + iid))
        self.assertEqual(self.app.item(iid)["status"], "active")
        # оператор видит «Мои на сегодня»
        t = self.press(700, "today")
        self.assertIn("Клиент", t)
        t = self.press(701, "today")
        self.assertNotIn("Клиент", t)
        t = self.press(OWNER, "today")
        self.assertIn("vn2", t)

    def test_notify_toggle(self):
        self.owner()
        self.press(OWNER, "notify")
        self.assertEqual(self.app.user_by_chat(OWNER)["tg_notify"], 0)
        self.press(OWNER, "notify")
        self.assertEqual(self.app.user_by_chat(OWNER)["tg_notify"], 1)


class Robustness(BotBase):
    def test_garbage_updates(self):
        self.owner()
        for up in [{}, {"message": {}}, {"message": {"chat": {"id": 1, "type": "group"}, "text": "/start"}},
                   {"message": {"chat": {"id": 5, "type": "private"}, "photo": []}},
                   tg_cb(OWNER, "u:999999"), tg_cb(OWNER, "zzz"), tg_cb(OWNER, "ur:abc"),
                   tg_cb(OWNER, "it:d:" + "0" * 32), tg_cb(OWNER, "nuro:admin"), tg_cb(OWNER, "rd:99999:y"),
                   tg_cb(OWNER, "lg:y:" + "a" * 20)]:
            try:
                self.bot.handle(up)
            except Exception as e:            # бот не должен падать ни на чём
                if not isinstance(e, (KeyError, TypeError)) or up:
                    raise

    def test_html_injection_escaped(self):
        self.owner()
        self.say(OWNER, "/adduser @evil_user; <b>Злой</b> & <i>; vn2; оператор")
        t = self.tg.last_text(OWNER)
        self.assertIn("&lt;b&gt;Злой&lt;/b&gt; &amp;", t)


if __name__ == "__main__":
    unittest.main()
