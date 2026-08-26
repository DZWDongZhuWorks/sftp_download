# log_monitor — SFTP 傳輸 Log 監視分析工具

解析 `sftp_transfer` 車隊各設備產生的 CSV 傳輸 log，彙整成**每裝置最新更新狀態**，
輸出**終端機彩色分群列表**與**自包含 HTML 報告**，供全覽與排查各設備的更新狀況。

- 獨立、**唯讀**、僅依賴 Python 標準函式庫（分析本身不需 `paramiko`）。
- 位於 `monitor/`（與 `deploy/` 同層），可 `python monitor/log_monitor.py` 直接執行，
  亦可 `from monitor.log_monitor import ...` 匯入。

---

## 運作概念

每次下載/上傳，`sftp_transfer` 本體會產生一份 CSV log（`utf-8-sig`），內含
`timestamp,device_name,version_info,level,message`，並在 `upload_log=True` 時上傳到遠端
`log_remote_dir`（基底 `/fleet/wanhai_nssms_deploy/sftp_logs/`）。

本工具**只分析本地目錄**：先由 `sftp_transfer` 本體把遠端 `sftp_logs` 下載到本地
（或直接分析本機自產的 `logs/`），再由本工具遞迴掃描、解析、分群呈現。

裝置身分取自 `device_name`，慣例為 `{vsl_name}_{ipc}_{project}`（如 `CLINK_IPC-1_ecdis`）；
無法解析者（靜態名如 `RADAR_UPLOADER`、舊命名）歸入「（未分類）」桶。

### 狀態判定（每份 log）

| 狀態 | 條件 |
|------|------|
| `success` 正常 | 有「任務結束」行且失敗數為 0 |
| `partial` 部分失敗 | 有「任務結束」行且失敗數 > 0 |
| `aborted` 中止 | 出現「任務中止：…」 |
| `incomplete` 未完成 | 無結束行（中斷/仍在執行） |
| `stale` 過期 | 最新一次為 `success`，但距今超過 `--stale-hours`（預設 72h） |

過期只套用在原本正常的裝置；`partial/aborted/incomplete` 一律以異常優先呈現。

---

## 呈現：階層分群

層級為 **方向（下載/上傳）＞ vessel ＞ IPC ＞ project**，可折疊。
**預設：正常群組收合、異常群組（含過期/失敗/中止）自動展開**，方便上百艘船時一眼定位問題。

- **CLI**：縮排階層 + 每層摘要徽章（`裝置 N｜正常 x｜過期 y｜異常 z`）。
  健康群組只印摘要行；問題群組展開列出各 project。
- **HTML**：原生 `<details>` 巢狀折疊 + 控制列（搜尋 / 方向 / 船 / IPC / 元件 / 狀態下拉、
  「只看異常」、「全部展開 / 全部收合」）。過濾即時重算各群「（符合 N）」並隱藏空群組。
  支援淺/深色，點列可展開該裝置的錯誤/警告與近期歷史。

---

### 報告是「靜態快照」

所有計算（讀 CSV、判狀態、過期、分群、每群計數）都在 **Python 產生報告的當下**完成，
結果直接寫死進 HTML。HTML 內嵌的 JS **只做畫面互動**（搜尋 / 過濾 / 折疊 / 重算「符合 N」），
**不會讀取 `fleet_logs`、也不會重算狀態**——瀏覽器開的是某一時刻的凍結快照。
要看新資料就重跑一次工具重新產生（`--watch` 會自動每 N 秒重產並覆寫同一份 `log_monitor.html`）。

> 註：來源 CSV 會隨每次傳輸持續累積（本體是「一次執行一檔」），但監視器依
> `(device_name, mode)` **只取最新一筆**呈現，畫面每台裝置仍只有一列。

---

## 使用方式

### A) 只分析（下載已由本體/排程完成）

```bash
python monitor/log_monitor.py --log-dir logs --html
```

### TUI 互動式終端機介面（`--tui`）

想要「接近 HTML 的控制能力」但留在終端機時，加上 `--tui`（stdlib `curses`，零新依賴）：

