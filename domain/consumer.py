"""Consumer health-overview builders (dashboard, summaries, doctor questions,
unified timeline, personal health graph, medication tracker, family overview).

Design rules
* DETERMINISTIC and RECORD-DERIVED. No generative model is involved, so nothing
  can be hallucinated: every line is rendered from a stored row and carries an
  evidence reference (source type/id, document id, page when known) plus its
  provenance. Absent data is reported as absent, never filled in.
* SCOPE-AWARE. Every builder takes `scopes`, the set of AccessScope names the
  caller holds for this patient; a section whose scope is missing is reported as
  unavailable and its rows are never loaded.
* NO CLINICAL JUDGEMENT. Nothing is labelled normal/abnormal/urgent; "overdue"
  is purely `due_date < today` on a user-confirmed or document-documented date.
"""
from __future__ import annotations
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from typing import Any
from uuid import UUID
from sqlalchemy import select, func
from sqlalchemy.orm import Session
from db.models import (Document, DocumentStatus, HealthAction, HealthActionStatus, HealthActionSource, HealthEvent, Handoff, HandoffStatus,
                       HealthFact, Investigation, Measurement, Medication, MedicationStatus, PatientProfile, DatePrecision, Provenance)
from domain.longitudinal import _fact_item, _live_facts, _fact_concept
from domain.measurements import build_series, SPECS

ALL_SCOPES = {"PROFILE", "DOCUMENTS", "MEDICATIONS", "INVESTIGATIONS", "TIMELINE", "HANDOFFS", "HEALTH_ACTIONS", "MEASUREMENTS"}
NOTICE = ("This view is assembled directly from your records. HealthGuard does not diagnose, interpret results or recommend treatment; "
          "every line shows where it came from.")
UNCONFIRMED = {Provenance.SYSTEM_DERIVED.value}
LIVE_DOC_EXCLUDED = (DocumentStatus.DELETED, DocumentStatus.DELETION_PENDING)


def awaiting_confirmation(a) -> bool:
    """A document-derived suggestion the user has not confirmed yet. Never presented as a confirmed commitment."""
    return a.source == HealthActionSource.DOCUMENT_DERIVED and a.confirmed_at is None and a.status == HealthActionStatus.PLANNED


def _iso(d):
    return d.isoformat() if isinstance(d, (date, datetime)) else d


def ref(source_type: str, source_id, *, document_id=None, page=None, provenance=None, label=None) -> dict:
    return {"source_type": source_type, "source_id": str(source_id) if source_id else None,
            "document_id": str(document_id) if document_id else None, "page": page,
            "provenance": getattr(provenance, "value", provenance), "label": label}


# ------------------------------------------------------------------ loading
class Ctx:
    """Rows for one patient, loaded only for scopes the caller holds."""
    def __init__(self, db: Session, patient: PatientProfile, scopes: set[str]):
        self.db, self.patient, self.scopes = db, patient, set(scopes)
        pid = patient.id
        has = self.scopes.__contains__
        self.docs = list(db.scalars(select(Document).where(Document.patient_id == pid, Document.status.not_in(LIVE_DOC_EXCLUDED)).order_by(Document.created_at.desc())).all()) if has("DOCUMENTS") else []
        self.doc_ids = {d.id for d in self.docs}
        self.facts = _live_facts(db, pid) if has("DOCUMENTS") else []
        self.events = list(db.scalars(select(HealthEvent).where(HealthEvent.patient_id == pid)).all()) if has("TIMELINE") else []
        self.meds = list(db.scalars(select(Medication).where(Medication.patient_id == pid)).all()) if has("MEDICATIONS") else []
        self.invs = list(db.scalars(select(Investigation).where(Investigation.patient_id == pid)).all()) if has("INVESTIGATIONS") else []
        self.actions = list(db.scalars(select(HealthAction).where(HealthAction.patient_id == pid)).all()) if has("HEALTH_ACTIONS") else []
        self.handoffs = list(db.scalars(select(Handoff).where(Handoff.patient_id == pid, Handoff.status == HandoffStatus.FINAL)).all()) if has("HANDOFFS") else []
        self.measurements = list(db.scalars(select(Measurement).where(Measurement.patient_id == pid)).all()) if has("MEASUREMENTS") else []

    def unavailable(self) -> list[str]:
        names = {"DOCUMENTS": "Documents and extracted facts", "MEDICATIONS": "Medications", "INVESTIGATIONS": "Investigations and results",
                 "TIMELINE": "Health events", "HEALTH_ACTIONS": "Follow-ups", "MEASUREMENTS": "Vitals", "HANDOFFS": "Doctor handoffs"}
        return [v for k, v in names.items() if k not in self.scopes]

    def live_doc(self, doc_id) -> bool:
        return doc_id is not None and doc_id in self.doc_ids


