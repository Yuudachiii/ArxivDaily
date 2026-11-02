# 模型提示入口

> 当前根配置为 `[selection] mode = "rules"`：日常抓取、筛选、翻译和推送都不读取这些提示，也不需要 LLM 密钥。以下内容只在主动切回 `mode = "llm"` 时生效。

全部提示集中在 `app/prompts/`，UTF-8 Markdown，可以直接中文修改。内容原样读取，不使用模板替换，JSON 花括号无需转义。每次启动读取一份快照，新运行使用修改后的文件。

| 文件 | 使用场景 | 适合调整 |
| --- | --- | --- |
| selection.md | 摘要终筛 | 研究兴趣、相关性、保留/拒绝标准 |
| review.md | uncertain 正文复审 | 确认所需证据；同时读取 selection 的要求 |
| summary.md | 完整档五段总结 | 长度、重点、表达、实验与限制 |
| section_notes.md | 启用证据链且正文超出输入容量 | 长文/附录证据提取重点 |
| translation.md | 双语标题和摘要 | 术语和表达，完整忠实翻译 |
| title_translation.md | 索引缺少中文标题 | 标题译法、名称和缩写 |

最常改 selection.md 与 summary.md。例如在 selection 补充“只做传统微调、没有 LLM 方法贡献的文章不入选”；在 summary 要求“优先解释训练目标如何影响模型能力，不罗列模块名”。关键词调整在 config.toml，模型兴趣调整在提示文件。

## 保留输出结构

文件已包含输出要求。可修改偏好和写作风格，保留 JSON 字段、枚举和证据要求：

- selection/review：status、route、tier、reason、abstract_evidence、uncertainty、四个方法/证据布尔标记及 score；review 另需 review_evidence。
- summary：background、contribution、method、experiments、conclusion、evidence。
- section_notes：notes 数组，包含 locator、quote、kind。
- translation/title_translation：title_zh、abstract_zh；只标题时 abstract_zh 为空字符串。

程序检查字段和完整响应；启用证据链时另检查来源、引文及数字。summary 输入中的 evidence_enabled 来自模块配置：false 时 evidence 必须为空数组，只生成总结。失败保留为可恢复阶段。

## 验证和缓存

`app/scripts/run-local.ps1 config` 检查可读、非空，显示内容指纹，不检查模型质量。离线 run 来自固定样本，也不能评价新提示质量。

评估新提示用少量 ID 的 `replay --live`，默认隔离、不发送，会产生模型费用，见 [运行说明](OPERATIONS.md)。

真实阶段记录提示指纹：复审包含 selection 和 section_notes，总结包含 section_notes；长文证据缓存按提取提示内容区分。已完成终筛、总结、翻译和投递继续复用，不因提示变化自动重做历史。失败阶段恢复使用新提示。机器翻译不读取这些 LLM 提示。
