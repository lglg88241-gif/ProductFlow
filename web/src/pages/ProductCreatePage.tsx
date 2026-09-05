import { useEffect, useMemo, useRef, useState, type ChangeEvent, type ReactNode } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import {
  ArrowLeft,
  CalendarDays,
  Check,
  ImagePlus,
  Loader2,
  Sparkles,
  Upload,
  X,
} from "lucide-react";
import { useNavigate } from "react-router-dom";

import { ImageDropZone } from "../components/ImageDropZone";
import { TopNav } from "../components/TopNav";
import { api, ApiError } from "../lib/api";
import { localizeCanvasTemplateSummary } from "../lib/canvasTemplateLocalization";
import type { TranslationKey } from "../lib/i18n";
import { useI18n } from "../lib/preferences";
import {
  appendExtractedCopy,
  EMPTY_POSTER_COPY_DRAFT,
  normalizeProductPriceForApi,
  serializePosterSourceNote,
  type PosterCopyDraft,
} from "./product-create/copyDraft";

const MAX_REFERENCE_FILES = 6;
const MOMENTS_TEMPLATE_KEYS = [
  "moments-beauty-clean-v1",
  "moments-beauty-neon-v1",
  "moments-beauty-luxe-v1",
] as const;

type MomentsTemplateKey = (typeof MOMENTS_TEMPLATE_KEYS)[number];

interface TemplateOption {
  key: MomentsTemplateKey;
  title: string;
  description: string;
  previewSrc: string;
  nodeCount: number;
  outputCount: number;
}

const TEMPLATE_FALLBACKS: Record<MomentsTemplateKey, { titleKey: TranslationKey; descriptionKey: TranslationKey; previewSrc: string }> = {
  "moments-beauty-clean-v1": {
    titleKey: "create.template.cleanTitle",
    descriptionKey: "create.template.cleanDescription",
    previewSrc: "/templates/emerald-model-editorial.jpg",
  },
  "moments-beauty-neon-v1": {
    titleKey: "create.template.neonTitle",
    descriptionKey: "create.template.neonDescription",
    previewSrc: "/templates/black-pink-impact-type.jpg",
  },
  "moments-beauty-luxe-v1": {
    titleKey: "create.template.luxeTitle",
    descriptionKey: "create.template.luxeDescription",
    previewSrc: "/templates/purple-black-event-report.jpg",
  },
};

const SCENE_OPTIONS = [
  { value: "美容护肤", labelKey: "create.scene.beauty" },
  { value: "轻体管理", labelKey: "create.scene.body" },
  { value: "门店活动", labelKey: "create.scene.store" },
  { value: "项目体验", labelKey: "create.scene.experience" },
  { value: "其他", labelKey: "create.scene.other" },
] as const satisfies ReadonlyArray<{ value: string; labelKey: TranslationKey }>;

const inputClassName =
  "w-full min-w-0 rounded-md border border-slate-200 bg-white px-3 py-2.5 text-sm text-slate-950 outline-none transition focus:border-indigo-500 focus:ring-2 focus:ring-indigo-500/15 dark:border-slate-700 dark:bg-slate-950/70 dark:text-slate-100 dark:focus:border-violet-400 dark:focus:ring-violet-400/15";
const textAreaClassName = `${inputClassName} resize-y leading-6`;

