#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Orihost 单服自动续期（Jexactyl 面板），以 Hiden 骨架为基座自研
# 流程（bundle.json 12:59 录制 + trace 抓包实测）：
#   进 /server/<短ID> → 关中央广告(要關閉) → 点 Renew → 点 Read Article（新标签读文章 dwell 秒）
#   → 回面板等倒计时 → 过 Turnstile → 点 Claim Renewal → complete 完成（+7 天）
# 右侧悬浮广告不影响，直接忽略；中央广告是外部广告商(ldrws)动态下发，只按关闭按钮匹配

import os
import re
import sys
import time
import random
import html
import requests
from playwright.sync_api import sync_playwright

# --- 环境变量 ---
ORIHOST_REMEMBER = (
    os.environ.get('ORIHOST_REMEMBER')
    or os.environ.get('ORIHOST_COOKIE')      # 兼容旧变量名
    or os.environ.get('ORI_COOKIE')
    or os.environ.get('COOKIE_VALUE')
    or ""
).strip()
ORIHOST_SERVER_IDS = os.environ.get('ORIHOST_SERVER_IDS') or ""  # 单服：短ID 或 完整 UUID
EMAIL = os.environ.get('EMAIL') or os.environ.get('ORIHOST_EMAIL') or ""  # 仅用于 TG 通知脱敏展示
TG_BOT_TOKEN = os.environ.get('TG_BOT_TOKEN') or ""
TG_CHAT_ID = os.environ.get('TG_CHAT_ID') or ""
# 兼容 TG_BOT="chat_id,bot_token" 写法
_TG_BOT = os.environ.get('TG_BOT') or ""
if not (TG_BOT_TOKEN and TG_CHAT_ID) and ',' in _TG_BOT:
    _a, _b = _TG_BOT.split(',', 1)
    TG_CHAT_ID, TG_BOT_TOKEN = _a.strip(), _b.strip()

BASE_URL = "https://panel.orihost.com"
SERVER_SHORT_ID = "8651e616"  # 地址栏 /server/ 后面那段
SERVER_UUID = "8651e616-52e2-46bb-8cbf-74159abb9815"  # 抓包实测：API 要用全量 UUID

# ── 2026-10-08 補丁（我哋 fork）：上游只把 ORIHOST_SERVER_IDS 讀入變數，但**從來冇用過**
# —— 上面兩行係硬編碼佢自己台（8651e616），README 寫「可選可填」實際唔生效。
# 我哋台係 36c736c8-…，唔覆蓋就會去錯台，症狀係「✅ Cookie 登录成功」之後
# 「⚠️ 未找到剩余天数文本 / 📝 cooldown 接口返回 {'http': 404} / ❌ 找不到 Renew 按钮」
# （實證 run 37821351526）。
# 支援：完整 UUID（36c736c8-xxxx-…）或短 ID（36c736c8）；多個以逗號分隔時取第一個。
if ORIHOST_SERVER_IDS:
    _ids = [x.strip() for x in ORIHOST_SERVER_IDS.replace(";", ",").split(",") if x.strip()]
    if _ids:
        _first = _ids[0]
        if "-" in _first:
            SERVER_UUID = _first
        SERVER_SHORT_ID = _first.split("-")[0][:8]
SERVER_URL = f"{BASE_URL}/server/{SERVER_SHORT_ID}"
API_COOLDOWN = f"{BASE_URL}/api/client/servers/{SERVER_UUID}/renew/cooldown"
API_BEGIN = f"{BASE_URL}/api/client/servers/{SERVER_UUID}/renew/begin"

# 文章页停留秒数（begin 返回 dwell_seconds=15，默认 15，可用环境变量覆盖）
ARTICLE_WAIT = int(os.environ.get('ARTICLE_WAIT') or "15")
# Claim 阶段总超时秒数（轮询总时长）
CLAIM_TIMEOUT = int(os.environ.get('CLAIM_TIMEOUT') or "60")

# --- 代理配置（由工作流 sing-box 步骤写入 $GITHUB_ENV，本地可用 ORIHOST_PROXY）---
_MANUAL_PROXY = os.environ.get('ORIHOST_PROXY') or os.environ.get('ORIHOST_GOST_PROXY') or ""
IS_PROXY = (os.environ.get('IS_PROXY', 'false').lower() == 'true') or bool(_MANUAL_PROXY)
PROXY_SERVER = os.environ.get('PROXY_SERVER') or _MANUAL_PROXY or "socks5://127.0.0.1:1080"
REQUESTS_PROXIES = {"http": PROXY_SERVER, "https": PROXY_SERVER} if IS_PROXY else None

