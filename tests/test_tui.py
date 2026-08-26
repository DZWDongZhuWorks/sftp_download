# -*- coding: utf-8 -*-
"""monitor/tui.py 純邏輯測試（不觸及 curses 繪製）。

curses.KEY_* 為模組常數，import 後即可用，無需真實終端機。
"""
import csv
import curses
from datetime import datetime
from types import SimpleNamespace
from unittest import mock

from monitor.log_monitor import (
    aggregate_by_device,
    build_tree,
    collect_logs,
    device_detail_lines,
)
from monitor import tui

# Bionic(18.04)的 ncurses 是 mouse v1，Python 3.6 的 curses 因此**沒有** BUTTON5_*。
# tui.py 本來就用 getattr 取這些常數（_mouse_bits），取不到就只是沒有「滾輪向下」，
# 不是錯誤。測試跟著用同一個 fallback：常數不存在時那幾條斷言沒有意義，略過它 ——
# 讓它 AttributeError 只會把整條船的健康檢查染紅（health_check 會跑這整套測試）。
BUTTON5_PRESSED = getattr(curses, "BUTTON5_PRESSED", None)

NOW = datetime(2026, 7, 27, 12, 0, 0)
RECENT = "2026-07-27 11:00:00"  # 1 小時前（未過期）
OLD = "2026-07-20 11:00:00"     # 7 天前（過期）


def _write(path, device_name, direction, when, success, skipped, failed, version=""):
    verb = "下載" if direction == "download" else "上傳"
    rows = [
        (when, "INFO", f"=== SFTP {verb}任務開始 ==="),
        (when, "INFO", f"=== {verb}任務結束：成功 {success}，略過 {skipped}，失敗 {failed} ==="),
    ]
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["timestamp", "device_name", "version_info", "level", "message"])
        for ts, level, msg in rows:
            w.writerow([ts, device_name, version, level, msg])


def _tree(tmp_path, specs, stale_hours=24):
    """specs 每筆 6 欄（無版本，等同舊 log）或 7 欄（第 7 欄為版本字串）。"""
    for i, spec in enumerate(specs):
        dev, direction, when, s, k, f = spec[:6]
        version = spec[6] if len(spec) > 6 else ""
        prefix = "D_" if direction == "download" else "U_"
        _write(tmp_path / f"{prefix}{dev}_{i}.csv", dev, direction, when, s, k, f, version)
    devices = aggregate_by_device(collect_logs(tmp_path), now=NOW, stale_hours=stale_hours)
    return build_tree(devices)


def test_load_tree_quiets_sync_output(tmp_path):
    args = SimpleNamespace(
        sync_config="sync.json",
        log_dir=tmp_path,
        mode="all",
        stale_hours=24,
        vessel=None,
        ipc=None,
        component=None,
        status="all",
    )
    with mock.patch.object(tui, "sync_logs", return_value=True) as sync:
        assert tui.load_tree(args, NOW) == []
    sync.assert_called_once_with("sync.json", quiet=True)


def _html_args(tmp_path, html):
    return SimpleNamespace(
        sync_config=None, log_dir=tmp_path, mode="all", stale_hours=24,
        vessel=None, ipc=None, component=None, status="all", html=html,
    )


def test_write_html_snapshot_disabled_without_flag(tmp_path):
    tree = _sorted_tree(tmp_path)
    assert tui.write_html_snapshot(_html_args(tmp_path, None), tree, NOW) == ""
    assert not list(tmp_path.glob("*.html"))


def test_write_html_snapshot_auto_path_and_content(tmp_path):
    """--tui --html（不帶路徑）要寫到 <log-dir>/log_monitor.html，與靜態輸出同一份。"""
    tree = _sorted_tree(tmp_path)
    note = tui.write_html_snapshot(_html_args(tmp_path, "__auto__"), tree, NOW)
    out = tmp_path / "log_monitor.html"
    assert note == "HTML→log_monitor.html"
    text = out.read_text(encoding="utf-8")
    # 樹上每台裝置都要進報告（含無法解析船名者），過濾語意與靜態輸出一致
    for dev in ("ecdis", "radar", "share", "RADAR_UPLOADER"):
        assert dev in text


def test_write_html_snapshot_explicit_path(tmp_path):
    tree = _sorted_tree(tmp_path)
    target = tmp_path / "sub" / "report.html"  # render_html 會自己建目錄
    note = tui.write_html_snapshot(_html_args(tmp_path, str(target)), tree, NOW)
    assert note == "HTML→report.html" and target.exists()


def test_write_html_snapshot_survives_write_error(tmp_path):
    """報告只是副產物：寫不出來要回報訊息，不能讓整個 TUI 當掉。"""
    tree = _sorted_tree(tmp_path)
    args = _html_args(tmp_path, "__auto__")
    with mock.patch.object(tui, "write_html_report", side_effect=PermissionError("ro")):
        assert tui.write_html_snapshot(args, tree, NOW) == "HTML 失敗：PermissionError"


def test_sync_progress_keeps_recent_lines_clean():
    # 高度足夠顯示滿 _SYNC_LINE_LIMIT 行
    class FakeScreen:
        def __init__(self):
            self.rows = {}

        def erase(self):
            self.rows.clear()

        def getmaxyx(self):
            return tui._SYNC_LINE_LIMIT + 4, 80

        def addstr(self, y, x, text, attr=0):
            self.rows[y] = text

        def refresh(self):
            pass

    limit = tui._SYNC_LINE_LIMIT
    total = limit + 5  # 餵超過上限，驗證只保留最近 limit 行

    def fake_sync(config, quiet, output_callback):
        assert config == "sync.json"
        assert quiet is True
        for number in range(1, total + 1):
            output_callback(f"\x1b[31mline {number}\x1b[0m\r")
        return True

    screen = FakeScreen()
    with mock.patch.object(tui, "sync_logs", side_effect=fake_sync):
        assert tui._sync_with_progress(screen, "sync.json") is True

    values = set(screen.rows.values())
    # 只保留最近 limit 行（line 6..25）；較早的 line 1..5 被丟棄
    for n in range(total - limit + 1, total + 1):
        assert f"line {n}" in values
    for n in range(1, total - limit + 1):
        assert f"line {n}" not in values
    # ANSI / 控制字元已清除
    assert all("\x1b" not in v for v in values)


# --- flatten：展開/收合 ----------------------------------------------------
def test_flatten_expand_collapse(tmp_path):
    tree = _tree(
        tmp_path,
        [
            ("CLINK_IPC-1_ecdis", "download", RECENT, 5, 0, 0),
            ("CLINK_IPC-1_radar", "download", RECENT, 3, 0, 0),
        ],
    )
    st = tui.TuiState()
    tui.seed_expanded(tree, st)  # 全健康 → 不預設展開

    default_rows = tui.flatten_tree(tree, st, NOW)
    assert [r.kind for r in default_rows] == ["mode"]  # 只有頂層

    tui.expand_all(st, tree)
    rows = tui.flatten_tree(tree, st, NOW)
    kinds = [r.kind for r in rows]
    assert kinds == ["mode", "vessel", "ipc", "device", "device"]
    assert rows[0].depth == 0 and rows[3].depth == 3

    tui.collapse_all(st)
    assert [r.kind for r in tui.flatten_tree(tree, st, NOW)] == ["mode"]


