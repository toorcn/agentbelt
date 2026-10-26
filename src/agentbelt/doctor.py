#!/usr/bin/env python3
"""doctor: one read-only command that tells an agent how a repo works and what's broken.

Usage:
  doctor [DIR]          human/agent-readable report
  doctor [DIR] --json   structured report

Reports: stack, required vs installed toolchain, whether deps are installed,
how to install/dev/build/test/lint, env vars that are expected but missing
(names only, never values), services/ports, git state, and a problem list.
Never writes, installs, or runs project code.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys

SKIP_DIRS = {"node_modules", ".git", ".venv", "venv", "dist", "build", ".next", "__pycache__", ".turbo", "target",
             ".cache", "coverage", ".vercel"}
NON_SOURCE = {"output", "out", "vendor", "third_party", "fixtures", "examples", "tmp", "temp", "generated"}
CODE_EXT = {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".py", ".go", ".rs", ".rb", ".svelte", ".vue"}
TEST_FILE = re.compile(r"(^|/)(tests?|__tests__|spec)/|\.(test|spec)\.\w+$|(^|/)test_[^/]+\.py$|_test\.(go|py)$")
VERSION = re.compile(r"\d+\.\d+(\.\d+)?")
ENV_REF = re.compile(
    r"process\.env\.([A-Z][A-Z0-9_]{2,})"
    r"|process\.env\[['\"]([A-Z][A-Z0-9_]{2,})['\"]\]"
    r"|import\.meta\.env\.([A-Z][A-Z0-9_]{2,})"
    r"|os\.environ(?:\.get)?\(?\[?['\"]([A-Z][A-Z0-9_]{2,})['\"]"
    r"|os\.getenv\(['\"]([A-Z][A-Z0-9_]{2,})['\"]"
    r"|env::var\(\"([A-Z][A-Z0-9_]{2,})\"\)"
    r"|os\.Getenv\(\"([A-Z][A-Z0-9_]{2,})\"\)"
)
# A read is optional if it has a fallback, is compared as a flag, or is guarded by a condition.
ENV_FALLBACK_AFTER = re.compile(r"""['"]?\]?(\?\.\w+\([^)]*\))?\)*\s*(\?\?|\|\||\?[^.?]|&&|[!=]==?|,\s*['"\w(\[{-])""")
ENV_GUARD_BEFORE = re.compile(r"(if\s*\(\s*!?|&&\s*|\|\|\s*|!!|Boolean\()$")
# Set by the runtime or platform, not by the developer.
ENV_BUILTIN = {
    "NODE_ENV", "PORT", "HOME", "PATH", "PWD", "USER", "SHELL", "TZ", "LANG", "TERM", "HOSTNAME", "TMPDIR", "DEBUG", "CI",
    "NEXT_RUNTIME", "NEXT_PHASE", "VITE_USER_NODE_ENV", "MODE", "DEV", "PROD", "SSR", "BASE_URL",
    "VERCEL", "VERCEL_ENV", "VERCEL_URL", "GITHUB_ACTIONS", "RAILWAY_ENVIRONMENT", "FLY_APP_NAME", "RENDER", "NETLIFY",
    "SYSTEMROOT", "APPDATA", "USERPROFILE", "COMSPEC",
}
SCRIPT_ROLES = [
    ("dev", ["dev", "start:dev", "serve", "develop", "start"]),
    ("build", ["build"]),
    ("test", ["test", "test:unit", "tests"]),
    ("lint", ["lint"]),
    ("typecheck", ["typecheck", "type-check", "tsc", "check-types", "check"]),
    ("format", ["format", "fmt"]),
]


def run(cmd, cwd=None, timeout=5):
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip() if r.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def read(path, limit=2_000_000):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except OSError:
        return None


def load_json(path):
    try:
        return json.loads(read(path) or "")
    except ValueError:
        return None


def env_keys(path):
    text = read(path)
    if text is None:
        return None
    keys = set()
    for line in text.splitlines():
        m = re.match(r"\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)", line)
        if m:
            keys.add((m.group(1), bool(m.group(2).strip().strip("'\""))))
    return keys


