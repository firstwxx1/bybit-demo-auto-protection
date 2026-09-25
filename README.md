# Bybit 主网 Demo Trading 自动风险保护

这是从原 OKX 版本迁移后的 Bybit 版本。它保留原有的风险报告、新闻与模型分析、MA/Fib 证据、4H/1m K 线、动态 TP/SL、SL-only、缓存安全门、熔断、主动平仓、Telegram、HTTP 服务、n8n 和部署文件，但交易所适配器已经全部改为 Bybit V5。

## 重要边界

- **只允许 Bybit Production/Mainnet 的 Demo Trading 模式**，请求地址固定为 `https://api-demo.bybit.com`。
- **不使用 Bybit Testnet**，也不使用真实主网交易地址 `https://api.bybit.com`；程序启动和每次请求都会拒绝这两个地址。
- 只读取已有的 `linear`、USDT 永续仓位，不开仓、不加仓、不反向开仓。
- TP/SL 通过 Bybit V5 `Set Trading Stop` 维护整仓保护；没有通过 n8n Code 节点拼接签名或直接下单。
- 主动平仓是独立开关，使用 `reduceOnly + closeOnTrigger + Market`，并且必须重新读取实时持仓、确认订单完全成交。
- 缓存持仓可以生成报告，但不能授权动态保护或主动平仓。
- `PROTECTION_EXECUTION_ENABLED` 和 `ACTIVE_CLOSE_EXECUTION_ENABLED` 默认都是 `false`；默认只报告、不修改 Demo 仓位。
- 本仓库不保存任何 API key、secret、Telegram token 或 n8n credential 绑定。

## Bybit Production Demo Trading 的准备

1. 登录 Bybit **主网**，不要进入 Testnet。
2. 在主网账户中切换到 **Demo Trading** 模式。
3. 在 Demo Trading 环境创建专用 API key/secret。不要把真实主网 API key 填到本程序的 `BYBIT_DEMO_*` 变量中。
4. 将 Demo Trading 的 key/secret 放在服务器环境变量或权限为 `0600` 的 `.env` 中，绝不要提交 Git。
5. 账户需要有 `linear` USDT 永续的 Demo 仓位，程序不会替你开仓。

Production Demo 和 Testnet 是两套不同环境：本项目选择的是主网接口上的 Demo Trading，而不是 Testnet。代码通过固定 host、`BYBIT_TRADING_MODE=demo`、实时持仓来源和 paper-only 信封共同限制执行边界。

> 如果所在地区或账户权限无法访问 Bybit Production Demo API，程序会得到网络/权限错误；这不应通过改成真实主网或 Testnet 地址来绕过。

## 安装

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Windows PowerShell 可使用：

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## 配置

推荐先复制以下模板到 `.env`，再填入自己的值。下面没有真实凭据：

```ini
BYBIT_DEMO_API_KEY=你的DemoTradingKey
BYBIT_DEMO_API_SECRET=你的DemoTradingSecret
BYBIT_API_BASE=https://api-demo.bybit.com
BYBIT_TRADING_MODE=demo
BYBIT_SETTLE_COIN=USDT

# 报告模型，可选；没有模型时固定风控仍会生成报告
RISK_MODEL_API_KEY=你的GPTKey
RISK_MODEL_API_BASE=https://api.openai.com/v1
RISK_MODEL=gpt-5.6-sol
GROK_API_KEY=你的GrokKey
GROK_API_BASE=https://api.x.ai/v1
GROK_MODEL=grok-4

# Telegram，可选；工作流默认不携带 credential 绑定
TELEGRAM_BOT_TOKEN=你的BotToken
TELEGRAM_CHAT_ID=你的ChatID

# 安全默认值：先保持 false
PROTECTION_EXECUTION_ENABLED=false
ACTIVE_CLOSE_EXECUTION_ENABLED=false

# 可选运行参数
POSITION_CACHE_PATH=state/last-successful-positions.json
PROTECTION_STATE_PATH=state/protection-state.json
PROTECTION_FAILURE_LIMIT=3
CACHE_MAX_AGE_SECONDS=3600
HTTP_TIMEOUT_SECONDS=20
FIXED_STOP_ENTRY_PCT=0.04855847842644323
FIXED_STOP_MAX_MARK_DISTANCE_PCT=0.015
FIXED_TAKE_PROFIT_MARK_PCT=
```

终端面板 `terminal_menu.py` 也会创建和保存 `.env`，保存时不回显 secret，并尝试设置 `0600` 权限。

## 运行方式

### 1. 默认报告模式（推荐先用这个）

```bash
python auto_runner.py --no-protection --no-telegram
```

