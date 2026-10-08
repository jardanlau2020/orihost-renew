#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Orihost 浏览器自动续期（SeleniumBase + 真浏览器）—— 已迁移到 renew-kit
# 背景：面板 claim 接口强制要求 Cloudflare Turnstile token（GET /api/client/renewal/complete?cf-turnstile-response=xxx），
#       纯 HTTP 调不通（无 token 直接 500），必须用真浏览器点验证。
# 流程：Cookie 免登 → 服务器页 → Renew（打开对话框）→ Read Article（新标签读文章）→ 倒计时 → 点 Turnstile → Claim Renewal
# 参考：katabump-renew-main（同款 Turnstile 处理 + xvfb 无头方案）
#
# ─────────────────────────── 迁移说明（renew-kit） ───────────────────────────
# 公共部分（环境变量读取 / 结果语义 / 报告排版 / TG 通知 / 退出码）交给 renewkit，
# 本文件只保留 orihost 自己的业务：Turnstile 过盾、Cookie 免登、claim 流程、
# watchdog 巡检、cron 自我調度。
#
# 迁移带来的行为变化（每条都有据，不是顺手改的）：
#
# 1. 退出码从 1/0/2 三档收敛成 0/1 两档。原来是
#      exit 1（没账号 / watchdog 读不到）、exit 2（renew 有失败）、exit 0
#    workflow 只看「非零」，分不出轻重，也没法把「面板抖了」和「token 废了」区分开。
#    现在只有 FAILED → 1。
#
# 2. TG 从「每台一条」改成「整轮一条」（renew-kit 的 RenewReport 排版）。
#    原来 3 台服务器就是 3 条消息，同一轮的信息被拆散，看的人得自己拼。
#
# 3. watchdog 的「剩 N 天，暂时唔使理」映射成 SKIPPED —— 静默。
#    这是本仓排程模式的常态（每 3 天巡检一次），天天发就是噪音。
#    而「⏰ 需人手續期」映射成 UNKNOWN —— **发** TG 但**不**标红：
#    它是预期内的状态（GHA 过唔到 Turnstile，续期本来就得人手），不是失败。
#    原来这里 exit 0 但每台一条 TG，方向是对的，现在只是收进统一语义。
#
# 4. 状态字符串（"✅ 正常" / "⏰ 需人手續期" / "⏭️ 跳过" / "❌ ..."）保留为
#    **内部协议**：renew_one_server / watchdog_one 的返回值一个字节没动，
#    只在报告边界由 _outcome_of() 映射成 Outcome。这样 1300 行浏览器逻辑
#    不用碰 —— 迁移的风险面就只有这个文件头和 main()。
#
# 5. ORIHOST_PROXY 的显式指定优先于工作流代理这一条**保留**：
#    节点链接（vless:// 之类）只能填 NODE_LINK，本地跑则用 ORIHOST_PROXY。
#    （上游 setup_proxy.sh 会把 IS_PROXY/PROXY_SERVER 写进 $GITHUB_ENV。）
#
# 6. TG_BOT（"chat_id,token" 兼容写法）**取消**。renewkit.notify 只认
#    TG_BOT_TOKEN / TELEGRAM_TOKEN + TG_CHAT_ID / TELEGRAM_CHAT_ID，四种都读，
#    少一个就静默跳过通知（不会因为通知挂了把续期判成失败）。

import json
import os
import re
import sys
import time
import random
import requests
from datetime import datetime, timezone, timedelta
from urllib.parse import unquote
from seleniumbase import SB

from renewkit import env
from renewkit.outcome import Outcome
from renewkit.report import RenewReport, shorten

PANEL = "https://panel.orihost.com"
# Laravel 默认 remember cookie 名（yanyumm1 实测 Orihost 可用）
DEFAULT_REMEMBER_NAME = "remember_web_59ba36addc2b2f9401580f014c7f58ea4e30989d"
# 文章页停留秒数（面板 dwell=15，多留 buffer；“过早关闭文章页会被警告”）
ARTICLE_WAIT = env.get_int("ARTICLE_WAIT", 30)
# Claim 按钮轮询上限
CLAIM_TIMEOUT = env.get_int("CLAIM_TIMEOUT", 150)

# ---------- 代理 ----------
# 优先级：ORIHOST_PROXY 显式指定 > 工作流 sing-box（IS_PROXY/PROXY_SERVER，由 NODE_LINK 转出）
def _get_proxy():
    explicit = (env.get("ORIHOST_PROXY") or env.get("ORIHOST_GOST_PROXY") or "").strip()
    if explicit:
        scheme = explicit.split("://", 1)[0].lower() if "://" in explicit else ""
        if scheme in ("http", "https", "socks4", "socks5", "socks5h"):
            return explicit
        print(f"  ⚠️ ORIHOST_PROXY 格式不支持 ({scheme}://)，节点链接请填 NODE_LINK")
    if (env.get("IS_PROXY") or "").lower() == "true":
        srv = (env.get("PROXY_SERVER") or "socks5://127.0.0.1:1080").strip()
        print(f"  🔗 使用 sing-box 代理: {srv}")
        return srv
    return ""

PROXY_STR = _get_proxy()
IS_PROXY = bool(PROXY_STR)


# ---------- 运行模式 ----------
# watchdog（默认，排程用）：只读剩余天数 + 到期 TG 提醒，**唔撳续期**。
#   原因：claim 接口强制 Cloudflare Turnstile，GHA runner IP 实测过唔到
#   （2026-09-23 run 35815226857 / 35819886961 —— 8 轮 8 次全败，两个出口都唔得）。
#   自动撳续期的边界退到「提醒」，动作留给人手。
# renew：真撳续期（只有人手 workflow_dispatch 拣 mode=renew 先用）。
MODE = (env.get("ORIHOST_MODE", "watchdog") or "watchdog").strip().lower()
WATCH_DAYS = env.get_int("ORIHOST_WATCH_DAYS", 7)
IS_WATCHDOG = MODE != "renew"


# ---------- 状态字符串 → renew-kit Outcome ----------
# 上游（renew_one_server / watchdog_one）沿用一串 emoji 状态字，这里做唯一一次映射。
# 保留那串字是为了不动 1300 行浏览器逻辑；映射集中在这里，改语义只改这一处。
#
#   ✅ 正常（watchdog，剩 > WATCH_DAYS） → SKIPPED  静默：排程模式的常态
#   ⏰ 需人手續期                        → UNKNOWN  发 TG 但**不**标红：
#                                        GHA 过唔到 Turnstile 是已知前提，不是失败
#   ⏭️ 跳过（已达上限）                  → ALREADY_MAX
#   ⏭️ 跳过（冷却中 / 未到窗口）          → SKIPPED
#   ⚠️ 未知结果                          → UNKNOWN
#   ✅ 续期成功                          → RENEWED
#   其余 ❌                             → FAILED（只有它 exit 1）
_ALREADY_MAX_HINTS = ("已达续期上限", "Renew Limit Reached", "renew limit")


def _outcome_of(status: str, message: str = "") -> Outcome:
    """把内部状态字映射成 Outcome。纯函数，方便离线把每种字都过一遍。"""
    s = (status or "").strip()
    blob = f"{s} {message or ''}"
    if "成功" in s:
        return Outcome.RENEWED
    if s.startswith("⏰"):
        return Outcome.UNKNOWN
    if s.startswith("⏭️") or "跳过" in s:
        return (Outcome.ALREADY_MAX
                if any(h.lower() in blob.lower() for h in _ALREADY_MAX_HINTS)
                else Outcome.SKIPPED)
    if s.startswith("⚠️"):
        return Outcome.UNKNOWN
    if s.startswith("✅"):
        return Outcome.SKIPPED
    return Outcome.FAILED


#: message 开头重复的「剩 N 天（…）」——天数已经由 TargetResult.expire 渲染成
#: 「（剩 N 天）」，再让 message 带一遍就会印成「剩 12 天 · 剩 12 天（>7 天…）」。
_RE_LEAD_DAYS = re.compile(r"^\s*剩\s*\d+(?:\.\d+)?\s*天\s*(?:[（(][^）)]*[）)]\s*)?")


def _detail_of(status: str, message: str) -> str:
    """给报告用的细节行。

    状态字本身（"❌ 续期失败"）对已经看过 TG 的人是零信息量 —— 真正有用的是
    后面那句 message（"Turnstile 验证 6 次未通过"）。所以优先用 message。

    但 message 里跟 expire 重复的部分要剥掉，否则实盘报告长这样：
        🟢 默认账号/36c736c8 · 状态良好（剩 12 天）
        ℹ️ 剩 12 天 · 剩 12 天（>7 天，暫唔使理）      ← 一眼看上去像 bug
    剥完是：
        ℹ️ 剩 12 天 · 正常
    """
    msg = _RE_LEAD_DAYS.sub("", str(message or ""))
    msg = msg.lstrip("→->·,，;； \t")
    msg = shorten(msg, 90)
    if msg:
        return msg
    return shorten(status.strip("✅❌⚠️⏭️⏰ "), 90) or "执行失败"


# ---------- 账号解析（与 orihost_renew.py 同一套变量名） ----------
def _split_ids(raw: str):
    return [s.strip() for s in (raw or "").replace(";", ",").split(",") if s.strip()]


def parse_auth_cookies(auth_raw: str):
    """把用户填的 token 还原成 [(name, value)]，支持裸 token / name=value / 完整 Cookie 串"""
    v = (auth_raw or "").strip()
    if "remember_web" in v and (";" in v or "XSRF-TOKEN" in v or "jexactyl_session" in v):
        out = []
        for item in v.split(";"):
            item = item.strip()
            if not item or "=" not in item:
                continue
            k, val = item.split("=", 1)
            k, val = k.strip(), val.strip()
            if not k or k.lower() in ("path", "expires", "domain", "max-age", "samesite", "secure", "httponly"):
                continue
            try:
                val = unquote(val)
            except Exception:
                pass
            out.append((k, val))
        return out
    if "=" in v and "remember_web" in v:
        name, val = v.split("=", 1)
        return [(name.strip(), val.strip())]
    return [(DEFAULT_REMEMBER_NAME, v)]


