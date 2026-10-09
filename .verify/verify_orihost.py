#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""orihost-renew 续期脚本（app.py / Playwright 版）验收 harness。

不碰真浏览器、不发真网络请求，全部用替身。四块：

  [A] 纯逻辑单测 —— MODE / 提醒阈值解析、server ID 解析、cron 表达式与 auto 行、
      remember token 提取、脱敏、代理优先级、_env_* helper
  [B] 场景矩阵 —— main() 的退出码与 TG 门控。浏览器层换成假实现，逐场景断言
      「到底发没发 TG、exit 0 还是 1」。四条不变量：
        · watchdog「剩 > 阈值」→ 静默，exit 0
        · watchdog「剩 ≤ 阈值」→ 发 TG，exit 0
        · watchdog「窗口开咗 / 读唔到」→ 发 TG；窗口开 exit 1（要人手）
        · renew 失败 → 发 TG，exit 1；成功 → exit 0
  [C] 静态与一致性 —— workflow 必须用 renew-kit（钉版本）+ app.py + xvfb；
      基准 cron 行必须还在（cron 自我调度的锚点）；setup_proxy.sh / README 对得上。
      其中最关键一条：**改写后的 workflow 仍是 cron 自我调度的合法锚点**
      —— app.py main() 会按到期日往 workflow 里追加 `# auto:` cron 行。
  [D] cron 自我調度 端到端探针 —— 真跑 app.py 的 updateCronSchedule（打桩 GitHub API）。

用法：
    python .verify/verify_orihost.py
