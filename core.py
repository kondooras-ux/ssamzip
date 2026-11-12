"""
core.py - 쌤ZIP 압축/해제 핵심 로직 (화면과 무관하게 동작)

속도 개선
- 이미 압축된 형식(jpg, mp4, docx, hwpx, zip ...)은 다시 압축하지 않고 저장만(STORED)
- ZIP 해제는 여러 스레드로 병렬 처리 (zlib/AES가 GIL을 풀어주므로 실제로 빨라짐)
- 폴더 안 여러 압축 파일은 동시에 처리
- 진행률 콜백은 0.08초 간격으로만 호출 (화면 갱신 부담 감소)

편의/안전
- 구형 윈도우 zip 한글 파일명(cp949) 자동 보정
- 압축 파일 안의 '../' 등 경로 탈출 차단
- 윈도우에서 쓸 수 없는 파일명 글자 자동 치환, 260자 넘는 경로 지원
- 맥에서 만든 zip의 __MACOSX, .DS_Store 찌꺼기 건너뛰기
- '폴더 안에 같은 이름 폴더가 또 들어있는' 이중 폴더 방지
- 같은 이름이 있으면 덮어쓰기/건너뛰기/이름 바꿔 저장 중 선택
- 디스크 공간 부족 사전 검사, 실패/취소 시 미완성 파일 정리
"""
import os
import re
import time
import lzma
import shutil
import threading
import zipfile

import pyzipper
import py7zr

import zipwriter
import moreformats as mf

ARCHIVE_EXTS = mf.ARCHIVE_EXTS

# 이미 압축된 형식: 다시 압축해도 크기가 거의 안 줄고 시간만 걸린다
STORED_EXTS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".avif",
    ".mp3", ".aac", ".m4a", ".ogg", ".flac",
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".webm", ".m4v",
    ".zip", ".7z", ".rar", ".gz", ".bz2", ".xz", ".zst", ".tgz",
    ".docx", ".xlsx", ".pptx", ".hwpx", ".odt", ".ods", ".odp", ".epub", ".apk", ".jar",
}

# pyzipper는 표준 zipfile과 다른 예외 클래스를 쓰므로 둘 다 잡아야 한다
BAD_ZIP = (zipfile.BadZipFile, pyzipper.BadZipFile)

VOLUME_RE = mf.VOLUME_RE

JUNK_FILE_NAMES = {"thumbs.db", "desktop.ini", ".ds_store"}

_WIN_BAD_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')
_WIN_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)),
                 *(f"lpt{i}" for i in range(1, 10))}


class Cancelled(Exception):
    """사용자가 취소함"""


class WrongPassword(Exception):
    """암호가 없거나 틀림"""


class ArchiveError(Exception):
    """사용자에게 그대로 보여줄 수 있는 오류"""


def friendly_oserror(e, where=""):
    """OSError를 사용자에게 보여줄 한국어 ArchiveError로 바꾼다 (바꿀 수 없으면 None)."""
    import errno
    code = getattr(e, "errno", None)
    if code == errno.ENOSPC:
        return ArchiveError("디스크 공간이 부족합니다. 저장 위치의 여유 공간을 확보한 뒤 다시 시도하세요.")
    if isinstance(e, PermissionError) or code in (errno.EACCES, errno.EPERM, errno.EROFS):
        return ArchiveError(f"저장 위치에 쓸 수 없습니다(권한 없음 또는 다른 프로그램이 사용 중){where}.")
    if code == errno.ENAMETOOLONG:
        return ArchiveError("경로나 파일 이름이 너무 깁니다. 더 짧은 폴더에 저장해 보세요.")
    if code in (errno.EEXIST, errno.ENOTDIR):
        return ArchiveError("같은 이름의 파일과 폴더가 겹쳐서 만들 수 없습니다.")
    return None


# ───────────────────────── 공용 도우미 ─────────────────────────

def fs(path):
    """윈도우 260자 제한 회피용 경로 변환 (윈도우가 아니면 그대로)."""
    if os.name != "nt":
        return path
    path = os.path.abspath(path)
    if len(path) < 240 or path.startswith("\\\\?\\"):
        return path
    if path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + path[2:]
    return "\\\\?\\" + path


def fmt_size(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:,.0f} {unit}" if unit == "B" else f"{n:,.1f} {unit}"
        n /= 1024


def fmt_time(sec):
    sec = int(max(0, sec))
    if sec < 60:
        return f"{sec}초"
    if sec < 3600:
        return f"{sec // 60}분 {sec % 60}초"
    return f"{sec // 3600}시간 {(sec % 3600) // 60}분"


def is_junk_file(name):
    """압축할 때 빼면 좋은 임시/시스템 파일"""
    n = name.lower()
    return n in JUNK_FILE_NAMES or n.startswith("~$")


def is_mac_junk(name):
    """맥에서 만든 zip에 딸려 오는 찌꺼기"""
    parts = name.replace("\\", "/").strip("/").split("/")
    return "__MACOSX" in parts or parts[-1].lower() == ".ds_store" or parts[-1].startswith("._")


def sanitize_component(comp, windows=None):
    if windows is None:
        windows = os.name == "nt"
    if not windows:
        return comp
    comp = _WIN_BAD_CHARS.sub("_", comp).rstrip(" .")
    if not comp:
        return "_"
    if comp.split(".")[0].lower() in _WIN_RESERVED:
        comp = "_" + comp
    return comp


def normalize_name(name):
    name = name.replace("\\", "/").lstrip("/")
    while name.startswith("./"):
        name = name[2:]
    return name


def safe_join(dest, name, windows=None):
    """압축 파일 안의 경로가 dest 밖으로 나가지 못하게 막는다(Zip Slip)."""
    parts = [p for p in normalize_name(name).split("/") if p not in ("", ".")]
    if ".." in parts or (parts and re.match(r"^[A-Za-z]:$", parts[0])):
        raise ValueError(f"위험한 경로가 들어 있어 풀지 않았습니다: {name}")
    parts = [sanitize_component(p, windows) for p in parts]
    target = os.path.abspath(os.path.join(dest, *parts)) if parts else os.path.abspath(dest)
    dest_abs = os.path.abspath(dest)
    if target != dest_abs and not target.startswith(dest_abs + os.sep):
        raise ValueError(f"위험한 경로가 들어 있어 풀지 않았습니다: {name}")
    return target


