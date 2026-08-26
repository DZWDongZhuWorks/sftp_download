# -*- coding: utf-8 -*-
"""monitor/log_monitor.py 的單元測試。

不需網路：於 tmp_path 寫入含 BOM 的合成 CSV log，直接驗證解析、彙整與呈現。
"""
import csv
from pathlib import Path
import subprocess
from datetime import datetime
from unittest import mock

import pytest

from monitor.log_monitor import (
    RunRecord,
    aggregate_by_device,
    build_parser,
    build_tree,
    collect_logs,
    group_is_problem,
    parse_device_name,
    parse_log_file,
    read_log_rows,
    render_cli,
    render_cli_grouped,
    render_html,
    sync_logs,
    write_html_report,
)


def write_log(path, device_name, rows, version_info=""):
    """rows: list of (timestamp, level, message)。以本體相同格式（utf-8-sig CSV）寫出。

    version_info 預設空字串 —— 那是舊 log 與「還沒宣告 VERSION.json 的專案」的實際樣子，
    也是絕大多數既有測試要的情境。
    """
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["timestamp", "device_name", "version_info", "level", "message"])
        for ts, level, msg in rows:
            w.writerow([ts, device_name, version_info, level, msg])
    return path


def _download_rows(start="2026-07-27 02:58:53", extra=None):
    rows = [
        (start, "INFO", "=== SFTP 下載任務開始 ==="),
        ("2026-07-27 02:59:21", "INFO", "共 2 個來源路徑，合併後發現 581 個檔案"),
    ]
    rows.extend(extra or [])
    return rows


# --- parse_log_file: 四態 --------------------------------------------------
def test_parse_success(tmp_path):
    p = write_log(
        tmp_path / "D_CLINK_IPC-1_ecdis_20260727_025853.csv",
        "CLINK_IPC-1_ecdis",
        _download_rows(
            extra=[("2026-07-27 02:59:56", "INFO", "=== 下載任務結束：成功 10，略過 571，失敗 0 ===")]
        ),
    )
    rec = parse_log_file(p)
    assert rec is not None
    assert rec.status == "success"
    assert rec.mode == "download"
    assert rec.device_name == "CLINK_IPC-1_ecdis"
    assert (rec.success, rec.skipped, rec.failed) == (10, 571, 0)
    assert rec.file_count == 581
    assert rec.started_at == datetime(2026, 7, 27, 2, 58, 53)


def test_parse_partial_with_failed_list(tmp_path):
    p = write_log(
        tmp_path / "D_dev_20260727_030000.csv",
        "CLINK_IPC-1_radar",
        _download_rows(
            extra=[
                ("2026-07-27 03:00:10", "ERROR", "檔案 a/x.bin 下載失敗，放棄重試"),
                ("2026-07-27 03:00:11", "INFO", "=== 下載任務結束：成功 3，略過 1，失敗 2 ==="),
                ("2026-07-27 03:00:11", "INFO", "失敗清單：a/x.bin, b/y.bin"),
            ]
        ),
    )
    rec = parse_log_file(p)
    assert rec.status == "partial"
    assert rec.failed == 2
    assert rec.failed_list == ["a/x.bin", "b/y.bin"]
    assert rec.errors  # 收集到 ERROR


def test_parse_aborted(tmp_path):
    p = write_log(
        tmp_path / "D_dev_20260727_040000.csv",
        "CLINK_IPC-1_scheduler",
        [
            ("2026-07-27 04:00:00", "INFO", "=== SFTP 下載任務開始 ==="),
            ("2026-07-27 04:00:02", "ERROR", "=== 任務中止：帳號或密碼錯誤 ==="),
        ],
    )
    rec = parse_log_file(p)
    assert rec.status == "aborted"
    assert rec.abort_reason == "帳號或密碼錯誤"


