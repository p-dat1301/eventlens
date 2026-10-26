"""Structural citation validity versus labelled semantic support.

For each hand-labelled case a scripted model emits the labelled claim text
citing a real, sent span of the labelled article. The T6 validator checks
structure (span sent, offsets, hash, claim binding); it cannot judge meaning.
The table shows how many structurally accepted claims the labels mark as
unsupported or contradicted - the gap no structural check closes.
"""

from collections.abc import Callable

from backend.rag.application.ports.synthesis import SynthesisRequest, SynthesisResponse
from backend.rag.composition import fixture_model_identity
from backend.rag.infra.llm.scripted_synthesis import ScriptedSynthesizer

from .corpus import EvalCorpus, SemanticCase
from .metrics import Json, ratio
from .rag_support import (
    CountingModel,
    answer_checks,
    ask,
    briefing_query,
    draft_response,
    first_span,
    hybrid_store,
    runtime,
)

LABELS = "fixtures/rag/semantic_support.v1.json (implementer-authored labels)"


def _claiming(case: SemanticCase) -> Callable[[SynthesisRequest], SynthesisResponse]:
    def respond(request: SynthesisRequest) -> SynthesisResponse:
        return draft_response(
            [(case.claim_text, first_span(request, case.article_id) or "")]
        )

    return respond


def _run(corpus: EvalCorpus, case: SemanticCase) -> dict[str, Json]:
    store = hybrid_store(corpus, (case.article_id,))
    model = CountingModel(
        ScriptedSynthesizer(fixture_model_identity(), [_claiming(case)])
    )
    composed, _ = runtime(store, model)
    asked = ask(composed, briefing_query(corpus.article(case.article_id).title))
    checks = (
        answer_checks(asked.outcome, dict(store.documents)) if asked.outcome else None
    )
    accepted = (
        checks is not None
        and checks["citations"] == checks["citations_structurally_valid"] == 1
    )
    return {
        "label": case.label,
        "route": asked.outcome.route.value if asked.outcome else str(asked.error_code),
        "structurally_accepted": accepted,
        "verbatim_in_span": bool(checks and checks["claims_verbatim_in_cited_span"]),
    }


def semantic_support(corpus: EvalCorpus) -> dict[str, Json]:
    """Per-case table and the structural/semantic summary with denominators."""
    rows = {case.case_id: _run(corpus, case) for case in corpus.semantic_cases}
    accepted = [r for r in rows.values() if r.get("structurally_accepted") is True]
    supported = [r for r in accepted if r.get("label") == "supported"]
    return {
        "labels": LABELS,
        "cases": dict(rows),
        "structural_acceptance": ratio(
            len(accepted),
            len(rows),
            "labelled claims the structural validator accepted / cases",
        ),
        "semantic_support_among_accepted": ratio(
            len(supported),
            len(accepted),
            "accepted claims labelled supported / accepted (gold-label judgement)",
        ),
        "accepted_but_unsupported_or_contradicted": len(accepted) - len(supported),
        "automatic_semantic_check": {
            "value": None,
            "status": "NOT MEASURED",
            "reason": "no entailment judge; support known only via human labels",
        },
    }
