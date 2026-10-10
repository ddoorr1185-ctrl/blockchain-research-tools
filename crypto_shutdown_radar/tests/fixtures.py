"""SYNTHETIC TEST DATA.

Every string below is fabricated for testing the classifier and pipeline.
None of it describes a real announcement by a real company unless
explicitly marked HISTORICAL_REAL_EVENT (see test_historical_events.py),
in which case it is a paraphrase used only to check classification shape,
not a verbatim quote.
"""

SYNTHETIC_BANKRUPTCY = [
    "We regret to inform our users that CoinVault Exchange has filed for Chapter 11 bankruptcy "
    "protection effective today. All withdrawals are suspended pending court proceedings.",
    "After months of financial distress, LendCrypto Inc. has declared bankruptcy and entered "
    "liquidation. Customer funds may not be fully recoverable.",
]

SYNTHETIC_PERMANENT_SHUTDOWN = [
    "After careful consideration, the TokenFi team has decided to permanently shut down the "
    "project effective March 1st. This is not a temporary pause.",
    "We are ceasing all operations of the Nimbus Protocol Foundation. This decision is final "
    "and the project will not be relaunched.",
    "It's time to say goodbye: we are sunsetting the project entirely and winding down the "
    "business over the next 30 days.",
]

SYNTHETIC_EXCHANGE_CLOSURE = [
    "ZenTrade exchange will permanently close to all users on June 30. We will delist all "
    "markets and cease all trading services ahead of the closure.",
]

SYNTHETIC_PROTOCOL_TERMINATION = [
    "The core team has voted to terminate the protocol. The chain will no longer produce "
    "blocks after block height 9,000,000.",
]

SYNTHETIC_PRODUCT_DISCONTINUATION = [
    "Our staking rewards product is being discontinued next month, but the core lending "
    "platform and the company continue to operate normally.",
]

SYNTHETIC_TEMPORARY_OUTAGE = [
    "Our API is temporarily suspended due to scheduled maintenance and will resume shortly. "
    "We apologize for the brief downtime.",
    "The network experienced a brief outage this morning; all services are back online and "
    "operating normally.",
]

SYNTHETIC_NEGATION = [
    "Despite recent rumors, we are NOT shutting down. The company remains fully operational "
    "and business as usual continues across all products.",
    "We want to directly address speculation: reports of our bankruptcy are false and unfounded. "
    "We continue normal operations.",
]

SYNTHETIC_RUMOR = [
    "Some users believe the project may close soon, though nothing has been confirmed by the team.",
    "Unconfirmed reports suggest the exchange could be forced to shut down, according to anonymous sources.",
]

SYNTHETIC_HISTORICAL_REFERENCE = [
    "Back in 2022, the company shut down after a liquidity crisis, which remains a cautionary "
    "tale for the industry today.",
    "On the three-year anniversary of the collapse, we look back at how the exchange filed for "
    "bankruptcy and what has changed since.",
]

SYNTHETIC_UNRELATED = [
    "An unrelated company sharing a similar name recently shut down; our project is a different "
    "entity with no relation to that business and continues to operate.",
]

SYNTHETIC_SUBSIDIARY = [
    "Our lending arm, a subsidiary of the parent company, has filed for bankruptcy. The main "
    "token protocol and its treasury are unaffected and continue to operate independently.",
]

# Deliberate duplicates (same underlying announcement, different wording/source)
SYNTHETIC_DUPLICATE_PAIR = [
    "BREAKING: ExampleChain Labs announces it is permanently shutting down the ExampleChain "
    "protocol effective immediately.",
    "ExampleChain Labs confirmed today that the ExampleChain protocol will be permanently shut "
    "down, effective immediately, according to an official statement.",
]
