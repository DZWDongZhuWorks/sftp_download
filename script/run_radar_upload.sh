#!/usr/bin/env bash
# 發布 radar（開發端 → STANDARD/radar）。這是 radar 的唯一正式發布路徑。
#
# 為什麼要有這支專屬腳本：radar 的版本標記 VERSION.json 必須「上傳前」在這台開發機
# 產生（船上沒有 .git，算不出自己是哪個 commit），所以發布動作 = stamp + upload 兩步。
# run_all_uploads.py 會 glob 所有 *_upload_settings.json、繞過 stamp，那條路徑上傳的
# 就是舊的（或不存在的）VERSION.json —— 船上開機時的 sha256 驗證會把它顯示成
# files:MISMATCH / files:UNSTAMPED，不會靜默混過去，但要正確發布請用這支。
#
# 未提交的修改（dirty）只警告不中止，dirty: true 會寫進 VERSION.json。
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

RADAR_DIR="$BASE_DIR/../../radar"
STAMP="$RADAR_DIR/tools/stamp_version.py"
radar_version="unknown"

if command -v python3 >/dev/null 2>&1 && [[ -f "$STAMP" ]]; then
    echo "=== 產生 radar 版本標記 (VERSION.json) ==="
    # stamp 失敗（不是 git repo、解析不到 __version__）只警告：版本標記是觀測用的，
    # 不該讓發布本身停擺；但船上會看到 files:UNSTAMPED，所以這裡一定要吵。
    if ! python3 "$STAMP"; then
        echo "*** 警告: 產生 VERSION.json 失敗，這次發布的內容將是「未標記版本」 ***" >&2
    fi
    radar_version="$(python3 "$STAMP" --print 2>/dev/null || true)"
    [[ -z "$radar_version" ]] && radar_version="unknown"
else
    echo "*** 警告: 找不到 python3 或 $STAMP，略過版本標記 ***" >&2
fi

echo "=== 上傳 radar（版本: $radar_version）==="
"$VENV_PY" "$BASE_DIR/main.py" --cli --mode upload --config "$config" \
    --version-info "$radar_version"
