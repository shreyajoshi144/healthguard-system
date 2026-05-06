import pandas as pd
from utils.logger import get_logger

logger = get_logger(__name__)

ACTION_MAP = {
    "Wait Time":         "Review scheduling system; add triage staff during peak hours; implement SMS queue alerts.",
    "Staff Behavior":    "Schedule mandatory soft-skills training; conduct 1:1 manager reviews; reward top-rated staff.",
    "Cleanliness":       "Increase housekeeping rounds to every 2 hours; add patient cleanliness feedback kiosks.",
    "Communication":     "Introduce patient communication checklist; provide multilingual discharge instructions.",
    "Treatment Quality": "Conduct clinical audit; peer-review flagged cases; update treatment protocols.",
    "Billing":           "Simplify billing statements; provide pre-treatment cost estimates; train billing staff.",
    "Facilities":        "Submit facility maintenance request; inspect equipment quarterly; improve signage.",
    "Appointment":       "Implement online booking system; send 48h reminder SMS; add cancellation waitlist.",
    "General Positive":  "Share positive feedback with staff; use as training examples; acknowledge in team meeting.",
    "Other":             "Log for manual review; assign to department quality officer.",
    "Uncategorized":     "Flag for manual categorization and follow-up.",
}

def map_actions(df: pd.DataFrame) -> pd.DataFrame:
    if "issue_category" in df.columns:
        df["recommended_action"] = df["issue_category"].map(ACTION_MAP).fillna(ACTION_MAP["Other"])
    else:
        df["recommended_action"] = ACTION_MAP["Uncategorized"]
    logger.info("Action mapping complete.")
    return df
