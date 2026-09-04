"""
Semantic Context Layer — YAML Loader
=====================================
Parses and validates the SCL YAML file against `SemanticContextLayer`
(uada/scl/schema.py). Also serializes an SCL back to YAML, used by
`scripts/onboard_db.py` to write the candidate file the SCLReflector
produces.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

import yaml
from pydantic import ValidationError

from uada.scl.schema import SemanticContextLayer

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


class _SCLSafeLoader(yaml.SafeLoader):
    """
    `yaml.SafeLoader` restricted to YAML 1.2 boolean resolution.

    PyYAML's default SafeLoader follows YAML 1.1, which resolves bare
    `on`/`off`/`yes`/`no`/`y`/`n` as booleans. `JoinDefinition.on` (the SQL
    join condition field) is exactly such a word: an unquoted `on:` key in
    the YAML silently becomes the boolean key `True`, dropping the field
    with no parse error. Restricting the boolean resolver to `true`/`false`
    (YAML 1.2) avoids this whole class of silent corruption.
    """


_SCLSafeLoader.yaml_implicit_resolvers = {
    first_char: [
        (tag, regexp)
        for tag, regexp in resolvers
        if tag != "tag:yaml.org,2002:bool"
    ]
    for first_char, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_SCLSafeLoader.add_implicit_resolver(
    "tag:yaml.org,2002:bool",
    re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"),
    list("tTfF"),
)


class SCLLoadError(Exception):
    """Raised when the SCL YAML cannot be read, parsed, or schema-validated."""


class SCLLoader:
    """Loads and saves the Semantic Context Layer YAML file."""

    @staticmethod
    def load(path: Path) -> SemanticContextLayer:
        """
        Read, parse, and validate the SCL YAML file at `path`.

        Returns:
            A validated SemanticContextLayer.

        Raises:
            SCLLoadError: The file is missing/unreadable, is not valid YAML,
                or fails schema validation. The message identifies the YAML
                line/column for parse errors, and the offending field path
                for schema validation errors.
        """
        try:
            raw_text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise SCLLoadError(f"Could not read SCL file '{path}': {exc}") from exc

        try:
            data = yaml.load(raw_text, Loader=_SCLSafeLoader)
        except yaml.YAMLError as exc:
            mark = getattr(exc, "problem_mark", None)
            location = f" at line {mark.line + 1}, column {mark.column + 1}" if mark else ""
            raise SCLLoadError(f"Invalid YAML in '{path}'{location}: {exc}") from exc

        if data is None:
            raise SCLLoadError(f"SCL file '{path}' is empty.")
        if not isinstance(data, dict):
            raise SCLLoadError(
                f"SCL file '{path}' must contain a YAML mapping at the top level, "
                f"got {type(data).__name__}."
            )

        try:
            scl = SemanticContextLayer.model_validate(data)
        except ValidationError as exc:
            raise SCLLoadError(SCLLoader._format_validation_error(path, exc)) from exc

        logger.info(
            "Loaded SCL '%s': %d table(s), %d metric(s), %d join(s).",
            scl.database.name,
            len(scl.tables),
            len(scl.metrics),
            len(scl.joins),
        )
        return scl

    @staticmethod
    def _format_validation_error(path: Path, exc: ValidationError) -> str:
        """Render a ValidationError as one line per offending field path."""
        lines = [f"SCL file '{path}' failed schema validation:"]
        for error in exc.errors():
            field_path = ".".join(str(part) for part in error["loc"]) or "<root>"
            lines.append(f"  - {field_path}: {error['msg']}")
        return "\n".join(lines)

    @staticmethod
    def save(scl: SemanticContextLayer, path: Path) -> None:
        """
        Serialize `scl` to YAML and write it to `path`.

        Creates parent directories as needed. Uses JSON-compatible dumping
        so enum members and Path values serialize as plain scalars.
        """
        data = scl.model_dump(mode="json")
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)
        logger.info("Saved SCL to '%s'.", path)
