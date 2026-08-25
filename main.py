"""SFTP 自動化下載工具進入點。

不帶參數執行 -> 啟動 GUI。
帶參數執行   -> 依參數以 CLI 模式執行（適合排程自動化）。

參數優先順序：command line > settings.json > 內建預設值。
"""

import argparse
import getpass
import os
import subprocess
import sys
from pathlib import Path

from downloader import SFTPDownloader, create_logger
from settings import PlaceholderError, load_settings
from uploader import SFTPUploader

DEFAULT_LOG_DIR = Path(__file__).resolve().parent / "logs"


def build_parser():
    parser = argparse.ArgumentParser(description="SFTP 自動化下載工具")
    parser.add_argument(
        "--cli",
        action="store_true",
        help="強制以 CLI 模式執行（不開啟 GUI）。當所有必要參數都已寫在 settings.json 時，可單獨帶這個旗標即可，不需重複輸入其他參數",
    )
    parser.add_argument(
        "--config",
        help="指定要讀取的設定檔路徑（預設為工具資料夾內的 settings.json）。"
        "適合同一台裝置需要下載多組不同的 SFTP 來源/本地路徑時，每組各自用一份設定檔、各排一個排程任務",
    )
    parser.add_argument(
        "--mode",
        choices=["download", "upload"],
        help="傳輸方向：download=遠端→本地（預設）、upload=本地→遠端。"
        "upload 模式下 --local-path 為來源、--remote-path 為目的地",
    )
    parser.add_argument("--host", help="SFTP 主機位址")
    parser.add_argument("--port", type=int, help="SFTP 連接埠（預設 22）")
    parser.add_argument("--username", help="SFTP 帳號")
    parser.add_argument(
        "--device-name",
        help="裝置/使用者識別名稱，用於標示 Log 是哪一台設備所產生（多台 edge device 共用同一 SFTP 帳號時仍可分辨）",
    )
    parser.add_argument(
        "--version-info",
        help="選填的上傳版號資訊，會一併記錄在 Log 中，不影響下載邏輯",
    )
    parser.add_argument("--password", help="SFTP 密碼（可用環境變數 SFTP_PASSWORD 取代，避免明碼留在指令紀錄）")
    parser.add_argument("--key-file", help="SSH 私鑰檔路徑（若使用金鑰登入，取代 --password）")
    parser.add_argument(
        "--remote-path",
        action="append",
        help="SFTP 路徑（檔案或目錄）。download 模式為來源，可重複指定多次、多個來源會合併下載到同一個本地端"
        "路徑；upload 模式為目的地，僅使用單一路徑（重複指定時取第一個）",
    )
    parser.add_argument("--local-path", help="本地端路徑：download 模式為儲存目的地、upload 模式為上傳來源")
    parser.add_argument(
        "--ignore-file",
        help="下載忽略設定檔路徑，內容格式完全同 .gitignore，符合規則的檔案/資料夾不會被下載；"
        "找不到該檔案則代表無需忽略任何檔案",
    )

    parser.add_argument("--no-auto-reconnect", action="store_true", help="停用斷線自動重連")
    parser.add_argument("--no-resume", action="store_true", help="停用斷點續傳")
    parser.add_argument("--no-wait-network", action="store_true", help="停用網路偵測自動下載")
    parser.add_argument(
        "--no-recursive",
        action="store_true",
        help="停用多層下載，只下載來源路徑當層的檔案，略過所有子資料夾（預設會下載所有子資料夾）",
    )
    parser.add_argument("--retry-count", type=int, help="重試次數上限，0 或不指定代表無限次重試（預設無限次）")
    parser.add_argument("--retry-delay", type=int, help="重試間隔秒數（預設 10）")

    parser.add_argument("--upload-log", action="store_true", help="下載結束後將 Log 上傳回 SFTP")
    parser.add_argument("--log-remote-dir", help="上傳 Log 的 SFTP 目錄（搭配 --upload-log 使用）")
    parser.add_argument("--log-dir", help="本地端 Log 儲存目錄（預設 ./logs）")
    parser.add_argument(
        "--duplicate-mode",
        choices=["duplicate", "overwrite"],
        help="來源檔案偵測到已更新版本時的處理方式：overwrite=直接覆蓋舊檔案（預設）、duplicate=另存新檔",
    )
    parser.add_argument(
        "--duplicate-suffix",
        help="duplicate-mode 為 duplicate 時，另存新檔用的檔名後綴（預設 copy，第二次更新起會自動加上流水號 copy1、copy2...）",
    )
    return parser


