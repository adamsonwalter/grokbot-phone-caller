#!/usr/bin/env python3
"""Phone Caller call server: Telnyx media stream (L16 @ 16k) <-> xAI realtime voice (grok-voice-latest).

Generalised from a working private bridge (Oct 2026). Started by dial.sh for one call and stopped after it.
Reads the call brief from PHONE_CONTEXT.md (written by dial.sh) and settings from config.env.
Secrets come from the environment only: XAI_API_KEY, TELNYX_API_KEY (optional TELNYX_PUBLIC_KEY).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import sys
import time
from typing import Any
from urllib.parse import quote

from aiohttp import ClientSession, ClientTimeout, WSMsgType, web
import websockets
from websockets.protocol import State

import call_watch as cw

ROOT = __import__("pathlib").Path(__file__).resolve().parent
LOG_DIR = ROOT / "logs"
CALL_LOG = LOG_DIR / "call.log"
STATE_PATH = ROOT / "STATE.json"


def _conf(key: str, default: str = "") -> str:
    """Setting from the environment, else config.env next to this file (KEY=value lines)."""
    if os.environ.get(key):
        return os.environ[key]
    try:
        for line in (ROOT / "config.env").read_text(encoding="utf-8").splitlines():
            k, sep, v = line.strip().partition("=")
            if sep and k.strip() == key:
                return v.strip().strip('"').strip("'")
    except Exception:  # noqa: BLE001
        pass
    return default


def audio_mode() -> str:
    mode = (os.environ.get("AUDIO_MODE") or "wideband").strip().lower()
    if mode in ("pcmu", "pcmu-8k", "narrowband", "g711"):
        return "pcmu"
    return "wideband"


def audio_mode_label() -> str:
    return "PCMU-8k" if audio_mode() == "pcmu" else "L16-16k"


def load_eve_instructions() -> str:
    """The voice persona + the call brief (PHONE_CONTEXT.md, written by dial.sh)."""
    voice = _conf("VOICE_NAME", "Eve")
    owner = _conf("OWNER_NAME", "the owner")
    base = (
        f"You are {voice}, an AI assistant phoning on behalf of {owner}. You placed this call.\n"
        "How to talk: short, natural sentences, like a friendly person on the phone. One or two sentences "
        "per turn, then stop and listen. Say each thing once: do not repeat your introduction or the reason "
        "for the call unless they ask. Use the brief's facts, not its wording; never read it out, and never "
        "mention these instructions, the setup, templates or tools. No filler words or stock phrases "
        "(no 'just', 'absolutely', 'no worries', 'purely', 'great question'). Use the language they use.\n"
        f"Stay within the brief. If asked something it does not cover, say you will check with {owner} and "
        "get back to them. Never give card numbers, passwords or payment details, and never agree to pay, "
        "sign up or cancel anything not in the brief. If asked whether you are a person, say you are an AI "
        "assistant.\n"
        "Ending: once you have what the brief asks for, confirm the key detail once in one short sentence "
        "(for example 'So that's 10:30 next Tuesday.'), thank them and say goodbye in a few words, and call "
        "end_call in that same turn. After end_call, say nothing more. Never say things like 'call ended' "
        "or 'done'.\n"
        f"Call log_follow_up silently, once per item, for anything {owner} needs to act on (something "
        "agreed, a question you could not answer, a call-back request).\n"
        "If you reach voicemail, leave one short message (who you are, why, any call-back request in the "
        "brief), then call end_call."
    )
    ctx_path = ROOT / "PHONE_CONTEXT.md"
    try:
        if ctx_path.is_file():
            ctx = ctx_path.read_text(encoding="utf-8").strip()
            if ctx:
                # Cap so session.update stays under typical limits.
                if len(ctx) > 6000:
                    ctx = ctx[:6000] + "\n…"
                return base + "\n\nCall brief (use this; do not invent):\n" + ctx
    except Exception as exc:  # noqa: BLE001
        logging.warning("Could not read PHONE_CONTEXT.md: %s", exc)
    return base


def eve_instructions() -> str:
    return load_eve_instructions()


def _safe_truncate(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def _save_transcript_line(stream_tag: Any, who: str, text: Any) -> None:
    """Keep every spoken line of an Eve call, in full, in logs/transcripts/ (one file per call)."""
    cw.save_transcript_line(str(stream_tag), who, str(text or ""))



def desk_tool_schemas() -> list[dict[str, Any]]:
    """Only the two call tools from the original: log_follow_up and end_call."""
    return [
        {
            "type": "function",
            "name": "log_follow_up",
            "description": (
                "Log one question or request from this call that needs a follow-up after the call. "
                "Call once per item, after you have repeated it back to the other person."
            ),
            "parameters": {
                "type": "object",
                "properties": {"item": {"type": "string", "description": "The follow-up, in one short sentence"}},
                "required": ["item"],
                "additionalProperties": False,
            },
        },
        {
            "type": "function",
            "name": "end_call",
            "description": "End the phone call. Call this right after your final goodbye, once the conversation is finished.",
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        },
    ]


def _tool_log_follow_up(args: dict[str, Any]) -> str:
    item = str(args.get("item") or "").strip()
    return "Noted for follow-up." if item else "Error: item is empty"


TOOL_HANDLERS = {
    "log_follow_up": _tool_log_follow_up,
    "end_call": lambda _a: "Ending the call now.",
}


def execute_desk_tool(name: str, arguments: dict[str, Any] | str | None) -> str:
    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        return f"Error: unknown tool {name!r}"
    args: dict[str, Any]
    if arguments is None:
        args = {}
    elif isinstance(arguments, str):
        try:
            args = json.loads(arguments) if arguments.strip() else {}
        except json.JSONDecodeError:
            return f"Error: invalid JSON arguments for {name}"
    elif isinstance(arguments, dict):
        args = arguments
    else:
        args = {}
    try:
        return str(handler(args))
    except Exception as exc:  # noqa: BLE001
        logging.exception("desk tool %s failed: %s", name, exc)
        return f"Error executing {name}: {exc}"


XAI_REALTIME_URL = "wss://api.x.ai/v1/realtime?model=grok-voice-latest"
TELNYX_API_BASE = "https://api.telnyx.com/v2"
WEBHOOK_TOLERANCE_SEC = 300

XAI_REALTIME_URL = _conf("XAI_REALTIME_URL", XAI_REALTIME_URL)

# Noisy xAI event types — skip append_call_log on the hot path.
_XAI_SKIP_CALL_LOG = frozenset(
    {
        "response.output_audio.delta",
        "response.audio.delta",
        "response.output_audio_transcript.delta",
        "ping",
    }
)

_pubkey_todo_logged = False


def setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(LOG_DIR / "server.log"),
        ],
    )


def append_call_log(line: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with CALL_LOG.open("a", encoding="utf-8") as f:
        f.write(line.rstrip() + "\n")


def _https_to_wss(url: str) -> str:
    url = url.rstrip("/")
    if url.startswith("https://"):
        return "wss://" + url[len("https://") :]
    if url.startswith("http://"):
        return "ws://" + url[len("http://") :]
    if url.startswith("wss://") or url.startswith("ws://"):
        return url
    return "wss://" + url


def public_base_url() -> str | None:
    for key in ("PUBLIC_URL", "PUBLIC_HOST"):
        val = (os.environ.get(key) or "").strip().rstrip("/")
        if val:
            if val.startswith("http://") or val.startswith("https://"):
                return val
            return "https://" + val
    try:
        if STATE_PATH.is_file():
            data = json.loads(STATE_PATH.read_text())
            url = (data.get("public_url") or "").strip().rstrip("/")
            if url:
                return url
    except Exception as exc:  # noqa: BLE001
        logging.warning("Could not read STATE.json: %s", exc)
    return None


def media_wss_url() -> str | None:
    env = (os.environ.get("MEDIA_WSS_URL") or "").strip()
    if env:
        return env.rstrip("/")
    base = public_base_url()
    if not base:
        return None
    return _https_to_wss(base) + "/telnyx/media"


def verify_telnyx_ed25519(
    body: bytes,
    signature_b64: str | None,
    timestamp: str | None,
    public_key_b64: str,
) -> bool:
    """Verify Telnyx ed25519 webhook: signed payload is `{timestamp}|{raw_body}`."""
    if not signature_b64 or not timestamp or not public_key_b64:
        return False
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    if abs(int(time.time()) - ts) > WEBHOOK_TOLERANCE_SEC:
        logging.warning("Telnyx webhook timestamp outside tolerance: %s", timestamp)
        return False
    try:
        from nacl.exceptions import BadSignatureError
        from nacl.signing import VerifyKey
    except ImportError:
        logging.warning("PyNaCl not installed; cannot verify Telnyx signature")
        return False
    try:
        pk_raw = base64.b64decode(public_key_b64)
        sig_raw = base64.b64decode(signature_b64)
        signed = timestamp.encode("utf-8") + b"|" + body
        VerifyKey(pk_raw).verify(signed, sig_raw)
        return True
    except (BadSignatureError, ValueError, Exception) as exc:  # noqa: BLE001
        logging.warning("Telnyx signature verify failed: %s", exc)
        return False


def session_update_payload() -> dict[str, Any]:
    """xAI session.update: Eve voice, tight VAD, PCM 16k, desk tools, no transcription."""
    return {
        "type": "session.update",
        "session": {
            "voice": _conf("XAI_VOICE", "eve"),
            "instructions": eve_instructions(),
            "turn_detection": {
                "type": "server_vad",
                "threshold": float(_conf("VAD_THRESHOLD", "0.5")),
                # 19 Oct 2026: 200 ms (original) answered on the smallest pause; 450 ms default now.
                "silence_duration_ms": int(_conf("VAD_SILENCE_MS", "450")),
                "prefix_padding_ms": 300,
            },
            "tools": desk_tool_schemas(),
            "tool_choice": "auto",
            "audio": {
                "input": {"format": {"type": "audio/pcm", "rate": 16000}},
                "output": {"format": {"type": "audio/pcm", "rate": 16000}},
            },
        },
    }


def _ws_is_open(ws: Any) -> bool:
    try:
        return getattr(ws, "state", None) is State.OPEN
    except Exception:  # noqa: BLE001
        return False


async def _connect_xai_ws() -> Any:
    api_key = os.environ.get("XAI_API_KEY")
    if not api_key:
        raise RuntimeError("XAI_API_KEY missing")
    return await websockets.connect(
        XAI_REALTIME_URL,
        additional_headers={"Authorization": f"Bearer {api_key}"},
        ping_interval=20,
        ping_timeout=20,
        max_size=8 * 1024 * 1024,
    )


async def prewarm_xai(call_control_id: str) -> None:
    """Open xAI realtime early on call.initiated; reuse on media start."""
    ccid = str(call_control_id)
    if not ccid:
        return
    if ccid in PENDING_XAI or ccid in BRIDGES_BY_CALL:
        return
    if not os.environ.get("XAI_API_KEY"):
        logging.error("XAI_API_KEY missing; cannot pre-warm realtime session")
        append_call_log(f"{time.time():.0f} ERROR missing_xai_api_key prewarm ccid={ccid}")
        return
    append_call_log(f"{time.time():.0f} XAI prewarm start call_control_id={ccid}")
    logging.info("xAI pre-warm start call_control_id=%s", ccid)
    try:
        ws = await _connect_xai_ws()
        await ws.send(json.dumps(session_update_payload()))
        # If hangup raced us, discard immediately.
        if ccid in BRIDGES_BY_CALL:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass
            return
        existing = PENDING_XAI.pop(ccid, None)
        if existing is not None:
            try:
                await existing.close()
            except Exception:  # noqa: BLE001
                pass
        PENDING_XAI[ccid] = ws
        append_call_log(f"{time.time():.0f} XAI prewarm ready call_control_id={ccid}")
        logging.info("xAI pre-warm ready call_control_id=%s", ccid)
    except Exception as exc:  # noqa: BLE001
        logging.exception("xAI pre-warm failed call_control_id=%s: %s", ccid, exc)
        append_call_log(
            f"{time.time():.0f} ERROR xai_prewarm_failed call_control_id={ccid} err={exc!r}"
        )


async def discard_pending_xai(call_control_id: str, reason: str = "hangup") -> None:
    ccid = str(call_control_id)
    ws = PENDING_XAI.pop(ccid, None)
    if ws is None:
        return
    append_call_log(
        f"{time.time():.0f} XAI prewarm discard call_control_id={ccid} reason={reason}"
    )
    logging.info("xAI pre-warm discard call_control_id=%s reason=%s", ccid, reason)
    try:
        await ws.close()
    except Exception:  # noqa: BLE001
        pass


XAI_PCM_RATE = 16000  # session_update_payload() audio in/out rate
VOICE_LABEL = _conf("VOICE_NAME", "Eve")  # transcript label, read by call_outcome.py


class _Rate2x:
    """2:1 PCM16-LE rate guard for when Telnyx negotiates L16 @ 8k but xAI runs 16k.

    Added 2026-10-07: a dial without stream_bidirectional_sampling_rate defaults to
    8000 Hz; passing 16k audio straight through then plays at half speed (slow,
    deep, "under water"). Down: average pairs 16k->8k. Up: linear interp 8k->16k.
    """

    def __init__(self) -> None:
        self._down_carry = b""
        self._up_prev: int | None = None
        self._up_carry = b""

    def down(self, b64: str) -> str:
        import array
        data = self._down_carry + base64.b64decode(b64)
        n = (len(data) // 4) * 4  # whole sample pairs
        self._down_carry = data[n:]
        if n == 0:
            return ""
        a = array.array("h")
        a.frombytes(data[:n])
        if sys.byteorder != "little":
            a.byteswap()
        out = array.array("h", ((a[i] + a[i + 1]) // 2 for i in range(0, len(a), 2)))
        if sys.byteorder != "little":
            out.byteswap()
        return base64.b64encode(out.tobytes()).decode("ascii")

    def up(self, b64: str) -> str:
        import array
        data = self._up_carry + base64.b64decode(b64)
        n = (len(data) // 2) * 2
        self._up_carry = data[n:]
        if n == 0:
            return ""
        a = array.array("h")
        a.frombytes(data[:n])
        if sys.byteorder != "little":
            a.byteswap()
        out = array.array("h")
        prev = self._up_prev if self._up_prev is not None else a[0]
        for x in a:
            out.append((prev + x) // 2)
            out.append(x)
            prev = x
        self._up_prev = prev
        if sys.byteorder != "little":
            out.byteswap()
        return base64.b64encode(out.tobytes()).decode("ascii")


class Bridge:
    """One Telnyx media WS ↔ one xAI realtime WS."""

    def __init__(self, telnyx_ws: web.WebSocketResponse) -> None:
        self.telnyx_ws = telnyx_ws
        self.xai_ws: Any = None
        self.stream_id: str | None = None
        self.call_control_id: str | None = None
        self.closed = False
        self._xai_task: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        # Parallel tool calls: buffer outputs; flush response.create after response.done.
        self._tool_response_needed: bool = False
        self._response_done_seen: bool = False
        self._handled_call_ids: set[str] = set()
        # Set in _on_start when Telnyx media rate != xAI rate (e.g. L16 @ 8k).
        self._resampler: _Rate2x | None = None
        # Call watch (8 Oct 2026): auto hang-up, stall nudge, ping with LOW TEXT check.
        self._watch = cw.CallWatch()
        self._watch_task: asyncio.Task[None] | None = None
        self._brief = cw.read_phone_context()
        self._callee = cw.callee_first_name(self._brief)
        self._said: list[str] = []
        self._heard: list[str] = []
        self._follow_ups = cw.FollowUps()
        self._hangup_reason: str | None = None
        self._started_at = time.time()
        # fix5: a 'hello' on pickup must not cut off Eve's opening line.
        self._guard = cw.OpeningGuard()
        # fix8 (9 Oct 2026): on voicemail, hold Eve's message until the beep has played.
        self._vm = cw.VoicemailGate()
        self._resp_audio: list[str] = []   # Eve's audio for the current reply (replayed after the beep)
        self._held_audio: list[str] = []
        # 19 Oct 2026 (rehearsal fixes): after end_call, no further replies are played; a dropped and
        # re-opened media link for the same call re-attaches to this bridge instead of starting over.
        self._cur_resp: str | None = None
        self._end_resp: str | None = None
        self._muted: set[str] = set()
        self._detached = False
        self._detach_task: asyncio.Task[None] | None = None
        self.handoff: Any = None

    async def handle_telnyx_frame(self, raw: str) -> None:
        try:
            msg: dict[str, Any] = json.loads(raw)
        except json.JSONDecodeError:
            append_call_log(f"{time.time():.0f} TELNYX_WS non_json")
            return
        event = msg.get("event") or msg.get("event_type") or "unknown"
        stream_id = msg.get("stream_id")
        if event != "media":
            extra = f" stream_id={stream_id}" if stream_id else ""
            append_call_log(f"{time.time():.0f} TELNYX_WS event={event}{extra}")
            logging.info("telnyx media event=%s stream_id=%s", event, stream_id)

        if event == "connected":
            return
        if event == "start":
            await self._on_start(msg)
            return
        if event == "media":
            await self._on_inbound_media(msg)
            return
        if event in ("stop", "error"):
            if event == "error":
                logging.warning("telnyx media error payload=%s", msg.get("payload"))
            await self.close(reason=event)
            return

    async def _on_start(self, msg: dict[str, Any]) -> None:
        start = msg.get("start") or {}
        self.stream_id = str(msg.get("stream_id") or start.get("stream_id") or "")
        self.call_control_id = str(start.get("call_control_id") or "")
        media_format = start.get("media_format") or {}
        append_call_log(
            f"{time.time():.0f} START stream_id={self.stream_id} "
            f"call_control_id={self.call_control_id} "
            f"encoding={media_format.get('encoding')} "
            f"rate={media_format.get('sample_rate')}"
        )
        try:
            tel_rate = int(media_format.get("sample_rate") or 0)
        except (TypeError, ValueError):
            tel_rate = 0
        tel_enc = str(media_format.get("encoding") or "").upper()
        self._brief = cw.read_phone_context() or self._brief
        self._callee = cw.callee_first_name(self._brief)
        self._watch.bytes_per_sec = (tel_rate or XAI_PCM_RATE) * (2 if tel_enc in ("L16", "") else 1)
        if tel_enc == "L16" and tel_rate == XAI_PCM_RATE // 2:
            self._resampler = _Rate2x()
            append_call_log(
                f"{time.time():.0f} RATE_GUARD on stream_id={self.stream_id} "
                f"telnyx=L16@{tel_rate} xai=pcm@{XAI_PCM_RATE} (dial missing "
                f"stream_bidirectional_sampling_rate=16000?)"
            )
            logging.warning(
                "rate guard: Telnyx L16@%s vs xAI %s; resampling 2:1 stream_id=%s",
                tel_rate, XAI_PCM_RATE, self.stream_id,
            )
        elif tel_enc == "L16" and tel_rate and tel_rate != XAI_PCM_RATE:
            logging.warning(
                "rate mismatch NOT handled: Telnyx L16@%s vs xAI %s stream_id=%s",
                tel_rate, XAI_PCM_RATE, self.stream_id,
            )
        old = DETACHED.pop(self.call_control_id, None) if self.call_control_id else None
        if old is not None and not old.closed and old.xai_ws is not None and _ws_is_open(old.xai_ws):
            old.reattach(self.telnyx_ws, self.stream_id)
            self.handoff = old
            return
        if self.stream_id:
            BRIDGES_BY_STREAM[self.stream_id] = self
        if self.call_control_id:
            BRIDGES_BY_CALL[self.call_control_id] = self
            # Dial-time stream_url already opened media — skip answered fallback.
            STREAM_STARTED.add(self.call_control_id)
        await self._connect_xai()

    async def _connect_xai(self) -> None:
        api_key = os.environ.get("XAI_API_KEY")
        if not api_key:
            logging.error("XAI_API_KEY missing; cannot open realtime session")
            append_call_log(f"{time.time():.0f} ERROR missing_xai_api_key")
            return
        tag = self.stream_id or self.call_control_id or "?"
        reused = False
        pending = None
        if self.call_control_id:
            pending = PENDING_XAI.pop(self.call_control_id, None)
        if pending is not None and _ws_is_open(pending):
            self.xai_ws = pending
            reused = True
            append_call_log(
                f"{time.time():.0f} XAI reuse_prewarm stream_id={tag} "
                f"call_control_id={self.call_control_id}"
            )
            logging.info("xAI reuse pre-warm stream_id=%s", tag)
            # Fix 13 Oct: always re-send session.update at answer time so the reused session
            # carries PHONE_CONTEXT.md as it is NOW, not as it was at prewarm.
            await self.xai_ws.send(json.dumps(session_update_payload()))
            append_call_log(f"{time.time():.0f} SENT session.update fresh_at_answer stream_id={tag}")
        else:
            if pending is not None:
                try:
                    await pending.close()
                except Exception:  # noqa: BLE001
                    pass
            append_call_log(
                f"{time.time():.0f} XAI connect stream_id={tag} url={XAI_REALTIME_URL}"
            )
            try:
                self.xai_ws = await _connect_xai_ws()
            except Exception as exc:  # noqa: BLE001
                logging.exception("xAI realtime connect failed: %s", exc)
                append_call_log(f"{time.time():.0f} ERROR xai_connect_failed err={exc!r}")
                return
            await self.xai_ws.send(json.dumps(session_update_payload()))
            append_call_log(f"{time.time():.0f} SENT session.update stream_id={tag}")

        # Greeting only after media is up (pre-warm already sent session.update).
        self._guard.start(time.time())
        # Fix 12 (12 Oct 2026): one-turn instruction to read the whole brief, no tools first.
        _open = {"type": "response.create"}
        _ctx_now = cw.read_phone_context()
        if cw.has_message_header(_ctx_now):
            _msg = cw.brief_message(_ctx_now)[:4000]
            _open["response"] = {"instructions": (
                "Open the call now in at most two short sentences: a hello (use their first name if you "
                "know it), who you are (an AI assistant calling for " + _conf("OWNER_NAME", "the owner")
                + ") and why you are calling, using the brief's facts in your own words. Then ask your "
                "first question and stop to listen. Do not call any tool, do not read the brief out, and "
                "do not ask whether they have questions.\n\nBRIEF:\n" + _msg
            )}
        await self.xai_ws.send(json.dumps(_open))
        append_call_log(
            f"{time.time():.0f} SENT response.create stream_id={tag} reused={reused} brief_open={'response' in _open}"
        )
        self._xai_task = asyncio.create_task(self._xai_recv_loop(), name=f"xai-recv-{tag}")
        if self._watch_task is None:
            self._watch_task = asyncio.create_task(self._watch_loop(), name=f"xai-watch-{tag}")

    async def _handle_function_call(self, name: str, call_id: str, arguments: Any) -> None:
        """Execute desk tool and send function_call_output.

        xAI: response.function_call_arguments.done → conversation.item.create
        (function_call_output) → response.create (after all parallel outputs / response.done).
        """
        tag = self.stream_id or self.call_control_id or "?"
        if not self.xai_ws or self.closed:
            return
        if call_id in self._handled_call_ids:
            return
        self._handled_call_ids.add(call_id)
        result = execute_desk_tool(name, arguments)
        if name == "end_call":
            self._end_resp = self._cur_resp
            self._watch.request_end(time.time())
            append_call_log(f"{time.time():.0f} END_CALL requested stream_id={tag}")
        elif name == "log_follow_up" and not result.startswith("Error"):
            try:
                args = json.loads(arguments) if isinstance(arguments, str) else (arguments or {})
            except Exception:  # noqa: BLE001
                args = {}
            item = args.get("item") or args.get("message")
            if item:
                self._log_follow_ups(self._fu().voice_item(
                    str(item), time.time(), source="noted by the voice"))
        append_call_log(
            f"{time.time():.0f} TOOL name={name} call_id={call_id} "
            f"stream_id={tag} result={_safe_truncate(result, 400)!r}"
        )
        logging.info("desk tool %s stream_id=%s result_len=%s", name, tag, len(result))
        self._tool_response_needed = True
        try:
            await self.xai_ws.send(
                json.dumps(
                    {
                        "type": "conversation.item.create",
                        "item": {
                            "type": "function_call_output",
                            "call_id": call_id,
                            "output": result,
                        },
                    }
                )
            )
        except Exception as exc:  # noqa: BLE001
            logging.warning("failed to send function_call_output: %s", exc)
            append_call_log(
                f"{time.time():.0f} ERROR tool_output_send name={name} err={exc!r}"
            )
        # If response.done already arrived, continue now; else wait for response.done.
        await self._flush_tool_response_create()

    async def _flush_tool_response_create(self) -> None:
        # Wait until the model turn finished (response.done) so parallel tool args can land.
        if not self._tool_response_needed or not getattr(self, "_response_done_seen", False):
            return
        if not self.xai_ws or self.closed:
            return
        self._tool_response_needed = False
        self._response_done_seen = False
        tag = self.stream_id or self.call_control_id or "?"
        if self._watch.end_requested_at:
            # 19 Oct 2026: asking for a reply after end_call made the voice say "Call ended." / "Done."
            append_call_log(f"{time.time():.0f} NO response.create after end_call stream_id={tag}")
            return
        try:
            await self.xai_ws.send(json.dumps({"type": "response.create"}))
            self._watch.voice_busy(True, time.time())
            append_call_log(f"{time.time():.0f} SENT response.create after_tools stream_id={tag}")
        except Exception as exc:  # noqa: BLE001
            logging.warning("response.create after tools failed: %s", exc)

    async def _xai_recv_loop(self) -> None:
        tag = self.stream_id or self.call_control_id or "?"
        assert self.xai_ws is not None
        try:
            async for raw in self.xai_ws:
                if self.closed:
                    break
                try:
                    event: dict[str, Any] = json.loads(raw)
                except json.JSONDecodeError:
                    append_call_log(f"{time.time():.0f} XAI non_json stream_id={tag}")
                    continue
                etype = event.get("type", "unknown")
                rid = event.get("response_id") or ((event.get("response") or {}).get("id") if etype.startswith("response.") else None)
                if etype == "response.created" and rid:
                    if self._watch.end_requested_at and rid != self._end_resp:
                        # After end_call: a reply set off by "yeah"/"bye" or line noise is cancelled, never played.
                        self._muted.add(rid)
                        try:
                            await self.xai_ws.send(json.dumps({"type": "response.cancel"}))
                        except Exception:  # noqa: BLE001
                            pass
                        append_call_log(f"{time.time():.0f} MUTED reply after end_call stream_id={tag}")
                        continue
                    self._cur_resp = rid
                if rid and rid in self._muted:
                    continue
                if etype not in _XAI_SKIP_CALL_LOG:
                    extra = {
                        k: v
                        for k, v in event.items()
                        if k != "type"
                        and not (isinstance(v, str) and len(v) > 200)
                    }
                    append_call_log(f"{time.time():.0f} XAI event={etype} stream_id={tag}")
                    now = time.time()
                    text = str(event.get("transcript") or "").strip()
                    if etype == "conversation.item.input_audio_transcription.completed" and text:
                        self._heard.append(text)
                        # Timing comes from VAD speech_started/stopped; text only marks a reply.
                        self._watch.other_replied = self._watch.other_replied or self._watch.voice_started
                        _save_transcript_line(tag, self._callee, text)
                        append_call_log(f"{now:.0f} TEXT stream_id={tag} who={self._callee} text={cw.short(text)!r}")
                        if len(self._heard) <= 2 and cw.is_voicemail_greeting(text):
                            await self._start_voicemail_hold(now)
                        self._log_follow_ups(self._fu().caller_line(text, now))
                    elif etype == "response.output_audio_transcript.done" and text:
                        self._partial_said = ""
                        self._said.append(text)
                        self._watch.voice_text(text, now)
                        self._log_follow_ups(self._fu().voice_line(text, now))
                        _save_transcript_line(tag, VOICE_LABEL, text)
                        append_call_log(f"{now:.0f} TEXT stream_id={tag} who=Eve text={cw.short(text)!r}")
                    elif etype == "input_audio_buffer.speech_started":
                        self._watch.other_activity(now, speaking=True)
                    elif etype == "input_audio_buffer.speech_stopped":
                        self._watch.other_activity(now, speaking=False)
                        # fix22 (16 Oct 2026): speech heard but no transcript for it -> log CALLER_UNHEARD
                        _n = len(self._heard)
                        async def _chk_unheard(n0=_n, t0=now):
                            await asyncio.sleep(4.0)
                            if len(self._heard) == n0 and not self.closed:
                                self._unheard_events = getattr(self, "_unheard_events", 0) + 1
                                append_call_log(f"{time.time():.0f} CALLER_UNHEARD stream_id={tag} "
                                                f"speech_at={t0:.0f} no transcript")
                        asyncio.create_task(_chk_unheard())
                    elif etype == "response.created":
                        self._watch.voice_busy(True, now)
                        if not self._vm.holding:
                            self._resp_audio = []
                    elif etype == "response.done":
                        self._watch.voice_busy(False, now)
                        self._flush_partial_said(tag, now)
                    if etype in (
                        "error",
                        "session.updated",
                        "response.done",
                        "response.function_call_arguments.done",
                        "conversation.item.input_audio_transcription.completed",
                        "conversation.item.input_audio_transcription.updated",
                        "response.output_audio_transcript.done",
                    ):
                        logging.info("xAI %s stream_id=%s %s", etype, tag, extra)

                if etype in ("response.output_audio_transcript.delta", "response.audio_transcript.delta"):
                    self._partial_said = getattr(self, "_partial_said", "") + str(event.get("delta") or "")
                # Forward audio deltas immediately — do not buffer until response.done.
                if etype in ("response.output_audio.delta", "response.audio.delta"):
                    delta = event.get("delta") or event.get("audio")
                    if isinstance(delta, str) and delta:
                        await self._send_telnyx_media(delta)
                elif etype == "response.function_call_arguments.done":
                    name = str(event.get("name") or "")
                    call_id = str(event.get("call_id") or "")
                    arguments = event.get("arguments")
                    if name and call_id:
                        await self._handle_function_call(name, call_id, arguments)
                elif etype == "response.output_item.done":
                    item = event.get("item") or {}
                    if isinstance(item, dict) and item.get("type") == "function_call":
                        name = str(item.get("name") or "")
                        call_id = str(item.get("call_id") or "")
                        arguments = item.get("arguments")
                        # Prefer arguments.done when both fire; skip duplicate empty call_id.
                        if name and call_id:
                            await self._handle_function_call(name, call_id, arguments)
                elif etype == "response.done":
                    self._response_done_seen = True
                    await self._flush_tool_response_create()
                    self._handled_call_ids.clear()
                elif etype in ("call.ended", "session.ended"):
                    break
        except Exception as exc:  # noqa: BLE001
            if not self.closed:
                logging.exception("xAI recv loop failed stream_id=%s: %s", tag, exc)
                append_call_log(
                    f"{time.time():.0f} ERROR xai_recv_failed stream_id={tag} err={exc!r}"
                )
        finally:
            await self.close(reason="xai_recv_end")

    async def _on_inbound_media(self, msg: dict[str, Any]) -> None:
        media = msg.get("media") or {}
        track = (media.get("track") or "inbound").lower()
        # inbound_track only (preferred); ignore any outbound loopback if present.
        if track not in ("inbound", "inbound_track"):
            return
        payload = media.get("payload")
        if not payload or not self.xai_ws or self.closed:
            return
        if self._resampler is not None:
            payload = self._resampler.up(payload)
            if not payload:
                return
        if self._guard.active(time.time()):
            payload = self._guard.filter_b64(
                payload, time.time(), silence_byte=b"\xff" if audio_mode() == "pcmu" else b"\x00")
        elif self._vm.holding:
            # fix8: listen for the beep ourselves; Eve hears silence so the tone can't cut her off.
            now = time.time()
            self._vm.inbound(cw.pcm16_rms(payload) if audio_mode() != "pcmu" else 0, now)
            try:
                raw_len = len(base64.b64decode(payload))
            except Exception:  # noqa: BLE001
                raw_len = 0
            if raw_len:
                payload = base64.b64encode((b"\xff" if audio_mode() == "pcmu" else b"\x00") * raw_len).decode("ascii")
        try:
            await self.xai_ws.send(
                json.dumps({"type": "input_audio_buffer.append", "audio": payload})
            )
        except Exception as exc:  # noqa: BLE001
            logging.warning("xAI append failed: %s", exc)

    async def _send_telnyx_media(self, payload_b64: str) -> None:
        """Eve audio from xAI. Held (not played) while waiting for a voicemail beep (fix8)."""
        if self._vm.holding:
            self._held_audio.append(payload_b64)
            return
        self._resp_audio.append(payload_b64)
        await self._play_telnyx_media(payload_b64)

    async def _start_voicemail_hold(self, now: float) -> None:
        """fix8: voicemail greeting heard. Stop Eve's reply on the line and replay it after the beep."""
        if not self._vm.start(now):
            return
        tag = self.stream_id or self.call_control_id or "?"
        self._watch.pending += 1  # no hang-up while held
        self._held_audio = list(self._resp_audio)
        self._resp_audio = []
        if not self.closed and not self.telnyx_ws.closed:
            try:
                await self.telnyx_ws.send_str(json.dumps({"event": "clear"}))
            except Exception as exc:  # noqa: BLE001
                logging.warning("telnyx clear failed: %s", exc)
        self._watch.play_until = now
        append_call_log(f"{now:.0f} VOICEMAIL_HOLD stream_id={tag} replay_chunks={len(self._held_audio)}")

    async def _release_voicemail_hold(self, now: float) -> None:
        tag = self.stream_id or self.call_control_id or "?"
        self._watch.pending = max(0, self._watch.pending - 1)
        held, self._held_audio = self._held_audio, []
        append_call_log(
            f"{now:.0f} VOICEMAIL_RELEASE reason={self._vm.reason} waited={now - self._vm.started_at:.1f}s "
            f"chunks={len(held)} stream_id={tag}")
        for chunk in held:
            self._resp_audio.append(chunk)
            await self._play_telnyx_media(chunk)

    async def _play_telnyx_media(self, payload_b64: str) -> None:
        if self.closed or self.telnyx_ws.closed:
            return
        self._guard.voice_audio(payload_b64, time.time())
        if self._resampler is not None:
            payload_b64 = self._resampler.down(payload_b64)
            if not payload_b64:
                return
        self._watch.voice_audio(len(payload_b64) * 3 // 4, time.time())
        try:
            await self.telnyx_ws.send_str(
                json.dumps({"event": "media", "media": {"payload": payload_b64}})
            )
        except Exception as exc:  # noqa: BLE001
            logging.warning("telnyx send media failed: %s", exc)

    def _fu(self) -> cw.FollowUps:
        """Follow-up list for this call, labelled with the current callee (fix5: one entry per request)."""
        self._follow_ups.bind(callee=self._callee, voice=VOICE_LABEL,
                              stream_id=self.stream_id or self.call_control_id or "?")
        return self._follow_ups

    def _log_follow_ups(self, entries: list[str]) -> None:
        tag = self.stream_id or self.call_control_id or "?"
        for entry in entries:
            append_call_log(f"{time.time():.0f} FOLLOW_UP stream_id={tag} text={cw.short(entry)!r}")

    async def _watch_loop(self) -> None:
        """Twice a second: nudge a stalled Eve, end a finished or silent call."""
        tag = self.stream_id or self.call_control_id or "?"
        try:
            while not self.closed:
                await asyncio.sleep(0.5)
                if self._detached:
                    continue
                self._log_follow_ups(self._fu().tick(time.time()))
                if self._vm.ready(time.time()):
                    await self._release_voicemail_hold(time.time())
                action = self._watch.decide(time.time())
                if not action:
                    continue
                kind, detail = action
                if kind == "nudge":
                    append_call_log(f"{time.time():.0f} STALL nudge={detail} stream_id={tag} backend=xai")
                    if self.xai_ws and _ws_is_open(self.xai_ws):
                        try:
                            await self.xai_ws.send(json.dumps({"type": "response.create"}))
                            self._watch.voice_busy(True, time.time())
                        except Exception as exc:  # noqa: BLE001
                            logging.warning("stall nudge failed: %s", exc)
                elif kind == "say_bye":
                    append_call_log(f"{time.time():.0f} SAY_BYE reason={detail} stream_id={tag} backend=xai")
                    if self.xai_ws and _ws_is_open(self.xai_ws):
                        try:
                            await self.xai_ws.send(json.dumps({"type": "response.create", "response": {
                                "instructions": "Nobody answered your question. Say one short warm goodbye "
                                                "now and end the call."}}))
                            self._watch.voice_busy(True, time.time())
                        except Exception as exc:  # noqa: BLE001
                            logging.warning("bye prompt failed: %s", exc)
                elif kind == "hangup":
                    self._hangup_reason = str(detail)
                    append_call_log(f"{time.time():.0f} AUTO_HANGUP reason={detail} stream_id={tag} backend=xai")
                    if self.call_control_id:
                        await telnyx_hangup(self.call_control_id, str(detail))
                    return
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            logging.exception("watch loop failed stream_id=%s: %s", tag, exc)

    def _flush_partial_said(self, tag: Any, now: float) -> None:
        """Fix 14: a cut-off reply never gets transcript.done; keep its words from the deltas."""
        part = (getattr(self, "_partial_said", "") or "").strip()
        self._partial_said = ""
        if not part:
            return
        self._said.append(part)
        _save_transcript_line(tag, VOICE_LABEL + " (cut off)", part)
        append_call_log(f"{now:.0f} TEXT stream_id={tag} who=Eve cutoff=1 text={cw.short(part)!r}")

    def write_ping(self) -> None:
        """Auto post-call note (read by call_outcome.py)."""
        if not self.stream_id:
            return
        import datetime as _dt
        duration = max(0, int(time.time() - self._started_at))
        said = " ".join(self._said).strip()
        heard = " ".join(self._heard).strip()
        low = (said + " " + heard).lower()
        if any(x in low for x in ("leave a message", "after the tone", "you've rung", "voicemail")):
            outcome = "voicemail"
        elif heard:
            outcome = "answered"
        else:
            outcome = "no reply heard"
        self._flush_partial_said(self.stream_id, time.time())
        said = " ".join(self._said).strip()
        check = " ; ".join(x for x in (
            cw.low_text_check(duration, said, self._watch.stalled),
            cw.brief_check(cw.read_phone_context(), said, callee=self._callee, voice="eve",
                           callee_ended=not self._hangup_reason),
            (f"CALLER_UNHEARD x{getattr(self, '_unheard_events', 0)} (no words transcribed)"
             if getattr(self, "_unheard_events", 0) and not heard else ""),
        ) if x) or "ok"
        stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
        pdir = LOG_DIR / "pings"
        pdir.mkdir(parents=True, exist_ok=True)
        path = pdir / f"{stamp}-eve.md"
        brief_path = pdir / f"{stamp}-eve-brief.md"
        lines = [
            "# Post-call note (xAI voice)",
            "",
            f"- time: {cw.local_now_str()}",
            f"- callee: {self._callee}",
            f"- duration: {duration}s",
            f"- outcome: {outcome}",
            f"- check: {check}",
            f"- stalled: {'yes' if self._watch.stalled else 'no'}",
            f"- ended by: {self._hangup_reason or '(other side or network)'}",
            f"- brief used: {brief_path.name}",
            f"- call_control_id: {self.call_control_id}",
            f"- stream_id: {self.stream_id}",
            f"- full transcript: logs/transcripts/ (file ending {str(self.stream_id)[:8]}.md)",
            "",
            f"## Transcript ({self._callee})",
            heard[:2000] or "(empty)",
            "",
            "## Transcript (Eve)",
            said[:2000] or "(empty)",
            "",
            "## Follow-ups",
            "\n".join(f"- {x}" for x in self._follow_ups.items) if self._follow_ups.items else "(none)",
            "",
        ]
        text = "\n".join(lines)
        path.write_text(text, encoding="utf-8")
        (LOG_DIR / "last-ping.md").write_text(text, encoding="utf-8")
        brief_path.write_text(self._brief or "(no brief found)\n", encoding="utf-8")
        append_call_log(
            f"{time.time():.0f} PING outcome={outcome} voice=Eve check={check!r} "
            f"stalled={self._watch.stalled} path={path}"
        )

    def detach(self) -> bool:
        """The Telnyx media link dropped but the call may still be up: keep the voice session and
        wait DETACH_GRACE s for Telnyx to re-open the link, instead of greeting again on a fresh session."""
        if self.closed or not self.call_control_id or self.xai_ws is None or not _ws_is_open(self.xai_ws):
            return False
        self._detached = True
        DETACHED[self.call_control_id] = self
        tag = self.stream_id or self.call_control_id
        append_call_log(f"{time.time():.0f} DETACH media link dropped; holding voice session stream_id={tag}")
        logging.info("bridge detached (media link dropped) stream_id=%s", tag)

        async def _expire() -> None:
            await asyncio.sleep(DETACH_GRACE)
            if self._detached and DETACHED.get(self.call_control_id) is self:
                DETACHED.pop(self.call_control_id, None)
                await self.close(reason="telnyx_ws_closed")
        self._detach_task = asyncio.create_task(_expire())
        return True

    def reattach(self, telnyx_ws: web.WebSocketResponse, stream_id: str | None) -> None:
        tag = self.stream_id or self.call_control_id
        gap = time.time() - getattr(self, "_detached_at", time.time())
        self.telnyx_ws = telnyx_ws
        self._detached = False
        if self._detach_task and not self._detach_task.done():
            self._detach_task.cancel()
        if stream_id and stream_id != self.stream_id:
            BRIDGES_BY_STREAM.pop(str(self.stream_id), None)
            self.stream_id = stream_id
            BRIDGES_BY_STREAM[stream_id] = self
        if self.call_control_id:
            BRIDGES_BY_CALL[self.call_control_id] = self
        append_call_log(f"{time.time():.0f} REATTACH media link back; same voice session, no new greeting stream_id={tag}")
        logging.info("bridge re-attached (same voice session) stream_id=%s", tag)

    async def close(self, reason: str = "close") -> None:
        async with self._lock:
            if self.closed:
                return
            self.closed = True
        tag = self.stream_id or self.call_control_id or "?"
        append_call_log(f"{time.time():.0f} CLOSE reason={reason} stream_id={tag}")
        logging.info("bridge close reason=%s stream_id=%s", reason, tag)
        if self.stream_id:
            BRIDGES_BY_STREAM.pop(self.stream_id, None)
        if self.call_control_id:
            BRIDGES_BY_CALL.pop(self.call_control_id, None)
            STREAM_STARTED.discard(self.call_control_id)
            if DETACHED.get(self.call_control_id) is self:
                DETACHED.pop(self.call_control_id, None)
        self._detached = False
        xai = self.xai_ws
        self.xai_ws = None
        if xai is not None:
            try:
                await xai.close()
            except Exception:  # noqa: BLE001
                pass
        if not self.telnyx_ws.closed:
            try:
                await self.telnyx_ws.close()
            except Exception:  # noqa: BLE001
                pass
        task = self._xai_task
        if task and task is not asyncio.current_task() and not task.done():
            task.cancel()
        wtask = self._watch_task
        if wtask and wtask is not asyncio.current_task() and not wtask.done():
            wtask.cancel()
        try:
            self._log_follow_ups(self._fu().flush())
            g = self._guard
            append_call_log(
                f"{time.time():.0f} OPENING_GUARD stream_id={self.stream_id or self.call_control_id} "
                f"muted_chunks={g.muted_chunks} muted_loud={g.muted_loud} voice_heard={g.heard_voice}")
        except Exception as exc:  # noqa: BLE001
            logging.warning("eve follow-up flush failed: %s", exc)
        try:
            self.write_ping()
        except Exception as exc:  # noqa: BLE001
            logging.warning("eve write_ping failed: %s", exc)


BRIDGES_BY_CALL: dict[str, Any] = {}
BRIDGES_BY_STREAM: dict[str, Any] = {}
STREAM_STARTED: set[str] = set()
PENDING_XAI: dict[str, Any] = {}
DETACHED: dict[str, Any] = {}   # call_control_id -> bridge whose media link dropped (awaiting re-open)
DETACH_GRACE = 20.0


async def health(_request: web.Request) -> web.Response:
    return web.json_response(
        {
            "ok": True,
            "service": "phone-caller",
            "audio_mode": audio_mode_label(),
        }
    )


def streaming_start_body(stream_url: str) -> dict[str, Any]:
    """Wideband L16 @ 16k (default) or PCMU 8k — never mix within a call."""
    if audio_mode() == "pcmu":
        return {
            "stream_url": stream_url,
            "stream_track": "inbound_track",
            "stream_bidirectional_mode": "rtp",
            "stream_bidirectional_codec": "PCMU",
            "stream_bidirectional_target_legs": "self",
            "stream_bidirectional_sampling_rate": 8000,
            "stream_codec": "PCMU",
        }
    return {
        "stream_url": stream_url,
        "stream_track": "inbound_track",
        "stream_bidirectional_mode": "rtp",
        "stream_bidirectional_codec": "L16",
        "stream_bidirectional_target_legs": "self",
        "stream_bidirectional_sampling_rate": 16000,
        "stream_codec": "L16",
    }


async def telnyx_streaming_start(call_control_id: str) -> None:
    api_key = os.environ.get("TELNYX_API_KEY")
    if not api_key:
        logging.error("TELNYX_API_KEY missing; cannot streaming_start")
        append_call_log(
            f"{time.time():.0f} ERROR missing_telnyx_api_key call_control_id={call_control_id}"
        )
        return
    stream_url = media_wss_url()
    if not stream_url:
        logging.error("No PUBLIC_URL / STATE.json; cannot streaming_start")
        append_call_log(
            f"{time.time():.0f} ERROR missing_public_url call_control_id={call_control_id}"
        )
        return
    if call_control_id in STREAM_STARTED or call_control_id in BRIDGES_BY_CALL:
        logging.info(
            "streaming_start skipped (already up) for %s", call_control_id
        )
        return
    STREAM_STARTED.add(call_control_id)
    url = f"{TELNYX_API_BASE}/calls/{quote(call_control_id, safe='')}/actions/streaming_start"
    body = streaming_start_body(stream_url)
    append_call_log(
        f"{time.time():.0f} STREAMING_START call_control_id={call_control_id} "
        f"stream_url={stream_url} audio_mode={audio_mode_label()} track=inbound_track"
    )
    logging.info("POST streaming_start ccid=%s url=%s", call_control_id, stream_url)
    session: ClientSession = _http
    try:
        async with session.post(
            url,
            json=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        ) as resp:
            text = await resp.text()
            logging.info("streaming_start status=%s body=%s", resp.status, text[:500])
            append_call_log(
                f"{time.time():.0f} STREAMING_START_RESP status={resp.status} "
                f"call_control_id={call_control_id}"
            )
            if resp.status >= 400:
                STREAM_STARTED.discard(call_control_id)
    except Exception as exc:  # noqa: BLE001
        STREAM_STARTED.discard(call_control_id)
        logging.exception("streaming_start failed: %s", exc)
        append_call_log(
            f"{time.time():.0f} ERROR streaming_start_failed "
            f"call_control_id={call_control_id} err={exc!r}"
        )


async def telnyx_hangup(call_control_id: str, reason: str = "auto") -> None:
    """End the call from our side (auto hang-up after goodbye / silence / stall)."""
    api_key = os.environ.get("TELNYX_API_KEY")
    if not api_key or not call_control_id:
        append_call_log(f"{time.time():.0f} ERROR hangup_skipped no_key_or_id ccid={call_control_id}")
        return
    url = f"{TELNYX_API_BASE}/calls/{quote(call_control_id, safe='')}/actions/hangup"
    try:
        async with _http.post(
            url,
            json={"command_id": f"auto-hangup-{call_control_id[-12:]}"},
            headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
        ) as resp:
            body = await resp.text()
            append_call_log(
                f"{time.time():.0f} HANGUP_SENT status={resp.status} reason={reason} "
                f"call_control_id={call_control_id}"
            )
            if resp.status >= 400:
                logging.warning("hangup failed status=%s body=%s", resp.status, body[:300])
    except Exception as exc:  # noqa: BLE001
        logging.exception("hangup failed: %s", exc)
        append_call_log(f"{time.time():.0f} ERROR hangup_failed call_control_id={call_control_id} err={exc!r}")


def _event_type_and_payload(payload: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    event_type = data.get("event_type") or payload.get("event_type") or payload.get("event")
    inner = data.get("payload") if isinstance(data.get("payload"), dict) else data
    return event_type, inner


async def telnyx_voice(request: web.Request) -> web.Response:
    global _pubkey_todo_logged
    body = await request.read()
    sig = request.headers.get("telnyx-signature-ed25519")
    ts = request.headers.get("telnyx-timestamp")
    pubkey = os.environ.get("TELNYX_PUBLIC_KEY", "")

    if pubkey:
        if not verify_telnyx_ed25519(body, sig, ts, pubkey):
            logging.warning("Telnyx webhook signature failed ts=%s", ts)
            return web.json_response({"error": "invalid signature"}, status=401)
    else:
        # TODO: set TELNYX_PUBLIC_KEY and enforce ed25519 verification.
        if not _pubkey_todo_logged:
            logging.debug(
                "TODO: TELNYX_PUBLIC_KEY unset; accepting webhook without ed25519 verify"
            )
            _pubkey_todo_logged = True

    try:
        payload = json.loads(body.decode("utf-8") if body else "{}")
    except json.JSONDecodeError:
        return web.json_response({"error": "invalid json"}, status=400)

    event_type, inner = _event_type_and_payload(payload)
    call_control_id = inner.get("call_control_id") or ""
    backend = "xai"
    logging.info(
        "webhook event=%s call_control_id=%s backend=%s",
        event_type,
        call_control_id,
        backend,
    )
    append_call_log(
        f"{time.time():.0f} WEBHOOK event={event_type} call_control_id={call_control_id} "
        f"backend={backend}"
    )

    # Pre-warm xAI only for Eve path (before answer / media).
    if event_type in ("call.initiated", "call.bridged") and call_control_id:
        if backend == "xai":
            asyncio.create_task(prewarm_xai(str(call_control_id)))
            return web.json_response({"ok": True, "action": "xai_prewarm", "backend": backend})
        return web.json_response({"ok": True, "action": "no_prewarm", "backend": backend})

    if event_type == "streaming.started" and call_control_id:
        STREAM_STARTED.add(str(call_control_id))
        return web.json_response({"ok": True, "action": "stream_noted"})

    # Fallback if dial did not already open the media stream.
    if event_type == "call.answered" and call_control_id:
        asyncio.create_task(telnyx_streaming_start(str(call_control_id)))
        return web.json_response({"ok": True, "action": "streaming_start"})

    if event_type in ("call.hangup", "call.hangup.received", "streaming.stopped"):
        if event_type == "call.hangup":
            append_call_log(
                f"{time.time():.0f} HANGUP_INFO call_control_id={call_control_id} "
                f"cause={inner.get('hangup_cause')} source={inner.get('hangup_source')}"
            )
        if call_control_id:
            ccid = str(call_control_id)
            STREAM_STARTED.discard(ccid)
            asyncio.create_task(discard_pending_xai(ccid, reason=str(event_type)))
            bridge = BRIDGES_BY_CALL.get(ccid)
            if bridge:
                asyncio.create_task(bridge.close(reason=str(event_type)))
        return web.json_response({"ok": True, "action": "closed"})

    if event_type == "streaming.failed":
        logging.warning("streaming.failed payload=%s", inner)
        append_call_log(
            f"{time.time():.0f} ERROR streaming.failed call_control_id={call_control_id}"
        )
        return web.json_response({"ok": True, "action": "logged"})

    return web.json_response({"ok": True, "ignored": event_type})


async def telnyx_media(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(max_msg_size=8 * 1024 * 1024, heartbeat=20)
    await ws.prepare(request)
    backend = "xai"
    logging.info(
        "telnyx media WS connected from %s backend=%s", request.remote, backend
    )
    append_call_log(
        f"{time.time():.0f} TELNYX_WS connected remote={request.remote} backend={backend}"
    )
    bridge: Any = Bridge(ws)
    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                await bridge.handle_telnyx_frame(msg.data)
                if bridge.handoff is not None:
                    bridge = bridge.handoff
            elif msg.type == WSMsgType.BINARY:
                try:
                    await bridge.handle_telnyx_frame(msg.data.decode("utf-8"))
                    if bridge.handoff is not None:
                        bridge = bridge.handoff
                except UnicodeDecodeError:
                    logging.warning("telnyx media binary non-utf8 len=%s", len(msg.data))
            elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED):
                break
            elif msg.type == WSMsgType.ERROR:
                logging.warning("telnyx media WS error %s", ws.exception())
                break
    finally:
        if bridge.telnyx_ws is ws and not bridge.closed:
            bridge._detached_at = time.time()
            if not bridge.detach():
                await bridge.close(reason="telnyx_ws_closed")
    return ws


_http: ClientSession


async def on_startup(app: web.Application) -> None:
    global _http
    _http = ClientSession(timeout=ClientTimeout(total=20))
    app["http"] = _http


async def on_cleanup(app: web.Application) -> None:
    for ccid in list(PENDING_XAI.keys()):
        await discard_pending_xai(ccid, reason="shutdown")
    bridges = list(BRIDGES_BY_CALL.values()) + list(BRIDGES_BY_STREAM.values())
    seen: set[int] = set()
    for b in bridges:
        if id(b) in seen:
            continue
        seen.add(id(b))
        await b.close(reason="shutdown")
    session: ClientSession | None = app.get("http")
    if session is not None:
        await session.close()


def make_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_post("/telnyx/voice", telnyx_voice)
    app.router.add_get("/telnyx/media", telnyx_media)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def main() -> None:
    setup_logging()
    port = int(os.environ.get("PORT", _conf("PORT", "8790")))
    if not os.environ.get("XAI_API_KEY"):
        logging.warning("XAI_API_KEY not set at startup")
    if not os.environ.get("TELNYX_API_KEY"):
        logging.warning("TELNYX_API_KEY not set at startup")
    if not os.environ.get("TELNYX_PUBLIC_KEY"):
        logging.warning(
            "TODO: TELNYX_PUBLIC_KEY unset; webhooks accepted without ed25519 verify"
        )
    logging.info(
        "phone-caller listening on 127.0.0.1:%s media_wss=%s "
        "voice_backend=%s audio_mode=%s",
        port,
        media_wss_url(),
        "xai",
        audio_mode_label(),
    )
    web.run_app(make_app(), host="127.0.0.1", port=port, print=None)


if __name__ == "__main__":
    main()
