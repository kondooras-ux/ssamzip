"""exe 만들기: python build.py --release (배포용 zip·SHA256SUMS.txt·latest.json까지 만들기)  /  python build.py   (폴더형, 권장)   /   python build.py --onefile  (exe 한 개)

폴더형(기본)을 권장하는 이유
- 우클릭 메뉴/더블클릭은 실행할 때마다 프로그램이 새로 켜진다. 폴더형은 압축을 풀지 않고 바로 시작해서 훨씬 빠르다.
- exe 한 개짜리는 켤 때마다 임시 폴더에 풀어서 느리고, 백신이 의심 파일로 잡는 경우가 더 많다.
- 배포는 dist\\ssamzip 폴더를 통째로 zip으로 묶어 전달하면 된다.
"""
import datetime
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
# 동적으로 불러와서 PyInstaller가 놓치기 쉬운 패키지들. 설치된 것만 자동으로 포함한다.
COLLECT = ["tkinterdnd2", "sv_ttk", "zopfli", "py7zr", "pyzipper", "Cryptodome", "pyppmd", "bcj",
           "inflate64", "brotli", "backports.zstd", "pyzstd", "zstandard", "multivolumefile", "texttable"]


def installed(name):
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def read_version():
    with open(os.path.join(HERE, "ssamzip.py"), encoding="utf-8") as f:
        return re.search(r'^VERSION = "([^"]+)"', f.read(), re.M).group(1)


def changelog_notes(version):
    """CHANGELOG.md 에서 해당 버전 절의 본문(업데이트 알림에 보여 줄 요약)"""
    try:
        with open(os.path.join(HERE, "CHANGELOG.md"), encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return ""
    m = re.search(r"^## \[?v?" + re.escape(version) + r"\]?.*?\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    return m.group(1).strip()[:1500] if m else ""


def write_buildinfo(path=None):
    """빌드 날짜와 쓴 라이브러리 버전을 buildinfo.json 으로 남긴다 (프로그램 '정보'에 표시됨)."""
    from importlib import metadata
    deps = {}
    for name in ("pyzipper", "py7zr", "zopfli", "tkinterdnd2", "sv-ttk", "pyinstaller"):
        try:
            deps[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            pass
    info = {"version": read_version(), "date": datetime.date.today().isoformat(),
            "python": sys.version.split()[0], "deps": deps}
    path = path or os.path.join(HERE, "buildinfo.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(info, f, ensure_ascii=False, indent=1)
    return path


def release_files(dist_dir, out_dir, version, notes=""):
    """
    배포용 파일 세트를 만든다.
      ssamzip-<버전>.zip   dist 폴더 내용을 담은 zip
      SHA256SUMS.txt       배포 파일의 SHA-256 (사람이 확인용으로 공지)
      latest.json          프로그램의 자동 업데이트가 읽는 안내 파일
    """
    os.makedirs(out_dir, exist_ok=True)
    name = f"ssamzip-{version}.zip"
    zpath = os.path.join(out_dir, name)
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for root, dirs, files in os.walk(dist_dir):
            dirs.sort()
            for f in sorted(files):
                full = os.path.join(root, f)
                z.write(full, os.path.relpath(full, dist_dir).replace(os.sep, "/"))
    h = hashlib.sha256()
    with open(zpath, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    digest = h.hexdigest()
    with open(os.path.join(out_dir, "SHA256SUMS.txt"), "w", encoding="utf-8", newline="\n") as f:
        f.write(f"{digest} *{name}\n")
    manifest = {"version": version, "url": name, "sha256": digest,
                "size": os.path.getsize(zpath), "notes": notes}
    with open(os.path.join(out_dir, "latest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    return manifest


def build_args(onefile=False):
    args = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed",
            "--name", "ssamzip", "--icon", os.path.join(HERE, "ssamzip.ico"),
            "--add-data", os.path.join(HERE, "ssamzip.ico") + os.pathsep + "."]
    for extra in ("CHANGELOG.md", "buildinfo.json"):
        if os.path.exists(os.path.join(HERE, extra)):
            args += ["--add-data", os.path.join(HERE, extra) + os.pathsep + "."]
    if onefile:
        args.append("--onefile")
    for m in COLLECT:
        if installed(m):
            args += ["--collect-all", m]
    args.append(os.path.join(HERE, "ssamzip.py"))
    return args


if __name__ == "__main__":
    if "--dry-run" not in sys.argv:
        write_buildinfo()
    a = build_args("--onefile" in sys.argv)
    if "--dry-run" in sys.argv:
        print(" ".join(a))
        sys.exit(0)
    rc = subprocess.call(a, cwd=HERE)
    if rc == 0 and "--release" in sys.argv:
        ver = read_version()
        dist = os.path.join(HERE, "dist", "ssamzip")
        m = release_files(dist, os.path.join(HERE, "release"), ver, changelog_notes(ver))
        print(f"배포 파일: release/ssamzip-{ver}.zip  SHA-256 {m['sha256']}")
    sys.exit(rc)
