"""
utils/streamlit_compat.py
──────────────────────────
Some pipeline modules (data_cleaner.py, sentiment.py) display small status
messages via `st.info` / `st.warning` when running inside the Streamlit app.
Those same modules are also imported by the offline training/inference path
(training/train_models.py, modules/ml_*.py), which runs as a plain Python
script with no active Streamlit session.

Calling `st.info(...)` etc. outside a real Streamlit script run doesn't
crash, but it prints "missing ScriptRunContext" warnings and a
"run it with `streamlit run ...`" notice to stdout — noise that has nothing
to do with training. `in_streamlit_context()` lets those call sites skip the
UI call (falling back to the existing logger call only) when there's no
active Streamlit session, with zero behavior change when the app is
actually running via `streamlit run app.py`.
"""

from __future__ import annotations


def in_streamlit_context() -> bool:
    """True only when called from inside a real, running Streamlit app.

    Streamlit's own `get_script_run_ctx()` logs a harmless but noisy
    "missing ScriptRunContext" WARNING every time it's called with no
    active session (which is always true here, since this helper exists
    specifically for the non-UI training/inference path). We briefly raise
    that one logger's level so the check itself stays silent; this doesn't
    touch any other logging in the app.
    """
    try:
        import logging
        from streamlit.runtime.scriptrunner import get_script_run_ctx

        st_logger = logging.getLogger("streamlit.runtime.scriptrunner_utils.script_run_context")
        previous_level = st_logger.level
        st_logger.setLevel(logging.ERROR)
        try:
            return get_script_run_ctx() is not None
        finally:
            st_logger.setLevel(previous_level)
    except Exception:
        return False


def conditional_cache_data(func):
    """
    Behaves exactly like `@st.cache_data(show_spinner=False)` when called
    from inside a real running Streamlit app (identical caching behavior,
    zero change) — but calls the wrapped function directly, uncached, when
    there's no active Streamlit session (e.g. from the FastAPI backend or
    the training/inference scripts).

    Why this exists: `st.cache_data`'s hasher assumes its arguments are
    real Streamlit `UploadedFile` objects (it stats a filesystem path via
    the object's `.name`). That assumption breaks for any other file-like
    object — such as the in-memory upload wrapper the FastAPI backend
    passes to `data_loader.load_data()` — with an unrelated
    FileNotFoundError. This wrapper only changes *when caching applies*;
    the wrapped function's own logic and return value are never altered.
    """
    import functools

    _cached_impl = {"fn": None}

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        if in_streamlit_context():
            import streamlit as st
            if _cached_impl["fn"] is None:
                _cached_impl["fn"] = st.cache_data(show_spinner=False)(func)
            return _cached_impl["fn"](*args, **kwargs)
        return func(*args, **kwargs)

    return wrapper
