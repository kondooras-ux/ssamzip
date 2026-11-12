"""
쌤ZIP 추가 형식 처리 (표준 라이브러리만 사용 + 선택적으로 윈도우 내장 tar.exe 이용)

- tar 계열 : .tar .tar.gz(.tgz) .tar.bz2(.tbz2) .tar.xz(.txz)  → 만들기/풀기/목록/검사
- 단일 파일 : .gz .bz2 .xz                                      → 만들기(파일 1개)/풀기
- 외부 도구 : .rar .cab .iso .lzh .cpio .ar .xar .zst ...        → 풀기/목록/검사만
              (윈도우 10/11 내장 bsdtar = C:\\Windows\\System32\\tar.exe 를 호출한다.
               RAR 암호 파일, 분할 RAR 등은 지원하지 않는다.)
"""
import os
import re
import gzip
import bz2
import lzma
import shutil
import tarfile
import tempfile
import subprocess
import zlib

# 이름 끝으로 형식을 판별한다 (긴 것부터 검사해야 .tar.gz 가 .gz 보다 먼저 잡힌다)
TAR_SUFFIXES = {
    ".tar.gz": "gz", ".tgz": "gz", ".tar.bz2": "bz2", ".tbz2": "bz2", ".tbz": "bz2",
    ".tar.xz": "xz", ".txz": "xz", ".tar": "",
}
SINGLE_SUFFIXES = (".gz", ".bz2", ".xz")
EXTERNAL_SUFFIXES = (
    ".rar", ".cab", ".iso", ".lzh", ".lha", ".cpio", ".ar", ".xar", ".zst", ".tzst",
    ".tar.zst", ".lz", ".tar.lz", ".lz4", ".z", ".tar.z", ".taz", ".a",
)
# zip과 같은 구조라서 풀 수 있는 문서/패키지 형식 (연결 프로그램 등록과 일괄 풀기 대상은 아님)
ZIPLIKE_SUFFIXES = (
    ".jar", ".war", ".apk", ".epub", ".docx", ".xlsx", ".pptx", ".hwpx",
    ".odt", ".ods", ".odp", ".xpi", ".nupkg", ".whl", ".egg",
)

# 연결 프로그램 등록·일괄 풀기·'압축 파일 찾기' 대상. (.iso 등 더 드문 형식은 직접 열기만 지원)
ARCHIVE_EXTS = (".zip", ".7z", ".tar", ".tgz", ".tar.gz", ".tbz2", ".tbz", ".tar.bz2",
                ".txz", ".tar.xz", ".gz", ".bz2", ".xz", ".rar", ".cab", ".lzh", ".lha", ".zst")
VOLUME_RE = re.compile(r"\.(zip|7z|tar|tgz|tbz2|txz|tar\.gz|tar\.bz2|tar\.xz|gz|bz2|xz|rar)\.001$", re.I)

# 만들 수 있는 형식: 키 → (확장자, 설명)
CREATE_FORMATS = {
    "zip": (".zip", "ZIP"),
    "7z": (".7z", "7z"),
    "tgz": (".tar.gz", "TAR.GZ"),
    "tbz2": (".tar.bz2", "TAR.BZ2"),
    "txz": (".tar.xz", "TAR.XZ"),
    "tar": (".tar", "TAR"),
    "gz": (".gz", "GZ (파일 1개)"),
    "bz2": (".bz2", "BZ2 (파일 1개)"),
    "xz": (".xz", "XZ (파일 1개)"),
}
NO_PASSWORD_FORMATS = {"tgz", "tbz2", "txz", "tar", "gz", "bz2", "xz"}
SINGLE_FORMATS = {"gz", "bz2", "xz"}


def format_ext(fmt):
    return CREATE_FORMATS[fmt][0]


def _all_suffixes():
    return sorted([*TAR_SUFFIXES, *SINGLE_SUFFIXES, *EXTERNAL_SUFFIXES, ".zip", ".7z"],
                  key=len, reverse=True)


def kind_of(path):
    """'zip' | '7z' | 'tar' | 'single' | 'ext' | None (파일 이름만 보고 판단)"""
    n = os.path.basename(path).lower()
    for s in sorted(TAR_SUFFIXES, key=len, reverse=True):
        if n.endswith(s):
            return "tar"
    if n.endswith(".zip") or n.endswith(ZIPLIKE_SUFFIXES):
        return "zip"
    if n.endswith(".7z"):
        return "7z"
    for s in sorted(EXTERNAL_SUFFIXES, key=len, reverse=True):
        if n.endswith(s):
            return "ext"
    if n.endswith(SINGLE_SUFFIXES):
        return "single"
    return None


