# 主动调试 arXiv 初筛

打开 [app/notebooks/arxiv_rules_debug.ipynb](../app/notebooks/arxiv_rules_debug.ipynb)，在 VS Code/Jupyter 中选择 conda ml 的 Python 内核。它只抓公开 arXiv 元数据，然后执行生产代码的关键词匹配；没有 LLM、翻译、正文、StateStore 或推送，不产生模型费用、不受已推送去重限制。

## 最常用的操作

1. 默认 DAYS=14、END_DATE=None，查询北京时间昨天结束的前 14 个完整自然日。也可指定 END_DATE。分类来自根 config.toml。
2. 运行抓取单元。没有缓存时联网分页，有缓存则复用。数据完整性、篇数、日期和抓取时间会显示出来。
3. 运行初筛单元查看文章列表、路线计数。点击标题打开论文，或指定 PAPER_ID 检查摘要和具体关键词依据。
4. 修改并保存根配置的 rules/groups/routes，再只运行初筛单元。它重新读取文件，用同一批元数据计算结果并保存与上一次的差异。5. 可选查看新增、移除和路线改变的论文，或导出含摘要/依据的 CSV。

默认“前 14 天”是本地自然日，底层转换成 UTC 的 submittedDate 查询。不是“最近 14 天修订过”的论文，也不是日常程序首次默认 7 天窗口。API 返回抓取时的版本，缓存不代表历史日期当时的文本。

## 缓存与覆盖

缓存保存分类/日期内全部已抓元数据，包含关键词不命中的负例；若只缓存旧关键词命中者，扩大关键词后会错误漏掉新入选者。关键词与路线不参与缓存键，日期、分类、调试上限参与。

缩小分类可以在原缓存内筛选；扩大分类或取消分类限制，需要重新抓取。改变日期也重跑参数/抓取单元。只改变 groups/routes/exclude/title_enabled 可本地重算。

默认调试上限 MAX_METADATA=10000，与正式 max_candidates 分开，不修改配置。网络失败或触及容量上限时 complete=False，显示部分结果和错误；不能当作该范围的全部文章。必要时缩小日期/分类；缓存中的错误可用 REFRESH=True 重新抓取。缓存不自动感知远端新数据，要更新也需 REFRESH=True。

缓存和 CSV 放 app/tmp/rules-debug，属于 Git 忽略的本机调试产物，不进入正式论文索引。表格默认仅展示前 100 篇，完整命中列表在 matches；一篇可命中多条路线，路线计数相加可大于去重论文数。当前 config.toml 已替换为 LLM 研究关键词；`[rules.groups]` 与 `[rules.routes]` 一起出现时会完整替换内置规则表，只想禁用单个路线时设为 `[]`。

## 内核

本机 ml 已安装 ipykernel，并已注册为 Python (ml)。若换机器后 Jupyter 中没有 ml 内核，在已激活的 ml 中注册：

```powershell
conda activate ml
python -m ipykernel install --user --name ml --display-name "Python (ml)"
```

Notebook 会检查 sys.executable，发现不是 ml 时明确停止。没有安装 pandas/nbformat 的要求，使用标准库和内核自带的 IPython 展示。提交 notebook 前清除运行输出，结果可保留在本机 CSV。
