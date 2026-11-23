# Token Monitor

轻量 Windows 桌面小插件，用于监控你订阅的 coding plan 用量与 API 账户余额。

依赖：`pystray` + `Pillow` + `requests` + `pywebview`。用量卡片使用
Windows 自带的 WebView2 渲染。启动后驻留系统托盘，左键打开卡片。

## 快速开始

```powershell
cd "C:\Users\Admin\Documents\token-monitor"
pip install -r requirements.txt
python monitor.py
```

首次启动会自动从 `config.example.json` 复制一份 `config.json`。用文本编辑器打开 `config.json`，填入你的 API Key，保存后点右键菜单里的 "Refresh now" 即可。

## 交互

- **左键单击托盘图标**：打开用量卡片。卡片显示 provider、已用百分比、分段进度条、剩余百分比和重置时间。
- **关闭卡片**：右上角 ×、Esc、再点托盘图标，或点击屏幕其他位置。
- **卡片底部**：左侧刷新按钮，右侧 View details，打开百炼 Token Plan 订阅页。可用 provider 的 `details_url` 覆盖。
- **右键菜单**：Show usage、Refresh now、Reload config、Bailian login (browser)（仅配置百炼时）、Open config folder、Quit。菜单不再显示 provider 状态行。
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
| `type` | 内置类型：`openai` / `deepseek` / `openrouter` / `anthropic_admin` / `minimax_coding_plan` / `bailian_token_plan` / `generic` |
| `api_key` | Bearer Token（`generic` 也可以放在 `headers` 里） |

### 内置类型

- **openai**：Dashboard billing，返回 prepaid 余额与已用金额。
- **deepseek**：`/user/balance`，返回人民币余额。
- **openrouter**：`/api/v1/credits`，返回 credits 余额与总额。
- **anthropic_admin**：需 admin key，请求 `/v1/organizations/usage`。
- **bailian_token_plan**：阿里云百炼 Token Plan 的月度 Credits 用量。见下方专节。
- **minimax_coding_plan**：MiniMax Coding Plan 的 5 小时与每周额度。

### 阿里云百炼 Token Plan

百炼没有开放 `sk-sp-` API Key 的额度查询接口；`sk-sp-` 只用于模型调用。
本 provider 直接调用 `bl usage token-plan` 使用的同一个 Console 网关，
不依赖 Node 进程。鉴权有两种方式，推荐用 AccessKey 自动续期。

#### 方式一：AccessKey 自动续期（推荐）

配置阿里云 AccessKey 后，监控会调用 `GenerateCLIAccessToken` 换取临时
令牌。临时令牌约 20 分钟有效，监控在过期前自动刷新，不需要浏览器登录。

主账号的 AccessKey 可直接使用。RAM 用户需要同时满足：

- RAM 系统策略 `AliyunTokenPlanReadOnlyAccess`
- RAM 系统策略 `AliyunBSSReadOnlyAccess`
- 自定义策略允许 `modelstudio:GenerateCLIAccessToken`
- 百炼控制台「账号管理」里的管理员或订阅套餐权限

自定义策略内容：

```json
{
  "Version": "1",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "modelstudio:GenerateCLIAccessToken",
      "Resource": "*"
    }
  ]
}
```

建议新建只读 RAM 用户，不要使用带写权限的密钥。AccessKey 只发送到
`modelstudio.<region>.aliyuncs.com`。

配置方式二选一。交给百炼 CLI 保存：

```powershell
bl auth login --open-api --access-key-id <AccessKey ID> --access-key-secret <AccessKey Secret>
```

或者直接写进 `config.json`：

```json
{
  "name": "Bailian Token Plan",
  "type": "bailian_token_plan",
  "access_key_id": "LTAI...",
  "access_key_secret": "..."
}
```

#### 方式二：Console 会话（会过期）

不配置 AccessKey 时，回退到 `bl auth login --console` 写入的会话。
该会话约 24 小时过期，过期后需要重新登录：

```powershell
npm install -g bailian-cli
bl auth login --console
```

过期时卡片显示
`console session expired; run: bl auth login --console`，并给出 Login 按钮。
配置了百炼 provider 时，托盘右键菜单也会多出 `Bailian login (browser)`。
点它在后台执行 `bl auth login --console`，浏览器登录完成后会自动重载配置
并刷新。也可以手动重新登录后点 "Reload config"；"Refresh now" 在失败
节流期内只会减少等待计数。

#### 字段

| 字段 | 说明 |
| --- | --- |
| `access_key_id` / `access_key_secret` | 阿里云 AccessKey，配置后自动续期 |
| `security_token` | STS 临时凭证的 token，可选 |
| `access_token` | 直接指定 Console 令牌，优先于 `~/.bailian/config.json` |
| `console_region` | `cn-beijing`（默认）或 `ap-southeast-1` |
| `console_site` | `domestic`（默认）或 `international` |
| `bailian_config` | 自定义 `bl` 配置路径 |

卡片显示当月已用百分比、剩余百分比和重置倒计时；如果接口返回多个
配额窗口，其他窗口会在主区块下方列出。

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
卡片详情里。Popcorn 的 usage 接口可直接用同一个请求：
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

该 provider 仍会正常抓取，只是不参与 "最坏状态" 计算，也就不会影响
托盘图标的状态点与柱状比例。卡片优先显示带窗口数据的主 provider。

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

进入节流后，卡片会显示该 provider 的错误。点 "Reload config" 会清空
所有 provider 的节流状态（相当于一次干净的 "全部重试"）。


## 打包为单个 exe（可选）

如果不想留 Python 环境，可以用 PyInstaller 打包：

```powershell
pip install -r requirements.txt pyinstaller
python -m PyInstaller TokenMonitor.spec --noconfirm
```

输出在 `dist\TokenMonitor.exe`。PyInstaller 会把 pywebview 和 WebView2
所需的 .NET 组件打进 exe；目标机仍需要 Microsoft Edge WebView2
Runtime（Windows 10/11 通常自带）。

## 常见问题

- **托盘没反应**：Windows 11 下默认会隐藏未识别图标，右键任务栏上 "隐藏的图标" 小右键菜单里可以调出。也可能是刚启动，等几秒再点。
- **卡片报错**：左键打开卡片看 Connection 区块；多数是 key 填错、接口路径变了，或百炼 Console 会话过期。百炼过期可直接点卡片里的 Login。
- **卡片打不开**：确认系统有 Microsoft Edge WebView2 Runtime。Windows 10/11 通常自带；缺失时安装 Evergreen Runtime 后重启程序。
- **只看到最坏的状态**：托盘图标只显示最坏状态；完整数据在左键卡片里。