def _event_ref(e: HealthEvent, ctx: Ctx) -> list[dict]:
    refs = [ref("HEALTH_EVENT", e.id, provenance=e.provenance, label=e.title)]
    if e.source_type.value == "DOCUMENT" and ctx.live_doc(e.source_id):
        refs.append(ref("DOCUMENT", e.source_id, document_id=e.source_id, provenance=Provenance.DOCUMENT_VERIFIED))
    return refs


def _med_refs(m: Medication, ctx: Ctx) -> list[dict]:
    refs = [ref("MEDICATION", m.id, provenance=m.provenance, label=m.medicine_name)]
    if ctx.live_doc(m.source_id):
        refs.append(ref("DOCUMENT", m.source_id, document_id=m.source_id, provenance=Provenance.DOCUMENT_VERIFIED))
    return refs


def _conflicts(ctx: Ctx) -> list[dict]:
    """Documented disagreements. Never resolved here; both sides are shown."""
    out: list[dict] = []
    by_concept: dict[tuple[str, str], list[HealthFact]] = defaultdict(list)
    for f in ctx.facts:
        by_concept[(f.fact_type, _fact_concept(f))].append(f)
    for (ftype, concept), group in by_concept.items():
        values = {(f.normalized_value or f.value).strip().lower() for f in group}
        if len(group) > 1 and len(values) > 1:
            out.append({"kind": "FACT_DISAGREEMENT", "title": f"Records disagree about “{concept}”",
                        "detail": "; ".join(sorted({f.value for f in group}))[:300],
                        "refs": [ref("HEALTH_FACT", f.id, document_id=f.document_id, page=f.source_page, provenance=f.provenance, label=f.value[:80]) for f in group[:6]]})
    by_name: dict[str, list[Medication]] = defaultdict(list)
    for m in ctx.meds:
        by_name[m.medicine_name.strip().lower()].append(m)
    today = date.today()
    for name, group in by_name.items():
        active = [m for m in group if m.status == MedicationStatus.ACTIVE]
        doses = {(m.dose or "").strip().lower() for m in active}
        if len(active) > 1 and len(doses) > 1:
            out.append({"kind": "MEDICATION_DOSE_DISAGREEMENT", "title": f"More than one active record for {group[0].medicine_name} with different doses",
                        "detail": "; ".join(sorted({m.dose or "dose not recorded" for m in active})), "refs": [r for m in active for r in _med_refs(m, ctx)[:1]]})
    for m in ctx.meds:
        stops = [e for e in m.events if e.event_type.value == "STOPPED"]
        if m.status == MedicationStatus.ACTIVE and stops:
            out.append({"kind": "MEDICATION_STATUS_MISMATCH", "title": f"{m.medicine_name} is marked active but a stop was also recorded",
                        "detail": "Confirm with your clinician which is current.", "refs": _med_refs(m, ctx)})
        elif m.status == MedicationStatus.ACTIVE and m.end_date and m.end_date < today:
            out.append({"kind": "MEDICATION_STATUS_MISMATCH", "title": f"{m.medicine_name} is marked active but has an end date in the past",
                        "detail": f"End date recorded: {m.end_date.isoformat()}", "refs": _med_refs(m, ctx)})
    return out


def _line(text, *, refs, provenance=None, date_=None, precision=None, kind=None, extra=None) -> dict:
    return {"text": text, "kind": kind, "refs": refs, "provenance": getattr(provenance, "value", provenance),
            "unconfirmed": getattr(provenance, "value", provenance) in UNCONFIRMED,
            "date": _iso(date_), "date_precision": getattr(precision, "value", precision) if date_ else (DatePrecision.UNKNOWN.value if precision is None else getattr(precision, "value", precision)), **(extra or {})}


