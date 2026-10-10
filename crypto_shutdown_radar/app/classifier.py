"""Shutdown/bankruptcy event classifier.

Deliberately rule-based (no paid LLM/API calls) so every decision is
inspectable and reproducible. The pipeline is:

  1. keyword/pattern matching (multilingual, phrase-level, not single words)
  2. negation detection (a denial must not become a positive match)
  3. rumor/speculation detection
  4. historical-reference detection
  5. entity scope detection (organization / token / subsidiary / product / unrelated)
  6. event type classification (A..J)
  7. transparent confidence + severity scoring

Confidence and severity are computed independently, as required: confidence
reflects how sure we are about WHAT was said; severity reflects HOW BAD it
would be IF true and officially confirmed.
"""
import re
from dataclasses import dataclass, field
from typing import Optional

EVENT_TYPES = {
    "A": "Permanent project shutdown",
    "B": "Bankruptcy or insolvency",
    "C": "Permanent exchange closure",
    "D": "Permanent protocol termination",
    "E": "Permanent product discontinuation",
    "F": "Temporary suspension",
    "G": "Routine maintenance",
    "H": "Rumor or speculation",
    "I": "Historical reference",
    "J": "Denial of a shutdown",
}

# Phrase-level patterns, grouped by the event type they indicate when
# un-negated and not speculative. Patterns are regexes matched case-insensitively
# against the full text. Multilingual coverage is intentionally partial:
# English is primary; a handful of high-signal Spanish/Chinese/Korean/Russian
# phrases are included because they recur in real-world crypto-collapse
# announcements. This is NOT a full translation of every pattern.
SHUTDOWN_PATTERNS = {
    "B": [  # bankruptcy / insolvency
        r"\bfil(?:e|ed|ing) for (?:chapter\s*(?:7|11)\s*)?bankruptcy\b",
        r"\bdeclar(?:e|ed|ing) bankruptcy\b",
        r"\bentering? (?:into )?liquidation\b",
        r"\bplaced into liquidation\b",
        r"\binsolven(?:t|cy)\b",
        r"\bunable to (?:meet|honor) (?:withdrawal|customer) (?:requests|obligations)\b",
        r"\bceas(?:e|ed|ing) to (?:be able to )?operate due to insolvency\b",
        r"quiebra",                      # Spanish: bankruptcy
        r"insolvencia",                  # Spanish: insolvency
        r"破产",                          # Chinese: bankruptcy
        r"파산",                          # Korean: bankruptcy
        r"банкротств",                   # Russian: bankrupt(cy)
    ],
    "A": [  # permanent project/company shutdown
        r"\bshut(?:ting|ted)? down\b(?: (?:permanently|for good|entirely))?",
        r"\bpermanently (?:clos(?:e|ing|ed)|discontin(?:ue|uing|ued))\b",
        r"\bceas(?:e|ed|ing) (?:all )?operations\b",
        r"\bwind(?:ing)? down (?:the|our) (?:business|company|project|operations)\b",
        r"\bend(?:ing)? all operations\b",
        r"\bsunsett(?:ing|ed) (?:the|our) project\b",
        r"\bthis (?:is|will be) (?:the )?(?:final|last) (?:update|announcement) before (?:we )?(?:shut|close)\b",
        r"关闭运营",                       # Chinese: close operations
        r"영구적으로 (?:폐쇄|종료)",         # Korean: permanently close/terminate
    ],
    "C": [  # permanent exchange closure
        r"\b(?:exchange|platform) (?:will|is) (?:permanently )?clos(?:e|ing)\b",
        r"\bdelist all (?:markets|pairs|trading) and (?:close|shut down)\b",
        r"\bcease all (?:trading|exchange) (?:services|operations)\b",
        r"\bwithdraw(?:al)?s? (?:will be )?(?:permanently )?(?:disabled|suspended) (?:as we close|before closure)\b",
    ],
    "D": [  # permanent protocol termination
        r"\b(?:the )?protocol will be (?:permanently )?(?:terminated|discontinued|shut down)\b",
        r"\bterminat(?:e|ing|ed) the protocol\b",
        r"\bdeprecat(?:e|ing|ed) the (?:protocol|network|chain) permanently\b",
        r"\bchain will (?:no longer|cease to) (?:be maintained|produce blocks)\b",
    ],
    "E": [  # permanent product discontinuation
        r"\bdiscontinu(?:e|ing|ed) (?:this|the|our) (?:product|service|feature)\b",
        r"\b(?:product|service|feature|offering)\b.{0,40}\b(?:is being|will be|has been) discontinued\b",
        r"\bdiscontinu(?:e|ing|ed)\b.{0,40}\b(?:product|service|feature|offering)\b",
        r"\bthis product (?:is|will be) (?:permanently )?retired\b",
        r"\bsunset(?:ting|ted) (?:this|the) (?:product|service|feature)\b",
    ],
    "F": [  # temporary suspension (explicitly NOT a shutdown)
        r"\btemporar(?:y|ily) (?:suspend|pause|halt)(?:ed|ing)?\b",
        r"\bwill resume (?:shortly|soon|within)\b",
        r"\bscheduled maintenance\b",
        r"\bback online (?:shortly|soon)\b",
        r"\bbrief(?:ly)? (?:unavailable|down)\b",
    ],
    "G": [  # routine maintenance
        r"\bscheduled (?:system )?maintenance\b",
        r"\brouting (?:network )?upgrade\b",
        r"\bplanned (?:downtime|maintenance window)\b",
    ],
}

