import re, threading, sys, ctypes, hashlib, time
import tkinter as tk
from tkinter import scrolledtext, messagebox, ttk
from collections import OrderedDict
import requests # type: ignore
from bs4 import BeautifulSoup # type: ignore

# ========== DPI 适配：获取系统缩放比例，避免高分屏字体模糊 ==========
def get_dpi_scale():
    if sys.platform != "win32":
        return 1.0
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # 启用 Per-Monitor DPI Aware
    except:
        try:
            ctypes.windll.user32.SetProcessDPIAware()  # 旧版 Windows 降级方案
        except:
            pass
    try:
        hdc = ctypes.windll.user32.GetDC(0)
        dpi = ctypes.windll.gdi32.GetDeviceCaps(hdc, 88)  # 88 = LOGPIXELSX
        ctypes.windll.user32.ReleaseDC(0, hdc)
        return max(dpi / 96.0, 1.0)  # 96 为标准 DPI
    except:
        return 1.0

def has_cn(s):
    """判断字符串是否含中文"""
    return bool(re.search(r'[\u4e00-\u9fff]', s))

# ========== FIFO 缓存：OrderedDict 实现，超上限自动淘汰最早条目 ==========
class QueryCache:
    __slots__ = ('max_size', 'cache')
    def __init__(self, max_size=50):
        self.max_size = max_size
        self.cache = OrderedDict()

    def get(self, word, source):
        key = f"{source}:{word}"
        if key in self.cache:
            self.cache.move_to_end(key)  # LRU 风格：最近使用的移到末尾
            return self.cache[key]
        return None

    def set(self, word, source, result):
        key = f"{source}:{word}"
        if key in self.cache:
            self.cache.move_to_end(key)
        else:
            if len(self.cache) >= self.max_size:
                self.cache.popitem(last=False)  # 淘汰最早插入的
        self.cache[key] = result

    def size(self):
        return len(self.cache)

cache = QueryCache()

# ========== HTTP Session（复用连接池）和 HTML 解析器选择 ==========
session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
})
try:
    import lxml # type: ignore
    PARSER = "lxml"  # C 扩展，速度快
except ImportError:
    PARSER = "html.parser"  # 内置回退

# ========== 有道词典查询：爬取 dict.youdao.com 提取释义/音标/例句 ==========
def fetch_youdao(word):
    try:
        resp = session.get(f"https://dict.youdao.com/search?q={word}", timeout=8)
        resp.raise_for_status()
    except Exception as e:
        return f"[错误] 网络请求失败: {e}"

    soup = BeautifulSoup(resp.text, PARSER)
    lines = []
    is_cn = has_cn(word)

    kw = soup.find("span", class_="keyword")
    lines.append("单词: " + (kw.text.strip() if kw else word))

    pts = soup.find_all("span", class_="phonetic")
    if pts:
        p = [t.text.strip() for t in pts if t.text.strip()]
        if p:
            lines.append(("拼音" if is_cn else "音标") + ": " + "  /  ".join(p))

    container = soup.find("div", class_="trans-container")
    if is_cn and not container:
        container = soup.find("div", id="phrsListTab")
    if container:
        meanings = []
        for tag in ["li", "p"]:
            items = container.find_all(tag)
            if items:
                meanings = [x.get_text(" ", strip=True) for x in items if x.get_text(" ", strip=True)]
            if meanings:
                break
        if not meanings:
            txt = container.get_text(" ", strip=True)
            if txt:
                meanings = [p.strip() for p in re.split(r'[;\n]+', re.sub(r'\s*;\s*', '; ', txt)) if p.strip()]
        lines.append(("英文翻译" if is_cn else "中文释义") + ":")
        if meanings:
            for m in meanings:
                lines.append("  \u2022 " + m)
        else:
            lines.append("(释义解析失败)")
    else:
        lines.append("未找到释义容器。")

    examples = (soup.find_all("div", class_="example-item") or
                soup.find_all("div", class_="example-sentence"))
    if not examples and is_cn:
        bil = soup.find("div", id="bilingual")
        if bil:
            examples = bil.find_all("div", class_="example-item")
    if examples:
        lines.append("\n例句:")
        for i, item in enumerate(examples):
            if i >= 3:
                break
            en = item.find(["div", "p"], class_=lambda c: c and "example-en" in c)
            zh = item.find(["div", "p"], class_=lambda c: c and "example-zh" in c)
            if en and zh:
                lines.append(f"  {en.text.strip()}\n  {zh.text.strip()}\n")
    return "\n".join(lines)

