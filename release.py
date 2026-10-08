# -*- coding: utf-8 -*-
"""Выпуск обновления: подписать собранные exe и выложить на сервер — программы обновятся сами.

  python build.py                 — сначала собрать (версия берётся из src/pz_common.py → APP_VERSION)
  python release.py               — подписать dist/*.exe → dist/updates/ (manifest.json + exe), ничего не отправляя
  python release.py --upload      — то же + залить на сервер в /var/lib/perezvon/updates

Закрытый ключ подписи: %APPDATA%\\PerezvonRelease\\update-key.json (есть только на этом ПК, доступ — только этой
учётке Windows). Потеряете ключ — программы перестанут принимать обновления, придётся раз переустановить вручную.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "src"))
import pz_update  # noqa: E402

SERVER = "root@89.124.122.158"
SSH_KEY = os.path.join(os.path.expanduser("~"), ".ssh", "id_ed25519")
KEY = os.path.join(os.environ.get("APPDATA", ""), "PerezvonRelease", "update-key.json")
APPS = {"client": "Перезвон.exe", "admin": "Перезвон Админ.exe"}


def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sign_dir(dist, out, version, key_path=KEY):
    key = json.load(open(key_path, encoding="utf-8"))
    n, d = int(key["n"], 16), int(key["d"], 16)
    if n != pz_update._pub_n():
        raise SystemExit("Ключ не совпадает с открытым ключом в src/pz_update.py — так подписывать нельзя")
    os.makedirs(out, exist_ok=True)
    manifest = {}
    for kind, name in APPS.items():
        src = os.path.join(dist, name)
        if not os.path.exists(src):
            continue
        fname = "%s-%s.exe" % (kind, version)
        dst = os.path.join(out, fname)
        shutil.copy2(src, dst)
        h, size = sha(dst), os.path.getsize(dst)
        sig = pz_update.rsa_sign(pz_update.manifest_message(kind, version, h, size), n, d)
        assert pz_update.rsa_verify(pz_update.manifest_message(kind, version, h, size), sig)
        manifest[kind] = {"version": version, "file": fname, "sha256": h, "size": size, "sig": sig}
        print("  %-6s %s  %.1f МБ  sha256 %s…" % (kind, version, size / 1048576, h[:16]))
    with open(os.path.join(out, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    return manifest


def upload(out, manifest):
    files = [os.path.join(out, e["file"]) for e in manifest.values()] + [os.path.join(out, "manifest.json")]
    ssh = ["-o", "BatchMode=yes", "-i", SSH_KEY]
    subprocess.run(["ssh"] + ssh + [SERVER, "mkdir -p /root/perezvon-release"], check=True)
    subprocess.run(["scp", "-q"] + ssh + files + [SERVER + ":/root/perezvon-release/"], check=True)
    keep = " ".join(e["file"] for e in manifest.values())
    remote = ("set -e; D=/var/lib/perezvon/updates; install -d -m 750 -o perezvon -g perezvon $D; "
              "cd /root/perezvon-release; for f in %s; do install -m 640 -o perezvon -g perezvon $f $D/$f; done; "
              "install -m 640 -o perezvon -g perezvon manifest.json $D/manifest.json.new; "
              "mv -f $D/manifest.json.new $D/manifest.json; "              # манифест — последним и атомарно
              "cd $D; for f in *.exe; do case \" %s \" in *\" $f \"*) ;; *) rm -f \"$f\";; esac; done; "
              "rm -rf /root/perezvon-release; ls -la $D" % (keep, keep))
    subprocess.run(["ssh"] + ssh + [SERVER, remote], check=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--upload", action="store_true")
    ap.add_argument("--dist", default=os.path.join(ROOT, "dist"))
    ap.add_argument("--out", default=None)
    ap.add_argument("--version", default=None)
    a = ap.parse_args()
    ver = a.version or re.search(r'APP_VERSION = "([\d.]+)"',
                                 open(os.path.join(ROOT, "src", "pz_common.py"), encoding="utf-8").read()).group(1)
    out = a.out or os.path.join(a.dist, "updates")
    print("Подписываю версию", ver)
    m = sign_dir(a.dist, out, ver)
    if not m:
        raise SystemExit("В %s нет собранных exe" % a.dist)
    if a.upload:
        upload(out, m)
        print("Выложено. Программы подхватят обновление в течение 6 часов (или «Проверить» в настройках).")


if __name__ == "__main__":
    main()
