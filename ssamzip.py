"""
쌤ZIP 2.0 - 윈도우용 압축 프로그램 (zip / 7z)

실행 방법
  ssamzip.exe                       메인 창
  ssamzip.exe 파일.zip              풀기 탭에서 열기 (더블클릭 연결)
  ssamzip.exe --compress 경로...     창 없이 바로 압축 (우클릭 메뉴)
  ssamzip.exe --extract-here zip...  창 없이 바로 풀기 (우클릭 메뉴)
  ssamzip.exe --compress-opts 경로... 압축 탭에 담아서 열기 (암호·옵션 설정)
  ssamzip.exe --batch 폴더           일괄 풀기 탭에서 열기

탐색기에서 여러 파일을 선택하고 우클릭하면 윈도우는 파일마다 프로그램을 따로 실행한다.
그래서 먼저 실행된 한 개만 남기고 나머지는 경로만 넘겨주고 종료하며, 남은 하나가 모아서 처리한다.
"""
import os
import sys
import json
import time
import queue
import socket
import secrets
import threading
import subprocess
import traceback

import updater

APP_NAME = "쌤ZIP"
VERSION = "3.3"
PORT = int(os.environ.get("SSAMZIP_PORT", "48721"))
import moreformats as mf
ARCHIVE_EXTS = mf.ARCHIVE_EXTS
LEVELS = {"빠르게": 1, "보통": 6, "최대 압축": 9, "초고압축 (느림)": 10}
OTHER_PLACEHOLDER = "기타 형식…"
SPLIT_CHOICES = ("사용 안 함", "100 MB", "650 MB (CD)", "700 MB", "1 GB", "4 GB (FAT32 USB)")
ENC_CHOICES = {"자동": "auto", "UTF-8": "utf-8", "한글(CP949)": "cp949"}


def is_archive(p):
    p = p.lower()
    return p.endswith(ARCHIVE_EXTS) or mf.kind_of(p) == "ext" or bool(mf.VOLUME_RE.search(p))


def parse_split(text):
    """'700 MB', '1 GB', '250' 같은 입력 → 바이트. 사용 안 함이면 None. 잘못되면 ValueError."""
    t = (text or "").strip().lower()
    if not t or t.startswith("사용"):
        return None
    import re as _re
    m = _re.match(r"^([0-9]+(?:\.[0-9]+)?)\s*(kb|mb|gb)?", t)
    if not m:
        raise ValueError
    n = float(m.group(1)) * {"kb": 1 << 10, "mb": 1 << 20, "gb": 1 << 30, None: 1 << 20}[m.group(2)]
    n = int(n)
    if n < 64 * 1024:
        raise ValueError
    return n
FLAGS = {"--compress": "compress", "--compress-opts": "compress-opts",
         "--extract-here": "extract-here", "--open": "open", "--batch": "batch"}
VALID_MODES = set(FLAGS.values()) | {"show"}


# ───────────────────────── 설정 / 경로 ─────────────────────────

def settings_dir():
    base = os.environ.get("SSAMZIP_HOME") or os.path.join(
        os.environ.get("APPDATA") or os.path.expanduser("~/.config"), "SsamZip")
    os.makedirs(base, exist_ok=True)
    return base


DEFAULTS = {
    "format": "zip", "level": 6, "open_folder": True, "exclude_junk": True,
    "skip_mac_junk": True, "policy": "rename", "theme": "light",
    "extract_mode": "beside", "batch_recursive": True, "batch_where": "beside",
    "update_url": "", "auto_update": True, "last_update_check": 0.0, "skip_version": "",
}


class Settings(dict):
    def __init__(self):
        super().__init__(DEFAULTS)
        self.path = os.path.join(settings_dir(), "settings.json")
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                # 값의 종류가 기본값과 같은 것만 받는다 (손으로 고치다 틀려도 앱이 죽지 않게)
                for k, v in data.items():
                    if k in DEFAULTS and isinstance(v, type(DEFAULTS[k])) and not (
                            isinstance(v, bool) != isinstance(DEFAULTS[k], bool)):
                        self[k] = v
        except (OSError, ValueError):
            pass
        if self["format"] not in mf.CREATE_FORMATS:
            self["format"] = "zip"
        if self["level"] not in LEVELS.values():
            self["level"] = 6
        if self["policy"] not in ("rename", "overwrite", "skip"):
            self["policy"] = "rename"
        if self["theme"] not in ("light", "dark"):
            self["theme"] = "light"
        if self["extract_mode"] not in ("beside", "custom"):
            self["extract_mode"] = "beside"
        if self["batch_where"] not in ("beside", "collect"):
            self["batch_where"] = "beside"

    def save(self):
        try:
            tmp = self.path + ".tmp"                  # 쓰다가 꺼져도 설정 파일이 깨지지 않게
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(dict(self), f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except OSError:
            pass


def skip_summary(skipped, limit=10):
    """건너뛴 항목을 사람이 읽을 목록으로"""
    lines = [f"• {n} : {why}" for n, why in skipped[:limit]]
    if len(skipped) > limit:
        lines.append(f"… 외 {len(skipped) - limit}개")
    return "\n".join(lines)


def log_error():
    try:
        with open(os.path.join(settings_dir(), "error.log"), "a", encoding="utf-8") as f:
            f.write(time.strftime("[%Y-%m-%d %H:%M:%S]\n") + traceback.format_exc() + "\n")
    except OSError:
        pass


def resource_path(name):
    """exe로 묶였을 때와 스크립트로 실행할 때 모두 리소스(아이콘 등)를 찾는다."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


def read_changelog():
    for name in ("CHANGELOG.md", "CHANGELOG.txt"):
        try:
            with open(resource_path(name), encoding="utf-8") as f:
                return f.read()
        except OSError:
            continue
    return "변경 이력 파일(CHANGELOG.md)을 찾을 수 없습니다."


def build_info():
    """빌드할 때 기록해 둔 정보(날짜, 라이브러리 버전)를 한 줄로. 없으면 빈 문자열."""
    try:
        with open(resource_path("buildinfo.json"), encoding="utf-8") as f:
            b = json.load(f)
        deps = ", ".join(f"{k} {v}" for k, v in b.get("deps", {}).items())
        return f"빌드 {b.get('date', '')} · Python {b.get('python', '')} · {deps}"
    except (OSError, ValueError):
        return ""


def reveal_open(path):
    """파일을 연결된 기본 프로그램으로 연다 (압축 파일 안의 파일 미리 열기용)."""
    try:
        if sys.platform == "win32":
            os.startfile(path)   # noqa
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except OSError:
        pass


def reveal(path):
    """탐색기에서 폴더를 열거나 파일을 선택해서 보여준다."""
    try:
        if sys.platform == "win32":
            if os.path.isdir(path):
                os.startfile(path)   # noqa
            else:
                subprocess.Popen(f'explorer /select,"{os.path.normpath(path)}"')
    except OSError:
        pass


# ───────────────────────── 단일 인스턴스 ─────────────────────────

def parse_args(argv):
    mode, paths = None, []
    for a in argv:
        if a in FLAGS and mode is None:
            mode = FLAGS[a]
            continue
        a = a.rstrip('"')                      # 'C:\"' 같은 따옴표 문제 보정
        if len(a) == 2 and a[1] == ":":
            a += "\\"
        paths.append(a)
    paths = [os.path.normpath(p) for p in paths if os.path.exists(p)]
    if mode is None and paths:                  # exe 위에 끌어다 놓았거나 연결 프로그램으로 열린 경우
        mode = "open" if all(is_archive(p) for p in paths) else "compress-opts"
    return mode, paths


def _token_path():
    return os.path.join(settings_dir(), "instance.token")


def acquire_server():
    """첫 번째 실행이면 (소켓, 토큰), 이미 실행 중이면 (None, None)"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", PORT))
    except OSError:
        s.close()
        return None, None
    token = secrets.token_hex(16)
    try:
        with open(_token_path(), "w") as f:
            f.write(token)
    except OSError:
        pass
    s.listen(16)
    return s, token


def send_request(mode, paths, tries=15):
    """실행 중인 쌤ZIP에 작업을 넘긴다. 성공하면 True."""
    if not os.path.exists(_token_path()):         # 다른 프로그램이 포트만 차지한 경우: 오래 기다리지 않는다
        tries = min(tries, 2)
    for _ in range(tries):
        try:
            with open(_token_path()) as f:
                token = f.read().strip()
            with socket.create_connection(("127.0.0.1", PORT), timeout=1.0) as c:
                c.sendall(json.dumps({"mode": mode, "paths": paths, "token": token}).encode("utf-8"))
                c.shutdown(socket.SHUT_WR)
                if c.recv(8) == b"OK":
                    return True
        except OSError:
            pass
        time.sleep(0.2)
    return False


def serve(srv, token, q):
    while True:
        try:
            conn, _ = srv.accept()
        except OSError:
            return
        with conn:
            try:
                conn.settimeout(3)
                data = b""
                while True:
                    chunk = conn.recv(65536)
                    if not chunk:
                        break
                    data += chunk
                req = json.loads(data.decode("utf-8"))
                if req.get("token") != token or req.get("mode") not in VALID_MODES:
                    continue
                paths = [p for p in req.get("paths", []) if isinstance(p, str) and os.path.exists(p)]
                conn.sendall(b"OK")
                q.put((req["mode"], paths))
            except Exception:
                pass


# ───────────────────────── 작업 실행기 ─────────────────────────

class Runner:
    """백그라운드 스레드에서 작업을 돌리고, 진행 상황은 '최신값'만 남겨 화면이 밀리지 않게 한다."""

    def __init__(self):
        self.events = queue.Queue()
        self.cancel = threading.Event()
        self.busy = False
        self.latest = None
        self.t0 = 0.0

    def progress(self, done, total, name=""):
        self.latest = (done, total, name)

    def start(self, fn):
        import core
        self.cancel.clear()
        self.latest = None
        self.busy = True
        self.t0 = time.monotonic()

        def run():
            try:
                self.events.put(("done", fn(self.progress, self.cancel)))
            except core.Cancelled:
                self.events.put(("cancel", None))
            except core.WrongPassword as e:
                self.events.put(("pw", e))
            except (core.ArchiveError, ValueError) as e:        # 안내용 오류: 로그에 쌓지 않는다
                self.events.put(("error", e))
            except OSError as e:
                log_error()
                self.events.put(("error", core.friendly_oserror(e) or e))
            except BaseException as e:      # 예상 못 한 오류도 화면에 알린다
                log_error()
                self.events.put(("error", e))

        threading.Thread(target=run, daemon=True).start()


# ───────────────────────── 화면 ─────────────────────────

