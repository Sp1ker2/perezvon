# -*- coding: utf-8 -*-
"""Сервер по HTTP: вход, роли, синхронизация, конфликты, админ-вид, мусор на входе, нагрузка."""
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
import urllib.request
import uuid

from helpers import TestServer
import pz_common as pc


def sync(api, store):
    body, pushed = store.build_push()
    res = api.call("POST", "/api/sync", body)
    return store.apply_sync(pushed, res), res


class Base(unittest.TestCase):
    cfg = None

    def setUp(self):
        self.S = TestServer(cfg=self.cfg)
        self.app = self.S.app
        self.tmp = tempfile.mkdtemp()
        self.anna, _ = self.app.create_user("Анна Смирнова", "vn2", "operator", "@anna_smirnova")
        self.oleg, _ = self.app.create_user("Олег Ким", "vn3", "operator", "oleg_kim")
        self.boss, self.boss_code = self.app.create_user("Начальник", "vn2", "admin", "boss_one")
        self.sup, _ = self.app.create_user("Главный", "vn2", "superadmin", "super_one")

    def tearDown(self):
        self.S.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def login(self, login, app="client"):
        api = pc.Api(self.S.url)
        res = pc.login_flow(api, login, app, "PC-TEST", threading.Event())
        return pc.Api(self.S.url, res["token"]), res

    def store(self, name):
        return pc.Store(os.path.join(self.tmp, name + ".json"))


class Login(Base):
    def test_by_username_any_form(self):
        for form in ["@anna_smirnova", "anna_smirnova", "Anna_Smirnova", '"фттф_ыьшктщмф', "https://t.me/anna_smirnova"]:
            api, res = self.login(form)
            self.assertEqual(res["user"]["name"], "Анна Смирнова", form)
            self.assertEqual(res["user"]["username"], "anna_smirnova")

    def test_unknown_and_garbage(self):
        api = pc.Api(self.S.url)
        with self.assertRaises(pc.ApiError) as c:
            api.call("POST", "/api/login", {"login": "@nobody_here"})
        self.assertEqual(c.exception.status, 403)
        self.assertIn("@nobody_here нет в списке", c.exception.message)
        with self.assertRaises(pc.ApiError) as c:
            api.call("POST", "/api/login", {"login": "!!"})
        self.assertEqual(c.exception.status, 400)

    def test_code_login(self):
        api, res = self.login(self.boss_code, "admin")
        self.assertEqual(res["user"]["role"], "admin")
        with self.assertRaises(pc.ApiError):
            self.login("0000-0000-0000")

    def test_admin_app_requires_admin_role(self):
        with self.assertRaises(pc.ApiError) as c:
            self.login("@anna_smirnova", "admin")
        self.assertEqual(c.exception.status, 403)
        self.assertIn("оператор", c.exception.message)
        self.login("@boss_one", "admin")
        self.login("@super_one", "admin")

    def test_blocked(self):
        api, _ = self.login("@anna_smirnova")
        self.app.update_user(self.anna["id"], blocked=1)
        with self.assertRaises(pc.ApiError) as c:
            api.call("POST", "/api/sync", {"items": []})
        self.assertEqual(c.exception.status, 401)           # программа разлогинена
        with self.assertRaises(pc.ApiError) as c:
            self.login("@anna_smirnova")
        self.assertIn("отключён", c.exception.message)

    def test_bruteforce_limit(self):
        api = pc.Api(self.S.url)
        for _ in range(10):
            with self.assertRaises(pc.ApiError):
                api.call("POST", "/api/login", {"login": "@nobody_%d" % _ if _ < 0 else "@nobody_xx"})
        with self.assertRaises(pc.ApiError) as c:
            api.call("POST", "/api/login", {"login": "@anna_smirnova"})
        self.assertEqual(c.exception.status, 429)

    def test_logout(self):
        api, _ = self.login("@anna_smirnova")
        api.call("POST", "/api/logout", {})
        with self.assertRaises(pc.ApiError) as c:
            api.call("POST", "/api/sync", {"items": []})
        self.assertEqual(c.exception.status, 401)


