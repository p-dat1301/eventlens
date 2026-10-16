"""Stage article vectors, reusing compatible complete generations."""

from dataclasses import dataclass

from eventlens_rag.domain.article_document import ArticleDocument
from eventlens_rag.domain.chunking import chunk_document
from eventlens_rag.domain.embedding import EmbeddingIdentity
from eventlens_rag.domain.staging import StageRequest
from eventlens_rag.ports.embeddings import EmbeddingPort
from eventlens_rag.ports.staging import GenerationStaging


@dataclass(frozen=True, slots=True)
class StagedGeneration:
    """Inactive generation identifier and whether embeddings were reused."""

    generation: int
    reused: bool


@dataclass(frozen=True, slots=True)
class EnsureGeneration:
    """Embed only when no complete generation matches the embedding key.

    The key covers content hash, normalization, chunker and embedding identity,
    never enrichment model or prompt versions, so metadata-only revisions make
    zero embedding calls.
    """

    staging: GenerationStaging
    embeddings: EmbeddingPort
    identity: EmbeddingIdentity

    def execute(self, article: ArticleDocument) -> StagedGeneration:
        """Return a complete inactive generation for the article revision."""
        existing = self.staging.find_complete(article, self.identity)
        if existing is not None:
            return StagedGeneration(existing, reused=True)
        chunks = chunk_document(article)
        vectors = tuple(self.embeddings.embed(chunk.content) for chunk in chunks)
        request = StageRequest(article, self.identity, chunks, vectors)
        return StagedGeneration(self.staging.stage(request), reused=False)
