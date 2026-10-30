"use client";

import { useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { ChevronDown, PanelLeftClose, PanelLeftOpen, X, Zap } from "lucide-react";
import { Button } from "@/components/ui/primitives";
import { BrandLogo } from "@/components/ui/brand-logo";
import { WindFarmIcon } from "@/components/ui/wind-icons";
import { useOpenVigilIdentity } from "@/components/providers/identity-provider";
import { useAccessibleDialog } from "@/lib/use-accessible-dialog";
import { isNavigationItemActive } from "@/lib/navigation-ownership";
import type { deriveWorkflowKpis } from "@/lib/client-workflow-overlays";
import type { RuntimeHealthSnapshot } from "@/lib/query-state";
import { cn } from "@/lib/utils";
import { visibleNavigationFor, type RuntimeMode } from "./app-shell-navigation";

const sidebarPreferenceKey = "openvigil-sidebar-collapsed";
const legacySidebarPreferenceKey = "windops-sidebar-collapsed";

export function useShellNavigation() {
  const [collapsed, setCollapsed] = useState(false);
  const [mobileOpen, setMobileOpen] = useState(false);
  const [overlayNavigation, setOverlayNavigation] = useState(false);
  const [navigationReady, setNavigationReady] = useState(false);
  const sidebarDialogRef = useAccessibleDialog<HTMLElement>(
    () => setMobileOpen(false),
    mobileOpen && overlayNavigation,
  );

  useEffect(() => {
    const overlayQuery = window.matchMedia("(max-width: 1023px)");
    const laptopQuery = window.matchMedia("(min-width: 1024px) and (max-width: 1439px)");
    const currentPreference = window.localStorage.getItem(sidebarPreferenceKey);
    const storedPreference =
      currentPreference ?? window.localStorage.getItem(legacySidebarPreferenceKey);
    if (currentPreference === null && storedPreference !== null) {
      window.localStorage.setItem(sidebarPreferenceKey, storedPreference);
    }

    const updateOverlayNavigation = () => {
      setOverlayNavigation(overlayQuery.matches);
      if (!overlayQuery.matches) setMobileOpen(false);
    };
    const frame = window.requestAnimationFrame(() => {
      setCollapsed(
        storedPreference === "true"
          ? true
          : storedPreference === "false"
            ? false
            : laptopQuery.matches,
      );
      updateOverlayNavigation();
      setNavigationReady(true);
    });
    overlayQuery.addEventListener("change", updateOverlayNavigation);
    return () => {
      window.cancelAnimationFrame(frame);
      overlayQuery.removeEventListener("change", updateOverlayNavigation);
    };
  }, []);

  useEffect(() => {
    if (!mobileOpen || !overlayNavigation) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previousOverflow;
    };
  }, [mobileOpen, overlayNavigation]);

  function toggleCollapsed() {
    const next = !collapsed;
    setCollapsed(next);
    window.localStorage.setItem(sidebarPreferenceKey, String(next));
  }
  return {
    collapsed,
    mobileOpen,
    overlayNavigation,
    navigationReady,
    setMobileOpen,
    sidebarDialogRef,
    toggleCollapsed,
  };
}

