import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2, Paperclip, Plus, Send } from "lucide-react";

import { TopNav } from "../components/TopNav";
import { api } from "../lib/api";
import type {
  AgentAssetEntry,
  AgentCopyReport,
  AgentDesignRecommendation,
  AgentSessionDetail,
  AgentToolEvent,
} from "../lib/agentTypes";
import { useI18n } from "../lib/preferences";
import type { ImageSessionRound } from "../lib/types";

type TranslationKey = Parameters<ReturnType<typeof useI18n>["t"]>[0];

const STAGE_LABEL_KEYS: Record<string, TranslationKey> = {
  clarify: "workbench.stage.clarify",
  recommend: "workbench.stage.recommend",
  produce: "workbench.stage.produce",
  review: "workbench.stage.review",
};

function CopyProposals({ event }: { event: AgentToolEvent }) {
  const { t } = useI18n();
  const copies = event.result.copies ?? [];
  if (copies.length === 0) return null;
  return (
    <div className="mt-2 space-y-2 rounded-xl border border-slate-200 bg-white p-3">
      <p className="text-xs font-medium text-slate-500">{t("workbench.copies")}</p>
      {copies.map((copy, index) => (
        <div key={index} className="rounded-lg bg-slate-50 p-2 text-sm text-slate-800">
          {copy.title ? <p className="font-medium">{copy.title}</p> : null}
          <p className="whitespace-pre-wrap">{copy.content}</p>
          {copy.hashtags && copy.hashtags.length > 0 ? (
            <p className="mt-1 text-xs text-slate-500">{copy.hashtags.map((tag) => `#${tag}`).join(" ")}</p>
          ) : null}
        </div>
      ))}
    </div>
  );
}

function GeneratedImages({ event }: { event: AgentToolEvent }) {
  const assets = event.result.completed_assets ?? [];
  if (assets.length === 0) return null;
  return (
    <div className="mt-2 flex flex-wrap gap-2">
      {assets.map((asset) => (
        <a key={asset.asset_id} href={asset.download_url} target="_blank" rel="noreferrer" className="block">
          <img
            src={asset.preview_url}
            alt={asset.size}
            className="h-32 w-32 rounded-lg border border-slate-200 object-cover"
          />
        </a>
      ))}
    </div>
  );
}

function AssetMatches({ event }: { event: AgentToolEvent }) {
  const matches = event.result.matches ?? [];
  if (matches.length === 0) return null;
  return (
    <div className="mt-2 flex flex-wrap gap-2">
      {matches.map((asset) => (
        <a key={asset.id} href={asset.download_url} target="_blank" rel="noreferrer" className="block w-24">
          <img
            src={asset.preview_url}
            alt={asset.title}
            className="h-24 w-24 rounded-lg border border-slate-200 object-cover"
          />
          <p className="mt-1 line-clamp-1 text-xs text-slate-500">{asset.title}</p>
        </a>
      ))}
    </div>
  );
}

function TemplateProfile({ event }: { event: AgentToolEvent }) {
  const { t } = useI18n();
  const profile = event.result.template_profile;
  if (!profile) return null;
  const rows: Array<[string, unknown]> = [
    ["layout", profile.layout],
    ["palette", Array.isArray(profile.palette) ? profile.palette.join(" / ") : profile.palette],
    ["typography", profile.typography],
    ["copy_slots", profile.copy_slots],
    ["mood", profile.mood],
  ];
  return (
    <div className="mt-2 rounded-xl border border-slate-200 bg-white p-3 text-sm">
      <p className="font-medium text-slate-800">{String(profile.summary ?? "")}</p>
      <dl className="mt-2 space-y-1 text-xs text-slate-600">
        {rows
          .filter(([, value]) => Boolean(value))
          .map(([key, value]) => (
            <div key={key} className="flex gap-2">
              <dt className="shrink-0 text-slate-400">{t(`workbench.profile.${key}` as TranslationKey)}</dt>
              <dd>{String(value)}</dd>
            </div>
          ))}
      </dl>
    </div>
  );
}

