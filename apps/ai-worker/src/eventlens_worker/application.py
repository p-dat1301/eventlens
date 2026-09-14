"""Framework-free worker use cases."""

from dataclasses import dataclass

from eventlens_contracts import ArticleBundle


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
