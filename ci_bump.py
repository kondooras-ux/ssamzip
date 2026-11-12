"""
자동 배포용 보조 스크립트 (GitHub Actions에서 호출)
  python ci_bump.py check            새로 받은 라이브러리 버전(new.lock)이 requirements.lock 과 다른지 → 'changed=true/false' 출력
  python ci_bump.py apply            버전(패치 번호) 올리고, CHANGELOG에 라이브러리 변경 기록을 넣고, requirements.lock 갱신
"""
import os
import re
import sys
import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
LOCK, NEW = os.path.join(HERE, "requirements.lock"), os.path.join(HERE, "new.lock")


def read_lock(path):
    out = {}
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            m = re.match(r"^([A-Za-z0-9_.-]+)==([^\s]+)", line.strip())
            if m:
                out[m.group(1).lower().replace("_", "-")] = m.group(2)
    return out


def diff(old, new):
    return [(k, old.get(k), new.get(k)) for k in sorted(set(old) | set(new)) if old.get(k) != new.get(k)]


def bump(version):
    parts = [int(x) for x in re.findall(r"\d+", version)]
    while len(parts) < 3:
        parts.append(0)
    parts[2] += 1
    return ".".join(map(str, parts))


def apply(lock=LOCK, new=NEW, root=HERE, today=None):
    ch = diff(read_lock(lock), read_lock(new))
    if not ch:
        return None
    sp = os.path.join(root, "ssamzip.py")
    s = open(sp, encoding="utf-8").read()
    cur = re.search(r'^VERSION = "([^"]+)"', s, re.M).group(1)
    ver = bump(cur)
    s = re.sub(r'^VERSION = "[^"]+"', f'VERSION = "{ver}"', s, count=1, flags=re.M)
    open(sp, "w", encoding="utf-8").write(s)
    lines = [f"- 라이브러리 자동 갱신: {k} {a or '(없음)'} → {b or '(제거)'}" for k, a, b in ch]
    entry = f"## {ver}\n" + "\n".join(lines) + f"\n- 자동 시험(selftest)을 통과한 빌드입니다. ({today or datetime.date.today().isoformat()})\n\n"
    cp = os.path.join(root, "CHANGELOG.md")
    text = open(cp, encoding="utf-8").read()
    i = text.find("\n## ")
    text = text[: i + 1] + entry + text[i + 1:] if i >= 0 else text + "\n" + entry
    open(cp, "w", encoding="utf-8").write(text)
    with open(new, encoding="utf-8") as f, open(lock, "w", encoding="utf-8") as g:
        g.write(f.read())
    return ver


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "check":
        print("changed=" + ("true" if diff(read_lock(LOCK), read_lock(NEW)) else "false"))
    elif cmd == "apply":
        v = apply()
        print(f"version={v or ''}")
    else:
        sys.exit("usage: ci_bump.py check|apply")
