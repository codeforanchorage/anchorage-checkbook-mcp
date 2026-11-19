# Built-in Plugins Reference

This fork ships one plugin.

## `anchorage_checkbook`

Read-only tools over the Municipality of Anchorage Open Checkbook: unaudited
non-payroll and payroll expenditures, a payroll cost rollup, procurement, and
revenue. The tools, the tables and the dataset's traps are documented in
[CHECKBOOK.md](CHECKBOOK.md).

### Configuration

```yaml
plugins:
  anchorage_checkbook:
    enabled: true
    service_url: "https://services2.arcgis.com/Ce3DhLRthdwbHlfF/arcgis/rest/services/MOA_OpenCheckbook_Hosted/FeatureServer"
    city_name: "Municipality of Anchorage"
    timeout: 20   # must stay below aws.lambda_timeout (28)
```

[`config-anchorage-checkbook.yaml`](../config-anchorage-checkbook.yaml) is the
complete configuration this fork deploys.

### Tools

Tool names carry the plugin prefix on the wire, for example
`anchorage_checkbook__spending_stats`.

- `list_tables()`
- `get_table_schema(table)`
- `spending_stats(table, group_by, stat_type, …filters)`
- `search_by_vendor(name_contains, table, fiscal_year, limit)`
- `top_vendors(fiscal_year, business_area, table, n)`
- `get_line_items(table, …filters, order_by, limit, offset)`
- `list_field_values(table, field, limit)`
- `query_checkbook(table, where, out_fields, order_by, limit, offset)`

## Upstream plugins not in this fork

Upstream OpenContext also has generic `ckan`, `arcgis` and `socrata` plugins.
This fork does not use them and they were deleted here; they remain in
[CityOfBoston/OpenContext](https://github.com/CityOfBoston/OpenContext).

## Custom plugins

To serve a different data source, write a plugin in `custom_plugins/` and
enable it in place of this one. See the
[Custom Plugins Guide](CUSTOM_PLUGINS.md).
