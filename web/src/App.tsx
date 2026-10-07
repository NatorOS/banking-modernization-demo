import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { Alert } from "@/components/arc/alert/alert";
import { Badge } from "@/components/arc/badge/badge";
import { Button } from "@/components/arc/button/button";
import { EmptyState } from "@/components/arc/empty-state/empty-state";
import { Input } from "@/components/arc/input/input";
import { JsonViewer } from "@/components/arc/json-viewer/json-viewer";
import { MetricCard } from "@/components/arc/metric-card/metric-card";
import SegmentedControl from "@/components/arc/segmented-control/segmented-control";
import { Skeleton } from "@/components/arc/skeleton/skeleton";
import { SortableDataTable } from "@/components/arc/sortable-data-table/sortable-data-table";
import { Stepper } from "@/components/arc/stepper/stepper";
import { ThemeSwitch, type Theme } from "@/components/arc/theme-switch/theme-switch";
import { api, type LegacyBaseline, type Reconciliation, type StateResponse, type Totals } from "@/lib/api";
import {
  actionPath, cents, duplicateActions, isEnabled, mainFlow, nextStage, operationActions, outcomeTone, progress,
  statusTone, succeeded, summarize, type ActionResponse, type DemoAction, type Stage,
} from "@/lib/demo";
import styles from "./App.module.css";

const NAMESPACE_PATTERN = /^demo-[a-z0-9-]+$/;
const backendLabel = import.meta.env.DEV
  ? "Vite dev server (proxy to 127.0.0.1:8000)"
  : document.querySelector<HTMLMetaElement>('meta[name="dashboard-backend"]')?.content ?? "unknown";
const initialNamespace = document.querySelector<HTMLInputElement>("#dashboard-namespace")?.value || "demo-local";

const stages: { value: Stage; label: string }[] = [
  { value: "", label: "Invariants only" },
  { value: "after_settlement", label: "After settlement" },
  { value: "after_return", label: "After return" },
];
const stageLabel = (stage: Stage) => stages.find(s => s.value === stage)?.label ?? stage;

type View =
  | { status: "loading" }
  | { status: "missing"; message: string }
  | { status: "error"; message: string }
  | { status: "ready"; data: StateResponse };
type Last = { text: string; ok: boolean; data: unknown };

/** MetricCard shows whole numbers, so fall back to the exact amount whenever cents would be rounded away. */
function Money({ label, value, context }: { label: string; value: number | undefined; context: string }) {
  const amount = value ?? 0;
  return <MetricCard label={label} value={amount / 100} context={amount % 100 === 0 ? context : `${cents(amount)} exactly`} />;
}

function Code({ value }: { value: unknown }) {
  const text = value == null || value === "" ? "–" : String(value);
  return <code className={styles.code} title={text}>{text}</code>;
}

const json = (value: unknown) => <Code value={JSON.stringify(value)} />;

