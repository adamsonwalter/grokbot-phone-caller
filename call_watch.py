"""Call helpers for the Phone Caller call server (server.py). From a working private setup, Oct 2026.

- CallWatch: decides when to nudge a stalled voice and when to hang up a finished call.
- LineBuffer: joins transcript fragments into whole lines.
- Follow-up log, local clock, callee name, brief copy, LOW TEXT check.
"""
from __future__ import annotations

import array
import base64
import re
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOG_DIR = ROOT / "logs"
FOLLOW_UPS_PATH = LOG_DIR / "follow-ups.md"
TRANSCRIPT_DIR = LOG_DIR / "transcripts"

# Timers (seconds), tuned on real calls in Oct 2026.
FIRST_REPLY_WAIT = 10.0   # after the voice's opening, wait this long for a first reply
SILENCE_HANGUP = 6.0      # after the voice's turn (or goodbye), hang up after this much silence
STALL_NUDGE = 8.0         # other side spoke, voice silent this long -> nudge
MAX_NUDGES = 2            # nudges per stall, then give up
STALL_GIVE_UP = 10.0      # after the last nudge, hang up if still nothing
MAX_NUDGES_PER_CALL = 4
BUSY_MAX = 25.0           # ignore a "voice busy" flag older than this (lost response.done)
ENERGY_SPEECH = 1500      # inbound PCM16 RMS above this delays the silence hang-up
END_CALL_HANGUP = 2.5     # fix7 (9 Oct 2026), 6.0 -> 2.5 on 19 Oct 2026: after end_call, hang up this long after the voice's last audio
END_CALL_BUSY_CAP = 8.0   # fix7: after end_call, wait for a still-open reply at most this long
QUESTIONS_WAIT = 5.0      # fix21 (16 Oct 2026): after "any questions?", no reply this long -> say bye
BYE_HANGUP = 3.0          # fix21: after that prompted goodbye has played, hang up this soon
BYE_PROMPT_CAP = 12.0     # fix21: hang up anyway this long after the bye prompt
VOICE_OUT_RMS = 300       # fix21: outbound chunk quieter than this is silence, not voice
CALLER_UNHEARD_SEC = 3.0  # fix22: loud caller audio with no transcript this long -> log
QUESTIONS_RE = re.compile(r"\b(any|have|got)\b[^.?!]{0,40}\bquestions?\b|anything else[^.?!]{0,30}\?", re.I)

GOODBYE_RE = re.compile(
    r"\b(bye|goodbye|talk soon|speak soon|take care|have a (good|great|lovely|nice) "
    r"(day|night|evening|afternoon|one|weekend))\b[\s.!,]*$",
    re.IGNORECASE,
)
FOLLOW_UP_RE = re.compile(r"follow[- ]?up", re.IGNORECASE)
# fix8 (9 Oct 2026): a voicemail greeting from the other side.
VOICEMAIL_RE = re.compile(
    r"leave (?:a|your|me a)? ?(?:detailed |short |brief )?message|after the (?:tone|beep)|at the (?:tone|beep)"
    r"|\bvoice ?mail\b|message bank|you(?:'ve| have) (?:rung|reached|called)|is not available|can'?t take your call"
    r"|unable to take your call|finished recording|record your message",
    re.IGNORECASE,
)


def is_voicemail_greeting(text: str) -> bool:
    return bool(VOICEMAIL_RE.search(text or ""))
# The other person asking for something to be passed on or followed up (fix2, 8 Oct 2026).
ASK_RE = re.compile(
    r"\b(?:tell|remind|ask|message|update|text)\s+(?:him|her|them)\b|\blet\s+(?:him|her|them)\s+know\b|\bfollow[- ]?up\b"
    r"|\bpass (?:it|that|this) on\b|\bcall me back\b|\bget back to me\b",
    re.IGNORECASE,
)


def local_now() -> datetime:
    """The computer's local time zone (set TZ to change it)."""
    return datetime.now().astimezone()


def local_now_str() -> str:
    return local_now().strftime("%Y-%m-%d %H:%M:%S %Z")



