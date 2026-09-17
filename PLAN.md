# jev_sentiment — implementation plan

A small, installable Python library and CLI that pulls company news from mainstream
financial news APIs, scores each article with a pluggable scoring function (Jev by
default), and aggregates the result into a single sentiment score per symbol or keyword.

## 1. Goals

| Goal | How it shows up |
|---|---|
| Installable | `pip install .` gives the `jev_sentiment` package and the `jevsent` CLI. |
| Two entry points | `NewsScorer` object for code, `jevsent` for the shell. Same engine underneath. |
| Mainstream sources | Finnhub, Alpha Vantage, Polygon, Tiingo, Marketaux, NewsAPI, plus generic RSS. |
| Pluggable scoring | Any callable matching the `ScoreFn` contract. Jev scorer and a keyword fallback ship in the box. |
| Async first | Every network call is `async`; sync wrappers are thin `asyncio.run` shims. |
| Persistent sources | CLI stores sources in a per-user JSON file. Empty `--source` means "all". |
| Neat and efficient | Small modules, dataclasses, one shared `httpx.AsyncClient`, SQLite score cache, concurrent fetch and scoring. |

## 2. Package layout

```
jev_sentiment/
├── pyproject.toml
├── README.md
├── PLAN.md
├── src/jev_sentiment/
│   ├── __init__.py          # public API re-exports
│   ├── models.py            # Article, ArticleScore, ScoredArticle, ScoreResult
│   ├── scorer.py            # NewsScorer: source_add / source_remove / score / ascore
│   ├── aggregate.py         # confidence-weighted, time-decayed aggregation
│   ├── cache.py             # SQLite cache of per-article scores
│   ├── config.py            # per-user source store (JSON) + env-var API key fallback
│   ├── http.py              # shared httpx client factory with retries
│   ├── cli.py               # typer app: `jevsent source add|list|remove`, `jevsent score`, `jevsent fetch`
│   ├── scoring/
│   │   ├── __init__.py      # registry: name -> ScoreFn factory
│   │   ├── protocol.py      # ScoreFn contract + normalisation helper
│   │   ├── jev.py           # TypeSafe Jev scorer (default when TYPESAFE_API_KEY is set)
│   │   └── keyword.py       # dependency-free lexicon scorer (fallback / tests)
│   └── sources/
│       ├── __init__.py      # registry: type name -> NewsSource class
│       ├── base.py          # NewsSource ABC
│       ├── finnhub.py
│       ├── alpha_vantage.py
│       ├── polygon.py
│       ├── tiingo.py
│       ├── marketaux.py
│       ├── newsapi.py
│       └── rss.py
└── tests/
```

## 3. Core data model (`models.py`)

```python
@dataclass(frozen=True, slots=True)
class Article:
    id: str                 # stable hash of (source, url or title+published)
    source: str             # source name, e.g. "finnhub"
    title: str
    published: datetime     # timezone-aware UTC
    url: str | None = None
    summary: str | None = None
    symbols: tuple[str, ...] = ()
    raw: Mapping[str, Any] = field(default_factory=dict, compare=False)

    @property
    def text(self) -> str    # title + summary, what scorers usually consume

@dataclass(frozen=True, slots=True)
class ArticleScore:
    score: float            # -1.0 (very negative) .. +1.0 (very positive)
    confidence: float = 1.0 # 0.0 .. 1.0, how much weight this score deserves
    relevance: float = 1.0  # 0.0 .. 1.0, how much the article is about the query
    labels: Mapping[str, Any] = {}  # free-form extras (event category, probabilities, ...)

@dataclass(frozen=True, slots=True)
class ScoredArticle:
    article: Article
    score: ArticleScore

@dataclass(slots=True)
class ScoreResult:
    query: str
    since: datetime
    until: datetime
    score: float            # aggregate in [-1, 1]
    confidence: float       # aggregate weight, 0..1
    n_articles: int
    by_source: dict[str, float]
    articles: list[ScoredArticle]

    def to_dict(self) -> dict
```

