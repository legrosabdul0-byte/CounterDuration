"""
视频时长统计 - 图形界面 (抢先测试版 / Beta)

界面用 CustomTkinter (现代深色/浅色主题, 圆角控件)。
扫描引擎复用 CounterDuration.py (analyze_metadata / deep_scan 等), 不重复造轮子。

打包:
    pip install customtkinter
    pyinstaller --onefile --windowed --name CounterDuration-gui \
        --collect-all customtkinter --add-binary "ffmpeg.exe;." CounterDurationGUI.py
"""

import os
import sys
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

DRIVE_LABELS = {
    "removable": "U盘/移动盘", "fixed": "本地硬盘", "remote": "网络盘",
    "cdrom": "光驱", "ramdisk": "内存盘", "noroot": "未知设备", "unknown": "未知设备",
}

MONO = "Consolas" if sys.platform == "win32" else "Menlo"


def fmt_hms(seconds):
    return str(datetime.timedelta(seconds=int(seconds)))


# ---------------------------------------------------------------------------
# 扫描工作线程 (后台跑, 通过 ui.* 回调更新界面)
# ---------------------------------------------------------------------------
def scan_one_folder(folder_path, ui):
    ffmpeg_path = get_ffmpeg_path()
    ui.log(f"📁  {folder_path}")
    ui.set_status("正在统计视频文件…")

    tasks = []
    for root_dir, _dirs, files in os.walk(folder_path):
        for f in files:
            if f.lower().endswith(SUPPORTED_EXTS):
                tasks.append(os.path.join(root_dir, f))

    total = len(tasks)
    ui.log(f"     共 {total} 个视频文件")
    if total == 0:
        return 0.0, 0, 0

    drive_type = get_drive_type(folder_path)
    ui.set_status("正在检测读取速度…")
    mbps = probe_disk_speed(tasks)
    workers = decide_workers(drive_type, mbps)
    speed_str = f"{mbps:.0f} MB/s" if mbps else "未知"
    ui.log(f"     设备: {DRIVE_LABELS.get(drive_type, '未知设备')}  ·  速度: {speed_str}")
    ui.log("─" * 56)

    total_seconds = 0.0
    valid = 0
    suspects = []
    done = 0
    ui.set_progress(0, total)

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        future_map = {ex.submit(analyze_metadata, (t, ffmpeg_path)): t for t in tasks}
        for fut in concurrent.futures.as_completed(future_map):
            if ui.stop_requested:
                break
            fp, dur, _status = fut.result()
            done += 1
            name = os.path.basename(fp)
            if dur > 0:
                total_seconds += dur
                valid += 1
                ui.log(f"✓  {name}    {fmt_hms(dur)}")
            else:
                suspects.append(fp)
            ui.set_progress(done, total)
            ui.set_status(f"快速读取中…  {done}/{total}")

    if suspects and not ui.stop_requested:
        ui.log("─" * 56)
        ui.log(f"⚙  {len(suspects)} 个文件需进一步核对，逐个精确核对中…")
        ui.start_busy()
        for fp in suspects:
            if ui.stop_requested:
                break
            name = os.path.basename(fp)
            timeout = compute_deep_timeout(_safe_size(fp), mbps)
            dur, method = deep_scan(
                fp, ffmpeg_path, timeout,
                on_progress=lambda sn, t: ui.set_status(f"精确核对 {sn} … 已读到 {t}"),
            )
            if dur > 0:
                total_seconds += dur
                valid += 1
                ui.log(f"★  {name}    {fmt_hms(dur)}  (精确核对)")
            else:
                ui.log(f"✗  {name}    {method}")
        ui.stop_busy()

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
            ui.log("")
            ui.log("═" * 56)
        seconds, valid, total = scan_one_folder(folder, ui)
        grand_seconds += seconds
        grand_valid += valid
        grand_total += total
        if multi:
            ui.log(f"     ↳ 本目录小计: {fmt_hms(seconds)}  ·  {valid}/{total} 文件")

    ui.finish(grand_seconds, grand_valid, grand_total, aborted=ui.stop_requested)