def callee_first_name(phone_context_text: str) -> str:
    for line in (phone_context_text or "").splitlines():
        key, sep, val = line.strip().partition(":")
        if sep and key.strip().lower() == "callee_first_name":
            name = val.strip().strip("*").strip()
            if name:
                return name.split()[0]
    m = re.search(r"This call is to\s+\*?\*?([A-Za-z][A-Za-z'-]*)", phone_context_text or "", re.I)
    return m.group(1) if m else "Them"


def read_phone_context() -> str:
    try:
        return (ROOT / "PHONE_CONTEXT.md").read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        return ""


def short(text: str, limit: int = 140) -> str:
    t = " ".join(str(text or "").split())
    return t if len(t) <= limit else t[: limit - 1] + "…"


def is_goodbye(text: str) -> bool:
    return bool(GOODBYE_RE.search((text or "").strip()))


def log_follow_up(text: str, *, callee: str, voice: str, stream_id: str = "") -> str:
    t = " ".join(str(text or "").split())
    if not t:
        return ""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    new = not FOLLOW_UPS_PATH.exists()
    with FOLLOW_UPS_PATH.open("a", encoding="utf-8") as fh:
        if new:
            fh.write("# Follow-ups from phone calls\n\n")
        fh.write(f"- {local_now_str()} | callee: {callee} | voice: {voice} | "
                 f"stream: {str(stream_id)[:8]} | {t}\n")
    return t


_PLEASANTRY_RE = re.compile(
    r"^(?:thank you|thanks|cheers|goodbye|bye|bye bye|ok|okay|great|lovely|got it|"
    r"talk soon|speak soon|take care)(?: (?:so much|very much|again))?[\s.!,]*$", re.IGNORECASE)
_FILLER_RE = re.compile(r"^(?:(?:uh|um|umm|er|erm|ah|oh|yeah|yes|so|okay|ok|right|look|well|just)\b[\s,.]*)+", re.IGNORECASE)
_READ_BACK_RE = re.compile(r"noting (?:that )?for follow[- ]?up\s*[:,-]?\s*", re.IGNORECASE)
_READ_BACK_START_RE = re.compile(r"(?:^|(?<=[.!?])\s+)noting (?:that )?for follow[- ]?up\s*[:,-]?\s*",
                                 re.IGNORECASE)
_WORD_RE = re.compile(r"[a-z0-9']+")


def tidy_request(text: str) -> str:
    """Drop leading 'uh, yeah,' and trailing 'Thank you. Goodbye.' from a follow-up request."""
    t = " ".join(str(text or "").split()).strip(" ,")
    t = _FILLER_RE.sub("", t).strip()
    sentences = [x.strip() for x in re.split(r"(?<=[.!?])\s+", t) if x.strip()]
    while len(sentences) > 1 and _PLEASANTRY_RE.match(sentences[-1]):
        sentences.pop()
    t = " ".join(sentences)
    t = re.sub(r"(?:[\s,.]+(?:thank you|thanks|goodbye|bye))+[\s.!,]*$", "", t, flags=re.IGNORECASE) or t
    t = t.rstrip(" .,;:")
    return (t[:1].upper() + t[1:]) if t else t


def _join_fragments(a: str, b: str) -> str:
    b = b.strip().lstrip(".,;: ").strip()
    if not b:
        return a
    if not a:
        return b
    if a[-1] not in ".!?,;:":
        a += "."
    return f"{a} {b}"


def _overlap(a: str, b: str) -> float:
    wa = {w for w in _WORD_RE.findall(a.lower()) if len(w) > 2}
    wb = {w for w in _WORD_RE.findall(b.lower()) if len(w) > 2}
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / min(len(wa), len(wb))


