#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""orihost-renew 续期脚本（renew-kit 迁移）验收 harness。

不碰真浏览器、不发真网络请求，全部用替身。三块：

  [A] 纯逻辑单测 —— 状态字→Outcome 映射矩阵 / 细节行 / 服务器 ID 切分 /
      token 三形态解析 / 多账号加载 / cron 表达式与 auto 行
  [B] 场景矩阵 —— run_all() 与 main() 的退出码。浏览器层换成假实现，
      逐场景断言 Outcome 与「到底点没点续期按钮」。重点守四条不变量：
        · watchdog「剩 > WATCH_DAYS 天」→ SKIPPED，静默，exit 0
        · watchdog「剩 ≤ WATCH_DAYS 天」→ UNKNOWN，发 TG，但 **不** 标红
        · 读唔到状态 → FAILED，exit 1
        · 免登失败 → 账号级一条，不是每台各一条
  [C] 静态与一致性 —— 旧实现的坑不得回归（散落的 exit 1/2、手写 TG、
      发完 TG 就删截图）；workflow / setup_proxy.sh / README 必须和代码对得上。
      其中最关键一条：**改写后的 workflow 仍是 cron 自我调度的合法锚点**
      —— 脚本 main() 末尾会按到期日往 workflow 里追加 `# auto:` cron 行，
      这条依赖「基准 cron 行还在」。workflow 重写最容易静默弄丢的就是它。

用法：
    PYTHONPATH=../_deps python .verify/verify_orihost.py    # 自动找 renew-kit
    RENEWKIT_PATH=/path/to/renew-kit python .verify/verify_orihost.py
退出码 0 = 全绿。
"""
from __future__ import annotations

import ast
import importlib.util
import io
import os
import re
import subprocess
import sys
import tokenize
import types
import warnings
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

#: 被测脚本第 637 行是内嵌 JS（`replace(/\s+/g, ' ')`），Python 每次编译都会
#: 报 SyntaxWarning —— 是本仓迁移前就有的，不是本次引入。本 harness 要重复
#: 加载模块几十次，不压掉的话输出会被同一行警告刷屏。
warnings.filterwarnings("ignore", category=SyntaxWarning)

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent                                   # _sync/orihost-renew
SCRIPT = ROOT / "orihost_browser_renew.py"
WORKFLOW = ROOT / ".github" / "workflows" / "renew.yml"
PROXY_SH = ROOT / "scripts" / "setup_proxy.sh"
README = ROOT / "README.md"
PROBE = ROOT / ".verify" / "probe_cron_writeback.py"

#: 有默认值 / 只在特定路径用到的调参项，不要求 workflow 一定传
OPTIONAL_ENV = {
    "ARTICLE_WAIT", "CLAIM_TIMEOUT",
    "ORIHOST_COOKIE", "ORIHOST_COOKIE_1", "ORI_COOKIE",          # 旧名兼容，脚本自行兜底
    "ORIHOST_GOST_PROXY",                                        # ORIHOST_PROXY 的别名
    "IS_PROXY", "PROXY_SERVER",                                  # setup_proxy.sh 写入 GITHUB_ENV
    "GITHUB_ACTIONS", "GITHUB_REPOSITORY", "GITHUB_REF_NAME",     # Actions 自带
    "GITHUB_TOKEN", "GH_TOKEN",                                   # GH_ROTATE_TOKEN 的兜底名
    "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID",                         # TG_* 的别名
    "DRY_RUN",                                                    # 由 inputs 控制，已传
}

#: 迁移后 workflow 里**不该**再出现的死变量（脚本已 0 引用）
DEAD_ENV = ("RENEWAL_MAX", "MAX_ATTEMPTS", "DWELL_EXTRA")


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


def strip_docstrings(src: str) -> str:
    """把所有 docstring 挖空（保留行号），用于「代码里不许出现 X」这类断言。

    本脚本的注释里特意写了「原来用 sys.exit(1/2)」「旧实现发完 TG 就
    os.remove 截图」来说明为什么不那么做，直接全文匹配会误伤。
    """
    tree = ast.parse(src)
    lines = src.splitlines()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.FunctionDef,
                                 ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        body = getattr(node, "body", None) or []
        if body and isinstance(body[0], ast.Expr) \
                and isinstance(body[0].value, ast.Constant) \
                and isinstance(body[0].value.value, str):
            doc = body[0]
            for i in range(doc.lineno - 1, doc.end_lineno):
                lines[i] = ""
    return "\n".join(lines)


def strip_comments(src: str) -> str:
    """去掉 ``#`` 注释（保留行号）。"""
    try:
        comments = [tok for tok in tokenize.generate_tokens(io.StringIO(src).readline)
                    if tok.type == tokenize.COMMENT]
    except tokenize.TokenError:
        return src
    lines = src.splitlines()
    for tok in comments:
        row, col = tok.start
        lines[row - 1] = lines[row - 1][:col]
    return "\n".join(lines)


def code_only(src: str) -> str:
    """只留可执行代码：挖掉 docstring 与注释。"""
    return strip_comments(strip_docstrings(src))


def find_renewkit() -> Path | None:
    """优先用本地源码（开发时与 renew-kit 同工作区），找不到就退回已安装的包。"""
    override = os.environ.get("RENEWKIT_PATH")
    if override:
        return Path(override)
    for cand in (ROOT.parents[1] / "renew-kit",
                 ROOT.parent / "renew-kit",
                 Path.home() / "renew-kit"):
        if (cand / "renewkit" / "__init__.py").is_file():
            return cand
    return None


