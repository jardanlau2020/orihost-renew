#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Orihost 浏览器自动续期（SeleniumBase + 真浏览器）
# 背景：面板 claim 接口强制要求 Cloudflare Turnstile token（GET /api/client/renewal/complete?cf-turnstile-response=xxx），
#       纯 HTTP 调不通（无 token 直接 500），必须用真浏览器点验证。
# 流程：Cookie 免登 → 服务器页 → Renew（打开对话框）→ Read Article（新标签读文章）→ 倒计时 → 点 Turnstile → Claim Renewal
# 参考：katabump-renew-main（同款 Turnstile 处理 + xvfb 无头方案）

import json
import os
import re
import sys
import time
import random
import requests as tg_lib
from datetime import datetime, timezone, timedelta
from urllib.parse import unquote
from seleniumbase import SB

PANEL = "https://panel.orihost.com"
# Laravel 默认 remember cookie 名（yanyumm1 实测 Orihost 可用）
DEFAULT_REMEMBER_NAME = "remember_web_59ba36addc2b2f9401580f014c7f58ea4e30989d"
# 文章页停留秒数（面板 dwell=15，多留 buffer；“过早关闭文章页会被警告”）
ARTICLE_WAIT = int(os.environ.get("ARTICLE_WAIT") or "30")
# Claim 按钮轮询上限
CLAIM_TIMEOUT = int(os.environ.get("CLAIM_TIMEOUT") or "150")

# ---------- 代理 ----------
# 优先级：ORIHOST_PROXY 显式指定 > 工作流 sing-box（IS_PROXY/PROXY_SERVER，由 NODE_LINK 转出）
def _get_proxy():
    explicit = (os.environ.get("ORIHOST_PROXY") or os.environ.get("ORIHOST_GOST_PROXY") or "").strip()
    if explicit:
        scheme = explicit.split("://", 1)[0].lower() if "://" in explicit else ""
        if scheme in ("http", "https", "socks4", "socks5", "socks5h"):
            return explicit
        print(f"  ⚠️ ORIHOST_PROXY 格式不支持 ({scheme}://)，节点链接请填 NODE_LINK")
    if os.environ.get("IS_PROXY", "").lower() == "true":
        srv = (os.environ.get("PROXY_SERVER") or "socks5://127.0.0.1:1080").strip()
        print(f"  🔗 使用 sing-box 代理: {srv}")
        return srv
    return ""

PROXY_STR = _get_proxy()
IS_PROXY = bool(PROXY_STR)

# ---------- Telegram ----------
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN") or ""
TG_CHAT_ID = os.environ.get("TG_CHAT_ID") or ""
if (not TG_BOT_TOKEN or not TG_CHAT_ID) and os.environ.get("TG_BOT"):
    try:
        _cid, _tok = os.environ["TG_BOT"].split(",", 1)
        TG_CHAT_ID = TG_CHAT_ID or _cid.strip()
        TG_BOT_TOKEN = TG_BOT_TOKEN or _tok.strip()
    except Exception:
        pass


def send_tg(msg: str):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return
    try:
        r = tg_lib.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT_ID, "text": msg, "parse_mode": "HTML",
                  "link_preview_options": {"is_disabled": True}},
            timeout=15,
        )
        ok = r.status_code == 200 and r.json().get("ok")
        print(f"  📨 TG {'已发送' if ok else '失败: ' + r.text[:80]}")
    except Exception as e:
        print(f"  TG 发送失败: {e}")


