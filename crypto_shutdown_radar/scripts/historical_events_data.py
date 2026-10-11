"""Researched (via web search, not fabricated) real-world crypto structural
-negative events, used by scripts/source_coverage_validation.py (Phase E).

CATEGORIZATION (Task 6): events are split so the historical table cannot be
used to inflate the coverage verdict with cases the shutdown detector isn't
actually designed for:

    A. bankruptcy / insolvency
    B. explicit permanent shutdown / ceasing operations
    C. project/token abandonment or wind-down
    D. protocol/market collapse WITHOUT an explicit shutdown announcement

This project's shutdown classifier (app/classifier.py) is designed to
detect explicit announcements -- categories A+B+C. Category D events
(a depeg death-spiral, a DeFi bank run) are real and severe, but there was
no "we are shutting down" statement for the classifier to have matched;
counting them as a detector success would be misleading. PRIMARY_TARGET
on each entry is True only for A/B/C.

PRIMARY SOURCE HONESTY: every entry below was found via web search, which
returns secondary reporting (news outlets), not the original company
statement/court filing itself. None of the primary announcements were
independently opened and read in this project. `primary_source_verified`
is therefore False for every entry, and `primary_source_url` is None
unless a specific URL to the operator's own channel was itself confirmed
reachable (none were, in this pass). This is intentionally conservative
per Task 6: "If a primary source cannot be verified, label PRIMARY SOURCE
NOT VERIFIED. Do not present secondary articles as primary evidence."

Blast and BitMart were specifically re-checked per Task 6's instruction to
verify the actual announcement/source, not just token identity: in both
cases, repeated web searches surfaced only secondary coverage (Decrypt,
Cointelegraph, TheDefiant, CryptoBriefing, KuCoin News) that itself
describes or embeds the operator's statement -- no search result
resolved to blast.io or bitmart.com's own announcement page/post. Both
remain PRIMARY SOURCE NOT VERIFIED.

`shortable_at_announcement` is 'UNKNOWN' for every entry: exchange REST
APIs (used elsewhere in this project for live futures checks) only expose
CURRENT listings, not historical ones, so whether a perpetual contract
existed at the moment of a years-old announcement cannot be verified for
free via any API this project uses. Treat any commonly-repeated claim
otherwise as unverified.
"""