def test_seed_expands_problem_groups(tmp_path):
    tree = _tree(tmp_path, [("CLINK_IPC-1_radar", "download", RECENT, 0, 0, 3)])  # 失敗
    st = tui.TuiState()
    tui.seed_expanded(tree, st)
    rows = tui.flatten_tree(tree, st, NOW)
    assert [r.kind for r in rows] == ["mode", "vessel", "ipc", "device"]  # 問題群組自動展開


def test_seed_respects_user_collapse(tmp_path):
    # 問題群組首次預設展開；使用者收合後再次 seed（同一棵樹）不應被重新展開
    tree = _tree(tmp_path, [("CLINK_IPC-1_radar", "download", RECENT, 0, 0, 3)])
    st = tui.TuiState()
    tui.seed_expanded(tree, st)
    vkey = ("V", "download", "CLINK")
    assert vkey in st.expanded
    st.expanded.discard(vkey)          # 使用者收合
    tui.seed_expanded(tree, st)        # 模擬重載後再次 seed
    assert vkey not in st.expanded     # 已在 seen，不再自動展開


# --- flatten：過濾 ---------------------------------------------------------
def test_flatten_filters(tmp_path):
    tree = _tree(
        tmp_path,
        [
            ("CLINK_IPC-1_ecdis", "download", RECENT, 5, 0, 0),   # success
            ("CLINK_IPC-1_radar", "download", RECENT, 0, 0, 2),   # partial
            ("CLINK_IPC-1_share", "download", OLD, 5, 0, 0),      # stale
        ],
    )
    st = tui.TuiState()
    tui.expand_all(st, tree)

    def devs(state):
        return [r for r in tui.flatten_tree(tree, state, NOW) if r.kind == "device"]

    assert len(devs(st)) == 3

    st.only_problem = True  # 非 success（partial + stale）
    assert len(devs(st)) == 2

    st.only_problem = False
    st.status = "ok"
    assert [r.ref.component for r in devs(st)] == ["ecdis"]

    st.status = "problem"  # 非 success 且非 stale → 只有 partial
    assert [r.ref.component for r in devs(st)] == ["radar"]

    st.status = "all"
    st.query = "share"
    assert [r.ref.component for r in devs(st)] == ["share"]


def test_flatten_mode_filter(tmp_path):
    tree = _tree(
        tmp_path,
        [
            ("CLINK_IPC-1_ecdis", "download", RECENT, 5, 0, 0),
            ("CLINK_IPC-1_ecdis", "upload", RECENT, 3, 0, 0),
        ],
    )
    st = tui.TuiState()
    tui.expand_all(st, tree)
    modes = {r.text.split()[0] for r in tui.flatten_tree(tree, st, NOW) if r.kind == "mode"}
    assert modes == {"↓", "↑"}  # _MODE_LABEL 前綴

    st.mode = "download"
    kept = {r.ref.latest.mode for r in tui.flatten_tree(tree, st, NOW) if r.kind == "device"}
    assert kept == {"download"}


def test_flatten_empty_when_all_filtered(tmp_path):
    tree = _tree(tmp_path, [("CLINK_IPC-1_ecdis", "download", RECENT, 5, 0, 0)])
    st = tui.TuiState()
    tui.expand_all(st, tree)
    st.query = "no-such-device"
    assert tui.flatten_tree(tree, st, NOW) == []


# --- reducer ---------------------------------------------------------------
def test_toggle_and_cycles():
    st = tui.TuiState()
    tui.toggle(st, ("M", "download"))
    assert ("M", "download") in st.expanded
    tui.toggle(st, ("M", "download"))
    assert ("M", "download") not in st.expanded

    assert st.mode == ""
    tui.cycle_mode(st); assert st.mode == "download"
    tui.cycle_mode(st); assert st.mode == "upload"
    tui.cycle_mode(st); assert st.mode == ""

    assert st.status == "all"
    tui.cycle_status(st); assert st.status == "ok"
    for _ in range(3):
        tui.cycle_status(st)
    assert st.status == "all"

    assert st.only_problem is False
    tui.toggle_problem(st); assert st.only_problem is True


def test_move_selection_clamps(tmp_path):
    tree = _tree(
        tmp_path,
        [
            ("CLINK_IPC-1_ecdis", "download", RECENT, 5, 0, 0),
            ("CLINK_IPC-1_radar", "download", RECENT, 3, 0, 0),
        ],
    )
    st = tui.TuiState()
    tui.expand_all(st, tree)
    rows = tui.flatten_tree(tree, st, NOW)
    tui.clamp_selection(rows, st)
    assert st.sel_key == rows[0].key

    tui.move_selection(rows, st, -5)  # 夾在頂端
    assert st.sel_key == rows[0].key
    tui.move_selection(rows, st, 999)  # 夾在底端
    assert st.sel_key == rows[-1].key

    # 空列表：選取清空、不崩潰
    tui.move_selection([], st, 1)
    assert st.sel_key is None


def test_clamp_selection_recovers_missing_key(tmp_path):
    tree = _tree(tmp_path, [("CLINK_IPC-1_ecdis", "download", RECENT, 5, 0, 0)])
    st = tui.TuiState()
    tui.expand_all(st, tree)
    rows = tui.flatten_tree(tree, st, NOW)
    st.sel_key = ("D", "download", "GONE", "IPC-9", "x")  # 不存在
    tui.clamp_selection(rows, st)
    assert st.sel_key == rows[0].key


def test_parent_key():
    assert tui.parent_key(("D", "download", "CLINK", "IPC-1", "ecdis")) == (
        "I", "download", "CLINK", "IPC-1"
    )
    assert tui.parent_key(("I", "download", "CLINK", "IPC-1")) == ("V", "download", "CLINK")
    assert tui.parent_key(("V", "download", "CLINK")) == ("M", "download")
    assert tui.parent_key(("M", "download")) is None


def test_key_action():
    assert tui.key_action(ord("q")) == "quit"
    assert tui.key_action(curses.KEY_UP) == "up"
    assert tui.key_action(ord("j")) == "down"
    assert tui.key_action(ord(" ")) == "enter"
    assert tui.key_action(curses.KEY_ENTER) == "enter"
    assert tui.key_action(curses.KEY_RIGHT) == "expand"
    assert tui.key_action(ord("/")) == "search"
    assert tui.key_action(ord("E")) == "expand_all"
    assert tui.key_action(ord("z")) is None


def test_mouse_event_kind_and_safe_initialisation():
    assert tui.mouse_event_kind(curses.BUTTON1_CLICKED) == "click"
    assert tui.mouse_event_kind(curses.BUTTON1_DOUBLE_CLICKED) == "activate"
    assert tui.mouse_event_kind(curses.BUTTON3_CLICKED) == "close"
    assert tui.mouse_event_kind(curses.BUTTON4_PRESSED) == "wheel_up"
    if BUTTON5_PRESSED is not None:
        assert tui.mouse_event_kind(BUTTON5_PRESSED) == "wheel_down"
    assert tui.mouse_event_kind(curses.BUTTON1_RELEASED) is None

    with mock.patch.object(curses, "mousemask", return_value=(curses.ALL_MOUSE_EVENTS, 0)) as mask, \
         mock.patch.object(curses, "mouseinterval") as interval:
        assert tui._enable_mouse() is True
    mask.assert_called_once_with(curses.ALL_MOUSE_EVENTS)
    interval.assert_called_once_with(tui._MOUSE_INTERVAL_MS)

    with mock.patch.object(curses, "mousemask", side_effect=curses.error):
        assert tui._enable_mouse() is False


