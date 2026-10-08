#!/bin/bash
# setup_proxy.sh - 代理节点解析与 sing-box 启动
export LC_ALL=C
set -e

# 默认测试节点（可通过环境变量覆盖）
export NODE_LINK=${NODE_LINK:-''}

if [ -z "$NODE_LINK" ]; then
  echo "[INFO] 未配置代理，直连模式"
  echo "IS_PROXY=false" >> $GITHUB_ENV
  exit 0
fi

if ! command -v jq &> /dev/null; then
  echo "[ERROR] jq 未安装，正在安装..."
  sudo apt-get update && sudo apt-get install -y jq
fi

# 晴天 patch：原寫法 `A && B || C && D` 優先級錯誤（curl 存在時都會選 wget）且 curl 缺 -L 跟 redirect
if command -v curl >/dev/null 2>&1; then
  COMMAND="curl -sLo"
elif command -v wget >/dev/null 2>&1; then
  COMMAND="wget -qO"
else
  echo "Error: neither curl nor wget found." >&2; exit 1
fi

# 晴天 patch：CWD 已有可用 sing-box 就跳過下載（GHA runner 首跑必無，照舊下載）
if [ -x ./sing-box ] && ./sing-box version >/dev/null 2>&1; then
  echo "[INFO] 檢測到本地 sing-box，跳過下載"
else

echo "[INFO] 获取 sing-box 最新版本..."
# 晴天 patch 2026-10-08：GHA runner 打未認證 GitHub API 常撞 rate limit（60/h/IP），
# 回嘅係 {"message":"API rate limit exceeded..."} 而唔係陣列 → jq 報
# "Cannot index string with string \"prerelease\"" 並因 set -e 直接殺死腳本，
# 連下面嘅 fallback 都去唔到（實證 run 37807287836，白白報「代理起唔到」）。
# 加 `2>/dev/null || true` 令查唔到就落 fallback 版本。
latest_version=$(curl -s "https://api.github.com/repos/SagerNet/sing-box/releases" 2>/dev/null | jq -r '[.[] | select(.prerelease==false)][0].tag_name | sub("^v"; "")' 2>/dev/null || true)
if [ -z "$latest_version" ] || [ "$latest_version" = "null" ]; then
  echo "[ERROR] 无法获取 sing-box 最新版本,将下载v1.13.16"
  export latest_version=1.13.16
fi
echo "[INFO] 最新稳定版本: v${latest_version}"

ARCH_RAW=$(uname -m)
case "${ARCH_RAW}" in
    'x86_64' | 'amd64')  ARCH='amd64' ;;
    'x86' | 'i686' | 'i386') ARCH='386' ;;
    'aarch64' | 'arm64') ARCH='arm64' ;;
    'armv7l')  ARCH='armv7' ;;
    's390x')   ARCH='s390x' ;;
    *) echo "不支持的架构: ${ARCH_RAW}"; exit 1 ;;
esac

download_version="$latest_version"
if ! $COMMAND "sing-box-${download_version}-linux-${ARCH}.tar.gz" "https://github.com/SagerNet/sing-box/releases/download/v${download_version}/sing-box-${download_version}-linux-${ARCH}.tar.gz"; then
    echo "[WARN] 下载版本 v${download_version} 失败，尝试备用版本 v1.13.16"
    download_version="1.13.16"
    if ! $COMMAND "sing-box-${download_version}-linux-${ARCH}.tar.gz" "https://github.com/SagerNet/sing-box/releases/download/v${download_version}/sing-box-${download_version}-linux-${ARCH}.tar.gz"; then
        echo "[ERROR] 备用版本也下载失败，请检查网络连接"
        exit 1
    fi
fi

# 解压并授权
tar -xzf "sing-box-${download_version}-linux-${ARCH}.tar.gz"
mv "sing-box-${download_version}-linux-${ARCH}/sing-box" ./
rm -f "sing-box-${download_version}-linux-${ARCH}.tar.gz"
rm -rf "sing-box-${download_version}-linux-${ARCH}"
chmod +x sing-box

