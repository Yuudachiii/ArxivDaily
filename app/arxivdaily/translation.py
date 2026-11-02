"""Title/abstract translation: shared LLM, official MT endpoints, or keyless free endpoints."""
from __future__ import annotations

import os
import time

import httpx

from .llm import _quiet_http_debug
from .models import Paper, ProcessingError, Translation


GOOGLE_FREE_URL = "https://translate.googleapis.com/translate_a/single"

# Providers that do not need an API key at all.
KEYLESS_PROVIDERS = {"google_free", "microsoft_free"}
MACHINE_PROVIDERS = {"deepl", "libretranslate", "google_free", "microsoft_free"}

# The direct Google endpoint throttles large days with 429/403. Other providers
# keep the historical mapping (403 is not retried, e.g. a bad key).
_RETRYABLE_STATUSES = {408, 429, 500, 502, 503, 504}
_GOOGLE_RETRYABLE_STATUSES = _RETRYABLE_STATUSES | {403}


def _load_translators():
    """Lazy import: rules-mode runs and tests must not require the dependency."""
    try:
        import translators
    except ImportError:
        raise ProcessingError("translation_dependency_missing") from None
    return translators


def _google_text(client: httpx.Client, text: str, timeout: float) -> str:
    """Translate one text through the public Google web endpoint."""
    response = client.get(
        GOOGLE_FREE_URL,
        params={"client": "gtx", "sl": "en", "tl": "zh-CN", "dt": "t", "q": text},
        timeout=timeout,
    )
    response.raise_for_status()
    data = response.json()
    segments = data[0] if isinstance(data, list) and data else None
    if not isinstance(segments, list):
        raise ProcessingError("translation_response_invalid")
    value = "".join(
        segment[0] for segment in segments
        if isinstance(segment, list) and segment and isinstance(segment[0], str)
    )
    if not value.strip():
        raise ProcessingError("translation_response_invalid")
    return value


def _bing_texts(texts: list[str], guard=None, timeout=None) -> list[str]:
    """Translate texts with the third-party ``translators`` Bing engine.

    The library is imported lazily so offline rules processing and the test
    suite do not need it installed. No region environment variable is set; the
    engine must resolve its own region as it did in the verified Actions probe.
    ``timeout`` may be a number or a callable returning the remaining budget.
    """
    translators = _load_translators()
    values: list[str] = []
    for text in texts:
        if guard is not None:
            guard()
        seconds = timeout() if callable(timeout) else timeout
        if seconds is not None and seconds <= 0.1:
            raise ProcessingError("translation_run_time_guard")
        try:
            kwargs = {} if seconds is None else {"timeout": seconds}
            value = translators.translate_text(
                text, translator="bing", from_language="en", to_language="zh", **kwargs
            )
        except ProcessingError:
            raise
        except Exception:
            # Library-specific exceptions are mapped onto the existing
            # retryable translation failure path.
            raise ProcessingError("translation_provider_unavailable") from None
        if not isinstance(value, str) or not value.strip():
            raise ProcessingError("translation_response_invalid")
        values.append(value)
    return values


class TranslationClient:
    def __init__(self, config, analysis, client=None, *, sleep=time.sleep):
        self.settings = config["translation"]
        self.analysis = analysis
        self.client = client
        self.sleep = sleep
        self._last_request_at: float | None = None

    def _pace(self) -> None:
        """Space Google requests so a 100-200 paper day stays under the quota."""
        interval = float(self.settings.get("request_interval", 0) or 0)
        if interval <= 0:
            return
        now = time.monotonic()
        if self._last_request_at is not None:
            wait = interval - (now - self._last_request_at)
            if wait > 0:
                self.sleep(min(wait, self.analysis._remaining_seconds(wait)))
        self._last_request_at = time.monotonic()

    def _google_values(self, client: httpx.Client, texts: list[str]) -> list[str]:
        values: list[str] = []
        for text in texts:
            self._pace()
            timeout = self.analysis._remaining_seconds(self.settings["timeout"])
            if timeout <= 0.1:
                raise ProcessingError("translation_run_time_guard")
            values.append(_google_text(client, text, timeout))
        return values

    def translate(self, paper: Paper, *, title_only=False) -> Translation:
        # Fixture runs must remain offline regardless of the selected backend.
        if self.settings["provider"] == "llm" or hasattr(self.analysis, "fixture"):
            return self.analysis.translate(paper, title_only=title_only)
        provider = self.settings["provider"]
        if provider not in MACHINE_PROVIDERS:
            raise ProcessingError("translation_provider_unsupported")
        self.analysis._begin_stage("translation")
        _quiet_http_debug()
        cfg = self.settings
        texts = [paper.title] if title_only else [paper.title, paper.abstract]
        # Only providers with an official key gate on a credential.
        key = os.environ.get(cfg["api_key_env"], "") if provider not in KEYLESS_PROVIDERS else ""
        if provider == "deepl" and not key:
            raise ProcessingError("translation_key_missing")
        retryable = _GOOGLE_RETRYABLE_STATUSES if provider == "google_free" else _RETRYABLE_STATUSES
        for attempt in range(cfg["attempts"]):
            self.analysis._guard()
            timeout = self.analysis._remaining_seconds(cfg["timeout"])
            if timeout <= 0.1:
                raise ProcessingError("translation_run_time_guard")
            try:
                if provider in {"deepl", "libretranslate", "google_free"}:
                    client = self.client or self.analysis.client
                if provider == "deepl":
                    response = client.post(cfg["base_url"].rstrip("/") + "/v2/translate",
                                           json={"text": texts, "source_lang": "EN", "target_lang": "ZH"},
                                           headers={"Authorization": "DeepL-Auth-Key " + key}, timeout=timeout)
                    response.raise_for_status()
                    values = [item["text"] for item in response.json()["translations"]]
                elif provider == "libretranslate":
                    payload = {"q": texts, "source": "en", "target": "zh", "format": "text"}
                    if key:
                        payload["api_key"] = key
                    response = client.post(cfg["base_url"].rstrip("/") + "/translate", json=payload, timeout=timeout)
                    response.raise_for_status()
                    values = response.json()["translatedText"]
                elif provider == "google_free":
                    values = self._google_values(client, texts)
                else:  # microsoft_free
                    values = _bing_texts(
                        texts,
                        guard=self.analysis._guard,
                        timeout=lambda: self.analysis._remaining_seconds(cfg["timeout"]),
                    )
                if not isinstance(values, list) or len(values) != len(texts) or any(not isinstance(v, str) or not v.strip() for v in values):
                    raise ProcessingError("translation_response_invalid")
                return Translation(values[0], "" if title_only else values[1])
            except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError, ProcessingError) as exc:
                if isinstance(exc, ProcessingError) and str(exc) == "translation_dependency_missing":
                    raise
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code not in retryable:
                    raise ProcessingError("translation_http_nonretryable") from None
                if attempt == cfg["attempts"] - 1:
                    raise ProcessingError("translation_request_failed") from None
                delay = min(cfg["retry_delay_seconds"] * 2**attempt, 8)
                self.sleep(min(delay, self.analysis._remaining_seconds(delay)))
        raise ProcessingError("translation_request_failed")
