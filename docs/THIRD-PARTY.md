# 第三方来源与许可

使用固定 `X-PG13/paper-digest@8906f9a12309956913eab29dade75c01cb7d0771` 的小部分元数据代码，位于 [app/arxivdaily/_vendor/paper_digest/](../app/arxivdaily/_vendor/paper_digest/README.md)。五个必要 Python 文件、原 [MIT 许可](../app/arxivdaily/_vendor/paper_digest/LICENSE)、[ORIGIN.json](../app/arxivdaily/_vendor/paper_digest/ORIGIN.json) 来源哈希保留。

实际复用基础 ID、作者/交叉分类规范化、重复元数据合并。应用先验证版本 ID，保留对应版本/时间/标题/摘要。上游采集器、模型、渲染、通知、CLI、状态与工作流未启用，本应用独立实现这些部分。

新增代码采用根及 app 的 MIT LICENSE。主要依赖：arxiv（MIT）、httpx（BSD-3-Clause）、beautifulsoup4（MIT）、pypdf（BSD-3-Clause）、Pillow（MIT-CMU）、pypdfium2（BSD-3-Clause、Apache-2.0 和捆绑许可）；微软免费翻译变体使用 translators（GPLv3），其依赖按各自许可分发；开发依赖 pytest（MIT）、PyYAML（MIT）。版本见 requirements.lock。translators 以未修改的依赖形式安装，应用只调用其公开的 `translate_text` API。

pypdfium2 wheel 带有 licenses/，包括 PDFium、freetype、libjpeg 等组件许可与数据署名。离线分发二进制时一起保留完整许可。应用通过依赖安装，没有复制二进制或引入 PyMuPDF。

论文原始材料用于本机阅读/回归，默认不提交。公开历史保存中文内容、必要内部短证据与具体版本出处。
