#!/usr/bin/env bash
# 更新 IPC-3 的 share(alarm_controller / button)
# ---------------------------------------------------------------------------
# 與 run_share_download.sh 的關係:同樣是把 share 拉下來,但**遠端路徑不同**。
#
#   run_share_download.sh        IPC1_share_download_settings.json  → STANDARD/share
#   run_share_ipc3_download.sh   IPC3_share_download_settings.json  → STANDARD/share-IPC3
#
# 兩台要的東西不一樣:IPC-3 需要的是 alarm_controller 與 button(由
# nssms-alarm-controller-ipc3 / nssms-button-ipc3 兩支常駐 unit 跑),而 IPC-1 那份的
# remote_path 涵蓋的是它自己那一套。共用一份設定會讓其中一台拿到另一台的檔案。
#
# 呼叫者:scheduler 的 reboot_script/start_share_ipc3.sh(roles.conf 的
# `ipc3 share_ipc3 oneshot update`)。也可以手動執行。
# ---------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="$(dirname "$SCRIPT_DIR")"

# 切換到專案根目錄，讓設定檔中的相對路徑（如 ignore_file: config/xxx_ignore.txt）
# 無論從哪個目錄或排程 (cron) 執行都能正確解析。
cd "$BASE_DIR"

# 開發機 (CLINK) 守門：見 _dev_guard.sh。share 下載會 overwrite 覆蓋開發端工作區，
# 在 CLINK 上一律略過，避免覆蓋未提交的修改。
source "$SCRIPT_DIR/_dev_guard.sh"
dev_guard "$BASE_DIR"

config="$SCRIPT_DIR/../config/IPC3_share_download_settings.json"

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

"$VENV_PY" "$BASE_DIR/main.py" --cli --config "$config"
