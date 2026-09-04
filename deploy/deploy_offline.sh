#!/usr/bin/env bash
#
# deploy_offline.sh — 船機的唯一一次人工安裝入口
# ---------------------------------------------------------------------------
# 這支腳本是船機唯一的一次性人工安裝入口。雙平台相容層只負責以
# deploy/platforms/<profile>/debs 離線補齊 tmux；Python 沿用船端既有預安裝，
# 不攜帶、安裝或限制 Python runtime 版本。階段 C 的 SFTP OTA 只走內部網路。
#
# 三個階段:
#
#   A. 一次性人工設定(**所有需要你輸入的東西都集中在這裡**)
#      1) 船舶身分檔 share/.env/vessel_basic_info.json(vsl_name / ipc)
#         —— 順便偵測殘留的接管旗標與舊格式 failover_state.json
#      2) install_autostart.sh   → nssms-boot.service + linger
#      3) 舊 clink_* 遷移        → 停用/移除三支 system unit + 加入 gpio 群組(需密碼)
#         **必須排在 7) 之前**:舊 clink_alarm_controller / clink_board_server 還活著時,
#         新的 nssms-alarm-controller / nssms-board-server 會撞 port 起不來。
#      4) install_docker_group.sh → 把使用者加進 docker 群組(需密碼)。web 平台
#         (start_web_docker.sh)跑在 systemd user session 裡、無法輸入 sudo 密碼,
#         所以「免 sudo 使用 docker」是它能開機自啟的前提。群組變更需重開機才生效。
#     4b) 無人值守開機的兩個前提(兩支都需密碼,兩支都要重開機才驗得出來):
#         install_udisks_mount_policy.sh → 資料碟掛載的 polkit 授權(預設 Y)
#         install_gdm_autologin.sh       → GDM 自動登入(預設 Y;全船隊都該是開的)
#         兩支修的是同一件事的兩半:開機那一刻沒有人登入圖形桌面。
#      5~7) 以下三步由**同一個問題**一併決定(它們是一個概念單位:週期排程與 ipc 接管):
#         5) install_timers.sh      → 週期排程 timer（依實體 IPC 篩選）
#         6) sudoers 白名單         → reboot / teamviewer 需要(這一步要輸入一次密碼)
#         7) install_services.sh    → 4 支常駐服務:heartbeat(雙向心跳/接管)、
#                                     alarm-controller / board-server / button(綁實體 IPC-1)
#      8) install_tmux_offline.sh → 以平台 profile 的 debs/ 離線補齊 tmux(需密碼)。
#         **必須排在 10) 之前**:10) 的提示要據此警告「缺 tmux 時 session 型專案全起不來」。
#         scheduler 的每一支 start_*.sh 都靠 tmux new-session,啟動器也靠 tmux has-session
#         對帳 —— 少了它,啟動流程會一項一項 exit 2,而船上又沒有網路可以 apt install。
#      9) install_setup_ssh_key.sh → 照片同步的免密碼登入(**僅實體 IPC-2**)。
#         這一步輸入的是**遠端主機的密碼**(給 ssh-copy-id),不是本機 sudo。
#         nssms-download-photos.timer 每 4 小時跑 script/download_photos.sh,而它以
#         BatchMode=yes 連線 —— 沒金鑰就是立刻失敗,不會有人在旁邊輸入密碼。
#     10) 詢問「之後要不要立即執行完整啟動流程」——**只問，執行在階段 C**
#      這一段結束後會印「以下不再需要任何輸入」,操作者可以離開終端機。
#
# 實作對應:上面這份大綱就是檔尾 main() 的內容,一行一個 stage_* 函式。改流程請同時改
# 兩邊 —— 或者只讀 main(),它才是權威。
#
#   B. sftp_transfer 專屬 venv(離線、無人干預)
#      wheelhouse + MANIFEST.txt sha256 校驗 → virtualenv → pip --no-index → 匯入驗證
#      路徑預設 ~/venv/wanhai_nssms/share/sftp_transfer(與 radar / SHM 的慣例一致)
#
#   C. 完整啟動流程與驗證(無人干預)
#      reboot_launcher.sh:掛載資料碟 → update_booster(SFTP 拉最新程式碼)→ 依角色安裝
#      各專案環境並啟動服務 → 然後才跑 health_check 與 automation_health_check
#      (順序是刻意的:啟動要在 venv 之後——SFTP 下載要用它;要在巡檢之前——服務起來後
#       那份巡檢才第一次真的有意義)
#      可用 --no-launch 關閉;選 n 也不會壞,下次開機 nssms-boot 會跑同一支啟動器。
#
# tmux 離線安裝目標：Ubuntu 18.04 / 22.04 ARM64。Python 沿用主機預安裝。
#
# 用法：
#   ./deploy_offline.sh                 # 建立/更新專屬 venv，安裝執行期相依 + 測試堆疊（預設）
#   ./deploy_offline.sh --skip-tests    # 不安裝 pytest 測試堆疊，健康檢查也略過單元測試
#   ./deploy_offline.sh --with-tests    # （保留相容；現為預設，明確要求安裝測試堆疊）
#   ./deploy_offline.sh --recreate      # 砍掉重建 venv（乾淨安裝）
#   ./deploy_offline.sh --no-health-check # 部署後不自動執行能力／自動化健康檢查
#   ./deploy_offline.sh --no-launch      # 部署後不執行啟動流程（不下載程式碼、不啟動服務）
#   ./deploy_offline.sh --check-only    # 驗證平台與 tmux payload，不安裝、不修改 HOME/systemd/dpkg
#   ./deploy_offline.sh --venv /path/to/venv        # 自訂 venv 路徑
#   ./deploy_offline.sh --python /path/python # 指定船端既有的 Python
#
# 特性：
#   * venv 安裝全程 --no-index，永不連 PyPI（階段 C 的 SFTP 下載另當別論）。
#   * 優先使用船端 python3.10，沒有時沿用 python3；不安裝 Python runtime。
#   * venv 與系統 site-packages 隔離。
#   * 在任何系統變更前驗證平台、tmux deb 與其 sha256 manifest。
#   * 安裝後在 venv 內驗證關鍵套件可正常匯入。
#   * 全程輸出（stdout + stderr）逐字寫進 logs/deploy_offline_<時間>.log，與兩支巡檢器
#     的 Markdown 報告放在同一個 logs/。報告記結果，這一份記過程；船上回報問題寄它。
#   * tmux 依平台選用 Bionic/Jammy deb；缺 sudo 時安全停止，不使用 rootless 解包。
# ---------------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLATFORMS_ROOT="${SCRIPT_DIR}/platforms"
# wheelhouse / virtualenv_wheels 與 debs 同構：各平台一份，放在自己的 profile 底下，
# manifest 就放在該目錄**裡面**。理由與 debs 相同 —— 校驗的基準目錄是 wheelhouse 自己，
# 混進一份共用 manifest 只會在換平台時對不上。實際路徑在 banner_and_preflight 裡由
# 偵測到的 profile 決定（見 resolve_wheelhouse）；這裡的值只是「還沒偵測」的預設。
WHEELHOUSE="${SCRIPT_DIR}/wheelhouse"
MANIFEST="${SCRIPT_DIR}/MANIFEST.txt"
VENV_WHEELS=""         # 空＝交給 install_virtualenv_offline.sh 自己找同層 virtualenv_wheels/
WHEELHOUSE_LAYOUT=""   # profile / legacy —— 只用於訊息，讓記錄看得出走了哪一條
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
SHARE_DIR="$(dirname "$PROJECT_DIR")"
# 船舶基本資訊檔：供各設定檔的 {vsl_name}/{ipc} 佔位符替換使用（見 settings.py）。
VESSEL_INFO="${SHARE_DIR}/.env/vessel_basic_info.json"

DEFAULT_VENV="${HOME}/venv/wanhai_nssms/share/sftp_transfer"
VENV_DIR="${DEFAULT_VENV}"
PYTHON_BIN=""          # 空字串＝自動偵測（優先 python3.10，其次 python3）
# 預設安裝測試堆疊（pytest 等），讓部署後的 health_check 預設就會實際跑單元測試。
# 以 --skip-tests 關閉：不裝測試套件，且轉傳 --skip-tests 讓 health_check 略過。
INSTALL_TESTS=1
CHECK_ONLY=0
SKIP_VERIFY=0
RECREATE=0
RUN_HEALTH=1
# 部署完成後是否立即跑一次完整啟動流程(下載程式碼 + 裝環境 + 啟動服務)。
RUN_LAUNCH=1
# 一旦宣告「以下不再需要任何輸入」就設為 1；此後任何提示都是程式錯誤（見 ask_yn）。
NO_MORE_INPUT=0
# tmux 的補齊結果。在 file scope 先給值（比照 MIGRATE_STATUS）：stage_launch_decision
# 與 stage_summary 都會讀它，而 set -u 下讀到未定義變數會直接中止部署。
TMUX_STATUS="未執行"
# 照片同步金鑰的設定結果。同上：stage_summary 會讀它，而該 stage 在非 IPC-2 上會提早
# return —— 雖然它 return 前一定已賦值，仍在 file scope 先給值，理由與 TMUX_STATUS 相同。
SSH_KEY_STATUS="未執行"

# --- 顏色輸出 --------------------------------------------------------------
if [ -t 1 ]; then
  R=$'\e[31m'; G=$'\e[32m'; Y=$'\e[33m'; B=$'\e[36m'; N=$'\e[0m'
else
  R=""; G=""; Y=""; B=""; N=""
fi
info()  { printf "%s[INFO]%s %s\n"  "$B" "$N" "$*"; }
ok()    { printf "%s[ OK ]%s %s\n"  "$G" "$N" "$*"; }
warn()  { printf "%s[WARN]%s %s\n"  "$Y" "$N" "$*"; }
err()   { printf "%s[FAIL]%s %s\n"  "$R" "$N" "$*" >&2; }

# --- 完整終端記錄（logs/deploy_offline_<時間>.log）--------------------------
# 兩支巡檢器各自會寫 Markdown 報告，但那兩份是「部署完成後的狀態」；部署當下的過程
# ——哪個安裝器 exit 幾、操作者選了 Y 還是 n、pip 卡在哪個 wheel、sudo 有沒有輸入
# ——原本只存在於終端機，關掉視窗就沒了。船上排錯時能寄回岸上的只有檔案，所以這一份
# 逐字記錄跟兩份報告一樣落在 logs/。
#
# 做法：把 stdout 與 stderr 一起接到 tee，一份原樣進終端機（保留顏色），一份經 sed
# 去掉 ANSI 逃脫碼、再逐行加上時間戳後進檔案（讓 log 能 grep、能貼進工單）。合流
# stderr 是刻意的：err() 寫 stderr，分兩份存會讓「哪一步失敗」失去時間順序。
#
# 兩個已知的取捨：
#   * 子程序（install_*.sh / health_check.py）的 stdout 從此是 pipe 而非 tty，它們的
#     `[ -t 1 ]` / isatty() 會關掉自己的顏色，螢幕上因此變單色。可接受：那些判斷全都
#     只影響顏色，真正會改變行為的分支看的是 `[ -t 0 ]`（stdin），而 stdin 不動 ——
#     所以 ask_yn 的提問與 sudo 密碼輸入都不受影響。本腳本自己的顏色也不受影響：上面
#     那段 `[ -t 1 ]` 在 main() 之前就算完了，那時 fd 1 還是終端機。
#   * 原始 fd 先存進 3/4，離開前由 EXIT trap 還原並等 tee 收工。少了這一步，最後幾行
#     會晚於 shell 提示符才印出來，也可能來不及寫進檔案。
TRANSCRIPT=""            # 記錄檔路徑；空字串＝這次沒留成記錄（stage_summary 會讀）
TRANSCRIPT_TEE_PID=""

# 記錄檔逐行加上時鐘。只加在**檔案**這一路，終端機那一路完全不動（見下方 exec）：
# 螢幕上是即時的，時間對站在機器前的人沒有用；真正需要它的是事後排錯 ——「停在哪一步、
# 停了多久」。原本整份記錄一個時間戳都沒有，於是要回答這個問題只能拿 launcher.log 的
# 起訖行、兩份 Markdown 報告的檔名時間去反推，而那幾個點之間的空白仍然是猜的。
#
# 為什麼是 bash 迴圈而不是 awk 或 moreutils 的 ts：機上的 awk 是 mawk（沒有 strftime），
# ts 也不在離線包裡。printf '%(...)T' 是 bash 4.2 起的內建，零依賴、不多開行程。
#
# 兩個要知道的限制（都不是這一層能修的）：
#   * 時間戳記的是「這一行**抵達**記錄器」的時刻。子程序的 stdout 在這裡是 pipe，所以
#     Python（health_check.py 等）會塊緩衝到 8 KB 才吐一次 —— 那一整批會拿到幾乎相同的
#     時間。要看某一行真正發生的時刻，得讓那支程式自己不緩衝。
#   * pip 那種用 \r 原地更新的進度是同一行，要等它換行才會落檔，時間戳因此是該段的結束
#     時刻而不是開始。終端機顯示不受影響（tee 是逐位元組轉發的）。
#
# read -r 保留反斜線；`|| [ -n "$line" ]` 讓最後一行沒有換行時也不會被丟掉。
stamp_lines() {
  local line
  while IFS= read -r line || [ -n "$line" ]; do
    printf '%(%F %T)T %s\n' -1 "$line"
  done
}

start_transcript() {
  local dir="${PROJECT_DIR}/logs"
  # 測試會真的把這支腳本跑起來(--check-only),而記錄檔預設寫進**專案的** logs/ ——
  # 那個目錄會被 fleet log 收走上傳。WH102-2 就出現過這個形狀:船上的 logs/ 躺著一份
  # 平台寫著 ubuntu-18.04 的部署記錄,實際上是 tests/test_offline_deploy.py 用
  # NSSMS_TEST_OVERRIDES 假造 Bionic 跑出來的產物 —— 但操作者讀到的是「這台 Jammy 被
  # 認成 Bionic 了」。偵測 override 只有測試會開(offline_common.sh 在非測試模式直接
  # 拒絕它),所以用它來把記錄改寫到暫存目錄,不必每支新測試都記得改設定。
  if [ "${NSSMS_TEST_OVERRIDES:-0}" = "1" ]; then
    dir="${TMPDIR:-/tmp}/nssms-deploy-transcripts"
  fi
  local path="${dir}/deploy_offline_$(date '+%Y%m%d_%H%M%S').log"
  # 寫不進去不是中止部署的理由（唯讀掛載、權限不對都可能）——少一份記錄而已。
  # 兩處的 2>/dev/null 都寫在失敗的重導向**之前**：重導向錯誤是由 shell 自己印的，
  # 寫在後面就來不及擋（`: >>path 2>/dev/null` 會漏出一行 Permission denied）。
  if ! mkdir -p "$dir" 2>/dev/null || ! : 2>/dev/null >>"$path"; then
    warn "無法寫入 ${dir}，本次不留完整終端記錄。"
    return 0
  fi
  TRANSCRIPT="$path"
  exec 3>&1 4>&2
  exec > >(tee >(sed -u 's/\x1b\[[0-9;?]*[a-zA-Z]//g' | stamp_lines >>"$TRANSCRIPT")) 2>&1
  # $! 取到的是最外層那支 tee（stamp_lines 在內層程序替換裡，不影響這個值）。tee 收工
  # 時內層才會看到 EOF，所以等它就等於等整條鏈 —— 這也是下面只 wait 一個 PID 的理由。
  TRANSCRIPT_TEE_PID=$!   # bash >= 5.1 會把程序替換的 PID 放進 $!；舊版取不到就少了等待
  trap stop_transcript EXIT
}

