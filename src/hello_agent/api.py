"""把命令行 Agent 暴露为 HTTP API。"""

from __future__ import annotations

import asyncio
import csv
from collections.abc import Callable
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
from threading import Lock
from time import perf_counter
from typing import Literal
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, Response, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from openai import OpenAIError
from pydantic import BaseModel, Field

from hello_agent.auth import AuthUser, LOGIN_SESSION_DAYS, SqliteAuthService
from hello_agent.admin import SqliteAdminService, list_release_records
from hello_agent.admin_auth import (
    ADMIN_SESSION_HOURS,
    AdminPrincipal,
    SqliteAdminAuthService,
)
from hello_agent.app import (
    AgentSession,
    DEFAULT_DATABASE_FILE,
    DEFAULT_TODO_FILE,
    MissingAPIKeyError,
    ToolActivity,
)
from hello_agent.collaboration import resume_collaboration, run_collaboration
from hello_agent.collaboration_routing import route_conversation_mode
from hello_agent.skills import list_skills, load_skill
from hello_agent.database import (
    SqliteAgentOpsStore,
    SqliteAutomationStore,
    SqliteChatAttachmentStore,
    SqliteCollaborationStore,
    SqliteConversationStore,
    SqliteEvaluationStore,
    SqliteKnowledgeDocumentStore,
    SqliteMemoryStore,
    SqliteMcpServerStore,
    SqliteObservabilityStore,
    SqliteOpenApiSourceStore,
    SqliteTodoStore,
    SqliteToolResultStore,
    SqliteTaskStore,
    SqliteTravelStore,
    SqliteWorkflowStore,
)
from hello_agent.automations import briefing_timezone
from hello_agent.scheduler import scheduler_enabled
from hello_agent.memory import MemoryCategory
from hello_agent.knowledge_documents import (
    MAX_UPLOAD_BYTES,
    delete_knowledge_document,
    public_knowledge_dir,
    read_knowledge_document_text,
    save_knowledge_document,
    seed_demo_knowledge,
    user_knowledge_dir,
    PUBLIC_KNOWLEDGE_OWNER,
)
from hello_agent.chat_attachments import MAX_ATTACHMENTS_PER_SESSION
from hello_agent.mcp_client import (
    KnowledgeMCPClient,
    MCPClientRegistry,
    WeatherMCPClient,
    build_user_mcp_registry,
    probe_remote_mcp,
    validate_remote_mcp_url,
)
from hello_agent.openapi_registry import build_user_openapi_registry
from hello_agent.openapi_tools import (
    parse_openapi_document,
    preview_openapi,
    select_operations,
)
from hello_agent.evaluations import run_core_evaluation
from hello_agent.knowledge import search_knowledge
from hello_agent.vector_knowledge import hybrid_search
from hello_agent.metrics import collect_metrics, collect_platform_cost
from hello_agent.task_queue import (
    cancel_task,
    resume_workflow_run,
    retry_task,
    schedule_daily_todo_briefing,
    schedule_evaluation_run,
    schedule_knowledge_index,
    schedule_workflow_run,
)
from hello_agent.planning import GoalPlan, PlanStep
from hello_agent.travel import TravelPlan
from hello_agent.workflows import (
    WorkflowDefinition,
    stream_workflow,
)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    session_id: UUID | None = None
    approve: bool = False
    operation_type: str | None = Field(default=None, max_length=40)


class ChatResponse(BaseModel):
    session_id: UUID
    answer: str
    approval_required: bool = False
    sources: list[dict[str, object]] = Field(default_factory=list)
    confidence: str = "none"
    grounding: str = "none"
    captured_regression: bool = False


class CollaborationRequest(BaseModel):
    goal: str = Field(min_length=1, max_length=4000)
    session_id: UUID | None = None
    operation_type: str = Field(default="collaboration", max_length=40)


class CollaborationRejectResponse(BaseModel):
    collaboration_id: UUID
    status: Literal["cancelled"] = "cancelled"


class CollaborationRoutingRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)


class CollaborationRoutingResponse(BaseModel):
    mode: Literal["chat", "collaboration"]
    use_collaboration: bool
    reason: str
    score: int = 0


class HealthResponse(BaseModel):
    status: str
    ocr_enabled: bool = False
    ocr_available: bool = False
    ocr_languages: str = ""


class AuthRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=8, max_length=128)


class RegisterRequest(AuthRequest):
    display_name: str = Field(min_length=1, max_length=50)


class UserResponse(BaseModel):
    id: str
    email: str
    display_name: str
    role: Literal["knowledge_manager", "member"]
    is_active: bool
    can_access_admin: bool = False


class AdminUserResponse(UserResponse):
    created_at: datetime
    last_login_at: datetime | None


class AdminUserListResponse(BaseModel):
    users: list[AdminUserResponse]


class AdminUserUpdateRequest(BaseModel):
    role: Literal["knowledge_manager", "member"]
    is_active: bool


class ManagedAdminUserResponse(BaseModel):
    id: UUID
    email: str
    display_name: str
    is_active: bool
    can_manage_knowledge: bool
    created_at: datetime
    last_login_at: datetime | None


class ManagedAdminUserListResponse(BaseModel):
    admins: list[ManagedAdminUserResponse]


class ManagedAdminUserUpdateRequest(BaseModel):
    is_active: bool
    can_manage_knowledge: bool


class AdminOverviewResponse(BaseModel):
    users: dict[str, object]
    runs_24h: dict[str, object]
    knowledge: dict[str, object]
    evaluations_7d: dict[str, object]
    activity_7d: list[dict[str, object]]
    generated_at: datetime
    privacy_notice: str


class AdminAuditUserResponse(BaseModel):
    id: UUID
    email: str
    display_name: str


class AdminAuditResponse(BaseModel):
    id: int
    action: str
    actor: AdminAuditUserResponse | None
    target: AdminAuditUserResponse | None
    changes: dict[str, object]
    ip_address: str | None
    created_at: datetime


class AdminAuditListResponse(BaseModel):
    logs: list[AdminAuditResponse]


class AdminPrincipalResponse(BaseModel):
    id: UUID
    email: str
    display_name: str
    role: Literal["admin"] = "admin"
    mfa_rebind_required: bool = False
    can_manage_knowledge: bool = False


class AdminPasswordLoginRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    password: str = Field(min_length=1, max_length=128)


class AdminLoginChallengeResponse(BaseModel):
    challenge_token: str
    requires_setup: bool
    email_recovery_available: bool
    expires_in: int


class AdminMFASetupRequest(BaseModel):
    challenge_token: str = Field(min_length=20, max_length=200)


class AdminMFASetupResponse(BaseModel):
    secret: str
    otpauth_uri: str


class AdminMFAVerifyRequest(AdminMFASetupRequest):
    code: str = Field(min_length=6, max_length=32)


class AdminMFAActivateRequest(AdminMFAVerifyRequest):
    new_password: str = Field(min_length=12, max_length=128)


class AdminMFAActivateResponse(BaseModel):
    user: AdminPrincipalResponse
    recovery_codes: list[str]


class AdminSecurityResponse(BaseModel):
    authenticator_enabled: bool
    email_otp_available: bool
    email_verified: bool
    email_recovery_enabled: bool
    masked_email: str
    email: str
    recovery_email: str | None
    recovery_codes_remaining: int
    active_sessions: int
    idle_timeout_minutes: int
    absolute_timeout_hours: int


class AdminSessionRevokeResponse(BaseModel):
    revoked_count: int


class AdminEmailSendRequest(BaseModel):
    challenge_token: str = Field(min_length=20, max_length=200)


class AdminEmailCodeRequest(AdminEmailSendRequest):
    code: str = Field(min_length=6, max_length=6)


class AdminEmailEnableRequest(BaseModel):
    code: str = Field(min_length=6, max_length=6)


class AdminEmailConfigureRequest(BaseModel):
    email: str | None = Field(default=None, min_length=3, max_length=254)


class AdminEmailSendResponse(BaseModel):
    masked_email: str
    expires_in: int = 300


class AdminMFARebindResponse(BaseModel):
    user: AdminPrincipalResponse
    recovery_codes: list[str]


class HistoryMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str
    operation_type: str | None = None
    sources: list[dict[str, object]] | None = None
    confidence: str | None = None
    grounding: str | None = None


class HistoryResponse(BaseModel):
    session_id: UUID
    messages: list[HistoryMessage]


class SessionSummary(BaseModel):
    session_id: UUID
    title: str
    preview: str
    message_count: int


class SessionListResponse(BaseModel):
    sessions: list[SessionSummary]


class AgentRunSummaryResponse(BaseModel):
    id: UUID
    session_id: UUID | None
    run_type: str
    title: str
    status: Literal["running", "success", "failed"]
    model: str | None
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    estimated_cost: float | None
    duration_ms: int | None
    error_message: str | None
    started_at: datetime
    finished_at: datetime | None


class AgentRunStepResponse(BaseModel):
    id: int
    step_type: str
    name: str
    status: str
    duration_ms: int
    detail: str
    created_at: datetime


class AgentRunDetailResponse(AgentRunSummaryResponse):
    steps: list[AgentRunStepResponse]


class AgentRunListResponse(BaseModel):
    runs: list[AgentRunSummaryResponse]


class EvaluationRunRequest(BaseModel):
    confirmed: Literal[True]
    prompt_version_id: UUID | None = None
    dataset_id: UUID | None = None
    model: str | None = Field(default=None, max_length=100)
    scorer: Literal["keyword", "exact", "llm_judge"] = "llm_judge"
    judge_enabled: bool = True
    baseline_run_id: UUID | None = None


class EvaluationRunSummaryResponse(BaseModel):
    id: UUID
    model: str
    prompt_version_id: UUID | None = None
    prompt_version_name: str | None = None
    dataset_id: UUID | None = None
    dataset_name: str | None = None
    scorer: Literal["keyword", "exact", "llm_judge"] = "keyword"
    judge_enabled: bool = False
    confidence: Literal["high", "medium", "low"] = "low"
    baseline_run_id: UUID | None = None
    regression_summary: dict[str, object] = Field(default_factory=dict)
    export_metadata: dict[str, object] = Field(default_factory=dict)
    status: Literal["queued", "running", "completed", "failed"]
    total_cases: int
    passed_cases: int
    score: float
    total_tokens: int
    estimated_cost: float | None = None
    duration_ms: int | None
    started_at: datetime
    finished_at: datetime | None


class EvaluationCaseResponse(BaseModel):
    id: int
    case_id: str
    name: str
    category: str
    status: Literal["passed", "failed"]
    duration_ms: int
    score: float = 0
    scorer: str = "keyword"
    confidence: Literal["high", "medium", "low"] = "low"
    failure_type: str | None = None
    failure_reason: str | None = None
    judge_score: float | None = None
    judge_summary: str | None = None
    judge_reasoning: str | None = None
    signals: dict[str, object] = Field(default_factory=dict)
    baseline_status: Literal["passed", "failed"] | None = None
    regression_label: Literal["new_failure", "fixed", "persistent_failure", "persistent_pass"] | None = None
    expected: str
    actual: str
    error_message: str | None


class EvaluationRunDetailResponse(EvaluationRunSummaryResponse):
    cases: list[EvaluationCaseResponse]


class EvaluationComparisonCaseResponse(BaseModel):
    case_id: str
    name: str
    current_status: Literal["passed", "failed"] | None = None
    baseline_status: Literal["passed", "failed"] | None = None
    current_score: float | None = None
    baseline_score: float | None = None
    regression_label: Literal["new_failure", "fixed", "persistent_failure", "persistent_pass", "added"] | None = None
    failure_reason: str | None = None


class EvaluationComparisonResponse(BaseModel):
    current_run_id: UUID
    baseline_run_id: UUID
    score_delta: float
    confidence_delta: int
    summary: dict[str, int]
    cases: list[EvaluationComparisonCaseResponse]


class EvaluationRunListResponse(BaseModel):
    runs: list[EvaluationRunSummaryResponse]


class PromptVersionResponse(BaseModel):
    id: UUID
    version: int
    name: str
    content: str
    change_note: str
    status: Literal["draft", "active", "archived"]
    created_at: datetime
    activated_at: datetime | None


class EvaluationTestCaseResponse(BaseModel):
    id: UUID
    dataset_id: UUID
    name: str
    category: str
    input_text: str
    scoring_method: Literal["keyword", "exact", "llm_judge"] = "keyword"
    expected_answer: str | None = None
    judge_rubric: str | None = None
    expected_keywords: list[str]
    enabled: bool
    created_at: datetime
    updated_at: datetime


class EvaluationDatasetResponse(BaseModel):
    id: UUID
    name: str
    description: str
    is_default: bool
    created_at: datetime
    updated_at: datetime
    cases: list[EvaluationTestCaseResponse]


class AgentOpsConfigResponse(BaseModel):
    prompts: list[PromptVersionResponse]
    datasets: list[EvaluationDatasetResponse]


class PromptCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    content: str = Field(min_length=1, max_length=12000)
    change_note: str = Field(default="", max_length=300)


class DatasetCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=300)


class EvaluationCaseCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    category: str = Field(default="general", max_length=30)
    input_text: str = Field(min_length=1, max_length=4000)
    scoring_method: Literal["keyword", "exact", "llm_judge"] = "keyword"
    expected_keywords: list[str] = Field(default_factory=list, max_length=20)
    expected_answer: str | None = Field(default=None, max_length=4000)
    judge_rubric: str | None = Field(default=None, max_length=2000)


class EvaluationCaseUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    category: str | None = Field(default=None, max_length=30)
    input_text: str | None = Field(default=None, min_length=1, max_length=4000)
    scoring_method: Literal["keyword", "exact", "llm_judge"] | None = None
    expected_keywords: list[str] | None = Field(default=None, max_length=20)
    expected_answer: str | None = Field(default=None, max_length=4000)
    judge_rubric: str | None = Field(default=None, max_length=2000)
    enabled: bool | None = None


class EvaluationLaunchResponse(BaseModel):
    run: EvaluationRunDetailResponse
    task: BackgroundTaskResponse


class WorkflowSaveRequest(BaseModel):
    id: UUID | None = None
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=300)
    definition: WorkflowDefinition


class WorkflowResponse(BaseModel):
    id: UUID
    name: str
    description: str
    definition: WorkflowDefinition
    created_at: datetime
    updated_at: datetime


class WorkflowListResponse(BaseModel):
    workflows: list[WorkflowResponse]


class WorkflowRunRequest(BaseModel):
    input: str = Field(default="", max_length=4000)


class WorkflowRunStepResponse(BaseModel):
    node_id: str
    node_type: Literal[
        "input", "agent", "knowledge", "llm", "mcp", "todo", "condition", "approval", "output"
    ]
    label: str
    status: Literal["running", "success", "failed", "skipped", "waiting"]
    duration_ms: int
    summary: str
    input: object | None = None
    output: object | None = None


class WorkflowRunResponse(BaseModel):
    id: UUID
    workflow_id: UUID
    status: Literal["queued", "running", "waiting_approval", "success", "failed", "cancelled"]
    input_text: str
    output_text: str
    steps: list[WorkflowRunStepResponse]
    error_message: str | None
    started_at: datetime
    finished_at: datetime | None
    duration_ms: int | None


class WorkflowRunListResponse(BaseModel):
    runs: list[WorkflowRunResponse]


class WorkflowApprovalRequest(BaseModel):
    confirmed: Literal[True]


class PlanRequest(BaseModel):
    goal: str = Field(min_length=1, max_length=2000)
    session_id: UUID | None = None
    operation_type: str = Field(default="plan", max_length=40)


class PlanResponse(BaseModel):
    session_id: UUID
    plan: GoalPlan


