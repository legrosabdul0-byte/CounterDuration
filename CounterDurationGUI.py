"""
视频时长统计 - 图形界面 (抢先测试版 / Beta)

界面用 CustomTkinter (现代深色/浅色主题, 圆角控件, 彩色统计卡片)。
扫描引擎复用 CounterDuration.py (analyze_metadata / deep_scan 等)。

打包:
    pip install customtkinter
    pyinstaller --onefile --windowed --name CounterDuration-gui \
        --collect-all customtkinter --add-binary "ffmpeg.exe;." CounterDurationGUI.py
"""

import os
import sys
import json
import queue
import datetime
import threading
import concurrent.futures

from tkinter import filedialog, messagebox
import customtkinter as ctk

# 复用核心扫描引擎 (同一份逻辑)
from CounterDuration import (
    get_ffmpeg_path,
    get_drive_type,
    probe_disk_speed,
    decide_workers,
    compute_deep_timeout,
    analyze_metadata,
    deep_scan,
    _safe_size,
    SUPPORTED_EXTS,
)

CLASS_SECONDS = 2700.0  # 45 分钟 / 课时


def _config_path():
    """配置只放系统标准目录里 (一个文件), 不往用户家目录乱塞。"""
    if sys.platform == "win32":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    else:
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config")
    return os.path.join(base, "CounterDuration", "settings.json")


CONFIG_PATH = _config_path()
MONO = "Consolas" if sys.platform == "win32" else "Menlo"

DRIVE_LABELS = {
    "removable": "U盘/移动盘", "fixed": "本地硬盘", "remote": "网络盘",
    "cdrom": "光驱", "ramdisk": "内存盘", "noroot": "未知设备", "unknown": "未知设备",
}

# 状态配色 (深/浅主题都清晰)
COLOR_OK = "#2fa572"
COLOR_DEEP = "#3b82f6"
COLOR_FAIL = "#e05260"
COLOR_DIM = "#8a8a8a"


def fmt_hms(seconds):
    return str(datetime.timedelta(seconds=int(seconds)))


def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(data):
    try:
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 扫描工作线程 (后台跑, 通过 ui.* 回调更新界面)
# ---------------------------------------------------------------------------
def scan_one_folder(folder_path, ui):
    ffmpeg_path = get_ffmpeg_path()
    ui.log(f"📁  {folder_path}", COLOR_DIM)
    ui.set_status("正在统计视频文件…")

    tasks = []
    for root_dir, _dirs, files in os.walk(folder_path):
        for f in files:
            if f.lower().endswith(SUPPORTED_EXTS):
                tasks.append(os.path.join(root_dir, f))

    total = len(tasks)
    if total == 0:
        ui.log("     未找到视频文件", COLOR_DIM)
        return 0.0, 0, 0

    drive_type = get_drive_type(folder_path)
    ui.set_status("正在检测读取速度…")
    mbps = probe_disk_speed(tasks)
    workers = decide_workers(drive_type, mbps)
    speed_str = f"{mbps:.0f} MB/s" if mbps else "未知"
    ui.log(f"     {total} 个文件  ·  {DRIVE_LABELS.get(drive_type, '未知设备')}  ·  {speed_str}", COLOR_DIM)
    ui.log("─" * 58, COLOR_DIM)

    total_seconds = 0.0
    valid = 0
    suspects = []
    done = 0
    ui.set_progress(0, total)

    # 阶段一: 快速读取 (自适应并发)
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        future_map = {ex.submit(analyze_metadata, (t, ffmpeg_path)): t for t in tasks}
        for fut in concurrent.futures.as_completed(future_map):
            if ui.stop_requested:
                break
            fp, dur, _status = fut.result()
            done += 1
            if dur > 0:
                total_seconds += dur
                valid += 1
                ui.record("ok", os.path.basename(fp), fp, dur, "成功")
            else:
                suspects.append(fp)
            ui.set_progress(done, total)
            ui.set_status(f"快速读取中…  {done}/{total}")

    # 阶段二: 逐个精确核对 (串行); 进度把它也算进去
    if suspects and not ui.stop_requested:
        ui.log(f"⚙  {len(suspects)} 个文件需进一步核对，逐个精确核对中…", COLOR_DEEP)
        denom = total + len(suspects)
        for fp in suspects:
            if ui.stop_requested:
                break
            timeout = compute_deep_timeout(_safe_size(fp), mbps)
            dur, method = deep_scan(
                fp, ffmpeg_path, timeout,
                on_progress=lambda sn, t: ui.set_status(f"精确核对 {sn} … 已读到 {t}"),
            )
            done += 1
            name = os.path.basename(fp)
            if dur > 0:
                total_seconds += dur
                valid += 1
                ui.record("deep", name, fp, dur, "精确核对")
            else:
                ui.record("fail", name, fp, 0.0, method)
            ui.set_progress(done, denom)

    return total_seconds, valid, total


