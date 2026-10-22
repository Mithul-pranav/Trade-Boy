from datetime import datetime, timezone

from fakes import FakeBroker, FakeLLM, agreeing_script, head, rebuttal, risk, skeptic, technical, view

from tradeboy.config import load_settings
from tradeboy.debate import Debate, gather_evidence
from tradeboy.models import Side

NOW = datetime(2026, 10, 22, 8, 0, tzinfo=timezone.utc)
SETTINGS = load_settings()


def run(script):
    broker = FakeBroker()
    llm = FakeLLM(script)
    evidence = gather_evidence(broker, None, "EUR_USD", NOW)
    return Debate(SETTINGS, llm).run(evidence, broker.account(), NOW), llm


def split_script(**overrides):
    script = {
        "technical": technical("long", thesis="TECH-SECRET"),
        "macro": view("short", thesis="MACRO-SECRET"),
        "positioning": view("long", thesis="POS-SECRET"),
        "technical_rebuttal": rebuttal("long"),
        "macro_rebuttal": rebuttal("short"),
        "positioning_rebuttal": rebuttal("long"),
        "skeptic": skeptic(("O1", "serious"), ("O2", "minor")),
        "risk_manager": risk(),
        "head_of_desk": head(answers=[("O1", True)]),
    }
    script.update(overrides)
    return script


def test_analysts_form_views_without_seeing_each_other():
    _, llm = run(split_script())
    first = {c["agent"]: c["prompt"] for c in llm.calls if c["agent"] in ("technical", "macro", "positioning")}
    assert "MACRO-SECRET" not in first["technical"] and "POS-SECRET" not in first["technical"]
    assert "TECH-SECRET" not in first["macro"]
    rebut = next(c["prompt"] for c in llm.calls if c["agent"] == "technical_rebuttal")
    assert "MACRO-SECRET" in rebut and "POS-SECRET" in rebut


def test_each_analyst_gets_its_own_evidence():
    _, llm = run(split_script())
    prompts = {c["agent"]: c for c in llm.calls}
    assert "recent_bars" in prompts["technical"]["prompt"] and "retail_long_pct" not in prompts["technical"]["prompt"]
    assert "retail_long_pct" in prompts["positioning"]["prompt"]
    assert prompts["macro"]["web_searches"] > 0 and prompts["technical"]["web_searches"] == 0


def test_stance_change_without_a_named_argument_is_ignored():
    result, _ = run(split_script(macro_rebuttal=rebuttal("long", changed_by="")))
    assert result.final_stances["macro"] == "short"
    assert result.stance_changes == 0
    assert "ignored" in result.transcript["rebuttals"]["macro"]["note"]


def test_stance_change_with_a_named_argument_counts():
    result, _ = run(split_script(macro_rebuttal=rebuttal("long", changed_by="technical: the breakout held")))
    assert result.final_stances["macro"] == "long"
    assert result.stance_changes == 1


def test_rebuttal_round_skipped_when_all_agree_but_skeptic_still_attacks():
    result, llm = run(agreeing_script())
    assert not any(a.endswith("_rebuttal") for a in llm.agents_called())
    assert "skeptic" in llm.agents_called()
    assert result.outcome.startswith("trade: long")


def test_unanswered_serious_objection_halves_the_size():
    result, _ = run(split_script(head_of_desk=head(answers=[("O1", False)])))
    assert result.proposal.risk_fraction == 0.5
    assert "O1" in result.outcome


def test_minor_objections_need_no_answer():
    result, _ = run(split_script())
    assert result.proposal.risk_fraction == 1.0


def test_risk_manager_can_shrink_a_side_but_never_block_it():
    result, _ = run(split_script(risk_manager=risk(long=0.0, short=1.0)))
    assert result.proposal.risk_fraction == 0.25


def test_head_of_desk_chooses_a_share_of_the_allowed_size():
    result, _ = run(split_script(risk_manager=risk(long=0.8), head_of_desk=head(size=0.5, answers=[("O1", True)])))
    assert result.proposal.risk_fraction == 0.4


def test_winning_trade_carries_risk_managers_size():
    result, _ = run(split_script(risk_manager=risk(long=0.5)))
    p = result.proposal
    assert (p.side, p.stop_loss, p.risk_fraction) == (Side.BUY, 1.0950, 0.5)
    assert p.debate_id == result.debate_id


def test_head_of_desk_uses_the_stronger_model():
    _, llm = run(split_script())
    models = {c["agent"]: c["model"] for c in llm.calls}
    assert models["head_of_desk"] == SETTINGS.claude.head_model
    assert models["technical"] == SETTINGS.claude.analyst_model


def test_every_debate_ends_in_a_trade_with_a_stop():
    result, _ = run(split_script(head_of_desk=head(side="short", stop=1.0950, target=1.1100)))
    p = result.proposal
    # A short's stop below the price is unusable: code sets one above it from volatility, and drops the target.
    assert p.side == Side.SELL and p.stop_loss > 1.0950 and p.take_profit is None
    assert "volatility" in result.outcome