class LoginConfirm(Base):
    """Режим «login_confirm»: вход по нику ждёт кнопку в Telegram (проверяем логику без бота)."""
    cfg = {"login_confirm": True}

    def test_need_start_then_confirm(self):
        api = pc.Api(self.S.url)
        with self.assertRaises(pc.ApiError) as c:
            api.call("POST", "/api/login", {"login": "@anna_smirnova"})
        self.assertTrue(c.exception.data.get("need_start"))
        # «нажала Старт»
        self.app.link_by_username("anna_smirnova", 777, "Анна")

        class FakeBot:
            username = "b"
            asked = []

            def ask_login(self, rid, u, app, pc_):
                FakeBot.asked.append(rid)
        self.app.bot = FakeBot()
        res = api.call("POST", "/api/login", {"login": "@anna_smirnova", "pc": "PC1"})
        rid = res["pending"]
        for _ in range(100):                    # запрос в Telegram уходит из фонового потока
            if FakeBot.asked:
                break
            time.sleep(0.02)
        self.assertEqual(FakeBot.asked, [rid])
        self.assertEqual(api.call("POST", "/api/login/poll", {"id": rid})["status"], "pending")
        self.assertIn("устарел", self.app.login_answer(rid, 999, True))     # чужой чат не подтвердит
        self.assertIn("подтверждён", self.app.login_answer(rid, 777, True))
        r = api.call("POST", "/api/login/poll", {"id": rid})
        self.assertEqual(r["status"], "ok")
        self.assertTrue(r["token"])
        with self.assertRaises(pc.ApiError):                                 # одноразовый
            api.call("POST", "/api/login/poll", {"id": rid})
        # отказ
        rid2 = api.call("POST", "/api/login", {"login": "@anna_smirnova"})["pending"]
        self.app.login_answer(rid2, 777, False)
        with self.assertRaises(pc.ApiError) as c:
            api.call("POST", "/api/login/poll", {"id": rid2})
        self.assertIn("отклонён", c.exception.message)
        self.app.bot = None

    def test_code_skips_confirm(self):
        self.login(self.boss_code, "admin")


