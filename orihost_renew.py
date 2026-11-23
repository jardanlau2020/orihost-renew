#!/usr/bin/env python3
"""
Orihost 免费服务器自动续期（SeleniumBase UC 模式 + uc_gui_click_captcha，同 hohai-checkin 的过盾方式）

流程: 登录(优先 cookie, 失效用账号密码) -> 每台服务器 Console 页 -> Renew -> Read Article(新标签停留)
      -> 过 Turnstile -> Claim Renewal -> 对比剩余天数 -> 导出最新 cookie

环境变量:
  ORIHOST_COOKIE    可选, 整段 Cookie 请求头, 优先使用
  ORIHOST_EMAIL     账号(邮箱/用户名)
  ORIHOST_PASSWORD  密码
  NODE_LINK         可选, 代理节点(vless/vmess/trojan/ss/socks5/http), node_proxy.py 转本地代理, 失败回落直连
  READ_SECONDS      看文章停留秒数, 默认 15
  COOKIE_OUT        导出 cookie 文件, 默认 cookie.txt

本地: pip install -r requirements.txt && python orihost_renew.py
Linux 无桌面: SeleniumBase 会自动用 xvfb (需 apt install xvfb)
"""
import json
import os
import re
import sys
from urllib.parse import quote
import threading
import time
from datetime import datetime

from node_proxy import setup_node_proxy, probe_exit_ip

BASE = "https://panel.orihost.com"
EMAIL = os.getenv("ORIHOST_EMAIL", "")
PASSWORD = os.getenv("ORIHOST_PASSWORD", "")
COOKIE = os.getenv("ORIHOST_COOKIE", "").strip()

# ── 本仓补丁：三级凭证解析（上游只有 EMAIL/PASSWORD 与 COOKIE 两级）──────────
# 优先级 1: ORIHOST_EMAIL + ORIHOST_PASSWORD（上游主线）
# 优先级 2: ORIHOST_COOKIE（整段 cookie 串）
# 优先级 3: ORIHOST_REMEMBER（Jexactyl 的 remember_web token，裸值或整段 cookie）
# 三个都没有时打一行明确提示，不静默失败。
REMEMBER_RAW = os.getenv("ORIHOST_REMEMBER", "").strip()
REMEMBER_COOKIE_NAME = "remember_web_59ba36addc2b2f9401580f014c7f58ea4e30989d"


def _remember_to_cookie(raw: str) -> str:
    """把 ORIHOST_REMEMBER 归一成整段 Cookie。

    兼容两种写法：
      - 裸 token（一长串字母数字）→ 拼成 name=value
      - 整段 Cookie（含 remember_web_xxx=）→ 原样用
    """
    raw = (raw or "").strip()
    if not raw:
        return ""
    if "remember_web" in raw:
        return raw
    return f"{REMEMBER_COOKIE_NAME}={raw}"


if not COOKIE and REMEMBER_RAW:
    COOKIE = _remember_to_cookie(REMEMBER_RAW)

if EMAIL and PASSWORD:
    _cred_path = "账密（ORIHOST_EMAIL + ORIHOST_PASSWORD）"
elif COOKIE:
    _cred_path = ("cookie（ORIHOST_COOKIE）" if os.getenv("ORIHOST_COOKIE", "").strip()
                  else "remember token（ORIHOST_REMEMBER）")
else:
    _cred_path = ""

if not _cred_path:
    print("❌ 缺凭证：需要 ORIHOST_EMAIL + ORIHOST_PASSWORD，"
          "或 ORIHOST_COOKIE，或 ORIHOST_REMEMBER（remember_web token）其中之一", flush=True)

NODE_LINK = os.getenv("NODE_LINK", "")
READ_SECONDS = int(os.getenv("READ_SECONDS", "25"))
COOKIE_OUT = os.getenv("COOKIE_OUT", "cookie.txt")

TS_IFRAME = 'iframe[src*="challenges.cloudflare.com"]'
MAX_DAYS = int(os.environ.get("MAX_DAYS", "21"))
# 2026-11-21（#90 dump 实锤）：面板真实按钮文案是 `Renew server`，上游用
# normalize-space()='Renew' 精确匹配，多一个词就永远匹配不到 -> 九轮下来弹窗
# 一次都没打开，Turnstile 自然也没出现过（iframes=0 / modal 空）。
# 改成 contains，并排除上限态按钮，避免把 "Renew Limit Reached" 当成可续期。
X_RENEW = "//button[contains(normalize-space(),'Renew') and not(contains(normalize-space(),'Limit'))]"
X_READ = ("//button[contains(translate(normalize-space()," 
          "    'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz')," 
          "    'read article')]")
X_CLAIM = ("//button[contains(translate(normalize-space()," 
           "    'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz')," 
           "    'claim')]")
X_SIGNIN = "//button[@type='submit' or contains(normalize-space(),'Sign In')]"


def log(*a):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}]", *a, flush=True)


def credential_path() -> str:
    """当前用的是哪条凭证路径（只报类型，不报值）。"""
    if EMAIL and PASSWORD:
        return "账密"
    if os.getenv("ORIHOST_COOKIE", "").strip():
        return "ORIHOST_COOKIE"
    if REMEMBER_RAW:
        return "ORIHOST_REMEMBER"
    return "无"


# ---------------------------------------------------------------- 网络

