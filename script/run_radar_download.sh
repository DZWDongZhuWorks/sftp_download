#!/usr/bin/env bash
# 更新 radar
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="$(dirname "$SCRIPT_DIR")"

# 切換到專案根目錄，讓設定檔中的相對路徑（如 ignore_file: config/xxx_ignore.txt）
# 無論從哪個目錄或排程 (cron) 執行都能正確解析。
cd "$BASE_DIR"

# 開發機 (CLINK) 守門：見 _dev_guard.sh。radar 下載會 overwrite 覆蓋開發端 git repo，
# 在 CLINK 上一律略過，避免覆蓋未提交的修改。
source "$SCRIPT_DIR/_dev_guard.sh"
dev_guard "$BASE_DIR"

config="$SCRIPT_DIR/../config/radar_download_settings.json"

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

# 版本資訊不在這裡取：main.py 一看到 radar 根目錄有 VERSION.json，就會把「下載前」的
# radar 版本填進 log CSV 的 version_info 欄（見 version_stamp.py）。
# 放在 main.py 是因為下載也可能走 run_all_downloads.py / run_selected_transfers.py，
# 只在這支腳本裡取的話換條路就靜默失去版本資訊。
"$VENV_PY" "$BASE_DIR/main.py" --cli --config "$config"