class Sync(Base):
    def test_items_follow_user_to_any_pc(self):
        api1, _ = self.login("@anna_smirnova")
        st1 = self.store("pc1")
        it = st1.add("Иванов", "89991234567", "договор", time.time() + 600)
        sync(api1, st1)
        self.assertEqual(st1.dirty_count(), 0)
        self.assertEqual(st1.items[it["id"]]["operator"], "Анна Смирнова")
        self.assertEqual(st1.items[it["id"]]["room"], "vn2")
        # другой компьютер — тот же ник → те же перезвоны
        api2, _ = self.login("anna_smirnova")
        st2 = self.store("pc2")
        sync(api2, st2)
        self.assertIn(it["id"], st2.items)
        self.assertEqual(st2.items[it["id"]]["fio"], "Иванов")
        # правка на втором видна на первом
        st2.done(it["id"])
        sync(api2, st2)
        ext, _ = sync(api1, st1)
        self.assertEqual(st1.items[it["id"]]["status"], "done")
        self.assertIn(it["id"], ext)

    def test_operators_isolated(self):
        a, _ = self.login("@anna_smirnova")
        o, _ = self.login("@oleg_kim")
        sa, so = self.store("a"), self.store("o")
        x = sa.add("Клиент Анны", "111", "", time.time() + 60)
        sync(a, sa)
        sync(o, so)
        self.assertNotIn(x["id"], so.items)
        # Олег не может изменить чужой перезвон, даже зная id
        body = {"now": time.time(), "since": 0, "items": [dict(sa.items[x["id"]], status="deleted", base=1)]}
        res = o.call("POST", "/api/sync", body)
        self.assertIn(x["id"], res["rejected"])
        self.assertEqual(self.app.item(x["id"])["status"], "active")

    def test_admin_change_wins_and_reaches_operator(self):
        a, _ = self.login("@anna_smirnova")
        st = self.store("a")
        x = st.add("К", "222", "", time.time() + 600)
        sync(a, st)
        self.app.item_action(x["id"], "done", "admin:Начальник")
        st.update(x["id"], note="правка оператора")       # конфликтующая правка с устаревшей базой
        ext, res = sync(a, st)
        self.assertEqual(st.items[x["id"]]["status"], "done")
        self.assertFalse(st.items[x["id"]]["dirty"])
        self.assertEqual(self.app.item(x["id"])["done_by"], "admin:Начальник")

    def test_edit_during_flight_not_lost(self):
        a, _ = self.login("@anna_smirnova")
        st = self.store("a")
        x = st.add("К", "333", "", time.time() + 600)
        body, pushed = st.build_push()
        st.update(x["id"], note="новая заметка")           # пока запрос «летит»
        res = a.call("POST", "/api/sync", body)
        st.apply_sync(pushed, res)
        self.assertTrue(st.items[x["id"]]["dirty"])
        sync(a, st)
        self.assertEqual(self.app.item(x["id"])["note"], "новая заметка")
        self.assertFalse(st.items[x["id"]]["dirty"])

    def test_epoch_reset_resends_everything(self):
        a, _ = self.login("@anna_smirnova")
        st = self.store("a")
        x = st.add("К", "444", "", time.time() + 600)
        sync(a, st)
        st.epoch = "другая-база"
        st.save()
        _, res = sync(a, st)
        self.assertTrue(res.get("reset"))
        self.assertTrue(st.items[x["id"]]["dirty"])
        self.assertEqual(st.since, 0)
        sync(a, st)
        self.assertFalse(st.items[x["id"]]["dirty"])

    def test_clock_skew(self):
        a, _ = self.login("@anna_smirnova")
        st = self.store("a")
        fake_now = time.time() - 3600                      # часы ПК отстают на час
        x = st.add("К", "555", "", fake_now + 900, now=fake_now)
        body, pushed = st.build_push(now=fake_now)
        res = a.call("POST", "/api/sync", body)
        st.apply_sync(pushed, res)
        srv_due = self.app.item(x["id"])["due"]
        self.assertAlmostEqual(srv_due, time.time() + 900, delta=5)        # на сервере — верное время
        self.assertAlmostEqual(st.items[x["id"]]["due"], fake_now + 900, delta=5)  # у ПК — его время
        # админ отложил на 15 мин → ПК получает в своих часах
        self.app.item_action(x["id"], "snooze15", "admin:x")
        body, pushed = st.build_push(now=fake_now)
        st.apply_sync(pushed, a.call("POST", "/api/sync", body))
        self.assertAlmostEqual(st.items[x["id"]]["due"], fake_now + 900, delta=5)

    def test_garbage_items_ignored(self):
        a, _ = self.login("@anna_smirnova")
        bad = [None, 5, "x", {}, {"id": "../../etc"}, {"id": "a" * 32, "due": "NaN"},
               {"id": "b" * 32, "due": float("inf") if False else "inf"},
               {"id": "c" * 32, "due": 1, "status": "hacked", "fio": "<b>x</b>" * 100, "attempts": -5}]
        res = a.call("POST", "/api/sync", {"now": time.time(), "items": bad})
        self.assertEqual(res["accepted"], ["c" * 32])
        it = self.app.item("c" * 32)
        self.assertEqual(it["status"], "active")
        self.assertEqual(it["attempts"], 0)
        self.assertLessEqual(len(it["fio"]), 200)

    def test_bad_requests(self):
        a, _ = self.login("@anna_smirnova")
        for raw in (b"not json", b"[1,2]", b"\xff\xfe"):
            req = urllib.request.Request(self.S.url + "/api/sync", data=raw, method="POST",
                                         headers={"Authorization": "Bearer " + a.token})
            with self.assertRaises(urllib.error.HTTPError) as c:
                urllib.request.urlopen(req)
            self.assertEqual(c.exception.code, 400)
        with self.assertRaises(pc.ApiError) as c:
            a.call("POST", "/api/sync", {"items": [{}] * 501})
        self.assertEqual(c.exception.status, 400)
        with self.assertRaises(pc.ApiError) as c:
            pc.Api(self.S.url, "fake-token").call("POST", "/api/sync", {})
        self.assertEqual(c.exception.status, 401)
        self.assertEqual(pc.Api(self.S.url).call("GET", "/api/ping")["ok"], True)
        with self.assertRaises(pc.ApiError) as c:
            pc.Api(self.S.url).call("GET", "/api/nothing")
        self.assertEqual(c.exception.status, 401)

    def test_401_never_drops_connection(self):
        """Раньше сервер отвечал 401, не дочитав тело, — на Windows это иногда рвало соединение (статус 0)."""
        api = pc.Api(self.S.url, "fake-token")
        body = {"items": [{"id": "a" * 32, "fio": "x" * 150, "due": 1}] * 40}
        for _ in range(50):
            with self.assertRaises(pc.ApiError) as c:
                api.call("POST", "/api/sync", body)
            self.assertEqual(c.exception.status, 401)

    def test_concurrency(self):
        """20 «компьютеров» × 15 синхронизаций с правками одновременно: ничего не теряется, ревизии уникальны."""
        users = []
        for i in range(20):
            self.app.create_user("Оп %d" % i, "vn%d" % (i % 3), "operator", "op_user_%02d" % i)
            users.append("@op_user_%02d" % i)
        errors = []
        made = {}

        def worker(i):
            try:
                api, _ = self.login(users[i])
                st = self.store("c%d" % i)
                ids = []
                for k in range(15):
                    ids.append(st.add("К%d-%d" % (i, k), "8999%07d" % k, "", time.time() + 60 * k)["id"])
                    if k % 3 == 0 and ids:
                        st.done(ids[0])
                    sync(api, st)
                sync(api, st)
                made[i] = (ids, st)
            except Exception as e:
                errors.append(repr(e))
        th = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        [t.start() for t in th]
        [t.join(60) for t in th]
        self.assertEqual(errors, [])
        n = self.app.db.one("SELECT COUNT(*) FROM items")[0]
        self.assertEqual(n, 20 * 15)
        revs = [r[0] for r in self.app.db.q("SELECT rev FROM items")]
        self.assertEqual(len(revs), len(set(revs)))
        for i, (ids, st) in made.items():
            self.assertEqual(st.dirty_count(), 0)
            self.assertEqual(self.app.item(ids[0])["status"], "done")


