"""Health measurements (vitals): validation and descriptive series.

Safety boundary: this module performs *plausibility* validation only (rejecting
physically impossible entry/typing errors) and *descriptive* statistics
(count/min/max/mean/latest). It never labels a value normal, high, low, or
concerning and never produces clinical advice. Interpretation belongs to a
clinician.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from statistics import fmean

NOT_CLINICAL_NOTICE = ("Descriptive statistics of values you or your records reported. "
                       "HealthGuard does not interpret measurements; discuss trends with your clinician.")

@dataclass(frozen=True)
class MeasurementSpec:
    kind: str
    label: str
    units: tuple[str, ...]          # first unit is canonical
    min_value: float                # plausibility bounds on the primary value (canonical unit)
    max_value: float
    secondary_label: str | None = None  # e.g. diastolic
    secondary_min: float | None = None
    secondary_max: float | None = None
    contexts: tuple[str, ...] = ()

SPECS: dict[str, MeasurementSpec] = {s.kind: s for s in [
    MeasurementSpec("BLOOD_PRESSURE", "Blood pressure", ("mmHg",), 40, 300, "Diastolic", 20, 200, ("RESTING", "AFTER_EXERCISE", "OTHER")),
    MeasurementSpec("BLOOD_GLUCOSE", "Blood glucose", ("mg/dL", "mmol/L"), 10, 1000, contexts=("FASTING", "POST_MEAL", "RANDOM", "BEDTIME")),
    MeasurementSpec("HEART_RATE", "Heart rate", ("bpm",), 20, 300),
    MeasurementSpec("WEIGHT", "Weight", ("kg", "lb"), 1, 700),
    MeasurementSpec("TEMPERATURE", "Body temperature", ("C", "F"), 25, 110),
    MeasurementSpec("SPO2", "Oxygen saturation", ("%",), 30, 100),
]}
# Unit-specific plausibility overrides where the generic bounds differ by unit.
_UNIT_BOUNDS = {
    ("BLOOD_GLUCOSE", "mmol/L"): (0.5, 55.0),
    ("WEIGHT", "lb"): (2.0, 1500.0),
    ("TEMPERATURE", "C"): (25.0, 45.0),
    ("TEMPERATURE", "F"): (77.0, 113.0),
}

class MeasurementValidationError(ValueError):
    pass

def validate_measurement(kind: str, value: float, value_secondary: float | None, unit: str | None, context: str | None, measured_at: datetime) -> tuple[str, str | None]:
    """Return (unit, context) normalised; raise MeasurementValidationError on invalid input."""
    kind = (kind or "").upper()
    spec = SPECS.get(kind)
    if spec is None:
        raise MeasurementValidationError("Unsupported measurement type.")
    unit = unit or spec.units[0]
    if unit not in spec.units:
        raise MeasurementValidationError(f"Unit must be one of: {', '.join(spec.units)}.")
    lo, hi = _UNIT_BOUNDS.get((kind, unit), (spec.min_value, spec.max_value))
    if not (lo <= value <= hi):
        raise MeasurementValidationError("The value is outside the plausible range for this measurement; please check it.")
    if spec.secondary_label:
        if value_secondary is None:
            raise MeasurementValidationError(f"{spec.secondary_label} value is required for this measurement.")
        if not (spec.secondary_min <= value_secondary <= spec.secondary_max):
            raise MeasurementValidationError(f"{spec.secondary_label} value is outside the plausible range; please check it.")
        if value_secondary > value:
            raise MeasurementValidationError("Diastolic value cannot exceed the systolic value.")
    elif value_secondary is not None:
        raise MeasurementValidationError("A secondary value is not supported for this measurement.")
    if context is not None:
        context = context.upper()
        if context not in spec.contexts:
            raise MeasurementValidationError("Unsupported context for this measurement.")
    ts = measured_at if measured_at.tzinfo else measured_at.replace(tzinfo=timezone.utc)
    if ts > datetime.now(timezone.utc) + timedelta(minutes=5):
        raise MeasurementValidationError("Measurement time cannot be in the future.")
    if ts.year < 1900:
        raise MeasurementValidationError("Measurement time is implausibly old.")
    return unit, context

def spec_catalog() -> list[dict]:
    return [{"kind": s.kind, "label": s.label, "units": list(s.units), "secondary_label": s.secondary_label, "contexts": list(s.contexts)} for s in SPECS.values()]

def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

def downsample(points: list[dict], max_points: int) -> list[dict]:
    """Evenly thin a chronologically sorted series, always keeping first and last."""
    n = len(points)
    if n <= max_points or max_points < 3:
        return points if n <= max_points else [points[0], points[-1]]
    step = (n - 1) / (max_points - 1)
    idx = sorted({round(i * step) for i in range(max_points)} | {0, n - 1})
    return [points[i] for i in idx]

def build_series(rows, kind: str, *, unit: str | None = None, max_points: int = 200) -> dict:
    """Descriptive series for one measurement kind. Points of different units are never mixed."""
    spec = SPECS[kind]
    rows = sorted(rows, key=lambda r: _aware(r.measured_at))
    units = sorted({r.unit for r in rows})
    chosen = unit or (rows[-1].unit if rows else spec.units[0])
    rows = [r for r in rows if r.unit == chosen]
    full = [{
        "id": str(r.id), "t": _aware(r.measured_at).isoformat(), "v": r.value, "v2": r.value_secondary,
        "context": r.context, "provenance": r.provenance.value, "source_type": r.source_type.value,
        "source_document_id": str(r.source_document_id) if r.source_document_id else None,
        "source_fact_id": str(r.source_fact_id) if r.source_fact_id else None,
    } for r in rows]
    stats = None
    if full:
        vals = [p["v"] for p in full]
        stats = {"count": len(vals), "min": min(vals), "max": max(vals), "mean": round(fmean(vals), 2), "latest": full[-1]["v"], "latest_at": full[-1]["t"], "first_at": full[0]["t"]}
        v2 = [p["v2"] for p in full if p["v2"] is not None]
        if v2:
            stats["secondary"] = {"min": min(v2), "max": max(v2), "mean": round(fmean(v2), 2), "latest": full[-1]["v2"]}
    return {
        "kind": kind, "label": spec.label, "unit": chosen, "available_units": units, "secondary_label": spec.secondary_label,
        "points": downsample(full, max_points), "total_points": len(full), "downsampled": len(full) > max_points,
        "stats": stats, "notice": NOT_CLINICAL_NOTICE,
    }