class TravelPlanRequest(BaseModel):
    request: str = Field(min_length=1, max_length=3000)
    session_id: UUID | None = None
    travel_plan_id: int | None = Field(default=None, ge=1)
    operation_type: str = Field(default="travel", max_length=40)


class TravelPlanResponse(BaseModel):
    session_id: UUID
    travel_plan_id: int
    version: int
    plan: TravelPlan


class TravelPlanSummaryResponse(BaseModel):
    id: int
    session_id: UUID
    status: Literal["needs_input", "ready", "confirmed"]
    title: str
    summary: str
    origin: str | None
    destination: str | None
    start_date: str | None
    end_date: str | None
    travelers: int | None
    budget: int | None
    created_at: datetime
    updated_at: datetime
    version: int
    confirmed_at: datetime | None
    workflow_completed_count: int
    workflow_total_count: int


class SavedTravelPlanResponse(TravelPlanSummaryResponse):
    preferences: list[str]
    missing_fields: list[str]
    clarification_questions: list[str]
    days: list[dict[str, object]]


class TravelCheckpointResponse(BaseModel):
    version: int
    status: Literal["needs_input", "ready", "confirmed"]
    user_input: str
    created_at: datetime


class TravelPreparationResponse(BaseModel):
    travel_plan_id: int
    plan: GoalPlan
    restored: bool = False


class TravelWorkflowStageResponse(BaseModel):
    key: Literal[
        "requirements", "itinerary", "confirmation", "preparations", "todo_sync"
    ]
    title: str
    status: Literal["pending", "waiting", "completed"]
    detail: str
    completed_at: datetime | None


class TravelWorkflowResponse(BaseModel):
    travel_plan_id: int
    completed_count: int
    total_count: int
    resumable: bool
    stages: list[TravelWorkflowStageResponse]


class TravelTodoSyncRequest(BaseModel):
    selected_steps: list[PlanStep] = Field(min_length=1, max_length=6)
    confirmed: Literal[True]


class PlanTodoSyncRequest(BaseModel):
    session_id: UUID
    plan: GoalPlan
    selected_steps: list[PlanStep] = Field(min_length=1, max_length=6)
    confirmed: Literal[True]


class TodoResponse(BaseModel):
    id: int
    title: str
    completed: bool
    plan_id: int | None = None
    description: str | None = None
    minutes: int | None = None


class TodoCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=100)
    plan_id: int | None = None


class TodoUpdateRequest(BaseModel):
    completed: bool


class PlanTodoSyncResponse(BaseModel):
    session_id: UUID
    plan: "TodoPlanResponse"


class TodoPlanCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=100)
    summary: str = Field(default="", max_length=300)
    priority: Literal["low", "medium", "high"] = "medium"


class TodoPlanResponse(BaseModel):
    id: int
    title: str
    summary: str
    priority: Literal["low", "medium", "high"]
    source_travel_plan_id: int | None = None
    created_at: datetime
    todos: list[TodoResponse]


class TodoPlanListResponse(BaseModel):
    plans: list[TodoPlanResponse]
    inbox: list[TodoResponse]


class MCPToolInfo(BaseModel):
    name: str
    description: str
    parameters: dict[str, object]


class MCPServerInfo(BaseModel):
    id: str | None = None
    name: str
    transport: str
    builtin: bool = True
    enabled: bool = True
    url: str | None = None
    has_auth: bool = False
    status: Literal["ready", "error", "disabled"] = "ready"
    error_message: str | None = None
    tools: list[MCPToolInfo]


class MCPToolsResponse(BaseModel):
    servers: list[MCPServerInfo]


class CreateRemoteMcpRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    url: str = Field(min_length=8, max_length=500)
    transport: Literal["sse", "streamable_http"] = "streamable_http"
    auth_token: str | None = Field(default=None, max_length=500)
    enabled: bool = True


class UpdateRemoteMcpRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    url: str | None = Field(default=None, min_length=8, max_length=500)
    transport: Literal["sse", "streamable_http"] | None = None
    auth_token: str | None = Field(default=None, max_length=500)
    clear_auth_token: bool = False
    enabled: bool | None = None


class OpenApiOperationInfo(BaseModel):
    operation_id: str
    tool_name: str
    method: str
    path: str
    summary: str
    unsafe: bool = False
    write: bool = False


class OpenApiToolInfo(BaseModel):
    name: str
    description: str
    parameters: dict[str, object]
    method: str | None = None
    path: str | None = None
    operation_id: str | None = None
    unsafe: bool = False


class OpenApiSourceInfo(BaseModel):
    id: str
    name: str
    base_url: str
    enabled: bool = True
    has_auth: bool = False
    auth_header: str = "Authorization"
    selected_operations: list[str] = Field(default_factory=list)
    status: Literal["ready", "error", "disabled"] = "ready"
    error_message: str | None = None
    tools: list[OpenApiToolInfo] = Field(default_factory=list)
    created_at: str | None = None
    updated_at: str | None = None


class OpenApiSourcesResponse(BaseModel):
    sources: list[OpenApiSourceInfo]


class OpenApiPreviewRequest(BaseModel):
    name: str = Field(default="OpenAPI", min_length=1, max_length=80)
    spec_text: str = Field(min_length=2, max_length=400_000)
    base_url: str | None = Field(default=None, max_length=500)


class OpenApiPreviewResponse(BaseModel):
    name: str
    title: str
    openapi_version: str
    base_url: str
    operation_count: int
    operations: list[OpenApiOperationInfo]


class CreateOpenApiSourceRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    spec_text: str = Field(min_length=2, max_length=400_000)
    base_url: str | None = Field(default=None, max_length=500)
    selected_operations: list[str] = Field(default_factory=list)
    auth_token: str | None = Field(default=None, max_length=500)
    auth_header: str = Field(default="Authorization", max_length=80)
    enabled: bool = True


class UpdateOpenApiSourceRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    base_url: str | None = Field(default=None, max_length=500)
    selected_operations: list[str] | None = None
    auth_token: str | None = Field(default=None, max_length=500)
    clear_auth_token: bool = False
    auth_header: str | None = Field(default=None, max_length=80)
    enabled: bool | None = None


class SkillSummary(BaseModel):
    name: str
    description: str


class SkillsResponse(BaseModel):
    skills: list[SkillSummary]


class SkillDetailResponse(BaseModel):
    name: str
    description: str
    body: str


class KnowledgeDocumentResponse(BaseModel):
    id: UUID
    original_name: str
    category: str
    file_type: str
    size_bytes: int
    status: Literal["ready", "processing", "failed"]
    chunk_count: int
    embedding_progress: int = 0
    embedding_model: str | None = None
    indexed_at: datetime | None = None
    error_message: str | None
    created_at: datetime
    updated_at: datetime


class KnowledgeDocumentListResponse(BaseModel):
    documents: list[KnowledgeDocumentResponse]


class ChatAttachmentResponse(BaseModel):
    id: UUID
    session_id: UUID
    original_name: str
    file_type: str
    size_bytes: int
    char_count: int
    preview: str = ""
    extraction_method: str = ""
    created_at: datetime


class ChatAttachmentListResponse(BaseModel):
    session_id: UUID
    attachments: list[ChatAttachmentResponse]
    max_attachments: int = MAX_ATTACHMENTS_PER_SESSION


class DemoKnowledgeResponse(BaseModel):
    created_count: int
    documents: list[KnowledgeDocumentResponse]


class KnowledgeDocumentPreviewResponse(BaseModel):
    document: KnowledgeDocumentResponse
    content: str
    truncated: bool


class MemoryItemResponse(BaseModel):
    id: str
    category: MemoryCategory
    content: str
    source_session_id: str | None = None
    created_at: datetime
    updated_at: datetime


class MemoryListResponse(BaseModel):
    memories: list[MemoryItemResponse]
    total: int


class CreateMemoryRequest(BaseModel):
    category: MemoryCategory = "fact"
    content: str = Field(min_length=2, max_length=200)


class KnowledgeSearchMatchResponse(BaseModel):
    source: str
    chunk: int
    content: str
    score: float
    keyword_score: float | None = None
    semantic_score: float | None = None
    retrieval: str = "关键词"


class KnowledgeSearchResponse(BaseModel):
    query: str
    document_count: int
    message: str | None
    confidence: Literal["high", "medium", "low"] = "low"
    retrieval_mode: Literal["hybrid", "keyword"] = "keyword"
    results: list[KnowledgeSearchMatchResponse]


class BackgroundTaskResponse(BaseModel):
    id: UUID
    task_type: str
    title: str
    status: Literal["queued", "running", "waiting", "succeeded", "failed", "cancelled"]
    progress: int
    payload: dict[str, object]
    result: dict[str, object] | None
    error_message: str | None
    retry_count: int
    max_retries: int
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class NotificationResponse(BaseModel):
    id: UUID
    task_id: UUID | None
    category: str
    level: Literal["info", "success", "warning", "error"]
    title: str
    message: str
    is_read: bool
    created_at: datetime


class WorkflowLaunchResponse(BaseModel):
    run: WorkflowRunResponse
    task: BackgroundTaskResponse


class TaskCenterResponse(BaseModel):
    tasks: list[BackgroundTaskResponse]
    notifications: list[NotificationResponse]
    unread_count: int


class AutomationSettingsResponse(BaseModel):
    daily_todo_briefing_enabled: bool
    daily_todo_briefing_hour: int
    last_daily_todo_briefing_on: str | None = None
    timezone: str
    scheduler_enabled: bool


class AutomationSettingsUpdateRequest(BaseModel):
    daily_todo_briefing_enabled: bool | None = None
    daily_todo_briefing_hour: int | None = Field(default=None, ge=0, le=23)


class AutomationRunResponse(BaseModel):
    task: BackgroundTaskResponse


def _confidence_rank(value: str | None) -> int:
    if value == "high":
        return 3
    if value == "medium":
        return 2
    return 1


def _build_evaluation_comparison(
    current_run: dict[str, object],
    baseline_run: dict[str, object],
) -> dict[str, object]:
    baseline_cases = {
        str(case["case_id"]): case for case in baseline_run.get("cases", [])
    }
    summary = {
        "new_failures": 0,
        "fixed": 0,
        "persistent_failures": 0,
        "persistent_passes": 0,
        "added": 0,
    }
    comparisons: list[dict[str, object]] = []
    for case in current_run.get("cases", []):
        baseline_case = baseline_cases.get(str(case["case_id"]))
        if baseline_case is None:
            label = "added"
            summary["added"] += 1
        else:
            current_status = str(case["status"])
            baseline_status = str(baseline_case["status"])
            if current_status == "failed" and baseline_status == "passed":
                label = "new_failure"
                summary["new_failures"] += 1
            elif current_status == "passed" and baseline_status == "failed":
                label = "fixed"
                summary["fixed"] += 1
            elif current_status == "failed" and baseline_status == "failed":
                label = "persistent_failure"
                summary["persistent_failures"] += 1
            else:
                label = "persistent_pass"
                summary["persistent_passes"] += 1
        comparisons.append(
            {
                "case_id": case["case_id"],
                "name": case["name"],
                "current_status": case["status"],
                "baseline_status": baseline_case["status"] if baseline_case else None,
                "current_score": case.get("score"),
                "baseline_score": baseline_case.get("score") if baseline_case else None,
                "regression_label": label,
                "failure_reason": case.get("failure_reason") or case.get("error_message"),
            }
        )
    return {
        "current_run_id": current_run["id"],
        "baseline_run_id": baseline_run["id"],
        "score_delta": round(
            float(current_run.get("score", 0)) - float(baseline_run.get("score", 0)),
            2,
        ),
        "confidence_delta": (
            _confidence_rank(str(current_run.get("confidence", "low")))
            - _confidence_rank(str(baseline_run.get("confidence", "low")))
        ),
        "summary": summary,
        "cases": comparisons,
    }


def _export_evaluation_run(run: dict[str, object], export_format: str) -> tuple[str, bytes]:
    safe_name = f"evaluation-{run['id']}"
    if export_format == "json":
        return (
            f"{safe_name}.json",
            json.dumps(run, ensure_ascii=False, indent=2).encode("utf-8"),
        )
    if export_format != "csv":
        raise ValueError("仅支持导出 json 或 csv。")
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "run_id",
            "prompt_version_name",
            "dataset_name",
            "model",
            "scorer",
            "judge_enabled",
            "confidence",
            "case_id",
            "case_name",
            "category",
            "status",
            "score",
            "failure_type",
            "failure_reason",
            "judge_score",
            "expected",
            "actual",
            "baseline_status",
            "regression_label",
        ]
    )
    for case in run.get("cases", []):
        writer.writerow(
            [
                run["id"],
                run.get("prompt_version_name"),
                run.get("dataset_name"),
                run.get("model"),
                run.get("scorer"),
                run.get("judge_enabled"),
                run.get("confidence"),
                case.get("case_id"),
                case.get("name"),
                case.get("category"),
                case.get("status"),
                case.get("score"),
                case.get("failure_type"),
                case.get("failure_reason"),
                case.get("judge_score"),
                case.get("expected"),
                case.get("actual"),
                case.get("baseline_status"),
                case.get("regression_label"),
            ]
        )
    return f"{safe_name}.csv", buffer.getvalue().encode("utf-8")


def _sse_event(event: dict[str, object]) -> str:
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


class ManagedSession:
    """每个会话独享一个 Agent 和一把锁，避免消息并发错序。"""

    def __init__(self, agent: AgentSession, user_id: str | None = None) -> None:
        self.agent = agent
        self.user_id = user_id
        self.lock = Lock()


SessionFactory = Callable[[str, str | None], AgentSession]
HistoryLoader = Callable[[str], list[dict[str, object]]]


def _default_session_factory(
    session_id: str,
    user_id: str | None = None,
) -> AgentSession:
    database_file = Path(
        os.getenv("TODO_DATABASE_FILE", str(DEFAULT_DATABASE_FILE))
    )
    legacy_json = DEFAULT_TODO_FILE.parent / "sessions" / session_id / "todos.json"
    mcp_client = None
    openapi_registry = None
    if user_id is not None:
        mcp_client = build_user_mcp_registry(database_file, user_id)
        openapi_registry = build_user_openapi_registry(database_file, user_id)
    return AgentSession(
        todo_store=SqliteTodoStore(
            database_file,
            session_id=user_id or session_id,
            legacy_json_path=legacy_json,
        ),
        travel_store=SqliteTravelStore(database_file, user_id or session_id),
        conversation_store=SqliteConversationStore(database_file, session_id),
        memory_store=(
            SqliteMemoryStore(database_file, user_id) if user_id else None
        ),
        tool_result_store=SqliteToolResultStore(
            database_file, user_id or session_id
        ),
        attachment_store=(
            SqliteChatAttachmentStore(database_file, user_id)
            if user_id
            else None
        ),
        mcp_client=mcp_client,
        openapi_registry=openapi_registry,
    )


def _default_history_loader(session_id: str) -> list[dict[str, object]]:
    database_file = Path(
        os.getenv("TODO_DATABASE_FILE", str(DEFAULT_DATABASE_FILE))
    )
    return SqliteConversationStore(database_file, session_id).list_history(200)


