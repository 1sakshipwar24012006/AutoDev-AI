# Sample buggy shop app

Used by `workflow-agent demo`. It intentionally contains two bugs in `shop/pricing.py`
(see `issue.md`) that the bundled tests expose:

```
python -m pytest -q        # fails before the agent runs
```

`issue.md` is the local "GitHub issue" used in mock mode. It is not copied into the
demo repository.