class AdminView(Base):
    def test_day_view_and_actions(self):
        a, _ = self.login("@anna_smirnova")
        o, _ = self.login("@oleg_kim")
        sa, so = self.store("a"), self.store("o")
        now = time.time()
        upcoming = sa.add("Будущий", "111", "", now + 3600)
        missed = sa.add("Пропущенный", "222", "", now - 3600)
        theirs = so.add("Олегов", "333", "", now + 60)
        sync(a, sa)
        sync(o, so)
        adm, _ = self.login("@boss_one", "admin")
        import datetime as dt
        start = dt.datetime.combine(dt.date.today(), dt.time.min).timestamp()
        res = adm.call("GET", "/api/admin/day", params={"from": start, "to": start + 86400})
        ids = {i["id"] for i in res["items"]}
        self.assertTrue({upcoming["id"], missed["id"], theirs["id"]} <= ids)
        self.assertIn("vn3", res["rooms"] + [u["room"] for u in res["users"]])
        names = {u["name"]: u for u in res["users"]}
        self.assertTrue(names["Анна Смирнова"]["online"])
        # оператор не может в админ-API
        with self.assertRaises(pc.ApiError) as c:
            a.call("GET", "/api/admin/day", params={"from": start, "to": start + 86400})
        self.assertEqual(c.exception.status, 403)
        # действия админа доходят до оператора
        adm.call("POST", "/api/admin/item", {"id": missed["id"], "action": "done"})
        sync(a, sa)
        self.assertEqual(sa.items[missed["id"]]["status"], "done")
        with self.assertRaises(pc.ApiError) as c:
            adm.call("POST", "/api/admin/item", {"id": missed["id"], "action": "done"})
        self.assertEqual(c.exception.status, 409)
        adm.call("POST", "/api/admin/item", {"id": missed["id"], "action": "restore"})
        sync(a, sa)
        self.assertEqual(sa.items[missed["id"]]["status"], "active")
        with self.assertRaises(pc.ApiError):
            adm.call("POST", "/api/admin/item", {"id": missed["id"], "action": "explode"})
        with self.assertRaises(pc.ApiError):
            adm.call("GET", "/api/admin/day", params={"from": 10, "to": 10 + 86400 * 100})

    def test_carry_over_missed(self):
        a, _ = self.login("@anna_smirnova")
        st = self.store("a")
        old = st.add("Вчерашний", "111", "", time.time() - 86400 * 2)
        sync(a, st)
        adm, _ = self.login("@super_one", "admin")
        import datetime as dt
        start = dt.datetime.combine(dt.date.today(), dt.time.min).timestamp()
        ids = lambda carry: {i["id"] for i in adm.call("GET", "/api/admin/day", params={
            "from": start, "to": start + 86400, "carry": carry})["items"]}
        self.assertIn(old["id"], ids("1"))
        self.assertNotIn(old["id"], ids("0"))


