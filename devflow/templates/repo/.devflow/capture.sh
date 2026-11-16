#!/usr/bin/env bash
# devflow 診斷採集（唯讀：不 exec、不重啟、不改狀態）：compose 專案的容器清單、退出狀態、
# 限時 log、健康探測與主機資源，遮罩秘密後寫到 $DEVFLOW_DIAGNOSTICS/<label>/（預設 .devflow/diagnostics）。
# deploy.sh 在回滾前呼叫；CI verify 與 repro workflow 失敗時也呼叫。採集出錯不影響呼叫端（一律 exit 0）。
# 用法：capture.sh <compose 專案名> <label> [since]
#   since：docker logs --since 的值（RFC3339 時間或 30m 這類相對時間）；省略 = 容器全部 log
# 不輸出 docker inspect 的 Config.Env（含部署秘密），只取 State / 映像 / 重啟次數。
set -uo pipefail
cd "$(dirname "$0")/.." || exit 0
project=${1:-} label=${2:-} since=${3:-}
if [[ ! $project =~ ^[a-z0-9][a-z0-9_.-]*$ || ! $label =~ ^[A-Za-z0-9._-]+$ || ! $since =~ ^[0-9A-Za-z:.+-]*$ ]]; then
  echo "capture: usage: capture.sh <compose project> <label> [since]" >&2
  exit 0
fi
set -a; . .devflow/config.env; set +a
out="${DEVFLOW_DIAGNOSTICS:-.devflow/diagnostics}/$label"
rm -rf "$out"
mkdir -p "$out/logs" || exit 0
err="$out/errors.txt"
lines=${DEVFLOW_DIAG_LOG_LINES:-2000}
filter=(--filter "label=com.docker.compose.project=$project")

docker ps -a "${filter[@]}" --format '{{json .}}' >"$out/ps.jsonl" 2>>"$err"
mapfile -t ids < <(docker ps -aq "${filter[@]}" 2>>"$err")
fmt='{"name":{{json .Name}},"service":{{json (index .Config.Labels "com.docker.compose.service")}},"image":{{json .Config.Image}},"image_id":{{json .Image}},"restart_count":{{.RestartCount}},"state":{{json .State}}}'
for id in "${ids[@]}"; do
  docker inspect --format "$fmt" "$id" >>"$out/state.jsonl" 2>>"$err"
  svc=$(docker inspect --format '{{index .Config.Labels "com.docker.compose.service"}}' "$id" 2>/dev/null)
  [[ $svc =~ ^[A-Za-z0-9._-]+$ ]] || svc=container
  docker logs --timestamps ${since:+--since "$since"} "$id" 2>&1 | tail -n "$lines" >"$out/logs/$svc-${id:0:12}.log"
done

if [[ -n ${DEVFLOW_HEALTH_PATH:-} && -n ${DEVFLOW_PORT:-} ]]; then
  curl -sS --max-time 5 -o "$out/health-body.txt" \
    -w '{"http_code":"%{http_code}","time_total":%{time_total}}\n' \
    "http://127.0.0.1:${DEVFLOW_PORT}${DEVFLOW_HEALTH_PATH}" >"$out/health.json" 2>>"$err"
  if [[ -f $out/health-body.txt ]]; then
    head -c 4096 "$out/health-body.txt" >"$out/health-body.tmp" && mv "$out/health-body.tmp" "$out/health-body.txt"
  fi
fi

{ echo "# disk (repo filesystem)"; df -hP .; echo "# memory"; grep -E '^(MemTotal|MemAvailable|SwapFree):' /proc/meminfo; } \
  >"$out/host.txt" 2>>"$err"

sha=${GITHUB_SHA:-$(git rev-parse HEAD 2>/dev/null || true)}
[[ $sha =~ ^[0-9a-f]*$ ]] || sha=
printf '{"label":"%s","project":"%s","since":"%s","captured_at":"%s","git_sha":"%s","containers":%d}\n' \
  "$label" "$project" "$since" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$sha" "${#ids[@]}" >"$out/manifest.json"

if ! python3 .devflow/redact.py deploy/.env "$out" 2>/dev/null; then
  rm -rf "$out/logs" "$out/health-body.txt" "$err"
  echo "redaction failed: logs removed (fail closed)" >"$err"
fi
[[ -s $err ]] || rm -f "$err"
echo "capture: $label -> $out (${#ids[@]} containers)"
exit 0
