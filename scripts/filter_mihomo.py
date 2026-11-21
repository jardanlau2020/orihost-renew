#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mihomo 配置预检 / 清洗。

背景（2026-11-21，orihost-renew run #74）：
    mihomo 1.19.25 起对 proxy 字段做严格校验，遇到
        fatal: Parse config error: proxy 63: '' has unset fields: tls-crypt
    直接拒启动。订阅里混进了残缺节点（空名字、缺必填字段、或者 mihomo 压根
    不认的类型），一个坏节点就让整份配置起不来 —— 结果就是「家宽没起来 →
    回落机房 IP → Cloudflare 拦」。

本脚本做的事：在 mihomo 启动**之前**把配置读一遍，丢掉解析不了的节点，并把
proxy-groups 里指向这些节点的引用一并清掉（否则 mihomo 会报
`proxy group X: proxy Y not found`，一样起不来）。

用法：
    python filter_mihomo.py <in.yaml> [out.yaml]
    python filter_mihomo.py --check <in.yaml>      # 只报告，不写文件

退出码：
    0 = 配置可用（可能丢掉了若干节点）
    1 = 清洗后一个节点都不剩，或输入不是合法 YAML —— 交给上层决定是否跳过代理

刻意不依赖第三方库以外的任何东西，PyYAML 即可。
"""
from __future__ import annotations

import argparse
import sys

import yaml

# mihomo 认的 proxy type（1.19.x）。不在表里的一律丢——留着必炸。
SUPPORTED_TYPES = {
    "ss", "ssr", "vmess", "vless", "trojan", "hysteria", "hysteria2",
    "tuic", "wireguard", "socks5", "http", "snell", "ssh", "mieru", "anytls",
    "openvpn",
}

# 每种类型的必填字段（值为空 / 缺失 → 丢）
REQUIRED = {
    "ss": ("server", "port", "cipher", "password"),
    "ssr": ("server", "port", "cipher", "password", "protocol", "obfs"),
    "vmess": ("server", "port", "uuid"),
    "vless": ("server", "port", "uuid"),
    "trojan": ("server", "port", "password"),
    "hysteria": ("server", "port"),
    "hysteria2": ("server", "port"),
    "tuic": ("server", "port"),
    "wireguard": ("server", "port", "private-key"),
    "socks5": ("server", "port"),
    "http": ("server", "port"),
    "snell": ("server", "port", "psk"),
    "ssh": ("server", "port", "username"),
    "mieru": ("server", "port", "username", "password"),
    "anytls": ("server", "port", "password"),
    # openvpn 是 mihomo 新加的类型，tls-crypt 属于它的必填项之一；
    # run #74 里 proxy 63 就是栽在 tls-crypt 上。
    "openvpn": ("server", "port", "tls-crypt"),
}

# 这些字段只要出现就必须非空，否则 mihomo 会报 "has unset fields"
CRITICAL_KEYS = {
    "name", "type", "server", "port", "uuid", "password", "cipher",
    "tls-crypt", "tls-auth", "private-key", "public-key", "psk",
    "username", "protocol", "obfs", "plugin", "servername",
}


def _blank(v) -> bool:
    if v is None:
        return True
    if isinstance(v, str) and not v.strip():
        return True
    return False


def why_bad(p: dict) -> str:
    """返回丢弃原因；空字符串表示这个节点没问题。"""
    if not isinstance(p, dict):
        return "不是映射"
    name = p.get("name")
    if _blank(name):
        return "name 为空"
    ptype = p.get("type")
    if _blank(ptype):
        return "type 为空"
    if ptype not in SUPPORTED_TYPES:
        return f"type={ptype} 不在 mihomo 支持列表"
    for k in REQUIRED.get(ptype, ("server", "port")):
        if _blank(p.get(k)):
            return f"{ptype} 缺必填字段 {k}"
    for k, v in p.items():
        if k in CRITICAL_KEYS and _blank(v):
            return f"字段 {k} 为空"
    port = p.get("port")
    try:
        iport = int(port)
        if not (0 < iport < 65536):
            return f"port 越界 {port}"
    except Exception:
        return f"port 不是整数 {port!r}"
    return ""


def filter_config(data: dict, verbose: bool = True):
    proxies = data.get("proxies") or []
    kept, dropped = [], []
    seen = set()
    for idx, p in enumerate(proxies):
        reason = why_bad(p)
        if not reason:
            name = str(p.get("name"))
            if name in seen:
                reason = f"name 重复：{name}"
            else:
                seen.add(name)
        if reason:
            dropped.append((idx, (p or {}).get("name") if isinstance(p, dict) else p, reason))
        else:
            kept.append(p)

    data["proxies"] = kept
    kept_names = {str(p.get("name")) for p in kept}

    # proxy-groups 里指向被丢节点的引用必须清掉，否则 mihomo 一样起不来
    removed_refs = 0
    for g in (data.get("proxy-groups") or []):
        refs = g.get("proxies")
        if not isinstance(refs, list):
            continue
        new_refs = [r for r in refs if r in kept_names or r in ("DIRECT", "REJECT", "PASS", "COMPATIBLE")]
        removed_refs += len(refs) - len(new_refs)
        # 组里一个都不剩 → 塞 DIRECT，避免空组报错
        g["proxies"] = new_refs or ["DIRECT"]

    # rules 里如果直接点名了被丢的节点（少见但可能），改成 DIRECT
    fixed_rules = 0
    new_rules = []
    for r in (data.get("rules") or []):
        parts = str(r).split(",")
        if len(parts) >= 2 and parts[1] not in kept_names and parts[1] not in ("DIRECT", "REJECT", "PASS", "COMPATIBLE"):
            parts[1] = "DIRECT"
            fixed_rules += 1
            r = ",".join(parts)
        new_rules.append(r)
    data["rules"] = new_rules

    if verbose:
        print(f"[filter] 节点 总 {len(proxies)} → 保留 {len(kept)}，丢弃 {len(dropped)}")
        for idx, name, reason in dropped[:20]:
            print(f"[filter]   - #{idx} {name!r}: {reason}")
        if len(dropped) > 20:
            print(f"[filter]   ... 其余 {len(dropped) - 20} 个同类")
        print(f"[filter] proxy-groups 清掉失效引用 {removed_refs} 条；rules 改写 {fixed_rules} 条")

    return kept, dropped


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("dst", nargs="?")
    ap.add_argument("--check", action="store_true", help="只报告，不写文件")
    ap.add_argument(
        "--in-place", action="store_true",
        help="明确允许原地覆盖 src（不指定 dst 时必须带这个，防手滑）",
    )
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument(
        "--allow-loss", action="store_true",
        help="住宅节点全丢时仍然 exit 0（默认 exit 2，避免 workflow 误判成功）",
    )
    args = ap.parse_args(argv)

    # 2026-11-21（冷王 review）：原来 dst 缺省就原地覆盖 src，workflow 一旦
    # 不带 dst 调用，会把住宅节点从订阅文件里真删掉。改成必须显式表态。
    if not args.check and not args.dst and not args.in_place:
        print(
            "[filter] ❌ 未指定输出文件。要原地覆盖请显式加 --in-place，"
            "否则请给第二个参数作为输出路径（推荐，保留原文件）。",
            file=sys.stderr,
        )
        return 1

    try:
        with open(args.src, encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except Exception as e:
        print(f"[filter] ❌ 读 YAML 失败: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    if not isinstance(data, dict):
        print("[filter] ❌ 顶层不是映射，配置结构不对", file=sys.stderr)
        return 1

    kept, dropped = filter_config(data, verbose=not args.quiet)
    if not kept:
        print("[filter] ❌ 清洗后没有任何可用节点", file=sys.stderr)
        return 1

    # 2026-11-21（冷王 review 第 ② 条）：住宅节点被丢光时不能 exit 0。
    # 否则 workflow 以为「家宽已就绪」，实际出口是机房 IP —— 和 #5 的
    # 「不静默降级直连」原则直接冲突，Cloudflare 那边照样拦。
    home_dropped = [d for d in dropped if "家宽" in str(d[1] or "")]
    home_kept = [p for p in kept if "家宽" in str(p.get("name") or "")]
    residential_lost = bool(home_dropped) and not home_kept

    if args.check:
        print("[filter] --check：不写文件")
    else:
        dst = args.dst or args.src
        with open(dst, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False, width=100000)
        print(f"[filter] ✅ 已写入 {dst}")
    if residential_lost:
        print(
            f"[filter] ❌ 住宅节点全部被丢弃（{len(home_dropped)} 个），"
            "清洗后的出口只剩机房 IP —— 这份配置对 Cloudflare 没有意义。",
            file=sys.stderr,
        )
        print(
            "[filter]   住宅节点是 openvpn 类型且缺 tls-crypt，mihomo 解析不了；"
            "改用 scripts/openvpn_egress.py 直接开隧道。",
            file=sys.stderr,
        )
        if not args.allow_loss:
            return 2
        print("[filter] ⚠️ --allow-loss 已指定，仍返回 0")
    return 0


if __name__ == "__main__":
    sys.exit(main())
