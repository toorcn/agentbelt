#!/usr/bin/env python3
"""peek: summarize a large data file so an agent can understand it without reading it.

Usage:
  peek FILE                 schema + stats + a few samples (JSON, JSONL, CSV/TSV, SQLite, logs/text)
  peek FILE --path a.b[0]   drill into a JSON sub-tree
  peek FILE --rows 5        show more sample rows
  peek FILE --json          machine-readable output

Output is capped (~2-3k tokens) no matter how big the file is.
"""
import argparse
import collections
import csv
import json
import os
import re
import sqlite3
import sys

MAX_KEYS = 40           # object keys shown per level
MAX_DEPTH = 6           # schema nesting depth
MAX_STR = 60            # example string truncation
SCAN_ROWS = 200_000     # rows scanned for CSV/JSONL stats
ARRAY_SAMPLE = 5000     # array items scanned per array
MAP_KEY_CAP = 500       # distinct keys tracked per object position
DISTINCT_CAP = 100_000  # distinct scalar values tracked per position

# ---------- credential redaction ----------
# Anything peek prints lands in an agent's context, so credentials are masked by field name
# (split into words, so `author` and `max_tokens` are left alone) and by known value formats.

SECRET_WORDS = {
    "password", "passwd", "pwd", "passphrase", "secret", "secrets", "token", "apikey", "accesskey", "secretkey",
    "privatekey", "credential", "credentials", "auth", "authorization", "session", "sessionid", "cookie", "cookies",
    "salt", "otp",
}
SECRET_PAIRS = {("api", "key"), ("access", "key"), ("private", "key"), ("secret", "key"), ("client", "secret"),
                ("api", "secret"), ("session", "id")}
NAME_WORD = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")
# Keys never start mid-word, so each format needs a boundary in front. Without it, URL slugs
# (`desk-lamp-...`) and base64 image data trip the patterns.
SECRET_VALUE = re.compile(
    r"""
    (?<![A-Za-z0-9+/_-])
    (?:
      sk-[A-Za-z0-9_-]{16,}                        # OpenAI, Anthropic
    | [sr]k_(live|test)_[A-Za-z0-9]{16,}           # Stripe
    | gh[pousr]_[A-Za-z0-9]{30,}                   # GitHub
    | github_pat_[A-Za-z0-9_]{40,}                 # GitHub fine-grained
    | xox[abprs]-[A-Za-z0-9-]{20,}                 # Slack
    | AKIA[0-9A-Z]{16}                             # AWS access key id
    | AIza[0-9A-Za-z_-]{35}                        # Google API key
    | eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}    # JWT
    | -----BEGIN\ [A-Z\ ]*PRIVATE\ KEY-----
    | [a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@      # user:password@ in a URL
    )
    """,
    re.X,
)
# key=value / key: value / "key": "value" pairs in free text, where the key names a secret.
# Pure numbers are left alone (max_tokens=4096).
SECRET_PAIR_TEXT = re.compile(
    r"""
    (?P<key>["']?[\w.-]*?(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key
                          |authorization|cookie|session[_-]?id|credential)s?[\w.-]*["']?)
    (?P<sep>\s*[:=]\s*["']?)
    (?!\d+(?:\.\d+)?\b)
    (?P<value>(?:bearer\s+|basic\s+)?[^\s"',;&}]+)
    """,
    re.I | re.X,
)
BEARER = re.compile(r"\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{16,}", re.I)
MASK = "<redacted>"


def name_words(name):
    return [w.lower() for w in NAME_WORD.findall(name or "")]


def is_secret_name(name):
    words = name_words(name)
    return any(w in SECRET_WORDS for w in words) or any(pair in SECRET_PAIRS for pair in zip(words, words[1:]))


def redact(v, name="", secret=False):
    """Mask a value if its field name or its own format says it is a credential."""
    if isinstance(v, (bytes, bytearray, memoryview)):
        return f"<blob {len(v)}B>"
    if isinstance(v, str) and v and (secret or is_secret_name(name) or SECRET_VALUE.search(v)):
        return f"<redacted {len(v)} chars>"
    return v


