/**
 * TypeScript mirrors of the Pydantic response models in uada/models/result.py
 * and the connection-management models in uada/api/routes/connections.py.
 * Every field is optional where the backend documents it as optional --
 * the UI must render whatever subset of fields is present.
 */

// ── Connections ────────────────────────────────────────────────────────────

export interface ConnectionSummary {
  connection_id: string;
  name: string;
  dialect: string;
  status?: string;
  table_count?: number;
  last_test_ok?: boolean | null;
  created_at?: string;
}

export interface ConnectionCreateRequest {
  name: string;
  dialect: string;
  host: string;
  port: number;
  database: string;
  username: string;
  password: string;
  ssl?: boolean;
  test_on_create?: boolean;
}

export interface ConnectionCreatedResponse {
  connection_id: string;
  name: string;
  dialect: string;
  test_result: TestConnectionResult | null;
}

export interface TestConnectionResult {
  ok: boolean;
  latency_ms?: number | null;
  server_version?: string | null;
  error?: string | null;
}

export interface DiscoverResponse {
  table_count: number;
  column_count: number;
  relationship_count: number;
  row_estimate: number;
}

export interface ColumnRefreshSummary {
  column_name: string;
  data_type: string;
  null_pct?: number | null;
  distinct_count?: number | null;
  min_value?: string | null;
  max_value?: string | null;
  is_temporal: boolean;
}

export interface TableRefreshSummary {
  table_name: string;
  row_count?: number | null;
  column_count: number;
  columns: ColumnRefreshSummary[];
}

export interface SchemaRefreshResponse {
  connection_id: string;
  schema_fingerprint: string;
  table_count: number;
  column_count: number;
  relationship_count?: number;
  profiled_table_count: number;
  profiled_column_count: number;
  tables: TableRefreshSummary[];
}

// ── Query result (row-level data) ───────────────────────────────────────────

export interface ColumnMeta {
  name: string;
  data_type: string;
  nullable: boolean;
}

export interface QueryResult {
  columns: ColumnMeta[];
  rows: unknown[][];
  row_count: number;
  is_truncated: boolean;
  truncated_at?: number | null;
  executed_sql: string | null;
  execution_time_ms: number;
  database_dialect: string;
}

// ── Visualisation ──────────────────────────────────────────────────────────

export interface VegaLiteSpec {
  spec: Record<string, unknown>;
  chart_type: string;
  title?: string | null;
  reasoning?: string | null;
}

export interface KpiSpec {
  value: number | string;
  label: string;
  formatted_value: string;
  trend_direction?: "up" | "down" | "flat" | null;
  trend_pct?: number | null;
  comparison_label?: string | null;
  unit?: string | null;
}

export interface VisualisationFallback {
  reason: string;
  data_available: boolean;
}

export type Visualisation = VegaLiteSpec | KpiSpec | VisualisationFallback;

export function isKpiSpec(v: Visualisation | null | undefined): v is KpiSpec {
  return !!v && "value" in v && "label" in v && "formatted_value" in v;
}

export function isVegaLiteSpec(v: Visualisation | null | undefined): v is VegaLiteSpec {
  return !!v && "spec" in v;
}

export function isVisualisationFallback(
  v: Visualisation | null | undefined,
): v is VisualisationFallback {
  return !!v && "reason" in v && !("spec" in v);
}

// ── P3 advanced analytics ──────────────────────────────────────────────────

export interface CorrelationPair {
  col_a: string;
  col_b: string;
  correlation: number;
  strength?: string;
}

export interface CorrelationResult {
  method: string;
  matrix: Record<string, Record<string, number>>;
  top_pairs: CorrelationPair[];
  min_rows_met: boolean;
  skipped: boolean;
}

export interface AnomalyRow {
  row_index: number;
  anomaly_score: number;
  is_anomaly: boolean;
  column_deviations: Record<string, number>;
}

export interface AnomalyResult {
  method: string;
  anomaly_rows: AnomalyRow[];
  anomaly_count: number;
  contamination: number | null;
  feature_columns: string[];
  skipped: boolean;
  skip_reason?: string | null;
}

export interface ForecastPoint {
  period: string;
  forecast: number;
  lower_ci: number;
  upper_ci: number;
  group?: string | null;
}

export interface ForecastResult {
  method: string;
  forecast_column: string;
  time_column: string;
  historical_periods: number;
  forecast_periods: number;
  points: ForecastPoint[];
  model_fit_rmse?: number | null;
  skipped: boolean;
  skip_reason?: string | null;
}

// ── P4-A insights depth ─────────────────────────────────────────────────────

export interface ColumnQualityIssue {
  column: string;
  issue_type: string;
  severity: "high" | "medium" | "low";
  detail: string;
}

export interface DataQualityReport {
  null_rates: Record<string, number>;
  duplicate_row_count: number;
  duplicate_row_pct: number;
  issues: ColumnQualityIssue[];
  overall_quality_score: number;
  summary: string;
  skipped: boolean;
  skip_reason?: string | null;
}

export interface ExplainabilityContext {
  sql_breakdown: string;
  tables_referenced: string[];
  filters_applied: string[];
  aggregations: string[];
  assumptions: string[];
  chart_rationale?: string | null;
  calculation_steps: string[];
}

export interface GeneratedInsights {
  key_findings: string[];
  drivers: string[];
  recommendations: string[];
  data_quality_notes: string[];
  confidence: "high" | "medium" | "low" | string;
}

export interface UADAError {
  stage: string;
  error_type: string;
  message: string;
  user_message: string;
  is_retryable: boolean;
  clarification_needed?: string | null;
}

// ── Final response ──────────────────────────────────────────────────────────

export interface UADAResponse {
  session_id: string;
  turn_id: number;
  timestamp: string;

  answer?: string | null;
  sql?: string | null;
  row_count?: number | null;
  is_truncated: boolean;
  execution_time_ms?: number | null;
  query_result?: QueryResult | null;
  visualisation?: Visualisation | null;
  supplementary_visualisations: Visualisation[];
  key_finding?: string | null;
  key_findings_bullets: string[];
  drivers: string[];
  anomaly_descriptions: string[];
  suggested_questions: string[];

  correlation_result?: CorrelationResult | null;
  anomaly_result?: AnomalyResult | null;
  forecast_result?: ForecastResult | null;

  data_quality?: DataQualityReport | null;
  explainability?: ExplainabilityContext | null;
  generated_insights?: GeneratedInsights | null;

  error?: UADAError | null;

  question_type?: string | null;
  complexity_tier?: string | null;
  investigation_steps?: number | null;
  evidence_nodes: string[];
  tables_used: string[];
  critic_score?: number | null;
  pipeline_duration_ms?: number | null;
}

// ── Client-side chat state (not part of the API contract) ──────────────────

export interface ChatMessage {
  id: string;
  role: "user" | "assistant";
  text: string;
  createdAt: number;
  response?: UADAResponse;
  errorText?: string;
}
