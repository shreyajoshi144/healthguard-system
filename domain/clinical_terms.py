"""Small, explicit clinical-language lexicon shared by grounding validation and retrieval.

This is deliberately a hand-curated, reviewable table, NOT a medical ontology and NOT a language model. It lets the
deterministic validators (1) treat common abbreviations and synonyms as the same thing, (2) detect opposite meanings
(high/low, started/stopped, ...), frequencies, units, negation and family-versus-patient attribution, and (3) expand
patient questions with the record vocabulary they usually mean. Coverage is limited: terms not listed here are matched
literally. Extending it is a reviewed code change with a regression test, never a runtime setting.
"""
from __future__ import annotations

import re

# Multi-word phrases first (longest match wins), then single tokens. Values are canonical forms.
PHRASE_CANON = {
    "blood sugar": "glucose", "blood glucose": "glucose", "high blood pressure": "hypertension", "low blood pressure": "hypotension",
    "sugar level": "glucose", "cholesterol level": "cholesterol",
    "blood pressure": "blood pressure", "heart attack": "myocardial infarction", "type 2 diabetes mellitus": "type 2 diabetes",
    "type ii diabetes": "type 2 diabetes", "type 1 diabetes mellitus": "type 1 diabetes", "hemoglobin a1c": "hba1c", "glycated hemoglobin": "hba1c",
    "glycosylated hemoglobin": "hba1c", "diabetes mellitus": "diabetes", "twice daily": "freq2", "two times daily": "freq2", "twice a day": "freq2",
    "once daily": "freq1", "once a day": "freq1", "one time daily": "freq1", "every day": "freq1", "three times daily": "freq3", "three times a day": "freq3",
    "four times daily": "freq4", "four times a day": "freq4", "ruled out": "ruledout", "no evidence of": "negcue", "free of": "negcue", "negative for": "negcue",
}
TOKEN_CANON = {
    "bp": "blood pressure", "hb": "hemoglobin", "hgb": "hemoglobin", "a1c": "hba1c", "htn": "hypertension", "dm": "diabetes", "t2dm": "type 2 diabetes",
    "t1dm": "type 1 diabetes", "mi": "myocardial infarction", "ecg": "electrocardiogram", "ekg": "electrocardiogram", "mri": "mri",
    "od": "freq1", "qd": "freq1", "daily": "freq1", "nightly": "freq1", "bd": "freq2", "bid": "freq2", "twice": "freq2", "tds": "freq3", "tid": "freq3",
    "thrice": "freq3", "qid": "freq4",
    # synonyms collapsed to one canonical token
    "elevated": "high", "raised": "high", "discontinued": "stopped", "ceased": "stopped", "withdrawn": "stopped", "ended": "stopped",
    "initiated": "started", "commenced": "started", "begun": "started", "rose": "increased", "risen": "increased", "rising": "increased",
    "fell": "decreased", "fallen": "decreased", "dropped": "decreased", "unremarkable": "normal", "reactive": "positive", "nonreactive": "negative",
    "allergic": "allergy", "done": "performed", "conducted": "performed", "undertaken": "performed", "tsh": "thyroid stimulating hormone",
    "wbc": "white blood cell", "rbc": "red blood cell", "plt": "platelet", "crp": "c-reactive protein", "allergies": "allergy", "tumour": "tumor", "tumours": "tumor", "denied": "denies",
}
# Opposites: a claim carrying one member while the evidence carries the other (and not the claim's) is a contradiction.
OPPOSITES = [("high", "low"), ("increased", "decreased"), ("normal", "abnormal"), ("positive", "negative"), ("started", "stopped")]
FREQ = {"freq1": 1, "freq2": 2, "freq3": 3, "freq4": 4}

NEGATION_CUES = {"no", "not", "denies", "without", "never", "none", "absent", "negcue", "cannot", "nil"}
NEGATION_WINDOW = 4

RELATIVES = {"father", "mother", "brother", "sister", "son", "daughter", "grandmother", "grandfather", "aunt", "uncle", "parent", "parents",
             "sibling", "siblings", "cousin", "relative", "relatives", "family"}

