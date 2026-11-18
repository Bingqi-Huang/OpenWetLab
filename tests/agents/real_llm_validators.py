"""Step and parameter validators for novel real-LLM tasks distinct from `sops/` (v0.6 #30b)."""

import json
from pathlib import Path
from typing import Any

from wetlab.lab.protocol import fingerprint

REPO_ROOT = Path(__file__).resolve().parents[2]


def _step_labels(steps: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    for s in steps:
        if "skill" in s:
            out.append(str(s["skill"]))
        elif "wait_until" in s:
            wu = s["wait_until"]
            if wu.get("device") == "pcr" and (wu.get("state") or {}).get("PcrState") == "Idle":
                out.append("wait_pcr_idle")
            else:
                out.append("wait_until")
        elif "wait" in s:
            out.append("wait")
    return out


def _has_subsequence(items: list[str], target: list[str]) -> bool:
    it = iter(items)
    return all(elem in it for elem in target)


def stored_sop_fingerprints() -> set[str]:
    fps: set[str] = set()
    for p in (REPO_ROOT / "sops").glob("*.json"):
        fps.add(fingerprint(json.loads(p.read_text(encoding="utf-8"))))
    return fps


def assert_matches_shake_and_42c_hold(protocol: dict[str, Any]) -> None:
    """Task 1 validator (distinct from all `sops/*.json`):
    shake LW-002 20s @ 500 rpm, then PCR LW-001 42℃ hold 45s (`lid_c=None`, `volume_ul=40`).
    """
    assert fingerprint(protocol) not in stored_sop_fingerprints()
    steps = protocol["steps"]

    shakes = [s["args"] for s in steps if s.get("skill") == "shake"]
    assert shakes == [{"seconds": 20, "rpm": 500}]

    starts = [
        (idx, s["args"])
        for idx, s in enumerate(steps)
        if s.get("skill") == "pcr_start_program"
    ]
    assert len(starts) == 1
    start_idx, args = starts[0]
    assert args["stages"] == [{"temperature_c": 42, "hold_seconds": 45}]
    assert args.get("lid_c") is None
    assert int(args["volume_ul"]) == 40

    tail = _step_labels(steps[start_idx + 1 :])
    assert _has_subsequence(tail, ["wait_pcr_idle", "pcr_confirm_finished", "pcr_open"])


def assert_matches_two_incubations_and_12cycle_pcr(protocol: dict[str, Any]) -> None:
    """Task 2 validator (distinct from all `sops/*.json`):
    65℃ 120s -> open/close -> 85℃ 90s -> open/close -> 12-cycle PCR + 10℃ 180s (`lid_c=105`).
    """
    assert fingerprint(protocol) not in stored_sop_fingerprints()
    steps = protocol["steps"]
    starts = [
        (idx, s["args"])
        for idx, s in enumerate(steps)
        if s.get("skill") == "pcr_start_program"
    ]
    assert len(starts) == 3, f"expected 3 pcr_start_program runs, got {len(starts)}"

    expected_stages = [
        [{"temperature_c": 65, "hold_seconds": 120}],
        [{"temperature_c": 85, "hold_seconds": 90}],
        [
            {"temperature_c": 95, "hold_seconds": 45},
            {
                "repeat": 12,
                "steps": [
                    {"temperature_c": 95, "hold_seconds": 15},
                    {"temperature_c": 58, "hold_seconds": 20},
                    {"temperature_c": 72, "hold_seconds": 30},
                ],
            },
            {"temperature_c": 72, "hold_seconds": 120},
            {"temperature_c": 10, "hold_seconds": 180},
        ],
    ]
    for (_, args), exp_stage in zip(starts, expected_stages, strict=True):
        assert args["stages"] == exp_stage
        assert args.get("lid_c") == 105
        assert int(args["volume_ul"]) == 30

    for k in range(2):
        between = _step_labels(steps[starts[k][0] + 1 : starts[k + 1][0]])
        assert _has_subsequence(
            between,
            ["wait_pcr_idle", "pcr_confirm_finished", "pcr_open", "pcr_close"],
        )

    final_tail = _step_labels(steps[starts[2][0] + 1 :])
    assert _has_subsequence(final_tail, ["wait_pcr_idle", "pcr_confirm_finished", "pcr_open"])
