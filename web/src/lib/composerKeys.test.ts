import { describe, expect, it } from "vitest";

import { shouldSubmitOnEnter } from "./composerKeys";

describe("shouldSubmitOnEnter", () => {
  it("普通回车提交", () => {
    expect(shouldSubmitOnEnter({ key: "Enter", shiftKey: false })).toBe(true);
  });

  it("Shift+Enter 不提交（留给换行）", () => {
    expect(shouldSubmitOnEnter({ key: "Enter", shiftKey: true })).toBe(false);
  });

  it("输入法组词中的回车不提交（中文/日文选词场景）", () => {
    expect(shouldSubmitOnEnter({ key: "Enter", shiftKey: false, isComposing: true })).toBe(false);
  });

  it("其他按键不提交", () => {
    expect(shouldSubmitOnEnter({ key: "a", shiftKey: false })).toBe(false);
  });
});
