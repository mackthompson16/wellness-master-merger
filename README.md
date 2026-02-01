# SCRIPT MERGE AND SPLIT TOOL

This script manages the manifests of input jsons, optionally using a master to track changes.

In add mode:
-  iterate through all of the working headers and add the corresponding features that exist in the input

In change mode:
- Override the feature level nodes (featureHub/feature) of each of the header trees.

In audit mode produce sections in the output:
-  diff_master: the items in master that are not in the working header tree
-  diff_app: the items in the working header tree that are not in master

In merge mode produce merged_manifest:
  the combination of the master and each header tree,
  achieved by overlaying the diff_app content onto master.

## Config:
- Ignore these fields:
    IGNORED_KEYS = {"url", "imageURL", "analyticsName", "appID", "backgroundImageURL"}
- Ignore these headers:
    IGNORED_PREFIXES = {"30", "demo", "TEST", "OLD", "_", "tier"}

# Implement these functions

### build_diff(input_manifest, master, input_headers):
    input_manifest = working JSON
    master = find diff from this file
    input_headers = working headers

    for every header:

        if mode is audit, produce two diffs: one from master->app and app->master
        thus, follow the schematic:
        {
            "diff_app" : { header_0:{},....header_n:{}},
            "diff_master" : { header_0:{},....header_n:{}},
        }
        if mode is merge, just produce app -> master
        {
            header_0:{},....header_n:{},
        }

        we have two roots to traverse: master and header.
        linearly traverse the entire tree (skipping IGNORED_KEYS).


        At each Node:
            If the node is a list, treat as a set; order does not matter.
            If both the master and header have the node, continue
            If mode is audit:
                If master contains a node that is not in the header tree:
                    add 1 to diff_count_master
                    add the path and data to "diff_master"{"header":{}}
            If the header tree contains a node that is not in master:
                add 1 to diff_count_app
                add the path to the correct path of our output data

        Extract_Prefix(header, input_manifest)

    return diff_overlay, diff_count_master, diff_app, diff_count_app, prefixes

### Extract_Prefix(header, input_manifest):
- this is used to find the correct analytics name to track.
- take the first value located at: header.features.(feature).analyticsName
    that is not "openSettings" (it will be the second value)
    if we find: "analyticsName": "IN|lawrenceCountyIN|resilienceReps"
    store it as:
    prefixes[header] = "IN|lawrenceCountyIN"
    we can assume they will all follow that format of 3 parts with "|".
    return prefixes

### add_to_file(master, input_manifest):
    for every header in input manifest:
        If add: 
            traverse the input headers, down the path that the master follows.
            find the first node in master that doesnt exist in this header, and add the corresponding contents here. 
        If override:
            traverse the input headers, down to the feature level contents. replace the contents with whatever master has (if it doesn't exist, add it). 
            e.g. replace working_header.features.(feature)  withmaster.features.(feature name)
    return modified JSON


### merge_outputs(master, overlays, input_headers, prefixes, app_id)
    master = base file
    overlays = content we need to append
    app_id = used for changing some data
    input_headers = working header

    start output:
    {
    "manifest": 
        {
        }
    }


    for every header in input_headers:
        add manifest.header to the output.
        add master to manifest.header

        now we have to do the merge part.
        traverse linearly through overlays, follow along with the manifest.header
        when we reach the depth where this node does not exist in the master, append it.
        Continue until all of the content on the overlay is added into the correct path in the output

        This last part is like a ctrl-f replace: 
            find-and-replace-all("ChangeMe", appID)
            find-and-replace-all("PATH", prefixes[Header])
    
    return output_json


### write_console_log()

    show the number of diffs found for each header, just print in console


## Follow this execution (already valid superblocks):

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
