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
        "pg_ls_dir",
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
    ) -> None:
        """
        Args:
            allowed_tables: Set of table names (lowercase) permitted in queries.
                            Built from the SCL with security exclusions already applied.
            max_subquery_depth: Maximum nesting depth of subqueries.
            inject_limit: If True, adds LIMIT {default_limit} when no LIMIT is present.
            default_limit: The LIMIT value to inject.
        """
        self._allowed_tables: frozenset[str] = frozenset(
            t.lower() for t in allowed_tables
        )
        self._max_subquery_depth = max_subquery_depth
        self._inject_limit = inject_limit
        self._default_limit = default_limit

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
        if self._allowed_tables:  # Empty set means "allow all" (e.g. during testing)
            for table_name in referenced_tables:
                # Strip schema prefix for comparison: "public.orders" → "orders"
                bare_name = table_name.split(".")[-1].lower()
                if bare_name not in self._allowed_tables:
                    violations.append(
                        ValidationViolation(
                            violation_type=ViolationType.TABLE_NOT_IN_ALLOWLIST,
                            detail=f"Table '{table_name}' is not in the allowed schema.",
                            user_message="Query references a table that is not available.",
                        )
                    )

        # ── Rule 5: Dangerous functions ────────────────────────────────────
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

        # ── Determine safety ───────────────────────────────────────────────
        if violations:
            return ValidationResult(
                is_safe=False,
                violations=violations,
                tables_referenced=list(referenced_tables),
                parse_time_ms=(time.perf_counter() - start) * 1000,
            )

        # ── LIMIT injection ────────────────────────────────────────────────
        normalised = stripped
        if self._inject_limit and isinstance(statement, exp.Select):
            if statement.args.get("limit") is None:
                normalised = (
                    sqlglot.parse_one(stripped, dialect=dialect)
                    .limit(self._default_limit)
                    .sql(dialect=dialect)
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
            if name and name.lower() in cte_names:
                continue
            db = table_node.args.get("db")
            schema = table_node.args.get("catalog")
            parts = [p for p in [schema, db, name] if p]
            tables.add(".".join(str(p) for p in parts))
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
