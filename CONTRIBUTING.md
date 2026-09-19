# Contributing

Thanks for looking at newsscore. Here is the short version.

## Setup

```bash
git clone https://github.com/mahynotch/newsscore.git && cd newsscore
uv sync                     # venv with the package, the Jev SDK and test tools
cp .env.example .env        # fill in whatever keys you have; blanks are ignored
uv run newsscore doctor
uv run pytest
```

## Most wanted: live testers

Every source except Tiingo has now been run against its real endpoint, on the free
tier of each provider. The gaps that remain are **Tiingo news**, which needs a plan
with the news add-on, and any provider on a *paid* tier, where pagination and rate
limits behave differently from anything tested here. If that is you, please run:

```bash
uv run newsscore source add <type>
uv run newsscore -v fetch AAPL -s <type> -d 7
uv run newsscore score AAPL -s <type> --scorer keyword -a 5
```

and open an issue with the provider, your plan tier, and the output. Errors already
have your key stripped out, but check anything you paste by hand.
"Works as expected" is as valuable as a bug report.

## Pull requests

1. One topic per PR. Small is good.
2. Add or update a test. Sources get a fixture test in `tests/test_sources.py`;
   scorers and engine changes go in `tests/test_core.py` or `tests/test_scorer.py`.
   A source that raises from a provider response must keep the key out of the
   message -- pass `secret=` to `SourceError`.
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
