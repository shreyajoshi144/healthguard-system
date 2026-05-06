import streamlit as st
import pandas as pd

from modules.data_loader import load_data
from modules.data_validator import validate_dataframe
from modules.data_cleaner import clean_data
from modules.preprocessor import preprocess_data
from modules.feature_engineer import engineer_features
from modules.eda import render_eda
from modules.sentiment import run_sentiment_analysis
from modules.issue_categorizer import categorize_issues
from modules.priority_scorer import score_priorities
from modules.action_mapper import map_actions
from modules.recommender import generate_recommendations
from modules.summarizer import summarize_feedback
from modules.insight_generator import generate_insights

from utils.logger import get_logger

logger = get_logger(__name__)
st.set_page_config(
    page_title="HealthGuard System",
    page_icon="🏥",
    layout="wide",
    initial_sidebar_state="expanded"
)

try:
    with open("assets/style.css", encoding="utf-8") as f:
        st.markdown(f"<style>{f.read()}</style>", unsafe_allow_html=True)
except:
    pass

with st.sidebar:
    st.markdown('''
    <div class="sidebar-header">
        <div class="sidebar-logo">🏥</div>
        <div class="sidebar-brand">
            <span class="brand-text">HealthGuard</span> <span class="brand-ai">System</span>
        </div>
    </div>
    ''', unsafe_allow_html=True)

    st.markdown('<div class="sidebar-nav-title">DATA PIPELINE</div>', unsafe_allow_html=True)

    options = [
        "📤 Data Intake & Validation",
        "📊 System Overview",
        "🗺️ Action Orchestration",
        "💬 Sentiment Intelligence",
        "🏷️ Issue Intelligence",
        "📊 Priority Engine",
        "💡 Recommendation Engine",
        "📝 Insight Summary"
    ]

    page = st.radio(
        " ",
        options,
        index=0,
        label_visibility="collapsed"
    )

for key in ["df_raw", "df", "insights", "current_page"]:
    if key not in st.session_state:
        st.session_state[key] = None

if st.session_state.current_page is None:
    st.session_state.current_page = "upload_overview"

