import { describe, expect, it } from "vitest";

import { zhCN } from "../lib/i18n/zh-CN";
import { enUS } from "../lib/i18n/en-US";
import { jaJP } from "../lib/i18n/ja-JP";
import { viVN } from "../lib/i18n/vi-VN";

/**
 * 邀请页依赖的文案键必须在四语言齐备：缺失会让受邀者看到 key 本身而非人话，
 * 而受邀者是外部用户，体验最不该出问题。
 */
describe("邀请页 i18n 键", () => {
  const required = [
    "invite.title",
    "invite.subtitle",
    "invite.username",
    "invite.usernamePlaceholder",
    "invite.password",
    "invite.passwordPlaceholder",
    "invite.submit",
    "invite.error",
    "invite.errorShort",
  ] as const;

  it.each([
    ["zh-CN", zhCN],
    ["en-US", enUS],
    ["ja-JP", jaJP],
    ["vi-VN", viVN],
  ])("%s 全部齐备", (_locale, dict) => {
    for (const key of required) {
      expect((dict as Record<string, string>)[key], key).toBeTruthy();
    }
  });

  it("四语言键数量完全一致（任一语言漏键会在此暴露）", () => {
    const counts = [zhCN, enUS, jaJP, viVN].map((dict) => Object.keys(dict).length);
    expect(new Set(counts).size).toBe(1);
  });
});