# ========== 百度翻译查询：调用 fanyi.baidu.com sug API（POST JSON） ==========
def fetch_baidu(word):
    try:
        resp = session.post("https://fanyi.baidu.com/sug", data={"kw": word}, timeout=8)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        return f"[错误] 百度翻译请求失败: {e}"
    if data.get("errno") != 0:
        return "[错误] 百度翻译返回异常。"
    entries = data.get("data", [])
    if not entries:
        return "未找到翻译结果。"
    lines = ["单词: " + word, "翻译结果:"]
    for e in entries:
        v = e.get("v", "")
        if v:
            lines.append("  \u2022 " + v)
    return "\n".join(lines)

# ========== 有道句子翻译：调用 fanyi.youdao.com JSON API，支持整句翻译 ==========
def fetch_youdao_translate(text):
    """
    翻译句子/段落 — 使用新版 fanyi.youdao.com TextTranslate 接口
    方案一：dict.youdao.com/jsonapi_s（带签名）
    方案二：dict.youdao.com/search 页面解析（兜底）
    方案三：dict.youdao.com/webtranslate 旧接口（最后尝试）
    """

    # ---------- 签名工具 ----------
    def _md5(s):
        return hashlib.md5(s.encode()).hexdigest()

    def _jsonapi_sign(q, ts):
        """dict.youdao.com/jsonapi_s 签名算法"""
        return _md5(f"{q}{ts}ebn5SipWJnb")

    # ---------- 方案一：jsonapi_s POST（新页面使用的接口）----------
    try:
        ts = str(int(time.time() * 10000))  # 13 位时间戳
        resp = session.post(
            "https://dict.youdao.com/jsonapi_s?doctype=json&jsonversion=4",
            data={
                "q": text,
                "t": ts,
                "client": "webmain",
                "keyfrom": "webfanyi.webmain",
                "sign": _jsonapi_sign(text, ts),
            },
            headers={"Referer": "https://fanyi.youdao.com/"},
            timeout=8)
        resp.raise_for_status()
        data = resp.json()
        # 从返回中提取翻译（可能有多个层级）
        translated = None
        for top_key in ("translateResult", "webtranslateResult"):
            results = data.get(top_key) or data.get("data", {}).get(top_key) or []
            for group in results:
                for item in group:
                    tgt = item.get("tgt", "")
                    if tgt:
                        translated = tgt
                        break
                if translated:
                    break
            if translated:
                break
        if translated:
            return "< 句子翻译 >\n原文: " + text + "\n翻译:\n  " + translated
    except Exception:
        pass

    # ---------- 方案二：dict.youdao.com/search 页面解析（同 fetch_youdao）----------
    try:
        resp = session.get(f"https://dict.youdao.com/search?q={text}", timeout=8)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, PARSER)
        # 查找翻译结果区域
        trans = soup.find("div", class_="trans-container")
        if trans:
            items = trans.find_all("p", class_="wordGroup") or trans.find_all("li")
            for item in items:
                txt = item.get_text(" ", strip=True)
                if txt and txt != text:
                    return "< 句子翻译 >\n原文: " + text + "\n翻译:\n  " + txt
            # 兜底：取整个容器文本
            whole = trans.get_text(" ", strip=True)
            if whole:
                return "< 句子翻译 >\n原文: " + text + "\n翻译:\n  " + whole
    except Exception:
        pass

    # ---------- 方案三：dict.youdao.com/webtranslate ----------
    try:
        resp = session.post("https://dict.youdao.com/webtranslate",
            data={"i": text, "from": "AUTO", "to": "AUTO", "doctype": "json"},
            headers={"Referer": "https://dict.youdao.com/"},
            timeout=8)
        resp.raise_for_status()
        data = resp.json()
        results = data.get("translateResult") or []
        for group in results:
            for item in group:
                tgt = item.get("tgt", "")
                if tgt:
                    return "< 句子翻译 >\n原文: " + text + "\n翻译:\n  " + tgt
    except Exception:
        pass

    return "[错误] 翻译请求失败，请检查网络或稍后重试。"