if page == "📤 Data Intake & Validation":

    st.markdown("""
    <div class="upload-hero">
        <div class="upload-hero-inner">
            <div class="upload-hero-tag">🏥HealthGuard System</div>
            <h1 class="upload-hero-title">
    Survey Data → <span class="upload-hero-accent">Clinical Actionable Insights</span>
</h1>
<p class="upload-hero-subtitle">
    Upload patient feedback in CSV or PDF format for processing.
</p>
        </div>
        <div class="upload-hero-glow"></div>
    </div>
    """, unsafe_allow_html=True)

    if st.session_state.current_page in ("upload_overview", "csv_upload", "pdf_upload"):

        col1, col2 = st.columns(2, gap="large")

        with col1:
            st.markdown("""
            <div class="upload-mode-card">
                <div class="upload-mode-icon">
                    <svg viewBox="0 0 64 64" fill="none" xmlns="http://www.w3.org/2000/svg">
                        <rect x="8" y="4" width="36" height="48" rx="4" stroke="currentColor" stroke-width="2.5" fill="none"/>
                        <path d="M36 4L44 12" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"/>
                        <rect x="36" y="4" width="8" height="8" rx="1" stroke="currentColor" stroke-width="2" fill="none"/>
                        <line x1="16" y1="24" x2="36" y2="24" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
                        <line x1="16" y1="32" x2="36" y2="32" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
                        <line x1="16" y1="40" x2="28" y2="40" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
                        <circle cx="50" cy="50" r="10" fill="rgba(99,102,241,0.15)" stroke="currentColor" stroke-width="2"/>
                        <path d="M47 50l2 2 4-4" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
                    </svg>
                </div>
                <h3>Analyze CSV</h3>
                <p>Upload structured patient satisfaction dataset with survey responses and ratings</p>
            </div>
            """, unsafe_allow_html=True)

            csv_file = st.file_uploader(
                "Select CSV file",
                type=["csv"],
                key="csv_uploader",
                help="Required columns: patient_id, department, feedback_text"
            )

            if csv_file:
                def validate_patient_dataset(df):
                    df.columns = [c.lower().strip().replace(' ', '_') for c in df.columns]

                    id_columns = ['patient_id', 'id', 'respondent_id', 'survey_id', 'record_id', 'patient_no']
                    has_id = any(col in df.columns for col in id_columns)

                    dept_columns = ['department', 'dept', 'ward', 'unit', 'location', 'service']
                    has_dept = any(col in df.columns for col in dept_columns)

                    text_columns = ['feedback_text', 'feedback', 'comments', 'comment', 'text', 'response', 'review']
                    text_col = None
                    for col in text_columns:
                        if col in df.columns:
                            text_col = col
                            break

                    if not text_col:
                        obj_cols = df.select_dtypes(include='object').columns.tolist()
                        if obj_cols:
                            avg_lengths = {col: df[col].dropna().astype(str).str.len().mean() for col in obj_cols}
                            text_col = max(avg_lengths, key=avg_lengths.get) if avg_lengths else None

                    has_text = text_col is not None

                    rating_columns = ['overall_rating', 'rating', 'satisfaction_score', 'score', 'satisfaction']
                    has_rating = any(col in df.columns for col in rating_columns)

                    if not has_text:
                        return False, "❌ Missing feedback/comment text column. Required for patient satisfaction analysis."

                    if df[text_col].dropna().shape[0] < 5:
                        return False, "❌ Not enough feedback data (minimum 5 responses required)."

                    sample_text = " ".join(df[text_col].dropna().astype(str).head(30)).lower()

                    healthcare_keywords = [
                        'patient', 'doctor', 'nurse', 'hospital', 'clinic', 'medical',
                        'treatment', 'care', 'staff', 'appointment', 'wait', 'service',
                        'health', 'physician', 'surgery', 'emergency', 'ward', 'department'
                    ]

                    keyword_matches = sum(1 for keyword in healthcare_keywords if keyword in sample_text)

                    if keyword_matches < 3:
                        return False, f"❌ This doesn't appear to be a patient satisfaction survey. Only {keyword_matches} healthcare-related keywords found. Please upload patient survey data."

                    col_text = " ".join(df.columns).lower()
                    col_keywords = ['patient', 'satisfaction', 'rating', 'feedback', 'department', 'staff', 'wait',
                                    'cleanliness', 'hospital', 'care']
                    col_matches = sum(1 for kw in col_keywords if kw in col_text)

                    if col_matches < 2:
                        return False, "❌ Column names don't match patient satisfaction survey format. Expected columns like: patient_id, department, rating, feedback_text."

                    details = []
                    if has_id:
                        details.append("ID column found")
                    if has_dept:
                        details.append("Department column found")
                    if has_rating:
                        details.append("Rating column found")
                    details.append(f"{keyword_matches} healthcare keywords detected")

                    return True, f"✅ Valid patient satisfaction dataset. {', '.join(details)}."

                logger.info(f"CSV uploaded: {csv_file.name}")
                with st.spinner("📥 Loading data..."):
                    df_raw = load_data(csv_file)

                if df_raw is None:
                    st.error("❌ Could not load CSV")
                    st.stop()

                is_valid, message = validate_patient_dataset(df_raw)
                if not is_valid:
                    st.error(message)
                    st.stop()

                st.success(message)

                issues = validate_dataframe(df_raw)
                st.markdown('<div class="validation-section-title">🔍 Validation Report</div>', unsafe_allow_html=True)
                if issues:
                    for issue in issues:
                        st.markdown(f'<div class="validation-warning">⚠️ {issue}</div>', unsafe_allow_html=True)
                else:
                    st.markdown('<div class="validation-pass">✅ Dataset validated — all checks passed</div>',
                                unsafe_allow_html=True)

                st.session_state.df_raw = df_raw
                logger.info(f"Data loaded: {df_raw.shape}")

                st.markdown("""
                <div class="pipeline-header">
                    <span class="pipeline-header-title">⚙️ Running Analysis Pipeline</span>
                </div>
                """, unsafe_allow_html=True)

                progress_bar = st.progress(0)
                status_text = st.empty()

                pipeline = [
                    ("🧹 Cleaning data...", lambda df: clean_data(df), 10),

                    ("⚙️ Preprocessing text...", lambda df:
                    __import__("modules.preprocessor", fromlist=['preprocess_data'])
                     .preprocess_data(df), 25),

                    ("🔧 Engineering features...", lambda df:
                    __import__("modules.feature_engineer", fromlist=['engineer_features'])
                     .engineer_features(df), 40),

                    ("💬 Running sentiment analysis...", lambda df:
                    __import__("modules.sentiment", fromlist=['run_sentiment_analysis'])
                     .run_sentiment_analysis(df), 60),

                    ("🏷️ Categorizing issues...", lambda df:
                    __import__("modules.issue_categorizer", fromlist=['categorize_issues'])
                     .categorize_issues(df), 75),

                    ("⚡ Scoring priorities...", lambda df:
                    __import__("modules.priority_scorer", fromlist=['score_priorities'])
                     .score_priorities(df), 88),

                    ("🗺️ Mapping actions...", lambda df:
                    __import__("modules.action_mapper", fromlist=['map_actions'])
                     .map_actions(df), 95),
                ]

                df_current = df_raw.copy()
                for label, func, pct in pipeline:
                    status_text.markdown(f'<div class="pipeline-step">{label}</div>', unsafe_allow_html=True)
                    progress_bar.progress(pct)
                    df_current = func(df_current.copy())

                progress_bar.progress(100)
                status_text.empty()
                st.session_state.df = df_current

                with st.spinner("🧠 Generating AI insights..."):
                    st.session_state.insights = generate_insights(df_current)

                st.success("✅ Full pipeline complete! Navigate using the sidebar →")
                st.session_state.current_page = "data_overview"
                st.rerun()

        with col2:
            st.markdown("""
            <div class="upload-mode-card upload-mode-card--pdf">
                <div class="upload-mode-icon">
                    <svg viewBox="0 0 64 64" fill="none" xmlns="http://www.w3.org/2000/svg">
                        <rect x="8" y="4" width="36" height="48" rx="4" stroke="currentColor" stroke-width="2.5" fill="none"/>
                        <path d="M36 4L44 12" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"/>
                        <rect x="36" y="4" width="8" height="8" rx="1" stroke="currentColor" stroke-width="2" fill="none"/>
                        <rect x="13" y="22" width="26" height="16" rx="3" stroke="currentColor" stroke-width="2" fill="rgba(139,92,246,0.1)"/>
                        <text x="16" y="34" font-size="9" fill="currentColor" font-family="monospace" font-weight="bold">PDF</text>
                    </svg>
                </div>
                <h3>Analyze PDF</h3>
                <p>Upload patient survey reports, clinical documents and feedback summaries</p>
            </div>
            """, unsafe_allow_html=True)

            pdf_file = st.file_uploader(
                "Upload survey report",
                type=["pdf"],
                key="pdf_uploader"
            )

            if pdf_file:
                st.warning("⚠️ Only patient survey PDFs allowed")
                confirm = st.checkbox("✅ Confirm this is a patient satisfaction report")
                if confirm:
                    st.success("✅ PDF accepted — processing can be added later")
                    st.session_state["pdf_file_data"] = pdf_file

    elif st.session_state.current_page == "data_overview":
        df = st.session_state.df
        if df is not None:
            st.markdown('<h2 class="section-title">📊 Dataset Overview</h2>', unsafe_allow_html=True)

            kpi_data = [
                ("📋 Total Responses", len(df), None),
                ("⭐ Avg Rating", f"{df['overall_rating'].mean():.2f}/5" if "overall_rating" in df.columns else "N/A",
                 None),
                ("😊 Positive %",
                 f"{(df['sentiment_label'] == 'Positive').mean() * 100:.1f}%" if "sentiment_label" in df.columns else "N/A",
                 "#2ecc71"),
                ("🏥 Departments", df["department"].nunique() if "department" in df.columns else "N/A", None),
                ("🔴 Critical Issues",
                 int((df["priority_score"] >= 7).sum()) if "priority_score" in df.columns else "N/A", "#e74c3c"),
            ]

            cols = st.columns(5)
            for i, (label, value, accent) in enumerate(kpi_data):
                border = f"border-left: 4px solid {accent};" if accent else ""
                with cols[i]:
                    st.markdown(f"""
                    <div class="kpi-card glass-card" style="{border}">
                        <div class="kpi-label">{label}</div>
                        <div class="kpi-value">{value}</div>
                    </div>
                    """, unsafe_allow_html=True)

            st.markdown("---")
            st.subheader("📄 Data Preview")
            st.dataframe(df.head(50), use_container_width=True)

            if st.button("← Upload New Dataset"):
                st.session_state.current_page = "upload_overview"
                st.session_state.df = None
                st.session_state.df_raw = None
                st.rerun()
        else:
            st.warning("No data loaded. Please upload a dataset.")
            if st.button("← Back to Upload"):
                st.session_state.current_page = "upload_overview"
                st.rerun()

