"""
쌤ZIP 자동 업데이트

동작 방식
1) 설정에 적은 '업데이트 주소'(latest.json)를 받아서 현재 버전보다 새 버전이 있는지 본다.
   GitHub 저장소 주소(https://github.com/사용자/저장소)만 적어도 releases/latest/download/latest.json 으로 바꿔 쓴다.
2) 새 버전이 있으면 파일을 받아서 SHA-256이 latest.json 과 같은지 확인한다. 다르면 버리고 설치하지 않는다.
3) exe로 묶인 윈도우 버전은 프로그램이 끝난 뒤 업데이트 스크립트가 파일을 바꾸고 다시 실행한다.
   (이전 파일은 .old 로 남겨 두었다가 실패하면 되돌린다.)

latest.json 형식
{"version": "3.2.0", "url": "ssamzip-3.2.0.zip", "sha256": "…64자…", "size": 12345678, "notes": "바뀐 점"}
url 이 상대 경로면 latest.json 이 있는 주소를 기준으로 받는다.

보안
- https 주소만 허용한다 (시험용으로 환경변수 SSAMZIP_UPDATE_INSECURE=1 이면 localhost http 허용).
- SHA-256은 '받는 도중 파일이 깨지거나 바뀌지 않았는지' 확인하는 용도다. 업데이트 주소를 올린 계정 자체가
  뚫리면 막을 수 없으므로, 그 저장소 계정은 2단계 인증을 켜 두어야 한다.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import zipfile

MAX_MANIFEST = 64 * 1024
GITHUB_RE = re.compile(r"^https://github\.com/([\w.-]+)/([\w.-]+?)(?:\.git)?/?$", re.I)


class UpdateError(Exception):
    pass


def parse_version(v):
    return tuple(int(x) for x in re.findall(r"\d+", str(v))) or (0,)


def is_newer(candidate, current):
    a, b = list(parse_version(candidate)), list(parse_version(current))
    n = max(len(a), len(b))
    return a + [0] * (n - len(a)) > b + [0] * (n - len(b))


def normalize_url(url):
    url = (url or "").strip()
    m = GITHUB_RE.match(url)
    if m:
        return f"https://github.com/{m.group(1)}/{m.group(2)}/releases/latest/download/latest.json"
    return url


def _insecure_ok():
    return os.environ.get("SSAMZIP_UPDATE_INSECURE") == "1"


def validate_url(url):
    p = urllib.parse.urlparse(url)
    if p.scheme == "https" and p.netloc:
        return url
    if p.scheme == "http" and p.hostname in ("localhost", "127.0.0.1") and _insecure_ok():
        return url
    raise UpdateError("업데이트 주소는 https:// 로 시작해야 합니다.")


def _open(url, timeout):
    req = urllib.request.Request(url, headers={"User-Agent": "ssamzip-updater"})
    return urllib.request.urlopen(req, timeout=timeout)


def fetch_manifest(url, timeout=8):
    url = validate_url(normalize_url(url))
    try:
        with _open(url, timeout) as r:
            final = r.geturl()
            validate_url(final)                       # 리디렉션 결과도 https여야 함
            raw = r.read(MAX_MANIFEST + 1)
    except UpdateError:
        raise
    except Exception as e:
        raise UpdateError(f"업데이트 정보를 가져오지 못했습니다. ({type(e).__name__})") from e
    if len(raw) > MAX_MANIFEST:
        raise UpdateError("업데이트 정보가 비정상적으로 큽니다.")
    try:
        m = json.loads(raw.decode("utf-8-sig"))
        version = str(m["version"]).strip()
        sha = str(m["sha256"]).strip().lower()
        link = str(m["url"]).strip()
    except (ValueError, KeyError, TypeError) as e:
        raise UpdateError("업데이트 정보 형식이 올바르지 않습니다.") from e
    if not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise UpdateError("업데이트 정보의 SHA-256 값이 올바르지 않습니다.")
    if not re.fullmatch(r"\d+(\.\d+){0,3}", version):
        raise UpdateError("업데이트 정보의 버전 형식이 올바르지 않습니다.")
    link = validate_url(urllib.parse.urljoin(final, link))
    size = m.get("size")
    return {"version": version, "url": link, "sha256": sha,
            "size": int(size) if isinstance(size, int) and size > 0 else None,
            "notes": str(m.get("notes", ""))[:4000]}


def check(url, current, timeout=8):
    """새 버전이 있으면 매니페스트(dict), 없으면 None"""
    m = fetch_manifest(url, timeout)
    return m if is_newer(m["version"], current) else None


def file_sha256(path, cancel=None, progress=None):
    h = hashlib.sha256()
    total = os.path.getsize(path)
    done = 0
    with open(path, "rb") as f:
        while True:
            if cancel is not None and cancel.is_set():
                raise UpdateError("취소되었습니다.")
            b = f.read(1 << 20)
            if not b:
                break
            h.update(b)
            done += len(b)
            if progress:
                progress(done, total)
    return h.hexdigest()


def download(manifest, dest_dir, progress=None, cancel=None, timeout=20):
    """받아서 SHA-256 확인까지 마친 파일 경로를 돌려준다. 확인에 실패하면 지우고 UpdateError."""
    try:
        os.makedirs(dest_dir, exist_ok=True)
    except OSError as e:
        raise UpdateError("받을 폴더를 만들 수 없습니다.") from e
    name = os.path.basename(urllib.parse.urlparse(manifest["url"]).path) or "update.bin"
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    final = os.path.join(dest_dir, name)
    part = final + ".part"
    h = hashlib.sha256()
    got = 0
    try:
        with _open(manifest["url"], timeout) as r, open(part, "wb") as out:
            validate_url(r.geturl())
            total = manifest.get("size") or int(r.headers.get("Content-Length") or 0)
            while True:
                if cancel is not None and cancel.is_set():
                    raise UpdateError("취소되었습니다.")
                b = r.read(1 << 18)
                if not b:
                    break
                out.write(b)
                h.update(b)
                got += len(b)
                if manifest.get("size") and got > manifest["size"] + 1024:
                    raise UpdateError("받는 파일이 안내된 크기보다 큽니다.")
                if progress:
                    progress(got, total)
        if manifest.get("size") and got != manifest["size"]:
            raise UpdateError("받은 파일 크기가 안내와 다릅니다. 다시 시도해 주세요.")
        if h.hexdigest() != manifest["sha256"]:
            raise UpdateError("받은 파일의 SHA-256이 안내와 다릅니다. 설치하지 않고 버렸습니다.")
        os.replace(part, final)
        return final
    except UpdateError:
        _rm(part)
        raise
    except Exception as e:
        _rm(part)
        raise UpdateError(f"받는 중 오류가 났습니다. ({type(e).__name__})") from e


def _rm(p):
    try:
        os.remove(p)
    except OSError:
        pass


# ───────────────────────── 설치 ─────────────────────────

def can_self_update():
    return os.name == "nt" and bool(getattr(sys, "frozen", False))


def app_dir():
    return os.path.dirname(os.path.abspath(sys.executable))


def stage(downloaded, staging_dir):
    """내려받은 파일(.zip 또는 .exe)을 staging_dir 에 펼쳐 둔다. 안전하지 않은 경로는 거부."""
    shutil.rmtree(staging_dir, ignore_errors=True)
    os.makedirs(staging_dir)
    if downloaded.lower().endswith(".zip"):
        root = os.path.abspath(staging_dir)
        with zipfile.ZipFile(downloaded) as z:
            names = [n for n in z.namelist() if n and not n.endswith("/")]
            for n in names:
                tgt = os.path.abspath(os.path.join(root, n))
                if os.path.isabs(n) or ".." in n.replace("\\", "/").split("/") or not tgt.startswith(root + os.sep):
                    raise UpdateError("업데이트 파일에 안전하지 않은 경로가 있어 설치하지 않았습니다.")
            z.extractall(root)
        # 폴더 하나로 감싸져 있으면 한 겹 벗긴다
        tops = os.listdir(root)
        if len(tops) == 1 and os.path.isdir(os.path.join(root, tops[0])):
            inner = os.path.join(root, tops[0])
            for n in os.listdir(inner):
                shutil.move(os.path.join(inner, n), os.path.join(root, n))
            os.rmdir(inner)
    else:
        shutil.copy2(downloaded, os.path.join(staging_dir, os.path.basename(downloaded)))
    return staging_dir


def build_update_script(app_directory, exe_name, staging_dir, pid=0):
    """업데이트를 마무리하는 .bat 내용. 프로그램이 끝나 실행 파일을 바꿀 수 있게 될 때까지 기다린다."""
    a, s, e = app_directory, staging_dir, exe_name
    return "\r\n".join([
        "@echo off",
        "chcp 65001 >nul",
        "setlocal",
        f'set "APP={a}"',
        f'set "NEW={s}"',
        f'set "EXE={e}"',
        f'set "PID={int(pid)}"',
        "set /a TRY=0",
        ":wait",
        'tasklist /FI "PID eq %PID%" 2>nul | find "%PID%" >nul',
        "if errorlevel 1 goto swap",
        "set /a TRY+=1",
        "if %TRY% GEQ 60 goto fail",
        "timeout /t 1 /nobreak >nul",
        "goto wait",
        ":swap",
        'move /y "%APP%\\%EXE%" "%APP%\\%EXE%.old" >nul 2>&1',
        'xcopy "%NEW%\\*" "%APP%\\" /E /Y /I /Q >nul',
        "if errorlevel 1 goto rollback",
        'if not exist "%APP%\\%EXE%" goto rollback',
        'del /q "%APP%\\%EXE%.old" >nul 2>&1',
        'rmdir /s /q "%NEW%" >nul 2>&1',
        'start "" "%APP%\\%EXE%"',
        "goto end",
        ":rollback",
        'del /q "%APP%\\%EXE%" >nul 2>&1',
        'move /y "%APP%\\%EXE%.old" "%APP%\\%EXE%" >nul 2>&1',
        'start "" "%APP%\\%EXE%"',
        "goto end",
        ":fail",
        'echo 업데이트를 마치지 못했습니다. 쌤ZIP이 종료되지 않았습니다.',
        "pause",
        ":end",
        '(goto) 2>nul & del "%~f0"',
        "",
    ])


def launch_update(downloaded):
    """받은 파일을 준비하고 업데이트 스크립트를 띄운다. 호출한 쪽은 곧바로 프로그램을 종료해야 한다."""
    if not can_self_update():
        raise UpdateError("exe로 설치된 윈도우 버전에서만 자동 설치할 수 있습니다. 받은 파일로 직접 바꿔 주세요.")
    work = tempfile.mkdtemp(prefix="ssamzip_upd_")
    staging = stage(downloaded, os.path.join(work, "new"))
    exe = os.path.basename(sys.executable)
    if not os.path.isfile(os.path.join(staging, exe)):
        raise UpdateError("업데이트 파일 안에 실행 파일이 없어 설치하지 않았습니다.")
    bat = os.path.join(work, "update.bat")
    with open(bat, "w", encoding="utf-8", newline="") as f:
        f.write(build_update_script(app_dir(), exe, staging, os.getpid()))
    subprocess.Popen(["cmd", "/c", bat], creationflags=0x08000000 | 0x00000008, close_fds=True)
    return bat


def due(last_check, interval_hours=24, now=None):
    return (now if now is not None else time.time()) - float(last_check or 0) >= interval_hours * 3600