def version_tuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:3])


def satisfies(installed, spec):
    """Very small semver-range check: handles >=X, ^X, ~X, X, X.x, ranges joined by ||."""
    if not installed or not spec:
        return None
    iv = version_tuple(installed)
    for alt in spec.split("||"):
        ok = True
        for part in alt.split():
            m = re.match(r"(>=|<=|>|<|\^|~|=)?v?(\d+(?:\.\d+)*)", part.strip())
            if not m:
                continue
            op, want = m.group(1) or "", version_tuple(m.group(2))
            n = len(want)
            if op == ">=":
                ok &= iv >= want
            elif op == ">":
                ok &= iv > want
            elif op == "<":
                ok &= iv < want
            elif op == "<=":
                ok &= iv <= want
            elif op == "^":
                ok &= iv >= want and iv[0] == want[0]
            else:  # ~, =, bare
                ok &= iv[:n] == want if op != "~" else (iv >= want and iv[:2] == want[:2])
        if ok:
            return True
    return False


class Report:
    def __init__(self, root):
        self.root = root
        self.stack = []
        self.toolchain = []   # (tool, wanted, installed, source, ok)
        self.commands = {}    # role -> command
        self.extra_commands = []
        self.env = {}
        self.services = []
        self.git = {}
        self.problems = []    # (severity, msg, fix)
        self.notes = []

    def problem(self, sev, msg, fix=None):
        self.problems.append((sev, msg, fix))


def parse_version(text):
    if not text:
        return None
    m = VERSION.search(text)
    return m.group(0) if m else text.splitlines()[0]


def tool_version(name, flag="--version"):
    return parse_version(run([name, flag]))


def exists(root, *names):
    return [n for n in names if os.path.exists(os.path.join(root, n))]