elif page == "💬 Sentiment Intelligence":
    if st.session_state.df is not None:
        import plotly.express as px

        df = st.session_state.df
        st.markdown('<h2 class="section-title">💬 Sentiment Intelligence</h2>', unsafe_allow_html=True)

        col1, col2, col3 = st.columns(3)
        if "sentiment_label" in df.columns:
            counts = df["sentiment_label"].value_counts()
            metrics = [
                ("😊 Positive", "Positive", "#2ecc71"),
                ("😐 Neutral", "Neutral", "#f39c12"),
                ("😞 Negative", "Negative", "#e74c3c"),
            ]
            for col, (label, key, color) in zip([col1, col2, col3], metrics):
                n = counts.get(key, 0)
                with col:
                    st.markdown(f"""
                    <div class="sentiment-card glass-card" style="border-left:4px solid {color};">
                        <div class="kpi-label">{label}</div>
                        <div class="kpi-value">{n}</div>
                        <div class="kpi-sub">{n / len(df) * 100:.1f}% of responses</div>
                    </div>
                    """, unsafe_allow_html=True)

        c1, c2 = st.columns(2)
        if "sentiment_label" in df.columns:
            with c1:
                fig = px.pie(df, names="sentiment_label", color="sentiment_label",
                             color_discrete_map={"Positive": "#2ecc71", "Neutral": "#f39c12", "Negative": "#e74c3c"},
                             title="Sentiment Distribution", hole=0.4)
                fig.update_layout(paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)", font_color="#e2e8f0")
                st.plotly_chart(fig, use_container_width=True)
            with c2:
                if "department" in df.columns:
                    dept_sent = df.groupby(["department", "sentiment_label"]).size().reset_index(name="count")
                    fig2 = px.bar(dept_sent, x="department", y="count", color="sentiment_label",
                                  color_discrete_map={"Positive": "#2ecc71", "Neutral": "#f39c12",
                                                      "Negative": "#e74c3c"},
                                  title="Sentiment by Department", barmode="group")
                    fig2.update_layout(paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                                       font_color="#e2e8f0")
                    st.plotly_chart(fig2, use_container_width=True)

        if "vader_compound" in df.columns:
            fig3 = px.histogram(df, x="vader_compound", nbins=30,
                                title="VADER Compound Score Distribution",
                                color_discrete_sequence=["#6366f1"])
            fig3.add_vline(x=0.05, line_dash="dash", line_color="#2ecc71", annotation_text="Positive threshold")
            fig3.add_vline(x=-0.05, line_dash="dash", line_color="#e74c3c", annotation_text="Negative threshold")
            fig3.update_layout(paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)", font_color="#e2e8f0")
            st.plotly_chart(fig3, use_container_width=True)

        st.subheader("📋 Sample Feedback with Sentiment")
        text_col = "feedback_text" if "feedback_text" in df.columns else df.select_dtypes(include="object").columns[0]
        cols_show = [c for c in [text_col, "department", "sentiment_label", "vader_compound"] if c in df.columns]
        st.dataframe(df[cols_show].dropna().head(30), use_container_width=True)
    else:
        st.markdown(
            '<div class="glass-card warning-box">⚠️ Please upload your survey data first via the <b>📤 Data Intake &amp; Validation</b> page.</div>',
            unsafe_allow_html=True)