def redact_text(line):
    """Mask credentials inside free text such as log lines."""
    line = SECRET_PAIR_TEXT.sub(lambda m: m.group("key") + m.group("sep") + MASK, line)
    line = BEARER.sub(lambda m: m.group(1) + " " + MASK, line)
    return SECRET_VALUE.sub(MASK, line)


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def short(v, name="", secret=False):
    v = redact(v, name, secret)
    s = json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v
    s = s.replace("\n", "\\n")
    return s if len(s) <= MAX_STR else s[: MAX_STR - 1] + "…"


# ---------- JSON schema inference ----------

class Node:
    """Merged shape of every value seen at one position in the document."""

    def __init__(self):
        self.count = 0
        self.types = collections.Counter()
        self.keys = {}            # key -> Node (objects)
        self.key_count = collections.Counter()
        self.items = None         # Node (arrays)
        self.lens = []            # array lengths
        self.examples = []
        self.distinct = set()
        self.num_min = self.num_max = None
        self.max_keys = 0         # largest object seen (keys may be capped)

    def merge(self, o):
        self.count += o.count
        self.types.update(o.types)
        self.key_count.update(o.key_count)
        self.max_keys = max(self.max_keys, o.max_keys)
        for k, c in o.keys.items():
            if k not in self.keys:
                if len(self.keys) >= MAP_KEY_CAP:
                    continue
                self.keys[k] = Node()
            self.keys[k].merge(c)
        if o.items is not None:
            if self.items is None:
                self.items = Node()
            self.items.merge(o.items)
        self.lens += o.lens
        for e in o.examples:
            if len(self.examples) < 3 and e not in self.examples:
                self.examples.append(e)
        if len(self.distinct) < DISTINCT_CAP:
            self.distinct |= o.distinct
        if o.num_min is not None:
            self.num_min = o.num_min if self.num_min is None else min(self.num_min, o.num_min)
            self.num_max = o.num_max if self.num_max is None else max(self.num_max, o.num_max)

    def add(self, v, depth=0):
        self.count += 1
        t = jtype(v)
        self.types[t] += 1
        if t == "object":
            self.max_keys = max(self.max_keys, len(v))
            if depth >= MAX_DEPTH:
                return
            for k, child in v.items():
                self.key_count[k] += 1
                if k not in self.keys:
                    if len(self.keys) >= MAP_KEY_CAP:
                        continue
                    self.keys[k] = Node()
                self.keys[k].add(child, depth + 1)
        elif t == "array":
            self.lens.append(len(v))
            if depth >= MAX_DEPTH:
                return
            if self.items is None:
                self.items = Node()
            for child in v[:ARRAY_SAMPLE]:
                self.items.add(child, depth + 1)
        else:
            if len(self.distinct) < DISTINCT_CAP:
                self.distinct.add(v if not isinstance(v, float) else round(v, 6))
            if len(self.examples) < 3 and v not in self.examples:
                self.examples.append(v)
            if t in ("int", "float"):
                self.num_min = v if self.num_min is None else min(self.num_min, v)
                self.num_max = v if self.num_max is None else max(self.num_max, v)


def jtype(v):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "bool"
    if isinstance(v, int):
        return "int"
    if isinstance(v, float):
        return "float"
    if isinstance(v, str):
        return "str"
    if isinstance(v, list):
        return "array"
    return "object"


