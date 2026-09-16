import { ApiError } from "./api";
import type { TranslationKey } from "./i18n";
import type { SessionState } from "./types";

/**
 * 数据隔离开关是否开启：后端未下发（旧版本）或缺省时视为关闭，
 * 保证关闭场景的行为与现状完全一致。
 */
export function isDataIsolationEnabled(state?: SessionState | null): boolean {
  return Boolean(state?.data_isolation_enabled);
}

type TranslateKey = (key: TranslationKey) => string;

/** 用户登录失败的展示文案：401 固定提示账号或密码错误，其余透出后端 detail，兜底通用文案。 */
export function userLoginErrorMessage(error: unknown, translateKey: TranslateKey): string {
  if (error instanceof ApiError) {
    if (error.status === 401) {
      return translateKey("login.userError");
    }
    return error.detail || translateKey("login.error");
  }
  return translateKey("login.error");
}