def setup_network():
    """返回 (本地代理URL或None, 脱敏出口IP, 模式)。

    2026-11-21 改：不再「没家宽就偷偷用 NODE_LINK 降级」。机房 IP 去续期是
    风险最高的跑法（Cloudflare 一眼看穿，Turnstile 必挂），所以要显式：
      - NODE_PROXY_URL 有值（workflow 起好 mihomo）→ 先验端口，不通就抛错
      - 没 NODE_PROXY_URL 但配了 HOME_SUB_URL → workflow 该起却没起来，抛错
      - 两者都没有 → 明确以「无代理」跑，日志写清楚
      - 想用 NODE_LINK 走 sing-box，必须显式设 ORIHOST_USE_NODE_LINK=1
    """
    ext = os.getenv("NODE_PROXY_URL", "").strip()
    home = (os.getenv("HOME_SUB_URL", "").strip()
            or os.getenv("HOME_SUB_GZ", "").strip())
    if ext:
        host_port = ext.split("://", 1)[-1]
        try:
            import socket
            host, port = host_port.rsplit(":", 1)
            with socket.create_connection((host, int(port)), timeout=3):
                pass
        except Exception as e:
            raise RuntimeError(
                f"代理端口不通（{host_port}）：{e} → 不降级直连，先修代理")
        log(f"使用家宽出口：{ext}")
        return ext, os.getenv("NODE_PROXY_IP", "") or "未知", "代理"

    if home:
        raise RuntimeError(
            "配了 HOME_SUB_URL / HOME_SUB_GZ 但 workflow 没起 mihomo（NODE_PROXY_URL 为空）"
            " → 不降级直连，先修 workflow")

    if os.getenv("ORIHOST_USE_NODE_LINK", "").strip().lower() in ("1", "true", "yes") and NODE_LINK.strip():
        try:
            proxy, ip, _ = setup_node_proxy(NODE_LINK, workdir=os.getenv("RUNNER_TEMP", "/tmp"))
            return proxy, ip or "未知", "代理(NODE_LINK)"
        except Exception as e:
            raise RuntimeError(f"NODE_LINK 代理起不来：{e} → 不降级直连") from e

    log("⚠️ 未配置 HOME_SUB_URL / HOME_SUB_GZ：以无代理模式运行"
        "（出口=机房 IP，Turnstile 可能必挂）")
    return None, probe_exit_ip() or "未知", "无代理"


# ---------------------------------------------------------------- Turnstile

def ts_token(sb):
    try:
        return sb.execute_script(
            "var e=document.querySelector('input[name=\"cf-turnstile-response\"]');"
            "return e?e.value:'';") or ""
    except Exception:
        return ""


def btn_enabled(sb, xpath):
    try:
        el = sb.find_element(xpath, by="xpath", timeout=1)
        return el.is_enabled() and el.get_attribute("disabled") is None
    except Exception:
        return False


RESET_TS_JS = """
try { if (window.turnstile) { document.querySelectorAll('.cf-turnstile, [data-sitekey]').forEach(function(w){ try{turnstile.reset(w);}catch(e){} }); try{turnstile.reset();}catch(e){} return 1; } } catch(e) {}
return 0;
"""


def rerender_detached(sb, secs=8):
    """弹窗里的 Turnstile 是在 webdriver 连接状态下渲染的, CF 会识别。
    重置 widget 后立刻断开 webdriver, 让它在"干净"状态下重新渲染。"""
    try:
        n = sb.execute_script(RESET_TS_JS)
    except Exception:
        n = 0
    try:
        sb.reconnect(secs)
    except Exception:
        sb.sleep(secs)
    return n


MARK_TS_JS = """
var inp=document.querySelector('input[name="cf-turnstile-response"]');
var el=inp?inp.parentElement:null;
while(el && el!==document.body && (el.offsetWidth<250 || el.offsetHeight<50)) el=el.parentElement;
if(!el || el===document.body){
  var f=document.querySelector('iframe[src*="challenges.cloudflare.com"]');
  el=f||null;
}
if(!el) return null;
el.scrollIntoView({block:'center'});
el.setAttribute('data-hark-ts','1');
var r=el.getBoundingClientRect();
return [r.left, r.top, r.width, r.height];
"""


def click_turnstile_box(sb):
    """自己算复选框的屏幕坐标再用系统鼠标点（uc_gui_click_captcha 在 SPA 弹窗里会选错元素）。"""
    rect = sb.execute_script(MARK_TS_JS)
    if not rect:
        log("没找到 Turnstile 容器，退回 uc_gui_click_captcha")
        sb.uc_gui_click_captcha()
        return
    sb.sleep(1)
    rect = sb.execute_script(
        "var r=document.querySelector('[data-hark-ts]').getBoundingClientRect();"
        "return [r.left,r.top,r.width,r.height];")
    w = sb.driver.get_window_rect()
    inner_h = sb.execute_script("return window.innerHeight;")
    x = w["x"] + rect[0] + 30
    y = w["y"] + w["height"] - inner_h + rect[1] + rect[3] / 2.0
    log(f"Turnstile 容器 {[int(v) for v in rect]}，点击屏幕坐标 ({int(x)}, {int(y)})")
    sb.uc_gui_click_x_y(x, y)


def pass_turnstile(sb, wait_btn_xpath, timeout=90, detach=False, require_token=False):
    """系统级鼠标点 Turnstile，直到目标按钮可点或拿到 token。
    detach=True: 用于页面内动态弹出的 Turnstile, 先断开 webdriver 重新渲染再点。
    require_token=True: 只认 ts_token 非空。X_RENEW 改成 contains 之后它一直是
    可点状态，用它当判据会立刻返回 True（点击 0 次）造成假阳性，接口续期那条路
    必须只看 token。"""
    end = time.time() + 20
    while time.time() < end and not sb.is_element_present(TS_IFRAME):
        sb.sleep(1)
    sb.sleep(3)
    start, clicks = time.time(), 0
    while time.time() - start < timeout:
        if ts_token(sb) or (not require_token and btn_enabled(sb, wait_btn_xpath)):
            log(f"Turnstile 已通过（点击 {clicks} 次）")
            return True
        if clicks < 4 and (clicks == 0 or time.time() - start > clicks * 20):
            if detach and clicks >= 2:
                rerender_detached(sb)
            try:
                if detach:
                    click_turnstile_box(sb)
                else:
                    sb.uc_gui_click_captcha()
            except Exception as e:
                log("uc_gui_click_captcha 异常：", str(e).splitlines()[0][:200])
            clicks += 1
            if detach and clicks == 1:
                sb.sleep(4)
                try:
                    sb.save_screenshot('fail_ts_after_click.png')
                except Exception:
                    pass
        sb.sleep(2)
    return False


# ---------------------------------------------------------------- 登录 / cookie

def logged_in(sb):
    return "/auth" not in sb.get_current_url()