# Words that assert causation, efficacy, adherence, trend or certainty. A claim using one must find it in the evidence itself.
INFERENCE_MARKERS = {"caused", "causes", "because", "due", "therefore", "controls", "controlled", "well", "poorly", "always", "never", "declining",
                     "improving", "worsening", "improved", "worsened", "likely", "probably", "suggests", "indicates", "indicating", "consistent",
                     "adherent", "compliant", "regularly", "effective", "ineffective", "stable", "deteriorating", "responsible"}

# Relational words that are legitimately derived from comparing cited values (not required to appear in the evidence).
RELATIONAL = {"from", "to", "than", "compared", "between", "both", "two", "disagree", "differ", "differs", "conflict", "conflicting", "discrepancy", "records", "reports", "values"}

STOPWORDS = {"he", "she", "him", "hers", "they", "them", "we", "you", "i", "me", "s", "the", "a", "an", "of", "in", "on", "at", "for", "and", "or", "to", "with", "by", "is", "are", "was", "were", "be", "been", "has", "have", "had",
             "patient", "patients", "record", "records", "recorded", "lists", "listed", "list", "shows", "show", "showed", "noted", "documented", "reported", "reports",
             "takes", "taking", "take", "taken", "took", "prescribed", "this", "that", "these", "those", "it", "its", "their", "his", "her", "my", "your", "any", "as",
             "per", "after", "before", "during", "date", "level", "result", "results", "value", "values", "test", "tested", "recent", "latest", "last", "on", "from", "than"}

UNIT_PATTERN = r"(?:mg/dl|g/dl|mmol/l|umol/l|µmol/l|ng/ml|pg/ml|miu/l|miu/ml|iu/l|u/l|mmhg|mcg|µg|mg|kg|lbs?|ml|iu|units?|bpm|cm|mm|%|g|l)"
_UNIT_CANON = {"µmol/l": "umol/l", "µg": "mcg", "lbs": "lb", "unit": "units"}

# Sentences inside a document that read as instructions to an AI are NOT clinical content and can never support a claim.
INJECTION_SENTENCE = re.compile(
    r"\b(?:ignore|disregard|forget|override)\b[^.]*\b(?:instruction|instructions|rule|rules|previous|above|prior|polic(?:y|ies))\b"
    r"|\b(?:system|assistant|developer|ai|model)\s*:"
    r"|\b(?:new|updated|real|actual)\s+instructions?\b"
    r"|\b(?:you|assistant)\s+(?:must|should|will|shall|are\s+to)\b"
    r"|\b(?:tell|instruct|state|say|reveal|respond|answer|diagnose|output|print|cite|mark|pretend)\b[^.]*\b(?:user|patient|that|everything|grounded|database|all)\b"
    r"|\bdo\s+not\s+follow\b|\bjailbreak\b|\bprompt\b",
    re.I,
)

# Plain-language question vocabulary -> record vocabulary (query expansion for retrieval).
QUERY_EXPANSION = {
    "allergic": ["allergy"], "allergies": ["allergy"], "sugar": ["glucose", "hba1c", "diabetes"], "kidney": ["creatinine", "egfr", "renal"],
    "kidneys": ["creatinine", "egfr", "renal"], "heart": ["cardiology", "cardiac", "ecg", "cholesterol"], "cardiologist": ["cardiology"],
    "pressure": ["hypertension", "blood pressure"], "cholesterol": ["lipid", "cholesterol"], "vitamin": ["vitamin"], "scan": ["mri", "ct", "ultrasound", "imaging"],
    "xray": ["x-ray", "imaging"], "thyroid": ["tsh", "thyroid"], "liver": ["alt", "ast", "bilirubin"], "blood": ["hemoglobin", "hba1c", "glucose"],
}


def expand_query(query: str) -> str:
    """Append the record vocabulary a plain-language question usually means ('kidney' -> creatinine). Adds only; idempotent."""
    words = re.findall(r"[a-z0-9]+", query.lower())
    have = set(words)
    extra: list[str] = []
    for w in words:
        for term in QUERY_EXPANSION.get(w, []):
            if term not in have and term not in extra:
                extra.append(term)
    return query if not extra else f"{query} {' '.join(extra)}"
