# Contributing

Thanks for looking at newsscore. Here is the short version.

## Setup

```bash
git clone <this repo> && cd newsscore
uv sync                     # venv with the package, the Jev SDK and test tools
cp .env.example .env        # fill in whatever keys you have; blanks are ignored
uv run newsscore doctor
uv run pytest
```

## Most wanted: live testers

Several sources have only been tested against fixture payloads. If you have a key
for Alpha Vantage, Marketaux, Tiingo (news-enabled plan) or a non-Yahoo RSS/Atom
feed, please run:

```bash
uv run newsscore source add <type>
uv run newsscore -v fetch AAPL -s <type> -d 7
uv run newsscore score AAPL -s <type> --scorer keyword -a 5
```

and open an issue with the provider, your plan tier, and the output (redact the key).
"Works as expected" is as valuable as a bug report.

## Pull requests

1. One topic per PR. Small is good.
2. Add or update a test. Sources get a fixture test in `tests/test_sources.py`;
   scorers and engine changes go in `tests/test_core.py` or `tests/test_scorer.py`.
3. `uv run pytest` must pass.
4. Update the README (sources table, scorers table, or the live-test status table)
   when behaviour or coverage changes.
5. Never commit `.env`, API keys, or cache files. `.gitignore` covers the usual paths;
   `git diff --cached` before committing is a good habit.

## Style

Plain Python 3.10+, type hints, dataclasses, docstrings that say *why*. No new
runtime dependencies without a reason in the PR description. Async for anything
that touches the network; sync wrappers stay thin.

## Reporting bugs

Include the command or code, the full error, the provider and plan if a source is
involved, and `uv run newsscore --version`.
