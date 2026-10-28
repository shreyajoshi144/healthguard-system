from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from domain import clinical_terms as ct
from ai.answer_provider import AnswerResult
from ai.embedding_provider import DeterministicEmbeddingProvider, cosine_similarity
from domain.ai_safety import classify_medical_safety

_TOKEN_RE = re.compile(r"[a-z0-9]+(?:[./-][a-z0-9]+)*", re.I)
_STOPWORDS = {"a", "an", "and", "are", "as", "for", "from", "has", "have", "in", "is", "it", "of", "on", "or", "the", "to", "was", "were", "with", "this", "that", "record", "records"}

SAFE_INSUFFICIENT_ANSWER = "The record does not contain enough information to answer that question."

def _unsafe_medical_claim(text: str) -> bool:
    # A claim is OUTPUT: judge it as a record statement (directives/inference blocked), not as a patient question.
    from domain.ai_safety import validate_medical_output
    return not validate_medical_output(text, record_statement=True).safe_for_provider


@dataclass(frozen=True)
class GroundingValidation:
    valid: bool
    answer: str
    citations: list[str]
    grounded: bool
    uncertainty: str | None
    reason: str | None = None


def _tokens(text: str) -> set[str]:
    return {t.lower() for t in _TOKEN_RE.findall(text) if t.lower() not in _STOPWORDS}


def _strip_instructions(text: str) -> str:
    """Sentences that read as instructions to an AI are not clinical content: they can never support a claim."""
    parts = re.split(r"(?<=[.!?])\s+|\n+", text)
    return " ".join(p for p in parts if p and not ct.INJECTION_SENTENCE.search(p))


def _evidence_text(item: dict[str, Any]) -> str:
    details = item.get("details") or {}
    parts = [item.get("source_text"), item.get("value"), details, item.get("date")]
    family = str(item.get("provenance", "")) == "FAMILY_REPORTED" or str(details.get("fact_type", "")).upper() == "FAMILY_HISTORY"
    text = " | ".join(str(x) for x in parts if x is not None).lower()   # "|" is a clause boundary: negation must not leak across parts
    return _strip_instructions(text) + (" family history" if family and "family" not in text else "")


_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november", "december"], 1)}
_MONTHS.update({k[:3]: v for k, v in list(_MONTHS.items())} | {"sept": 9})
_MON = "|".join(sorted(_MONTHS, key=len, reverse=True))
# Full dates are compared as atomic values so "2024-03-01" can never be satisfied by the digits 2024, 3 and 1 appearing separately.
_ISO_DATE = re.compile(r"(?<![\d.])(\d{4})-(\d{1,2})-(\d{1,2})(?![\d])")
_SLASH_DATE = re.compile(r"(?<![\d.])(\d{1,2})[/.](\d{1,2})[/.](\d{4})(?![\d])")   # ambiguous locale: compared literally, never reinterpreted
_MDY_DATE = re.compile(rf"\b({_MON})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b", re.I)
_DMY_DATE = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MON})\.?,?\s+(\d{{4}})\b", re.I)
_MY_DATE = re.compile(rf"\b({_MON})\.?\s+(\d{{4}})\b", re.I)
_NUMBER = re.compile(r"(?<![\d.])\d+(?:\.\d+)?")


def _norm_number(raw: str) -> str:
    from decimal import Decimal
    d = Decimal(raw).normalize()
    return format(d, "f")


def _anchors(text: str) -> tuple[set[tuple], set[str]]:
    """Return (dates, numbers) mentioned in ``text``. Dates are removed before numbers are read so that the parts of a
    date are never treated as independent quantities."""
    t = text.lower()
    dates: set[tuple] = set()

    def take(rx, conv):
        nonlocal t
        for m in rx.finditer(t):
            dates.add(conv(m))
        t = rx.sub(" ", t)

    take(_ISO_DATE, lambda m: ("ymd", int(m[1]), int(m[2]), int(m[3])))
    take(_SLASH_DATE, lambda m: ("slash", int(m[1]), int(m[2]), int(m[3])))
    take(_MDY_DATE, lambda m: ("ymd", int(m[3]), _MONTHS[m[1].lower().rstrip(".")], int(m[2])))
    take(_DMY_DATE, lambda m: ("ymd", int(m[3]), _MONTHS[m[2].lower().rstrip(".")], int(m[1])))
    take(_MY_DATE, lambda m: ("ym", int(m[2]), _MONTHS[m[1].lower().rstrip(".")]))
    return dates, {_norm_number(n) for n in _NUMBER.findall(t)}


