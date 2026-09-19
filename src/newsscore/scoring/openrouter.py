"""Call Jev through OpenRouter's Decisions router instead of TypeSafe directly.

TypeSafe gates new accounts behind a waiting list, so the Jev scorer used to be
unusable for most people. OpenRouter resells the same System One models through a
near-identical wire protocol, and this transport lets an OpenRouter key drive the
same scorer. It needs no SDK -- only ``httpx``, which is already a dependency -- so
``pip install newsscore`` is enough.

The two routes differ only in packaging:

=====================  ==============================  ================================
                       TypeSafe                        OpenRouter
=====================  ==============================  ================================
endpoint               ``POST /v1/systemone``          ``POST /api/alpha/decisions``
questions              SDK objects                     JSON objects with a ``type`` tag
answers                typed objects                   JSON objects with a ``type`` tag
model id               ``jev-1.13.0``                  ``typesafe/jev-1.13``
credential             ``TYPESAFE_API_KEY``            ``OPENROUTER_API_KEY``
``[jev]`` extra        required                        not needed
=====================  ==============================  ================================

Model ids differ in spelling as well as namespace. OpenRouter marks a floating alias
with a leading ``~``: ``~typesafe/jev-latest`` moves, ``typesafe/jev-1.13`` and the
dated ``typesafe/jev-1.13-20260917`` do not. An alias is unpinned, so it disables
caching here for the same reason ``jev-latest`` does on TypeSafe.

Two things are worth knowing before relying on it.

The Decisions endpoint is **alpha** at OpenRouter, which means its shape may change
without a deprecation period. Nothing here can protect you from that; the failure
would surface as every article failing to parse, which the engine reports rather
than quietly scoring as neutral.

Whether the two routes give the *same answers* is an empirical question and not a
promise, even for a matching version string. The cache fingerprint therefore includes
the provider, so a score fetched through one is never reused for the other. Compare
them yourself on your own articles before assuming a benchmark transfers.
"""

from __future__ import annotations

import os
import re
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Mapping

if TYPE_CHECKING:  # only annotations need httpx here; the call imports it for real
    import httpx

from .protocol import ScorerUnavailable

#: OpenRouter's Decisions router. Alpha, per their OpenAPI description.
DECISIONS_URL = "https://openrouter.ai/api/alpha/decisions"

ENV_KEY = "OPENROUTER_API_KEY"

#: Statuses that another attempt could plausibly get past.
RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504, 529})
#: Statuses that will reject every other article in exactly the same way, so the run
#: should stop rather than repeat the rejection once per headline.
FATAL_STATUS = frozenset({401, 402, 403, 404})


#: A 400 is normally about the one request that caused it, so it fails that article
#: alone. A rejected *model id* is the exception: the id is the same for every
#: article, so the run would otherwise make one doomed request per headline to learn
#: the same thing. OpenRouter distinguishes these only in prose, hence the match.
_MISSING_MODEL = re.compile(r"model\b.*\b(does not exist|not found|unknown)", re.I)


class OpenRouterError(RuntimeError):
    """One failed Decisions request, carrying its own retry verdict.

    The engine's classifier reads ``retryable`` and ``fatal`` off the exception, so
    the HTTP taxonomy lives here rather than being re-derived from a message.
    """

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.retryable = status in RETRYABLE_STATUS if status else False
        self.fatal = bool(status in FATAL_STATUS if status else False) or (
            status == 400 and bool(_MISSING_MODEL.search(message))
        )


