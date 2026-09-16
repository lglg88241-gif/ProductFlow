import { useEffect, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ArrowRight, ChevronDown, LayoutGrid } from "lucide-react";
import { useNavigate } from "react-router-dom";

import { api, ApiError } from "../lib/api";
import { useI18n } from "../lib/preferences";
import { userLoginErrorMessage } from "../lib/userAuth";

interface LoginPageProps {
  authenticated: boolean;
  /** 数据隔离开关：开启时以用户登录为主表单，管理员密钥折叠保留；关闭时维持现状（仅管理员密钥） */
  dataIsolationEnabled: boolean;
}

const inputClassName =
  "w-full rounded-md border border-zinc-200 bg-white px-3 py-2 text-sm text-zinc-950 transition-shadow placeholder:text-zinc-400 focus:border-zinc-900 focus:outline-none focus:ring-1 focus:ring-zinc-900 dark:border-slate-700 dark:bg-[#0b1220] dark:text-slate-100 dark:placeholder:text-slate-500 dark:focus:border-violet-400 dark:focus:ring-violet-400/25";

const labelClassName =
  "mb-1.5 block text-[11px] font-semibold uppercase tracking-widest text-zinc-500 dark:text-slate-400";

const submitButtonClassName =
  "flex w-full items-center justify-center rounded-md bg-zinc-900 px-4 py-2 text-sm font-medium text-white shadow-sm shadow-zinc-900/20 transition-colors hover:bg-zinc-800 disabled:opacity-60 dark:bg-gradient-to-r dark:from-indigo-500 dark:to-violet-500 dark:shadow-violet-900/35 dark:ring-1 dark:ring-violet-300/35";

const errorClassName = "text-xs font-medium text-red-500 dark:text-red-300";

export function LoginPage({ authenticated, dataIsolationEnabled }: LoginPageProps) {
  const { t } = useI18n();
  const [key, setKey] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [adminError, setAdminError] = useState("");
  const [userError, setUserError] = useState("");
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  useEffect(() => {
    if (authenticated) {
      navigate("/products", { replace: true });
    }
  }, [authenticated, navigate]);

  const loginMutation = useMutation({
    mutationFn: (adminKey: string) => api.createSession(adminKey),
    onSuccess: async () => {
      queryClient.removeQueries({ queryKey: ["settings-lock-state"] });
      queryClient.removeQueries({ queryKey: ["config"] });
      await queryClient.invalidateQueries({ queryKey: ["session"] });
      navigate("/products", { replace: true });
    },
    onError: (mutationError) => {
      if (mutationError instanceof ApiError) {
        setAdminError(mutationError.detail);
        return;
      }
      setAdminError(t("login.error"));
    },
  });

  const userLoginMutation = useMutation({
    mutationFn: (input: { username: string; password: string }) => api.userLogin(input.username, input.password),
    onSuccess: async () => {
      // 用户会话 cookie 已由后端种下：刷新 user/me 让 App 侧鉴权判定生效
      await queryClient.invalidateQueries({ queryKey: ["user-me"] });
      navigate("/products", { replace: true });
    },
    onError: (mutationError) => {
      setUserError(userLoginErrorMessage(mutationError, t));
    },
  });

  const handleLogin = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setAdminError("");
    loginMutation.mutate(key);
  };

  const handleUserLogin = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setUserError("");
    userLoginMutation.mutate({ username, password });
  };

  const adminKeyForm = (
    <form onSubmit={handleLogin} className="space-y-4">
      <div>
        <label className={labelClassName}>{t("login.adminKey")}</label>
        <input
          type="password"
          value={key}
          onChange={(event) => setKey(event.target.value)}
          className={inputClassName}
          placeholder={t("login.adminKeyPlaceholder")}
          autoComplete="current-password"
        />
      </div>

      {adminError ? <div className={errorClassName}>{adminError}</div> : null}

      <button type="submit" disabled={loginMutation.isPending} className={submitButtonClassName}>
        {t("login.submit")} <ArrowRight size={14} className="ml-2 opacity-70" />
      </button>
    </form>
  );

  const userLoginForm = (
    <form onSubmit={handleUserLogin} className="space-y-4">
      <div>
        <label htmlFor="login-username" className={labelClassName}>
          {t("login.username")}
        </label>
        <input
          id="login-username"
          name="username"
          type="text"
          value={username}
          onChange={(event) => setUsername(event.target.value)}
          className={inputClassName}
          placeholder={t("login.usernamePlaceholder")}
          autoComplete="username"
        />
      </div>
      <div>
        <label htmlFor="login-password" className={labelClassName}>
          {t("login.password")}
        </label>
        <input
          id="login-password"
          name="password"
          type="password"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          className={inputClassName}
          placeholder={t("login.passwordPlaceholder")}
          autoComplete="current-password"
        />
      </div>

      {userError ? <div className={errorClassName}>{userError}</div> : null}

      <button type="submit" disabled={userLoginMutation.isPending} className={submitButtonClassName}>
        {t("login.submit")} <ArrowRight size={14} className="ml-2 opacity-70" />
      </button>
    </form>
  );

  return (
    <div className="relative flex min-h-screen flex-col items-center justify-center bg-zinc-50 dark:bg-[#060a12] dark:text-slate-100">
      <div className="absolute inset-0 bg-[linear-gradient(to_right,#e4e4e7_1px,transparent_1px),linear-gradient(to_bottom,#e4e4e7_1px,transparent_1px)] bg-[size:4rem_4rem] opacity-50 [mask-image:radial-gradient(ellipse_60%_60%_at_50%_50%,#000_70%,transparent_100%)] dark:bg-[linear-gradient(to_right,rgba(71,85,105,0.34)_1px,transparent_1px),linear-gradient(to_bottom,rgba(71,85,105,0.34)_1px,transparent_1px)] dark:opacity-70" />

      <div className="relative w-full max-w-sm px-6">
        <div className="mb-10">
          <div className="mb-4 flex h-10 w-10 items-center justify-center rounded-lg bg-zinc-900 shadow-sm shadow-zinc-900/20 dark:border dark:border-violet-400/35 dark:bg-violet-500/18 dark:shadow-violet-950/30">
            <LayoutGrid size={20} className="text-white" strokeWidth={2} />
          </div>
          <h1 className="text-2xl font-semibold tracking-tight text-zinc-900 dark:text-white">{t("app.brand")}</h1>
          <p className="mt-1 text-sm text-zinc-500 dark:text-slate-400">{t("login.subtitle")}</p>
        </div>

        {dataIsolationEnabled ? (
          <div className="space-y-4">
            {userLoginForm}
            <details className="group rounded-md border border-zinc-200 dark:border-slate-700">
              <summary className="flex cursor-pointer list-none items-center justify-between px-3 py-2 text-xs font-semibold text-zinc-500 transition-colors hover:text-zinc-900 dark:text-slate-400 dark:hover:text-slate-100 [&::-webkit-details-marker]:hidden">
                {t("login.adminKeyToggle")}
                <ChevronDown size={13} className="transition-transform group-open:rotate-180" aria-hidden="true" />
              </summary>
              <div className="px-3 pb-3">{adminKeyForm}</div>
            </details>
          </div>
        ) : (
          adminKeyForm
        )}
      </div>
    </div>
  );
}
