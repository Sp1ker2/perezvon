# -*- coding: utf-8 -*-
"""Автообновление «Перезвона» и «Перезвон Админ».

Схема:
  1. Программа спрашивает сервер: GET /api/update?app=client|admin → {version, sha256, size, sig}.
  2. Если версия новее — качает файл (GET /api/update/file?app=…) в папку update рядом с данными.
  3. Проверяет размер, SHA-256 и ПОДПИСЬ RSA-3072 (PKCS#1 v1.5, SHA-256) открытым ключом, зашитым ниже.
     Закрытый ключ есть только на ПК, где собирают релизы, — сервер подписать файл не может,
     так что даже взломанный сервер не подсунет программам чужой exe.
  4. Программа запускает скачанный exe с «--replace <путь> --pid <pid>» и закрывается.
     Новый exe ждёт выхода старого, делает копию старого (.old), встаёт на его место и запускает его.
     Если новая версия не поднялась за 25 с — возвращает старую и запускает её.

Только стандартная библиотека (RSA-проверка — несколько строк на pow()).
"""
import hashlib
import os
import shutil
import ssl
import subprocess
import sys
import threading
import time
import urllib.request

import pz_common as pc

# открытый ключ подписи релизов (RSA-3072, e=65537); закрытый — %APPDATA%\PerezvonRelease\update-key.json
PUB_N = None   # подставляется ниже из PUB_N_HEX
PUB_E = 65537
PUB_N_HEX = (
    "a10afafeef05efff1b4235b27ab5131538ba989da6fa4f9e7f8366b8e198d840f775cf31b14237fee83f083d916f6bb3"
    "99458a954f6eeb8e7184c97b7adb4b898a36e58a620c2811e993a7581791b469f17223220fa0e3f21c956d67a3801e97"
    "18d748753c6f520a49b362c2526f616ea659ed46d6d9caf76a7e4cf843de46eb0a798626dafe85e487bc08d9e943e50f"
    "c505f49a50dcabfd9c9ac91a9a9ba7c2d4f10a150b231c8ac76f827f7e8a1590dfc5beaaf3bbaf578d7d2826fbd62c05"
    "e3b6a9759cab7f2230bfa33a161d615ec360c52b3f551e9b0ec91bfda5cc7b7abbaa362323fe0dbf2c0458c808b8f559"
    "54f592331c64a0657cac49b88324b2d9adeb9557d8d70a5de58c29a8699c8c1f82c37165f341aaaee49011734a7ae403"
    "b64464a9020ca5a85493bada08a70fbacf301c5801c380e50e228cbfc91837a1f98603047dca5f95cac2a5701ce17889"
    "710ff9b50723f0a269fa0c29f9a0aed48d461ccf8fe01e7281c0b5f07463e7d7f6b79fc49f6f8cbd800c5e19409a182f")

CHECK_FIRST = int(os.environ.get("PEREZVON_UPDATE_FIRST") or 60)   # первая проверка через минуту
CHECK_EVERY = 6 * 3600    # потом раз в 6 часов
MAX_SIZE = 150 * 1024 * 1024
STARTUP_WAIT = 25         # сколько ждать, что новая версия поднялась, прежде чем откатить

_SHA256_PREFIX = bytes.fromhex("3031300d060960864801650304020105000420")


def _pub_n():
    global PUB_N
    if PUB_N is None:
        PUB_N = int(PUB_N_HEX, 16) if not PUB_N_HEX.startswith("__") else 0
    return PUB_N


def manifest_message(app_kind, version, sha256, size):
    """Что именно подписано: программа, версия, хеш и размер файла — подменить ни одно нельзя."""
    return ("perezvon-update|%s|%s|%s|%d" % (app_kind, version, sha256.lower(), int(size))).encode("utf-8")


def _emsa(msg, k):
    t = _SHA256_PREFIX + hashlib.sha256(msg).digest()
    if k < len(t) + 11:
        raise ValueError("ключ слишком короткий")
    return b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t


def rsa_sign(msg, n, d):
    k = (n.bit_length() + 7) // 8
    s = pow(int.from_bytes(_emsa(msg, k), "big"), d, n)
    return s.to_bytes(k, "big").hex()


