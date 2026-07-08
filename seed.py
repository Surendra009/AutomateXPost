"""Seed database with realistic fake drafts for UI testing."""

from datetime import datetime, timedelta
import hashlib

from database import get_session, init_db
from models import Draft, Headline
from pipeline.story_key import title_fingerprint

SEED_HEADLINES = [
    {
        "source": "CNBC Markets",
        "url": "https://example.com/nvda-earnings",
        "title": "Nvidia beats Q4 estimates as data center revenue surges 93%",
        "summary": "Nvidia reported quarterly revenue of $22.1 billion, topping analyst estimates of $20.4 billion.",
        "format": "BREAKING",
        "text": "Nvidia beat Q4 estimates\n$22.1B revenue vs $20.4B expected — mostly data center\n\n$NVDA",
        "impact": "high",
        "category": "earnings",
        "tickers": "NVDA",
        "confidence": 0.93,
    },
    {
        "source": "Reuters Business",
        "url": "https://example.com/fed-rates",
        "title": "Fed holds rates steady, signals two cuts possible in 2026",
        "summary": "The Federal Reserve kept rates at 4.25%-4.50%. Officials penciled in two 2026 cuts vs one in December.",
        "format": "CONTEXT",
        "text": "Fed held rates at 4.25-4.50%\nNow penciling two cuts in 2026, up from one — a bit more dovish\n\n$SPY",
        "impact": "high",
        "category": "macro",
        "tickers": "SPY",
        "confidence": 0.90,
    },
    {
        "source": "CNBC Markets",
        "url": "https://example.com/rivn-offering",
        "title": "Rivian shares fall after $1.5 billion stock offering",
        "summary": "Rivian sold 75 million shares at about $20 to raise $1.5 billion. Stock dropped on dilution concerns.",
        "format": "BREAKING",
        "text": "Rivian sold 75M shares at ~$20\nRaising $1.5B — stock down ~15% on dilution fears\n\n$RIVN",
        "impact": "high",
        "category": "earnings",
        "tickers": "RIVN",
        "confidence": 0.91,
    },
    {
        "source": "Reuters Business",
        "url": "https://example.com/openai-funding",
        "title": "OpenAI reportedly in talks to raise $40B at $300B valuation",
        "summary": "OpenAI is negotiating a funding round at a $300 billion valuation.",
        "format": "CONTEXT",
        "text": "OpenAI reportedly raising $40B at a $300B valuation\nWould roughly double its last round\n\n$MSFT",
        "impact": "med",
        "category": "ai",
        "tickers": "MSFT",
        "confidence": 0.82,
    },
    {
        "source": "SEC EDGAR 8-K",
        "url": "https://example.com/tsla-8k",
        "title": "Tesla files 8-K: CEO compensation plan approved by shareholders",
        "summary": "Tesla shareholders approved Musk pay package tied to market cap milestones.",
        "format": "BREAKING",
        "text": "Tesla shareholders approved Musk's new pay package\nClears a governance overhang that's been open since Delaware\n\n$TSLA",
        "impact": "med",
        "category": "regulatory",
        "tickers": "TSLA",
        "confidence": 0.86,
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
                title_fp=title_fingerprint(item["title"]),
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