class SessionManager:
    """缓存活动会话；Agent 重建时会从 SQLite 恢复对话。"""

    def __init__(
        self,
        session_factory: SessionFactory | None = None,
        history_loader: HistoryLoader | None = None,
    ) -> None:
        self._session_factory = session_factory or _default_session_factory
        self._history_loader = history_loader or (
            _default_history_loader if session_factory is None else lambda _session_id: []
        )
        self._sessions: dict[UUID, ManagedSession] = {}
        self._lock = Lock()

    def get_or_create(
        self,
        session_id: UUID | None,
        user_id: str | None = None,
    ) -> tuple[UUID, ManagedSession]:
        resolved_id = session_id or uuid4()
        with self._lock:
            managed = self._sessions.get(resolved_id)
            if managed is None:
                managed = ManagedSession(
                    self._session_factory(str(resolved_id), user_id),
                    user_id=user_id,
                )
                self._sessions[resolved_id] = managed
        return resolved_id, managed

    def refresh_mcp_for_user(self, user_id: str, database_file: Path) -> None:
        registry = build_user_mcp_registry(database_file, user_id)
        with self._lock:
            for managed in self._sessions.values():
                if managed.user_id == user_id:
                    managed.agent.mcp_client = registry

    def refresh_openapi_for_user(self, user_id: str, database_file: Path) -> None:
        registry = build_user_openapi_registry(database_file, user_id)
        with self._lock:
            for managed in self._sessions.values():
                if managed.user_id == user_id:
                    managed.agent.openapi_registry = registry

    def load_history(self, session_id: UUID) -> list[dict[str, object]]:
        return self._history_loader(str(session_id))


AUTH_COOKIE_NAME = "hello_agent_login"
ADMIN_COOKIE_NAME = "hello_agent_admin_login"


def _can_access_admin(user: AuthUser) -> bool:
    """仅用于控制 Agent 前端入口显示；后台仍使用独立 MFA 认证。"""
    return user.is_super_admin


def _user_response(user: AuthUser) -> UserResponse:
    return UserResponse(**user.__dict__, can_access_admin=_can_access_admin(user))