NEGATION_PATTERNS = [
    r"\bnot shutting down\b",
    r"\bnot closing\b",
    r"\bno plans to (?:shut down|close|cease operations)\b",
    r"\bwe (?:are|'re) not\b.{0,40}\b(?:shut|clos|ceas)",
    r"\bdenies?\b.{0,40}\b(?:shutdown|bankruptcy|closure|closing)\b",
    r"\b(?:rumors?|reports?|claims?) (?:of|about) .{0,40}(?:shutdown|bankruptcy|closure) (?:are|is) (?:false|untrue|incorrect|unfounded)\b",
    r"\bcontinu(?:e|ing) (?:normal )?operations?\b",
    r"\bremains? (?:fully )?operational\b",
    r"\bbusiness as usual\b",
]

RUMOR_PATTERNS = [
    r"\brumou?rs?\b",
    r"\ballegedly\b",
    r"\bsources? (?:say|claim|suggest)\b",
    r"\bspeculat(?:e|ion|ing)\b",
    r"\bmay\b.{0,30}\b(?:shut|clos|bankrupt)",
    r"\bmight (?:shut|close|file for bankruptcy)\b",
    r"\bunconfirmed reports?\b",
    r"\bsome (?:users?|investors?|traders?) (?:believe|fear|worry)\b",
    r"\bcould (?:be forced to|potentially)\b.{0,30}\b(?:shut|clos|liquidat)",
]

HISTORICAL_PATTERNS = [
    r"\bin (?:19|20)\d{2}[,]? (?:the )?(?:company|project|exchange)?.{0,40}\b(?:shut down|collapsed|filed for bankruptcy|closed)\b",
    r"\b(?:anniversary|years? since|remember when)\b",
    r"\bback in (?:19|20)\d{2}\b",
    r"\bpreviously (?:shut down|collapsed|went bankrupt)\b",
]

UNRELATED_HINTS = [
    r"\bunrelated (?:company|organization|project|entity)\b",
    r"\bno relation to\b",
    r"\bdifferent company (?:with|sharing) (?:a )?similar name\b",
]

SUBSIDIARY_HINTS = [r"\bsubsidiary\b", r"\baffiliate\b", r"\bits (?:lending|trading|custody) arm\b"]
PRODUCT_HINTS = [r"\b(?:this|the|our) (?:product|feature|service|app)\b(?! of the (?:company|project))"]


def _any_match(patterns, text):
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return m
    return None


@dataclass
class ClassificationResult:
    event_type: str
    event_label: str
    severity: str           # CRITICAL | HIGH | MEDIUM | NONE
    confidence: float        # 0.0 - 1.0, independent of severity
    affected_scope: str      # organization | token | subsidiary | product | unrelated | unknown
    evidence: list = field(default_factory=list)
    is_actionable: bool = False  # True only for A/B/C/D (+E at HIGH) non-negated, non-rumor, non-historical


