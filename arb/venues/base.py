from __future__ import annotations

from typing import Protocol

from arb.models import Quote


class VenueAdapter(Protocol):
    name: str

    def fetch_quotes(self, sport: str) -> list[Quote]:
        ...

