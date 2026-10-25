# -*- coding: utf-8 -*-
"""
划词翻译 · 桌面版
=================
零第三方依赖的 Windows 划词翻译工具：
- 选中文字自动翻译（中英自动识别）
- 词性、释义、常用短语、词形、例句
- 生词本按天分组、搜索、CSV 导出
- 一键网页搜索、有道发音、本地缓存

运行：双击同目录的「启动.vbs」。
"""
from __future__ import annotations

import argparse
import ctypes
import json
import logging
import os
import queue
import re
import sys
import threading
import time
import traceback
import urllib.parse
import webbrowser
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Callable, Optional

import tkinter as tk
import tkinter.font as tkfont
import tkinter.messagebox as messagebox
from tkinter import ttk

FROZEN = bool(getattr(sys, "frozen", False))
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
APP_DIR = (Path(sys.executable).resolve().parent if FROZEN
           else Path(__file__).resolve().parent)
VERSION = "1.2.0"
APP_TITLE = "划词翻译"

# 让任务栏图标独立分组
try:
    import winutil
except Exception as exc:  # pragma: no cover
    ctypes.windll.user32.MessageBoxW(
        None, f"缺少 winutil.py，无法启动。\n\n{exc}", APP_TITLE, 0x10
    )
    raise

# ---------------------------------------------------------------------------
# 配色 & 字体
# ---------------------------------------------------------------------------
BG = "#F4F6F5"
CARD = "#FBFDFC"
INK = "#1F2933"
MUTED = "#667085"
FAINT = "#98A2B3"
BORDER = "#DCE8E3"
BORDER_SOFT = "#EAF3EF"
ACCENT = "#0F766E"
ACCENT_DARK = "#0B5C56"
ACCENT_SOFT = "#E6F4F1"
ACCENT_PALE = "#F0FAF8"
WARN = "#B45309"
ERROR = "#B42318"
SHADOW = "#DDE5E1"
GLASS_RIM = "#D3E4DD"
GLASS_ALPHA = 0.975

POS_COLORS = {
    "n.": ("#E8F0FE", "#1D4ED8"),
    "v.": ("#E7F6EC", "#15803D"),
    "vt.": ("#E7F6EC", "#15803D"),
    "vi.": ("#E7F6EC", "#15803D"),
    "adj.": ("#FFF4E5", "#B45309"),
    "adv.": ("#F3E8FF", "#7E22CE"),
    "prep.": ("#EEF2F6", "#475569"),
    "pron.": ("#E6F4F1", "#0F766E"),
    "conj.": ("#EEF2F6", "#475569"),
    "int.": ("#FCE7F3", "#BE185D"),
    "num.": ("#FEF3C7", "#92400E"),
    "art.": ("#F1F5F9", "#475569"),
    "aux.": ("#F1F5F9", "#475569"),
    "abbr.": ("#F1F5F9", "#475569"),
    "phr.": ("#E6F4F1", "#0F766E"),
    "classifier": ("#F3E8FF", "#7E22CE"),
    "idiom": ("#FFF4E5", "#B45309"),
    "part.": ("#EEF2F6", "#475569"),
}
DEFAULT_POS_COLOR = ("#EEF2F6", "#475569")

ENGINES = {
    "bing": ("Bing", "https://www.bing.com/search?q={q}"),
    "baidu": ("百度", "https://www.baidu.com/s?wd={q}"),
    "google": ("Google", "https://www.google.com/search?q={q}"),
    "youdao": ("有道词典", "https://dict.youdao.com/result?word={q}&lang={lang}"),
}

DEFAULTS = {
    "auto_enabled": True,
    "double_click": True,
    "show_launcher": True,
    "auto_start": False,
    "first_run": True,
    "launcher_pos": None,
    "search_engine": "bing",
    "popup_width": 440,
    "popup_collapsed_width": 390,
    "popup_max_height": 600,
}


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
class Config:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.data = dict(DEFAULTS)
        self.load()

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self.data.update(raw)
        except Exception:
            logging.getLogger(__name__).exception("读取设置失败，使用默认设置")
            try:
                backup = self.path.with_name(
                    self.path.stem + ".损坏-" + datetime.now().strftime("%Y%m%d%H%M%S") + ".json"
                )
                os.replace(self.path, backup)
            except Exception:
                pass

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, DEFAULTS.get(key, default))

    def set(self, key: str, value: Any, save: bool = True) -> None:
        self.data[key] = value
        if save:
            self.save()

    def save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(self.data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self.path)
        except Exception:
            logging.getLogger(__name__).exception("保存设置失败")


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def truncate_text(text: str, limit: int, tail: str = "…") -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[: max(0, limit - len(tail))] + tail


def has_translatable(text: str) -> bool:
    return bool(re.search(r"[A-Za-z\u3400-\u4dbf\u4e00-\u9fff]", text or ""))


def round_rect(canvas: tk.Canvas, x1: int, y1: int, x2: int, y2: int,
               r: int, **kwargs: Any) -> int:
    r = max(0, min(r, (x2 - x1) // 2, (y2 - y1) // 2))
    points = [
        x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
        x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
        x1, y2, x1, y2 - r, x1, y1 + r, x1, y1,
    ]
    return canvas.create_polygon(points, smooth=True, **kwargs)


class FlatButton(tk.Label):
    """轻量扁平按钮；比 tk.Button 更容易控制配色。"""

    def __init__(
        self,
        master: tk.Misc,
        text: str,
        command: Optional[Callable[[], None]] = None,
        *,
        bg: str = CARD,
        fg: str = INK,
        hover_bg: Optional[str] = None,
        border: Optional[str] = None,
        font: Optional[tuple] = None,
        padx: int = 12,
        pady: int = 7,
        enabled: bool = True,
    ):
        super().__init__(
            master,
            text=text,
            bg=bg,
            fg=fg,
            font=font or ("Microsoft YaHei UI", 9),
            padx=padx,
            pady=pady,
            bd=0,
            relief="flat",
            cursor="hand2" if enabled else "arrow",
            takefocus=0,
        )
        if border:
            self.configure(highlightthickness=1, highlightbackground=border,
                           highlightcolor=border)
        self._command = command
        self._bg = bg
        self._fg = fg
        self._hover = hover_bg or bg
        self._enabled = enabled
        self._border = border
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_click)

    def _on_enter(self, _event: Any = None) -> None:
        if self._enabled:
            self.configure(bg=self._hover)

    def _on_leave(self, _event: Any = None) -> None:
        if self._enabled:
            self.configure(bg=self._bg)

    def _on_click(self, _event: Any = None) -> None:
        if self._enabled and self._command:
            self._command()

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)
        self.configure(cursor="hand2" if enabled else "arrow")
        if not enabled:
            self.configure(bg=self._bg)

    def set_style(self, *, text: Optional[str] = None, bg: Optional[str] = None,
                  fg: Optional[str] = None, hover_bg: Optional[str] = None) -> None:
        if text is not None:
            self.configure(text=text)
        if bg is not None:
            self._bg = bg
            self.configure(bg=bg)
        if fg is not None:
            self._fg = fg
            self.configure(fg=fg)
        if hover_bg is not None:
            self._hover = hover_bg


