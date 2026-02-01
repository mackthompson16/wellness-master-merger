import copy
import json
import re
from typing import Any, Dict, List, Optional, Tuple

# Fields and headers to ignore during comparison
IGNORED_KEYS = {"url", "imageURL", "analyticsName", "appID", "backgroundImageURL"}
IGNORED_PREFIXES = {"30", "demo", "TEST", "OLD", "_", "tier"}
OCCV_APPS_RE = re.compile(r"/ocvapps/[^/]+/", re.IGNORECASE)


# ---------- JSON helpers ----------
def parse_json_maybe_double_encoded(raw: str) -> Any:
    """
    Handles normal JSON and double-encoded JSON strings.
    Returns a dict/list object.
    """
    obj = json.loads(raw)
    while isinstance(obj, str):
        obj = json.loads(obj)
    return obj


def unwrap_manifest(obj: Any) -> Any:
    if isinstance(obj, dict) and "manifest" in obj and isinstance(obj["manifest"], dict):
        return obj["manifest"]
    return obj


def resolve_path(value: Any) -> Optional[str]:
    if not value:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("file", "path", "name"):
            if key in value and isinstance(value[key], str):
                return value[key]
    return None


def select_app_headers(manifest: Dict[str, Any]) -> List[str]:
    def is_ignored(name: str) -> bool:
        lowered = name.lower()
        return any(
            lowered.startswith(prefix.lower())
            for prefix in IGNORED_PREFIXES
        )

    return [k for k in manifest.keys() if not is_ignored(k)]


# ---------- Normalization / comparison ----------
def sanitize_string(value: str) -> str:
    return OCCV_APPS_RE.sub("/ocvapps/<APP>/", value)


def normalize(node: Any, path: List[str]) -> Tuple[str, Any]:
    if isinstance(node, dict):
        filtered = {k: v for k, v in node.items() if k not in IGNORED_KEYS}
        return (
            "dict",
            tuple(
                sorted((k, normalize(v, path + [k])) for k, v in filtered.items())
            ),
        )
    if isinstance(node, list):
        norm_items = [normalize(i, path) for i in node]
        norm_sorted = tuple(sorted(norm_items, key=lambda x: json.dumps(x, sort_keys=True)))
        return ("list", norm_sorted)
    if isinstance(node, str):
        return ("scalar", sanitize_string(node))
    return ("scalar", node)


def meaningfully_equal(a: Any, b: Any, path: List[str]) -> bool:
    return normalize(a, path) == normalize(b, path)


# ---------- Diff (split) ----------
def _dedup_items(items: List[Any], existing_norms: set, path: List[str]) -> List[Any]:
    uniques: List[Any] = []
    seen = set(existing_norms)
    for item in items:
        norm_item = normalize(item, path)
        if norm_item in seen:
            continue
        seen.add(norm_item)
        uniques.append(copy.deepcopy(item))
    return uniques


def _list_key(item: Any) -> Any:
    if isinstance(item, dict):
        if "featureID" in item:
            return ("featureID", item.get("featureID"))
        item_type = item.get("type")
        payload = item.get("payload")
        if item_type and isinstance(payload, dict) and "headerText" in payload:
            return ("type_header", item_type, payload.get("headerText"))
        if item_type:
            return ("type", item_type)
    try:
        return ("json", json.dumps(item, sort_keys=True))
    except TypeError:
        return ("repr", repr(item))


def diff(current: Any, master: Any, path: List[str]) -> Optional[Any]:
    """
    Traverse top-down; include parent chain only when a leaf differs.
    Lists are compared as sets of normalized items (order-insensitive) and
    deduplicated to avoid emitting duplicates in overlays.
    """
    if master is None:
        if isinstance(current, list):
            return _dedup_items(current, set(), path) or None
        if isinstance(current, dict):
            return {
                k: diff(v, None, path + [k])
                for k, v in current.items()
                if k not in IGNORED_KEYS
            }
        return copy.deepcopy(current)

    if isinstance(current, dict) and isinstance(master, dict):
        filtered = {k: v for k, v in current.items() if k not in IGNORED_KEYS}
        result: Dict[str, Any] = {}
        changed = False
        for k, v in filtered.items():
            m_val = master.get(k)
            child = diff(v, m_val, path + [k])
            if child is not None:
                changed = True
                result[k] = child
        return result if changed else None

    if isinstance(current, list) and isinstance(master, list):
        master_map = {}
        for m_item in master:
            key = _list_key(m_item)
            master_map.setdefault(key, []).append(m_item)

        result_list: List[Any] = []
        changed = False

        for c_item in current:
            key = _list_key(c_item)
            candidates = master_map.get(key)
            if candidates:
                m_item = candidates.pop(0)
                if not candidates:
                    master_map.pop(key, None)
                delta = diff(c_item, m_item, path + [str(key)])
                if delta is not None:
                    changed = True
                    result_list.append(delta)
            else:
                changed = True
                result_list.append(copy.deepcopy(c_item))

        return result_list if changed else None

    return None if meaningfully_equal(current, master, path) else copy.deepcopy(current)