def load_accounts():
    """账号来源（变量名与 orihost_renew.py 保持一致）。

    变量名是拼出来的（ORIHOST_REMEMBER_1..19），所以这里用 env.get 而不是
    os.environ —— env.get 顺带做了 strip，用户从浏览器复制 token 时尾巴上
    那个换行/空格是最高频的坑（README 专门提醒过）。
    """
    accounts = []
    for i in range(1, 20):
        token_raw = (env.get(f"ORIHOST_REMEMBER_{i}") or env.get(f"ORIHOST_COOKIE_{i}") or "").strip()
        ids = _split_ids(env.get(f"ORIHOST_SERVER_IDS_{i}"))
        if not token_raw and not ids:
            continue
        if not token_raw or not ids:
            print(f"⚠️ 账号{i} 配置不完整，跳过")
            continue
        accounts.append({"label": f"账号{i}", "auth": token_raw, "servers": ids})
    if not accounts:
        single_auth = (env.get("ORIHOST_REMEMBER") or env.get("ORI_COOKIE")
                       or env.get("ORIHOST_COOKIE") or "").strip()
        single_ids = _split_ids(env.get("ORIHOST_SERVER_IDS") or env.get("ORIHOST_SERVER_IDS_1"))
        if single_auth and single_ids:
            accounts.append({"label": "默认账号", "auth": single_auth, "servers": single_ids})
    return accounts


# ---------- Turnstile 处理（移植自 katabump，经实测有效） ----------
_EXPAND_JS = """
(function() {
    var ts = document.querySelector('input[name="cf-turnstile-response"]');
    if (!ts) return 'no-turnstile';
    var el = ts;
    for (var i = 0; i < 20; i++) {
        el = el.parentElement;
        if (!el) break;
        var s = window.getComputedStyle(el);
        if (s.overflow === 'hidden' || s.overflowX === 'hidden' || s.overflowY === 'hidden')
            el.style.overflow = 'visible';
        el.style.minWidth = 'max-content';
    }
    document.querySelectorAll('iframe').forEach(function(f){
        if (f.src && f.src.includes('challenges.cloudflare.com')) {
            f.style.width = '300px'; f.style.height = '65px';
            f.style.minWidth = '300px';
            f.style.visibility = 'visible'; f.style.opacity = '1';
        }
    });
    return 'done';
})()
"""

_SOLVED_JS = """
(function(){
    var i = document.querySelector('input[name="cf-turnstile-response"]');
    return !!(i && i.value && i.value.length > 20);
})()
"""

_HAS_TURNSTILE_JS = """
(function(){
    if (document.querySelector('input[name="cf-turnstile-response"]')) return true;
    var fs = document.querySelectorAll('iframe');
    for (var i = 0; i < fs.length; i++) {
        if (fs[i].src && fs[i].src.includes('challenges.cloudflare.com')) return true;
    }
    return false;
})()
"""


# 面板免费方案会插广告：续期对话框弹出时，中间会有个「Download is ready / Tap to proceed」
# 嘅固定遮罩（z-index 好高），正好盖住 Turnstile 组件 —— 唔清走佢，点极都点唔到 checkbox
# （run 35452946138 截图实证：组件白框被广告盖住，Claim Renewal 一直 disabled）。
_JS_KILL_AD = """
(function () {
    var out = [];
    function hide(el, why) {
        try {
            el.style.setProperty('display', 'none', 'important');
            out.push(why + ':' + el.tagName);
        } catch (e) {}
    }
    var markers = ['download is ready', 'tap to proceed'];
    var all = document.querySelectorAll('div,section,aside,iframe,ins');
    for (var i = 0; i < all.length; i++) {
        var el = all[i];
        var t = (((el.innerText || el.textContent) || '')).toLowerCase().slice(0, 400);
        if (!t) continue;
        for (var j = 0; j < markers.length; j++) {
            if (t.indexOf(markers[j]) < 0) continue;
            var p = el;
            for (var k = 0; k < 8 && p.parentElement; k++) {
                var st = window.getComputedStyle(p);
                var z = parseInt(st.zIndex || '0', 10);
                if (st.position === 'fixed' || z >= 100) break;
                p = p.parentElement;
            }
            hide(p, 'marker');
            break;
        }
    }
    var els = document.querySelectorAll('body > *, body > * > *');
    for (var i = 0; i < els.length; i++) {
        var el = els[i];
        var st = window.getComputedStyle(el);
        var z = parseInt(st.zIndex || '0', 10);
        if (st.position !== 'fixed' && st.position !== 'absolute') continue;
        if (z < 900) continue;
        var r = el.getBoundingClientRect();
        if (r.width * r.height < 0.2 * window.innerWidth * window.innerHeight) continue;
        var txt = ((el.innerText || '') + '').toLowerCase();
        if (txt.indexOf('renew your server') >= 0) continue;
        hide(el, 'zindex' + z);
    }
    return out.join(' ') || 'none';
})()
"""

# 上游 woshizaiyu 2026-09-29（536d7e7 / 2c76c89）移植：廣告 iframe 按**域名**攔截。
# 我哋原本嘅 _JS_KILL_AD 只按文案（download is ready）＋高 z-index 掃遮罩，
# 域名黑名單係另一條互補路：連文案都未 load 到嘅廣告 iframe 一樣清得走。
# ⚠️ 明確豁免 challenges.cloudflare.com —— 嗰個係 Turnstile 驗證組件，唔可以殺。
_JS_KILL_AD_IFRAMES = """
(function () {
    var bad = ['n6wxm.com', 'nap5k.com', '5gvci.com', 'jhnwr.com',
               'my.rtmark.net', 'rtmark.net', 'vignette', 'tag.min.js',
               'doubleclick.net', 'googlesyndication', 'adservice.google'];
    var out = [], fs = document.querySelectorAll('iframe');
    for (var i = fs.length - 1; i >= 0; i--) {
        var src = (fs[i].src || '').toLowerCase();
        if (src.indexOf('challenges.cloudflare.com') >= 0) continue;
        for (var j = 0; j < bad.length; j++) {
            if (src.indexOf(bad[j]) >= 0) { out.push(bad[j]); fs[i].remove(); break; }
        }
    }
    return out.join(',') || 'none';
})()
"""

# 上游 2c76c89：廣告關閉按鈕文案係「要關閉」。
# 收窄到只撳「身處 fixed/absolute 高 z-index 浮層」而且唔喺對話框入面嘅嗰粒，
# 避免誤撳面板自己嘅 Close。呢個函式**只喺開續期對話框之前**跑（見 kill_page_ads 註釋），
# 所以就算撞中都唔會撳熄對話框。
_JS_CLICK_AD_CLOSE = """
(function () {
    var want = ['要關閉', '關閉廣告', '关闭广告', '要关闭', '关闭', 'Close', '✕', '×', '✖'];
    var all = document.querySelectorAll('span,button,a,div');
    for (var i = 0; i < all.length; i++) {
        var el = all[i];
        var t = (el.textContent || '').trim();
        if (want.indexOf(t) < 0) continue;
        var r = el.getBoundingClientRect();
        if (r.width === 0 && r.height === 0) continue;
        if (el.closest('[role="dialog"]') || el.closest('[aria-modal="true"]')) continue;
        var p = el, overlay = false;
        for (var k = 0; k < 6 && p; k++) {
            var st = window.getComputedStyle(p);
            if (st.position === 'fixed' || st.position === 'absolute') {
                if (parseInt(st.zIndex || '0', 10) >= 100) overlay = true;
                break;
            }
            p = p.parentElement;
        }
        if (!overlay) continue;
        try { el.click(); } catch (e) { continue; }
        return 'clicked:' + t;
    }
    return 'none';
})()
"""

# ── 2026-10-08：面板彈「You have 1 new message! $50,000 credited to your demo account」
# 廣告模態（run 37818019788 截圖實證）。佢帶 role="dialog"/aria-modal，
# 而舊 _JS_CLICK_AD_CLOSE 見到 role=dialog 就 `continue` 跳過 → 永遠清唔到 →
# 「Claim Renewal」一直 disabled → Turnstile 根本唔會 render。
# 所以另寫一個專治佢嘅 killer：認文案（new message / credited to your demo），
# 喺模態內部撳 Close/Continue；冇掣就直接 remove 成個模態。
_JS_KILL_AD_MODAL = """
(function () {
    var keys = ['new message', 'credited to your demo', 'demo account', '50,000'];
    var boxes = document.querySelectorAll('[role="dialog"],[aria-modal="true"],div');
    for (var i = 0; i < boxes.length; i++) {
        var t = (boxes[i].innerText || '').toLowerCase();
        if (!t || t.length > 400) continue;
        var hit = false;
        for (var k = 0; k < keys.length; k++) {
            if (t.indexOf(keys[k]) >= 0) { hit = true; break; }
        }
        if (!hit) continue;
        var cand = boxes[i].querySelectorAll('button,a,span,div');
        for (var j = 0; j < cand.length; j++) {
            var txt = (cand[j].textContent || '').trim();
            if (['Close', 'Continue', '关闭', '繼續', '继续', '\u2715', '\u00d7'].indexOf(txt) < 0) continue;
            var r = cand[j].getBoundingClientRect();
            if (r.width === 0 && r.height === 0) continue;
            try { cand[j].click(); return 'closed:' + txt; } catch (e) {}
        }
        try { boxes[i].remove(); return 'removed'; } catch (e) {}
    }
    return 'none';
})()
"""

