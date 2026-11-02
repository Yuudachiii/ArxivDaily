"""Public, credential-free configuration for the daily digest."""
from __future__ import annotations

from copy import deepcopy
import ipaddress
import math
from pathlib import Path
import re
import tomllib
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


DEFAULTS = {
    "presentation": {
        "title": True, "title_translation": True,
        "abstract": True, "abstract_translation": True,
        "llm_summary": False, "evidence": False,
        "source": True,
        "screening_routes": True,
    },
    "prompts": {"directory": ""},
    "selection": {"mode": "llm", "route_priority": ["medical_reasoning", "medical_objective", "general_rl", "transferable_objective", "general_reasoning"]},
    "translation": {
        "provider": "llm", "label": "", "base_url": "https://api-free.deepl.com",
        "api_key_env": "TRANSLATION_API_KEY", "timeout": 30, "attempts": 3,
        "retry_delay_seconds": 1, "request_interval": 1.0, "stage_seconds": 900,
    },
    "collection": {
        "page_size": 100, "delay_seconds": 3.0, "initial_days": 1,
        "lookback_days": 1, "shard_limit": 10000, "max_candidates": 10000,
    },
    "rules": {
        "categories": ["cs.CV", "cs.LG", "cs.AI", "eess.IV"],
        "title_enabled": False, "exclude": [],
        "groups": {
            "SEG": ["segmentation", "segmenting", "segmentation mask", "semantic segmentation", "instance segmentation", "pixel-level perception", "pixel-wise perception", "vessel segmentation"],
            "REASON": ["reasoning segmentation", "reasoning-based segmentation", "position reasoning", "positional reasoning", "spatial reasoning", "medical reasoning", "implicit query", "implicit queries", "implicit instruction", "complex instruction", "chain-of-thought", "chain of thought", "visual reasoning"],
            "VLM": ["vision-language model", "vision language model", "multimodal large language model", "multi-modal large language model", "MLLM", "VLM", "large language model", "language-guided segmentation", "text-guided segmentation"],
            "MED": ["medical", "biomedical", "clinical", "radiology", "radiologist", "anatomical", "anatomy", "pathology", "lesion", "tumor", "tumour", "MRI", "CT", "X-ray", "ultrasound", "endoscopy", "angiography", "coronary", "retinal", "histopathology", "polyp"],
            "RL_PREF": ["reinforcement learning", "direct preference optimization", "preference optimization", "preference alignment", "preference-based learning", "policy optimization", "reward optimization", "RLHF", "DPO", "PPO", "GRPO", "RL"],
            "OBJECTIVE": ["loss", "loss function", "objective", "optimization", "regularization", "boundary", "topological", "topology", "connectivity", "Betti", "structural constraint", "clDice"],
            "SAM": ["segment anything", "segment anything model", "SAM", "SAM2", "SAM 2", "MedSAM"],
            "THEORY_HINT": ["theoretical", "theory", "provable", "theorem", "convergence", "generalization bound", "guarantee", "transferability", "transferable"],
        },
        "routes": {
            "medical_reasoning": [["MED", "SEG", "REASON"], ["MED", "SEG", "VLM"]],
            "medical_objective": [["MED", "SEG", "RL_PREF"], ["MED", "SEG", "OBJECTIVE"]],
            "general_rl": [["SEG", "REASON", "RL_PREF"], ["SEG", "VLM", "RL_PREF"]],
            "transferable_objective": [["SEG", "OBJECTIVE"], ["SEG", "SAM"]],
            "general_reasoning": [["SEG", "REASON"]],
        },
    },
    "limits": {"full_daily_limit": 5, "review_daily_limit": 2, "stage_attempts": 3, "recovery_days": 14, "run_seconds": 2400},
    "llm": {
        "filter": {"provider": "deepseek", "protocol": "chat_completions", "base_url": "https://api.deepseek.com", "model": "deepseek-flash", "api_key_env": "DEEPSEEK_API_KEY", "effort": "high", "max_tokens": 16384, "timeout": 120, "stage_seconds": 180, "attempts": 3, "retry_delay_seconds": 1, "image_capable": False, "input_chars": 30000},
        "summary": {"provider": "deepseek", "protocol": "chat_completions", "base_url": "https://api.deepseek.com", "model": "deepseek-flash", "api_key_env": "DEEPSEEK_API_KEY", "effort": "max", "max_tokens": 65536, "timeout": 240, "stage_seconds": 900, "attempts": 3, "retry_delay_seconds": 1, "image_capable": True, "input_chars": 120000, "max_section_calls": 64},
        "pricing": {"cache_hit_per_million": 0.04, "cache_miss_per_million": 2.0, "output_per_million": 8.0, "currency": "CNY", "policy": "peak_upper_bound"},
        "max_run_cost": None,
    },
    "fulltext": {"timeout": 30, "attempts": 3, "min_chars": 2500, "chunk_chars": 20000, "max_download_bytes": 25000000, "max_visual_pages": 8},
    "delivery": {"sendkey_env": "SERVERCHAN_SENDKEY", "daily_limit": 5, "chain_attempts": 3, "poll_limit": 3, "body_bytes": 32768, "quota_timezone": "Asia/Shanghai", "query_counts_quota": False},
    "archive": {"public_base_url": "", "state_branch": "arxivdaily-state"},
}