def run_scan(folders, ui):
    grand_seconds = 0.0
    grand_valid = 0
    grand_total = 0
    multi = len(folders) > 1

    for folder in folders:
        if ui.stop_requested:
            break
        if multi:
            ui.log("", None)
            ui.log("═" * 58, COLOR_DIM)
        seconds, valid, total = scan_one_folder(folder, ui)
        grand_seconds += seconds
        grand_valid += valid
        grand_total += total
        if multi:
            ui.log(f"     ↳ 本目录小计: {fmt_hms(seconds)}  ·  {valid}/{total} 文件", COLOR_DIM)

    ui.finish(grand_seconds, grand_valid, grand_total, aborted=ui.stop_requested)


# ---------------------------------------------------------------------------
# 统计卡片
# ---------------------------------------------------------------------------
class StatCard(ctk.CTkFrame):
    def __init__(self, master, caption, color):
        super().__init__(master, corner_radius=12)
        self.grid_columnconfigure(0, weight=1)
        self.value = ctk.CTkLabel(self, text="0", font=ctk.CTkFont(size=22, weight="bold"),
                                  text_color=color)
        self.value.grid(row=0, column=0, padx=14, pady=(10, 0), sticky="w")
        ctk.CTkLabel(self, text=caption, font=ctk.CTkFont(size=12),
                     text_color=("gray35", "gray60")).grid(row=1, column=0, padx=14,
                                                            pady=(0, 10), sticky="w")

    def set(self, text):
        self.value.configure(text=str(text))


