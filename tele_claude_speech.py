"""Speech-to-text port and adapters.

Hexagonal shape: one abstract base (``SpeechToTextPort``) plus two
concrete adapters (``ElevenLabsAdapter``, ``SelfHostedAdapter``). The
factory at the bottom resolves the active adapter from env, with an
optional override so the "Try other provider" callback can swap
providers without mutating the environment.

This module imports nothing from ``tele_claude.py`` — strict one-way
dependency (handler → speech, never the reverse). Keeps the port
testable in isolation.

Env vars:
    TELE_CLAUDE_STT_PROVIDER     ``elevenlabs`` (default) or ``selfhost``
    TELE_CLAUDE_STT_API_KEY      API key for the active provider; may be
                                 ``None`` (the stub adapter raises anyway)
    TELE_CLAUDE_STT_BASE_URL     Base URL for the self-hosted adapter
    TELE_CLAUDE_STT_TAG_EVENTS   ``1`` to let Scribe inline non-speech
                                 event labels (e.g. ``(laughter)``,
                                 ``(youthful music)``) into the
                                 transcript. Off by default — those
                                 tags pollute dictation when the
                                 recording captures background sound.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from pathlib import Path

import aiohttp

import tele_claude_constants as constants


# ---------- Errors ----------


class TranscriptionError(Exception):
    """Raised when speech-to-text fails.

    The message is rendered to the user in the Telegram error reply, so
    keep it short and human-readable. The handler shows ``str(err)``
    verbatim — no separate "user-facing" / "internal" message split.
    """


# ---------- Port (the abstract) ----------


class SpeechToTextPort(ABC):
    """Audio file on disk → transcript string. One method, one job.

    Adapters keep state limited to construction-time configuration
    (api key, base url). No instance is reused across requests; the
    factory builds a fresh adapter per call.
    """

    @abstractmethod
    async def transcribe(self, audio_path: Path) -> str:
        """Transcribe the audio file at ``audio_path``.

        Raises:
            TranscriptionError: any failure (network, auth, format,
                empty audio, etc.). Caller renders ``str(err)`` to
                the user.
        """


# ---------- Adapters ----------


class ElevenLabsAdapter(SpeechToTextPort):
    """ElevenLabs Scribe — https://elevenlabs.io/docs/api-reference/speech-to-text.

    Verified surface (May 2026 docs):
      POST  https://api.elevenlabs.io/v1/speech-to-text
      header  xi-api-key: <key>
      body    multipart/form-data
        file       <binary audio>
        model_id   scribe_v1
      response  {"text": "...", "words": [...], "language_code": "..."}
    """

    _ENDPOINT = "https://api.elevenlabs.io/v1/speech-to-text"
    _MODEL_ID = "scribe_v1"
    # Telegram caps voice notes at 60 min, but typical dictation is under
    # 2 min; 60s comfortably covers Scribe's processing time for that
    # length. Bump if users start hitting timeouts on longer recordings.
    _TIMEOUT_SECONDS = 60

    def __init__(self, api_key: str | None, tag_events: bool = False) -> None:
        self._api_key = api_key
        self._tag_events = tag_events

    async def transcribe(self, audio_path: Path) -> str:
        if not self._api_key:
            raise TranscriptionError(
                "ElevenLabs API key not set (TELE_CLAUDE_STT_API_KEY)"
            )
        try:
            data = await self._post(audio_path)
        except aiohttp.ClientError as e:
            raise TranscriptionError(f"network error: {e}") from e
        except TimeoutError as e:
            raise TranscriptionError("ElevenLabs request timed out") from e

        text = ""
        if isinstance(data, dict):
            raw = data.get("text")
            if isinstance(raw, str):
                text = raw.strip()
        if not text:
            raise TranscriptionError("empty transcript")
        return text

    async def _post(self, audio_path: Path) -> object:
        timeout = aiohttp.ClientTimeout(total=self._TIMEOUT_SECONDS)
        # Open inside the request scope — aiohttp streams the file
        # without a wasted in-memory load.
        with audio_path.open("rb") as fh:
            form = aiohttp.FormData()
            form.add_field(
                "file", fh, filename=audio_path.name, content_type="audio/ogg"
            )
            form.add_field("model_id", self._MODEL_ID)
            form.add_field(
                "tag_audio_events", "true" if self._tag_events else "false"
            )
            headers = {"xi-api-key": self._api_key or ""}
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    self._ENDPOINT, data=form, headers=headers
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        raise TranscriptionError(
                            f"ElevenLabs HTTP {resp.status}: {body[:200]}"
                        )
                    return await resp.json()


class SelfHostedAdapter(SpeechToTextPort):
    """Stub. Real implementation lands once the self-hosted API docs
    are provided. Constructor stores config so the eventual impl can
    use it without changing call sites.
    """

    def __init__(self, base_url: str | None, api_key: str | None) -> None:
        self._base_url = base_url
        self._api_key = api_key

    async def transcribe(self, audio_path: Path) -> str:  # noqa: ARG002 — kept for ABC contract
        raise NotImplementedError(
            "self-hosted STT adapter not yet implemented — "
            "set TELE_CLAUDE_STT_PROVIDER=elevenlabs for now"
        )


# ---------- Factory ----------


def get_stt_port(provider: str | None = None) -> SpeechToTextPort:
    """Resolve the active STT adapter.

    Args:
        provider: optional override. If None, reads
            ``TELE_CLAUDE_STT_PROVIDER`` (default ``"elevenlabs"``).
            The retry callbacks pass an explicit override so swapping
            providers doesn't require touching the environment.

    Raises:
        ValueError: unknown provider name. Misconfiguration is a
            startup-class error, distinct from runtime STT failures —
            handler renders without retry buttons.
    """
    name = (
        provider
        or os.environ.get("TELE_CLAUDE_STT_PROVIDER", "elevenlabs")
    ).lower()
    api_key = os.environ.get("TELE_CLAUDE_STT_API_KEY") or None
    tag_events = constants.env_truthy("TELE_CLAUDE_STT_TAG_EVENTS")

    if name == "elevenlabs":
        return ElevenLabsAdapter(api_key=api_key, tag_events=tag_events)
    if name == "selfhost":
        base_url = os.environ.get("TELE_CLAUDE_STT_BASE_URL") or None
        return SelfHostedAdapter(base_url=base_url, api_key=api_key)
    raise ValueError(
        f"unknown STT provider: {name!r} "
        f"(set TELE_CLAUDE_STT_PROVIDER to 'elevenlabs' or 'selfhost')"
    )


def other_provider(current: str) -> str:
    """Return the opposite provider name.

    Used by the "Try other provider" callback so the swap logic
    doesn't have to be duplicated. Anything other than ``elevenlabs``
    flips to ``elevenlabs`` (defensive default — if the saved provider
    is corrupted or stale, swap-from-unknown lands on the working one).
    """
    return "selfhost" if current.lower() == "elevenlabs" else "elevenlabs"
