"""Which application uses which AI model, and how much.

The System tab answers "what is this machine doing"; this one answers the
question one layer up — *which of my applications is talking to which model*.
That question has no single source of truth on a developer machine, so it is
assembled from four, each of which knows something the others cannot:

* **Declared** — a model written down against an application by hand. Beats
  every guess below, and is the only source that can be right about an app
  that has not run yet.
* **Detected** — a scan of the application's own folder: which SDK it imports,
  which ``*_API_KEY`` its environment names, which model ids appear in its
  source. Says what an app *can* reach before it reaches anything, which is
  why the tab has rows on a machine that has made no calls today.
* **Live** — the application's process tree, walked for open sockets to a
  provider's address. This is the only source that can say *right now*, and
  the only one that cannot be fooled by a stale import. It cannot count calls:
  one HTTP/2 connection carries many, so a connection means "talking", never
  "talked N times".
* **Measured** — the count, from the two places a real number exists. Apps that
  post to ``/api/ai/usage`` report exactly what their SDK told them. Everything
  else has its captured log read for the usage blocks the SDKs already print,
  which is how an app that was never instrumented still gets a token count.

Nothing here intercepts, proxies or decrypts traffic. An unmeasured app shows
as *connected* with no token count rather than being silently guessed at — the
tab is meant to be trusted, so what it does not know it says it does not know.
"""

import json
import os
import re
import socket
import sqlite3
from collections import namedtuple
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import log_path
from .database import db, utcnow

try:
    import psutil
except ImportError:  # pragma: no cover - the System tab reports this too
    psutil = None

# The live half is walked on this timer; usage rows arrive as they happen.
SAMPLE_SECONDS = 15.0
RETENTION_HOURS = 24
# Detection re-scans a folder at most this often. Source files do not sprout
# new SDKs between two ticks, and walking every app's tree on a 15s timer
# would make the monitor the busiest thing on the machine.
RESCAN_SECONDS = 900.0
# Provider hostnames resolve to CDN addresses that do move, but not quickly.
DNS_TTL_SECONDS = 600.0

# Walk guards. A node_modules tree is thousands of files deep and none of them
# are the application; these caps keep a scan to well under a second.
SCAN_MAX_FILES = 400
SCAN_MAX_BYTES = 400_000
SCAN_MAX_DEPTH = 4
SKIP_DIRS = {
    "node_modules", ".git", ".venv", "venv", "env", "__pycache__", "dist",
    "build", ".next", ".nuxt", "target", "vendor", ".cache", "coverage",
    ".mypy_cache", ".pytest_cache", "site-packages", ".tox", "out",
}
CODE_SUFFIXES = {".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".go",
                 ".rb", ".rs", ".java", ".php", ".sh", ".env", ".json",
                 ".yaml", ".yml", ".toml", ".ini", ".txt", ".md"}


# ------------------------------------------------------------------ providers


class Provider:
    """One place a model can be reached, and how to recognise an app using it.

    ``hosts`` is what makes the live half work: the addresses these names
    resolve to are the addresses an application's socket will be pointing at.
    ``ports`` does the same job for a model served on this machine, where there
    is no hostname to resolve — only a port that is, by convention, that
    runtime's.
    """

    __slots__ = ("key", "label", "hosts", "ports", "env_keys", "packages",
                 "markers", "local", "domain", "host_labels")

    def __init__(self, key, label, hosts=(), ports=(), env_keys=(), packages=(),
                 markers=(), local=False, domain="", host_labels=()):
        self.key = key
        self.label = label
        self.hosts = tuple(hosts)
        self.ports = tuple(ports)
        self.env_keys = tuple(env_keys)
        self.packages = tuple(packages)
        self.markers = tuple(markers)
        self.local = local
        # A strict DNS suffix, matched at a label boundary. Never a substring:
        # `x.ai` as a substring also matches `matrix.ai`.
        self.domain = domain
        # For providers with no suffix of their own — Bedrock and Vertex live
        # under a shared cloud domain, and matching that domain would file
        # every S3 or Cloud Storage request as an AI call.
        self.host_labels = tuple(host_labels)


