#!/usr/bin/env bash
set -u

unset HTTP_PROXY HTTPS_PROXY ALL_PROXY NO_PROXY
unset http_proxy https_proxy all_proxy no_proxy

failed=0

probe() {
  label=$1
  proxy_flag=$2
  proxy_value=$3
  url=$4
  expected=$5
  code=$(curl --disable -4 --noproxy '' "$proxy_flag" "$proxy_value" -sS -o /dev/null \
    --connect-timeout 5 --max-time 12 -w '%{http_code}' "$url" 2>/dev/null)
  curl_status=$?
  # 2026-10-17: one retry after 3s so a single transient timeout is not a failure.
  if [[ $curl_status -ne 0 || "$code" != "$expected" ]]; then
    printf '%s first_attempt code=%s curl_status=%s retrying\n' "$label" "${code:-000}" "$curl_status"
    sleep 3
    code=$(curl --disable -4 --noproxy '' "$proxy_flag" "$proxy_value" -sS -o /dev/null \
      --connect-timeout 5 --max-time 12 -w '%{http_code}' "$url" 2>/dev/null)
    curl_status=$?
  fi
  if [[ $curl_status -ne 0 || "$code" != "$expected" ]]; then
    failed=1
  fi
  printf '%s code=%s expected=%s curl_status=%s\n' \
    "$label" "${code:-000}" "$expected" "$curl_status"
}

probe telegram_http -x http://127.0.0.1:7890 \
  https://api.telegram.org/ 302
probe telegram_socks --socks5-hostname 127.0.0.1:7891 \
  https://api.telegram.org/ 302
probe gstatic_http -x http://127.0.0.1:7890 \
  https://www.gstatic.com/generate_204 204
probe gstatic_socks --socks5-hostname 127.0.0.1:7891 \
  https://www.gstatic.com/generate_204 204

if [[ $failed -eq 0 ]]; then
  echo 'mihomo_egress_status=ok'
  exit 0
fi

echo 'mihomo_egress_status=failed'
exit 1
