"""Read a Finviz screener as a universe source.

Finviz screens the *business*: valuation, growth, and trend. This scanner
screens the *option*: implied volatility, spread, assignment odds. Neither
answers the other's question, and used alone each has a blind spot — a Finviz
screen cannot tell you the premium is too thin to bother with, and an
option-only screen will happily suggest selling puts on something you would
never want to own.

So Finviz supplies the candidate list and the fundamentals; everything
downstream stays as it was.

One screener page per request, with a delay between pages. This is meant for
a handful of page loads when you rebuild the universe, not a per-symbol API:
Finviz rate-limits aggressively and their terms reserve bulk access for Elite
subscribers, who get a proper CSV export endpoint.
"""

from __future__ import annotations

import gzip
import re
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from html.parser import HTMLParser

from .netio import FetchError, ssl_context

FINVIZ_BASE = "https://finviz.com/screener.ashx"
ROWS_PER_PAGE = 20
PAGE_DELAY_SECONDS = 1.5

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip",
}

# The valuation view carries P/E and PEG, which is what the ownership gate
# needs. v=111 (overview) has P/E but no PEG.
VIEW_VALUATION = 121
VIEW_PERFORMANCE = 141

_TICKER_ATTR = re.compile(r'data-boxover-ticker="([A-Z][A-Z.\-]{0,6})"')
_TOTAL = re.compile(r"([\d,]+)\s*Total")


@dataclass
class ScreenRow:
    ticker: str
    values: dict[str, str] = field(default_factory=dict)

    def number(self, column: str) -> float | None:
        """Parse a Finviz cell into a float, or None for '-' and '%' junk."""
        raw = (self.values.get(column) or "").strip()
        if not raw or raw in {"-", "N/A"}:
            return None
        raw = raw.replace(",", "").replace("%", "")
        multiplier = 1.0
        if raw and raw[-1] in "KMBT":
            multiplier = {"K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}[raw[-1]]
            raw = raw[:-1]
        try:
            return float(raw) * multiplier
        except ValueError:
            return None


class _ScreenerParser(HTMLParser):
    """Pull the header row and every ``tr.styled-row`` out of a screener page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.headers: list[str] = []
        self.rows: list[tuple[str, list[str]]] = []
        self._in_header = False
        self._in_row = False
        self._in_cell = False
        self._cell: list[str] = []
        self._row: list[str] = []
        self._row_ticker = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        classes = attributes.get("class") or ""
        if tag == "th" and "table-header" in classes:
            self._in_header = True
            self._cell = []
        elif tag == "tr" and "styled-row" in classes:
            self._in_row = True
            self._row = []
            self._row_ticker = ""
        elif tag == "td" and self._in_row:
            # The symbol comes from the cell's own attribute. The rendered
            # text is unusable: the logo's alt text runs into it, so the
            # ticker cell reads "AAIG" rather than "AIG".
            ticker = attributes.get("data-boxover-ticker")
            if ticker and not self._row_ticker:
                self._row_ticker = ticker.strip().upper()
            self._in_cell = True
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "th" and self._in_header:
            self.headers.append("".join(self._cell).strip())
            self._in_header = False
        elif tag == "td" and self._in_cell:
            self._row.append("".join(self._cell).strip())
            self._in_cell = False
        elif tag == "tr" and self._in_row:
            if len(self._row) > 3 and self._row_ticker:
                self.rows.append((self._row_ticker, self._row))
            self._in_row = False

    def handle_data(self, data: str) -> None:
        if self._in_cell or self._in_header:
            self._cell.append(data)


def _fetch_html(url: str, timeout: float = 30.0) -> str:
    request = urllib.request.Request(url, headers=_BROWSER_HEADERS)
    try:
        with urllib.request.urlopen(
            request, timeout=timeout, context=ssl_context()
        ) as response:
            raw = response.read()
            if response.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            return raw.decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise FetchError(
                "Finviz is rate-limiting this IP (HTTP 429). Wait a few minutes "
                "and rebuild the universe less often."
            ) from exc
        raise FetchError(f"Finviz returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise FetchError(f"Could not reach Finviz: {exc}") from exc


def build_url(filters: str, *, view: int = VIEW_VALUATION, offset: int = 1) -> str:
    url = f"{FINVIZ_BASE}?v={view}&f={filters}"
    return url if offset <= 1 else f"{url}&r={offset}"


def parse_filters(screener_url: str) -> str:
    """Pull the ``f=`` filter string out of a full screener URL."""
    match = re.search(r"[?&]f=([^&]+)", screener_url)
    if not match:
        raise ValueError(
            "That URL has no f= filter string. Copy the address bar after "
            "setting your filters on finviz.com/screener.ashx."
        )
    return match.group(1)


def fetch_screen(
    filters: str,
    *,
    view: int = VIEW_VALUATION,
    max_rows: int = 200,
    verbose: bool = True,
) -> list[ScreenRow]:
    """Page through a Finviz screen and return its rows.

    ``filters`` is the ``f=`` string from a screener URL, for example
    ``fa_pe_u30,fa_peg_u2,sh_price_o50,ta_perf_13wup,ta_perf2_4wdown``.
    """
    html = _fetch_html(build_url(filters, view=view))
    total_match = _TOTAL.search(html)
    total = int(total_match.group(1).replace(",", "")) if total_match else 0
    if verbose:
        print(f"  Finviz reports {total} matching stock(s)")
    if not total:
        return []

    wanted = min(total, max_rows)
    rows: list[ScreenRow] = []
    offset = 1
    headers: list[str] = []

    while len(rows) < wanted:
        if offset > 1:
            time.sleep(PAGE_DELAY_SECONDS)
            html = _fetch_html(build_url(filters, view=view, offset=offset))

        parser = _ScreenerParser()
        parser.feed(html)
        headers = headers or parser.headers

        if not parser.rows:
            break

        for ticker, cells in parser.rows:
            rows.append(
                ScreenRow(
                    ticker=ticker,
                    values=dict(zip(headers, cells)) if headers else {},
                )
            )

        if verbose:
            print(f"  fetched {len(rows)}/{wanted}")
        if len(parser.rows) < ROWS_PER_PAGE:
            break
        offset += ROWS_PER_PAGE

    return rows[:wanted]


def fetch_fundamentals(
    filters: str, *, max_rows: int = 200, verbose: bool = True
) -> dict[str, dict[str, float | None]]:
    """Map each screened ticker to the fundamentals the ownership gate uses."""
    rows = fetch_screen(
        filters, view=VIEW_VALUATION, max_rows=max_rows, verbose=verbose
    )
    result: dict[str, dict[str, float | None]] = {}
    for row in rows:
        result[row.ticker] = {
            "pe": row.number("P/E"),
            "forward_pe": row.number("Forward P/E"),
            "peg": row.number("PEG"),
            "price": row.number("Price"),
            "market_cap": row.number("Market Cap"),
            "volume": row.number("Volume"),
        }
    return result
