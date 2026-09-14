"""Products this account is not permitted to trade.

The broker refuses orders in funds that hold crypto: spot trusts such as ETHA
and IBIT, futures funds such as BITO, and the covered-call and overlay funds
built on top of them. The scanner recommended them anyway - a scan on
2026-09-14 ranked IBIT fifth and ETHA sixth, so two of the top ten were trades
that could not be placed.

What counts is the holding, not the theme. Coinbase, Strategy and the bitcoin
miners are operating companies, and blockchain-industry ETFs hold their
shares; those trade like any other equity and stay in. The test is the fund's
registered name, which states what it holds.

Two layers, because the name is not always to hand:

* the name rule, applied wherever the asset list is available, which catches
  funds launched after this file was written;
* a ticker snapshot, used when it is not, so ``--symbols``, a Finviz screen or
  a universe file built before this rule existed are still filtered.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

# Something in the name says the fund holds a coin, a coin future or a basket
# of them.
CRYPTO_HOLDING = re.compile(
    r"\b(?:bitcoin|ether|ethereum|solana|xrp|litecoin|dogecoin|crypto)\b",
    re.IGNORECASE,
)

# Names that mention crypto but describe an equity holding: the companies
# themselves, and funds of mining, exchange and infrastructure stocks.
# "Blockchain" is deliberately absent - Global X's BITS pairs blockchain
# stocks with bitcoin futures, and the futures are the restricted part.
EQUITY_EXPOSURE = re.compile(
    r"\b(?:common\s+stock|depositary|miners?|mining|industry|economy|"
    r"ecosystem|infrastructure|thematic|crypto\s+equity)\b",
    re.IGNORECASE,
)

# Every optionable fund the name rule matched on 2026-09-14. Used only when
# the live asset list cannot be fetched; the rule is the source of truth.
KNOWN_CRYPTO_FUNDS = frozenset({
    "AETH", "ARKB", "BAGY", "BBB", "BCCC", "BITB", "BITI", "BITO", "BITS",
    "BITU", "BITX", "BITY", "BLOX", "BSOL", "BTC", "BTCI", "BTCL", "BTCO",
    "BTCW", "BTCZ", "BTF", "BTGD", "BTRN", "EETH", "ETH", "ETHA", "ETHB",
    "ETHD", "ETHE", "ETHT", "ETHU", "ETHV", "ETHW", "ETU", "EZBC", "EZET",
    "FBTC", "FETH", "FSOL", "GBTC", "GDOG", "GSOL", "GXRP", "HODL", "IBIT",
    "LTCC", "MAXI", "MSBT", "NCIQ", "QETH", "SBIT", "SETH", "SLON", "SOEZ",
    "SOLC", "SOLT", "SOLZ", "SPBC", "TETH", "TXXD", "UXRP", "VSOL", "XBTY",
    "XRP", "XRPC", "XRPI", "XRPR", "XRPT", "XRPZ", "XXRP", "YBIT", "YBTC",
    "YETH",
})

NOT_PERMITTED = "crypto fund (no trading permission)"


def is_crypto_fund(name: str) -> bool:
    """True when a registered asset name describes a fund holding crypto."""
    if not name:
        return False
    return bool(CRYPTO_HOLDING.search(name)) and not EQUITY_EXPOSURE.search(name)


def crypto_fund_symbols(assets: Iterable[dict] | None = None) -> frozenset[str]:
    """The snapshot, plus anything in ``assets`` whose name matches the rule."""
    found = set(KNOWN_CRYPTO_FUNDS)
    for asset in assets or ():
        if is_crypto_fund(str(asset.get("name", ""))):
            found.add(str(asset.get("symbol", "")).upper())
    return frozenset(found)


def load_crypto_fund_symbols(provider) -> tuple[frozenset[str], bool]:
    """Restricted tickers, and whether the live name scan actually ran.

    Falls back to the snapshot rather than failing: a scan that cannot reach
    the asset list should still keep ETHA out of its results.
    """
    if provider is None:
        return KNOWN_CRYPTO_FUNDS, False
    try:
        # Imported here: universe imports this module for its prefilter.
        from .universe import fetch_optionable_assets

        assets = fetch_optionable_assets(provider)
    except Exception:
        return KNOWN_CRYPTO_FUNDS, False
    return crypto_fund_symbols(assets), True


def split_restricted(
    symbols: Iterable[str], restricted: frozenset[str]
) -> tuple[list[str], list[str]]:
    """Return (tradable, excluded), preserving the order of the input."""
    kept, dropped = [], set()
    for symbol in symbols:
        if symbol.upper() in restricted:
            dropped.add(symbol.upper())
        else:
            kept.append(symbol)
    return kept, sorted(dropped)
