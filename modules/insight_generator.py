"""
modules/insight_generator.py
────────────────────────────
Generates structured AI-style insights from the processed patient survey DataFrame.
No external LLM required — fully deterministic rule-based insight engine.
"""

from __future__ import annotations
import pandas as pd
import numpy as np
from datetime import datetime
from utils.helpers import find_rating_col, find_text_col
from utils.logger import get_logger

logger = get_logger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────

def generate_insights(df: pd.DataFrame) -> dict:
    """
    Analyse the processed survey DataFrame and return a structured insights dict:
    {
        "key_findings": [...],
        "trends":       [...],
        "alerts":       [...],
        "top_department":   str | None,
        "worst_department": str | None,
        "narrative":    str,
        "report_text":  str,
    }
    """
    logger.info("Generating insights from DataFrame with shape %s", df.shape)
    insights: dict = {
        "key_findings":      [],
        "trends":            [],
        "alerts":            [],
        "top_department":    None,
        "worst_department":  None,
        "narrative":         "",
        "report_text":       "",
    }

    try:
        _add_volume_insight(df, insights)
        _add_rating_insights(df, insights)
        _add_sentiment_insights(df, insights)
        _add_department_insights(df, insights)
        _add_issue_insights(df, insights)
        _add_priority_insights(df, insights)
        _add_text_insights(df, insights)
        _add_trend_insights(df, insights)
        insights["narrative"] = _build_narrative(df, insights)
        insights["report_text"] = _build_report(df, insights)
    except Exception as exc:
        logger.exception("Insight generation error: %s", exc)
        insights["key_findings"].append({
            "icon": "⚠️",
            "title": "Partial insight generation",
            "description": f"Some insights could not be computed: {exc}",
        })

    logger.info("Insight generation complete — %d key findings", len(insights["key_findings"]))
    return insights


# ─────────────────────────────────────────────────────────────────────────────
# Private helpers
# ─────────────────────────────────────────────────────────────────────────────

def _add_volume_insight(df: pd.DataFrame, ins: dict) -> None:
    """Total survey response volume."""
    n = len(df)
    icon = "📋"
    if n < 20:
        desc = f"Only {n} responses collected — findings may not be statistically significant. Aim for ≥ 100 responses."
    elif n < 100:
        desc = f"{n} responses collected. Moderate sample — interpret trends with caution."
    else:
        desc = f"{n} total responses analysed. Sample size is robust for reliable conclusions."
    ins["key_findings"].append({"icon": icon, "title": "Survey Volume", "description": desc})


def _add_rating_insights(df: pd.DataFrame, ins: dict) -> None:
    """Overall rating statistics."""
    rating_col = find_rating_col(df)
    if rating_col is None:
        return

    avg = df[rating_col].mean()
    median = df[rating_col].median()
    pct_low = (df[rating_col] <= 2).mean() * 100
    pct_high = (df[rating_col] >= 4).mean() * 100

    # Icon by performance tier
    if avg >= 4.0:
        icon, level = "⭐", "excellent"
    elif avg >= 3.0:
        icon, level = "📊", "moderate"
    else:
        icon, level = "⚠️", "poor"

    ins["key_findings"].append({
        "icon": icon,
        "title": f"Overall Rating — {avg:.2f} / 5 ({level.title()})",
        "description": (
            f"Mean: {avg:.2f} | Median: {median:.1f} | "
            f"Low-rated (≤2): {pct_low:.1f}% | High-rated (≥4): {pct_high:.1f}%."
        ),
    })

    if pct_low > 20:
        ins["alerts"].append({
            "level": "critical",
            "message": f"{pct_low:.1f}% of patients gave a rating of 2 or below — immediate corrective action required.",
        })

    # NPS insight
    if "nps_category" in df.columns:
        promoters = (df["nps_category"] == "Promoter").mean() * 100
        detractors = (df["nps_category"] == "Detractor").mean() * 100
        nps = promoters - detractors
        ins["key_findings"].append({
            "icon": "📈",
            "title": f"Net Promoter Score (NPS) — {nps:+.1f}",
            "description": (
                f"Promoters: {promoters:.1f}% | Detractors: {detractors:.1f}%. "
                + ("NPS above 0 indicates more advocates than critics." if nps > 0
                   else "Negative NPS — more detractors than promoters. Focus on service recovery.")
            ),
        })
        if nps < -10:
            ins["alerts"].append({
                "level": "critical",
                "message": f"NPS is {nps:+.1f} — critically low. Patient loyalty is at risk.",
            })