# ── 2026-10-08：SeleniumBase 一旦行過 execute_cdp_cmd，之後 execute_script 會
# **靜默返 None**（同 wait_for_ready_state_complete 嗰個坑同源；run 37817479347 實證：
# 第 1 輪正常、之後全部「读 Turnstile 状态失败: None」）。
# js_eval 做兩件事：未掂過 CDP 就用 execute_script；一旦見到 None 就永久轉
# Runtime.evaluate，令全條鏈（清廣告、搵掣、點掣）喺兩種模式下都行得通。
_CDP_MODE = False


def js_eval(sb, script):
    global _CDP_MODE
    if not _CDP_MODE:
        try:
            v = sb.execute_script(script)
            if v is not None and v != "":
                return v
            _CDP_MODE = True
            print("  ℹ️ execute_script 返 None → 轉 CDP Runtime.evaluate")
        except Exception:
            _CDP_MODE = True
    try:
        r = sb.driver.execute_cdp_cmd("Runtime.evaluate", {
            "expression": script, "returnByValue": True, "awaitPromise": False})
        return (r.get("result") or {}).get("value")
    except Exception:
        return None


_JS_TS_INFO = """
(function () {
    var inp = document.querySelector('input[name="cf-turnstile-response"]');
    var out = {token: inp ? String(inp.value || '').length : -1, rects: []};
    var fs = document.querySelectorAll('iframe');
    for (var i = 0; i < fs.length; i++) {
        var src = fs[i].src || '';
        if (src.indexOf('challenges.cloudflare.com') < 0) continue;
        var r = fs[i].getBoundingClientRect();
        out.rects.push([Math.round(r.left), Math.round(r.top),
                        Math.round(r.width), Math.round(r.height)]);
    }
    return JSON.stringify(out);
})()
"""


def kill_ad_overlay(sb):
    """清走盖住 Turnstile 嘅广告遮罩；返清咗几多个。

    2026-10-08：加埋專治「You have 1 new message」模態嘅 killer（見 _JS_KILL_AD_MODAL）。
    """
    parts = []
    try:
        parts.append("modal=" + str(js_eval(sb, _JS_KILL_AD_MODAL)))
    except Exception as e:
        parts.append("modal=err:" + str(e)[:40])
    try:
        parts.append("overlay=" + str(js_eval(sb, _JS_KILL_AD)))
    except Exception as e:
        parts.append("overlay=err:" + str(e)[:40])
    return " ".join(parts)


def kill_page_ads(sb):
    """页面级清广告（上游 536d7e7 + 2c76c89 移植）。

    同 handle_turnstile 入面嘅 kill_ad_overlay 唔同，呢個係**開續期對話框之前**跑：
    上游實測廣告 iframe/浮層會蓋住服务器页嘅续期入口，令按鈕搵唔到。
    返回三段結果字串，方便睇 log。
    """
    parts = []
    for name, js in (("iframe", _JS_KILL_AD_IFRAMES),
                     ("overlay", _JS_KILL_AD),
                     ("close", _JS_CLICK_AD_CLOSE)):
        try:
            parts.append(f"{name}={sb.execute_script(js)}")
        except Exception as e:
            parts.append(f"{name}=err:{str(e)[:40]}")
    return " ".join(parts)


def wait_page_ready(sb, timeout=15):
    """等页面完全载入再做嘢（上游 218a50f 移植）。

    ⚠️ 2026-09-30 修正（run 36675590134 / 36675791322 實證）：
    原本先叫 `sb.wait_for_ready_state_complete()`，之後 `execute_script` 會**靜默返 None**
    （面板 API 讀唔到（None）→ watchdog 誤報「狀態讀取失敗」＋發紅單 TG）。
    同一份代碼換返 8f9b382（未加呢個 wait）即恢復 `renewal=14 天`，
    所以呢度**淨用純 JS 輪詢**，唔再掂 SeleniumBase 嘅 wait API（佢會令 driver 轉 CDP/斷線）。
    返 True/False 之外，會 print 一句健康檢查，方便下次一眼睇到 driver 有冇斷。
    """
    end = time.time() + max(5, timeout)
    ok = False
    while time.time() < end:
        try:
            if str(sb.execute_script("(function(){return document.readyState})()")) == "complete":
                ok = True
                break
        except Exception:
            pass
        time.sleep(1)
    print(f"    （page ready: {ok}, js_health: {js_health(sb)}）")
    return ok


def ts_info(sb):
    """读 Turnstile 状态：token 长度 + 组件 iframe 视口坐标"""
    try:
        raw = sb.execute_script(_JS_TS_INFO)
    except Exception as e:
        return None, "err:" + str(e)[:60]
    try:
        return json.loads(raw), None
    except Exception:
        return None, str(raw)[:80]


def ts_info_cdp(sb):
    """用 CDP `Runtime.evaluate` 讀 Turnstile 狀態（token 長度 + iframe rects）。

    點解唔用 execute_script（2026-10-08 實證 run 37817479347）：
      SeleniumBase 一旦行過 `driver.execute_cdp_cmd(...)`，之後 `execute_script`
      會**靜默返 None**（唔會拋錯）—— 同 `wait_for_ready_state_complete` 嗰個坑
      同源。症狀：第 1 輪正常、之後全部「读 Turnstile 状态失败: None」。
      ⇒ 一轉 CDP 就全程 CDP：讀狀態用 Runtime.evaluate、點擊用 Input.dispatchMouseEvent。
    """
    global _CDP_MODE
    _CDP_MODE = True
    try:
        r = sb.driver.execute_cdp_cmd("Runtime.evaluate", {
            "expression": _JS_TS_INFO, "returnByValue": True, "awaitPromise": False})
        val = (r.get("result") or {}).get("value")
        if val is None:
            return None, "cdp 回 None"
        return json.loads(val), None
    except Exception as e:
        return None, "cdp err:" + str(e)[:70]


def ts_frames_cdp(sb):
    """用 CDP frame 樹搵 challenge iframe 嘅真實方框（返 (rects, err)）。

    點解要咁做（2026-10-08 實證 run 37815182349）：
      `_JS_TS_INFO` 行 `document.querySelectorAll('iframe')` → 報 `iframe=[]`，
      8 輪都「未见到 Turnstile iframe」→ **由頭到尾冇撳過**。
      原因：Cloudflare 將挑戰 iframe 渲染喺 **closed shadow DOM** 入面，
      任何 DOM 查詢（querySelectorAll / locator）都搵唔到；
      唯一覆蓋得到嘅方法係行**瀏覽器層 frame 樹**（CDP `Page.getFrameTree`），
      再用 frameOwner(backendNodeId) → `DOM.getBoxModel` 攞 iframe 喺頁面嘅方框。
      （同一堵牆 fridaydev 10-08 都撞過，佢用 Playwright 嘅 page.frames 解決。）

    返 rects = [[x, y, w, h], ...]（頁面座標，同 _JS_TS_INFO 同格式），err = 錯誤字串或 None。
    """
    global _CDP_MODE
    _CDP_MODE = True
    rects = []
    try:
        tree = sb.driver.execute_cdp_cmd("Page.getFrameTree", {})
    except Exception as e:
        return rects, "getFrameTree err:" + str(e)[:60]
    stack = [tree.get("frameTree")]
    while stack:
        node = stack.pop()
        if not node:
            continue
        for ch in (node.get("childFrames") or []):
            stack.append(ch)
        fr = node.get("frame") or {}
        url = fr.get("url") or ""
        if "challenges.cloudflare.com" not in url:
            continue
        owner = fr.get("frameOwner")          # subframe 先有 backendNodeId
        if not owner:
            continue
        try:
            nid = sb.driver.execute_cdp_cmd(
                "DOM.pushNodesByBackendIdsToFrontend",
                {"backendNodeIds": [owner]})["nodeIds"][0]
            box = sb.driver.execute_cdp_cmd("DOM.getBoxModel", {"nodeId": nid})["model"]["border"]
            xs, ys = box[0::2], box[1::2]
            x, y = int(min(xs)), int(min(ys))
            w, h = int(max(xs) - min(xs)), int(max(ys) - min(ys))
            if w > 10 and h > 10:
                rects.append([x, y, w, h])
        except Exception as e:
            print(f"  ⚠️ CDP 攞 challenge frame 方框失敗: {str(e)[:80]}")
    return rects, None


def ts_click_cdp(sb, x, y):
    """用 CDP 派发真鼠标事件点 checkbox（唔依赖 X11/pyautogui，坐标係视口坐标）"""
    try:
        for t in ("mouseMoved", "mousePressed", "mouseReleased"):
            sb.driver.execute_cdp_cmd("Input.dispatchMouseEvent", {
                "type": t, "x": int(x), "y": int(y), "button": "left", "clickCount": 1,
            })
            time.sleep(0.08)
        return "cdp-ok"
    except Exception as e:
        return "cdp-err:" + str(e)[:60]


