import copy
import json
import re
from typing import Any, Dict, List, Optional, Tuple

# Fields and headers to ignore during comparison
IGNORED_KEYS = {
    "type",
    "url",
    "imageURL",
    "analyticsName",
    "appID",
    "backgroundImageURL",
}
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
    if (
        isinstance(obj, dict)
        and "manifest" in obj
        and isinstance(obj["manifest"], dict)
    ):
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
        return any(lowered.startswith(prefix.lower()) for prefix in IGNORED_PREFIXES)

    return [k for k in manifest.keys() if not is_ignored(k)]


# ---------- Normalization / comparison ----------
def sanitize_string(value: str) -> str:
    return OCCV_APPS_RE.sub("/ocvapps/<APP>/", value)


def normalize(node: Any, path: List[str]) -> Tuple[str, Any]:
    if isinstance(node, dict):
        filtered = {k: v for k, v in node.items() if k not in IGNORED_KEYS}
        return (
            "dict",
            tuple(sorted((k, normalize(v, path + [k])) for k, v in filtered.items())),
        )
    if isinstance(node, list):
        norm_items = [normalize(i, path) for i in node]
        norm_sorted = tuple(
            sorted(norm_items, key=lambda x: json.dumps(x, sort_keys=True))
        )
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
        manifest = (
            input_manifest.get(header) if isinstance(input_manifest, dict) else None
        )
        master_root = master
        if compute_diffs:
            overlay_from_app = (
                diff(manifest, master_root, []) if manifest is not None else None
            )
            diff_app[header] = overlay_from_app if overlay_from_app is not None else {}
            diff_count_app[header] = (
                count_nodes(overlay_from_app) if overlay_from_app is not None else 0
            )

            if audit_mode:
                overlay_from_master = (
                    diff(master_root, manifest, []) if master_root is not None else None
                )
                diff_master[header] = (
                    overlay_from_master if overlay_from_master is not None else {}
                )
                diff_count_master[header] = (
                    count_nodes(overlay_from_master)
                    if overlay_from_master is not None
                    else 0
                )
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
        diff_overlay: Dict[str, Any] = {
            "diff_app": diff_app,
            "diff_master": diff_master,
        }
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



def _remove_list_items_in_place(current_list: List[Any], remove_list: List[Any], path: List[str]) -> None:
    """Treat lists as sets (order-insensitive). Remove any items from current_list that match items in remove_list."""
    if not current_list or not remove_list:
        return

    remove_norms = {normalize(item, path) for item in remove_list}
    kept: List[Any] = []
    for item in current_list:
        if normalize(item, path) in remove_norms:
            continue
        kept.append(item)

    current_list[:] = kept
def _matches_remove_pattern(target: Any, pattern: Any, path: List[str]) -> bool:
    """
    Returns True if 'target' matches 'pattern' as a subset, for the purpose of removal.
    - Dict: all keys in pattern must exist in target and match recursively.
    - List: we treat pattern list as "these items should be removed"; match means there is
      at least one overlapping element between target list and pattern list (set semantics).
    - Scalars: meaningfully_equal.
    """
    if pattern is None:
        return True

    if isinstance(pattern, dict):
        if not isinstance(target, dict):
            return False
        for k, p_val in pattern.items():
            if k not in target:
                return False
            if not _matches_remove_pattern(target[k], p_val, path + [k]):
                return False
        return True

    if isinstance(pattern, list):
        if not isinstance(target, list):
            return False
        if len(pattern) == 0:
            # empty list pattern means "delete the node" at dict-level, not list-level matching
            return True
        target_norms = {normalize(x, path) for x in target}
        pattern_norms = {normalize(x, path) for x in pattern}
        return len(target_norms.intersection(pattern_norms)) > 0

    # scalar
    return meaningfully_equal(target, pattern, path)