或使用离线 fixture，不访问交易所：

```bash
python auto_runner.py --fixture examples/eth-short.json --no-protection --no-telegram
```

此模式会生成风险报告和保护候选，但不会调用 `Set Trading Stop`、订单创建或主动平仓接口。

### 2. 手动开启 Demo TP/SL 更新

完成报告、账户、symbol 和强平边界检查后，才考虑将下面变量改成 `true`：

```ini
PROTECTION_EXECUTION_ENABLED=true
ACTIVE_CLOSE_EXECUTION_ENABLED=false
```

然后运行：

```bash
python auto_runner.py --no-telegram
```

在执行模式下，程序仍然要求：

- `BYBIT_API_BASE=https://api-demo.bybit.com`
- `BYBIT_TRADING_MODE=demo`
- 持仓来自本次实时查询而不是缓存
- 报告中的 TP/SL 通过独立确定性校验
- candidate symbol 与实时持仓完全匹配
- 状态文件没有熔断

### 3. 固定保护兼容入口

```bash
python protection_runner.py
```

它保留旧程序的固定止损入口，但底层已改为 Bybit V5 `Set Trading Stop`。fixture 永远只返回 `FIXTURE_EXECUTION_BLOCKED`，不会执行。

### 4. 主动平仓

主动平仓不和动态 TP/SL 共用开关：

```ini
ACTIVE_CLOSE_EXECUTION_ENABLED=true
```

只有显式传入 `--active-close-execution`，并且实时 receipt 仍然有效时，才会进入主动平仓检查。缓存、持仓变更、方向变更、数量变更、部分成交、订单状态未知都会失败关闭或打开熔断。

## Symbol 与数量规则

- Bybit linear USDT 永续使用 `ETHUSDT`、`BTCUSDT` 这类 symbol，不使用 OKX 的 `ETH-USDT-SWAP` 写法。
- 数量使用 Bybit linear position 的合约数量（`size`），不是现货币数量。
- `positionIdx=0` 表示单向持仓；`1`/`2` 用于双向持仓的多/空方向。
- 当前适配器固定 `category=linear`、`settleCoin=USDT`，会跟随分页读取所有非零持仓。

## n8n：为什么选择 HTTP bridge + Python，而不是在节点里签名

工作流文件：

- `n8n/bybit-demo-risk-report.15-node-reconstructed.json`：18 节点的完整报告/保护编排。
- `n8n/bybit-demo-risk-report.template.json`：最小模板。
- `tests/verify_n8n_15_node.js`：离线验证报告证据链和失败关闭。

n8n 仍然保留调度、HTTP、Code、Telegram 等节点，但 n8n **不直接访问 Bybit，也不在 Code 节点中保存 HMAC 签名逻辑**：

1. `report_http_service.py` 只监听 `127.0.0.1:38635/report`，调用 `bybit_live_reporter.py` 生成报告。
2. `dynamic_protection_service.py` 只监听 `127.0.0.1:38636/protect`，校验实时报告信封后才可能调用保护引擎。
3. Python 负责 API timestamp/签名、host 白名单、分页持仓、K 线、TP/SL 方向与强平边界、缓存阻断、receipt、审计状态和熔断。
4. n8n 负责证据字段核对、HTML 转义、报告发送和流程编排。

这样比在 n8n Code 节点中复制 Bybit 签名和下单逻辑更安全：secret 只在 Python 进程环境中出现，关键规则只有一个实现，且可以在本地单元测试中失败关闭。工作流默认 `active=false`，Telegram 节点也保持 `disabled=true`；导入后要由人工配置 credential 并单独启用。

n8n 只需要访问两个 loopback 地址：

- `POST http://127.0.0.1:38635/report`
- `POST http://127.0.0.1:38636/protect`

## 控制面板一键部署（Ubuntu/Debian VPS）

支持 aaPanel/宝塔等控制面板提供的 **Ubuntu/Debian 服务器主机终端**。不要在 Docker 容器终端内运行；若面板终端当前不是 root，请先切换为 root。安装器会处理系统依赖、Python 虚拟环境、项目依赖、`.env`、systemd 服务和离线测试，无需手工创建这些文件。

### 首次安装

```bash
git clone https://github.com/firstwxx1/bybit-demo-auto-protection.git /opt/bybit-demo-risk-reporter
cd /opt/bybit-demo-risk-reporter
bash deploy/install_bybit_demo.sh
```

### 已安装项目的更新

```bash
cd /opt/bybit-demo-risk-reporter
git pull --ff-only
bash deploy/install_bybit_demo.sh
```

