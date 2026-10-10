# 阶跃星辰多账号余额看板 (StepFun)

轻量级多账号余额监控看板，用于[阶跃星辰开放平台](https://platform.stepfun.com)的多账号余额查询。

纯 Python 标准库实现，**零第三方依赖**。单文件后端 + 单文件前端。
项目结构参考了基元律动余额看板（tokenrhythm-balance-dashboard），上游改为阶跃星辰**官方公开 API**：

```
GET https://api.stepfun.com/v1/accounts
Authorization: Bearer <API Key>
→ { type, balance, total_cash_balance, total_voucher_balance }
```

## 功能

- 双数据源合并展示：
  - **API Key**（官方公开 API `/v1/accounts`，免登录）：余额、充值总额、赠送总额
  - **控制台凭据**（会话 Cookie，导入一次）：赠送拆分为 **API 赠送 / Step Plan**、套餐名称/到期/自动续费、套餐额度、本月/累计消费、手机号/昵称/实名状态
- **两种套餐额度模型都支持**（字段取自控制台 `QueryStepPlanRateLimit`，按字段实际有无自适应）：
  - **Token Plan**（`plan_family=2`，现行 Plus/Pro/Max，2026-10-14 前购买）：**Credit 月池** —— 如 Plus 为 1600M/月，按月发放、月末清零，另有加油包单独 30 天到期；显示本周期已用/剩余与**重置时间**
  - **老 Coding Plan**（`plan_family=1`）：**5 小时滚动 + 周** 双限额，显示两档剩余比例与各自重置时间
  - **2026-10-14 起的新规则**：官方通知 Credit 额度由月限额改为**周限额** —— 看板不需要改，字段有值就画（详见下节）
  - 未启用那套字段接口会返回 0，看板据此只画真正生效的那套
- 余额增量：与上次成功刷新对比，显示 Δ 变化
- 手动刷新模式：启动时刷一次首轮，之后由你点「立即刷新」（或 `query.bat`），不后台定时打上游
- 控制台凭据导入：在已登录浏览器 F12 → Network → 任一 `Dashboard/` 请求 → Copy as cURL → 粘贴即自动解析验证
- **会话自愈**：短效令牌（30 分钟）失效时自动用长效 Cookie 调 passport 刷新接口换新会话并持久化，正常情况下导入一次长期可用；自愈也救不活（长效 Cookie 过期）时卡片会提示重导
- 凭据存本地 `accounts.json`，页面只显示 Key 尾号/掩码手机号

## 套餐额度与时间口径（2026-10 实测 + 2026-10-14 变更预告）

阶跃把几种限额模型放在**同一个** `QueryStepPlanRateLimit` 响应里，用 `plan_family` 区分；未启用那套字段返回 0 / `reset_time="0"`。**看板按「字段有没有值」自适应渲染，不写死任何一套口径。**

> ⚠️ **2026-10-14 起规则要变** —— 官方 2026-10-09 控制台站内通知《Step Plan 调整通知》（两个号都收到，原文摘录）：
> 我们将于 2026 年 10 月 14 日起增加 Step Plan 供应……调整额度与档位：**Step Plan 的 Credit 额度由月限额调整为周限额**，并精简套餐档位。……**在 2026 年 10 月 14 日之前购买 Step Plan 的用户，可按原套餐权益使用至当前订阅期限结束**。
> （同通知还提到：Step 5 Preview 的 API 价格微调并引入峰谷时段定价；季付/年付折扣力度降低。）

| 维度 | 2026-10-14 前购买的 Token Plan（含本机两个号） | 老 Coding Plan | 2026-10-14 起新购 / 续费 |
|---|---|---|---|
| `plan_family` | 2 = TOKEN | 1 = CODING | 预计 3 = TOKEN_V3（枚举里已存在） |
| 限额模型 | **Credit 月池**：按月发放、月内任意时段消耗、月末清零（加油包独立 30 天） | **5 小时滚动 + 周** 双限额 | **Credit 周限额**（按周给额度、按周清零；具体字段以届时接口为准） |
| 关键字段 | `plan_credit_rate_limit.subscription_credit_left_rate` / `_reset_time`、`credit_buckets[]`（`type=1` 月池、`2` 加油包） | `five_hour_usage_left_rate` / `_reset_time`、`weekly_usage_left_rate` / `_reset_time` | 未知：走 `weekly_usage_*`，或带「周」`next_reset_at` 的 `credit_buckets` 都有可能 |
| 官方说法 | 「Credit 月池：额度按月发放，月内任意时段消耗，月末清零」 | 「按请求次数（Prompt）……5 小时 / 周限额」 | 「Credit 额度由月限额调整为周限额」 |

- **本机两个号的现状**（2026-10-10 实测）：`plan_family=2`，5h/周 两套字段恒为 0、重置时间 `"0"`（未设置）；真正生效的是 Credit 月池 = 1600M/月（Plus 档，具体额度以接口返回为准），`next_reset_at` 均为 10-20（开通日 + 30 天）。两号 12-04 到期、未开自动续费 → **到期后按新规则购买，届时才是周限额**。
- 渲染规则（`index.html`）：`five_hour_*` / `weekly_*` 有值 → 画「5 小时剩余 / 每周剩余」两条；有 `credit_buckets` → 画「本周期已用 / 总 / 剩余 % + 重置时间」——周期长短由接口的 `next_reset_at` 决定，月/周都画得对。
- 控制台前端是同一套判断（protobuf `QueryStepPlanRateLimitResponse`：字段 3~6 = 5h/周，10~11 = Credit）：`planFamily===TOKEN ? Credit + 加油包 : 5 小时用量 + 每周用量`。
- 官方通知说「具体价格、各档位额度、峰谷时段将在正式实施前公布」→ **10-14 之后拿一个按新规则购买的号复测一次**，看新额度落在哪套字段上。
- 用量记录 `QueryStepPlanUsages` **默认只回第一页 10 条**，必须显式带 `page`/`page_size` 翻页；本看板翻全页求和 = 本订阅周期累计（与 `credit_total − credit_residual` 对得上，误差 <1‰），另单列"今日"。
- 实名状态取 `UserInfo.real_name_status`：`1=未实名 2=已实名 3=已注销实名`（枚举见控制台前端 protobuf；未实名账号在平台侧受限，看板只如实显示）。

## 快速开始 · 两种用法

**按需查询（推荐，平时零进程）**：

```bash
python query.py     # 或双击 query.bat
```

跑一次：查询全部账号 → 控制台打印汇总表 → 生成静态报表 `report.html`（双击即可在浏览器查看，数据内嵌、无需服务）。已加入 .gitignore（内含完整密钥）。

**看板模式（需要增删账号/导入凭据时再开）**：

```bash
python server.py    # 或双击 start-dashboard.bat → http://127.0.0.1:9156
```

控制台凭据（会话 Cookie）寿命有限（实测约几十分钟，且只有刚导完有效），所以控制台数据按「导入后立即拉一次快照」的节奏使用：导入 cURL → 自动刷新拿全量数据 → 之后靠 query.py 拉官方 API 基础余额；会话过期重导一次即可。

### 配置账号

在网页上直接添加（备注名 + API Key），或编辑 `accounts.txt`，每行一个：

```
备注名 sk-xxxxxxxxxxxxxxxx
sk-xxxxxxxxxxxxxxxx
```

Key 在阶跃控制台「账户管理 → 接口密钥」创建；组织空间项目下创建的 Key 计费走组织 Credit。

## 项目结构

```
server.py              # 后端：HTTP 服务 + 官方API/控制台双源封装 + 会话自愈
index.html             # 前端：单文件页面（余额卡片 / 套餐额度条 / 密钥复制）
query.py               # 一次性查询 CLI：打印汇总表 + 生成静态报表 report.html
accounts.json          # 账号与凭据（本地生成，不提交仓库）
data.json              # 查询结果缓存（运行时自动生成）
console_session.log    # 会话自愈观测日志（运行时自动生成）
start-dashboard.bat    # Windows 一键启动看板
query.bat              # Windows 一键查询（免服务）
```

## 与参考项目（基元律动看板）的差异

| 维度 | 基元律动 | 阶跃星辰 |
|---|---|---|
| 鉴权 | 网页 session（sess_ + Cookie），会过期需重抓 | 官方 API Key，长期有效 |
| 余额接口 | /api/wallet/summary（内部接口） | /v1/accounts（官方公开） |
| 用量统计 | /api/usage-summary + 按 Key 按模型聚合 | 官方未提供，看板不含 |
| Key 管理 | 可经网页 session 创建/删除 Key | 控制台操作，看板不含 |

## 安全说明

- 凭据文件 `accounts.txt` 为本地文件，请勿提交到仓库（.gitignore 已排除）
- 服务默认监听 `0.0.0.0:9156`，公网部署请配合反向代理 + 认证，或改为只绑内网
- 本工具仅调用阶跃星辰官方 API，不涉及破解或逆向

## License

[MIT](./LICENSE)
