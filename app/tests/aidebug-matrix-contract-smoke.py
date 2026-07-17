#!/usr/bin/env python3
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aidebug.runner import aidebug_health  # noqa: E402
from aidebug.runner import relay_model_matrix  # noqa: E402


HASH = "a" * 64


def listing(*, scope: str, available: int, selected: int, requested: int = 0) -> dict[str, object]:
    return {
        "scope": scope,
        "available_count": available if scope == "all" else 22,
        "available_gemini_count": available,
        "selected_count": selected,
        "requested_count": requested,
        "complete_snapshot": requested == 0,
        "available_models_sha256": HASH,
    }


def main() -> int:
    generated = relay_model_matrix.build_model_listing(
        ["claude-test", "gemini-a", "gemini-b"],
        ["gemini-a", "gemini-b"],
        [],
        scope="gemini",
    )
    assert generated["available_count"] == 3
    assert generated["available_gemini_count"] == 2
    assert generated["available_provider_counts"]["gemini"] == 2
    assert generated["selected_provider_counts"]["gemini"] == 2
    assert generated["selected_count"] == 2
    assert generated["complete_snapshot"] is True
    assert len(str(generated["available_models_sha256"])) == 64
    reconciled = relay_model_matrix.merge_parameter_matrix(
        {"models": [], "summary": {}},
        {"models": [], "summary": {}, "model_listing": generated},
    )
    assert reconciled["model_listing"] == generated
    all_ok, all_mode = aidebug_health._matrix_listing_complete(
        {"model_listing": listing(scope="all", available=22, selected=22)},
        entry_count=22,
        scope="all",
    )
    assert all_ok and all_mode == "snapshot"
    gemini_ok, gemini_mode = aidebug_health._matrix_listing_complete(
        {"model_listing": listing(scope="gemini", available=20, selected=20)},
        entry_count=20,
        scope="gemini",
    )
    assert gemini_ok and gemini_mode == "snapshot"
    partial_ok, partial_mode = aidebug_health._matrix_listing_complete(
        {"model_listing": listing(scope="all", available=22, selected=1, requested=1)},
        entry_count=1,
        scope="all",
    )
    assert not partial_ok and partial_mode == "partial"
    legacy_ok, legacy_mode = aidebug_health._matrix_listing_complete(
        {"summary": {"model_count": 22}},
        entry_count=22,
        scope="all",
    )
    assert legacy_ok and legacy_mode == "legacy"
    config = relay_model_matrix.load_config()
    contracts = relay_model_matrix.build_local_provider_contracts(config)
    assert contracts["ok"] is True
    assert set(contracts["providers"]) == {"gpt", "gemini", "grok", "deepseek"}
    assert all(contract["ok"] is True for contract in contracts["providers"].values())
    assert contracts["dual_star_isolation"]["ok"] is True
    gpt55 = relay_model_matrix.provider_parameter_payload_contract(
        config,
        replace(
            relay_model_matrix.star_for_provider(config, "gpt", slot="main"),
            api_key="fixture-matrix-smoke",
            model="gpt-5.5-codex",
        ),
        "reasoning_effort",
    )
    assert gpt55["local_sent"] is True
    assert gpt55["sent_value"] == "xhigh"
    print("aidebug_matrix_contract_smoke=ok all=22 gemini=20 providers=4 dual=isolated partial=blocked legacy=compatible")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
