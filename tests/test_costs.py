from datetime import datetime, timezone

from tradeboy.cli import net_result
from tradeboy.journal import Journal
from tradeboy.llm import Usage

NOW = datetime(2026, 10, 22, 8, 0, tzinfo=timezone.utc)


def test_net_result_subtracts_api_costs(tmp_path):
    journal = Journal(tmp_path / "j.sqlite3")
    journal.record_usage(NOW, "d1", [Usage("head_of_desk", "m", 1, 1, 0, 0.30), Usage("macro", "m", 1, 1, 0, 0.20)])
    assert journal.total_spend() == 0.5
    assert net_result(1.00, journal.total_spend(), 1.30) == {"trading": 1.0, "api_costs": 0.65, "net": 0.35}