class Roles(Base):
    def test_last_superadmin_protected(self):
        with self.assertRaises(ValueError):
            self.app.update_user(self.sup["id"], role="admin")
        with self.assertRaises(ValueError):
            self.app.update_user(self.sup["id"], blocked=1)
        with self.assertRaises(ValueError):
            self.app.delete_user(self.sup["id"])
        s2, _ = self.app.create_user("Второй", "vn2", "superadmin", "super_two")
        self.app.update_user(self.sup["id"], role="admin")       # теперь можно — есть второй
        self.assertEqual(self.app.user(self.sup["id"])["role"], "admin")

    def test_owner_protected_and_bootstrapped(self):
        self.app.owners = {8267218455}
        o = self.app.ensure_owner(8267218455, {"first_name": "Хозяин", "username": "the_owner"})
        self.assertEqual(o["role"], "superadmin")
        self.assertEqual(o["tg_username"], "the_owner")
        s2, _ = self.app.create_user("Другой", "vn2", "superadmin", "super_two")
        with self.assertRaises(ValueError):
            self.app.update_user(o["id"], role="operator")
        with self.assertRaises(ValueError):
            self.app.update_user(o["id"], blocked=1)
        with self.assertRaises(ValueError):
            self.app.delete_user(o["id"])
        # если кто-то всё же понизил в базе — владелец восстанавливается при следующем сообщении
        self.app.db.x("UPDATE users SET role='operator' WHERE id=?", (o["id"],))
        self.assertEqual(self.app.ensure_owner(8267218455, {})["role"], "superadmin")
        self.assertIsNone(self.app.ensure_owner(111, {}))
        # и может войти в админку по нику
        self.login("@the_owner", "admin")

    def test_username_unique_and_rename(self):
        with self.assertRaises(ValueError):
            self.app.create_user("Двойник", "vn2", "operator", "@ANNA_SMIRNOVA")
        self.app.update_user(self.anna["id"], tg_username="@anna_new")
        self.login("@anna_new")
        with self.assertRaises(pc.ApiError):
            self.login("@anna_smirnova")

    def test_room_rename_moves_people_and_items(self):
        a, _ = self.login("@anna_smirnova")
        st = self.store("a")
        x = st.add("К", "111", "", time.time() + 60)
        sync(a, st)
        rid = [r["id"] for r in self.app.rooms() if r["name"] == "vn2"][0]
        self.app.rename_room(rid, "VN-2 зал")
        self.assertEqual(self.app.user(self.anna["id"])["room"], "VN-2 зал")
        sync(a, st)
        self.assertEqual(st.items[x["id"]]["room"], "VN-2 зал")
        with self.assertRaises(ValueError):
            self.app.delete_room(rid)                         # в комнате есть люди
        r3 = [r["id"] for r in self.app.rooms() if r["name"] == "vn3"][0]
        with self.assertRaises(ValueError):
            self.app.rename_room(r3, "vn-2 ЗАЛ")             # без учёта регистра — уже есть


class Notify(Base):
    def test_notify_once(self):
        from helpers import FakeTelegram
        import perezvon_server as srv
        tg = FakeTelegram()
        try:
            self.app.bot = srv.Bot(self.app, "T", api_base=tg.url)
            self.app.link_by_username("anna_smirnova", 501, "Анна")
            self.app.link_by_username("boss_one", 502, "Босс")
            self.app.link_by_username("oleg_kim", 503, "Олег")
            self.app.update_user(self.oleg["id"], tg_notify=0)
            a, _ = self.login("@anna_smirnova")
            st = self.store("a")
            due = st.add("Сейчас", "89991112233", "заметка <b>", time.time() - 5)
            miss = st.add("Давно", "222", "", time.time() - 3600)
            fut = st.add("Потом", "333", "", time.time() + 3600)
            sync(a, st)
            srv.notify_once(self.app)
            anna = tg.sent(501)
            texts = "\n".join(m["text"] for m in anna)
            self.assertIn("Пора перезвонить", texts)
            self.assertIn("+79991112233", texts)
            self.assertIn("заметка &lt;b&gt;", texts)          # HTML экранирован
            self.assertNotIn("Потом", texts)
            boss = "\n".join(m["text"] for m in tg.sent(502))
            self.assertIn("Пропущен перезвон", boss)
            self.assertIn("Давно", boss)
            self.assertNotIn("Сейчас", boss)                  # ещё в пределах 10 минут
            self.assertEqual(tg.sent(503), [])
            n = len(tg.calls)
            srv.notify_once(self.app)                         # повторно — тишина
            self.assertEqual(len(tg.calls), n)
            # отложили → снова напомнит в срок
            self.app.item_action(due["id"], "snooze15", "x")
            self.app.db.x("UPDATE items SET due=? WHERE id=?", (time.time() - 1, due["id"]))
            srv.notify_once(self.app)
            self.assertGreater(len(tg.calls), n)
            self.assertIsNotNone(fut)
            self.assertIsNotNone(miss)
        finally:
            tg.close()
            self.app.bot = None


if __name__ == "__main__":
    unittest.main()
