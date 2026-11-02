"""Title/abstract translation: shared LLM, official MT endpoints, or keyless free endpoints."""
from __future__ import annotations

import os
import time

import httpx

from .llm import _quiet_http_debug
from .models import Paper, ProcessingError, Translation


GOOGLE_FREE_URL = "https://translate.googleapis.com/translate_a/single"
MICROSOFT_FREE_AUTH_URL = "https://edge.microsoft.com/translate/auth"
MICROSOFT_FREE_TRANSLATE_URL = "https://api-edge.cognitive.microsofttranslator.com/translate"

# Providers that do not need an API key at all.
KEYLESS_PROVIDERS = {"google_free", "microsoft_free"}
MACHINE_PROVIDERS = {"deepl", "libretranslate", "google_free", "microsoft_free"}


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


def _microsoft_texts(client: httpx.Client, texts: list[str], timeout: float) -> list[str]:
    """Translate all texts with one Edge Translator POST after an auth GET."""
    auth = client.get(MICROSOFT_FREE_AUTH_URL, timeout=timeout)
    auth.raise_for_status()
    token = auth.text.strip()
    if not token:
        raise ProcessingError("translation_auth_token_missing")
    response = client.post(
        MICROSOFT_FREE_TRANSLATE_URL,
        params={"api-version": "3.0", "from": "en", "to": "zh-Hans"},
        json=[{"Text": text} for text in texts],
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        timeout=timeout,
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, list) or len(data) != len(texts):
        raise ProcessingError("translation_response_invalid")
    values: list[str] = []
    for item in data:
        try:
            value = item["translations"][0]["text"]
        except (TypeError, KeyError, IndexError):
            raise ProcessingError("translation_response_invalid") from None
        values.append(value)
    return values


class TranslationClient:
    def __init__(self, config, analysis, client=None, *, sleep=time.sleep):
        self.settings = config["translation"]
        self.analysis = analysis
        self.client = client
        self.sleep = sleep

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
        for attempt in range(cfg["attempts"]):
            self.analysis._guard()
            timeout = self.analysis._remaining_seconds(cfg["timeout"])
            if timeout <= 0.1:
                raise ProcessingError("translation_run_time_guard")
            try:
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
                    values = [_google_text(client, text, timeout) for text in texts]
                else:  # microsoft_free
                    values = _microsoft_texts(client, texts, timeout)
                if not isinstance(values, list) or len(values) != len(texts) or any(not isinstance(v, str) or not v.strip() for v in values):
                    raise ProcessingError("translation_response_invalid")
                return Translation(values[0], "" if title_only else values[1])
            except (httpx.HTTPError, ValueError, KeyError, TypeError, IndexError, ProcessingError) as exc:
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code not in {408, 429, 500, 502, 503, 504}:
                    raise ProcessingError("translation_http_nonretryable") from None
                if attempt == cfg["attempts"] - 1:
                    raise ProcessingError("translation_request_failed") from None
                delay = min(cfg["retry_delay_seconds"] * 2**attempt, 8)
                self.sleep(min(delay, self.analysis._remaining_seconds(delay)))
        raise ProcessingError("translation_request_failed")