def check_node(rep, root):
    pkg = load_json(os.path.join(root, "package.json"))
    if pkg is None:
        return
    rep.stack.append("node")
    deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
    for fw, label in [("next", "next.js"), ("vite", "vite"), ("@remix-run/react", "remix"), ("astro", "astro"),
                      ("@sveltejs/kit", "sveltekit"), ("nuxt", "nuxt"), ("expo", "expo"), ("electron", "electron"),
                      ("express", "express"), ("hono", "hono"), ("@nestjs/core", "nestjs"), ("react", "react"),
                      ("vue", "vue"), ("svelte", "svelte"), ("typescript", "typescript"), ("prisma", "prisma"),
                      ("drizzle-orm", "drizzle"), ("tailwindcss", "tailwind"), ("three", "three.js")]:
        if fw in deps:
            rep.stack.append(f"{label}@{deps[fw].lstrip('^~')}")

    # package manager
    locks = {"pnpm-lock.yaml": "pnpm", "yarn.lock": "yarn", "bun.lockb": "bun", "bun.lock": "bun", "package-lock.json": "npm"}
    lockfiles = exists(root, *locks)
    managers = list(dict.fromkeys(locks[f] for f in lockfiles))
    pm_field = pkg.get("packageManager", "")
    pm_name, _, pm_want = pm_field.partition("@")
    pm = pm_name or (managers[0] if managers else "npm")
    if len(managers) > 1:
        rep.problem("warn", f"multiple lockfiles ({', '.join(lockfiles)})", f"use {pm}; delete the others once confirmed")
    if not lockfiles:
        rep.notes.append("no lockfile: installs are not reproducible")
    pm_ver = tool_version(pm)
    pm_source = "packageManager" if pm_field else "lockfile" if lockfiles else "default"
    rep.toolchain.append((pm, pm_want or None, pm_ver, pm_source, pm_ver is not None))
    if pm_ver is None:
        rep.problem("error", f"{pm} is not installed", f"corepack enable  (or npm i -g {pm})")

    # node version
    want, src = None, None
    for f in (".nvmrc", ".node-version"):
        t = (read(os.path.join(root, f)) or "").strip()
        if t:
            want, src = t, f
            break
    if not want and pkg.get("engines", {}).get("node"):
        want, src = pkg["engines"]["node"], "engines.node"
    if not want and pkg.get("volta", {}).get("node"):
        want, src = pkg["volta"]["node"], "volta"
    node_ver = tool_version("node")
    if not want or want.lower() in ("lts/*", "lts", "node", "latest"):
        ok = True
    else:
        ok = satisfies(node_ver, want)
    rep.toolchain.append(("node", want, node_ver, src, bool(ok) and node_ver is not None))
    if node_ver is None:
        rep.problem("error", "node is not installed")
    elif ok is False:
        rep.problem("error", f"node {node_ver} does not satisfy {want} ({src})", f"nvm use  /  nvm install {want}")

    # dependencies
    nm = os.path.join(root, "node_modules")
    install = {"npm": "npm ci" if managers == ["npm"] else "npm install",
               "pnpm": "pnpm install", "yarn": "yarn install", "bun": "bun install"}[pm]
    rep.commands["install"] = install
    # npm records each install in node_modules/.package-lock.json; older than the lockfile means stale
    hidden_lock = os.path.join(nm, ".package-lock.json")
    if not os.path.isdir(nm):
        rep.problem("error", "dependencies not installed (no node_modules)", install)
    elif ("package-lock.json" in lockfiles and os.path.exists(hidden_lock)
          and os.path.getmtime(os.path.join(root, "package-lock.json")) > os.path.getmtime(hidden_lock) + 5):
        rep.problem("warn", "lockfile is newer than node_modules: deps may be stale", install)
    else:
        missing = [d for d in list(deps)[:200] if not os.path.exists(os.path.join(nm, d))]
        if missing:
            examples = ", ".join(missing[:4])
            rep.problem("error", f"{len(missing)} declared deps missing from node_modules (e.g. {examples})", install)

    # scripts
    scripts = pkg.get("scripts", {})
    run_prefix = "npm run" if pm == "npm" else pm
    for role, names in SCRIPT_ROLES:
        for n in names:
            if n in scripts and role not in rep.commands:
                rep.commands[role] = f"{run_prefix} {n}"
                break
    if "test" in scripts and re.search(r"no test specified", scripts["test"]):
        rep.commands.pop("test", None)
        rep.notes.append("`test` script is the npm placeholder: there are no tests")
    for n, cmd in scripts.items():
        m = re.search(r"(?:-p|--port)[ =](\d{2,5})", cmd)
        if m:
            rep.services.append(f"script `{n}` serves on port {m.group(1)}")
    known = {c.split()[-1] for c in rep.commands.values()}
    others = [n for n in scripts if n not in known and not n.startswith(("pre", "post"))]
    if others:
        rep.extra_commands.append(f"other scripts: {', '.join(others[:15])}{' …' if len(others) > 15 else ''}")

    # workspaces / monorepo
    ws = pkg.get("workspaces")
    if isinstance(ws, dict):
        ws = ws.get("packages")
    if ws or exists(root, "pnpm-workspace.yaml", "turbo.json"):
        rep.stack.append("monorepo" + (f" ({', '.join(ws)})" if ws else ""))
    dev_script = scripts.get(rep.commands.get("dev", "").split()[-1], "") if "dev" in rep.commands else ""
    if not re.search(r"(?:-p|--port)[ =]\d", dev_script):
        if any(s.startswith("next.js") for s in rep.stack) and dev_script:
            rep.services.append("next.js dev server defaults to http://localhost:3000")
        elif any(s.startswith("vite") for s in rep.stack):
            rep.services.append("vite dev server defaults to http://localhost:5173")


