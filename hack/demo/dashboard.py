# SPDX-License-Identifier: Apache-2.0
"""로컬 탐지 대시보드 — records.jsonl 을 브라우저에서 실시간으로 본다.

    python3 hack/demo/dashboard.py            # http://localhost:8899
    KCTX=kind-argus-local python3 hack/demo/dashboard.py --port 8899

무엇이 아닌가: **007 대시보드가 아니다.** 그쪽은 제품이고 이건 live_view.py 의
브라우저판 — 로컬 검증 세션에서 "지금 무엇이 어떻게 탐지되고 있나"를 보는 관측 도구다.
그래서 hack/demo 에 있고, 의존성 0(stdlib), 파일 하나다.

배선:
  · 레코드   kubectl exec deploy/litellm -- tail -f records.jsonl   (파드 재시작 시 재접속)
  · 정책     오퍼레이터 /v1/policy 를 파드 안 python 으로 대신 조회 (30초마다)
  · 화면     /       한 페이지 (인라인 HTML/JS, 2초 폴링)
             /data   {policy, records[]} JSON
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

KCTX = os.environ.get("KCTX", "kind-argus-local")
NS = os.environ.get("ARGUS_NS", "argus-system")
RECORDS_PATH = "/var/argus/records/records.jsonl"
KEEP = 2000          # 브라우저로 보내는 최대 레코드 수 (오래된 것부터 버린다)

_records: collections.deque[dict] = collections.deque(maxlen=KEEP)
_policy: dict = {}
_status = {"tail": "connecting", "policy": "connecting"}
_lock = threading.Lock()

# 브라우저로 보낼 필드만 추린다 — 원본에는 프롬프트가 없지만(FR-003) 그래도 최소로.
_FIELDS = ("ts", "principal", "principal_source", "team", "run_id", "conversation_hash",
           "tool_name", "model", "would_block", "status", "policy_hit", "observations",
           "decide_source", "control_level", "policy_source", "decide_reason")


def _kubectl(*args: str) -> list[str]:
    return ["kubectl", "--context", KCTX, "-n", NS, *args]


def _tail_loop() -> None:
    """레코드 파일을 따라간다. exec 이 죽으면(파드 재시작 등) 3초 후 처음부터 다시 —
    maxlen deque 라 중복 유입은 처음 KEEP 건 이후 자연히 밀려난다."""
    while True:
        try:
            proc = subprocess.Popen(
                _kubectl("exec", "deploy/litellm", "--",
                         "tail", "-n", "+1", "-f", RECORDS_PATH),
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            with _lock:
                _records.clear()
                _status["tail"] = "ok"
            for line in proc.stdout:  # type: ignore[union-attr]
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                with _lock:
                    _records.append({k: r.get(k) for k in _FIELDS})
        except Exception:
            pass
        with _lock:
            _status["tail"] = "reconnecting"
        time.sleep(3)


def _policy_loop() -> None:
    """게이지의 soft/hard 임계 — 컨트롤러가 CR 에서 렌더한 policy.json 을 그대로 읽는다.
    (/v1/policy 는 요약만 주고 axes 가 없다. 가드레일이 판정에 쓰는 파일이 정본이다.)"""
    while True:
        try:
            out = subprocess.run(
                _kubectl("exec", "deploy/litellm", "--",
                         "cat", "/etc/argus/policy/policy.json"),
                capture_output=True, text=True, timeout=15).stdout
            with _lock:
                _policy.update(json.loads(out))
                _status["policy"] = "ok"
        except Exception:
            with _lock:
                _status["policy"] = "unreachable"
        time.sleep(30)


PAGE = r"""<!doctype html><html lang="ko"><head><meta charset="utf-8">
<title>ARGUS 로컬 탐지 뷰</title>
<style>
:root{--bg:#0f1420;--card:#1a2130;--line:#2a3347;--fg:#dbe2f0;--dim:#8b96ad;
      --ok:#3fb27f;--soft:#e2b93b;--hard:#e05252;--accent:#5b8def}
*{box-sizing:border-box;margin:0}
body{background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,'Apple SD Gothic Neo',sans-serif;padding:18px}
h1{font-size:17px;margin-bottom:2px} h1 small{color:var(--dim);font-weight:400;margin-left:8px}
#meta{color:var(--dim);font-size:12px;margin-bottom:14px}
#meta b{color:var(--fg);font-weight:600}
.pill{display:inline-block;border:1px solid var(--line);border-radius:20px;padding:1px 10px;margin-right:6px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:12px;margin-bottom:18px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}
.card.blocked{border-color:var(--hard)}
.card h2{font-size:13px;font-family:ui-monospace,monospace;display:flex;justify-content:space-between;align-items:center}
.tag{font-size:11px;border-radius:4px;padding:1px 6px;background:#26304a;color:var(--accent);font-family:ui-monospace,monospace}
.tag.spoof{background:#4a2626;color:var(--hard)}
.krow{color:var(--dim);font-size:12px;margin:6px 0 8px}
.krow b{color:var(--fg)}
.tools span{display:inline-block;background:#232c40;border-radius:4px;padding:1px 7px;margin:0 4px 4px 0;font-size:12px;font-family:ui-monospace,monospace}
.axis{display:grid;grid-template-columns:110px 1fr 84px;gap:8px;align-items:center;font-size:11px;margin-top:4px}
.axis .nm{color:var(--dim);font-family:ui-monospace,monospace}
.bar{height:7px;background:#232c40;border-radius:4px;overflow:hidden;position:relative}
.bar i{display:block;height:100%;border-radius:4px}
.axis .v{text-align:right;font-family:ui-monospace,monospace;color:var(--dim)}
table{width:100%;border-collapse:collapse;font-size:12px}
th{color:var(--dim);text-align:left;font-weight:500;padding:4px 8px;border-bottom:1px solid var(--line)}
td{padding:3px 8px;border-bottom:1px solid #1e2636;font-family:ui-monospace,monospace;white-space:nowrap}
td.reason{white-space:normal;font-family:inherit;color:var(--dim)}
.wb{color:var(--hard);font-weight:700}.pass{color:var(--ok)}
.lvl-soft{color:var(--soft)}.lvl-hard{color:var(--hard)}
#win button{background:#232c40;color:var(--dim);border:1px solid var(--line);border-radius:6px;padding:2px 10px;margin-left:4px;cursor:pointer;font-size:12px}
#win button.on{color:#fff;background:var(--accent);border-color:var(--accent)}
section h3{font-size:13px;color:var(--dim);margin:16px 0 8px;font-weight:600}
</style></head><body>
<h1>ARGUS 로컬 탐지 뷰 <small>records.jsonl 실시간 · 로컬 kind 전용</small></h1>
<div id="meta">연결 중…</div>
<div id="win">기간:
  <button data-m="15">15분</button><button data-m="60">1시간</button><button data-m="0" class="on">전체</button>
</div>
<section><h3>주체별 탐지 현황 — 같은 에이전트는 도구가 달라도 여기 한 카드다</h3>
<div class="grid" id="cards"></div></section>
<section><h3>이벤트 스트림 (최근 40건)</h3>
<table><thead><tr><th>시각(UTC)</th><th>principal</th><th>출처</th><th>도구</th><th>run</th><th>판정</th><th>사유</th></tr></thead>
<tbody id="rows"></tbody></table></section>
<script>
let winMin = 0;
document.querySelectorAll('#win button').forEach(b=>b.onclick=()=>{
  winMin=+b.dataset.m;
  document.querySelectorAll('#win button').forEach(x=>x.classList.toggle('on',x===b));
  render();
});
let D={records:[],policy:{},status:{}};
const short=s=>s?s.slice(0,10):'—';
const AXES=['principal_calls','duplicate_calls','run_steps','rotation_gap'];
function thr(ax){const a=((D.policy||{}).axes||{})[ax]||{};return {soft:+a.soft||0,hard:+a.hard||0};}
function gauge(ax,v){
  const t=thr(ax); const max=Math.max(t.hard*1.15,v,1);
  const col = t.hard&&v>=t.hard?'var(--hard)':(t.soft&&v>=t.soft?'var(--soft)':'var(--ok)');
  return `<div class="axis"><span class="nm">${ax}</span>
    <span class="bar"><i style="width:${Math.min(100,v/max*100)}%;background:${col}"></i></span>
    <span class="v">${v}${t.hard?' / '+t.hard:''}</span></div>`;
}
function render(){
  const cut = winMin? Date.now()-winMin*6e4 : 0;
  const recs = D.records.filter(r=>!cut||Date.parse(r.ts)>=cut);
  const last = recs[recs.length-1]||{};
  document.getElementById('meta').innerHTML =
    `<span class="pill">정책 <b>${last.policy_source||'?'}</b></span>`+
    `<span class="pill">레벨 <b>${last.control_level||'?'}</b></span>`+
    `<span class="pill">레코드 <b>${recs.length}</b>건</span>`+
    `<span class="pill">would_block <b>${recs.filter(r=>r.would_block).length}</b>건</span>`+
    `<span class="pill">tail: ${D.status.tail} · policy: ${D.status.policy}</span>`;
  const by={};
  for(const r of recs){
    const p=r.principal||'(없음)';
    (by[p]=by[p]||{recs:[],tools:new Set(),runs:new Set(),convs:new Set(),srcs:new Set()});
    const g=by[p]; g.recs.push(r);
    if(r.tool_name)g.tools.add(r.tool_name);
    if(r.run_id)g.runs.add(r.run_id);
    if(r.conversation_hash)g.convs.add(r.conversation_hash);
    if(r.principal_source)g.srcs.add(r.principal_source);
  }
  const cards = Object.entries(by).sort((a,b)=>b[1].recs.length-a[1].recs.length).map(([p,g])=>{
    const wb=g.recs.filter(r=>r.would_block).length;
    const lastR=g.recs[g.recs.length-1];
    const obs=lastR.observations||{};
    const hits={}; g.recs.forEach(r=>(r.policy_hit||[]).forEach(h=>{hits[h.axis+':'+h.level]=(hits[h.axis+':'+h.level]||0)+1}));
    const hitStr=Object.entries(hits).map(([k,n])=>{
      const lv=k.endsWith('hard')?'lvl-hard':'lvl-soft';
      return `<span class="${lv}">${k}×${n}</span>`;}).join(' ')||'<span class="pass">없음</span>';
    const srcs=[...g.srcs].map(s=>`<span class="tag${s==='header'?' spoof':''}">${s}</span>`).join(' ');
    return `<div class="card${wb?' blocked':''}">
      <h2><span>${short(p)}…</span>${srcs}</h2>
      <div class="krow">호출 <b>${g.recs.length}</b> · run <b>${g.runs.size}</b> · 대화 <b>${g.convs.size}</b>
        · team <b>${short(lastR.team)}</b> · would_block <b class="${wb?'wb':'pass'}">${wb}</b></div>
      <div class="krow">정책 히트: ${hitStr}</div>
      <div class="tools">${[...g.tools].map(t=>`<span>${t}</span>`).join('')||'<span>—</span>'}</div>
      ${AXES.filter(a=>obs[a]!=null).map(a=>gauge(a,obs[a])).join('')}
    </div>`;
  });
  document.getElementById('cards').innerHTML=cards.join('')||'<div class="card">이 기간의 레코드가 없다 — 트래픽을 흘려보내라 (hack/demo/multi_tool_identity.py)</div>';
  document.getElementById('rows').innerHTML = recs.slice(-40).reverse().map(r=>{
    const hit=(r.policy_hit||[]).map(h=>h.axis+':'+h.level).join(',');
    return `<tr><td>${(r.ts||'').slice(11,19)}</td><td>${short(r.principal)}</td>
      <td>${r.principal_source||''}</td><td>${r.tool_name||''}</td><td>${r.run_id||''}</td>
      <td class="${r.would_block?'wb':'pass'}">${r.would_block?'would_block':(r.status||'')}</td>
      <td class="reason">${r.decide_reason||hit||''}</td></tr>`;
  }).join('');
}
async function poll(){
  try{ D=await (await fetch('/data')).json(); render(); }catch(e){}
  setTimeout(poll,2000);
}
poll();
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 — http.server 계약
        if self.path.startswith("/data"):
            with _lock:
                body = json.dumps({"policy": _policy, "status": _status,
                                   "records": list(_records)}).encode()
            ctype = "application/json"
        else:
            body, ctype = PAGE.encode(), "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # 폴링이 2초마다라 액세스 로그는 소음이다
        pass


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8899)
    args = ap.parse_args()
    threading.Thread(target=_tail_loop, daemon=True).start()
    threading.Thread(target=_policy_loop, daemon=True).start()
    print(f"[argus-dash] http://localhost:{args.port}  (ctx={KCTX}, ns={NS}) — Ctrl-C 로 종료")
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