def test_parse_incomplete(tmp_path):
    p = write_log(
        tmp_path / "D_dev_20260727_050000.csv",
        "CLINK_IPC-1_share",
        [("2026-07-27 05:00:00", "INFO", "=== SFTP 下載任務開始 ===")],
    )
    rec = parse_log_file(p)
    assert rec.status == "incomplete"
    assert rec.success is None


def test_parse_upload_mode_from_content(tmp_path):
    # 檔名無前綴，仍應由起始行判定為 upload
    p = write_log(
        tmp_path / "legacy_uploader_20260727_060000.csv",
        "RADAR_UPLOADER",
        [
            ("2026-07-27 06:00:00", "INFO", "=== SFTP 上傳任務開始 ==="),
            ("2026-07-27 06:00:05", "INFO", "=== 上傳任務結束：成功 4，略過 0，失敗 0 ==="),
        ],
    )
    rec = parse_log_file(p)
    assert rec.mode == "upload"
    assert rec.status == "success"


def test_parse_multi_job_summary(tmp_path):
    p = write_log(
        tmp_path / "U_dev_20260727_070000.csv",
        "dev",
        [
            ("2026-07-27 07:00:00", "INFO", "=== SFTP 上傳任務開始 ==="),
            ("2026-07-27 07:00:05", "INFO", "=== 上傳任務結束（3 組）：成功 7，略過 2，失敗 0 ==="),
        ],
    )
    rec = parse_log_file(p)
    assert (rec.success, rec.skipped, rec.failed) == (7, 2, 0)
    assert rec.status == "success"


def test_parse_non_log_csv_returns_none(tmp_path):
    p = tmp_path / "other.csv"
    with open(p, "w", encoding="utf-8", newline="") as fh:
        csv.writer(fh).writerow(["foo", "bar", "baz"])
    assert parse_log_file(p) is None


# --- parse_device_name -----------------------------------------------------
@pytest.mark.parametrize(
    "name,expected",
    [
        ("CLINK_IPC-1_ecdis", ("CLINK", "IPC-1", "ecdis")),
        ("WH289_IPC-1_RADAR_DOWNLOADER", ("WH289", "IPC-1", "RADAR_DOWNLOADER")),
        ("RADAR_UPLOADER", (None, None, "RADAR_UPLOADER")),
        # 英文船名含底線：船名整段要進 vsl，不能在第一個 _ 就切斷。
        (
            "WH_PORT_KELANG_EXPRESS_IPC-1_ecdis",
            ("WH_PORT_KELANG_EXPRESS", "IPC-1", "ecdis"),
        ),
        # comp 自身含 IPC-N 時，邊界仍取第一個 _IPC-N_（非貪婪）。
        (
            "WH_PORT_KELANG_EXPRESS_IPC-1_sync_IPC-2_mirror",
            ("WH_PORT_KELANG_EXPRESS", "IPC-1", "sync_IPC-2_mirror"),
        ),
    ],
)
def test_parse_device_name(name, expected):
    assert parse_device_name(name) == expected


# --- collect_logs / aggregate ---------------------------------------------
def test_collect_logs_mode_filter(tmp_path):
    write_log(
        tmp_path / "D_a_20260727_010000.csv",
        "a",
        _download_rows(extra=[("2026-07-27 02:59:56", "INFO", "=== 下載任務結束：成功 1，略過 0，失敗 0 ===")]),
    )
    write_log(
        tmp_path / "U_b_20260727_010000.csv",
        "b",
        [
            ("2026-07-27 01:00:00", "INFO", "=== SFTP 上傳任務開始 ==="),
            ("2026-07-27 01:00:05", "INFO", "=== 上傳任務結束：成功 1，略過 0，失敗 0 ==="),
        ],
    )
    assert len(collect_logs(tmp_path, mode="all")) == 2
    downloads = collect_logs(tmp_path, mode="download")
    assert [r.device_name for r in downloads] == ["a"]


