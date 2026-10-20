#!/usr/bin/env bash
# dial.sh - the one way to place a call (generalised from a working private setup, Oct 2026).
#
# Usage: dial.sh [--dry-run] --name="Who" --purpose="Why" <+E164> <brief-file | "brief text">
#        dial.sh --path-check   (start server + tunnel, run the end-to-end checks, stop; never dials)
#
# READ-BACK BEFORE DIALLING: without a confirmed read-back (call_guard.py) nothing is dialled (also in
#   --dry-run): a read-back is written to logs/readbacks/ and printed, exit 6 (exit 7 = card number/password
#   in the brief). Show it to the owner; after their yes: call_guard.py confirm <id> --owner-said "..." and
#   re-run the same command.
# REPORT BACK: when the call ends an outcome note goes to logs/call-outcomes/ and 'CALL OUTCOME FILE:' /
#   'CALL OUTCOME:' lines are printed: relay that to the owner straight away.
#
# Steps: (a) read-back guard (b) start the call server (server.py, localhost) and a temporary Cloudflare
# quick tunnel, then check end to end: local /health, public /health, webhook POST, media WebSocket upgrade,
# voice API reachable (c) POST /v2/calls (L16 @ 16k, stream on dial) (d) watch webhooks + Telnyx
# GET /v2/calls/{id} until the media stream starts (e) stay in the foreground until hang-up (keeps the
# computer awake) (f) outcome note (g) stop the tunnel and server. Technical failure before answer, or
# answered but no stream within 20s -> hang up and retry once. Every step is logged to logs/dial-attempts.log.
#
# Exit: 0 call connected with media stream (or dry-run passed) | 1 phone path down, nothing dialled
#       2 not answered / busy / declined                        | 3 call placed but failed after retry
#       4 usage or setup error                                  | 6 read-back not confirmed, nothing dialled
#       7 brief has a card number or password, nothing dialled
set -uo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
[[ -f "$ROOT/config.env" ]] || { echo "dial.sh: not set up yet - run setup.sh first" >&2; exit 4; }
source "$ROOT/config.env"   # FROM, CONN, OWNER_NAME, VOICE_NAME, XAI_VOICE, PORT
PORT=${PORT:-8790}
LOG=$ROOT/logs/dial-attempts.log
CALLLOG=$ROOT/logs/call.log
PY=$ROOT/.venv/bin/python
CF=${CLOUDFLARED:-$(command -v cloudflared || echo "$ROOT/bin/cloudflared")}
export PATH="$HOME/.local/bin:$HOME/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
: "${TELNYX_API_KEY:?TELNYX_API_KEY is not set (add it in the bot's secret box)}"
: "${XAI_API_KEY:?XAI_API_KEY is not set (add it in the bot's secret box)}"

