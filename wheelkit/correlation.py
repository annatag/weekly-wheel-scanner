"""Which underlyings are secretly the same bet.

The sector limit already in ``risk`` counts positions per GICS-style sector
string. That check cannot see the exposure it most needs to: ETFs arrive with
no sector at all, so GDX and SLV sat side by side as two "uncategorised"
positions and were treated as diversification. They are one trade on the gold
price with two tickers, and when the metal turned they turned together - the
two that needed managing in the same week were exactly these two.

So correlation is declared explicitly, by group, for the names this scanner
actually surfaces. A lookup table is the honest tool here: a rolling
correlation matrix estimated from a few hundred days of returns would put a
number on it, but the number moves most in the crisis when the grouping is
supposed to bind, and it would still need a threshold chosen by hand.

Unknown symbols belong to no group and are never constrained. The table is
meant to be extended - add a name the first time it is one of two open
positions that move together.
"""

from __future__ import annotations

# group name -> the tickers that are, for risk purposes, one position.
GROUPS: dict[str, tuple[str, ...]] = {
    "precious metals": (
        "GLD", "IAU", "GLDM", "SLV", "SIVR", "PSLV", "PHYS",
        "GDX", "GDXJ", "SIL", "SILJ", "RING", "NUGT", "JNUG",
        "NEM", "GOLD", "AEM", "KGC", "AU", "AUY", "PAAS", "AG", "HL",
        "WPM", "FNV", "RGLD", "EGO", "IAG", "BTG", "CDE", "SSRM",
    ),
    "industrial metals": (
        "COPX", "CPER", "XME", "PICK",
        "FCX", "SCCO", "TECK", "RIO", "BHP", "VALE", "AA", "CLF", "X", "NUE",
    ),
    "energy": (
        "XLE", "XOP", "OIH", "USO", "BNO", "AMLP", "VDE",
        "XOM", "CVX", "COP", "OXY", "SLB", "HAL", "DVN", "FANG", "EOG",
        "MRO", "APA", "HES", "PSX", "VLO", "MPC", "BKR",
    ),
    "natural gas": ("UNG", "BOIL", "AR", "EQT", "RRC", "SWN", "CHK", "LNG"),
    "semiconductors": (
        "SMH", "SOXX", "SOXL", "XSD",
        "NVDA", "AMD", "INTC", "MU", "QCOM", "AVGO", "TXN", "ADI", "MRVL",
        "AMAT", "LRCX", "KLAC", "ASML", "TSM", "ON", "NXPI", "SWKS", "STX", "WDC",
    ),
    "megacap tech": (
        "QQQ", "TQQQ", "XLK", "VGT",
        "AAPL", "MSFT", "GOOGL", "GOOG", "AMZN", "META", "NFLX", "CRM", "ORCL",
        "ADBE", "NOW",
    ),
    "us large cap": ("SPY", "VOO", "IVV", "SPXL", "RSP"),
    "us small cap": ("IWM", "TNA", "VTWO", "IJR"),
    "big banks": (
        "XLF", "KBE", "VFH",
        "JPM", "BAC", "C", "WFC", "GS", "MS", "USB", "PNC", "TFC", "SCHW",
    ),
    "regional banks": ("KRE", "IAT", "RF", "KEY", "CFG", "HBAN", "FITB", "ZION",
                       "MTB", "CMA", "ALLY"),
    "crypto proxies": (
        "BITO", "IBIT", "FBTC", "GBTC", "BITX", "ETHE",
        "COIN", "MARA", "RIOT", "CLSK", "HUT", "MSTR", "HOOD", "CIFR", "WULF",
    ),
    "china": ("FXI", "KWEB", "MCHI", "YINN", "ASHR",
              "BABA", "JD", "PDD", "NIO", "LI", "XPEV", "BIDU", "TCOM"),
    "airlines": ("JETS", "DAL", "UAL", "AAL", "LUV", "ALK", "JBLU", "SAVE"),
    "cruise and travel": ("CCL", "RCL", "NCLH", "ABNB", "EXPE", "BKNG", "MAR", "HLT"),
    "homebuilders": ("XHB", "ITB", "DHI", "LEN", "PHM", "TOL", "NVR", "KBH", "BZH"),
    "biotech": ("XBI", "IBB", "LABU", "MRNA", "BNTX", "NVAX", "SRPT", "ALNY"),
    "big pharma": ("XLV", "PFE", "MRK", "LLY", "BMY", "ABBV", "JNJ", "AMGN", "GILD"),
    "ev and clean energy": ("TSLA", "RIVN", "LCID", "NIO", "FSLR", "ENPH", "SEDG",
                            "PLUG", "RUN", "TAN", "ICLN"),
    "long duration rates": ("TLT", "TMF", "EDV", "ZROZ", "IEF", "VGLT"),
    "utilities": ("XLU", "NEE", "DUK", "SO", "D", "AEP", "EXC"),
    "retail": ("XRT", "WMT", "TGT", "COST", "HD", "LOW", "M", "KSS", "GPS", "DG",
               "DLTR", "BBY"),
    "payments and fintech": ("V", "MA", "PYPL", "SQ", "XYZ", "AFRM", "SOFI",
                             "UPST", "FI"),
}

# Built once: the reverse index is what every lookup actually needs. A ticker
# may sit in more than one group - NIO is both a China name and an EV name,
# and it should count against whichever exposure you already hold - so this
# maps to a tuple rather than to a single group.
_BY_SYMBOL: dict[str, tuple[str, ...]] = {}
for _group, _symbols in GROUPS.items():
    for _symbol in _symbols:
        _BY_SYMBOL[_symbol] = _BY_SYMBOL.get(_symbol, ()) + (_group,)


def groups_for(symbol: str) -> tuple[str, ...]:
    """Every correlation group a ticker belongs to; empty if unclassified."""
    return _BY_SYMBOL.get((symbol or "").strip().upper(), ())


def group_for(symbol: str) -> str | None:
    """The primary group, for messages that need to name just one."""
    found = groups_for(symbol)
    return found[0] if found else None


def group_members(group: str) -> tuple[str, ...]:
    return GROUPS.get(group, ())


def are_correlated(a: str, b: str) -> bool:
    """True when two tickers are the same bet in different clothes."""
    return bool(set(groups_for(a)) & set(groups_for(b)))