# ------------------------------------------------------------------ summaries
def build_summary(ctx: Ctx, mode: str) -> dict:
    short = mode == "short"
    cap = 3 if short else 25
    sections: list[dict] = []
    p = ctx.patient

    if "PROFILE" in ctx.scopes:
        bits = [f"{p.first_name} {p.last_name}"] + ([f"born {p.date_of_birth.isoformat()}"] if p.date_of_birth else []) + ([p.gender] if p.gender else []) + ([f"blood group {p.blood_group}"] if p.blood_group else [])
        sections.append({"key": "profile", "title": "Profile", "items": [_line(", ".join(bits), refs=[ref("PROFILE", p.id, provenance=Provenance.USER_REPORTED)], provenance=Provenance.USER_REPORTED, kind="PROFILE")], "empty_message": None})

    if "DOCUMENTS" in ctx.scopes:
        conds = sorted([f for f in ctx.facts if f.fact_type.upper().startswith(("DIAGNOSIS", "CONDITION"))], key=lambda f: (f.temporal_date is None, -(f.temporal_date.toordinal() if f.temporal_date else 0)))
        sections.append({"key": "conditions", "title": "Conditions documented in your records", "items": [
            _line(f.value, refs=[ref("HEALTH_FACT", f.id, document_id=f.document_id, page=f.source_page, provenance=f.provenance, label=f.value[:80])], provenance=f.provenance, date_=f.temporal_date, precision=f.temporal_precision, kind="CONDITION",
                  extra={"quote": f.source_text[:240] if not short else None}) for f in conds[:cap]],
            "empty_message": "No diagnoses or conditions are documented in your uploaded records.", "total": len(conds)})

    if "MEDICATIONS" in ctx.scopes:
        active = [m for m in ctx.meds if m.status == MedicationStatus.ACTIVE]
        active.sort(key=lambda m: m.medicine_name.lower())
        sections.append({"key": "medications", "title": "Current medications (as recorded)", "items": [
            _line(" ".join(x for x in [m.medicine_name, m.dose, m.frequency, m.route] if x), refs=_med_refs(m, ctx), provenance=m.provenance, date_=m.start_date, precision=DatePrecision.EXACT if m.start_date else None, kind="MEDICATION") for m in active[:cap]],
            "empty_message": "No active medications are recorded.", "total": len(active)})
        if not short:
            changes = sorted([(e, m) for m in ctx.meds for e in m.events], key=lambda t: (t[0].event_date is None, -(t[0].event_date.toordinal() if t[0].event_date else 0)))
            sections.append({"key": "medication_changes", "title": "Medication changes", "items": [
                _line(f"{m.medicine_name}: {e.event_type.value.lower()}" + (f" ({e.dose})" if e.dose else ""), refs=_med_refs(m, ctx), provenance=e.provenance, date_=e.event_date, precision=DatePrecision.EXACT if e.event_date else None, kind="MEDICATION_EVENT") for e, m in changes[:cap]],
                "empty_message": "No medication starts, changes or stops are recorded.", "total": len(changes)})

    if "INVESTIGATIONS" in ctx.scopes:
        rows = sorted(ctx.invs, key=lambda i: (i.performed_date or i.ordered_date) is None, reverse=False)
        rows.sort(key=lambda i: (i.performed_date or i.ordered_date or date.min), reverse=True)
        items = []
        for i in rows[:cap]:
            res = sorted(i.results, key=lambda r: r.result_date or date.min, reverse=True)
            txt = f"{i.name} — {i.status.value.lower()}" + (f"; result as reported: {res[0].result_summary[:160]}" if res else "; no result recorded")
            refs = [ref("INVESTIGATION", i.id, provenance=i.provenance, label=i.name)] + [ref("INVESTIGATION_RESULT", r.id, document_id=r.source_id if ctx.live_doc(r.source_id) else None, provenance=i.provenance) for r in res[:2]]
            items.append(_line(txt, refs=refs, provenance=i.provenance, date_=i.performed_date or i.ordered_date, precision=DatePrecision.EXACT if (i.performed_date or i.ordered_date) else None, kind="INVESTIGATION"))
        sections.append({"key": "investigations", "title": "Investigations and results (as reported)", "items": items, "empty_message": "No investigations are recorded.", "total": len(rows)})

    if "TIMELINE" in ctx.scopes:
        ev = sorted(ctx.events, key=lambda e: (e.event_date or date.min), reverse=True)
        sections.append({"key": "events", "title": "Recent health events", "items": [
            _line(e.title + (f" — {e.description[:160]}" if (e.description and not short) else ""), refs=_event_ref(e, ctx), provenance=e.provenance, date_=e.event_date, precision=e.date_precision, kind="EVENT") for e in ev[:cap]],
            "empty_message": "No health events are recorded.", "total": len(ev)})

    if "MEASUREMENTS" in ctx.scopes:
        items = []
        for kind in SPECS:
            rows = [m for m in ctx.measurements if m.kind == kind]
            if not rows: continue
            last = max(rows, key=lambda m: m.measured_at)
            val = f"{last.value:g}/{last.value_secondary:g}" if last.value_secondary is not None else f"{last.value:g}"
            items.append(_line(f"{SPECS[kind].label}: latest {val} {last.unit} ({len(rows)} reading{'s' if len(rows) != 1 else ''} recorded)", refs=[ref("MEASUREMENT", last.id, document_id=last.source_document_id if ctx.live_doc(last.source_document_id) else None, provenance=last.provenance)], provenance=last.provenance, date_=last.measured_at.date(), precision=DatePrecision.EXACT, kind="VITAL"))
        sections.append({"key": "vitals", "title": "Vitals you or your records reported", "items": items[:cap], "empty_message": "No vitals are recorded.", "total": len(items)})

    if "HEALTH_ACTIONS" in ctx.scopes:
        open_ = [a for a in ctx.actions if a.status in (HealthActionStatus.PLANNED, HealthActionStatus.SCHEDULED)]
        open_.sort(key=lambda a: (a.due_date is None, a.due_date or date.max))
        items = []
        for a in open_[:cap]:
            confirmed = not awaiting_confirmation(a)
            refs = [ref("HEALTH_ACTION", a.id, provenance=a.provenance, label=a.title)] + ([ref("DOCUMENT", a.source_document_id, document_id=a.source_document_id, provenance=Provenance.DOCUMENT_VERIFIED)] if ctx.live_doc(a.source_document_id) else [])
            items.append(_line(a.title + ("" if confirmed else " (suggested from a document — not yet confirmed by you)"), refs=refs, provenance=a.provenance, date_=a.due_date, precision=a.temporal_precision, kind="FOLLOW_UP", extra={"confirmed": confirmed}))
        sections.append({"key": "follow_ups", "title": "Open follow-ups", "items": items, "empty_message": "No open follow-ups.", "total": len(open_)})

    conflicts = _conflicts(ctx)
    sections.append({"key": "conflicts", "title": "Conflicts and uncertainty", "items": [
        _line(c["title"], refs=c["refs"], kind=c["kind"], extra={"detail": c["detail"]}) for c in conflicts[: (3 if short else 25)]],
        "empty_message": "No disagreements between your records were detected.", "total": len(conflicts)})

    unconfirmed = sum(1 for s in sections for it in s["items"] if it["unconfirmed"])
    limitations = [f"{n} is not included because it is outside the access granted to you." for n in ctx.unavailable()]
    if ctx.docs and any(d.status != DocumentStatus.COMPLETED for d in ctx.docs):
        limitations.append("Some uploaded documents are still processing or failed, so this summary may be incomplete.")
    if unconfirmed:
        limitations.append(f"{unconfirmed} item(s) are system-derived and unconfirmed.")
    return {"mode": mode, "generated_at": datetime.now(timezone.utc).isoformat(), "generation": "RECORD_DERIVED_NO_GENERATIVE_MODEL",
            "sections": sections, "limitations": limitations, "notice": NOTICE}