PROVIDERS: dict[str, Provider] = {
    p.key: p
    for p in [
        Provider(
            "anthropic", "Anthropic",
            hosts=("api.anthropic.com",),
            env_keys=("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_API_KEY"),
            packages=("anthropic", "@anthropic-ai/sdk", "@anthropic-ai/claude-agent-sdk"),
            markers=("import anthropic", "from anthropic", "@anthropic-ai/", "Anthropic("),
            domain="anthropic.com",
        ),
        Provider(
            "openai", "OpenAI",
            hosts=("api.openai.com",),
            env_keys=("OPENAI_API_KEY", "OPENAI_API_BASE", "OPENAI_ORG_ID"),
            packages=("openai", "langchain-openai", "langchain_openai"),
            markers=("import openai", "from openai", "OpenAI(", "openai.ChatCompletion"),
            domain="openai.com",
        ),
        Provider(
            "google", "Google",
            hosts=("generativelanguage.googleapis.com", "aiplatform.googleapis.com"),
            env_keys=("GOOGLE_API_KEY", "GEMINI_API_KEY", "GOOGLE_GENAI_API_KEY",
                      "GOOGLE_APPLICATION_CREDENTIALS"),
            packages=("google-generativeai", "google-genai", "@google/generative-ai"),
            markers=("google.generativeai", "from google import genai", "GenerativeModel("),
            host_labels=("generativelanguage", "aiplatform"),
        ),
        Provider(
            "mistral", "Mistral",
            hosts=("api.mistral.ai",),
            env_keys=("MISTRAL_API_KEY",),
            packages=("mistralai", "@mistralai/mistralai"),
            markers=("from mistralai", "import mistralai"),
            domain="mistral.ai",
        ),
        Provider(
            "cohere", "Cohere",
            hosts=("api.cohere.com", "api.cohere.ai"),
            env_keys=("COHERE_API_KEY", "CO_API_KEY"),
            packages=("cohere",),
            markers=("import cohere", "from cohere"),
            domain="cohere.com",
        ),
        Provider(
            "groq", "Groq",
            hosts=("api.groq.com",),
            env_keys=("GROQ_API_KEY",),
            packages=("groq",),
            markers=("from groq", "import groq"),
            domain="groq.com",
        ),
        Provider(
            "deepseek", "DeepSeek",
            hosts=("api.deepseek.com",),
            env_keys=("DEEPSEEK_API_KEY",),
            markers=("api.deepseek.com",),
            domain="deepseek.com",
        ),
        Provider(
            "xai", "xAI",
            hosts=("api.x.ai",),
            env_keys=("XAI_API_KEY", "GROK_API_KEY"),
            markers=("api.x.ai",),
            domain="x.ai",
        ),
        Provider(
            "openrouter", "OpenRouter",
            hosts=("openrouter.ai",),
            env_keys=("OPENROUTER_API_KEY",),
            markers=("openrouter.ai",),
            domain="openrouter.ai",
        ),
        Provider(
            "together", "Together",
            hosts=("api.together.xyz",),
            env_keys=("TOGETHER_API_KEY",),
            packages=("together",),
            markers=("api.together.xyz",),
            domain="together.xyz",
        ),
        Provider(
            "perplexity", "Perplexity",
            hosts=("api.perplexity.ai",),
            env_keys=("PERPLEXITY_API_KEY", "PPLX_API_KEY"),
            markers=("api.perplexity.ai",),
            domain="perplexity.ai",
        ),
        Provider(
            "huggingface", "Hugging Face",
            hosts=("api-inference.huggingface.co", "huggingface.co"),
            env_keys=("HF_TOKEN", "HUGGINGFACE_API_KEY", "HUGGINGFACEHUB_API_TOKEN"),
            packages=("transformers", "huggingface_hub"),
            markers=("from transformers", "huggingface_hub"),
            domain="huggingface.co",
        ),
        Provider(
            "azure", "Azure OpenAI",
            hosts=(),
            env_keys=("AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT"),
            markers=("azure.openai", "AzureOpenAI("),
            domain="openai.azure.com",
        ),
        Provider(
            "bedrock", "AWS Bedrock",
            hosts=(),
            env_keys=("AWS_BEARER_TOKEN_BEDROCK", "CLAUDE_CODE_USE_BEDROCK"),
            markers=("bedrock-runtime", "boto3.client(\"bedrock"),
            host_labels=("bedrock",),
        ),
        # Served on this machine. No hostname to resolve — the port is the tell.
        Provider(
            "ollama", "Ollama (local)",
            ports=(11434,),
            env_keys=("OLLAMA_HOST", "OLLAMA_BASE_URL"),
            packages=("ollama",),
            markers=("localhost:11434", "127.0.0.1:11434", "import ollama"),
            local=True,
        ),
        Provider(
            "lmstudio", "LM Studio (local)",
            ports=(1234,),
            env_keys=("LMSTUDIO_HOST",),
            markers=("localhost:1234/v1", "127.0.0.1:1234/v1"),
            local=True,
        ),
        Provider(
            "vllm", "vLLM (local)",
            ports=(8000, 8001),
            packages=("vllm",),
            markers=("import vllm", "from vllm"),
            local=True,
        ),
        Provider(
            "llamacpp", "llama.cpp (local)",
            ports=(8080,),
            packages=("llama-cpp-python",),
            markers=("llama_cpp", "llama-server"),
            local=True,
        ),
    ]
}

# Model ids as they are actually written in code and config.
#
# Every pattern insists on the shape a real model id has — a version number, a
# family word, or an Ollama `name:tag` — and none will match a bare family
# word on its own. That strictness is the point: an earlier, looser version of
# this table read the words *philosophy* and *philippines* out of a README and
# filed them as models. A tab that invents a model nobody runs is worse than
# one that misses an unusual name, so these err towards missing.
MODEL_PATTERNS: list[tuple[re.Pattern, str]] = [
    # claude-3-5-sonnet-20241022 · claude-opus-4-1 · claude-sonnet-4-5 · claude-haiku-4-5
    (re.compile(
        r"\bclaude-(?:(?:\d[\d.]*-)+(?:opus|sonnet|haiku)|(?:opus|sonnet|haiku|instant)-\d[\d.]*)"
        r"(?:-[a-z0-9]+)*\b", re.I), "anthropic"),
    # gpt-4o-mini · gpt-4.1-nano · gpt-3.5-turbo · gpt-5
    (re.compile(r"\bgpt-(?:3\.5|4o|4\.1|4|5(?:\.\d)?)(?:[.\-][a-z0-9]+)*\b", re.I), "openai"),
    # The reasoning line. A suffix is required — a bare "o3" in prose is far
    # more often a footnote marker than a model.
    (re.compile(r"\bo[1345]-(?:mini|pro|preview|high|low|deep-research)\b", re.I), "openai"),
    (re.compile(r"\btext-embedding-(?:3-(?:small|large)|ada-002)\b", re.I), "openai"),
    # gemini-2.5-flash · gemini-1.5-pro · gemini-pro
    (re.compile(r"\bgemini-(?:\d[\d.]*|pro|ultra|nano)(?:-[a-z0-9]+)*\b", re.I), "google"),
    (re.compile(r"\bmistral-(?:tiny|small|medium|large|nemo|saba)(?:-[a-z0-9]+)*\b", re.I), "mistral"),
    (re.compile(r"\bmixtral-8x\d+b(?:-[a-z0-9]+)*\b", re.I), "mistral"),
    (re.compile(r"\bcommand-(?:r-plus|r|a)(?:-\d{2,}|-[a-z]+)*\b", re.I), "cohere"),
    (re.compile(r"\bdeepseek-(?:chat|reasoner|coder|v\d|r\d)(?:[.:\-][a-z0-9.]+)*\b", re.I), "deepseek"),
    (re.compile(r"\bgrok-\d(?:[.\-][a-z0-9]+)*\b", re.I), "xai"),
    # Anything served locally, in the `name:tag` form Ollama always writes.
    # The tag is what makes this reliable — no prose carries one.
    (re.compile(
        r"\b(?:llama|qwen|phi|gemma|codellama|codegemma|starcoder|deepseek-r\d|mixtral|"
        r"mistral|vicuna|orca|tinyllama|nomic-embed-text|mxbai-embed|llava|granite|"
        r"solar|dolphin|wizardlm|neural-chat|openchat)[\d.]*(?:-[a-z0-9.]+)*:[a-z0-9][\w.\-]*\b",
        re.I), "ollama"),
    # …and the same families written without a tag, where a version is required.
    (re.compile(r"\bllama-?\d[\d.]*(?:-[a-z0-9]+)*\b", re.I), "ollama"),
    (re.compile(r"\bqwen\d[\d.]*(?:-[a-z0-9]+)*\b", re.I), "ollama"),
    (re.compile(r"\b(?:gemma|phi)-?\d[\d.]*(?:-[a-z0-9]+)*\b", re.I), "ollama"),
]

# Model names Ollama says are installed here. These are not guesses — the
# runtime is naming its own models — so they are matched literally and beat
# every pattern above. Refreshed whenever the local runtime is probed.
_local_models: set[str] = set()
_local_lock = threading.Lock()