def check_python(rep, root):
    pyproject = read(os.path.join(root, "pyproject.toml"))
    reqs = exists(root, "requirements.txt", "requirements-dev.txt", "requirements/dev.txt")
    if not (pyproject or reqs or exists(root, "setup.py")):
        return
    rep.stack.append("python")
    uv = bool(exists(root, "uv.lock"))
    poetry = bool(exists(root, "poetry.lock")) or "[tool.poetry" in (pyproject or "")
    tool = "uv" if uv else "poetry" if poetry else "pip"
    want, src = None, None
    t = read(os.path.join(root, ".python-version"))
    if t and t.strip():
        want, src = t.strip().splitlines()[0], ".python-version"
    elif pyproject:
        m = re.search(r'requires-python\s*=\s*"([^"]+)"', pyproject)
        if m:
            want, src = m.group(1), "requires-python"
    venv = next((d for d in (".venv", "venv", "env") if os.path.exists(os.path.join(root, d, "pyvenv.cfg"))), None)
    py = os.path.join(root, venv, "bin", "python") if venv else (shutil.which("python3") or "python3")
    py_ver = tool_version(py)
    ok = satisfies(py_ver, want.replace(",", " ")) if want else True
    rep.toolchain.append(("python" + (f" ({venv})" if venv else ""), want, py_ver, src, bool(ok) and py_ver is not None))
    if ok is False:
        rep.problem("error", f"python {py_ver} does not satisfy {want} ({src})", f"uv python install {want}" if uv else None)
    if tool != "pip":
        tv = tool_version(tool)
        rep.toolchain.append((tool, None, tv, "lockfile", tv is not None))
        if tv is None:
            fix = "curl -LsSf https://astral.sh/uv/install.sh | sh" if tool == "uv" else "pipx install poetry"
            rep.problem("error", f"{tool} is not installed", fix)
    if tool == "uv":
        install = "uv sync"
    elif tool == "poetry":
        install = "poetry install"
    else:
        target = f"-r {reqs[0]}" if reqs else "-e ."
        install = f"python3 -m venv .venv && .venv/bin/pip install {target}"
    # in a mixed Node + Python repo, the Node install command stays primary
    is_mixed = "node" in rep.stack
    rep.commands.setdefault("install", install)
    if not venv and tool != "poetry":
        rep.problem("warn" if is_mixed else "error", "no virtualenv found (.venv/venv)", install)
    deps_text = (pyproject or "") + "".join(read(os.path.join(root, r)) or "" for r in reqs)
    for fw in ("django", "fastapi", "flask", "streamlit", "pytest", "torch", "langchain", "anthropic", "openai"):
        if re.search(rf"(?i)\b{fw}\b", deps_text):
            rep.stack.append(fw)
    runner = {"uv": "uv run ", "poetry": "poetry run "}.get(tool, ".venv/bin/" if venv else "")
    if "pytest" in deps_text or os.path.isdir(os.path.join(root, "tests")):
        rep.commands.setdefault("test", f"{runner}pytest")
    if "ruff" in deps_text:
        rep.commands.setdefault("lint", f"{runner}ruff check .")
    if "fastapi" in rep.stack:
        entry = exists(root, "main.py", "app/main.py", "src/main.py", "app.py", "server.py")
        if entry:
            module = entry[0][:-3].replace("/", ".")
            rep.commands.setdefault("dev", f"{runner}uvicorn {module}:app --reload")
            rep.services.append("uvicorn defaults to http://127.0.0.1:8000")
    if "django" in rep.stack and exists(root, "manage.py"):
        rep.commands.setdefault("dev", f"{runner}python manage.py runserver")
    if "streamlit" in rep.stack:
        entry = exists(root, "app.py", "streamlit_app.py", "main.py")
        if entry:
            rep.commands.setdefault("dev", f"{runner}streamlit run {entry[0]}")