# ------------------------------------------------------------------ doctor questions
def build_doctor_questions(ctx: Ctx) -> dict:
    summary = build_summary(ctx, "detailed")
    by = {s["key"]: s for s in summary["sections"]}
    documented = [it for k in ("conditions", "medications", "medication_changes", "investigations") for it in by.get(k, {}).get("items", [])][:30]
    uncertain: list[dict] = []
    questions: list[dict] = []

    def q(text, basis, category):
        if len(questions) < 15:
            questions.append({"question": text, "basis": basis, "category": category})

    conflicts = _conflicts(ctx)
    for c in conflicts:
        uncertain.append({"kind": c["kind"], "text": c["title"], "detail": c["detail"], "refs": c["refs"]})
        q(f"My records show different information here: {c['title'].lower()} ({c['detail'][:140]}). Which is correct and current?", c["refs"], "CONFLICT")
    for m in ctx.meds:
        if m.provenance.value in UNCONFIRMED:
            uncertain.append({"kind": "UNCONFIRMED_MEDICATION", "text": f"{m.medicine_name} is system-derived, not confirmed", "detail": None, "refs": _med_refs(m, ctx)})
            q(f"{m.medicine_name} appears in my records but was not confirmed by me or a document. Am I meant to be taking it?", _med_refs(m, ctx), "UNCONFIRMED")
    for m in sorted([m for m in ctx.meds if m.status == MedicationStatus.ACTIVE], key=lambda m: m.medicine_name.lower())[:5]:
        desc = " ".join(x for x in [m.medicine_name, m.dose, m.frequency] if x)
        q(f"My records list {desc}. Is this still correct, and is there anything about how I take it that I should confirm with you?", _med_refs(m, ctx), "MEDICATION")
    for m in ctx.meds:
        for e in m.events:
            if e.event_type.value == "STOPPED":
                q(f"My records show {m.medicine_name} was stopped" + (f" on {e.event_date.isoformat()}" if e.event_date else " (date not recorded)") + ". Can you confirm that and tell me what to keep in mind?", _med_refs(m, ctx), "MEDICATION")
                break
    for i in ctx.invs:
        if i.results:
            r = sorted(i.results, key=lambda r: r.result_date or date.min, reverse=True)[0]
            q(f"I have a result recorded for {i.name}. Can you explain what it means for me and whether any next steps are needed?", [ref("INVESTIGATION_RESULT", r.id, provenance=i.provenance, label=i.name)], "RESULT")
        elif i.status.value in ("ORDERED", "SCHEDULED"):
            q(f"{i.name} is recorded as {i.status.value.lower()} but I have no result on file. Where does this stand?", [ref("INVESTIGATION", i.id, provenance=i.provenance, label=i.name)], "RESULT")
    for a in ctx.actions:
        if a.status in (HealthActionStatus.PLANNED, HealthActionStatus.SCHEDULED):
            when = f"due {a.due_date.isoformat()}" if a.due_date else "with no date documented"
            q(f"My records include the follow-up “{a.title[:100]}” ({when}). Should it still go ahead, and when?", [ref("HEALTH_ACTION", a.id, provenance=a.provenance, label=a.title)], "FOLLOW_UP")
            if not a.due_date:
                uncertain.append({"kind": "UNDATED_FOLLOW_UP", "text": f"Follow-up “{a.title[:80]}” has no documented date", "detail": None, "refs": [ref("HEALTH_ACTION", a.id, provenance=a.provenance)]})
    for e in ctx.events:
        if e.date_precision in (DatePrecision.UNKNOWN, DatePrecision.APPROXIMATE, DatePrecision.YEAR):
            uncertain.append({"kind": "IMPRECISE_DATE", "text": f"“{e.title}” has an {e.date_precision.value.lower()} date", "detail": None, "refs": _event_ref(e, ctx)})
    for kind in SPECS:
        rows = [m for m in ctx.measurements if m.kind == kind]
        if len(rows) >= 2:
            lo, hi = min(r.measured_at for r in rows), max(r.measured_at for r in rows)
            q(f"I recorded {len(rows)} {SPECS[kind].label.lower()} readings between {lo.date().isoformat()} and {hi.date().isoformat()}. Would you like to review them?", [ref("MEASUREMENT", rows[0].id, provenance=rows[0].provenance, label=SPECS[kind].label)], "VITALS")
    if any(d.status in (DocumentStatus.FAILED, DocumentStatus.PROCESSING, DocumentStatus.QUEUED) for d in ctx.docs):
        uncertain.append({"kind": "INCOMPLETE_DOCUMENTS", "text": "Some uploaded documents are not fully processed, so the record may be incomplete.", "detail": None, "refs": []})
    return {"generated_at": summary["generated_at"], "generation": "RECORD_DERIVED_NO_GENERATIVE_MODEL",
            "documented_facts": documented, "uncertainty": uncertain[:30], "suggested_questions": questions,
            "empty_message": None if questions else "There is not enough documented information to suggest specific questions yet. Add documents, medications or follow-ups first.",
            "limitations": summary["limitations"], "notice": "Questions are prompts to discuss with your clinician; they are not medical advice and do not imply a finding."}


