import type { ActivityEvent, Alarm, Decision, EvidenceItem, Mission, WorkOrder } from "./types";
import { evidenceItems } from "./operations-fixtures/evidence.ts";
import { alarms } from "./operations-fixtures/alarms.ts";
import { missions, featuredMission } from "./operations-fixtures/missions.ts";
import { decisions, featuredDecision } from "./operations-fixtures/decisions.ts";
import { workOrders } from "./operations-fixtures/work-orders.ts";
import { activityEvents } from "./operations-fixtures/activity.ts";

export {
  evidenceItems,
  alarms,
  missions,
  featuredMission,
  decisions,
  featuredDecision,
  workOrders,
  activityEvents,
};

const normalizeOperationId = (id: string): string => id.trim().toUpperCase();

/** Return a mission by canonical ID (for example, `MISSION-2026-0823`). */
export const getMission = (id: string): Mission | undefined => {
  const normalized = normalizeOperationId(id);
  return missions.find((mission) => mission.id === normalized);
};

export const getAlarm = (id: string): Alarm | undefined => {
  const normalized = normalizeOperationId(id);
  return alarms.find((alarm) => alarm.id === normalized);
};

export const getWorkOrder = (id: string): WorkOrder | undefined => {
  const normalized = normalizeOperationId(id);
  return workOrders.find((workOrder) => workOrder.id === normalized);
};

export const getDecision = (id: string): Decision | undefined => {
  const normalized = normalizeOperationId(id);
  return decisions.find((decision) => decision.id === normalized);
};

export const getEvidenceForMission = (missionId: string): readonly EvidenceItem[] => {
  const normalized = normalizeOperationId(missionId);
  return evidenceItems.filter((evidence) => evidence.missionId === normalized);
};

export const getActivityForMission = (missionId: string): readonly ActivityEvent[] => {
  const normalized = normalizeOperationId(missionId);
  return activityEvents.filter((event) => event.missionId === normalized);
};

export const getAlarmsForTurbine = (turbineId: string): readonly Alarm[] => {
  const normalized = normalizeOperationId(turbineId);
  return alarms.filter((alarm) => alarm.turbineId === normalized);
};
