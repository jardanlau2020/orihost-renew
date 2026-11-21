#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从订阅配置里挑一个 openvpn 住宅节点，生成 .ovpn 并起隧道。

背景（2026-11-21，orihost-renew run #74）：
    订阅里的 69 个住宅节点（`🏠 XX-家宽-NN`）**全部是 `type: openvpn`**。
    mihomo 1.19.25 的 openvpn 适配器强制要求 `tls-crypt` 非空，而这批节点
    根本没这个字段，于是 mihomo 直接
        fatal: Parse config error: proxy 63: '' has unset fields: tls-crypt
    整份配置起不来。用 filter_mihomo.py 把坏节点丢掉虽然能让 mihomo 起来，
    但丢掉的正正就是全部住宅出口 —— 剩下的 63 个全是 vless/trojan 机房节点，
    等于白干。

所以换一条路：**不让 mihomo 解析 openvpn，直接起一个真正的 openvpn 客户端。**
节点里已经内联了 `ca` / `cert` / `key` 和账号 `vpn/vpn`，够开隧道了。隧道起来
之后，续期脚本直接走系统出口，不需要任何 http/socks 代理。

用法：
    python openvpn_egress.py --config home_sub.yaml --list
    python openvpn_egress.py --config home_sub.yaml --name '🏠 KR-家宽-01' --out node.ovpn
    python openvpn_egress.py --config home_sub.yaml --pick-kr --out node.ovpn

退出码：0 = 生成了 .ovpn；1 = 找不到可用节点 / 缺关键字段
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile

import yaml

# openvpn 客户端必需字段（节点里缺任何一项都开不了隧道）
NEEDED = ("server", "port", "ca", "cert", "key")


def load_nodes(path: str):
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    out = []
    for n in (data.get("proxies") or []):
        if not isinstance(n, dict):
            continue
        if n.get("type") != "openvpn":
            continue
        if any(not str(n.get(k) or "").strip() for k in NEEDED):
            continue
        out.append(n)
    return out


def render_ovpn(n: dict, auth_path: str = "") -> str:
    name = str(n.get("name") or "node")
    proto = str(n.get("proto") or "tcp").lower()
    lines = [
        f"# generated from subscription node: {name}",
        "client",
        "dev tun",
        f"proto {proto}",
        f"remote {n['server']} {n['port']}",
        "resolv-retry infinite",
        "nobind",
        "persist-key",
        "persist-tun",
        "verb 3",
        f"cipher {n.get('cipher') or 'AES-128-CBC'}",
        f"auth {n.get('auth') or 'SHA1'}",
        # 住宅节点一般是自签 CA，不校验对端名字
        "remote-cert-tls server",
        "auth-nocache",
        # 把订阅里的 DNS 写进来，避免解析走本地
        "pull",
    ]
    if str(n.get("udp", "")).lower() in ("true", "1"):
        lines = [l for l in lines if not l.startswith("proto ")] + ["proto udp"]
    if auth_path:
        lines.append(f"auth-user-pass {auth_path}")
    body = ["<ca>", str(n["ca"]).strip(), "</ca>",
            "<cert>", str(n["cert"]).strip(), "</cert>",
            "<key>", str(n["key"]).strip(), "</key>"]
    return "\n".join(lines + body) + "\n"


def pick(nodes, want_name=None, region=None):
    if want_name:
        for n in nodes:
            if str(n.get("name")) == want_name:
                return n
        return None
    if region:
        for n in nodes:
            if region.upper() in str(n.get("name")).upper():
                return n
        return None
    return nodes[0] if nodes else None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True, help="mihomo 订阅 yaml")
    ap.add_argument("--name", help="按节点名精确挑")
    ap.add_argument("--region", help="按地区关键字挑，例如 KR / JP / MY")
    ap.add_argument("--list", action="store_true", help="只列出可用住宅节点")
    ap.add_argument("--out", help="输出 .ovpn 路径")
    args = ap.parse_args(argv)

    try:
        nodes = load_nodes(args.config)
    except Exception as e:
        print(f"[ovpn] ❌ 读配置失败: {type(e).__name__}: {e}", file=sys.stderr)
        return 1

    if args.list:
        print(f"[ovpn] 可用住宅节点 {len(nodes)} 个：")
        for n in nodes:
            print(f"[ovpn]   {n.get('name')}  {n.get('server')}:{n.get('port')} {n.get('proto')}")
        return 0

    if not nodes:
        print("[ovpn] ❌ 订阅里没有字段完整的 openvpn 住宅节点", file=sys.stderr)
        return 1

    n = pick(nodes, args.name, args.region)
    if not n:
        print(f"[ovpn] ❌ 找不到节点 name={args.name!r} region={args.region!r}", file=sys.stderr)
        print(f"[ovpn] 可选：{[x.get('name') for x in nodes[:10]]}", file=sys.stderr)
        return 1

    # 2026-11-21（冷王 review 第 ③ 条）：账号密码不能写进仓库检出目录。
    # 原来写 ./auth.txt，会留在 $GITHUB_WORKSPACE 里；改到 /tmp 且 chmod 600，
    # .ovpn 里用绝对路径引用。
    auth_path = ""
    if n.get("username") or n.get("password"):
        # 用 tempfile.gettempdir()：Linux/GHA 上是 /tmp，本机 Windows 上也能跑，
        # 不写进仓库检出目录。
        auth_path = os.path.join(
            tempfile.gettempdir(), f"orihost-ovpn-auth-{os.getpid()}.txt")
        fd = os.open(auth_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(f"{n.get('username') or 'vpn'}\n{n.get('password') or 'vpn'}\n")
        try:
            os.chmod(auth_path, 0o600)
        except Exception:
            pass  # Windows 上 chmod 语义有限，不影响功能
        print(f"[ovpn] 🔐 账号文件写到 {auth_path}（600，不进工作区）")

    text = render_ovpn(n, auth_path)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"[ovpn] ✅ 已生成 {args.out}（节点 {n.get('name')} {n.get('server')}:{n.get('port')}）")
        print("[ovpn] 起隧道：sudo openvpn --config " + args.out + " --daemon --log /tmp/ovpn.log")
        print("[ovpn] 验证出口：curl -s --max-time 15 https://api.ipify.org")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
