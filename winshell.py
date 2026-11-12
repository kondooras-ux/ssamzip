"""
winshell.py - 윈도우 탐색기 연동 (관리자 권한 불필요, 현재 사용자 전용)

1) 우클릭 메뉴
   - 파일/폴더:  쌤ZIP으로 압축 / 쌤ZIP으로 압축 (암호·옵션)
   - zip/7z:     쌤ZIP: 여기에 풀기 / 쌤ZIP: 폴더 지정해서 풀기
   - 폴더:       쌤ZIP: 폴더 안 압축파일 전부 풀기 (폴더 빈 곳 우클릭에서도 가능)
2) .zip / .7z '연결 프로그램' 등록 (더블클릭으로 열기)
   윈도우 10/11은 보안 정책상 프로그램이 기본 앱을 직접 바꾸지 못한다.
   그래서 '연결 프로그램 목록'에 등록하고, 사용자가 한 번만 선택하도록 설정 창을 열어준다.

레지스트리를 쓰는 부분(apply/remove)과 '무엇을 쓸지 계획'(build_plan)을 분리해 두어
윈도우가 아닌 곳에서도 계획만 따로 검증할 수 있다.
"""
import os
import sys

IS_WIN = sys.platform == "win32"
PROG_ID = "SsamZip.Archive"
VERB_PREFIX = "SsamZip."
ARCHIVE_EXTS = (".zip", ".7z", ".tar", ".tgz", ".gz", ".tbz2", ".bz2", ".txz", ".xz",
                ".rar", ".cab", ".lzh", ".zst")
CLASSES = r"Software\Classes"

# (키 경로(HKCU\Software\Classes 기준), 값 이름 또는 None=기본값, 값, 종류)
REG_SZ, REG_NONE = "REG_SZ", "REG_NONE"