def strip_ext(name):
    """압축 파일 이름에서 압축 확장자만 뗀다 (a.tar.gz → a). 모르는 확장자면 마지막 확장자만 뗀다."""
    low = name.lower()
    for s in _all_suffixes() + sorted(ZIPLIKE_SUFFIXES, key=len, reverse=True):
        if low.endswith(s) and len(name) > len(s):
            return name[: -len(s)]
    return os.path.splitext(name)[0] or name


def _tar_mode(path, write=False):
    return "r:*"


# ───────────────────────── 외부 도구(bsdtar) ─────────────────────────

def find_bsdtar():
    """쓸 수 있는 bsdtar 실행 파일 경로. 없으면 None. (환경변수 SSAMZIP_BSDTAR 로 테스트/수동 지정 가능)"""
    env = os.environ.get("SSAMZIP_BSDTAR")
    if env:
        return env if os.path.isfile(env) else None
    if os.name == "nt":
        root = os.environ.get("SystemRoot", r"C:\Windows")
        p = os.path.join(root, "System32", "tar.exe")
        if os.path.isfile(p):
            return p
    return shutil.which("bsdtar")


def _popen(args, **kw):
    flags = 0x08000000 if os.name == "nt" else 0       # CREATE_NO_WINDOW
    return subprocess.Popen(args, creationflags=flags, **kw)


def _run_ext(args, cancel=None, stdout=None):
    """bsdtar 실행. (returncode, stderr 텍스트). 취소하면 프로세스를 끝내고 Cancelled를 던진다."""
    from core import Cancelled
    p = _popen(args, stdout=stdout if stdout is not None else subprocess.DEVNULL,
               stderr=subprocess.PIPE)
    err = b""
    while True:
        try:
            _, err = p.communicate(timeout=0.2)
            break
        except subprocess.TimeoutExpired:
            if cancel is not None and cancel.is_set():
                p.kill()
                p.communicate()
                raise Cancelled()
    text = err.decode("utf-8", "replace").strip()
    return p.returncode, text


def _abs(path):
    """'-'로 시작하는 이름이 옵션으로 읽히지 않도록 절대 경로로 넘긴다"""
    return os.path.abspath(path)


def _need_bsdtar():
    from core import ArchiveError
    exe = find_bsdtar()
    if not exe:
        raise ArchiveError("이 형식은 윈도우 10/11에 들어 있는 tar.exe가 있어야 풀 수 있습니다. "
                           "(이 컴퓨터에서는 찾지 못했습니다)")
    return exe


def _last_line(text):
    lines = [x for x in text.splitlines() if x.strip()]
    return lines[-1] if lines else ""


