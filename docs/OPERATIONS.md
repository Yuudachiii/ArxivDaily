# 运行、归档与恢复

## 你需要的两个归档

每个翻译变体各有一套状态分支和归档，名称对应 provider：

| 变体 | 状态分支 | 总索引 | 每日简报 |
| --- | --- | --- | --- |
| 谷歌 `google_free` | `arxivdaily-state-google` | `archive/papers.md` | `archive/YYYY-MM-DD.md` 与 `archive/YYYY/MM/DD/` |
| 微软 `microsoft_free` | `arxivdaily-state-microsoft` | `archive/papers.md` | `archive/YYYY-MM-DD.md` 与 `archive/YYYY/MM/DD/` |

索引列首次收集日期（北京时间）、英文/中文标题和链接，按基础 arXiv ID 去重，覆盖规则命中的候选。标题翻译失败先显示“待翻译”。每日汇总 `archive/YYYY-MM-DD.md` 是当日入口；`archive/YYYY/MM/DD/<digest-id>.md` 为每次发送的独立快照，同目录的 `notification.md` 为通知正文，JSON 为关联清单。规则模式没有每日篇数上限，所有命中论文的完整内容都写入当日快照；Server酱单条消息放不下的条目在消息里以归档链接呈现。已发送快照不覆盖，恢复补发建立新快照。

这些文件在对应状态分支，README 已提供直接链接。本机只有正式分支 checkout 到 `app/.state` 时才有对应远端状态；离线与未发送的真实试跑位于 `app/runs/dry-*/`。

## 分支如何自动更新

1. Actions 定时或手工启动，从 main 检出应用、根配置和提示。
2. 读取配置的状态分支名；首次正式运行没有分支时，自动建立只有空 state.json 的独立根提交。两个变体各建自己的分支。
3. 将状态分支检出到 `app/.state`，读取上次游标、处理阶段、缓存及发送记录。
4. 抓取/处理时保存阶段检查点；同步 publisher 检查远端没有被其他写入者推进，只提交 state.json 与允许的归档文件，再推送。
5. 更新总索引，生成当日汇总及发送快照。发送前确认公开快照可访问且字节一致，并保存发送意图/额度。
6. 调用 Server酱，有限查询，保存 confirmed、failed 或 unknown，再同步状态分支。

一次运行可能提交多次，不是一天仅一次；这是为了中断后恢复并复用已完成工作。两个变体使用不同的 job 级 concurrency 组（`arxivdaily-state-google` / `arxivdaily-state-microsoft`），互不取消；同一变体只串行、不并发。状态分支 push 不再触发日报，因为工作流只有 schedule 和 workflow_dispatch。

main 维护代码，状态分支维护机器状态和历史。存档放 main 技术上可行，但当前 publisher 面向独立 checkout，合并需要改发布/冲突处理。保留独立分支可避免频繁检查点混入代码历史和争用 main。机器状态本身支撑去重和恢复；规则模式不产生模型调用费用，状态里不记录 LLM 用量。

## Actions 日常操作

工作流当前配置北京时间每天 10:00（Asia/Shanghai），定时执行自动发送两条 Server酱通知（谷歌、微软），实际启动可能延迟。每次按提交时间回看最近 7 天，继续使用已有去重记录，只推送尚未发送的论文。规则模式不需要 LLM 密钥；Secrets 只需配置 `SERVERCHAN_SENDKEY`。`app/requirements.lock` 固定了 `translators` 及其依赖，Actions 安装依赖后微软变体无需额外配置即可使用 Bing 引擎。允许 Actions contents 写权限。真实发送前检查代码库和归档公开可读。

手工 mode 可选 daily、replay（指定 IDs）、backfill（UTC 起止日期）、retry（已有 ID 与失败阶段）；内容统一读取根配置 `[presentation]` 的模块开关。手工 dispatch 会同时运行两个翻译变体；只想验证一个变体时，可临时减少 matrix 或使用本地命令。

send 默认关闭，此时使用真实 arXiv 和免费翻译端点做隔离试跑，不要求 `SERVERCHAN_SENDKEY`。打开 send 后才修改正式状态并发送；定时运行自动发送。失败查看 job 日志和 `arxivdaily-run-report-<provider>` artifact，报告为 `app/tmp/reports/run.json`，保留 3 天。

## 本机运行