def test_main_mouse_row_mapping_and_actions():
    rows = [
        tui.Row("mode", 0, ("M", "download"), "下載", "success", object()),
        tui.Row("vessel", 1, ("V", "download", "CLINK"), "CLINK", "success", object()),
        tui.Row("device", 2, ("D", "download", "CLINK", "IPC-1", "ecdis"),
                "ecdis", "success", object()),
    ]
    state = tui.TuiState()

    # 分群資料從 y=2 開始；表頭、底列與資料後的空白都不可選。
    assert tui.mouse_row_index(1, maxy=10, state=state, scroll=0, total=3) is None
    assert tui.mouse_row_index(2, maxy=10, state=state, scroll=0, total=3) == 0
    assert tui.mouse_row_index(4, maxy=10, state=state, scroll=0, total=3) == 2
    assert tui.mouse_row_index(5, maxy=10, state=state, scroll=0, total=3) is None
    assert tui.mouse_row_index(9, maxy=10, state=state, scroll=0, total=3) is None

    # 單擊文字只選取；雙擊啟動；群組三角形（depth*2）單擊即開合。
    assert tui.main_mouse_action(8, 3, curses.BUTTON1_CLICKED, rows, state, 10) == (
        "select", 1
    )
    assert tui.main_mouse_action(8, 3, curses.BUTTON1_DOUBLE_CLICKED, rows, state, 10) == (
        "enter", 1
    )
    assert tui.main_mouse_action(2, 3, curses.BUTTON1_CLICKED, rows, state, 10) == (
        "enter", 1
    )
    assert tui.main_mouse_action(8, 4, curses.BUTTON1_CLICKED, rows, state, 10) == (
        "select", 2
    )
    assert tui.main_mouse_action(0, 0, curses.BUTTON4_PRESSED, rows, state, 10) == (
        "wheel_up", None
    )

    # 平坦模式多一行欄名，因此第一筆從 y=3 開始；scroll 要加回資料索引。
    state.flat = True
    state.scroll = 1
    assert tui.mouse_row_index(2, maxy=10, state=state, scroll=1, total=3) is None
    assert tui.mouse_row_index(3, maxy=10, state=state, scroll=1, total=3) == 1


def test_popup_mouse_actions():
    bounds = dict(x0=10, y0=5, w=30, h=8)
    assert tui.popup_mouse_action(12, 7, curses.BUTTON1_CLICKED, **bounds) == "activate"
    assert tui.popup_mouse_action(2, 2, curses.BUTTON1_CLICKED, **bounds) == "close"
    assert tui.popup_mouse_action(12, 7, curses.BUTTON3_CLICKED, **bounds) == "close"
    assert tui.popup_mouse_action(2, 2, curses.BUTTON4_PRESSED, **bounds) == "wheel_up"


def test_main_loop_routes_mouse_click_to_selection():
    """完整接線：KEY_MOUSE → getmouse 座標換算 → 下一幀選取列改變。"""
    rows = [
        tui.Row("mode", 0, ("M", "download"), "下載", "success", object()),
        tui.Row("vessel", 1, ("V", "download", "CLINK"), "CLINK", "success", object()),
    ]

    class FakeScreen:
        def __init__(self):
            self.keys = iter([curses.KEY_MOUSE, ord("q")])

        def timeout(self, _delay):
            pass

        def getmaxyx(self):
            return 20, 80

        def getch(self):
            return next(self.keys)

    selected = []
    args = SimpleNamespace(flat=False, watch=None)
    with mock.patch.object(curses, "curs_set"), \
         mock.patch.object(curses, "has_colors", return_value=False), \
         mock.patch.object(tui, "_enable_mouse") as enable, \
         mock.patch.object(tui, "load_tree", return_value=[]), \
         mock.patch.object(tui, "write_html_snapshot", return_value=""), \
         mock.patch.object(tui, "visible_rows", return_value=rows), \
         mock.patch.object(tui, "_draw",
                           side_effect=lambda _s, st, *_a: selected.append(st.sel_key)), \
         mock.patch.object(tui, "_read_mouse",
                           return_value=(8, 3, curses.BUTTON1_CLICKED)):
        tui._main_loop(FakeScreen(), args)

    enable.assert_called_once_with()
    assert selected == [rows[0].key, rows[1].key]


# --- 明細 ------------------------------------------------------------------
def test_display_width_helpers():
    assert tui.disp_width("abc") == 3
    assert tui.disp_width("裝置") == 4          # CJK 全形各佔 2 欄
    assert tui.disp_width("6天前") == 5          # 1 半形 + 2 全形
    # pad_display 依顯示寬度補滿，不論 CJK
    assert tui.disp_width(tui.pad_display("裝置", 10)) == 10
    assert tui.disp_width(tui.pad_display("abc", 10)) == 10
    assert tui.pad_display("x", 5, "right") == "    x"
    # fit_display 不超過欄寬、且不會切半個全形字
    s, w = tui.fit_display("遠端路徑不存在", 5)
    assert w <= 5 and tui.disp_width(s) == w


def test_device_line_columns_align():
    # 不同 age 文字（天/小時/分鐘前）下，detail 前的固定欄位顯示寬度一致
    import types
    from datetime import datetime as _dt

    def fake(comp, age_secs):
        rec = types.SimpleNamespace(
            mode="download",
            started_at=_dt(2026, 7, 27, 1, 0, 0),
            file_count=5,
            success=5, skipped=0, failed=0,
            status="success", abort_reason="", errors=[], warnings=[],
        )
        d = types.SimpleNamespace(
            component=comp, latest=rec, is_stale=False, status="success",
            display_status="success", device_name="x", vessel="V", ipc="IPC-1",
            last_seen=_dt(2026, 7, 27, 1, 0, 0),
        )
        return d

    now = _dt(2026, 7, 27, 1, 30, 0)      # 30 分鐘前
    later = _dt(2026, 7, 30, 1, 0, 0)     # 3 天前
    fixed = 20 + 1 + 19 + 1 + 5 + 1 + 9 + 1 + 9
    w1 = tui.disp_width(tui.fit_display(tui._device_line(fake("ecdis", 0), now), fixed)[0])
    w2 = tui.disp_width(tui.fit_display(tui._device_line(fake("radar", 0), later), fixed)[0])
    assert w1 == w2 == fixed


def test_device_detail_lines(tmp_path):
    p = tmp_path / "D_CLINK_IPC-1_radar_0.csv"
    with open(p, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["timestamp", "device_name", "version_info", "level", "message"])
        w.writerow([RECENT, "CLINK_IPC-1_radar", "", "INFO", "=== SFTP 下載任務開始 ==="])
        w.writerow([RECENT, "CLINK_IPC-1_radar", "", "ERROR", "檔案 a/x.bin 下載失敗，放棄重試"])
        w.writerow([RECENT, "CLINK_IPC-1_radar", "", "INFO", "=== 下載任務結束：成功 0，略過 0，失敗 1 ==="])
        w.writerow([RECENT, "CLINK_IPC-1_radar", "", "INFO", "失敗清單：a/x.bin"])
    devices = aggregate_by_device(collect_logs(tmp_path), now=NOW, stale_hours=24)
    lines = device_detail_lines(devices[0])
    text = "\n".join(lines)
    assert "CLINK_IPC-1_radar" in text
    assert "失敗清單：a/x.bin" in text
    assert any("ERROR" in ln for ln in lines)


