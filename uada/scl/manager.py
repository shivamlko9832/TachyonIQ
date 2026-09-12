"""
Semantic Context Layer — Manager
==================================
Wraps a loaded `SemanticContextLayer` with fast, case-insensitive lookup
indexes and produces the documents the retrieval layer indexes for schema
linking.

This is the object the rest of the pipeline talks to -- the SQLValidator's
table allowlist, the QueryPlanner's metric/join resolution, and the
retrieval engine's document corpus all come from here rather than walking
`SemanticContextLayer` directly.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from uada.retrieval.interface import DocType

if TYPE_CHECKING:
    from uada.scl.schema import (
        GlossaryTerm,
        JoinDefinition,
        MetricDefinition,
        SemanticContextLayer,
        TableDefinition,
    )

logger = logging.getLogger(__name__)


class SCLManager:
    """Indexed, query-friendly view over a loaded SemanticContextLayer."""

    def __init__(self, scl: SemanticContextLayer) -> None:
        self._scl = scl
        self._table_index: dict[str, TableDefinition] = {t.name: t for t in scl.tables}
        self._metric_index = self._build_metric_index(scl.metrics)
        self._glossary_index = self._build_glossary_index(scl.glossary)
        self._analysis_profile_index = {
            profile.name.lower(): profile for profile in scl.analysis_profiles
        }
        for profile in scl.analysis_profiles:
            for alias in profile.aliases:
                self._analysis_profile_index[alias.lower()] = profile
        self._allowed_tables: frozenset[str] = frozenset(scl.get_allowed_tables())

        logger.info(
            "SCLManager built for '%s': %d table(s), %d metric(s), %d glossary term(s), "
            "%d allowed table(s).",
            scl.database.name,
            len(self._table_index),
            len(scl.metrics),
            len(scl.glossary),
            len(self._allowed_tables),
        )

    @property
    def scl(self) -> SemanticContextLayer:
        """The underlying, fully-validated SemanticContextLayer."""
        return self._scl

    def get_allowed_tables(self) -> frozenset[str]:
        """Table names permitted in generated SQL. Used to build the SQLValidator allowlist."""
        return self._allowed_tables

    def get_table(self, name: str) -> TableDefinition | None:
        """Look up a table definition by its exact database name."""
        return self._table_index.get(name)

    def resolve_metric(self, name: str) -> MetricDefinition | None:
        """Case-insensitive lookup of a metric by name or alias."""
        return self._metric_index.get(name.lower())

    def resolve_glossary_term(self, term: str) -> GlossaryTerm | None:
        """Case-insensitive lookup of a glossary term by term text or alias."""
        return self._glossary_index.get(term.lower())

    def match_analysis_profile(self, question: str):
        """Return the most specific governed profile whose alias occurs in the question."""
        lowered = question.lower()
        matches = [
            (alias, profile)
            for alias, profile in self._analysis_profile_index.items()
            if alias in lowered
        ]
        return max(matches, key=lambda item: len(item[0]))[1] if matches else None

    def get_join_path(self, from_table: str, to_table: str) -> JoinDefinition | None:
        """
        Look up the join between two tables, in either direction.

        Returns the first matching JoinDefinition, or None if the SCL
        defines no join between the two tables.
        """
        for join in self._scl.joins:
            if join.from_table == from_table and join.to_table == to_table:
                return join
            if join.from_table == to_table and join.to_table == from_table:
                return join
        return None

    def to_indexable_documents(self) -> list[dict[str, Any]]:
        """
        Build the document corpus for the retrieval engine.

        One document per included table, metric, glossary term, and
        example query. Excluded tables (and their columns) never produce
        documents or appear in another document's content, so they can
        never be retrieved.
        """
        documents: list[dict[str, Any]] = []

        for table in self._scl.included_tables:
            documents.append(self._table_document(table))
        for metric in self._scl.metrics:
            documents.append(self._metric_document(metric))
        for term in self._scl.glossary:
            documents.append(self._glossary_document(term))
        for index, example in enumerate(self._scl.examples):
            documents.append(self._example_document(index, example.question))

        return documents

    def _build_metric_index(
        self, metrics: list[MetricDefinition]
    ) -> dict[str, MetricDefinition]:
        index: dict[str, MetricDefinition] = {}
        for metric in metrics:
            index[metric.name.lower()] = metric
            for alias in metric.aliases:
                index[alias.lower()] = metric
        return index

    def _build_glossary_index(self, glossary: list[GlossaryTerm]) -> dict[str, GlossaryTerm]:
        index: dict[str, GlossaryTerm] = {}
        for term in glossary:
            index[term.term.lower()] = term
            for alias in term.aliases:
                index[alias.lower()] = term
        return index

    def _table_document(self, table: TableDefinition) -> dict[str, Any]:
        description = table.description or "No description available."
        columns = ", ".join(
            f"{c.name} ({c.description})" if c.description else c.name
            for c in table.included_columns
        )
        return {
            "doc_id": f"table:{table.name}",
            "content": f"{table.name}: {description}. Columns: {columns}",
            "metadata": {
                "doc_type": DocType.TABLE,
                "table_name": table.name,
                "excluded": False,
            },
        }

    def _metric_document(self, metric: MetricDefinition) -> dict[str, Any]:
        return {
            "doc_id": f"metric:{metric.name}",
            "content": f"{metric.name}: {metric.description}. Formula: {metric.formula}",
            "metadata": {
                "doc_type": DocType.METRIC,
                "table_name": None,
                "excluded": False,
            },
        }

    def _glossary_document(self, term: GlossaryTerm) -> dict[str, Any]:
        return {
            "doc_id": f"glossary:{term.term}",
            "content": f"'{term.term}': {term.description}",
            "metadata": {
                "doc_type": DocType.GLOSSARY,
                "table_name": term.applies_to_table,
                "excluded": False,
            },
        }

    def _example_document(self, index: int, question: str) -> dict[str, Any]:
        return {
            "doc_id": f"example:{index}",
            "content": f"Question: {question}",
            "metadata": {
                "doc_type": DocType.EXAMPLE,
                "table_name": None,
                "excluded": False,
            },
        }
