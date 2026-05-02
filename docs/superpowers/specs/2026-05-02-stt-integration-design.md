# Speech-to-Text Integration — Design Spec

**Date:** 2026-05-02
**Status:** Draft, awaiting user review
**Topic:** Add voice-message → transcript → confirm → forward-to-pane flow to the
Telegram bot, with a port-and-adapter shape that supports two providers
(ElevenLabs Scribe, self-hosted stub).

---

## Problem

The bot currently rejects voice notes (`tele_claude.py:1741`: filters explicitly
exclude audio). Hands-free dictation from a phone — the canonical use case for
voice messages — is impossible. We want voice notes to be transcribed and
forwarded to the active Claude pane, with a confirmation gate so misheard
identifiers/commands never silently reach Claude.

## Non-goals

- Audio file uploads (`.mp3`, `.m4a`) — voice notes only for v1.
- Persisted transcripts (no DB, no history).
- Streaming / real-time STT.
- Refactoring the existing flat-file codebase to full DDD. The new module is
  port-and-adapter shaped; the rest of the bot stays as-is.
- Touching the existing typed-text flow. Voice handling is purely additive.

---

## UX

| Stage | What you see in the chat |
|-------|--------------------------|
| Send a voice note | Voice note appears in the chat as usual |
| Bot transcribes | Bot replies with `🎤 Transcript (%PANE):\n<text>` and `[✅ Send]` `[❌ Cancel]` buttons |
| Tap `✅ Send` | Card edits to `✅ Sent to %PANE`; transcript is sent to the pane via the same path as a typed message |
| Tap `❌ Cancel` | Card edits to `❌ Cancelled`; pane untouched |
| STT fails | Card reads `❌ STT failed: <reason>` with `[🔁 Retry]` `[🔁 Try other provider]` buttons |
| Retry succeeds | Card morphs into the success confirm card (retry buttons replaced by Send/Cancel) |
| TTL expires before tap | Next callback finds no pending state → card edits to `⌛ expired`, buttons cleared |

### Button-lifecycle invariants

- Terminal states (`Sent`, `Cancelled`, `expired`) **always** clear `reply_markup`.
- Transient error states keep `[Retry]` `[Try other provider]` until success or cancel.
- A successful retry **replaces** the retry buttons with Send/Cancel — they never
  coexist.

---

## Architecture

### Module boundary

One new file: `tele_claude_speech.py`. Internally sectioned:

```
Section 1: Errors            class TranscriptionError(Exception)
Section 2: Port              class SpeechToTextPort(ABC)
                                @abstractmethod
                                async transcribe(audio_path: Path) -> str
Section 3: Adapters          class ElevenLabsAdapter(SpeechToTextPort)
                             class SelfHostedAdapter(SpeechToTextPort)  # stub
Section 4: Factory           def get_stt_port(provider=None) -> SpeechToTextPort
                             def other_provider(current: str) -> str
```

Strict one-way dependency: handler → speech, never the reverse. `tele_claude_speech`
imports nothing from `tele_claude.py`. This keeps the port testable in isolation
later if test infra is added.

### Why this shape (and not full DDD)

The user's original framing was "DDD with abstract first, then adapters." The
honest fit for that framing is hexagonal / ports-and-adapters, not full
aggregate-style DDD — there are no aggregates here, no entities with identity,
no commands/events. A transcript is a transient string on the way to
`tmux send-keys`. Adding `VoiceMessage` / `TranscriptionResult` value objects
plus an application layer would be ceremony with no payoff. The single-file
module honors the abstract-then-adapter framing without inventing a domain that
doesn't exist.

### Why one file, not a subpackage

The existing codebase is flat-module (`tele_claude_*.py`). One file matches that
convention and is the smallest unit that still expresses the port/adapter split
through internal sectioning. If the self-hosted adapter grows complex enough to
warrant its own file, splitting later is a 5-minute refactor.

---

## Component contracts

### Port

```python
class TranscriptionError(Exception):
    """Raised when speech-to-text fails. The message is shown to the user
    in the Telegram error reply, so keep it short and human-readable."""


class SpeechToTextPort(ABC):
    @abstractmethod
    async def transcribe(self, audio_path: Path) -> str:
        """Transcribe the audio file at `audio_path`.

        Raises TranscriptionError on any failure (network, auth, format,
        empty audio, etc.). Caller renders `str(err)` to the user.
        """
```

**Why path, not bytes:** The handler already writes the voice note to disk
(mirrors the existing image/file caching pattern). Adapters that want bytes can
read the file; HTTP-multipart adapters stream directly without a wasted
in-memory load.

**Why plain `str` out:** YAGNI. ElevenLabs returns confidence/timestamps/language
but nothing in the bot consumes them. Defer a richer result type until there's
a real use case.

**Why one exception type:** The bot's only branching on errors is "show the
message." No retry-on-network-but-not-on-auth logic. The retry button retries
unconditionally; the swap button swaps unconditionally. No introspection needed.

### ElevenLabs adapter

