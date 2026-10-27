PY ?= python
SEED ?= 20261022

.PHONY: install test starter full validate reproduce reproduce-full docs clean

install:
	$(PY) -m pip install -r requirements.txt
	$(PY) -m pip install -e . --no-deps

test:
	$(PY) -m pytest -q

starter:
	$(PY) -m liqfc.cli all --scale starter --seed $(SEED)

full:
	$(PY) -m liqfc.cli all --scale full --seed $(SEED)

validate:
	$(PY) -m liqfc.cli validate --scale starter

reproduce:
	$(PY) -m liqfc.cli reproduce --scale starter

reproduce-full:
	$(PY) scripts/reproduce.py --scale full

docs:
	$(PY) -m liqfc.cli docs

clean:
	$(PY) -c "import shutil; shutil.rmtree('output', ignore_errors=True)"