def create_api(
    session_manager: SessionManager | None = None,
    auth_service: SqliteAuthService | None = None,
    auth_required: bool = True,
) -> FastAPI:
    manager = session_manager or SessionManager()
    database_file = (
        auth_service.database_path
        if auth_service is not None
        else Path(os.getenv("TODO_DATABASE_FILE", str(DEFAULT_DATABASE_FILE)))
    )
    auth = auth_service or SqliteAuthService(database_file)
    admin_service = SqliteAdminService(database_file)
    admin_auth = SqliteAdminAuthService(database_file)
    api = FastAPI(
        title="Hello Agent API",
        description="DeepSeek Agent 学习项目的 HTTP API",
        version="0.1.0",
    )
    api.add_middleware(
        CORSMiddleware,
        allow_origins=[
            origin.strip()
            for origin in os.getenv(
                "FRONTEND_ORIGINS",
                (
                    "http://localhost:5173,http://127.0.0.1:5173,"
                    "http://localhost:5174,http://127.0.0.1:5174"
                ),
            ).split(",")
            if origin.strip()
        ],
        allow_origin_regex=os.getenv(
            "FRONTEND_ORIGIN_REGEX",
            (
                r"^http://(localhost|127\.0\.0\.1|10(?:\.\d{1,3}){3}|"
                r"192\.168(?:\.\d{1,3}){2}|172\.(?:1[6-9]|2\d|3[01])"
                r"(?:\.\d{1,3}){2}):(?:5173|5174)$"
            ),
        ),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type"],
    )

    def current_user(request: Request) -> AuthUser | None:
        if not auth_required:
            return None
        user = auth.authenticate(request.cookies.get(AUTH_COOKIE_NAME))
        if user is None:
            raise HTTPException(status_code=401, detail="请先登录。")
        return user

    def current_admin_session(request: Request) -> AdminPrincipal:
        admin = admin_auth.authenticate(request.cookies.get(ADMIN_COOKIE_NAME))
        if admin is None:
            raise HTTPException(status_code=401, detail="请完成后台双重认证登录。")
        return admin

    def current_admin(
        admin: AdminPrincipal = Depends(current_admin_session),
    ) -> AdminPrincipal:
        return admin

    def current_admin_knowledge_manager(
        admin: AdminPrincipal = Depends(current_admin_session),
    ) -> AdminPrincipal:
        if not admin.can_manage_knowledge:
            raise HTTPException(
                status_code=403,
                detail="当前后台账号只有查看权限，不能上传、删除或导入知识库资料。",
            )
        return admin

    def current_knowledge_editor(
        user: AuthUser | None = Depends(current_user),
    ) -> AuthUser:
        if user is None:
            raise HTTPException(status_code=401, detail="请先登录。")
        if user.role != "knowledge_manager":
            raise HTTPException(
                status_code=403,
                detail="仅系统管理员或知识库管理员可以管理知识文档。",
            )
        return user

    def set_login_cookie(response: Response, token: str) -> None:
        response.set_cookie(
            key=AUTH_COOKIE_NAME,
            value=token,
            max_age=LOGIN_SESSION_DAYS * 24 * 60 * 60,
            httponly=True,
            secure=os.getenv("AUTH_COOKIE_SECURE", "false").lower() == "true",
            samesite="lax",
            path="/",
        )

    def set_admin_cookie(response: Response, token: str) -> None:
        response.set_cookie(
            key=ADMIN_COOKIE_NAME,
            value=token,
            max_age=ADMIN_SESSION_HOURS * 60 * 60,
            httponly=True,
            secure=os.getenv(
                "ADMIN_COOKIE_SECURE",
                os.getenv("AUTH_COOKIE_SECURE", "false"),
            ).lower() == "true",
            samesite="strict",
            path="/",
        )

    def get_agent_session(
        requested_id: UUID | None,
        user: AuthUser | None,
    ) -> tuple[UUID, ManagedSession]:
        if user is None:
            return manager.get_or_create(requested_id, None)
        if requested_id is None:
            resolved_id = uuid4()
            auth.claim_new_agent_session(user.id, str(resolved_id))
            return manager.get_or_create(resolved_id, user.id)
        if not auth.owns_agent_session(user.id, str(requested_id)):
            raise HTTPException(status_code=403, detail="你无权访问这个 Agent 会话。")
        return manager.get_or_create(requested_id, user.id)

    def user_todo_store(user: AuthUser) -> SqliteTodoStore:
        return SqliteTodoStore(database_file, session_id=user.id)

    def user_travel_store(user: AuthUser | None) -> SqliteTravelStore:
        return SqliteTravelStore(
            database_file,
            user_id=user.id if user is not None else "anonymous",
        )

    def observability_store(
        user: AuthUser | None,
    ) -> SqliteObservabilityStore:
        return SqliteObservabilityStore(
            database_file,
            user_id=user.id if user is not None else "anonymous",
        )

    def evaluation_store(user: AuthUser) -> SqliteEvaluationStore:
        return SqliteEvaluationStore(database_file, user.id)

    def agentops_store(user: AuthUser) -> SqliteAgentOpsStore:
        return SqliteAgentOpsStore(database_file, user.id)

    def knowledge_document_store(user: AuthUser) -> SqliteKnowledgeDocumentStore:
        return SqliteKnowledgeDocumentStore(database_file, user.id)

    def user_memory_store(user: AuthUser) -> SqliteMemoryStore:
        return SqliteMemoryStore(database_file, user.id)

    def user_attachment_store(user: AuthUser) -> SqliteChatAttachmentStore:
        return SqliteChatAttachmentStore(database_file, user.id)

    def user_collaboration_store(user: AuthUser) -> SqliteCollaborationStore:
        return SqliteCollaborationStore(database_file, user.id)

    def task_store(user: AuthUser) -> SqliteTaskStore:
        return SqliteTaskStore(database_file, user.id)

    def automation_store() -> SqliteAutomationStore:
        return SqliteAutomationStore(database_file)

    def workflow_store(user: AuthUser) -> SqliteWorkflowStore:
        return SqliteWorkflowStore(database_file, user.id)

    def workflow_mcp_client(user: AuthUser) -> MCPClientRegistry:
        return build_user_mcp_registry(database_file, user.id)

    def user_mcp_store(user: AuthUser) -> SqliteMcpServerStore:
        return SqliteMcpServerStore(database_file, user.id)

    def refresh_user_mcp(user: AuthUser) -> None:
        manager.refresh_mcp_for_user(user.id, database_file)

    def user_openapi_store(user: AuthUser) -> SqliteOpenApiSourceStore:
        return SqliteOpenApiSourceStore(database_file, user.id)

    def refresh_user_openapi(user: AuthUser) -> None:
        manager.refresh_openapi_for_user(user.id, database_file)

    def public_knowledge_store() -> SqliteKnowledgeDocumentStore:
        return SqliteKnowledgeDocumentStore(
            database_file,
            PUBLIC_KNOWLEDGE_OWNER,
        )

    def estimated_model_cost(
        usage: dict[str, int], model: str | None = None
    ) -> float:
        from hello_agent.usage import estimate_cost

        return estimate_cost(usage, model)

    def finish_observed_run(
        store: SqliteObservabilityStore,
        run_id: str,
        agent: AgentSession,
        status: Literal["success", "failed", "waiting_approval", "cancelled"],
        error_message: str | None = None,
    ) -> None:
        for step in agent.trace_steps:
            store.add_step(
                run_id,
                str(step["step_type"]),
                str(step["name"]),
                str(step["status"]),
                int(step["duration_ms"]),
                str(step["detail"]),
            )
        if hasattr(agent, "estimated_cost_total"):
            cost = agent.estimated_cost_total()
        else:
            cost = estimated_model_cost(
                getattr(agent, "model_usage", {}),
                getattr(agent, "model", None),
            )
        if hasattr(agent, "models_label"):
            used_model = agent.models_label()
        else:
            used_model = getattr(agent, "model", None)
        store.finish_run(
            run_id,
            status,
            usage=getattr(agent, "model_usage", {}),
            estimated_cost=cost,
            error_message=error_message,
            model=used_model,
        )

    def maybe_capture_grounding_failure(
        user: AuthUser | None,
        question: str,
        answer: str,
        agent: AgentSession,
    ) -> bool:
        if user is None or not getattr(agent, "last_capture_grounding_failure", False):
            return False
        try:
            captured = agentops_store(user).capture_grounding_failure(
                question=question,
                answer=answer,
                confidence=str(getattr(agent, "last_confidence", "low")),
                grounding=str(getattr(agent, "last_grounding", "refused")),
            )
        except Exception:
            return False
        return captured is not None

    @api.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        from hello_agent.ocr import ocr_status

        status = ocr_status()
        return HealthResponse(
            status="ok",
            ocr_enabled=bool(status.get("enabled")),
            ocr_available=bool(status.get("available")),
            ocr_languages=str(status.get("languages") or ""),
        )

    @api.post("/auth/register", response_model=UserResponse, status_code=201)
    def register(request: RegisterRequest, response: Response) -> UserResponse:
        try:
            user = auth.register(
                request.email,
                request.password,
                request.display_name,
            )
            token = auth.create_login_session(user.id)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        set_login_cookie(response, token)
        return _user_response(user)

    @api.post("/auth/login", response_model=UserResponse)
    def login(request: AuthRequest, response: Response) -> UserResponse:
        try:
            user, token = auth.login(request.email, request.password)
        except ValueError as error:
            raise HTTPException(status_code=401, detail=str(error)) from error
        set_login_cookie(response, token)
        return _user_response(user)

    @api.post("/auth/logout", status_code=204)
    def logout(request: Request, response: Response) -> Response:
        auth.logout(request.cookies.get(AUTH_COOKIE_NAME))
        response.delete_cookie(AUTH_COOKIE_NAME, path="/")
        response.status_code = 204
        return response

    @api.get("/auth/me", response_model=UserResponse)
    def me(user: AuthUser | None = Depends(current_user)) -> UserResponse:
        if user is None:
            raise HTTPException(status_code=401, detail="请先登录。")
        return _user_response(user)

    @api.post(
        "/admin-auth/login",
        response_model=AdminLoginChallengeResponse,
    )
    def admin_password_login(
        credentials: AdminPasswordLoginRequest,
    ) -> AdminLoginChallengeResponse:
        try:
            result = admin_auth.password_login(
                credentials.email, credentials.password
            )
        except ValueError as error:
            raise HTTPException(status_code=401, detail=str(error)) from error
        return AdminLoginChallengeResponse(**result)

    @api.post(
        "/admin-auth/mfa/setup",
        response_model=AdminMFASetupResponse,
    )
    def admin_mfa_setup(
        setup: AdminMFASetupRequest,
    ) -> AdminMFASetupResponse:
        try:
            result = admin_auth.start_mfa_setup(setup.challenge_token)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return AdminMFASetupResponse(**result)

    @api.post(
        "/admin-auth/mfa/activate",
        response_model=AdminMFAActivateResponse,
    )
    def admin_mfa_activate(
        activation: AdminMFAActivateRequest,
        response: Response,
    ) -> AdminMFAActivateResponse:
        try:
            admin, token, recovery_codes = admin_auth.activate_mfa(
                activation.challenge_token,
                activation.code,
                activation.new_password,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        set_admin_cookie(response, token)
        return AdminMFAActivateResponse(
            user=AdminPrincipalResponse(**admin.__dict__),
            recovery_codes=recovery_codes,
        )

    @api.post(
        "/admin-auth/mfa/verify",
        response_model=AdminPrincipalResponse,
    )
    def admin_mfa_verify(
        verification: AdminMFAVerifyRequest,
        response: Response,
    ) -> AdminPrincipalResponse:
        try:
            admin, token = admin_auth.verify_mfa(
                verification.challenge_token, verification.code
            )
        except ValueError as error:
            raise HTTPException(status_code=401, detail=str(error)) from error
        set_admin_cookie(response, token)
        return AdminPrincipalResponse(**admin.__dict__)

    @api.get("/admin-auth/me", response_model=AdminPrincipalResponse)
    def admin_me(
        admin: AdminPrincipal = Depends(current_admin_session),
    ) -> AdminPrincipalResponse:
        return AdminPrincipalResponse(**admin.__dict__)

    @api.post("/admin-auth/logout", status_code=204)
    def admin_logout(request: Request, response: Response) -> Response:
        admin_auth.logout(request.cookies.get(ADMIN_COOKIE_NAME))
        response.delete_cookie(ADMIN_COOKIE_NAME, path="/")
        response.status_code = 204
        return response

    @api.get("/admin-auth/security", response_model=AdminSecurityResponse)
    def admin_security(
        admin: AdminPrincipal = Depends(current_admin_session),
    ) -> AdminSecurityResponse:
        return AdminSecurityResponse(**admin_auth.security_status(admin.id))

    @api.delete(
        "/admin-auth/sessions/others",
        response_model=AdminSessionRevokeResponse,
    )
    def revoke_other_admin_sessions(
        request: Request,
        admin: AdminPrincipal = Depends(current_admin),
    ) -> AdminSessionRevokeResponse:
        token = request.cookies.get(ADMIN_COOKIE_NAME, "")
        return AdminSessionRevokeResponse(
            revoked_count=admin_auth.revoke_other_sessions(admin.id, token)
        )

    @api.post(
        "/admin-auth/email/login/send",
        response_model=AdminEmailSendResponse,
    )
    def send_admin_login_email_code(
        request: AdminEmailSendRequest,
    ) -> AdminEmailSendResponse:
        try:
            masked = admin_auth.send_login_email_code(request.challenge_token)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except (RuntimeError, OSError) as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        return AdminEmailSendResponse(masked_email=masked)

    @api.post(
        "/admin-auth/email/login/verify",
        response_model=AdminPrincipalResponse,
    )
    def verify_admin_login_email_code(
        verification: AdminEmailCodeRequest,
        response: Response,
    ) -> AdminPrincipalResponse:
        try:
            admin, token = admin_auth.verify_login_email(
                verification.challenge_token, verification.code
            )
        except ValueError as error:
            raise HTTPException(status_code=401, detail=str(error)) from error
        set_admin_cookie(response, token)
        return AdminPrincipalResponse(**admin.__dict__)

    @api.post(
        "/admin-auth/security/email/send",
        response_model=AdminEmailSendResponse,
    )
    def send_admin_email_enable_code(
        request: AdminEmailConfigureRequest | None = None,
        admin: AdminPrincipal = Depends(current_admin),
    ) -> AdminEmailSendResponse:
        try:
            masked = admin_auth.send_email_enable_code_to(
                admin.id, request.email if request else None
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except (RuntimeError, OSError) as error:
            raise HTTPException(status_code=503, detail=str(error)) from error
        return AdminEmailSendResponse(masked_email=masked)

    @api.post("/admin-auth/security/email/enable", status_code=204)
    def enable_admin_email_recovery(
        verification: AdminEmailEnableRequest,
        response: Response,
        admin: AdminPrincipal = Depends(current_admin),
    ) -> Response:
        try:
            admin_auth.enable_email_recovery(admin.id, verification.code)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        response.status_code = 204
        return response

    @api.delete("/admin-auth/security/email", status_code=204)
    def disable_admin_email_recovery(
        response: Response,
        admin: AdminPrincipal = Depends(current_admin),
    ) -> Response:
        admin_auth.disable_email_recovery(admin.id)
        response.status_code = 204
        return response

    @api.post(
        "/admin-auth/mfa/rebind/setup",
        response_model=AdminMFASetupResponse,
    )
    def admin_mfa_rebind_setup(
        admin: AdminPrincipal = Depends(current_admin_session),
    ) -> AdminMFASetupResponse:
        try:
            return AdminMFASetupResponse(**admin_auth.start_mfa_rebind(admin.id))
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @api.post(
        "/admin-auth/mfa/rebind/complete",
        response_model=AdminMFARebindResponse,
    )
    def admin_mfa_rebind_complete(
        verification: AdminEmailEnableRequest,
        request: Request,
        admin: AdminPrincipal = Depends(current_admin_session),
    ) -> AdminMFARebindResponse:
        try:
            updated, recovery_codes = admin_auth.complete_mfa_rebind(
                admin.id,
                verification.code,
                request.cookies.get(ADMIN_COOKIE_NAME, ""),
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return AdminMFARebindResponse(
            user=AdminPrincipalResponse(**updated.__dict__),
            recovery_codes=recovery_codes,
        )

    @api.get("/admin/overview", response_model=AdminOverviewResponse)
    def admin_overview(
        _admin: AdminPrincipal = Depends(current_admin),
    ) -> AdminOverviewResponse:
        return AdminOverviewResponse(**admin_service.overview())

    @api.get("/admin/metrics")
    def admin_metrics(
        period: Literal["day", "week", "month"] = Query(default="day"),
        model: str | None = None,
        _admin: AdminPrincipal = Depends(current_admin),
    ) -> dict[str, object]:
        return collect_platform_cost(database_file, period=period, model=model)

    @api.get("/admin/releases")
    def admin_releases(
        limit: int = Query(default=20, ge=1, le=100),
        _admin: AdminPrincipal = Depends(current_admin),
    ) -> dict[str, object]:
        return {"releases": list_release_records(limit=limit)}

    @api.get("/admin/users", response_model=AdminUserListResponse)
    def admin_users(
        _admin: AdminPrincipal = Depends(current_admin),
    ) -> AdminUserListResponse:
        return AdminUserListResponse(
            users=[AdminUserResponse(**user) for user in admin_service.list_users()]
        )

    @api.get("/admin/admin-users", response_model=ManagedAdminUserListResponse)
    def admin_admin_users(
        _admin: AdminPrincipal = Depends(current_admin),
    ) -> ManagedAdminUserListResponse:
        return ManagedAdminUserListResponse(
            admins=[
                ManagedAdminUserResponse(**admin)
                for admin in admin_service.list_admin_users()
            ]
        )

    @api.patch(
        "/admin/users/{user_id}",
        response_model=AdminUserResponse,
    )
    def update_admin_user(
        user_id: UUID,
        update: AdminUserUpdateRequest,
        request: Request,
        admin: AdminPrincipal = Depends(current_admin),
    ) -> AdminUserResponse:
        forwarded = request.headers.get("x-forwarded-for", "").split(",", 1)[0]
        ip_address = forwarded.strip() or (
            request.client.host if request.client is not None else None
        )
        try:
            user = admin_service.update_user(
                admin.id,
                str(user_id),
                update.role,
                update.is_active,
                ip_address,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return AdminUserResponse(**user)

    @api.patch(
        "/admin/admin-users/{admin_id}",
        response_model=ManagedAdminUserResponse,
    )
    def update_managed_admin_user(
        admin_id: UUID,
        update: ManagedAdminUserUpdateRequest,
        request: Request,
        admin: AdminPrincipal = Depends(current_admin),
    ) -> ManagedAdminUserResponse:
        forwarded = request.headers.get("x-forwarded-for", "").split(",", 1)[0]
        ip_address = forwarded.strip() or (
            request.client.host if request.client is not None else None
        )
        try:
            managed_admin = admin_service.update_admin_user(
                admin.id,
                str(admin_id),
                can_manage_knowledge=update.can_manage_knowledge,
                is_active=update.is_active,
                ip_address=ip_address,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return ManagedAdminUserResponse(**managed_admin)

    @api.get("/admin/audit-logs", response_model=AdminAuditListResponse)
    def admin_audit_logs(
        _admin: AdminPrincipal = Depends(current_admin),
    ) -> AdminAuditListResponse:
        return AdminAuditListResponse(
            logs=[
                AdminAuditResponse(**log)
                for log in admin_service.list_audit_logs()
            ]
        )

    @api.get(
        "/admin/knowledge/documents",
        response_model=KnowledgeDocumentListResponse,
    )
    def admin_list_knowledge_documents(
        _admin: AdminPrincipal = Depends(current_admin),
    ) -> KnowledgeDocumentListResponse:
        return KnowledgeDocumentListResponse(
            documents=[
                KnowledgeDocumentResponse(**document)
                for document in public_knowledge_store().list()
            ]
        )

    @api.get(
        "/admin/knowledge/documents/{document_id}/preview",
        response_model=KnowledgeDocumentPreviewResponse,
    )
    def admin_preview_knowledge_document(
        document_id: UUID,
        _admin: AdminPrincipal = Depends(current_admin),
    ) -> KnowledgeDocumentPreviewResponse:
        store = public_knowledge_store()
        try:
            document = store.get(str(document_id))
            content = read_knowledge_document_text(
                store,
                PUBLIC_KNOWLEDGE_OWNER,
                str(document_id),
            )
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        truncated = len(content) > 12000
        return KnowledgeDocumentPreviewResponse(
            document=KnowledgeDocumentResponse(**document),
            content=content[:12000],
            truncated=truncated,
        )

    @api.get(
        "/admin/knowledge/search",
        response_model=KnowledgeSearchResponse,
    )
    def admin_search_knowledge(
        query: str = Query(min_length=1, max_length=300),
        limit: int = Query(default=5, ge=1, le=10),
        _admin: AdminPrincipal = Depends(current_admin),
    ) -> KnowledgeSearchResponse:
        public_dir = public_knowledge_dir()
        result = hybrid_search(
            database_file,
            query=query,
            owner_ids=[PUBLIC_KNOWLEDGE_OWNER],
            limit=limit,
            roots=[public_dir],
            public_root=public_dir,
        )
        return KnowledgeSearchResponse(**result)

    @api.post(
        "/admin/knowledge/documents",
        response_model=KnowledgeDocumentResponse,
        status_code=201,
    )
    async def admin_upload_knowledge_document(
        file: UploadFile = File(...),
        category: str = Form(default="未分类", max_length=50),
        _admin: AdminPrincipal = Depends(current_admin_knowledge_manager),
    ) -> KnowledgeDocumentResponse:
        content = await file.read(MAX_UPLOAD_BYTES + 1)
        try:
            document = save_knowledge_document(
                public_knowledge_store(),
                PUBLIC_KNOWLEDGE_OWNER,
                file.filename or "",
                content,
                category,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        schedule_knowledge_index(
            database_file,
            PUBLIC_KNOWLEDGE_OWNER,
            str(document["id"]),
            str(document["original_name"]),
        )
        return KnowledgeDocumentResponse(**document)

    @api.post(
        "/admin/knowledge/documents/{document_id}/reindex",
        response_model=KnowledgeDocumentResponse,
    )
    def admin_reindex_knowledge_document(
        document_id: UUID,
        _admin: AdminPrincipal = Depends(current_admin_knowledge_manager),
    ) -> KnowledgeDocumentResponse:
        store = public_knowledge_store()
        try:
            document = store.update_index_status(
                str(document_id), status="processing", progress=0
            )
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        schedule_knowledge_index(
            database_file,
            PUBLIC_KNOWLEDGE_OWNER,
            str(document_id),
            str(document["original_name"]),
        )
        return KnowledgeDocumentResponse(**document)

    @api.delete("/admin/knowledge/documents/{document_id}", status_code=204)
    def admin_remove_knowledge_document(
        document_id: UUID,
        response: Response,
        _admin: AdminPrincipal = Depends(current_admin_knowledge_manager),
    ) -> Response:
        try:
            delete_knowledge_document(
                public_knowledge_store(),
                PUBLIC_KNOWLEDGE_OWNER,
                str(document_id),
            )
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        response.status_code = 204
        return response

    @api.post(
        "/admin/knowledge/demo",
        response_model=DemoKnowledgeResponse,
        status_code=201,
    )
    def admin_add_demo_knowledge(
        _admin: AdminPrincipal = Depends(current_admin_knowledge_manager),
    ) -> DemoKnowledgeResponse:
        store = public_knowledge_store()
        created = seed_demo_knowledge(store, PUBLIC_KNOWLEDGE_OWNER)
        for document in created:
            schedule_knowledge_index(
                database_file,
                PUBLIC_KNOWLEDGE_OWNER,
                str(document["id"]),
                str(document["original_name"]),
            )
        return DemoKnowledgeResponse(
            created_count=len(created),
            documents=[KnowledgeDocumentResponse(**item) for item in store.list()],
        )

    @api.get("/todos", response_model=list[TodoResponse])
    def list_user_todos(
        user: AuthUser | None = Depends(current_user),
    ) -> list[TodoResponse]:
        if user is None:
            raise HTTPException(status_code=401, detail="请先登录。")
        return [TodoResponse(**todo) for todo in user_todo_store(user).list_all()]

    @api.get("/todo-plans", response_model=TodoPlanListResponse)
    def list_user_todo_plans(
        user: AuthUser | None = Depends(current_user),
    ) -> TodoPlanListResponse:
        if user is None:
            raise HTTPException(status_code=401, detail="请先登录。")
        return TodoPlanListResponse(**user_todo_store(user).list_plans())

    @api.post("/todo-plans", response_model=TodoPlanResponse, status_code=201)
    def create_user_todo_plan(
        request: TodoPlanCreateRequest,
        user: AuthUser | None = Depends(current_user),
    ) -> TodoPlanResponse:
        if user is None:
            raise HTTPException(status_code=401, detail="请先登录。")
        try:
            plan = user_todo_store(user).create_plan(
                request.title,
                request.summary,
                request.priority,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return TodoPlanResponse(**plan)

    @api.post("/todos", response_model=TodoResponse, status_code=201)
    def create_user_todo(
        request: TodoCreateRequest,
        user: AuthUser | None = Depends(current_user),
    ) -> TodoResponse:
        if user is None:
            raise HTTPException(status_code=401, detail="请先登录。")
        try:
            todo = user_todo_store(user).add(request.title, request.plan_id)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return TodoResponse(**todo)

    @api.patch("/todos/{todo_id}", response_model=TodoResponse)
    def update_user_todo(
        todo_id: int,
        request: TodoUpdateRequest,
        user: AuthUser | None = Depends(current_user),
    ) -> TodoResponse:
        if user is None:
            raise HTTPException(status_code=401, detail="请先登录。")
        try:
            todo = user_todo_store(user).set_completed(
                todo_id, request.completed
            )
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return TodoResponse(**todo)

    @api.delete("/todos/{todo_id}", status_code=204)
    def delete_user_todo(
        todo_id: int,
        response: Response,
        user: AuthUser | None = Depends(current_user),
    ) -> Response:
        if user is None:
            raise HTTPException(status_code=401, detail="请先登录。")
        try:
            user_todo_store(user).delete(todo_id)
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        response.status_code = 204
        return response

    @api.get(
        "/knowledge/documents",
        response_model=KnowledgeDocumentListResponse,
    )
    def list_knowledge_documents(
        user: AuthUser | None = Depends(current_user),
    ) -> KnowledgeDocumentListResponse:
        if user is None:
            raise HTTPException(status_code=401, detail="请先登录。")
        return KnowledgeDocumentListResponse(
            documents=[
                KnowledgeDocumentResponse(**document)
                for document in knowledge_document_store(user).list()
            ]
        )

    @api.post(
        "/knowledge/documents",
        response_model=KnowledgeDocumentResponse,
        status_code=201,
    )
    async def upload_knowledge_document(
        file: UploadFile = File(...),
        category: str = Form(default="未分类", max_length=50),
        user: AuthUser = Depends(current_knowledge_editor),
    ) -> KnowledgeDocumentResponse:
        content = await file.read(MAX_UPLOAD_BYTES + 1)
        try:
            document = save_knowledge_document(
                knowledge_document_store(user),
                user.id,
                file.filename or "",
                content,
                category,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        schedule_knowledge_index(
            database_file,
            user.id,
            str(document["id"]),
            str(document["original_name"]),
        )
        return KnowledgeDocumentResponse(**document)

    @api.get("/knowledge/search", response_model=KnowledgeSearchResponse)
    def search_user_knowledge(
        query: str = Query(min_length=1, max_length=300),
        limit: int = Query(default=5, ge=1, le=10),
        user: AuthUser | None = Depends(current_user),
    ) -> KnowledgeSearchResponse:
        if user is None:
            raise HTTPException(status_code=401, detail="请先登录。")
        public_dir = public_knowledge_dir()
        user_dir = user_knowledge_dir(user.id)
        result = hybrid_search(
            database_file,
            query,
            [PUBLIC_KNOWLEDGE_OWNER, user.id],
            limit,
            roots=[public_dir, user_dir],
            public_root=public_dir,
        )
        return KnowledgeSearchResponse(**result)

    @api.post(
        "/knowledge/documents/{document_id}/reindex",
        response_model=KnowledgeDocumentResponse,
    )
    def reindex_knowledge_document(
        document_id: UUID,
        user: AuthUser = Depends(current_knowledge_editor),
    ) -> KnowledgeDocumentResponse:
        store = knowledge_document_store(user)
        try:
            document = store.update_index_status(
                str(document_id), status="processing", progress=0
            )
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        schedule_knowledge_index(
            database_file,
            user.id,
            str(document_id),
            str(document["original_name"]),
        )
        return KnowledgeDocumentResponse(**document)

    @api.delete("/knowledge/documents/{document_id}", status_code=204)
    def remove_knowledge_document(
        document_id: UUID,
        response: Response,
        user: AuthUser = Depends(current_knowledge_editor),
    ) -> Response:
        try:
            delete_knowledge_document(
                knowledge_document_store(user),
                user.id,
                str(document_id),
            )
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        response.status_code = 204
        return response

    @api.get("/chat/attachments", response_model=ChatAttachmentListResponse)
    def list_chat_attachments(
        session_id: UUID = Query(...),
        user: AuthUser | None = Depends(current_user),
    ) -> ChatAttachmentListResponse:
        if user is None:
            raise HTTPException(status_code=401, detail="请先登录。")
        resolved_id, _managed = get_agent_session(session_id, user)
        items = user_attachment_store(user).list(str(resolved_id))
        return ChatAttachmentListResponse(
            session_id=resolved_id,
            attachments=[
                ChatAttachmentResponse(
                    id=UUID(str(item["id"])),
                    session_id=UUID(str(item["session_id"])),
                    original_name=str(item["original_name"]),
                    file_type=str(item["file_type"]),
                    size_bytes=int(item["size_bytes"]),
                    char_count=int(item["char_count"]),
                    preview=str(item.get("preview") or ""),
                    extraction_method=str(item.get("extraction_method") or ""),
                    created_at=datetime.fromisoformat(str(item["created_at"])),
                )
                for item in items
            ],
        )

    @api.post(
        "/chat/attachments",
        response_model=ChatAttachmentResponse,
        status_code=201,
    )
    async def upload_chat_attachment(
        file: UploadFile = File(...),
        session_id: UUID | None = Form(default=None),
        user: AuthUser | None = Depends(current_user),
    ) -> ChatAttachmentResponse:
        if user is None:
            raise HTTPException(status_code=401, detail="请先登录。")
        resolved_id, _managed = get_agent_session(session_id, user)
        content = await file.read(MAX_UPLOAD_BYTES + 1)
        try:
            item = user_attachment_store(user).create(
                session_id=str(resolved_id),
                original_name=file.filename or "",
                content=content,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return ChatAttachmentResponse(
            id=UUID(str(item["id"])),
            session_id=UUID(str(item["session_id"])),
            original_name=str(item["original_name"]),
            file_type=str(item["file_type"]),
            size_bytes=int(item["size_bytes"]),
            char_count=int(item["char_count"]),
            preview=str(item.get("preview") or ""),
            extraction_method=str(item.get("extraction_method") or ""),
            created_at=datetime.fromisoformat(str(item["created_at"])),
        )

    @api.delete("/chat/attachments/{attachment_id}", status_code=204)
    def delete_chat_attachment(
        attachment_id: UUID,
        response: Response,
        user: AuthUser | None = Depends(current_user),
    ) -> Response:
        if user is None:
            raise HTTPException(status_code=401, detail="请先登录。")
        try:
            user_attachment_store(user).delete(str(attachment_id))
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        response.status_code = 204
        return response

    @api.get("/memories", response_model=MemoryListResponse)
    def list_user_memories(
        category: MemoryCategory | None = Query(default=None),
        user: AuthUser | None = Depends(current_user),
    ) -> MemoryListResponse:
        if user is None:
            raise HTTPException(status_code=401, detail="请先登录。")
        memories = user_memory_store(user).list(category)
        return MemoryListResponse(
            memories=[MemoryItemResponse(**item) for item in memories],
            total=len(memories),
        )

    @api.post("/memories", response_model=MemoryItemResponse, status_code=201)
    def create_user_memory(
        request: CreateMemoryRequest,
        user: AuthUser | None = Depends(current_user),
    ) -> MemoryItemResponse:
        if user is None:
            raise HTTPException(status_code=401, detail="请先登录。")
        try:
            memory = user_memory_store(user).upsert(request.category, request.content)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return MemoryItemResponse(**memory)

    @api.delete("/memories/{memory_id}", status_code=204)
    def delete_user_memory(
        memory_id: str,
        response: Response,
        user: AuthUser | None = Depends(current_user),
    ) -> Response:
        if user is None:
            raise HTTPException(status_code=401, detail="请先登录。")
        try:
            user_memory_store(user).delete(memory_id)
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        response.status_code = 204
        return response

    @api.delete("/memories", status_code=204)
    def clear_user_memories(
        response: Response,
        user: AuthUser | None = Depends(current_user),
    ) -> Response:
        if user is None:
            raise HTTPException(status_code=401, detail="请先登录。")
        user_memory_store(user).clear()
        response.status_code = 204
        return response

    @api.post(
        "/knowledge/demo",
        response_model=DemoKnowledgeResponse,
        status_code=201,
    )
    def add_demo_knowledge(
        user: AuthUser = Depends(current_knowledge_editor),
    ) -> DemoKnowledgeResponse:
        store = knowledge_document_store(user)
        created = seed_demo_knowledge(store, user.id)
        for document in created:
            schedule_knowledge_index(
                database_file,
                user.id,
                str(document["id"]),
                str(document["original_name"]),
            )
        return DemoKnowledgeResponse(
            created_count=len(created),
            documents=[KnowledgeDocumentResponse(**item) for item in store.list()],
        )

    @api.get("/automations/settings", response_model=AutomationSettingsResponse)
    def get_automation_settings(
        user: AuthUser | None = Depends(current_user),
    ) -> AutomationSettingsResponse:
        if user is None:
            raise HTTPException(status_code=401, detail="请先登录。")
        settings = automation_store().get_settings(user.id)
        return AutomationSettingsResponse(
            **settings,
            timezone=str(briefing_timezone()),
            scheduler_enabled=scheduler_enabled(),
        )

    @api.patch("/automations/settings", response_model=AutomationSettingsResponse)
    def update_automation_settings(
        request: AutomationSettingsUpdateRequest,
        user: AuthUser | None = Depends(current_user),
    ) -> AutomationSettingsResponse:
        if user is None:
            raise HTTPException(status_code=401, detail="请先登录。")
        try:
            settings = automation_store().update_settings(
                user.id,
                daily_todo_briefing_enabled=request.daily_todo_briefing_enabled,
                daily_todo_briefing_hour=request.daily_todo_briefing_hour,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return AutomationSettingsResponse(
            **settings,
            timezone=str(briefing_timezone()),
            scheduler_enabled=scheduler_enabled(),
        )

    @api.post(
        "/automations/daily-todo-briefing/run",
        response_model=AutomationRunResponse,
    )
    def run_daily_todo_briefing_now(
        user: AuthUser | None = Depends(current_user),
    ) -> AutomationRunResponse:
        if user is None:
            raise HTTPException(status_code=401, detail="请先登录。")
        task = schedule_daily_todo_briefing(
            database_file,
            user.id,
            trigger="manual",
        )
        return AutomationRunResponse(task=BackgroundTaskResponse(**task))

    @api.get("/tasks", response_model=TaskCenterResponse)
    def list_background_tasks(
        user: AuthUser | None = Depends(current_user),
    ) -> TaskCenterResponse:
        if user is None:
            raise HTTPException(status_code=401, detail="请先登录。")
        store = task_store(user)
        return TaskCenterResponse(
            tasks=[BackgroundTaskResponse(**item) for item in store.list()],
            notifications=[
                NotificationResponse(**item) for item in store.list_notifications()
            ],
            unread_count=store.unread_count(),
        )

    @api.post("/tasks/{task_id}/retry", response_model=BackgroundTaskResponse)
    def retry_background_task(
        task_id: UUID,
        user: AuthUser | None = Depends(current_user),
    ) -> BackgroundTaskResponse:
        if user is None:
            raise HTTPException(status_code=401, detail="请先登录。")
        try:
            task = retry_task(database_file, user.id, str(task_id))
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return BackgroundTaskResponse(**task)

    @api.post("/tasks/{task_id}/cancel", response_model=BackgroundTaskResponse)
    def cancel_background_task(
        task_id: UUID,
        user: AuthUser | None = Depends(current_user),
    ) -> BackgroundTaskResponse:
        if user is None:
            raise HTTPException(status_code=401, detail="请先登录。")
        try:
            task = cancel_task(database_file, user.id, str(task_id))
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return BackgroundTaskResponse(**task)

    @api.post(
        "/notifications/{notification_id}/read",
        response_model=NotificationResponse,
    )
    def mark_notification_read(
        notification_id: UUID,
        user: AuthUser | None = Depends(current_user),
    ) -> NotificationResponse:
        if user is None:
            raise HTTPException(status_code=401, detail="请先登录。")
        try:
            notification = task_store(user).mark_notification_read(
                str(notification_id)
            )
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return NotificationResponse(**notification)

    @api.post("/notifications/read-all", status_code=204)
    def mark_all_notifications_read(
        response: Response,
        user: AuthUser | None = Depends(current_user),
    ) -> Response:
        if user is None:
            raise HTTPException(status_code=401, detail="请先登录。")
        task_store(user).mark_all_notifications_read()
        response.status_code = 204
        return response

    @api.get("/tasks/events")
    async def stream_background_tasks(
        request: Request,
        user: AuthUser | None = Depends(current_user),
    ) -> StreamingResponse:
        if user is None:
            raise HTTPException(status_code=401, detail="请先登录。")

        async def events():
            store = task_store(user)
            last_payload = ""
            heartbeat = 0
            while not await request.is_disconnected():
                payload = {
                    "type": "task_center",
                    "tasks": store.list(),
                    "notifications": store.list_notifications(),
                    "unread_count": store.unread_count(),
                }
                serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
                if serialized != last_payload:
                    yield _sse_event(payload)
                    last_payload = serialized
                    heartbeat = 0
                elif heartbeat >= 14:
                    yield ": keepalive\n\n"
                    heartbeat = 0
                heartbeat += 1
                await asyncio.sleep(1)

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @api.get("/mcp/tools", response_model=MCPToolsResponse)
    def mcp_tools(
        user: AuthUser | None = Depends(current_user),
    ) -> MCPToolsResponse:
        """返回内置 MCP 与当前用户已启用的远程 MCP 工具。"""
        if user is None:
            raise HTTPException(status_code=401, detail="请先登录。")
        try:
            registry = build_user_mcp_registry(database_file, user.id)
            server_infos = registry.server_infos()
            configs = {
                str(item["id"]): item for item in user_mcp_store(user).list()
            }
            enriched: list[MCPServerInfo] = []
            for info in server_infos:
                config = configs.get(str(info.get("id") or ""))
                if config is not None:
                    info["has_auth"] = bool(config.get("has_auth"))
                    if not config.get("enabled", True):
                        info["enabled"] = False
                        info["status"] = "disabled"
                enriched.append(MCPServerInfo(**info))
            for config in user_mcp_store(user).list():
                if config.get("enabled"):
                    continue
                enriched.append(
                    MCPServerInfo(
                        id=str(config["id"]),
                        name=str(config["name"]),
                        transport=(
                            "SSE"
                            if config["transport"] == "sse"
                            else "HTTP"
                        ),
                        builtin=False,
                        enabled=False,
                        url=str(config["url"]),
                        has_auth=bool(config.get("has_auth")),
                        status="disabled",
                        error_message=str(config["last_error"])
                        if config.get("last_error")
                        else None,
                        tools=[],
                    )
                )
        except Exception as error:
            raise HTTPException(
                status_code=503,
                detail=f"MCP 工具发现失败：{error}",
            ) from error

        return MCPToolsResponse(servers=enriched)

    @api.post("/mcp/servers", response_model=MCPServerInfo, status_code=201)
    def create_remote_mcp_server(
        request: CreateRemoteMcpRequest,
        user: AuthUser | None = Depends(current_user),
    ) -> MCPServerInfo:
        if user is None:
            raise HTTPException(status_code=401, detail="请先登录。")
        try:
            url = validate_remote_mcp_url(request.url)
            tools = probe_remote_mcp(
                name=request.name,
                url=url,
                transport=request.transport,
                auth_token=request.auth_token,
            )
            existing_names = build_user_mcp_registry(
                database_file, user.id
            ).tool_names
            conflicts = {
                str(tool["function"]["name"])
                for tool in tools
                if isinstance(tool.get("function"), dict)
            } & existing_names
            if conflicts:
                raise ValueError(
                    "与现有工具重名：" + "、".join(sorted(conflicts))
                )
            created = user_mcp_store(user).create(
                request.name,
                url,
                request.transport,
                request.auth_token,
                request.enabled,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        refresh_user_mcp(user)
        return MCPServerInfo(
            id=str(created["id"]),
            name=str(created["name"]),
            transport="SSE" if created["transport"] == "sse" else "HTTP",
            builtin=False,
            enabled=bool(created["enabled"]),
            url=str(created["url"]),
            has_auth=bool(created["has_auth"]),
            status="ready",
            tools=[
                MCPToolInfo(
                    name=str(tool["function"]["name"]),
                    description=str(tool["function"]["description"]),
                    parameters=tool["function"]["parameters"],  # type: ignore[arg-type]
                )
                for tool in tools
                if isinstance(tool.get("function"), dict)
            ],
        )

    @api.patch("/mcp/servers/{server_id}", response_model=MCPServerInfo)
    def update_remote_mcp_server(
        server_id: str,
        request: UpdateRemoteMcpRequest,
        user: AuthUser | None = Depends(current_user),
    ) -> MCPServerInfo:
        if user is None:
            raise HTTPException(status_code=401, detail="请先登录。")
        store = user_mcp_store(user)
        try:
            current = store.get(server_id)
            next_url = (
                validate_remote_mcp_url(request.url)
                if request.url is not None
                else str(current["url"])
            )
            next_transport = request.transport or str(current["transport"])
            next_name = request.name or str(current["name"])
            token = store.auth_token(server_id)
            if request.clear_auth_token:
                token = None
            elif request.auth_token is not None:
                token = request.auth_token.strip() or None
            if request.enabled is not False:
                probe_remote_mcp(
                    name=next_name,
                    url=next_url,
                    transport=next_transport,
                    auth_token=token,
                )
            updated = store.update(
                server_id,
                name=request.name,
                url=next_url if request.url is not None else None,
                transport=request.transport,
                auth_token=request.auth_token,
                clear_auth_token=request.clear_auth_token,
                enabled=request.enabled,
                clear_last_error=True,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        refresh_user_mcp(user)
        return MCPServerInfo(
            id=str(updated["id"]),
            name=str(updated["name"]),
            transport="SSE" if updated["transport"] == "sse" else "HTTP",
            builtin=False,
            enabled=bool(updated["enabled"]),
            url=str(updated["url"]),
            has_auth=bool(updated["has_auth"]),
            status="disabled" if not updated["enabled"] else "ready",
            tools=[],
        )

    @api.delete("/mcp/servers/{server_id}", status_code=204)
    def delete_remote_mcp_server(
        server_id: str,
        response: Response,
        user: AuthUser | None = Depends(current_user),
    ) -> Response:
        if user is None:
            raise HTTPException(status_code=401, detail="请先登录。")
        try:
            user_mcp_store(user).delete(server_id)
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        refresh_user_mcp(user)
        response.status_code = 204
        return response

    def _openapi_source_payload(
        source: dict[str, object],
        tools: list[dict[str, object]] | None = None,
    ) -> OpenApiSourceInfo:
        tool_items = tools if tools is not None else list(source.get("tools") or [])
        status: Literal["ready", "error", "disabled"] = "ready"
        if not source.get("enabled", True):
            status = "disabled"
        elif source.get("error_message") or source.get("last_error"):
            status = "error"
        error = source.get("error_message") or source.get("last_error")
        return OpenApiSourceInfo(
            id=str(source["id"]),
            name=str(source["name"]),
            base_url=str(source["base_url"]),
            enabled=bool(source.get("enabled", True)),
            has_auth=bool(source.get("has_auth")),
            auth_header=str(source.get("auth_header") or "Authorization"),
            selected_operations=[
                str(item) for item in (source.get("selected_operations") or [])
            ],
            status=status,
            error_message=str(error) if error else None,
            tools=[
                OpenApiToolInfo(
                    name=str(tool.get("name") or ""),
                    description=str(tool.get("description") or ""),
                    parameters=tool.get("parameters")  # type: ignore[arg-type]
                    if isinstance(tool.get("parameters"), dict)
                    else {},
                    method=str(tool["method"]) if tool.get("method") else None,
                    path=str(tool["path"]) if tool.get("path") else None,
                    operation_id=(
                        str(tool["operation_id"]) if tool.get("operation_id") else None
                    ),
                    unsafe=bool(tool.get("unsafe")),
                )
                for tool in tool_items
                if isinstance(tool, dict)
            ],
            created_at=str(source["created_at"]) if source.get("created_at") else None,
            updated_at=str(source["updated_at"]) if source.get("updated_at") else None,
        )

    @api.post("/openapi/preview", response_model=OpenApiPreviewResponse)
    def openapi_preview(
        request: OpenApiPreviewRequest,
        user: AuthUser | None = Depends(current_user),
    ) -> OpenApiPreviewResponse:
        if user is None:
            raise HTTPException(status_code=401, detail="请先登录。")
        try:
            preview = preview_openapi(
                name=request.name,
                spec_text=request.spec_text,
                base_url=request.base_url,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return OpenApiPreviewResponse(
            name=str(preview["name"]),
            title=str(preview["title"]),
            openapi_version=str(preview["openapi_version"]),
            base_url=str(preview["base_url"]),
            operation_count=int(preview["operation_count"]),
            operations=[
                OpenApiOperationInfo(
                    operation_id=str(item["operation_id"]),
                    tool_name=str(item["tool_name"]),
                    method=str(item["method"]),
                    path=str(item["path"]),
                    summary=str(item["summary"]),
                    unsafe=bool(item.get("unsafe")),
                    write=bool(item.get("write")),
                )
                for item in preview["operations"]
            ],
        )

    @api.get("/openapi/sources", response_model=OpenApiSourcesResponse)
    def list_openapi_sources(
        user: AuthUser | None = Depends(current_user),
    ) -> OpenApiSourcesResponse:
        if user is None:
            raise HTTPException(status_code=401, detail="请先登录。")
        store = user_openapi_store(user)
        registry = build_user_openapi_registry(database_file, user.id)
        ready = {str(item["id"]): item for item in registry.source_infos()}
        sources: list[OpenApiSourceInfo] = []
        for item in store.list():
            source_id = str(item["id"])
            if source_id in ready:
                merged = {**item, **ready[source_id]}
                sources.append(_openapi_source_payload(merged))
            else:
                sources.append(
                    _openapi_source_payload(
                        {
                            **item,
                            "tools": [],
                            "status": "disabled"
                            if not item.get("enabled", True)
                            else "error",
                            "error_message": item.get("last_error"),
                        }
                    )
                )
        return OpenApiSourcesResponse(sources=sources)

    @api.post("/openapi/sources", response_model=OpenApiSourceInfo, status_code=201)
    def create_openapi_source(
        request: CreateOpenApiSourceRequest,
        user: AuthUser | None = Depends(current_user),
    ) -> OpenApiSourceInfo:
        if user is None:
            raise HTTPException(status_code=401, detail="请先登录。")
        try:
            preview = preview_openapi(
                name=request.name,
                spec_text=request.spec_text,
                base_url=request.base_url,
            )
            selected = select_operations(
                list(preview["operations"]),
                request.selected_operations,
            )
            selected_ids = [str(item["operation_id"]) for item in selected]
            created = user_openapi_store(user).create(
                request.name,
                str(preview["base_url"]),
                parse_openapi_document(request.spec_text),
                selected_ids,
                request.auth_token,
                request.auth_header,
                request.enabled,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        refresh_user_openapi(user)
        registry = build_user_openapi_registry(database_file, user.id)
        infos = {
            str(item["id"]): item for item in registry.source_infos()
        }
        merged = {**created, **infos.get(str(created["id"]), {})}
        return _openapi_source_payload(merged)

    @api.patch("/openapi/sources/{source_id}", response_model=OpenApiSourceInfo)
    def update_openapi_source(
        source_id: str,
        request: UpdateOpenApiSourceRequest,
        user: AuthUser | None = Depends(current_user),
    ) -> OpenApiSourceInfo:
        if user is None:
            raise HTTPException(status_code=401, detail="请先登录。")
        store = user_openapi_store(user)
        try:
            current = store.get(source_id)
            selected = request.selected_operations
            if selected is not None:
                select_operations(
                    list(
                        preview_openapi(
                            name=str(current["name"]),
                            spec_text=json.dumps(current["spec"], ensure_ascii=False),
                            base_url=request.base_url or str(current["base_url"]),
                        )["operations"]
                    ),
                    selected,
                )
            updated = store.update(
                source_id,
                name=request.name,
                base_url=request.base_url,
                selected_operations=selected,
                auth_token=request.auth_token,
                clear_auth_token=request.clear_auth_token,
                auth_header=request.auth_header,
                enabled=request.enabled,
                clear_last_error=True,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        refresh_user_openapi(user)
        registry = build_user_openapi_registry(database_file, user.id)
        infos = {
            str(item["id"]): item for item in registry.source_infos()
        }
        merged = {**updated, **infos.get(str(updated["id"]), {})}
        return _openapi_source_payload(merged)

    @api.delete("/openapi/sources/{source_id}", status_code=204)
    def delete_openapi_source(
        source_id: str,
        response: Response,
        user: AuthUser | None = Depends(current_user),
    ) -> Response:
        if user is None:
            raise HTTPException(status_code=401, detail="请先登录。")
        try:
            user_openapi_store(user).delete(source_id)
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        refresh_user_openapi(user)
        response.status_code = 204
        return response

    @api.get("/skills", response_model=SkillsResponse)
    def skills_catalog(
        _user: AuthUser | None = Depends(current_user),
    ) -> SkillsResponse:
        """返回当前可按需加载的 Agent Skill 目录。"""
        return SkillsResponse(
            skills=[
                SkillSummary(name=item.name, description=item.description)
                for item in list_skills()
            ]
        )

    @api.get("/skills/{name}", response_model=SkillDetailResponse)
    def skill_detail(
        name: str,
        _user: AuthUser | None = Depends(current_user),
    ) -> SkillDetailResponse:
        """返回指定 Skill 的完整工作说明书。"""
        try:
            skill = load_skill(name)
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return SkillDetailResponse(
            name=skill.name,
            description=skill.description,
            body=skill.body,
        )

    @api.get("/workflows", response_model=WorkflowListResponse)
    def list_workflows(
        user: AuthUser | None = Depends(current_user),
    ) -> WorkflowListResponse:
        if user is None:
            raise HTTPException(status_code=401, detail="请先登录。")
        return WorkflowListResponse(
            workflows=[
                WorkflowResponse(**item) for item in workflow_store(user).list()
            ]
        )

    @api.post("/workflows", response_model=WorkflowResponse)
    def save_workflow(
        request: WorkflowSaveRequest,
        user: AuthUser | None = Depends(current_user),
    ) -> WorkflowResponse:
        if user is None:
            raise HTTPException(status_code=401, detail="请先登录。")
        try:
            saved = workflow_store(user).save(
                request.name,
                request.description,
                request.definition.model_dump(),
                str(request.id) if request.id else None,
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return WorkflowResponse(**saved)

    @api.get("/workflows/{workflow_id}", response_model=WorkflowResponse)
    def get_workflow(
        workflow_id: UUID,
        user: AuthUser | None = Depends(current_user),
    ) -> WorkflowResponse:
        if user is None:
            raise HTTPException(status_code=401, detail="请先登录。")
        try:
            saved = workflow_store(user).get(str(workflow_id))
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return WorkflowResponse(**saved)

    @api.post(
        "/workflows/{workflow_id}/runs",
        response_model=WorkflowLaunchResponse,
    )
    def run_workflow(
        workflow_id: UUID,
        request: WorkflowRunRequest,
        user: AuthUser | None = Depends(current_user),
    ) -> WorkflowLaunchResponse:
        if user is None:
            raise HTTPException(status_code=401, detail="请先登录。")
        store = workflow_store(user)
        try:
            saved = store.get(str(workflow_id))
            WorkflowDefinition.model_validate(saved["definition"])
            run_id = store.create_run(
                str(workflow_id), request.input, status="queued"
            )
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        task = schedule_workflow_run(
            database_file,
            user.id,
            str(workflow_id),
            run_id,
            str(saved["name"]),
        )
        return WorkflowLaunchResponse(
            run=WorkflowRunResponse(**store.get_run(run_id)),
            task=BackgroundTaskResponse(**task),
        )

    @api.get("/workflow-runs/{run_id}", response_model=WorkflowRunResponse)
    def get_workflow_run(
        run_id: UUID,
        user: AuthUser | None = Depends(current_user),
    ) -> WorkflowRunResponse:
        if user is None:
            raise HTTPException(status_code=401, detail="请先登录。")
        try:
            run = workflow_store(user).get_run(str(run_id))
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return WorkflowRunResponse(**run)

    @api.post(
        "/workflow-runs/{run_id}/approve",
        response_model=WorkflowLaunchResponse,
    )
    def approve_workflow_run_async(
        run_id: UUID,
        _request: WorkflowApprovalRequest,
        user: AuthUser | None = Depends(current_user),
    ) -> WorkflowLaunchResponse:
        if user is None:
            raise HTTPException(status_code=401, detail="请先登录。")
        store = workflow_store(user)
        try:
            run = store.get_run(str(run_id))
            if run["status"] != "waiting_approval":
                raise ValueError("当前工作流不在等待确认状态。")
            store.reopen_run(str(run_id))
            task = resume_workflow_run(database_file, user.id, str(run_id))
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return WorkflowLaunchResponse(
            run=WorkflowRunResponse(**store.get_run(str(run_id))),
            task=BackgroundTaskResponse(**task),
        )

    def workflow_event_stream(
        user: AuthUser,
        workflow_id: str,
        run_id: str,
        input_text: str,
        approved: bool,
    ):
        store = workflow_store(user)
        saved = store.get(workflow_id)
        definition = WorkflowDefinition.model_validate(saved["definition"])
        yield _sse_event({"type": "run", "run_id": run_id})
        terminal = False
        try:
            for event in stream_workflow(
                definition,
                input_text,
                [user_knowledge_dir(user.id), public_knowledge_dir()],
                public_knowledge_dir(),
                workflow_mcp_client(user),
                approved=approved,
                todo_store=user_todo_store(user),
                workflow_store=store,
                current_workflow_id=workflow_id,
            ):
                event_type = str(event.get("type", ""))
                if event_type == "approval_required":
                    store.finish_run(
                        run_id,
                        "waiting_approval",
                        "",
                        list(event.get("steps", [])),
                    )
                    terminal = True
                elif event_type == "node_failed":
                    store.finish_run(
                        run_id,
                        "failed",
                        "",
                        list(event.get("steps", [])),
                        str(event.get("message", "工作流执行失败")),
                    )
                    terminal = True
                elif event_type == "run_completed":
                    store.finish_run(
                        run_id,
                        "success",
                        str(event.get("output", "")),
                        list(event.get("steps", [])),
                    )
                    terminal = True
                yield _sse_event(event)
        except Exception as error:
            if not terminal:
                store.finish_run(run_id, "failed", "", [], str(error))
            yield _sse_event({"type": "error", "message": str(error)})

    @api.post("/workflows/{workflow_id}/runs/stream")
    def stream_workflow_run(
        workflow_id: UUID,
        request: WorkflowRunRequest,
        user: AuthUser | None = Depends(current_user),
    ) -> StreamingResponse:
        if user is None:
            raise HTTPException(status_code=401, detail="请先登录。")
        store = workflow_store(user)
        try:
            store.get(str(workflow_id))
            run_id = store.create_run(str(workflow_id), request.input)
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return StreamingResponse(
            workflow_event_stream(
                user, str(workflow_id), run_id, request.input, approved=False
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @api.post("/workflow-runs/{run_id}/approve/stream")
    def approve_workflow_run(
        run_id: UUID,
        _request: WorkflowApprovalRequest,
        user: AuthUser | None = Depends(current_user),
    ) -> StreamingResponse:
        if user is None:
            raise HTTPException(status_code=401, detail="请先登录。")
        store = workflow_store(user)
        try:
            run = store.get_run(str(run_id))
            if run["status"] != "waiting_approval":
                raise ValueError("当前工作流不在等待确认状态。")
            store.reopen_run(str(run_id))
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return StreamingResponse(
            workflow_event_stream(
                user,
                str(run["workflow_id"]),
                str(run_id),
                str(run["input_text"]),
                approved=True,
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @api.get(
        "/workflows/{workflow_id}/runs",
        response_model=WorkflowRunListResponse,
    )
    def list_workflow_runs(
        workflow_id: UUID,
        user: AuthUser | None = Depends(current_user),
    ) -> WorkflowRunListResponse:
        if user is None:
            raise HTTPException(status_code=401, detail="请先登录。")
        try:
            runs = workflow_store(user).list_runs(str(workflow_id))
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return WorkflowRunListResponse(
            runs=[WorkflowRunResponse(**run) for run in runs]
        )

    @api.get(
        "/sessions/{session_id}/messages",
        response_model=HistoryResponse,
        response_model_exclude_none=True,
    )
    def history(
        session_id: UUID,
        user: AuthUser | None = Depends(current_user),
    ) -> HistoryResponse:
        if user is not None and not auth.owns_agent_session(user.id, str(session_id)):
            raise HTTPException(status_code=403, detail="你无权访问这个 Agent 会话。")
        return HistoryResponse(
            session_id=session_id,
            messages=[
                HistoryMessage(**message)
                for message in manager.load_history(session_id)
            ],
        )

    @api.get("/sessions", response_model=SessionListResponse)
    def list_sessions(
        user: AuthUser | None = Depends(current_user),
    ) -> SessionListResponse:
        if user is None:
            raise HTTPException(status_code=401, detail="请先登录。")
        return SessionListResponse(
            sessions=[
                SessionSummary(**summary)
                for summary in SqliteConversationStore.list_user_sessions(
                    database_file,
                    user.id,
                )
            ]
        )

    @api.get("/observability/runs", response_model=AgentRunListResponse)
    def list_agent_runs(
        user: AuthUser | None = Depends(current_user),
    ) -> AgentRunListResponse:
        if user is None:
            raise HTTPException(status_code=401, detail="请先登录。")
        return AgentRunListResponse(
            runs=[
                AgentRunSummaryResponse(**run)
                for run in observability_store(user).list_runs()
            ]
        )

    @api.get(
        "/observability/runs/{run_id}",
        response_model=AgentRunDetailResponse,
    )
    def get_agent_run(
        run_id: UUID,
        user: AuthUser | None = Depends(current_user),
    ) -> AgentRunDetailResponse:
        if user is None:
            raise HTTPException(status_code=401, detail="请先登录。")
        try:
            run = observability_store(user).get_run(str(run_id))
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return AgentRunDetailResponse(**run)

    @api.get("/metrics")
    def get_metrics(
        days: int = Query(default=7, ge=1, le=90),
        model: str | None = None,
        prompt_version_id: str | None = None,
        user: AuthUser | None = Depends(current_user),
    ) -> dict[str, object]:
        if user is None:
            raise HTTPException(status_code=401, detail="请先登录。")
        return collect_metrics(
            database_file,
            user.id,
            days=days,
            model=model,
            prompt_version_id=prompt_version_id,
        )

    @api.get("/agentops/config", response_model=AgentOpsConfigResponse)
    def get_agentops_config(
        user: AuthUser | None = Depends(current_user),
    ) -> AgentOpsConfigResponse:
        if user is None:
            raise HTTPException(status_code=401, detail="请先登录。")
        return AgentOpsConfigResponse(**agentops_store(user).config())

    @api.post("/agentops/prompts", response_model=PromptVersionResponse)
    def create_prompt_version(
        request: PromptCreateRequest,
        user: AuthUser | None = Depends(current_user),
    ) -> PromptVersionResponse:
        if user is None:
            raise HTTPException(status_code=401, detail="请先登录。")
        return PromptVersionResponse(**agentops_store(user).create_prompt(
            request.name, request.content, request.change_note
        ))

    @api.post("/agentops/prompts/{prompt_id}/activate", response_model=PromptVersionResponse)
    def activate_prompt_version(
        prompt_id: UUID,
        user: AuthUser | None = Depends(current_user),
    ) -> PromptVersionResponse:
        if user is None:
            raise HTTPException(status_code=401, detail="请先登录。")
        try:
            return PromptVersionResponse(**agentops_store(user).activate_prompt(str(prompt_id)))
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @api.post("/agentops/datasets", response_model=EvaluationDatasetResponse)
    def create_evaluation_dataset(
        request: DatasetCreateRequest,
        user: AuthUser | None = Depends(current_user),
    ) -> EvaluationDatasetResponse:
        if user is None:
            raise HTTPException(status_code=401, detail="请先登录。")
        return EvaluationDatasetResponse(**agentops_store(user).create_dataset(
            request.name, request.description
        ))

    @api.post("/agentops/datasets/{dataset_id}/cases", response_model=EvaluationTestCaseResponse)
    def create_evaluation_case(
        dataset_id: UUID,
        request: EvaluationCaseCreateRequest,
        user: AuthUser | None = Depends(current_user),
    ) -> EvaluationTestCaseResponse:
        if user is None:
            raise HTTPException(status_code=401, detail="请先登录。")
        try:
            value = agentops_store(user).create_case(
                str(dataset_id), request.name, request.category,
                request.input_text, request.expected_keywords,
                request.scoring_method, request.expected_answer, request.judge_rubric,
            )
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return EvaluationTestCaseResponse(**value)

    @api.patch("/agentops/cases/{case_id}", response_model=EvaluationTestCaseResponse)
    def update_evaluation_case(
        case_id: UUID,
        request: EvaluationCaseUpdateRequest,
        user: AuthUser | None = Depends(current_user),
    ) -> EvaluationTestCaseResponse:
        if user is None:
            raise HTTPException(status_code=401, detail="请先登录。")
        try:
            value = agentops_store(user).update_case(
                str(case_id), **request.model_dump(exclude_unset=True)
            )
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return EvaluationTestCaseResponse(**value)

    @api.delete("/agentops/cases/{case_id}", status_code=204)
    def delete_evaluation_case(
        case_id: UUID,
        user: AuthUser | None = Depends(current_user),
    ) -> Response:
        if user is None:
            raise HTTPException(status_code=401, detail="请先登录。")
        try:
            agentops_store(user).delete_case(str(case_id))
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return Response(status_code=204)

    @api.get("/evaluations/runs", response_model=EvaluationRunListResponse)
    def list_evaluation_runs(
        user: AuthUser | None = Depends(current_user),
    ) -> EvaluationRunListResponse:
        if user is None:
            raise HTTPException(status_code=401, detail="请先登录。")
        return EvaluationRunListResponse(
            runs=[
                EvaluationRunSummaryResponse(**run)
                for run in evaluation_store(user).list_runs()
            ]
        )

    @api.get(
        "/evaluations/runs/{run_id}",
        response_model=EvaluationRunDetailResponse,
    )
    def get_evaluation_run(
        run_id: UUID,
        user: AuthUser | None = Depends(current_user),
    ) -> EvaluationRunDetailResponse:
        if user is None:
            raise HTTPException(status_code=401, detail="请先登录。")
        try:
            run = evaluation_store(user).get(str(run_id))
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return EvaluationRunDetailResponse(**run)

    @api.post(
        "/evaluations/runs",
        response_model=EvaluationRunDetailResponse | EvaluationLaunchResponse,
    )
    def create_evaluation_run(
        request: EvaluationRunRequest,
        user: AuthUser | None = Depends(current_user),
    ) -> EvaluationRunDetailResponse | EvaluationLaunchResponse:
        if user is None:
            raise HTTPException(status_code=401, detail="请先登录。")
        store = evaluation_store(user)
        model = request.model or os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash")
        if request.prompt_version_id is not None and request.dataset_id is not None:
            config_store = agentops_store(user)
            try:
                prompt = config_store.get_prompt(str(request.prompt_version_id))
                dataset = config_store.get_dataset(str(request.dataset_id))
            except ValueError as error:
                raise HTTPException(status_code=404, detail=str(error)) from error
            enabled_cases = [case for case in dataset["cases"] if case["enabled"]]
            if not enabled_cases:
                raise HTTPException(status_code=400, detail="测试集至少需要一个已启用用例。")
            baseline_run_id = (
                str(request.baseline_run_id)
                if request.baseline_run_id is not None else next(
                    (
                        str(run["id"])
                        for run in store.list_runs(limit=50)
                        if run["status"] == "completed"
                        and run.get("dataset_id") == str(dataset["id"])
                    ),
                    None,
                )
            )
            run_id = store.start(
                model, len(enabled_cases),
                prompt_version_id=str(prompt["id"]),
                prompt_version_name=f"v{prompt['version']} · {prompt['name']}",
                dataset_id=str(dataset["id"]), dataset_name=str(dataset["name"]),
                scorer=request.scorer,
                judge_enabled=request.judge_enabled,
                baseline_run_id=baseline_run_id,
                status="queued",
            )
            task = schedule_evaluation_run(
                database_file, user.id, run_id, str(prompt["id"]),
                f"v{prompt['version']} · {prompt['name']}", str(dataset["id"]),
                str(dataset["name"]), model, request.scorer, request.judge_enabled,
                baseline_run_id,
            )
            return EvaluationLaunchResponse(
                run=EvaluationRunDetailResponse(**store.get(run_id)),
                task=BackgroundTaskResponse(**task),
            )
        run_id = store.start(
            model,
            total_cases=6,
            scorer=request.scorer,
            judge_enabled=request.judge_enabled,
        )
        try:
            result = run_core_evaluation(store, run_id)
            store.finish(
                run_id,
                result["passed_cases"],
                result["total_tokens"],
                score=round(result["passed_cases"] / 6 * 100, 2),
                confidence="medium",
            )
        except Exception as error:
            store.finish(run_id, 0, 0, status="failed")
            raise HTTPException(
                status_code=500,
                detail=f"评测运行失败：{error}",
            ) from error
        return EvaluationRunDetailResponse(**store.get(run_id))

    @api.get(
        "/evaluations/runs/{run_id}/compare",
        response_model=EvaluationComparisonResponse,
    )
    def compare_evaluation_run(
        run_id: UUID,
        baseline_run_id: UUID | None = Query(default=None),
        user: AuthUser | None = Depends(current_user),
    ) -> EvaluationComparisonResponse:
        if user is None:
            raise HTTPException(status_code=401, detail="请先登录。")
        store = evaluation_store(user)
        try:
            current_run = store.get(str(run_id))
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        target_baseline_id = (
            str(baseline_run_id)
            if baseline_run_id is not None
            else str(current_run.get("baseline_run_id") or "")
        )
        if not target_baseline_id:
            raise HTTPException(status_code=400, detail="当前运行没有可比较的基线版本。")
        try:
            baseline_run = store.get(target_baseline_id)
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return EvaluationComparisonResponse(
            **_build_evaluation_comparison(current_run, baseline_run)
        )

    @api.get("/evaluations/runs/{run_id}/export")
    def export_evaluation_run(
        run_id: UUID,
        format: Literal["json", "csv"] = Query(default="json"),
        user: AuthUser | None = Depends(current_user),
    ) -> Response:
        if user is None:
            raise HTTPException(status_code=401, detail="请先登录。")
        try:
            run = evaluation_store(user).get(str(run_id))
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        filename, payload = _export_evaluation_run(run, format)
        return Response(
            content=payload,
            media_type="application/json" if format == "json" else "text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @api.post("/collaborations/stream")
    def collaboration_stream(
        request: CollaborationRequest,
        user: AuthUser | None = Depends(current_user),
    ) -> StreamingResponse:
        """运行规划；完成后暂停等人确认，再由 approve 继续执行与审核。"""
        if user is None:
            raise HTTPException(status_code=401, detail="请先登录。")
        session_id, managed = get_agent_session(request.session_id, user)
        run_store = observability_store(user)
        collab_store = user_collaboration_store(user)
        run_id = run_store.start_run(
            "multi_agent",
            request.goal[:120],
            str(session_id),
            os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash"),
        )

        def generate_events():
            yield _sse_event({"type": "session", "session_id": str(session_id)})
            collaboration_id: str | None = None
            try:
                with managed.lock:
                    managed.agent.reset_trace()
                    for event in run_collaboration(
                        request.goal,
                        managed.agent,
                        pause_after_plan=True,
                    ):
                        payload: dict[str, object] = {
                            "type": "collaboration",
                            "agent": event.agent,
                            "label": event.label,
                            "status": event.status,
                            "detail": event.detail,
                            "content": event.content,
                            "duration_ms": event.duration_ms,
                            "stage_id": event.stage_id,
                            "call_id": event.call_id,
                            "tool_name": event.tool_name,
                            "tool_source": event.tool_source,
                        }
                        if event.status == "waiting_approval":
                            record = collab_store.create_waiting(
                                session_id=str(session_id),
                                goal=request.goal,
                                plan_text=event.content or "",
                                observability_run_id=run_id,
                            )
                            collaboration_id = str(record["id"])
                            payload["collaboration_id"] = collaboration_id
                        yield _sse_event(payload)
                finish_observed_run(
                    run_store, run_id, managed.agent, "waiting_approval"
                )
                yield _sse_event(
                    {
                        "type": "done",
                        "answer": "",
                        "approval_required": True,
                        "collaboration_id": collaboration_id,
                    }
                )
            except (MissingAPIKeyError, OpenAIError, ValueError, RuntimeError) as error:
                if collaboration_id:
                    try:
                        collab_store.mark_failed(collaboration_id, str(error))
                    except ValueError:
                        pass
                finish_observed_run(
                    run_store, run_id, managed.agent, "failed", str(error)
                )
                yield _sse_event({"type": "error", "message": str(error)})

        return StreamingResponse(
            generate_events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @api.post("/collaborations/{collaboration_id}/approve")
    def approve_collaboration(
        collaboration_id: UUID,
        user: AuthUser | None = Depends(current_user),
    ) -> StreamingResponse:
        """确认规划后继续执行 Agent 与审核 Agent。"""
        if user is None:
            raise HTTPException(status_code=401, detail="请先登录。")
        collab_store = user_collaboration_store(user)
        try:
            record = collab_store.get(str(collaboration_id))
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        if record["status"] != "waiting_approval":
            raise HTTPException(
                status_code=400,
                detail="该协作任务不在等待确认状态。",
            )
        session_id, managed = get_agent_session(
            UUID(str(record["session_id"])), user
        )
        run_store = observability_store(user)
        observability_run_id = str(record.get("observability_run_id") or "")
        if not observability_run_id:
            observability_run_id = run_store.start_run(
                "multi_agent",
                str(record["goal"])[:120],
                str(session_id),
                os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash"),
            )
        collab_store.mark_running(str(collaboration_id))

        def generate_events():
            yield _sse_event({"type": "session", "session_id": str(session_id)})
            final_answer = ""
            try:
                with managed.lock:
                    managed.agent.reset_trace()
                    for event in resume_collaboration(
                        str(record["goal"]),
                        str(record["plan_text"]),
                        managed.agent,
                    ):
                        yield _sse_event(
                            {
                                "type": "collaboration",
                                "agent": event.agent,
                                "label": event.label,
                                "status": event.status,
                                "detail": event.detail,
                                "content": event.content,
                                "duration_ms": event.duration_ms,
                                "stage_id": event.stage_id,
                                "call_id": event.call_id,
                                "tool_name": event.tool_name,
                                "tool_source": event.tool_source,
                                "collaboration_id": str(collaboration_id),
                            }
                        )
                        if event.agent == "reviewer" and event.status == "completed":
                            final_answer = event.content or ""
                    if final_answer:
                        user_message = (
                            "请通过多 Agent 协作完成："
                            f"{str(record['goal']).strip()}"
                        )
                        managed.agent.messages.extend(
                            [
                                {"role": "user", "content": user_message},
                                {"role": "assistant", "content": final_answer},
                            ]
                        )
                        if managed.agent.conversation_store is not None:
                            managed.agent.conversation_store.append_turn(
                                user_message,
                                final_answer,
                                operation_type="collaboration",
                            )
                collab_store.mark_completed(str(collaboration_id), final_answer)
                finish_observed_run(
                    run_store, observability_run_id, managed.agent, "success"
                )
                yield _sse_event({"type": "done", "answer": final_answer})
            except (MissingAPIKeyError, OpenAIError, ValueError, RuntimeError) as error:
                try:
                    collab_store.mark_failed(str(collaboration_id), str(error))
                except ValueError:
                    pass
                finish_observed_run(
                    run_store,
                    observability_run_id,
                    managed.agent,
                    "failed",
                    str(error),
                )
                yield _sse_event({"type": "error", "message": str(error)})

        return StreamingResponse(
            generate_events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @api.post(
        "/collaborations/{collaboration_id}/reject",
        response_model=CollaborationRejectResponse,
    )
    def reject_collaboration(
        collaboration_id: UUID,
        user: AuthUser | None = Depends(current_user),
    ) -> CollaborationRejectResponse:
        """拒绝规划，取消后续执行。"""
        if user is None:
            raise HTTPException(status_code=401, detail="请先登录。")
        collab_store = user_collaboration_store(user)
        try:
            record = collab_store.get(str(collaboration_id))
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        if record["status"] != "waiting_approval":
            raise HTTPException(
                status_code=400,
                detail="该协作任务不在等待确认状态。",
            )
        collab_store.mark_cancelled(str(collaboration_id))
        observability_run_id = record.get("observability_run_id")
        if observability_run_id:
            try:
                observability_store(user).finish_run(
                    str(observability_run_id),
                    "cancelled",
                    error_message="用户取消了多 Agent 协作计划",
                )
            except ValueError:
                pass
        return CollaborationRejectResponse(collaboration_id=collaboration_id)

    @api.post("/routing/collaboration", response_model=CollaborationRoutingResponse)
    def route_collaboration_mode(
        request: CollaborationRoutingRequest,
        user: AuthUser | None = Depends(current_user),
    ) -> CollaborationRoutingResponse:
        """判断普通输入是否应自动走多 Agent 协作。"""
        if user is None:
            raise HTTPException(status_code=401, detail="请先登录。")
        result = route_conversation_mode(request.message)
        return CollaborationRoutingResponse(
            mode=result["mode"],  # type: ignore[arg-type]
            use_collaboration=bool(result["use_collaboration"]),
            reason=str(result["reason"]),
            score=int(result.get("score") or 0),
        )

    @api.post("/chat", response_model=ChatResponse)
    def chat(
        request: ChatRequest,
        user: AuthUser | None = Depends(current_user),
    ) -> ChatResponse:
        session_id, managed = get_agent_session(request.session_id, user)
        run_store = observability_store(user)
        run_id = run_store.start_run(
            "chat",
            request.message[:120],
            str(session_id),
            os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash"),
        )
        run_error: str | None = None
        managed.agent.reset_trace()
        try:
            with managed.lock:
                answer = managed.agent.ask(
                    request.message,
                    approval_callback=lambda _name, _arguments: request.approve,
                )
                SqliteConversationStore(
                    database_file, str(session_id)
                ).set_latest_user_operation(request.operation_type)
                approval_required = (
                    managed.agent.last_confirmation_requested
                    and managed.agent.last_action_cancelled
                )
        except MissingAPIKeyError as error:
            run_error = str(error)
            raise HTTPException(status_code=503, detail=str(error)) from error
        except OpenAIError as error:
            run_error = str(error)
            raise HTTPException(status_code=502, detail=f"模型调用失败：{error}") from error
        except ValueError as error:
            run_error = str(error)
            raise HTTPException(status_code=400, detail=str(error)) from error
        except RuntimeError as error:
            run_error = str(error)
            raise HTTPException(status_code=500, detail=str(error)) from error
        finally:
            finish_observed_run(
                run_store,
                run_id,
                managed.agent,
                "failed" if run_error else "success",
                run_error,
            )

        return ChatResponse(
            session_id=session_id,
            answer=answer,
            approval_required=approval_required,
            sources=list(getattr(managed.agent, "last_sources", []) or []),
            confidence=str(getattr(managed.agent, "last_confidence", "none")),
            grounding=str(getattr(managed.agent, "last_grounding", "none")),
            captured_regression=maybe_capture_grounding_failure(
                user, request.message, answer, managed.agent
            ),
        )

    @api.post("/chat/stream")
    def chat_stream(
        request: ChatRequest,
        user: AuthUser | None = Depends(current_user),
    ) -> StreamingResponse:
        session_id, managed = get_agent_session(request.session_id, user)
        run_store = observability_store(user)
        run_id = run_store.start_run(
            "chat_stream",
            request.message[:120],
            str(session_id),
            os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash"),
        )
        managed.agent.reset_trace()

        def generate_events():
            yield _sse_event(
                {"type": "session", "session_id": str(session_id)}
            )
            answer_parts: list[str] = []
            try:
                with managed.lock:
                    for event in managed.agent.ask_stream(
                        request.message,
                        approval_callback=lambda _name, _arguments: request.approve,
                        include_tool_activity=True,
                    ):
                        if isinstance(event, ToolActivity):
                            yield _sse_event(
                                {
                                    "type": "tool_activity",
                                    "call_id": event.call_id,
                                    "tool_name": event.tool_name,
                                    "source": event.source,
                                    "status": event.status,
                                    "message": event.message,
                                }
                            )
                        else:
                            answer_parts.append(event)
                            yield _sse_event({"type": "delta", "content": event})
                    SqliteConversationStore(
                        database_file, str(session_id)
                    ).set_latest_user_operation(request.operation_type)
                    approval_required = (
                        managed.agent.last_confirmation_requested
                        and managed.agent.last_action_cancelled
                    )
                final_answer = "".join(answer_parts)
                history = getattr(managed.agent, "conversation", None)
                if not isinstance(history, list):
                    history = getattr(managed.agent, "messages", [])
                last_message = history[-1] if history else None
                if isinstance(last_message, dict) and last_message.get("role") == "assistant":
                    final_answer = str(last_message.get("content") or final_answer)
                captured = maybe_capture_grounding_failure(
                    user, request.message, final_answer, managed.agent
                )
                if hasattr(managed.agent, "grounding_payload"):
                    grounding = managed.agent.grounding_payload()
                else:
                    grounding = {
                        "sources": list(getattr(managed.agent, "last_sources", []) or []),
                        "confidence": str(
                            getattr(managed.agent, "last_confidence", "none")
                        ),
                        "grounding": str(
                            getattr(managed.agent, "last_grounding", "none")
                        ),
                    }
                yield _sse_event(
                    {
                        "type": "done",
                        "approval_required": approval_required,
                        "sources": grounding.get("sources", []),
                        "confidence": grounding.get("confidence", "none"),
                        "grounding": grounding.get("grounding", "none"),
                        "captured_regression": captured,
                    }
                )
            except (MissingAPIKeyError, OpenAIError, ValueError, RuntimeError) as error:
                finish_observed_run(
                    run_store,
                    run_id,
                    managed.agent,
                    "failed",
                    str(error),
                )
                yield _sse_event({"type": "error", "message": str(error)})
            else:
                finish_observed_run(
                    run_store,
                    run_id,
                    managed.agent,
                    "success",
                )

        return StreamingResponse(
            generate_events(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    @api.post("/plans", response_model=PlanResponse)
    def create_plan(
        request: PlanRequest,
        user: AuthUser | None = Depends(current_user),
    ) -> PlanResponse:
        session_id, managed = get_agent_session(request.session_id, user)
        run_store = observability_store(user)
        run_id = run_store.start_run(
            "goal_plan", request.goal[:120], str(session_id),
            os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash"),
        )
        run_error: str | None = None
        managed.agent.reset_trace()
        try:
            with managed.lock:
                plan = managed.agent.create_plan(request.goal)
                SqliteConversationStore(
                    database_file, str(session_id)
                ).set_latest_user_operation(request.operation_type)
        except MissingAPIKeyError as error:
            run_error = str(error)
            raise HTTPException(status_code=503, detail=str(error)) from error
        except OpenAIError as error:
            run_error = str(error)
            raise HTTPException(status_code=502, detail=f"模型调用失败：{error}") from error
        except ValueError as error:
            run_error = str(error)
            raise HTTPException(status_code=400, detail=str(error)) from error
        except RuntimeError as error:
            run_error = str(error)
            raise HTTPException(status_code=500, detail=str(error)) from error
        finally:
            finish_observed_run(
                run_store, run_id, managed.agent,
                "failed" if run_error else "success", run_error,
            )

        return PlanResponse(session_id=session_id, plan=plan)

    @api.post("/travel-plans", response_model=TravelPlanResponse)
    def create_travel_plan(
        request: TravelPlanRequest,
        user: AuthUser | None = Depends(current_user),
    ) -> TravelPlanResponse:
        session_id, managed = get_agent_session(request.session_id, user)
        store = user_travel_store(user)
        run_store = observability_store(user)
        run_id = run_store.start_run(
            "travel_plan", request.request[:120], str(session_id),
            os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash"),
        )
        run_error: str | None = None
        managed.agent.reset_trace()
        try:
            previous_plan = None
            if request.travel_plan_id is not None:
                previous_saved = store.get(request.travel_plan_id)
                if previous_saved["session_id"] != str(session_id):
                    raise ValueError("旅行计划不属于当前 Agent 会话。")
                previous_plan = TravelPlan.model_validate(previous_saved)
            with managed.lock:
                plan = managed.agent.create_travel_plan(
                    request.request,
                    previous_plan=previous_plan,
                )
                SqliteConversationStore(
                    database_file, str(session_id)
                ).set_latest_user_operation(request.operation_type)
                database_started_at = perf_counter()
                saved = store.save(
                    str(session_id),
                    plan,
                    user_input=request.request,
                    travel_plan_id=request.travel_plan_id,
                )
                managed.agent.trace_steps.append(
                    {
                        "step_type": "database",
                        "name": "保存旅行计划",
                        "status": "success",
                        "duration_ms": int(
                            (perf_counter() - database_started_at) * 1000
                        ),
                        "detail": f"版本 {saved['version']}",
                    }
                )
        except MissingAPIKeyError as error:
            run_error = str(error)
            raise HTTPException(status_code=503, detail=str(error)) from error
        except OpenAIError as error:
            run_error = str(error)
            raise HTTPException(status_code=502, detail=f"模型调用失败：{error}") from error
        except ValueError as error:
            run_error = str(error)
            raise HTTPException(status_code=400, detail=str(error)) from error
        except RuntimeError as error:
            run_error = str(error)
            raise HTTPException(status_code=500, detail=str(error)) from error
        finally:
            finish_observed_run(
                run_store, run_id, managed.agent,
                "failed" if run_error else "success", run_error,
            )

        return TravelPlanResponse(
            session_id=session_id,
            travel_plan_id=int(saved["id"]),
            version=int(saved["version"]),
            plan=plan,
        )

    @api.get(
        "/travel-plans",
        response_model=list[TravelPlanSummaryResponse],
    )
    def list_travel_plans(
        user: AuthUser | None = Depends(current_user),
    ) -> list[TravelPlanSummaryResponse]:
        return [
            TravelPlanSummaryResponse(**item)
            for item in user_travel_store(user).list_all()
        ]

    @api.get(
        "/travel-plans/pending/{session_id}",
        response_model=TravelPlanResponse | None,
    )
    def pending_travel_plan(
        session_id: UUID,
        user: AuthUser | None = Depends(current_user),
    ) -> TravelPlanResponse | None:
        if user is not None and not auth.owns_agent_session(user.id, str(session_id)):
            raise HTTPException(status_code=403, detail="你无权访问这个 Agent 会话。")
        saved = user_travel_store(user).latest_pending(str(session_id))
        if saved is None:
            return None
        return TravelPlanResponse(
            session_id=session_id,
            travel_plan_id=int(saved["id"]),
            version=int(saved["version"]),
            plan=TravelPlan.model_validate(saved),
        )

    @api.get(
        "/travel-plans/{travel_plan_id}",
        response_model=SavedTravelPlanResponse,
    )
    def get_travel_plan(
        travel_plan_id: int,
        user: AuthUser | None = Depends(current_user),
    ) -> SavedTravelPlanResponse:
        try:
            saved = user_travel_store(user).get(travel_plan_id)
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return SavedTravelPlanResponse(**saved)

    @api.post(
        "/travel-plans/{travel_plan_id}/confirm",
        response_model=TravelPlanResponse,
    )
    def confirm_travel_plan(
        travel_plan_id: int,
        user: AuthUser | None = Depends(current_user),
    ) -> TravelPlanResponse:
        try:
            saved = user_travel_store(user).confirm(travel_plan_id)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        return TravelPlanResponse(
            session_id=UUID(str(saved["session_id"])),
            travel_plan_id=travel_plan_id,
            version=int(saved["version"]),
            plan=TravelPlan.model_validate(saved),
        )

    @api.post(
        "/travel-plans/{travel_plan_id}/preparations",
        response_model=TravelPreparationResponse,
    )
    def create_travel_preparations(
        travel_plan_id: int,
        user: AuthUser | None = Depends(current_user),
    ) -> TravelPreparationResponse:
        store = user_travel_store(user)
        saved = store.get(travel_plan_id)
        session_id = UUID(str(saved["session_id"]))
        _, managed = get_agent_session(session_id, user)
        run_store = observability_store(user)
        run_id = run_store.start_run(
            "travel_preparation", f"旅行计划 #{travel_plan_id} 准备清单",
            str(session_id), os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash"),
        )
        run_error: str | None = None
        restored = False
        managed.agent.reset_trace()
        try:
            existing = store.get_preparation(travel_plan_id)
            if existing is not None:
                restored = True
                plan = GoalPlan.model_validate(existing)
                managed.agent.trace_steps.append(
                    {
                        "step_type": "cache",
                        "name": "恢复准备清单",
                        "status": "success",
                        "duration_ms": 0,
                        "detail": "命中已持久化结果，未调用模型",
                    }
                )
            else:
                travel_plan = TravelPlan.model_validate(saved)
                with managed.lock:
                    plan = managed.agent.create_travel_preparation(travel_plan)
                    database_started_at = perf_counter()
                    store.save_preparation(travel_plan_id, plan.model_dump())
                    managed.agent.trace_steps.append(
                        {
                            "step_type": "database",
                            "name": "保存准备清单",
                            "status": "success",
                            "duration_ms": int(
                                (perf_counter() - database_started_at) * 1000
                            ),
                            "detail": f"旅行计划 #{travel_plan_id}",
                        }
                    )
        except MissingAPIKeyError as error:
            run_error = str(error)
            raise HTTPException(status_code=503, detail=str(error)) from error
        except OpenAIError as error:
            run_error = str(error)
            raise HTTPException(status_code=502, detail=f"模型调用失败：{error}") from error
        except ValueError as error:
            run_error = str(error)
            raise HTTPException(status_code=400, detail=str(error)) from error
        except RuntimeError as error:
            run_error = str(error)
            raise HTTPException(status_code=500, detail=str(error)) from error
        finally:
            finish_observed_run(
                run_store, run_id, managed.agent,
                "failed" if run_error else "success", run_error,
            )
        return TravelPreparationResponse(
            travel_plan_id=travel_plan_id,
            plan=plan,
            restored=restored,
        )

    @api.post(
        "/travel-plans/{travel_plan_id}/todos",
        response_model=PlanTodoSyncResponse,
    )
    def sync_travel_preparations(
        travel_plan_id: int,
        request: TravelTodoSyncRequest,
        user: AuthUser | None = Depends(current_user),
    ) -> PlanTodoSyncResponse:
        run_store = observability_store(user)
        run_id = run_store.start_run(
            "todo_sync", f"旅行计划 #{travel_plan_id} 同步 Todo", model=None
        )
        database_started_at = perf_counter()
        try:
            travel_store = user_travel_store(user)
            saved = travel_store.get(travel_plan_id)
            if saved["status"] != "confirmed":
                raise ValueError("请先确认旅行计划，再同步准备事项。")
            todo_store = SqliteTodoStore(
                database_file,
                session_id=user.id if user is not None else "anonymous",
            )
            todo_plan_title = f"{str(saved['title'])[:89]} · 出行准备"
            saved_plan = todo_store.create_plan_with_steps(
                todo_plan_title,
                f"旅行计划 #{travel_plan_id} 的出行前准备事项。",
                "high",
                [step.model_dump() for step in request.selected_steps],
                source_travel_plan_id=travel_plan_id,
            )
            travel_store.mark_todo_synced(
                travel_plan_id,
                int(saved_plan["id"]),
            )
            run_store.add_step(
                run_id, "database", "同步准备事项到 Todo", "success",
                int((perf_counter() - database_started_at) * 1000),
                f"Todo 计划 #{saved_plan['id']}，{len(saved_plan['todos'])} 项",
            )
        except ValueError as error:
            run_store.add_step(
                run_id, "database", "同步准备事项到 Todo", "failed",
                int((perf_counter() - database_started_at) * 1000), str(error),
            )
            run_store.finish_run(run_id, "failed", error_message=str(error))
            raise HTTPException(status_code=400, detail=str(error)) from error
        run_store.finish_run(run_id, "success")
        return PlanTodoSyncResponse(
            session_id=UUID(str(saved["session_id"])),
            plan=TodoPlanResponse(**saved_plan),
        )

    @api.get(
        "/travel-plans/{travel_plan_id}/workflow",
        response_model=TravelWorkflowResponse,
    )
    def get_travel_workflow(
        travel_plan_id: int,
        user: AuthUser | None = Depends(current_user),
    ) -> TravelWorkflowResponse:
        try:
            workflow = user_travel_store(user).workflow(travel_plan_id)
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return TravelWorkflowResponse(**workflow)

    @api.get(
        "/travel-plans/{travel_plan_id}/checkpoints",
        response_model=list[TravelCheckpointResponse],
    )
    def travel_plan_checkpoints(
        travel_plan_id: int,
        user: AuthUser | None = Depends(current_user),
    ) -> list[TravelCheckpointResponse]:
        try:
            checkpoints = user_travel_store(user).list_checkpoints(travel_plan_id)
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return [TravelCheckpointResponse(**item) for item in checkpoints]

    @api.post("/plans/todos", response_model=PlanTodoSyncResponse)
    def sync_plan_todos(
        request: PlanTodoSyncRequest,
        user: AuthUser | None = Depends(current_user),
    ) -> PlanTodoSyncResponse:
        """只有前端明确确认后，才把选中的计划步骤写入 Todo。"""
        session_id, managed = get_agent_session(request.session_id, user)
        try:
            with managed.lock:
                if user is None:
                    todos = managed.agent.add_plan_todos(
                        [step.title for step in request.selected_steps]
                    )
                    saved_plan = {
                        "id": 0,
                        "title": request.plan.title,
                        "summary": request.plan.summary,
                        "priority": request.plan.priority,
                        "created_at": datetime.now(timezone.utc).replace(tzinfo=None),
                        "todos": todos,
                    }
                else:
                    saved_plan = user_todo_store(user).create_plan_with_steps(
                        request.plan.title,
                        request.plan.summary,
                        request.plan.priority,
                        [step.model_dump() for step in request.selected_steps],
                    )
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error
        except OSError as error:
            raise HTTPException(status_code=500, detail="Todo 保存失败。") from error

        return PlanTodoSyncResponse(
            session_id=session_id,
            plan=TodoPlanResponse(**saved_plan),
        )

    return api


app = create_api()