def test_aggregate_picks_latest_and_flags_stale(tmp_path):
    write_log(
        tmp_path / "D_dev_20260720_010000.csv",
        "CLINK_IPC-1_ecdis",
        [
            ("2026-07-20 01:00:00", "INFO", "=== SFTP 下載任務開始 ==="),
            ("2026-07-20 01:00:05", "INFO", "=== 下載任務結束：成功 9，略過 0，失敗 0 ==="),
        ],
    )
    write_log(
        tmp_path / "D_dev_20260727_010000.csv",
        "CLINK_IPC-1_ecdis",
        [
            ("2026-07-27 01:00:00", "INFO", "=== SFTP 下載任務開始 ==="),
            ("2026-07-27 01:00:05", "INFO", "=== 下載任務結束：成功 5，略過 0，失敗 0 ==="),
        ],
    )
    records = collect_logs(tmp_path)
    now = datetime(2026, 7, 27, 3, 0, 0)
    devices = aggregate_by_device(records, now=now, stale_hours=24)
    assert len(devices) == 1
    d = devices[0]
    assert d.run_count == 2
    assert d.latest.started_at == datetime(2026, 7, 27, 1, 0, 0)  # 取最新
    assert d.latest.success == 5
    assert d.is_stale is False
    assert d.display_status == "success"

    # 門檻縮到 1 小時 → 逾期
    devices_stale = aggregate_by_device(records, now=now, stale_hours=1)
    assert devices_stale[0].is_stale is True
    assert devices_stale[0].display_status == "stale"


def test_aggregate_sort_severity_first(tmp_path):
    write_log(
        tmp_path / "D_ok_20260727_020000.csv",
        "ok_dev",
        [
            ("2026-07-27 02:00:00", "INFO", "=== SFTP 下載任務開始 ==="),
            ("2026-07-27 02:00:05", "INFO", "=== 下載任務結束：成功 1，略過 0，失敗 0 ==="),
        ],
    )
    write_log(
        tmp_path / "D_bad_20260727_020000.csv",
        "bad_dev",
        [
            ("2026-07-27 02:00:00", "INFO", "=== SFTP 下載任務開始 ==="),
            ("2026-07-27 02:00:05", "INFO", "=== 下載任務結束：成功 0，略過 0，失敗 3 ==="),
        ],
    )
    now = datetime(2026, 7, 27, 2, 30, 0)
    devices = aggregate_by_device(collect_logs(tmp_path), now=now, stale_hours=24)
    assert devices[0].device_name == "bad_dev"  # 異常排最前


# --- render ----------------------------------------------------------------
def _sample_devices(tmp_path):
    write_log(
        tmp_path / "D_CLINK_IPC-1_ecdis_20260727_010000.csv",
        "CLINK_IPC-1_ecdis",
        [
            ("2026-07-27 01:00:00", "INFO", "=== SFTP 下載任務開始 ==="),
            ("2026-07-27 01:00:05", "INFO", "=== 下載任務結束：成功 10，略過 571，失敗 0 ==="),
        ],
    )
    now = datetime(2026, 7, 27, 1, 30, 0)
    return aggregate_by_device(collect_logs(tmp_path), now=now, stale_hours=24), now


def test_render_cli_plain_has_no_ansi(tmp_path):
    devices, now = _sample_devices(tmp_path)
    out = render_cli(devices, now=now, use_color=False)
    assert "\033[" not in out
    assert "CLINK_IPC-1_ecdis" in out
    assert "裝置 1" in out
    assert "10/571/0" in out


def test_render_cli_color_has_ansi(tmp_path):
    devices, now = _sample_devices(tmp_path)
    out = render_cli(devices, now=now, use_color=True)
    assert "\033[" in out


def test_render_html_writes_file(tmp_path):
    devices, now = _sample_devices(tmp_path)
    out = tmp_path / "report.html"
    render_html(devices, out, generated_at=now, log_dir=str(tmp_path), stale_hours=24)
    assert out.exists()
    content = out.read_text(encoding="utf-8")
    assert "CLINK_IPC-1_ecdis" in content
    assert "chip s-success" in content
    assert "<!DOCTYPE html>" in content


