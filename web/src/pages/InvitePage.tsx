import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ArrowRight, UserPlus } from "lucide-react";
import { useNavigate, useParams } from "react-router-dom";

import { api, ApiError } from "../lib/api";
import { useI18n } from "../lib/preferences";

const inputClassName =
  "w-full rounded-md border border-zinc-200 bg-white px-3 py-2 text-sm text-zinc-950 transition-shadow placeholder:text-zinc-400 focus:border-zinc-900 focus:outline-none focus:ring-1 focus:ring-zinc-900 dark:border-slate-700 dark:bg-[#0b1220] dark:text-slate-100 dark:placeholder:text-slate-500 dark:focus:border-violet-400 dark:focus:ring-violet-400/25";

const labelClassName =
  "mb-1.5 block text-[11px] font-semibold uppercase tracking-widest text-zinc-500 dark:text-slate-400";

const submitButtonClassName =
  "flex w-full items-center justify-center rounded-md bg-zinc-900 px-4 py-2 text-sm font-medium text-white shadow-sm shadow-zinc-900/20 transition-colors hover:bg-zinc-800 disabled:opacity-60 dark:bg-gradient-to-r dark:from-indigo-500 dark:to-violet-500 dark:shadow-violet-900/35 dark:ring-1 dark:ring-violet-300/35";

const errorClassName = "text-xs font-medium text-red-500 dark:text-red-300";

/**
 * 一次性邀请兑换页（/invite/:token）：受邀者自助设置用户名与密码。
 *
 * 后端契约：兑换成功即建立用户会话（服务端 cookie），因此这里直接进入应用。
 * 邀请一次性：失效/已用/过期都返回同一条人话提示，不泄漏具体原因。
 */
export function InvitePage() {
  const { t } = useI18n();
  const { token } = useParams<{ token: string }>();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");

  const redeemMutation = useMutation({
    mutationFn: () => api.redeemInvite(token ?? "", username.trim(), password),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["session"] });
      await queryClient.invalidateQueries({ queryKey: ["user-me"] });
      navigate("/products", { replace: true });
    },
    onError: (err: unknown) => {
      // 400（密码过短/用户名为空）与 401/410（邀请失效）区分展示，其余透出后端 detail
      if (err instanceof ApiError && err.status === 400) {
        setError(err.detail || t("invite.errorShort"));
        return;
      }
      setError(err instanceof ApiError && err.detail ? err.detail : t("invite.error"));
    },
  });

  const submit = () => {
    if (!username.trim() || password.length < 6) {
      setError(t("invite.errorShort"));
      return;
    }
    setError("");
    redeemMutation.mutate();
  };

  return (
    <div className="flex min-h-screen items-center justify-center bg-zinc-50 px-4 py-10 dark:bg-[#070b14]">
      <div className="w-full max-w-md rounded-2xl border border-zinc-200 bg-white p-8 shadow-sm dark:border-slate-800 dark:bg-[#0b1220]">
        <div className="mb-6">
          <span className="mb-3 inline-flex h-10 w-10 items-center justify-center rounded-xl bg-indigo-600 text-white shadow-sm">
            <UserPlus size={18} />
          </span>
          <h1 className="text-xl font-semibold text-zinc-950 dark:text-slate-50">{t("invite.title")}</h1>
          <p className="mt-1 text-sm text-zinc-500 dark:text-slate-400">{t("invite.subtitle")}</p>
        </div>

        <form
          className="space-y-4"
          onSubmit={(event) => {
            event.preventDefault();
            submit();
          }}
        >
          <div>
            <label className={labelClassName} htmlFor="invite-username">
              {t("invite.username")}
            </label>
            <input
              id="invite-username"
              className={inputClassName}
              value={username}
              autoComplete="username"
              placeholder={t("invite.usernamePlaceholder")}
              onChange={(event) => setUsername(event.target.value)}
            />
          </div>
          <div>
            <label className={labelClassName} htmlFor="invite-password">
              {t("invite.password")}
            </label>
            <input
              id="invite-password"
              type="password"
              className={inputClassName}
              value={password}
              autoComplete="new-password"
              placeholder={t("invite.passwordPlaceholder")}
              onChange={(event) => setPassword(event.target.value)}
            />
          </div>

          {error ? (
            <p className={errorClassName} role="alert">
              {error}
            </p>
          ) : null}

          <button type="submit" className={submitButtonClassName} disabled={redeemMutation.isPending}>
            {t("invite.submit")}
            <ArrowRight size={15} className="ml-1.5" />
          </button>
        </form>
      </div>
    </div>
  );
}