def _apply_remove_list_by_pattern(current_list: List[Any], remove_list: List[Any], path: List[str]) -> None:
    if not current_list or not remove_list:
        return

    # (1) Whole-element removals (exact match, set semantics)
    remove_norms = {normalize(x, path) for x in remove_list}
    kept: List[Any] = []
    for item in current_list:
        if normalize(item, path) in remove_norms:
            continue
        kept.append(item)
    current_list[:] = kept

    dict_patterns = [x for x in remove_list if isinstance(x, dict)]
    if not dict_patterns:
        return

    # (2) Partial-pattern recursion — but ONLY into items that match the pattern
    for pat in dict_patterns:
        if len(pat) == 0:
            continue

        for item in current_list:
            if not isinstance(item, dict):
                continue
            # ✅ gate: only apply if this list element actually matches the pattern (subset match)
            if _matches_remove_pattern(item, pat, path + ["<list-item>"]):
                _apply_remove(item, pat, path + ["<list-item>"])

    # (3) Cleanup empty dicts left behind
    current_list[:] = [x for x in current_list if not (isinstance(x, dict) and len(x) == 0)]



def _apply_remove(current: Any, remove_spec: Any, path: List[str]) -> Any:
    if remove_spec is None:
        return current

    # Root-level list removal (supported)
    if isinstance(current, list) and isinstance(remove_spec, list):
        _apply_remove_list_by_pattern(current, remove_spec, path)
        return current

    if isinstance(current, dict) and isinstance(remove_spec, dict):
        for k, r_val in remove_spec.items():
            if k not in current:
                continue

            c_val = current.get(k)

            if isinstance(r_val, dict):
                if len(r_val) == 0:
                    current.pop(k, None)
                    continue
                if isinstance(c_val, dict):
                    _apply_remove(c_val, r_val, path + [k])
                else:
                    current.pop(k, None)
                continue

            if isinstance(r_val, list):
                if len(r_val) == 0:
                    current.pop(k, None)
                    continue
                if isinstance(c_val, list):
                    _apply_remove_list_by_pattern(c_val, r_val, path + [k])
                else:
                    current.pop(k, None)
                continue

            current.pop(k, None)

        return current

    return current



def _strip_insert_index(node: Any) -> Any:
    """Deep-copy node but remove any 'insert_index' keys in dicts (any depth)."""
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k == "insert_index":
                continue
            out[k] = _strip_insert_index(v)
        return out
    if isinstance(node, list):
        return [_strip_insert_index(i) for i in node]
    return copy.deepcopy(node)


def _list_update_with_optional_insert_index(
    current_list: List[Any], update_list: List[Any], path: List[str]
) -> None:
    """
    Update semantics for lists:
      - Default: add missing items (dedupe by normalize()).
      - Special: dict items with {"insert_index": i, ...} PATCH the existing element at i
        by applying _apply_update into that element (merge within tree), not by inserting
        a new element. insert_index is removed from output.
    """
    if not update_list:
        return

    # Dedupe set for non-indexed adds (ignore insert_index)
    seen = {normalize(_strip_insert_index(item), path) for item in current_list}

    indexed_patches: List[Tuple[int, Any]] = []
    non_indexed: List[Any] = []

    for raw in update_list:
        if isinstance(raw, dict) and isinstance(raw.get("insert_index"), int):
            idx = int(raw["insert_index"])
            patch = _strip_insert_index(raw)
            indexed_patches.append((idx, patch))
        else:
            non_indexed.append(_strip_insert_index(raw))

    # Apply patches in ascending index order (doesn't matter much since we don't insert/shift)
    indexed_patches.sort(key=lambda t: t[0])

    for idx, patch in indexed_patches:
        if idx < 0:
            idx = 0

        # If target exists, PATCH it (merge within that tree)
        if idx < len(current_list):
            target = current_list[idx]

            # If both are dicts/lists, apply update recursively; otherwise don't override scalars.
            # (If target is scalar and patch is dict, we can't merge safely -> leave unchanged.)
            if isinstance(target, dict) and isinstance(patch, dict):
                _apply_update(target, patch, path + [f"[{idx}]"])
            elif isinstance(target, list) and isinstance(patch, list):
                # patch list into target list (add missing items)
                _list_update_with_optional_insert_index(target, patch, path + [f"[{idx}]"])
            else:
                # Type mismatch or scalar target: do not override
                pass

        else:
            # Out of range: create a new element (clamp-to-end behavior)
            current_list.append(copy.deepcopy(patch))
            # Update seen so later non-indexed dedupe works
            seen.add(normalize(patch, path))

    # Now handle non-indexed items as "add missing" (set semantics)
    for item in non_indexed:
        n = normalize(item, path)
        if n in seen:
            continue
        seen.add(n)
        current_list.append(copy.deepcopy(item))