# --- CSV 原始資料檢視：版面純函式 ------------------------------------------
def test_slice_display_never_splits_wide_char():
    s = "2026-07-27 11:00:00 ERROR 檔案 a/x.bin 下載失敗"
    assert tui.slice_display(s, 0, 19) == "2026-07-27 11:00:00"
    # off 落在全形「檔」中間 → 補一格空白佔位，整列不左移、寬度不超過 cols
    assert tui.slice_display(s, 26, 6) == "檔案 a"
    half = tui.slice_display(s, 27, 6)
    assert half.startswith(" ") and "檔" not in half
    for off in range(0, tui.disp_width(s) + 2):
        assert tui.disp_width(tui.slice_display(s, off, 10)) <= 10
    # 位移超出整列寬度 → 空字串；cols 非正 → 空字串
    assert tui.slice_display(s, tui.disp_width(s), 10) == ""
    assert tui.slice_display(s, 0, 0) == ""


def test_slice_display_matches_fit_display_at_zero_offset():
    s = "檔案 a/x.bin 下載失敗，放棄重試"
    for cols in range(1, 30):
        assert tui.slice_display(s, 0, cols) == tui.fit_display(s, cols)[0]


_CSV_RAW = [
    ["2026-07-27 11:00:00", "CLINK_IPC-1_radar", "", "INFO", "=== SFTP 下載任務開始 ==="],
    ["2026-07-27 11:00:01", "CLINK_IPC-1_radar", "v1", "ERROR", "檔案 a/x.bin 下載失敗"],
]


def test_csv_rows_drops_constant_columns():
    rows = tui.csv_rows(_CSV_RAW)
    assert [r.level for r in rows] == ["INFO", "ERROR"]
    assert [r.ts for r in rows] == ["2026-07-27 11:00:00", "2026-07-27 11:00:01"]
    # device_name / version_info 全檔同值 → 放標題列，不進格線
    assert all("CLINK_IPC-1_radar" not in r.message and "v1" not in r.message for r in rows)


def test_csv_rows_flattens_control_chars_and_short_rows():
    rows = tui.csv_rows(
        [["2026-07-27 11:00:00", "dev", "", "ERROR", "第一行\n第二行\t尾"], ["ts", "dev"]]
    )
    assert rows[0].message == "第一行 第二行 尾"
    assert rows[1].level == "" and rows[1].message == ""  # 短列補齊，不炸 IndexError


def test_csv_line_pins_time_and_level():
    """釘住欄不隨 hoff 移動，只有訊息捲動——往右讀長訊息時仍看得到時間戳。"""
    rows = tui.csv_rows(_CSV_RAW)
    pinned = "2026-07-27 11:00:01 ERROR   "
    assert tui.disp_width(pinned) == tui._CSV_PINNED_W
    for hoff in (0, 4, 8, 40):
        line = tui.csv_line(rows[1], hoff, 80)
        assert line.startswith(pinned)  # 時間/級別永遠在原位
        assert tui.disp_width(line) <= 80
    # hoff 真的推動了訊息欄
    assert tui.csv_line(rows[1], 0, 80) != tui.csv_line(rows[1], 4, 80)
    # 訊息一律從 _CSV_PINNED_W 欄開始（CJK 也不例外）
    assert {tui.disp_width(tui.fit_display(tui.csv_line(r, 0, 80), tui._CSV_PINNED_W)[0])
            for r in rows} == {tui._CSV_PINNED_W}


def test_csv_line_survives_terminal_narrower_than_pinned_columns():
    rows = tui.csv_rows(_CSV_RAW)
    for width in (1, 5, 27, 28, 29):
        assert tui.disp_width(tui.csv_line(rows[0], 0, width)) <= width


def test_csv_header_line_aligns_with_body_and_shows_hscroll():
    rows = tui.csv_rows(_CSV_RAW)
    body = tui.csv_line(rows[0], 16, 80)
    header = tui.csv_header_line(16, 80)
    # 表頭與內文的訊息欄起點一致
    assert tui.disp_width(tui.fit_display(header, tui._CSV_PINNED_W)[0]) == \
        tui.disp_width(tui.fit_display(body, tui._CSV_PINNED_W)[0])
    assert "←16" in header          # 已捲掉 16 欄的提示
    assert "←" not in tui.csv_header_line(0, 80)
    assert tui.csv_header_line(0, 80).startswith("時間")


def test_csv_sort_rows():
    raw = [
        ["t1", "d", "", "INFO", "b 訊息"],
        ["t2", "d", "", "ERROR", "c 訊息"],
        ["t3", "d", "", "WARNING", "a 訊息"],
        ["t4", "d", "", "INFO", "b 訊息"],
    ]
    rows = tui.csv_rows(raw)

    # 原序：不動；降冪＝反轉（檔尾最新在前）
    assert [r.ts for r in tui.csv_sort_rows(rows, "原序", False)] == ["t1", "t2", "t3", "t4"]
    assert [r.ts for r in tui.csv_sort_rows(rows, "原序", True)] == ["t4", "t3", "t2", "t1"]

    # 級別依嚴重度而非字母序；降冪把 ERROR 推到最上面做 triage
    assert [r.level for r in tui.csv_sort_rows(rows, "級別", True)] == [
        "ERROR", "WARNING", "INFO", "INFO"
    ]
    assert [r.level for r in tui.csv_sort_rows(rows, "級別", False)][0] == "INFO"

    # 穩定排序：同級別維持原始時間順序，log 脈絡不被打散
    assert [r.ts for r in tui.csv_sort_rows(rows, "級別", True)][-2:] == ["t1", "t4"]

    # 訊息：把重複樣式聚成一團
    assert [r.message for r in tui.csv_sort_rows(rows, "訊息", False)] == [
        "a 訊息", "b 訊息", "b 訊息", "c 訊息"
    ]

    # 未知級別不炸，排在最後（rank 0）
    unknown = tui.csv_rows([["t5", "d", "", "TRACE", "x"]]) + rows
    assert tui.csv_sort_rows(unknown, "級別", True)[-1].level == "TRACE"

    # 排序不會就地改動來源
    assert [r.ts for r in rows] == ["t1", "t2", "t3", "t4"]


def test_csv_sort_label():
    assert tui.csv_sort_label("級別", True) == "排序：級別↓"
    assert tui.csv_sort_label("原序", False) == "排序：原序↑"


def test_csv_sort_cycle_excludes_time():
    """時間在單執行緒 logger 的 log 裡單調遞增，依時間排等於原序 → 不佔一個循環狀態。"""
    assert tui._CSV_SORT_CYCLE == ["原序", "級別", "訊息"]