def test_write_html_report_paths(tmp_path):
    """靜態、--watch 與 TUI 共用這個產出點，三種 html_path 語意都要對。"""
    devices, now = _sample_devices(tmp_path)
    assert write_html_report(devices, now, tmp_path, 24, None) is None  # 沒下 --html
    assert not list(tmp_path.glob("*.html"))

    auto = write_html_report(devices, now, tmp_path, 24, "__auto__")   # 旗標式 --html
    assert auto == tmp_path / "log_monitor.html" and auto.exists()

    explicit = write_html_report(devices, now, tmp_path, 24, str(tmp_path / "x" / "r.html"))
    assert explicit == tmp_path / "x" / "r.html" and explicit.exists()


def test_render_empty(tmp_path):
    now = datetime(2026, 7, 27, 1, 0, 0)
    out = render_cli([], now=now, use_color=False)
    assert "無資料" in out
    html_out = tmp_path / "e.html"
    render_html([], html_out, generated_at=now)
    assert "無資料" in html_out.read_text(encoding="utf-8")


# --- sync_logs -------------------------------------------------------------
def test_sync_logs_success(tmp_path):
    cfg = tmp_path / "c.json"
    cfg.write_text("{}", encoding="utf-8")
    with mock.patch("monitor.log_monitor.subprocess.run") as m:
        m.return_value = mock.Mock(returncode=0)
        assert sync_logs(cfg) is True
    args = m.call_args[0][0]
    assert "--cli" in args and "download" in args and str(cfg) in args
    # call_args[1] 而不是 .kwargs：後者是 3.8 才有的存取器，而船上的 venv 是 3.6
    # —— 在 3.6 上 .kwargs 會回傳一個空的 Call，於是這兩行永遠成立（假通過）。
    assert "stdout" not in m.call_args[1]
    assert "stderr" not in m.call_args[1]


def test_sync_logs_quiet_suppresses_child_output(tmp_path):
    cfg = tmp_path / "c.json"
    cfg.write_text("{}", encoding="utf-8")
    with mock.patch("monitor.log_monitor.subprocess.run") as m:
        m.return_value = mock.Mock(returncode=0)
        assert sync_logs(cfg, quiet=True) is True
    assert m.call_args[1]["stdout"] is subprocess.DEVNULL
    assert m.call_args[1]["stderr"] is subprocess.DEVNULL


def test_sync_logs_streams_combined_output(tmp_path):
    cfg = tmp_path / "c.json"
    cfg.write_text("{}", encoding="utf-8")

    class FakeProcess:
        stdout = iter(["first\n", "second\r\n"])

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

        def kill(self):
            raise AssertionError("successful process must not be killed")

    lines = []
    with mock.patch(
        "monitor.log_monitor.subprocess.Popen", return_value=FakeProcess()
    ) as popen:
        assert sync_logs(cfg, quiet=True, output_callback=lines.append) is True
    assert lines == ["first", "second"]
    assert popen.call_args[1]["stdout"] is subprocess.PIPE
    assert popen.call_args[1]["stderr"] is subprocess.STDOUT


def test_sync_logs_nonzero_returns_false(tmp_path):
    cfg = tmp_path / "c.json"
    cfg.write_text("{}", encoding="utf-8")
    with mock.patch("monitor.log_monitor.subprocess.run") as m:
        m.return_value = mock.Mock(returncode=1)
        assert sync_logs(cfg) is False


def test_sync_logs_missing_config_returns_false(tmp_path):
    assert sync_logs(tmp_path / "nope.json") is False


def test_build_parser_defaults():
    args = build_parser().parse_args([])
    assert args.mode == "all"
    assert args.stale_hours == 72
    assert args.html is None
    assert args.flat is False
    assert args.status == "all"