def inject_cookie(sb):
    if not COOKIE:
        return
    n = 0
    for part in COOKIE.split(";"):
        if "=" in part:
            k, v = part.strip().split("=", 1)
            if k.upper() == "XSRF-TOKEN":  # 旧 XSRF 会和面板新下发的重复 -> CSRF token mismatch, 让面板自己重新下发
                continue
            try:
                sb.driver.add_cookie({"name": k, "value": v, "domain": "panel.orihost.com",
                                      "path": "/", "secure": True})
                n += 1
            except Exception as e:
                log(f"cookie {k} 注入失败：{e}")
    log(f"已注入 {n} 个 cookie")


def save_cookies(sb):
    jar = [c for c in sb.driver.get_cookies() if "orihost.com" in c.get("domain", "")]
    if not jar:
        return
    header = "; ".join(f"{c['name']}={c['value']}" for c in jar)
    if os.getenv("GITHUB_ACTIONS"):
        for c in jar:
            if len(c["value"]) > 6:
                print(f"::add-mask::{c['value']}", flush=True)
    fd = os.open(COOKIE_OUT, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(header)
    log(f"已导出 {len(jar)} 个 cookie" + (" (未变化)" if header == COOKIE else ""))


def login(sb):
    # 不走 reconnect：它内部 driver.reconnect() 在 GHA 上会弄死 chromedriver
    # （#82/#85 卡 300s，#87 换成 uc_open 后登录立刻通过）。
    sb.uc_open(BASE + "/")
    sb.sleep(3)  # uc_open 是 call_me_later 延迟 5 秒跳转
    sb.sleep(3)
    if COOKIE:
        inject_cookie(sb)
        # 只改这一处（CEO 控制变量）：run #85 的 300 秒卡死就在这里，
        # uc_open_with_reconnect 内部 driver.reconnect() 会把 chromedriver 弄死。
        sb.uc_open(BASE + "/")
        # uc_open 的跳转是 call_me_later 延迟 5 秒，等 3 秒足够。
        # 不用 sb.current_url —— BaseCase 没有这个属性（run #86 就是踩在这），
        # 要读 URL 得用 sb.get_current_url()。这轮不引入新变量，就睡 3 秒。
        sb.sleep(3)
        if logged_in(sb):
            log("Cookie 登录成功")
            return
        log("Cookie 已失效，改用账号密码登录")
    elif logged_in(sb):
        log("已是登录状态")
        return

    if not (EMAIL and PASSWORD):
        raise RuntimeError("未登录且没有设置 ORIHOST_EMAIL / ORIHOST_PASSWORD")
    sb.uc_open(BASE + "/auth/login")
    sb.sleep(3)
    sb.wait_for_element('input[type="password"]', timeout=40)
    try:
        sb.click("//button[normalize-space()='Got it']", by="xpath", timeout=2)
    except Exception:
        pass
    user_sel = ('form input:not([type="password"]):not([type="hidden"])'
                ':not([type="checkbox"]):not([type="submit"])')
    sb.type(user_sel, EMAIL)
    sb.type('input[type="password"]', PASSWORD)
    if not pass_turnstile(sb, X_SIGNIN):
        sb.save_screenshot("fail_login.png")
        raise RuntimeError("登录页 Turnstile 未通过")
    sb.click(X_SIGNIN, by="xpath")
    end = time.time() + 40
    while time.time() < end and not logged_in(sb):
        sb.sleep(1)
    if not logged_in(sb):
        sb.save_screenshot("fail_login.png")
        raise RuntimeError("账号密码登录失败（仍停在 /auth）")
    log("账号密码登录成功")


# ---------------------------------------------------------------- 续期

KILL_OVERLAY_JS = """
var n=0;
document.querySelectorAll('iframe,div,ins').forEach(function(e){
  var src=(e.src||'')+'';
  if(src.indexOf('challenges.cloudflare.com')>=0) return;
  if(e.querySelector && e.querySelector('iframe[src*="challenges.cloudflare.com"]')) return;
  var st=getComputedStyle(e);
  if(st.position==='fixed' && parseInt(st.zIndex||'0')>=2147483000){e.remove();n++;}
});
return n;
"""


COOKIE_JS = r"""
var hit=null;
document.querySelectorAll('div,section,aside').forEach(function(e){
  if(!hit && /We use cookies/i.test(e.innerText||'') && e.querySelector('button')) hit=e;
});
if(!hit) return 0;
var el=hit; while(el.parentElement && /We use cookies/i.test(el.parentElement.innerText||'') && el.parentElement.innerText.length<1500) el=el.parentElement;
var btns=[].slice.call(el.querySelectorAll('button,a'));
var b=btns.find(function(x){return /accept|agree|got it|understand|allow|continue|^ok/i.test((x.innerText||'').trim());})||btns[btns.length-1];
if(b){b.click();return 1;}
el.remove();return 2;
"""


def dismiss_cookie(sb):
    """面板新增的 Cookie 同意弹窗会挡住续期弹窗 -> 点同意（找不到按钮就移除）。"""
    try:
        r = sb.execute_script(COOKIE_JS) or 0
        if r:
            log("已关闭 Cookie 同意弹窗")
            sb.sleep(1)
        return r
    except Exception:
        return 0


def kill_overlays(sb):
    """移除广告插页（全屏 fixed、z-index 最大的 iframe/div），保留 Turnstile。"""
    dismiss_cookie(sb)
    try:
        n = sb.execute_script(KILL_OVERLAY_JS) or 0
        if n:
            log(f"已移除 {n} 个广告遮罩")
        return n
    except Exception:
        return 0


def driver_alive(sb) -> bool:
    """driver 还活着吗。2026-11-21：run #73 就是半路 driver 死掉，
    之后任何 window_handles 调用都抛 Connection refused 收场。"""
    try:
        sb.driver.window_handles
        return True
    except Exception:
        return False


def driver_call(sb, fn, *args, **kwargs):
    """调 WebDriver；连接被拒（driver 半路死）时等 2 秒重试一次。"""
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        msg = str(e)
        if not any(k in msg for k in ("Connection refused", "MaxRetryError",
                                      "NewConnectionError", "localhost")):
            raise
        log("⚠️ driver 连接被拒，2 秒后重试一次")
        time.sleep(2)
        return fn(*args, **kwargs)


_STEP = {"name": "启动"}


def step(name):
    """标记当前步骤，心跳日志会带出来（run #82 黑箱 4 分 49 秒的教训）。"""
    _STEP["name"] = name
    log(f"▶ {name}")


def start_heartbeat(interval=20):
    """后台线程每 interval 秒打一行心跳：当前步骤 + 已耗时。

    2026-11-21（冷王）：这里**不碰 sb.driver**。Selenium 的 client 不是线程安全的，
    心跳线程和主线程共用同一个 HTTP session 会互相插队，可能抛
    invalid session id，也可能把主线程正在等的响应吃掉（表现为偶发卡住）——
    那正好会污染我们加心跳要诊断的现象。心跳只回答「死在哪一步、跑了多久」。
    """
    stop = threading.Event()
    t0 = time.time()

    def _beat():
        while not stop.wait(interval):
            log(f"💓 心跳：步骤={_STEP['name']} 已耗时={int(time.time() - t0)}s")

    t = threading.Thread(target=_beat, daemon=True)
    t.start()
    return stop


def close_extra_tabs(sb, main):
    for h in list(driver_call(sb, lambda: sb.driver.window_handles)):
        if h != main:
            try:
                sb.driver.switch_to.window(h)
                sb.driver.close()
            except Exception:
                pass
    sb.driver.switch_to.window(main)


def safe_click(sb, xpath, timeout=20):
    sb.wait_for_element(xpath, by="xpath", timeout=timeout)
    kill_overlays(sb)
    try:
        sb.click(xpath, by="xpath", timeout=5)
    except Exception as e:
        if "intercepted" not in str(e) and "not clickable" not in str(e):
            raise
        kill_overlays(sb)
        sb.js_click(xpath, by="xpath")



def server_urls(sb):
    sb.uc_open(BASE + "/")   # 不走 reconnect（同上）
    sb.sleep(3)
    try:
        sb.wait_for_element("a[href*='/server/']", timeout=40)
    except Exception:
        log("首页没找到服务器链接")
    urls = set()
    for a in sb.find_elements("a[href*='/server/']"):
        m = re.search(r"/server/([0-9a-zA-Z]{8})", a.get_attribute("href") or "")
        if m:
            urls.add(f"{BASE}/server/{m.group(1)}")
    return sorted(urls)


def renewal_days(sb):
    try:
        txt = sb.get_text("body")
    except Exception:
        return None
    m = re.search(r"renewal in\s*:?\s*(\d+)\s*day", txt, re.I)
    return int(m.group(1)) if m else None


API_JS = r"""
const [method, path, body, done] = [arguments[0], arguments[1], arguments[2], arguments[arguments.length - 1]];
const m = document.cookie.match(/XSRF-TOKEN=([^;]+)/);
const h = {"Accept": "application/json", "Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest"};
if (m) h["X-XSRF-TOKEN"] = decodeURIComponent(m[1]);
fetch(path, {method, headers: h, credentials: "same-origin", body: body ? JSON.stringify(body) : undefined})
  .then(async r => done({status: r.status, text: await r.text()}))
  .catch(e => done({status: 0, text: String(e)}));
"""
STATE_CN = {"running": "运行中", "offline": "已关机", "starting": "启动中", "stopping": "停止中"}
AUTO_START_SKIP = {x.strip() for x in os.getenv("AUTO_START_SKIP", "").split(",") if x.strip()}
AUTO_START = os.getenv("AUTO_START", "1").strip().lower() not in ("0", "false", "no", "")


def panel_api(sb, method, path, body=None):
    """用浏览器里已登录的会话调用面板 client API（同源 fetch，带 XSRF）。"""
    sb.driver.set_script_timeout(20)
    return sb.driver.execute_async_script(API_JS, method, path, body)


def fmt_uptime(ms):
    sec = int(ms or 0) // 1000
    d, sec = divmod(sec, 86400)
    h, sec = divmod(sec, 3600)
    m = sec // 60
    return f"{d}d {h}h {m}m" if d else f"{h}h {m}m"


def api_state(sb, sid):
    try:
        r = panel_api(sb, "GET", f"/api/client/servers/{sid}/resources")
        if r and r.get("status") == 200:
            a = json.loads(r["text"])["attributes"]
            up = a.get("resources", {}).get("uptime", 0)
            return a.get("current_state"), (fmt_uptime(up) if up else None)
        log(f"{sid}: 状态接口返回 {r.get('status') if r else None}")
    except Exception as e:
        log(f"{sid}: 状态接口异常 {type(e).__name__}")
    return None, None


def server_info(sb, sid=None):
    """服务器状态和运行时长：优先 client API，失败再读页面。"""
    if sid:
        st, up = api_state(sb, sid)
        if st:
            return STATE_CN.get(st, st), up
    try:
        txt = sb.get_text("body")
    except Exception:
        return None, None
    m = re.search(r"UPTIME\s*(\d+d\s*\d+h\s*\d+m|\d+h\s*\d+m(?:\s*\d+s)?|\d+m\s*\d+s|Offline)", txt, re.I)
    if not m:
        return None, None
    if m.group(1).lower() == "offline":
        return "已关机", None
    return "运行中", m.group(1)


def ensure_running(sb, sid, rec):
    """关机就发开机信号，记录到报告。"""
    st, _ = api_state(sb, sid)
    if st != "offline" or not AUTO_START or sid in AUTO_START_SKIP:
        return
    r = panel_api(sb, "POST", f"/api/client/servers/{sid}/power", {"signal": "start"})
    if r and r.get("status") in (200, 204):
        log(f"{sid}: 已关机，已发送开机指令")
        for _ in range(12):
            time.sleep(5)
            st, up = api_state(sb, sid)
            if st in ("running", "starting"):
                break
        rec["status"], rec["uptime"] = STATE_CN.get(st, st), up
        rec["started"] = st in ("running", "starting")
        if not rec["started"]:
            rec["start_error"] = f"开机后状态仍为 {st}"
    else:
        rec["started"] = False
        rec["start_error"] = f"开机接口返回 {r.get('status') if r else None}"
        log(f"{sid}: {rec['start_error']}")


def open_server(sb, url):
    sb.uc_open(url)   # 不走 reconnect；后面 renewal_days 轮询会等页面到位
    sb.sleep(3)
    end = time.time() + 40
    while time.time() < end and renewal_days(sb) is None:
        sb.sleep(1)
    return renewal_days(sb)


def back_to_main(sb, main):
    sb.driver.switch_to.window(main)
    try:
        sb.driver.execute_cdp_cmd("Page.bringToFront", {})
    except Exception:
        pass
    try:
        sb.execute_script("window.dispatchEvent(new Event('focus'));"
                          "document.dispatchEvent(new Event('visibilitychange'));")
    except Exception:
        pass


def read_article(sb, sid, main):
    """点 Read Article -> 文章新标签停留 -> 回到面板（文章标签保持打开，页面要求 Keep it open）。"""
    old = set(sb.driver.window_handles)
    safe_click(sb, X_READ)
    end = time.time() + 15
    while time.time() < end and len(sb.driver.window_handles) == len(old):
        sb.sleep(1)
    new = [h for h in sb.driver.window_handles if h not in old]
    if new:
        sb.driver.switch_to.window(new[-1])
        sb.sleep(3)
        try:
            log(f"{sid}: 文章页 {sb.get_current_url()[:80]}")
        except Exception:
            pass
        sb.sleep(READ_SECONDS)
    else:
        log(f"{sid}: 没有弹出文章标签，直接等待 {READ_SECONDS}s")
        sb.sleep(READ_SECONDS)
    back_to_main(sb, main)
    sb.sleep(3)
    kill_overlays(sb)


# 2026-11-21（run 44894132904，4 天 -> 11 天，11 轮里唯一成功的一轮）：
# 这一段原来的判断被实测推翻，按事实重写——
#   * 面板确实有页面续期流程，不是「写给旧面板、永远匹配不到」。上游匹配不到是
#     因为按钮文案带后缀 / 大小写不同：`Renew server`（不是 `Renew`）、
#     `Read article`（不是 `Read Article`）。改成 contains + 大小写不敏感即可。
#   * 真正让天数上涨的是页面流程，不是接口：那次接口 complete 即使走到
#     token 长度=0 也回 400 "Failed to validate Turnstile data"，因为接口路径
#     不打开续期弹窗，拿不到 cf-turnstile-response（该 token 只能由
#     「点开弹窗 -> Read article -> widget 渲染」产生）。
# 但接口路径暂时保持默认开启：唯一一轮成功样本里 renew/begin 跑在页面流程之前，
# 无法排除它打开了续期窗口期。等 11-24 09:07 cron 再跑一轮（凑两轮样本）后再定，
# 期间不要动这个开关。
API_RENEW = os.getenv("API_RENEW", "1").strip().lower() not in ("0", "false", "no", "")


def api_renew(sb, sid, url, before):
    """接口续期（参考 JJJuines/orihost-renew）：renew/begin -> 等 dwell_seconds -> renewal/complete。
    成功返回续期后的天数；任何异常/未生效返回 None，交给页面点击流程兜底。"""
    try:
        r = panel_api(sb, "POST", f"/api/client/servers/{sid}/renew/begin")
        st = r.get("status") if r else None
        if st != 200:
            log(f"{sid}: 接口续期 begin 返回 {st}：{(r or {}).get('text', '')[:120]}，改用页面流程")
            if st == 419:
                raise RuntimeError("CSRF token mismatch")
            return None
        try:
            dwell = int(json.loads(r["text"]).get("dwell_seconds") or 15)
        except Exception:
            dwell = 15
        log(f"{sid}: 接口续期已开始，等待 {dwell + 2}s")
        time.sleep(min(dwell, 120) + 2)
        # complete 需要 cf-turnstile-response（#88：不带就固定回 400）。
        # 先读页面上现成的 token；没有就用 pass_turnstile（系统级鼠标 + 断 webdriver
        # 重渲染）现场过盾再读——阿滨试过 turnstile.render() 等回调，45 秒拿不到，
        # 能过的只有上游这套。
        token = ""
        try:
            open_server(sb, url)
            sb.sleep(2)
            token = ts_token(sb) or ""
            if not token:
                # #90 实锤：Renew 按钮一直没被点到，弹窗没开，所以页面上
                # iframes=0、modal 空、Turnstile 根本不存在。先点开弹窗再找盾。
                log(f"{sid}: 页面没有现成 token，先点开续期弹窗")
                try:
                    safe_click(sb, X_RENEW, timeout=15)
                except Exception as e:
                    log(f"{sid}: 点 Renew 打开弹窗失败 {type(e).__name__}: {str(e)[:120]}")
                sb.sleep(3)
                dump_buttons(sb, f"{sid}-弹窗")
                token = ts_token(sb) or ""
            if not token:
                log(f"{sid}: 弹窗里没有现成 token，走 pass_turnstile 过盾")
                try:
                    pass_turnstile(sb, X_RENEW, detach=True, timeout=60, require_token=True)
                except Exception as e:
                    log(f"{sid}: pass_turnstile 异常 {type(e).__name__}: {str(e)[:120]}")
                token = ts_token(sb) or ""
        except Exception as e:
            log(f"{sid}: 取 Turnstile token 异常 {type(e).__name__}: {str(e)[:150]}")
        log(f"{sid}: Turnstile token 长度={len(token)}")

        qs = ("?cf-turnstile-response=" + quote(token, safe="")) if token else ""
        try:
            r2 = panel_api(sb, "GET", f"/api/client/renewal/complete{qs}")
        except Exception as e:
            log(f"{sid}: complete 调用异常 {type(e).__name__}: {str(e)[:200]}")
            return None
        log(f"{sid}: complete 原始返回 status={r2.get('status') if r2 else None} "
            f"body={((r2 or {}).get('text') or '')[:500]}")
        try:
            d = json.loads((r2 or {}).get("text") or "{}")
        except Exception as e:
            log(f"{sid}: complete body 不是 JSON：{type(e).__name__}: {str(e)[:120]}")
            d = {}
        log(f"{sid}: complete 解析后 renewed={d.get('renewed_count')} "
            f"skipped={d.get('skipped_count')}")
        if not (r2 and r2.get("status") == 200 and (d.get("renewed_count") or 0) > 0):
            return None
        after = open_server(sb, url)
        log(f"{sid}: {before} 天 -> {after} 天（接口）")
        # 2026-11-23 冷王 #89：`before is None` 说明续期前没读到天数 —— 没有基准
        # 就没有「比原来晚」的证据，这属于「读不到」，不能当成功返回。
        # 原写法 `before is None or ...` 会把「续期前页面没加载出来」变成 ✅。
        if before is not None and after is not None and (after > before or after >= MAX_DAYS):
            return after
        return None
    except RuntimeError:
        raise
    except Exception as e:
        log(f"{sid}: 接口续期异常 {type(e).__name__}，改用页面流程")
        return None


def dump_buttons(sb, tag=""):
    """dump 页面结构：buttons / 含 renew|read|claim|article 的 links / iframe 数 / url / 弹窗文本。

    2026-11-21：九轮下来连 Renew 按钮长什么样都不知道。这个 dump 挂在失败路径上，
    任何一步抛异常都会打印，用来定位「选择器对不上」还是「弹窗压根没打开」。
    """
    try:
        info = sb.execute_script(
            "return {"
            "buttons: Array.from(document.querySelectorAll('button'))"
            ".map(function(b){return (b.textContent||'').trim()})"
            ".filter(function(t){return t}).slice(0,50),"
            "links: Array.from(document.querySelectorAll('a'))"
            ".map(function(a){return (a.textContent||'').trim()})"
            ".filter(function(t){return t}).slice(0,80),"
            "iframes: document.querySelectorAll('iframe').length,"
            "url: location.href,"
            "modal: (function(){"
            "var s=['.ReactModal__Content','div[role=\"dialog\"]','[aria-modal=\"true\"]',"
            "'[class*=\"odal\"]','[class*=\"ialog\"]'];"
            "for(var i=0;i<s.length;i++){var e=null;"
            "try{e=document.querySelector(s[i]);}catch(x){continue;}"
            "var t=e?(e.innerText||'').trim():'';"
            "if(t) return s[i]+' => '+t.slice(0,500);}"
            "return '';})(),"
            "disabled: Array.from(document.querySelectorAll('button'))"
            ".filter(function(b){return b.disabled;})"
            ".map(function(b){return (b.textContent||'').trim()}).slice(0,20)};")
        links = info.get("links") or []
        hit = [t for t in links
               if any(k in t.lower() for k in ("renew", "read", "claim", "article"))]
        log(f"🔎 dump{('[' + tag + ']') if tag else ''} url={info.get('url')}")
        log(f"🔎 dump buttons={info.get('buttons')}")
        log(f"🔎 dump iframes={info.get('iframes')}")
        log(f"🔎 dump disabled={info.get('disabled')}")
        log(f"🔎 dump modal={(info.get('modal') or '')[:400]}")
        log(f"🔎 dump links含renew/read/claim={hit}")
    except Exception as e:
        log(f"🔎 dump 失败：{type(e).__name__}: {e}")


def renew(sb, url):
    sid = url.rsplit("/", 1)[-1]
    try:
        before = open_server(sb, url)
        sb.sleep(2)
        kill_overlays(sb)
        main = sb.driver.current_window_handle
        # 面板续期上限约 21 天；已到上限时没有 Renew 按钮，视为无需续期
        if not sb.is_element_visible(X_RENEW, by="xpath"):
            sb.sleep(3)
        limit = sb.is_element_present("//*[contains(normalize-space(),'Renew Limit Reached')]", by="xpath")
        if limit or (not sb.is_element_visible(X_RENEW, by="xpath") and before is not None and before >= MAX_DAYS):
            log(f"{sid}: 剩余 {before} 天，已达上限，跳过")
            st, up = server_info(sb, sid)
            return {"before": before, "after": before, "status": st, "uptime": up, "skipped": True}
        if API_RENEW:
            after = api_renew(sb, sid, url, before)
            if after is not None:
                st, up = server_info(sb, sid)
                return {"before": before, "after": after, "status": st, "uptime": up, "via": "api"}
            open_server(sb, url)
            sb.sleep(2)
            kill_overlays(sb)
        safe_click(sb, X_RENEW)
        sb.sleep(2)
        # 弹窗开没开、里面有哪些按钮/iframe，一次打出来（#90 之前一直在猜选择器）
        dump_buttons(sb, f"{sid}-Renew后")
        close_extra_tabs(sb, main)

        for i in range(3):  # 广告可能吃掉 Read Article 的点击, 最多点 3 次
            read_article(sb, sid, main)
            end = time.time() + 15
            while time.time() < end and not sb.is_element_present(X_CLAIM, by="xpath"):
                sb.sleep(1)
            if sb.is_element_present(X_CLAIM, by="xpath"):
                break
            dismiss_cookie(sb)
            if sb.is_element_present(X_CLAIM, by="xpath"):
                break
            try:
                txt = sb.get_text("div[role='dialog'], .ReactModal__Content")[:160].replace("\n", " | ")
            except Exception:
                txt = "?"
            if "CSRF token mismatch" in txt:
                raise RuntimeError("CSRF token mismatch")
            dump_buttons(sb, f"第{i + 1}次")
            log(f"{sid}: 第 {i + 1} 次阅读后没出现 Claim Renewal（弹窗：{txt}），重试")
            sb.save_screenshot(f"fail_{sid}_read{i + 1}.png")
            close_extra_tabs(sb, main)
        sb.wait_for_element(X_CLAIM, by="xpath", timeout=10)
        # 文章是独立弹窗, 在 xvfb 里会盖住面板窗口, 系统级点击会点到文章上 -> 先关掉
        close_extra_tabs(sb, main)
        back_to_main(sb, main)
        sb.sleep(3)
        kill_overlays(sb)
        try:
            sb.click("//button[normalize-space()='Got it']", by="xpath", timeout=2)
        except Exception:
            pass

        if not pass_turnstile(sb, X_CLAIM, detach=True):
            raise RuntimeError("续期弹窗 Turnstile 未通过")
        safe_click(sb, X_CLAIM)
        sb.sleep(5)
        close_extra_tabs(sb, main)
        after = open_server(sb, url)
        log(f"{sid}: {before} 天 -> {after} 天")
        if before is not None and after is not None and (after > before or after >= MAX_DAYS):
            st, up = server_info(sb, sid)
            return {"before": before, "after": after, "status": st, "uptime": up}
        raise RuntimeError("剩余天数没有增加")
    except Exception:
        try:
            sb.save_screenshot(f"fail_{sid}.png")
        except Exception:
            pass
        raise



# ---------------------------------------------------------------- Telegram 推送

TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "").strip()
SCHEDULE_TEXT = os.getenv("SCHEDULE_TEXT", "每 3 天 09:00（北京时间）")