# ========== 查询入口：缓存优先 → 按数据源分发 → 错误结果不入缓存 ==========
def fetch_word_info(word, source):
    cached = cache.get(word, source)
    if cached:
        return cached, True
    if source == "baidu":
        result = fetch_baidu(word)
    elif source == "youdao_translate":
        result = fetch_youdao_translate(word)
    else:
        result = fetch_youdao(word)
    if not result.startswith("[错误]"):
        cache.set(word, source, result)
    return result, False

# ========== Canvas 圆角按钮：支持悬停颜色过渡动画、禁用态 ==========
class ModernButton(tk.Canvas):
    def __init__(self, parent, text, command, width=80, height=30,
                 bg="#4A90D9", hover_bg="#5BA0E9", fg="white", font=("Segoe UI", 9)):
        super().__init__(parent, width=width, height=height,
                         bg=parent["bg"], highlightthickness=0, bd=0)
        self.cmd, self.bg, self.hover_bg = command, bg, hover_bg
        self.fg, self.text, self.enabled = fg, text, True
        self._cur = bg
        r, w, h = 6, width, height
        self._items = [
            self.create_arc((0, 0, r*2, r*2), start=90, extent=90),
            self.create_arc((w-r*2, 0, w, r*2), start=0, extent=90),
            self.create_arc((0, h-r*2, r*2, h), start=180, extent=90),
            self.create_arc((w-r*2, h-r*2, w, h), start=270, extent=90),
            self.create_rectangle((r, 0, w-r, h)),
            self.create_rectangle((0, r, w, h-r)),
        ]
        self._tid = self.create_text(w/2, h/2, font=font)
        self._apply(bg)
        self.bind("<Enter>", lambda e: self._anim_to(self.hover_bg))
        self.bind("<Leave>", lambda e: self._anim_to(self.bg))
        self.bind("<Button-1>", lambda e: self.cmd() if self.enabled else None)

    def _apply(self, color):
        """统一更新所有图形颜色"""
        self._cur = color
        for item in self._items:
            self.itemconfig(item, fill=color, outline=color)
        self.itemconfig(self._tid, text=self.text, fill=self.fg)

    def _lerp(self, a, b, t):
        """十六进制颜色线性插值，t=0→a, t=1→b"""
        def l(x, y): return int(x + (y - x) * t)
        return f"#{l(int(a[1:3],16),int(b[1:3],16)):02x}{l(int(a[3:5],16),int(b[3:5],16)):02x}{l(int(a[5:7],16),int(b[5:7],16)):02x}"

    def _anim_to(self, target):
        """10 帧递归动画：从当前色过渡到目标色"""
        if not self.enabled:
            return
        from_c = self._cur
        def step(s):
            if s >= 10:
                self._apply(target)
                return
            self._apply(self._lerp(from_c, target, s / 10))
            self.after(15, step, s + 1)
        step(0)

    def transition_to(self, new_bg, new_hover_bg, new_fg, new_text):
        """外部主动切换（如置顶按钮）"""
        self.bg, self.hover_bg, self.fg, self.text = new_bg, new_hover_bg, new_fg, new_text
        self.itemconfig(self._tid, text=new_text, fill=new_fg)
        self._anim_to(new_bg)

    def set_enabled(self, enabled):
        self.enabled = enabled
        self._apply(self.bg if enabled else "#B0BEC5")

