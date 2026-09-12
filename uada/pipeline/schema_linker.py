"""
Schema Linker (pipeline step 2)
=================================
Deterministic. Turns a natural-language question (plus condensed
conversation context) into a `SchemaContext`: the security-filtered
subset of the SCL relevant to answering it.

Retrieval surfaces *candidate* tables/metrics/glossary terms/examples;
this module is the security boundary between "what the retriever thinks
is relevant" and "what actually gets shown to the LLM" -- every table is
re-checked against `SCLManager.get_allowed_tables()` before it can appear
in the returned SchemaContext, regardless of how it scored.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from uada.models.schema_context import (
    ColumnContext,
    ExampleContext,
    GlossaryContext,
    JoinContext,
    MetricContext,
    SchemaContext,
    TableContext,
)
from uada.retrieval.interface import DocType

if TYPE_CHECKING:
    from uada.config import Settings
    from uada.retrieval.hybrid import HybridRetriever
    from uada.scl.manager import SCLManager
    from uada.scl.schema import ColumnDefinition

logger = logging.getLogger(__name__)


class SchemaLinker:
    """Links a question to the relevant, security-filtered slice of the SCL."""

    def __init__(
        self,
        retriever: HybridRetriever,
        scl_manager: SCLManager,
        settings: Settings,
    ) -> None:
        self._retriever = retriever
        self._scl_manager = scl_manager
        self._settings = settings

    def link(self, question: str, session_context: str | None = None) -> SchemaContext:
        """
        Retrieve and assemble the SchemaContext for `question`.

        Args:
            question: The user's natural-language question.
            session_context: Condensed prior-turn context, if this is a
                follow-up question. Appended to the retrieval query.

        Returns:
            A SchemaContext containing only retrieved, security-allowed
            schema elements.
        """
        query = self._build_query(question, session_context)
        top_k = self._settings.retrieval_final_k

        # Retrieved separately per doc_type, each with its own top_k budget
        # -- not one shared budget across all types. A worked example's
        # content ("Question: What was total revenue last quarter?") is
        # often a near-exact match for the question itself and will
        # systematically outrank a table's one-line description under a
        # combined ranking, starving tables out of the result entirely
        # once a schema has more than a couple of examples or metrics.
        # Confirmed empirically against a real demo schema (3 tables, 4
        # metrics, 8 examples): the "orders" table didn't appear even in
        # the top 10 combined results for a plain revenue question.
        table_results = self._retriever.retrieve(
            query, top_k=top_k, filters={"doc_type": DocType.TABLE}
        )
        metric_results = self._retriever.retrieve(
            query, top_k=top_k, filters={"doc_type": DocType.METRIC}
        )
        glossary_results = self._retriever.retrieve(
            query, top_k=top_k, filters={"doc_type": DocType.GLOSSARY}
        )
        example_results = self._retriever.retrieve(
            query, top_k=top_k, filters={"doc_type": DocType.EXAMPLE}
        )

        tables: list[TableContext] = []
        seen_tables: set[str] = set()
        for result in table_results:
            _, _, identifier = result.document.doc_id.partition(":")
            if identifier in seen_tables:
                continue
            table_ctx = self._build_table_context(identifier, result.score)
            if table_ctx is not None:
                seen_tables.add(identifier)
                tables.append(table_ctx)

        metrics: list[MetricContext] = []
        for result in metric_results:
            _, _, identifier = result.document.doc_id.partition(":")
            metric_ctx = self._build_metric_context(identifier, result.score)
            if metric_ctx is not None:
                metrics.append(metric_ctx)

        glossary_terms: list[GlossaryContext] = []
        for result in glossary_results:
            _, _, identifier = result.document.doc_id.partition(":")
            glossary_ctx = self._build_glossary_context(identifier)
            if glossary_ctx is not None:
                glossary_terms.append(glossary_ctx)

        examples: list[ExampleContext] = []
        for result in example_results:
            _, _, identifier = result.document.doc_id.partition(":")
            example_ctx = self._build_example_context(identifier, result.score)
            if example_ctx is not None:
                examples.append(example_ctx)

        total_retrieved = (
            len(table_results) + len(metric_results) + len(glossary_results) + len(example_results)
        )
        context = SchemaContext(
            tables=tables,
            joins=self._collect_joins(tables),
            metrics=metrics,
            glossary_terms=glossary_terms,
            examples=examples,
            dialect=self._scl_manager.scl.database.dialect.value,
            default_time_column=self._default_time_column(tables),
            fiscal_year_start_month=self._scl_manager.scl.database.fiscal_year_start_month,
            retrieval_query=query,
            total_retrieved=total_retrieved,
        )
        logger.info(
            "Schema linked: %d table(s), %d metric(s), %d glossary term(s), %d example(s) "
            "from %d retrieved candidate(s).",
            len(tables),
            len(metrics),
            len(glossary_terms),
            len(examples),
            total_retrieved,
        )
        return context

    def _build_query(self, question: str, session_context: str | None) -> str:
        """Combine the question with condensed conversation context, if any."""
        if not session_context:
            return question
        return f"{question}\n\nConversation context:\n{session_context}"

    def _build_table_context(self, table_name: str, score: float) -> TableContext | None:
        """
        Build a TableContext for `table_name`, or None if it isn't allowed.

        This is the security check: a table must be in
        `SCLManager.get_allowed_tables()` to appear in a SchemaContext,
        regardless of its retrieval score.
        """
        if table_name not in self._scl_manager.get_allowed_tables():
            logger.warning("Retrieved table '%s' is excluded; dropping.", table_name)
            return None

        table = self._scl_manager.get_table(table_name)
        if table is None:
            return None

        return TableContext(
            table_name=table.name,
            description=table.description,
            grain=table.grain,
            aliases=list(table.aliases),
            columns=[self._build_column_context(table.name, col) for col in table.included_columns],
            retrieval_score=score,
        )

    def _build_column_context(self, table_name: str, column: ColumnDefinition) -> ColumnContext:
        return ColumnContext(
            column_name=column.name,
            table_name=table_name,
            data_type=column.type,
            description=column.description,
            is_primary_key=column.is_primary_key,
            is_foreign_key=column.is_foreign_key,
            references=column.references,
            is_temporal=column.is_temporal,
            is_default_time_column=column.default_time_column,
            semantic_type=column.semantic_type.value,
            aggregation=column.aggregation,
            enum_values=column.enum_values,
        )

    def _build_metric_context(self, name: str, score: float) -> MetricContext | None:
        metric = self._scl_manager.resolve_metric(name)
        if metric is None:
            return None
        return MetricContext(
            metric_name=metric.name,
            description=metric.description,
            formula=metric.formula,
            filters=metric.filters,
            unit=metric.unit,
            retrieval_score=score,
        )

    def _build_glossary_context(self, term_text: str) -> GlossaryContext | None:
        term = self._scl_manager.resolve_glossary_term(term_text)
        if term is None:
            return None
        return GlossaryContext(
            term=term.term,
            description=term.description,
            sql_filter=term.sql_filter,
        )

    def _build_example_context(self, index_text: str, score: float) -> ExampleContext | None:
        try:
            index = int(index_text)
        except ValueError:
            return None

        examples = self._scl_manager.scl.examples
        if not 0 <= index < len(examples):
            return None

        example = examples[index]
        return ExampleContext(
            question=example.question,
            sql=example.sql,
            intent_type=example.intent_type,
            similarity_score=score,
        )

    def _collect_joins(self, tables: list[TableContext]) -> list[JoinContext]:
        """Join paths connecting pairs of *retrieved* tables (not all SCL joins)."""
        joins: list[JoinContext] = []
        seen: set[frozenset[str]] = set()
        table_names = [t.table_name for t in tables]

        for i, from_name in enumerate(table_names):
            for to_name in table_names[i + 1 :]:
                pair = frozenset((from_name, to_name))
                if pair in seen:
                    continue
                join_def = self._scl_manager.get_join_path(from_name, to_name)
                if join_def is None:
                    continue
                seen.add(pair)
                joins.append(
                    JoinContext(
                        from_table=join_def.from_table,
                        to_table=join_def.to_table,
                        join_type=join_def.join_type,
                        on_condition=join_def.on,
                        description=join_def.description,
                    )
                )
        return joins

    def _default_time_column(self, tables: list[TableContext]) -> str | None:
        """The first retrieved table's default time column, if any is configured."""
        for table in tables:
            for column in table.columns:
                if column.is_default_time_column:
                    return column.column_name
        return None
