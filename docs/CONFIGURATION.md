# 配置说明

唯一日常入口是根目录 `config.toml`。本地 CLI 按应用位置寻找它，不依赖当前目录；Actions 读取同一文件。`--config PATH` 指定另一份配置，命令行覆盖只影响当次运行。提示目录相对路径以配置文件所在目录为基准。

修改后运行 `app/scripts/run-local.ps1 config` 检查；加 `--show` 查看实际配置。没有外部请求。缺键使用内部默认值；未知键、无效数值或缺失/空提示文件明确报错。

## 两种筛选模式

`[selection] mode` 决定终筛方式：

```toml
[selection]
mode = "rules"          # 当前默认：确定性关键词规则，不调用任何模型
route_priority = [ ... ] # 配置的路线名，按顺序排列，决定条目排序
```

- `rules`：日常路径使用 `RulesAnalysis`，只按 `[rules]` 的分类和关键词路线筛选。不做模型复审、总结、证据或 LLM 翻译；每篇命中论文统一进入“翻译档”，只呈现 `[presentation]` 勾选的元数据模块。不需要任何 LLM 密钥。
- `llm`：保留的旧行为，使用 `[llm]` 配置和 `app/prompts/` 做模型终筛、复审和总结，需要 `DEEPSEEK_API_KEY`。仓库默认 `config.toml` 已改为 `rules`；只有主动切回时才需要模型密钥。

规则模式不允许 `llm_summary` 或 `evidence`，CLI 会在启动时明确报错；需要全文总结时切回 `mode = "llm"`。

## 内容模块与额度

只使用 `[presentation]` 选择每篇论文的内容，固定顺序为：标题 → 标题翻译 → 摘要 → 摘要翻译 → LLM 总结 → 证据链 → 来源 → 初筛路径。至少启用一个模块。

```toml
[presentation]
title = true
title_translation = true
abstract = true
abstract_translation = true
llm_summary = false     # 规则模式必须为 false
evidence = false        # 规则模式必须为 false
source = true
screening_routes = true
```

不启用总结和证据链时不下载正文、不复审、不占用完整档名额；规则模式命中的论文以翻译档呈现，翻译失败保留为可恢复阶段。`screening_routes` 使用关键词初筛保存的全部命中路线（如 `llm_core`、`llm_training`），放在每篇最后。关闭标题时以 arXiv ID 作为条目标识。

`limits.full_daily_limit` 只影响 `llm` 模式的完整总结档；规则模式不生成总结，所以不会截断当日论文数，通知正文超出 `delivery.body_bytes` 时整篇移到公开归档，不截半篇。

## arXiv 初筛

1. `rules.categories` 中任一分类出现在论文任一交叉分类即可；空数组表示不限分类。默认 `["cs.CL", "cs.LG", "cs.AI", "cs.CV"]`。
2. 默认标题和摘要都参与匹配（`title_enabled = true`）；`exclude` 任一短语命中则排除。
3. `rules.groups` 同组词是 OR；每个路线内层列表是 AND，多个内层列表是 OR，各路线也为 OR。

```toml
[rules.groups]
LLM = ["large language model", "LLM", "foundation model"]
TRAIN = ["pre-training", "instruction tuning", "SFT", "RLHF", "DPO"]
INFER = ["chain-of-thought", "CoT", "reasoning", "in-context learning"]

[rules.routes]
llm_core = [["LLM"]]
llm_training = [["LLM", "TRAIN"]]
llm_reasoning = [["LLM", "INFER"]]
```

`llm_training` 对应 `LLM AND TRAIN`。忽略大小写，统一连字符/空格，缩写使用完整词边界，避免 CT/RL 匹配到其他单词内部。

**`[rules.groups]` 与 `[rules.routes]` 一起出现时会完整替换内置规则表**，不会和旧的分割/医学默认规则混在一起；只写 `[rules.groups]` 而不写路线时，按组名覆盖/新增同义词并保留内置路线。若只提供 `[rules.routes]` 而未提供 `[selection] route_priority`，路线优先级自动取配置中的路线顺序。

```powershell
& ./app/scripts/run-local.ps1 config --abstract 'We pre-train a large language model with instruction tuning.' --categories cs.CL
```

预览只显示初筛命中，不代表最终推送。增加关键词扩大候选范围；规则模式没有语义模型兜底，宁可宽召回。

检查实际日期范围会召回哪些文章，使用 [初筛 notebook](../app/notebooks/arxiv_rules_debug.ipynb)。它缓存分类元数据，让关键词/组合调整在本地反复比较；详见 [主动调试说明](DEBUGGING.md)。

## 路线优先级

`selection.route_priority` 必须是 `[rules.routes]` 中已配置路线名的非空、不重复列表。规则模式下所有条目都是翻译档，顺序只影响同一批条目的排列。默认顺序把 reasoning、training、agent、RAG、efficiency 等方向排在宽泛的 `llm_core` 之前。

## 翻译与标题后缀

`translation.provider` 可选 `llm`、`deepl`、`libretranslate`、`google_free`、`microsoft_free`。仓库默认 `llm` 仅用于兼容旧测试；Actions 会按 matrix 覆盖为免费服务。

- `google_free`：GET `translate.googleapis.com/translate_a/single`，每个文本一次请求，无需密钥。
- `microsoft_free`：先 GET `edge.microsoft.com/translate/auth` 取得 Bearer token，再一次 POST 翻译标题和摘要，无需密钥。
- `deepl`：官方免费 API 端点，需要 `TRANSLATION_API_KEY` 环境变量/Secret。
- `libretranslate`：自己的实例；本机 localhost HTTP 可用；机器翻译失败不会自动切换收费服务。
- `llm`：复用终筛模型和密钥，规则模式下不可用；CLI 会明确拒绝或自动改用 `google_free`。

`translation.label` 会追加到微信推送标题：`arXiv 每日简报 2026-10-02 · 谷歌`。留空则保持原标题；最长 32 字符。两个变体分别使用“谷歌”“微软”。

两者都走现有 `attempts`、`timeout`、退避重试和运行时间预算；响应形状不符合约定时明确失败，不做静默降级。

## 推送、归档与凭证

`[delivery]` 只引用密钥的环境变量名。规则模式日常运行唯一必需的 Secret 是 `SERVERCHAN_SENDKEY`；`DEEPSEEK_API_KEY` 和 `TRANSLATION_API_KEY` 在规则模式下都不需要。

`archive.state_branch` 由 Actions 读取。仓库默认值 `arxivdaily-state` 只是本地兜底；工作流的两个变体会在运行副本中分别覆盖为：

- `arxivdaily-state-google`：谷歌翻译变体的状态、索引和归档。
- `arxivdaily-state-microsoft`：微软翻译变体的状态、索引和归档。

`archive.public_base_url` 默认留空，Actions 在当次工作副本中按 `${{ github.repository }}` 和变体分支生成 `https://raw.githubusercontent.com/<owner>/<repo>/<state_branch>`，不把生成值提交回 main；fork 自动使用自己的仓库。显式填写时保留原值，需匹配实际公开归档。本地 `--send` 不执行 Actions 准备步骤，需在配置中提供完整公开地址和同步发布命令。

抓取、正文、推送等高级设置位于配置后半部分。`[llm]` 各段在规则模式下不会使用，保留是为了切回 `mode = "llm"` 时仍能通过校验并运行。
