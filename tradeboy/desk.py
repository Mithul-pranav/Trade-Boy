"""The desk's loop: scan the pairs, debate what the scanner flags, and hand winning trades to the executor."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from . import agents
from .config import Settings
from .debate import Debate, DebateResult, gather_evidence
from .executor import Executor
from .journal import Journal
from .alerts import Alerts
from .llm import AgentError, CreditsExhausted, SpendCapReached
from .market import find_setups, market_open, summarize
from .models import pip_size

log = logging.getLogger("tradeboy")

# If nearly every debate starts with all analysts on the same side, they're probably echoing each other.
AGREEMENT_ALARM_RATE = 0.85
AGREEMENT_ALARM_MIN_DEBATES = 10
# After the Claude API says the credits are gone, try again this often (a refused call costs nothing).
CREDITS_RETRY = timedelta(hours=6)


class Desk:
    def __init__(
        self, settings: Settings, broker, journal: Journal, llm, calendar=None, dry_run: bool = True,
        alerts: Alerts | None = None, news=None, macro=None, quant=None, rates=None,
    ):
        self.settings = settings
        self.alerts = alerts or Alerts(journal)
        self.broker = broker
        self.journal = journal
        self.llm = llm
        self.calendar = calendar
        self.news = news
        self.macro = macro
        self.quant = quant
        self.rates = rates
        self.dry_run = dry_run
        self.executor = Executor(settings, broker, journal, calendar, dry_run=dry_run)

    # -- the cycle -----------------------------------------------------------------------------

    def cycle(self, now: datetime) -> None:
        """One pass, run every scan interval."""
        if not market_open(now):
            log.info("Market closed.")
            return
        if self.executor.check_kill_switch(now):
            self.alerts.send(
                "halted",
                "HALTED. The original 100 USD trading budget is gone, so every trade is closed and nothing "
                "new will open. Trading resumes only with your approval (tradeboy resume --new-budget).",
                now,
            )
            return
        self.score_calls(now)
        if not self._budget_ok(now):
            return
        left_today = self.settings.claude.max_debates_per_day - self.journal.debates_today(now)
        if left_today <= 0:
            log.info("Daily debate limit reached.")
            return
        try:
            flagged = self.scan(now)
        except CreditsExhausted as exc:
            self._credits_out(exc, now)
            flagged = []
        except (AgentError, SpendCapReached) as exc:
            log.error("Scan failed: %s", exc)
            flagged = []
        finally:
            self._journal_usage(now, None)
        for instrument, reason in flagged[:left_today]:
            self.debate(instrument, now, reason)

    def _budget_ok(self, now: datetime) -> bool:
        paused = self.journal.get_state("credits_paused_until")
        if paused and now < datetime.fromisoformat(paused):
            log.info("Claude API credits ran out; debates paused until %s.", paused)
            return False
        claude = self.settings.claude
        left = min(
            claude.monthly_budget_usd - self.journal.month_spend(now),
            claude.daily_budget_usd - self.journal.day_spend(now),
        )
        if left <= 0:
            monthly = self.journal.month_spend(now) >= claude.monthly_budget_usd
            self.alerts.send(
                "spend_cap",
                f"The {'monthly' if monthly else 'daily'} Claude API spend cap "
                f"({claude.monthly_budget_usd if monthly else claude.daily_budget_usd} USD) is reached. Debates "
                f"pause until it resets; open trades keep their stop losses at OANDA.",
                now,
            )
            return False
        self.llm.budget_left_usd = left
        return True

    def scan(self, now: datetime) -> list[tuple[str, str]]:
        """A free code screen finds pairs with a setup; Claude is asked only to choose between several."""
        quotes = self.broker.quotes(self.settings.pairs)
        open_pairs = {t.instrument for t in self.broker.account().open_trades}
        cooldown = timedelta(hours=self.settings.claude.debate_cooldown_hours)
        candidates = {}
        for pair in self.settings.pairs:
            quote = quotes[pair]
            if pair in open_pairs or quote.spread_pips > self.settings.risk.max_spread_pips:
                continue
            last = self.journal.last_debate(pair)
            if last and now - last < cooldown:
                continue
            h1 = summarize(pair, self.broker.candles(pair, "H1", 100))
            h4 = summarize(pair, self.broker.candles(pair, "H4", 100))
            setups = find_setups(h1, h4)
            if setups:
                candidates[pair] = {"setups": setups, "spread_pips": round(quote.spread_pips, 1), "H1": h1, "H4": h4}
        limit = self.settings.claude.max_pairs_per_scan
        if len(candidates) <= limit:
            flagged = [(p, "; ".join(c["setups"])) for p, c in candidates.items()]
        else:
            answer = self.llm.ask(
                agent="scanner", model=self.settings.claude.scanner_model, system=agents.SCANNER,
                prompt=(
                    f"Time (UTC): {now.isoformat(timespec='minutes')}\nThese pairs passed the code screen. "
                    f"Pick at most {limit} worth a full debate, or none.\n{json.dumps(candidates, indent=1)}"
                ),
                schema=agents.SCAN_SCHEMA, effort="low",
            )
            flagged = [(f["instrument"], f["reason"]) for f in answer["flagged"] if f["instrument"] in candidates]
        log.info("Scanner flagged: %s", ", ".join(p for p, _ in flagged) or "nothing")
        return flagged[:limit]

    # -- one debate ------------------------------------------------------------------------------

    def debate(self, instrument: str, now: datetime, reason: str = "") -> DebateResult | None:
        if self.journal.is_halted():
            log.warning("Desk is halted; no debate.")
            return None
        if not self._budget_ok(now):
            return None
        log.info("Debate on %s starting.", instrument)
        try:
            evidence = gather_evidence(self.broker, self.calendar, instrument, now, reason, news=self.news,
                                       macro=self.macro, rates=self.rates)
            result = Debate(self.settings, self.llm, self.journal.track_records(), quant=self.quant).run(
                evidence, self.executor.budget_account(), now
            )
        except CreditsExhausted as exc:
            self._credits_out(exc, now)
            self._journal_usage(now, None)
            return None
        except (AgentError, SpendCapReached) as exc:
            log.error("Debate on %s failed: %s", instrument, exc)
            self._journal_usage(now, None)
            return None

        if result.proposal is not None:
            execution = self.executor.execute(result.proposal, now)
            decision = execution.decision
            if not decision.approved:
                result.outcome = f"{result.outcome}; no order: hard limits rejected it: " + "; ".join(decision.reasons)
            elif execution.trade_id:
                result.outcome = f"{result.outcome}; opened {decision.units} units, trade {execution.trade_id}"
            else:
                result.outcome = f"{result.outcome}; dry run, would open {decision.units} units"
            result.transcript["execution"] = {
                "approved": decision.approved, "units": decision.units, "risk_amount": decision.risk_amount,
                "reasons": decision.reasons, "trade_id": execution.trade_id, "dry_run": self.dry_run,
            }
        result.transcript["outcome"] = result.outcome

        self.journal.record_debate(
            result.debate_id, now, instrument, result.outcome,
            agreed_first=len(set(result.initial_stances.values())) == 1,
            agreed_last=len(set(result.final_stances.values())) == 1,
            stance_changes=result.stance_changes, transcript=result.transcript,
        )
        self._record_calls(result, evidence.quote, now)
        self._journal_usage(now, result.debate_id)
        self._agreement_alarm(now)
        log.info("Debate %s: %s", result.debate_id, result.outcome)
        return result

    def _record_calls(self, result: DebateResult, quote, now: datetime) -> None:
        mid = (quote.bid + quote.ask) / 2
        views = result.transcript["independent_views"]
        for agent, stance in result.final_stances.items():
            hours = min(max(int(views[agent]["hold_hours"]), 1), 168)
            confidence = result.transcript["rebuttals"][agent]["final_confidence"]
            self.journal.record_call(
                result.debate_id, agent, result.instrument, stance, confidence, mid, now, now + timedelta(hours=hours)
            )
        quant = result.transcript.get("quant")
        if quant:
            hours = min(max(int(quant["hold_hours"]), 1), 168)
            self.journal.record_call(
                result.debate_id, "quant", result.instrument, quant["stance"], quant["confidence"], mid, now,
                now + timedelta(hours=hours),
            )
        head = result.transcript["head_of_desk"]
        if head["side"] in ("long", "short"):
            hours = min(max(int(head["hold_hours"]), 1), 168)
            self.journal.record_call(
                result.debate_id, "head_of_desk", result.instrument, head["side"], 100, mid, now,
                now + timedelta(hours=hours),
            )

    def score_calls(self, now: datetime) -> None:
        """Score each analyst's call once its own time horizon has passed: pips in its favour, or against."""
        for debate_id, agent, instrument, stance, price, due in self.journal.calls_due(now):
            later = self.broker.price_at(instrument, due)
            if later is None:
                continue
            pips = (later - price) / pip_size(instrument) * (1 if stance == "long" else -1)
            self.journal.score_call(debate_id, agent, round(pips, 1))

    def _credits_out(self, exc: Exception, now: datetime) -> None:
        self.journal.set_state("credits_paused_until", (now + CREDITS_RETRY).isoformat())
        self.alerts.send(
            "credits",
            "The Claude API credits have run out (or the API key stopped working), so the agents can't "
            "debate. No new trades will open; open trades keep their stop losses at OANDA. Add credits at "
            f"console.anthropic.com and the desk retries every 6 hours. Details: {str(exc)[:200]}",
            now,
        )

    def _journal_usage(self, now: datetime, debate_id: str | None) -> None:
        usage = self.llm.drain_usage()
        if usage:
            self.journal.record_usage(now, debate_id, usage)

    def _agreement_alarm(self, now: datetime) -> None:
        stats = self.journal.agreement_rate()
        if stats and stats[1] >= AGREEMENT_ALARM_MIN_DEBATES and stats[0] >= AGREEMENT_ALARM_RATE:
            self.alerts.send(
                "agreement",
                f"Agreement alarm: the analysts started on the same side in {stats[0]:.0%} of the last "
                f"{stats[1]} debates. They may be echoing each other; the prompts need a review.",
                now,
            )
            self.journal.set_state("agreement_alarm", f"{stats[0]:.2f} over {stats[1]} debates")
