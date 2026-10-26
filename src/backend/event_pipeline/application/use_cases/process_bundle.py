"""Framework-free worker use cases."""

from dataclasses import dataclass

from backend.event_pipeline.application.use_cases.pipeline_runner import PipelineRunner
from backend.event_pipeline.domain.models import ProcessingTrace
from backend.shared.contracts.bundle import ArticleBundle


@dataclass(frozen=True, slots=True)
class BundleValidation:
    """Validated bundle summary for output adapters."""

    article_id: str
    media_count: int


def validate_bundle_json(bundle_json: str) -> BundleValidation:
    """Parse bundle JSON at worker boundary and summarize it."""
    bundle = ArticleBundle.model_validate_json(bundle_json)
    return BundleValidation(
        article_id=str(bundle.article.article_id),
        media_count=len(bundle.media),
    )


def process_bundle_json(bundle_json: str, runner: PipelineRunner) -> ProcessingTrace:
    """Parse one bundle at worker boundary and process it synchronously."""
    bundle = ArticleBundle.model_validate_json(bundle_json)
    return runner.run((bundle,))[0]
