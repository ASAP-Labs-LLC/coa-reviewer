"""Benchmark harness for the COA Reviewer web app.

Drives the *real* Flask app, in a real headless Chromium, over N synthetic
samples, and records how long it takes a reviewer to open and switch between
COAs. Nothing in here is imported by the app; nothing in here modifies it.

``bench/browser.py`` carries the measurement definitions and the known
limits of each signal; ``bench/run.py`` documents every run parameter.

The preview path is the *real* ``COASession`` by default, driven against
``bench/qbench_fake.py`` on loopback, so the single session lock, the poll
cadence and the render wait are all genuinely paid for. ``bench/realpreview.py``
holds that wiring and the lock instrumentation. ``--fake-preview`` restores the
older instant ``bench.fakes.FakeCoaSession`` for reproducing earlier results.
"""
