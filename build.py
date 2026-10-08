# -*- coding: utf-8 -*-
"""Сборка: иконка + «Перезвон.exe» и «Перезвон Админ.exe» (PyInstaller, один файл, без консоли).

  python build.py           — собрать оба
Нужно только на машине сборки: Python 3.10+, pyinstaller, pillow. Пользователям ничего ставить не надо.
"""
import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(ROOT, "src")
DIST = os.path.join(ROOT, "dist")
WORK = os.path.join(ROOT, "build")


def version():
    s = open(os.path.join(SRC, "pz_common.py"), encoding="utf-8").read()
    return re.search(r'APP_VERSION = "([\d.]+)"', s).group(1)


def make_icon(path, glyph, bg):
    from PIL import Image, ImageDraw, ImageFont
    big = 512
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((16, 16, big - 16, big - 16), radius=110, fill=bg)
    f = ImageFont.truetype(os.path.join(os.environ["WINDIR"], "Fonts", "segmdl2.ttf"), 290)
    box = d.textbbox((0, 0), glyph, font=f)
    w, h = box[2] - box[0], box[3] - box[1]
    d.text(((big - w) / 2 - box[0], (big - h) / 2 - box[1]), glyph, font=f, fill="white")
    img.save(path, sizes=[(16, 16), (20, 20), (24, 24), (32, 32), (40, 40), (48, 48), (64, 64), (128, 128), (256, 256)])


def version_file(path, name, desc, ver):
    nums = (ver.split(".") + ["0"] * 4)[:4]
    t = tuple(int(x) for x in nums)
    open(path, "w", encoding="utf-8").write('''VSVersionInfo(
  ffi=FixedFileInfo(filevers=%r, prodvers=%r, mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0,
                    date=(0, 0)),
  kids=[StringFileInfo([StringTable('041904B0', [
      StringStruct('CompanyName', 'Перезвон'), StringStruct('FileDescription', %r),
      StringStruct('FileVersion', %r), StringStruct('InternalName', %r),
      StringStruct('OriginalFilename', %r), StringStruct('ProductName', 'Перезвон'),
      StringStruct('ProductVersion', %r)])]),
    VarFileInfo([VarStruct('Translation', [0x419, 1200])])])
''' % (t, t, desc, ver, name, name + ".exe", ver))


def build(script, name, desc, icon, ver):
    vf = os.path.join(WORK, name + "-version.txt")
    version_file(vf, name, desc, ver)
    cmd = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile", "--windowed",
           "--name", name, "--icon", icon, "--version-file", vf,
           "--add-data", "%s%s." % (icon, os.pathsep),
           "--distpath", DIST, "--workpath", os.path.join(WORK, name), "--specpath", WORK,
           "--paths", SRC,
           "--exclude-module", "PIL", "--exclude-module", "numpy", "--exclude-module", "unittest",
           os.path.join(SRC, script)]
    print(">>", name)
    subprocess.run(cmd, check=True)


def main():
    ver = version()
    os.makedirs(WORK, exist_ok=True)
    os.makedirs(DIST, exist_ok=True)
    ico_c = os.path.join(WORK, "perezvon.ico")
    ico_a = os.path.join(WORK, "perezvon-admin", "perezvon.ico")
    os.makedirs(os.path.dirname(ico_a), exist_ok=True)
    make_icon(ico_c, "", (61, 139, 253, 255))          # трубка на синем
    make_icon(ico_a, "", (34, 181, 115, 255))           # люди на зелёном
    build("perezvon.py", "Перезвон", "Перезвон — напоминания перезвонить", ico_c, ver)
    build("perezvon_admin.py", "Перезвон Админ", "Перезвон — админка", ico_a, ver)
    for n in ("Перезвон.exe", "Перезвон Админ.exe"):
        p = os.path.join(DIST, n)
        print("%s  %.1f МБ" % (p, os.path.getsize(p) / 1048576))
    shutil.copy2(os.path.join(ROOT, "server", "perezvon_server.py"), os.path.join(DIST, "perezvon_server.py"))
    print("версия", ver)


if __name__ == "__main__":
    main()
