import os
import sys
import subprocess
import re
import datetime
import tkinter as tk
from tkinter import filedialog, messagebox
import concurrent.futures
import time
import threading

print_lock = threading.Lock()

# --- 预编译正则 (避免每次调用重复编译) ---
DURATION_RE = re.compile(r"Duration:\s*(\d{2}:\d{2}:\d{2}\.\d{2})")
TIME_RE = re.compile(r"time=\s*(\d{2}:\d{2}:\d{2}\.\d{2})")

# 头部时长天生不可信的裸流 / 监控格式 -> 一律视为可疑, 走深度扫描
RAW_STREAM_EXTS = ('.264', '.h264', '.265', '.h265', '.hevc', '.dav')
# ffmpeg 输出里出现这些标记说明索引/封装有问题 -> 可疑
ERROR_MARKERS = (
    "moov atom not found",
    "invalid data found",
    "could not find codec parameters",
    "error while decoding",
    "partial file",
)
# 码率低于该阈值视为可疑 (可能截断 / header 撒谎)
MIN_BITRATE = 100 * 1024  # 100 KB/s
SUPPORTED_EXTS = ('.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.dav', '.264', '.ts')

# Windows 下隐藏子进程控制台窗口
_SUBPROCESS_FLAGS = 0x08000000 if sys.platform == "win32" else 0  # CREATE_NO_WINDOW


def get_resource_path(filename):
    if hasattr(sys, '_MEIPASS'):
        return os.path.join(sys._MEIPASS, filename)
    return os.path.join(os.path.abspath("."), filename)


def get_ffmpeg_path():
    """跨平台选择 ffmpeg 可执行文件名 (打包发布为 Windows, 本地调试兼容其它系统)。"""
    binary = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
    return get_resource_path(binary)


def parse_ffmpeg_time(time_str):
    try:
        h, m, s = time_str.split(':')
        return int(h) * 3600 + int(m) * 60 + float(s)
    except (ValueError, AttributeError):
        return 0.0


def _safe_size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


# --- 盘类型探测 (Windows, 零依赖 ctypes) ---
def get_drive_type(path):
    """返回 removable / fixed / remote / cdrom / ramdisk / unknown。
    非 Windows 一律按 fixed (本地) 处理。"""
    if sys.platform != "win32":
        return "fixed"
    try:
        import ctypes
        drive = os.path.splitdrive(os.path.abspath(path))[0]
        if not drive:
            return "unknown"
        root = drive + "\\"
        t = ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(root))
        return {
            0: "unknown", 1: "noroot", 2: "removable",
            3: "fixed", 4: "remote", 5: "cdrom", 6: "ramdisk",
        }.get(t, "unknown")
    except Exception:
        return "unknown"


