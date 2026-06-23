"""
视频时长统计 - 图形界面 (抢先测试版 / Beta)

复用 CounterDuration.py 里的扫描引擎 (analyze_metadata / deep_scan 等),
只把"黑框框打印"换成窗口里的实时日志 + 进度条 + 汇总。

打包: pyinstaller --onefile --windowed --name CounterDuration-gui \
        --add-binary "ffmpeg.exe;." CounterDurationGUI.py
"""

import os
import sys
import queue
import datetime
import threading
import concurrent.futures

import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

# 复用核心扫描引擎 (同一份逻辑, 不重复造轮子)
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


def fmt_hms(seconds):
    return str(datetime.timedelta(seconds=int(seconds)))


# ---------------------------------------------------------------------------
# 扫描工作线程: 复用引擎, 通过回调把进度/日志抛给 UI (UI 自己保证线程安全)
# ---------------------------------------------------------------------------
def scan_one_folder(folder_path, ui):
    """扫描单个目录, 返回 (总秒数, 有效文件数, 文件总数)。"""
    ffmpeg_path = get_ffmpeg_path()
    ui.log(f"📁 目标目录: {folder_path}")
    ui.set_status("正在统计视频文件...")

    tasks = []
    for root_dir, _dirs, files in os.walk(folder_path):
        for f in files:
            if f.lower().endswith(SUPPORTED_EXTS):
                tasks.append(os.path.join(root_dir, f))

    total = len(tasks)
    ui.log(f"   共 {total} 个视频文件")
    if total == 0:
        return 0.0, 0, 0

    drive_type = get_drive_type(folder_path)
    ui.set_status("正在检测读取速度...")
    mbps = probe_disk_speed(tasks)
    workers = decide_workers(drive_type, mbps)
    speed_str = f"{mbps:.0f} MB/s" if mbps else "未知"
    ui.log(f"   设备类型: {DRIVE_LABELS.get(drive_type, '未知设备')} | 读取速度: {speed_str}")
    ui.log("-" * 60)

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
            name = os.path.basename(fp)
            if dur > 0:
                total_seconds += dur
                valid += 1
                ui.log(f"√ {name}    {fmt_hms(dur)}")
            else:
                suspects.append(fp)
            ui.set_progress(done, total)
            ui.set_status(f"快速读取中... {done}/{total}")

    # 阶段二: 逐个精确核对 (串行)
    if suspects and not ui.stop_requested:
        ui.log("-" * 60)
        ui.log(f"⚙ 有 {len(suspects)} 个文件需要进一步核对, 正在逐个精确核对...")
        ui.start_busy()
        for fp in suspects:
            if ui.stop_requested:
                break
            name = os.path.basename(fp)
            timeout = compute_deep_timeout(_safe_size(fp), mbps)
            dur, method = deep_scan(
                fp, ffmpeg_path, timeout,
                on_progress=lambda sn, t: ui.set_status(f"精确核对 {sn} ... 已读到 {t}"),
            )
            if dur > 0:
                total_seconds += dur
                valid += 1
                ui.log(f"★ {name}    {fmt_hms(dur)}  (精确核对)")
            else:
                ui.log(f"✗ {name}    {method}")
        ui.stop_busy()

    return total_seconds, valid, total


def run_scan(folders, ui):
    """扫描入口 (在后台线程里跑)。"""
    grand_seconds = 0.0
    grand_valid = 0
    grand_total = 0
    multi = len(folders) > 1

    for folder in folders:
        if ui.stop_requested:
            break
        if multi:
            ui.log("")
            ui.log("#" * 60)
        seconds, valid, total = scan_one_folder(folder, ui)
        grand_seconds += seconds
        grand_valid += valid
        grand_total += total
        if multi:
            ui.log(f"   ↳ 本目录: {fmt_hms(seconds)} | {valid}/{total} 文件")

    ui.finish(grand_seconds, grand_valid, grand_total, aborted=ui.stop_requested)


