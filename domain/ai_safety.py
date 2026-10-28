from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

SAFE_MEDICAL_RESPONSE = (
    "HealthGuard can provide educational context from the record, but it cannot make a personalized medical "
    "decision. Please discuss diagnosis, treatment, medication changes, or urgent symptoms with a qualified healthcare professional."
)

@dataclass(frozen=True)
class SafetyDecision:
    safe_for_provider: bool
    flags: tuple[str, ...]

@dataclass(frozen=True)
class SemanticSafetyAssessment:
    intent: str | None
    score: int
    ambiguous: bool

_WORD_RE = re.compile(r"[a-z0-9]+")
_PATTERNS = {
    "DIAGNOSIS_REQUEST": (
        r"\b(?:what|which)\s+(?:condition|disease|diagnosis)\b",
        r"\b(?:do|does)\s+(?:i|the patient|this)\s+(?:have|has)\b",
        r"\b(?:is|am)\s+(?:i|the patient)\s+(?:diabetic|anemic|infected|having)\b",
        r"\bdiagnos(?:e|is|ed|ing)\b",
        r"\b(?:what|which)\s+(?:illness|sickness|ailment|disorder|disease|condition|problem)\b[^.?!]*\b(?:i|me|my)\b",
        r"\bwhat(?:'s|\s+is)\s+wrong\s+with\s+(?:me|my)\b",
        r"\bis\s+(?:my|the|this|that)\s+(?:\w+\s+)?(?:malignant|benign|cancerous|serious|dangerous|fatal|terminal|life[- ]threatening)\b",
        r"\b(?:tumou?r|cancer|lump)\b[^.?!]*\b(?:malignant|benign|cancerous)\b",
        r"\b(?:do|does|would|could|might|can)\s+(?:my|these|this|the|those)\s+(?:\w+\s+){0,2}(?:symptoms?|signs?|results?|pains?|rash|lump|findings?)\s+(?:mean|indicate|suggest|show|point\s+to)\b",
        r"\bis\s+(?:this|that|it|my\s+\w+|the\s+\w+)\s+(?:\w+\s+)?(?:cancer|cancerous|malignant|an?\s+infection|infected|serious|dangerous|diabetic|fatal|life[- ]threatening)\b",
    ),
    "MEDICATION_CHANGE": (
        r"\b(?:start|stop|skip|change|increase|decrease|double|halve|reduce|raise|lower|discontinue|restart|switch)\b.*\b(?:dose|medication|medicine|drug|tablet|pill)\b",
        r"\b(?:dose|medication|medicine|drug|tablet|pill)\b.*\b(?:start|stop|skip|change|increase|decrease|double|halve|reduce|raise|lower|discontinue|restart|switch)\b",
        r"\b(?:should|can|may|could)\s+i\s+(?:take|skip|stop|start|change|double|halve)\b",
        r"\b(?:skip(?:ped|ping)?|double(?:d|ing)?|halve(?:d|ing)?|stop(?:ped|ping)?|start(?:ed|ing)?)\s+(?:this|it|that|my|the)?\b",
        r"\b(?:do\s+i\s+really\s+need|do\s+i\s+need)\b.*\b(?:medicine|medication|drug|pill|tablet)\b",
        r"\b(?:you|patient)\s+should\s+(?:stop|start|change|increase|decrease|skip|double|halve|restart|switch)\b.*\b(?:medication|medicine|drug|dose|tablet|pill)\b",
        r"\b(?:stop|start|change|increase|decrease|skip|double|halve|restart|switch)\s+(?:the\s+)?(?:medication|medicine|drug|dose|tablet|pill)\b",
        r"\b(?:stop|start|change|increase|decrease|skip|double|halve|restart|switch)\s+(?:my|this|the)?\s*[a-z][a-z-]{2,}\b",
        r"\bhow\s+(?:much|many)\b[^.?!]*\b(?:should|can|may|do)\s+i\b[^.?!]*\b(?:take|inject|use|give|apply|have|swallow)\b",
        r"\bshould\s+i\s+(?:inject|use|give|apply|swallow|have)\b",
        r"\b(?:right|correct|proper|safe|recommended|appropriate|best|ideal)\s+(?:dose|dosage|amount)\b[^.?!]*\b(?:for\s+me|i|my)\b",
    ),
    "TREATMENT_REQUEST": (
        r"\b(?:which|what)\s+(?:treatment|therapy|medicine|drug)\b",
        r"\b(?:recommend|choose|prescribe|suggest)\b.*\b(?:treatment|therapy|medicine|drug|medication)\b",
        r"\bwhat\s+should\s+i\s+(?:do|take)\b",
        r"\bprescribe\b",
        r"\b(?:recommend|suggest)\s+(?:me\s+)?(?:an?|some|something|the\s+best|a\s+good)\b",
        r"\b(?:tell|advise|help)\s+me\s+(?:decide\s+)?(?:whether|if|what)\b",
        r"\bshould\s+i\s+(?:have|get|undergo|go\s+(?:to|for)|see|try|book|cancel)\b",
    ),
    "ADHERENCE_INFERENCE": (
        r"\b(?:am\s+i|is\s+the\s+patient|did\s+i|has\s+the\s+patient)\b.*\b(?:taking|taking\s+it|compliant|adherent|noncompliant|miss(?:ed|ing))\b",
        r"\b(?:adherence|compliance)\b",
    ),
    "URGENT_MEDICAL_DECISION": (
        r"\b(?:chest\s+pain|trouble\s+breathing|difficulty\s+breathing|severe\s+bleeding|unconscious|overdose|fainting|stroke)\b",
    ),
}

