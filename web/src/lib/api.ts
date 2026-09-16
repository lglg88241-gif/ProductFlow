import type {
  AgentAssetEntry,
  AgentAssetListResponse,
  AgentSession,
  AgentSessionDetail,
  AgentSessionListResponse,
  AgentTurnResponse,
} from "./agentTypes";
import type {
  ApplyWorkflowTemplateGroupInput,
  CanvasTemplateSummary,
  CanvasTemplateListResponse,
  ConfigResponse,
  ConfigUpdateRequest,
  CopySet,
  CopySetUpdateRequest,
  DuplicateWorkflowNodeGroupInput,
  GalleryEntry,
  GalleryEntryListResponse,
  GenerationQueueOverview,
  CreateUserTemplateGroupInput,
  CreateProductInput,
  CopyInputExtractionResponse,
  ImageSessionDetail,
  ImageSessionDiscussionResponse,
  ImageSessionListResponse,
  ImageSessionStatus,
  ImageToolOptions,
  ProductDetail,
  ProductHistory,
  ProviderBinding,
  ProviderBindingUpdateRequest,
  ProviderConfigResponse,
  ProviderProfile,
  ProviderProfileCreateRequest,
  ProviderProfileUpdateRequest,
  ProductWorkflow,
  ProductWorkflowStatus,
  ProductWritebackResponse,
  ProductListResponse,
  RuntimeConfig,
  SettingsLockState,
  SettingsExportPayload,
  SettingsImportCommitResponse,
  SettingsImportPreviewResponse,
  SessionState,
  UpdateUserTemplateGroupInput,
  AuthUser,
} from "./types";

const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL as string | undefined)?.replace(/\/$/, "") ?? "";

/** 用户登录类端点的 CSRF 最小防线头：所有非 GET 请求统一附加（与后端 user_auth 约定一致） */
const CSRF_HEADER_NAME = "X-Requested-With";
const CSRF_HEADER_VALUE = "productflow";

export class ApiError extends Error {
  status: number;
  detail: string;

  constructor(status: number, detail: string) {
    super(detail);
    this.status = status;
    this.detail = detail;
  }
}

function toApiUrl(path: string): string {
  if (path.startsWith("http://") || path.startsWith("https://")) {
    return path;
  }
  return `${API_BASE_URL}${path}`;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const method = (init?.method ?? "GET").toUpperCase();
  const response = await fetch(toApiUrl(path), {
    ...init,
    credentials: "include",
    headers: {
      ...(init?.body instanceof FormData ? {} : { "Content-Type": "application/json" }),
      // 非 GET 一律带上 CSRF 校验头；调用方显式传入的同名头可覆盖
      ...(method === "GET" ? {} : { [CSRF_HEADER_NAME]: CSRF_HEADER_VALUE }),
      ...init?.headers,
    },
  });

  if (!response.ok) {
    let detail = "请求失败";
    try {
      const payload = (await response.json()) as { detail?: string };
      detail = payload.detail ?? detail;
    } catch {
      detail = response.statusText || detail;
    }
    throw new ApiError(response.status, detail);
  }

  if (response.status === 204) {
    return undefined as T;
  }
  return (await response.json()) as T;
}