# --- 模式（2026-10-09 加）---------------------------------------------------
# watchdog：只讀監看。到續期窗口 → TG 提醒人手撳，**唔會自動撳**。
#   背景：2026-10-09 用只讀探針（renew-kit tools/shield_probe.py）實證，
#   站方 Turnstile 判定嘅係自動化瀏覽器環境，唔係出口 IP —— 機房（本機 SG、
#   節點 UAE/HK）同**住宅家寬（HK HKT 42.200.173.5）**一樣判敗；同一部瀏覽器
#   同一頁換官方 dummy sitekey 1x00000000000000000000AA 就 5.1 秒攞到 token。
#   即係換節點解決唔到 → 按用戶拍板降級做 watchdog + 人手撳。
# renew：維持原本全自動嘗試（留返做 dispatch 用）。
MODE = (os.environ.get('ORIHOST_MODE') or 'watchdog').strip().lower()
# watchdog 模式剩幾日先出「預備提醒」（> 呢個數就靜默）。
# 默認 3：同 FridayDev watchdog 嘅 urgent 門檻（≤3 日）一致。
WATCHDOG_ALERT_DAYS = int(os.environ.get('ORIHOST_WATCH_DAYS') or os.environ.get('ORIHOST_ALERT_DAYS') or "3")

# remember_web cookie 名（bundle.json 12:59 录制快照实测）
REMEMBER_COOKIE_NAME = "remember_web_59ba36addc2b2f9401580f014c7f58ea4e30989d"


# --- 日志 ---
def log(message):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}", flush=True)


STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
window.chrome = { runtime: {} };
"""


def extract_remember_token(raw):
    """从裸 token 或整段 Cookie 字符串中提取 remember_web 值"""
    raw = (raw or "").strip().strip('"').strip("'")
    if not raw:
        return ""
    # 整段 Cookie：找 remember_web_xxx=yyy
    m = re.search(r'remember_web_[0-9a-f]+=([^;\s]+)', raw)
    if m:
        return m.group(1).strip()
    # Cookie 头里直接给了值（eyJ 开头几百字符）
    if raw.startswith('eyJ') and len(raw) > 100 and ' ' not in raw and '\n' not in raw:
        return raw
    # 短 ID 场景兜底：去掉前缀后返回
    if '=' in raw and 'remember_web' in raw:
        return raw.split('=', 1)[1].split(';')[0].strip()
    return raw


def mask_server(text):
    """完整 UUID 脱敏，日志只留短 ID"""
    if not text:
        return text
    return text.replace(SERVER_UUID, f"{SERVER_SHORT_ID}(已脱敏)")


def get_current_ip(proxy_server=None):
    """获取当前出口IP"""
    proxies = {"http": proxy_server, "https": proxy_server} if (proxy_server and IS_PROXY) else None
    try:
        resp = requests.get("https://api.ip.sb/ip", proxies=proxies, timeout=15)
        if resp.status_code == 200:
            return resp.text.strip()
        return "获取失败"
    except Exception as e:
        log(f"❌ 获取出口IP失败: {e}")
        return "获取失败"


def send_telegram_notification(status, old_due, new_due, current_ip="未知"):
    """发送 Telegram 通知（对齐 eooce/Auto-Renew-HidenCloud 风格）"""
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        log("⚠️ Telegram 未配置，跳过通知")
        return False

    local_time = time.gmtime(time.time() + 8 * 3600)
    now = time.strftime("%Y-%m-%d %H:%M:%S", local_time)
    if '@' in EMAIL:
        name, domain = EMAIL.split('@', 1)
        if len(name) > 4:
            masked_email = f"{name[:2]}****{name[-2:]}@{domain}"
        else:
            masked_email = f"{name}@{domain}"
    elif EMAIL:
        masked_email = EMAIL[:2] + '****'
    else:
        masked_email = f"服务器 {SERVER_SHORT_ID}"

    text = (
        f"🎉 Orihost 续期通知\n\n"
        f"{html.escape(status)}\n"
        f"👤 账号: {html.escape(masked_email)}\n"
        f"📅 续期前到期：{html.escape(str(old_due))}\n"
        f"📅 续期后到期：{html.escape(str(new_due))}\n"
        f"🌐 续期使用IP: {html.escape(str(current_ip))}\n"
        f"🕒 续期时间：{html.escape(now)}"
    )
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML"}
    try:
        resp = requests.post(url, json=payload, timeout=10, proxies=REQUESTS_PROXIES)
        if resp.status_code == 200:
            log("✅ Telegram 通知发送成功")
            return True
        log(f"❌ Telegram 通知失败: {resp.text[:200]}")
        return False
    except Exception as e:
        log(f"❌ Telegram 通知异常: {e}")
        return False


def human_mouse_click(page, locator, timeout=8000):
    """真鼠标轨迹点击：先随意晃两下（造移动历史），再分两段逼近目标，
    带抖动和停顿，最后落点。Turnstile 看鼠标移动生物特征，合成 click 无轨迹易被判机器人。"""
    try:
        box = locator.bounding_box(timeout=timeout)
    except Exception:
        return False
    if not box:
        return False
    tx = box["x"] + box["width"] / 2 + random.uniform(-3, 3)
    ty = box["y"] + box["height"] / 2 + random.uniform(-2, 2)
    try:
        # 1) 先在附近晃一下，制造移动历史
        page.mouse.move(tx + random.uniform(-200, 200), ty + random.uniform(-120, 120), steps=10)
        time.sleep(random.uniform(0.15, 0.35))
        # 2) 分两段逼近目标
        page.mouse.move(tx + random.uniform(-60, 60), ty + random.uniform(-40, 40), steps=12)
        time.sleep(random.uniform(0.15, 0.4))
        page.mouse.move(tx, ty, steps=18)
        time.sleep(random.uniform(0.2, 0.5))
        page.mouse.down()
        time.sleep(random.uniform(0.05, 0.15))
        page.mouse.up()
        return True
    except Exception as e:
        log(f"⚠️ 真鼠标点击失败: {str(e)[:120]}")
        return False


def expand_turnstile(page):
    """weirdhost 同款：把被盖住/压缩的验证框展开（overflow/尺寸修复），免得点不到"""
    try:
        page.evaluate(
            """() => {
                document.querySelectorAll('.cf-turnstile').forEach(function(c) {
                    c.style.overflow = 'visible'; c.style.width = '300px'; c.style.height = '65px';
                });
                document.querySelectorAll('iframe').forEach(function(f) {
                    if (f.src && f.src.includes('challenges.cloudflare.com')) {
                        f.style.width = '300px'; f.style.height = '65px';
                        f.style.visibility = 'visible'; f.style.opacity = '1';
                    }
                });
            }"""
        )
    except Exception:
        pass


def challenge_frame_elements(page):
    """frame 樹反查 challenge iframe（closed shadow DOM 唯一覆蓋法）。

    ── 2026-10-08 我哋 fork 嘅補丁 ──
    上游用 `page.locator('iframe[src*="challenges.cloudflare.com"]')` 判有冇驗證，
    但新版 Turnstile 將挑戰 iframe 渲染喺 **closed shadow DOM** 入面，
    任何 DOM 查詢（querySelectorAll / locator）都搵唔到 → `count()` 恆 0 →
    `handle_cloudflare` 開頭就 `return True` 當「冇驗證」→ 唔撳 → 冇 token →
    `Claim Renewal` 一直 disabled → 「超时未点到 Claim Renewal」。
    （實證 run 37821801137：📅 当前剩余：6 天 / cooldown {'seconds': 0} /
     點到 Renew 同 Read Article / 但「Turnstile token 未生成」。）

    唯一覆蓋得到嘅方法係行 **`page.frames`（瀏覽器層 frame 樹）**反查 url 含
    challenges.cloudflare.com，再用 `frame_element()` 攞返 iframe 元素。
    （同一招喺 fridaydev 已驗證有效：`[FRAMES] challenge 命中 1`。）

    返 [(element, box, frame), ...]（frame 用嚟開 frame-scoped CDP session）。
    """
    out = []
    try:
        for f in page.frames:
            if "challenges.cloudflare.com" not in (f.url or ""):
                continue
            try:
                fe = f.frame_element()
                if not fe.is_visible():
                    continue
                box = fe.bounding_box()
                if box and box.get("width", 0) > 10 and box.get("height", 0) > 10:
                    out.append((fe, box, f))
            except Exception:
                continue
    except Exception:
        pass
    return out


def ts_true_click(page, frame):
    """用 frame-scoped CDP session 穿透 closed shadow DOM，攞 checkbox **真座標**再派真事件。

    ── 2026-10-08 我哋 fork 嘅補丁（按 digest/procedure/cf-turnstile-shadow-dom-click.md）──
    實證 run 37822542763：frame 樹反查成功（命中 1）、撳到 iframe 三次，但挑戰框
    之後又返嚟、token 始終 0 → 撳中位置唔對。食譜明寫「不要凭可见 DOM 猜坐标」：
    某案例一直撳 (222,312) 而真值係 (216,312)，只差 6px 就連續 24 輪點唔中。
    正解：為該 frame 開獨立 CDP session → DOM.getDocument(pierce=True) 穿透 closed
    shadow DOM → 對目標節點 getBoxModel 攞真座標 → 派 Input.dispatchMouseEvent。
    注意：frame-scoped session 嘅 Input 座標係 frame 本地座標，唔使加 iframe 偏移。

    返 (成功?, 說明)。
    """
    cdp = None
    try:
        cdp = page.context.new_cdp_session(frame)
    except Exception as e:
        return False, f"new_cdp_session 失敗: {str(e)[:60]}"
    try:
        doc = cdp.send("DOM.getDocument", {"pierce": True, "depth": -1})
        root = doc["root"]["nodeId"]
        for sel in ('input[type="checkbox"]', 'label', 'div#cf-turnstile', 'body'):
            try:
                nid = (cdp.send("DOM.querySelector",
                                {"nodeId": root, "selector": sel}) or {}).get("nodeId") or 0
            except Exception:
                nid = 0
            if not nid:
                continue
            try:
                model = cdp.send("DOM.getBoxModel", {"nodeId": nid})["model"]["border"]
            except Exception:
                continue
            x = (model[0] + model[2] + model[4] + model[6]) / 4.0
            y = (model[1] + model[3] + model[5] + model[7]) / 4.0
            for t in ("mouseMoved", "mousePressed", "mouseReleased"):
                cdp.send("Input.dispatchMouseEvent",
                         {"type": t, "x": x, "y": y, "button": "left", "clickCount": 1})
                time.sleep(0.08)
            return True, f"{sel} @ ({x:.0f},{y:.0f})"
        return False, "pierce 後搵唔到 checkbox/label"
    except Exception as e:
        return False, f"cdp: {str(e)[:70]}"
    finally:
        try:
            if cdp is not None:
                cdp.detach()
        except Exception:
            pass


def handle_cloudflare(page, timeout=90):
    """处理 Cloudflare Turnstile 验证（复用 Hiden 骨架写法）
    timeout：本轮最多等待秒数；轮询中请传小值（如 15），避免一轮卡死
    策略：iframe 内 checkbox 点 → 不行就 force 点 iframe 中心；每轮都带 token 检查"""
    iframe_selector = 'iframe[src*="challenges.cloudflare.com"]'
    # 2026-10-08 補：唔可以只信 locator —— closed shadow DOM 下 count 恆 0。
    _fr = challenge_frame_elements(page)
    if not _fr and page.locator(iframe_selector).count() == 0:
        return True
    log(f"⚠️ 检测到 Cloudflare 验证（frame 樹命中 {len(_fr)} 個 / locator "
        f"{page.locator(iframe_selector).count()} 個）...")
    start_time = time.time()
    while time.time() - start_time < timeout:
        _fr = challenge_frame_elements(page)
        if not _fr and page.locator(iframe_selector).count() == 0:
            log("✅ Cloudflare 验证通过！")
            return True
        # token 已有则直接过
        try:
            token = page.evaluate(
                '() => document.querySelector("[name=cf-turnstile-response]")?.value || ""'
            )
            if token and len(token) > 20:
                log("✅ Turnstile token 已生成")
                return True
        except Exception:
            pass
        # 策略1：先展开验证框，再真鼠标轨迹点击 checkbox（weirdhost 同款思路）
        expand_turnstile(page)
        clicked = False
        # 策略0（2026-10-08 補）：用 frame 樹攞到嘅 iframe 元素直接撳 ——
        # closed shadow DOM 下 locator 永遠 count=0，呢個係唯一撳得到嘅方法。
        for _fe, _box, _fr_obj in _fr:
            # 先試 CDP 真座標（穿透 closed shadow DOM，唔靠猜）
            _ok, _why = ts_true_click(page, _fr_obj)
            if _ok:
                log(f"🖱️ CDP 真座標點 checkbox：{_why}")
                clicked = True
                break
            log(f"⚠️ CDP 真座標路失敗（{_why}），退回 frame 元素點擊")
            try:
                _fe.scroll_into_view_if_needed(timeout=3000)
            except Exception:
                pass
            try:
                _fe.click(position={"x": min(30, _box["width"] / 2),
                                    "y": _box["height"] / 2}, timeout=5000)
                log("🖱️ 點 Turnstile iframe（frame 樹反查，closed shadow DOM）")
                clicked = True
                break
            except Exception as e:
                log(f"⚠️ frame 樹點擊失敗，退回 locator 路: {str(e)[:100]}")
        try:
            frame = page.frame_locator(iframe_selector)
            checkbox = frame.locator('input[type="checkbox"]')
            if checkbox.count() > 0 and checkbox.first.is_visible(timeout=3000):
                checkbox.first.scroll_into_view_if_needed(timeout=3000)
                log("🖱️ 真鼠标轨迹点击验证复选框...")
                if human_mouse_click(page, checkbox.first):
                    clicked = True
                else:
                    log("🖱️ 轨迹点击失败，改普通点击...")
                    try:
                        checkbox.first.click(timeout=5000)
                    except Exception:
                        checkbox.first.click(force=True, timeout=5000)
                    clicked = True
        except Exception as e:
            log(f"⚠️ 复选框点击失败，换 force 策略: {str(e)[:120]}")
        # 策略2：直接 force 点 iframe 中心
        if not clicked:
            try:
                fr = page.locator(iframe_selector).first
                if fr.is_visible(timeout=3000):
                    log("🖱️ 直接点击验证框中心（force）...")
                    fr.click(force=True, timeout=5000)
                    clicked = True
            except Exception as e:
                log(f"⚠️ 验证框点击失败: {str(e)[:120]}")
        if clicked:
            time.sleep(5)
            continue
        time.sleep(2)
    # 超时前最后看一次 token
    try:
        token = page.evaluate(
            '() => document.querySelector("[name=cf-turnstile-response]")?.value || ""'
        )
        if token and len(token) > 20:
            log("✅ Turnstile token 已生成（超时前命中）")
            return True
    except Exception:
        pass
    log("❌ 验证超时。")
    return False


def wait_turnstile_token(page, timeout=90):
    """等待 Turnstile token 生成（Claim 前置条件）"""
    log("⏳ 等待 Turnstile token...")
    start = time.time()
    while time.time() - start < timeout:
        try:
            token = page.evaluate(
                '() => document.querySelector("[name=cf-turnstile-response]")?.value || ""'
            )
        except Exception:
            token = ""
        if token and len(token) > 20:
            log("✅ Turnstile token 已生成")
            return True
        time.sleep(1)
    log("⚠️ Turnstile token 未生成（可能免验证或验证失败）")
    return False


def close_center_ad(page, rounds=3):
    """关闭屏幕中央广告弹窗（steps/S000008、S000015 实测：繁体『要關閉』按钮）
    右侧悬浮广告不影响，直接忽略。广告内容每次动态变化，只按关闭按钮匹配。"""
    closed = 0
    for i in range(rounds):
        try:
            btn = page.locator('button:has-text("要關閉")')
            if btn.count() == 0:
                btn = page.locator('button:has-text("關閉")')
            visible = False
            for idx in range(btn.count()):
                try:
                    if btn.nth(idx).is_visible():
                        log(f"🖱️ 关闭中央广告（第 {i+1} 轮）...")
                        btn.nth(idx).click()
                        closed += 1
                        visible = True
                        time.sleep(1.5)
                        break
                except Exception:
                    continue
            if not visible:
                break
        except Exception:
            break
    if closed:
        log(f"✅ 已关闭中央广告 {closed} 次")
    return closed


def dismiss_cookie_banner(page):
    """点掉底部 cookie 横幅（We use cookies → Got it），免得遮挡对话框"""
    try:
        btn = page.locator('button:has-text("Got it")')
        for idx in range(btn.count()):
            try:
                if btn.nth(idx).is_visible():
                    btn.nth(idx).click()
                    time.sleep(1)
                    break
            except Exception:
                continue
    except Exception:
        pass


def login(page):
    """remember_web Cookie 登录（单服 MVP 不做账密兜底）"""
    token = extract_remember_token(ORIHOST_REMEMBER)
    if not token:
        log("❌ 缺少 ORIHOST_REMEMBER（remember_web token 值）")
        return False
    log("📇 尝试 Cookie 登录...")
    try:
        page.context.add_cookies([{
            'name': REMEMBER_COOKIE_NAME,
            'value': token,
            'domain': 'panel.orihost.com',
            'path': '/',
            'expires': int(time.time()) + 3600 * 24 * 365,
            'httpOnly': True,
            'secure': True,
            'sameSite': 'Lax'
        }])
        page.goto(SERVER_URL, wait_until="domcontentloaded", timeout=60000)
        time.sleep(2)
        close_center_ad(page)
        handle_cloudflare(page)
        log(f"📝 当前URL: {mask_server(page.url)} | Title: {page.title()}")
        if "auth/login" in page.url:
            log("❌ Cookie 失效（被踢回登录页），请重新获取 remember_web token")
            page.screenshot(path="login_fail.png")
            return False
        if SERVER_SHORT_ID not in page.url:
            log(f"⚠️ 未进入服务器页：{mask_server(page.url)}")
            page.screenshot(path="login_fail.png")
            return False
        log("✅ Cookie 登录成功，已到达服务器页")
        return True
    except Exception as e:
        log(f"❌ 登录异常: {e}")
        try:
            page.screenshot(path="login_fail.png")
        except Exception:
            pass
        return False


def api_cooldown(page):
    """读续期冷却（bundle.json：GET .../renew/cooldown → {"seconds":0}），页面上下文内请求自动带 Cookie+XSRF"""
    try:
        data = page.evaluate(
            """async (url) => {
                const r = await fetch(url, {credentials: 'same-origin', headers: {'Accept': 'application/json'}});
                if (!r.ok) return {http: r.status};
                return await r.json();
            }""",
            API_COOLDOWN,
        )
        log(f"📝 cooldown 接口返回: {data}")
        if isinstance(data, dict) and "seconds" in data:
            return int(data["seconds"])
        return None
    except Exception as e:
        log(f"⚠️ cooldown 接口读取失败（走 UI 流程）: {e}")
        return None


def get_renewal_days(page):
    """读取面板『Current renewal in: N days』/『RENEWAL IN N Days』，返回天数（int）或 None"""
    try:
        body_text = page.locator("body").inner_text(timeout=10000)
    except Exception as e:
        log(f"❌ 读取页面文本失败: {e}")
        return None
    patterns = [
        r"Current renewal in:\s*(\d+)\s*days?",
        r"RENEWAL IN\s*(\d+)\s*Days?",
    ]
    for pattern in patterns:
        m = re.search(pattern, body_text, re.IGNORECASE)
        if m:
            days = int(m.group(1))
            log(f"📅 当前剩余：{days} 天")
            return days
    log("⚠️ 未找到剩余天数文本")
    return None


def watchdog_check(page):
    """只讀：判有冇到續期窗口。**唔撳任何掣、唔過 Turnstile、唔提交**。

    回傳 (window_open, note)：
      True  = 窗口開咗（Renew 掣可撳、無冷卻、無 Renew Limit Reached）
      False = 未到窗口
      None  = 讀唔到（面板載入失敗／CF 擋），保守當「未開」但會照 TG 講一聲
    """
    secs = api_cooldown(page)
    try:
        if SERVER_SHORT_ID not in page.url:
            page.goto(SERVER_URL, wait_until="domcontentloaded", timeout=60000)
        time.sleep(2)
        close_center_ad(page)
        body = page.locator("body").inner_text(timeout=10000)
    except Exception as e:
        return None, f"讀面板失敗: {type(e).__name__}: {e}"

    if "Renew Limit Reached" in body or "renewal limit" in body.lower():
        return False, "Renew Limit Reached（未到窗口）"
    try:
        btn = page.locator('button:has-text("Renew")').first
        if btn.count() == 0:
            return False, "搵唔到 Renew 掣"
        if btn.is_disabled():
            return False, "Renew 掣置灰（disabled）"
    except Exception as e:
        return None, f"讀 Renew 掣失敗: {type(e).__name__}: {e}"
    if secs is not None and secs > 0:
        return False, f"冷卻中（{secs}s）"
    return True, "Renew 掣可撳 + 無冷卻 → 窗口開咗"


def renew_service(page):
    """Orihost 续期芯：Renew → Read Article（新标签 dwell）→ Turnstile → Claim Renewal
    返回 True / False / "NOT_TIME"（冷却中或未到续期条件）"""

    # 0. 先查冷却：seconds>0 直接跳过
    seconds = api_cooldown(page)
    if seconds is not None and seconds > 0:
        log(f"⏳ 冷却中，剩余 {seconds}s，本轮跳过")
        return "NOT_TIME"

    try:
        log("➡ 进入续期流程...")
        if SERVER_SHORT_ID not in page.url:
            page.goto(SERVER_URL, wait_until="domcontentloaded", timeout=60000)
        time.sleep(2)
        close_center_ad(page)
        handle_cloudflare(page)

        # 1. 点 Renew（bundle 选择器按文本兜底，避免 styled-components 类名漂移）
        # 未到续期时间时按钮为 disabled 置灰（文案 "Renew Limit Reached"），直接跳过不报错
        log("🖱️ 点击 'Renew'...")
        renew_btn = page.locator('button:has-text("Renew")').first
        try:
            renew_btn.wait_for(state="visible", timeout=15000)
        except Exception:
            log("❌ 找不到 Renew 按钮")
            page.screenshot(path="renew_no_button.png")
            return False
        try:
            body_pre = page.locator("body").inner_text(timeout=5000)
        except Exception:
            body_pre = ""
        if "Renew Limit Reached" in body_pre or "renewal limit" in body_pre.lower():
            log("⏳ 未到续期时间（Renew Limit Reached），本轮跳过")
            return "NOT_TIME"
        try:
            if renew_btn.is_disabled() or not renew_btn.is_enabled():
                log("⏳ Renew 按钮置灰（disabled），未到续期时间，本轮跳过")
                return "NOT_TIME"
        except Exception:
            pass
        renew_btn.scroll_into_view_if_needed()
        try:
            renew_btn.click(timeout=10000)
        except Exception as e:
            msg = str(e)
            if "not enabled" in msg.lower() or "disabled" in msg.lower():
                log("⏳ Renew 按钮不可点（置灰），未到续期时间，本轮跳过")
                return "NOT_TIME"
            # 点击超时后复查一次：可能是刚变成置灰
            try:
                body_retry = page.locator("body").inner_text(timeout=5000)
            except Exception:
                body_retry = ""
            if "Renew Limit Reached" in body_retry or "renewal limit" in body_retry.lower():
                log("⏳ 未到续期时间（Renew Limit Reached），本轮跳过")
                return "NOT_TIME"
            try:
                if renew_btn.is_disabled():
                    log("⏳ Renew 按钮置灰（disabled），未到续期时间，本轮跳过")
                    return "NOT_TIME"
            except Exception:
                pass
            log(f"❌ 点击 Renew 失败: {msg[:200]}")
            page.screenshot(path="renew_click_fail.png")
            return False
        time.sleep(2)
        close_center_ad(page)

        # 2. 等续期对话框出现
        dlg_text = page.locator('div.fixed.inset-0')
        try:
            dlg_text.first.wait_for(state="visible", timeout=15000)
        except Exception:
            log("❌ 续期对话框未弹出（可能未到续期条件）")
            page.screenshot(path="renew_no_dialog.png")
            return "NOT_TIME"
        body = page.locator("body").inner_text()
        if "Current renewal in" in body:
            m = re.search(r"Current renewal in:\s*(\d+)", body)
            if m:
                log(f"📅 对话框显示剩余 {m.group(1)} 天")

        # 3. 点 Read Article（新标签打开文章，begin 接口此时触发）
        log("🖱️ 点击 'Read Article'...")
        read_btn = page.locator('button:has-text("Read Article")').first
        try:
            read_btn.wait_for(state="visible", timeout=15000)
        except Exception:
            # 可能已在倒计时/可 Claim 状态
            if "Claim Renewal" in body or "second(s)" in body:
                log("➡ 已在倒计时/待 Claim 状态，跳过 Read Article")
            else:
                log("❌ 找不到 Read Article 按钮")
                page.screenshot(path="renew_no_read.png")
                return False
        else:
            read_btn.scroll_into_view_if_needed()
            try:
                with page.expect_popup(timeout=15000) as pop:
                    read_btn.click()
                article = pop.value
                # popup 先是 about:blank，等它导航到真实文章页再 dwell
                for _ in range(20):
                    try:
                        cur = article.url
                    except Exception:
                        cur = ""
                    if cur and cur != "about:blank":
                        break
                    time.sleep(1)
                try:
                    article.wait_for_load_state("domcontentloaded", timeout=30000)
                except Exception:
                    pass
                try:
                    log(f"📖 文章页已打开: {(article.url or '')[:80]}...")
                except Exception:
                    log("📖 文章页已打开")
                if (article.url or "") in ("", "about:blank"):
                    log("⚠️ 文章页仍是空白页（begin 已在点击时触发，继续倒计时）")
                dwell = max(ARTICLE_WAIT, 15)
                log(f"⏳ 模拟阅读 {dwell}s...")
                for _ in range(dwell):
                    time.sleep(1)
                    try:
                        article.evaluate("() => window.scrollBy(0, 200)")
                    except Exception:
                        pass
                    # 文章页也可能弹广告/验证，只做最小处理
                    try:
                        if article.locator('iframe[src*="challenges.cloudflare.com"]').count() > 0:
                            pass
                    except Exception:
                        pass
                try:
                    article.close()
                except Exception:
                    pass
                log("📖 文章页已关闭，回到面板")
            except Exception as e:
                log(f"⚠️ 新标签未捕获（{e}），改用等待倒计时继续")

        # 4. 回面板：等倒计时走完（"Thanks for reading"），轮询 Claim 可点
        try:
            page.bring_to_front()
        except Exception:
            pass
        if SERVER_SHORT_ID not in page.url:
            page.goto(SERVER_URL, wait_until="domcontentloaded", timeout=60000)
        time.sleep(2)
        close_center_ad(page)
        dismiss_cookie_banner(page)
        handle_cloudflare(page)

        log("⏳ 等待倒计时结束（Thanks for reading）...")
        claimed = False
        start_wait = time.time()
        poll_timeout = CLAIM_TIMEOUT
        while time.time() - start_wait < poll_timeout:
            try:
                body = page.locator("body").inner_text(timeout=5000)
            except Exception:
                time.sleep(2)
                continue
            # 未到条件/上限的几种文案直接判跳过
            if "Renew Limit Reached" in body or "renewal limit" in body.lower():
                log("⚠️ 面板显示续期次数已达上限，本轮跳过")
                page.screenshot(path="renew_limit.png")
                return "NOT_TIME"
            if "Thanks for reading" in body or "Claim Renewal" in body:
                # 广告可能挡住验证框，先清；cookie 横幅也顺手点掉
                close_center_ad(page, rounds=1)
                dismiss_cookie_banner(page)
                # 盾必须主动点击才会出 token，每轮都试一次（小超时，不卡死轮询）
                handle_cloudflare(page, timeout=15)
                # 免验证场景可能直接可点；否则等一小会儿 token
                wait_turnstile_token(page, timeout=10)
                claim_btn = page.locator('button:has-text("Claim Renewal")').first
                try:
                    if claim_btn.count() and claim_btn.is_visible() and claim_btn.is_enabled():
                        log("🖱️ 点击 'Claim Renewal'...")  # 前端随即调 GET /api/client/renewal/complete?cf-turnstile-response=
                        claim_btn.click()
                        claimed = True
                        break
                    else:
                        log("⏳ Claim 按钮仍不可点（Turnstile 未通过），继续等待...")
                except Exception:
                    pass
            else:
                # 倒计时还没走完
                m = re.search(r"claim your renewal in\s*(\d+)\s*second", body, re.IGNORECASE)
                if m:
                    log(f"⏳ 倒计时中…{m.group(1)}s")
            time.sleep(5)

        if not claimed:
            log("❌ 超时未点到 Claim Renewal")
            page.screenshot(path="renew_claim_timeout.png")
            return False

        # 5. 等 complete 生效：轮询剩余天数变大或成功文案
        log("⏳ 等待续期生效...")
        time.sleep(5)
        close_center_ad(page)
        handle_cloudflare(page)
        try:
            page.goto(SERVER_URL, wait_until="domcontentloaded", timeout=60000)
            time.sleep(3)
        except Exception:
            pass
        log("✅ Claim 已点击，续期请求已提交")
        return True

    except Exception as e:
        log(f"❌ 续费异常: {e}")
        try:
            page.screenshot(path="renew_error.png")
        except Exception:
            pass
        return False


def main():
    if not ORIHOST_REMEMBER:
        log("❌ 缺少登录凭证：请设置 ORIHOST_REMEMBER（remember_web token 值）")
        sys.exit(1)

    with sync_playwright() as p:
        try:
            if IS_PROXY:
                log(f"⚙️ 代理已启用: {PROXY_SERVER}")
            else:
                log("🌐 直连模式（未使用代理）")

            current_ip = get_current_ip(PROXY_SERVER)
            log(f"🎯 当前出口IP: {current_ip}")

            log("🚀 启动浏览器...")
            browser = p.chromium.launch(
                channel="chrome",
                headless=False,
                args=['--no-sandbox', '--disable-blink-features=AutomationControlled', '--disable-infobars']
            )
            context = browser.new_context(
                viewport={'width': 1920, 'height': 1080},
                user_agent='Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36',
                proxy={"server": PROXY_SERVER} if IS_PROXY else None
            )
            page = context.new_page()
            page.add_init_script(STEALTH_JS)

            if not login(page):
                send_telegram_notification("❌ 登录失败（Cookie 失效）", "未知", "未知", current_ip)
                sys.exit(1)

            # 续期前剩余天数
            old_days = get_renewal_days(page)
            old_due = f"剩余 {old_days} 天" if old_days is not None else "未知"

            # ── watchdog 模式：只讀監看，唔自動撳（2026-10-09 用戶拍板）────────
            if MODE != "renew":
                window_open, note = watchdog_check(page)
                log(f"🔒【watchdog 讀數】{note}")
                if window_open is None:
                    log("⚠️ 讀唔到面板狀態 → TG 講一聲，唔標紅")
                    send_telegram_notification(f"⚠️ watchdog 讀唔到面板狀態：{note}",
                                               old_due, old_due, current_ip)
                    sys.exit(0)
                if window_open:
                    log("🔔【可續期窗口已開】watchdog 模式唔會自動撳，已 TG 提醒人手")
                    send_telegram_notification(
                        "🔔 已進入續期窗口，請人手撳（watchdog 模式唔會自動撳）",
                        old_due, old_due, current_ip)
                    # 需要人手 = 要人注意 → 標紅（同 FridayDev watchdog 語義一致：
                    # 映射成非 FAILED 就等於永久綠燈，watchdog 白裝）
                    sys.exit(1)
                if old_days is not None and old_days <= WATCHDOG_ALERT_DAYS:
                    log(f"⏳ 剩 {old_days} 日 ≤ {WATCHDOG_ALERT_DAYS} 日 → 預備提醒")
                    send_telegram_notification(
                        f"⏳ 未可續（仲有 {old_days} 日）· 預備提醒",
                        old_due, old_due, current_ip)
                    sys.exit(0)
                log(f"😴 未到窗口（剩 {old_days} 日）→ 靜默，唔發 TG")
                sys.exit(0)

            # 执行续期
            renew_result = renew_service(page)

            new_due = old_due
            if renew_result == "NOT_TIME":
                log("⏳ 未到续期条件，本轮跳过")
                status = "⏳ 未到续期条件，本轮跳过"
            elif renew_result is False:
                log("❌ 续期失败")
                status = "❌ 续期失败（详见 Actions 日志截图）"
            else:
                new_days = get_renewal_days(page)
                new_due = f"剩余 {new_days} 天" if new_days is not None else "已提交待确认"
                log(f"📆 续期后：{new_due}")
                status = "✅ 续期成功"

            send_telegram_notification(status, old_due, new_due, current_ip)

            if renew_result is False:
                sys.exit(1)
            sys.exit(0)
        except Exception as e:
            log(f"❌ 浏览器启动出错: {e}")
            sys.exit(1)
        finally:
            if 'browser' in locals() and browser:
                try:
                    browser.close()
                except Exception:
                    pass


if __name__ == "__main__":
    main()
