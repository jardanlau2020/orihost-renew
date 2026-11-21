# orihost-renew

Orihost（panel.orihost.com，Jexactyl）免费服务器的自动续期。

**2026-11-21 起整份采用上游方案**，不再是 watchdog 只提醒：

| 来源 | 内容 |
| :--- | :--- |
| 上游 `gxjxzhoo/auto-renew-orihost` @ `e204408`（2026-11-01） | `orihost_renew.py`（29KB）、`node_proxy.py`（18KB）、`requirements.txt` |
| 本仓补丁 | 三级凭证解析（含 `ORIHOST_REMEMBER` 桥接）、家宽出口 workflow |

## 为什么用上游

Orihost 续期要先过 Cloudflare Turnstile，纯代码层很难过。上游的做法是：

- `pass_turnstile()` + `click_turnstile_box()`：算 Turnstile 容器的**屏幕坐标，用系统级鼠标点**（不是 `element.click()`，CF 会识破）
- `rerender_detached()`：**断开 webdriver 连接后重新渲染**，躲开「webdriver 状态下渲染的 Turnstile 必被识别」
- 续期走接口 `renew/begin` → `renewal/complete`，失败回退页面流程
- 登录后导出最新 cookie，配了 `GH_ROTATE_TOKEN` 时自动回写 secret

## 凭证（按优先级，谁有就用谁）

| 优先级 | Secret | 说明 |
| :---: | :--- | :--- |
| 1 | `ORIHOST_EMAIL` + `ORIHOST_PASSWORD` | 上游主线，登录后自己管 session |
| 2 | `ORIHOST_COOKIE` | 整段 Cookie 串（panel.orihost.com 请求头那一行） |
| 3 | `ORIHOST_REMEMBER` | Jexactyl 的 `remember_web_59ba36...` token，裸值或整段 Cookie 都认 |

三个都没有时，日志会打一行「缺凭证：需要 … 其中之一」，不静默失败。

## 家宽出口

Orihost 会记录续期来源 IP，GHA 机房 IP 是按 IP 风控的典型特征，所以默认走住宅出口：

1. workflow 用 `HOME_SUB_URL`（secret）拉订阅 → mihomo（Clash Meta ≥ 1.19.25）起本地 `127.0.0.1:7890`
2. `curl -x` 打 `api.ipify.org` 验出口：**不通或与直连 IP 相同就失败退出**，不白跑
3. 脚本通过 `NODE_PROXY_URL` 使用这个出口（上游原生支持该 env，无需改代码）
4. 收工 kill mihomo

没配 `HOME_SUB_URL` 时回落到 `NODE_LINK`（sing-box，上游 `node_proxy.py` 自带）。

## 其他 Secret

| 名称 | 必填 | 说明 |
| :--- | :---: | :--- |
| `HOME_SUB_URL` | 推荐 | 家宽订阅地址 |
| `NODE_LINK` | ❌ | 备用代理节点（家宽没配时用） |
| `TG_BOT_TOKEN` / `TG_CHAT_ID` | ❌ | 续期成功/失败通知 |
| `GH_ROTATE_TOKEN` | ❌ | 用于自动回写 `ORIHOST_COOKIE`（需 Secrets 写权限） |

## 手动触发

`workflow_dispatch` 一个入参：`no_proxy` = 跳过家宽直连跑（排查用）。

## 验证

```bash
pip install -r requirements.txt
ORIHOST_EMAIL=xxx ORIHOST_PASSWORD=xxx python orihost_renew.py
```

产物：`run.log` / `fail_*.png` / `mihomo.log`（artifact `orihost-run`，保留 7 天）。

## 已知限制

- Turnstile 过不过取决于 Cloudflare 当次判定，失败会截图留证并重试；连续失败看 `fail_*.png`。
- 家宽节点是共享的，会掉线；订阅里已带 fallback / url-test。
