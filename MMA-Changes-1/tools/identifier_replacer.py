# identifier_replacer.py

"""
Identifier Replacer（仅 .py）

功能概述
---------
- 递归扫描指定目录（默认 backend/app）中的 .py 文件；
- **按映射批量替换**：每行“标识符=数值”（支持 , = : 空格 作为分隔）；
- **仅替换数值右值**：形如
    name = 数字
    name: <type> = 数字
    obj.name = 数字
    调用形参 name=数字  (func(..., name=123))
- **可排除函数**：在这些函数调用内出现的参数不替换（默认排除 get_iopub_msg）；
- **扫描当前值**：不修改，仅列出当前这些标识符被赋的数值（含文件/行号/上下文/汇总）；
- 使用 tokenize，自动规避字符串与注释；
- 支持为改动文件创建 .bak 备份与一键恢复；
- **新增**：一键清理当前根目录下的所有 .bak 文件（递归）；
- GUI 基于 ttkbootstrap。

使用提示
---------
左侧文本框：每行一对 “标识符 与 数值”，如：
    MAX_CHAT_TURNS,60
    max_retries=5
    timeout : 3000
预览无误后再应用修改。
"""

from __future__ import annotations

import io
import os
import re
import sys
import shutil
from pathlib import Path
from typing import List, Tuple, Dict, Set

from tkinter import *
import tkinter.filedialog as filedialog
from tkinter.scrolledtext import ScrolledText
import ttkbootstrap as ttk

# =========================
# 配置常量
# =========================
DATA_CONFIG = {
    "app": None,
    "window": None,
    "screen": None,
    "py_path": os.path.dirname(os.path.abspath(__file__)),
}

SCREEN_CONFIG = {"borderwidth": 5, "relief": "raised"}
MAIN_FRAME_CONFIG = {"borderwidth": 5, "relief": "sunken"}
FLAT_FRAME_CONFIG = {"borderwidth": 0}

DEFAULT_ROOT = "backend/app"
EXCLUDE_DIRS = {
    ".git",
    ".svn",
    ".hg",
    "__pycache__",
    "node_modules",
    ".venv",
    "venv",
    "dist",
    "build",
    ".mypy_cache",
    ".pytest_cache",
    ".idea",
    ".vscode",
}
DEFAULT_EXCLUDE_CALLS = {"get_iopub_msg"}  # 在这些函数调用内的参数不替换
NUMERIC_RE = re.compile(r"\d+(?:\.\d+)?(?:[eE][+-]?\d+)?")  # 合法数字（整数/小数/科学计数）


# =========================
# 文件与文本工具
# =========================
def read_text_safely(p: Path) -> Tuple[str, str]:
    """尽量以正确编码读取文本；失败则忽略错误。"""
    for enc in ("utf-8", "utf-8-sig", "gb18030", "latin-1"):
        try:
            return p.read_text(encoding=enc), enc
        except UnicodeDecodeError:
            continue
        except Exception:
            break
    return p.read_text(errors="ignore"), "utf-8"


def write_text_safely(p: Path, text: str, encoding: str) -> None:
    p.write_text(text, encoding=encoding)


def iter_py_files(root: Path):
    """遍历根目录下的 .py 文件（排除常见无关目录）。"""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        for fn in filenames:
            if fn.endswith(".py"):
                yield Path(dirpath) / fn


def delete_bak_files_recursively(root: Path, log_func=None) -> int:
    """
    递归删除 root 下所有以 .bak 结尾的文件。
    返回删除数量。log_func 若提供，按行输出日志。
    """
    removed = 0
    for dirpath, dirnames, filenames in os.walk(root):
        # 过滤无关目录
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_DIRS]
        for fn in filenames:
            if fn.endswith(".bak"):
                fp = Path(dirpath) / fn
                try:
                    fp.unlink(missing_ok=True)
                    removed += 1
                    if log_func:
                        log_func(f"[已删除] {fp}\n")
                except Exception as e:
                    if log_func:
                        log_func(f"[删除失败] {fp}  ({e})\n")
    return removed


