"""Researched (via web search, not fabricated) real-world crypto structural
-negative events, used by scripts/source_coverage_validation.py (Phase E).

Each entry's `coingecko_id` is a best-effort identifier for the CoinGecko
free /coins/{id}/history endpoint. CoinGecko often keeps pages for delisted/
dead coins, but this is NOT guaranteed -- the validation harness marks a
coin UNKNOWN rather than fabricating a price if the lookup fails.

IMPORTANT ON FUTURES AVAILABILITY: exchange REST APIs only expose CURRENT
listings, not historical ones. Whether a perpetual contract existed AT THE
TIME of a years-old announcement cannot be verified for free via any REST
API used elsewhere in this project. Every event below is correctly marked
UNKNOWN for that field; do not treat commonly-repeated claims (e.g. "FTT
had perps on major exchanges") as verified just because they're widely
repeated -- they were not independently re-checked against a historical
record in this project.

Citations are the actual URLs returned by web search during development.
"""

HISTORICAL_EVENTS = [
    {
        "project": "FTX",
        "token": "FTX Token",
        "ticker": "FTT",
        "coingecko_id": "ftx-token",
        "event_type": "Bankruptcy (Chapter 11)",
        "announcement_date": "2022-11-11",
        "primary_source": "FTX Chapter 11 filing, U.S. Bankruptcy Court, District of Delaware (Nov 11, 2022); "
                           "preceded by CZ/Binance public statements Nov 6-8, 2022",
        "earliest_secondary_report": "~2022-11-02 (CoinDesk published the leaked Alameda balance sheet) / "
                                      "2022-11-06 (CZ announced Binance would liquidate its FTT holdings)",
        "would_detect_via": "crypto news RSS (CoinDesk/Cointelegraph/TheBlock/Decrypt -- all broke this live); "
                            "exchange announcement feeds would NOT have caught this (FTX did not announce its own collapse)",
        "estimated_detection_delay": "Within our RSS poll cadence (10-30 min) of each news outlet's own publish time; "
                                      "outlets were themselves publishing within hours of the triggering leak, well "
                                      "before the Nov 11 filing itself",
        "futures_available_at_announcement": "UNKNOWN (not independently re-verifiable via a free historical-listings API)",
        "sources": [
            "https://www.theblock.co/post/186132/ftx-collapse-timeline-six-days-that-rocked-the-crypto-industry",
        ],
        "notes": "FTT fell from ~$22 to below $18 around Nov 7, 2022; by March 2023 FTT was reported ~98% below its "
                 "Sept 2021 peak. Multiple sources give differing intra-week figures.",
    },
    {
        "project": "Terra (Terraform Labs)",
        "token": "TerraUSD / Luna",
        "ticker": "UST / LUNA",
        "coingecko_id": "terra-luna",
        "event_type": "Stablecoin depeg + protocol collapse (not a formal bankruptcy filing at the time)",
        "announcement_date": "2022-05-09",
        "primary_source": "Terraform Labs / Do Kwon public statements during the depeg (no single primary announcement URL "
                           "confirmed independently in this project)",
        "earliest_secondary_report": "2022-05-07 (UST begins slipping from $1)",
        "would_detect_via": "crypto news RSS (extensively covered live by every major outlet)",
        "estimated_detection_delay": "Minutes to low hours; this was one of the most heavily and immediately covered "
                                      "events in crypto media history",
        "futures_available_at_announcement": "UNKNOWN (not independently re-verifiable)",
        "sources": [
            "https://www.coindesk.com/learn/the-fall-of-terra-a-timeline-of-the-meteoric-rise-and-crash-of-ust-and-luna",
            "https://www.businesstoday.in/crypto/story/terra-luna-crashes-over-85-heres-why-333127-2022-05-11",
        ],
        "notes": "LUNA fell from ~$80 to a few cents by May 12, 2022. UST fell from $1 to ~$0.25-0.35 within ~48-72h. "
                 "coingecko_id may resolve to the post-fork 'terra-luna-classic' listing rather than live 2022 data; "
                 "verify which CoinGecko id actually returns pre-collapse history before trusting the price table.",
    },
    {
        "project": "Celsius Network",
        "token": "Celsius",
        "ticker": "CEL",
        "coingecko_id": "celsius-degree-token",
        "event_type": "Bankruptcy (Chapter 11), preceded by a withdrawal freeze",
        "announcement_date": "2022-07-13",
        "primary_source": "Celsius Chapter 11 filing, U.S. Bankruptcy Court, Southern District of New York (Jul 13, 2022)",
        "earliest_secondary_report": "2022-06-13 (withdrawal freeze announced)",
        "would_detect_via": "crypto news RSS",
        "estimated_detection_delay": "Within RSS poll cadence of publish time; the freeze itself was covered live a month earlier",
        "futures_available_at_announcement": "UNKNOWN (not independently re-verifiable)",
        "sources": [
            "https://www.business-standard.com/amp/article/markets/crypto-lender-celsius-files-for-bankruptcy-in-new-york-after-sharp-sell-off-122071400765_1.html",
            "https://thedefiant.io/celsius-cel-up-since-crisis/",
        ],
        "notes": "Reports differ: investing.com cites a drop from $0.9549 to $0.4239; FXEmpire cites a 58% drop in hours. "
                 "CEL later rose ~30% in the weeks after filing (short-lived), illustrating that post-announcement "
                 "price action is not always a clean, sustained decline.",
    },
    {
        "project": "Voyager Digital",
        "token": "Voyager Token",
        "ticker": "VGX",
        "coingecko_id": "voyager-token",
        "event_type": "Bankruptcy (Chapter 11)",
        "announcement_date": "2022-07-06",
        "primary_source": "Voyager Digital Chapter 11 filing, U.S. Bankruptcy Court, Southern District of New York (Jul 6, 2022)",
        "earliest_secondary_report": "2022-07-06 (same day, widely covered)",
        "would_detect_via": "crypto news RSS",
        "estimated_detection_delay": "Within RSS poll cadence of publish time (same-day coverage)",
        "futures_available_at_announcement": "UNKNOWN (not independently re-verifiable)",
        "sources": [
            "https://www.theblock.co/post/261444/voyager-token-surges-after-nearly-one-third-of-supply-is-burned",
            "https://ambcrypto.com/why-vgx-surged-by-over-100-despite-voyagers-claims-of-bankruptcy/",
        ],
        "notes": "IMPORTANT COUNTEREXAMPLE: VGX did not sustainedly decline after the bankruptcy filing -- it surged "
                 "over 100% by July 13, 2022 on an unrelated social-media pump campaign, then fell back. This event "
                 "shows a bankruptcy filing does not guarantee a clean, monitorable sustained-decline trade even "
                 "with perfect, instant detection.",
    },
    {
        "project": "Multichain",
        "token": "Multichain",
        "ticker": "MULTI",
        "coingecko_id": "multichain",
        "event_type": "Ceasing operations (CEO detained by Chinese authorities; team lost server/fund access)",
        "announcement_date": "2023-07-14",
        "primary_source": "Multichain team statement (project's own Twitter/Medium at the time; not independently re-fetched)",
        "earliest_secondary_report": "2023-05-24 (price/community reaction to CEO's disappearance)",
        "would_detect_via": "crypto news RSS; GitHub activity (repo went quiet) as a weak corroborating signal",
        "estimated_detection_delay": "The underlying distress was visible via secondary reporting ~7 weeks before "
                                      "the formal 'ceasing operations' statement -- a case where early distress "
                                      "signals (MULTI already down ~50% by late May) preceded the formal event by weeks",
        "futures_available_at_announcement": "UNKNOWN (not independently re-verifiable)",
        "sources": [
            "https://blockworks.com/news/multichain-ceo-missing-trust-reserve",
            "https://coinmarketcap.com/academy/de/article/multichain-shuts-down-operations-after-ceo-detained",
        ],
        "notes": "Good example of a slow-motion collapse: CEO unreachable from ~May 21, 2023; formal cessation "
                 "statement ~July 14, 2023. A monitoring system would have had weeks of lead time from secondary "
                 "reporting alone, well before the formal announcement.",
    },
    {
        "project": "Iron Finance",
        "token": "Titan",
        "ticker": "TITAN",
        "coingecko_id": "titan-hard",
        "event_type": "Protocol collapse via bank run (not a company bankruptcy -- a DeFi protocol failure)",
        "announcement_date": "2021-06-17",
        "primary_source": "Iron Finance team post-mortem (project's own Medium/blog at the time; not independently re-fetched)",
        "earliest_secondary_report": "2021-06-16 (sell-off begins)",
        "would_detect_via": "crypto news RSS (widely covered given Mark Cuban's public involvement)",
        "estimated_detection_delay": "Hours; this was a fast, violent single-day collapse, not a slow leak",
        "futures_available_at_announcement": "UNKNOWN (not independently re-verifiable)",
        "sources": [
            "https://en.wikipedia.org/wiki/Iron_Finance",
            "https://stockhead.com.au/?p=130855",
        ],
        "notes": "TITAN fell from ~$65 toward effectively $0 within hours on June 16-17, 2021. coingecko_id "
                 "'titan-hard' is a guess at the dead listing and may not resolve -- verify before trusting.",
    },
    {
        "project": "Hotbit",
        "token": None,
        "ticker": None,
        "coingecko_id": None,
        "event_type": "Permanent exchange closure (voluntary wind-down)",
        "announcement_date": "2023-05-22",
        "primary_source": "Hotbit's own announcement (exchange's official channel at the time; not independently re-fetched)",
        "earliest_secondary_report": "2023-05-22 (same day)",
        "would_detect_via": "crypto news RSS; would NOT be caught by a company-specific official RSS/blog feed "
                             "since Hotbit had no widely-tracked native token for this project's coin-centric monitoring",
        "estimated_detection_delay": "Within RSS poll cadence of publish time",
        "futures_available_at_announcement": "N/A -- no major native token to check",
        "sources": [
            "https://blockworks.co/news/hotbit-crypto-exchange-shuts-down",
            "https://forklog.com/en/hotbit-exchange-to-shut-down-after-five-years/",
        ],
        "notes": "Illustrates a real blind spot: this project's universe is coin-centric (top ~1000 by market cap). "
                 "An exchange closure with no major associated token (Hotbit) falls outside what the coin-mapping "
                 "approach watches at all, and would only be caught via the generic crypto-news RSS feeds, never "
                 "via a per-coin official-source mapping.",
    },
    {
        "project": "Blast (L2 network)",
        "token": "Blast",
        "ticker": "BLAST",
        "coingecko_id": "blast",
        "event_type": "Permanent protocol/network shutdown (voluntary wind-down, not bankruptcy)",
        "announcement_date": "2026-10-03",
        "primary_source": "Blast team announcement (project's own channel; see secondary coverage below)",
        "earliest_secondary_report": "2026-10-03",
        "would_detect_via": "crypto news RSS (Decrypt, CryptoBriefing, KuCoin news all covered same-day)",
        "estimated_detection_delay": "Within RSS poll cadence of publish time",
        "futures_available_at_announcement": "UNKNOWN (not independently re-verifiable)",
        "sources": [
            "https://decrypt.co/379972/ethereum-layer-2-blast-shutting-down",
            "https://cryptobriefing.com/blast-layer-2-shuts-down-operations/",
        ],
        "notes": "Re-confirms the finding from the prior validation round: this was a voluntary wind-down citing "
                 "unsustainable operating costs (~$110/day chain revenue), NOT a bankruptcy filing. BLAST token was "
                 "reported ~98-99% below its June 2024 peak at the time of the announcement.",
    },
    {
        "project": "BitMart",
        "token": "BitMart Token",
        "ticker": "BMX",
        "coingecko_id": "bitmart-token",
        "event_type": "Permanent exchange closure (voluntary wind-down)",
        "announcement_date": "2026-07-27",
        "primary_source": "BitMart's own announcement (exchange's official channel; see secondary coverage below)",
        "earliest_secondary_report": "2026-07-27",
        "would_detect_via": "crypto news RSS (Decrypt, Cointelegraph, TheDefiant); exchange's own announcement "
                             "feed if BitMart's is among the configured announcement sources (it is not, currently "
                             "-- only Binance's is configured)",
        "estimated_detection_delay": "Within RSS poll cadence of publish time",
        "futures_available_at_announcement": "UNKNOWN (not independently re-verifiable)",
        "sources": [
            "https://decrypt.co/374346/bitmart-becomes-latest-crypto-exchange-to-shut-down",
            "https://whale-alert.io/stories/d4d201aabc112d/BitMart-says-it-will-wind-down-trading-by-Aug-26-and-fully-cease-operations-in-2027-sending-BMX-down-nearly-70",
        ],
        "notes": "CONFIRMED: BMX is BitMart's own native token (BitMart Token, ERC-20; originally launched as BMC "
                 "in Dec 2017, renamed BMX in Jan 2018 -- multiple independent sources agree). This re-validates "
                 "the prior round's finding: voluntary wind-down (trading halt Aug 26, 2026; full cessation by "
                 "Jan 31, 2027), NOT a bankruptcy filing. BMX fell ~70-81% on the news depending on the source/window.",
    },
    {
        "project": "Eliza Labs / ai16z",
        "token": "elizaOS",
        "ticker": "ai16z / ELIZAOS",
        "coingecko_id": "ai16z",
        "event_type": "Project/token abandonment (foundation wind-down after a lawsuit settlement; the open-source "
                       "software continues without a token)",
        "announcement_date": "2026-08-04",
        "primary_source": "Founder Shaw Walters's public statement (project's own channel; see secondary coverage below)",
        "earliest_secondary_report": "2026-08-04 to 2026-08-06",
        "would_detect_via": "crypto news RSS (Decrypt, BeInCrypto, CoinTelegraph, TheDefiant all covered it)",
        "estimated_detection_delay": "Within RSS poll cadence of publish time",
        "futures_available_at_announcement": "UNKNOWN (not independently re-verifiable)",
        "sources": [
            "https://decrypt.co/374958",
            "https://beincrypto.com/eliza-token-record-low-founder-dead/",
            "https://thedefiant.io/news/tokens/eliza-labs-shaw-walters-says-ai16z-token-is-dead-after-settling-burwick-law-class-action",
        ],
        "notes": "Token hit a record low ~2 days after the announcement (~$0.000277-0.000354 depending on which of "
                 "the two related tokens/outlet is cited), down ~99.99% from a peak commonly cited around "
                 "$2.4-2.5B valuation in January 2025. No formal single 'shutdown date' -- Aug 4, 2026 is when "
                 "the founder declared the token dead and wind-down began.",
    },
]