# --- 测速: seek 到文件中间, 时间盒读取一小段 (与文件大小解耦) ---
def probe_disk_speed(files, time_budget=1.5, byte_cap=256 * 1024 * 1024, chunk=4 * 1024 * 1024):
    """挑最大的文件, seek 到中间读 ~1.5s 测 MB/s。
    从中间读可避开 ffmpeg 刚读过的头部缓存, 更接近深扫真实速度。
    返回 MB/s 或 None。"""
    if not files:
        return None
    candidate = max(files, key=_safe_size)
    size = _safe_size(candidate)
    if size <= 0:
        return None
    try:
        with open(candidate, "rb") as f:
            f.seek(size // 2)
            read_bytes = 0
            t0 = time.time()
            while read_bytes < byte_cap and (time.time() - t0) < time_budget:
                data = f.read(chunk)
                if not data:
                    break
                read_bytes += len(data)
            elapsed = time.time() - t0
    except OSError:
        return None
    if elapsed <= 0 or read_bytes <= 0:
        return None
    return (read_bytes / (1024 * 1024)) / elapsed


def decide_workers(drive_type, mbps):
    """决定元数据阶段并发数。深度扫描永远串行, 不受此影响。
    WORKERS 环境变量可强制覆盖自适应。"""
    env = os.environ.get("WORKERS")
    if env:
        try:
            return max(1, int(env))
        except ValueError:
            pass
    # 可移动盘 / 网络盘 / 光驱 / 未知 -> 保守串行 (兜住垃圾 U 盘)
    if drive_type in ("removable", "remote", "cdrom", "noroot", "unknown"):
        return 1
    cpu = os.cpu_count() or 4
    if mbps is None:
        return 2
    if mbps > 400:
        return min(cpu, 8)
    if mbps > 150:
        return min(cpu, 4)
    if mbps > 60:
        return 2
    return 1


def compute_deep_timeout(file_size, mbps, safety=4.0, floor=30.0, ceil=1800.0):
    """根据实测速度 + 文件大小动态计算深扫超时, 取代死板的 600s。"""
    if not mbps or mbps <= 0:
        return 600.0
    est = (file_size / (1024 * 1024)) / mbps  # 完整读一遍的预计秒数
    return max(floor, min(ceil, est * safety))


# --- 阶段一: 快速元数据分析 + 可疑判定 (可并发) ---
def analyze_metadata(file_info):
    """只读头部, 判断能否直接信任 header。
    返回 (file_path, duration_or_0, status):
      duration > 0  -> 干净文件, 直接采信 header
      duration == 0 -> 可疑, 需要进入深度扫描 (status 为可疑原因)"""
    file_path, ffmpeg_path = file_info
    ext = os.path.splitext(file_path)[1].lower()

    cmd_quick = [ffmpeg_path, "-i", file_path]
    try:
        result = subprocess.run(
            cmd_quick, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding='utf-8', errors='ignore', timeout=10,
            creationflags=_SUBPROCESS_FLAGS,
        )
    except subprocess.TimeoutExpired:
        return file_path, 0.0, "可疑:元数据超时"
    except Exception:
        return file_path, 0.0, "可疑:元数据错误"

    output = result.stderr or ""
    low = output.lower()

    # 规则3: ffmpeg 报错标记 -> 可疑
    if any(marker in low for marker in ERROR_MARKERS):
        return file_path, 0.0, "可疑:封装异常"

    # 规则4: 裸流 / 监控格式 -> 头部时长无意义, 可疑
    if ext in RAW_STREAM_EXTS:
        return file_path, 0.0, "可疑:裸流格式"

    # 规则1: 没有 Duration -> 可疑
    match = DURATION_RE.search(output)
    if not match:
        return file_path, 0.0, "可疑:无时长"

    header_seconds = parse_ffmpeg_time(match.group(1))
    if header_seconds <= 0:
        return file_path, 0.0, "可疑:时长为零"

    # 规则2: 码率不合理 -> 可疑
    file_size = _safe_size(file_path)
    if file_size <= 0:
        return file_path, 0.0, "可疑:大小异常"
    if file_size / header_seconds < MIN_BITRATE:
        return file_path, 0.0, "可疑:码率过低"

    # 全部通过 -> 干净, 直接信任 header
    return file_path, header_seconds, "索引读取"


# --- 阶段二辅助: 跑一次 ffmpeg 并实时刷进度, 返回 (stderr文本, 是否超时) ---
def _run_ffmpeg_capture(cmd, timeout, short_name):
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            bufsize=0, creationflags=_SUBPROCESS_FLAGS,
        )
    except Exception:
        return "", False

    collected = bytearray()

    def reader():
        try:
            while True:
                buf = proc.stderr.read(512)
                if not buf:
                    break
                collected.extend(buf)
        except Exception:
            pass

    t = threading.Thread(target=reader, daemon=True)
    t.start()

    start = time.time()
    timed_out = False
    while True:
        if proc.poll() is not None:
            break
        if time.time() - start > timeout:
            proc.kill()
            timed_out = True
            break
        tail = bytes(collected[-4096:]).decode('utf-8', 'ignore')
        progress = TIME_RE.findall(tail)
        if progress:
            with print_lock:
                print(f"\r-> 正在精确核对 {short_name} ... 已读到 {progress[-1]}", end="", flush=True)
        time.sleep(0.5)

    t.join(timeout=1)
    return bytes(collected).decode('utf-8', 'ignore'), timed_out


def _clear_progress_line():
    with print_lock:
        print("\r" + " " * 78 + "\r", end="", flush=True)


def _extract_duration(output):
    matches = TIME_RE.findall(output)
    if matches:
        final_seconds = parse_ffmpeg_time(matches[-1])
        if final_seconds > 1:
            return final_seconds
    return 0.0


def _failure_reason(output):
    """把 ffmpeg 报错翻译成人话, 让用户知道是坏档而不是工具 bug。"""
    low = output.lower()
    if "moov atom not found" in low:
        return "文件损坏:moov缺失(录制中断)"
    if "invalid data found" in low:
        return "文件损坏:数据非法"
    if "could not find codec parameters" in low:
        return "无法识别编码"
    if "error while decoding" in low:
        return "解码出错:帧损坏"
    return "解析失败"


