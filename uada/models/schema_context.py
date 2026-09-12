"""
SchemaContext — Output of the Schema Linker
============================================
SchemaContext is produced by pipeline step 2 (Schema Linker) and passed
to pipeline step 3 (Analytical Intent Extractor) and step 4 (Query Planner).

It contains only the schema elements that are relevant to the current question,
filtered through security exclusions from the SCL.

It does NOT contain the full database schema — only the retrieved subset.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ColumnContext(BaseModel):
    """A single column, with its metadata, available for query generation."""

    column_name: str
    table_name: str
    data_type: str
    description: str | None = None
    is_primary_key: bool = False
    is_foreign_key: bool = False
    references: str | None = None  # "other_table.other_column"
    is_temporal: bool = False
    is_default_time_column: bool = False
    semantic_type: str | None = None  # "measure", "dimension", "identifier"
    aggregation: str | None = None    # Default aggregation for measures
    enum_values: list[str] | None = None
    retrieval_score: float = Field(
        default=0.0,
        description="RRF score from the hybrid retrieval. Higher = more relevant.",
    )


class TableContext(BaseModel):
    """A table and its relevant columns, as retrieved by the schema linker."""

    table_name: str
    description: str | None = None
    grain: str | None = None  # "order", "customer", "day"
    aliases: list[str] = Field(default_factory=list)
    columns: list[ColumnContext] = Field(default_factory=list)
    retrieval_score: float = Field(default=0.0)


class JoinContext(BaseModel):
    """A join path between two tables, from the SCL."""

    from_table: str
    to_table: str
    join_type: str = "LEFT"
    on_condition: str
    description: str | None = None


class MetricContext(BaseModel):
    """A metric definition from the SCL, retrieved as relevant to the question."""

    metric_name: str
    description: str
    formula: str
    filters: str | None = None
    unit: str | None = None
    retrieval_score: float = Field(default=0.0)


class GlossaryContext(BaseModel):
    """A glossary term from the SCL, retrieved as relevant to the question."""

    term: str
    description: str
    sql_filter: str | None = None  # Ready-to-use SQL filter fragment


class ExampleContext(BaseModel):
    """A Q&A example from the SCL, retrieved as relevant to the question."""

    question: str
    sql: str
    intent_type: str | None = None
    similarity_score: float = Field(default=0.0)


class SchemaContext(BaseModel):
    """
    The schema context for a specific user question.

    Produced by the Schema Linker (step 2).
    Consumed by the Intent Extractor (step 3) and Query Planner (step 4).

    Contains only the retrieved, security-filtered subset of the schema.
    All excluded tables/columns have already been removed.
    """

    # ── Retrieved elements ────────────────────────────────────────────────────
    tables: list[TableContext] = Field(
        default_factory=list,
        description="Relevant tables, ordered by retrieval score descending.",
    )
    joins: list[JoinContext] = Field(
        default_factory=list,
        description="Join paths connecting the retrieved tables.",
    )
    metrics: list[MetricContext] = Field(
        default_factory=list,
        description="Relevant metric definitions from the SCL.",
    )
    glossary_terms: list[GlossaryContext] = Field(
        default_factory=list,
        description="Relevant glossary terms from the SCL.",
    )
    examples: list[ExampleContext] = Field(
        default_factory=list,
        description="Relevant Q&A examples from the SCL, ordered by similarity.",
    )

    # ── Database metadata ─────────────────────────────────────────────────────
    dialect: str = Field(description="SQL dialect of the connected database.")
    default_time_column: str | None = Field(
        default=None,
        description="The default time column for this schema, from SCL configuration.",
    )
    fiscal_year_start_month: int = Field(
        default=1,
        ge=1,
        le=12,
        description="Fiscal year start month copied from the SCL database metadata.",
    )

    # ── Retrieval metadata ────────────────────────────────────────────────────
    retrieval_query: str = Field(description="The query used for retrieval, for tracing.")
    total_retrieved: int = Field(
        description="Total candidates retrieved before RRF merge."
    )

    def to_prompt_context(self) -> str:
        """
        Returns a compact string representation for injection into LLM prompts.
        Prioritises readability and token efficiency.
        """
        lines: list[str] = [f"Database dialect: {self.dialect}"]

        if self.tables:
            lines.append("\nRelevant tables:")
            for table in self.tables:
                desc = f" — {table.description}" if table.description else ""
                lines.append(f"  {table.table_name}{desc}")
                if table.grain:
                    lines.append(f"    Grain: {table.grain}")
                for col in table.columns:
                    flags = []
                    if col.is_primary_key:
                        flags.append("PK")
                    if col.is_foreign_key:
                        flags.append(f"FK→{col.references}")
                    if col.is_temporal:
                        flags.append("temporal")
                    flag_str = f" [{', '.join(flags)}]" if flags else ""
                    col_desc = f" — {col.description}" if col.description else ""
                    lines.append(
                        f"    {col.column_name} ({col.data_type}){flag_str}{col_desc}"
                    )

        if self.joins:
            lines.append("\nAvailable joins:")
            for j in self.joins:
                lines.append(
                    f"  {j.from_table} → {j.to_table}: {j.on_condition}"
                )

        if self.metrics:
            lines.append("\nAvailable metrics:")
            for m in self.metrics:
                lines.append(f"  {m.metric_name}: {m.formula}")
                if m.filters:
                    lines.append(f"    Filter: {m.filters}")

        if self.glossary_terms:
            lines.append("\nBusiness terms:")
            for g in self.glossary_terms:
                lines.append(f"  '{g.term}': {g.description}")
                if g.sql_filter:
                    lines.append(f"    SQL: {g.sql_filter}")

        if self.examples:
            lines.append("\nExample queries:")
            for e in self.examples[:3]:  # Cap at 3 examples in prompt
                lines.append(f"  Q: {e.question}")
                lines.append(f"  SQL: {e.sql}")

        return "\n".join(lines)