def list_ext(path, limit=None, cancel=None):
    from core import ArchiveError
    exe = _need_bsdtar()
    p = _popen([exe, "-tf", _abs(path)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = p.communicate()
    if p.returncode != 0 and not out:
        raise ArchiveError("압축 파일을 열 수 없습니다: " + (_last_line(err.decode("utf-8", "replace"))
                                                           or "손상되었거나 암호가 걸려 있을 수 있습니다."))
    names = [x for x in out.decode("utf-8", "replace").splitlines() if x.strip()]
    ents = [(n, 0) for n in names[:limit]]
    return ents, len(names), 0


def test_ext(path, cancel=None):
    from core import ArchiveError
    exe = _need_bsdtar()
    names = list_ext(path)[1]
    rc, err = _run_ext([exe, "-xOf", _abs(path)], cancel)
    if rc != 0:
        raise ArchiveError("파일에 문제가 있습니다: " + (_last_line(err) or "손상되었거나 암호가 걸려 있을 수 있습니다."))
    return {"files": names, "bad": [], "size": 0}


def extract_ext(path, dest, policy, strip_root, skip_mac_junk, progress, cancel, only):
    """bsdtar로 임시 폴더에 푼 뒤, 안전 검사를 거쳐 dest로 옮긴다."""
    from core import (ArchiveError, _plan_entries, _Progress, _check_space, fs, Cancelled)
    exe = _need_bsdtar()
    os.makedirs(fs(dest), exist_ok=True)
    tmp = tempfile.mkdtemp(prefix=".ssamzip_", dir=dest)
    try:
        rc, err = _run_ext([exe, "-xf", _abs(path), "-C", tmp], cancel)
        entries, skipped = [], []
        for root, dirs, files in os.walk(tmp):
            for d in dirs:
                full = os.path.join(root, d)
                rel = os.path.relpath(full, tmp).replace(os.sep, "/")
                if os.path.islink(full):
                    skipped.append((rel, "링크는 건너뜀"))
                else:
                    entries.append((full, rel + "/", True, 0))
            for f in files:
                full = os.path.join(root, f)
                rel = os.path.relpath(full, tmp).replace(os.sep, "/")
                if os.path.islink(full) or not os.path.isfile(full):
                    skipped.append((rel, "링크는 건너뜀"))
                else:
                    entries.append((full, rel, False, os.path.getsize(full)))
        if rc != 0 and not entries:
            raise ArchiveError("풀 수 없습니다: " + (_last_line(err) or "손상되었거나 암호가 걸려 있을 수 있습니다."))
        if rc != 0:
            skipped.append(("(일부)", "일부 항목을 풀지 못함: " + _last_line(err)))
        dirs_, files_ = _plan_entries(entries, dest, policy, strip_root, skip_mac_junk, only, skipped)
        total = sum(f[2] for f in files_)
        _check_space(dest, total)
        for d in dirs_:
            os.makedirs(fs(d), exist_ok=True)
        prog = _Progress(progress, total)
        for src, target, size, name in files_:
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            os.makedirs(fs(os.path.dirname(target)), exist_ok=True)
            if os.path.exists(fs(target)):
                os.remove(fs(target))
            shutil.move(fs(src), fs(target))
            prog.add(size, name)
    finally:
        shutil.rmtree(fs(tmp), ignore_errors=True)
    if progress:
        progress(total or 1, total or 1, "")
    return {"dest": dest, "files": len(files_), "size": total, "skipped": skipped,
            "targets": [f[1] for f in files_]}


# ───────────────────────── tar ─────────────────────────

_TAR_ERRORS = (tarfile.TarError, EOFError, zlib.error, lzma.LZMAError, OSError, ValueError)


def _bad(e=None):
    from core import ArchiveError
    return ArchiveError("압축 파일이 손상되었거나 지원하지 않는 형식입니다.")


def _tar_entries(tf):
    """(순번, 이름, 폴더 여부, 크기) 목록과 건너뛸 항목"""
    ents, skipped = [], []
    for i, m in enumerate(tf.getmembers()):
        if m.isdir():
            ents.append((i, m.name, True, 0))
        elif m.isfile():
            ents.append((i, m.name, False, m.size))
        else:
            skipped.append((m.name, "링크·장치 파일은 건너뜀"))
    return ents, skipped


def list_tar(path, limit=None):
    try:
        with tarfile.open(path, "r:*") as tf:
            ents, _ = _tar_entries(tf)
    except _TAR_ERRORS as e:
        raise _bad(e) from e
    out = [(n + ("/" if d and not n.endswith("/") else ""), s) for _, n, d, s in ents[:limit]]
    return out, len(ents), sum(e[3] for e in ents)


def extract_tar(path, dest, policy, strip_root, skip_mac_junk, progress, cancel, only):
    from core import (_plan_entries, _Progress, _check_space, fs, Cancelled)
    try:
        with tarfile.open(path, "r:*") as tf:
            entries, skipped = _tar_entries(tf)
        dirs, files = _plan_entries(entries, dest, policy, strip_root, skip_mac_junk, only, skipped)
        total = sum(f[2] for f in files)
        _check_space(dest, total)
        for d in dirs:
            os.makedirs(fs(d), exist_ok=True)
        want = {f[0]: f for f in files}              # 순번 → (순번, 대상, 크기, 이름)
        prog = _Progress(progress, total)
        done = []
        if want:
            with tarfile.open(path, "r:*") as tf:
                for i, m in enumerate(tf):
                    f = want.get(i)
                    if f is None:
                        continue
                    if cancel is not None and cancel.is_set():
                        raise Cancelled()
                    _, target, size, name = f
                    os.makedirs(fs(os.path.dirname(target)), exist_ok=True)
                    src = tf.extractfile(m)
                    ok = False
                    try:
                        with open(fs(target), "wb") as out:
                            while True:
                                if cancel is not None and cancel.is_set():
                                    raise Cancelled()
                                buf = src.read(1 << 20)
                                if not buf:
                                    break
                                out.write(buf)
                                prog.add(len(buf), name)
                        ok = True
                    finally:
                        if not ok:
                            try:
                                os.remove(fs(target))
                            except OSError:
                                pass
                    try:
                        os.utime(fs(target), (m.mtime, m.mtime))
                    except (OSError, OverflowError, ValueError):
                        pass
                    done.append(target)
    except Cancelled:
        raise
    except _TAR_ERRORS as e:
        from core import friendly_oserror
        if isinstance(e, OSError) and not isinstance(e, (EOFError,)):
            fe = friendly_oserror(e)
            if fe:
                raise fe from e
            if getattr(e, "errno", None) is not None:
                raise
        raise _bad(e) from e
    if progress:
        progress(total or 1, total or 1, "")
    return {"dest": dest, "files": len(files), "size": total, "skipped": skipped,
            "targets": [f[1] for f in files]}


def test_tar(path, progress=None, cancel=None):
    from core import Cancelled, _Progress
    bad, n, total = [], 0, 0
    try:
        with tarfile.open(path, "r:*") as tf:
            ents, _ = _tar_entries(tf)
            tsize = sum(e[3] for e in ents)
        prog = _Progress(progress, tsize)
        with tarfile.open(path, "r:*") as tf:
            for m in tf:
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                if not m.isfile():
                    continue
                n += 1
                src = tf.extractfile(m)
                got = 0
                while True:
                    buf = src.read(1 << 20)
                    if not buf:
                        break
                    got += len(buf)
                    prog.add(len(buf), m.name)
                if got != m.size:
                    bad.append(m.name)
                total += got
    except Cancelled:
        raise
    except _TAR_ERRORS as e:
        raise _bad(e) from e
    return {"files": n, "bad": bad, "size": total}


# ───────────────────────── 단일 파일 (.gz .bz2 .xz) ─────────────────────────

def _single_opener(path):
    n = path.lower()
    if n.endswith(".gz"):
        return gzip.open
    if n.endswith(".bz2"):
        return bz2.open
    return lzma.open


def single_name(path):
    return strip_ext(os.path.basename(path)) if kind_of(path) == "single" else os.path.basename(path)


def looks_like_tar(path):
    """.gz/.bz2/.xz 안쪽이 tar면 True (이름이 .tar.gz가 아니어도 tar로 다루기 위해)"""
    try:
        with _single_opener(path)(path, "rb") as f:
            head = f.read(512)
        return len(head) >= 262 and head[257:262] == b"ustar"
    except (OSError, EOFError, zlib.error, lzma.LZMAError, ValueError):
        return False


def list_single(path):
    return [(single_name(path), 0)], 1, 0


def extract_single(path, dest, policy, skip_mac_junk, progress, cancel, only):
    from core import (_plan_entries, _Progress, fs, Cancelled, friendly_oserror)
    name = single_name(path)
    skipped = []
    dirs, files = _plan_entries([(None, name, False, 0)], dest, policy, False, skip_mac_junk, only, skipped)
    if not files:
        return {"dest": dest, "files": 0, "size": 0, "skipped": skipped, "targets": []}
    target = files[0][1]
    os.makedirs(fs(os.path.dirname(target)), exist_ok=True)
    csize = os.path.getsize(path)
    prog = _Progress(progress, csize)
    total, ok = 0, False
    try:
        with open(path, "rb") as raw:                     # 진행률은 읽은 압축 데이터 기준
            opener = _single_opener(path)
            with opener(raw, "rb") as src, open(fs(target), "wb") as out:
                last = 0
                while True:
                    if cancel is not None and cancel.is_set():
                        raise Cancelled()
                    buf = src.read(1 << 20)
                    if not buf:
                        break
                    out.write(buf)
                    total += len(buf)
                    pos = raw.tell()
                    prog.add(max(pos - last, 0), name)
                    last = pos
        ok = True
    except Cancelled:
        raise
    except (EOFError, zlib.error, lzma.LZMAError, ValueError) as e:
        raise _bad(e) from e
    except OSError as e:
        fe = friendly_oserror(e)
        if fe:
            raise fe from e
        if isinstance(e, (gzip.BadGzipFile,)) or e.errno is None:
            raise _bad(e) from e
        raise
    finally:
        if not ok:
            try:
                os.remove(fs(target))
            except OSError:
                pass
    if progress:
        progress(csize or 1, csize or 1, "")
    return {"dest": dest, "files": 1, "size": total, "skipped": skipped, "targets": [target]}


def test_single(path, progress=None, cancel=None):
    from core import Cancelled
    total = 0
    try:
        with _single_opener(path)(path, "rb") as f:
            while True:
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                buf = f.read(1 << 20)
                if not buf:
                    break
                total += len(buf)
    except Cancelled:
        raise
    except (OSError, EOFError, zlib.error, lzma.LZMAError, ValueError) as e:
        raise _bad(e) from e
    return {"files": 1, "bad": [], "size": total}


# ───────────────────────── 만들기 ─────────────────────────

_LEVEL_GZ = {0: 1, 1: 1, 6: 6, 9: 9, 10: 9}
_LEVEL_BZ2 = {0: 1, 1: 1, 6: 6, 9: 9, 10: 9}
_LEVEL_XZ = {0: 0, 1: 1, 6: 6, 9: 9, 10: 9 | lzma.PRESET_EXTREME}


class _CountingReader:
    def __init__(self, f, prog, name, cancel):
        self.f, self.prog, self.name, self.cancel = f, prog, name, cancel

    def read(self, n=-1):
        from core import Cancelled
        if self.cancel is not None and self.cancel.is_set():
            raise Cancelled()
        b = self.f.read(n)
        if b:
            self.prog.add(len(b), self.name)
        return b


def compress_tar(items, part, fmt, level, prog, cancel, skipped):
    """items: [(원본 전체 경로, 압축 안 이름)]  이름이 '/'로 끝나면 빈 폴더"""
    from core import fs, Cancelled
    comp = {"tgz": "gz", "tbz2": "bz2", "txz": "xz", "tar": ""}[fmt]
    kw = {}
    if comp == "gz":
        kw["compresslevel"] = _LEVEL_GZ.get(level, 6)
    elif comp == "bz2":
        kw["compresslevel"] = _LEVEL_BZ2.get(level, 6)
    elif comp == "xz":
        kw["preset"] = _LEVEL_XZ.get(level, 6)
    mode = "w:" + comp if comp else "w"
    with tarfile.open(part, mode, format=tarfile.PAX_FORMAT, **kw) as tf:
        for full, arc in items:
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            name = arc.rstrip("/")
            try:
                st = os.stat(fs(full))
            except OSError:
                skipped.append((arc, "읽을 수 없음"))
                continue
            ti = tarfile.TarInfo(name)
            ti.mtime = int(st.st_mtime)
            ti.uid = ti.gid = 0
            ti.uname = ti.gname = ""
            if arc.endswith("/"):
                ti.type = tarfile.DIRTYPE
                ti.mode = 0o755
                tf.addfile(ti)
                continue
            ti.size = st.st_size
            ti.mode = 0o644
            try:
                src = open(fs(full), "rb")
            except OSError:
                skipped.append((arc, "읽을 수 없음 (다른 프로그램이 사용 중이거나 권한 없음)"))
                continue
            with src:
                tf.addfile(ti, _CountingReader(src, prog, arc, cancel))


def compress_single(items, part, fmt, level, prog, cancel):
    """파일 하나를 gz / bz2 / xz 로 압축"""
    from core import ArchiveError, fs, Cancelled
    files = [(f, a) for f, a in items if not a.endswith("/")]
    if len(files) != 1 or len(items) != 1:
        raise ArchiveError(f".{fmt} 형식은 파일 하나만 압축할 수 있습니다. 여러 개는 TAR.GZ 등을 선택하세요.")
    full, arc = files[0]
    if fmt == "gz":
        out = gzip.GzipFile(filename=os.path.basename(arc), mode="wb",
                            compresslevel=_LEVEL_GZ.get(level, 6), fileobj=open(part, "wb"),
                            mtime=int(os.path.getmtime(fs(full))))
    elif fmt == "bz2":
        out = bz2.BZ2File(part, "wb", compresslevel=_LEVEL_BZ2.get(level, 6))
    else:
        out = lzma.LZMAFile(part, "wb", preset=_LEVEL_XZ.get(level, 6))
    try:
        with open(fs(full), "rb") as src:
            while True:
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                buf = src.read(1 << 20)
                if not buf:
                    break
                out.write(buf)
                prog.add(len(buf), arc)
    finally:
        fobj = getattr(out, "fileobj", None)
        out.close()
        if fmt == "gz" and fobj is not None:
            fobj.close()
