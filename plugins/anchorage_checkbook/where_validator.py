"""Input validators for the Open Checkbook escape-hatch query tool.

This module deliberately does NOT copy the keyword blocklist from
``plugins/arcgis/where_validator.py``. That validator rejects the bare
token ``UNION`` (among others), which on the Checkbook breaks real
queries: 68 rows across 14+ distinct vendors contain "UNION" --
'IBEW Local Union 1547', 'Credit Union 1', 'Plumbers & Steamfitters
Union Local 367', 'UAA STUDENT UNION', and more. A model that writes
``Vendor_Name = 'Credit Union 1'`` must not be told its query is a SQL
injection.

The replacement targets the actual injection SHAPES with word-boundary
regexes (``UNION ... SELECT``, ``INSERT INTO``, ...) after masking
string-literal contents, so quoted data values can never trip the scan
and bare tokens that are legal in names never match. Structured-param
tools bypass this entirely -- the server composes their WHERE clauses
itself with proper escaping.
"""

import difflib
import re
from typing import Iterable, Optional


class CheckbookWhereValidator:
    """Validates raw WHERE clause strings for the escape-hatch tool."""

    # Injection SHAPES (multi-token statement patterns), not bare
    # keywords. 'UNION' alone is legal data; 'UNION SELECT' is not.
    INJECTION_SHAPES = [
        (r"\bUNION\s+(ALL\s+)?SELECT\b", "UNION ... SELECT"),
        (r"\bSELECT\b[\s\S]*\bFROM\b", "SELECT ... FROM"),
        (r"\bINSERT\s+INTO\b", "INSERT INTO"),
        (r"\bUPDATE\s+\w+\s+SET\b", "UPDATE ... SET"),
        (r"\bDELETE\s+FROM\b", "DELETE FROM"),
        (r"\bDROP\s+(TABLE|VIEW|INDEX|DATABASE)\b", "DROP"),
        (r"\bALTER\s+TABLE\b", "ALTER TABLE"),
        (r"\bCREATE\s+(TABLE|VIEW|INDEX|DATABASE)\b", "CREATE"),
        (r"\bTRUNCATE\s+TABLE\b", "TRUNCATE TABLE"),
        (r"\bMERGE\s+INTO\b", "MERGE INTO"),
        (r"\bEXEC(UTE)?\s*\(", "EXEC("),
        (r"\bEXEC(UTE)?\s+\w", "EXEC"),
        (r"\bGRANT\s+\w+\s+ON\b", "GRANT ... ON"),
        (r"\bREVOKE\s+\w+\s+ON\b", "REVOKE ... ON"),
        (r"\bWAITFOR\s+DELAY\b", "WAITFOR DELAY"),
        (r"\bSLEEP\s*\(", "SLEEP("),
        (r"\bBENCHMARK\s*\(", "BENCHMARK("),
        (r"\bDECLARE\s+@", "DECLARE @"),
    ]

    # Statement separators, comments, and system-object prefixes have
    # no legitimate use in a single WHERE clause. Scanned on the
    # literal-masked copy, so quoted data never trips them.
    FORBIDDEN_SUBSTRINGS = [";", "--", "/*", "*/", "@@", "xp_", "sp_"]

    MAX_LENGTH = 2000

    @staticmethod
    def _mask_string_literals(where: str) -> str:
        """Blank out the contents of '...' literals so the injection
        scan only sees SQL code, never quoted data values (a vendor
        like 'Credit Union 1' or 'SMITH; JONES' must not trip it).

        Handles the SQL-standard '' escape for a literal quote. Raises
        on an unbalanced quote -- itself a good reason to reject the
        clause. (Same masking approach as the arcgis validator; the
        difference is what gets scanned afterwards.)
        """
        out = []
        i, n = 0, len(where)
        in_str = False
        while i < n:
            c = where[i]
            if not in_str:
                out.append(c)
                if c == "'":
                    in_str = True
                i += 1
            elif c == "'":
                if i + 1 < n and where[i + 1] == "'":
                    i += 2  # escaped '' inside the literal
                    continue
                in_str = False
                out.append(c)
                i += 1
            else:
                i += 1  # mask literal content
        if in_str:
            raise ValueError("Unbalanced quote in WHERE clause")
        return "".join(out)

    @classmethod
    def validate(cls, where: str) -> str:
        """Validate a raw WHERE clause string.

        Returns the original clause if valid ("1=1" when empty).
        Raises ValueError on statement separators/comments or on SQL
        statement shapes outside quoted string literals, or on an
        unbalanced quote.
        """
        if not where:
            return "1=1"

        where = where.strip()
        if not where:
            return "1=1"

        if len(where) > cls.MAX_LENGTH:
            raise ValueError(
                f"WHERE clause exceeds max length ({cls.MAX_LENGTH} chars)"
            )

        masked = cls._mask_string_literals(where)

        lowered = masked.lower()
        for bad in cls.FORBIDDEN_SUBSTRINGS:
            if bad in lowered:
                raise ValueError(
                    f"Forbidden substring {bad!r} detected in WHERE "
                    f"clause (outside string literals)"
                )

        for pattern, shape in cls.INJECTION_SHAPES:
            if re.search(pattern, masked, re.IGNORECASE):
                raise ValueError(
                    f"SQL statement shape '{shape}' detected in WHERE "
                    f"clause. Only filter expressions are allowed. Note "
                    f"that data VALUES containing SQL-looking words are "
                    f"fine when quoted: Vendor_Name = 'Credit Union 1' "
                    f"is a valid clause."
                )

        return where

    # Reserved SQL/Esri tokens that look like identifiers but aren't
    # field references (same set as the arcgis validator -- this part
    # of its design is sound and is copied, not imported, per the
    # one-fork-one-server doctrine).
    SQL_RESERVED = frozenset(
        {
            "AND",
            "OR",
            "NOT",
            "BETWEEN",
            "IN",
            "LIKE",
            "ESCAPE",
            "IS",
            "NULL",
            "TRUE",
            "FALSE",
            "DATE",
            "TIMESTAMP",
            "TIME",
            "CURRENT_DATE",
            "CURRENT_TIMESTAMP",
            "YEAR",
            "MONTH",
            "DAY",
            "HOUR",
            "MINUTE",
            "SECOND",
            "CASE",
            "WHEN",
            "THEN",
            "ELSE",
            "END",
            "UPPER",
            "LOWER",
            "TRIM",
            "LTRIM",
            "RTRIM",
            "LENGTH",
            "LEN",
            "SUBSTRING",
            "SUBSTR",
            "CHARINDEX",
            "POSITION",
            "COALESCE",
            "NULLIF",
            "CAST",
            "AS",
            "EXTRACT",
            "TO_DATE",
            "TO_TIMESTAMP",
            "ABS",
            "ROUND",
            "CEIL",
            "CEILING",
            "FLOOR",
            "MIN",
            "MAX",
            "SUM",
            "AVG",
            "COUNT",
            "STDDEV",
            "ANY",
            "ALL",
            "SOME",
            "DISTINCT",
        }
    )

    _STRING_LITERAL_RE = re.compile(r"'(?:[^']|'')*'")
    _NUM_LITERAL_RE = re.compile(r"\b\d+(?:\.\d+)?\b")
    _IDENT_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")

    @classmethod
    def validate_against_schema(
        cls,
        where: str,
        allowed_fields: Optional[Iterable[str]],
        schema_hint: str = "Call get_table_schema to see all field names.",
    ) -> None:
        """Verify every identifier in ``where`` is a real table field.

        Catches typo'd field names -- a hallucination magnet for LLMs
        -- before they hit ArcGIS, which would otherwise return a
        cryptic 'Unable to perform query' error. Unknown identifiers
        raise with a difflib suggestion so the caller (often a model)
        can self-correct.
        """
        if not where:
            return
        stripped_where = where.strip()
        if not stripped_where or stripped_where == "1=1":
            return
        if not allowed_fields:
            return

        allowed_set = {f for f in allowed_fields if f}
        if not allowed_set:
            return

        # Drop string literals first so values like 'Police Department'
        # don't get mis-tokenized as field names, then numeric literals.
        no_strings = cls._STRING_LITERAL_RE.sub("", where)
        no_numbers = cls._NUM_LITERAL_RE.sub("", no_strings)
        candidates = set(cls._IDENT_RE.findall(no_numbers))
        candidates = {c for c in candidates if c.upper() not in cls.SQL_RESERVED}
        unknown = sorted(c for c in candidates if c not in allowed_set)
        if not unknown:
            return

        sorted_allowed = sorted(allowed_set)
        parts = []
        for u in unknown:
            suggestions = difflib.get_close_matches(u, sorted_allowed, n=1, cutoff=0.6)
            if suggestions:
                parts.append(
                    f"Field {u!r} not found in this table -- did you "
                    f"mean {suggestions[0]!r}? (Field names are "
                    f"case-sensitive.)"
                )
            else:
                parts.append(f"Field {u!r} not found in this table.")
        parts.append(schema_hint)
        raise ValueError(" ".join(parts))


