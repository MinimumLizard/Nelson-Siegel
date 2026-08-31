"""SGCP RV pipeline — Stage 0: the data layer.

This package ingests Sri Lanka PDMO daily secondary-market reports into a
SQLite database. Future stages (a `curves/` module for Nelson-Siegel fitting
and a `signals/` module for rich/cheap residuals) will read from that database
and never touch the PDFs directly — the database is the contract between
stages.

Run the entry points as modules, e.g.:

    python -m pipeline.backfill
    python -m pipeline.update
    python -m pipeline.report --isin LKB00934F154
"""