function RecommendationCards({
  event,
  onPick,
}: {
  event: AgentToolEvent;
  onPick: (message: string) => void;
}) {
  const { t } = useI18n();
  const recommendations = event.result.recommendations ?? [];
  if (recommendations.length === 0) {
    return event.result.message ? (
      <p className="mt-2 text-sm text-slate-500">{event.result.message}</p>
    ) : null;
  }
  return (
    <div className="mt-2 grid gap-2 sm:grid-cols-3">
      {recommendations.map((item: AgentDesignRecommendation) => (
        <div key={item.id} className="overflow-hidden rounded-xl border border-slate-200 bg-white">
          <a href={item.download_url} target="_blank" rel="noreferrer">
            <img src={item.preview_url} alt={item.title} className="h-28 w-full object-cover" />
          </a>
          <div className="space-y-1 p-2">
            <p className="text-xs font-medium text-slate-800">{item.title}</p>
            <p className="text-xs text-slate-500">{item.why}</p>
            <button
              type="button"
              onClick={() => onPick(t("workbench.pickTemplate", { title: item.title }))}
              className="w-full rounded-lg bg-slate-900 px-2 py-1 text-xs font-medium text-white hover:bg-slate-700"
            >
              {t("workbench.useThis")}
            </button>
          </div>
        </div>
      ))}
    </div>
  );
}

function CopyReportCard({ event }: { event: AgentToolEvent }) {
  const { t } = useI18n();
  const report: AgentCopyReport | undefined = event.result.report;
  if (!report) return null;
  return (
    <div className="mt-2 space-y-2 rounded-xl border border-slate-200 bg-white p-3 text-sm">
      {report.headline ? <p className="text-base font-semibold text-slate-900">{report.headline}</p> : null}
      {report.moments_caption ? (
        <p className="whitespace-pre-wrap text-slate-800">{report.moments_caption}</p>
      ) : null}
      {report.selling_points && report.selling_points.length > 0 ? (
        <ul className="list-inside list-disc text-xs text-slate-600">
          {report.selling_points.map((point, index) => (
            <li key={index}>{point}</li>
          ))}
        </ul>
      ) : null}
      {report.hashtags && report.hashtags.length > 0 ? (
        <p className="text-xs text-slate-500">{report.hashtags.map((tag) => `#${tag}`).join(" ")}</p>
      ) : null}
      {report.publishing_tips ? (
        <p className="rounded-lg bg-amber-50 p-2 text-xs text-amber-700">
          {t("workbench.report.tips")}: {report.publishing_tips}
        </p>
      ) : null}
    </div>
  );
}

function ToolEventCard({ event, onPick }: { event: AgentToolEvent; onPick: (message: string) => void }) {
  if (event.tool === "write_copy") return <CopyProposals event={event} />;
  if (event.tool === "generate_image" || event.tool === "edit_image") return <GeneratedImages event={event} />;
  if (event.tool === "search_assets") return <AssetMatches event={event} />;
  if (event.tool === "analyze_template") return <TemplateProfile event={event} />;
  if (event.tool === "recommend_designs") return <RecommendationCards event={event} onPick={onPick} />;
  if (event.tool === "write_copy_report") return <CopyReportCard event={event} />;
  return null;
}

