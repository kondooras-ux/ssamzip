"""
zipwriter.py - 쌤ZIP 전용 고성능 ZIP 작성기 (암호 없는 일반 zip용)

표준 zipfile보다 나은 점
- 병렬 압축: 파일을 블록(2MB)으로 나눠 여러 스레드가 동시에 deflate 한다.
  블록마다 앞 블록의 마지막 32KB를 '사전(zdict)'으로 넘겨서, 나누지 않았을 때와
  압축률이 거의 같다. (zlib은 GIL을 풀고 동작하므로 코어 수만큼 빨라진다)
- 초고압축(level 10): Zopfli. 같은 deflate 형식이라 어떤 프로그램으로 풀어도 되고,
  zlib -9보다 보통 3~8% 더 작다. 대신 압축할 때만 오래 걸린다.
- 압축 안 되는 파일 자동 판별: 앞/중간/뒤를 표본으로 시험해 이득이 거의 없으면
  저장(STORED)만 한다. 결과가 원본보다 커지면 저장으로 되돌린다.
- ZIP64 지원: 4GB 넘는 파일, 65535개 넘는 항목
"""
import os
import time
import zlib
import struct
from collections import deque
from concurrent.futures import ThreadPoolExecutor

try:                                   # 없어도 동작한다 (level 10은 zlib -9로 대체)
    import zopfli.zlib as _zopfli
except Exception:                      # pragma: no cover
    _zopfli = None

BLOCK = 2 * 1024 * 1024               # 병렬 압축 블록 크기
ZOPFLI_MAX = 24 * 1024 * 1024         # 이보다 큰 파일은 Zopfli 대신 zlib -9 병렬
ZOPFLI_ITER = 15
SAMPLE = 64 * 1024
Z64 = 0xFFFFFFFF                      # 이 값 이상이면 ZIP64 필드 사용 (테스트에서 낮춤)
LOCAL_Z64 = 0x7FFFFFFF                # 로컬 헤더에 ZIP64 칸을 미리 잡아 둘 파일 크기
LEVEL_ULTRA = 10

# 이미 압축된 형식: 크면 표본 검사 없이 바로 저장
SURE_STORED_EXTS = {
    ".mp3", ".aac", ".m4a", ".ogg", ".flac",
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".webm", ".m4v",
    ".zip", ".7z", ".rar", ".gz", ".bz2", ".xz", ".zst", ".tgz",
    ".jpg", ".jpeg", ".heic", ".avif", ".webp",
}


class Cancelled(Exception):
    pass


