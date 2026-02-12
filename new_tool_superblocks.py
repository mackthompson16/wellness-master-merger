import copy
import json
import re
import os
from typing import Any, Dict, List, Optional, Tuple

# ---------- Shared constants ----------
IGNORED_KEYS = {
    "type",
    "url",
    "imageURL",
    "analyticsName",
    "appID",
    "backgroundImageURL",
    "textHex",
}
IGNORED_PREFIXES = {"30", "demo", "TEST", "OLD", "_", "tier"}
OCCV_APPS_RE = re.compile(r"/ocvapps/[^/]+/", re.IGNORECASE)


# ---------- Utils ----------
def parse_json_maybe_double_encoded(raw: str) -> Any:
    obj = json.loads(raw)
    while isinstance(obj, str):
        obj = json.loads(obj)
    return obj


def unwrap_manifest(obj: Any) -> Any:
    if isinstance(obj, dict) and "manifest" in obj and isinstance(obj["manifest"], dict):
        return obj["manifest"]
    return obj


def select_app_headers(manifest: Dict[str, Any]) -> List[str]:
    def is_ignored(name: str) -> bool:
        lowered = name.lower()
        return any(lowered.startswith(prefix.lower()) for prefix in IGNORED_PREFIXES)

    return [k for k in manifest.keys() if not is_ignored(k)]


def extract_master_label(file_entry: Any) -> str:
    if isinstance(file_entry, dict):
        for key in ("name", "path", "file"):
            value = file_entry.get(key)
            if isinstance(value, str) and value.strip():
                return os.path.basename(value)
    return "featureHubs"


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
        norm_sorted = tuple(sorted(norm_items, key=lambda x: json.dumps(x, sort_keys=True)))
        return ("list", norm_sorted)
    if isinstance(node, str):
        return ("scalar", sanitize_string(node))
    return ("scalar", node)


def meaningfully_equal(a: Any, b: Any, path: List[str]) -> bool:
    return normalize(a, path) == normalize(b, path)


def list_key(item: Any) -> Any:
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
            if isinstance(analytics, str) and analytics and not analytics.endswith("|openSettings"):
                parts = analytics.split("|")
                if len(parts) >= 2:
                    return "|".join(parts[:2])
                return analytics
    return None


def stringify_leaf(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True)


def deep_copy(value: Any) -> Any:
    return copy.deepcopy(value)


def collect_leaf_values(node: Any) -> set:
    leaves = set()

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for k, v in value.items():
                if k in IGNORED_KEYS:
                    continue
                walk(v)
            return
        if isinstance(value, list):
            for item in value:
                walk(item)
            return
        leaves.add(stringify_leaf(value))

    walk(node)
    return leaves


def prune_missing_to_absent_values(node: Any, app_leaf_values: set) -> Optional[Any]:
    if isinstance(node, dict):
        out: Dict[str, Any] = {}
        for k, v in node.items():
            if k in IGNORED_KEYS:
                continue
            pruned = prune_missing_to_absent_values(v, app_leaf_values)
            if pruned is not None:
                out[k] = pruned
        return out if out else None

    if isinstance(node, list):
        out_list: List[Any] = []
        for item in node:
            pruned = prune_missing_to_absent_values(item, app_leaf_values)
            if pruned is not None:
                out_list.append(pruned)
        return out_list if out_list else None

    return node if stringify_leaf(node) not in app_leaf_values else None


def _json_key(value: Any) -> str:
    return json.dumps(value, sort_keys=True)


def _collect_missing_nodes(node: Any, path: List[str], out: List[Tuple[str, Any]]) -> None:
    """
    Collect missing nodes at their current path without recursively exploding
    each subtree into nested summary entries.
    """
    if isinstance(node, dict):
        for k, v in node.items():
            next_path = path + [k]
            out.append((".".join(next_path), v))
        return

    if isinstance(node, list):
        for idx, item in enumerate(node):
            next_path = path + [f"[{idx}]"]
            out.append((".".join(next_path), item))
        return

    out.append((".".join(path), node))


