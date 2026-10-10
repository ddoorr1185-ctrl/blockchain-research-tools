"""Validates the classifier against two real-world 2026 crypto shutdown
events. Texts below are PARAPHRASES written for this test, not verbatim
quotes, and are based on reporting verified via web search during
development:

- Blast (BLAST token): Ethereum L2 network announced it was shutting down
  in early October 2026, citing operating costs exceeding revenue (~$110/day
  chain revenue per DefiLlama). This was a voluntary wind-down due to
  unsustainable economics, NOT a bankruptcy filing. BLAST token fell
  ~98-99% from its June 2024 peak on the news.
  Sources: https://decrypt.co/379972/ethereum-layer-2-blast-shutting-down ,
  https://cryptobriefing.com/blast-layer-2-shuts-down-operations/

- BitMart (BMX token): Exchange announced in late July 2026 it would wind
  down trading by Aug 26, 2026 and fully cease operations by Jan 31, 2027,
  after nine years in business. This is a voluntary wind-down; no bankruptcy
  filing was found in available reporting. BMX token fell roughly 70-81% on
  the news.
  Sources: https://decrypt.co/374346/bitmart-becomes-latest-crypto-exchange-to-shut-down ,
  https://whale-alert.io/stories/d4d201aabc112d/BitMart-says-it-will-wind-down-trading-by-Aug-26-and-fully-cease-operations-in-2027-sending-BMX-down-nearly-70

IMPORTANT LIMITATION: this verification relied on web search summaries
during development, not direct retrieval of primary-source announcements
(the sandbox used to build this system blocks outbound HTTP to most
external hosts by organization policy). Before trusting these as ground
truth, cross-check against the primary announcements (Blast's own blog/X,
BitMart's own announcement page) once running on a machine with normal
network access.
"""
from app.classifier import classify


def test_blast_l2_shutdown_classified_as_permanent_protocol_termination():
    text = (
        "The Blast team has decided to shut down the Blast network. Daily costs of running the "
        "chain now exceed the revenue it generates, and continuing to operate the network is no "
        "longer financially sustainable. Users have until October 26 to withdraw funds through the "
        "normal bridge interface; after that, the protocol will no longer be maintained."
    )
    r = classify(text, source_credibility=0.9, is_primary_source=True)
    assert r.event_type in ("A", "D")
    assert r.severity == "CRITICAL"
    assert r.is_actionable


def test_bitmart_exchange_wind_down_classified_as_exchange_closure():
    text = (
        "BitMart will wind down its exchange and end all trading by August 26, with full cessation "
        "of operations by early next year. The platform will cease all trading services and stop "
        "accepting new deposits as part of the closure."
    )
    r = classify(text, source_credibility=0.9, is_primary_source=True)
    assert r.event_type == "C"
    assert r.severity == "CRITICAL"
    assert r.is_actionable


def test_neither_historical_event_is_misclassified_as_bankruptcy():
    """Both Blast and BitMart were voluntary wind-downs, not bankruptcy
    filings, per available reporting. The classifier should not invent a
    bankruptcy classification (type B) that the source text doesn't support.
    """
    blast_text = "The Blast team has decided to shut down the network due to unsustainable operating costs."
    bitmart_text = "BitMart will wind down its exchange and end all trading by August 26."
    for text in (blast_text, bitmart_text):
        r = classify(text, source_credibility=0.9, is_primary_source=True)
        assert r.event_type != "B"
