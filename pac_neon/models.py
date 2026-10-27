from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional
import copy
import uuid

try:
    from pydantic import BaseModel, Field  # type: ignore
except ImportError:
    class _FieldDef:
        def __init__(self, default: Any = None, default_factory: Any = None):
            self.default = default
            self.default_factory = default_factory

    def Field(default: Any = None, *, default_factory: Any = None, **_: Any) -> Any:  # type: ignore
        return _FieldDef(default=default, default_factory=default_factory)

    class BaseModel:  # type: ignore
        def __init__(self, **kwargs: Any):
            for cls in reversed(self.__class__.__mro__):
                ann = getattr(cls, "__annotations__", {})
                for key in ann:
                    if key in kwargs:
                        setattr(self, key, kwargs[key])
                    elif hasattr(cls, key):
                        val = getattr(cls, key)
                        if isinstance(val, _FieldDef):
                            setattr(
                                self,
                                key,
                                val.default_factory() if val.default_factory is not None else copy.deepcopy(val.default),
                            )
                        else:
                            setattr(self, key, copy.deepcopy(val))
                    else:
                        setattr(self, key, None)
            for k, v in kwargs.items():
                setattr(self, k, v)

        def model_dump(self) -> Dict[str, Any]:
            def _conv(v: Any) -> Any:
                if isinstance(v, BaseModel):
                    return v.model_dump()
                if isinstance(v, Enum):
                    return v.value
                if isinstance(v, list):
                    return [_conv(i) for i in v]
                if isinstance(v, dict):
                    return {dk: _conv(dv) for dk, dv in v.items()}
                return v

            out: Dict[str, Any] = {}
            for k, v in self.__dict__.items():
                if not k.startswith("_"):
                    out[k] = _conv(v)
            return out


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class AgentGameState(str, Enum):
    """Pac-Man inspired game states representing real agent activity."""

    CHOMPING = "CHOMPING"              # Coding / Editing files (eating pellets!)
    THINKING = "THINKING"              # Reasoning / Planning
    RUNNING_TOOL = "RUNNING_TOOL"      # Executing CLI / MCP tool
    POWER_PELLET_WAIT = "POWER_PELLET_WAIT"  # Waiting for user permission (y/n) or input!
    WARPING = "WARPING"                # Sending or receiving cross-repo context
    STAGE_CLEAR = "STAGE_CLEAR"        # Task completed
    GAME_OVER = "GAME_OVER"            # Error / Crashed
    IDLE = "IDLE"                      # Ready in spawn house


class PermissionResolutionAction(str, Enum):
    ALLOW = "allow"
    ALWAYS = "always"
    DENY = "deny"
    CUSTOM = "custom"


class PermissionRequestItem(BaseModel):
    id: str = Field(default_factory=lambda: f"perm-{uuid.uuid4().hex[:8]}")
    agent_id: str
    agent_name: str
    provider: str
    repo_name: str
    tool_name: str = "Bash / Terminal"
    command_preview: str
    reason: str = "Agente aguardando confirmação para executar ação."
    full_details: Dict[str, Any] = Field(default_factory=dict)
    options: List[str] = Field(default_factory=list)
    channel: str = "pty"  # "pty" | "hook" | "sdk" | "simulated"
    status: str = "PENDING"  # "PENDING" | "APPROVED" | "DENIED"
    resolution_input: Optional[str] = None
    created_at: str = Field(default_factory=utc_now_iso)
    resolved_at: Optional[str] = None


class ContextFruit(BaseModel):
    """A 'Pac-Man Bonus Fruit' carrying shared context across repositories via the Warp Tunnel."""

    id: str = Field(default_factory=lambda: f"fruit-{uuid.uuid4().hex[:8]}")
    fruit_type: str = "cherry_contract"  # cherry_contract | strawberry_spec | orange_pr | key_token
    title: str
    source_agent_id: str
    source_agent_name: str
    source_repo: str
    target_agent_id: Optional[str] = None
    target_repo: str
    content: str
    created_at: str = Field(default_factory=utc_now_iso)
    consumed: bool = False


class ProviderMetadata(BaseModel):
    """Describes an AI Agent Provider (Antigravity, Claude, Gemini, Codex, or future ones)."""

    id: str
    display_name: str
    arcade_title: str
    neon_color: str
    secondary_color: str
    sprite_type: str  # "pac_neon" | "ghost_clyde" | "ghost_inky" | "ghost_funky" | "ghost_pinky"
    log_paths: List[str] = Field(default_factory=list)
    cli_command: str = ""
    supports_pty_wrap: bool = True
    supports_hooks: bool = True


