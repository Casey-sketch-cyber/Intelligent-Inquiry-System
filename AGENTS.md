# 询价系统协作说明

本项目是 Windows 电子元件采购工作台，按型号管理询价、供应商报价和回复。默认部署由办公电脑提供工作台，另一台已配对的 Windows 电脑负责 QQ 和网站操作。

技术栈为 Python 3.12、Flask、SQLite、原生 HTML/CSS/JavaScript、Playwright、Windows UI Automation 和 OpenAI SDK（用于 DeepSeek）。

## 环境与命令

- 项目、临时文件、依赖、日志、数据和构建产物放在 D 盘。按 [README](README.md) 设置临时文件和缓存路径后，用 `python -m venv .venv` 创建环境，再运行 `.\.venv\Scripts\python.exe -m pip install -r requirements.txt`。
- 双机入口为 `启动双机工作台.bat`，单机入口为 `启动天芯询价.bat`。部署及工作机状态页见 [双机部署说明](docs/dual-machine.md)。
- 离线回归命令为 `.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v`。网页改动可运行 `tests\ui_smoke.py` 或 `tests\lan_ui_smoke.py`。JavaScript 用 `node --check ui\static\app.js` 和 `node --check ui\static\workbench.js` 检查。
- `.env.example` 和 `settings.example.toml` 是示例。保留已有 `.env` 与 `settings.toml`。离线测试使用替身和临时数据库，`tools/test_llm.py` 会连接真实模型服务。

## 目录

- `inquiry/` 负责询价、会话校验、回复队列、采集、解析和报表。
- `ui/` 提供 Flask API 和工作台页面。`lanhost/` 管理双机配对、办公端代理和工作机。`vmhost/` 提供虚拟机独立模式。
- `tests/` 存放离线回归和界面验收。`tools/` 存放操作和诊断工具。完整模块说明见 [架构文档](docs/ARCHITECTURE.md)。

## 工作规则

1. 开工前读 `docs/PROGRESS.md`、`docs/ARCHITECTURE.md` 和 `docs/DECISIONS.md`。在 `D:\PROJECT\lzt` 的本地项目中，同时按工作区规则读写根目录的私有 `PROGRESS.md`。
2. 检查待处理的审查意见，再从 `main` 为每项任务新建 `<工具名>/<任务简述>` 分支。依赖未合并的任务时，从对应分支开始，并在 PR 说明中注明依赖。
3. 只改本任务涉及的文件。保持现有命名和结构，新增注释及文档使用中文。添加依赖时说明用途。
4. 提交前运行相关测试和检查。实际发送 QQ 消息需要用户明确授权。提交只包含源码、测试、示例配置和适合共享的文档。
5. 按 `ai-workflow` 更新共享进度，记录未完成事项。重要取舍写入 `docs/DECISIONS.md`。密钥、真实配置、数据库、聊天内容、配对文件及 AI 私有记录留在本地。
6. 推送任务分支并创建 PR，由审查者决定是否合并。无法创建 PR 时推送分支并如实报告。禁止自行合并到 `main`、强制推送或改写已推送的历史。
7. 与用户用中文沟通。共享流程见 [ai-workflow](https://github.com/Casey-sketch-cyber/ai-workflow)。