def rsa_verify(msg, sig_hex, n=None, e=PUB_E):
    n = _pub_n() if n is None else n
    if not n:
        return False
    k = (n.bit_length() + 7) // 8
    try:
        s = int(sig_hex, 16)
    except (TypeError, ValueError):
        return False
    if not 0 < s < n or len(sig_hex) != 2 * k:
        return False
    em = pow(s, e, n).to_bytes(k, "big")
    expected = _emsa(msg, k)
    diff = 0
    for a, b in zip(em, expected):
        diff |= a ^ b
    return diff == 0 and len(em) == len(expected)


def vtuple(v):
    try:
        return tuple(int(x) for x in str(v).split("."))
    except ValueError:
        return (0,)


# ───────────────────────────── генерация ключа (только на ПК сборки) ─────────────────────────────

def _is_probable_prime(n, rounds=40):
    import secrets
    if n < 2:
        return False
    for p in (2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37):
        if n % p == 0:
            return n == p
    d, r = n - 1, 0
    while d % 2 == 0:
        d //= 2
        r += 1
    for _ in range(rounds):
        a = secrets.randbelow(n - 3) + 2
        x = pow(a, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(r - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def _prime(bits):
    import secrets
    while True:
        c = secrets.randbits(bits) | (1 << (bits - 1)) | (1 << (bits - 2)) | 1
        if c % PUB_E != 1 and _is_probable_prime(c):
            return c


def generate_key(bits=3072):
    while True:
        p, q = _prime(bits // 2), _prime(bits // 2)
        n = p * q
        if p != q and n.bit_length() == bits:
            break
    d = pow(PUB_E, -1, (p - 1) * (q - 1))
    return {"n": hex(n)[2:], "e": PUB_E, "d": hex(d)[2:]}


# ───────────────────────────── проверка и скачивание ─────────────────────────────

class Updater:
    """Фоновая проверка обновлений. Всё сетевое — в потоке; главный поток спрашивает .ready и зовёт .apply()."""

    def __init__(self, app_kind, current_version, server_getter, work_dir, log=None):
        self.kind = app_kind
        self.current = current_version
        self.server = server_getter
        self.dir = os.path.join(work_dir, "update")
        self.log = log or (lambda m: None)
        self.ready = None              # {"path", "version"} — скачано и проверено
        self.status = "ещё не проверяли"
        self.busy = False
        self.next_at = time.time() + CHECK_FIRST
        self.ctx = ssl.create_default_context()

    def enabled(self):
        """Обновляем только собранный exe (не исходники) и только с ключом в программе."""
        return getattr(sys, "frozen", False) and bool(_pub_n())

    def tick(self):
        if self.busy or self.ready or time.time() < self.next_at or not self.enabled():
            return
        self.check_now()

    def check_now(self):
        if self.busy:
            return
        self.busy = True
        self.next_at = time.time() + CHECK_EVERY
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        try:
            self._check()
        except Exception as e:                   # сеть/сервер — тихо попробуем в следующий раз
            self.status = "не удалось проверить: %s" % (getattr(e, "message", None) or e)
            self.next_at = time.time() + 1800
        finally:
            self.busy = False

    def _check(self):
        info = pc.Api(self.server(), timeout=15).call("GET", "/api/update", params={"app": self.kind})
        ver = str(info.get("version") or "")
        if not ver or vtuple(ver) <= vtuple(self.current):
            self.status = "установлена последняя версия (%s)" % self.current
            return
        size, sha = int(info.get("size") or 0), str(info.get("sha256") or "").lower()
        if not 0 < size <= MAX_SIZE or len(sha) != 64:
            raise ValueError("сервер прислал странное описание обновления")
        if not rsa_verify(manifest_message(self.kind, ver, sha, size), info.get("sig") or ""):
            self.status = "обновление %s отклонено: неверная подпись" % ver
            self.log(self.status)
            return
        os.makedirs(self.dir, exist_ok=True)
        path = os.path.join(self.dir, "%s-%s.exe" % (self.kind, ver))
        if not (os.path.exists(path) and _file_sha(path) == sha):
            self.status = "скачиваю %s…" % ver
            tmp = path + ".part"
            url = self.server().rstrip("/") + "/api/update/file?app=%s&v=%s" % (self.kind, ver)
            req = urllib.request.Request(url, headers={"User-Agent": "Perezvon/" + self.current})
            h = hashlib.sha256()
            got = 0
            with urllib.request.urlopen(req, timeout=60, context=self.ctx if url.startswith("https") else None) as r, \
                    open(tmp, "wb") as f:
                while True:
                    chunk = r.read(1 << 16)
                    if not chunk:
                        break
                    got += len(chunk)
                    if got > size:
                        raise ValueError("файл больше заявленного")
                    h.update(chunk)
                    f.write(chunk)
            if got != size or h.hexdigest() != sha:
                os.remove(tmp)
                raise ValueError("файл обновления повреждён при скачивании")
            os.replace(tmp, path)
        for name in os.listdir(self.dir):        # старые скачанные версии не копим
            if name != os.path.basename(path):
                try:
                    os.remove(os.path.join(self.dir, name))
                except OSError:
                    pass
        self.ready = {"path": path, "version": ver}
        self.status = "скачана версия %s — установится сама" % ver
        self.log("готово обновление %s → %s" % (self.current, ver))

    def apply(self, target, relaunch_args=()):
        """Запустить новый exe в режиме замены. После True вызывающий должен сразу закрыться."""
        if not self.ready:
            return False
        args = ["--replace", target, "--pid", str(os.getpid())]
        if relaunch_args:
            args += ["--then"] + list(relaunch_args)
        self.log("ставлю %s поверх %s" % (self.ready["version"], target))
        launch_clean(self.ready["path"], args)
        return True


def _file_sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def launch_clean(path, args=()):
    """Запуск другой PyInstaller-программы без наших служебных переменных окружения:
    иначе новая копия может взять временную папку этой (которую мы удалим при выходе) и упасть."""
    env = {k: v for k, v in os.environ.items() if not (k.startswith("_PYI") or k.startswith("_MEI"))}
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    subprocess.Popen([path] + list(args), close_fds=True, env=env, cwd=os.path.dirname(path) or None,
                     creationflags=0x00000008 | 0x00000200 if sys.platform == "win32" else 0)


# ───────────────────────────── режим замены (запускается в НОВОМ exe) ─────────────────────────────

def mutex_exists(name):
    if sys.platform != "win32":
        return False
    import ctypes
    k32 = ctypes.windll.kernel32
    h = k32.OpenMutexW(0x00100000, False, "Local\\%s.mutex" % name)
    if h:
        k32.CloseHandle(h)
        return True
    return False


def _pid_alive(pid):
    if sys.platform != "win32":
        return False
    import ctypes
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x00100000, False, int(pid))         # SYNCHRONIZE
    if not h:
        return False
    try:
        return k32.WaitForSingleObject(h, 0) == 0x102        # WAIT_TIMEOUT → ещё жив
    finally:
        k32.CloseHandle(h)


def run_replace(argv, instance_alive, log):
    """«--replace <target> --pid <pid> [--then args…]»: встать на место target и запустить его.
    instance_alive() — есть ли запущенная копия программы (по мьютексу). Возвращает код выхода."""
    i = argv.index("--replace")
    target = argv[i + 1]
    pid = int(argv[argv.index("--pid") + 1]) if "--pid" in argv else 0
    then = argv[argv.index("--then") + 1:] if "--then" in argv else []
    me = os.path.abspath(sys.executable)
    log("замена: %s → %s" % (me, target))
    for _ in range(120):                                      # ждём, пока старая версия закроется
        if not (pid and _pid_alive(pid)) and not instance_alive():
            break
        time.sleep(0.25)
    time.sleep(0.5)
    backup = target + ".old"
    try:
        if os.path.exists(target):
            shutil.copy2(target, backup)
    except OSError as e:
        log("не сделал резервную копию: %s" % e)
    for attempt in range(40):
        try:
            shutil.copy2(me, target + ".new")
            os.replace(target + ".new", target)
            break
        except OSError:
            time.sleep(0.5)
    else:
        log("не смог заменить файл — запускаю старую версию")
        launch_clean(target, then)
        return 1
    launch_clean(target, then)
    for _ in range(int(STARTUP_WAIT * 4)):                    # поднялась ли новая версия?
        time.sleep(0.25)
        if instance_alive():
            log("обновлено, новая версия запущена")
            return 0
    log("новая версия не запустилась за %d с — возвращаю старую" % STARTUP_WAIT)
    try:
        if os.path.exists(backup):
            shutil.copy2(backup, target)
    except OSError as e:
        log("откат не удался: %s" % e)
    launch_clean(target, then)
    return 2