class FollowUps:
    """Per-call follow-up list (fix5): one clean entry per request.

    The caller's broken-up phrases from one turn are joined into a single entry, written once
    the turn is over. The voice's own read-back (or the voice's log_follow_up tool) for the
    same request is not saved again: the caller's full words win. A voice item is only saved
    when no caller request is waiting for it (e.g. a follow-up the voice raised itself).
    """

    JOIN_IDLE = 10.0     # caller pauses longer than this end the request
    SETTLE = 2.0         # after the voice replies, wait this long for more caller words
    MATCH_WINDOW = 45.0  # a voice read-back within this of a caller request is the same request

    def __init__(self) -> None:
        self.items: list[str] = []
        self._seen: set[str] = set()
        self._pending: dict | None = None      # caller request still being spoken
        self._caller_reqs: list[dict] = []     # committed caller requests (for matching read-backs)
        self._voice_written: list[tuple[float, str]] = []
        self._meta: dict = {}

    def bind(self, *, callee: str, voice: str, stream_id: str = "") -> None:
        self._meta = {"callee": callee, "voice": voice, "stream_id": stream_id}

    # ---- events ---------------------------------------------------------------------------
    def caller_line(self, text: str, now: float, *, source: str = "asked on the call",
                    force: bool = False) -> list[str]:
        """A finished line from the caller. Joins onto a request still in progress."""
        t = " ".join(str(text or "").split())
        if not t:
            return []
        p = self._pending
        if p is not None and not p["voice_replied"] and now - p["last"] <= self.JOIN_IDLE:
            if t not in p["text"]:
                p["text"] = _join_fragments(p["text"], t)
            p["last"] = now
            return []
        out = self._commit_pending()
        if not force and is_voicemail_greeting(t):
            return out  # fix9: a recorded greeting is never a request
        if force or ASK_RE.search(t):
            self._pending = {"text": t, "source": source, "start": now, "last": now,
                             "voice_replied": False, "replied_at": 0.0, "matched": False}
        return out

    def caller_request(self, text: str, now: float, *, source: str) -> list[str]:
        """The voice passed the caller's words on: make sure they become a request."""
        if self._pending is not None:
            return []
        t = " ".join(str(text or "").split())
        for req in self._caller_reqs:
            if _overlap(tidy_request(t), req["text"]) >= 0.8:
                return []
        return self.caller_line(t, now, source=source, force=True)

    def voice_line(self, text: str, now: float) -> list[str]:
        """A finished line from the voice: ends the caller's turn; a read-back may be an item."""
        t = " ".join(str(text or "").split())
        out: list[str] = []
        if self._pending is not None and len(t.split()) >= 3 and not self._pending["voice_replied"]:
            self._pending["voice_replied"] = True
            self._pending["replied_at"] = now
        # fix9 (9 Oct 2026): only a line (or sentence) that starts "Noting for follow-up:" is an
        # item. The voice's own brief is never saved.
        m = _READ_BACK_START_RE.search(t)
        if m:
            out += self.voice_item(t[m.end():], now, source="noted by voice")
        return out

    def voice_item(self, text: str, now: float, *, source: str) -> list[str]:
        """An item the voice logged or read back. Skipped when it repeats a caller request."""
        t = tidy_request(text)
        if not t:
            return []
        if self._pending is not None:
            self._pending["matched"] = True
            return []
        for req in reversed(self._caller_reqs):
            if now - req["at"] <= self.MATCH_WINDOW and (not req["matched"] or _overlap(t, req["text"]) >= 0.3
                                                        or now - req["matched_at"] <= 20):
                if not req["matched"]:
                    req["matched"], req["matched_at"] = True, now
                return []
        for at, prev in self._voice_written:
            if now - at <= 30 and _overlap(t, prev) >= 0.5:
                return []
        entry = self._write(t, source)
        if entry:
            self._voice_written.append((now, t))
            return [entry]
        return []

    def tick(self, now: float) -> list[str]:
        p = self._pending
        if p is None:
            return []
        if now - p["last"] >= self.JOIN_IDLE or (p["voice_replied"] and now - max(p["last"], p["replied_at"]) >= self.SETTLE):
            return self._commit_pending(now)
        return []

    def flush(self) -> list[str]:
        return self._commit_pending()

    # ---- internals ------------------------------------------------------------------------
    def _commit_pending(self, now: float | None = None) -> list[str]:
        p, self._pending = self._pending, None
        if p is None:
            return []
        text = tidy_request(p["text"])
        entry = self._write(text, p["source"])
        at = now if now is not None else p["last"]
        self._caller_reqs.append({"text": text, "at": max(at, p["last"]), "matched": p["matched"],
                                  "matched_at": at if p["matched"] else 0.0})
        return [entry] if entry else []

    def _write(self, text: str, source: str) -> str | None:
        t = " ".join(str(text or "").split())
        key = re.sub(r"[^a-z0-9 ]", "", t.lower()).strip()[:120]
        if not key or key in self._seen:
            return None
        self._seen.add(key)
        entry = f"{t} ({source})"
        if self._meta:
            log_follow_up(entry, **self._meta)
        self.items.append(entry)
        return entry

    def note(self, text: str, *, source: str, callee: str, voice: str, stream_id: str = "") -> str | None:
        """Direct write (kept for older callers): one entry, deduped."""
        if not self._meta:
            self.bind(callee=callee, voice=voice, stream_id=stream_id)
        return self._write(text, source)