def note_local_models(names) -> None:
    with _local_lock:
        _local_models.clear()
        for name in names:
            cleaned = (name or "").strip().lower()
            if len(cleaned) > 2:
                _local_models.add(cleaned)


def local_model_names() -> set[str]:
    with _local_lock:
        return set(_local_models)

# USD per million tokens, (input, output). A built-in table goes stale — it is
# here so a cost column exists at all, and any model missing from it is shown
# with no cost rather than a made-up one.
PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-4": (15.0, 75.0),
    "claude-opus-5": (15.0, 75.0),
    "claude-sonnet-4": (3.0, 15.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-3-7-sonnet": (3.0, 15.0),
    "claude-3-5-sonnet": (3.0, 15.0),
    "claude-haiku-4": (1.0, 5.0),
    "claude-3-5-haiku": (0.8, 4.0),
    "claude-3-haiku": (0.25, 1.25),
    "gpt-4.1-nano": (0.1, 0.4),
    "gpt-4.1-mini": (0.4, 1.6),
    "gpt-4.1": (2.0, 8.0),
    "gpt-4o-mini": (0.15, 0.6),
    "gpt-4o": (2.5, 10.0),
    "gpt-4-turbo": (10.0, 30.0),
    "gpt-4": (30.0, 60.0),
    "gpt-3.5-turbo": (0.5, 1.5),
    "o4-mini": (1.1, 4.4),
    "o3-mini": (1.1, 4.4),
    "o3": (2.0, 8.0),
    "o1-mini": (1.1, 4.4),
    "o1": (15.0, 60.0),
    "gemini-2.5-pro": (1.25, 10.0),
    "gemini-2.5-flash": (0.3, 2.5),
    "gemini-2.0-flash": (0.1, 0.4),
    "gemini-1.5-pro": (1.25, 5.0),
    "gemini-1.5-flash": (0.075, 0.3),
    "mistral-large": (2.0, 6.0),
    "mistral-small": (0.2, 0.6),
    "deepseek-chat": (0.27, 1.1),
    "deepseek-reasoner": (0.55, 2.19),
    "command-r-plus": (2.5, 10.0),
    "command-r": (0.15, 0.6),
    "grok-4": (3.0, 15.0),
    "grok-3": (3.0, 15.0),
}


def price_of(model: str) -> tuple[float, float] | None:
    """Longest matching prefix, so a `-mini` is never charged as its parent."""
    if not model:
        return None
    name = model.lower().split("/")[-1]
    best: tuple[str, tuple[float, float]] | None = None
    for prefix, rate in PRICES.items():
        if name.startswith(prefix) and (best is None or len(prefix) > len(best[0])):
            best = (prefix, rate)
    return best[1] if best else None


def cost_of(model: str, input_tokens: int, output_tokens: int) -> float:
    rate = price_of(model)
    if rate is None:
        return 0.0
    return round(
        (input_tokens * rate[0] + output_tokens * rate[1]) / 1_000_000.0, 6
    )


def provider_of_model(model: str) -> str:
    for pattern, key in MODEL_PATTERNS:
        if pattern.search(model or ""):
            return key
    return ""


def provider_label(key: str) -> str:
    provider = PROVIDERS.get(key)
    return provider.label if provider else (key or "unknown")


# ------------------------------------------------------------------ detection


def _iter_files(root: Path):
    """Source files worth reading, shallowest first, hard-capped."""
    seen = 0
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack and seen < SCAN_MAX_FILES:
        folder, depth = stack.pop(0)
        try:
            entries = sorted(folder.iterdir(), key=lambda p: p.name)
        except (PermissionError, OSError):
            continue
        for entry in entries:
            if seen >= SCAN_MAX_FILES:
                return
            name = entry.name
            try:
                if entry.is_dir():
                    if depth < SCAN_MAX_DEPTH and name not in SKIP_DIRS and not name.startswith(".git"):
                        stack.append((entry, depth + 1))
                    continue
            except OSError:
                continue
            if name.startswith(".env") or entry.suffix.lower() in CODE_SUFFIXES:
                seen += 1
                yield entry


def _read_head(path: Path) -> str:
    try:
        if path.stat().st_size > SCAN_MAX_BYTES * 4:
            with path.open("r", errors="replace") as handle:
                return handle.read(SCAN_MAX_BYTES)
        return path.read_text(errors="replace")[:SCAN_MAX_BYTES]
    except (OSError, ValueError):
        return ""


# Folders that are not a project. An application registered against `/` or a
# home directory is a service — mysql, ollama, a tunnel — with no source tree
# of its own, and reading one would describe every project on the machine
# rather than that application. Its registered environment is still honoured,
# because that part really is about this app.
NOT_A_PROJECT = {
    Path("/"), Path("/usr"), Path("/etc"), Path("/opt"), Path("/var"),
    Path("/tmp"), Path("/srv"), Path("/root"), Path("/home"), Path.home(),
    Path.home() / "Desktop", Path.home() / "Documents", Path.home() / "Downloads",
}


def is_project_dir(root: Path) -> bool:
    try:
        resolved = root.resolve()
    except OSError:
        return False
    return resolved not in NOT_A_PROJECT


def scan_folder(working_dir: str, declared_env: dict[str, str] | None = None) -> list[dict]:
    """What this application's own files say it talks to.

    Evidence is kept with every finding, because a detection nobody can check
    is a rumour. `env` beats `code` beats `dependency`: an API key in the
    environment is a decision, an import is a possibility.
    """
    root = Path(working_dir).expanduser()
    found: dict[tuple[str, str], dict] = {}

    def note(provider: str, model: str, kind: str, evidence: str) -> None:
        key = (provider, model)
        rank = {"env": 3, "code": 2, "dependency": 1}
        current = found.get(key)
        if current is None or rank[kind] > rank[current["kind"]]:
            found[key] = {
                "provider": provider,
                "model": model,
                "kind": kind,
                "evidence": evidence[:200],
            }

    # The environment God Father itself will hand the process is the strongest
    # statement of intent there is — it was typed in on purpose.
    for name in (declared_env or {}):
        upper = name.upper()
        for provider in PROVIDERS.values():
            if upper in provider.env_keys:
                note(provider.key, "", "env", f"registered environment: {name}")

    if not root.is_dir() or not is_project_dir(root):
        return list(found.values())

    installed = local_model_names()

    for path in _iter_files(root):
        text = _read_head(path)
        if not text:
            continue
        try:
            where = str(path.relative_to(root))
        except ValueError:
            where = path.name

        lowered = text.lower()

        for provider in PROVIDERS.values():
            for key_name in provider.env_keys:
                if key_name in text:
                    note(provider.key, "", "env", f"{where}: {key_name}")
            for marker in provider.markers:
                if marker.lower() in lowered:
                    note(provider.key, "", "code", f"{where}: {marker}")
            for package in provider.packages:
                if path.name in ("requirements.txt", "package.json", "pyproject.toml",
                                 "Pipfile", "go.mod") and package in text:
                    note(provider.key, "", "dependency", f"{where}: {package}")

        # Model ids, which are the half that makes the row worth reading.
        for pattern, provider_key in MODEL_PATTERNS:
            for match in pattern.findall(text)[:12]:
                model = match if isinstance(match, str) else match[0]
                model = model.strip().strip(".,\"'")
                if len(model) < 5:
                    continue
                note(provider_key, model.lower(), "code", f"{where}: {model}")

        # A name the local runtime says it has installed is not a guess.
        for name in installed:
            if name in lowered:
                note("ollama", name, "code", f"{where}: {name} (installed locally)")

    return _collapse_tags(list(found.values()))