# ------------------------------------------------------------------ medication tracker
def build_medication_tracker(ctx: Ctx) -> dict:
    conflicts = _conflicts(ctx)
    conflict_by_med: dict[str, list[dict]] = defaultdict(list)
    for c in conflicts:
        for r in c["refs"]:
            if r["source_type"] == "MEDICATION":
                conflict_by_med[r["source_id"]].append({"kind": c["kind"], "title": c["title"]})
    rows = []
    for m in ctx.meds:
        evs = sorted(m.events, key=lambda e: (e.event_date is None, e.event_date or date.max, e.created_at))
        rows.append({
            "id": str(m.id), "name": m.medicine_name, "dose": m.dose, "frequency": m.frequency, "route": m.route, "status": m.status.value,
            "start_date": _iso(m.start_date), "end_date": _iso(m.end_date), "provenance": m.provenance.value,
            "confirmation": "UNCONFIRMED_INFERENCE" if m.provenance.value in UNCONFIRMED else "RECORDED",
            "evidence": _med_refs(m, ctx), "conflicts": conflict_by_med.get(str(m.id), []),
            "events": [{"id": str(e.id), "type": e.event_type.value, "date": _iso(e.event_date), "date_precision": "EXACT" if e.event_date else "UNKNOWN", "dose": e.dose, "frequency": e.frequency, "notes": e.notes, "provenance": e.provenance.value, "unconfirmed": e.provenance.value in UNCONFIRMED} for e in evs],
        })
    rows.sort(key=lambda r: (r["status"] != "ACTIVE", r["name"].lower()))
    return {"current": [r for r in rows if r["status"] == "ACTIVE"], "history": [r for r in rows if r["status"] != "ACTIVE"], "conflict_count": len(conflicts),
            "notice": "Medication records are shown as recorded. HealthGuard does not recommend starting, changing or stopping any medicine. Items marked unconfirmed were inferred by the system and are not confirmed medication history."}