fi  # 晴天 patch：本地已有 sing-box 跳過下載

proto=$(echo "$NODE_LINK" | cut -d':' -f1)
content="${NODE_LINK#*://}"
content="${content%%#*}"

echo "[INFO] 协议: $proto"
# echo "[INFO] 原始内容: $content"

# 初始化变量
outbound_type=""
outbound_server=""
outbound_port=""
outbound_uuid=""
outbound_flow=""
outbound_transport_type="tcp"
outbound_path="/"
outbound_host=""
outbound_security="none"
outbound_sni=""
outbound_fingerprint="chrome"
outbound_reality_pbk=""
outbound_reality_sid=""
outbound_password=""
outbound_up_mbps=100
outbound_down_mbps=100
outbound_obfs_password=""
outbound_auth=""
outbound_congestion="bbr"
outbound_udp_over_stream="true"
outbound_zerortt="false"
outbound_username=""
outbound_password2=""
outbound_version="5"
outbound_insecure="false"
outbound_alpn=""

# 辅助函数：URL 解码
url_decode() {
  local encoded="$1"
  printf '%b' "$(echo "$encoded" | sed 's/%/\\x/g')"
}

case "$proto" in
  vless)
    uuid_host="${content#*://}"
    uuid="${uuid_host%%@*}"
    rest="${uuid_host#*@}"
    if [[ "$rest" == *"?"* ]]; then
      host_port="${rest%%\?*}"
      query="${rest#*\?}"
    else
      host_port="$rest"
      query=""
    fi
    outbound_server="${host_port%:*}"
    outbound_port="${host_port#*:}"
    outbound_uuid="$uuid"
    outbound_type="vless"
    if [ -n "$query" ]; then
      flow=$(echo "$query" | grep -o 'flow=[^&]*' | cut -d= -f2)
      [ -n "$flow" ] && outbound_flow="$flow"
      ttype=$(echo "$query" | grep -o 'type=[^&]*' | cut -d= -f2)
      [ -n "$ttype" ] && outbound_transport_type="$ttype"
      path_raw=$(echo "$query" | grep -o 'path=[^&]*' | cut -d= -f2)
      if [ -n "$path_raw" ]; then
        path_decoded=$(url_decode "$path_raw")
        outbound_path="${path_decoded%%\?*}"
      fi
      host=$(echo "$query" | grep -o 'host=[^&]*' | cut -d= -f2)
      [ -n "$host" ] && outbound_host="$host"
      sec=$(echo "$query" | grep -o 'security=[^&]*' | cut -d= -f2)
      [ -n "$sec" ] && outbound_security="$sec"
      sni=$(echo "$query" | grep -o 'sni=[^&]*' | cut -d= -f2)
      [ -n "$sni" ] && outbound_sni="$sni"
      fp=$(echo "$query" | grep -o 'fp=[^&]*' | cut -d= -f2)
      [ -n "$fp" ] && outbound_fingerprint="$fp"
      pbk=$(echo "$query" | grep -o 'pbk=[^&]*' | cut -d= -f2)
      [ -n "$pbk" ] && outbound_reality_pbk="$pbk"
      sid=$(echo "$query" | grep -o 'sid=[^&]*' | cut -d= -f2)
      [ -n "$sid" ] && outbound_reality_sid="$sid"
      # 晴天 patch 2026-10-08：alpn 原本只喺 tuic 分支解析，vless 完全冇 →
      # 要求鎖 http/1.1 嘅節點（ws 走 CF）TLS 通唔到。實證：run 37806041608（SG 328）。
      alpn_raw=$(echo "$query" | grep -o 'alpn=[^&]*' | cut -d= -f2)
      if [ -n "$alpn_raw" ]; then
        outbound_alpn=$(url_decode "$alpn_raw")
      fi
      ins=$(echo "$query" | grep -o 'insecure=[^&]*' | cut -d= -f2)
      [ "$ins" = "1" ] || [ "$ins" = "true" ] && outbound_insecure="true"
      alins=$(echo "$query" | grep -o 'allowInsecure=[^&]*' | cut -d= -f2)
      [ "$alins" = "1" ] || [ "$alins" = "true" ] && outbound_insecure="true"
      # 晴天 patch 2026-10-08：trojan 都要解析 alpn（同 vless 一樣嘅理由）
      alpn_raw=$(echo "$query" | grep -o 'alpn=[^&]*' | cut -d= -f2)
      if [ -n "$alpn_raw" ]; then
        outbound_alpn=$(url_decode "$alpn_raw")
      fi
    fi
    [ -z "$outbound_host" ] && outbound_host="$outbound_server"
    [ -z "$outbound_sni" ] && outbound_sni="$outbound_server"
    ;;

  vmess)
    b64="${content}"
    mod=$(( ${#b64} % 4 ))
    if [ $mod -eq 2 ]; then b64="${b64}=="; elif [ $mod -eq 3 ]; then b64="${b64}="; fi
    decoded=$(echo "$b64" | base64 -d 2>/dev/null)
    if [ -z "$decoded" ]; then
      echo "[ERROR] VMess 解码失败"
      exit 1
    fi
    add=$(echo "$decoded" | jq -r '.add // ""')
    port=$(echo "$decoded" | jq -r '.port // 443')
    id=$(echo "$decoded" | jq -r '.id // ""')
    aid=$(echo "$decoded" | jq -r '.aid // 0')
    net=$(echo "$decoded" | jq -r '.net // "tcp"')
    tls=$(echo "$decoded" | jq -r '.tls // ""')
    sni=$(echo "$decoded" | jq -r '.sni // ""')
    host=$(echo "$decoded" | jq -r '.host // ""')
    path_raw=$(echo "$decoded" | jq -r '.path // "/"')
    path_decoded=$(url_decode "$path_raw")
    outbound_path="${path_decoded%%\?*}"
    fp=$(echo "$decoded" | jq -r '.fp // "chrome"')
    scy=$(echo "$decoded" | jq -r '.scy // "auto"')
    outbound_type="vmess"
    outbound_server="$add"
    outbound_port="$port"
    outbound_uuid="$id"
    outbound_transport_type="$net"
    outbound_host="${host:-$add}"
    outbound_sni="${sni:-$add}"
    outbound_fingerprint="$fp"
    outbound_security="$tls"
    outbound_flow=""
    ;;

  trojan)
    pass_rest="${content#*://}"
    password="${pass_rest%%@*}"
    rest="${pass_rest#*@}"
    if [[ "$rest" == *"?"* ]]; then
      host_port="${rest%%\?*}"
      query="${rest#*\?}"
    else
      host_port="$rest"
      query=""
    fi
    outbound_server="${host_port%:*}"
    outbound_port="${host_port#*:}"
    outbound_password="$password"
    outbound_type="trojan"
    if [ -n "$query" ]; then
      ttype=$(echo "$query" | grep -o 'type=[^&]*' | cut -d= -f2)
      [ -n "$ttype" ] && outbound_transport_type="$ttype"
      path_raw=$(echo "$query" | grep -o 'path=[^&]*' | cut -d= -f2)
      if [ -n "$path_raw" ]; then
        path_decoded=$(url_decode "$path_raw")
        outbound_path="${path_decoded%%\?*}"
      fi
      host=$(echo "$query" | grep -o 'host=[^&]*' | cut -d= -f2)
      [ -n "$host" ] && outbound_host="$host"
      sni=$(echo "$query" | grep -o 'sni=[^&]*' | cut -d= -f2)
      [ -n "$sni" ] && outbound_sni="$sni"
      fp=$(echo "$query" | grep -o 'fp=[^&]*' | cut -d= -f2)
      [ -n "$fp" ] && outbound_fingerprint="$fp"
      ins=$(echo "$query" | grep -o 'insecure=[^&]*' | cut -d= -f2)
      [ "$ins" = "1" ] || [ "$ins" = "true" ] && outbound_insecure="true"
      alins=$(echo "$query" | grep -o 'allowInsecure=[^&]*' | cut -d= -f2)
      [ "$alins" = "1" ] || [ "$alins" = "true" ] && outbound_insecure="true"
    fi
    [ -z "$outbound_host" ] && outbound_host="$outbound_server"
    [ -z "$outbound_sni" ] && outbound_sni="$outbound_server"
    ;;

  hysteria2|hy2)
    auth=""
    if [[ "$content" == *"@"* ]]; then
      auth="${content%%@*}"
      host_port="${content#*@}"
    else
      host_port="$content"
    fi
    if [[ "$host_port" == *"?"* ]]; then
      hp="${host_port%%\?*}"
      query="${host_port#*\?}"
    else
      hp="$host_port"
      query=""
    fi
    hp="${hp%/}"                    
    outbound_server="${hp%:*}"
    outbound_port="${hp#*:}"
    outbound_type="hysteria2"
    outbound_auth="$auth"
    
    if [ -n "$query" ]; then
      obfs=$(echo "$query" | grep -o 'obfs=[^&]*' | cut -d= -f2)
      [ -n "$obfs" ] && outbound_obfs_password="$obfs"
      sni=$(echo "$query" | grep -o 'sni=[^&]*' | cut -d= -f2)
      [ -n "$sni" ] && outbound_sni="$sni"
      fp=$(echo "$query" | grep -o 'fp=[^&]*' | cut -d= -f2)
      [ -n "$fp" ] && outbound_fingerprint="$fp"
      ins=$(echo "$query" | grep -o 'insecure=[^&]*' | cut -d= -f2)
      [ "$ins" = "1" ] || [ "$ins" = "true" ] && outbound_insecure="true"
      alins=$(echo "$query" | grep -o 'allowInsecure=[^&]*' | cut -d= -f2)
      [ "$alins" = "1" ] || [ "$alins" = "true" ] && outbound_insecure="true"
    fi
    [ -z "$outbound_sni" ] && outbound_sni="$outbound_server"
    ;;

  tuic)
    # 分离 uuid:password 部分（用 %3A 分隔）
    uuid_pass="${content%%@*}"
    rest="${content#*@}"
    # 替换 %3A 为 :
    uuid_pass_clean=$(echo "$uuid_pass" | sed 's/%3A/:/g')
    if [[ "$uuid_pass_clean" == *":"* ]]; then
      outbound_uuid="${uuid_pass_clean%:*}"
      outbound_password2="${uuid_pass_clean#*:}"
    else
      outbound_uuid="$uuid_pass_clean"
      outbound_password2=""
    fi
    # 解析 host:port 和 query
    if [[ "$rest" == *"?"* ]]; then
      host_port="${rest%%\?*}"
      query="${rest#*\?}"
    else
      host_port="$rest"
      query=""
    fi
    outbound_server="${host_port%:*}"
    outbound_port="${host_port#*:}"
    outbound_type="tuic"
    # 解析参数
    if [ -n "$query" ]; then
      sni=$(echo "$query" | grep -o 'sni=[^&]*' | cut -d= -f2)
      [ -n "$sni" ] && outbound_sni="$sni"
      fp=$(echo "$query" | grep -o 'fp=[^&]*' | cut -d= -f2)
      [ -n "$fp" ] && outbound_fingerprint="$fp"
      ins=$(echo "$query" | grep -o 'insecure=[^&]*' | cut -d= -f2)
      [ "$ins" = "1" ] || [ "$ins" = "true" ] && outbound_insecure="true"
      alins=$(echo "$query" | grep -o 'allowInsecure=[^&]*' | cut -d= -f2)
      [ "$alins" = "1" ] || [ "$alins" = "true" ] && outbound_insecure="true"
      cc=$(echo "$query" | grep -o 'congestion_control=[^&]*' | cut -d= -f2)
      [ -n "$cc" ] && outbound_congestion="$cc"
      alpn=$(echo "$query" | grep -o 'alpn=[^&]*' | cut -d= -f2)
      [ -n "$alpn" ] && outbound_alpn="$alpn"
    fi
    [ -z "$outbound_sni" ] && outbound_sni="$outbound_server"
    ;;

  anytls)
    password="${content%%@*}"
    rest="${content#*@}"
    if [[ "$rest" == *"?"* ]]; then
      host_port="${rest%%\?*}"
      query="${rest#*\?}"
    else
      host_port="$rest"
      query=""
    fi
    outbound_server="${host_port%:*}"
    outbound_port="${host_port#*:}"
    outbound_password="$password"
    outbound_type="anytls"
    if [ -n "$query" ]; then
      sni=$(echo "$query" | grep -o 'sni=[^&]*' | cut -d= -f2)
      [ -n "$sni" ] && outbound_sni="$sni"
      fp=$(echo "$query" | grep -o 'fp=[^&]*' | cut -d= -f2)
      [ -n "$fp" ] && outbound_fingerprint="$fp"
      ins=$(echo "$query" | grep -o 'insecure=[^&]*' | cut -d= -f2)
      [ "$ins" = "1" ] || [ "$ins" = "true" ] && outbound_insecure="true"
      alins=$(echo "$query" | grep -o 'allowInsecure=[^&]*' | cut -d= -f2)
      [ "$alins" = "1" ] || [ "$alins" = "true" ] && outbound_insecure="true"
    fi
    [ -z "$outbound_sni" ] && outbound_sni="$outbound_server"
    ;;

  ss)
    # SIP002: ss://base64(method:password)@host:port#tag（晴天 patch：支援 shadowsocks 家寬節點）
    userinfo_host="${content%%@*}"
    rest="${content#*@}"
    if [[ "$rest" == *"?"* ]]; then
      host_port="${rest%%\?*}"
      query="${rest#*\?}"
    else
      host_port="$rest"
      query=""
    fi
    host_port="${host_port%%#*}"
    decoded=$(echo "$userinfo_host" | base64 -d 2>/dev/null || true)
    if [[ "$decoded" == *":"* ]]; then
      outbound_username="${decoded%%:*}"
      outbound_password2="${decoded#*:}"
    else
      # 舊格式：整條 link base64
      full_decoded=$(echo "$content" | base64 -d 2>/dev/null || true)
      meth_pass="${full_decoded%%@*}"
      outbound_username="${meth_pass%%:*}"
      outbound_password2="${meth_pass#*:}"
    fi
    outbound_server="${host_port%:*}"
    outbound_port="${host_port#*:}"
    outbound_type="shadowsocks"
    ;;

  socks5|socks)
    if [[ "$content" == *"@"* ]]; then
      user_pass="${content%%@*}"
      host_port="${content#*@}"
      decoded=$(echo "$user_pass" | base64 -d 2>/dev/null || true)
      if [ -n "$decoded" ] && [[ "$decoded" == *":"* ]]; then
        outbound_username="${decoded%:*}"
        outbound_password2="${decoded#*:}"
      else
        if [[ "$user_pass" == *":"* ]]; then
          outbound_username="${user_pass%:*}"
          outbound_password2="${user_pass#*:}"
        else
          outbound_username="$user_pass"
          outbound_password2=""
        fi
      fi
    else
      host_port="$content"
    fi
    outbound_server="${host_port%:*}"
    outbound_port="${host_port#*:}"
    outbound_type="socks"
    ;;


  *)
    echo "[ERROR] 不支持的协议: $proto"
    exit 1
    ;;
