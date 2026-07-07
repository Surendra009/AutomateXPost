"""Seed database with realistic fake drafts for UI testing."""

from datetime import datetime, timedelta
import hashlib

from database import get_session, init_db
from models import Draft, Headline

SEED_HEADLINES = [
    {
        "source": "CNBC Markets",
        "url": "https://example.com/nvda-earnings",
        "title": "Nvidia beats Q4 estimates as data center revenue surges 93%",
        "summary": "Nvidia reported quarterly revenue of $22.1 billion, topping analyst estimates of $20.4 billion. Data center revenue hit $18.4 billion, up 93% year over year, driven by demand for AI training chips.",
        "format": "BREAKING",
        "text": "Nvidia beat Q4 — revenue came in at $22.1B vs ~$20.4B expected, with data center sales at $18.4B (+93% YoY). The entire beat was AI infrastructure demand. $NVDA",
        "impact": "high",
        "category": "earnings",
        "tickers": "NVDA",
        "confidence": 0.92,
    },
    {
        "source": "Reuters Business",
        "url": "https://example.com/fed-rates",
        "title": "Fed holds rates steady, signals two cuts possible in 2026",
        "summary": "The Federal Reserve kept its benchmark rate at 4.25%-4.50%. Updated projections show officials penciling in two rate cuts for 2026, up from one in the December forecast.",
        "format": "CONTEXT",
        "text": "Fed held at 4.25-4.50% but the dot plot now shows two cuts penciled in for 2026, up from one in December. $SPY little changed; $XLRE and $XLU among the early leaders.",
        "impact": "high",
        "category": "macro",
        "tickers": "SPY,QQQ",
        "confidence": 0.88,
    },
    {
        "source": "TechCrunch",
        "url": "https://example.com/openai-funding",
        "title": "OpenAI reportedly in talks to raise $40B at $300B valuation",
        "summary": "Sources say OpenAI is negotiating a funding round that would value the company at $300 billion, roughly double its last raise. SoftBank and others are reportedly in talks to lead.",
        "format": "SUMMARY",
        "text": "OpenAI is reportedly in talks to raise $40B at a $300B valuation, per people familiar with the matter.\n\nThat would roughly double its last round and make it the most valuable private AI company. Watch $MSFT (49% owner) and $GOOGL — a better-capitalized OpenAI tightens the race in enterprise AI.",
        "impact": "med",
        "category": "ai",
        "tickers": "MSFT,GOOGL",
        "confidence": 0.75,
    },
    {
        "source": "The Verge AI",
        "url": "https://example.com/anthropic-claude",
        "title": "Anthropic launches Claude with expanded computer use capabilities",
        "summary": "Anthropic announced Claude can now interact with desktop applications — clicking, typing, and navigating UIs on behalf of users. The feature rolls out to enterprise API customers first.",
        "format": "CONTEXT",
        "text": "Anthropic shipped expanded computer-use for Claude — the model can now click and type inside desktop apps via the API. It's enterprise-only for now, but puts pressure on OpenAI's agent roadmap. $GOOGL",
        "impact": "med",
        "category": "ai",
        "tickers": "GOOGL",
        "confidence": 0.70,
    },
    {
        "source": "SEC EDGAR 8-K",
        "url": "https://example.com/tsla-8k",
        "title": "Tesla files 8-K: CEO compensation plan approved by shareholders",
        "summary": "Tesla shareholders approved an updated compensation package for CEO Elon Musk at the annual meeting. The plan ties payouts to market cap and operational milestones through 2030.",
        "format": "BREAKING",
        "text": "Tesla shareholders approved Musk's updated pay package in an 8-K filing today — payouts are tied to market cap and ops milestones through 2030. Removes a lingering governance overhang. $TSLA",
        "impact": "med",
        "category": "regulatory",
        "tickers": "TSLA",
        "confidence": 0.85,
    },
]


def seed(force: bool = False) -> None:
    init_db()
    with get_session() as session:
        existing = session.exec(
            __import__("sqlmodel").select(Draft).where(Draft.status == "pending")
        ).all()
        if existing and not force:
            print(f"Already have {len(existing)} pending drafts, skipping seed.")
            return
        if existing and force:
            for d in existing:
                session.delete(d)
            session.commit()

        now = datetime.utcnow()
        for i, item in enumerate(SEED_HEADLINES):
            published = now - timedelta(minutes=15 * (len(SEED_HEADLINES) - i))
            chash = hashlib.sha256(f"{item['title']}|{item['url']}".encode()).hexdigest()[:32]

            headline = Headline(
                source=item["source"],
                url=item["url"],
                title=item["title"],
                summary=item["summary"],
                published_at=published,
                hash=chash,
                status="drafted",
            )
            session.add(headline)
            session.flush()

            draft = Draft(
                headline_id=headline.id,
                text=item["text"],
                format=item["format"],
                impact=item["impact"],
                category=item["category"],
                tickers=item["tickers"],
                confidence=item["confidence"],
                status="pending",
                created_at=published,
            )
            session.add(draft)

        session.commit()
        print(f"Seeded {len(SEED_HEADLINES)} fake drafts.")


if __name__ == "__main__":
    import sys
    seed(force="--force" in sys.argv)