elif page == "🏷️ Issue Intelligence":
    if st.session_state.df is not None:
        import plotly.express as px

        df = st.session_state.df
        st.markdown('<h2 class="section-title">🏷️ Issue Intelligence</h2>', unsafe_allow_html=True)

        if "issue_category" in df.columns:
            c1, c2 = st.columns(2)
            with c1:
                cat_counts = df["issue_category"].value_counts().reset_index()
                fig = px.bar(cat_counts, x="issue_category", y="count",
                             color="issue_category", title="Issue Category Distribution")
                fig.update_layout(paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                                  font_color="#e2e8f0", showlegend=False)
                st.plotly_chart(fig, use_container_width=True)
            with c2:
                fig_pie = px.pie(cat_counts, names="issue_category", values="count",
                                 title="Issue Share", hole=0.35)
                fig_pie.update_layout(paper_bgcolor="rgba(0,0,0,0)", font_color="#e2e8f0")
                st.plotly_chart(fig_pie, use_container_width=True)
    else:
        st.markdown(
            '<div class="glass-card warning-box">⚠️ Please upload your survey data first via the <b>📤 Data Intake &amp; Validation</b> page.</div>',
            unsafe_allow_html=True)

elif page == "📊 Priority Engine":
    if st.session_state.df is not None:
        import plotly.express as px

        df = st.session_state.df
        st.markdown('<h2 class="section-title">⚡ Priority Engine</h2>', unsafe_allow_html=True)

        if "priority_score" in df.columns:
            p_labels = ["🟢 Low", "🟡 Medium", "🟠 High", "🔴 Critical"]
            p_ranges = [(0, 3), (3, 5), (5, 7), (7, 10.1)]
            p_colors = ["#2ecc71", "#f39c12", "#e67e22", "#e74c3c"]
            cols = st.columns(4)
            for i, (label, (lo, hi), color) in enumerate(zip(p_labels, p_ranges, p_colors)):
                n = ((df["priority_score"] >= lo) & (df["priority_score"] < hi)).sum()
                with cols[i]:
                    st.markdown(f"""
                    <div class="kpi-card glass-card" style="border-left:4px solid {color}">
                        <div class="kpi-label">{label}</div>
                        <div class="kpi-value">{n}</div>
                    </div>
                    """, unsafe_allow_html=True)

            c1, c2 = st.columns(2)
            with c1:
                fig = px.histogram(df, x="priority_score", nbins=20,
                                   title="Priority Score Distribution",
                                   color_discrete_sequence=["#e74c3c"])
                fig.update_layout(paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)", font_color="#e2e8f0")
                st.plotly_chart(fig, use_container_width=True)
            with c2:
                if "department" in df.columns:
                    dept_priority = df.groupby("department")["priority_score"].mean().sort_values(
                        ascending=False).reset_index()
                    fig2 = px.bar(dept_priority, x="department", y="priority_score",
                                  title="Avg Priority Score by Department",
                                  color="priority_score", color_continuous_scale="RdYlGn_r", text_auto=".2f")
                    fig2.update_layout(paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                                       font_color="#e2e8f0")
                    st.plotly_chart(fig2, use_container_width=True)

            st.subheader("🔴 Top 20 Critical Cases")
            top_cols = [c for c in ["patient_id", "department", "feedback_text", "priority_score", "priority_label",
                                    "recommended_action"] if c in df.columns]
            st.dataframe(df.nlargest(20, "priority_score")[top_cols], use_container_width=True)
    else:
        st.markdown(
            '<div class="glass-card warning-box">⚠️ Please upload your survey data first via the <b>📤 Data Intake &amp; Validation</b> page.</div>',
            unsafe_allow_html=True)