def _apply_update(current: Any, update_spec: Any, path: List[str]) -> Any:
    """
    Apply master.update to a working node.
    Rule: follow each branch until a node does not exist in working; add that node and all its children.
    - Does NOT override existing scalar values.
    - Lists are treated as sets; ONLY add missing items.
    - List items may contain {"insert_index": N, ...} to control insertion position (0-indexed).
      insert_index is removed from output.
    """
    if update_spec is None:
        return current

    # Dict update
    if isinstance(update_spec, dict):
        if not isinstance(current, dict):
            return current

        for k, u_val in update_spec.items():
            if k not in current:
                # add entire subtree, but strip insert_index everywhere before storing
                current[k] = _strip_insert_index(u_val)
                continue

            c_val = current.get(k)

            if isinstance(u_val, dict) and isinstance(c_val, dict):
                _apply_update(c_val, u_val, path + [k])

            elif isinstance(u_val, list) and isinstance(c_val, list):
                # ✅ use insert-aware list updater here
                _list_update_with_optional_insert_index(c_val, u_val, path + [k])

            else:
                # scalar/type mismatch: do not override
                pass

        return current

    # Root list update
    if isinstance(update_spec, list) and isinstance(current, list):
        # ✅ use insert-aware list updater here too
        _list_update_with_optional_insert_index(current, update_spec, path)
        return current

    return current


def _select_instructions_for_header(
    master: Dict[str, Any], header: str
) -> Tuple[Optional[Any], Optional[Any]]:
    """
    Supports two master shapes:
      1) Per-header: master['remove'][header], master['update'][header]
      2) Global: master['remove'], master['update'] applied to all headers
    """
    remove_root = master.get("remove")
    update_root = master.get("update")

    remove_spec = None
    update_spec = None

    if isinstance(remove_root, dict) and header in remove_root:
        remove_spec = remove_root.get(header)
    else:
        remove_spec = remove_root

    if isinstance(update_root, dict) and header in update_root:
        update_spec = update_root.get(header)
    else:
        update_spec = update_root

    return remove_spec, update_spec


def update_file(
    master: Dict[str, Any],
    input_manifest: Dict[str, Any],
    input_headers: List[str],
    mode: str,
) -> Dict[str, Any]:
    # master here is already unwrapped (a "manifest" dict), same for input_manifest
    updated_manifest: Dict[str, Any] = {}

    for header in input_headers:
        working_header = input_manifest.get(header)
        if working_header is None:
            continue

        current = copy.deepcopy(working_header)

        remove_spec, update_spec = _select_instructions_for_header(master, header)

        # Apply removals first, then updates (per README)
        if remove_spec is not None:
            current = _apply_remove(current, remove_spec, [header])

        if update_spec is not None:
            current = _apply_update(current, update_spec, [header])

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
    lines.append("-App --| --- Unique --- | ---Missing---")
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

if mode_lower == "update":
    modified = update_file(master, input_manifest, input_headers, mode_lower)
    return modified

diff_overlay, diff_count_master, diff_count_app, prefixes = build_diff(
    input_manifest, master, input_headers, mode
)

write_console_log(input_headers, diff_count_app, diff_count_master)

if mode_lower == "audit":
    return diff_overlay

merged_output = merge_outputs(master, diff_overlay, input_headers, prefixes, app_id)

return merged_output