def detect_root(names):
    """모든 항목이 폴더 하나 안에 들어 있으면 그 폴더 이름, 아니면 None."""
    roots, has_inner = set(), False
    for n in names:
        n = normalize_name(n).strip("/")
        if not n:
            continue
        parts = n.split("/")
        roots.add(parts[0])
        if len(parts) > 1:
            has_inner = True
    if len(roots) == 1 and has_inner:
        return next(iter(roots))
    return None


def unique_file(path):
    if not os.path.exists(fs(path)):
        return path
    base, ext = os.path.splitext(path)
    i = 2
    while os.path.exists(fs(f"{base} ({i}){ext}")):
        i += 1
    return f"{base} ({i}){ext}"


def unique_dir(path):
    if not os.path.exists(fs(path)):
        return path
    i = 2
    while os.path.exists(fs(f"{path} ({i})")):
        i += 1
    return f"{path} ({i})"


def archive_stem(path):
    name = os.path.basename(path)
    if VOLUME_RE.search(name):
        name = name[:-4]
    return mf.strip_ext(name)


def _check_space(dest, need):
    probe = os.path.abspath(dest)
    while probe and not os.path.exists(probe):
        parent = os.path.dirname(probe)
        if parent == probe:
            break
        probe = parent
    try:
        free = shutil.disk_usage(probe).free
    except OSError:
        return
    if need > free:
        raise ArchiveError(
            f"디스크 공간이 부족합니다. (필요 {fmt_size(need)} / 남은 공간 {fmt_size(free)})")


class _Progress:
    """진행률 콜백을 일정 간격으로만 호출하고, 여러 스레드에서 안전하게 쓰게 한다."""

    def __init__(self, cb, total, interval=0.08):
        self.cb, self.total, self.interval = cb, max(total, 1), interval
        self.done, self.last = 0, 0.0
        self.lock = threading.Lock()

    def add(self, n, name=""):
        if not self.cb:
            return
        with self.lock:
            self.done += n
            now = time.monotonic()
            if now - self.last >= self.interval or self.done >= self.total:
                self.last = now
                self.cb(min(self.done, self.total), self.total, name)


# ───────────────────────── 압축 ─────────────────────────

def collect_files(paths, exclude_junk=True):
    """선택한 파일/폴더를 [(실제경로, 압축 내부 경로)]로 펼친다. 폴더 항목은 '/'로 끝난다."""
    items, seen = [], set()

    def add(full, arc):
        if arc not in seen:
            seen.add(arc)
            items.append((full, arc))

    for p in paths:
        p = os.path.abspath(p)
        if len(p) >= 200:                       # 윈도우 260자 제한 회피 (짧은 경로는 손대지 않음)
            p = fs(p)
        base = os.path.dirname(p)
        if os.path.isdir(p):
            for root, dirs, files in os.walk(p):
                dirs[:] = sorted(d for d in dirs if not (exclude_junk and is_junk_file(d)))
                files = sorted(f for f in files if not (exclude_junk and is_junk_file(f)))
                rel_root = os.path.relpath(root, base).replace("\\", "/")
                if not dirs and not files:
                    add(root, rel_root + "/")          # 빈 폴더 보존
                for f in files:
                    full = os.path.join(root, f)
                    if os.path.isfile(full):          # 깨진 바로가기, 소켓 같은 특수 파일은 제외
                        add(full, f"{rel_root}/{f}")
        elif os.path.isfile(p):
            if not (exclude_junk and is_junk_file(os.path.basename(p))):
                add(p, os.path.basename(p))
    return items


def scan_size(paths, exclude_junk=True):
    """(파일 수, 총 크기) - 화면에 요약을 보여줄 때 사용"""
    items = collect_files(paths, exclude_junk)
    files = [f for f, a in items if not a.endswith("/")]
    total = 0
    for f in files:
        try:
            total += os.path.getsize(f)
        except OSError:
            pass
    return len(files), total


def compress(paths, out_path, fmt="zip", password=None, level=6,
             progress=None, cancel=None, exclude_junk=True, workers=None, split_size=None):
    """
    fmt: 'zip' | '7z' | 'tgz'(.tar.gz) | 'tbz2'(.tar.bz2) | 'txz'(.tar.xz) | 'tar' | 'gz' | 'bz2' | 'xz'
         (gz/bz2/xz는 파일 하나만, tar 계열과 단일 형식은 암호 없음)
    level: 0(저장만) / 1(빠름) / 6(보통) / 9(최대) / 10(초고압축: zip은 Zopfli, 7z는 설정 두 가지를 시험)
    workers: 압축에 쓸 스레드 수(기본: CPU 코어 수, 최대 8)
    split_size: 바이트. 주면 결과가 그보다 클 때 'out.zip.001, .002 ...'로 나눠 저장(7-Zip과 같은 방식)
    progress(done_bytes, total_bytes, 현재파일)
    반환: {'orig': 원본 바이트, 'size': 압축 파일 바이트, 'files': 파일 수}
    """
    if fmt in mf.NO_PASSWORD_FORMATS and password:
        raise ArchiveError("이 형식은 암호를 지원하지 않습니다. 암호가 필요하면 ZIP이나 7z를 선택하세요.")
    items = collect_files(paths, exclude_junk)
    skipped = []                               # [(압축 안의 이름, 이유)] 읽을 수 없어 건너뛴 파일
    out_abs = os.path.abspath(out_path)
    part = out_abs + ".part"
    items = [(f, a) for f, a in items if os.path.abspath(f) not in (out_abs, part)]
    if not items:
        raise ArchiveError("압축할 파일이 없습니다.")
    sizes = {f: os.path.getsize(f) for f, a in items if not a.endswith("/")}
    total = sum(sizes.values())
    prog = _Progress(progress, total)
    os.makedirs(os.path.dirname(out_abs) or ".", exist_ok=True)
    try:
        if fmt == "zip":
            _compress_zip(items, sizes, part, password, level, prog, cancel, workers, skipped)
        elif fmt == "7z":
            _compress_7z(items, sizes, part, password, level, prog, cancel, skipped)
        elif fmt in ("tgz", "tbz2", "txz", "tar"):
            mf.compress_tar(items, part, fmt, level, prog, cancel, skipped)
        elif fmt in mf.SINGLE_FORMATS:
            mf.compress_single(items, part, fmt, level, prog, cancel)
        else:
            raise ArchiveError(f"지원하지 않는 형식입니다: {fmt}")
        if len(skipped) >= len(sizes) and sizes:
            raise ArchiveError("읽을 수 있는 파일이 하나도 없습니다. (다른 프로그램이 사용 중이거나 권한이 없음)")
        os.replace(part, out_abs)          # 끝까지 성공했을 때만 진짜 이름으로
    except OSError as e:
        if os.path.exists(part):
            try:
                os.remove(part)
            except OSError:
                pass
        fe = friendly_oserror(e)
        if fe:
            raise fe from e
        raise
    except BaseException:
        if os.path.exists(part):
            try:
                os.remove(part)
            except OSError:
                pass
        raise
    if progress:
        progress(total or 1, total or 1, "")
    size = os.path.getsize(out_abs)
    parts = [out_abs]
    if split_size and size > split_size:
        parts = split_archive(out_abs, split_size)
    return {"orig": total, "size": size, "files": len(sizes) - len(skipped), "parts": parts,
            "skipped": skipped}