def looks_like_map(node):
    """Objects keyed by ids/paths/names (e.g. package-lock 'packages') -> show as map."""
    if len(node.keys) < 20:
        return False
    rare = sum(1 for c in node.key_count.values() if c <= max(1, node.count // 10))
    return rare / len(node.keys) > 0.8


def render(node, name, indent, out, parent_count, secret=False):
    secret = secret or is_secret_name(name)
    pad = "  " * indent
    types = "|".join(t for t, _ in node.types.most_common())
    opt = "" if node.count >= parent_count else f"  (in {node.count}/{parent_count})"
    if "object" in node.types and node.keys:
        if looks_like_map(node):
            merged = Node()
            for child in node.keys.values():
                merged.merge(child)
            keys = ", ".join(short(k) for k in list(node.keys)[:3])
            out.append(f"{pad}{name}: map<str, {'|'.join(merged.types)}> {node.max_keys} keys, e.g. {keys}{opt}")
            # every value of a map shares one merged schema
            render(merged, "<value>", indent + 1, out, merged.count, secret)
            return
        out.append(f"{pad}{name}: {types}{opt}")
        for i, (k, child) in enumerate(sorted(node.keys.items(), key=lambda kv: -node.key_count[kv[0]])):
            if i >= MAX_KEYS:
                out.append(f"{pad}  … {len(node.keys) - MAX_KEYS} more keys")
                break
            render(child, k, indent + 1, out, node.types["object"], secret)
    elif "array" in node.types:
        lens = node.lens
        ln = f"len {min(lens)}" if min(lens) == max(lens) else f"len {min(lens)}–{max(lens)}"
        if max(lens) > ARRAY_SAMPLE:
            ln += f", schema from first {ARRAY_SAMPLE}"
        out.append(f"{pad}{name}: array ({ln}){opt}")
        if node.items is not None and node.items.count:
            render(node.items, "[]", indent + 1, out, node.items.count, secret)
    else:
        bits = [types]
        if node.num_min is not None and node.num_min != node.num_max:
            bits.append(f"range {node.num_min}…{node.num_max}")
        nd = len(node.distinct)
        if node.types.get("str") and nd <= 8 and node.count > nd * 2:
            bits.append("enum{" + ", ".join(short(x, name, secret) for x in sorted(node.distinct, key=str)) + "}")
        elif node.examples:
            bits.append("e.g. " + ", ".join(short(x, name, secret) for x in node.examples[:2]))
        if node.count > 1 and nd >= node.count and node.types.get("str", 0) + node.types.get("int", 0) == node.count:
            bits.append("unique")
        out.append(f"{pad}{name}: {'  '.join(bits)}{opt}")


def resolve_path(doc, path):
    for part in re.findall(r"[^.\[\]]+|\[\d+\]", path):
        if part.startswith("["):
            doc = doc[int(part[1:-1])]
        else:
            doc = doc[part]
    return doc


def peek_json(path, args, out):
    with open(path, "rb") as f:
        doc = json.load(f)
    if args.path:
        doc = resolve_path(doc, args.path)
        out.append(f"at path: {args.path}")
    root = Node()
    root.add(doc)
    render(root, "$", 0, out, 1)
    sample = doc
    if isinstance(doc, list) and doc:
        sample = doc[: args.rows]
        out.append(f"\nsample ({len(sample)} of {len(doc)}):")
    else:
        out.append("\nsample (truncated):")
    out.append(truncate_json(sample))


def truncate_json(v, budget=1200):
    """Compact JSON with long strings/arrays/objects elided, roughly within budget chars."""
    def trim(x, depth, key="", secret=False):
        secret = secret or is_secret_name(key)
        x = redact(x, key, secret)
        if isinstance(x, str):
            return x if len(x) <= MAX_STR else x[:MAX_STR] + "…"
        if isinstance(x, list):
            items = [trim(i, depth + 1, secret=secret) for i in x[:3]]
            if len(x) > 3:
                items.append(f"…+{len(x) - 3}")
            return items
        if isinstance(x, dict):
            if depth > 3:
                return "{…}"
            d = {}
            for i, (k, val) in enumerate(x.items()):
                if i >= 12:
                    d["…"] = f"+{len(x) - 12} keys"
                    break
                d[k] = trim(val, depth + 1, k, secret)
            return d
        return x
    s = json.dumps(trim(v, 0), ensure_ascii=False, indent=1)
    return s if len(s) <= budget else s[:budget] + "\n…"


def peek_jsonl(path, args, out):
    root = Node()
    first = []
    bad = n = 0
    with open(path, "rb") as f:
        for line in f:
            if not line.strip():
                continue
            n += 1
            if n > SCAN_ROWS:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                bad += 1
                continue
            root.add(obj)
            if len(first) < args.rows:
                first.append(obj)
    line = f"records: {n}"
    if n > SCAN_ROWS:
        line += f"  (stats from first {SCAN_ROWS})"
    if bad:
        line += f"  unparseable lines: {bad}"
    out.append(line)
    render(root, "record", 0, out, root.count)
    out.append(f"\nsample ({len(first)}):")
    for obj in first:
        out.append(truncate_json(obj, 400).replace("\n", ""))


# ---------- CSV ----------

def infer_scalar(s):
    if s == "":
        return "empty", None
    try:
        return "int", int(s)
    except ValueError:
        pass
    try:
        return "float", float(s)
    except ValueError:
        pass
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?.*)?", s):
        return "date", s
    if s.lower() in ("true", "false"):
        return "bool", s
    return "str", s


def peek_csv(path, args, out):
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        head = f.read(65536)
        f.seek(0)
        try:
            dialect = csv.Sniffer().sniff(head, delimiters=",\t;|")
        except csv.Error:
            dialect = csv.excel_tab if path.endswith(".tsv") else csv.excel
        reader = csv.reader(f, dialect)
        header = next(reader)
        cols = [{"types": collections.Counter(), "empty": 0, "vals": collections.Counter(),
                 "min": None, "max": None, "maxlen": 0} for _ in header]
        rows = 0
        ragged = 0
        samples = []
        for row in reader:
            rows += 1
            if len(row) != len(header):
                ragged += 1
            if len(samples) < args.rows:
                samples.append(row)
            if rows > SCAN_ROWS:
                continue
            for c, cell in zip(cols, row):
                t, v = infer_scalar(cell.strip())
                if t == "empty":
                    c["empty"] += 1
                    continue
                c["types"][t] += 1
                c["maxlen"] = max(c["maxlen"], len(cell))
                if len(c["vals"]) < 5000 or cell in c["vals"]:
                    c["vals"][cell] += 1
                if t in ("int", "float", "date"):
                    c["min"] = v if c["min"] is None else min(c["min"], v)
                    c["max"] = v if c["max"] is None else max(c["max"], v)
    delim = {",": "comma", "\t": "tab", ";": "semicolon", "|": "pipe"}.get(dialect.delimiter, dialect.delimiter)
    out.append(f"rows: {rows}  columns: {len(header)}  delimiter: {delim}" + (f"  ragged rows: {ragged}" if ragged else ""))
    scanned = min(rows, SCAN_ROWS)
    for name, c in zip(header, cols):
        t = c["types"].most_common(1)[0][0] if c["types"] else "empty"
        if t == "int" and c["types"].get("float"):
            t = "float"
        mixed = len(c["types"]) > 1 and not set(c["types"]) <= {"int", "float"}
        bits = [t + (" (mixed: " + ",".join(c["types"]) + ")" if mixed else "")]
        if c["empty"]:
            bits.append(f"null {100 * c['empty'] / max(scanned, 1):.0f}%")
        nd = len(c["vals"])
        if c["min"] is not None:
            bits.append(f"range {c['min']}…{c['max']}")
        if t == "str" and nd <= 12 and scanned > nd * 3:
            top = ", ".join(f"{short(k, name)}({v})" for k, v in c["vals"].most_common(6))
            bits.append(f"{nd} values: {top}")
        else:
            unique = scanned > 1 and nd == scanned - c["empty"]
            bits.append(f"distinct {nd}{'+' if nd >= 5000 else ''}" + (" unique" if unique else ""))
            if t == "str":
                bits.append("e.g. " + ", ".join(short(k, name) for k, _ in c["vals"].most_common(2)))
        out.append(f"  {name}: " + "  ".join(bits))
    out.append(f"\nsample ({len(samples)}):")
    out.append(",".join(header))
    for r in samples:
        out.append(",".join(short(x, h) for x, h in zip(r, header)))


# ---------- SQLite ----------

# settings-style tables (key, value) where the secret's name is data, not a column name
KV_KEY_COLUMNS = {"key", "name", "option", "option_name", "setting", "config_key", "k"}
KV_VALUE_COLUMNS = {"value", "val", "option_value", "setting_value", "config_value", "data", "v"}

def peek_sqlite(path, args, out):
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    tables = con.execute(
        "select name, type from sqlite_master"
        " where type in ('table', 'view') and name not like 'sqlite_%' order by name"
    ).fetchall()
    out.append(f"tables/views: {len(tables)}")
    for name, kind in tables:
        q = name.replace('"', '""')
        try:
            n = con.execute(f'select count(*) from "{q}"').fetchone()[0]
        except sqlite3.Error as e:
            n = f"? ({e})"
        cols = con.execute(f'pragma table_info("{q}")').fetchall()
        colspec = ", ".join(f"{c[1]} {c[2] or '?'}{' PK' if c[5] else ''}" for c in cols)
        out.append(f"\n{kind} {name}  rows: {n}\n  {colspec}")
        fks = con.execute(f'pragma foreign_key_list("{q}")').fetchall()
        for fk in fks:
            out.append(f"  fk {fk[3]} -> {fk[2]}.{fk[4]}")
        names = [c[1] for c in cols]
        lowered = [n.lower() for n in names]
        key_col = next((i for i, n in enumerate(lowered) if n in KV_KEY_COLUMNS), None)
        value_col = next((i for i, n in enumerate(lowered) if n in KV_VALUE_COLUMNS), None)
        for row in con.execute(f'select * from "{q}" limit {min(args.rows, 3)}'):
            row_names = list(names)
            if key_col is not None and value_col is not None and is_secret_name(str(row[key_col])):
                row_names[value_col] = str(row[key_col])
            out.append("  " + " | ".join(short(v, n) for v, n in zip(row, row_names)))


# ---------- logs / text ----------

NORMALIZERS = [
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})?"), "<ts>"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<uuid>"),
    (re.compile(r"\b0x[0-9a-f]+\b|\b[0-9a-f]{12,}\b", re.I), "<hex>"),
    (re.compile(r"\b\d+(\.\d+)?(ms|s|kb|mb|%)?\b", re.I), "<n>"),
]
TEST_SUMMARY = re.compile(
    r"^# (tests|pass|fail|skipped|todo|cancelled) \d+"                 # TAP / node:test
    r"|\b\d+ (passed|failed|errors?|skipped)\b.*\bin [\d.]+s"        # pytest
    r"|^(Tests|Test Suites|Test Files):\s+.*\d+ (passed|failed|total)"  # jest / vitest
    r"|^(FAIL|ok|PASS)\s+\S+\s+[\d.]+s$",                           # go test
    re.I)
