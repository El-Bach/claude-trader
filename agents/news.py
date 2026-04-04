"""
NEWS — News & Sentiment Analyst
APEX Capital AI

Monitors the outside world continuously.
Broadcasts risk level, events, and sentiment to the whole team.

Data sources:
- ForexFactory economic calendar (scheduled events)
- NewsAPI.org (breaking financial news)
- CNN Fear & Greed Index (market sentiment)
- RSS feeds (Reuters, FXStreet, Investing.com)
- Derived VIX proxy from price volatility

Risk levels broadcast:
- CRITICAL: trade immediately (news in < 15 min)
- HIGH:     no new entries allowed
- MEDIUM:   reduce confidence thresholds by 10 points
- LOW:      normal operation

Runs FIRST in every main cycle — before DOLLAR.
Collaborates with MONITOR for real-time position protection.
"""

import os
import json
import feedparser
import requests
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

# ── News API config ───────────────────────────────────────────────
NEWS_API_KEY = os.getenv("NEWS_API_KEY", "")   # newsapi.org free key

# ── ForexFactory countries to monitor ────────────────────────────
FF_COUNTRIES = ["USD", "EUR", "GBP", "JPY", "XAU"]

# ── RSS feeds (free, no key required) ────────────────────────────
# Investing.com removed — frequently blocks scrapers and returns stale data
RSS_FEEDS = {
    "FXStreet":   "https://www.fxstreet.com/rss/news",
    "ForexLive":  "https://www.forexlive.com/feed/news",
    "Reuters":    "https://feeds.reuters.com/reuters/businessNews",
    "MarketWatch":"https://feeds.content.dowjones.io/public/rss/mw_realtimeheadlines",
}

# ── Keywords that trigger HIGH risk ──────────────────────────────
HIGH_RISK_KEYWORDS = [
    "fed rate", "federal reserve", "fomc", "powell",
    "ecb rate", "lagarde", "boj intervention", "bank of japan",
    "emergency", "surprise rate", "flash crash", "circuit breaker",
    "war", "attack", "sanctions", "default", "crisis",
    "cpi", "inflation", "nfp", "non-farm payroll", "gdp",
    "recession", "geopolit",
]

MEDIUM_RISK_KEYWORDS = [
    "fed speak", "pmi", "retail sales", "trade balance",
    "unemployment", "consumer confidence", "housing",
    "oil", "opec", "treasury", "yield",
]


