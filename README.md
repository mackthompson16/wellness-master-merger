# Wellness Management Tool

Keep many app manifests consistent without hand-editing every file. This tool uses a master manifest as the source of truth to update, audit, and merge changes across all app headers.

At a glance:
- Update: push additions/changes/removals to all working manifests.
- Audit: identify what is missing or outdated compared to the master.
- Merge: consolidate audit differences into a clean output while keeping master values for conflicting leaf scalars.

## Inputs and outputs
- Inputs: a working manifest (the app set) and a master manifest (the source of truth).
- Output (Audit mode): a JSON report of differences.
- Output (Merge/Update modes): a merged manifest ready to use.

## Modes
- Update: Use the master manifest to stage changes for the working manifest. All additions/changes live under an `update` section (path -> payload). Removals live under a `remove` section. For each removal branch, walk to the deepest node and delete that final node from the working header if it exists. Apply removals before updates. The master can carry new data with explicit paths; follow the path until it no longer exists in the working header, then append the data at that point.
- Audit: Produce JSON that reports differences between master and each working header: items present only in master (`diff_master`) and items present only in the working header (`diff_app`).
- Merge: Produce one JSON by overlaying `diff_app` content onto master for every header. If the final leaf nodes match in path but the scalar value (string/number) differs, always keep the master value. Do not apply this override to lists; lists are treated as sets and order is ignored.

## When to use this
- You manage many manifests that should follow the same structure and content.
- You want one place to define updates and removals.
- You need an objective audit report for missing or outdated content.
- You want to merge differences while preserving master as the source of truth.

## Config
- Ignored keys: `IGNORED_KEYS = {"url", "imageURL", "analyticsName", "appID", "backgroundImageURL"}`
- Ignored header prefixes: `IGNORED_PREFIXES = {"30", "demo", "TEST", "OLD", "_", "tier"}`

## JSON examples
- Update/remove:
```json
// master (instructions)
{
  "manifest": {
    "update": {
      "headerA": { "features": { "newFeature": { "text": "Hello" ,} } }
    },
    "remove": {
      "headerA": { "features": { "oldFeature": {} } }
    }
  }
}

// input (working)
{
  "manifest": {
    "headerA": { "features": { "oldFeature": { "text": "Deprecated" } } }
  }
}

// output (update mode)
{
  "manifest": {
    "headerA": { "features": { "newFeature": { "text": "Hello" } } }
  }
}
```
- Merge preference for master leaf:
```json
// master
{ "manifest": { "headerA": { "features": { "welcome": { "text": "From master" } } } } }
// overlay (diff_app)
{ "headerA": { "features": { "welcome": { "text": "From app" }, "extra": { "text": "New" } } } }
// merged
{ "manifest": { "headerA": { "features": { "welcome": { "text": "From master" }, "extra": { "text": "New" } } } } }
```

# Implement these functions

### build_diff(input_manifest, master, input_headers, mode):
    input_manifest = working JSON
    master = master JSON
    input_headers = working headers

    for every header:
        if mode is audit, produce two diffs: master -> app and app -> master
            {
                "diff_app": { header_0:{}, ...., header_n:{} },
                "diff_master": { header_0:{}, ...., header_n:{} },
            }
        if mode is merge, only produce app -> master
            { header_0:{}, ...., header_n:{} }

        traverse master and header linearly (skip IGNORED_KEYS).

        At each node:
            If the node is a list, treat it as a set; order does not matter.
            If both master and header have the node, continue.
            If mode is audit and master has a node missing in header:
                add 1 to diff_count_master
                record the path/data in diff_master for that header
            If header has a node missing in master:
                add 1 to diff_count_app
                record the path/data in diff_app for that header

        Extract_Prefix(header, input_manifest)

    return diff_overlay, diff_count_master, diff_app, diff_count_app, prefixes

### Extract_Prefix(header, input_manifest):
- Find the first analytics name at: header.features.(feature).analyticsName that is not "openSettings" (it will be the second value).
- When you find something like: "analyticsName": "IN|lawrenceCountyIN|resilienceReps"
    store: prefixes[header] = "IN|lawrenceCountyIN"
- Assume they all follow the format of 3 pipe-separated parts.
- return prefixes

### update(master, input_manifest):
    For every header in the input:
        Apply master.remove (if it exists):
            for every branch in remove, walk down to the deepest node and delete that final node from the working header if present.
        Apply master.update (add/change content):
            follow the specified path until it no longer exists in the working header, then append the provided data at that point.

### merge_outputs(master, overlays, input_headers, prefixes, app_id)
    master = base file
    overlays = content to append
    app_id = used for changing some data
    input_headers = working headers

    start output:
    {
        "manifest": {}
    }

    for every header in input_headers:
        add manifest.header to the output.
        add master to manifest.header

        perform the merge:
            traverse overlays alongside manifest.header
            when a node in overlays is missing from master, append it to manifest.header
            if you reach the end of a diff path and only the final leaf value differs, keep the master scalar value (not applied to lists)
            continue until all overlay content is merged into the correct path in the output

        replace placeholders:
            find-and-replace-all("ChangeMe", appID)
            find-and-replace-all("PATH", prefixes[Header])

    return output_json


### write_console_log(input_headers, diff_count_app, diff_count_master)
    print the number of diffs found for each header

## Practical workflow (high level)
1) Keep your master manifests updated.
2) Run Audit to see what is missing or outdated.
3) Run Merge to produce a clean output that preserves master values for conflicting leaf scalars.
4) Run Update to push new content or removals across all working manifests.

## Execution flow (already valid superblocks):
app_id = Input220.value
mode = Dropdown130.selectedOptionValue
print("Mode: ", mode)
input_manifest_raw = parse_json_maybe_double_encoded(
    FilePicker1.files[0]["readContents"]()
)

master_raw = parse_json_maybe_double_encoded(FilePicker2.files[0]["readContents"]())
input_manifest = unwrap_manifest(input_manifest_raw)
master = unwrap_manifest(master_raw)

input_headers = select_app_headers(input_manifest)

diff_overlay, diff_count_master, diff_count_app, prefixes = build_diff(
    input_manifest, master, input_headers, mode
)

write_console_log(input_headers, diff_count_app, diff_count_master)

if mode == "Audit":
    return diff_overlay
else:
    return merge_outputs(master, diff_overlay, input_headers, prefixes, app_id)