def count_nodes(node: Any) -> int:
    if isinstance(node, dict):
        return len(node) + sum(count_nodes(v) for v in node.values())
    if isinstance(node, list):
        return len(node) + sum(count_nodes(i) for i in node)
    return 1


def extract_prefix(header: str, manifest: Dict[str, Any]) -> Optional[str]:
    features = manifest.get("features", {})
    for f_key in sorted(features):
        if f_key == "openSettings":
            continue
        node = features.get(f_key)
        if isinstance(node, dict):
            analytics = node.get("analyticsName")
            if (
                isinstance(analytics, str)
                and analytics
                and not analytics.endswith("|openSettings")
            ):
                parts = analytics.split("|")
                if len(parts) >= 2:
                    return "|".join(parts[:2])
                return analytics
    return None


def build_diff(
    input_manifest: Dict[str, Any],
    master: Dict[str, Any],
    input_headers: List[str],
    mode: str,
) -> Tuple[Dict[str, Any], Dict[str, int], Dict[str, int], Dict[str, str]]:
    diff_master: Dict[str, Any] = {}
    diff_count_master: Dict[str, int] = {}
    diff_app: Dict[str, Any] = {}
    diff_count_app: Dict[str, int] = {}
    prefixes: Dict[str, str] = {}
    mode_lower = mode.lower()
    audit_mode = mode_lower == "audit"
    compute_diffs = audit_mode or mode_lower == "merge"

    for header in input_headers:
        manifest = input_manifest.get(header) if isinstance(input_manifest, dict) else None
        master_root = master
        if compute_diffs:
            overlay_from_app = diff(manifest, master_root, []) if manifest is not None else None
            diff_app[header] = overlay_from_app if overlay_from_app is not None else {}
            diff_count_app[header] = count_nodes(overlay_from_app) if overlay_from_app is not None else 0

            if audit_mode:
                overlay_from_master = diff(master_root, manifest, []) if master_root is not None else None
                diff_master[header] = overlay_from_master if overlay_from_master is not None else {}
                diff_count_master[header] = count_nodes(overlay_from_master) if overlay_from_master is not None else 0
            else:
                diff_count_master[header] = 0
        else:
            diff_count_app[header] = 0
            diff_count_master[header] = 0

        if manifest is not None:
            prefix = extract_prefix(header, manifest)
            if prefix:
                prefixes[header] = prefix

    if audit_mode:
        diff_overlay: Dict[str, Any] = {"diff_app": diff_app, "diff_master": diff_master}
    elif compute_diffs:
        diff_overlay = diff_app
    else:
        diff_overlay = {}

    return diff_overlay, diff_count_master, diff_count_app, prefixes


# ---------- Merge ----------
def merge_overlay_into_master(master: Any, overlay: Any, path: List[str]) -> Any:
    if overlay is None:
        return copy.deepcopy(master)

    if isinstance(master, dict) and isinstance(overlay, dict):
        result = copy.deepcopy(master)
        for k, o_val in overlay.items():
            if k in result:
                result[k] = merge_overlay_into_master(result[k], o_val, path + [k])
            else:
                result[k] = copy.deepcopy(o_val)
        return result

    if isinstance(master, list) and isinstance(overlay, list):
        result = copy.deepcopy(master)
        seen_norms = {normalize(m, path) for m in result}
        for o in overlay:
            norm_o = normalize(o, path)
            if norm_o in seen_norms:
                continue
            seen_norms.add(norm_o)
            result.append(copy.deepcopy(o))
        return result

    return copy.deepcopy(overlay)


def _replace_placeholders(node: Any, app_id: str, prefix: Optional[str]) -> Any:
    if isinstance(node, dict):
        return {k: _replace_placeholders(v, app_id, prefix) for k, v in node.items()}
    if isinstance(node, list):
        return [_replace_placeholders(i, app_id, prefix) for i in node]
    if isinstance(node, str):
        replaced = node.replace("ChangeMe", app_id)
        if prefix:
            replaced = replaced.replace("PATH", prefix)
        if app_id:
            replaced = OCCV_APPS_RE.sub(f"/ocvapps/{app_id}/", replaced)
        return replaced
    return node


