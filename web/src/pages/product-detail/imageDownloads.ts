import { DEFAULT_LOCALE, translate, type TranslationKey, type TranslationParams } from "../../lib/i18n";
import type { PosterVariant, ProductDetail, SourceAsset, WorkflowNode } from "../../lib/types";
import type { DownloadableImage } from "../../lib/image-downloads";
import {
  compactDateTime,
  getExtensionFromFilename,
  getExtensionFromMime,
  sanitizeFilenamePart,
  toImageUrl,
} from "../../lib/image-downloads";
import { workflowNodeDisplayTitle } from "./nodeDisplay";
import { outputStringArray } from "./utils";

type TranslateFunction = (key: TranslationKey, params?: TranslationParams) => string;

const defaultT: TranslateFunction = (key, params) => translate(DEFAULT_LOCALE, key, params);

export function getSourceImageAsset(product: ProductDetail): SourceAsset | null {
  return (
    product.source_assets.find((asset) => asset.kind === "original_image") ??
    null
  );
}

export function buildSourceImageDownload(
  product: ProductDetail,
  asset: SourceAsset,
  label: string,
  previewUrl?: string,
  t: TranslateFunction = defaultT,
): DownloadableImage {
  const productName = sanitizeFilenamePart(product.name, t("chat.productFallback"));
  const imageLabel = sanitizeFilenamePart(label, t("detail.referenceImage"));
  const extension = getExtensionFromFilename(
    asset.original_filename,
    asset.mime_type,
  );
  return {
    previewUrl: toImageUrl(previewUrl, asset.preview_url, asset.download_url),
    downloadUrl: toImageUrl(asset.download_url, asset.preview_url),
    filename: `${productName}-${imageLabel}-${compactDateTime(asset.created_at)}${extension}`,
    alt: `${product.name} ${label}`,
  };
}

export function buildPosterDownload(
  productName: string,
  poster: PosterVariant,
  previewUrl?: string,
  t: TranslateFunction = defaultT,
): DownloadableImage {
  const productLabel = sanitizeFilenamePart(productName, t("chat.productFallback"));
  const posterLabel = poster.kind === "main_image" ? t("detail.mainImage") : t("detail.promoImage");
  const extension = getExtensionFromMime(poster.mime_type);
  return {
    previewUrl: toImageUrl(previewUrl, poster.preview_url, poster.download_url),
    downloadUrl: toImageUrl(poster.download_url, poster.preview_url),
    filename: `${productLabel}-${posterLabel}-${compactDateTime(poster.created_at)}${extension}`,
    alt: `${productName} ${posterLabel}`,
  };
}

export function getSourceImageDownload(
  product: ProductDetail,
  t: TranslateFunction = defaultT,
): DownloadableImage | null {
  const sourceAsset = getSourceImageAsset(product);
  return sourceAsset
    ? buildSourceImageDownload(product, sourceAsset, t("detail.mainImage"), undefined, t)
    : null;
}

export function getNodeImageDownload(
  node: WorkflowNode,
  product: ProductDetail,
  t: TranslateFunction = defaultT,
): DownloadableImage | null {
  if (node.node_type === "product_context") {
    return getSourceImageDownload(product, t);
  }
  if (node.node_type === "reference_image") {
    const ids = outputStringArray(node, "source_asset_ids");
    const asset = ids
      .map((id) =>
        product.source_assets.find((item: SourceAsset) => item.id === id),
      )
      .find((item): item is SourceAsset => Boolean(item));
    return asset
      ? buildSourceImageDownload(product, asset, workflowNodeDisplayTitle(node, t), undefined, t)
      : null;
  }
  return null;
}