# ---------------------------------------------------------------------------
# 主窗口 (CustomTkinter)
# ---------------------------------------------------------------------------
class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.q = queue.Queue()
        self.stop_requested = False
        self.scanning = False
        self.folders = []

        self.title("视频时长统计")
        self.geometry("820x640")
        self.minsize(720, 560)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)

        # ── 顶栏: 标题 + 主题切换 ──
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=20, pady=(18, 6))
        header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            header, text="🎬  视频时长统计",
            font=ctk.CTkFont(size=24, weight="bold"),
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(
            header, text="抢先测试版 Beta", text_color=("#3b7dd8", "#6aa3f0"),
            font=ctk.CTkFont(size=12, weight="bold"),
        ).grid(row=1, column=0, sticky="w")
        self.theme_switch = ctk.CTkSegmentedButton(
            header, values=["浅色", "深色", "跟随系统"], command=self._on_theme,
            width=200,
        )
        self.theme_switch.set("深色")
        self.theme_switch.grid(row=0, column=1, rowspan=2, sticky="e")

        # ── 目录选择卡片 ──
        card = ctk.CTkFrame(self, corner_radius=14)
        card.grid(row=1, column=0, sticky="ew", padx=20, pady=8)
        card.grid_columnconfigure(0, weight=1)

        bar = ctk.CTkFrame(card, fg_color="transparent")
        bar.grid(row=0, column=0, sticky="ew", padx=14, pady=(12, 6))
        bar.grid_columnconfigure(2, weight=1)
        ctk.CTkButton(bar, text="➕  添加文件夹 / U盘", command=self.add_folder,
                     width=170, height=36).grid(row=0, column=0)
        ctk.CTkButton(bar, text="清空", command=self.clear_folders, width=70, height=36,
                     fg_color="transparent", border_width=1).grid(row=0, column=1, padx=8)
        self.count_var = ctk.StringVar(value="未选择目录")
        ctk.CTkLabel(bar, textvariable=self.count_var, text_color=("gray40", "gray60")).grid(
            row=0, column=2, sticky="e")

        self.folder_box = ctk.CTkTextbox(card, height=70, font=ctk.CTkFont(family=MONO, size=12),
                                         activate_scrollbars=True)
        self.folder_box.grid(row=1, column=0, sticky="ew", padx=14, pady=(0, 12))
        self.folder_box.configure(state="disabled")

        # ── 操作 + 进度 ──
        ctrl = ctk.CTkFrame(self, fg_color="transparent")
        ctrl.grid(row=2, column=0, sticky="ew", padx=20, pady=(2, 6))
        ctrl.grid_columnconfigure(2, weight=1)
        self.start_btn = ctk.CTkButton(ctrl, text="▶  开始扫描", command=self.on_start,
                                      width=130, height=40, font=ctk.CTkFont(size=14, weight="bold"))
        self.start_btn.grid(row=0, column=0)
        self.stop_btn = ctk.CTkButton(ctrl, text="■ 停止", command=self.on_stop, width=90, height=40,
                                     fg_color="#c0392b", hover_color="#a33125", state="disabled")
        self.stop_btn.grid(row=0, column=1, padx=8)

        self.progress = ctk.CTkProgressBar(ctrl, height=14, corner_radius=8)
        self.progress.grid(row=0, column=2, sticky="ew", padx=(12, 0))
        self.progress.set(0)
        self.status_var = ctk.StringVar(value="就绪")
        ctk.CTkLabel(self, textvariable=self.status_var, text_color=("gray40", "gray60"),
                    anchor="w").grid(row=2, column=0, sticky="sw", padx=24, pady=(46, 0))

        # ── 实时日志 ──
        self.log_box = ctk.CTkTextbox(self, font=ctk.CTkFont(family=MONO, size=13),
                                      corner_radius=14)
        self.log_box.grid(row=3, column=0, sticky="nsew", padx=20, pady=8)
        self.log_box.configure(state="disabled")

        # ── 底部汇总条 ──
        self.summary = ctk.CTkLabel(
            self, text="累计时长  --:--:--      折算课时  --",
            font=ctk.CTkFont(size=18, weight="bold"),
            fg_color=("#e8f0fe", "#1f2a3a"), corner_radius=12, height=52,
        )
        self.summary.grid(row=4, column=0, sticky="ew", padx=20, pady=(4, 18))

        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(80, self._drain)

        if not os.path.exists(get_ffmpeg_path()):
            messagebox.showerror("缺少组件", "核心组件 ffmpeg 缺失, 无法扫描。")
            self.start_btn.configure(state="disabled")

    # ---- 主题 ----
    def _on_theme(self, value):
        ctk.set_appearance_mode({"浅色": "light", "深色": "dark", "跟随系统": "system"}[value])

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

    def clear_folders(self):
        if self.scanning:
            return
        self.folders.clear()
        self._refresh_folders()

    def _refresh_folders(self):
        self.folder_box.configure(state="normal")
        self.folder_box.delete("1.0", "end")
        for i, f in enumerate(self.folders, 1):
            self.folder_box.insert("end", f"{i}.  {f}\n")
        self.folder_box.configure(state="disabled")
        n = len(self.folders)
        self.count_var.set("未选择目录" if n == 0 else f"已选 {n} 个目录")

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
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
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
        self.destroy()

    # ---- 工作线程 -> 入队 -> 主线程执行 ----
    def log(self, text):
        self.q.put(lambda: self._append_log(text))

    def set_status(self, text):
        self.q.put(lambda: self.status_var.set(text))

    def set_progress(self, done, total):
        self.q.put(lambda: self.progress.set(done / max(total, 1)))

    def start_busy(self):
        def _do():
            self.progress.configure(mode="indeterminate")
            self.progress.start()
        self.q.put(_do)

    def stop_busy(self):
        def _do():
            self.progress.stop()
            self.progress.configure(mode="determinate")
            self.progress.set(1)
        self.q.put(_do)

    def finish(self, seconds, valid, total, aborted=False):
        def _do():
            self.progress.stop()
            self.progress.configure(mode="determinate")
            self.progress.set(1)
            hours = seconds / CLASS_SECONDS
            self.summary.configure(
                text=f"累计时长  {fmt_hms(seconds)}      折算课时  {hours:.1f}      "
                     f"({valid}/{total} 文件)")
            self.set_status("已停止" if aborted else "扫描完成 ✅")
            self.scanning = False
            self.start_btn.configure(state="normal")
            self.stop_btn.configure(state="disabled")
            if not aborted:
                messagebox.showinfo(
                    "扫描完成",
                    f"累计时长: {fmt_hms(seconds)}\n"
                    f"折算课时: {hours:.1f} 课时 (45分钟/课时)\n"
                    f"成功: {valid} / {total} 文件",
                )
        self.q.put(_do)

    # ---- 主线程消费队列 ----
    def _drain(self):
        try:
            while True:
                self.q.get_nowait()()
        except queue.Empty:
            pass
        self.after(80, self._drain)

    def _append_log(self, text):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", text + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _clear_log(self):
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")


def main():
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")
    App().mainloop()


if __name__ == "__main__":
    main()