def merge_outputs(
    master: Dict[str, Any],
    overlays: Dict[str, Any],
    input_headers: List[str],
    prefixes: Dict[str, str],
    app_id: str,
) -> Dict[str, Any]:
    merged = {"manifest": {}}
    for header in input_headers:
        overlay = overlays.get(header)
        base = master.get(header) if isinstance(master, dict) else {}
        compiled = merge_overlay_into_master(base, overlay, [])
        prefix = prefixes.get(header)
        merged["manifest"][header] = _replace_placeholders(compiled, app_id, prefix)
    return merged


# ---------- Add / Override ----------
def _add_missing_from_master(master_node: Any, working_node: Any, path: List[str]) -> Any:
    if master_node is None:
        return copy.deepcopy(working_node)

    if working_node is None:
        return copy.deepcopy(master_node)

    if isinstance(master_node, dict) and isinstance(working_node, dict):
        result = copy.deepcopy(working_node)
        for k, m_val in master_node.items():
            if k not in result:
                result[k] = copy.deepcopy(m_val)
            else:
                result[k] = _add_missing_from_master(m_val, result[k], path + [k])
        return result

    if isinstance(master_node, list) and isinstance(working_node, list):
        result = copy.deepcopy(working_node)
        seen_norms = {normalize(item, path) for item in result}
        for m_item in master_node:
            norm = normalize(m_item, path)
            if norm in seen_norms:
                continue
            seen_norms.add(norm)
            result.append(copy.deepcopy(m_item))
        return result

    # If types differ or are scalar, keep the working value (add does not override).
    return copy.deepcopy(working_node)


def add_to_file(
    master: Dict[str, Any],
    input_manifest: Dict[str, Any],
    input_headers: List[str],
    mode: str,
) -> Dict[str, Any]:
    mode_lower = mode.lower()
    updated_manifest: Dict[str, Any] = {}

    for header, working_header in input_manifest.items():
        if header not in input_headers:
            updated_manifest[header] = copy.deepcopy(working_header)
            continue

        master_header = master.get(header)
        current = copy.deepcopy(working_header)

        if mode_lower == "add" and master_header is not None:
            current = _add_missing_from_master(master_header, current, [])
        elif mode_lower in {"change", "override"} and isinstance(master_header, dict):
            master_features = master_header.get("features", {})
            if isinstance(master_features, dict):
                working_features = current.get("features")
                if not isinstance(working_features, dict):
                    working_features = {}
                else:
                    working_features = copy.deepcopy(working_features)
                for feat_name, feat_node in master_features.items():
                    working_features[feat_name] = copy.deepcopy(feat_node)
                current["features"] = working_features

        updated_manifest[header] = current

    return {"manifest": updated_manifest}


# ---------- Logging ----------
def write_console_log(
    input_headers: List[str],
    diff_count_app: Dict[str, int],
    diff_count_master: Dict[str, int],
) -> None:
    total_app = sum(diff_count_app.values())
    total_master = sum(diff_count_master.values())
    lines = []
    lines.append(f"Headers processed: {len(input_headers)}")
    lines.append(f"Total unique (app->master): {total_app}")
    lines.append(f"Total missing (master->app): {total_master}")
    lines.append("")
    lines.append("Header | Unique->Master | Missing->App")
    lines.append("------ | -------------- | -------------")
    sorted_headers = sorted(
        input_headers,
        key=lambda h: diff_count_app.get(h, 0) + diff_count_master.get(h, 0),
        reverse=True,
    )
    for header in sorted_headers:
        app_count = diff_count_app.get(header, 0)
        master_count = diff_count_master.get(header, 0)
        lines.append(f"{header} | {app_count} | {master_count}")
    print("\n".join(lines))


# ---------- Superblocks pipeline (no main) ----------
app_id = Input220.value

input_manifest_raw = parse_json_maybe_double_encoded(
    FilePicker1.files[0]["readContents"]()
)

mode = Dropdown130.selectedOptionValue
print("Mode: ", mode)

master_raw = parse_json_maybe_double_encoded(FilePicker2.files[0]["readContents"]())
input_manifest = unwrap_manifest(input_manifest_raw)
master = unwrap_manifest(master_raw)

input_headers = select_app_headers(input_manifest)
mode_lower = mode.lower()

if mode_lower in {"add", "change", "override"}:
    modified = add_to_file(master, input_manifest, input_headers, mode_lower)
    return modified

diff_overlay, diff_count_master, diff_count_app, prefixes = build_diff(
    input_manifest, master, input_headers, mode
)

write_console_log(input_headers, diff_count_app, diff_count_master)

if mode_lower == "audit":
    print("Mode: Audit")
    return diff_overlay

print("Mode: Merge")
merged_output = merge_outputs(
    master, diff_overlay, input_headers, prefixes, app_id
)

return merged_output
