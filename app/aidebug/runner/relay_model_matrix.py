from __future__ import annotations

import argparse
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import re
import sys
import time
from typing import Any, Callable


PROJECTLING_DIR = Path(__file__).resolve().parents[2]
NOTE_DIR = PROJECTLING_DIR / "aidebug" / "notes"
DEFAULT_JSON = NOTE_DIR / "projectling-relay-model-compatibility.json"
DEFAULT_MD = NOTE_DIR / "projectling-relay-model-compatibility.md"
DEFAULT_PARAMETER_JSON = NOTE_DIR / "projectling-gemini-parameter-support.json"
DEFAULT_PARAMETER_MD = NOTE_DIR / "projectling-gemini-parameter-support.md"
DEFAULT_CHANNEL_OBSERVATION = PROJECTLING_DIR / "aidebug" / "state" / "relay-channel-observation-20260710.json"

sys.path.insert(0, str(PROJECTLING_DIR))

from projectling import (  # noqa: E402
    DeepSeekClient,
    StarAPIConfig,
    _normalize_star_parameters,
    _provider_default_model,
    deepseek_usage_cache_summary,
    load_config,
)


SECRET_RE = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{8,}|Bearer\s+[A-Za-z0-9._-]{8,})\b", re.IGNORECASE)
PONG_RE = re.compile(r"^[`\"']*pong[`\"'.!。！]*$", re.IGNORECASE)
PROVIDER_ORDER = ("gpt", "gemini", "grok", "deepseek")
PROVIDER_PARAMETER_NAMES = {
    "gpt": ["reasoning_effort", "verbosity", "max_tokens"],
    "gemini": ["temperature", "top_p", "top_k", "max_tokens", "json_output"],
    "grok": ["temperature", "top_p", "reasoning_effort", "max_tokens"],
    "deepseek": ["temperature", "reasoning_effort", "max_tokens"],
}


def timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def safe_text(value: Any, *, limit: int = 500) -> str:
    text = SECRET_RE.sub("[REDACTED]", str(value or "")).replace("\r", " ").replace("\n", " ").strip()
    return text[:limit]


def is_exact_pong(value: Any) -> bool:
    normalized = "".join(str(value or "").strip().split())
    return bool(PONG_RE.fullmatch(normalized))


def has_image_output(value: Any) -> bool:
    lowered = str(value or "").lower()
    return "data:image/" in lowered or "![image](" in lowered


def model_provider(model: str, *, fallback: str = "unknown") -> str:
    lowered = str(model or "").strip().lower()
    if lowered.startswith(("gpt-", "codex-", "o1-", "o3-", "o4-")) or "codex" in lowered:
        return "gpt"
    if lowered.startswith("gemini-"):
        return "gemini"
    if lowered.startswith("grok-") or lowered.startswith("xai-"):
        return "grok"
    if lowered.startswith("deepseek-"):
        return "deepseek"
    return fallback if fallback in PROVIDER_ORDER else "unknown"


def model_category(model: str, *, provider_hint: str = "") -> str:
    lowered = model.lower()
    if "image" in lowered:
        return "image"
    if "agent" in lowered:
        return "agent"
    if lowered.startswith("claude"):
        return "claude"
    if "thinking" in lowered:
        return "thinking"
    if "lite" in lowered:
        return "lite"
    if "flash" in lowered:
        return "flash"
    if "pro" in lowered:
        return "pro"
    provider = model_provider(model, fallback=provider_hint)
    if provider == "gpt":
        return "codex"
    if provider == "grok":
        return "grok"
    if provider == "deepseek":
        return "deepseek"
    if provider == "gemini":
        return "gemini"
    return "unknown"


def supports_reasoning_probe(model: str, *, provider_hint: str = "") -> bool:
    provider = model_provider(model, fallback=provider_hint)
    lowered = str(model or "").lower()
    return provider in {"gpt", "grok"} or "thinking" in lowered or "reasoner" in lowered or lowered.startswith("deepseek-v4-")


def star_for_provider(config: Any, provider: str, *, slot: str) -> StarAPIConfig:
    normalized_provider = str(provider or "").strip().lower()
    if normalized_provider not in PROVIDER_ORDER:
        raise ValueError(f"unsupported provider: {provider}")
    normalized_slot = "executor" if str(slot or "").strip().lower() == "executor" else "main"
    base_star = config.executor_api if normalized_slot == "executor" else config.main_api
    provider_keys = {
        "gpt": config.gpt_api_key,
        "gemini": config.gemini_api_key,
        "grok": config.grok_api_key,
        "deepseek": config.deepseek_api_key,
    }
    provider_bases = {
        "gpt": config.gpt_base_url,
        "gemini": config.gemini_base_url,
        "grok": config.grok_base_url,
        "deepseek": config.deepseek_base_url,
    }
    provider_models = {
        "gpt": (config.gpt_planner_model, config.gpt_executor_model),
        "gemini": (config.gemini_planner_model, config.gemini_executor_model),
        "grok": (config.grok_planner_model, config.grok_executor_model),
        "deepseek": (config.deepseek_planner_model, config.deepseek_executor_model),
    }
    same_provider = base_star.provider == normalized_provider
    model_index = 1 if normalized_slot == "executor" else 0
    model = (
        base_star.model
        if same_provider and base_star.model
        else provider_models[normalized_provider][model_index]
        or _provider_default_model(normalized_provider, normalized_slot)
    )
    profile = base_star.parameter_profile if same_provider else "default"
    parameters = _normalize_star_parameters(
        normalized_provider,
        profile,
        model=model,
        custom=base_star.parameters if same_provider else {},
    )
    return replace(
        base_star,
        slot=normalized_slot,
        provider=normalized_provider,
        api_key=base_star.api_key if same_provider and base_star.api_key else provider_keys[normalized_provider],
        base_url=str(base_star.base_url if same_provider and base_star.base_url else provider_bases[normalized_provider]).rstrip("/"),
        model=model,
        parameter_profile=profile,
        parameters=parameters,
        key_source=base_star.key_source if same_provider else "provider",
    )