# Independent structured semantic layer. This intentionally does not reuse the
# regex policy: it models medical intent from normalized tokens, action/target
# groups, decision frames, and subject context. It is conservative when intent
# is ambiguous, especially when an action is paired with a clinical target.
_SEMANTIC_ACTIONS = {
    "start", "stop", "skip", "change", "increase", "decrease", "double", "halve",
    "reduce", "raise", "lower", "discontinue", "restart", "switch", "take", "prescribe",
    "recommend", "choose", "diagnose", "diagnosis", "treat", "treatment", "therapy",
}
_SEMANTIC_MEDICATION_TARGETS = {
    "medication", "medicine", "drug", "dose", "tablet", "pill", "prescription", "metformin",
}
_SEMANTIC_DIAGNOSIS_TARGETS = {
    "condition", "disease", "diagnosis", "diabetes", "anemia", "infection", "infected", "cancer",
}
_SEMANTIC_DECISION_FRAMES = {
    "should", "recommend", "okay", "safe", "need", "necessary", "choose", "whether", "can", "could", "may",
}
_SEMANTIC_ADHERENCE_TERMS = {"adherence", "compliance", "compliant", "adherent", "noncompliant"}
_SEMANTIC_URGENCY = {"overdose", "unconscious", "stroke", "fainting", "bleeding"}