def _anchors_supported(claim_text: str, evidence_text: str) -> bool:
    """Every date and number the claim states must be present in the cited evidence (same value, any notation)."""
    c_dates, c_nums = _anchors(claim_text)
    e_dates, e_nums = _anchors(evidence_text)
    for d in c_dates:
        if d[0] == "ym":
            if not any(e[0] in {"ymd", "ym"} and e[1:3] == d[1:3] for e in e_dates):
                return False
        elif d not in e_dates:
            return False
    return c_nums <= e_nums


_WORDS = re.compile(r"[a-z0-9]+(?:[./-][a-z0-9]+)*|[,;:.()|]", re.I)
_NUMERIC = re.compile(r"^\d[\d.,/:-]*$")
_UNIT_RE = re.compile(rf"^{ct.UNIT_PATTERN}$")
_UNIT_PAIR = re.compile(rf"(?<![\d.])(\d+(?:\.\d+)?)\s*({ct.UNIT_PATTERN})(?![a-z])", re.I)
_PHRASES = sorted(ct.PHRASE_CANON, key=len, reverse=True)
_PHRASE_RE = re.compile(r"\b(" + "|".join(re.escape(p) for p in _PHRASES) + r")\b")
_DATE_RES = (_ISO_DATE, _SLASH_DATE, _MDY_DATE, _DMY_DATE, _MY_DATE)
_NONCONTENT = ct.STOPWORDS | ct.NEGATION_CUES | set(ct.FREQ) | {"ruledout", "|", ",", ";", ":", ".", "(", ")"}
_CURRENT = {"currently", "current", "now", "still", "ongoing"}


def _canon_words(text: str) -> list[str]:
    t = text.lower()
    for rx in _DATE_RES:
        t = rx.sub(" ", t)
    t = _PHRASE_RE.sub(lambda m: " " + ct.PHRASE_CANON[m.group(1)] + " ", t)
    out: list[str] = []
    for w in _WORDS.findall(t):
        mapped = ct.TOKEN_CANON.get(w, w)
        out.extend(mapped.split())
    return out


def _content(words: list[str]) -> set[str]:
    res = set()
    for w in words:
        if w in _NONCONTENT or _NUMERIC.match(w) or _UNIT_RE.match(w) or "/" in w and not w.isalpha():
            continue
        res.add(w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w)
    return res


def _negated(words: list[str]) -> set[str]:
    neg: set[str] = set()
    for i, w in enumerate(words):
        if w == "ruledout":
            for j in range(max(0, i - 3), i):
                neg.add(words[j])
        elif w in ct.NEGATION_CUES:
            for nxt in words[i + 1:i + 1 + ct.NEGATION_WINDOW]:
                if nxt in {"|", ",", ";", ":", ".", "(", ")"}:
                    break
                neg.add(nxt)
    return {n[:-1] if len(n) > 3 and n.endswith("s") and not n.endswith("ss") else n for n in neg}


def _unit_pairs(text: str) -> set[tuple[str, str]]:
    out = set()
    for n, u in _UNIT_PAIR.findall(text.lower()):
        u = ct._UNIT_CANON.get(u, u)
        out.add((_norm_number(n), u))
    return out


def _numbers_from_to(text: str):
    m = re.search(r"from\s+(\d+(?:\.\d+)?)\s*(?:[a-z%/]+)?\s+to\s+(\d+(?:\.\d+)?)", text.lower())
    return (float(m.group(1)), float(m.group(2))) if m else None


def _contradicts(claim: str, c_words: list[str], e_text: str, e_words: list[str]) -> bool:
    cs, es = set(c_words), set(e_words)
    for a, b in ct.OPPOSITES:                                   # high/low, started/stopped, ...
        if (a in cs and b in es and a not in es) or (b in cs and a in es and b not in es):
            return True
    cf = {ct.FREQ[w] for w in c_words if w in ct.FREQ}; ef = {ct.FREQ[w] for w in e_words if w in ct.FREQ}
    if cf and ef and cf.isdisjoint(ef):                         # once vs twice daily
        return True
    e_units = _unit_pairs(e_text)
    for n, u in _unit_pairs(claim):                             # same number, different unit
        if (n, u) not in e_units and any(n == n2 for n2, _ in e_units):
            return True
    c_neg, e_neg = _negated(c_words), _negated(e_words)
    for tok in _content(c_words) & _content(e_words):           # negated in one, affirmed in the other
        if (tok in c_neg) != (tok in e_neg):
            return True
    c_rel, e_rel = ct.RELATIVES & cs, ct.RELATIVES & es         # patient vs family-member attribution
    if bool(c_rel) != bool(e_rel):
        return True
    specific_c, specific_e = c_rel - {"family", "relative", "relatives"}, e_rel - {"family", "relative", "relatives"}
    if specific_c and specific_e and not specific_c <= specific_e:
        return True
    if cs & _CURRENT and "stopped" in es and "stopped" not in cs:   # "currently takes X" vs "X discontinued"
        return True
    ft = _numbers_from_to(claim)
    if ft and ((ft[1] < ft[0] and "increased" in cs) or (ft[1] > ft[0] and "decreased" in cs)):
        return True
    return False


