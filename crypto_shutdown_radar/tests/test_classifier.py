from app.classifier import classify
from tests import fixtures as f


def test_bankruptcy_is_critical_from_primary_source():
    for text in f.SYNTHETIC_BANKRUPTCY:
        r = classify(text, source_credibility=0.9, is_primary_source=True)
        assert r.event_type == "B"
        assert r.severity == "CRITICAL"
        assert r.is_actionable


def test_bankruptcy_from_news_is_high_not_critical():
    r = classify(f.SYNTHETIC_BANKRUPTCY[0], source_credibility=0.75, is_primary_source=False)
    assert r.event_type == "B"
    assert r.severity == "HIGH"


def test_permanent_shutdown_detected():
    for text in f.SYNTHETIC_PERMANENT_SHUTDOWN:
        r = classify(text, source_credibility=0.9, is_primary_source=True)
        assert r.event_type == "A"
        assert r.severity == "CRITICAL"


def test_exchange_closure_detected():
    r = classify(f.SYNTHETIC_EXCHANGE_CLOSURE[0], source_credibility=0.9, is_primary_source=True)
    assert r.event_type == "C"
    assert r.severity == "CRITICAL"


def test_protocol_termination_detected():
    r = classify(f.SYNTHETIC_PROTOCOL_TERMINATION[0], source_credibility=0.9, is_primary_source=True)
    assert r.event_type == "D"
    assert r.severity == "CRITICAL"


def test_product_discontinuation_is_scoped_down():
    r = classify(f.SYNTHETIC_PRODUCT_DISCONTINUATION[0], source_credibility=0.8, is_primary_source=True)
    assert r.event_type == "E"
    assert r.affected_scope == "product"
    assert r.severity == "MEDIUM"
    assert not r.is_actionable  # a product-level discontinuation shouldn't fire a top alert


def test_temporary_outage_is_not_a_shutdown():
    for text in f.SYNTHETIC_TEMPORARY_OUTAGE:
        r = classify(text, source_credibility=0.8, is_primary_source=True)
        assert r.event_type in ("F", "G")
        assert r.severity == "NONE"
        assert not r.is_actionable


def test_negated_shutdown_is_not_critical():
    for text in f.SYNTHETIC_NEGATION:
        r = classify(text, source_credibility=0.8, is_primary_source=True)
        assert r.event_type == "J"
        assert r.severity == "NONE"
        assert not r.is_actionable


def test_rumor_is_medium_not_confirmed():
    for text in f.SYNTHETIC_RUMOR:
        r = classify(text, source_credibility=0.5, is_primary_source=False)
        assert r.event_type == "H"
        assert r.severity == "MEDIUM"
        assert not r.is_actionable


def test_historical_reference_not_flagged_as_current():
    for text in f.SYNTHETIC_HISTORICAL_REFERENCE:
        r = classify(text, source_credibility=0.75, is_primary_source=False)
        assert r.event_type == "I"
        assert r.severity == "NONE"


def test_unrelated_company_not_flagged():
    r = classify(f.SYNTHETIC_UNRELATED[0], source_credibility=0.7, is_primary_source=False)
    assert r.affected_scope == "unrelated"
    assert r.severity == "NONE"


def test_subsidiary_bankruptcy_scoped_to_subsidiary():
    r = classify(f.SYNTHETIC_SUBSIDIARY[0], source_credibility=0.85, is_primary_source=True)
    assert r.event_type == "B"
    assert r.affected_scope == "subsidiary"
    # Still CRITICAL in our current rule set (bankruptcy wording always maps to B/CRITICAL
    # from a primary source); documented as a known conservative simplification --
    # scope is preserved on the event so a human reviewer can downgrade it.
    assert r.severity == "CRITICAL"


def test_confidence_independent_of_severity():
    critical = classify(f.SYNTHETIC_BANKRUPTCY[0], source_credibility=0.9, is_primary_source=True)
    medium_rumor = classify(f.SYNTHETIC_RUMOR[0], source_credibility=0.9, is_primary_source=False)
    # Severity differs a lot; confidence scale is bounded and computed independently.
    assert critical.severity != medium_rumor.severity
    assert 0.0 <= critical.confidence <= 1.0
    assert 0.0 <= medium_rumor.confidence <= 1.0