## 4. Scoring function contract (`scoring/protocol.py`)

The user-supplied scorer is the centre of the design, so the contract is small and forgiving:

```python
ScoreFn = Callable[[Sequence[Article], str], ScoreOutput | Awaitable[ScoreOutput]]
ScoreOutput = Sequence[ArticleScore | float | Mapping[str, Any]]
```

* **Input**: a batch of `Article` objects and the query string (symbol or keyword) they were fetched for.
* **Output**: one item per input article, in the same order. Each item may be an `ArticleScore`,
  a bare `float` in [-1, 1] (confidence and relevance default to 1.0), or a dict with keys
  `score`, and optionally `confidence`, `relevance`, `labels`.
* The function may be sync or `async`; the engine detects which via `inspect.isawaitable` on the result.
* Batching: the engine calls the scorer in chunks (`batch_size`, default 16) concurrently
  (`concurrency`, default 4), so the scorer only has to handle one batch at a time.
* `normalise_scores(raw, n)` converts any accepted output form into `list[ArticleScore]` and
  raises a clear `ValueError` if the length is wrong or values are out of range.

This is documented in the README and in the docstring of `protocol.py`.

## 5. Built-in scorers

**`jev`** (`scoring/jev.py`) — default when `TYPESAFE_API_KEY` is set and `typesafe-sdk` is installed.
One `system_one` call per article with a fixed question set:

| Question | Type | Maps to |
|---|---|---|
| `sentiment` | `Score`, 5 levels from "clearly negative for the stock" to "clearly positive" | `score` = expected value of level probabilities rescaled to [-1, 1] |
| `relevant` | `Noul` "Is this article materially about {query}?" | `relevance` |
| `category` | `Choice` among earnings / guidance / m&a / legal / product / macro / analyst / other | `labels["category"]`, `labels["category_probs"]` |
| `novel` | `Noul` "Does this contain new information rather than rehash?" | `labels["novel"]` |

`confidence` = the Score answer's calibrated confidence. Uses `AsyncTypeSafeClient`, bounded by a semaphore.
The state passed is `{"query": ..., "title": ..., "summary": ..., "source": ..., "published": ...}`.

**`keyword`** (`scoring/keyword.py`) — a Loughran-McDonald-style mini lexicon (a few hundred terms
embedded in the module), no network, used as fallback and in tests. Score = (pos − neg) / (pos + neg + k),
confidence grows with the number of matched terms.

## 6. News sources (`sources/`)

```python
class NewsSource(ABC):
    type_name: ClassVar[str]        # registry key, e.g. "finnhub"
    env_key: ClassVar[str | None]   # env var fallback for api_key, e.g. "FINNHUB_API_KEY"
    def __init__(self, api_key: str | None = None, name: str | None = None, **options)
    async def fetch(self, query: str, since: datetime, until: datetime, client: httpx.AsyncClient) -> list[Article]
```

| Type | Endpoint | Query semantics | Notes |
|---|---|---|---|
| `finnhub` | `GET /api/v1/company-news` | ticker | date-range params, unix timestamps |
| `alpha_vantage` | `NEWS_SENTIMENT` | ticker | `time_from=YYYYMMDDTHHMM`, limit 1000, vendor sentiment kept in `raw` |
| `polygon` | `GET /v2/reference/news` | ticker | follows `next_url` pagination |
| `tiingo` | `GET /tiingo/news` | ticker | |
| `marketaux` | `GET /v1/news/all` | ticker | page-based pagination, entity list → `symbols` |
| `newsapi` | `GET /v2/everything` | free-text keyword | `pageSize=100`, `language=en` |
| `rss` | any RSS 2.0 / Atom URL | keyword filter on title+summary | stdlib XML, no key needed |