# 待傳輸專案若自帶版本標記腳本，就放在這個相對路徑（目前只有 radar 有）。
VERSION_STAMP_REL = Path("tools") / "stamp_version.py"


def _version_stamp_script(local_path):
    """回傳 local_path 底下的版本標記腳本，沒有就回 None。

    這是一個**約定**而非某個專案的特例：任何專案只要在自己根目錄放一支
    `tools/stamp_version.py`（無參數 = 產生版本標記、`--print` = 印出單行版本字串），
    就自動獲得下面 _apply_version_stamp() 的行為，不必再改這裡。
    """
    if not local_path:
        return None
    try:
        script = (Path(local_path) / VERSION_STAMP_REL).resolve()
    except OSError:
        return None
    return script if script.is_file() else None


def _apply_version_stamp(mode, local_path, version_info):
    """上傳前產生版本標記；並在未指定 version_info 時以該版本填入 log 的 version_info 欄。

    為什麼放在這裡而不是某支 run_*.sh：發布/更新有很多條路（run_all_uploads.py、
    run_selected_transfers.py、run_radar_*.sh、手動 main.py --cli），run_cli 是它們**共同**
    的收口。放在單一腳本裡的話，換一條路走就靜默失去版本資訊。
    （GUI 不走 run_cli，另有自己的流程，不受這裡影響。）

    上傳 = 發布端動作，必須「上傳前」產生標記：船上沒有 .git，算不出自己是哪個 commit。
    下載時只讀不寫，取到的是「下載前」的版本 —— 那正是要記進 log 的（下載後的版本由
    scheduler 的 start_radar.sh 記進 launcher.log）。
    任何失敗都只警告：版本資訊是觀測用的，不該讓傳輸本身停擺。標記缺漏不會被吃掉 ——
    船上開機時的 sha256 驗證會顯示成 files:UNSTAMPED / files:MISMATCH。
    """
    script = _version_stamp_script(local_path)
    if script is None:
        return version_info
    if mode == "upload":
        print(f"=== 產生版本標記: {script} ===")
        try:
            if subprocess.call([sys.executable, str(script)], timeout=600) != 0:
                print("警告：產生版本標記失敗，這次上傳的內容將是「未標記版本」", file=sys.stderr)
        except (OSError, subprocess.SubprocessError) as e:
            print(f"警告：無法執行版本標記腳本（{e}）", file=sys.stderr)
    if version_info:
        return version_info      # 呼叫端明確指定過，不覆蓋
    try:
        # capture_output= / text= 是 3.7+，Bionic 船端只有 3.6（見 tests 的
        # ShipInterpreterCompatTests）。
        proc = subprocess.run([sys.executable, str(script), "--print"],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              universal_newlines=True, timeout=600)
        stamped = (proc.stdout or "").strip().splitlines()
        if stamped and stamped[0]:
            print(f"版本: {stamped[0]}")
            return stamped[0]
    except (OSError, subprocess.SubprocessError) as e:
        print(f"警告：無法取得版本字串（{e}）", file=sys.stderr)
    return "unknown"


def _resolve(cli_value, settings, key, fallback=None):
    if cli_value is not None:
        return cli_value
    return settings.get(key, fallback)


