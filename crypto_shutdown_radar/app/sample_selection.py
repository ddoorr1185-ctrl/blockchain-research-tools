"""Builds and persists the deterministic 100-project validation sample
used by scripts/source_coverage_validation.py.

Strata (by CoinGecko market-cap rank at selection time):
    40 from rank  1-100
    30 from rank  101-300
    20 from rank  301-600
    10 from rank  601-1000

Excludes obvious stablecoins and wrapped/bridged representations of
another chain's native asset that do not correspond to an independently
operating project/company (see EXCLUDED_SYMBOLS below). This is a
documented heuristic, not a perfect classifier -- it intentionally does
NOT exclude liquid-staking derivatives like stETH, since Lido is itself
an independently operating project.

The sample is persisted to the `validation_sample` table under a fixed
`SAMPLE_VERSION` the first time it's built, and every subsequent call
reuses exactly those coingecko_ids (re-reading their current rank/website
from `coins`, which can drift without changing *which* coins are in the
sample) -- so re-running the validation always measures the same 100
projects, per Task 4's "store the exact sample so reruns use the same 100".
"""
from app.db import now_iso

SAMPLE_VERSION = "v1"

STRATA = [
    ("1-100", 1, 100, 40),
    ("101-300", 101, 300, 30),
    ("301-600", 301, 600, 20),
    ("601-1000", 601, 1000, 10),
]

# Well-known stablecoins (symbol, lowercase). Excluded because they are not
# "a project/company that could structurally shut down" in the sense this
# project detects -- their risk profile (depeg) is a different phenomenon.
STABLECOIN_SYMBOLS = {
    "usdt", "usdc", "dai", "busd", "tusd", "usdp", "gusd", "usdd", "fdusd",
    "pyusd", "usde", "frax", "lusd", "susd", "ustc", "eurs", "eurt", "usds",
    "usdy", "usdx", "crvusd", "usd1", "fdusd", "usdc.e", "usdt.e",
}

# Bridged/wrapped representations of ANOTHER chain's native asset, with no
# independently operating company behind the wrapper itself (the underlying
# asset's own project is a different entry and is not excluded). Liquid
# staking tokens (stETH, wstETH, rETH, cbETH, ...) are deliberately NOT
# included here -- those ARE issued by an independently operating project
# (Lido, Rocket Pool, Coinbase, ...) that can itself face a shutdown event.
WRAPPED_SYMBOLS = {
    "wbtc", "weth", "wbnb", "wmatic", "wavax", "wftm", "wone", "wcro",
    "renbtc", "hbtc", "btcb", "wsteth_bridged",
}


def classify_exclusion(name: str, symbol: str) -> "str | None":
    """Returns a short reason string if this coin should be excluded from
    the validation sample, or None if it should be included. Pure
    function, no network/DB access, directly testable.
    """
    sym = (symbol or "").strip().lower()
    nm = (name or "").strip().lower()
    if sym in STABLECOIN_SYMBOLS:
        return f"stablecoin (symbol={sym})"
    if sym in WRAPPED_SYMBOLS:
        return f"wrapped/bridged representation, no independent operator (symbol={sym})"
    if "wrapped" in nm:
        return f"name contains 'wrapped' (name={name})"
    if nm.endswith(" usd") or nm == "usd coin" or "stablecoin" in nm:
        return f"name indicates stablecoin (name={name})"
    return None


def build_sample_candidates(coin_rows: list) -> dict:
    """Pure function: given coin rows (each with at least coingecko_id,
    name, symbol, market_cap_rank) sorted by rank ascending, returns the
    stratified selection as {stratum_label: [rows]} using the exclusion
    rules above, deterministically (lowest rank first within each
    stratum's rank window, never chosen because a source happened to be
    easy to find).
    """
    by_rank = sorted(coin_rows, key=lambda r: r["market_cap_rank"] or 10**9)

    selected = {}
    for label, lo, hi, n in STRATA:
        candidates = [
            r for r in by_rank
            if lo <= (r["market_cap_rank"] or -1) <= hi
            and classify_exclusion(r.get("name", ""), r.get("symbol", "")) is None
        ]
        selected[label] = candidates[:n]
    return selected


def get_or_build_validation_sample(conn, all_coin_rows: list, sample_version: str = SAMPLE_VERSION) -> list:
    """Returns the persisted sample if one already exists for
    `sample_version`; otherwise builds it from `all_coin_rows` (expected:
    every active coin, i.e. SELECT * FROM coins WHERE is_active=1 ORDER BY
    market_cap_rank), persists it, and returns it.

    Return shape: list of {coingecko_id, rank_at_selection, stratum,
    symbol, name, website} -- symbol/name/website are read fresh from
    `coins` (not persisted in validation_sample) so a display always shows
    current data for a coin that's been in the sample all along.
    """
    existing = conn.execute(
        "SELECT coingecko_id, rank_at_selection, stratum FROM validation_sample WHERE sample_version=?",
        (sample_version,),
    ).fetchall()

    if existing:
        coin_ids = [r["coingecko_id"] for r in existing]
        stratum_by_id = {r["coingecko_id"]: r["stratum"] for r in existing}
        rank_by_id = {r["coingecko_id"]: r["rank_at_selection"] for r in existing}
        rows_by_id = {r["coingecko_id"]: r for r in all_coin_rows}
        out = []
        for cid in coin_ids:
            current = rows_by_id.get(cid, {})
            out.append({
                "coingecko_id": cid,
                "rank_at_selection": rank_by_id[cid],
                "stratum": stratum_by_id[cid],
                "symbol": current.get("symbol", ""),
                "name": current.get("name", ""),
                "website": current.get("website", ""),
                "market_cap_rank": current.get("market_cap_rank", rank_by_id[cid]),
            })
        return out

    candidates_by_stratum = build_sample_candidates(all_coin_rows)
    out = []
    ts = now_iso()
    for stratum, rows in candidates_by_stratum.items():
        for row in rows:
            conn.execute(
                "INSERT INTO validation_sample (sample_version, coingecko_id, rank_at_selection, stratum, selected_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (sample_version, row["coingecko_id"], row["market_cap_rank"], stratum, ts),
            )
            out.append({
                "coingecko_id": row["coingecko_id"],
                "rank_at_selection": row["market_cap_rank"],
                "stratum": stratum,
                "symbol": row.get("symbol", ""),
                "name": row.get("name", ""),
                "website": row.get("website", ""),
                "market_cap_rank": row["market_cap_rank"],
            })
    return out