Shared behaviour in `base.py`: date coercion helpers, deterministic `Article.id`, and a `_get_json`
wrapper that raises `SourceError(source, status, message)` so one failing source does not kill a run
(the engine collects errors into `ScoreResult.errors` and logs a warning).

## 7. Engine (`scorer.py`)

```python
class NewsScorer:
    def __init__(self, score_fn: ScoreFn | str | None = None, *, cache: bool | Path = True,
                 half_life_hours: float = 48.0, batch_size: int = 16, concurrency: int = 4,
                 timeout: float = 20.0)
    def source_add(self, source: str | NewsSource, /, *, api_key: str | None = None,
                   name: str | None = None, **options) -> NewsSource
    def source_remove(self, name: str) -> None
    def sources(self) -> list[str]
    @classmethod
    def from_config(cls, path: Path | None = None, **kwargs) -> "NewsScorer"   # loads saved CLI sources

    async def afetch(self, query, *, days=7, since=None, until=None, sources=None) -> list[Article]
    async def ascore(self, query, *, days=7, since=None, until=None, sources=None) -> ScoreResult
    def fetch(...) / def score(...)   # sync wrappers
```

Flow of `ascore`:
1. Resolve time window and the source subset (`None` → all).
2. `asyncio.gather` fetch across sources with one shared `httpx.AsyncClient`; dedupe by `Article.id`
   and by normalised title.
3. Look up cached `ArticleScore`s (key = article id + scorer name); score only misses, in batches,
   with bounded concurrency; write back to cache.
4. `aggregate()` → `ScoreResult`.

## 8. Aggregation (`aggregate.py`)

weight_i = confidence_i × relevance_i × 0.5^(age_hours_i / half_life)
score = Σ weight_i · score_i / Σ weight_i
confidence = 1 − exp(−Σ weight_i / k) (saturating; k = 3 so ~3 solid articles ≈ 0.63)

Deterministic, documented, and easy to swap: `NewsScorer(aggregate_fn=...)` is a documented hook.

## 9. Local configuration (`config.py`)

* Location: `platformdirs.user_config_dir("jev_sentiment")/sources.json`, override with `JEVSENT_CONFIG`.
* Shape: `{"sources": {"<name>": {"type": "finnhub", "api_key": "...", "options": {...}}}}`.
* API keys are optional in the file; sources fall back to their `env_key`.
* File written with mode 0600 where the OS supports it.

## 10. CLI (`cli.py`, typer)

```
jevsent source add <type> [--name NAME] [--api-key KEY] [--option k=v]...
jevsent source list
jevsent source remove <name>
jevsent source types                       # show supported source types + env var names
jevsent fetch <query> [--source NAME]... [--days N] [--json]
jevsent score <query> [--source NAME]... [--days N] [--scorer jev|keyword] [--json] [--show-articles]
jevsent config-path
```

`--source` is repeatable; omitted means all saved sources. Output is a compact table by default,
JSON with `--json` so it pipes into other tools.

## 11. Dependencies

* Runtime: `httpx`, `typer`, `platformdirs`. Optional extra `jev`: `typesafe-sdk`.
* Dev: `pytest`, `pytest-asyncio`, `respx` (mock httpx).
* Python ≥ 3.10.

## 12. Tests

* Model helpers and `normalise_scores` edge cases.
* Aggregation math (weights, decay, empty input).
* Keyword scorer sanity.
* Each source's response parser against a fixture payload (respx-mocked).
* `NewsScorer` end-to-end with a fake source and a user-supplied sync and async scorer.
* Config store round-trip, CLI `source add/list/remove` via `typer.testing.CliRunner`.

## 13. Build order

1. `models`, `scoring/protocol`, `aggregate`, `scoring/keyword` (pure, testable without network).
2. `sources/base` + `http`, then each concrete source.
3. `cache`, `config`.
4. `scorer.NewsScorer`.
5. `scoring/jev`.
6. `cli`.
7. README, tests, install check (`pip install -e .` then `jevsent --help`).
