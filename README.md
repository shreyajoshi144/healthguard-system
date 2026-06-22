# 🏥 HealthGuard System

Patient Satisfaction Intelligence Platform - Transform raw patient feedback into actionable healthcare intelligence using NLP, sentiment analysis, priority scoring, and automated insight generation.

---

# 🚀 Overview

**HealthGuard System** is an end-to-end healthcare analytics platform designed to help hospitals and healthcare organizations analyze patient satisfaction surveys at scale.

The platform combines:

* Natural Language Processing (NLP)
* Sentiment Analysis
* Intelligent Issue Categorization
* Priority Scoring
* Automated Recommendations
* Interactive Analytics Dashboards

to convert unstructured patient feedback into measurable operational insights.

---
## Live Demo : https://healthguard-system-zrgg.onrender.com/
---
<img width="1536" height="1024" alt="ChatGPT Image Jun 22, 2026, 01_05_14 PM" src="https://github.com/user-attachments/assets/e48041ac-d9fd-4602-87d5-e814a9d14e21" />

# ✨ Core Features

## 📤 Data Intake & Validation

* Upload CSV or PDF patient survey datasets
* Automatic healthcare dataset validation
* Dynamic survey structure detection
* Rejects non-healthcare datasets
* Duplicate and null handling
* Flexible schema support

---

## 📋 Supported Input Columns

| Category   | Accepted Variations                             |
| ---------- | ----------------------------------------------- |
| Patient ID | `patient_id`, `id`, `respondent_id`             |
| Department | `department`, `dept`, `ward`                    |
| Feedback   | `feedback_text`, `comments`, `review`           |
| Rating     | `overall_rating`, `score`, `satisfaction_score` |

---

# 🧹 Data Cleaning Engine

Automated preprocessing pipeline includes:

* Column normalization
* Rating clipping (1–5)
* Date parsing
* Missing value imputation
* Duplicate removal
* Numeric conversion

### Implemented In

* `data_cleaner.py`
* `data_validator.py`

---

# ⚙️ NLP & Text Intelligence

## Text Preprocessing

* Lowercasing
* Tokenization
* Stopword removal
* Lemmatization
* URL cleaning
* Text normalization

## Feature Engineering

Derived features include:

* Word count
* Character count
* NPS category
* Rating bands
* Promoter/detractor flags
* Composite satisfaction score

---

# 💬 Sentiment Analysis

Powered using **VADER Sentiment Analysis**.

## Outputs

* Positive / Neutral / Negative classification
* Compound sentiment score
* Sentiment distribution
* Department-level sentiment benchmarking

## Sentiment Thresholds

| Sentiment | Threshold    |
| --------- | ------------ |
| Positive  | `>= 0.05`    |
| Negative  | `<= -0.05`   |
| Neutral   | Between both |

---

# 🏷️ Issue Intelligence

Automatically categorizes patient complaints into healthcare issue categories:

* Wait Time
* Staff Behaviour
* Cleanliness
* Communication
* Treatment Quality
* Billing
* Facilities
* Appointment Issues

Uses rule-based keyword classification optimized for healthcare survey feedback.

---

# ⚡ Priority Scoring Engine

Each patient complaint is ranked using a weighted severity model.

## Priority Factors

* Low ratings
* Negative sentiment
* Long/detailed complaints
* Critical issue categories

## Output Labels

* 🔴 Critical
* 🟠 High
* 🟡 Medium
* 🟢 Low

## Priority Formula

```math
Priority Score =
((6 - rating) × 1.5)
+
(( - sentiment + 1 ) / 2 × 3)
+
((word_count / max_word_count) × 1.5)
+
critical_category
```

---

# 🗺️ Action Recommendation Engine

Automatically maps operational actions to detected healthcare issues.

| Issue Category  | Recommended Action                   |
| --------------- | ------------------------------------ |
| Wait Time       | Add triage staff, improve scheduling |
| Cleanliness     | Increase housekeeping frequency      |
| Billing         | Simplify billing workflow            |
| Staff Behaviour | Conduct soft-skills training         |

---

# 📊 Exploratory Data Analytics

Interactive dashboards built using **Plotly + Streamlit**.

## Visualizations

* Rating distribution
* Department benchmarking
* Sentiment trends
* KPI cards
* Time-series analysis
* Correlation analysis
* Department comparison charts

---

# 🧠 Insight Generation

Automatically generates executive-ready insights from processed datasets.

## Generated Insights

* Department performance
* Sentiment trends
* NPS performance
* Complaint hotspots
* Critical alerts
* Operational recommendations

No external LLM required. Fully deterministic rule-based insight engine.

---

# 📁 Project Structure

```text
healthguard-system/
│
├── app.py
├── requirements.txt
├── README.md
│
├── assets/
│   └── style.css
│
├── modules/
│   ├── data_loader.py
│   ├── data_validator.py
│   ├── data_cleaner.py
│   ├── preprocessor.py
│   ├── feature_engineer.py
│   ├── sentiment.py
│   ├── issue_categorizer.py
│   ├── priority_scorer.py
│   ├── action_mapper.py
│   ├── recommender.py
│   ├── summarizer.py
│   ├── insight_generator.py
│   └── eda.py
│
├── utils/
│   ├── config.py
│   ├── helpers.py
│   └── logger.py
│
├── logs/
│   └── app.log
│
└── data/
    ├── raw/
    └── processed/
```

---

# 🎨 UI/UX Design

Modern healthcare SaaS-style interface with:

* Glassmorphism design system
* Animated dashboard cards
* Interactive sidebar navigation
* Responsive layouts
* Dynamic filters
* Dark premium theme

Built entirely with custom Streamlit styling.

---

# 🛠️ Tech Stack

## Backend & Analytics

* Python
* Pandas
* NumPy
* Scikit-learn

## NLP

* NLTK
* VADER Sentiment
* Sumy

## Visualization

* Plotly
* Matplotlib
* Streamlit

## Utilities

* pdfplumber
* Logging Framework
* Custom Helpers

---

# 🚀 Quick Start

## 1️⃣ Clone Repository

```bash
git clone <repository-url>
cd healthguard-system
```

---

## 2️⃣ Install Dependencies

```bash
pip install -r requirements.txt
```

---

## 3️⃣ Download NLTK Resources

```bash
python -c "import nltk; nltk.download('punkt'); nltk.download('stopwords'); nltk.download('wordnet')"
```

---

## 4️⃣ Run Application

```bash
streamlit run app.py
```

---

# 📈 Analytics Capabilities

## KPI Tracking

* Average Rating
* Positive Sentiment %
* Critical Complaints
* Department Performance
* NPS Score

## Healthcare Intelligence

* Operational bottleneck detection
* Complaint hotspot analysis
* Patient loyalty scoring
* Staff performance insights
* Service quality monitoring

---

# 🔒 Validation Rules

The system accepts only healthcare-related patient satisfaction datasets.

## Validation Checks Include

* Minimum survey rows
* Healthcare keywords
* Required columns
* Rating ranges
* Feedback availability

---


# 📌 Future Enhancements

* Real-time streaming analytics
* Predictive patient satisfaction modeling
* Multi-language feedback support
* LLM-powered recommendation engine
* Automated PDF report exports
* Role-based admin dashboards

---

