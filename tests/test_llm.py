from types import SimpleNamespace as NS

import pytest

from tradeboy.llm import AgentError, ClaudeLLM, SpendCapReached, cost


def usage(i=1000, o=500, searches=0):
    return NS(input_tokens=i, output_tokens=o, cache_read_input_tokens=0, cache_creation_input_tokens=0,
              server_tool_use=NS(web_search_requests=searches))


def response(text, stop="end_turn", **u):
    return NS(content=[NS(type="server_tool_use"), NS(type="text", text=text)], stop_reason=stop, usage=usage(**u))


class FakeClient:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []
        self.beta = NS(messages=NS(create=self._create))

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        return self.responses.pop(0)


def ask(llm, model="claude-opus-5-5", searches=0):
    return llm.ask(agent="macro", model=model, system="s", prompt="p", schema={"type": "object"},
                   web_searches=searches)


def test_returns_the_json_answer_and_counts_cost():
    llm = ClaudeLLM(FakeClient(response('{"stance": "long"}', searches=2)))
    assert ask(llm, searches=2) == {"stance": "long"}
    [u] = llm.drain_usage()
    assert u.cost_usd == pytest.approx((1000 * 4 + 500 * 20) / 1e6 + 0.02)
    assert u.web_searches == 2


def test_request_shape():
    client = FakeClient(response("{}"))
    ask(ClaudeLLM(client), searches=2)
    req = client.requests[0]
    assert req["output_config"]["format"]["type"] == "json_schema"
    assert req["tools"][0]["type"] == "web_search_20260209" and req["tools"][0]["max_uses"] == 2
    assert req["fallbacks"] == "default"
    assert req["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_no_fallback_param_for_haiku():
    client = FakeClient(response("{}"))
    ask(ClaudeLLM(client), model="claude-haiku-5-5")
    assert "fallbacks" not in client.requests[0]


def test_paused_turn_is_continued():
    client = FakeClient(response("", stop="pause_turn"), response('{"ok": true}'))
    assert ask(ClaudeLLM(client), searches=1) == {"ok": True}
    assert len(client.requests) == 2 and client.requests[1]["messages"][-1]["role"] == "assistant"


def test_spend_cap_stops_calls():
    llm = ClaudeLLM(FakeClient(response("{}")), budget_left_usd=0.015)
    ask(llm)  # costs 0.014
    ask_again = ClaudeLLM(FakeClient(), budget_left_usd=0)
    with pytest.raises(SpendCapReached):
        ask(ask_again)


def test_refusal_and_bad_json_raise_agent_error():
    with pytest.raises(AgentError):
        ask(ClaudeLLM(FakeClient(response("", stop="refusal"))))
    with pytest.raises(AgentError):
        ask(ClaudeLLM(FakeClient(response("not json"))))


def test_cost_table():
    assert cost("claude-haiku-5-5", usage(1_000_000, 0)) == pytest.approx(0.10)


def test_credit_error_becomes_credits_exhausted():
    import anthropic

    class Broke(FakeClient):
        def _create(self, **kwargs):
            err = anthropic.BadRequestError.__new__(anthropic.BadRequestError)
            Exception.__init__(err, "Your credit balance is too low to access the Anthropic API.")
            err.status_code = 400
            raise err

    from tradeboy.llm import CreditsExhausted
    with pytest.raises(CreditsExhausted):
        ask(ClaudeLLM(Broke()))
