"""Test isolation: the in-process rate limiter is module-global state; without a reset, request counts from earlier
tests leak into later ones and cause order-dependent 429s (reproducible on the original Sprint 5 ZIP)."""
import pytest
from domain import rate_limit

@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    rate_limit._local.clear()
    yield
    rate_limit._local.clear()


def pytest_configure(config):
    """Reliability tests decide at *collection* time (unittest.skipUnless) whether their inputs exist. The train/val/test
    splits are generated, seeded and deterministic, so build them first; otherwise a clean checkout silently skips 7 tests
    and only a re-run (with splits left behind by the first run) executes them."""
    try:
        import evaluation.splits as s
        if not (s.TRAIN_PATH.exists() and s.VAL_PATH.exists() and s.TEST_PATH.exists()):
            s.build_splits()
    except Exception as exc:                                  # never hide the problem: the guard test below then fails loudly
        import warnings
        warnings.warn(f"could not build evaluation splits before collection: {exc!r}")
