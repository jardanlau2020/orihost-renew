#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Orihost 浏览器自动续期（SeleniumBase + 真浏览器）
# 背景：面板 claim 接口强制要求 Cloudflare Turnstile token（GET /api/client/renewal/complete?cf-turnstile-response=xxx），
#       纯 HTTP 调不通（无 token 直接 500），必须用真浏览器点验证。
# 流程：Cookie 免登 → 服务器页 → Renew（打开对话框）→ Read Article（新标签读文章）→ 倒计时 → 点 Turnstile → Claim Renewal
# 参考：katabump-renew-main（同款 Turnstile 处理 + xvfb 无头方案）

import json
import os
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


def handle_turnstile(sb) -> bool:
    print("🔍 处理 Cloudflare Turnstile 验证...")
    time.sleep(2)
    try:
        if sb.execute_script(_SOLVED_JS):
            print("✅ 已静默通过")
            return True
    except Exception:
        pass
    for _ in range(3):
        try:
            sb.execute_script(_EXPAND_JS)
        except Exception:
            pass
        time.sleep(0.5)
    for attempt in range(6):
        try:
            if sb.execute_script(_SOLVED_JS):
                print(f"✅ Turnstile 通过（第 {attempt} 次尝试）")
                return True
        except Exception:
            pass
        print(f"🖱️ 第 {attempt + 1} 次调用 uc_gui_click_captcha...")
        try:
            sb.uc_gui_click_captcha()
        except Exception as e:
            print(f"⚠️ uc_gui_click_captcha 调用异常: {e}")
        for _ in range(16):
            time.sleep(0.5)
            try:
                if sb.execute_script(_SOLVED_JS):
                    print(f"✅ Turnstile 通过（第 {attempt + 1} 次尝试）")
                    return True
            except Exception:
                pass
        print(f"⚠️ 第 {attempt + 1} 次未通过，重试...")
    print("  ❌ Turnstile 6 次均失败")
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
_JS_CLICK_BY_TEXT = """
(function () {
    var want = %s;
    var exact = %s;
    var all = document.querySelectorAll('button,a,div,span,p,strong');
    var match = null, depth = 0;
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
        if (d > depth) { depth = d; match = el; }
    }
    if (!match) return 'not-found';
    var tgt = match.closest('button,a,[role=button]')
           || match.querySelector('button,a,[role=button]')
           || match;
    var href = (tgt.getAttribute && (tgt.getAttribute('href') || '')) || '';
    if (href.indexOf('/premium') >= 0 || href.indexOf('/services') >= 0) return 'not-found';
    if (tgt.disabled) return 'disabled:' + (tgt.textContent || '').trim().slice(0, 30);
    try { tgt.scrollIntoView({block: 'center'}); } catch (e) {}
    tgt.click();
    return 'clicked:' + tgt.tagName + ':' + (tgt.textContent || '').trim().slice(0, 30);
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
            last = sb.execute_script(script) or "not-found"
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
            "(function(){return document.body ? document.body.innerText : ''})()"
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
        js = (
            "(function(){"
            "function done(v){if(!window.__oriDone){window.__oriDone=1;cb(v);}}"
            "var cb=arguments[arguments.length-1];"
            "setTimeout(function(){done('TIMEOUT(8s)')},8000);"
            "fetch('/api/client/servers/" + sid + "',{credentials:'include',"
            "headers:{'Accept':'application/json'}})"
            ".then(function(r){return r.json()})"
            ".then(function(d){var a=(d&&d.attributes)||d||{};"
            "done(JSON.stringify({renewable:a.renewable,renewal:a.renewal,status:a.status,"
            "keys:Object.keys(a).slice(0,40)}))})"
            ".catch(function(e){done('ERR '+e)})"
            "})()"
        )
        print("    --- 面板 API ---", sb.execute_async_script(js))
    except Exception as e:
        print("    API 诊断失败:", str(e)[:150])


def read_renew_result(sb, sid) -> dict:
    """点完 Claim / Renew Now 之后读页面结果"""
    time.sleep(8)
    src = page_text(sb)
    if "renew limit reached" in src:
        return {"status": "⏭️ 跳过", "message": "已达续期上限（Renew Limit Reached）"}
    if any(k in src for k in ("renewed", "successfully renewed", "renewal successful", "extended")):
        return {"status": "✅ 续期成功", "message": "Claim 成功（页面确认）"}
    if "captcha" in src and "complete" in src:
        return {"status": "❌ 续期失败", "message": "提交后仍提示先完成验证"}
    sb.save_screenshot(f"claim_unknown_{sid}.png")
    return {"status": "⚠️ 未知结果", "message": "已点 Claim，但没读到明确成功提示，请人工看一眼面板"}


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

    # 2. 点 Read Article（会弹新标签；唔切换窗口，等倒计时自己跑完）
    print("  🖱️ 点 Read Article...")
    handles_before = set()
    try:
        handles_before = set(sb.driver.window_handles)
    except Exception:
        pass
    read_res = click_by_text(sb, "read article", timeout=15)
    if str(read_res).startswith("clicked"):
        print(f"  📰 文章页已打开（{read_res}），停留 {ARTICLE_WAIT}s（提前关闭会被警告）...")
        # 等新标签真出现
        opened = False
        for _ in range(10):
            time.sleep(1)
            try:
                if set(sb.driver.window_handles) - handles_before:
                    opened = True
                    break
            except Exception:
                break
        if not opened:
            print("  ⚠️ 未检测到新标签（可能被弹窗拦截），继续尝试")
        time.sleep(ARTICLE_WAIT)
        # 顺手关掉文章标签（失败唔影响；handles_before 空 = 当初读唔到，千祈唔好乱关窗）
        if handles_before:
            try:
                extra = set(sb.driver.window_handles) - handles_before
                for h in extra:
                    sb.driver.switch_to.window(h)
                    sb.driver.close()
                sb.driver.switch_to.window(list(handles_before)[0])
            except Exception as e:
                print("  ⚠️ 关文章标签失败（唔影响续期）:", str(e)[:80])
        else:
            print("  ℹ️ 读唔到 window_handles，文章标签照留（唔影响续期）")
        time.sleep(3)
    else:
        # 可能已经在 reading 状态（倒计时中），直接往下走
        print(f"  ℹ️ 没点到 Read Article（{read_res}），可能已在倒计时，直接等待")
        time.sleep(3)

    # 3+4+5. 等 Claim 可点（期间过 Turnstile；验证组件可能迟啲先渲染，所以每轮都查）
    print("  \u23f3 等倒计时走完，找 Claim Renewal...")
    ts_state = None  # None=未处理过  True=无组件或已过  False=过唔到
    clicked = False
    deadline = time.time() + CLAIM_TIMEOUT + 90
    while time.time() < deadline:
        res = click_by_text(sb, "claim renewal", timeout=6)
        sres = str(res)
        if sres.startswith("clicked"):
            print(f"  \U0001f5b1\ufe0f 点 Claim Renewal: {sres}")
            clicked = True
            break
        if ts_state is None and (sres.startswith("disabled") or "not-found" in sres):
            try:
                has_ts = sb.execute_script(_HAS_TURNSTILE_JS)
            except Exception:
                has_ts = False
            if has_ts:
                ts_state = handle_turnstile(sb)
                if ts_state is False:
                    sb.save_screenshot(f"turnstile_fail_{sid}.png")
                    return {"status": "\u274c 续期失败", "message": "Turnstile 验证 6 次未通过"}
            # 冇见到组件就保持 None，下轮再查
        time.sleep(3)
    if not clicked:
        sb.save_screenshot(f"no_claim_btn_{sid}.png")
        return {"status": "\u274c 续期失败", "message": "等唔到可点嘅 Claim Renewal（倒计时/验证未完成）"}

    # 6. 读结果
    return read_renew_result(sb, sid)


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