def _merge(default: dict, supplied: dict, path: str = "") -> dict:
    result = deepcopy(default)
    for key, value in supplied.items():
        here = f"{path}.{key}" if path else key
        if key not in default:
            raise ValueError(f"unknown config key: {here}")
        if isinstance(default[key], dict):
            if not isinstance(value, dict):
                raise ValueError(f"config table expected: {here}")
            # Supplied route names replace the built-in scheme, so a project can
            # fully repurpose its selection routes. When the file also replaces
            # the routes, its groups are a complete replacement too; a file that
            # only adds groups keeps the built-in groups for its default routes.
            if here == "rules.routes":
                result[key] = deepcopy(value)
            elif here == "rules.groups":
                result[key] = deepcopy(value) if "routes" in supplied else deepcopy(default[key]) | deepcopy(value)
            else:
                result[key] = _merge(default[key], value, here)
        else:
            result[key] = value
    return result


def _integer(value: object, name: str, minimum: int, maximum: int) -> None:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be an integer from {minimum} to {maximum}")


def _number(value: object, name: str, minimum: float) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < minimum:
        raise ValueError(f"{name} must be a finite number at least {minimum}")


def _bounded_number(value: object, name: str, minimum: float, maximum: float) -> None:
    _number(value, name, minimum)
    if value > maximum:
        raise ValueError(f"{name} must be at most {maximum}")


def _https_base_url(value: object, name: str) -> None:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{name} must be a public HTTPS base URL")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname or ""
        try:
            public_host = ipaddress.ip_address(hostname).is_global
        except ValueError:
            public_host = "." in hostname and not hostname.endswith((".local", ".internal", ".localhost"))
        valid = (
            parsed.scheme == "https" and public_host
            and parsed.username is None and parsed.password is None
            and not parsed.query and not parsed.fragment
            and (parsed.port is None or 1 <= parsed.port <= 65535)
        )
    except ValueError as exc:
        raise ValueError(f"{name} must be a public HTTPS base URL") from exc
    if not valid or re.search(r"\s", value) or parsed.path.endswith("/chat/completions"):
        raise ValueError(f"{name} must be a public HTTPS base URL")


def default_config_path() -> Path:
    """Use the repository configuration regardless of the caller's directory."""
    path = Path(__file__).resolve().parents[2] / "config.toml"
    if not path.is_file():
        raise ValueError("configuration not found; supply --config PATH")
    return path


