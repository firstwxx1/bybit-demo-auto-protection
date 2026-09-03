# OKX 模拟盘 GPT 自动止盈止损 + Telegram 推送

基于原 `okx-demo-protection` 项目改造，新增以下能力：

- **GPT 自动止盈止损**：每 30 分钟调用 GPT 分析持仓，自动在 OKX 模拟盘挂止盈+止损条件单
- **Telegram 推送**：每周期自动推送完整风险报告 + 止盈止损执行状态到 Telegram
- **GPT 止损建议**：GPT 返回 `stop_loss` 字段，经确定性校验后作为止损候选（而非固定比例）
- **SL-only 降级**：当止盈无效时，仍可只挂止损单

## 安全边界（不变）

- 只读 OKX V5 持仓 + 4H K 线
- 执行仅限模拟盘（`x-simulated-trading: 1`）
- 只挂 `reduceOnly` 条件单，永不开仓/加仓/反向
- 缓存持仓永不下单
- 连续异常触发持久化熔断
- GPT 止损必须通过方向、强平边界、杠杆距离校验

## 安装

```bash
cd /root/okx-demo-auto-protection
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

## 配置

```bash
cp .env.example .env
chmod 600 .env
# 编辑 .env 填入密钥
```

必须配置的环境变量：

```ini
# OKX 模拟盘
OKX_DEMO_API_KEY=你的key
OKX_DEMO_API_SECRET=你的secret
OKX_DEMO_PASSPHRASE=你的passphrase

# GPT 风险分析
RISK_MODEL_API_KEY=你的GPT key
RISK_MODEL_API_BASE=https://api.openai.com/v1
RISK_MODEL=gpt-5.6-sol

# Grok 新闻（可选，不配则跳过新闻）
GROK_API_KEY=你的grok key
GROK_API_BASE=https://api.x.ai/v1
GROK_MODEL=grok-4

# Telegram 推送
TELEGRAM_BOT_TOKEN=你的bot token
TELEGRAM_CHAT_ID=你的chat id

# 开启自动止盈止损下单
PROTECTION_EXECUTION_ENABLED=true
```

## 运行

### 单次运行（测试）

```bash
.venv/bin/python auto_runner.py
```

### 30 分钟定时运行

```bash
# 添加到 crontab
*/30 * * * * cd /root/okx-demo-auto-protection && .venv/bin/python auto_runner.py >> logs/auto-runner.log 2>&1
```

### 仅报告不下单（调试）

```bash
.venv/bin/python auto_runner.py --no-protection
```

### 跳过 Telegram 推送

```bash
.venv/bin/python auto_runner.py --no-telegram
```

### 离线 fixture 测试

```bash
.venv/bin/python auto_runner.py --fixture examples/eth-short.json --no-protection --no-telegram
```

## 工作流程

```
每 30 分钟
    │
    ▼
┌─────────────────────┐
│ 1. 读取 OKX 模拟盘持仓 │  (GET /api/v5/account/positions)
│    (实时或缓存)        │  缓存持仓永不下单
└─────────┬───────────┘
          │
          ▼
┌─────────────────────┐
│ 2. 获取 4H K 线       │  (GET /api/v5/market/candles)
│    获取 1m K 线       │
└─────────┬───────────┘
          │
          ▼
┌─────────────────────┐
│ 3. Grok 搜索新闻     │  (可选)
│    GPT 风险分析      │  返回 risk_level, confidence, recommendation,
│                     │  take_profit, stop_loss
└─────────┬───────────┘
          │
          ▼
┌─────────────────────┐
│ 4. 生成风险报告      │  确定性校验止损方向/强平/杠杆
│    (与原报告格式一致)  │  GPT 止损替代固定止损
│                     │  确定性校验止盈方向/Fib/RR
└─────────┬───────────┘
          │
          ▼
┌─────────────────────┐
│ 5. 提取校验后的 TP/SL │
│    TP+SL 均有效 → 挂 TP+SL 条件单
│    仅 SL 有效  → 挂 SL-only 条件单
│    均无效      → 仅报告
└─────────┬───────────┘
          │
          ▼
┌─────────────────────┐
│ 6. 对账已有保护单     │  匹配则跳过，参数变化先建新再撤旧
│    (reduceOnly)      │  熔断保护
└─────────┬───────────┘
          │
          ▼
┌─────────────────────┐
│ 7. Telegram 推送     │  完整报告 + 执行状态
└─────────────────────┘
```

## 与原项目的区别

| | 原 protection | auto-protection (本改造) |
|---|---|---|
| 止损来源 | 固定比例 (4.86%) | GPT 建议 + 固定兜底 |
| 止盈来源 | 固定比例或 Fib | GPT 建议 + ATR/Fib 程序兜底 |
| 执行触发 | 手动运行 protection_runner | 每 30 分钟自动 |
| Telegram | 无 | 每周期推送 |
| GPT 止损 | 不支持 | 支持 (stop_loss 字段) |
| SL-only 降级 | 不支持 | 支持 |

## 测试

```bash
.venv/bin/pytest -q
```

## 文件说明

| 文件 | 说明 |
|---|---|
| `auto_runner.py` | **新增** — 主入口：GPT分析→止盈止损→Telegram |
| `telegram_notifier.py` | **新增** — Telegram Bot API 推送 |
| `model_clients.py` | **修改** — GPT 增加 stop_loss 字段 |
| `okx_demo_risk_reporter.py` | **修改** — 报告支持 GPT 止损来源 |
| `protection_engine.py` | 未改 — 复用原有 reconcile_dynamic + build_stop_order |
| `protection_runner.py` | 未改 — 原有固定止损入口保留 |
| `live_reporter.py` | 未改 — 持仓读取 + K线获取 |
| `terminal_menu.py` | 未改 — 终端配置菜单 |
