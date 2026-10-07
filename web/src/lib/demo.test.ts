import { describe, expect, it } from "vitest";
import { actionPath, duplicateActions, isEnabled, mainFlow, nextStage, progress, summarize, type Payment } from "./demo";

const byId = (id: string) => [...mainFlow, ...duplicateActions].find(a => a.id === id)!;
const pay = (payment_id: string, status: string): Payment => ({ payment_id, status, amount_cents: 100 });
const settled = [pay("PAY-001", "SETTLED"), pay("PAY-002", "SETTLED"), pay("PAY-003", "SETTLED")];
const returned = [pay("PAY-001", "SETTLED"), pay("PAY-002", "RETURNED"), pay("PAY-003", "SETTLED")];

describe("6b duplicate return guard", () => {
  const sixB = byId("duplicate-return");

  it("posts a new-ID return event for PAY-002 through the simulated provider-event route", () => {
    expect(actionPath("demo-local", sixB.act)).toBe("/api/namespaces/demo-local/demo/provider-event");
    expect(sixB.body).toEqual({ payment_id: "PAY-002", type: "returned", variant: 2 });
  });

  it("stays disabled until PAY-002 is RETURNED, so it can never apply the first return", () => {
    expect(isEnabled(sixB, [])).toBe(false);
    expect(isEnabled(sixB, settled)).toBe(false);
    expect(isEnabled(sixB, returned)).toBe(true);
  });

  it("leaves every other action enabled", () => {
    for (const a of [...mainFlow, ...duplicateActions].filter(a => a.id !== "duplicate-return")) {
      expect(isEnabled(a, [])).toBe(true);
    }
  });
});

describe("reconciliation stage", () => {
  const ok = { results: [{ outcome: "APPLIED", http_status: 200 }] };

  it("moves only after a successful action", () => {
    expect(nextStage("", byId("settle"), 200, ok)).toBe("after_settlement");
    expect(nextStage("after_settlement", byId("return"), 200, ok)).toBe("after_return");
    expect(nextStage("after_return", byId("duplicate-return"), 200, { results: [{ outcome: "DUPLICATE_EFFECT" }] })).toBe("after_return");
    expect(nextStage("after_return", byId("reset"), 200, {})).toBe("");
  });

  it("keeps the current stage when the action fails", () => {
    expect(nextStage("after_settlement", byId("return"), 409, { error: { code: "invalid_transition" } })).toBe("after_settlement");
    expect(nextStage("", byId("return"), 200, { results: [{ http_status: 422, error: { code: "event_rejected" } }] })).toBe("");
  });

  it("does not switch to after settlement when there was nothing to settle", () => {
    expect(nextStage("", byId("settle"), 200, { results: [], message: "nothing to settle" })).toBe("");
  });
});

describe("summary and progress", () => {
  it("summarizes outcomes, then messages, then errors", () => {
    expect(summarize("6b", 200, { results: [{ outcome: "DUPLICATE_EFFECT" }] })).toBe("6b: HTTP 200 · DUPLICATE_EFFECT");
    expect(summarize("3 · Settle", 200, { results: [], message: "nothing to settle" })).toBe("3 · Settle: HTTP 200 · nothing to settle");
    expect(summarize("x", 400, { error: { message: "bad body" } })).toBe("x: HTTP 400 · bad body");
  });

  it("derives the next main-flow step from state", () => {
    expect(progress(false, [])).toBe(0);
    expect(progress(true, [])).toBe(1);
    expect(progress(true, [pay("PAY-001", "SUBMITTED")])).toBe(2);
    expect(progress(true, settled)).toBe(3);
    expect(progress(true, returned)).toBe(mainFlow.length);
  });
});
