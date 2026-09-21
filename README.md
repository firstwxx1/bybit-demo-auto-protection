# Bybit 主网 Demo 自动止盈止损

这是一个面向 **Bybit 主网 Demo Trading** 的已有仓位保护程序，使用 Bybit V5 API。

## 运行边界

- 只允许访问 `https://api-demo.bybit.com`。
- 明确拒绝 Bybit 真实主网和 Testnet 地址。
- 当前实现面向 `linear` USDT 永续合约。
- 只处理已经存在的实时仓位；缓存持仓禁止触发保护更新。
- 只调用 `Set Trading Stop` 更新该仓位的 TP/SL，不开仓、加仓或反向交易。
- `PROTECTION_EXECUTION_ENABLED` 默认关闭；首次运行应使用报告模式。

## 安装

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

## 配置

API Key 必须在 Bybit 主网登录后切换到 Demo Trading，再为 Demo Trading 创建。不要提交 `.env`。

```ini
BYBIT_DEMO_API_KEY=你的DemoTradingKey
BYBIT_DEMO_API_SECRET=你的DemoTradingSecret
BYBIT_API_BASE=https://api-demo.bybit.com
BYBIT_SETTLE_COIN=USDT

RISK_MODEL_API_KEY=你的GPTKey
RISK_MODEL_API_BASE=https://api.openai.com/v1
RISK_MODEL=gpt-5.6-sol

# 可选
GROK_API_KEY=你的GrokKey
GROK_API_BASE=https://api.x.ai/v1
GROK_MODEL=grok-4
TELEGRAM_BOT_TOKEN=你的BotToken
TELEGRAM_CHAT_ID=你的ChatID

# 默认 false。完成报告和人工检查后才考虑开启。
PROTECTION_EXECUTION_ENABLED=false
```

## 运行

报告模式：

```bash
.venv/bin/python auto_runner.py --no-protection --no-telegram
```

使用离线 fixture：

```bash
.venv/bin/python auto_runner.py --fixture examples/eth-short.json --no-protection --no-telegram
```

启用 Demo Trading 保护更新前，必须确认环境中的 `BYBIT_API_BASE` 仍为 `https://api-demo.bybit.com`，并确认 API Key 属于 Demo Trading。程序本身会再次拒绝非 Demo 地址。

## 测试

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
```

适配器测试不发送网络请求，覆盖签名、地址锁定、持仓字段归一化和 TP/SL 方向校验。完整 pytest 测试也可在安装依赖后运行：

```bash
.venv/bin/pytest -q
```

## 主要文件

- `auto_runner.py`：GPT 风险分析、确定性校验和周期入口。
- `bybit_adapter.py`：Bybit Demo V5 签名、持仓和 Trading Stop 客户端。
- `bybit_live_reporter.py`：实时持仓/K 线读取及过期缓存保护。
- `bybit_risk_reporter.py`：Bybit 线性合约报告和确定性风控计算核心。
- `bybit_protection_core.py`：只读、paper-only 的 Fib 分段平仓候选评估，不执行交易。
- `model_clients.py`、`telegram_notifier.py`：可选的模型分析和 Telegram 推送。
