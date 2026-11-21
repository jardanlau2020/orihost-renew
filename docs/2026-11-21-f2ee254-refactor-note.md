# 重构补档：`f2ee254`（2026-11-21 08:53Z）

> 这份文档是补档，不是新决策。原 commit 的 message 在写入时中文被替换成 `?`，
> 按规矩**不改历史**（不 revert、不 force push），所以在这里把范围和来源写清楚。

## 为什么会有这条

`f2ee254` 是一次直接落到 `main` 的大改动，message 乱码，且一次混了 5 个文件 800 多行，
事后无法逐行审。冷王复核时要求补一份正常说明，故有此档。

## 改动范围（5 个文件，+804/-534）

| 文件 | 改动 |
|---|---|
| `orihost_renew.py` | +804/-534，整份采用上游实现 |
| `node_proxy.py` | 新增 464 行，sing-box / vless 本地代理 |
| `.github/workflows/renew.yml` | +121/-103 |
| `README.md` | -224/+42 |
| `requirements.txt` | -5/+1，只留 seleniumbase |

## 上游来源

`gxjxzhoo/auto-renew-orihost @ e204408`

## 合规核对

- 硬编码凭证扫描：`ghp_` / `github_pat_` / `sk-` / `AKIA` / `password=` 字面量 **0 命中**
- 凭证全部从环境变量读取：`ORIHOST_EMAIL` / `ORIHOST_PASSWORD` / `ORIHOST_COOKIE` / `ORIHOST_REMEMBER`

## 流程问题（已认）

1. **commit message 乱码**：根因是 PowerShell 5.1 的 `Invoke-RestMethod` 把字符串 body
   按 Latin-1 发送，非 ASCII 直接变 `?`。已改用 `tools/gh-commit.ps1`
   （UTF-8 字节发送 + 提交后回读校验 `message_ok`）。
2. **一次混 5 个文件 800+ 行**，无法逐行审。
3. **未经 PR 直接落 main**。

## 后续约束

- 这个量级的重构必须走 PR，不直接落 `main`。
- 所有 `main` 上的 commit 由冷王过一眼；乱码或绕过 PR 直接打回。
- 本仓 `has_issues=false`，补档类说明统一放 `docs/`。