# ========== 圆角背景工具：用 Canvas 四角圆弧模拟圆角矩形（Tkinter 原生不支持圆角） ==========
def _draw_rounded(cv, w, h, r, fill, outline=""):
    """核心绘制函数：4 个圆弧 + 2 个矩形拼接成圆角矩形"""
    cv.delete("all")
    if w < 4 or h < 4:
        return
    cv.create_arc((1, 1, r*2, r*2), start=90, extent=90, fill=fill, outline=outline)
    cv.create_arc((w-r*2-1, 1, w-1, r*2), start=0, extent=90, fill=fill, outline=outline)
    cv.create_arc((1, h-r*2-1, r*2, h-1), start=180, extent=90, fill=fill, outline=outline)
    cv.create_arc((w-r*2-1, h-r*2-1, w-1, h-1), start=270, extent=90, fill=fill, outline=outline)
    cv.create_rectangle((r, 1, w-r, h-1), fill=fill, outline="")
    cv.create_rectangle((1, r, w-1, h-r), fill=fill, outline="")

def add_rounded_bg(parent, fill="#FFF", border="#E2E8F0", r=10):
    """给 Frame 叠加圆角背景（覆盖整个 Frame）"""
    cv = tk.Canvas(parent, bg=parent["bg"], highlightthickness=0, bd=0)
    cv.place(x=0, y=0, relwidth=1, relheight=1)
    cv.tk.call('lower', cv._w)
    def redraw(_=None):
        _draw_rounded(cv, cv.winfo_width(), cv.winfo_height(), r, fill, border)
    cv.bind("<Configure>", redraw)
    parent.after(20, redraw)
    return cv

def add_rounded_bg_widget(widget, fill="#FFF", r=8, pad=4, border=""):
    """给单独控件（Entry/Text）后面添加圆角背景，带内边距"""
    parent = widget.master
    cv = tk.Canvas(parent, bg=parent["bg"], highlightthickness=0, bd=0)
    def redraw(_=None):
        try:
            x, y = widget.winfo_x() - pad, widget.winfo_y() - pad
            w, h = widget.winfo_width() + 2*pad, widget.winfo_height() + 2*pad
        except:
            return
        cv.place(x=x, y=y, width=w, height=h)
        _draw_rounded(cv, w, h, r, fill, border)
        try:
            cv.tk.call('lower', cv._w, widget._w)
        except:
            pass
    widget.bind("<Configure>", redraw)
    parent.bind("<Configure>", redraw)
    widget.after(20, redraw)
    return cv

