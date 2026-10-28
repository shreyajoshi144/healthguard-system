
from __future__ import annotations
import csv
from pathlib import Path

from evaluation.data_contract import FeedbackRecord, encode_secondary_issues, validate_record
from utils.config import CHALLENGE_SET_PATH, CHALLENGE_DATASET_VERSION
from utils.logger import get_logger

logger = get_logger(__name__)

DV = CHALLENGE_DATASET_VERSION


def _rec(text, primary, sentiment, severity, review, challenge_type, secondary=None) -> FeedbackRecord:
    return FeedbackRecord(
        feedback_text=text,
        primary_issue=primary,
        secondary_issues=secondary or [],
        sentiment=sentiment,
        severity=severity,
        requires_human_review=review,
        dataset_version=DV,
        challenge_type=challenge_type,
    )


CHALLENGE_RECORDS: list[FeedbackRecord] = [

    # ── paraphrase: same categories as training, totally different wording ──
    _rec(
        "By the time a physician actually walked in, I had already been sitting there for what felt like half my afternoon.",
        "Wait Time", "Negative", "High", False, "paraphrase",
    ),
    _rec(
        "Every single person on that floor greeted me with a smile and made sure I understood exactly what was happening at each step.",
        "General Positive", "Positive", "Low", False, "paraphrase",
    ),
    _rec(
        "My invoice arrived with line items I'd never agreed to and nobody in billing could explain where the extra charges came from.",
        "Billing", "Negative", "High", False, "paraphrase",
    ),
    _rec(
        "The exam room floor hadn't been mopped in what looked like days, and there were visible stains on the chair.",
        "Cleanliness", "Negative", "High", False, "paraphrase",
    ),
    _rec(
        "Trying to move my visit to another day involved four separate phone transfers and I still ended up with the wrong slot.",
        "Appointment", "Negative", "Medium", False, "paraphrase",
    ),
    _rec(
        "The specialist barely glanced at my chart before scribbling a prescription and moving on to the next room.",
        "Treatment Quality", "Negative", "High", False, "paraphrase",
    ),

    # ── ambiguous: reasonable people could pick two different categories ───
    _rec(
        "It was fine, I guess. Nothing really stood out either way.",
        "Other", "Neutral", "Low", True, "ambiguous",
        secondary=[],
    ),
    _rec(
        "I don't really know how to describe the visit — it wasn't bad, but it wasn't the kind of place I'd want to come back to.",
        "Other", "Neutral", "Medium", True, "ambiguous",
    ),
    _rec(
        "Something about the whole visit felt off, though I couldn't tell you exactly what.",
        "Other", "Negative", "Medium", True, "ambiguous",
    ),
    _rec(
        "The visit was quick, which I suppose is good, but it also felt a little rushed.",
        "Wait Time", "Neutral", "Medium", True, "ambiguous",
        secondary=["Treatment Quality"],
    ),
    _rec(
        "Can't complain, can't really praise it either. Just an appointment.",
        "Uncategorized", "Neutral", "Low", True, "ambiguous",
    ),

    # ── multi_issue: two or more categories clearly present at once ────────
    _rec(
        "The wait was forever, and when a nurse finally called me back she was short with me and never explained what she was doing.",
        "Wait Time", "Negative", "High", False, "multi_issue",
        secondary=["Staff Behavior", "Communication"],
    ),
    _rec(
        "Between the confusing bill and the fact that the front desk staff argued with me about it for twenty minutes, I left more frustrated than when I walked in.",
        "Billing", "Negative", "Critical", True, "multi_issue",
        secondary=["Staff Behavior"],
    ),
    _rec(
        "The room itself was clean enough, but I had to wait so long for a bed that I started to wonder if they'd forgotten about me.",
        "Wait Time", "Negative", "Medium", False, "multi_issue",
        secondary=["Cleanliness"],
    ),
    _rec(
        "Rescheduling was a nightmare, and then when I finally got in, the doctor seemed to have no idea why I was even there.",
        "Appointment", "Negative", "High", True, "multi_issue",
        secondary=["Communication", "Treatment Quality"],
    ),
    _rec(
        "Loved the care I got from my actual doctor, but the parking situation and the outdated waiting room really brought the whole visit down.",
        "General Positive", "Neutral", "Medium", True, "multi_issue",
        secondary=["Facilities"],
    ),

    # ── low_information: short/vague, little signal ────────────────────────
    _rec("Meh.", "Uncategorized", "Neutral", "Low", True, "low_information"),
    _rec("It was okay I suppose.", "Uncategorized", "Neutral", "Low", True, "low_information"),
    _rec("Not bad.", "Uncategorized", "Neutral", "Low", True, "low_information"),
    _rec("Visit happened.", "Uncategorized", "Neutral", "Low", True, "low_information"),
    _rec("N/A", "Uncategorized", "Neutral", "Low", True, "low_information"),

    # ── unfamiliar_wording: right category, slang/atypical phrasing ────────
    _rec(
        "Waiting room felt like actual purgatory, ngl. Been there longer than my last flight delay.",
        "Wait Time", "Negative", "Medium", False, "unfamiliar_wording",
    ),
    _rec(
        "Doc was a total legend honestly, broke it all down for me like I was five and I actually got it for once.",
        "Communication", "Positive", "Low", False, "unfamiliar_wording",
        secondary=["General Positive"],
    ),
    _rec(
        "Billing dept straight up ghosted me for three weeks after I called about the weird charge.",
        "Billing", "Negative", "High", False, "unfamiliar_wording",
    ),
    _rec(
        "Front desk lady had major attitude issues ngl, kinda ruined the whole vibe of the visit.",
        "Staff Behavior", "Negative", "Medium", False, "unfamiliar_wording",
    ),
    _rec(
        "Place was spotless tbh, like hotel-lobby clean, didn't expect that from a clinic.",
        "Cleanliness", "Positive", "Low", False, "unfamiliar_wording",
        secondary=["General Positive"],
    ),

    # ── out_of_domain: not hospital/clinic feedback at all ──────────────────
    _rec(
        "The pizza arrived cold and the delivery driver couldn't find our building for twenty minutes.",
        "Other", "Negative", "Low", True, "out_of_domain",
    ),
    _rec(
        "This laptop's battery life is incredible, easily lasts me a full day of work without charging.",
        "Other", "Positive", "Low", True, "out_of_domain",
    ),
    _rec(
        "Great hiking trail, well marked, though it gets crowded on weekends.",
        "Other", "Positive", "Low", True, "out_of_domain",
    ),
    _rec(
        "Can you send me the quarterly sales report by end of day tomorrow?",
        "Uncategorized", "Neutral", "Low", True, "out_of_domain",
    ),

    # ── edge_case: negation, sarcasm, mixed/contradictory sentiment ────────
    _rec(
        "The staff were anything but rude — genuinely some of the kindest people I've dealt with at any clinic.",
        "Staff Behavior", "Positive", "Low", False, "edge_case",
        secondary=["General Positive"],
    ),
    _rec(
        "Oh great, ANOTHER two hours in the waiting room, exactly what I was hoping for on my day off.",
        "Wait Time", "Negative", "High", True, "edge_case",
    ),
    _rec(
        "I wasn't unimpressed, if that makes sense — it just didn't blow me away either.",
        "Other", "Neutral", "Low", True, "edge_case",
    ),
    _rec(
        "Loved the doctor, hated the wait, would recommend to a friend but only if they've got nowhere to be that day.",
        "Wait Time", "Neutral", "Medium", True, "edge_case",
        secondary=["Treatment Quality", "General Positive"],
    ),
    _rec(
        "Not once did anyone explain what the extra fee was for, and no, 'that's just how it works here' does not count as an explanation.",
        "Billing", "Negative", "High", True, "edge_case",
        secondary=["Communication"],
    ),
    _rec(
        "Five stars if I could give zero.",
        "Uncategorized", "Negative", "Medium", True, "edge_case",
    ),
]


def generate_challenge_set(records: list[FeedbackRecord] = CHALLENGE_RECORDS) -> list[dict]:
    """Validate every record against the data contract, then return them as
    plain dicts (CSV-ready, secondary_issues encoded as a string)."""
    rows = []
    for rec in records:
        errors = validate_record(rec.to_dict())
        if errors:
            raise ValueError(f"Challenge record failed contract validation: {errors}\nRecord: {rec}")
        row = rec.to_dict()
        row["secondary_issues"] = encode_secondary_issues(row["secondary_issues"])
        rows.append(row)
    return rows


def write_challenge_set(path: Path = CHALLENGE_SET_PATH) -> Path:
    rows = generate_challenge_set()
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(rows[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    logger.info("Wrote %d challenge-set rows to %s", len(rows), path)
    return path


if __name__ == "__main__":
    out_path = write_challenge_set()
    print(f"Wrote {len(CHALLENGE_RECORDS)} synthetic challenge/OOD rows to {out_path}")
    from collections import Counter
    types = Counter(r.challenge_type for r in CHALLENGE_RECORDS)
    print("By challenge_type:", dict(types))