class OpeningGuard:
    """Ignore the caller's sound while the voice opens the call (fix5).

    A 'hello' as the phone is picked up made the voice treat it as an interruption and drop its
    greeting. While active, caller audio is replaced with silence of the same length. Active from
    start() until the voice has been audible for AFTER_AUDIO seconds; if no voice audio arrives
    within FIRST_WAIT the guard lifts anyway, so a silent voice never makes the caller unheard.
    """

    FIRST_WAIT = 4.0
    AFTER_AUDIO = 1.5
    VOICE_RMS = 300

    def __init__(self) -> None:
        self.until: float | None = None
        self.heard_voice = False
        self.muted_chunks = 0
        self.muted_loud = 0
        self._cap = 0.0

    def start(self, now: float) -> None:
        if self.until is None:
            self.until = now + self.FIRST_WAIT
            self._cap = now + self.FIRST_WAIT + self.AFTER_AUDIO

    def voice_audio(self, payload_b64: str, now: float, *, is_pcm: bool = True) -> None:
        if self.heard_voice or self.until is None or now >= self.until:
            return
        if is_pcm and pcm16_rms(payload_b64) < self.VOICE_RMS:
            return
        self.heard_voice = True
        self.until = min(now + self.AFTER_AUDIO, self._cap)

    def active(self, now: float) -> bool:
        return self.until is not None and now < self.until

    def filter_b64(self, payload_b64: str, now: float, *, silence_byte: bytes = b"\x00") -> str:
        """Return the caller audio unchanged, or same-length silence while the guard is on."""
        if not self.active(now):
            return payload_b64
        try:
            raw = base64.b64decode(payload_b64)
        except Exception:  # noqa: BLE001
            return payload_b64
        self.muted_chunks += 1
        if silence_byte == b"\x00" and pcm16_rms(payload_b64) >= ENERGY_SPEECH:
            self.muted_loud += 1
        return base64.b64encode(silence_byte * len(raw)).decode("ascii")


def save_transcript_line(stream_tag: str, who: str, text: str) -> None:
    """One file per call: logs/transcripts/YYYY-MM-DD-<stream8>.md, written as the call runs."""
    t = str(text or "").strip()
    if not t:
        return
    try:
        TRANSCRIPT_DIR.mkdir(parents=True, exist_ok=True)
        now = local_now()
        f = TRANSCRIPT_DIR / f"{now.strftime('%Y-%m-%d')}-{str(stream_tag)[:8]}.md"
        with f.open("a", encoding="utf-8") as fh:
            fh.write(f"[{now.strftime('%H:%M:%S')}] {who}: {t}\n\n")
    except Exception:  # noqa: BLE001
        pass


def low_text_check(duration_sec: float, voice_text: str, stalled: bool) -> str:
    """'' when fine, else 'LOW TEXT (...)' for the ping."""
    words = len((voice_text or "").split())
    reasons = []
    if duration_sec > 45 and words < 15:
        reasons.append(f"{int(duration_sec)}s call but only {words} words from the voice")
    if stalled:
        reasons.append("voice stalled")
    return "LOW TEXT (" + "; ".join(reasons) + ")" if reasons else ""