class AgentUnit(BaseModel):
    """Normalized state of a single working AI Agent in any context/repository."""

    id: str
    name: str
    provider: str
    repo_name: str
    repo_path: str
    branch: str = "main"
    game_state: AgentGameState = AgentGameState.CHOMPING
    score_xp: int = 100
    pellets_eaten: int = 1
    pellets_total: int = 20
    context_health_pct: int = 85  # Remaining context window % ("1UP / Shield")
    hallucination_risk_pct: int = 0  # 0-100% (<25% Baixo/Focado, 25-59% Atenção, >=60% Crítico/Alucinando)
    health_status: str = "HEALTHY"  # "HEALTHY" | "WARNING" | "CRITICAL" | "STALLED"
    context_tokens_used: int = 0  # Real or estimated tokens in current context window
    context_tokens_limit: int = 200000
    active_elapsed_sec: int = 0  # Seconds spent in current turn/action or last turn duration
    is_stalled: bool = False  # True if agent appears hung or stalled
    health_signals: List[str] = Field(default_factory=list)
    current_action: str = "Aguardando próxima missão..."
    last_thought: str = ""
    active_tool: Optional[str] = None
    terminal_tail: List[str] = Field(default_factory=list)
    artifacts: List[str] = Field(default_factory=list)
    subagents: List[str] = Field(default_factory=list)
    pending_permission: Optional[PermissionRequestItem] = None
    controllable: bool = True
    control_mode: str = "pty"  # "pty" | "hook" | "sdk" | "watcher" | "superset"
    neon_color: str = "#00E5FF"
    sprite_type: str = "pac_neon"
    updated_at: str = Field(default_factory=utc_now_iso)
    is_live_discovered: bool = False
    workspace_title: str = ""
    session_id: Optional[str] = None
    archived: bool = False
    archived_at: Optional[str] = None
    pid: Optional[int] = None
    tty: Optional[str] = None
    host_app: Optional[str] = None
    last_active_ts: float = 0.0
    superset_workspace_id: Optional[str] = None
    superset_terminal_id: Optional[str] = None
    superset_deep_link: Optional[str] = None
    neon_meta: Dict[str, Any] = Field(default_factory=dict)


