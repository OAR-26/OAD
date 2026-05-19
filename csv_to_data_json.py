#!/usr/bin/env python3
"""
Convert simulator workload CSV to data.json for goard.

Usage:
    python csv_to_data_json.py input.csv output.json [options]

Format configs live in formats/*.yaml next to this script.
Each YAML defines column mappings, resource format, and state map
for one simulator. New simulators: add a YAML file, no code changes.

Options:
    --format STR          Format name (e.g. batsim) or path to YAML config.
                          Auto-detected from CSV headers if omitted.
    --formats-dir DIR     Directory with format YAML files.
                          Default: formats/ next to this script.
    --base-time INT       Unix timestamp for simulation time 0.
                          Default: now - ceil(max_finish_time * time_scale)
    --time-scale FLOAT    Multiply all simulation times by this factor.
    --cores-per-node INT  Processors per node (default: 1)
    --cluster STR         Cluster name (default: cluster1)
    --site STR            Site name (default: site1)
    --node-prefix STR     Node hostname prefix (default: node)
    --domain STR          Hostname domain suffix (default: .local)
"""

import argparse
import csv
import json
import math
import sys
import time
from pathlib import Path

try:
    import yaml
except ImportError:
    print("ERROR: pyyaml required. Install: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

SCRIPT_DIR = Path(__file__).parent


# ── Config loading ────────────────────────────────────────────────────────────

def load_config(path):
    with open(path) as f:
        return yaml.safe_load(f)


def detect_format(headers, formats_dir):
    """Score every YAML in formats_dir against CSV headers, return best."""
    header_set = {h.strip() for h in headers}
    best_score, best_cfg, best_name = -1, None, None

    for yaml_path in Path(formats_dir).glob("*.yaml"):
        try:
            cfg = load_config(yaml_path)
        except Exception as e:
            print(f"Warning: could not load {yaml_path}: {e}", file=sys.stderr)
            continue

        sig = cfg.get("signature_columns", [])
        score = sum(1 for col in sig if col in header_set)

        if score > best_score:
            best_score, best_cfg, best_name = score, cfg, yaml_path.stem

    return best_cfg, best_name, best_score


def resolve_config(args):
    """Return (config_dict, format_name) from --format flag or auto-detect."""
    formats_dir = args.formats_dir or (SCRIPT_DIR / "formats")

    if args.format:
        p = Path(args.format)
        if not p.suffix:
            p = Path(formats_dir) / f"{args.format}.yaml"
        cfg = load_config(p)
        return cfg, cfg.get("name", p.stem)

    # Auto-detect: peek at CSV headers
    with open(args.input, newline='') as f:
        sample = f.read(4096)

    dialect = csv.Sniffer().sniff(sample, delimiters=',\t;')
    first_line = sample.splitlines()[0]
    headers = first_line.split(dialect.delimiter)

    cfg, name, score = detect_format(headers, formats_dir)

    if cfg is None:
        print(
            "ERROR: no matching format found in formats/. "
            "Pass --format explicitly.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"Auto-detected format: {name!r} (score={score})", file=sys.stderr)
    return cfg, name


# ── Column access ─────────────────────────────────────────────────────────────

def get_col(row, canonical, cfg):
    """Get value from row by canonical name using config column mapping."""
    candidates = cfg.get("columns", {}).get(canonical, [])
    if isinstance(candidates, str):
        candidates = [candidates]
    for name in candidates:
        if name in row:
            return row[name]
    return ''


# ── Resource parsing ──────────────────────────────────────────────────────────

def _parse_range_or_list(s):
    """'0 4-7 9' or '0,4-7,9' -> [0, 4, 5, 6, 7, 9]"""
    s = (s or '').strip()
    if not s:
        return []
    result = []
    for token in s.replace(',', ' ').split():
        if '-' in token:
            a, b = token.split('-', 1)
            result.extend(range(int(a), int(b) + 1))
        else:
            result.append(int(token))
    return result


_RESOURCE_PARSERS = {
    'range_or_list': _parse_range_or_list,
}


def parse_resources(s, cfg):
    fmt = cfg.get("resource_format", "range_or_list")
    parser = _RESOURCE_PARSERS.get(fmt)
    if parser is None:
        print(f"ERROR: unknown resource_format {fmt!r}", file=sys.stderr)
        sys.exit(1)
    return parser(s)


# ── State inference ───────────────────────────────────────────────────────────

def infer_state(row, cfg):
    """Return (goard_state, exit_code) using config state_map + fallback."""
    state_map = cfg.get("state_map", {})
    fs = get_col(row, 'final_state', cfg).strip().upper()

    if fs in state_map:
        entry = state_map[fs]
        return (entry[0], entry[1])

    # Fallback: timing + numeric success field
    start  = float(get_col(row, 'start',  cfg) or 0)
    finish = float(get_col(row, 'finish', cfg) or 0)

    if start <= 0:
        return ('Waiting', None)
    if finish <= 0:
        return ('Running', None)

    try:
        ok = int(float(get_col(row, 'success', cfg) or 0))
    except ValueError:
        ok = 0

    return ('Terminated', 0) if ok == 1 else ('Error', 1)


# ── Resource object builder ───────────────────────────────────────────────────

def make_resource(resource_id, proc_id, node_name, cluster, site,
                  core_in_node, cores_per_node):
    return {
        "resource_id": resource_id,
        "network_address": node_name,
        "host": node_name,
        "cluster": cluster,
        "site": site,
        "type": "default",
        "state": "Alive",
        "core": (proc_id + 1) * 10,
        "core_count": cores_per_node,
        "thread_count": cores_per_node,
        "cpu": proc_id + 1,
        "cpu_count": 1,
        "cpucore": cores_per_node,
        "cpuarch": "x86_64",
        "cputype": "Unknown",
        "cpufreq": "0",
        "cpuset": str(core_in_node),
        "memnode": 0,
        "memcore": 0,
        "memcpu": 0,
        "gpu": 0,
        "gpu_count": 0,
        "gpu_model": None,
        "gpu_compute_capability": None,
        "gpu_compute_capability_major": 0,
        "gpu_mem": 0,
        "gpudevice": None,
        "drain": "NO",
        "available_upto": 2147483646,
        "last_available_upto": 0,
        "production": "YES",
        "besteffort": "YES",
        "deploy": "NO",
        "next_state": "UnChanged",
        "next_finaud_decision": "NO",
        "state_num": 1,
        "scheduler_priority": 0,
        "cluster_priority": 0,
        "suspended_jobs": "0",
        "eth_rate": 1,
        "eth_count": 1,
        "eth_kavlan_count": 0,
        "ib": "NO",
        "ib_count": 0,
        "ib_rate": 0,
        "opa_count": 0,
        "opa_rate": 0,
        "myri": "NO",
        "myri_count": 0,
        "myri_rate": 0,
        "mic": "NO",
        "virtual": "NO",
        "wattmeter": "NO",
        "exotic": "NO",
        "grub": None,
        "maintenance": "NO",
        "chassis": "",
        "switch": "",
        "disk": None,
        "disktype": None,
        "disk_reservation_count": 0,
        "nodeset": None,
        "subnet_address": None,
        "subnet_prefix": None,
        "slash_16": None,
        "slash_17": None,
        "slash_18": None,
        "slash_19": None,
        "slash_20": None,
        "slash_21": None,
        "slash_22": None,
        "vlan": None,
        "last_job_date": 0,
        "expiry_date": 0,
        "finaud_decision": "NO",
        "comment": "",
        "nodemodel": "",
        "max_walltime": 86400,
        "chunks": None,
        "ip": None,
        "rconsole": None,
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Convert simulator workload CSV to data.json for goard"
    )
    parser.add_argument("input",  help="Input CSV file")
    parser.add_argument("output", help="Output JSON file")
    parser.add_argument(
        "--format",
        default=None,
        help="Format name (e.g. batsim) or path to YAML config. "
             "Auto-detected if omitted.",
    )
    parser.add_argument(
        "--formats-dir",
        default=None,
        help="Directory with format YAML files (default: formats/ next to script)",
    )
    parser.add_argument("--base-time",      type=int,   default=None)
    parser.add_argument("--time-scale",     type=float, default=1.0)
    parser.add_argument("--cores-per-node", type=int,   default=1)
    parser.add_argument("--cluster",        default="cluster1")
    parser.add_argument("--site",           default="site1")
    parser.add_argument("--node-prefix",    default="node")
    parser.add_argument("--domain",         default=".local")

    args = parser.parse_args()

    if args.time_scale <= 0:
        print("ERROR: --time-scale must be > 0", file=sys.stderr)
        sys.exit(1)

    cfg, fmt_name = resolve_config(args)
    scale = args.time_scale

    print(f"Using format: {fmt_name}", file=sys.stderr)

    # ── Auto-detect delimiter ─────────────────────────────────────────────────
    with open(args.input, newline='') as f:
        sample = f.read(4096)

    dialect = csv.Sniffer().sniff(sample, delimiters=',\t;')
    delimiter = dialect.delimiter

    # ── Read CSV ──────────────────────────────────────────────────────────────
    rows = []
    with open(args.input, newline='') as f:
        reader = csv.DictReader(f, delimiter=delimiter)
        for row in reader:
            rows.append({
                (k.strip() if k else k): (v.strip() if v else '')
                for k, v in row.items()
            })

    if not rows:
        print("ERROR: no rows in CSV", file=sys.stderr)
        sys.exit(1)

    print(f"Read {len(rows)} rows, delimiter={repr(delimiter)}", file=sys.stderr)
    print(f"Columns: {list(rows[0].keys())}", file=sys.stderr)
    print(f"time_scale={scale}", file=sys.stderr)

    # ── base_time ─────────────────────────────────────────────────────────────
    max_finish = max(float(get_col(r, 'finish', cfg) or 0) for r in rows)
    scaled_max = max_finish * scale

    if args.base_time is None:
        args.base_time = int(time.time()) - int(math.ceil(scaled_max))
        print(
            f"base_time={args.base_time} (now - {int(math.ceil(scaled_max))}s)",
            file=sys.stderr,
        )

    def to_unix(t):
        v = float(t) if t else 0.0
        return 0 if v <= 0 else args.base_time + int(v * scale)

    # ── Collect all resource IDs ──────────────────────────────────────────────
    all_procs = set()
    for row in rows:
        all_procs.update(parse_resources(get_col(row, 'resources', cfg), cfg))

    if not all_procs:
        print(
            "Warning: no resource IDs found, defaulting to proc 0",
            file=sys.stderr,
        )
        all_procs = {0}

    cpn = args.cores_per_node
    resources = []
    proc_to_rid = {}

    for proc_id in range(max(all_procs) + 1):
        rid = proc_id + 1
        node_id = proc_id // cpn
        core_in_node = proc_id % cpn
        node_name = f"{args.node_prefix}{node_id + 1}{args.domain}"
        proc_to_rid[proc_id] = rid
        resources.append(
            make_resource(rid, proc_id, node_name, args.cluster, args.site,
                          core_in_node, cpn)
        )

    # ── Build jobs ────────────────────────────────────────────────────────────
    jobs = {}
    for row in rows:
        job_id = get_col(row, 'job_id', cfg).strip()
        if not job_id:
            continue

        procs = parse_resources(get_col(row, 'resources', cfg), cfg)
        resource_ids = [str(proc_to_rid[p]) for p in procs if p in proc_to_rid]
        node_names = list(dict.fromkeys(
            resources[proc_to_rid[p] - 1]['host']
            for p in procs if p in proc_to_rid
        ))

        start    = float(get_col(row, 'start',      cfg) or 0)
        finish   = float(get_col(row, 'finish',     cfg) or 0)
        sub      = float(get_col(row, 'submission', cfg) or 0)
        walltime = int(float(get_col(row, 'walltime', cfg) or 0) * scale)
        workload = get_col(row, 'workload', cfg) or 'unknown'

        state, exit_code = infer_state(row, cfg)

        jobs[job_id] = {
            "owner": workload,
            "state": state,
            "start_time":       to_unix(start),
            "stop_time":        to_unix(finish),
            "submission_time":  to_unix(sub),
            "walltime":         walltime,
            "original_start_time":      start,
            "original_stop_time":       finish,
            "original_submission_time": sub,
            "original_walltime":        float(get_col(row, 'walltime', cfg) or 0),
            "time_scale":       scale,
            "queue":            "default",
            "resource_id":      resource_ids,
            "network_address":  node_names,
            "job_type":         "PASSIVE",
            "types":            ["PASSIVE"],
            "name":             f"job-{job_id}",
            "project":          workload,
            "command":          "",
            "message":          "",
            "resubmit_job_id":  0,
            "array_id": (
                int(job_id) if job_id.lstrip('-').isdigit() else 0
            ),
            "properties":        "",
            "assigned_hostnames": node_names,
            "cpuset_name":       f"oar_{job_id}",
            "exit_code":         exit_code,
            "log_name":          "",
            "stderr_file":       f"./{job_id}.err",
            "stdout_file":       f"./{job_id}.out",
            "events":            [],
        }

    output = {"resources": resources, "jobs": jobs, "dead_resources": {}}

    with open(args.output, 'w') as f:
        json.dump(output, f, indent=2)

    print(
        f"Done: {len(resources)} resources, {len(jobs)} jobs -> {args.output}",
        file=sys.stderr,
    )


if __name__ == '__main__':
    main()