# ---------------------------------------------------------------------------
# 主窗口
# ---------------------------------------------------------------------------
class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.cfg = load_config()
        self.q = queue.Queue()
        self.stop_requested = False
        self.scanning = False
        self.folders = list(self.cfg.get("folders", []))
        self.results = []  # (name, path, seconds, status)
        self.stat_total = self.stat_ok = self.stat_fail = 0
        self.last_seconds = 0.0

        self.title("视频时长统计")
        self.geometry(self.cfg.get("geometry", "860x720"))
        self.minsize(760, 620)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(4, weight=1)

        # ── 顶栏 ──
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=22, pady=(18, 4))
        header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(header, text="🎬  视频时长统计",
                     font=ctk.CTkFont(size=25, weight="bold")).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(header, text="抢先测试版 Beta", text_color=("#3b7dd8", "#6aa3f0"),
                     font=ctk.CTkFont(size=12, weight="bold")).grid(row=1, column=0, sticky="w")
        self.theme_switch = ctk.CTkSegmentedButton(
            header, values=["浅色", "深色", "跟随系统"], command=self._on_theme, width=210)
        self.theme_switch.set({"light": "浅色", "dark": "深色", "system": "跟随系统"}
                              .get(self.cfg.get("theme", "dark"), "深色"))
        self.theme_switch.grid(row=0, column=1, rowspan=2, sticky="e")

        # ── 目录卡片 (可逐项删除) ──
        card = ctk.CTkFrame(self, corner_radius=14)
        card.grid(row=1, column=0, sticky="ew", padx=22, pady=8)
        card.grid_columnconfigure(0, weight=1)
        bar = ctk.CTkFrame(card, fg_color="transparent")
        bar.grid(row=0, column=0, sticky="ew", padx=14, pady=(12, 4))
        bar.grid_columnconfigure(2, weight=1)
        ctk.CTkButton(bar, text="➕  添加文件夹 / U盘", command=self.add_folder,
                      width=180, height=36).grid(row=0, column=0)
        ctk.CTkButton(bar, text="清空", command=self.clear_folders, width=70, height=36,
                      fg_color="transparent", border_width=1).grid(row=0, column=1, padx=8)
        self.count_var = ctk.StringVar(value="未选择目录")
        ctk.CTkLabel(bar, textvariable=self.count_var,
                     text_color=("gray40", "gray60")).grid(row=0, column=2, sticky="e")
        self.folder_frame = ctk.CTkScrollableFrame(card, height=86, fg_color=("gray92", "gray17"))
        self.folder_frame.grid(row=1, column=0, sticky="ew", padx=14, pady=(2, 12))
        self.folder_frame.grid_columnconfigure(0, weight=1)

        # ── 操作 + 进度 ──
        ctrl = ctk.CTkFrame(self, fg_color="transparent")
        ctrl.grid(row=2, column=0, sticky="ew", padx=22, pady=(2, 2))
        ctrl.grid_columnconfigure(2, weight=1)
        self.start_btn = ctk.CTkButton(ctrl, text="▶  开始扫描", command=self.on_start,
                                       width=130, height=42,
                                       font=ctk.CTkFont(size=15, weight="bold"))
        self.start_btn.grid(row=0, column=0)
        self.stop_btn = ctk.CTkButton(ctrl, text="■ 停止", command=self.on_stop, width=90, height=42,
                                      fg_color=COLOR_FAIL, hover_color="#b8434f", state="disabled")
        self.stop_btn.grid(row=0, column=1, padx=8)
        self.progress = ctk.CTkProgressBar(ctrl, height=16, corner_radius=8)
        self.progress.grid(row=0, column=2, sticky="ew", padx=(12, 0))
        self.progress.set(0)
        self.status_var = ctk.StringVar(value="就绪")
        ctk.CTkLabel(self, textvariable=self.status_var, text_color=("gray40", "gray60"),
                     anchor="w").grid(row=2, column=0, sticky="sw", padx=26, pady=(50, 0))

        # ── 统计卡片栏 ──
        stats = ctk.CTkFrame(self, fg_color="transparent")
        stats.grid(row=3, column=0, sticky="ew", padx=22, pady=(8, 2))
        for i in range(4):
            stats.grid_columnconfigure(i, weight=1)
        self.card_total = StatCard(stats, "文件总数", ("gray20", "gray85"))
        self.card_ok = StatCard(stats, "成功", COLOR_OK)
        self.card_fail = StatCard(stats, "需核对 / 失败", COLOR_FAIL)
        self.card_hours = StatCard(stats, "折算课时", COLOR_DEEP)
        for i, c in enumerate((self.card_total, self.card_ok, self.card_fail, self.card_hours)):
            c.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else 8, 0))

        # ── 日志 ──
        log_wrap = ctk.CTkFrame(self, corner_radius=14)
        log_wrap.grid(row=4, column=0, sticky="nsew", padx=22, pady=8)
        log_wrap.grid_columnconfigure(0, weight=1)
        log_wrap.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(log_wrap, text="实时日志", font=ctk.CTkFont(size=13, weight="bold"),
                     text_color=("gray35", "gray65")).grid(row=0, column=0, sticky="w",
                                                            padx=16, pady=(10, 0))
        self.log_box = ctk.CTkTextbox(log_wrap, font=ctk.CTkFont(family=MONO, size=13),
                                      corner_radius=10)
        self.log_box.grid(row=1, column=0, sticky="nsew", padx=12, pady=(4, 12))
        self.log_box.configure(state="disabled")
        for tag, col in (("ok", COLOR_OK), ("deep", COLOR_DEEP),
                         ("fail", COLOR_FAIL), ("dim", COLOR_DIM)):
            self.log_box._textbox.tag_configure(tag, foreground=col)

        # ── 底部: 汇总 + 导出 ──
        bottom = ctk.CTkFrame(self, fg_color="transparent")
        bottom.grid(row=5, column=0, sticky="ew", padx=22, pady=(2, 18))
        bottom.grid_columnconfigure(0, weight=1)
        self.summary = ctk.CTkLabel(
            bottom, text="累计时长  --:--:--      折算课时  --",
            font=ctk.CTkFont(size=18, weight="bold"),
            fg_color=("#e8f0fe", "#1f2a3a"), corner_radius=12, height=54)
        self.summary.grid(row=0, column=0, sticky="ew")
        self.export_btn = ctk.CTkButton(bottom, text="📋  复制结果", command=self.copy_results,
                                        width=120, height=44, state="disabled",
                                        fg_color=COLOR_OK, hover_color="#268a5f")
        self.export_btn.grid(row=0, column=1, padx=(10, 0))

        self._refresh_folders()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(80, self._drain)

        if not os.path.exists(get_ffmpeg_path()):
            messagebox.showerror("缺少组件", "核心组件 ffmpeg 缺失, 无法扫描。")
            self.start_btn.configure(state="disabled")

    # ---- 主题 ----
    def _on_theme(self, value):
        mode = {"浅色": "light", "深色": "dark", "跟随系统": "system"}[value]
        ctk.set_appearance_mode(mode)
        self.cfg["theme"] = mode
        save_config(self.cfg)

    # ---- 目录管理 ----
    def add_folder(self):
        if self.scanning:
            return
        f = filedialog.askdirectory(title="选择视频文件夹")
        if f:
            f = os.path.abspath(f)
            if f not in self.folders:
                self.folders.append(f)
            self._refresh_folders()

    def remove_folder(self, path):
        if self.scanning:
            return
        if path in self.folders:
            self.folders.remove(path)
            self._refresh_folders()

    def clear_folders(self):
        if self.scanning:
            return
        self.folders.clear()
        self._refresh_folders()

    def _refresh_folders(self):
        for w in self.folder_frame.winfo_children():
            w.destroy()
        if not self.folders:
            ctk.CTkLabel(self.folder_frame, text="（还没添加目录，点上面的「添加文件夹 / U盘」）",
                         text_color=("gray50", "gray55")).grid(row=0, column=0, sticky="w", pady=6)
        for i, f in enumerate(self.folders):
            row = ctk.CTkFrame(self.folder_frame, fg_color="transparent")
            row.grid(row=i, column=0, sticky="ew", pady=2)
            row.grid_columnconfigure(0, weight=1)
            ctk.CTkLabel(row, text=f"{i + 1}.  {f}", anchor="w",
                         font=ctk.CTkFont(family=MONO, size=12)).grid(row=0, column=0, sticky="ew")
            ctk.CTkButton(row, text="✕", width=28, height=24, fg_color="transparent",
                          border_width=1, text_color=COLOR_FAIL,
                          command=lambda p=f: self.remove_folder(p)).grid(row=0, column=1, padx=4)
        self.count_var.set("未选择目录" if not self.folders else f"已选 {len(self.folders)} 个目录")
        self.cfg["folders"] = self.folders
        save_config(self.cfg)

    # ---- 扫描控制 ----
    def on_start(self):
        if self.scanning:
            return
        if not self.folders:
            self.add_folder()
            if not self.folders:
                return
        self.scanning = True
        self.stop_requested = False
        self.results = []
        self.stat_total = self.stat_ok = self.stat_fail = 0
        self._update_stats(0.0)
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.export_btn.configure(state="disabled")
        self._clear_log()
        self.summary.configure(text="扫描中…")
        threading.Thread(target=run_scan, args=(list(self.folders), self), daemon=True).start()

    def on_stop(self):
        if self.scanning:
            self.stop_requested = True
            self.set_status("正在停止…（当前文件扫完即停）")

    def on_close(self):
        if self.scanning and not messagebox.askyesno("确认", "扫描进行中, 确定要退出吗?"):
            return
        self.stop_requested = True
        self.cfg["geometry"] = self.geometry()
        save_config(self.cfg)
        self.destroy()

    # ---- 复制结果到剪贴板 (不落文件) ----
    def copy_results(self):
        if not self.results:
            messagebox.showinfo("提示", "还没有可复制的结果。")
            return
        hours = self.last_seconds / CLASS_SECONDS
        lines = ["文件名\t时长\t状态"]
        for name, _p, secs, status in self.results:
            lines.append(f"{name}\t{fmt_hms(secs) if secs > 0 else '--:--:--'}\t{status}")
        lines += [
            "",
            f"累计时长\t{fmt_hms(self.last_seconds)}",
            f"折算课时\t{hours:.1f}",
            f"成功/总数\t{self.stat_ok}/{self.stat_total}",
        ]
        text = "\n".join(lines)
        self.clipboard_clear()
        self.clipboard_append(text)
        self.update()  # 确保剪贴板写入生效
        self.set_status("结果已复制到剪贴板，可直接粘进 Excel / 记事本 / 聊天框")
        messagebox.showinfo("已复制",
                            "结果已复制到剪贴板。\n直接 Ctrl+V 粘到 Excel 会自动分列。")

    # ---- 工作线程 -> 入队 -> 主线程执行 ----
    def log(self, text, tag=None):
        self.q.put(lambda: self._append_log(text, tag))

    def record(self, kind, name, path, secs, status_text):
        def _do():
            self.results.append((name, path, secs, status_text))
            self.stat_total += 1
            if kind == "fail":
                self.stat_fail += 1
            else:
                self.stat_ok += 1
            self._update_stats(None)
            icon = {"ok": "✓", "deep": "★", "fail": "✗"}[kind]
            dur = fmt_hms(secs) if secs > 0 else "--:--:--"
            self._append_log(f"{icon}  {name:<32} {dur}   {status_text}", kind)
        self.q.put(_do)

    def set_status(self, text):
        self.q.put(lambda: self.status_var.set(text))

    def set_progress(self, done, total):
        self.q.put(lambda: self.progress.set(done / max(total, 1)))

    # 兼容旧引擎调用 (现进度已纳入深扫, 这俩留空)
    def start_busy(self):
        pass

    def stop_busy(self):
        pass

    def finish(self, seconds, valid, total, aborted=False):
        def _do():
            self.progress.set(1)
            self.last_seconds = seconds
            self._update_stats(seconds)
            hours = seconds / CLASS_SECONDS
            self.summary.configure(
                text=f"累计时长  {fmt_hms(seconds)}      折算课时  {hours:.1f}      "
                     f"({valid}/{total} 文件)")
            self.set_status("已停止" if aborted else "扫描完成 ✅")
            self.scanning = False
            self.start_btn.configure(state="normal")
            self.stop_btn.configure(state="disabled")
            if self.results:
                self.export_btn.configure(state="normal")
            if not aborted:
                messagebox.showinfo(
                    "扫描完成",
                    f"累计时长: {fmt_hms(seconds)}\n"
                    f"折算课时: {hours:.1f} 课时 (45分钟/课时)\n"
                    f"成功: {valid} / {total} 文件")
        self.q.put(_do)

    def _update_stats(self, seconds):
        self.card_total.set(self.stat_total)
        self.card_ok.set(self.stat_ok)
        self.card_fail.set(self.stat_fail)
        if seconds is not None:
            self.card_hours.set(f"{seconds / CLASS_SECONDS:.1f}")

    # ---- 主线程消费队列 ----
    def _drain(self):
        try:
            while True:
                self.q.get_nowait()()
        except queue.Empty:
            pass
        self.after(80, self._drain)

    def _append_log(self, text, tag=None):
        self.log_box.configure(state="normal")
        if tag:
            self.log_box.insert("end", text + "\n", tag)
        else:
            self.log_box.insert("end", text + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _clear_log(self):
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")


def main():
    cfg = load_config()
    ctk.set_appearance_mode(cfg.get("theme", "dark"))
    ctk.set_default_color_theme("blue")
    App().mainloop()


if __name__ == "__main__":
    main()
