"""Build the Intent Board — what this cycle must try to cover.

Step 0: returns an empty board with a clear TODO for calendar/watchlist seeds.
Later steps seed earnings prints, macro/Fed, watchlist, and themes.
"""

from __future__ import annotations

from logging_config import setup_logging
from pipeline.v2.types import Intent

logger = setup_logging()


def build_intent_board() -> list[Intent]:
    """Return must-cover intents for this cycle.

    Step 0 scaffold: no seeds yet. Step 1 will add:
    - Finnhub earnings calendar → earnings_print
    - Finnhub economic calendar → macro_print / fed_decision
    - Watchlist → company_material
    - Themes → ai_catalyst / politics_policy / fed_speak
    """
    intents: list[Intent] = []
    logger.info("v2 intent board: %d intents (scaffold — seeds not wired)", len(intents))
    return intents
