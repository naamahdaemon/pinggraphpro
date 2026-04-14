#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import platform
import subprocess
import threading
import time
import re
import queue
from collections import deque
from contextlib import contextmanager
import tkinter as tk
from tkinter import ttk, messagebox

import matplotlib
matplotlib.use("TkAgg")
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure


# ---------------------------
# Ping runner (thread)
# ---------------------------
class PingWorker(threading.Thread):
    def __init__(self, host: str, interval_s: float, timeout_ms: int, out_q: queue.Queue, stop_event: threading.Event):
        super().__init__(daemon=True)
        self.host = host
        self.interval_s = max(0.1, float(interval_s))
        self.timeout_ms = int(timeout_ms)
        self.out_q = out_q
        self.stop_event = stop_event
        self.is_windows = platform.system().lower().startswith("win")

        self.re_time_win = re.compile(r"(?:temps|time)[=<]?\s*([0-9]+)\s*ms", re.IGNORECASE)
        self.re_time_unix = re.compile(r"time[=<]?\s*([0-9]+(?:\.[0-9]+)?)\s*ms", re.IGNORECASE)

    def run(self):
        while not self.stop_event.is_set():
            start = time.time()
            ms = self.ping_once()
            self.out_q.put((time.time(), ms))

            elapsed = time.time() - start
            sleep_for = self.interval_s - elapsed
            if sleep_for > 0:
                self.stop_event.wait(sleep_for)

    def ping_once(self):
        try:
            if self.is_windows:
                cmd = ["ping", "-n", "1", "-w", str(self.timeout_ms), self.host]
            else:
                timeout_s = max(1, int((self.timeout_ms + 999) / 1000))
                cmd = ["ping", "-c", "1", "-W", str(timeout_s), self.host]

            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=max(2, self.timeout_ms / 1000 + 2),
            )
            out = (proc.stdout or "") + "\n" + (proc.stderr or "")

            if self.is_windows:
                m = self.re_time_win.search(out)
                if m:
                    return float(m.group(1))
                if "délai d’attente" in out.lower() or "timed out" in out.lower() or proc.returncode != 0:
                    return None
                return None
            else:
                m = self.re_time_unix.search(out)
                if m:
                    return float(m.group(1))
                return None

        except (subprocess.TimeoutExpired, FileNotFoundError):
            return None
        except Exception:
            return None