def mask_email(e):
    if "@" not in e:
        return (e[:1] + "***" + e[-1:]) if len(e) > 2 else "***"
    u, d = e.split("@", 1)
    return f"{u[:1]}***{u[-1:]}@{d}" if len(u) > 1 else f"{u}***@{d}"


def build_report(mode, ip, results, login_error=None):
    import html
    esc = html.escape
    bad = bool(login_error) or any(not r["ok"] for r in results)
    ok_n = sum(1 for r in results if r["ok"])
    head = "⚠️ <b>Orihost 运行报告（有异常）</b>" if bad else "✅ <b>Orihost 运行报告</b>"
    node = {"代理": "🟢 已启用（代理）", "直连": "⚪ 未启用（直连）"}.get(mode, f"🔴 {esc(mode)}")
    now = datetime.utcnow().timestamp() + 8 * 3600
    lines = [head, "",
             f"👤 账号：{esc(mask_email(EMAIL))}",
             f"🌐 节点状态：{node}",
             f"📍 出口IP：{esc(str(ip))}",
             f"🕐 时间：{datetime.utcfromtimestamp(now):%Y/%m/%d %H:%M}"]
    if login_error:
        lines.append(f"❌ 登录失败：{esc(login_error[:200])}")
    else:
        lines.append(f"📊 共 {len(results)} 台：续期成功 {ok_n}，异常 {len(results) - ok_n}")
    sep = "━━━━━━━━━━━━━━━"
    for r in results:
        lines += [sep, "", f"🖥 服务器 <code>#{esc(r['id'])}</code>"]
        if r.get("status"):
            lines.append(f"⚡ 状态：{r['status']}")
        if r.get("uptime"):
            lines.append(f"⏰ 运行时长：{esc(r['uptime'])}")
        if r.get("started"):
            lines.append("🔌 检测到关机，已自动开机")
        elif r.get("start_error"):
            lines.append(f"🔌 自动开机失败：{esc(r['start_error'])}")
        if r.get("skipped"):
            lines += ["✅ 已达续期上限，无需续期", f"📅 剩余：约 {r['before']} 天"]
        elif r["ok"]:
            lines += ["🎉 续期成功",
                      f"📅 续期前：约 {r['before']} 天",
                      f"✅ 续期后：约 {r['after']} 天"]
        else:
            lines += ["❌ 续期失败", f"⚠️ 原因：{esc(r.get('error') or '未知')[:200]}"]
            if r.get("before") is not None:
                lines.append(f"📅 剩余：约 {r['before']} 天")
    lines += [sep, "", f"⏰ 自动检查：{SCHEDULE_TEXT}"]
    return "\n".join(lines)


