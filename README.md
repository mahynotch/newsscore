# newsscore

News sentiment scoring for stocks, powered by TypeSafe **Jev** by default. Mainstream financial news APIs go in, a pluggable
scoring function (TypeSafe's **Jev** by default) scores every article, and one
aggregate number per symbol comes out. Use it from Python (sync or async) or from the
`newsscore` command line.

```
sources ──► fetch (async, concurrent) ──► de-dupe ──► score_fn (batched, cached) ──► aggregate ──► ScoreResult
```

## Install

From PyPI:

```bash
pip install "newsscore[jev]"     # with the Jev scorer (recommended)
pip install newsscore            # without it; keyword scorer or your own score_fn
```

Or with uv: `uv add "newsscore[jev]"`, or `uv tool install "newsscore[jev]"` for the CLI only.

The latest unreleased code is on GitHub, and a clone is the way to hack on it:

```bash
pip install "newsscore[jev] @ git+https://github.com/mahynotch/newsscore.git"   # main branch

git clone https://github.com/mahynotch/newsscore.git && cd newsscore
pip install -e ".[jev,dev]"     # editable, with the Jev SDK and the test tools
```

Python 3.10+. Runtime dependencies: `httpx`, `typer`, `platformdirs`.

### Two ways to reach Jev

`newsscore` can call Jev either directly or through OpenRouter. The questions asked are
identical -- one definition renders into both wire formats -- so the only differences
are the credential, the dependency and who bills you.

| | `provider="typesafe"` | `provider="openrouter"` |
|---|---|---|
| credential | `TYPESAFE_API_KEY` | `OPENROUTER_API_KEY` |
| needs `[jev]` extra | yes | **no**, just `httpx` |
| model id | `jev-1.13.0` | `typesafe/jev-1.13` |
| endpoint | `POST /v1/systemone` | `POST /api/alpha/decisions` |
| sign-up | waiting list | open |

```bash
newsscore score AAPL --scorer jev --provider openrouter
```

```python
NewsScorer("jev", scorer_options={"provider": "openrouter"})
```

Leave it unset and whichever key you have is used, preferring TypeSafe, so an existing
setup keeps working untouched. `newsscore doctor` reports both routes.

#### Do the two routes agree?

Measured, rather than assumed. The same 310 AAPL articles were scored down both routes:

| | TypeSafe vs OpenRouter | same route, scored twice |
|---|---|---|
| impact label agreement | 97.1% (kappa 0.934) | 96.7% (kappa 0.92) |
| category agreement | 97.7% | 99.3% |
| sentiment, max abs. difference | 0.095 | 0.095 |
| tokens per article | 1241 | 1241 |
| aggregate over all 310 | +0.1160 | +0.1170 |

**The gap between providers is no bigger than the model's own run-to-run variation**,
so the benchmark in [Is impact worth asking for?](#is-impact-worth-asking-for) carries
over. That is evidence, not a guarantee: OpenRouter resolves `typesafe/jev-1.13` to a
dated build (`typesafe/jev-1.13-20260917`, visible in `labels["model"]`), and it is
free to resolve it elsewhere later. Pin the dated id yourself if that matters --
OpenRouter accepts it directly.

The provider is therefore part of the cache fingerprint, and scores fetched through one
route are never reused for the other. Switching providers means rescoring.

OpenRouter spells its floating aliases with a leading `~`, so the moving pointer is
`~typesafe/jev-latest` and the pinned versions are `typesafe/jev-1.13` and the dated
`typesafe/jev-1.13-20260917`. As on TypeSafe, **asking for an alias switches caching
off** rather than filing answers under a name that may mean something else tomorrow:

```python
JevScorer(provider="openrouter", model="~typesafe/jev-latest").fingerprint is None
```

One more caveat: OpenRouter's Decisions endpoint is **alpha**, so its shape can change
without a deprecation period.

### Do I need the `[jev]` extra?

Only for the default scorer. `[jev]` adds one package, `typesafe-sdk`, and `JevScorer` is
the only thing that imports it — sources, aggregation, caching, the CLI and your own
`score_fn` all work without it.

| you want | install | also needed |
|---|---|---|
| Jev scoring (the point of this package) | `[jev]` | `TYPESAFE_API_KEY` |
| your own model as `score_fn` | plain | nothing |
| a look at the plumbing, offline | plain | nothing — you get the `keyword` scorer |

`NewsScorer()` uses Jev when the SDK is importable **and** `TYPESAFE_API_KEY` is set, and
otherwise falls back to the `keyword` lexicon scorer with only a log line. That fallback is
a test double, not a trading signal, so run `newsscore doctor` to see which one you have
before trusting a number.

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
newsscore fetch AAPL --out news.json             # ...saved as JSON instead of printed
newsscore score AAPL --out aapl.json             # full result, every scored article
newsscore source list | types | remove NAME
newsscore doctor                                 # shows every path in use
```

Sample output (real run, four free-tier sources, Jev scorer):

```
query       AAPL
window      2026-09-10 .. 2026-09-17  (7 days)
scorer      jev-v1
articles    310
status      ok
counts      fetched=326 deduplicated=16 filtered=0 submitted=310 scored=310 failed=0
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
| **Output** | A sequence with **one item per input article, same order**. Each item is an `ArticleScore`, **or** a number in `[-1, 1]`, **or** a dict `{"score": float, "confidence"?: float, "relevance"?: float, "labels"?: dict}`, **or** a `ScoreItemError` to fail that one article. |
| `score` | `-1.0` very bearish .. `+1.0` very bullish. |
| `confidence` | `[0, 1]`, how sure the scorer is. Aggregation weight. Default `1.0`. |
| `relevance` | `[0, 1]`, how much the article is about `query`. Aggregation weight. Default `1.0`. |
| `labels` | Anything you want to keep (category, probabilities, model version). Stored in the cache. |
| **Errors** | Return a `ScoreItemError` to fail one article and keep its siblings. Raising fails the batch, which the engine then retries and splits to isolate the bad article. Either way the failure lands in `ScoreResult.failures` and never enters the aggregate as neutral. |
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
weight_i   = confidence_i * relevance_i * impact_i * 0.5 ** (age_hours_i / half_life_hours)   # half_life 48h
score      = sum(weight_i * score_i) / sum(weight_i)
confidence = 1 - exp(-sum(weight_i) / 3)      # ~0.63 with three solid fresh articles
```

`impact_i` is `1.0` unless you switch on impact weighting, so by default this is
exactly the confidence/relevance/decay scheme and nothing else.

Every term is configurable, and nothing about the defaults changed when these knobs
were added:

| control | default | what it does |
|---|---|---|
| `half_life_hours` | `48` | decay half-life; `0` disables decay |
| `lookback_hours` | `None` | ignore articles older than this when aggregating |
| `use_relevance` | `True` | set `False` to drop the relevance term |
| `impact_weights` | `None` | see [Expected impact](#expected-impact) |
| `aggregate_fn` | built-in | replace the scheme entirely |

`lookback_hours` bounds what is *aggregated*; `days`/`since` bound what is *fetched*.
They are separate on purpose: you can collect a week of news and still aggregate only
the last 48 hours of it without re-fetching.

The reference time is explicit everywhere — `until` for `score`, `as_of` for
`score_articles` and `aggregate_scored` — so a run replays deterministically. Articles
published after it are excluded rather than counted as the freshest evidence available.

### Auditing an aggregate

Every run reports, per article, each weight term and a normalised `contribution`.
**The contributions sum to the reported score**, so any aggregate can be traced to the
articles and the term that moved it:

```
 score    conf   rel  impact  decay   weight   contrib   headline
 +0.99   0.99  0.97    1.0  0.973    0.934   +0.3157   Apple Stock Gets Stunning Price Target Hike
 +0.47   0.94  0.95    1.0  0.969    0.866   +0.1367   Apple Stock Near Buy Point As iPhone 18 ...
 +0.49   0.87  0.60    1.0  0.976    0.509   +0.0857   GM is re-downloading Apple CarPlay ...
 +0.42   0.58  0.95    1.0  0.970    0.534   +0.0753   Apple will essentially sell 'every single'...
 +0.29   0.65  0.16    1.0  0.967    0.101   +0.0101   Sector Update: Tech Stocks Rise Late ...
                                                 sum =   +0.6235
```

```python
result.total_weight          # 2.944
result.reconciles()          # True
[c.to_dict() for c in result.contributions]
```

`reconciles()` is also true, vacuously, for a custom `aggregate_fn`: contributions
summing to the score is a property of a weighted mean, not of aggregation in general,
so your own function is never asked to provide them and reports none.

A zero `total_weight` is reported as `status="no_weight"`, which is deliberately not
the same as a genuine `0.0` — see [When a run goes wrong](#when-a-run-goes-wrong).

### Reproducing another aggregator

Comparing schemes needs no model calls at all. `quant_aggregator()` is the scheme used
by the existing `quant` pipeline — confidence x impact x decay, a 12-hour half-life, a
48-hour lookback and no relevance term — so it can be reproduced exactly without this
library changing its own general-purpose defaults:

```python
from newsscore import NewsScorer, quant_aggregator

saved = json.loads(Path("aapl.json").read_text())
theirs = NewsScorer(aggregate_fn=quant_aggregator()).aggregate_scored(
    saved["articles"], saved["query"], as_of=saved["until"]
)                                                  # +0.6034 against the default's +0.6235
```

From the command line, `--half-life`, `--lookback`, `--no-relevance` and
`--impact-weighting` cover the same ground.

## Scoring news you already have

If your pipeline already collects articles, skip the fetching entirely and use the
scoring and aggregation on their own. Same scorer, same cache, same aggregator, so
results are directly comparable with the fetching path.

```python
from newsscore import NewsScorer

scorer = NewsScorer()                      # no sources needed
result = scorer.score_articles(my_articles, "AAPL", as_of=cutoff)
for item in result.articles:
    print(item.article.id, item.score.score, item.score.confidence)
```

`my_articles` are `Article` objects or plain dicts shaped like `Article.to_dict()`.
Only `title` and `published` are required; `id`, `source`, `url`, `summary` and
`symbols` are **carried through exactly as given** and never regenerated, so the
outcome for each article binds to your own id. Use `ascore_articles` in async code.

* **`as_of`** is the reference time for decay *and* the eligibility cutoff. Articles
  published after it are counted as `filtered`, not treated as fresh evidence. Fix it
  to replay a run deterministically.
* **One article, two targets is two tasks.** `score_articles(arts, "AAPL")` and
  `score_articles(arts, "MSFT")` are scored, cached and reported independently —
  relevance alone can differ enough to matter.
* **Every input is accounted for** in `result.counts`, which is why `filtered` and
  `deduplicated` are reported separately. Pass `dedupe=False` to keep syndicated copies.

### From the command line

```bash
newsscore score-articles news.json -q AAPL                  # a file
cat news.json | newsscore score-articles -q AAPL --json     # or stdin
newsscore fetch AAPL --out news.json                        # this output is valid input
```

The input is either a JSON array of articles or an object carrying the target with them:

```json
{"query": "AAPL", "as_of": "2026-09-18T12:00:00Z", "articles": [
  {"id": "a1", "source": "internal", "title": "Apple beats estimates",
   "published": "2026-09-17T14:00:00Z", "url": "https://...", "symbols": ["AAPL"]}
]}
```

`--scorer`, `--half-life`, `--no-cache`, `--articles`, `--json`, `--out` and `--fail-on`
work exactly as they do for `newsscore score`.

### Re-aggregating without paying again

Changing how scores are combined needs no model calls at all. Hand the scored articles
back with different settings:

```python
import json
saved = json.loads(Path("aapl.json").read_text())

NewsScorer(half_life_hours=12).aggregate_scored(saved["articles"], saved["query"],
                                                as_of=saved["until"])
```

`aggregate_scored` is synchronous, never touches the network or the scorer, and returns
a full `ScoreResult`. It is the cheap way to sweep a half-life over a saved run, or to
re-score history after changing only the aggregation.

## Expected impact

Jev answers a second, separate question about every article: how *material* is this
news for the target, over the next 1–5 trading days? That lands on `ArticleScore` as
`expected_impact` — `"low"`, `"medium"`, `"high"`, or `None` from a scorer that does
not judge impact — with the full distribution in `labels["impact_probs"]`.

Impact is not sentiment. Sentiment is direction and strength; impact is how much the
news should move your view at all. A lawsuit is strongly bearish **and** high impact; a
routine supplier contract is mildly bullish and low impact. The rubric says this
explicitly, because a model asked casually will otherwise just read impact off the
strength of the sentiment.

```python
for item in result.articles:
    s = item.score
    print(f"{s.score:+.2f} {s.expected_impact or '-':>6}  {item.article.title}")
```

**The question is off by default**, because it costs about 24% more tokens per article
and the label does nothing unless you also switch on impact weighting. Ask for it when
you intend to use it:

```python
NewsScorer("jev", scorer_options={"impact": True})    # ask for it: ~24% more tokens
JevScorer(impact=True, horizon="the next trading day")   # ask about a different period
JevScorer(impact=True, impact_rubric={...})              # reword the three levels
```

or `--impact` on the command line. With the question off, `expected_impact` is `None`
everywhere and nothing else changes -- see [Is impact worth
asking for?](#is-impact-worth-asking-for) for the evidence.

The rubric keys stay `low`/`medium`/`high` -- only their descriptions are yours, since
`ArticleScore.expected_impact` and the weight mapping are defined on those three. The
horizon, the rubric wording and whether the question is asked at all are **all part of
the cache fingerprint**, so changing any of them rescores rather than reusing answers
given to a different question.

### Weighting by impact

Off by default. Pass a mapping to switch it on, in code or with `--impact-weighting`:

```python
from newsscore import NewsScorer, IMPACT_WEIGHTS       # {"high": 3.0, "medium": 1.5, "low": 1.0}

scorer = NewsScorer(impact_weights=IMPACT_WEIGHTS)     # or your own mapping
```

Articles whose scorer supplies no impact weigh `1.0`, so enabling this changes only the
articles that actually carry a judgement, and the `keyword` scorer or your own function
keeps working untouched.

### Is impact worth asking for?

Short answer: **the label is real, but nothing shows it makes your number better.** It
is off by default for that reason. The measurements below are from this repository's
benchmark against the live Jev API on real news.

**It is not a restatement of sentiment.** On 308 live AAPL articles, the best possible
two-threshold rule on `|sentiment|` reproduces the impact label 71.8% of the time
against a 70.1% majority-class baseline -- a 1.7pp gain, i.e. none. Normalised mutual
information between `|sentiment|` and impact is 0.215, and AUC for ranking `high`
against the rest is 0.70. Category and relevance each explain about as much as
sentiment does. So the question really is asking something else.

**It is reproducible.** Scoring the same 150 articles twice with identical settings
changed the label 3.3% of the time, Cohen's kappa 0.92. The signal is not sampling noise.

**Asking it does not disturb the other answers.** Differences in sentiment, confidence
and relevance between the impact-on and impact-off arms are indistinguishable from the
run-to-run noise floor (max |delta| 0.100 against 0.095 measured on identical repeats,
median zero, no directional bias).

**Most articles are `low`.** 70.3% low, 24.5% medium, 5.2% high. Under `IMPACT_WEIGHTS`
only 30% of articles get a weight other than `1.0`.

**It moves the answer a little, and sometimes flips it.**

| basket size | median abs. change | p90 | sign flips |
|---|---|---|---|
| 5 articles | 0.020 | 0.094 | 4.2% |
| 10 articles | 0.033 | 0.111 | 7.2% |
| 20 articles | 0.028 | 0.086 | 6.8% |
| 40 articles | 0.021 | 0.068 | 8.5% |

**But it is not validated against what actually happened.** On 597 articles across 20
symbols, using split- and dividend-adjusted daily closes, and measuring each article's
move as a residual against SPY scaled by the symbol's own residual volatility:

| | low | medium | high | high - low | p |
|---|---|---|---|---|---|
| news day + next | 0.680 | 0.645 | 0.740 | +0.060 | 0.219 |
| 5 days forward | 0.490 | 0.617 | 0.677 | +0.187 | 0.085 |

Residualising matters here: on raw returns every one of these differences washes out,
because three weeks of megacap moves are mostly market beta.

The five-day ordering is monotone and in the right direction, which is what a weak but
real signal looks like; it is not significant at conventional levels, and the one-day
test is flat. Switching impact weighting on did not improve how well the daily
aggregate tracked the forward residual return (Spearman -0.036 to -0.032 -- both
indistinguishable from zero, in a window where plain sentiment had no forward power
either). With only 18 `high`-impact articles in the sample this study is underpowered:
settling it at conventional significance would take on the order of 15,000 scored
articles, given a ~3-5% base rate for `high`.

So: a stable, non-redundant label, of unproven value, that costs 24% more and changes
your number by about 0.03. Turn it on if you plan to validate it on your own universe.

Before you put a 3× weight on `high`, label a few hundred headlines yourself and check
the labels agree with you. See the caveat at the end of this file.

## What a cached score belongs to

A score is reused only when it would be identical to recompute. The cache key covers
the scorer, its **contract fingerprint**, the target, the article id *and* a digest of
the article's text:

| change | old scores reused? |
|---|---|
| same everything | **yes** — served locally, no request |
| headline edited under the same url | no — content is part of the identity |
| different target symbol | no — a separate scoring task |
| rubric, horizon or question wording changed | no — the model was asked something else |
| model version changed | no |

The last three are what `fingerprint` buys you. A scorer publishes one as
`fn.fingerprint`; set it to a digest of whatever changes your answers, or leave the
attribute off to cache on the scorer name alone as before.

### The model is pinned

`JevScorer` requests a pinned version (`jev-1.13.0`) rather than `jev-latest`. This is
deliberate: the API resolves an alias server-side and only tells you which version
answered *after* the call, so an alias can never be part of a cache lookup — a silent
model swap would keep serving the old model's scores forever. Ask for an alias anyway
and the scorer refuses to cache rather than store answers it cannot attribute:

```python
JevScorer(model="jev-latest").fingerprint is None      # -> caching disabled, with a warning
```

Expect scores to move when the pinned default is raised; the cache invalidates wholesale
at that point, which is the correct behaviour rather than a bug.

### Provenance

Every score carries where it came from, in `labels`: `requested_model` and the `model`
that actually answered, `request_id`, `latency_ms`, `usage`, and `local_cache_hit`.

`local_cache_hit` refers to **newsscore's own SQLite cache**, not any provider-side
cache: a hit means no request was made at all. Run-level `result.usage` counts only
what this run actually spent, so cached articles contribute nothing to it, while what
they originally cost stays on the article for auditing.

```python
result.usage                  # {"input_tokens": 1663, "output_tokens": 279, "requests": 2}
```

### Two different confidences

`ArticleScore.confidence` is the scorer's own calibrated confidence in one article's
answer. `ScoreResult.confidence` — also available as the clearer
`ScoreResult.evidence_confidence` — is an *aggregate evidence* measure that grows with
how much weighted evidence there is. They are different quantities. Neither is a
probability that a trade will be profitable, and one is not a substitute for the other
in a review threshold.

## When a run goes wrong

A sentiment of `0.0` can mean two completely different things, so every result carries
a `status` and a reconcilable set of `counts`. Check the status before acting on the
score.

| `status` | meaning | `score` is |
|---|---|---|
| `ok` | every article scored, and the aggregate rests on real weight | a reading; `0.0` here is genuinely neutral news |
| `partial` | some articles failed, the rest carry weight | a reading over fewer articles than you asked for |
| `no_weight` | articles scored, but nothing carried weight (all zero confidence or relevance) | **not** a reading; `0.0` means "no evidence" |
| `all_failed` | every scoring attempt failed | **not** a reading |
| `no_articles` | nothing matched the window | **not** a reading |

```python
result = scorer.score("AAPL")
if not result.ok:                       # True only for status == "ok"
    print(result.status, result.counts, result.failures)
```

`counts` reconciles exactly: `fetched == deduplicated + filtered + submitted` and
`submitted == scored + failed`, counted in scoring tasks (one article for one query).
Every task that did not produce a score appears in `result.failures` with its article
id, an `error_type` and the number of attempts made.

### Failures are isolated, not fatal

One bad article never costs you its batch. A scoring function reports a single failure
by returning a `ScoreItemError`; one that raises instead is retried, then split in half
repeatedly until the offending article is alone, so its siblings still get scored — and
**successes are cached even when siblings fail**, so a rerun never pays for them twice.

Retryable failures (timeouts, rate limits, 5xx) are retried with exponential backoff and
jitter: `NewsScorer(retries=2, retry_backoff=0.5)`. Run-wide problems such as bad
credentials stop the run instead of repeating the same rejection once per headline.

### Strict exits for pipelines

By default the CLI always exits 0. `--fail-on` turns the status into an exit code while
still writing the JSON and the diagnostics:

```bash
newsscore score AAPL --json --fail-on unusable    # exit 3 if there is no usable aggregate
newsscore score AAPL --json --fail-on partial     # exit 4 as well if any article failed
```

Asking for a scorer by name that cannot run here is an error, not a downgrade:
`--scorer jev` without `TYPESAFE_API_KEY` exits 1 with a message rather than silently
scoring with the keyword lexicon. Only the automatic default falls back, and only when
you did not name a scorer.

## Where things are stored

`newsscore` writes nothing you did not ask for. Run `newsscore doctor` to print the
actual paths on your machine.

| what | where | how to change it |
|---|---|---|
| **fetched articles** | nowhere — printed to the terminal | `--out FILE` to save them as JSON, or `--json` and redirect |
| **scored results** | nowhere — printed to the terminal | `--out FILE`, or `--json` and redirect |
| **article scores** (the cache) | `platformdirs.user_cache_dir("newsscore")/scores.sqlite` | `NEWSSCORE_CACHE`; `--no-cache` / `cache=False` to skip it; `newsscore cache-clear` to wipe it |
| **saved sources** | `platformdirs.user_config_dir("newsscore")/sources.json` | `NEWSSCORE_CONFIG`; `newsscore config-path` to print it |
| **API keys** | `.env` in the working directory | `NEWSSCORE_ENV`, or real environment variables |

### Saving articles and results

```bash
newsscore fetch AAPL -d 7 --out aapl-articles.json     # every article, one JSON array
newsscore score AAPL --out runs/aapl-2026-09-18.json   # aggregate + every scored article, dirs created
```

`--out` creates missing parent directories and always writes UTF-8, which the shell's
own `>` redirection does not reliably do on Windows. In Python, `result.to_dict()` and
`article.to_dict()` give the same structures, so you can send them wherever you like:

```python
import json
result = scorer.score("AAPL", days=7)
Path("aapl.json").write_text(json.dumps(result.to_dict(), indent=2, default=str), encoding="utf-8")
```

### The score cache

Scores — not articles — are cached under `(scorer name, query, article id)`, so re-running
a query only pays for articles you have not seen before, and back-tests replay for free.
It is an ordinary SQLite file; point `NEWSSCORE_CACHE` at a project directory if you want
one cache per research project rather than one per user.

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

## Status: what has and has not been tested live

This is a 0.2 release built and verified on one machine with the API plans its
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

`expected_impact` is new and unvalidated. On a handful of hand-written headlines it
behaves sensibly — effusive but immaterial CEO language comes back `low`, a revenue
restatement comes back `high` — but on that sample impact and the magnitude of the
sentiment score did not visibly come apart, so nothing here demonstrates that it
carries information beyond "how strong is this news". Build a labelled set before you
let `high` carry a 3× weight in anything that trades.

Jev launched in September 2026 and its accuracy on financial text has not been
independently benchmarked. Build a small hand-labelled set and compare `jev`
against your alternatives before trusting any signal. Headline sentiment on large
caps is largely priced in within minutes; treat the aggregate as a research input,
not a trade trigger.