def _provider_parameter_setup(star: StarAPIConfig, name: str) -> tuple[StarAPIConfig, dict[str, Any], Any, bool]:
    provider = star.provider
    if name not in PROVIDER_PARAMETER_NAMES[provider]:
        raise ValueError(f"{provider} does not expose parameter probe {name}")
    custom: dict[str, Any] = {}
    request_kwargs: dict[str, Any] = {"temperature": None, "max_tokens": 24}
    thinking_enabled = False
    expected: Any
    if name == "temperature":
        custom["temperature"] = 0.37
        expected = 0.37
    elif name == "top_p":
        custom["top_p"] = 0.73
        expected = 0.73
    elif name == "top_k":
        custom["top_k"] = 17
        expected = 17
    elif name == "max_tokens":
        request_kwargs["max_tokens"] = 8
        expected = 8
    elif name == "json_output":
        custom["response_mime_type"] = "application/json"
        expected = "application/json"
    elif name == "reasoning_effort":
        custom["reasoning_effort"] = "ultra" if provider == "gpt" else "high" if provider == "grok" else "max"
        thinking_enabled = True
        expected = _normalize_star_parameters(provider, "default", model=star.model, custom=custom).get("reasoning_effort")
    elif name == "verbosity":
        custom["verbosity"] = "high"
        expected = "high"
    else:  # pragma: no cover - guarded by provider parameter map
        raise ValueError(name)
    parameters = _normalize_star_parameters(provider, "default", model=star.model, custom=custom)
    return replace(star, parameter_profile="default", parameters=parameters), request_kwargs, expected, thinking_enabled


def provider_parameter_payload_contract(base_config: Any, star: StarAPIConfig, name: str) -> dict[str, Any]:
    probe_star, request_kwargs, expected, thinking_enabled = _provider_parameter_setup(star, name)
    json_mode = name == "json_output"
    messages = [
        {"role": "system", "content": "Return only a JSON object with ok=true." if json_mode else "Return only pong."},
        {"role": "user", "content": "ping"},
    ]
    payload = DeepSeekClient(base_config, probe_star)._build_payload(
        messages=messages,
        tools=None,
        tool_choice="none",
        model=probe_star.model,
        thinking_enabled=thinking_enabled,
        stream=False,
        **request_kwargs,
    )
    sent_value = _parameter_payload_value(payload, name)
    return {
        "provider": probe_star.provider,
        "slot": probe_star.slot,
        "model": probe_star.model,
        "parameter": name,
        "local_sent": sent_value == expected,
        "sent_value": sent_value,
        "expected_value": expected,
        "payload_keys": sorted(str(key) for key in payload),
    }


def build_local_provider_contracts(base_config: Any) -> dict[str, Any]:
    model_overrides = {
        "gpt": "gpt-5.6-codex",
        "gemini": "gemini-3-flash",
        "grok": "grok-4-fast",
        "deepseek": "deepseek-v4-pro",
    }
    provider_contracts: dict[str, Any] = {}
    for provider in PROVIDER_ORDER:
        star = replace(
            star_for_provider(base_config, provider, slot="main"),
            api_key="fixture-local-contract",
            base_url="https://contract.invalid/v1",
            model=model_overrides[provider],
            retry_count=0,
        )
        probes = {
            name: provider_parameter_payload_contract(base_config, star, name)
            for name in PROVIDER_PARAMETER_NAMES[provider]
        }
        provider_contracts[provider] = {
            "ok": all(bool(probe.get("local_sent")) for probe in probes.values()),
            "model": star.model,
            "parameters": probes,
        }

    multimodal_messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "probe"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}},
            ],
        }
    ]
    main = replace(
        star_for_provider(base_config, "gpt", slot="main"),
        api_key="fixture-main-contract",
        model="gpt-5.6-codex",
        parameter_profile="coding",
        parameters=_normalize_star_parameters("gpt", "coding", model="gpt-5.6-codex", custom={"reasoning_effort": "ultra", "verbosity": "low"}),
    )
    executor = replace(
        star_for_provider(base_config, "grok", slot="executor"),
        api_key="fixture-executor-contract",
        model="grok-4-fast",
        parameter_profile="data",
        parameters=_normalize_star_parameters("grok", "data", model="grok-4-fast", custom={"temperature": 0.0, "reasoning_effort": "high"}),
    )
    main_payload = DeepSeekClient(base_config, main)._build_payload(
        messages=multimodal_messages,
        tools=None,
        tool_choice="none",
        temperature=None,
        stream=False,
        thinking_enabled=True,
        max_tokens=64,
    )
    executor_payload = DeepSeekClient(base_config, executor)._build_payload(
        messages=multimodal_messages,
        tools=None,
        tool_choice="none",
        temperature=None,
        stream=False,
        thinking_enabled=True,
        max_tokens=64,
    )
    isolation_checks = {
        "models": main_payload.get("model") == main.model and executor_payload.get("model") == executor.model,
        "reasoning": main_payload.get("reasoning_effort") == "ultra" and executor_payload.get("reasoning_effort") == "high",
        "provider_parameters": main_payload.get("verbosity") == "low" and "verbosity" not in executor_payload and "temperature" not in main_payload and executor_payload.get("temperature") == 0.0,
        "multimodal": isinstance(main_payload.get("messages", [{}])[0].get("content"), list) and isinstance(executor_payload.get("messages", [{}])[0].get("content"), list),
    }
    return {
        "schema_version": 1,
        "providers": provider_contracts,
        "dual_star_isolation": {
            "ok": all(isolation_checks.values()),
            "main": {"slot": main.slot, "provider": main.provider, "model": main.model},
            "executor": {"slot": executor.slot, "provider": executor.provider, "model": executor.model},
            "checks": isolation_checks,
        },
        "ok": all(bool(contract.get("ok")) for contract in provider_contracts.values()) and all(isolation_checks.values()),
    }


