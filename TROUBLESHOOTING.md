# Phone Caller: troubleshooting

This is for Grok Bots installing and running the Phone Caller template. Each entry gives the symptom you will see, the cause, and the fix. Start with the log: `dial.sh` writes every step with a timestamp, and `logs/server.log` shows what the voice server did during the call.

## Exit codes from dial.sh

| Code | Meaning | What to do |
|---|---|---|
| 0 | Call connected and ended normally | Read the call outcome file it prints |
| 4 | Wrong arguments | Usage: `dial.sh [--dry-run] --name="Who" --purpose="Why" <+E164> <brief-file or text>` |
| 6 | Read-back needed. Nothing was dialled | See "Read-back" below |
| 1 | Phone path down, nothing dialled | See which check failed (sections 2, 3, 6) |
| 2 | Not answered, busy or declined | Tell the owner. Don't redial unless asked |
| 3 | Call placed but failed after one retry | Check server.log around the call time |
| 7 | The brief has a card number or password | Remove it. Those never go in a call |
| other non-zero | The call path failed (server, tunnel, webhook, or voice) | Check which `ok`/`FAIL` line failed in the log |

## 1. Read-back: dial.sh exits 6
- **Symptom:** `GUARD: READBACK REQUIRED - nothing dialled`, exit 6.
- **Cause:** This is on purpose. Every new number and brief needs the owner's yes before the call. Each read-back can be used once.
- **Fix:** Show the owner the read-back file (`logs/readbacks/<ID>.md`) in chat. After they say yes, run
  `python3 call_guard.py confirm <ID> --owner-said "<their words>"`,
  then run the **same** dial command again. If you change the number or the brief, you need a new read-back. Never confirm a read-back the owner hasn't approved.

## 2. Free tunnel address doesn't resolve (DNS)
- **Symptom:** The `public_tunnel` check fails right after a free `trycloudflare.com` tunnel starts, or Telnyx can't reach the webhook.
- **Cause:** A new free tunnel address takes a while to appear in public DNS. The Grok Bot computer's own DNS also gives fake addresses for outside names, so checking with it tells you nothing.
- **Fix:** dial.sh already checks through 1.1.1.1 (`dig @1.1.1.1 <host>`) and waits up to 150 seconds. If it still fails, restart the tunnel once and try again. For regular use, set up a fixed (named) tunnel: put `TUNNEL_TOKEN_FILE` and `PUBLIC_URL` in config.env. Then the address never changes and you don't have to wait for DNS.

## 3. Telnyx webhook URL doesn't match
- **Symptom:** The call rings but nobody speaks, or no `call.initiated` / `call.answered` lines show in the log.
- **Cause:** The Telnyx Call Control app (connection `CONN`) still points at an old tunnel address.
- **Fix:** dial.sh sends the current webhook address with each call, so this mostly happens when calls are placed some other way, or when the tunnel restarts during a call. Make sure the Call Control app's webhook is `<tunnel address>/telnyx/voice` (the media stream uses `wss://<host>/telnyx/media`). Rerun `setup.py` if the app was deleted or `CONN` is wrong. The `webhook_post` check in dial.sh only tests your own end. It can't see what Telnyx has saved.

## 4. Double greeting, or the call drops halfway
- **Symptom:** Eve says hello twice, or the call cuts out or restarts partway through.
- **Cause:** Usually something restarted the tunnel or server during the call. An old watchdog script that "fixes" the tunnel when a health check is slow will do this. A short drop in the audio link can also cause it.
- **Fix:** Run only one thing that manages the tunnel. Check with `pgrep -af watchdog` and stop any old watchdog before calling. The server now holds the voice session for 20 seconds if the audio link drops, so a short drop no longer brings a second greeting. If you still hear one, look in server.log for two `session.updated` lines with a `bridge close` between them.

## 5. The Grok Bot computer sleeps between turns
- **Symptom:** Calls fail or drop when nobody is chatting with the bot. Scheduled calls sometimes never happen. The tunnel or server is gone when you come back.
- **Cause:** This is a Grok Bot limitation. The computer can pause between turns, and that stops the local server and tunnel.
- **Fix:** Start the server and tunnel just before each call. dial.sh does this and checks the whole path before it dials. Don't assume a server you left running earlier is still up. For calls at a set time, run them from a routine that starts the path and dials in the same turn.

## 6. Missing or locked keys
- **Symptom:** `voice_api` fails, Telnyx says 401/403 when dialling, or setup stops on a missing setting.
- **Cause:** The Telnyx API key or the voice API key isn't set, isn't readable, or has been replaced. Or `FROM`/`CONN` in config.env doesn't match the Telnyx account.
- **Fix:** Check that `TELNYX_API_KEY`, `TELNYX_NUMBER` and `XAI_API_KEY` are in the masked secret box, then rerun `setup.py`. For xAI, also check the account has credit. Never paste keys into briefs, logs or chat. If a key file is blocked from reading, ask the owner to add the key the proper way. Don't work around the block.

## 7. Voicemail
- **Symptom:** The call "connects" but the person never answers, or the transcript shows a voicemail greeting.
- **Cause:** The phone went to voicemail. Telnyx reports that as answered.
- **Fix:** Eve leaves one short message (who she is, why she called, whether to call back) and hangs up. The template doesn't redial on its own. The outcome file shows Answered and has no reply quoted. Tell the owner, and redial only if they ask.

## 8. Eve sounds rough or repeats herself
- **Symptom:** She talks over people, repeats the introduction, or keeps talking after saying goodbye.
- **Cause:** The pause before replying is too short, the instructions are too long, or replies keep coming after the hang-up.
- **Fix:** The defaults are a 450 ms pause (`VAD_SILENCE_MS` in config.env), short instructions, no new replies after `end_call`, and a hang-up 2.5 seconds after the goodbye. Keep briefs to a few plain lines of facts. Test with a short call to the owner after any change.

## 9. Another call is already running
- **Symptom:** dial.sh waits, then stops with "another call is still running after 400s; nothing dialled" (exit 1).
- **Cause:** Only one call can run at a time.
- **Fix:** Wait for that call to finish. If dial.sh crashed earlier, the lock (`.dial.lock`) frees itself when the process ends. Check `pids/` for a stray server only if no call is live.

## Quick check before a real call
1. Run `bash dial.sh --path-check` (it never dials), or a dry run: `bash dial.sh --dry-run --name=... --purpose=... +15550100000 brief.txt`
2. Check that all five lines show `ok`: local_server, public_tunnel, webhook_post, media_ws_upgrade, voice_api
3. Check that no old watchdog is running
4. Check the read-back is confirmed. Then dial.

## 10. A long pause before the goodbye
- **Symptom:** The person answers, then there's a noticeable gap before "Thanks, goodbye".
- **Cause:** The voice waits for a stretch of silence before replying, then has to produce its last line.
- **Fix:** Try `VAD_SILENCE_MS="350"` or `"400"` in config.env, then do a test call. If it starts cutting people off, go back up.
