"""期权波动率雷达 · TQSDK 桌面端（专业终端风）

运行：python app.py
"""

from __future__ import annotations

import json
import math
import os
import queue
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

    def draw(self):
        if not HAS_MPL:
            return
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
        self._refresh_job = None
        self._last_refresh = ""
        self._action_widgets: list[tk.Widget] = []

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
        self.root.title("期权波动率雷达 · TQSDK")
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
        tk.Label(bar, text="刷新", bg=PANEL, fg=MUTED, font=(self.font_name, 9)).pack(side="left", padx=(10, 0))
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

        # 4 个 KPI 卡
        kpi = tk.Frame(parent, bg=BG)
        kpi.pack(fill="x", padx=10)
        self.kpi_cards: dict[str, dict] = {}
        for key in ["net_gex", "zero_gamma", "call_wall", "put_wall"]:
            self.kpi_cards[key] = self._make_kpi_card(kpi, key)

        # 关键价位卡片（参考图样式）
        lvl = tk.Frame(parent, bg=BG)
        lvl.pack(fill="x", padx=10, pady=(8, 0))
        self.level_cards: dict[str, dict] = {}
        for key in ["call_wall", "put_wall", "gamma_wall", "vol_trigger"]:
            self.level_cards[key] = self._make_level_card(lvl, key)

        # PCR 日内变化
        pcr_wrap = tk.Frame(parent, bg=BG)
        pcr_wrap.pack(fill="x", padx=10, pady=(6, 0))
        tk.Label(pcr_wrap, text="PCR 日内变化（Put OI / Call OI）", bg=BG, fg=MUTED,
                 font=(self.font_name, 8, "bold")).pack(anchor="w")
        self.pcr_chart = ChartPanel(pcr_wrap, figsize=(7.2, 1.4))
        self.pcr_chart.frame.pack(fill="x")

        # 图表
        self.gex_chart = ChartPanel(parent, figsize=(7.2, 3.6))
        self.gex_chart.frame.pack(fill="both", expand=True, padx=10, pady=8)

        # 明细表
        body = tk.Frame(parent, bg=BG)
        body.pack(fill="both", expand=False, padx=10, pady=(0, 10))
        cols = ["strike", "call_gex", "put_gex", "net_gex", "oi", "share_pct", "cum_share_pct", "cum_net_gex"]
        self.gex_tree = self._mk_tree(body, cols, widths=[70, 90, 90, 90, 80, 70, 80, 95],
                                      titles=["行权价", "Call GEX", "Put GEX", "净GEX", "持仓", "占比%", "累计占比%", "累计净GEX"])
        self.gex_tree.bind("<Double-1>", self._on_gex_double)

    def _make_kpi_card(self, parent, key: str) -> dict:
        colors = {"net_gex": (CYAN, "净GEX"), "zero_gamma": (YELLOW, "零伽马位"),
                  "call_wall": (GREEN, "看涨阻力 Call Wall"), "put_wall": (RED, "看跌支撑 Put Wall")}
        color, label = colors[key]
        card = tk.Frame(parent, bg=CARD, highlightbackground=BORDER, highlightthickness=1)
        card.pack(side="left", expand=True, fill="x", padx=3)
        tk.Label(card, text=label, bg=CARD, fg=MUTED, font=(self.font_name, 8)).pack(anchor="w", padx=8, pady=(5, 0))
        val = tk.Label(card, text="—", bg=CARD, fg=color, font=(self.mono_font, 16, "bold"))
        val.pack(anchor="w", padx=8, pady=(1, 0))
        sub = tk.Label(card, text="", bg=CARD, fg=MUTED, font=(self.mono_font, 8))
        sub.pack(anchor="w", padx=8, pady=(0, 5))
        return {"card": card, "val": val, "sub": sub, "color": color}

    def _make_level_card(self, parent, key: str) -> dict:
        palette = {
            "call_wall": (GREEN, "① 看涨阻力位", "Call Wall", "价格向上易遇天花板"),
            "put_wall": (RED, "② 看跌支撑位", "Put Wall", "价格向下易托住地板"),
            "gamma_wall": (YELLOW, "③ 伽马关键位", "Gamma Wall", "价格容易被钉住波动收敛"),
            "vol_trigger": (PURPLE, "④ 波动变化位", "Vol Trigger", "市场可能从收敛切换为放大"),
        }
        color, title, en, hint = palette[key]
        card = tk.Frame(parent, bg=CARD, highlightbackground=color, highlightthickness=2)
        card.pack(side="left", expand=True, fill="x", padx=3, pady=4)
        head = tk.Frame(card, bg=CARD)
        head.pack(fill="x", padx=8, pady=(5, 0))
        tk.Label(head, text=title, bg=CARD, fg=color, font=(self.font_name, 9, "bold")).pack(side="left")
        tk.Label(head, text=en, bg=CARD, fg=MUTED, font=(self.mono_font, 8)).pack(side="right")
        val = tk.Label(card, text="—", bg=CARD, fg=FG, font=(self.mono_font, 15, "bold"))
        val.pack(anchor="w", padx=8, pady=(2, 0))
        sub = tk.Label(card, text=hint, bg=CARD, fg=MUTED, font=(self.font_name, 8), wraplength=150, justify="left")
        sub.pack(anchor="w", padx=8, pady=(0, 5))
        return {"val": val, "sub": sub, "card": card, "color": color}

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
        self._fill_chain()
        self._fill_gex()
        self._fill_skew()
        self._fill_cone()
        self._fill_history()
        self._check_alerts()

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
        self.gex_info.configure(
            text=f"F={fnum(F)} | 净GEX={fyi(m['net_gex'])} | Call={fyi(m['call_gex'])} | Put={fyi(m['put_gex'])}")

        # KPI
        self._set_kpi("net_gex", fyi(m["net_gex"]), f"零伽马 {fnum(m['zero_gamma'], 1)}")
        self._set_kpi("zero_gamma", fnum(m["zero_gamma"], 1), fpct(m["key_levels"]["zero_gamma"]["dist_pct"]))
        self._set_kpi("call_wall", fnum(m["call_wall"], 1), fpct(m["key_levels"]["call_wall"]["dist_pct"]))
        self._set_kpi("put_wall", fnum(m["put_wall"], 1), fpct(m["key_levels"]["put_wall"]["dist_pct"]))

        # 关键价位卡
        for key in ["call_wall", "put_wall", "gamma_wall", "vol_trigger"]:
            lv = m["key_levels"][key]
            self.level_cards[key]["val"].configure(text=fnum(lv["strike"], 1))
            self.level_cards[key]["sub"].configure(
                text=f"{lv['hint']} | 距现价 {fpct(lv['dist_pct'])}")
            # 异动高亮：距现价绝对值 < 0.5% 时边框加粗变色
            near = _valid(lv["dist_pct"]) and abs(lv["dist_pct"]) < 0.5
            self.level_cards[key]["card"].configure(
                highlightbackground=YELLOW if near else self.level_cards[key]["color"])

        # 图表
        ax = self.gex_chart.axes()
        if ax is not None and gex is not None and not gex.empty:
            strikes = gex["strike"].values
            net = gex["net_gex"].values
            cum = gex["cum_net_gex"].values
            n = len(strikes)

            # 柱：按净GEX符号上色，按幅度设透明度（更暗=更大）
            abs_net = np.abs(net)
            max_abs = abs_net.max() if abs_net.max() > 0 else 1.0
            bar_colors = []
            for v, a in zip(net, abs_net):
                if v >= 0:
                    alpha = 0.35 + 0.6 * (a / max_abs)
                    bar_colors.append((0.0, 0.84, 0.56, alpha))  # GREEN
                else:
                    alpha = 0.35 + 0.6 * (a / max_abs)
                    bar_colors.append((1.0, 0.23, 0.23, alpha))  # RED
            step = (strikes[-1] - strikes[0]) / max(n - 1, 1) if n > 1 else 1.0
            width = step * 0.62
            ax.bar(strikes, net / 1e8, color=bar_colors, width=width, edgecolor="none",
                   label="净GEX(亿)")
            ax.axhline(0, color=BORDER, linewidth=0.8)

            # 累计净GEX：白色粗线 + 下方淡色填充，与零伽马竖线区分
            ax2 = ax.twinx()
            ax2.plot(strikes, cum / 1e8, color=FG, linewidth=2.0, zorder=3, label="累计净GEX(亿)")
            ax2.fill_between(strikes, 0, cum / 1e8, color=CYAN, alpha=0.10, zorder=1)
            ax2.axhline(0, color=BORDER, linewidth=0.8)

            # 关键价位竖线：精简标注，避免重叠
            markers = [
                (m["zero_gamma"], CYAN, "ZG", "Zero Gamma"),
                (m["call_wall"], GREEN, "CW", "Call Wall"),
                (m["put_wall"], RED, "PW", "Put Wall"),
                (m["gamma_wall"], YELLOW, "GW", "Gamma Wall"),
                (m["vol_trigger"], PURPLE, "VT", "Vol Trigger"),
            ]
            y_top = net.max() / 1e8 * 0.92 if net.max() > 0 else 0.05
            valid = [(s, c, l) for s, c, l, _ in markers if _valid(s)]
            # 简单去重：相邻标签错开上下
            label_y = []
            for s, c, l in valid:
                ax.axvline(s, color=c, linestyle="--", linewidth=1.1, alpha=0.9)
                # 选择上下位置避免重叠
                placed_below = any(abs(s - s2) < step * 1.5 for s2 in label_y)
                yy = y_top * (0.42 if placed_below else 0.96)
                label_y.append(s)
                ax.text(s, yy, f"{l}\n{fnum(s, 1)}", color=c, fontsize=6.5,
                        ha="center", va="top",
                        bbox=dict(boxstyle="round,pad=0.15", fc=CARD, ec=c, lw=0.6, alpha=0.92))

            # 现价 F：琥珀色实线，最醒目
            ax.axvline(F, color=ACCENT, linestyle="-", linewidth=1.8, alpha=0.95, zorder=4)
            ax.text(F, net.min() / 1e8 * 0.88 if net.min() < 0 else -0.05,
                    f"F={fnum(F, 1)}", color=ACCENT, fontsize=8, ha="center", va="top",
                    fontweight="bold")

            ax.set_xlabel("行权价", color=MUTED, fontsize=8)
            ax.set_ylabel("净GEX (亿)", color=MUTED, fontsize=8)
            ax2.set_ylabel("累计净GEX (亿)", color=FG, fontsize=8)
            ax2.tick_params(axis="y", colors=FG, labelsize=8)
            ax.tick_params(axis="y", colors=MUTED, labelsize=8)
            ax.set_title("GEX 分布 · 累计净GEX · 五大关键价位", color=FG, fontsize=9.5, pad=6)

            # 小图例
            from matplotlib.patches import Patch
            from matplotlib.lines import Line2D
            handles = [
                Patch(facecolor=GREEN, edgecolor="none", alpha=0.8, label="净GEX>0"),
                Patch(facecolor=RED, edgecolor="none", alpha=0.8, label="净GEX<0"),
                Line2D([0], [0], color=FG, linewidth=2, label="累计净GEX"),
                Line2D([0], [0], color=ACCENT, linewidth=1.8, label=f"现价 F={fnum(F, 1)}"),
            ]
            ax.legend(handles=handles, loc="upper left", fontsize=7, facecolor=CARD,
                      edgecolor=BORDER, labelcolor=FG, framealpha=0.92, handlelength=1.6)
        self.gex_chart.draw()

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