def _zip_info(zf, full, arc, level):
    st = os.stat(full)
    cls = getattr(zf, "zipinfo_cls", zipfile.ZipInfo)
    zi = cls(arc)
    t = time.localtime(st.st_mtime)[:6]
    zi.date_time = t if t[0] >= 1980 else (1980, 1, 1, 0, 0, 0)
    zi.external_attr = (st.st_mode & 0xFFFF) << 16
    zi.compress_type = (zipfile.ZIP_STORED
                        if (level == 0 or st.st_size == 0 or zipwriter.looks_incompressible(full, st.st_size))
                        else zipfile.ZIP_DEFLATED)
    zi._compresslevel = min(level, 9)
    return zi, st.st_size


def _compress_zip(items, sizes, part, password, level, prog, cancel, workers=None, skipped=None):
    skipped = skipped if skipped is not None else []
    if password:
        _compress_zip_aes(items, part, password, level, prog, cancel, skipped)
        return
    # 암호 없는 zip: 자체 작성기 (병렬 deflate / Zopfli / 압축불가 자동판별 / ZIP64)
    def on_prog(n, name):
        prog.add(n, name)
    w = zipwriter.ZipWriter(part, workers=workers, level=level, cancel=cancel, progress=on_prog)
    try:
        for full, arc in items:
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            if arc.endswith("/"):
                try:
                    mt = os.stat(full).st_mtime
                except OSError:
                    mt = None
                w.add_dir(arc, mt)
            else:
                try:
                    w.add_file(full, arc)
                except zipwriter.SkipFile as sk:
                    skipped.append((arc, sk.reason))
                    prog.add(sizes.get(full, 0), arc)
        w.close()
    except zipwriter.Cancelled:
        w.abort()
        raise Cancelled()
    except BaseException:
        w.abort()
        raise


def _compress_zip_aes(items, part, password, level, prog, cancel, skipped):
    """암호(AES-256) zip. 암호화는 한 줄로 이어져야 해서 병렬 압축은 하지 않는다."""
    lv = min(max(level, 1), 9)
    zf = pyzipper.AESZipFile(part, "w", compression=pyzipper.ZIP_DEFLATED,
                             compresslevel=lv, encryption=pyzipper.WZ_AES)
    zf.setpassword(password.encode("utf-8"))
    zf.setencryption(pyzipper.WZ_AES, nbits=256)
    with zf:
        for full, arc in items:
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            if arc.endswith("/"):
                zf.writestr(arc, b"")
                continue
            try:
                zi, size = _zip_info(zf, fs(full), arc, lv)
                src = open(fs(full), "rb")
            except OSError as ex:
                skipped.append((arc, zipwriter._why(ex)))
                continue
            with src, zf.open(zi, "w", force_zip64=size > 0x7FFFFFFF) as dst:
                while True:
                    buf = src.read(4 * 1024 * 1024)
                    if not buf:
                        break
                    if cancel is not None and cancel.is_set():
                        raise Cancelled()
                    dst.write(buf)
                    prog.add(len(buf), arc)


_X86_EXTS = {".exe", ".dll", ".sys", ".ocx", ".scr", ".cpl", ".so"}