# ------------------------------------------------------------------ unified timeline
def build_unified_timeline(db: Session, ctx: Ctx, *, types: set[str] | None = None, limit: int = 200, offset: int = 0) -> dict:
    from domain.longitudinal import build_longitudinal_timeline
    base = build_longitudinal_timeline(db, ctx.patient.id, allowed_scopes=ctx.scopes & {"DOCUMENTS", "TIMELINE", "MEDICATIONS", "INVESTIGATIONS"})
    items = [dict(i) for i in base]
    live = {str(i) for i in ctx.doc_ids}
    for it in items:
        ev = it.get("evidence") or {}
        cand = ev.get("document_id") or it.get("document_id") or (ev.get("source_id") if ev.get("source_type") in (None, "DOCUMENT") else None) or (it.get("source_id") if it["item_type"] in ("MEDICATION", "MEDICATION_EVENT", "INVESTIGATION_RESULT") else None)
        it["document_id"] = str(cand) if cand and str(cand) in live else None      # only live documents are linkable
        if it["item_type"] == "HEALTH_FACT":
            it["title"] = it.get("value") or it.get("title")
        it["provenance"] = it.get("provenance") or it.get("provenance_label")
    for d in ctx.docs:
        items.append({"item_type": "DOCUMENT", "id": str(d.id), "date": d.created_at.date(), "date_precision": "EXACT", "title": d.filename, "details": {"document_type": d.document_type.value, "status": d.status.value, "date_meaning": "upload date, not the clinical date"},
                      "provenance": None, "document_id": str(d.id), "evidence": {"document_id": str(d.id)}})
    for a in ctx.actions:
        items.append({"item_type": "HEALTH_ACTION", "id": str(a.id), "date": a.due_date, "date_precision": a.temporal_precision.value, "title": a.title, "details": {"status": a.status.value, "source": a.source.value, "confirmed": not (awaiting_confirmation(a))},
                      "provenance": a.provenance.value, "document_id": str(a.source_document_id) if ctx.live_doc(a.source_document_id) else None, "evidence": {"document_id": str(a.source_document_id) if ctx.live_doc(a.source_document_id) else None}})
    for h in ctx.handoffs:
        items.append({"item_type": "HANDOFF", "id": str(h.id), "date": h.updated_at.date(), "date_precision": "EXACT", "title": h.title, "details": {"status": h.status.value, "date_meaning": "finalized date"}, "provenance": None, "document_id": None, "evidence": {"evidence_ref_count": len(h.evidence_refs or [])}})
    for kind in SPECS:
        rows = sorted([m for m in ctx.measurements if m.kind == kind], key=lambda m: m.measured_at, reverse=True)[:20]
        for m in rows:
            val = f"{m.value:g}/{m.value_secondary:g}" if m.value_secondary is not None else f"{m.value:g}"
            items.append({"item_type": "MEASUREMENT", "id": str(m.id), "date": m.measured_at.date(), "date_precision": "EXACT", "title": f"{SPECS[kind].label}: {val} {m.unit}", "details": {"context": m.context}, "provenance": m.provenance.value, "document_id": str(m.source_document_id) if ctx.live_doc(m.source_document_id) else None,
                          "evidence": {"document_id": str(m.source_document_id) if ctx.live_doc(m.source_document_id) else None}})
    if types:
        items = [i for i in items if i["item_type"] in types]
    items.sort(key=lambda x: (x["date"] is None, -(x["date"].toordinal() if isinstance(x["date"], date) else 0) if x["date"] else 0, x["item_type"], x["id"]))
    total = len(items)
    counts = Counter(i["item_type"] for i in items)
    return {"total": total, "offset": offset, "limit": limit, "counts": dict(counts), "order": "newest first; undated items last", "items": items[offset: offset + limit],
            "unavailable": ctx.unavailable(), "notice": NOTICE}


