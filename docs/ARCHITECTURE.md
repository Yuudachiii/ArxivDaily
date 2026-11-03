# 架构与行为

Python 批处理应用，没有网页界面或独立数据库。Actions 负责启动，每个翻译变体各有一条状态分支，保存可恢复 JSON 状态及 Markdown 历史。

| 模块 | 职责 |
| --- | --- |
| cli / config / prompts | 命令、配置校验、提示加载 |
| collection / rules | arXiv 分页、日期窗口、确定性关键词召回 |
| rulesmode | 规则模式适配器：把初筛命中转成 selection 决策，不调用模型 |
| llm / fulltext / translation | 可选 LLM 终筛/总结、正文/图表、标题摘要翻译（Google 直连、`translators` Bing、DeepL、LibreTranslate） |
| pipeline | 阶段编排、篇数/费用预算、恢复、索引、推送标题 |
| models | 论文、判断、内容与事件结构 |
| state | 原子保存、运行锁、检查点、脱敏 |
| render / delivery | Markdown、通知容量、Server酱结果 |
| simulation | 明确标注的离线实现 |
| upstream / _vendor | 固定上游元数据规范化 |

## 两种分析实现

`config["selection"]["mode"]` 在 CLI 选择分析实现：

- `rules`：`RulesAnalysis` 实现 pipeline/translation 使用的 `AnalysisClient` 子集（`select`、`review`、`summarize`、`translate`、`client`、`usage_records`、`_guard`、`_remaining_seconds`、`_begin_stage`、`run_deadline_monotonic`、`on_usage`、`usage_start_cursor`、`calls`、`prompt_fingerprint`）。`select` 对已命中的候选返回 `status=select`、`tier=translation`、`score=0`，路线取首个初筛路线或 `rules`；`review`/`summarize`/`translate` 只在被错误调用时抛 `ProcessingError("llm_disabled")`。pipeline 中 `llm_summary`/`evidence` 关闭时不会走到这些方法。
- `llm`：沿用 `AnalysisClient`，需要模型密钥，测试和旧配置继续支持。

规则模式按标题和摘要中的明确 LLM 用语召回，按提交时间从近到远分配每渠道每天最多 10 篇未发送论文，再进行免费翻译和推送。最近 7 天的文章数按 5 篇或 10 篇档位补齐（0 篇时目标为 5），只从最近 30 天补到该档位；数量不足时发送实际篇数，全无未发送文章时发送无新增通知。未取得当天名额的文章不判定、不翻译，继续保留至次日。每天的名单和已有发送记录分别用于限制处理数量和防止重发。此路径不下载正文、不调用大模型，两个翻译渠道使用各自的状态分支独立计数。

## 每日双变体

工作流用一个两元素 matrix（`google_free` / `microsoft_free`）并行运行两个独立 job：

- 每个 job 在“Prepare public configuration”步骤把运行副本的 `[translation] provider/label` 和 `[archive] state_branch` 改成自己的值，并按当前仓库生成 `public_base_url`。
- 状态分支分别为 `arxivdaily-state-google` 与 `arxivdaily-state-microsoft`，历史、索引、去重互不共享。
- job 级 concurrency 组按变体区分；同一变体串行，两个变体不互相取消。
- 推送标题追加 `translation.label`（如 `· 谷歌`），因此两条微信消息可区分；标题函数对空值/超长 label 保持原样。
- `google_free` 直连 Google 端点，按 `request_interval` 在请求间暂停，429/403 退避重试；`microsoft_free` 惰性导入 `translators` 包并用 Bing 引擎翻译，依赖固定在 `app/requirements.lock`。

## 核心约定

初筛宽召回，不决定相关性；标题和摘要都参与关键词，但完整词边界避免短缩写误命中。多分类、多路线合并到同一基础 arXiv ID。规则模式的命中路线全部保留并显示在每篇末尾。

规则模式命中的条目统一为翻译档，内容按 `presentation` 模块组合；翻译失败是可恢复阶段，不会降级成“无新增”。LLM 模式的 select/reject/uncertain 三态、全文复审、总结和证据链行为保持不变。

总索引覆盖规则候选；中文标题独立翻译，失败不丢行。正常无新增仍通知，失败单独报告。推送正文按 UTF-8 字节控制容量，单条 Server酱消息放不下的论文整篇留在公开归档快照，消息里给归档链接，不截半篇；归档本身保留全部命中论文的完整字段。

## 内容生命周期

main：代码、配置、提示、测试。两条状态分支：各自的 state.json、可变日期汇总/总索引、不可变发送快照。

成功内容跨运行复用，实际提示指纹随阶段记录。规则模式下 selection 指纹来自规则表；翻译服务名记录在阶段字段中。新配置不自动清除成功结果。副作用前保存检查点，发布失败阻止继续；发送确认与保存间仍有未知结果边界，用 unknown 和有限补发处理。

原文/PDF/图像/临时文件/密钥不写公开状态；翻译凭据（DeepL 等）只在内存中从环境变量读取，免费端点不需要密钥。隐藏推理和原始 provider 响应不进入公共归档。

规则、参数从 config.toml 调整；任务提示只影响 `llm` 模式。新增输出类别、改变档位/证据规则、状态格式属于代码迭代。核心源码维持 app，避免同时迁移安装契约。
