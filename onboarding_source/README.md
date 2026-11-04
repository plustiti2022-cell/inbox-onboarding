# 开户邮件自动跟进

Windows 正式流程由两个独立程序组成。抓取程序从 Microsoft Graph 读邮件并写入 Lark 多维表格，AI 程序从表格读取待办并写回处理结果。两者使用 HTTPS API 和现有用户授权。

## 当前结构

- Lark Base 内两张表：邮件信息表、开户进度表。
- 用户身份：项目自己完成 OAuth 授权，取得用户令牌后读写表格。
- 应用凭据：从项目根目录 .env 读取 LARK_APP_ID、LARK_APP_SECRET，用于应用校验和 OAuth 客户端认证。
- AI 提示词保持原内容。三字段 OR 匹配、正序处理、空值保留等原业务逻辑继续复用。

## 依赖

本机已建立项目 .venv，启动脚本优先使用它。其他环境可运行：

    python -m venv .venv
    .venv\Scripts\python.exe -m pip install -r onboarding_source/requirements.txt

凭据缓存使用 Windows DPAPI，并绑定当前 Windows 用户与机器。

## 首次设置

所有命令在项目根目录运行：

    .venv\Scripts\python.exe onboarding_source/setup_lark.py --verify-app
    .venv\Scripts\python.exe onboarding_source/setup_lark.py --authorize

第二个命令直接请求官方 OAuth 授权，显示用户授权链接，并在 outputs/lark/auth/user_authorization.png 生成二维码。完成授权后：

    .venv\Scripts\python.exe onboarding_source/setup_lark.py --finish-authorization

应用的接口权限和用户 OAuth 授权是两个环节。仅 App ID / App Secret 不会自动取得用户身份。应用凭据校验使用的应用令牌不会作为用户令牌用于表格读写；用户令牌缺失或失效时不会切换成应用身份。

用户令牌、刷新令牌和待完成的设备授权状态仅保存在 .runtime/lark/对应AppID/ 下的 DPAPI 加密文件，程序不打印令牌。缓存与 .env 均已加入 gitignore。用户令牌临近到期时自动刷新并保存新令牌；没有刷新令牌或授权被撤销时，需要重新授权。

已有用户授权可直接复用。首次用户授权申请 `bitable:app` 和 `offline_access`，分别用于读写多维表格和刷新令牌。

创建新 Base 和两张表：

    .venv\Scripts\python.exe onboarding_source/setup_lark.py --create --name "开户邮件自动跟进"

创建成功后将 Base 链接、Base token、两张 Table ID 写回 .env。已成功完成的设置会复用，避免中断后重复创建。只清理由本次创建产生、且确认没有记录的默认空表。

## 运行

从项目根目录分别启动两个程序。每次运行一轮后退出。

    .venv\Scripts\python.exe onboarding_source\fetch_mail.py
    .venv\Scripts\python.exe onboarding_source\process_mail.py

抓取程序在启动时固定截止时间。首次回补过去 24 小时，失败后沿用首次起点。此后取“启动时间的上一个整点”和“上次成功截止时间”中较早者为起点。查询包含起点、排除截止时间。已有 `internetMessageId` 直接跳过，本轮重复也跳过。所有分页和写入成功后才推进 `.runtime/graph/` 下对应邮箱与目标表的检查点。

AI 程序处理本轮读取到的待办快照，包括空白、未完成和失败状态。两个程序可同时执行，互不等待。后续入表的邮件在下一轮处理，按每轮内的收件时间排序。部署时配置 Graph 每小时 `:03`、AI 每小时 `:30`，此版本提供手动启动入口供 Windows 验收。

工作流 2 的内存控制：两张表每页读取 50 条，通过现有 HTTPS 分页接口写入本机临时 SQLite 快照，不把整表放进 Python 内存。全部分页成功后，先校验待处理邮件时间并在磁盘排序，再逐封读取正文、串行调用 AI。处理队列固定，写回状态不会造成翻页漏行；完成快照后新入表的邮件留到下一轮。此方案减少内存占用，仍需在开始分析前读完本轮快照，不是每完成 50 封就重新搜索云端待办。

开户进度匹配与模型上下文只读取公司、姓名、邮箱和匹配用的记录 ID，不重复复制进度描述、卡点等字段；记录 ID 不发送给模型。客户身份列表仍随客户数量增长，以保留原匹配规则。SQLite 页缓存约 2 MiB，排序使用磁盘临时存储；快照文件位于当前 Windows 用户临时目录下的 `onboarding-snapshot-*`，正常结束及捕获异常后删除，强制终止进程可能留下临时文件。快照不是恢复依据，下轮仍重新读取 Lark。

两个入口都支持 `--env`，AI 入口也支持 `--model`。原 `run_onboarding.ps1` 继续提供 Excel 测试和旧版命令。单独使用旧版 `--watch` 调试时需另装 `lark-oapi==1.7.5` 并配置事件订阅。

## 一次性迁移现有 Excel

    .venv\Scripts\python.exe onboarding_source/migrate_to_lark.py 'outputs/onboarding_01a19f8a/开户自动跟进.xlsx'

先迁移开户进度，再迁移邮件；保留原 AI 处理状态，不重新分析已经完成的邮件。云端采用真实 record_id，本地 ID 与云端 ID 的映射保存在项目 .runtime 下。

