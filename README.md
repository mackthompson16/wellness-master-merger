# Wellness Management Tool

Keep many app manifests consistent without hand-editing every file. The master manifest is the source of truth for audit, merge, and update operations.

## Inputs and outputs
- Inputs:
  - Working manifest file (`FilePicker1`)
  - Master manifest file (`FilePicker2`)
- Output:
  - `audit` mode: JSON diff report
  - `merge` mode: merged manifest
  - `update` mode: updated manifest

## Modes
- `audit`: compares each app header against master and returns structured diff data.
- `merge`: overlays app-only content onto master for each header; conflicting scalar leaves keep master value.
- `update`: applies `remove` rules then `update` rules from master instructions.

## Audit behavior
Audit runs per header and tracks 3 categories:

1. `text_diff`
- Leaf-level value mismatches where both sides have a node but scalar text/value differs.
- Leaf format is JSON-safe:
  ```json
  { "app": "<app value>", "master": "<master value>" }
  ```

2. `missing_content`
- Content present in master but not present in the app header.
- Includes path and subtree/leaf that is missing.

3. `unique_content`
- Content present in app header but not present in master.
- Includes path and subtree/leaf that is unique to that app.

Notes:
- `IGNORED_KEYS` are excluded from structural/key comparison.
- Lists are compared by stable item keys where possible (`featureID`, `type`, etc).

## Audit JSON output
Top-level shape:

```json
{
  "text_diff": {
    "<header>": { "...": "..." }
  },
  "missing_content": {
    "<header>": { "...": "..." }
  },
  "unique_content": {
    "<header>": { "...": "..." }
  },
  "summary": [
    {
      "path": "submenus.fitSubmenu2.sections.[0].horizontalImageItems",
      "tree": [
        { "featureID": "tactical2" }
      ],
      "count": 5,
      "headers": ["HeaderA", "HeaderB", "HeaderC", "HeaderD", "HeaderE"]
    }
  ],
  "key_label_summary": [
    {
      "key": "featureID",
      "total_count": 120,
      "header_count": 7,
      "headers": ["HeaderA", "HeaderB"]
    }
  ]
}
```

Field meanings:
- `text_diff[header]`: only value differences (`app` vs `master`) at leaf nodes.
- `missing_content[header]`: missing in app, present in master.
- `unique_content[header]`: present in app, absent in master.
- `summary`: repeated missing nodes aggregated by exact `path + tree`.
  - `count` = number of times that exact node appears in `missing_content`.
  - `headers` = which headers contained that missing node.
- `key_label_summary`: key label frequency during audit traversal.
  - `total_count` = total occurrences of the key label in diff traversal.
  - `header_count` = number of headers where that key label appeared in diff traversal.

## Console output
Audit console output includes:
- Per-header table:
  - `<master filename> | Text/Label Differences | Unique Data | Missing Data`
- Key label table:
  - `Key Label | Total Count | Header Count`

## Config
- Ignored keys:
  - `type`, `url`, `imageURL`, `analyticsName`, `appID`, `backgroundImageURL`, `textHex`
- Ignored header prefixes:
  - `30`, `demo`, `TEST`, `OLD`, `_`, `tier`

## Execution flow (Superblocks)
```python
app_id = Input220.value
mode = Dropdown130.selectedOptionValue
input_manifest_raw = parse_json_maybe_double_encoded(FilePicker1.files[0]["readContents"]())
master_raw = parse_json_maybe_double_encoded(FilePicker2.files[0]["readContents"]())

input_manifest = unwrap_manifest(input_manifest_raw)
master = unwrap_manifest(master_raw)
input_headers = select_app_headers(input_manifest)

if mode.lower() == "update":
    return update_file(master, input_manifest, input_headers)

audit_output, missing_counts, unique_counts, prefixes = build_audit(
    input_manifest, master, input_headers
)

if mode.lower() in ("audit", "audit-text", "audit-structure"):
    return audit_output

return merge_outputs(master, audit_output["unique_content"], prefixes, app_id)
```
