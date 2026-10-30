"""Request and response schemas."""

import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class AppBase(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = ""
    command: str = Field(min_length=1)
    working_dir: str = Field(min_length=1)
    port: int | None = Field(default=None, ge=1, le=65535)
    env: dict[str, str] = Field(default_factory=dict)
    auto_restart: bool = False
    start_on_boot: bool = False
    share_lan: bool = False
    # Watch working_dir and refresh this app's wall tile when it changes.
    live_reload: bool = False
    # Blank means "use the registered port". Accepts a full URL or a path.
    preview_url: str = ""
    # Whether this app appears on the news ticker, and the words that make one
    # of its log lines news. Comma-separated, matched case-insensitively.
    news_enabled: bool = True
    news_keywords: str = ""
    # Focus Mode: the apps this one cannot run without, whether Focus Mode may
    # ever stop it, and the Ollama models it uses (comma-separated).
    needs: list[int] = Field(default_factory=list)
    protected: bool = False
    uses_models: str = ""

    @field_validator("preview_url")
    @classmethod
    def _check_preview(cls, v: str) -> str:
        v = (v or "").strip()
        if v and not v.startswith(("http://", "https://", "/")):
            raise ValueError("must be a full URL (http://…) or a path starting with /")
        return v

    @field_validator("name", "command", "working_dir")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be blank")
        return v


class AppCreate(AppBase):
    # Checked on input only: AppOut also inherits AppBase, and a saved app whose
    # folder has since been deleted must still be listable.
    @field_validator("working_dir")
    @classmethod
    def _expand_dir(cls, v: str) -> str:
        path = Path(v).expanduser()
        if not path.is_dir():
            raise ValueError(f"working directory does not exist: {path}")
        return str(path.resolve())


class AppUpdate(BaseModel):
    """Every field optional — only what is sent gets changed."""

    name: str | None = Field(default=None, min_length=1, max_length=80)
    description: str | None = None
    command: str | None = Field(default=None, min_length=1)
    working_dir: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    env: dict[str, str] | None = None
    auto_restart: bool | None = None
    start_on_boot: bool | None = None
    share_lan: bool | None = None
    live_reload: bool | None = None
    preview_url: str | None = None
    news_enabled: bool | None = None
    news_keywords: str | None = None
    needs: list[int] | None = None
    protected: bool | None = None
    uses_models: str | None = None

    @field_validator("preview_url")
    @classmethod
    def _check_preview(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if v and not v.startswith(("http://", "https://", "/")):
            raise ValueError("must be a full URL (http://…) or a path starting with /")
        return v

    @field_validator("working_dir")
    @classmethod
    def _expand_dir(cls, v: str | None) -> str | None:
        if v is None:
            return None
        path = Path(v).expanduser()
        if not path.is_dir():
            raise ValueError(f"working directory does not exist: {path}")
        return str(path.resolve())


# "external" = the port is serving, but this process was not started by God Father,
# so its output belongs to whichever terminal launched it and cannot be captured here.
Status = Literal["running", "stopped", "crashed", "starting", "external"]


class Runtime(BaseModel):
    status: Status = "stopped"
    pid: int | None = None
    started_at: str | None = None
    uptime_seconds: float | None = None
    exit_code: int | None = None
    restart_count: int = 0
    cpu_percent: float | None = None
    memory_mb: float | None = None
    port_open: bool | None = None
    last_error: str | None = None
    # Local-network sharing
    lan_url: str | None = None
    share_active: bool = False
    share_detail: str | None = None
    firewall_open: bool | None = None


class AppOut(AppBase):
    id: int
    created_at: str
    updated_at: str
    runtime: Runtime


class LogLine(BaseModel):
    ts: str
    stream: Literal["stdout", "stderr", "system"]
    text: str


class EventOut(BaseModel):
    id: int
    app_id: int
    type: str
    message: str
    created_at: str


class PortInfo(BaseModel):
    port: int
    pid: int | None = None
    process: str | None = None
    address: str = ""
    managed_app_id: int | None = None
    managed_app_name: str | None = None


class Candidate(BaseModel):
    """An application found running on this machine, offered for import."""

    suggested_name: str
    description: str
    command: str
    working_dir: str
    port: int | None = None
    extra_ports: list[int] = Field(default_factory=list)
    kind: Literal["project", "tool", "service"]
    pid: int | None = None
    already_registered: bool = False
    registered_as: str | None = None
    note: str | None = None
    importable: bool = True


class ImportItem(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    description: str = ""
    command: str = Field(min_length=1)
    working_dir: str
    port: int | None = Field(default=None, ge=1, le=65535)


class ImportRequest(BaseModel):
    items: list[ImportItem]


class ImportSkip(BaseModel):
    name: str
    reason: str


class ImportResult(BaseModel):
    created: list[AppOut]
    skipped: list[ImportSkip]


class Occupant(BaseModel):
    """Whoever currently holds an app's port, and whether we may stop it."""

    occupied: bool = False
    pid: int | None = None
    process: str | None = None
    command: str | None = None
    user: str | None = None
    killable: bool = False
    reason: str | None = None
    managed_by: str | None = None


class NetworkWallToggle(BaseModel):
    enabled: bool
    port: int | None = Field(default=None, ge=1, le=65535)


class NetworkWallState(BaseModel):
    enabled: bool = False
    port: int = 9200
    url: str | None = None
    lan_ip: str | None = None
    firewall_managed: bool = False
    manual_command: str | None = None


class KillResult(BaseModel):
    ok: bool
    message: str
    port: int


class WallTile(BaseModel):
    app_id: int
    name: str
    port: int | None = None
    url: str | None = None
    status: Status = "stopped"
    # web = embeddable page · blocked = refuses framing · text = not a page
    # error = HTTP error · down = no response · noport = nothing to show
    kind: Literal["web", "blocked", "text", "error", "down", "noport"] = "down"
    http_status: int | None = None
    content_type: str | None = None
    note: str | None = None
    # Bumped by the file watcher; the wall reloads a tile whose rev moved.
    watching: bool = False
    rev: int = 0


class NewsItem(BaseModel):
    id: int
    app_id: int | None = None
    app_name: str
    # good = payment, signup, app came up · info = routine · warn · error
    level: Literal["good", "info", "warn", "error"]
    # event = start/stop/crash · app = the app announced it · log = picked out
    # of its output · watch = an app run elsewhere changed state
    source: Literal["event", "app", "log", "watch"]
    message: str
    created_at: str


class LogTapeItem(BaseModel):
    """One captured log line on the ticker's All logs tape."""

    id: int
    app_id: int
    app_name: str
    level: Literal["good", "info", "warn", "error"]
    stream: Literal["stdout", "stderr", "system"]
    message: str
    created_at: str


class NewsPost(BaseModel):
    """News sent by an application itself. `app` is its name or its id."""

    app: str | int
    message: str = Field(min_length=1, max_length=1000)
    level: Literal["good", "info", "warn", "error"] = "info"


class ShareRequest(BaseModel):
    enabled: bool


class ShareStatus(BaseModel):
    enabled: bool
    lan_url: str | None = None
    relay_active: bool = False
    detail: str = ""
    firewall_open: bool | None = None
    firewall_managed: bool = False
    manual_command: str | None = None


class TunnelRoute(BaseModel):
    """One local port offered to the internet through Cloudflare."""

    id: int
    port: int
    hostname: str = ""
    enabled: bool = False
    public_url: str = ""
    last_error: str = ""
    metrics_port: int | None = None
    # Protection in front of the port. The header's *value* is deliberately
    # absent: it is a credential, and anything that can read this endpoint
    # would otherwise be handed every one configured on the machine. The
    # secret path is not a credential in the same sense — it is part of the
    # address you hand out, so share_url carries it.
    guarded: bool = False
    guard_header_name: str = ""
    guard_header_set: bool = False
    guard_secret_path: bool = False
    guard_oauth: bool = False
    # Whether an approval password has been set. OAuth cannot be switched on
    # without one, so this is really "is the configuration finished".
    guard_oauth_ready: bool = False
    guard_port: int | None = None
    # public_url plus the secret path — the address to actually give someone.
    share_url: str = ""
    # The application row running this route, so the tab can link to its logs.
    app_id: int | None = None
    app_name: str | None = None
    app_status: Status = "stopped"
    # The registered app that owns this port, when there is one. Purely a label.
    serves_app_id: int | None = None
    serves_app_name: str | None = None
    port_open: bool = False
    created_at: str
    updated_at: str


def _check_header_name(v: str | None) -> str | None:
    """A header name, or a clear refusal. Anything else would let a caller
    smuggle extra headers in through a newline."""
    if v is None:
        return None
    v = v.strip()
    if not v:
        return ""
    if not re.fullmatch(r"[A-Za-z0-9!#$%&'*+.^_`|~-]+", v):
        raise ValueError(
            "a header name may only contain letters, digits and - _ . — "
            "for example Authorization or X-Api-Key"
        )
    return v


def _check_header_value(v: str | None) -> str | None:
    if v is None:
        return None
    v = v.strip()
    if any(ch in v for ch in "\r\n\0"):
        raise ValueError("a header value cannot contain line breaks")
    return v


class TunnelRouteCreate(BaseModel):
    port: int = Field(ge=1, le=65535)
    hostname: str = ""
    guard_header_name: str = ""
    guard_header_value: str = ""
    # Turning this on generates the secret; it is never supplied by hand.
    guard_secret_path: bool = False
    guard_oauth: bool = False
    guard_oauth_password: str = ""

    @field_validator("hostname")
    @classmethod
    def _check_hostname(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if not v:
            return ""
        if "://" in v or "/" in v:
            raise ValueError("enter just the hostname, without https:// or a path")
        if "." not in v:
            raise ValueError("a hostname needs a domain, for example app.example.com")
        return v

    @field_validator("guard_header_name")
    @classmethod
    def _header_name(cls, v: str) -> str:
        return _check_header_name(v) or ""

    @field_validator("guard_header_value")
    @classmethod
    def _header_value(cls, v: str) -> str:
        return _check_header_value(v) or ""


class TunnelRouteUpdate(BaseModel):
    hostname: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    guard_header_name: str | None = None
    # None leaves the stored credential alone — which is what the form sends
    # back when nobody retyped it, since it is never handed out to be echoed.
    # "" clears it.
    guard_header_value: str | None = None
    guard_secret_path: bool | None = None
    guard_oauth: bool | None = None
    # Same rule as the header value: absent means "leave it", since the stored
    # hash is never sent to the browser to be echoed back.
    guard_oauth_password: str | None = None

    @field_validator("hostname")
    @classmethod
    def _check_hostname(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip().lower()
        if not v:
            return ""
        if "://" in v or "/" in v:
            raise ValueError("enter just the hostname, without https:// or a path")
        if "." not in v:
            raise ValueError("a hostname needs a domain, for example app.example.com")
        return v

    @field_validator("guard_header_name")
    @classmethod
    def _header_name(cls, v: str | None) -> str | None:
        return _check_header_name(v)

    @field_validator("guard_header_value")
    @classmethod
    def _header_value(cls, v: str | None) -> str | None:
        return _check_header_value(v)


class TunnelToken(BaseModel):
    """One key issued for a route. The key itself is not here — only the hash
    was ever stored, so this is the record of it, not the thing."""

    id: int
    label: str = ""
    client_id: str = ""
    created_at: str = ""
    last_used_at: str = ""
    expires_at: str = ""
    revoked: bool = False
    expired: bool = False


class TunnelStatus(BaseModel):
    """What the Cloudflare tab needs to render itself."""

    installed: bool = False
    version: str = ""
    # quick = no domain, random addresses · named = your own hostnames
    mode: Literal["quick", "named"] = "quick"
    logged_in: bool = False
    tunnel_name: str = ""
    tunnels: list[str] = Field(default_factory=list)
    config_path: str = ""
    login_hint: str = ""
    install_hint: str = ""
    routes: list[TunnelRoute] = Field(default_factory=list)


class TunnelPoint(BaseModel):
    """One bucket on the chart."""

    ts: str
    rpm: float = 0.0
    errors: int = 0
    bytes_sent: int = 0
    bytes_received: int = 0
    concurrent: int = 0
    rtt_ms: float = 0.0


class TunnelRouteMetrics(BaseModel):
    route_id: int
    port: int
    public_url: str = ""
    label: str = ""
    live: bool = False
    # Answering its metrics endpoint. A live route that is not reporting is
    # the failure worth seeing — it means the daemon is up but not healthy.
    reporting: bool = False
    requests: int = 0
    errors: int = 0
    error_rate: float = 0.0
    rpm: float = 0.0
    peak_rpm: float = 0.0
    concurrent: int = 0
    connections: int = 0
    bytes_sent: int = 0
    bytes_received: int = 0
    rtt_ms: float = 0.0
    edge_location: str = ""
    codes: dict[str, int] = Field(default_factory=dict)
    points: list[TunnelPoint] = Field(default_factory=list)


class TunnelMetrics(BaseModel):
    """Everything the dashboard draws, for one time window."""

    window_minutes: int = 60
    sampling: bool = False
    last_sample: str = ""
    sample_seconds: float = 10.0
    retention_hours: int = 24
    live_routes: int = 0
    requests: int = 0
    errors: int = 0
    error_rate: float = 0.0
    rpm: float = 0.0
    concurrent: int = 0
    bytes_sent: int = 0
    bytes_received: int = 0
    codes: dict[str, int] = Field(default_factory=dict)
    routes: list[TunnelRouteMetrics] = Field(default_factory=list)
    # Says plainly what a local dashboard cannot know, so its absence does not
    # read as a bug.
    note: str = ""


class TunnelNameRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    create: bool = False

    @field_validator("name")
    @classmethod
    def _strip(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be blank")
        return v


class NetworkInfo(BaseModel):
    lan_ip: str | None = None
    subnet: str | None = None
    ufw_installed: bool = False
    ufw_manageable: bool = False
    setup_hint: str | None = None


class GpuInfo(BaseModel):
    name: str = ""
    percent: float = 0.0
    vram_used: int = 0
    vram_total: int = 0
    temp: float = 0.0
    power: float = 0.0
    power_limit: float = 0.0
    clock_mhz: float = 0.0
    fan: float = 0.0


class SystemNow(BaseModel):
    """The gauges: what the machine is doing this second."""

    ts: str = ""
    cpu_percent: float = 0.0
    cpu_per_core: list[float] = Field(default_factory=list)
    cpu_count: int = 0
    cpu_physical: int = 0
    cpu_freq_mhz: float = 0.0
    cpu_temp: float = 0.0
    load1: float = 0.0
    load5: float = 0.0
    load15: float = 0.0
    mem_used: int = 0
    mem_total: int = 0
    mem_available: int = 0
    mem_cached: int = 0
    mem_percent: float = 0.0
    swap_used: int = 0
    swap_total: int = 0
    swap_percent: float = 0.0
    disk_used: int = 0
    disk_total: int = 0
    disk_percent: float = 0.0
    uptime_seconds: int = 0
    gpu: GpuInfo | None = None


class ProcessRow(BaseModel):
    pid: int
    name: str = ""
    cpu: float = 0.0
    memory: int = 0
    vram: int = 0
    user: str = ""
    # The registered application this process belongs to, if any.
    app: str | None = None


class AppLoad(BaseModel):
    """One registered application's share of the machine, children included."""

    app: str
    cpu: float = 0.0
    memory: int = 0
    vram: int = 0
    processes: int = 0


class DiskRow(BaseModel):
    device: str
    mount: str
    fstype: str = ""
    used: int = 0
    total: int = 0
    percent: float = 0.0


class SystemPoint(BaseModel):
    ts: str
    cpu: float = 0.0
    cpu_temp: float = 0.0
    memory: float = 0.0
    swap: int = 0
    gpu: float = 0.0
    gpu_temp: float = 0.0
    vram: float = 0.0
    mem_used: int = 0
    vram_used: int = 0
    disk_read: float = 0.0
    disk_write: float = 0.0
    net_sent: float = 0.0
    net_recv: float = 0.0


class SystemEvent(BaseModel):
    id: int
    ts: str
    kind: str
    level: str = "warning"
    message: str = ""
    value: float = 0.0
    threshold: float = 0.0
    culprits: list[ProcessRow] = Field(default_factory=list)


class SystemState(BaseModel):
    """Everything the System tab draws."""

    available: bool = True
    unavailable_reason: str = ""
    monitoring: bool = False
    last_sample: str = ""
    sample_seconds: float = 30.0
    retention_hours: int = 24
    window_minutes: int = 60
    now: SystemNow = Field(default_factory=SystemNow)
    disks: list[DiskRow] = Field(default_factory=list)
    top_cpu: list[ProcessRow] = Field(default_factory=list)
    top_memory: list[ProcessRow] = Field(default_factory=list)
    top_vram: list[ProcessRow] = Field(default_factory=list)
    apps: list[AppLoad] = Field(default_factory=list)
    process_count: int = 0
    points: list[SystemPoint] = Field(default_factory=list)
    events: list[SystemEvent] = Field(default_factory=list)
    gpu_note: str = ""


class Stats(BaseModel):
    total: int
    running: int
    stopped: int
    crashed: int
    external: int
    ports_in_use: int


class ActionResult(BaseModel):
    ok: bool
    message: str
    runtime: Runtime


class Capture(BaseModel):
    """One webcam still, filed against the event that triggered it."""

    id: int
    app_id: int | None = None
    app_name: str = ""
    # started · stopped · crashed · takeover · shutdown (God Father itself)
    # power_loss (the last frame before the machine died) · test
    kind: str
    message: str = ""
    file: str = ""
    # Folder of preserved frames, if this capture has footage of its own. Only a
    # power cut does: everything else's surroundings are still in the ring.
    clip: str = ""
    ok: bool = False
    note: str = ""
    created_at: str


class CameraState(BaseModel):
    enabled: bool = False
    device: str = ""
    devices: list[str] = Field(default_factory=list)
    max_mb: int = 500
    used_mb: float = 0.0
    count: int = 0
    available: bool = True
    unavailable_reason: str | None = None
    capture_starts: bool = False
    incident_minutes: float = 5.0
    incident_mb: float = 0.0

    # The most recent power cut that still has rescued footage, so the view can
    # offer it directly instead of making someone find the card it belongs to.
    # Looked up rather than derived from the loaded list: a power cut can be
    # thousands of captures back, and this is the one recording that cannot be
    # taken again.
    last_cut_id: int | None = None
    last_cut_at: str | None = None
    last_cut_frames: int = 0

    # The recording. `recording` is whether the stream process is alive;
    # `healthy` is whether frames are actually landing. Keeping them apart is
    # the point — a stream that is alive and silently producing nothing is the
    # failure mode that looks exactly like success, and it is the reason
    # newest_age is published rather than assumed.
    recording: bool = False
    healthy: bool = False
    newest_age: float | None = None
    interval: int = 10
    keep_minutes: int = 20
    ring_frames: int = 0
    ring_mb: float = 0.0
    restarts: int = 0
    stream_error: str = ""


class RingSummary(BaseModel):
    """What the rolling recording currently holds, for the clip player."""

    frames: int = 0
    minutes_held: float = 0.0
    first: str | None = None
    last: str | None = None
    interval: int = 10
    keep_minutes: int = 20
    fps: int = 4
    # One second of clip is this many seconds of real time.
    speed: int = 40
    playable: bool = False
    note: str = ""


class CameraUpdate(BaseModel):
    enabled: bool | None = None
    device: str | None = None
    max_mb: int | None = Field(default=None, ge=1, le=100_000)
    capture_starts: bool | None = None
    interval: int | None = Field(default=None, ge=2, le=300)
    keep_minutes: int | None = Field(default=None, ge=1, le=1440)
    incident_minutes: float | None = Field(default=None, ge=0.5, le=120)


# ------------------------------------------------- which app uses which model


class AiLink(BaseModel):
    """One statement that an application uses a model, and who is making it."""

    id: int
    app_id: int
    provider: str = ""
    provider_label: str = ""
    model: str = ""
    # declared = written down by hand · observed = actually seen in use
    # detected = read out of the application's own source
    source: Literal["declared", "observed", "detected"] = "detected"
    # For a detected link, what kind of evidence: env, code or dependency.
    kind: str = ""
    evidence: str = ""
    first_seen: str = ""
    last_seen: str = ""


class AiAppRow(BaseModel):
    """One application's whole relationship with AI, in one row."""

    app_id: int | None = None
    app: str
    running: bool = False
    providers: list[str] = Field(default_factory=list)
    provider_labels: list[str] = Field(default_factory=list)
    models: list[str] = Field(default_factory=list)
    links: list[AiLink] = Field(default_factory=list)
    # A socket to a provider is open right now.
    live: bool = False
    connections: int = 0
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    errors: int = 0
    last_used: str = ""
    # False means "we know it uses this, we cannot count it" — which is a
    # different thing from zero, and is shown as such.
    measured: bool = False


class AiModelRow(BaseModel):
    provider: str = ""
    provider_label: str = ""
    model: str = ""
    apps: list[str] = Field(default_factory=list)
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    errors: int = 0
    avg_latency_ms: float = 0.0
    last_used: str = ""
    # USD per million tokens, from the built-in table. 0 means "not priced".
    price_in: float = 0.0
    price_out: float = 0.0
    local: bool = False


class AiLiveRow(BaseModel):
    app_id: int
    app: str
    provider: str = ""
    provider_label: str = ""
    connections: int = 0
    remote: str = ""
    local: bool = False


class AiLocalModel(BaseModel):
    name: str
    size: int = 0
    vram: int = 0
    expires_at: str = ""
    # Loaded into memory now, as opposed to merely downloaded.
    loaded: bool = False


class AiCall(BaseModel):
    id: int
    ts: str
    app_id: int | None = None
    app_name: str = ""
    provider: str = ""
    provider_label: str = ""
    model: str = ""
    source: str = "ingest"
    endpoint: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    ok: bool = True
    error: str = ""


class AiPoint(BaseModel):
    ts: str
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    errors: int = 0
    connections: int = 0


class AiTotals(BaseModel):
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    errors: int = 0
    avg_latency_ms: float = 0.0


class AiState(BaseModel):
    """Everything the AI models tab draws."""

    available: bool = True
    unavailable_reason: str = ""
    monitoring: bool = False
    last_sample: str = ""
    last_error: str = ""
    sample_seconds: float = 15.0
    retention_hours: int = 24
    window_minutes: int = 60
    totals: AiTotals = Field(default_factory=AiTotals)
    apps: list[AiAppRow] = Field(default_factory=list)
    models: list[AiModelRow] = Field(default_factory=list)
    live: list[AiLiveRow] = Field(default_factory=list)
    ollama_reachable: bool = False
    local_models: list[AiLocalModel] = Field(default_factory=list)
    points: list[AiPoint] = Field(default_factory=list)
    calls: list[AiCall] = Field(default_factory=list)
    # How many provider addresses the resolver currently knows. Zero means the
    # live half cannot identify anything, which is worth saying out loud.
    known_addresses: int = 0


class AiUsageIn(BaseModel):
    """One call, reported by the application that made it.

    Identify the app however is convenient from inside it — its registered
    name, its God Father id, or the port it serves on. A report that matches
    nothing is still counted, filed under the name it gave.
    """

    app: str = ""
    app_id: int | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    provider: str = ""
    model: str = ""
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    latency_ms: float = Field(default=0.0, ge=0)
    ok: bool = True
    error: str = ""
    endpoint: str = ""
    # Let a caller batch up and report late without the row landing on now.
    ts: str = ""


class AiDeclareIn(BaseModel):
    app_id: int
    provider: str = ""
    model: str = Field(default="", max_length=120)
    note: str = ""

    @field_validator("model")
    @classmethod
    def _clean_model(cls, v: str) -> str:
        return (v or "").strip().lower()


# ------------------------------------------------------------------ analytics


class TrafficPath(BaseModel):
    app_id: int | None = None
    method: str
    path: str
    requests: int = 0
    errors: int = 0
    avg_ms: float | None = None


class TrafficFigures(BaseModel):
    requests: int = 0
    # Average over the whole window, and over the last five minutes.
    rpm: float = 0.0
    rpm_now: float = 0.0
    s2xx: int = 0
    s3xx: int = 0
    s4xx: int = 0
    s5xx: int = 0
    errors: int = 0
    error_rate: float = 0.0
    server_error_rate: float = 0.0
    loopback: int = 0
    lan: int = 0
    external: int = 0
    bytes: int = 0
    # How many requests carried a duration. Zero means avg_ms and p95_ms are
    # unknown, not fast — uvicorn, for one, never prints a duration.
    timed: int = 0
    avg_ms: float | None = None
    p95_ms: float | None = None


class TrafficApp(TrafficFigures):
    app_id: int
    name: str
    port: int | None = None
    status: str = "stopped"
    # False when this app's log has never shown a request line — then every
    # count above is unknown rather than zero, and `note` says why.
    counted: bool = False
    note: str = ""
    last_request: str | None = None
    share: float = 0.0
    cpu_percent: float | None = None
    memory_mb: float | None = None
    uptime_seconds: float | None = None
    crashes: int = 0
    restarts: int = 0
    spark: list[int] = Field(default_factory=list)
    top_paths: list[TrafficPath] = Field(default_factory=list)


class TrafficPoint(BaseModel):
    ts: str
    requests: int = 0
    errors: int = 0
    rpm: float = 0.0


class TrafficTotals(TrafficFigures):
    apps_total: int = 0
    apps_counted: int = 0
    apps_active: int = 0
    running: int = 0
    stopped: int = 0
    crashed: int = 0
    cpu_percent: float = 0.0
    memory_mb: float = 0.0
    crashes: int = 0
    restarts: int = 0


class TrafficState(BaseModel):
    """Everything the Analytics tab on the live wall draws."""

    monitoring: bool = False
    last_scan: str = ""
    last_error: str = ""
    sample_seconds: float = 10.0
    retention_days: int = 7
    window_minutes: int = 60
    bucket_minutes: float = 1.0
    totals: TrafficTotals = Field(default_factory=TrafficTotals)
    apps: list[TrafficApp] = Field(default_factory=list)
    points: list[TrafficPoint] = Field(default_factory=list)
    # Requests by local hour of day across the window, 0–23.
    by_hour: list[int] = Field(default_factory=list)
    top_paths: list[TrafficPath] = Field(default_factory=list)