def launcher(script_path=None):
    """프로그램을 실행하는 명령어(따옴표 포함). exe로 묶였으면 exe, 아니면 pythonw + 스크립트."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    exe = sys.executable
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    if IS_WIN and os.path.exists(pyw):
        exe = pyw
    script = os.path.abspath(script_path or sys.argv[0])
    return f'"{exe}" "{script}"'


def icon_path():
    return sys.executable if getattr(sys, "frozen", False) else None


def build_plan(cmd, icon=None):
    plan = []

    def verb(base, key, label, flag, arg="%1"):
        k = f"{base}\\shell\\{VERB_PREFIX}{key}"
        plan.append((k, None, label, REG_SZ))
        if icon:
            plan.append((k, "Icon", f'"{icon}"', REG_SZ))
        plan.append((k + "\\command", None, f'{cmd} {flag} "{arg}"', REG_SZ))

    for base in ("*", "Directory"):
        verb(base, "Compress", "쌤ZIP으로 압축", "--compress")
        verb(base, "CompressOpts", "쌤ZIP으로 압축 (암호·옵션)", "--compress-opts")
    for ext in ARCHIVE_EXTS:
        base = f"SystemFileAssociations\\{ext}"
        verb(base, "ExtractHere", "쌤ZIP: 여기에 풀기", "--extract-here")
        verb(base, "ExtractTo", "쌤ZIP: 폴더 지정해서 풀기", "--open")
    verb("Directory", "Batch", "쌤ZIP: 폴더 안 압축파일 전부 풀기", "--batch")
    verb("Directory\\Background", "Batch", "쌤ZIP: 폴더 안 압축파일 전부 풀기", "--batch", "%V")
    return plan


def build_assoc_plan(cmd, icon=None):
    plan = [
        (PROG_ID, None, "쌤ZIP 압축 파일", REG_SZ),
        (PROG_ID + "\\shell", None, "open", REG_SZ),
        (PROG_ID + "\\shell\\open", None, "쌤ZIP으로 열기", REG_SZ),
        (PROG_ID + "\\shell\\open\\command", None, f'{cmd} --open "%1"', REG_SZ),
    ]
    if icon:
        plan.append((PROG_ID + "\\DefaultIcon", None, f'"{icon}",0', REG_SZ))
    for ext in ARCHIVE_EXTS:
        plan.append((f"{ext}\\OpenWithProgids", PROG_ID, "", REG_NONE))
    return plan


def _top_keys(plan):
    """지울 때 쓸 최상위 키 목록"""
    keys = set()
    for k, name, _v, _t in plan:
        if "\\OpenWithProgids" in k:
            continue
        keys.add(k.split("\\command")[0] if "\\shell\\" + VERB_PREFIX in k else k.split("\\")[0])
    return sorted(keys)


def _wr():
    import winreg
    return winreg


def _write(plan, winreg=None):
    wr = winreg or _wr()
    kinds = {REG_SZ: wr.REG_SZ, REG_NONE: wr.REG_NONE}
    for key, name, value, kind in plan:
        with wr.CreateKeyEx(wr.HKEY_CURRENT_USER, f"{CLASSES}\\{key}", 0, wr.KEY_SET_VALUE) as h:
            if kind == REG_NONE:
                wr.SetValueEx(h, name or "", 0, kinds[kind], b"")
            else:
                wr.SetValueEx(h, name or "", 0, kinds[kind], value)


def _delete_tree(root, path, wr):
    try:
        with wr.OpenKey(root, path, 0, wr.KEY_ALL_ACCESS) as h:
            while True:
                try:
                    sub = wr.EnumKey(h, 0)
                except OSError:
                    break
                _delete_tree(root, f"{path}\\{sub}", wr)
        wr.DeleteKey(root, path)
    except FileNotFoundError:
        pass


def _notify():
    if not IS_WIN:
        return
    try:
        import ctypes
        ctypes.windll.shell32.SHChangeNotify(0x08000000, 0, None, None)   # 연결 정보 갱신
    except Exception:
        pass


def context_plan(script_path=None):
    return build_plan(launcher(script_path), icon_path())


def assoc_plan(script_path=None):
    return build_assoc_plan(launcher(script_path), icon_path())


def install_context_menu(script_path=None, winreg=None):
    _write(context_plan(script_path), winreg)
    _notify()


def uninstall_context_menu(script_path=None, winreg=None):
    wr = winreg or _wr()
    for key in _top_keys(context_plan(script_path)):
        _delete_tree(wr.HKEY_CURRENT_USER, f"{CLASSES}\\{key}", wr)
    _notify()


def install_association(script_path=None, winreg=None):
    _write(assoc_plan(script_path), winreg)
    _notify()


def uninstall_association(winreg=None):
    wr = winreg or _wr()
    _delete_tree(wr.HKEY_CURRENT_USER, f"{CLASSES}\\{PROG_ID}", wr)
    for ext in ARCHIVE_EXTS:
        try:
            with wr.OpenKey(wr.HKEY_CURRENT_USER, f"{CLASSES}\\{ext}\\OpenWithProgids", 0,
                            wr.KEY_SET_VALUE) as h:
                wr.DeleteValue(h, PROG_ID)
        except FileNotFoundError:
            pass
    _notify()


def _exists(path, winreg=None):
    wr = winreg or _wr()
    try:
        with wr.OpenKey(wr.HKEY_CURRENT_USER, f"{CLASSES}\\{path}"):
            return True
    except OSError:
        return False


def is_context_installed(winreg=None):
    if not IS_WIN and winreg is None:
        return False
    return _exists(f"*\\shell\\{VERB_PREFIX}Compress", winreg)


def is_association_installed(winreg=None):
    if not IS_WIN and winreg is None:
        return False
    return _exists(PROG_ID, winreg)


def open_default_apps_settings():
    """윈도우 '기본 앱' 설정 화면을 연다 (zip/7z 기본 프로그램을 쌤ZIP으로 고르는 곳)"""
    if IS_WIN:
        os.startfile("ms-settings:defaultapps")   # noqa
