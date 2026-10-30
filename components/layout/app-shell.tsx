"use client";

import { useEffect, useMemo, useState, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  ChevronDown,
  Cloud,
  CloudFog,
  CloudLightning,
  CloudRain,
  CloudSnow,
  CloudSun,
  Menu,
  Monitor,
  Moon,
  Search,
  Sun,
  type LucideIcon,
} from "lucide-react";
import { Avatar, Button } from "@/components/ui/primitives";
import { usePageEntrance } from "@/lib/use-page-entrance";
import { StatusBadge } from "@/components/data-display/status-badge";
import { RuntimeHealthBadge } from "@/components/ui/query-state";
import { agents } from "@/lib/agent-data";
import type { CurrentWeather, CurrentWeatherCondition } from "@/lib/current-weather";
import { turbines, windFarm } from "@/lib/farm-data";
import { alarms, decisions, missions } from "@/lib/operations-data";
import { cn } from "@/lib/utils";
import { hydrateDemoWorkflow, useDemoWorkflow } from "@/lib/use-demo-workflow";
import { useOpenVigilIdentity } from "@/components/providers/identity-provider";
import {
  deriveWorkflowKpis,
  overlayClientAgents,
  overlayClientAlarms,
  overlayClientDecisions,
  overlayClientMissions,
} from "@/lib/client-workflow-overlays";
import { apiGet } from "@/lib/api-client";
import {
  deriveQueryViewState,
  mergeRuntimeHealth,
  queryRuntimeHealth,
  type RuntimeHealthSnapshot,
} from "@/lib/query-state";

import type { RuntimeMode } from "./app-shell-navigation";
import { ShellSidebar, useShellNavigation } from "./app-shell-sidebar";
import { CommandPalette } from "./app-shell-search";
import { ShellNotifications } from "./app-shell-notifications";

type RuntimeEnvelope = {
  readonly data: {
    readonly productionReady: boolean;
  };
};

type CurrentWeatherEnvelope = {
  readonly data: CurrentWeather;
};

const themePreferenceKey = "openvigil-theme";
const legacyThemePreferenceKey = "windops-theme";

const snapshotTime = new Date(windFarm.lastUpdatedAt).toLocaleTimeString("zh-CN", {
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
  timeZone: windFarm.timezone,
});

const weatherIcons: Readonly<Partial<Record<CurrentWeatherCondition, LucideIcon>>> = {
  clear: Sun,
  "partly-cloudy": CloudSun,
  cloudy: Cloud,
  overcast: Cloud,
  rain: CloudRain,
  snow: CloudSnow,
  storm: CloudLightning,
  fog: CloudFog,
};

async function fetchCurrentWeather(signal: AbortSignal): Promise<CurrentWeatherEnvelope> {
  const response = await fetch("/api/weather", {
    headers: { accept: "application/json" },
    signal,
  });
  if (!response.ok) throw new Error(`Current weather returned ${response.status}.`);
  return (await response.json()) as CurrentWeatherEnvelope;
}

function ThemeControl() {
  const [theme, setTheme] = useState<"light" | "dark" | "system">("light");

  useEffect(() => {
    const current = window.localStorage.getItem(themePreferenceKey);
    const stored = current ?? window.localStorage.getItem(legacyThemePreferenceKey);
    if (current === null && stored !== null) {
      window.localStorage.setItem(themePreferenceKey, stored);
    }
    const preference =
      stored === "dark" || stored === "light" || stored === "system" ? stored : "light";
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const apply = (value: "light" | "dark" | "system") => {
      document.documentElement.dataset.theme =
        value === "system" ? (media.matches ? "dark" : "light") : value;
      document.documentElement.style.colorScheme =
        value === "system" ? (media.matches ? "dark" : "light") : value;
      window.dispatchEvent(new CustomEvent("openvigil-theme-change"));
    };
    apply(preference);
    const frame = window.requestAnimationFrame(() => setTheme(preference));
    const handleSystemChange = () => {
      if ((window.localStorage.getItem(themePreferenceKey) ?? "light") === "system") {
        apply("system");
      }
    };
    media.addEventListener("change", handleSystemChange);
    return () => {
      window.cancelAnimationFrame(frame);
      media.removeEventListener("change", handleSystemChange);
    };
  }, []);

  function toggleTheme() {
    const next = theme === "light" ? "dark" : theme === "dark" ? "system" : "light";
    setTheme(next);
    window.localStorage.setItem(themePreferenceKey, next);
    document.documentElement.dataset.theme =
      next === "system"
        ? window.matchMedia("(prefers-color-scheme: dark)").matches
          ? "dark"
          : "light"
        : next;
    document.documentElement.style.colorScheme = document.documentElement.dataset.theme;
    window.dispatchEvent(new CustomEvent("openvigil-theme-change"));
  }

  return (
    <Button
      size="icon"
      variant="ghost"
      onClick={toggleTheme}
      aria-label={`当前${theme === "system" ? "跟随系统" : theme === "light" ? "浅色" : "深色"}，点击切换主题`}
      title={`主题：${theme}`}
    >
      {theme === "light" ? (
        <Sun size={17} />
      ) : theme === "dark" ? (
        <Moon size={17} />
      ) : (
        <Monitor size={17} />
      )}
    </Button>
  );
}