POST `multipart/form-data` to `https://api.elevenlabs.io/v1/speech-to-text` with
`file=<audio>` + `model_id=scribe_v1`, header `xi-api-key: <key>`. Read `text`
from JSON response. 60s timeout. Empty transcript → `TranscriptionError("empty
transcript")`. Non-200 → `TranscriptionError("ElevenLabs HTTP <code>: <body[:200]>")`.

> **API surface caveat:** the endpoint path, model id, and response shape are
> sketched from memory. Verified against current ElevenLabs docs at
> implementation time, before the wire-up commit.

### Self-hosted adapter

Stub. Constructor accepts `base_url` and `api_key` (both optional). `transcribe()`
raises `NotImplementedError("self-hosted STT adapter not yet implemented — set
TELE_CLAUDE_STT_PROVIDER=elevenlabs for now")`. Real implementation lands when
the user provides API docs.

### Factory

```python
def get_stt_port(provider: str | None = None) -> SpeechToTextPort:
    name = (provider or os.environ.get("TELE_CLAUDE_STT_PROVIDER", "elevenlabs")).lower()
    api_key = os.environ.get("TELE_CLAUDE_STT_API_KEY") or None
    if name == "elevenlabs":
        return ElevenLabsAdapter(api_key=api_key)
    if name == "selfhost":
        base_url = os.environ.get("TELE_CLAUDE_STT_BASE_URL") or None
        return SelfHostedAdapter(base_url=base_url, api_key=api_key)
    raise ValueError(
        f"unknown STT provider: {name!r} "
        f"(set TELE_CLAUDE_STT_PROVIDER to 'elevenlabs' or 'selfhost')"
    )


def other_provider(current: str) -> str:
    return "selfhost" if current.lower() == "elevenlabs" else "elevenlabs"
```

- Built per-call, not cached. Adapters are stateless beyond `api_key`; env var
  changes take effect on the next message without a restart.
- `provider=` override exists so the "Try other provider" button can swap
  without mutating the env var.
- `ValueError` (not `TranscriptionError`) for unknown provider — misconfiguration
  is a startup-class error; rendered without retry buttons.

---

## Configuration

Three new env vars in `~/.config/tele-claude/env`:

| Var | Purpose | Default |
|-----|---------|---------|
| `TELE_CLAUDE_STT_PROVIDER` | `elevenlabs` or `selfhost` | `elevenlabs` |
| `TELE_CLAUDE_STT_API_KEY` | API key for the active provider; may be `None` | unset |
| `TELE_CLAUDE_STT_BASE_URL` | Base URL for self-hosted; only consumed by `SelfHostedAdapter` | unset |
| `TELE_CLAUDE_STT_PENDING_TTL_SECONDS` | TTL for unconfirmed pending transcripts | `900` (15 min) |

New constants in `tele_claude_constants.py`:

```python
VOICE_DIR = STATE_DIR / "voice"
PENDING_VOICE_DIR = STATE_DIR / "pending_voice"
PENDING_VOICE_TTL_SECONDS = int(
    os.environ.get("TELE_CLAUDE_STT_PENDING_TTL_SECONDS", "900")
)
```

New dependency in `pyproject.toml`: `aiohttp`. Already pulled in transitively
by `python-telegram-bot`, so the install footprint is zero — but we declare it
explicitly to avoid relying on a transitive dep.

---

## Data flow

```
You record voice note → Telegram delivers Voice update
        │
        ▼
on_voice(update, context):
  1. Auth check (chat_id in CLAUDE_TELEGRAM_CHAT_ID).
  2. Resolve target pane (forum topic → pane, else active pane).
     If none: reply "❌ no active pane — /panes first" and bail.
  3. Download voice .ogg → ~/.cache/tele-claude/voice/tg_<msg>_<uniq>.ogg
     (mirrors existing image/file pattern, sanitised path).
  4. provider = active provider name (env-resolved or default).
     Persist initial pending state to
     ~/.cache/tele-claude/pending_voice/<chat>_<msg>.json:
       {audio_path, target_pane, expires_at, provider, transcript: null}
     port = get_stt_port(provider)
     try:
       transcript = await port.transcribe(audio_path)
     except TranscriptionError as e:
       reply "❌ STT failed: <e>" + [🔁 Retry] [🔁 Try other provider]
       (pending state already persisted, transcript stays null); bail.
     except NotImplementedError as e:
       same handling — retry against same provider is useless, but
       "Try other" still works. Bail.
     except ValueError as e:
       reply "❌ STT misconfigured: <e>" — NO retry buttons.
       Delete pending state file. Bail.
  5. On success: update pending state with transcript field set.
  6. Reply with confirm card:
       🎤 Transcript (%PANE):
       <transcript>
       [✅ Send] [❌ Cancel]
     callback_data: "voice:send:<chat>:<msg>" / "voice:cancel:<chat>:<msg>"
```

### Callback handlers (extend `on_callback`)

