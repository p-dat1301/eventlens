"""T9 deterministic evaluation harnesses (fixture corpus, fake adapters only).

Outer-ring package like ``cli_main``: it wires in-memory adapters, the fake
enricher and scripted/fake synthesizers into the real compiled ingestion and
query StateGraphs. Nothing inside ``domain``/``application``/``workflows``/
``infra`` imports it. Reports prove control flow and accounting on a small
synthetic corpus; they are never live model-quality or production SLO evidence.
"""