def _claim_supported_multi(claim_text: str, evidence_texts: list[str]) -> bool:
    """True only if the cited evidence (alone or, for multi-source claims, together) states what the claim states.

    Checks, in order: every number/date in the claim is in the evidence; no cited evidence contradicts the claim
    (negation, opposite terms, frequency, unit, family-vs-patient, current-vs-stopped); the claim uses no causal,
    efficacy, adherence or trend wording the evidence does not itself use; and all of its content words are covered.
    """
    if not evidence_texts:
        return False
    claim_l = claim_text.lower()
    c_words = _canon_words(claim_l); c_content = _content(c_words)
    per = [(t, _canon_words(t)) for t in evidence_texts]
    union_text = " ".join(evidence_texts)
    related = [(t, w) for t, w in per if _content(w) & c_content]
    if not related or not _anchors_supported(claim_l, union_text):
        return False
    for t, w in related:
        if _contradicts(claim_l, c_words, t, w):
            return False
    e_all: set[str] = set().union(*[set(w) for _, w in per])
    e_content = set().union(*[_content(w) for _, w in per])
    if (set(c_words) & ct.INFERENCE_MARKERS) - e_all:
        return False
    # every number must sit in an evidence item that is about what the claim is about (blocks Hemoglobin 1.1 from Creatinine 1.1)
    c_nums = _anchors(claim_l)[1]
    for n in c_nums:
        if not any(n in _anchors(t)[1] for t, _ in related):
            return False
    exempt = ct.RELATIONAL | ({"increased", "decreased"} if len(evidence_texts) > 1 or _numbers_from_to(claim_l) else set())
    missing = c_content - e_content - exempt
    overlap = c_content & e_content
    if not missing:
        return True
    return len(c_content) >= 4 and len(missing) == 1 and len(overlap) >= 2 and not (missing & ct.INFERENCE_MARKERS)


def _claim_supported(claim_text: str, evidence_text: str) -> bool:
    return _claim_supported_multi(claim_text, [evidence_text])


def _contradiction_groups(evidence: list[dict[str, Any]]) -> list[set[str]]:
    """Facts that say different things about the same thing AT THE SAME TIME. A dated series (HbA1c in 2023 and 2024) is
    history, not a contradiction; two values for the same date, or 'asthma' next to 'asthma ruled out', are."""
    groups: dict[tuple, list[tuple[dict[str, Any], bool]]] = {}
    for item in evidence:
        if item.get("source_type") != "HEALTH_FACT":
            continue
        details = item.get("details") or {}
        fact_type = str(details.get("fact_type") or "").upper()
        value = str(item.get("value") or details.get("normalized_value") or "").strip().lower()
        if not fact_type or not value:
            continue
        words = _canon_words(value)
        content = _content(words) - {"diagnosis", "dx"}
        negated = bool(_negated(words) & (content | {w for w in words}) - {"diagnosis", "dx", ":", "|"} - {"ruledout"}) or "ruledout" in words
        groups.setdefault((fact_type, " ".join(sorted(content)) or value, item.get("date")), []).append((item, negated))
    out = []
    for members in groups.values():
        values = {str(i.get("value") or "").strip().lower() for i, _ in members}
        polarities = {n for _, n in members}
        if len(members) > 1 and (len(values) > 1 or len(polarities) > 1):
            out.append({str(i["evidence_id"]) for i, _ in members})
    return out