elif page == "🗺️ Action Orchestration":
    if st.session_state.df is not None:
        df = st.session_state.df
        st.markdown('<h2 class="section-title">🗺️ Action Orchestration</h2>', unsafe_allow_html=True)
        st.info("Each complaint is automatically mapped to a recommended action based on category and severity.")

        if "recommended_action" in df.columns:
            text_col = "feedback_text" if "feedback_text" in df.columns else df.select_dtypes(include="object").columns[
                0]
            show_cols = [c for c in [text_col, "department", "issue_category", "priority_label", "priority_score",
                                     "recommended_action"] if c in df.columns]

            dept_filter = "All"
            if "department" in df.columns:
                dept_filter = st.selectbox("Filter by Department",
                                           ["All"] + sorted(df["department"].dropna().unique().tolist()))

            priority_filter = st.multiselect("Filter by Priority", ["🔴 Critical", "🟠 High", "🟡 Medium", "🟢 Low"],
                                             default=["🔴 Critical", "🟠 High"])

            filtered = df.copy()
            if dept_filter != "All":
                filtered = filtered[filtered["department"] == dept_filter]
            if priority_filter and "priority_label" in filtered.columns:
                filtered = filtered[filtered["priority_label"].isin(priority_filter)]

            st.dataframe(filtered[show_cols].dropna().head(50), use_container_width=True)
            st.download_button("⬇️ Download Action Plan (CSV)", filtered[show_cols].to_csv(index=False),
                               file_name="action_plan.csv", mime="text/csv")
    else:
        st.markdown(
            '<div class="glass-card warning-box">⚠️ Please upload your survey data first via the <b>📤 Data Intake &amp; Validation</b> page.</div>',
            unsafe_allow_html=True)

