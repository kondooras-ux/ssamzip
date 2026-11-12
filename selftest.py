"""설치/빌드 직후 점검: python selftest.py  — 모든 만들기/풀기 형식을 한 바퀴 돌려 보고 결과를 출력한다. 실패가 있으면 종료 코드 1."""
import os
import sys
import shutil
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core
import moreformats as mf


def main():
    tmp = tempfile.mkdtemp(prefix="ssamzip_selftest_")
    results = []
    try:
        src = os.path.join(tmp, "원본")
        os.makedirs(os.path.join(src, "하위 폴더"))
        os.makedirs(os.path.join(src, "빈 폴더"))
        data = {"가.txt": "안녕하세요 " * 400, os.path.join("하위 폴더", "b.bin"): os.urandom(4096)}
        for rel, d in data.items():
            with open(os.path.join(src, rel), "wb") as f:
                f.write(d if isinstance(d, bytes) else d.encode())

        def same(dst):
            for rel, d in data.items():
                p = os.path.join(dst, "원본", rel)
                if not os.path.isfile(p) or open(p, "rb").read() != (d if isinstance(d, bytes) else d.encode()):
                    return False
            return os.path.isdir(os.path.join(dst, "원본", "빈 폴더"))

        for fmt in ("zip", "7z", "tgz", "tbz2", "txz", "tar"):
            name = f"t_{fmt}"
            try:
                arc = os.path.join(tmp, name + mf.format_ext(fmt))
                core.compress([src], arc, fmt, level=6)
                dst = os.path.join(tmp, "x_" + name)
                core.extract(arc, dst)
                ok = same(dst) and not core.test_archive(arc)["bad"]
                results.append((fmt, ok, ""))
            except Exception as e:
                results.append((fmt, False, f"{type(e).__name__}: {e}"))
        try:
            arc = os.path.join(tmp, "pw.zip")
            core.compress([src], arc, "zip", password="가나다1234")
            dst = os.path.join(tmp, "x_pw")
            core.extract(arc, dst, password="가나다1234")
            results.append(("zip 암호(AES)", same(dst), ""))
        except Exception as e:
            results.append(("zip 암호(AES)", False, f"{type(e).__name__}: {e}"))
        for fmt in ("gz", "bz2", "xz"):
            try:
                one = os.path.join(tmp, "one.txt")
                with open(one, "wb") as f:
                    f.write(b"abc" * 1000)
                arc = os.path.join(tmp, "one" + mf.format_ext(fmt))
                core.compress([one], arc, fmt)
                dst = os.path.join(tmp, "x_" + fmt)
                core.extract(arc, dst)
                results.append((fmt, os.path.getsize(os.path.join(dst, os.listdir(dst)[0])) == 3000, ""))
            except Exception as e:
                results.append((fmt, False, f"{type(e).__name__}: {e}"))
        results.append(("RAR 등 풀기용 tar.exe", True, "있음" if mf.find_bsdtar() else "없음(RAR·CAB·ISO는 풀 수 없음)"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    bad = 0
    for name, ok, note in results:
        print(("OK   " if ok else "FAIL ") + name + (f"  {note}" if note else ""))
        bad += (not ok)
    print("자체 점검 " + ("통과" if not bad else f"실패 {bad}건"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
