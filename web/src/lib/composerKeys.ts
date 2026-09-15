/** 输入框回车提交的判定逻辑（纯函数，便于测试与复用）。 */

export interface EnterKeyLike {
  key: string;
  shiftKey: boolean;
  /** React 合成事件的 nativeEvent.isComposing；中文输入法组词回车时为 true。 */
  isComposing?: boolean;
}

/**
 * 是否应把这次回车当作"提交"。
 *
 * 中文/日文输入法选词时按回车不应发送消息——这正是本项目的主场景（zh-CN 为默认语言）。
 * Shift+Enter 始终留给换行。
 */
export function shouldSubmitOnEnter(event: EnterKeyLike): boolean {
  return event.key === "Enter" && !event.shiftKey && !event.isComposing;
}
