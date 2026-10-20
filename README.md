# grokbot-phone-caller

A free phone-call template for Grok Bots. Your bot can ring people for you (book a table, check opening hours, pass on a message) in a natural AI voice, and then report back what was said.

## How it works

```
their phone <-> Telnyx <-> free Cloudflare tunnel <-> server.py on the bot's computer <-> xAI realtime voice
```

- **Telnyx** carries the phone call and streams the call audio.
- **xAI realtime voice** does the talking. Audio is passed between Telnyx and xAI by `server.py`, which runs on the bot's computer only for the length of the call.
- **A free Cloudflare quick tunnel** gives the bot's computer a temporary public address, so Telnyx can reach it.
- **`dial.sh`** places each call. It checks the read-back, starts the server and tunnel, tests the whole path, dials, waits for the hang-up, writes an outcome note, and stops everything.

## Bring your own keys

Each user uses their own accounts. Calls are billed by Telnyx and xAI. Keep these in the bot's masked secret box as environment variables, never in files or chat:

- `TELNYX_API_KEY`: Telnyx API key
- `TELNYX_NUMBER`: your Telnyx number, e.g. `+15550100000`
- `XAI_API_KEY`: xAI API key with credit

## Quick start

```bash
pip install -r requirements.txt   # setup.py also makes a venv for you
python3 setup.py --from "$TELNYX_NUMBER" --owner "Your name" --voice eve --voice-name Eve --countries US
bash dial.sh --path-check         # checks everything, never dials
bash dial.sh --name="Who" --purpose="Why" +15550100000 "brief text or brief file"
```

**Read-back before dialling:** with no confirmed read-back, nothing is dialled. dial.sh prints a read-back and exits 6. The bot shows it to its owner, records their yes with `python3 call_guard.py confirm <ID> --owner-said "..."`, and runs the same command again.

Exit codes: 0 connected | 1 phone path down | 2 not answered | 3 failed after retry | 4 usage/setup error | 6 read-back needed | 7 card number or password in the brief.

## Known limitation

The Grok Bot computer can go to sleep between turns. If that happens during a call, the server and tunnel stop and the call drops. This is a Grok Bot limitation, not a fault in these scripts. dial.sh stays in the foreground until the call ends, so place calls in a turn that can wait for it to finish.

See [TROUBLESHOOTING.md](TROUBLESHOOTING.md) for common problems and fixes.

## License

MIT