def pcm16_rms(payload_b64: str) -> int:
    """Rough loudness of a PCM16 chunk. Uses the lower of LE/BE readings so a byte-order
    mix-up reads as quiet, not loud (quiet only lets the hang-up timer run as designed)."""
    try:
        raw = base64.b64decode(payload_b64)
    except Exception:  # noqa: BLE001
        return 0
    n = len(raw) - (len(raw) % 2)
    if n < 2:
        return 0
    best = None
    for swap in (False, True):
        a = array.array("h")
        a.frombytes(raw[:n])
        if swap:
            a.byteswap()
        step = max(1, len(a) // 160)
        vals = a[::step]
        rms = int((sum(v * v for v in vals) / len(vals)) ** 0.5)
        best = rms if best is None else min(best, rms)
    return best or 0


class VoicemailGate:
    """fix8 (9 Oct 2026): on voicemail, hold the voice until the beep has played.

    Started when the other side's first line looks like a voicemail greeting. While held, the
    server keeps (does not play) the voice's audio and sends the voice silence instead of the
    line, so the beep can't interrupt it. Released when a beep (loud burst) has been heard and
    then BEEP_QUIET s of quiet, or after NO_BEEP_QUIET s of quiet with no beep, or after MAX_WAIT.
    """

    LOUD = 1500           # inbound PCM16 RMS that counts as tone/speech
    BEEP_QUIET = 0.6
    NO_BEEP_QUIET = 2.5
    SPEECH_RUN = 1.5      # a loud run longer than this is more greeting, not the beep
    MAX_WAIT = 8.0

    def __init__(self) -> None:
        self.started_at = 0.0
        self.released_at = 0.0
        self.last_loud = 0.0
        self.loud_since = 0.0
        self.beep = False
        self.reason = ""
        self.started = False
        self.released = False

    @property
    def holding(self) -> bool:
        return self.started and not self.released

    def start(self, now: float) -> bool:
        if self.started:
            return False
        self.started = True
        self.started_at = now
        self.last_loud = now
        return True

    def inbound(self, rms: int, now: float) -> None:
        if not self.holding:
            return
        if rms >= self.LOUD:
            if not self.loud_since or now - self.last_loud > 0.3:
                self.loud_since = now
            self.last_loud = now
            run = now - self.loud_since
            self.beep = run <= self.SPEECH_RUN  # a long loud run is more greeting: wait again
        elif now - self.last_loud > 0.3:
            self.loud_since = 0.0

    def ready(self, now: float) -> bool:
        if not self.holding:
            return False
        quiet = now - self.last_loud
        if now - self.started_at >= self.MAX_WAIT:
            self.reason = "max_wait"
        elif self.beep and quiet >= self.BEEP_QUIET:
            self.reason = "beep"
        elif not self.beep and quiet >= self.NO_BEEP_QUIET:
            self.reason = "quiet"
        else:
            return False
        self.released_at = now
        self.released = True
        return True


class LineBuffer:
    """Joins transcript fragments into lines; flush on speaker change, pause or sentence end."""

    def __init__(self, gap: float = 1.2) -> None:
        self.gap = gap
        self.parts: list[str] = []
        self.last = 0.0

    def add(self, delta: str, now: float) -> None:
        self.parts.append(delta)
        self.last = now

    def ready(self, now: float) -> bool:
        return bool(self.parts) and now - self.last >= self.gap

    def take(self) -> str:
        line = "".join(self.parts).strip()
        self.parts = []
        return line


class CallWatch:
    """Pure timing logic. Feed it events; call decide(now) about twice a second.

    Returns None, ("nudge", n) or ("hangup", reason). Nothing runs until the voice has
    finished its first turn, and never while either side is talking or backend work is open.
    """

    def __init__(self, bytes_per_sec: int = 32000) -> None:
        self.bytes_per_sec = max(1, bytes_per_sec)
        self.voice_started = False
        self.play_until = 0.0        # estimated end of voice audio on the phone
        self.last_voice = 0.0        # last voice audio/text activity
        self.busy_since = 0.0        # voice generating a reply (Eve response.created)
        self.other_speaking = False  # Eve VAD speech_started .. speech_stopped
        self.last_other = 0.0        # last time the other side made sound/text
        self.other_replied = False   # other side has spoken since the voice's first turn
        self.last_sound = 0.0        # loud inbound audio (no transcript yet); only delays hang-up
        self.pending = 0             # open tool / delegation work
        self.goodbye = False
        self.end_requested_at = 0.0  # fix7: when the voice called end_call
        self.stalled = False
        self.nudges_this_stall = 0
        self.nudges_total = 0
        self.last_nudge = 0.0
        self.done = False
        self.asked_questions_at = 0.0  # fix21: voice asked "any questions?"
        self.bye_prompted_at = 0.0     # fix21: we told the voice to say goodbye

    # --- events ---
    def voice_audio(self, nbytes: int, now: float, *, silent: bool = False) -> None:
        # fix21 (16 Oct 2026): OpenAI Live streams silence in real time all session; a silent chunk
        # must not push play_until forward or the silence/no-reply hang-ups can never fire.
        if silent:
            return
        self.voice_started = True
        self.play_until = max(self.play_until, now) + nbytes / self.bytes_per_sec
        self.last_voice = now
        if self.last_other and now > self.last_other:
            self.nudges_this_stall = 0  # voice answered; stall episode over

    def voice_text(self, text: str, now: float) -> None:
        self.last_voice = max(self.last_voice, now)
        if is_goodbye(text):
            self.goodbye = True
        if text and QUESTIONS_RE.search(text):
            self.asked_questions_at = now

    def voice_busy(self, busy: bool, now: float) -> None:
        self.busy_since = now if busy else 0.0

    def other_activity(self, now: float, speaking: bool | None = None) -> None:
        if speaking is not None:
            self.other_speaking = speaking
        self.last_other = now
        if self.voice_started:
            self.other_replied = True
            self.goodbye = False  # they kept talking after a goodbye — keep the call up

    def other_sound(self, now: float) -> None:
        """Loud inbound audio. Ignored while the voice plays (echo). Never triggers a nudge."""
        if now >= self.play_until + 0.3:
            self.last_sound = now

    def request_end(self, now: float | None = None) -> None:
        self.goodbye = True
        if not self.end_requested_at:
            self.end_requested_at = now if now is not None else time.time()

    # --- decision ---
    def decide(self, now: float):
        if self.done or not self.voice_started:
            return None
        busy = bool(self.busy_since) and now - self.busy_since < BUSY_MAX
        if self.bye_prompted_at:
            # fix21: we asked the voice to say goodbye after an unanswered "any questions?".
            if self.last_other > self.bye_prompted_at or self.other_speaking:
                self.bye_prompted_at = 0.0  # they spoke after all; normal handling
            else:
                since = now - self.bye_prompted_at
                if since >= BYE_PROMPT_CAP or (since >= 1.5 and not busy and now >= self.play_until
                        and now - max(self.play_until, self.last_voice) >= BYE_HANGUP):
                    self.done = True
                    return ("hangup", "no_answer_after_questions")
                return None
        if self.end_requested_at:
            # fix7 (9 Oct 2026): the voice has said goodbye and called end_call. Hang up
            # END_CALL_HANGUP s after its last audio has played, whatever the other side's line is
            # doing (voicemail tone / line noise no longer keeps the call open).
            if self.pending > 0 or now < self.play_until:
                return None
            if busy and now - self.end_requested_at < END_CALL_BUSY_CAP:
                return None
            if now - max(self.play_until, self.last_voice) >= END_CALL_HANGUP:
                self.done = True
                return ("hangup", "goodbye")
            return None
        if busy or self.other_speaking or self.pending > 0 or now < self.play_until:
            return None
        voice_end = max(self.play_until, self.last_voice)
        if self.last_other > voice_end:
            # Other side spoke last and the voice has not answered: stall handling.
            waited = now - self.last_other
            if (self.nudges_this_stall < MAX_NUDGES and self.nudges_total < MAX_NUDGES_PER_CALL
                    and waited >= STALL_NUDGE * (self.nudges_this_stall + 1)):
                self.nudges_this_stall += 1
                self.nudges_total += 1
                self.stalled = True
                self.last_nudge = now
                return ("nudge", self.nudges_total)
            if (self.nudges_this_stall >= MAX_NUDGES and now - self.last_nudge >= STALL_GIVE_UP
                    and now - max(self.last_other, self.last_sound) >= SILENCE_HANGUP):
                self.done = True
                return ("hangup", "stalled_no_reply")
            return None
        if (self.asked_questions_at and self.asked_questions_at >= self.last_other
                and now - max(voice_end, self.last_sound, self.asked_questions_at) >= QUESTIONS_WAIT):
            self.bye_prompted_at = now
            self.asked_questions_at = 0.0
            self.goodbye = True
            return ("say_bye", "no_answer_after_questions")
        quiet = now - max(voice_end, self.last_other, self.last_sound)
        limit = SILENCE_HANGUP if (self.other_replied or self.goodbye) else FIRST_REPLY_WAIT
        if quiet >= limit:
            self.done = True
            return ("hangup", "goodbye" if self.goodbye else ("silence" if self.other_replied else "no_first_reply"))
        return None


class PcmResampler:
    """Stateful linear resampler for mono PCM16 little-endian (fix3, 8 Oct 2026).
    Carries odd bytes and the last sample across chunks, so chunk boundaries never click."""

    def __init__(self, src_rate: int, dst_rate: int) -> None:
        self.src_rate, self.dst_rate = int(src_rate), int(dst_rate)
        self.step = self.src_rate / self.dst_rate
        self.pos = 0.0
        self.prev: int | None = None
        self.carry = b""

    def convert(self, data: bytes) -> bytes:
        data = self.carry + data
        n = len(data) - (len(data) % 2)
        self.carry = data[n:]
        if n == 0:
            return b""
        a = array.array("h")
        a.frombytes(data[:n])
        if sys.byteorder != "little":
            a.byteswap()
        s = ([self.prev] if self.prev is not None else []) + a.tolist()
        out = array.array("h")
        pos = self.pos
        last = len(s) - 1
        while pos < last:
            i = int(pos)
            f = pos - i
            out.append(int(s[i] + (s[i + 1] - s[i]) * f))
            pos += self.step
        self.pos = pos - last
        self.prev = s[-1]
        if sys.byteorder != "little":
            out.byteswap()
        return out.tobytes()


class ChunkStats:
    """Sizes of audio deltas from OpenAI, for call.log."""

    def __init__(self) -> None:
        self.count = 0
        self.total = 0
        self.min = None
        self.max = 0
        self.odd = 0

    def add(self, nbytes: int) -> None:
        self.count += 1
        self.total += nbytes
        self.min = nbytes if self.min is None else min(self.min, nbytes)
        self.max = max(self.max, nbytes)
        self.odd += nbytes % 2

    def summary(self) -> str:
        avg = self.total // self.count if self.count else 0
        return f"chunks={self.count} min={self.min or 0} max={self.max} avg={avg} odd={self.odd}"


# ---- Brief delivery check (fix 13, 12 Oct 2026) ----
MISSED_POINTS_FILE = Path(__file__).resolve().parent / "logs" / "missed-points.md"
MESSAGE_HEADER = "## THIS CALL HAS A MESSAGE TO DELIVER"
_STOP = set("the a an and or but to of in on at for with is are was be it its it's this that "
            "your you he she they them her his has have will just so also about from by as up "
            "hullo hello hi eve".split())


def has_message_header(ctx_text: str) -> bool:
    return (ctx_text or "").lstrip().startswith(MESSAGE_HEADER)


def brief_message(ctx_text: str) -> str:
    """The '### Message' body dial.sh wrote, or '' when this call has no message to deliver."""
    if not has_message_header(ctx_text):
        return ""
    m = re.search(r"^### Message\s*\n(.*?)(?=^## |\Z)", ctx_text, re.S | re.M)
    return m.group(1).strip() if m else ""


def brief_points(message: str) -> list[str]:
    pts: list[str] = []
    for line in (message or "").splitlines():
        line = re.sub(r"^\s*([-*•]|\d+[.)])\s*", "", line).strip()
        if not line:
            continue
        pts.extend(s.strip() for s in re.split(r"(?<=[.!?])\s+(?=[A-Z])", line) if s.strip())
    return pts


def _keys(point: str) -> list[str]:
    toks = re.findall(r"[A-Za-z0-9][A-Za-z0-9:'’]*", point)
    keys = []
    for t in toks:
        lt = t.lower().strip("'’")
        if lt in _STOP or len(lt) < 3 and not any(c.isdigit() for c in lt):
            continue
        if any(c.isdigit() for c in t) or t[0].isupper() or len(lt) >= 6:
            keys.append(lt)
    return keys


def missed_points(message: str, voice_text: str) -> list[str]:
    """Points whose key words (names, times, dates, long words) mostly never appear in what the voice said."""
    said = (voice_text or "").lower().replace("’", "'")
    said_nums = re.sub(r"[^a-z0-9 ]", " ", said)
    out = []
    for p in brief_points(message):
        ks = _keys(p)
        if not ks:
            continue
        hit = sum(1 for k in ks if k in said or re.sub(r"[^a-z0-9]", " ", k).strip() in said_nums)
        if hit / len(ks) < 0.5:
            out.append(p)
    return out


def record_missed(points: list[str], *, callee: str, voice: str) -> str:
    """Append missed points to logs/missed-points.md; returns 'BRIEF INCOMPLETE: ...' or ''."""
    if not points:
        return ""
    try:
        MISSED_POINTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with MISSED_POINTS_FILE.open("a", encoding="utf-8") as fh:
            fh.write(f"\n## epoch={int(time.time())} {local_now_str()} callee={callee} voice={voice}\n")
            fh.writelines(f"- {p}\n" for p in points)
    except Exception:  # noqa: BLE001
        pass
    return "BRIEF INCOMPLETE: " + " | ".join(points)


CALL_KIND_RE = re.compile(r"^<!-- call_kind: *(\w+) *-->", re.M)
_INSTRUCTION_RE = re.compile(
    r"^(open|then say|say|read|keep|do not|don't|never|be |no small talk|callee_first_name|end the call|"
    r"if (he|she|they|you)|\(read|eve:|note:)", re.I)


def call_kind(ctx_text: str) -> str:
    """fix24 (16 Oct 2026): 'routine' (scheduled brief), 'ondemand' or 'casual'; dial.sh writes it."""
    m = CALL_KIND_RE.search(ctx_text or "")
    return (m.group(1).lower() if m else "routine")


def is_instruction(point: str) -> bool:
    return bool(_INSTRUCTION_RE.match(point.strip().strip('"\u201c'))) or point.rstrip().endswith(":")


def brief_check(ctx_text: str, voice_text: str, *, callee: str, voice: str,
                callee_ended: bool = False) -> str:
    """fix24 (16 Oct 2026): no check for casual/on-demand calls; skip instruction lines; when the
    callee hung up, points after the last one delivered are not 'missed' (he chose to end)."""
    msg = brief_message(ctx_text)
    if not msg or call_kind(ctx_text) in ("casual", "ondemand"):
        return ""
    keep_lines = [ln for ln in msg.splitlines()
                  if not is_instruction(re.sub(r"^\s*([-*\u2022]|\d+[.)])\s*", "", ln))]
    pts = [p for p in brief_points("\n".join(keep_lines)) if not is_instruction(p)
           and not re.search(r"^[\"\u201c]|[\"\u201d]$", p)]
    missed = set(missed_points("\n".join(pts), voice_text))
    if callee_ended and missed:
        last_hit = max((i for i, p in enumerate(pts) if p not in missed and _keys(p)), default=-1)
        missed = {p for i, p in enumerate(pts) if p in missed and i < last_hit}
    out = [p for p in pts if p in missed]
    return record_missed(out, callee=callee, voice=voice)