def test_clamp_scroll_and_hscroll():
    # 內容比畫面高 → 夾在 [0, total - view_h]
    assert tui.clamp_scroll(100, 10, 999) == 90
    assert tui.clamp_scroll(100, 10, -5) == 0
    # 內容比畫面短 → 不能捲
    assert tui.clamp_scroll(3, 10, 5) == 0
    # view_h 為 0/負（極小終端機）不得產生負上限
    assert tui.clamp_scroll(5, 0, 99) == 4

    assert tui.clamp_hscroll(100, 40, 999) == 60
    assert tui.clamp_hscroll(100, 40, -5) == 0
    assert tui.clamp_hscroll(10, 40, 5) == 0  # 最寬列比畫面窄 → 不能水平捲
    assert tui.clamp_hscroll(10, 0, 99) == 9


def test_device_drilldown_enter_descends_other_key_exits():
    """明細 ↔ CSV 的控制流：Enter 往下鑽、非 Enter 離開，且每輪都先重畫主畫面。"""
    dev = SimpleNamespace(latest=SimpleNamespace(path="x.csv"))
    repaints = []
    seen = []

    # Enter、Enter、q → 進 CSV 兩次後離開
    keys = iter([13, curses.KEY_ENTER, ord("q")])
    with mock.patch.object(tui, "_popup", side_effect=lambda *a, **k: next(keys)), \
         mock.patch.object(tui, "device_detail_lines", return_value=["明細"]), \
         mock.patch.object(tui, "_csv_viewer", side_effect=lambda s, rec, w: seen.append(rec)):
        tui._device_drilldown(None, dev, 0, repaint=lambda: repaints.append(1))

    assert seen == [dev.latest, dev.latest]
    # 三次彈窗前各重畫一次：不先重畫，明細會疊在 CSV 全螢幕殘影上
    assert len(repaints) == 3


def test_device_drilldown_space_closes_instead_of_drilling():
    """Space 在主列表等同 Enter，但在彈窗裡只關閉——想關卻更深入一層很惱人。"""
    dev = SimpleNamespace(latest=SimpleNamespace(path="x.csv"))
    with mock.patch.object(tui, "_popup", return_value=ord(" ")), \
         mock.patch.object(tui, "device_detail_lines", return_value=["明細"]), \
         mock.patch.object(tui, "_csv_viewer") as viewer:
        tui._device_drilldown(None, dev, 0, repaint=lambda: None)
    viewer.assert_not_called()


def test_csv_key_action():
    assert tui.csv_key_action(ord("q")) == "close"
    assert tui.csv_key_action(27) == "close"          # Esc 逐層往上
    assert tui.csv_key_action(curses.KEY_UP) == "up"
    assert tui.csv_key_action(ord("j")) == "down"
    assert tui.csv_key_action(curses.KEY_NPAGE) == "pgdn"
    assert tui.csv_key_action(ord("G")) == "bottom"
    assert tui.csv_key_action(curses.KEY_LEFT) == "left"
    assert tui.csv_key_action(ord("l")) == "right"
    assert tui.csv_key_action(ord("0")) == "hreset"
    assert tui.csv_key_action(curses.KEY_HOME) == "hreset"
    assert tui.csv_key_action(ord("s")) == "sort_col"
    assert tui.csv_key_action(ord("S")) == "sort_dir"
    assert tui.csv_key_action(ord("z")) is None
    assert tui.csv_key_action(-1) is None             # watch 逾時不該當成按鍵


def test_csv_mouse_action():
    assert tui.csv_mouse_action(5, curses.BUTTON4_PRESSED, 20) == "wheel_up"
    assert tui.csv_mouse_action(
        5, curses.BUTTON_SHIFT | curses.BUTTON4_PRESSED, 20
    ) == "left"
    if BUTTON5_PRESSED is not None:
        assert tui.csv_mouse_action(5, BUTTON5_PRESSED, 20) == "wheel_down"
        assert tui.csv_mouse_action(
            5, curses.BUTTON_SHIFT | BUTTON5_PRESSED, 20
        ) == "right"
    assert tui.csv_mouse_action(5, curses.BUTTON3_CLICKED, 20) == "close"
    assert tui.csv_mouse_action(19, curses.BUTTON1_CLICKED, 20) == "close"
    assert tui.csv_mouse_action(5, curses.BUTTON1_CLICKED, 20) is None


def test_csv_apply_scroll_and_sort():
    v = tui.CsvView()
    assert (v.sort_key, v.desc, v.off, v.hoff) == ("原序", False, 0, 0)

    geom = dict(total=100, view_h=10)
    tui.csv_apply(v, "down", **geom); assert v.off == 1
    tui.csv_apply(v, "wheel_down", **geom); assert v.off == 1 + tui._MOUSE_SCROLL_LINES
    tui.csv_apply(v, "wheel_up", **geom); assert v.off == 1
    tui.csv_apply(v, "pgdn", **geom); assert v.off == 11
    tui.csv_apply(v, "pgup", **geom); assert v.off == 1
    tui.csv_apply(v, "bottom", **geom); assert v.off == 100  # 由 clamp_scroll 收尾
    tui.csv_apply(v, "top", **geom); assert v.off == 0

    tui.csv_apply(v, "right", **geom); assert v.hoff == tui._CSV_HSTEP
    tui.csv_apply(v, "right", **geom); assert v.hoff == tui._CSV_HSTEP * 2
    tui.csv_apply(v, "left", **geom); assert v.hoff == tui._CSV_HSTEP
    tui.csv_apply(v, "hreset", **geom); assert v.hoff == 0

    # s 循環欄位、S 切換升降；兩者都把垂直位移歸零（位置已無意義）
    v.off = 50
    tui.csv_apply(v, "sort_col", **geom)
    assert (v.sort_key, v.off) == ("級別", 0)
    tui.csv_apply(v, "sort_col", **geom); assert v.sort_key == "訊息"
    tui.csv_apply(v, "sort_col", **geom); assert v.sort_key == "原序"  # 繞回
    v.off = 50
    tui.csv_apply(v, "sort_dir", **geom)
    assert (v.desc, v.off) == (True, 0)
    tui.csv_apply(v, "sort_dir", **geom); assert v.desc is False
    # 排序切換不動水平位移
    v.hoff = 24
    tui.csv_apply(v, "sort_col", **geom)
    assert v.hoff == 24


# --- 平坦模式與排序 --------------------------------------------------------
def _sorted_tree(tmp_path):
    """四種嚴重度/身分的裝置：success、partial、stale，加一台無法解析船名的。"""
    return _tree(
        tmp_path,
        [
            ("CLINK_IPC-1_ecdis", "download", RECENT, 5, 0, 0),   # success
            ("CLINK_IPC-1_radar", "download", RECENT, 0, 0, 2),   # partial
            ("CLINK_IPC-2_share", "download", OLD, 5, 0, 0),      # stale
            ("RADAR_UPLOADER", "upload", RECENT, 1, 0, 0),        # 無 vessel/ipc
        ],
    )


def test_flat_rows_are_single_level_devices():
    st = tui.TuiState(flat=True)
    assert tui.visible_rows([], st, NOW) == []


def test_flat_rows_use_tree_group_names(tmp_path):
    """key 的群組名必須取自樹（已把 None 正規化），否則切換模式時選取會掉。"""
    tree = _sorted_tree(tmp_path)
    flat = tui.flatten_flat(tree, tui.TuiState(flat=True), NOW)
    assert {r.kind for r in flat} == {"device"}
    assert {r.depth for r in flat} == {0}

    st_grouped = tui.TuiState()
    tui.expand_all(st_grouped, tree)
    grouped = tui.flatten_tree(tree, st_grouped, NOW)
    # 平坦與分群的裝置 key 必須是同一組
    assert {r.key for r in flat} == {r.key for r in grouped if r.kind == "device"}
    # 無法解析船名者用樹的 sentinel，不是 d.vessel（None）
    assert any(k[2] == "（未分類）" and k[3] == "—" for k in (r.key for r in flat))