def _collapse_tags(findings: list[dict]) -> list[dict]:
    """Fold `qwen2.5-coder` into `qwen2.5-coder:14b` when both were found.

    Only locally served models, and only against a tagged name. The untagged
    match is the same reference read less completely, so keeping both would
    list one model twice. It is deliberately not done for hosted models —
    `gpt-4o` is a prefix of `gpt-4o-mini` and they are not the same thing.
    """
    tagged_bases = [
        item["model"].split(":", 1)[0]
        for item in findings
        if item["provider"] == "ollama" and ":" in item["model"]
    ]
    if not tagged_bases:
        return findings

    def redundant(item: dict) -> bool:
        model = item["model"]
        if item["provider"] != "ollama" or not model or ":" in model:
            return False
        return any(
            base == model or (base.startswith(model) and base[len(model)] in ".-_")
            for base in tagged_bases
        )

    return [item for item in findings if not redundant(item)]


def _apps() -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT id, name, working_dir, env FROM apps ORDER BY name"
        ).fetchall()
    out = []
    for row in rows:
        try:
            env = json.loads(row["env"] or "{}")
        except (ValueError, TypeError):
            env = {}
        out.append(
            {"id": row["id"], "name": row["name"], "working_dir": row["working_dir"], "env": env}
        )
    return out


def rescan(app_ids: list[int] | None = None) -> int:
    """Re-read applications' folders and refresh what they are linked to.

    Detected links are replaced wholesale; declared and observed ones are left
    alone. A model you wrote down by hand does not disappear because a scan
    could not find it in the source, and a model actually seen on the wire is
    not a guess to be revised.
    """
    written = 0
    for app in _apps():
        if app_ids is not None and app["id"] not in app_ids:
            continue
        try:
            findings = scan_folder(app["working_dir"], app["env"])
        except Exception:
            continue
        now = utcnow()
        with db() as conn:
            conn.execute(
                "DELETE FROM ai_links WHERE app_id = ? AND source = 'detected'", (app["id"],)
            )
            for item in findings:
                conn.execute(
                    """INSERT INTO ai_links
                           (app_id, provider, model, source, kind, evidence, first_seen, last_seen)
                       VALUES (?, ?, ?, 'detected', ?, ?, ?, ?)
                       ON CONFLICT(app_id, provider, model, source) DO UPDATE SET
                           kind = excluded.kind,
                           evidence = excluded.evidence,
                           last_seen = excluded.last_seen""",
                    (app["id"], item["provider"], item["model"], item["kind"],
                     item["evidence"], now, now),
                )
                written += 1
        _scan_times[app["id"]] = time.monotonic()
    return written


def link_seen(app_id: int, provider: str, model: str, source: str = "observed",
              evidence: str = "") -> None:
    """Record that an app really did reach a provider — the strongest evidence."""
    now = utcnow()
    try:
        with db() as conn:
            conn.execute(
                """INSERT INTO ai_links
                       (app_id, provider, model, source, kind, evidence, first_seen, last_seen)
                   VALUES (?, ?, ?, ?, 'observed', ?, ?, ?)
                   ON CONFLICT(app_id, provider, model, source) DO UPDATE SET
                       last_seen = excluded.last_seen,
                       evidence = CASE WHEN excluded.evidence != '' THEN excluded.evidence
                                       ELSE ai_links.evidence END""",
                (app_id, provider, (model or "").lower(), source, evidence[:200], now, now),
            )
    except Exception:
        pass


def declare(app_id: int, provider: str, model: str, note: str = "") -> None:
    now = utcnow()
    with db() as conn:
        conn.execute(
            """INSERT INTO ai_links
                   (app_id, provider, model, source, kind, evidence, first_seen, last_seen)
               VALUES (?, ?, ?, 'declared', 'declared', ?, ?, ?)
               ON CONFLICT(app_id, provider, model, source) DO UPDATE SET
                   evidence = excluded.evidence, last_seen = excluded.last_seen""",
            (app_id, provider, (model or "").lower(), note[:200], now, now),
        )


def undeclare(link_id: int) -> None:
    with db() as conn:
        conn.execute("DELETE FROM ai_links WHERE id = ? AND source = 'declared'", (link_id,))


_scan_times: dict[int, float] = {}


# ----------------------------------------------------------------------- live


class Addresses:
    """Provider hostnames resolved to addresses, and the reverse.

    Forward resolution is what identifies a socket: an application's connection
    to Anthropic points at whatever ``api.anthropic.com`` resolves to here, and
    resolving it ourselves gives that same set. Reverse lookups fill the gaps —
    a regional endpoint we never listed — and are cached hard, because a DNS
    round trip on a 15-second timer is a cost, and one on a request path is a
    hang waiting to happen.
    """

    def __init__(self) -> None:
        self._forward: dict[str, str] = {}   # ip -> provider key
        self._resolved_at = 0.0
        self._reverse: dict[str, str] = {}   # ip -> provider key or ""
        self._lock = threading.Lock()

    def refresh(self, force: bool = False) -> None:
        if not force and time.monotonic() - self._resolved_at < DNS_TTL_SECONDS:
            return
        mapping: dict[str, str] = {}
        for provider in PROVIDERS.values():
            for host in provider.hosts:
                try:
                    infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
                except (socket.gaierror, OSError):
                    continue
                for info in infos:
                    address = info[4][0]
                    mapping[address] = provider.key
        with self._lock:
            # Merged rather than replaced: an address that answered last time
            # is still that provider's, and a DNS blip should not blank the tab.
            self._forward.update(mapping)
            self._resolved_at = time.monotonic()

    def lookup(self, ip: str) -> str:
        with self._lock:
            hit = self._forward.get(ip)
            if hit:
                return hit
            return self._reverse.get(ip, "")

    def learn(self, ip: str) -> str:
        """Reverse-resolve an unknown address. Monitor thread only — it blocks."""
        with self._lock:
            if ip in self._reverse or ip in self._forward:
                return self._forward.get(ip) or self._reverse.get(ip, "")
        name = ""
        try:
            name = socket.gethostbyaddr(ip)[0].lower()
        except (socket.herror, socket.gaierror, OSError):
            name = ""
        key = ""
        if name:
            name = name.rstrip(".")
            labels = name.split(".")
            for provider in PROVIDERS.values():
                suffix = provider.domain
                if suffix and (name == suffix or name.endswith("." + suffix)):
                    key = provider.key
                    break
                if provider.host_labels and any(
                    label.startswith(marker)
                    for label in labels
                    for marker in provider.host_labels
                ):
                    key = provider.key
                    break
        with self._lock:
            if len(self._reverse) > 2000:
                self._reverse.clear()
            self._reverse[ip] = key
        return key

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._forward)


