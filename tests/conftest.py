import pytest

from liqfc.anomalies.injectors import inject_all
from liqfc.core.config import load_settings
from liqfc.pipeline import generate_baseline, make_fx, run_system


@pytest.fixture(scope="session")
def settings():
    return load_settings("starter")


@pytest.fixture(scope="session")
def baseline(settings):
    ds, _ = generate_baseline(settings)
    return ds


@pytest.fixture(scope="session")
def calfx(settings, baseline):
    return make_fx(baseline, settings)


@pytest.fixture(scope="session")
def injected(settings, baseline, calfx):
    cal, fx = calfx
    return inject_all(baseline, settings, cal, fx)


@pytest.fixture(scope="session")
def base_result(settings, baseline):
    return run_system(baseline, settings)


@pytest.fixture(scope="session")
def inj_result(settings, injected):
    return run_system(injected[0], settings)
