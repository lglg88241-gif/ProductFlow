import { type CSSProperties, useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Image as ImageIcon, Loader2 } from "lucide-react";
import { useNavigate } from "react-router-dom";

import { GalleryImagePreviewDialog } from "../components/GalleryImagePreviewDialog";
import { TopNav } from "../components/TopNav";
import { api } from "../lib/api";
import { formatDateTime } from "../lib/format";
import { useI18n } from "../lib/preferences";
import type { GalleryEntry } from "../lib/types";
import { galleryEntrySizeLabel, galleryTileLayout } from "./gallery/helpers";

function metadataRows(entry: GalleryEntry, locale: ReturnType<typeof useI18n>["locale"], t: ReturnType<typeof useI18n>["t"]) {
  return [
    ["gallery.meta.size", galleryEntrySizeLabel(entry, locale)],
    ["gallery.meta.model", [entry.provider_name, entry.model_name].filter(Boolean).join(" / ") || t("common.unknown")],
    ["gallery.meta.session", entry.image_session_title],
    [
      "gallery.meta.candidate",
      entry.candidate_index != null && entry.candidate_count != null
        ? `${entry.candidate_index}/${entry.candidate_count}`
        : t("common.unknown"),
    ],
    ["gallery.meta.savedAt", formatDateTime(entry.created_at, locale)],
  ] as const;
}