def build_missing_summary(missing_content: Dict[str, Any]) -> List[Dict[str, Any]]:
    aggregate: Dict[str, Dict[str, Any]] = {}

    for header, header_missing in missing_content.items():
        if not header_missing:
            continue

        nodes: List[Tuple[str, Any]] = []
        _collect_missing_nodes(header_missing, [], nodes)

        for path_str, tree in nodes:
            sig = f"{path_str}|{_json_key(tree)}"
            if sig not in aggregate:
                aggregate[sig] = {
                    "path": path_str,
                    "tree": tree,
                    "count": 0,
                    "headers": set(),
                }
            aggregate[sig]["count"] += 1
            aggregate[sig]["headers"].add(header)

    summary: List[Dict[str, Any]] = []
    for row in aggregate.values():
        headers = sorted(row["headers"])
        summary.append(
            {
                "path": row["path"],
                "tree": row["tree"],
                "count": row["count"],
                "headers": headers,
            }
        )

    summary.sort(key=lambda x: (x["count"], x["path"]), reverse=True)
    return summary


def _increment_key_label(
    key_label_counts: Dict[str, Dict[str, Any]], key: str, header: str
) -> None:
    if key not in key_label_counts:
        key_label_counts[key] = {"key": key, "count": 0, "headers": set()}
    key_label_counts[key]["count"] += 1
    key_label_counts[key]["headers"].add(header)


def _increment_key_labels_from_tree(
    node: Any, key_label_counts: Dict[str, Dict[str, Any]], header: str
) -> None:
    if isinstance(node, dict):
        for k, v in node.items():
            _increment_key_label(key_label_counts, k, header)
            _increment_key_labels_from_tree(v, key_label_counts, header)
        return
    if isinstance(node, list):
        for item in node:
            _increment_key_labels_from_tree(item, key_label_counts, header)


# ---------- Audit ----------
def _append_child(container: Dict[str, Any], key: str, value: Any) -> None:
    if value is None:
        return
    container[key] = value


def _pair_list_items(current: List[Any], master: List[Any]) -> Tuple[List[Tuple[Any, Any]], List[Any], List[Any]]:
    master_map: Dict[Any, List[Any]] = {}
    for m_item in master:
        key = list_key(m_item)
        master_map.setdefault(key, []).append(m_item)

    matched: List[Tuple[Any, Any]] = []
    unique_items: List[Any] = []
    for c_item in current:
        key = list_key(c_item)
        candidates = master_map.get(key)
        if candidates:
            matched.append((c_item, candidates.pop(0)))
            if not candidates:
                master_map.pop(key, None)
        else:
            unique_items.append(deep_copy(c_item))

    missing_items: List[Any] = []
    for leftovers in master_map.values():
        for item in leftovers:
            missing_items.append(deep_copy(item))

    return matched, unique_items, missing_items


