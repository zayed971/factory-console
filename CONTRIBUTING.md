# Contributing

Run the tests from the repository folder on Windows with Python 3.11 or newer:

```console
python -m unittest discover -s tests -v
```

Keep tests fast (under 30 seconds), isolated, and based on `unittest`. Use fake
process rows and mocked commands; never suspend or terminate real processes in
tests. HTTP integration tests must use a temporary loopback server, with no
internet dependency. Test fixtures and servers must be cleaned up.

Keep `server.py` and `factory_feed.py` standard library only. Keep the page
dependency-free, with no external scripts, fonts, stylesheets, or build step.
Follow the surrounding style and include regression tests for fixes.

Protect privacy: never commit `config.json`, `state.json`, or `feeds/` contents.
Use `config.example.json` and synthetic fixtures instead of personal paths,
process details, or job logs.
