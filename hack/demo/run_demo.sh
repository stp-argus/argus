#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
#
# ARGUS 데모 — 샘플앱이 실제로 돌고, ARGUS 가 그걸 어떻게 보는지 보여준다.
#
#   터미널 1:  python3 hack/demo/live_view.py docs/research/litellm-lab/records/records.jsonl
#   터미널 2:  bash hack/demo/run_demo.sh
#
# 3막 구성이다. 각 막 사이에 멈춰서 화면을 볼 시간을 준다.
#
#   1막 · 정상 에이전트    → 아무 일도 안 일어난다 (이게 요점이다)
#   2막 · 재시도 폭풍      → 동일 호출 반복 축이 잡는다
#   3막 · 핑퐁 재귀        → run 스텝수는 눈이 멀고 principal 축이 잡는다  ⭐
#
# 3막이 이 제품의 논지다. per-run 상한만 보는 도구는 3막에서 전부 실패한다.
set -u
ARGUS=${ARGUS:-$(cd "$(dirname "$0")/../.." && pwd)}
APP=${APP:-$ARGUS/../argus-sample-app}   # 형제 디렉터리
MODEL=${LLM_MODEL:-argus-real-gpt}      # 실비용 측정이 필요없으면 mock 별칭으로 바꾼다

# 탭 주소·키. 랩(docker compose)과 kind 를 같은 스크립트로 돌리기 위해 env 로 뺐다.
#   랩  : ARGUS_BASE=http://127.0.0.1:4000  ARGUS_KEY=sk-argus-lab-1234
#   kind: ARGUS_BASE=http://127.0.0.1:4400  ARGUS_KEY=sk-argus-kind
ARGUS_BASE=${ARGUS_BASE:-http://127.0.0.1:4000}
ARGUS_KEY=${ARGUS_KEY:-sk-argus-lab-1234}

# 판독할 레코드 파일. 탭이 랩이 아닌데 랩 파일을 읽으면 **지난 실행의 결과를 이번 결과로
# 보고한다** — kind 관통 때 실제로 겪었다. 그래서 ① 기본값을 탭 주소에서 유도하고
# ② 아래 판독 단계에서 T0 이후 레코드가 실제로 늘었는지 확인한다.
#   kind : RECORDS=<포워딩한 파드의 파일을 복사한 경로>
if [ -n "${RECORDS:-}" ]; then :
elif [ "$ARGUS_BASE" = "http://127.0.0.1:4000" ]; then
  RECORDS=$ARGUS/docs/research/litellm-lab/records/records.jsonl
else
  echo "!! ARGUS_BASE 가 랩(:4000)이 아닌데 RECORDS 가 없다." >&2
  echo "   랩 파일을 읽으면 지난 실행 결과를 이번 결과로 보고하게 된다 — RECORDS 를 지정하라." >&2
  exit 2
fi
DEMO_T0=$(python3 -c 'import time; print(time.time())')

export OPENAI_BASE_URL="$ARGUS_BASE"
export ANTHROPIC_BASE_URL="$ARGUS_BASE"
export GEMINI_BASE_URL="$ARGUS_BASE"
export LITELLM_BASE="$ARGUS_BASE"
export LLM_API_KEY="$ARGUS_KEY" LITELLM_MASTER_KEY="$ARGUS_KEY"
export LLM_MODE=real LLM_WIRE=openai LLM_MODEL="$MODEL"
export MAX_CALLS=${MAX_CALLS:-40} MAX_COST_USD=${MAX_COST_USD:-0.5}

# 발표할 때는 막 사이에 멈춰 화면을 보여준다. 자동 검증은 DEMO_NONINTERACTIVE=1.
pause() {
  echo
  if [ "${DEMO_NONINTERACTIVE:-0}" = "1" ]; then echo "  (비대화 모드 — 계속)"; sleep 2; return; fi
  read -r -p "  ▶ 엔터를 누르면 다음 막으로... " _
  echo
}
act()   { echo; echo "════════════════════════════════════════════════"; echo "  $1"; echo "════════════════════════════════════════════════"; }

act "1막 · 정상 에이전트 — 아무 일도 안 일어나야 한다"
echo "  여러 principal 이 각자, run 당 3~5스텝에 작업을 끝낸다."
echo "  도구 인자가 매번 다르고 결과는 성공이다."
python3 "$ARGUS/hack/demo/normal_agent.py"
echo
echo "  → 화면에 차단이 0 이어야 한다. 정상을 안 막는 것이 가장 어렵다."
pause

act "2막 · 재시도 폭풍 — 같은 도구를 같은 인자로 반복한다"
echo "  대화는 매 스텝 자란다. 그래서 전체 프롬프트 해시는 매번 다르다."
echo "  하지만 (도구, 인자) 지문은 고정이다 — 그걸 본다."
cd "$APP" && SCENARIO=retry-storm PRINCIPAL=demo-retry HARD_STOP=40 RATE_MS=250 \
  npx tsx src/runner/index.ts 2>&1 | tail -4
echo
echo "  → '동일 호출 반복' 축이 차오르다가 hard 임계에서 걸린다."
pause

act "3막 · 핑퐁 재귀 — 여기가 논지다"
echo "  runId 를 5콜마다 회전시킨다. per-run 카운터는 매번 리셋된다."
echo "  화면에서 두 축을 나란히 보라:"
echo "    · run 스텝수     → 5 를 안 넘는다. 정상과 구별되지 않는다"
echo "    · principal 누적 → 계속 올라간다. 여기서 걸린다"
cd "$APP" && SCENARIO=pingpong-recursion PRINCIPAL=demo-pingpong HARD_STOP=40 \
  ROTATION_EVERY=5 AGENTS=2 RATE_MS=250 npx tsx src/runner/index.ts 2>&1 | tail -4
echo
echo "  → per-run 상한만 보는 도구는 이 폭주를 영원히 못 잡는다."
pause

# ── 4막 — 판정 중앙화(8/17) 덕에 처음 가능해진 장면이다.
# 전에는 CR 패치→반영이 kubelet ~85초라 "잠깐 기다리시죠"가 됐다.
# 지금은 컨트롤러가 렌더 즉시 오퍼레이터 엔진에 반영한다 → 실측 3초.
act "4막 · 선언이 판정을 구동한다 — 폭주 도중에 Enforce 를 켠다"
if [ -z "${DEMO_KCTX:-}" ]; then
  echo "  (DEMO_KCTX 미설정 — 랩 모드라 4막은 건너뛴다. kind/dev 에서: DEMO_KCTX=kind-argus-local)"
else
  echo "  지금 이 순간에도 2·3막의 폭주 코호트는 쿨다운·윈도우 안에 있다."
  echo "  차단을 켜는 것은 재배포가 아니라 **선언 한 줄**이다:"
  echo
  echo "    $ kubectl patch runawaypolicy observe-default --type merge \\"
  echo "        -p '{\"spec\":{\"controlLevel\":\"Enforce\"}}'"
  echo
  kubectl --context "$DEMO_KCTX" -n argus-system patch runawaypolicy observe-default \
    --type merge -p '{"spec":{"controlLevel":"Enforce"}}' >/dev/null
  sleep 3   # 컨트롤러 reconcile — kubelet 85초가 아니다
  LEVEL=$(kubectl --context "$DEMO_KCTX" -n argus-system exec deploy/litellm -- python3 -c \
    'import json,urllib.request;print(json.loads(urllib.request.urlopen("http://argus.argus-system.svc:8080/v1/policy",timeout=5).read())["control_level"])' 2>/dev/null)
  echo "  3초 뒤 오퍼레이터 엔진: controlLevel=$LEVEL"
  echo "  같은 폭주를 다시 보낸다 — 이번엔 429 가 와야 한다:"
  cd "$APP" && SCENARIO=retry-storm PRINCIPAL=demo-retry HARD_STOP=8 RATE_MS=250 \
    npx tsx src/runner/index.ts 2>&1 | tail -3
  echo
  echo "  → 오퍼레이터 로그(터미널 2)에 [argus] 차단 ... id=d-... 가 찍혔다."
  echo "  → 러너가 'quota-limited' 로 표기하는 것은 알려진 계약 구멍이다"
  echo "     (LiteLLM 이 decision_id 를 자기 오류 봉투 안에 넣는다 — block-contract.md)."
  # 데모가 끝나면 반드시 원복 — 손으로 올린 것은 손으로 내린다 (SSA 소유권)
  kubectl --context "$DEMO_KCTX" -n argus-system patch runawaypolicy observe-default \
    --type merge -p '{"spec":{"controlLevel":"Observe"}}' >/dev/null
  echo "  (원복: controlLevel=Observe)"
fi
echo

act "판독"
# 신선도 확인 먼저. 이 막을 건너뛰면 엉뚱한 파일을 읽고도 표가 예쁘게 나온다.
FRESH=$(python3 - "$RECORDS" "$DEMO_T0" <<'PY'
import datetime as dt, json, sys
path, t0 = sys.argv[1], float(sys.argv[2])
n = 0
try:
    with open(path) as f:
        for line in f:
            try:
                # ts 는 ISO-8601 UTC 문자열이다 ("...Z"). epoch 가 아니다.
                ts = json.loads(line)["ts"].replace("Z", "+00:00")
                if dt.datetime.fromisoformat(ts).timestamp() >= t0:
                    n += 1
            except Exception:
                pass
except OSError as e:
    print(f"ERR {e}")
    raise SystemExit(0)
print(n)
PY
)
case "$FRESH" in
  ERR*)  echo "!! 레코드 파일을 못 읽었다: $RECORDS ($FRESH)"; exit 2 ;;
  0)     echo "!! $RECORDS 에 이번 실행($DEMO_T0 이후) 레코드가 0건이다."
         echo "   탭이 다른 파일에 쓰고 있거나 가드레일이 발화하지 않았다. 판독을 멈춘다."
         exit 2 ;;
  *)     echo "  이번 실행 레코드 ${FRESH}건 — $RECORDS" ;;
esac
python3 "$ARGUS/hack/analyze/verdict_report.py" "$RECORDS"
