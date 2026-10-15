"""Portable native desktop monitor for the HP OMEN USB power supply."""

from __future__ import annotations

import argparse
from collections import deque
import csv
import ctypes
from datetime import datetime
import json
import logging
from logging.handlers import RotatingFileHandler
import math
import os
from pathlib import Path
import sys
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from psu_service import SensorStats, TelemetryCollector, flatten_snapshot, worker_main
from hwinfo_bridge import HWiNFOBridge
from app_version import VERSION


BG, PANEL, SURFACE = "#10151e", "#181f2b", "#202a39"
TEXT, MUTED, LINE = "#edf2f7", "#99a8bb", "#2d394c"
ACCENT, BLUE, AMBER, RED = "#55dec5", "#79afff", "#edc47a", "#ff8d91"
BASE_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
def resolve_data_dir(*, portable=False, base_dir=BASE_DIR, local_app_data=None):
    if portable:
        return Path(base_dir) / "data"
    local_app_data = local_app_data or os.environ.get("LOCALAPPDATA")
    return (Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local") / "OmenPsuMonitor"


DATA_DIR = resolve_data_dir()
ASSET_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "assets"
CSV_FIELDS = ["timestamp_utc", "power", "temperature", "efficiency", "fan"] + [
    f"{kind}.rail_{rail}" for rail in range(1, 8) for kind in ("voltage", "current")]


def format_value(value, unit="", suffix=True):
    if value is None or not math.isfinite(value):
        return "—"
    decimals = 2 if unit in ("V", "A") else 1 if unit in ("°C", "%") else 0
    result = f"{value:,.{decimals}f}"
    return f"{result} {unit}" if suffix and unit else result


class OmenPsuApp(tk.Tk):
    def __init__(self, *, enable_hwinfo=False, auto_close=None):
        super().__init__()
        self.title("OMEN PSU Monitor · 电源监测")
        self.geometry("1260x840")
        self.minsize(1080, 730)
        self.configure(bg=BG)
        if (ASSET_DIR / "omen_psu.ico").exists():
            self.iconbitmap(str(ASSET_DIR / "omen_psu.ico"))
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.config_path = DATA_DIR / "settings.json"
        self.config_data = self._load_settings()
        self.interval = tk.StringVar(value=str(self.config_data.get("interval", 2)))
        self.hwinfo_enabled = tk.BooleanVar(value=enable_hwinfo or bool(self.config_data.get("hwinfo", False)))
        self.topmost_enabled = tk.BooleanVar(value=False)
        self.chart_window = tk.StringVar(value="近 5 分钟")
        self.stats, self.readings, self.cards = {}, {}, {}
        self.records = deque(maxlen=50000)
        self.selected_sensor = "power"
        self.last_received = None
        self.sample_count = 0
        self.connected = False
        self.paused = False
        self.closing = False
        self.csv_file = self.csv_writer = None
        self.csv_path = None
        self.bridge = HWiNFOBridge()
        self.published_count = 0
        self.bridge_message = ""
        self.collector = TelemetryCollector(interval=float(self.interval.get()))
        self._style()
        self._build()
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Control-s>", lambda _: self.export_csv())
        self.bind("<F5>", lambda _: self.reset_statistics())
        self.bind("<space>", self._space_pause)
        self.after(150, lambda: self.collector.start() if not self.paused and not self.closing else None)
        self.after(150, self._poll_events)
        self.after(1000, self._age_tick)
        if auto_close:
            self.after(int(auto_close * 1000), self.close)

    def _load_settings(self):
        try:
            candidate = self.config_path
            legacy = BASE_DIR / "data" / "settings.json"
            if not candidate.exists() and candidate != legacy and legacy.is_file():
                candidate = legacy
            config = json.loads(candidate.read_text(encoding="utf-8"))
            interval = float(config.get("interval", 2))
            return {"interval": interval if interval in (1, 2, 3, 5, 10) else 2,
                    "hwinfo": config.get("hwinfo") is True}
        except (OSError, ValueError, TypeError, AttributeError):
            return {"interval": 2, "hwinfo": False}

    def _style(self):
        self.option_add("*Font", ("Microsoft YaHei UI", 10))
        self.option_add("*TCombobox*Listbox.background", SURFACE)
        self.option_add("*TCombobox*Listbox.foreground", TEXT)
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TFrame", background=BG)
        style.configure("TLabel", background=BG, foreground=TEXT)
        style.configure("TButton", background=SURFACE, foreground=TEXT, borderwidth=0,
                        padding=(13, 9), focuscolor=ACCENT, font=("Microsoft YaHei UI", 10))
        style.map("TButton", background=[("active", "#304058"), ("disabled", PANEL)],
                  foreground=[("disabled", "#62738a")])
        style.configure("Accent.TButton", background="#21554e", foreground=ACCENT)
        style.map("Accent.TButton", background=[("active", "#2c6c61")])
        style.configure("TCheckbutton", background=BG, foreground=TEXT, padding=4,
                        indicatorbackground=SURFACE, indicatorforeground=ACCENT, font=("Microsoft YaHei UI", 10))
        style.map("TCheckbutton", background=[("active", BG)],
                  foreground=[("active", TEXT)])
        style.configure("TCombobox", fieldbackground=SURFACE, background=SURFACE,
                        foreground=TEXT, arrowcolor=MUTED, bordercolor=LINE, padding=5, font=("Microsoft YaHei UI", 10))
        style.map("TCombobox", fieldbackground=[("readonly", SURFACE)],
                  foreground=[("readonly", TEXT)], selectbackground=[("readonly", SURFACE)])
        style.configure("Treeview", background=PANEL, fieldbackground=PANEL,
                        foreground=TEXT, borderwidth=0, rowheight=30,
                        font=("Microsoft YaHei UI", 10))
        style.configure("Treeview.Heading", background=SURFACE, foreground=MUTED,
                        font=("Microsoft YaHei UI", 10), relief="flat", padding=9)
        style.map("Treeview", background=[("selected", "#284940")],
                  foreground=[("selected", "#a3ffeb")])
        style.configure("Vertical.TScrollbar", background=SURFACE, troughcolor=PANEL,
                        borderwidth=0, arrowcolor=MUTED)

    def label(self, parent, text="", *, fg=TEXT, bg=BG, font=None, **kwargs):
        return tk.Label(parent, text=text, fg=fg, bg=bg,
                        font=font or ("Microsoft YaHei UI", 10), **kwargs)

    def _build(self):
        outer = tk.Frame(self, bg=BG, padx=24, pady=20)
        outer.pack(fill="both", expand=True)
        header = tk.Frame(outer, bg=BG)
        header.pack(fill="x")
        mark = tk.Canvas(header, width=43, height=47, bg=BG, highlightthickness=0)
        mark.pack(side="left", padx=(0, 12))
        mark.create_polygon(26, 3, 10, 25, 22, 25, 15, 44, 35, 19, 24, 19,
                            fill=ACCENT, outline="")
        title = tk.Frame(header, bg=BG)
        title.pack(side="left")
        self.label(title, "OMEN 电源监测", font=("Microsoft YaHei UI", 22, "bold")).pack(anchor="w")
        self.label(title, "HP GAMING PSU  ·  实时传感器", fg=MUTED).pack(anchor="w", pady=(2, 0))
        status_box = tk.Frame(header, bg=BG)
        status_box.pack(side="right")
        self.status_label = self.label(status_box, "●  正在连接", fg=AMBER)
        self.status_label.pack(anchor="e")
        self.updated_label = self.label(status_box, "等待第一组数据", fg=MUTED, font=("Microsoft YaHei UI", 9))
        self.updated_label.pack(anchor="e", pady=(5, 0))

        cards = tk.Frame(outer, bg=BG)
        cards.pack(fill="x", pady=(22, 18))
        for index, (key, title, unit, color) in enumerate([
            ("power", "电源功率", "W", ACCENT), ("temperature", "电源温度", "°C", AMBER),
            ("efficiency", "转换效率", "%", BLUE), ("fan", "风扇转速", "RPM", "#c5adff")]):
            cards.columnconfigure(index, weight=1, uniform="cards")
            card = tk.Frame(cards, bg=PANEL, padx=18, pady=13, highlightbackground=LINE, highlightthickness=1)
            card.grid(row=0, column=index, sticky="ew", padx=(0 if index == 0 else 6, 0 if index == 3 else 6))
            self.label(card, title, fg=MUTED, bg=PANEL).pack(anchor="w")
            value_row = tk.Frame(card, bg=PANEL)
            value_row.pack(fill="x", pady=(8, 5))
            value = self.label(value_row, "—", fg=color, bg=PANEL, font=("Segoe UI", 29, "bold"))
            value.pack(side="left")
            self.label(value_row, unit, fg=MUTED, bg=PANEL).pack(side="left", anchor="s", padx=7, pady=7)
            summary = self.label(card, "最低 —   /   最高 —", fg=MUTED, bg=PANEL, font=("Microsoft YaHei UI", 9))
            summary.pack(anchor="w")
            self.cards[key] = (value, summary, unit)

        toolbar = tk.Frame(outer, bg=BG)
        toolbar.pack(fill="x", pady=(0, 15))
        self.pause_button = ttk.Button(toolbar, text="暂停采集", command=self.toggle_pause)
        self.pause_button.pack(side="left", padx=(0, 8))
        ttk.Button(toolbar, text="重置统计", command=self.reset_statistics).pack(side="left", padx=(0, 8))
        self.record_button = ttk.Button(toolbar, text="记录 CSV", command=self.toggle_recording)
        self.record_button.pack(side="left", padx=(0, 8))
        ttk.Button(toolbar, text="导出历史", command=self.export_csv).pack(side="left")
        self.label(toolbar, "刷新", fg=MUTED).pack(side="left", padx=(20, 6))
        interval_box = ttk.Combobox(toolbar, textvariable=self.interval, values=(1, 2, 3, 5, 10),
                                    state="readonly", width=3)
        interval_box.pack(side="left")
        interval_box.bind("<<ComboboxSelected>>", self.change_interval)
        self.label(toolbar, "秒", fg=MUTED).pack(side="left", padx=6)
        ttk.Checkbutton(toolbar, text="窗口置顶", variable=self.topmost_enabled,
                        command=lambda: self.attributes("-topmost", self.topmost_enabled.get())).pack(side="right")

        body = tk.PanedWindow(outer, orient="horizontal", bg=BG, sashwidth=16,
                              sashrelief="flat", borderwidth=0, opaqueresize=True)
        table_panel = tk.Frame(body, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        body.add(table_panel, minsize=600, stretch="always")
        table_header = tk.Frame(table_panel, bg=PANEL, padx=14, pady=12)
        table_header.pack(fill="x")
        self.label(table_header, "传感器", bg=PANEL, font=("Microsoft YaHei UI", 11, "bold")).pack(side="left")
        self.label(table_header, "点击一行查看曲线", bg=PANEL, fg=MUTED, font=("Microsoft YaHei UI", 9)).pack(side="right")
        table = tk.Frame(table_panel, bg=PANEL)
        table.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(table, columns=("current", "minimum", "maximum", "average"),
                                 show="tree headings", selectmode="browse", height=13)
        self.tree.heading("#0", text="传感器")
        self.tree.column("#0", width=205, minwidth=165, stretch=True)
        for column, title in [("current", "当前"), ("minimum", "最低"), ("maximum", "最高"), ("average", "平均")]:
            self.tree.heading(column, text=title)
            self.tree.column(column, width=91, minwidth=75, stretch=False, anchor="e")
        scrollbar = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.tag_configure("alternate", background="#1c2532")
        self.tree.tag_configure("offline", foreground="#748297")
        self.tree.bind("<<TreeviewSelect>>", self._select_sensor)

        graph_panel = tk.Frame(body, bg=PANEL, highlightbackground=LINE, highlightthickness=1)
        body.add(graph_panel, minsize=360, stretch="always")
        graph_header = tk.Frame(graph_panel, bg=PANEL, padx=14, pady=12)
        graph_header.pack(fill="x")
        self.chart_title = self.label(graph_header, "功率 · 历史曲线", bg=PANEL, font=("Microsoft YaHei UI", 11, "bold"))
        self.chart_title.pack(anchor="w")
        chart_choice = ttk.Combobox(graph_header, textvariable=self.chart_window,
                                    values=("近 5 分钟", "近 10 分钟", "全部历史"), state="readonly", width=11)
        chart_choice.pack(anchor="w", pady=(10, 0))
        chart_choice.bind("<<ComboboxSelected>>", lambda _: self.draw_chart())
        self.canvas = tk.Canvas(graph_panel, bg=PANEL, highlightthickness=0, height=260)
        self.canvas.pack(fill="both", expand=True, padx=8)
        self.canvas.bind("<Configure>", lambda _: self.draw_chart())
        self.chart_summary = self.label(graph_panel, "连接后开始记录曲线", bg=PANEL, fg=MUTED,
                                        font=("Microsoft YaHei UI", 9), anchor="w", padx=16, pady=12)
        self.chart_summary.pack(fill="x")

        bottom_panel = tk.Frame(outer, bg=BG)
        bottom_panel.pack(side="bottom", fill="x")
        bridge_row = tk.Frame(bottom_panel, bg=BG)
        bridge_row.pack(fill="x", pady=(16, 0))
        ttk.Checkbutton(bridge_row, text="接入 HWiNFO64", variable=self.hwinfo_enabled,
                        command=self.toggle_hwinfo).pack(side="left")
        self.bridge_label = self.label(bridge_row, "未启用", fg=MUTED, font=("Microsoft YaHei UI", 9))
        self.bridge_label.pack(side="left", padx=10)
        ttk.Button(bridge_row, text="使用说明", command=self.show_help).pack(side="right")
        self.footer = self.label(bottom_panel, "读取测量数据 · 不更改电源设置", fg=MUTED,
                                  font=("Microsoft YaHei UI", 9), anchor="w")
        self.footer.pack(fill="x", pady=(12, 0))
        body.pack(fill="both", expand=True)

    def _space_pause(self, event):
        if event.widget is self.tree:
            self.toggle_pause()
            return "break"

    def _poll_events(self):
        if self.closing:
            return
        for event in self.collector.get_events():
            if event.get("event") == "snapshot":
                try:
                    self._snapshot(event)
                except (ValueError, KeyError, TypeError) as exc:
                    logging.exception("Invalid sensor snapshot: %s", exc)
                    self._offline("本次数据无效，等待下一组读数")
            elif event.get("event") == "status":
                state = event.get("state", "offline")
                if state not in ("connected",):
                    self._offline(event.get("message", "正在重新连接"))
            elif event.get("event") == "error":
                self._offline("连接暂时中断，正在重试")
                logging.warning("Collector: %s", event)
        self.after(150, self._poll_events)

    def _snapshot(self, event):
        if self.paused:
            return
        now = time.monotonic()
        timestamp = event.get("received_monotonic", now)
        if not isinstance(timestamp, (int, float)) or not math.isfinite(timestamp) or timestamp > now + 1:
            raise ValueError("Invalid sample timestamp")
        if now - timestamp > max(15, float(self.interval.get()) * 3):
            self._offline("读数已过期，等待新数据")
            return
        readings = flatten_snapshot(event)
        if not readings:
            return
        self.last_received = timestamp
        self.connected = True
        self.sample_count += 1
        seen = set()
        for reading in readings:
            seen.add(reading.key)
            self.readings[reading.key] = reading
            self.stats.setdefault(reading.key, SensorStats()).update(reading.value, timestamp)
        for key, stats in self.stats.items():
            if key not in seen:
                stats.mark_offline()
        record = {"timestamp_utc": event.get("timestamp_utc", datetime.now().astimezone().isoformat()),
                  **{reading.key: reading.value for reading in readings}}
        self.records.append(record)
        if self.csv_writer:
            try:
                self.csv_writer.writerow(record)
                self.csv_file.flush()
            except OSError as exc:
                self._stop_recording()
                messagebox.showerror("日志写入失败", str(exc), parent=self)
        self.status_label.configure(text="●  电源已连接", fg=ACCENT)
        self._render_values()
        if self.hwinfo_enabled.get():
            self._publish(readings)
        self._write_runtime_status()

    def _offline(self, message):
        self.connected = False
        for stats in self.stats.values():
            stats.mark_offline()
        self.status_label.configure(text="●  已暂停" if self.paused else "●  等待电源", fg=AMBER)
        self.updated_label.configure(text="采集已暂停" if self.paused else message)
        self._clear_bridge()
        self._render_values()
        self._write_runtime_status()

    def _render_values(self):
        for key, (value, summary, unit) in self.cards.items():
            stats = self.stats.get(key)
            value.configure(text=format_value(stats.current if stats and stats.online else None, unit, suffix=False))
            if stats and stats.count:
                summary.configure(text=f"最低 {format_value(stats.minimum, unit, False)}   /   最高 {format_value(stats.maximum, unit, False)}")
        for index, (key, reading) in enumerate(self.readings.items()):
            stats = self.stats[key]
            label = reading.name if reading.group == "电源" else f"{reading.group} · {reading.name}"
            values = [format_value(stats.current if stats.online else None, reading.unit),
                      format_value(stats.minimum, reading.unit), format_value(stats.maximum, reading.unit),
                      format_value(stats.average, reading.unit)]
            tags = (("alternate",) if index % 2 else ()) + (() if stats.online else ("offline",))
            if self.tree.exists(key):
                self.tree.item(key, text=label, values=values, tags=tags)
            else:
                self.tree.insert("", "end", iid=key, text=label, values=values, tags=tags)
        if not self.tree.selection() and self.tree.exists(self.selected_sensor):
            self.tree.selection_set(self.selected_sensor)
        self.draw_chart()
        logging_text = f" · 正在记录 {self.csv_path.name}" if self.csv_path else ""
        self.footer.configure(text=f"{len(self.readings)} 个传感器 · {self.sample_count} 组快照 · 读取测量数据{logging_text}")

    def _select_sensor(self, _):
        selection = self.tree.selection()
        if selection:
            self.selected_sensor = selection[0]
            self.draw_chart()

    def draw_chart(self):
        canvas = self.canvas
        canvas.delete("all")
        width, height = max(canvas.winfo_width(), 200), max(canvas.winfo_height(), 120)
        stats = self.stats.get(self.selected_sensor)
        reading = self.readings.get(self.selected_sensor)
        if not stats or not stats.history or not reading:
            canvas.create_text(width / 2, height / 2, text="连接后将显示历史曲线", fill=MUTED,
                               font=("Microsoft YaHei UI", 11))
            return
        title = reading.name if reading.group == "电源" else f"{reading.group} · {reading.name}"
        self.chart_title.configure(text=title + " · 历史曲线")
        history = list(stats.history)
        window = {"近 5 分钟": 300, "近 10 分钟": 600}.get(self.chart_window.get())
        if window:
            history = [point for point in history if point[0] >= history[-1][0] - window]
        left, right, top, bottom = 58, width - 20, 20, height - 34
        values = [point[1] for point in history]
        low, high = min(values), max(values)
        padding = max((high - low) * 0.16, abs(high) * 0.025, 0.05 if reading.unit in ("V", "A") else 1)
        low, high = max(0, low - padding), high + padding
        for index in range(5):
            y = top + (bottom - top) * index / 4
            number = high - (high - low) * index / 4
            canvas.create_line(left, y, right, y, fill=LINE, dash=(3, 5))
            canvas.create_text(left - 10, y, text=format_value(number, reading.unit, False),
                               fill=MUTED, anchor="e", font=("Segoe UI", 9))
        start, end = history[0][0], history[-1][0]
        span = max(end - start, float(self.interval.get()))
        coordinates = [(left + (t - start) / span * (right - left),
                        bottom - (value - low) / (high - low) * (bottom - top)) for t, value in history]
        if len(coordinates) > 1:
            area = [(coordinates[0][0], bottom), *coordinates, (coordinates[-1][0], bottom)]
            canvas.create_polygon(*[number for pair in area for number in pair], fill="#1c3838", outline="")
            segment = []
            for index, point in enumerate(coordinates):
                if index and history[index][0] - history[index - 1][0] > max(float(self.interval.get()) * 3, 6):
                    if len(segment) > 2:
                        canvas.create_line(*segment, fill=ACCENT, width=2)
                    segment = []
                segment.extend(point)
            if len(segment) > 2:
                canvas.create_line(*segment, fill=ACCENT, width=2)
        x, y = coordinates[-1]
        canvas.create_oval(x - 3, y - 3, x + 3, y + 3, fill=ACCENT, outline="")
        canvas.create_text(left, bottom + 20, text=f"{int(end - start)} 秒前", fill=MUTED, anchor="w", font=("Microsoft YaHei UI", 9))
        canvas.create_text(right, bottom + 20, text="最新", fill=MUTED, anchor="e", font=("Microsoft YaHei UI", 9))
        self.chart_summary.configure(text=f"{len(history)} 个采样点  ·  平均 {format_value(sum(values) / len(values), reading.unit)}")

    def toggle_pause(self):
        if self.paused:
            self.paused = False
            self.pause_button.configure(text="暂停采集")
            self.status_label.configure(text="●  正在连接", fg=AMBER)
            self.collector.start()
        else:
            self.paused = True
            self.collector.stop()
            self.pause_button.configure(text="继续采集")
            self._offline("采集已暂停")

    def reset_statistics(self):
        for stats in self.stats.values():
            stats.reset(keep_current=True)
        self.records.clear()
        self.sample_count = 0
        self._render_values()

    def change_interval(self, _=None):
        self._offline("更新采样间隔，正在连接")
        self.collector.set_interval(float(self.interval.get()))

    def toggle_recording(self):
        if self.csv_file:
            self._stop_recording()
            return
        logs = DATA_DIR / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        default = "omen_psu_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".csv"
        path = filedialog.asksaveasfilename(parent=self, title="保存实时采样日志", initialdir=logs,
                                          initialfile=default, defaultextension=".csv",
                                          filetypes=[("CSV 文件", "*.csv")])
        if not path:
            return
        try:
            self.csv_path = Path(path)
            self.csv_file = self.csv_path.open("w", newline="", encoding="utf-8-sig")
            self.csv_writer = csv.DictWriter(self.csv_file, fieldnames=CSV_FIELDS, extrasaction="ignore")
            self.csv_writer.writeheader()
            self.csv_file.flush()
            self.record_button.configure(text="停止记录", style="Accent.TButton")
            self._render_values()
        except OSError as exc:
            self._stop_recording()
            messagebox.showerror("无法创建日志", str(exc), parent=self)

    def _stop_recording(self):
        try:
            if self.csv_file:
                self.csv_file.close()
        except OSError:
            logging.exception("Cannot flush or close CSV log")
        finally:
            self.csv_file = self.csv_writer = self.csv_path = None
            self.record_button.configure(text="记录 CSV", style="TButton")

    def export_csv(self):
        if not self.records:
            messagebox.showinfo("还没有数据", "收到传感器读数后即可导出。", parent=self)
            return
        path = filedialog.asksaveasfilename(parent=self, title="导出当前会话历史", initialdir=DATA_DIR,
                                          initialfile="omen_psu_history.csv", defaultextension=".csv",
                                          filetypes=[("CSV 文件", "*.csv")])
        if path:
            if self.csv_path and Path(path).resolve() == self.csv_path.resolve():
                messagebox.showwarning("文件正在记录", "这个文件正在写入实时日志。请另选文件，或先停止记录。", parent=self)
                return
            try:
                with open(path, "w", newline="", encoding="utf-8-sig") as output:
                    writer = csv.DictWriter(output, fieldnames=CSV_FIELDS, extrasaction="ignore")
                    writer.writeheader()
                    writer.writerows(self.records)
                self.footer.configure(text=f"已导出 {len(self.records)} 组快照 · {Path(path).name}")
            except OSError as exc:
                messagebox.showerror("导出失败", str(exc), parent=self)

    def toggle_hwinfo(self):
        if self.hwinfo_enabled.get() and self.connected:
            self._publish([reading for key, reading in self.readings.items() if self.stats[key].online])
        elif not self.hwinfo_enabled.get():
            self._clear_bridge()
        else:
            self.bridge_label.configure(text="等待电源数据", fg=MUTED)

    def _publish(self, readings):
        try:
            self.published_count = self.bridge.publish(readings)
            self.bridge_message = f"已发布 {self.published_count} 项 · 首次接入需重启 HWiNFO"
            self.bridge_label.configure(text=self.bridge_message, fg=ACCENT)
        except (OSError, RuntimeError, ValueError) as exc:
            self.published_count = 0
            self.bridge_message = "接入失败：" + str(exc)
            self.bridge_label.configure(text=self.bridge_message, fg=RED)
            logging.exception("HWiNFO publishing failed")

    def _clear_bridge(self):
        try:
            self.bridge.cleanup()
            self.published_count = 0
            self.bridge_label.configure(text="等待实时数据" if self.hwinfo_enabled.get() else "未启用", fg=MUTED)
        except (OSError, RuntimeError) as exc:
            logging.warning("HWiNFO cleanup: %s", exc)
            self.bridge_label.configure(text="清理失败，请查看日志", fg=RED)

    def _age_tick(self):
        if self.closing:
            return
        if self.connected and self.last_received is not None:
            age = time.monotonic() - self.last_received
            self.updated_label.configure(text=f"{age:.0f} 秒前更新 · 每 {float(self.interval.get()):g} 秒采样")
            if age > max(15, float(self.interval.get()) * 3):
                self._offline("读数已过期，正在重新连接")
        self.after(1000, self._age_tick)

    def _write_runtime_status(self):
        try:
            status = {"version": VERSION, "online": self.connected, "paused": self.paused,
                      "samples": self.sample_count, "hwinfo_enabled": self.hwinfo_enabled.get(),
                      "hwinfo_published": self.published_count,
                      "updated_local": datetime.now().astimezone().isoformat(),
                      "readings": {key: {"value": stat.current if stat.online else None,
                                         "unit": self.readings[key].unit, "online": stat.online}
                                   for key, stat in self.stats.items()}}
            temporary = DATA_DIR / "runtime_status.json.tmp"
            temporary.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(DATA_DIR / "runtime_status.json")
        except OSError:
            logging.exception("Cannot write runtime status")

    def show_help(self):
        messagebox.showinfo("使用说明", "OMEN PSU Monitor " + VERSION + "\n\n"
                            "• 自动连接 HP OMEN USB 电源，断开后自动重试。\n"
                            "• 点击传感器查看曲线；F5 重置统计；Ctrl+S 导出历史。\n"
                            "• ‘记录 CSV’连续保存新读数，‘导出历史’保存本次会话。\n"
                            "• 勾选‘接入 HWiNFO64’后，首次需要重新打开 HWiNFO。\n"
                            "  在其 Sensors 列表中查看 HP OMEN PSU (ImprovingDual)。\n"
                            "• 保持本程序运行以持续提供 HWiNFO 读数。\n"
                            "• 暂停、断开或退出时撤销本程序发布的自定义传感器。\n\n"
                            "只读取测量值，不调整风扇、电源设置或固件。\n"
                            "曲线最多保留每项 600 个点，导出历史最多 50000 组。\n"
                            f"日志和配置目录：\n{DATA_DIR}", parent=self)

    def close(self):
        if self.closing:
            return
        self.closing = True
        self.connected = False
        for timer_id in self.tk.call("after", "info"):
            try:
                self.after_cancel(timer_id)
            except tk.TclError:
                pass
        self.collector.stop()
        for stats in self.stats.values():
            stats.mark_offline()
        self._clear_bridge()
        self._stop_recording()
        try:
            self.config_path.write_text(json.dumps({"interval": float(self.interval.get()),
                                                   "hwinfo": self.hwinfo_enabled.get()}, indent=2), encoding="utf-8")
        except OSError:
            logging.exception("Cannot save settings")
        self._write_runtime_status()
        self.destroy()


def main():
    global DATA_DIR
    if "--collector-worker" in sys.argv:
        return worker_main()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hwinfo", action="store_true", help="Enable the HWiNFO custom sensor bridge.")
    parser.add_argument("--portable", action="store_true", help="Keep settings and logs in the EXE's adjacent data folder.")
    parser.add_argument("--auto-close", type=float, help=argparse.SUPPRESS)
    args = parser.parse_args()
    DATA_DIR = resolve_data_dir(portable=args.portable)
    mutex = None
    if sys.platform == "win32":
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        kernel.CreateMutexW.restype = ctypes.c_void_p
        kernel.CloseHandle.argtypes, kernel.CloseHandle.restype = [ctypes.c_void_p], ctypes.c_int
        mutex = kernel.CreateMutexW(None, False, "Local\\ImprovingDual.OmenPsuMonitor")
        if mutex and ctypes.get_last_error() == 183:
            kernel.CloseHandle(mutex)
            root = tk.Tk()
            root.withdraw()
            messagebox.showinfo("程序已在运行", "OMEN PSU Monitor 已打开，请查看已有窗口。", parent=root)
            root.destroy()
            return 0
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                            handlers=[RotatingFileHandler(DATA_DIR / "app.log", maxBytes=1024 * 1024,
                                                          backupCount=3, encoding="utf-8")])
        app = OmenPsuApp(enable_hwinfo=args.hwinfo, auto_close=args.auto_close)
        app.mainloop()
        return 0
    except Exception:
        logging.exception("Application error")
        try:
            messagebox.showerror("启动失败", f"程序启动失败，请检查目录权限。\n日志目录：{DATA_DIR}")
        except tk.TclError:
            pass
        return 1
    finally:
        if mutex:
            kernel.CloseHandle(mutex)


if __name__ == "__main__":
    raise SystemExit(main())