def test_flat_default_is_global_vessel_order(tmp_path):
    """平坦模式預設＝全域船隻名稱遞增，樹的順序當 tiebreak。"""
    tree = _sorted_tree(tmp_path)
    devs = [it.dev for it in tui.tree_devices(tree)]
    flat = tui.flatten_flat(tree, tui.TuiState(flat=True), NOW)
    assert [r.ref for r in flat] == tui.sort_devices(devs, "船隻名稱", False)
    vessels = [r.ref.vessel or "（未分類）" for r in flat]
    assert vessels == sorted(vessels, key=str.casefold)


def test_grouped_default_keeps_device_order_within_ipc(tmp_path):
    """預設（船隻名稱↑）不動 IPC 底下的裝置列：那一層仍是 build_tree 的 (-嚴重度, 元件)。

    船名對整個 IPC 群組是常數，作用在裝置列上必然是 no-op——它排的是船群那一層。
    """
    tree = _sorted_tree(tmp_path)
    st = tui.TuiState()
    tui.expand_all(st, tree)
    shown = [r.ref for r in tui.flatten_tree(tree, st, NOW) if r.kind == "device"]
    from_tree = [
        d for m in tree for v in m.vessels for ip in v.ipcs for d in ip.devices
    ]
    assert shown == from_tree


def _multi_vessel_tree(tmp_path):
    """同一方向下三艘船＋一台無法解析船名的，用來驗證船群那一層的排序。"""
    return _tree(
        tmp_path,
        [
            ("MMM_IPC-1_ecdis", "download", RECENT, 5, 0, 0),
            ("aaa_IPC-1_ecdis", "download", OLD, 5, 0, 0),     # stale：資料層會排到最前
            ("ZZZ_IPC-1_ecdis", "download", RECENT, 5, 0, 0),
            ("RADAR_UPLOADER", "download", RECENT, 1, 0, 0),   # 無 vessel → （未分類）
        ],
    )


def _vessel_names(tree, st):
    return [r.ref.name for r in tui.flatten_tree(tree, st, NOW) if r.kind == "vessel"]


def test_grouped_vessel_sort_reorders_vessel_groups(tmp_path):
    """「船隻名稱」在分群模式必須重排船群，否則這個欄位在分群模式是死鍵。"""
    tree = _multi_vessel_tree(tmp_path)
    st = tui.TuiState(sort_key="船隻名稱", sort_desc=False)
    tui.expand_all(st, tree)
    asc = _vessel_names(tree, st)
    assert asc == ["aaa", "MMM", "ZZZ", "（未分類）"]  # casefold：aaa 不因小寫墊底

    st.sort_desc = True
    assert _vessel_names(tree, st) == list(reversed(asc))


def test_grouped_vessel_sort_matches_flat_vessel_order(tmp_path):
    """兩種檢視的船名次序必須一致（含 （未分類） sentinel 的位置）。"""
    tree = _multi_vessel_tree(tmp_path)
    for desc in (False, True):
        st = tui.TuiState(sort_key="船隻名稱", sort_desc=desc)
        tui.expand_all(st, tree)
        flat = tui.flatten_flat(tree, tui.TuiState(flat=True, sort_desc=desc), NOW)
        flat_order = list(dict.fromkeys(r.key[2] for r in flat))
        assert _vessel_names(tree, st) == flat_order


def test_grouped_other_sort_keys_keep_data_layer_vessel_order(tmp_path):
    """非船名欄位在群組層沒有單一值可比，船群維持 build_tree 的次序（過期在前、未分類墊底）。"""
    tree = _multi_vessel_tree(tmp_path)
    from_tree = [v.name for m in tree for v in m.vessels]
    assert from_tree[0] == "aaa" and from_tree[-1] == "（未分類）"  # 資料層：嚴重度優先
    for key in ("更新時間", "嚴重度", "裝置名稱"):
        for desc in (False, True):
            st = tui.TuiState(sort_key=key, sort_desc=desc)
            tui.expand_all(st, tree)
            assert _vessel_names(tree, st) == from_tree


def test_sort_value_and_missing_last_seen():
    from datetime import datetime as _dt
    d = SimpleNamespace(last_seen=None, device_name="ECDIS", vessel="CLINK",
                        display_status="nonsense")
    assert tui.sort_value(d, "更新時間") == _dt.min      # 從未回報視為最舊
    assert tui.sort_value(d, "船隻名稱") == "clink"
    assert tui.sort_value(d, "裝置名稱") == "ecdis"      # casefold，大小寫不影響排序
    assert tui.sort_value(d, "嚴重度") == 0              # 未知狀態當健康


def test_sort_devices_by_each_field(tmp_path):
    tree = _sorted_tree(tmp_path)
    devs = [it.dev for it in tui.tree_devices(tree)]

    sev_desc = tui.sort_devices(devs, "嚴重度", True)
    assert sev_desc[0].display_status == "partial"        # 最嚴重在前
    assert sev_desc[-1].display_status == "success"
    assert tui.sort_devices(devs, "嚴重度", False)[0].display_status == "success"

    # 更新時間升冪＝最久未更新在前（找失聯裝置）
    oldest_first = tui.sort_devices(devs, "更新時間", False)
    assert oldest_first[0].component == "share"           # OLD
    assert tui.sort_devices(devs, "更新時間", True)[-1].component == "share"

    names = [d.device_name for d in tui.sort_devices(devs, "裝置名稱", False)]
    assert names == sorted(names, key=str.casefold)

    vessels = [d.vessel or "（未分類）" for d in tui.sort_devices(devs, "船隻名稱", False)]
    assert vessels == sorted(vessels, key=str.casefold)

    # 穩定排序：同嚴重度維持輸入（資料層）順序
    same = [d for d in devs if d.display_status == "success"]
    assert tui.sort_devices(same, "嚴重度", True) == same
    # 不就地改動來源
    before = list(devs)
    tui.sort_devices(devs, "裝置名稱", True)
    assert devs == before


def test_flat_honours_filters(tmp_path):
    tree = _sorted_tree(tmp_path)
    st = tui.TuiState(flat=True)
    assert len(tui.flatten_flat(tree, st, NOW)) == 4

    st.only_problem = True                     # 非 success（partial + stale）
    assert len(tui.flatten_flat(tree, st, NOW)) == 2

    st.only_problem = False
    st.mode = "upload"
    assert [r.ref.component for r in tui.flatten_flat(tree, st, NOW)] == ["RADAR_UPLOADER"]

    st.mode = ""
    st.query = "share"
    assert [r.ref.component for r in tui.flatten_flat(tree, st, NOW)] == ["share"]


