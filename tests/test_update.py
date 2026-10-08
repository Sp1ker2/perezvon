# -*- coding: utf-8 -*-
"""Автообновление: подпись, скачивание с сервера, отказ от подделок, замена exe с откатом."""
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

from helpers import TestServer
import pz_update as u

KEY = u.generate_key(1024)          # тестовый ключ (боевой — только на ПК сборки)
N, D = int(KEY["n"], 16), int(KEY["d"], 16)


class Signature(unittest.TestCase):
    def test_sign_verify_and_tamper(self):
        msg = u.manifest_message("client", "1.2.3", "ab" * 32, 1000)
        sig = u.rsa_sign(msg, N, D)
        self.assertTrue(u.rsa_verify(msg, sig, N))
        for bad in (u.manifest_message("client", "1.2.4", "ab" * 32, 1000),     # другая версия
                    u.manifest_message("admin", "1.2.3", "ab" * 32, 1000),      # другая программа
                    u.manifest_message("client", "1.2.3", "cd" * 32, 1000),     # другой файл
                    u.manifest_message("client", "1.2.3", "ab" * 32, 1001)):    # другой размер
            self.assertFalse(u.rsa_verify(bad, sig, N))
        flipped = ("0" if sig[-1] != "0" else "1")
        self.assertFalse(u.rsa_verify(msg, sig[:-1] + flipped, N))
        for junk in ("", "zz", "00", sig + "00", "f" * len(sig)):
            self.assertFalse(u.rsa_verify(msg, junk, N))

    def test_matches_reference_library(self):
        try:
            from cryptography.hazmat.primitives.asymmetric import rsa, padding
            from cryptography.hazmat.primitives import hashes
        except ImportError:
            self.skipTest("cryptography нет")
        msg = b"perezvon-update|client|9.9.9|x|1"
        sig = u.rsa_sign(msg, N, D)
        rsa.RSAPublicNumbers(65537, N).public_key().verify(bytes.fromhex(sig), msg, padding.PKCS1v15(),
                                                           hashes.SHA256())
        priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        ref = priv.sign(msg, padding.PKCS1v15(), hashes.SHA256()).hex()
        self.assertTrue(u.rsa_verify(msg, ref, priv.public_key().public_numbers().n))

    def test_production_key_embedded(self):
        self.assertEqual(u._pub_n().bit_length(), 3072)

    def test_versions(self):
        self.assertGreater(u.vtuple("1.0.10"), u.vtuple("1.0.9"))
        self.assertGreater(u.vtuple("1.1"), u.vtuple("1.0.99"))
        self.assertEqual(u.vtuple("мусор"), (0,))


