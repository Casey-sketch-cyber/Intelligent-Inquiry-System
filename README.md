# 天芯智能询价系统

Windows 本地采购工作台：从 IC交易网搜索供应商，通过已登录的 QQ 询价，保存回复并用 DeepSeek 整理报价和回复建议。界面仅监听本机。

## 局域网双机运行（当前部署路线）

办公电脑运行网页工作台，另一台专用 Windows 电脑运行 QQ、网站和回复采集。入口为 `启动双机工作台.bat`；工作机离线安装到 `D:\TianxinWorker`，在本人登录 QQ 和网站后导入连接文件。完整过程见 [双机部署说明](docs/dual-machine.md)。

常规双机流程已接入：在办公电脑新建询价，任务先持久化，再由第二台依次执行，无需运行测试脚本。新安装默认锁定实发，配对且空闲时可在办公工作台授权开放；当前企业工作机已按用户授权开放。工作机启动默认暂停，可从办公电脑恢复。私有迁移包包含本企业资料，不可分发给其他客户。

本轮实体测试已对三个指定型号各 5000 个、各三家完成九条询价。常规入口已验证实体接单和验证码等待；完整九条实发来自此前限定测试，不能将离线替身测试当作新增实体实发。发信后不自动核对回复；点击顶部「收集回复」才读取一轮，完成即停止，中途可点「暂停收集回复」。网站要求验证码时等待本人处理。

离线包自带独立 Python，解压在 D 盘并使用专属依赖环境；不再依赖系统 Python 安装器。同包安装中断可保留数据后继续，旧包可用附带运行环境的修复工具续装。

## 虚拟机独立运行

新增 VirtualBox 运行方式：办公电脑运行控制台，QQ、网站搜索和回复采集在独立 Windows 虚拟机内执行。入口是 `启动独立运行环境.bat`。首次安装、数据迁移、正常关机以及验收范围见 [虚拟机部署说明](docs/virtual-machine.md)。

当前提供的配对配置为**只读验收模式**，真实询价、回复和名片发送均被拦截。主机软件、虚拟机配置和虚拟磁盘放在 D 盘；虚拟机内的工作目录为 `D:\TianxinRuntime`。离线测试通过不代表真实 QQ 或 Windows 来宾安装已验收。

## 安装与存储

项目必须放在 **D 盘**。需要 Windows、Python 3.12、Edge、桌面 QQ，以及 IC交易网和 DeepSeek 的有效配置。在项目目录的 PowerShell 中执行：

```powershell
$env:TEMP = Join-Path $PWD '.cache\tmp'
$env:TMP = $env:TEMP
$env:TMPDIR = $env:TEMP
$env:PIP_CACHE_DIR = Join-Path $PWD '.cache\pip'
$env:PYTHONPYCACHEPREFIX = Join-Path $PWD '.cache\pycache'
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $PWD '.cache\playwright'
New-Item -ItemType Directory -Force $env:TEMP | Out-Null
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item settings.example.toml settings.toml
Copy-Item .env.example .env
```

在 `.env` 填入模型密钥，在 `settings.toml` 设置参数。**已有配置请保留，不要覆盖。** 双击 `启动天芯询价.bat` 打开系统。

临时文件及缓存位于 `.cache/`；数据库、日志、报告、下载、两个专用 Edge 配置均位于 `data/`。源码仓库不包含真实配置、密钥、名片、客户聊天记录或浏览器登录数据。备份正在使用的数据库请使用 SQLite backup。

界面默认使用 8765，冲突时自动避让，实际地址记录在 `data/ui_server.json`。重复启动核对项目和服务实例。指定 `ui/server.py --port 18765` 时端口冲突明确报错。

## 日常操作

