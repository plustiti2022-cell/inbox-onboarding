# DeepSeek 多維表格 Agent（最小版）

## Lark 聊天修改與回覆

沿用現有應用、Base 與使用者 OAuth，直接在「邮件多维表格」機器人的單聊發送修改指令：

```sh
.venv/bin/python -m bitable_agent.bot \
  --auth-project "$HOME/Library/Application Support/inbox-onboarding"
```

例如發送「將 ABC公司的简要描述改成等待合规审核」，寫入獲 Lark 確認後回覆「已更新 ABC公司：简要描述 → 等待合规审核」，並附上記錄 ID。客戶不唯一、找不到或沒有修改內容時會回覆「未更新」；寫入未獲確認時不聲稱成功。

此聊天入口只接受現有 OAuth 授權人的單聊文字指令。使用 `update_prompt.txt` 要求 AI 回傳六個中文欄位及 `is_onboarding`；以公司名、姓名、郵箱匹配唯一記錄，只有非 null 的新資訊會寫入，相同身份不重寫。「更新客戶狀態」這類自然語言會同步提取球權、卡點和簡要描述；若明確要求只修改某列，才保留其他進度欄位。卡點的空字串表示明確清空。寫入時另附當前北京時間和 `internetMessageId=人工修改`，經辦人保留。

程式使用官方 SDK 的 WebSocket 長連線。既有機器人及發訊息權限可沿用；Developer Console 的「事件與回調 → 事件配置」須選「使用長連線接收事件」，添加應用身份事件 `im.message.receive_v1`。接收單聊需要應用身份權限 `im:message.p2p_msg:readonly`（舊版 `im:message.p2p_msg` 亦可），回覆需要 `im:message:send_as_bot` 或 `im:message`。只補缺少項目並發布版本，無需新增公網回調網址、加密策略或卡片回調。

檢查既有機器人權限（不發訊息、不改表）：

```sh
.venv/bin/python -m bitable_agent.bot --check
```

權限清單預檢只作提示，正式啟動仍會建立長連線，由 Lark 實際校驗事件與 API 權限。終端輸出帶北京時間的 `connected`、`message_received`、`ai_result`、`update_prepared`、`update_confirmed`、`processing_finished`、`reply_sent` 等 JSON 日誌；AI 結果和預備寫入欄位均附來源訊息 ID，忽略或去重的訊息亦會記錄原因，不記錄原始指令正文。

開啟連線供 Developer Console 驗證，期間只連線、不處理訊息：

```sh
.venv/bin/python -m bitable_agent.bot --check-connection --timeout 60
```

相同 `message_id` 不會重複執行更新。已完成但回覆失敗的結果存於 `.runtime/bitable_bot/`，再次收到該訊息或重啟後只補發回覆；程序中斷時不自動重做可能已提交的修改。

離線驗證：`.venv/bin/python -m unittest bitable_agent.test_agent bitable_agent.test_bot -v`。

這個獨立目錄提供一個命令列 agent：讀取專案根目錄 `.env`，讓 DeepSeek 自行選擇工具，透過現有的 Lark OAuth / HTTPS 客戶端操作多維表格。原有郵件流程不需修改。

## 執行

在專案根目錄執行，沿用現有 `.venv`：

```sh
.venv/bin/python -m bitable_agent '列出可用的多維表格，再查看开户进度表有哪些欄位'
.venv/bin/python -m bitable_agent '在开户进度表新增一筆記錄，公司名称為測試公司，简要描述為等待提交資料'
.venv/bin/python -m bitable_agent '搜尋开户进度表公司名称為測試公司的記錄，只把简要描述改為已收到資料'
.venv/bin/python -m bitable_agent '將开户进度表記錄 recXXXX 的卡点清空'
.venv/bin/python -m bitable_agent '刪除开户进度表記錄 recXXXX'
```

寫入指令會直接操作 `.env` 指定的真實表格。每次命令為獨立對話；後續指令請提供表名、搜尋條件或記錄 ID。

另裝環境時：

```sh
python -m pip install -r bitable_agent/requirements.txt
```

可指定 `--env /path/to/.env`。需要記錄完整工具參數和結果時，加上 `--trace outputs/bitable_agent/trace.json`；這個檔案可能包含表格資料，`outputs/` 已由 Git 忽略。終端預設只顯示工具名稱、成功與否和最後回答。

本機現有 Lark 授權位於部署目錄，這個工作目錄沒有獨立授權。執行時加入以下選項即可直接復用，不會複製令牌：

