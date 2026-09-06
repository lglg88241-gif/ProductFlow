import type { ImageSessionDetail } from "./types";

export interface AgentMessage {
  id: string;
  role: "user" | "assistant" | "tool";
  content: string;
  tool_name: string | null;
  image_session_id: string | null;
  created_at: string;
}

export interface AgentSession {
  id: string;
  title: string;
  stage: string;
  image_session_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface AgentSessionDetail extends AgentSession {
  messages: AgentMessage[];
}

export interface AgentSessionListResponse {
  items: AgentSession[];
}

export interface AgentGenerationTaskStatus {
  image_session_id: string | null;
  task_id: string;
  status: string;
}

export interface AgentToolEvent {
  tool: string;
  result: {
    status?: string;
    message?: string;
    image_session_id?: string;
    completed_assets?: Array<{
      asset_id: string;
      download_url: string;
      preview_url: string;
      size: string;
    }>;
    copies?: Array<{ title?: string; content?: string; hashtags?: string[] }>;
    matches?: AgentAssetEntry[];
    recommendations?: AgentDesignRecommendation[];
    report?: AgentCopyReport;
    template_profile?: Record<string, unknown> | null;
    tags?: string[];
    title?: string;
    [key: string]: unknown;
  };
}

export interface AgentAssetEntry {
  id: string;
  kind: string;
  title: string;
  source: string;
  mime_type: string;
  width: number | null;
  height: number | null;
  tags: string[];
  template_profile: Record<string, unknown> | null;
  download_url: string;
  preview_url: string;
  thumbnail_url: string;
  created_at: string;
}

export interface AgentDesignRecommendation extends AgentAssetEntry {
  why: string;
}

export interface AgentCopyReport {
  headline?: string;
  moments_caption?: string;
  selling_points?: string[];
  hashtags?: string[];
  publishing_tips?: string;
  [key: string]: unknown;
}

export interface AgentAssetListResponse {
  items: AgentAssetEntry[];
}

export interface AgentTurnResponse {
  session: AgentSessionDetail;
  tool_events: AgentToolEvent[];
  pending_generation_tasks: AgentGenerationTaskStatus[];
  image_session_snapshot?: ImageSessionDetail | null;
}
