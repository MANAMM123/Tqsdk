"""期权波动率雷达 · TQSDK 桌面端

按交易所查询期权链，计算 IV / HV / 25Δ偏度 / GEX，并给出排名与占比。
运行：python app.py
"""

from __future__ import annotations

import json
import math
import os
import queue
import sys
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
except Exception as _exc:  # tqsdk 未安装 / 版本不兼容
    EXCHANGES = {"SHFE": "上期所", "DCE": "大商所", "CZCE": "郑商所", "CFFEX": "中金所"}
    Engine = None  # type: ignore[assignment]
    IMPORT_ERROR = _exc

# 打包成可执行文件后，配置文件放在 exe 同目录，避免写入临时解包目录
BASE_DIR = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

# 密码等敏感信息只从环境变量读取，不落盘
ENV_USERNAME = "TQ_USERNAME"
ENV_PASSWORD = "TQ_PASSWORD"
ENV_RATE = "TQ_RISK_FREE_RATE"

BG = "#0b0f14"
PANEL = "#131a23"
CARD = "#18212b"
BORDER = "#263241"
FG = "#e6edf3"
MUTED = "#8b9bb0"
ACCENT = "#f0b429"
BLUE = "#58a6ff"
UP = "#ff5c5c"
DOWN = "#26a69a"


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
    """把金额转成 亿 / 万。"""
    if not _valid(x):
        return "—"
    v = float(x)
    if abs(v) >= 1e8:
        return f"{v / 1e8:+.2f}亿"
    if abs(v) >= 1e4:
        return f"{v / 1e4:+.1f}万"
    return f"{v:+.0f}"


def fyiw(x) -> str:
    if not _valid(x):
        return "—"
    return f"{float(x) / 1e4:,.1f}"


def load_config() -> dict:
    """读取配置：非敏感项来自 config.json，账号密码优先取环境变量。"""
    cfg = {"username": "", "password": "", "risk_free_rate": 0.0}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        for key in ("username", "risk_free_rate"):
            if key in data:
                cfg[key] = data[key]
    except Exception:
        pass

    if os.environ.get(ENV_USERNAME, "").strip():
        cfg["username"] = os.environ[ENV_USERNAME].strip()
    if os.environ.get(ENV_PASSWORD, "").strip():
        cfg["password"] = os.environ[ENV_PASSWORD].strip()
    try:
        if os.environ.get(ENV_RATE, "").strip():
            cfg["risk_free_rate"] = float(os.environ[ENV_RATE].strip())
    except ValueError:
        pass
    return cfg


def save_config(cfg: dict):
    """只把非敏感项写入 config.json，密码永不落盘。"""
    data = {"username": cfg.get("username", ""), "risk_free_rate": cfg.get("risk_free_rate", 0.0)}
    with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except Exception:
        pass