def check_other(rep, root):
    def has(f):
        return os.path.exists(os.path.join(root, f))
    ruby_test = "bundle exec rspec" if has("spec") else "bundle exec rake test"
    for marker, lang, tool, cmds in [
        ("Cargo.toml", "rust", "cargo", {"build": "cargo build", "test": "cargo test", "dev": "cargo run"}),
        ("go.mod", "go", "go", {"build": "go build ./...", "test": "go test ./...", "dev": "go run ."}),
        ("Gemfile", "ruby", "bundle", {"install": "bundle install", "test": ruby_test}),
        ("pubspec.yaml", "flutter/dart", "flutter", {"install": "flutter pub get", "test": "flutter test", "dev": "flutter run"}),
        ("Package.swift", "swift", "swift", {"build": "swift build", "test": "swift test"}),
    ]:
        if has(marker):
            rep.stack.append(lang)
            for k, v in cmds.items():
                rep.commands.setdefault(k, v)
            version = tool_version(tool, "version" if tool == "go" else "--version")
            rep.toolchain.append((tool, None, version, marker, version is not None))
            if version is None:
                rep.problem("error", f"{tool} is not installed")
    xcode = [f for f in os.listdir(root) if f.endswith((".xcodeproj", ".xcworkspace"))]
    if xcode:
        rep.stack.append(f"xcode ({xcode[0]})")
    mk = read(os.path.join(root, "Makefile"))
    if mk:
        targets = [t for t in re.findall(r"^([a-zA-Z][\w.-]*):(?!=)", mk, re.M) if t not in (".PHONY",)]
        rep.extra_commands.append("make targets: " + ", ".join(dict.fromkeys(targets[:15])))
        for role in ("dev", "build", "test", "lint", "install"):
            if role in targets:
                rep.commands.setdefault(role, f"make {role}")
    jf = read(os.path.join(root, "justfile")) or read(os.path.join(root, "Justfile"))
    if jf:
        recipes = re.findall(r"^([a-zA-Z][\w-]*)\s*(?:[^\n:=]*):(?!=)", jf, re.M)
        rep.extra_commands.append("just recipes: " + ", ".join(recipes[:15]))
    for dc in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml"):
        t = read(os.path.join(root, dc))
        if t:
            svc = re.findall(r"^  ([a-zA-Z0-9_-]+):\s*$", t.split("services:", 1)[-1], re.M)
            ports = re.findall(r"['\"]?(\d{2,5}):\d{2,5}['\"]?", t)
            line = f"{dc}: services {', '.join(svc[:8])}"
            if ports:
                line += f"; host ports {', '.join(dict.fromkeys(ports))}"
            rep.services.append(line)
            if not shutil.which("docker"):
                rep.problem("warn", f"{dc} present but docker is not installed")
            break
    if has("Dockerfile"):
        rep.stack.append("docker")
    for f in ("vercel.json", "netlify.toml", "fly.toml", "railway.json", "render.yaml", "wrangler.toml", "wrangler.jsonc"):
        if has(f):
            rep.stack.append("deploy:" + f.split(".")[0])


def check_ci(rep, root):
    wf = os.path.join(root, ".github", "workflows")
    if not os.path.isdir(wf):
        return
    runs = []
    for f in sorted(os.listdir(wf))[:6]:
        t = read(os.path.join(wf, f)) or ""
        for m in re.finditer(r"^\s*-?\s*run:[ \t]*([^|>\s].*)$", t, re.M):
            c = m.group(1).strip().strip("'\"")
            if not re.match(r"(echo|cd |ls|cat |mkdir|export )", c):
                runs.append(c)
    if runs:
        rep.extra_commands.append("CI runs: " + "; ".join(list(dict.fromkeys(runs))[:8]))


def more(items, shown):
    return " …" if len(items) > shown else ""


def source_files(root, limit=5000):
    """Code files worth scanning. Uses git when possible so .gitignore'd output doesn't count."""
    def keep_dir(part):
        return part not in SKIP_DIRS and part not in NON_SOURCE and not part.startswith(".")

    listed = run(["git", "ls-files", "-co", "--exclude-standard"], cwd=root, timeout=10)
    if listed is not None:
        paths = [p for p in listed.splitlines()
                 if os.path.splitext(p)[1] in CODE_EXT and all(keep_dir(d) for d in p.split("/")[:-1])]
        return paths[:limit]
    paths = []
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if keep_dir(d)]
        paths += [os.path.relpath(os.path.join(dirpath, f), root) for f in files if os.path.splitext(f)[1] in CODE_EXT]
        if len(paths) >= limit:
            break
    return paths[:limit]


