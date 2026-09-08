# CI dependency lock

`ci.in` contains the exact top-level runtime, test, build, and supply-chain
tools used by CI. `ci.lock` is the fully resolved, hash-checked installation
input for Python 3.11-3.13 on Linux and Python 3.12 on Windows.

Regenerate the lock from the repository root with a clean Python 3.12
environment:

```console
python -m pip install "pip-tools==7.6.1"
python scripts/update_ci_lock.py
```

Review both the dependency version changes and the generated hashes. Validate
the result exactly as CI installs it:

```console
python -m pip install --require-hashes -r requirements/ci.lock
python -m pip install --no-build-isolation --no-deps --editable .
python -m pip check
```

The update script resolves exact versions with pip-compile and retrieves SHA-256
digests from the corresponding official PyPI release JSON over HTTPS. This avoids
downloading every foreign-platform wheel merely to compute its digest. Installation
still uses `--require-hashes` and verifies the bytes actually downloaded. An existing
pip-compile output can be supplied with `--resolved build/ci-pins.txt`.

The real semantic-model integration job explicitly installs its CPU inference
dependencies separately; these optional large packages are not part of the base
framework lock. Its public model weights are fixed to a source revision.