def _add_sentiment_insights(df: pd.DataFrame, ins: dict) -> None:
    """Sentiment distribution insights."""
    if "sentiment_label" not in df.columns:
        return

    counts = df["sentiment_label"].value_counts(normalize=True) * 100
    pos = counts.get("Positive", 0)
    neu = counts.get("Neutral", 0)
    neg = counts.get("Negative", 0)

    icon = "😊" if pos >= 60 else ("😐" if pos >= 40 else "😞")
    ins["key_findings"].append({
        "icon": icon,
        "title": f"Sentiment — {pos:.1f}% Positive",
        "description": (
            f"Positive: {pos:.1f}% | Neutral: {neu:.1f}% | Negative: {neg:.1f}%. "
            + ("Overall patient sentiment is favourable." if pos >= 60
               else "Significant negative sentiment detected — review complaints urgently." if neg > 30
               else "Mixed sentiment — targeted improvements recommended.")
        ),
    })

    if neg > 30:
        ins["alerts"].append({
            "level": "warning",
            "message": f"{neg:.1f}% of feedback is negative — review top complaint categories immediately.",
        })

    if "vader_compound" in df.columns:
        avg_compound = df["vader_compound"].mean()
        ins["trends"].append({
            "label": "Average VADER Compound Score",
            "value": f"{avg_compound:+.3f} (range −1 to +1; positive = favourable)",
        })


def _add_department_insights(df: pd.DataFrame, ins: dict) -> None:
    """Per-department performance."""
    if "department" not in df.columns:
        return

    rating_col = find_rating_col(df)
    dept_stats: list[dict] = []

    for dept in df["department"].dropna().unique():
        sub = df[df["department"] == dept]
        row: dict = {"department": dept, "n": len(sub)}
        if rating_col:
            row["avg_rating"] = sub[rating_col].mean()
        if "sentiment_label" in sub.columns:
            row["pct_negative"] = (sub["sentiment_label"] == "Negative").mean() * 100
        dept_stats.append(row)

    dept_df = pd.DataFrame(dept_stats)

    if rating_col and "avg_rating" in dept_df.columns:
        best = dept_df.loc[dept_df["avg_rating"].idxmax()]
        worst = dept_df.loc[dept_df["avg_rating"].idxmin()]
        ins["top_department"] = f"{best['department']} (avg {best['avg_rating']:.2f}/5)"
        ins["worst_department"] = f"{worst['department']} (avg {worst['avg_rating']:.2f}/5)"

        ins["key_findings"].append({
            "icon": "🏥",
            "title": "Department Performance Spread",
            "description": (
                f"Best performer: {best['department']} ({best['avg_rating']:.2f}/5) | "
                f"Needs improvement: {worst['department']} ({worst['avg_rating']:.2f}/5) | "
                f"Rating gap: {best['avg_rating'] - worst['avg_rating']:.2f} points."
            ),
        })

        if worst["avg_rating"] < 2.5:
            ins["alerts"].append({
                "level": "critical",
                "message": f"Department '{worst['department']}' has a critically low avg rating of {worst['avg_rating']:.2f}/5.",
            })

    # Departments with high negative sentiment
    if "pct_negative" in dept_df.columns:
        high_neg = dept_df[dept_df["pct_negative"] > 35]
        for _, row in high_neg.iterrows():
            ins["alerts"].append({
                "level": "warning",
                "message": (
                    f"'{row['department']}' has {row['pct_negative']:.1f}% negative sentiment — "
                    "review staffing and patient communication."
                ),
            })


def _add_issue_insights(df: pd.DataFrame, ins: dict) -> None:
    """Top issues and categorisation insights."""
    if "issue_category" not in df.columns:
        return

    cat_counts = df["issue_category"].value_counts()
    top_issue = cat_counts.index[0] if not cat_counts.empty else "Unknown"
    top_pct = cat_counts.iloc[0] / len(df) * 100 if not cat_counts.empty else 0

    ins["key_findings"].append({
        "icon": "🏷️",
        "title": f"Top Complaint Category — {top_issue}",
        "description": (
            f"'{top_issue}' is the most common issue ({top_pct:.1f}% of responses). "
            f"Total distinct categories: {df['issue_category'].nunique()}."
        ),
    })

    # List top 3 issues as a trend
    for i, (cat, cnt) in enumerate(cat_counts.head(3).items()):
        ins["trends"].append({
            "label": f"#{i+1} Most Reported Issue",
            "value": f"{cat} — {cnt} responses ({cnt/len(df)*100:.1f}%)",
        })


def _add_priority_insights(df: pd.DataFrame, ins: dict) -> None:
    """Priority score summary."""
    if "priority_score" not in df.columns:
        return

    critical_n = (df["priority_score"] >= 7).sum()
    high_n = ((df["priority_score"] >= 5) & (df["priority_score"] < 7)).sum()
    avg_ps = df["priority_score"].mean()

    ins["key_findings"].append({
        "icon": "⚡",
        "title": f"Priority Scoring — {critical_n} Critical Cases",
        "description": (
            f"Critical (≥7): {critical_n} | High (5–7): {high_n} | "
            f"Average priority score: {avg_ps:.2f}/10. "
            + ("Urgent escalation required for critical cases." if critical_n > 0 else "No critical cases detected.")
        ),
    })

    if critical_n > 0:
        ins["alerts"].append({
            "level": "critical",
            "message": f"{critical_n} cases scored 7+ on priority — immediate follow-up recommended.",
        })


