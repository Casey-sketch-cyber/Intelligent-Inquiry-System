# 架构说明

## 目录与模块

| 模块 | 职责 | 主要入口 |
|---|---|---|
| `ui/` | Flask 服务、工作台页面、型号与报价查询、任务和回复提交 | `ui/server.py`、`ui/static/index.html` |
| `lanhost/` | 双机部署、配对、办公端本机代理、工作机服务 | `lanhost/controller.py`、`lanhost/worker.py`、`lanhost/transport.py` |
| `inquiry/` | 批次持久化、供应商搜索、QQ 会话绑定、消息读写和报价解析 | `inquiry/jobs.py`、`inquiry/runner.py`、`inquiry/qq.py` |
| `vmhost/` | VirtualBox 管理、来宾服务、基于共享目录的签名通信 | `vmhost/host.py`、`vmhost/guest.py`、`vmhost/bridge.py` |
| `tests/`、`tools/` | 离线回归、浏览器验收、部署及诊断工具 | `tests/test_lan.py`、`tests/lan_ui_smoke.py` |

## 数据流

1. 办公端提交型号、数量和话术。双机模式下，本机代理通过配对后的 TLS 通道转发签名请求。工作机将批次和任务事务写入 SQLite，依次执行。
2. 工作机通过 Playwright 连接专用 Edge，读取 IC交易网供应商信息，再核验 QQ 会话和公司资料。确认发送后记录供应商绑定和原询价消息锚点。请求和发送结果不明确时保留待核实状态。
3. 点击「收集回复」后，工作机按本轮已确认的会话列表读取。消息先入库，DeepSeek 异步提取报价、生成回复建议。用户确认后，回复正文进入队列，工作机复核绑定并发送。
4. 页面从 API 读取任务和报价，报表模块从数据库生成 Excel。双机模式以工作机业务库为准。`inquiry/runtime.py` 将运行数据和临时目录限制在 D 盘。

## 外部依赖与服务

- Windows、Python 3.12、Edge、桌面 QQ。QQ 自动化依赖 UI Automation 和 pywin32。
- IC交易网需由用户登录，验证码由用户处理。模型配置使用 `.env.example` 中的 `DEEPSEEK_API_KEY` 和 `DEEPSEEK_BASE_URL`。
- SQLite 文件、配对材料、浏览器会话、报告和诊断输出保存在 `data/`，依赖与缓存放在 `.venv/` 和 `.cache/`。

## 待补充

- T07 工作用 QQ 和 Edge 版本待从实机自检取得。审核前只读诊断确认工作机的 QQ 主窗口存在，空闲界面尚未定位到全部发送所需控件。
- 工作机升级与 QQ 客户端兼容性的长期验收范围，待更多实机记录后补充。
