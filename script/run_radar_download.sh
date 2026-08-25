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

# 記下「下載前」的本機 radar 版本。main.py 會把它寫進 log CSV 的 version_info 欄，
# 而那份 log 依 radar_download_settings.json 的 log_remote_dir 自動上傳到岸端
# sftp_logs/download/{vsl_name}/{ipc}/radar，岸端用 monitor/tui.py 即可逐船看版本。
# 「下載後」的版本由 scheduler 的 reboot_script/start_radar.sh 記進 launcher.log：
# 兩者相同就表示這次 OTA 沒有換版。
# 版本取自 radar 的 VERSION.json（發布端 tools/stamp_version.py 產生，隨鏡像上船）；
# 取不到（還沒 stamp、OTA 不完整、沒有 python3）一律以 unknown 帶過，絕不擋下下載。
RADAR_DIR="$BASE_DIR/../../radar"
radar_version="unknown"
if command -v python3 >/dev/null 2>&1 && [[ -f "$RADAR_DIR/tools/stamp_version.py" ]]; then
    radar_version="$(python3 "$RADAR_DIR/tools/stamp_version.py" --print 2>/dev/null || true)"
    [[ -z "$radar_version" ]] && radar_version="unknown"
fi
echo "radar 版本（下載前）: $radar_version"

"$VENV_PY" "$BASE_DIR/main.py" --cli --config "$config" --version-info "$radar_version"