def _add_text_insights(df: pd.DataFrame, ins: dict) -> None:
    """Feedback richness / text quality insights."""
    if "word_count" not in df.columns:
        return

    avg_wc = df["word_count"].mean()
    has_feedback_pct = df.get("has_feedback", pd.Series([1] * len(df))).mean() * 100

    ins["key_findings"].append({
        "icon": "📝",
        "title": "Feedback Richness",
        "description": (
            f"Average feedback length: {avg_wc:.1f} words. "
            f"{has_feedback_pct:.1f}% of responses contain substantive text feedback. "
            + ("Patients are providing detailed, actionable comments." if avg_wc > 30
               else "Short average feedback length — consider redesigning open-text prompts.")
        ),
    })


def _add_trend_insights(df: pd.DataFrame, ins: dict) -> None:
    """Time-based trends."""
    date_cols = df.select_dtypes(include="datetime64[ns]").columns.tolist()
    if not date_cols:
        return

    dc = date_cols[0]
    rating_col = find_rating_col(df)

    if rating_col:
        monthly = df.groupby(df[dc].dt.to_period("M"))[rating_col].mean()
        if len(monthly) >= 2:
            first_period = monthly.iloc[0]
            last_period = monthly.iloc[-1]
            delta = last_period - first_period
            direction = "improved" if delta > 0 else "declined"
            ins["trends"].append({
                "label": "Rating Trend (first vs latest month)",
                "value": f"{first_period:.2f} → {last_period:.2f} ({direction} by {abs(delta):.2f} points)",
            })
            if delta < -0.5:
                ins["alerts"].append({
                    "level": "warning",
                    "message": f"Avg rating has declined by {abs(delta):.2f} points over the survey period.",
                })

    # Peak complaint period
    if "issue_category" in df.columns:
        monthly_complaints = df[df["issue_category"] != "General Positive"].groupby(
            df[dc].dt.to_period("M")
        ).size()
        if not monthly_complaints.empty:
            peak = monthly_complaints.idxmax()
            ins["trends"].append({
                "label": "Peak Complaint Month",
                "value": str(peak),
            })


def _build_narrative(df: pd.DataFrame, ins: dict) -> str:
    """Compose a human-readable executive narrative."""
    lines = [
        f"PATIENT SATISFACTION ANALYSIS — Executive Summary",
        f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
        f"{'─' * 60}",
        "",
    ]

    n = len(df)
    lines.append(f"This report covers {n} patient survey responses.")

    rating_col = find_rating_col(df)
    if rating_col:
        avg = df[rating_col].mean()
        lines.append(
            f"The overall average satisfaction rating is {avg:.2f} out of 5, placing the facility in the "
            + ("top tier (≥4)." if avg >= 4 else "moderate tier (3–4)." if avg >= 3 else "below-average tier (<3).")
        )

    if "sentiment_label" in df.columns:
        pos = (df["sentiment_label"] == "Positive").mean() * 100
        neg = (df["sentiment_label"] == "Negative").mean() * 100
        lines.append(
            f"Sentiment analysis reveals {pos:.1f}% positive and {neg:.1f}% negative responses."
        )

    if ins.get("top_department"):
        lines.append(f"Best-performing department: {ins['top_department']}.")
    if ins.get("worst_department"):
        lines.append(f"Department requiring attention: {ins['worst_department']}.")

    if "priority_score" in df.columns:
        crit = (df["priority_score"] >= 7).sum()
        lines.append(
            f"Priority scoring identified {crit} critical-priority cases that require immediate follow-up."
        )

    lines += [
        "",
        "Key alerts raised in this report:",
    ]
    for alert in ins.get("alerts", []):
        lines.append(f"  • [{alert['level'].upper()}] {alert['message']}")

    if not ins.get("alerts"):
        lines.append("  • No critical alerts at this time.")

    return "\n".join(lines)


def _build_report(df: pd.DataFrame, ins: dict) -> str:
    """Build a plain-text downloadable report."""
    sections = [
        ins["narrative"],
        "",
        "KEY FINDINGS",
        "─" * 40,
    ]
    for f in ins.get("key_findings", []):
        sections.append(f"[{f['icon']}] {f['title']}")
        sections.append(f"    {f['description']}")
        sections.append("")

    sections += ["TRENDS", "─" * 40]
    for t in ins.get("trends", []):
        sections.append(f"  {t['label']}: {t['value']}")

    sections += ["", "ALERTS", "─" * 40]
    for a in ins.get("alerts", []):
        sections.append(f"  [{a['level'].upper()}] {a['message']}")

    return "\n".join(sections)