def install_stubs() -> bool:
    """塞最小替身，好让被测脚本能被 import。

    · requests：renewkit.http 顶层 import；本 harness 不会真发请求。
    · seleniumbase：被测脚本 `from seleniumbase import SB`，本机必然没有。
    返回是否装了 seleniumbase 替身。
    """
    if importlib.util.find_spec("requests") is None:
        req = types.ModuleType("requests")

        class RequestException(Exception):
            pass

        class Session:
            def __init__(self, *a, **k):
                self.headers = {}

        req.RequestException = RequestException
        req.Session = Session
        req.Response = type("Response", (), {})
        sys.modules["requests"] = req

        adapters = types.ModuleType("requests.adapters")

        class HTTPAdapter:
            def __init__(self, *a, **k):
                pass

        adapters.HTTPAdapter = HTTPAdapter
        req.adapters = adapters
        sys.modules["requests.adapters"] = adapters

        u3 = types.ModuleType("urllib3")
        sys.modules["urllib3"] = u3
        u3util = types.ModuleType("urllib3.util")
        sys.modules["urllib3.util"] = u3util
        u3retry = types.ModuleType("urllib3.util.retry")

        class Retry:
            def __init__(self, *a, **k):
                pass

        u3retry.Retry = Retry
        sys.modules["urllib3.util.retry"] = u3retry

    if importlib.util.find_spec("seleniumbase") is None:
        sb_mod = types.ModuleType("seleniumbase")

        class SB:                      # 真身由每个场景覆盖，这里只为过 import
            def __init__(self, *a, **k):
                raise RuntimeError("seleniumbase 替身：不应被真正实例化")

        sb_mod.SB = SB
        sys.modules["seleniumbase"] = sb_mod
        return True
    return False


def load_renewkit(renewkit: Path | None) -> None:
    if renewkit is not None:
        sys.path.insert(0, str(renewkit))
    else:
        try:
            import renewkit  # noqa: F401
        except ImportError:
            raise SystemExit(
                "找不到 renewkit：先 `pip install renewkit`，"
                "或用 RENEWKIT_PATH 指向 renew-kit 源码目录")


#: 影响脚本行为的全部环境变量 —— 每次加载模块前先清干净，免得上一个场景串味
MANAGED_ENV = (
    "ORIHOST_MODE", "ORIHOST_WATCH_DAYS",
    "ORIHOST_REMEMBER", "ORIHOST_SERVER_IDS",
    "ORIHOST_REMEMBER_1", "ORIHOST_SERVER_IDS_1",
    "ORIHOST_REMEMBER_2", "ORIHOST_SERVER_IDS_2",
    "ORIHOST_REMEMBER_3", "ORIHOST_SERVER_IDS_3",
    "ORIHOST_COOKIE", "ORIHOST_COOKIE_1", "ORI_COOKIE",
    "ORIHOST_PROXY", "ORIHOST_GOST_PROXY", "IS_PROXY", "PROXY_SERVER",
    "GH_ROTATE_TOKEN", "GH_TOKEN", "GITHUB_TOKEN",
    "GITHUB_ACTIONS", "GITHUB_REPOSITORY", "GITHUB_REF_NAME",
    "DRY_RUN", "TG_BOT_TOKEN", "TG_CHAT_ID", "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID",
    "ARTICLE_WAIT", "CLAIM_TIMEOUT",
)

_load_counter = [0]


def load_main(**envs):
    """按给定环境变量重新加载被测脚本。

    模块级常量（MODE / IS_WATCHDOG / WATCH_DAYS / PROXY_STR）是在 import 时
    读的，所以要换环境就得重新 exec 一遍；用递增模块名保证每次都真的重跑。
    """
    for key in MANAGED_ENV:
        os.environ.pop(key, None)
    for key, value in envs.items():
        if value is not None:
            os.environ[key] = str(value)
    _load_counter[0] += 1
    name = f"orihost_main_{_load_counter[0]}"
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    with redirect_stdout(io.StringIO()):
        spec.loader.exec_module(mod)
    return mod


# ------------------------------------------------------- [B] 浏览器层替身

class FakeDriver:
    def __init__(self, cookies=None):
        self._cookies = list(cookies if cookies is not None else [
            {"name": "jexactyl_session", "value": "s"},
            {"name": "XSRF-TOKEN", "value": "x"},
        ])
        self.added: list[dict] = []

    def add_cookie(self, cookie):
        self.added.append(cookie)
        self._cookies.append(cookie)

    def get_cookies(self):
        return list(self._cookies)


class FakeSB:
    """最小 SeleniumBase 替身：只记录被调用的接口。"""

    def __init__(self, pages=None, **kwargs):
        self.kwargs = kwargs
        self.pages = list(pages or [{}])
        self.driver = FakeDriver()
        self.calls: list = []
        self.opened: list[str] = []
        self.shots: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def _cur(self):
        return self.pages[0] if self.pages else {}

    def open(self, url):
        self.calls.append(("open", url))
        self.opened.append(url)

    def get_text(self, selector=""):
        return self._cur().get("text", "203.0.113.9")

    def get_page_source(self):
        return self._cur().get("source", "<html><body>dashboard</body></html>")

    def get_current_url(self):
        return self._cur().get("url", "https://panel.orihost.com/dashboard")

    def delete_all_cookies(self):
        self.calls.append(("delete_all_cookies",))

    def save_screenshot(self, name):
        self.shots.append(name)

    def execute_script(self, script):
        return self._cur().get("js", "null")


class _FakeTime:
    def sleep(self, *a, **k):
        pass


class _FakeRandom:
    def randint(self, a, b):
        return a


def _install(mod, sb_factory=None, login_ok=True, per_server=None):
    """把浏览器/网络/时间都换成替身。per_server 是逐台依次弹出的返回队列。"""
    calls: list = []
    seq = list(per_server or [])

    def _next(_sb, _sv):
        return seq.pop(0) if seq else {"status": "✅ 正常", "days": 30,
                                       "message": "剩 30 天（>7 天，暫唔使理）"}

    mod.SB = sb_factory or (lambda **kw: FakeSB(**kw))
    mod.time = _FakeTime()
    mod.random = _FakeRandom()
    mod.cookie_login = lambda sb, auth: login_ok
    mod.watchdog_one = _next
    mod.renew_one_server = _next
    return calls


#: 场景控制键（不是环境变量，必须在交给 load_main 之前摘掉）
_CTRL_KEYS = ("mode", "watch_days", "sb_factory", "login_ok", "per_server")


def _split_kw(kw: dict):
    ctrl = {k: kw.pop(k) for k in _CTRL_KEYS if k in kw}
    return ctrl, kw


def _load_for_scenario(ctrl, envs):
    return load_main(ORIHOST_MODE=ctrl.get("mode", "watchdog"),
                     ORIHOST_WATCH_DAYS=str(ctrl.get("watch_days", "7")), **envs)