1. **新建询价**：逐行输入，或点「批量粘贴型号」。每行一个型号，可附数量；未填数量的行使用「统一数量」。一批支持 1–100 个型号，重复或不合法条目会阻止提交。
2. **统一话术**：必须包含 `{part}` 和 `{qty}` 或 `{qtyk}`。每个型号显示自己的完整预览。双机模式默认常规实发、每型号最多三家，提交前显示确认；可勾选试运行，只搜索不发送。原单机模式仍默认试运行。
3. **询价任务**：按型号合并并支持搜索。供应商列表及家数只包含已确认发送成功的记录，失败、等待协助及结果未确认的记录不展示；原始尝试仍保留供排错和防止重发。每家公司展示最近已询价的记录，历史任务可单独打开。不同后缀、斜杠或连字符的型号不会混为一个。
4. **查看会话 / 回复**：查看原话及 DeepSeek 建议，选择后可编辑，也可手动输入。点击「发送到 QQ」后，仅将确认的正文发给记录中绑定的供应商和 QQ 会话。型号仍保存在系统内的绑定记录中。
5. **名片**：设置中独立控制「询价后发送名片」。默认关闭，关闭后保留图片。
6. **收集回复 / 报告**：顶部按钮在每个页面固定展示。点击一次，只处理点击时已绑定的询价会话一轮；读取失败保留原因，不自动重试。详情页可以只收集当前型号；暂停不影响新询价。报告以已收集并解析的数据为准。

## 原本机模式的后台能力与前台协助

默认模式不切前台、不恢复窗口、不使用全局鼠标键盘。文字发送、会话切换、翻页优先使用标准无障碍接口。**接口可用性取决于 QQ 版本及控件，不能承诺所有步骤都能完全在后台执行。**

不支持后台操作的步骤会暂停并保留进度。首次从网页唤起新会话、粘贴名片等，需要在设置中开启「QQ 前台协助」，再由用户手动把 QQ 放到前台。切到其他软件后，需要鼠标键盘的步骤暂停，不会把 QQ 抢回来。QQ 主窗口可以被遮挡；最小化、只留托盘或锁屏时不能保证读取。

常规发信流程不再轮询回复，也不在结束时自动逐家补收。空闲时不会自动检查当前可见会话；只有用户点击「收集回复」才执行一次有限读取，暂停或结束后等待下一次点击。发送时为核对供应商、去重和确认发出所必需的读取仍保留。

## 回复与发送可靠性

- 发送前复核供应商公司及会话，身份冲突或不足时阻止发送。
- IC交易网的型号搜索、翻页和供应商补查默认间隔至少 30 秒，任意连续 10 分钟最多 10 次。额度在 SQLite 中保留，重启后继续生效。可用 `settings.toml` 的 `search_min_interval` 和 `search_max_per_10min` 调整。
- 询价、名片、界面回复共享持久化限速记录：最短间隔 20 秒、每小时最多 60 次发送尝试，可进一步放慢。重启不会清空额度。这不能保证不触发腾讯风控，没有使用绕过机制。
- 每次回复有唯一请求标识，双击和网络重试不会重复入队。发送中断或结果无法确认时标为待核实，不自动重发。
- 原文先保存，解析与建议通过独立队列处理；失败最多自动重试三次，也可手动重试。模型建议不会自动发送。
- 同一个 QQ 会话可询价多个型号；没有型号且存在多个可能归属的消息保存在「待确认归属」，人工关联后再解析。
- 读取需确认询价锚点或已保存消息的连续重叠。部分快照只能补充数据，不能覆盖已有完整记录。旧任务时间不明或历史不连续时需要补收或人工核对。
- 图片只保存占位文字，不做 OCR。有效报价要求明确正单价；税务未知和美元报价不参与含税人民币排序。

## 命令行

```powershell
.\.venv\Scripts\python.exe -B main.py --dry-run STM32F405RGT6:5000 GD32F405RGT6:3000
.\.venv\Scripts\python.exe -B main.py --foreground-assist STM32F405RGT6:5000
.\.venv\Scripts\python.exe -B main.py --collect 3 4
.\.venv\Scripts\python.exe -B main.py --report 3 4
.\.venv\Scripts\python.exe -B main.py --stop
```

命令行默认后台模式，前台协助需要显式添加参数。停止使用界面按钮或 `main.py --stop`，不要强行结束正在发送的进程。真实询价请先核对预览和供应商范围。

## 离线验收

先按安装步骤设置 D 盘临时目录，再执行：

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -v
.\.venv\Scripts\python.exe -B tests\ui_smoke.py
.\.venv\Scripts\python.exe -B tests\vm_ui_smoke.py
.\.venv\Scripts\python.exe -B tests\lan_ui_smoke.py
node --check ui\static\app.js
node --check ui\static\workbench.js
```

测试使用隔离数据库及模拟 QQ / 模型，禁止真实发送。浏览器验收使用隔离本机服务、D 盘临时 Edge 配置，截图保存在 `data/qa/`。`tools/test_llm.py` 会真实调用模型，不属于离线测试。
