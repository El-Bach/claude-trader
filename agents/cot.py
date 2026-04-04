"""
COT — CFTC Commitment of Traders (free, no API key)
APEX Capital AI

Fetches the latest non-commercial (speculative) net positioning for:
  EUR  → EURO FX futures (proxy for EUR/USD direction)
  GBP  → British Pound futures (proxy for GBP/USD direction)
  JPY  → Japanese Yen futures (inverted → bearish JPY futures = bullish USD/JPY)
  GOLD → Gold (COMEX) futures

Data source: CFTC Socrata public API — updated every Friday (Tuesday snapshot).
No auth required. Full try/except — returns {"available": False} on any failure.
"""

import requests

# ── CFTC Socrata public API endpoints ─────────────────────────────────────────
_FIN_URL  = "https://publicreporting.cftc.gov/resource/gpe5-46if.json"  # Financial futures
_COMM_URL = "https://publicreporting.cftc.gov/resource/6dca-aqww.json"  # Legacy (commodities incl. Gold)

_CONFIG = {
    "EUR": {
        "url":    _FIN_URL,
        "market": "EURO FX - CHICAGO MERCANTILE EXCHANGE",
        "invert": False,  # Long EUR futures = bullish EUR/USD
        "label":  "EUR/USD (CME Euro FX futures)",
    },
    "GBP": {
        "url":    _FIN_URL,
        "market": "BRITISH POUND STERLING - CHICAGO MERCANTILE EXCHANGE",
        "invert": False,  # Long GBP futures = bullish GBP/USD
        "label":  "GBP/USD (CME British Pound futures)",
    },
    "JPY": {
        "url":    _FIN_URL,
        "market": "JAPANESE YEN - CHICAGO MERCANTILE EXCHANGE",
        "invert": True,   # Long JPY futures = bullish JPY = BEARISH USD/JPY price
        "label":  "USD/JPY (CME JPY futures, inverted)",
    },
    "GOLD": {
        "url":    _COMM_URL,
        "market": "GOLD - COMMODITY EXCHANGE INC.",
        "invert": False,  # Long gold futures = bullish gold
        "label":  "Gold (COMEX GC futures)",
    },
}


def _signal(net_pct: float) -> str:
    if   net_pct >  30: return "EXTREME_BULLISH"
    elif net_pct >  15: return "BULLISH"
    elif net_pct < -30: return "EXTREME_BEARISH"
    elif net_pct < -15: return "BEARISH"
    else:               return "NEUTRAL"


def get_cot_data(instrument: str) -> dict:
    """
    Fetch latest 2 weeks of COT data for 'EUR', 'GBP', 'JPY', or 'GOLD'.

    Returns:
        available      : bool
        instrument     : str
        report_date    : str   YYYY-MM-DD (Tuesday snapshot date)
        net_position   : int   non-commercial long minus short (direction-adjusted)
        net_pct_oi     : float net as % of open interest (-100 to +100)
        weekly_change  : int   this week net minus last week net
        signal         : str   EXTREME_BULLISH | BULLISH | NEUTRAL | BEARISH | EXTREME_BEARISH
        label          : str   human-readable description
    """
    cfg = _CONFIG.get(instrument.upper())
    if not cfg:
        return {"available": False, "error": f"Unknown instrument: {instrument}"}

    try:
        params = {
            "market_and_exchange_names": cfg["market"],
            "$order":                    "report_date_as_yyyy_mm_dd DESC",
            "$limit":                    2,
        }
        resp = requests.get(cfg["url"], params=params, timeout=10)
        resp.raise_for_status()
        rows = resp.json()

        if not rows:
            return {"available": False, "error": "No COT data returned from CFTC API"}

        r     = rows[0]
        long_ = int(r.get("noncomm_positions_long_all",  0) or 0)
        short_= int(r.get("noncomm_positions_short_all", 0) or 0)
        oi    = int(r.get("open_interest_all",           1) or 1) or 1
        net   = long_ - short_

        if len(rows) > 1:
            r2       = rows[1]
            l2       = int(r2.get("noncomm_positions_long_all",  0) or 0)
            s2       = int(r2.get("noncomm_positions_short_all", 0) or 0)
            prev_net = l2 - s2
        else:
            prev_net = net

        change  = net - prev_net
        net_pct = round(net / oi * 100, 1)

        # Invert for JPY: long JPY futures = bullish JPY = bearish USD/JPY price
        if cfg["invert"]:
            net     = -net
            net_pct = -net_pct
            change  = -change

        return {
            "available":    True,
            "instrument":   instrument.upper(),
            "report_date":  r.get("report_date_as_yyyy_mm_dd", "unknown")[:10],
            "net_position": net,
            "net_pct_oi":   net_pct,
            "weekly_change": change,
            "signal":       _signal(net_pct),
            "label":        cfg["label"],
        }

    except Exception as e:
        return {"available": False, "error": str(e)}


def cot_text(cot: dict) -> str:
    """Format COT data for Claude prompt."""
    if not cot.get("available"):
        return f"COT data unavailable ({cot.get('error', 'unknown error')})"
    sign   = "+" if cot["net_position"] >= 0 else ""
    chg    = cot["weekly_change"]
    chg_s  = ("+" if chg >= 0 else "") + str(chg)
    return (
        f"Signal: {cot['signal']} | Net: {sign}{cot['net_position']:,} contracts "
        f"({sign}{cot['net_pct_oi']}% of OI) | Week-on-week change: {chg_s}\n"
        f"Report date: {cot['report_date']} (Tuesday snapshot, published Friday)\n"
        f"Source: {cot['label']}"
    )