elif page == "💡 Recommendation Engine":
    if st.session_state.df is not None:
        df = st.session_state.df
        st.markdown('<h2 class="section-title">💡 Recommendation Engine</h2>', unsafe_allow_html=True)
        recs = generate_recommendations(df)
        for dept, items in recs.items():
            with st.expander(f"📌 {dept}", expanded=False):
                for item in items:
                    st.markdown(f"- {item}")
    else:
        st.markdown(
            '<div class="glass-card warning-box">⚠️ Please upload your survey data first via the <b>📤 Data Intake &amp; Validation</b> page.</div>',
            unsafe_allow_html=True)

elif page == "📝 Insight Summary":
    if st.session_state.df is not None:
        df = st.session_state.df
        st.markdown('<h2 class="section-title">📝 Insight Summary</h2>', unsafe_allow_html=True)

        if 'expand_all' not in st.session_state:
            st.session_state.expand_all = False

        st.markdown('<div class="filter-section glass-card">', unsafe_allow_html=True)

        col1, col2 = st.columns([2, 1])
        with col1:
            search_query = st.text_input(
                "🔍 Search in feedback",
                placeholder="Search keywords in patient feedback...",
                key="feedback_search"
            )
        with col2:
            col_expand, col_collapse = st.columns(2)
            with col_expand:
                if st.button("📂 Expand All", use_container_width=True):
                    st.session_state.expand_all = True
            with col_collapse:
                if st.button("📁 Collapse All", use_container_width=True):
                    st.session_state.expand_all = False

        filter_col1, filter_col2, filter_col3 = st.columns(3)

        with filter_col1:
            dept_options = ["All Departments"] + sorted(
                df["department"].dropna().unique().tolist()) if "department" in df.columns else ["All Departments"]
            selected_dept = st.selectbox("🏥 Department", dept_options, key="dept_filter")

        with filter_col2:
            sentiment_options = ["All Sentiments", "Positive", "Neutral", "Negative"]
            selected_sentiment = st.selectbox("😊 Sentiment", sentiment_options, key="sentiment_filter")

        with filter_col3:
            priority_options = ["All Priorities", "Critical", "High", "Medium", "Low"]
            selected_priority = st.selectbox("⚡ Priority", priority_options, key="priority_filter")

        st.markdown('</div>', unsafe_allow_html=True)

        filtered_df = df.copy()

        if search_query:
            text_col = "feedback_text" if "feedback_text" in df.columns else df.select_dtypes(include="object").columns[
                0]
            filtered_df = filtered_df[filtered_df[text_col].str.contains(search_query, case=False, na=False)]

        if selected_dept != "All Departments" and "department" in filtered_df.columns:
            filtered_df = filtered_df[filtered_df["department"] == selected_dept]

        if selected_sentiment != "All Sentiments" and "sentiment_label" in filtered_df.columns:
            filtered_df = filtered_df[filtered_df["sentiment_label"] == selected_sentiment]

        if selected_priority != "All Priorities" and "priority_label" in filtered_df.columns:
            filtered_df = filtered_df[filtered_df["priority_label"] == selected_priority]

        if len(filtered_df) < len(df):
            st.info(f"📊 Showing {len(filtered_df)} of {len(df)} responses based on filters")

        if len(filtered_df) > 0:
            summaries = summarize_feedback(filtered_df)

            dept_metrics = {}
            if "department" in filtered_df.columns and "sentiment_label" in filtered_df.columns:
                for dept in filtered_df["department"].dropna().unique():
                    dept_data = filtered_df[filtered_df["department"] == dept]
                    total = len(dept_data)
                    positive = (dept_data[
                                    "sentiment_label"] == "Positive").sum() if "sentiment_label" in dept_data.columns else 0
                    negative = (dept_data[
                                    "sentiment_label"] == "Negative").sum() if "sentiment_label" in dept_data.columns else 0

                    top_issue = "N/A"
                    if "issue_category" in dept_data.columns:
                        issue_counts = dept_data["issue_category"].value_counts()
                        top_issue = issue_counts.index[0] if len(issue_counts) > 0 else "N/A"

                    dept_metrics[dept] = {
                        "positive_pct": (positive / total * 100) if total > 0 else 0,
                        "negative_pct": (negative / total * 100) if total > 0 else 0,
                        "top_issue": top_issue,
                        "total": total
                    }

            for dept, summary in summaries.items():
                metrics = dept_metrics.get(dept, {})
                pos_pct = metrics.get("positive_pct", 0)
                neg_pct = metrics.get("negative_pct", 0)
                top_issue = metrics.get("top_issue", "N/A")
                total = metrics.get("total", 0)

                if pos_pct >= 70:
                    border_color = "#2ecc71"
                    border_class = "summary-card-positive"
                elif neg_pct >= 40:
                    border_color = "#e74c3c"
                    border_class = "summary-card-negative"
                else:
                    border_color = "rgba(99, 102, 241, 0.3)"
                    border_class = "summary-card-neutral"

                with st.expander(f"📋 {dept} Summary", expanded=st.session_state.expand_all):
                    metric_col1, metric_col2, metric_col3, metric_col4 = st.columns(4)
                    with metric_col1:
                        st.markdown(f"""
                        <div class="summary-metric">
                            <div class="summary-metric-label">Total Responses</div>
                            <div class="summary-metric-value">{total}</div>
                        </div>
                        """, unsafe_allow_html=True)
                    with metric_col2:
                        st.markdown(f"""
                        <div class="summary-metric">
                            <div class="summary-metric-label">😊 Positive</div>
                            <div class="summary-metric-value" style="color: #2ecc71;">{pos_pct:.1f}%</div>
                        </div>
                        """, unsafe_allow_html=True)
                    with metric_col3:
                        st.markdown(f"""
                        <div class="summary-metric">
                            <div class="summary-metric-label">😞 Negative</div>
                            <div class="summary-metric-value" style="color: #e74c3c;">{neg_pct:.1f}%</div>
                        </div>
                        """, unsafe_allow_html=True)
                    with metric_col4:
                        st.markdown(f"""
                        <div class="summary-metric">
                            <div class="summary-metric-label">Top Issue</div>
                            <div class="summary-metric-value" style="font-size: 0.9rem;">{top_issue}</div>
                        </div>
                        """, unsafe_allow_html=True)

                    st.markdown(f"""
                    <div class="sentiment-bar-container">
                        <div class="sentiment-bar-positive" style="width: {pos_pct}%;"></div>
                        <div class="sentiment-bar-negative" style="width: {neg_pct}%;"></div>
                    </div>
                    """, unsafe_allow_html=True)

                    st.markdown(
                        f'<div class="summary-box glass-card" style="border-left: 4px solid {border_color};">{summary}</div>',
                        unsafe_allow_html=True)

        else:
            st.warning("⚠️ No feedback matches your filters. Try adjusting the search or filter criteria.")

        st.markdown("---")
        st.subheader("🔑 Key Themes & Feedback Intelligence")

        text_col = "feedback_text" if "feedback_text" in filtered_df.columns else None
        if text_col and len(filtered_df) > 0:
            from collections import Counter
            import re

            def clean_for_keywords(text):
                text = str(text).lower()
                return re.sub(r'[^a-zA-Z\s]', '', text)

            stopwords = {'the', 'is', 'and', 'to', 'of', 'in', 'for', 'on', 'with', 'was',
                         'were', 'it', 'this', 'that', 'very', 'had', 'have', 'has', 'a',
                         'an', 'at', 'by', 'from', 'as', 'but', 'not', 'they', 'their',
                         'them', 'i', 'we', 'you', 'be', 'been', 'are', 'or'}

            all_words = ' '.join(filtered_df[text_col].fillna("").apply(clean_for_keywords)).split()
            filtered_words = [w for w in all_words if w not in stopwords and len(w) > 3]
            top_kw = Counter(filtered_words).most_common(10)

            badges_html = "".join([f'<span class="keyword-badge">{w} ({c})</span>' for w, c in top_kw])
            st.markdown(f'<div class="notebook-summary-card glass-card"><h4>🔑 Top Keywords</h4>{badges_html}</div>',
                        unsafe_allow_html=True)

            if "department" in filtered_df.columns and "sentiment_label" in filtered_df.columns:
                dept_pos = (filtered_df.groupby("department")["sentiment_label"]
                            .apply(lambda x: (x == "Positive").mean() * 100).round(1))
                if len(dept_pos) > 0:
                    best_dept = dept_pos.idxmax()
                    worst_dept = dept_pos.idxmin()

                    st.markdown(f"""
                    <div class="dept-insight-row">
                        <div class="dept-best glass-card">
                            🏆 <b>Best Department</b><br>{best_dept}
                            <br><small>{dept_pos[best_dept]:.1f}% positive</small>
                        </div>
                        <div class="dept-worst glass-card">
                            ⚠️ <b>Needs Improvement</b><br>{worst_dept}
                            <br><small>{dept_pos[worst_dept]:.1f}% positive</small>
                        </div>
                    </div>
                    """, unsafe_allow_html=True)
    else:
        st.markdown(
            '<div class="glass-card warning-box">⚠️ Please upload your survey data first via the <b>📤 Data Intake &amp; Validation</b> page.</div>',
            unsafe_allow_html=True)

