"""Anchorage Open Checkbook plugin implementation for OpenContext.

Read-only, domain-shaped access to the Municipality of Anchorage Open
Checkbook (MOA_OpenCheckbook_Hosted Feature Service on MOAGIS / ArcGIS
Online): unaudited expenditures (non-payroll and payroll), a payroll
cost rollup, procurement, and revenue. The per-table semantics -- which
fields are dollar measures, which field names the payee, which fields
are ``code : label`` strings -- are baked into the ``TABLES`` registry
so a model can ask "what did MOA spend with vendor X in FY2025?" in one
call, with no schema pre-flight.

This service has NO geometry: every table is a plain attribute table
(the service's OC_Point layer is an empty placeholder for the public
Experience Builder app and is deliberately not exposed). There are no
spatial tools here; route spatial questions to the Anchorage GIS MCP
and parcel/assessment questions to the Anchorage Parcels MCP.

Data quality is the whole point of this plugin. The upstream ETL
double-loads entire fiscal years (flagged only by the ``Duplicate``
column), stamps every row with a snapshot ``PubDate`` that is not
transaction time, and stores NET amounts that include huge offsetting
entries. The tools enforce the safe defaults (see the trap layer) so a
generic ArcGIS client's mistakes are unrepresentable here.
"""

import asyncio
import difflib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import httpx

from core.interfaces import (
    MCPPlugin,
    PluginType,
    ToolDefinition,
    ToolInputError,
    ToolResult,
)
from plugins.anchorage_checkbook.config_schema import AnchorageCheckbookPluginConfig
from plugins.anchorage_checkbook.where_validator import (
    CheckbookWhereValidator,
    OrderByValidator,
    OutFieldsValidator,
)

logger = logging.getLogger(__name__)

CASE_SENSITIVE_NOTE = "Field names are case-sensitive."

# Fiscal_Year is stored as a STRING on every table ('2023', not 2023);
# the WHERE composers quote it accordingly.
FISCAL_YEAR_FIELD = "Fiscal_Year"
DUPLICATE_FIELD = "Duplicate"
PERIOD_FIELD = "Month_Fiscal_Period"
PUBDATE_FIELD = "PubDate"

PERIOD_NOTE = (
    "Month_Fiscal_Period runs 1-16: periods 1-12 are fiscal months, "
    "13-16 are year-end ADJUSTMENT periods, not calendar months. Never "
    "map a period number to a month name."
)
LOCATION_NOTE = (
    "Location is the VENDOR'S billing city/state (e.g. 'LEESBURG, VA', "
    "often with trailing whitespace or junk values), NOT a Municipality "
    "of Anchorage location. It supports no geographic analysis."
)
NET_NOTE = (
    "Amounts are NET and include large offsetting entries (single rows "
    "range from about -$749M to +$743M); gross totals are meaningless."
)
FY2026_PARTIAL_NOTE = (
    "FY2026 is partial (loaded through fiscal period 7); never compare "
    "it to a full year without saying so."
)
REVENUE_FY2024_NOTE = (
    "Revenue (table 4) has NO FY2024 rows at all -- years present are "
    "2018-2023, 2025, 2026. A missing FY2024 is a DATA GAP, not zero "
    "revenue."
)
PROCUREMENT_FY2025_NOTE = (
    "Procurement (table 3) FY2025 totals ~$1.18B vs $310-530M in every "
    "other year -- likely a bulk encumbrance load, not a real spending "
    "spike. Treat FY2025 as an outlier in any time series."
)
VENDOR_NORMALIZATION_NOTE = (
    "Vendor names are NOT normalized upstream: one entity can appear "
    "under several spellings (e.g. 'TLO TRANSUNION', 'TRANSUNION "
    "SHAREAB', 'TransUnion Risk and Alternative Data Solutions Inc'). "
    "A total for a single spelling may undercount the entity; distinct "
    "spellings are listed separately and never silently merged."
)
REFUNDS_NOTE = (
    "'Refunds' is a real Vendor_Name value (~3.9k rows) but it is an "
    "accounting label, not a business entity."
)

# The upstream ETL double-loads whole fiscal years as exact shadow
# copies flagged Duplicate='Yes' (verified: the 'Yes' sum equals the
# 'No' sum for the affected year on every table). FY2023 is duplicated
# on ALL five populated tables; Revenue is duplicated in FY2023 AND
# FY2026 -- which is why dedup filters on the flag, never on a year.
DUPLICATE_WARNING = (
    "**WARNING -- DUPLICATES INCLUDED:** the default Duplicate='No' "
    "filter is disabled for this response. The upstream ETL "
    "double-loaded whole fiscal years as exact shadow copies flagged "
    "Duplicate='Yes' (FY2023 on every table; FY2023 AND FY2026 on "
    "Revenue). Sums over this response double-count those years."
)

# Where the 'code : label' convention applies (Fund, G_L_Account),
# this exact separator -- space, colon, space -- splits code from label.
CODE_LABEL_SEP = " : "

# Structured filter params (§3.1 preferred path): tool argument name ->
# target field, matched as a case-insensitive substring (LIKE
# '%value%', server-escaped). fiscal_year / fiscal_period /
# vendor_contains are handled separately (typed validation and the
# per-table entity field).
CONTAINS_FILTER_FIELDS: Tuple[Tuple[str, str], ...] = (
    ("business_area", "Business_Area"),
    ("fund", "Fund"),
    ("gl_account", "G_L_Account"),
    ("process_type", "Process_Type"),
)


@dataclass(frozen=True)
class TableInfo:
    """Static semantics for one Open Checkbook table.

    This registry is the checkbook analog of the parcels plugin's
    DEFAULT_FIELD_MAP: the one block a maintainer edits when the
    upstream service changes. ``all_fields`` mirrors the vendored
    schema snapshot so tools can validate field references even when
    the startup schema fetch failed.
    """

    id: int
    name: str
    label: str
    description: str
    all_fields: Tuple[str, ...]
    # Dollar-valued fields. Table 2 (payroll rollup) has FOUR and no
    # 'Amount' -- never hardcode a single amount field name.
    measure_fields: Tuple[str, ...]
    default_measure: Optional[str]
    # Fields it makes sense to group/filter by.
    dimension_fields: Tuple[str, ...]
    # Fields stored as 'code : label' strings (split in output).
    code_label_fields: Tuple[str, ...] = ()
    # The payee/counterparty field, when the table has one.
    entity_field: Optional[str] = None
    status: str = "ok"  # 'ok' | 'empty'
    caveats: Tuple[str, ...] = field(default=())