def _audit_node(
    current: Any,
    master: Any,
    path: List[str],
    app_leaf_values: set,
    key_label_counts: Dict[str, Dict[str, Any]],
    header: str,
) -> Tuple[Optional[Any], Optional[Any], Optional[Any]]:
    if master is None:
        _increment_key_labels_from_tree(current, key_label_counts, header)
        return None, None, deep_copy(current)
    if current is None:
        pruned = prune_missing_to_absent_values(master, app_leaf_values)
        if pruned is not None:
            _increment_key_labels_from_tree(pruned, key_label_counts, header)
        return None, pruned, None

    if isinstance(current, dict) and isinstance(master, dict):
        text_diff: Dict[str, Any] = {}
        missing_content: Dict[str, Any] = {}
        unique_content: Dict[str, Any] = {}

        keys = set(current.keys()) | set(master.keys())
        for key in sorted(keys):
            if key in IGNORED_KEYS:
                continue
            c_has = key in current
            m_has = key in master
            if not c_has and m_has:
                pruned_missing = prune_missing_to_absent_values(master[key], app_leaf_values)
                if pruned_missing is not None:
                    missing_content[key] = pruned_missing
                    _increment_key_label(key_label_counts, key, header)
                    _increment_key_labels_from_tree(pruned_missing, key_label_counts, header)
                continue
            if c_has and not m_has:
                unique_content[key] = deep_copy(current[key])
                _increment_key_label(key_label_counts, key, header)
                _increment_key_labels_from_tree(current[key], key_label_counts, header)
                continue

            t_child, m_child, u_child = _audit_node(
                current[key],
                master[key],
                path + [key],
                app_leaf_values,
                key_label_counts,
                header,
            )
            _append_child(text_diff, key, t_child)
            _append_child(missing_content, key, m_child)
            _append_child(unique_content, key, u_child)
            if t_child is not None or m_child is not None or u_child is not None:
                _increment_key_label(key_label_counts, key, header)

        return (
            text_diff if text_diff else None,
            missing_content if missing_content else None,
            unique_content if unique_content else None,
        )

    if isinstance(current, list) and isinstance(master, list):
        matched, unique_items, missing_items = _pair_list_items(current, master)
        list_text_diff: List[Any] = []
        for c_item, m_item in matched:
            t_child, _, _ = _audit_node(
                c_item,
                m_item,
                path + ["<list-item>"],
                app_leaf_values,
                key_label_counts,
                header,
            )
            if t_child is not None:
                list_text_diff.append(t_child)

        pruned_missing_items: List[Any] = []
        for m_item in missing_items:
            pruned = prune_missing_to_absent_values(m_item, app_leaf_values)
            if pruned is not None:
                pruned_missing_items.append(pruned)

        return (
            list_text_diff if list_text_diff else None,
            pruned_missing_items if pruned_missing_items else None,
            unique_items if unique_items else None,
        )

    if isinstance(current, (dict, list)) or isinstance(master, (dict, list)):
        pruned = prune_missing_to_absent_values(master, app_leaf_values)
        if pruned is not None:
            _increment_key_labels_from_tree(pruned, key_label_counts, header)
        _increment_key_labels_from_tree(current, key_label_counts, header)
        return None, pruned, deep_copy(current)

    app_leaf = stringify_leaf(current)
    master_leaf = stringify_leaf(master)
    if app_leaf == master_leaf:
        return None, None, None
    return {"app": app_leaf, "master": master_leaf}, None, None


def build_audit(
    input_manifest: Dict[str, Any],
    master: Dict[str, Any],
    input_headers: List[str],
) -> Tuple[Dict[str, Any], Dict[str, int], Dict[str, int], Dict[str, str]]:
    text_diff: Dict[str, Any] = {}
    missing_content: Dict[str, Any] = {}
    unique_content: Dict[str, Any] = {}
    missing_counts: Dict[str, int] = {}
    unique_counts: Dict[str, int] = {}
    prefixes: Dict[str, str] = {}
    key_label_counts: Dict[str, Dict[str, Any]] = {}

    for header in input_headers:
        app_header = input_manifest.get(header) if isinstance(input_manifest, dict) else None
        if app_header is None:
            continue

        app_leaf_values = collect_leaf_values(app_header)
        t_node, m_node, u_node = _audit_node(
            app_header,
            master,
            [header],
            app_leaf_values,
            key_label_counts,
            header,
        )
        text_diff[header] = t_node if t_node is not None else {}
        missing_content[header] = m_node if m_node is not None else {}
        unique_content[header] = u_node if u_node is not None else {}

        missing_counts[header] = count_nodes(m_node) if m_node is not None else 0
        unique_counts[header] = count_nodes(u_node) if u_node is not None else 0

        prefix = extract_prefix(header, app_header)
        if prefix:
            prefixes[header] = prefix

    key_label_summary: List[Dict[str, Any]] = []
    for row in key_label_counts.values():
        headers = sorted(row["headers"])
        key_label_summary.append(
            {
                "key": row["key"],
                "total_count": row["count"],
                "header_count": len(headers),
                "headers": headers,
            }
        )
    key_label_summary.sort(key=lambda x: (x["total_count"], x["header_count"]), reverse=True)

    output = {
        "text_diff": text_diff,
        "missing_content": missing_content,
        "unique_content": unique_content,
        "summary": build_missing_summary(missing_content),
        "key_label_summary": key_label_summary,
    }
    return output, missing_counts, unique_counts, prefixes


