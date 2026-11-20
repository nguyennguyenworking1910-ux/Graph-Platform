from pathlib import Path

import pytest

from graph_platform.config import Settings

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Real repo config, isolated data dir."""
    return Settings(root=ROOT, data_dir=tmp_path / "data")


@pytest.fixture(scope="session")
def built(tmp_path_factory) -> Settings:
    """Two tenants taken through simulate -> trunk -> resolution -> graph projection."""
    import shutil

    from graph_platform.graph import build_graph
    from graph_platform.resolution import resolve_parties
    from graph_platform.simulator import SimConfig, simulate
    from graph_platform.trunk import pipeline

    root = tmp_path_factory.mktemp("built")
    shutil.copytree(ROOT / "conf", root / "conf", ignore=shutil.ignore_patterns("dictionaries"))
    s = Settings(root=root, data_dir=root / "data")
    for tenant, parties in (("alpha_group", 6000), ("beta_group", 1500)):
        simulate(s, SimConfig(tenant=tenant, parties=parties))
        assert all(r.applied for r in pipeline.onboard(s, tenant).values())
        resolve_parties(s, tenant)
        build_graph(s, tenant)
    return s
