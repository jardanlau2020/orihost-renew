# Orihost 免费服务器自动续期 / 巡检

基于 Jexactyl 面板的自动续期脚本，解决免费容器 7 天过期删机问题。
核心：`remember_web` 长效 token 自动置换 session + XSRF，不用频繁手动更新 Cookie。

> **当前状态（重要）**：面板的 `Claim Renewal` 接口强制要求 Cloudflare Turnstile
> token，GitHub Actions 的 runner IP 实测过唔到（8 轮 8 次全败，挂代理亦无用）。
> 所以**排程默认只做 watchdog**：读面板剩余天数，剩 ≤7 天发 Telegram 提醒，续期
> 动作留给人手。真撳续期只能手动 `workflow_dispatch` 选 `mode=renew`，且预期红。

## 文件结构

```text
orihost-renew/
├── orihost_browser_renew.py    # 主力：浏览器版（Cookie免登+读文章+Turnstile+Claim），
│                               #       已迁移到 renew-kit（见下「renew-kit 迁移」）
├── orihost_renew.py            # 备用：纯 API 版（仅满额检测/诊断，claim 过唔到验证）
├── requirements.txt            # 依赖：curl_cffi + requests + seleniumbase
├── scripts/setup_proxy.sh      # 代理引导：装 sing-box → 真探测出口 → 写 GITHUB_ENV
├── .github/workflows/renew.yml # Actions：每 3 天 watchdog 巡检 + 手动触发
├── .verify/verify_orihost.py   # 离线验收 harness（188 项断言，不碰真浏览器）
├── .verify/probe_cron_writeback.py  # 端到端探针：真跑 cron 回写（打桩 GitHub API）
└── README.md                   # 本说明文件
```

## renew-kit 迁移

