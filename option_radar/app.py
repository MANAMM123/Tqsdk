"""期权波动率雷达 · TQSDK 桌面端（专业终端风）

运行：python app.py
"""

from __future__ import annotations

import json
import math
import os
import queue
import re
import threading
import time
import tkinter as tk
from tkinter import filedialog, font as tkfont, messagebox, ttk

import numpy as np
import pandas as pd

try:
    import matplotlib

    matplotlib.use("TkAgg")
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
    from matplotlib.figure import Figure

    HAS_MPL = True
except Exception:  # pragma: no cover
    HAS_MPL = False

try:
    from engine import EXCHANGES, Engine
    IMPORT_ERROR = None
except Exception as _exc:
    EXCHANGES = {"SHFE": "上期所", "DCE": "大商所", "CZCE": "郑商所", "CFFEX": "中金所"}
    Engine = None  # type: ignore[assignment]
    IMPORT_ERROR = _exc

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if getattr(__import__("sys"), "frozen", False):
    BASE_DIR = os.path.dirname(__import__("sys").executable)
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

# ---- 终端风配色 ----
BG = "#0a0e14"
PANEL = "#131a21"
CARD = "#16202a"
CARD2 = "#1c2834"
BORDER = "#263241"
FG = "#e6edf3"
MUTED = "#8b9bb0"
ACCENT = "#f0b429"
CYAN = "#00b8d4"
GREEN = "#00d68f"
RED = "#ff3b3b"
YELLOW = "#ffb000"
PURPLE = "#b388ff"
BLUE = "#4f9dff"

APP_VERSION = "1.2.3"


def _valid(x) -> bool:
    try:
        return math.isfinite(float(x))
    except (TypeError, ValueError):
        return False


def fnum(x, digits: int = 2) -> str:
    if not _valid(x):
        return "—"
    return f"{float(x):,.{digits}f}"


def fint(x) -> str:
    if not _valid(x):
        return "—"
    return f"{int(round(float(x))):,}"


def fvol(x) -> str:
    if not _valid(x):
        return "—"
    return f"{float(x) * 100:.2f}%"


def fpct(x, digits: int = 1, signed: bool = True) -> str:
    if not _valid(x):
        return "—"
    return f"{float(x):+,.{digits}f}%" if signed else f"{float(x):.{digits}f}%"


def readable_contract(symbol: str) -> str:
    """郑商所三位月份补全年份：CZCE.SA701 → SA2701；其它市场原样返回代码。"""
    code = str(symbol or "").split(".")[-1].strip()
    m = re.match(r"^([A-Za-z]+)(\d{3})$", code)
    if not m:
        return code or "—"
    return f"{m.group(1)}{str(2020 + int(m.group(2)[0]))[-2:]}{m.group(2)[1:]}"


def fyi(x) -> str:
    if not _valid(x):
        return "—"
    v = float(x)
    if abs(v) >= 1e8:
        return f"{v / 1e8:+.2f}亿"
    if abs(v) >= 1e4:
        return f"{v / 1e4:+.1f}万"
    return f"{v:+.0f}"


def load_config() -> dict:
    cfg = {"username": "", "password": "", "risk_free_rate": 0.0, "favorites": [], "refresh_sec": 0}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            cfg.update(json.load(fh))
    except Exception:
        pass
    cfg.setdefault("favorites", [])
    cfg.setdefault("refresh_sec", 0)
    return cfg


def save_config(cfg: dict):
    with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except Exception:
        pass


class SettingsDialog(tk.Toplevel):
    def __init__(self, parent, font_name, cfg: dict, on_save):
        super().__init__(parent)
        self.title("快期账户设置")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.on_save = on_save
        self.transient(parent)
        self.grab_set()

        body = tk.Frame(self, bg=BG, padx=24, pady=20)
        body.pack(fill="both", expand=True)
        tk.Label(body, text="TQSDK 登录", bg=BG, fg=FG,
                 font=(font_name, 14, "bold")).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 4))
        tk.Label(body, text="使用快期账户（手机号 + 密码）登录，行情数据由天勤提供。",
                 bg=BG, fg=MUTED, font=(font_name, 9)).grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 14))

        tk.Label(body, text="账号", bg=BG, fg=MUTED, font=(font_name, 10)).grid(row=2, column=0, sticky="w")
        self.user_entry = tk.Entry(body, bg=CARD, fg=FG, insertbackground=FG, relief="flat",
                                   font=(font_name, 11), width=28)
        self.user_entry.grid(row=2, column=1, sticky="ew", padx=(10, 0), ipady=6)
        self.user_entry.insert(0, cfg.get("username", ""))

        tk.Label(body, text="密码", bg=BG, fg=MUTED, font=(font_name, 10)).grid(row=3, column=0, sticky="w", pady=(10, 0))
        self.pwd_entry = tk.Entry(body, bg=CARD, fg=FG, insertbackground=FG, relief="flat",
                                  font=(font_name, 11), width=28, show="*")
        self.pwd_entry.grid(row=3, column=1, sticky="ew", padx=(10, 0), pady=(10, 0), ipady=6)

        tk.Label(body, text="密码仅保存在内存，不写入磁盘。", bg=BG, fg=MUTED,
                 font=(font_name, 8)).grid(row=4, column=0, columnspan=2, sticky="w", pady=(12, 0))

        btns = tk.Frame(body, bg=BG)
        btns.grid(row=5, column=0, columnspan=2, sticky="e", pady=(18, 0))
        tk.Button(btns, text="取消", command=self.destroy, bg=PANEL, fg=FG, relief="flat",
                  font=(font_name, 10), padx=16, pady=5, cursor="hand2").pack(side="left", padx=6)
        tk.Button(btns, text="保存并登录", command=self._save, bg=ACCENT, fg="#111111", relief="flat",
                  font=(font_name, 10, "bold"), padx=16, pady=5, cursor="hand2").pack(side="left")
        self.bind("<Return>", lambda e: self._save())
        self.update_idletasks()
        x = parent.winfo_rootx() + (parent.winfo_width() - self.winfo_width()) // 2
        y = parent.winfo_rooty() + 160
        self.geometry(f"+{max(x, 40)}+{max(y, 40)}")

    def _save(self):
        user = self.user_entry.get().strip()
        pwd = self.pwd_entry.get().strip()
        if not user or not pwd:
            messagebox.showwarning("提示", "账号和密码不能为空。", parent=self)
            return
        self.destroy()
        self.on_save(user, pwd)


class ChartPanel:
    def __init__(self, parent, figsize=(6.0, 3.6)):
        self.frame = tk.Frame(parent, bg=CARD)
        self.figure = None
        self.canvas = None
        if not HAS_MPL:
            tk.Label(self.frame, text="matplotlib 未安装，图表不可用", bg=CARD, fg=MUTED,
                     font=("TkDefaultFont", 10)).pack(fill="both", expand=True)
            return
        self.figure = Figure(figsize=figsize, dpi=100, facecolor=CARD)
        self.canvas = FigureCanvasTkAgg(self.figure, master=self.frame)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)
        self.placeholder("暂无数据")

    def placeholder(self, text):
        if not HAS_MPL:
            return
        self.figure.clear()
        ax = self.figure.add_subplot(111)
        ax.set_facecolor(CARD)
        ax.axis("off")
        ax.text(0.5, 0.5, text, color=MUTED, ha="center", va="center", fontsize=12)
        self.canvas.draw_idle()

    def axes(self):
        if not HAS_MPL:
            return None
        self.figure.clear()
        ax = self.figure.add_subplot(111)
        ax.set_facecolor(CARD)
        for spine in ax.spines.values():
            spine.set_color(BORDER)
        ax.tick_params(colors=MUTED, labelsize=8)
        ax.grid(color=BORDER, alpha=0.4, linestyle="--", linewidth=0.5)
        return ax

    def draw(self, tight: bool = True):
        if not HAS_MPL:
            return
        if tight:
            self.figure.tight_layout()
        self.canvas.draw_idle()

    def save_png(self, path):
        if not HAS_MPL or self.figure is None:
            return
        self.figure.savefig(path, dpi=150, facecolor=CARD, bbox_inches="tight")