def classify(text: str, *, source_credibility: float = 0.6, is_primary_source: bool = False) -> ClassificationResult:
    """Classify a single text item. `source_credibility` in [0,1] reflects
    how trustworthy the source channel is; `is_primary_source` marks an
    official company/exchange channel as opposed to third-party news.
    """
    text = text or ""

    negation_match = _any_match(NEGATION_PATTERNS, text)
    if negation_match:
        return ClassificationResult(
            event_type="J",
            event_label=EVENT_TYPES["J"],
            severity="NONE",
            confidence=min(0.9, 0.6 + 0.3 * source_credibility),
            affected_scope="organization",
            evidence=[negation_match.group(0)],
            is_actionable=False,
        )

    historical_match = _any_match(HISTORICAL_PATTERNS, text)

    rumor_match = _any_match(RUMOR_PATTERNS, text)

    # Find the strongest matching shutdown-type pattern across categories,
    # preferring more severe categories (B > A > C > D > E) when several match.
    priority = ["B", "C", "A", "D", "E", "F", "G"]
    matched_type = None
    matched_evidence = []
    for etype in priority:
        m = _any_match(SHUTDOWN_PATTERNS[etype], text)
        if m:
            matched_type = etype
            matched_evidence.append(m.group(0))
            break

    if matched_type is None:
        if rumor_match:
            return ClassificationResult(
                event_type="H",
                event_label=EVENT_TYPES["H"],
                severity="MEDIUM",
                confidence=round(0.35 + 0.1 * source_credibility, 2),
                affected_scope="unknown",
                evidence=[rumor_match.group(0)],
                is_actionable=False,
            )
        event_type = "I" if historical_match else "G"
        return ClassificationResult(
            event_type=event_type,
            event_label=EVENT_TYPES[event_type],
            severity="NONE",
            confidence=0.2,
            affected_scope="unknown",
            evidence=[],
            is_actionable=False,
        )

    if historical_match and not rumor_match:
        return ClassificationResult(
            event_type="I",
            event_label=EVENT_TYPES["I"],
            severity="NONE",
            confidence=0.5,
            affected_scope="unknown",
            evidence=[historical_match.group(0)] + matched_evidence,
            is_actionable=False,
        )

    unrelated_match = _any_match(UNRELATED_HINTS, text)
    subsidiary_match = _any_match(SUBSIDIARY_HINTS, text)
    product_match = _any_match(PRODUCT_HINTS, text)
    if unrelated_match:
        scope = "unrelated"
    elif subsidiary_match:
        scope = "subsidiary"
    elif product_match:
        scope = "product"
    elif matched_type in ("E",):
        scope = "product"
    else:
        scope = "organization"

    # Temporary/maintenance categories are never shutdown events.
    if matched_type in ("F", "G"):
        return ClassificationResult(
            event_type=matched_type,
            event_label=EVENT_TYPES[matched_type],
            severity="NONE",
            confidence=0.7,
            affected_scope=scope,
            evidence=matched_evidence,
            is_actionable=False,
        )

    if rumor_match:
        return ClassificationResult(
            event_type="H",
            event_label=EVENT_TYPES["H"],
            severity="MEDIUM",
            confidence=0.35 + 0.1 * source_credibility,
            affected_scope=scope,
            evidence=matched_evidence + [rumor_match.group(0)],
            is_actionable=False,
        )

    # Confirmed (non-negated, non-rumor, non-historical) event.
    # Severity: bankruptcy/permanent org-wide shutdown from a primary
    # source => CRITICAL. Same claim from secondary news => HIGH (one
    # notch down until corroborated by a primary source). Product-level
    # or subsidiary-scoped events are capped at HIGH/MEDIUM since they do
    # not necessarily threaten the token itself.
    if matched_type in ("A", "B", "C", "D"):
        if scope == "unrelated":
            severity = "NONE"
        elif is_primary_source:
            severity = "CRITICAL"
        else:
            severity = "HIGH"
    elif matched_type == "E":
        severity = "MEDIUM" if scope in ("product", "subsidiary") else "HIGH"
    else:
        severity = "MEDIUM"

    # Confidence: base on pattern specificity + source credibility + primary-source bonus.
    confidence = 0.55 + 0.25 * source_credibility + (0.1 if is_primary_source else 0.0)
    confidence = max(0.0, min(0.95, confidence))

    return ClassificationResult(
        event_type=matched_type,
        event_label=EVENT_TYPES[matched_type],
        severity=severity,
        confidence=round(confidence, 2),
        affected_scope=scope,
        evidence=matched_evidence,
        is_actionable=severity in ("CRITICAL", "HIGH") and scope != "unrelated",
    )