退出码 0 = 全绿。
"""
from __future__ import annotations

import importlib.util
import io
import os
import re
import subprocess
import sys
import types
import warnings
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

warnings.filterwarnings("ignore", category=SyntaxWarning)

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent                                   # _sync/orihost-renew
SCRIPT = ROOT / "app.py"                             # workflow 实际跑的脚本
LEGACY_SCRIPT = ROOT / "orihost_browser_renew.py"    # 旧 SeleniumBase 版（保留，不接线）
WORKFLOW = ROOT / ".github" / "workflows" / "renew.yml"
PROXY_SH = ROOT / "scripts" / "setup_proxy.sh"
README = ROOT / "README.md"
PROBE = ROOT / ".verify" / "probe_cron_writeback.py"

#: workflow 里 renew-kit composite action 的钉版本（改这里 + workflow + README 三处）
RENEWKIT_REF = "v0.5.3"

#: 有默认值 / 由 Actions 或 setup_proxy.sh 注入的变量，不要求 workflow 一定传
OPTIONAL_ENV = {
    "ARTICLE_WAIT", "CLAIM_TIMEOUT",
    "ORIHOST_COOKIE", "ORI_COOKIE", "COOKIE_VALUE",         # 旧名兼容，脚本自行兜底
    "ORIHOST_GOST_PROXY",                                 # ORIHOST_PROXY 的别名
    "IS_PROXY", "PROXY_SERVER",                           # setup_proxy.sh 写入 GITHUB_ENV
    "GITHUB_ACTIONS", "GITHUB_REPOSITORY", "GITHUB_REF_NAME",  # Actions 自带
    "GITHUB_TOKEN", "GH_TOKEN",                           # GH_ROTATE_TOKEN 的兜底名
    "TG_BOT",                                             # TG_BOT_TOKEN/TG_CHAT_ID 的合并写法
    "EMAIL", "ORIHOST_EMAIL",                             # 仅用于 TG 通知脱敏展示
    "ORIHOST_ALERT_DAYS",                                 # ORIHOST_WATCH_DAYS 的旧名
    "DRY_RUN",                                            # 由 inputs 控制，已传
}

#: app.py 是多账号的保留位（workflow 传了 ORIHOST_REMEMBER_N / ORIHOST_SERVER_IDS_N，
#: 当前 app.py 只读单账号；这两个前缀不算「传了不读」）。
MULTI_ACCOUNT_PREFIXES = ("ORIHOST_REMEMBER_", "ORIHOST_SERVER_IDS_")

#: 迁移后 workflow 里**不该**再出现的死变量（脚本已 0 引用）
DEAD_ENV = ("RENEWAL_MAX", "MAX_ATTEMPTS", "DWELL_EXTRA")

#: 每次 load 之前要清掉的环境变量（否则场景之间互相污染）
MANAGED_ENV = (
    "ORIHOST_MODE", "ORIHOST_WATCH_DAYS", "ORIHOST_ALERT_DAYS",
    "ORIHOST_REMEMBER", "ORIHOST_COOKIE", "ORI_COOKIE",
    "ORIHOST_SERVER_IDS",
    "ORIHOST_REMEMBER_1", "ORIHOST_SERVER_IDS_1",
    "ORIHOST_REMEMBER_2", "ORIHOST_SERVER_IDS_2",
    "ORIHOST_REMEMBER_3", "ORIHOST_SERVER_IDS_3",
    "ORIHOST_PROXY", "ORIHOST_GOST_PROXY", "IS_PROXY", "PROXY_SERVER",
    "EMAIL", "ORIHOST_EMAIL",
    "TG_BOT_TOKEN", "TG_CHAT_ID", "TG_BOT",
    "ARTICLE_WAIT", "CLAIM_TIMEOUT",
    "GITHUB_ACTIONS", "GITHUB_REPOSITORY", "GITHUB_REF_NAME",
    "GH_ROTATE_TOKEN", "GH_TOKEN", "DRY_RUN",
)


# ----------------------------------------------------------------- 基础设施

class Checks:
    def __init__(self) -> None:
        self.ok = 0
        self.fails: list[str] = []
        self.skips: list[str] = []

    def section(self, title: str) -> None:
        print(f"\n{title}")

    def check(self, name: str, cond: bool, extra: str = "") -> bool:
        if cond:
            self.ok += 1
            print(f"  \u2705 {name}")
        else:
            tag = f"  [{extra}]" if extra else ""
            self.fails.append(name + tag)
            print(f"  \u274c {name}{tag}")
        return bool(cond)

    def eq(self, name: str, got, want) -> bool:
        return self.check(name, got == want, f"got={got!r} want={want!r}")

    def skip(self, name: str, why: str) -> None:
        self.skips.append(name)
        print(f"  \u26aa SKIP {name} — {why}")

    def report(self) -> int:
        print("\n" + "=" * 62)
        total = self.ok + len(self.fails)
        if self.fails:
            print(f"\u274c {len(self.fails)}/{total} 项失败")
            for f in self.fails:
                print(f"   - {f}")
        else:
            print(f"\u2705 全部通过（{self.ok} 项）"
                  + (f"，{len(self.skips)} 项跳过" if self.skips else ""))
        return 1 if self.fails else 0


def _have(modname: str) -> bool:
    try:
        __import__(modname)
        return True
    except Exception:                                            # noqa: BLE001
        return False


def install_stubs() -> list[str]:
    """app.py 顶层 import requests 与 playwright；本机没有时装最小替身。"""
    stubbed: list[str] = []
    if not _have("playwright.sync_api"):
        pw = types.ModuleType("playwright")
        pw_sa = types.ModuleType("playwright.sync_api")
        pw_sa.sync_playwright = lambda *a, **k: None
        pw.sync_api = pw_sa
        sys.modules["playwright"] = pw
        sys.modules["playwright.sync_api"] = pw_sa
        stubbed.append("playwright")
    if not _have("requests"):
        req = types.ModuleType("requests")
        req.get = lambda *a, **k: None
        req.post = lambda *a, **k: None
        sys.modules["requests"] = req
        stubbed.append("requests")
    return stubbed


_load_counter = [0]


def load_app(**envs):
    """按给定环境变量重新加载 app.py（模块级常量在 import 时求值）。"""
    for key in MANAGED_ENV:
        os.environ.pop(key, None)
    for key, value in envs.items():
        if value is not None:
            os.environ[key] = str(value)
    _load_counter[0] += 1
    name = f"orihost_app_{_load_counter[0]}"
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    with redirect_stdout(io.StringIO()):
        spec.loader.exec_module(mod)
    return mod


# ------------------------------------------------------- [B] 浏览器层替身

class FakePage:
    def add_init_script(self, *a, **k):
        return None


class FakeContext:
    def new_page(self):
        return FakePage()


class FakeBrowser:
    def __init__(self):
        self.closed = False

    def new_context(self, **kw):
        return FakeContext()

    def close(self):
        self.closed = True


class FakeChromium:
    def launch(self, **kw):
        return FakeBrowser()


class FakePlaywright:
    def __init__(self):
        self.chromium = FakeChromium()


class _PWContext:
    def __enter__(self):
        return FakePlaywright()

    def __exit__(self, *exc):
        return False


def _fake_sync_playwright():
    return _PWContext()


_CTRL_KEYS = ("mode", "watch_days", "login_ok", "days", "watchdog", "renew", "tg_calls")


def _split_kw(kw: dict):
    ctrl = {k: kw.pop(k) for k in _CTRL_KEYS if k in kw}
    return ctrl, kw


def run_main(**kw):
    """跑一次 main()，返回 (exit_code, tg_calls)。"""
    ctrl, envs = _split_kw(kw)
    envs.setdefault("ORIHOST_REMEMBER", "tok-a")
    envs.setdefault("ORIHOST_SERVER_IDS", "aaaaaaaa")
    envs.setdefault("ORIHOST_MODE", ctrl.get("mode", "watchdog"))
    envs.setdefault("ORIHOST_WATCH_DAYS", str(ctrl.get("watch_days", "3")))
    mod = load_app(**envs)
    tg_calls: list = ctrl.get("tg_calls") if ctrl.get("tg_calls") is not None else []

    mod.sync_playwright = _fake_sync_playwright
    mod.get_current_ip = lambda *a, **k: "203.0.113.9"
    mod.login = lambda page: ctrl.get("login_ok", True)
    mod.get_renewal_days = lambda page: ctrl.get("days", 30)
    mod.watchdog_check = lambda page: ctrl.get("watchdog", (False, "未到窗口"))
    mod.renew_service = lambda page: ctrl.get("renew", "ok")

    def _tg(status, old_due, new_due, current_ip="未知"):
        tg_calls.append(status)
        return True

    mod.send_telegram_notification = _tg
    with redirect_stdout(io.StringIO()):
        try:
            mod.main()
            code = 0
        except SystemExit as e:
            code = e.code if e.code is not None else 0
        except Exception as e:                                   # noqa: BLE001
            code = f"EXC:{type(e).__name__}: {e}"
    return code, tg_calls


ACC1 = {"ORIHOST_REMEMBER": "tok-a", "ORIHOST_SERVER_IDS": "aaaaaaaa"}


# ============================================================ [A] 纯逻辑

def test_pure(c: Checks) -> None:
    c.section("[A] 纯逻辑单测")
    mod = load_app(**ACC1)

    # ---- A1 remember token 三形态 ----
    c.eq("A1 裸 eyJ token 原样返回",
         mod.extract_remember_token("eyJ" + "x" * 120), "eyJ" + "x" * 120)
    c.eq("A1 完整 Cookie 串里抠 remember_web_xxx",
         mod.extract_remember_token("a=1; remember_web_ab12=SECRET; b=2"), "SECRET")
    c.eq("A1 带引号被剥掉", mod.extract_remember_token('"short-id"'), "short-id")
    c.eq("A1 空串返回空", mod.extract_remember_token(""), "")
    c.eq("A1 None 不崩", mod.extract_remember_token(None), "")

    # ---- A2 mask_server 脱敏 ----
    c.eq("A2 完整 UUID 被替换成短 ID(已脱敏)",
         mod.mask_server(f"server {mod.SERVER_UUID} up"),
         f"server {mod.SERVER_SHORT_ID}(已脱敏) up")
    c.eq("A2 空串原样返回", mod.mask_server(""), "")

    # ---- A3 MODE 解析 ----
    c.check("A3 默认 watchdog", load_app(**ACC1).MODE == "watchdog")
    c.check("A3 mode=renew", load_app(ORIHOST_MODE="renew", **ACC1).MODE == "renew")
    c.check("A3 mode 大小写不敏感",
            load_app(ORIHOST_MODE="RENEW", **ACC1).MODE == "renew")
    c.check("A3 未知 mode 也按 watchdog 走（main 只认 'renew'）",
            load_app(ORIHOST_MODE="whatever", **ACC1).MODE != "renew")

    # ---- A4 提醒阈值（这是 watchdog 静默/提醒的开关）----
    c.eq("A4 默认 3 天", load_app(**ACC1).WATCHDOG_ALERT_DAYS, 3)
    c.eq("A4 ORIHOST_WATCH_DAYS 生效", load_app(ORIHOST_WATCH_DAYS="7", **ACC1).WATCHDOG_ALERT_DAYS, 7)
    c.eq("A4 旧名 ORIHOST_ALERT_DAYS 兜底",
         load_app(ORIHOST_ALERT_DAYS="5", **ACC1).WATCHDOG_ALERT_DAYS, 5)
    c.eq("A4 新名优先于旧名",
         load_app(ORIHOST_WATCH_DAYS="7", ORIHOST_ALERT_DAYS="5", **ACC1).WATCHDOG_ALERT_DAYS, 7)

    # ---- A5 server ID 解析（短 ID 或完整 UUID）----
    m_short = load_app(ORIHOST_REMEMBER="t", ORIHOST_SERVER_IDS="36c736c8")
    c.eq("A5 短 ID：SERVER_SHORT_ID 用它", m_short.SERVER_SHORT_ID, "36c736c8")
    c.check("A5 短 ID：SERVER_UUID 保持默认", m_short.SERVER_UUID != "36c736c8")
    m_full = load_app(ORIHOST_REMEMBER="t",
                      ORIHOST_SERVER_IDS="36c736c8-1111-2222-3333-444455556666")
    c.eq("A5 完整 UUID：短 ID 取前 8 位", m_full.SERVER_SHORT_ID, "36c736c8")
    c.eq("A5 完整 UUID：SERVER_UUID 用它", m_full.SERVER_UUID,
         "36c736c8-1111-2222-3333-444455556666")
    c.check("A5 SERVER_URL 用短 ID", m_full.SERVER_SHORT_ID in m_full.SERVER_URL)
    c.check("A5 cooldown/begin API 用完整 UUID",
            m_full.SERVER_UUID in m_full.API_COOLDOWN and m_full.SERVER_UUID in m_full.API_BEGIN)

    # ---- A6 代理优先级（app.py: PROXY_SERVER env > ORIHOST_PROXY > 默认 sing-box）----
    m = load_app(ORIHOST_PROXY="http://127.0.0.1:7890", IS_PROXY="true",
                 PROXY_SERVER="socks5://127.0.0.1:1080", **ACC1)
    c.check("A6 ORIHOST_PROXY 显式指定 → IS_PROXY true", m.IS_PROXY is True)
    c.eq("A6 PROXY_SERVER env 优先于 ORIHOST_PROXY", m.PROXY_SERVER,
         "socks5://127.0.0.1:1080")
    c.eq("A6 只有 ORIHOST_PROXY 时用它",
         load_app(ORIHOST_PROXY="http://127.0.0.1:7890", **ACC1).PROXY_SERVER,
         "http://127.0.0.1:7890")
    c.eq("A6 无显式则用 sing-box 的 PROXY_SERVER",
         load_app(IS_PROXY="true", PROXY_SERVER="socks5://127.0.0.1:1080", **ACC1).PROXY_SERVER,
         "socks5://127.0.0.1:1080")
    c.check("A6 IS_PROXY=false → REQUESTS_PROXIES 为 None",
            load_app(IS_PROXY="false", **ACC1).REQUESTS_PROXIES is None)

    # ---- A7 cron 自我调度的纯函数 ----
    t = datetime(2026, 10, 15, 10, 0, tzinfo=timezone.utc)
    c.eq("A7 _cron_expr 带月/日（唔用 *）", mod._cron_expr(t), "0 10 15 10 *")
    line = mod._build_auto_line(t, 7)
    c.check("A7 auto 行带机器可读标记", mod._AUTO_TAG in line)
    c.check("A7 auto 行被 _AUTO_LINE_RE 认到", mod._AUTO_LINE_RE.search(line) is not None)
    c.eq("A7 auto 行里的 cron 可回读",
         mod._AUTO_LINE_RE.search(line).group("cron"), "0 10 15 10 *")
    c.check("A7 auto 行也是普通 cron 行（_CRON_LINE_RE 认）",
            mod._CRON_LINE_RE.search(line) is not None)
    c.check("A7 _AUTO_LINE_RE 不误抓普通 cron 行",
            mod._AUTO_LINE_RE.search("    - cron: '0 10 */3 * *'") is None)

    # ---- A8 _env_* helper（app.py 不依赖 renewkit，自带这两个）----
    c.eq("A8 _env_get 缺失返回空", mod._env_get("__NOPE__"), "")
    c.eq("A8 _env_get 缺失返回默认", mod._env_get("__NOPE__", "dflt"), "dflt")
    os.environ["ORIHOST_MODE"] = "  renew  "
    c.eq("A8 _env_get 去空白", mod._env_get("ORIHOST_MODE"), "renew")
    os.environ.pop("ORIHOST_MODE", None)
    os.environ["DRY_RUN"] = "true"
    c.check("A8 _env_dry_run 认 true", mod._env_dry_run() is True)
    os.environ["DRY_RUN"] = "0"
    c.check("A8 _env_dry_run 认 0 为假", mod._env_dry_run() is False)
    os.environ.pop("DRY_RUN", None)


# ============================================================ [B] 场景矩阵

def test_scenarios(c: Checks) -> None:
    c.section("[B] main() 场景矩阵（退出码 + TG 门控）")

    # B0 没凭证 → exit 1，不碰浏览器
    code, tg = run_main(ORIHOST_REMEMBER="", ORIHOST_SERVER_IDS="")
    c.eq("B0 缺 ORIHOST_REMEMBER → exit 1", code, 1)
    c.eq("B0 缺凭证不发 TG", len(tg), 0)

    # B1 watchdog 常态：剩 30 > 阈值 3 → 静默，exit 0
    code, tg = run_main(days=30, watch_days=3, watchdog=(False, "未到窗口"))
    c.eq("B1 watchdog 剩 30 天（>3）→ exit 0", code, 0)
    c.eq("B1 watchdog 常态静默（不发 TG）", len(tg), 0)

    # B2 watchdog 预备提醒：剩 3 ≤ 阈值 3 → 发 TG，但仍 exit 0
    code, tg = run_main(days=3, watch_days=3, watchdog=(False, "未到窗口"))
    c.eq("B2 watchdog 剩 3 天（≤3）→ exit 0", code, 0)
    c.eq("B2 预备提醒发 1 条 TG", len(tg), 1)

    # B3 watchdog 窗口已开 → 要人手 → 发 TG + exit 1
    code, tg = run_main(days=1, watch_days=3, watchdog=(True, "Renew 掣可撳"))
    c.eq("B3 窗口开 → exit 1（要人手注意）", code, 1)
    c.eq("B3 窗口开发 1 条 TG", len(tg), 1)

    # B4 watchdog 读唔到 → 发 TG 但不标红（exit 0）
    code, tg = run_main(days=None, watch_days=3, watchdog=(None, "讀面板失敗"))
    c.eq("B4 读唔到 → exit 0（不标红）", code, 0)
    c.eq("B4 读唔到发 1 条 TG", len(tg), 1)

    # B5 renew 成功 → exit 0 + TG
    code, tg = run_main(mode="renew", days=7, renew="ok")
    c.eq("B5 renew 成功 → exit 0", code, 0)
    c.eq("B5 renew 成功发 1 条 TG", len(tg), 1)

    # B6 renew 失败 → exit 1 + TG
    code, tg = run_main(mode="renew", days=7, renew=False)
    c.eq("B6 renew 失败 → exit 1", code, 1)
    c.eq("B6 renew 失败发 1 条 TG", len(tg), 1)

    # B7 免登失败 → exit 1 + TG
    code, tg = run_main(mode="renew", login_ok=False)
    c.eq("B7 免登失败 → exit 1", code, 1)
    c.eq("B7 免登失败发 1 条 TG", len(tg), 1)

    # B8 renew 未到条件（NOT_TIME）→ exit 0（预期内状态）
    code, tg = run_main(mode="renew", days=7, renew="NOT_TIME")
    c.eq("B8 renew NOT_TIME → exit 0", code, 0)


# ============================================================ [C] 静态与一致性

def test_static(c: Checks) -> None:
    c.section("[C] 静态与一致性")
    src = SCRIPT.read_text(encoding="utf-8")

    # ---- C1 app.py 自带 cron 自我调度（2026-10-09 从旧脚本移植）----
    c.check("C1 定义了 updateCronSchedule", "def updateCronSchedule(" in src)
    c.check("C1 有 _CRON_LINE_RE 锚点正则", "_CRON_LINE_RE = re.compile(" in src)
    c.check("C1 有 _AUTO_LINE_RE 幂等正则", "_AUTO_LINE_RE = re.compile(" in src)
    c.check("C1 DRY_RUN 时跳过 cron 回写", "_env_dry_run()" in src)
    c.check("C1 非 CI 跳过 cron 回写", 'GITHUB_ACTIONS' in src)
    c.check("C1 回写用 GH_ROTATE_TOKEN", "GH_ROTATE_TOKEN" in src)
    c.check("C1 watchdog 分支真的调用 updateCronSchedule",
            "updateCronSchedule(_expiry" in src)
    c.check("C1 cron 异常不冒泡（包 try/except）",
            "cron 自我調度異常" in src)

    # ---- C2 TG 用 HTML parse_mode 时必须转义（否则含 & / < 时 API 400）----
    c.check("C2 send_telegram_notification 对正文做 html.escape",
            src.count("html.escape(") >= 6)
    c.check("C2 引入了 html 模块", re.search(r"^import html$", src, re.M) is not None)

    # ---- C3 旧脚本仍在仓里，但 workflow 不接线它 ----
    c.check("C3 orihost_browser_renew.py 仍存在（保留参考）", LEGACY_SCRIPT.is_file())

    # ---- C4 workflow 接线 ----
    if not WORKFLOW.is_file():
        c.check("C4 workflow 存在", False, str(WORKFLOW))
        return
    wf = WORKFLOW.read_text(encoding="utf-8")
    wf_code = "\n".join(l for l in wf.splitlines() if not l.strip().startswith("#"))

    c.check("C4 用 renew-kit composite action",
            "jardanlau2020/renew-kit/.github/actions/renew@" in wf)
    c.check(f"C4 renew-kit 钉在 {RENEWKIT_REF}（不是 @main）",
            f"actions/renew@{RENEWKIT_REF}" in wf)
    c.eq(f"C4 action 版本与 renewkit-ref 一致（各 1 处）",
         wf_code.count(RENEWKIT_REF), 2)
    c.check("C4 script 指向 app.py（真实文件）",
            "script: app.py" in wf and SCRIPT.is_file())
    c.check("C4 主命令套 xvfb-run（过盾要真实 X 显示）",
            "xvfb-run" in wf and "python3 app.py" in wf)
    c.check("C4 apt 装了 xvfb", "xvfb" in wf)
    c.check("C4 排障产物是 *.png", "artifact-paths:" in wf and "*.png" in wf)
    c.check("C4 引用了 scripts/setup_proxy.sh", "bash scripts/setup_proxy.sh" in wf)
    c.check("C4 失败兜底通知开着", 'notify-on-failure: "true"' in wf)

    # 基准 cron 行必须留着 —— 它是 cron 自我调度的插入锚点
    base = "    - cron: '0 10 */3 * *'"
    c.check("C4 基准 cron 行保留（自我调度的锚点）", base in wf)
    mod = load_app(**ACC1)
    c.check("C4 基准 cron 行能被 _CRON_LINE_RE 认到",
            mod._CRON_LINE_RE.search(wf) is not None)

    # 模拟一次真实插入：证明改写后的 workflow 仍是合法锚点，且基准行不被破坏。
    # ⚠️ workflow 里可能已有脚本回写过的 auto 行，先剥掉再模拟「干净基线」，
    #    否则会误判（这是 harness 之前假绿/假红的根因）。
    clean = mod._AUTO_LINE_RE.sub("", wf)
    matches = list(mod._CRON_LINE_RE.finditer(clean))
    c.check("C4 干净基线上只有基准 cron 行", len(matches) == 1, f"n={len(matches)}")
    last = matches[-1]
    new_line = mod._build_auto_line(datetime(2026, 10, 15, 10, 0, tzinfo=timezone.utc), 7)
    updated = clean[:last.end()] + "\n" + new_line + clean[last.end():]
    c.check("C4 插入 auto 行后基准行原样保留", base in updated)
    c.eq("C4 插入后 cron 行变成 2 条",
         len(list(mod._CRON_LINE_RE.finditer(updated))), 2)
    prev = mod._AUTO_LINE_RE.search(updated)
    c.check("C4 插入后 auto 行可被 _AUTO_LINE_RE 回读", prev is not None)
    if prev:
        c.eq("C4 回读的 cron 与写入一致", prev.group("cron"), "0 10 15 10 *")
    again = mod._AUTO_LINE_RE.search(updated)
    c.check("C4 同日重跑幂等（表达式相同 → 视为无需变更）",
            again is not None and again.group("cron") == mod._cron_expr(
                datetime(2026, 10, 15, 10, 0, tzinfo=timezone.utc)))

    # ---- C5 workflow env ↔ 代码 env 对得上 ----
    step_env = _extract_step_env(wf)
    c.check("C5 提取到 step env 块", len(step_env) > 5, f"n={len(step_env)}")
    read_envs = set(re.findall(r'(?:os\.environ\.get|_env_get)\(\s*["\']([A-Z0-9_]+)["\']', src))
    read_envs |= {"ORIHOST_REMEMBER_1", "ORIHOST_SERVER_IDS_1",
                  "ORIHOST_REMEMBER_2", "ORIHOST_SERVER_IDS_2",
                  "ORIHOST_REMEMBER_3", "ORIHOST_SERVER_IDS_3"}
    read_envs -= OPTIONAL_ENV
    missing = sorted(read_envs - step_env)
    c.check("C5 脚本读的变量 workflow 都传了", not missing, f"missing={missing}")
    c.check("C5 workflow 传了 NODE_LINK（setup_proxy.sh 的命门）", "NODE_LINK" in step_env)
    c.check("C5 NODE_LINK 接的是 secrets.NODE_LINK",
            re.search(r"NODE_LINK:\s*\$\{\{\s*secrets\.NODE_LINK\s*\}\}", wf) is not None)
    c.check("C5 workflow 传了 GH_ROTATE_TOKEN（cron 回写用）", "GH_ROTATE_TOKEN" in step_env)
    c.check("C5 workflow 传了 TG_BOT_TOKEN / TG_CHAT_ID",
            {"TG_BOT_TOKEN", "TG_CHAT_ID"} <= step_env)
    c.check("C5 DRY_RUN 由 inputs 控制",
            re.search(r"DRY_RUN:\s*\$\{\{\s*inputs\.dry_run", wf) is not None)
    for dead in DEAD_ENV:
        c.check(f"C5 不再传死变量 {dead}（脚本已 0 引用）", dead not in step_env)
    allowed_extra = {"NODE_LINK", "DRY_RUN", "TG_BOT_TOKEN", "TG_CHAT_ID",
                     "GH_ROTATE_TOKEN"} | set(OPTIONAL_ENV)
    extra = sorted(v for v in step_env
                   if v not in read_envs and v not in allowed_extra
                   and not v.startswith(MULTI_ACCOUNT_PREFIXES))
    c.check("C5 workflow 没有传脚本不读的变量", not extra, f"extra={extra}")

    # ---- C6 setup_proxy.sh ----
    if not PROXY_SH.is_file():
        c.check("C6 scripts/setup_proxy.sh 存在", False, str(PROXY_SH))
    else:
        sh = PROXY_SH.read_text(encoding="utf-8")
        c.check("C6 写 IS_PROXY 到 GITHUB_ENV", 'IS_PROXY=true' in sh and 'IS_PROXY=false' in sh)
        c.check("C6 写 PROXY_SERVER 到 GITHUB_ENV", 'PROXY_SERVER=${proxy}' in sh)
        c.check("C6 NODE_LINK 为空时打 ::warning::", "::warning::NODE_LINK" in sh)
        c.check("C6 NODE_LINK 为空时指路 workflow env", "secrets.NODE_LINK" in sh)
        c.check("C6 真探测出口（不是只看进程）", "api.ipify.org" in sh or "PROBE_URL" in sh)
        c.check("C6 引用上游 installer（本地 vendored 版）",
                "setup_proxy_upstream.sh" in sh)
        c.check("C6 不覆写 ORIHOST_PROXY（那是脚本的显式代理变量）",
                "ORIHOST_PROXY=" not in sh)
        c.check("C6 用 GITHUB_ENV 而不是 export（跨 step 生效）", "$GITHUB_ENV" in sh)
        c.check("C6 代理端口/探测 URL 可被环境变量覆盖",
                "ORIHOST_SINGBOX_PORT" in sh and "ORIHOST_PROXY_PROBE_URL" in sh)

    # ---- C7 README ----
    if not README.is_file():
        c.check("C7 README 存在", False, str(README))
    else:
        rd = README.read_text(encoding="utf-8")
        c.check("C7 README 提到 renew-kit 迁移", "renew-kit" in rd)
        c.check("C7 README 说明 watchdog / renew 两种模式", "watchdog" in rd and "renew" in rd)
        c.check("C7 README 说明 GHA 过唔到 Turnstile", "Turnstile" in rd)
        c.check("C7 README 保留 NODE_LINK 说明", "NODE_LINK" in rd)
        c.check("C7 README 说明退出码收敛成 0/1", "退出码" in rd)
        c.check(f"C7 README 记录了 renew-kit 的钉版本 {RENEWKIT_REF}",
                RENEWKIT_REF in rd)
        c.check("C7 README 文件结构提到 app.py 是主力",
                "app.py" in rd)


def _extract_step_env(wf: str) -> set[str]:
    """抠出 renew-kit step 的 env: 块里的变量名（该块缩进为 10 空格）。"""
    lines = wf.splitlines()
    start = None
    for i, ln in enumerate(lines):
        if ln.strip() == "env:" and ln.startswith("        env:"):
            start = i
            break
    if start is None:
        return set()
    out: set[str] = set()
    for ln in lines[start + 1:]:
        if not ln.strip():
            continue
        if not ln.startswith("          "):
            break
        m = re.match(r"\s+([A-Z][A-Z0-9_]*):", ln)
        if m:
            out.add(m.group(1))
    return out


# ----------------------------------------------------------------- 主流程

def test_probe(c: Checks) -> None:
    """[D] 端到端：真跑 app.py 的 updateCronSchedule（打桩 GitHub API）。"""
    c.section("[D] cron 自我調度 端到端探针")
    if not PROBE.is_file():
        c.check("D 探针脚本存在", False, str(PROBE))
        return
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT), env.get("PYTHONPATH", "")])
    try:
        proc = subprocess.run([sys.executable, str(PROBE)], cwd=str(ROOT), env=env,
                              capture_output=True, text=True, timeout=180)
    except Exception as exc:                                     # noqa: BLE001
        c.check("D 探针可执行", False, f"{type(exc).__name__}: {exc}")
        return
    tail = "\n".join(proc.stdout.strip().splitlines()[-4:])
    c.check("D 探针退出码 0（新 workflow 是合法锚点 + 回写幂等）",
            proc.returncode == 0, f"rc={proc.returncode}\n{tail}\n{proc.stderr[-400:]}")
    c.check("D 探针覆盖了「基准行不被破坏」", "基准 cron 行原样保留" in proc.stdout)
    c.check("D 探针覆盖了「同日重跑不刷 commit」", "同日重跑不发 PUT" in proc.stdout)
    c.check("D 探针覆盖了「换到期日要更新」", "旧 auto 行被替换而不是叠加" in proc.stdout)
    c.check("D 探针确认没动仓库真文件", "仓库真文件未被改动" in proc.stdout)


def main() -> int:
    print("orihost-renew 验收 harness（app.py / Playwright 版）")
    print(f"  script   : {SCRIPT}")
    print(f"  workflow : {WORKFLOW}")

    stubbed = install_stubs()
    if stubbed:
        print(f"  替身模块 : {', '.join(stubbed)}（离线验收用）")

    c = Checks()
    if not SCRIPT.is_file():
        print(f"\n\u274c 找不到被测脚本 {SCRIPT}")
        return 1

    test_pure(c)
    test_scenarios(c)
    test_static(c)
    test_probe(c)
    return c.report()


if __name__ == "__main__":
    sys.exit(main())