# ===========================================================================
# v2：階層分群 + 折疊 + 過濾
# ===========================================================================
NOW = datetime(2026, 7, 27, 12, 0, 0)
_RECENT = "2026-07-27 11:00:00"  # 1 小時前（未過期）
_OLD = "2026-07-20 11:00:00"     # 7 天前（過期）


def _summary_rows(direction, when, success, skipped, failed):
    verb = "下載" if direction == "download" else "上傳"
    return [
        (when, "INFO", f"=== SFTP {verb}任務開始 ==="),
        (when, "INFO", f"=== {verb}任務結束：成功 {success}，略過 {skipped}，失敗 {failed} ==="),
    ]


def _devices(tmp_path, stale_hours=24):
    return aggregate_by_device(collect_logs(tmp_path), now=NOW, stale_hours=stale_hours)


def test_aggregate_splits_upload_download_same_device(tmp_path):
    # 同一 device_name 同時有下載與上傳 log → 應拆成 2 個 device
    write_log(
        tmp_path / "D_CLINK_IPC-1_ecdis_20260727_110000.csv",
        "CLINK_IPC-1_ecdis",
        _summary_rows("download", _RECENT, 5, 0, 0),
    )
    write_log(
        tmp_path / "U_CLINK_IPC-1_ecdis_20260727_110000.csv",
        "CLINK_IPC-1_ecdis",
        _summary_rows("upload", _RECENT, 3, 0, 0),
    )
    devices = _devices(tmp_path)
    modes = sorted(d.latest.mode for d in devices)
    assert modes == ["download", "upload"]
    assert len(devices) == 2


def test_build_tree_hierarchy_and_rollup(tmp_path):
    write_log(  # download CLINK/IPC-1/ecdis 正常
        tmp_path / "D_CLINK_IPC-1_ecdis_20260727_110000.csv",
        "CLINK_IPC-1_ecdis",
        _summary_rows("download", _RECENT, 5, 0, 0),
    )
    write_log(  # download CLINK/IPC-1/radar 部分失敗
        tmp_path / "D_CLINK_IPC-1_radar_20260727_110000.csv",
        "CLINK_IPC-1_radar",
        _summary_rows("download", _RECENT, 1, 0, 2),
    )
    write_log(  # upload 靜態名（無 vessel/ipc）
        tmp_path / "U_RADAR_UPLOADER_20260727_110000.csv",
        "RADAR_UPLOADER",
        _summary_rows("upload", _RECENT, 4, 0, 0),
    )
    tree = build_tree(_devices(tmp_path))

    modes = [m.mode for m in tree]
    assert modes == ["download", "upload"]  # download 先於 upload

    dl = tree[0]
    assert dl.summary.total == 2 and dl.summary.ok == 1 and dl.summary.bad == 1
    assert dl.summary.worst == "partial"
    clink = dl.vessels[0]
    assert clink.name == "CLINK"
    assert clink.ipcs[0].name == "IPC-1"
    comps = {d.component for d in clink.ipcs[0].devices}
    assert comps == {"ecdis", "radar"}
    # 問題元件（radar）排在健康元件（ecdis）之前
    assert clink.ipcs[0].devices[0].component == "radar"

    ul = tree[1]
    assert ul.vessels[0].name == "（未分類）"
    assert ul.vessels[0].ipcs[0].name == "—"


def test_group_is_problem():
    from monitor.log_monitor import GroupSummary

    assert group_is_problem(GroupSummary(total=3, ok=3)) is False
    assert group_is_problem(GroupSummary(total=3, ok=2, stale=1, worst="stale")) is True
    assert group_is_problem(GroupSummary(total=1, bad=1, worst="partial")) is True


