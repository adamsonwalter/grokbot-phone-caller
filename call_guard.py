#!/usr/bin/env python3
"""call_guard.py - READ-BACK BEFORE DIALLING.

Every call needs the owner's yes in chat first. The bot shows: the number, the person or business,
the purpose, and the key details the voice will say. Only after a yes does the bot record the
confirmation; dial.sh refuses otherwise (exit 6).

Usage:
  call_guard.py readback --to +E164 --brief-file F [--name "Who"] [--purpose "Why"] [--voice eve]
        -> writes logs/readbacks/<id>.md (+ .json), prints the read-back text, exit 0
           (exit 7 if the brief contains a card number or password: those never go in a call script)
  call_guard.py confirm <id> --owner-said "their exact yes"
        -> ONLY after the owner has said yes in chat to THIS read-back.
  call_guard.py check --to +E164 --brief-file F [--name ..] [--purpose ..]
        -> exit 0 = may dial (a confirmed, unused, <3h old read-back with the same number and brief);
           exit 6 = refused (a fresh read-back file is written and printed); exit 7 = sensitive line
  call_guard.py consume <id>   -> marks a confirmation used (dial.sh does this on a real dial)
A confirmation is single-use and bound to the number + exact brief text: change either and you need a new yes.
Generalised from a working private setup (Oct 2026).
"""
import argparse, datetime, hashlib, json, os, re, sys

ROOT = os.path.dirname(os.path.abspath(__file__))
RB_DIR = os.path.join(ROOT, "logs/readbacks")
EXEMPT = set()                    # no exemptions: every call is read back first
MAX_AGE_SECS = 3 * 3600
# Card numbers (13-19 digits, spaces/dashes allowed), CVV/PIN/password mentions never go in a call script.
SENSITIVE_RE = re.compile(r"(?:\d[ -]?){13,19}|\bcvv\b|\bcvc\b|\bpin (?:number|code)\b|\bpassword\b|\bpasscode\b", re.I)
SETTLED_RE = re.compile(r"(?!x)x")  # placeholder: matches nothing


def norm_number(n):
    n = re.sub(r"[\s()-]", "", str(n or ""))
    return n


def is_exempt(to): return norm_number(to) in EXEMPT


def clean_brief(text):
    """Sensitive lines (card numbers, PINs, passwords) never go in a script."""
    keep, sensitive = [], 0
    for l in (text or "").splitlines():
        if SETTLED_RE.search(l): continue
        if SENSITIVE_RE.search(l): sensitive += 1; continue
        keep.append(l)
    return "\n".join(keep).strip(), sensitive


def brief_hash(text):
    t, _ = clean_brief(text)
    return hashlib.sha256(re.sub(r"\s+", " ", t).strip().encode("utf-8")).hexdigest()[:16]


def lookup_name(to):
    """No contacts lookup in the template: the bot always passes --name."""
    return ""


def now(): return datetime.datetime.now().astimezone()


def _paths(rid): return (os.path.join(RB_DIR, rid + ".md"), os.path.join(RB_DIR, rid + ".json"),
                         os.path.join(RB_DIR, rid + ".confirmed"), os.path.join(RB_DIR, rid + ".used"))


