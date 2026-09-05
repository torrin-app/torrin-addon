Run the file-selection regressions with the project environment:

```sh
uv sync
uv run python -m unittest discover -s tests -v
```

These exercise the real parser, discovery filter, debrid availability, metadata replacement and playback selection. Provider HTTP calls, catalog access and database writes are mocked; no credentials are required.
