# Contributing

Thanks for helping. This is a small research codebase; keep changes focused.

1. `python -m venv .venv && . .venv/bin/activate && pip install -e '.[dev,tokens]'`
2. Make the change with tests. Every behavior the paper specifies has a test;
   if you change a rule (reconciliation, SCD2, as-of, replay), update
   `docs/design-notes.md` and the test that pins it.
3. Run the same checks CI runs:
   `ruff check . && python -m mypy && pytest --cov`
4. Use synthetic data only. Do not commit real or proprietary data, names or schemas.
5. Open a pull request describing what changed and why.

By contributing you agree your contribution is licensed under Apache-2.0.