def scan_env(rep, root):
    examples = exists(root, ".env.example", ".env.sample", ".env.template", ".env.local.example", ".env.dist", "example.env")
    actual_files = exists(root, ".env", ".env.local", ".env.development", ".env.development.local")
    expected, actual, empty, commented = set(), set(), set(), set()
    for f in examples:
        expected |= {k for k, _ in env_keys(os.path.join(root, f)) or ()}
        commented |= set(re.findall(r"^\s*#\s*([A-Z][A-Z0-9_]{2,})\s*=", read(os.path.join(root, f)) or "", re.M))
    for f in actual_files:
        for k, has_val in env_keys(os.path.join(root, f)) or ():
            actual.add(k)
            if not has_val:
                empty.add(k)
    referenced, optional = {}, set()
    for rel in source_files(root):
        if TEST_FILE.search(rel):  # tests usually set their own env
            continue
        text = read(os.path.join(root, rel), 400_000) or ""
        for m in ENV_REF.finditer(text):
            k = next(g for g in m.groups() if g)
            referenced.setdefault(k, rel)
            after = text[m.end(): m.end() + 60]
            before = text[max(0, m.start() - 30): m.start()]
            if ENV_FALLBACK_AFTER.match(after) or ENV_GUARD_BEFORE.search(before):
                optional.add(k)
    referenced = {k: v for k, v in referenced.items() if k not in ENV_BUILTIN}
    unset = [k for k in (expected | set(referenced)) if k not in actual and k not in os.environ]
    # commented-out entries in an example file document optional settings
    missing = sorted(k for k in unset if k not in optional and not (k in commented and k not in expected))
    missing_optional = sorted(k for k in unset if k not in missing)
    rep.env = {
        "env files": actual_files or ["(none)"],
        "example files": examples or ["(none)"],
        "referenced in code": len(referenced),
        "missing": missing,
        "missing (has fallback)": missing_optional,
        "empty": sorted(empty & (expected | set(referenced))),
    }
    if missing:
        detail = ", ".join(k + (f" ({referenced[k]})" if k in referenced else "") for k in missing[:12])
        if examples and not actual_files:
            target = ".env.local" if any(s.startswith("next.js") for s in rep.stack) else ".env"
            fix = f"cp {examples[0]} {target} and fill in values"
        else:
            fix = "add them to the env file"
        rep.problem("warn", f"{len(missing)} env vars required but not set: {detail}{more(missing, 12)}", fix)
    if missing_optional:
        names = ", ".join(missing_optional[:8])
        rep.notes.append(f"unset but with a fallback in code ({len(missing_optional)}): {names}{more(missing_optional, 8)}")
    if referenced and not examples:
        rep.notes.append("code reads env vars but there is no .env.example to document them")
    if os.path.exists(os.path.join(root, ".env")) and run(["git", "rev-parse", "--is-inside-work-tree"], cwd=root) == "true":
        gi = read(os.path.join(root, ".gitignore")) or ""
        tracked = run(["git", "ls-files", "--error-unmatch", ".env"], cwd=root) is not None
        if tracked:
            rep.problem("error", ".env is committed to git (secrets exposure)", "git rm --cached .env && echo .env >> .gitignore")
        elif not re.search(r"^\.env", gi, re.M):
            rep.problem("warn", ".env is not in .gitignore", "echo .env >> .gitignore")


def check_git(rep, root):
    if run(["git", "rev-parse", "--is-inside-work-tree"], cwd=root) != "true":
        rep.git = {"repo": False}
        return
    branch = run(["git", "branch", "--show-current"], cwd=root) or "(detached)"
    status = run(["git", "--no-optional-locks", "status", "--porcelain"], cwd=root) or ""
    lines = [l for l in status.splitlines() if l]
    upstream = run(["git", "rev-parse", "--abbrev-ref", "@{u}"], cwd=root)
    ab = run(["git", "rev-list", "--left-right", "--count", "HEAD...@{u}"], cwd=root) if upstream else None
    last = run(["git", "log", "-1", "--format=%cr: %s"], cwd=root)
    untracked = sum(1 for l in lines if l.startswith("??"))
    rep.git = {
        "branch": branch,
        "uncommitted": f"{len(lines) - untracked} modified, {untracked} untracked",
        "upstream": upstream or "(none)",
        "last commit": last,
    }
    if ab:
        a, b = ab.split()
        rep.git["ahead/behind"] = f"{a}/{b} (as of last fetch)"
    if any(l[:2] in ("UU", "AA", "DD") for l in lines):
        rep.problem("error", "unresolved merge conflicts")


