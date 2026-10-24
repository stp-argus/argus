#!/bin/bash
# SPDX-License-Identifier: Apache-2.0
#
# 신원 사다리 검증용 가상 키 6개 발급 — hack/demo/*.py 드라이버의 전제.
#
#   kubectl -n argus-system port-forward svc/litellm 4400:4000 &
#   eval "$(bash hack/demo/provision_keys.sh)"     # KEY_A/B/C1/C2/D/E export
#
# 발급 구조가 곧 시나리오다:
#   agent-a / agent-b   서로 다른 user_id — 갈라져야 하는 두 주체
#   agent-c 키 2개      **같은 user_id** — 키를 회전해도 principal 이 유지되는가 (핑퐁)
#   agent-d / agent-e   realistic_agents.py(data-analyst) · stuck_session.py(support-bot) 용
#   agent-f             k8s 러너 Job(pingpong-recursion) 전용 — 로컬 드라이버와 코호트를 섞지 않는다
# 전부 team-research — 팀 축(record.team)은 하나로 묶여야 한다.
#
# stdout 은 eval 용 export 문만 낸다. 사람 눈용 진행 메시지는 stderr.
set -euo pipefail
BASE=${ARGUS_BASE:-http://127.0.0.1:4400}
# alias 는 LiteLLM DB 에서 유일해야 한다 — 같은 DB 에 두 번 발급하면 400 (2026-09-07 겪음).
# 반복 실행(e2e)은 ALIAS_SUFFIX 로 구분한다. user_id 는 그대로라 principal 은 안 갈라진다.
SFX=${ALIAS_SUFFIX:-}
MASTER=${LITELLM_MASTER_KEY:?LITELLM_MASTER_KEY 필요 (kind: Secret litellm-master-key)}

gen() { # user_id alias
  curl -sf "$BASE/key/generate" \
    -H "Authorization: Bearer $MASTER" -H "Content-Type: application/json" \
    -d "{\"user_id\": \"$1\", \"key_alias\": \"$2\", \"team_id\": \"team-research\"}" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["key"])'
}

# 팀·유저가 없으면 키 발급이 거부될 수 있어 먼저 만든다 (이미 있으면 무시).
curl -s "$BASE/team/new" -H "Authorization: Bearer $MASTER" -H "Content-Type: application/json" \
  -d '{"team_id": "team-research"}' >/dev/null 2>&1 || true
for u in agent-a agent-b agent-c agent-d agent-e agent-f; do
  curl -s "$BASE/user/new" -H "Authorization: Bearer $MASTER" -H "Content-Type: application/json" \
    -d "{\"user_id\": \"$u\", \"team_id\": \"team-research\"}" >/dev/null 2>&1 || true
done

echo "가상 키 발급 중 ($BASE)..." >&2
echo "export KEY_A=$(gen agent-a agent-a-key$SFX)"
echo "export KEY_B=$(gen agent-b agent-b-key$SFX)"
echo "export KEY_C1=$(gen agent-c agent-c-key-1$SFX)"
echo "export KEY_C2=$(gen agent-c agent-c-key-2$SFX)"
echo "export KEY_D=$(gen agent-d agent-d-key$SFX)"
echo "export KEY_E=$(gen agent-e agent-e-key$SFX)"
echo "export KEY_F=$(gen agent-f agent-f-key$SFX)"
echo "완료 — 7개 (agent-a, agent-b, agent-c x2, agent-d, agent-e, agent-f)" >&2