# ------------------------------------------------------------------ personal health graph
def build_health_graph(ctx: Ctx, max_nodes: int = 300) -> dict:
    """Nodes/edges derived only from real stored relationships. No inferred edges."""
    nodes: dict[str, dict] = {}
    edges: list[dict] = []

    def node(nid, ntype, label, **kw):
        if nid not in nodes and len(nodes) < max_nodes:
            nodes[nid] = {"id": nid, "type": ntype, "label": (label or "")[:90], **kw}
        return nid in nodes

    def edge(a, b, rel):
        if a in nodes and b in nodes:
            edges.append({"source": a, "target": b, "relation": rel})

    pid = f"patient:{ctx.patient.id}"
    node(pid, "PATIENT", f"{ctx.patient.first_name} {ctx.patient.last_name}" if "PROFILE" in ctx.scopes else "Patient")
    for d in ctx.docs:
        if node(f"document:{d.id}", "DOCUMENT", d.filename, provenance=None, status=d.status.value): edge(pid, f"document:{d.id}", "HAS_DOCUMENT")
    for m in ctx.meds:
        mid = f"medication:{m.id}"
        if node(mid, "MEDICATION", m.medicine_name, provenance=m.provenance.value, status=m.status.value):
            edge(pid, mid, "HAS_MEDICATION")
            if ctx.live_doc(m.source_id): edge(mid, f"document:{m.source_id}", "EVIDENCE")
            for e in m.events:
                eid = f"medication_event:{e.id}"
                if node(eid, "MEDICATION_EVENT", e.event_type.value.title(), provenance=e.provenance.value, date=_iso(e.event_date)): edge(mid, eid, "CHANGE")
    for i in ctx.invs:
        iid = f"investigation:{i.id}"
        if node(iid, "INVESTIGATION", i.name, provenance=i.provenance.value, status=i.status.value, date=_iso(i.performed_date or i.ordered_date)):
            edge(pid, iid, "HAS_INVESTIGATION")
            for r in i.results:
                rid = f"result:{r.id}"
                if node(rid, "RESULT", r.result_summary[:60], provenance=i.provenance.value, date=_iso(r.result_date)):
                    edge(iid, rid, "RESULT")
                    if ctx.live_doc(r.source_id): edge(rid, f"document:{r.source_id}", "EVIDENCE")
    for e in ctx.events:
        eid = f"event:{e.id}"
        if node(eid, "EVENT", e.title, provenance=e.provenance.value, date=_iso(e.event_date), date_precision=e.date_precision.value):
            edge(pid, eid, "HAS_EVENT")
            if e.source_type.value == "DOCUMENT" and ctx.live_doc(e.source_id): edge(eid, f"document:{e.source_id}", "EVIDENCE")
    for a in ctx.actions:
        aid = f"action:{a.id}"
        if node(aid, "FOLLOW_UP", a.title, provenance=a.provenance.value, status=a.status.value, date=_iso(a.due_date)):
            edge(pid, aid, "HAS_FOLLOW_UP")
            for col, prefix, rel in [(a.source_document_id, "document", "EVIDENCE"), (a.source_event_id, "event", "ARISES_FROM"), (a.source_investigation_id, "investigation", "ARISES_FROM"), (a.source_medication_event_id, "medication_event", "ARISES_FROM"), (a.source_fact_id, "fact", "ARISES_FROM")]:
                if col: edge(aid, f"{prefix}:{col}", rel)
    for f in ctx.facts:
        fid = f"fact:{f.id}"
        if node(fid, "FACT", f.value, provenance=f.provenance.value, date=_iso(f.temporal_date), date_precision=f.temporal_precision.value, page=f.source_page): edge(fid, f"document:{f.document_id}", "EVIDENCE")
    by_concept: dict[tuple, list[HealthFact]] = defaultdict(list)
    for f in ctx.facts: by_concept[(f.fact_type, _fact_concept(f))].append(f)
    for group in by_concept.values():
        vals = {(f.normalized_value or f.value).strip().lower() for f in group}
        if len(group) > 1 and len(vals) > 1:
            for x, y in zip(group, group[1:]):
                if (x.normalized_value or x.value).strip().lower() != (y.normalized_value or y.value).strip().lower(): edge(f"fact:{x.id}", f"fact:{y.id}", "CONFLICTS_WITH")
    for kind in SPECS:
        rows = [m for m in ctx.measurements if m.kind == kind]
        if rows:
            vid = f"vitals:{kind}"
            if node(vid, "VITALS", f"{SPECS[kind].label} ({len(rows)} readings)"): edge(pid, vid, "HAS_VITALS")
            for dref in {m.source_document_id for m in rows if ctx.live_doc(m.source_document_id)}: edge(vid, f"document:{dref}", "EVIDENCE")
    for h in ctx.handoffs:
        if node(f"handoff:{h.id}", "HANDOFF", h.title): edge(pid, f"handoff:{h.id}", "HAS_HANDOFF")
    total_possible = 1 + len(ctx.docs) + len(ctx.meds) + sum(len(m.events) for m in ctx.meds) + len(ctx.invs) + sum(len(i.results) for i in ctx.invs) + len(ctx.events) + len(ctx.actions) + len(ctx.facts) + len(ctx.handoffs)
    return {"nodes": list(nodes.values()), "edges": edges, "node_count": len(nodes), "edge_count": len(edges), "truncated": total_possible > len(nodes), "total_possible_nodes": total_possible,
            "unavailable": ctx.unavailable(), "notice": "Every edge is a relationship stored in your records (ownership or a recorded source link). No relationships are inferred."}