```bash
python monitor/log_monitor.py --log-dir fleet_logs --tui
# 搭配即時監視（每 60 秒重抓遠端 log 再刷新，保留展開/選取狀態）
python monitor/log_monitor.py --sync-config config/log_monitor_sync.json \
    --log-dir fleet_logs --tui --watch 60
# 邊看 TUI 邊產出 HTML 報告（首輪與每次重載都覆寫同一份）
python monitor/log_monitor.py --log-dir fleet_logs --tui --html
```

同步期間由 TUI 安全顯示 `main.py` 最近 20 行輸出；完整紀錄仍寫入原本的 CSV log。

`--tui` 可以與 `--html` 併用：**首輪分析結束後寫一次，之後每次重載（`r` 手動、`--watch`
自動）都跟著覆寫同一份**，等同 `--watch --html` 的儀表板行為。寫出的檔名會顯示在畫面
第二行（`HTML→log_monitor.html`），寫入失敗只會顯示 `HTML 失敗：<例外>`，不會中斷 TUI。
報告內容是**完整快照**（只套 CLI 參數的過濾）；TUI 內的 `/`、`m`、`s`、`p` 純屬畫面過濾，
不影響報告。

**非互動式終端機（cron／管線／`| grep`）會自動退回靜態分群輸出**，不會卡住或崩潰。

滑鼠事件由 curses 接收；SSH 裡使用 tmux 時需啟用 `set -g mouse on`。若 curses 或終端機
沒有提供滑鼠事件，TUI 會安靜保留完整鍵盤操作，不影響啟動。

操作：

| 輸入 | 動作 |
|------|------|
| 左鍵單擊／雙擊 | 單擊選取；雙擊開合群組或開裝置明細；群組列的 `▶`/`▼` 可單擊開合 |
| 滾輪 | 主畫面每次移動三列；明細與 CSV 內容捲動；CSV 中 `Shift`+滾輪水平捲動 |
| 明細／CSV 滑鼠操作 | 明細內左鍵進 CSV，右鍵或點彈窗外返回；CSV 右鍵或點底列返回 |
| `↑`/`↓` 或 `k`/`j`、`PgUp`/`PgDn`、`Home`/`End` | 移動選取 |
| `Enter` / `Space` | 群組開合；在裝置列開明細（明細再按 `Enter` 看該筆 CSV 原始資料） |
| `→`/`l`、`←`/`h` | 展開 / 收合（裝置列 `←` 跳回父群） |
| `E` / `C` | 全部展開 / 全部收合（僅分群模式看得到效果） |
| `f` | 切換 **平坦／分群** 檢視（平坦＝全船隊一張表） |
| `o` / `O` | 循環排序欄位（船隻名稱／更新時間／嚴重度／裝置名稱／版本）／切換升降冪 |
| `/` | 搜尋（`Esc` 清除） |
| `m` / `s` / `p` | 循環方向 / 循環狀態 / 只看異常 |
| `v` | 顯示 / 隱藏**版本欄**（預設顯示；窄終端機塞不下時關掉） |
| `r` | 立即重載 | 
| `?` / `q` | 說明 / 離開 |

目前的檢視模式與排序狀態顯示在畫面第二行（`檢視: 平坦｜排序：更新時間↑`）。

#### 平坦模式（`f`）與排序（`o`／`O`）

分群視圖適合「定位是哪艘船的哪台 IPC 出問題」，但跳不出群組的框——每個 IPC 底下只有
幾個元件，在裡面排序意義不大。**平坦模式**忽略 方向/船/IPC 分群，把全船隊裝置排成單一
表格，欄位為 `向｜船｜IPC｜元件｜版本｜最後執行｜檔案｜成/略/失｜距今｜摘要`。
方向欄是必要的——同一台裝置的上傳與下載是兩筆紀錄，少了它兩列會長得一樣。

#### 版本欄（`v`）

「這艘船跑的是哪一版」以前只能點進單筆 log 的 CSV 檢視才看得到。現在主清單（分群與平坦
模式）與 HTML 報告都有一欄**版本**，內容是該次傳輸當下的程式碼版本，例如 `0.4.1+20b8056`
（宣告版號 + git commit；發布端未提交修改時會有 `-dirty` 後綴）。