def handle_turnstile(sb) -> bool:
    """处理续期对话框内嘅 Cloudflare Turnstile。

    经验（run 35452946138）：uc_gui_click_captcha 点极都过唔到，因为
    ① 面板免费方案会弹广告遮罩（Download is ready）盖住组件
    ② pyautogui 嘅盲点坐标撞正遮罩
    所以改成：先清广告 → 读组件真实坐标 → 用 CDP 派发真鼠标事件点 checkbox。

    2026-10-08 大修（run 37815182349 / 37817479347 實證）：
      · `iframe=[]`：`_JS_TS_INFO` 行 querySelectorAll('iframe')，但挑戰 iframe 收喺
        **closed shadow DOM** → DOM 查詢永遠搵唔到 → 8 輪白等、由頭到尾冇撳過。
        ⇒ 落 CDP `Page.getFrameTree` 反查（同 fridaydev 用 page.frames 同一個道理）。
      · 一掂過 CDP，`execute_script` 就靜默返 None ⇒ 全程 CDP（Runtime.evaluate 讀狀態）。
      · 清廣告（execute_script）必須喺轉 CDP **之前**做。
    """
    print("🔍 处理 Cloudflare Turnstile 验证...")
    time.sleep(2)

    # ── JS 階段（趁 execute_script 仲正常）：靜默通過快檢 + 清廣告 ──
    try:
        if sb.execute_script(_SOLVED_JS):
            print("✅ 已静默通过")
            return True
    except Exception:
        pass
    try:
        killed = kill_ad_overlay(sb)
    except Exception:
        killed = "err"

    # ── CDP 階段：之後全部唔再掂 execute_script ──
    frames_probed = False
    for attempt in range(8):
        info, err = ts_info_cdp(sb)
        if info is None:
            print(f"  ⚠️ 读 Turnstile 状态失败: {err}")
            time.sleep(2)
            continue
        tok, rects = info.get("token"), info.get("rects") or []
        if isinstance(tok, int) and tok > 20:
            print(f"✅ Turnstile 通过（第 {attempt + 1} 轮，token 长度 {tok}）")
            return True
        if attempt == 0:
            print(f"  组件: token_len={tok} iframe={rects} 清广告={killed}")
        if not rects and not frames_probed:
            # 可見 DOM 搵唔到 ≠ 冇：挑戰 iframe 收喺 closed shadow DOM，落 CDP frame 樹反查
            frames_probed = True
            cdp_rects, cerr = ts_frames_cdp(sb)
            if cdp_rects:
                rects = cdp_rects
                print(f"  🔎 可见 DOM 冇 iframe，但 CDP frame 樹搵到 {len(cdp_rects)} 個"
                      f"（closed shadow DOM）→ {cdp_rects[0]}")
        if not rects:
            print(f"  ⚠️ 第 {attempt + 1} 轮：可见 DOM 同 CDP frame 樹都未见到"
                  f" Turnstile iframe，等一等再试")
            time.sleep(3)
            continue
        x, y, w, h = rects[0]
        # checkbox 喺组件左侧约 24px 处、垂直居中
        cx, cy = x + 24, y + max(h // 2, 16)
        res = ts_click_cdp(sb, cx, cy)
        print(f"  ️ 第 {attempt + 1} 轮点 checkbox ({cx},{cy}) → {res}")
        for _ in range(10):
            time.sleep(1)
            info, _ = ts_info_cdp(sb)
            if info and isinstance(info.get("token"), int) and info["token"] > 20:
                print(f"✅ Turnstile 通过（第 {attempt + 1} 轮，token 长度 {info['token']}）")
                return True
        print(f"  ⚠️ 第 {attempt + 1} 轮未通过，重试...")
    print("  ❌ Turnstile 8 轮均失败")
    return False


def page_text(sb) -> str:
    try:
        return (sb.get_page_source() or "").lower()
    except Exception:
        return ""


# 面板入口按钮名字系「Renew」（停权页「Renew Server」），且按钮内可能只有裸文字节点 + SVG 图标
# （run 35450509946 实测：BUTTON[Renew] 存在，但旧逻辑要求「无子元素」→ 误判为冇按钮）。
# 所以改成：先收集所有文案精确匹配的节点，取**树最深**嘅一个做锚点。
_JS_RENEW_PROBE = """
(function () {
    var all = document.querySelectorAll('button,a,div,span,p,strong');
    var match = null, depth = 0;
    for (var i = 0; i < all.length; i++) {
        var el = all[i];
        var t = (el.textContent || '').trim();
        if (!t || t.length > 40) continue;
        var lt = t.toLowerCase();
        if (lt.indexOf('renew limit reached') >= 0) return 'limit';
        if (lt !== 'renew' && lt !== 'renew server') continue;
        var r = el.getBoundingClientRect();
        if (r.width === 0 && r.height === 0 && el.offsetParent === null) continue;
        var a = el.closest('a');
        if (a) {
            var h = a.getAttribute('href') || '';
            if (h.indexOf('/premium') >= 0 || h.indexOf('/services') >= 0) continue;
        }
        var d = 0, n = el;
        while (n.parentElement) { d++; n = n.parentElement; }
        if (d > depth) { depth = d; match = el; }
    }
    return match ? 'ok' : 'none';
})()
"""

# 注意：SeleniumBase 的 CDP 模式（driver 断线后 is_cdp_swap_needed）会用 cdp.evaluate(script)
# 执行，唔支持 arguments[..]；所以文案直接嵌进脚本，唔用 execute_script 传参。
#
# 两段式定位（run 35450777798 血案：子串匹配「read article」会命中说明段里面嘅
# <strong>Read Article</strong> 内联字，佢比真正嘅按钮更深 → 拣错元素、白白点咗空气）：
#   ① 先揀位于互动容器（button/a/[role=button]）内部、树最深嘅候选 → 正路
#   ② 冇先退而求其次揀任意最深候选
_JS_CLICK_BY_TEXT = """
(function () {
    var want = %s;
    var exact = %s;
    var all = document.querySelectorAll('button,a,div,span,p,strong');
    var best = null, bestDepth = -1, fallback = null, fallbackDepth = -1;
    for (var i = 0; i < all.length; i++) {
        var el = all[i];
        var t = (el.textContent || '').trim();
        if (!t || t.length > 60) continue;
        var lt = t.toLowerCase();
        if (exact) { if (lt !== want) continue; }
        else if (lt.indexOf(want) < 0) continue;
        var r = el.getBoundingClientRect();
        if (r.width === 0 && r.height === 0 && el.offsetParent === null) continue;
        var anc = el.closest('a');
        if (anc) {
            var ah = anc.getAttribute('href') || '';
            if (ah.indexOf('/premium') >= 0 || ah.indexOf('/services') >= 0) continue;
        }
        var d = 0, n = el;
        while (n.parentElement) { d++; n = n.parentElement; }
        var inter = el.closest('button,a,[role=button],[role=tab]');
        if (inter) {
            if (d > bestDepth) { bestDepth = d; best = el; }
        } else if (d > fallbackDepth) { fallbackDepth = d; fallback = el; }
    }
    var match = best || fallback;
    if (!match) return 'not-found';
    var tgt = match.closest('button,a,[role=button],[role=tab]') || match;
    var href = (tgt.getAttribute && (tgt.getAttribute('href') || '')) || '';
    if (href.indexOf('/premium') >= 0 || href.indexOf('/services') >= 0) return 'not-found';
    try { tgt.scrollIntoView({block: 'center'}); } catch (e) {}
    if (tgt.disabled) return 'disabled:' + (tgt.textContent || '').trim().slice(0, 30);
    tgt.click();
    return 'clicked:' + tgt.tagName + ':' + (tgt.textContent || '').trim().slice(0, 30);
})()
"""


# 面板「Renew your server」对话框用 window.open('about:blank','_blank') 开文章页。
# JS 合成 click 冇 user activation → Chrome 直接当弹窗拦截 → window.open 返 null →
# 面板弹 danger flash 并停在 confirm 状态，永远到唔到 ready（run 35450777798 实证）。
# 所以先装垫片：返一个假 window，令面板行得落去；真正开文章页由 Python 侧用 CDP 做。
_JS_PATCH_WINDOW_OPEN = """
(function () {
    window.__oriArticleUrl = '';
    window.__oriDummyWin = null;
    if (window.__oriPatched) return 'already';
    window.__oriPatched = true;
    window.open = function (u, n, f) {
        var w = { closed: false, opener: null, name: n || '', __oriDummy: true };
        w.location = {};
        Object.defineProperty(w.location, 'href', {
            get: function () { return window.__oriArticleUrl || 'about:blank'; },
            set: function (v) { window.__oriArticleUrl = v || ''; }
        });
        w.close = function () { w.closed = true; };
        w.focus = function () {};
        window.__oriDummyWin = w;
        if (u && u !== 'about:blank') { window.__oriArticleUrl = u; }
        return w;
    };
    return 'patched';
})()
"""

_JS_GET_ARTICLE_URL = "(function () { return window.__oriArticleUrl || ''; })()"

# 诊断用：钩 XHR，记录面板 /renew/* 请求嘅原始回包（主要想知 dwell_seconds 几多）
_JS_PATCH_XHR = """
(function () {
    if (window.__oriXhrPatched) return 'already';
    window.__oriXhrPatched = true;
    var O = XMLHttpRequest.prototype.open, S = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function (m, u) { this.__oriUrl = u; return O.apply(this, arguments); };
    XMLHttpRequest.prototype.send = function (b) {
        var self = this;
        this.addEventListener('load', function () {
            if ((self.__oriUrl || '').indexOf('/renew') >= 0) {
                window.__oriLastRenew = self.__oriUrl + ' [' + self.status + '] ' + String(self.responseText).slice(0, 300);
            }
        });
        return S.apply(this, arguments);
    };
    return 'patched';
})()
"""

_JS_DIAG = """
(function () {
    var b = document.body;
    var out = {
        href: location.href.slice(0, 90),
        patched: !!window.__oriPatched,
        dummy: !!window.__oriDummyWin,
        closed: !!(window.__oriDummyWin && window.__oriDummyWin.closed),
        article: (window.__oriArticleUrl || '').slice(0, 80),
        renew: (window.__oriLastRenew || '').slice(0, 200),
        modaltxt: '',
        state: ''
    };
    var all = document.querySelectorAll('div');
    for (var i = all.length - 1; i >= 0; i--) {
        var t = all[i].textContent || '';
        if (t.indexOf('Renew your server') >= 0 && t.length < 900) {
            out.modaltxt = t.slice(0, 260).replace(/\s+/g, ' ');
            break;
        }
    }
    if (!out.modaltxt) out.modaltxt = 'no-modal';
    out.state = (function () {
        var t = ((b && (b.innerText || b.textContent)) || '').toLowerCase();
        if (t.indexOf('you renewed recently') >= 0) return 'cooldown';
        if (t.indexOf('thanks for reading') >= 0) return 'ready';
        if (t.indexOf('you can claim your renewal in') >= 0) return 'reading';
        if (t.indexOf('click read article to open') >= 0) return 'confirm';
        return 'closed';
    })();
    return JSON.stringify(out);
})()
"""

_JS_MODAL_STATE = """
(function () {
    var b = document.body;
    var t = ((b && (b.innerText || b.textContent)) || '').toLowerCase();
    if (t.indexOf('you renewed recently') >= 0) return 'cooldown';
    if (t.indexOf('thanks for reading') >= 0) return 'ready';
    if (t.indexOf('you can claim your renewal in') >= 0) return 'reading';
    if (t.indexOf('click read article to open') >= 0) return 'confirm';
    return 'closed';
})()
"""

# 同步 XHR：CDP 模式下 execute_async_script 会走 cdp.evaluate（唔支持 callback）→ 必 timeout，
# 所以读 API 一律用 sync XHR，唔用 async script。
_JS_SYNC_GET_SERVER = """
(function () {
    try {
        var x = new XMLHttpRequest();
        x.open('GET', '/api/client/servers/%s', false);
        x.setRequestHeader('Accept', 'application/json');
        x.send(null);
        var d = JSON.parse(x.responseText);
        var a = (d && d.attributes) || {};
        return JSON.stringify({renewal: a.renewal, renewable: a.renewable, status: a.status});
    } catch (e) { return 'ERR ' + e; }
})()
"""


def _js_click_script(text, exact):
    return _JS_CLICK_BY_TEXT % (json.dumps(text.lower()), "true" if exact else "false")


def click_by_text(sb, text, timeout=10, exact=False):
    """按文案点击（纯 JS 路）。

    面板 UI kit 嘅 button/a 经 WebDriver 读 .text 全返空（实测 34 个 element 全部系空字符串），
    而且 driver 断线后 SeleniumBase 会转 CDP 模式、element 属性访问会抛
    "'NoneType' object is not callable" → 只能用 document.querySelectorAll + click()，
    事件会冒泡到 React handler，效果等同真人点击。
    返回 'clicked:...' / 'disabled:...' / 'not-found' / 'js-err:...'
    """
    script = _js_click_script(text, exact)
    end = time.time() + timeout
    last = "not-found"
    while time.time() < end:
        try:
            raw = js_eval(sb, script)          # 2026-10-08：CDP 模式下 execute_script 會返 None
            last = "js-null" if raw is None or raw == "" else str(raw)
        except Exception as e:
            last = "js-err:" + str(e)[:90]
            time.sleep(1)
            continue
        if str(last).startswith("clicked") or str(last).startswith("disabled"):
            return last
        time.sleep(1)
    return last


def open_renew_dialog(sb, timeout=25):
    """點開续期对话框。

    面板 2026-09 改版：服务器页上的入口按钮文案系「Renew」（停权页系「Renew Server」），
    「Renew Now」/「Read Article」只出现在点击之后弹出嘅对话框里面
    （且「Renew Now」只有 ad-free 帐号先见到）。
    返回 'ok' 已点开 / 'limit' 已达上限 / None 找唔到。
    """
    end = time.time() + timeout
    probe = ""
    while time.time() < end:
        try:
            probe = sb.execute_script(_JS_RENEW_PROBE) or ""
        except Exception as e:
            probe = "err:" + str(e)[:90]
        if probe == "limit":
            return "limit"
        if probe == "ok":
            res = click_by_text(sb, "renew", timeout=6, exact=True)
            print(f"  \U0001f5b1\ufe0f 点续期入口: {res}")
            if str(res).startswith("clicked"):
                return "ok"
        time.sleep(1)
    print("    probe:", probe)
    return None


def dump_page_debug(sb, sid):
    """搵唔到续期入口时嘅现场取证：整页文字 + button/a 文案 + 面板 API 的 renewal 字段"""
    print("  \U0001f9ea 现场诊断：")
    try:
        print("    URL:", sb.execute_script("(function(){return location.href})()"))
    except Exception as e:
        print("    URL 读取失败:", str(e)[:100])
    try:
        txt = sb.execute_script(
            "(function(){var b=document.body;return (b&&(b.innerText||b.textContent))||''})()"
        ) or ""
        print("    --- 整页文字（前 1500 字）---")
        print("    " + txt[:1500].replace("\n", " | "))
    except Exception as e:
        print("    文字读取失败:", str(e)[:120])
    try:
        js = """
        (function () {
            var els = document.querySelectorAll('button,a');
            var out = [];
            for (var i = 0; i < els.length; i++) {
                var e = els[i];
                var t = (e.textContent || '').trim().slice(0, 24);
                out.push(e.tagName + '[' + t + '|vis=' + (e.offsetParent !== null) + ']');
            }
            return out.join(' ');
        })()
        """
        print("    --- button/a 文案 ---")
        print("    " + str(sb.execute_script(js))[:1800])
    except Exception as e:
        print("    按钮枚举失败:", str(e)[:120])
    try:
        print("    --- 面板 API ---", sb.execute_script(_JS_SYNC_GET_SERVER % sid))
    except Exception as e:
        print("    API 诊断失败:", str(e)[:150])


def api_renewal(sb, sid):
    """直接同步读面板 API 嘅 renewal 天数（唔靠页面文字，最可信）

    ⚠️ 2026-09-30：`execute_script` 喺 CDP 模式可以**靜默返 None**（唔 throw）。
    舊碼一撞到就當「讀唔到」→ 發紅單 TG。而家重試 3 次再算失敗，並印健康檢查。
    """
    last = None
    for i in range(3):
        try:
            raw = sb.execute_script(_JS_SYNC_GET_SERVER % sid)
        except Exception as e:
            last = "err:" + str(e)[:60]
            raw = None
        if raw not in (None, "", "null", "ERR null"):
            try:
                info = json.loads(raw)
                if info:
                    return info, None
                last = str(raw)[:80]
            except Exception:
                last = str(raw)[:80]
        else:
            last = "execute_script 返 None（driver 可能轉咗 CDP）"
            print(f"    ⚠️ 面板 API 第 {i+1} 次讀唔到（{last}），js_health: {js_health(sb)}")
        if i < 2:
            time.sleep(2)
    return None, last


_RE_COUNTDOWN = re.compile(r"claim your renewal in[^0-9]{0,140}?(\d{1,4})", re.I)


def dialog_countdown(sb):
    """从 page source 抽对话框倒计时剩余秒数（HTML 里係 claim your renewal in <strong>N</strong>）"""
    try:
        src = re.sub(r"\s+", " ", sb.get_page_source() or "")
    except Exception:
        return None
    m = _RE_COUNTDOWN.search(src)
    return int(m.group(1)) if m else None


def js_health(sb):
    """CDP 模式下 createTarget 后 execute_script 可能静默返 None，先探一探"""
    try:
        v = sb.execute_script("(function(){return 'pong:' + (1+1)})()")
    except Exception as e:
        return "err:" + str(e)[:60]
    return repr(v)


def detect_state(sb):
    """读对话框状态；JS 路返空时退而用 page_source 判断（两路互不依赖）"""
    try:
        v = sb.execute_script(_JS_MODAL_STATE) or ""
    except Exception:
        v = ""
    if v:
        return v
    src = page_text(sb)
    if "you renewed recently" in src:
        return "cooldown"
    if "thanks for reading" in src:
        return "ready"
    if "you can claim your renewal in" in src:
        return "reading"
    if "click read article to open" in src:
        return "confirm"
    return ""


def print_diag(sb, tag=""):
    try:
        print(f"    \U0001f9ea 诊断{tag}: {sb.execute_script(_JS_DIAG)}")
    except Exception as e:
        print(f"    \U0001f9ea 诊断{tag} 失败: {str(e)[:120]}")


def wait_modal_state(sb, target, timeout, note=""):
    """等 Renew 对话框走到指定状态（confirm → reading → ready/closed）"""
    end = time.time() + timeout
    last = ""
    polls = 0
    while time.time() < end:
        polls += 1
        last = detect_state(sb)
        if last == target:
            return last
        cd = dialog_countdown(sb)
        if polls <= 4 or polls % 5 == 0:
            print(f"    （第 {polls} 次轮询 state={last!r} 倒计时={cd}）")
        if polls == 1:
            print(f"    JS 健康检查: {js_health(sb)}")
        time.sleep(3)
    print(f"    （等 {target} 超时{note}，最后状态={last!r}，倒计时={dialog_countdown(sb)}，轮询 {polls} 次）")
    return last


def read_renew_result(sb, sid, days_before=None) -> dict:
    """点完 Claim / Renew Now 之后读结果。

    面板成功后会 window.location.reload()，所以先用 API 对比续期天数最稳，
    页面文字只做辅助（唔再靠 'renewed' 之类模糊关键字）。
    """
    time.sleep(8)
    src = page_text(sb)
    if "renew limit reached" in src:
        return {"status": "\u23ed\ufe0f 跳过", "message": "已达续期上限（Renew Limit Reached）"}
    info, err = api_renewal(sb, sid)
    if info:
        days = info.get("renewal")
        print(f"    API：renewal={days} renewable={info.get('renewable')} status={info.get('status')}")
        if days_before is not None and isinstance(days, (int, float)) and days > days_before:
            # 上游 e8c5de8 移植：续期成功留一张截图做证据（workflow 照旧 upload *.png）
            try:
                sb.save_screenshot(f"renew_success_{sid}.png")
                print(f"  📸 成功截图: renew_success_{sid}.png")
            except Exception:
                pass
            return {"status": "\u2705 续期成功",
                    "days": days,
                    "message": f"续期天数 {days_before} → {days} 天（+{round(days - days_before)}）"}
        if days_before is not None and days == days_before:
            sb.save_screenshot(f"claim_noadvance_{sid}.png")
            return {"status": "\u26a0\ufe0f 未知结果",
                    "days": days,
                    "message": f"Claim 已提交，但天数仍系 {days} 天（未后移），请人工确认"}
    if any(k in src for k in ("renewed successfully", "successfully renewed", "extended")):
        try:
            sb.save_screenshot(f"renew_success_{sid}.png")
            print(f"  📸 成功截图: renew_success_{sid}.png")
        except Exception:
            pass
        return {"status": "\u2705 续期成功", "message": "Claim 成功（页面确认）"}
    if err:
        print(f"    API 读取失败: {err}")
    sb.save_screenshot(f"claim_unknown_{sid}.png")
    return {"status": "\u26a0\ufe0f 未知结果", "message": "已点 Claim，但没读到明确成功提示，请人工看一眼面板"}


# ---------- Cookie 免登 ----------
def cookie_login(sb, auth_raw: str) -> bool:
    print("🍪 Cookie 免登...")
    sb.open(PANEL + "/")
    time.sleep(3)
    try:
        sb.delete_all_cookies()
    except Exception:
        pass
    for name, val in parse_auth_cookies(auth_raw):
        try:
            sb.driver.add_cookie({"name": name, "value": val, "domain": "panel.orihost.com", "path": "/"})
        except Exception as e:
            print(f"  ⚠️ cookie 写入失败 {name}: {e}")
    sb.open(PANEL + "/dashboard")
    time.sleep(6)
    src = page_text(sb)
    if "login" in (sb.get_current_url() or "").lower() and ("sign in" in src or "password" in src and "dashboard" not in src):
        print("  ❌ Cookie 登录失败（仍在登录页），remember 可能失效")
        return False
    print("  ✅ 已登录")
    save_rotated_cookies(sb)
    return True


def save_rotated_cookies(sb):
    """免登成功后，把浏览器内最新 remember_web cookie 写回 GitHub secret（防一次性轮换）。"""
    try:
        import os, base64
        gt = env.get("GH_ROTATE_TOKEN") or env.get("GITHUB_TOKEN")
        repo = env.get("GITHUB_REPOSITORY")  # jardanlau2020/orihost-renew
        if not gt or not repo or "/" not in repo:
            return  # 本地跑冇環境，靜默跳過
        val = None
        cookies = None
        for attempt in range(3):  # driver 重連期間 get_cookies 會斷線，retry 3 次
            try:
                cookies = sb.driver.get_cookies()
                break
            except Exception:
                time.sleep(2)
        if cookies is None:
            try:  # 兜底：driver API 断线时走 CDP 直接读 cookie store
                cookies = (sb.driver.execute_cdp_cmd("Network.getAllCookies", {}) or {}).get("cookies") or []
            except Exception:
                cookies = None
        if not cookies:
            print("  ℹ️ 拿不到浏览器 cookies（driver 断线），跳过写回")
            return
        for c in cookies:
            if c["name"].startswith("remember_web_"):
                val = c["value"]
                break
        import json as _json, base64 as _b64, urllib.parse as _up
        if not val:
            print("  ℹ️ 浏览器内无 remember_web cookie，跳过写回")
            return
        # 格式驗證：確保係正版 Laravel token（防寫壞 secret 害死下一輪）
        try:
            dec = _up.unquote(val)
            payload = _json.loads(_b64.b64decode(dec + "=" * (-len(dec) % 4)))
            assert sorted(payload.keys()) == ["iv", "mac", "tag", "value"], payload.keys()
        except Exception:
            print(f"  ⚠️ remember 格式异常，跳过写回（防寫壞 secret）: {val[:40]}...")
            return
        import requests as _rq
        r = _rq.get(f"https://api.github.com/repos/{repo}/actions/secrets/public-key",
                    headers={"Authorization": f"Bearer {gt}"}, timeout=20)
        kd = r.json()
        from nacl import encoding as _enc, public as _pub
        pk = _pub.PublicKey(kd["key"].encode(), _enc.Base64Encoder())
        enc = _pub.SealedBox(pk).encrypt(val.encode())
        body = {"encrypted_value": base64.b64encode(enc).decode(), "key_id": kd["key_id"]}
        rr = _rq.put(f"https://api.github.com/repos/{repo}/actions/secrets/ORIHOST_REMEMBER",
                     headers={"Authorization": f"Bearer {gt}"}, json=body, timeout=20)
        print(f"  🔁 remember 已轮换写回 secret: HTTP {rr.status_code}")
        # 同步埋 server IDs（其實唔會變，但保險）
    except Exception as e:
        print(f"  ⚠️ 写回 secret 失败（不影响续期）: {str(e)[:120]}")


# ---------- 单台续期 ----------
def renew_one_server(sb, server_uuid: str) -> dict:
    sid = (server_uuid or "").split("-")[0][:8]
    print(f"\n  🖥 [{sid}] 打开服务器页...")
    # 面板路由用的是 8 位短 ID（如 /server/8651e616），填了完整 UUID 也只取前 8 位
    sb.open(f"{PANEL}/server/{sid}")
    time.sleep(8)
    # 上游 218a50f：等 readyState 完全載入先搵按鈕（未 load 完就搵 → 誤判冇入口）
    wait_page_ready(sb)
    time.sleep(2)

    # 先读面板 API 真实状态（最可信）：renewable=False 或 renewal>=18 就係已达上限
    days_before = None
    info, err = api_renewal(sb, sid)
    if info:
        days_before = info.get("renewal")
        print(f"  📊 续期前：renewal={days_before} 天 renewable={info.get('renewable')} status={info.get('status')}")
        d = days_before
        if info.get("renewable") is False or (isinstance(d, (int, float)) and d >= 18):
            return {"status": "\u23ed\ufe0f 跳过",
                    "days": d,
                    "message": f"已达续期上限（API: renewal={d} 天 renewable={info.get('renewable')}）"}
    elif err:
        print(f"  ⚠️ 读续期天数失败: {err}")

    src = page_text(sb)
    if "renew limit reached" in src:
        return {"status": "⏭️ 跳过", "message": "已达续期上限（Renew Limit Reached）"}
    if "expired renewal" in src or "suspended due" in src:
        print("  ⚠️ 服务器因过期被暂停，走续期流程恢复")

    # 1. 打开续期对话框：页面级入口按钮文案系「Renew」（停权页系「Renew Server」）
    print("  🔍 找 Renew 入口按钮...")
    # 上游 536d7e7 / 2c76c89：廣告 iframe / 浮層會蓋住入口，先清一次（唔影響 JS 點擊本身）
    print("  🧹 清页面广告:", kill_page_ads(sb))
    state = open_renew_dialog(sb, timeout=25)
    if state == "limit":
        return {"status": "⏭️ 跳过", "message": "已达续期上限（Renew Limit Reached）"}
    if state is None:
        dump_page_debug(sb, sid)
        try:
            sb.execute_script("window.scrollTo(0, document.body.scrollHeight)")
        except Exception:
            pass
        time.sleep(1)
        sb.save_screenshot(f"no_renew_btn_{sid}.png")
        return {"status": "❌ 续期失败", "message": "没找到 Renew 入口按钮（页面结构可能变了）"}
    time.sleep(4)

    # 1b. 对话框里的两种快路
    dlg_src = page_text(sb)
    if "you renewed recently" in dlg_src:
        return {"status": "⏭️ 跳过", "message": "刚续期过，对话框显示冷却中（You renewed recently）"}
    now_res = click_by_text(sb, "renew now", timeout=5)
    if str(now_res).startswith("clicked"):
        print(f"  ⚡ ad-free 帐号：对话框里直接 Renew Now（{now_res}）")
        # days_before 一定要传：唔传嘅话 read_renew_result 里「续期后 > 续期前」嘅
        # API 增量对比会失效，只能退回模糊嘅页面文字判断（易误判成功）。
        return read_renew_result(sb, sid, days_before)

    # 2. 装 window.open 垫片 → 点 Read Article
    #    面板靠 window.open('about:blank') 开文章页；JS 合成 click 冇 user activation，
    #    Chrome 直接拦截 → window.open 返 null → 面板弹「Please allow pop-ups」并永远停在
    #    confirm 状态（run 35450777798 实证：按钮点中咗，但对话框文字冇变、冇新标签）。
    #    垫片返一个假 window 令面板状态机行得落去；真文章页由 Python 侧用 CDP 开。
    print("  🩹 装 window.open 垫片...")
    try:
        print("    ", sb.execute_script(_JS_PATCH_WINDOW_OPEN))
    except Exception as e:
        print("  ⚠️ 垫片失败:", str(e)[:80])
    try:
        print("    XHR 诊断钩:", sb.execute_script(_JS_PATCH_XHR))
    except Exception as e:
        print("  ⚠️ XHR 钩失败:", str(e)[:80])

    print("  🖱️ 点 Read Article...")
    read_res = click_by_text(sb, "read article", timeout=15)
    print(f"    {read_res}")
    time.sleep(2)

    # 2b. 等面板 POST /renew/begin 返文章 URL，再真开一个标签去读
    art_url = ""
    for _ in range(20):
        try:
            art_url = str(sb.execute_script(_JS_GET_ARTICLE_URL) or "")
        except Exception:
            art_url = ""
        if art_url.startswith("http"):
            break
        time.sleep(1)
    # 2b. 唔开真标签：文章页係第三方站（albeu.com），面板根本核验唔到「有冇真读过」，
    #     而且 Target.createTarget 会搞烂 CDP/pydoll 连線 —— 之后所有 execute_script 静默返 None
    #     （run 35452477886 实证：js_health 由 'pong:2' 变 None，click 全部误报 not-found）。
    #     倒计时係面板自己嘅 setInterval，只认佢自己 window.open 返嚟嘅假 window，
    #     所以照等就得。真要去访问一次文章页，用页面内 fetch 就够。
    if art_url.startswith("http"):
        try:
            sb.execute_script(
                "(function(){try{fetch(%s,{credentials:'include',mode:'no-cors'})"
                ".catch(function(){})}catch(e){}return 'fetched'})()" % json.dumps(art_url)
            )
            print("    📄 已用页面内 fetch 触发一次文章请求（唔开新标签）")
        except Exception as e:
            print(f"    （fetch 文章页失败，唔影响倒计时: {str(e)[:60]}）")
    else:
        print(f"  ⚠️ 未拿到文章 URL（面板可能仍在 confirm），read_res={read_res}")

    # 2c. 等对话框由 reading 走到 ready（dwell 秒数由面板自己数）
    print(f"  ⏳ 等文章停留倒计时（最多 {CLAIM_TIMEOUT}s）...")
    cd = dialog_countdown(sb)
    wait_ready = CLAIM_TIMEOUT if not cd else min(max(CLAIM_TIMEOUT, cd + 60), 600)
    print(f"    （面板报倒计时 {cd}s → 最多等 {wait_ready}s）")
    st = wait_modal_state(sb, "ready", wait_ready)
    if st == "cooldown":
        return {"status": "\u23ed\ufe0f 跳过", "message": "刚续期过，对话框显示冷却中（You renewed recently）"}
    if st == "confirm":
        sb.save_screenshot(f"stuck_confirm_{sid}.png")
        return {"status": "\u274c 续期失败", "message": "对话框卡在 confirm（Read Article 未生效/弹窗被拦）"}

    # 3. 过 Turnstile（如果有）→ 点 Claim Renewal
    print("  ⏳ 找 Claim Renewal...")
    ts_state = None
    clicked = False
    n_try = 0
    deadline = time.time() + CLAIM_TIMEOUT
    while time.time() < deadline:
        # 2026-10-08：Claim Renewal 一直 disabled 嘅元兇就係嗰個廣告模態 —— 先閂佢
        try:
            mk = js_eval(sb, _JS_KILL_AD_MODAL)
            if mk and mk != "none":
                print(f"  🧹 已閂廣告模態: {mk}")
                time.sleep(1)
        except Exception:
            pass
        res = click_by_text(sb, "claim renewal", timeout=6)
        sres = str(res)
        if sres.startswith("clicked"):
            print(f"  \U0001f5b1\ufe0f 点 Claim Renewal: {sres}")
            clicked = True
            break
        # 2026-10-08：判據由 _HAS_TURNSTILE_JS 改成 CDP frame 樹。
        # _HAS_TURNSTILE_JS 只係睇「有冇隱藏 token input」——但個 input 一開對話框就存在，
        # 於是 Claim 仲係 disabled 就衝入 handle_turnstile，白燒 8 輪（~30s）然後直接判死，
        # 完全冇等過 Claim 變 enabled（run 37818767183 實證：一次 claim 都未試過）。
        # 真判據＝CDP frame 樹見到 challenges.cloudflare.com 嘅 frame（closed shadow DOM
        # 唯一睇得到嘅方法）；見到就係真挑戰，見唔到就繼續等 Claim。
        if ts_state is None:
            try:
                frames, _ferr = ts_frames_cdp(sb)
            except Exception:
                frames = []
            if frames:
                print(f"  🛡️ 見到真 challenge frame {frames[0]} → 處理 Turnstile")
                ts_state = handle_turnstile(sb)
                if ts_state is False:
                    sb.save_screenshot(f"turnstile_fail_{sid}.png")
                    return {"status": "\u274c 续期失败", "message": "Turnstile 验证 6 次未通过"}
        if not clicked and ts_state is None:
            n_try += 1
            if n_try <= 6 or n_try % 5 == 0:
                print(f"    （第 {n_try} 次：state={detect_state(sb)!r} "
                      f"倒计时={dialog_countdown(sb)} claim={sres[:40]}）")
        time.sleep(3)
    if not clicked:
        sb.save_screenshot(f"no_claim_btn_{sid}.png")
        return {"status": "\u274c 续期失败", "message": "等唔到可点嘅 Claim Renewal（倒计时/验证未完成）"}

    # 6. 读结果
    return read_renew_result(sb, sid, days_before)


# ---------- watchdog：只讀狀態 ----------
def watchdog_one(sb, server_uuid: str) -> dict:
    """排程模式：登入 + 讀面板 API 剩餘天數，唔撳任何續期。"""
    sid = (server_uuid or "").split("-")[0][:8]
    print(f"\n  🖥 [{sid}] 讀續期狀態...")
    sb.open(f"{PANEL}/server/{sid}")
    time.sleep(8)
    # 上游 218a50f：API 讀數都要等頁面 load 完（唔係會拿到空 shell）
    wait_page_ready(sb)
    info, err = api_renewal(sb, sid)
    if not info:
        return {"status": "❌ 狀態讀取失敗", "message": f"面板 API 讀唔到（{err}）"}
    d = info.get("renewal")
    ren = info.get("renewable")
    print(f"  📊 renewal={d} 天 renewable={ren} status={info.get('status')}")
    # days 一齊帶返去，main() 用它算 cron 自我調度（見 updateCronSchedule）
    if isinstance(d, (int, float)) and d <= WATCH_DAYS:
        return {"status": "⏰ 需人手續期", "days": d,
                "message": f"剩 {d} 天（renewable={ren}）→ 去 panel 人手撳 Renew（GHA 過唔到 Turnstile）"}
    return {"status": "✅ 正常", "days": d, "message": f"剩 {d} 天（>{WATCH_DAYS} 天，暫唔使理）"}


# ---------- cron 自我調度（移植自上游 orihost_browser_renew.py / oyz FreezeHost） ----------
# 上游原版只服務 renew：續期成功後把 cron **改寫成**「到期前一天」嘅一次性定時。
# 但本 fork 嘅 cron 係 `0 10 */3 * *`（每 3 日 watchdog 巡檢），直接改寫會令兜底
# 巡檢停擺 —— 一旦算錯日期就靜默死掉，而且再冇任何 run 去修正它。
#
# 所以呢度改成**追加一條、冪等替換**：原有 schedule 一行不動，另外 append 一條
# 帶 `# auto:` 標記嘅窗口定時；下次再調度時只覆蓋帶標記嗰條。
#   on:
#     schedule:
#       - cron: '0 10 */3 * *'                      # 基準：永遠保留
#       - cron: '0 10 15 10 *'  # auto: renew-window=2026-10-15T10:00Z lead=1
#
# 點解唔用「併入 day-of-month」寫法（`0 10 1,4,...,31,15 * *`）：
# 每次 run 都往列表塞一日，跑一個月就會退化成「每日」，基準節奏被自己嘅輸出污染。
# 追加式冇呢個問題：基準線永不可變，auto 行永遠最多一條。

_AUTO_TAG = "# auto: renew-window="
# 只匹配「帶 auto 標記嘅 schedule 行」（連行尾換行），用嚟做冪等替換
_AUTO_LINE_RE = re.compile(
    r"^[ \t]*-[ \t]*cron:[ \t]*(?P<q>['\"])(?P<cron>[^'\"]*)(?P=q)[^\n]*"
    + re.escape(_AUTO_TAG) + r"[^\n]*\n?",
    re.M,
)
# 最後一條普通 schedule cron 行（append 位置）
_CRON_LINE_RE = re.compile(
    r"^[ \t]*-[ \t]*cron:[ \t]*(['\"])[^'\"]*\1[^\n]*$", re.M
)


def _workflow_path():
    for name in ("renew.yml", "renew-browser.yml"):
        p = os.path.join(os.getcwd(), ".github", "workflows", name)
        if os.path.exists(p):
            return p
    return None


def _cron_expr(next_run) -> str:
    """窗口定時表達式：帶月/日（唔用 *），避免殘留行每月誤觸。"""
    return f"0 10 {next_run.day} {next_run.month} *"


def _build_auto_line(next_run, lead_days: int) -> str:
    """生成 auto schedule 行（含機器可讀標記，供下次冪等替換）。"""
    return (
        f"    - cron: '{_cron_expr(next_run)}'"
        f"  {_AUTO_TAG}{next_run.strftime('%Y-%m-%dT%H:%MZ')} lead={lead_days}"
    )


def updateCronSchedule(expires_at, lead_days: int = 1) -> bool:
    """按到期時間追加/更新一條 auto cron，令下次巡檢落在「到期前 lead_days 日」。

    expires_at：ISO 8601 字串（可帶 Z）或 datetime。
    lead_days：renew 模式傳 1（同上游一致）；watchdog 模式傳 WATCH_DAYS。

    回寫走 **Contents API** 而唔係 `git push`：上游靠 checkout 嘅 extraheader +
    GIT_ASKPASS，喺 fork 上容易因權限靜默失敗；API 直接帶 token，成敗一目了然。
    """
    if env.dry_run():
        print("  ℹ️ DRY_RUN 演練，跳過 cron 回寫")
        return False
    if (env.get("GITHUB_ACTIONS") or "").lower() != "true":
        print("  ℹ️ 非 CI 環境，跳過 cron 回寫")
        return False
    repo = env.get("GITHUB_REPOSITORY")
    if "/" not in repo:
        print("  ℹ️ 無 GITHUB_REPOSITORY，跳過 cron 回寫")
        return False
    token = env.get("GH_ROTATE_TOKEN") or env.get("GH_TOKEN")
    if not token:
        print("  ℹ️ 未提供 GH_ROTATE_TOKEN / GH_TOKEN，跳過 cron 回寫")
        return False

    wf = _workflow_path()
    if not wf:
        print("  ⚠️ 未找到 workflow 檔案，跳過 cron 回寫")
        return False

    try:
        # ---- 算目標日（到期前 lead_days 日）----
        if isinstance(expires_at, datetime):
            t = expires_at
        else:
            t = datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)

        def _snap(dt_):
            # 對齊基準 cron 嘅 10:00 UTC。唔對齊嘅話每次 run 算出來嘅分秒都唔同
            # → auto 行次次都變 → 每跑一次就多一個 commit（repo 會被自己刷爆）。
            return dt_.astimezone(timezone.utc).replace(
                hour=10, minute=0, second=0, microsecond=0)

        next_run = _snap(t - timedelta(days=max(0, int(lead_days))))
        if next_run <= datetime.now(timezone.utc):
            # 窗口已過（例如天數讀數偏差），退回「明日 10:00」跑一次確認
            next_run = _snap(datetime.now(timezone.utc) + timedelta(hours=12))

        new_line = _build_auto_line(next_run, max(0, int(lead_days)))

        with open(wf, "r", encoding="utf-8") as f:
            old = f.read()

        # ---- 冪等替換：有 auto 行就換掉，冇就 append 喺最後一條 cron 之後 ----
        prev = _AUTO_LINE_RE.search(old)
        if prev:
            # 只比 cron 表達式，唔比註釋裡嘅時間戳：表達式一樣就當無事發生，
            # 避免「同一日、只係秒數唔同」都觸發一次提交。
            if prev.group("cron") == _cron_expr(next_run):
                print(f"  ℹ️ auto cron 無需變更（{prev.group('cron')}）")
                return False
            updated = old[:prev.start()] + new_line + "\n" + old[prev.end():]
        else:
            last = None
            for last in _CRON_LINE_RE.finditer(old):
                pass
            if last is None:
                print("  ⚠️ workflow 無 cron 行，跳過 cron 回寫")
                return False
            insert_at = last.end()
            updated = old[:insert_at] + "\n" + new_line + old[insert_at:]

        if updated == old:
            print("  ℹ️ cron 無需變更")
            return False

        # ---- Contents API 回寫 ----
        api = f"https://api.github.com/repos/{repo}/contents/.github/workflows/{os.path.basename(wf)}"
        hdr = {"Authorization": f"Bearer {token}",
               "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}
        branch = env.get("GITHUB_REF_NAME") or "main"
        r = requests.get(f"{api}?ref={branch}", headers=hdr, timeout=20)
        if r.status_code != 200:
            print(f"  ⚠️ 讀 workflow 失敗 HTTP {r.status_code}: {r.text[:120]}")
            return False
        sha = r.json().get("sha")
        import base64 as _b64
        body = {
            "message": f"chore(cron): 下次巡檢 {next_run.strftime('%Y-%m-%d %H:%M UTC')}",
            "content": _b64.b64encode(updated.encode("utf-8")).decode(),
            "branch": branch,
        }
        if sha:
            body["sha"] = sha
        r2 = requests.put(api, headers=hdr, json=body, timeout=20)
        if r2.status_code in (200, 201):
            print(f"  ✅ cron 已回寫: {new_line.strip()}")
            return True
        print(f"  ⚠️ cron 回寫失敗 HTTP {r2.status_code}: {r2.text[:160]}")
        return False
    except Exception as e:
        print(f"  ⚠️ cron 回寫異常（不影響續期）: {str(e)[:140]}")
        return False


# ---------- 主入口 ----------
SERVICE = "Orihost"

#: 静默的结果：本仓排程是「每 3 天一次巡检」，「剩 N 天，暂唔使理」是常态，
#: 每次都发 TG 就是噪音。需要人知道的（RENEWED / ALREADY_MAX / UNKNOWN / FAILED）
#: 才打扰人 —— 注意 UNKNOWN 也在打扰之列：watchdog 的「⏰ 需人手續期」就映射成它，
#: 而那条**必须**发出去（GHA 过唔到 Turnstile，续期本来就得人手）。
QUIET_OUTCOMES = frozenset({Outcome.SKIPPED, Outcome.TRANSIENT})


def _target_name(label: str, server_uuid: str) -> str:
    """报告里的目标名：账号标签 + 短 ID（面板地址栏 /server/ 后面那 8 位）。"""
    sid = (server_uuid or "").split("-")[0][:8]
    return f"{label}/{sid}"


def run_all() -> RenewReport:
    """跑完所有账号的所有服务器，返回报告。不做任何 exit、不发 TG。"""
    report = RenewReport(service=SERVICE)
    accounts = load_accounts()
    if not accounts:
        report.add(SERVICE, Outcome.FAILED,
                   detail="未配置账号（需要 ORIHOST_REMEMBER + ORIHOST_SERVER_IDS）")
        return report

    sb_kwargs = {"uc": True, "headless": False,
                 "chromium_arg": "--disable-popup-blocking,--disable-notifications"}
    if IS_PROXY:
        print(f"🔗 挂载代理: {PROXY_STR}")
        sb_kwargs["proxy"] = PROXY_STR
    else:
        print("🌐 未使用代理，直连访问")

    print("🚀 启动浏览器...")
    with SB(**sb_kwargs) as sb:
        try:
            sb.open("https://api.ip.sb/ip")
            print(f"📍 当前出口IP: {sb.get_text('body')}")
        except Exception:
            pass
        for acc in accounts:
            label = acc["label"]
            print(f"\n{'=' * 42}\n {label}：{len(acc['servers'])} 台\n{'=' * 42}")
            if not cookie_login(sb, acc["auth"]):
                # remember token 废了 —— 这是**账号级**失败，不是服务器级的。
                # 原来每台各报一条 TG（3 台就是 3 条一样的「登录失败」），
                # 现在收成一条，把受影响的台数写在目标名里。
                report.add(f"{label}（{len(acc['servers'])} 台）", Outcome.FAILED,
                           detail="Cookie 免登失败，remember token 可能已失效")
                continue
            for sv in acc["servers"]:
                try:
                    r = watchdog_one(sb, sv) if IS_WATCHDOG else renew_one_server(sb, sv)
                except Exception as e:
                    r = {"status": "❌ 状态读取失败" if IS_WATCHDOG else "❌ 续期失败",
                         "message": f"异常: {type(e).__name__}: {str(e)[:110]}"}
                status = r.get("status", "")
                message = r.get("message", "")
                report.add(_target_name(label, sv), _outcome_of(status, message),
                           expire=r.get("days"), detail=_detail_of(status, message))
                print(f"  {status} {message}")
                time.sleep(random.randint(2, 5))
    return report


def main() -> int:
    print("#" * 42)
    print("   Orihost 浏览器自动续期" + ("（代理开）" if IS_PROXY else "（直连）"))
    print(f"   模式 MODE={MODE}"
          + ("（watchdog：只讀狀態 + 到期提醒，唔撳續期）" if IS_WATCHDOG else "（renew：真撳續期）"))
    print("#" * 42)

    try:
        report = run_all()
    except Exception as exc:
        # 浏览器/驱动起不来之类。不能让 traceback 直接甩给 workflow ——
        # 甩出去 action 只看到 "Process completed with exit code 1"，
        # 连是哪一步挂的都看不出来。
        report = RenewReport(service=SERVICE)
        report.add(SERVICE, Outcome.FAILED,
                   detail=f"{type(exc).__name__}: {shorten(str(exc), 140)}")

    # ---------- cron 自我調度（按到期日排下一次巡檢） ----------
    # 攞全部 server 中剩餘天數最少嘅一台算窗口：
    #   watchdog：窗口 = 到期前 WATCH_DAYS 日（即 days 啱啱跌到閾值嗰日）→ 提醒貼住臨界點
    #   renew   ：窗口 = 到期前 1 日（同上游一致）
    # 只喺「表達式真係變咗」先提交，所以穩定嘅到期日下唔會刷 commit。
    # 注意：天數讀數現在存在 TargetResult.expire 上（int = 剩餘天數），
    # 所以這裡要從 report.results 取，而不是原來的裸 dict list。
    days_list = [r.expire for r in report.results if isinstance(r.expire, (int, float))]
    if days_list:
        min_days = min(days_list)
        lead = WATCH_DAYS if IS_WATCHDOG else 1
        expiry = datetime.now(timezone.utc) + timedelta(days=float(min_days))
        print(f"\n⏱ 自我調度：最緊急剩 {min_days} 天（到期 {expiry.date()}）→ 目標 = 到期前 {lead} 日")
        updateCronSchedule(expiry, lead_days=lead)
    else:
        print("\n⏱ 自我調度：本輪無有效天數讀數，跳過")

    if IS_WATCHDOG:
        warn = sum(1 for r in report.results if r.outcome is Outcome.UNKNOWN)
        bad = sum(1 for r in report.results if r.outcome is Outcome.FAILED)
        print(f"\n{'=' * 42}\n📊 watchdog 汇总：{len(report.results)} 台｜{warn} 台需人手續期｜"
              f"{bad} 台讀唔到\n{'=' * 42}")
    else:
        c = report.counts()
        done = c.get("renewed", 0)
        skip = c.get("skipped", 0) + c.get("already_max", 0)
        print(f"\n{'=' * 42}\n📊 汇总：{done} 成功 / {skip} 跳过 / {c.get('failed', 0)} 失败，"
              f"共 {len(report.results)} 台\n{'=' * 42}")

    # 退出码收敛成两档（renew-kit 的 RenewReport.finish）：
    #   0 = 正常（含 SKIPPED / ALREADY_MAX / UNKNOWN / TRANSIENT）
    #   1 = 真失败，需要人
    # 原来是 1/0/2 三档散落在各处，workflow 只看「非零」，分不出轻重。
    notify_tg = any(r.outcome not in QUIET_OUTCOMES for r in report.results)
    return report.finish(notify_tg=notify_tg)


if __name__ == "__main__":
    sys.exit(main())
