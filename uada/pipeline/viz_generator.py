"""
Visualisation Generator (pipeline step 9)
============================================
Chart type selection is deterministic and applied first (CHART_SELECTION_RULES).
For the shapes those rules cover, the Vega-Lite spec is built directly with
Altair -- no LLM call needed, and no risk of a hallucinated spec. The LLM
Agent this module also builds is used only as a fallback for result shapes
none of the deterministic rules match; the model chooses "mark"/"encoding"
only, never data values -- the real DataFrame is always injected afterward,
and any encoding referencing a column the DataFrame doesn't have is rejected.

(The phase spec's own generate() bullet list never actually says when the
Agent gets called, despite instructing __init__ to build one -- this
fallback-only role is the most consistent reading against the pipeline's
overall stated architecture, which marks this stage "LLM" while also
describing CHART_SELECTION_RULES as deterministic and applied first.)
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

import altair as alt
import pandas as pd
from pydantic_ai import Agent

from uada.models.intent import QuestionType
from uada.models.result import ChartType, VegaLiteSpec, VisualisationFallback

if TYPE_CHECKING:
    from uada.config import Settings
    from uada.models.intent import AnalyticalIntent
    from uada.models.result import AnalysedResult

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """
You are a data visualisation assistant. You receive a description of a
tabular analytical query result (columns, row count, a few sample rows,
and the user's original question) that a deterministic rule set could not
confidently classify, and must choose the most appropriate chart type and
produce a Vega-Lite v5 "mark" and "encoding".

Rules:
- Output a single JSON object with "mark" and "encoding" keys only.
- Choose "mark" from: "bar", "line", "area", "point", "arc", "rect", "boxplot", "rule".
- Reference only the column names given in the context.
- Do not include a "data" key -- the actual data is supplied separately.
- Keep the spec minimal: mark and encoding only, no titles or themes.
""".strip()

_MAX_BAR_ROWS = 20
_VALID_MARKS = {c.value for c in ChartType}


class _NoChart:
    """Sentinel: the deterministic rules say this result needs no chart at all."""


class _Undetermined:
    """Sentinel: no deterministic rule matched; fall back to the LLM."""


_NO_CHART = _NoChart()
_UNDETERMINED = _Undetermined()


class VisualisationGenerator:
    """Pipeline step 9: AnalysedResult -> VegaLiteSpec | VisualisationFallback."""

    def __init__(self, settings: Settings) -> None:
        """
        Build the PydanticAI agent for `settings.llm_model`.

        `defer_model_check=True` for the same reason as the other LLM
        stages: constructing Agent(model=settings.llm_model, ...) eagerly
        resolves the model string, and the default "ollama:..." model
        raises immediately without OLLAMA_BASE_URL set -- which it never
        is in tests/CI.
        """
        self._settings = settings
        self.agent: Agent[None, dict[str, Any]] = Agent(
            settings.llm_model,
            output_type=dict[str, Any],
            system_prompt=SYSTEM_PROMPT,
            retries=settings.llm_max_retries,
            defer_model_check=True,
        )

    async def generate(
        self, result: AnalysedResult, intent: AnalyticalIntent
    ) -> VegaLiteSpec | VisualisationFallback:
        """Select a chart type and build its Vega-Lite spec for `result`."""
        decision = self._select_chart_type(result, intent)
        if isinstance(decision, _NoChart):
            return VisualisationFallback(reason="Single value; no chart needed.")

        df = pd.DataFrame(
            result.query_result.rows, columns=result.query_result.column_names
        )
        if df.empty:
            return VisualisationFallback(reason="No rows returned.", data_available=False)

        try:
            if isinstance(decision, ChartType):
                spec = self._build_altair_spec(df, decision, result, intent)
            else:
                spec = await self._generate_via_llm(df, result, intent)
        except Exception as exc:  # noqa: BLE001 - any chart-construction failure is a fallback
            logger.warning("Chart construction failed: %s", exc)
            return VisualisationFallback(reason=f"Chart construction failed: {exc}")

        if spec is None:
            return VisualisationFallback(reason="Could not determine an appropriate chart type.")

        is_valid, error = spec.is_valid()
        if not is_valid:
            return VisualisationFallback(reason=f"Generated chart spec invalid: {error}")
        return spec

    # ── Deterministic chart-type selection ──────────────────────────────────

    def _select_chart_type(
        self, result: AnalysedResult, intent: AnalyticalIntent
    ) -> ChartType | _NoChart | _Undetermined:
        row_count = result.query_result.row_count
        numeric_column_count = len(result.numeric_summaries)

        if row_count <= 1 and numeric_column_count <= 1 and not intent.dimensions:
            return _NO_CHART
        if intent.question_type == QuestionType.TIME_SERIES:
            return ChartType.LINE
        if intent.question_type == QuestionType.RANKING and row_count <= _MAX_BAR_ROWS:
            return ChartType.BAR
        if intent.question_type == QuestionType.COMPARISON and result.has_comparison:
            return ChartType.BAR  # grouped, via a color encoding -- see _build_altair_spec
        if len(intent.dimensions) >= 2:
            return ChartType.BAR
        return _UNDETERMINED

    # ── Altair (deterministic) path ─────────────────────────────────────────

    def _build_altair_spec(
        self,
        df: pd.DataFrame,
        chart_type: ChartType,
        result: AnalysedResult,
        intent: AnalyticalIntent,
    ) -> VegaLiteSpec:
        numeric_columns = [c.column for c in result.numeric_summaries if c.column in df.columns]
        if not numeric_columns:
            raise ValueError("No numeric column available to plot.")
        y_column = numeric_columns[0]

        if chart_type == ChartType.LINE:
            x_column = result.time_column or self._first_categorical_column(df, numeric_columns)
            if x_column is None:
                raise ValueError("No column available for the time axis.")
            chart: alt.Chart = alt.Chart(df).mark_line(point=True).encode(
                x=alt.X(f"{x_column}:O", title=x_column),
                y=alt.Y(f"{y_column}:Q", title=y_column),
            )
        else:
            x_column = self._first_categorical_column(df, numeric_columns) or y_column
            encodings: dict[str, Any] = {
                "x": alt.X(f"{x_column}:N", title=x_column, sort="-y"),
                "y": alt.Y(f"{y_column}:Q", title=y_column),
            }
            if intent.question_type == QuestionType.COMPARISON:
                color_column = self._first_categorical_column(
                    df, [*numeric_columns, x_column]
                )
                if color_column is not None:
                    encodings["color"] = alt.Color(f"{color_column}:N", title=color_column)
            chart = alt.Chart(df).mark_bar().encode(**encodings)

        vega_dict: dict[str, Any] = chart.to_dict()
        return VegaLiteSpec(
            spec=vega_dict, chart_type=chart_type, title=self._build_title(intent)
        )

    def _first_categorical_column(
        self, df: pd.DataFrame, exclude: list[str]
    ) -> str | None:
        for column in df.columns:
            if column not in exclude:
                return str(column)
        return None

    def _build_title(self, intent: AnalyticalIntent) -> str | None:
        question = intent.raw_question.strip()
        if not question:
            return None
        return question if len(question) <= 100 else question[:97] + "..."

    # ── LLM fallback path ────────────────────────────────────────────────────

    async def _generate_via_llm(
        self, df: pd.DataFrame, result: AnalysedResult, intent: AnalyticalIntent
    ) -> VegaLiteSpec | None:
        context = {
            "question": intent.raw_question,
            "question_type": intent.question_type.value,
            "columns": list(df.columns),
            "row_count": len(df),
            "sample_rows": df.head(5).to_dict(orient="records"),
            "narrative_insight": result.narrative_insight,
        }
        message = f"Result context:\n{json.dumps(context, indent=2, default=str)}"
        agent_result = await self.agent.run(message)
        spec_dict = agent_result.output

        mark_name = self._mark_name(spec_dict.get("mark"))
        if mark_name is None or mark_name not in _VALID_MARKS:
            logger.warning("LLM viz spec has an invalid or missing mark: %r", spec_dict.get("mark"))
            return None
        if not self._references_valid_columns(spec_dict, set(df.columns)):
            logger.warning("LLM viz spec references a column not present in the result.")
            return None

        spec_dict = dict(spec_dict)
        spec_dict["data"] = {"values": df.to_dict(orient="records")}
        spec_dict.setdefault("$schema", "https://vega.github.io/schema/vega-lite/v5.json")

        return VegaLiteSpec(
            spec=spec_dict, chart_type=ChartType(mark_name), title=self._build_title(intent)
        )

    def _mark_name(self, mark: Any) -> str | None:
        if isinstance(mark, str):
            return mark
        if isinstance(mark, dict):
            value = mark.get("type")
            return value if isinstance(value, str) else None
        return None

    def _references_valid_columns(self, spec_dict: dict[str, Any], columns: set[str]) -> bool:
        encoding = spec_dict.get("encoding")
        if not isinstance(encoding, dict):
            return False
        for channel in encoding.values():
            if not isinstance(channel, dict):
                continue
            field = channel.get("field")
            if field is not None and field not in columns:
                return False
        return True