export function GalleryPage() {
  const { locale, t } = useI18n();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [previewEntry, setPreviewEntry] = useState<GalleryEntry | null>(null);
  const [gridContentWidth, setGridContentWidth] = useState<number | null>(null);
  const [isDesktopGrid, setIsDesktopGrid] = useState(false);
  const gridRef = useRef<HTMLDivElement | null>(null);

  const galleryQuery = useQuery({
    queryKey: ["gallery"],
    queryFn: api.listGalleryEntries,
  });
  const entries = galleryQuery.data?.items ?? [];

  useEffect(() => {
    const updateGridMetrics = () => {
      setIsDesktopGrid(window.matchMedia("(min-width: 1024px)").matches);
      if (gridRef.current) {
        setGridContentWidth(gridRef.current.clientWidth);
      }
    };

    updateGridMetrics();
    window.addEventListener("resize", updateGridMetrics);

    const gridElement = gridRef.current;
    const resizeObserver =
      typeof ResizeObserver === "undefined" || !gridElement ? null : new ResizeObserver(updateGridMetrics);
    if (gridElement) {
      resizeObserver?.observe(gridElement);
    }

    return () => {
      window.removeEventListener("resize", updateGridMetrics);
      resizeObserver?.disconnect();
    };
  }, []);

  const logoutMutation = useMutation({
    mutationFn: api.destroySession,
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["session"] });
      navigate("/login", { replace: true });
    },
  });

  return (
    <div className="min-h-screen bg-slate-50 text-slate-950 dark:bg-[#060a12] dark:text-slate-100">
      <TopNav breadcrumbs={t("gallery.title")} onHome={() => navigate("/products")} onLogout={() => logoutMutation.mutate()} />

      <main className="mx-auto w-full max-w-[1480px] px-4 py-5 pb-28 sm:px-6 lg:px-8 lg:py-8">
        {galleryQuery.isLoading ? (
          <div className="flex min-h-[calc(100svh-10rem)] items-center justify-center text-slate-500">
            <Loader2 size={28} className="animate-spin" />
          </div>
        ) : galleryQuery.isError ? (
          <div className="flex min-h-[calc(100svh-10rem)] items-center justify-center px-6 text-sm font-medium text-red-700 dark:text-red-300">
            {t("gallery.loadFailed")}
          </div>
        ) : entries.length ? (
          <section aria-labelledby="gallery-heading">
            <header className="mb-6 flex flex-wrap items-end justify-between gap-4 border-b border-slate-200 pb-5 dark:border-slate-800">
              <div className="min-w-0">
                <div className="text-xs font-semibold uppercase tracking-[0.16em] text-indigo-600 dark:text-violet-300">
                  {t("gallery.feed")}
                </div>
                <h1 id="gallery-heading" className="mt-2 text-2xl font-semibold tracking-tight sm:text-3xl">
                  {t("gallery.title")}
                </h1>
                <p className="mt-2 max-w-2xl text-sm leading-6 text-slate-500 dark:text-slate-400">
                  {t("gallery.description")}
                </p>
              </div>
              <div className="text-sm font-medium text-slate-500 dark:text-slate-400">
                {t("gallery.count", { count: entries.length })}
              </div>
            </header>

            <div
              ref={gridRef}
              className="grid grid-flow-dense grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-12 lg:auto-rows-[8px]"
            >
              {entries.map((entry, index) => {
                const tileLayout = galleryTileLayout(entry, index, gridContentWidth ?? undefined);
                const tileStyle: CSSProperties = {
                  aspectRatio: tileLayout.aspectRatio,
                  ...(isDesktopGrid ? { gridRowEnd: `span ${tileLayout.rowSpan}` } : {}),
                };
                return (
                  <button
                    key={entry.id}
                    type="button"
                    onClick={() => setPreviewEntry(entry)}
                    className={`group relative min-w-0 overflow-hidden rounded-md border border-slate-200 bg-slate-900 text-left shadow-sm transition duration-200 hover:border-indigo-300 hover:shadow-lg focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500 dark:border-slate-800 dark:hover:border-violet-400/60 ${tileLayout.className}`}
                    style={tileStyle}
                  >
                    <div className="relative h-full overflow-hidden bg-slate-950">
                      <img
                        src={api.toApiUrl(entry.image.thumbnail_url)}
                        alt={entry.prompt ?? entry.image.original_filename}
                        loading="lazy"
                        decoding="async"
                        className="h-full w-full object-contain transition duration-300 group-hover:scale-[1.01]"
                      />
                      <div className="absolute inset-0 bg-gradient-to-t from-black/82 via-black/5 to-transparent opacity-85 transition-opacity group-hover:opacity-95" />
                      <div className="absolute inset-x-0 bottom-0 p-4 text-white">
                        <div className="line-clamp-2 text-sm font-semibold leading-5">
                          {entry.prompt ?? entry.image.original_filename}
                        </div>
                        <div className="mt-2 flex flex-wrap items-center gap-2 text-[11px] font-semibold text-white/70">
                          <span>{galleryEntrySizeLabel(entry, locale)}</span>
                          <span>{formatDateTime(entry.created_at, locale)}</span>
                        </div>
                      </div>
                    </div>
                  </button>
                );
              })}
            </div>
          </section>
        ) : (
          <div className="flex min-h-[calc(100svh-10rem)] flex-col items-center justify-center px-6 text-sm text-slate-600 dark:text-slate-400">
            <ImageIcon size={30} className="mb-4 text-indigo-500" />
            <div className="text-xl font-semibold text-slate-950 dark:text-white">{t("gallery.title")}</div>
            <div className="mt-4 text-center">{t("gallery.empty")}</div>
          </div>
        )}
      </main>

      {previewEntry ? (
        <GalleryImagePreviewDialog
          ariaLabel={t("gallery.previewLabel")}
          imageUrl={api.toApiUrl(previewEntry.image.preview_url)}
          imageAlt={previewEntry.prompt ?? previewEntry.image.original_filename}
          title={t("gallery.prompt")}
          subtitle={previewEntry.image.original_filename}
          body={previewEntry.prompt ?? t("gallery.noPrompt")}
          metadataRows={metadataRows(previewEntry, locale, t).map(([label, value]) => ({ label: t(label), value }))}
          providerNotes={previewEntry.provider_notes}
          providerNotesTitle={t("gallery.providerNotes")}
          downloadUrl={previewEntry.image.download_url}
          downloadLabel={t("gallery.download")}
          closeLabel={t("gallery.closePreview")}
          onClose={() => setPreviewEntry(null)}
        />
      ) : null}
    </div>
  );
}