addresses = Addresses()


def _managed_pids() -> dict[int, tuple[int, str]]:
    """Every pid of every registered app, children included.

    Same rule as the System tab: an app is answerable for what it spawned, so
    `npm start` carries the node process that is actually holding the socket.
    """
    from .manager import manager

    owned: dict[int, tuple[int, str]] = {}
    if psutil is None:
        return owned

    for app in _apps():
        process = manager.peek(app["id"])
        if process is None or not process.is_alive or process.proc is None:
            continue
        try:
            parent = psutil.Process(process.proc.pid)
        except Exception:
            continue
        owned[parent.pid] = (app["id"], app["name"])
        try:
            for child in parent.children(recursive=True):
                owned[child.pid] = (app["id"], app["name"])
        except Exception:
            continue
    return owned


LOOPBACK = ("127.", "::1", "0.0.0.0")

# The three fields the walk below actually reads. Only needed for the
# no-privilege path, where per-process rows arrive without a pid attached.
Socket = namedtuple("Socket", "status raddr pid")


def live(learn: bool = False) -> list[dict]:
    """Which registered application has a socket open to a model, right now.

    ``learn`` allows a blocking reverse lookup and so is only ever set by the
    monitor thread; a request asking for this state gets whatever is cached.
    """
    if psutil is None:
        return []

    owned = _managed_pids()
    if not owned:
        return []

    # Resolving hostnames blocks. The monitor thread keeps the table warm, so a
    # request only ever resolves on the very first call, when there is nothing
    # cached to answer with and a short wait beats an empty tab.
    if learn or not addresses.count:
        addresses.refresh()
    rows: dict[tuple[int, str], dict] = {}

    try:
        connections = psutil.net_connections(kind="inet")
    except (psutil.AccessDenied, PermissionError):
        # Without privilege the system-wide call fails, but asking each process
        # directly still works for our own children — which is exactly the set
        # this cares about. Those rows carry no pid of their own (psutil drops
        # the field when you already know whose they are), so it is put back.
        connections = []
        for pid in list(owned):
            try:
                for conn in psutil.Process(pid).net_connections(kind="inet"):
                    connections.append(Socket(conn.status, conn.raddr, pid))
            except Exception:
                continue
    except Exception:
        return []

    for conn in connections:
        pid = getattr(conn, "pid", None)
        if pid is None or pid not in owned:
            continue
        if conn.status != "ESTABLISHED" or not conn.raddr:
            continue

        ip = conn.raddr.ip
        port = conn.raddr.port
        app_id, app_name = owned[pid]

        provider_key = ""
        if any(ip.startswith(prefix) for prefix in LOOPBACK):
            # A model served on this machine. Only the port can say which.
            for provider in PROVIDERS.values():
                if provider.local and port in provider.ports:
                    provider_key = provider.key
                    break
            if not provider_key:
                continue
        else:
            provider_key = addresses.lookup(ip)
            if not provider_key and learn and port in (443, 80):
                provider_key = addresses.learn(ip)
            if not provider_key:
                continue

        key = (app_id, provider_key)
        row = rows.setdefault(
            key,
            {
                "app_id": app_id,
                "app": app_name,
                "provider": provider_key,
                "provider_label": provider_label(provider_key),
                "connections": 0,
                "remote": f"{ip}:{port}",
                "local": PROVIDERS[provider_key].local,
            },
        )
        row["connections"] += 1

    return sorted(rows.values(), key=lambda r: (-r["connections"], r["app"]))


# ---------------------------------------------------------------- local models


_ollama: dict = {"reachable": False, "models": [], "checked": ""}


def _http_json(url: str, timeout: float = 1.5) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None


def probe_ollama() -> dict:
    """What Ollama has loaded. The one local runtime that will simply tell you.

    ``/api/ps`` is the interesting half — a model in ``/api/tags`` is merely
    downloaded, while one in ``ps`` is in memory now and holding the VRAM the
    System tab is showing you.
    """
    host = os.environ.get("OLLAMA_HOST", "127.0.0.1:11434").replace("http://", "").strip("/")
    running = _http_json(f"http://{host}/api/ps")
    if running is None:
        return {"reachable": False, "models": [], "checked": utcnow()}

    installed = _http_json(f"http://{host}/api/tags") or {}
    have = {m.get("name", "") for m in installed.get("models", [])}

    models = []
    for entry in running.get("models", []):
        models.append(
            {
                "name": entry.get("name") or entry.get("model", ""),
                "size": int(entry.get("size") or 0),
                "vram": int(entry.get("size_vram") or 0),
                "expires_at": entry.get("expires_at", "") or "",
                "loaded": True,
            }
        )
    loaded_names = {m["name"] for m in models}
    for name in sorted(have - loaded_names)[:20]:
        models.append({"name": name, "size": 0, "vram": 0, "expires_at": "", "loaded": False})

    # Detection gets these names back: a model the runtime says it has is a
    # fact about this machine, and beats every pattern guess.
    note_local_models({m["name"] for m in models})

    return {"reachable": True, "models": models, "checked": utcnow()}


# -------------------------------------------------------------------- measured