def now_bj():
    return (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S")


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
    accounts = []
    for i in range(1, 20):
        token_raw = (os.environ.get(f"ORIHOST_REMEMBER_{i}") or os.environ.get(f"ORIHOST_COOKIE_{i}") or "").strip()
        ids = _split_ids(os.environ.get(f"ORIHOST_SERVER_IDS_{i}") or "")
        if not token_raw and not ids:
            continue
        if not token_raw or not ids:
            print(f"⚠️ 账号{i} 配置不完整，跳过")
            continue
        accounts.append({"label": f"账号{i}", "auth": token_raw, "servers": ids})
    if not accounts:
        single_auth = (os.environ.get("ORIHOST_REMEMBER") or os.environ.get("ORI_COOKIE") or os.environ.get("ORIHOST_COOKIE") or "").strip()
        single_ids = _split_ids(os.environ.get("ORIHOST_SERVER_IDS") or os.environ.get("ORIHOST_SERVER_IDS_1") or "")
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
    """清走盖住 Turnstile 嘅广告遮罩；返清咗几多个"""
    try:
        return sb.execute_script(_JS_KILL_AD)
    except Exception as e:
        return "err:" + str(e)[:60]


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
    所以改成：先清广告 → 读组件 iframe 真实坐标 → 用 CDP 派发真鼠标事件点 checkbox。
    """
    print("🔍 处理 Cloudflare Turnstile 验证...")
    time.sleep(2)
    try:
        if sb.execute_script(_SOLVED_JS):
            print("✅ 已静默通过")
            return True
    except Exception:
        pass
    for attempt in range(8):
        killed = kill_ad_overlay(sb)
        info, err = ts_info(sb)
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
        if not rects:
            print(f"  ⚠️ 第 {attempt + 1} 轮：未见到 Turnstile iframe，等一等再试")
            time.sleep(3)
            continue
        try:
            sb.execute_script(_EXPAND_JS)
        except Exception:
            pass
        x, y, w, h = rects[0]
        # checkbox 喺组件左侧约 24px 处、垂直居中
        cx, cy = x + 24, y + max(h // 2, 16)
        res = ts_click_cdp(sb, cx, cy)
        print(f"  ️ 第 {attempt + 1} 轮点 checkbox ({cx},{cy}) → {res}")
        for _ in range(10):
            time.sleep(1)
            info, _ = ts_info(sb)
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
            raw = sb.execute_script(script)
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
    """直接同步读面板 API 嘅 renewal 天数（唔靠页面文字，最可信）"""
    try:
        raw = sb.execute_script(_JS_SYNC_GET_SERVER % sid)
    except Exception as e:
        return None, "err:" + str(e)[:60]
    try:
        return json.loads(raw), None
    except Exception:
        return None, str(raw)[:80]


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
            return {"status": "\u2705 续期成功",
                    "message": f"续期天数 {days_before} → {days} 天（+{round(days - days_before)}）"}
        if days_before is not None and days == days_before:
            sb.save_screenshot(f"claim_noadvance_{sid}.png")
            return {"status": "\u26a0\ufe0f 未知结果",
                    "message": f"Claim 已提交，但天数仍系 {days} 天（未后移），请人工确认"}
    if any(k in src for k in ("renewed successfully", "successfully renewed", "extended")):
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
        gt = os.environ.get("GH_ROTATE_TOKEN") or os.environ.get("GITHUB_TOKEN")
        repo = os.environ.get("GITHUB_REPOSITORY")  # jardanlau2020/orihost-renew
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

    # 先读面板 API 真实状态（最可信）：renewable=False 或 renewal>=18 就係已达上限
    days_before = None
    info, err = api_renewal(sb, sid)
    if info:
        days_before = info.get("renewal")
        print(f"  📊 续期前：renewal={days_before} 天 renewable={info.get('renewable')} status={info.get('status')}")
        d = days_before
        if info.get("renewable") is False or (isinstance(d, (int, float)) and d >= 18):
            return {"status": "\u23ed\ufe0f 跳过",
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
        return read_renew_result(sb, sid)

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
        res = click_by_text(sb, "claim renewal", timeout=6)
        sres = str(res)
        if sres.startswith("clicked"):
            print(f"  \U0001f5b1\ufe0f 点 Claim Renewal: {sres}")
            clicked = True
            break
        if ts_state is None:
            try:
                has_ts = sb.execute_script(_HAS_TURNSTILE_JS)
            except Exception:
                has_ts = False
            if has_ts:
                ts_state = handle_turnstile(sb)
                if ts_state is False:
                    sb.save_screenshot(f"turnstile_fail_{sid}.png")
                    return {"status": "\u274c 续期失败", "message": "Turnstile 验证 6 次未通过"}
            else:
                pass  # 统一由下面嘅状态行打印
        if not clicked and ts_state is None:
            n_try += 1
            if n_try <= 4 or n_try % 6 == 0:
                print(f"    （第 {n_try} 次：state={detect_state(sb)!r} 倒计时={dialog_countdown(sb)} claim={sres[:40]}）")
        time.sleep(3)
    if not clicked:
        sb.save_screenshot(f"no_claim_btn_{sid}.png")
        return {"status": "\u274c 续期失败", "message": "等唔到可点嘅 Claim Renewal（倒计时/验证未完成）"}

    # 6. 读结果
    return read_renew_result(sb, sid, days_before)


def fmt_msg(status, label, server_uuid, detail):
    sid = (server_uuid or "").split("-")[0][:8]
    return f"🖥 Orihost 浏览器续期\n{status}\n👤 {label}\n🆔 {sid}\n📌 {detail}\n⏰ {now_bj()}（北京）"


# ---------- 主入口 ----------
def main():
    print("#" * 42)
    print("   Orihost 浏览器自动续期" + ("（代理开）" if IS_PROXY else "（直连）"))
    print("#" * 42)
    accounts = load_accounts()
    if not accounts:
        print("❌ 未配置账号。请设置 ORIHOST_REMEMBER + ORIHOST_SERVER_IDS ...")
        sys.exit(1)

    sb_kwargs = {"uc": True, "headless": False,
                 "chromium_arg": "--disable-popup-blocking,--disable-notifications"}
    if IS_PROXY:
        print(f"🔗 挂载代理: {PROXY_STR}")
        sb_kwargs["proxy"] = PROXY_STR
    else:
        print("🌐 未使用代理，直连访问")

    results = []
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
                for sv in acc["servers"]:
                    info = {"label": label, "server": sv, "status": "❌ 登录失败", "message": "Cookie 免登失败，remember 可能失效"}
                    results.append(info)
                    send_tg(fmt_msg(info["status"], label, sv, info["message"]))
                continue
            for sv in acc["servers"]:
                try:
                    r = renew_one_server(sb, sv)
                except Exception as e:
                    r = {"status": "❌ 续期失败", "message": f"异常: {str(e)[:120]}"}
                info = {"label": label, "server": sv, "status": r["status"], "message": r.get("message", "")}
                results.append(info)
                print(f"  {info['status']} {info['message']}")
                send_tg(fmt_msg(info["status"], label, sv, info["message"]))
                time.sleep(random.randint(2, 5))

    ok = sum(1 for r in results if "成功" in r["status"])
    skip = sum(1 for r in results if "跳过" in r["status"])
    fail = len(results) - ok - skip
    print(f"\n{'=' * 42}\n📊 汇总：{ok} 成功 / {skip} 跳过 / {fail} 失败，共 {len(results)} 台\n{'=' * 42}")
    if fail:
        sys.exit(2)


if __name__ == "__main__":
    main()
