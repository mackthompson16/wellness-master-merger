# Wellness Management Tool

Keep many app manifests consistent without hand-editing every file. This tool uses a **master manifest** as the source of truth to update, audit, and merge changes across all app headers.

---

## At a glance
- **Update**: Push additions and removals into working manifests.
- **Audit**: Report what is missing or extra compared to the master.
- **Merge**: Produce a clean manifest while preserving master values for conflicting leaf scalars.

---

## Inputs and outputs
- **Inputs**
  - Working manifest (the app set)
  - Master manifest (source of truth / instructions)
- **Outputs**
  - **Audit mode**: JSON diff report
  - **Merge / Update modes**: Updated manifest ready for use

---

## Modes

### Update
Use the master manifest as **instructions**.

- All additions live under `manifest.update`
- All removals live under `manifest.remove`
- **Order**:
  1. Apply `remove`
  2. Apply `update`

#### Remove semantics
- Walk each removal branch to the **deepest node** and delete only that node.
- Lists are treated as **sets** (order does not matter).
- If a list is expanded (i.e., contains items), only the explicitly listed items are removed.
- If the final node is an empty list ([]) or empty object ({}), the entire node at that path is deleted from the working manifest.

#### Update semantics
- Follow each update path until it no longer exists in the working header, then add the provided subtree.
- Existing scalar values are **not overridden**.
- Lists are treated as sets (no duplicates).
- **Special list patching**:
  - List items may include `"insert_index": N`
  - Instead of adding a new list element, the update is **patched into the existing element at index `N`**
  - `insert_index` is **not included** in the final output

---

### Audit
Produce a JSON report of differences per header:
- `diff_master`: items present in master but missing from the app
- `diff_app`: items present in the app but missing from master

Rules:
- Lists are compared as sets (order ignored)
- Ignored keys and headers are skipped
- Output is informational only (no data is modified)

---

### Merge
Create a single manifest by overlaying app differences onto master.

Rules:
- Missing nodes from the app are added
- If a leaf scalar (string/number) differs at the same path, **keep the master value**
- Lists are merged as sets (no ordering guarantees)
- Placeholders are replaced after merge:
  - `"ChangeMe"` → `app_id`
  - `"PATH"` → extracted header prefix

---

## When to use this
- You manage many manifests with shared structure
- You want one place to define updates and removals
- You need an objective audit of drift
- You want safe merges that preserve master intent

---

## Config

### Ignored keys
```python
IGNORED_KEYS = {
  "url", "imageURL", "analyticsName", "appID", "backgroundImageURL"
}
```
## JSON examples
### Update/Remove
```
// master (instructions)
{ "manifest": {
  "remove": { "headerA": { "features": { "oldFeature": {} } } },
  "update": { "headerA": { "features": { "newFeature": { "text": "Hello" } } } }
} }
```

### Merge preference for master leaf
```
// master
{ "manifest": { "headerA": { "features": { "welcome": { "text": "From master" } } } } }

// diff_app
{ "headerA": { "features": {
  "welcome": { "text": "From app" },
  "extra": { "text": "New" }
} } }

// merged
{ "manifest": { "headerA": { "features": {
  "welcome": { "text": "From master" },
  "extra": { "text": "New" }
} } } }
```

## Function definitions

### `build_diff(input_manifest, master, input_headers, mode)`
**Inputs**
- `input_manifest`: working manifest JSON
- `master`: master manifest JSON
- `input_headers`: list of headers to process
- `mode`: `"audit"` or `"merge"`

**Behavior**
- Traverse master and working headers (skip ignored keys).
- Lists are treated as sets (order ignored).
- **Audit mode**:
  - Record items missing from app → `diff_master`
  - Record items extra in app → `diff_app`
- **Merge mode**:
  - Record only app → master differences (`diff_app`)
- Count changed nodes per header.
- Extract analytics prefix per header.

**Returns**
- `diff_overlay`
- `diff_count_master`
- `diff_count_app`
- `prefixes`

---

### `Extract_Prefix(header, input_manifest)`
**Behavior**
- Scan `header.features.(feature).analyticsName`
- Skip `openSettings`
- Expect pipe format: `"A|B|C"`
- Store prefix `"A|B"`

**Returns**
- `{ header: prefix }`

---

### `update(master, input_manifest)`
**Behavior (per header)**
1. **Apply `master.remove`**
   - Walk to deepest node and delete only that node.
   - Lists matched by existence (set semantics, order ignored).
   - Partial patterns apply only to matching list elements.
2. **Apply `master.update`**
   - Follow path until missing, then add subtree.
   - Existing scalar values are not overridden.
   - Lists dedupe items.
   - List items may include `"insert_index": N` to patch the existing element at index `N`
     (no new element added; `insert_index` removed from output).

**Returns**
- Updated manifest JSON

---

### `merge_outputs(master, overlays, input_headers, prefixes, app_id)`
**Inputs**
- `master`: base manifest
- `overlays`: diff content to apply
- `input_headers`: headers to process
- `prefixes`: extracted analytics prefixes
- `app_id`: app identifier for placeholder replacement

**Behavior**
- Start with `{ "manifest": {} }`
- For each header:
  - Merge overlay into master
  - If leaf scalar differs, keep master value
  - Lists are merged as sets
- Replace placeholders:
  - `"ChangeMe"` → `app_id`
  - `"PATH"` → `prefixes[header]`

**Returns**
- Merged manifest JSON

---

### `write_console_log(input_headers, diff_count_app, diff_count_master)`
**Behavior**
- Print per-header counts of:
  - App-only differences
  - Master-only differences
- Print totals across all headers