环境变量读取、结果语义、报告排版、TG 通知、退出码这些通用部分交给
[`renew-kit`](https://github.com/jardanlau2020/renew-kit)（钉 `v0.4.2`），
workflow 里只留一个 composite action 调用。本仓自己保留业务逻辑。

行为变化（每条都有据）：

| 项 | 迁移前 | 迁移后 |
|---|---|---|
| 退出码 | `1`（没账号/读唔到）、`2`（renew 失败）、`0` 三档 | 只有 `FAILED → 1`，其余 `0`。三档对 workflow 来说都是「非零」，分不出轻重 |
| TG 粒度 | **每台一条**（3 台 = 3 条） | **整轮一条**（`RenewReport` 统一排版），同轮信息不再被拆散 |
| watchdog 常态 | `exit 0` + 每台一条 TG | 映射成 `SKIPPED`，**静默**（每 3 天一次巡检，天天发就是噪音） |
| 需人手續期 | `exit 0` + 每台一条 TG | 映射成 `UNKNOWN`，**发** TG 但**不**标红（这是预期内状态，不是失败） |
| `TG_BOT` 兼容写法 | 支持 `chat_id,token` | **取消**。`renewkit.notify` 只认 `TG_BOT_TOKEN`/`TELEGRAM_TOKEN` + `TG_CHAT_ID`/`TELEGRAM_CHAT_ID` |
| 死变量 | `RENEWAL_MAX` / `MAX_ATTEMPTS` / `DWELL_EXTRA` | 已从 workflow 与本文档移除（脚本 0 引用） |

**状态字符串保留为内部协议**：`renew_one_server` / `watchdog_one` 返回的
`"✅ 正常"` / `"⏰ 需人手續期"` / `"⏭️ 跳过"` / `"❌ ..."` 一个字节没动，只在报告
边界由 `_outcome_of()` 映射成 `Outcome`。这样 1300 行浏览器逻辑不用碰 —— 迁移的
风险面就只有文件头和 `main()`。

映射表：

| 内部状态字 | Outcome | 发 TG | 标红 |
|---|---|---|---|
| `✅ 续期成功` | `RENEWED` | ✅ | ❌ |
| `✅ 正常`（watchdog，剩 > 7 天） | `SKIPPED` | ❌ | ❌ |
| `⏰ 需人手續期`（剩 ≤ 7 天） | `UNKNOWN` | ✅ | ❌ |
| `⚠️ 未知结果` | `UNKNOWN` | ✅ | ❌ |
| `⏭️ 跳过`（已达上限） | `ALREADY_MAX` | ❌ | ❌ |
| `⏭️ 跳过`（冷却中 / 未到窗口） | `SKIPPED` | ❌ | ❌ |
| `❌ 狀態讀取失敗` / `❌ 续期失败` | `FAILED` | ✅ | ✅ |

## 续期原理

面板前端扒出来的真实流程（`assets/bundle.*.js`）：

1. 点 `Renew Now` → `POST /api/client/servers/{id}/renew/begin` 返回文章链接 + `dwell_seconds`
2. 点 `Read Article` 新标签读文章（提前关闭会被警告），面板内倒计时
3. 倒计时走完出 Cloudflare Turnstile，必须点过验证，`Claim Renewal` 按钮才可点
4. 点 `Claim Renewal` → `GET /api/client/renewal/complete?cf-turnstile-response=xxx` 完成续期（+7 天）

结论：`complete` 强制要 Turnstile token，无 token 直接 500，所以主力跑**浏览器版**
（真浏览器点验证，移植自 katabump 的过盾方案）；纯 API 版保留作满额检测和诊断用。

**为什么排程唔自动续**：GHA runner IP 过唔到 Turnstile（见顶部状态说明）。第 3 步
卡住 → 第 4 步点唔到 → 只会白红一轮。所以排程退到 watchdog。

## 一、获取 remember token（填的是令牌，不是邮箱密码）

> 脚本不需要你的邮箱和密码，只需要登录态令牌。令牌失效了重新取一次即可，密码改了也不受影响。

1. 浏览器打开 `https://panel.orihost.com` 并登录（登录页如果有 `Remember me` 勾上）
2. 按 `F12` 打开开发者工具 → 顶部切到 `Application（Edge 显示“应用程序”）` → 左侧展开 `Cookies` → 点 `https://panel.orihost.com`
3. 右边列表里找到名字以 `remember_web_` 开头的那一行（后面跟一串 hash，如 `remember_web_59ba36...`）
4. 双击它的 `Value（值）` 那一格，全选复制（一长串无空格字符，几百个字符长度）。这就是要填的 `ORIHOST_REMEMBER`
5. 填的时候注意：只粘贴纯值，前后不要带空格、不要带引号、不要带 `remember_web_xxx=` 前缀（带了也能用，但纯值最稳）

格式长这样（已脱敏，只看形状，别照抄）：
- token：`eyJpdiI6...中间几百字符...In0=`，字母数字+符号组成，一整行无空格无换行
- 对错自查：长度几百字符、以 `eyJ` 开头是正常的；如果只有几十字符，你大概率复制的是别的 cookie，重找 `remember_web_` 开头那行

备选方法：`F12` → `Network（网络）` → 刷新页面 → 点任意 `activity` 请求 → `Request Headers` 里复制 `Cookie` 整段（脚本会自动从里面提取）。

## 二、获取服务器 ID

进面板点开你的服务器，看浏览器地址栏：

```text
https://panel.orihost.com/server/8651e616
                                  └─ 8 位短 ID，填这个就行 ─┘
```

多台用英文逗号分隔：`id1,id2`。
填完整 UUID（`670475f5-1206-...` 形如 8-4-4-4-12）也兼容，脚本会自动取前 8 位。

## 三、GitHub Actions 部署（推荐）

1. 新建仓库，把本目录文件推上去（保持 `orihost_browser_renew.py` 在仓库根目录，
   `scripts/setup_proxy.sh` 在 `scripts/` 下）
2. 进仓库 `Settings → Secrets and variables → Actions`，点 `Secrets` 页签 → `New repository secret`，按下表逐个建（保存后值不可见是正常的）：

   名字必须一字不差（大写+下划线），所有变量全部建在 `Secrets` 下。完整对照表：

| 名称 | 必填 | 说明 |
|---|---|---|
| `ORIHOST_REMEMBER` | 是 | 第一步拿到的 remember token 值 |
| `ORIHOST_SERVER_IDS` | 是 | 服务器短 ID（地址栏 `/server/` 后面那段），逗号分隔 |
| `TG_BOT_TOKEN` | 否 | Telegram 机器人 token（`TELEGRAM_TOKEN` 亦可） |
| `TG_CHAT_ID` | 否 | Telegram 聊天 ID（`TELEGRAM_CHAT_ID` 亦可） |
| `NODE_LINK` | 否 | 代理节点完整分享链接（vless/vmess/trojan/hysteria2/tuic/anytls/socks5），不填则直连 |
| `ORIHOST_PROXY` | 否 | 手动指定的 http(s)/socks 代理，如 `http://127.0.0.1:1081`；节点链接填 `NODE_LINK`，不要填这里 |
| `GH_ROTATE_TOKEN` | 否 | 有 `contents:write` 的 PAT。用于 ① 把轮换后的 remember token 写回 secret；② cron 自我調度回写 workflow。不填则这两项静默跳过，不影响续期 |

3. 去 `Actions → Orihost Auto Renew → Run workflow` 手动跑一次（mode 保持 `watchdog`），TG 能收到提醒即正常
4. 定时是 `0 10 */3 * *`（每 3 天，北京时间 18:00）。**这是巡检，不是续期** ——
   7 天有效期，每 3 天看一次，剩 ≤7 天就提醒你。

### 代理（sing-box）与 `NODE_LINK`

workflow 的 `setup-command` 会跑 `scripts/setup_proxy.sh`：调上游
`https://main.ssss.nyc.mn/setup_proxy.sh` 装 sing-box，然后**真经代理连一次
`api.ipify.org`** 验证出口（只看进程在不在不算数），最后把 `IS_PROXY` /
`PROXY_SERVER` 写进 `$GITHUB_ENV` 供续期步骤读取。

> ⚠️ `NODE_LINK` **必须**传到（workflow 的 `env` 里那一行）。上游 installer 的
> 入口是 `export NODE_LINK=${NODE_LINK:-''}` —— 取到空值就静默走「未配置代理，
> 直连模式」。直连时出口是 runner 机房 IP，面板前面的 Cloudflare 对这类 IP 更凶，
> watchdog 读天数都可能过唔到。`setup_proxy.sh` 第 0 步会为此打 `::warning::`。

优先级：`ORIHOST_PROXY`（显式指定）> `NODE_LINK`（sing-box）> 直连。
本地没有 sing-box 步骤，`NODE_LINK` 只在 Actions 里生效。

### cron 自我調度

`main()` 末尾会按「最紧急那台的到期日」把一条带 `# auto: renew-window=` 标记的
cron 行追加/替换到 workflow 里（走 Contents API，需要 `GH_ROTATE_TOKEN`）：

```yaml
on:
  schedule:
    - cron: '0 10 */3 * *'                                    # 基准巡检，永远保留
    - cron: '0 10 15 10 *'  # auto: renew-window=2026-10-15T10:00Z lead=7
```

设计要点：**追加而非改写**。基准线永不可变（否则一旦算错日期就静默死掉，而且
再冇任何 run 去修正它）；auto 行最多一条，表达式没变就不提交（否则每次 run 都
多一个 commit，repo 被自己刷爆）。

### 多账号

| 账号 | 在 Secrets 里建这两个 |
|---|---|
| 账号1 | `ORIHOST_REMEMBER_1` + `ORIHOST_SERVER_IDS_1` |
| 账号2 | `ORIHOST_REMEMBER_2` + `ORIHOST_SERVER_IDS_2` |
| 账号3 | `ORIHOST_REMEMBER_3` + `ORIHOST_SERVER_IDS_3` |

单账号用不带后缀的即可；多账号与单账号可混用，脚本会自动汇总。
旧变量名 `ORIHOST_COOKIE / ORIHOST_COOKIE_1 / ORI_COOKIE` 仍兼容（完整 Cookie 或裸 token 均可）。
**注意**：免登失败是账号级错误，报告里合成一条（目标名带受影响台数），不是每台各报一条。

## 四、本地运行（Windows）

```bat
pip install -r requirements.txt
set ORIHOST_REMEMBER=你的remember值
set ORIHOST_SERVER_IDS=你的服务器短ID
python orihost_browser_renew.py
```

多台 / TG / 代理（cmd 示例）：

```bat
set ORIHOST_SERVER_IDS=id1,id2
set TG_BOT_TOKEN=123:abc
set TG_CHAT_ID=123456789
set ORIHOST_PROXY=http://127.0.0.1:7890
set ORIHOST_MODE=watchdog
python orihost_browser_renew.py
```

纯 API 版（只诊断，过唔到 claim 验证）：

```bat
python orihost_renew.py
```

本地要走代理请填 `ORIHOST_PROXY`（需是本机能连上的 http/socks 代理）。

## 五、环境变量全表

| 变量 | 默认 | 说明 |
|---|---|---|
| `ORIHOST_MODE` | `watchdog` | `watchdog` = 只读天数 + 到期提醒；`renew` = 真撳续期（GHA 过唔到 Turnstile，预期红） |
| `ORIHOST_WATCH_DAYS` | `7` | watchdog 的提醒阈值：剩 ≤ 该天数就报 `⏰ 需人手續期` |
| `ARTICLE_WAIT` | `30` | 文章页停留秒数（面板 dwell=15，多留 buffer，过早关闭会被警告） |
| `CLAIM_TIMEOUT` | `150` | 等 Claim 按钮可点的轮询上限（秒） |
| `ORIHOST_PROXY` | 空 | 显式代理，优先级最高；`ORIHOST_GOST_PROXY` 同效 |
| `NODE_LINK` | 空 | 节点分享链接，只在 Actions 里由 `setup_proxy.sh` 转成本地代理 |
| `DRY_RUN` | 空 | `1`/`true` 时演练：不点击续期、不回写 cron |
| `TG_BOT_TOKEN` / `TG_CHAT_ID` | 空 | Telegram 通知；`TELEGRAM_TOKEN` / `TELEGRAM_CHAT_ID` 同效 |

## 六、常见问题

- **419 / session 刷新失败**：`remember_web` 已失效，重新登录按第一步重取 token 更新到 Secrets
- **401 未认证**：同上，多为 token 填错（多了空格或只复制了一半）
- **skipped / 已达上限**：正常现象，本周期续满了，下个周期 Actions 会再续
- **面板显示 Renew Limit Reached / complete 报 500**：续期次数已满（免费服常见上限 7 次），脚本会自动判为跳过；等天数消耗、空出次数后定时任务会自动再续，不用管
- **TG 收到「⏰ 需人手續期」**：这是**正常提醒**，不是失败 —— 去面板手动点 Renew 即可。本轮 job 不会标红
- **TG 收不到**：先确认 `TG_BOT_TOKEN` 与 `TG_CHAT_ID` 都填了，且机器人已和你开过会话（先给机器人发一句话）
- **被 Cloudflare 拦截**：把节点链接填到 `NODE_LINK` 走代理，或换个时间手动重跑
- **日志出现 `NODE_LINK 未传入` 警告**：workflow 的 `env` 少了 `NODE_LINK: ${{ secrets.NODE_LINK }}`，本次是直连出口
- **job 标红但只有「❌ 狀態讀取失敗」**：面板抖了或代理死了，看同轮日志的出口 IP；重跑即可

## 七、离线验收

不碰真浏览器、不发真网络请求：

```bash
PYTHONPATH=../_deps python .verify/verify_orihost.py          # 188 项断言
PYTHONPATH=../_deps python .verify/probe_cron_writeback.py    # cron 回写端到端
```

`verify_orihost.py` 覆盖：状态字→Outcome 映射矩阵、token 三形态解析、多账号加载、
代理优先级、`run_all()` / `main()` 退出码场景矩阵（含「watchdog 常态静默」
「⏰ 不标红」「读唔到标红」「免登失败合成一条」）、workflow↔代码 env 一致性、
`setup_proxy.sh` 契约，并会**调用**下面那个探针。

`probe_cron_writeback.py` 用真实函数 + 打桩的 Contents API 跑通 cron 自我調度：
auto 行写对、基准行不被破坏、同日重跑不刷 commit、换到期日会更新。
全程在临时目录里操作，不碰仓库真文件 —— 因为这是本仓唯一「脚本反向改写自己
workflow」的路径，也是最容易静默弄丢的一条（基准 cron 行没了，调度会一声不响
地死掉，而续期本身完全正常）。

## 安全提醒

- token 等同于登录态，只放 GitHub Secrets，不要提交到代码里
- 本仓库为公开仓库，切勿把 token / UUID 写进代码、README 或 Actions 日志里
