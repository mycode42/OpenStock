# Repository instructions

## Before pushing to GitHub

Commits may be created without running tests. Before every `git push`, run the full verification used by `.github/workflows/tests.yml`:

```sh
poetry check --lock
poetry install --with ai,ml,research --no-interaction --no-ansi
poetry build
poetry run python -m pytest -q
```

Resolve any failures before pushing. The push should happen only after all four commands pass.