esac

if [ -z "$outbound_server" ] || [ -z "$outbound_port" ]; then
  echo "[ERROR] 无法解析服务器地址或端口"
  exit 1
fi

# 构建 outbound 对象
jq_outbound="{\"type\":\"$outbound_type\",\"tag\":\"proxy\",\"server\":\"$outbound_server\",\"server_port\":$outbound_port"

case "$outbound_type" in
  vless)
    jq_outbound="$jq_outbound,\"uuid\":\"$outbound_uuid\""
    [ -n "$outbound_flow" ] && jq_outbound="$jq_outbound,\"flow\":\"$outbound_flow\""
    if [ "$outbound_transport_type" != "tcp" ]; then
      jq_outbound="$jq_outbound,\"transport\":{\"type\":\"$outbound_transport_type\",\"path\":\"$outbound_path\",\"headers\":{\"Host\":\"$outbound_host\"}}"
    fi
    tls_enabled="false"
    [ "$outbound_security" = "tls" ] || [ "$outbound_security" = "reality" ] && tls_enabled="true"
    tls_json="{\"enabled\":$tls_enabled,\"server_name\":\"$outbound_sni\",\"insecure\":$outbound_insecure,\"utls\":{\"enabled\":true,\"fingerprint\":\"$outbound_fingerprint\"}"
    if [ -n "$outbound_alpn" ]; then
      tls_json="$tls_json,\"alpn\":[\"$outbound_alpn\"]"
    fi
    [ "$outbound_security" = "reality" ] && tls_json="$tls_json,\"reality\":{\"enabled\":true,\"public_key\":\"$outbound_reality_pbk\",\"short_id\":\"$outbound_reality_sid\"}"
    tls_json="$tls_json}"
    jq_outbound="$jq_outbound,\"tls\":$tls_json"
    ;;
  vmess)
    jq_outbound="$jq_outbound,\"uuid\":\"$outbound_uuid\",\"security\":\"auto\""
    jq_outbound="$jq_outbound,\"transport\":{\"type\":\"$outbound_transport_type\",\"path\":\"$outbound_path\",\"headers\":{\"Host\":\"$outbound_host\"}}"
    tls_enabled="false"
    [ "$outbound_security" = "tls" ] && tls_enabled="true"
    jq_outbound="$jq_outbound,\"tls\":{\"enabled\":$tls_enabled,\"server_name\":\"$outbound_sni\",\"insecure\":$outbound_insecure,\"utls\":{\"enabled\":true,\"fingerprint\":\"$outbound_fingerprint\"}}"
    ;;
  trojan)
    jq_outbound="$jq_outbound,\"password\":\"$outbound_password\""
    # 晴天 patch 2026-10-08：原本無條件加 transport，但 URI 寫 type=tcp 時會生成
    # "transport":{"type":"tcp"}，sing-box 直接 FATAL（unknown transport type: tcp）
    # —— 同 vless 分支一樣要判 tcp。實證：run 37806402371（trojan SG 556）。
    if [ "$outbound_transport_type" != "tcp" ]; then
      jq_outbound="$jq_outbound,\"transport\":{\"type\":\"$outbound_transport_type\",\"path\":\"$outbound_path\",\"headers\":{\"Host\":\"$outbound_host\"}}"
    fi
    trojan_tls="{\"enabled\":true,\"server_name\":\"$outbound_sni\",\"insecure\":$outbound_insecure,\"utls\":{\"enabled\":true,\"fingerprint\":\"$outbound_fingerprint\"}"
    if [ -n "$outbound_alpn" ]; then
      trojan_tls="$trojan_tls,\"alpn\":[\"$outbound_alpn\"]"
    fi
    trojan_tls="$trojan_tls}"
    jq_outbound="$jq_outbound,\"tls\":$trojan_tls"
    ;;
  hysteria2)
    jq_outbound="$jq_outbound,\"up_mbps\":$outbound_up_mbps,\"down_mbps\":$outbound_down_mbps"
    [ -n "$outbound_obfs_password" ] && jq_outbound="$jq_outbound,\"obfs\":{\"type\":\"salamander\",\"password\":\"$outbound_obfs_password\"}"
    [ -n "$outbound_auth" ] && jq_outbound="$jq_outbound,\"password\":\"$outbound_auth\""
    jq_outbound="$jq_outbound,\"tls\":{\"enabled\":true,\"server_name\":\"$outbound_sni\",\"insecure\":$outbound_insecure}"
    ;;
  tuic)
    jq_outbound="$jq_outbound,\"uuid\":\"$outbound_uuid\""
    [ -n "$outbound_password2" ] && jq_outbound="$jq_outbound,\"password\":\"$outbound_password2\""
    jq_outbound="$jq_outbound,\"congestion_control\":\"$outbound_congestion\",\"udp_over_stream\":$outbound_udp_over_stream,\"zero_rtt_handshake\":$outbound_zerortt"
    tls_json="{\"enabled\":true,\"server_name\":\"$outbound_sni\",\"insecure\":$outbound_insecure"
    if [ -n "$outbound_alpn" ]; then
      tls_json="$tls_json,\"alpn\":[\"$outbound_alpn\"]"
    fi
    tls_json="$tls_json}"
    jq_outbound="$jq_outbound,\"tls\":$tls_json"
    ;;
  anytls)
    jq_outbound="$jq_outbound,\"password\":\"$outbound_password\""
    jq_outbound="$jq_outbound,\"tls\":{\"enabled\":true,\"server_name\":\"$outbound_sni\",\"insecure\":$outbound_insecure,\"utls\":{\"enabled\":true,\"fingerprint\":\"$outbound_fingerprint\"}}"
    ;;
  shadowsocks)
    jq_outbound="$jq_outbound,\"method\":\"$outbound_username\",\"password\":\"$outbound_password2\""
    ;;

  socks)
    [ -n "$outbound_username" ] && jq_outbound="$jq_outbound,\"username\":\"$outbound_username\""
    [ -n "$outbound_password2" ] && jq_outbound="$jq_outbound,\"password\":\"$outbound_password2\""
    jq_outbound="$jq_outbound,\"version\":\"$outbound_version\""
    ;;