如果项目已克隆到其他路径，请把上面命令中的目录替换为实际路径。安装器会按项目实际路径生成服务配置。

### 在中文配置面板填写密钥

安装及测试结束后会打开中文配置菜单：

1. 选择 `1`，填写 **Bybit 主网 Production Demo Trading** API Key 和 Secret。输入密钥时不会回显。
2. 模型和 Telegram 是可选配置，分别选择 `2`、`3`；暂时不用可跳过。
3. 选择 `0` 退出配置面板。

`.env` 会限制为仅 root 可读写（权限 `0600`）。先保持 `PROTECTION_EXECUTION_ENABLED=false` 和 `ACTIVE_CLOSE_EXECUTION_ENABLED=false`。这是安全默认值：先只生成报告，不更新 TP/SL、不主动平仓。

安装器适用于 Ubuntu/Debian + systemd。它不会开放公网端口；两个 HTTP 服务都只绑定 `127.0.0.1`。

### 检查安装并发起只读报告

在服务器主机终端依次执行：

```bash
curl http://127.0.0.1:38635/healthz
curl http://127.0.0.1:38636/healthz
systemctl status bybit-demo-report-http.service --no-pager
systemctl status bybit-demo-dynamic-protection.service --no-pager
```

健康检查正常后，可以手动发起一次只读报告请求：

```bash
curl -sS -X POST http://127.0.0.1:38635/report \
  -H 'Content-Type: application/json' \
  --data '{"paper_only":true,"trading_mode":"demo"}'
```

如果 Demo Trading 账户尚无 USDT 线性永续仓位，报告可能提示没有持仓；程序不会替你开仓。

### 配置 n8n

确认两个本机服务正常后，在 n8n 导入服务器仓库中的工作流文件：

```text
/opt/bybit-demo-risk-reporter/n8n/bybit-demo-risk-report.15-node-reconstructed.json
```

先保持工作流关闭并手动测试。工作流里的 `127.0.0.1` 必须能访问运行 Python 服务的环境。如果 n8n 在 Docker 容器内，容器中的 `127.0.0.1` 指向容器本身，不是 VPS 主机；应先配置 n8n 到主机服务的安全、可达网络路径。不要为了连通而把 `38635` 或 `38636` 端口开放到公网。

## systemd 部署

推荐使用上面的 `deploy/install_bybit_demo.sh`，它会自动按当前仓库目录生成服务文件、配置环境文件并启动服务。若需要手动安装，请将 service 文件中的 `@APP_DIR@` 替换成仓库绝对路径后，再复制到 `/etc/systemd/system/`；服务读取仓库根目录的 `.env` 文件。

服务仅绑定 loopback，不要对公网开放 `38635` 或 `38636`。健康检查：

```bash
curl http://127.0.0.1:38635/healthz
curl http://127.0.0.1:38636/healthz
```

## 测试与离线校验

```bash
python -m unittest discover -s tests -p 'test_*.py' -v
python -m pytest -q
node --check tests/verify_n8n_15_node.js
node tests/verify_n8n_15_node.js \
  n8n/bybit-demo-risk-report.15-node-reconstructed.json \
  examples/bybit-demo-report-fixture.json
python -m compileall -q .
```

测试不会访问 Bybit，不会使用任何凭据，也不会执行下单、撤单、Trading Stop 或主动平仓。

## 文件说明

- `bybit_adapter.py`：Bybit V5 Demo client、HMAC、分页持仓、K 线、Trading Stop、reduce-only close order。
- `bybit_live_reporter.py`：实时持仓、1m/4H K 线、receipt、报告和主动平仓闸门。
- `bybit_risk_reporter.py`：MA/Fib、止损方向/强平边界、风险收益比和中文风险报告。
- `bybit_protection_core.py`：只读的 Fib 分段平仓候选评估。
- `dynamic_protection_service.py`：n8n 动态 TP/SL loopback bridge。
- `report_http_service.py`：n8n 报告 loopback bridge。
- `auto_runner.py`：GPT/Grok/固定风控与周期入口。
- `protection_runner.py`：固定保护兼容入口。
- `active_close_adapter.py`：独立主动平仓适配器和审计状态。
- `terminal_menu.py`：中文终端配置/状态面板。
- `deploy/`：systemd service 文件。
- `state/`、`logs/`、`.env`：运行时生成，已在 `.gitignore` 中排除。

## 凭据安全声明

本次迁移、测试和提交过程中没有读取、输出或复制任何 API key、secret 或 Telegram token，也没有访问真实主网账户。请继续把所有真实值放在运行环境中，不要写入 README、fixture、n8n JSON 或 Git 历史。
