import type { KeyboardEvent } from "react";
import { Loader2, Send, Sparkles } from "lucide-react";

import type { ImageChatTranslate } from "./display";

interface ImageChatComposerProps {
  draft: string;
  onDraftChange: (value: string) => void;
  onDiscuss: () => void;
  onGenerate: () => void;
  onKeyDown: (event: KeyboardEvent<HTMLTextAreaElement>) => void;
  discussionBusy: boolean;
  generationBusy: boolean;
  disabled: boolean;
  generationCount: number;
  className?: string;
  t: ImageChatTranslate;
}

/** The only surface that can submit a discussion or image generation request. */
export function ImageChatComposer({
  draft,
  onDraftChange,
  onDiscuss,
  onGenerate,
  onKeyDown,
  discussionBusy,
  generationBusy,
  disabled,
  generationCount,
  className,
  t,
}: ImageChatComposerProps) {
  const busy = discussionBusy || generationBusy;
  const hasDraft = Boolean(draft.trim());

  return (
    <div className={`mt-3 rounded-2xl border border-slate-200 bg-white p-2.5 shadow-sm dark:border-slate-700/80 dark:bg-[#0f1726] ${className ?? ""}`}>
      <textarea
        value={draft}
        onChange={(event) => onDraftChange(event.target.value)}
        onKeyDown={onKeyDown}
        rows={2}
        maxLength={4000}
        placeholder={t("chat.discussionPlaceholder")}
        aria-label={t("chat.prompt")}
        className="w-full resize-none rounded-xl border border-transparent bg-slate-50 px-3 py-2.5 text-sm leading-6 text-slate-900 outline-none transition-colors placeholder:text-slate-400 focus:border-indigo-300 focus:bg-white focus:ring-2 focus:ring-indigo-100 dark:bg-slate-950/70 dark:text-slate-100 dark:placeholder:text-slate-500 dark:focus:border-violet-400 dark:focus:bg-slate-950"
      />
      <div className="mt-2 flex items-center justify-between gap-2">
        <span className="min-w-0 truncate px-1 text-[11px] text-slate-400 dark:text-slate-500">
          {t("chat.composerHint")}
        </span>
        <div className="flex shrink-0 items-center gap-2">
          <button
            type="button"
            onClick={onDiscuss}
            disabled={disabled || !hasDraft || busy}
            title={t("chat.discuss")}
            aria-label={t("chat.discuss")}
            className="inline-flex h-10 items-center justify-center gap-1.5 rounded-xl border border-slate-200 bg-white px-3 text-sm font-semibold text-slate-700 transition-colors hover:border-indigo-300 hover:text-indigo-700 disabled:cursor-not-allowed disabled:opacity-50 dark:border-slate-700 dark:bg-slate-950/70 dark:text-slate-200 dark:hover:border-violet-400/55 dark:hover:text-violet-100"
          >
            {discussionBusy ? <Loader2 size={15} className="animate-spin" /> : <Send size={15} />}
            <span>{discussionBusy ? t("chat.discussing") : t("chat.discuss")}</span>
          </button>
          <button
            type="button"
            onClick={onGenerate}
            disabled={disabled || !hasDraft || busy}
            title={t("chat.startGenerate")}
            aria-label={t("chat.startGenerate")}
            className="inline-flex h-10 items-center justify-center gap-1.5 rounded-xl bg-indigo-600 px-3 text-sm font-semibold text-white shadow-sm shadow-indigo-500/20 transition-colors hover:bg-indigo-500 disabled:cursor-not-allowed disabled:opacity-50 dark:bg-violet-600 dark:shadow-violet-900/35 dark:ring-1 dark:ring-violet-300/35"
          >
            {generationBusy ? <Loader2 size={15} className="animate-spin" /> : <Sparkles size={15} />}
            <span>{generationCount > 1 ? t("chat.startGenerateCount", { count: generationCount }) : t("chat.startGenerate")}</span>
          </button>
        </div>
      </div>
    </div>
  );
}
