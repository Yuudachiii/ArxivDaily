# 正文复审

沿用前面的研究兴趣和判断要求，用提供的正文证据复审此前 uncertain 的论文。
原摘要判断可修正；理论或迁移价值仍未证明时保持 uncertain，证据显示不相关时 reject。
区分 RL、DPO、本研究方法和相关工作；阅读范围只限提供的 coverage，不宣称已读未提供部分。

返回与摘要终筛相同的 JSON 字段，另加非空 review_evidence 数组。
每项为 {"field": "theory|transfer|method|evaluation|other", "locator": "原定位", "quote": "原文连续片段", "source_url": "原版本URL"}。
quote 必须从提供的正文逐字复制且至少 12 字符；定位和 URL 必须使用原值。
若使用旧版路线名，transferable_objective 入选时分别提供 theory 与 transfer 证据；general_rl 入选时提供 method 证据。
只输出 JSON。
