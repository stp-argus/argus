# 업그레이드

## 0.1.x → 0.2.0

| 변경 | 해야 할 일 |
| --- | --- |
| CRD 가 `templates/` → `crds/` 로 이동, `crd.install` 값 삭제 | 새 설치는 Helm 이 CRD 를 먼저 넣는다. **Helm 은 `crds/` 를 업그레이드하지 않는다** — CRD 가 바뀐 릴리스에서는 `kubectl apply -f charts/argus/crds/` (ArgoCD 는 동기화 때 적용한다). CRD 설치를 건너뛰려면 `--skip-crds` |
| 오퍼레이터 Deployment selector 에 `app.kubernetes.io/component: operator` 추가 | selector 는 불변이다. 업그레이드 **전에** `kubectl -n argus-system delete deploy argus` (그동안 가드레일이 로컬 폴백으로 판정을 이어 가고, 카운터는 새로 시작한다). ArgoCD 는 삭제 후 동기화 |
| `storageClassName` 기본값 `local-path` → `""`(클러스터 기본) | **반드시** 기존 값을 values 에 명시: `litellm.database.persistence.storageClassName`, `litellm.records.storageClassName`. 빠뜨리면 Helm 4 서버 사이드 적용이 그 필드를 지우려다 `PersistentVolumeClaim ... spec is immutable` 로 업그레이드가 실패한다(kind 실측) |
| 차트 값 `litellm.guardrail.failMode`(`ARGUS_FAIL_MODE`) 삭제 — 아무 코드도 읽지 않던 값 | fail-closed 는 정책 `policy.backstop.failureMode: Closed` 로 (Enforce 에서 오퍼레이터가 `ARGUS_FAIL_CLOSED_GRACE_S`, 기본 60초 넘게 안 닿으면 차단) |
| 최상위 `controlLevel`(오퍼레이터 env `ARGUS_CONTROL_LEVEL`) 삭제 — 오퍼레이터가 읽지 않던 값 | 차단 수준은 `policy.controlLevel` 하나 |
| `values.schema.json` 추가 — 모르는 키·잘못된 값은 설치 전에 거절 | 거절되면 메시지의 경로를 고친다 (예: `policy.detect.duplicateCall` → `duplicateCalls`) |
| 차단 상태코드가 차트 값 `litellm.guardrail.blockStatus`(기본 403)로 | `extraEnv.ARGUS_BLOCK_STATUS` 로 넣던 것은 지운다 |
| `mockUpstream`·`generateSecrets` 추가 (기본 off) | 없음 |