DRY=0; PATHCHECK=0; ARGS=(); CNAME=""; PURPOSE=""
for a in "$@"; do case "$a" in --dry-run) DRY=1;; --path-check) PATHCHECK=1;; --name=*) CNAME=${a#--name=};; --purpose=*) PURPOSE=${a#--purpose=};; -h|--help) sed -n 2,26p "$0"; exit 0;; *) ARGS+=("$a");; esac; done
[[ $PATHCHECK == 1 ]] && ARGS=(+10000000000 "path check")
[[ ${#ARGS[@]} -eq 2 ]] || { echo "usage: $0 [--dry-run] --name=\"Who\" --purpose=\"Why\" <+E164> <brief-file|text>" >&2; exit 4; }
TO=${ARGS[0]}; BRIEF=${ARGS[1]}
[[ "$TO" =~ ^\+[1-9][0-9]{7,14}$ ]] || { echo "bad number (use +countrycode...): $TO" >&2; exit 4; }

RUN_ID=$(date +%Y%m%d-%H%M%S)-$$
T0=$(date +%s.%N)
el(){ python3 -c "import time;print(f'{time.time()-$T0:.1f}s')"; }
log(){ mkdir -p "$ROOT/logs"; local l="$(date '+%Y-%m-%d %H:%M:%S %Z') [$RUN_ID] +$(el) $*"; echo "$l" >> "$LOG"; echo "$l" >&2; }
die(){ log "RESULT exit=$1 $2"; echo "dial.sh: $2" >&2; exit "$1"; }

log "START to=$TO dry_run=$DRY brief=$( [[ -f "$BRIEF" ]] && echo "file:$BRIEF" || echo "text(${#BRIEF} chars)")"

# ---- brief ----
if [[ -f "$BRIEF" ]]; then BRIEF_TEXT=$(cat "$BRIEF"); else BRIEF_TEXT=$BRIEF; fi
[[ -n "${BRIEF_TEXT// /}" ]] || die 4 "brief is empty"

# ---- read-back before dialling (call_guard.py). Applies to --dry-run too. ----
if [[ $PATHCHECK == 0 ]]; then
GF=$(mktemp /tmp/guard-brief.XXXXXX); printf '%s\n' "$BRIEF_TEXT" > "$GF"
GOUT=$(python3 "$ROOT/call_guard.py" check --to "$TO" --brief-file "$GF" --name "$CNAME" --purpose "$PURPOSE" --voice "${VOICE_NAME:-Eve}"); grc=$?; rm -f "$GF"
printf '%s\n' "$GOUT"
GUARD_ID=$(sed -n 's/^GUARD_ID: //p' <<<"$GOUT" | head -1)
log "  read-back guard rc=$grc id=${GUARD_ID:-none}"
(( grc == 0 )) || die "$grc" "read-back not confirmed for $TO; nothing dialled (show the owner logs/readbacks/$GUARD_ID.md)"
fi
GUARD_ID=${GUARD_ID:-}

# ---- (b) call server + temporary tunnel, only for this call ----
mkdir -p "$ROOT/logs" "$ROOT/pids"
stop_path(){
  for p in cloudflared server; do
    [[ -f "$ROOT/pids/$p.pid" ]] && kill "$(cat "$ROOT/pids/$p.pid")" 2>/dev/null; rm -f "$ROOT/pids/$p.pid"
  done
}
trap stop_path EXIT
# Resolve a hostname through Cloudflare DNS-over-HTTPS (1.1.1.1), not the computer's resolver: some bot
# computers' resolvers hand back a placeholder address and proxy traffic badly (from the original setup, 10 Oct 2026).
doh_ip(){ curl -fsS -m 5 -H 'accept: application/dns-json' "https://1.1.1.1/dns-query?name=$1&type=A" 2>/dev/null \
  | python3 -c "import json,sys;print(next((a['data'] for a in json.load(sys.stdin).get('Answer',[]) if a.get('type')==1),''))" 2>/dev/null; }
start_server(){
  nohup env PUBLIC_URL="$PUBLIC" MEDIA_WSS_URL="wss://${HOST}/telnyx/media" PORT="$PORT" \
    "$PY" "$ROOT/server.py" >>"$ROOT/logs/server.out" 2>&1 &
  echo $! > "$ROOT/pids/server.pid"
  for i in $(seq 1 40); do curl -fsS -m 2 "http://127.0.0.1:${PORT}/health" >/dev/null 2>&1 && break; sleep 0.25; done
}
start_path(){
  stop_path
  PUBLIC=""
  # Optional fixed tunnel (a Cloudflare named tunnel on your own domain): set TUNNEL_TOKEN_FILE and PUBLIC_URL in
  # config.env, with the tunnel's public hostname pointed at http://localhost:$PORT. Most reliable; needs a Cloudflare account.
  if [[ -n "${TUNNEL_TOKEN_FILE:-}" && -n "${PUBLIC_URL:-}" ]]; then
    # 19 Oct 2026: another program that also runs this tunnel (e.g. an older watchdog) can kill or restart it
    # mid-call, which drops the media link. Warn so it can be paused first.
    local other; other=$(pgrep -f "run --token-file $TUNNEL_TOKEN_FILE" | tr '\n' ' ')
    [[ -n "$other" ]] && log "  WARN another process already runs this fixed tunnel (pid $other); if something else manages it, it may restart it mid-call"
    : > "$ROOT/logs/cloudflared.out"
    nohup "$CF" --no-autoupdate tunnel --protocol http2 --edge 198.41.192.27:7844 --edge 198.41.200.13:7844 \
      run --token-file "$TUNNEL_TOKEN_FILE" >>"$ROOT/logs/cloudflared.out" 2>&1 < /dev/null &
    echo $! > "$ROOT/pids/cloudflared.pid"
    for i in $(seq 1 40); do grep -q "Registered tunnel connection" "$ROOT/logs/cloudflared.out" 2>/dev/null && break; sleep 0.5; done
    grep -q "Registered tunnel connection" "$ROOT/logs/cloudflared.out" || { log "  fixed tunnel did not register (see logs/cloudflared.out)"; return 1; }
    PUBLIC=${PUBLIC_URL%/}; HOST=${PUBLIC#https://}
    start_server; log "  path up: fixed tunnel $PUBLIC server pid $(cat "$ROOT/pids/server.pid")"; return 0
  fi
  # Plain quick tunnel first. Fallback (from the original setup, 8 Oct 2026): some computers' DNS returns a
  # placeholder address for Cloudflare's tunnel edge, so the tunnel never registers; then pin the edge over http2.
  local mode
  for mode in plain pinned; do
    : > "$ROOT/logs/cloudflared.out"
    if [[ $mode == plain ]]; then
      nohup "$CF" tunnel --url "http://127.0.0.1:${PORT}" --no-autoupdate ${CLOUDFLARED_ARGS:-} >>"$ROOT/logs/cloudflared.out" 2>&1 &
    else
      nohup "$CF" tunnel --url "http://127.0.0.1:${PORT}" --no-autoupdate --protocol http2 \
        --edge 198.41.192.27:7844 --edge 198.41.200.13:7844 >>"$ROOT/logs/cloudflared.out" 2>&1 &
    fi
    echo $! > "$ROOT/pids/cloudflared.pid"
    for i in $(seq 1 40); do
      # the URL is printed before the edge connection is up; wait for "Registered tunnel connection" too
      if grep -q "Registered tunnel connection" "$ROOT/logs/cloudflared.out" 2>/dev/null; then
        PUBLIC=$(grep -oE "https://[a-z0-9-]+\.trycloudflare\.com" "$ROOT/logs/cloudflared.out" 2>/dev/null | head -1 || true)
        [[ -n "$PUBLIC" ]] && break
      fi
      grep -qE "429 Too Many Requests|failed to unmarshal" "$ROOT/logs/cloudflared.out" 2>/dev/null && (( i > 6 )) && break
      sleep 0.5
    done
    if [[ -n "$PUBLIC" ]]; then
      # The hostname must exist in public DNS before Telnyx can reach it. Wait up to 150s (checked via 1.1.1.1);
      # on 19 Oct 2026 new quick-tunnel names took ~60s+ to appear, sometimes never.
      local h=${PUBLIC#https://} w ip=""
      for w in $(seq 1 50); do ip=$(doh_ip "$h"); [[ -n "$ip" ]] && break; sleep 3; done
      if [[ -n "$ip" ]]; then log "  quick tunnel up ($mode), public DNS ok after ~$((w*3))s"; break; fi
      log "  quick tunnel ($mode) registered but its hostname never appeared in public DNS (150s)"
      PUBLIC=""
      kill "$(cat "$ROOT/pids/cloudflared.pid")" 2>/dev/null; rm -f "$ROOT/pids/cloudflared.pid"
      continue
    fi
    log "  quick tunnel ($mode) did not register in 20s"
    kill "$(cat "$ROOT/pids/cloudflared.pid")" 2>/dev/null; rm -f "$ROOT/pids/cloudflared.pid"
  done
  [[ -n "$PUBLIC" ]] || { log "  could not open the quick tunnel (see logs/cloudflared.out)"; return 1; }
  HOST=${PUBLIC#https://}
  start_server
  log "  path up: tunnel $PUBLIC server pid $(cat "$ROOT/pids/server.pid")"
}
FAIL=""
chk(){ local name=$1; shift; local s=$(date +%s.%N); if "$@"; then log "  ok   $name ($(python3 -c "import time;print(f'{time.time()-$s:.2f}s')"))"; else log "  FAIL $name"; FAIL="$FAIL $name"; fi; }
c_local(){ curl -fsS -m 5 "http://127.0.0.1:${PORT}/health" 2>/dev/null | grep -q '"ok": *true'; }
RES=()   # curl --resolve pin for the public host, set by pin_host
pin_host(){ local ip; ip=$(doh_ip "$HOST"); RES=(); PIN_IP=""; [[ -n "$ip" ]] && { RES=(--resolve "$HOST:443:$ip"); PIN_IP=$ip; }; }
c_public(){ pin_host; curl -fsS -m 12 "${RES[@]}" "$PUBLIC/health" 2>/dev/null | grep -q '"service": *"phone-caller"'; }
# Webhook path through the tunnel: harmless unknown event; server answers 200 "ignored" (or 401 if signatures enforced).
c_webhook(){ local code; code=$(curl -sS -m 12 "${RES[@]}" -o /tmp/dialsh-wh.$$ -w '%{http_code}' -X POST -H 'Content-Type: application/json' \
   -d '{"data":{"event_type":"dialsh.probe","payload":{}}}' "$PUBLIC/telnyx/voice" 2>/dev/null)
   { [[ $code == 200 ]] && grep -q '"ignored"' /tmp/dialsh-wh.$$; } || { [[ $code == 401 ]] && grep -q 'invalid signature' /tmp/dialsh-wh.$$; }; local r=$?; rm -f /tmp/dialsh-wh.$$; return $r; }
# Media WebSocket upgrade through the tunnel (101 + ping/pong), then close.
c_ws(){ WSS="wss://${HOST}/telnyx/media?probe=dialsh" WS_HOST="$HOST" WS_IP="${PIN_IP:-}" "$PY" - <<'PYEOF' 2>/dev/null
import asyncio, os, sys, websockets
async def main():
    kw = {"host": os.environ["WS_IP"], "port": 443, "server_hostname": os.environ["WS_HOST"]} if os.environ.get("WS_IP") else {}
    async with websockets.connect(os.environ["WSS"], open_timeout=12, close_timeout=3, **kw) as ws:
        pong = await ws.ping(); await asyncio.wait_for(pong, 5)
try: asyncio.run(main()); sys.exit(0)
except Exception: sys.exit(1)
PYEOF
}
c_voiceapi(){ local code; code=$(curl -sS -m 8 -o /dev/null -w '%{http_code}' https://api.x.ai/v1/models 2>/dev/null); [[ $code =~ ^(200|401|403)$ ]]; }
verify(){ FAIL=""; chk local_server c_local; chk public_tunnel c_public; chk webhook_post c_webhook; chk media_ws_upgrade c_ws; chk voice_api c_voiceapi; [[ -z "$FAIL" ]]; }
ready(){ # start + verify, up to 3 tries (a fresh quick tunnel can take a few seconds to answer)
  local try
  for try in 1 2 3; do
    log "path try $try"
    if [[ $try == 1 || "$FAIL" == *public_tunnel* || "$FAIL" == *media_ws* || "$FAIL" == *webhook* || -z "${PUBLIC:-}" ]]; then start_path || continue; fi
    # a brand-new quick tunnel hostname can take a while to answer publicly: wait up to 45s for it
    local w; for w in $(seq 1 15); do c_public && break; sleep 3; done
    if verify; then log "PATH OK (try $try)"; return 0; fi
  done
  log "PATH STILL FAILING:$FAIL"; return 1
}

ready || die 1 "phone path down (failing:$FAIL); nothing dialled"
if [[ $PATHCHECK == 1 ]]; then die 0 "PATH CHECK passed in $(el) (nothing dialled)"; fi
if [[ $DRY == 1 ]]; then die 0 "DRY RUN passed in $(el); would dial $TO (no call placed)"; fi

# ---- one call at a time ----
exec 7>"$ROOT/.dial.lock"
if ! flock -w 400 7; then die 1 "another call is still running after 400s; nothing dialled"; fi
log "call lock acquired"
[[ -n "$GUARD_ID" ]] && { python3 "$ROOT/call_guard.py" consume "$GUARD_ID"; log "  read-back $GUARD_ID used"; }

# ---- brief into PHONE_CONTEXT.md (read by server.py at answer time) ----
{ printf '## THIS CALL HAS A MESSAGE TO DELIVER\n<!-- call_kind: ondemand -->\nYou placed this call to %s. What the call is for is below: use its facts, not its wording, and say each point once. If nobody answers within about 5 seconds, say a short goodbye and end the call.\nCALLEE_FIRST_NAME: %s\n\n### Message\n%s\n\n' "${CNAME:-them}" "$(cut -d' ' -f1 <<<"${CNAME:-Them}")" "$BRIEF_TEXT"; } > "$ROOT/PHONE_CONTEXT.md.new" && mv -f "$ROOT/PHONE_CONTEXT.md.new" "$ROOT/PHONE_CONTEXT.md"
log "brief written to PHONE_CONTEXT.md"

telnyx(){ # telnyx METHOD PATH [json]  -> prints body; key never printed
  "$PY" - "$@" <<'PYEOF'
import json, os, sys, urllib.request, urllib.error
key = os.environ.get("TELNYX_API_KEY", "")
m, p = sys.argv[1], sys.argv[2]; d = sys.argv[3].encode() if len(sys.argv) > 3 else None
r = urllib.request.Request("https://api.telnyx.com" + p, data=d, method=m,
    headers={"Authorization": "Bearer " + key, "Content-Type": "application/json", "Accept": "application/json"})
try:
    with urllib.request.urlopen(r, timeout=20) as resp: print(resp.read().decode())
except urllib.error.HTTPError as e: print(json.dumps({"http_error": e.code, "body": e.read().decode()[:500]}))
except Exception as e: print(json.dumps({"error": str(e)[:300]}))
PYEOF
}

place(){ # sets CCID
  local body; body=$(python3 - "$TO" "$FROM" "$CONN" "$PUBLIC" <<'PYEOF'
import json, sys
to, frm, conn, front = sys.argv[1:5]
print(json.dumps({"connection_id": conn, "to": to, "from": frm,
  "timeout_secs": 60, "time_limit_secs": 300, "answering_machine_detection": "disabled",
  "webhook_url": f"{front}/telnyx/voice", "webhook_url_method": "POST",
  "stream_url": "wss://" + front.split("://", 1)[1] + "/telnyx/media", "stream_track": "inbound_track",
  "stream_bidirectional_mode": "rtp", "stream_bidirectional_codec": "L16", "stream_bidirectional_target_legs": "self",
  "stream_bidirectional_sampling_rate": 16000, "stream_codec": "L16"}))
PYEOF
)
  local resp; resp=$(telnyx POST /v2/calls "$body")
  echo "$resp" > "$ROOT/logs/last-dial.json"
  CCID=$(echo "$resp" | python3 -c "import json,sys;print((json.load(sys.stdin).get('data') or {}).get('call_control_id',''))" 2>/dev/null)
  [[ -n "$CCID" ]] || { log "  POST /v2/calls FAILED: $(echo "$resp" | head -c 300)"; return 1; }
  log "  POST ok call_control_id=$CCID"; return 0
}

# watch -> sets OUTCOME: stream_ok | no_answer | tech_fail | no_stream
watch(){
  local off=$1 s=$(date +%s) init=0 ans=0 ans_t=0 cause="" n=0 warned=0
  OUTCOME=tech_fail
  while :; do
    local new; new=$(tail -c +$((off+1)) "$CALLLOG" 2>/dev/null)
    grep -q "event=call.initiated call_control_id=$CCID" <<<"$new" && { [[ $init == 0 ]] && log "  webhook call.initiated received"; init=1; }
    if grep -q "event=call.answered call_control_id=$CCID" <<<"$new" && [[ $ans == 0 ]]; then ans=1; ans_t=$(date +%s); log "  call.answered"; fi
    if grep -q "event=streaming.started call_control_id=$CCID" <<<"$new" || grep -q "TELNYX_WS event=start" <<<"$new"; then
      log "  media stream started"; OUTCOME=stream_ok; return; fi
    if grep -q "HANGUP_INFO call_control_id=$CCID" <<<"$new"; then
      cause=$(grep "HANGUP_INFO call_control_id=$CCID" <<<"$new" | sed -n 's/.*cause=\([^ ]*\).*/\1/p' | tail -1)
      log "  hangup cause=$cause answered=$ans"
      if [[ $ans == 1 ]]; then OUTCOME=no_stream
      elif [[ "$cause" =~ ^(user_busy|timeout|no_answer|call_rejected|normal_clearing|originator_cancel)$ ]]; then OUTCOME=no_answer
      else OUTCOME=tech_fail; fi; return
    fi
    local now=$(date +%s)
    if [[ $init == 0 && $warned == 0 && $((now-s)) -ge 15 ]]; then warned=1; log "  WARN no call.initiated webhook after 15s (webhook path?)"; fi
    if [[ $ans == 1 && $((now-ans_t)) -ge 20 ]]; then log "  answered but no media stream after 20s"; OUTCOME=no_stream; return; fi
    if (( n % 6 == 5 )); then
      local alive; alive=$(telnyx GET "/v2/calls/$CCID" | python3 -c "import json,sys;d=json.load(sys.stdin);print((d.get('data') or {}).get('is_alive','?'))" 2>/dev/null)
      if [[ "$alive" == False ]]; then sleep 2; new=$(tail -c +$((off+1)) "$CALLLOG")
        grep -q "HANGUP_INFO call_control_id=$CCID" <<<"$new" && continue
        log "  Telnyx says call ended (is_alive=false), no hangup webhook seen"; OUTCOME=$([[ $ans == 1 ]] && echo no_stream || echo tech_fail); return; fi
    fi
    (( now - s >= 95 )) && { log "  watch timeout 95s (answered=$ans)"; OUTCOME=$([[ $ans == 1 ]] && echo no_stream || echo tech_fail); return; }
    n=$((n+1)); sleep 1
  done
}

# wait_end: stay in the foreground (keeps the computer awake) until the call ends. Cap 360s (calls cap at 300s).
wait_end(){
  local off=$1 s=$(date +%s) alive cause
  log "  waiting for call to end (poll 10s, cap 360s)"
  while :; do
    sleep 10
    if tail -c +$((off+1)) "$CALLLOG" 2>/dev/null | grep -q "HANGUP_INFO call_control_id=$CCID"; then
      cause=$(tail -c +$((off+1)) "$CALLLOG" | grep "HANGUP_INFO call_control_id=$CCID" | sed -n 's/.*cause=\([^ ]*\).*/\1/p' | tail -1)
      log "  call ended (hangup webhook, cause=${cause:-?}) after $(( $(date +%s) - s ))s"; return 0; fi
    alive=$(telnyx GET "/v2/calls/$CCID" | python3 -c "import json,sys;d=json.load(sys.stdin);print((d.get('data') or {}).get('is_alive','?'))" 2>/dev/null)
    if [[ "$alive" == False ]]; then log "  call ended (Telnyx is_alive=false) after $(( $(date +%s) - s ))s"; return 0; fi
    (( $(date +%s) - s >= 360 )) && { log "  WARN call still alive (is_alive=$alive) after 360s cap; exiting wait"; return 0; }
  done
}

outcome_note(){ sleep 4  # let the server write its post-call note
  python3 "$ROOT/call_outcome.py" --selfhosted --ccid "${CCID:-}" --callee "${CNAME:-$TO}" --to "$TO" --status "$1" 2>&1 | tee -a "$LOG" || true; }

hangup(){ telnyx POST "/v2/calls/$CCID/actions/hangup" '{}' >/dev/null; log "  hangup sent for $CCID"; }

for attempt in 1 2; do
  log "DIAL attempt $attempt to=$TO"
  OFF=$(stat -c %s "$CALLLOG" 2>/dev/null || echo 0)
  if place; then watch "$OFF"; else OUTCOME=tech_fail; CCID=""; fi
  log "  outcome=$OUTCOME"
  case $OUTCOME in
    stream_ok) wait_end "$OFF"; outcome_note answered; die 0 "connected: $TO, media stream up, call ended (attempt $attempt, ccid=$CCID)";;
    no_answer) outcome_note no_answer; die 2 "not answered/declined (no fault on our side)";;
    no_stream) hangup;;
  esac
  [[ $attempt == 2 ]] && break
  ready || die 3 "retry aborted: phone path down (failing:$FAIL)"
done
outcome_note failed
die 3 "call failed after retry (last outcome=$OUTCOME)"