def load_config(path: str | Path | None = None) -> dict:
    """Load TOML over defaults; only names of environment secrets are accepted."""
    supplied = {}
    if path is not None:
        with Path(path).open("rb") as handle:
            supplied = tomllib.load(handle)
    config = _merge(DEFAULTS, supplied)
    # A replaced route table must not inherit the built-in priority list; keep
    # configuration files that only define routes usable without extra noise.
    supplied_selection = supplied.get("selection")
    if (isinstance(supplied.get("rules"), dict) and "routes" in supplied["rules"]
            and not (isinstance(supplied_selection, dict) and "route_priority" in supplied_selection)):
        config["selection"]["route_priority"] = list(config["rules"]["routes"])
    branch = config["archive"]["state_branch"]
    if not isinstance(branch, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", branch) or ".." in branch or branch.endswith("/"):
        raise ValueError("archive.state_branch must be a valid branch name")
    directory = config["prompts"]["directory"]
    if not isinstance(directory, str):
        raise ValueError("prompts.directory must be a directory path")
    if directory:
        base = Path(path).resolve().parent if path is not None else Path.cwd()
        config["prompts"]["directory"] = str((base / directory).resolve())
    mode = config["selection"]["mode"]
    if mode not in {"llm", "rules"}:
        raise ValueError("selection.mode must be llm or rules")
    for key, value in config["presentation"].items():
        if type(value) is not bool:
            raise ValueError(f"presentation.{key} must be boolean")
    if not any(config["presentation"].values()):
        raise ValueError("presentation must enable at least one module")
    translation = config["translation"]
    if translation["provider"] not in {"llm", "deepl", "libretranslate", "google_free", "microsoft_free"}:
        raise ValueError("translation.provider must be llm, deepl, libretranslate, google_free, or microsoft_free")
    label = translation["label"]
    if not isinstance(label, str) or len(label) > 32 or any(ord(character) < 32 for character in label):
        raise ValueError("translation.label must be a string of at most 32 characters")
    name = translation["api_key_env"]
    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
        raise ValueError("translation.api_key_env must be an environment variable name")
    # Self-hosted LibreTranslate can run on loopback over HTTP.
    local = urlsplit(translation["base_url"]) if isinstance(translation["base_url"], str) else None
    if not (translation["provider"] == "libretranslate" and local and
            local.scheme == "http" and local.hostname in {"localhost", "127.0.0.1", "::1"}
            and not local.username and not local.password and not local.query and not local.fragment):
        _https_base_url(translation["base_url"], "translation.base_url")
    _bounded_number(translation["timeout"], "translation.timeout", 1, 300)
    _integer(translation["attempts"], "translation.attempts", 1, 10)
    _bounded_number(translation["retry_delay_seconds"], "translation.retry_delay_seconds", 0, 60)
    _bounded_number(translation["request_interval"], "translation.request_interval", 0, 60)
    _bounded_number(translation["stage_seconds"], "translation.stage_seconds", 1, 2700)
    coll = config["collection"]
    for key, upper in (("page_size", 2000), ("initial_days", 7), ("lookback_days", 14), ("shard_limit", 10000), ("max_candidates", 10000)):
        _integer(coll[key], f"collection.{key}", 1, upper)
    _number(coll["delay_seconds"], "collection.delay_seconds", 3)
    limits = config["limits"]
    for key, lower, upper in (("full_daily_limit", 1, 10), ("review_daily_limit", 0, 10), ("stage_attempts", 1, 10), ("recovery_days", 1, 14), ("run_seconds", 1, 2700)):
        _integer(limits[key], f"limits.{key}", lower, upper)
    delivery = config["delivery"]
    for key, lower, upper in (("daily_limit", 1, 5), ("poll_limit", 0, 3), ("chain_attempts", 1, 3), ("body_bytes", 1, 32768)):
        _integer(delivery[key], f"delivery.{key}", lower, upper)
    if type(delivery["query_counts_quota"]) is not bool:
        raise ValueError("delivery.query_counts_quota must be boolean")
    try:
        ZoneInfo(delivery["quota_timezone"])
    except (TypeError, ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("delivery.quota_timezone must be a valid IANA timezone") from exc
    pricing = config["llm"]["pricing"]
    for key in ("cache_hit_per_million", "cache_miss_per_million", "output_per_million"):
        _number(pricing[key], f"llm.pricing.{key}", 0)
    if not isinstance(pricing["policy"], str) or pricing["policy"] not in {"peak_upper_bound", "custom"}:
        raise ValueError("llm.pricing.policy must be peak_upper_bound or custom")
    if not isinstance(pricing["currency"], str) or not re.fullmatch(r"[A-Z]{3}", pricing["currency"]):
        raise ValueError("llm.pricing.currency must be a three-letter ISO currency")
    if pricing["policy"] == "peak_upper_bound" and any(pricing[key] != DEFAULTS["llm"]["pricing"][key] for key in ("cache_hit_per_million", "cache_miss_per_million", "output_per_million", "currency")):
        raise ValueError("custom LLM pricing must set policy = 'custom'")
    if pricing["policy"] == "peak_upper_bound" and any(
        (config["llm"][role][key].rstrip("/") if key == "base_url" and isinstance(config["llm"][role][key], str) else config["llm"][role][key]) != DEFAULTS["llm"][role][key]
        for role in ("filter", "summary") for key in ("provider", "model", "base_url")
    ):
        raise ValueError("changing LLM provider, model, or endpoint requires pricing.policy = 'custom'")
    if config["llm"]["max_run_cost"] is not None:
        _number(config["llm"]["max_run_cost"], "llm.max_run_cost", 0.01)
    for role in ("filter", "summary"):
        settings = config["llm"][role]
        name = settings["api_key_env"]
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ValueError(f"llm.{role}.api_key_env must be an environment variable name")
        if not isinstance(settings["provider"], str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]*", settings["provider"]):
            raise ValueError(f"llm.{role}.provider must be a provider name")
        if settings["protocol"] != "chat_completions":
            raise ValueError(f"llm.{role}.protocol currently supports chat_completions only")
        if not isinstance(settings["model"], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,199}", settings["model"]):
            raise ValueError(f"llm.{role}.model must be a model identifier")
        _https_base_url(settings["base_url"], f"llm.{role}.base_url")
        expected_effort = "high" if role == "filter" else "max"
        if settings["effort"] != expected_effort:
            raise ValueError(f"llm.{role}.effort must remain {expected_effort} for the confirmed thinking budget")
        _integer(settings["max_tokens"], f"llm.{role}.max_tokens", 1, 131072)
        _integer(settings["input_chars"], f"llm.{role}.input_chars", 1024, 1000000)
        _integer(settings["attempts"], f"llm.{role}.attempts", 1, 10)
        _bounded_number(settings["timeout"], f"llm.{role}.timeout", 1, 900)
        _bounded_number(settings["stage_seconds"], f"llm.{role}.stage_seconds", 1, 2700)
        _bounded_number(settings["retry_delay_seconds"], f"llm.{role}.retry_delay_seconds", 0, 60)
        if type(settings["image_capable"]) is not bool:
            raise ValueError(f"llm.{role}.image_capable must be boolean")
    _integer(config["llm"]["summary"]["max_section_calls"], "llm.summary.max_section_calls", 1, 256)
    fulltext = config["fulltext"]
    _bounded_number(fulltext["timeout"], "fulltext.timeout", 1, 300)
    for key, lower, upper in (("attempts", 1, 10), ("min_chars", 1, 1000000), ("chunk_chars", 256, 500000), ("max_download_bytes", 1024, 100000000), ("max_visual_pages", 0, 64)):
        _integer(fulltext[key], f"fulltext.{key}", lower, upper)
    name = config["delivery"]["sendkey_env"]
    if not isinstance(name, str) or not name.isidentifier():
        raise ValueError("delivery.sendkey_env must be an environment variable name")
    rules = config["rules"]
    if not isinstance(rules["categories"], list) or not all(isinstance(x, str) and re.fullmatch(r"[A-Za-z-]+\.[A-Za-z0-9-]+", x) for x in rules["categories"]):
        raise ValueError("rules.categories must be a list of category names")
    if not isinstance(rules["title_enabled"], bool):
        raise ValueError("rules.title_enabled must be boolean")
    if not isinstance(rules["exclude"], list) or not all(isinstance(x, str) for x in rules["exclude"]):
        raise ValueError("rules.exclude must be a list of phrases")
    groups, routes = rules["groups"], rules["routes"]
    if not isinstance(groups, dict) or not all(isinstance(v, list) and v and all(isinstance(x, str) and x.strip() for x in v) for v in groups.values()):
        raise ValueError("rules.groups must map names to phrase lists")
    if not isinstance(routes, dict) or not all(isinstance(v, list) and all(isinstance(c, list) and c and all(isinstance(g, str) and g in groups for g in c) for c in v) for v in routes.values()):
        raise ValueError("rules.routes must map names to OR clauses of AND groups")
    priority = config["selection"]["route_priority"]
    if (not isinstance(priority, list) or not priority
            or any(not isinstance(name, str) or name not in routes for name in priority)
            or len(set(priority)) != len(priority)):
        raise ValueError("selection.route_priority must be a non-empty list of distinct configured route names")
    return config