def tg_send(text):
    if not (TG_BOT_TOKEN and TG_CHAT_ID):
        log("未设置 TG_BOT_TOKEN / TG_CHAT_ID，跳过推送")
        return
    import json
    import urllib.request
    data = json.dumps({"chat_id": TG_CHAT_ID, "text": text, "parse_mode": "HTML",
                       "disable_web_page_preview": True}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
                                 data=data, headers={"Content-Type": "application/json"})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 直连
    for i in range(3):
        try:
            with opener.open(req, timeout=20) as resp:
                if resp.status == 200:
                    log("Telegram 推送成功")
                    return
        except Exception as e:
            log(f"Telegram 推送失败（第 {i + 1} 次）：{str(e)[:120]}")
            time.sleep(3)


# ---------------------------------------------------------------- main

def driver_error(e) -> bool:
    """是不是「驱动/浏览器起不来」那类错（区别于业务失败）。"""
    msg = str(e) or type(e).__name__
    return any(k in msg for k in (
        "Connection refused", "MaxRetryError", "NewConnectionError", "localhost",
        "WebDriver", "chromedriver", "session not created", "DevToolsActivePort"))


def _run_browser(kw, results, state):
    """开一只浏览器跑完整流程。driver 起不来就抛，由 main 决定是否重开。"""
    from seleniumbase import SB
    with SB(**kw) as sb:
        # 元素等待上限收到 20 秒：上一轮 driver 起不来时跑到 332 秒才超时，
        # 那是驱动连接层的问题，不该让每个 wait 都按默认 30-60 秒慢慢熬。
        try:
            sb.set_default_timeout(20)
        except Exception:
            pass
        # driver 健康检查：run #73 就是「SB 起来了但浏览器没起来」，
        # 之后第一次调 window_handles 才炸。这里先探一次，立刻定性。
        if not driver_alive(sb):
            raise RuntimeError(
                "driver 启动后连不上（window_handles 失败）→ 检查 Xvfb / Chrome / "
                "图形依赖；workflow 必须用 xvfb-run 包住 python 命令")
        log("✅ driver 健康检查通过")
        hb_stop = start_heartbeat()
        step("登录")
        try:
            login(sb)
        except Exception as e:
            state["login_error"] = str(e).splitlines()[0] if str(e) else type(e).__name__
            try:
                sb.save_screenshot("fail_login.png")
            except Exception:
                pass
            raise
        save_cookies(sb)  # 登录成功立即导出，续期失败也不影响 cookie 更新
        step("枚举服务器")

        urls = server_urls(sb)
        log(f"找到 {len(urls)} 台服务器")
        if not urls:
            # 冷王 #87 指出：results 为空时 any(...) 为 False → exit 0 → 假绿。
            # 一台都没枚举到必须判失败，否则「找不到服务器」会被看板当成成功。
            log("❌ 一台服务器都没枚举到（登录状态或页面结构问题），本轮判失败")
            results.append({"id": "-", "ok": False,
                            "error": "一台服务器都没枚举到（登录状态或页面结构变化）"})
        step("逐台续期")
        for u in urls:
            sid = u.rsplit("/", 1)[-1]
            rec = {"id": sid, "ok": False, "error": None, "before": None}
            for attempt in range(1, 3):  # 每台最多 2 次
                try:
                    step(f"续期 {u.rsplit('/', 1)[-1]} 第{attempt}次")
                    step(f"续期 {u.rsplit('/', 1)[-1]} 第{attempt}次")
                    info = renew(sb, u)
                    rec.update(info, ok=True)
                    break
                except Exception as e:
                    msg = str(e).splitlines()[0][:300] if str(e).strip() else "页面元素未出现"
                    if msg.startswith("Message:"):
                        msg = "页面元素未出现（Claim Renewal / 按钮）"
                    rec["error"] = msg
                    dump_buttons(sb, f"{sid}失败{attempt}")
                    log(f"{u} 第 {attempt} 次续期失败：{msg}")
                    if "CSRF" in msg:  # 会话 token 不一致 -> 清 cookie 用账号密码重新登录
                        # 2026-11-23：CSRF 不一致必须显式记成失败信号。
                        # 实证 run 45000404309：CSRF -> 清 cookie 重登 -> 重登抛错 -> results 为空
                        # -> 旧版 `any(not r["ok"] for r in results)` 在空列表上恒 False -> 0/0 绿。
                        state["csrf_mismatch"] = True
                        log("CSRF 不一致，清除 cookie 后重新登录")
                        global COOKIE
                        COOKIE = ""
                        sb.driver.delete_all_cookies()
                        login(sb)
                        save_cookies(sb)
            if not rec["ok"]:
                try:
                    rec["before"] = renewal_days(sb)
                    rec["status"], rec["uptime"] = server_info(sb, sid)
                except Exception:
                    pass
            try:
                ensure_running(sb, sid, rec)
            except Exception as e:
                log(f"{sid}: 自动开机异常 {type(e).__name__}")
            results.append(rec)
        save_cookies(sb)