export function WorkbenchPage() {
  const { t } = useI18n();
  const queryClient = useQueryClient();
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);
  const [draft, setDraft] = useState("");
  const scrollRef = useRef<HTMLDivElement | null>(null);

  const sessionsQuery = useQuery({ queryKey: ["agent-sessions"], queryFn: api.listAgentSessions });

  const detailQuery = useQuery({
    queryKey: ["agent-session", activeSessionId],
    queryFn: () => api.getAgentSession(activeSessionId as string),
    enabled: activeSessionId != null,
  });

  const detail: AgentSessionDetail | undefined = detailQuery.data;

  const imageSessionQuery = useQuery({
    queryKey: ["agent-image-session", detail?.image_session_id ?? "none"],
    queryFn: () => api.getImageSession(detail!.image_session_id as string),
    enabled: detail?.image_session_id != null,
    refetchInterval: 5000,
  });

  const pendingTasks = imageSessionQuery.data?.generation_tasks?.filter(
    (task) => task.status === "queued" || task.status === "running",
  );

  const sendMutation = useMutation({
    mutationFn: (content: string) => api.sendAgentMessage(activeSessionId as string, content),
    onSuccess: () => {
      void detailQuery.refetch();
      if (detail?.image_session_id) void imageSessionQuery.refetch();
    },
  });

  const assetsQuery = useQuery({ queryKey: ["agent-assets"], queryFn: () => api.listAgentAssets() });
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const uploadMutation = useMutation({
    mutationFn: (file: File) => api.uploadAgentAsset(file, "template"),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["agent-assets"] });
    },
  });

  const createMutation = useMutation({
    mutationFn: () => api.createAgentSession({}),
    onSuccess: (created) => {
      void queryClient.invalidateQueries({ queryKey: ["agent-sessions"] });
      setActiveSessionId(created.id);
    },
  });

  useEffect(() => {
    if (activeSessionId == null && sessionsQuery.data && sessionsQuery.data.items.length > 0) {
      setActiveSessionId(sessionsQuery.data.items[0].id);
    }
  }, [activeSessionId, sessionsQuery.data]);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [detail?.messages.length, sendMutation.isPending]);

  const submit = () => {
    const content = draft.trim();
    if (!content || activeSessionId == null || sendMutation.isPending) return;
    setDraft("");
    sendMutation.mutate(content);
  };

  const rounds: ImageSessionRound[] = imageSessionQuery.data?.rounds ?? [];

  return (
    <div className="flex min-h-screen flex-col bg-slate-100">
      <TopNav />
      <div className="mx-auto flex w-full max-w-6xl flex-1 gap-4 p-4">
        <aside className="hidden w-56 shrink-0 flex-col gap-2 md:flex">
          <div className="flex flex-col gap-2 overflow-y-auto">
            <button
              type="button"
              onClick={() => createMutation.mutate()}
              className="flex items-center justify-center gap-2 rounded-xl bg-slate-900 px-3 py-2 text-sm font-medium text-white hover:bg-slate-700"
            >
              <Plus className="h-4 w-4" />
              {t("workbench.newSession")}
            </button>
            <div className="flex flex-col gap-1">
              {(sessionsQuery.data?.items ?? []).map((session) => (
                <button
                  key={session.id}
                  type="button"
                  onClick={() => setActiveSessionId(session.id)}
                  className={`rounded-lg px-3 py-2 text-left text-sm ${
                    session.id === activeSessionId
                      ? "bg-white font-medium text-slate-900 shadow-sm"
                      : "text-slate-600 hover:bg-white/60"
                  }`}
                >
                  <span className="line-clamp-1">{session.title}</span>
                  <span className="text-xs text-slate-400">
                    {t(STAGE_LABEL_KEYS[session.stage] ?? "workbench.stage.clarify")}
                  </span>
                </button>
              ))}
            </div>
            <div className="mt-2 border-t border-slate-200 pt-2">
              <p className="mb-2 px-1 text-xs font-medium text-slate-400">{t("workbench.assets")}</p>
              <div className="grid grid-cols-3 gap-1">
                {(assetsQuery.data?.items ?? []).slice(0, 9).map((asset: AgentAssetEntry) => (
                  <a key={asset.id} href={asset.preview_url} target="_blank" rel="noreferrer" title={asset.title}>
                    <img
                      src={asset.thumbnail_url}
                      alt={asset.title}
                      className="h-16 w-full rounded-md border border-slate-200 object-cover"
                    />
                  </a>
                ))}
              </div>
            </div>
          </div>
        </aside>

        <main className="flex min-w-0 flex-1 flex-col rounded-2xl bg-slate-50 shadow-sm">
          <header className="border-b border-slate-200 px-4 py-3">
            <h1 className="text-base font-semibold text-slate-900">{t("workbench.title")}</h1>
            <p className="text-xs text-slate-500">{t("workbench.subtitle")}</p>
          </header>

          <div ref={scrollRef} className="flex-1 space-y-3 overflow-y-auto p-4">
            {(detail?.messages ?? [])
              .filter((message) => message.role !== "tool")
              .map((message) => (
                <div
                  key={message.id}
                  className={`flex ${message.role === "user" ? "justify-end" : "justify-start"}`}
                >
                  <div
                    className={`max-w-[80%] whitespace-pre-wrap rounded-2xl px-3 py-2 text-sm ${
                      message.role === "user"
                        ? "bg-slate-900 text-white"
                        : "border border-slate-200 bg-white text-slate-800"
                    }`}
                  >
                    {message.content}
                  </div>
                </div>
              ))}
            {sendMutation.isPending ? (
              <div className="flex items-center gap-2 text-sm text-slate-500">
                <Loader2 className="h-4 w-4 animate-spin" />
                {t("workbench.working")}
              </div>
            ) : null}
            {(sendMutation.data?.tool_events ?? []).map((event, index) => (
              <ToolEventCard
                key={`event-${index}`}
                event={event}
                onPick={(message: string) => sendMutation.mutate(message)}
              />
            ))}
            {rounds.length > 0 ? (
              <div className="rounded-xl border border-slate-200 bg-white p-3">
                <p className="mb-2 text-xs font-medium text-slate-500">{t("workbench.latestOutputs")}</p>
                <div className="flex flex-wrap gap-2">
                  {rounds
                    .slice(-4)
                    .reverse()
                    .map((round) =>
                      round.generated_asset ? (
                        <a
                          key={round.id}
                          href={round.generated_asset.download_url}
                          target="_blank"
                          rel="noreferrer"
                          className="group relative block"
                          title={t("workbench.download")}
                        >
                          <img
                            src={round.generated_asset.thumbnail_url}
                            alt={round.size}
                            className="h-28 w-28 rounded-lg border border-slate-200 object-cover"
                          />
                        </a>
                      ) : null,
                    )}
                </div>
                {pendingTasks && pendingTasks.length > 0 ? (
                  <p className="mt-2 flex items-center gap-1 text-xs text-amber-600">
                    <Loader2 className="h-3 w-3 animate-spin" />
                    {t("workbench.generating")}
                  </p>
                ) : null}
              </div>
            ) : null}
            {detail && detail.messages.filter((message) => message.role === "user").length === 0 ? (
              <p className="py-10 text-center text-sm text-slate-400">{t("workbench.emptyHint")}</p>
            ) : null}
          </div>

          <footer className="border-t border-slate-200 p-3">
            <div className="flex items-end gap-2">
              <input
                ref={fileInputRef}
                type="file"
                accept="image/png,image/jpeg,image/webp"
                className="hidden"
                onChange={(event) => {
                  const file = event.target.files?.[0];
                  if (file) uploadMutation.mutate(file);
                  event.target.value = "";
                }}
              />
              <button
                type="button"
                onClick={() => fileInputRef.current?.click()}
                disabled={uploadMutation.isPending}
                className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl border border-slate-200 bg-white text-slate-500 hover:border-slate-400 disabled:opacity-40"
                title={t("workbench.upload")}
              >
                {uploadMutation.isPending ? (
                  <Loader2 className="h-4 w-4 animate-spin" />
                ) : (
                  <Paperclip className="h-4 w-4" />
                )}
              </button>
              <textarea
                value={draft}
                onChange={(event) => setDraft(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" && !event.shiftKey) {
                    event.preventDefault();
                    submit();
                  }
                }}
                rows={2}
                placeholder={t("workbench.inputPlaceholder")}
                className="flex-1 resize-none rounded-xl border border-slate-200 bg-white px-3 py-2 text-sm outline-none focus:border-slate-400"
              />
              <button
                type="button"
                onClick={submit}
                disabled={!draft.trim() || activeSessionId == null || sendMutation.isPending}
                className="flex items-center gap-1 rounded-xl bg-slate-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-40"
              >
                <Send className="h-4 w-4" />
                {t("workbench.send")}
              </button>
            </div>
            {uploadMutation.data ? (
              <p className="mt-2 line-clamp-1 text-xs text-emerald-600">
                {t("workbench.uploaded")}: {uploadMutation.data.title}
              </p>
            ) : null}
          </footer>
        </main>
      </div>
    </div>
  );
}
