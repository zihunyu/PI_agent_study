# CI dependency lock

`ci.in` contains the exact top-level runtime, test, build, and supply-chain
tools used by CI. `ci.lock` is the fully resolved, hash-checked installation
input for Python 3.11-3.13 on Linux and Python 3.12 on Windows.

Regenerate the lock from the repository root with a clean Python 3.12
environment:

```console
python -m pip install "pip-tools==7.6.1"
python -m piptools compile --generate-hashes --resolver=backtracking \
  --allow-unsafe --strip-extras \
  --output-file=requirements/ci.lock requirements/ci.in
```

Review both the dependency version changes and the generated hashes. Validate
the result exactly as CI installs it:

```console
python -m pip install --require-hashes -r requirements/ci.lock
python -m pip install --no-build-isolation --no-deps --editable .
python -m pip check
```