elif page == "📊 System Overview":
    if st.session_state.df is not None:
        df = st.session_state.df
        st.markdown('<h2 class="section-title">📊 System Overview</h2>', unsafe_allow_html=True)

        kpi_data = [
            ("📋 Total Responses", len(df), None),
            ("⭐ Avg Rating", f"{df['overall_rating'].mean():.2f}/5" if "overall_rating" in df.columns else "N/A", None),
            ("😊 Positive %",
             f"{(df['sentiment_label'] == 'Positive').mean() * 100:.1f}%" if "sentiment_label" in df.columns else "N/A",
             "#2ecc71"),
            ("🏥 Departments", df["department"].nunique() if "department" in df.columns else "N/A", None),
            ("🔴 Critical Issues",
             int((df["priority_score"] >= 7).sum()) if "priority_score" in df.columns else "N/A", "#e74c3c"),
        ]

        cols = st.columns(5)
        for i, (label, value, accent) in enumerate(kpi_data):
            border = f"border-left: 4px solid {accent};" if accent else ""
            with cols[i]:
                st.markdown(f"""
                <div class="kpi-card glass-card" style="{border}">
                    <div class="kpi-label">{label}</div>
                    <div class="kpi-value">{value}</div>
                </div>
                """, unsafe_allow_html=True)

        st.divider()
        render_eda(df)
    else:
        st.markdown(
            '<div class="glass-card warning-box">⚠️ Please upload your survey data first via the <b>📤 Data Intake &amp; Validation</b> page.</div>',
            unsafe_allow_html=True)