def main():
    log(f"凭证路径: {credential_path()}")
    from seleniumbase import SB

    proxy_url, ip, mode = setup_network()
    log(f"网络模式：{mode}，出口 IP：{ip}")
    log(f"平台={sys.platform} DISPLAY={os.environ.get('DISPLAY') or '（空）'}")
    # 2026-11-21：xvfb 交给 workflow 的 xvfb-run 显式管理（SB 自管那套在 GHA 上
    # 起不来：driver 连 localhost:43457 被拒、332 秒才超时）。这里只要求有 DISPLAY。
    if sys.platform.startswith("linux") and not os.environ.get("DISPLAY"):
        log("⚠️ DISPLAY 为空：headless=False 需要 Xvfb（workflow 用 xvfb-run 提供）")
    kw = dict(uc=True, test=True, headless=False, locale_code="en",
              page_load_strategy="eager")
    if proxy_url:
        kw["proxy"] = proxy_url.split("://", 1)[-1]  # SB 要 host:port

    results = []
    state = {"login_error": None, "csrf_mismatch": False}
    try:
        # load藤 #73 要求：seleniumbase 报 localhost:port refused 时重启 driver 重试一次，
        # 别整个 run 直接挂。这里直接重开整只浏览器（driver 半路死了重连不可靠）。
        for attempt in (1, 2):
            try:
                _run_browser(kw, results, state)
                break
            except Exception as e:
                if attempt == 1 and driver_error(e):
                    log("⚠️ driver 起不来 / 半路死掉，重开浏览器重试一次")
                    time.sleep(3)
                    continue
                raise
    except Exception as e:
        first = str(e).splitlines()[0] if str(e) else type(e).__name__
        # 2026-11-21 run 44886971638：driver 连 localhost:43457 被拒、332 秒才超时。
        # 那是环境问题（Xvfb/Chrome 没起来），不是业务失败，报错要说人话。
        if driver_error(e):
            first = (f"浏览器驱动起不来（{first[:100]}）→ 检查 Xvfb / Chrome / "
                     f"图形依赖；headless=False 必须有可用的 Xvfb")
        if state["login_error"] is None:
            results.append({"id": "-", "ok": False, "error": f"脚本异常：{first[:200]}"})
        log("运行异常：", e)
    finally:
        try:
            hb_stop.set()
        except Exception:
            pass
        try:
            tg_send(build_report(mode, ip, results, state["login_error"]))
        except Exception as e:
            log("生成/发送报告失败：", e)

    ok = sum(1 for r in results if r["ok"])
    log(f"完成：{ok}/{len(results)} 台成功续期")

    # 2026-11-23 修「退出码不跟结果走」：
    #   旧写法 sys.exit(1 if login_error or any(not r["ok"] for r in results) else 0)
    #   在 results 为空时 any(...) 恒为 False → 一台没成也报 0/0 且 job 绿灯。
    # 现在：登录失败 / 结果为空 / 零成功 / 存在失败项 / 出现 CSRF 不一致，任一命中即非零退出。
    reasons = []
    if state.get("login_error"):
        reasons.append(f"登录失败（{state['login_error']}）")
    if not results:
        reasons.append("结果为空（0/0，没有任何一台进入续期流程）")
    elif ok == 0:
        reasons.append(f"零成功（{ok}/{len(results)}）")
    if any(not r["ok"] for r in results):
        reasons.append(f"存在失败项（{len(results) - ok} 台）")
    if state.get("csrf_mismatch"):
        reasons.append("出现 CSRF token mismatch（会话不一致，本轮不可信）")

    if reasons:
        log("❌ 本轮判失败：" + "；".join(reasons) + " → 退出码 1")
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