class Download(unittest.TestCase):
    def setUp(self):
        self.S = TestServer()
        self.work = tempfile.mkdtemp()
        self.upd = os.path.join(self.S.dir, "updates")
        os.makedirs(self.upd)
        self.payload = os.urandom(300_000)
        self.p = mock.patch.object(u, "PUB_N", N)
        self.p.start()

    def tearDown(self):
        self.p.stop()
        self.S.close()
        shutil.rmtree(self.work, ignore_errors=True)

    def publish(self, version, payload=None, sig_ok=True, kind="client", sha=None, size=None, fname=None,
                write=True):
        payload = self.payload if payload is None else payload
        fname = fname or "%s-%s.exe" % (kind, version)
        if write:
            with open(os.path.join(self.upd, os.path.basename(fname)), "wb") as f:
                f.write(payload)
        import hashlib
        h = sha or hashlib.sha256(payload).hexdigest()
        n = len(payload) if size is None else size
        sig = u.rsa_sign(u.manifest_message(kind, version, h, n), N, D)
        if not sig_ok:
            sig = u.rsa_sign(u.manifest_message(kind, "6.6.6", h, n), N, D)
        with open(os.path.join(self.upd, "manifest.json"), "w") as f:
            json.dump({kind: {"version": version, "file": fname, "sha256": h, "size": n, "sig": sig}}, f)

    def updater(self, current="1.0.1", kind="client"):
        return u.Updater(kind, current, lambda: self.S.url, self.work)

    def test_newer_version_downloaded_and_verified(self):
        self.publish("1.0.2")
        up = self.updater()
        up._check()
        self.assertTrue(up.ready)
        self.assertEqual(up.ready["version"], "1.0.2")
        self.assertEqual(open(up.ready["path"], "rb").read(), self.payload)
        import urllib.request
        real = urllib.request.urlopen
        urls = []

        def spy(req, *a, **kw):
            urls.append(req.full_url if hasattr(req, "full_url") else str(req))
            return real(req, *a, **kw)
        up2 = self.updater()                     # повторная проверка — файл уже скачан, заново не качает
        with mock.patch("urllib.request.urlopen", spy):
            up2._check()
        self.assertTrue(up2.ready)
        self.assertFalse([x for x in urls if "/update/file" in x], urls)

    def test_same_or_older_ignored(self):
        self.publish("1.0.1")
        up = self.updater("1.0.1")
        up._check()
        self.assertIsNone(up.ready)
        self.assertIn("последняя", up.status)
        self.publish("1.0.0")
        up._check()
        self.assertIsNone(up.ready)

    def test_bad_signature_rejected(self):
        self.publish("1.0.2", sig_ok=False)
        up = self.updater()
        up._check()
        self.assertIsNone(up.ready)
        self.assertIn("неверная подпись", up.status)
        self.assertFalse(os.path.exists(os.path.join(self.work, "update", "client-1.0.2.exe")))

    def test_file_swapped_on_server_rejected(self):
        """Сервер взломан: манифест подписан честно, а файл подменён — ставить нельзя."""
        self.publish("1.0.2")
        with open(os.path.join(self.upd, "client-1.0.2.exe"), "wb") as f:
            f.write(os.urandom(len(self.payload)))
        up = self.updater()
        with self.assertRaises(ValueError):
            up._check()
        self.assertIsNone(up.ready)
        self.assertEqual([x for x in os.listdir(os.path.join(self.work, "update")) if x.endswith(".exe")], [])

    def test_oversize_and_garbage_manifest(self):
        self.publish("1.0.2", size=u.MAX_SIZE + 1)
        with self.assertRaises(ValueError):
            self.updater()._check()
        with open(os.path.join(self.upd, "manifest.json"), "w") as f:
            f.write("{мусор")
        up = self.updater()
        up._check()
        self.assertIsNone(up.ready)

    def test_server_endpoints(self):
        import pz_common as pc
        api = pc.Api(self.S.url)
        self.assertEqual(api.call("GET", "/api/update", params={"app": "client"}), {"version": None})
        # попытка выйти из папки обновлений: «../perezvon.db» → ищется только updates/perezvon.db (его нет)
        self.publish("1.0.2", fname="../perezvon.db", write=False)
        self.assertTrue(os.path.exists(os.path.join(self.S.dir, "perezvon.db")))
        self.assertEqual(api.call("GET", "/api/update", params={"app": "client"})["version"], None)
        with self.assertRaises(pc.ApiError):
            api.call("GET", "/api/update/file", params={"app": "client"})
        self.publish("1.0.2")
        info = api.call("GET", "/api/update", params={"app": "client"})
        self.assertEqual(set(info), {"version", "sha256", "size", "sig"})
        self.assertEqual(api.call("GET", "/api/update", params={"app": "evil"}), {"version": None})
        with self.assertRaises(pc.ApiError) as c:
            api.call("GET", "/api/update/file", params={"app": "admin"})
        self.assertEqual(c.exception.status, 404)

    def test_disabled_outside_exe(self):
        up = self.updater()
        self.assertFalse(up.enabled())                         # из исходников не обновляемся
        with mock.patch.object(sys, "frozen", True, create=True):
            self.assertTrue(up.enabled())


class Replace(unittest.TestCase):
    """Новая версия встаёт на место старой; не поднялась — откат на старую."""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.target = os.path.join(self.d, "Перезвон.exe")
        self.new = os.path.join(self.d, "update", "client-9.exe")
        os.makedirs(os.path.dirname(self.new))
        open(self.target, "wb").write(b"OLD")
        open(self.new, "wb").write(b"NEW")
        self.launched = []
        self.logs = []
        self.patches = [mock.patch.object(sys, "executable", self.new),
                        mock.patch.object(u, "launch_clean", lambda p, a=(): self.launched.append((p, list(a)))),
                        mock.patch.object(u, "STARTUP_WAIT", 1)]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        shutil.rmtree(self.d, ignore_errors=True)

    def argv(self):
        return ["x.exe", "--replace", self.target, "--pid", "0", "--then", "--updated", "--autostart"]

    def test_success(self):
        rc = u.run_replace(self.argv(), lambda: bool(self.launched), self.logs.append)
        self.assertEqual(rc, 0)
        self.assertEqual(open(self.target, "rb").read(), b"NEW")
        self.assertEqual(open(self.target + ".old", "rb").read(), b"OLD")
        self.assertEqual(self.launched, [(self.target, ["--updated", "--autostart"])])

    def test_rollback_when_new_does_not_start(self):
        rc = u.run_replace(self.argv(), lambda: False, self.logs.append)
        self.assertEqual(rc, 2)
        self.assertEqual(open(self.target, "rb").read(), b"OLD")         # вернули старую
        self.assertEqual(len(self.launched), 2)                          # и запустили её
        self.assertTrue(any("возвращаю старую" in m for m in self.logs))

    def test_waits_for_old_to_exit(self):
        alive = {"n": 3}

        def instance_alive():
            if self.launched:
                return True
            alive["n"] -= 1
            return alive["n"] > 0
        rc = u.run_replace(self.argv(), instance_alive, self.logs.append)
        self.assertEqual(rc, 0)
        self.assertEqual(alive["n"], 0)                                  # дождался выхода старой


if __name__ == "__main__":
    unittest.main()
