"""Zużycie tokenów agenta (`response.usage`) i szacowany koszt."""
from __future__ import annotations

from dataclasses import astuple, dataclass

# USD za 1 mln tokenów: (wejście, wyjście, odczyt cache, zapis cache 5 min)
PRICES = {"claude-opus-5-5": (4.0, 20.0, 0.20, 5.0)}

LOCAL_PREFIX = "ollama/"  # modele lokalne: bez kosztu API


@dataclass
class TokenUsage:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0

    @classmethod
    def from_response(cls, usage) -> TokenUsage:
        def get(name: str) -> int:
            return getattr(usage, name, None) or 0
        return cls(get("input_tokens"), get("output_tokens"), get("cache_read_input_tokens"),
                   get("cache_creation_input_tokens"))

    def cost_usd(self, model: str) -> float | None:
        """None = model spoza cennika (np. zapasowy) – lepiej brak kosztu niż zły koszt."""
        if model.startswith(LOCAL_PREFIX):
            return 0.0
        price = PRICES.get(model)
        return sum(n * p for n, p in zip(astuple(self), price, strict=True)) / 1e6 if price else None
