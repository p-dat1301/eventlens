"""PostgreSQL column types SQLAlchemy does not ship (pgvector)."""

from typing import cast, override

from sqlalchemy.dialects.postgresql.base import ischema_names
from sqlalchemy.types import UserDefinedType

EMBEDDING_DIMENSION = 1536


class Vector(UserDefinedType[str]):
    """``vector(n)`` column; values travel as the pgvector text literal."""

    cache_ok: bool | None = True

    def __init__(self, dimension: int = EMBEDDING_DIMENSION) -> None:
        """Fix the column dimension (the RAG schema uses 1536 only)."""
        self.dimension: int = dimension

    @override
    def get_col_spec(self, **kw: object) -> str:
        """Render the DDL type."""
        return f"vector({self.dimension})"


# Let reflection (Alembic's drift comparison) recognize pgvector columns.
cast("dict[str, object]", ischema_names)["vector"] = Vector