迁移重试会使用映射与已有记录核对，避免迁移中断后重复新增，不覆盖已经存在的云端记录。日常 Graph 抓取则通过 `fetch_mail.py` 独立去重。

## 表格字段和写入行为

邮件信息表保留 18 个业务字段，原 Excel 的 record_id 列改由云端系统记录 ID 提供。原收件人长表达式表头在输入时转为 toRecipients / ccRecipients / bccRecipients 的 names、addresses 字段。

开户进度表的业务字段：

| 公司名称 | 客户名称 | 邮件 | 球在谁方 | 卡点 | 简要描述 | 经办人 | 最新收件时间 | internetMessageId |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |

每次新增或更新进度时，「最新收件时间」和 `internetMessageId` 均取自本次来源邮件。现有进度行在下次更新时写入这两个值。给已有 Base 补建 `internetMessageId` 可运行 `.venv\Scripts\python.exe onboarding_source\setup_lark.py --add-progress-message-id`。

经办人使用邮箱 `@` 前面的文本。日期使用原生日期字段，传入毫秒时间戳，读取后还原带时区的 ISO 时间；是否有附件使用布尔字段，其余默认文本字段。

- 每封邮件一次 AI 请求，输入本次邮件和已有客户的公司/姓名/邮箱列表，不传旧进度或额外历史邮件。
- 客户邮箱从邮件参与人和正文识别，不直接等于发件人。
- 公司名称 OR 客户名称 OR 邮箱，忽略空值，统一全半角、首尾空白和大小写后完整相等匹配。
- 唯一命中更新，无命中新增；多条命中或全部身份为空，当前邮件失败并停止本轮。
- 未明确变化的字段保留原值；明确的新卡点整段替换，明确解决才清空。
- 经办人按配置在程序中的邮箱名单依次匹配发件人、收件人和抄送人。首个命中层级内优先取第一个非 James 的邮箱，只有 James 命中或三层均未命中时填 `james.yang`。球权按原提示词六条规则判断。
- 成功写 AI已处理、是否开户 是/否；失败写 AI失败 和原因。下一轮从最早未完成邮件继续。
- 附件不下载、不解析，不把“请查收附件”当成资料审核通过。

Lark 写入是独立请求，先更新进度，再更新邮件状态。读取和记录写入遇到临时网络或服务错误时，每隔 10 秒重试一次，最多重试 5 次。新增记录在同一次操作内沿用同一个 `client_token`。若用完重试仍失败，下一轮重新读取云端，按三字段找到已创建的进度。

## 报告和本地测试

Graph 抓取将窗口、抓取量、写入量和去重数量输出到标准输出。AI 报告保存在 `outputs/lark/runs/`，包含 `run_report.json` 和逐封 `results.jsonl`。

Lark AI 每完成一封即追加并关闭 `results.jsonl`，内存仅累加成功、失败、新增、更新等计数。`run_report.json` 保留汇总与错误信息，不再重复保存详细 `items`；逐封结果统一查 `results.jsonl`。本地 Excel 测试模式继续保留原报告结构。

本地 Excel 模式继续可用：

    .\run_onboarding.ps1 --backend excel --resume --model 'qwen3.8-max'

它仍使用 outputs/onboarding_01a19f8a/开户自动跟进.xlsx，可通过 --workbook 指定其他文件。Lark 模式不要求 Node.js 或 Excel 写入依赖。

验证：

    .venv\Scripts\python.exe -m unittest discover -s onboarding_source/tests -p "test_*.py" -v
    .venv\Scripts\python.exe onboarding_source/tests/verify_cli.py onboarding_source/tests/_outputs/cli_new_run
    .venv\Scripts\python.exe onboarding_source/tests/verify_live_model.py onboarding_source/tests/_outputs/live_max.json --model 'qwen3.8-max'
    .venv\Scripts\python.exe onboarding_source/tests/verify_live_lark.py
    .venv\Scripts\python.exe onboarding_source/tests/verify_live_lark.py --exercise

`test_windows_jobs.py` 使用模拟 Graph、Lark 和模型验证两个入口。`verify_cli.py` 使用本机模拟模型接口验证旧版 Excel 命令。

verify_live_lark.py 默认只读核对原 Excel 与迁移映射，检查两表数量及全部字段值。--exercise 会临时创建合成测试邮件，通过 .env 当前主模型验证新增客户、更新同一客户并清除卡点、排除非开户邮件，最后删除自己的测试记录并重新核对原数据。执行前要求云端没有真实未完成邮件；不会修改 AI 提示词。

## 配置项

.env 读取主模型的 LLM_API_HOST、LLM_API_KEY、LLM_MODEL、LLM_TIMEOUT_SECONDS，邮件沿用原四项 MICROSOFT_GRAPH_*。新增：

    ONBOARDING_BACKEND=lark
    LARK_APP_ID=应用ID
    LARK_APP_SECRET=应用Secret
    LARK_BRAND=lark
    LARK_USER_SCOPES=bitable:app offline_access
    LARK_BASE_TOKEN=创建后写入
    LARK_BASE_URL=创建后写入
    LARK_MAIL_TABLE_ID=创建后写入
    LARK_PROGRESS_TABLE_ID=创建后写入

LARK_BRAND 可为 lark 或 feishu，对应官方国际版或中国版域名。当前提供的应用凭据已在 Lark 官方域名验证。