def run_app(mode, paths, req_q=None, on_ready=None):
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox, simpledialog
    import tkinter.font as tkfont
    import core
    import winshell
    import zipwriter

    if sys.platform == "win32":                    # 글자/화면이 흐릿하지 않게
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass

    try:
        from tkinterdnd2 import DND_FILES, TkinterDnD
        root = TkinterDnD.Tk()
        dnd_ok = True
    except Exception:
        root = tk.Tk()
        dnd_ok = False
    if mode in ("compress", "extract-here"):
        root.withdraw()
    if sys.platform == "win32":
        try:
            root.iconbitmap(default=resource_path("ssamzip.ico"))      # 작업 표시줄/창 아이콘
        except Exception:
            pass
    try:
        import sv_ttk
        has_sv = True
    except Exception:
        has_sv = False

    class App:
        def __init__(self):
            self.root = root
            self.s = Settings()
            self.runner = Runner()
            self.ui_calls = queue.Queue()
            self.req_q = req_q if req_q is not None else queue.Queue()
            self.pending = {}
            self.deferred = []
            self.flush_scheduled = False
            self.main_visible = True
            self.on_done = None
            self.last_result = None
            self.qwin = None
            self.job_label = ""
            # 압축 탭 상태
            self.c_items, self.c_name_dirty, self.c_dir_dirty = [], False, False
            self.open_tmp = []            # 압축 파일 안에서 바로 연 파일의 임시 폴더들
            # 일괄 풀기 탭 상태
            self.b_rows, self.b_lock = [], threading.Lock()
            self.b_state, self.b_prog, self.b_dirty, self.b_active = {}, {}, set(), False

            def tk_error(exc, val, tb):
                log_error()
                try:
                    self.status.set(f"예상하지 못한 오류: {val}")
                    self.set_busy(False)
                except Exception:
                    pass
            root.report_callback_exception = tk_error
            root.title(f"{APP_NAME} {VERSION}")
            root.geometry("860x780")
            root.minsize(760, 600)
            self.setup_style()
            self.build_ui()
            root.protocol("WM_DELETE_WINDOW", self.on_close)
            root.after(80, self.tick)

        # ── 스타일 ──
        def setup_style(self):
            self.style = ttk.Style()
            base = tkfont.nametofont("TkDefaultFont")
            if not has_sv:
                if sys.platform == "win32":
                    for n in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
                        tkfont.nametofont(n).configure(family="Malgun Gothic", size=10)
                self.style.theme_use("vista" if "vista" in self.style.theme_names() else "clam")
            fam = base.actual("family")
            self.f_title = tkfont.Font(family=fam, size=17, weight="bold")
            self.f_small = tkfont.Font(family=fam, size=9)
            self.f_bold = tkfont.Font(family=fam, size=10, weight="bold")
            self.accent = "Accent.TButton" if has_sv else "TButton"
            self.switch = "Switch.TCheckbutton" if has_sv else "TCheckbutton"
            self.apply_theme()

        def apply_theme(self):
            dark = self.s["theme"] == "dark"
            if has_sv:
                sv_ttk.set_theme("dark" if dark else "light")
            self.ok_color = "#66bb6a" if dark else "#2e7d32"
            self.bad_color = "#ef5350" if dark else "#c62828"
            self.mute_color = "#9e9e9e" if dark else "#6b6b6b"
            if hasattr(self, "b_tree"):
                self.b_tree.tag_configure("ok", foreground=self.ok_color)
                self.b_tree.tag_configure("fail", foreground=self.bad_color)
                self.c_hint.configure(foreground=self.mute_color)

        # ── 전체 레이아웃 ──
        def build_ui(self):
            top = ttk.Frame(root, padding=(18, 14, 18, 4))
            top.pack(fill="x")
            ttk.Label(top, text=APP_NAME, font=self.f_title).pack(side="left")
            ttk.Label(top, text="  압축 · 해제 · 학생 제출 파일 일괄 풀기",
                      font=self.f_small).pack(side="left", pady=(8, 0))

            bottom = ttk.Frame(root, padding=(18, 8, 18, 14))
            bottom.pack(fill="x", side="bottom")        # 창이 작아져도 상태줄이 가려지지 않게 먼저 배치
            self.nb = ttk.Notebook(root)
            self.nb.pack(fill="both", expand=True, padx=14, pady=(4, 0))
            self.tab_c, self.tab_e = ttk.Frame(self.nb, padding=12), ttk.Frame(self.nb, padding=12)
            self.tab_b, self.tab_s = ttk.Frame(self.nb, padding=12), ttk.Frame(self.nb, padding=12)
            for t, name in ((self.tab_c, "  압축하기  "), (self.tab_e, "  풀기  "),
                            (self.tab_b, "  폴더 일괄 풀기  "), (self.tab_s, "  설정  ")):
                self.nb.add(t, text=name)
            self.action_buttons = []
            self.build_compress(self.tab_c)
            self.build_extract(self.tab_e)
            self.build_batch(self.tab_b)
            self.build_settings(self.tab_s)

            self.prog = ttk.Progressbar(bottom, maximum=1000)
            self.prog.pack(fill="x")
            row = ttk.Frame(bottom)
            row.pack(fill="x", pady=(6, 0))
            self.status = tk.StringVar(value="준비됨")
            self.detail = tk.StringVar(value="")
            ttk.Label(row, textvariable=self.status).pack(side="left")
            ttk.Label(row, textvariable=self.detail, font=self.f_small).pack(side="left", padx=12)
            self.cancel_btn = ttk.Button(row, text="취소", state="disabled",
                                         command=self.cancel_job)
            self.cancel_btn.pack(side="right")
            self.reveal_btn = ttk.Button(row, text="결과 폴더 열기", state="disabled",
                                         command=lambda: self.last_result and reveal(self.last_result))
            self.reveal_btn.pack(side="right", padx=6)
            self.apply_theme()
            root.update_idletasks()                       # 내용이 다 보이는 최소 높이로 맞춤(작은 화면이면 화면 높이까지)
            root.minsize(760, min(root.winfo_reqheight(), max(root.winfo_screenheight() - 70, 600)))

        def autowrap(self, label, margin=36):
            """창 폭에 맞춰 긴 안내 문구가 잘리지 않고 줄바꿈되게 한다."""
            def on_conf(e, l=label):
                l.configure(wraplength=max(240, e.width - margin))
            label.master.bind("<Configure>", on_conf, add="+")
            return label

        # ── 공용 위젯 도우미 ──
        def make_tree(self, parent, cols, height=7):
            frame = ttk.Frame(parent)
            tree = ttk.Treeview(frame, columns=[c[0] for c in cols], show="headings",
                                height=height, selectmode="extended")
            for key, text, width, anchor in cols:
                tree.heading(key, text=text)
                tree.column(key, width=width, anchor=anchor, stretch=(key == cols[0][0]))
            sb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
            tree.configure(yscrollcommand=sb.set)
            tree.pack(side="left", fill="both", expand=True)
            sb.pack(side="right", fill="y")
            return frame, tree

        def dnd(self, widget, handler):
            if dnd_ok:
                widget.drop_target_register(DND_FILES)
                widget.dnd_bind("<<Drop>>", lambda e: handler(list(root.tk.splitlist(e.data))))

        def path_row(self, parent, var, browse):
            f = ttk.Frame(parent)
            e = ttk.Entry(f, textvariable=var)
            e.pack(side="left", fill="x", expand=True)
            ttk.Button(f, text="찾기", width=6, command=browse).pack(side="left", padx=(6, 0))
            return f, e

        def pad_labels(self, frame):
            """옵션 카드의 왼쪽 라벨 열에 같은 여백을 줘서 입력칸과 붙지 않게 한다."""
            frame.columnconfigure(0, minsize=118)
            for w in frame.grid_slaves(column=0):
                w.grid_configure(padx=(0, 14))

        def toggle_show(self, var, *entries):
            for e in entries:
                e.configure(show="" if var.get() else "●")

        # ═════════════════ 압축 탭 ═════════════════
        def build_compress(self, tab):
            tab.columnconfigure(0, weight=1)
            tab.rowconfigure(1, weight=1)
            hint = "여기에 파일이나 폴더를 끌어다 놓으세요" if dnd_ok else "아래 버튼으로 파일이나 폴더를 추가하세요"
            self.zone = ttk.Label(tab, text=hint, anchor="center", relief="groove", padding=16)
            self.zone.grid(row=0, column=0, sticky="ew")
            fr, self.c_tree = self.make_tree(tab, (
                ("name", "이름", 300, "w"), ("kind", "종류", 60, "center"),
                ("size", "크기", 90, "e"), ("where", "위치", 260, "w")), height=4)
            fr.grid(row=1, column=0, sticky="nsew", pady=8)
            self.c_tree.bind("<Delete>", lambda e: self.remove_c())
            self.dnd(self.zone, self.add_c_paths)
            self.dnd(self.c_tree, self.add_c_paths)

            bar = ttk.Frame(tab)
            bar.grid(row=2, column=0, sticky="ew")
            ttk.Button(bar, text="파일 추가", command=self.pick_c_files).pack(side="left")
            ttk.Button(bar, text="폴더 추가", command=self.pick_c_folder).pack(side="left", padx=6)
            ttk.Button(bar, text="선택 삭제", command=self.remove_c).pack(side="left")
            ttk.Button(bar, text="모두 지우기", command=self.clear_c).pack(side="left", padx=6)
            self.c_summary = ttk.Label(bar, text="추가된 항목이 없습니다")
            self.c_summary.pack(side="right")

            opt = ttk.LabelFrame(tab, text=" 압축 옵션 ", padding=12)
            opt.grid(row=3, column=0, sticky="ew", pady=(10, 8))
            opt.columnconfigure(1, weight=1)
            self.c_fmt = tk.StringVar(value=self.s["format"])
            r = ttk.Frame(opt)
            r.grid(row=0, column=1, sticky="w")
            ttk.Radiobutton(r, text="ZIP", value="zip", variable=self.c_fmt,
                            command=self.on_fmt).pack(side="left")
            ttk.Radiobutton(r, text="7z", value="7z", variable=self.c_fmt,
                            command=self.on_fmt).pack(side="left", padx=12)
            self.other_fmts = {lbl: k for k, (e, lbl) in mf.CREATE_FORMATS.items() if k not in ("zip", "7z")}
            self.c_other = tk.StringVar(value=OTHER_PLACEHOLDER)
            oc = ttk.Combobox(r, textvariable=self.c_other, state="readonly", width=15,
                              values=(OTHER_PLACEHOLDER, *self.other_fmts))
            oc.pack(side="left")
            oc.bind("<<ComboboxSelected>>", self.on_other_fmt)
            self.c_hint = ttk.Label(r, text="", font=self.f_small)
            self.c_hint.pack(side="left", padx=8)
            ttk.Label(opt, text="형식").grid(row=0, column=0, sticky="w", pady=3, padx=(0, 14))

            ttk.Label(opt, text="압축 수준").grid(row=1, column=0, sticky="w", pady=3)
            self.c_level = tk.StringVar(value={v: k for k, v in LEVELS.items()}.get(self.s["level"], "보통"))
            lvf = ttk.Frame(opt)
            lvf.grid(row=1, column=1, sticky="w")
            lvc = ttk.Combobox(lvf, textvariable=self.c_level, state="readonly", width=16,
                               values=tuple(LEVELS))
            lvc.pack(side="left")
            lvc.bind("<<ComboboxSelected>>", lambda e: self.on_level())
            self.c_lv_hint = ttk.Label(lvf, text="", font=self.f_small)
            self.c_lv_hint.pack(side="left", padx=10)

            ttk.Label(opt, text="암호").grid(row=2, column=0, sticky="w", pady=3)
            pw = ttk.Frame(opt)
            pw.grid(row=2, column=1, sticky="w")
            self.c_pw, self.c_pw2 = tk.StringVar(), tk.StringVar()
            e1 = ttk.Entry(pw, textvariable=self.c_pw, show="●", width=18)
            e2 = ttk.Entry(pw, textvariable=self.c_pw2, show="●", width=18)
            self.c_pw_entries = (e1, e2)
            e1.pack(side="left")
            ttk.Label(pw, text=" 확인 ").pack(side="left")
            e2.pack(side="left")
            self.c_show = tk.BooleanVar()
            ttk.Checkbutton(pw, text="암호 보기", variable=self.c_show,
                            command=lambda: self.toggle_show(self.c_show, e1, e2)).pack(side="left", padx=10)
            ttk.Label(pw, text="(선택)", font=self.f_small).pack(side="left")

            ttk.Label(opt, text="저장 이름").grid(row=3, column=0, sticky="w", pady=3)
            nm = ttk.Frame(opt)
            nm.grid(row=3, column=1, sticky="ew")
            self.c_name = tk.StringVar()
            ne = ttk.Entry(nm, textvariable=self.c_name)
            ne.pack(side="left", fill="x", expand=True)
            ne.bind("<KeyRelease>", lambda e: setattr(self, "c_name_dirty", True))
            self.c_ext = ttk.Label(nm, text=".zip", width=8)
            self.c_ext.pack(side="left", padx=4)

            ttk.Label(opt, text="저장 폴더").grid(row=4, column=0, sticky="w", pady=3)
            self.c_outdir = tk.StringVar()
            pr, pe = self.path_row(opt, self.c_outdir, self.pick_c_outdir)
            pr.grid(row=4, column=1, sticky="ew")
            pe.bind("<KeyRelease>", lambda e: setattr(self, "c_dir_dirty", True))

            ttk.Label(opt, text="나눠 저장").grid(row=5, column=0, sticky="w", pady=3)
            spf = ttk.Frame(opt)
            spf.grid(row=5, column=1, sticky="w")
            self.c_split = tk.StringVar(value=SPLIT_CHOICES[0])
            ttk.Combobox(spf, textvariable=self.c_split, width=16, values=SPLIT_CHOICES).pack(side="left")
            ttk.Label(spf, text="(메일·USB용 · 직접 입력 예: 250 MB)",
                      font=self.f_small).pack(side="left", padx=10)

            sw = ttk.Frame(opt)
            sw.grid(row=6, column=1, sticky="w", pady=(6, 0))
            self.c_junk = tk.BooleanVar(value=self.s["exclude_junk"])
            self.c_open = tk.BooleanVar(value=self.s["open_folder"])
            ttk.Checkbutton(sw, text="임시 파일 제외 (Thumbs.db, ~$ 등)", variable=self.c_junk,
                            style=self.switch).pack(side="left")
            ttk.Checkbutton(sw, text="완료 후 폴더 열기", variable=self.c_open,
                            style=self.switch).pack(side="left", padx=18)

            self.pad_labels(opt)
            b = ttk.Button(tab, text="압축 시작", style=self.accent, command=self.do_compress)
            b.grid(row=4, column=0, sticky="e")
            self.action_buttons.append(b)
            self.on_fmt()

        def on_other_fmt(self, event=None):
            k = self.other_fmts.get(self.c_other.get())
            if k:
                self.c_fmt.set(k)
            self.on_fmt()

        def on_fmt(self):
            f = self.c_fmt.get()
            if f in ("zip", "7z"):
                self.c_other.set(OTHER_PLACEHOLDER)
            self.c_ext.configure(text=mf.format_ext(f))
            hints = {"zip": "빠르고 어디서나 열림", "7z": "용량이 더 작지만 느림 (7-Zip 등 필요)",
                     "tgz": "리눅스·맥 배포용 (암호 없음)", "tbz2": "gzip보다 작음 (암호 없음)",
                     "txz": "가장 작은 tar (암호 없음)", "tar": "압축 없이 묶기만",
                     "gz": "파일 하나만 압축", "bz2": "파일 하나만 압축", "xz": "파일 하나만 압축"}
            self.c_hint.configure(text=hints.get(f, ""))
            nopw = f in mf.NO_PASSWORD_FORMATS
            for e in self.c_pw_entries:
                e.configure(state="disabled" if nopw else "normal")
            if nopw:
                self.c_pw.set("")
                self.c_pw2.set("")
            self.on_level()

        def on_level(self):
            lv, f = LEVELS.get(self.c_level.get(), 6), self.c_fmt.get()
            if f not in ("zip", "7z"):
                self.c_lv_hint.configure(text="초고압축은 '최대 압축'과 거의 같음" if lv == 10 and f in ("tgz", "tbz2") else "")
            elif lv != 10:
                self.c_lv_hint.configure(text="여러 코어로 동시에 압축" if f == "zip" else "")
            elif f == "zip":
                self.c_lv_hint.configure(
                    text="Zopfli · 같은 ZIP인데 3~8% 더 작게, 수십 배 느림 (암호 ZIP 제외)"
                    if zipwriter.zopfli_available() else "zopfli 미설치 → '최대 압축'과 같음")
            else:
                self.c_lv_hint.configure(text="설정 두 가지를 모두 시험해 작은 쪽 선택 · 약 2배 느림")

        def pick_c_files(self):
            self.add_c_paths(filedialog.askopenfilenames(title="압축할 파일 선택"))

        def pick_c_folder(self):
            d = filedialog.askdirectory(title="압축할 폴더 선택")
            if d:
                self.add_c_paths([d])

        def pick_c_outdir(self):
            d = filedialog.askdirectory(title="저장할 폴더 선택")
            if d:
                self.c_outdir.set(os.path.normpath(d))
                self.c_dir_dirty = True

        def add_c_paths(self, paths):
            for p in paths:
                p = os.path.normpath(p)
                if not os.path.exists(p) or any(i["path"] == p for i in self.c_items):
                    continue
                item = {"path": p, "dir": os.path.isdir(p), "size": None, "files": None}
                self.c_items.append(item)
                if item["dir"]:
                    shown = "계산 중…"
                    threading.Thread(target=self._scan_item, args=(item,), daemon=True).start()
                else:
                    item["size"], item["files"] = os.path.getsize(p), 1
                    shown = core.fmt_size(item["size"])
                self.c_tree.insert("", "end", iid=p, values=(
                    os.path.basename(p) or p, "폴더" if item["dir"] else "파일", shown, os.path.dirname(p)))
            self.after_c_change()

        def _scan_item(self, item):
            files, size = core.scan_size([item["path"]], self.c_junk.get())

            def apply():
                item["files"], item["size"] = files, size
                if self.c_tree.exists(item["path"]):
                    self.c_tree.set(item["path"], "size", core.fmt_size(size))
                self.after_c_change(refresh_names=False)
            self.ui(apply)

        def remove_c(self):
            for iid in self.c_tree.selection():
                self.c_tree.delete(iid)
                self.c_items = [i for i in self.c_items if i["path"] != iid]
            self.after_c_change()

        def clear_c(self):
            self.c_tree.delete(*self.c_tree.get_children())
            self.c_items = []
            self.c_name_dirty = self.c_dir_dirty = False
            self.after_c_change()

        def after_c_change(self, refresh_names=True):
            n = len(self.c_items)
            if not n:
                self.c_summary.configure(text="추가된 항목이 없습니다")
            else:
                pend = any(i["size"] is None for i in self.c_items)
                files = sum(i["files"] or 0 for i in self.c_items)
                size = sum(i["size"] or 0 for i in self.c_items)
                self.c_summary.configure(
                    text=f"항목 {n}개 · 파일 {files:,}개 · {core.fmt_size(size)}" + (" (계산 중…)" if pend else ""))
            if refresh_names and n:
                if not self.c_name_dirty:
                    self.c_name.set(self.suggest_name())
                if not self.c_dir_dirty:
                    self.c_outdir.set(os.path.dirname(self.c_items[0]["path"]))

        def suggest_name(self):
            return quick_name([i["path"] for i in self.c_items])

        def do_compress(self):
            if not self.c_items:
                messagebox.showwarning(APP_NAME, "압축할 파일이나 폴더를 먼저 추가하세요.")
                return
            pw = self.c_pw.get()
            if pw and pw != self.c_pw2.get():
                messagebox.showwarning("암호 확인", "암호와 확인 칸이 서로 다릅니다.")
                return
            fmt = self.c_fmt.get()
            level = LEVELS[self.c_level.get()]
            try:
                split = parse_split(self.c_split.get())
            except ValueError:
                messagebox.showwarning(APP_NAME, "나눠 저장 크기를 알 수 없습니다. 예: 700 MB, 1 GB, 250")
                return
            paths = [i["path"] for i in self.c_items]
            name = core.sanitize_component(self.c_name.get().strip() or "압축파일", windows=True)
            out_dir = self.c_outdir.get().strip() or os.path.dirname(paths[0])
            out = os.path.join(out_dir, name + mf.format_ext(fmt))
            if os.path.exists(out):
                ans = messagebox.askyesnocancel(
                    "같은 이름의 파일이 있습니다",
                    f"'{os.path.basename(out)}' 파일이 이미 있습니다.\n\n"
                    "예 = 덮어쓰기\n아니오 = 이름 바꿔서 저장\n취소 = 돌아가기")
                if ans is None:
                    return
                if ans is False:
                    out = core.unique_file(out)
            junk = self.c_junk.get()
            self.s.update(format=fmt, level=level, exclude_junk=junk, open_folder=self.c_open.get())
            self.s.save()

            def fn(cb, cancel):
                return core.compress(paths, out, fmt, pw or None, level, cb, cancel, junk,
                                     split_size=split)

            def done(r):
                pct = (1 - r["size"] / r["orig"]) * 100 if r["orig"] else 0
                extra = f" · {len(r['parts'])}조각" if len(r.get("parts", [])) > 1 else ""
                sk = r.get("skipped") or []
                skt = f" · {len(sk)}개 건너뜀" if sk else ""
                self.finish_ok(f"압축 완료 · {core.fmt_size(r['orig'])} → {core.fmt_size(r['size'])}"
                               f" ({pct:.0f}% 절감){extra}{skt}",
                               r["parts"][0] if extra else out, self.c_open.get() and not sk)
                if sk:
                    messagebox.showwarning("일부 파일은 압축하지 못했습니다",
                                           f"읽을 수 없는 파일 {len(sk)}개를 빼고 압축했습니다.\n"
                                           f"(다른 프로그램에서 열려 있으면 닫고 다시 시도하세요)\n\n{skip_summary(sk)}")
            self.run_job(fn, done, "압축 중…")

        # ═════════════════ 풀기 탭 ═════════════════
        def build_extract(self, tab):
            tab.columnconfigure(0, weight=1)
            tab.rowconfigure(2, weight=1)
            head = ttk.Frame(tab)
            head.grid(row=0, column=0, sticky="ew")
            ttk.Label(head, text="압축 파일 (zip · 7z · tar · gz · rar … · .001)" + (" · 끌어다 놓기 가능" if dnd_ok else ""),
                      font=self.f_bold).pack(side="left")
            self.e_filter = tk.StringVar()
            fe = ttk.Entry(head, textvariable=self.e_filter, width=22)
            fe.pack(side="right")
            ttk.Label(head, text="내용 검색 ").pack(side="right")
            self.e_filter.trace_add("write", lambda *a: self.render_preview())
            self.e_names = []
            self.e_arc = tk.StringVar()
            fr, self.e_arc_entry = self.path_row(tab, self.e_arc, self.pick_archive)
            fr.grid(row=1, column=0, sticky="ew", pady=6)
            self.e_arc_entry.bind("<Return>", lambda e: self.refresh_preview())
            pf, self.e_tree = self.make_tree(tab, (("name", "압축 파일 안의 내용", 520, "w"),
                                                   ("size", "크기", 100, "e")), height=5)
            pf.grid(row=2, column=0, sticky="nsew")
            self.dnd(self.e_arc_entry, self.drop_archive)
            self.dnd(self.e_tree, self.drop_archive)
            self.e_tree.bind("<Double-1>", self.open_selected_entry)
            self.e_info = ttk.Label(tab, text="압축 파일을 선택하면 내용이 여기에 표시됩니다 · 항목을 더블클릭하면 바로 열어봅니다")
            self.e_info.grid(row=3, column=0, sticky="w", pady=(6, 0))

            opt = ttk.LabelFrame(tab, text=" 풀기 옵션 ", padding=12)
            opt.grid(row=4, column=0, sticky="ew", pady=(10, 8))
            opt.columnconfigure(1, weight=1)
            ttk.Label(opt, text="암호").grid(row=0, column=0, sticky="w", pady=3, padx=(0, 14))
            pw = ttk.Frame(opt)
            pw.grid(row=0, column=1, sticky="w")
            self.e_pw = tk.StringVar()
            self.e_pw_entry = ttk.Entry(pw, textvariable=self.e_pw, show="●", width=22)
            self.e_pw_entry.pack(side="left")
            self.e_pw_entry.bind("<Return>", lambda e: self.refresh_preview())
            self.e_show = tk.BooleanVar()
            ttk.Checkbutton(pw, text="암호 보기", variable=self.e_show,
                            command=lambda: self.toggle_show(self.e_show, self.e_pw_entry)).pack(side="left", padx=10)
            ttk.Label(pw, text="(암호가 걸린 파일만 입력)", font=self.f_small).pack(side="left")

            ttk.Label(opt, text="풀 위치").grid(row=1, column=0, sticky="nw", pady=3)
            where = ttk.Frame(opt)
            where.grid(row=1, column=1, sticky="ew")
            where.columnconfigure(0, weight=1)
            self.e_mode = tk.StringVar(value=self.s["extract_mode"])
            ttk.Radiobutton(where, text="압축 파일 옆에 폴더를 만들어서 풀기 (추천 · 폴더가 겹치지 않게 정리)",
                            value="beside", variable=self.e_mode).grid(row=0, column=0, sticky="w")
            ttk.Radiobutton(where, text="지정한 폴더에 그대로 풀기", value="custom",
                            variable=self.e_mode).grid(row=1, column=0, sticky="w", pady=(4, 0))
            self.e_dest = tk.StringVar()
            dr, _ = self.path_row(where, self.e_dest, self.pick_e_dest)
            dr.grid(row=2, column=0, sticky="ew", padx=(22, 0), pady=(2, 0))

            ttk.Label(opt, text="같은 이름이 있으면").grid(row=2, column=0, sticky="w", pady=3)
            self.e_policy = tk.StringVar(value=self.s["policy"])
            pf2 = ttk.Frame(opt)
            pf2.grid(row=2, column=1, sticky="w")
            for text, val in (("이름 바꿔 저장", "rename"), ("덮어쓰기", "overwrite"), ("건너뛰기", "skip")):
                ttk.Radiobutton(pf2, text=text, value=val, variable=self.e_policy).pack(side="left", padx=(0, 12))

            ttk.Label(opt, text="파일 이름 글자").grid(row=3, column=0, sticky="w", pady=3)
            ef = ttk.Frame(opt)
            ef.grid(row=3, column=1, sticky="w")
            self.e_enc = tk.StringVar(value="자동")
            ec = ttk.Combobox(ef, textvariable=self.e_enc, state="readonly", width=12,
                              values=tuple(ENC_CHOICES))
            ec.pack(side="left")
            ec.bind("<<ComboboxSelected>>", lambda e: self.refresh_preview())
            ttk.Label(ef, text="(한글이 깨져 보일 때만 바꿔 보세요)", font=self.f_small).pack(side="left", padx=10)

            sw = ttk.Frame(opt)
            sw.grid(row=4, column=1, sticky="w", pady=(6, 0))
            self.e_mac = tk.BooleanVar(value=self.s["skip_mac_junk"])
            self.e_open = tk.BooleanVar(value=self.s["open_folder"])
            ttk.Checkbutton(sw, text="맥 찌꺼기(__MACOSX, .DS_Store) 제외", variable=self.e_mac,
                            style=self.switch).pack(side="left")
            ttk.Checkbutton(sw, text="완료 후 폴더 열기", variable=self.e_open,
                            style=self.switch).pack(side="left", padx=18)

            self.pad_labels(opt)
            bar = ttk.Frame(tab)
            bar.grid(row=5, column=0, sticky="ew")
            b1 = ttk.Button(bar, text="무결성 검사", command=self.do_test)
            b1.pack(side="left")
            b2 = ttk.Button(bar, text="선택한 것만 풀기", command=lambda: self.do_extract(selected=True))
            b2.pack(side="left", padx=6)
            b = ttk.Button(bar, text="풀기 시작", style=self.accent, command=self.do_extract)
            b.pack(side="right")
            self.action_buttons.extend([b1, b2, b])

        def pick_archive(self):
            f = filedialog.askopenfilename(title="압축 파일 선택", filetypes=[
                ("압축 파일", "*.zip *.7z *.tar *.tgz *.gz *.tbz2 *.bz2 *.txz *.xz *.rar *.cab *.iso *.lzh *.lha *.zst *.001"), ("모든 파일", "*.*")])
            if f:
                self.set_archive(f)

        def drop_archive(self, paths):
            for p in paths:
                if is_archive(p):
                    self.set_archive(p)
                    return

        def pick_e_dest(self):
            d = filedialog.askdirectory(title="풀 위치 선택")
            if d:
                self.e_dest.set(os.path.normpath(d))
                self.e_mode.set("custom")

        def set_archive(self, path):
            self.e_arc.set(os.path.normpath(path))
            self.e_pw.set("")
            self.refresh_preview()

        def refresh_preview(self):
            path, pw = self.e_arc.get().strip(), self.e_pw.get() or None
            self.e_tree.delete(*self.e_tree.get_children())
            self.e_names = []
            if not os.path.isfile(path):
                return
            self.e_info.configure(text="내용을 읽는 중…")
            encoding = ENC_CHOICES[self.e_enc.get()]

            def work():
                try:
                    enc = core.archive_encrypted(path)
                    names, total, size = core.list_archive(path, pw, limit=5000, encoding=encoding)
                    res = ("ok", enc, names, (total, size))
                except core.WrongPassword:
                    res = ("pw", True, [], (0, 0))
                except Exception as e:
                    res = ("err", False, [], str(e))
                self.ui(lambda: self.show_preview(path, res))
            threading.Thread(target=work, daemon=True).start()

        def show_preview(self, path, res):
            if path != self.e_arc.get().strip():
                return
            kind, enc, names, extra = res
            if kind == "pw":
                self.e_info.configure(text="암호가 걸린 파일입니다. 암호를 입력하고 Enter를 누르면 내용이 보입니다.")
                self.e_pw_entry.focus_set()
                return
            if kind == "err":
                self.e_info.configure(text=f"열 수 없습니다: {extra}")
                return
            self.e_names = names
            self.e_total = extra
            self.e_enc_flag = enc
            self.render_preview()
            if enc and not self.e_pw.get():
                self.e_pw_entry.focus_set()

        def render_preview(self):
            if not getattr(self, "e_names", None) and not hasattr(self, "e_total"):
                return
            q = self.e_filter.get().strip().lower()
            self.e_tree.delete(*self.e_tree.get_children())
            shown = 0
            for n, sz in self.e_names:
                if q and q not in n.lower():
                    continue
                self.e_tree.insert("", "end", values=(n, core.fmt_size(sz) if sz and not n.endswith("/") else ""))
                shown += 1
            total, size = self.e_total
            more = f" (처음 {len(self.e_names):,}개만 읽음)" if total > len(self.e_names) else ""
            lock = " · 암호 있음" if getattr(self, "e_enc_flag", False) else ""
            flt = f" · 검색 결과 {shown:,}개" if q else ""
            self.e_info.configure(text=f"항목 {total:,}개 · 풀면 {core.fmt_size(size)}{more}{lock}{flt}")
            if getattr(self, "e_enc_flag", False) and not self.e_pw.get():
                self.e_pw_entry.focus_set()

        def selected_names(self):
            return [self.e_tree.set(i, "name") for i in self.e_tree.selection()]

        def open_selected_entry(self, event):
            iid = self.e_tree.identify_row(event.y)
            if not iid:
                return
            name = self.e_tree.set(iid, "name")
            if name.endswith("/"):
                return
            arc, pw = self.e_arc.get().strip(), self.e_pw.get() or None
            encoding = ENC_CHOICES[self.e_enc.get()]
            self.status.set(f"'{os.path.basename(name)}' 여는 중…")

            def work():
                try:
                    import tempfile
                    tmpd = tempfile.mkdtemp(prefix="ssamzip_open_")
                    self.open_tmp.append(tmpd)
                    p = core.open_entry(arc, name, tmpd, pw, encoding)
                    self.ui(lambda: (self.status.set("열었습니다"), reveal_open(p)))
                except core.WrongPassword:
                    self.ui(lambda: (self.status.set("암호가 필요하거나 틀렸습니다"),
                                     self.e_pw_entry.focus_set()))
                except Exception as e:
                    msg = str(e) or e.__class__.__name__
                    self.ui(lambda: (self.status.set("열 수 없습니다"), messagebox.showerror(APP_NAME, msg)))
            threading.Thread(target=work, daemon=True).start()

        def do_test(self):
            arc = self.e_arc.get().strip()
            if not os.path.isfile(arc):
                messagebox.showwarning(APP_NAME, "검사할 압축 파일을 먼저 선택하세요.")
                return
            pw, encoding = self.e_pw.get() or None, ENC_CHOICES[self.e_enc.get()]

            def fn(cb, cancel):
                return core.test_archive(arc, pw, cb, cancel, encoding)

            def done(r):
                if r["bad"]:
                    shown = "\n".join(r["bad"][:15]) + ("\n…" if len(r["bad"]) > 15 else "")
                    self.status.set(f"검사 결과 · 문제 있는 파일 {len(r['bad'])}개")
                    messagebox.showwarning("무결성 검사", f"문제가 있는 파일 {len(r['bad'])}개:\n\n{shown}")
                else:
                    self.finish_ok(f"검사 완료 · 파일 {r['files']:,}개 모두 이상 없음", None, False)
            self.run_job(fn, done, "검사 중…")

        def do_extract(self, selected=False):
            arc = self.e_arc.get().strip()
            if not os.path.isfile(arc):
                messagebox.showwarning(APP_NAME, "풀 압축 파일을 먼저 선택하세요.")
                return
            only = None
            if selected:
                only = self.selected_names()
                if not only:
                    messagebox.showinfo(APP_NAME, "위 목록에서 풀 항목을 먼저 선택하세요. (Ctrl/Shift로 여러 개 선택)")
                    return
            encoding = ENC_CHOICES[self.e_enc.get()]
            pw = self.e_pw.get() or None
            policy, mac = self.e_policy.get(), self.e_mac.get()
            mode = self.e_mode.get()
            dest = self.e_dest.get().strip()
            if mode == "custom" and not dest:
                messagebox.showwarning(APP_NAME, "풀 위치를 지정하거나 '압축 파일 옆에 폴더 만들기'를 선택하세요.")
                return
            self.s.update(extract_mode=mode, policy=policy, skip_mac_junk=mac, open_folder=self.e_open.get())
            self.s.save()

            def fn(cb, cancel):
                if mode == "beside":
                    return core.extract_smart(arc, os.path.dirname(arc), pw, policy, mac, cb, cancel,
                                              only=only, encoding=encoding)
                return core.extract(arc, dest, pw, policy, False, mac, cb, cancel,
                                    only=only, encoding=encoding)

            def done(r):
                sk = r.get("skipped") or []
                self.finish_ok(f"풀기 완료 · 파일 {r['files']:,}개 · {core.fmt_size(r['size'])}"
                               + (f" · {len(sk)}개 건너뜀" if sk else ""),
                               r["dest"], self.e_open.get() and not sk)
                if sk:
                    messagebox.showwarning("일부 항목은 풀지 않았습니다",
                                           f"안전하지 않거나 겹치는 항목 {len(sk)}개를 건너뛰고 풀었습니다.\n\n{skip_summary(sk)}")
            self.run_job(fn, done, "푸는 중…")

        # ═════════════════ 일괄 풀기 탭 ═════════════════
        def build_batch(self, tab):
            tab.columnconfigure(0, weight=1)
            tab.rowconfigure(2, weight=1)
            bl = ttk.Label(tab, text="학생 제출 파일처럼 폴더 안에 흩어진 압축 파일(zip·7z·tar·rar 등)을 한 번에 풉니다. "
                                     "각 압축 파일 이름으로 폴더를 만들어 정리해요.")
            bl.grid(row=0, column=0, sticky="w")
            self.autowrap(bl, 30)
            top = ttk.Frame(tab)
            top.grid(row=1, column=0, sticky="ew", pady=8)
            top.columnconfigure(0, weight=1)
            self.b_folder = tk.StringVar()
            fr, self.b_entry = self.path_row(top, self.b_folder, self.pick_b_folder)
            fr.grid(row=0, column=0, sticky="ew")
            self.b_entry.bind("<Return>", lambda e: self.batch_scan())
            self.b_rec = tk.BooleanVar(value=self.s["batch_recursive"])
            ttk.Checkbutton(top, text="하위 폴더 포함", variable=self.b_rec, style=self.switch,
                            command=self.batch_scan).grid(row=0, column=1, padx=10)
            ttk.Button(top, text="다시 찾기", command=self.batch_scan).grid(row=0, column=2)
            tf, self.b_tree = self.make_tree(tab, (("name", "압축 파일", 430, "w"), ("size", "크기", 80, "e"),
                                                   ("state", "상태", 230, "w")), height=6)
            tf.grid(row=2, column=0, sticky="nsew")
            self.b_tree.bind("<Double-1>", self.b_open_row)
            self.dnd(self.b_entry, self.drop_batch)
            self.dnd(self.b_tree, self.drop_batch)
            self.b_summary = ttk.Label(tab, text="폴더를 선택하거나 끌어다 놓으면 압축 파일을 찾아줍니다")
            self.b_summary.grid(row=3, column=0, sticky="w", pady=(6, 0))

            opt = ttk.LabelFrame(tab, text=" 일괄 풀기 옵션 ", padding=12)
            opt.grid(row=4, column=0, sticky="ew", pady=(10, 8))
            opt.columnconfigure(1, weight=1)
            ttk.Label(opt, text="풀 위치").grid(row=0, column=0, sticky="nw", padx=(0, 14))
            w = ttk.Frame(opt)
            w.grid(row=0, column=1, sticky="ew")
            w.columnconfigure(0, weight=1)
            self.b_where = tk.StringVar(value=self.s["batch_where"])
            ttk.Radiobutton(w, text="각 압축 파일 옆에 (이름 폴더로 정리)", value="beside",
                            variable=self.b_where).grid(row=0, column=0, sticky="w")
            ttk.Radiobutton(w, text="한 폴더에 모아서", value="collect",
                            variable=self.b_where).grid(row=1, column=0, sticky="w", pady=(4, 0))
            self.b_out = tk.StringVar()
            orow, _ = self.path_row(w, self.b_out, self.pick_b_out)
            orow.grid(row=2, column=0, sticky="ew", padx=(22, 0), pady=(2, 0))
            ttk.Label(opt, text="암호").grid(row=1, column=0, sticky="w", pady=(8, 0))
            pwf = ttk.Frame(opt)
            pwf.grid(row=1, column=1, sticky="w", pady=(8, 0))
            self.b_pw = tk.StringVar()
            pe = ttk.Entry(pwf, textvariable=self.b_pw, show="●", width=22)
            pe.pack(side="left")
            self.b_show = tk.BooleanVar()
            ttk.Checkbutton(pwf, text="암호 보기", variable=self.b_show,
                            command=lambda: self.toggle_show(self.b_show, pe)).pack(side="left", padx=10)
            ttk.Label(opt, text="암호 후보").grid(row=2, column=0, sticky="nw", pady=(8, 0))
            self.b_cands = tk.Text(opt, height=2, width=30, wrap="none", font=self.f_small)
            self.b_cands.grid(row=2, column=1, sticky="w", pady=(8, 0))
            cl = ttk.Label(opt, text="암호가 제각각이면 한 줄에 하나씩 적으세요. 암호가 걸린 파일마다 위 암호와 후보를 "
                                     "차례로 시도하고, 암호 없는 파일은 그냥 풉니다. 실패한 항목만 남으니 다시 시작하면 그것만 처리해요.",
                           font=self.f_small, justify="left")
            cl.grid(row=3, column=1, sticky="w", pady=(2, 0))
            self.autowrap(cl, 190)
            self.pad_labels(opt)
            b = ttk.Button(tab, text="일괄 풀기 시작", style=self.accent, command=self.do_batch)
            b.grid(row=5, column=0, sticky="e")
            self.action_buttons.append(b)

        def pick_b_folder(self):
            d = filedialog.askdirectory(title="압축 파일이 들어 있는 폴더 선택")
            if d:
                self.b_folder.set(os.path.normpath(d))
                self.batch_scan()

        def pick_b_out(self):
            d = filedialog.askdirectory(title="모아서 풀 폴더 선택")
            if d:
                self.b_out.set(os.path.normpath(d))
                self.b_where.set("collect")

        def drop_batch(self, paths):
            if paths and os.path.isdir(paths[0]):
                self.b_folder.set(os.path.normpath(paths[0]))
                self.batch_scan()
            else:
                arcs = [p for p in paths if is_archive(p)]
                if arcs:
                    self.batch_set(arcs, f"(선택한 파일 {len(arcs)}개)")

        def batch_scan(self):
            folder = self.b_folder.get().strip()
            if not os.path.isdir(folder) or self.b_active:
                return
            self.s["batch_recursive"] = self.b_rec.get()
            self.batch_set(core.find_archives(folder, self.b_rec.get()), None)

        def batch_set(self, archives, label):
            if self.b_active:
                return
            if label:
                self.b_folder.set(label)
            base = self.b_folder.get().strip()
            self.b_rows = [{"path": p, "ok": False} for p in archives]
            self.b_tree.delete(*self.b_tree.get_children())
            for i, p in enumerate(archives):
                try:
                    rel = os.path.relpath(p, base) if os.path.isdir(base) else os.path.basename(p)
                    size = core.fmt_size(os.path.getsize(p))
                except (OSError, ValueError):
                    rel, size = p, ""
                self.b_tree.insert("", "end", iid=str(i), values=(rel, size, "대기"))
            self.b_summary.configure(
                text=f"압축 파일 {len(archives)}개를 찾았습니다" if archives else "압축 파일이 없습니다 (zip·7z·tar·gz·rar 등)")

        def b_open_row(self, event):
            iid = self.b_tree.identify_row(event.y)
            if iid:
                reveal(self.b_rows[int(iid)]["path"])

        def do_batch(self):
            idxs = [i for i, r in enumerate(self.b_rows) if not r["ok"]]
            if not idxs:
                messagebox.showinfo(APP_NAME, "풀 압축 파일이 없습니다. 폴더를 먼저 선택하세요.")
                return
            out_dir = None
            if self.b_where.get() == "collect":
                out_dir = self.b_out.get().strip()
                if not out_dir:
                    messagebox.showwarning(APP_NAME, "모아서 풀 폴더를 지정하세요.")
                    return
            pw = self.b_pw.get() or None
            cands = [ln.strip() for ln in self.b_cands.get("1.0", "end").splitlines() if ln.strip()]
            policy = self.s["policy"]
            archives = [self.b_rows[i]["path"] for i in idxs]
            self.s["batch_where"] = self.b_where.get()
            self.s.save()
            with self.b_lock:
                self.b_state.clear()
                self.b_prog.clear()
                self.b_dirty = set(idxs)
                for i in idxs:
                    self.b_state[i] = ("wait", None)
            self.b_active = True
            base = self.b_folder.get().strip()

            def event(k, kind, payload):
                i = idxs[k]
                with self.b_lock:
                    if kind == "progress":
                        if self.b_state.get(i, ("", None))[0] in ("start", "wait"):
                            self.b_prog[i] = payload
                    else:
                        self.b_state[i] = (kind, payload)
                    self.b_dirty.add(i)

            def fn(cb, cancel):
                return core.batch_extract(archives, out_dir, pw, policy, event, cancel, passwords=cands)

            def done(results):
                self.b_active = False
                self.batch_refresh(force=True)
                ok = sum(1 for _, s, _m in results if s)
                bad = len(results) - ok
                if bad:
                    self.finish_ok(f"일괄 풀기 끝 · 성공 {ok}개 · 실패 {bad}개 (표에서 사유 확인)", None, False)
                else:
                    self.finish_ok(f"일괄 풀기 완료 · {ok}개 모두 성공", out_dir or (base if os.path.isdir(base) else None),
                                   self.s["open_folder"])
                self.b_summary.configure(text=f"성공 {sum(1 for r in self.b_rows if r['ok'])}개 · "
                                              f"실패/대기 {sum(1 for r in self.b_rows if not r['ok'])}개")

            self.run_job(fn, done, "일괄 풀기 중…")

        def batch_refresh(self, force=False):
            with self.b_lock:
                dirty, self.b_dirty = self.b_dirty, set()
                states, progs = dict(self.b_state), dict(self.b_prog)
            for i in dirty:
                kind, payload = states.get(i, ("wait", None))
                if kind == "ok":
                    self.b_rows[i]["ok"] = True
                    text, tag = "완료 → " + os.path.basename(payload or ""), "ok"
                elif kind == "fail":
                    text, tag = f"실패: {payload}", "fail"
                elif kind == "skip":
                    text, tag = "취소됨", ""
                elif kind == "start":
                    text, tag = f"푸는 중 {progs.get(i, 0) * 100:.0f}%", ""
                else:
                    text, tag = "대기", ""
                if self.b_tree.exists(str(i)):
                    self.b_tree.set(str(i), "state", text)
                    self.b_tree.item(str(i), tags=(tag,) if tag else ())
            if states:
                finished = sum(1 for k, _ in states.values() if k in ("ok", "fail", "skip"))
                running = sum(progs.get(i, 0) for i, (k, _) in states.items() if k == "start")
                self.prog["value"] = (finished + running) / len(states) * 1000
                self.status.set(f"일괄 풀기 중… {finished}/{len(states)}")

        # ═════════════════ 설정 탭 ═════════════════
        def build_settings(self, tab):
            tab.columnconfigure(0, weight=1)
            is_win = winshell.IS_WIN
            sh = ttk.LabelFrame(tab, text=" 윈도우 탐색기 연동 ", padding=8)
            sh.grid(row=0, column=0, sticky="ew")
            sh.columnconfigure(1, weight=1)
            ttk.Label(sh, text="우클릭 메뉴", font=self.f_bold).grid(row=0, column=0, sticky="w")
            self.sh_ctx = ttk.Label(sh, text="")
            self.sh_ctx.grid(row=0, column=1, sticky="w", padx=12)
            bf = ttk.Frame(sh)
            bf.grid(row=0, column=2)
            ttk.Button(bf, text="등록", command=lambda: self.shell_action("ctx_on")).pack(side="left")
            ttk.Button(bf, text="해제", command=lambda: self.shell_action("ctx_off")).pack(side="left", padx=6)
            l1 = ttk.Label(sh, text="우클릭: '쌤ZIP으로 압축' · '여기에 풀기' · '폴더 안 압축파일 전부 풀기' "
                                    "(윈도우 11은 '추가 옵션 표시' Shift+F10 안에 있음)", font=self.f_small)
            l1.grid(row=1, column=0, columnspan=3, sticky="w", pady=(2, 6))
            self.autowrap(l1)
            ttk.Label(sh, text="압축 파일 더블클릭", font=self.f_bold).grid(row=2, column=0, sticky="w")
            self.sh_assoc = ttk.Label(sh, text="")
            self.sh_assoc.grid(row=2, column=1, sticky="w", padx=12)
            bf2 = ttk.Frame(sh)
            bf2.grid(row=2, column=2)
            ttk.Button(bf2, text="등록", command=lambda: self.shell_action("assoc_on")).pack(side="left")
            ttk.Button(bf2, text="해제", command=lambda: self.shell_action("assoc_off")).pack(side="left", padx=6)
            l2 = ttk.Label(sh, text="윈도우는 기본 앱을 프로그램이 바꿀 수 없어서, '등록' 뒤 열리는 설정에서 "
                                    ".zip .7z .tar .gz .rar 등을 쌤ZIP으로 한 번만 선택해야 합니다.", font=self.f_small)
            l2.grid(row=3, column=0, columnspan=3, sticky="w", pady=(2, 0))
            self.autowrap(l2)
            if not is_win:
                ttk.Label(sh, text="※ 윈도우에서만 사용할 수 있는 기능입니다.", foreground=self.bad_color
                          ).grid(row=4, column=0, columnspan=3, sticky="w", pady=(6, 0))

            df = ttk.LabelFrame(tab, text=" 기본값 ", padding=8)
            df.grid(row=1, column=0, sticky="ew", pady=5)
            ttk.Label(df, text="화면 모양").grid(row=0, column=0, sticky="w", pady=3, padx=(0, 14))
            self.set_theme = tk.StringVar(value=self.s["theme"])
            tf = ttk.Frame(df)
            tf.grid(row=0, column=1, sticky="w")
            ttk.Radiobutton(tf, text="밝게", value="light", variable=self.set_theme,
                            command=self.on_theme).pack(side="left")
            ttk.Radiobutton(tf, text="어둡게", value="dark", variable=self.set_theme,
                            command=self.on_theme).pack(side="left", padx=12)
            if not has_sv:
                ttk.Label(tf, text="(sv-ttk 설치 시 적용)", font=self.f_small).pack(side="left")
            ttk.Label(df, text="우클릭 압축은 압축 탭에서 마지막으로 쓴 형식·수준을 그대로 사용합니다.", font=self.f_small
                      ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))

            up = ttk.LabelFrame(tab, text=" 업데이트 ", padding=8)
            up.grid(row=2, column=0, sticky="ew", pady=(0, 5))
            up.columnconfigure(1, weight=1)
            ttk.Label(up, text="업데이트 주소").grid(row=0, column=0, sticky="w", padx=(0, 14))
            self.set_upurl = tk.StringVar(value=self.s["update_url"])
            ue = ttk.Entry(up, textvariable=self.set_upurl)
            ue.grid(row=0, column=1, sticky="ew")
            ue.bind("<FocusOut>", lambda e: self.save_update_settings())
            ue.bind("<Return>", lambda e: self.save_update_settings())
            ttk.Button(up, text="지금 확인", command=lambda: self.check_update(True)).grid(row=0, column=2, padx=(8, 0))
            self.set_autoup = tk.BooleanVar(value=self.s["auto_update"])
            ttk.Checkbutton(up, text="시작할 때 하루 한 번 새 버전 확인", variable=self.set_autoup,
                            style=self.switch, command=self.save_update_settings
                            ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))
            self.up_status = ttk.Label(up, text=f"현재 버전 {VERSION}", font=self.f_small)
            self.up_status.grid(row=2, column=0, columnspan=3, sticky="w", pady=(4, 0))
            l3 = ttk.Label(up, text="GitHub 저장소 주소나 latest.json 주소를 적으세요 · 받은 파일은 SHA-256이 맞을 때만 설치",
                           font=self.f_small)
            l3.grid(row=3, column=0, columnspan=3, sticky="w", pady=(2, 0))
            self.autowrap(l3)

            inf = ttk.LabelFrame(tab, text=" 정보 ", padding=8)
            inf.grid(row=3, column=0, sticky="ew")
            l4 = ttk.Label(inf, text=f"{APP_NAME} {VERSION} · ZIP(AES-256 암호) · 7z · TAR · GZ/BZ2/XZ 만들기, "
                                     f"RAR·CAB·ISO 등 풀기 · 설정 위치: {settings_dir()}", font=self.f_small)
            l4.pack(anchor="w", fill="x")
            self.autowrap(l4)
            bi = build_info()
            if bi:
                l5 = ttk.Label(inf, text=bi, font=self.f_small)
                l5.pack(anchor="w", pady=(2, 0), fill="x")
                self.autowrap(l5)
            bf3 = ttk.Frame(inf)
            bf3.pack(anchor="w", pady=(8, 0))
            ttk.Button(bf3, text="변경 이력", command=self.show_changelog).pack(side="left")
            ttk.Button(bf3, text="파일 해시(SHA-256) 계산", command=self.hash_tool).pack(side="left", padx=8)
            self.refresh_shell_status()

        def save_update_settings(self):
            self.s["update_url"] = self.set_upurl.get().strip()
            self.s["auto_update"] = bool(self.set_autoup.get())
            self.s.save()

        def maybe_auto_check(self):
            if self.s["auto_update"] and self.s["update_url"] and self.main_visible \
                    and updater.due(self.s["last_update_check"]):
                self.check_update(False)

        def check_update(self, manual):
            if manual:
                self.save_update_settings()
            url = self.s["update_url"].strip()
            if not url:
                if manual:
                    messagebox.showinfo(APP_NAME, "업데이트 주소가 비어 있습니다.\n배포하는 GitHub 저장소 주소나 latest.json 주소를 적어 주세요.")
                return
            self.s["last_update_check"] = time.time()
            self.s.save()
            if manual:
                self.up_status.configure(text="확인하는 중…")

            def work():
                m = err = None
                try:
                    m = updater.check(url, VERSION)
                except updater.UpdateError as e:
                    err = str(e)
                except Exception as e:      # 네트워크 등 예상 밖 오류도 프로그램을 멈추지 않게
                    err = f"업데이트를 확인하지 못했습니다. ({type(e).__name__})"
                self.ui(lambda: self.on_update_checked(m, err, manual))
            threading.Thread(target=work, daemon=True).start()

        def on_update_checked(self, m, err, manual):
            if err:
                self.up_status.configure(text=err)
                if manual:
                    messagebox.showwarning(APP_NAME, err)
                return
            if not m:
                self.up_status.configure(text=f"최신 버전입니다 (현재 {VERSION})")
                if manual:
                    messagebox.showinfo(APP_NAME, f"최신 버전을 쓰고 있습니다. (현재 {VERSION})")
                return
            self.up_status.configure(text=f"새 버전 {m['version']} 이(가) 있습니다")
            if not manual and m["version"] == self.s["skip_version"]:
                return
            notes = ("\n\n" + m["notes"]) if m["notes"] else ""
            how = "받아서 설치하고 다시 시작할까요?" if updater.can_self_update() else "파일을 받아 둘까요? (직접 바꿔 넣어야 합니다)"
            ans = messagebox.askyesnocancel(
                APP_NAME, f"새 버전 {m['version']} (현재 {VERSION})이 있습니다.{notes}\n\n{how}\n\n"
                          "예 = 지금 · 아니오 = 나중에 · 취소 = 이 버전은 알리지 않기")
            if ans is None:
                self.s["skip_version"] = m["version"]
                self.s.save()
            elif ans:
                self.download_update(m)

        def download_update(self, m):
            win = tk.Toplevel(root)
            win.title("업데이트 받는 중")
            win.resizable(False, False)
            win.transient(root)
            pb = ttk.Progressbar(win, length=360, maximum=100)
            pb.pack(padx=18, pady=(18, 6))
            lab = ttk.Label(win, text="받는 중…")
            lab.pack(padx=18)
            cancel = threading.Event()
            ttk.Button(win, text="취소", command=cancel.set).pack(pady=12)
            win.protocol("WM_DELETE_WINDOW", cancel.set)
            import shutil
            tmpd = os.path.join(settings_dir(), "updates")      # 받은 파일은 설정 폴더 안에 두어 사용자가 찾을 수 있게
            shutil.rmtree(tmpd, ignore_errors=True)

            def prog(done, total):
                if total:
                    pct = done * 100 / total
                    self.ui(lambda: (pb.configure(value=pct),
                                     lab.configure(text=f"{core.fmt_size(done)} / {core.fmt_size(total)}")))

            def work():
                path = err = None
                try:
                    path = updater.download(m, tmpd, prog, cancel)
                except updater.UpdateError as e:
                    err = str(e)
                except Exception as e:
                    err = f"받는 중 오류가 났습니다. ({type(e).__name__})"
                self.ui(lambda: self.after_download(win, path, err, m))
            threading.Thread(target=work, daemon=True).start()

        def after_download(self, win, path, err, m):
            win.destroy()
            if err:
                if "취소" not in err:
                    messagebox.showerror(APP_NAME, err)
                return
            if updater.can_self_update():
                if self.runner.busy:
                    messagebox.showinfo(APP_NAME, "진행 중인 작업이 있어 지금은 설치하지 않습니다. 작업이 끝난 뒤 다시 확인해 주세요.")
                    return
                try:
                    updater.launch_update(path)
                except updater.UpdateError as e:
                    messagebox.showerror(APP_NAME, str(e))
                    return
                self.cleanup_tmp()
                root.destroy()
            else:
                messagebox.showinfo(APP_NAME, f"새 버전 {m['version']} 파일을 받았습니다.\n받은 파일의 SHA-256 확인까지 마쳤습니다.\n\n{path}")
                reveal(path)

        def show_changelog(self):
            text = read_changelog()
            win = tk.Toplevel(root)
            win.title(f"{APP_NAME} 변경 이력")
            win.geometry("640x480")
            fr = ttk.Frame(win, padding=8)
            fr.pack(fill="both", expand=True)
            t = tk.Text(fr, wrap="word", font=self.f_small, relief="flat")
            sb = ttk.Scrollbar(fr, command=t.yview)
            t.configure(yscrollcommand=sb.set)
            sb.pack(side="right", fill="y")
            t.pack(side="left", fill="both", expand=True)
            t.insert("1.0", text)
            t.configure(state="disabled")

        def hash_tool(self):
            p = filedialog.askopenfilename(title="해시를 계산할 파일 선택")
            if not p:
                return
            self.up_status.configure(text="해시 계산 중…")

            def work():
                try:
                    h = updater.file_sha256(p)
                except (OSError, updater.UpdateError) as e:
                    msg = f"파일을 읽지 못했습니다.\n{e}"
                    self.ui(lambda: messagebox.showerror(APP_NAME, msg))
                    return

                def show():
                    root.clipboard_clear()
                    root.clipboard_append(h)
                    self.up_status.configure(text="")
                    messagebox.showinfo("SHA-256", f"{os.path.basename(p)}\n\n{h}\n\n(클립보드에 복사했습니다)")
                self.ui(show)
            threading.Thread(target=work, daemon=True).start()

        def on_theme(self):
            self.s["theme"] = self.set_theme.get()
            self.s.save()
            self.apply_theme()

        def refresh_shell_status(self):
            def txt(ok):
                return "● 등록됨" if ok else "○ 등록 안 됨"
            self.sh_ctx.configure(text=txt(winshell.is_context_installed()))
            self.sh_assoc.configure(text=txt(winshell.is_association_installed()))

        def shell_action(self, what):
            if not winshell.IS_WIN:
                messagebox.showinfo(APP_NAME, "윈도우에서만 사용할 수 있는 기능입니다.")
                return
            try:
                script = os.path.abspath(sys.argv[0])
                if what == "ctx_on":
                    winshell.install_context_menu(script)
                    messagebox.showinfo(APP_NAME, "우클릭 메뉴를 등록했습니다.\n(윈도우 11은 '추가 옵션 표시'에 나타납니다)")
                elif what == "ctx_off":
                    winshell.uninstall_context_menu(script)
                elif what == "assoc_on":
                    winshell.install_association(script)
                    if messagebox.askyesno(APP_NAME, "연결 프로그램에 등록했습니다.\n\n지금 기본 앱 설정 화면을 열어서 "
                                                     ".zip / .7z 등의 기본 앱을 쌤ZIP으로 선택하시겠어요?"):
                        winshell.open_default_apps_settings()
                elif what == "assoc_off":
                    winshell.uninstall_association()
            except OSError as e:
                messagebox.showerror(APP_NAME, f"레지스트리를 쓰지 못했습니다.\n{e}")
            self.refresh_shell_status()

        # ═════════════════ 작업 공통 ═════════════════
        def ui(self, fn):
            self.ui_calls.put(fn)

        def set_busy(self, busy):
            for b in self.action_buttons:
                b.configure(state="disabled" if busy else "normal")
            self.cancel_btn.configure(state="normal" if busy else "disabled")
            if busy:
                self.reveal_btn.configure(state="disabled")
                self.last_result = None

        def run_job(self, fn, on_done, label):
            if self.runner.busy:
                return
            self.on_done = on_done
            self.job_label = label
            self.prog["value"] = 0
            self.status.set(label)
            self.detail.set("")
            self.set_busy(True)
            self.runner.start(fn)

        def cancel_job(self):
            self.runner.cancel.set()
            self.status.set("취소하는 중…")

        def finish_ok(self, text, result_path, auto_open):
            self.status.set(text)
            self.prog["value"] = 1000
            self.detail.set("")
            self.last_result = result_path
            if result_path:
                self.reveal_btn.configure(state="normal")
                if auto_open:
                    reveal(result_path)

        def update_progress_ui(self):
            r = self.runner
            p = r.latest
            if not p or self.b_active:
                return
            done, total, name = p
            self.prog["value"] = done / total * 1000 if total else 0
            el = max(time.monotonic() - r.t0, 0.001)
            spd = done / el
            eta = (total - done) / spd if spd > 0 else 0
            self.status.set(f"{self.job_label} {done / total * 100:.0f}%" if total else self.job_label)
            tail = f" · 남은 시간 {core.fmt_time(eta)}" if el > 1.5 and done < total else ""
            self.detail.set(f"{core.fmt_size(done)} / {core.fmt_size(total)} · {core.fmt_size(spd)}/초{tail}")
            self.quick_progress(name)

        def handle_job_event(self, kind, payload):
            self.runner.busy = False
            self.set_busy(False)
            handler, self.on_done = self.on_done, None
            self.detail.set("")
            if kind == "done":
                self.prog["value"] = 1000
                if handler:
                    handler(payload)
                return
            if self.b_active:
                self.b_active = False
                self.batch_refresh(force=True)
            if kind == "cancel":
                self.status.set("취소했습니다")
                self.prog["value"] = 0
            elif kind == "pw":
                self.status.set("암호가 필요하거나 틀렸습니다")
                self.prog["value"] = 0
                if self.qwin is None:
                    messagebox.showwarning("암호 확인", "암호가 없거나 틀렸습니다.\n암호를 입력한 뒤 다시 시도하세요.")
                    if self.nb.index(self.nb.select()) == 1:
                        self.e_pw_entry.focus_set()
            else:
                self.status.set("오류가 발생했습니다")
                self.prog["value"] = 0
                if self.qwin is None:
                    messagebox.showerror(APP_NAME, f"작업 중 문제가 생겼습니다.\n\n{payload}")

        # ═════════════════ 주기 처리 ═════════════════
        def tick(self):
            try:
                while True:
                    self.ui_calls.get_nowait()()
            except queue.Empty:
                pass
            try:
                while True:
                    mode, paths = self.req_q.get_nowait()
                    self.handle_request(mode, paths)
            except queue.Empty:
                pass
            r = self.runner
            if r.busy:
                self.update_progress_ui()
                if self.b_active:
                    self.batch_refresh()
                try:
                    while True:
                        kind, payload = r.events.get_nowait()
                        self.runner_event(kind, payload)
                except queue.Empty:
                    pass
            elif self.deferred and not self.flush_scheduled:
                m, p = self.deferred.pop(0)
                self.dispatch(m, p)
            root.after(80, self.tick)

        def runner_event(self, kind, payload):
            if self.qwin is not None or self.quick_ctx:
                self.quick_event(kind, payload)
            else:
                self.handle_job_event(kind, payload)

        # ═════════════════ 외부 요청(우클릭/더블클릭) ═════════════════
        quick_ctx = None
        first_req = None
        flush_job = None
        quit_job = None

        def handle_request(self, mode, paths):
            """
            요청을 잠깐 모아서 처리한다. 새 요청이 올 때마다 대기 시간을 다시 잡되 처음부터 최대 3초까지만 기다린다.
            (탐색기는 선택한 파일마다 프로그램을 따로 실행하고, 실행 속도는 PC 상태에 따라 들쭉날쭉하다)
            """
            self.pending.setdefault(mode, []).extend(paths)
            now = time.monotonic()
            if self.first_req is None:
                self.first_req = now
            if self.flush_job is not None:
                root.after_cancel(self.flush_job)
            gap = 700 if mode in ("compress", "extract-here") else 300     # 창 열기는 덜 기다린다
            left = max(1, int(3000 - (now - self.first_req) * 1000))
            self.flush_scheduled = True
            self.flush_job = root.after(min(gap, left), self.flush_requests)

        def flush_requests(self):
            self.flush_scheduled = False
            self.flush_job = None
            self.first_req = None
            pend, self.pending = self.pending, {}
            for mode, paths in pend.items():
                if self.runner.busy:
                    self.deferred.append((mode, paths))
                else:
                    self.dispatch(mode, paths)

        def show_main(self, tab=None):
            self.main_visible = True
            root.deiconify()
            root.lift()
            root.attributes("-topmost", True)
            root.after(300, lambda: root.attributes("-topmost", False))
            root.focus_force()
            if tab is not None:
                self.nb.select(tab)

        def dispatch(self, mode, paths):
            if mode == "show":
                self.show_main()
            elif mode == "compress" and paths:
                self.quick_compress(paths)
            elif mode == "extract-here" and paths:
                self.quick_extract([p for p in paths if is_archive(p)])
            elif mode == "compress-opts" and paths:
                self.show_main(self.tab_c)
                self.add_c_paths(paths)
            elif mode == "open" and paths:
                self.show_main(self.tab_e)
                if len(paths) == 1:
                    self.set_archive(paths[0])
                else:
                    self.nb.select(self.tab_b)
                    self.batch_set(paths, f"(선택한 파일 {len(paths)}개)")
            elif mode == "batch" and paths:
                self.show_main(self.tab_b)
                self.b_folder.set(paths[0])
                self.batch_scan()
            else:
                self.show_main()

        # ── 빠른 실행(창 없이 진행률만) ──
        def open_quick_window(self, title):
            if self.qwin is not None:
                return
            w = tk.Toplevel(root)
            w.title(APP_NAME)
            w.geometry("460x170")
            w.resizable(False, False)
            w.protocol("WM_DELETE_WINDOW", self.cancel_job)
            self.q_title, self.q_status = tk.StringVar(value=title), tk.StringVar(value="")
            f = ttk.Frame(w, padding=18)
            f.pack(fill="both", expand=True)
            ttk.Label(f, textvariable=self.q_title, font=self.f_bold).pack(anchor="w")
            ttk.Label(f, textvariable=self.q_status, font=self.f_small, wraplength=420).pack(anchor="w", pady=(2, 8))
            self.q_prog = ttk.Progressbar(f, maximum=1000)
            self.q_prog.pack(fill="x")
            self.q_detail = tk.StringVar()
            ttk.Label(f, textvariable=self.q_detail, font=self.f_small).pack(anchor="w", pady=(6, 0))
            ttk.Button(f, text="취소", command=self.cancel_job).pack(anchor="e", pady=(4, 0))
            w.update_idletasks()
            x = (w.winfo_screenwidth() - 460) // 2
            y = (w.winfo_screenheight() - 170) // 3
            w.geometry(f"+{x}+{y}")
            w.attributes("-topmost", True)
            self.qwin = w

        def close_quick_window(self):
            if self.qwin is not None:
                self.qwin.destroy()
                self.qwin = None

        def maybe_quit(self):
            """작업이 끝나도 잠깐 기다렸다가 종료한다 (늦게 도착하는 요청을 같은 프로그램이 받도록)"""
            if self.quit_job is None:
                self.quit_job = root.after(1500, self._quit_if_idle)

        def _quit_if_idle(self):
            self.quit_job = None
            if (not self.main_visible and not self.runner.busy and not self.deferred
                    and not self.pending and not self.flush_scheduled and self.req_q.empty()):
                root.destroy()
            elif not self.main_visible:
                self.maybe_quit()

        def quick_progress(self, name=""):
            """빠른 실행 작은 창에도 같은 진행 상황을 보여준다"""
            if self.qwin is not None:
                self.q_prog["value"] = self.prog["value"]
                self.q_detail.set(self.detail.get())
                self.q_status.set(name)

        def quick_compress(self, paths):
            fmt, level = self.s["format"], min(self.s["level"], 9)   # 우클릭 압축은 기다리지 않게 최대 9
            if fmt in mf.SINGLE_FORMATS:          # gz/bz2/xz는 파일 하나만 되므로 우클릭 압축에서는 zip으로 대신
                fmt = "zip"
            out = core.unique_file(os.path.join(os.path.dirname(paths[0]) or ".",
                                                quick_name(paths) + mf.format_ext(fmt)))
            junk = self.s["exclude_junk"]
            self.open_quick_window(f"압축 중 · {os.path.basename(out)}")
            self.quick_ctx = {"kind": "compress", "out": out}
            self.job_label = "압축 중…"
            self.runner.start(lambda cb, cancel: core.compress(paths, out, fmt, None, level, cb, cancel, junk))

        def quick_extract(self, archives):
            if not archives:
                self.maybe_quit()
                return
            self.open_quick_window("압축 푸는 중")
            self.quick_ctx = {"kind": "extract", "list": archives, "i": 0, "results": [], "pw": None}
            self._qx_start()

        def _qx_start(self):
            c = self.quick_ctx
            arc = c["list"][c["i"]]
            self.q_title.set(f"푸는 중 ({c['i'] + 1}/{len(c['list'])}) · {os.path.basename(arc)}")
            policy, mac, pw = self.s["policy"], self.s["skip_mac_junk"], c["pw"]
            self.job_label = "푸는 중…"
            self.runner.start(lambda cb, cancel: core.extract_smart(
                arc, os.path.dirname(arc), pw, policy, mac, cb, cancel))

        def quick_event(self, kind, payload):
            r = self.runner
            r.busy = False
            c = self.quick_ctx
            if c is None:                           # 안전장치
                self.handle_job_event(kind, payload)
                return
            if c["kind"] == "compress":
                self.quick_ctx = None
                self.close_quick_window()
                if kind == "done":
                    sk = (payload or {}).get("skipped") or []
                    if sk:
                        messagebox.showwarning(APP_NAME, f"읽을 수 없는 파일 {len(sk)}개를 빼고 압축했습니다.\n"
                                                         f"(다른 프로그램에서 열려 있으면 닫고 다시 시도하세요)\n\n{skip_summary(sk)}")
                    if self.s["open_folder"]:
                        reveal(c["out"])
                elif kind in ("error", "pw"):
                    messagebox.showerror(APP_NAME, f"압축하지 못했습니다.\n\n{payload}")
                self.maybe_quit()
                return
            arc = c["list"][c["i"]]
            if kind == "pw":
                pw = simpledialog.askstring("암호 입력", f"'{os.path.basename(arc)}' 의 암호를 입력하세요.",
                                            parent=self.qwin, show="*")
                if pw:
                    c["pw"] = pw
                    self._qx_start()
                    return
                c["results"].append((arc, False, "암호 입력 안 함"))
            elif kind == "done":
                c["results"].append((arc, True, payload["dest"]))
                for n, why in payload.get("skipped") or []:
                    c.setdefault("skipped", []).append((f"{os.path.basename(arc)} › {n}", why))
            elif kind == "cancel":
                c["results"].append((arc, False, "취소됨"))
                c["i"] = len(c["list"])
            else:
                c["results"].append((arc, False, str(payload)))
            c["i"] += 1
            if c["i"] < len(c["list"]):
                self._qx_start()
                return
            self.quick_ctx = None
            self.close_quick_window()
            bad = [(a, m) for a, ok, m in c["results"] if not ok and m != "취소됨"]
            if bad:
                messagebox.showwarning(APP_NAME, "다음 파일은 풀지 못했습니다.\n\n" + "\n".join(
                    f"• {os.path.basename(a)} : {m}" for a, m in bad[:12]))
            if c.get("skipped"):
                messagebox.showwarning(APP_NAME, f"일부 항목은 안전하지 않거나 겹쳐서 풀지 않았습니다.\n\n"
                                                 f"{skip_summary(c['skipped'])}")
            okd = [m for a, ok, m in c["results"] if ok]
            if okd and self.s["open_folder"]:
                reveal(okd[0] if len(okd) == 1 else os.path.dirname(c["list"][0]))
            self.maybe_quit()

        def cleanup_tmp(self):
            import shutil
            for d in self.open_tmp:
                shutil.rmtree(d, ignore_errors=True)

        def on_close(self):
            if self.runner.busy:
                if not messagebox.askyesno(APP_NAME, "작업이 진행 중입니다. 취소하고 종료할까요?"):
                    return
                self.runner.cancel.set()
            self.cleanup_tmp()
            root.destroy()

    app = App()
    if mode in ("compress", "extract-here"):
        app.main_visible = False
    if mode:
        app.handle_request(mode, paths)
    if on_ready:
        root.after(300, lambda: on_ready(app))
    if not mode:
        root.after(4000, app.maybe_auto_check)
    root.mainloop()
    return 0


