from __future__ import annotations

RETRIEVAL_EVALUATION_DATASET = [
    {"id":"medication_1","query":"What dose of metformin was recorded?","relevant":["metformin"],"records":["metformin|Medication: Metformin 500 mg twice daily","glucose|Lab glucose 110 mg/dL","mri|MRI brain no acute findings"]},
    {"id":"lab_1","query":"What was my hemoglobin?","relevant":["hemoglobin"],"records":["hemoglobin|Hemoglobin 13.2 g/dL","metformin|Metformin 500 mg twice daily","mri|MRI brain no acute findings"]},
    {"id":"investigation_1","query":"What did my MRI show?","relevant":["mri_result"],"records":["mri_result|MRI brain: no acute findings","hemoglobin|Hemoglobin 13.2 g/dL","metformin|Metformin 500 mg twice daily"]},
    {"id":"date_1","query":"What was glucose in 2025?","relevant":["glucose_2025"],"records":["glucose_2024|Glucose 100 mg/dL, 2024","glucose_2025|Glucose 110 mg/dL, 2025","metformin|Metformin 500 mg twice daily"]},
    {"id":"historical_1","query":"What medication was recorded historically?","relevant":["old_med"],"records":["old_med|Historical medication: amoxicillin 250 mg","mri|MRI brain no acute findings","hemoglobin|Hemoglobin 13.2 g/dL"]},
    {"id":"multi_1","query":"Summarize glucose and metformin","relevant":["glucose","metformin"],"records":["glucose|Glucose 110 mg/dL","metformin|Metformin 500 mg twice daily","mri|MRI brain no acute findings"]},
    {"id":"contradiction_1","query":"What are the hemoglobin results?","relevant":["hb_a","hb_b"],"records":["hb_a|Hemoglobin 13.2 g/dL","hb_b|Hemoglobin 10.1 g/dL","metformin|Metformin 500 mg twice daily"]},
    {"id":"no_evidence_1","query":"What is my blood type?","relevant":[],"records":["glucose|Glucose 110 mg/dL","metformin|Metformin 500 mg twice daily","mri|MRI brain no acute findings"]},
    {"id":"prescription_compare_1","query":"What changed between my two prescriptions?","relevant":["rx_old","rx_new"],"records":["rx_old|Prescription: Metformin 500 mg once daily","rx_new|Prescription: Metformin 500 mg twice daily","mri|MRI brain no acute findings"]},
    {"id":"hospitalization_1","query":"When was I hospitalized?","relevant":["hospital"],"records":["hospital|Hospitalized for 4 days in 2022","glucose|Glucose 110 mg/dL","metformin|Metformin 500 mg"]},
    {"id":"followup_1","query":"What follow-up was documented after my visit?","relevant":["followup"],"records":["followup|Follow-up: repeat blood test in 3 months","metformin|Metformin 500 mg","mri|MRI brain no acute findings"]},
    {"id":"document_1","query":"Which document contains my discharge information?","relevant":["discharge_doc"],"records":["discharge_doc|Discharge summary from hospital admission","metformin|Prescription Metformin 500 mg","glucose|Laboratory report glucose 110 mg/dL"]},
    {"id":"historical_event_1","query":"What surgery did I report from 2019?","relevant":["surgery_2019"],"records":["surgery_2019|Appendix surgery in 2019, user reported","metformin|Metformin 500 mg","glucose|Glucose 110 mg/dL"]},
    {"id":"date_history_1","query":"What medication was recorded in 2021?","relevant":["med_2021"],"records":["med_2021|Amoxicillin recorded in 2021","med_2025|Metformin recorded in 2025","mri|MRI brain no acute findings"]},
    {"id":"multi_document_1","query":"Show documents related to my recent admission","relevant":["admission_doc","discharge_doc"],"records":["admission_doc|Admission note for recent hospitalization","discharge_doc|Discharge summary for recent hospitalization","glucose|Glucose 110 mg/dL"]},
    {"id":"contradiction_2","query":"What glucose results are recorded?","relevant":["glucose_a","glucose_b"],"records":["glucose_a|Glucose 100 mg/dL","glucose_b|Glucose 110 mg/dL","metformin|Metformin 500 mg"]},
    {"id":"paraphrase_1","query":"What was my blood sugar reading?","relevant":["glucose_para"],"records":["glucose_para|Glucose 105 mg/dL","metformin|Metformin 500 mg","mri|MRI brain no acute findings"]},
]
