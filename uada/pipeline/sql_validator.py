"""
SQL Security Validator
=======================
Deterministic, AST-based SQL security enforcement.

Architecture principle (ADR-007):
- The LLM is NOT a security boundary.
- This validator is the security boundary.
- Violations STOP execution immediately. No LLM retry. No fallback.
- The validator never calls an LLM.

Five defense layers (this module implements layers 3 and partially 4):
  Layer 1: Read-only database credentials       (DatabaseAdapter)
  Layer 2: Schema allowlist                     (SCLManager → this validator)
  Layer 3: SQLGlot AST validation               (THIS MODULE)
  Layer 4: Execution constraints (timeout/rows) (QueryExecutor)
  Layer 5: Prompt injection protection           (Pipeline orchestrator)

SQLGlot note (from its own documentation):
  SQLGlot is intentionally lenient — a query that parses successfully
  may still fail at execution time. It is a parser/transpiler, not a
  security validator. The security rules below are UADA's custom policy
  applied ON TOP of the SQLGlot AST.

Usage:
    validator = SQLValidator(allowed_tables={"orders", "customers"})
    result = validator.validate(sql, dialect="postgres")
    if not result.is_safe:
        # log incident, return error to user, DO NOT execute
        raise SecurityViolation(result.violations[0])
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

import sqlglot
import sqlglot.expressions as exp


# ── Dangerous SQL functions (deny-list) ───────────────────────────────────────
# Functions that should never appear in analytical queries.
# This list is conservative — add more as discovered.

DANGEROUS_FUNCTIONS: frozenset[str] = frozenset(
    {
        # SQL Server / T-SQL
        "xp_cmdshell",
        "xp_regread",
        "xp_fileexist",
        "xp_dirtree",
        "openrowset",
        "opendatasource",
        "bulk",
        # MySQL
        "load_file",
        "into_outfile",
        "into_dumpfile",
        # PostgreSQL
        "pg_read_file",
        "pg_read_binary_file",
        "pg_ls_dir",
        "pg_ls_waldir",
        "pg_stat_file",
        "pg_sleep",
        "copy",
        "lo_import",
        "lo_export",
        # General
        "eval",
        "exec",
        "execute",
        "sleep",
        "benchmark",
        "user",
        "current_user",
        "session_user",
        "system_user",
        "version",
        "@@version",
        # DuckDB / file and network access
        "read_csv",
        "read_csv_auto",
        "read_parquet",
        "parquet_scan",
        "read_json",
        "read_json_auto",
        "read_text",
        "glob",
        "http_get",
        "httpfs",
        "sqlite_scan",
        "load_extension",
        "install",
        "attach",
        "pragma",
    }
)

# System schema prefixes — queries touching these are rejected.
SYSTEM_SCHEMA_PREFIXES: frozenset[str] = frozenset(
    {
        "information_schema",
        "pg_catalog",
        "pg_temp",
        "pg_toast",
        "mysql",
        "sys",
        "msdb",
        "master",
        "tempdb",
        "performance_schema",
        "sqlite_master",
        "sqlite_sequence",
    }
)

# Statement types that are NEVER permitted.
FORBIDDEN_STATEMENT_TYPES: tuple[type[exp.Expression], ...] = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.TruncateTable,
    exp.Command,     # EXEC, CALL, etc.
    exp.Transaction,
    exp.Commit,
    exp.Rollback,
    exp.Grant,
    exp.Revoke,
    exp.Use,
    exp.Set,
)


# ── Result types ──────────────────────────────────────────────────────────────


class ViolationType(str, Enum):
    NON_SELECT_STATEMENT = "non_select_statement"
    MULTIPLE_STATEMENTS = "multiple_statements"
    SYSTEM_TABLE_ACCESS = "system_table_access"
    TABLE_NOT_IN_ALLOWLIST = "table_not_in_allowlist"
    DANGEROUS_FUNCTION = "dangerous_function"
    SUBQUERY_DEPTH_EXCEEDED = "subquery_depth_exceeded"
    PARSE_FAILURE = "parse_failure"
    COMMENT_INJECTION = "comment_injection"
    EMPTY_QUERY = "empty_query"
    WRITE_OPERATION = "write_operation"
    ROW_LOCK = "row_lock"
    LIMIT_EXCEEDED = "limit_exceeded"
    COLUMN_NOT_ALLOWED = "column_not_allowed"
    ROW_FILTER_INVALID = "row_filter_invalid"


@dataclass
class ValidationViolation:
    violation_type: ViolationType
    detail: str
    user_message: str


@dataclass
class ValidationResult:
    is_safe: bool
    violations: list[ValidationViolation] = field(default_factory=list)
    normalised_sql: str | None = None  # SQL with LIMIT injected if needed
    tables_referenced: list[str] = field(default_factory=list)
    parse_time_ms: float = 0.0

    @property
    def first_violation(self) -> ValidationViolation | None:
        return self.violations[0] if self.violations else None


# ── Validator ─────────────────────────────────────────────────────────────────


class SQLValidator:
    """
    Deterministic SQL security validator.

    Instantiate once per database connection with the allowed table set.
    Call validate() for every generated SQL before execution.

    Thread-safe: validate() has no shared mutable state.
    """

    def __init__(
        self,
        allowed_tables: set[str],
        max_subquery_depth: int = 3,
        inject_limit: bool = True,
        default_limit: int = 1000,
        excluded_columns: dict[str, set[str]] | None = None,
        row_filters: dict[str, str] | None = None,
    ) -> None:
        """
        Args:
            allowed_tables: Set of table names (lowercase) permitted in queries.
                            Built from the SCL with security exclusions already applied.
            max_subquery_depth: Maximum nesting depth of subqueries.
            inject_limit: If True, adds LIMIT {default_limit} when no LIMIT is present.
            default_limit: The LIMIT value to inject.
            excluded_columns: Per-table column names that must never be
                              selected or referenced.
            row_filters: Per-table predicates appended to every query touching
                         that table.
        """
        # An empty allowlist is fail-closed.  Treating it as "all tables" is
        # unsafe in production because a missing or malformed semantic model
        # would silently become unrestricted database access.
        self._allowed_tables: frozenset[str] = frozenset(
            t.strip().lower() for t in allowed_tables if t and t.strip()
        )
        self._max_subquery_depth = max_subquery_depth
        self._inject_limit = inject_limit
        self._default_limit = default_limit
        self._excluded_columns = {
            table.lower(): {column.lower() for column in columns}
            for table, columns in (excluded_columns or {}).items()
        }
        self._row_filters = {
            table.lower(): predicate.strip()
            for table, predicate in (row_filters or {}).items()
            if predicate and predicate.strip()
        }

    def validate(self, sql: str, dialect: str = "postgres") -> ValidationResult:
        """
        Validate a SQL string against the security policy.

        Returns a ValidationResult. If is_safe is False, the caller MUST NOT
        execute the SQL. Log the violation and return an error to the user.

        This method never raises an exception — all errors are represented
        in the ValidationResult.
        """
        import time

        start = time.perf_counter()

        # ── Guard: empty input ─────────────────────────────────────────────
        stripped = sql.strip()
        if not stripped:
            return ValidationResult(
                is_safe=False,
                violations=[
                    ValidationViolation(
                        violation_type=ViolationType.EMPTY_QUERY,
                        detail="SQL string is empty.",
                        user_message="No query was generated.",
                    )
                ],
            )

        # ── Guard: comment injection ───────────────────────────────────────
        # Inline comments can hide malicious SQL after the visible portion.
        if "--" in stripped or "/*" in stripped:
            return ValidationResult(
                is_safe=False,
                violations=[
                    ValidationViolation(
                        violation_type=ViolationType.COMMENT_INJECTION,
                        detail="SQL contains comment sequences (-- or /*). "
                               "Comments are not permitted in generated SQL.",
                        user_message="Generated query contains unsupported syntax.",
                    )
                ],
            )

        # ── Parse ──────────────────────────────────────────────────────────
        try:
            statements = sqlglot.parse(stripped, dialect=dialect, error_level=None)
        except Exception as exc:
            return ValidationResult(
                is_safe=False,
                violations=[
                    ValidationViolation(
                        violation_type=ViolationType.PARSE_FAILURE,
                        detail=f"SQLGlot parse error: {exc}",
                        user_message="Could not parse the generated query.",
                    )
                ],
            )

        # Filter out None results from the parser
        statements = [s for s in statements if s is not None]

        # ── Guard: multiple statements ─────────────────────────────────────
        if len(statements) > 1:
            return ValidationResult(
                is_safe=False,
                violations=[
                    ValidationViolation(
                        violation_type=ViolationType.MULTIPLE_STATEMENTS,
                        detail=f"Query contains {len(statements)} statements. "
                               "Only a single SELECT statement is permitted.",
                        user_message="Generated query is invalid.",
                    )
                ],
            )

        if not statements:
            return ValidationResult(
                is_safe=False,
                violations=[
                    ValidationViolation(
                        violation_type=ViolationType.PARSE_FAILURE,
                        detail="Parser returned no statements.",
                        user_message="Could not parse the generated query.",
                    )
                ],
            )

        statement = statements[0]

        violations: list[ValidationViolation] = []

        # ── Rule 1: SELECT-only ────────────────────────────────────────────
        if isinstance(statement, FORBIDDEN_STATEMENT_TYPES):
            violations.append(
                ValidationViolation(
                    violation_type=ViolationType.NON_SELECT_STATEMENT,
                    detail=f"Statement type is {type(statement).__name__}. "
                           "Only SELECT statements are permitted.",
                    user_message="This query cannot be executed.",
                )
            )

        elif not isinstance(statement, (exp.Select, exp.Union, exp.Intersect, exp.Except)):
            violations.append(
                ValidationViolation(
                    violation_type=ViolationType.NON_SELECT_STATEMENT,
                    detail=f"Unexpected statement type: {type(statement).__name__}.",
                    user_message="This query cannot be executed.",
                )
            )

        # Stop here if not a SELECT — further checks are irrelevant
        if violations:
            return ValidationResult(
                is_safe=False,
                violations=violations,
                parse_time_ms=(time.perf_counter() - start) * 1000,
            )

        # A SELECT wrapper can still contain a write (for example a DELETE in
        # a CTE), SELECT INTO, or a row-locking clause.  Walk the complete AST
        # instead of checking only the root node.
        nested_writes = [
            node for node in statement.walk()
            if isinstance(node, FORBIDDEN_STATEMENT_TYPES)
        ]
        if nested_writes:
            violations.append(
                ValidationViolation(
                    violation_type=ViolationType.WRITE_OPERATION,
                    detail="Query contains a write operation inside a SELECT tree.",
                    user_message="This query cannot be executed.",
                )
            )
        if any(isinstance(node, exp.Into) for node in statement.walk()):
            violations.append(
                ValidationViolation(
                    violation_type=ViolationType.WRITE_OPERATION,
                    detail="SELECT INTO is not permitted.",
                    user_message="This query cannot be executed.",
                )
            )
        if any(isinstance(node, exp.Lock) for node in statement.walk()):
            violations.append(
                ValidationViolation(
                    violation_type=ViolationType.ROW_LOCK,
                    detail="Row-locking clauses are not permitted for read-only analytics.",
                    user_message="This query cannot be executed.",
                )
            )

        # ── Rule 2: Extract all referenced tables ──────────────────────────
        referenced_tables = self._extract_tables(statement)

        # ── Rule 3: System schema access ───────────────────────────────────
        for table_name in referenced_tables:
            table_lower = table_name.lower()
            for sys_prefix in SYSTEM_SCHEMA_PREFIXES:
                if table_lower == sys_prefix or table_lower.startswith(f"{sys_prefix}."):
                    violations.append(
                        ValidationViolation(
                            violation_type=ViolationType.SYSTEM_TABLE_ACCESS,
                            detail=f"Query references system table/schema: '{table_name}'.",
                            user_message="This query cannot be executed.",
                        )
                    )

        # ── Rule 4: Table allowlist ────────────────────────────────────────
        for table_name in referenced_tables:
            # Qualified names are matched exactly.  In particular,
            # tenant_b.orders must not match an allowlisted bare `orders`.
            table_lower = table_name.lower()
            bare_name = table_lower.rsplit(".", 1)[-1]
            allowed = table_lower in self._allowed_tables or (
                "." not in table_lower and bare_name in self._allowed_tables
            )
            if not allowed:
                violations.append(
                    ValidationViolation(
                        violation_type=ViolationType.TABLE_NOT_IN_ALLOWLIST,
                        detail=f"Table '{table_name}' is not in the allowed schema.",
                        user_message="Query references a table that is not available.",
                    )
                )

        # ── Rule 4b: Column exclusions ───────────────────────────────────
        table_aliases = {
            node.alias.lower(): node.name.lower()
            for node in statement.find_all(exp.Table)
            if node.name and node.alias
        }
        for star in statement.find_all(exp.Star):
            parent = star.parent
            qualifier = (parent.table or "").lower() if isinstance(parent, exp.Column) else ""
            physical_table = table_aliases.get(qualifier, qualifier)
            blocked = (
                self._excluded_columns.get(physical_table, set())
                if physical_table
                else {
                    column
                    for ref in referenced_tables
                    for column in self._excluded_columns.get(ref.lower().rsplit(".", 1)[-1], set())
                }
            )
            if blocked:
                violations.append(
                    ValidationViolation(
                        violation_type=ViolationType.COLUMN_NOT_ALLOWED,
                        detail="Wildcard selection would expose a protected column.",
                        user_message="The query selects protected columns.",
                    )
                )
        for column_node in statement.find_all(exp.Column):
            column_name = (column_node.name or "").lower()
            table_name = (column_node.table or "").lower()
            if table_name:
                physical_table = table_aliases.get(table_name, table_name)
                blocked = self._excluded_columns.get(physical_table, set())
                if column_name in blocked:
                    violations.append(
                        ValidationViolation(
                            violation_type=ViolationType.COLUMN_NOT_ALLOWED,
                            detail=f"Column '{physical_table}.{column_name}' is excluded by policy.",
                            user_message="The query references a protected column.",
                        )
                    )
            elif any(column_name in columns for columns in self._excluded_columns.values()):
                violations.append(
                    ValidationViolation(
                        violation_type=ViolationType.COLUMN_NOT_ALLOWED,
                        detail=f"Column '{column_name}' is excluded by policy.",
                        user_message="The query references a protected column.",
                    )
                )

        # ── Rule 5: Dangerous functions ───────────────────────────────────

        for func_node in statement.find_all(exp.Anonymous, exp.Func):
            func_name = ""
            if isinstance(func_node, exp.Anonymous):
                func_name = func_node.name.lower()
            elif hasattr(func_node, "sql_name"):
                func_name = func_node.sql_name().lower()

            if func_name in DANGEROUS_FUNCTIONS:
                violations.append(
                    ValidationViolation(
                        violation_type=ViolationType.DANGEROUS_FUNCTION,
                        detail=f"Query contains dangerous function: '{func_name}'.",
                        user_message="Generated query contains unsupported operations.",
                    )
                )

        # ── Rule 6: Subquery depth ─────────────────────────────────────────
        depth = self._measure_subquery_depth(statement)
        if depth > self._max_subquery_depth:
            violations.append(
                ValidationViolation(
                    violation_type=ViolationType.SUBQUERY_DEPTH_EXCEEDED,
                    detail=f"Subquery nesting depth {depth} exceeds maximum {self._max_subquery_depth}.",
                    user_message="Generated query is too complex.",
                )
            )

        # ── Rule 7: Database row policies ─────────────────────────────────
        # Apply trusted predicates after validation and before SQL rendering so
        # an LLM cannot omit a configured tenant/visibility condition. UNION
        # branches are rejected when a policy is configured because applying a
        # predicate only to the outer SELECT would be ambiguous.
        applicable_filters = [
            (table, predicate)
            for table, predicate in self._row_filters.items()
            if any(
                ref.lower() == table or ref.lower().rsplit(".", 1)[-1] == table
                for ref in referenced_tables
            )
        ]
        if applicable_filters:
            targets = list(statement.find_all(exp.Select))
            if not isinstance(statement, exp.Select) or len(targets) != 1:
                violations.append(
                    ValidationViolation(
                        violation_type=ViolationType.ROW_FILTER_INVALID,
                        detail="Configured row policy cannot be safely applied to this query shape.",
                        user_message="This query shape is not permitted for the selected data policy.",
                    )
                )
            else:
                select = targets[0]
                aliases = {
                    node.name.lower(): (node.alias or node.name)
                    for node in select.find_all(exp.Table)
                    if node.name
                }
                for table, predicate in applicable_filters:
                    try:
                        policy_expression = sqlglot.parse_one(predicate, dialect=dialect)
                    except Exception as exc:
                        violations.append(
                            ValidationViolation(
                                violation_type=ViolationType.ROW_FILTER_INVALID,
                                detail=f"Configured row policy for '{table}' could not be parsed: {exc}",
                                user_message="The selected data policy is invalid.",
                            )
                        )
                        continue
                    alias = aliases.get(table)
                    if alias and alias != table:
                        for column_node in policy_expression.find_all(exp.Column):
                            if (column_node.table or "").lower() == table:
                                column_node.set("table", exp.Identifier(this=alias))
                    select = select.where(policy_expression)
                statement = select

        # ── Determine safety ───────────────────────────────────────────────
        if violations:
            return ValidationResult(
                is_safe=False,
                violations=violations,
                tables_referenced=list(referenced_tables),
                parse_time_ms=(time.perf_counter() - start) * 1000,
            )

        # ── LIMIT injection ────────────────────────────────────────────────
        normalised = statement.sql(dialect=dialect) if applicable_filters else stripped
        if self._inject_limit:
            parsed = statement
            existing_limit = parsed.args.get("limit") if hasattr(parsed, "args") else None
            limit_value: int | None = None
            if existing_limit is not None:
                expression = existing_limit.args.get("expression")
                if isinstance(expression, exp.Literal) and not expression.is_string:
                    try:
                        limit_value = int(expression.this)
                    except (TypeError, ValueError):
                        limit_value = None
                if limit_value is None or limit_value < 0 or limit_value > self._default_limit:
                    parsed = parsed.limit(self._default_limit)
                    normalised = parsed.sql(dialect=dialect)
                    if limit_value is not None and limit_value > self._default_limit:
                        violations.append(
                            ValidationViolation(
                                violation_type=ViolationType.LIMIT_EXCEEDED,
                                detail=f"Requested LIMIT {limit_value} exceeds policy maximum {self._default_limit}.",
                                user_message="This query exceeds the maximum result size.",
                            )
                        )
            else:
                # `limit()` on a UNION applies to the complete set, avoiding
                # the unbounded result path that a Select-only check misses.
                normalised = parsed.limit(self._default_limit).sql(dialect=dialect)

        if violations:
            return ValidationResult(
                is_safe=False,
                violations=violations,
                tables_referenced=list(referenced_tables),
                parse_time_ms=(time.perf_counter() - start) * 1000,
            )

        return ValidationResult(
            is_safe=True,
            normalised_sql=normalised,
            tables_referenced=list(referenced_tables),
            parse_time_ms=(time.perf_counter() - start) * 1000,
        )

    # ── Private helpers ────────────────────────────────────────────────────────

    def _extract_tables(self, statement: exp.Expression) -> set[str]:
        """Extract all physical table names referenced in the statement.

        CTE names (defined by a WITH clause) are excluded: SQLGlot represents
        a CTE reference as an ordinary ``exp.Table`` node, but it is a
        query-local alias, not a real table subject to the allowlist.
        """
        cte_names: set[str] = set()
        for with_node in statement.find_all(exp.With):
            for cte in with_node.expressions:
                alias = cte.alias
                if alias:
                    cte_names.add(alias.lower())

        tables: set[str] = set()
        for table_node in statement.find_all(exp.Table):
            name = table_node.name
            # Only an unqualified reference can resolve to a CTE.  A
            # qualified `private.secrets` remains a physical table even if a
            # CTE named `secrets` exists in the query.
            db = table_node.args.get("db")
            catalog = table_node.args.get("catalog")
            if name and not db and not catalog and name.lower() in cte_names:
                continue
            parts = [p for p in [catalog, db, name] if p]
            tables.add(".".join(getattr(p, "name", str(p)) for p in parts))
        return tables

    def _measure_subquery_depth(self, node: exp.Expression, current_depth: int = 0) -> int:
        """Recursively measure maximum subquery nesting depth.

        Only direct children are traversed (via ``iter_expressions``), and
        depth increments once per ``exp.Subquery`` boundary. SQLGlot wraps
        every nested SELECT (in FROM, IN, EXISTS, scalar position, etc.) in
        a Subquery node around the inner Select, so counting Subquery alone
        — rather than Subquery and Select separately — avoids double-counting
        each physical nesting level. A full-tree walk (``.walk()``) would also
        re-visit already-nested descendants from every ancestor, inflating
        the depth further; iterating direct children avoids that too.
        """
        max_depth = current_depth
        for child in node.iter_expressions():
            if isinstance(child, exp.Subquery):
                depth = self._measure_subquery_depth(child, current_depth + 1)
            else:
                depth = self._measure_subquery_depth(child, current_depth)
            max_depth = max(max_depth, depth)
        return max_depth
