# -*- coding: utf-8 -*-
"""Сквозная проверка автообновления на НАСТОЯЩИХ exe (в отдельной папке, рабочую установку не трогает).

  python build.py && python build.py --version 1.0.99 --dist dist-test --only client
  python tests/e2e_update.py
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

import helpers  # noqa: F401
from helpers import TestServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import release  # noqa: E402
import pz_update  # noqa: E402

INST = "Perezvon.E2E"


def sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


def main():
    old_exe = os.path.join(ROOT, "dist", "Перезвон.exe")
    new_exe = os.path.join(ROOT, "dist-test", "Перезвон.exe")
    S = TestServer()
    E = tempfile.mkdtemp(prefix="pz-e2e-")
    ok = False
    try:
        release.sign_dir(os.path.join(ROOT, "dist-test"), os.path.join(S.dir, "updates"), "1.0.99")
        os.makedirs(os.path.join(E, "app"))
        os.makedirs(os.path.join(E, "home"))
        target = os.path.join(E, "app", "Перезвон.exe")
        shutil.copy2(old_exe, target)
        json.dump({"server": S.url, "local_mode": True}, open(os.path.join(E, "home", "settings.json"), "w"))
        env = dict(os.environ, PEREZVON_HOME=os.path.join(E, "home"), PEREZVON_INSTANCE=INST,
                   PEREZVON_NOINSTALL="1", PEREZVON_UPDATE_FIRST="3")
        subprocess.Popen([target, "--autostart"], env=env)
        t0 = time.time()
        while time.time() - t0 < 120:
            time.sleep(1)
            if sha(target) == sha(new_exe) and pz_update.mutex_exists(INST):
                ok = True
                break
        print("обновилось за %.0f с" % (time.time() - t0) if ok else "НЕ обновилось за 120 с")
        log = os.path.join(E, "home", "install.log")
        print(open(log, encoding="utf-8").read() if os.path.exists(log) else "(нет install.log)")
        print("старая копия сохранена:", os.path.exists(target + ".old") and sha(target + ".old") == sha(old_exe))
    finally:
        import ctypes
        for _ in range(3):
            pz_update.mutex_exists(INST) and _quit()
            time.sleep(2)
        S.close()
        shutil.rmtree(E, ignore_errors=True)
    return 0 if ok else 1


def _quit():
    import ctypes
    k32 = ctypes.windll.kernel32
    h = k32.OpenEventW(0x0002, False, "Local\\%s.quit" % INST)
    if h:
        k32.SetEvent(h)
        k32.CloseHandle(h)


if __name__ == "__main__":
    sys.exit(main())