def compute_context_and_hallucination_metrics(
    *,
    tokens_used: int,
    tokens_limit: int = 200000,
    steps_count: int = 0,
    recent_tool_errors: int = 0,
    last_error_target: str = "",
    thrashing_target: str = "",
    thrashing_count: int = 0,
    recent_interruptions: int = 0,
    truncated_steps_count: int = 0,
    overthinking_read_only_count: int = 0,
    game_state: Optional[AgentGameState] = None,
    age_seconds: float = 0.0,
    active_elapsed_sec: int = 0,
) -> Dict[str, Any]:
    """Calculates real context health %, hallucination risk %, stall/overthinking status, and diagnostic signals in PT-BR."""
    safe_limit = max(1, int(tokens_limit or 200000))
    safe_tokens = max(0, int(tokens_used or 0))
    if safe_tokens == 0 and steps_count > 0:
        safe_tokens = min(safe_limit, steps_count * 750)

    token_usage_pct = min(100, int(round((safe_tokens / safe_limit) * 100)))
    elapsed_sec = max(0, int(active_elapsed_sec or 0))
    inactivity_sec = max(0, int(round(age_seconds or 0.0)))

    if game_state in (AgentGameState.RUNNING_TOOL, AgentGameState.THINKING, AgentGameState.CHOMPING):
        elapsed_sec = max(elapsed_sec, inactivity_sec)

    # 1. Context Window Saturation Risk
    if token_usage_pct <= 40:
        window_risk = int(token_usage_pct * 0.15)  # 0..6%
    elif token_usage_pct <= 70:
        window_risk = 6 + int((token_usage_pct - 40) * 0.45)  # 6..19%
    elif token_usage_pct <= 85:
        window_risk = 19 + int((token_usage_pct - 70) * 1.2)  # 19..37%
    else:
        window_risk = 37 + int((token_usage_pct - 85) * 2.2)  # 37..70%

    # 2. Recent Tool / Edit Errors Risk (in last 15 steps)
    error_risk = min(48, recent_tool_errors * 14)
    if recent_tool_errors >= 3:
        error_risk = max(error_risk, 34)

    # 3. Loop / Thrashing Risk (>=3 repeated calls on the same target in last 10 steps)
    thrash_risk = 0
    if thrashing_count >= 3:
        thrash_risk = min(55, 32 + (thrashing_count - 3) * 10)

    # 4. Analysis Paralysis / Overthinking Risk (>=12 read/search tools in last 15 steps without edit/conclusion)
    overthink_risk = 0
    if overthinking_read_only_count >= 12:
        overthink_risk = min(38, 26 + (overthinking_read_only_count - 12) * 4)

    # 5. User Interruptions & Context Truncations Risk
    interrupt_risk = min(24, recent_interruptions * 12)
    trunc_risk = min(18, truncated_steps_count * 2) if truncated_steps_count >= 3 else 0

    hallucination_risk_pct = min(
        100,
        max(0, window_risk + error_risk + thrash_risk + overthink_risk + interrupt_risk + trunc_risk),
    )

    # Context Health % discounts both token window saturation and active degradation/hallucination signals
    degradation_penalty = int((error_risk + thrash_risk + overthink_risk + interrupt_risk + trunc_risk) * 0.65)
    raw_health = 100 - int(token_usage_pct * 0.60) - degradation_penalty
    context_health_pct = max(5, min(100, raw_health))

    # 6. Stall & Hung Tool / Inference Detection
    is_stalled = False
    stall_signals: List[str] = []
    if game_state == AgentGameState.RUNNING_TOOL and age_seconds > 90:
        is_stalled = True
        stall_signals.append(
            f"⏳ Comando em execução há mais de {inactivity_sec}s (possível processo travado ou aguardando input)"
        )
    elif game_state in (AgentGameState.THINKING, AgentGameState.CHOMPING) and age_seconds > 60:
        is_stalled = True
        stall_signals.append(
            f"⏳ IA sem resposta há {inactivity_sec}s (possível lentidão de API ou contexto pesado)"
        )

    # Traffic-light Health Status
    if is_stalled:
        health_status = "STALLED"
    elif (
        hallucination_risk_pct >= 60
        or context_health_pct < 35
        or token_usage_pct >= 90
        or thrashing_count >= 4
    ):
        health_status = "CRITICAL"
    elif (
        hallucination_risk_pct >= 25
        or context_health_pct < 65
        or token_usage_pct >= 70
        or recent_tool_errors >= 2
        or thrashing_count >= 3
        or overthinking_read_only_count >= 12
        or elapsed_sec > 120
    ):
        health_status = "WARNING"
    else:
        health_status = "HEALTHY"

    # Human-readable diagnostic signals in Portuguese (up to 4)
    signals: List[str] = list(stall_signals)

    if overthinking_read_only_count >= 12:
        signals.append("🌀 Espiral de busca: muitas leituras seguidas sem progresso de código")

    if thrashing_count >= 3 and thrashing_target:
        signals.append(f"Loop detectado: {thrashing_count} falhas seguidas em {thrashing_target}")
    elif thrashing_count >= 3:
        signals.append(f"Loop detectado: {thrashing_count} chamadas repetidas seguidas na mesma ferramenta")

    if recent_tool_errors >= 2:
        suffix = f" ({last_error_target})" if last_error_target else ""
        signals.append(f"{recent_tool_errors} erros recentes de tool/edição{suffix}")
    elif recent_tool_errors == 1:
        suffix = f" ({last_error_target})" if last_error_target else ""
        signals.append(f"1 falha recente de tool/edição{suffix}")

    if elapsed_sec > 120 and not is_stalled:
        signals.append(f"⏱️ Turno lento: {elapsed_sec}s acumulados (acima de 120s)")

    tokens_k = f"{max(1, safe_tokens // 1000)}k" if safe_tokens >= 1000 else str(safe_tokens)
    limit_k = f"{safe_limit // 1000}k"
    if token_usage_pct >= 75:
        signals.append(f"Janela de contexto crítica em {token_usage_pct}% ({tokens_k}/{limit_k} tokens)")
    elif token_usage_pct >= 45:
        signals.append(f"Janela de contexto em {token_usage_pct}% ({tokens_k}/{limit_k} tokens)")

    if recent_interruptions >= 1 and len(signals) < 4:
        if recent_interruptions == 1:
            signals.append("1 interrupção manual recente pelo usuário")
        else:
            signals.append(f"{recent_interruptions} interrupções manuais recentes pelo usuário")

    if truncated_steps_count >= 3 and len(signals) < 4:
        signals.append(f"{truncated_steps_count} truncamentos de saída por pressão de contexto")

    return {
        "context_health_pct": context_health_pct,
        "hallucination_risk_pct": hallucination_risk_pct,
        "health_status": health_status,
        "context_tokens_used": safe_tokens,
        "context_tokens_limit": safe_limit,
        "active_elapsed_sec": elapsed_sec,
        "is_stalled": is_stalled,
        "health_signals": signals[:4],
    }



class NeonCliActionRequest(BaseModel):
    action: str  # "gh_open" | "gh_prs" | "dd_service" | "dd_logs" | "flow_open" | "pam_pam_token"
    env: str = "prod"
    jira_key: Optional[str] = None


class ArchiveAgentRequest(BaseModel):
    archived: bool = True


class RenameWorkspaceRequest(BaseModel):
    title: str
    sync_external: bool = True


class SpawnAgentRequest(BaseModel):
    provider: str
    name: Optional[str] = None
    repo_path: str
    initial_prompt: Optional[str] = None
    command_override: Optional[str] = None
    target_host: Optional[str] = None
    use_real_pty: bool = False
    use_superset: bool = False
    superset_workspace_id: Optional[str] = None



class DirectCommandRequest(BaseModel):
    prompt: str
    auto_enter: bool = True


class ResolvePermissionRequest(BaseModel):
    action: PermissionResolutionAction
    custom_text: Optional[str] = None


class SendWarpFruitRequest(BaseModel):
    source_agent_id: str
    target_agent_id: str
    fruit_type: str = "cherry_contract"
    title: str
    content: str


class SelfHandoffRequest(BaseModel):
    custom_handoff: Optional[str] = None
    mode: str = "clear_and_continue"  # "clear_and_continue" | "compact"


class HookPermissionPayload(BaseModel):
    agent_id: Optional[str] = None
    agent_name: Optional[str] = None
    provider: str = "claude"
    repo_path: str = "."
    tool_name: str
    command_preview: str
    reason: Optional[str] = None
    timeout_seconds: int = 120
