# newsscore

News sentiment scoring for stocks, powered by TypeSafe **Jev** by default. Mainstream financial news APIs go in, a pluggable
scoring function (TypeSafe's **Jev** by default) scores every article, and one
aggregate number per symbol comes out. Use it from Python (sync or async) or from the
`newsscore` command line.

```
sources ──► fetch (async, concurrent) ──► de-dupe ──► score_fn (batched, cached) ──► aggregate ──► ScoreResult
```

## Install

```bash
pip install .            # library + CLI
pip install ".[jev]"     # also the TypeSafe SDK for the Jev scorer
```

Python 3.10+. Runtime dependencies: `httpx`, `typer`, `platformdirs`.

## API keys: the `.env` file

Copy `.env.example` to `.env` in the project (or working) directory and fill in the
keys you have:

```
TYPESAFE_API_KEY=...        # Jev scorer
FINNHUB_API_KEY=...
POLYGON_API_KEY=...
```

`newsscore` and `NewsScorer.from_config()` load it automatically. Real environment
variables always win, blank values are ignored, and the file is git-ignored. Lookup:
`$NEWSSCORE_ENV` alone if set, otherwise `./.env`, then `.env` in the user config directory. In code you
can also call `load_env("path/to/.env")` yourself before constructing sources.

## Quick start: CLI

```bash
# 1. save some sources (stored in a per-user JSON file, see `newsscore config-path`)
newsscore source add yahoo                       # keyless RSS, works immediately
newsscore source add finnhub --api-key YOUR_KEY
newsscore source add polygon                     # key taken from $POLYGON_API_KEY

# 2. score
newsscore score AAPL                             # all saved sources, last 7 days
newsscore score AAPL -s finnhub -s yahoo -d 3    # only these sources, last 3 days
newsscore score AAPL --scorer keyword -a 10      # force the offline scorer, show 10 articles
newsscore score AAPL --json | jq .score

# 3. look around
newsscore fetch AAPL                             # articles only, no scoring
newsscore source list | types | remove NAME
newsscore doctor
```

Sample output (real run, four free-tier sources, Jev scorer):

```
query       AAPL
window      2026-09-10 .. 2026-09-17  (7 days)
scorer      jev-v1
articles    310
score       +0.120   (-1 bearish .. +1 bullish)
confidence  1.000
by source   finnhub=+0.10  newsapi=+0.25  polygon=+0.28  yahoo=+0.10

+0.50 c=0.98 r=0.97  09-17 21:45 [yahoo]   Apple (AAPL) Outperforms Broader Market: What You Need to Know
-0.01 c=0.99 r=0.02  09-17 20:20 [yahoo]   Should You Buy HP Stock For The Shares It Keeps Retiring?
-0.98 c=0.97 r=0.97  09-17 16:44 [yahoo]   Apple (AAPL) Faces a $2.7 Billion UK Lawsuit over its App Tracking Rules
```

`c` is Jev's confidence and `r` its relevance; the HP article is correctly weighted
out. 310 articles took about 15 s to score the first time and under 2 s from cache.

## Quick start: Python

```python
from newsscore import NewsScorer

scorer = NewsScorer()                          # Jev if TYPESAFE_API_KEY is set, else keyword scorer
scorer.source_add("finnhub", api_key="...")
scorer.source_add("yahoo")                     # keyless
scorer.source_add("rss", name="ft", url="https://www.ft.com/companies?format=rss")

result = scorer.score("AAPL", days=7)          # blocking
print(result.score, result.confidence, result.n_articles)
for item in result.articles[:5]:
    print(item.score.score, item.article.title)
```

Inside an async pipeline use the `a`-prefixed methods; everything network-bound is
`async` and sources are fetched concurrently:

```python
import asyncio
from newsscore import NewsScorer

async def main():
    scorer = NewsScorer.from_config()          # loads the sources saved by the CLI
    results = await asyncio.gather(*(scorer.ascore(s, days=3) for s in ["AAPL", "MSFT", "NVDA"]))
    for r in results:
        print(r.query, f"{r.score:+.3f}", f"conf={r.confidence:.2f}", r.n_articles)
    await scorer.aclose()

asyncio.run(main())
```

Pass your own `httpx.AsyncClient` with `NewsScorer(http_client=client)` to share
connection pools with the rest of your pipeline. `result.to_dict()` gives plain JSON.

## Bring your own scoring function

The scorer is just a callable. Pass it as `score_fn`:

```python
from newsscore import NewsScorer, Article, ArticleScore, per_article

# batch form (preferred): one call per batch of up to `batch_size` articles
async def my_scorer(articles: list[Article], query: str) -> list[ArticleScore]:
    texts = [a.text for a in articles]
    probs = await my_model.predict(texts)               # your code
    return [ArticleScore(score=2 * p - 1, confidence=abs(2 * p - 1)) for p in probs]

my_scorer.name = "my-model-v3"                          # stable cache key
scorer = NewsScorer(score_fn=my_scorer, batch_size=32, concurrency=2)

# or one article at a time
scorer = NewsScorer(score_fn=per_article(lambda a, q: 0.5 if "beat" in a.text.lower() else 0.0),
                    scorer_name="beat-rule")
```

### Contract

| | |
|---|---|
| **Signature** | `fn(articles: Sequence[Article], query: str)`, plain or `async` |
| **Input** | `articles`: the batch (see `batch_size`, default 16). `query`: the symbol or keyword they were fetched for. Each `Article` has `id`, `source`, `title`, `published` (UTC), `url`, `summary`, `symbols`, `raw` (vendor payload) and `text` (title + summary). |
| **Output** | A sequence with **one item per input article, same order**. Each item is an `ArticleScore`, **or** a number in `[-1, 1]`, **or** a dict `{"score": float, "confidence"?: float, "relevance"?: float, "labels"?: dict}`. |
| `score` | `-1.0` very bearish .. `+1.0` very bullish. |
| `confidence` | `[0, 1]`, how sure the scorer is. Aggregation weight. Default `1.0`. |
| `relevance` | `[0, 1]`, how much the article is about `query`. Aggregation weight. Default `1.0`. |
| `labels` | Anything you want to keep (category, probabilities, model version). Stored in the cache. |
| **Errors** | An exception fails only that batch. The message lands in `ScoreResult.errors`; other batches proceed. |
| **Caching** | Results are cached under `(scorer name, query, article id)`. Set `fn.name` or `NewsScorer(scorer_name=...)`; anonymous lambdas are not cached. |

The full contract also lives in the docstring of `newsscore/scoring/protocol.py`.

## Built-in scorers

| name | needs | what it does |
|---|---|---|
| `jev` (default when available) | `pip install ".[jev]"`, `TYPESAFE_API_KEY` | One Jev `system_one` call per article asking a 5-level sentiment `Score`, a relevance `Noul`, an event `Choice` (earnings, guidance, M&A, legal, product, analyst, management, macro, other) and a novelty `Noul`. Score is the probability-weighted level rescaled to `[-1, 1]`; confidence is Jev's calibrated confidence. |
| `keyword` | nothing | Small Loughran-McDonald-style lexicon with negation handling. Offline fallback and test double, not a trading signal. |

Select explicitly with `NewsScorer(score_fn="keyword")` or `newsscore score --scorer keyword`.
Tune Jev with `NewsScorer(score_fn=JevScorer(concurrency=16, model="jev-latest"))`.

## News sources

| type | key env var | query | notes |
|---|---|---|---|
| `finnhub` | `FINNHUB_API_KEY` | ticker | company-news endpoint |
| `alpha_vantage` | `ALPHAVANTAGE_API_KEY` | ticker | vendor sentiment kept in `Article.raw` |
| `polygon` / `massive` | `POLYGON_API_KEY` or `MASSIVE_API_KEY` | ticker | Polygon.io is now Massive.com; same keys. Follows `next_url`; option `max_pages` (5) |
| `tiingo` | `TIINGO_API_KEY` | ticker | option `limit` (1000) |
| `marketaux` | `MARKETAUX_API_KEY` | ticker | option `max_pages` (3), `language` |
| `newsapi` | `NEWSAPI_API_KEY` | keyword | pass a company name; option `max_pages` (1), `language` |
| `rss` | none | keyword | option `url` (use `{query}` as placeholder), `match` (true) |
| `yahoo` | none | ticker | Yahoo Finance headline RSS preset |

Add a source by type and options:

```bash
newsscore source add marketaux --api-key KEY -o max_pages=5
newsscore source add rss --name sec -o url="https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&output=atom"
```

or in code: `scorer.source_add("marketaux", api_key="KEY", max_pages=5)`.

### Writing a source

```python
from newsscore import NewsSource, register

@register
class MySource(NewsSource):
    type_name = "mine"
    env_key = "MINE_API_KEY"          # or requires_key = False

    async def fetch(self, query, since, until, client):
        data = await self._get_json(client, "https://...", params={"q": query, "key": self.api_key})
        return [self._article(title=d["title"], published=parse_dt(d["ts"]), url=d["url"]) for d in data]
```

`since`/`until` are aware UTC datetimes; `client` is a shared `httpx.AsyncClient`.
Registered types are available to the CLI too.

## How the aggregate is computed

```
weight_i   = confidence_i * relevance_i * 0.5 ** (age_hours_i / half_life_hours)   # half_life 48h
score      = sum(weight_i * score_i) / sum(weight_i)
confidence = 1 - exp(-sum(weight_i) / 3)      # ~0.63 with three solid fresh articles
```

Change the half-life with `NewsScorer(half_life_hours=24)` or replace the whole thing
with `NewsScorer(aggregate_fn=my_fn)` where `my_fn(scored, now) -> Aggregate`.

## Caching and configuration

* **Scores** are cached in SQLite at `platformdirs.user_cache_dir("newsscore")/scores.sqlite`
  (override with `NEWSSCORE_CACHE`; disable with `cache=False` or `--no-cache`; wipe with
  `newsscore cache-clear`). Re-running a query only pays for unseen articles, and
  back-tests can replay from the cache.
* **Sources** saved by the CLI live in `platformdirs.user_config_dir("newsscore")/sources.json`
  (override with `NEWSSCORE_CONFIG`). API keys may be left out and supplied via env vars.

## Development

```bash
uv sync                  # creates .venv with the package, the Jev SDK and test tools
uv run newsscore doctor
uv run pytest
```

Or with plain pip: `pip install -e ".[jev,dev]"` then `pytest`.

Test artefacts (per-test config files and score caches) are written under
`E:\test_data\jev_sentiment` when that drive exists; set `NEWSSCORE_TEST_DATA` to move them,
or they fall back to pytest's temporary directory.

The design notes are in [PLAN.md](PLAN.md).

## Status: what has and has not been tested live

This is a 0.1 release built and verified on one machine with the API plans its
author happens to have. Every source has unit tests against fixture payloads that
follow the provider's documented response shape, but only some have been run
against the real endpoint.

| Component | Unit tests | Live-tested | Notes |
|---|---|---|---|
| `finnhub` | yes | **yes** | free tier, 237 AAPL articles / 7 days |
| `polygon` / `massive` | yes | **yes** | free tier via `api.massive.com`, pagination exercised |
| `newsapi` | yes | **yes** | developer plan (24 h delay) |
| `yahoo` RSS | yes | **yes** | keyless |
| `jev` scorer | yes (fake client) | **yes** | 310 articles scored with a real `TYPESAFE_API_KEY` |
| `alpha_vantage` | yes | **no** | no key available; quota-message handling untested live |
| `marketaux` | yes | **no** | no key available; page-size behaviour on the free plan unverified |
| `tiingo` | yes | **no** | live call returned 403 on the free plan, so the parser has never seen real data |
| generic `rss` (Atom) | yes | **no** | Atom branch only covered by a fixture; RSS 2.0 covered via Yahoo |
| `keyword` scorer | yes | n/a | offline |

Untested does not mean broken, but field names and pagination details are exactly
where providers drift from their docs. If you hold a key for one of the untested
sources, running `newsscore fetch AAPL -s <source>` and reporting the outcome is the
single most useful contribution right now.

## Contributing

Testers, bug reports and pull requests are all welcome.

* **Testers.** Run `newsscore doctor`, then `newsscore fetch` and `newsscore score` against
  any source you have a key for. Open an issue with the provider, plan tier, the
  command, and the output (redact your key). A short "works for me" note is useful too.
* **New sources.** Subclass `NewsSource`, implement `fetch`, register the class, add
  a fixture test in `tests/test_sources.py` and a row to the sources table above.
  See "Writing a source" for the shape.
* **New scorers.** Anything matching the `ScoreFn` contract can be added to
  `newsscore/scoring/` and registered in `SCORERS`.
* **Pull requests.** Keep them focused, run `uv run pytest` before pushing, and
  update the README table when you change what is tested. Don't commit `.env`.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the short version of the workflow.

## Caveats

Jev launched in September 2026 and its accuracy on financial text has not been
independently benchmarked. Build a small hand-labelled set and compare `jev`
against your alternatives before trusting any signal. Headline sentiment on large
caps is largely priced in within minutes; treat the aggregate as a research input,
not a trade trigger.
