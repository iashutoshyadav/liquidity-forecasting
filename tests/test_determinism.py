import hashlib

from liqfc.core.config import load_settings
from liqfc.core.rng import stream
from liqfc.exporters.csv_json import dataset_hash, write_tables
from liqfc.pipeline import generate_baseline


def _sha_tree(root):
    return {p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(root.rglob("*")) if p.is_file()}


def test_same_seed_same_dataset(settings, baseline):
    again, _ = generate_baseline(settings)
    assert dataset_hash(again.tables) == dataset_hash(baseline.tables)


def test_different_seed_different_dataset(baseline):
    other, _ = generate_baseline(load_settings("starter", seed=1))
    assert dataset_hash(other.tables) != dataset_hash(baseline.tables)
    assert len(other["ledger_entry"]) == len(baseline["ledger_entry"])


def test_independent_streams():
    a = stream(20261022, "entities").random(3)
    b = stream(20261022, "entities").random(3)
    c = stream(20261022, "ledger:sundry").random(3)
    assert (a == b).all() and not (a == c).all()


def test_output_files_byte_identical(tmp_path, baseline, settings):
    again, _ = generate_baseline(settings)
    write_tables(baseline.tables, tmp_path / "a")
    write_tables(again.tables, tmp_path / "b")
    assert _sha_tree(tmp_path / "a") == _sha_tree(tmp_path / "b")
    sample = (tmp_path / "a" / "csv" / "ledger_entry.csv").read_bytes()
    assert b"\r\n" not in sample


def test_derived_outputs_deterministic(settings, baseline, base_result):
    from liqfc.pipeline import run_system
    again = run_system(baseline, settings)
    assert dataset_hash(again.derived) == dataset_hash(base_result.derived)