def test_render_cli_grouped_collapses_healthy(tmp_path):
    write_log(  # 健康船：WH999 全部正常且新鮮
        tmp_path / "D_WH999_IPC-1_ecdis_20260727_110000.csv",
        "WH999_IPC-1_ecdis",
        _summary_rows("download", _RECENT, 5, 0, 0),
    )
    write_log(  # 問題船：CLINK 有失敗
        tmp_path / "D_CLINK_IPC-1_radar_20260727_110000.csv",
        "CLINK_IPC-1_radar",
        _summary_rows("download", _RECENT, 0, 0, 3),
    )
    tree = build_tree(_devices(tmp_path))

    auto = render_cli_grouped(tree, now=NOW, use_color=False, expand="auto")
    # 問題船 CLINK 展開 → 列出 radar；健康船 WH999 收合 → 不列 ecdis
    assert "radar" in auto
    assert "ecdis" not in auto
    assert "WH999" in auto  # 摘要行仍在

    all_exp = render_cli_grouped(tree, now=NOW, use_color=False, expand="all")
    assert "ecdis" in all_exp and "radar" in all_exp

    none_exp = render_cli_grouped(tree, now=NOW, use_color=False, expand="none")
    assert "ecdis" not in none_exp and "radar" not in none_exp

    assert "\033[" not in auto  # --no-color


def test_render_html_grouped_structure(tmp_path):
    write_log(
        tmp_path / "D_CLINK_IPC-1_ecdis_20260727_110000.csv",
        "CLINK_IPC-1_ecdis",
        _summary_rows("download", _RECENT, 5, 0, 0),
    )
    write_log(  # 問題船：過期
        tmp_path / "D_WH289_IPC-1_radar_20260720_110000.csv",
        "WH289_IPC-1_radar",
        _summary_rows("download", _OLD, 5, 0, 0),
    )
    out = tmp_path / "r.html"
    render_html(_devices(tmp_path), out, generated_at=NOW, log_dir=str(tmp_path))
    content = out.read_text(encoding="utf-8")

    assert "↓ 下載" in content
    assert 'details class="vessel"' in content
    assert 'details class="ipc"' in content
    assert "data-vessel=" in content and "data-ipc=" in content
    assert "data-component=" in content and "data-status=" in content
    # 控制列
    assert 'id="fv"' in content and 'id="fi"' in content and 'id="fs"' in content
    assert "只看異常" in content
    # 過期船（WH289）群組預設展開；正常船（CLINK）收合
    assert 'data-vessel="WH289" open' in content
    assert 'data-vessel="CLINK">' in content  # 無 open


def test_render_html_upload_download_sections(tmp_path):
    write_log(
        tmp_path / "D_CLINK_IPC-1_ecdis_20260727_110000.csv",
        "CLINK_IPC-1_ecdis",
        _summary_rows("download", _RECENT, 5, 0, 0),
    )
    write_log(
        tmp_path / "U_CLINK_IPC-1_ecdis_20260727_110000.csv",
        "CLINK_IPC-1_ecdis",
        _summary_rows("upload", _RECENT, 3, 0, 0),
    )
    out = tmp_path / "r.html"
    render_html(_devices(tmp_path), out, generated_at=NOW)
    content = out.read_text(encoding="utf-8")
    assert "↓ 下載" in content and "↑ 上傳" in content
    assert content.count("mode-sec") >= 2


# --- read_log_rows：TUI 檢視原始資料用 -------------------------------------
def test_read_log_rows_returns_every_row(tmp_path):
    p = write_log(
        tmp_path / "D_CLINK_IPC-1_radar_20260727_110000.csv",
        "CLINK_IPC-1_radar",
        [
            (_RECENT, "INFO", "=== SFTP 下載任務開始 ==="),
            (_RECENT, "ERROR", "檔案 a/x.bin 下載失敗，放棄重試"),
            (_RECENT, "INFO", "=== 下載任務結束：成功 0，略過 0，失敗 1 ==="),
        ],
    )
    rows, truncated = read_log_rows(p)
    assert truncated is False
    assert len(rows) == 3  # INFO 也保留（不像 parse_log_file 只留 ERROR/WARNING）
    assert all(len(r) == 5 for r in rows)
    assert rows[1][3] == "ERROR"
    assert rows[1][4] == "檔案 a/x.bin 下載失敗，放棄重試"
    assert rows[0][1] == "CLINK_IPC-1_radar"


