# Token Monitor

轻量 Windows 桌面小插件，用于监控你订阅的 coding plan 用量与 API 账户余额。

依赖：`pystray` + `Pillow` + `requests`。不需要 GUI 框架。启动后驻留系统托盘，内存占用不到 30MB。

## 快速开始

```powershell
cd "C:\Users\Admin\Documents\token monitor"
pip install -r requirements.txt
python monitor.py
```

首次启动会自动从 `config.example.json` 复制一份 `config.json`。用文本编辑器打开 `config.json`，填入你的 API Key，保存后点右键菜单里的 "立即刷新" 即可。

## 交互

- **悬浮提示**：鼠标悬在托盘图标上可看到多行 tooltip，包含每个 provider 的名称、值与状态色点，末行会标注 "last update: 30s ago" 之类的相对时间，方便判断数据新鲜度。
- **右键菜单**：顶部是每个 provider 的状态项（不可点，仅用于查看），接下来是 "立即刷新"、"重载配置"、"打开配置目录"、"退出"。改完 `config.json` 不必重启进程，点 "重载配置" 即可。
- **托盘图标**：透明背景的 Windows 原生风格线性图标，根据最坏状态的 provider 动态重绘。百分比类 provider 直接显示用量；余额类 provider 如果配置了 `monthly_budget`，会按“预算 - 当前余额”计算已用比例。

状态颜色轮换：

- 白/灰图标 — 用量 < 60%，不额外强调
- 黄色小点 — 60% ≤ 用量 < 85%
- 红色小点 — 用量 ≥ 85%
- 灰色空状态 — 未获取到任何数据

## 配置说明

`config.json` 顶层字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `refresh_interval_seconds` | int | 后台刷新间隔，默认 300 |
| `request_timeout_seconds` | int | 单次请求超时，默认 10 |
| `providers` | array | 要监控的 provider 列表 |

每个 provider 公共字段：

| 字段 | 说明 |
| --- | --- |
| `name` | 显示名 |
| `type` | 内置类型：`openai` / `deepseek` / `openrouter` / `anthropic_admin` / `generic` |
| `api_key` | Bearer Token（`generic` 也可以放在 `headers` 里） |

### 内置类型

- **openai**：Dashboard billing，返回 prepaid 余额与已用金额。
- **deepseek**：`/user/balance`，返回人民币余额。
- **openrouter**：`/api/v1/credits`，返回 credits 余额与总额。
- **anthropic_admin**：需 admin key，请求 `/v1/organizations/usage`。

### 通用 generic 类型

适配任何返回 JSON 的接口。百分比用法：

```json
{
  "name": "Cursor 订阅",
  "type": "generic",
  "url": "https://api.example.com/v1/usage",
  "method": "GET",
  "headers": { "Authorization": "Bearer xxx" },
  "used_path": "data.used",
  "limit_path": "data.limit",
  "format": "percent"
}
```

余额用法：

```json
{
  "name": "某服务",
  "type": "generic",
  "url": "https://api.example.com/account",
  "headers": { "Authorization": "Bearer xxx" },
  "value_path": "balance",
  "format": "currency",
  "currency": "USD"
}
```

`format` 可选：`percent` / `currency` / `number`。
`used_path` + `limit_path` 优先，会自动计算百分比。
路径支持点记号嵌套与数组下标，如 `data.items.0.value`。

如果接口返回的是余额，但需要用月度预算衡量托盘图标里的柱状比例，可以加
`monthly_budget`。例如预算为 30 美元、当前余额为 9 美元时，已用比例为
`(30 - 9) / 30 = 70%`。

```json
{
  "name": "某服务",
  "type": "generic",
  "url": "https://api.example.com/account",
  "headers": { "Authorization": "Bearer xxx" },
  "value_path": "balance",
  "format": "currency",
  "currency": "USD",
  "monthly_budget": 30
}
```

如果接口余额与实际可用余额有固定偏差，可以加 `offset` 做矫正。`offset`
只作用在 `value_path` 这一类直接取值的配置上，负数表示从接口值里扣掉。
界面只显示矫正后的余额，不显示 offset 审计文本：

```json
{
  "value_path": "balance",
  "format": "currency",
  "currency": "USD",
  "offset": -3.72
}
```

如果同一个响应里还有今日花费，可以用 `today_cost_path` 抽取并显示到
tooltip 和右键菜单详情里。Popcorn 的 usage 接口可直接用同一个请求：
`GET https://api-us.popcorn.wiki/v1/usage`，今日花费路径是
`usage.today.cost`。如果接口返回的是 standard cost，可以用 `today_cost_rate`
做费率折算，例如 `6.1061 * 0.125 = 0.7633`，界面显示折算后的金额。

```json
{
  "value_path": "remaining",
  "format": "currency",
  "currency": "USD",
  "offset": -3.72,
  "today_cost_path": "usage.today.cost",
  "today_cost_rate": 0.125
}
```

### 告警阈值

货币类 provider 默认余额 `<= 1.0` 时触发红色告警，对 OpenRouter credits 这类
余额本来就很小的服务常常是误报。可以在 provider 配置里加一个 `alert_below`
字段覆盖阈值，例如：

```json
{
  "name": "OpenRouter",
  "type": "openrouter",
  "api_key": "sk-or-xxx",
  "alert_below": 20.0
}
```

`alert_below` 只对货币类（`USD` / `CNY` / ...）生效；不填时按 1.0 算。

### 只看不参与托盘颜色（informational）

如果你有 5 个 provider，其中某个余额常年是 $0 但并不想让它把托盘图标染成红，
可以给它加 `informational: true`：

```json
{
  "name": "Anthropic 老号",
  "type": "anthropic_admin",
  "admin_key": "sk-ant-xxx",
  "informational": true
}
```

该 provider 仍会正常抓取，照常出现在悬浮提示和右键菜单里，只是不参与
"最坏状态"计算，也就不会影响托盘图标的状态点与柱状比例。

### 失败节流

如果某个 provider 一直报错（鉴权失败、接口 5xx 等），默认会每 5 分钟重试一次，
既浪费请求对失败服务也不友好。可以在 provider 配置里加 `failure_pause_cycles`
（默认 5），表示「连续失败时跳过多少轮再重试一次」：

```json
{
  "name": "老服务",
  "type": "generic",
  "url": "https://api.example.com/usage",
  "headers": { "Authorization": "Bearer xxx" },
  "value_path": "balance",
  "format": "currency",
  "currency": "USD",
  "failure_pause_cycles": 10
}
```

进入节流后，右键菜单里会显示 `Provider: paused (1/10 fails)  |  <last error>`，
悬浮提示里显示 `○ Provider: paused (1/10 fails) ERR <last error>`。
点 "重载配置" 会清空所有 provider 的节流状态（相当于一次干净的"全部重试"）。


## 打包为单个 exe（可选）

如果不想留 Python 环境，可以用 PyInstaller 打包：

```powershell
pip install pyinstaller
pyinstaller --noconsole --onefile --name TokenMonitor monitor.py
```

输出在 `dist\TokenMonitor.exe`。

## 常见问题

- **托盘没反应**：Windows 11 下默认会隐藏未识别图标，右键任务栏上 "隐藏的图标" 小右键菜单里可以调出。也可能是刚启动刚完成首次拉取，等几秒即可。
- **provider 报 ERR**：点右键菜单看具体错误信息；多数是 key 填错或接口路径变了。
- **只看到最坏的状态**：托盘图标的设计就是一眼警示 "有东西要装上了"，悬浮 tooltip 和右键菜单里都有全量。
