# Contributing

Use Linux and Python 3.11+. The bridge has no third-party Python dependencies.

```sh
python3 -m unittest discover -s tests -v
python3 -m py_compile spark_workers.py worker_harnesses.py spark_proxy.py make_model_catalog.py install.py
```

Keep changes small. Job state, queueing, permissions, and Git operations belong in
`spark_workers.py`; harness protocol differences belong in `worker_harnesses.py`.
Prefer another small adapter to introducing a framework or protocol service.

For lifecycle changes, test a clean installation in a temporary prefix, including
paths with spaces. Use fake harnesses in the default suite so contributors need
no GPU or credentials. Live model tests are an explicit, separate check. Never
commit private profiles, credentials, sessions, or raw model logs.

Preserve old job records and single-worker configuration. Worker selection must
never silently change model providers or increase execution permissions. A
completed task must remain reviewable before application, and applying it must
preserve the user's Git index.

Describe the behavior change, checks run, and any version-specific protocol
assumptions in a contribution. If reporting a bug, include harness versions and
redacted status/error details rather than uploading an entire private state tree.