# ---------------------------------------------------------------------------
# 主窗口
# ---------------------------------------------------------------------------
class App:
    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self.stop_requested = False
        self.scanning = False
        self.folders = []

        root.title("视频时长统计  (抢先测试版 Beta)")
        root.geometry("720x560")
        root.minsize(620, 480)

        # 顶部: 目录选择
        top = ttk.Frame(root, padding=(12, 10, 12, 4))
        top.pack(fill="x")

        ttk.Button(top, text="➕ 添加文件夹 / U盘", command=self.add_folder).pack(side="left")
        ttk.Button(top, text="清空列表", command=self.clear_folders).pack(side="left", padx=6)
        self.count_var = tk.StringVar(value="未选择目录")
        ttk.Label(top, textvariable=self.count_var, foreground="#555").pack(side="left", padx=10)

        # 已选目录列表
        list_frame = ttk.Frame(root, padding=(12, 0))
        list_frame.pack(fill="x")
        self.folder_list = tk.Listbox(list_frame, height=3, activestyle="none")
        self.folder_list.pack(fill="x")

        # 操作按钮
        btns = ttk.Frame(root, padding=(12, 8))
        btns.pack(fill="x")
        self.start_btn = ttk.Button(btns, text="▶ 开始扫描", command=self.on_start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(btns, text="■ 停止", command=self.on_stop, state="disabled")
        self.stop_btn.pack(side="left", padx=6)

        # 进度条 + 状态
        prog = ttk.Frame(root, padding=(12, 0))
        prog.pack(fill="x")
        self.progress = ttk.Progressbar(prog, mode="determinate", maximum=100)
        self.progress.pack(fill="x")
        self.status_var = tk.StringVar(value="就绪")
        ttk.Label(prog, textvariable=self.status_var, foreground="#555").pack(anchor="w", pady=(2, 0))

        # 实时日志
        log_frame = ttk.Frame(root, padding=(12, 6))
        log_frame.pack(fill="both", expand=True)
        self.log_box = scrolledtext.ScrolledText(
            log_frame, height=12, state="disabled", wrap="none",
            font=("Consolas", 10) if sys.platform == "win32" else ("Menlo", 11),
        )
        self.log_box.pack(fill="both", expand=True)

        # 底部汇总
        self.summary_var = tk.StringVar(value="累计时长: --:--:--    折算课时: -- 课时")
        summary = ttk.Label(root, textvariable=self.summary_var, padding=(12, 8),
                            font=("", 13, "bold"), foreground="#1a5fb4")
        summary.pack(fill="x")

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.after(80, self._drain)

        if not os.path.exists(get_ffmpeg_path()):
            messagebox.showerror("缺少组件", "核心组件 ffmpeg 缺失, 无法扫描。")
            self.start_btn.config(state="disabled")

    # ---- 目录管理 ----
    def add_folder(self):
        if self.scanning:
            return
        f = filedialog.askdirectory(title="选择视频文件夹")
        if f:
            f = os.path.abspath(f)
            if f not in self.folders:
                self.folders.append(f)
                self.folder_list.insert("end", f)
            self._refresh_count()

    def clear_folders(self):
        if self.scanning:
            return
        self.folders.clear()
        self.folder_list.delete(0, "end")
        self._refresh_count()

    def _refresh_count(self):
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
        self.start_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self._clear_log()
        self.summary_var.set("扫描中...")
        threading.Thread(target=run_scan, args=(list(self.folders), self), daemon=True).start()

    def on_stop(self):
        if self.scanning:
            self.stop_requested = True
            self.set_status("正在停止... (当前文件扫完即停)")

    def on_close(self):
        if self.scanning and not messagebox.askyesno("确认", "扫描进行中, 确定要退出吗?"):
            return
        self.stop_requested = True
        self.root.destroy()

    # ---- 以下方法供工作线程调用: 一律入队, 由主线程执行 (Tk 非线程安全) ----
    def log(self, text):
        self.q.put(lambda: self._append_log(text))

    def set_status(self, text):
        self.q.put(lambda: self.status_var.set(text))

    def set_progress(self, done, total):
        def _do():
            self.progress.config(mode="determinate", maximum=max(total, 1))
            self.progress["value"] = done
        self.q.put(_do)

    def start_busy(self):
        def _do():
            self.progress.config(mode="indeterminate")
            self.progress.start(12)
        self.q.put(_do)

    def stop_busy(self):
        def _do():
            self.progress.stop()
            self.progress.config(mode="determinate")
        self.q.put(_do)

    def finish(self, seconds, valid, total, aborted=False):
        def _do():
            self.progress.stop()
            self.progress.config(mode="determinate", maximum=max(total, 1))
            self.progress["value"] = total
            hours = seconds / CLASS_SECONDS
            self.summary_var.set(
                f"累计时长: {fmt_hms(seconds)}    折算课时: {hours:.1f} 课时    "
                f"({valid}/{total} 文件)"
            )
            self.set_status("已停止" if aborted else "扫描完成 ✅")
            self.scanning = False
            self.start_btn.config(state="normal")
            self.stop_btn.config(state="disabled")
            if not aborted:
                messagebox.showinfo(
                    "扫描完成",
                    f"累计时长: {fmt_hms(seconds)}\n"
                    f"折算课时: {hours:.1f} 课时 (45分钟/课时)\n"
                    f"成功: {valid} / {total} 文件",
                )
        self.q.put(_do)

    # ---- 主线程: 消费队列 ----
    def _drain(self):
        try:
            while True:
                fn = self.q.get_nowait()
                fn()
        except queue.Empty:
            pass
        self.root.after(80, self._drain)

    def _append_log(self, text):
        self.log_box.config(state="normal")
        self.log_box.insert("end", text + "\n")
        self.log_box.see("end")
        self.log_box.config(state="disabled")

    def _clear_log(self):
        self.log_box.config(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.config(state="disabled")


def main():
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista" if sys.platform == "win32" else "clam")
    except tk.TclError:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