def normalize_user_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    text = text.replace("\u200b", " ").replace("\u200c", " ").replace("\u200d", " ")
    # Conservative homoglyph/leet normalization makes obfuscated safety bypasses
    # converge with the semantic layer without trusting the input as code.
    text = text.translate(str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t"}))
    return re.sub(r"\s+", " ", text).strip().lower()


def _semantic_assess(text: str) -> SemanticSafetyAssessment:
    tokens = set(_WORD_RE.findall(normalize_user_text(text)))
    actions = tokens & _SEMANTIC_ACTIONS
    medication_targets = tokens & _SEMANTIC_MEDICATION_TARGETS
    diagnosis_targets = tokens & _SEMANTIC_DIAGNOSIS_TARGETS
    decision_frames = tokens & _SEMANTIC_DECISION_FRAMES
    adherence = tokens & _SEMANTIC_ADHERENCE_TERMS
    urgency = tokens & _SEMANTIC_URGENCY

    candidates: list[tuple[str, int]] = []
    if urgency:
        candidates.append(("URGENT_MEDICAL_DECISION", 4 + int(bool(decision_frames))) )
    if actions & {"diagnose", "diagnosis", "treat", "treatment", "therapy"} or (diagnosis_targets and decision_frames):
        candidates.append(("DIAGNOSIS_REQUEST" if diagnosis_targets else "TREATMENT_REQUEST", 3 + int(bool(decision_frames))))
    if actions & {"start", "stop", "skip", "change", "increase", "decrease", "double", "halve", "reduce", "raise", "lower", "discontinue", "restart", "switch", "take"} and medication_targets:
        candidates.append(("MEDICATION_CHANGE", 3 + int(bool(decision_frames))))
    if adherence:
        candidates.append(("ADHERENCE_INFERENCE", 3))
    if not candidates:
        # Ambiguous clinical action language is safer to reject than to pass to a provider.
        if actions and (medication_targets or diagnosis_targets) and not decision_frames:
            return SemanticSafetyAssessment(None, 2, True)
        return SemanticSafetyAssessment(None, 0, False)
    candidates.sort(key=lambda x: x[1], reverse=True)
    intent, score = candidates[0]
    ambiguous = len(candidates) > 1 and candidates[1][1] >= score - 1
    return SemanticSafetyAssessment(intent, score, ambiguous)


# A question ABOUT the record ("when did I start metformin?") is not a request TO change medication. The loose verb patterns
# above cannot tell these apart, so framing is decided first: lookup wording without any decision/imperative wording.
_DECISION_FRAME = re.compile(r"\b(?:should|shall|ought|can\s+i|could\s+i|may\s+i|is\s+it\s+(?:safe|ok|okay|fine|alright)|do\s+i\s+(?:need|have\s+to)|need\s+to|want\s+to|going\s+to|what\s+(?:should|can|do)\s+i|how\s+(?:much|many)\b[^.?!]*\b(?:(?:should|can|may)\s+i|do\s+i\s+(?:take|need|inject|use|give|apply|swallow)))\b")
_IMPERATIVE = re.compile(r"(?:^|[.;,!?:]\s*|\b(?:and|then|also|now|so)\s+)(?:please\s+)?(?:stop|start|skip|double|halve|increase|decrease|reduce|raise|lower|switch|change|restart|discontinue)\b")
_LOOKUP_FRAME = re.compile(
    r"\b(?:when\s+(?:did|was|were)|what\s+date|how\s+many\s+(?:visits?|appointments?|documents?|reports?|results?|records?|tests?|medications?|medicines?)"
    r"|how\s+often|who\s+(?:prescribed|ordered|wrote)|which\s+(?:medicines?|medications?|drugs?|tablets?)\s+(?:were|was|did)"
    r"|according\s+to\s+my\s+records?|recorded|on\s+file|in\s+my\s+records?|listed|documented"
    r"|scheduled|upcoming|next\s+(?:appointment|visit|follow-?up)"
    r"|(?:was|were)\s+(?:\w+\s+){0,4}(?:started|stopped|changed|discontinued|prescribed|increased|decreased))\b")
_DO_I_HAVE = r"\b(?:do|does)\s+(?:i|the patient|this)\s+(?:have|has)\b"


def is_record_lookup(normalized: str) -> bool:
    return bool(_LOOKUP_FRAME.search(normalized)) and not _DECISION_FRAME.search(normalized) and not _IMPERATIVE.search(normalized)


def classify_medical_safety(text: str) -> SafetyDecision:
    normalized = normalize_user_text(text)
    flags: list[str] = []
    for flag, patterns in _PATTERNS.items():
        if any(re.search(pattern, normalized, re.I | re.S) for pattern in patterns):
            flags.append(flag)
    semantic = _semantic_assess(normalized)
    if semantic.intent and semantic.score >= 3:
        flags.append(semantic.intent)
    if semantic.ambiguous:
        flags.append("AMBIGUOUS_MEDICAL_INTENT")
    if is_record_lookup(normalized):
        flags = [f for f in flags if f not in {"MEDICATION_CHANGE", "AMBIGUOUS_MEDICAL_INTENT"}]
        if re.search(r"\b(?:listed|recorded|on\s+file|documented|in\s+my\s+records?)\b", normalized):
            flags = [f for f in flags if f != "ADHERENCE_INFERENCE"]        # "how many medications am I listed as taking" is a count, not adherence
        diag = [p for p in _PATTERNS["DIAGNOSIS_REQUEST"] if re.search(p, normalized, re.I | re.S)]
        if "DIAGNOSIS_REQUEST" in flags and diag == [_DO_I_HAVE] and re.search(r"\b(?:visits?|appointments?|documents?|reports?|results?|records?|follow-?ups?|allerg\w+|medications?|prescriptions?)\b", normalized):
            flags = [f for f in flags if f != "DIAGNOSIS_REQUEST"]
    if "URGENT_MEDICAL_DECISION" in flags:
        flags.insert(0, "URGENT_MEDICAL_DECISION")
    return SafetyDecision(not flags, tuple(dict.fromkeys(flags)))


_DIRECTIVE_TOKENS = {"should", "must", "need", "needs", "ought", "recommend", "recommended", "recommends", "advise", "advised", "advises", "consider", "try", "instead",
                     "suggest", "suggests", "suggested", "please", "can", "could", "might", "would", "shall", "stop", "start", "skip", "double", "halve", "increase",
                     "decrease", "reduce", "raise", "lower", "switch", "change", "restart", "discontinue", "take", "avoid", "continue"}


def validate_medical_output(text: str, *, record_statement: bool = False) -> SafetyDecision:
    """Validate model-produced medical text using the same deterministic policy plus semantic layer.

    Output is stricter than input: a provider must never turn educational context into a personalized
    diagnosis, treatment directive, medication change, or adherence conclusion.
    """
    normalized = normalize_user_text(text)
    tokens = set(_WORD_RE.findall(normalized))
    if record_statement and not (tokens & _DIRECTIVE_TOKENS):
        # A declarative restatement of the record (past tense, no directive/modal wording) is not a diagnosis or an order; whether it is
        # TRUE is decided by grounding. Only adherence conclusions stay blocked here.
        adherence = [f for f in classify_medical_safety(normalized).flags if f == "ADHERENCE_INFERENCE"]
        return SafetyDecision(not adherence, tuple(adherence))
    decision = classify_medical_safety(normalized)
    flags = list(decision.flags)
    directive_frames = {"you", "patient", "your"} & tokens
    action_target = (tokens & _SEMANTIC_ACTIONS) and (tokens & (_SEMANTIC_MEDICATION_TARGETS | _SEMANTIC_DIAGNOSIS_TARGETS))
    if directive_frames and action_target:
        flags.append("UNSAFE_MEDICAL_OUTPUT")
    # Declarative diagnostic certainty is unsafe even without a question frame.
    if (tokens & {"have", "has", "is", "are"}) and tokens & _SEMANTIC_DIAGNOSIS_TARGETS:
        flags.append("UNSAFE_DIAGNOSTIC_OUTPUT")
    return SafetyDecision(not flags, tuple(dict.fromkeys(flags)))