def probe_token_budget(model: str, probe: str) -> int:
    category = model_category(model)
    if category in {"pro", "thinking", "claude", "agent", "image"}:
        return {"text": 256, "stream": 256, "tool": 512, "thinking": 512}.get(probe, 256)
    return {"text": 32, "stream": 32, "tool": 96, "thinking": 256}.get(probe, 64)


def response_message(response: dict[str, Any]) -> dict[str, Any]:
    choices = response.get("choices") if isinstance(response.get("choices"), list) else []
    choice = choices[0] if choices and isinstance(choices[0], dict) else {}
    return choice.get("message") if isinstance(choice.get("message"), dict) else {}


def usage_summary(response: dict[str, Any]) -> dict[str, Any]:
    usage = response.get("usage") if isinstance(response.get("usage"), dict) else {}
    cache = deepseek_usage_cache_summary(usage)
    return {
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "total_tokens": usage.get("total_tokens"),
        "cached_tokens": cache.get("cache_hit_tokens"),
        "cache_miss_tokens": cache.get("cache_miss_tokens"),
    }


def run_probe(fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    started = time.monotonic()
    try:
        result = fn()
        result["latency_seconds"] = round(time.monotonic() - started, 3)
        result.setdefault("status", "ok" if result.get("ok") else "limited")
        return result
    except Exception as exc:
        error_text, http_status = _parameter_error_summary(safe_text(exc))
        return {
            "ok": False,
            "status": "error",
            "latency_seconds": round(time.monotonic() - started, 3),
            "error_type": type(exc).__name__,
            "error": error_text,
            "http_status": http_status,
        }


def text_probe(client: DeepSeekClient, model: str) -> dict[str, Any]:
    response = client.chat_completions(
        messages=[
            {"role": "system", "content": "Return only the word pong."},
            {"role": "user", "content": "ping"},
        ],
        tools=None,
        tool_choice="none",
        model=model,
        temperature=0.0,
        thinking_enabled=False,
        max_tokens=probe_token_budget(model, "text"),
    )
    message = response_message(response)
    content = safe_text(message.get("content"))
    reasoning = safe_text(message.get("reasoning_content"))
    ok = is_exact_pong(content) or (not content and is_exact_pong(reasoning))
    return {
        "ok": ok,
        "status": "ok" if ok else "limited",
        "content_preview": content[:120],
        "image_output_present": has_image_output(content),
        "reasoning_present": bool(reasoning),
        "finish_reason": safe_text((response.get("choices") or [{}])[0].get("finish_reason") if response.get("choices") else ""),
        "usage": usage_summary(response),
    }


def stream_probe(client: DeepSeekClient, model: str) -> dict[str, Any]:
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    usage: dict[str, Any] = {}
    chunks = 0
    for chunk in client.chat_completions_stream(
        messages=[
            {"role": "system", "content": "Return only the word pong."},
            {"role": "user", "content": "ping"},
        ],
        tools=None,
        tool_choice="none",
        model=model,
        temperature=0.0,
        thinking_enabled=False,
        max_tokens=probe_token_budget(model, "stream"),
    ):
        chunks += 1
        if isinstance(chunk.get("usage"), dict):
            usage = chunk["usage"]
        choices = chunk.get("choices") if isinstance(chunk.get("choices"), list) else []
        choice = choices[0] if choices and isinstance(choices[0], dict) else {}
        delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
        if delta.get("content"):
            content_parts.append(str(delta.get("content")))
        if delta.get("reasoning_content"):
            reasoning_parts.append(str(delta.get("reasoning_content")))
    content = safe_text("".join(content_parts))
    reasoning = safe_text("".join(reasoning_parts))
    ok = is_exact_pong(content) or (not content and is_exact_pong(reasoning))
    response = {"usage": usage}
    return {
        "ok": chunks > 0 and ok,
        "status": "ok" if chunks > 0 and ok else "limited",
        "chunks": chunks,
        "content_preview": content[:120],
        "image_output_present": has_image_output(content),
        "reasoning_present": bool(reasoning),
        "usage": usage_summary(response),
    }


def tool_probe(client: DeepSeekClient, model: str) -> dict[str, Any]:
    tools = [
        {
            "type": "function",
            "function": {
                "name": "matrix_ping",
                "description": "Return a ping value to the caller.",
                "parameters": {
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
            },
        }
    ]
    response = client.chat_completions(
        messages=[
            {"role": "system", "content": "Use the provided function when the user requests it."},
            {"role": "user", "content": "Call matrix_ping with value pong. Do not answer directly."},
        ],
        tools=tools,
        tool_choice="auto",
        model=model,
        temperature=0.0,
        thinking_enabled=False,
        max_tokens=probe_token_budget(model, "tool"),
    )
    message = response_message(response)
    tool_calls = message.get("tool_calls") if isinstance(message.get("tool_calls"), list) else []
    names = [
        str((call.get("function") or {}).get("name") or "")
        for call in tool_calls
        if isinstance(call, dict)
    ]
    arguments = [
        safe_text((call.get("function") or {}).get("arguments"), limit=200)
        for call in tool_calls
        if isinstance(call, dict)
    ]
    ok = "matrix_ping" in names
    return {
        "ok": ok,
        "status": "ok" if ok else "limited",
        "tool_call_count": len(tool_calls),
        "tool_names": names,
        "arguments": arguments,
        "content_preview": safe_text(message.get("content"), limit=120),
        "usage": usage_summary(response),
    }


def thinking_probe(client: DeepSeekClient, model: str) -> dict[str, Any]:
    response = client.chat_completions(
        messages=[
            {"role": "system", "content": "Think briefly, then return only pong in final content."},
            {"role": "user", "content": "ping"},
        ],
        tools=None,
        tool_choice="none",
        model=model,
        temperature=0.0,
        thinking_enabled=True,
        max_tokens=probe_token_budget(model, "thinking"),
    )
    message = response_message(response)
    content = safe_text(message.get("content"))
    reasoning = safe_text(message.get("reasoning_content"))
    ok = is_exact_pong(content) or (not content and is_exact_pong(reasoning))
    return {
        "ok": ok,
        "status": "ok" if ok else "limited",
        "content_preview": content[:120],
        "reasoning_present": bool(reasoning),
        "reasoning_chars": len(reasoning),
        "usage": usage_summary(response),
    }


def _parameter_error_class(error_text: str) -> str:
    lowered = str(error_text or "").casefold()
    if any(marker in lowered for marker in ("channel_circuit_open", "circuit breaker", "temporarily suspended", "当前无可用token", "http 503")):
        return "model_unavailable"
    if any(marker in lowered for marker in ("http 400", "http 422", "invalid parameter", "unsupported parameter", "unknown field")):
        return "rejected"
    if any(marker in lowered for marker in ("http 404", "model not found", "no available channel")):
        return "model_unavailable"
    return "request_error"


def _parameter_error_summary(error_text: str) -> tuple[str, int | None]:
    text = str(error_text or "")
    lowered = text.casefold()
    match = re.search(r"\bHTTP\s+(\d{3})\b", text, re.IGNORECASE)
    http_status = int(match.group(1)) if match else None
    if "timed out" in lowered:
        return "network_timeout", http_status
    if "channel_circuit_open" in lowered or "circuit breaker" in lowered:
        return "channel_circuit_open", http_status
    if "当前无可用token" in lowered or "no available token" in lowered:
        return "no_available_token", http_status
    if "thinking is not enabled for this model" in lowered:
        return "thinking_mode_conflict", http_status
    if "do request failed" in lowered:
        return "upstream_request_failed", http_status
    if "no available channel" in lowered or "model not found" in lowered:
        return "model_or_channel_unavailable", http_status
    if http_status is not None:
        return "upstream_http_error", http_status
    return "request_error", None


def _parameter_probe_config(base_config: Any, name: str) -> tuple[Any, dict[str, Any]]:
    updates: dict[str, Any] = {
        "api_provider": "gemini",
        "api_key": base_config.gemini_api_key,
        "base_url": base_config.gemini_base_url,
        "temperature": 0.0,
        "gemini_top_p": None,
        "gemini_top_k": None,
        "gemini_response_mime_type": "",
        "gemini_extra_body_json": "",
    }
    request_kwargs: dict[str, Any] = {"temperature": 0.0, "max_tokens": 24}
    if name == "temperature":
        updates["temperature"] = 0.37
        request_kwargs["temperature"] = 0.37
    elif name == "top_p":
        updates["gemini_top_p"] = 0.73
    elif name == "top_k":
        updates["gemini_top_k"] = 17
    elif name == "max_tokens":
        request_kwargs["max_tokens"] = 8
    elif name == "json_output":
        updates["gemini_response_mime_type"] = "application/json"
    return replace(base_config, **updates), request_kwargs


def _parameter_payload_value(payload: dict[str, Any], name: str) -> Any:
    if name in {"temperature", "top_p", "max_tokens", "reasoning_effort", "verbosity"}:
        return payload.get(name)
    extra_body = payload.get("extra_body") if isinstance(payload.get("extra_body"), dict) else {}
    google = extra_body.get("google") if isinstance(extra_body.get("google"), dict) else {}
    generation = google.get("generation_config") if isinstance(google.get("generation_config"), dict) else {}
    if name == "top_k":
        return generation.get("topK")
    if name == "json_output":
        return generation.get("responseMimeType")
    return None


def parameter_probe(base_config: Any, model: str, name: str) -> dict[str, Any]:
    config, request_kwargs = _parameter_probe_config(base_config, name)
    client = DeepSeekClient(config)
    json_mode = name == "json_output"
    messages = [
        {
            "role": "system",
            "content": "Return only a JSON object with ok=true." if json_mode else "Return only the word pong.",
        },
        {"role": "user", "content": "ping"},
    ]
    thinking_enabled = "image" in model.casefold()
    payload = client._build_payload(
        messages=messages,
        tools=None,
        tool_choice="none",
        model=model,
        thinking_enabled=thinking_enabled,
        stream=False,
        **request_kwargs,
    )
    sent_value = _parameter_payload_value(payload, name)
    expected_value = {
        "temperature": 0.37,
        "top_p": 0.73,
        "top_k": 17,
        "max_tokens": 8,
        "json_output": "application/json",
    }[name]
    local_sent = sent_value == expected_value
    if not local_sent:
        return {
            "ok": False,
            "classification": "local_not_sent",
            "local_sent": False,
            "sent_value": sent_value,
            "thinking_enabled": thinking_enabled,
        }
    response = client.chat_completions(
        messages=messages,
        tools=None,
        tool_choice="none",
        model=model,
        thinking_enabled=thinking_enabled,
        **request_kwargs,
    )
    message = response_message(response)
    content = safe_text(message.get("content"), limit=160)
    response_model = safe_text(response.get("model"), limit=160)
    content_ok = False
    if json_mode:
        try:
            parsed = json.loads(content)
            content_ok = isinstance(parsed, dict) and parsed.get("ok") is True
        except Exception:
            content_ok = False
    else:
        content_ok = is_exact_pong(content)
    model_match = not response_model or response_model == model
    classification = "accepted_unverified" if model_match else "accepted_model_mismatch"
    return {
        "ok": True,
        "classification": classification,
        "local_sent": True,
        "sent_value": sent_value,
        "http_status": 200,
        "request_model": model,
        "thinking_enabled": thinking_enabled,
        "response_model": response_model,
        "model_match": model_match,
        "content_ok": content_ok,
        "content_preview": content,
        "usage": usage_summary(response),
    }


def run_parameter_probe(base_config: Any, model: str, name: str) -> dict[str, Any]:
    started = time.monotonic()
    try:
        result = parameter_probe(base_config, model, name)
    except Exception as exc:
        raw_error = safe_text(exc)
        error_text, http_status = _parameter_error_summary(raw_error)
        result = {
            "ok": False,
            "classification": _parameter_error_class(raw_error),
            "local_sent": True,
            "request_model": model,
            "thinking_enabled": "image" in model.casefold(),
            "error_type": type(exc).__name__,
            "error": error_text,
            "http_status": http_status,
        }
    result["latency_seconds"] = round(time.monotonic() - started, 3)
    return result


def provider_parameter_probe(base_config: Any, star: StarAPIConfig, model: str, name: str) -> dict[str, Any]:
    model_star = replace(star, model=model)
    probe_star, request_kwargs, expected, thinking_enabled = _provider_parameter_setup(model_star, name)
    json_mode = name == "json_output"
    messages = [
        {
            "role": "system",
            "content": "Return only a JSON object with ok=true." if json_mode else "Return only the word pong.",
        },
        {"role": "user", "content": "ping"},
    ]
    client = DeepSeekClient(base_config, probe_star)
    payload = client._build_payload(
        messages=messages,
        tools=None,
        tool_choice="none",
        model=model,
        thinking_enabled=thinking_enabled,
        stream=False,
        **request_kwargs,
    )
    sent_value = _parameter_payload_value(payload, name)
    local_sent = sent_value == expected
    if not local_sent:
        return {
            "ok": False,
            "classification": "local_not_sent",
            "local_sent": False,
            "sent_value": sent_value,
            "expected_value": expected,
            "request_model": model,
            "provider": probe_star.provider,
            "slot": probe_star.slot,
            "thinking_enabled": thinking_enabled,
        }
    response = client.chat_completions(
        messages=messages,
        tools=None,
        tool_choice="none",
        model=model,
        thinking_enabled=thinking_enabled,
        **request_kwargs,
    )
    message = response_message(response)
    content = safe_text(message.get("content"), limit=160)
    response_model = safe_text(response.get("model"), limit=160)
    if json_mode:
        try:
            parsed = json.loads(content)
            content_ok = isinstance(parsed, dict) and parsed.get("ok") is True
        except Exception:
            content_ok = False
    else:
        content_ok = is_exact_pong(content)
    model_match = not response_model or response_model == model
    return {
        "ok": True,
        "classification": "accepted_unverified" if model_match else "accepted_model_mismatch",
        "local_sent": True,
        "sent_value": sent_value,
        "expected_value": expected,
        "http_status": 200,
        "provider": probe_star.provider,
        "slot": probe_star.slot,
        "request_model": model,
        "thinking_enabled": thinking_enabled,
        "response_model": response_model,
        "model_match": model_match,
        "content_ok": content_ok,
        "content_preview": content,
        "usage": usage_summary(response),
    }


def run_provider_parameter_probe(base_config: Any, star: StarAPIConfig, model: str, name: str) -> dict[str, Any]:
    started = time.monotonic()
    try:
        result = provider_parameter_probe(base_config, star, model, name)
    except Exception as exc:
        raw_error = safe_text(exc)
        error_text, http_status = _parameter_error_summary(raw_error)
        result = {
            "ok": False,
            "classification": _parameter_error_class(raw_error),
            "local_sent": True,
            "provider": star.provider,
            "slot": star.slot,
            "request_model": model,
            "error_type": type(exc).__name__,
            "error": error_text,
            "http_status": http_status,
        }
    result["latency_seconds"] = round(time.monotonic() - started, 3)
    return result


def build_provider_parameter_matrix(base_config: Any, star: StarAPIConfig, models: list[str]) -> dict[str, Any]:
    parameter_names = PROVIDER_PARAMETER_NAMES[star.provider]
    entries: list[dict[str, Any]] = []
    for index, model in enumerate(models, start=1):
        print(f"[parameters {star.provider} {index}/{len(models)}] {model}", flush=True)
        probes = {
            name: run_provider_parameter_probe(base_config, star, model, name)
            for name in parameter_names
        }
        entries.append(
            {
                "model": model,
                "provider": star.provider,
                "category": model_category(model, provider_hint=star.provider),
                "probes": probes,
            }
        )
    classifications: dict[str, int] = {}
    for entry in entries:
        for probe in entry["probes"].values():
            label = str(probe.get("classification") or "unknown")
            classifications[label] = classifications.get(label, 0) + 1
    return {
        "schema_version": 2,
        "generated_at": timestamp(),
        "endpoint": star.base_url,
        "provider": star.provider,
        "slot": star.slot,
        "parameters": parameter_names,
        "summary": {"probe_count": len(entries) * len(parameter_names), "classifications": classifications},
        "local_contracts": build_local_provider_contracts(base_config),
        "models": entries,
    }


def write_parameter_markdown(payload: dict[str, Any], path: Path) -> None:
    parameter_names = payload.get("parameters") or []
    provider = str(payload.get("provider") or "gemini")
    provider_label = {"gpt": "GPT / Codex", "gemini": "Gemini", "grok": "Grok", "deepseek": "DeepSeek"}.get(provider, provider)
    lines = [
        f"# {provider_label} 模型参数真实性矩阵",
        "",
        f"- generated_at: {payload.get('generated_at')}",
        f"- endpoint: {payload.get('endpoint')}",
        f"- model_count: {len(payload.get('models') or [])}",
        "- `accepted_unverified` 表示上游接受请求且本地确认参数已发送，不代表已证明采样效果。",
        "- `model_unavailable` 表示模型或渠道当前不可用，不能据此判断参数支持。",
        "",
        "| 模型 | " + " | ".join(parameter_names) + " |",
        "|---|" + "---|" * len(parameter_names),
    ]
    for entry in payload.get("models") or []:
        probes = entry.get("probes") if isinstance(entry.get("probes"), dict) else {}
        labels = [str((probes.get(name) or {}).get("classification") or "-") for name in parameter_names]
        lines.append("| " + " | ".join([str(entry.get("model") or ""), *labels]) + " |")
    lines.extend(
        [
            "",
            "## 判定规则",
            "",
            "- `accepted_unverified`: 参数字段已进入真实请求，上游返回成功，但无法仅凭单次响应证明效果。",
            "- `accepted_model_mismatch`: 请求成功，但响应模型与请求模型不一致。",
            "- `rejected`: 上游明确拒绝参数或请求体。",
            "- `model_unavailable`: 503、circuit breaker、无可用 token、404 或无渠道。",
            "- `local_not_sent`: ProjectLing 本地没有把设置写入请求体，属于本地缺陷。",
            "- 详细 request/response model、耗时、usage 与脱敏错误见 JSON。",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def build_parameter_matrix(base_config: Any, models: list[str]) -> dict[str, Any]:
    return build_provider_parameter_matrix(
        base_config,
        star_for_provider(base_config, "gemini", slot="main"),
        models,
    )


def build_model_listing(
    available: list[str],
    selected: list[str],
    requested: list[str],
    *,
    scope: str,
) -> dict[str, Any]:
    available_gemini = [model for model in available if model.lower().startswith("gemini-")]
    available_provider_counts = {
        provider: sum(1 for model in available if model_provider(model) == provider)
        for provider in PROVIDER_ORDER
    }
    selected_provider_counts = {
        provider: sum(1 for model in selected if model_provider(model) == provider)
        for provider in PROVIDER_ORDER
    }
    return {
        "scope": scope,
        "available_count": len(available),
        "available_gemini_count": len(available_gemini),
        "available_provider_counts": available_provider_counts,
        "selected_provider_counts": selected_provider_counts,
        "selected_count": len(selected),
        "requested_count": len(requested),
        "complete_snapshot": not requested,
        "available_models_sha256": hashlib.sha256(
            json.dumps(available, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
    }


def merge_parameter_matrix(base_payload: dict[str, Any], retry_payload: dict[str, Any]) -> dict[str, Any]:
    base_entries = base_payload.get("models") if isinstance(base_payload.get("models"), list) else []
    retry_entries = retry_payload.get("models") if isinstance(retry_payload.get("models"), list) else []
    merged = {
        str(entry.get("model") or ""): entry
        for entry in base_entries
        if isinstance(entry, dict) and entry.get("model")
    }
    for entry in retry_entries:
        if isinstance(entry, dict) and entry.get("model"):
            merged[str(entry["model"])] = entry
    ordered_names = [
        str(entry.get("model") or "")
        for entry in base_entries
        if isinstance(entry, dict) and entry.get("model")
    ]
    ordered_names.extend(name for name in merged if name not in ordered_names)
    entries = [merged[name] for name in ordered_names]
    classifications: dict[str, int] = {}
    for entry in entries:
        probes = entry.get("probes") if isinstance(entry.get("probes"), dict) else {}
        for probe in probes.values():
            label = str(probe.get("classification") or "unknown")
            classifications[label] = classifications.get(label, 0) + 1
    payload = dict(base_payload)
    payload["generated_at"] = timestamp()
    payload["reconciled_at"] = timestamp()
    payload["models"] = entries
    if isinstance(retry_payload.get("model_listing"), dict):
        payload["model_listing"] = dict(retry_payload["model_listing"])
    payload["summary"] = {
        "probe_count": sum(len(entry.get("probes") or {}) for entry in entries),
        "classifications": classifications,
    }
    return payload


def classify(
    model: str,
    probes: dict[str, dict[str, Any]],
    *,
    configured_planner: str,
    configured_executor: str,
) -> tuple[str, str, list[str]]:
    category = model_category(model)
    text_ok = probes.get("text", {}).get("ok") is True
    stream_ok = probes.get("stream", {}).get("ok") is True
    tool_ok = probes.get("tool", {}).get("ok") is True
    reasons: list[str] = []
    if text_ok:
        reasons.append("text")
    if stream_ok:
        reasons.append("sse")
    if tool_ok:
        reasons.append("tool")

    specialized_output = any(
        bool(probe.get("image_output_present")) or bool(probe.get("content_preview")) or int(probe.get("tool_call_count") or 0) > 0
        for probe in probes.values()
        if isinstance(probe, dict) and probe.get("status") != "error"
    )
    if category in {"image", "agent"}:
        support = "unsupported"
        verdict = "diagnostic_only" if text_ok or stream_ok or tool_ok or specialized_output else "incompatible"
        reasons.append(f"specialized_{category}")
    elif model == configured_planner and text_ok and stream_ok:
        support = "planner_only" if not tool_ok else "stable"
        verdict = "recommended"
        reasons.append("configured_planner")
    elif model == configured_executor and text_ok and stream_ok and tool_ok:
        support = "stable"
        verdict = "recommended"
        reasons.append("configured_executor")
    elif text_ok and stream_ok and tool_ok and category in {"flash", "pro", "lite", "codex", "grok", "deepseek", "gemini"}:
        support = "stable"
        verdict = "recommended" if "preview" not in model else "usable_limited"
    elif text_ok and stream_ok and category == "pro":
        support = "planner_only"
        verdict = "usable_limited"
        reasons.append("no_stable_tool_call")
    elif text_ok and (stream_ok or tool_ok):
        support = "limited"
        verdict = "usable_limited"
        reasons.append(f"specialized_{category}" if category in {"thinking", "claude"} else "partial_contract")
    elif text_ok or stream_ok or tool_ok:
        support = "unsupported"
        verdict = "diagnostic_only"
        reasons.append("partial_only")
    else:
        support = "unsupported"
        verdict = "unavailable"
        reasons.append("all_probes_failed")
    return verdict, support, reasons


def normalize_captured_entry(entry: dict[str, Any], *, configured_planner: str, configured_executor: str) -> dict[str, Any]:
    normalized = dict(entry)
    probes = normalized.get("probes") if isinstance(normalized.get("probes"), dict) else {}
    for name in ("text", "stream", "thinking"):
        probe = probes.get(name) if isinstance(probes.get(name), dict) else None
        if probe is None or probe.get("status") == "error":
            continue
        content = str(probe.get("content_preview") or "")
        probe["image_output_present"] = has_image_output(content)
        probe["ok"] = is_exact_pong(content)
        probe["status"] = "ok" if probe["ok"] else "limited"
    verdict, support, reasons = classify(
        str(normalized.get("model") or ""),
        probes,
        configured_planner=configured_planner,
        configured_executor=configured_executor,
    )
    normalized["verdict"] = verdict
    normalized["projectling_support"] = support
    normalized["reasons"] = reasons
    normalized["probes"] = probes
    return normalized


def load_channel_observation(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"source": "missing", "luna_match_count": None, "exact_5_6_compact_exposed": None}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"source": "invalid", "error": safe_text(exc)}
    return payload if isinstance(payload, dict) else {"source": "invalid_type"}


def write_markdown(payload: dict[str, Any], path: Path) -> None:
    summary = payload["summary"]
    observation = payload.get("channel_observation") or {}
    lines = [
        "# ProjectLing Relay Model Compatibility",
        "",
        f"- generated_at: {payload['generated_at']}",
        f"- endpoint: {payload['endpoint']}",
        f"- provider: {payload.get('provider')}",
        f"- slot: {payload.get('slot', 'main')}",
        f"- model_count: {summary['model_count']}",
        f"- recommended: {summary['recommended']}",
        f"- usable_limited: {summary['usable_limited']}",
        f"- diagnostic_only: {summary['diagnostic_only']}",
        f"- incompatible_or_unavailable: {summary['incompatible_or_unavailable']}",
        f"- luna_match_count: {observation.get('luna_match_count')}",
        f"- exact_5_6_compact_exposed: {observation.get('exact_5_6_compact_exposed')}",
        "",
        "## Matrix",
        "",
        "| Model | Provider | Category | Text | SSE | Tool | Thinking | Verdict | ProjectLing |",
        "|---|---|---|---:|---:|---:|---:|---|---|",
    ]
    for entry in payload["models"]:
        probes = entry["probes"]
        thinking = probes.get("thinking") if isinstance(probes.get("thinking"), dict) else None
        if thinking is None:
            thinking_label = "-"
        elif thinking.get("ok") and thinking.get("reasoning_present"):
            thinking_label = "yes"
        elif thinking.get("ok"):
            thinking_label = "accepted/no-trace"
        else:
            thinking_label = "no"
        lines.append(
            "| "
            + " | ".join(
                [
                    entry["model"],
                    str(entry.get("provider") or model_provider(str(entry.get("model") or ""), fallback=str(payload.get("provider") or ""))),
                    entry["category"],
                    "yes" if probes.get("text", {}).get("ok") else "no",
                    "yes" if probes.get("stream", {}).get("ok") else "no",
                    "yes" if probes.get("tool", {}).get("ok") else "no",
                    thinking_label,
                    entry["verdict"],
                    entry["projectling_support"],
                ]
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## Channel Findings",
            "",
            f"- `luna`: server metadata match count is {observation.get('luna_match_count')}; no exposed model contains that name.",
            f"- exact `5.6 compact`: exposed={observation.get('exact_5_6_compact_exposed')}.",
            "- Related but not exposed to this token: "
            + ", ".join(f"`{model}`" for model in observation.get("related_codex_models_not_exposed_to_current_token", [])),
            "- Image and agent variants are not recommended as ProjectLing main/executor chat models even when a basic compatibility probe returns data.",
            f"- Local multi-Provider payload and dual-star isolation contract: `{bool((payload.get('local_contracts') or {}).get('ok'))}`.",
            "- Detailed latencies, response shapes, usage, and sanitized errors are in the JSON artifact.",
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="relay-model-matrix")
    parser.add_argument("--json-output", type=Path, default=DEFAULT_JSON)
    parser.add_argument("--markdown-output", type=Path, default=DEFAULT_MD)
    parser.add_argument("--channel-observation", type=Path, default=DEFAULT_CHANNEL_OBSERVATION)
    parser.add_argument("--reconcile-base", type=Path)
    parser.add_argument("--reconcile-retry", type=Path, action="append", default=[])
    parser.add_argument("--model", action="append", default=[])
    parser.add_argument("--provider", choices=PROVIDER_ORDER, help="query one Provider; defaults to the selected star Provider")
    parser.add_argument("--slot", choices=("main", "executor"), default="main", help="star credentials/model defaults to use")
    parser.add_argument("--timeout", type=float, default=75.0)
    parser.add_argument("--parameter-matrix", action="store_true")
    parser.add_argument("--local-contracts", action="store_true", help="validate all Provider payloads and dual-star isolation without network calls")
    parser.add_argument("--parameter-reconcile-base", type=Path)
    parser.add_argument("--parameter-json-output", type=Path, default=DEFAULT_PARAMETER_JSON)
    parser.add_argument("--parameter-markdown-output", type=Path, default=DEFAULT_PARAMETER_MD)
    args = parser.parse_args(argv)

    base_config = load_config()
    selected_slot = str(args.slot)
    slot_star = base_config.executor_api if selected_slot == "executor" else base_config.main_api
    selected_provider = str(args.provider or slot_star.provider)
    selected_star = replace(
        star_for_provider(base_config, selected_provider, slot=selected_slot),
        timeout_seconds=max(5.0, float(args.timeout)),
        retry_count=0,
        enable_sse=True,
    )
    configured_main = star_for_provider(base_config, selected_provider, slot="main")
    configured_executor = star_for_provider(base_config, selected_provider, slot="executor")
    config = replace(
        base_config,
        api_provider=selected_provider,
        api_key=selected_star.api_key,
        base_url=selected_star.base_url,
        model=selected_star.model,
        timeout_seconds=max(5.0, float(args.timeout)),
        retry_count=0,
        enable_sse=True,
    )
    if args.local_contracts:
        payload = build_local_provider_contracts(base_config)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if payload.get("ok") else 1
    if args.reconcile_base:
        base_payload = json.loads(args.reconcile_base.read_text(encoding="utf-8"))
        base_entries = base_payload.get("models") if isinstance(base_payload.get("models"), list) else []
        merged = {str(entry.get("model") or ""): entry for entry in base_entries if isinstance(entry, dict)}
        for retry_path in args.reconcile_retry:
            retry_payload = json.loads(retry_path.read_text(encoding="utf-8"))
            for entry in retry_payload.get("models") if isinstance(retry_payload.get("models"), list) else []:
                if isinstance(entry, dict) and entry.get("model"):
                    merged[str(entry["model"])] = entry
        entries = [
            normalize_captured_entry(
                merged[model],
                configured_planner=str(configured_main.model),
                configured_executor=str(configured_executor.model),
            )
            for model in sorted(merged)
        ]
        verdict_counts = {
            name: sum(1 for entry in entries if entry["verdict"] == name)
            for name in ("recommended", "usable_limited", "diagnostic_only", "incompatible", "unavailable")
        }
        payload = dict(base_payload)
        payload["generated_at"] = timestamp()
        payload["reconciled_from"] = {
            "base": str(args.reconcile_base),
            "retry": [str(path) for path in args.reconcile_retry],
        }
        payload["channel_observation"] = load_channel_observation(args.channel_observation)
        payload["local_contracts"] = build_local_provider_contracts(base_config)
        payload["summary"] = {
            "model_count": len(entries),
            **verdict_counts,
            "incompatible_or_unavailable": verdict_counts["incompatible"] + verdict_counts["unavailable"],
        }
        payload["models"] = entries
        args.json_output.parent.mkdir(parents=True, exist_ok=True)
        args.json_output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        write_markdown(payload, args.markdown_output)
        print(json.dumps({"status": "ok", "mode": "reconcile", "models": len(entries), "summary": payload["summary"]}, ensure_ascii=False))
        return 0
    client = DeepSeekClient(config, selected_star)
    listed = client.list_models()
    data = listed.get("data") if isinstance(listed.get("data"), list) else []
    available = sorted(str(item.get("id") or "") for item in data if isinstance(item, dict) and item.get("id"))
    requested = [str(model).strip() for model in args.model if str(model).strip()]
    provider_filtered = [model for model in available if model_provider(model) == selected_provider]
    candidate_models = provider_filtered if args.provider else available
    models = [model for model in candidate_models if not requested or model in requested]
    if args.parameter_matrix:
        parameter_models = [model for model in available if model_provider(model) == selected_provider]
        if requested:
            parameter_models = [model for model in parameter_models if model in requested]
        payload = build_provider_parameter_matrix(config, selected_star, parameter_models)
        payload["model_listing"] = build_model_listing(
            available,
            parameter_models,
            requested,
            scope=selected_provider,
        )
        if args.parameter_reconcile_base:
            base_parameter_payload = json.loads(args.parameter_reconcile_base.read_text(encoding="utf-8"))
            payload = merge_parameter_matrix(base_parameter_payload, payload)
        parameter_json_output = args.parameter_json_output
        parameter_markdown_output = args.parameter_markdown_output
        if selected_provider != "gemini" and parameter_json_output == DEFAULT_PARAMETER_JSON:
            parameter_json_output = NOTE_DIR / f"projectling-{selected_provider}-parameter-support.json"
        if selected_provider != "gemini" and parameter_markdown_output == DEFAULT_PARAMETER_MD:
            parameter_markdown_output = NOTE_DIR / f"projectling-{selected_provider}-parameter-support.md"
        parameter_json_output.parent.mkdir(parents=True, exist_ok=True)
        parameter_json_output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        write_parameter_markdown(payload, parameter_markdown_output)
        print(
            json.dumps(
                {
                    "status": "ok",
                    "mode": "parameter_reconcile" if args.parameter_reconcile_base else "parameter_matrix",
                    "models": len(payload.get("models") or []),
                    "summary": payload["summary"],
                    "provider": selected_provider,
                    "slot": selected_slot,
                    "json": str(parameter_json_output),
                    "markdown": str(parameter_markdown_output),
                },
                ensure_ascii=False,
            )
        )
        return 0

    entries: list[dict[str, Any]] = []
    for index, model in enumerate(models, start=1):
        print(f"[{index}/{len(models)}] {model}", flush=True)
        probes = {
            "text": run_probe(lambda model=model: text_probe(client, model)),
            "stream": run_probe(lambda model=model: stream_probe(client, model)),
            "tool": run_probe(lambda model=model: tool_probe(client, model)),
        }
        if supports_reasoning_probe(model, provider_hint=selected_provider):
            probes["thinking"] = run_probe(lambda model=model: thinking_probe(client, model))
        verdict, support, reasons = classify(
            model,
            probes,
            configured_planner=str(configured_main.model),
            configured_executor=str(configured_executor.model),
        )
        entries.append(
            {
                "model": model,
                "provider": model_provider(model, fallback=selected_provider),
                "category": model_category(model, provider_hint=selected_provider),
                "configured_role": "planner" if model == configured_main.model else "executor" if model == configured_executor.model else "",
                "verdict": verdict,
                "projectling_support": support,
                "reasons": reasons,
                "probes": probes,
            }
        )

    verdict_counts = {name: sum(1 for entry in entries if entry["verdict"] == name) for name in ("recommended", "usable_limited", "diagnostic_only", "incompatible", "unavailable")}
    payload = {
        "schema_version": 1,
        "generated_at": timestamp(),
        "endpoint": selected_star.base_url,
        "provider": selected_provider,
        "slot": selected_slot,
        "configured_models": {"planner": configured_main.model, "executor": configured_executor.model},
        "model_listing": build_model_listing(available, models, requested, scope=selected_provider if args.provider else "all"),
        "channel_observation": load_channel_observation(args.channel_observation),
        "local_contracts": build_local_provider_contracts(base_config),
        "summary": {
            "model_count": len(entries),
            **verdict_counts,
            "incompatible_or_unavailable": verdict_counts["incompatible"] + verdict_counts["unavailable"],
        },
        "models": entries,
    }
    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    write_markdown(payload, args.markdown_output)
    print(json.dumps({"status": "ok", "models": len(entries), "summary": payload["summary"], "json": str(args.json_output), "markdown": str(args.markdown_output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