class OpenRouterDecisions:
    """Minimal async client for one Decisions endpoint, shaped like the TypeSafe SDK.

    ``system_one`` returns an object with the same attributes the SDK's response has,
    so :meth:`~newsscore.scoring.jev.JevScorer._convert` works against either
    provider unchanged and cannot drift between them.
    """

    provider = "openrouter"

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "typesafe/jev-1.13",
        *,
        timeout: float = 30.0,
        url: str = DECISIONS_URL,
        referer: str | None = None,
        title: str | None = "newsscore",
    ) -> None:
        self._api_key = api_key or os.environ.get(ENV_KEY)
        self._model = model
        self._timeout = timeout
        self._url = url
        self._referer = referer
        self._title = title
        self._client: httpx.AsyncClient | None = None

    def _headers(self) -> dict[str, str]:
        headers = {"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"}
        # Optional attribution headers; OpenRouter uses them for its public app list.
        if self._referer:
            headers["HTTP-Referer"] = self._referer
        if self._title:
            headers["X-Title"] = self._title
        return headers

    def _get_client(self) -> httpx.AsyncClient:
        import httpx

        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout)
        return self._client

    async def system_one(self, *, state: Any, questions: Mapping[str, Any]) -> Any:
        """Ask one set of questions about one piece of state."""
        import httpx

        payload = {"model": self._model, "state": state, "questions": dict(questions)}
        try:
            response = await self._get_client().post(
                self._url, json=payload, headers=self._headers()
            )
        except httpx.TimeoutException as exc:
            raise OpenRouterError(f"OpenRouter timed out: {exc}", status=408) from exc
        except httpx.TransportError as exc:
            raise OpenRouterError(f"cannot reach OpenRouter: {exc}", status=503) from exc

        if response.status_code >= 400:
            raise OpenRouterError(
                f"OpenRouter returned {response.status_code}: {_detail(response)}",
                status=response.status_code,
            )
        try:
            body = response.json()
        except ValueError as exc:
            raise OpenRouterError(f"OpenRouter sent a non-JSON body: {exc}") from exc
        return _as_sdk_response(body)

    async def aclose(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            await client.aclose()


def preflight(api_key: str | None = None) -> None:
    """Fail at construction when OpenRouter cannot be called, not on the first article."""
    if not (api_key or os.environ.get(ENV_KEY)):
        raise ScorerUnavailable(
            f"the Jev scorer on OpenRouter needs an API key: pass api_key= or set {ENV_KEY}"
        )


def to_questions(spec: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Neutral question spec to the Decisions wire format.

    The wire format *is* the neutral format -- a ``type`` tag plus ``instructions``
    and, for score and choice, ``criteria`` -- so this only drops empty criteria,
    which the schema makes optional for ``noul`` but not for the other two.
    """
    out: dict[str, dict[str, Any]] = {}
    for name, question in spec.items():
        item: dict[str, Any] = {"type": question["type"], "instructions": question["instructions"]}
        criteria = question.get("criteria")
        if criteria is not None:
            item["criteria"] = list(criteria) if question["type"] == "score" else dict(criteria)
        out[name] = item
    return out


def _detail(response: httpx.Response) -> str:
    """The provider's own message, when it sent one."""
    try:
        body = response.json()
    except ValueError:
        return response.text[:300]
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        return str(error.get("message") or error)[:300]
    return str(error or body)[:300]


def _as_sdk_response(body: Mapping[str, Any]) -> Any:
    """Wrap a Decisions body so it reads exactly like a TypeSafe SDK response.

    Answers arrive as JSON objects tagged with their type; the SDK hands back objects
    with ``score``/``noul``/``choice`` attributes. Presenting the same surface keeps
    one conversion path for both providers, so a fix or a bug can never apply to only
    one of them.
    """
    answers = {}
    for name, answer in (body.get("answers") or {}).items():
        if not isinstance(answer, Mapping):
            raise OpenRouterError(f"answer {name!r} is not an object: {answer!r}")
        answers[name] = SimpleNamespace(
            score=answer.get("score"),
            noul=answer.get("noul"),
            choice=answer.get("choice"),
            confidence=answer.get("confidence"),
            probabilities=_probabilities(answer),
            legend=answer.get("legend"),
        )
    usage = body.get("usage") or {}
    return SimpleNamespace(
        answers=answers,
        model=body.get("model"),
        provider=body.get("provider"),
        # The SDK calls it request_id; Decisions calls it id. Same job.
        request_id=body.get("id"),
        usage=SimpleNamespace(
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            cost=usage.get("cost"),
        ),
    )


def _probabilities(answer: Mapping[str, Any]) -> dict[Any, float] | None:
    """Probability keys, with score levels normalised to their integer index.

    A score answer's keys may come back as the level *text* rather than its position,
    depending on how the router renders them. The conversion downstream expects an
    index, so ordinal keys are mapped back through the answer's own ``legend`` when
    one is present, and left alone otherwise.
    """
    probs = answer.get("probabilities")
    if not isinstance(probs, Mapping) or not probs:
        return None
    if answer.get("type") != "score":
        return {str(k): float(v) for k, v in probs.items()}

    out: dict[Any, float] = {}
    legend = answer.get("legend") if isinstance(answer.get("legend"), Mapping) else {}
    order = list(legend) if legend else []
    for key, value in probs.items():
        index: Any = key
        try:
            index = int(key)
        except (TypeError, ValueError):
            index = order.index(key) if key in order else key
        out[index] = float(value)
    return out
