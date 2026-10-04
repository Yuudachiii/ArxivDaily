# ArxivDaily 项目规则

- 操作前先读取 `Z:\LLM\小插件\AGENTS.md`，再读取本文件。
- Git 根目录是本目录；当前任务仅在 `Yuudachiii/ArxivDaily` fork 上实验。
- 应用在 `app/`，工作流在 `.github/workflows/`；本地依赖放 `app/.runtime/`。
- 凭据只通过环境变量或 GitHub Actions Secrets 注入，不写源码、日志或提交。
- 离线验收使用现有测试和 `simulate`，真实调用需要单独确认费用与推送范围。
- 原始论文 HTML/PDF/图片不随 Git 分发；涉及这些文件的测试须注明覆盖限制。
- 本地变更日志为 `docs/CHANGE_LOG.md`，只在本地维护并保持 Git 忽略。