def detect_subprojects(root):
    """If root has no manifest but children do (e.g. client/ + server/), report them."""
    markers = ("package.json", "pyproject.toml", "requirements.txt", "Cargo.toml", "go.mod", "pubspec.yaml")
    if any(os.path.exists(os.path.join(root, m)) for m in markers):
        return []
    subs = []
    for d in sorted(os.listdir(root)):
        p = os.path.join(root, d)
        if os.path.isdir(p) and d not in SKIP_DIRS and not d.startswith("."):
            if any(os.path.exists(os.path.join(p, m)) for m in markers):
                subs.append(d)
    return subs


def diagnose(root):
    rep = Report(root)
    check_node(rep, root)
    check_python(rep, root)
    check_other(rep, root)
    check_ci(rep, root)
    scan_env(rep, root)
    check_git(rep, root)
    return rep


def fmt(rep, label=None):
    o = [f"## {label or os.path.basename(rep.root) or rep.root}"]
    o.append("stack: " + (", ".join(dict.fromkeys(rep.stack)) or "unknown (no manifest found)"))
    if rep.toolchain:
        o.append("toolchain:")
        for tool, want, have, src, ok in rep.toolchain:
            o.append(f"  {'ok ' if ok else 'BAD'} {tool}: have {have or 'MISSING'}" + (f", want {want} ({src})" if want else ""))
    if rep.commands:
        o.append("commands:")
        for role in ("install", "dev", "build", "test", "lint", "typecheck", "format"):
            if role in rep.commands:
                o.append(f"  {role:<9} {rep.commands[role]}")
    for e in rep.extra_commands:
        o.append(f"  {e}")
    if rep.services:
        o.append("services: " + "; ".join(dict.fromkeys(rep.services)))
    env = rep.env
    if env and (env["referenced in code"] or env["example files"] != ["(none)"]):
        o.append(f"env: files {', '.join(env['env files'])}; examples {', '.join(env['example files'])}; "
                 f"{env['referenced in code']} vars read in code")
    if rep.git.get("repo") is False:
        o.append("git: not a repository")
    elif rep.git:
        o.append("git: " + "; ".join(f"{k} {v}" for k, v in rep.git.items()))
    if rep.problems:
        o.append("problems:")
        for sev, msg, fix in sorted(rep.problems, key=lambda p: p[0] != "error"):
            o.append(f"  [{sev}] {msg}" + (f"\n          fix: {fix}" if fix else ""))
    else:
        o.append("problems: none found")
    for n in rep.notes:
        o.append(f"note: {n}")
    return "\n".join(o)


def to_dict(rep):
    return {
        "root": rep.root, "stack": list(dict.fromkeys(rep.stack)),
        "toolchain": [dict(zip(("tool", "want", "have", "source", "ok"), t)) for t in rep.toolchain],
        "commands": rep.commands, "other": rep.extra_commands, "services": rep.services,
        "env": rep.env, "git": rep.git,
        "problems": [dict(zip(("severity", "message", "fix"), p)) for p in rep.problems], "notes": rep.notes,
    }


def main():
    ap = argparse.ArgumentParser(prog="doctor", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dir", nargs="?", default=".")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    root = os.path.abspath(args.dir)
    if not os.path.isdir(root):
        sys.exit(f"doctor: not a directory: {args.dir}")
    subs = detect_subprojects(root)
    if not subs:
        rep = diagnose(root)
        print(json.dumps(to_dict(rep), indent=1) if args.json else fmt(rep))
        return
    reports = [(diagnose(os.path.join(root, s)), s + "/") for s in subs]
    if args.json:
        print(json.dumps([to_dict(r) for r, _ in reports], indent=1))
        return
    print(f"no manifest at root; found {len(subs)} sub-projects: {', '.join(subs)}\n")
    print("\n\n".join(fmt(r, label) for r, label in reports))


if __name__ == "__main__":
    main()