def write_readback(to, brief_text, name="", purpose="", voice="eve"):
    """Write the read-back files. Returns (rid, md_text, problems)."""
    os.makedirs(RB_DIR, exist_ok=True)
    to = norm_number(to); name = (name or "").strip() or lookup_name(to)
    shown, sensitive = clean_brief(brief_text)
    h = brief_hash(brief_text)
    rid = now().strftime("%Y%m%d-%H%M%S") + "-" + h[:8]
    problems = []
    if not name: problems.append("WHO IS MISSING - add --name (person or business) before showing the owner")
    if not (purpose or "").strip(): problems.append("PURPOSE IS MISSING - add --purpose before showing the owner")
    if sensitive: problems.append(f"PRIVATE: {sensitive} line(s) contain a card number, PIN or password - remove them; never in a call script")
    md = "\n".join([
        f"# Read-back before dialling ({rid})",
        "",
        "Show this to the owner in chat and dial ONLY after they say yes.",
        "",
        f"- Number: {to}",
        f"- Calling: {name or '(not given)'}",
        f"- Purpose: {(purpose or '').strip() or '(not given)'}",
        f"- Voice: {voice.title()}",
        f"- Written: {now().strftime('%a %d %b %Y %H:%M %Z')}",
        "",
        f"## What {voice.title()} will say (key details)",
        shown or "(empty brief)",
        "",
        *([f"## Problems (fix before asking the owner)"] + [f"- {p}" for p in problems] + [""] if problems else []),
        "## After the owner says yes",
        f"python3 {ROOT}/call_guard.py confirm {rid} --owner-said \"<their words>\"",
        "then re-run the SAME dial command (same number, same brief). Changing either needs a new read-back.",
        "",
    ])
    md_p, js_p, _, _ = _paths(rid)
    open(md_p, "w", encoding="utf-8").write(md)
    json.dump({"id": rid, "to": to, "name": name, "purpose": (purpose or "").strip(), "voice": voice,
               "brief_hash": h, "created": now().isoformat(), "problems": problems},
              open(js_p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return rid, md, problems


def confirm(rid, owner_said):
    md_p, js_p, c_p, _ = _paths(rid)
    if not os.path.exists(js_p): return False, f"no read-back {rid}"
    meta = json.load(open(js_p, encoding="utf-8"))
    if meta.get("problems"): return False, "read-back has problems; fix them and make a new read-back: " + "; ".join(meta["problems"])
    if not (owner_said or "").strip(): return False, "--owner-said is required (quote the owner's yes)"
    json.dump({"id": rid, "confirmed_at": now().isoformat(), "owner_said": owner_said.strip()},
              open(c_p, "w", encoding="utf-8"), ensure_ascii=False)
    return True, f"confirmed {rid} for {meta['to']} ({meta.get('name')})"


def find_confirmed(to, brief_text):
    """Return the id of a confirmed, unused, fresh read-back for this number + brief, else None."""
    to, h = norm_number(to), brief_hash(brief_text)
    if not os.path.isdir(RB_DIR): return None
    for f in sorted(os.listdir(RB_DIR), reverse=True):
        if not f.endswith(".json"): continue
        rid = f[:-5]; _, js_p, c_p, u_p = _paths(rid)
        if not os.path.exists(c_p) or os.path.exists(u_p): continue
        try:
            meta = json.load(open(js_p, encoding="utf-8")); conf = json.load(open(c_p, encoding="utf-8"))
            age = (now() - datetime.datetime.fromisoformat(conf["confirmed_at"])).total_seconds()
        except Exception:
            continue
        if meta.get("to") == to and meta.get("brief_hash") == h and 0 <= age <= MAX_AGE_SECS and not meta.get("problems"):
            return rid
    return None


def check(to, brief_text, name="", purpose="", voice="eve"):
    """(rc, message, rid). rc 0 may dial | 6 read-back needed | 7 sensitive line in brief."""
    if is_exempt(to): return 0, "exempt", None
    _, sensitive = clean_brief(brief_text)
    if sensitive:
        rid, md, _ = write_readback(to, brief_text, name, purpose, voice)
        return 7, f"REFUSED: brief has {sensitive} sensitive line(s); remove them. Read-back: {_paths(rid)[0]}", rid
    rid = find_confirmed(to, brief_text)
    if rid: return 0, f"confirmed by the owner (read-back {rid})", rid
    rid, md, problems = write_readback(to, brief_text, name, purpose, voice)
    return 6, ("READBACK REQUIRED - nothing dialled. Show the owner " + _paths(rid)[0] +
               " in chat; after their yes run: call_guard.py confirm " + rid + " --owner-said \"...\" and re-run the same dial."
               + (" PROBLEMS: " + "; ".join(problems) if problems else "")), rid


def consume(rid):
    _, _, c_p, u_p = _paths(rid)
    if os.path.exists(c_p): open(u_p, "w").write(now().isoformat() + "\n"); return True
    return False


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    for n in ("readback", "check"):
        p = sp.add_parser(n); p.add_argument("--to", required=True); p.add_argument("--brief-file", required=True)
        p.add_argument("--name", default=""); p.add_argument("--purpose", default=""); p.add_argument("--voice", default="eve")
    p = sp.add_parser("confirm"); p.add_argument("id"); p.add_argument("--owner-said", default="")
    p = sp.add_parser("consume"); p.add_argument("id")
    a = ap.parse_args()
    if a.cmd in ("readback", "check"):
        text = open(a.brief_file, encoding="utf-8").read()
        if a.cmd == "readback":
            rid, md, problems = write_readback(a.to, text, a.name, a.purpose, a.voice)
            print(md); print(f"READBACK FILE: {_paths(rid)[0]}")
            return 7 if any(p.startswith("PRIVATE") for p in problems) else 0
        rc, msg, rid = check(a.to, text, a.name, a.purpose, a.voice)
        print(f"GUARD: {msg}")
        if rid: print(f"GUARD_ID: {rid}")
        if rc == 6: print(open(_paths(rid)[0], encoding="utf-8").read())
        return rc
    if a.cmd == "confirm":
        ok, msg = confirm(a.id, a.owner_said); print(msg); return 0 if ok else 4
    if a.cmd == "consume":
        return 0 if consume(a.id) else 4


if __name__ == "__main__":
    sys.exit(main())
