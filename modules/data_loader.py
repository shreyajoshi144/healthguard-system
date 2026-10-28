import pandas as pd
import pdfplumber
import streamlit as st
import io
from utils.logger import get_logger
from utils.helpers import is_patient_survey
from utils.streamlit_compat import in_streamlit_context, conditional_cache_data

logger = get_logger(__name__)

@conditional_cache_data
def load_data(uploaded_file) -> pd.DataFrame:
    """Load CSV or PDF into a DataFrame with patient survey validation."""
    name = uploaded_file.name.lower()
    logger.info("Loading file: %s", name)

    if name.endswith(".csv"):
        try:
            # Reset file pointer to beginning
            uploaded_file.seek(0)
            # Use quoting parameter to handle commas in text fields
            df = pd.read_csv(uploaded_file, encoding="utf-8", on_bad_lines="skip")
            valid, reason = is_patient_survey(df)
            if not valid:
                if in_streamlit_context():
                    st.error(f"❌ {reason}")
                logger.warning("Dataset rejected: %s", reason)
                return None
            if in_streamlit_context():
                st.success(f"✅ Loaded {len(df)} rows, {len(df.columns)} columns. {reason}")
            logger.info("CSV loaded: %s", df.shape)
            return df
        except Exception as e:
            if in_streamlit_context():
                st.error(f"CSV load error: {e}")
            logger.exception("CSV load failed")
            return None

    elif name.endswith(".pdf"):
        try:
            rows = []
            with pdfplumber.open(uploaded_file) as pdf:
                for page in pdf.pages:
                    tables = page.extract_tables()
                    for table in tables:
                        if table:
                            rows.extend(table)
            if not rows:
                if in_streamlit_context():
                    st.error("No tables found in PDF. Please use CSV for best results.")
                return None
            df = pd.DataFrame(rows[1:], columns=rows[0])
            valid, reason = is_patient_survey(df)
            if not valid:
                if in_streamlit_context():
                    st.error(f"❌ {reason}")
                return None
            if in_streamlit_context():
                st.success(f"✅ Extracted {len(df)} rows from PDF")
            logger.info("PDF loaded: %s", df.shape)
            return df
        except Exception as e:
            if in_streamlit_context():
                st.error(f"PDF load error: {e}")
            logger.exception("PDF load failed")
            return None

    return None