# ---------------------------
# GUI app
# ---------------------------
class PingGraphApp:
    LIGHT = {
        "bg": "#f5f7fb",
        "panel": "#ffffff",
        "panel2": "#eef2f7",
        "fg": "#1f2937",
        "muted": "#6b7280",
        "accent": "#2563eb",
        "accent_2": "#60a5fa",
        "ok": "#16a34a",
        "warn": "#dc2626",
        "grid": "#dbe2ea",
        "plot_bg": "#ffffff",
        "line": "#2563eb",
        "spine": "#cbd5e1",
        "titlebar": "#e9edf5",
        "titlebar_btn_hover": "#d7deea",
        "danger_hover": "#ef4444",
    }

    DARK = {
        "bg": "#111827",
        "panel": "#1f2937",
        "panel2": "#0f172a",
        "fg": "#f9fafb",
        "muted": "#9ca3af",
        "accent": "#60a5fa",
        "accent_2": "#93c5fd",
        "ok": "#22c55e",
        "warn": "#f87171",
        "grid": "#374151",
        "plot_bg": "#111827",
        "line": "#60a5fa",
        "spine": "#4b5563",
        "titlebar": "#0b1220",
        "titlebar_btn_hover": "#1f2937",
        "danger_hover": "#b91c1c",
    }

    RESIZE_BORDER = 6
    MIN_WIDTH = 920
    MIN_HEIGHT = 560

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Ping Graph Pro")
        self.root.geometry("1080x680")
        self.root.minsize(self.MIN_WIDTH, self.MIN_HEIGHT)

        self.is_windows = platform.system().lower().startswith("win")

        self.data_ts = deque(maxlen=6000)
        self.data_ms = deque(maxlen=6000)
        self.out_q = queue.Queue()
        self.stop_event = threading.Event()
        self.worker = None

        self.theme_var = tk.StringVar(value="dark")
        self.host_var = tk.StringVar(value="1.1.1.1")
        self.interval_var = tk.DoubleVar(value=1.0)
        self.timeout_var = tk.IntVar(value=1000)
        self.window_var = tk.IntVar(value=120)

        self.status_var = tk.StringVar(value="Idle.")
        self.stats_var = tk.StringVar(value="")
        self.last_ping_var = tk.StringVar(value="—")
        self.loss_var = tk.StringVar(value="0%")
        self.avg_var = tk.StringVar(value="—")
        self.state_badge_var = tk.StringVar(value="STOPPED")

        self.style = ttk.Style()

        # move state
        self._drag_start_x = 0
        self._drag_start_y = 0
        self._drag_win_x = 0
        self._drag_win_y = 0

        # resize state
        self._resize_mode = None
        self._resize_start_pointer_x = 0
        self._resize_start_pointer_y = 0
        self._resize_start_x = 0
        self._resize_start_y = 0
        self._resize_start_w = 0
        self._resize_start_h = 0

        # maximize state
        self.is_maximized = False
        self.restore_geometry = None

        self._set_borderless(True)
        self._build_ui()
        self._build_plot()
        self.apply_theme(self.theme_var.get())
        self._bind_resize_handlers()
        self._bind_titlebar_handlers()
        self._ui_tick()

    # ---------------------------
    # Native/borderless mode
    # ---------------------------
    def _set_borderless(self, enabled: bool):
        try:
            self.root.overrideredirect(enabled)
        except Exception:
            pass

    def _sync_window_mode(self):
        # Maximized/minimized => native frame back on, to keep better Windows behavior
        try:
            state = self.root.state()
        except Exception:
            state = "normal"

        if state in ("zoomed", "iconic"):
            self._set_borderless(False)
        else:
            if not self.is_maximized:
                self._set_borderless(True)

    # ---------------------------
    # Title bar / drag
    # ---------------------------
    def _bind_titlebar_handlers(self):
        self._bind_drag(self.title_left)
        self._bind_drag(self.title_label)
        self._bind_drag(self.title_icon)

        self.titlebar.bind("<Double-Button-1>", self._on_titlebar_double_click)
        self.title_left.bind("<Double-Button-1>", self._on_titlebar_double_click)
        self.title_label.bind("<Double-Button-1>", self._on_titlebar_double_click)
        self.title_icon.bind("<Double-Button-1>", self._on_titlebar_double_click)

        self.root.bind("<Map>", lambda e: self.root.after(10, self._sync_window_mode))
        self.root.bind("<Configure>", self._on_root_configure, add="+")

    def _on_root_configure(self, event):
        self.root.after_idle(self._sync_window_mode)

    def _start_move(self, event):
        if self._resize_mode is not None:
            return

        # if maximized, restore first and place window under cursor proportionally
        if self.is_maximized:
            try:
                current_w = self.root.winfo_width()
                rel_x = event.x_root / max(1, current_w)
            except Exception:
                rel_x = 0.5

            old_geo = self.restore_geometry or f"{self.MIN_WIDTH}x{self.MIN_HEIGHT}+50+50"
            self.toggle_maximize()

            self.root.update_idletasks()
            restored_w = self.root.winfo_width()
            restored_h = self.root.winfo_height()

            new_x = int(event.x_root - restored_w * rel_x)
            new_y = int(event.y_root - 18)
            self.root.geometry(f"{restored_w}x{restored_h}+{new_x}+{new_y}")

        self._drag_start_x = event.x_root
        self._drag_start_y = event.y_root
        self._drag_win_x = self.root.winfo_x()
        self._drag_win_y = self.root.winfo_y()

    def _do_move(self, event):
        if self._resize_mode is not None or self.is_maximized:
            return
        dx = event.x_root - self._drag_start_x
        dy = event.y_root - self._drag_start_y
        x = self._drag_win_x + dx
        y = self._drag_win_y + dy
        self.root.geometry(f"+{x}+{y}")

    def _bind_drag(self, widget):
        widget.bind("<Button-1>", self._start_move)
        widget.bind("<B1-Motion>", self._do_move)

    def _on_titlebar_double_click(self, event):
        self.toggle_maximize()

    def _minimize_window(self):
        self._set_borderless(False)
        self.root.iconify()
        self.root.after(250, self._sync_window_mode)

    def toggle_maximize(self):
        if self.is_maximized:
            self.restore_window()
        else:
            self.maximize_window()

    def maximize_window(self):
        if self.is_maximized:
            return

        self.restore_geometry = self.root.geometry()
        self.is_maximized = True

        self._set_borderless(False)
        try:
            self.root.state("zoomed")
        except Exception:
            # Fallback non-Windows
            sw = self.root.winfo_screenwidth()
            sh = self.root.winfo_screenheight()
            self.root.geometry(f"{sw}x{sh}+0+0")

        self.max_btn.configure(text="❐")
        self.state_badge_var.set("MAXIMIZED")

    def restore_window(self):
        if not self.is_maximized:
            return

        self.is_maximized = False
        try:
            self.root.state("normal")
        except Exception:
            pass

        if self.restore_geometry:
            self.root.geometry(self.restore_geometry)

        self._set_borderless(True)
        self.max_btn.configure(text="□")
        self.state_badge_var.set("STOPPED" if self.worker is None else "RUNNING")

    def _set_titlebar_button_bg(self, widget, bg):
        try:
            widget.configure(bg=bg)
        except Exception:
            pass

    # ---------------------------
    # Resize handling
    # ---------------------------
    def _bind_resize_handlers(self):
        for widget in (self.root, self.root_container, self.outer, self.titlebar):
            widget.bind("<Motion>", self._on_global_motion, add="+")
            widget.bind("<Leave>", self._on_global_leave, add="+")
            widget.bind("<Button-1>", self._on_resize_press, add="+")
            widget.bind("<B1-Motion>", self._on_resize_drag, add="+")
            widget.bind("<ButtonRelease-1>", self._on_resize_release, add="+")

        self.titlebar.bind("<Button-1>", self._titlebar_press_wrapper, add="+")
        self.titlebar.bind("<B1-Motion>", self._titlebar_drag_wrapper, add="+")
        self.titlebar.bind("<ButtonRelease-1>", self._on_resize_release, add="+")

    def _titlebar_press_wrapper(self, event):
        mode = self._detect_resize_mode(event.x_root, event.y_root)
        if mode and not self.is_maximized:
            self._on_resize_press(event)
        else:
            self._start_move(event)

    def _titlebar_drag_wrapper(self, event):
        if self._resize_mode:
            self._on_resize_drag(event)
        else:
            self._do_move(event)

    def _detect_resize_mode(self, x_root, y_root):
        if self.is_maximized:
            return None

        x = self.root.winfo_x()
        y = self.root.winfo_y()
        w = self.root.winfo_width()
        h = self.root.winfo_height()
        b = self.RESIZE_BORDER

        left = x_root <= x + b
        right = x_root >= x + w - b
        top = y_root <= y + b
        bottom = y_root >= y + h - b

        if top and left:
            return "nw"
        if top and right:
            return "ne"
        if bottom and left:
            return "sw"
        if bottom and right:
            return "se"
        if left:
            return "w"
        if right:
            return "e"
        if top:
            return "n"
        if bottom:
            return "s"
        return None

    def _cursor_for_mode(self, mode):
        if self.is_windows:
            mapping = {
                "n": "sb_v_double_arrow",
                "s": "sb_v_double_arrow",
                "e": "sb_h_double_arrow",
                "w": "sb_h_double_arrow",
                "ne": "size_ne_sw",
                "sw": "size_ne_sw",
                "nw": "size_nw_se",
                "se": "size_nw_se",
            }
        else:
            mapping = {
                "n": "sb_v_double_arrow",
                "s": "sb_v_double_arrow",
                "e": "sb_h_double_arrow",
                "w": "sb_h_double_arrow",
                "ne": "top_right_corner",
                "sw": "bottom_left_corner",
                "nw": "top_left_corner",
                "se": "bottom_right_corner",
            }
        return mapping.get(mode, "")

    def _apply_cursor_everywhere(self, cursor):
        widgets = [self.root, self.root_container, self.titlebar]
        for widget in widgets:
            try:
                widget.configure(cursor=cursor)
            except Exception:
                pass

    def _on_global_motion(self, event):
        if self._resize_mode is not None or self.is_maximized:
            return
        mode = self._detect_resize_mode(event.x_root, event.y_root)
        self._apply_cursor_everywhere(self._cursor_for_mode(mode) if mode else "")

    def _on_global_leave(self, event):
        if self._resize_mode is None:
            self._apply_cursor_everywhere("")

    def _on_resize_press(self, event):
        if self.is_maximized:
            return

        mode = self._detect_resize_mode(event.x_root, event.y_root)
        if not mode:
            return

        self._resize_mode = mode
        self._resize_start_pointer_x = event.x_root
        self._resize_start_pointer_y = event.y_root
        self._resize_start_x = self.root.winfo_x()
        self._resize_start_y = self.root.winfo_y()
        self._resize_start_w = self.root.winfo_width()
        self._resize_start_h = self.root.winfo_height()
        self._apply_cursor_everywhere(self._cursor_for_mode(mode))

    def _on_resize_drag(self, event):
        if not self._resize_mode or self.is_maximized:
            return

        dx = event.x_root - self._resize_start_pointer_x
        dy = event.y_root - self._resize_start_pointer_y

        x = self._resize_start_x
        y = self._resize_start_y
        w = self._resize_start_w
        h = self._resize_start_h

        mode = self._resize_mode

        if "e" in mode:
            w = max(self.MIN_WIDTH, self._resize_start_w + dx)

        if "s" in mode:
            h = max(self.MIN_HEIGHT, self._resize_start_h + dy)

        if "w" in mode:
            new_w = max(self.MIN_WIDTH, self._resize_start_w - dx)
            x = self._resize_start_x + (self._resize_start_w - new_w)
            w = new_w

        if "n" in mode:
            new_h = max(self.MIN_HEIGHT, self._resize_start_h - dy)
            y = self._resize_start_y + (self._resize_start_h - new_h)
            h = new_h

        self.root.geometry(f"{int(w)}x{int(h)}+{int(x)}+{int(y)}")

    def _on_resize_release(self, event):
        self._resize_mode = None
        mode = self._detect_resize_mode(event.x_root, event.y_root)
        self._apply_cursor_everywhere(self._cursor_for_mode(mode) if mode else "")

    # ---------------------------
    # UI
    # ---------------------------
    def _build_ui(self):
        self.root_container = tk.Frame(self.root, bd=0, highlightthickness=1)
        self.root_container.pack(fill="both", expand=True)

        # Title bar custom
        self.titlebar = tk.Frame(self.root_container, height=36, bd=0, highlightthickness=0)
        self.titlebar.pack(fill="x", side="top")
        self.titlebar.pack_propagate(False)

        self.title_left = tk.Frame(self.titlebar, bd=0, highlightthickness=0)
        self.title_left.pack(side="left", fill="both", expand=True)

        self.title_icon = tk.Label(self.title_left, text="●", font=("Segoe UI", 10, "bold"), cursor="fleur")
        self.title_icon.pack(side="left", padx=(10, 6))

        self.title_label = tk.Label(
            self.title_left,
            text="Ping Graph Pro",
            font=("Segoe UI", 10, "bold"),
            anchor="w",
            cursor="fleur",
        )
        self.title_label.pack(side="left")

        self.title_right = tk.Frame(self.titlebar, bd=0, highlightthickness=0)
        self.title_right.pack(side="right", fill="y")

        self.min_btn = tk.Label(self.title_right, text="—", width=4, font=("Segoe UI", 11), cursor="hand2")
        self.min_btn.pack(side="left", fill="y")

        self.max_btn = tk.Label(self.title_right, text="□", width=4, font=("Segoe UI", 10), cursor="hand2")
        self.max_btn.pack(side="left", fill="y")

        self.close_btn = tk.Label(self.title_right, text="✕", width=4, font=("Segoe UI", 10), cursor="hand2")
        self.close_btn.pack(side="left", fill="y")

        self.min_btn.bind("<Button-1>", lambda e: self._minimize_window())
        self.max_btn.bind("<Button-1>", lambda e: self.toggle_maximize())
        self.close_btn.bind("<Button-1>", lambda e: self.on_close())

        # Main content
        self.outer = ttk.Frame(self.root_container, style="App.TFrame", padding=14)
        self.outer.pack(fill=tk.BOTH, expand=True)

        self.header = ttk.Frame(self.outer, style="Card.TFrame", padding=(16, 14))
        self.header.pack(fill=tk.X, pady=(0, 12))

        title_wrap = ttk.Frame(self.header, style="Card.TFrame")
        title_wrap.pack(side=tk.LEFT, fill=tk.X, expand=True)

        ttk.Label(title_wrap, text="Ping Graph Pro", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            title_wrap,
            text="Monitoring simple et propre de la latence réseau",
            style="Subtitle.TLabel",
        ).pack(anchor="w", pady=(2, 0))

        theme_wrap = ttk.Frame(self.header, style="Card.TFrame")
        theme_wrap.pack(side=tk.RIGHT)

        ttk.Label(theme_wrap, text="Theme", style="Muted.TLabel").pack(anchor="e")
        self.theme_combo = ttk.Combobox(
            theme_wrap,
            textvariable=self.theme_var,
            values=["light", "dark"],
            state="readonly",
            width=10,
        )
        self.theme_combo.pack(anchor="e", pady=(4, 0))
        self.theme_combo.bind("<<ComboboxSelected>>", lambda e: self.apply_theme(self.theme_var.get()))

        self.toolbar = ttk.Frame(self.outer, style="Card.TFrame", padding=16)
        self.toolbar.pack(fill=tk.X, pady=(0, 12))

        for i in range(9):
            self.toolbar.grid_columnconfigure(i, weight=1)

        ttk.Label(self.toolbar, text="Host / IP", style="FieldLabel.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Entry(self.toolbar, textvariable=self.host_var, width=20).grid(row=1, column=0, sticky="ew", padx=(0, 12))

        ttk.Label(self.toolbar, text="Interval (s)", style="FieldLabel.TLabel").grid(row=0, column=1, sticky="w", padx=(0, 8))
        ttk.Spinbox(self.toolbar, textvariable=self.interval_var, from_=0.2, to=10.0, increment=0.1, width=8).grid(
            row=1, column=1, sticky="ew", padx=(0, 12)
        )

        ttk.Label(self.toolbar, text="Timeout (ms)", style="FieldLabel.TLabel").grid(row=0, column=2, sticky="w", padx=(0, 8))
        ttk.Spinbox(self.toolbar, textvariable=self.timeout_var, from_=200, to=5000, increment=100, width=8).grid(
            row=1, column=2, sticky="ew", padx=(0, 12)
        )

        ttk.Label(self.toolbar, text="Window (points)", style="FieldLabel.TLabel").grid(row=0, column=3, sticky="w", padx=(0, 8))
        ttk.Spinbox(self.toolbar, textvariable=self.window_var, from_=30, to=6000, increment=10, width=10).grid(
            row=1, column=3, sticky="ew", padx=(0, 12)
        )

        self.start_btn = ttk.Button(self.toolbar, text="▶ Start", command=self.start, style="Accent.TButton")
        self.start_btn.grid(row=1, column=4, sticky="ew", padx=(6, 8))

        self.stop_btn = ttk.Button(self.toolbar, text="■ Stop", command=self.stop, state=tk.DISABLED, style="Danger.TButton")
        self.stop_btn.grid(row=1, column=5, sticky="ew", padx=(0, 8))

        self.clear_btn = ttk.Button(self.toolbar, text="⟲ Clear", command=self.clear_data, style="Soft.TButton")
        self.clear_btn.grid(row=1, column=6, sticky="ew", padx=(0, 8))

        badge_wrap = ttk.Frame(self.toolbar, style="Card.TFrame")
        badge_wrap.grid(row=0, column=7, rowspan=2, sticky="e", padx=(12, 0))
        ttk.Label(badge_wrap, text="Status", style="Muted.TLabel").pack(anchor="e")
        ttk.Label(badge_wrap, textvariable=self.state_badge_var, style="Badge.TLabel").pack(anchor="e", pady=(4, 0))

        self.body = ttk.Frame(self.outer, style="App.TFrame")
        self.body.pack(fill=tk.BOTH, expand=True)

        self.left_panel = ttk.Frame(self.body, style="Card.TFrame", padding=4)
        self.left_panel.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(0, 12))

        self.right_panel = ttk.Frame(self.body, style="Card.TFrame", padding=14, width=230)
        self.right_panel.pack(side=tk.RIGHT, fill=tk.Y)
        self.right_panel.pack_propagate(False)

        ttk.Label(self.right_panel, text="Live Stats", style="PanelTitle.TLabel").pack(anchor="w", pady=(0, 10))
        self._stat_card(self.right_panel, "Last ping", self.last_ping_var)
        self._stat_card(self.right_panel, "Average", self.avg_var)
        self._stat_card(self.right_panel, "Packet loss", self.loss_var)

        ttk.Separator(self.right_panel).pack(fill=tk.X, pady=12)

        ttk.Label(self.right_panel, text="Session", style="PanelTitle.TLabel").pack(anchor="w", pady=(0, 8))
        ttk.Label(self.right_panel, textvariable=self.status_var, style="Body.TLabel", wraplength=180, justify="left").pack(anchor="w")

        ttk.Separator(self.right_panel).pack(fill=tk.X, pady=12)
        ttk.Label(self.right_panel, text="Summary", style="PanelTitle.TLabel").pack(anchor="w", pady=(0, 8))
        ttk.Label(self.right_panel, textvariable=self.stats_var, style="Body.TLabel", wraplength=180, justify="left").pack(anchor="w")

    def _stat_card(self, parent, title, var):
        box = ttk.Frame(parent, style="InnerCard.TFrame", padding=(12, 10))
        box.pack(fill=tk.X, pady=(0, 10))
        ttk.Label(box, text=title, style="MutedPanel.TLabel").pack(anchor="w")
        ttk.Label(box, textvariable=var, style="StatValue.TLabel").pack(anchor="w", pady=(4, 0))

    def _build_plot(self):
        self.fig = Figure(figsize=(8, 4), dpi=100)
        self.fig.subplots_adjust(left=0.07, right=0.985, top=0.93, bottom=0.11)

        self.ax = self.fig.add_subplot(111)

        self.ax.set_title("Latency (ms)", pad=8)
        self.ax.set_xlabel("Time (s)")
        self.ax.set_ylabel("Latency (ms)")
        self.ax.grid(True, linestyle="--", linewidth=0.7, alpha=0.8)

        (self.line,) = self.ax.plot([], [], linewidth=2.0)
        self.canvas = FigureCanvasTkAgg(self.fig, master=self.left_panel)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True, padx=0, pady=0)

    # ---------------------------
    # Theme
    # ---------------------------
    def apply_theme(self, theme_name: str):
        palette = self.DARK if theme_name == "dark" else self.LIGHT
        self.colors = palette

        try:
            self.style.theme_use("clam")
        except Exception:
            pass

        self.root.configure(bg=palette["bg"])
        self.root_container.configure(bg=palette["bg"], highlightbackground=palette["spine"], highlightcolor=palette["spine"])

        self.titlebar.configure(bg=palette["titlebar"])
        self.title_left.configure(bg=palette["titlebar"])
        self.title_right.configure(bg=palette["titlebar"])
        self.title_icon.configure(bg=palette["titlebar"], fg=palette["accent"])
        self.title_label.configure(bg=palette["titlebar"], fg=palette["fg"])
        self.min_btn.configure(bg=palette["titlebar"], fg=palette["fg"])
        self.max_btn.configure(bg=palette["titlebar"], fg=palette["fg"])
        self.close_btn.configure(bg=palette["titlebar"], fg=palette["fg"])

        self.min_btn.bind("<Enter>", lambda e: self._set_titlebar_button_bg(self.min_btn, palette["titlebar_btn_hover"]))
        self.min_btn.bind("<Leave>", lambda e: self._set_titlebar_button_bg(self.min_btn, palette["titlebar"]))
        self.max_btn.bind("<Enter>", lambda e: self._set_titlebar_button_bg(self.max_btn, palette["titlebar_btn_hover"]))
        self.max_btn.bind("<Leave>", lambda e: self._set_titlebar_button_bg(self.max_btn, palette["titlebar"]))
        self.close_btn.bind("<Enter>", lambda e: self._set_titlebar_button_bg(self.close_btn, palette["danger_hover"]))
        self.close_btn.bind("<Leave>", lambda e: self._set_titlebar_button_bg(self.close_btn, palette["titlebar"]))

        self.style.configure("App.TFrame", background=palette["bg"])
        self.style.configure("Card.TFrame", background=palette["panel"])
        self.style.configure("InnerCard.TFrame", background=palette["panel2"])

        self.style.configure("Title.TLabel", background=palette["panel"], foreground=palette["fg"], font=("Segoe UI", 18, "bold"))
        self.style.configure("Subtitle.TLabel", background=palette["panel"], foreground=palette["muted"], font=("Segoe UI", 10))
        self.style.configure("PanelTitle.TLabel", background=palette["panel"], foreground=palette["fg"], font=("Segoe UI", 11, "bold"))
        self.style.configure("Body.TLabel", background=palette["panel"], foreground=palette["fg"], font=("Segoe UI", 10))
        self.style.configure("Muted.TLabel", background=palette["panel"], foreground=palette["muted"], font=("Segoe UI", 9))
        self.style.configure("MutedPanel.TLabel", background=palette["panel2"], foreground=palette["muted"], font=("Segoe UI", 9))
        self.style.configure("FieldLabel.TLabel", background=palette["panel"], foreground=palette["fg"], font=("Segoe UI", 9, "bold"))
        self.style.configure("StatValue.TLabel", background=palette["panel2"], foreground=palette["fg"], font=("Segoe UI", 16, "bold"))
        self.style.configure("Badge.TLabel", background=palette["accent"], foreground="#ffffff", font=("Segoe UI", 9, "bold"), padding=(10, 4))

        self.style.configure("TLabel", background=palette["panel"], foreground=palette["fg"], font=("Segoe UI", 10))
        self.style.configure(
            "TEntry",
            fieldbackground=palette["panel2"],
            foreground=palette["fg"],
            bordercolor=palette["spine"],
            lightcolor=palette["spine"],
            darkcolor=palette["spine"],
            insertcolor=palette["fg"],
            padding=6,
        )
        self.style.configure(
            "TCombobox",
            fieldbackground=palette["panel2"],
            background=palette["panel"],
            foreground=palette["fg"],
            arrowcolor=palette["fg"],
            bordercolor=palette["spine"],
            lightcolor=palette["spine"],
            darkcolor=palette["spine"],
            padding=5,
        )
        self.style.map(
            "TCombobox",
            fieldbackground=[("readonly", palette["panel2"])],
            foreground=[("readonly", palette["fg"])],
            selectbackground=[("readonly", palette["panel2"])],
            selectforeground=[("readonly", palette["fg"])],
        )
        self.style.configure(
            "TSpinbox",
            fieldbackground=palette["panel2"],
            foreground=palette["fg"],
            arrowsize=12,
            bordercolor=palette["spine"],
            lightcolor=palette["spine"],
            darkcolor=palette["spine"],
            padding=5,
        )

        self.style.configure(
            "Accent.TButton",
            background=palette["accent"],
            foreground="#ffffff",
            borderwidth=0,
            focusthickness=0,
            padding=(12, 8),
            font=("Segoe UI", 10, "bold"),
        )
        self.style.map(
            "Accent.TButton",
            background=[("active", palette["accent_2"]), ("disabled", palette["grid"])],
            foreground=[("disabled", "#ffffff")],
        )

        self.style.configure(
            "Danger.TButton",
            background=palette["warn"],
            foreground="#ffffff",
            borderwidth=0,
            focusthickness=0,
            padding=(12, 8),
            font=("Segoe UI", 10, "bold"),
        )
        self.style.map(
            "Danger.TButton",
            background=[("active", palette["danger_hover"]), ("disabled", palette["grid"])],
            foreground=[("disabled", "#ffffff")],
        )

        self.style.configure(
            "Soft.TButton",
            background=palette["panel2"],
            foreground=palette["fg"],
            borderwidth=1,
            padding=(12, 8),
            font=("Segoe UI", 10),
        )
        self.style.map("Soft.TButton", background=[("active", palette["panel"])])

        self._apply_plot_theme()
        self.canvas.draw_idle()

    def _apply_plot_theme(self):
        c = self.colors
        self.fig.patch.set_facecolor(c["panel"])
        self.ax.set_facecolor(c["plot_bg"])

        self.ax.title.set_color(c["fg"])
        self.ax.xaxis.label.set_color(c["fg"])
        self.ax.yaxis.label.set_color(c["fg"])
        self.ax.tick_params(axis="x", colors=c["muted"])
        self.ax.tick_params(axis="y", colors=c["muted"])

        for spine in self.ax.spines.values():
            spine.set_color(c["spine"])

        self.ax.grid(True, linestyle="--", linewidth=0.7, color=c["grid"], alpha=0.85)
        self.line.set_color(c["line"])

    # ---------------------------
    # Actions
    # ---------------------------
    def start(self):
        host = self.host_var.get().strip()
        if not host:
            messagebox.showerror("Error", "Please enter a host or IP.")
            return

        self.data_ts.clear()
        self.data_ms.clear()
        with self._drain_queue():
            pass

        self.stop_event.clear()
        self.worker = PingWorker(
            host=host,
            interval_s=float(self.interval_var.get()),
            timeout_ms=int(self.timeout_var.get()),
            out_q=self.out_q,
            stop_event=self.stop_event,
        )
        self.worker.start()

        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)
        self.state_badge_var.set("RUNNING")

        if self.is_maximized:
            self.state_badge_var.set("MAXIMIZED")

        self.status_var.set(f"Pinging {host} ...")

    def stop(self):
        self.stop_event.set()
        self.worker = None
        self.start_btn.config(state=tk.NORMAL)
        self.stop_btn.config(state=tk.DISABLED)
        self.state_badge_var.set("MAXIMIZED" if self.is_maximized else "STOPPED")
        self.status_var.set("Stopped.")

    def clear_data(self):
        self.data_ts.clear()
        self.data_ms.clear()
        with self._drain_queue():
            pass

        self.line.set_data([], [])
        self.ax.relim()
        self.ax.autoscale_view()

        self.stats_var.set("")
        self.last_ping_var.set("—")
        self.loss_var.set("0%")
        self.avg_var.set("—")
        self.canvas.draw_idle()

    def on_close(self):
        self.stop_event.set()
        self.root.destroy()

    # ---------------------------
    # UI refresh
    # ---------------------------
    def _ui_tick(self):
        updated = False
        while True:
            try:
                ts, ms = self.out_q.get_nowait()
            except queue.Empty:
                break
            self.data_ts.append(ts)
            self.data_ms.append(ms)
            updated = True

        if updated:
            self._update_plot_and_stats()

        self.root.after(200, self._ui_tick)

    def _update_plot_and_stats(self):
        n = int(self.window_var.get())
        xs = list(self.data_ts)[-n:]
        ys_raw = list(self.data_ms)[-n:]

        if not xs:
            return

        t0 = xs[0]
        x_rel = [x - t0 for x in xs]
        ys = [float("nan") if v is None else float(v) for v in ys_raw]

        self.line.set_data(x_rel, ys)
        self.ax.relim()
        self.ax.autoscale_view()

        vals = [v for v in ys_raw if v is not None]
        loss = 0
        if ys_raw:
            loss = int(round(100 * (sum(1 for v in ys_raw if v is None) / len(ys_raw))))

        if ys_raw and ys_raw[-1] is None:
            self.last_ping_var.set("timeout")
        elif ys_raw:
            self.last_ping_var.set(f"{ys_raw[-1]:.1f} ms")

        self.loss_var.set(f"{loss}%")

        if vals:
            vmin = min(vals)
            vmax = max(vals)
            vavg = sum(vals) / len(vals)
            self.avg_var.set(f"{vavg:.1f} ms")
            self.stats_var.set(f"min {vmin:.1f} ms | avg {vavg:.1f} ms | max {vmax:.1f} ms | loss ~{loss}%")
        else:
            self.avg_var.set("—")
            self.stats_var.set(f"no replies | loss ~{loss}%")

        self.canvas.draw_idle()

    @contextmanager
    def _drain_queue(self):
        try:
            while True:
                self.out_q.get_nowait()
        except queue.Empty:
            yield


def main():
    root = tk.Tk()
    app = PingGraphApp(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()


if __name__ == "__main__":
    main()