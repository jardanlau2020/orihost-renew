#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端探针：在真实 workflow 文件上跑一遍 updateCronSchedule（打桩 GitHub API）。

为什么单独留一个探针而不是只写进 verify_orihost.py：
    cron 自我調度是本仓唯一「脚本反向改写自己的 workflow」的路径，也是 workflow
    重写最容易静默弄丢的东西 —— 基准 cron 行一旦没了，_CRON_LINE_RE 找不到插入
    点，函数只打一行 ⚠️ 就放弃，续期本身完全正常，于是没人会发现调度已经死了。
    harness 里的正则级断言能覆盖「锚点还在」，但覆盖不了「函数真跑起来能写出
    一条合法、幂等的 auto 行」。这里用真实函数 + 打桩 API 把它跑通。

安全：全程在 tempfile 建的临时目录里操作（把 renew.yml 复制过去当锚点），
      绝不改动仓库里的真文件。退出码 0 = 通过。
"""
from __future__ import annotations

import base64
import importlib.util
import io
import os
import shutil
import sys
import tempfile
import types
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent          # _sync/orihost-renew
SCRIPT = REPO / "orihost_browser_renew.py"
REAL_WF = REPO / ".github" / "workflows" / "renew.yml"

FAILURES: list[str] = []


def ok(name: str) -> None:
    print(f"  \u2705 {name}")


def fail(name: str, extra: str = "") -> None:
    FAILURES.append(name)
    print(f"  \u274c {name}" + (f"  [{extra}]" if extra else ""))


def expect(name: str, cond: bool, extra: str = "") -> bool:
    ok(name) if cond else fail(name, extra)
    return bool(cond)


# --------------------------------------------------------------- 打桩

_captured: dict = {}


class _Resp:
    def __init__(self, code, payload=None, text=""):
        self.status_code = code
        self._payload = payload or {}
        self.text = text

    def json(self):
        return self._payload


def _fake_get(url, headers=None, timeout=None, **kw):
    _captured["get_url"] = url
    return _Resp(200, {"sha": "deadbeef" * 5})


def _fake_put(url, headers=None, json=None, timeout=None, **kw):
    _captured["put_url"] = url
    _captured["put_body"] = json
    return _Resp(201)


def install_stubs() -> None:
    """requests 换成只认 get/put 的替身（要拦 Contents API），seleniumbase 也塞一个。

    ⚠️ 必须在 import 被测脚本之前装：renewkit.http 顶层就 import requests，
    晚一步就拿到真身，PUT 会真的发出去。
    """
    req = types.ModuleType("requests")
    req.get = _fake_get
    req.put = _fake_put
    req.RequestException = Exception
    req.Session = type("Session", (), {"__init__": lambda self, *a, **k: None})
    req.Response = _Resp

    adapters = types.ModuleType("requests.adapters")
    adapters.HTTPAdapter = type("HTTPAdapter", (), {"__init__": lambda self, *a, **k: None})
    req.adapters = adapters
    sys.modules["requests"] = req
    sys.modules["requests.adapters"] = adapters

    for name in ("urllib3", "urllib3.util", "urllib3.util.retry"):
        sys.modules.setdefault(name, types.ModuleType(name))
    sys.modules["urllib3.util.retry"].Retry = type(
        "Retry", (), {"__init__": lambda self, *a, **k: None})

    sb = types.ModuleType("seleniumbase")
    sb.SB = type("SB", (), {"__init__": lambda self, *a, **k: None})
    sys.modules["seleniumbase"] = sb


def add_renewkit_path() -> None:
    override = os.environ.get("RENEWKIT_PATH")
    if override and (Path(override) / "renewkit" / "__init__.py").is_file():
        sys.path.insert(0, override)
        return
    for cand in (REPO.parents[1] / "renew-kit", REPO.parent / "renew-kit"):
        if (cand / "renewkit" / "__init__.py").is_file():
            sys.path.insert(0, str(cand))
            return


def load_script():
    spec = importlib.util.spec_from_file_location("orihost_cron_probe", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["orihost_cron_probe"] = mod
    with redirect_stdout(io.StringIO()):
        spec.loader.exec_module(mod)
    return mod


def main() -> int:
    print("cron 自我調度 端到端探针（updateCronSchedule）")
    if not REAL_WF.is_file():
        print(f"\u274c 找不到 workflow: {REAL_WF}")
        return 1

    install_stubs()
    add_renewkit_path()
    try:
        mod = load_script()
    except ModuleNotFoundError as e:
        print(f"\u274c 依赖缺失: {e}（用 PYTHONPATH 指向 renew-kit / 依赖目录）")
        return 1

    # ---- 在临时目录里复刻锚点，绝不碰真文件 ----
    tmp = Path(tempfile.mkdtemp(prefix="orihost-cron-probe-"))
    try:
        (tmp / ".github" / "workflows").mkdir(parents=True)
        shutil.copy(REAL_WF, tmp / ".github" / "workflows" / "renew.yml")
        real_before = REAL_WF.read_text(encoding="utf-8")
        cwd0 = os.getcwd()
        os.chdir(tmp)

        os.environ.update({
            "GITHUB_ACTIONS": "true",
            "GITHUB_REPOSITORY": "jardanlau2020/orihost-renew",
            "GITHUB_REF_NAME": "main",
            "GH_ROTATE_TOKEN": "ghp_fake_for_probe",
        })
        os.environ.pop("DRY_RUN", None)

        base = (tmp / ".github" / "workflows" / "renew.yml").read_text(encoding="utf-8")

        # 1) 锚点文件找得到
        expect("_workflow_path() 命中 renew.yml",
               mod._workflow_path() == str(tmp / ".github" / "workflows" / "renew.yml"),
               str(mod._workflow_path()))

        # 2) 基准 cron 行还在（插入锚点）
        anchors = list(mod._CRON_LINE_RE.finditer(base))
        expect("基准 cron 行可被 _CRON_LINE_RE 命中", len(anchors) >= 1,
               f"n={len(anchors)}")

        # 3) 真跑一次回写
        expiry = datetime(2026, 10, 15, 10, 0, tzinfo=timezone.utc)
        _captured.clear()
        with redirect_stdout(io.StringIO()) as buf:
            wrote = mod.updateCronSchedule(expiry, lead_days=7)
        log = buf.getvalue().strip()
        print(f"     {log}")
        expect("首次回写返回 True", wrote is True)
        expect("回写走 Contents API PUT",
               "put_url" in _captured and "/contents/.github/workflows/renew.yml"
               in _captured.get("put_url", ""), _captured.get("put_url", ""))

        body = _captured.get("put_body") or {}
        expect("PUT 带 branch", body.get("branch") == "main", str(body.get("branch")))
        expect("PUT 带 sha（更新而非新建）", bool(body.get("sha")))
        expect("commit message 说明下次巡检时间",
               "chore(cron)" in (body.get("message") or ""), body.get("message", ""))

        try:
            updated = base64.b64decode(body["content"]).decode("utf-8")
        except Exception as e:                                   # noqa: BLE001
            fail("回写内容是合法 base64", str(e))
            updated = ""

        # 4) 回写内容：auto 行写进去了，基准行没被破坏
        #    到期 10-15 减 lead=7 → 10-08
        expect("auto cron 行写入（0 10 8 10 *）", "0 10 8 10 *" in updated)
        expect("基准 cron 行原样保留（永不可变）",
               "    - cron: '0 10 */3 * *'" in updated)
        rows = list(mod._CRON_LINE_RE.finditer(updated))
        expect("插入后恰好 2 条 cron 行（基准 + auto）", len(rows) == 2, f"n={len(rows)}")
        auto = mod._AUTO_LINE_RE.search(updated)
        expect("auto 行可被 _AUTO_LINE_RE 回读", auto is not None)
        expect("回读的 cron 与预期一致",
               bool(auto) and auto.group("cron") == "0 10 8 10 *",
               auto.group("cron") if auto else "None")

        # 5) 幂等：模拟第一次的 commit 已落地（写回临时文件），再跑一次
        (tmp / ".github" / "workflows" / "renew.yml").write_text(updated, encoding="utf-8")
        _captured.clear()
        with redirect_stdout(io.StringIO()) as buf:
            wrote2 = mod.updateCronSchedule(expiry, lead_days=7)
        print(f"     {buf.getvalue().strip()}")
        expect("同日重跑返回 False（无变更）", wrote2 is False)
        expect("同日重跑不发 PUT（不刷 commit）", "put_url" not in _captured)

        # 6) 换了到期日则要更新（不能一味跳过）
        _captured.clear()
        with redirect_stdout(io.StringIO()):
            wrote3 = mod.updateCronSchedule(
                datetime(2026, 11, 20, 10, 0, tzinfo=timezone.utc), lead_days=7)
        expect("换到期日 → 重新回写 True", wrote3 is True)
        body3 = _captured.get("put_body") or {}
        upd3 = base64.b64decode(body3["content"]).decode("utf-8") if body3 else ""
        expect("旧 auto 行被替换而不是叠加",
               upd3.count(mod._AUTO_TAG) == 1, f"auto行数={upd3.count(mod._AUTO_TAG)}")
        expect("新 cron 为 0 10 13 11 *", "0 10 13 11 *" in upd3)

        os.chdir(cwd0)
        expect("仓库真文件未被改动", REAL_WF.read_text(encoding="utf-8") == real_before)
    finally:
        os.chdir(REPO)
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if FAILURES:
        print(f"\u274c {len(FAILURES)} 项失败")
        return 1
    print("\u2705 全部通过：新 workflow 是 cron 自我調度的合法锚点，且回写幂等")
    return 0


if __name__ == "__main__":
    sys.exit(main())