FAIL_LINE = re.compile(r"^\s*not ok \d+|^\s*(FAIL|FAILED)\b|::\S+ FAILED\b|^\s*[✗×]|^--- FAIL")
LEVEL = re.compile(r"\b(FATAL|ERROR|ERR|WARN|WARNING|INFO|DEBUG|TRACE|FAIL(?:ED)?|PASS(?:ED)?|Traceback|Exception)\b")
LEVEL_ALIASES = {
    "ERR": "ERROR", "TRACEBACK": "ERROR", "EXCEPTION": "ERROR",
    "WARNING": "WARN", "FAILED": "FAIL", "PASSED": "PASS",
}


def level_of(line):
    m = LEVEL.search(line)
    if not m:
        return None
    word = m.group(1).upper()
    return LEVEL_ALIASES.get(word, word)


def norm(line):
    line = redact_text(line)
    for rx, rep in NORMALIZERS:
        line = rx.sub(rep, line)
    return line.strip()[:160]


def peek_text(path, args, out):
    patterns = collections.Counter()
    first_seen = {}
    levels = collections.Counter()
    first, tail = [], collections.deque(maxlen=max(args.rows, 8))
    n = 0
    first_err = None
    summary, fails = [], []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            n += 1
            line = line.rstrip("\n")
            if len(first) < args.rows:
                first.append(line)
            tail.append(line)
            if TEST_SUMMARY.search(line):
                summary.append(redact_text(line.strip()))
            if FAIL_LINE.search(line) and len(fails) < 10:
                fails.append((n, redact_text(line.strip())[:200]))
            lv = level_of(line)
            if lv:
                levels[lv] += 1
                if lv in ("ERROR", "FATAL", "FAIL") and first_err is None:
                    first_err = (n, redact_text(line)[:300])
            if line.strip():
                p = norm(line)
                patterns[p] += 1
                first_seen.setdefault(p, n)
    out.append(f"lines: {n}  distinct line patterns: {len(patterns)}")
    if levels:
        out.append("levels: " + "  ".join(f"{k}={v}" for k, v in levels.most_common()))
    if summary:
        out.append("test summary: " + " | ".join(summary[-8:]))
    if fails:
        out.append("failing tests:")
        out.extend(f"  L{i}: {l}" for i, l in fails)
    if first_err:
        out.append(f"first error/failure at line {first_err[0]}: {first_err[1]}")
    out.append("\nmost repeated patterns:")
    for p, c in patterns.most_common(8):
        if c < 2:
            break
        out.append(f"  {c:>6}x  {p}")
    rare = [p for p, c in patterns.items() if c == 1 and LEVEL.search(p)]
    if rare:
        out.append("\none-off error/warn lines (first 8):")
        for p in sorted(rare, key=first_seen.get)[:8]:
            out.append(f"  L{first_seen[p]}: {p}")
    out.append(f"\nfirst {len(first)} lines:")
    out.extend("  " + redact_text(l)[:200] for l in first)
    out.append(f"last {len(tail)} lines:")
    out.extend("  " + redact_text(l)[:200] for l in tail)


