"""Optional headline tone from FinBERT (ProsusAI/finbert), run locally on the CPU.

FinBERT was trained on company and market news, so its label is the tone of the headline, not a call
on a currency pair: "Dollar surges" reads positive whether you hold USD or EUR/USD. The macro analyst
gets it as a hint next to the headline and still decides what the story means for the pair.

Needs the optional extra (`pip install -e '.[sentiment]'`, which pulls in transformers and torch,
about 2 GB on disk and 1 GB of RAM while loaded). If the model can't be loaded, headlines go out
without a score and the agents are told sentiment is missing.
"""

from __future__ import annotations

MODEL = "ProsusAI/finbert"


class FinBert:
    def __init__(self, model: str = MODEL, pipeline_factory=None):
        self.model = model
        self._factory = pipeline_factory
        self._pipe = None
        self._cache: dict[str, dict] = {}
        self.last_error: str | None = None

    def _load(self):
        if self._pipe is None:
            factory = self._factory
            if factory is None:
                from transformers import pipeline  # imported late: it's heavy and optional

                factory = lambda: pipeline("text-classification", model=self.model, device=-1)
            self._pipe = factory()
        return self._pipe

    def score(self, titles: list[str]) -> list[dict | None]:
        """{"label": positive | neutral | negative, "confidence": 0..1} per title, or None if unscored."""
        todo = [t for t in dict.fromkeys(titles) if t not in self._cache]
        if todo:
            if len(self._cache) > 5000:  # old headlines stop showing up after two days
                self._cache.clear()
            try:
                results = self._load()(todo, truncation=True)
                for title, result in zip(todo, results):
                    self._cache[title] = {"label": result["label"].lower(), "confidence": round(float(result["score"]), 2)}
                self.last_error = None
            except Exception as exc:  # missing package, failed download, bad input: score nothing, say so
                self.last_error = f"Headline sentiment (FinBERT) unavailable: {str(exc)[:150] or type(exc).__name__}"
        return [self._cache.get(t) for t in titles]
