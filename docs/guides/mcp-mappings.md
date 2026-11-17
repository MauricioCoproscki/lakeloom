# Mapping an upstream MCP server

Lakeloom can read lake metadata through another MCP server, such as a Databricks or Snowflake
one, instead of a native connector (FR-SRC-05). A mapping YAML binds each `LakeSource`
operation to one of that server's tools. The format is described in
[ADR 0008](../decisions/0008-lake-sources-design.md): the tool name, an argument template, and
JMESPath expressions that pick the fields out of the tool result.

The examples in `examples/mcp-mappings/` are **not verified** against real servers.
`databricks.example.yaml` and `snowflake.example.yaml` keep `<tool-from-tools/list>` as every
tool name. Lakeloom does not guess third-party tool names: you read them from your own server
with `lakeloom sources check`.

## The loop

1. Copy an example next to your sources file and point an `mcp` source at it:

   ```yaml
   sources:
     - type: mcp
       name: dbx
       transport: streamable_http
       url: https://mcp.example.com/mcp
       token_env: DBX_MCP_TOKEN
       mapping: dbx.yaml
   ```

2. List the server's tools, with their input schemas:

   ```console
   $ lakeloom sources check dbx --tools
   list_catalogs   <tool-from-tools/list>  placeholder
   …
   tool    list_catalogs   List Unity Catalog catalogs.   {"properties": {}, "type": "object"}
   tool    get_table       Get one table.  {"properties": {"full_name": {"type": "string"}}, …}
   result  not ok
   ```

3. Edit the mapping:
   - put each tool name in place of its placeholder;
   - rename the arguments to the tool's input schema;
   - adjust the JMESPath expressions to the shape the tool returns.

4. Check the mapping against `tools/list` without calling anything:

   ```console
   $ lakeloom sources check dbx
   list_catalogs   list_catalogs   ok
   list_schemas    list_schemas    ok
   list_tables     list_tables     schema_mismatch argument 'page_token' is not in the tool's input schema
   describe_table  get_table       ok
   result  not ok
   ```

5. When every operation is `ok`, run the read operations once:

   ```console
   $ lakeloom sources check dbx --call
   …
   call    list_catalogs   ok      3 catalogs
   call    list_schemas    ok      4 schemas
   call    list_tables     ok      12 tables
   call    describe_table  ok      main.sales.orders: 9 columns
   result  ok
   ```

   `--call` runs `list_catalogs`, then `list_schemas` on the first catalog, then `list_tables`
   on the first schema, then `describe_table` on the first table. It uses the same adapter code
   as the server, so a JMESPath that picks the wrong field fails here exactly as it would in a
   scan. `get_table_version` and `profile_table` are never called.

`--json` prints the whole report. The exit code is 0 when everything is `ok`, 3 when something
is not, and 1 on an error (an unknown source name, for example). The bearer token is never
printed.

## What the statuses mean

| Status | Meaning |
|---|---|
| `ok` | The tool exists and the mapping's arguments fit its input schema. |
| `placeholder` | The tool name is still `<…>`. |
| `missing` | The server's `tools/list` has no tool of that name. |
| `schema_mismatch` | A mapped argument is not in the tool's input schema, or a required one is not mapped. Schemas that declare no properties are not checked. |
| call `error` | The operation ran and failed. The detail carries Lakeloom's error code, message and hint. |
| call `skipped` | The operation could not run: it is unmapped or not `ok`, or the step before it returned nothing. |

## Contributing a verified mapping

If you get a mapping to `result ok` with `--call` against a real server, please open a pull
request with it. Include the server's name and version and the date you checked. Remove
credentials and any host names you do not want public.