def test_read_log_rows_truncates_at_max_rows(tmp_path):
    p = write_log(
        tmp_path / "D_CLINK_IPC-1_radar_20260727_110000.csv",
        "CLINK_IPC-1_radar",
        [(_RECENT, "INFO", f"第 {i} 行") for i in range(10)],
    )
    rows, truncated = read_log_rows(p, max_rows=4)
    assert truncated is True and len(rows) == 4
    assert rows[-1][4] == "第 3 行"


def test_read_log_rows_pads_short_rows(tmp_path):
    # 短列補齊而非略過：檢視原始資料時不該偷偷藏列
    p = tmp_path / "D_x_20260727_110000.csv"
    with open(p, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["timestamp", "device_name", "version_info", "level", "message"])
        w.writerow([_RECENT, "dev"])
    rows, _ = read_log_rows(p)
    assert rows == [[_RECENT, "dev", "", "", ""]]


def test_read_log_rows_rejects_foreign_and_missing(tmp_path):
    # 表頭不符 → 空結果
    alien = tmp_path / "alien.csv"
    with open(alien, "w", encoding="utf-8", newline="") as fh:
        csv.writer(fh).writerows([["a", "b"], ["1", "2"]])
    assert read_log_rows(alien) == ([], False)
    # 只有表頭的空 log → 空結果但不算截斷
    empty = write_log(tmp_path / "D_e_20260727_110000.csv", "dev", [])
    assert read_log_rows(empty) == ([], False)
    # 缺檔（--watch 重新下載期間檔案可能被換掉）→ 不拋錯
    assert read_log_rows(tmp_path / "nope.csv") == ([], False)


# --- version_info：向上相容（有些專案沒有版號）-----------------------------
def test_parse_keeps_version_info(tmp_path):
    """CSV 第三欄的版本要留在 RunRecord 上 —— 岸端主清單靠它回答「哪艘船跑哪一版」。"""
    log = write_log(tmp_path / "D_WH289_IPC-1_RADAR_20260826_010000.csv",
                    "WH289_IPC-1_RADAR", _download_rows(), version_info="0.4.1+20b8056")
    rec = parse_log_file(log)
    assert rec.version_info == "0.4.1+20b8056"


def test_parse_without_version_info_is_empty(tmp_path):
    """沒有版本是正常狀態,不是錯誤:舊 log 與尚未宣告 VERSION.json 的專案都會是空的。"""
    log = write_log(tmp_path / "D_WH289_IPC-1_ecdis_20260826_010000.csv",
                    "WH289_IPC-1_ecdis", _download_rows())
    rec = parse_log_file(log)
    assert rec.version_info == ""


def test_parse_version_info_takes_first_non_empty(tmp_path):
    """同一份 log 每列都是同一個值;真有前幾列空白（舊版 handler）也要取得到。"""
    path = tmp_path / "D_WH289_IPC-1_RADAR_20260826_020000.csv"
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["timestamp", "device_name", "version_info", "level", "message"])
        w.writerow(["2026-08-26 01:00:00", "WH289_IPC-1_RADAR", "", "INFO", "=== SFTP 下載任務開始 ==="])
        w.writerow(["2026-08-26 01:00:01", "WH289_IPC-1_RADAR", "0.4.1+20b8056", "INFO", "共 1 個來源路徑，合併後發現 3 個檔案"])
    assert parse_log_file(path).version_info == "0.4.1+20b8056"


def test_run_record_version_info_defaults_to_empty():
    """RunRecord 給了預設值 —— 既有的建構呼叫端不必跟著改（向上相容）。"""
    rec = RunRecord(path=Path("x.csv"), device_name="d", mode="download")
    assert rec.version_info == ""