**沒有版本是正常狀態，不是錯誤**：版本要專案在自己的根目錄放 `VERSION.json` 才會有
（見 sftp_transfer 的 README〈版本標記〉），而且舊 log 一律沒有。這種情況顯示 `—`，
排序時排到最後——升冪時想看的是「誰是哪一版」，一整排破折號排在最前面只會擋路。

版本排序是拿來**把同版本的船聚在一起**的，不是比新舊：它是字串比較，所以 `0.10.0` 會排在
`0.9.0` 前面。要找「誰還沒更新」，先用版本排序聚在一起，再看哪一群的版號是舊的。

分群模式的版本欄寬度是跟摘要欄借的（整列總寬不變）；平坦模式則是整整多一欄，80 欄的終端機
會擠掉摘要——那時候用 `v` 關掉即可。

| 欄位 | 升冪 `↑` | 降冪 `↓` |
|------|----------|----------|
| 嚴重度 | 正常在前 | **最嚴重在前** |
| 更新時間 | **最久未更新在前**（找失聯裝置最快的一招） | 最近更新在前 |
| 船隻名稱 | A→Z（不分大小寫） | Z→A |
| 裝置名稱 | A→Z（不分大小寫） | Z→A |
| 版本 | A→Z，**沒有版本的排最後** | Z→A，沒有版本的排最前 |

- 預設 `船隻名稱↑`，使用 `o` 會依序切換為「更新時間」、「嚴重度」、「裝置名稱」、「版本」，再繞回船隻名稱。
- 排序是**穩定**的：同鍵值維持資料層給的 船/IPC/元件 次序。
- 未回報過（無時間戳）的裝置在「更新時間」視為最舊。
- 排序也套用在分群模式，所以 `o`／`O` 兩邊都有用。分群模式下排序作用在**該欄位有意義的
  那一層**：「船隻名稱」重排的是**船群**（同一 IPC 底下的裝置船名都一樣，套在裝置列上必然
  沒有效果），其餘欄位重排 IPC 底下的裝置列。
- 船群以外的群組順序（方向、IPC）仍由資料層決定（未分類置底，再依群組最嚴重狀態、名稱），
  不受 `o` 影響。
- 選取是以**裝置**為單位（key 兩種模式共用），切換檢視或重新排序後游標會跟著同一台
  裝置跑，不會跳回第一列。
- 排序純屬 TUI 呈現，**不影響** CLI 與 HTML 報告的輸出。

#### 三層鑽取：列表 → 明細 → CSV 原始資料

`Enter` 逐層往下、`Esc`/`q` 逐層往上。明細彈窗維持原本的精簡摘要（狀態、成功/略過/失敗、
最近數條 ERROR/WARNING、來源檔）；在明細再按 `Enter`，會全畫面列出**該筆 log CSV 的每一行**
（含 `parse_log_file` 平時丟掉的 INFO 列），`ERROR` 紅、`WARNING` 黃。

同一份 log 的 `device_name`/`version_info` 每列都相同，故放標題列，格線只留 `時間｜級別｜訊息`。
**時間與級別為釘住欄不隨水平捲動移動**，只有訊息欄會捲——往右讀長訊息時仍看得到時間戳。

| 鍵 | 動作 |
|----|------|
| `↑`/`↓` 或 `k`/`j`、`PgUp`/`PgDn` | 垂直捲動 / 翻頁 |
| `←`/`h`、`→`/`l` | 水平捲動訊息欄（看完整的長訊息，一次 8 欄） |
| `g` / `G` | 跳到首 / 末筆 |
| `0` / `Home` | 水平位移復位 |
| `s` / `S` | 循環排序欄位 / 切換升降冪（目前排序顯示在標題列） |
| `q` / `Esc` | 返回明細彈窗（可再按 `Enter` 重進） |

排序欄位循環為 `原序 → 級別 → 訊息`：