PAGES = ["概览", "期权链", "GEX分析", "偏度", "波动率锥", "历史", "跨市场"]


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.font_name = self._pick_font(root)
        self.mono_font = self._pick_mono(root)
        self.cfg = load_config()
        self.engine: Engine | None = None
        self.ui_queue: queue.Queue = queue.Queue()
        self.pending = 0
        self.connected = False
        self.exchange = "SHFE"
        self.products: list[dict] = []
        self.chain_result: dict | None = None
        self.df_overview: pd.DataFrame | None = None
        self.df_chain: pd.DataFrame | None = None
        self.df_gex: pd.DataFrame | None = None
        self.df_skew: pd.DataFrame | None = None
        self.df_cross: pd.DataFrame | None = None
        self.df_history: pd.DataFrame | None = None
        self._nav_buttons: dict[str, tk.Button] = {}
        self._current_page = "概览"
        self._dirty_pages: set[str] = set()
        self._refresh_job = None
        self._last_refresh = ""
        self._action_widgets: list[tk.Widget] = []
        # 进场信号：ATM IV 触底回升监测
        self.sound_on = bool(self.cfg.get("sound", True))
        self._iv_track: dict[str, list[tuple[float, float]]] = {}
        self._signal_ts: dict[str, float] = {}
        self._signal_info: dict[str, dict] = {}

        self._setup_window()
        self._setup_style()
        self._build_ui()
        self._start_engine()
        self.root.after(150, self._poll)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------------------------------------------------------------- setup
    def _pick_font(self, root) -> str:
        families = set(tkfont.families(root))
        for name in ("Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC",
                     "Source Han Sans SC", "SimHei", "WenQuanYi Micro Hei", "DejaVu Sans"):
            if name in families:
                return name
        return "TkDefaultFont"

    def _pick_mono(self, root) -> str:
        families = set(tkfont.families(root))
        for name in ("Consolas", "Cascadia Mono", "JetBrains Mono", "Source Code Pro",
                     "Menlo", "DejaVu Sans Mono", "Courier New"):
            if name in families:
                return name
        return "TkDefaultFont"

    def _setup_window(self):
        self.root.title(f"期权波动率雷达 · TQSDK v{APP_VERSION}")
        self.root.geometry("1500x920")
        self.root.minsize(1280, 760)
        self.root.configure(bg=BG)
        if HAS_MPL:
            matplotlib.rcParams["font.sans-serif"] = [self.font_name, "DejaVu Sans"]
            matplotlib.rcParams["axes.unicode_minus"] = False

    def _setup_style(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure(".", background=BG, foreground=FG, fieldbackground=CARD, bordercolor=BORDER,
                        font=(self.font_name, 10))
        style.configure("TFrame", background=BG)
        style.configure("Panel.TFrame", background=PANEL)
        style.configure("Card.TFrame", background=CARD)
        style.configure("TLabel", background=BG, foreground=FG)
        style.configure("Muted.TLabel", background=BG, foreground=MUTED, font=(self.font_name, 9))
        style.configure("Card.TLabel", background=CARD, foreground=FG, font=(self.mono_font, 9))
        style.configure("CardMuted.TLabel", background=CARD, foreground=MUTED, font=(self.mono_font, 8))
        style.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(0, 0, 0, 0))
        style.configure("TNotebook.Tab", background=PANEL, foreground=MUTED, padding=(14, 7),
                        font=(self.font_name, 9, "bold"))
        style.map("TNotebook.Tab", background=[("selected", CARD)], foreground=[("selected", ACCENT)])
        style.configure("Treeview", background=CARD, fieldbackground=CARD, foreground=FG, rowheight=22,
                        borderwidth=0, font=(self.mono_font, 9))
        style.configure("Treeview.Heading", background=PANEL, foreground=MUTED, relief="flat",
                        font=(self.mono_font, 8, "bold"))
        style.map("Treeview.Heading", background=[("active", BORDER)])
        style.map("Treeview", background=[("selected", BORDER)], foreground=[("selected", FG)])
        style.configure("TCombobox", fieldbackground=CARD, background=PANEL, foreground=FG,
                        lightcolor=BORDER, darkcolor=BORDER, arrowcolor=FG)
        style.map("TCombobox", fieldbackground=[("readonly", CARD)])
        style.configure("Horizontal.TProgressbar", background=ACCENT, troughcolor=PANEL, bordercolor=BORDER,
                        lightcolor=ACCENT, darkcolor=ACCENT)
        style.configure("TSeparator", background=BORDER)

    # ---------------------------------------------------------------- UI
    def _build_ui(self):
        self._build_toolbar()
        main = tk.Frame(self.root, bg=BG)
        main.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        self._build_sidebar(main)
        self._build_content(main)

    def _build_toolbar(self):
        bar = tk.Frame(self.root, bg=PANEL, height=54)
        bar.pack(fill="x", padx=6, pady=(6, 0))
        bar.pack_propagate(False)

        left = tk.Frame(bar, bg=PANEL)
        left.pack(side="left", fill="y", padx=10)
        tk.Label(left, text="期权波动率雷达", bg=PANEL, fg=ACCENT,
                 font=(self.font_name, 13, "bold")).pack(side="left")
        tk.Label(left, text=f"v{APP_VERSION}", bg=PANEL, fg=MUTED,
                 font=(self.mono_font, 9)).pack(side="left", padx=(6, 0))
        self.status_lbl = tk.Label(left, text="未连接", bg=PANEL, fg=RED,
                                   font=(self.font_name, 10))
        self.status_lbl.pack(side="left", padx=(12, 0))

        # 交易所按钮
        exbar = tk.Frame(bar, bg=PANEL)
        exbar.pack(side="left", padx=10)
        self.ex_buttons: dict[str, tk.Button] = {}
        for ex, name in EXCHANGES.items():
            b = tk.Button(exbar, text=name, command=lambda e=ex: self._switch_exchange(e),
                          bg=PANEL, fg=MUTED, relief="flat", activebackground=CARD, activeforeground=FG,
                          font=(self.font_name, 10), padx=10, pady=4, cursor="hand2")
            b.pack(side="left", padx=2)
            self.ex_buttons[ex] = b

        # 品种/到期
        sel = tk.Frame(bar, bg=PANEL)
        sel.pack(side="left", padx=10)
        tk.Label(sel, text="品种", bg=PANEL, fg=MUTED, font=(self.font_name, 9)).pack(side="left")
        self.product_combo = ttk.Combobox(sel, state="readonly", width=10, font=(self.mono_font, 10))
        self.product_combo.pack(side="left", padx=4)
        self.product_combo.bind("<<ComboboxSelected>>", lambda e: self._on_product_change())
        tk.Label(sel, text="到期", bg=PANEL, fg=MUTED, font=(self.font_name, 9)).pack(side="left", padx=(10, 0))
        self.expiry_combo = ttk.Combobox(sel, state="readonly", width=10, font=(self.mono_font, 10))
        self.expiry_combo.pack(side="left", padx=4)
        self.expiry_combo.bind("<<ComboboxSelected>>", lambda e: self._on_expiry_change())

        # 刷新间隔
        tk.Label(bar, text="自动刷新", bg=PANEL, fg=MUTED, font=(self.font_name, 9)).pack(side="left", padx=(10, 0))
        self.refresh_combo = ttk.Combobox(bar, values=["关", "5s", "10s", "15s", "30s"], width=5, state="readonly")
        self.refresh_combo.set("关" if self.cfg.get("refresh_sec", 0) == 0 else f"{self.cfg['refresh_sec']}s")
        self.refresh_combo.pack(side="left", padx=4)
        self.refresh_combo.bind("<<ComboboxSelected>>", lambda e: self._on_refresh_interval_change())

        self.refresh_btn = self._mk_btn(bar, "刷新", self._on_refresh, CYAN)
        self.refresh_btn.pack(side="left", padx=4)
        self._action_widgets.append(self.refresh_btn)

        self.fav_btn = self._mk_btn(bar, "★ 加自选", self._add_favorite, YELLOW, "#111")
        self.fav_btn.pack(side="left", padx=4)

        self.time_lbl = tk.Label(bar, text="", bg=PANEL, fg=MUTED, font=(self.mono_font, 9))
        self.time_lbl.pack(side="left", padx=8)

        right = tk.Frame(bar, bg=PANEL)
        right.pack(side="right", padx=10)
        self.alert_lbl = tk.Label(right, text="", bg=PANEL, fg=YELLOW, font=(self.font_name, 9, "bold"))
        self.alert_lbl.pack(side="right", padx=8)
        self.sound_btn = self._mk_btn(right, "信号声:开" if self.sound_on else "信号声:关",
                                      self._toggle_sound, CARD2)
        self.sound_btn.pack(side="right", padx=4)
        self.settings_btn = self._mk_btn(right, "账户", self._open_settings, CARD2)
        self.settings_btn.pack(side="right", padx=4)

    def _mk_btn(self, parent, text, cmd, color=PANEL, fg_color=FG) -> tk.Button:
        b = tk.Button(parent, text=text, command=cmd, bg=color, fg=fg_color, relief="flat",
                      activebackground=BORDER, activeforeground=FG, font=(self.font_name, 9),
                      padx=12, pady=5, cursor="hand2")
        return b

    def _build_sidebar(self, parent):
        side = tk.Frame(parent, bg=PANEL, width=150)
        side.pack(side="left", fill="y")
        side.pack_propagate(False)

        tk.Label(side, text="导航", bg=PANEL, fg=MUTED, font=(self.font_name, 9, "bold")).pack(
            anchor="w", padx=12, pady=(12, 4))
        for name in PAGES:
            b = tk.Button(side, text="  " + name, command=lambda n=name: self._show_page(n),
                          bg=PANEL, fg=FG, relief="flat", anchor="w", activebackground=CARD, activeforeground=ACCENT,
                          font=(self.font_name, 10), padx=14, pady=8, cursor="hand2")
            b.pack(fill="x")
            self._nav_buttons[name] = b

        ttk.Separator(side, orient="horizontal").pack(fill="x", padx=8, pady=8)
        tk.Label(side, text="自选", bg=PANEL, fg=MUTED, font=(self.font_name, 9, "bold")).pack(
            anchor="w", padx=12, pady=(0, 4))
        self.fav_frame = tk.Frame(side, bg=PANEL)
        self.fav_frame.pack(fill="x", padx=4)
        self._refresh_favorites()

    def _build_content(self, parent):
        self.notebook = ttk.Notebook(parent)
        self.notebook.pack(fill="both", expand=True, padx=(6, 0))
        # 取消 notebook tab 显示，用侧边栏切换
        self.notebook.configure(padding=0)

        self._page_overview = tk.Frame(self.notebook, bg=BG)
        self._page_chain = tk.Frame(self.notebook, bg=BG)
        self._page_gex = tk.Frame(self.notebook, bg=BG)
        self._page_skew = tk.Frame(self.notebook, bg=BG)
        self._page_cone = tk.Frame(self.notebook, bg=BG)
        self._page_history = tk.Frame(self.notebook, bg=BG)
        self._page_cross = tk.Frame(self.notebook, bg=BG)

        for name, page in zip(PAGES, [self._page_overview, self._page_chain, self._page_gex,
                                      self._page_skew, self._page_cone, self._page_history, self._page_cross]):
            self.notebook.add(page, text=name)

        self._build_overview(self._page_overview)
        self._build_chain(self._page_chain)
        self._build_gex(self._page_gex)
        self._build_skew(self._page_skew)
        self._build_cone(self._page_cone)
        self._build_history(self._page_history)
        self._build_cross(self._page_cross)
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)
        self._show_page("概览")

    # ---- 概览 ----
    def _build_overview(self, parent):
        top = tk.Frame(parent, bg=BG)
        top.pack(fill="x", padx=10, pady=8)
        self._mk_btn(top, "扫描全品种", self._on_scan, ACCENT, "#111").pack(side="left")
        self._mk_btn(top, "导出 CSV", self._on_export_overview).pack(side="left", padx=6)
        tk.Label(top, text="双击行跳转期权链", bg=BG, fg=MUTED, font=(self.font_name, 9)).pack(side="left", padx=12)

        body = tk.Frame(parent, bg=BG)
        body.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        cols = ["gex_rank", "product", "expiry", "days", "F", "atm_iv", "hv20", "iv_hv_pct",
                "skew_pct", "net_gex", "zero_gamma", "pcr_oi", "iv_percentile"]
        self.overview_tree = self._mk_tree(body, cols, widths=[55, 60, 55, 45, 80, 65, 65, 80, 80, 95, 80, 70, 75],
                                           titles=["排名", "品种", "到期", "天数", "标的F", "ATM IV", "HV20", "IV-HV%",
                                                   "偏度%", "净GEX", "零伽马", "PCR%", "IV分位%"])
        self.overview_tree.bind("<Double-1>", self._on_overview_double)

    # ---- 期权链 ----
    def _build_chain(self, parent):
        top = tk.Frame(parent, bg=BG)
        top.pack(fill="x", padx=10, pady=8)
        self._mk_btn(top, "查询期权链", self._on_query_chain, ACCENT, "#111").pack(side="left")
        self._mk_btn(top, "导出 CSV", self._on_export_chain).pack(side="left", padx=6)
        self.chain_info = tk.Label(top, text="", bg=BG, fg=MUTED, font=(self.mono_font, 9))
        self.chain_info.pack(side="left", padx=12)

        body = tk.Frame(parent, bg=BG)
        body.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        cols = ["strike", "option_class", "price", "iv", "delta", "gamma", "vanna", "vega",
                "open_interest", "gex", "gex_share_pct"]
        self.chain_tree = self._mk_tree(body, cols, widths=[70, 60, 80, 70, 70, 80, 80, 70, 90, 95, 80],
                                        titles=["行权价", "类型", "价格", "IV", "Delta", "Gamma", "Vanna", "Vega",
                                                "持仓量", "GEX", "GEX占比%"])
        self.chain_tree.tag_configure("CALL", foreground=GREEN)
        self.chain_tree.tag_configure("PUT", foreground=RED)

    # ---- GEX 仪表盘 ----
    def _build_gex(self, parent):
        top = tk.Frame(parent, bg=BG)
        top.pack(fill="x", padx=10, pady=8)
        self._mk_btn(top, "查询GEX", self._on_query_chain, ACCENT, "#111").pack(side="left")
        self._mk_btn(top, "导出图片", lambda: self._export_figure(self.gex_chart, "gex")).pack(side="left", padx=6)
        self.gex_info = tk.Label(top, text="", bg=BG, fg=MUTED, font=(self.mono_font, 9))
        self.gex_info.pack(side="left", padx=12)

        # 7 个 KPI 卡（对齐参考图：现价 / Net GEX / Gamma Flip / Max Pain / 两墙 / ATM IV）
        kpi = tk.Frame(parent, bg=BG)
        kpi.pack(fill="x", padx=10)
        self.kpi_cards: dict[str, dict] = {}
        for key in ["price", "net_gex", "gamma_flip", "max_pain", "call_wall", "put_wall", "atm_iv"]:
            self.kpi_cards[key] = self._make_kpi_card(kpi, key)

        # 读图结论：区域 → 距墙 → Flip 三步读数
        concl = tk.Frame(parent, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        concl.pack(fill="x", padx=10, pady=(8, 0))
        head = tk.Frame(concl, bg=CARD)
        head.pack(fill="x", padx=8, pady=(4, 0))
        tk.Label(head, text="读图结论", bg=CARD, fg=ACCENT,
                 font=(self.font_name, 9, "bold")).pack(side="left")
        tk.Label(head, text="先看区域 → 再看距墙 → 最后盯 Gamma Flip", bg=CARD, fg=MUTED,
                 font=(self.font_name, 8)).pack(side="left", padx=8)
        self.gex_conclusion = tk.Label(
            concl, text="查询后显示：① 区域 ② 距墙距离 ③ Gamma Flip",
            bg=CARD, fg=FG, font=(self.font_name, 9), justify="left", anchor="w",
            wraplength=1000)
        self.gex_conclusion.pack(fill="x", padx=8, pady=(2, 6))

        # GEX 主图
        self.gex_chart = ChartPanel(parent, figsize=(7.2, 4.0))
        self.gex_chart.frame.pack(fill="both", expand=True, padx=10, pady=8)

        # PCR 日内变化
        pcr_wrap = tk.Frame(parent, bg=BG)
        pcr_wrap.pack(fill="x", padx=10, pady=(6, 0))
        tk.Label(pcr_wrap, text="PCR 日内变化（Put OI / Call OI）", bg=BG, fg=MUTED,
                 font=(self.font_name, 8, "bold")).pack(anchor="w")
        self.pcr_chart = ChartPanel(pcr_wrap, figsize=(7.2, 1.3))
        self.pcr_chart.frame.pack(fill="x")

        # 明细表
        body = tk.Frame(parent, bg=BG)
        body.pack(fill="both", expand=False, padx=10, pady=(0, 10))
        cols = ["strike", "call_gex", "put_gex", "net_gex", "oi", "share_pct", "cum_share_pct", "cum_net_gex"]
        self.gex_tree = self._mk_tree(body, cols, widths=[70, 90, 90, 90, 80, 70, 80, 95],
                                      titles=["行权价", "Call GEX", "Put GEX", "净GEX", "持仓", "占比%", "累计占比%", "累计净GEX"])
        self.gex_tree.bind("<Double-1>", self._on_gex_double)

    def _make_kpi_card(self, parent, key: str) -> dict:
        colors = {
            "price": (ACCENT, "最新价 F"),
            "net_gex": (CYAN, "NET GEX"),
            "gamma_flip": (YELLOW, "GAMMA FLIP"),
            "max_pain": (BLUE, "MAX PAIN"),
            "call_wall": (GREEN, "CALL WALL"),
            "put_wall": (RED, "PUT WALL"),
            "atm_iv": (PURPLE, "ATM IV"),
        }
        color, label = colors[key]
        card = tk.Frame(parent, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        card.pack(side="left", expand=True, fill="x", padx=2)
        tk.Label(card, text=label, bg=CARD, fg=MUTED, font=(self.font_name, 8)).pack(anchor="w", padx=7, pady=(4, 0))
        val = tk.Label(card, text="—", bg=CARD, fg=color, font=(self.mono_font, 13, "bold"))
        val.pack(anchor="w", padx=7, pady=(0, 0))
        sub = tk.Label(card, text="", bg=CARD, fg=MUTED, font=(self.mono_font, 8))
        sub.pack(anchor="w", padx=7, pady=(0, 4))
        return {"card": card, "val": val, "sub": sub, "color": color}

    # ---- 偏度 ----
    def _build_skew(self, parent):
        top = tk.Frame(parent, bg=BG)
        top.pack(fill="x", padx=10, pady=8)
        self._mk_btn(top, "期限结构", self._on_term_structure, ACCENT, "#111").pack(side="left")
        self._mk_btn(top, "导出图片", lambda: self._export_figure(self.skew_chart, "skew")).pack(side="left", padx=6)
        self.skew_summary = tk.Label(top, text="", bg=BG, fg=MUTED, font=(self.mono_font, 9), justify="left")
        self.skew_summary.pack(side="left", padx=12)

        self.skew_chart = ChartPanel(parent, figsize=(7.2, 3.2))
        self.skew_chart.frame.pack(fill="x", padx=10, pady=8)

        body = tk.Frame(parent, bg=BG)
        body.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        cols = ["expiry", "days", "F", "atm_iv", "hv20", "iv_hv_pct", "skew_25d", "skew_pct", "net_gex"]
        self.skew_tree = self._mk_tree(body, cols, widths=[55, 45, 80, 65, 65, 80, 80, 70, 90],
                                       titles=["到期", "天数", "F", "ATM IV", "HV20", "IV-HV%", "25Δ偏度", "偏度%", "净GEX"])

    # ---- 波动率锥 ----
    def _build_cone(self, parent):
        top = tk.Frame(parent, bg=BG)
        top.pack(fill="x", padx=10, pady=8)
        self._mk_btn(top, "刷新锥", self._on_query_chain, ACCENT, "#111").pack(side="left")
        self._mk_btn(top, "导出图片", lambda: self._export_figure(self.cone_chart, "cone")).pack(side="left", padx=6)
        self.cone_info = tk.Label(top, text="", bg=BG, fg=MUTED, font=(self.mono_font, 9))
        self.cone_info.pack(side="left", padx=12)

        self.cone_chart = ChartPanel(parent, figsize=(7.2, 3.6))
        self.cone_chart.frame.pack(fill="both", expand=True, padx=10, pady=8)

        body = tk.Frame(parent, bg=BG)
        body.pack(fill="both", expand=False, padx=10, pady=(0, 10))
        cols = ["window", "mean", "p10", "p25", "p50", "p75", "p90", "current"]
        self.cone_tree = self._mk_tree(body, cols, widths=[60, 70, 70, 70, 70, 70, 70, 70],
                                       titles=["窗口", "均值", "P10", "P25", "P50", "P75", "P90", "当前HV"])

    # ---- 历史 ----
    def _build_history(self, parent):
        top = tk.Frame(parent, bg=BG)
        top.pack(fill="x", padx=10, pady=8)
        self._mk_btn(top, "刷新历史", self._on_query_chain, ACCENT, "#111").pack(side="left")
        self._mk_btn(top, "导出图片", lambda: self._export_figure(self.history_chart, "history")).pack(side="left", padx=6)
        tk.Label(top, text="Call Wall / Put Wall / 零伽马位 历史轨迹（本地记录，每日一条）",
                 bg=BG, fg=MUTED, font=(self.font_name, 9)).pack(side="left", padx=12)

        self.history_chart = ChartPanel(parent, figsize=(7.2, 3.6))
        self.history_chart.frame.pack(fill="both", expand=True, padx=10, pady=8)

        body = tk.Frame(parent, bg=BG)
        body.pack(fill="both", expand=False, padx=10, pady=(0, 10))
        cols = ["date", "F", "call_wall", "put_wall", "gamma_wall", "vol_trigger", "zero_gamma", "atm_iv", "pcr_oi"]
        self.history_tree = self._mk_tree(body, cols, widths=[90, 80, 80, 80, 80, 80, 80, 65, 65],
                                          titles=["日期", "F", "Call Wall", "Put Wall", "Gamma Wall",
                                                  "Vol Trigger", "零伽马", "ATM IV", "PCR%"])

    # ---- 跨市场 ----
    def _build_cross(self, parent):
        top = tk.Frame(parent, bg=BG)
        top.pack(fill="x", padx=10, pady=8)
        self._mk_btn(top, "扫描四大交易所", self._on_cross_compare, ACCENT, "#111").pack(side="left")
        tk.Label(top, text="过滤品种", bg=BG, fg=MUTED, font=(self.font_name, 9)).pack(side="left", padx=(14, 4))
        self.cross_filter = tk.Entry(top, bg=CARD, fg=FG, insertbackground=FG, relief="flat",
                                     font=(self.mono_font, 10), width=14)
        self.cross_filter.pack(side="left", ipady=4)
        self.cross_filter.bind("<Return>", lambda e: self._on_cross_compare())
        self._mk_btn(top, "导出 CSV", self._on_export_cross).pack(side="left", padx=6)

        body = tk.Frame(parent, bg=BG)
        body.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        cols = ["gex_rank_all", "exchange_name", "product", "expiry", "days", "F", "atm_iv",
                "iv_hv_pct", "net_gex", "pcr_oi"]
        self.cross_tree = self._mk_tree(body, cols, widths=[60, 65, 60, 55, 45, 80, 65, 80, 95, 65],
                                        titles=["全市场排名", "交易所", "品种", "到期", "天数", "F", "ATM IV",
                                                "IV-HV%", "净GEX", "PCR%"])

    # ---------------------------------------------------------------- helpers
    def _mk_tree(self, parent, cols, widths, titles) -> ttk.Treeview:
        wrap = tk.Frame(parent, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        wrap.pack(fill="both", expand=True)
        vsb = ttk.Scrollbar(wrap, orient="vertical")
        hsb = ttk.Scrollbar(wrap, orient="horizontal")
        tree = ttk.Treeview(wrap, columns=cols, show="headings",
                            yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        vsb.configure(command=tree.yview)
        hsb.configure(command=tree.xview)
        for c, w, t in zip(cols, widths, titles):
            tree.heading(c, text=t)
            tree.column(c, width=w, anchor="center", stretch=False)
        tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        hsb.pack(side="bottom", fill="x")
        return tree

    def _show_page(self, name: str):
        self._current_page = name
        try:
            idx = PAGES.index(name)
            self.notebook.select(idx)
        except Exception:
            pass
        self._sync_nav(name)
        # 切换时补画标记为脏的页面
        if name in self._dirty_pages:
            self._fill_page(name)

    def _on_tab_changed(self, _event=None):
        """用户直接点 notebook 标签时同步当前页。"""
        try:
            name = PAGES[self.notebook.index("current")]
        except Exception:
            return
        if name == self._current_page:
            return
        self._current_page = name
        self._sync_nav(name)
        if name in self._dirty_pages:
            self._fill_page(name)

    def _sync_nav(self, name: str):
        for n, b in self._nav_buttons.items():
            b.configure(bg=ACCENT if n == name else PANEL, fg="#111" if n == name else FG)

    # ---------------------------------------------------------------- engine
    def _start_engine(self):
        if Engine is None:
            self._set_status(f"导入失败：{IMPORT_ERROR}", RED)
            return
        self.engine = Engine(self.cfg, self._notify)
        self.engine.start()
        if self.cfg.get("username") and os.environ.get("TQ_PASSWORD"):
            self._submit(self.engine.connect, self.cfg["username"], os.environ["TQ_PASSWORD"],
                         callback=self._after_connect)
        else:
            self.root.after(200, self._open_settings)

    def _open_settings(self):
        SettingsDialog(self.root, self.font_name, self.cfg, self._on_login)

    def _on_login(self, user, pwd):
        self.cfg["username"] = user
        save_config(self.cfg)
        if self.engine is None:
            self.engine = Engine(self.cfg, self._notify)
            self.engine.start()
        self._submit(self.engine.connect, user, pwd, callback=self._after_connect)

    def _after_connect(self, _fut):
        self._set_status(f"已登录：{Engine._mask(self.cfg.get('username', ''))}", CYAN)
        self.connected = True
        self._switch_exchange(self.exchange)

    def _switch_exchange(self, ex: str):
        for e, b in self.ex_buttons.items():
            b.configure(bg=ACCENT if e == ex else PANEL, fg="#111" if e == ex else FG)
        self.exchange = ex
        self._submit(self.engine.load_products, ex, False, callback=self._after_products)

    def _after_products(self, fut):
        res = fut.result()
        self.products = res["products"]
        self.product_combo["values"] = [p["product"] for p in self.products]
        if self.products:
            self.product_combo.set(self.products[0]["product"])
            self._on_product_change()
        self._set_status(f"{EXCHANGES.get(self.exchange)}：{len(self.products)} 个品种", CYAN)

    def _on_product_change(self):
        prod = self.product_combo.get()
        p = next((x for x in self.products if x["product"] == prod), None)
        if p is None:
            return
        self.expiry_combo["values"] = [f"{e['key']} ({e['days']}d)" for e in p["expiries"]]
        if p["expiries"]:
            self.expiry_combo.current(0)

    def _on_expiry_change(self):
        pass

    def _selected(self):
        prod = self.product_combo.get()
        exp_text = self.expiry_combo.get()
        key = exp_text.split(" ")[0] if exp_text else ""
        return prod, key

    def _on_refresh(self):
        page = self._current_page
        if page == "跨市场":
            self._on_cross_compare()
        elif page == "概览":
            self._on_scan()
        else:
            self._on_query_chain()

    def _on_refresh_interval_change(self):
        val = self.refresh_combo.get()
        sec = 0 if val == "关" else int(val.rstrip("s"))
        self.cfg["refresh_sec"] = sec
        save_config(self.cfg)
        if self._refresh_job is not None:
            self.root.after_cancel(self._refresh_job)
            self._refresh_job = None
        if sec > 0:
            self._schedule_refresh(sec)

    def _schedule_refresh(self, sec):
        self._refresh_job = self.root.after(sec * 1000, self._auto_refresh)

    def _auto_refresh(self):
        if not self.connected:
            sec = self.cfg.get("refresh_sec", 0)
            if sec > 0:
                self._schedule_refresh(sec)
            return
        page = self._current_page
        if page == "跨市场":
            self._on_cross_compare()
        elif page == "概览":
            self._on_scan()
        elif page in ("期权链", "GEX分析", "偏度", "波动率锥", "历史"):
            self._on_query_chain(silent=True)
        sec = self.cfg.get("refresh_sec", 0)
        if sec > 0:
            self._schedule_refresh(sec)

    def _on_query_chain(self, silent=False):
        prod, key = self._selected()
        if not prod or not key:
            if not silent:
                messagebox.showinfo("提示", "请先选择品种与到期日。")
            return
        self._submit(self.engine.query_chain, self.exchange, prod, key,
                     callback=lambda f: self._after_chain(f, silent))

    def _after_chain(self, fut, silent=False):
        try:
            self.chain_result = fut.result()
        except Exception as exc:  # noqa: BLE001
            if not silent:
                messagebox.showerror("查询失败", str(exc))
            return
        self._last_refresh = self._now()
        self.time_lbl.configure(text=f"更新 {self._last_refresh}")
        self._check_entry_signal()
        # 只重绘当前可见页，其余页面标脏、切换时再画（降低刷新开销）
        self._dirty_pages = {"期权链", "GEX分析", "偏度", "波动率锥", "历史"}
        self._fill_page(self._current_page)
        self._check_alerts()

    # ---------------------------------------------------------------- entry signal
    def _toggle_sound(self):
        self.sound_on = not self.sound_on
        self.sound_btn.configure(text="信号声:开" if self.sound_on else "信号声:关",
                                 fg=FG if self.sound_on else MUTED)
        self.cfg["sound"] = self.sound_on
        save_config(self.cfg)
        if self.sound_on:
            self._play_entry_sound()

    def _play_entry_sound(self):
        """标准进场提示音：三声上行（880→1175→1568Hz）。"""
        if not self.sound_on:
            return

        def _run():
            try:
                import winsound  # Windows
                for freq, dur in ((880, 120), (0, 60), (1175, 120), (0, 60), (1568, 220)):
                    if freq:
                        winsound.Beep(freq, dur)
                    else:
                        time.sleep(dur / 1000.0)
                return
            except Exception:
                pass
            try:
                self.root.bell()
                time.sleep(0.15)
                self.root.bell()
            except Exception:
                pass

        threading.Thread(target=_run, daemon=True).start()

    def _check_entry_signal(self):
        """第③步（Gamma Flip）之后：ATM IV 由跌转升，触发标准进场信号。"""
        if not self.chain_result:
            return
        r = self.chain_result
        iv = r["metrics"].get("atm_iv")
        key = f"{r['exchange']}.{r['product']}.{r['expiry']}"
        now = time.time()
        samples = self._iv_track.setdefault(key, [])
        if _valid(iv):
            samples.append((now, float(iv)))
            if len(samples) > 80:
                del samples[:-80]

        info = {"trigger": False, "active": False, "text": ""}
        if _valid(iv):
            if len(samples) >= 4:
                window = [v for _, v in samples[-11:-1]]  # 最近 10 次（不含当前）
                low = min(window)
                need = max(0.005, 0.03 * low)            # 至少 0.5 个波动点或 3%
                rebound = float(iv) - low
                rising = samples[-1][1] > samples[-2][1]
                prior_fall = (max(window) - low) > 0.002  # 之前确实跌过，才算"再次上升"
                if rising and rebound >= need and prior_fall:
                    info = {"trigger": True, "active": True,
                            "text": (f"④ 信号：ATM IV {fvol(iv)} 自近 10 次低点 {fvol(low)} "
                                     f"回升 +{rebound * 100:.2f} 个点（+{rebound / low * 100:.1f}%），"
                                     f"进场信号已触发并响铃。")}
                    if now - self._signal_ts.get(key, 0.0) >= 180:  # 3 分钟冷却
                        self._signal_ts[key] = now
                        self._play_entry_sound()
                    else:
                        info["text"] = (f"④ 信号持续：ATM IV {fvol(iv)} 已自低点回升 "
                                        f"{rebound * 100:+.2f} 个点，冷却中不重复响铃。")
                else:
                    info = {"trigger": False, "active": False,
                            "text": (f"④ 等待：ATM IV {fvol(iv)}，较近 10 次低点 {fvol(low)} "
                                     f"回升 {rebound * 100:+.2f} 个点，再升 {max(0.0, need - rebound) * 100:.2f} "
                                     f"个点触发进场信号。")}
            else:
                info = {"trigger": False, "active": False,
                        "text": f"④ 等待：ATM IV {fvol(iv)}，样本 {len(samples)}/4，继续刷新后开始监测。"}
        self._signal_info[key] = info

    def _fill_page(self, name: str):
        if not self.chain_result:
            return
        fillers = {
            "期权链": self._fill_chain,
            "GEX分析": self._fill_gex,
            "偏度": self._fill_skew,
            "波动率锥": self._fill_cone,
            "历史": self._fill_history,
        }
        fn = fillers.get(name)
        if fn is None:
            return
        fn()
        self._dirty_pages.discard(name)

    def _on_scan(self):
        self._submit(self.engine.scan_exchange, self.exchange, callback=self._after_scan)

    def _after_scan(self, fut):
        try:
            self.df_overview = fut.result()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("扫描失败", str(exc))
            return
        self._fill_overview()

    def _on_cross_compare(self):
        kw = self.cross_filter.get().strip()
        self._submit(self.engine.cross_exchange_compare, kw, callback=self._after_cross)

    def _after_cross(self, fut):
        try:
            self.df_cross = fut.result()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("跨市场对比失败", str(exc))
            return
        self._fill_cross()

    def _on_term_structure(self):
        prod, _ = self._selected()
        if not prod:
            return
        self._submit(self.engine.term_structure, self.exchange, prod, callback=self._after_term)

    def _after_term(self, fut):
        try:
            self.df_skew = fut.result()
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("期限结构失败", str(exc))
            return
        self._fill_skew_term()

    # ---------------------------------------------------------------- fill
    def _fill_overview(self):
        t = self.overview_tree
        for i in t.get_children():
            t.delete(i)
        if self.df_overview is None or self.df_overview.empty:
            return
        for r in self.df_overview.itertuples(index=False):
            t.insert("", "end", values=(
                fint(r.gex_rank), r.product, r.expiry, fint(r.days), fnum(r.F, 1),
                fvol(r.atm_iv), fvol(r.hv20), fpct(r.iv_hv_pct), fpct(r.skew_pct),
                fyi(r.net_gex), fnum(r.zero_gamma, 1), fpct(r.pcr_oi), fpct(r.iv_percentile, signed=False),
            ))

    def _fill_chain(self):
        if not self.chain_result:
            return
        r = self.chain_result
        df = r["chain"]
        info = (f"{r['exchange']}.{r['product']} {r['expiry']} | F={fnum(r['F'])} | "
                f"IV={fvol(r['metrics']['atm_iv'])} | HV20={fvol(r['metrics']['hv20'])} | 合约={r['metrics']['contracts']}")
        self.chain_info.configure(text=info)
        t = self.chain_tree
        for i in t.get_children():
            t.delete(i)
        if df is None or df.empty:
            return
        for row in df.itertuples(index=False):
            cp = str(row.option_class).upper()
            tag = "CALL" if cp.startswith("C") else "PUT"
            t.insert("", "end", tags=(tag,), values=(
                fnum(row.strike, 1), cp, fnum(row.price), fvol(row.iv), fpct(row.delta, signed=False),
                fnum(row.gamma, 5), fnum(row.vanna, 5), fnum(row.vega, 4),
                fint(row.open_interest), fyi(row.gex), fpct(row.gex_share_pct, signed=False),
            ))

    def _fill_gex(self):
        if not self.chain_result:
            return
        r = self.chain_result
        gex = r["gex"]
        m = r["metrics"]
        F = r["F"]
        spot_name = readable_contract(r["spot_symbol"]) if r["spot_symbol"] not in ("—", "平价隐含") else r["spot_symbol"]
        self.gex_info.configure(
            text=(f"{r['exchange']}.{r['product']} 期权到期 {r['expiry']} · 标的 {spot_name}"
                  f" | {r['days']}天 | 合约 {m['contracts']}"))

        # KPI（7 卡，对齐参考图）
        self._set_kpi("price", fnum(F, 1), f"标的 {spot_name} 期货" if spot_name not in ("—", "平价隐含") else f"标的 {spot_name}")
        self._set_kpi("net_gex", fyi(m["net_gex"]),
                      f"Call {fyi(m['call_gex'])} / Put {fyi(m['put_gex'])}")
        self._set_kpi("gamma_flip", fnum(m["zero_gamma"], 1),
                      fpct(m["key_levels"]["zero_gamma"]["dist_pct"]))
        self._set_kpi("max_pain", fnum(m.get("max_pain"), 1),
                      fpct(m["key_levels"]["max_pain"]["dist_pct"]))
        self._set_kpi("call_wall", fnum(m["call_wall"], 1),
                      fpct(m["key_levels"]["call_wall"]["dist_pct"]))
        self._set_kpi("put_wall", fnum(m["put_wall"], 1),
                      fpct(m["key_levels"]["put_wall"]["dist_pct"]))
        self._set_kpi("atm_iv", fvol(m["atm_iv"]), f"HV20 {fvol(m['hv20'])}")

        # 读图结论（区域 → 距墙 → Flip）
        self.gex_conclusion.configure(text=self._gex_reading(r))

        # 图表
        ax = self.gex_chart.axes()
        fig = self.gex_chart.figure
        if ax is not None and gex is not None and not gex.empty:
            strikes = gex["strike"].values.astype(float)
            net = gex["net_gex"].values
            cum = gex["cum_net_gex"].values
            n = len(strikes)
            x0, x1 = float(strikes.min()), float(strikes.max())
            span = max(x1 - x0, 1.0)
            step = span / max(n - 1, 1)
            scale = 1e8

            # 区域底色：Gamma Flip 左侧=负Gamma区，右侧=正Gamma区
            zg = m.get("zero_gamma")
            if _valid(zg) and x0 < zg < x1:
                ax.axvspan(x0, zg, color=RED, alpha=0.045, zorder=0)
                ax.axvspan(zg, x1, color=ACCENT, alpha=0.055, zorder=0)
                ax.text(0.015, 0.03, "负 Gamma 区 · 放大波动", transform=ax.transAxes,
                        color=RED, fontsize=7.5, va="bottom", alpha=0.95,
                        bbox=dict(boxstyle="round,pad=0.18", fc=BG, ec="none", alpha=0.6))
                ax.text(0.985, 0.03, "正 Gamma 区 · 压制波动", transform=ax.transAxes,
                        color=ACCENT, fontsize=7.5, ha="right", va="bottom", alpha=0.95,
                        bbox=dict(boxstyle="round,pad=0.18", fc=BG, ec="none", alpha=0.6))

            # 柱：正=红（Call 堆），负=绿（Put 堆），透明度随幅度加深
            abs_net = np.abs(net)
            max_abs = abs_net.max() if abs_net.max() > 0 else 1.0
            bar_colors = []
            for v, a in zip(net, abs_net):
                alpha = 0.30 + 0.60 * (a / max_abs)
                bar_colors.append((1.0, 0.30, 0.36, alpha) if v >= 0 else (0.0, 0.80, 0.55, alpha))
            ax.bar(strikes, net / scale, color=bar_colors, width=step * 0.60,
                   edgecolor="none", zorder=2)
            ax.axhline(0, color=BORDER, linewidth=0.8, zorder=1)

            # 累计净GEX：白色主曲线 + 淡填充
            ax2 = ax.twinx()
            ax2.plot(strikes, cum / scale, color=FG, linewidth=1.9, zorder=4)
            ax2.fill_between(strikes, 0, cum / scale, color=FG, alpha=0.05, zorder=1)
            ax2.axhline(0, color=BORDER, linewidth=0.8)

            def _curve_at(s):
                return float(np.interp(s, strikes, cum)) / scale

            # 墙：细虚线 + 曲线上的圆圈；Flip / Max Pain：醒目虚线
            labels = []
            for s, c, name in [(m.get("call_wall"), GREEN, "Call Wall"),
                               (m.get("put_wall"), RED, "Put Wall")]:
                if not _valid(s):
                    continue
                ax.axvline(s, color=c, linestyle="--", linewidth=1.0, alpha=0.5, zorder=3)
                ax2.plot([s], [_curve_at(s)], marker="o", markersize=6.5, markerfacecolor=CARD,
                         markeredgecolor=c, markeredgewidth=1.6, linestyle="none", zorder=6)
                labels.append(((s - x0) / span, 0.78, f"{name} {fnum(s, 1)}", c))
            for s, c, name in [(m.get("zero_gamma"), YELLOW, "Gamma Flip"),
                               (m.get("max_pain"), BLUE, "Max Pain")]:
                if not _valid(s):
                    continue
                ax.axvline(s, color=c, linestyle="--", linewidth=1.25, alpha=0.95, zorder=3)
                labels.append(((s - x0) / span, 0.97, f"{name} {fnum(s, 1)}", c))

            # 标签错位排布，避免互相压字
            labels.sort(key=lambda t: t[0])
            placed: list[float] = []
            for xf, yf, text, color in labels:
                if any(abs(xf - px) < 0.10 for px in placed):
                    yf = min(yf, 0.64)
                placed.append(xf)
                ha = "left" if xf > 0.92 else ("right" if xf < 0.08 else "center")
                ax.annotate(text, xy=(xf, yf), xycoords="axes fraction", ha=ha, va="top",
                            color=color, fontsize=7, annotation_clip=False,
                            bbox=dict(boxstyle="round,pad=0.22", fc=CARD, ec=color, lw=0.7, alpha=0.95))

            # 最新价：琥珀色实线 + 轴下方高亮标签（不遮挡曲线）
            ax.axvline(F, color=ACCENT, linewidth=1.6, alpha=0.95, zorder=5)
            fx = min(max((F - x0) / span, 0.10), 0.90)
            ax.annotate(f"最新价 {fnum(F, 1)} · NET GEX {fyi(m['net_gex'])}",
                        xy=(fx, -0.055), xycoords="axes fraction", ha="center", va="top",
                        color=ACCENT, fontsize=7.5, fontweight="bold", annotation_clip=False,
                        bbox=dict(boxstyle="round,pad=0.32", fc="#2b2410", ec=ACCENT, lw=0.8, alpha=0.95))

            ax.set_ylabel("净GEX (亿)", color=MUTED, fontsize=8)
            ax2.set_ylabel("累计净GEX (亿)", color=FG, fontsize=8)
            ax.tick_params(colors=MUTED, labelsize=8)
            ax2.tick_params(axis="y", colors=FG, labelsize=8)
            # 关掉科学计数法的 1e6 偏移，避免读数被误读
            from matplotlib.ticker import ScalarFormatter
            for axis in (ax.yaxis, ax2.yaxis):
                fmt = ScalarFormatter(useOffset=False)
                fmt.set_scientific(False)
                axis.set_major_formatter(fmt)
            ax.set_title(f"{r['product'].upper()} · GEX 结构", color=FG, fontsize=9.5, pad=8)

            from matplotlib.lines import Line2D
            from matplotlib.patches import Patch
            handles = [
                Patch(facecolor=(1.0, 0.30, 0.36), alpha=0.8, label="正GEX柱 (Call)"),
                Patch(facecolor=(0.0, 0.80, 0.55), alpha=0.8, label="负GEX柱 (Put)"),
                Line2D([0], [0], color=FG, linewidth=1.9, label="累计净GEX"),
                Line2D([0], [0], color=ACCENT, linewidth=1.6, label=f"最新价 {fnum(F, 1)}"),
                Line2D([0], [0], color=YELLOW, linestyle="--", linewidth=1.2, label="Gamma Flip"),
                Line2D([0], [0], color=BLUE, linestyle="--", linewidth=1.2, label="Max Pain"),
                Line2D([0], [0], color=GREEN, linestyle="--", linewidth=1.0, label="Call Wall"),
                Line2D([0], [0], color=RED, linestyle="--", linewidth=1.0, label="Put Wall"),
            ]
            fig.legend(handles=handles, loc="lower center", ncol=8, fontsize=6.5,
                       bbox_to_anchor=(0.5, 0.014), frameon=False, labelcolor=MUTED,
                       handlelength=1.3, columnspacing=0.9, handletextpad=0.5)
            fig.subplots_adjust(left=0.075, right=0.915, top=0.90, bottom=0.215)
        self.gex_chart.draw(tight=False)

        # 明细表
        t = self.gex_tree
        for i in t.get_children():
            t.delete(i)
        if gex is None or gex.empty:
            return
        for row in gex.itertuples(index=False):
            t.insert("", "end", values=(
                fnum(row.strike, 1), fyi(row.call_gex), fyi(row.put_gex), fyi(row.net_gex),
                fint(row.oi), fpct(row.share_pct, signed=False), fpct(row.cum_share_pct, signed=False),
                fyi(row.cum_net_gex),
            ))

        # PCR 日内曲线
        pcr_df = r.get("pcr_series")
        ax3 = self.pcr_chart.axes()
        if ax3 is not None and pcr_df is not None and len(pcr_df) > 1:
            ax3.plot(pcr_df["time"], pcr_df["pcr"], color=ACCENT, linewidth=1.5, marker="o", markersize=3)
            ax3.tick_params(axis="x", labelrotation=0, labelsize=7)
            ax3.set_ylabel("PCR%", color=MUTED, fontsize=8)
            ax3.set_title("PCR 日内变化", color=FG, fontsize=8, pad=2)
        elif ax3 is not None:
            ax3.text(0.5, 0.5, "开启自动刷新后显示 PCR 日内轨迹", color=MUTED, ha="center", va="center", fontsize=8)
            ax3.axis("off")
        self.pcr_chart.draw()

    def _gex_reading(self, r) -> str:
        """按「区域 → 距墙 → Gamma Flip」三步生成读图结论。"""
        m, F = r["metrics"], r["F"]
        if not _valid(F):
            return "现价缺失，无法读图（可稍后刷新重试）。"
        zg, cw, pw, mp = (m.get("zero_gamma"), m.get("call_wall"),
                          m.get("put_wall"), m.get("max_pain"))
        atm = m.get("atm_iv")

        if _valid(zg):
            above = F >= zg
            zone = "正Gamma区（对冲盘压波动，行情易收敛）" if above else "负Gamma区（对冲盘追涨杀跌，波动被放大）"
            action = "适合卖波动、赚收敛的钱" if above else "别急着抄底摸顶，让趋势走"
            gap = (F - zg) / F * 100.0
            flip = (f"现价在 Gamma Flip 上方 {gap:.2f}%，处于收敛一侧；"
                    if above else f"现价在 Gamma Flip 下方 {abs(gap):.2f}%，处于放大一侧；")
            if abs(gap) < 0.3:
                flip += "贴边运行是最典型的变盘前画面，留意下一小时柱子。"
        else:
            zone, action, flip = "—", "—", "未检出 Gamma Flip（累计净GEX未穿零）。"

        d_cw = (cw - F) / F * 100.0 if _valid(cw) else float("nan")
        d_pw = (pw - F) / F * 100.0 if _valid(pw) else float("nan")
        if _valid(d_cw) and _valid(d_pw):
            if abs(d_cw) < 0.5 or abs(d_pw) < 0.5:
                wall = "已贴墙，容易出事，注意方向选择"
            else:
                wall = "上下都不挨墙，属舒适区，适合磨时间"
        else:
            wall = "关键位不足，暂无法判断空间"

        line1 = f"① 区域：现价 {fnum(F, 1)} 在 {zone}，{action}。"
        line2 = (f"② 距墙：上方 Call Wall {fnum(cw, 1)}（{fpct(d_cw)}）、"
                 f"下方 Put Wall {fnum(pw, 1)}（{fpct(d_pw)}），{wall}；"
                 f"Max Pain {fnum(mp, 1)} 为到期引力中心。")
        line3 = f"③ Flip：{flip}"
        if _valid(atm):
            line3 += (f" ATM IV {fvol(atm)}，"
                      + ("正区＋高隐波，卖波动更舒服。" if _valid(zg) and F >= zg
                         else "负区优先顺势，别逆势卖波动。"))
        lines = [line1, line2, line3]
        key = f"{r['exchange']}.{r['product']}.{r['expiry']}"
        info = self._signal_info.get(key) or {}
        if info.get("text"):
            lines.append(info["text"])
        return "\n".join(lines)

    def _set_kpi(self, key, val, sub):
        self.kpi_cards[key]["val"].configure(text=val)
        self.kpi_cards[key]["sub"].configure(text=sub)

    def _fill_skew(self):
        if not self.chain_result:
            return
        r = self.chain_result
        m = r["metrics"]
        summary = (f"标的F {fnum(r['F'])} | 到期 {r['expiry']} ({r['days']}天)\n"
                   f"ATM IV {fvol(m['atm_iv'])} | 25Δ偏度 {fvol(m['skew_25d'])} (占ATM {fpct(m['skew_pct'])}) | "
                   f"Call25Δ {fvol(m['iv_25d_call'])} | Put25Δ {fvol(m['iv_25d_put'])}")
        self.skew_summary.configure(text=summary)
        ax = self.skew_chart.axes()
        df = r["chain"]
        if ax is not None and df is not None and not df.empty:
            for cp, color, lbl in [("C", GREEN, "Call"), ("P", RED, "Put")]:
                sub = df[df["option_class"].str.upper().str.startswith(cp)]
                if sub.empty:
                    continue
                ax.plot(sub["strike"], sub["iv"] * 100, "o-", color=color, markersize=3,
                        linewidth=1.2, label=lbl)
            ax.axhline(m["atm_iv"] * 100, color=YELLOW, linestyle="--", linewidth=1, label=f"ATM {fvol(m['atm_iv'])}")
            ax.set_xlabel("行权价", color=MUTED, fontsize=8)
            ax.set_ylabel("IV (%)", color=MUTED, fontsize=8)
            ax.legend(fontsize=8, facecolor=CARD, edgecolor=BORDER, labelcolor=FG)
            ax.set_title("IV 微笑曲线", color=FG, fontsize=10)
        self.skew_chart.draw()

    def _fill_skew_term(self):
        if self.df_skew is None or self.df_skew.empty:
            return
        t = self.skew_tree
        for i in t.get_children():
            t.delete(i)
        for r in self.df_skew.itertuples(index=False):
            t.insert("", "end", values=(
                r.expiry, fint(r.days), fnum(r.F, 1), fvol(r.atm_iv), fvol(r.hv20),
                fpct(r.iv_hv_pct), fvol(r.skew_25d), fpct(r.skew_pct), fyi(r.net_gex),
            ))

    def _fill_cone(self):
        if not self.chain_result:
            return
        r = self.chain_result
        cone = r.get("cone", {})
        df = cone.get("cone") if isinstance(cone, dict) else None
        iv_rank = cone.get("iv_rank_in_hv") if isinstance(cone, dict) else float("nan")
        self.cone_info.configure(
            text=f"当前 IV {fvol(r['metrics']['atm_iv'])} 在历史 HV 中分位 {fpct(iv_rank, signed=False)}")
        ax = self.cone_chart.axes()
        if ax is not None and df is not None and not df.empty:
            wins = df["window"].astype(str).values
            for col, color, ls in [("p10", RED, ":"), ("p25", YELLOW, "--"), ("p50", FG, "-"),
                                   ("p75", CYAN, "--"), ("p90", GREEN, ":")]:
                ax.plot(wins, df[col] * 100, color=color, linestyle=ls, linewidth=1.3, label=col)
            ax.plot(wins, df["current"] * 100, "o-", color=ACCENT, linewidth=2, markersize=5, label="当前HV")
            ax.axhline(r["metrics"]["atm_iv"] * 100, color=PURPLE, linestyle="-.", linewidth=1.2, label="当前IV")
            ax.set_xlabel("HV 窗口（交易日）", color=MUTED, fontsize=8)
            ax.set_ylabel("波动率 (%)", color=MUTED, fontsize=8)
            ax.legend(fontsize=7, facecolor=CARD, edgecolor=BORDER, labelcolor=FG)
            ax.set_title("波动率锥：当前 IV 在历史 HV 分位中的位置", color=FG, fontsize=10)
        self.cone_chart.draw()

        t = self.cone_tree
        for i in t.get_children():
            t.delete(i)
        if df is None or df.empty:
            return
        for row in df.itertuples(index=False):
            t.insert("", "end", values=(
                f"{row.window}日", fvol(row.mean), fvol(row.p10), fvol(row.p25), fvol(row.p50),
                fvol(row.p75), fvol(row.p90), fvol(row.current),
            ))

    def _fill_history(self):
        if not self.chain_result:
            return
        r = self.chain_result
        df = self.engine.load_history(r["exchange"], r["product"], r["expiry"])
        self.df_history = df
        ax = self.history_chart.axes()
        if ax is not None and df is not None and not df.empty:
            dates = df["date"].values
            for col, color, lbl in [("call_wall", GREEN, "Call Wall"),
                                    ("put_wall", RED, "Put Wall"),
                                    ("zero_gamma", CYAN, "Zero Gamma"),
                                    ("F", FG, "标的F")]:
                ax.plot(dates, df[col], "o-", color=color, markersize=3, linewidth=1.2, label=lbl)
            ax.set_xlabel("日期", color=MUTED, fontsize=8)
            ax.set_ylabel("价位", color=MUTED, fontsize=8)
            ax.tick_params(axis="x", labelrotation=45, labelsize=7)
            ax.legend(fontsize=8, facecolor=CARD, edgecolor=BORDER, labelcolor=FG)
            ax.set_title("关键价位历史轨迹", color=FG, fontsize=10)
        self.history_chart.draw()

        t = self.history_tree
        for i in t.get_children():
            t.delete(i)
        if df is None or df.empty:
            return
        for row in df.itertuples(index=False):
            t.insert("", "end", values=(
                row.date, fnum(row.F, 1), fnum(row.call_wall, 1), fnum(row.put_wall, 1),
                fnum(row.gamma_wall, 1), fnum(row.vol_trigger, 1), fnum(row.zero_gamma, 1),
                fvol(row.atm_iv), fpct(row.pcr_oi, signed=False),
            ))

    def _fill_cross(self):
        if self.df_cross is None or self.df_cross.empty:
            return
        t = self.cross_tree
        for i in t.get_children():
            t.delete(i)
        for r in self.df_cross.itertuples(index=False):
            t.insert("", "end", values=(
                fint(r.gex_rank_all), r.exchange_name, r.product, r.expiry, fint(r.days),
                fnum(r.F, 1), fvol(r.atm_iv), fpct(r.iv_hv_pct), fyi(r.net_gex), fpct(r.pcr_oi),
            ))

    # ---------------------------------------------------------------- alerts / favorites / export
    def _check_alerts(self):
        if not self.chain_result:
            self.alert_lbl.configure(text="")
            return
        m = self.chain_result["metrics"]
        alerts = []
        skey = f"{self.chain_result['exchange']}.{self.chain_result['product']}.{self.chain_result['expiry']}"
        if (self._signal_info.get(skey) or {}).get("active"):
            alerts.append("ATM IV 触底回升 · 进场信号")
        for key, name in [("call_wall", "Call Wall"), ("put_wall", "Put Wall")]:
            d = m["key_levels"][key]["dist_pct"]
            if _valid(d) and abs(d) < 0.5:
                alerts.append(f"现价距{name}仅 {fpct(d)}")
        self.alert_lbl.configure(text="⚠ " + "  ".join(alerts) if alerts else "")

    def _refresh_favorites(self):
        for w in self.fav_frame.winfo_children():
            w.destroy()
        favs = self.cfg.get("favorites", [])
        if not favs:
            tk.Label(self.fav_frame, text="（空）点上方★添加", bg=PANEL, fg=MUTED,
                     font=(self.font_name, 8)).pack(anchor="w", padx=8)
            return
        for f in favs:
            row = tk.Frame(self.fav_frame, bg=PANEL)
            row.pack(fill="x", padx=4, pady=1)
            tk.Button(row, text=f, bg=PANEL, fg=CYAN, relief="flat", anchor="w",
                      font=(self.mono_font, 9), cursor="hand2",
                      command=lambda ff=f: self._goto_favorite(ff)).pack(side="left", fill="x", expand=True)
            tk.Button(row, text="×", bg=PANEL, fg=RED, relief="flat", font=(self.font_name, 9),
                      cursor="hand2", command=lambda ff=f: self._remove_favorite(ff)).pack(side="right")

    def _add_favorite(self):
        prod, key = self._selected()
        if not prod:
            return
        tag = f"{self.exchange}.{prod}.{key}"
        favs = self.cfg.setdefault("favorites", [])
        if tag not in favs:
            favs.append(tag)
            save_config(self.cfg)
            self._refresh_favorites()

    def _remove_favorite(self, tag):
        favs = self.cfg.get("favorites", [])
        if tag in favs:
            favs.remove(tag)
            save_config(self.cfg)
            self._refresh_favorites()

    def _goto_favorite(self, tag):
        parts = tag.split(".", 2)
        if len(parts) != 3:
            return
        ex, prod, expiry = parts
        self._switch_exchange(ex)
        # 等品种加载完
        def _pick():
            self.product_combo.set(prod)
            self._on_product_change()
            # 选中对应到期
            vals = list(self.expiry_combo["values"])
            for i, v in enumerate(vals):
                if v.startswith(expiry):
                    self.expiry_combo.current(i)
                    break
            self._show_page("GEX分析")
            self._on_query_chain()
        self.root.after(800, _pick)

    def _export_figure(self, chart: ChartPanel, prefix: str):
        if not HAS_MPL or chart.figure is None:
            messagebox.showinfo("提示", "暂无图表可导出。")
            return
        path = filedialog.asksaveasfilename(defaultextension=".png",
                                            filetypes=[("PNG", "*.png")],
                                            initialfile=f"{prefix}_{self._now().replace(':', '')}.png")
        if not path:
            return
        chart.save_png(path)
        messagebox.showinfo("导出成功", f"已保存到 {path}")

    def _on_export_overview(self):
        self._export_csv(self.df_overview, "overview")

    def _on_export_chain(self):
        self._export_csv(self.df_chain, "chain")

    def _on_export_cross(self):
        self._export_csv(self.df_cross, "cross")

    def _export_csv(self, df, prefix):
        if df is None or df.empty:
            messagebox.showinfo("提示", "暂无数据可导出。")
            return
        path = filedialog.asksaveasfilename(defaultextension=".csv",
                                            filetypes=[("CSV", "*.csv")],
                                            initialfile=f"{prefix}_{self._now().replace(':', '')}.csv")
        if not path:
            return
        df.to_csv(path, index=False, encoding="utf-8-sig")
        messagebox.showinfo("导出成功", f"已保存到 {path}")

    # ---------------------------------------------------------------- tree events
    def _on_overview_double(self, _event):
        sel = self.overview_tree.selection()
        if not sel:
            return
        vals = self.overview_tree.item(sel[0])["values"]
        prod, expiry = vals[1], vals[2]
        self.product_combo.set(prod)
        self._on_product_change()
        vals_e = list(self.expiry_combo["values"])
        for i, v in enumerate(vals_e):
            if v.startswith(str(expiry)):
                self.expiry_combo.current(i)
                break
        self._show_page("期权链")
        self._on_query_chain()

    def _on_gex_double(self, _event):
        sel = self.gex_tree.selection()
        if not sel:
            return
        vals = self.gex_tree.item(sel[0])["values"]
        # 滚动图表到该行权价（简化：切到期权链页）
        self._show_page("期权链")

    # ---------------------------------------------------------------- run loop
    def _submit(self, fn, *args, callback=None) -> bool:
        if self.engine is None:
            return False
        self._set_busy(True)
        fut = self.engine.submit(fn, *args)
        if callback:
            self.root.after(80, lambda: self._poll_future(fut, callback))
        else:
            self.root.after(80, lambda: self._poll_future(fut, None))
        return True

    def _poll_future(self, fut, callback):
        if not fut.done():
            self.root.after(100, lambda: self._poll_future(fut, callback))
            return
        try:
            if callback:
                callback(fut)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("错误", str(exc))
        finally:
            self._set_busy(False)

    def _notify(self, kind, msg, *args):
        if kind == "status":
            self._set_status(msg, CYAN)

    def _poll(self):
        while True:
            try:
                kind, payload = self.ui_queue.get_nowait()
            except queue.Empty:
                break
        self.root.after(150, self._poll)

    def _set_busy(self, busy: bool):
        if busy:
            self.pending += 1
        else:
            self.pending = max(0, self.pending - 1)
        state = "disabled" if self.pending > 0 else "normal"
        for w in self._action_widgets:
            try:
                w.configure(state=state)
            except Exception:
                pass
        if self.pending > 0:
            self.root.configure(cursor="watch")
        else:
            self.root.configure(cursor="")

    def _set_status(self, text: str, color: str):
        self.status_lbl.configure(text=text, fg=color)

    @staticmethod
    def _now() -> str:
        from datetime import datetime
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _on_close(self):
        if self._refresh_job is not None:
            try:
                self.root.after_cancel(self._refresh_job)
            except Exception:
                pass
        if self.engine is not None:
            self.engine.stop()
        self.root.destroy()


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
