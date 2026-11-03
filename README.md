# ArxivDaily

每天抓取 arXiv 上与大语言模型（LLM）研究相关的论文，按关键词规则确定性筛选，翻译标题和摘要后用 Server酱推送微信简报。项目由 GitHub Actions 定时运行，每天以两种免费翻译变体各推送一次（谷歌、微软），中文侧可比较两版译文。

**日常路径不调用任何大模型，不需要 DeepSeek 或任何 LLM API key。** 筛选完全由 `config.toml` 中的分类、同义词组和 AND/OR 路线决定；翻译使用免费、无需密钥的 Google / Microsoft 服务。唯一必需的 Secret 是 `SERVERCHAN_SENDKEY`。

## 每日如何运行

- 北京时间每天 10:00（`Asia/Shanghai`）触发。
- 每个翻译渠道每天最多处理并推送 **10 篇未发送论文**，按提交时间从近到远选择，超出名额的留待次日。补齐档位为 **5 篇和 10 篇**：最近 7 天有 7 篇就补到 10 篇，有 1 篇或 0 篇则尝试补到 5 篇，恰好 5 篇不再补。只从最近 30 天内补齐，仍不足时按实际数量发送，完全没有未发送文章时发送无新增通知。
- 标题或摘要须命中大语言模型、LLM 或多模态大语言模型等明确用语；仅出现 foundation model、base model、agent 等泛词不入选。保留双语标题、摘要、来源和初筛路径。单条微信消息受 Server酱 32768 字节上限约束，放不下的论文整篇写入公开归档，消息中提供链接。
- 两个并行 job 使用同一份配置和同一套规则，分别采用 `google_free`、`microsoft_free` 翻译，各推送一条微信消息；标题带 `· 谷歌` / `· 微软` 后缀以便区分。
- `google_free` 直连 Google 翻译端点，请求之间默认暂停 1 秒，并对 429/403 退避重试；`microsoft_free` 使用 `translators` 包的 Bing 引擎（依赖已固定在 `app/requirements.lock`）。
- 两个 job 各用独立状态分支 `arxivdaily-state-google`、`arxivdaily-state-microsoft`，互不覆盖去重状态和历史归档。

## 日常只需调整两个入口

| 想调整什么 | 修改哪里 |
| --- | --- |
| 分类、关键词组与组合路线、路线优先级、内容模块、推送设置 | 根 [config.toml](config.toml)，参见 [配置说明](docs/CONFIGURATION.md) |
| 可选：切回 LLM 模式时的研究兴趣、总结详略、翻译提示 | [app/prompts/](app/prompts/)，参见 [提示说明](docs/PROMPTS.md) |

修改后提交到 `main`，下一次 Actions 使用新版本。本地立即读取当前文件。已成功生成和已推送的内容继续复用，不因修改配置自动重做或重发。

## 阅读论文与历史

每个变体维护自己的公开归档：

- 谷歌变体：[总论文索引](../../blob/arxivdaily-state-google/archive/papers.md) · [每日简报归档](../../tree/arxivdaily-state-google/archive)
- 微软变体：[总论文索引](../../blob/arxivdaily-state-microsoft/archive/papers.md) · [每日简报归档](../../tree/arxivdaily-state-microsoft/archive)
- [Actions 运行记录](../../actions/workflows/daily-digest.yml)：查看运行结果、错误及脱敏报告。

索引和简报由程序自动更新到对应状态分支，无需手工编辑。这些链接在 GitHub 上指向当前仓库，fork 后会指向自己的归档与 Actions。`archive.public_base_url` 默认留空，Actions 按当前仓库和变体分支自动生成；首次运行前归档链接可能尚不存在。fork 建议只复制 main 分支，避免继承上游的去重和投递状态，并在自己的仓库配置 `SERVERCHAN_SENDKEY`、启用 Actions。

## 本机检查和试跑

在项目根目录的 PowerShell 执行，启动脚本使用 conda `ml` 的 Python 和工作区依赖。

```powershell
# 检查配置与提示文件，无外部请求。
& ./app/scripts/run-local.ps1 config
# 查看完整生效配置。
& ./app/scripts/run-local.ps1 config --show
# 预览摘要的关键词命中，不调用模型。
& ./app/scripts/run-local.ps1 config --abstract 'We pre-train a large language model with instruction tuning.' --categories cs.CL
# 合成样本离线试跑，不修改正式状态。
& ./app/scripts/run-local.ps1 run
```

`run` 默认离线；`--live` 使用真实 arXiv 和免费翻译端点，不发送；`--send` 才更新正式状态并推送，还需同步发布命令。规则模式本地真实运行不需要 LLM 密钥：仓库默认的 `provider = "llm"` 只是兼容测试的安全默认值，Actions 会按变体覆盖；本地未指定翻译服务时 CLI 会自动改用 `google_free`，也可以显式选择：

```powershell
& ./app/scripts/run-local.ps1 run --live --translation-provider google_free
```

参数见 [运行说明](docs/OPERATIONS.md)。离线样本的判断和翻译是模拟结果。

要查看最近 14 天的规则初筛结果（调试用，与每日 7 天抓取窗口无关），打开 [初筛调试 notebook](app/notebooks/arxiv_rules_debug.ipynb)，选择 conda ml 内核。它只抓 arXiv 元数据，支持缓存、规则对比、命中依据和 CSV 导出；不调用模型或使用正式去重记录。见 [主动调试说明](docs/DEBUGGING.md)。

## 项目结构

```text
config.toml              唯一日常配置入口
.github/workflows/       定时/手工运行
docs/                    配置、提示、运行、架构与维护
app/
  prompts/               可选 LLM 模式的任务提示（规则模式不读取）
  notebooks/             主动调试入口
  arxivdaily/            核心代码（rulesmode.py 为确定性筛选适配器）
  scripts/               本地启动、状态发布、公开文件检查
  tools/                 可选开发工具
  tests/                 回归测试
  fixtures/              离线样本与固定来源清单
  pyproject.toml         Python 包与运行依赖
  requirements.lock     锁定依赖
```

本机依赖 `.runtime/`、正式状态 `.state/`、隔离试跑 `runs/`、临时输出 `tmp/` 位于 `app/`，不进入代码仓库。凭证只通过环境变量或 Actions Secrets 提供。

参见 [维护说明](docs/DEVELOPMENT.md)、[架构与行为](docs/ARCHITECTURE.md)、[第三方来源](docs/THIRD-PARTY.md)。新增代码采用 [MIT](LICENSE)。
