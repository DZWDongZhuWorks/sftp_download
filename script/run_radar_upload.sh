#!/usr/bin/env bash
# 發布 radar（開發端 → STANDARD/radar）。
#
# 版本標記：main.py 會在上傳前自動執行 radar/tools/stamp_version.py 產生 VERSION.stamp.json
# （船上沒有 .git，算不出自己是哪個 commit，所以只能在發布端產生），並把版本字串填進
# log CSV 的 version_info 欄 —— 見 main.py 的 _apply_version_stamp。
# 因此不論走這支、run_all_uploads.py、run_selected_transfers.py 還是手動
# `main.py --cli --mode upload --config config/radar_upload_settings.json`，
# 都會帶上版本標記；這支只是慣例上的具名入口（比照 run_share_upload.sh）。
# 例外：GUI 不走 run_cli，用 GUI 上傳 radar 前請自行執行一次 radar/tools/stamp_version.py。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="$(dirname "$SCRIPT_DIR")"

# 切換到專案根目錄，讓設定檔中的相對路徑（如 ignore_file: config/xxx_ignore.txt）
# 無論從哪個目錄或排程 (cron) 執行都能正確解析。
cd "$BASE_DIR"

# 注意：上傳「刻意不套用」_dev_guard.sh 的 CLINK 守門。CLINK 是 STANDARD 的發佈源頭，
# 發佈動作正是要從這台往上傳；守門只用於下載（避免 STANDARD 覆蓋開發端未提交的修改）。
config="$SCRIPT_DIR/../config/radar_upload_settings.json"

if [[ ! -f "$config" ]]; then
    echo "找不到設定檔: $config" >&2
    exit 1
fi

# 使用 sftp_transfer 專屬 venv 的 Python 啟動（離線部署由 deploy/deploy_offline.sh 建立）
VENV_PY="${SFTP_TRANSFER_VENV:-$HOME/venv/wanhai_nssms/share/sftp_transfer}/bin/python"

if [[ ! -x "$VENV_PY" ]]; then
    echo "找不到 sftp_transfer 專屬 venv 的 Python: $VENV_PY" >&2
    echo "請先執行 deploy/deploy_offline.sh 建立 venv。" >&2
    exit 1
fi

"$VENV_PY" "$BASE_DIR/main.py" --cli --mode upload --config "$config"