# ---------- Merge ----------
def merge_overlay_into_master(master: Any, overlay: Any, path: List[str]) -> Any:
    if overlay is None:
        return master

    if master is None:
        return deep_copy(overlay)

    if isinstance(master, dict) and isinstance(overlay, dict):
        result = deep_copy(master)
        for k, o_val in overlay.items():
            if k in result:
                result[k] = merge_overlay_into_master(result[k], o_val, path + [k])
            else:
                result[k] = deep_copy(o_val)
        return result

    if isinstance(master, list) and isinstance(overlay, list):
        result = deep_copy(master)
        seen = {normalize(x, path) for x in result}
        for o in overlay:
            n = normalize(o, path)
            if n not in seen:
                seen.add(n)
                result.append(deep_copy(o))
        return result

    return master


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


def merge_outputs(master_json: dict, diff_output: dict, prefixes: dict, app_id: str) -> dict:
    out = {"manifest": {}}
    for header, header_overlay in diff_output.items():
        merged = deep_copy(master_json)
        merged = merge_overlay_into_master(merged, header_overlay, path=[header])
        prefix = prefixes.get(header)
        out["manifest"][header] = _replace_placeholders(merged, app_id, prefix)
    return out


# ---------- Update ----------
def _matches_remove_pattern(target: Any, pattern: Any, path: List[str]) -> bool:
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
            return True
        target_norms = {normalize(x, path) for x in target}
        pattern_norms = {normalize(x, path) for x in pattern}
        return len(target_norms.intersection(pattern_norms)) > 0

    return meaningfully_equal(target, pattern, path)


def _apply_remove_list_by_pattern(current_list: List[Any], remove_list: List[Any], path: List[str]) -> None:
    if not current_list or not remove_list:
        return

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

    for pat in dict_patterns:
        if len(pat) == 0:
            continue
        for item in current_list:
            if not isinstance(item, dict):
                continue
            if _matches_remove_pattern(item, pat, path + ["<list-item>"]):
                _apply_remove(item, pat, path + ["<list-item>"])

    current_list[:] = [x for x in current_list if not (isinstance(x, dict) and len(x) == 0)]


def _apply_remove(current: Any, remove_spec: Any, path: List[str]) -> Any:
    if remove_spec is None:
        return current

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
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k == "insert_index":
                continue
            out[k] = _strip_insert_index(v)
        return out
    if isinstance(node, list):
        return [_strip_insert_index(i) for i in node]
    return deep_copy(node)


def _list_update_with_optional_insert_index(current_list: List[Any], update_list: List[Any], path: List[str]) -> None:
    if not update_list:
        return

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

    indexed_patches.sort(key=lambda t: t[0])

    for idx, patch in indexed_patches:
        if idx < 0:
            idx = 0

        if idx < len(current_list):
            target = current_list[idx]
            if isinstance(target, dict) and isinstance(patch, dict):
                _apply_update(target, patch, path + [f"[{idx}]"])
            elif isinstance(target, list) and isinstance(patch, list):
                _list_update_with_optional_insert_index(target, patch, path + [f"[{idx}]"])
        else:
            current_list.append(deep_copy(patch))
            seen.add(normalize(patch, path))

    for item in non_indexed:
        n = normalize(item, path)
        if n in seen:
            continue
        seen.add(n)
        current_list.append(deep_copy(item))


def _apply_update(current: Any, update_spec: Any, path: List[str]) -> Any:
    if update_spec is None:
        return current

    if isinstance(update_spec, dict):
        if not isinstance(current, dict):
            return current

        for k, u_val in update_spec.items():
            if k not in current:
                current[k] = _strip_insert_index(u_val)
                continue

            c_val = current.get(k)
            if isinstance(u_val, dict) and isinstance(c_val, dict):
                _apply_update(c_val, u_val, path + [k])
            elif isinstance(u_val, list) and isinstance(c_val, list):
                _list_update_with_optional_insert_index(c_val, u_val, path + [k])

        return current

    if isinstance(update_spec, list) and isinstance(current, list):
        _list_update_with_optional_insert_index(current, update_spec, path)
        return current

    return current