def run_all_of(**kw):
    """跑一次 run_all()，返回 (mod, report, stdout)。"""
    ctrl, envs = _split_kw(kw)
    mod = _load_for_scenario(ctrl, envs)
    _install(mod, sb_factory=ctrl.get("sb_factory"),
             login_ok=ctrl.get("login_ok", True),
             per_server=ctrl.get("per_server"))
    with redirect_stdout(io.StringIO()) as buf:
        report = mod.run_all()
    return mod, report, buf.getvalue()


def main_of(**kw):
    """跑一次 main()，返回 (exit_code, stdout)。"""
    ctrl, envs = _split_kw(kw)
    mod = _load_for_scenario(ctrl, envs)
    _install(mod, sb_factory=ctrl.get("sb_factory"),
             login_ok=ctrl.get("login_ok", True),
             per_server=ctrl.get("per_server"))
    with redirect_stdout(io.StringIO()) as buf:
        code = mod.main()
    return code, buf.getvalue()


ACC1 = {"ORIHOST_REMEMBER": "tok-a", "ORIHOST_SERVER_IDS": "aaaaaaaa"}


# ============================================================ [A] 纯逻辑

def test_pure(c: Checks) -> None:
    c.section("[A] 纯逻辑单测")
    mod = load_main(**ACC1)

    # ---- A1 状态字 → Outcome 映射矩阵（这是整个迁移的语义边界）----
    O = mod.Outcome
    cases = [
        # (status, message, 期望 Outcome, 说明)
        ("✅ 续期成功", "续期天数 7 → 14 天（+7）", O.RENEWED, "renew 成功"),
        ("✅ 正常", "剩 30 天（>7 天，暫唔使理）", O.SKIPPED, "watchdog 常态，静默"),
        ("⏰ 需人手續期", "剩 3 天（renewable=True）", O.UNKNOWN, "要发 TG 但不标红"),
        ("⚠️ 未知结果", "Claim 已提交，但天数仍系 7 天", O.UNKNOWN, "读唔到明确结果"),
        ("⏭️ 跳过", "已达续期上限（Renew Limit Reached）", O.ALREADY_MAX, "满额"),
        ("⏭️ 跳过", "已达续期上限（API: renewal=21 天 renewable=False）", O.ALREADY_MAX, "满额(API)"),
        ("⏭️ 跳过", "刚续期过，对话框显示冷却中（You renewed recently）", O.SKIPPED, "冷却"),
        ("❌ 续期失败", "Turnstile 验证 6 次未通过", O.FAILED, "过盾失败"),
        ("❌ 狀態讀取失敗", "面板 API 讀唔到（execute_script 返 None）", O.FAILED, "watchdog 读唔到"),
        ("❌ 续期失败", "没找到 Renew 入口按钮（页面结构可能变了）", O.FAILED, "入口丢了"),
    ]
    for status, msg, want, note in cases:
        got = mod._outcome_of(status, msg)
        c.check(f"A1 {note}: {status!r} → {want.value}", got is want,
                f"got={got.value if got else got}")

    # 只有 FAILED 让 job 标红 —— 这是 renew-kit 的核心不变量
    c.check("A1 只有 FAILED 是 error（UNKNOWN 不算）",
            not O.UNKNOWN.is_error and not O.SKIPPED.is_error
            and not O.ALREADY_MAX.is_error and not O.TRANSIENT.is_error
            and O.FAILED.is_error)
    c.eq("A1 UNKNOWN.exit_code == 0（⏰ 不标红）", O.UNKNOWN.exit_code, 0)
    c.eq("A1 FAILED.exit_code == 1", O.FAILED.exit_code, 1)
    # 空/None 不得崩（真实场景里 r.get("status","") 可能给空）
    c.check("A1 空 status 不抛异常且判 FAILED",
            mod._outcome_of("", "") is O.FAILED and mod._outcome_of(None, None) is O.FAILED)

    # ---- A2 _detail_of ----
    c.eq("A2 优先用 message",
         mod._detail_of("❌ 续期失败", "Turnstile 验证 6 次未通过"),
         "Turnstile 验证 6 次未通过")
    c.eq("A2 message 为空时退回 status（剥 emoji）",
         mod._detail_of("❌ 续期失败", ""), "续期失败")
    c.eq("A2 全空兜底", mod._detail_of("", ""), "执行失败")
    long_msg = "x" * 300
    c.check("A2 长 message 被截断", len(mod._detail_of("❌", long_msg)) <= 90,
            f"len={len(mod._detail_of('❌', long_msg))}")
    c.check("A2 传异常对象不二次崩", isinstance(mod._detail_of("❌", ValueError("boom")), str))

    # ---- A3 _split_ids ----
    c.eq("A3 逗号分隔", mod._split_ids("a,b,c"), ["a", "b", "c"])
    c.eq("A3 分号也算分隔", mod._split_ids("a;b"), ["a", "b"])
    c.eq("A3 去空白去空项", mod._split_ids(" a , , b "), ["a", "b"])
    c.eq("A3 空串", mod._split_ids(""), [])
    c.eq("A3 None", mod._split_ids(None), [])

    # ---- A4 parse_auth_cookies 三形态 ----
    bare = mod.parse_auth_cookies("eyJpdiI6ImFiYyJ9")
    c.eq("A4 裸 token → 落到默认 remember cookie 名", bare,
         [(mod.DEFAULT_REMEMBER_NAME, "eyJpdiI6ImFiYyJ9")])
    named = mod.parse_auth_cookies("remember_web_abc=eyJpdiI6Inh4In0=")
    c.eq("A4 name=value 形态", named, [("remember_web_abc", "eyJpdiI6Inh4In0=")])
    full = mod.parse_auth_cookies(
        "remember_web_abc=eyJpdiI6Inh4In0=; jexactyl_session=abc%3D%3D; XSRF-TOKEN=zzz")
    names = [n for n, _ in full]
    c.eq("A4 完整 Cookie 串解析出 3 个", len(full), 3)
    c.check("A4 完整 Cookie 串含 remember_web", any(n.startswith("remember_web") for n in names))
    c.check("A4 URL 编码被解码（jexactyl_session）",
            dict(full).get("jexactyl_session") == "abc==", f"got={dict(full).get('jexactyl_session')!r}")
    attrs = mod.parse_auth_cookies(
        "remember_web_abc=v; Path=/; Expires=Wed, 01 Jan 2031 00:00:00 GMT; Domain=.x.com; Secure; HttpOnly")
    c.check("A4 Cookie 属性行被剔除（Path/Expires/Domain/Secure/HttpOnly）",
            [n for n, _ in attrs] == ["remember_web_abc"], f"got={[n for n, _ in attrs]}")
    c.eq("A4 尾部空格被吃掉",
         mod.parse_auth_cookies("  remember_web_abc=v  "), [("remember_web_abc", "v")])

    # ---- A5 load_accounts ----
    m = load_main(ORIHOST_REMEMBER_1="t1", ORIHOST_SERVER_IDS_1="s1,s2",
                  ORIHOST_REMEMBER_2="t2", ORIHOST_SERVER_IDS_2="s3")
    acc = m.load_accounts()
    c.eq("A5 多账号 _1/_2 都读到", [a["label"] for a in acc], ["账号1", "账号2"])
    c.eq("A5 账号1 两台", acc[0]["servers"], ["s1", "s2"])
    c.eq("A5 账号2 一台", acc[1]["servers"], ["s3"])

    m = load_main(ORIHOST_REMEMBER_1="t1", ORIHOST_SERVER_IDS_1="", ORIHOST_REMEMBER="ts",
                  ORIHOST_SERVER_IDS="ss")
    acc = m.load_accounts()
    c.eq("A5 账号1 不完整 → 跳过，落回单账号", [a["label"] for a in acc], ["默认账号"])
    c.eq("A5 单账号 servers", acc[0]["servers"], ["ss"])

    m = load_main(ORI_COOKIE="tc", ORIHOST_SERVER_IDS="sc")
    c.eq("A5 旧名 ORI_COOKIE 兼容", [a["label"] for a in m.load_accounts()], ["默认账号"])

    m = load_main(ORIHOST_REMEMBER="t", ORIHOST_SERVER_IDS_1="s-only")
    c.eq("A5 单账号可借 ORIHOST_SERVER_IDS_1", m.load_accounts()[0]["servers"], ["s-only"])

    m = load_main()
    c.eq("A5 全空 → 无账号", m.load_accounts(), [])

    # ---- A6 _target_name ----
    c.eq("A6 短 ID", mod._target_name("账号1", "8651e616"), "账号1/8651e616")
    c.eq("A6 完整 UUID 取前 8 位",
         mod._target_name("账号2", "670475f5-1206-4a3b-9c1d-000000000000"), "账号2/670475f5")
    c.eq("A6 空 ID 不崩", mod._target_name("账号1", ""), "账号1/")

    # ---- A7 代理优先级 ----
    m = load_main(ORIHOST_PROXY="http://127.0.0.1:7890", IS_PROXY="true",
                  PROXY_SERVER="socks5://127.0.0.1:1080")
    c.eq("A7 ORIHOST_PROXY 显式指定优先", m.PROXY_STR, "http://127.0.0.1:7890")
    c.check("A7 IS_PROXY 随之 true", m.IS_PROXY is True)
    m = load_main(IS_PROXY="true", PROXY_SERVER="socks5://127.0.0.1:1080")
    c.eq("A7 无显式则用 sing-box", m.PROXY_STR, "socks5://127.0.0.1:1080")
    m = load_main(IS_PROXY="false")
    c.eq("A7 IS_PROXY=false → 直连", m.PROXY_STR, "")
    m = load_main(ORIHOST_PROXY="vless://xxx@host:443")
    c.eq("A7 不支持的 scheme 被拒（节点链接应填 NODE_LINK）", m.PROXY_STR, "")

    # ---- A8 模式 ----
    m = load_main(**ACC1)
    c.check("A8 默认 watchdog", m.IS_WATCHDOG is True and m.MODE == "watchdog")
    m = load_main(ORIHOST_MODE="renew", **ACC1)
    c.check("A8 mode=renew", m.IS_WATCHDOG is False)
    m = load_main(ORIHOST_MODE="RENEW", **ACC1)
    c.check("A8 mode 大小写不敏感", m.IS_WATCHDOG is False)
    m = load_main(ORIHOST_MODE="whatever", **ACC1)
    c.check("A8 未知 mode 退回 watchdog（保守）", m.IS_WATCHDOG is True)

    # ---- A9 cron 自我调度的纯函数 ----
    t = datetime(2026, 10, 15, 10, 0, tzinfo=timezone.utc)
    c.eq("A9 _cron_expr 带月/日（唔用 *）", mod._cron_expr(t), "0 10 15 10 *")
    line = mod._build_auto_line(t, 1)
    c.check("A9 auto 行带机器可读标记", mod._AUTO_TAG in line)
    c.check("A9 auto 行被 _AUTO_LINE_RE 认到", mod._AUTO_LINE_RE.search(line) is not None)
    c.eq("A9 auto 行里的 cron 可回读",
         mod._AUTO_LINE_RE.search(line).group("cron"), "0 10 15 10 *")
    c.check("A9 auto 行被 _CRON_LINE_RE 认到（它也是普通 cron 行）",
            mod._CRON_LINE_RE.search(line) is not None)
    # 不含标记的普通行不该被 _AUTO_LINE_RE 误抓
    c.check("A9 _AUTO_LINE_RE 不误抓普通 cron 行",
            mod._AUTO_LINE_RE.search("    - cron: '0 10 */3 * *'") is None)


