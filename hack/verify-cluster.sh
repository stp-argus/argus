#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
#
# 배포된 클러스터에서 **선언이 판정을 구동하는가**를 끝까지 확인한다.
#
#   bash hack/verify-cluster.sh                    # 현재 컨텍스트
#   KCTX=stp-argus-dev bash hack/verify-cluster.sh # 컨텍스트 지정
#
# 왜 필요한가 — 파드가 `Running` 인 것은 아무것도 증명하지 않는다. 이 프로젝트에서
# 실제로 겪은 실패는 전부 "떠 있는데 안 도는" 형태였다:
#   · 컨트롤러가 빈 프로세스였다 (CR 의 status 가 비어 있는 것이 유일한 증거였다)
#   · 가드레일이 등록됐는데 훅이 한 번도 발화하지 않았다 (guardrail_id: null)
#   · 레코드가 전부 버려지는데 로그 한 줄 없었다 (경로가 디렉터리였다)
#   · 설정 이름이 어긋나 축이 조용히 꺼진 채 돌았다
#
# 그래서 각 단계는 "존재"가 아니라 **동작의 흔적**을 본다.
set -u

NS=${NS:-argus-system}
POLICY=${POLICY:-observe-default}
K="kubectl${KCTX:+ --context $KCTX} -n $NS"
TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT

pass=0; fail=0; warn=0
ok()   { echo "  ✅ $1"; pass=$((pass+1)); }
no()   { echo "  ❌ $1"; fail=$((fail+1)); }
hm()   { echo "  ⚠️  $1"; warn=$((warn+1)); }
step() { echo; echo "── $1 ──"; }

echo "컨텍스트: ${KCTX:-$(kubectl config current-context)}  네임스페이스: $NS"

step "1. 파드"
$K get pods 2>/dev/null | sed 's/^/  /'

step "2. 컨트롤러가 일하는가 (CR 의 status — 여기가 비면 나머지는 볼 필요 없다)"
$K get runawaypolicy "$POLICY" -o json > "$TMP/cr.json" 2>/dev/null
if [ ! -s "$TMP/cr.json" ]; then
  no "RunawayPolicy/$POLICY 가 없다"
else
  python3 - "$TMP/cr.json" > "$TMP/cr.out" <<'PY'
import json, sys
st = json.load(open(sys.argv[1])).get("status", {})
conds = {c["type"]: c["status"] for c in st.get("conditions", [])}
axes = st.get("activeAxes", [])
print("conditions: " + (" ".join(f"{k}={v}" for k, v in conds.items()) or "(없음)"))
print("activeAxes: " + (",".join(axes) or "(없음)"))
if conds.get("Accepted") == "True" and conds.get("Programmed") == "True":
    print("OK 컨트롤러가 CR 을 받아 렌더했다")
elif not conds:
    print("NO status 가 비어 있다 — 컨트롤러가 안 돈다. RBAC(configmaps) 부터 의심하라")
else:
    print("NO 조건이 충족되지 않았다")
if not axes:
    print("NO 활성 축이 하나도 없다 — 임계값을 선언하지 않았다")
PY
  while IFS= read -r line; do
    case "$line" in
      OK\ *) ok "${line#OK }" ;;
      NO\ *) no "${line#NO }" ;;
      *)     echo "  $line" ;;
    esac
  done < "$TMP/cr.out"
fi

step "3. 렌더된 정책"
$K get cm argus-policy -o jsonpath='{.data.policy\.json}' > "$TMP/policy.json" 2>/dev/null
if [ ! -s "$TMP/policy.json" ]; then
  no "ConfigMap argus-policy 가 없다"
else
  python3 -m json.tool < "$TMP/policy.json" | sed 's/^/  /'
  ok "정책이 렌더돼 있다"
fi

step "4. 탭 컨테이너 안의 정책 (kubelet 동기화까지 최대 ~90초 걸린다)"
POD=$($K get pod -l app.kubernetes.io/component=litellm -o name 2>/dev/null | head -1); POD=${POD#pod/}
if [ -z "$POD" ]; then
  no "LiteLLM 파드를 찾지 못했다"
else
  INPOD=$($K exec "$POD" -- python3 -c \
    'import json;print(json.load(open("/etc/argus/policy/policy.json"))["control_level"])' 2>/dev/null)
  if [ -z "$INPOD" ]; then
    hm "컨테이너 안에 정책 파일이 없다 — env 폴백으로 돌고 있다 (그 자체는 정상 동작)"
  else
    ok "컨테이너 안 controlLevel = $INPOD"
  fi
fi

step "5. 가드레일이 실제로 등록됐는가 (guardrail_id 가 null 이면 미발화)"
if [ -n "${POD:-}" ]; then
  # curl 이 아니라 python 으로 부른다 — LiteLLM 이미지에 curl 이 없다(있다고 가정하면
  # "목록을 못 읽었다"가 나오는데, 그건 가드레일 문제로 오독되기 딱 좋다).
  $K exec "$POD" -- python3 -c \
    'import os,urllib.request;print(urllib.request.urlopen(urllib.request.Request("http://127.0.0.1:4000/guardrails/list",headers={"Authorization":"Bearer "+os.environ["LITELLM_MASTER_KEY"]}),timeout=5).read().decode())' \
    > "$TMP/gr.json" 2>/dev/null
  GID=$(python3 - "$TMP/gr.json" <<'PY'
import json, sys
try:
    gs = json.load(open(sys.argv[1])).get("guardrails", [])
except Exception:
    raise SystemExit
print(",".join(f"{g['guardrail_name']}:{g['guardrail_id'] or 'NULL'}" for g in gs))
PY
)
  case "$GID" in
    "")      hm "가드레일 목록을 못 읽었다" ;;
    *NULL*)  no "guardrail_id 가 null 이다 — 훅이 콜백 체인에 안 붙었다 (LiteLLM 버전 확인)" ;;
    *)       ok "가드레일 등록됨: $GID" ;;
  esac
fi

step "6. 레코드가 쌓이고 있는가 + 정책이 탭까지 도달했는가"
if [ -n "${POD:-}" ]; then
  N=$($K exec "$POD" -- sh -c 'wc -l < /var/argus/records/records.jsonl' 2>/dev/null | tr -d ' ')
  if [ -z "$N" ]; then
    no "레코드 파일이 없다 — ARGUS_RECORDS_PATH 가 디렉터리를 가리키면 조용히 전부 유실된다"
  else
    ok "레코드 ${N}줄"
    $K exec "$POD" -- sh -c 'tail -1 /var/argus/records/records.jsonl' > "$TMP/last.json" 2>/dev/null
    python3 - "$TMP/last.json" > "$TMP/last.out" 2>/dev/null <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))
src = d.get("policy_source")
print(f"최근 레코드: level={d.get('control_level')} policy_source={src}")
print(f"  관측치: {d.get('observations')}")
print("OK 선언한 정책이 판정까지 도달했다" if (src or "").startswith("crd:")
      else "HM policy_source 가 crd: 가 아니다 — CR 이 아니라 폴백 기본값으로 돌고 있다")
PY
    if [ -s "$TMP/last.out" ]; then
      while IFS= read -r line; do
        case "$line" in
          OK\ *) ok "${line#OK }" ;;
          HM\ *) hm "${line#HM }" ;;
          *)     echo "  $line" ;;
        esac
      done < "$TMP/last.out"
    else
      hm "최근 레코드를 파싱하지 못했다"
    fi
  fi
fi

step "결과"
echo "  통과 $pass · 경고 $warn · 실패 $fail"
[ "$fail" -gt 0 ] && exit 1
exit 0