HISTORICAL_EVENTS = [
    {
        "project": "FTX",
        "token": "FTX Token",
        "ticker": "FTT",
        "coingecko_id": "ftx-token",
        "category": "A",
        "primary_target": True,
        "exact_event_claim": "FTX Trading Ltd., Alameda Research, and ~130 affiliated companies filed for Chapter 11 bankruptcy protection in the U.S. Bankruptcy Court for the District of Delaware.",
        "exact_event_timestamp": "2022-11-11",
        "primary_source_verified": False,
        "primary_source_url": None,
        "earliest_secondary_report": "2022-11-02 (CoinDesk published the leaked Alameda balance sheet, the trigger event)",
        "would_detect_via": "crypto news RSS (CoinDesk/Cointelegraph/TheBlock/Decrypt all broke this live); "
                            "exchange announcement feeds would NOT have caught this (FTX did not announce its own collapse)",
        "estimated_detection_delay": "Within our RSS poll cadence (10-30 min) of each news outlet's own publish time; "
                                      "outlets were themselves publishing within hours of the triggering leak, well "
                                      "before the Nov 11 filing itself",
        "shortable_at_announcement": "UNKNOWN",
        "sources": [
            "https://www.theblock.co/post/186132/ftx-collapse-timeline-six-days-that-rocked-the-crypto-industry",
        ],
        "notes": "FTT fell from ~$22 to below $18 around Nov 7, 2022; by March 2023 FTT was reported ~98% below its "
                 "Sept 2021 peak. Multiple sources give differing intra-week figures. The Chapter 11 petition itself "
                 "is public record (case docketed in Delaware) but its docket URL was not independently fetched here.",
    },
    {
        "project": "Terra (Terraform Labs)",
        "token": "TerraUSD / Luna",
        "ticker": "UST / LUNA",
        "coingecko_id": "terra-luna",
        "category": "D",
        "primary_target": False,
        "exact_event_claim": "UST lost its $1 peg and entered a death spiral with LUNA's hyperinflationary minting mechanism; "
                              "no company statement announcing a shutdown was made -- this was an uncontrolled protocol "
                              "collapse, not an explicit cessation announcement.",
        "exact_event_timestamp": "2022-05-09",
        "primary_source_verified": False,
        "primary_source_url": None,
        "earliest_secondary_report": "2022-05-07 (UST begins slipping from $1)",
        "would_detect_via": "crypto news RSS (extensively covered live by every major outlet)",
        "estimated_detection_delay": "Minutes to low hours; this was one of the most heavily and immediately covered "
                                      "events in crypto media history",
        "shortable_at_announcement": "UNKNOWN",
        "sources": [
            "https://www.coindesk.com/learn/the-fall-of-terra-a-timeline-of-the-meteoric-rise-and-crash-of-ust-and-luna",
            "https://www.businesstoday.in/crypto/story/terra-luna-crashes-over-85-heres-why-333127-2022-05-11",
        ],
        "notes": "CATEGORY D, not a detector target: there was no explicit shutdown/ceasing-operations statement to "
                 "classify -- the protocol simply collapsed. LUNA fell from ~$80 to a few cents by May 12, 2022. "
                 "coingecko_id may resolve to the post-fork 'terra-luna-classic' listing rather than live 2022 "
                 "data; verify which CoinGecko id actually returns pre-collapse history before trusting any price.",
    },
    {
        "project": "Celsius Network",
        "token": "Celsius",
        "ticker": "CEL",
        "coingecko_id": "celsius-degree-token",
        "category": "A",
        "primary_target": True,
        "exact_event_claim": "Celsius Network LLC and affiliated debtors filed for Chapter 11 bankruptcy protection in the "
                              "U.S. Bankruptcy Court for the Southern District of New York.",
        "exact_event_timestamp": "2022-07-13",
        "primary_source_verified": False,
        "primary_source_url": None,
        "earliest_secondary_report": "2022-06-13 (withdrawal freeze announced)",
        "would_detect_via": "crypto news RSS",
        "estimated_detection_delay": "Within RSS poll cadence of publish time; the freeze itself was covered live a month earlier",
        "shortable_at_announcement": "UNKNOWN",
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
        "category": "A",
        "primary_target": True,
        "exact_event_claim": "Voyager Digital and its main operating subsidiaries filed voluntary Chapter 11 petitions in "
                              "the U.S. Bankruptcy Court for the Southern District of New York.",
        "exact_event_timestamp": "2022-07-06",
        "primary_source_verified": False,
        "primary_source_url": None,
        "earliest_secondary_report": "2022-07-06 (same day, widely covered)",
        "would_detect_via": "crypto news RSS",
        "estimated_detection_delay": "Within RSS poll cadence of publish time (same-day coverage)",
        "shortable_at_announcement": "UNKNOWN",
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
        "category": "B",
        "primary_target": True,
        "exact_event_claim": "The Multichain team stated it was 'forced to cease operations' because it lacked "
                              "information sources and operating funds after CEO Zhaojun had been in custody since "
                              "May 21, 2023.",
        "exact_event_timestamp": "2023-07-14",
        "primary_source_verified": False,
        "primary_source_url": None,
        "earliest_secondary_report": "2023-05-24 (price/community reaction to CEO's disappearance)",
        "would_detect_via": "crypto news RSS; GitHub activity (repo went quiet) as a weak corroborating signal",
        "estimated_detection_delay": "The underlying distress was visible via secondary reporting ~7 weeks before "
                                      "the formal 'ceasing operations' statement -- a case where early distress "
                                      "signals (MULTI already down ~50% by late May) preceded the formal event by weeks",
        "shortable_at_announcement": "UNKNOWN",
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
        "category": "D",
        "primary_target": False,
        "exact_event_claim": "A DeFi 'bank run' on the IRON/TITAN pool caused TITAN to collapse to near zero within "
                              "hours; the team's post-mortem described the mechanism but this was a protocol failure "
                              "in progress, not a prior announced shutdown.",
        "exact_event_timestamp": "2021-06-17",
        "primary_source_verified": False,
        "primary_source_url": None,
        "earliest_secondary_report": "2021-06-16 (sell-off begins)",
        "would_detect_via": "crypto news RSS (widely covered given Mark Cuban's public involvement)",
        "estimated_detection_delay": "Hours; this was a fast, violent single-day collapse, not a slow leak",
        "shortable_at_announcement": "UNKNOWN",
        "sources": [
            "https://en.wikipedia.org/wiki/Iron_Finance",
            "https://stockhead.com.au/?p=130855",
        ],
        "notes": "CATEGORY D, not a detector target: no explicit pre-collapse shutdown statement existed to classify. "
                 "TITAN fell from ~$65 toward effectively $0 within hours on June 16-17, 2021. coingecko_id "
                 "'titan-hard' is a guess at the dead listing and may not resolve -- verify before trusting.",
    },
    {
        "project": "Hotbit",
        "token": None,
        "ticker": None,
        "coingecko_id": None,
        "category": "B",
        "primary_target": True,
        "exact_event_claim": "Hotbit announced it would stop all centralized exchange (CEX) operations from "
                              "2023-05-22 04:00 UTC and asked users to withdraw assets by 2023-06-21 04:00 UTC.",
        "exact_event_timestamp": "2023-05-22",
        "primary_source_verified": False,
        "primary_source_url": None,
        "earliest_secondary_report": "2023-05-22 (same day)",
        "would_detect_via": "crypto news RSS; would NOT be caught by a company-specific official RSS/blog feed "
                             "since Hotbit had no widely-tracked native token for this project's coin-centric monitoring",
        "estimated_detection_delay": "Within RSS poll cadence of publish time",
        "shortable_at_announcement": "N/A -- no major native token to check",
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
        "category": "B",
        "primary_target": True,
        "exact_event_claim": "The Blast team announced it would shut down the network, citing operating costs "
                              "(~$110/day chain revenue per DefiLlama) that no longer justified running it, with no "
                              "credible path to economic sustainability.",
        "exact_event_timestamp": "2026-10-02",
        "primary_source_verified": False,
        "primary_source_url": None,
        "earliest_secondary_report": "2026-10-02/03",
        "would_detect_via": "crypto news RSS (Decrypt, CryptoBriefing, KuCoin news all covered same-day)",
        "estimated_detection_delay": "Within RSS poll cadence of publish time",
        "shortable_at_announcement": "UNKNOWN",
        "sources": [
            "https://decrypt.co/379972/ethereum-layer-2-blast-shutting-down",
            "https://cryptobriefing.com/blast-layer-2-shuts-down-operations/",
            "https://www.kucoin.com/news/flash/blast-announces-network-shutdown-as-layer-2-costs-exceed-revenue",
        ],
        "notes": "RE-VERIFIED per Task 6 (not just token identity): repeated web search for the primary blast.io/"
                 "@blast announcement itself returned only secondary coverage that reproduces or embeds the "
                 "statement (e.g. Forklog's reproduction of the @blast post, dated Oct 2, 2026) -- no result "
                 "resolved directly to blast.io or the @blast account. Remains PRIMARY SOURCE NOT VERIFIED. "
                 "This was a voluntary wind-down citing unsustainable operating costs, NOT a bankruptcy filing. "
                 "BLAST token was reported ~98-99% below its June 2024 peak at the time of the announcement. "
                 "Standard-interface withdrawals were available through 2026-10-26.",
    },
    {
        "project": "BitMart",
        "token": "BitMart Token",
        "ticker": "BMX",
        "coingecko_id": "bitmart-token",
        "category": "B",
        "primary_target": True,
        "exact_event_claim": "BitMart announced it would wind down its exchange, halting trading on 2026-08-26 and "
                              "fully ceasing operations by 2027-01-31, citing an evaluation of its operating "
                              "conditions, market environment, and future strategic direction.",
        "exact_event_timestamp": "2026-07-26",
        "primary_source_verified": False,
        "primary_source_url": None,
        "earliest_secondary_report": "2026-07-26 (notice reported published 9:40pm ET)",
        "would_detect_via": "crypto news RSS (Decrypt, Cointelegraph, TheDefiant, TheBlock); exchange's own "
                             "announcement feed if BitMart's were among the configured announcement sources (it is "
                             "not currently -- only Binance's is configured)",
        "estimated_detection_delay": "Within RSS poll cadence of publish time",
        "shortable_at_announcement": "UNKNOWN",
        "sources": [
            "https://decrypt.co/374346/bitmart-becomes-latest-crypto-exchange-to-shut-down",
            "https://thedefiant.io/news/cefi/bitmart-to-wind-down-exchange-end-trading-by-aug-26",
            "https://www.theblock.co/post/409670/bitmart-to-wind-down-trading-platform-as-global-cex-says-he-was-not-consulted",
        ],
        "notes": "RE-VERIFIED per Task 6 (not just token identity): BMX is confirmed as BitMart's own native token "
                 "(ERC-20; launched as BMC in Dec 2017, renamed BMX Jan 2018 -- multiple independent sources agree). "
                 "The actual wind-down notice itself was not found on bitmart.com directly in this search pass -- "
                 "every result is secondary coverage citing the notice. Remains PRIMARY SOURCE NOT VERIFIED. "
                 "Global CEO Nenter (Nathan) Chow reportedly said he was not consulted and learned of the decision "
                 "when it became public -- a signal that even insiders were surprised, relevant to how early any "
                 "monitoring system could realistically have caught wind of this. BMX fell ~70-81% on the news "
                 "depending on the source/window.",
    },
    {
        "project": "Eliza Labs / ai16z",
        "token": "elizaOS",
        "ticker": "ai16z / ELIZAOS",
        "coingecko_id": "ai16z",
        "category": "C",
        "primary_target": True,
        "exact_event_claim": "Founder Shaw Walters declared the ELIZAOS/ai16z token 'dead' and announced the "
                              "foundation was winding down, after settling a class-action lawsuit by handing the "
                              "remaining treasury to a holder group; the open-source elizaOS software continues "
                              "without a token.",
        "exact_event_timestamp": "2026-08-04",
        "primary_source_verified": False,
        "primary_source_url": None,
        "earliest_secondary_report": "2026-08-04 to 2026-08-06",
        "would_detect_via": "crypto news RSS (Decrypt, BeInCrypto, CoinTelegraph, TheDefiant all covered it)",
        "estimated_detection_delay": "Within RSS poll cadence of publish time",
        "shortable_at_announcement": "UNKNOWN",
        "sources": [
            "https://decrypt.co/374958",
            "https://beincrypto.com/eliza-token-record-low-founder-dead/",
            "https://thedefiant.io/news/tokens/eliza-labs-shaw-walters-says-ai16z-token-is-dead-after-settling-burwick-law-class-action",
        ],
        "notes": "CATEGORY C (token/project abandonment + foundation wind-down), not a company bankruptcy -- the "
                 "underlying open-source software continues. Token hit a record low ~2 days after the announcement "
                 "(~$0.000277-0.000354 depending on which related token/outlet is cited), down ~99.99% from a peak "
                 "commonly cited around $2.4-2.5B valuation in January 2025. No formal single 'shutdown date' -- "
                 "Aug 4, 2026 is when the founder declared the token dead and wind-down began.",
    },
]

PRIMARY_TARGET_EVENTS = [e for e in HISTORICAL_EVENTS if e["primary_target"]]
CONTEXT_ONLY_EVENTS = [e for e in HISTORICAL_EVENTS if not e["primary_target"]]
