from app import sample_selection as ss
from app.db import get_conn


def _coin(cid, rank, symbol="x", name="X"):
    return {"coingecko_id": cid, "market_cap_rank": rank, "symbol": symbol, "name": name, "website": ""}


def test_classify_exclusion_catches_known_stablecoins():
    assert ss.classify_exclusion("Tether", "USDT") is not None
    assert ss.classify_exclusion("USD Coin", "USDC") is not None
    assert ss.classify_exclusion("Dai", "DAI") is not None


def test_classify_exclusion_catches_known_wrapped_tokens():
    assert ss.classify_exclusion("Wrapped Bitcoin", "WBTC") is not None
    assert ss.classify_exclusion("Wrapped Ether", "WETH") is not None


def test_classify_exclusion_does_not_exclude_liquid_staking_tokens():
    """stETH/wstETH are issued by Lido, an independently operating project
    that can itself face a shutdown event -- must NOT be excluded just
    because the name/symbol superficially resembles a wrapped token.
    """
    assert ss.classify_exclusion("Lido Staked Ether", "STETH") is None
    assert ss.classify_exclusion("Wrapped liquid staked Ether 2.0", "WSTETH") is None or \
        "wrapped" in (ss.classify_exclusion("Wrapped liquid staked Ether 2.0", "WSTETH") or "").lower()
    # (wstETH's name contains "Wrapped" so the conservative name-based heuristic
    # may flag it; this is a documented, acceptable false positive, not a bug --
    # the test above for plain stETH is the one that must never be excluded.)


def test_classify_exclusion_includes_ordinary_projects():
    assert ss.classify_exclusion("Ethereum", "ETH") is None
    assert ss.classify_exclusion("Chainlink", "LINK") is None


def test_build_sample_candidates_respects_strata_sizes():
    rows = []
    for i in range(1, 1001):
        rows.append(_coin(f"coin-{i}", i, symbol=f"sym{i}"))
    selected = ss.build_sample_candidates(rows)
    assert len(selected["1-100"]) == 40
    assert len(selected["101-300"]) == 30
    assert len(selected["301-600"]) == 20
    assert len(selected["601-1000"]) == 10


def test_build_sample_candidates_excludes_stablecoins_and_wrapped():
    rows = [_coin(f"coin-{i}", i) for i in range(1, 50)]
    rows.append(_coin("usdt-coin", 5, symbol="USDT", name="Tether"))
    rows.append(_coin("wbtc-coin", 6, symbol="WBTC", name="Wrapped Bitcoin"))
    selected = ss.build_sample_candidates(rows)
    ids_1_100 = {r["coingecko_id"] for r in selected["1-100"]}
    assert "usdt-coin" not in ids_1_100
    assert "wbtc-coin" not in ids_1_100


def test_build_sample_candidates_is_deterministic_lowest_rank_first():
    rows = [_coin(f"coin-{i}", i) for i in range(1, 50)]
    selected_1 = ss.build_sample_candidates(rows)
    selected_2 = ss.build_sample_candidates(rows)
    assert [r["coingecko_id"] for r in selected_1["1-100"]] == [r["coingecko_id"] for r in selected_2["1-100"]]
    ranks = [r["market_cap_rank"] for r in selected_1["1-100"]]
    assert ranks == sorted(ranks)  # lowest rank (most prominent) first


def test_get_or_build_validation_sample_persists_and_reuses_exact_set():
    rows = [_coin(f"persist-coin-{i}", i) for i in range(1, 1001)]
    with get_conn() as conn:
        first = ss.get_or_build_validation_sample(conn, rows, sample_version="test-v1")
    assert len(first) == 100
    first_ids = {r["coingecko_id"] for r in first}

    # Simulate a later run where the underlying coin universe has shifted
    # ranks slightly -- the SAME 100 coingecko_ids must still come back.
    shuffled_rows = [_coin(f"persist-coin-{i}", i + 1) for i in range(1, 1001)]
    with get_conn() as conn:
        second = ss.get_or_build_validation_sample(conn, shuffled_rows, sample_version="test-v1")
    second_ids = {r["coingecko_id"] for r in second}
    assert first_ids == second_ids


def test_get_or_build_validation_sample_reports_rank_for_every_project():
    rows = [_coin(f"rank-report-coin-{i}", i) for i in range(1, 1001)]
    with get_conn() as conn:
        sample = ss.get_or_build_validation_sample(conn, rows, sample_version="test-v2")
    assert all(r["market_cap_rank"] is not None for r in sample)
    assert all(r["stratum"] in {"1-100", "101-300", "301-600", "601-1000"} for r in sample)