# The usage blocks SDKs already print. Reading these is what gives a token
# count for an application nobody instrumented — it costs nothing, it is
# already on disk, and the alternative is an empty column.
# Every field name is anchored with a lookbehind, because these names are each
# other's suffixes and the wrong one is silently plausible: `eval_count` sits
# inside `prompt_eval_count`, and `input_tokens` inside Anthropic's
# `cache_read_input_tokens`. Without the anchor a 1,100-token reply gets
# recorded as 300, and nothing about the number looks wrong.
TOKEN_PATTERNS = [
    (re.compile(r'(?<![\w_])"?input_tokens"?\s*[:=]\s*(\d+)'),
     re.compile(r'(?<![\w_])"?output_tokens"?\s*[:=]\s*(\d+)')),
    (re.compile(r'(?<![\w_])"?prompt_tokens"?\s*[:=]\s*(\d+)'),
     re.compile(r'(?<![\w_])"?completion_tokens"?\s*[:=]\s*(\d+)')),
    (re.compile(r'(?<![\w_])"?promptTokenCount"?\s*[:=]\s*(\d+)'),
     re.compile(r'(?<![\w_])"?candidatesTokenCount"?\s*[:=]\s*(\d+)')),
    (re.compile(r'(?<![\w_])"?prompt_eval_count"?\s*[:=]\s*(\d+)'),
     re.compile(r'(?<![\w_])"?eval_count"?\s*[:=]\s*(\d+)')),
]
# A cheap first filter, so the four patterns above are only run on lines that
# could possibly match. Note `eval_count` explicitly: Ollama's usage block is
# the one shape here that never says the word "token".
USAGE_HINT = re.compile(r"input_tokens|output_tokens|prompt_tokens|completion_tokens|"
                        r"promptTokenCount|candidatesTokenCount|eval_count", re.I)
MODEL_IN_LINE = re.compile(r'"?model"?\s*[:=]\s*"?([A-Za-z0-9][\w.:\-/]{3,60})"?')
LATENCY_IN_LINE = re.compile(r'"?(?:latency_ms|duration_ms|elapsed_ms)"?\s*[:=]\s*([\d.]+)')

# Where each app's log had been read up to. Seeded at end-of-file on the first
# pass, so starting God Father does not re-count a week of history as though it
# happened this minute.
_log_offsets: dict[int, int] = {}


def _scan_log(app_id: int, app_name: str) -> int:
    path = log_path(app_id)
    try:
        size = path.stat().st_size
    except OSError:
        return 0

    start = _log_offsets.get(app_id)
    if start is None:
        _log_offsets[app_id] = size
        return 0
    if size < start:
        # Rotated out from under us; the new file starts at the beginning.
        start = 0
    if size == start:
        return 0

    try:
        with path.open("r", errors="replace") as handle:
            handle.seek(start)
            chunk = handle.read(2_000_000)
            _log_offsets[app_id] = handle.tell()
    except OSError:
        return 0

    added = 0
    for raw in chunk.splitlines():
        if len(raw) > 20000:
            continue
        # The captured line is a JSON envelope; the payload is what the app printed.
        text = raw
        try:
            envelope = json.loads(raw)
            if isinstance(envelope, dict) and "text" in envelope:
                text = str(envelope.get("text", ""))
        except (ValueError, TypeError):
            pass
        if not USAGE_HINT.search(text):
            continue

        for in_pattern, out_pattern in TOKEN_PATTERNS:
            in_match = in_pattern.search(text)
            out_match = out_pattern.search(text)
            if not in_match and not out_match:
                continue
            input_tokens = int(in_match.group(1)) if in_match else 0
            output_tokens = int(out_match.group(1)) if out_match else 0
            if input_tokens + output_tokens == 0:
                break

            model_match = MODEL_IN_LINE.search(text)
            model = (model_match.group(1) if model_match else "").lower()
            provider = provider_of_model(model)
            latency_match = LATENCY_IN_LINE.search(text)

            record(
                app_id=app_id,
                app_name=app_name,
                provider=provider,
                model=model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                latency_ms=float(latency_match.group(1)) if latency_match else 0.0,
                ok=True,
                error="",
                source="log",
                endpoint="",
            )
            added += 1
            break
    return added


def scan_logs() -> int:
    total = 0
    for app in _apps():
        try:
            total += _scan_log(app["id"], app["name"])
        except Exception:
            continue
    return total


