# Study Exchange 3.0 · 本地优先学习应用

PWA + Python 标准库 + SQLite。积分不能兑换，没有真实下单、钱包或交易凭证。无需 npm 或 pip 安装。

## 启动
安装 Python 3.10+，解压后在终端执行：

```bash
cd study-exchange-v3
python3 server.py
```

访问 http://127.0.0.1:8765 。Mac 也可运行 `bash start.command`，Windows 运行 `start-windows.bat`（需 Python Launcher）。退出终端即停止服务，重启保留数据。可设置 `STUDY_PORT` 修改端口。

支持安装 PWA 的浏览器可以将其作为独立窗口打开。iPhone 要添加到主屏幕，须先通过安全 HTTPS 地址访问服务。默认只绑定 127.0.0.1，不能直接跨设备访问。若需跨设备共享，请在个人常开主机部署，并通过有身份认证及 TLS 的私有反向代理访问；设置 `STUDY_ALLOWED_HOST` 为代理传递的精确 Host（含非默认端口），不要开放后端端口。当前包不含公网托管、账号认证或原生安装包。

SQLite 文件 `study.sqlite3` 自动创建。V2 浏览器 localStorage 不自动迁移，请保留旧版导出。

## 备份与恢复

- 页面右上角「备份数据库」下载完整 SQLite 快照（`GET /api/backup`，文件名 `study-exchange-YYYY-MM-DD.sqlite3`）。快照由 SQLite 在线备份接口生成，服务运行中也能得到一致副本。
- 恢复：停止服务，把快照改名为 `study.sqlite3` 覆盖原文件（或用 `STUDY_DB` 指向它），再启动。页面不提供上传恢复，避免误覆盖账本。
- 「导出 JSON」只用于查看记录，不能用来恢复。

## 使用体验

- 首次打开显示三步引导（记录任务 → 审核或自评 → 确认入账），完成或关闭后不再显示。
- 表单在提交前逐项提示长度与范围要求，并按约 5 分钟 1 SC 给出申报建议；未提交的内容自动保存在本浏览器，刷新不丢失。领域选「其他」时可填写自定义领域。
- 未入账的任务可以删除（`POST /api/tasks/delete`）；已入账任务不可删除，账本只追加不改写。
- 本地服务断开时顶部显示离线提示并可重试；页面隐藏时暂停定时刷新，回到页面或网络恢复时自动刷新。
- 账本可按「全部 / 发币 / 交易」筛选，默认显示最近 30 条。
- 状态标签用颜色区分：绿色正常，黄色需注意（如自评模式、报价过期），红色不可用。

## 模型审核

服务端读取环境变量（密钥不进入前端）：

```bash
export LLM_BASE_URL='https://api.openai.com/v1'
export LLM_API_KEY='YOUR_KEY'
export LLM_MODEL='YOUR_AUTHORIZED_MODEL'
python3 server.py
```

Windows PowerShell 使用 `$env:LLM_API_KEY='...'` 等语法。支持 OpenAI-compatible Chat Completions，需自行配置有权限的模型 API。无密钥时明确标记自评模式，不能伪装为模型审核。模型建议，用户确认；任务必须填写名称、领域、学习过程、成果证据、时间和申报币数。相同成果指纹拒绝重复提交，自评或修改建议需要理由。1 SC 约 5 分钟有效学习，模型结合质量调整。每项 0–100 SC；XP 等于累计确认发币量，代表自申报成果积分而非外部认证能力。不要提交公司内部材料、密钥或其他敏感信息，模型会收到任务及近期任务标题和积分。

## 市场与结算

- Polymarket：Gamma REST 获取最多 60 个活跃市场，筛选接受订单的二元 YES/NO 事件。页面直接订阅公开 CLOB market WebSocket，10 秒 PING、断线重连、REST 每 15 秒回退；超过 45 秒无报价则阻止开仓。显示接收时刻，不保证零延迟。离开市场页即停止订阅，减少干扰。
- 后端开仓/平仓重新请求 `GET /book?token_id=...`，按公开卖盘/买盘深度逐档模拟，深度不足拒绝整单。模拟 1 SC 对应 1 价格计量单位，不可兑换。不含实际平台费、最小订单、撮合竞争、延迟、价格冲击等真实执行限制。
- 后台每 60 秒检查持仓。仅在 Gamma 明确 `closed=true`、`umaResolutionStatus=resolved` 且 outcomePrices 精确为 1/0、0/1 或 0.5/0.5 时自动结算。其他生命周期（尤其未暴露 UMA 终态的 V2 市场）保守等待；不是全面覆盖所有新协议的结算引擎。不会从临近 1 的报价或截止日期推断结果。服务停机期间不运行检查，启动后重新检查。活跃事件可手动平仓。
- BTC/NVDA：历史回放而非实时行情。BTC 使用 Coinbase 完整日线；NVDA 使用 Twelve Data 历史日线，需 `TWELVE_DATA_API_KEY` 及相应权限。最近 30 个采样点隐藏后 15 个，真实行情本身公开，不是防作弊竞赛。虚构演练明确标记。
- BTC/NVDA 逐仓模拟：1/2/5/10x，名义金额 0.1% 模拟费，维持保证金 0.6% 简化清算价。检查后半区间高低价是否触及参考清算线。忽略真实跳空、资金费、阶梯保证金与滑点。最多损失该次虚拟保证金。
- 每日最多新建一次仓位（UTC+8），单次 1–20 SC，同时最多一个未结算仓位。交易不扣 XP，无真实资金入口。

## 验证

```bash
python3 -m unittest -v test_server
```

本地测试覆盖发币、重复申报、并发、模型审核模拟、自评、订单簿深度、断网不成交、结算、回放隐藏、删除未入账任务、数据库备份与 HTTP 下载。交付环境无法访问外部 API，真实行情与模型需在具备网络访问权限和模型配置的环境联调。失败不生成假行情或假审核。

官方接口文档：
- https://docs.polymarket.com/market-data/realtime-data
- https://docs.polymarket.com/api-reference/markets/get-market-by-id
- https://docs.polymarket.com/api-reference/market-data/get-order-book