export function AppShell({
  children,
  activePath = "/",
  runtimeMode,
  pageHealth,
}: {
  children: ReactNode;
  activePath?: string;
  runtimeMode: RuntimeMode;
  pageHealth?: RuntimeHealthSnapshot;
}) {
  const isProduction = runtimeMode === "production";
  const pageEntranceRef = usePageEntrance(activePath);
  const { session } = useOpenVigilIdentity();
  const workflow = useDemoWorkflow();
  const displayAlarms = useMemo(
    () => (isProduction ? [] : overlayClientAlarms(alarms, workflow)),
    [isProduction, workflow],
  );
  const displayDecisions = useMemo(
    () => (isProduction ? [] : overlayClientDecisions(decisions, workflow)),
    [isProduction, workflow],
  );
  const displayMissions = useMemo(
    () => (isProduction ? [] : overlayClientMissions(missions, workflow)),
    [isProduction, workflow],
  );
  const displayAgents = useMemo(
    () => (isProduction ? [] : overlayClientAgents(agents, workflow)),
    [isProduction, workflow],
  );
  const workflowKpis = useMemo(
    () => deriveWorkflowKpis(turbines, displayAlarms, displayMissions, displayAgents),
    [displayAgents, displayAlarms, displayMissions],
  );
  const pendingDecisionCount = useMemo(
    () =>
      displayDecisions.filter((decision) =>
        ["under-review", "revision-requested"].includes(decision.status),
      ).length,
    [displayDecisions],
  );
  const navigation = useShellNavigation();
  const { collapsed, mobileOpen, overlayNavigation, navigationReady, setMobileOpen } = navigation;
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [notificationsOpen, setNotificationsOpen] = useState(false);
  const [currentTime, setCurrentTime] = useState("--:--:--");
  const runtimeQuery = useQuery({
    queryKey: ["production-runtime"],
    queryFn: ({ signal }) => apiGet<RuntimeEnvelope>("/api/runtime", signal),
    enabled: isProduction,
    retry: false,
    refetchInterval: 30_000,
    staleTime: 45_000,
  });
  const weatherQuery = useQuery({
    queryKey: ["current-weather"],
    queryFn: ({ signal }) => fetchCurrentWeather(signal),
    retry: 1,
    refetchInterval: 600_000,
    staleTime: 300_000,
  });
  const currentWeather = weatherQuery.data?.data;
  const WeatherIcon = currentWeather
    ? (weatherIcons[currentWeather.condition] ?? CloudSun)
    : CloudSun;
  const runtimeState = deriveQueryViewState({
    data: runtimeQuery.data?.data,
    dataUpdatedAt: runtimeQuery.dataUpdatedAt,
    error: runtimeQuery.error,
    isError: runtimeQuery.isError,
    isFetching: runtimeQuery.isFetching,
    isPending: runtimeQuery.isPending,
    isStale: runtimeQuery.isStale,
    isEmpty: () => false,
    staleAfterMs: 60_000,
  });
  const systemHealth = mergeRuntimeHealth(
    isProduction ? queryRuntimeHealth(runtimeState, "生产运行时") : null,
    isProduction ? pageHealth : null,
  );
  useEffect(() => {
    if (!isProduction) hydrateDemoWorkflow();

    const updateClock = () =>
      setCurrentTime(
        new Date().toLocaleTimeString("zh-CN", {
          hour: "2-digit",
          minute: "2-digit",
          second: "2-digit",
          hour12: false,
          timeZone: windFarm.timezone,
        }),
      );
    updateClock();
    const clockTimer = window.setInterval(updateClock, 1_000);

    function handleKeyboard(event: KeyboardEvent) {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setPaletteOpen((value) => !value);
      }
      if (event.key === "Escape") {
        setPaletteOpen(false);
        setNotificationsOpen(false);
        setMobileOpen(false);
      }
    }
    window.addEventListener("keydown", handleKeyboard);
    return () => {
      window.clearInterval(clockTimer);
      window.removeEventListener("keydown", handleKeyboard);
    };
  }, [isProduction, setMobileOpen]);

  return (
    <div className={cn("app-shell", collapsed && !overlayNavigation && "app-shell--collapsed")}>
      <ShellSidebar
        navigation={navigation}
        runtimeMode={runtimeMode}
        activePath={activePath}
        workflowKpis={workflowKpis}
        pendingDecisionCount={pendingDecisionCount}
        displayAgentCount={displayAgents.length}
        systemHealth={systemHealth}
      />

      <div className="app-column">
        <header className="topbar">
          <div className="topbar__left">
            <Button
              size="icon"
              variant="ghost"
              className="mobile-menu-button"
              disabled={!navigationReady}
              onClick={() => setMobileOpen(true)}
              aria-label="打开导航"
              aria-controls="openvigil-primary-sidebar"
              aria-expanded={mobileOpen}
            >
              <Menu size={19} />
            </Button>
            <div className="live-context" aria-live="polite">
              <StatusBadge
                value={runtimeMode}
                label={isProduction ? "Production" : "Demo"}
                tone="neutral"
                compact
              />
              <span>
                {isProduction ? "PostgreSQL / TimescaleDB 权威数据" : `SCADA 快照 ${snapshotTime}`}
              </span>
              <time dateTime={currentTime === "--:--:--" ? undefined : currentTime}>
                {currentTime}
              </time>
              {isProduction ? (
                <RuntimeHealthBadge health={systemHealth} compact />
              ) : (
                <span
                  title={
                    workflow.lastSyncError ?? `WT-023 服务端工作流修订 ${workflow.serverRevision}`
                  }
                >
                  <StatusBadge
                    value={workflow.syncStatus}
                    label={
                      workflow.syncStatus === "loading"
                        ? "同步中"
                        : workflow.syncStatus === "saving"
                          ? "保存中"
                          : workflow.syncStatus === "error"
                            ? "离线"
                            : !workflow.writable
                              ? "只读"
                              : workflow.persistence === "d1"
                                ? `D1 · R${workflow.serverRevision}`
                                : "内存"
                    }
                    tone={
                      workflow.syncStatus === "error"
                        ? "critical"
                        : workflow.syncStatus === "loading" || workflow.syncStatus === "saving"
                          ? "info"
                          : !workflow.writable
                            ? "warning"
                            : workflow.persistence === "d1"
                              ? "success"
                              : "warning"
                    }
                    compact
                  />
                </span>
              )}
            </div>
          </div>
          <div className="topbar__right">
            <div
              className="weather-chip"
              aria-live="polite"
              title={
                currentWeather
                  ? `和风天气 · 获取于 ${new Date(currentWeather.fetchedAt).toLocaleString("zh-CN")}${
                      currentWeather.stale ? " · 当前显示陈旧缓存" : ""
                    }`
                  : weatherQuery.isPending
                    ? "正在获取实时天气"
                    : "实时天气暂不可用，请检查服务端配置或网络"
              }
            >
              <WeatherIcon size={17} aria-hidden="true" />
              <span>
                <strong>
                  {currentWeather
                    ? `${currentWeather.locationName} · ${currentWeather.conditionText} ${currentWeather.temperatureC.toFixed(0)}°C`
                    : weatherQuery.isPending
                      ? "实时天气同步中"
                      : "实时天气暂不可用"}
                </strong>
                <small>
                  {currentWeather
                    ? `${currentWeather.windDirection} ${currentWeather.windSpeedMps.toFixed(1)} m/s${currentWeather.stale ? " · 陈旧" : ""}`
                    : weatherQuery.isPending
                      ? "正在连接和风天气"
                      : "保留业务页面，稍后自动重试"}
                  {currentWeather ? (
                    <>
                      {" · "}
                      <a href="https://www.qweather.com/" target="_blank" rel="noreferrer">
                        和风天气
                      </a>
                    </>
                  ) : null}
                </small>
              </span>
            </div>
            <button
              className="global-search"
              onClick={() => setPaletteOpen(true)}
              aria-label="打开全局搜索"
            >
              <Search size={16} />
              <span>搜索机组、告警、Mission…</span>
              <kbd>Ctrl K</kbd>
            </button>
            <div className="topbar-divider" />
            <ThemeControl />
            <ShellNotifications
              open={notificationsOpen}
              onToggle={() => setNotificationsOpen((value) => !value)}
              runtimeMode={runtimeMode}
              workflow={workflow}
            />
            {isProduction && session ? (
              <a
                className="user-menu"
                href={session.signOutPath}
                title="安全退出 OpenVigil"
                data-authenticated-subject={session.subject}
              >
                <Avatar label={session.displayName} tone="slate" size="sm" />
                <span>
                  <strong>{session.displayName}</strong>
                  <small>{session.roles.join(" · ")} · Sites 委托身份</small>
                </span>
                <ChevronDown size={14} />
              </a>
            ) : (
              <a className="user-menu" href="/login" aria-label="打开登录页面">
                <Avatar label="林 工" tone="slate" size="sm" />
                <span>
                  <strong>林工</strong>
                  <small>值班工程师</small>
                </span>
                <ChevronDown size={14} />
              </a>
            )}
          </div>
        </header>
        <main className="app-main" ref={pageEntranceRef}>
          {children}
        </main>
      </div>

      <CommandPalette
        open={paletteOpen}
        onClose={() => setPaletteOpen(false)}
        runtimeMode={runtimeMode}
      />
    </div>
  );
}