def _is_x86_exec(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in _X86_EXTS:
        return True
    try:
        with open(path, "rb") as f:
            head = f.read(4)
    except OSError:
        return False
    return head[:2] == b"MZ" or head == b"\x7fELF"


def _dict_size(total, level):
    """파일 총량에 맞는 사전 크기(2의 거듭제곱). 너무 크면 메모리만 먹는다."""
    cap = 64 << 20 if level >= 10 else (32 << 20 if level >= 9 else 8 << 20)
    d = 1 << 20
    while d < total and d < cap:
        d <<= 1
    return d


def _7z_filters(items, sizes, level, preset_extra=0):
    total = sum(sizes.values())
    preset = {1: 1, 6: 5, 9: 9, 10: 9}.get(level, 5) | preset_extra
    lz = {"id": py7zr.FILTER_LZMA2, "preset": preset}
    if level >= 9:
        lz["dict_size"] = _dict_size(total, level)
    chain = [lz]
    if level >= 6 and total:
        x86 = sum(sizes[f] for f, a in items if f in sizes and _is_x86_exec(f))
        if x86 >= total * 0.4:                  # 실행파일이 많으면 x86 변환 필터가 3~5% 더 줄여 준다
            chain.insert(0, {"id": py7zr.FILTER_X86})
    return chain


def _write_7z(items, sizes, part, password, filters, prog, cancel, skipped=None):
    kwargs = {"filters": filters}
    if password:
        kwargs.update(password=password, header_encryption=True)
    with py7zr.SevenZipFile(part, "w", **kwargs) as z:
        for full, arc in items:
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            try:
                if not arc.endswith("/"):
                    open(fs(full), "rb").close()           # 읽을 수 있는지 먼저 확인 (사용 중인 파일 등)
            except OSError as ex:
                if skipped is not None:
                    skipped.append((arc, zipwriter._why(ex)))
                prog.add(sizes.get(full, 0), arc)
                continue
            z.write(fs(full), arc.rstrip("/"))
            prog.add(sizes.get(full, 0), arc)


def _compress_7z(items, sizes, part, password, level, prog, cancel, skipped=None):
    # 비슷한 파일끼리 모이면 solid 압축이 더 잘 된다: 폴더 먼저, 그다음 확장자 순
    items = sorted(items, key=lambda it: (0 if it[1].endswith("/") else 1,
                                          os.path.splitext(it[1])[1].lower(), it[1]))
    total = sum(sizes.values())
    if level >= 10 and total <= 48 << 20:
        # 초고압축: 두 설정을 모두 해 보고 더 작은 쪽을 쓴다 (시간은 약 2배)
        alt = part + ".b"
        try:
            half = _Progress(None, 1)
            _write_7z(items, sizes, part, password, _7z_filters(items, sizes, level), prog, cancel, skipped)
            _write_7z(items, sizes, alt, password,
                      _7z_filters(items, sizes, level, lzma.PRESET_EXTREME), half, cancel, [])
            if os.path.getsize(alt) < os.path.getsize(part):
                os.replace(alt, part)
        finally:
            if os.path.exists(alt):
                os.remove(alt)
        return
    _write_7z(items, sizes, part, password, _7z_filters(items, sizes, level), prog, cancel, skipped)


# ───────────────────────── 목록 / 해제 ─────────────────────────

def _fix_zip_name(info, encoding="auto"):
    """
    'UTF-8 표시'가 없는 zip의 파일명을 복원한다. 표시가 없으면 파이썬은 cp437로 읽어 한글이 깨진다.
    - 맥/리눅스에서 만든 zip: 실제로는 UTF-8인데 표시만 빠진 경우가 많다
    - 알집/구형 윈도우 zip: 실제로 cp949
    원래 바이트가 올바른 UTF-8이면 UTF-8로, 아니면 cp949로 해석한다.
    """
    name = info.filename
    if info.flag_bits & 0x800:
        return name
    try:
        raw = name.encode("cp437")
    except UnicodeEncodeError:
        return name
    if all(b < 0x80 for b in raw):
        return name
    for enc in (("utf-8", "cp949") if encoding == "auto" else (encoding,)):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return name


def archive_encrypted(path):
    """암호가 걸린 압축 파일인지 (확실할 때만 True)"""
    if VOLUME_RE.search(path):
        try:
            with joined_volumes(path) as p:
                return archive_encrypted(p)
        except Exception:
            return False
    kind = mf.kind_of(path)
    try:
        if kind == "zip":
            with pyzipper.AESZipFile(path) as zf:
                return any(i.flag_bits & 0x1 for i in zf.infolist())
        if kind == "7z":
            try:
                with py7zr.SevenZipFile(path, "r") as z:
                    return bool(z.needs_password())
            except py7zr.exceptions.PasswordRequired:
                return True
    except Exception:
        pass
    return False


def _open_7z(path, password):
    """7z 열기. 파일 이름까지 암호화된 파일은 틀린 암호면 헤더가 깨져 읽히므로 따로 처리한다."""
    kw = {"password": password} if password else {}
    try:
        return py7zr.SevenZipFile(path, "r", **kw)
    except py7zr.exceptions.PasswordRequired as e:
        raise WrongPassword("암호가 필요합니다.") from e
    except Exception as e:
        if password:
            raise WrongPassword("암호가 틀렸거나 파일이 손상되었습니다.") from e
        raise ArchiveError("압축 파일이 손상되었거나 지원하지 않는 형식입니다.") from e


def list_archive(path, password=None, limit=None, encoding="auto"):
    """([(이름, 크기)], 전체 항목 수, 전체 크기). 7z는 파일 이름까지 암호화된 경우 암호가 필요하다."""
    if VOLUME_RE.search(path):
        with joined_volumes(path) as p:
            return list_archive(p, password, limit, encoding)
    kind = mf.kind_of(path)
    if kind == "tar":
        return mf.list_tar(path, limit)
    if kind == "single":
        return mf.list_tar(path, limit) if mf.looks_like_tar(path) else mf.list_single(path)
    if kind == "ext":
        return mf.list_ext(path, limit)
    try:
        if kind == "zip":
            with pyzipper.AESZipFile(path) as zf:
                infos = zf.infolist()
                out = [(_fix_zip_name(i, encoding), i.file_size) for i in infos[:limit]]
                return out, len(infos), sum(i.file_size for i in infos)
        if kind == "7z":
            with _open_7z(path, password) as z:
                infos = z.list()
                out = [(i.filename + ("/" if i.is_directory else ""), i.uncompressed or 0)
                       for i in infos[:limit]]
                return out, len(infos), sum(i.uncompressed or 0 for i in infos)
    except (*BAD_ZIP, py7zr.exceptions.Bad7zFile, lzma.LZMAError) as e:
        if password:
            raise WrongPassword("암호가 틀렸거나 파일이 손상되었습니다.") from e
        raise ArchiveError("압축 파일이 손상되었거나 암호가 걸려 있습니다.") from e
    raise ArchiveError("지원하지 않는 형식입니다. (zip, 7z, tar, gz, bz2, xz, rar, cab, iso 등)")


def extract(path, dest, password=None, policy="rename", strip_root=False,
            skip_mac_junk=True, progress=None, cancel=None, workers=None,
            only=None, encoding="auto"):
    """
    압축 파일을 dest 폴더에 푼다.
    policy: 같은 이름 파일이 있을 때 'rename'(이름 바꿔 저장) | 'overwrite' | 'skip'
    strip_root: 전체가 폴더 하나 안에 들어 있으면 그 폴더 껍질을 벗겨서 푼다
    only: 이름 목록을 주면 그 파일(폴더는 'dir/' 형태)만 푼다
    encoding: zip 파일명 해석 'auto' | 'utf-8' | 'cp949' ('UTF-8 표시'가 없는 zip에만 적용)
    반환: {'dest': dest, 'files': 파일 수, 'size': 총 바이트}
    """
    if not os.path.isfile(path):
        raise ArchiveError("압축 파일을 찾을 수 없습니다.")
    if VOLUME_RE.search(path):
        with joined_volumes(path) as p:
            info = extract(p, dest, password, policy, strip_root, skip_mac_junk,
                           progress, cancel, workers, only, encoding)
        propagate_motw(path, info.get("targets", []))
        return info
    kind = mf.kind_of(path)
    if kind is None:
        raise ArchiveError("지원하지 않는 형식입니다. (zip, 7z, tar, gz, bz2, xz, rar, cab, iso 등)")
    os.makedirs(fs(dest), exist_ok=True)
    workers = workers or min(4, os.cpu_count() or 1)
    if only:
        strip_root = False
    try:
        if kind == "tar" or (kind == "single" and mf.looks_like_tar(path)):
            info = mf.extract_tar(path, dest, policy, strip_root, skip_mac_junk, progress, cancel, only)
        elif kind == "single":
            info = mf.extract_single(path, dest, policy, skip_mac_junk, progress, cancel, only)
        elif kind == "ext":
            info = mf.extract_ext(path, dest, policy, strip_root, skip_mac_junk, progress, cancel, only)
        else:
            fn = _extract_zip if kind == "zip" else _extract_7z
            info = fn(path, dest, password, policy, strip_root, skip_mac_junk,
                      progress, cancel, workers, only, encoding)
        propagate_motw(path, info.get("targets", []))
        return info
    except BAD_ZIP as e:
        raise ArchiveError("압축 파일이 손상되었거나 zip 형식이 아닙니다.") from e
    except OSError as e:
        fe = friendly_oserror(e)
        if fe:
            raise fe from e
        raise


def propagate_motw(archive, targets, force=False):
    """
    인터넷에서 받은 압축 파일에는 윈도우가 '출처 표시(Zone.Identifier)'를 붙여 둔다.
    푼 파일에도 같은 표시를 옮겨 줘야 스마트스크린/오피스 보호 보기가 계속 동작한다.
    (표시가 없으면 아무 일도 하지 않는다. 윈도우(NTFS)에서만 동작, force는 테스트용)
    반환: 표시를 옮긴 파일 수
    """
    if os.name != "nt" and not force:
        return 0
    try:
        with open(fs(archive) + ":Zone.Identifier", "rb") as f:
            data = f.read(4096)
    except OSError:
        return 0
    if b"ZoneId" not in data:
        return 0
    n = 0
    for t in targets:
        try:
            with open(fs(t) + ":Zone.Identifier", "wb") as f:
                f.write(data)
            n += 1
        except OSError:
            pass
    return n


def _only_filter(only):
    names = {normalize_name(o).rstrip("/") for o in only}
    folders = [normalize_name(o) for o in only if o.endswith("/")]

    def want(n, is_dir):
        k = n.rstrip("/")
        return k in names or any(n.startswith(f) for f in folders)
    return want


def _blocked(target, dest):
    """target의 상위 경로 중에 같은 이름의 '파일'이 이미 있어서 폴더를 만들 수 없는지"""
    d = os.path.dirname(target)
    dest_abs = os.path.abspath(dest)
    while d and len(d) > len(dest_abs) and d.startswith(dest_abs):
        if os.path.isfile(fs(d)):
            return True
        d = os.path.dirname(d)
    return False


def _plan_entries(entries, dest, policy, strip_root, skip_mac_junk, only=None, skipped=None):
    """
    entries: [(원본 항목 객체, 이름, 폴더 여부, 크기)]
    반환: (dirs, files)  files = [(항목 객체, 대상 경로, 크기, 이름)]
    """
    kept = [e for e in entries if not (skip_mac_junk and is_mac_junk(e[1]))]
    if only:
        want = _only_filter(only)
        kept = [e for e in kept if want(normalize_name(e[1]), e[2])]
    root = detect_root([e[1] for e in kept]) if strip_root else None
    dirs, files, claimed = [], [], set()
    for obj, name, is_dir, size in kept:
        n = normalize_name(name)
        if root:
            if n.strip("/") == root:
                continue
            n = n[len(root) + 1:] if n.startswith(root + "/") else n
        if not n.strip("/"):
            continue
        try:
            target = safe_join(dest, n)
        except ValueError:
            if skipped is not None:
                skipped.append((n, "위험한 경로(안전을 위해 건너뜀)"))
            continue
        if _blocked(target, dest):
            if skipped is not None:
                skipped.append((n, "같은 이름의 파일이 이미 있어 폴더를 만들 수 없음"))
            continue
        if is_dir:
            if os.path.isfile(fs(target)):
                if skipped is not None:
                    skipped.append((n, "같은 이름의 파일이 이미 있음"))
                continue
            dirs.append(target)
            continue
        if os.path.normcase(target) in claimed:   # 같은 이름(윈도우는 대소문자 무시)이 두 번 들어 있는 경우
            target = _free_name(target, claimed)
        elif os.path.exists(fs(target)):
            if policy == "skip":
                continue
            if policy == "rename":
                target = _free_name(target, claimed)
        claimed.add(os.path.normcase(target))
        files.append((obj, target, size, n))
    return dirs, files


def _free_name(path, claimed):
    base, ext = os.path.splitext(path)
    i = 2
    while True:
        cand = f"{base} ({i}){ext}"
        if os.path.normcase(cand) not in claimed and not os.path.exists(fs(cand)):
            return cand
        i += 1


def _set_mtime(target, date_time):
    try:
        ts = time.mktime(tuple(date_time) + (0, 0, -1))
        os.utime(fs(target), (ts, ts))
    except (OverflowError, ValueError, OSError):
        pass


def _extract_zip(path, dest, password, policy, strip_root, skip_mac_junk,
                 progress, cancel, workers, only=None, encoding="auto"):
    pwd = password.encode("utf-8") if password else None
    with pyzipper.AESZipFile(path) as zf:
        entries = [(i, _fix_zip_name(i, encoding), i.is_dir(), i.file_size) for i in zf.infolist()]
    skipped = []
    dirs, files = _plan_entries(entries, dest, policy, strip_root, skip_mac_junk, only, skipped)
    total = sum(f[2] for f in files)
    _check_space(dest, total)
    for d in dirs:
        os.makedirs(fs(d), exist_ok=True)
    prog = _Progress(progress, total)
    stop = threading.Event()

    def cancelled():
        return stop.is_set() or (cancel is not None and cancel.is_set())

    def one(zf, info, target, name):
        os.makedirs(fs(os.path.dirname(target)), exist_ok=True)
        try:
            with zf.open(info, "r", pwd=pwd) as src, open(fs(target), "wb") as dst:
                while True:
                    buf = src.read(1024 * 1024)
                    if not buf:
                        break
                    if cancelled():
                        raise Cancelled()
                    dst.write(buf)
                    prog.add(len(buf), name)
        except BaseException:
            try:
                os.remove(fs(target))
            except OSError:
                pass
            raise
        _set_mtime(target, info.date_time)

    def convert(e):
        if isinstance(e, RuntimeError) and "password" in str(e).lower():
            return WrongPassword("암호가 없거나 틀렸습니다.")
        if isinstance(e, BAD_ZIP):
            return WrongPassword("암호가 틀렸거나 파일이 손상되었습니다.") if password \
                else ArchiveError("파일이 손상되었습니다.")
        if isinstance(e, NotImplementedError):
            return ArchiveError("지원하지 않는 압축 방식입니다.")
        return e

    files.sort(key=lambda f: -f[2])           # 큰 파일부터 나눠 맡기면 균형이 좋다
    parallel = workers > 1 and len(files) >= 4 and total >= 4 * 1024 * 1024
    if not parallel:
        try:
            with pyzipper.AESZipFile(path) as zf:
                for info, target, size, name in files:
                    if cancelled():
                        raise Cancelled()
                    one(zf, info, target, name)
        except Cancelled:
            raise
        except Exception as e:
            raise convert(e) from e
    else:
        lock, idx, errors = threading.Lock(), [0], []

        def run():
            try:
                with pyzipper.AESZipFile(path) as zf:     # 스레드마다 자기 핸들
                    while not cancelled():
                        with lock:
                            if idx[0] >= len(files):
                                return
                            info, target, size, name = files[idx[0]]
                            idx[0] += 1
                        one(zf, info, target, name)
            except BaseException as e:
                errors.append(e)
                stop.set()

        threads = [threading.Thread(target=run, daemon=True) for _ in range(workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        if errors:
            real = [e for e in errors if not isinstance(e, Cancelled)]
            if real:
                raise convert(real[0]) from real[0]
            raise Cancelled()
        if cancel is not None and cancel.is_set():
            raise Cancelled()
    if progress:
        progress(total or 1, total or 1, "")
    return {"dest": dest, "files": len(files), "size": total, "skipped": skipped,
            "targets": [f[1] for f in files]}


class _Writer7z(py7zr.io.Py7zIO):
    """py7zr가 푼 데이터를 받아 최종 위치에 바로 쓴다. target이 None이면 버린다(건너뛴 항목)."""

    def __init__(self, target, name, mtime, prog, cancel, registry):
        self.target, self.name, self.mtime = target, name, mtime
        self.prog, self.cancel, self.n, self.done = prog, cancel, 0, False
        self.f = None
        if target is not None:
            os.makedirs(fs(os.path.dirname(target)), exist_ok=True)
            self.f = open(fs(target), "w+b")
        registry.append(self)

    def write(self, s):
        if self.cancel is not None and self.cancel.is_set():
            raise Cancelled()
        if self.f is not None:
            self.f.write(s)
        self.n += len(s)
        self.prog.add(len(s), self.name)
        return len(s)

    def read(self, size=None):
        if self.f is None:
            return b""
        return self.f.read() if size is None else self.f.read(size)

    def seek(self, offset, whence=0):
        return self.f.seek(offset, whence) if self.f is not None else 0

    def flush(self):
        if self.f is not None:
            self.f.flush()

    def size(self):
        return self.n

    def close(self):
        if self.done:
            return
        self.done = True
        if self.f is not None:
            self.f.close()
            if self.mtime:
                try:
                    os.utime(fs(self.target), (self.mtime, self.mtime))
                except (OSError, OverflowError, ValueError):
                    pass

    def discard(self):
        """실패/취소 시 쓰다 만 파일 정리"""
        if self.done:
            return
        self.done = True
        if self.f is not None:
            try:
                self.f.close()
                os.remove(fs(self.target))
            except OSError:
                pass


class _Factory7z(py7zr.io.WriterFactory):
    def __init__(self, targets, prog, cancel):
        self.targets, self.prog, self.cancel = targets, prog, cancel
        self.writers = []

    def create(self, filename):
        key = filename.replace("\\", "/")
        target, mtime = self.targets.get(key, (None, None))
        return _Writer7z(target, key, mtime, self.prog, self.cancel, self.writers)


def _extract_7z(path, dest, password, policy, strip_root, skip_mac_junk,
                progress, cancel, workers, only=None, encoding="auto"):
    with _open_7z(path, password) as z:
        infos = z.list()
        entries = []
        for i in infos:
            mt = None
            try:
                mt = i.creationtime.timestamp() if i.creationtime else None
            except (AttributeError, OSError, OverflowError, ValueError):
                pass
            entries.append(((i.filename, mt), i.filename, i.is_directory, i.uncompressed or 0))
        skipped = []
        dirs, files = _plan_entries(entries, dest, policy, strip_root, skip_mac_junk, only, skipped)
        total = sum(f[2] for f in files)
        _check_space(dest, total)
        for d in dirs:
            os.makedirs(fs(d), exist_ok=True)
        targets = {normalize_name(obj[0]): (target, obj[1]) for obj, target, size, name in files}
        prog = _Progress(progress, total)
        factory = _Factory7z(targets, prog, cancel)
        ok = False
        try:
            z.extractall(path=None, factory=factory)
            ok = True
        except Cancelled:
            raise
        except BaseException as e:
            if cancel is not None and cancel.is_set():
                raise Cancelled() from e
            _convert_7z_error(e, password)
            raise
        finally:
            for w in factory.writers:
                w.close() if ok else w.discard()
    if cancel is not None and cancel.is_set():
        raise Cancelled()
    if progress:
        progress(total or 1, total or 1, "")
    return {"dest": dest, "files": len(files), "size": total, "skipped": skipped,
            "targets": [f[1] for f in files]}


def _convert_7z_error(e, password):
    if isinstance(e, (WrongPassword, ArchiveError)):
        return
    if isinstance(e, py7zr.exceptions.PasswordRequired):
        raise WrongPassword("암호가 필요합니다.") from e
    wrong = (py7zr.exceptions.CrcError, py7zr.exceptions.Bad7zFile, lzma.LZMAError,
             getattr(py7zr.exceptions, "DecompressionError", ()))
    if isinstance(e, wrong):
        if password:
            raise WrongPassword("암호가 틀렸거나 파일이 손상되었습니다.") from e
        raise ArchiveError("파일이 손상되었거나 암호가 걸려 있습니다.") from e


# ───────────────────────── 분할 압축 / 무결성 검사 / 변환 ─────────────────────────

def split_archive(path, part_size, remove_original=True):
    """path를 part_size 바이트씩 잘라 path.001, path.002 ... 로 저장하고 경로 목록을 돌려준다."""
    if part_size < 64 * 1024:
        raise ArchiveError("분할 크기가 너무 작습니다. (최소 64KB)")
    parts, n = [], 0
    try:
        with open(path, "rb") as src:
            while True:
                chunk = src.read(min(part_size, 8 << 20))
                if not chunk:
                    break
                n += 1
                p = f"{path}.{n:03d}"
                parts.append(p)
                written = 0
                with open(p, "wb") as dst:
                    while chunk:
                        dst.write(chunk)
                        written += len(chunk)
                        if written >= part_size:
                            break
                        chunk = src.read(min(part_size - written, 8 << 20))
    except BaseException:
        for p in parts:
            try:
                os.remove(p)
            except OSError:
                pass
        raise
    if remove_original:
        os.remove(path)
    return parts


def volume_parts(first):
    """'a.zip.001'의 모든 조각 경로 (001, 002, ... 연속된 것까지)"""
    base = first[:-4]
    out, n = [], 1
    while os.path.isfile(f"{base}.{n:03d}"):
        out.append(f"{base}.{n:03d}")
        n += 1
    return out


class joined_volumes:
    """분할된 압축 파일(.001 ...)을 임시 폴더에서 하나로 이어 붙여 경로를 준다. with 문에서 쓴다."""

    def __init__(self, first):
        self.first = first
        self.tmp = None

    def __enter__(self):
        base = os.path.basename(self.first)[:-4]
        if not self.first.endswith(".001"):
            raise ArchiveError("분할 압축 파일은 .001 파일을 선택해 주세요.")
        parts = volume_parts(self.first)
        if not parts:
            raise ArchiveError("분할 압축 파일의 첫 조각(.001)을 찾을 수 없습니다.")
        need = sum(os.path.getsize(p) for p in parts)
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="ssamzip_")
        try:
            _check_space(self.tmp, need)
            out = os.path.join(self.tmp, base)
            with open(out, "wb") as dst:
                for p in parts:
                    with open(p, "rb") as src:
                        shutil.copyfileobj(src, dst, 8 << 20)
        except BaseException:
            shutil.rmtree(self.tmp, ignore_errors=True)
            raise
        return out

    def __exit__(self, *a):
        if self.tmp:
            shutil.rmtree(self.tmp, ignore_errors=True)
        return False


def test_archive(path, password=None, progress=None, cancel=None, encoding="auto"):
    """
    압축 파일이 온전한지 검사한다 (모든 파일을 실제로 풀어 보며 CRC 확인, 디스크에는 쓰지 않음).
    반환: {'files': 검사한 파일 수, 'bad': [문제 있는 이름...], 'size': 총 바이트}
    암호가 틀리면 WrongPassword, 파일 구조 자체가 깨졌으면 ArchiveError.
    """
    if VOLUME_RE.search(path):
        with joined_volumes(path) as p:
            return test_archive(p, password, progress, cancel, encoding)
    kind = mf.kind_of(path)
    if not os.path.isfile(path):
        raise ArchiveError("압축 파일을 찾을 수 없습니다.")
    if kind == "zip":
        return _test_zip(path, password, progress, cancel, encoding)
    if kind == "7z":
        return _test_7z(path, password, progress, cancel)
    if kind == "tar" or (kind == "single" and mf.looks_like_tar(path)):
        return mf.test_tar(path, progress, cancel)
    if kind == "single":
        return mf.test_single(path, progress, cancel)
    if kind == "ext":
        return mf.test_ext(path, cancel)
    raise ArchiveError("지원하지 않는 형식입니다.")


def _test_zip(path, password, progress, cancel, encoding):
    pwd = password.encode("utf-8") if password else None
    bad, files = [], 0
    try:
        zf = pyzipper.AESZipFile(path)
    except BAD_ZIP as e:
        raise ArchiveError("압축 파일의 구조가 손상되었습니다.") from e
    with zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        total = sum(i.file_size for i in infos)
        prog = _Progress(progress, total)
        for info in infos:
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            name = _fix_zip_name(info, encoding)
            try:
                with zf.open(info, "r", pwd=pwd) as src:
                    while True:
                        buf = src.read(1 << 20)
                        if not buf:
                            break
                        if cancel is not None and cancel.is_set():
                            raise Cancelled()
                        prog.add(len(buf), name)
            except Cancelled:
                raise
            except RuntimeError as e:
                if "password" in str(e).lower():
                    raise WrongPassword("암호가 없거나 틀렸습니다.") from e
                bad.append(name)
            except NotImplementedError:
                bad.append(name)
            except Exception as e:
                if isinstance(e, BAD_ZIP) and info.flag_bits & 0x1 and password:
                    raise WrongPassword("암호가 틀렸거나 파일이 손상되었습니다.") from e
                bad.append(name)
            files += 1
    if progress:
        progress(total or 1, total or 1, "")
    return {"files": files, "bad": bad, "size": total}


def _test_7z(path, password, progress, cancel):
    class _Null(py7zr.io.Py7zIO):
        def __init__(self, name, prog):
            self.name, self.prog, self.n = name, prog, 0

        def write(self, s):
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            self.n += len(s)
            self.prog.add(len(s), self.name)
            return len(s)

        def read(self, size=None):
            return b""

        def seek(self, offset, whence=0):
            return 0

        def flush(self):
            pass

        def size(self):
            return self.n

    class _NullFactory(py7zr.io.WriterFactory):
        def __init__(self, prog):
            self.prog = prog

        def create(self, filename):
            return _Null(filename, self.prog)

    with _open_7z(path, password) as z:
        infos = z.list()
        total = sum(i.uncompressed or 0 for i in infos if not i.is_directory)
        nfiles = sum(1 for i in infos if not i.is_directory)
        prog = _Progress(progress, total)
        try:
            z.extractall(path=None, factory=_NullFactory(prog))
        except Cancelled:
            raise
        except BaseException as e:
            if cancel is not None and cancel.is_set():
                raise Cancelled() from e
            _convert_7z_error(e, password)
            if isinstance(e, (py7zr.exceptions.CrcError,)):
                return {"files": nfiles, "bad": ["(일부 파일의 CRC 불일치)"], "size": total}
            raise ArchiveError("압축 파일이 손상되었습니다.") from e
    if progress:
        progress(total or 1, total or 1, "")
    return {"files": nfiles, "bad": [], "size": total}


def open_entry(path, name, dest_dir, password=None, encoding="auto", cancel=None):
    """압축 파일 안의 파일 하나만 dest_dir에 풀고 그 경로를 돌려준다 (더블클릭해서 바로 열기용)."""
    info = extract(path, dest_dir, password, policy="overwrite", only=[name],
                   cancel=cancel, encoding=encoding)
    if info["files"] != 1:
        raise ArchiveError("압축 파일에서 해당 파일을 찾을 수 없습니다.")
    n = normalize_name(name)
    return safe_join(dest_dir, n)


def convert_archive(src, dst, fmt, level=6, password_in=None, password_out=None,
                    progress=None, cancel=None, workers=None):
    """zip ↔ 7z 변환: 임시 폴더에 풀었다가 다시 압축한다. 진행률은 절반씩(풀기 0~50%, 압축 50~100%)."""
    import tempfile
    tmp = tempfile.mkdtemp(prefix="ssamzip_conv_")
    try:
        def p1(d, t, n):
            if progress:
                progress(d / max(t, 1) * 0.5, 1.0, n)

        def p2(d, t, n):
            if progress:
                progress(0.5 + d / max(t, 1) * 0.5, 1.0, n)
        extract(src, tmp, password_in, "overwrite", False, False, p1, cancel, workers)
        top = [os.path.join(tmp, n) for n in sorted(os.listdir(tmp))]
        if not top:
            raise ArchiveError("변환할 파일이 없습니다.")
        return compress(top, dst, fmt, password_out, level, p2, cancel,
                        exclude_junk=False, workers=workers)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ───────────────────────── 스마트 풀기 / 일괄 풀기 ─────────────────────────

def extract_smart(archive, base_dir, password=None, policy="rename",
                  skip_mac_junk=True, progress=None, cancel=None, workers=None,
                  only=None, encoding="auto"):
    """
    '압축파일이름' 폴더를 만들어 그 안에 푼다. 압축 파일 안에 폴더가 하나뿐이면
    그 껍질은 벗겨서 '이름/이름/..' 이중 폴더가 생기지 않게 한다.
    같은 이름 폴더가 이미 있으면 '이름 (2)'로 만든다. 실패하면 만든 폴더를 지운다.
    """
    dest = unique_dir(os.path.join(base_dir, sanitize_component(archive_stem(archive))))
    created = not os.path.exists(fs(dest))
    try:
        return extract(archive, dest, password, policy, True, skip_mac_junk,
                       progress, cancel, workers, only, encoding)
    except BaseException:
        if created:
            shutil.rmtree(fs(dest), ignore_errors=True)
        raise


def find_archives(folder, recursive=True):
    out = []
    if recursive:
        for root, dirs, files in os.walk(folder):
            dirs.sort()
            for f in sorted(files):
                if (f.lower().endswith(ARCHIVE_EXTS) or VOLUME_RE.search(f)) and not f.startswith("._"):
                    out.append(os.path.join(root, f))
    else:
        for f in sorted(os.listdir(folder)):
            p = os.path.join(folder, f)
            if os.path.isfile(p) and (f.lower().endswith(ARCHIVE_EXTS) or VOLUME_RE.search(f)) \
                    and not f.startswith("._"):
                out.append(p)
    return out


def batch_extract(archives, out_dir=None, password=None, policy="rename",
                  on_event=None, cancel=None, max_workers=None, passwords=None):
    """
    archives: 풀 압축 파일 경로 리스트
    out_dir: None이면 각 압축 파일 옆에, 지정하면 그 폴더 한 곳에 모아서 푼다
    passwords: 암호 후보 목록. 암호가 걸린 파일마다 후보를 차례로 시도한다 (password는 맨 앞 후보)
    on_event(index, kind, payload): kind = 'start' | 'progress'(0~1) | 'ok'(dest) | 'fail'(메시지) | 'skip'
    반환: [(경로, 성공여부, 메시지)]
    """
    max_workers = max_workers or min(4, (os.cpu_count() or 2))
    cands = []
    for pw in ([password] if password else []) + list(passwords or []):
        if pw and pw not in cands:
            cands.append(pw)
    results = [None] * len(archives)
    emit = on_event or (lambda *a: None)
    lock, nxt = threading.Lock(), [0]

    def work():
        while True:
            with lock:
                i = nxt[0]
                nxt[0] += 1
            if i >= len(archives):
                return
            arc = archives[i]
            if cancel is not None and cancel.is_set():
                results[i] = (arc, False, "취소됨")
                emit(i, "skip", "취소됨")
                continue
            emit(i, "start", None)
            base = out_dir or os.path.dirname(arc)
            try:
                info = _smart_locked(arc, base, cands, policy, i, emit, cancel, lock)
                results[i] = (arc, True, info["dest"])
                emit(i, "ok", info["dest"])
            except Cancelled:
                results[i] = (arc, False, "취소됨")
                emit(i, "skip", "취소됨")
            except WrongPassword as e:
                results[i] = (arc, False, "암호 필요" if not cands else
                              ("암호가 맞지 않습니다" if len(cands) > 1 else str(e)))
                emit(i, "fail", results[i][2])
            except Exception as e:
                results[i] = (arc, False, str(e) or e.__class__.__name__)
                emit(i, "fail", results[i][2])

    n = max(1, min(max_workers, len(archives)))
    threads = [threading.Thread(target=work, daemon=True) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def _smart_locked(arc, base, cands, policy, i, emit, cancel, lock):
    # 폴더 이름 확정은 잠금 안에서, 실제 해제는 잠금 밖에서 병렬로
    with lock:
        dest = unique_dir(os.path.join(base, sanitize_component(archive_stem(arc))))
        os.makedirs(fs(dest), exist_ok=True)

    def cb(done, total, name):
        emit(i, "progress", done / total if total else 1.0)

    # 암호가 안 걸린 파일은 후보를 쓸 필요가 없다
    tries = cands if (cands and archive_encrypted(arc)) else [None]
    try:
        for k, pw in enumerate(tries):
            try:
                return extract(arc, dest, pw, policy, True, True, cb, cancel, workers=1)
            except WrongPassword:
                if k == len(tries) - 1:
                    raise
                shutil.rmtree(fs(dest), ignore_errors=True)       # 쓰다 만 것을 지우고 다음 후보로
                os.makedirs(fs(dest), exist_ok=True)
    except BaseException:
        shutil.rmtree(fs(dest), ignore_errors=True)
        raise
