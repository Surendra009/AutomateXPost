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
        "summary": "Nvidia reported quarterly revenue of $22.1 billion, topping analyst estimates of $20.4 billion.",
        "format": "BREAKING",
        "text": "Nvidia printed $22.1B in Q4 revenue vs $20.4B expected — a $1.7B beat. Data center alone did $18.4B (+93% YoY), which is basically the entire AI capex cycle showing up in one line item. Supply remains the gating factor, not demand. $NVDA $AMD $AVGO",
        "impact": "high",
        "category": "earnings",
        "tickers": "NVDA,AMD,AVGO",
        "confidence": 0.93,
    },
    {
        "source": "Reuters Business",
        "url": "https://example.com/fed-rates",
        "title": "Fed holds rates steady, signals two cuts possible in 2026",
        "summary": "The Federal Reserve kept rates at 4.25%-4.50%. Officials penciled in two 2026 cuts vs one in December.",
        "format": "CONTEXT",
        "text": "The hawkish surprise isn't the hold — it's the dots. Fed officials now pencil two cuts in 2026 vs one back in December, even with inflation still above target. That's a dovish shift in the forecast, not the statement. $SPY $TLT $XLF",
        "impact": "high",
        "category": "macro",
        "tickers": "SPY,TLT,XLF",
        "confidence": 0.90,
    },
    {
        "source": "TechCrunch",
        "url": "https://example.com/openai-funding",
        "title": "OpenAI reportedly in talks to raise $40B at $300B valuation",
        "summary": "OpenAI is negotiating a funding round at a $300 billion valuation.",
        "format": "SUMMARY",
        "text": "OpenAI is reportedly negotiating a $40B round at a $300B valuation — roughly 2x its last mark, per sources.\n\nThe read-through: more private capital chasing compute means $MSFT's Azure/OpenAI tie-up faces a better-funded partner-competitor, while $GOOGL and $META need to keep pace on model spend. Watch whether SoftBank leads and what compute commitments come with it.",
        "impact": "med",
        "category": "ai",
        "tickers": "MSFT,GOOGL,META",
        "confidence": 0.82,
    },
    {
        "source": "The Verge AI",
        "url": "https://example.com/anthropic-claude",
        "title": "Anthropic launches Claude with expanded computer use capabilities",
        "summary": "Claude can now interact with desktop apps via API for enterprise customers.",
        "format": "CONTEXT",
        "text": "Anthropic's computer-use API lets Claude click/type inside desktop apps — enterprise-only for now, but it's the first shipped agent loop at scale outside labs. Read-through is workflow automation vendors and $CRM/$NOW integration partners; the bottleneck shifts from model IQ to permissions/security.",
        "impact": "med",
        "category": "ai",
        "tickers": "CRM,NOW",
        "confidence": 0.78,
    },
    {
        "source": "SEC EDGAR 8-K",
        "url": "https://example.com/tsla-8k",
        "title": "Tesla files 8-K: CEO compensation plan approved by shareholders",
        "summary": "Tesla shareholders approved Musk pay package tied to market cap milestones through 2030.",
        "format": "BREAKING",
        "text": "Tesla 8-K: shareholders signed off on Musk's updated comp plan — payouts hinge on market-cap and ops milestones through 2030. Removes a governance overhang that's been hanging since the Delaware ruling. Doesn't fix demand, but one less headline risk for holders. $TSLA",
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