# ========== 主 GUI 窗口：输入 → 设置 → 结果三行卡片布局 ==========
class YoudaoDictApp:
    PLACEHOLDER = "请输入英文或中文单词..."
    PH_COLOR = "#9CA3AF"
    TXT_COLOR = "#2C3E50"

    def __init__(self, root, dpi=1.0):
        self.root = root
        root.title("词典查询-V1.1.0")
        try:
            root.tk.call('tk', 'scaling', 1.0)
        except:
            pass

        s = lambda v: max(int(v * dpi), 1)  # 缩放系数
        bw, bh = s(72), s(30)  # 按钮基准尺寸
        root.geometry(f"{s(420)}x{s(300)}")  # 横向加宽让按钮完全显示，纵向略微减小
        root.resizable(True, True)
        root.configure(bg="#F5F6FA")
        root.attributes("-topmost", False)
        self._top = False

        C = {"card": "#FFF", "dark": "#2C3E50", "gray": "#7F8C8D",
             "accent": "#4A90D9", "hover": "#5BA0E9", "border": "#E2E8F0"}
        self._C = C
        self._f = lambda base: ("Segoe UI", s(base + 4))  # 字号基准 +4（不改变窗口）
        f = self._f

        # ---- 输入行 ----
        top = self._card(root, (12, 6))
        tk.Label(top, text="单词/中文", font=f(14), bg=C["card"], fg=C["dark"]).pack(side=tk.LEFT)
        self.entry = tk.Entry(top, width=20, font=f(12), relief="flat", bg="#F0F2F5",
                              fg=self.PH_COLOR, insertbackground=C["accent"], bd=0, highlightthickness=0)
        self.entry.pack(side=tk.LEFT, padx=8, ipady=4)
        add_rounded_bg_widget(self.entry, "#F0F2F5", 8, 4)
        self.entry.insert(0, self.PLACEHOLDER)
        self.entry.bind("<FocusIn>", self._on_focus_in)
        self.entry.bind("<FocusOut>", self._on_focus_out)
        self.entry.bind("<Return>", lambda e: self.query())

        self.btn_q = ModernButton(top, "查询", self.query, bw, bh,
                                   bg=C["accent"], hover_bg=C["hover"],
                                   font=("Segoe UI", s(9+6), "bold"))
        self.btn_q.pack(side=tk.LEFT)

        # 缓存容量显示
        self.lbl_cache = tk.Label(top, text="", font=f(5), bg=C["card"], fg="#10B981")
        self.lbl_cache.pack(side=tk.LEFT, padx=(6, 0))

        # ---- 设置行：模式切换 + 数据源下拉 + 功能按钮 ----
        src = self._card(root, (0, 8))
        sty = ttk.Style()
        sty.configure("M.TRadiobutton", background=C["card"], font=f(9))

        self.mode_var = tk.StringVar(value="word")
        for t, v in [("单词查询", "word"), ("句子翻译", "sentence")]:
            rb = ttk.Radiobutton(src, text=t, variable=self.mode_var, value=v,
                                 style="M.TRadiobutton", command=self._on_mode_change)
            rb.pack(side=tk.LEFT, padx=(0, 16))

        # 数据源下拉菜单（Menubutton + Menu，字体绝对可控）
        self._src_map = {"有道词典": "youdao", "百度翻译": "baidu", "有道翻译": "youdao_translate"}
        self.src_var = tk.StringVar(value="有道词典")
        # 主按钮：与输入框风格一致（浅灰背景、扁平、圆角）
        self.src_combo = tk.Menubutton(src, textvariable=self.src_var, font=f(9),
            bg="#F0F2F5", fg=C["dark"], activebackground="#E0E2E5",
            relief="flat", highlightthickness=0, borderwidth=0,
            padx=10, pady=4, cursor="hand2", indicatoron=True)
        add_rounded_bg_widget(self.src_combo, "#F0F2F5", 8, 4)
        # 下拉菜单
        self._combo_menu = tk.Menu(self.src_combo, tearoff=0, font=f(9),
            bg=C["card"], fg=C["dark"],
            activebackground=C["accent"], activeforeground="white",
            bd=1, relief="solid")
        for label in ("有道词典", "百度翻译"):
            self._combo_menu.add_command(label=label,
                command=lambda v=label: self.src_var.set(v))
        self.src_combo.config(menu=self._combo_menu)
        self.src_combo.pack(side=tk.LEFT, padx=(0, 16))

        self.btn_top = ModernButton(src, "置顶", self._toggle_top, s(60), s(26),
                                     bg="#E8F0FE", hover_bg="#D0E4F7", fg=C["accent"], font=f(9))
        self.btn_top.pack(side=tk.LEFT, padx=(16, 0))

        self.btn_cache = ModernButton(src, "查看缓存", self._show_cache, s(60), s(26),
                                       bg="#E8F0FE", hover_bg="#D0E4F7", fg=C["accent"], font=f(9))
        self.btn_cache.pack(side=tk.LEFT, padx=(8, 0))

        # ---- 结果区 ----
        res = self._card(root, (0, 12), expand=True)
        self.text = scrolledtext.ScrolledText(res, wrap=tk.WORD, font=f(11),
            bg="#FAFBFC", fg=C["dark"], relief="flat", bd=0, highlightthickness=0,
            padx=12, pady=12)
        self.text.pack(expand=True, fill=tk.BOTH)
        add_rounded_bg_widget(self.text, "#FAFBFC", 10, 6)
        welcome = (
            "欢迎使用词典查询工具!\n"
            "  版本号V1.1.0\n"
            "  更新日志\n"
            "    \u00b7优化了UI界面\n"
            "    \u00b7按钮悬停和点击新增颜色过渡动画\n"
            "    \u00b7新增查看缓存表格（双击行自动查询）\n"
            "    \u00b7新增有道翻译(句子)模式，支持整句翻译\n"
            "    \u00b7优化了查询逻辑，运行更快\n"
            "    \u00b7修复了部分已知问题"
        )
        self.text.insert(tk.END, welcome)
        self.text.config(state=tk.DISABLED)

        self.entry.focus_set()
        self.entry.icursor(0)

    def _card(self, parent, pady, expand=False):
        """复用卡片容器：带圆角背景的 Frame"""
        f = tk.Frame(parent, bg=self._C["card"], bd=0, highlightthickness=0)
        f.pack(pady=pady, padx=12, fill=tk.BOTH if expand else tk.X, expand=expand)
        inner = tk.Frame(f, bg=self._C["card"])
        inner.pack(pady=12, padx=14, fill=tk.BOTH if expand else tk.X, expand=expand)
        add_rounded_bg(f, self._C["card"], self._C["border"])
        return inner

    def _on_mode_change(self):
        """切换模式时更新下拉菜单选项"""
        self._combo_menu.delete(0, "end")
        if self.mode_var.get() == "sentence":
            self._combo_menu.add_command(label="有道翻译",
                command=lambda: self.src_var.set("有道翻译"))
            self.src_var.set("有道翻译")
        else:
            for label in ("有道词典", "百度翻译"):
                self._combo_menu.add_command(label=label,
                    command=lambda v=label: self.src_var.set(v))
            self.src_var.set("有道词典")

    def _on_focus_in(self, e):
        """输入框聚焦时清空占位文字"""
        if self.entry.get() == self.PLACEHOLDER:
            self.entry.delete(0, tk.END)
            self.entry.config(fg=self.TXT_COLOR)

    def _on_focus_out(self, e):
        """输入框失焦时恢复占位文字"""
        if not self.entry.get().strip():
            self.entry.delete(0, tk.END)
            self.entry.insert(0, self.PLACEHOLDER)
            self.entry.config(fg=self.PH_COLOR)

    def _toggle_top(self):
        """切换窗口置顶，带动画"""
        self._top = not self._top
        self.root.attributes("-topmost", self._top)
        if self._top:
            self.btn_top.transition_to(self._C["accent"], "#5BA0E9", "white", "取消置顶")
        else:
            self.btn_top.transition_to("#E8F0FE", "#D0E4F7", self._C["accent"], "置顶")

    def _show_cache(self):
        """弹出缓存窗口，Treeview 表格展示，双击自动查询"""
        if cache.size() == 0:
            messagebox.showinfo("缓存为空", "当前还没有查询过任何单词。")
            return
        dpi = get_dpi_scale()
        s = lambda v: int(v * dpi)
        win = tk.Toplevel(self.root)
        win.title("缓存单词列表")
        win.geometry(f"{s(620)}x{s(420)}")
        win.configure(bg="#F5F6FA")
        win.resizable(True, True)
        win.transient(self.root)
        win.grab_set()

        info = self._card(win, (12, 4))
        tk.Label(info, text=f"共 {cache.size()} 个缓存条目 — 双击行可自动查询",
                 font=("Segoe UI", s(13)), bg="#FFF", fg="#7F8C8D").pack()

        lf = tk.Frame(win, bg="#FFF")
        lf.pack(pady=(4, 12), padx=12, expand=True, fill=tk.BOTH)
        inner = tk.Frame(lf, bg="#FFF")
        inner.pack(pady=10, padx=10, expand=True, fill=tk.BOTH)
        add_rounded_bg(lf, "#FFF", "#E2E8F0", 10)

        tree = ttk.Treeview(inner, columns=("w", "s", "sum"), show="headings", selectmode="browse")
        sty = ttk.Style()
        sty.configure("C.Treeview", font=("Segoe UI", s(14)), rowheight=s(36),
                       background="#FAFBFC", fieldbackground="#FAFBFC", foreground="#2C3E50")
        sty.configure("C.Treeview.Heading", font=("Segoe UI", s(14), "bold"), relief="flat")
        tree.configure(style="C.Treeview")
        for col, txt, w in [("w", "单词", s(160)), ("s", "来源", s(100)), ("sum", "简要", s(280))]:
            tree.heading(col, text=txt)
            tree.column(col, width=w, minwidth=s(60))
        vsb = ttk.Scrollbar(inner, orient=tk.VERTICAL, command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.pack(side=tk.LEFT, expand=True, fill=tk.BOTH)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)

        rows = []
        for key, val in cache.cache.items():
            src, wd = key.split(":", 1)
            brief = "；".join([l.lstrip("\u2022- ") for l in val.split("\n") if l.strip().startswith(("\u2022", "-"))][:3])
            if not brief:
                brief = val.split("\n")[0][:60]
            tree.insert("", tk.END, values=(wd, src, brief[:80]))
            rows.append((wd, src))

        def ondbl(_):
            sel = tree.selection()
            if not sel:
                return
            idx = tree.index(sel[0])
            if idx < len(rows):
                wd, src = rows[idx]
                self.entry.delete(0, tk.END)
                self.entry.config(fg=self.TXT_COLOR)
                self.entry.insert(0, wd)
                # 根据缓存的数据源自动切换模式和下拉菜单
                display = {"youdao": "有道词典", "baidu": "百度翻译",
                           "youdao_translate": "有道翻译"}.get(src, "有道词典")
                self.mode_var.set("sentence" if src == "youdao_translate" else "word")
                self._on_mode_change()
                self.src_var.set(display)
                win.destroy()
                self.query()
        tree.bind("<Double-1>", ondbl)

        btn = ModernButton(win, "关闭", win.destroy, s(80), s(30),
                            font=("Segoe UI", s(9), "bold"))
        btn.pack(pady=(0, 12))

    def _get_source(self):
        """从下拉菜单获取实际数据源标识"""
        txt = self.src_var.get()
        return self._src_map.get(txt, "youdao")

    def query(self):
        """主查询入口：校验 → 缓存命中直接显示 → 否则开子线程"""
        word = self.entry.get().strip()
        if not word or word == self.PLACEHOLDER:
            messagebox.showwarning("提示", "请输入单词或中文")
            return
        source = self._get_source()
        cached = cache.get(word, source)
        if cached:
            self._show_result(cached)
            return
        self._set_ui_state(tk.DISABLED)
        self.btn_q.set_enabled(False)
        threading.Thread(target=self._run_query, args=(word, source), daemon=True).start()

    def _run_query(self, word, source):
        """后台线程：网络请求 → 切回主线程更新 UI"""
        result, _ = fetch_word_info(word, source)
        self.root.after(0, self._show_result, result)

    def _show_result(self, result):
        """显示结果到文本框，更新缓存标签"""
        self.text.config(state=tk.NORMAL)
        self.text.delete(1.0, tk.END)
        self.text.insert(tk.END, result)
        self.text.config(state=tk.DISABLED)
        if result.startswith("[错误]"):
            self.lbl_cache.config(text="")
            self.root.after(500, self._reset_ui)
        else:
            self.lbl_cache.config(text=f"\u25cf 缓存({cache.size()}/50)")
            self._reset_ui()

    def _reset_ui(self):
        """恢复控件到可交互状态"""
        self.btn_q.set_enabled(True)
        self.entry.config(state=tk.NORMAL)
        self.entry.select_range(0, tk.END)
        self.entry.focus_set()

    def _set_ui_state(self, state):
        self.entry.config(state=state)

if __name__ == "__main__":
    root = tk.Tk()
    app = YoudaoDictApp(root, get_dpi_scale())
    root.mainloop()
