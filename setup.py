#!/usr/bin/env python3
"""setup.py - one-time setup for Phone Caller (run from the folder holding these scripts).

  python3 setup.py --from +E164 --owner "Your name" [--voice eve] [--voice-name Eve] [--countries US,GB]

Checks XAI_API_KEY and TELNYX_API_KEY (from the environment), checks the caller number is in the Telnyx
account, creates an outbound voice profile and a Call Control app named phone-caller (field names checked
against the Telnyx API schema, Oct 2026), makes a Python venv with aiohttp/websockets, fetches cloudflared
if missing, and writes config.env. Safe to re-run: reuses an existing phone-caller profile and app.
"""
import argparse, json, os, platform, shutil, subprocess, sys, urllib.error, urllib.parse, urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))

def die(msg, rc=4): print("SETUP FAILED: " + msg); sys.exit(rc)

def key(name):
    v = os.environ.get(name, "").strip()
    if not v: die(f"{name} is not set. Add it in the bot's secret box (masked), then run setup again.")
    return v

def http(method, url, k, body=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, method=method,
                                 headers={"Authorization": "Bearer " + k, "Content-Type": "application/json", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r: return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e: return e.code, {"body": e.read().decode()[:400]}
    except Exception as e: return 0, {"error": str(e)[:300]}

def tx(method, path, body=None): return http(method, "https://api.telnyx.com" + path, key("TELNYX_API_KEY"), body)

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--from", dest="frm", required=True); ap.add_argument("--owner", required=True)
    ap.add_argument("--voice", default="eve"); ap.add_argument("--voice-name", default="")
    ap.add_argument("--countries", default="", help="alpha-2 codes you will call, e.g. US,GB,AU")
    a = ap.parse_args()
    frm = a.frm.replace(" ", "")
    if not (frm.startswith("+") and frm[1:].isdigit()): die("--from must be +countrycode..., e.g. +15551234567")

    st, _ = http("GET", "https://api.x.ai/v1/models", key("XAI_API_KEY"))
    if st != 200: die(f"xAI did not accept XAI_API_KEY (HTTP {st}). Check the key and that the account has credit.")
    print("xAI key OK.")
    st, bal = tx("GET", "/v2/balance")
    if st != 200: die(f"Telnyx did not accept TELNYX_API_KEY (HTTP {st}).")
    b = bal.get("data") or {}; print(f"Telnyx key OK. Balance {b.get('balance')} {b.get('currency')}.")
    st, nums = tx("GET", "/v2/phone_numbers?" + urllib.parse.urlencode({"filter[phone_number]": frm}))
    num = ((nums.get("data") or [None])[0]) if st == 200 else None
    if not num: die(f"{frm} is not a number in this Telnyx account. Buy one with Voice (Numbers > Search & Buy), then re-run.")
    print(f"Number {frm} found in the account.")

    # Outbound voice profile (reuse by name)
    st, lst = tx("GET", "/v2/outbound_voice_profiles?" + urllib.parse.urlencode({"filter[name][contains]": "phone-caller"}))
    ovp = next((p for p in (lst.get("data") or []) if p.get("name") == "phone-caller"), None) if st == 200 else None
    countries = [c.strip().upper() for c in a.countries.split(",") if c.strip()]
    if not ovp:
        body = {"name": "phone-caller", "traffic_type": "conversational", "service_plan": "global", "enabled": True,
                "daily_spend_limit": "5.00", "daily_spend_limit_enabled": True}
        if countries: body["whitelisted_destinations"] = countries
        st, r = tx("POST", "/v2/outbound_voice_profiles", body)
        if st >= 300: die(f"could not create the outbound voice profile (HTTP {st}): {json.dumps(r)[:300]}", 3)
        ovp = r.get("data") or {}
        print(f"Created outbound voice profile {ovp.get('id')} (daily spend cap US$5; change it in the Telnyx portal).")
    else:
        print(f"Reusing outbound voice profile {ovp.get('id')}.")
    # Call Control app (reuse by name). Its own webhook is a placeholder: dial.sh sets webhook_url per call.
    st, lst = tx("GET", "/v2/call_control_applications?" + urllib.parse.urlencode({"filter[application_name][contains]": "phone-caller"}))
    app = next((x for x in (lst.get("data") or []) if x.get("application_name") == "phone-caller"), None) if st == 200 else None
    if not app:
        st, r = tx("POST", "/v2/call_control_applications", {"application_name": "phone-caller",
                   "webhook_event_url": "https://example.com/phone-caller-set-per-call", "webhook_api_version": "2",
                   "active": True, "outbound": {"outbound_voice_profile_id": ovp.get("id")}})
        if st >= 300: die(f"could not create the Call Control app (HTTP {st}): {json.dumps(r)[:300]}", 3)
        app = r.get("data") or {}
        print(f"Created Call Control app {app.get('id')}.")
    else:
        print(f"Reusing Call Control app {app.get('id')}.")
    if not num.get("connection_id"):
        st, _ = tx("PATCH", f"/v2/phone_numbers/{num['id']}", {"connection_id": app.get("id")})
        print("Linked the number to the app." if st < 300 else f"Note: could not link the number to the app (HTTP {st}); outbound calls may still work.")

    # Python venv + packages
    venv = os.path.join(ROOT, ".venv")
    if not os.path.exists(os.path.join(venv, "bin", "python")):
        subprocess.run([sys.executable, "-m", "venv", venv], check=False)
    py = os.path.join(venv, "bin", "python")
    if not os.path.exists(py): die("could not create a Python venv (python3 -m venv). Install python3-venv and re-run.")
    r = subprocess.run([py, "-m", "pip", "install", "--quiet", "-r", os.path.join(ROOT, "requirements.txt")])
    if r.returncode: die("pip install failed (aiohttp, websockets, pynacl).")
    print("Python packages installed.")
    # cloudflared (quick tunnel, no account)
    cf = shutil.which("cloudflared") or os.path.join(ROOT, "bin", "cloudflared")
    if not os.path.exists(cf):
        if sys.platform == "darwin": die("install cloudflared first (Mac: brew install cloudflared), then re-run.")
        arch = {"x86_64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine(), "amd64")
        url = f"https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-{arch}"
        os.makedirs(os.path.dirname(cf), exist_ok=True)
        print(f"Downloading cloudflared ({arch})...")
        urllib.request.urlretrieve(url, cf); os.chmod(cf, 0o755)
    print("cloudflared ready.")

    vn = a.voice_name or a.voice.title()
    with open(os.path.join(ROOT, "config.env"), "w") as f:
        f.write(f'FROM="{frm}"\nCONN="{app.get("id")}"\nOWNER_NAME="{a.owner}"\nVOICE_NAME="{vn}"\n'
                f'XAI_VOICE="{a.voice}"\nPORT="8790"\nCLOUDFLARED="{cf}"\n')
    for f in ("dial.sh",): os.chmod(os.path.join(ROOT, f), 0o755)
    print(f"Saved config.env. Setup done. Next: a dry run (dial.sh --dry-run ...) then a rehearsal call to your own mobile.")

if __name__ == "__main__":
    main()
