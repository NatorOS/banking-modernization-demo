// Pure dashboard logic, kept free of React so it can be unit tested.

export type Stage = "" | "after_settlement" | "after_return";

export type Payment = {
  payment_id: string;
  amount_cents: number;
  status: string;
  provider_payment_id?: string | null;
  last_submission_outcome?: string | null;
  pending_return?: unknown;
};

export type ActionResult = { outcome?: string; http_status?: number; error?: { code?: string; message?: string } };
export type ActionResponse = { results?: ActionResult[]; message?: string; error?: { code?: string; message?: string } };

export type DemoAction = {
  id: string;
  act: string;
  label: string;
  body?: Record<string, unknown>;
  /** Only enabled once this payment is RETURNED, so the "duplicate" can never be the first return. */
  requiresReturned?: string;
};

export const mainFlow: DemoAction[] = [
  { id: "reset", act: "reset", label: "1 · Reset namespace" },
  { id: "submit", act: "submit-fixtures", label: "2 · Submit fixtures" },
  { id: "settle", act: "settle-all", label: "3 · Settle" },
  { id: "return", act: "return-pay-002", label: "5 · Return PAY-002 (R01)" },
];

export const duplicateActions: DemoAction[] = [
  { id: "retry", act: "submit-fixtures", label: "2b · Retry submissions" },
  { id: "replay-settlement", act: "replay-settlement", label: "4 · Replay settlement (same event ID)" },
  { id: "duplicate-settlement", act: "duplicate-settlement", label: "4b · Duplicate settlement (new event ID)" },
  { id: "replay-return", act: "replay-return", label: "6 · Replay return" },
  {
    id: "duplicate-return",
    act: "provider-event",
    label: "6b · Duplicate return (new event ID)",
    body: { payment_id: "PAY-002", type: "returned", variant: 2 },
    requiresReturned: "PAY-002",
  },
];

export const operationActions: DemoAction[] = [
  { id: "reconcile-pending", act: "reconcile-pending", label: "Reconcile pending" },
];

export function actionPath(namespace: string, act: string): string {
  const base = `/api/namespaces/${encodeURIComponent(namespace)}`;
  if (act === "reset" || act === "reconcile-pending") return `${base}/${act}`;
  return `${base}/demo/${act}`;
}

export function isEnabled(action: DemoAction, payments: Payment[]): boolean {
  if (!action.requiresReturned) return true;
  return payments.some(p => p.payment_id === action.requiresReturned && p.status === "RETURNED");
}

export function succeeded(status: number, data: ActionResponse): boolean {
  return status === 200 && (data.results ?? []).every(r => !r.error && (r.http_status ?? 200) < 400);
}

/** Only a successful action moves the expected reconciliation stage; a failed one keeps the current comparison. */
export function nextStage(current: Stage, action: DemoAction, status: number, data: ActionResponse): Stage {
  if (!succeeded(status, data)) return current;
  const { act, body } = action;
  let stage = current;
  if (act === "return-pay-002" || act === "replay-return" || (act === "provider-event" && body?.type === "returned")) stage = "after_return";
  if ((act === "settle-all" && (data.results ?? []).length) || act.includes("settlement")) stage = "after_settlement";
  if (act === "reset") stage = "";
  return stage;
}

export function summarize(label: string, status: number, data: ActionResponse): string {
  const outcomes = (data.results ?? []).map(r => r.outcome || r.error?.code || "").filter(Boolean).join(", ");
  const summary = outcomes || data.message || data.error?.message || "";
  return `${label}: HTTP ${status}${summary ? ` · ${summary}` : ""}`;
}

/** Index of the next main-flow step, derived from state so a refresh or namespace switch stays accurate. */
export function progress(hasState: boolean, payments: Payment[]): number {
  if (!hasState) return 0;
  if (!payments.length) return 1;
  if (payments.some(p => p.status === "PENDING_SUBMISSION" || p.status === "SUBMITTED")) return 2;
  if (payments.some(p => p.payment_id === "PAY-002" && p.status === "RETURNED")) return mainFlow.length;
  return 3;
}

export function cents(value: number | null | undefined): string {
  if (value == null) return "";
  return (value / 100).toLocaleString("en-US", { style: "currency", currency: "USD" });
}

export type Tone = "neutral" | "success" | "info" | "warning" | "danger";

export function outcomeTone(outcome: string): Tone {
  if (outcome === "APPLIED") return "success";
  if (outcome === "REJECTED") return "danger";
  if (outcome === "PARKED") return "info";
  return "warning";
}

export function statusTone(status: string): Tone {
  if (status === "SETTLED") return "success";
  if (status === "RETURNED") return "warning";
  if (status === "REJECTED") return "danger";
  return "info";
}
