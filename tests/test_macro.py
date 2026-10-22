from datetime import datetime, timedelta, timezone

import httpx
import pytest

from fakes import FakeBroker, FakeLLM, agreeing_script

from tradeboy.config import load_settings
from tradeboy.debate import Debate, gather_evidence
from tradeboy.macro import FredData, Series, parse_csv, summarize

NOW = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
DFF = "observation_date,DFF\n2026-09-18,4.33\n2026-09-21,.\n2026-09-22,4.08\n2026-10-20,4.09\n2026-10-21,4.08\n"
DGS2 = "observation_date,DGS2\n2026-10-20,3.50\n2026-10-21,3.48\n"
DGS10 = "DATE,DGS10\n2026-10-20,4.01\n2026-10-21,4.03\n"
# Hand-made monthly index: 300.0 a year before 309.0, so y/y = 3.00%; a year before 309.9 is 301.0, 2.96%.
CPI = ("observation_date,CPIAUCSL\n2025-08-01,300.0\n2025-09-01,301.0\n2026-08-01,309.0\n2026-09-01,309.9\n")
SERIES = [Series("DFF", "Fed funds rate", "USD"), Series("DGS2", "US 2Y", "USD"), Series("DGS10", "US 10Y", "USD"),
          Series("CPIAUCSL", "US CPI", "USD", "yoy"), Series("ECBDFR", "ECB deposit rate", "EUR")]
PAGES = {"DFF": DFF, "DGS2": DGS2, "DGS10": DGS10, "CPIAUCSL": CPI}


def fred(fail=(), log=None):
    def handler(request):
        sid = request.url.params["id"]
        if log is not None:
            log.append(sid)
        if sid in fail or sid not in PAGES:
            return httpx.Response(404, text="Series not found")
        return httpx.Response(200, text=PAGES[sid])
    return FredData(SERIES, http=httpx.Client(transport=httpx.MockTransport(handler)))


def test_parse_skips_missing_values_and_accepts_both_header_styles():
    assert parse_csv(DFF)[1] == (datetime(2026, 9, 22).date(), 4.08)
    assert len(parse_csv(DFF)) == 4 and parse_csv(DGS10)[-1][1] == 4.03
    with pytest.raises(ValueError):
        parse_csv("observation_date,DFF\n2026-10-21,.\n")


def test_level_series_reports_latest_value_date_and_one_month_change():
    row = summarize(SERIES[0], parse_csv(DFF))
    # Latest 4.08 on 10-21; 28 days earlier is 09-23, last reading on or before it is 4.08 (09-22).
    assert row == {"series": "DFF", "name": "Fed funds rate", "currency": "USD", "value": 4.08,
                   "as_of": "2026-10-21", "change_1m": 0.0}


def test_cpi_is_turned_into_a_year_on_year_change():
    row = summarize(SERIES[3], parse_csv(CPI))
    assert row["value"] == 2.96 and row["as_of"] == "2026-09-01" and row["name"] == "US CPI (% y/y)"
    assert row["change_1m"] == -0.04  # 2.96 now vs 3.00 the month before


def test_snapshot_filters_by_currency_adds_the_curve_and_reports_failures():
    data = fred()
    usd = data.snapshot({"USD", "JPY"}, NOW)
    assert [r["series"] for r in usd] == ["DFF", "DGS2", "DGS10", "CPIAUCSL", "DGS10-DGS2"]
    assert usd[-1]["value"] == 0.55
    assert data.gaps({"USD"}) == []
    assert data.snapshot({"EUR"}, NOW) == []  # never made up
    assert data.gaps({"EUR"}) and "ECB deposit rate" in data.gaps({"EUR"})[0] and "404" in data.gaps({"EUR"})[0]


def test_downloads_once_a_day_and_keeps_the_last_good_copy():
    log = []
    data = fred(log=log)
    data.snapshot({"USD"}, NOW)
    data.snapshot({"USD"}, NOW + timedelta(hours=5))
    assert log.count("DFF") == 1
    data.http = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    later = data.snapshot({"USD"}, NOW + timedelta(hours=25))
    assert later[0]["value"] == 4.08 and data.gaps({"USD"})  # stale copy kept, failure still reported


def test_shipped_settings_list_series_for_every_traded_currency():
    settings = load_settings()
    ids = {s["id"] for s in settings.fred_series}
    assert {"DFF", "DGS3MO", "DGS2", "DGS10", "CPIAUCSL", "UNRATE"} <= ids
    covered = {s["currency"] for s in settings.fred_series}
    assert covered == {"USD", "EUR", "GBP", "JPY", "AUD", "CAD", "CHF", "NZD"}


def test_macro_analyst_sees_fred_data():
    evidence = gather_evidence(FakeBroker(), None, "EUR_USD", NOW, macro=fred())
    assert evidence.macro[0]["series"] == "DFF"
    assert any("ECB deposit rate" in g for g in evidence.data_gaps)
    llm = FakeLLM(agreeing_script())
    Debate(load_settings(), llm).run(evidence, FakeBroker().account(), NOW)
    macro = next(c["prompt"] for c in llm.calls if c["agent"] == "macro")
    assert "FRED" in macro and "Fed funds rate" in macro and "ECB deposit rate" in macro
    tech = next(c["prompt"] for c in llm.calls if c["agent"] == "technical")
    assert "FRED" not in tech