class OutFieldsValidator:
    """Validates an ArcGIS ``outFields`` parameter.

    Accepts ``*`` or a comma-separated list of bare field identifiers.
    (Copied from plugins/arcgis/where_validator.py -- identifier-only
    validation has no blocklist problem.)
    """

    _IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
    MAX_FIELDS = 100

    @classmethod
    def validate(cls, out_fields: str) -> str:
        if out_fields is None:
            return "*"
        value = out_fields.strip()
        if not value or value == "*":
            return "*"

        parts = [p.strip() for p in value.split(",")]
        if len(parts) > cls.MAX_FIELDS:
            raise ValueError(f"out_fields exceeds max of {cls.MAX_FIELDS} fields")
        for part in parts:
            if not cls._IDENT.match(part):
                raise ValueError(f"Invalid field name in out_fields: {part!r}")
        return ",".join(parts)


class OrderByValidator:
    """Validates an ArcGIS ``orderByFields`` parameter.

    Accepts one or more comma-separated ``<field>[ ASC|DESC]`` entries.
    (Copied from plugins/arcgis/where_validator.py.)
    """

    _ENTRY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\s+(ASC|DESC))?$", re.IGNORECASE)
    MAX_FIELDS = 10

    @classmethod
    def validate(cls, order_by: str) -> str:
        if not order_by:
            return ""
        value = order_by.strip()
        if not value:
            return ""

        parts = [p.strip() for p in value.split(",")]
        if len(parts) > cls.MAX_FIELDS:
            raise ValueError(f"order_by exceeds max of {cls.MAX_FIELDS} fields")
        for part in parts:
            if not cls._ENTRY.match(part):
                raise ValueError(f"Invalid order_by entry: {part!r}")
        return ",".join(parts)