stop_transcript() {
  [ -n "$TRANSCRIPT_TEE_PID" ] || return 0
  exec 1>&3 2>&4          # 先放掉寫入端，tee 才看得到 EOF
  wait "$TRANSCRIPT_TEE_PID" 2>/dev/null || true
  TRANSCRIPT_TEE_PID=""
}

# --help 只印檔頭那一段（第 2 行到第一個非註解行為止）。原本是 grep '^#' "$0"，
# 會把全檔 170 多行 column-0 實作註解一起倒出來 —— 這支腳本的實作註解特別多、特別長，
# 於是 --help 反而是最難讀的那份說明。
usage() { awk 'NR == 1 { next } !/^#/ { exit } { sub(/^# ?/, ""); print }' "$0"; exit 0; }

# --- 小工具 ----------------------------------------------------------------
# 執行一支指令並把離開碼留在全域 RC，不中斷腳本。原本每一處都寫成
#     set +e; cmd; RC=$?; set -e
# 三行一組，共 17 組。其實 errexit 對 `cmd || RC=$?` 的左側本來就豁免，不需要關掉它
# —— 關掉反而危險：那三行之間日後被插進新指令時，那些指令會無聲失去 errexit 保護。
RC=0
run_rc() { RC=0; "$@" || RC=$?; }

# wheelhouse 裡有沒有這個套件的輪子。wheel 檔名的 name 欄位用底線與大小寫變體,
# 比對前一起正規化(PEP 503)。
wheelhouse_has() {  # $1 = 套件名, $2 = wheelhouse 目錄
  local norm; norm="$(printf '%s' "$1" | tr 'A-Z_.' 'a-z--')"
  find "$2" -maxdepth 1 -name '*.whl' -printf '%f\n' 2>/dev/null \
    | sed 's/-.*//' | tr 'A-Z_.' 'a-z--' | grep -qx "$norm"
}

# 是非題。提示文字（含 "[Y/n]" / "[y/N]"）由呼叫點自帶：它同時是給操作者看的說明**和**
# 預設值的宣告，分開寫必然會有一天不一致。$2 是「直接按 Enter」與非互動時採用的預設。
#
# 原本 11 處各自手寫 read + case：`""|Y|y)` 是預設同意、`Y|y)` 是預設拒絕，兩者只差
# 三個字元，而其中兩處的預設值管的是「會不會讓一台正在接管的船失去接管」——
# 26ebe1a 修的就是那兩處被寫錯的預設值。收成一處後，預設值變成呼叫點上讀得出來的參數。
ask_yn() {  # $1=提示（須含 [Y/n] 或 [y/N]） $2=預設 Y|N → rc 0=同意
  local ans=""
  if [ "$NO_MORE_INPUT" -eq 1 ]; then
    # 守住「所有需要輸入的東西都集中在最前面」。venv 建置是一長段無人干預的流程，若它
    # 之後還冒出提示，操作者就得守在機器前等它跑完才能回答 —— 那是這支腳本最實際的體驗
    # 問題。寧可在開發時當場中止，也不要在船上讓人乾等。
    err "內部錯誤：宣告「不再需要輸入」之後仍出現提示：$1"
    exit 1
  fi
  if [ -t 0 ]; then
    read -r -p "$1" ans || ans=""
  fi
  [ -n "$ans" ] || ans="$2"
  # 把實際採用的答案印出來，否則終端記錄裡會是一串沒有答案的問題：提示本身走 stderr、
  # 已隨 2>&1 進 log，但操作者敲的字只由終端機回顯，不經 fd 1/2。非互動時這一行也
  # 順便說明採用了哪個預設。
  printf "       （採用：%s）\n" "$ans"
  case "$ans" in Y|y) return 0 ;; *) return 1 ;; esac
}

# --check-only 的承諾是「只驗證，不安裝」。把它做成一道實際的閘門，而不是靠每一段各自
# 記得寫 if：任何會改變機器狀態的動作（寫身分檔、刪檔、佈署 unit、sudoers、usermod、
# 建 venv）都先過這裡。26ebe1a 是「漏掉守衛，--check-only 一路把身分檔與 systemd 都改掉」
# 的事故修復，而 fd4f8db 新增 docker 群組時又得手工複製一次守衛 —— 下一個新增步驟若
# 忘了寫，這道閘門會讓它當場中止，而不是靜默造成變更。
mutating() {  # $1... = 動作說明
  if [ "$CHECK_ONLY" -eq 1 ]; then
    err "內部錯誤：--check-only 下不該執行變更動作：$*"
    exit 1
  fi
}