def _select_instructions_for_header(master: Dict[str, Any], header: str) -> Tuple[Optional[Any], Optional[Any]]:
    remove_root = master.get("remove")
    update_root = master.get("update")

    if isinstance(remove_root, dict) and header in remove_root:
        remove_spec = remove_root.get(header)
    else:
        remove_spec = remove_root

    if isinstance(update_root, dict) and header in update_root:
        update_spec = update_root.get(header)
    else:
        update_spec = update_root

    return remove_spec, update_spec


def update_file(master: Dict[str, Any], input_manifest: Dict[str, Any], input_headers: List[str]) -> Dict[str, Any]:
    updated_manifest: Dict[str, Any] = {}

    for header in input_headers:
        working_header = input_manifest.get(header)
        if working_header is None:
            continue

        current = deep_copy(working_header)
        remove_spec, update_spec = _select_instructions_for_header(master, header)

        if remove_spec is not None:
            current = _apply_remove(current, remove_spec, [header])
        if update_spec is not None:
            current = _apply_update(current, update_spec, [header])

        updated_manifest[header] = current

    return {"manifest": updated_manifest}


# ---------- Logging + flow ----------
def write_console_log(
    input_headers: List[str],
    text_counts: Dict[str, int],
    unique_counts: Dict[str, int],
    missing_counts: Dict[str, int],
    master_label: str,
    key_label_summary: Optional[Any] = None,
) -> None:
    total_text = sum(text_counts.values())
    total_unique = sum(unique_counts.values())
    total_missing = sum(missing_counts.values())
    lines = []
    lines.append(f"Headers processed: {len(input_headers)}")
    lines.append(f"Total text_diff (leaf mismatches): {total_text}")
    lines.append(f"Total unique_content (app-only): {total_unique}")
    lines.append(f"Total missing_content (master-only): {total_missing}")
    lines.append("")
    lines.append(f"{master_label} | Text/Label Differences | Unique Data | Missing Data")
    sorted_headers = sorted(
        input_headers,
        key=lambda h: text_counts.get(h, 0) + unique_counts.get(h, 0) + missing_counts.get(h, 0),
        reverse=True,
    )
    for header in sorted_headers:
        lines.append(
            f"{header} | {text_counts.get(header, 0)} | {unique_counts.get(header, 0)} | {missing_counts.get(header, 0)}"
        )

    if isinstance(key_label_summary, list) and key_label_summary:
        rows = [r for r in key_label_summary if r.get("total_count", 0) >= 3]
        if rows:
            lines.append("")
            lines.append("Key Label | Total Count | Header Count")
            for row in rows:
                lines.append(
                    f"{row.get('key')} | {row.get('total_count', 0)} | {row.get('header_count', 0)}"
                )

    print("\n".join(lines))


app_id = Input220.value
input_manifest_raw = parse_json_maybe_double_encoded(FilePicker1.files[0]["readContents"]())
mode = Dropdown130.selectedOptionValue
print("Mode: ", mode)

master_raw = parse_json_maybe_double_encoded(FilePicker2.files[0]["readContents"]())
master_label = extract_master_label(FilePicker2.files[0])
input_manifest = unwrap_manifest(input_manifest_raw)
master = unwrap_manifest(master_raw)
input_headers = select_app_headers(input_manifest)
mode_lower = mode.lower()

if mode_lower == "update":
    return update_file(master, input_manifest, input_headers)

audit_output, missing_counts, unique_counts, prefixes = build_audit(
    input_manifest, master, input_headers
)
text_counts = {}
for header in input_headers:
    node = audit_output["text_diff"].get(header, {})
    text_counts[header] = count_nodes(node) if node else 0

write_console_log(
    input_headers,
    text_counts,
    unique_counts,
    missing_counts,
    master_label,
    audit_output.get("key_label_summary"),
)

if mode_lower in ("audit", "audit-text", "audit-structure"):
    return audit_output

# merge mode uses app-only content as overlay, while keeping master scalar values.
return merge_outputs(master, audit_output["unique_content"], prefixes, app_id)