esac
jq_outbound="$jq_outbound}"

# 生成配置（无 udp 字段）
cat << EOF > sing-box-config.json
{
  "log": {"level": "warn"},
  "inbounds": [
    {"type": "socks", "tag": "socks-in", "listen": "127.0.0.1", "listen_port": 1080},
    {"type": "http", "tag": "http-in", "listen": "127.0.0.1", "listen_port": 1081}
  ],
  "outbounds": [$jq_outbound]
}
EOF

# echo "[DEBUG] 生成的 sing-box 配置:"
# cat sing-box-config.json

if ! jq empty sing-box-config.json 2>/dev/null; then
  echo "[ERROR] 生成的 sing-box 配置无效"
  # cat sing-box-config.json
  exit 1
fi

echo "[INFO] ✅ sing-box 配置已生成"

# 清理旧进程
echo "[INFO] 清理旧进程..."
pkill -f sing-box 2>/dev/null || true
fuser -k 1080/tcp 2>/dev/null || true
sleep 2

./sing-box run -c sing-box-config.json > sing-box.log 2>&1 &
sleep 5

if ! pgrep -f sing-box > /dev/null; then
  echo "[ERROR] sing-box 进程启动失败，查看日志:"
  cat sing-box.log
  exit 1
fi

echo "[INFO] 测试代理连接..."
for i in {1..3}; do
  if curl -x socks5://127.0.0.1:1080 -s --max-time 15 https://api.ipify.org > /dev/null 2>&1; then
    echo "[INFO] ✅ 代理连接成功"
    echo "IS_PROXY=true" >> $GITHUB_ENV
    echo "PROXY_SERVER=socks5://127.0.0.1:1080" >> $GITHUB_ENV
    exit 0
  fi
  echo "[WARN] 尝试 $i/3..."
  sleep 3
done

echo "[ERROR] ❌ 代理连接失败"
echo "---- sing-box 日志 ----"
cat sing-box.log
exit 1