def test_selection_survives_view_toggle_even_when_collapsed(tmp_path):
    """切平坦再切回來，游標要停在同一台裝置——這是 reveal() 的回歸測試。"""
    tree = _sorted_tree(tmp_path)
    st = tui.TuiState()
    tui.expand_all(st, tree)
    grouped = tui.flatten_tree(tree, st, NOW)
    target = [r for r in grouped if r.kind == "device"][2].key
    st.sel_key = target

    tui.toggle_flat(st)                        # → 平坦
    flat = tui.flatten_flat(tree, st, NOW)
    tui.clamp_selection(flat, st)
    assert st.sel_key == target

    st.expanded.clear()                        # 模擬使用者在平坦模式期間全部收合
    tui.toggle_flat(st)                        # → 回分群，reveal 應展開祖先
    back = tui.flatten_tree(tree, st, NOW)
    tui.clamp_selection(back, st)
    assert st.sel_key == target


def test_collapse_or_parent_does_not_jump_in_flat(tmp_path):
    tree = _sorted_tree(tmp_path)
    st = tui.TuiState()
    tui.expand_all(st, tree)
    dev_row = [r for r in tui.flatten_tree(tree, st, NOW) if r.kind == "device"][0]

    st.sel_key = dev_row.key
    tui.collapse_or_parent(st, dev_row)        # 分群：裝置列 ← 跳父群
    assert st.sel_key == tui.parent_key(dev_row.key)

    st.flat = True
    st.sel_key = dev_row.key
    tui.collapse_or_parent(st, dev_row)        # 平坦：沒有父群，不能亂跳
    assert st.sel_key == dev_row.key

    # 群組列仍是先收合自己
    grp = [r for r in tui.flatten_tree(tree, tui.TuiState(), NOW) if r.kind == "mode"][0]
    st2 = tui.TuiState()
    st2.expanded.add(grp.key)
    tui.collapse_or_parent(st2, grp)
    assert grp.key not in st2.expanded

    tui.collapse_or_parent(st2, None)          # 空列表不炸


def test_sort_reducers_and_defaults():
    st = tui.TuiState()
    assert (st.flat, st.sort_key, st.sort_desc) == (False, "船隻名稱", False)
    assert tui._SORT_CYCLE == ["船隻名稱", "更新時間", "嚴重度", "裝置名稱", "版本"]

    tui.cycle_sort(st); assert st.sort_key == "更新時間"
    tui.cycle_sort(st); assert st.sort_key == "嚴重度"
    tui.cycle_sort(st); assert st.sort_key == "裝置名稱"
    tui.cycle_sort(st); assert st.sort_key == "版本"
    tui.cycle_sort(st); assert st.sort_key == "船隻名稱"    # 繞回
    st.sort_key = "亂填"
    tui.cycle_sort(st); assert st.sort_key == "更新時間"     # 不在循環內也不炸

    tui.toggle_sort_dir(st); assert st.sort_desc is True
    tui.toggle_sort_dir(st); assert st.sort_desc is False

    st.scroll = 9
    tui.toggle_flat(st)
    assert st.flat is True and st.scroll == 0
    assert tui.sort_label("更新時間", False) == "排序：更新時間↑"


def test_grouped_mode_also_sorts_devices(tmp_path):
    """o/O 在分群模式不是死鍵：套在 IPC 群組內的裝置列，群組本身順序不動。"""
    tree = _tree(
        tmp_path,
        [
            ("CLINK_IPC-1_aaa", "download", RECENT, 5, 0, 0),
            ("CLINK_IPC-1_zzz", "download", RECENT, 5, 0, 0),
        ],
    )
    st = tui.TuiState()
    tui.expand_all(st, tree)
    asc = [r.ref.component for r in tui.flatten_tree(tree, st, NOW) if r.kind == "device"]

    st.sort_key, st.sort_desc = "裝置名稱", True
    desc = [r.ref.component for r in tui.flatten_tree(tree, st, NOW) if r.kind == "device"]
    assert desc == list(reversed(asc))
    # 群組列仍在（順序由資料層決定，不受排序影響）
    assert [r.kind for r in tui.flatten_tree(tree, st, NOW)][:3] == ["mode", "vessel", "ipc"]


def test_flat_line_and_header_align():
    """欄位起點由 _FLAT_COLS 單一來源保證：CJK 船名與不同 age 文字都不該讓欄位位移。"""
    fixed = sum(tui._FLAT_COLS) + len(tui._FLAT_COLS) - 1

    def item(vessel, comp, age_now):
        rec = SimpleNamespace(mode="download", started_at=NOW, file_count=5,
                              success=5, skipped=0, failed=0, status="success",
                              abort_reason="", errors=[], warnings=[])
        dev = SimpleNamespace(component=comp, latest=rec, is_stale=False,
                              status="success", display_status="success",
                              device_name="x", vessel=vessel, ipc="IPC-1",
                              last_seen=age_now)
        return tui.FlatItem("download", vessel, "IPC-1", dev)

    from datetime import datetime as _dt
    a = tui._device_line_flat(item("（未分類）", "ecdis", _dt(2026, 7, 27, 11, 30)), NOW)
    b = tui._device_line_flat(item("CLINK", "SHM-stream-manager", _dt(2026, 7, 20)), NOW)
    assert tui.disp_width(tui.fit_display(a, fixed)[0]) == fixed
    assert tui.disp_width(tui.fit_display(b, fixed)[0]) == fixed

    header = tui.flat_header_line()
    assert header.startswith(tui._FLAT_GUTTER)
    body = header[len(tui._FLAT_GUTTER):]
    assert tui.disp_width(tui.fit_display(body, fixed)[0]) == fixed
    assert header.rstrip().endswith("摘要")
    # 每個欄名都必須真的塞得進自己的欄寬（全形字放進 1 欄寬會被整個丟掉）
    for label, w in zip(["↕", "船", "IPC", "元件", "最後執行", "檔案", "成/略/失", "距今"],
                        tui._FLAT_COLS):
        assert tui.disp_width(label) <= w, f"欄名 {label!r} 放不進 {w} 欄"
        assert label in body


def test_footer_hint_and_body_height_follow_view():
    flat, grouped = tui.TuiState(flat=True), tui.TuiState()
    assert "f分群" in tui.footer_hint(flat) and "全展收" not in tui.footer_hint(flat)
    assert "o欄位/O升降" in tui.footer_hint(flat)
    assert "E/C全展收" in tui.footer_hint(grouped) and "f平坦" in tui.footer_hint(grouped)
    # 平坦多一行凍結欄名
    assert tui.body_height(24, flat) == 20
    assert tui.body_height(24, grouped) == 21
    assert tui.body_height(2, flat) >= 1        # 極小終端機不得為 0/負


def test_key_action_flat_and_sort():
    assert tui.key_action(ord("f")) == "toggle_flat"
    assert tui.key_action(ord("o")) == "sort_field"
    assert tui.key_action(ord("O")) == "sort_dir"
    assert tui.key_action(ord("s")) == "cycle_status"   # s 仍是狀態過濾，沒被搶走


