"""Seed database with realistic fake drafts for UI testing."""

from datetime import datetime, timedelta

from database import get_session, init_db
from models import Draft, Headline

SEED_HEADLINES = [
    {
        "source": "CNBC Markets",
        "url": "https://example.com/nvda-earnings",
        "title": "Nvidia beats Q4 estimates as data center revenue surges 93%",
        "summary": "Nvidia reported quarterly revenue of $22.1 billion, topping analyst estimates. Data center segment drove growth amid AI chip demand.",
        "format": "BREAKING",
        "text": "NVDA BEATS Q4 ESTIMATES — DATA CENTER REVENUE UP 93% TO $18.4B",
        "impact": "high",
        "category": "earnings",
        "tickers": "NVDA",
        "confidence": 0.92,
    },
    {
        "source": "Reuters Business",
        "url": "https://example.com/fed-rates",
        "title": "Fed holds rates steady, signals two cuts possible in 2026",
        "summary": "The Federal Reserve kept its benchmark rate unchanged at 4.25-4.50%, with officials projecting potential rate cuts later this year.",
        "format": "CONTEXT",
        "text": "$SPY flat after the Fed held rates at 4.25-4.50% and signaled up to two cuts may come in 2026.",
        "impact": "high",
        "category": "macro",
        "tickers": "SPY,QQQ",
        "confidence": 0.88,
    },
    {
        "source": "TechCrunch",
        "url": "https://example.com/openai-funding",
        "title": "OpenAI reportedly in talks to raise $40B at $300B valuation",
        "summary": "Sources say OpenAI is negotiating a massive funding round that would make it one of the most valuable private companies globally.",
        "format": "CONTEXT",
        "text": "OpenAI is reportedly in talks to raise $40B at a $300B valuation, which would reshape the AI sector's competitive landscape.",
        "impact": "med",
        "category": "ai",
        "tickers": "MSFT,GOOGL",
        "confidence": 0.75,
    },
    {
        "source": "The Verge AI",
        "url": "https://example.com/anthropic-claude",
        "title": "Anthropic launches Claude with expanded computer use capabilities",
        "summary": "Anthropic announced new agentic features allowing Claude to interact with desktop applications autonomously.",
        "format": "SUMMARY",
        "text": "Anthropic rolled out expanded computer-use features for Claude, letting the model interact with desktop apps.\n\nThe update targets enterprise automation workflows. Rivals including OpenAI and Google are racing to ship similar agent capabilities.",
        "impact": "med",
        "category": "ai",
        "tickers": "GOOGL",
        "confidence": 0.70,
    },
    {
        "source": "SEC EDGAR 8-K",
        "url": "https://example.com/tsla-8k",
        "title": "Tesla files 8-K: CEO compensation plan approved by shareholders",
        "summary": "Tesla Inc filed Form 8-K regarding shareholder approval of executive compensation arrangements.",
        "format": "BREAKING",
        "text": "TESLA 8-K: SHAREHOLDERS APPROVE UPDATED CEO COMPENSATION PLAN",
        "impact": "med",
        "category": "regulatory",
        "tickers": "TSLA",
        "confidence": 0.85,
    },
]


def seed() -> None:
    init_db()
    with get_session() as session:
        existing = session.exec(
            __import__("sqlmodel").select(Draft).where(Draft.status == "pending")
        ).all()
        if existing:
            print(f"Already have {len(existing)} pending drafts, skipping seed.")
            return

        now = datetime.utcnow()
        for i, item in enumerate(SEED_HEADLINES):
            published = now - timedelta(minutes=15 * (len(SEED_HEADLINES) - i))
            import hashlib
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
    seed()
