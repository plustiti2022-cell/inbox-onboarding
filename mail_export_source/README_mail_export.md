# 最近 24 小时邮件导出

`export_mail_last24h.py` 是主程序，`write_mail_workbook.mjs` 负责生成 Excel。
主程序只从指定 `.env` 读取下列四项，不包含密钥、不调用大模型：

```dotenv
MICROSOFT_GRAPH_TENANT_ID=你的租户
MICROSOFT_GRAPH_CLIENT_ID=你的应用
MICROSOFT_GRAPH_CLIENT_SECRET=你的密钥
MICROSOFT_GRAPH_USER_ID=你的邮箱或用户ID
```

使用 Microsoft Graph `/v1.0/users/{user}/messages`，范围是整个邮箱所有文件夹。
按 `receivedDateTime` 筛选 `[运行时刻 - 24 小时, 运行时刻)`，逐页读取 `@odata.nextLink`。
接口查询和最终导出均按 `receivedDateTime` 从早到晚正序排列。
每封邮件一行，共 17 列。保留原来的 15 列及列顺序，第 16 列 `AI处理` 固定写入 `AI未完成`，最后一列 `是否为开户邮件` 留空。六个收件人表头保留原表达式。
名称和地址分别过滤空值后用 `, ` 拼接。HTML 去除标签、脚本、样式和注释，保留文本及分段，解码 HTML 实体。
`receivedDateTime` 写为 Excel 日期，按 UTC 显示并标注 `Z`；`hasAttachments` 为布尔值。

仅对 HTTP 429、500、502、503、504 和网络错误重试，最多共 3 次，间隔 1 秒、2 秒。
没有备用接口、备用账户、自动更换配置、自动截断或字符替换。
请求或导出失败即报错退出。

## 运行

需要 Python 3.10+、`certifi`、Node.js 和 `@oai/artifact-tool`。
源码在当前 `mail_export_source` 文件夹内；默认读取上一级目录的 `.env`，不查找其他配置路径。如果依赖已配置到当前运行环境：

```powershell
python export_mail_last24h.py
```

当前这台电脑使用已安装的 Codex 运行时，可在源码所在目录运行：

```powershell
& "$env:USERPROFILE\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" .\export_mail_last24h.py --node "$env:USERPROFILE\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe" --node-modules "$env:USERPROFILE\.cache\codex-runtimes\codex-primary-runtime\dependencies\node\node_modules"
```

也可以使用 `--env 路径` 指定 `.env`，使用 `--output-dir 路径` 指定新的输出目录。
每次运行默认创建 `outputs/mail_last24h_时间戳/`，其中包含：

- `mail_last24h.xlsx`：最终 Excel。
- `mapped_rows.json`：按同样字段映射后的值。
- `run_manifest.json`：实际时间窗、邮件数、分页数和运行状态。
- `validation.json`：导出前的字段逐格比对结果。

可用 `python verify_mail_workbook.py 输出目录` 独立读取保存后的 XLSX，检查表头、数据、日期、布尔值、时间窗、筛选表和冻结表头。
Excel 正文单元格可能需要在编辑栏展开查看。

接口文档：https://learn.microsoft.com/en-us/graph/api/user-list-messages?view=graph-rest-1.0
