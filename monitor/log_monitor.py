#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""log_monitor.py — SFTP 傳輸 Log 監視分析工具（獨立、唯讀、僅依賴 stdlib）。

解析一個本地 log 目錄（由 sftp_transfer 本體先把遠端 log_remote_dir
`/fleet/wanhai_nssms_deploy/sftp_logs/` 下載下來），彙整成「每裝置最新更新狀態」，
同時輸出終端機彩色列表與自包含 HTML 報告，供用戶全覽與排查各設備的更新狀況。

用法：
  # A) 只分析（下載已由本體/排程完成）
    python monitor/log_monitor.py --log-dir logs --html

  # B) 一鍵即時監視（工具自行觸發本體下載遠端 sftp_logs 再分析）
    python monitor/log_monitor.py --sync-config config/xxx_download_settings.json \\
      --log-dir <該 config 的 local_path> --watch 60 --html
    
    python monitor/log_monitor.py --sync-config config/log_monitor_sync.json --log-dir fleet_logs --watch 60 --html

離開碼：一切正常 -> 0；有任何裝置處於異常（partial/aborted/incomplete）或過期 -> 1。
"""
from typing import Dict, List, Optional, Tuple

import argparse
import csv
import html
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

MAIN_SCRIPT = PROJECT_DIR / "main.py"
DEFAULT_LOG_DIR = PROJECT_DIR / "logs"
TS_FMT = "%Y-%m-%d %H:%M:%S"
# 本體 downloader._CSVFileHandler 寫出的欄位順序
CSV_COLUMNS = ["timestamp", "device_name", "version_info", "level", "message"]

# --- 解析錨點（對應 downloader.py / uploader.py 的訊息字串）------------------
_RE_START = re.compile(r"SFTP (下載|上傳)任務開始")
_RE_FILE_COUNT = re.compile(r"發現 (\d+) 個檔案")
_RE_SUMMARY = re.compile(
    r"(下載|上傳)任務結束(?:（\d+ 組）)?：成功 (\d+)，略過 (\d+)，失敗 (\d+)"
)
_RE_ABORT = re.compile(r"任務中止：(.+?)\s*={0,3}\s*$")
_RE_FAILED_LIST = re.compile(r"失敗清單：(.+)")
# vsl 必須容許底線：少數船隻用英文船名而非船號（如 WH_PORT_KELANG_EXPRESS），
# 而 device_name 本身就是用 _ 串接欄位。非貪婪是刻意的——vsl 是第一個欄位，
# 邊界要取第一個 _IPC-N_；comp 是自由格式（.+），本身就可能含 IPC-N，
# 貪婪會從尾端回溯而切錯欄位。船名不含 -，所以第一個 _IPC-N_ 必為真邊界。
_RE_DEVICE = re.compile(r"^(?P<vsl>[A-Za-z0-9_]+?)_(?P<ipc>IPC-\d+)_(?P<comp>.+)$")

# 狀態嚴重度（數字越大越該優先呈現）
_SEVERITY = {"aborted": 4, "incomplete": 3, "partial": 3, "stale": 2, "success": 0}


# ---------------------------------------------------------------------------
# 資料模型
# ---------------------------------------------------------------------------
@dataclass
class RunRecord:
    """單一 log 檔＝一次傳輸執行的彙整結果。"""

    path: Path
    device_name: str
    mode: str  # 'download' | 'upload'
    # 該次傳輸的程式碼版本（CSV 第三欄）。舊 log 與尚未宣告 VERSION.json 的專案是空字串 ——
    # 版本標記是後來才加的，船隊裡新舊 log 會並存很久，所以呈現端一律要能吃空值。
    # 給預設值也是為了向上相容：既有的 RunRecord(...) 呼叫端不必跟著改。
    version_info: str = ""
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    file_count: Optional[int] = None
    success: Optional[int] = None
    skipped: Optional[int] = None
    failed: Optional[int] = None
    status: str = "incomplete"  # success | partial | aborted | incomplete
    abort_reason: str = ""
    failed_list: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


@dataclass
class DeviceStatus:
    """依 device_name 彙整後的單一裝置狀態。"""

    device_name: str
    vessel: Optional[str]
    ipc: Optional[str]
    component: str
    latest: RunRecord
    run_count: int
    last_seen: Optional[datetime]
    is_stale: bool
    history: List[RunRecord] = field(default_factory=list)

    @property
    def display_status(self) -> str:
        """對外呈現用狀態：latest 正常但過期時回報 stale。"""
        if self.latest.status == "success" and self.is_stale:
            return "stale"
        return self.latest.status


# ---------------------------------------------------------------------------
# 解析
# ---------------------------------------------------------------------------
def parse_device_name(name: str) -> Tuple[Optional[str], Optional[str], str]:
    """把 device_name 拆成 (vessel, ipc, component)，best-effort。

    主樣式 `{vsl}_{ipc}_{component}`（如 CLINK_IPC-1_ecdis）；不符者
    vessel/ipc=None、component=整串（涵蓋 RADAR_UPLOADER、舊命名）。
    """
    m = _RE_DEVICE.match(name or "")
    if m:
        return m.group("vsl"), m.group("ipc"), m.group("comp")
    return None, None, (name or "unknown")


def _detect_mode(filename: str, start_direction: Optional[str]) -> str:
    """判定傳輸方向：優先看起始行內容，其次看檔名前綴。"""
    if start_direction == "上傳":
        return "upload"
    if start_direction == "下載":
        return "download"
    if filename.startswith("U_"):
        return "upload"
    return "download"


def parse_log_file(path) -> Optional[RunRecord]:
    """解析單一 CSV log 檔為 RunRecord；檔案損壞/非本工具格式回傳 None。"""
    path = Path(path)
    device_name = ""
    version_info = ""
    started_at = None
    ended_at = None
    file_count = None
    success = skipped = failed = None
    abort_reason = ""
    failed_list: List[str] = []
    warnings: List[str] = []
    errors: List[str] = []
    start_direction = None
    summary_direction = None
    saw_rows = False

    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.reader(fh)
            header = next(reader, None)
            if not header or header[:2] != CSV_COLUMNS[:2]:
                return None  # 非本工具的 CSV log
            for row in reader:
                if len(row) < 5:
                    continue
                ts_raw, dev, version, level, message = row[0], row[1], row[2], row[3], row[4]
                saw_rows = True
                if dev and not device_name:
                    device_name = dev
                # 同一份 log 的每列都是同一個值（_CSVFileHandler 的實例屬性），取第一個非空的即可
                if version and not version_info:
                    version_info = version
                ts = _parse_ts(ts_raw)
                if ts is not None:
                    if started_at is None:
                        started_at = ts
                    ended_at = ts

                m = _RE_START.search(message)
                if m:
                    start_direction = m.group(1)
                m = _RE_FILE_COUNT.search(message)
                if m:
                    file_count = int(m.group(1))
                m = _RE_SUMMARY.search(message)
                if m:
                    summary_direction = m.group(1)
                    success, skipped, failed = int(m.group(2)), int(m.group(3)), int(m.group(4))
                m = _RE_ABORT.search(message)
                if m:
                    abort_reason = m.group(1)
                m = _RE_FAILED_LIST.search(message)
                if m:
                    failed_list = [p.strip() for p in m.group(1).split(",") if p.strip()]

                if level == "WARNING":
                    warnings.append(message)
                elif level == "ERROR":
                    errors.append(message)
    except (OSError, csv.Error, UnicodeError):
        return None

    if not saw_rows:
        return None

    if abort_reason:
        status = "aborted"
    elif success is not None:
        status = "partial" if failed else "success"
    else:
        status = "incomplete"

    if not device_name:
        # 退而求其次：由檔名還原（去掉 D_/U_ 前綴與尾端時間戳記）
        device_name = _device_from_filename(path.name)

    direction = summary_direction or start_direction
    return RunRecord(
        path=path,
        device_name=device_name,
        version_info=version_info,
        mode=_detect_mode(path.name, direction),
        started_at=started_at,
        ended_at=ended_at,
        file_count=file_count,
        success=success,
        skipped=skipped,
        failed=failed,
        status=status,
        abort_reason=abort_reason,
        failed_list=failed_list,
        warnings=warnings,
        errors=errors,
    )


_ROW_LIMIT = 5000


def read_log_rows(path, max_rows: int = _ROW_LIMIT) -> Tuple[List[List[str]], bool]:
    """讀單一 log CSV 的原始列，供 TUI 檢視「那一筆到底寫了什麼」。

    parse_log_file 只留彙整後的純量與 ERROR/WARNING 訊息，原始列全丟；要逐行回看
    就得依 RunRecord.path 現場重讀。回傳 (rows, truncated)，每列固定補齊成
    CSV_COLUMNS 的 5 欄。

    讀不到／格式不符／空檔一律回傳空列表而非拋錯，讓 curses 端不必包 try。
    """
    rows: List[List[str]] = []
    truncated = False
    width = len(CSV_COLUMNS)
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.reader(fh)
            header = next(reader, None)
            if not header or header[:2] != CSV_COLUMNS[:2]:
                return [], False  # 非本工具的 CSV log
            for row in reader:
                if len(rows) >= max_rows:
                    truncated = True
                    break
                # 短列補齊而非略過：檢視原始資料時不該偷偷藏列。
                rows.append((row + [""] * width)[:width])
    except (OSError, csv.Error, UnicodeError):
        return [], False
    return rows, truncated


def _parse_ts(value: str) -> Optional[datetime]:
    try:
        return datetime.strptime(value.strip(), TS_FMT)
    except (ValueError, AttributeError):
        return None


_RE_FILENAME = re.compile(r"^(?:[DU]_)?(?P<dev>.+?)_\d{8}_\d{6}$")


def _device_from_filename(filename: str) -> str:
    stem = Path(filename).stem
    m = _RE_FILENAME.match(stem)
    if m:
        return m.group("dev")
    return stem or "unknown"


def collect_logs(log_dir, mode: str = "all") -> List[RunRecord]:
    """遞迴掃描 log_dir 下所有 *.csv，解析成 RunRecord 清單並依 mode 過濾。

    用 rglob 是為了涵蓋遠端 sftp_logs 下載後的巢狀結構
    （download/{vsl}/{ipc}/{component}/*.csv）；平面目錄（如既有 logs/）同樣適用。
    """
    log_dir = Path(log_dir)
    records: List[RunRecord] = []
    for path in sorted(log_dir.rglob("*.csv")):
        rec = parse_log_file(path)
        if rec is None:
            continue
        if mode != "all" and rec.mode != mode:
            continue
        records.append(rec)
    return records


def aggregate_by_device(
    records: List[RunRecord], now: datetime, stale_hours: float
) -> List[DeviceStatus]:
    """依 (device_name, mode) 分組，取最新一次執行為代表並計算過期與排序。

    以 (device_name, mode) 而非單純 device_name 為鍵：同名 project 的上傳與下載
    共用同一 device_name（如 ecdis_download / ecdis_upload 皆為 {vsl}_{ipc}_ecdis），
    合併會遺失其中一個方向；分開才能各自呈現與排查。
    """
    groups: Dict[Tuple[str, str], List[RunRecord]] = {}
    for rec in records:
        groups.setdefault((rec.device_name, rec.mode), []).append(rec)

    stale_delta = timedelta(hours=stale_hours)
    devices: List[DeviceStatus] = []
    for (device_name, _mode), recs in groups.items():
        recs_sorted = sorted(
            recs, key=lambda r: r.started_at or datetime.min, reverse=True
        )
        latest = recs_sorted[0]
        last_seen = latest.started_at
        is_stale = last_seen is None or (now - last_seen) > stale_delta
        vessel, ipc, component = parse_device_name(device_name)
        devices.append(
            DeviceStatus(
                device_name=device_name,
                vessel=vessel,
                ipc=ipc,
                component=component,
                latest=latest,
                run_count=len(recs),
                last_seen=last_seen,
                is_stale=is_stale,
                history=recs_sorted,
            )
        )

    devices.sort(
        key=lambda d: (
            -_SEVERITY.get(d.display_status, 0),
            d.last_seen or datetime.min,
            d.device_name,
        )
    )
    return devices


# ---------------------------------------------------------------------------
# 階層分群（方向 > vessel > IPC > project）與 rollup
# ---------------------------------------------------------------------------
_UNCLASSIFIED_VESSEL = "（未分類）"
_UNKNOWN_IPC = "—"
_MODE_LABEL = {"download": "↓ 下載", "upload": "↑ 上傳"}
_MODE_ORDER = {"download": 0, "upload": 1}


@dataclass
class GroupSummary:
    """一個群組（mode/vessel/ipc）的 rollup 統計。"""

    total: int = 0
    ok: int = 0
    stale: int = 0
    bad: int = 0
    worst: str = "success"  # 群內 display_status 最嚴重者


@dataclass
class IpcGroup:
    name: str
    summary: GroupSummary
    devices: List[DeviceStatus]


@dataclass
class VesselGroup:
    name: str
    summary: GroupSummary
    ipcs: List[IpcGroup]


@dataclass
class ModeGroup:
    mode: str
    summary: GroupSummary
    vessels: List[VesselGroup]


def _summarize(devices: List[DeviceStatus]) -> GroupSummary:
    s = GroupSummary(total=len(devices))
    worst_sev = -1
    for d in devices:
        st = d.display_status
        if st == "success":
            s.ok += 1
        elif st == "stale":
            s.stale += 1
        else:
            s.bad += 1
        sev = _SEVERITY.get(st, 0)
        if sev > worst_sev:
            worst_sev, s.worst = sev, st
    return s


def group_is_problem(summary: GroupSummary) -> bool:
    """群組是否含需關注的裝置（過期 / 失敗 / 中止 / 未完成）→ 預設展開。"""
    return bool(summary.bad or summary.stale)


def _group_sort_key(summary: GroupSummary, name: str):
    # 未分類 / 未知 IPC 置底；其餘先依 worst 嚴重度 desc、再名稱
    is_bucket = name in (_UNCLASSIFIED_VESSEL, _UNKNOWN_IPC)
    return (is_bucket, -_SEVERITY.get(summary.worst, 0), name)


def build_tree(devices: List[DeviceStatus]) -> List[ModeGroup]:
    """把扁平 DeviceStatus 依 mode → vessel → ipc 建成階層樹並由下而上算 rollup。"""
    tree: Dict[str, Dict[str, Dict[str, List[DeviceStatus]]]] = {}
    for d in devices:
        mode = d.latest.mode
        vessel = d.vessel or _UNCLASSIFIED_VESSEL
        ipc = d.ipc or _UNKNOWN_IPC
        tree.setdefault(mode, {}).setdefault(vessel, {}).setdefault(ipc, []).append(d)

    mode_groups: List[ModeGroup] = []
    for mode in sorted(tree, key=lambda m: _MODE_ORDER.get(m, 9)):
        vessels: List[VesselGroup] = []
        mode_devices: List[DeviceStatus] = []
        for vessel, ipc_map in tree[mode].items():
            ipcs: List[IpcGroup] = []
            vessel_devices: List[DeviceStatus] = []
            for ipc, devs in ipc_map.items():
                devs_sorted = sorted(
                    devs, key=lambda d: (-_SEVERITY.get(d.display_status, 0), d.component)
                )
                ipcs.append(
                    IpcGroup(name=ipc, summary=_summarize(devs_sorted), devices=devs_sorted)
                )
                vessel_devices.extend(devs_sorted)
            ipcs.sort(key=lambda g: _group_sort_key(g.summary, g.name))
            vessels.append(
                VesselGroup(name=vessel, summary=_summarize(vessel_devices), ipcs=ipcs)
            )
            mode_devices.extend(vessel_devices)
        vessels.sort(key=lambda g: _group_sort_key(g.summary, g.name))
        mode_groups.append(
            ModeGroup(mode=mode, summary=_summarize(mode_devices), vessels=vessels)
        )
    return mode_groups


# ---------------------------------------------------------------------------
# 選配：觸發本體下載遠端 log
# ---------------------------------------------------------------------------
def _run_with_output_callback(command, cwd, timeout, output_callback):
    """執行子行程並逐行回傳合併後的 stdout/stderr，同時保留 timeout。"""
    import queue
    import threading
    import time

    proc = subprocess.Popen(
        command,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        # text= 是 3.7 才有的別名;Bionic 的 venv 是 3.6,只認 universal_newlines=。
        # 有給 encoding= 時 universal_newlines 的行為與 text=True 相同。
        universal_newlines=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    pending = queue.Queue()
    finished = object()

    def read_output():
        try:
            for line in proc.stdout:
                pending.put(line)
        finally:
            pending.put(finished)

    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    deadline = None if timeout is None else time.monotonic() + timeout
    reader_done = False
    try:
        while not (reader_done and proc.poll() is not None):
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, timeout)
                wait = min(0.1, remaining)
            else:
                wait = 0.1
            try:
                item = pending.get(timeout=wait)
            except queue.Empty:
                continue
            if item is finished:
                reader_done = True
            else:
                output_callback(item.rstrip("\r\n"))
        return subprocess.CompletedProcess(command, proc.wait())
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        reader.join(timeout=1)


def sync_logs(
    sync_config,
    timeout: Optional[float] = 600,
    quiet: bool = False,
    output_callback=None,
) -> bool:
    """以子行程呼叫 sftp_transfer 本體下載遠端 log（沿用 run_all_downloads.py 模式）。

    失敗僅回傳 False、不拋例外，讓分析仍能沿用既有本地資料。
    quiet=True 時不讓子行程或警告寫入終端機，避免破壞 curses/TUI 畫面。
    output_callback 若有提供，stdout/stderr 會合併後逐行傳入 callback。
    """
    sync_config = Path(sync_config)
    if not sync_config.exists():
        if not quiet:
            print(f"[WARN] 同步設定檔不存在，略過下載：{sync_config}", file=sys.stderr)
        return False
    command = [
        sys.executable,
        str(MAIN_SCRIPT),
        "--cli",
        "--mode",
        "download",
        "--config",
        str(sync_config),
    ]
    try:
        if output_callback is not None:
            proc = _run_with_output_callback(
                command, str(PROJECT_DIR), timeout, output_callback
            )
        else:
            output_kwargs = (
                {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
                if quiet
                else {}
            )
            proc = subprocess.run(
                command,
                cwd=str(PROJECT_DIR),
                timeout=timeout,
                **output_kwargs,
            )
    except (subprocess.SubprocessError, OSError) as exc:
        if not quiet:
            print(f"[WARN] 觸發下載失敗，改用既有本地 log：{exc}", file=sys.stderr)
        return False
    if proc.returncode != 0:
        if not quiet:
            print(
                f"[WARN] 本體下載回傳非零（{proc.returncode}），改用既有本地 log",
                file=sys.stderr,
            )
        return False
    return True


# ---------------------------------------------------------------------------
# 呈現：CLI
# ---------------------------------------------------------------------------
_STATUS_LABEL = {
    "success": "正常",
    "partial": "部分失敗",
    "aborted": "中止",
    "incomplete": "未完成",
    "stale": "過期",
}
_STATUS_COLOR = {  # ANSI code
    "success": "32",
    "partial": "31",
    "aborted": "31",
    "incomplete": "33",
    "stale": "33",
}


def _color(code: str, text: str, use_color: bool) -> str:
    return f"\033[{code}m{text}\033[0m" if use_color else text


def _humanize_age(last_seen: Optional[datetime], now: datetime) -> str:
    if last_seen is None:
        return "—"
    secs = max(0, int((now - last_seen).total_seconds()))
    if secs < 3600:
        return f"{secs // 60}分鐘前"
    if secs < 86400:
        return f"{secs // 3600}小時前"
    return f"{secs // 86400}天前"


def _counts_str(rec: RunRecord) -> str:
    if rec.success is None:
        return "—"
    return f"{rec.success}/{rec.skipped}/{rec.failed}"


def _detail_str(dev: DeviceStatus) -> str:
    rec = dev.latest
    if rec.abort_reason:
        return f"中止：{rec.abort_reason}"
    if rec.errors:
        return rec.errors[-1]
    if rec.status == "success" and dev.is_stale:
        return "逾期未回報"
    if rec.warnings:
        return rec.warnings[-1]
    return ""


def device_detail_lines(dev: DeviceStatus) -> List[str]:
    """該裝置最新一次執行的多行純文字明細（與 HTML 明細同源，供 TUI 彈窗/其他純文字用途）。"""
    rec = dev.latest
    lines: List[str] = []
    lines.append(f"裝置：{dev.device_name}（{_MODE_LABEL.get(rec.mode, rec.mode)}）")
    lines.append(
        f"狀態：{_STATUS_LABEL.get(dev.display_status, dev.display_status)}"
        f"｜最後執行：{rec.started_at.strftime(TS_FMT) if rec.started_at else '—'}"
        f"｜檔案：{'—' if rec.file_count is None else rec.file_count}"
        f"｜成功/略/失：{_counts_str(rec)}"
    )
    if rec.abort_reason:
        lines.append(f"中止原因：{rec.abort_reason}")
    if rec.failed_list:
        lines.append("失敗清單：" + ", ".join(rec.failed_list))
    for e in rec.errors[-5:]:
        lines.append(f"ERROR：{e}")
    for w in rec.warnings[-5:]:
        lines.append(f"WARNING：{w}")
    lines.append(f"來源檔：{rec.path.name}｜歷史執行 {dev.run_count} 次")
    if len(dev.history) > 1:
        hist = "、".join(
            (r.started_at.strftime("%m-%d %H:%M") if r.started_at else "—")
            + f"（{_STATUS_LABEL.get(r.status, r.status)}）"
            for r in dev.history[:6]
        )
        lines.append(f"近期：{hist}")
    return lines


def render_cli(devices: List[DeviceStatus], now: datetime, use_color: bool = True) -> str:
    """組出終端機彩色列表（回傳字串，方便測試）。"""
    lines: List[str] = []
    total = len(devices)
    ok = sum(1 for d in devices if d.display_status == "success")
    stale = sum(1 for d in devices if d.display_status == "stale")
    bad = total - ok - stale

    header = (
        f"SFTP Log 監視 — 產生於 {now.strftime(TS_FMT)}｜"
        f"裝置 {total}｜"
        + _color("32", f"正常 {ok}", use_color)
        + "｜"
        + _color("31", f"異常 {bad}", use_color)
        + "｜"
        + _color("33", f"過期 {stale}", use_color)
    )
    lines.append(header)
    lines.append("-" * 96)

    if not devices:
        lines.append("（無資料：找不到任何可解析的 log）")
        return "\n".join(lines)

    row_fmt = "{light}  {dev:<28} {mode:<4} {last:<18} {files:>6} {counts:>10}  {age:<10} {detail}"
    lines.append(
        "  狀態  {dev:<28} {mode:<4} {last:<18} {files:>6} {counts:>10}  {age:<10} 摘要".format(
            dev="裝置", mode="方向", last="最後執行", files="檔案", counts="成功/略/失", age="距今"
        )
    )
    for d in devices:
        st = d.display_status
        light = _color(_STATUS_COLOR.get(st, "0"), "●", use_color)
        rec = d.latest
        lines.append(
            row_fmt.format(
                light=light,
                dev=(d.device_name[:28]),
                mode="↓" if rec.mode == "download" else "↑",
                last=rec.started_at.strftime(TS_FMT) if rec.started_at else "—",
                files=("—" if rec.file_count is None else str(rec.file_count)),
                counts=_counts_str(rec),
                age=_humanize_age(d.last_seen, now),
                detail=_detail_str(d)[:60],
            )
        )
    return "\n".join(lines)


def _summary_badge(s: GroupSummary, use_color: bool) -> str:
    parts = [f"裝置 {s.total}", _color("32", f"正常 {s.ok}", use_color)]
    if s.stale:
        parts.append(_color("33", f"過期 {s.stale}", use_color))
    if s.bad:
        parts.append(_color("31", f"異常 {s.bad}", use_color))
    return "｜".join(parts)


def _dot(status: str, use_color: bool) -> str:
    return _color(_STATUS_COLOR.get(status, "0"), "●", use_color)


def render_cli_grouped(
    mode_groups: List[ModeGroup], now: datetime, use_color: bool = True, expand: str = "auto"
) -> str:
    """階層分群的終端機輸出：方向 > vessel > IPC > project。

    expand: 'auto'（正常收合、異常展開）/ 'all'（全展開）/ 'none'（全收合）。
    收合的群組只印摘要行、不列出下層。
    """
    lines: List[str] = []
    total = sum(m.summary.total for m in mode_groups)
    ok = sum(m.summary.ok for m in mode_groups)
    stale = sum(m.summary.stale for m in mode_groups)
    bad = sum(m.summary.bad for m in mode_groups)
    lines.append(
        f"SFTP Log 監視 — 產生於 {now.strftime(TS_FMT)}｜裝置 {total}｜"
        + _color("32", f"正常 {ok}", use_color)
        + "｜"
        + _color("31", f"異常 {bad}", use_color)
        + "｜"
        + _color("33", f"過期 {stale}", use_color)
    )
    lines.append("=" * 96)
    if total == 0:
        lines.append("（無資料：找不到任何可解析的 log）")
        return "\n".join(lines)

    def is_expanded(summary: GroupSummary) -> bool:
        if expand == "all":
            return True
        if expand == "none":
            return False
        return group_is_problem(summary)

    for m in mode_groups:
        lines.append(
            f"{_dot(m.summary.worst, use_color)} {_MODE_LABEL.get(m.mode, m.mode)}"
            f"  [{_summary_badge(m.summary, use_color)}]"
        )
        for v in m.vessels:
            v_exp = is_expanded(v.summary)
            lines.append(
                f"  {'▼' if v_exp else '▶'} {_dot(v.summary.worst, use_color)} {v.name}"
                f"  [{_summary_badge(v.summary, use_color)}]"
            )
            if not v_exp:
                continue
            for ip in v.ipcs:
                ip_exp = is_expanded(ip.summary)
                lines.append(
                    f"    {'▼' if ip_exp else '▶'} {_dot(ip.summary.worst, use_color)} {ip.name}"
                    f"  [{_summary_badge(ip.summary, use_color)}]"
                )
                if not ip_exp:
                    continue
                for d in ip.devices:
                    rec = d.latest
                    lines.append(
                        "      {light} {comp:<22} {last:<18} {files:>6} {counts:>10}"
                        "  {age:<10} {detail}".format(
                            light=_dot(d.display_status, use_color),
                            comp=d.component[:22],
                            last=rec.started_at.strftime(TS_FMT) if rec.started_at else "—",
                            files=("—" if rec.file_count is None else str(rec.file_count)),
                            counts=_counts_str(rec),
                            age=_humanize_age(d.last_seen, now),
                            detail=_detail_str(d)[:56],
                        )
                    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 呈現：HTML（單一自包含檔，內嵌 CSS/JS，不引外部資源）
# ---------------------------------------------------------------------------
_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SFTP Log 監視</title>
<style>
:root{--bg:#f7f8fa;--fg:#1c1e21;--muted:#6b7280;--card:#fff;--border:#e3e6ea;
--ok:#188038;--warn:#b8860b;--bad:#d93025;--chiptext:#fff;--hover:#eef1f5;}
@media (prefers-color-scheme:dark){:root{--bg:#16181c;--fg:#e6e8eb;--muted:#9aa0a6;
--card:#1f2226;--border:#2c3036;--hover:#262a30;}}
*{box-sizing:border-box;}
body{margin:0;padding:24px;background:var(--bg);color:var(--fg);
font-family:-apple-system,"Noto Sans TC","Segoe UI",Roboto,sans-serif;}
h1{font-size:20px;margin:0 0 4px;}
.meta{color:var(--muted);font-size:13px;margin-bottom:16px;}
.cards{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:12px;}
.card{background:var(--card);border:1px solid var(--border);border-radius:10px;padding:12px 18px;min-width:92px;}
.card .n{font-size:24px;font-weight:700;}
.card .l{font-size:12px;color:var(--muted);}
.controls{position:sticky;top:0;z-index:5;background:var(--bg);padding:8px 0;margin-bottom:8px;
display:flex;gap:8px;flex-wrap:wrap;align-items:center;}
input,select{padding:7px 10px;border:1px solid var(--border);border-radius:8px;background:var(--card);color:var(--fg);font-size:14px;}
button{padding:7px 12px;border:1px solid var(--border);border-radius:8px;background:var(--card);color:var(--fg);font-size:13px;cursor:pointer;}
button:hover{background:var(--hover);}
label.cb{font-size:13px;display:flex;align-items:center;gap:4px;cursor:pointer;}
.mode-sec{margin-bottom:18px;}
.mode-h{font-size:16px;font-weight:700;margin:16px 0 6px;display:flex;align-items:center;gap:10px;}
details{border:1px solid var(--border);border-radius:8px;margin:6px 0;background:var(--card);overflow:hidden;}
details.ipc{margin:6px 8px 8px;}
summary{cursor:pointer;padding:8px 12px;font-weight:600;list-style:none;display:flex;align-items:center;gap:8px;}
summary::-webkit-details-marker{display:none;}
.caret{display:inline-block;transition:transform .12s;color:var(--muted);}
details[open]>summary .caret{transform:rotate(90deg);}
.badge{font-size:12px;color:var(--muted);font-weight:400;}
.badge .ok{color:var(--ok);} .badge .warn{color:var(--warn);} .badge .bad{color:var(--bad);}
.match{font-size:12px;color:var(--warn);font-weight:400;margin-left:4px;}
.dot{display:inline-block;width:9px;height:9px;border-radius:50%;flex:none;}
table{border-collapse:collapse;width:100%;font-size:14px;}
th,td{padding:7px 12px;text-align:left;border-bottom:1px solid var(--border);white-space:nowrap;}
th{color:var(--muted);font-weight:600;font-size:12px;}
tr.lf{cursor:pointer;} tr.lf:hover{background:var(--hover);}
.chip{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;color:var(--chiptext);}
.s-success{background:var(--ok);} .s-partial,.s-aborted{background:var(--bad);}
.s-incomplete,.s-stale{background:var(--warn);}
.detail{display:none;} .detail.open{display:table-row;}
.detail td{white-space:normal;color:var(--muted);font-size:13px;background:var(--bg);}
.mono{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;}
.err{color:var(--bad);} .warn{color:var(--warn);} .muted{color:var(--muted);}
.empty{color:var(--muted);padding:12px;}
</style>
</head>
<body>
<h1>SFTP 傳輸 Log 監視</h1>
<div class="meta">產生於 @@GENERATED_AT@@｜來源目錄 <span class="mono">@@LOG_DIR@@</span>｜過期門檻 @@STALE_HOURS@@ 小時</div>
<div class="cards">
  <div class="card"><div class="n">@@TOTAL@@</div><div class="l">裝置</div></div>
  <div class="card"><div class="n" style="color:var(--ok)">@@OK@@</div><div class="l">正常</div></div>
  <div class="card"><div class="n" style="color:var(--bad)">@@BAD@@</div><div class="l">異常</div></div>
  <div class="card"><div class="n" style="color:var(--warn)">@@STALE@@</div><div class="l">過期</div></div>
</div>
<div class="controls">
  <input id="q" placeholder="搜尋…">
  <select id="fm"><option value="">全部方向</option><option value="download">↓ 下載</option><option value="upload">↑ 上傳</option></select>
  <select id="fv"><option value="">全部船隻</option>@@VESSEL_OPTS@@</select>
  <select id="fi"><option value="">全部 IPC</option>@@IPC_OPTS@@</select>
  <select id="fc"><option value="">全部元件</option>@@COMP_OPTS@@</select>
  <select id="fs"><option value="">全部狀態</option><option value="success">正常</option><option value="stale">過期</option><option value="partial">部分失敗</option><option value="aborted">中止</option><option value="incomplete">未完成</option></select>
  <label class="cb"><input type="checkbox" id="ob">只看異常</label>
  <button onclick="expandAll(true)">全部展開</button>
  <button onclick="expandAll(false)">全部收合</button>
</div>
@@BODY@@
<script>
function val(id){var e=document.getElementById(id);return e?e.value:'';}
function vis(node){return Array.prototype.filter.call(node.querySelectorAll('tr.lf'),function(r){return r.style.display!=='none';}).length;}
function setMatch(dt){var s=dt.querySelector(':scope > summary .match');if(!s)return;var v=vis(dt),t=dt.querySelectorAll('tr.lf').length;s.textContent=(v===t)?'':'（符合 '+v+'）';}
function applyFilters(){
  var q=val('q').toLowerCase(),fm=val('fm'),fv=val('fv'),fi=val('fi'),fc=val('fc'),fs=val('fs');
  var ob=document.getElementById('ob').checked;
  document.querySelectorAll('tr.lf').forEach(function(tr){
    var d=tr.dataset,show=true;
    if(fm&&d.mode!==fm)show=false;
    if(fv&&d.vessel!==fv)show=false;
    if(fi&&d.ipc!==fi)show=false;
    if(fc&&d.component!==fc)show=false;
    if(fs&&d.status!==fs)show=false;
    if(ob&&d.status==='success')show=false;
    if(q&&tr.textContent.toLowerCase().indexOf(q)<0)show=false;
    tr.style.display=show?'':'none';
    var det=tr.nextElementSibling;
    if(det&&det.classList.contains('detail'))det.classList.remove('open');
  });
  document.querySelectorAll('details.ipc').forEach(function(dt){dt.style.display=vis(dt)?'':'none';setMatch(dt);});
  document.querySelectorAll('details.vessel').forEach(function(dt){dt.style.display=vis(dt)?'':'none';setMatch(dt);});
  document.querySelectorAll('.mode-sec').forEach(function(sec){sec.style.display=vis(sec)?'':'none';});
}
function expandAll(o){document.querySelectorAll('details').forEach(function(d){if(d.style.display!=='none')d.open=o;});}
document.addEventListener('click',function(e){
  if(!e.target.closest)return;
  var tr=e.target.closest('tr.lf');
  if(tr){var det=tr.nextElementSibling;if(det&&det.classList.contains('detail'))det.classList.toggle('open');}
});
['q','fm','fv','fi','fc','fs'].forEach(function(id){var el=document.getElementById(id);if(el){el.addEventListener('input',applyFilters);el.addEventListener('change',applyFilters);}});
document.getElementById('ob').addEventListener('change',applyFilters);
</script>
</body>
</html>
"""


def _esc(s) -> str:
    return html.escape("" if s is None else str(s))


def _detail_html(dev: DeviceStatus) -> str:
    rec = dev.latest
    parts: List[str] = []
    if rec.abort_reason:
        parts.append(f'<div class="err">中止原因：{_esc(rec.abort_reason)}</div>')
    if rec.failed_list:
        parts.append(
            '<div class="err">失敗清單：<span class="mono">'
            + _esc(", ".join(rec.failed_list))
            + "</span></div>"
        )
    for e in rec.errors[-5:]:
        parts.append(f'<div class="err mono">ERROR：{_esc(e)}</div>')
    for w in rec.warnings[-5:]:
        parts.append(f'<div class="warn mono">WARNING：{_esc(w)}</div>')
    parts.append(
        f'<div class="muted">來源檔：<span class="mono">{_esc(rec.path.name)}</span>'
        f"｜歷史執行 {dev.run_count} 次</div>"
    )
    if len(dev.history) > 1:
        hist = "、".join(
            (r.started_at.strftime("%m-%d %H:%M") if r.started_at else "—")
            + f"（{_STATUS_LABEL.get(r.status, r.status)}）"
            for r in dev.history[:6]
        )
        parts.append(f'<div class="muted">近期：{_esc(hist)}</div>')
    return "".join(parts) or '<div class="muted">無額外明細</div>'


def _html_badges(summary: GroupSummary) -> str:
    parts = [f"裝置 {summary.total}", f'<span class="ok">正常 {summary.ok}</span>']
    if summary.stale:
        parts.append(f'<span class="warn">過期 {summary.stale}</span>')
    if summary.bad:
        parts.append(f'<span class="bad">異常 {summary.bad}</span>')
    return '<span class="badge">' + " · ".join(parts) + "</span>"


def _leaf_row(d: DeviceStatus, generated_at: datetime) -> str:
    rec = d.latest
    st = d.display_status
    vessel = d.vessel or _UNCLASSIFIED_VESSEL
    ipc = d.ipc or _UNKNOWN_IPC
    row = (
        "<tr class='lf' data-mode='{mode}' data-vessel='{v}' data-ipc='{i}' "
        "data-component='{c}' data-status='{st}'>"
        "<td><span class='chip s-{st}'>{label}</span></td>"
        "<td>{comp}</td><td>{version}</td><td>{last}</td><td>{files}</td><td>{counts}</td>"
        "<td>{age}</td><td>{detail}</td></tr>"
    ).format(
        mode=_esc(rec.mode),
        v=_esc(vessel),
        i=_esc(ipc),
        c=_esc(d.component),
        st=st,
        label=_STATUS_LABEL.get(st, st),
        comp=_esc(d.component),
        version=_esc(rec.version_info or "—"),   # 沒宣告版號的專案是空字串 → 顯示破折號
        last=_esc(rec.started_at.strftime(TS_FMT) if rec.started_at else "—"),
        files=("—" if rec.file_count is None else rec.file_count),
        counts=_esc(_counts_str(rec)),
        age=_esc(_humanize_age(d.last_seen, generated_at)),
        detail=_esc(_detail_str(d)),
    )
    return row + f"<tr class='detail'><td colspan='8'>{_detail_html(d)}</td></tr>"


def render_html(
    devices: List[DeviceStatus],
    out_path,
    generated_at: datetime,
    log_dir: str = "",
    stale_hours: float = 72,
) -> Path:
    """產生單一自包含 HTML 報告（方向>vessel>IPC>project 巢狀可折疊 + 多維過濾）。"""
    out_path = Path(out_path)
    total = len(devices)
    ok = sum(1 for d in devices if d.display_status == "success")
    stale = sum(1 for d in devices if d.display_status == "stale")
    bad = total - ok - stale
    tree = build_tree(devices)

    def _opts(vals):
        return "".join(f"<option value='{_esc(v)}'>{_esc(v)}</option>" for v in sorted(vals))

    vessel_opts = _opts({d.vessel or _UNCLASSIFIED_VESSEL for d in devices})
    ipc_opts = _opts({d.ipc or _UNKNOWN_IPC for d in devices})
    comp_opts = _opts({d.component for d in devices})

    parts: List[str] = []
    for m in tree:
        parts.append(
            f'<section class="mode-sec"><div class="mode-h">'
            f'<span class="dot s-{m.summary.worst}"></span>'
            f"{_esc(_MODE_LABEL.get(m.mode, m.mode))}{_html_badges(m.summary)}</div>"
        )
        for v in m.vessels:
            vopen = " open" if group_is_problem(v.summary) else ""
            parts.append(
                f'<details class="vessel" data-vessel="{_esc(v.name)}"{vopen}>'
                f'<summary><span class="caret">▶</span>'
                f'<span class="dot s-{v.summary.worst}"></span>{_esc(v.name)}'
                f'{_html_badges(v.summary)}<span class="match"></span></summary>'
            )
            for ip in v.ipcs:
                iopen = " open" if group_is_problem(ip.summary) else ""
                parts.append(
                    f'<details class="ipc" data-ipc="{_esc(ip.name)}"{iopen}>'
                    f'<summary><span class="caret">▶</span>'
                    f'<span class="dot s-{ip.summary.worst}"></span>{_esc(ip.name)}'
                    f'{_html_badges(ip.summary)}<span class="match"></span></summary>'
                    "<table><thead><tr>"
                    "<th>狀態</th><th>元件</th><th>版本</th><th>最後執行</th><th>檔案</th>"
                    "<th>成功/略/失</th><th>距今</th><th>摘要</th></tr></thead><tbody>"
                )
                for d in ip.devices:
                    parts.append(_leaf_row(d, generated_at))
                parts.append("</tbody></table></details>")
            parts.append("</details>")
        parts.append("</section>")
    body = (
        "".join(parts)
        if total
        else '<div class="empty">（無資料：找不到任何可解析的 log）</div>'
    )

    doc = _HTML_TEMPLATE
    for token, value in [
        ("@@GENERATED_AT@@", _esc(generated_at.strftime(TS_FMT))),
        ("@@LOG_DIR@@", _esc(log_dir)),
        ("@@STALE_HOURS@@", _esc(stale_hours)),
        ("@@TOTAL@@", str(total)),
        ("@@OK@@", str(ok)),
        ("@@BAD@@", str(bad)),
        ("@@STALE@@", str(stale)),
        ("@@VESSEL_OPTS@@", vessel_opts),
        ("@@IPC_OPTS@@", ipc_opts),
        ("@@COMP_OPTS@@", comp_opts),
        ("@@BODY@@", body),
    ]:
        doc = doc.replace(token, value)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(doc, encoding="utf-8")
    return out_path


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="SFTP 傳輸 Log 監視分析工具（解析本地 log 目錄，輸出 CLI 列表與 HTML 報告）"
    )
    p.add_argument("--log-dir", default=str(DEFAULT_LOG_DIR), help="要分析的本地 log 目錄（預設 ./logs）")
    p.add_argument("--mode", choices=["download", "upload", "all"], default="all", help="只看某方向")
    p.add_argument("--stale-hours", type=float, default=72, help="逾期告警門檻（小時，預設 72）")
    p.add_argument(
        "--html",
        nargs="?",
        const="__auto__",
        default=None,
        metavar="PATH",
        help="另存 HTML 報告；不接路徑則覆寫 <log-dir>/log_monitor.html；"
             "watch/TUI 每次刷新時同步覆寫",
    )
    p.add_argument("--sync-config", default=None, help="分析前先用此 download 設定檔觸發本體下載遠端 log")
    p.add_argument("--watch", type=float, default=None, help="每 N 秒刷新（搭配 --sync-config 才會重新下載）")
    p.add_argument("--vessel", default=None, help="只顯示指定船名（vessel）")
    p.add_argument("--ipc", default=None, help="只顯示指定 IPC")
    p.add_argument("--component", default=None, help="只顯示指定元件（project/component）")
    p.add_argument(
        "--status",
        choices=["ok", "stale", "problem", "all"],
        default="all",
        help="只顯示某狀態：ok=正常、stale=過期、problem=失敗/中止/未完成",
    )
    p.add_argument("--flat", action="store_true", help="改用舊的平面表格（不分群）")
    p.add_argument(
        "--tui",
        action="store_true",
        help="互動式終端機介面（curses）：可用鍵盤/滑鼠展開、收合、搜尋、過濾及看明細；非 TTY 自動退回靜態輸出",
    )
    grp = p.add_mutually_exclusive_group()
    grp.add_argument("--expand-all", action="store_true", help="分群時全部展開")
    grp.add_argument("--collapsed", action="store_true", help="分群時全部收合")
    p.add_argument("--no-color", action="store_true", help="停用 ANSI 顏色")
    return p


def _status_matches(dev: DeviceStatus, status: str) -> bool:
    if status == "all":
        return True
    st = dev.display_status
    if status == "ok":
        return st == "success"
    if status == "stale":
        return st == "stale"
    if status == "problem":
        return st not in ("success", "stale")
    return True


def _apply_filters(devices, vessel=None, ipc=None, component=None, status="all"):
    if vessel:
        devices = [d for d in devices if d.vessel == vessel]
    if ipc:
        devices = [d for d in devices if d.ipc == ipc]
    if component:
        devices = [d for d in devices if d.component == component]
    if status and status != "all":
        devices = [d for d in devices if _status_matches(d, status)]
    return devices


def write_html_report(devices: list, now: datetime, log_dir,
                      stale_hours: float, html_path) -> Optional[Path]:
    """依 `--html` 設定寫出目前快照，供靜態、`--watch` 與 TUI 共用。

    TUI 不走 _run_once，卻必須寫到同一個地方，所以產出點集中在這裡；兩邊各算一次路徑
    遲早會漂移（例如只有一邊套用 --log-dir）。html_path=None 代表沒下 --html，什麼都不做。
    """
    if html_path is None:
        return None
    # __auto__＝旗標式的 --html：固定檔名、覆寫同一份，--watch/TUI 就是原地刷新的儀表板，
    # 不會堆積檔案。需保留歷史快照時，改用 --html <明確路徑>。
    target = Path(log_dir) / "log_monitor.html" if html_path == "__auto__" else Path(html_path)
    render_html(
        devices,
        target,
        generated_at=now,
        log_dir=str(log_dir),
        stale_hours=stale_hours,
    )
    return target


def _run_once(args, use_color: bool) -> Tuple[str, List[DeviceStatus]]:
    if args.sync_config:
        sync_logs(args.sync_config)
    now = datetime.now()
    records = collect_logs(args.log_dir, mode=args.mode)
    devices = aggregate_by_device(records, now=now, stale_hours=args.stale_hours)
    devices = _apply_filters(
        devices, args.vessel, args.ipc, args.component, args.status
    )
    if args.flat:
        out = render_cli(devices, now=now, use_color=use_color)
    else:
        expand = "all" if args.expand_all else "none" if args.collapsed else "auto"
        out = render_cli_grouped(
            build_tree(devices), now=now, use_color=use_color, expand=expand
        )

    target = write_html_report(devices, now, args.log_dir, args.stale_hours, args.html)
    if target is not None:
        out += f"\n\nHTML 報告已寫出：{target}"
    return out, devices


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    use_color = (not args.no_color) and sys.stdout.isatty()

    if args.tui:
        if not sys.stdout.isatty():
            print("[INFO] 非互動式終端機，改用靜態輸出。", file=sys.stderr)
        else:
            try:
                from monitor import tui
            except Exception as exc:  # pragma: no cover - 極少數環境缺 curses
                print(f"[WARN] 無法載入 TUI（{exc}），改用靜態輸出。", file=sys.stderr)
            else:
                return tui.run_app(args)

    if args.watch:
        try:
            while True:
                out, _ = _run_once(args, use_color)
                os.system("cls" if os.name == "nt" else "clear")
                print(out, flush=True)
                _sleep(args.watch)
        except KeyboardInterrupt:
            print("\n已停止監視。")
            return 0

    out, devices = _run_once(args, use_color)
    print(out)
    bad = sum(1 for d in devices if d.display_status not in ("success",))
    return 1 if bad else 0


def _sleep(seconds: float) -> None:
    import time

    time.sleep(seconds)


if __name__ == "__main__":
    sys.exit(main())