All callbacks load a single pending-state file
(`~/.cache/tele-claude/pending_voice/<chat>_<msg>.json`) — same file for
success and error paths; `transcript` is `null` until STT succeeds.

| Prefix | Action |
|--------|--------|
| `voice:send:<chat>:<msg>` | Load pending state. If `transcript` is null (shouldn't happen — Send button is only shown after success): no-op error toast. Else: forward transcript via the existing send-to-pane path, edit card to `✅ Sent to %PANE`, clear `reply_markup`, delete pending file + audio file. |
| `voice:cancel:<chat>:<msg>` | Delete pending file + audio file, edit card to `❌ Cancelled`, clear `reply_markup`. |
| `voice:retry:<chat>:<msg>` | Load pending state, re-run `port.transcribe()` with the saved `provider`. On success: write `transcript` back to pending file, edit card → confirm card (Send/Cancel buttons). On failure: leave pending file as-is, edit card → fresh error card (Retry/Swap buttons). |
| `voice:swap:<chat>:<msg>` | Load pending state, swap `provider = other_provider(saved_provider)` and write back to pending file, run new adapter, edit card per same success/failure rules as Retry. |

Callback-data format keeps total bytes well under Telegram's 64-byte limit
for any realistic chat/message id.

### TTL cleanup

Pending files older than `PENDING_VOICE_TTL_SECONDS` are swept on bot startup
(same defensive pattern as `tele_claude_questions.py`). `on_callback` also
checks per-call: a missing pending file means the entry has expired or been
processed → edit card to `⌛ expired`, clear `reply_markup`. The audio file is
deleted on terminal callbacks (Send/Cancel/expired-cleanup) to avoid disk
accretion.

---

## Wiring into `tele_claude.py`

Single insertion in `main()`:

```python
app.add_handler(MessageHandler(filters.VOICE, on_voice))
```

Slotted **before** the existing `filters.TEXT & ~filters.COMMAND` handler.
`filters.VOICE` and `filters.TEXT` are disjoint, so ordering is for clarity, not
correctness.

Single extension in `on_callback`: a new branch for `data.startswith("voice:")`
that dispatches to the four sub-handlers above. Existing prefixes
(`pane:`, `q:`, `perm:`, etc.) are not touched.

Helper extraction: the body of `on_message` that pushes text to the active pane
becomes a small helper (`_send_text_to_pane(chat_id, pane, text, ...)`) so both
`on_message` and the `voice:send` callback share one implementation. Pure
refactor — no behavior change.

---

## Error catalogue

| Origin | Exception | UX |
|--------|-----------|-----|
| Bad provider name in env | `ValueError` from factory | `❌ STT misconfigured: <msg>` (no buttons) |
| Missing API key | `TranscriptionError` from adapter | `❌ STT failed: ElevenLabs API key not set …` + Retry/Swap |
| Network / timeout | `TranscriptionError` | `❌ STT failed: network error: …` + Retry/Swap |
| Provider HTTP 4xx/5xx | `TranscriptionError` | `❌ STT failed: ElevenLabs HTTP <code>: <body[:200]>` + Retry/Swap |
| Empty transcript | `TranscriptionError` | `❌ STT failed: empty transcript` + Retry/Swap |
| Self-hosted adapter called | `NotImplementedError` | `❌ STT failed: self-hosted STT adapter not yet implemented …` + Retry/Swap |
| Pending state expired | (no exception, callback miss) | `⌛ expired`, no buttons |
| No active pane | (no exception, early bail) | `❌ no active pane — /panes first` |

---

## Out-of-scope details (deferred)

- **Caption-on-voice handling.** Voice notes can have captions in Telegram. Rare
  in practice; treat caption as ignored for v1. Easy to add later (concat to
  transcript or prepend).
- **Long-audio chunking.** ElevenLabs has audio length limits. v1 surfaces the
  provider's error verbatim; chunking can come if it actually bites.
- **Per-chat provider override.** All v1 chats share one provider. Per-chat
  state in `state.json` is a small additive change later.
- **Rich result type.** Confidence scores / language codes / timestamps are
  available from Scribe but not consumed.
- **Tests.** Repo has no test infra. Adding pytest + a fake adapter for the
  port is straightforward when the team is ready, but it's not part of v1.

---

## Implementation checklist (for the plan that follows)

1. Add `aiohttp` to `pyproject.toml` dependencies; refresh `uv.lock`.
2. Add new constants to `tele_claude_constants.py`.
3. Create `tele_claude_speech.py` with errors, port, two adapters, factory.
4. Extract `_send_text_to_pane` helper in `tele_claude.py` (refactor only).
5. Add `on_voice` handler in `tele_claude.py`.
6. Extend `on_callback` with `voice:*` branch.
7. Register `MessageHandler(filters.VOICE, on_voice)` in `main()`.
8. Add startup sweep for expired pending-voice files.
9. Update README — new env vars, voice-flow section, troubleshooting.
10. Verify ElevenLabs API surface against current docs before the wire-up
    commit; adjust endpoint / model id / response parsing if needed.