- **原序**：檔案順序；降冪即反轉，等於「檔尾最新在前」。
- **級別**：依**嚴重度**而非字母序（`CRITICAL > ERROR > WARNING > INFO > DEBUG`），
  降冪把 ERROR 推到最上面，方便一眼抓錯。
- **訊息**：把重複樣式（如大量「略過（已完整下載）」）聚成一團看出規律。

排序是**穩定**的：同鍵值維持原始時間順序，log 的脈絡不會被打散。
不提供「依時間排序」——這份 CSV 由單執行緒 logger 寫出，timestamp 天生單調遞增，
依時間排的結果就等於原序，多一個狀態沒有意義。

### B) 一鍵即時監視（工具自行觸發本體下載遠端 sftp_logs 再分析）

需先備一份 download 設定檔（`remote_path` 指向遠端 `sftp_logs`、`local_path` 等於 `--log-dir`）。
本倉庫已提供 `config/log_monitor_sync.json`（`local_path=fleet_logs`）。

```bash
python monitor/log_monitor.py \
    --sync-config config/log_monitor_sync.json \
    --log-dir fleet_logs --watch 60 --html
```

每 60 秒重新下載遠端 log 再刷新彩色列表。`--watch` 若不搭配 `--sync-config`，僅重讀本地目錄。

---

## 參數

| 參數 | 說明 |
|------|------|
| `--log-dir PATH` | 要分析的本地 log 目錄（預設 `logs`）。遞迴掃描 `*.csv`，涵蓋巢狀結構。 |
| `--mode {download,upload,all}` | 只看某方向（預設 `all`）。 |
| `--stale-hours N` | 逾期告警門檻（小時，預設 72）。 |
| `--html [PATH]` | 另存 HTML 報告；不接路徑則**覆寫** `<log-dir>/log_monitor.html`（固定檔名，`--watch`／`--tui` 每次刷新皆原地覆寫、不堆檔）。需保留歷史快照時改接明確路徑。 |
| `--sync-config PATH` | 分析前先用此 download 設定檔觸發本體下載遠端 log。 |
| `--watch SECONDS` | 每 N 秒清畫面刷新（搭配 `--sync-config` 才會重新下載）。 |
| `--vessel NAME` | 只顯示指定船名。 |
| `--ipc NAME` | 只顯示指定 IPC。 |
| `--component NAME` | 只顯示指定元件（project）。 |
| `--status {ok,stale,problem,all}` | 只顯示某狀態（`problem`=失敗/中止/未完成）。 |
| `--flat` | 改用舊的平面表格（不分群，方便 grep）；搭配 `--tui` 時 TUI 直接以平坦模式啟動。 |
| `--tui` | 互動式終端機介面（curses，見上節）；非 TTY 自動退回靜態輸出。 |
| `--expand-all` / `--collapsed` | 分群時強制全展開 / 全收合（覆寫預設折疊）。 |
| `--no-color` | 停用 ANSI 顏色。 |

離開碼：全部正常 → `0`；有任何非 `success`（含過期）→ `1`。

---

## 測試

```bash
pytest -q tests/test_log_monitor.py
```

測試沿用 `tests/conftest.py` 的 `tmp_path` 風格、於暫存目錄寫入含 BOM 的合成 CSV，
不需網路；`sync_logs` 以 mock `subprocess.run` 驗證。

---

## 檔案

| 檔案 | 說明 |
|------|------|
| `monitor/log_monitor.py` | 主程式（解析、分群、CLI/HTML 呈現、選配觸發下載、`--tui` 進入點）。 |
| `monitor/tui.py` | curses 互動式 TUI（`--tui`）；資料層沿用 `log_monitor`。 |
| `monitor/__init__.py` | 使 `import monitor.log_monitor` 於測試中可用。 |
| `config/log_monitor_sync.json` | 抓取遠端 `sftp_logs` 的 download 設定檔（依慣例不納入版控）。 |
| `tests/test_log_monitor.py` | 解析/分群/呈現的單元測試。 |
| `tests/test_tui.py` | TUI 純邏輯測試（flatten/reducer/按鍵映射）。 |