```sh
.venv/bin/python -m bitable_agent \
  --auth-project "$HOME/Library/Application Support/inbox-onboarding" \
  '查看开户进度表有哪些欄位'
```

## 設定與提示詞

沿用 `.env` 的 `LLM_API_HOST`、`LLM_API_KEY`、`LLM_MODEL`，不複製金鑰，不選用備用模型。目前建立時的設定為 `https://api.deepseek.com` / `deepseek-flash`。

`LLM_ENABLE_THINKING=false` 對應 DeepSeek 的 `thinking: {"type": "disabled"}`；設為 `true` 可啟用 thinking，迴圈會保留 DeepSeek 所需的 `reasoning_content`。預設模型請求逾時 120 秒，可用 `LLM_TIMEOUT_SECONDS` 調整。

最基本提示詞在 `prompt.txt`，第一句為：

> 你是一個用來更新 Lark 多維表格的助手，可以查詢、新增、修改和刪除記錄。

其餘規則要求真實查詢、只更新指定欄位、明確要求才能清空或刪除、搜尋結果不唯一時詢問使用者、如實回報工具結果。

## 工具

| 工具 | 功能 | HTTP |
| --- | --- | --- |
| `list_tables` | 列出 `.env` 配置的表名和 ID | 本機設定 |
| `list_fields` | 讀取真實欄位與類型 | `GET .../fields` |
| `search_records` | 分頁瀏覽或按文字／單選欄位精確搜尋 | `POST .../records/search` |
| `get_record` | 讀取單筆記錄 | `GET .../records/{record_id}` |
| `create_record` | 新增記錄 | `POST .../records` |
| `update_record` | 修改指定欄位 | `PUT .../records/{record_id}` |
| `delete_record` | 刪除指定記錄 | `DELETE .../records/{record_id}` |

路徑前綴為 `/open-apis/bitable/v1/apps/{app_token}/tables/{table_id}`。只允許 `.env` 的 `LARK_MAIL_TABLE_ID` 和 `LARK_PROGRESS_TABLE_ID`，Lark 使用者授權沿用專案既有的 Keychain（macOS）或 DPAPI（Windows）儲存。

目前可寫入文字、數字、單選、多選、日期和勾選欄位。日期使用帶時區的 ISO 8601 字串；`null` 或空字串會清空欄位。未傳入的欄位保留。人員、附件、關聯和公式等欄位未加入寫入支援；不建立表或修改欄位結構。

工具定義以 Chat Completions `tools` 傳給 DeepSeek，使用 `tool_choice=auto`。Python 依 `tool_calls` 執行操作，將對應的 `tool_call_id` 和結果回傳模型，直到收到最後文字回答。預設最多 12 輪模型回覆、30 次工具呼叫。錯誤以 `ok=false` 回傳模型；上限或模型連線失敗會退出，已完成的寫入不會回滾。Lark HTTP 層沿用既有的暫時錯誤重試和新增 `client_token`；agent 不會自動重跑整個任務。

## 測試

離線測試（不呼叫模型、不寫雲端）：

```sh
.venv/bin/python -m unittest bitable_agent.test_agent -v
```

真實 DeepSeek + Lark 驗收：

```sh
.venv/bin/python -m bitable_agent.verify_live
```

本機驗收使用 `.venv/bin/python -m bitable_agent.verify_live --auth-project "$HOME/Library/Application Support/inbox-onboarding"`。

六個測試為查表與欄位、新增、搜尋後更新、清空欄位、查無記錄、刪除。驗收不強制模型選擇特定工具，而是核對實際成功工具呼叫和重新讀取的雲端值；更新測試同時確認其他欄位保留。

測試只建立唯一 `AGENT_TEST_<隨機值>` 公司記錄。測試工具執行層限制模型只能讀寫本輪建立的記錄，並在 `finally` 清理本輪標記的資料。完整參數、工具結果、雲端驗證結果與清理狀態存於 `outputs/bitable_agent/AGENT_TEST_*.json`。若進程遭強制終止，使用報告中的唯一標記和 `created_ids` 核對遺留記錄。

2026-11-10 驗證結果：10 個離線測試全部通過；使用 `.env` 的 `deepseek-flash` 完成 6 個真實模型與 Lark 測試，全部通過，測試記錄剩餘 0 筆。報告為 `outputs/bitable_agent/AGENT_TEST_50317611e83b4bce.json`。

官方參考：[DeepSeek Tool Calls](https://api-docs.deepseek.com/guides/tool_calls/)、[DeepSeek Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/)、[Lark Base API](https://open.larksuite.com/document/server-docs/docs/bitable-v1/bitable-overview)。