# --- 解析參數 --------------------------------------------------------------
parse_args() {
  while [ $# -gt 0 ]; do
    case "$1" in
      --with-tests)  INSTALL_TESTS=1 ;;
      --skip-tests)  INSTALL_TESTS=0 ;;
      --check-only)  CHECK_ONLY=1 ;;
      --skip-verify) SKIP_VERIFY=1 ;;
      --recreate)    RECREATE=1 ;;
      --no-health-check) RUN_HEALTH=0 ;;
      --no-launch)   RUN_LAUNCH=0 ;;
      --venv)        VENV_DIR="${2:?--venv 需要一個路徑參數}"; shift ;;
      --python)      PYTHON_BIN="${2:?--python 需要一個路徑參數}"; shift ;;
      -h|--help)     usage ;;
      *) err "未知參數：$1"; echo "執行 --help 查看用法" >&2; exit 2 ;;
    esac
    shift
  done

  if [ -z "$PYTHON_BIN" ]; then
    # 固定系統路徑優先，避免操作者從已啟用的 venv 內執行時，PATH 的 python3
    # 污染 bootstrap，造成 pip --user 在 venv 內被拒絕。
    if [ -x /usr/bin/python3.10 ]; then
      PYTHON_BIN="/usr/bin/python3.10"
    elif [ -x /usr/bin/python3 ]; then
      PYTHON_BIN="/usr/bin/python3"
    elif command -v python3.10 >/dev/null 2>&1; then
      PYTHON_BIN="$(command -v python3.10)"
    else
      PYTHON_BIN="$(command -v python3 2>/dev/null || true)"
    fi
  elif command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v "$PYTHON_BIN")"
  fi

  case "$VENV_DIR" in
    /*) ;;
    *) err "--venv 必須使用絕對路徑：$VENV_DIR"; exit 2 ;;
  esac
  VENV_DIR="$(readlink -m -- "$VENV_DIR")"
  case "$VENV_DIR" in
    /|/bin|/boot|/dev|/etc|/home|/lib|/lib64|/opt|/proc|/root|/run|/sbin|/srv|/sys|/tmp|/usr|/var|\
    "$HOME"|"$PROJECT_DIR"|"$SHARE_DIR")
      err "拒絕把重要目錄當成 venv：$VENV_DIR"
      exit 2
      ;;
  esac
}

# 依偵測到的 profile 決定 wheelhouse / virtualenv_wheels 的實際位置。
# 必須在 nssms_detect_profile 之後呼叫（它要 $PROFILE_DIR）。
#
# 兩種佈局：
#   profile — deploy/platforms/<profile>/wheelhouse/{*.whl,MANIFEST.txt}   ← 正規
#   legacy  — deploy/wheelhouse/ + deploy/MANIFEST.txt                     ← 過渡
#
# legacy 留著是因為已經派送到船上的舊離線包就是那個形狀，換版不該讓它們一次全部失效。
# 但 legacy 只有**單一**一份，換平台必然對不上 —— 所以走 legacy 時會 warn，並且
# 相容性檢查照跑（那才是真正的守門，不是靠目錄名字）。
resolve_wheelhouse() {
  local prof_wh="${PROFILE_DIR}/wheelhouse"
  local prof_vw="${PROFILE_DIR}/virtualenv_wheels"

  if [ -d "$prof_wh" ]; then
    WHEELHOUSE="$prof_wh"
    MANIFEST="${prof_wh}/MANIFEST.txt"
    WHEELHOUSE_LAYOUT="profile：${NSSMS_PROFILE_ID}"
  else
    WHEELHOUSE="${SCRIPT_DIR}/wheelhouse"
    MANIFEST="${SCRIPT_DIR}/MANIFEST.txt"
    WHEELHOUSE_LAYOUT="legacy 共用目錄"
    warn "找不到 ${prof_wh}，退回共用的 deploy/wheelhouse/。"
    warn "共用目錄只有一份，換平台一定對不上；請盡快改成 per-profile 佈局。"
  fi

  # virtualenv 的 bootstrap 輪子同理，但它可以共用：目前那一組全部宣告
  # Requires-Python >=3.6，Bionic 與 Jammy 都吃得下。若哪天要為某個 profile 另備一份，
  # 放 platforms/<profile>/virtualenv_wheels/ 就會自動被挑走。
  if [ -d "$prof_vw" ]; then
    VENV_WHEELS="$prof_vw"
  elif [ -d "${SCRIPT_DIR}/virtualenv_wheels" ]; then
    VENV_WHEELS="${SCRIPT_DIR}/virtualenv_wheels"
  else
    VENV_WHEELS=""
  fi
}

banner_and_preflight() {
  local preflight_failures=0
  local preflight_rc=4
  echo "==========================================================="
  echo " sftp_transfer 離線部署 (offline deploy — 專屬 venv)"
  echo "==========================================================="
  # 開頭就報路徑（不只在總結）：--check-only 與各種 err + exit 1 都到不了 stage_summary，
  # 而那些正是最需要「記錄在哪」的情況。這一行本身也會進記錄，等於檔案自帶檔名。
  [ -n "$TRANSCRIPT" ] && info "完整終端記錄：$TRANSCRIPT"

  # --- 嚴格前置檢查：本函式完成前不得有任何持久變更 -------------------------
  # shellcheck source=deploy/lib/offline_common.sh
  . "${SCRIPT_DIR}/lib/offline_common.sh"
  nssms_detect_profile "$PLATFORMS_ROOT" || exit $?
  PROFILE_DIR="$NSSMS_PROFILE_DIR"
  export NSSMS_PROFILE_ID NSSMS_PROFILE_DIR
  info "平台 profile  : $NSSMS_PROFILE_ID"
  info "系統版本      : $NSSMS_OS_ID $NSSMS_OS_VERSION / $NSSMS_ARCH"
  info "glibc          : $NSSMS_GLIBC"
  if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    err "找不到船端預安裝的 Python：$PYTHON_BIN"
    preflight_failures=$((preflight_failures + 1))
  else
    if ! "$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if getattr(sys, "base_prefix", sys.prefix) == sys.prefix and not hasattr(sys, "real_prefix") else 1)' >/dev/null 2>&1; then
      err "基底直譯器位於虛擬環境中：$PYTHON_BIN"
      err "請以 --python /usr/bin/python3 指定系統直譯器；尚未做任何持久變更。"
      preflight_failures=$((preflight_failures + 1))
    fi
    PY_VER="$("$PYTHON_BIN" -c 'import sys;print(".".join(map(str,sys.version_info[:3])))')"
    PY_TAG="$("$PYTHON_BIN" -c 'import sys;print("cp%d%d"%sys.version_info[:2])')"
    info "基底直譯器    : $PYTHON_BIN ($PY_VER, $PY_TAG；船端預安裝)"
  fi
  resolve_wheelhouse
  info "Wheelhouse    : $WHEELHOUSE（$WHEELHOUSE_LAYOUT）"
  info "專案目錄      : $PROJECT_DIR"
  info "專屬 venv     : $VENV_DIR"
  info "船舶資訊檔    : $VESSEL_INFO"

  # --- wheelhouse 與「這個」直譯器是否真的相容 ------------------------------
  # 這一項刻意放在階段 A 之前。它擋的是「preflight 全綠、階段 B 才發現輪子根本
  # 裝不上」——那個時序下 systemd/sudoers/tmux 已經改完，而沒裝完的 venv 會讓兩支
  # OTA 腳本的 `[ -x $VENV_PY ]` 守門失效（venv 在、paramiko 不在），於是那條船
  # 失去唯一的下載路徑。詳見 lib/wheel_compat.py 的檔頭。
  if [ -z "${PY_VER:-}" ]; then
    : # 直譯器都找不到，上面已經記過一筆，這裡不重複
  else
    WHEEL_REQUIRED=(paramiko bcrypt cryptography pynacl cffi pycparser)
    PY_MM="$("$PYTHON_BIN" -c 'import sys;print("%d.%d"%sys.version_info[:2])')"
    # 目標平台明確傳進去，不讓 checker 從「執行它的直譯器」去猜（見該檔 main() 的註解）。
    run_rc "$PYTHON_BIN" "${SCRIPT_DIR}/lib/wheel_compat.py" \
      --py "$PY_MM" --glibc "$NSSMS_GLIBC" --arch "$(uname -m)" \
      "$WHEELHOUSE" "${WHEEL_REQUIRED[@]}"
    case "$RC" in
      0) ok "wheelhouse 與 $PY_TAG / glibc $NSSMS_GLIBC 相容。" ;;
      *)
        err "wheelhouse 與本機不相容（exit=$RC）；尚未做任何持久變更。"
        err "本機需要的是 $PY_TAG / glibc $NSSMS_GLIBC 的輪子。"
        err "正確做法是為這個 profile 另備一份 wheelhouse："
        err "  deploy/platforms/${NSSMS_PROFILE_ID}/wheelhouse/"
        err "重建方式見 deploy/README.md 的「未來如何更新 / 重建 wheelhouse」。"
        preflight_failures=$((preflight_failures + 1))
        [ "$RC" -ne 6 ] || preflight_rc=6
        ;;
    esac
  fi

  # virtualenv bootstrap 輪子的完整性。**只在確定需要它時**才列為部署前提:已經有
  # virtualenv 的機器不該因為這個目錄少一個檔而卡住(它那時根本用不到)。
  #
  # 為什麼要校驗:它是船上每一個 venv 的前提,而 OTA 走 SFTP —— 少送或截斷一個檔,失敗會
  # 晚到 pip 解析相依那一刻才以「找不到相依」浮出來,而那時已經動過機器了。這裡讓它提早、
  # 明確地失敗,與 tmux debs 用的是同一支校驗器。
  if [ -n "$PYTHON_BIN" ] && command -v "$PYTHON_BIN" >/dev/null 2>&1 \
     && ! "$PYTHON_BIN" -m virtualenv --version >/dev/null 2>&1; then
    if [ -z "$VENV_WHEELS" ]; then
      err "$PYTHON_BIN 沒有 virtualenv,而找不到 virtualenv bootstrap 輪子目錄。"
      err "應位於 deploy/virtualenv_wheels/ 或 platforms/${NSSMS_PROFILE_ID}/virtualenv_wheels/。"
      preflight_failures=$((preflight_failures + 1))
    elif [ ! -f "$VENV_WHEELS/MANIFEST.txt" ]; then
      # 舊離線包沒有這份 manifest。缺它只是少一道校驗,不該讓那些包一次全部失效。
      warn "$VENV_WHEELS 沒有 MANIFEST.txt,略過完整性校驗(舊離線包可能沒有這份)。"
    elif ! nssms_verify_flat_manifest "$VENV_WHEELS" "$VENV_WHEELS/MANIFEST.txt" \
            '*.whl' "virtualenv bootstrap 輪子"; then
      err "virtualenv bootstrap 輪子校驗未通過;尚未做任何持久變更。"
      err "重建方式見 deploy/README.md 的「未來如何更新 / 重建 wheelhouse」。"
      preflight_failures=$((preflight_failures + 1))
    fi
  fi

  run_rc bash "$SCRIPT_DIR/install_tmux_offline.sh" --check-only --profile-dir "$PROFILE_DIR"
  case "$RC" in
    0|5) ok "tmux profile 與本機 ABI 驗證通過。" ;;
    *)
      err "tmux preflight 失敗（exit=$RC）；尚未執行 dpkg。"
      preflight_failures=$((preflight_failures + 1))
      [ "$RC" -ne 6 ] || preflight_rc=6
      ;;
  esac

  if [ "$preflight_failures" -ne 0 ]; then
    err "離線部署 preflight 共發現 $preflight_failures 個問題；未執行任何持久變更。"
    exit "$preflight_rc"
  fi
  ok "雙平台 tmux 離線資產 preflight 通過。"

  if [ "$CHECK_ONLY" -eq 1 ]; then
    ok "--check-only 完成：未執行安裝或其他持久變更。"
    exit 0
  fi

}

# --- 船舶基本資訊檔（vessel_basic_info.json）檢查 / 互動建立 ----------------
# 剛啟動就先確認它存在且內容正確（需含非空的 vsl_name / ipc）；
# 缺少或內容不正確時，以互動問答讓使用者輸入並建立該檔。
# 印出現有內容；有效回傳 0、檔案不存在回傳 3、內容不正確回傳 2、
# 「欄位有效但處於接管中」回傳 4。
#
# 為什麼需要區分 4:身分檔現在同時承載接管旗標(failover / failover_since)。若只檢查
# vsl_name/ipc 非空，一台帶著 failover=true 的機器重新部署會被判定「有效，沿用現有內容」
# ——**靜默保留接管狀態**。而重新部署幾乎總是意味著機器被重裝、搬移或換角色，那個旗標
# 幾乎確定是殘留;殘留下去會讓本機一直以 emer 角色啟動、與對方形成雙主。
vessel_info_show() {
  "$PYTHON_BIN" - "$VESSEL_INFO" <<'PY'
import json, sys
path = sys.argv[1]
try:
    with open(path, encoding="utf-8") as f:
        info = json.load(f)
except FileNotFoundError:
    sys.exit(3)
except Exception as e:  # noqa
    print(f"內容無法解析：{e}")
    sys.exit(2)
if not isinstance(info, dict):
    print("內容不是 JSON 物件")
    sys.exit(2)
for k, v in info.items():
    print(f"{k} = {v}")
missing = [k for k in ("vsl_name", "ipc") if not str(info.get(k, "")).strip()]
if missing:
    print("缺少或為空的必要欄位：" + ", ".join(missing))
    sys.exit(2)
# 真假白名單與 scheduler/failover/role.py 的 is_failover_on() 一致。
if str(info.get("failover", "")).strip().lower() in {"true", "1", "yes"}:
    sys.exit(4)
sys.exit(0)
PY
}

# 清除身分檔的接管旗標（只移除那兩個欄位，不動 ipc / vsl_name）。
clear_failover_flag() {
  "$PYTHON_BIN" - "$VESSEL_INFO" <<'PY'
import json, os, sys
path = sys.argv[1]
with open(path, encoding="utf-8") as f:
    info = json.load(f)
for key in ("failover", "failover_since", "failover_since_iso"):
    info.pop(key, None)
tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(info, f, ensure_ascii=False, indent=2)
    f.write("\n")
os.replace(tmp, path)   # 原子替換，避免任何讀者看到半寫檔
PY
}

vessel_get() {  # $1=key → 印出現有值（去頭尾空白），讀取失敗則印空字串
  "$PYTHON_BIN" - "$VESSEL_INFO" "$1" <<'PY' 2>/dev/null || true
import json, sys
try:
    info = json.load(open(sys.argv[1], encoding="utf-8"))
    print(str(info.get(sys.argv[2], "")).strip())
except Exception:
    print("")
PY
}

prompt_field() {  # $1=提示文字 $2=key → 結果放進 REPLY_VAL（不可為空，有舊值則當預設）
  local cur val
  cur="$(vessel_get "$2")"
  while true; do
    if [ -n "$cur" ]; then
      read -r -p "  $1 [$cur]: " val || val=""
      val="${val:-$cur}"
    else
      read -r -p "  $1: " val || val=""
    fi
    val="$(printf '%s' "$val" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
    if [ -n "$val" ]; then REPLY_VAL="$val"; return 0; fi
    warn "  不可為空，請重新輸入。"
  done
}

create_vessel_info() {
  if [ ! -t 0 ]; then
    err "非互動終端機，無法以問答建立船舶資訊檔。"
    err "請手動建立 $VESSEL_INFO ，內容範例：{\"vsl_name\": \"WH289\", \"ipc\": \"IPC-1\"}"
    exit 1
  fi
  # 本函式是整檔覆寫（只寫 vsl_name / ipc），所以會連帶清掉接管旗標。這在「內容不正確
  # 要重建」的路徑上正是想要的效果，但必須明說 —— 否則使用者不會知道自己剛剛結束了接管。
  # 註：A4 的「只碰兩個欄位、絕不新建」規則約束的是 heartbeat.py 與 failover_ctl.sh
  # 這兩個自動寫入者；deploy 是身分檔的產生者，整檔覆寫是刻意的例外。
  if [ -f "$VESSEL_INFO" ] && grep -q '"failover"' "$VESSEL_INFO" 2>/dev/null; then
    warn "注意：重建身分檔會一併清除接管旗標（failover / failover_since）。"
    warn "若本機正在替對方接管，重建後角色會回到正常值。"
  fi
  local vsl ipc
  while true; do
    echo ""
    info "請輸入船舶基本資訊："
    prompt_field "船名 vsl_name（例：WH289）" "vsl_name"; vsl="$REPLY_VAL"
    prompt_field "IPC 代號 ipc（例：IPC-1）"  "ipc";      ipc="$REPLY_VAL"
    echo ""
    echo "  即將寫入 $VESSEL_INFO ："
    echo "    vsl_name = $vsl"
    echo "    ipc      = $ipc"
    if ask_yn "  確認無誤？[Y/n] " Y; then break; fi
    warn "重新輸入。"
  done
  mutating "建立/覆寫船舶基本資訊檔"
  mkdir -p "$(dirname "$VESSEL_INFO")"
  VSL_NAME="$vsl" IPC="$ipc" "$PYTHON_BIN" - "$VESSEL_INFO" <<'PY'
import json, os, sys
data = {"vsl_name": os.environ["VSL_NAME"], "ipc": os.environ["IPC"]}
with open(sys.argv[1], "w", encoding="utf-8") as f:
    json.dump(data, f, ensure_ascii=False, indent=2)
    f.write("\n")
PY
  ok "已建立/更新船舶基本資訊檔：$VESSEL_INFO"
}

# --check-only 的承諾是「只驗證，不安裝」（見檔頭用法）。以下這一整段「一次性設定」
# 會建立身分檔、清除接管旗標、刪除舊格式狀態檔、佈署 systemd unit 與 sudoers ——
# 全部都是變更。所以 --check-only 一律只回報現況、不執行任何動作。
#
# 這個 guard 是後補的：CHECK_ONLY 原本要到 venv 那一段才被檢查，於是 --check-only 會
# 一路把身分檔與 systemd 都改掉。加入「清除接管旗標」「刪除舊格式狀態檔」之後風險升級
# ——對一台真的正在接管中的船,那會直接讓它失去接管。
announce_check_only() {
  DRYRUN_NOTE=""
  if [ "$CHECK_ONLY" -eq 1 ]; then
    DRYRUN_NOTE="（--check-only：只回報，不執行）"
    echo ""
    info "--check-only：以下一次性設定只回報現況，不做任何變更。"
  fi
}

stage_vessel_info() {
  echo ""
  info "檢查船舶基本資訊檔 ..."
  VESSEL_RC=0
  VESSEL_OUT="$(vessel_info_show)" || VESSEL_RC=$?
  [ -n "$VESSEL_OUT" ] && printf '%s\n' "$VESSEL_OUT" | sed 's/^/       /'
  if [ "$CHECK_ONLY" -eq 1 ]; then
    case "$VESSEL_RC" in
      0) ok   "船舶基本資訊檔有效。" ;;
      4) warn "船舶基本資訊檔有效，但**帶著 failover 接管旗標** —— 本機會以 emer 角色啟動。$DRYRUN_NOTE" ;;
      3) warn "找不到船舶基本資訊檔；正式部署時會以互動問答建立。$DRYRUN_NOTE" ;;
      *) warn "船舶基本資訊檔內容不正確；正式部署時會重新建立。$DRYRUN_NOTE" ;;
    esac
  elif [ "$VESSEL_RC" -eq 0 ]; then
    ok "船舶基本資訊檔有效，沿用現有內容。"
  elif [ "$VESSEL_RC" -eq 4 ]; then
    # 欄位有效，但帶著接管旗標。重新部署幾乎總是意味著機器被重裝、搬移或換角色，
    # 所以預設清除;真的正在接管中(對方確實故障)才選 n。
    echo ""
    warn "══════════════════ 本機處於接管狀態 ══════════════════"
    warn "身分檔帶有 failover 旗標，本機會以 emer 角色啟動（等同對方的完整服務清單）。"
    warn "═══════════════════════════════════════════════════"
    # 不猜預設值,把證據給操作者。成本是不對稱的:
    #   誤清 → 若對方真的死了,船上立刻失去那些服務,而且沒有任何告警(安靜的嚴重故障)
    #   誤留 → 啟動器每次都印雙主警告、巡檢 24 小時後升 WARN、status 直接顯示(很吵,可回復)
    # 所以預設保留(Enter = N),並先跑一次唯讀的 status 讓操作者看對方到底活不活著。
    FAILOVER_CTL="${SHARE_DIR}/scheduler/failover/failover_ctl.sh"
    if [ -f "$FAILOVER_CTL" ]; then
      echo ""
      info "先確認對方是否還活著（failover_ctl.sh status，唯讀）："
      # 唯讀查詢，對端沒回應時會回非 0 —— 那正是我們要給操作者看的資訊，不是錯誤。
      bash "$FAILOVER_CTL" status 2>&1 | sed 's/^/       /' || true
    fi
    echo ""
    warn "判讀:"
    warn "  * 上面顯示對方**有回應** → 這個旗標是殘留,應該清除(否則兩台同時跑同一批服務)"
    warn "  * 上面顯示對方**無回應** → 本機可能真的在替它接管,清除會讓船上失去那些服務"
    if [ ! -t 0 ]; then
      warn "非互動終端機：不擅自更動身分檔，保留現狀。"
      warn "如需清除請執行：bash $FAILOVER_CTL clear"
    fi
    # 預設 N（保留）—— 誤清是安靜的嚴重故障，誤留是很吵但可回復的，見上方成本分析。
    if ask_yn "  清除接管旗標？（不確定就按 Enter 保留，之後可用 failover_ctl.sh clear）[y/N] " N; then
      mutating "清除身分檔的接管旗標"
      if clear_failover_flag; then
        ok "已清除接管旗標（vsl_name / ipc 未變更）。"
        info "角色要生效仍需執行：bash ${SHARE_DIR}/scheduler/reboot_launcher.sh --reconcile"
      else
        warn "清除失敗，保留現狀。請改用 failover_ctl.sh clear 處理。"
      fi
    else
      warn "保留接管旗標。本機將繼續以 emer 角色啟動。"
    fi
  elif [ "$VESSEL_RC" -eq 3 ]; then
    warn "找不到船舶基本資訊檔，將以互動問答建立。"
    create_vessel_info
  else
    warn "船舶基本資訊檔內容不正確，將重新建立。"
    create_vessel_info
  fi
}

# --- 由身分檔推導出的兩個顯示值 --------------------------------------------
# 刻意算在這裡（而不是總結段）:兩者都只依賴身分檔，而身分檔到上一行才定案
#（可能剛被建立、也可能剛清掉接管旗標）。
#
# DEPLOY_VSL_UPPER 原本在總結段才賦值，但**啟動流程的提示比它早 200 行**就要用它判斷
# 「本機是不是 CLINK 開發機」—— 那裡讀到的一直是空字串（寫成 ${DEPLOY_VSL_UPPER:-}
# 所以 set -u 也不會抱怨），於是那句「開發機不會下載程式碼」的警告從來沒印出過。
# effective_role.sh 與 vessel_get 都是唯讀的，提前呼叫不影響 --check-only 的承諾。
compute_identity() {
  EFFECTIVE_ROLE_SH="${SHARE_DIR}/scheduler/failover/effective_role.sh"
  if [ -f "$EFFECTIVE_ROLE_SH" ]; then
    DEPLOY_ROLE="$(bash "$EFFECTIVE_ROLE_SH" --quiet 2>/dev/null || echo "（判定失敗）")"
  else
    DEPLOY_ROLE="（找不到 effective_role.sh）"
  fi
  # 開發機(CLINK)的 OTA 守門會讓「第一次開機自動下載程式碼」這件事不成立,後面要據此提醒。
  DEPLOY_VSL_UPPER="$(printf '%s' "$(vessel_get vsl_name)" | tr '[:lower:]' '[:upper:]')"
}

# --- 舊格式的接管狀態檔（已廢除）------------------------------------------
# 若 .env/ 是從舊機複製過來的，這個檔會讓新機被 heartbeat 遷移成「接管中」——
# 首次部署的機器不該繼承別台的接管狀態。
# 【移除條件】全隊確認升級完成後，連同 scheduler/failover/role.py 的遷移碼一起刪掉。
stage_legacy_failover_state() {
  LEGACY_FAILOVER="${SHARE_DIR}/.env/failover_state.json"
  if [ -f "$LEGACY_FAILOVER" ]; then
    echo ""
    warn "偵測到舊格式的接管狀態檔：$LEGACY_FAILOVER"
    warn "它已廢除。若保留，heartbeat 啟動時會把它遷移成本機的接管狀態。"
    warn "判讀與上面同一個道理:若 .env/ 是從舊機複製過來的,這是殘留,該刪;"
    warn "若本機真的在替一台死掉的對端接管,刪掉就會失去接管。"
    warn "保留是可回復的（遷移後會出現在 failover_ctl.sh status 與巡檢報告裡）,所以預設保留。"
    legacy_del=1   # 1 = 保留（預設）
    if [ "$CHECK_ONLY" -eq 1 ]; then
      warn "$DRYRUN_NOTE 正式部署時會詢問是否刪除。"
    else
      [ -t 0 ] || warn "非互動終端機：不擅自刪除，保留現狀。"
      ask_yn "  刪除它？（不確定就按 Enter 保留）[y/N] " N && legacy_del=0
    fi
    if [ "$legacy_del" -eq 0 ]; then
      mutating "刪除舊格式接管狀態檔"
      rm -f "$LEGACY_FAILOVER" && ok "已刪除 $LEGACY_FAILOVER"
    else
      warn "保留舊格式接管狀態檔。heartbeat 啟動時會把它遷移進身分檔;"
      warn "若確認是殘留,遷移後執行:bash ${SHARE_DIR}/scheduler/failover/failover_ctl.sh clear"
    fi
  fi
}

# --- 開機自動執行設定（scheduler/install_autostart.sh） --------------------
# 與船舶資訊檔一樣，是需要使用者留意的一次性設定：詢問是否設定開機自動啟動
# （systemd user service + linger）。install_autostart.sh 具冪等性，可重複執行。
#
# 以 --require-linger 呼叫，讓 install_autostart.sh 用離開碼區分結果，deploy 才能
# 「掌握」實際成功狀態(而非只知道有沒有崩)。deploy 端僅據以警告、不中斷部署。
#   0 = 完全成功(service enabled + linger on)
#   3 = user service 已裝，但 linger 未開啟(開機免登入自動執行需要它)
#   4 = 設定失敗(找不到腳本 / 無法寫入 unit / user manager 不可用)
#   2 = install_autostart.sh 參數錯誤
# AUTOSTART_STATUS 供最後的部署總結顯示；先給預設值(set -u 下需先定義)。
stage_autostart() {
  AUTOSTART_INSTALLER="${SHARE_DIR}/scheduler/install_autostart.sh"
  AUTOSTART_STATUS="未執行"
  echo ""
  info "檢查開機自動執行設定 ..."
  if [ ! -f "$AUTOSTART_INSTALLER" ]; then
    warn "找不到 $AUTOSTART_INSTALLER ，略過開機自動執行設定。"
    AUTOSTART_STATUS="略過（找不到安裝腳本）"
  elif [ "$CHECK_ONLY" -eq 1 ]; then
    # --check-only 不佈署 unit；改用安裝器自己的 --check-only 回報現況
    #（它會一併檢查啟動器的必要檔案是否齊全,缺就回 4）。
    run_rc bash "$AUTOSTART_INSTALLER" --check-only
    AUTOSTART_RC="$RC"
    [ "$AUTOSTART_RC" -eq 0 ] \
      && AUTOSTART_STATUS="現況正常$DRYRUN_NOTE" \
      || AUTOSTART_STATUS="現況有問題（rc=$AUTOSTART_RC）$DRYRUN_NOTE"
  elif [ ! -t 0 ]; then
    # 非互動終端機：不擅自更動 systemd / linger，僅提示手動指令。
    warn "非互動終端機，略過開機自動執行設定。"
    warn "如需設定，請手動執行：bash $AUTOSTART_INSTALLER"
    AUTOSTART_STATUS="略過（非互動終端機）"
  elif ask_yn "  是否設定開機自動啟動 scheduler（reboot_launcher.sh）？[Y/n] " Y; then
    # 捕捉離開碼判讀結果；install_autostart.sh 於非互動/無權限時不會中斷，
    # 這裡即使回非 0 也只警告，不影響 sftp_transfer 的部署結果。
    mutating "佈署 nssms-boot user unit"
    run_rc bash "$AUTOSTART_INSTALLER" --require-linger
    AUTOSTART_RC="$RC"
    case "$AUTOSTART_RC" in
      0) ok   "開機自動執行：已設定並啟用（service enabled + linger on）"
         AUTOSTART_STATUS="已啟用" ;;
      3) warn "開機自動執行：user service 已安裝，但 linger 未開啟；請手動執行 sudo loginctl enable-linger $(id -un)"
         AUTOSTART_STATUS="部分完成（linger 未開啟）" ;;
      4) warn "開機自動執行：設定失敗（rc=4）。可能原因:"
         warn "  * 缺少啟動器必要檔案(reboot_launcher.sh / reboot_script/roles.conf /"
         warn "    failover/effective_role.sh)—— 離線包不完整,見上方 install_autostart 的明細"
         warn "  * 找不到腳本 / 無法寫入 unit / systemd user manager 不可用"
         AUTOSTART_STATUS="設定失敗（rc=4）" ;;
      2) warn "開機自動執行：install_autostart.sh 參數錯誤（rc=2）"
         AUTOSTART_STATUS="設定失敗（參數錯誤）" ;;
      *) warn "開機自動執行：未預期的結果（rc=$AUTOSTART_RC），請檢視上方訊息"
         AUTOSTART_STATUS="未知（rc=$AUTOSTART_RC）" ;;
    esac
  else
    info "略過開機自動執行設定。日後可執行：bash $AUTOSTART_INSTALLER"
    AUTOSTART_STATUS="使用者略過"
  fi
}

# --- 一次性遷移：舊的 clink_* 系統服務 → nssms 常駐服務 --------------------
# alarm / board / button 原本是 /etc/systemd/system/ 下的三支 system unit（clink_*），
# 現已收編為 scheduler/services/ 的 user unit。這一步把舊的停掉並移除。
#
# **必須排在 install_services.sh 之前**：舊 unit 還活著時，新的 alarm/board 會撞 port
# （`OSError: [Errno 98] Address already in use`）。
#
# 順帶把使用者加進 gpio 群組：nssms-button 跑的 btn 是用 libgpiod 開 /dev/gpiochip*，
# 那些節點是 root:gpio 660，所以「gpio 群組成員」就足夠 —— 不需要保留一支 root unit、
# 不需要 sudoers 白名單。注意群組變更只對**新** session 生效，所以 button 可能要到
# 重登入 / 重開機才會起來（那是預期行為，不是失敗）。
#
# 冪等：舊 unit 不存在、使用者已在群組時，整段安靜跳過。
LEGACY_UNITS=(clink_alarm_controller clink_board_server clink_button)
MIGRATE_STATUS="未執行"

legacy_present() {  # 回傳 0 = 至少還有一支舊 unit 存在
  local u
  for u in "${LEGACY_UNITS[@]}"; do
    [ -f "/etc/systemd/system/${u}.service" ] && return 0
  done
  return 1
}
gpio_needed() {  # 回傳 0 = 需要加入 gpio 群組
  getent group gpio >/dev/null 2>&1 || return 1   # 沒有 gpio 群組就不用加
  id -nG "$(id -un)" | tr ' ' '\n' | grep -qx gpio && return 1
  return 0
}

stage_clink_migration() {
  echo ""
  info "檢查舊 clink_* 系統服務的遷移狀態 ..."
  if ! legacy_present && ! gpio_needed; then
    ok "無需遷移（舊 clink_* 不存在，且已在 gpio 群組）。"
    MIGRATE_STATUS="無需遷移"
  elif [ "$CHECK_ONLY" -eq 1 ]; then
    legacy_present && warn "仍存在舊 clink_* 系統服務（需遷移）：$(
      for u in "${LEGACY_UNITS[@]}"; do
        [ -f "/etc/systemd/system/${u}.service" ] && printf '%s ' "$u"
      done)"
    gpio_needed && warn "使用者 $(id -un) 尚未加入 gpio 群組（nssms-button 需要）。"
    MIGRATE_STATUS="待遷移$DRYRUN_NOTE"
  elif [ ! -t 0 ]; then
    warn "非互動終端機，略過 clink_* 遷移（需 sudo）。"
    warn "如需遷移，請手動執行："
    warn "  sudo systemctl disable --now ${LEGACY_UNITS[*]}"
    warn "  sudo rm -f /etc/systemd/system/clink_{alarm_controller,board_server,button}.service"
    warn "  sudo usermod -aG gpio $(id -un)   # 之後需重登入或重開機"
    MIGRATE_STATUS="略過（非互動終端機）"
  else
    legacy_present && warn "偵測到舊的 clink_* 系統服務，它們與新的 nssms 常駐服務會撞 port。"
    gpio_needed && info "另外需把 $(id -un) 加進 gpio 群組（nssms-button 讀 GPIO 用）。"
    if ask_yn "  現在執行遷移（停用並移除舊 clink_*、加入 gpio 群組）？（需輸入一次密碼）[Y/n] " Y; then
      mutating "停用移除舊 clink_* system unit / 加入 gpio 群組"
      MIGRATE_RC=0
      if legacy_present; then
        # 舊 unit 可能已經 disable 或本來就沒 enable，disable 失敗不算錯。
        sudo systemctl disable --now "${LEGACY_UNITS[@]}" 2>/dev/null || true
        for u in "${LEGACY_UNITS[@]}"; do
          sudo rm -f "/etc/systemd/system/${u}.service" || MIGRATE_RC=1
        done
        sudo systemctl daemon-reload || MIGRATE_RC=1
        [ "$MIGRATE_RC" -eq 0 ] && ok "已停用並移除舊 clink_* 系統服務。" \
                                || warn "舊 clink_* 移除時有項目失敗，請檢視上方訊息。"
      fi
      if gpio_needed; then
        run_rc sudo usermod -aG gpio "$(id -un)"
        if [ "$RC" -eq 0 ]; then
          ok "已把 $(id -un) 加進 gpio 群組。"
          warn "群組變更只對新 session 生效 —— nssms-button 要到重登入/重開機才會起來。"
        else
          warn "加入 gpio 群組失敗（exit=$RC），nssms-button 將無法讀取 GPIO。"
          MIGRATE_RC=1
        fi
      fi
      [ "$MIGRATE_RC" -eq 0 ] && MIGRATE_STATUS="已遷移" \
                              || MIGRATE_STATUS="部分完成"
    else
      warn "略過遷移。**新的 alarm / board 常駐服務會因 port 被舊 clink_* 佔用而起不來。**"
      MIGRATE_STATUS="使用者略過（新服務會撞 port）"
    fi
  fi
}

# --- docker 群組（scheduler/install_docker_group.sh） ----------------------
# web 平台的 docker compose 現在由 reboot_script/start_web_docker.sh 在開機流程中啟動,
# 而那支腳本跑在 systemd user session 裡 —— 沒有終端機、無法輸入 sudo 密碼。所以
# `sudo docker compose up -d`（web/site/install.sh 的做法）在開機流程裡行不通,必須讓
# 使用者本身就是 docker 群組成員。
#
# 語意與上面那段的「加入 gpio 群組」完全相同,擺在它後面是刻意的:兩者都是群組變更、
# 都需要密碼、都要重開機才生效,操作者一次看完一組同類型的事。
#
# 冪等：已在群組時安裝器自己會判斷並安靜跳過（rc=0）。
# 這一步失敗不中斷部署：web 起不來不影響 SHM / radar / 心跳等主要服務。
stage_docker_group() {
  DOCKER_GROUP_INSTALLER="${SHARE_DIR}/scheduler/install_docker_group.sh"
  DOCKER_GROUP_STATUS="未執行"
  echo ""
  info "檢查 docker 群組（web 平台開機自啟的前提）..."
  if [ ! -f "$DOCKER_GROUP_INSTALLER" ]; then
    warn "找不到 $DOCKER_GROUP_INSTALLER ，略過 docker 群組設定。"
    warn "web 平台將無法由開機流程啟動（仍可人工 sudo docker compose up -d）。"
    DOCKER_GROUP_STATUS="略過（找不到安裝腳本）"
  elif [ "$CHECK_ONLY" -eq 1 ]; then
    run_rc bash "$DOCKER_GROUP_INSTALLER" --check-only
    DOCKER_RC="$RC"
    case "$DOCKER_RC" in
      0) DOCKER_GROUP_STATUS="已就緒" ;;
      3) DOCKER_GROUP_STATUS="已加入，待重開機生效" ;;
      4) DOCKER_GROUP_STATUS="docker 未安裝$DRYRUN_NOTE" ;;
      *) DOCKER_GROUP_STATUS="未加入$DRYRUN_NOTE" ;;
    esac
  elif [ ! -t 0 ]; then
    warn "非互動終端機，略過 docker 群組設定（需 sudo）。"
    warn "如需設定，請手動執行：bash $DOCKER_GROUP_INSTALLER"
    DOCKER_GROUP_STATUS="略過（非互動終端機）"
  else
    # 先唯讀探一次:已就緒/已加入待重開機/沒 docker 這三種情況都不需要問任何問題。
    run_rc bash "$DOCKER_GROUP_INSTALLER" --check-only >/dev/null 2>&1
    DOCKER_RC="$RC"
    if [ "$DOCKER_RC" -eq 0 ]; then
      ok "docker 群組已就緒（$(id -un) 可直接使用 docker）。"
      DOCKER_GROUP_STATUS="已就緒"
    elif [ "$DOCKER_RC" -eq 3 ]; then
      ok "docker 群組已設定，等重開機後生效。"
      DOCKER_GROUP_STATUS="已加入，待重開機生效"
    elif [ "$DOCKER_RC" -eq 4 ]; then
      warn "本機沒有 docker（或沒有 docker 群組），略過。web 平台無法在此機器上啟動。"
      DOCKER_GROUP_STATUS="docker 未安裝"
    else
      if ask_yn "  把 $(id -un) 加進 docker 群組（web 平台開機自啟的前提）？（需輸入一次密碼）[Y/n] " Y; then
        mutating "把使用者加進 docker 群組"
        run_rc bash "$DOCKER_GROUP_INSTALLER"
        DOCKER_RC="$RC"
        case "$DOCKER_RC" in
          0) ok "docker 群組已就緒。"
             DOCKER_GROUP_STATUS="已就緒" ;;
          3) ok "已加入 docker 群組；群組變更只對新 session 生效 —— 需重開機。"
             warn "在重開機之前，start_web_docker.sh 會因權限不足而起不來 web 平台（預期行為）。"
             DOCKER_GROUP_STATUS="已加入，待重開機生效" ;;
          *) warn "docker 群組設定失敗（exit=$DOCKER_RC），web 平台將無法由開機流程啟動。"
             DOCKER_GROUP_STATUS="失敗（exit=$DOCKER_RC）" ;;
        esac
      else
        info "略過 docker 群組設定。日後可執行：bash $DOCKER_GROUP_INSTALLER"
        warn "未加入群組時，開機流程中的 web 平台（start_web_docker.sh）會起不來。"
        DOCKER_GROUP_STATUS="使用者略過"
      fi
    fi
  fi
}

# --- 無人值守開機的兩個前提（scheduler 的兩支一次性 root 設定器）------------
# 這兩支修的是同一件事的兩半:**開機那一刻沒有人登入圖形桌面**。同一份程式碼在
# autologin=true 的機台上會成功、在 false 的機台上每次都失敗,而那個逐機設定從來沒有被
# 宣告過(CLINK/WHA02=true、WH332=false,三台的來源與交付時序沒有紀錄)。
#
#   * 資料碟掛載 —— 這顆碟 HintSystem=true,polkit 套用的是
#     org.freedesktop.udisks2.filesystem-mount-**system**,三條路預設全要 admin 認證。
#     於是掛載只在「開機那一刻該使用者剛好有一個帶認證代理的圖形 session」時才成功。
#     install_udisks_mount_policy.sh 補一份本機授權覆蓋把那個前提移除(WH332 實機驗收過)。
#   * 實體桌面 —— 沒有 autologin 的機台開機後 :0 上坐的是 GDM greeter(身分是 gdm,不是
#     使用者),使用者根本還沒有 X display。WH335 實測開機 07:11、圖形登入 07:44,中間
#     33 分鐘任何要開視窗的服務都會秒退。install_gdm_autologin.sh 設定 GDM 自動登入。
#
# 證據:scheduler/docs/nvme-boot-mount-incident.md §1/§2.1/§4、
#       scheduler/docs/ecdis-x-display-without-autologin.md。
#
# 【兩題都預設 Y,因為兩者都是船機的應然狀態,不是選項】
#   * polkit 授權:它只放行「掛這顆碟」這一個 action 給這一個帳號,沒有其他曝險;少了它,
#     radar / ecdis / wave / web 會在下一次開機一起死,而且沒有任何錯誤訊息。
#   * 自動登入:**全船隊的機器都應該是開的**。這台是駕駛台的操作終端,開機後要自己進到
#     桌面把該顯示的東西顯示出來 —— 沒有人會在開機後跑去鍵盤前打密碼。實測 false 的機台
#     (WH332)不是誰做過的保安決定,而是**安裝時的設定失誤**:那個逐機漂移從來沒有被
#     宣告過,也沒有人負責(CLINK/WHA02=true、WH332=false,三台的來源與交付時序沒有紀錄,
#     見事故報告 §2.1「另一項」)。它一關,後續一整排要開視窗的服務都起不來。
#     所以這一題的提示不是「要不要開」,而是「偵測到這台沒開 —— 這是裝錯了,現在補」。
#
# 【這一段只負責裝,絕不宣告修好了】兩支修的東西**都只有重開機才驗得出來**,而部署流程
# 最後那一次啟動不是重開機。所以總結只會說「已安裝,待重開機驗證」。
#
# 兩支的離開碼契約相同(刻意對齊,這裡才能用同一套 case 判讀):
#   0 就緒/成功  1 尚未就緒或失敗  2 參數錯誤  3 需要 root  4 前提不成立  5 沙盒守衛
#   6 無從判定(讀不到 → **不代表沒裝**)
# 這一步失敗不中斷部署:它修的是「下一次開機」,不是這一次部署。
stage_unattended_boot() {
  MOUNT_POLICY_INSTALLER="${SHARE_DIR}/scheduler/install_udisks_mount_policy.sh"
  GDM_AUTOLOGIN_INSTALLER="${SHARE_DIR}/scheduler/install_gdm_autologin.sh"
  MOUNT_POLICY_STATUS="未執行"
  GDM_AUTOLOGIN_STATUS="未執行"

  echo ""
  info "檢查無人值守開機的兩個前提（資料碟掛載授權 / GDM 自動登入）..."

  # 兩支 --status 需要的權限不同:polkit 那份住在 0700 的 50-local.d，一般使用者連列都列
  # 不出來（它會誠實回 6「無從判定」而不是「沒安裝」——「看不到」不等於「沒有」）;
  # GDM 的 custom.conf 是 0644，一般使用者就讀得到。所以先試 sudo -n（前面幾個階段多半
  # 已經把憑證快取住了），拿不到就退回一般身分，讓安裝器自己去回報無從判定。
  unattended_probe() {  # $1 = 安裝器路徑；結果留在全域 RC
    if sudo -n true 2>/dev/null; then
      run_rc sudo -n bash "$1" --status
    else
      run_rc bash "$1" --status
    fi
  }

  # ---- (1) 資料碟掛載的 polkit 授權 ----
  if [ ! -f "$MOUNT_POLICY_INSTALLER" ]; then
    warn "找不到 $MOUNT_POLICY_INSTALLER ，略過資料碟掛載授權。"
    warn "這台的資料碟將繼續依賴「開機那一刻有圖形登入」——autologin=false 的機台每次開機都掛不上。"
    MOUNT_POLICY_STATUS="略過（找不到安裝腳本）"
  else
    unattended_probe "$MOUNT_POLICY_INSTALLER" >/dev/null 2>&1
    MP_RC="$RC"
    if [ "$CHECK_ONLY" -eq 1 ]; then
      case "$MP_RC" in
        0) MOUNT_POLICY_STATUS="已就緒" ;;
        4) MOUNT_POLICY_STATUS="前提不成立（polkit 0.106+ 或帳號問題）$DRYRUN_NOTE" ;;
        6) MOUNT_POLICY_STATUS="無從判定（讀不到 50-local.d）$DRYRUN_NOTE" ;;
        *) MOUNT_POLICY_STATUS="尚未安裝$DRYRUN_NOTE" ;;
      esac
    elif [ "$MP_RC" -eq 0 ]; then
      ok "資料碟掛載授權已就緒。"
      MOUNT_POLICY_STATUS="已就緒"
    elif [ "$MP_RC" -eq 4 ]; then
      warn "資料碟掛載授權的前提不成立（polkit 0.106+ 或帳號問題）——安裝器會說明原因:"
      warn "  bash $MOUNT_POLICY_INSTALLER --status"
      MOUNT_POLICY_STATUS="前提不成立（未安裝）"
    elif [ ! -t 0 ]; then
      warn "非互動終端機，略過資料碟掛載授權（需 sudo）。"
      warn "如需安裝，請手動執行:sudo bash $MOUNT_POLICY_INSTALLER"
      MOUNT_POLICY_STATUS="略過（非互動終端機）"
    elif ask_yn "  安裝資料碟掛載的 polkit 授權？不裝的話,autologin=false 的機台每次開機都掛不上碟（需輸入一次密碼）[Y/n] " Y; then
      mutating "安裝資料碟掛載的 polkit 授權"
      run_rc sudo bash "$MOUNT_POLICY_INSTALLER"
      MP_RC="$RC"
      case "$MP_RC" in
        0) ok "資料碟掛載授權已安裝——**要重開機後看開機快照才算驗證過**。"
           MOUNT_POLICY_STATUS="已安裝，待重開機驗證" ;;
        4) warn "前提不成立，未安裝（安裝器已說明原因）。"
           MOUNT_POLICY_STATUS="前提不成立（未安裝）" ;;
        *) warn "資料碟掛載授權安裝失敗（exit=$MP_RC）。"
           MOUNT_POLICY_STATUS="失敗（exit=$MP_RC）" ;;
      esac
    else
      info "略過資料碟掛載授權。日後可執行:sudo bash $MOUNT_POLICY_INSTALLER"
      warn "在那之前,這台的資料碟仍依賴「開機那一刻有帶認證代理的圖形 session」。"
      MOUNT_POLICY_STATUS="使用者略過"
    fi
  fi

  # ---- (2) GDM 自動登入 ----
  echo ""
  if [ ! -f "$GDM_AUTOLOGIN_INSTALLER" ]; then
    warn "找不到 $GDM_AUTOLOGIN_INSTALLER ，略過 GDM 自動登入設定。"
    warn "舊的離線包不帶這一支;若這台沒開自動登入,開機後不會有使用者的圖形 session。"
    warn "請以較新的離線包重跑,或人工設定 /etc/gdm3/custom.conf 的 AutomaticLoginEnable=true。"
    GDM_AUTOLOGIN_STATUS="略過（找不到安裝腳本）"
  else
    # 這一支的 --status 不需要 root（custom.conf 是 0644），所以直接跑,不去動 sudo 憑證。
    run_rc bash "$GDM_AUTOLOGIN_INSTALLER" --status >/dev/null 2>&1
    GA_RC="$RC"
    if [ "$CHECK_ONLY" -eq 1 ]; then
      case "$GA_RC" in
        0) GDM_AUTOLOGIN_STATUS="已開啟" ;;
        4) GDM_AUTOLOGIN_STATUS="前提不成立（不是 GDM 或設定檔異常）$DRYRUN_NOTE" ;;
        6) GDM_AUTOLOGIN_STATUS="無從判定（讀不到 custom.conf）$DRYRUN_NOTE" ;;
        *) GDM_AUTOLOGIN_STATUS="未開啟$DRYRUN_NOTE" ;;
      esac
    elif [ "$GA_RC" -eq 0 ]; then
      ok "GDM 自動登入已開啟。"
      GDM_AUTOLOGIN_STATUS="已開啟"
    elif [ "$GA_RC" -eq 4 ]; then
      warn "GDM 自動登入的前提不成立（這台可能不是 GDM,或設定檔沒有 [daemon] 段）:"
      warn "  bash $GDM_AUTOLOGIN_INSTALLER --status"
      GDM_AUTOLOGIN_STATUS="前提不成立（未設定）"
    elif [ ! -t 0 ]; then
      warn "非互動終端機，略過 GDM 自動登入設定（需 sudo）。"
      warn "如需設定，請手動執行:sudo bash $GDM_AUTOLOGIN_INSTALLER"
      GDM_AUTOLOGIN_STATUS="略過（非互動終端機）"
    else
      # 【這一題的問法】見本函式檔頭:autologin=false 不是一個選項,是一台裝錯的機器。
      # 所以提示先說「這台沒開」是異常,再問要不要補 —— 而不是中性地問「要不要開」,
      # 那會讓操作者以為兩個答案一樣好。
      warn "這台**沒有開自動登入** —— 全船隊的機器都應該是開的,這通常是安裝時的設定失誤。"
      warn "沒開的後果:開機後 :0 上坐的是 GDM greeter,使用者的桌面根本不存在,"
      warn "在有人手動登入之前,任何要開視窗的服務都會秒退（WH335 實測那段是 33 分鐘）。"
      if ask_yn "  現在補上 GDM 自動登入（$(id -un)）？（需輸入一次密碼）[Y/n] " Y; then
        mutating "設定 GDM 自動登入"
        run_rc sudo bash "$GDM_AUTOLOGIN_INSTALLER"
        GA_RC="$RC"
        case "$GA_RC" in
          0) ok "GDM 自動登入已設定——**下次開機生效**（本腳本刻意不重啟 gdm3,那會殺掉當下的圖形 session）。"
             GDM_AUTOLOGIN_STATUS="已設定，待重開機生效" ;;
          4) warn "前提不成立，未設定（安裝器已說明原因）。"
             GDM_AUTOLOGIN_STATUS="前提不成立（未設定）" ;;
          *) warn "GDM 自動登入設定失敗（exit=$GA_RC）。"
             GDM_AUTOLOGIN_STATUS="失敗（exit=$GA_RC）" ;;
        esac
      else
        info "略過 GDM 自動登入。日後可執行:sudo bash $GDM_AUTOLOGIN_INSTALLER --user $(id -un)"
        warn "**這台會以一個已知不完整的狀態交船**:開機後不會有使用者的圖形 session,"
        warn "要開視窗的服務一律起不來(setup_display 會短等就 exit 0 —— 那是它的預期行為,"
        warn "不是開機失敗,所以 log 上也不會有紅字告訴你這件事)。"
        GDM_AUTOLOGIN_STATUS="**使用者略過（這台會缺自動登入）**"
      fi
    fi
  fi
}

# --- 週期排程設定（scheduler/install_timers.sh + sudoers 白名單） ----------
# 與開機自動執行同屬「需使用者留意的一次性設定」：
#   1) install_timers.sh 佈署/啟用 systemd user timer（純 user 層，免 root）。
#   2) reboot / teamviewer 這兩支 timer 需 root，改由極窄的 /etc/sudoers.d 白名單
#      放行；安裝白名單需一次性輸入密碼（sudo）——趁部署互動時一併完成。
# 兩步皆冪等；非互動終端機時不擅自更動，僅印出手動指令。
stage_scheduler_units() {
  TIMERS_INSTALLER="${SHARE_DIR}/scheduler/install_timers.sh"
  SERVICES_INSTALLER="${SHARE_DIR}/scheduler/install_services.sh"
  SUDOERS_SRC="${SHARE_DIR}/scheduler/etc/nssms-scheduler.sudoers"
  SUDOERS_DST="/etc/sudoers.d/nssms-scheduler"
  SCHED_STATUS="未執行"
  SUDOERS_STATUS="未執行"
  SERVICES_STATUS="未執行"
  echo ""
  info "檢查週期排程設定 ..."
  if [ ! -f "$TIMERS_INSTALLER" ]; then
    warn "找不到 $TIMERS_INSTALLER ，略過週期排程設定。"
    SCHED_STATUS="略過（找不到安裝腳本）"
  elif [ "$CHECK_ONLY" -eq 1 ]; then
    # --check-only 不佈署 timer、不裝 sudoers、不重啟 heartbeat;只回報現況。
    run_rc bash "$TIMERS_INSTALLER" --check-only
    SCHED_STATUS="僅回報現況$DRYRUN_NOTE"
    [ -f "$SUDOERS_DST" ] && SUDOERS_STATUS="已存在" || SUDOERS_STATUS="未安裝$DRYRUN_NOTE"
    if [ -f "$SERVICES_INSTALLER" ]; then
      run_rc bash "$SERVICES_INSTALLER" --check-only
      SERVICES_STATUS="僅回報現況$DRYRUN_NOTE"
    else
      SERVICES_STATUS="略過（找不到安裝腳本）"
    fi
  elif [ ! -t 0 ]; then
    warn "非互動終端機，略過週期排程設定。"
    warn "如需設定，請手動執行：bash $TIMERS_INSTALLER"
    warn "reboot / teamviewer 需 sudo 白名單，見 $SUDOERS_SRC 檔頭安裝說明。"
    SCHED_STATUS="略過（非互動終端機）"
  elif ask_yn "  是否設定週期排程與 IPC1↔IPC2 接管（IPC3 只裝通用 timer，不啟用心跳/failover）？[Y/n] " Y; then
    # 三個動作(timer / sudoers / 常駐服務)刻意只問一題:它們是同一個概念單位,分開問只會
    # 讓操作者面對三個不知道能不能各自拒絕的問題。
    mutating "佈署 timer / sudoers / 常駐服務"

    # (1) 佈署 / 啟用 timer（user 層，免 root；失敗只警告不中斷部署）
    run_rc bash "$TIMERS_INSTALLER"
    TIMERS_RC="$RC"
    if [ "$TIMERS_RC" -eq 0 ]; then
      ok "週期排程 timer 已佈署並啟用。"
      SCHED_STATUS="已啟用"
    else
      warn "週期排程 timer 設定有項目失敗（exit=$TIMERS_RC），請檢視上方訊息。"
      SCHED_STATUS="部分完成（exit=$TIMERS_RC）"
    fi

    # (2) sudo 白名單（reboot / teamviewer 需要；此步需輸入密碼一次）
    echo ""
    if [ ! -f "$SUDOERS_SRC" ]; then
      warn "找不到 $SUDOERS_SRC ，略過 sudo 白名單安裝。"
      warn "未安裝白名單時，reboot / teamviewer 兩支 timer 會因 sudo 需密碼而失敗。"
      SUDOERS_STATUS="略過（找不到來源檔）"
    else
      # 先把「這台機器應該長的樣子」渲染出來,才能拿去跟已安裝的那份比對。
      #   * 開頭的使用者欄位:來源檔預設 mic-733ao,換人也正確。
      #   * 規則裡內嵌的絕對路徑:白名單是**逐字比對**指令路徑的,只換使用者欄位而不換
      #     路徑的話,換名機器上的規則會指到不存在的 /home/mic-733ao/...,sudo 永遠比對
      #     不到(失敗方向安全:清不掉、資料保留,但那一條規則等於沒裝)。
      CUR_USER="$(id -un)"
      TMP_SUDOERS="$(mktemp)"
      sed -e "s/^mic-733ao /${CUR_USER} /" \
          -e "s#/home/mic-733ao/#${HOME%/}/#g" "$SUDOERS_SRC" > "$TMP_SUDOERS"

      # 【為什麼不是「檔案存在就沿用」】改版前這裡只看 $SUDOERS_DST 存不存在,存在就完全
      # 不比對內容。於是白名單一旦裝過,**任何後續新增的規則都永遠傳不到已部署的船上**
      # ——而失敗是靜默的:那條規則對應的功能只會安靜地不動作。實際踩到的是
      # nssms-shipboard-alert-upload 的第三條規則(清 UPLOAD_DATA_DIR):沒有它,上傳與
      # 驗證都成功、清空卻失敗,包裹目錄會無限成長,而 timer 每小時重試一次。
      #
      # cmp 需要讀 /etc/sudoers.d/ 底下的檔(0440 root:root,一般使用者讀不到),所以用
      # sudo -n:此時 sudo 憑證通常已被前面幾個階段(linger / clink 遷移 / docker 群組)
      # 快取住,比對不需要再問一次密碼。無法免密碼比對時就落到下面的安裝分支——寧可多問
      # 一次密碼、重裝一份內容相同的檔案,也不要漏掉規則。內容相同時完全不動作。
      if [ -f "$SUDOERS_DST" ] && sudo -n cmp -s "$TMP_SUDOERS" "$SUDOERS_DST" 2>/dev/null; then
        ok "sudo 白名單已是最新（$SUDOERS_DST），無需變更。"
        SUDOERS_STATUS="已是最新"
        rm -f "$TMP_SUDOERS"
      elif ask_yn "  timer 需 sudo 白名單（reboot / teamviewer / 清理 upload_data），現在安裝或更新？（需輸入一次密碼）[Y/n] " Y; then
        mutating "安裝/更新 sudo 白名單"
        # 先驗證語法（絕不安裝壞掉的 sudoers，以免打壞整個 sudo）。
        if sudo visudo -c -f "$TMP_SUDOERS" >/dev/null 2>&1; then
          if sudo install -m 0440 -o root -g root "$TMP_SUDOERS" "$SUDOERS_DST"; then
            ok "已安裝/更新 sudo 白名單：$SUDOERS_DST"
            SUDOERS_STATUS="已安裝"
          else
            warn "sudo 白名單安裝失敗（install 失敗）。"
            SUDOERS_STATUS="安裝失敗"
          fi
        else
          warn "sudo 白名單語法驗證未通過，未安裝（避免打壞 sudo）。"
          SUDOERS_STATUS="驗證失敗（未安裝）"
        fi
        rm -f "$TMP_SUDOERS"
      else
        info "略過 sudo 白名單安裝。日後可依 $SUDOERS_SRC 檔頭說明手動安裝。"
        SUDOERS_STATUS="使用者略過"
        rm -f "$TMP_SUDOERS"
      fi
    fi

    # (3) 常駐服務（user 層,免 root）：
    #     nssms-heartbeat（僅實體 IPC1/IPC2 佈署，角色自動分派；IPC3 為 N/A）
    #     nssms-alarm-controller / nssms-board-server / nssms-button
    #       （硬體實體綁 IPC-1，由 install_services.sh 依 NSSMS-BaseIPC 判定）
    #
    #     舊 clink_* 的停用**必須排在這之前**（見上方一次性遷移段）：舊的 system unit 還
    #     活著時，新 unit 會撞 port。這裡只負責裝。
    echo ""
    if [ ! -f "$SERVICES_INSTALLER" ]; then
      warn "找不到 $SERVICES_INSTALLER ，略過常駐服務安裝。"
      SERVICES_STATUS="略過（找不到安裝腳本）"
    else
      run_rc bash "$SERVICES_INSTALLER"
      SV_RC="$RC"
      if [ "$SV_RC" -eq 0 ]; then
        ok "常駐服務已佈署並啟用（heartbeat / alarm / board / button）。"
        SERVICES_STATUS="已啟用"
      else
        warn "常駐服務安裝有項目失敗（exit=$SV_RC），請檢視上方訊息。"
        SERVICES_STATUS="部分完成（exit=$SV_RC）"
      fi
    fi
  else
    info "略過週期排程設定。日後可執行：bash $TIMERS_INSTALLER"
    SCHED_STATUS="使用者略過"
  fi
}

# --- tmux 離線補齊（deploy/install_tmux_offline.sh + debs/） ----------------
# scheduler 的整個開機服務模型建立在 tmux 之上:每一支 reboot_script/start_*.sh 都以
# `tmux new-session` 啟動,reboot_launcher.sh 以 `tmux has-session` 做差異對帳。少了它,
# 啟動流程會一項一項 exit 2,而啟動器對個別失敗是「記錄並繼續」—— 於是總結看起來只是
# 「有項目失敗」,要交叉三份 log 才會發現原因是缺一個指令。船上又沒有對外網路,
# `apt install tmux` 不成立,所以這件事在船上原本**無法自救**。
#
# 語意與上面兩段的群組設定同一類:一次性、需要密碼、由專用安裝器負責。tmux 是啟動
# 前提，所以缺 sudo 或 dpkg 失敗時整份部署必須停止，不再留下「部署成功但全無 session」。
#
# **必須排在 stage_launch_decision 之前**:那一題的提示要據 TMUX_STATUS 警告操作者
# 「現在按 Y 立刻啟動,session 型專案會全部起不來」。
#
# 冪等:tmux 已可用時安裝器自己會判斷並安靜跳過（rc=0），這裡不問任何問題。
# 離開碼見 install_tmux_offline.sh 檔頭:0 就緒 / 4 離線包不完整 /
# 5 (--check-only) 待安裝 / 6 平台不相容 / 1 失敗。
stage_tmux() {
  TMUX_INSTALLER="${SCRIPT_DIR}/install_tmux_offline.sh"
  TMUX_STATUS="未執行"
  echo ""
  info "檢查 tmux（scheduler 所有 session 型專案的前提）..."
  if [ ! -f "$TMUX_INSTALLER" ]; then
    warn "找不到 $TMUX_INSTALLER ，略過 tmux 檢查。"
    warn "若本機沒有 tmux，啟動流程的 session 型專案會全部起不來。"
    TMUX_STATUS="略過（找不到安裝腳本）"
    return
  fi
  # preflight 已做過全套 asset/ABI probe；這裡再判斷現有 tmux 能否保留。
  run_rc bash "$TMUX_INSTALLER" --check-only --profile-dir "$PROFILE_DIR"
  TMUX_RC="$RC"
  if [ "$TMUX_RC" -eq 0 ]; then
    ok "tmux 已可用（$(tmux -V 2>/dev/null || echo '版本未知')）。"
    TMUX_STATUS="已就緒"
    return
  fi
  # main 的 --check-only 已在全域 preflight 結束；以下只可能是正式部署。
  warn "本機的 tmux 不可用 —— 所有 session 型專案（shm / radar / wave / ecdis / flag）都起不來。"
  if [ "$TMUX_RC" -ne 5 ]; then
    err "tmux 狀態或離線資產異常（exit=$TMUX_RC），停止部署。"
    exit "$TMUX_RC"
  fi
  if [ ! -t 0 ]; then
    info "非互動終端機：只有 sudo 已預先授權時才能安裝 tmux。"
  elif ! ask_yn "  現在以隨附的 deb 離線安裝 tmux？（需輸入一次密碼）[Y/n] " Y; then
    err "tmux 是完整部署的必要條件；使用者取消安裝，停止部署。"
    exit 1
  fi
  mutating "以 dpkg 離線安裝 tmux"
  run_rc bash "$TMUX_INSTALLER" --profile-dir "$PROFILE_DIR"
  TMUX_RC="$RC"
  if [ "$TMUX_RC" -ne 0 ]; then
    err "tmux 安裝失敗（exit=$TMUX_RC），停止部署。"
    exit "$TMUX_RC"
  fi
  ok "tmux 已安裝並通過 session 能力測試（dpkg）。"
  TMUX_STATUS="已安裝（dpkg / $NSSMS_PROFILE_ID）"
}

# --- 照片同步的 SSH 金鑰（scheduler/install_setup_ssh_key.sh）---------------
# nssms-download-photos.timer（每 4 小時，僅實體 IPC-2）會跑 script/download_photos.sh，
# 而那支腳本以 `ssh -o BatchMode=yes` 連遠端 nsms master —— **沒有金鑰就是立刻失敗**，
# 不會有人在旁邊輸入密碼。所以金鑰必須在這裡（唯一的人工互動視窗）一併設好。
#
# 這一步要輸入的是**遠端主機的密碼**（給 ssh-copy-id），不是本機 sudo —— 與 gpio /
# docker 群組 / sudoers 那幾步性質不同，但同樣「只有現在有人在鍵盤前」。
#
# **只在實體 IPC-2 上做。** 閘門刻意重用 timer 用的同一支 services/require_base_ipc.sh，
# 而不是在這裡自己判一次身分：兩份判定必然有一天不一致，而不一致不會有任何執行期錯誤 ——
# 只會讓某台機器安靜地少設一把金鑰。用 base ipc（而非 DEPLOY_ROLE）也和 timer 一致：
# 接管只寫 failover 旗標、不改 `ipc`，所以 ipc2emer 期間照樣要有金鑰。
#
# 冪等：install_setup_ssh_key.sh 會先以 BatchMode 探測，已就緒就直接回 0、不問密碼，
# 所以重跑部署不會再卡在提示上。
stage_ssh_key() {
  SSH_KEY_INSTALLER="${SHARE_DIR}/scheduler/install_setup_ssh_key.sh"
  BASE_IPC_GATE="${SHARE_DIR}/scheduler/services/require_base_ipc.sh"
  SSH_KEY_STATUS="未執行"
  echo ""
  info "檢查照片同步的 SSH 金鑰（nssms-download-photos 的前提）..."

  if [ ! -f "$SSH_KEY_INSTALLER" ]; then
    warn "找不到 $SSH_KEY_INSTALLER ，略過金鑰設定。"
    warn "若本機是 IPC-2，照片同步排程會每 4 小時失敗一次（ssh 無金鑰可用）。"
    SSH_KEY_STATUS="略過（找不到安裝腳本）"
    return
  fi

  # 角色閘門。找不到閘門腳本時**不擅自代它決定**：照樣往下走，讓安裝器自己判斷
  # （最壞情況是在 IPC-1 上多問一題，比在 IPC-2 上安靜跳過安全得多）。
  if [ -f "$BASE_IPC_GATE" ]; then
    if ! bash "$BASE_IPC_GATE" ipc2 >/dev/null 2>&1; then
      info "本機實體身分不是 IPC-2 —— 照片同步排程不會在此執行，略過金鑰設定。"
      SSH_KEY_STATUS="不適用（非實體 IPC-2）"
      return
    fi
  else
    warn "找不到 $BASE_IPC_GATE ，無法判定實體身分，照樣檢查金鑰。"
  fi

  # 先唯讀探一次：已就緒（rc=0）就什麼都不必問，這是重跑部署時的絕大多數情況。
  run_rc bash "$SSH_KEY_INSTALLER" --check-only
  SSH_KEY_RC="$RC"
  if [ "$SSH_KEY_RC" -eq 0 ]; then
    ok "照片同步的免密碼登入已可用。"
    SSH_KEY_STATUS="已就緒"
    return
  fi

  if [ "$SSH_KEY_RC" -eq 4 ]; then
    # 缺 ssh-copy-id 等指令，問也沒用 —— 沒有工具可以用。
    warn "本機缺少 ssh / ssh-keygen / ssh-copy-id，無法設定金鑰登入。"
    SSH_KEY_STATUS="無法設定（缺 openssh-client）"
    return
  fi

  if [ "$CHECK_ONLY" -eq 1 ]; then
    SSH_KEY_STATUS="尚未設定$DRYRUN_NOTE"
    return
  fi

  if [ ! -t 0 ]; then
    # 非互動：ssh-copy-id 需要遠端密碼，沒有 tty 就無從輸入。比照 gpio / docker 群組
    # 那兩步的處理 —— 放棄並印出手動指令，而不是跑一個必定失敗的 ssh-copy-id。
    warn "非互動終端機，略過金鑰設定（ssh-copy-id 需輸入遠端主機密碼）。"
    warn "如需設定，請手動執行：bash $SSH_KEY_INSTALLER"
    SSH_KEY_STATUS="略過（非互動終端機）"
    return
  fi

  if ! ask_yn "  設定照片同步的 SSH 金鑰登入？（需輸入一次**遠端主機**的密碼）[Y/n] " Y; then
    info "略過金鑰設定。日後可執行：bash $SSH_KEY_INSTALLER"
    warn "在設定之前，nssms-download-photos.timer 每 4 小時會失敗一次（ssh 無金鑰）。"
    SSH_KEY_STATUS="使用者略過（照片同步會失敗）"
    return
  fi

  mutating "產生 SSH 金鑰並複製公鑰到遠端主機"
  run_rc bash "$SSH_KEY_INSTALLER"
  SSH_KEY_RC="$RC"
  case "$SSH_KEY_RC" in
    0) ok "照片同步的免密碼登入已設定完成。"
       SSH_KEY_STATUS="已設定" ;;
    4) warn "本機缺少 ssh / ssh-keygen / ssh-copy-id，未設定。"
       SSH_KEY_STATUS="無法設定（缺 openssh-client）" ;;
    *) warn "金鑰設定失敗（exit=$SSH_KEY_RC），照片同步排程會每 4 小時失敗一次。"
       warn "常見原因：遠端主機沒開機、IP 不符、密碼輸入錯誤。日後可重跑：bash $SSH_KEY_INSTALLER"
       SSH_KEY_STATUS="失敗（exit=$SSH_KEY_RC）" ;;
  esac
}

# --- 是否於部署完成後立即執行完整啟動流程（只收集決定，執行在最後面） ------
# 到目前為止只做完「一次性設定」:身分、systemd 骨架、sudoers。各專案的**程式碼、環境安裝
# 與服務啟動**全部在啟動流程裡:
#     reboot_launcher.sh → update_booster.sh(SFTP 拉最新程式碼)→ 依角色套用 update+env+run
# 少了它,部署跑完機器上一個 tmux session 都沒有,而總結卻是一排「已啟用」。
#
# **決定在這裡收集,執行放到最後面。** 理由:接下來的 venv 建置是一長段無人干預的流程,
# 若把詢問放在它之後,操作者就得守在機器前等它跑完才能回答那一題 —— 所有需要人輸入的東西
# 都該集中在最前面。(而且啟動流程本身可能要數分鐘,問完就能一路跑到底。)
#
# 選 n 也不會壞:只要前面的開機 unit 裝成功了,下次開機 nssms-boot 就會跑同一支啟動器。
stage_launch_decision() {
  LAUNCHER="${SHARE_DIR}/scheduler/reboot_launcher.sh"
  LAUNCH_STATUS="未執行"
  LAUNCH_DECISION="skip"
  echo ""
  info "檢查是否於部署完成後立即執行完整啟動流程 ..."
  if [ "$CHECK_ONLY" -eq 1 ]; then
    warn "--check-only:不執行啟動流程。$DRYRUN_NOTE"
    LAUNCH_STATUS="略過（--check-only）"
  elif [ "$RUN_LAUNCH" -eq 0 ]; then
    info "--no-launch:略過啟動流程。"
    LAUNCH_STATUS="略過（--no-launch）"
  elif [ ! -f "$LAUNCHER" ]; then
    warn "找不到 $LAUNCHER ，略過啟動流程。"
    LAUNCH_STATUS="略過（找不到啟動器）"
  elif [ ! -t 0 ]; then
    warn "非互動終端機:不擅自啟動服務。"
    warn "如需啟動請於部署後執行:bash $LAUNCHER"
    LAUNCH_STATUS="略過（非互動終端機）"
  else
    info "它會:掛載資料碟 → SFTP 拉最新程式碼 → 安裝各專案環境 → 啟動服務。"
    info "首次部署沒有 launcher_state.json,所以是全相位套用,可能需要數分鐘。"
    info "會在本腳本的最後、健康檢查之前執行(這之後不再需要你輸入任何東西)。"
    if [ "${DEPLOY_VSL_UPPER:-}" = "CLINK" ]; then
      warn "本機 vsl_name=CLINK(開發機):update_booster 會刻意略過整個 OTA,"
      warn "所以**不會**下載程式碼,只會用機上現有版本啟動。"
    fi
    # 上一步(stage_tmux)剛判定過 tmux。沒有它就沒有 session 可開,現在啟動只會得到一份
    # 「一堆項目失敗」的紀錄 —— 那不是啟動失敗,是前提不成立,值得在按 Y 之前先說清楚。
    case "$TMUX_STATUS" in
      已就緒|已安裝*) ;;
      *)
        warn "本機 tmux 不可用($TMUX_STATUS):session 型專案(shm / radar / wave /"
        warn "ecdis / flag)會全部 exit 2。先補齊 tmux 再啟動比較有意義:"
        warn "  bash ${SCRIPT_DIR}/install_tmux_offline.sh"
        ;;
    esac
    if ask_yn "  部署完成後立即執行?（選 n 則下次開機由 nssms-boot 自動跑）[Y/n] " Y; then
      LAUNCH_DECISION="run"; ok "已排入:部署完成後會執行一次完整啟動流程。"
    else
      LAUNCH_STATUS="使用者略過"
      info "略過。下次開機 nssms-boot 會自動執行,或手動:bash $LAUNCHER"
    fi
  fi
}


stage_wheelhouse_and_venv() {
  if [ ! -d "$WHEELHOUSE" ]; then
    err "wheelhouse 目錄不存在：$WHEELHOUSE"; exit 1
  fi
  WHL_COUNT=$(find "$WHEELHOUSE" -maxdepth 1 -name '*.whl' | wc -l | tr -d ' ')
  if [ "$WHL_COUNT" -eq 0 ]; then
    err "wheelhouse 內沒有任何 .whl 檔案"; exit 1
  fi
  ok "找到 $WHL_COUNT 個 wheel 檔案"

  # 建立 venv 改用 python3.10 -m virtualenv（與 radar / SHM 一致，不再依賴系統
  # python3-venv / ensurepip）。若目標直譯器尚未安裝 virtualenv，先以隨附的離線
  # 安裝腳本補齊（install_virtualenv_offline.sh + virtualenv_wheels/）。
  VENV_INSTALLER="${SCRIPT_DIR}/install_virtualenv_offline.sh"
  if "$PYTHON_BIN" -m virtualenv --version >/dev/null 2>&1; then
    ok "virtualenv 可用：$("$PYTHON_BIN" -m virtualenv --version 2>&1 | awk '{print $2}')"
  elif [ "$CHECK_ONLY" -eq 1 ]; then
    # --check-only 只驗證、不安裝；僅回報缺 virtualenv，實際部署時才會離線補齊。
    warn "$PYTHON_BIN 尚未安裝 virtualenv（--check-only 不進行安裝）。"
    warn "實際部署時將以 $VENV_INSTALLER 離線補齊。"
  else
    warn "$PYTHON_BIN 尚未安裝 virtualenv，將以隨附腳本離線安裝 ..."
    if [ ! -f "$VENV_INSTALLER" ]; then
      err "找不到離線安裝腳本：$VENV_INSTALLER"; exit 1
    fi
    # VENV_WHEELS_DIR 讓 profile 專屬的 virtualenv_wheels/ 生效（見 resolve_wheelhouse）；
    # 空字串時不設，維持安裝器自己找同層目錄的既有行為。
    if [ -n "$VENV_WHEELS" ]; then
      PYTHON_BIN="$PYTHON_BIN" VENV_WHEELS_DIR="$VENV_WHEELS" bash "$VENV_INSTALLER"
    else
      PYTHON_BIN="$PYTHON_BIN" bash "$VENV_INSTALLER"
    fi
    if ! "$PYTHON_BIN" -m virtualenv --version >/dev/null 2>&1; then
      err "virtualenv 離線安裝後，$PYTHON_BIN 仍無法使用（可能裝到了其他解譯器）。"
      err "請確認 $PYTHON_BIN 與 install_virtualenv_offline.sh 選用的解譯器一致。"
      exit 1
    fi
    ok "virtualenv 離線安裝完成並可用：$("$PYTHON_BIN" -m virtualenv --version 2>&1 | awk '{print $2}')"
  fi

  # --- 校驗 wheel 完整性 ------------------------------------------------------
  if [ "$SKIP_VERIFY" -eq 0 ] && [ -f "$MANIFEST" ]; then
    info "以 MANIFEST.txt 校驗 wheel sha256 ..."
    if ( cd "$WHEELHOUSE" && grep -E '^[0-9a-f]{64}  ' "$MANIFEST" | sha256sum -c --quiet ) 2>/dev/null; then
      ok "所有 wheel 檔案 sha256 校驗通過"
    else
      err "wheel 校驗失敗，檔案可能損毀或被竄改。可用 --skip-verify 強制略過。"; exit 1
    fi
  else
    warn "略過 wheel sha256 校驗"
  fi

  if [ "$CHECK_ONLY" -eq 1 ]; then
    ok "--check-only 完成：環境與 wheel 皆就緒，未執行安裝。"
    exit 0
  fi

  # --- 建立 / 沿用 venv ------------------------------------------------------
  VENV_PY="${VENV_DIR}/bin/python"
  if [ "$RECREATE" -eq 1 ] && [ -d "$VENV_DIR" ]; then
    warn "--recreate：移除既有 venv $VENV_DIR"
    rm -rf "$VENV_DIR"
  fi

  # 記住這個 venv 是不是本次才建立的:安裝失敗時只能收掉自己建的那一個(見下方 pip 失敗
  # 的處理)。沿用既有 venv 時它可能是上一次成功部署留下、正在跑服務的環境,不能碰。
  local venv_created=0
  if [ -x "$VENV_PY" ]; then
    ok "沿用既有 venv：$VENV_DIR"
  else
    info "建立專屬 venv（$PYTHON_BIN -m virtualenv，離線，含 pip）..."
    mkdir -p "$(dirname "$VENV_DIR")"
    "$PYTHON_BIN" -m virtualenv "$VENV_DIR"
    if [ ! -x "$VENV_PY" ]; then
      err "venv 建立失敗：找不到 $VENV_PY"; exit 1
    fi
    venv_created=1
    ok "venv 建立完成"
  fi
  info "venv pip 版本 : $("$VENV_PY" -m pip --version 2>/dev/null | awk '{print $2}')"

  # --- 執行離線安裝 ----------------------------------------------------------
  RUNTIME_PKGS=(paramiko bcrypt cryptography pynacl cffi pycparser invoke typing-extensions)
  TEST_PKGS=(pytest pytest-cov coverage pluggy iniconfig packaging pygments tomli exceptiongroup)
  # 只有舊平台才需要的標準庫 backport。dataclasses 是 3.7 才進標準庫,而
  # monitor/log_monitor.py、monitor/tui.py、run_selected_transfers.py、pack_upload.py
  # 都用 @dataclass —— Bionic 的 venv 是 3.6,少了它那四支人工工具一律
  # ModuleNotFoundError(在 Bionic 開發機 192.168.6.230 實測確認)。
  #
  # 與 TEST_PKGS 同樣走「wheelhouse 有才裝」而**不是**放進 RUNTIME_PKGS:Jammy 的
  # wheelhouse 刻意不放它(3.10 已內建,而 dataclasses==0.8 的 python_requires 是
  # >=3.6,<3.7,pip 在 3.10 上本來就會拒絕)。放進 RUNTIME_PKGS 會讓 preflight 在
  # Jammy 上把「正確地不存在」判成缺件。也刻意不受 --skip-tests 影響:那四支工具是
  # 給人用的,不屬於測試堆疊。
  BACKPORT_PKGS=(dataclasses)

  PKGS=("${RUNTIME_PKGS[@]}")
  local missing_backports=()
  for pkg in "${BACKPORT_PKGS[@]}"; do
    if wheelhouse_has "$pkg" "$WHEELHOUSE"; then
      PKGS+=("$pkg")
    else
      missing_backports+=("$pkg")
    fi
  done
  if [ "${#missing_backports[@]}" -gt 0 ]; then
    info "標準庫 backport: 本 profile 無 ${missing_backports[*]}（該版 Python 內建則屬正常）"
  else
    info "標準庫 backport: ${BACKPORT_PKGS[*]}（舊平台的 3.6 需要）"
  fi
  # 測試堆疊裝在**第二次** pip 呼叫,所以收在自己的陣列裡而不是併進 PKGS(理由見下方)。
  local test_pkgs=()
  if [ "$INSTALL_TESTS" -eq 1 ]; then
    # 執行期相依是**必須**的（preflight 的 wheel_compat.py 已經強制它們存在）；
    # 測試堆疊則按 wheelhouse 實際有什麼裝什麼。理由：同一份清單套到不同 Python 會有
    # 客觀上不存在的成員 —— 例如 exceptiongroup 的 backport 要 >=3.7，Bionic 的 py3.6
    # 沒有任何真版本（PyPI 上只有一個 0.0.0a0 佔位套件，還會把 trio 一串拖進來）。
    # 為此讓整個部署失敗是不對的：測試堆疊不是船上跑服務的必要條件。
    local skipped=()
    for pkg in "${TEST_PKGS[@]}"; do
      if wheelhouse_has "$pkg" "$WHEELHOUSE"; then
        test_pkgs+=("$pkg")
      else
        skipped+=("$pkg")
      fi
    done
    if [ "${#skipped[@]}" -gt 0 ]; then
      info "安裝範圍      : 執行期相依 + 測試堆疊（本 profile 缺 ${skipped[*]}，略過）"
      warn "此 profile 的 wheelhouse 沒有 ${skipped[*]}；health_check 的單元測試段會受限。"
    else
      info "安裝範圍      : 執行期相依 + 測試堆疊 (pytest；預設)"
    fi
  else
    info "安裝範圍      : 執行期相依 (paramiko 堆疊；--skip-tests)"
  fi

  # 為什麼拆成兩次 pip 呼叫,而不是把兩組名字併成一次:pip 的相依解析是全有全無的 ——
  # 任何一顆**間接**相依缺席,整批都不會裝,連 paramiko 都不會。而這兩組的份量完全不同:
  #
  #   執行期相依裝不起來 = 這條船沒有 OTA(唯一的程式碼下載路徑)  → 必須中止部署
  #   測試堆疊裝不起來   = 船上少了 pytest,health_check 少一段   → 不該中止部署
  #
  # WHA03 IPC-3 的首次部署就是被「併成一次」害的:Bionic 的 wheelhouse 少了
  # importlib-metadata(pytest 7.0.1 與 pluggy 1.0.0 在 python_version < "3.8" 的間接相依),
  #     ERROR: No matching distribution found for importlib-metadata>=0.12
  # 一行帶走整批,paramiko 一顆都沒裝到,而那之前 systemd / sudoers / tmux 都已經改完了。
  # 上面那道「wheelhouse 有才裝」的過濾看的是清單上的名字,看不見間接相依;真正擋得住
  # 這個形狀的是 tests/test_offline_deploy.py 的相依閉包測試(WheelhouseClosureTests),
  # 拆開安裝是第二道:就算閉包又破了,壞的也只會是測試堆疊。
  info "開始離線安裝到 venv（--no-index，不連外網）..."
  run_rc "$VENV_PY" -m pip install \
    --no-index \
    --find-links "$WHEELHOUSE" \
    --upgrade \
    "${PKGS[@]}"
  PIP_RC="$RC"
  if [ "$PIP_RC" -ne 0 ]; then
    # 這裡是全腳本最需要「說清楚」的失敗:階段 A 已經動過機器(systemd / sudoers / tmux),
    # 而這一步沒完成。原本只印一行 exit code 就結束,操作者看到的是「安裝突然跳出」。
    err "pip 安裝失敗（exit=$PIP_RC）—— 執行期相依沒有裝完,部署到此為止。"
    err "wheelhouse：$WHEELHOUSE"
    err "缺哪一顆寫在上面 pip 的最後幾行;那通常是**間接**相依(安裝清單上沒有它的名字)。"
    err "補齊該 profile 的 wheelhouse(連同 MANIFEST.txt)後重跑本腳本即可 ——"
    err "階段 A 已完成的設定會被沿用,不需要從頭來過。"
    if [ "$venv_created" -eq 1 ]; then
      rm -rf "$VENV_DIR"
      warn "已移除本次建立的半成品 venv：$VENV_DIR"
      warn "留著它比沒有更危險:script/run_sftp_self_update.sh 只檢查 bin/python 在不在,"
      warn "「venv 在、paramiko 不在」會通過那道守門,拖到 OTA 當下才 ImportError。"
    fi
    exit "$PIP_RC"
  fi
  ok "執行期相依安裝完成"

  if [ "${#test_pkgs[@]}" -gt 0 ]; then
    info "安裝測試堆疊（失敗不中止部署）..."
    run_rc "$VENV_PY" -m pip install \
      --no-index \
      --find-links "$WHEELHOUSE" \
      --upgrade \
      "${test_pkgs[@]}"
    if [ "$RC" -ne 0 ]; then
      warn "測試堆疊安裝失敗（exit=$RC）；執行期相依已就緒,部署繼續。"
      warn "後果只有一個:health_check 的單元測試段跑不了。補齊 wheelhouse 後重跑即可。"
    else
      ok "測試堆疊安裝完成"
    fi
  fi
  ok "套件安裝完成"

  # --- 安裝後驗證 ------------------------------------------------------------
  info "在 venv 內驗證關鍵套件可正常匯入 ..."
  # 以下到 PY 為止刻意不縮排:heredoc 的終止符不允許有前導空白,而中間是 Python 程式
  # —— 跟著函式縮排就是不同的程式(IndentationError)。請不要「順手對齊」它。
  "$VENV_PY" - <<'PY'
import importlib, sys
mods = ["paramiko", "cryptography", "nacl", "bcrypt", "cffi"]
fail = False
for m in mods:
    try:
        mod = importlib.import_module(m)
        v = getattr(mod, "__version__", "?")
        print(f"  [ OK ] {m:<14} {v}")
    except Exception as e:  # noqa
        print(f"  [FAIL] {m:<14} {e}")
        fail = True
sys.exit(1 if fail else 0)
PY
  ok "匯入驗證通過"

  echo "-----------------------------------------------------------"
  ok "離線部署完成！專屬 venv：$VENV_DIR"
}

# systemd 設定的互動（含 sudoers 密碼）必須在階段 A 完成，但 shipboard upload 的
# ExecStart 依賴階段 B 才建立的專屬 venv。venv 完成後無提示地再同步一次 timer：讓該 unit
# 在首次部署也以完整環境收尾，並由 install_timers 清除 bootstrap 期間可能留下的 failed latch。
stage_finalize_venv_dependent_units() {
  case "${SCHED_STATUS:-}" in
    已啟用|部分完成*) ;;
    *) return 0 ;;
  esac
  [ -f "${TIMERS_INSTALLER:-}" ] || return 0

  echo ""
  info "專屬 venv 已完成，重新同步依賴 venv 的 timer 狀態 ..."
  run_rc bash "$TIMERS_INSTALLER"
  if [ "$RC" -eq 0 ]; then
    ok "venv 相依 timer 已完成最終同步。"
    SCHED_STATUS="已啟用"
  else
    warn "venv 完成後重同步 timer 仍有項目失敗（exit=$RC）。"
    SCHED_STATUS="部分完成（venv 後重試 exit=$RC）"
  fi
}

# --- 執行完整啟動流程（決定已在前面收集，這裡只執行） ----------------------
# 刻意放在 venv 之後:update_booster 的 SFTP 下載要用 sftp_transfer 的 venv。
# 也刻意放在健康檢查**之前**:服務起來之後,那份巡檢才第一次真的有意義
#(否則 tmux 段永遠是「預期 session 不存在」,報告等於白給)。
# 這裡不再詢問任何事 —— 所有互動都集中在前面,這之後全程無人干預。
stage_launch_exec() {
  if [ "$LAUNCH_DECISION" = "run" ]; then
    echo ""
    echo "── 執行完整啟動流程（reboot_launcher.sh）──"
    run_rc bash "$LAUNCHER"
    LAUNCH_RC="$RC"
    if [ "$LAUNCH_RC" -eq 0 ]; then
      ok "啟動流程完成（所有項目成功）。"
      LAUNCH_STATUS="已完成"
    else
      # 啟動器對個別項目失敗是「記錄並繼續」,所以非 0 代表有項目失敗而非整體中止。
      warn "啟動流程有項目失敗（exit=$LAUNCH_RC）。詳見上方總結與"
      warn "  ${SHARE_DIR}/scheduler/logs/launcher.log"
      LAUNCH_STATUS="有項目失敗（exit=$LAUNCH_RC）"
    fi
  fi
}

# --- 部署後自動健康檢查 ----------------------------------------------------
stage_health_check() {
  HEALTH_RC=0
  if [ "$RUN_HEALTH" -eq 1 ]; then
    if [ -f "$SCRIPT_DIR/health_check.py" ]; then
      echo ""
      info "自動執行健康檢查（能力測試 + SFTP 連線 + 健康報告）..."
      echo "==========================================================="
      # 未安裝測試堆疊（--skip-tests）時，轉傳 --skip-tests 讓 health_check 直接略過
      # 單元測試那一項（記 INFO 而非 WARN）。預設有裝 pytest 時則實際跑測試。
      HEALTH_ARGS=()
      [ "$INSTALL_TESTS" -eq 0 ] && HEALTH_ARGS+=(--skip-tests)
      run_rc "$VENV_PY" "$SCRIPT_DIR/health_check.py" "${HEALTH_ARGS[@]}"
      HEALTH_RC="$RC"
      echo "==========================================================="
      if [ "$HEALTH_RC" -eq 0 ]; then
        ok "健康檢查結果：HEALTHY"
      else
        warn "健康檢查發現問題（exit=$HEALTH_RC），請檢視上方報告。"
      fi
    else
      warn "找不到 health_check.py，略過自動健康檢查。"
    fi
  else
    info "已指定 --no-health-check，略過能力健康檢查。"
  fi
}

# --- 隱性自動化存活巡檢 ----------------------------------------------------
# 放在部署流程最後，以實際 systemd 狀態驗證：user manager / linger / unit /
# timer / sudoers / heartbeat / tmux。使用 --compact --fail-on-warn，部署畫面
# 只顯示 WARN/FAIL 與總結；完整明細仍寫入 logs/automation_health_report_<時間>.md。
# wave 尚未提供時由巡檢器列為 SKIP，不影響整體健康。
stage_automation_check() {
  AUTOMATION_CHECKER="${SCRIPT_DIR}/automation_health_check.py"
  AUTOMATION_RC=0
  AUTOMATION_STATUS="未執行"
  if [ "$RUN_HEALTH" -eq 1 ]; then
    echo ""
    info "執行隱性自動化存活巡檢（compact）..."
    if [ -f "$AUTOMATION_CHECKER" ]; then
      run_rc "$PYTHON_BIN" "$AUTOMATION_CHECKER" --compact --fail-on-warn
      AUTOMATION_RC="$RC"
      case "$AUTOMATION_RC" in
        0)
          ok "自動化存活巡檢：HEALTHY"
          AUTOMATION_STATUS="HEALTHY"
          ;;
        1)
          warn "自動化存活巡檢：UNHEALTHY（有 FAIL；請查看上方與 Markdown 報告）"
          AUTOMATION_STATUS="UNHEALTHY（exit=1）"
          ;;
        2)
          warn "自動化存活巡檢：DEGRADED（有 WARN；請查看上方與 Markdown 報告）"
          AUTOMATION_STATUS="DEGRADED（exit=2）"
          ;;
        *)
          warn "自動化存活巡檢未預期結束（exit=$AUTOMATION_RC）"
          AUTOMATION_STATUS="執行異常（exit=$AUTOMATION_RC）"
          ;;
      esac
    else
      warn "找不到 $AUTOMATION_CHECKER，略過自動化存活巡檢。"
      AUTOMATION_RC=127
      AUTOMATION_STATUS="略過（找不到巡檢腳本）"
    fi
  else
    info "已指定 --no-health-check，略過自動化存活巡檢。"
    AUTOMATION_STATUS="略過（--no-health-check）"
  fi
}

print_verification() {  # 兩條收尾路徑都要印的驗證指令
  echo "    tmux ls                                     # 應列出本角色該有的 session"
  echo "    tail -f ${SHARE_DIR}/scheduler/logs/launcher.log       # 開機做了什麼"
  echo "    tail -f ${SHARE_DIR}/scheduler/failover/logs/heartbeat.log"
  echo "    ${PYTHON_BIN} ${AUTOMATION_CHECKER}"
}
print_docker_reboot_warning() {  # 剛加入 docker 群組時,重開機不是建議而是必要
  case "$DOCKER_GROUP_STATUS" in
    已加入*)
      echo ""
      warn "  本次剛把 $(id -un) 加進 docker 群組,而群組變更只對**新** session 生效。"
      warn "  所以這一輪請務必重開機 —— 不重開機的話 systemd user manager 仍是舊的群組,"
      warn "  web 平台(start_web_docker.sh)會因權限不足而起不來。"
      ;;
  esac
}

stage_summary() {
  echo ""
  echo "── 部署總結 ──"
  # 首次部署最容易搞錯的就是「這台裝成 IPC-1 還是 IPC-2」,而它決定了會啟動哪些服務。
  # 直接印出啟動器實際會採用的有效角色,一眼可見。
  #（DEPLOY_ROLE / DEPLOY_VSL_UPPER 在身分檔定案後就算好了,見上方「由身分檔推導」段。）
  printf "  本機有效角色    ：%s\n" "$DEPLOY_ROLE"
  printf "  開機自動執行設定：%s\n" "$AUTOSTART_STATUS"
  printf "  週期排程 timer   ：%s\n" "$SCHED_STATUS"
  printf "  常駐服務        ：%s\n" "$SERVICES_STATUS"
  printf "  clink_* 遷移    ：%s\n" "$MIGRATE_STATUS"
  printf "  docker 群組      ：%s\n" "$DOCKER_GROUP_STATUS"
  printf "  sudo 白名單      ：%s\n" "$SUDOERS_STATUS"
  printf "  資料碟掛載授權  ：%s\n" "$MOUNT_POLICY_STATUS"
  printf "  GDM 自動登入    ：%s\n" "$GDM_AUTOLOGIN_STATUS"
  printf "  tmux            ：%s\n" "$TMUX_STATUS"
  printf "  照片同步金鑰    ：%s\n" "$SSH_KEY_STATUS"
  [ "$RUN_HEALTH" -eq 1 ] && printf "  健康檢查        ：%s\n" \
    "$( [ "$HEALTH_RC" -eq 0 ] && echo HEALTHY || echo "有問題（exit=$HEALTH_RC）" )"
  printf "  完整啟動流程    ：%s\n" "$LAUNCH_STATUS"
  printf "  自動化存活巡檢  ：%s\n" "$AUTOMATION_STATUS"

  echo ""
  # 這一段的文案取決於啟動流程到底跑了沒。原本無條件印「⚠ 尚未完成:服務還沒有啟動」+
  # 「接下來請二選一」,那是 4647125 把啟動流程納進本腳本**之前**的事實 —— 之後就變成
  # 總結上一行剛寫「完整啟動流程:已完成」,下一段卻叫操作者去把服務啟動起來。
  case "$LAUNCH_STATUS" in
    已完成|有項目失敗*)
      if [ "$LAUNCH_STATUS" = "已完成" ]; then
        echo "── 部署完成:服務已啟動 ──"
        echo "  已依角色 $DEPLOY_ROLE 走完 update+env+run,本角色該有的服務應該都在跑了。"
      else
        echo "── ⚠ 部署完成,但啟動流程有項目失敗 ──"
        echo "  已依角色 $DEPLOY_ROLE 嘗試套用 update+env+run,有項目未成功。"
        echo "  啟動器對個別項目是「記錄並繼續」,所以其餘服務仍可能正常運作 —— 先看是哪一項:"
        echo "    tail -50 ${SHARE_DIR}/scheduler/logs/launcher.log"
      fi
      echo ""
      echo "  請驗證:"
      print_verification
      echo ""
      echo "  建議仍在方便時重開機一次,完整走過真實開機路徑(nssms-boot → reboot_launcher):"
      echo "    sudo reboot"
      print_docker_reboot_warning
      ;;
    *)
      echo "── ⚠ 尚未完成:服務還沒有啟動 ──"
      # 沒跑啟動流程時,本腳本就只做了「一次性人工設定」:身分、systemd 骨架、sudoers、
      # sftp_transfer 的 venv。各專案的程式碼、環境安裝與服務啟動,全部由第一次開機的
      #   nssms-boot → reboot_launcher.sh → update_booster.sh(OTA)→ 依角色全相位套用
      # 完成。不講清楚的話,操作者看到上面一排「已啟用」會以為部署完成了。
      echo "  本次只做了一次性設定(身分 / systemd / sudoers / sftp_transfer venv)。"
      echo "  各專案的程式碼、環境與服務由第一次開機流程完成:"
      echo "    nssms-boot → reboot_launcher.sh → update_booster.sh(SFTP 拉最新程式碼)"
      echo "                                    → 依角色 $DEPLOY_ROLE 套用 update+env+run"
      echo ""
      echo "  所以接下來請二選一:"
      echo "    sudo reboot                                # 建議:完整走一次真實開機流程"
      echo "    systemctl --user start nssms-boot          # 或立即手動觸發一次(不重開機)"
      print_docker_reboot_warning
      echo ""
      echo "  想先確認會做什麼(不執行任何動作):"
      echo "    bash ${SHARE_DIR}/scheduler/reboot_launcher.sh --dry-run"
      echo ""
      echo "  啟動後的驗證:"
      print_verification
      ;;
  esac
  if [ "${DEPLOY_VSL_UPPER:-}" = "CLINK" ]; then
    echo ""
    warn "  本機 vsl_name=CLINK(開發機):update_booster 會刻意略過整個 OTA,"
    warn "  所以程式碼**不會**自動下載,需人工放置或 rsync。"
  fi
  echo ""
  echo "啟用 venv："
  echo "  source \"$VENV_DIR/bin/activate\""
  echo ""
  echo "以此 venv 執行工具（不啟用也可以直接用絕對路徑）："
  echo "  \"$VENV_PY\" \"$PROJECT_DIR/main.py\" --cli"
  echo ""
  echo "如需單獨再跑一次健康檢查："
  echo "  \"$VENV_PY\" \"$SCRIPT_DIR/health_check.py\""
  echo ""
  echo "如需單獨再跑一次自動化存活巡檢："
  echo "  \"$PYTHON_BIN\" \"$AUTOMATION_CHECKER\""
  # 印在總結最後：船上要回報問題時，這是唯一需要寄回岸上的檔案（兩份 Markdown 報告
  # 只有結果，這一份有過程）。
  if [ -n "$TRANSCRIPT" ]; then
    echo ""
    echo "本次部署的完整終端記錄（含以上全部輸出）："
    echo "  $TRANSCRIPT"
  fi
  echo "==========================================================="
}

# --- 主流程 ----------------------------------------------------------------
# 這個函式就是檔頭那份 A/B/C 大綱本身。原本它是一支 1100 行的直線腳本，流程只存在於
# 檔頭的註解裡 —— 而註解會漂移（A4~A7 的編號就漂過一次），main() 不會跟自己漂移。
#
# 定義順序刻意等於呼叫順序：scheduler/tests/test_first_deploy.sh 用「某句註解的行號
# 先後」來守幾條順序不變式（venv → 啟動 → 巡檢、遷移 → install_services），那些斷言
# 看的是文字位置，所以搬動段落時文字順序必須跟著執行順序。
main() {
  parse_args "$@"
  start_transcript                   # 刻意在 parse_args 之後：--help 與參數錯誤不留檔
  banner_and_preflight

  # ---- 階段 A：一次性人工設定（所有需要輸入的東西都在這一段）----
  announce_check_only
  stage_vessel_info                  # A1 身分檔（含殘留接管旗標的判讀）
  compute_identity                   # 由身分檔推導 DEPLOY_ROLE / DEPLOY_VSL_UPPER
  stage_legacy_failover_state        #    舊格式接管狀態檔
  stage_autostart                    # A2 nssms-boot.service + linger
  stage_clink_migration              # A3 舊 clink_* —— **必須早於 A7，否則撞 port**
  stage_docker_group                 # A4 docker 群組（web 平台開機自啟的前提）
  stage_unattended_boot              # A4b 無人值守開機的兩個前提（掛載授權 / 自動登入）
  stage_scheduler_units              # A5/A6/A7 timer + sudoers + 常駐服務
  stage_tmux                         # A8 tmux 離線補齊 —— **必須早於 A10**（見該函式）
  stage_ssh_key                      # A9 照片同步的 SSH 金鑰（僅實體 IPC-2）
  stage_launch_decision              # A10 只收集決定，執行在階段 C

  echo ""
  info "以下不再需要任何輸入,可以離開終端機。"
  # 這行宣告從此變成可執行的約束:之後任何提示都會讓 ask_yn 當場中止（見它的註解）。
  # 原本這條不變式只靠 scheduler/tests/test_first_deploy.sh 比對「檔案裡最後一個讀取提示
  # 的行號」來守,那是文字層面的近似;提示全部改走 ask_yn 之後行號已經守不住,改由執行期把關。
  NO_MORE_INPUT=1

  # ---- 階段 B：sftp_transfer 專屬 venv（離線、無人干預）----
  stage_wheelhouse_and_venv
  stage_finalize_venv_dependent_units

  # ---- 階段 C：完整啟動流程與驗證（無人干預）----
  stage_launch_exec                  # 刻意在 venv 之後（SFTP 下載要用它）
  stage_health_check                 # 也刻意在啟動之後（否則巡檢的 tmux 段沒有意義）
  stage_automation_check
  stage_summary

  # 部署本身成功即回傳 0；健康檢查結果另以訊息呈現，不影響部署離開碼。
  exit 0
}

main "$@"