def validate_grounded_answer(result: AnswerResult, evidence: list[dict[str, Any]], *, expected_patient_id: str | None = None, evidence_patient_ids: dict[str, str] | None = None) -> GroundingValidation:
    by_id = {str(item["evidence_id"]): item for item in evidence if item.get("evidence_id")}
    if len(by_id) != len(evidence):
        return GroundingValidation(False, SAFE_INSUFFICIENT_ANSWER, [], False, "Evidence identifiers were invalid or duplicated.", "invalid evidence identifiers")
    for item in evidence:
        evidence_id = str(item.get("evidence_id"))
        actual_patient_id = item.get("patient_id")
        if actual_patient_id is None and evidence_patient_ids:
            actual_patient_id = evidence_patient_ids.get(evidence_id)
        if expected_patient_id is not None and str(actual_patient_id) != str(expected_patient_id):
            return GroundingValidation(False, SAFE_INSUFFICIENT_ANSWER, [], False, "Evidence ownership could not be verified.", "patient ownership mismatch")
        provenance = str(item.get("provenance", ""))
        if provenance and provenance not in {"DOCUMENT_VERIFIED", "USER_REPORTED", "FAMILY_REPORTED", "SYSTEM_DERIVED"}:
            return GroundingValidation(False, SAFE_INSUFFICIENT_ANSWER, [], False, "Evidence provenance is invalid.", "invalid provenance")
    if not evidence:
        return GroundingValidation(False, SAFE_INSUFFICIENT_ANSWER, [], False, "No retrieved evidence was available.", "empty evidence")

    if not result.grounded:
        if result.claims or result.citations:
            return GroundingValidation(False, SAFE_INSUFFICIENT_ANSWER, [], False, result.uncertainty, "ungrounded response contained claims")
        return GroundingValidation(True, SAFE_INSUFFICIENT_ANSWER, [], False, result.uncertainty or "The retrieved evidence was insufficient.")

    if not result.claims:
        return GroundingValidation(False, SAFE_INSUFFICIENT_ANSWER, [], False, "No grounded claims were returned.", "missing claims")

    all_citations: list[str] = []
    rendered_claims: list[str] = []
    for claim in result.claims:
        text = claim.text.strip()
        if not text or not claim.citations:
            return GroundingValidation(False, SAFE_INSUFFICIENT_ANSWER, [], False, "A factual claim lacked evidence citations.", "uncited claim")
        for citation in claim.citations:
            if citation not in by_id:
                return GroundingValidation(False, SAFE_INSUFFICIENT_ANSWER, [], False, "A generated citation was not part of the authorized retrieval result.", "invalid citation")
        if _unsafe_medical_claim(text):
            return GroundingValidation(False, SAFE_INSUFFICIENT_ANSWER, [], False, "Medical recommendations or unsupported clinical conclusions are not permitted.", "medical safety policy")
        cited_texts = [_evidence_text(by_id[citation]) for citation in claim.citations]
        supported = any(_claim_supported(text, t) for t in cited_texts) or (len(cited_texts) > 1 and _claim_supported_multi(text, cited_texts))
        if not supported:
            return GroundingValidation(False, SAFE_INSUFFICIENT_ANSWER, [], False, "A factual claim was not sufficiently supported by its cited evidence.", "unsupported claim")
        rendered_claims.append(text)
        all_citations.extend(claim.citations)

    unique_citations = list(dict.fromkeys(all_citations))
    if result.citations and set(result.citations) != set(unique_citations):
        return GroundingValidation(False, SAFE_INSUFFICIENT_ANSWER, [], False, "Provider citation metadata did not match claim citations.", "citation mismatch")
    answer = " ".join(rendered_claims).strip()
    cited_set = set(unique_citations)
    for contradiction_group in _contradiction_groups(evidence):
        if not contradiction_group.issubset(cited_set):
            return GroundingValidation(False, SAFE_INSUFFICIENT_ANSWER, [], False, "Conflicting evidence was not fully represented.", "contradictory evidence omitted")
    if not answer:
        return GroundingValidation(False, SAFE_INSUFFICIENT_ANSWER, [], False, "No answer text was produced.", "empty answer")
    return GroundingValidation(True, answer, unique_citations, True, result.uncertainty)


def validate_medication_context_output(result, evidence: list[dict[str, Any]], *, expected_patient_id: str | None = None):
    """Validate the complete medication response, including educational content.

    Educational fields are not patient facts, but they are still model output and
    therefore pass the canonical medical-safety output policy before being returned.
    Patient claims additionally pass the normal grounding/provenance/citation path.
    """
    from domain.ai_safety import validate_medical_output
    educational_text = "\n".join(str(v) for v in (result.educational or {}).values() if v is not None)
    complete_output = "\n".join(x for x in [result.answer, educational_text] if x)
    safety = validate_medical_output(complete_output)
    if not safety.safe_for_provider:
        return GroundingValidation(False, SAFE_INSUFFICIENT_ANSWER, [], False,
                                   "Medication context contained unsafe medical output.", "medical safety policy")

    answer_result = AnswerResult(
        result.answer,
        result.patient_claims,
        [c for claim in result.patient_claims for c in claim.citations],
        result.grounded,
        None,
    )
    if result.patient_claims:
        return validate_grounded_answer(
            answer_result,
            evidence,
            expected_patient_id=expected_patient_id,
        )
    if result.grounded:
        return GroundingValidation(False, SAFE_INSUFFICIENT_ANSWER, [], False,
                                   "A grounded medication response contained no patient claims.", "missing claims")
    # Educational-only output is allowed only when it is explicitly ungrounded;
    # the complete output has already passed the medical-safety boundary.
    return GroundingValidation(True, result.answer.strip() or SAFE_INSUFFICIENT_ANSWER, [], False, None)