# ============================================================ [B] 场景矩阵

def test_scenarios(c: Checks) -> None:
    c.section("[B] run_all() / main() 场景矩阵")
    O = None

    # ---- B1 watchdog：剩 > WATCH_DAYS → SKIPPED，静默 ----
    mod, rep, out = run_all_of(mode="watchdog", watch_days="7", **ACC1,
                               per_server=[{"status": "✅ 正常", "days": 30,
                                            "message": "剩 30 天（>7 天，暫唔使理）"}])
    O = mod.Outcome
    c.eq("B1 watchdog 常态 → SKIPPED", rep.results[0].outcome, O.SKIPPED)
    c.eq("B1 天数是 int 且带出来（供 cron 调度用）", rep.results[0].expire, 30)
    c.eq("B1 exit_code 0", rep.exit_code, 0)
    c.check("B1 全部 SKIPPED → notify_tg=False（静默，不发 TG）",
            all(r.outcome in mod.QUIET_OUTCOMES for r in rep.results))

    # ---- B2 watchdog：剩 ≤ WATCH_DAYS → UNKNOWN，发 TG 但不标红 ----
    mod, rep, out = run_all_of(mode="watchdog", watch_days="7", **ACC1,
                               per_server=[{"status": "⏰ 需人手續期", "days": 3,
                                            "message": "剩 3 天（renewable=True）→ 去 panel 人手撳 Renew"}])
    c.eq("B2 需人手續期 → UNKNOWN", rep.results[0].outcome, O.UNKNOWN)
    c.eq("B2 但 exit_code 仍是 0（不标红）", rep.exit_code, 0)
    c.check("B2 UNKNOWN 会触发通知（不在 QUIET 里）",
            rep.results[0].outcome not in mod.QUIET_OUTCOMES)
    c.check("B2 报告里带上了 message 细节", "人手" in rep.results[0].detail
            or "剩 3 天" in rep.results[0].detail, rep.results[0].detail)

    # ---- B3 watchdog：API 读唔到 → FAILED，exit 1 ----
    mod, rep, out = run_all_of(mode="watchdog", **ACC1,
                               per_server=[{"status": "❌ 狀態讀取失敗",
                                            "message": "面板 API 讀唔到（execute_script 返 None）"}])
    c.eq("B3 读唔到状态 → FAILED", rep.results[0].outcome, O.FAILED)
    c.eq("B3 exit_code 1", rep.exit_code, 1)
    c.eq("B3 报告里带 API 错误细节",
         rep.results[0].detail, "面板 API 讀唔到（execute_script 返 None）")

    # ---- B4 renew：成功 / 满额 / 冷却 / 过盾失败 ----
    mod, rep, out = run_all_of(mode="renew", **ACC1,
                               per_server=[{"status": "✅ 续期成功", "days": 14,
                                            "message": "续期天数 7 → 14 天（+7）"}])
    c.eq("B4 renew 成功 → RENEWED", rep.results[0].outcome, O.RENEWED)
    c.eq("B4 RENEWED exit_code 0", rep.exit_code, 0)

    mod, rep, out = run_all_of(mode="renew", **ACC1,
                               per_server=[{"status": "⏭️ 跳过",
                                            "message": "已达续期上限（Renew Limit Reached）"}])
    c.eq("B4 满额 → ALREADY_MAX（不是 FAILED）", rep.results[0].outcome, O.ALREADY_MAX)
    c.eq("B4 ALREADY_MAX exit_code 0", rep.exit_code, 0)

    mod, rep, out = run_all_of(mode="renew", **ACC1,
                               per_server=[{"status": "⏭️ 跳过",
                                            "message": "刚续期过，对话框显示冷却中"}])
    c.eq("B4 冷却 → SKIPPED（与满额区分开）", rep.results[0].outcome, O.SKIPPED)

    mod, rep, out = run_all_of(mode="renew", **ACC1,
                               per_server=[{"status": "❌ 续期失败",
                                            "message": "Turnstile 验证 6 次未通过"}])
    c.eq("B4 过盾失败 → FAILED", rep.results[0].outcome, O.FAILED)
    c.eq("B4 FAILED exit_code 1", rep.exit_code, 1)

    # ---- B5 免登失败是账号级一条，不是每台一条 ----
    mod, rep, out = run_all_of(mode="watchdog", login_ok=False,
                               ORIHOST_REMEMBER_1="t", ORIHOST_SERVER_IDS_1="s1,s2,s3")
    c.eq("B5 免登失败 → 只报 1 条（账号级）", len(rep.results), 1)
    c.eq("B5 目标名带受影响台数", rep.results[0].name, "账号1（3 台）")
    c.eq("B5 outcome FAILED", rep.results[0].outcome, O.FAILED)
    c.check("B5 细节指向 remember token", "remember" in rep.results[0].detail)

    # ---- B6 无账号 → FAILED，exit 1，不启动浏览器 ----
    started = []

    def _sb_factory(**kw):
        started.append(kw)
        return FakeSB(**kw)

    mod = load_main(ORIHOST_MODE="watchdog")
    _install(mod, sb_factory=_sb_factory)
    with redirect_stdout(io.StringIO()) as buf:
        rep = mod.run_all()
    c.eq("B6 无账号 → 1 条 FAILED", (len(rep.results), rep.results[0].outcome),
         (1, O.FAILED))
    c.check("B6 无账号不启动浏览器（省 runner 分钟）", not started, f"started={started}")
    c.check("B6 提示需要哪两个变量",
            "ORIHOST_REMEMBER" in rep.results[0].detail
            and "ORIHOST_SERVER_IDS" in rep.results[0].detail, rep.results[0].detail)

    # ---- B7 main() 退出码：三档收敛成 0/1 ----
    code, out = main_of(mode="watchdog", **ACC1,
                        per_server=[{"status": "✅ 正常", "days": 30, "message": "剩 30 天"}])
    c.eq("B7 watchdog 常态 → exit 0", code, 0)
    c.check("B7 常态不发 TG（日志里没发通知）", "Telegram 未配置" in out or "通知已发送" not in out,
            out[-200:])
    c.check("B7 watchdog 汇总行出现", "watchdog 汇总" in out)

    code, out = main_of(mode="watchdog", **ACC1,
                        per_server=[{"status": "⏰ 需人手續期", "days": 2, "message": "剩 2 天"}])
    c.eq("B7 需人手續期 → 仍 exit 0（实盘的关键：不标红）", code, 0)

    code, out = main_of(mode="watchdog", **ACC1,
                        per_server=[{"status": "❌ 狀態讀取失敗", "message": "讀唔到"}])
    c.eq("B7 读唔到 → exit 1", code, 1)

    code, out = main_of(mode="renew", **ACC1,
                        per_server=[{"status": "✅ 续期成功", "days": 14, "message": "7 → 14"}])
    c.eq("B7 renew 成功 → exit 0", code, 0)

    code, out = main_of(mode="renew", **ACC1,
                        per_server=[{"status": "❌ 续期失败", "message": "过盾失败"}])
    c.eq("B7 renew 失败 → exit 1", code, 1)

    # ---- B8 浏览器起不来 → 收敛成一条 FAILED，不甩 traceback ----
    class BoomSB:
        def __init__(self, **kw):
            raise RuntimeError("chromedriver 起不来")

    code, out = main_of(mode="watchdog", sb_factory=BoomSB, **ACC1)
    c.eq("B8 浏览器异常 → exit 1", code, 1)
    c.check("B8 异常被收敛成报告（不是裸 traceback）",
            "RuntimeError" in out and "Traceback" not in out, out[-300:])

    # ---- B9 多账号多台：逐台一行 ----
    mod, rep, out = run_all_of(mode="watchdog",
                               ORIHOST_REMEMBER_1="t1", ORIHOST_SERVER_IDS_1="a1,a2",
                               ORIHOST_REMEMBER_2="t2", ORIHOST_SERVER_IDS_2="b1",
                               per_server=[{"status": "✅ 正常", "days": 30, "message": "ok"},
                                           {"status": "✅ 正常", "days": 30, "message": "ok"},
                                           {"status": "⏰ 需人手續期", "days": 4, "message": "剩 4 天"}])
    c.eq("B9 3 台 → 3 条结果", len(rep.results), 3)
    c.eq("B9 目标名带账号标签",
         [r.name for r in rep.results], ["账号1/a1", "账号1/a2", "账号2/b1"])
    c.eq("B9 混合结果 exit 0（无 FAILED）", rep.exit_code, 0)

    # ---- B10 真实 watchdog_one 的状态字（不打桩，只换掉网络/等待） ----
    m = load_main(ORIHOST_MODE="watchdog", ORIHOST_WATCH_DAYS="7", **ACC1)
    m.wait_page_ready = lambda sb, timeout=None: None
    m.time = _FakeTime()

    m.api_renewal = lambda sb, sid: ({"renewal": 30, "renewable": True, "status": "ok"}, None)
    r = m.watchdog_one(FakeSB(), "aaaaaaaa")
    c.eq("B10 真实 watchdog_one：剩 30 天 → ✅ 正常", r["status"], "✅ 正常")
    c.eq("B10 带 days=30", r.get("days"), 30)
    c.check("B10 该状态映射成 SKIPPED", m._outcome_of(r["status"], r["message"]) is O.SKIPPED)

    m.api_renewal = lambda sb, sid: ({"renewal": 3, "renewable": True, "status": "ok"}, None)
    r = m.watchdog_one(FakeSB(), "aaaaaaaa")
    c.eq("B10 剩 3 天 → ⏰ 需人手續期", r["status"], "⏰ 需人手續期")
    c.check("B10 该状态映射成 UNKNOWN（发 TG 不标红）",
            m._outcome_of(r["status"], r["message"]) is O.UNKNOWN)

    m.api_renewal = lambda sb, sid: (None, "execute_script 返 None")
    r = m.watchdog_one(FakeSB(), "aaaaaaaa")
    c.eq("B10 API 读唔到 → ❌ 狀態讀取失敗", r["status"], "❌ 狀態讀取失敗")
    c.check("B10 该状态映射成 FAILED（标红）",
            m._outcome_of(r["status"], r["message"]) is O.FAILED)
    c.check("B10 错误细节带上 API 原因", "execute_script" in r["message"], r["message"])

    # ---- B11 真实 cookie_login：注入几个 cookie / 登录页判定 ----
    m = load_main(ORIHOST_MODE="watchdog", **ACC1)
    m.time = _FakeTime()
    m.save_rotated_cookies = lambda sb: None          # 隔离写回 secret 的副作用
    sb = FakeSB(pages=[{"url": "https://panel.orihost.com/dashboard",
                        "source": "<html>dashboard</html>"}])
    ok = m.cookie_login(sb, "remember_web_abc=v1; jexactyl_session=s2")
    c.check("B11 免登成功返回 True", ok is True)
    c.eq("B11 注入了 2 个 cookie", len(sb.driver.added), 2)
    c.check("B11 cookie 落在 panel.orihost.com 域",
            all(x["domain"] == "panel.orihost.com" for x in sb.driver.added))

    sb = FakeSB(pages=[{"url": "https://panel.orihost.com/auth/login",
                        "source": "<html>sign in to continue</html>"}])
    ok = m.cookie_login(sb, "remember_web_abc=stale")
    c.check("B11 仍在登录页 → 返回 False", ok is False)

    # ---- B12 真实 cookie_login 走裸 token（落到默认 remember 名） ----
    sb = FakeSB(pages=[{"url": "https://panel.orihost.com/dashboard",
                        "source": "<html>dashboard</html>"}])
    m.cookie_login(sb, "eyJpdiI6ImFiYyJ9")
    c.eq("B12 裸 token 注入的 cookie 名是默认 remember 名",
         sb.driver.added[0]["name"], m.DEFAULT_REMEMBER_NAME)