class NewsAgent:
    NAME = "NEWS"

    def __init__(self):
        self.last_broadcast = None

    # ================================================================ #
    #  DATA FETCHING
    # ================================================================ #

    def fetch_forex_factory(self) -> list:
        """Fetch economic calendar from ForexFactory."""
        try:
            r = requests.get(
                "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
                timeout=10)
            if r.status_code != 200:
                return []
            now    = datetime.utcnow()
            events = []
            for e in r.json():
                if e.get("impact") not in ["High", "Medium"]:
                    continue
                if e.get("country") not in FF_COUNTRIES:
                    continue
                try:
                    et   = datetime.strptime(
                        e["date"], "%Y-%m-%dT%H:%M:%S%z"
                    ).replace(tzinfo=None)
                    diff = et - now
                    mins = diff.total_seconds() / 60

                    # Only events in next 4 hours or last 15 min
                    if -15 <= mins <= 240:
                        events.append({
                            "title":    e.get("title", "?"),
                            "country":  e.get("country", "?"),
                            "impact":   e.get("impact", "?"),
                            "time_utc": et.strftime("%H:%M UTC"),
                            "minutes":  round(mins, 0),
                            "previous": e.get("previous", ""),
                            "forecast": e.get("forecast", ""),
                        })
                except Exception:
                    pass
            return sorted(events, key=lambda x: x["minutes"])
        except Exception as e:
            print(f"[{self.NAME}] ForexFactory error: {e}")
            return []

    def fetch_rss_headlines(self) -> list:
        """Fetch latest headlines from RSS feeds. Returns up to 20 headlines."""
        headlines    = []
        feeds_ok     = 0
        feeds_failed = 0

        for source, url in RSS_FEEDS.items():
            try:
                feed = feedparser.parse(url)
                if not feed.entries:
                    feeds_failed += 1
                    continue
                feeds_ok += 1
                for entry in feed.entries[:5]:   # top 5 per source
                    title = entry.get("title", "").strip()
                    if not title or len(title) < 10:
                        continue
                    # Parse published time for freshness display
                    pub_raw = entry.get("published", "")
                    try:
                        import email.utils
                        ts = email.utils.parsedate_to_datetime(pub_raw)
                        age_min = (datetime.utcnow() - ts.replace(tzinfo=None)).total_seconds() / 60
                        age_str = f"{int(age_min)}m ago" if age_min < 120 else f"{int(age_min/60)}h ago"
                    except Exception:
                        age_str = ""

                    headlines.append({
                        "source": source,
                        "title":  title[:120],
                        "time":   pub_raw,
                        "age":    age_str,
                    })
            except Exception as e:
                feeds_failed += 1
                print(f"[{self.NAME}] RSS {source} failed: {e}")

        print(f"[{self.NAME}] RSS: {feeds_ok}/{len(RSS_FEEDS)} feeds OK, "
              f"{len(headlines)} headlines fetched")
        return headlines[:20]

    def fetch_fear_greed(self) -> dict:
        """Fetch CNN Fear & Greed Index with proper browser headers."""
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            "Referer": "https://edition.cnn.com/markets/fear-and-greed",
            "Accept":  "application/json, text/plain, */*",
            "Origin":  "https://edition.cnn.com",
        }

        # Try primary CNN endpoint
        urls = [
            "https://production.dataviz.cnn.io/index/fearandgreed/graphdata",
            "https://production.dataviz.cnn.io/index/fearandgreed/graphdata/1",
        ]
        for url in urls:
            try:
                r = requests.get(url, timeout=10, headers=headers)
                print(f"[{self.NAME}] Fear/Greed HTTP {r.status_code} from {url.split('/')[-1]}")
                if r.status_code == 200:
                    data  = r.json()
                    fg    = data.get("fear_and_greed", {})
                    score = fg.get("score")
                    if score is not None:
                        score_f = round(float(score), 1)
                        rating  = fg.get("rating", "Neutral")
                        regime  = ("RISK_OFF" if score_f < 40 else
                                   "RISK_ON"  if score_f > 60 else "NEUTRAL")
                        print(f"[{self.NAME}] Fear/Greed: {score_f} ({rating}) → {regime}")
                        return {"score": score_f, "rating": rating, "regime": regime}
                    else:
                        print(f"[{self.NAME}] Fear/Greed: unexpected JSON: {str(data)[:120]}")
                else:
                    print(f"[{self.NAME}] Fear/Greed: {r.status_code} — {r.text[:80]}")
            except Exception as e:
                print(f"[{self.NAME}] Fear/Greed error ({url.split('/')[-1]}): {e}")

        # Fallback: Alternative.me (crypto-based but directionally useful)
        try:
            r = requests.get(
                "https://api.alternative.me/fng/?limit=1",
                timeout=8,
                headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code == 200:
                entry  = r.json().get("data", [{}])[0]
                score_f = round(float(entry.get("value", 50)), 1)
                rating  = entry.get("value_classification", "Neutral")
                regime  = ("RISK_OFF" if score_f < 40 else
                           "RISK_ON"  if score_f > 60 else "NEUTRAL")
                print(f"[{self.NAME}] Fear/Greed fallback (Crypto): {score_f} ({rating})")
                return {"score": score_f, "rating": rating, "regime": regime}
        except Exception as e:
            print(f"[{self.NAME}] Fear/Greed fallback error: {e}")

        print(f"[{self.NAME}] Fear/Greed: all sources failed — using neutral default")
        return {"score": 50, "rating": "Neutral", "regime": "NEUTRAL"}

    def fetch_newsapi(self) -> list:
        """Fetch breaking financial news from NewsAPI."""
        if not NEWS_API_KEY:
            return []
        try:
            r = requests.get(
                "https://newsapi.org/v2/top-headlines",
                params={
                    "category": "business",
                    "language": "en",
                    "pageSize": 10,
                    "apiKey":   NEWS_API_KEY,
                },
                timeout=8)
            if r.status_code == 200:
                articles = r.json().get("articles", [])
                return [{
                    "source": a.get("source", {}).get("name", "?"),
                    "title":  a.get("title", "")[:120],
                    "time":   a.get("publishedAt", ""),
                } for a in articles if a.get("title")]
        except Exception:
            pass
        return []

    # ================================================================ #
    #  RISK ASSESSMENT
    # ================================================================ #

    def _assess_event_risk(self, events: list) -> tuple[str, list]:
        """Assess risk level from upcoming economic events."""
        critical_events = []
        high_events     = []
        medium_events   = []

        for e in events:
            mins   = e.get("minutes", 999)
            impact = e.get("impact", "")
            title  = e.get("title", "").lower()

            if impact == "High" and -5 <= mins <= 15:
                critical_events.append(e)
            elif impact == "High" and mins <= 60:
                high_events.append(e)
            elif impact == "Medium" and mins <= 30:
                medium_events.append(e)
            elif impact == "High" and mins <= 120:
                medium_events.append(e)

        if critical_events:
            return "CRITICAL", critical_events
        elif high_events:
            return "HIGH", high_events
        elif medium_events:
            return "MEDIUM", medium_events
        return "LOW", []

    def _assess_headline_risk(self, headlines: list) -> tuple[str, list]:
        """
        Check headlines for risk keywords.
        Capped at MEDIUM — old RSS headlines mentioning 'fed' or 'cpi'
        should never block entries on their own. Only ForexFactory timed
        events drive HIGH/CRITICAL.
        """
        medium_hits = []

        for h in headlines:
            title_lower = h.get("title", "").lower()
            for kw in HIGH_RISK_KEYWORDS + MEDIUM_RISK_KEYWORDS:
                if kw in title_lower:
                    medium_hits.append(h)
                    break

        if medium_hits:
            return "MEDIUM", medium_hits[:3]
        return "LOW", []

    def _combine_risk(self, event_risk: str,
                      headline_risk: str,
                      fg_regime: str) -> str:
        """Combine all risk signals into final risk level."""
        risk_order = {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1}

        max_risk = max(
            risk_order.get(event_risk, 1),
            risk_order.get(headline_risk, 1),
            2 if fg_regime == "RISK_OFF" else 1,
        )

        reverse = {4: "CRITICAL", 3: "HIGH", 2: "MEDIUM", 1: "LOW"}
        return reverse[max_risk]

    # ================================================================ #
    #  RULE-BASED SYNTHESIS (no AI required)
    # ================================================================ #

    def _synthesise(self, events: list, headlines: list,
                    fear_greed: dict, risk_level: str,
                    key_events: list) -> dict:
        """
        Derive all trading implications from raw data without any AI call.
        Uses Fear/Greed for sentiment, ForexFactory events for USD bias,
        and keyword counting for instrument implications.
        """
        fg_score = fear_greed.get("score", 50)
        fg_regime = fear_greed.get("regime", "NEUTRAL")

        # ── Sentiment from Fear & Greed ───────────────────────────────
        if fg_score < 35:
            sentiment = "RISK_OFF"
        elif fg_score > 65:
            sentiment = "RISK_ON"
        else:
            sentiment = "NEUTRAL"

        # ── USD bias from upcoming HIGH impact USD events ─────────────
        usd_events = [e for e in events
                      if e.get("country") == "USD" and e.get("impact") == "High"]
        usd_bias = "NEUTRAL"
        if usd_events:
            # If USD event imminent: mark neutral (direction unknown before release)
            usd_bias = "NEUTRAL"

        # Refine with headline keywords
        all_text = " ".join(h.get("title", "").lower() for h in headlines)
        bullish_usd_kw = [
            "dollar rises", "dollar gains", "dollar strength", "usd strength",
            "hawkish", "rate hike", "strong jobs", "beat expectations",
            "above forecast", "stronger than expected", "hot inflation",
            "fed holds", "no rate cut", "higher for longer",
        ]
        bearish_usd_kw = [
            "dollar falls", "dollar weakens", "dollar drops", "usd weakness",
            "dovish", "rate cut", "miss expectations", "below forecast",
            "recession", "weaker than expected", "soft inflation",
            "tariff", "trade war", "deficit", "debt ceiling",
        ]
        bull_hits = sum(1 for kw in bullish_usd_kw if kw in all_text)
        bear_hits = sum(1 for kw in bearish_usd_kw if kw in all_text)
        if bull_hits > bear_hits:
            usd_bias = "BULLISH"
        elif bear_hits > bull_hits:
            usd_bias = "BEARISH"

        # ── Instrument implications ───────────────────────────────────
        # Gold: RISK_OFF → BULLISH (safe haven), strong USD → BEARISH
        if sentiment == "RISK_OFF" or usd_bias == "BEARISH":
            gold_impl = "BULLISH"
        elif sentiment == "RISK_ON" and usd_bias == "BULLISH":
            gold_impl = "BEARISH"
        else:
            gold_impl = "NEUTRAL"

        # EURUSD: inverse of USD bias
        eurusd_impl = {"BULLISH": "BEARISH", "BEARISH": "BULLISH",
                       "NEUTRAL": "NEUTRAL"}.get(usd_bias, "NEUTRAL")

        # USDJPY: USD bias + risk sentiment (RISK_OFF strengthens JPY = bearish USDJPY)
        if sentiment == "RISK_OFF":
            usdjpy_impl = "BEARISH"
        elif usd_bias == "BULLISH":
            usdjpy_impl = "BULLISH"
        elif usd_bias == "BEARISH":
            usdjpy_impl = "BEARISH"
        else:
            usdjpy_impl = "NEUTRAL"

        # ── Risk reason and key events ────────────────────────────────
        block = risk_level in ("CRITICAL", "HIGH")

        if key_events:
            e = key_events[0]
            risk_reason = (f"{e.get('impact','?')} impact event: "
                           f"{e.get('title','?')} ({e.get('country','?')}) "
                           f"in {e.get('minutes',0):.0f} min")
        elif risk_level == "MEDIUM":
            risk_reason = "Medium-impact events or elevated news sentiment"
        else:
            risk_reason = "No significant events — normal conditions"

        key_event_titles = [
            f"{e.get('title','?')} ({e.get('country','?')}) in {e.get('minutes',0):.0f} min"
            for e in key_events[:3]
        ]

        # ── Dynamic summary ───────────────────────────────────────────
        status_str = "⛔ Entries BLOCKED" if block else "✅ Normal conditions"
        usd_str    = f"USD {usd_bias}" if usd_bias != "NEUTRAL" else "USD neutral"
        summary = (
            f"{status_str}. {usd_str}. "
            f"Fear/Greed {fg_score:.0f}/100 ({fear_greed.get('rating','Neutral')}). "
            f"Sentiment: {sentiment}."
        )
        if key_event_titles:
            summary += f" Event: {key_event_titles[0]}"

        # ── Top 3 most relevant headlines ────────────────────────────
        # Prefer headlines that match trading keywords
        scored = []
        all_kw = HIGH_RISK_KEYWORDS + MEDIUM_RISK_KEYWORDS
        for h in headlines:
            t     = h.get("title", "").lower()
            score = sum(1 for kw in all_kw if kw in t)
            scored.append((score, h))
        scored.sort(key=lambda x: -x[0])
        top_headlines = [h for _, h in scored[:3]] if scored else headlines[:3]

        # Keyword hit counts for transparency
        all_text   = " ".join(h.get("title", "").lower() for h in headlines)
        hawk_hits  = sum(1 for kw in bullish_usd_kw if kw in all_text)
        dove_hits  = sum(1 for kw in bearish_usd_kw if kw in all_text)

        return {
            "risk_level":         risk_level,
            "risk_reason":        risk_reason,
            "sentiment":          sentiment,
            "sentiment_reason":   f"Fear/Greed at {fg_score:.0f} ({fear_greed.get('rating','Neutral')})",
            "usd_bias":           usd_bias,
            "gold_implication":   gold_impl,
            "eurusd_implication": eurusd_impl,
            "usdjpy_implication": usdjpy_impl,
            "block_new_entries":  block,
            "key_events":         key_event_titles,
            "top_headlines":      top_headlines,
            "hawk_hits":          hawk_hits,
            "dove_hits":          dove_hits,
            "summary":            summary,
        }

    # ================================================================ #
    #  MAIN ANALYSE FUNCTION
    # ================================================================ #

    def analyse(self) -> dict:
        """
        Run full news and sentiment analysis.
        Returns broadcast dict consumed by MANAGER and all agents.
        """
        print(f"\n[{self.NAME}] Starting news & sentiment analysis...")

        # Fetch all data sources
        events     = self.fetch_forex_factory()
        headlines  = self.fetch_rss_headlines()
        fear_greed = self.fetch_fear_greed()
        breaking   = self.fetch_newsapi()

        # Combine all headlines
        all_headlines = headlines + breaking

        # Pre-assess risk without Claude
        event_risk,    key_events    = self._assess_event_risk(events)
        headline_risk, key_headlines = self._assess_headline_risk(
            all_headlines)
        combined_risk = self._combine_risk(
            event_risk, headline_risk, fear_greed.get("regime", "NEUTRAL"))

        # Rule-based synthesis — no AI required
        analysis = self._synthesise(
            events, all_headlines, fear_greed, combined_risk, key_events)

        # Build broadcast
        broadcast = {
            "agent":              self.NAME,
            "timestamp":          datetime.utcnow().isoformat(),
            "risk_level":         analysis.get("risk_level", combined_risk),
            "risk_reason":        analysis.get("risk_reason", ""),
            "sentiment":          analysis.get("sentiment", "NEUTRAL"),
            "sentiment_reason":   analysis.get("sentiment_reason", ""),
            "usd_bias":           analysis.get("usd_bias", "NEUTRAL"),
            "gold_implication":   analysis.get("gold_implication", "NEUTRAL"),
            "eurusd_implication": analysis.get("eurusd_implication", "NEUTRAL"),
            "usdjpy_implication": analysis.get("usdjpy_implication", "NEUTRAL"),
            "block_new_entries":  analysis.get("block_new_entries", False),
            "key_events":         analysis.get("key_events", []),
            "summary":            analysis.get("summary", ""),
            "fear_greed_score":   fear_greed.get("score", 50),
            "fear_greed_rating":  fear_greed.get("rating", "Neutral"),
            "upcoming_events":    events[:5],
            "event_count":        len(events),
            "top_headlines":      analysis.get("top_headlines", []),
            "headline_count":     len(all_headlines),
            "hawk_hits":          analysis.get("hawk_hits", 0),
            "dove_hits":          analysis.get("dove_hits", 0),
        }

        self.last_broadcast = broadcast

        # Print summary
        risk  = broadcast["risk_level"]
        sent  = broadcast["sentiment"]
        fg    = broadcast["fear_greed_score"]
        block = broadcast["block_new_entries"]

        risk_icon = {
            "CRITICAL": "🚨", "HIGH": "⚠️",
            "MEDIUM": "⚡", "LOW": "✅"
        }.get(risk, "❓")

        print(f"[{self.NAME}] {risk_icon} Risk: {risk} | "
              f"Sentiment: {sent} | "
              f"Fear/Greed: {fg:.0f} | "
              f"Block entries: {block}")
        print(f"[{self.NAME}] {broadcast['risk_reason']}")
        print(f"[{self.NAME}] USD:{broadcast['usd_bias']} | "
              f"Gold:{broadcast['gold_implication']} | "
              f"EURUSD:{broadcast['eurusd_implication']} | "
              f"USDJPY:{broadcast['usdjpy_implication']}")

        if broadcast["key_events"]:
            print(f"[{self.NAME}] Key events:")
            for ev in broadcast["key_events"]:
                print(f"[{self.NAME}]   → {ev}")

        if events:
            print(f"[{self.NAME}] Upcoming ({len(events)} events):")
            for e in events[:3]:
                mins = e.get("minutes", 0)
                timing = f"in {mins:.0f} min" if mins > 0 else "NOW"
                print(f"[{self.NAME}]   [{e['impact']}] "
                      f"{e['title']} ({e['country']}) {timing}")

        top = broadcast.get("top_headlines", [])
        if top:
            print(f"[{self.NAME}] Top headlines ({broadcast.get('headline_count',0)} total, "
                  f"hawk:{broadcast.get('hawk_hits',0)} dove:{broadcast.get('dove_hits',0)}):")
            for h in top:
                age = f" [{h.get('age','')}]" if h.get("age") else ""
                print(f"[{self.NAME}]   [{h.get('source','')}]{age} {h.get('title','')[:80]}")

        return broadcast

    # ================================================================ #
    #  HELPERS FOR OTHER AGENTS
    # ================================================================ #

    def is_high_risk(self) -> bool:
        if not self.last_broadcast:
            return False
        return self.last_broadcast.get("risk_level") in ("CRITICAL", "HIGH")

    def get_risk_level(self) -> str:
        if not self.last_broadcast:
            return "LOW"
        return self.last_broadcast.get("risk_level", "LOW")

    def should_block_entries(self) -> bool:
        if not self.last_broadcast:
            return False
        return self.last_broadcast.get("block_new_entries", False)

    def get_summary_for_telegram(self) -> str:
        if not self.last_broadcast:
            return "No news analysis available"
        b    = self.last_broadcast
        risk = b.get("risk_level", "LOW")
        icon = {"CRITICAL":"🚨","HIGH":"⚠️","MEDIUM":"⚡","LOW":"✅"}.get(risk,"❓")
        return (
            f"{icon} <b>News Risk: {risk}</b>\n"
            f"Sentiment: {b.get('sentiment','?')} | "
            f"Fear/Greed: {b.get('fear_greed_score',50):.0f}/100\n"
            f"{b.get('summary','')}"
        )