def test_key_press_reorders_flat_list(tmp_path):
    """完整接線：按鍵 → key_action → reducer → visible_rows 的順序真的改變。

    純邏輯層做這件事比在 pty 上看畫面可靠——curses 只重送有變化的儲存格，
    重建畫面必有殘影，容易誤判成「排序沒生效」。
    """
    tree = _sorted_tree(tmp_path)
    st = tui.TuiState()
    dispatch = {
        "toggle_flat": tui.toggle_flat,
        "sort_field": tui.cycle_sort,
        "sort_dir": tui.toggle_sort_dir,
    }

    def press(ch):
        act = tui.key_action(ch)
        assert act in dispatch, f"{ch!r} 沒有對應動作"
        dispatch[act](st)
        # 分群模式會夾雜群組列，只取裝置列的名字比順序
        return [r.ref.device_name for r in tui.visible_rows(tree, st, NOW)
                if r.kind == "device"]

    order_vessel = press(ord("f"))              # → 平坦，船隻名稱↑（預設升冪）
    assert st.flat is True
    assert len(order_vessel) == 4               # 4 台裝置全部單層列出

    order_time = press(ord("o"))                # → 更新時間↑
    assert st.sort_key == "更新時間"
    assert order_time != order_vessel           # 真的重排了
    assert order_time[0] == "CLINK_IPC-2_share"      # OLD＝最久未更新，升冪排最前
    # ecdis / radar / RADAR_UPLOADER 的時間戳都是 RECENT（平手）→ 穩定排序沿用樹序
    assert set(order_time[1:]) == {
        "CLINK_IPC-1_radar", "CLINK_IPC-1_ecdis", "RADAR_UPLOADER"
    }

    order_time_desc = press(ord("O"))           # → 更新時間↓
    assert st.sort_desc is True
    assert order_time_desc[-1] == "CLINK_IPC-2_share"  # OLD＝最舊，降冪排最後

    press(ord("O"))                             # → 切回升冪，後續欄位都在升冪下比對
    assert st.sort_desc is False

    order_severity = press(ord("o"))            # → 嚴重度↑
    assert st.sort_key == "嚴重度"
    severity_by_device = {
        r.ref.device_name: tui._SEVERITY.get(r.ref.display_status, 0)
        for r in tui.visible_rows(tree, st, NOW) if r.kind == "device"
    }
    assert [severity_by_device[n] for n in order_severity] == sorted(
        severity_by_device[n] for n in order_severity
    )

    order_name = press(ord("o"))                # → 裝置名稱↑
    assert st.sort_key == "裝置名稱"
    assert order_name == sorted(order_name, key=str.casefold)

    back = press(ord("f"))                      # → 回分群
    assert st.flat is False
    assert [tui.key_action(c) for c in b"foO"] == ["toggle_flat", "sort_field", "sort_dir"]
    # 群組都還收合著（sel_key 為 None 時 reveal 無事可做）→ 只有頂層 mode 列
    assert back == []
    assert {r.kind for r in tui.visible_rows(tree, st, NOW)} == {"mode"}
    # 展開後裝置回來，且沿用剛才選的排序欄位
    tui.expand_all(st, tree)
    names = [r.ref.device_name for r in tui.visible_rows(tree, st, NOW) if r.kind == "device"]
    assert names == sorted(names, key=str.casefold)


# --- 版本欄：向上相容（有些專案沒有版號）-----------------------------------
def _dev_with_version(version):
    return SimpleNamespace(latest=SimpleNamespace(version_info=version))


def _col_start(line, needle):
    """needle 在該列的**顯示**起點（CJK 全形算 2 欄，不能用字元索引比）。"""
    return tui.disp_width(line[:line.index(needle)])


def test_version_str_handles_missing_version():
    """沒有版本是正常狀態（舊 log / 尚未宣告 VERSION.json 的專案），要顯示破折號而非爆掉。"""
    assert tui._version_str(SimpleNamespace(version_info="0.4.1+20b8056")) == "0.4.1+20b8056"
    assert tui._version_str(SimpleNamespace(version_info="")) == "—"
    assert tui._version_str(SimpleNamespace(version_info="   ")) == "—"
    assert tui._version_str(SimpleNamespace(version_info=None)) == "—"
    assert tui._version_str(SimpleNamespace()) == "—"          # 連欄位都沒有的舊物件


def test_device_line_shows_version_without_growing_the_row(tmp_path):
    """版本欄的寬度是跟摘要借的：分群模式的列已有 3 層縮排，整列再長 17 欄會把摘要推出畫面。

    摘要短的時候整列本來就不會補滿，所以這裡把摘要灌長來逼出上限，比的是「最寬會多寬」。
    """
    tree = _tree(tmp_path, [("WH289_IPC-1_RADAR", "download", RECENT, 5, 1, 0, "0.4.1+20b8056")])
    dev = tree[0].vessels[0].ipcs[0].devices[0]
    with mock.patch.object(tui, "_detail_str", return_value="x" * 200):
        with_v = tui._device_line(dev, NOW, True)
        without_v = tui._device_line(dev, NOW, False)
    assert "0.4.1+20b8056" in with_v
    assert "0.4.1+20b8056" not in without_v
    assert tui.disp_width(with_v) == tui.disp_width(without_v)


def test_device_line_without_version_shows_dash(tmp_path):
    tree = _tree(tmp_path, [("WH289_IPC-1_ecdis", "download", RECENT, 5, 1, 0)])
    dev = tree[0].vessels[0].ipcs[0].devices[0]
    assert "—" in tui._device_line(dev, NOW, True)


def test_flat_layout_inserts_version_after_component():
    """版本插在「方向 船 IPC 元件」之後：身分後面接版本才好讀。"""
    cols_off, aligns_off = tui._flat_layout(False)
    cols_on, aligns_on = tui._flat_layout(True)
    assert cols_off == tui._FLAT_COLS
    assert len(cols_on) == len(cols_off) + 1
    assert cols_on[4] == tui._VERSION_W and aligns_on[4] == "left"
    assert cols_on[:4] == cols_off[:4] and cols_on[5:] == cols_off[4:]


def test_flat_header_and_row_agree_on_columns(tmp_path):
    """表頭與內文共用同一份版面 —— 兩邊各算一次遲早會錯開。"""
    tree = _tree(tmp_path, [("WH289_IPC-1_RADAR", "download", RECENT, 5, 1, 0, "0.10.0+811a5c3")])
    item = tui.tree_devices(tree)[0]
    for show in (True, False):
        header = tui.flat_header_line(show)
        row = tui._FLAT_GUTTER + tui._device_line_flat(item, NOW, show)
        assert ("版本" in header) is show
        assert ("0.10.0+811a5c3" in row) is show
        # 「最後執行」這一欄在表頭與內文的顯示起點必須一致（欄位起點只有一個來源）
        assert _col_start(header, "最後執行") == _col_start(row, "07-27 11:00")


def test_sort_by_version_groups_same_version_and_pushes_missing_last():
    """版本排序是把同版本的船聚在一起；沒有版本的排最後（一整排破折號排前面只會擋路）。"""
    assert tui.sort_value(_dev_with_version("0.4.1+aaa"), "版本") == "0.4.1+aaa"
    assert tui.sort_value(_dev_with_version(""), "版本") == "~"
    devs = [_dev_with_version(""), _dev_with_version("0.4.1+aaa"), _dev_with_version("0.4.1+aaa")]
    ordered = sorted(devs, key=lambda d: tui.sort_value(d, "版本"))
    assert [tui.sort_value(d, "版本") for d in ordered] == ["0.4.1+aaa", "0.4.1+aaa", "~"]


def test_toggle_version_flag():
    st = tui.TuiState()
    assert st.show_version is True                 # 預設顯示：這是最常要的答案
    tui.toggle_version(st); assert st.show_version is False
    tui.toggle_version(st); assert st.show_version is True


def test_key_v_maps_to_toggle_version():
    assert tui.key_action(ord("v")) == "toggle_version"