class ToggleSwitch(tk.Canvas):
    """自绘开关，替代 Windows 原生小方框。"""

    def __init__(self, master: tk.Misc, variable: tk.BooleanVar,
                 command: Optional[Callable[[], None]] = None,
                 bg: str = CARD, width: int = 42, height: int = 23):
        self.app = None
        self.var = variable
        self.command = command
        self._sw = max(30, int(width))
        self._sh = max(18, int(height))
        super().__init__(
            master, width=self._sw, height=self._sh, bg=bg,
            highlightthickness=0, bd=0, cursor="hand2", takefocus=0,
        )
        self.bind("<Button-1>", self._toggle)
        self.var.trace_add("write", self._on_var)
        self._draw()

    def _on_var(self, *_args: Any) -> None:
        try:
            self._draw()
        except tk.TclError:
            pass

    def _draw(self) -> None:
        try:
            self.delete("all")
            on = bool(self.var.get())
            h = self._sh
            w = self._sw
            r = max(4, (h - 4) // 2)
            round_rect(self, 1, 2, w - 1, h - 2, r,
                       fill=ACCENT if on else "#D0D5DD", outline="")
            d = h - 9
            x = w - d - 4 if on else 4
            self.create_oval(x, 4, x + d, 4 + d, fill="#FFFFFF", outline="")
        except tk.TclError:
            pass

    def _toggle(self, _event: Any = None) -> None:
        self.var.set(not bool(self.var.get()))
        if self.command:
            self.command()


# ---------------------------------------------------------------------------
# Toast
# ---------------------------------------------------------------------------
class Toast:
    def __init__(self, app: "App"):
        self.app = app
        self.win: Optional[tk.Toplevel] = None
        self.label: Optional[tk.Label] = None
        self._after: Optional[str] = None
        self.hwnd = 0

    def show(self, text: str, ms: int = 1900) -> None:
        if self.win is None or not self.win.winfo_exists():
            self._build()
        assert self.win is not None and self.label is not None
        self.label.configure(text=text)
        self.win.update_idletasks()
        w = max(self.app.px(180), self.label.winfo_reqwidth() + self.app.px(26))
        h = self.label.winfo_reqheight() + self.app.px(16)
        left, top, right, bottom = winutil.get_work_area()
        x = right - w - self.app.px(22)
        y = bottom - h - self.app.px(22)
        self.win.geometry(f"{w}x{h}+{x}+{y}")
        self.win.deiconify()
        self.win.update()
        if self.hwnd:
            winutil.set_round_region(self.hwnd, self.app.px(12))
        self.win.lift()
        try:
            winutil.keep_topmost_noactivate(self.hwnd)
        except Exception:
            pass
        if self._after:
            try:
                self.win.after_cancel(self._after)
            except Exception:
                pass
        self._after = self.win.after(ms, self.hide)

    def _build(self) -> None:
        self.win = tk.Toplevel(self.app.root)
        self.win.withdraw()
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        try:
            self.win.attributes("-alpha", 0.96)
        except Exception:
            pass
        self.win.configure(bg="#202A2E")
        outer = tk.Frame(self.win, bg="#202A2E")
        outer.pack(fill="both", expand=True)
        self.label = tk.Label(
            outer,
            text="",
            bg="#202A2E",
            fg="#F8FAFC",
            font=(self.app.font_ui, 9),
            padx=self.app.px(13),
            pady=self.app.px(8),
        )
        self.label.pack(fill="both", expand=True)
        self.win.update_idletasks()
        hwnd = winutil.get_root_window(int(self.win.winfo_id()))
        if hwnd:
            self.hwnd = hwnd
            winutil.apply_rounded_corners(hwnd)
            winutil.set_round_region(hwnd, self.app.px(12))
        self.app.register_ui_window(self.hwnd)

    def hide(self) -> None:
        if self.win is not None and self.win.winfo_exists():
            self.win.withdraw()


# ---------------------------------------------------------------------------
# 翻译弹窗
# ---------------------------------------------------------------------------
class TranslationPopup:
    def __init__(self, app: "App"):
        self.app = app
        self.win = tk.Toplevel(app.root)
        self.win.withdraw()
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.configure(bg=CARD)
        try:
            self.win.attributes("-alpha", GLASS_ALPHA)
        except Exception:
            pass
        self.visible = False
        self.hwnd = 0
        self.anchor = (0, 0)
        self.result: Optional[Any] = None
        self.current_text = ""
        self.current_src = "en"
        self.request_id = 0
        self.loading = False
        self.expanded = False
        self.mode = "idle"          # idle / loading / result / error
        self.error_message = ""
        self._footer_visible = True
        self._hover_armed = False
        self._last_inside: Optional[bool] = None
        self._shown_at = 0.0
        self._outside_since = 0.0
        self._expand_job: Optional[str] = None
        self._anim_job: Optional[str] = None
        self._animating = False
        self._anim_token = 0
        self._last_geom: Optional[tuple[int, int, int, int]] = None
        self._drag_origin: Optional[tuple[int, int, int, int]] = None
        self._phrase_map: dict[str, str] = {}
        self._last_terms: list[str] = []
        self._build()
        self.win.after(100, self._watch_hover)

    # ------------------------------------------------------------------
    def _build(self) -> None:
        px = self.app.px
        self.rim = tk.Frame(self.win, bg=GLASS_RIM)
        self.rim.pack(fill="both", expand=True)
        self.outer = tk.Frame(
            self.rim, bg=CARD, highlightthickness=1,
            highlightbackground="#FFFFFF",
        )
        self.outer.pack(fill="both", expand=True, padx=1, pady=1)
        self.top_line = tk.Frame(self.outer, bg="#E9F7F2", height=1)
        self.top_line.pack(fill="x")

        # 顶部
        self.header = tk.Frame(self.outer, bg=CARD)
        self.header.pack(fill="x", padx=px(14), pady=(px(8), px(5)))

        self.logo = tk.Canvas(
            self.header, width=px(27), height=px(27), bg=CARD,
            highlightthickness=0, bd=0,
        )
        self.logo.pack(side="left", padx=(0, px(9)))
        round_rect(self.logo, px(1), px(1), px(26), px(26), px(7),
                   fill=ACCENT, outline="")
        self.logo.create_text(
            px(13.5), px(13.5), text="译", fill="#FFFFFF",
            font=(self.app.font_ui, 10, "bold"),
        )

        self.dir_label = tk.Label(
            self.header, text="EN → 中文", bg=ACCENT_SOFT, fg=ACCENT_DARK,
            font=(self.app.font_ui, 8, "bold"), padx=px(8), pady=px(2),
            cursor="hand2",
        )
        self.dir_label.pack(side="left", pady=px(2))
        self.dir_label.bind("<Button-1>", lambda _e: self.app.swap_direction())

        self.close_btn = tk.Label(
            self.header, text="×", bg=CARD, fg=FAINT,
            font=(self.app.font_ui, 15), padx=px(4), pady=0, cursor="hand2",
        )
        self.close_btn.pack(side="right", padx=(px(4), 0))
        self.close_btn.bind("<Enter>", lambda _e: self.close_btn.configure(fg=INK))
        self.close_btn.bind("<Leave>", lambda _e: self.close_btn.configure(fg=FAINT))
        self.close_btn.bind("<Button-1>", lambda _e: self.hide())

        self.brand = tk.Label(
            self.header, text="划词翻译", bg=CARD, fg=FAINT,
            font=(self.app.font_ui, 8),
        )
        self.brand.pack(side="right", padx=(0, px(4)))

        # 内容
        self.body = tk.Frame(self.outer, bg=CARD)
        self.body.pack(fill="both", expand=True, padx=px(14), pady=(0, px(8)))
        self.text = tk.Text(
            self.body,
            width=1,
            height=1,
            wrap="word",
            bd=0,
            highlightthickness=0,
            bg=CARD,
            fg=INK,
            font=(self.app.font_ui, 10),
            padx=0,
            pady=0,
            cursor="arrow",
            state="disabled",
            takefocus=0,
            spacing1=0,
            spacing2=px(2),
            spacing3=0,
        )
        self.text.pack(side="left", fill="both", expand=True)
        style = ttk.Style(self.win)
        try:
            style.configure(
                "Popup.Vertical.TScrollbar",
                gripcount=0, background=BORDER, darkcolor=BG, lightcolor=BG,
                troughcolor=CARD, bordercolor=CARD, arrowcolor=MUTED,
                arrowsize=px(8), width=px(7),
            )
        except Exception:
            pass
        self.scrollbar = ttk.Scrollbar(
            self.body, orient="vertical", command=self.text.yview,
            style="Popup.Vertical.TScrollbar",
        )
        self.text.configure(yscrollcommand=self.scrollbar.set)
        self._scroll_visible = False

        # 底栏
        self.footer_sep = tk.Frame(self.outer, bg=BORDER_SOFT, height=1)
        self.footer_sep.pack(fill="x", padx=px(12))
        self.footer = tk.Frame(self.outer, bg="#F7FBF9")
        self.footer.pack(fill="x", padx=px(12), pady=(px(7), px(9)))

        self.status = tk.Label(
            self.footer, text="", bg="#F7FBF9", fg=FAINT,
            font=(self.app.font_ui, 8), anchor="w",
        )
        self.status.pack(side="left", fill="x", expand=True)

        self.copy_btn = FlatButton(
            self.footer, "复制", self.app.copy_current,
            bg=CARD, fg=MUTED, hover_bg=BG, border=BORDER,
            font=(self.app.font_ui, 9), padx=px(9), pady=px(5),
        )
        self.copy_btn.pack(side="right", padx=(px(6), 0))

        self.search_btn = FlatButton(
            self.footer, "网页搜索", self.app.search_current,
            bg=CARD, fg=MUTED, hover_bg=BG, border=BORDER,
            font=(self.app.font_ui, 9), padx=px(9), pady=px(5),
        )
        self.search_btn.pack(side="right", padx=(px(6), 0))

        self.add_btn = FlatButton(
            self.footer, "＋ 生词本", self.app.add_current,
            bg=ACCENT, fg="#FFFFFF", hover_bg=ACCENT_DARK,
            font=(self.app.font_ui, 9, "bold"), padx=px(11), pady=px(5),
        )
        self.add_btn.pack(side="right")

        self._configure_tags()
        self.win.bind("<Escape>", lambda _e: self.hide())
        self.win.bind("<Control-c>", lambda _e: self.app.copy_current())
        self.win.bind("<Control-d>", lambda _e: self.app.add_current())
        self.header.bind("<ButtonPress-1>", self._start_drag)
        self.header.bind("<B1-Motion>", self._on_drag)
        for w in (self.logo, self.brand, self.dir_label):
            w.bind("<ButtonPress-1>", self._start_drag)
            w.bind("<B1-Motion>", self._on_drag)
        self.text.bind("<MouseWheel>", self._on_wheel)
        self.win.bind("<MouseWheel>", self._on_wheel)

    def _configure_tags(self) -> None:
        px = self.app.px
        f = self.app.font_ui
        self.text.tag_configure("source", font=(f, 9), foreground=MUTED,
                                spacing1=px(2), spacing3=px(7))
        self.text.tag_configure("trans", font=(f, 18, "bold"), foreground=INK,
                                spacing1=px(1), spacing3=px(9))
        self.text.tag_configure("compact_trans", font=(f, 15, "bold"),
                                foreground=INK, spacing1=px(4), spacing3=px(4))
        self.text.tag_configure("hint", font=(f, 8), foreground=FAINT,
                                spacing1=px(6))
        self.text.tag_configure("section", font=(f, 8, "bold"), foreground=FAINT,
                                spacing1=px(9), spacing3=px(4))
        self.text.tag_configure("meaning", font=(f, 10), foreground="#344054",
                                spacing3=px(4))
        self.text.tag_configure("small", font=(f, 8), foreground=FAINT,
                                spacing3=px(2))
        self.text.tag_configure("phone", font=(f, 9), foreground=MUTED,
                                spacing3=px(3))
        self.text.tag_configure("divider", font=(f, 1), background=BORDER_SOFT)
        self.text.tag_configure("error_title", font=(f, 12, "bold"),
                                foreground=ERROR, spacing3=px(5))
        self.text.tag_configure("term", background=ACCENT_SOFT,
                                foreground=ACCENT_DARK, font=(f, 9, "bold"))
        self.text.tag_configure("phrase_en", font=(f, 9, "bold"),
                                foreground=ACCENT, spacing1=px(2))
        self.text.tag_configure("phrase_zh", font=(f, 9), foreground=MUTED)
        self.text.tag_configure("example_en", font=(f, 9), foreground="#344054",
                                spacing1=px(3))
        self.text.tag_configure("example_zh", font=(f, 9), foreground=MUTED,
                                spacing3=px(4))
        self.text.tag_configure("audio", font=(f, 10), foreground=ACCENT,
                                underline=True)
        self.text.tag_configure("loading", font=(f, 11), foreground=ACCENT,
                                spacing1=px(4))
        for pos, (bg, fg) in POS_COLORS.items():
            tag = self._pos_tag_name(pos)
            self.text.tag_configure(tag, background=bg, foreground=fg,
                                    font=(f, 8, "bold"))
        self.text.tag_bind("audio", "<Enter>",
                           lambda _e: self.text.configure(cursor="hand2"))
        self.text.tag_bind("audio", "<Leave>",
                           lambda _e: self.text.configure(cursor="arrow"))
        self.text.tag_bind("audio", "<Button-1>",
                           lambda _e: self.app.play_current_audio())

    @staticmethod
    def _pos_tag_name(pos: str) -> str:
        safe = re.sub(r"[^A-Za-z0-9_]", "_", pos or "x")
        return "pos_" + safe

    # ------------------------------------------------------------------
    def _set_text(self, builder: Callable[[], None]) -> None:
        self.text.configure(state="normal")
        try:
            self.text.delete("1.0", "end")
            self._phrase_map.clear()
            builder()
            self.text.yview_moveto(0.0)
        finally:
            self.text.configure(state="disabled")

    def _source_text(self) -> str:
        t = re.sub(r"\s+", " ", self.current_text or "").strip()
        return truncate_text(t, 210)

    def _direction_text(self, src: str) -> str:
        return "EN → 中文" if src != "zh" else "中文 → EN"

    # ------------------------------------------------------------------
    def prepare_show(self) -> None:
        """新内容出现时先保持紧凑模式。"""
        self._cancel_animation()
        self.expanded = False
        self._cancel_expand()
        self._hover_armed = False
        self._last_inside = None
        self._shown_at = time.time()
        self._outside_since = 0.0
        self._hide_footer()

    def _compact_translation(self, translation: str) -> str:
        t = re.sub(r"\s+", " ", str(translation or "")).strip()
        if not t:
            return "未找到释义"
        parts = [x.strip() for x in re.split(r"[；;]", t) if x.strip()]
        if len(parts) > 2:
            t = "；".join(parts[:2])
        return truncate_text(t, 52)

    def _show_footer(self) -> None:
        if self._footer_visible:
            return
        px = self.app.px
        self.footer_sep.pack(fill="x", padx=px(12))
        self.footer.pack(fill="x", padx=px(12), pady=(px(7), px(9)))
        self._footer_visible = True

    def _hide_footer(self) -> None:
        if not self._footer_visible:
            return
        self.footer.pack_forget()
        self.footer_sep.pack_forget()
        self._footer_visible = False

    # ------------------------------------------------------------------
    def show_loading(self, text: str, src_lang: str, anchor: tuple[int, int],
                     request_id: int) -> None:
        self.current_text = text
        self.current_src = src_lang
        self.request_id = request_id
        self.anchor = anchor
        self.result = None
        self.loading = True
        self.mode = "loading"
        self.error_message = ""
        self.prepare_show()
        self._paint_current(reposition=True)

    def render(self, result: Any, request_id: int, reposition: bool = False) -> None:
        self._cancel_animation()
        self.request_id = request_id
        self.result = result
        self.loading = False
        self.mode = "result"
        self.error_message = ""
        self.current_text = result.text
        self.current_src = result.src_lang
        self._last_terms = list(getattr(result, "terms", []) or [])
        if self._last_inside:
            self._hover_armed = True
        self._paint_current(reposition=reposition)

    def render_error(self, message: str, request_id: int) -> None:
        self._cancel_animation()
        self.request_id = request_id
        self.loading = False
        self.result = None
        self.mode = "error"
        self.error_message = str(message or "")
        self.expanded = False
        self._hide_footer()
        if self._last_inside:
            self._hover_armed = True
        self._paint_current(reposition=False)

    def set_expanded(self, expanded: bool) -> None:
        if self.mode not in ("result", "error"):
            return
        if self.expanded == expanded:
            return
        self._cancel_animation()
        token = self._anim_token
        self._animating = True
        self.expanded = bool(expanded)
        # 保持窗口可见，只做轻微透明度过渡，避免中间出现空白/消失。
        self.win.attributes("-alpha", 0.88)
        self._paint_current(reposition=False, present=False)
        try:
            self.win.update()
        except Exception:
            pass
        self._present(reposition=False)
        try:
            self.win.update()
        except Exception:
            pass

        def done() -> None:
            if token != self._anim_token:
                return
            self._animating = False
            self.win.attributes("-alpha", GLASS_ALPHA)
            self._set_region()

        # 给新内容极短时间完成重排，然后淡入到正常透明度。
        self._anim_job = self.win.after(
            10, lambda: self._fade_to(GLASS_ALPHA, 100, done)
        )

    # ------------------------------------------------------------------
    def _paint_current(self, reposition: bool = False,
                       present: bool = True) -> None:
        expanded = bool(self.expanded and self.mode in ("result", "error"))
        if expanded:
            self._show_footer()
        else:
            self._hide_footer()
        self.dir_label.configure(text=self._direction_text(self.current_src))

        if self.mode == "loading":
            self._build_loading(expanded=False)
            self.status.configure(text="正在翻译")
            self.add_btn.set_enabled(False)
            self.copy_btn.set_enabled(False)
            self.copy_btn.set_style(fg=FAINT)
        elif self.mode == "error":
            self._build_error(expanded)
            self.status.configure(text="连接失败")
            self.copy_btn.set_enabled(False)
            self.copy_btn.set_style(fg=FAINT)
            self.search_btn.set_enabled(True)
            self.add_btn.set_style(text="重试", bg=ACCENT, fg="#FFFFFF",
                                   hover_bg=ACCENT_DARK)
            self.add_btn._command = self.app.retry_current  # type: ignore[attr-defined]
            self.add_btn.set_enabled(True)
        elif self.mode == "result" and self.result is not None:
            self._build_result(expanded)
            if self.result.from_cache:
                status = "缓存 · 即时"
            else:
                status = (f"{self.result.engine or '在线翻译'} · "
                          f"{self.result.latency_ms} ms")
            self.status.configure(text=truncate_text(status, 42))
            self.copy_btn.set_enabled(True)
            self.copy_btn.set_style(fg=MUTED)
            self.search_btn.set_enabled(True)
            self._update_add_button()
        if present:
            self._present(reposition=reposition)

    # ------------------------------------------------------------------
    def _build_loading(self, expanded: bool) -> None:
        def build() -> None:
            self.text.insert("end",
                             truncate_text(self._source_text(), 44), "source")
            self.text.insert("end", "\n\n")
            self.text.insert("end", "正在翻译…", "loading")
            self.text.insert("end", "\n")
            self.text.insert("end", "正在连接翻译引擎，通常 0.1–0.5 秒出结果",
                             "small")

        self._set_text(build)

    def _build_result(self, expanded: bool) -> None:
        result = self.result
        if result is None:
            return
        if not expanded:
            def build_compact() -> None:
                self.text.insert("end",
                                 truncate_text(self._source_text(), 44), "source")
                self.text.insert("end", "\n")
                self.text.insert("end", self._compact_translation(result.translation),
                                 "compact_trans")
                self.text.insert("end", "\n")
                if result.senses or result.phrases or result.examples:
                    hint = "悬停展开 · 词性 / 短语 / 例句"
                else:
                    hint = "悬停展开 · 更多操作"
                self.text.insert("end", hint, "hint")
            self._set_text(build_compact)
            return

        def build() -> None:
            px = self.app.px
            self.text.insert("end", self._source_text(), "source")
            self.text.insert("end", "\n")

            # 音标 / 拼音
            phones = tuple(getattr(result, "phones", ("", "")) or ("", ""))
            phone_bits: list[str] = []
            if result.src_lang == "zh":
                if phones and phones[0]:
                    phone_bits.append(f"拼音 {phones[0]}")
            else:
                if phones and phones[0]:
                    phone_bits.append(f"英 /{phones[0]}/")
                if len(phones) > 1 and phones[1] and phones[1] != phones[0]:
                    phone_bits.append(f"美 /{phones[1]}/")
            if phone_bits:
                self.text.insert("end", "   ".join(phone_bits), "phone")
                self.text.insert("end", "   🔊", "audio")
                self.text.insert("end", "\n")

            self.text.insert("end", "\n")
            self.text.insert("end", result.translation or "未找到释义", "trans")
            self.text.insert("end", "\n")

            # 中文查词的英文表达
            if result.src_lang == "zh" and result.terms:
                self.text.insert("end", "英文表达\n", "section")
                for i, term in enumerate(result.terms[:6]):
                    if i:
                        self.text.insert("end", "   ")
                    self.text.insert("end", f" {term} ", "term")
                self.text.insert("end", "\n")

            if result.senses:
                heading = "词性与释义" if any(s.pos for s in result.senses) else "释义"
                self.text.insert("end", heading + "\n", "section")
                for sense in result.senses[:6]:
                    if sense.pos:
                        tag = self._pos_tag_name(sense.pos)
                        if sense.pos not in POS_COLORS:
                            self.text.tag_configure(
                                tag, background=DEFAULT_POS_COLOR[0],
                                foreground=DEFAULT_POS_COLOR[1],
                                font=(self.app.font_ui, 8, "bold"),
                            )
                        self.text.insert("end", f" {sense.pos} ", tag)
                        self.text.insert("end", "  ")
                    self.text.insert("end", sense.meaning, "meaning")
                    self.text.insert("end", "\n")

            if result.phrases:
                self.text.insert("end", "常用短语\n", "section")
                for i, (en, zh) in enumerate(result.phrases[:4]):
                    tag = f"phrase_{i}"
                    self._phrase_map[tag] = en
                    self.text.insert("end", en, ("phrase_en", tag))
                    if zh:
                        self.text.insert("end", "   " + zh, "phrase_zh")
                    self.text.insert("end", "\n")
                    self.text.tag_bind(tag, "<Enter>",
                                       lambda _e: self.text.configure(cursor="hand2"))
                    self.text.tag_bind(tag, "<Leave>",
                                       lambda _e: self.text.configure(cursor="arrow"))
                    self.text.tag_bind(
                        tag, "<Button-1>",
                        lambda _e, t=tag: self.app.translate_phrase(self._phrase_map.get(t, "")),
                    )

            if result.forms:
                self.text.insert("end", "词形变化\n", "section")
                bits = [f"{a} {b}" for a, b in result.forms[:4]]
                self.text.insert("end", " · ".join(bits), "small")
                self.text.insert("end", "\n")

            if result.examples:
                self.text.insert("end", "例句\n", "section")
                for en, zh in result.examples[:1]:
                    self.text.insert("end", en, "example_en")
                    self.text.insert("end", "\n")
                    self.text.insert("end", zh, "example_zh")
                    self.text.insert("end", "\n")

            if not (result.senses or result.phrases or result.examples or result.terms):
                self.text.insert("end", "这是一个整句翻译，未查询到词条释义。", "small")

        self._set_text(build)

    def _build_error(self, expanded: bool) -> None:
        if not expanded:
            def build_compact() -> None:
                self.text.insert("end",
                                 truncate_text(self._source_text(), 44), "source")
                self.text.insert("end", "\n\n")
                self.text.insert("end", "翻译失败\n", "error_title")
                self.text.insert("end", "悬停展开 · 重试 / 网页搜索", "hint")
            self._set_text(build_compact)
            return

        def build() -> None:
            self.text.insert("end", self._source_text(), "source")
            self.text.insert("end", "\n\n")
            self.text.insert("end", "翻译失败\n", "error_title")
            self.text.insert("end", "网络暂时不可用，或翻译接口繁忙。\n\n", "meaning")
            self.text.insert("end", truncate_text(self.error_message, 150), "small")
            self.text.insert("end", "\n\n点击下方「重试」，或直接用「网页搜索」。", "small")

        self._set_text(build)

    def _update_add_button(self) -> None:
        if not self.result:
            self.add_btn.set_enabled(False)
            return
        self.add_btn._command = self.app.add_current  # type: ignore[attr-defined]
        if self.app.vocab.has(self.result.text):
            self.add_btn.set_style(text="✓ 已收藏", bg=ACCENT_SOFT, fg=ACCENT,
                                   hover_bg=ACCENT_SOFT)
            self.add_btn.set_enabled(False)
        else:
            self.add_btn.set_style(text="＋ 生词本", bg=ACCENT, fg="#FFFFFF",
                                   hover_bg=ACCENT_DARK)
            self.add_btn.set_enabled(True)

    # ------------------------------------------------------------------
    def _cancel_expand(self) -> None:
        if self._expand_job:
            try:
                self.win.after_cancel(self._expand_job)
            except Exception:
                pass
            self._expand_job = None

    def _expand_now(self) -> None:
        self._expand_job = None
        logging.getLogger(__name__).debug(
            "悬停展开 visible=%s expanded=%s mode=%s",
            self.visible, self.expanded, self.mode,
        )
        if (self.visible and not self.expanded
                and self.mode in ("result", "error")):
            self.set_expanded(True)

    def _watch_hover(self) -> None:
        """轮询鼠标是否在弹窗上：进入展开，移开自动收起。"""
        try:
            if self.visible and self.hwnd:
                x, y = winutil.get_cursor_pos()
                left, top, right, bottom = winutil.get_window_rect(self.hwnd)
                inside = left <= x < right and top <= y < bottom
                if inside != self._last_inside:
                    self._last_inside = inside
                    logging.getLogger(__name__).debug(
                        "悬停检测 pos=%s,%s rect=%s,%s,%s,%s inside=%s",
                        x, y, left, top, right, bottom, inside,
                    )
                now = time.time()
                if inside:
                    self._outside_since = 0.0
                    if (not self._animating
                            and not self.expanded
                            and self.mode in ("result", "error")
                            and self._hover_armed
                            and now - self._shown_at >= 0.3
                            and self._expand_job is None):
                        self._expand_job = self.win.after(110, self._expand_now)
                else:
                    self._hover_armed = True
                    self._outside_since = self._outside_since or now
                    self._cancel_expand()
                    if (not self._animating and self.expanded
                            and now - self._outside_since >= 0.42):
                        self._outside_since = 0.0
                        self.set_expanded(False)
            else:
                self._cancel_expand()
        except Exception:
            pass
        try:
            self.win.after(100, self._watch_hover)
        except Exception:
            pass

    # ------------------------------------------------------------------
    def _cancel_animation(self) -> None:
        self._anim_token += 1
        if self._anim_job:
            try:
                self.win.after_cancel(self._anim_job)
            except Exception:
                pass
            self._anim_job = None
        self._animating = False

    def _set_region(self) -> None:
        if self.hwnd:
            try:
                winutil.set_round_region(self.hwnd, self.app.px(15))
            except Exception:
                pass

    def _apply_geom(self, w: int, h: int, x: int, y: int) -> None:
        self.win.geometry(f"{w}x{h}+{x}+{y}")
        self.win.update_idletasks()
        self._last_geom = (w, h, x, y)
        self._set_region()

    def _animate_show(self, w: int, h: int, x: int, y: int) -> None:
        self._cancel_animation()
        token = self._anim_token
        self._animating = True
        start_y = y + self.app.px(6)
        self._apply_geom(w, h, x, start_y)
        self.win.attributes("-alpha", 0.0)
        steps = 2

        def step(i: int) -> None:
            if token != self._anim_token or not self.visible:
                return
            p = i / steps
            ease = 1.0 - (1.0 - p) ** 3
            yy = int(round(start_y + (y - start_y) * ease))
            if self.hwnd:
                winutil.resize_window(self.hwnd, x, yy, w, h)
            else:
                self.win.geometry(f"{w}x{h}+{x}+{yy}")
            self.win.attributes("-alpha", max(0.0, GLASS_ALPHA * ease))
            if i < steps:
                self._anim_job = self.win.after(30, lambda: step(i + 1))
            else:
                self._anim_job = None
                self._animating = False
                self._apply_geom(w, h, x, y)
                self.win.attributes("-alpha", GLASS_ALPHA)

        step(1)

    def _fade_to(self, target_alpha: float, duration_ms: int,
                 on_done: Optional[Callable[[], None]] = None) -> None:
        try:
            start_alpha = float(self.win.attributes("-alpha"))
        except Exception:
            start_alpha = GLASS_ALPHA
        token = self._anim_token
        steps = 2
        self._animating = True

        def step(i: int) -> None:
            if token != self._anim_token or not self.visible:
                return
            p = i / steps
            ease = 1.0 - (1.0 - p) ** 3
            alpha = start_alpha + (target_alpha - start_alpha) * ease
            self.win.attributes("-alpha", max(0.0, min(1.0, alpha)))
            if i < steps:
                self._anim_job = self.win.after(30, lambda: step(i + 1))
            else:
                self._anim_job = None
                if on_done:
                    on_done()

        step(1)

    # ------------------------------------------------------------------
    def _present(self, reposition: bool = False) -> None:
        """计算高度、定位并显示；紧凑模式只占一小块屏幕。"""
        px = self.app.px
        first_show = not self.visible
        expanded = bool(self.expanded and self.mode in ("result", "error"))
        if expanded:
            width = px(int(self.app.config.get("popup_width", 440)))
        else:
            width = px(int(self.app.config.get("popup_collapsed_width", 390)))
        max_h = px(int(self.app.config.get("popup_max_height", 600)))
        left, top, right, bottom = winutil.get_work_area(*self.anchor)
        wl, wt, wr, wb = left, top, right, bottom
        max_h = min(max_h, max(px(220), bottom - top - px(24)))
        if width > right - left - px(16):
            width = right - left - px(16)

        if first_show:
            self.win.attributes("-alpha", 0.0)
            x, y = self._calc_pos(width, max_h)
            self.win.geometry(f"{width}x{max_h}+{x}+{y}")
            self.win.deiconify()
            self.win.update()
            self._register_hwnd()
        else:
            self.win.update_idletasks()
            # 宽度变化会改变换行行数，必须先用目标宽度重排再量高度，
            # 否则从展开收回紧凑模式时会少算一行，把提示裁掉。
            if self.win.winfo_width() != width:
                self.win.geometry(
                    f"{width}x{max(self.win.winfo_height(), px(160))}"
                )
                self.win.update()

        # 先按无滚动条测量
        if self._scroll_visible:
            self._hide_scrollbar()
            self.win.update_idletasks()
        content_h = self._content_height()
        header_h = self.header.winfo_reqheight() + px(14)
        footer_h = (self.footer.winfo_reqheight() + px(16) + 1
                    if self._footer_visible else 0)
        body_pad = px(8)
        wanted = header_h + footer_h + content_h + body_pad
        min_h = px(120 if not expanded else 190)
        win_h = max(min_h, wanted)

        if win_h > max_h:
            win_h = max_h
            self._show_scrollbar()
            self.win.update_idletasks()
            content_h = self._content_height()
            wanted = header_h + footer_h + content_h + body_pad
            if wanted <= max_h:
                win_h = max(min_h, wanted)
                self._hide_scrollbar()
            else:
                self.text.yview_moveto(0.0)
        else:
            self._hide_scrollbar()

        if reposition or first_show:
            x, y = self._calc_pos(width, win_h)
        else:
            x = int(self.win.winfo_x())
            y = int(self.win.winfo_y())
            wl, wt, wr, wb = winutil.get_work_area(x, y)
            x = max(wl + px(8), min(x, wr - width - px(8)))
            y = max(wt + px(8), min(y, wb - win_h - px(8)))
        self.win.geometry(f"{width}x{win_h}+{x}+{y}")
        self.win.update()
        # 如果 Text 实际高度比内容少一行，补一点窗口高度
        actual = self.text.winfo_height()
        if actual > 0 and actual + px(2) < content_h and win_h < max_h:
            win_h = min(max_h, win_h + (content_h - actual) + px(2))
            y = max(wt + px(8), min(y, wb - win_h - px(8)))
            self.win.geometry(f"{width}x{win_h}+{x}+{y}")
            self.win.update()

        self.visible = True
        self._last_geom = (width, win_h, x, y)
        if first_show:
            self._animate_show(width, win_h, x, y)
        else:
            if not self._animating:
                self.win.attributes("-alpha", GLASS_ALPHA)
            self._set_region()
        self.win.lift()
        try:
            winutil.keep_topmost_noactivate(self.hwnd)
        except Exception:
            pass

    def _content_height(self) -> int:
        """返回内容像素高度；count 保障长文，bbox 修正最后一行。"""
        try:
            self.text.update_idletasks()
            count_h = 0
            if self.text.winfo_width() > 20:
                val = self.text.count("1.0", "end-1c", "ypixels")
                if isinstance(val, (tuple, list)):
                    val = val[0] if val else 0
                if val:
                    count_h = int(val) + self.app.px(18)
            bbox_h = 0
            for idx in ("end-3c", "end-2c", "end-1c"):
                bbox = self.text.bbox(idx)
                if bbox:
                    bbox_h = int(bbox[1] + bbox[3]) + self.app.px(14)
                    break
            if bbox_h or count_h:
                return max(count_h, bbox_h)
            h = int(self.text.winfo_height())
            return h if h > 1 else self.app.px(120)
        except Exception:
            return self.app.px(120)

    def _show_scrollbar(self) -> None:
        if not self._scroll_visible:
            self.scrollbar.pack(side="right", fill="y", padx=(self.app.px(4), 0))
            self._scroll_visible = True

    def _hide_scrollbar(self) -> None:
        if self._scroll_visible:
            self.scrollbar.pack_forget()
            self._scroll_visible = False

    def _calc_pos(self, w: int, h: int) -> tuple[int, int]:
        px = self.app.px
        ax, ay = self.anchor
        left, top, right, bottom = winutil.get_work_area(ax, ay)
        gap = px(14)
        x = ax + gap
        if x + w > right - px(8):
            x = ax - w - gap
        x = max(left + px(8), min(x, right - w - px(8)))
        y = ay + gap
        if y + h > bottom - px(8):
            y = ay - h - gap
        y = max(top + px(8), min(y, bottom - h - px(8)))
        return int(x), int(y)

    def _register_hwnd(self) -> None:
        try:
            hwnd = winutil.get_root_window(int(self.win.winfo_id()))
        except Exception:
            hwnd = 0
        if hwnd and hwnd != self.hwnd:
            self.hwnd = hwnd
            winutil.apply_rounded_corners(hwnd)
            self.app.register_ui_window(hwnd)

    def hide(self) -> None:
        self.loading = False
        self.visible = False
        self.expanded = False
        self._hover_armed = False
        self._outside_since = 0.0
        self._cancel_expand()
        self._cancel_animation()
        self._hide_footer()
        try:
            self.win.withdraw()
        except Exception:
            pass

    # ------------------------------------------------------------------
    def _start_drag(self, event: tk.Event) -> None:
        self._drag_origin = (
            int(event.x_root), int(event.y_root),
            int(self.win.winfo_x()), int(self.win.winfo_y()),
        )

    def _on_drag(self, event: tk.Event) -> None:
        if not self._drag_origin:
            return
        sx, sy, wx, wy = self._drag_origin
        nx = wx + int(event.x_root) - sx
        ny = wy + int(event.y_root) - sy
        self.win.geometry(f"+{nx}+{ny}")

    def _on_wheel(self, event: tk.Event) -> None:
        if not self._scroll_visible:
            return
        delta = -1 if event.delta > 0 else 1
        self.text.yview_scroll(delta * 2, "units")
        return "break"


# ---------------------------------------------------------------------------
# 生词本窗口
# ---------------------------------------------------------------------------
class WordbookWindow:
    def __init__(self, app: "App"):
        self.app = app
        self._placed = False
        self.win = tk.Toplevel(app.root)
        self.win.title("生词本 · 划词翻译")
        self.win.configure(bg=BG)
        self.win.withdraw()
        try:
            if app.icon_path.exists():
                self.win.iconbitmap(str(app.icon_path))
        except Exception:
            pass
        self.win.minsize(app.px(640), app.px(460))
        self.win.protocol("WM_DELETE_WINDOW", self.hide)
        self.win.bind("<Escape>", lambda _e: self.hide())
        self._build()
        self.win.update_idletasks()
        hwnd = winutil.get_root_window(int(self.win.winfo_id()))
        if hwnd:
            app.register_ui_window(hwnd)

    def _build(self) -> None:
        px = self.app.px
        f = self.app.font_ui
        header = tk.Frame(self.win, bg=CARD)
        header.pack(fill="x")
        inner = tk.Frame(header, bg=CARD)
        inner.pack(fill="x", padx=px(22), pady=px(16))

        left = tk.Frame(inner, bg=CARD)
        left.pack(side="left", fill="x", expand=True)
        tk.Label(left, text="生词本", bg=CARD, fg=INK,
                 font=(f, 19, "bold")).pack(anchor="w")
        self.subtitle = tk.Label(
            left, text="", bg=CARD, fg=MUTED, font=(f, 9),
        )
        self.subtitle.pack(anchor="w", pady=(px(3), 0))

        right = tk.Frame(inner, bg=CARD)
        right.pack(side="right")
        search_box = tk.Frame(right, bg=CARD, highlightthickness=1,
                              highlightbackground=BORDER)
        search_box.pack(side="left", padx=(0, px(8)))
        tk.Label(search_box, text="⌕", bg=CARD, fg=FAINT,
                 font=(f, 12)).pack(side="left", padx=(px(8), px(2)))
        self.search_var = tk.StringVar(value="搜索生词")
        self._search_placeholder = True
        self.search_entry = tk.Entry(
            search_box, textvariable=self.search_var, bd=0, relief="flat",
            bg=CARD, fg=FAINT, insertbackground=INK, font=(f, 9), width=18,
        )
        self.search_entry.pack(side="left", padx=(0, px(8)), pady=px(6))
        self.search_entry.bind("<KeyRelease>", lambda _e: self.refresh())
        self.search_entry.bind("<Escape>", lambda _e: self.hide())
        self.search_entry.bind("<FocusIn>", self._search_focus_in)
        self.search_entry.bind("<FocusOut>", self._search_focus_out)

        self.export_btn = FlatButton(
            right, "导出 CSV", self.export_csv,
            bg=CARD, fg=MUTED, hover_bg=BG, border=BORDER,
            font=(f, 9), padx=px(10), pady=px(6),
        )
        self.export_btn.pack(side="left", padx=(0, px(6)))
        self.settings_btn = FlatButton(
            right, "设置", self.app.open_settings,
            bg=CARD, fg=MUTED, hover_bg=BG, border=BORDER,
            font=(f, 9), padx=px(10), pady=px(6),
        )
        self.settings_btn.pack(side="left")

        sep = tk.Frame(self.win, bg=BORDER, height=1)
        sep.pack(fill="x")

        body = tk.Frame(self.win, bg=BG)
        body.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(body, bg=BG, highlightthickness=0, bd=0)
        self.canvas.pack(side="left", fill="both", expand=True)
        style = ttk.Style(self.win)
        try:
            style.configure(
                "Book.Vertical.TScrollbar",
                gripcount=0, background=BORDER, darkcolor=BG, lightcolor=BG,
                troughcolor=BG, bordercolor=BG, arrowcolor=MUTED,
                arrowsize=px(9), width=px(9),
            )
        except Exception:
            pass
        self.vbar = ttk.Scrollbar(
            body, orient="vertical", command=self.canvas.yview,
            style="Book.Vertical.TScrollbar",
        )
        self.vbar.pack(side="right", fill="y")
        self.canvas.configure(yscrollcommand=self.vbar.set)
        self.inner = tk.Frame(self.canvas, bg=BG)
        self._inner_id = self.canvas.create_window(
            (0, 0), window=self.inner, anchor="nw"
        )
        self.inner.bind("<Configure>", self._on_inner_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self.inner.bind("<MouseWheel>", self._on_wheel)
        self.canvas.bind("<Enter>", lambda _e: self._bind_wheel(True))
        self.canvas.bind("<Leave>", lambda _e: self._bind_wheel(False))

        footer = tk.Frame(self.win, bg=CARD)
        footer.pack(fill="x")
        self.footer_label = tk.Label(
            footer, text="数据保存在本文件夹的「生词本.json」",
            bg=CARD, fg=FAINT, font=(f, 8), anchor="w",
        )
        self.footer_label.pack(fill="x", padx=px(22), pady=px(9))

    def _on_inner_configure(self, _event: Any = None) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_configure(self, event: tk.Event) -> None:
        self.canvas.itemconfigure(self._inner_id, width=int(event.width))

    def _on_wheel(self, event: tk.Event) -> str:
        self.canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")
        return "break"

    def _search_focus_in(self, _event: Any = None) -> None:
        if self._search_placeholder:
            self._search_placeholder = False
            self.search_var.set("")
            self.search_entry.configure(fg=INK)

    def _search_focus_out(self, _event: Any = None) -> None:
        if not self.search_var.get().strip():
            self._search_placeholder = True
            self.search_var.set("搜索生词")
            self.search_entry.configure(fg=FAINT)
            self.refresh()

    def _search_query(self) -> str:
        return "" if self._search_placeholder else self.search_var.get().strip()

    def _bind_wheel(self, bind: bool) -> None:
        if bind:
            self.canvas.bind_all("<MouseWheel>", self._on_wheel)
        else:
            self.canvas.unbind_all("<MouseWheel>")

    # ------------------------------------------------------------------
    def refresh(self) -> None:
        px = self.app.px
        f = self.app.font_ui
        for child in self.inner.winfo_children():
            child.destroy()
        groups = self.app.vocab.grouped(self._search_query())
        total = self.app.vocab.count()
        today = self.app.vocab.today_count()
        self.subtitle.configure(
            text=f"按天整理 · 共 {total} 个 · 今天新增 {today} 个"
        )
        if not groups:
            empty = tk.Frame(self.inner, bg=BG)
            empty.pack(fill="both", expand=True, padx=px(24), pady=px(60))
            tk.Label(empty, text="✦", bg=BG, fg=ACCENT,
                     font=(f, 28)).pack()
            tk.Label(
                empty,
                text="还没有收藏生词" if total == 0 else "没有找到匹配的生词",
                bg=BG, fg=INK, font=(f, 13, "bold"),
            ).pack(pady=(px(8), px(4)))
            tk.Label(
                empty,
                text=(
                    "在翻译弹窗里点击「＋ 生词本」即可收藏。"
                    if total == 0 else "换个关键词试试。"
                ),
                bg=BG, fg=MUTED, font=(f, 9),
            ).pack()
            return

        wrap = max(px(260), self.canvas.winfo_width() - px(220))
        for group in groups:
            self._build_group(group, wrap, px, f)
        spacer = tk.Frame(self.inner, bg=BG, height=px(18))
        spacer.pack(fill="x")

    def _build_group(self, group: dict[str, Any], wrap: int, px: int, f: str) -> None:
        header = tk.Frame(self.inner, bg=BG)
        header.pack(fill="x", padx=px(22), pady=(px(18), px(7)))
        tk.Label(
            header, text=group["label"], bg=BG, fg=INK,
            font=(f, 12, "bold"),
        ).pack(side="left")
        day = group["date"]
        sub = (
            f"{day.month}月{day.day}日 {group['weekday']}"
            if group["label"] in ("今天", "昨天")
            else group["weekday"]
        )
        tk.Label(
            header, text=sub,
            bg=BG, fg=FAINT, font=(f, 8),
        ).pack(side="left", padx=(px(8), 0), pady=(px(2), 0))
        count = len(group["entries"])
        tk.Label(
            header, text=f"{count}", bg=ACCENT_SOFT, fg=ACCENT,
            font=(f, 8, "bold"), padx=px(7), pady=px(1),
        ).pack(side="right")

        card = tk.Frame(
            self.inner, bg=CARD, highlightthickness=1,
            highlightbackground=BORDER,
        )
        card.pack(fill="x", padx=px(22))
        entries = group["entries"]
        for i, entry in enumerate(entries):
            self._build_entry(card, entry, wrap, px, f)
            if i != len(entries) - 1:
                tk.Frame(card, bg=BORDER_SOFT, height=1).pack(fill="x",
                                                               padx=px(14))

    def _build_entry(self, parent: tk.Frame, entry: dict[str, Any],
                     wrap: int, px: int, f: str) -> None:
        row = tk.Frame(parent, bg=CARD)
        row.pack(fill="x", padx=px(14), pady=px(10))

        right = tk.Frame(row, bg=CARD)
        right.pack(side="right", anchor="n", padx=(px(8), 0))
        tk.Label(
            right, text=self.app.vocab.time_text(entry), bg=CARD, fg=FAINT,
            font=(f, 8),
        ).pack(anchor="e")
        actions = tk.Frame(right, bg=CARD)
        actions.pack(anchor="e", pady=(px(5), 0))
        search = tk.Label(
            actions, text="搜", bg=CARD, fg=MUTED, font=(f, 8),
            padx=px(5), pady=px(1), cursor="hand2",
        )
        search.pack(side="left")
        delete = tk.Label(
            actions, text="删", bg=CARD, fg=FAINT, font=(f, 8),
            padx=px(5), pady=px(1), cursor="hand2",
        )
        delete.pack(side="left")
        for w in (search, delete):
            w.bind("<Enter>", lambda _e, x=w: x.configure(fg=ACCENT))
            w.bind("<Leave>", lambda _e, x=w: x.configure(fg=MUTED))
        search.bind("<Button-1>", lambda _e, t=entry["text"]: self.app.search_text(t))
        delete.bind("<Button-1>", lambda _e, eid=entry["id"]: self.delete_entry(eid))

        left = tk.Frame(row, bg=CARD)
        left.pack(side="left", fill="x", expand=True)
        top_line = tk.Frame(left, bg=CARD)
        top_line.pack(fill="x", anchor="w")
        word = tk.Label(
            top_line, text=entry["text"], bg=CARD, fg=INK,
            font=(f, 12, "bold"), cursor="hand2", anchor="w",
        )
        word.pack(side="left")
        pos = str(entry.get("pos") or "")
        if pos:
            bg, fg = DEFAULT_POS_COLOR
            for p in [x.strip() for x in pos.split("/") if x.strip()]:
                if p in POS_COLORS:
                    bg, fg = POS_COLORS[p]
                    break
            tk.Label(
                top_line, text=f" {pos} ", bg=bg, fg=fg,
                font=(f, 7, "bold"), padx=px(3), pady=px(1),
            ).pack(side="left", padx=(px(6), 0))
        translation = str(entry.get("translation") or "")
        trans = tk.Label(
            left, text=truncate_text(translation, 120), bg=CARD, fg=MUTED,
            font=(f, 9), anchor="w", justify="left", wraplength=wrap,
        )
        trans.pack(fill="x", pady=(px(3), 0))
        for w in (word, trans):
            w.bind("<Button-1>", lambda _e, t=entry["text"]: self.app.open_from_wordbook(t))
            w.bind("<Enter>", lambda _e, x=w: x.configure(fg=ACCENT_DARK if x is word else INK))
            w.bind("<Leave>",
                   lambda _e, x=w: x.configure(fg=INK if x is word else MUTED))

    def delete_entry(self, entry_id: str) -> None:
        entry = next((e for e in self.app.vocab.all() if e.get("id") == entry_id), None)
        if not entry:
            return
        if not messagebox.askyesno(
            "删除生词",
            f"从生词本删除「{entry['text']}」？",
            parent=self.win,
        ):
            return
        self.app.vocab.remove(entry_id)
        self.refresh()
        self.app.toast.show("已删除")

    def export_csv(self) -> None:
        name = f"生词本_{datetime.now().strftime('%Y%m%d')}.csv"
        path = self.app.data_dir / name
        try:
            n = self.app.vocab.export_csv(path)
        except Exception:
            logging.getLogger(__name__).exception("导出 CSV 失败")
            self.app.toast.show("导出失败，请查看日志")
            return
        self.app.toast.show(f"已导出 {n} 条 · {name}", 2600)

    def show(self) -> None:
        self.refresh()
        if not self._placed:
            px = self.app.px
            left, top, right, bottom = winutil.get_work_area()
            w = min(px(880), right - left - px(60))
            h = min(px(700), bottom - top - px(60))
            x = left + (right - left - w) // 2
            y = top + (bottom - top - h) // 2
            self.win.geometry(f"{w}x{h}+{x}+{y}")
            self._placed = True
        self.win.deiconify()
        self.win.lift()
        try:
            self.win.focus_force()
        except Exception:
            pass
        self.win.after(80, self.refresh)

    def hide(self) -> None:
        self.win.withdraw()


# ---------------------------------------------------------------------------
# 设置窗口
# ---------------------------------------------------------------------------
class SettingsWindow:
    def __init__(self, app: "App"):
        self.app = app
        self._placed = False
        self.win = tk.Toplevel(app.root)
        self.win.title("设置 · 划词翻译")
        self.win.configure(bg=BG)
        self.win.withdraw()
        try:
            if app.icon_path.exists():
                self.win.iconbitmap(str(app.icon_path))
        except Exception:
            pass
        self.win.resizable(False, False)
        self.win.protocol("WM_DELETE_WINDOW", self.hide)
        self.win.bind("<Escape>", lambda _e: self.hide())
        self.vars: dict[str, tk.BooleanVar] = {}
        self._build()
        self.win.update_idletasks()
        hwnd = winutil.get_root_window(int(self.win.winfo_id()))
        if hwnd:
            app.register_ui_window(hwnd)

    def _build(self) -> None:
        px = self.app.px
        f = self.app.font_ui
        self.content = tk.Frame(self.win, bg=BG)
        self.content.pack(fill="both", expand=True, padx=px(22), pady=px(18))
        tk.Label(self.content, text="设置", bg=BG, fg=INK,
                 font=(f, 18, "bold")).pack(anchor="w")
        tk.Label(
            self.content, text="调整划词方式与数据选项。修改会立即生效。",
            bg=BG, fg=MUTED, font=(f, 9),
        ).pack(anchor="w", pady=(px(3), px(14)))

        card = tk.Frame(self.content, bg=CARD, highlightthickness=1,
                        highlightbackground=BORDER)
        card.pack(fill="x")
        self._check(card, "auto_enabled", "划词后自动翻译",
                    "选中文字松开鼠标，立即弹出翻译。关闭后仍可用 Alt+Q。")
        self._divider(card)
        self._check(card, "double_click", "双击单词也翻译",
                    "双击选中一个单词时同样触发。")
        self._divider(card)
        self._check(card, "show_launcher", "显示桌面悬浮球",
                    "屏幕右侧的小圆球，左键打开生词本，右键显示菜单。")
        self._divider(card)
        self._check(card, "auto_start", "开机自动启动",
                    "登录 Windows 后自动在后台运行。")

        engine_card = tk.Frame(self.content, bg=CARD, highlightthickness=1,
                               highlightbackground=BORDER)
        engine_card.pack(fill="x", pady=(px(14), 0))
        row = tk.Frame(engine_card, bg=CARD)
        row.pack(fill="x", padx=px(16), pady=px(13))
        txt = tk.Frame(row, bg=CARD)
        txt.pack(side="left", fill="x", expand=True)
        tk.Label(txt, text="网页搜索使用", bg=CARD, fg=INK,
                 font=(f, 10, "bold")).pack(anchor="w")
        tk.Label(txt, text="点击弹窗里的「网页搜索」时打开",
                 bg=CARD, fg=MUTED, font=(f, 8)).pack(anchor="w", pady=(px(2), 0))
        self.engine_var = tk.StringVar()
        self.engine_combo = ttk.Combobox(
            row, textvariable=self.engine_var,
            values=[v[0] for v in ENGINES.values()],
            state="readonly", width=10, font=(f, 9),
        )
        self.engine_combo.pack(side="right")
        self.engine_combo.bind("<<ComboboxSelected>>", self._on_engine)

        util_card = tk.Frame(self.content, bg=CARD, highlightthickness=1,
                             highlightbackground=BORDER)
        util_card.pack(fill="x", pady=(px(14), 0))
        util = tk.Frame(util_card, bg=CARD)
        util.pack(fill="x", padx=px(16), pady=px(13))
        self.cache_label = tk.Label(
            util, text="", bg=CARD, fg=MUTED, font=(f, 8), anchor="w",
        )
        self.cache_label.pack(side="left", fill="x", expand=True)
        FlatButton(util, "清空缓存", self.clear_cache, bg=CARD, fg=MUTED,
                   hover_bg=BG, border=BORDER, font=(f, 8),
                   padx=px(9), pady=px(4)).pack(side="right", padx=(px(6), 0))
        FlatButton(util, "打开数据文件夹", self.app.open_data_folder,
                   bg=CARD, fg=MUTED, hover_bg=BG, border=BORDER,
                   font=(f, 8), padx=px(9), pady=px(4)).pack(side="right")

        hotkey_card = tk.Frame(self.content, bg=ACCENT_PALE,
                               highlightthickness=1,
                               highlightbackground="#D8EFEA")
        hotkey_card.pack(fill="x", pady=(px(14), 0))
        hk = tk.Frame(hotkey_card, bg=ACCENT_PALE)
        hk.pack(fill="x", padx=px(16), pady=px(12))
        tk.Label(hk, text="快捷键", bg=ACCENT_PALE, fg=ACCENT_DARK,
                 font=(f, 9, "bold")).pack(anchor="w")
        self.hotkey_label = tk.Label(
            hk, text="", bg=ACCENT_PALE, fg=MUTED,
            font=(f, 8), justify="left", anchor="w",
        )
        self.hotkey_label.pack(anchor="w", pady=(px(3), 0))

        tk.Label(
            self.content,
            text=f"划词翻译 v{VERSION} · 腾讯翻译 + 有道词典 · 数据仅保存在本机",
            bg=BG, fg=FAINT, font=(f, 8),
        ).pack(anchor="w", pady=(px(14), 0))

    def _divider(self, parent: tk.Frame) -> None:
        tk.Frame(parent, bg=BORDER_SOFT, height=1).pack(fill="x", padx=self.app.px(16))

    def _check(self, parent: tk.Frame, key: str, title: str, desc: str) -> None:
        px = self.app.px
        f = self.app.font_ui
        row = tk.Frame(parent, bg=CARD)
        row.pack(fill="x", padx=px(16), pady=px(11))
        txt = tk.Frame(row, bg=CARD)
        txt.pack(side="left", fill="x", expand=True)
        tk.Label(txt, text=title, bg=CARD, fg=INK,
                 font=(f, 10, "bold")).pack(anchor="w")
        tk.Label(txt, text=desc, bg=CARD, fg=MUTED,
                 font=(f, 8), justify="left").pack(anchor="w", pady=(px(2), 0))
        var = tk.BooleanVar(value=bool(self.app.config.get(key)))
        self.vars[key] = var
        sw = ToggleSwitch(
            row, var, command=lambda k=key, v=var: self.app.set_option(k, bool(v.get())),
            bg=CARD, width=px(42), height=px(23),
        )
        sw.pack(side="right", padx=(px(8), 0))

    def _on_engine(self, _event: Any = None) -> None:
        label = self.engine_var.get()
        for key, (name, _url) in ENGINES.items():
            if name == label:
                self.app.config.set("search_engine", key)
                self.app.toast.show(f"网页搜索：{name}")
                break

    def refresh(self) -> None:
        for key, var in self.vars.items():
            var.set(bool(self.app.config.get(key)))
        label = ENGINES.get(self.app.config.get("search_engine", "bing"),
                            ENGINES["bing"])[0]
        self.engine_var.set(label)
        self.cache_label.configure(
            text=f"翻译缓存 {self.app.translator.cache.count()} 条"
        )
        hk = self.app.hook.hotkeys if self.app.hook else {}
        q = hk.get(101) or "Alt+Q"
        w = hk.get(102) or "Alt+W"
        self.hotkey_label.configure(
            text=f"{q}  翻译当前选中文字\n{w}  打开生词本\nEsc  关闭翻译弹窗"
        )

    def clear_cache(self) -> None:
        if not messagebox.askyesno("清空缓存", "清空本地翻译缓存？\n生词本不会受影响。",
                                   parent=self.win):
            return
        self.app.translator.cache.clear()
        self.app.translator.save_cache(force=True)
        self.refresh()
        self.app.toast.show("翻译缓存已清空")

    def show(self) -> None:
        self.refresh()
        if not self._placed:
            px = self.app.px
            self.win.update_idletasks()
            w = max(px(560), self.win.winfo_reqwidth())
            h = self.win.winfo_reqheight()
            left, top, right, bottom = winutil.get_work_area()
            x = left + (right - left - w) // 2
            y = top + max(px(30), (bottom - top - h) // 2)
            self.win.geometry(f"{w}x{h}+{x}+{y}")
            self._placed = True
        self.win.deiconify()
        self.win.lift()
        try:
            self.win.focus_force()
        except Exception:
            pass

    def hide(self) -> None:
        self.win.withdraw()


# ---------------------------------------------------------------------------
# 首次启动引导
# ---------------------------------------------------------------------------
class WelcomeWindow:
    def __init__(self, app: "App"):
        self.app = app
        self.win = tk.Toplevel(app.root)
        self.win.title("欢迎使用划词翻译")
        self.win.configure(bg=CARD)
        self.win.withdraw()
        try:
            if app.icon_path.exists():
                self.win.iconbitmap(str(app.icon_path))
        except Exception:
            pass
        self.win.protocol("WM_DELETE_WINDOW", self.finish)
        self.win.bind("<Escape>", lambda _e: self.finish())
        self._build()
        self.win.update_idletasks()
        hwnd = winutil.get_root_window(int(self.win.winfo_id()))
        if hwnd:
            app.register_ui_window(hwnd)

    def _build(self) -> None:
        px = self.app.px
        f = self.app.font_ui
        body = tk.Frame(self.win, bg=CARD)
        body.pack(fill="both", expand=True, padx=px(28), pady=px(18))

        logo = tk.Canvas(body, width=px(52), height=px(52), bg=CARD,
                         highlightthickness=0)
        logo.pack(anchor="w")
        round_rect(logo, px(2), px(2), px(50), px(50), px(13),
                   fill=ACCENT, outline="")
        logo.create_text(px(26), px(27), text="译", fill="#FFFFFF",
                         font=(f, 21, "bold"))

        tk.Label(body, text="划词翻译已就绪", bg=CARD, fg=INK,
                 font=(f, 20, "bold")).pack(anchor="w", pady=(px(11), px(3)))
        tk.Label(
            body, text="在任意软件里选中文字，松开鼠标即可翻译。",
            bg=CARD, fg=MUTED, font=(f, 10),
        ).pack(anchor="w")

        bullets = [
            ("中英自动识别", "英文显示词性、释义、短语和例句；中文自动译成英文。"),
            ("按天整理生词本", "点「＋ 生词本」收藏，右边悬浮球随时打开。"),
            ("不打断你的节奏", "本地缓存让常用词几乎瞬间出现；Esc 关闭弹窗。"),
        ]
        box = tk.Frame(body, bg=ACCENT_PALE)
        box.pack(fill="x", pady=(px(12), px(10)))
        for title, desc in bullets:
            row = tk.Frame(box, bg=ACCENT_PALE)
            row.pack(fill="x", padx=px(14), pady=px(5))
            tk.Label(row, text="✓", bg=ACCENT_PALE, fg=ACCENT,
                     font=(f, 9, "bold")).pack(side="left", anchor="n")
            txt = tk.Frame(row, bg=ACCENT_PALE)
            txt.pack(side="left", fill="x", expand=True, padx=(px(7), 0))
            tk.Label(txt, text=title, bg=ACCENT_PALE, fg=ACCENT_DARK,
                     font=(f, 9, "bold")).pack(anchor="w")
            tk.Label(txt, text=desc, bg=ACCENT_PALE, fg=MUTED,
                     font=(f, 8), justify="left").pack(anchor="w")

        hk = tk.Frame(body, bg=CARD)
        hk.pack(fill="x", pady=(0, px(10)))
        self._pill(hk, "Alt+Q", "翻译选中文字")
        self._pill(hk, "Alt+W", "打开生词本")
        self._pill(hk, "Esc", "关闭弹窗")

        self.autostart_var = tk.BooleanVar(value=bool(self.app.config.get("auto_start")))
        auto_row = tk.Frame(body, bg=CARD)
        auto_row.pack(anchor="w", pady=(px(2), px(12)))
        ToggleSwitch(
            auto_row, self.autostart_var, bg=CARD,
            width=self.app.px(42), height=self.app.px(23),
        ).pack(side="left")
        tk.Label(auto_row, text="开机自动启动", bg=CARD, fg=MUTED,
                 font=(f, 9)).pack(side="left", padx=(px(8), 0))

        btn_row = tk.Frame(body, bg=CARD)
        btn_row.pack(fill="x")
        FlatButton(
            btn_row, "开始使用", self.finish, bg=ACCENT, fg="#FFFFFF",
            hover_bg=ACCENT_DARK, font=(f, 10, "bold"),
            padx=px(24), pady=px(8),
        ).pack(side="right")
        FlatButton(
            btn_row, "使用说明", self._open_readme, bg=CARD, fg=MUTED,
            hover_bg=BG, border=BORDER, font=(f, 9),
            padx=px(14), pady=px(8),
        ).pack(side="right", padx=(0, px(8)))

    def _pill(self, parent: tk.Frame, key: str, text: str) -> None:
        px = self.app.px
        f = self.app.font_ui
        wrap = tk.Frame(parent, bg=BG)
        wrap.pack(side="left", padx=(0, px(6)))
        tk.Label(wrap, text=key, bg=BG, fg=INK, font=(f, 8, "bold"),
                 padx=px(6), pady=px(2)).pack(side="left")
        tk.Label(wrap, text=text, bg=CARD, fg=FAINT,
                 font=(f, 8)).pack(side="left", padx=(px(3), 0))

    def _open_readme(self) -> None:
        for name in ("使用说明.txt", "README.md"):
            for base in (self.app.base_dir, self.app.resource_dir):
                readme = base / name
                if readme.exists():
                    os.startfile(str(readme))  # type: ignore[attr-defined]
                    return

    def show(self) -> None:
        px = self.app.px
        self.win.update_idletasks()
        w = max(px(540), self.win.winfo_reqwidth())
        h = self.win.winfo_reqheight()
        left, top, right, bottom = winutil.get_work_area(0, 0)
        w = min(w, right - left - px(20))
        h = min(h, bottom - top - px(20))
        x = left + max(px(8), (right - left - w) // 2)
        y = top + max(px(8), (bottom - top - h) // 2)
        self.win.geometry(f"{w}x{h}+{x}+{y}")
        self.win.deiconify()
        self.win.lift()
        try:
            self.win.focus_force()
        except Exception:
            pass

    def finish(self) -> None:
        self.app.set_option("auto_start", bool(self.autostart_var.get()))
        self.app.config.set("first_run", False)
        self.win.withdraw()
        self.app.toast.show("划词即译已开启 · 选中文字试试", 2600)


# ---------------------------------------------------------------------------
# 桌面悬浮球
# ---------------------------------------------------------------------------
class LauncherBubble:
    def __init__(self, app: "App"):
        self.app = app
        self.win = tk.Toplevel(app.root)
        self.win.withdraw()
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        try:
            self.win.attributes("-alpha", 0.96)
            self.win.attributes("-transparentcolor", "#010203")
        except Exception:
            pass
        self.size = app.px(52)
        self.win.configure(bg="#010203")
        self.canvas = tk.Canvas(
            self.win, width=self.size, height=self.size, bg="#010203",
            highlightthickness=0, bd=0,
        )
        self.canvas.pack()
        self._build()
        self._drag_start: Optional[tuple[int, int, int, int]] = None
        self._dragged = False
        self.win.update_idletasks()
        hwnd = winutil.get_root_window(int(self.win.winfo_id()))
        if hwnd:
            self.hwnd = hwnd
            app.register_ui_window(hwnd)
        self._place(initial=True)

    def _build(self) -> None:
        px = self.app.px
        self.canvas.delete("all")
        self.canvas.create_oval(
            px(3), px(3), self.size - px(3), self.size - px(3),
            fill=ACCENT, outline=ACCENT_DARK, width=1,
        )
        self.canvas.create_text(
            self.size / 2, self.size / 2 - px(1), text="译",
            fill="#FFFFFF", font=(self.app.font_ui, 15, "bold"),
        )
        self.dot = self.canvas.create_oval(
            self.size - px(14), self.size - px(14),
            self.size - px(8), self.size - px(8),
            fill="#7EE2C4", outline="#FFFFFF", width=1,
        )
        self.canvas.bind("<Enter>", self._on_enter)
        self.canvas.bind("<Leave>", self._on_leave)
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_motion)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Button-3>", self._on_menu)
        self.win.bind("<Button-3>", self._on_menu)

    def _on_enter(self, _event: Any = None) -> None:
        try:
            self.win.attributes("-alpha", 1.0)
        except Exception:
            pass

    def _on_leave(self, _event: Any = None) -> None:
        try:
            self.win.attributes("-alpha", 0.96)
        except Exception:
            pass

    def _on_press(self, event: tk.Event) -> None:
        self._drag_start = (
            int(event.x_root), int(event.y_root),
            int(self.win.winfo_x()), int(self.win.winfo_y()),
        )
        self._dragged = False

    def _on_motion(self, event: tk.Event) -> None:
        if not self._drag_start:
            return
        sx, sy, wx, wy = self._drag_start
        dx = int(event.x_root) - sx
        dy = int(event.y_root) - sy
        if abs(dx) + abs(dy) > 4:
            self._dragged = True
        self._move(wx + dx, wy + dy)

    def _on_release(self, _event: Any = None) -> None:
        if self._dragged:
            pos = [int(self.win.winfo_x()), int(self.win.winfo_y())]
            self.app.config.set("launcher_pos", pos)
        else:
            self.app.open_wordbook()
        self._drag_start = None

    def _on_menu(self, event: tk.Event) -> None:
        px = self.app.px
        f = self.app.font_ui
        menu = tk.Menu(
            self.win, tearoff=0, bg=CARD, fg=INK,
            activebackground=ACCENT_SOFT, activeforeground=ACCENT_DARK,
            font=(f, 9), bd=0,
        )
        menu.add_command(label="打开生词本", command=self.app.open_wordbook)
        menu.add_separator()
        menu.add_command(
            label="暂停划词" if self.app.config.get("auto_enabled") else "启用划词",
            command=self.app.toggle_auto,
        )
        menu.add_command(label="设置", command=self.app.open_settings)
        menu.add_separator()
        menu.add_checkbutton(
            label="开机自启", onvalue=True, offvalue=False,
            variable=tk.BooleanVar(value=bool(self.app.config.get("auto_start"))),
            command=lambda: self.app.set_option("auto_start",
                                                not bool(self.app.config.get("auto_start"))),
        )
        menu.add_separator()
        menu.add_command(label="退出", command=self.app.quit)
        try:
            menu.tk_popup(int(event.x_root), int(event.y_root))
        finally:
            menu.grab_release()

    def _move(self, x: int, y: int) -> None:
        left, top, right, bottom = winutil.get_work_area(x + self.size // 2,
                                                         y + self.size // 2)
        x = max(left + self.app.px(2), min(int(x), right - self.size - self.app.px(2)))
        y = max(top + self.app.px(2), min(int(y), bottom - self.size - self.app.px(2)))
        self.win.geometry(f"+{x}+{y}")

    def _place(self, initial: bool = False) -> None:
        pos = self.app.config.get("launcher_pos")
        if pos and isinstance(pos, (list, tuple)) and len(pos) == 2:
            x, y = int(pos[0]), int(pos[1])
        else:
            left, top, right, bottom = winutil.get_work_area(0, 0)
            x = right - self.size - self.app.px(16)
            y = top + (bottom - top) // 2 - self.size // 2
        self._move(x, y)

    def set_state(self, enabled: bool) -> None:
        try:
            self.canvas.itemconfigure(
                self.dot, fill="#7EE2C4" if enabled else "#F6C177"
            )
        except Exception:
            pass

    def show(self) -> None:
        self._place()
        self.win.deiconify()
        self.win.lift()
        try:
            winutil.keep_topmost_noactivate(self.hwnd)
        except Exception:
            pass

    def hide(self) -> None:
        self.win.withdraw()


# ---------------------------------------------------------------------------
# 主程序
# ---------------------------------------------------------------------------
class App:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.base_dir = APP_DIR
        self.resource_dir = RESOURCE_DIR
        self.data_dir = Path(args.data_dir).resolve() if args.data_dir else APP_DIR
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.icon_path = self.resource_dir / "图标.ico"
        self.script_path = (Path(sys.executable).resolve() if FROZEN
                            else Path(__file__).resolve())
        self._ui_hwnds: set[int] = set()
        self._closing = False
        self._latest_rid = 0
        self.current_result = None
        self.current_text = ""
        self.current_anchor = (0, 0)
        self._capture_queue: queue.Queue = queue.Queue(maxsize=1)
        self._translation_queue: queue.Queue = queue.Queue(maxsize=1)
        self.ui_queue: queue.Queue = queue.Queue()

        winutil.set_dpi_awareness()
        winutil.set_app_user_model_id()
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title(APP_TITLE)
        try:
            if self.icon_path.exists():
                self.root.iconbitmap(str(self.icon_path))
        except Exception:
            pass

        dpi = float(self.root.winfo_fpixels("1i"))
        self.scale = max(1.0, dpi / 96.0)
        try:
            self.root.tk.call("tk", "scaling", dpi / 72.0)
        except Exception:
            pass
        self.font_ui = self._pick_font()
        self.root.report_callback_exception = self._tk_exception

        self.config = Config(self.data_dir / "设置.json")
        from vocab_store import VocabStore
        from trans_core import Translator

        self.vocab = VocabStore(self.data_dir / "生词本.json")
        self.translator = Translator(self.data_dir / "翻译缓存.json")
        self.toast = Toast(self)
        self.popup = TranslationPopup(self)
        self.wordbook = WordbookWindow(self)
        self.settings = SettingsWindow(self)
        self.welcome = WelcomeWindow(self)
        self.bubble = LauncherBubble(self)
        self.bubble.set_state(bool(self.config.get("auto_enabled")))

        self.hook: Optional[winutil.GlobalInput] = None
        if not args.no_hook and not (args.demo or args.demo_wordbook):
            self.hook = winutil.GlobalInput(
                self._on_hook_event,
                self._ui_windows,
                lambda: self.popup.visible,
                lambda: bool(self.config.get("auto_enabled")),
            )
            self.hook.start()
        self.settings.hotkey_label.configure(
            text="Alt+Q  翻译当前选中文字\nAlt+W  打开生词本\nEsc  关闭翻译弹窗"
        )

        self._capture_thread = threading.Thread(
            target=self._capture_worker, name="capture-worker", daemon=True
        )
        self._capture_thread.start()
        self._translation_thread = threading.Thread(
            target=self._translation_worker, name="translation-worker", daemon=True
        )
        self._translation_thread.start()

        self.root.after(25, self._drain_events)
        self.root.after(20000, self._periodic)
        self.root.protocol("WM_DELETE_WINDOW", self.quit)

        self.bubble.set_state(bool(self.config.get("auto_enabled")))
        if not self.config.get("show_launcher", True):
            self.bubble.hide()
        if args.demo:
            self.bubble.hide()
            self.root.after(500, self._show_demo_popup)
        elif args.demo_wordbook:
            self.bubble.hide()
            self.root.after(500, self._show_demo_wordbook)
        elif self.config.get("first_run", True):
            if self.config.get("show_launcher", True):
                self.bubble.show()
            self.root.after(500, self.welcome.show)
        else:
            if self.config.get("show_launcher", True):
                self.bubble.show()
                self.root.after(700, lambda: self.toast.show(
                    "划词翻译已就绪 · 选中文字即可翻译", 2400
                ))

    # ------------------------------------------------------------------
    def _pick_font(self) -> str:
        try:
            families = set(tkfont.families(self.root))
        except Exception:
            families = set()
        for name in ("Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI",
                     "Arial Unicode MS"):
            if name in families:
                return name
        return "Segoe UI"

    def px(self, value: float) -> int:
        return max(1, int(round(float(value) * self.scale)))

    def _tk_exception(self, exc: type, val: BaseException, tb: Any) -> None:
        logging.getLogger(__name__).error(
            "Tk 回调异常", exc_info=(exc, val, tb)
        )
        try:
            self.toast.show("程序遇到一个错误，已记录日志", 2600)
        except Exception:
            pass

    # ------------------------------------------------------------------
    def register_ui_window(self, hwnd: int) -> None:
        if hwnd:
            self._ui_hwnds.add(int(hwnd))

    def _ui_windows(self) -> tuple[int, ...]:
        return tuple(self._ui_hwnds)

    def _on_hook_event(self, event: winutil.HookEvent) -> None:
        if event.kind in ("select", "double"):
            if event.kind == "double" and not self.config.get("double_click", True):
                return
            anchor = (event.x, event.y) if event.x or event.y else winutil.get_cursor_pos()
            self._request_capture(anchor, manual=False)
        elif event.kind == "outside":
            if self.popup.visible:
                self.ui_queue.put(("hide_popup",))
        elif event.kind == "escape":
            if self.popup.visible:
                self.ui_queue.put(("hide_popup",))
        elif event.kind == "hotkey":
            if event.hotkey_id == 101:
                self._request_capture(winutil.get_cursor_pos(), manual=True)
            elif event.hotkey_id == 102:
                self.ui_queue.put(("open_wordbook",))

    # ------------------------------------------------------------------
    def _request_capture(self, anchor: tuple[int, int], manual: bool) -> None:
        if self._closing:
            return
        req = (anchor, manual, time.time())
        try:
            self._capture_queue.put_nowait(req)
        except queue.Full:
            try:
                self._capture_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._capture_queue.put_nowait(req)
            except queue.Full:
                pass

    def _capture_worker(self) -> None:
        while True:
            item = self._capture_queue.get()
            if item is None:
                return
            anchor, manual, _ts = item
            try:
                time.sleep(0.012 if manual else 0.045)
                text, reason = winutil.capture_selection(
                    settle=0.02 if manual else 0.04,
                    wait_mods=0.7 if manual else 0.0,
                )
                self.ui_queue.put(("captured", text, reason, anchor, manual))
            except Exception:
                logging.getLogger(__name__).exception("取词异常")
                self.ui_queue.put(("captured", "", "error", anchor, manual))

    # ------------------------------------------------------------------
    def _translation_worker(self) -> None:
        while True:
            item = self._translation_queue.get()
            if item is None:
                return
            rid, text, src_override, force = item
            if rid != self._latest_rid:
                continue
            try:
                result = self.translator.translate(
                    text, src_override=src_override, force=force
                )
                self.ui_queue.put(("translated", rid, result))
            except Exception as exc:
                logging.getLogger(__name__).info("翻译失败：%s", exc)
                self.ui_queue.put(("translate_error", rid, str(exc)))

    def _drain_events(self) -> None:
        if self._closing:
            return
        try:
            while True:
                event = self.ui_queue.get_nowait()
                kind = event[0]
                if kind == "captured":
                    self._on_captured(*event[1:])
                elif kind == "translated":
                    self._on_translated(event[1], event[2])
                elif kind == "translate_error":
                    self._on_translate_error(event[1], event[2])
                elif kind == "hide_popup":
                    self.popup.hide()
                elif kind == "open_wordbook":
                    self.open_wordbook()
                elif kind == "toast":
                    self.toast.show(event[1], event[2] if len(event) > 2 else 1900)
        except queue.Empty:
            pass
        except Exception:
            logging.getLogger(__name__).exception("处理界面事件异常")
        if not self._closing:
            self.root.after(25, self._drain_events)

    def _on_captured(self, text: str, reason: str, anchor: tuple[int, int],
                     manual: bool) -> None:
        text = re.sub(r"[ \t]+", " ", (text or "").replace("\r\n", "\n")).strip()
        if not text:
            if manual:
                msg = {
                    "modifiers": "请松开快捷键后再试",
                    "clipboard_busy": "剪贴板被占用，请稍后重试",
                    "empty": "没有检测到选中文字，先选中再按 Alt+Q",
                    "error": "取词出错，请查看日志",
                }.get(reason, "没有检测到选中文字")
                self.toast.show(msg, 2200)
            return
        if not has_translatable(text):
            if manual:
                self.toast.show("选中的内容没有可翻译文字")
            return
        if len(text) > 1500:
            text = text[:1500]
            self.toast.show("文本较长，已截取前 1500 字", 2200)
        if (not manual and self.popup.visible and text == self.popup.current_text
                and self.popup.result is not None):
            self.popup.anchor = anchor
            self.popup.prepare_show()
            self.popup._paint_current(reposition=True)
            return
        self._show_translation(text, anchor)

    def _show_translation(self, text: str, anchor: tuple[int, int],
                          src_override: Optional[str] = None,
                          force: bool = False) -> None:
        from trans_core import detect_lang

        self._latest_rid += 1
        rid = self._latest_rid
        src = src_override or detect_lang(text)
        self.current_text = text
        self.current_anchor = anchor
        self.popup.anchor = anchor
        self.popup.prepare_show()
        if not force:
            cached = self.translator.cache.get(text, src)
            if cached:
                self.current_result = cached
                self.popup.render(cached, rid, reposition=True)
                return
        self.current_result = None
        self.popup.show_loading(text, src, anchor, rid)
        req = (rid, text, src_override, force)
        try:
            self._translation_queue.put_nowait(req)
        except queue.Full:
            try:
                self._translation_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._translation_queue.put_nowait(req)
            except queue.Full:
                pass

    def _on_translated(self, rid: int, result: Any) -> None:
        if rid != self._latest_rid or not self.popup.visible:
            return
        self.current_result = result
        self.popup.render(result, rid)

    def _on_translate_error(self, rid: int, message: str) -> None:
        if rid != self._latest_rid or not self.popup.visible:
            return
        self.current_result = None
        self.popup.render_error(message, rid)

    # ------------------------------------------------------------------
    def retry_current(self) -> None:
        if not self.current_text:
            return
        self._show_translation(self.current_text, self.current_anchor, force=True)

    def swap_direction(self) -> None:
        if not self.current_text:
            return
        src = "en"
        if self.popup.result:
            src = "en" if self.popup.result.src_lang == "zh" else "zh"
        elif self.popup.current_src == "en":
            src = "zh"
        self._show_translation(self.current_text, self.current_anchor,
                               src_override=src, force=False)

    def translate_phrase(self, phrase: str) -> None:
        if not phrase:
            return
        self._show_translation(phrase, winutil.get_cursor_pos())

    def open_from_wordbook(self, text: str) -> None:
        anchor = winutil.get_cursor_pos()
        self._show_translation(text, anchor)

    def add_current(self) -> None:
        if not self.current_result:
            return
        entry, added = self.vocab.add(
            self.current_result.text,
            self.current_result.translation,
            self.current_result.pos_summary(),
            self.current_result.src_lang,
            self.current_result.engine,
        )
        if added:
            self.toast.show("已加入生词本 · 今天")
        else:
            self.toast.show("这个生词已经在生词本里了")
        self.popup._update_add_button()
        if self.wordbook.win.winfo_viewable():
            self.wordbook.refresh()

    def copy_current(self) -> None:
        if self.current_result:
            text = self.current_result.plain_text()
        else:
            text = self.current_text
        if not text:
            return
        if winutil.copy_text(text):
            self.toast.show("已复制翻译")
        else:
            self.toast.show("复制失败，剪贴板被占用")

    def search_current(self) -> None:
        text = self.current_text
        self.search_text(text)
        self.popup.hide()

    def search_text(self, text: str) -> None:
        text = (text or "").strip()
        if not text:
            return
        key = str(self.config.get("search_engine", "bing"))
        name, pattern = ENGINES.get(key, ENGINES["bing"])
        q = urllib.parse.quote(text)
        lang = "zh" if re.search(r"[\u4e00-\u9fff]", text) else "en"
        url = pattern.format(q=q, lang=lang)
        threading.Thread(
            target=lambda: webbrowser.open(url, new=2), daemon=True
        ).start()
        self.toast.show(f"已在 {name} 中搜索")

    def play_current_audio(self) -> None:
        if not self.current_text:
            return
        q = urllib.parse.quote(self.current_text)
        url = f"https://dict.youdao.com/dictvoice?audio={q}&type=2"
        winutil.play_audio_url(url)

    # ------------------------------------------------------------------
    def set_option(self, key: str, value: Any) -> None:
        self.config.set(key, value)
        if key == "auto_enabled":
            self.bubble.set_state(bool(value))
            self.toast.show("划词已开启" if value else "划词已暂停")
        elif key == "show_launcher":
            if value:
                self.bubble.show()
            else:
                self.bubble.hide()
        elif key == "auto_start":
            ok = winutil.set_autostart(bool(value), str(self.script_path))
            if not ok:
                self.toast.show("设置开机自启失败")
                self.config.set("auto_start", False)
            else:
                self.toast.show("已开启开机自启" if value else "已关闭开机自启")
        if hasattr(self, "settings"):
            try:
                self.settings.refresh()
            except Exception:
                pass

    def toggle_auto(self) -> None:
        self.set_option("auto_enabled", not bool(self.config.get("auto_enabled")))

    def open_wordbook(self) -> None:
        self.wordbook.show()

    def open_settings(self) -> None:
        self.settings.show()

    def open_data_folder(self) -> None:
        winutil.open_folder(self.data_dir)

    def _periodic(self) -> None:
        if self._closing:
            return
        try:
            self.translator.save_cache()
        except Exception:
            logging.getLogger(__name__).exception("周期保存缓存失败")
        self.root.after(20000, self._periodic)

    # ------------------------------------------------------------------
    def quit(self) -> None:
        if self._closing:
            return
        self._closing = True
        logging.getLogger(__name__).info("退出程序")
        try:
            if self.hook:
                self.hook.stop()
        except Exception:
            pass
        try:
            self.config.save()
            self.vocab.save()
            self.translator.save_cache(force=True)
        except Exception:
            logging.getLogger(__name__).exception("退出保存失败")
        for q in (self._capture_queue, self._translation_queue):
            try:
                q.put_nowait(None)
            except Exception:
                pass
        try:
            self.root.quit()
        except Exception:
            pass
        try:
            self.root.destroy()
        except Exception:
            pass

    # ------------------------------------------------------------------
    def _show_demo_popup(self) -> None:
        from trans_core import Result, Sense

        result = Result(
            text="serendipity",
            src_lang="en",
            translation="意外发现美好事物的运气；机缘巧合",
            senses=[
                Sense("n.", "（发现珍奇事物的）意外能力；机缘巧合；运气"),
                Sense("n.", "意外发现美好事物的现象；不期而遇的惊喜"),
            ],
            phrases=[
                ("serendipity effect", "机缘巧合效应"),
                ("pure serendipity", "纯粹的意外之喜"),
                ("a moment of serendipity", "机缘巧合的瞬间"),
            ],
            forms=[("复数", "serendipities")],
            examples=[(
                "We stumbled upon the little bookshop by pure serendipity.",
                "我们纯粹靠机缘巧合发现了那家小书店。",
            )],
            phones=("ˌserənˈdɪpəti", "ˌserənˈdɪpəti"),
            engine="腾讯翻译 + 有道词典",
            latency_ms=168,
        )
        left, top, right, bottom = winutil.get_work_area()
        anchor = (left + (right - left) // 2 - self.px(210),
                  top + (bottom - top) // 2 - self.px(230))
        self.popup.anchor = anchor
        self.popup.current_text = result.text
        self.popup.render(result, 1)
        self.current_result = result
        self.current_text = result.text

    def _show_demo_wordbook(self) -> None:
        if self.vocab.count() == 0:
            now = datetime.now().astimezone()
            self.vocab.add("serendipity", "意外发现美好事物的运气；机缘巧合", "n.",
                           "en", "腾讯翻译 + 有道词典", created_at=now)
            self.vocab.add("take off", "起飞；脱下；突然成功", "v.", "en",
                           "腾讯翻译 + 有道词典",
                           created_at=now - timedelta(minutes=40))
            self.vocab.add("ephemeral", "短暂的；转瞬即逝的", "adj.", "en",
                           "腾讯翻译 + 有道词典",
                           created_at=now - timedelta(days=1, hours=2))
            self.vocab.add("resilience", "韧性；恢复力", "n.", "en",
                           "腾讯翻译 + 有道词典",
                           created_at=now - timedelta(days=1, hours=3))
            self.vocab.add("ubiquitous", "无处不在的；普遍存在的", "adj.", "en",
                           "腾讯翻译 + 有道词典",
                           created_at=now.replace(hour=15, minute=20, second=0,
                                                  microsecond=0) - timedelta(days=1))
            self.vocab.add("苹果", "apple", "n.", "zh", "腾讯翻译 + 有道词典",
                           created_at=now - timedelta(days=3))
            self.vocab.add("a piece of cake", "小菜一碟；轻而易举的事", "phr.",
                           "en", "腾讯翻译 + 有道词典",
                           created_at=now - timedelta(days=6))
        self.wordbook.show()

    def run(self) -> None:
        self.root.mainloop()


# ---------------------------------------------------------------------------
# 启动
# ---------------------------------------------------------------------------
def setup_logging(data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    log_path = data_dir / "运行日志.log"
    handler = RotatingFileHandler(
        log_path, maxBytes=500_000, backupCount=1, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    ))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    root.addHandler(handler)
    logging.getLogger(__name__).info(
        "启动 %s v%s · Python %s · DPI %s", APP_TITLE, VERSION,
        sys.version.split()[0], winutil.get_system_dpi(),
    )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=APP_TITLE)
    p.add_argument("--data-dir", default="", help="数据目录（测试用）")
    p.add_argument("--demo", action="store_true", help="显示翻译弹窗演示")
    p.add_argument("--demo-wordbook", action="store_true", help="显示生词本演示")
    p.add_argument("--no-hook", action="store_true", help="不安装全局钩子（测试用）")
    p.add_argument("--exit-after", type=float, default=0, help="N 秒后自动退出")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    data_dir = Path(args.data_dir).resolve() if args.data_dir else APP_DIR
    winutil.set_dpi_awareness()
    if not args.demo and not args.demo_wordbook and not args.data_dir:
        if not winutil.acquire_single_instance():
            winutil.message_box(
                "划词翻译已经在运行。\n\n请查看屏幕右侧的「译」悬浮球；"
                "如果看不到，按 Alt+W 打开生词本。",
                APP_TITLE, 0x40,
            )
            return
    setup_logging(data_dir)
    try:
        app = App(args)
        if args.exit_after > 0:
            app.root.after(int(args.exit_after * 1000), app.quit)
        app.run()
    except Exception:
        logging.getLogger(__name__).exception("程序启动失败")
        msg = traceback.format_exc(limit=6)
        ctypes.windll.user32.MessageBoxW(
            None,
            f"划词翻译启动失败。\n\n{msg}\n\n日志：{data_dir / '运行日志.log'}",
            APP_TITLE,
            0x10,
        )


if __name__ == "__main__":
    main()