# --- 阶段二: 全量深度扫描 (串行, 带实时进度 + 动态超时) ---
def deep_scan(file_path, ffmpeg_path, timeout):
    filename = os.path.basename(file_path)
    short_name = filename if len(filename) <= 30 else filename[:27] + "..."

    # 第一遍: 流复制 (快, 不解码), 多数可疑文件够用
    out1, to1 = _run_ffmpeg_capture(
        [ffmpeg_path, "-i", file_path, "-c", "copy", "-f", "null", "-"],
        timeout, short_name,
    )
    if to1:
        _clear_progress_line()
        return 0.0, "读取超时"
    dur = _extract_duration(out1)
    if dur > 0:
        _clear_progress_line()
        return dur, "全量校验"

    # 第二遍: 容错全解码 (慢, 但能救回索引损坏 / 时间戳缺失但帧数据尚存的文件)
    out2, to2 = _run_ffmpeg_capture(
        [ffmpeg_path, "-err_detect", "ignore_err", "-fflags", "+discardcorrupt",
         "-i", file_path, "-f", "null", "-"],
        timeout, short_name,
    )
    _clear_progress_line()
    if to2:
        return 0.0, "读取超时"
    dur = _extract_duration(out2)
    if dur > 0:
        return dur, "深度解码"

    # 两遍都拿不到时长 -> 文件真的残缺, 给出具体原因
    return 0.0, _failure_reason(out1 + "\n" + out2)


# --- 主流程 ---
def scan_folder(folder_path):
    ffmpeg_path = get_ffmpeg_path()

    print(f"目标目录: {folder_path}")
    print("正在统计视频文件...", end="", flush=True)

    tasks = []
    for root, dirs, files in os.walk(folder_path):
        for file in files:
            if file.lower().endswith(SUPPORTED_EXTS):
                tasks.append(os.path.join(root, file))

    total_files = len(tasks)
    print(f" 就绪 (共 {total_files} 个文件)")

    if total_files == 0:
        return 0.0, 0, 0, 0.0

    # 自适应: 盘类型 + 中间段测速 -> 决定元数据阶段并发数
    drive_type = get_drive_type(folder_path)
    print("正在检测读取速度...", end="", flush=True)
    mbps = probe_disk_speed(tasks)
    workers = decide_workers(drive_type, mbps)
    drive_label = {
        "removable": "U盘/移动盘", "fixed": "本地硬盘", "remote": "网络盘",
        "cdrom": "光驱", "ramdisk": "内存盘", "noroot": "未知设备", "unknown": "未知设备",
    }.get(drive_type, "未知设备")
    speed_str = f"{mbps:.0f} MB/s" if mbps else "未知"
    print(f" 完成 (设备类型: {drive_label}, 读取速度: {speed_str})")

    print("-" * 80)
    print("扫描方式: 快速读取 + 逐个精确核对 (确保时长准确)")
    print("-" * 80)
    print(f"{'文件名':<35} | {'处理方式':<10} | {'时长':<10} | {'状态'}")
    print("-" * 80)

    total_seconds = 0.0
    valid_count = 0
    results_failed = []
    suspects = []
    start_time = time.time()

    # 阶段一: 快速元数据 (按自适应并发)
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {executor.submit(analyze_metadata, (task, ffmpeg_path)): task for task in tasks}
        for future in concurrent.futures.as_completed(future_map):
            file_path, duration, status = future.result()
            filename = os.path.basename(file_path)
            if duration > 0:
                total_seconds += duration
                valid_count += 1
                time_str = str(datetime.timedelta(seconds=int(duration)))
                with print_lock:
                    print(f"\u221a {filename:<35} | {'快速读取':<10} | {time_str:<10} | 成功")
            else:
                suspects.append(file_path)

    # 阶段二: 深度扫描 (串行, 任何盘都不并发, 避免抖动)
    if suspects:
        print("-" * 80)
        print(f"有 {len(suspects)} 个文件需要进一步核对, 正在逐个精确核对...")
        print("-" * 80)
        for file_path in suspects:
            filename = os.path.basename(file_path)
            timeout = compute_deep_timeout(_safe_size(file_path), mbps)
            duration, method = deep_scan(file_path, ffmpeg_path, timeout)
            if duration > 0:
                total_seconds += duration
                valid_count += 1
                time_str = str(datetime.timedelta(seconds=int(duration)))
                with print_lock:
                    print(f"\u2605 {filename:<35} | {'精确核对':<10} | {time_str:<10} | 成功")
            else:
                results_failed.append((filename, method))
                with print_lock:
                    print(f"X {filename:<35} | {'--':<10} | --:--:--   | {method}")

    elapsed = time.time() - start_time
    return total_seconds, valid_count, total_files, elapsed