def record(app_id: int | None, app_name: str, provider: str, model: str,
           input_tokens: int = 0, output_tokens: int = 0, latency_ms: float = 0.0,
           ok: bool = True, error: str = "", source: str = "ingest",
           endpoint: str = "", ts: str = "") -> int:
    """Store one call. The only place a usage row is ever written."""
    model = (model or "").strip().lower()
    if not provider:
        provider = provider_of_model(model)
    cost = cost_of(model, input_tokens, output_tokens)

    def insert(owner: int | None) -> int:
        with db() as conn:
            cursor = conn.execute(
                """INSERT INTO ai_calls
                       (ts, app_id, app_name, provider, model, source, endpoint,
                        input_tokens, output_tokens, cost_usd, latency_ms, ok, error)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (ts or utcnow(), owner, app_name, provider, model, source, endpoint,
                 int(input_tokens), int(output_tokens), cost, float(latency_ms),
                 1 if ok else 0, error[:300]),
            )
            return cursor.lastrowid

    try:
        row_id = insert(app_id)
    except sqlite3.IntegrityError:
        # The application row went away between reading its log and writing
        # this. What it spent was still spent, so the call is kept under the
        # name it had rather than thrown away with the registration.
        row_id = insert(None)
        app_id = None

    if app_id and model:
        link_seen(app_id, provider, model, "observed", f"{source}: seen in use")
    return row_id


def resolve_app(name: str = "", app_id: int | None = None,
                port: int | None = None) -> tuple[int | None, str]:
    """Work out which registered application a usage report belongs to.

    Reports arrive from inside the application, which knows its own name or the
    port it serves but not the row id God Father filed it under.
    """
    with db() as conn:
        if app_id is not None:
            row = conn.execute("SELECT id, name FROM apps WHERE id = ?", (app_id,)).fetchone()
            if row:
                return row["id"], row["name"]
        if name:
            row = conn.execute(
                "SELECT id, name FROM apps WHERE lower(name) = lower(?)", (name.strip(),)
            ).fetchone()
            if row:
                return row["id"], row["name"]
        if port is not None:
            row = conn.execute("SELECT id, name FROM apps WHERE port = ?", (port,)).fetchone()
            if row:
                return row["id"], row["name"]
    # Unregistered callers are still worth counting — they are usage on this
    # machine, and refusing them would make the totals quietly wrong.
    return None, (name.strip() or "unregistered")


# ------------------------------------------------------------------ retention


def sweep() -> None:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=RETENTION_HOURS)).isoformat(
        timespec="seconds"
    )
    with db() as conn:
        conn.execute("DELETE FROM ai_activity WHERE ts < ?", (cutoff,))
        # Calls are kept a fortnight. They are small, and a spend figure is
        # only useful if it reaches back further than the charts do.
        older = (datetime.now(timezone.utc) - timedelta(days=14)).isoformat(timespec="seconds")
        conn.execute("DELETE FROM ai_calls WHERE ts < ?", (older,))


# ------------------------------------------------------------------- the loop


class AiMonitor:
    """Walks the live half on a timer, and turns app logs into usage rows."""

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.last_run: str = ""
        self.last_error: str = ""

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
        self._thread = None

    def _store_live(self, rows: list[dict]) -> None:
        if not rows:
            return
        now = utcnow()
        with db() as conn:
            for row in rows:
                conn.execute(
                    """INSERT INTO ai_activity (ts, app_id, app_name, provider, connections)
                       VALUES (?, ?, ?, ?, ?)""",
                    (now, row["app_id"], row["app"], row["provider"], row["connections"]),
                )
        for row in rows:
            link_seen(row["app_id"], row["provider"], "", "observed",
                      f"open connection to {row['remote']}")

    def _loop(self) -> None:
        global _ollama
        rounds = 0
        # Ollama first: the cold-start scan below matches against the model
        # names it reports, so asking it afterwards would waste the first pass.
        try:
            _ollama = probe_ollama()
        except Exception:
            pass
        # Then everything registered gets scanned once, so the tab has content
        # before any application has been touched.
        try:
            rescan()
        except Exception as error:
            self.last_error = str(error)

        while not self._stop.is_set():
            try:
                # Kept warm here rather than lazily, so the tab can say how many
                # provider addresses it knows — and so the first application to
                # open a socket is recognised on the very first tick.
                addresses.refresh()
                self._store_live(live(learn=True))
                _ollama = probe_ollama()
                scan_logs()
                self.last_run = utcnow()
                self.last_error = ""
            except Exception as error:
                self.last_error = str(error)

            rounds += 1
            # A folder is re-read every RESCAN_SECONDS, a sweep runs hourly.
            if rounds % max(1, int(RESCAN_SECONDS / SAMPLE_SECONDS)) == 0:
                try:
                    rescan()
                except Exception:
                    pass
            if rounds % max(1, int(3600 / SAMPLE_SECONDS)) == 0:
                try:
                    sweep()
                except Exception:
                    pass

            self._stop.wait(SAMPLE_SECONDS)


monitor = AiMonitor()


def ollama_state() -> dict:
    return dict(_ollama)


# -------------------------------------------------------------------- querying


def _since(minutes: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat(timespec="seconds")


def links() -> dict[int, list[dict]]:
    """Every app's model links, strongest source first."""
    order = {"declared": 0, "observed": 1, "detected": 2}
    grouped: dict[int, list[dict]] = {}
    with db() as conn:
        rows = conn.execute("SELECT * FROM ai_links ORDER BY last_seen DESC").fetchall()
    for row in rows:
        item = dict(row)
        item["provider_label"] = provider_label(item["provider"])
        grouped.setdefault(item["app_id"], []).append(item)
    for values in grouped.values():
        values.sort(key=lambda r: (order.get(r["source"], 9), r["provider"], r["model"]))
    return grouped


def totals(minutes: int) -> dict:
    since = _since(minutes)
    with db() as conn:
        row = conn.execute(
            """SELECT COUNT(*) AS calls,
                      COALESCE(SUM(input_tokens), 0)  AS input_tokens,
                      COALESCE(SUM(output_tokens), 0) AS output_tokens,
                      COALESCE(SUM(cost_usd), 0)      AS cost,
                      COALESCE(SUM(1 - ok), 0)        AS errors,
                      COALESCE(AVG(NULLIF(latency_ms, 0)), 0) AS latency
                 FROM ai_calls WHERE ts >= ?""",
            (since,),
        ).fetchone()
    return {
        "calls": row["calls"] or 0,
        "input_tokens": row["input_tokens"] or 0,
        "output_tokens": row["output_tokens"] or 0,
        "cost_usd": round(row["cost"] or 0.0, 4),
        "errors": row["errors"] or 0,
        "avg_latency_ms": round(row["latency"] or 0.0, 1),
    }


def by_app(minutes: int) -> list[dict]:
    """One row per application: what it uses, and how much it used it."""
    since = _since(minutes)
    grouped = links()
    live_now = {(row["app_id"], row["provider"]): row for row in live()}

    with db() as conn:
        usage = {
            row["key"]: dict(row)
            for row in conn.execute(
                """SELECT COALESCE(app_id, -1) AS key, app_name,
                          COUNT(*) AS calls,
                          COALESCE(SUM(input_tokens), 0)  AS input_tokens,
                          COALESCE(SUM(output_tokens), 0) AS output_tokens,
                          COALESCE(SUM(cost_usd), 0)      AS cost,
                          COALESCE(SUM(1 - ok), 0)        AS errors,
                          MAX(ts) AS last_used
                     FROM ai_calls WHERE ts >= ?
                     GROUP BY COALESCE(app_id, -1)""",
                (since,),
            )
        }
        apps = conn.execute(
            "SELECT id, name, port FROM apps ORDER BY name"
        ).fetchall()

    from .manager import manager

    rows = []
    for app in apps:
        app_id = app["id"]
        found = grouped.get(app_id, [])
        counted = usage.get(app_id, {})
        connections = [row for (a, _), row in live_now.items() if a == app_id]
        process = manager.peek(app_id)

        models = sorted({item["model"] for item in found if item["model"]})
        providers = sorted({item["provider"] for item in found}
                           | {row["provider"] for row in connections})

        if not found and not counted and not connections:
            continue

        rows.append(
            {
                "app_id": app_id,
                "app": app["name"],
                "running": bool(process and process.is_alive),
                "providers": providers,
                "provider_labels": [provider_label(key) for key in providers],
                "models": models,
                "links": found,
                "live": bool(connections),
                "connections": sum(row["connections"] for row in connections),
                "calls": counted.get("calls", 0),
                "input_tokens": counted.get("input_tokens", 0),
                "output_tokens": counted.get("output_tokens", 0),
                "cost_usd": round(counted.get("cost", 0.0) or 0.0, 4),
                "errors": counted.get("errors", 0),
                "last_used": counted.get("last_used", "") or "",
                "measured": bool(counted.get("calls")),
            }
        )

    # Usage reported by something that is not a registered application still
    # counts — it is spend on this machine, and dropping it would quietly make
    # the totals disagree with the bill.
    seen = {row["app_id"] for row in rows}
    for key, counted in usage.items():
        if key not in seen:
            rows.append(
                {
                    "app_id": None,
                    "app": counted.get("app_name") or "unregistered",
                    "running": False,
                    "providers": [],
                    "provider_labels": [],
                    "models": [],
                    "links": [],
                    "live": False,
                    "connections": 0,
                    "calls": counted.get("calls", 0),
                    "input_tokens": counted.get("input_tokens", 0),
                    "output_tokens": counted.get("output_tokens", 0),
                    "cost_usd": round(counted.get("cost", 0.0) or 0.0, 4),
                    "errors": counted.get("errors", 0),
                    "last_used": counted.get("last_used", "") or "",
                    "measured": True,
                }
            )

    rows.sort(key=lambda r: (-r["calls"], not r["live"], r["app"].lower()))
    return rows


def by_model(minutes: int) -> list[dict]:
    """One row per model: who uses it, and what it cost."""
    since = _since(minutes)
    grouped = links()

    with db() as conn:
        measured = conn.execute(
            """SELECT provider, model,
                      COUNT(*) AS calls,
                      COALESCE(SUM(input_tokens), 0)  AS input_tokens,
                      COALESCE(SUM(output_tokens), 0) AS output_tokens,
                      COALESCE(SUM(cost_usd), 0)      AS cost,
                      COALESCE(SUM(1 - ok), 0)        AS errors,
                      COALESCE(AVG(NULLIF(latency_ms, 0)), 0) AS latency,
                      MAX(ts) AS last_used,
                      COUNT(DISTINCT COALESCE(app_id, -1)) AS app_count
                 FROM ai_calls WHERE ts >= ?
                 GROUP BY provider, model""",
            (since,),
        ).fetchall()
        names = {row["id"]: row["name"] for row in conn.execute("SELECT id, name FROM apps")}

    rows: dict[tuple[str, str], dict] = {}

    def bucket(provider: str, model: str) -> dict:
        key = (provider, model)
        return rows.setdefault(
            key,
            {
                "provider": provider,
                "provider_label": provider_label(provider),
                "model": model,
                "apps": [],
                "calls": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cost_usd": 0.0,
                "errors": 0,
                "avg_latency_ms": 0.0,
                "last_used": "",
                "price_in": 0.0,
                "price_out": 0.0,
                "local": bool(PROVIDERS.get(provider) and PROVIDERS[provider].local),
            },
        )

    for app_id, found in grouped.items():
        for item in found:
            if not item["model"]:
                continue
            row = bucket(item["provider"], item["model"])
            name = names.get(app_id)
            if name and name not in row["apps"]:
                row["apps"].append(name)

    for entry in measured:
        row = bucket(entry["provider"], entry["model"] or "(model not reported)")
        row["calls"] += entry["calls"]
        row["input_tokens"] += entry["input_tokens"]
        row["output_tokens"] += entry["output_tokens"]
        row["cost_usd"] = round(row["cost_usd"] + (entry["cost"] or 0.0), 4)
        row["errors"] += entry["errors"]
        row["avg_latency_ms"] = round(entry["latency"] or 0.0, 1)
        row["last_used"] = max(row["last_used"], entry["last_used"] or "")

    for (provider, model), row in rows.items():
        rate = price_of(model)
        if rate:
            row["price_in"], row["price_out"] = rate
        row["apps"].sort()

    return sorted(
        rows.values(),
        key=lambda r: (-r["calls"], -len(r["apps"]), r["provider"], r["model"]),
    )


def history(minutes: int, points: int = 100) -> list[dict]:
    """Usage and connection activity on one timeline.

    Both series matter and neither replaces the other: tokens say how much work
    was asked for, connections say an app was talking at all — which is the
    only signal there is for an app that reports no usage.
    """
    since = _since(minutes)
    bucket_seconds = max(60, int(minutes * 60 / points))

    with db() as conn:
        calls = conn.execute(
            """SELECT ts, input_tokens, output_tokens, cost_usd, ok
                 FROM ai_calls WHERE ts >= ? ORDER BY ts""",
            (since,),
        ).fetchall()
        activity = conn.execute(
            """SELECT ts, SUM(connections) AS connections
                 FROM ai_activity WHERE ts >= ? GROUP BY ts ORDER BY ts""",
            (since,),
        ).fetchall()

    if not calls and not activity:
        return []

    def stamp(value: str) -> float:
        try:
            return datetime.fromisoformat(value).timestamp()
        except (ValueError, TypeError):
            return 0.0

    end = datetime.now(timezone.utc).timestamp()
    start = end - minutes * 60
    slots = max(1, int((end - start) / bucket_seconds))
    buckets = [
        {"ts": "", "calls": 0, "input_tokens": 0, "output_tokens": 0,
         "cost_usd": 0.0, "errors": 0, "connections": 0}
        for _ in range(slots)
    ]

    for index in range(slots):
        moment = datetime.fromtimestamp(start + (index + 1) * bucket_seconds, timezone.utc)
        buckets[index]["ts"] = moment.isoformat(timespec="seconds")

    def slot_of(value: str) -> int | None:
        seconds = stamp(value)
        if not seconds:
            return None
        index = int((seconds - start) / bucket_seconds)
        return index if 0 <= index < slots else None

    for row in calls:
        index = slot_of(row["ts"])
        if index is None:
            continue
        bucket = buckets[index]
        bucket["calls"] += 1
        bucket["input_tokens"] += row["input_tokens"] or 0
        bucket["output_tokens"] += row["output_tokens"] or 0
        bucket["cost_usd"] = round(bucket["cost_usd"] + (row["cost_usd"] or 0.0), 6)
        bucket["errors"] += 0 if row["ok"] else 1

    for row in activity:
        index = slot_of(row["ts"])
        if index is None:
            continue
        # The peak within the bucket, not the mean: a connection that existed
        # is a fact, and averaging it away hides short bursts entirely.
        buckets[index]["connections"] = max(
            buckets[index]["connections"], row["connections"] or 0
        )

    return buckets


def recent_calls(limit: int = 80) -> list[dict]:
    with db() as conn:
        # By when the call happened rather than when the row was written: a
        # usage block read out of a log can be minutes old by the time it is
        # stored, and the log is meant to read as a timeline.
        rows = conn.execute(
            "SELECT * FROM ai_calls ORDER BY ts DESC, id DESC LIMIT ?", (limit,)
        ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["ok"] = bool(item["ok"])
        item["provider_label"] = provider_label(item["provider"])
        out.append(item)
    return out


def clear_calls() -> None:
    with db() as conn:
        conn.execute("DELETE FROM ai_calls")
        conn.execute("DELETE FROM ai_activity")
