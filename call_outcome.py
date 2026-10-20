#!/usr/bin/env python3
"""call_outcome.py - REPORT BACK AUTOMATICALLY WHEN A CALL ENDS.

Writes a short outcome note for every call once it has ended:
  logs/call-outcomes/<YYYY-MM-DD>-<callid>.md   (+ logs/call-outcomes/LATEST.md copy)
covering who was called, whether they answered, what was agreed (quoting the other side's own words
from the transcript), and anything needing the owner, plus a one-line "Tell the owner" sentence.
dial.sh prints 'CALL OUTCOME FILE: <path>' and 'CALL OUTCOME: <line>'; the bot relays that line at once.
Source: logs/pings/*-eve.md + logs/transcripts/ written by server.py.
Manual: call_outcome.py --selfhosted --ccid ID --callee NAME --to +E164 [--status no_answer|answered|failed]
Generalised from a working private setup (Oct 2026).
"""
import argparse, datetime, os, re, sys

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(ROOT, "logs/call-outcomes")
PRIVATE_RE = re.compile(r"(?:\d[ -]?){13,19}|\bcvv\b|\bcvc\b|\bpassword\b|\bpasscode\b", re.I)  # never quoted

AGREE_RE = re.compile(r"\b(yes|yep|yeah|sure|ok(ay)?|fine|great|perfect|confirm(ed)?|booked|book (you|that|it)|"
                      r"that works|sounds good|no problem|will do|i'?ll|we'?ll|see you|done|agreed|deal|"
                      r"(at|by|on) \d{1,2}([:.]\d\d)?\s*(am|pm)?|monday|tuesday|wednesday|thursday|friday|saturday|sunday|"
                      r"tomorrow|tonight|next week)\b|はい|了解|わかりました|大丈夫|お願いします|[0-9０-９一二三四五六七八九十]+時|にしてください", re.I)
ACTION_RE = re.compile(r"\?|call (me |him |her |them )?back|ring (me |him )?back|get back to|let (me|us) know|"
                       r"can (you|he|she)|could (you|he|she)|need(s)? (to|a|an|the)|please (send|email|call|confirm)|"
                       r"send (me|us|it)|email (me|us)|deposit|pay|price|cost|quote|confirm (with|by)|"
                       r"折り返し|連絡して|教えて|ください|か[。？]?\s*$", re.I)
def _voice_name():
    try:
        for l in open(os.path.join(ROOT, "config.env"), encoding="utf-8"):
            k, _, v = l.strip().partition("=")
            if k == "VOICE_NAME" and v.strip(): return v.strip().strip('"')
    except Exception:
        pass
    return "Eve"
VOICE_NAMES = (_voice_name(), _voice_name() + " (cut off)")


def now(): return datetime.datetime.now().astimezone()


def _ok(t): return t and not PRIVATE_RE.search(t)


def _q(t, n=220):
    t = re.sub(r"\s+", " ", t).strip()
    return '"' + (t if len(t) <= n else t[:n - 1] + "…") + '"'


def summarise(callee, to, call_id, outcome, lines, voicemail_left_text="", transcript_path="",
              telnyx_summary="", attempt=None, readback_id=None, when=None):
    """Pure function. lines = [(speaker, text), ...] in order; the voice is VOICE_NAMES, anyone else is
    the other side. outcome: answered | voicemail_left | voicemail_no_message | silent | no_answer |
    tech_fail | failed. Returns (markdown, tell_owner_line)."""
    when = when or now()
    other = [t.strip() for s, t in lines if s not in VOICE_NAMES and (t or "").strip()]
    other_ok = [t for t in other if _ok(t)]
    answered = outcome == "answered" or (outcome not in ("no_answer", "tech_fail", "failed", "silent")
                                         and not outcome.startswith("voicemail") and bool(other))
    is_owner = False  # template: every call is to someone else
    needs, agreed = [], []
    if outcome.startswith("voicemail"):
        ans_txt = "No - reached voicemail" + (" (the voice left a message)" if outcome == "voicemail_left" else " (no message left)")
        agreed_txt = ["Nothing - it went to voicemail."]
        if not is_owner:
            needs.append("Watch for a call back" if outcome == "voicemail_left" else "Decide whether to try again later")
    elif outcome in ("no_answer", "silent"):
        ans_txt = "No - no answer" if outcome == "no_answer" else "Picked up but nobody spoke"
        agreed_txt = ["Nothing - they didn't answer."] if outcome == "no_answer" else ["Nothing - no words heard."]
        if not is_owner: needs.append("Decide whether to try again later")
    elif outcome in ("tech_fail", "failed"):
        ans_txt = "No - the call didn't go through (technical fault)"
        agreed_txt = ["Nothing."]
        needs.append("Decide whether to try again later (fault on our side)")
    else:
        ans_txt = "Yes"
        agreed = list(dict.fromkeys(t for t in other_ok if AGREE_RE.search(t)))
        # Prefer substantive lines over a bare "yes"/"はい" when picking what to quote.
        subst = [t for t in agreed if len(re.sub(r"[\s、。,.!?！？]", "", t)) > 6]
        agreed = subst or agreed
        agreed_txt = [_q(t) for t in agreed[-4:]] or ["Nothing clearly agreed in their words - see the quotes below."]
        for t in other_ok:
            if ACTION_RE.search(t): needs.append(f"They said {_q(t, 160)} - may need your reply")
    needs = list(dict.fromkeys(needs))[:5]
    who = callee or "(unknown)"
    if outcome.startswith("voicemail") or outcome in ("no_answer", "silent", "tech_fail", "failed"):
        tell = f"I rang {who} ({to}): {ans_txt[5:].lower() if ans_txt.startswith('No - ') else ans_txt.lower()}."
    elif agreed:
        tell = f"I rang {who} and they answered. They said {_q(agreed[-1], 160)}."
    else:
        tell = f"I rang {who} and they answered; nothing was clearly agreed."
    tell = re.sub(r'([.!?。！？])"\.$', r'\1"', tell)
    if needs and not is_owner: tell += " " + ("Needs you: " + needs[0][0].lower() + needs[0][1:] + ".").replace("..", ".")
    md = [f"# Call outcome - {who} ({to})",
          f"- When: {when.strftime('%a %d %b %Y %H:%M %Z')}" + (f"  |  attempt {attempt}" if attempt else ""),
          f"- Call id: {call_id or '(none)'}",
          f"- Answered: {ans_txt}",
          "", "## What was agreed (their own words)", *[f"- {x}" for x in agreed_txt],
          "", "## Needs you", *([f"- {x}" for x in needs] or ["- Nothing"]),
          "", "## What they said (quoted from the transcript)",
          *([f"- {_q(t)}" for t in other_ok[:8]] or ["- (nothing heard from them)"])]
    if voicemail_left_text and _ok(voicemail_left_text):
        md += ["", "## Message the voice left", f"- {_q(voicemail_left_text, 400)}"]
    if telnyx_summary:
        s = " ".join(x for x in re.split(r"(?<=[.!?])\s+", telnyx_summary) if _ok(x)).strip()
        if s: md += ["", "## Telnyx's own summary (machine-written, not a quote)", s]
    md += ["", f"- Transcript: {transcript_path or '(none)'}"]
    if readback_id: md += [f"- Read-back confirmed by the owner: logs/readbacks/{readback_id}.md"]
    md += ["", "## Tell the owner", tell, ""]
    return "\n".join(md), tell