```powershell
# 无外部请求的合成样本。
& ./app/scripts/run-local.ps1 run
# 少量真实论文试跑，不推送，不需要 LLM 密钥。
& ./app/scripts/run-local.ps1 replay --ids 2504.11008v2 --live --translation-provider google_free
# 历史日期补采，不发送时仍隔离。
& ./app/scripts/run-local.ps1 run --start 2026-10-01 --end 2026-10-03 --live --translation-provider google_free
```

仓库默认 `translation.provider = "llm"` 只是测试兼容值；`selection.mode = "rules"` 时本地 `--live` 若不指定 `--translation-provider`，CLI 会自动改用 `google_free`，也可以显式改成 `microsoft_free`。

启动脚本切换工作目录到 app，参数中的相对报告/样本/状态路径据此解释；默认配置仍是根 config.toml，默认报告 tmp/reports/run.json。直接运行 python -m arxivdaily.cli 时，相对路径以当前目录为准。

本机正式发送还需 `--send`、`--state-dir .state`、`--checkpoint-command <同步发布命令>`，且 `.state` 是对应正式分支的独立 Git checkout；`.state` 中已有该变体自己的去重状态。优先通过 Actions 操作正式状态，不复制 mock 状态到正式分支。

返回码：0 成功，1 部分失败/可恢复问题，2 配置或持久化等错误。confirmed 是平台确认，不表示用户已读；超时或无法确认保存 unknown，下一次有限补发，可能有少量重复。

## 恢复与历史操作

### 三层去重

1. 抓取内去重：同一基础 ID 的交叉分类、分页边界和版本合并，保留最新元数据与分类。`2610.12345v1/v2` 是同一基础 ID。
2. 处理去重：每篇的规则判断、翻译、长文证据缓存保存在 state.json。已成功阶段复用，不因为重跑或改配置自动重做；已处理版本和内容保持对应关系，另外记录观察到的新版本。
3. 投递去重：论文的 confirmed 投递事件使其退出普通自动处理/推送。改模块、修订、改规则都不自动重推；晋升/重投必须显式请求。

两个变体各有独立状态，同一篇论文会在两个分支里分别投递一次。已拒绝的历史候选保留判断，改规则不自动重新评估它。要检验新关键词的召回，用不读取正式状态的 notebook 或独立 replay。

### 如何恢复

抓取成功与后续处理成功分别保存。采集中断保留已抓论文、标记覆盖不完整，不推进完整采集游标。下次滚动回看重新扫描，有界分片/分页恢复且按基础 ID 合并。

首次自动窗口与之后每天的滚动回看都是 7 天：正常运行时处理 `[now-7d, now]` 的提交时间范围，覆盖公开和索引延迟。已有状态中的旧 1 天下限不会限制新窗口；已确认发送的论文不会因再次采集而重复推送。超过 7 天的缺失日期记录为覆盖缺口并告警，需要时用 `--start/--end` 手工补采。论文首次发现后允许恢复 14 天，这是恢复限制，不会扩大每日抓取窗口。Notebook 使用明确人工窗口（默认 14 天），不受每日 7 天限制。

处理阶段先记录尝试意图，成功后保存结果。下一次优先复用成功部分，再补失败部分：

| 情况 | 下次行为 |
| --- | --- |
| 规则筛选成功、翻译失败 | 复用判断，重试翻译 |
| 推送失败 | 复用内容，只恢复投递 |
| 推送 confirmed | 普通运行跳过该论文 |
| 发送中崩溃或无法确认 | sending 转 unknown；有限补发、可能重复，不重新翻译 |

默认阶段最多 3 次跨运行尝试，失败后同一 UTC 日不会反复重试该阶段；请求内另有有限网络重试。超过首次发现后的 recovery_days 或累计次数则要求人工恢复。投递链的累计次数沿补发保留，不因建立新快照而清零。状态写入/发布失败时停止新的外部副作用。

正常重跑复用成功阶段，恢复失败阶段。自动重试超界后可指定 `run --retry-stage selection|body|review|summary|translation|title_translation|delivery --retry-ids ID...`；规则模式下 `body/review/summary` 会明确报错，因为日常路径不生成全文总结。

默认复制指定历史状态到隔离空间；--live 检查真实接口；--send 与 publisher 才修改正式状态。delivery 复用内容、不重新调用翻译。--promote 需要 LLM 总结/证据模块，规则模式不可用；--resend ID 显式重投已送内容，仍要求实发路径。

不把翻译失败降级成“无新增”。旧快照不改，冲突停止发布而不是强制覆盖。推送/告警/重试共享每日发送额度。