export function App() {
  const [theme, setTheme] = useState<Theme>("light");
  const [draft, setDraft] = useState(initialNamespace);
  const [namespace, setNamespace] = useState(initialNamespace);
  const [stage, setStage] = useState<Stage>("");
  const [view, setView] = useState<View>({ status: "loading" });
  const [recon, setRecon] = useState<Reconciliation | null>(null);
  const [legacy, setLegacy] = useState<LegacyBaseline | null | "error">(null);
  const [last, setLast] = useState<Last | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const request = useRef(0);

  useEffect(() => { document.documentElement.dataset.theme = theme; }, [theme]);

  const loadLegacy = useCallback(async () => {
    try {
      const { status, data } = await api<LegacyBaseline>("GET", "/api/legacy/baseline");
      setLegacy(status === 200 && data.result ? data : "error");
    } catch {
      setLegacy("error");
    }
  }, []);

  const refresh = useCallback(async (ns: string, nextStageValue: Stage) => {
    const id = ++request.current;
    const base = `/api/namespaces/${encodeURIComponent(ns)}`;
    try {
      const state = await api<StateResponse>("GET", `${base}/state`);
      if (id !== request.current) return;
      if (state.status !== 200) {
        setRecon(null);
        setView(state.data.error?.code === "unknown_namespace" || state.status === 404
          ? { status: "missing", message: state.data.error?.message ?? "No state for this namespace" }
          : { status: "error", message: state.data.error?.message ?? `HTTP ${state.status}` });
        return;
      }
      setView({ status: "ready", data: state.data });
      const query = nextStageValue ? `?stage=${nextStageValue}` : "";
      const report = await api<Reconciliation>("GET", `${base}/reconciliation${query}`);
      if (id !== request.current) return;
      setRecon(report.data.invariants ? report.data : null);
    } catch (error) {
      if (id !== request.current) return;
      setView({ status: "error", message: error instanceof Error ? error.message : "Request failed" });
    }
  }, []);

  useEffect(() => { void loadLegacy(); }, [loadLegacy]);
  useEffect(() => { void refresh(namespace, stage); }, [namespace, stage, refresh]);

  const draftError = NAMESPACE_PATTERN.test(draft.trim()) ? undefined : "Use demo- followed by lowercase letters, digits, or hyphens";
  const applyNamespace = (event?: FormEvent) => {
    event?.preventDefault();
    const next = draft.trim();
    if (!NAMESPACE_PATTERN.test(next) || next === namespace) return;
    setLast(null);
    setStage("");
    setView({ status: "loading" });
    setNamespace(next);
  };

  const run = async (action: DemoAction) => {
    setBusy(action.id);
    try {
      const { status, data } = await api<ActionResponse>("POST", actionPath(namespace, action.act), action.body ?? {});
      setLast({ text: summarize(action.label, status, data), ok: succeeded(status, data), data });
      const following = nextStage(stage, action, status, data);
      if (following === stage) await refresh(namespace, stage);
      else setStage(following);
    } catch (error) {
      setLast({ text: `${action.label}: ${error instanceof Error ? error.message : "request failed"}`, ok: false, data: null });
    } finally {
      setBusy(null);
    }
  };

  const reloadAll = async () => {
    setLast(null);
    setBusy("refresh");
    if (legacy === "error") setLegacy(null);
    await Promise.all([refresh(namespace, stage), loadLegacy()]);
    setBusy(null);
  };

  const data = view.status === "ready" ? view.data : null;
  const payments = data?.snapshot.payments ?? [];
  const step = progress(data !== null, payments);
  const primaryId = (mainFlow[step] ?? mainFlow[0]).id;
  const actual: Totals | undefined = data?.reconciliation.actual;

  const actionButton = (action: DemoAction) => {
    const enabled = isEnabled(action, payments);
    return (
      <Button
        key={action.id}
        variant={action.id === primaryId ? "primary" : "secondary"}
        size="sm"
        loading={busy === action.id}
        disabled={!enabled || (busy !== null && busy !== action.id)}
        title={enabled ? undefined : `Enabled once ${action.requiresReturned} is RETURNED (after step 5)`}
        onClick={() => void run(action)}
      >
        {action.label}
      </Button>
    );
  };
  const blockedDuplicate = duplicateActions.find(a => !isEnabled(a, payments));

  const failedChecks = recon ? [...recon.invariants, ...recon.provider_reconciliation].filter(c => !c.ok) : [];
  const passedInvariants = recon ? recon.invariants.filter(c => c.ok).length : 0;

  return (
    <main className={styles.page}>
      <header className={styles.header}>
        <div className={styles.titleRow}>
          <h1 className={styles.title}>Batch to event-driven payments</h1>
          <ThemeSwitch
            theme={theme}
            iconOnly
            label="Switch theme"
            onThemeChange={next => {
              if (!document.startViewTransition) return setTheme(next);
              document.startViewTransition(() => setTheme(next));
            }}
          />
        </div>
        <div className={styles.meta}>
          <Badge tone="warning">Synthetic data, deterministic simulated provider</Badge>
          <Badge tone="info">Backend: {backendLabel}</Badge>
          {data && <span className={styles.muted}>Run {data.snapshot.run} · provider {data.provider.name} ({data.provider.mode}) · store {data.store}</span>}
        </div>
      </header>

      <section className={styles.card} aria-labelledby="actions-heading">
        <div className={styles.cardHead}>
          <h2 id="actions-heading" className={styles.heading}>Demo actions</h2>
          <form className={styles.namespace} onSubmit={applyNamespace}>
            <Input
              label="Namespace"
              value={draft}
              onChange={event => setDraft(event.target.value)}
              onBlur={() => applyNamespace()}
              error={draftError}
              spellCheck={false}
              autoComplete="off"
            />
          </form>
        </div>
        <Stepper
          label="Demo progress"
          current={step}
          completeLabel="Main flow complete"
          steps={[
            { id: "reset", label: "Reset", description: "Fresh run in this namespace" },
            { id: "submit", label: "Submit", description: "Reserve funds, call provider" },
            { id: "settle", label: "Settle", description: "Post cash, match legacy" },
            { id: "return", label: "Return", description: "Compensating entry" },
          ]}
        />
        <div className={styles.groups}>
          <div className={styles.group} role="group" aria-labelledby="flow-heading">
            <h3 id="flow-heading" className={styles.subheading}>Main flow</h3>
            <div className={styles.buttons}>{mainFlow.map(actionButton)}</div>
          </div>
          <div className={styles.group} role="group" aria-labelledby="dup-heading">
            <h3 id="dup-heading" className={styles.subheading}>Duplicates and retries</h3>
            <div className={styles.buttons}>{duplicateActions.map(actionButton)}</div>
            {blockedDuplicate && (
              <p className={styles.hint}>{blockedDuplicate.label.split(" ")[0]} is enabled once {blockedDuplicate.requiresReturned} is RETURNED (after step 5), so it can only ever be a duplicate.</p>
            )}
          </div>
          <div className={styles.group} role="group" aria-labelledby="ops-heading">
            <h3 id="ops-heading" className={styles.subheading}>Operations</h3>
            <div className={styles.buttons}>
              {operationActions.map(actionButton)}
              <Button variant="ghost" size="sm" loading={busy === "refresh"} disabled={busy !== null && busy !== "refresh"} onClick={() => void reloadAll()}>
                Refresh
              </Button>
            </div>
          </div>
        </div>
        <p className={styles.last} role="status" aria-live="polite">
          {last ? <><Badge tone={last.ok ? "success" : "danger"} size="sm">{last.ok ? "OK" : "Check"}</Badge> {last.text}</>
            : <span className={styles.muted}>Choose an action. Reset affects only the selected demo-* namespace.</span>}
        </p>
      </section>

      <div className={styles.split}>
        <section className={styles.card} aria-labelledby="legacy-heading" aria-busy={legacy === null}>
          <div className={styles.cardHead}>
            <h2 id="legacy-heading" className={styles.heading}>Original batch outcome (legacy/batch.py)</h2>
            {legacy && legacy !== "error" && (
              <Badge tone={legacy.matches_golden ? "success" : "danger"}>{legacy.matches_golden ? "Matches golden" : "Golden mismatch"}</Badge>
            )}
          </div>
          {legacy === null && <Skeleton lines={4} label="Loading legacy baseline" />}
          {legacy === "error" && (
            <Alert tone="danger" title="Legacy baseline could not load">
              <Button variant="secondary" size="sm" onClick={() => { setLegacy(null); void loadLegacy(); }}>Load baseline again</Button>
            </Alert>
          )}
          {legacy && legacy !== "error" && legacy.result && (
            <>
              <div className={styles.kpis}>
                <Money label="Cash" value={legacy.result.cash_cents} context="After the end-of-day batch" />
                <Money label="Settled" value={legacy.result.settled_cents} context="All three payments" />
                <Money label="Debits and credits" value={legacy.result.debit_cents} context="Each side, balanced" />
              </div>
              <p className={styles.muted}>
                {Object.entries(legacy.result.statuses).map(([id, status]) => `${id}: ${status}`).join(" · ")}. One end-of-day transaction: no provider, no async events, no return workflow.
              </p>
            </>
          )}
        </section>

        <section className={styles.card} aria-labelledby="modern-heading" aria-busy={view.status === "loading"}>
          <h2 id="modern-heading" className={styles.heading}>Modern outcome</h2>
          {view.status === "loading" && <Skeleton lines={4} label="Loading balances" />}
          {view.status === "missing" && (
            <EmptyState
              title="This namespace has no run yet"
              description={`${view.message}. Start a fresh run to load balances.`}
              action={<Button variant="secondary" size="sm" disabled={busy !== null} onClick={() => void run(mainFlow[0])}>Reset {namespace}</Button>}
            />
          )}
          {view.status === "error" && (
            <Alert tone="danger" title="State could not load">
              {view.message}. <Button variant="secondary" size="sm" onClick={() => void reloadAll()}>Load state again</Button>
            </Alert>
          )}
          {actual && (
            <div className={styles.kpis}>
              <Money label="Cash (posted)" value={actual.cash_cents} context={`Opening ${cents(actual.opening_cash_cents)}`} />
              <Money label="Available" value={actual.available_cents} context="Cash minus reserved" />
              <Money label="Reserved" value={actual.reserved_cents} context="Held until settlement" />
              <Money label="Debits" value={actual.debit_cents} context="Journal total" />
              <Money label="Credits" value={actual.credit_cents} context="Journal total" />
              <Money label="Net settled outflow" value={actual.net_settled_outflow_cents} context="Settled minus returned" />
              <Money label="Returned" value={actual.returned_cents} context="Compensated by returns" />
            </div>
          )}
        </section>
      </div>

      {data && (
        <>
          <section className={styles.card} aria-labelledby="payments-heading">
            <h2 id="payments-heading" className={styles.heading}>Payments</h2>
            <div className={styles.tableScroll}>
              <SortableDataTable
                caption="Payments"
                rowKey="payment_id"
                emptyMessage="No payments yet. Submit fixtures to create them."
                rows={payments.map(p => ({ ...p }))}
                defaultSort={{ key: "payment_id", direction: "asc" }}
                columns={[
                  { key: "payment_id", label: "Payment", sortable: true },
                  { key: "amount_cents", label: "Amount", numeric: true, sortable: true, render: v => cents(v as number) },
                  { key: "status", label: "Status", sortable: true, render: (v, row) => (
                    <span className={styles.inline}>
                      <Badge tone={statusTone(String(v))} size="sm">{String(v)}</Badge>
                      {row.pending_return ? <Badge tone="info" size="sm">Return parked</Badge> : null}
                    </span>
                  ) },
                  { key: "provider_payment_id", label: "Provider ref", render: v => <Code value={v} /> },
                  { key: "last_submission_outcome", label: "Last submission" },
                ]}
              />
            </div>
          </section>

          <section className={styles.card} aria-labelledby="recon-heading">
            <div className={styles.cardHead}>
              <h2 id="recon-heading" className={styles.heading}>Reconciliation</h2>
              <div className={styles.tableScroll}>
                <SegmentedControl label="Expected stage" value={stage} onValueChange={value => setStage(value as Stage)} options={stages} />
              </div>
            </div>
            {!recon && <Skeleton lines={2} label="Loading reconciliation" />}
            {recon && (
              <Alert tone={recon.status === "PASS" ? "success" : "danger"} title={`${recon.status} · ${stageLabel((recon.stage ?? "") as Stage)}`}>
                Unexplained differences: {recon.unexplained_differences}. Invariant checks passed: {passedInvariants}/{recon.invariants.length}.
                {recon.stage === "after_settlement" && " Expected values are the legacy golden batch result."}
              </Alert>
            )}
            {recon && recon.differences.length > 0 && (
              <div className={styles.tableScroll}>
                <SortableDataTable caption="Expected and actual differences" rowKey="field"
                  rows={recon.differences.map(d => ({ ...d }))}
                  columns={[{ key: "field", label: "Field" }, { key: "expected", label: "Expected", render: json }, { key: "actual", label: "Actual", render: json }]} />
              </div>
            )}
            {failedChecks.length > 0 && (
              <div className={styles.tableScroll}>
                <SortableDataTable caption="Failed checks" rowKey="check"
                  rows={failedChecks.map(c => ({ ...c }))}
                  columns={[{ key: "check", label: "Check" }, { key: "expected", label: "Expected", render: json }, { key: "actual", label: "Actual", render: json }]} />
              </div>
            )}
            {recon && recon.explained_exceptions.length > 0 && (
              <div className={styles.tableScroll}>
                <SortableDataTable caption="Explained exceptions" rowKey="key"
                  rows={recon.explained_exceptions.map((e, i) => ({ key: String(i), item: e.payment_id || e.event_id || "", explanation: e.explanation }))}
                  columns={[{ key: "item", label: "Item" }, { key: "explanation", label: "Explanation" }]} />
              </div>
            )}
          </section>

          <section className={styles.card} aria-labelledby="journal-heading">
            <h2 id="journal-heading" className={styles.heading}>Immutable journal</h2>
            <div className={styles.tableScroll}>
              <SortableDataTable
                caption="Journal entries"
                rowKey="entry_id"
                emptyMessage="No journal entries yet. Settlement posts the first pair."
                rows={data.snapshot.journal.map(j => ({ ...j }))}
                columns={[
                  { key: "entry_id", label: "Entry", render: v => <Code value={v} /> },
                  { key: "effect", label: "Effect", sortable: true },
                  { key: "side", label: "Side" },
                  { key: "account", label: "Account" },
                  { key: "amount_cents", label: "Amount", numeric: true, render: v => cents(v as number) },
                  { key: "event_id", label: "Event", render: v => <Code value={v} /> },
                  { key: "posted_at", label: "Posted", sortable: true },
                ]}
              />
            </div>
          </section>

          <section className={styles.card} aria-labelledby="events-heading">
            <h2 id="events-heading" className={styles.heading}>Event history</h2>
            <p className={styles.muted}>Every delivery, including duplicates and rejections. Newest first.</p>
            <div className={styles.tableScroll}>
              <SortableDataTable
                caption="Event deliveries"
                rowKey="key"
                emptyMessage="No provider events yet."
                rows={data.snapshot.deliveries.map((d, i) => ({ ...d, key: String(i) })).reverse()}
                columns={[
                  { key: "received_at", label: "Received" },
                  { key: "event_id", label: "Event ID", render: v => <Code value={v} /> },
                  { key: "type", label: "Type" },
                  { key: "payment_id", label: "Payment" },
                  { key: "outcome", label: "Outcome", render: v => <Badge tone={outcomeTone(String(v))} size="sm">{String(v)}</Badge> },
                  { key: "reason", label: "Reason" },
                ]}
              />
            </div>
          </section>
        </>
      )}

      <section className={styles.card} aria-labelledby="raw-heading">
        <h2 id="raw-heading" className={styles.heading}>Last API response</h2>
        {last?.data != null
          ? <JsonViewer data={last.data} rootName="response" defaultExpandDepth={2} maxHeight={360} label="Last API response" />
          : <EmptyState title="No response yet" description="Run an action to see the raw API response here." />}
      </section>
    </main>
  );
}