export function ShellSidebar({
  navigation,
  runtimeMode,
  activePath,
  workflowKpis,
  pendingDecisionCount,
  displayAgentCount,
  systemHealth,
}: {
  navigation: ReturnType<typeof useShellNavigation>;
  runtimeMode: RuntimeMode;
  activePath: string;
  workflowKpis: ReturnType<typeof deriveWorkflowKpis>;
  pendingDecisionCount: number;
  displayAgentCount: number;
  systemHealth: RuntimeHealthSnapshot;
}) {
  const {
    collapsed,
    mobileOpen,
    overlayNavigation,
    setMobileOpen,
    sidebarDialogRef,
    toggleCollapsed,
  } = navigation;
  const { can } = useOpenVigilIdentity();
  const isProduction = runtimeMode === "production";
  const visibleNavigation = useMemo(
    () => visibleNavigationFor(runtimeMode, can),
    [can, runtimeMode],
  );
  return (
    <>
      {mobileOpen ? (
        <button
          className="mobile-backdrop"
          aria-label="关闭导航"
          onClick={() => setMobileOpen(false)}
        />
      ) : null}
      <aside
        ref={sidebarDialogRef}
        id="openvigil-primary-sidebar"
        className={cn("sidebar", mobileOpen && "sidebar--mobile-open")}
        role={mobileOpen && overlayNavigation ? "dialog" : undefined}
        aria-modal={mobileOpen && overlayNavigation ? "true" : undefined}
        aria-label={mobileOpen && overlayNavigation ? "主导航抽屉" : undefined}
        aria-hidden={overlayNavigation && !mobileOpen ? "true" : undefined}
        inert={overlayNavigation && !mobileOpen ? true : undefined}
        tabIndex={mobileOpen && overlayNavigation ? -1 : undefined}
      >
        <div className="sidebar__brand">
          <Link href="/" className="brand-lockup" aria-label="OpenVigil 首页">
            <BrandLogo />
            <span className="brand-copy">
              <strong>OpenVigil</strong>
              <small>工业智能</small>
            </span>
          </Link>
          <Button
            size="icon"
            variant="ghost"
            className="sidebar__mobile-close"
            onClick={() => setMobileOpen(false)}
            aria-label="关闭导航"
          >
            <X size={17} />
          </Button>
        </div>

        <Link className="farm-switcher" href="/wind-farms" aria-label="选择风场">
          <span className="farm-switcher__icon">
            <WindFarmIcon size={17} />
          </span>
          <span className="farm-switcher__copy">
            <small>{isProduction ? "生产资产范围" : "当前风场"}</small>
            <strong>{isProduction ? "风场目录" : "华东海上风电场"}</strong>
          </span>
          <ChevronDown size={14} />
        </Link>

        <nav className="sidebar__nav" aria-label="主导航">
          {visibleNavigation.map((group) => (
            <div className="nav-group" key={group.label}>
              <span className="nav-group__label">{group.label}</span>
              <div className="nav-group__items">
                {group.items.map((item) => {
                  const Icon = item.icon;
                  const href =
                    isProduction && item.href === "/turbines/WT-023" ? "/wind-farms" : item.href;
                  const active = isNavigationItemActive(activePath, href, runtimeMode);
                  if (item.disabled) {
                    return (
                      <span
                        className="nav-item nav-item--disabled"
                        aria-disabled="true"
                        title={`${item.label} · 下一阶段`}
                        key={item.label}
                      >
                        <Icon size={17} />
                        <span>{item.label}</span>
                        <small>即将开放</small>
                      </span>
                    );
                  }
                  return (
                    <a
                      className={cn("nav-item", active && "nav-item--active")}
                      href={href}
                      key={item.label}
                      title={collapsed ? item.label : undefined}
                      aria-current={active ? "page" : undefined}
                    >
                      <Icon size={17} />
                      <span>{item.label}</span>
                      {item.badge && !isProduction ? (
                        <small>
                          {item.href === "/alarms"
                            ? workflowKpis.activeAlarmCount
                            : item.href === "/missions"
                              ? workflowKpis.activeMissionCount
                              : item.href === "/decisions"
                                ? pendingDecisionCount
                                : item.href === "/agents"
                                  ? workflowKpis.activeAgentCount
                                  : item.badge}
                        </small>
                      ) : null}
                    </a>
                  );
                })}
              </div>
            </div>
          ))}
        </nav>

        <div className="sidebar__footer">
          <div className="system-health" data-health={isProduction ? systemHealth.status : "ready"}>
            <span className="system-health__icon">
              <Zap size={16} />
            </span>
            <span>
              <strong>{isProduction ? `Production · ${systemHealth.label}` : "AI 系统正常"}</strong>
              <small>
                {isProduction
                  ? systemHealth.detail
                  : `${workflowKpis.onlineAgentCount} / ${displayAgentCount} Agent 在线`}
              </small>
            </span>
            <span
              className={cn(
                "system-health__pulse",
                isProduction && `system-health__pulse--${systemHealth.status}`,
              )}
            />
          </div>
          <button
            className="sidebar-collapse"
            onClick={toggleCollapsed}
            aria-label={collapsed ? "展开侧栏" : "收起侧栏"}
          >
            {collapsed ? <PanelLeftOpen size={17} /> : <PanelLeftClose size={17} />}
            <span>{collapsed ? "展开" : "收起侧栏"}</span>
          </button>
        </div>
      </aside>
    </>
  );
}