export function ProductCreatePage() {
  const { locale, t } = useI18n();
  const navigate = useNavigate();
  const copyInputRef = useRef<HTMLInputElement | null>(null);
  const [projectName, setProjectName] = useState("");
  const [scene, setScene] = useState("");
  const [mainImage, setMainImage] = useState<File | null>(null);
  const [referenceFiles, setReferenceFiles] = useState<File[]>([]);
  const [draft, setDraft] = useState<PosterCopyDraft>(EMPTY_POSTER_COPY_DRAFT);
  const [selectedTemplateKey, setSelectedTemplateKey] = useState<MomentsTemplateKey>(
    "moments-beauty-clean-v1",
  );
  const [error, setError] = useState("");
  const [copyNotice, setCopyNotice] = useState("");

  const templatesQuery = useQuery({
    queryKey: ["canvas-templates"],
    queryFn: api.listCanvasTemplates,
  });

  const templateOptions = useMemo<TemplateOption[]>(() => {
    const sourceByKey = new Map(
      (templatesQuery.data?.items ?? [])
        .filter((template) => MOMENTS_TEMPLATE_KEYS.includes(template.key as MomentsTemplateKey))
        .map((template) => [template.key, localizeCanvasTemplateSummary(template, locale)]),
    );
    return MOMENTS_TEMPLATE_KEYS.map((key) => {
      const fallback = TEMPLATE_FALLBACKS[key];
      const source = sourceByKey.get(key);
      return {
        key,
        title: source?.title ?? t(fallback.titleKey),
        description: source?.description ?? t(fallback.descriptionKey),
        previewSrc: fallback.previewSrc,
        nodeCount: source?.preview_nodes.length ?? 5,
        outputCount: source?.output_slots.length ?? 1,
      };
    });
  }, [locale, t, templatesQuery.data?.items]);

  const selectedTemplate =
    templateOptions.find((template) => template.key === selectedTemplateKey) ?? templateOptions[0];
  const mainImagePreviewUrl = useObjectUrl(mainImage);
  const referencePreviews = useObjectUrls(referenceFiles);
  const selectedSceneLabel = SCENE_OPTIONS.find((option) => option.value === scene)?.value ?? scene;

  const extractCopyMutation = useMutation({
    mutationFn: api.extractCopyInput,
    onSuccess: (result) => {
      setDraft((current) => ({ ...current, rawCopy: appendExtractedCopy(current.rawCopy, result.text) }));
      setCopyNotice(result.source_kind === "image_ocr" ? t("create.copyOcrDone") : t("create.copyFileDone"));
      setError("");
    },
    onError: (mutationError) => {
      setCopyNotice("");
      setError(mutationError instanceof ApiError ? mutationError.detail : t("create.copyExtractFailed"));
    },
  });

  const createProductMutation = useMutation({
    mutationFn: () => {
      if (!mainImage) {
        throw new Error(t("create.requiredImage"));
      }
      return api.createProduct({
        name: projectName.trim(),
        category: scene || undefined,
        price: normalizeProductPriceForApi(draft.price),
        source_note: serializePosterSourceNote({ projectName, scene: selectedSceneLabel, draft }),
        canvas_template_key: selectedTemplate.key,
        template_language: locale,
        file: mainImage,
        referenceFiles,
      });
    },
    onSuccess: (product) => navigate(`/products/${product.id}`),
    onError: (mutationError) => {
      setError(mutationError instanceof ApiError ? mutationError.detail : mutationError instanceof Error ? mutationError.message : t("create.failed"));
    },
  });

  const updateDraft = <K extends keyof PosterCopyDraft>(key: K, value: PosterCopyDraft[K]) => {
    setDraft((current) => ({ ...current, [key]: value }));
  };

  const updateSellingPoint = (index: number, value: string) => {
    setDraft((current) => {
      const sellingPoints = [...current.sellingPoints];
      sellingPoints[index] = value;
      return { ...current, sellingPoints };
    });
  };

  const handleMainImageFiles = (files: File[]) => {
    setMainImage(files[0] ?? null);
    setError("");
  };

  const handleReferenceFiles = (files: File[]) => {
    setReferenceFiles((current) => [...current, ...files].slice(0, MAX_REFERENCE_FILES));
    setError("");
  };

  const handleCopyFileChange = (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    event.currentTarget.value = "";
    if (file) {
      extractCopyMutation.mutate(file);
    }
  };

  const handleSubmit = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setError("");
    createProductMutation.mutate();
  };

  return (
    <div className="min-h-screen bg-slate-50 pb-[calc(5rem+env(safe-area-inset-bottom))] text-slate-950 dark:bg-[#060a12] dark:text-slate-100 lg:pb-8">
      <TopNav breadcrumbs={t("create.title")} onHome={() => navigate("/products")} />
      <main className="mx-auto w-full max-w-[1480px] px-4 py-5 sm:px-6 lg:px-8 lg:py-8">
        <header className="mb-6 flex flex-wrap items-end justify-between gap-4 border-b border-slate-200 pb-5 dark:border-slate-800">
          <div className="min-w-0">
            <div className="mb-2 flex items-center gap-2 text-xs font-semibold uppercase tracking-[0.16em] text-indigo-600 dark:text-violet-300">
              <Sparkles size={14} aria-hidden="true" />
              {t("create.eyebrow")}
            </div>
            <h1 className="text-2xl font-semibold tracking-tight sm:text-3xl">{t("create.title")}</h1>
            <p className="mt-2 max-w-3xl text-sm leading-6 text-slate-500 dark:text-slate-400">{t("create.description")}</p>
          </div>
          <button
            type="button"
            onClick={() => navigate("/products")}
            className="inline-flex min-h-10 items-center gap-2 rounded-md border border-slate-200 bg-white px-3 text-sm font-semibold text-slate-600 transition hover:border-slate-300 hover:text-slate-950 dark:border-slate-700 dark:bg-slate-950/70 dark:text-slate-300 dark:hover:border-slate-500 dark:hover:text-white"
          >
            <ArrowLeft size={16} aria-hidden="true" />
            {t("create.back")}
          </button>
        </header>

        {error ? (
          <div className="mb-5 flex items-start gap-2 border-l-2 border-red-500 bg-red-50 px-3 py-2.5 text-sm text-red-700 dark:bg-red-500/10 dark:text-red-200">
            <span>{error}</span>
          </div>
        ) : null}

        <form onSubmit={handleSubmit} className="grid gap-8 xl:grid-cols-[minmax(0,1fr)_360px]">
          <div className="min-w-0 space-y-8">
            <section aria-labelledby="project-copy-title" className="border-b border-slate-200 pb-8 dark:border-slate-800">
              <SectionHeading id="project-copy-title" title={t("create.copySection")} meta={t("create.copySectionMeta")} />
              <div className="mt-5 grid gap-x-5 gap-y-4 sm:grid-cols-2">
                <Field label={t("create.projectName")} required>
                  <input required maxLength={80} className={inputClassName} value={projectName} onChange={(event) => setProjectName(event.target.value)} placeholder={t("create.projectNamePlaceholder")} />
                </Field>
                <Field label={t("create.scene")}>
                  <select className={inputClassName} value={scene} onChange={(event) => setScene(event.target.value)}>
                    <option value="">{t("create.scenePlaceholder")}</option>
                    {SCENE_OPTIONS.map((option) => <option key={option.value} value={option.value}>{t(option.labelKey)}</option>)}
                  </select>
                </Field>
                <Field label={t("create.brand")}>
                  <input className={inputClassName} value={draft.brand} onChange={(event) => updateDraft("brand", event.target.value)} placeholder={t("create.brandPlaceholder")} />
                </Field>
                <Field label={t("create.subject")}>
                  <input className={inputClassName} value={draft.subject} onChange={(event) => updateDraft("subject", event.target.value)} placeholder={t("create.subjectPlaceholder")} />
                </Field>
                <Field label={t("create.price")}>
                  <input className={inputClassName} value={draft.price} onChange={(event) => updateDraft("price", event.target.value)} placeholder={t("create.pricePlaceholder")} />
                </Field>
                <Field label={t("create.date")}>
                  <div className="relative">
                    <CalendarDays size={16} className="pointer-events-none absolute left-3 top-3 text-slate-400" aria-hidden="true" />
                    <input className={`${inputClassName} pl-9`} value={draft.date} onChange={(event) => updateDraft("date", event.target.value)} placeholder={t("create.datePlaceholder")} />
                  </div>
                </Field>
                <Field label={t("create.headline")} required>
                  <input required className={inputClassName} value={draft.headline} onChange={(event) => updateDraft("headline", event.target.value)} placeholder={t("create.headlinePlaceholder")} />
                </Field>
                <Field label={t("create.subheadline")}>
                  <input className={inputClassName} value={draft.subheadline} onChange={(event) => updateDraft("subheadline", event.target.value)} placeholder={t("create.subheadlinePlaceholder")} />
                </Field>
              </div>

              <div className="mt-5">
                <div className="mb-2 flex items-center justify-between gap-3">
                  <label className="text-sm font-semibold text-slate-800 dark:text-slate-200">{t("create.sellingPoints")}</label>
                  <span className="text-xs text-slate-400">{t("create.sellingPointsCount")}</span>
                </div>
                <div className="grid gap-3 sm:grid-cols-2">
                  {draft.sellingPoints.map((point, index) => (
                    <input key={index} className={inputClassName} value={point} onChange={(event) => updateSellingPoint(index, event.target.value)} placeholder={t("create.sellingPointPlaceholder", { index: index + 1 })} />
                  ))}
                </div>
              </div>

              <div className="mt-5 grid gap-5 lg:grid-cols-[minmax(0,0.9fr)_minmax(0,1.1fr)]">
                <Field label={t("create.supplement")}>
                  <textarea rows={5} className={textAreaClassName} value={draft.supplement} onChange={(event) => updateDraft("supplement", event.target.value)} placeholder={t("create.supplementPlaceholder")} />
                </Field>
                <Field label={t("create.rawCopy")} meta={copyNotice}>
                  <textarea rows={7} className={textAreaClassName} value={draft.rawCopy} onChange={(event) => updateDraft("rawCopy", event.target.value)} placeholder={t("create.rawCopyPlaceholder")} />
                  <div className="mt-2 flex flex-wrap items-center gap-2">
                    <label className="inline-flex min-h-9 cursor-pointer items-center gap-2 rounded-md border border-slate-200 bg-white px-3 text-xs font-semibold text-slate-700 transition hover:border-indigo-300 hover:text-indigo-700 dark:border-slate-700 dark:bg-slate-950/70 dark:text-slate-300 dark:hover:border-violet-400 dark:hover:text-violet-200">
                      {extractCopyMutation.isPending ? <Loader2 size={14} className="animate-spin" aria-hidden="true" /> : <Upload size={14} aria-hidden="true" />}
                      {t("create.uploadCopy")}
                      <input ref={copyInputRef} type="file" accept=".txt,.md,.markdown,text/plain,text/markdown,image/png,image/jpeg,image/webp" className="sr-only" onChange={handleCopyFileChange} disabled={extractCopyMutation.isPending} />
                    </label>
                    {extractCopyMutation.isPending ? <span className="text-xs text-slate-500 dark:text-slate-400">{t("create.copyExtracting")}</span> : null}
                  </div>
                </Field>
              </div>
            </section>

            <section aria-labelledby="materials-title" className="border-b border-slate-200 pb-8 dark:border-slate-800">
              <SectionHeading id="materials-title" title={t("create.materialsSection")} meta={t("create.materialsSectionMeta")} />
              <div className="mt-5 grid gap-5 lg:grid-cols-[minmax(0,1.15fr)_minmax(0,0.85fr)]">
                <div>
                  <Field label={t("create.mainVisual")} required meta={mainImage?.name}>
                    <ImageDropZone
                      accept="image/png,image/jpeg,image/webp"
                      ariaLabel={t("create.mainVisualAria")}
                      className="group relative flex min-h-[260px] cursor-pointer flex-col items-center justify-center overflow-hidden rounded-md border border-dashed border-slate-300 bg-white text-center transition hover:border-indigo-400 dark:border-slate-700 dark:bg-slate-950/55 dark:hover:border-violet-400"
                      onFiles={handleMainImageFiles}
                    >
                      {({ isDragging }) => mainImagePreviewUrl ? (
                        <>
                          <img src={mainImagePreviewUrl} alt={t("create.mainVisualPreviewAlt")} className="absolute inset-0 h-full w-full object-contain p-3" />
                          <span className="absolute bottom-3 left-3 inline-flex items-center gap-1 rounded bg-slate-950/75 px-2 py-1 text-xs font-medium text-white">{t("create.replaceImage")}</span>
                        </>
                      ) : (
                        <>
                          <ImagePlus size={30} className="mb-3 text-slate-400" aria-hidden="true" />
                          <span className="text-sm font-semibold text-slate-700 dark:text-slate-200">{isDragging ? t("create.dropImage") : t("create.mainVisualEmpty")}</span>
                          <span className="mt-1 text-xs text-slate-400">{t("create.imageTypes")}</span>
                        </>
                      )}
                    </ImageDropZone>
                  </Field>
                </div>
                <div>
                  <Field label={t("create.referenceMaterials")} meta={`${referenceFiles.length} / ${MAX_REFERENCE_FILES}`}>
                    <ImageDropZone
                      multiple
                      accept="image/png,image/jpeg,image/webp"
                      ariaLabel={t("create.referenceMaterialsAria")}
                      className="flex min-h-[178px] cursor-pointer flex-col items-center justify-center rounded-md border border-dashed border-slate-300 bg-white px-4 text-center transition hover:border-indigo-400 dark:border-slate-700 dark:bg-slate-950/55 dark:hover:border-violet-400"
                      onFiles={handleReferenceFiles}
                    >
                      {({ isDragging }) => (
                        <>
                          <Upload size={25} className="mb-2 text-slate-400" aria-hidden="true" />
                          <span className="text-sm font-semibold text-slate-700 dark:text-slate-200">{isDragging ? t("create.dropMaterials") : t("create.addMaterials")}</span>
                          <span className="mt-1 text-xs text-slate-400">{t("create.materialTypes")}</span>
                        </>
                      )}
                    </ImageDropZone>
                    {referencePreviews.length ? (
                      <div className="mt-3 grid grid-cols-3 gap-2 sm:grid-cols-4">
                        {referencePreviews.map(({ file, url }, index) => (
                          <div key={`${file.name}-${index}`} className="group relative aspect-square overflow-hidden rounded border border-slate-200 bg-slate-100 dark:border-slate-700 dark:bg-slate-900">
                            <img src={url} alt={file.name} className="h-full w-full object-cover" />
                            <button type="button" onClick={() => setReferenceFiles((current) => current.filter((_, itemIndex) => itemIndex !== index))} className="absolute right-1 top-1 inline-flex h-6 w-6 items-center justify-center rounded-full bg-slate-950/70 text-white opacity-0 transition group-hover:opacity-100 focus:opacity-100" aria-label={t("create.removeMaterial", { name: file.name })} title={t("create.removeMaterial", { name: file.name })}>
                              <X size={13} aria-hidden="true" />
                            </button>
                          </div>
                        ))}
                      </div>
                    ) : null}
                  </Field>
                </div>
              </div>
            </section>

            <section className="flex flex-col gap-4 border-b border-slate-200 pb-8 dark:border-slate-800 sm:flex-row sm:items-center sm:justify-between">
              <div>
                <div className="text-sm font-semibold text-slate-900 dark:text-white">{t("create.submitReady")}</div>
                <div className="mt-1 text-xs text-slate-500 dark:text-slate-400">{t("create.submitMeta")}</div>
              </div>
              <div className="flex flex-wrap gap-2 sm:justify-end">
                <button type="button" onClick={() => navigate("/products")} className="inline-flex min-h-11 items-center gap-2 rounded-md border border-slate-200 bg-white px-4 text-sm font-semibold text-slate-700 transition hover:border-slate-300 dark:border-slate-700 dark:bg-slate-950/70 dark:text-slate-300 dark:hover:border-slate-500">
                  <ArrowLeft size={16} aria-hidden="true" />
                  {t("create.cancel")}
                </button>
                <button type="submit" disabled={createProductMutation.isPending || !selectedTemplate} className="inline-flex min-h-11 items-center justify-center gap-2 rounded-md bg-indigo-600 px-5 text-sm font-semibold text-white shadow-sm transition hover:bg-indigo-500 disabled:cursor-not-allowed disabled:opacity-55 dark:bg-violet-600 dark:hover:bg-violet-500">
                  {createProductMutation.isPending ? <Loader2 size={16} className="animate-spin" aria-hidden="true" /> : <Sparkles size={16} aria-hidden="true" />}
                  {t("create.submit")}
                </button>
              </div>
            </section>
          </div>

          <aside className="min-w-0 self-start xl:sticky xl:top-5">
            <section aria-labelledby="template-title" className="border border-slate-200 bg-white p-4 dark:border-slate-800 dark:bg-[#0f1726]">
              <div className="flex items-start justify-between gap-3 border-b border-slate-200 pb-4 dark:border-slate-800">
                <div>
                  <h2 id="template-title" className="text-base font-semibold">{t("create.templateTitle")}</h2>
                  <p className="mt-1 text-xs leading-5 text-slate-500 dark:text-slate-400">{t("create.templateDescription")}</p>
                </div>
                {templatesQuery.isFetching ? <Loader2 size={16} className="shrink-0 animate-spin text-slate-400" aria-label={t("create.templateLoading")} /> : null}
              </div>
              <div className="mt-4 space-y-3">
                {templateOptions.map((template) => {
                  const selected = template.key === selectedTemplateKey;
                  return (
                    <button key={template.key} type="button" aria-pressed={selected} onClick={() => setSelectedTemplateKey(template.key)} className={`flex w-full min-w-0 items-center gap-3 rounded-md border p-3 text-left transition ${selected ? "border-indigo-500 bg-indigo-50/70 ring-1 ring-indigo-500 dark:border-violet-400 dark:bg-violet-500/10 dark:ring-violet-400" : "border-slate-200 bg-white hover:border-slate-300 dark:border-slate-700 dark:bg-slate-950/45 dark:hover:border-slate-500"}`}>
                      <TemplatePreview src={template.previewSrc} title={template.title} />
                      <span className="min-w-0 flex-1">
                        <span className="flex items-center justify-between gap-2">
                          <span className="truncate text-sm font-semibold text-slate-900 dark:text-white">{template.title}</span>
                          {selected ? <Check size={16} className="shrink-0 text-indigo-600 dark:text-violet-300" aria-hidden="true" /> : null}
                        </span>
                        <span className="mt-1 block text-xs leading-5 text-slate-500 dark:text-slate-400">{template.description}</span>
                        <span className="mt-2 flex flex-wrap gap-1.5 text-[10px] font-semibold text-slate-400 dark:text-slate-500">
                          <span>{t("create.templateVertical")}</span>
                          <span aria-hidden="true">·</span>
                          <span>{t("create.templateNodes", { count: template.nodeCount })}</span>
                          <span aria-hidden="true">·</span>
                          <span>{t("create.templateOutputs", { count: template.outputCount })}</span>
                        </span>
                      </span>
                    </button>
                  );
                })}
              </div>
              {templatesQuery.isError ? <div className="mt-3 text-xs text-amber-700 dark:text-amber-300">{t("create.templateLoadFailed")}</div> : null}
              <div className="mt-5 border-t border-slate-200 pt-4 dark:border-slate-800">
                <div className="text-xs font-semibold uppercase tracking-[0.14em] text-slate-400">{t("create.templateSelected")}</div>
                <div className="mt-2 text-sm font-semibold text-slate-900 dark:text-white">{selectedTemplate?.title}</div>
              </div>
            </section>
          </aside>
        </form>
      </main>
    </div>
  );
}

