# Mac mini 部署與交接

正式流程沿用現有 Microsoft Graph 信箱、Lark Base 與模型。登入 Mac 帳號後，抓信和 AI 處理分別在每小時 `:03` 和 `:30` 由 `launchd` 執行。排程採用 Mac 的本地時區。重新開機並登入後，由下一個排程時點接續。

## 目前部署狀態

2026 年 11 月 5 日，兩個 LaunchAgent 均已啟用。工作流 2 的標籤為 `local.inbox-onboarding.process`，透過部署目錄的 Python 虛擬環境執行 `onboarding_source/process_mail.py`，使用部署目錄 `.env` 內的 `deepseek-flash` 設定。工作流 2 的標準輸出和錯誤日誌分別位於 `outputs/lark/launchd/process.stdout.log` 和 `outputs/lark/launchd/process.stderr.log`，每輪報告位於 `outputs/lark/runs/`。

同日 10:09（香港時間）以新版設定處理 55 封待辦，失敗 0 封。10:43 啟用工作流 2 後，透過 LaunchAgent 立即觸發一次，退出碼為 0，當輪待辦為 0。抓信的部署及交接紀錄見 [工作流 1 部署紀錄](workflow-1-deployment.md)。

## 安裝

從 Git 專案根目錄執行。需要 Python 3.11。部署目錄位於使用者的 Library，供背景工作從固定路徑啟動。

```sh
deploy_dir="$HOME/Library/Application Support/inbox-onboarding"
umask 077
install -d -m 700 "$deploy_dir"
rsync -r --exclude '__pycache__' --exclude '.DS_Store' --exclude '_outputs' \
  onboarding_source mail_export_source "$deploy_dir/"
if [ ! -e "$deploy_dir/.env" ]; then
  install -m 600 .env "$deploy_dir/.env"
fi
chmod 600 "$deploy_dir/.env"
python3 -m venv "$deploy_dir/.venv"
"$deploy_dir/.venv/bin/python" -m pip install -r "$deploy_dir/onboarding_source/requirements.txt"
cd "$deploy_dir"
```

部署使用正式的 `.env`，設定名稱見 `onboarding_source/.env.example`。沿用原 Base token 和兩張表的 ID。更新既有部署時，先停用排程，再更新程式和依賴。

## 授權與切換

```sh
.venv/bin/python -B onboarding_source/setup_lark.py --verify-app
.venv/bin/python -B onboarding_source/setup_lark.py --authorize
```

用原本有權編輯正式 Base 的 Lark 使用者開啟授權連結。也可掃描部署目錄內 `outputs/lark/auth/user_authorization.png` 的 QR code。完成後執行以下命令。若 macOS 提示鑰匙圈存取，允許本部署使用的 Python 讀取此專案的項目，再繼續驗證。

```sh
.venv/bin/python -B onboarding_source/setup_lark.py --finish-authorization
.venv/bin/python -B onboarding_source/macos_jobs.py --preflight
```

OAuth 令牌保存在目前帳號的登入鑰匙圈。預檢會唯讀檢查 Lark 表欄位和 Graph 信箱，並送一封合成郵件給模型。

確認 Windows 排程和執行中的工作均已停止。若有該信箱與目標表最後的 Graph 檢查點，保留其原檔名並複製至部署目錄的 `.runtime/graph/`。同一組信箱與目標表設定會找到同一個檢查點。若找不到，首次抓取過去 24 小時；超出該時間窗的停機期間郵件需要另行補抓。

先手動跑一輪正式流程，核對抓信摘要、檢查點和 `outputs/lark/runs/` 下的 AI 報告與 Lark 寫回結果。抓信以 `internetMessageId` 去重。若沒有待辦，AI 報告應顯示零筆處理。

```sh
.venv/bin/python -B onboarding_source/fetch_mail.py
.venv/bin/python -B onboarding_source/process_mail.py
```

## 啟用排程

手動驗證成功後執行。若原先已安裝排程，先依下一節停用。

```sh
agent_dir="$HOME/Library/LaunchAgents"
domain="gui/$(id -u)"
install -d "$agent_dir"
.venv/bin/python -B onboarding_source/macos_jobs.py --render-plists .runtime/launchd
for job in fetch process; do
  name="local.inbox-onboarding.$job.plist"
  /usr/bin/plutil -lint ".runtime/launchd/$name" || break
  install -m 600 ".runtime/launchd/$name" "$agent_dir/$name" || break
  /bin/launchctl enable "$domain/local.inbox-onboarding.$job" || break
  /bin/launchctl bootstrap "$domain" "$agent_dir/$name" || break
done
```

首次安裝後，核對兩個排程各觸發一次且退出碼為零。日誌位於 `outputs/lark/launchd/`。抓信進度保存在 `.runtime/graph/`，AI 詳細報告保存在 `outputs/lark/runs/`。

模型連線失敗時，先查看該輪的 `run_report.json` 和 `results.jsonl`。流程會在失敗郵件停止，下次從最早未完成的郵件接續。更換模型服務時，在部署目錄的 `.env` 更新 `LLM_API_HOST`、`LLM_MODEL` 和該服務對應的 `LLM_API_KEY`。先用 `macos_jobs.py --preflight` 驗證合成郵件，再重跑 AI。

已判為開戶郵件但三項客戶身分皆空，或命中多筆進度時，程式將郵件標為「人工核查」，留下原因並繼續本輪。命中多筆時，處理說明也會列出各進度記錄 ID。後續排程會跳過該郵件，報告的 `manual_review` 記錄本輪轉交人工的數量。

本機使用指定的 Qwen 網址與 `qwen3.8-max`。若模型在回覆前斷線，可在該部署的 `.env` 設定 `LLM_ENABLE_THINKING=false`，並用合成郵件預檢與正式待辦重試確認。[Qwen API 參數](https://docs.qwencloud.com/api-reference/chat/openai-chat)

## 查看、重跑及停用

```sh
domain="gui/$(id -u)"
/bin/launchctl print "$domain/local.inbox-onboarding.fetch"
/bin/launchctl print "$domain/local.inbox-onboarding.process"
```

排程已載入時，需要提前執行就用 `launchctl kickstart "$domain/local.inbox-onboarding.fetch"` 或 `launchctl kickstart "$domain/local.inbox-onboarding.process"`，避免另開同一工作的手動程序。

要停用，等目前工作退出，再移除兩個已載入的工作和設定檔，避免下次登入時自動載入。保留部署目錄內的憑據、檢查點和日誌。

```sh
for job in fetch process; do
  /bin/launchctl bootout "$domain/local.inbox-onboarding.$job"
  rm -f "$HOME/Library/LaunchAgents/local.inbox-onboarding.$job.plist"
done
```

若要交還 Windows，先確認兩個 Mac 工作都已退出，再把 Mac 最後的 Graph 檢查點複製回 Windows，並確認原本的 Lark 使用者授權仍有效。