def quick_name(paths):
    """압축 파일 이름 자동 제안: 항목이 하나면 그 이름, 여러 개면 부모 폴더 이름"""
    if len(paths) == 1:
        p = paths[0].rstrip("\\/")
        base = os.path.basename(p)
        return (base if os.path.isdir(p) else os.path.splitext(base)[0]) or "압축파일"
    parent = os.path.basename(os.path.dirname(paths[0]).rstrip("\\/"))
    return parent or "압축파일"


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    mode, paths = parse_args(argv)
    srv, token = acquire_server()                   # 무거운 모듈을 불러오기 전에 가장 먼저 자리 선점
    if srv is None:
        if send_request(mode or "show", paths):
            return 0
        return run_app(mode, paths)                  # 실행 중인 쌤ZIP과 연결 실패 → 그냥 새 창으로 계속
    req_q = queue.Queue()
    # 화면을 준비하는 동안에도 다른 프로세스의 요청을 바로 받아 두어야 한다
    threading.Thread(target=serve, args=(srv, token, req_q), daemon=True).start()
    return run_app(mode, paths, req_q)


def _fatal(msg):
    log_error()
    try:
        if sys.platform == "win32":
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                0, f"{msg}\n\n자세한 기록: {os.path.join(settings_dir(), 'error.log')}", APP_NAME, 0x10)
    except Exception:
        pass


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException as e:                       # 창 없는 exe는 오류가 조용히 사라지므로 알려 준다
        _fatal(f"쌤ZIP을 시작하지 못했습니다.\n{e.__class__.__name__}: {e}")
        sys.exit(1)