# =========================
# 代码分析/替换引擎
# =========================
class PyIdentTransformer:
    """纯逻辑引擎：负责扫描与替换，不涉及 GUI。"""

    @staticmethod
    def nearest_call_name(tokens, idx_target: int) -> str | None:
        """
        从目标 token 向前回溯，找其最近的“(”并取其前的 NAME（函数名）。
        用于判断“name=数字”是否位于某函数调用实参列表中。
        """
        from token import OP, NAME, NL, INDENT, DEDENT

        depth = 0
        j = idx_target - 1
        while j >= 0:
            t = tokens[j]
            if t.type == OP:
                if t.string == ")":
                    depth += 1
                elif t.string == "(":
                    if depth == 0:
                        k = j - 1
                        last_name = None
                        while k >= 0:
                            tk = tokens[k]
                            if tk.type in (NL, INDENT, DEDENT):
                                k -= 1
                                continue
                            if tk.type == NAME:
                                last_name = tk.string
                                break
                            elif tk.type == OP and tk.string in (".", "]"):
                                k -= 1
                                continue
                            else:
                                break
                            k -= 1
                        return last_name
                    else:
                        depth -= 1
            j -= 1
        return None

    @staticmethod
    def transform_source(src: str, mapping: Dict[str, str], exclude_calls: Set[str]) -> Tuple[str, List[dict], int]:
        """
        替换源码中 mapping 指定的“标识符 = 数字”右值；也支持
        标识符右侧为函数调用（如 Field(default=60) / Field(60)）的场景。
        返回：(new_src, changes, count)
        """
        import tokenize
        from token import NAME, OP, NUMBER, NL, NEWLINE, INDENT, DEDENT

        lines = src.splitlines(keepends=True)
        tokens = list(tokenize.generate_tokens(io.StringIO(src).readline))
        tokens_mut = tokens[:]

        changes: List[dict] = []
        count = 0
        i = 0

        while i < len(tokens_mut):
            tok = tokens_mut[i]
            if tok.type == NAME and tok.string in mapping:
                new_value = mapping[tok.string]
                saw_colon = False
                eq_idx = None

                # 向后找到 '='（同一行内）
                k = i + 1
                while k < len(tokens_mut):
                    t = tokens_mut[k]
                    if t.type in (NL, NEWLINE):
                        break
                    if t.type == OP and t.string == ":":
                        saw_colon = True
                    if t.type == OP and t.string == "=":
                        eq_idx = k
                        break
                    k += 1

                if eq_idx is None:
                    i += 1
                    continue

                # '=' 右侧第一个非格式 token
                m = eq_idx + 1
                while m < len(tokens_mut) and tokens_mut[m].type in (NL, INDENT, DEDENT):
                    m += 1

                target_num_idx = None
                old_num = None
                replaced_inside_call = False
                func_name_for_repr = None

                # 情况 1：直接是数字
                if m < len(tokens_mut) and tokens_mut[m].type == NUMBER:
                    target_num_idx = m
                    old_num = tokens_mut[m].string

                # 情况 2：右侧是函数调用（如 Field(...)）
                elif (
                    m + 1 < len(tokens_mut)
                    and tokens_mut[m].type == NAME
                    and tokens_mut[m + 1].type == OP
                    and tokens_mut[m + 1].string == "("
                ):
                    func_name_for_repr = tokens_mut[m].string
                    # 在该圆括号对内查找：优先 default=数字；否则取第一层级(深度1)出现的第一个数字
                    depth = 0
                    j = m + 1
                    first_number_idx = None
                    default_number_idx = None
                    while j < len(tokens_mut):
                        t = tokens_mut[j]
                        if t.type == OP and t.string == "(":
                            depth += 1
                        elif t.type == OP and t.string == ")":
                            depth -= 1
                            if depth == 0:
                                break

                        if depth >= 1:
                            # default=NUMBER
                            if t.type == NAME and t.string == "default":
                                j2 = j + 1
                                while j2 < len(tokens_mut) and tokens_mut[j2].type in (NL, INDENT, DEDENT):
                                    j2 += 1
                                if j2 < len(tokens_mut) and tokens_mut[j2].type == OP and tokens_mut[j2].string == "=":
                                    j3 = j2 + 1
                                    while j3 < len(tokens_mut) and tokens_mut[j3].type in (NL, INDENT, DEDENT):
                                        j3 += 1
                                    if j3 < len(tokens_mut) and tokens_mut[j3].type == NUMBER:
                                        default_number_idx = j3
                                        break
                            # 记录深度1的第一个数字（作为兜底，例如 Field(60)）
                            if depth == 1 and t.type == NUMBER and first_number_idx is None:
                                first_number_idx = j
                        j += 1

                    target_num_idx = default_number_idx if default_number_idx is not None else first_number_idx
                    if target_num_idx is not None:
                        old_num = tokens_mut[target_num_idx].string
                        replaced_inside_call = True

                # 若找到了数字目标，就执行替换
                if target_num_idx is not None and old_num is not None:
                    # 如果该赋值其实是“某函数的参数”，并且该函数在排除名单中，则跳过
                    call_name = PyIdentTransformer.nearest_call_name(tokens_mut, i)
                    if call_name and call_name in exclude_calls:
                        i += 1
                        continue

                    if NUMERIC_RE.fullmatch(old_num):
                        tokens_mut[target_num_idx] = tokenize.TokenInfo(
                            type=NUMBER,
                            string=str(new_value),
                            start=tokens_mut[target_num_idx].start,
                            end=tokens_mut[target_num_idx].end,
                            line=tokens_mut[target_num_idx].line,
                        )
                        count += 1

                        line_no = tok.start[0]
                        raw_line = lines[line_no - 1] if 0 <= line_no - 1 < len(lines) else ""
                        preview_line = raw_line.replace(old_num, str(new_value), 1) if old_num in raw_line else raw_line

                        if replaced_inside_call and func_name_for_repr:
                            ctx_old = (
                                f"{tok.string}{': ...' if saw_colon else ''} = {func_name_for_repr}(...{old_num}...)"
                            )
                            ctx_new = (
                                f"{tok.string}{': ...' if saw_colon else ''} = {func_name_for_repr}(...{new_value}...)"
                            )
                        else:
                            ctx_old = f"{tok.string}{': ...' if saw_colon else ''} = {old_num}"
                            ctx_new = f"{tok.string}{': ...' if saw_colon else ''} = {new_value}"

                        changes.append(
                            {
                                "lineno": line_no,
                                "old_repr": ctx_old,
                                "new_repr": ctx_new,
                                "context": preview_line.rstrip("\n"),
                            }
                        )

            i += 1

        if count == 0:
            return src, changes, 0
        new_src = tokenize.untokenize(tokens_mut)
        return new_src, changes, count

    @staticmethod
    def extract_values(src: str, idents: Set[str], exclude_calls: Set[str]) -> List[dict]:
        """
        仅扫描、提取当前值（不修改）：
        返回 [{name, value, lineno, context}]
        """
        import tokenize
        from token import NAME, OP, NUMBER, NL, NEWLINE, INDENT, DEDENT

        lines = src.splitlines(keepends=True)
        tokens = list(tokenize.generate_tokens(io.StringIO(src).readline))
        out: List[dict] = []

        i = 0
        while i < len(tokens):
            tok = tokens[i]
            if tok.type == NAME and tok.string in idents:
                saw_colon = False
                eq_idx = None
                num_idx = None
                k = i + 1
                while k < len(tokens):
                    t = tokens[k]
                    if t.type in (NL, NEWLINE):
                        break
                    if t.type == OP and t.string == ":":
                        saw_colon = True
                    if t.type == OP and t.string == "=":
                        eq_idx = k
                        m = k + 1
                        while m < len(tokens) and tokens[m].type in (NL, INDENT, DEDENT):
                            m += 1
                        if m < len(tokens) and tokens[m].type == NUMBER:
                            num_idx = m
                        break
                    k += 1

                if eq_idx is not None and num_idx is not None:
                    call_name = PyIdentTransformer.nearest_call_name(tokens, i)
                    if call_name and call_name in exclude_calls:
                        i += 1
                        continue
                    val = tokens[num_idx].string
                    if NUMERIC_RE.fullmatch(val):
                        line_no = tok.start[0]
                        raw_line = lines[line_no - 1] if 0 <= line_no - 1 < len(lines) else ""
                        out.append(
                            {
                                "name": tok.string,
                                "value": val,
                                "lineno": line_no,
                                "context": raw_line.rstrip("\n"),
                            }
                        )
            i += 1
        return out


