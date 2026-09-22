# Bybit Demo 15 节点报告恢复证据

日期锚点：2026-09-22

## 结论

历史 15 节点报告正文可以由本地 fixture 与当前 Bybit Demo 报告程序核对，但不等于 n8n 执行记录或 Telegram 服务器备份。

原始 n8n 导出、旧 SQLite、执行记录和 Telegram 服务器备份均未作为本仓库部署依赖。因此 `n8n/bybit-demo-risk-report.15-node-reconstructed.json` 是安全边界约束下的逆向重建版，不是字节级恢复，也未自动导入当前 n8n 数据库。

文件保留了历史 15 个“报告证据”职责，并增加 3 个 Bybit Demo 动态保护桥接职责（共 18 个节点）：

1. 每 30 分钟触发
2. Demo Trading 只读安全门
3. 本地报告 HTTP 入口
4. 持仓缓存与过期标记
5. 核对实际持仓币种
6. 核对 MA5/10/20/60
7. 核对局部 Fib 结构
8. 核对 Grok 新闻摘要
9. 汇总市场证据
10. 核对 GPT 风险分析
11. 独立止损安全校验
12. 止盈与 Fib 有效性校验
13. 生成中文风险报告
14. 报告字段与 paper-only 校验
15. Telegram HTML 输出
16. 提取实时动态 TP/SL 保护信封
17. 调用本地动态保护桥接
18. 保护结果安全闸门

## 运行边界

- 工作流 `active=false`，导入后需要人工配置 Telegram 凭据并明确启用。
- n8n 只访问 `127.0.0.1:38635/report` 与 `127.0.0.1:38636/protect`。
- Bybit V5 签名、实时持仓、K 线、Trading Stop、主动平仓、安全门和熔断全部由 Python 负责。
- 只允许 Bybit 主网 Demo Trading：`https://api-demo.bybit.com`。
- 禁止真实主网和 Testnet 地址；工作流文本也会拒绝这些地址和交易端点。
- 缓存持仓可以生成报告，但不能生成动态保护信封或授权执行。
- 默认保护执行关闭，桥接在报告模式下返回 `REPORT_ONLY`，而不是触发交易。

## 证据来源

- `examples/bybit-demo-report-fixture.json`：无凭据的 Bybit Demo 报告 fixture。
- `bybit_live_reporter.py`：实时位置、1m/4H K 线、模型证据和保护信封的唯一 Python 入口。
- `bybit_risk_reporter.py`：MA、Fib、止损方向/强平边界、风险收益比和 paper-only 报告。
- `dynamic_protection_service.py`：实时来源、报告版本、时间有效期、symbol 匹配和 Demo host 安全门。
- `tests/verify_n8n_15_node.js`：逐节点执行 Code 节点，验证报告证据链和失败关闭。

## stdout 契约

报告入口的 stdout 必须满足：

1. 以 `【Bybit Demo量化风险报告】` 开头（允许 Markdown 粗体包裹）。
2. 包含 `不执行交易`。
3. 正文之后的逐行 JSON 只作为 metadata 解析。
4. 包含实时实际持仓 symbol（如 `ETHUSDT`）。
5. 包含 MA5、MA10、MA20、MA60。
6. Fib 数据不足时只能明确无效，禁止伪造数字。
7. 包含新闻状态、风险等级、止损和止盈校验。
8. 动态保护 metadata 必须标记 `position_source=realtime`、`paper_only=true`、`trading_mode=demo` 和 `source=live_reporter`。
9. 不允许输出真实主网、Testnet 或直接交易端点。

## 验证项目

- JSON 解析、18 个节点和 17 条连接。
- 工作流未激活。
- 只读入口和保护入口均为 loopback HTTP bridge。
- 所有 Code 节点使用 `node --check` 语法验证。
- Bybit Demo fixture 可以通过报告证据链。
- 删除 `不执行交易` 时必须失败关闭。
- 缓存持仓不能通过实时保护信封校验。

## 未验证事项

- 原始 n8n 节点名称、坐标、版本和精确拓扑。
- 真实外部 API 响应、真实 Telegram 投递和用户账户中的 Demo Trading API key。
- 本文件不代表已在用户 n8n 实例中自动导入或激活工作流。

## 安全状态

本次没有读取、输出或复制任何 API key、secret 或 Telegram token；没有访问真实账户；没有执行下单、撤单、Trading Stop 或主动平仓。
