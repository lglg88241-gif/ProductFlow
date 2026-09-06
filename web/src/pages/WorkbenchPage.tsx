import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2, Plus, Send } from "lucide-react";

import { TopNav } from "../components/TopNav";
import { api } from "../lib/api";
import type { AgentSessionDetail, AgentToolEvent } from "../lib/agentTypes";
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

function ToolEventCard({ event }: { event: AgentToolEvent }) {
  if (event.tool === "write_copy") return <CopyProposals event={event} />;
  if (event.tool === "generate_image" || event.tool === "edit_image") return <GeneratedImages event={event} />;
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
          <button
            type="button"
            onClick={() => createMutation.mutate()}
            className="flex items-center justify-center gap-2 rounded-xl bg-slate-900 px-3 py-2 text-sm font-medium text-white hover:bg-slate-700"
          >
            <Plus className="h-4 w-4" />
            {t("workbench.newSession")}
          </button>
          <div className="flex flex-col gap-1 overflow-y-auto">
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
              <ToolEventCard key={`event-${index}`} event={event} />
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
          </footer>
        </main>
      </div>
    </div>
  );
}