# =========================
# UI 组件
# =========================
class TextWidget(ttk.Frame):
    """右侧日志窗口"""

    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)
        self.textbox = ScrolledText(self, undo=True)
        self.textbox.place(relx=0, rely=0, relwidth=1, relheight=1)
        self.textbox.config(state="normal")
        self.textbox.bind("<Control-a>", self._select_all)
        self.textbox.bind("<Control-z>", lambda e: self.textbox.edit_undo())

    def clear(self):
        self.textbox.delete("1.0", "end")

    def set(self, s: str):
        self.clear()
        self.textbox.insert("end", s)

    def log(self, s: str):
        self.textbox.insert("end", s)
        self.textbox.see("end")

    def _select_all(self, event):
        self.textbox.tag_add("sel", "1.0", "end")
        return "break"


# =========================
# 主界面
# =========================
class IdentifierReplacer(ttk.Frame):
    """
    通用标识符替换器（GUI）
    - 左侧：范围与操作 / 参数 / 使用说明
    - 右侧：日志输出
    """

    def __init__(self, master):
        super().__init__(master, **SCREEN_CONFIG)
        self.place(relx=0, rely=0, relwidth=1, relheight=1)

        # UI 变量
        self.root_dir = StringVar(master=master, value=str(Path(DEFAULT_ROOT)))
        self.make_backup = BooleanVar(master=master, value=True)
        self.exclude_calls = StringVar(master=master, value=",".join(sorted(DEFAULT_EXCLUDE_CALLS)))

        self.target_names_text: ScrolledText | None = None  # 多行映射输入
        # 关键：初始化为 None，这样“未预览”时 apply() 能识别到需要即时扫描
        self.preview_cache: Dict[Path, Tuple[str, str, List[dict]]] | None = None

        # 引擎
        self.engine = PyIdentTransformer()

        self._build_ui()

    # ----- 建 UI -----
    def _build_ui(self):
        """左右可拖拽（单 PanedWindow）；左侧三块，右侧日志。"""
        self.main_paned = ttk.PanedWindow(self, orient="horizontal")
        self.main_paned.place(relx=0, rely=0, relwidth=1, relheight=1)

        left = ttk.Frame(self, borderwidth=0)
        right = ttk.Frame(self, **MAIN_FRAME_CONFIG)

        self.main_paned.add(left, weight=44)  # 左右权重
        self.main_paned.add(right, weight=56)

        # 初始化分隔条位置：首次 <Configure> 时按容器宽度 44% 设置
        self._sash_initialized = False
        self.main_paned.bind("<Configure>", self._on_first_configure)

        left.columnconfigure(0, weight=1)
        left.rowconfigure(0, minsize=190, weight=0)
        left.rowconfigure(1, weight=0)
        left.rowconfigure(2, weight=1)

        # --- 顶部：范围与操作 ---
        top_card = ttk.Labelframe(left, text="范围与操作", bootstyle="primary", padding=8)
        top_card.grid(row=0, column=0, sticky="ew", padx=6, pady=(6, 4))
        top_card.columnconfigure(0, weight=1)

        row_dir = ttk.Frame(top_card)
        row_dir.grid(row=0, column=0, sticky="ew")
        row_dir.columnconfigure(1, weight=1)
        ttk.Label(row_dir, text="扫描根目录：").grid(row=0, column=0, sticky="w", padx=(2, 6), pady=(2, 6))
        ttk.Entry(row_dir, textvariable=self.root_dir).grid(row=0, column=1, sticky="we", pady=(2, 6))
        ttk.Button(row_dir, text="浏览…", command=self._browse_dir).grid(
            row=0, column=2, sticky="w", padx=(6, 0), pady=(2, 6)
        )

        ttk.Separator(top_card, orient="horizontal").grid(row=1, column=0, sticky="ew", pady=4)

        btns = ttk.Frame(top_card)
        btns.grid(row=2, column=0, sticky="ew")
        # 这里原来是 range(4)，改成 3；再加 uniform 让三列等宽
        for c in range(3):
            btns.columnconfigure(c, weight=1, uniform="btns")

        # 下面保持 3 列布局即可，按钮会自动拉满整行宽度
        ttk.Button(btns, text="扫描当前值", command=self.scan_current_values).grid(
            row=0, column=0, sticky="nsew", padx=4, pady=4
        )
        ttk.Button(btns, text="预览变更", command=self.preview_changes).grid(
            row=0, column=1, sticky="nsew", padx=4, pady=4
        )
        ttk.Button(btns, text="应用修改", command=self.apply_changes).grid(
            row=0, column=2, sticky="nsew", padx=4, pady=4
        )

        ttk.Button(btns, text="恢复 .bak", command=self.restore_backups).grid(
            row=1, column=0, sticky="nsew", padx=4, pady=4
        )
        ttk.Button(btns, text="清理 .bak", command=self.clean_bak_files).grid(
            row=1, column=1, sticky="nsew", padx=4, pady=4
        )
        ttk.Button(btns, text="打开目录", command=self.open_dir).grid(row=1, column=2, sticky="nsew", padx=4, pady=4)

        # --- 参数卡 ---
        param_card = ttk.Labelframe(left, text="参数", bootstyle="secondary", padding=8)
        param_card.grid(row=1, column=0, sticky="ew", padx=6, pady=(0, 4))
        param_card.columnconfigure(0, weight=0)
        param_card.columnconfigure(1, weight=1)

        ttk.Label(param_card, text="标识符=数值\n每行一对\n, = : 或空格分隔均可").grid(
            row=0, column=0, sticky="nw", padx=(2, 6), pady=(2, 4)
        )
        self.target_names_text = ScrolledText(param_card, height=8, undo=True)
        self.target_names_text.grid(row=0, column=1, sticky="we", padx=(0, 6), pady=(2, 4))
        default_pairs = [
            "max_chat_turns,600",
            "MAX_CHAT_TURNS,600",
            "max_retries,50",
            "MAX_RETRIES,50",
            "DEFAULT_MAX_RETRIES,50",
            "timeout,36000",
        ]
        self.target_names_text.insert("end", "\n".join(default_pairs))

        ttk.Label(param_card, text="排除函数（逗号分隔，可为空）：").grid(
            row=1, column=0, sticky="w", padx=(2, 6), pady=(2, 4)
        )
        ttk.Entry(param_card, textvariable=self.exclude_calls, width=40).grid(
            row=1, column=1, sticky="we", padx=(0, 6), pady=(2, 4)
        )
        ttk.Checkbutton(param_card, text="为改动文件创建 .bak 备份", variable=self.make_backup).grid(
            row=2, column=0, columnspan=2, sticky="w", padx=(2, 6), pady=(0, 4)
        )

        # --- 说明 ---
        tips_wrap = ttk.Frame(left, borderwidth=0)
        tips_wrap.grid(row=2, column=0, sticky="nsew", padx=6, pady=(0, 6))
        tips = Text(tips_wrap, wrap="word", relief="flat", bd=0, highlightthickness=0, cursor="arrow", takefocus=0)
        tips.place(relx=0, rely=0, relwidth=1, relheight=1)
        tips.insert(
            "end",
            "使用说明：\n"
            "• “扫描当前值”只查看，不修改；“预览变更/应用修改”按映射替换。\n"
            "• 每行一对：标识符 与 数值\n"
            "• 分隔符：逗号 , / 等号 = / 空格 / 冒号 :。\n"
            "• 仅替换右值为数字的赋值或调用参数\n"
            "• 可在“排除函数”里写入不希望替换其参数的函数名。\n"
            "• 若不再需要备份文件，可点击“清理 .bak”一键删除。\n",
        )
        tips.configure(state="disabled")

        # --- 右侧日志 ---
        self.log = TextWidget(right, **MAIN_FRAME_CONFIG)
        self.log.place(relx=0, rely=0, relwidth=1, relheight=1)
        self._log("准备就绪：可先点“扫描当前值”查看，再预览/应用修改。\n")

    # ----- 窗口初始化分隔条位置 -----
    def _on_first_configure(self, event=None):
        if getattr(self, "_sash_initialized", False):
            return
        try:
            width = self.main_paned.winfo_width()
            if width and width > 1:
                self._safe_set_sashpos(0, int(width * 0.44))
                self._sash_initialized = True
            else:
                self.after(120, self._on_first_configure)
        except Exception:
            self.after(200, self._on_first_configure)

    def _safe_set_sashpos(self, idx: int, px: int):
        try:
            self.main_paned.sashpos(idx, px)
        except Exception:
            try:
                self.main_paned.sash_place(idx, px, 0)
            except Exception:
                pass

    # ----- 行为：扫描当前值 -----
    def scan_current_values(self):
        """扫描并显示当前值（不改动文件）"""
        from tkinter import messagebox
        from collections import defaultdict, Counter

        self.log.clear()
        root = Path(self.root_dir.get()).resolve()
        if not root.exists():
            messagebox.showerror("错误", f"目录不存在：{root}")
            return

        # 允许只写标识符
        idents = self._parse_idents_from_text()
        if not idents:
            messagebox.showerror("错误", "请在左侧文本框至少写一个标识符（如 MAX_CHAT_TURNS）。")
            return

        exclude_calls = set([s.strip() for s in self.exclude_calls.get().split(",") if s.strip()])
        self._log(
            f"开始扫描当前值：根目录 = {root}\n标识符 = {', '.join(sorted(idents))}"
            f"\n排除函数 = {', '.join(sorted(exclude_calls)) or '无'}\n\n"
        )

        results: Dict[Path, List[dict]] = {}
        for fp in iter_py_files(root):
            try:
                text, _ = read_text_safely(fp)
            except Exception as e:
                self._log(f"[跳过] 读取失败: {fp} ({e})\n")
                continue
            rows = self.engine.extract_values(text, idents, exclude_calls)
            if rows:
                results[fp] = rows

        if not results:
            self._log("未发现任何匹配的赋值或调用形参（数值型）。\n")
            return

        from collections import defaultdict, Counter

        summary = defaultdict(Counter)
        files_touched = 0
        total_hits = 0
        for fp, rows in sorted(results.items(), key=lambda kv: str(kv[0])):
            files_touched += 1
            self._log(f"[文件] {fp}\n")
            for r in rows:
                total_hits += 1
                summary[r["name"]][r["value"]] += 1
                self._log(f"  行 {r['lineno']:>4}: {r['name']} = {r['value']}\n")
                self._log(f"          {r['context']}\n")
            self._log("\n")

        self._log("========== 当前值汇总 ==========\n")
        self._log(f"涉及文件数：{files_touched}\n")
        self._log(f"命中总数：{total_hits}\n")
        for name in sorted(summary.keys()):
            pairs = ", ".join([f"{val}×{cnt}" for val, cnt in summary[name].most_common()])
            self._log(f"{name}: {pairs}\n")

    def _parse_idents_from_text(self) -> Set[str]:
        """
        仅解析标识符集合（用于‘扫描当前值’）。
        允许每行只写标识符，也允许写成 ident=123 之类，都会取左边的 ident。
        """
        raw_text = self.target_names_text.get("1.0", "end") if self.target_names_text else ""
        idents: Set[str] = set()
        for line in raw_text.splitlines():
            s = line.strip()
            if not s:
                continue
            s = s.split("#", 1)[0].strip()
            if not s:
                continue
            # 取第一个字段作为标识符
            parts = re.split(r"[,\s=:]+", s)
            if parts and parts[0]:
                idents.add(parts[0].strip())
        return idents

    # ----- 行为：预览 -----
    def preview_changes(self):
        """根据映射生成替换预览，不写回文件。"""
        from tkinter import messagebox

        self.log.clear()
        root = Path(self.root_dir.get()).resolve()
        if not root.exists():
            messagebox.showerror("错误", f"目录不存在：{root}")
            return

        mapping = self._parse_mapping_from_text()
        if not mapping:
            messagebox.showerror("错误", "请至少填写一行：标识符 与 数值。")
            return

        # 校验每个 value 必须是数字
        bad = [f"{k}→{v}" for k, v in mapping.items() if not NUMERIC_RE.fullmatch(v)]
        if bad:
            messagebox.showerror("错误", "以下映射的数值非法（需为数字）：\n" + "\n".join(bad))
            return

        exclude_calls = set([s.strip() for s in self.exclude_calls.get().split(",") if s.strip()])
        pairs_show = ", ".join([f"{k}={v}" for k, v in mapping.items()])
        self._log(
            f"开始预览：根目录 = {root}\n映射 = {pairs_show}\n排除函数 = {', '.join(sorted(exclude_calls)) or '无'}\n\n"
        )

        results: Dict[Path, Tuple[str, str, List[dict]]] = {}
        for fp in iter_py_files(root):
            try:
                text, _ = read_text_safely(fp)
            except Exception as e:
                self._log(f"[跳过] 读取失败: {fp} ({e})\n")
                continue
            new_text, changes, cnt = self.engine.transform_source(text, mapping, exclude_calls)
            if cnt > 0 and changes:
                results[fp] = (text, new_text, changes)

        total_files = sum(1 for _ in iter_py_files(root))
        touched = len(results)
        total_changes = sum(len(v[2]) for v in results.values())

        if touched == 0:
            self._log("未发现可替换的位置。\n")
            return

        for fp, (_, _, changes) in sorted(results.items(), key=lambda kv: str(kv[0])):
            self._log(f"[文件] {fp}\n")
            for ch in changes:
                self._log(f"  行 {ch['lineno']:>4}: {ch['old_repr']}  →  {ch['new_repr']}\n")
                self._log(f"          {ch['context']}\n")
            self._log("\n")

        self._log("========== 预览汇总 ==========\n")
        self._log(f"扫描 .py 文件数：{total_files}\n")
        self._log(f"将修改文件数：{touched}\n")
        self._log(f"替换总次数：{total_changes}\n")
        self._log("（请确认无误后点击“应用修改”）\n")

        self.preview_cache = results

    # ----- 行为：应用 -----
    def apply_changes(self):
        """将预览或即时扫描的修改写回磁盘，支持 .bak 备份。"""
        from tkinter import messagebox

        root = Path(self.root_dir.get()).resolve()

        # 关键：None 或 {} 都会触发重新扫描
        results = getattr(self, "preview_cache", None)
        if not results:
            mapping = self._parse_mapping_from_text()
            if not mapping:
                messagebox.showerror("错误", "请先填写映射（每行：标识符 与 数值）。")
                return
            bad = [f"{k}→{v}" for k, v in mapping.items() if not NUMERIC_RE.fullmatch(v)]
            if bad:
                messagebox.showerror("错误", "以下映射的数值非法（需为数字）：\n" + "\n".join(bad))
                return
            exclude_calls = set([s.strip() for s in self.exclude_calls.get().split(",") if s.strip()])

            # 即时扫描，生成将要写回的结果
            results = {}
            for fp in iter_py_files(root):
                try:
                    text, _ = read_text_safely(fp)
                except Exception as e:
                    self._log(f"[跳过] 读取失败: {fp} ({e})\n")
                    continue
                new_text, changes, cnt = self.engine.transform_source(text, mapping, exclude_calls)
                if cnt > 0 and changes:
                    results[fp] = (text, new_text, changes)

        if not results:
            messagebox.showinfo("提示", "没有可应用的修改。")
            return

        do_backup = self.make_backup.get()
        applied_files = 0  # noqa: F841
        applied_changes = 0

        for fp, (_old_text, new_text, changes) in results.items():
            try:
                _, enc = read_text_safely(fp)
                if do_backup:
                    bak = fp.with_suffix(fp.suffix + ".bak")
                    try:
                        shutil.copy2(fp, bak)
                    except Exception as e:
                        self._log(f"[警告] 无法创建备份 {bak}: {e}\n")
                write_text_safely(fp, new_text, enc)
                applied_files += 1
                applied_changes += len(changes)
                self._log(f"[已修改] {fp}  替换 {len(changes)} 处\n")
            except Exception as e:
                self._log(f"[错误] 写入失败：{fp}  ({e})\n")

        self._log("\n========== 应用完成 ==========\n")
        self._log(f"修改文件数：{applied_files}\n")
        self._log(f"替换总次数：{applied_changes}\n")
        if do_backup:
            self._log("已为改动文件创建 .bak 备份。\n")

        from tkinter import messagebox

        messagebox.showinfo("完成", f"已修改 {applied_files} 个文件，共 {applied_changes} 处。")

        # 用完即清空，避免下次误用旧预览结果
        self.preview_cache = None

    # ----- 行为：恢复 .bak -----
    def restore_backups(self):
        """恢复同名 .py 文件的 .bak 备份"""
        from tkinter import messagebox

        root = Path(self.root_dir.get()).resolve()
        restored = 0
        scanned = 0

        for fp in iter_py_files(root):
            scanned += 1
            bak = fp.with_suffix(fp.suffix + ".bak")
            if bak.exists():
                try:
                    shutil.copy2(bak, fp)
                    restored += 1
                    self._log(f"[已恢复] {fp} ← {bak}\n")
                except Exception as e:
                    self._log(f"[错误] 恢复失败：{fp} ({e})\n")

        if restored == 0:
            messagebox.showinfo("提示", f"扫描 {scanned} 个 .py 文件，未发现可恢复的 .bak 文件。")
        else:
            messagebox.showinfo("完成", f"已恢复 {restored} 个文件。")

    # ----- 行为：清理 .bak -----
    def clean_bak_files(self):
        """递归删除当前根目录下所有 .bak 文件"""
        from tkinter import messagebox

        self.log.clear()
        root = Path(self.root_dir.get()).resolve()
        if not root.exists():
            messagebox.showerror("错误", f"目录不存在：{root}")
            return

        self._log(f"开始清理 .bak 文件：根目录 = {root}\n\n")
        removed = delete_bak_files_recursively(root, log_func=self._log)

        self._log("\n========== 清理完成 ==========\n")
        self._log(f"删除 .bak 文件数：{removed}\n")

        messagebox.showinfo("完成", f"已删除 {removed} 个 .bak 文件。")

    # ----- 辅助：日志与输入 -----
    def _log(self, s: str):
        self.log.log(s)

    def _browse_dir(self):
        d = filedialog.askdirectory(initialdir=str(Path(self.root_dir.get()).resolve()))
        if d:
            self.root_dir.set(d)

    def open_dir(self):
        p = Path(self.root_dir.get()).resolve()
        try:
            if sys.platform.startswith("win"):
                os.startfile(p)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                os.system(f'open "{p}"')
            else:
                os.system(f'xdg-open "{p}"')
        except Exception as e:
            from tkinter import messagebox

            messagebox.showerror("错误", f"无法打开目录：{e}")

    def _parse_mapping_from_text(self) -> Dict[str, str]:
        """
        从多行文本框解析映射：
          每行：identifier 与 value
          允许分隔符：逗号/等号/空格/冒号（可混用）
        返回：{identifier: value(str)}
        """
        raw_text = self.target_names_text.get("1.0", "end") if self.target_names_text else ""
        mapping: Dict[str, str] = {}
        for line in raw_text.splitlines():
            s = line.strip()
            if not s:
                continue
            s = s.split("#", 1)[0].strip()  # 去除行内注释
            if not s:
                continue
            parts = re.split(r"[,\s=:]+", s)
            if len(parts) < 2:
                continue
            ident, val = parts[0].strip(), parts[1].strip()
            if ident:
                mapping[ident] = val
        return mapping


# =========================
# 应用主体
# =========================
class App:
    def __init__(self, py_path=os.path.dirname(os.path.abspath(__file__))):
        DATA_CONFIG["app"] = self
        DATA_CONFIG["py_path"] = py_path
        DATA_CONFIG["window"] = ttk.Window(
            themename="sandstone",
            title="Identifier Replacer - 仅 .py",
        )

        # 窗口尺寸
        min_h = 900
        min_w = int(min_h * 4 / 3)
        DATA_CONFIG["window"].minsize(min_w, min_h)
        DATA_CONFIG["window"].geometry(f"{min_w}x{min_h}")

        # 单屏界面
        DATA_CONFIG["screen"] = IdentifierReplacer(DATA_CONFIG["window"])

        DATA_CONFIG["window"].mainloop()


# =========================
# 入口
# =========================
if __name__ == "__main__":
    App()
