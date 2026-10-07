import type { ActionResponse, Payment } from "./demo";

export type JournalRow = {
  entry_id: string; payment_id: string; effect: string; side: string; account: string;
  amount_cents: number; event_id: string; posted_at: string;
};
export type Delivery = {
  received_at: string; event_id?: string | null; type?: string | null; payment_id?: string | null;
  outcome: string; reason?: string | null;
};
export type Totals = {
  opening_cash_cents?: number; cash_cents: number; settled_cents: number; reserved_cents?: number;
  available_cents?: number; debit_cents: number; credit_cents: number; net_settled_outflow_cents?: number;
  returned_cents?: number; statuses: Record<string, string>;
};
export type StateResponse = {
  snapshot: { run: number; payments: Payment[]; journal: JournalRow[]; deliveries: Delivery[] };
  reconciliation: { actual: Totals };
  provider: { name: string; mode: string };
  store: string;
};
export type Check = { check: string; ok: boolean; expected: unknown; actual: unknown };
export type Reconciliation = {
  status: string; stage?: string | null; unexplained_differences: number;
  differences: { field: string; expected: unknown; actual: unknown }[];
  invariants: Check[]; provider_reconciliation: Check[];
  explained_exceptions: { payment_id?: string; event_id?: string; explanation: string }[];
};
export type LegacyBaseline = { result?: Totals; matches_golden?: boolean };
export type ApiError = { error?: { code?: string; message?: string } };

export async function api<T>(method: "GET" | "POST", path: string, body?: unknown): Promise<{ status: number; data: T & ApiError }> {
  const res = await fetch(path, {
    method,
    headers: { "content-type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  return { status: res.status, data: data as T & ApiError };
}

export type { ActionResponse };