# ------------------------------------------------------------------ dashboard
def build_dashboard(ctx: Ctx, *, family: dict | None) -> dict:
    today = date.today()
    p = ctx.patient
    out: dict[str, Any] = {"patient": {"id": str(p.id), "name": f"{p.first_name} {p.last_name}" if "PROFILE" in ctx.scopes else None}, "generated_at": datetime.now(timezone.utc).isoformat(), "unavailable": ctx.unavailable(), "sections": {}}
    s = out["sections"]
    if "DOCUMENTS" in ctx.scopes:
        counts = Counter(d.status.value for d in ctx.docs)
        s["documents"] = {"total": len(ctx.docs), "by_status": dict(counts), "recent": [{"id": str(d.id), "filename": d.filename, "document_type": d.document_type.value, "status": d.status.value, "created_at": d.created_at} for d in ctx.docs[:5]]}
    if "MEDICATIONS" in ctx.scopes:
        active = [m for m in ctx.meds if m.status == MedicationStatus.ACTIVE]
        recent = sorted([(e, m) for m in ctx.meds for e in m.events], key=lambda t: (t[0].event_date or date.min), reverse=True)[:5]
        s["medications"] = {"active_count": len(active), "active": [{"id": str(m.id), "name": m.medicine_name, "dose": m.dose, "frequency": m.frequency, "provenance": m.provenance.value, "unconfirmed": m.provenance.value in UNCONFIRMED} for m in active[:6]],
                            "recent_changes": [{"id": str(e.id), "medicine": m.medicine_name, "type": e.event_type.value, "date": _iso(e.event_date), "provenance": e.provenance.value} for e, m in recent]}
    if "INVESTIGATIONS" in ctx.scopes:
        rows = sorted(ctx.invs, key=lambda i: (i.performed_date or i.ordered_date or date.min), reverse=True)[:5]
        s["investigations"] = {"total": len(ctx.invs), "recent": [{"id": str(i.id), "name": i.name, "status": i.status.value, "date": _iso(i.performed_date or i.ordered_date), "has_result": bool(i.results), "result_as_reported": (sorted(i.results, key=lambda r: r.result_date or date.min, reverse=True)[0].result_summary[:140] if i.results else None), "provenance": i.provenance.value} for i in rows]}
    if "TIMELINE" in ctx.scopes:
        ev = sorted(ctx.events, key=lambda e: (e.event_date or date.min), reverse=True)[:5]
        s["events"] = {"total": len(ctx.events), "recent": [{"id": str(e.id), "title": e.title, "event_type": e.event_type.value, "date": _iso(e.event_date), "date_precision": e.date_precision.value, "provenance": e.provenance.value} for e in ev]}
    if "MEASUREMENTS" in ctx.scopes:
        latest = []
        for kind in SPECS:
            rows = [m for m in ctx.measurements if m.kind == kind]
            if rows:
                m = max(rows, key=lambda r: r.measured_at)
                latest.append({"kind": kind, "label": SPECS[kind].label, "value": m.value, "value_secondary": m.value_secondary, "unit": m.unit, "measured_at": m.measured_at, "provenance": m.provenance.value, "count": len(rows)})
        s["vitals"] = {"latest": latest, "total": len(ctx.measurements)}
    if "HEALTH_ACTIONS" in ctx.scopes:
        openers = [a for a in ctx.actions if a.status in (HealthActionStatus.PLANNED, HealthActionStatus.SCHEDULED)]
        openers.sort(key=lambda a: (a.due_date is None, a.due_date or date.max))
        s["follow_ups"] = {"open_count": len(openers), "suggested_unconfirmed_count": sum(1 for a in openers if awaiting_confirmation(a)),
                           "upcoming": [{"id": str(a.id), "title": a.title, "status": a.status.value, "due_date": _iso(a.due_date), "date_precision": a.temporal_precision.value, "overdue": bool(a.due_date and a.due_date < today), "source": a.source.value, "confirmed": not (awaiting_confirmation(a))} for a in openers[:5]]}
    conflicts = _conflicts(ctx)
    s["conflicts"] = {"count": len(conflicts), "items": [{"kind": c["kind"], "title": c["title"], "refs": c["refs"][:3]} for c in conflicts[:5]]}
    if family is not None:
        s["family"] = family
    return out
