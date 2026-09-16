"""Stats shown under a reply, combined from oMLX usage chunks."""

from canopy.server import UsageTotals


def _turn(prompt, cached, completion, prefill_s, gen_s, total_s):
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "prompt_tokens_details": {"cached_tokens": cached},
        "prompt_eval_duration": prefill_s,
        "generation_duration": gen_s,
        "total_time": total_s,
    }


def test_single_reply_counts_only_uncached_tokens_for_prompt_speed():
    u = UsageTotals()
    # The real 13:59 reply: 108,198-token prompt, 88,064 cached, 83s prefill.
    u.add(_turn(108_198, 88_064, 16_127, 83.0, 693.7, 777.5))
    out = u.to_dict()
    assert out["prompt_tokens"] == 108_198
    assert out["prompt_tokens_per_second"] == round(20_134 / 83.0, 1)  # ~242.6, not ~1,303
    assert out["generation_tokens_per_second"] == round(16_127 / 693.7, 1)
    assert out["total_time"] == 777.5


def test_tool_turns_report_latest_context_and_summed_generation():
    u = UsageTotals()
    u.add(_turn(10_000, 0, 200, 20.0, 10.0, 30.0))
    u.add(_turn(30_000, 10_000, 300, 40.0, 15.0, 55.0))
    out = u.to_dict()
    assert out["prompt_tokens"] == 30_000  # context now, not 40,000
    assert out["completion_tokens"] == 500
    assert out["total_tokens"] == 30_500
    assert out["prompt_tokens_per_second"] == round(30_000 / 60.0, 1)
    assert out["generation_tokens_per_second"] == round(500 / 25.0, 1)
    assert out["total_time"] == 85.0


def test_backends_without_timings_only_report_counts():
    u = UsageTotals()
    u.add({"prompt_tokens": 12, "completion_tokens": 34})
    assert u.to_dict() == {"prompt_tokens": 12, "completion_tokens": 34, "total_tokens": 46}


def test_fully_cached_prompt_has_no_prompt_speed():
    u = UsageTotals()
    u.add(_turn(5_000, 5_000, 10, 0.5, 1.0, 1.5))
    assert "prompt_tokens_per_second" not in u.to_dict()