class SkipFile(Exception):
    """파일을 읽을 수 없어(사용 중, 권한 없음, 중간에 사라짐 등) 건너뛴다. 이미 쓴 부분은 되돌려 둔 상태."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def _fs(path):
    """윈도우 260자 제한 회피 (윈도우가 아니면 그대로)"""
    if os.name != "nt":
        return path
    path = os.path.abspath(path)
    if len(path) < 240 or path.startswith("\\\\?\\"):
        return path
    return ("\\\\?\\UNC\\" + path[2:]) if path.startswith("\\\\") else "\\\\?\\" + path


def _why(e):
    """건너뛴 이유를 사람이 읽을 말로"""
    if isinstance(e, PermissionError):
        return "다른 프로그램이 사용 중이거나 권한이 없음"
    if isinstance(e, FileNotFoundError):
        return "파일이 없어짐(깨진 바로가기 포함)"
    return e.strerror or e.__class__.__name__


class _ReadFail(Exception):
    """원본 파일을 읽다가 실패 (내부용)"""


def _rd(src, n):
    try:
        return src.read(n)
    except OSError as ex:
        raise _ReadFail() from ex


def zopfli_available():
    return _zopfli is not None


def default_workers():
    return max(1, min(os.cpu_count() or 2, 8))


# ───────────────────────── 압축 가능성 표본 검사 ─────────────────────────

def _incompressible(sample):
    return bool(sample) and len(zlib.compress(sample, 1)) >= len(sample) * 0.97


def looks_incompressible(path, size):
    """True면 압축해도 이득이 거의 없다고 판단 (표본 압축 결과가 97% 이상)"""
    if size < 512:
        return False
    if os.path.splitext(path)[1].lower() in SURE_STORED_EXTS and size > 256 * 1024:
        return True
    try:
        with open(_fs(path), "rb") as f:
            if size <= SAMPLE * 3:
                sample = f.read()
            else:
                parts = []
                for pos in (0, size // 2 - SAMPLE // 2, size - SAMPLE):
                    f.seek(pos)
                    parts.append(f.read(SAMPLE))
                sample = b"".join(parts)
    except OSError:
        return False
    return _incompressible(sample)


# ───────────────────────── 블록 압축 (작업 스레드에서 실행) ─────────────────────────

def _deflate_block(data, level, zdict, final):
    """raw deflate 한 블록. 마지막이 아니면 SYNC_FLUSH로 바이트 경계에서 끝낸다."""
    if zdict:
        c = zlib.compressobj(level, zlib.DEFLATED, -15, 8, zlib.Z_DEFAULT_STRATEGY, zdict)
    else:
        c = zlib.compressobj(level, zlib.DEFLATED, -15, 8)
    return c.compress(data) + c.flush(zlib.Z_FINISH if final else zlib.Z_SYNC_FLUSH)


def _zopfli_whole(data, iterations):
    return _zopfli.compress(data, numiterations=iterations)[2:-4]   # zlib 머리/꼬리 제거 → raw deflate


# ───────────────────────── ZIP 구조 ─────────────────────────

def _dos_time(mtime):
    try:
        t = time.localtime(mtime)
    except (OverflowError, OSError, ValueError):
        return 0, (1 << 5) | 1
    if t.tm_year < 1980:
        return 0, (1 << 5) | 1
    if t.tm_year > 2107:                       # ZIP 날짜는 2107년까지만 표현 가능
        return (23 << 11) | (59 << 5) | 29, (127 << 9) | (12 << 5) | 31
    return ((t.tm_hour << 11) | (t.tm_min << 5) | (t.tm_sec // 2),
            ((t.tm_year - 1980) << 9) | (t.tm_mon << 5) | t.tm_mday)


def _local_header(e, method, crc, csize, usize):
    if e.z64:
        extra = struct.pack("<HHQQ", 1, 16, usize, csize)
        cs = us = 0xFFFFFFFF
        need = 45
    else:
        extra, cs, us, need = b"", csize, usize, 20
    return struct.pack("<IHHHHHIIIHH", 0x04034B50, need, e.flag, method, e.dtime, e.ddate,
                       crc, cs, us, len(e.name_b), len(extra)) + e.name_b + extra


class _Entry:
    __slots__ = ("name_b", "flag", "method", "dtime", "ddate", "crc", "csize", "usize",
                 "offset", "attr", "z64", "src", "hlen")


class ZipWriter:
    def __init__(self, path, workers=None, level=6, cancel=None, progress=None):
        self.f = open(path, "w+b")
        self.level = level
        self.workers = workers or default_workers()
        self.cancel = cancel
        self.progress = progress           # progress(바이트수, 이름)
        self.entries = []
        self.pool = ThreadPoolExecutor(self.workers)
        self.pending = deque()             # (entry, future, first, last, rawlen, name)
        self.max_pending = self.workers * (2 if level == LEVEL_ULTRA else 3)
        self._closed = False

    # ── 항목 만들기 ──
    def _new_entry(self, arc, mtime, mode, size):
        e = _Entry()
        e.name_b = arc.encode("utf-8")
        e.flag = 0x800 if any(ord(c) > 127 for c in arc) else 0
        e.dtime, e.ddate = _dos_time(time.time() if mtime is None else mtime)
        e.attr = (mode & 0xFFFF) << 16
        e.z64 = size >= LOCAL_Z64
        e.crc = e.csize = e.usize = 0
        e.method = 0
        e.src = None
        e.offset = 0
        e.hlen = 0
        return e

    def add_dir(self, arc, mtime=None, mode=0o40755):
        self._flush_all()
        e = self._new_entry(arc if arc.endswith("/") else arc + "/", mtime, mode, 0)
        e.attr |= 0x10
        self._write_whole_stored(e, b"")

    def add_bytes(self, arc, data, mtime=None, mode=0o100644):
        """메모리의 데이터를 넣는다 (작은 항목/테스트용)"""
        self._flush_all()
        e = self._new_entry(arc, mtime, mode, len(data))
        if self.level == 0 or len(data) < 64 or _incompressible(data[:SAMPLE * 3]):
            self._write_whole_stored(e, data)
            return
        e.crc, e.usize = zlib.crc32(data), len(data)
        zl = 9 if self.level == LEVEL_ULTRA else self.level
        if self.level == LEVEL_ULTRA and _zopfli is not None:
            fut = self.pool.submit(_zopfli_whole, data, ZOPFLI_ITER)
        else:
            fut = self.pool.submit(_deflate_block, data, zl, b"", True)
        e.method = 8
        e.src = data
        self._enqueue(e, fut, True, True, len(data), arc)

    def add_file(self, full, arc, mtime=None, mode=None):
        if self.cancel is not None and self.cancel.is_set():
            raise Cancelled()
        try:
            st = os.stat(_fs(full))
        except OSError as e:
            raise SkipFile(_why(e)) from e
        size = st.st_size
        e = self._new_entry(arc, st.st_mtime if mtime is None else mtime,
                            st.st_mode if mode is None else mode, size)
        if self.level == 0 or size == 0 or looks_incompressible(full, size):
            self._flush_all()
            self._write_file_stored(e, full)
            return
        e.method = 8
        e.src = full
        ultra = self.level == LEVEL_ULTRA and _zopfli is not None and size <= ZOPFLI_MAX
        zl = 9 if self.level == LEVEL_ULTRA else self.level
        try:
            src = open(_fs(full), "rb")
        except OSError as ex:
            raise SkipFile(_why(ex)) from ex
        try:
            self._add_stream(e, src, arc, ultra, zl)
        except _ReadFail as rf:                      # 읽다가 실패: 이 파일에 쓴 것을 되돌리고 건너뜀
            self._rollback(e)
            raise SkipFile(_why(rf.__cause__)) from rf.__cause__
        finally:
            src.close()

    def _add_stream(self, e, src, arc, ultra, zl):
        """읽기 오류만 _ReadFail로 바꾼다. 쓰기 오류(디스크 부족 등)는 그대로 올려 보낸다."""
        crc = total = 0
        first = True
        if ultra:
            data = _rd(src, -1)
            e.crc, e.usize = zlib.crc32(data), len(data)
            self._enqueue(e, self.pool.submit(_zopfli_whole, data, ZOPFLI_ITER),
                          True, True, len(data), arc)
            return
        prev_tail = b""
        buf = _rd(src, BLOCK)
        if not buf:                                  # 읽는 사이 비어 버린 파일
            self._flush_all()
            self._write_whole_stored(e, b"")
            return
        while buf:
            if self.cancel is not None and self.cancel.is_set():
                raise Cancelled()
            nxt = _rd(src, BLOCK)
            final = not nxt
            crc = zlib.crc32(buf, crc)
            total += len(buf)
            if final:
                e.crc, e.usize = crc, total
            fut = self.pool.submit(_deflate_block, buf, zl, prev_tail, final)
            self._enqueue(e, fut, first, final, len(buf), arc)
            first = False
            prev_tail = buf[-32768:]
            buf = nxt

    def _rollback(self, e):
        """e에 속한 대기 블록을 버리고, 이미 헤더를 썼다면 파일을 e 시작 위치로 되돌린다."""
        while self.pending and self.pending[-1][0] is e:
            self.pending.pop()[1].cancel()
        if e.hlen:
            self.f.seek(e.offset)
            self.f.truncate()
            e.hlen = 0

    # ── 대기열 / 기록 ──
    def _enqueue(self, e, fut, first, last, rawlen, name):
        self.pending.append((e, fut, first, last, rawlen, name))
        while len(self.pending) > self.max_pending:
            self._drain_one()

    def _flush_all(self):
        while self.pending:
            self._drain_one()

    def _drain_one(self):
        e, fut, first, last, rawlen, name = self.pending.popleft()
        if self.cancel is not None and self.cancel.is_set():
            raise Cancelled()
        out = fut.result()
        f = self.f
        if first:
            e.offset = f.tell()
            hdr = _local_header(e, 8, 0, 0, 0)
            e.hlen = len(hdr)
            f.write(hdr)
        f.write(out)
        e.csize += len(out)
        if self.progress:
            self.progress(rawlen, name)
        if last:
            self._finish(e)

    def _finish(self, e):
        f = self.f
        data_start = e.offset + e.hlen
        if e.csize >= e.usize:                       # 압축했더니 더 커짐 → 저장으로 되돌림
            f.seek(data_start)
            f.truncate()
            self._copy_raw(e)
            e.method, e.csize = 0, e.usize
            f.seek(e.offset)
            f.write(_local_header(e, 0, e.crc, e.csize, e.usize))
            f.seek(0, os.SEEK_END)
        else:
            end = f.tell()
            f.seek(e.offset)
            f.write(_local_header(e, 8, e.crc, e.csize, e.usize))
            f.seek(end)
        e.src = None
        self.entries.append(e)

    def _copy_raw(self, e):
        if isinstance(e.src, (bytes, bytearray)):
            self.f.write(e.src)
            return
        with open(_fs(e.src), "rb") as s:
            n = 0
            while n < e.usize:
                b = s.read(min(4 << 20, e.usize - n))
                if not b:
                    break
                self.f.write(b)
                n += len(b)

    def _write_whole_stored(self, e, data):
        e.method, e.crc, e.csize, e.usize = 0, zlib.crc32(data), len(data), len(data)
        e.offset = self.f.tell()
        hdr = _local_header(e, 0, e.crc, e.csize, e.usize)
        e.hlen = len(hdr)
        self.f.write(hdr)
        self.f.write(data)
        self.entries.append(e)

    def _write_file_stored(self, e, full):
        try:
            s = open(_fs(full), "rb")                      # 헤더를 쓰기 전에 먼저 열어 본다
        except OSError as ex:
            raise SkipFile(_why(ex)) from ex
        e.method = 0
        e.offset = self.f.tell()
        hdr = _local_header(e, 0, 0, 0, 0)
        e.hlen = len(hdr)
        self.f.write(hdr)
        crc = n = 0
        try:
            with s:
                while True:
                    if self.cancel is not None and self.cancel.is_set():
                        raise Cancelled()
                    b = _rd(s, 4 << 20)
                    if not b:
                        break
                    self.f.write(b)
                    crc = zlib.crc32(b, crc)
                    n += len(b)
                    if self.progress:
                        self.progress(len(b), os.path.basename(full))
        except _ReadFail as rf:
            self._rollback(e)
            raise SkipFile(_why(rf.__cause__)) from rf.__cause__
        e.crc, e.csize, e.usize = crc, n, n
        end = self.f.tell()
        self.f.seek(e.offset)
        self.f.write(_local_header(e, 0, crc, n, n))
        self.f.seek(end)
        self.entries.append(e)

    # ── 마무리 ──
    def _write_central(self):
        f = self.f
        cd_off = f.tell()
        buf = bytearray()
        for e in self.entries:
            big = [e.usize >= Z64, e.csize >= Z64, e.offset >= Z64]
            extra = b""
            if any(big):
                vals = [v for v, b in zip((e.usize, e.csize, e.offset), big) if b]
                extra = struct.pack("<HH" + "Q" * len(vals), 1, 8 * len(vals), *vals)
            need = 45 if (e.z64 or any(big)) else 20
            buf += struct.pack(
                "<IHHHHHHIIIHHHHHII", 0x02014B50, (3 << 8) | need, need, e.flag, e.method,
                e.dtime, e.ddate, e.crc,
                0xFFFFFFFF if big[1] else e.csize, 0xFFFFFFFF if big[0] else e.usize,
                len(e.name_b), len(extra), 0, 0, 0, e.attr,
                0xFFFFFFFF if big[2] else e.offset) + e.name_b + extra
        f.write(buf)
        cd_size = len(buf)
        n = len(self.entries)
        if n > 0xFFFF or cd_off >= Z64 or cd_size >= Z64:
            z64_off = f.tell()
            f.write(struct.pack("<IQHHIIQQQQ", 0x06064B50, 44, (3 << 8) | 45, 45, 0, 0,
                                n, n, cd_size, cd_off))
            f.write(struct.pack("<IIQI", 0x07064B50, 0, z64_off, 1))
            f.write(struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, min(n, 0xFFFF), min(n, 0xFFFF),
                                min(cd_size, 0xFFFFFFFF), min(cd_off, 0xFFFFFFFF), 0))
        else:
            f.write(struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, n, n, cd_size, cd_off, 0))

    def close(self):
        if self._closed:
            return
        self._flush_all()
        self._write_central()
        self.pool.shutdown(wait=True)
        self.f.close()
        self._closed = True

    def abort(self):
        self.pool.shutdown(wait=False, cancel_futures=True)
        try:
            self.f.close()
        except OSError:
            pass
        self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, et, ev, tb):
        if et is None:
            self.close()
        else:
            self.abort()
        return False