# ============================================================ [C] 静态一致性

def test_static(c: Checks) -> None:
    c.section("[C] 静态与一致性")
    src = SCRIPT.read_text(encoding="utf-8")
    code = code_only(src)

    # ---- C1 退出码只有一处 ----
    c.eq("C1 只有一处 sys.exit(", code.count("sys.exit("), 1)
    c.check("C1 且是 sys.exit(main())", "sys.exit(main())" in code)
    for bad in ("sys.exit(1)", "sys.exit(2)", "sys.exit(0)"):
        c.check(f"C1 没有散落的 {bad}", bad not in code)

    # ---- C2 旧实现的痕迹不得回归 ----
    for token in ("fmt_msg", "send_tg", "tg_lib", "now_local(", "_esc(", "_short("):
        c.check(f"C2 已移除旧实现符号 {token}", token not in code)
    c.check("C2 不再手写 TG_BOT_TOKEN 读取（交给 renewkit.notify）",
            "TG_BOT_TOKEN" not in code)
    c.check("C2 不再有 os.environ（改用 renewkit.env）", "os.environ" not in code)
    c.check("C2 不再发完通知就删截图（upload *.png 是排障证据）",
            "os.remove(" not in code and "unlink(" not in code)

    # ---- C3 renew-kit 接线 ----
    c.check("C3 从 renewkit 读 env", "from renewkit import env" in code)
    c.check("C3 用 renewkit 的 Outcome", "from renewkit.outcome import Outcome" in code)
    c.check("C3 用 renewkit 的 RenewReport", "from renewkit.report import RenewReport" in code)
    c.check("C3 没有 import 未使用的 notify", "from renewkit import env, notify" not in code
            and "renewkit.notify" not in code)
    c.check("C3 run_all() 返回 RenewReport", "def run_all() -> RenewReport" in code)
    c.check("C3 main() 返回 int", "def main() -> int" in code)
    c.check("C3 用 report.finish() 收尾（渲染+通知+退出码一处搞定）",
            "report.finish(notify_tg=notify_tg)" in code)

    # ---- C4 QUIET_OUTCOMES 语义 ----
    mod = load_main(**ACC1)
    c.eq("C4 QUIET_OUTCOMES == {SKIPPED, TRANSIENT}",
         {o.value for o in mod.QUIET_OUTCOMES}, {"skipped", "transient"})
    c.check("C4 UNKNOWN 不在 QUIET 里（⏰ 必须发出去）",
            mod.Outcome.UNKNOWN not in mod.QUIET_OUTCOMES)
    c.check("C4 通知门控用的是 QUIET_OUTCOMES",
            "r.outcome not in QUIET_OUTCOMES" in code)
    c.check("C4 状态映射只在一处（_outcome_of）", code.count("def _outcome_of") == 1)

    # ---- C5 cron 自我调度的守卫 ----
    c.check("C5 DRY_RUN 时跳过 cron 回写", "if env.dry_run():" in code)
    c.check("C5 非 CI 跳过 cron 回写", 'env.get("GITHUB_ACTIONS")' in code)
    c.check("C5 回写用 GH_ROTATE_TOKEN", 'env.get("GH_ROTATE_TOKEN")' in code)

    # ---- C6 workflow 接线 ----
    if not WORKFLOW.is_file():
        c.check("C6 workflow 存在", False, str(WORKFLOW))
        return
    wf = WORKFLOW.read_text(encoding="utf-8")
    # 注释里的「改这里两个 v0.4.2 即可」也算字面命中，所以先剥掉注释行再数
    wf_code = "\n".join(l for l in wf.splitlines() if not l.strip().startswith("#"))

    c.check("C6 用 renew-kit composite action", "jardanlau2020/renew-kit/.github/actions/renew@" in wf)
    c.check("C6 renew-kit 钉在 v0.4.2（不是 @main）",
            "actions/renew@v0.4.2" in wf)
    c.eq("C6 action 版本与 renewkit-ref 一致（各 1 处）", wf_code.count("v0.4.2"), 2)
    c.check("C6 script 指向真实文件",
            "script: orihost_browser_renew.py" in wf and SCRIPT.is_file())
    c.check("C6 主命令套 xvfb-run（过盾要真实 X 显示）",
            "xvfb-run" in wf and "python3 orihost_browser_renew.py" in wf)
    c.check("C6 apt 装了 xvfb", "xvfb" in wf)
    c.check("C6 排障产物是 *.png", "artifact-paths:" in wf and "*.png" in wf)
    c.check("C6 引用了 scripts/setup_proxy.sh",
            "bash scripts/setup_proxy.sh" in wf)
    c.check("C6 失败兜底通知开着", 'notify-on-failure: "true"' in wf)

    # 基准 cron 行必须留着 —— 它是 cron 自我调度的插入锚点
    base = "    - cron: '0 10 */3 * *'"
    c.check("C6 基准 cron 行保留（自我调度的锚点）", base in wf)
    mod = load_main(**ACC1)
    c.check("C6 基准 cron 行能被 _CRON_LINE_RE 认到（脚本靠它找插入点）",
            mod._CRON_LINE_RE.search(wf) is not None)

    # 模拟一次真实插入：证明改写后的 workflow 仍是合法锚点，且基准行不被破坏
    matches = list(mod._CRON_LINE_RE.finditer(wf))
    c.check("C6 至少有一条可插入的 cron 行", len(matches) >= 1, f"n={len(matches)}")
    last = matches[-1]
    new_line = mod._build_auto_line(datetime(2026, 10, 15, 10, 0, tzinfo=timezone.utc), 7)
    updated = wf[:last.end()] + "\n" + new_line + wf[last.end():]
    c.check("C6 插入 auto 行后基准行原样保留", base in updated)
    c.eq("C6 插入后 cron 行变成 2 条", len(list(mod._CRON_LINE_RE.finditer(updated))), 2)
    prev = mod._AUTO_LINE_RE.search(updated)
    c.check("C6 插入后 auto 行可被 _AUTO_LINE_RE 回读", prev is not None)
    if prev:
        c.eq("C6 回读的 cron 与写入一致", prev.group("cron"), "0 10 15 10 *")
    # 幂等：同一天再跑一次，cron 表达式相同 → 不产生新提交
    again = mod._AUTO_LINE_RE.search(updated)
    c.check("C6 同日重跑幂等（表达式相同 → 视为无需变更）",
            again is not None and again.group("cron") == mod._cron_expr(
                datetime(2026, 10, 15, 10, 0, tzinfo=timezone.utc)))

    # ---- C7 workflow env ↔ 代码 env 对得上 ----
    step_env = _extract_step_env(wf)
    c.check("C7 提取到 step env 块", len(step_env) > 5, f"n={len(step_env)}")

    read_envs = set(re.findall(r'env\.get(?:_int)?\(\s*"([A-Z0-9_]+)"', src))
    # 拼出来的名字（ORIHOST_REMEMBER_{i} / ORIHOST_SERVER_IDS_{i}）要展开
    read_envs |= {"ORIHOST_REMEMBER_1", "ORIHOST_SERVER_IDS_1",
                  "ORIHOST_REMEMBER_2", "ORIHOST_SERVER_IDS_2",
                  "ORIHOST_REMEMBER_3", "ORIHOST_SERVER_IDS_3"}
    read_envs -= OPTIONAL_ENV
    missing = sorted(read_envs - step_env)
    c.check("C7 脚本读的变量 workflow 都传了", not missing, f"missing={missing}")
    c.check("C7 workflow 传了 NODE_LINK（setup_proxy.sh 的命门）", "NODE_LINK" in step_env)
    c.check("C7 NODE_LINK 接的是 secrets.NODE_LINK",
            re.search(r"NODE_LINK:\s*\$\{\{\s*secrets\.NODE_LINK\s*\}\}", wf) is not None)
    c.check("C7 workflow 传了 GH_ROTATE_TOKEN（cron 回写用）", "GH_ROTATE_TOKEN" in step_env)
    c.check("C7 workflow 传了 TG_BOT_TOKEN / TG_CHAT_ID", {"TG_BOT_TOKEN", "TG_CHAT_ID"} <= step_env)
    c.check("C7 DRY_RUN 由 inputs 控制",
            re.search(r"DRY_RUN:\s*\$\{\{\s*inputs\.dry_run", wf) is not None)
    for dead in DEAD_ENV:
        c.check(f"C7 不再传死变量 {dead}（脚本已 0 引用）", dead not in step_env)
    # 反向：workflow 传的变量脚本要么读、要么是 renewkit/Actions 自用
    allowed_extra = {"NODE_LINK", "DRY_RUN", "TG_BOT_TOKEN", "TG_CHAT_ID",
                     "GH_ROTATE_TOKEN"} | set(OPTIONAL_ENV)
    read_all = set(re.findall(r'env\.get(?:_int)?\(\s*"([A-Z0-9_]+)"', src)) | {
        "ORIHOST_REMEMBER_1", "ORIHOST_SERVER_IDS_1", "ORIHOST_REMEMBER_2",
        "ORIHOST_SERVER_IDS_2", "ORIHOST_REMEMBER_3", "ORIHOST_SERVER_IDS_3"}
    extra = sorted(step_env - read_all - allowed_extra)
    c.check("C7 workflow 没有传脚本不读的变量", not extra, f"extra={extra}")

    # ---- C8 setup_proxy.sh ----
    if not PROXY_SH.is_file():
        c.check("C8 scripts/setup_proxy.sh 存在", False, str(PROXY_SH))
    else:
        sh = PROXY_SH.read_text(encoding="utf-8")
        c.check("C8 写 IS_PROXY 到 GITHUB_ENV", 'IS_PROXY=true' in sh and 'IS_PROXY=false' in sh)
        c.check("C8 写 PROXY_SERVER 到 GITHUB_ENV", 'PROXY_SERVER=${proxy}' in sh)
        c.check("C8 NODE_LINK 为空时打 ::warning::", "::warning::NODE_LINK" in sh)
        c.check("C8 NODE_LINK 为空时指路 workflow env", "secrets.NODE_LINK" in sh)
        c.check("C8 真探测出口（不是只看进程）", "api.ipify.org" in sh or "PROBE_URL" in sh)
        c.check("C8 引用上游 installer", "main.ssss.nyc.mn/setup_proxy.sh" in sh)
        c.check("C8 不覆写 ORIHOST_PROXY（那是脚本的显式代理变量）",
                "ORIHOST_PROXY=" not in sh)
        c.check("C8 用 GITHUB_ENV 而不是 export（跨 step 生效）", "$GITHUB_ENV" in sh)
        c.check("C8 代理端口/探测 URL 可被环境变量覆盖",
                "ORIHOST_SINGBOX_PORT" in sh and "ORIHOST_PROXY_PROBE_URL" in sh)

    # ---- C9 README ----
    if not README.is_file():
        c.check("C9 README 存在", False, str(README))
    else:
        rd = README.read_text(encoding="utf-8")
        c.check("C9 README 提到 renew-kit 迁移", "renew-kit" in rd)
        c.check("C9 README 说明 watchdog / renew 两种模式", "watchdog" in rd and "renew" in rd)
        c.check("C9 README 说明 GHA 过唔到 Turnstile", "Turnstile" in rd)
        c.check("C9 README 保留 NODE_LINK 说明", "NODE_LINK" in rd)
        c.check("C9 README 有代理两档/或明确直连风险说明",
                "直连" in rd or "代理" in rd)
        c.check("C9 README 说明退出码收敛成 0/1", "退出码" in rd)
        c.check("C9 README 记录了 renew-kit 的钉版本", "v0.4.2" in rd)
        # 死变量只许出现在「已移除」的说明里，不许再进「环境变量全表」
        table = _readme_env_table(rd)
        c.check("C9 抠到了环境变量全表", len(table) > 4, f"n={len(table)}")
        for dead in DEAD_ENV:
            c.check(f"C9 环境变量全表不再列 {dead}（脚本已 0 引用）", dead not in table)