def write(md, call_id, out_dir=None, when=None):
    out_dir = out_dir or OUT_DIR; when = when or now()
    os.makedirs(out_dir, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_-]", "", (call_id or "nocall").replace("v3:", ""))[-24:] or "nocall"
    p = os.path.join(out_dir, f"{when.strftime('%Y-%m-%d')}-{safe}.md")
    for q in (p, os.path.join(out_dir, "LATEST.md")):
        with open(q, "w", encoding="utf-8") as fh: fh.write(md)
    return p


def selfhosted(ccid, callee, to, status=""):
    """Self-hosted path: find server.py's post-call ping for this call, then its transcript file."""
    pdir, tdir = os.path.join(ROOT, "logs/pings"), os.path.join(ROOT, "logs/transcripts")
    ping, ptxt = None, ""
    if ccid and os.path.isdir(pdir):
        for f in sorted(os.listdir(pdir), reverse=True)[:60]:
            if f.endswith("-eve.md"):
                x = open(os.path.join(pdir, f), encoding="utf-8").read()
                if ccid in x: ping, ptxt = os.path.join(pdir, f), x; break
    lines, tpath, oc = [], "", status or "no_answer"
    if ping:
        o = (re.search(r"^- outcome: (.+)$", ptxt, re.M) or [None, ""])[1].strip()
        oc = {"voicemail": "voicemail_left", "answered": "answered", "no reply heard": "silent"}.get(o, oc)
        sid = (re.search(r"^- stream_id: (\S+)", ptxt, re.M) or [None, ""])[1][:8]
        if sid and os.path.isdir(tdir):
            for f in sorted(os.listdir(tdir), reverse=True):
                if f.endswith(f"-{sid}.md"):
                    tpath = os.path.join(tdir, f)
                    for m in re.finditer(r"^\[\d\d:\d\d:\d\d\] ([^:]+): (.*)$", open(tpath, encoding="utf-8").read(), re.M):
                        lines.append((m[1].strip(), m[2]))
                    break
        if not lines:
            heard = ptxt.split("## Transcript (", 1)[1].split("\n", 1)[1].split("\n## ", 1)[0].strip() if "## Transcript (" in ptxt else ""
            if heard and heard != "(empty)": lines.append((callee, heard))
        tpath = tpath or ping
    md, tell = summarise(callee, to, ccid, oc, lines, transcript_path=tpath)
    return write(md, ccid), tell


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--selfhosted", action="store_true"); ap.add_argument("--ccid", default="")
    ap.add_argument("--callee", default=""); ap.add_argument("--to", default=""); ap.add_argument("--status", default="")
    ap.add_argument("--out-dir")
    a = ap.parse_args()
    try:
        if a.selfhosted:
            p, tell = selfhosted(a.ccid, a.callee or "the callee", a.to, a.status)
        else:
            ap.print_help(); return 4
    except Exception as e:
        print(f"CALL OUTCOME: (note failed: {e})"); return 1
    print(f"CALL OUTCOME FILE: {p}"); print(f"CALL OUTCOME: {tell}"); return 0


if __name__ == "__main__":
    sys.exit(main())
