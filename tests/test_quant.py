from datetime import datetime, timezone

from fakes import FakeBroker, FakeLLM, candles, head, rebuttal

from tradeboy.config import load_settings
from tradeboy.debate import Debate, gather_evidence
from tradeboy.quant import SIGNALS, QuantSeat, snapshot
from test_debate import split_script

NOW = datetime(2026, 10, 22, 8, 0, tzinfo=timezone.utc)
SETTINGS = load_settings()
RISING = candles(n=150)  # a steady climb: momentum says long


def validated(signal="momentum_20d", inverted=False):
    return {"passed": True, "validated": [{
        "key": f"{signal}/swing", "signal": signal, "inverted": inverted, "exit": "swing", "label": "Momentum",
        "trades": 400, "win_rate_pct": 58.0, "avg_r": 0.12, "t_stat": 2.6, "stop_atr": 1.0, "max_hours": 120,
    }]}


def test_snapshot_feeds_every_signal():
    snap = snapshot("EUR_USD", RISING, RISING, RISING, carry_pct=-1.5)
    assert snap["d_change_20"] > 0 and snap["carry_pct"] == -1.5
    readings = {s.name: s.rule(snap) for s in SIGNALS}
    assert readings["momentum_20d"] == "long" and readings["carry"] == "short"


def test_without_a_tested_edge_the_seat_says_so():
    view = QuantSeat(None).view("EUR_USD", RISING, RISING, RISING)
    assert view["significant"] is False and view["confidence"] == 50
    assert "NOT a statistical edge" in view["thesis"]


def test_a_validated_signal_gives_a_significant_view_with_its_numbers():
    view = QuantSeat(validated()).view("EUR_USD", RISING, RISING, RISING)
    assert view["stance"] == "long" and view["significant"] and view["confidence"] == 58
    assert "400" in view["thesis"] and view["hold_hours"] == 120
    inverted = QuantSeat(validated(inverted=True)).view("EUR_USD", RISING, RISING, RISING)
    assert inverted["stance"] == "short"


def run(script, quant):
    broker = FakeBroker()
    evidence = gather_evidence(broker, None, "EUR_USD", NOW)
    llm = FakeLLM(script)
    return Debate(SETTINGS, llm, quant=quant).run(evidence, broker.account(), NOW), llm


def all_short_script(**overrides):
    from fakes import risk, skeptic, technical, view

    script = {
        "technical": technical("short", stop=1.1300, target=None), "macro": view("short"),
        "positioning": view("short"), "skeptic": skeptic(("O1", "minor")), "risk_manager": risk(),
        "head_of_desk": head(side="short", stop=1.1300, target=None),
        **{f"{a}_rebuttal": rebuttal("short") for a in ("technical", "macro", "positioning")},
    }
    script.update(overrides)
    return script


def test_going_against_a_significant_quant_signal_halves_the_size():
    result, llm = run(all_short_script(), QuantSeat(validated()))
    assert result.transcript["quant"]["stance"] == "long"
    assert result.proposal.risk_fraction == 0.5 and "quant" in result.outcome
    # The analysts all agreed, but the quant seat didn't, so they must answer it in a rebuttal round.
    rebut = next(c["prompt"] for c in llm.calls if c["agent"] == "technical_rebuttal")
    assert "quant seat" in rebut


def test_a_coin_flip_quant_view_costs_nothing_and_changes_nothing():
    result, llm = run(all_short_script(), QuantSeat(None))
    assert result.proposal.risk_fraction == 1.0
    assert not any(a.endswith("_rebuttal") for a in llm.agents_called())
    assert "quant_seat" in next(c["prompt"] for c in llm.calls if c["agent"] == "head_of_desk")


def test_debates_without_a_quant_seat_are_unchanged():
    result, _ = run(split_script(), None)
    assert "quant" not in result.transcript and result.proposal.risk_fraction == 1.0