def _readme_env_table(rd: str) -> str:
    """抠出 README 里「环境变量全表」那一节的表格文本。

    只在这一节里禁死变量：README 别处（如「迁移行为变化」表）**应该**提到
    `RENEWAL_MAX` 已被移除 —— 那是有效信息，不是「当有效变量宣传」。
    """
    m = re.search(r"^##[^\n]*环境变量全表[^\n]*$", rd, re.M)
    if not m:
        return ""
    rest = rd[m.end():]
    nxt = re.search(r"^##[^\n]+$", rest, re.M)
    return rest[:nxt.start()] if nxt else rest


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

def test_probe(c: Checks, renewkit: Path | None) -> None:
    """[D] 端到端：真跑 updateCronSchedule（打桩 GitHub API）。

    单独成段而不是并进 C6：C6 只做正则级断言（锚点还在、幂等成立），
    这一段跑的是**真实函数**，能抓到「正则没问题但函数跑不通」的情况
    —— 例如 _workflow_path() 认的文件名、PUT body 的字段、base64 编码。
    """
    c.section("[D] cron 自我調度 端到端探针")
    if not PROBE.is_file():
        c.check("D 探针脚本存在", False, str(PROBE))
        return
    env = dict(os.environ)
    pp = [str(ROOT), str(ROOT.parents[1] / "_deps")]
    if renewkit is not None:
        pp.insert(0, str(renewkit))
    if os.environ.get("RENEWKIT_PATH"):
        pp.insert(0, os.environ["RENEWKIT_PATH"])
    env["PYTHONPATH"] = os.pathsep.join(pp + [env.get("PYTHONPATH", "")])
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
    print("orihost-renew 迁移验收 harness")
    print(f"  script   : {SCRIPT}")
    print(f"  workflow : {WORKFLOW}")

    renewkit = find_renewkit()
    stubbed = install_stubs()
    print(f"  renewkit : {renewkit or '（用已安装的包）'}")
    if stubbed:
        print("  seleniumbase: 本机没有 → 已装最小替身（离线验收用）")

    c = Checks()
    if not SCRIPT.is_file():
        print(f"\n\u274c 找不到被测脚本 {SCRIPT}")
        return 1
    try:
        load_renewkit(renewkit)
    except SystemExit as e:
        print(f"\n\u274c {e}")
        return 1

    test_pure(c)
    test_scenarios(c)
    test_static(c)
    test_probe(c, renewkit)
    return c.report()


if __name__ == "__main__":
    sys.exit(main())