function SectionHeading({ id, title, meta }: { id: string; title: string; meta: string }) {
  return (
    <div className="flex flex-wrap items-end justify-between gap-3">
      <h2 id={id} className="text-lg font-semibold tracking-tight text-slate-950 dark:text-white">{title}</h2>
      <span className="text-xs text-slate-400 dark:text-slate-500">{meta}</span>
    </div>
  );
}

function Field({ label, required = false, meta, children }: { label: string; required?: boolean; meta?: string; children: ReactNode }) {
  return (
    <div className="min-w-0">
      <div className="mb-2 flex items-center justify-between gap-2">
        <label className="text-sm font-semibold text-slate-800 dark:text-slate-200">
          {label}{required ? <span className="ml-1 text-red-500" aria-hidden="true">*</span> : null}
        </label>
        {meta ? <span className="max-w-[55%] truncate text-xs text-slate-400" title={meta}>{meta}</span> : null}
      </div>
      {children}
    </div>
  );
}

function TemplatePreview({ src, title }: { src: string; title: string }) {
  return (
    <span className="relative h-28 w-[4.7rem] shrink-0 overflow-hidden rounded border border-slate-200 bg-slate-100 dark:border-slate-700 dark:bg-slate-900">
      <img src={src} alt={`${title}样板`} className="h-full w-full object-cover" loading="lazy" />
    </span>
  );
}

function useObjectUrl(file: File | null): string | null {
  const url = useMemo(() => file ? URL.createObjectURL(file) : null, [file]);
  useEffect(() => {
    return () => {
      if (url) {
        URL.revokeObjectURL(url);
      }
    };
  }, [url]);
  return url;
}

function useObjectUrls(files: File[]): Array<{ file: File; url: string }> {
  const urls = useMemo(() => files.map((file) => ({ file, url: URL.createObjectURL(file) })), [files]);
  useEffect(() => {
    return () => urls.forEach(({ url }) => URL.revokeObjectURL(url));
  }, [urls]);
  return urls;
}