# --- 报告输出 ---
def print_report(seconds, valid, total, cost_time, title="视频统计分析报告"):
    final_time = str(datetime.timedelta(seconds=int(seconds)))
    class_hours = seconds / 2700.0  # 45 分钟 / 课时

    print("\n" + "=" * 80)
    print(title)
    print("-" * 80)
    print(f"执行耗时 : {cost_time:.1f} 秒")
    print(f"处理进度 : {valid} / {total} 文件")
    print("-" * 80)
    print(f"累计时长 : {final_time}")
    print(f"折算课时 : {class_hours:.1f} 课时 (标准: 45分钟/课时)")
    print("=" * 80)


# --- 运行模式判定 (single / multi) ---
def is_multi_mode():
    """决定单目录还是多目录(多U盘)模式。
    优先级: 环境变量 SCAN_MODE > 可执行文件名是否含 'multi'。
    这样同一份源码可被 PyInstaller 打成 single / multi 两个 exe。"""
    env = os.environ.get("SCAN_MODE")
    if env:
        return env.strip().lower() == "multi"
    if getattr(sys, "frozen", False):
        name = os.path.basename(sys.executable).lower()
    else:
        name = os.path.basename(sys.argv[0]).lower()
    return "multi" in name


# --- 文件夹选择 (单目录 / 多目录) ---
def select_folders(multi=False):
    root = tk.Tk()
    root.withdraw()
    folders = []

    if not multi:
        f = filedialog.askdirectory(title="选择视频文件夹")
        if f:
            folders.append(os.path.abspath(f))
        root.destroy()
        return folders

    # 多目录模式: 逐个选择, 直到用户选择不再继续
    while True:
        f = filedialog.askdirectory(title=f"选择第 {len(folders) + 1} 个文件夹 / U盘")
        if f:
            f = os.path.abspath(f)
            if f not in folders:
                folders.append(f)
            else:
                messagebox.showinfo("提示", "该目录已添加, 已自动跳过重复项。")
        more = messagebox.askyesno(
            "继续添加",
            f"当前已选 {len(folders)} 个目录。\n是否继续添加下一个 (例如另一个 U盘)?",
        )
        if not more:
            break
    root.destroy()
    return folders


def main():
    if not os.path.exists(get_ffmpeg_path()):
        print("【错误】核心组件 ffmpeg 缺失。")
        input("按回车键退出...")
        sys.exit()

    multi = is_multi_mode()
    print(f"扫描范围: {'多个文件夹 (汇总统计)' if multi else '单个文件夹'}")

    folders = select_folders(multi)
    if not folders:
        input("\n未选择任何文件夹, 按回车键退出...")
        return

    grand_seconds = 0.0
    grand_valid = 0
    grand_total = 0
    grand_cost = 0.0

    for folder in folders:
        if multi:
            print("\n" + "#" * 80)
            print(f"开始扫描目录: {folder}")
            print("#" * 80)
        try:
            seconds, valid, total, cost_time = scan_folder(folder)
        except KeyboardInterrupt:
            print("\n已手动中断当前目录。")
            seconds, valid, total, cost_time = 0.0, 0, 0, 0.0

        grand_seconds += seconds
        grand_valid += valid
        grand_total += total
        grand_cost += cost_time

        # 多目录模式下先输出每个目录的小结
        if multi:
            print_report(seconds, valid, total, cost_time, title=f"目录统计: {folder}")

    # 汇总报告: 单目录直接出报告; 多目录(>1)再出一份总表
    if not multi:
        print_report(grand_seconds, grand_valid, grand_total, grand_cost)
    elif len(folders) > 1:
        print_report(
            grand_seconds, grand_valid, grand_total, grand_cost,
            title=f"全部 {len(folders)} 个目录汇总",
        )

    input("\n按回车键退出...")


if __name__ == "__main__":
    main()
