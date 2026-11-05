# 工作流 1 部署紀錄

2026 年 11 月 4 日 18:14（香港時間）核對時，Mac mini 已啟用工作流 1。程式從既有 Microsoft Graph 信箱抓取郵件，將新郵件寫入正式 Lark Base 的「邮件信息表」。當時僅啟用抓信，兩個工作流的最新啟用狀態見 [Mac mini 部署與交接](macos-deployment.md#目前部署狀態)。

## 排程與執行位置

| 項目 | 目前設定 |
| --- | --- |
| 執行方式 | macOS 使用者層級的 `launchd` LaunchAgent |
| 執行時間 | 每小時 `:03`，採用 Mac 的本地時區（香港時間） |
| LaunchAgent | `local.inbox-onboarding.fetch` |
| 排程檔 | `~/Library/LaunchAgents/local.inbox-onboarding.fetch.plist` |
| 部署目錄 | `~/Library/Application Support/inbox-onboarding` |
| 入口 | 部署目錄下的 `.venv/bin/python -B onboarding_source/fetch_mail.py` |
| 本機設定 | 部署目錄下的 `.env`，僅目前帳號可讀寫。Lark 使用者 OAuth 存在登入鑰匙圈。 |

排程在該 macOS 帳號登入後生效。兩輪之間，`launchctl print` 顯示 `state = not running` 屬正常。

## 驗證結果

| 香港時間 | 觸發方式 | 抓取 | 新增至 Lark | 略過重複 | 退出碼 |
| --- | --- | ---: | ---: | ---: | ---: |
| 2026-11-04 18:03 | 排程自動觸發 | 4 | 4 | 0 | 0 |
| 2026-11-04 18:10 | 恢復排程後，透過 `launchd` 手動觸發 | 4 | 0 | 4 | 0 |

18:10 的驗證沿用原有檢查點。四封郵件已在 Lark，程式透過 `internetMessageId` 略過重複，並將檢查點推進到本輪抓信的截止時間。

## 查看狀態與日誌

```sh
job="gui/$(id -u)/local.inbox-onboarding.fetch"
deploy_dir="$HOME/Library/Application Support/inbox-onboarding"

/bin/launchctl print "$job"
tail -n 30 "$deploy_dir/outputs/lark/launchd/fetch.stdout.log"
tail -n 30 "$deploy_dir/outputs/lark/launchd/fetch.stderr.log"
```

查看最近一輪的 `last exit code`，以及日誌中的 `fetched`、`imported` 和 `skipped_existing_or_duplicate`。抓信檢查點位於部署目錄下的 `.runtime/graph/`，同一信箱與目標表會沿用同一檔案。只有本輪讀取和寫入全部成功後，才會更新上次成功的截止時間。

## 手動觸發、暫停與恢復

在上面的終端機工作階段，等本輪退出後，可手動觸發一次。

```sh
/bin/launchctl kickstart "$job"
```

暫停時，等本輪完成，再停用並卸載 LaunchAgent。排程檔、日誌和檢查點會保留。

```sh
/bin/launchctl disable "$job"
/bin/launchctl bootout "$job"
```

恢復時，在同一 macOS 帳號登入後執行下列命令。下一輪會沿用保留的檢查點。

```sh
/bin/launchctl enable "$job"
/bin/launchctl bootstrap "gui/$(id -u)" \
  "$HOME/Library/LaunchAgents/local.inbox-onboarding.fetch.plist"
```

如需重裝 Python 環境或重新授權 Lark，請參閱 [Mac mini 部署與交接](macos-deployment.md)。