TABLES: Dict[int, TableInfo] = {
    0: TableInfo(
        id=0,
        name="OC_UnauditedExpenditure_NonPayroll",
        label="Non-payroll expenditures",
        description=(
            "Unaudited non-payroll expenditure line items -- the primary "
            "table behind the public Open Checkbook app. One row per "
            "accounting line: vendor payments, journal entries, fund "
            "transfers, refunds."
        ),
        all_fields=(
            "OBJECTID",
            "SourceFile",
            "Fund",
            "Location",
            "Duplicate",
            "Business_Area",
            "Fiscal_Year",
            "G_L_Account",
            "Month_Fiscal_Period",
            "Vendor_Name",
            "PubDate",
            "Amount",
        ),
        measure_fields=("Amount",),
        default_measure="Amount",
        dimension_fields=(
            "Fiscal_Year",
            "Month_Fiscal_Period",
            "Business_Area",
            "Fund",
            "G_L_Account",
            "Vendor_Name",
            "SourceFile",
        ),
        code_label_fields=("Fund", "G_L_Account"),
        entity_field="Vendor_Name",
        caveats=(
            "~54k rows (post-dedup) have NULL Vendor_Name -- journal "
            "entries, fund transfers, and accounting lines, not vendor "
            "payments. Vendor-facing tools exclude them.",
            "'Refunds' is a real non-vendor Vendor_Name label (~3.9k "
            "rows); it is included in vendor results but is not a "
            "business entity.",
            NET_NOTE,
            LOCATION_NOTE,
        ),
    ),
    1: TableInfo(
        id=1,
        name="OC_UnauditedExpenditure_Payroll",
        label="Payroll expenditures",
        description=(
            "Unaudited payroll expenditure line items by fund and G/L "
            "account. No vendor/payee field -- payroll is not attributed "
            "to individuals here."
        ),
        all_fields=(
            "OBJECTID",
            "SourceFile",
            "Business_Area",
            "Fiscal_Year",
            "Fund",
            "G_L_Account",
            "Month_Fiscal_Period",
            "Duplicate",
            "PubDate",
            "Amount",
        ),
        measure_fields=("Amount",),
        default_measure="Amount",
        dimension_fields=(
            "Fiscal_Year",
            "Month_Fiscal_Period",
            "Business_Area",
            "Fund",
            "G_L_Account",
            "SourceFile",
        ),
        code_label_fields=("Fund", "G_L_Account"),
        entity_field=None,
        caveats=(
            "Has no Vendor_Name or any payee field.",
            NET_NOTE,
        ),
    ),
    2: TableInfo(
        id=2,
        name="OC_UnauditedPayroll",
        label="Payroll cost rollup",
        description=(
            "Payroll costs pre-aggregated to one row per department "
            "(Business_Area) per fiscal year (~150 rows). Its dollar "
            "measures are Total_Payroll_Cost, Salaries_Wages, Overtime, "
            "and Liabilities_Benefits -- there is NO 'Amount' field."
        ),
        all_fields=(
            "OBJECTID",
            "SourceFile",
            "Business_Area",
            "Fiscal_Year",
            "Duplicate",
            "PubDate",
            "Liabilities_Benefits",
            "Overtime",
            "Salaries_Wages",
            "Total_Payroll_Cost",
        ),
        measure_fields=(
            "Total_Payroll_Cost",
            "Salaries_Wages",
            "Overtime",
            "Liabilities_Benefits",
        ),
        default_measure="Total_Payroll_Cost",
        dimension_fields=("Fiscal_Year", "Business_Area", "SourceFile"),
        code_label_fields=(),
        entity_field=None,
        caveats=(
            "Already a department x fiscal-year rollup -- do not sum it "
            "on top of table 1 (payroll expenditures) or you will "
            "double-count payroll.",
            "No Amount field: use Total_Payroll_Cost (or Salaries_Wages "
            "/ Overtime / Liabilities_Benefits).",
            "No Month_Fiscal_Period field (annual granularity only).",
        ),
    ),
    3: TableInfo(
        id=3,
        name="OC_UnauditedProcurement",
        label="Procurement",
        description=(
            "Unaudited procurement records: purchase orders with vendor, "
            "PO number/description, and process type."
        ),
        all_fields=(
            "OBJECTID",
            "SourceFile",
            "Location",
            "Duplicate",
            "Business_Area",
            "Fiscal_Year",
            "Month_Fiscal_Period",
            "PO_Description",
            "Process_Type",
            "Purchase_Order",
            "Vendor_Name",
            "PubDate",
            "Amount",
        ),
        measure_fields=("Amount",),
        default_measure="Amount",
        dimension_fields=(
            "Fiscal_Year",
            "Month_Fiscal_Period",
            "Business_Area",
            "Process_Type",
            "Vendor_Name",
            "Purchase_Order",
            "SourceFile",
        ),
        code_label_fields=(),
        entity_field="Vendor_Name",
        caveats=(
            "FY2025 totals ~$1.18B vs $310-530M in every other year -- "
            "likely a bulk encumbrance load. Flag it in any Procurement "
            "time series.",
            NET_NOTE,
            LOCATION_NOTE,
        ),
    ),
    4: TableInfo(
        id=4,
        name="OC_UnauditedRevenue",
        label="Revenue",
        description=(
            "Unaudited revenue line items by fund and G/L account. The "
            "payer field is Customer_Business_Name (not Vendor_Name)."
        ),
        all_fields=(
            "OBJECTID",
            "SourceFile",
            "Business_Area",
            "Customer_Business_Name",
            "Fiscal_Year",
            "Fund",
            "G_L_Account",
            "Month_Fiscal_Period",
            "Duplicate",
            "PubDate",
            "Amount",
        ),
        measure_fields=("Amount",),
        default_measure="Amount",
        dimension_fields=(
            "Fiscal_Year",
            "Month_Fiscal_Period",
            "Business_Area",
            "Fund",
            "G_L_Account",
            "Customer_Business_Name",
            "SourceFile",
        ),
        code_label_fields=("Fund", "G_L_Account"),
        entity_field="Customer_Business_Name",
        caveats=(
            "Revenue has NO FY2024 rows at all. Years present: "
            "2018-2023, 2025, 2026. A FY2024 gap in results is a data "
            "gap, not zero revenue.",
            "Duplicate rows exist in FY2023 AND FY2026 (the double-load "
            "is not a one-year event).",
            NET_NOTE,
        ),
    ),
    5: TableInfo(
        id=5,
        name="OC_UnaudRev_vs_Exp",
        label="Revenue vs expenditure",
        description=(
            "Intended revenue-vs-expenditure comparison by fund and "
            "fiscal year. Currently EMPTY (0 rows) upstream."
        ),
        all_fields=(
            "OBJECTID",
            "SourceFile",
            "Fiscal_Year",
            "Fund",
            "Duplicate",
            "PubDate",
            "Difference",
            "Expenditure",
            "Revenue",
        ),
        measure_fields=("Difference", "Expenditure", "Revenue"),
        default_measure="Difference",
        dimension_fields=("Fiscal_Year", "Fund", "SourceFile"),
        code_label_fields=("Fund",),
        entity_field=None,
        status="empty",
        caveats=("Empty upstream: every query returns 0 rows.",),
    ),
    # Table 6 (OC_Point) is a 0-row geometry placeholder that exists
    # only so the public Experience Builder app can mount a map widget.
    # It is intentionally absent from this registry and never exposed.
}

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class AnchorageCheckbookPlugin(MCPPlugin):
    """Plugin for the Municipality of Anchorage Open Checkbook.

    Wraps the public MOA_OpenCheckbook_Hosted Feature Service (six
    attribute tables: non-payroll and payroll expenditures, payroll
    rollup, procurement, revenue, and an empty revenue-vs-expenditure
    table), exposing read-only financial tools with the per-table
    semantics baked in.
    """

    plugin_name = "anchorage_checkbook"
    plugin_type = PluginType.OPEN_DATA
    plugin_version = "1.0.0"

    # Retry policy for transient ArcGIS failures (same policy as the
    # parcels plugin, copied per the one-fork-one-server doctrine).
    ARCGIS_MAX_ATTEMPTS = 3
    ARCGIS_RETRY_BACKOFF_S = 0.5

    # Server maxRecordCount on this service (verified). A single query
    # page can never exceed this; row-returning tools page with
    # resultOffset when the caller's limit is larger.
    SERVER_PAGE_SIZE = 2000
    # Hard cap on raw rows in one tool response (§4.6).
    MAX_ROWS = 500
    # Hard cap on groups in one spending_stats response.
    MAX_GROUPS = 200
    # get_table_schema reports distinct-value counts up to this many,
    # then '>N' (Vendor_Name has tens of thousands of spellings).
    CARDINALITY_CAP = 1000

    SCHEMA_SNAPSHOT_PATH = Path(__file__).parent / "schema" / "checkbook_tables.json"

    def __init__(self, config: Dict[str, Any]) -> None:
        super().__init__(config)
        self.plugin_config: Optional[AnchorageCheckbookPluginConfig] = None
        self.client: Optional[httpx.AsyncClient] = None
        # Live per-table metadata captured at initialize(); empty when
        # the startup fetch failed (plugin still starts -- degraded >
        # down). Field validation then falls back to the registry.
        self._live_fields: Dict[int, set] = {}
        self._date_fields: Dict[int, set] = {}
        # ETL snapshot date per table (trap 2.3: PubDate is provenance,
        # not transaction time). Filled lazily, one query per table per
        # process lifetime.
        self._pubdate_cache: Dict[int, Optional[str]] = {}
        # Field types from the vendored schema snapshot, loaded lazily
        # for get_table_schema.
        self._snapshot_types: Optional[Dict[int, Dict[str, str]]] = None

    # ── Lifecycle ─────────────────────────────────────────────────────

    async def initialize(self) -> bool:
        try:
            self.plugin_config = AnchorageCheckbookPluginConfig(**self.config)
        except Exception as e:
            # Misconfiguration is fatal -- fail fast so the deploy is
            # fixed rather than serving broken tools.
            logger.error(
                f"Failed to validate anchorage_checkbook config: {e}",
                exc_info=True,
            )
            return False

        self.client = httpx.AsyncClient(timeout=self.plugin_config.timeout)

        # Reachability + schema-drift check across all six tables.
        # Failures are logged loudly but do NOT block startup: a
        # transient blip at cold start must not take the whole server
        # down (degraded > down).
        try:
            metas = await asyncio.gather(
                *(
                    self._request_json(self._table_url(tid), {"f": "json"})
                    for tid in TABLES
                )
            )
            for tid, meta in zip(TABLES, metas):
                self._capture_table_meta(tid, meta)
            self._check_schema_drift()
        except Exception as e:
            logger.warning(
                "anchorage_checkbook: service unreachable at startup; "
                "starting anyway (queries will retry per call)",
                extra={"error": str(e)},
            )

        self._initialized = True
        logger.info(
            f"Anchorage Checkbook plugin initialized for {self.plugin_config.city_name}"
        )
        return True

    def _capture_table_meta(self, table_id: int, meta: Dict[str, Any]) -> None:
        fields = meta.get("fields") or []
        self._live_fields[table_id] = {f.get("name") for f in fields if f.get("name")}
        self._date_fields[table_id] = {
            f.get("name")
            for f in fields
            if f.get("type") == "esriFieldTypeDate" and f.get("name")
        }

    def _check_schema_drift(self) -> None:
        """Diff live field names against the registry (which mirrors the
        vendored schema snapshot). On drift, log a loud structured
        warning naming the diverged tables -- but keep serving
        (degraded > down)."""
        for tid, info in TABLES.items():
            live = self._live_fields.get(tid)
            if not live:
                continue
            expected = set(info.all_fields)
            missing = sorted(expected - live)
            added = sorted(live - expected)
            if missing or added:
                logger.warning(
                    "anchorage_checkbook: SCHEMA DRIFT DETECTED on table "
                    "%s (%s) -- live fields differ from the registry. "
                    "Tools referencing missing fields will fail; refresh "
                    "schema/checkbook_tables.json and review TABLES.",
                    tid,
                    info.name,
                    extra={"missing_fields": missing, "added_fields": added},
                )

    async def shutdown(self) -> None:
        if self.client:
            await self.client.aclose()
            self.client = None
        self._initialized = False
        logger.info("Anchorage Checkbook plugin shut down")

    async def health_check(self) -> bool:
        try:
            resp = await self.client.get(self._table_url(0), params={"f": "json"})
            return resp.status_code == 200
        except Exception as e:
            logger.error(f"Health check failed: {e}")
            return False

    # ── Table helpers ─────────────────────────────────────────────────

    def _table_url(self, table_id: int) -> str:
        return f"{self.plugin_config.service_url}/{table_id}"

    @staticmethod
    def _table_info(table_id: Any) -> TableInfo:
        """Resolve and validate a caller-supplied table id."""
        try:
            tid = int(table_id)
        except (TypeError, ValueError):
            tid = -1
        info = TABLES.get(tid)
        if info is None:
            options = "; ".join(f"{i.id}={i.name} ({i.label})" for i in TABLES.values())
            raise ToolInputError(
                f"table must be one of 0-5 (got {table_id!r}). "
                f"Tables: {options}. Call list_tables for details."
            )
        return info

    def _fields_for(self, table_id: int) -> set:
        """Known field names for a table: live schema when the startup
        fetch succeeded, registry fallback otherwise."""
        return self._live_fields.get(table_id) or set(TABLES[table_id].all_fields)

    # ── Trap layer ────────────────────────────────────────────────────
    # Each helper below encodes one verified data trap (work order §2).
    # Tools compose these instead of re-deciding the semantics locally,
    # so the safe behavior cannot be skipped by accident.

    def _dedup_parts(self, args: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
        """Trap 2.1 -- the Duplicate double-load.

        Returns (where_clause, warning): the ``Duplicate='No'`` clause
        injected into EVERY query by default, or (None, warning) when
        the caller explicitly set include_duplicates=True. The filter
        is on the flag, never on a fiscal year -- Revenue proves the
        double-load recurs (FY2023 AND FY2026).
        """
        if bool(args.get("include_duplicates", False)):
            return None, DUPLICATE_WARNING
        return f"{DUPLICATE_FIELD} = 'No'", None

    @staticmethod
    def _reject_pubdate_filter(clause: Optional[str]) -> None:
        """Trap 2.3 -- PubDate is a snapshot stamp, not transaction time.

        Rejects any filter that references PubDate. It carries a single
        value across all rows (when the ETL last ran), so a date filter
        on it either matches everything or nothing.
        """
        if clause and re.search(rf"\b{PUBDATE_FIELD}\b", clause, re.IGNORECASE):
            raise ToolInputError(
                f"Filtering on {PUBDATE_FIELD} is not supported: it is "
                f"the ETL snapshot stamp -- one identical value on every "
                f"row saying when the data was last published, NOT when "
                f"a transaction happened. Filter time with "
                f"{FISCAL_YEAR_FIELD} plus {PERIOD_FIELD} instead. "
                f"{PERIOD_NOTE} The snapshot date is reported in every "
                f"response's provenance line."
            )

    async def _pubdate_snapshot(self, table_id: int) -> Optional[str]:
        """ETL snapshot date for one table, cached per process.

        Surfaced once per response as provenance (trap 2.3); never
        exposed as a filterable axis. None for empty tables or when the
        probe fails (provenance then omits the date -- degraded > down).
        """
        if table_id not in self._pubdate_cache:
            value = None
            try:
                records, _ = await self._query_table(table_id, "1=1", PUBDATE_FIELD, 1)
                if records:
                    value = self._ms_to_iso_smart(records[0].get(PUBDATE_FIELD))
            except Exception as e:
                logger.warning(f"PubDate probe failed for table {table_id}: {e}")
            self._pubdate_cache[table_id] = value
        return self._pubdate_cache[table_id]

    @staticmethod
    def _split_code_label(value: Any) -> Tuple[Optional[str], Any]:
        """Trap 2.7 -- coded fields are 'code : label' strings.

        '141000 : Anchorage Roads & Drainage SA' -> ('141000',
        'Anchorage Roads & Drainage SA'). Values without the separator
        (e.g. plain Business_Area labels) return (None, value).
        """
        if isinstance(value, str) and CODE_LABEL_SEP in value:
            code, label = value.split(CODE_LABEL_SEP, 1)
            return code.strip(), label.strip()
        return None, value

    def _expand_code_labels(
        self, records: List[Dict[str, Any]], info: TableInfo
    ) -> List[Dict[str, Any]]:
        """Replace each 'code : label' field with <field>_code and
        <field>_label keys, preserving column order."""
        if not info.code_label_fields:
            return records
        out: List[Dict[str, Any]] = []
        for record in records:
            expanded: Dict[str, Any] = {}
            for key, value in record.items():
                if key in info.code_label_fields:
                    code, label = self._split_code_label(value)
                    expanded[f"{key}_code"] = code
                    expanded[f"{key}_label"] = label
                else:
                    expanded[key] = value
            out.append(expanded)
        return out

    @staticmethod
    def _fiscal_notices(info: TableInfo, years: Optional[Iterable[Any]]) -> List[str]:
        """Trap 2.8 -- known completeness gaps.

        Notices for the fiscal years a query touches. ``years`` is the
        set of years the query was filtered to; None means unfiltered
        (the query spans all years, so every applicable gap is noted).
        """
        touched = None if years is None else {str(y) for y in years if y is not None}

        def touches(year: str) -> bool:
            return touched is None or year in touched

        notes: List[str] = []
        # Multi-year revenue results silently skip FY2024; a query
        # pinned to one other year doesn't need the warning.
        if info.id == 4 and (touched is None or "2024" in touched or len(touched) > 1):
            notes.append(REVENUE_FY2024_NOTE)
        if info.id == 3 and touches("2025"):
            notes.append(PROCUREMENT_FY2025_NOTE)
        if touches("2026"):
            notes.append(FY2026_PARTIAL_NOTE)
        return notes

    @staticmethod
    def _require_entity_field(info: TableInfo) -> str:
        """Resolve the payee field via the registry (trap 2.4 + §4.4:
        table 4 uses Customer_Business_Name -- never special-cased in
        tool bodies)."""
        if not info.entity_field:
            with_entity = "; ".join(
                f"table {i.id} ({i.label}) uses {i.entity_field}"
                for i in TABLES.values()
                if i.entity_field
            )
            raise ToolInputError(
                f"Table {info.id} ({info.name}) has no vendor/payee "
                f"field. Tables with one: {with_entity}."
            )
        return info.entity_field

    def _entity_not_null_clause(self, info: TableInfo) -> str:
        """Trap 2.4 -- Vendor_Name NULLs.

        ~54k post-dedup rows in table 0 are true-NULL journal entries,
        fund transfers, and accounting lines. Every vendor-facing query
        excludes them explicitly.
        """
        return f"{self._require_entity_field(info)} IS NOT NULL"

    @staticmethod
    def _int_arg(args: Dict[str, Any], name: str, default: Any = None) -> int:
        """Coerce a caller-supplied argument to int, or explain why not.

        A bare int() raises "invalid literal for int() with base 10:
        'FY25'" -- which reads as a server fault in the logs and tells the
        caller nothing about which argument was wrong. An explicit null is
        treated as "not supplied", the same as omitting the key.
        """
        raw = args.get(name, default)
        if raw is None:
            raw = default
        try:
            return int(raw)
        except (TypeError, ValueError) as exc:
            raise ToolInputError(
                f"{name} must be an integer (got {raw!r})"
            ) from exc

    @staticmethod
    def _float_arg(args: Dict[str, Any], name: str, default: Any = None) -> float:
        """Coerce a caller-supplied argument to float, or explain why not."""
        raw = args.get(name, default)
        if raw is None:
            raw = default
        try:
            return float(raw)
        except (TypeError, ValueError) as exc:
            raise ToolInputError(
                f"{name} must be a number (got {raw!r})"
            ) from exc

    @staticmethod
    def _validate_fiscal_year(value: Any) -> str:
        """Fiscal_Year is a STRING field upstream; accept int or str
        input and return the quoted-literal-ready 4-digit string."""
        text = str(value).strip()
        if not re.fullmatch(r"\d{4}", text):
            raise ToolInputError(
                f"fiscal_year must be a 4-digit year (got {value!r}), "
                f"e.g. 2023. Years present vary by table -- call "
                f"list_tables for per-table coverage."
            )
        return text

    @staticmethod
    def _validate_period(value: Any) -> int:
        """Trap 2.2 -- Month_Fiscal_Period runs 1-16, not 1-12."""
        try:
            period = int(value)
        except (TypeError, ValueError):
            raise ToolInputError(
                f"fiscal_period must be an integer 1-16 (got {value!r}). {PERIOD_NOTE}"
            )
        if not 1 <= period <= 16:
            raise ToolInputError(
                f"fiscal_period must be 1-16 (got {period}). {PERIOD_NOTE}"
            )
        return period

    # ── WHERE composition (§3.1: structured params, not raw SQL) ──────
    # The main tools take structured filters and the SERVER composes
    # the WHERE clause with proper escaping (doubled single quotes);
    # raw user SQL never enters those paths. The query_checkbook escape
    # hatch runs through CheckbookWhereValidator, which targets
    # injection SHAPES (UNION ... SELECT) rather than bare tokens, so
    # vendors like 'IBEW Local Union 1547' stay queryable.

    @staticmethod
    def _sql_quote(value: str) -> str:
        return "'" + str(value).replace("'", "''") + "'"

    @staticmethod
    def _escape_like(value: str) -> str:
        return str(value).replace("'", "''")

    def _contains_clause(self, field_name: str, value: Any) -> str:
        """Case-insensitive substring match. LIKE is case-insensitive
        on this backend (verified: '%chugach%' and the UPPER-wrapped
        variant both return 2,401 rows) -- never wrap in UPPER()."""
        return f"{field_name} LIKE '%{self._escape_like(value)}%'"

    @staticmethod
    def _combine_where(*clauses: Optional[str]) -> str:
        parts = [c for c in clauses if c and c.strip() and c.strip() != "1=1"]
        if not parts:
            return "1=1"
        if len(parts) == 1:
            return parts[0]
        return " AND ".join(f"({p})" for p in parts)

    def _check_field_on_table(
        self, info: TableInfo, field_name: str, arg_name: str
    ) -> None:
        """Reject a structured filter that names a field this table
        lacks (e.g. fund on the payroll rollup)."""
        if field_name not in self._fields_for(info.id):
            raise ToolInputError(
                f"{arg_name} does not apply to table {info.id} "
                f"({info.name}): it has no {field_name} field. Fields "
                f"on this table: {', '.join(info.all_fields)}."
            )

    def _check_field_exists(
        self, info: TableInfo, field_name: str, arg_name: str
    ) -> str:
        """Validate a caller-supplied field name against one table's
        schema, with a difflib suggestion on a miss."""
        field_name = (field_name or "").strip()
        if not _IDENT_RE.match(field_name):
            raise ToolInputError(
                f"{arg_name} must be a single field name "
                f"(got {field_name!r}). {CASE_SENSITIVE_NOTE}"
            )
        known = self._fields_for(info.id)
        if field_name not in known:
            suggestion = difflib.get_close_matches(
                field_name, sorted(known), n=1, cutoff=0.6
            )
            hint = f" Did you mean {suggestion[0]!r}?" if suggestion else ""
            raise ToolInputError(
                f"{arg_name} {field_name!r} is not a field on table "
                f"{info.id} ({info.name}).{hint} Fields: "
                f"{', '.join(sorted(known))}. {CASE_SENSITIVE_NOTE}"
            )
        return field_name

    def _structured_where(
        self, info: TableInfo, args: Dict[str, Any]
    ) -> Tuple[List[str], Optional[set]]:
        """Compose WHERE clauses from structured filter params.

        Returns (clauses, years): years is the fiscal-year set the
        filters pin the query to, or None when unfiltered (spans all
        years) -- feed it to _fiscal_notices. A vendor_contains filter
        also injects the entity IS NOT NULL clause (trap 2.4).
        """
        clauses: List[str] = []
        years: Optional[set] = None
        if args.get("fiscal_year") is not None:
            year = self._validate_fiscal_year(args["fiscal_year"])
            clauses.append(f"{FISCAL_YEAR_FIELD} = '{year}'")
            years = {year}
        if args.get("fiscal_period") is not None:
            self._check_field_on_table(info, PERIOD_FIELD, "fiscal_period")
            period = self._validate_period(args["fiscal_period"])
            clauses.append(f"{PERIOD_FIELD} = {period}")
        for arg_name, field_name in CONTAINS_FILTER_FIELDS:
            value = args.get(arg_name)
            if value is None or not str(value).strip():
                continue
            self._check_field_on_table(info, field_name, arg_name)
            clauses.append(self._contains_clause(field_name, str(value).strip()))
        vendor = args.get("vendor_contains")
        if vendor is not None and str(vendor).strip():
            entity = self._require_entity_field(info)
            clauses.append(self._contains_clause(entity, str(vendor).strip()))
            clauses.append(f"{entity} IS NOT NULL")
        return clauses, years

    def _default_out_fields(
        self, info: TableInfo, include_duplicates: bool = False
    ) -> str:
        """Every substantive field. OBJECTID (row id) and PubDate (the
        constant snapshot stamp, surfaced in provenance instead) are
        noise; Duplicate is constant 'No' unless duplicates were
        requested."""
        skip = {"OBJECTID", PUBDATE_FIELD}
        if not include_duplicates:
            skip.add(DUPLICATE_FIELD)
        return ",".join(f for f in info.all_fields if f not in skip)

    def _validate_out_fields(
        self,
        info: TableInfo,
        out_fields: Optional[str],
        include_duplicates: bool = False,
    ) -> str:
        if not out_fields or not str(out_fields).strip():
            return self._default_out_fields(info, include_duplicates)
        validated = OutFieldsValidator.validate(str(out_fields))
        if validated == "*":
            return validated
        for field_name in validated.split(","):
            self._check_field_exists(info, field_name, "out_fields")
        return validated

    def _validate_order_by(self, info: TableInfo, order_by: Any) -> str:
        validated = OrderByValidator.validate(str(order_by or ""))
        if validated:
            for entry in validated.split(","):
                self._check_field_exists(info, entry.split()[0], "order_by")
        return validated

    def _validate_raw_where(self, info: TableInfo, raw_where: str) -> str:
        """Full validation pipeline for the query_checkbook escape
        hatch: injection-shape scan (word-boundary regexes, so 'Credit
        Union 1' is legal data), PubDate-filter rejection (trap 2.3),
        and per-table schema check with typo suggestions."""
        where = CheckbookWhereValidator.validate(raw_where)
        self._reject_pubdate_filter(where)
        CheckbookWhereValidator.validate_against_schema(
            where,
            self._fields_for(info.id),
            schema_hint=(
                f"Call get_table_schema(table={info.id}) to see all field names."
            ),
        )
        return where

    # ── HTTP / query plumbing ─────────────────────────────────────────

    @staticmethod
    def _arcgis_error_text(err: Any) -> str:
        """Non-empty message from an ArcGIS error object."""
        if not isinstance(err, dict):
            return str(err) or "unknown ArcGIS error"
        parts: List[str] = []
        code = err.get("code")
        if code is not None:
            parts.append(f"code {code}")
        msg = (err.get("message") or "").strip()
        if msg:
            parts.append(msg)
        details = [str(d) for d in (err.get("details") or []) if d]
        if details:
            parts.append("; ".join(details))
        return " -- ".join(parts) or (
            "ArcGIS returned an error with no message (usually a transient "
            "server blip -- retry the request)"
        )

    @classmethod
    def _is_transient_arcgis_error(cls, err: Any) -> bool:
        """5xx-class codes and empty-message errors are transient."""
        if not isinstance(err, dict):
            return False
        if err.get("code") in (500, 502, 503, 504):
            return True
        return not (err.get("message") or "").strip()

    async def _request_json(self, url: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """GET a service endpoint, retrying transient upstream failures.

        Copied from the parcels plugin's `_layer_query` (which itself
        ports anchorage_gis `_request_json_with_retry`): retries httpx
        transport/timeout errors, HTTP 5xx, and transient ArcGIS error
        bodies; raises immediately (with a rewritten, actionable
        message) on real errors.
        """
        last_desc = "unknown error"
        attempt = 0
        while attempt < self.ARCGIS_MAX_ATTEMPTS:
            attempt += 1
            transient = False
            try:
                resp = await self.client.get(url, params=params)
            except (httpx.TransportError, httpx.TimeoutException) as e:
                transient, last_desc = True, f"network error: {e!r}"
            else:
                status = resp.status_code
                if status >= 500:
                    transient, last_desc = True, f"upstream HTTP {status}"
                elif status >= 400:
                    raise RuntimeError(
                        f"Feature Service error (HTTP {status}): {resp.text[:200]}"
                    )
                else:
                    try:
                        payload = resp.json()
                    except Exception as e:
                        raise ValueError(
                            "Feature Service returned non-JSON "
                            f"(content-type "
                            f"{resp.headers.get('content-type', '?')})"
                        ) from e
                    err = payload.get("error") if isinstance(payload, dict) else None
                    if not err:
                        return payload
                    if self._is_transient_arcgis_error(err):
                        transient = True
                        last_desc = self._arcgis_error_text(err)
                    else:
                        raise RuntimeError(self._arcgis_error_text(err))
            if not transient or attempt >= self.ARCGIS_MAX_ATTEMPTS:
                break
            await asyncio.sleep(self.ARCGIS_RETRY_BACKOFF_S * attempt)
        raise RuntimeError(
            f"Feature Service request failed after {attempt} attempt(s): {last_desc}"
        )

    async def _fetch_count(self, table_id: int, where: str) -> Optional[int]:
        """Total records matching a WHERE clause (returnCountOnly)."""
        try:
            data = await self._request_json(
                f"{self._table_url(table_id)}/query",
                {"f": "json", "where": where, "returnCountOnly": "true"},
            )
            return data.get("count")
        except Exception as e:
            logger.warning(
                f"count query failed for table {table_id} where={where!r}: {e}"
            )
            return None

    async def _query_table(
        self,
        table_id: int,
        where: str,
        out_fields: str,
        limit: int,
        offset: int = 0,
        order_by: Optional[str] = None,
        extra_params: Optional[Dict[str, Any]] = None,
    ) -> Tuple[List[Dict[str, Any]], bool]:
        """Fetch up to `limit` attribute rows from one table, paging
        past the server's maxRecordCount with resultOffset.

        Returns (records, exceeded_transfer_limit) where the flag is
        True when the server reported more rows remain past the last
        page fetched.
        """
        records: List[Dict[str, Any]] = []
        exceeded = False
        while len(records) < limit:
            want = min(self.SERVER_PAGE_SIZE, limit - len(records))
            params: Dict[str, Any] = {
                "f": "json",
                "where": where,
                "outFields": out_fields,
                "returnGeometry": "false",
                "resultRecordCount": str(want),
                "resultOffset": str(offset + len(records)),
            }
            if order_by:
                params["orderByFields"] = order_by
            if extra_params:
                params.update(extra_params)
            data = await self._request_json(
                f"{self._table_url(table_id)}/query", params
            )
            feats = data.get("features") or []
            records.extend(f.get("attributes") or {} for f in feats)
            exceeded = bool(data.get("exceededTransferLimit"))
            if not feats or (len(feats) < want and not exceeded):
                break
        return records[:limit], exceeded

    async def _query_statistics(
        self,
        table_id: int,
        where: str,
        out_statistics: List[Dict[str, Any]],
        group_by: Optional[str] = None,
        order_by: Optional[str] = None,
        extra_params: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """Server-side outStatistics query; returns attribute rows."""
        params: Dict[str, Any] = {
            "f": "json",
            "where": where,
            "outStatistics": json.dumps(out_statistics, separators=(",", ":")),
            "returnGeometry": "false",
        }
        if group_by:
            params["groupByFieldsForStatistics"] = group_by
        if order_by:
            params["orderByFields"] = order_by
        if extra_params:
            params.update(extra_params)
        data = await self._request_json(f"{self._table_url(table_id)}/query", params)
        return [f.get("attributes") or {} for f in data.get("features") or []]

    async def _fetch_distinct_values(
        self,
        table_id: int,
        fieldname: str,
        where: str = "1=1",
        limit: int = 2000,
    ) -> List[Any]:
        """Distinct values of one field via returnDistinctValues.

        NOTE: this service ADVERTISES supportsCountDistinct but
        count_distinct ERRORS in practice (verified live). Distinct
        values are therefore always fetched this way and counted
        client-side -- never with a count_distinct statistic.
        """
        records, _ = await self._query_table(
            table_id,
            where,
            fieldname,
            limit,
            order_by=fieldname,
            extra_params={"returnDistinctValues": "true"},
        )
        return [r.get(fieldname) for r in records]

    # ── Formatting helpers ────────────────────────────────────────────

    @staticmethod
    def _with_retrieved_footer(text: str) -> str:
        # Stamp every tool response with a UTC retrieval timestamp so
        # models can tell stale outputs from fresh ones. Skip when a
        # provenance header already carries a Retrieved: line.
        if not text or "Retrieved:" in text:
            return text
        retrieved_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return f"{text}\n\n_Retrieved: {retrieved_at}_"

    @staticmethod
    def _ms_to_iso_smart(ms: Any) -> Any:
        # Midnight UTC -> date-only; non-midnight -> full ISO; on
        # failure return the raw value (losing data is worse than
        # showing epoch ms).
        if ms is None or ms == "":
            return ms
        try:
            dt = datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)
        except (ValueError, TypeError, OSError):
            return ms
        if dt.hour == dt.minute == dt.second == dt.microsecond == 0:
            return dt.strftime("%Y-%m-%d")
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    @staticmethod
    def _fmt_money(value: Any) -> str:
        if value is None or value == "":
            return "--"
        try:
            f = float(value)
        except (TypeError, ValueError):
            return str(value)
        if f < 0:
            return f"-${abs(f):,.2f}"
        return f"${f:,.2f}"

    def _clamp_limit(
        self, args: Dict[str, Any], default: int, maximum: int
    ) -> Tuple[int, int]:
        """Clamp the limit argument to [1, maximum].

        §3.2: clamping is never silent. Callers pass both values to the
        provenance footer, which echoes 'limit=N (requested M,
        clamped)' -- the eBird pattern.
        """
        requested = self._int_arg(args, "limit", default)
        return max(1, min(requested, maximum)), requested

    @staticmethod
    def _limit_echo(limit: int, requested: Optional[int]) -> str:
        if requested is not None and requested != limit:
            return f"limit={limit} (requested {requested}, clamped)"
        return f"limit={limit}"

    def _provenance_footer(
        self,
        info: TableInfo,
        *,
        where: Optional[str] = None,
        limit: Optional[int] = None,
        requested: Optional[int] = None,
        row_count: Optional[int] = None,
        total_count: Optional[int] = None,
        pubdate: Optional[str] = None,
        extra: Optional[List[str]] = None,
    ) -> str:
        """The provenance block ending every response (§4): service
        URL + table id, the EFFECTIVE where (including the injected
        Duplicate clause), row counts, clamp status, and the PubDate
        snapshot date (trap 2.3: surfaced once, as provenance)."""
        lines = [
            "---",
            f"Source: {self._table_url(info.id)} (table {info.id}: {info.name})",
        ]
        query_parts = []
        if where is not None:
            query_parts.append(f"where={where!r}")
        if limit is not None:
            query_parts.append(self._limit_echo(limit, requested))
        if query_parts:
            lines.append(f"Query: {', '.join(query_parts)}")
        if row_count is not None:
            row_line = f"Rows returned: {row_count:,}"
            if total_count is not None:
                row_line += f" of {total_count:,} matching"
            lines.append(row_line)
        if extra:
            lines.extend(extra)
        retrieved_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        lines.append(
            f"Data snapshot (PubDate): {pubdate or 'unavailable'} | "
            f"Retrieved: {retrieved_at}"
        )
        return "\n".join(lines)

    @staticmethod
    def _truncation_banner(shown: int, total: int, limit: int) -> str:
        return (
            f"**TRUNCATED:** showing {shown:,} of {total:,} matching "
            f"rows (limit={limit}). The rows shown are a SAMPLE -- do "
            f"not sum or count them as if complete. The true "
            f"matching-row count is {total:,}; narrow the filters or "
            f"page with offset for the rest."
        )

    @staticmethod
    def _table_cell(value: Any) -> str:
        if value is None:
            return ""
        return str(value).replace("|", "\\|")

    def _render_value(self, table_id: int, key: str, value: Any) -> Any:
        date_fields = self._date_fields.get(table_id) or {PUBDATE_FIELD}
        if key in date_fields:
            return self._ms_to_iso_smart(value)
        return value

    def _format_table(self, records: List[Dict[str, Any]], table_id: int) -> List[str]:
        """§3.3: compact pipe-delimited table -- one header line, one
        row per record -- replacing the parcels 'Record N:' blocks
        (~30 bytes/record of pure formatting on 12 short fields)."""
        columns: List[str] = list(records[0].keys())
        seen = set(columns)
        for record in records[1:]:
            for key in record:
                if key not in seen:
                    seen.add(key)
                    columns.append(key)
        lines = [" | ".join(columns)]
        for record in records:
            lines.append(
                " | ".join(
                    self._table_cell(self._render_value(table_id, k, record.get(k)))
                    for k in columns
                )
            )
        return lines

    def _format_rows_response(
        self,
        info: TableInfo,
        records: List[Dict[str, Any]],
        *,
        where: str,
        limit: int,
        requested: Optional[int] = None,
        total_count: Optional[int] = None,
        heading: Optional[str] = None,
        notices: Optional[List[str]] = None,
        pubdate: Optional[str] = None,
    ) -> str:
        """Assemble a row-listing response.

        Layout: heading, §3.4 BOOKENDED truncation banner (top AND
        bottom, quoting the true total from returnCountOnly), trap
        notices, count line, §3.3 compact table, provenance footer.
        Records should already be code-label-expanded by the caller.
        """
        lines: List[str] = []
        if heading:
            lines += [heading, ""]
        truncated = (
            bool(records) and total_count is not None and total_count > len(records)
        )
        banner = (
            self._truncation_banner(len(records), total_count, limit)
            if truncated
            else None
        )
        if banner:
            lines.append(banner)
        if notices:
            lines.extend(notices)
        if banner or notices:
            lines.append("")
        if not records:
            lines.append("No rows matched.")
        else:
            if total_count is not None:
                lines.append(
                    f"Returned {len(records):,} row(s); TOTAL rows "
                    f"matching the filter: {total_count:,} -- use that "
                    f"figure for 'how many?' questions, not a count of "
                    f"the rows below."
                )
            else:
                lines.append(f"Returned {len(records):,} row(s).")
            lines.append("")
            lines.extend(self._format_table(records, info.id))
        if banner:
            lines += ["", banner]
        lines += [
            "",
            self._provenance_footer(
                info,
                where=where,
                limit=limit,
                requested=requested,
                row_count=len(records),
                total_count=total_count,
                pubdate=pubdate,
            ),
        ]
        return "\n".join(lines)

    # ── Tool: list_tables ─────────────────────────────────────────────

    async def _list_tables(self, args: Dict[str, Any]) -> str:
        infos = list(TABLES.values())
        counts = await asyncio.gather(
            *(self._fetch_count(i.id, "1=1") for i in infos),
            *(self._fetch_count(i.id, f"{DUPLICATE_FIELD} = 'No'") for i in infos),
        )
        totals = counts[: len(infos)]
        dedups = counts[len(infos) :]
        pubdate = await self._pubdate_snapshot(0)

        def fmt_count(n: Optional[int]) -> str:
            return f"{n:,}" if n is not None else "n/a"

        lines = [
            f"# Open Checkbook tables ({self.plugin_config.city_name})",
            "",
            f"Six attribute tables (no geometry). {NET_NOTE} Every tool "
            f"filters {DUPLICATE_FIELD}='No' by default -- totals will "
            f"not match the public dashboard unless its user also "
            f"filters that column. {FY2026_PARTIAL_NOTE}",
            "",
        ]
        for info, total, dedup in zip(infos, totals, dedups):
            lines.append(f"### Table {info.id} -- {info.name} ({info.label})")
            if info.status == "empty":
                lines.append("- Status: EMPTY (0 rows upstream)")
            lines.append(
                f"- Rows: {fmt_count(total)} total / {fmt_count(dedup)} "
                f"with the default {DUPLICATE_FIELD}='No' filter"
            )
            lines.append(f"- {info.description}")
            lines.append(
                f"- Measure fields (net dollars): {', '.join(info.measure_fields)}"
            )
            lines.append(f"- Dimension fields: {', '.join(info.dimension_fields)}")
            if info.entity_field:
                lines.append(f"- Payee field: {info.entity_field}")
            if info.code_label_fields:
                lines.append(
                    f"- Coded 'code : label' fields: "
                    f"{', '.join(info.code_label_fields)}"
                )
            if info.caveats:
                lines.append("- Caveats:")
                lines.extend(f"  - {c}" for c in info.caveats)
            lines.append("")
        lines.append(
            "_The upstream service also carries an OC_Point layer: an "
            "empty geometry placeholder that exists only so the public "
            "app can mount a map widget. It holds no data and is not "
            "exposed here._"
        )
        retrieved_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        lines += [
            "",
            "---",
            f"Source: {self.plugin_config.service_url}",
            f"Data snapshot (PubDate): {pubdate or 'unavailable'} | "
            f"Retrieved: {retrieved_at}",
        ]
        return "\n".join(lines)

    # ── Tool: get_table_schema ────────────────────────────────────────

    def _snapshot_field_types(self, table_id: int) -> Dict[str, str]:
        if self._snapshot_types is None:
            try:
                with open(self.SCHEMA_SNAPSHOT_PATH, encoding="utf-8") as fh:
                    snap = json.load(fh)
                self._snapshot_types = {
                    int(tid): {
                        f["name"]: f.get("type", "?") for f in t.get("fields", [])
                    }
                    for tid, t in snap.items()
                }
            except Exception as e:
                logger.warning(f"could not load vendored schema snapshot: {e}")
                self._snapshot_types = {}
        return self._snapshot_types.get(table_id, {})

    def _field_notes(self, info: TableInfo, field_name: str) -> str:
        notes: List[str] = []
        if field_name in info.code_label_fields:
            notes.append(
                "'code : label' string; outputs split it into "
                f"{field_name}_code and {field_name}_label"
            )
        if field_name == info.entity_field:
            notes.append(
                "payee/entity field; spellings are unnormalized (see search_by_vendor)"
            )
        if field_name in info.measure_fields:
            notes.append("NET dollar measure (includes offsetting entries)")
        if field_name == DUPLICATE_FIELD:
            notes.append(
                "ETL double-load flag; every tool filters Duplicate='No' by default"
            )
        if field_name == PUBDATE_FIELD:
            notes.append(
                "ETL snapshot stamp, identical on every row -- NOT "
                "transaction time; date filters on it are rejected"
            )
        if field_name == PERIOD_FIELD:
            notes.append(PERIOD_NOTE)
        if field_name == FISCAL_YEAR_FIELD:
            notes.append("stored as a STRING, e.g. '2023'")
        if field_name == "Location":
            notes.append(LOCATION_NOTE)
        if field_name == "OBJECTID":
            notes.append("internal row id")
        return "; ".join(notes)

    async def _get_table_schema(self, args: Dict[str, Any]) -> str:
        info = self._table_info(args.get("table"))
        types = self._snapshot_field_types(info.id)

        # Cardinality via returnDistinctValues, counted client-side --
        # NEVER count_distinct, which this service advertises but which
        # errors in practice (verified live).
        card_fields = [
            f
            for f in info.all_fields
            if f not in ("OBJECTID", PUBDATE_FIELD) and f not in info.measure_fields
        ]
        distinct_lists = await asyncio.gather(
            *(
                self._fetch_distinct_values(info.id, f, limit=self.CARDINALITY_CAP + 1)
                for f in card_fields
            ),
            return_exceptions=True,
        )
        cardinality: Dict[str, str] = {}
        for f, values in zip(card_fields, distinct_lists):
            if isinstance(values, Exception):
                cardinality[f] = "n/a"
            elif len(values) > self.CARDINALITY_CAP:
                cardinality[f] = f">{self.CARDINALITY_CAP:,}"
            else:
                cardinality[f] = f"{len(values):,}"

        pubdate = await self._pubdate_snapshot(info.id)
        lines = [
            f"# Schema: table {info.id} -- {info.name} ({info.label})",
            "",
            info.description,
        ]
        if info.status == "empty":
            lines += ["", "**Status: EMPTY** -- every query returns 0 rows."]
        lines += [
            "",
            "Field | Type | Distinct values | Notes",
        ]
        for f in info.all_fields:
            ftype = types.get(f, "?").replace("esriFieldType", "")
            lines.append(
                f"{f} | {ftype} | {cardinality.get(f, '--')} | "
                f"{self._field_notes(info, f)}"
            )
        lines += [
            "",
            f"(Distinct-value counts are computed client-side from "
            f"returnDistinctValues, capped at {self.CARDINALITY_CAP:,}; "
            f"the service's count_distinct statistic is advertised but "
            f"broken. Measure fields are continuous and not counted.) "
            f"{CASE_SENSITIVE_NOTE}",
            "",
            self._provenance_footer(info, pubdate=pubdate),
        ]
        return "\n".join(lines)

    # ── Tool: spending_stats ──────────────────────────────────────────

    STAT_TYPES = (
        "count",
        "sum",
        "avg",
        "min",
        "max",
        "stddev",
        "var",
        "percentile_cont",
    )

    @staticmethod
    def _parse_group_by(raw: Any) -> List[str]:
        if raw is None:
            return []
        if isinstance(raw, str):
            return [g.strip() for g in raw.split(",") if g.strip()]
        if isinstance(raw, (list, tuple)):
            return [str(g).strip() for g in raw if str(g).strip()]
        raise ToolInputError(
            f"group_by must be a field name or a list of field names (got {raw!r})"
        )

    async def _spending_stats(self, args: Dict[str, Any]) -> str:
        info = self._table_info(args.get("table"))
        measure = str(args.get("measure") or info.default_measure or "").strip()
        if measure not in info.measure_fields:
            raise ToolInputError(
                f"measure must be one of {list(info.measure_fields)} for "
                f"table {info.id} ({info.name}), got {measure!r}. There "
                f"is no universal 'Amount' field -- table 2's measures "
                f"are Total_Payroll_Cost/Salaries_Wages/Overtime/"
                f"Liabilities_Benefits."
            )
        stat_type = str(args.get("stat_type") or "sum").strip().lower()
        if stat_type not in self.STAT_TYPES:
            raise ToolInputError(
                f"stat_type must be one of {list(self.STAT_TYPES)} "
                f"(got {stat_type!r}). Median = percentile_cont with "
                f"percentile=0.5."
            )
        groups = self._parse_group_by(args.get("group_by"))
        for g in groups:
            if g == PUBDATE_FIELD:
                self._reject_pubdate_filter(g)
            self._check_field_exists(info, g, "group_by")

        dedup_clause, dup_warning = self._dedup_parts(args)
        clauses, years = self._structured_where(info, args)
        where = self._combine_where(dedup_clause, *clauses)

        out_name = f"{stat_type}_{measure}"
        stat_entry: Dict[str, Any] = {
            "statisticType": stat_type,
            "onStatisticField": measure,
            "outStatisticFieldName": out_name,
        }
        percentile = None
        if stat_type == "percentile_cont":
            percentile = self._float_arg(args, "percentile", 0.5)
            if not (0.0 <= percentile <= 1.0):
                raise ToolInputError(
                    f"percentile must be between 0 and 1 "
                    f"(got {percentile}); 0.5 is the median"
                )
            stat_entry["statisticParameters"] = {"value": percentile}
        out_statistics = [
            stat_entry,
            {
                "statisticType": "count",
                "onStatisticField": "OBJECTID",
                "outStatisticFieldName": "row_count",
            },
        ]

        limit, requested = self._clamp_limit(args, default=50, maximum=self.MAX_GROUPS)
        order = str(args.get("order") or "").strip().lower()
        if order not in ("", "group", "value"):
            raise ToolInputError(f"order must be 'group' or 'value' (got {order!r})")
        if not order:
            # Time-series groupings read chronologically; everything
            # else ranks by the statistic.
            order = (
                "group"
                if groups and groups[0] in (FISCAL_YEAR_FIELD, PERIOD_FIELD)
                else "value"
            )
        order_by = (
            ",".join(groups) if (order == "group" and groups) else f"{out_name} DESC"
        )

        rows = await self._query_statistics(
            info.id,
            where,
            out_statistics,
            group_by=",".join(groups) if groups else None,
            order_by=order_by,
            extra_params={"resultRecordCount": str(limit)} if groups else None,
        )

        stat_label = stat_type
        if percentile is not None:
            stat_label = f"percentile_cont({percentile})"
        heading = f"## Net {stat_label} of {measure}" + (
            f" by {', '.join(groups)}" if groups else ""
        )

        notices: List[str] = []
        if dup_warning:
            notices.append(dup_warning)
        notices.append(NET_NOTE)
        notices.extend(self._fiscal_notices(info, sorted(years) if years else None))
        if info.status == "empty":
            notices.append(f"Table {info.id} is EMPTY upstream (0 rows).")
        # Trap 2.8, made explicit in-band: a Fiscal_Year grouping on
        # Revenue that spans years silently skips FY2024.
        if (
            info.id == 4
            and FISCAL_YEAR_FIELD in groups
            and years is None
            and not any(str(r.get(FISCAL_YEAR_FIELD)) == "2024" for r in rows)
        ):
            notices.append(
                "Confirmed in these results: FY2024 is ABSENT from the "
                "groups below (no revenue rows exist for it upstream)."
            )

        lines = [heading, ""]
        lines.extend(notices)
        lines.append("")
        if not rows:
            lines.append("No statistics returned (0 matching rows).")
        else:
            money_stat = stat_type != "count"
            display: List[Dict[str, Any]] = []
            for row in rows:
                rec: Dict[str, Any] = {}
                for g in groups:
                    rec[g] = row.get(g)
                value = row.get(out_name)
                rec[f"net_{stat_label}_{measure}" if money_stat else out_name] = (
                    self._fmt_money(value) if money_stat else value
                )
                rec["rows"] = row.get("row_count")
                display.append(rec)
            display = self._expand_code_labels(display, info)
            lines.extend(self._format_table(display, info.id))
            if groups:
                lines.append("")
                group_note = f"({len(rows)} group(s), ordered by {order}."
                if len(rows) >= limit:
                    group_note += (
                        f" The group list may be truncated at "
                        f"limit={limit}; raise limit for more.)"
                    )
                else:
                    group_note += ")"
                lines.append(group_note)
        lines += [
            "",
            self._provenance_footer(
                info,
                where=where,
                limit=limit if groups else None,
                requested=requested if groups else None,
                row_count=len(rows),
                pubdate=await self._pubdate_snapshot(info.id),
            ),
        ]
        return "\n".join(lines)

    # ── Tool: search_by_vendor ────────────────────────────────────────

    async def _entity_group_stats(
        self,
        info: TableInfo,
        where: str,
        measure: str,
        limit: int,
    ) -> List[Dict[str, Any]]:
        """Distinct entity spellings with per-spelling net total + row
        count, ordered by net total descending."""
        entity = self._require_entity_field(info)
        return await self._query_statistics(
            info.id,
            where,
            [
                {
                    "statisticType": "sum",
                    "onStatisticField": measure,
                    "outStatisticFieldName": "net_total",
                },
                {
                    "statisticType": "count",
                    "onStatisticField": "OBJECTID",
                    "outStatisticFieldName": "row_count",
                },
            ],
            group_by=entity,
            order_by="net_total DESC",
            extra_params={"resultRecordCount": str(limit)},
        )

    async def _search_by_vendor(self, args: Dict[str, Any]) -> str:
        name = str(args.get("name_contains") or "").strip()
        if not name:
            raise ToolInputError("name_contains is required")
        info = self._table_info(args.get("table", 0))
        entity = self._require_entity_field(info)
        measure = info.default_measure
        limit, requested = self._clamp_limit(args, default=50, maximum=200)

        dedup_clause, dup_warning = self._dedup_parts(args)
        year_clause = None
        years = None
        if args.get("fiscal_year") is not None:
            year = self._validate_fiscal_year(args["fiscal_year"])
            year_clause = f"{FISCAL_YEAR_FIELD} = '{year}'"
            years = {year}
        where = self._combine_where(
            dedup_clause,
            self._contains_clause(entity, name),
            self._entity_not_null_clause(info),
            year_clause,
        )

        rows = await self._entity_group_stats(info, where, measure, limit)

        notices = [VENDOR_NORMALIZATION_NOTE]
        if dup_warning:
            notices.insert(0, dup_warning)
        if any(str(r.get(entity)) == "Refunds" for r in rows):
            notices.append(REFUNDS_NOTE)
        notices.extend(self._fiscal_notices(info, sorted(years) if years else None))

        lines = [
            f"## {entity} spellings matching `{name}` (case-insensitive substring)",
            "",
        ]
        lines.extend(notices)
        lines.append("")
        if not rows:
            lines.append(
                f"No {entity} values contain `{name}`. The match is a "
                f"case-insensitive substring; try a shorter fragment. "
                f"NULL-payee rows (journal entries, transfers) are "
                f"always excluded."
            )
        else:
            combined_net = sum(r.get("net_total") or 0 for r in rows)
            combined_rows = sum(r.get("row_count") or 0 for r in rows)
            summary = (
                f"{len(rows)} distinct spelling(s); combined net total "
                f"{self._fmt_money(combined_net)} across "
                f"{combined_rows:,} rows"
            )
            if len(rows) >= limit:
                summary += (
                    f" (spelling list truncated at limit={limit}; the "
                    f"combined figures cover only the spellings shown)"
                )
            lines += [summary + ".", ""]
            display = [
                {
                    entity: r.get(entity),
                    "net_total": self._fmt_money(r.get("net_total")),
                    "rows": r.get("row_count"),
                }
                for r in rows
            ]
            lines.extend(self._format_table(display, info.id))
        lines += [
            "",
            self._provenance_footer(
                info,
                where=where,
                limit=limit,
                requested=requested,
                row_count=len(rows),
                pubdate=await self._pubdate_snapshot(info.id),
            ),
        ]
        return "\n".join(lines)

    # ── Tool: top_vendors ─────────────────────────────────────────────

    async def _top_vendors(self, args: Dict[str, Any]) -> str:
        info = self._table_info(args.get("table", 0))
        entity = self._require_entity_field(info)
        measure = info.default_measure
        requested = self._int_arg(args, "n", 20)
        n = max(1, min(requested, 100))

        dedup_clause, dup_warning = self._dedup_parts(args)
        filter_args = {k: args.get(k) for k in ("fiscal_year", "business_area")}
        clauses, years = self._structured_where(info, filter_args)
        where = self._combine_where(
            dedup_clause,
            self._entity_not_null_clause(info),
            f"{entity} <> 'Refunds'",
            *clauses,
        )

        rows = await self._entity_group_stats(info, where, measure, n)

        notices = [
            f"NULL payees (journal entries, fund transfers) and the "
            f"'Refunds' accounting label are excluded. {REFUNDS_NOTE}",
            VENDOR_NORMALIZATION_NOTE,
            NET_NOTE,
        ]
        if dup_warning:
            notices.insert(0, dup_warning)
        notices.extend(self._fiscal_notices(info, sorted(years) if years else None))

        scope_bits = []
        if args.get("fiscal_year") is not None:
            scope_bits.append(f"FY{args['fiscal_year']}")
        if args.get("business_area"):
            scope_bits.append(f"Business_Area~'{args['business_area']}'")
        scope = f" ({', '.join(scope_bits)})" if scope_bits else " (all years)"

        lines = [
            f"## Top {n} payees by net {measure} -- table {info.id} "
            f"({info.label}){scope}",
            "",
        ]
        lines.extend(notices)
        lines.append("")
        if not rows:
            lines.append("No matching rows.")
        else:
            display = [
                {
                    "rank": i,
                    entity: r.get(entity),
                    "net_total": self._fmt_money(r.get("net_total")),
                    "rows": r.get("row_count"),
                }
                for i, r in enumerate(rows, 1)
            ]
            lines.extend(self._format_table(display, info.id))
        lines += [
            "",
            self._provenance_footer(
                info,
                where=where,
                limit=n,
                requested=requested,
                row_count=len(rows),
                pubdate=await self._pubdate_snapshot(info.id),
            ),
        ]
        return "\n".join(lines)

    # ── Tool: get_line_items ──────────────────────────────────────────

    async def _get_line_items(self, args: Dict[str, Any]) -> str:
        info = self._table_info(args.get("table"))
        include_dup = bool(args.get("include_duplicates", False))
        dedup_clause, dup_warning = self._dedup_parts(args)
        clauses, years = self._structured_where(info, args)
        where = self._combine_where(dedup_clause, *clauses)
        limit, requested = self._clamp_limit(args, default=100, maximum=self.MAX_ROWS)
        offset = max(0, self._int_arg(args, "offset", 0))
        order_by = self._validate_order_by(info, args.get("order_by"))
        out_fields = self._validate_out_fields(
            info, args.get("out_fields"), include_dup
        )

        records_task = self._query_table(
            info.id,
            where,
            out_fields,
            limit,
            offset=offset,
            order_by=order_by or None,
        )
        (records, _), total = await asyncio.gather(
            records_task, self._fetch_count(info.id, where)
        )
        records = self._expand_code_labels(records, info)

        notices: List[str] = []
        if dup_warning:
            notices.append(dup_warning)
        notices.extend(self._fiscal_notices(info, sorted(years) if years else None))
        if info.status == "empty":
            notices.append(f"Table {info.id} is EMPTY upstream (0 rows).")

        text = self._format_rows_response(
            info,
            records,
            where=where,
            limit=limit,
            requested=requested,
            total_count=total,
            heading=(
                f"## Line items -- table {info.id} ({info.label})"
                + (f", offset {offset}" if offset else "")
            ),
            notices=notices or None,
            pubdate=await self._pubdate_snapshot(info.id),
        )
        remaining = None if total is None else total - (offset + len(records))
        if remaining is not None and remaining > 0:
            text += (
                f"\n**MORE PAGES AVAILABLE:** call get_line_items again "
                f"with offset={offset + len(records)} (same filters) "
                f"for the next page."
            )
        return text

    # ── Tool: list_field_values ───────────────────────────────────────

    async def _list_field_values(self, args: Dict[str, Any]) -> str:
        info = self._table_info(args.get("table"))
        field_name = self._check_field_exists(
            info, str(args.get("field") or ""), "field"
        )
        if field_name in info.measure_fields:
            raise ToolInputError(
                f"{field_name} is a continuous dollar measure -- listing "
                f"its distinct values is not meaningful. Use "
                f"spending_stats (sum/avg/min/max/percentile_cont) "
                f"instead."
            )
        if field_name == PUBDATE_FIELD:
            self._reject_pubdate_filter(field_name)
        if field_name == "OBJECTID":
            raise ToolInputError("OBJECTID is an internal row id; nothing to list.")

        limit, requested = self._clamp_limit(args, default=100, maximum=1000)
        dedup_clause, dup_warning = self._dedup_parts(args)
        where = self._combine_where(dedup_clause)
        values = await self._fetch_distinct_values(
            info.id, field_name, where=where, limit=limit + 1
        )
        more = len(values) > limit
        values = values[:limit]

        notices: List[str] = []
        if dup_warning:
            notices.append(dup_warning)
        if field_name == PERIOD_FIELD:
            notices.append(PERIOD_NOTE)
        if field_name == "Location":
            notices.append(LOCATION_NOTE)
        if field_name == info.entity_field:
            notices.append(VENDOR_NORMALIZATION_NOTE)
        if field_name == FISCAL_YEAR_FIELD:
            notices.extend(self._fiscal_notices(info, None))

        lines = [
            f"## Distinct values of {field_name} -- table {info.id} ({info.label})",
            "",
        ]
        lines.extend(notices)
        if notices:
            lines.append("")
        if not values:
            lines.append("No values (table or field is empty).")
        else:
            lines.append(
                f"{len(values):,} distinct value(s)"
                + (
                    f" -- MORE EXIST past limit={limit}; raise limit "
                    f"or narrow the query"
                    if more
                    else ""
                )
                + " (counted client-side via returnDistinctValues; the "
                "service's count_distinct statistic is broken)."
            )
            lines.append("")
            if field_name in info.code_label_fields:
                display = [
                    dict(
                        zip(
                            ("code", "label"),
                            self._split_code_label(v),
                        )
                    )
                    for v in values
                ]
                lines.extend(self._format_table(display, info.id))
            else:
                for v in values:
                    lines.append(f"- {self._render_value(info.id, field_name, v)}")
        lines += [
            "",
            self._provenance_footer(
                info,
                where=where,
                limit=limit,
                requested=requested,
                row_count=len(values),
                pubdate=await self._pubdate_snapshot(info.id),
            ),
        ]
        return "\n".join(lines)

    # ── Tool: query_checkbook ─────────────────────────────────────────

    async def _query_checkbook(self, args: Dict[str, Any]) -> str:
        info = self._table_info(args.get("table"))
        raw_where = str(args.get("where") or "").strip()
        if not raw_where:
            raise ToolInputError(
                "where is required ('1=1' matches everything). Prefer "
                "the structured tools (spending_stats, get_line_items) "
                "when they can express your filter."
            )
        include_dup = bool(args.get("include_duplicates", False))
        user_where = self._validate_raw_where(info, raw_where)
        dedup_clause, dup_warning = self._dedup_parts(args)
        where = self._combine_where(dedup_clause, user_where)
        limit, requested = self._clamp_limit(args, default=200, maximum=self.MAX_ROWS)
        offset = max(0, self._int_arg(args, "offset", 0))
        order_by = self._validate_order_by(info, args.get("order_by"))
        out_fields = self._validate_out_fields(
            info, args.get("out_fields"), include_dup
        )

        records_task = self._query_table(
            info.id,
            where,
            out_fields,
            limit,
            offset=offset,
            order_by=order_by or None,
        )
        (records, _), total = await asyncio.gather(
            records_task, self._fetch_count(info.id, where)
        )
        records = self._expand_code_labels(records, info)

        notices: List[str] = []
        if dup_warning:
            notices.append(dup_warning)
        if info.status == "empty":
            notices.append(f"Table {info.id} is EMPTY upstream (0 rows).")

        text = self._format_rows_response(
            info,
            records,
            where=where,
            limit=limit,
            requested=requested,
            total_count=total,
            heading=f"## query_checkbook -- table {info.id} ({info.label})",
            notices=notices or None,
            pubdate=await self._pubdate_snapshot(info.id),
        )
        remaining = None if total is None else total - (offset + len(records))
        if remaining is not None and remaining > 0:
            text += (
                f"\n**MORE PAGES AVAILABLE:** call query_checkbook "
                f"again with offset={offset + len(records)} (same "
                f"where/order_by) for the next page."
            )
        return text

    # ── Tool definitions ──────────────────────────────────────────────

    def get_tools(self) -> List[ToolDefinition]:
        annotations = {"readOnlyHint": True, "openWorldHint": True}
        city = (
            self.plugin_config.city_name
            if self.plugin_config
            else "Municipality of Anchorage"
        )
        dedup_note = (
            "Duplicate rows (ETL double-loads: FY2023 everywhere, plus "
            "FY2026 in Revenue) are filtered out by default, so totals "
            "will not match the public dashboard unless it is also "
            "filtered on the Duplicate column."
        )
        routing_note = (
            "This server covers ONLY Open Checkbook financial tables; "
            "route parcel/assessment questions to the Anchorage Parcels "
            "MCP and spatial questions to the Anchorage GIS MCP."
        )
        table_schema = {
            "type": "integer",
            "enum": [0, 1, 2, 3, 4, 5],
            "description": (
                "Table id: 0=Non-payroll expenditures (primary), "
                "1=Payroll expenditures, 2=Payroll cost rollup (no "
                "Amount field), 3=Procurement, 4=Revenue (payee field "
                "is Customer_Business_Name), 5=Revenue-vs-expenditure "
                "(EMPTY). Call list_tables for details."
            ),
        }
        include_duplicates_schema = {
            "type": "boolean",
            "default": False,
            "description": (
                "Set True to DISABLE the default Duplicate='No' filter. "
                "The ETL double-loaded whole fiscal years (FY2023 on "
                "every table; FY2023 AND FY2026 on Revenue) as exact "
                "shadow copies -- including them double-counts those "
                "years. The response will carry a warning."
            ),
        }
        fiscal_year_schema = {
            "type": "integer",
            "description": (
                "4-digit fiscal year, e.g. 2023. (Stored as a string "
                "upstream; converted automatically.) Coverage gaps: "
                "Revenue has no FY2024; FY2026 is partial (through "
                "period 7)."
            ),
        }
        fiscal_period_schema = {
            "type": "integer",
            "minimum": 1,
            "maximum": 16,
            "description": PERIOD_NOTE,
        }
        business_area_schema = {
            "type": "string",
            "description": (
                "Department filter, case-insensitive substring (e.g. "
                "'Police', 'Parks'). 20 values -- see "
                "list_field_values(table, 'Business_Area')."
            ),
        }
        fund_schema = {
            "type": "string",
            "description": (
                "Fund filter, case-insensitive substring matched "
                "against the 'code : label' string -- a fund code "
                "('141000') or label fragment ('Roads') both work."
            ),
        }
        limit_schema = lambda default, maximum: {  # noqa: E731
            "type": "integer",
            "default": default,
            "description": f"Max results (1-{maximum}; clamps are echoed "
            f"in the provenance line)",
        }
        return [
            ToolDefinition(
                name="list_tables",
                description=(
                    f"The six {city} Open Checkbook tables with live row "
                    f"counts (total and deduplicated), measure fields, "
                    f"dimension fields, payee field, and per-table "
                    f"caveats (Revenue is missing FY2024; the payroll "
                    f"rollup has no Amount field; table 5 is empty). "
                    f"START HERE for discovery -- there is no separate "
                    f"catalog service. Zero arguments. {dedup_note}"
                ),
                input_schema={"type": "object", "properties": {}},
                annotations=annotations,
            ),
            ToolDefinition(
                name="get_table_schema",
                description=(
                    f"Fields of one Checkbook table: type, distinct-value "
                    f"count (via returnDistinctValues -- the service's "
                    f"count_distinct is broken), and semantics: which "
                    f"fields are 'code : label' strings (Fund, "
                    f"G_L_Account), the payee field, the net-dollar "
                    f"measures, and the traps (Duplicate flag, PubDate "
                    f"snapshot stamp, Location = vendor's billing "
                    f"city/state NOT {city} geography, fiscal periods "
                    f"13-16 = year-end adjustments). {CASE_SENSITIVE_NOTE}"
                ),
                input_schema={
                    "type": "object",
                    "properties": {"table": table_schema},
                    "required": ["table"],
                },
                annotations=annotations,
            ),
            ToolDefinition(
                name="spending_stats",
                description=(
                    f"THE WORKHORSE for aggregate questions: server-side "
                    f"statistics (default: net sum + row count) over any "
                    f"Checkbook table, optionally grouped by one or more "
                    f"fields. All dollar figures are NET of offsetting "
                    f"entries -- gross totals are meaningless. Example "
                    f"-- net non-payroll spending by department in 2025: "
                    f"spending_stats(table=0, group_by=['Business_Area'], "
                    f"fiscal_year=2025). Also supports stat_type count/"
                    f"avg/min/max/stddev/var/percentile_cont (median = "
                    f"percentile=0.5). 'code : label' group values are "
                    f"split into code and label columns. {dedup_note} "
                    f"{routing_note}"
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "table": table_schema,
                        "group_by": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "Field(s) to group by, e.g. "
                                "['Fiscal_Year'] or ['Business_Area', "
                                "'Fiscal_Year']. " + CASE_SENSITIVE_NOTE
                            ),
                        },
                        "measure": {
                            "type": "string",
                            "description": (
                                "Dollar field to aggregate. Defaults to "
                                "the table's primary measure (Amount, "
                                "or Total_Payroll_Cost on table 2 -- "
                                "which has NO Amount field; its other "
                                "measures are Salaries_Wages, Overtime, "
                                "Liabilities_Benefits)."
                            ),
                        },
                        "stat_type": {
                            "type": "string",
                            "enum": list(self.STAT_TYPES),
                            "default": "sum",
                            "description": (
                                "Statistic to compute (row count is "
                                "always included). percentile_cont uses "
                                "the percentile parameter."
                            ),
                        },
                        "percentile": {
                            "type": "number",
                            "default": 0.5,
                            "description": (
                                "For percentile_cont only: 0-1 (0.5 = median)"
                            ),
                        },
                        "fiscal_year": fiscal_year_schema,
                        "fiscal_period": fiscal_period_schema,
                        "business_area": business_area_schema,
                        "fund": fund_schema,
                        "gl_account": {
                            "type": "string",
                            "description": (
                                "G/L account filter, substring against "
                                "the 'code : label' string."
                            ),
                        },
                        "vendor_contains": {
                            "type": "string",
                            "description": (
                                "Payee filter, case-insensitive "
                                "substring. Adds an IS NOT NULL guard. "
                                "Vendor names are unnormalized -- one "
                                "entity may have several spellings."
                            ),
                        },
                        "order": {
                            "type": "string",
                            "enum": ["value", "group"],
                            "description": (
                                "'value' ranks groups by the statistic "
                                "(descending); 'group' orders by the "
                                "group fields. Default: 'group' when "
                                "grouping by Fiscal_Year or "
                                "Month_Fiscal_Period, else 'value'."
                            ),
                        },
                        "limit": limit_schema(50, 200),
                        "include_duplicates": include_duplicates_schema,
                    },
                    "required": ["table"],
                },
                annotations=annotations,
            ),
            ToolDefinition(
                name="search_by_vendor",
                description=(
                    f"Find payees by name fragment (case-insensitive "
                    f"substring -- the backend's LIKE is already "
                    f"case-insensitive). Returns DISTINCT SPELLINGS with "
                    f"a per-spelling net total and row count, because "
                    f"vendor names are NOT normalized upstream: one "
                    f"entity may appear under several spellings (e.g. "
                    f"TransUnion at least three ways), so a "
                    f"single-spelling total may undercount the entity. "
                    f"NULL payees (journal entries, fund transfers) are "
                    f"always excluded; the 'Refunds' label is real but "
                    f"is not a business. On the Revenue table the payee "
                    f"field is Customer_Business_Name. Example: "
                    f"search_by_vendor(name_contains='chugach'). "
                    f"{dedup_note}"
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "name_contains": {
                            "type": "string",
                            "description": (
                                "Payee name fragment (case-insensitive substring match)"
                            ),
                        },
                        "table": {**table_schema, "default": 0},
                        "fiscal_year": fiscal_year_schema,
                        "limit": limit_schema(50, 200),
                        "include_duplicates": include_duplicates_schema,
                    },
                    "required": ["name_contains"],
                },
                annotations=annotations,
            ),
            ToolDefinition(
                name="top_vendors",
                description=(
                    f"Convenience ranking: top N payees by net total on "
                    f"one table, optionally scoped to a fiscal year "
                    f"and/or department. NULL payees and the 'Refunds' "
                    f"accounting label are excluded (the response says "
                    f"so). Figures are NET. Spellings are unnormalized "
                    f"-- the same entity may hold several ranks. "
                    f"Example: top_vendors(fiscal_year=2025, n=20). "
                    f"{dedup_note}"
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "table": {**table_schema, "default": 0},
                        "fiscal_year": fiscal_year_schema,
                        "business_area": business_area_schema,
                        "n": {
                            "type": "integer",
                            "default": 20,
                            "description": "How many payees (1-100)",
                        },
                        "include_duplicates": include_duplicates_schema,
                    },
                    "required": [],
                },
                annotations=annotations,
            ),
            ToolDefinition(
                name="get_line_items",
                description=(
                    f"Raw accounting rows from one table, filtered by "
                    f"structured params (no SQL). Compact pipe-delimited "
                    f"output; 'code : label' fields are split; hard cap "
                    f"{self.MAX_ROWS} rows per call with a bookended "
                    f"TRUNCATED banner quoting the true match count. Use "
                    f"spending_stats for totals -- do NOT sum a "
                    f"truncated row listing. Example: get_line_items("
                    f"table=0, fiscal_year=2025, vendor_contains="
                    f"'premera'). {dedup_note}"
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "table": table_schema,
                        "fiscal_year": fiscal_year_schema,
                        "fiscal_period": fiscal_period_schema,
                        "business_area": business_area_schema,
                        "fund": fund_schema,
                        "gl_account": {
                            "type": "string",
                            "description": (
                                "G/L account filter, substring against "
                                "the 'code : label' string."
                            ),
                        },
                        "process_type": {
                            "type": "string",
                            "description": (
                                "Procurement (table 3) only: process type substring."
                            ),
                        },
                        "vendor_contains": {
                            "type": "string",
                            "description": (
                                "Payee substring filter (adds IS NOT NULL)."
                            ),
                        },
                        "order_by": {
                            "type": "string",
                            "description": (
                                "e.g. 'Amount DESC'. Note: amounts are "
                                "NET -- 'largest transactions' include "
                                "offsetting entries booked and reversed "
                                "at huge values; do not read the top "
                                "rows as the biggest real payments "
                                "without checking for a matching "
                                "negative row."
                            ),
                        },
                        "out_fields": {
                            "type": "string",
                            "description": (
                                "Comma-separated field names or '*'. "
                                "Defaults to all substantive fields "
                                "(OBJECTID/PubDate/Duplicate omitted). "
                                + CASE_SENSITIVE_NOTE
                            ),
                        },
                        "offset": {
                            "type": "integer",
                            "default": 0,
                            "description": "Skip this many rows (pagination)",
                        },
                        "limit": limit_schema(100, 500),
                        "include_duplicates": include_duplicates_schema,
                    },
                    "required": ["table"],
                },
                annotations=annotations,
            ),
            ToolDefinition(
                name="list_field_values",
                description=(
                    f"Distinct values of a dimension field "
                    f"(Business_Area, Fund, G_L_Account, Process_Type, "
                    f"Fiscal_Year, Month_Fiscal_Period, ...) via "
                    f"returnDistinctValues, counted client-side (the "
                    f"service's count_distinct is broken). 'code : "
                    f"label' values are split into code and label "
                    f"columns. Month_Fiscal_Period values run 1-16: "
                    f"13-16 are year-end adjustment periods, not "
                    f"calendar months. {CASE_SENSITIVE_NOTE}"
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "table": table_schema,
                        "field": {
                            "type": "string",
                            "description": (
                                "Field to list (not a dollar measure; "
                                "use spending_stats for those). " + CASE_SENSITIVE_NOTE
                            ),
                        },
                        "limit": limit_schema(100, 1000),
                        "include_duplicates": include_duplicates_schema,
                    },
                    "required": ["table", "field"],
                },
                annotations=annotations,
            ),
            ToolDefinition(
                name="query_checkbook",
                description=(
                    f"Escape hatch: run a SQL WHERE clause against one "
                    f"Checkbook table when the structured tools cannot "
                    f"express the filter. The clause is validated "
                    f"against injection SHAPES (vendor names containing "
                    f"'UNION', like 'Credit Union 1' or 'IBEW Local "
                    f"Union 1547', are fine) and against the table "
                    f"schema. Duplicate='No' is STILL injected unless "
                    f"include_duplicates=True. Date filters on PubDate "
                    f"are rejected -- use Fiscal_Year + "
                    f"Month_Fiscal_Period. Example: query_checkbook("
                    f'table=0, where="Amount < -1000000 AND '
                    f"Fiscal_Year = '2025'\"). Fiscal_Year is a STRING "
                    f"-- quote it. {CASE_SENSITIVE_NOTE} {routing_note}"
                ),
                input_schema={
                    "type": "object",
                    "properties": {
                        "table": table_schema,
                        "where": {
                            "type": "string",
                            "description": (
                                "SQL WHERE clause ('1=1' matches "
                                "everything). String values in single "
                                "quotes; Fiscal_Year is a string "
                                "('2025'). " + CASE_SENSITIVE_NOTE
                            ),
                        },
                        "out_fields": {
                            "type": "string",
                            "description": (
                                "Comma-separated field names or '*'; "
                                "defaults to all substantive fields."
                            ),
                        },
                        "order_by": {
                            "type": "string",
                            "description": "e.g. 'Amount DESC'",
                        },
                        "offset": {
                            "type": "integer",
                            "default": 0,
                            "description": "Skip this many rows (pagination)",
                        },
                        "limit": limit_schema(200, 500),
                        "include_duplicates": include_duplicates_schema,
                    },
                    "required": ["table", "where"],
                },
                annotations=annotations,
            ),
        ]

    # ── Dispatch ──────────────────────────────────────────────────────

    async def execute_tool(
        self, tool_name: str, arguments: Dict[str, Any]
    ) -> ToolResult:
        handlers: Dict[str, Any] = {
            "list_tables": self._list_tables,
            "get_table_schema": self._get_table_schema,
            "spending_stats": self._spending_stats,
            "search_by_vendor": self._search_by_vendor,
            "top_vendors": self._top_vendors,
            "get_line_items": self._get_line_items,
            "list_field_values": self._list_field_values,
            "query_checkbook": self._query_checkbook,
        }
        handler = handlers.get(tool_name)
        if handler is None:
            return ToolResult(
                content=[],
                success=False,
                error_message=f"Unknown tool: {tool_name}",
            )
        try:
            text = await handler(arguments)
            return ToolResult(
                content=[{"type": "text", "text": self._with_retrieved_footer(text)}],
                success=True,
            )
        except ToolInputError as e:
            # The caller asked for something invalid. That is not a
            # server fault, so no traceback -- a stack trace here is a
            # false claim that this server broke, and it buries the real
            # ones. The message still reaches the caller verbatim.
            logger.warning(
                f"Invalid arguments for tool {tool_name}: {e}",
                extra={"tool_name": tool_name, "error_type": type(e).__name__},
            )
            return ToolResult(
                content=[],
                success=False,
                error_message=str(e) if str(e) else "Invalid tool arguments",
            )

        except Exception as e:
            logger.error(f"Error executing tool {tool_name}: {e}", exc_info=True)
            return ToolResult(
                content=[],
                success=False,
                error_message=str(e) if str(e) else "Tool execution failed",
            )
