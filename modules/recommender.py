import pandas as pd
from typing import Dict, List
from utils.logger import get_logger

logger = get_logger(__name__)

def generate_recommendations(df: pd.DataFrame) -> Dict[str, List[str]]:
    """Generate dept-specific, data-driven recommendations."""
    recs = {}

    if "department" not in df.columns:
        recs["Overall"] = _generic_recs(df)
        return recs

    for dept in df["department"].dropna().unique():
        dept_df = df[df["department"] == dept]
        dept_recs = []

        rating_col = _find_rating_col(df)
        if rating_col:
            avg = dept_df[rating_col].mean()
            if avg < 3:
                dept_recs.append(f"⚠️ Avg rating is critically low ({avg:.2f}/5) — urgent intervention required.")
            elif avg < 4:
                dept_recs.append(f"📉 Avg rating ({avg:.2f}/5) is below target — review patient experience touchpoints.")
            else:
                dept_recs.append(f"✅ Strong avg rating ({avg:.2f}/5) — maintain current practices.")

        if "sentiment_label" in dept_df.columns:
            neg_pct = (dept_df["sentiment_label"] == "Negative").mean() * 100
            if neg_pct > 30:
                dept_recs.append(f"🔴 {neg_pct:.1f}% negative sentiment — investigate top complaints immediately.")
            elif neg_pct > 15:
                dept_recs.append(f"🟠 {neg_pct:.1f}% negative sentiment — monitor and address recurring issues.")

        if "issue_category" in dept_df.columns:
            top_issue = dept_df["issue_category"].mode()
            if len(top_issue):
                dept_recs.append(f"🏷️ Most frequent issue: **{top_issue[0]}** — prioritize resolution.")

        if "priority_score" in dept_df.columns:
            critical = (dept_df["priority_score"] >= 7).sum()
            if critical > 0:
                dept_recs.append(f"🚨 {critical} critical-priority cases need immediate follow-up.")

        if "word_count" in dept_df.columns:
            avg_words = dept_df["word_count"].mean()
            if avg_words > 50:
                dept_recs.append("📝 Patients are writing detailed feedback — conduct structured qualitative review.")

        if "nps_category" in dept_df.columns:
            promoters  = (dept_df["nps_category"] == "Promoter").mean() * 100
            detractors = (dept_df["nps_category"] == "Detractor").mean() * 100
            nps = promoters - detractors
            if nps < 0:
                dept_recs.append(f"📊 NPS is {nps:+.1f} — more detractors than promoters. Focus on service recovery.")
            else:
                dept_recs.append(f"📊 NPS is {nps:+.1f} — positive loyalty score. Sustain quality standards.")

        recs[dept] = dept_recs if dept_recs else ["No significant issues detected. Continue monitoring."]

    logger.info("Recommendations generated for %d departments.", len(recs))
    return recs

def _generic_recs(df):
    return ["Upload data with 'department' column for dept-specific recommendations."]

def _find_rating_col(df):
    for c in ["overall_rating","rating","satisfaction_score","score"]:
        if c in df.columns and pd.api.types.is_numeric_dtype(df[c]):
            return c
    return None