class SettingsDialog(tk.Toplevel):
    def __init__(self, parent, cfg: dict, on_save, font_name: str = "TkDefaultFont"):
        super().__init__(parent)
        self.title("快期账户设置")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.on_save = on_save
        self.font_name = font_name
        self.transient(parent)
        self.grab_set()

        body = tk.Frame(self, bg=BG, padx=24, pady=20)
        body.pack(fill="both", expand=True)
        tk.Label(body, text="TQSDK 登录", bg=BG, fg=FG,
                 font=(self.font_name, 14, "bold")).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 4))
        tk.Label(body, text="使用快期账户（手机号 + 密码）登录，行情数据由天勤提供。",
                 bg=BG, fg=MUTED, font=(self.font_name, 9)).grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 14))

        tk.Label(body, text="账号", bg=BG, fg=MUTED, font=(self.font_name, 10)).grid(row=2, column=0, sticky="w")
        self.user_entry = tk.Entry(body, bg=CARD, fg=FG, insertbackground=FG, relief="flat",
                                   font=(self.font_name, 11), width=28)
        self.user_entry.grid(row=2, column=1, sticky="ew", padx=(10, 0), ipady=6)
        self.user_entry.insert(0, cfg.get("username", ""))

        tk.Label(body, text="密码", bg=BG, fg=MUTED, font=(self.font_name, 10)).grid(row=3, column=0, sticky="w", pady=(10, 0))
        self.pwd_entry = tk.Entry(body, bg=CARD, fg=FG, insertbackground=FG, relief="flat",
                                  font=(self.font_name, 11), width=28, show="*")
        self.pwd_entry.grid(row=3, column=1, sticky="ew", padx=(10, 0), pady=(10, 0), ipady=6)
        self.pwd_entry.insert(0, cfg.get("password", ""))

        tk.Label(body, text="账号保存到 config.json；密码不落盘，仅本次运行有效。\n"
                           f"长期免输入可设置环境变量 {ENV_USERNAME} / {ENV_PASSWORD} 后启动。",
                 bg=BG, fg=MUTED, font=(self.font_name, 8), justify="left").grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(12, 0))

        btns = tk.Frame(body, bg=BG)
        btns.grid(row=5, column=0, columnspan=2, sticky="e", pady=(18, 0))
        tk.Button(btns, text="取消", command=self.destroy, bg=PANEL, fg=FG, relief="flat",
                  font=(self.font_name, 10), padx=16, pady=5, cursor="hand2").pack(side="left", padx=6)
        tk.Button(btns, text="保存并登录", command=self._save, bg=ACCENT, fg="#111111", relief="flat",
                  font=(self.font_name, 10, "bold"), padx=16, pady=5, cursor="hand2").pack(side="left")
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
    def __init__(self, parent, figsize=(5.4, 3.4)):
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


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.font_name = self._pick_font(root)
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
        self._action_widgets: list[tk.Widget] = []
        self._sort_state: dict = {}

        self._setup_window()
        self._setup_style()
        self._build_ui()
        self._start_engine()
        self.root.after(120, self._poll)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---------------------------------------------------------------- setup
    def _pick_font(self, root) -> str:
        families = set(tkfont.families(root))
        for name in ("Microsoft YaHei UI", "Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC",
                     "Source Han Sans SC", "SimHei", "WenQuanYi Micro Hei", "DejaVu Sans"):
            if name in families:
                return name
        return "TkDefaultFont"

    def _setup_window(self):
        self.root.title("期权波动率雷达 · TQSDK")
        self.root.geometry("1440x900")
        self.root.minsize(1200, 760)
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
        style.configure("Card.TLabel", background=CARD, foreground=FG, font=(self.font_name, 9))
        style.configure("CardMuted.TLabel", background=CARD, foreground=MUTED, font=(self.font_name, 8))
        style.configure("TNotebook", background=BG, borderwidth=0, tabmargins=(0, 6, 0, 0))
        style.configure("TNotebook.Tab", background=PANEL, foreground=MUTED, padding=(18, 9),
                        font=(self.font_name, 10, "bold"))
        style.map("TNotebook.Tab", background=[("selected", CARD)], foreground=[("selected", ACCENT)])
        style.configure("Treeview", background=CARD, fieldbackground=CARD, foreground=FG, rowheight=25,
                        borderwidth=0, font=(self.font_name, 9))
        style.configure("Treeview.Heading", background=PANEL, foreground=MUTED, relief="flat",
                        font=(self.font_name, 9, "bold"), padding=(4, 6))
        style.map("Treeview.Heading", background=[("active", BORDER)])
        style.map("Treeview", background=[("selected", "#1f3a56")], foreground=[("selected", FG)])
        style.configure("TCombobox", fieldbackground=CARD, background=PANEL, foreground=FG,
                        arrowcolor=FG, bordercolor=BORDER, lightcolor=CARD, darkcolor=CARD,
                        insertcolor=FG, padding=5)
        style.map("TCombobox", fieldbackground=[("readonly", CARD)],
                  foreground=[("readonly", FG)], background=[("readonly", PANEL)])
        style.configure("TProgressbar", background=ACCENT, troughcolor=PANEL, borderwidth=0, thickness=6)
        style.configure("Vertical.TScrollbar", background=PANEL, troughcolor=BG, bordercolor=BG,
                        arrowcolor=MUTED)
        self.root.option_add("*TCombobox*Listbox.background", CARD)
        self.root.option_add("*TCombobox*Listbox.foreground", FG)
        self.root.option_add("*TCombobox*Listbox.selectBackground", BLUE)

    # ---------------------------------------------------------------- ui
    def _build_ui(self):
        header = tk.Frame(self.root, bg=BG, padx=20, pady=14)
        header.pack(fill="x")
        tk.Label(header, text="期权波动率雷达", bg=BG, fg=FG,
                 font=(self.font_name, 20, "bold")).pack(side="left")
        tk.Label(header, text="  TQSDK · 四大交易所期权  IV / HV / 25Δ SKEW / GEX",
                 bg=BG, fg=MUTED, font=(self.font_name, 10)).pack(side="left", padx=(8, 0), pady=(8, 0))

        self.account_btn = tk.Button(header, text="账户", command=self._open_settings, bg=CARD, fg=FG,
                                     relief="flat", font=(self.font_name, 10, "bold"), padx=16, pady=5,
                                     activebackground=BORDER, activeforeground=FG, cursor="hand2")
        self.account_btn.pack(side="right")
        self.state_label = tk.Label(header, text="● 未登录", bg=BG, fg=UP, font=(self.font_name, 10))
        self.state_label.pack(side="right", padx=14)

        toolbar = tk.Frame(self.root, bg=PANEL, padx=16, pady=12)
        toolbar.pack(fill="x")
        self.ex_buttons = {}
        for code, label in EXCHANGES.items():
            btn = tk.Button(toolbar, text=label, command=lambda c=code: self._switch_exchange(c),
                            bg=CARD, fg=MUTED, relief="flat", font=(self.font_name, 10, "bold"),
                            padx=14, pady=6, activebackground=BORDER, activeforeground=FG, cursor="hand2")
            btn.pack(side="left", padx=(0, 6))
            self.ex_buttons[code] = btn
            self._action_widgets.append(btn)

        tk.Label(toolbar, text="品种", bg=PANEL, fg=MUTED, font=(self.font_name, 9)).pack(side="left", padx=(14, 4))
        self.product_box = ttk.Combobox(toolbar, width=12, state="readonly")
        self.product_box.pack(side="left")
        self.product_box.bind("<<ComboboxSelected>>", lambda e: self._on_product_change())

        tk.Label(toolbar, text="到期", bg=PANEL, fg=MUTED, font=(self.font_name, 9)).pack(side="left", padx=(12, 4))
        self.expiry_box = ttk.Combobox(toolbar, width=22, state="readonly")
        self.expiry_box.pack(side="left")

        self.query_btn = tk.Button(toolbar, text="查询期权链", command=self._query_chain, bg=ACCENT, fg="#111111",
                                   relief="flat", font=(self.font_name, 10, "bold"), padx=16, pady=6,
                                   activebackground="#d9a11f", activeforeground="#111111", cursor="hand2")
        self.query_btn.pack(side="left", padx=(14, 6))
        self.scan_btn = tk.Button(toolbar, text="扫描全品种", command=self._scan, bg=BLUE, fg="#0b1622",
                                  relief="flat", font=(self.font_name, 10, "bold"), padx=16, pady=6,
                                  activebackground="#3f8fe0", activeforeground="#0b1622", cursor="hand2")
        self.scan_btn.pack(side="left", padx=6)
        self.export_btn = tk.Button(toolbar, text="导出 CSV", command=self._export, bg=CARD, fg=FG,
                                    relief="flat", font=(self.font_name, 10), padx=14, pady=6,
                                    activebackground=BORDER, activeforeground=FG, cursor="hand2")
        self.export_btn.pack(side="left", padx=6)
        self._action_widgets += [self.product_box, self.expiry_box, self.query_btn, self.scan_btn, self.export_btn]

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=14, pady=(10, 0))

        self._build_overview_tab()
        self._build_chain_tab()
        self._build_gex_tab()
        self._build_skew_tab()

        status = tk.Frame(self.root, bg=BG, padx=16, pady=8)
        status.pack(fill="x")
        self.progress = ttk.Progressbar(status, mode="determinate", length=180)
        self.progress.pack(side="right")
        self.status_var = tk.StringVar(value="启动中…")
        tk.Label(status, textvariable=self.status_var, bg=BG, fg=MUTED, anchor="w",
                 font=(self.font_name, 9)).pack(side="left", fill="x", expand=True)

        self._switch_exchange_visual("SHFE")

    def _new_tree(self, parent, columns, height=18):
        frame = tk.Frame(parent, bg=CARD)
        tree = ttk.Treeview(frame, columns=[c[0] for c in columns], show="headings", height=height)
        for key, title, width in columns:
            tree.heading(key, text=title, command=lambda k=key: self._sort_tree(tree, k))
            tree.column(key, width=width, anchor="e" if key not in ("product", "instrument_id", "option_class",
                                                                   "expiry", "name") else "w", stretch=False)
        vsb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        hsb = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        frame.rowconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        tree.tag_configure("neg", foreground=DOWN)
        tree.tag_configure("pos", foreground=UP)
        for tag in ("zero", "one"):
            tree.tag_configure(tag, background=CARD)
        return frame, tree

    def _build_overview_tab(self):
        tab = tk.Frame(self.notebook, bg=BG)
        self.notebook.add(tab, text="  品种概览  ")
        top = tk.Frame(tab, bg=BG, pady=8)
        top.pack(fill="x")
        tk.Label(top, text="按交易所扫描各品种最近到期期权，按 |净 GEX| 排名；占比为各品种净 GEX 绝对值占全部品种之和。",
                 bg=BG, fg=MUTED, font=(self.font_name, 9)).pack(side="left")
        tk.Label(top, text="双击行可跳到该品种期权链", bg=BG, fg=BLUE,
                 font=(self.font_name, 9)).pack(side="right")
        cols = [("gex_rank", "GEX排名", 70), ("product", "品种", 70), ("expiry", "到期", 62),
                ("days", "剩余天", 62), ("F", "标的价", 92), ("atm_iv", "ATM IV", 78),
                ("hv20", "HV20", 78), ("hv60", "HV60", 78), ("iv_hv_pct", "IV/HV溢价", 88),
                ("skew_25d", "25Δ偏度(vol)", 96), ("skew_pct", "偏度/ATM", 82),
                ("net_gex", "净GEX(亿)", 92), ("gex_share_pct", "GEX占比", 78),
                ("pcr_oi", "PCR(持仓)", 82), ("iv_percentile", "IV链内分位", 82), ("contracts", "合约数", 64)]
        frame, self.tree_overview = self._new_tree(tab, cols, height=20)
        frame.pack(fill="both", expand=True)
        self.tree_overview.bind("<Double-1>", self._jump_from_overview)

    def _build_chain_tab(self):
        tab = tk.Frame(self.notebook, bg=BG)
        self.notebook.add(tab, text="  期权链  ")
        top = tk.Frame(tab, bg=BG, pady=8)
        top.pack(fill="x")
        self.chain_title = tk.Label(top, text="选择品种与到期后点击「查询期权链」", bg=BG, fg=FG,
                                    font=(self.font_name, 10, "bold"))
        self.chain_title.pack(side="left")
        tk.Label(top, text="价格优先取买一/卖一中间价，其次最新价、昨收", bg=BG, fg=MUTED,
                 font=(self.font_name, 8)).pack(side="right")
        cols = [("instrument_id", "合约", 138), ("option_class", "类型", 54), ("strike", "行权价", 88),
                ("price", "现价", 78), ("bid_price1", "买一", 78), ("ask_price1", "卖一", 78),
                ("iv", "IV", 64), ("delta", "Δ", 64), ("gamma", "Γ", 90), ("open_interest", "持仓", 72),
                ("gex", "GEX(万)", 86), ("gex_share_pct", "占比%", 70), ("days", "剩余天", 62)]
        frame, self.tree_chain = self._new_tree(tab, cols, height=20)
        frame.pack(fill="both", expand=True)

    def _build_gex_tab(self):
        tab = tk.Frame(self.notebook, bg=BG)
        self.notebook.add(tab, text="  GEX 排名  ")
        paned = tk.PanedWindow(tab, orient="horizontal", bg=BG, sashwidth=6, borderwidth=0)
        paned.pack(fill="both", expand=True, pady=8)

        left = tk.Frame(paned, bg=CARD)
        self.gex_head = tk.Label(left, text="净 GEX 分布与累计占比", bg=CARD, fg=FG,
                                 font=(self.font_name, 10, "bold"), anchor="w")
        self.gex_head.pack(fill="x", padx=12, pady=(10, 0))
        self.gex_sub = tk.Label(left, text="—", bg=CARD, fg=MUTED,
                                font=(self.font_name, 9), anchor="w", justify="left")
        self.gex_sub.pack(fill="x", padx=12)
        self.gex_chart = ChartPanel(left)
        self.gex_chart.frame.pack(fill="both", expand=True, padx=6, pady=6)
        paned.add(left, stretch="always")

        right = tk.Frame(paned, bg=BG)
        cols = [("strike", "行权价", 92), ("call_gex", "Call GEX(万)", 100), ("put_gex", "Put GEX(万)", 100),
                ("net_gex", "净GEX(万)", 96), ("share_pct", "|净|占比%", 82),
                ("cum_share_pct", "累计占比%", 88), ("cum_net_gex", "累计净GEX(万)", 108),
                ("oi", "持仓", 72)]
        frame, self.tree_gex = self._new_tree(right, cols, height=20)
        frame.pack(fill="both", expand=True)
        paned.add(right, stretch="always")

    def _build_skew_tab(self):
        tab = tk.Frame(self.notebook, bg=BG)
        self.notebook.add(tab, text="  偏度 SKEW  ")
        paned = tk.PanedWindow(tab, orient="horizontal", bg=BG, sashwidth=6, borderwidth=0)
        paned.pack(fill="both", expand=True, pady=8)

        left = tk.Frame(paned, bg=CARD)
        tk.Label(left, text="隐含波动率微笑", bg=CARD, fg=FG, font=(self.font_name, 10, "bold"),
                 anchor="w").pack(fill="x", padx=12, pady=(10, 0))
        self.skew_chart = ChartPanel(left)
        self.skew_chart.frame.pack(fill="both", expand=True, padx=6, pady=6)
        paned.add(left, stretch="always")

        right = tk.Frame(paned, bg=BG)
        card = tk.Frame(right, bg=CARD, padx=16, pady=12)
        card.pack(fill="x")
        tk.Label(card, text="SKEW / 波动率 / GEX 摘要", bg=CARD, fg=FG,
                 font=(self.font_name, 11, "bold")).grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 10))
        self.mvars = {}
        rows = [
            ("product", "品种", "expiry", "到期"),
            ("days", "剩余天数", "spot", "标的"),
            ("F", "标的价", "contracts", "合约数"),
            ("atm_iv", "ATM IV", "hv20", "HV20"),
            ("hv60", "HV60", "iv_hv_pct", "IV/HV 溢价"),
            ("iv_pct", "IV 链内分位", "iv_range", "IV 区间"),
            ("p25", "25Δ Put IV", "c25", "25Δ Call IV"),
            ("skew", "25Δ 偏度", "skew_pct", "偏度 / ATM"),
            ("netgex", "净 GEX", "callputgex", "Call / Put GEX"),
            ("zero", "零伽马位", "maxstrike", "|GEX| 最大行权价"),
            ("callwall", "Call 墙", "putwall", "Put 墙"),
            ("pcr", "PCR(持仓)", "updated", "计算时间"),
        ]
        for i, (k1, t1, k2, t2) in enumerate(rows, start=1):
            v1 = tk.StringVar(value="—")
            v2 = tk.StringVar(value="—")
            self.mvars[k1] = v1
            self.mvars[k2] = v2
            tk.Label(card, text=t1, bg=CARD, fg=MUTED, font=(self.font_name, 9)).grid(
                row=i, column=0, sticky="w", pady=2)
            tk.Label(card, textvariable=v1, bg=CARD, fg=FG, font=(self.font_name, 10, "bold")).grid(
                row=i, column=1, sticky="w", padx=(8, 20), pady=2)
            tk.Label(card, text=t2, bg=CARD, fg=MUTED, font=(self.font_name, 9)).grid(
                row=i, column=2, sticky="w", pady=2)
            tk.Label(card, textvariable=v2, bg=CARD, fg=FG, font=(self.font_name, 10, "bold")).grid(
                row=i, column=3, sticky="w", padx=(8, 0), pady=2)
        tk.Label(card, text="IV 由 Black-76 反解；HV 为标的日线 20/60 日年化；"
                            "GEX = Γ×OI×乘数×F²×1%（Call 正 / Put 负）；"
                            "25Δ 偏度 = Put25Δ IV − Call25Δ IV。",
                 bg=CARD, fg=MUTED, font=(self.font_name, 8), wraplength=430, justify="left").grid(
            row=len(rows) + 1, column=0, columnspan=4, sticky="w", pady=(12, 0))

        term = tk.Frame(right, bg=BG, pady=8)
        term.pack(fill="both", expand=True)
        head = tk.Frame(term, bg=BG)
        head.pack(fill="x")
        tk.Label(head, text="偏度期限结构", bg=BG, fg=FG, font=(self.font_name, 10, "bold")).pack(side="left")
        self.term_btn = tk.Button(head, text="计算全期限偏度", command=self._term_structure, bg=CARD, fg=FG,
                                  relief="flat", font=(self.font_name, 9), padx=12, pady=4,
                                  activebackground=BORDER, activeforeground=FG, cursor="hand2")
        self.term_btn.pack(side="right")
        cols = [("expiry", "到期", 62), ("days", "剩余天", 62), ("atm_iv", "ATM IV", 72),
                ("hv20", "HV20", 72), ("iv_hv_pct", "IV/HV", 72), ("skew_25d", "25Δ偏度", 80),
                ("skew_pct", "偏度/ATM", 80), ("net_gex", "净GEX(亿)", 88)]
        frame, self.tree_skew = self._new_tree(term, cols, height=8)
        frame.pack(fill="both", expand=True, pady=(6, 0))
        paned.add(right, stretch="always")

    # ---------------------------------------------------------------- engine
    def _start_engine(self):
        self.engine = Engine(self.cfg, self._notify)
        self.engine.start()
        if self.cfg.get("username") and self.cfg.get("password"):
            self.root.after(150, lambda: self._connect(self.cfg["username"], self.cfg["password"]))
        else:
            self.root.after(300, self._open_settings)

    def _notify(self, kind, text, frac=None):
        self.ui_queue.put((kind, text, frac))

    def _poll(self):
        try:
            while True:
                kind, text, frac = self.ui_queue.get_nowait()
                if kind == "status":
                    self.status_var.set(text)
                elif kind == "progress":
                    if _valid(frac):
                        self.progress.stop()
                        self.progress.configure(mode="determinate", value=float(frac) * 100)
                    self.status_var.set(text)
                elif kind == "error":
                    self.status_var.set(text)
        except queue.Empty:
            pass
        self.root.after(120, self._poll)

    def _connect(self, user, pwd):
        self.cfg["username"], self.cfg["password"] = user, pwd
        # 密码只放在当前进程内，便于自动重连；不写入文件
        os.environ[ENV_USERNAME] = user
        os.environ[ENV_PASSWORD] = pwd
        save_config(self.cfg)
        self._run(self.engine.connect, user, pwd, on_ok=self._on_connected,
                  on_error=self._on_login_error, label="正在登录天勤…")

    def _on_connected(self, _):
        self.connected = True
        self.state_label.configure(text="● 已登录", fg=DOWN)
        self.status_var.set("登录成功，正在加载合约列表…")
        self._load_products(self.exchange)

    def _on_login_error(self, exc):
        self.connected = False
        self.state_label.configure(text="● 未登录", fg=UP)
        msg = str(exc)
        self.status_var.set("登录失败：" + msg)
        messagebox.showerror("登录失败",
                             f"{msg}\n\n请点击右上角「账户」检查快期账号与密码；\n"
                             "若密码已更换，保存后会自动重新登录。", parent=self.root)
        self.root.after(200, self._open_settings)

    def _open_settings(self):
        if self.pending:
            return
        SettingsDialog(self.root, self.cfg, lambda u, p: self._connect(u, p), font_name=self.font_name)

    def _run(self, fn, *args, on_ok=None, on_error=None, label="处理中…"):
        if not self.engine:
            return
        self.pending += 1
        self._set_busy(True)
        if label:
            self.status_var.set(label)
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)
        fut = self.engine.submit(fn, *args)

        def check():
            if not fut.done():
                self.root.after(140, check)
                return
            try:
                try:
                    result = fut.result()
                except Exception as exc:  # noqa: BLE001
                    (on_error or self._on_error)(exc)
                else:
                    if on_ok:
                        try:
                            on_ok(result)
                        except Exception as exc:  # noqa: BLE001
                            self._on_error(exc)
            finally:
                self.pending = max(0, self.pending - 1)
                if self.pending == 0:
                    self.progress.stop()
                    self.progress.configure(mode="determinate", value=0)
                    self._set_busy(False)

        self.root.after(140, check)

    def _set_busy(self, busy):
        for widget in self._action_widgets:
            try:
                widget.configure(state="disabled" if busy else ("readonly" if isinstance(widget, ttk.Combobox) else "normal"))
            except Exception:
                pass

    def _on_error(self, exc):
        msg = str(exc) or exc.__class__.__name__
        self.status_var.set("失败：" + msg)
        messagebox.showerror("操作失败", msg, parent=self.root)
        if ("登录" in msg or "认证" in msg or "credentials" in msg) and not self.connected:
            self.root.after(100, self._open_settings)

    # ---------------------------------------------------------------- actions
    def _switch_exchange(self, code):
        if self.pending:
            return
        self.exchange = code
        self._switch_exchange_visual(code)
        self._load_products(code)

    def _switch_exchange_visual(self, code):
        for c, btn in self.ex_buttons.items():
            btn.configure(bg=ACCENT if c == code else CARD, fg="#111111" if c == code else MUTED)

    def _load_products(self, exchange):
        self.products = []
        self.product_box.configure(values=[])
        self.expiry_box.configure(values=[])
        self._run(self.engine.load_products, exchange, True, on_ok=self._on_products,
                  label=f"加载 {EXCHANGES.get(exchange, exchange)} 合约…")

    def _on_products(self, res):
        self.products = res.get("products", [])
        values = [p["product"] for p in self.products]
        self.product_box.configure(values=values)
        if values:
            self.product_box.current(0)
            self._on_product_change()
        self.status_var.set(f"{EXCHANGES.get(self.exchange, self.exchange)}：{len(values)} 个期权品种已就绪")

    def _on_product_change(self):
        idx = self.product_box.current()
        if idx < 0 or idx >= len(self.products):
            return
        prod = self.products[idx]
        self.expiry_box.configure(values=[
            f"{e['key']} · 剩余{int(e['days']) if _valid(e['days']) else '?'}天 · {e['count']}张"
            for e in prod["expiries"]])
        if prod["expiries"]:
            self.expiry_box.current(0)
        self.chain_title.configure(text=f"{prod['product']} 期权链 · 选择到期后查询")

    def _selected(self):
        idx = self.product_box.current()
        eidx = self.expiry_box.current()
        if idx < 0 or idx >= len(self.products):
            raise RuntimeError("请先选择品种。")
        prod = self.products[idx]
        if eidx < 0 or eidx >= len(prod["expiries"]):
            raise RuntimeError("请先选择到期。")
        return prod["product"], prod["expiries"][eidx]["key"]

    def _query_chain(self):
        try:
            product, expiry = self._selected()
        except Exception as exc:
            messagebox.showinfo("提示", str(exc), parent=self.root)
            return
        self._run(self.engine.query_chain, self.exchange, product, expiry, 15,
                  on_ok=self._on_chain, label=f"查询 {product} {expiry} 期权链…")

    def _on_chain(self, res):
        self.chain_result = res
        chain = res["chain"]
        gex = res["gex"]
        m = res["metrics"]
        self.df_chain = chain.copy()

        self.chain_title.configure(
            text=f"{res['product']} {res['expiry']} · 标的 {res['spot_symbol']} {fnum(res['F'])} · "
                 f"剩余 {res['days']} 天 · {len(chain)} 个合约")
        self._fill_chain(chain)
        self._fill_gex(gex, m)
        self._fill_skew(res)
        self._fill_metrics(res)
        self._draw_gex_chart(gex)
        self._draw_smile(chain, res["F"])
        self.notebook.select(1)
        self.status_var.set(f"{res['product']} {res['expiry']} 计算完成")

    def _clear_tree(self, tree):
        for item in tree.get_children(""):
            tree.delete(item)

    def _fill_chain(self, chain: pd.DataFrame):
        tree = self.tree_chain
        self._clear_tree(tree)
        for r in chain.itertuples(index=False):
            tag = ("neg",) if _valid(r.gex) and r.gex < 0 else ("pos",)
            tree.insert("", "end", values=(
                r.instrument_id, "看涨 C" if r.option_class == "CALL" else "看跌 P",
                fnum(r.strike, 2), fnum(r.price, 2), fnum(r.bid_price1, 2), fnum(r.ask_price1, 2),
                fvol(r.iv), fnum(r.delta, 3), fnum(r.gamma, 5), fint(r.open_interest),
                fyiw(r.gex), fpct(r.gex_share_pct, 1, signed=False), fint(r.days),
            ), tags=tag)

    def _fill_gex(self, gex: pd.DataFrame, metrics: dict):
        tree = self.tree_gex
        self._clear_tree(tree)
        ranked = gex.reindex(gex["net_gex"].abs().sort_values(ascending=False).index) if not gex.empty else gex
        for r in ranked.itertuples(index=False):
            tag = ("pos",) if r.net_gex >= 0 else ("neg",)
            tree.insert("", "end", values=(
                fnum(r.strike, 2), fyiw(r.call_gex), fyiw(r.put_gex), fyiw(r.net_gex),
                fpct(r.share_pct, 1, signed=False), fpct(r.cum_share_pct, 1, signed=False),
                fyiw(r.cum_net_gex), fint(r.oi),
            ), tags=tag)
        if not gex.empty:
            eighty = gex[gex["cum_share_pct"] >= 80]
            k80 = fnum(eighty["strike"].iloc[0], 2) if not eighty.empty else "—"
            self.gex_sub.configure(
                text=f"净 GEX {fyi(metrics['net_gex'])} / 1%   |   "
                     f"零伽马 {fnum(metrics['zero_gamma'], 2)}   |   "
                     f"|GEX| 最大 {fnum(metrics['max_gex_strike'], 2)}   |   "
                     f"累计占比 80% 处 {k80}")
        self.df_gex = gex.copy()

    def _fill_skew(self, res: dict):
        m = res["metrics"]
        self.df_skew = pd.DataFrame([{
            "expiry": res["expiry"], "days": res["days"], "F": res["F"],
            "atm_iv": m["atm_iv"], "hv20": m["hv20"], "iv_hv_pct": m["iv_hv_pct"],
            "skew_25d": m["skew_25d"], "skew_pct": m["skew_pct"], "net_gex": m["net_gex"],
        }])
        self._fill_skew_table(self.df_skew)

    def _fill_skew_table(self, df: pd.DataFrame):
        tree = self.tree_skew
        self._clear_tree(tree)
        if df is None or df.empty:
            return
        for r in df.itertuples(index=False):
            tag = ("neg",) if _valid(r.net_gex) and r.net_gex < 0 else ("pos",)
            tree.insert("", "end", values=(
                str(r.expiry), fint(r.days), fvol(r.atm_iv), fvol(r.hv20),
                fpct(r.iv_hv_pct, 1), fnum(r.skew_25d, 2), fpct(r.skew_pct, 1),
                fyi(r.net_gex),
            ), tags=tag)

    def _fill_metrics(self, res: dict):
        m = res["metrics"]
        mv = self.mvars
        mv["product"].set(res["product"])
        mv["expiry"].set(str(res["expiry"]))
        mv["days"].set(f"{res['days']} 天")
        mv["spot"].set(res["spot_symbol"] or "—")
        mv["F"].set(fnum(res["F"], 2))
        mv["contracts"].set(str(m["contracts"]))
        mv["atm_iv"].set(fvol(m["atm_iv"]))
        mv["hv20"].set(fvol(m["hv20"]))
        mv["hv60"].set(fvol(m["hv60"]))
        mv["iv_hv_pct"].set(fpct(m["iv_hv_pct"], 1))
        mv["iv_pct"].set(fpct(m["iv_percentile"], 1, signed=False))
        mv["iv_range"].set(f"{fvol(m['iv_min'])} ~ {fvol(m['iv_max'])}")
        mv["p25"].set(fvol(m["iv_25d_put"]))
        mv["c25"].set(fvol(m["iv_25d_call"]))
        mv["skew"].set(f"{fnum(m['skew_25d'], 2)} vol")
        mv["skew_pct"].set(fpct(m["skew_pct"], 1))
        mv["netgex"].set(fyi(m["net_gex"]))
        mv["callputgex"].set(f"{fyi(m['call_gex'])} / {fyi(m['put_gex'])}")
        mv["zero"].set(fnum(m["zero_gamma"], 2))
        mv["maxstrike"].set(fnum(m["max_gex_strike"], 2))
        mv["callwall"].set(fnum(m["call_wall"], 2))
        mv["putwall"].set(fnum(m["put_wall"], 2))
        mv["pcr"].set(fpct(m["pcr_oi"], 1, signed=False))
        mv["updated"].set(pd.Timestamp.now().strftime("%H:%M:%S"))

    def _draw_gex_chart(self, gex: pd.DataFrame):
        if not HAS_MPL:
            self.gex_chart.placeholder("matplotlib 未安装")
            return
        if gex.empty:
            self.gex_chart.placeholder("无 GEX 数据")
            return
        ax = self.gex_chart.axes()
        x = gex["strike"].values
        net = gex["net_gex"].values / 1e4
        colors = [UP if v >= 0 else DOWN for v in net]
        ax.bar(x, net, width=(x[1] - x[0]) * 0.75 if len(x) > 1 else 1, color=colors, alpha=0.85)
        ax.axhline(0, color=MUTED, linewidth=0.8)
        ax.set_ylabel("净 GEX（万元 / 1%）", color=MUTED, fontsize=9)
        ax.set_xlabel("行权价", color=MUTED, fontsize=9)
        for label in ax.get_xticklabels():
            label.set_rotation(45)
            label.set_fontsize(7)
        ax2 = ax.twinx()
        ax2.plot(x, gex["cum_share_pct"].values, color=BLUE, linewidth=1.4, marker="o", markersize=3)
        ax2.set_ylim(0, 105)
        ax2.set_ylabel("累计占比 %", color=BLUE, fontsize=8)
        ax2.tick_params(colors=BLUE, labelsize=7)
        ax.set_title("GEX 排名分布", color=FG, fontsize=11, loc="left")
        self.gex_chart.draw()

    def _draw_smile(self, chain: pd.DataFrame, F: float):
        if not HAS_MPL:
            self.skew_chart.placeholder("matplotlib 未安装")
            return
        valid = chain[chain["iv"].notna() & np.isfinite(chain["iv"])]
        if valid.empty:
            self.skew_chart.placeholder("无有效 IV")
            return
        ax = self.skew_chart.axes()
        calls = valid[valid["option_class"] == "CALL"]
        puts = valid[valid["option_class"] == "PUT"]
        if not calls.empty:
            ax.scatter(calls["strike"], calls["iv"] * 100, s=20, color=UP, alpha=0.8, label="Call IV", zorder=3)
        if not puts.empty:
            ax.scatter(puts["strike"], puts["iv"] * 100, s=20, color=DOWN, alpha=0.8, label="Put IV", zorder=3)
        curve = valid.groupby("strike")["iv"].mean().sort_index()
        ax.plot(curve.index.values, curve.values * 100, color=ACCENT, linewidth=1.6, label="微笑曲线", zorder=2)
        ax.axvline(F, color=BLUE, linestyle="--", linewidth=1.0, label=f"标的 {F:.2f}")
        ax.set_xlabel("行权价", color=MUTED, fontsize=9)
        ax.set_ylabel("隐含波动率 %", color=MUTED, fontsize=9)
        ax.set_title("IV Smile", color=FG, fontsize=11, loc="left")
        legend = ax.legend(facecolor=CARD, edgecolor=BORDER, labelcolor=FG, fontsize=8, loc="upper right")
        if legend:
            legend.get_frame().set_alpha(0.9)
        for label in ax.get_xticklabels():
            label.set_rotation(30)
            label.set_fontsize(7)
        self.skew_chart.draw()

    def _scan(self):
        self._run(self.engine.scan_exchange, self.exchange, False, on_ok=self._on_scan,
                  label=f"扫描 {EXCHANGES.get(self.exchange, self.exchange)} 全部期权品种…")

    def _on_scan(self, df: pd.DataFrame):
        self.df_overview = df
        tree = self.tree_overview
        self._clear_tree(tree)
        for r in df.itertuples(index=False):
            tag = ("pos",) if _valid(r.iv_hv_pct) and r.iv_hv_pct > 0 else ("neg",)
            tree.insert("", "end", values=(
                fint(r.gex_rank), str(r.product), str(r.expiry), fint(r.days), fnum(r.F, 2),
                fvol(r.atm_iv), fvol(r.hv20), fvol(r.hv60), fpct(r.iv_hv_pct, 1),
                f"{fnum(r.skew_25d, 2)}", fpct(r.skew_pct, 1), fyi(r.net_gex),
                fpct(r.gex_share_pct, 1, signed=False), fpct(r.pcr_oi, 1, signed=False),
                fpct(r.iv_percentile, 1, signed=False), fint(r.contracts),
            ), tags=tag)
        self.notebook.select(0)
        self.status_var.set(f"已扫描 {len(df)} 个品种，按 |净GEX| 排名完成")

    def _jump_from_overview(self, _event=None):
        sel = self.tree_overview.selection()
        if not sel:
            return
        vals = self.tree_overview.item(sel[0], "values")
        product, expiry = vals[1], vals[2]
        idx = next((i for i, p in enumerate(self.products) if p["product"] == product), None)
        if idx is None:
            return
        self.product_box.current(idx)
        self._on_product_change()
        eidx = next((i for i, e in enumerate(self.products[idx]["expiries"]) if e["key"] == expiry), None)
        if eidx is not None:
            self.expiry_box.current(eidx)
            
            self._query_chain()

    def _term_structure(self):
        try:
            product, _ = self._selected()
        except Exception as exc:
            messagebox.showinfo("提示", str(exc), parent=self.root)
            return
        self._run(self.engine.term_structure, self.exchange, product, on_ok=self._on_term,
                  label=f"计算 {product} 全期限偏度…")

    def _on_term(self, df: pd.DataFrame):
        self._fill_skew_table(df)
        self.status_var.set(f"{self._selected()[0]} 全期限偏度计算完成（{len(df)} 个到期）")

    def _export(self):
        tab = self.notebook.index(self.notebook.select())
        data = {0: self.df_overview, 1: self.df_chain, 2: self.df_gex, 3: self.df_skew}.get(tab)
        if data is None or data.empty:
            messagebox.showinfo("提示", "当前页没有可导出的数据。", parent=self.root)
            return
        default = f"{self.exchange}_{self._safe_name()}.csv"
        path = filedialog.asksaveasfilename(parent=self.root, defaultextension=".csv",
                                            initialfile=default, filetypes=[("CSV", "*.csv")])
        if not path:
            return
        try:
            data.to_csv(path, index=False, encoding="utf-8-sig")
            self.status_var.set(f"已导出 {os.path.basename(path)}")
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("导出失败", str(exc), parent=self.root)

    def _safe_name(self) -> str:
        try:
            product, expiry = self._selected()
            return f"{product}_{expiry}"
        except Exception:
            return "options"

    def _sort_tree(self, tree, col):
        state = self._sort_state.get(tree, (None, False))
        reverse = not state[1] if state[0] == col else False
        self._sort_state[tree] = (col, reverse)
        items = [(tree.set(k, col), k) for k in tree.get_children("")]

        def key(pair):
            text = pair[0].replace(",", "").replace("%", "").replace("+", "").replace("亿", "e8").replace("万", "e4")
            try:
                return (0, float(text))
            except ValueError:
                return (1, pair[0])

        items.sort(key=key, reverse=reverse)
        for index, (_, k) in enumerate(items):
            tree.move(k, "", index)

    def _on_close(self):
        if self.engine:
            self.engine.stop()
        try:
            self.root.destroy()
        except Exception:
            pass


def main():
    root = tk.Tk()
    if Engine is None:
        root.withdraw()
        if sys.stderr is not None:
            print(f"依赖导入失败：{IMPORT_ERROR}", file=sys.stderr)
        messagebox.showerror("无法启动",
                             f"依赖导入失败：{IMPORT_ERROR}\n\n"
                             "请先执行：pip install -r requirements.txt\n"
                             "并确认 Python 版本为 3.9 ~ 3.12。")
        root.destroy()
        return
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
