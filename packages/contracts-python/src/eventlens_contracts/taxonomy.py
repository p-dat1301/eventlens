"""Controlled taxonomy shared by RAG contract boundaries."""

from typing import Literal

TopicTag = Literal[
    "business",
    "technology",
    "politics",
    "economy",
    "public_safety",
    "weather",
    "transportation",
    "health",
    "energy",
    "environment",
    "other",
]