# ---------- main ----------

def detect(path):
    ext = os.path.splitext(path)[1].lower()
    with open(path, "rb") as f:
        head = f.read(4096)
    if head.startswith(b"SQLite format 3"):
        return "sqlite"
    if ext in (".jsonl", ".ndjson"):
        return "jsonl"
    if ext in (".csv", ".tsv"):
        return "csv"
    if ext == ".json" or head.lstrip()[:1] in (b"{", b"["):
        # JSONL often starts with "{" too: check if line 2 also starts a value
        lines = head.splitlines()
        if ext != ".json" and len(lines) > 1 and lines[0].rstrip().endswith(b"}") and lines[1].lstrip()[:1] == b"{":
            return "jsonl"
        return "json"
    if b"\0" in head:
        return "binary"
    return "text"


def main():
    ap = argparse.ArgumentParser(prog="peek", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("file")
    ap.add_argument("--path", help="JSON path to drill into, e.g. items[0].meta")
    ap.add_argument("--rows", type=int, default=3, help="sample rows/lines (default 3)")
    ap.add_argument("--as", dest="kind", choices=["json", "jsonl", "csv", "sqlite", "text"], help="force format")
    ap.add_argument("--json", action="store_true", help="emit {kind, size, summary} as JSON")
    args = ap.parse_args()

    if not os.path.isfile(args.file):
        sys.exit(f"peek: no such file: {args.file}")
    size = os.path.getsize(args.file)
    kind = args.kind or detect(args.file)
    if size <= 4096 and kind != "sqlite" and not args.path and not args.json:
        # a summary of a tiny file costs about as much as the file itself
        print(f"{args.file}  [{kind}, {human(size)}, shown in full]")
        with open(args.file, encoding="utf-8", errors="replace") as f:
            for line in f:
                sys.stdout.write(redact_text(line))
        return
    out = []
    try:
        if kind == "binary":
            out.append("binary file; not summarized (try `file` or a format-specific tool)")
        else:
            summarize = {"json": peek_json, "jsonl": peek_jsonl, "csv": peek_csv, "sqlite": peek_sqlite, "text": peek_text}
            summarize[kind](args.file, args, out)
    except (ValueError, KeyError, IndexError, TypeError) as e:
        if kind == "json" and not args.path:
            out = ["(invalid JSON as a whole; falling back to line view) " + str(e)[:120]]
            kind = "jsonl"
            peek_jsonl(args.file, args, out)
        else:
            sys.exit(f"peek: {type(e).__name__}: {e}")
    body = "\n".join(out)
    if len(body) > 12000:
        body = body[:12000] + "\n… (output capped; use --path to drill in)"
    if args.json:
        print(json.dumps({"file": args.file, "kind": kind, "size": size, "summary": body}))
    else:
        print(f"{args.file}  [{kind}, {human(size)}]")
        print(body)


if __name__ == "__main__":
    main()
