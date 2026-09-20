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
  - **控制台凭据**（会话 Cookie，导入一次）：赠送拆分为 **API 赠送 / Step Plan**、套餐名称/到期/自动续费、Plan Credit 已用量、本月/累计消费、手机号/昵称
- 余额增量：与上次成功刷新对比，显示 Δ 变化
- 手动刷新模式：启动时刷一次首轮，之后由你点「立即刷新」（或 `query.bat`），不后台定时打上游
- 控制台凭据导入：在已登录浏览器 F12 → Network → 任一 `Dashboard/` 请求 → Copy as cURL → 粘贴即自动解析验证
- **会话自愈**：短效令牌（30 分钟）失效时自动用长效 Cookie 调 passport 刷新接口换新会话并持久化，正常情况下导入一次长期可用；自愈也救不活（长效 Cookie 过期）时卡片会提示重导
- 凭据存本地 `accounts.json`，页面只显示 Key 尾号/掩码手机号

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