export const api = {
  toApiUrl,
  getSessionState(): Promise<SessionState> {
    return request<SessionState>("/api/auth/session");
  },
  createSession(adminKey: string): Promise<{ ok: boolean }> {
    return request("/api/auth/session", {
      method: "POST",
      body: JSON.stringify({ admin_key: adminKey }),
    });
  },
  destroySession(): Promise<{ ok: boolean }> {
    return request("/api/auth/session", { method: "DELETE" });
  },
  /** 当前用户账号（数据隔离开启时用于鉴权判定；401 = 未登录） */
  getMe(): Promise<AuthUser> {
    return request<AuthUser>("/api/auth/user/me");
  },
  /** 用一次性邀请兑换账号（自助设置用户名与密码）；成功即建立用户会话 */
  redeemInvite(token: string, username: string, password: string): Promise<AuthUser> {
    return request<AuthUser>("/api/auth/invite/redeem", {
      method: "POST",
      body: JSON.stringify({ token, username, password }),
    });
  },
  userLogin(username: string, password: string): Promise<AuthUser> {
    return request("/api/auth/user/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    });
  },
  userLogout(): Promise<{ ok: boolean }> {
    return request("/api/auth/user/logout", { method: "POST" });
  },
  listProducts(input?: { page?: number; page_size?: number }): Promise<ProductListResponse> {
    const page = input?.page ?? 1;
    const pageSize = input?.page_size ?? 20;
    return request(`/api/products?page=${encodeURIComponent(page)}&page_size=${encodeURIComponent(pageSize)}`);
  },
  getProduct(productId: string): Promise<ProductDetail> {
    return request(`/api/products/${productId}`);
  },
  deleteProduct(productId: string): Promise<void> {
    return request(`/api/products/${productId}`, { method: "DELETE" });
  },
  getProductHistory(productId: string): Promise<ProductHistory> {
    return request(`/api/products/${productId}/history`);
  },
  getConfig(): Promise<ConfigResponse> {
    return request("/api/settings");
  },
  getProviderConfig(): Promise<ProviderConfigResponse> {
    return request("/api/settings/provider-config");
  },
  createProviderProfile(payload: ProviderProfileCreateRequest): Promise<ProviderProfile> {
    return request("/api/settings/provider-profiles", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },
  updateProviderProfile(profileId: string, payload: ProviderProfileUpdateRequest): Promise<ProviderProfile> {
    return request(`/api/settings/provider-profiles/${profileId}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    });
  },
  archiveProviderProfile(profileId: string): Promise<ProviderProfile> {
    return request(`/api/settings/provider-profiles/${profileId}`, { method: "DELETE" });
  },
  updateProviderBinding(purpose: "text" | "image" | "agent", payload: ProviderBindingUpdateRequest): Promise<ProviderBinding> {
    return request(`/api/settings/provider-bindings/${purpose}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    });
  },
  getSettingsLockState(): Promise<SettingsLockState> {
    return request("/api/settings/lock-state");
  },
  getRuntimeConfig(): Promise<RuntimeConfig> {
    return request("/api/settings/runtime");
  },
  getGenerationQueueOverview(): Promise<GenerationQueueOverview> {
    return request("/api/generation-queue");
  },
  unlockSettings(token: string): Promise<SettingsLockState> {
    return request("/api/settings/unlock", {
      method: "POST",
      body: JSON.stringify({ token }),
    });
  },
  updateConfig(payload: ConfigUpdateRequest): Promise<ConfigResponse> {
    return request("/api/settings", {
      method: "PATCH",
      body: JSON.stringify(payload),
    });
  },
  exportSettings(): Promise<SettingsExportPayload> {
    return request("/api/settings/export");
  },
  previewSettingsImport(payload: SettingsExportPayload): Promise<SettingsImportPreviewResponse> {
    return request("/api/settings/import/preview", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },
  importSettings(payload: SettingsExportPayload): Promise<SettingsImportCommitResponse> {
    return request("/api/settings/import", {
      method: "POST",
      body: JSON.stringify(payload),
    });
  },
  async createProduct(input: CreateProductInput): Promise<ProductDetail> {
    const formData = new FormData();
    formData.set("name", input.name);
    formData.set("image", input.file);
    input.referenceFiles?.forEach((referenceFile) => {
      formData.append("reference_images", referenceFile);
    });
    if (input.category) {
      formData.set("category", input.category);
    }
    if (input.price) {
      formData.set("price", input.price);
    }
    if (input.source_note) {
      formData.set("source_note", input.source_note);
    }
    if (input.canvas_template_key !== undefined) {
      formData.set("canvas_template_key", input.canvas_template_key);
    }
    if (input.template_language !== undefined) {
      formData.set("template_language", input.template_language);
    }
    return request("/api/products", {
      method: "POST",
      body: formData,
    });
  },
  async extractCopyInput(file: File): Promise<CopyInputExtractionResponse> {
    const formData = new FormData();
    formData.set("file", file);
    return request("/api/copy-inputs/extract", {
      method: "POST",
      body: formData,
    });
  },
  async addReferenceImages(productId: string, files: File[]): Promise<ProductDetail> {
    const formData = new FormData();
    files.forEach((file) => {
      formData.append("reference_images", file);
    });
    return request(`/api/products/${productId}/reference-images`, {
      method: "POST",
      body: formData,
    });
  },
  deleteSourceAsset(assetId: string): Promise<ProductDetail> {
    return request(`/api/source-assets/${assetId}`, { method: "DELETE" });
  },
  updateCopySet(copySetId: string, payload: CopySetUpdateRequest): Promise<CopySet> {
    return request(`/api/copy-sets/${copySetId}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    });
  },
  confirmCopySet(copySetId: string): Promise<CopySet> {
    return request(`/api/copy-sets/${copySetId}/confirm`, { method: "POST" });
  },
  listImageSessions(): Promise<ImageSessionListResponse> {
    return request("/api/image-sessions");
  },
  createImageSession(input: { title?: string }): Promise<ImageSessionDetail> {
    return request("/api/image-sessions", {
      method: "POST",
      body: JSON.stringify(input),
    });
  },
  getImageSession(sessionId: string): Promise<ImageSessionDetail> {
    return request(`/api/image-sessions/${sessionId}`);
  },
  getImageSessionStatus(sessionId: string): Promise<ImageSessionStatus> {
    return request(`/api/image-sessions/${sessionId}/status`);
  },
  updateImageSession(sessionId: string, input: { title: string }): Promise<ImageSessionDetail> {
    return request(`/api/image-sessions/${sessionId}`, {
      method: "PATCH",
      body: JSON.stringify(input),
    });
  },
  deleteImageSession(sessionId: string): Promise<void> {
    return request(`/api/image-sessions/${sessionId}`, { method: "DELETE" });
  },
  async addImageSessionReferenceImages(sessionId: string, files: File[]): Promise<ImageSessionDetail> {
    const formData = new FormData();
    files.forEach((file) => {
      formData.append("reference_images", file);
    });
    return request(`/api/image-sessions/${sessionId}/reference-images`, {
      method: "POST",
      body: formData,
    });
  },
  deleteImageSessionReferenceImage(sessionId: string, assetId: string): Promise<ImageSessionDetail> {
    return request(`/api/image-sessions/${sessionId}/reference-images/${assetId}`, { method: "DELETE" });
  },
  generateImageSessionRound(
    sessionId: string,
    input: {
      prompt: string;
      size: string;
      base_asset_id?: string | null;
      selected_reference_asset_ids?: string[];
      generation_count?: number;
      tool_options?: ImageToolOptions | null;
    },
  ): Promise<ImageSessionDetail> {
    return request(`/api/image-sessions/${sessionId}/generate`, {
      method: "POST",
      body: JSON.stringify(input),
    });
  },
  sendImageSessionMessage(
    sessionId: string,
    input: {
      content: string;
      current_asset_id?: string | null;
      selected_reference_asset_ids?: string[];
    },
  ): Promise<ImageSessionDiscussionResponse> {
    return request(`/api/image-sessions/${sessionId}/messages`, {
      method: "POST",
      body: JSON.stringify(input),
    });
  },
  retryImageSessionGenerationTask(sessionId: string, taskId: string): Promise<ImageSessionDetail> {
    return request(`/api/image-sessions/${sessionId}/generation-tasks/${taskId}/retry`, { method: "POST" });
  },
  cancelImageSessionGenerationTask(sessionId: string, taskId: string): Promise<ImageSessionDetail> {
    return request(`/api/image-sessions/${sessionId}/generation-tasks/${taskId}/cancel`, { method: "POST" });
  },
  attachImageSessionAssetToProduct(
    sessionId: string,
    assetId: string,
    input: { product_id: string; target: "reference" | "main_source" },
  ): Promise<ProductWritebackResponse> {
    return request(`/api/image-sessions/${sessionId}/assets/${assetId}/attach-to-product`, {
      method: "POST",
      body: JSON.stringify(input),
    });
  },
  listGalleryEntries(): Promise<GalleryEntryListResponse> {
    return request("/api/gallery");
  },
  saveGalleryEntry(imageSessionAssetId: string): Promise<GalleryEntry> {
    return request("/api/gallery", {
      method: "POST",
      body: JSON.stringify({ image_session_asset_id: imageSessionAssetId }),
    });
  },
  getProductWorkflow(productId: string): Promise<ProductWorkflow> {
    return request(`/api/products/${productId}/workflow`);
  },
  getProductWorkflowStatus(productId: string): Promise<ProductWorkflowStatus> {
    return request(`/api/products/${productId}/workflow/status`);
  },
  listCanvasTemplates(): Promise<CanvasTemplateListResponse> {
    return request("/api/workflow/canvas-templates");
  },
  applyWorkflowTemplateGroup(productId: string, input: ApplyWorkflowTemplateGroupInput): Promise<ProductWorkflow> {
    return request(`/api/products/${productId}/workflow/template-groups`, {
      method: "POST",
      body: JSON.stringify(input),
    });
  },
  duplicateWorkflowNodeGroup(
    productId: string,
    input: DuplicateWorkflowNodeGroupInput,
  ): Promise<ProductWorkflow> {
    return request(`/api/products/${productId}/workflow/node-groups/duplicate`, {
      method: "POST",
      body: JSON.stringify(input),
    });
  },
  createUserTemplateGroup(productId: string, input: CreateUserTemplateGroupInput): Promise<CanvasTemplateSummary> {
    return request(`/api/products/${productId}/workflow/user-template-groups`, {
      method: "POST",
      body: JSON.stringify(input),
    });
  },
  updateUserTemplateGroup(templateId: string, input: UpdateUserTemplateGroupInput): Promise<CanvasTemplateSummary> {
    return request(`/api/workflow/user-template-groups/${templateId}`, {
      method: "PATCH",
      body: JSON.stringify(input),
    });
  },
  archiveUserTemplateGroup(templateId: string): Promise<void> {
    return request(`/api/workflow/user-template-groups/${templateId}`, {
      method: "DELETE",
    });
  },
  createWorkflowNode(
    productId: string,
    input: {
      node_type: ProductWorkflow["nodes"][number]["node_type"];
      title: string;
      position_x: number;
      position_y: number;
      config_json: Record<string, unknown>;
    },
  ): Promise<ProductWorkflow> {
    return request(`/api/products/${productId}/workflow/nodes`, {
      method: "POST",
      body: JSON.stringify(input),
    });
  },
  updateWorkflowNode(
    nodeId: string,
    input: {
      title?: string;
      position_x?: number;
      position_y?: number;
      config_json?: Record<string, unknown>;
    },
  ): Promise<ProductWorkflow> {
    return request(`/api/workflow-nodes/${nodeId}`, {
      method: "PATCH",
      body: JSON.stringify(input),
    });
  },
  updateWorkflowNodeCopy(nodeId: string, payload: CopySetUpdateRequest): Promise<ProductWorkflow> {
    return request(`/api/workflow-nodes/${nodeId}/copy`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    });
  },
  async uploadWorkflowNodeImage(
    nodeId: string,
    input: { file: File; role?: string; label?: string },
  ): Promise<ProductWorkflow> {
    const formData = new FormData();
    formData.set("image", input.file);
    if (input.role) {
      formData.set("role", input.role);
    }
    if (input.label) {
      formData.set("label", input.label);
    }
    return request(`/api/workflow-nodes/${nodeId}/image`, {
      method: "POST",
      body: formData,
    });
  },
  bindWorkflowNodeImage(
    nodeId: string,
    input: { source_asset_id?: string; poster_variant_id?: string },
  ): Promise<ProductWorkflow> {
    return request(`/api/workflow-nodes/${nodeId}/image-source`, {
      method: "POST",
      body: JSON.stringify(input),
    });
  },
  createWorkflowEdge(
    productId: string,
    input: { source_node_id: string; target_node_id: string; source_handle?: string; target_handle?: string },
  ): Promise<ProductWorkflow> {
    return request(`/api/products/${productId}/workflow/edges`, {
      method: "POST",
      body: JSON.stringify(input),
    });
  },
  deleteWorkflowEdge(edgeId: string): Promise<ProductWorkflow> {
    return request(`/api/workflow-edges/${edgeId}`, { method: "DELETE" });
  },
  deleteWorkflowNode(nodeId: string): Promise<ProductWorkflow> {
    return request(`/api/workflow-nodes/${nodeId}`, { method: "DELETE" });
  },
  runProductWorkflow(productId: string, input?: { start_node_id?: string }): Promise<ProductWorkflow> {
    return request(`/api/products/${productId}/workflow/run`, {
      method: "POST",
      body: JSON.stringify(input ?? {}),
    });
  },
  cancelProductWorkflowRun(productId: string, runId: string): Promise<ProductWorkflow> {
    return request(`/api/products/${productId}/workflow/runs/${runId}/cancel`, { method: "POST" });
  },
  retryProductWorkflowRun(productId: string, runId: string): Promise<ProductWorkflow> {
    return request(`/api/products/${productId}/workflow/runs/${runId}/retry`, { method: "POST" });
  },
  listAgentSessions(): Promise<AgentSessionListResponse> {
    return request("/api/agent/sessions");
  },
  createAgentSession(input: { title?: string }): Promise<AgentSession> {
    return request("/api/agent/sessions", {
      method: "POST",
      body: JSON.stringify(input),
    });
  },
  getAgentSession(sessionId: string): Promise<AgentSessionDetail> {
    return request(`/api/agent/sessions/${sessionId}`);
  },
  deleteAgentSession(sessionId: string): Promise<void> {
    return request(`/api/agent/sessions/${sessionId}`, { method: "DELETE" });
  },
  sendAgentMessage(sessionId: string, content: string): Promise<AgentTurnResponse> {
    return request(`/api/agent/sessions/${sessionId}/messages`, {
      method: "POST",
      body: JSON.stringify({ content }),
    });
  },
  /** SSE 流式对话：每收到一帧调用 onEvent；流结束时 resolve 最后一个 done 帧。 */
  async streamAgentMessage(
    sessionId: string,
    content: string,
    onEvent: (event: string, data: Record<string, unknown>) => void,
  ): Promise<Record<string, unknown> | null> {
    const response = await fetch(toApiUrl(`/api/agent/sessions/${sessionId}/messages/stream`), {
      method: "POST",
      headers: { "Content-Type": "application/json", [CSRF_HEADER_NAME]: CSRF_HEADER_VALUE },
      credentials: "include",
      body: JSON.stringify({ content }),
    });
    if (!response.ok || !response.body) {
      let detail = `HTTP ${response.status}`;
      try {
        const payload = (await response.json()) as { detail?: string };
        if (payload.detail) detail = payload.detail;
      } catch {
        /* 保留默认 detail */
      }
      throw new ApiError(response.status, detail);
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let doneFrame: Record<string, unknown> | null = null;
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let boundary = buffer.indexOf("\n\n");
      while (boundary !== -1) {
        const block = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        boundary = buffer.indexOf("\n\n"); // 立即重算，注释块 continue 时也不会死循环
        let event = "message";
        let data: Record<string, unknown> = {};
        let hasFields = false;
        for (const line of block.split("\n")) {
          if (line.startsWith(":")) continue; // SSE 注释帧（如心跳 ": ping"），不是事件
          if (line.startsWith("event: ")) {
            event = line.slice(7);
            hasFields = true;
          } else if (line.startsWith("data: ")) {
            try {
              data = JSON.parse(line.slice(6)) as Record<string, unknown>;
            } catch {
              data = {};
            }
            hasFields = true;
          }
        }
        if (!hasFields) continue; // 纯注释块：整体跳过，不派发事件
        if (event === "done") doneFrame = data;
        onEvent(event, data);
      }
    }
    return doneFrame;
  },
  listAgentAssets(kind?: string): Promise<AgentAssetListResponse> {
    const query = kind ? `?kind=${encodeURIComponent(kind)}` : "";
    return request(`/api/agent/assets${query}`);
  },
  async uploadAgentAsset(file: File, kind: string, agentSessionId?: string | null): Promise<AgentAssetEntry> {
    const body = new FormData();
    body.append("file", file);
    body.append("kind", kind);
    // 带上会话 id：后端会自动打标并把"刚上传了模板"写进会话，agent 因此知情
    if (agentSessionId) body.append("agent_session_id", agentSessionId);
    return request("/api/agent/assets", { method: "POST", body });
  },
  deleteAgentAsset(assetId: string): Promise<void> {
    return request(`/api/agent/assets/${assetId}`, { method: "DELETE" });
  },
};