def run_cli(args):
    try:
        settings = load_settings(args.config) if args.config else load_settings()
    except PlaceholderError as e:
        print(f"錯誤：{e}", file=sys.stderr)
        return 1

    mode = args.mode or settings.get("mode") or "download"
    host = _resolve(args.host, settings, "host")
    port = _resolve(args.port, settings, "port", 22)
    device_name = _resolve(args.device_name, settings, "device_name")
    version_info = _resolve(args.version_info, settings, "version_info", "")
    username = _resolve(args.username, settings, "username")
    key_file = _resolve(args.key_file, settings, "key_file")
    remote_path = _resolve(args.remote_path, settings, "remote_path")
    local_path = _resolve(args.local_path, settings, "local_path")
    ignore_file = _resolve(args.ignore_file, settings, "ignore_file")
    retry_count = _resolve(args.retry_count, settings, "retry_count", None)
    retry_delay = _resolve(args.retry_delay, settings, "retry_delay", 10)
    log_remote_dir = _resolve(args.log_remote_dir, settings, "log_remote_dir")
    # log_dir 留空字串代表「未設定」，不像 retry_count=0 是有意義的值，因此用 or 串接才能正確回退到預設值。
    log_dir = args.log_dir or settings.get("log_dir") or str(DEFAULT_LOG_DIR)
    duplicate_mode = args.duplicate_mode or settings.get("duplicate_mode") or "overwrite"
    duplicate_suffix = args.duplicate_suffix or settings.get("duplicate_suffix") or "copy"

    # 布林旗標：settings.json 提供基準值，CLI 的 --no-* / --upload-log 只能單向覆蓋（關閉/開啟）。
    auto_reconnect = False if args.no_auto_reconnect else bool(settings.get("auto_reconnect", True))
    resume = False if args.no_resume else bool(settings.get("resume", True))
    wait_for_network = False if args.no_wait_network else bool(settings.get("wait_for_network", True))
    recursive = False if args.no_recursive else bool(settings.get("recursive", True))
    upload_log = True if args.upload_log else bool(settings.get("upload_log", False))

    missing = [
        name
        for name, value in (
            ("--host", host),
            ("--device-name", device_name),
            ("--username", username),
            ("--remote-path", remote_path),
            ("--local-path", local_path),
        )
        if not value
    ]
    if missing:
        print(f"錯誤：缺少必要參數 {', '.join(missing)}（可透過 command line 或 settings.json 提供）", file=sys.stderr)
        return 1
    if upload_log and not log_remote_dir:
        print("錯誤：啟用上傳 Log 時必須指定 --log-remote-dir（或設定檔中的 log_remote_dir）", file=sys.stderr)
        return 1

    password = args.password or os.environ.get("SFTP_PASSWORD") or settings.get("password")
    if not key_file and not password:
        password = getpass.getpass(f"請輸入 {username}@{host} 的密碼: ")

    # 待傳輸的專案自帶 tools/stamp_version.py 時：上傳前先產生版本標記，並把版本填進
    # log 的 version_info 欄（見 _apply_version_stamp）。其他專案不受影響。
    version_info = _apply_version_stamp(mode, local_path, version_info)

    logger, log_file = create_logger(log_dir, device_name, version_info, mode=mode)
    transfer_cls = SFTPUploader if mode == "upload" else SFTPDownloader
    transfer = transfer_cls(
        host=host,
        port=port,
        username=username,
        password=password,
        key_file=key_file,
        remote_path=remote_path,
        local_path=local_path,
        auto_reconnect=auto_reconnect,
        resume=resume,
        wait_for_network=wait_for_network,
        recursive=recursive,
        ignore_file=ignore_file or None,
        retry_count=retry_count,
        retry_delay=retry_delay,
        upload_log=upload_log,
        remote_log_dir=log_remote_dir,
        duplicate_mode=duplicate_mode,
        duplicate_suffix=duplicate_suffix,
        logger=logger,
        log_file=log_file,
    )
    success = transfer.run()
    return 0 if success else 1


def main():
    if len(sys.argv) == 1:
        from gui import launch_gui

        launch_gui()
        return 0

    parser = build_parser()
    args = parser.parse_args()
    return run_cli(args)


if __name__ == "__main__":
    sys.exit(main())
