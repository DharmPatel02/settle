// k6 load test for POST /v1/transfers.
//
//   k6 run -e API_KEY=<key> -e SCENARIO=steady -e RATE=100 -e LABEL=baseline transfers.js
//
// SCENARIO: smoke | steady | spike | hot. Accounts come from seed.py (accounts.json).
// The raw summary is saved to docs/perf-results/<scenario>-<label>.json.
import http from "k6/http";
import { check } from "k6";
import { Counter } from "k6/metrics";
import { SharedArray } from "k6/data";
import exec from "k6/execution";
import { textSummary } from "https://jslib.k6.io/k6-summary/0.1.0/index.js";

const BASE_URL = __ENV.BASE_URL || "http://localhost:8000";
const SCENARIO = __ENV.SCENARIO || "smoke";
const RATE = Number(__ENV.RATE || 100);
const DURATION = __ENV.DURATION || "2m";
const LABEL = __ENV.LABEL || "run";

const seeded = JSON.parse(open("./accounts.json"));
const accounts = new SharedArray("accounts", () => seeded.accounts);
const hot = new SharedArray("hot", () => seeded.hot);

const completed = new Counter("transfers_completed");
const held = new Counter("transfers_pending_review");
const refused = new Counter("transfers_declined_or_rejected");

// 202 (held for review) and 422 (declined by risk, or insufficient funds) are correct business
// answers, not failures. Anything else (5xx, 409, 429) counts against the error-rate threshold.
http.setResponseCallback(http.expectedStatuses(201, 202, 422));

const scenarios = {
  smoke: { executor: "constant-vus", vus: 1, duration: "30s" },
  steady: {
    executor: "constant-arrival-rate",
    rate: RATE,
    timeUnit: "1s",
    duration: DURATION,
    preAllocatedVUs: 50,
    maxVUs: 500,
  },
  spike: {
    executor: "ramping-arrival-rate",
    startRate: Math.round(RATE / 4),
    timeUnit: "1s",
    preAllocatedVUs: 100,
    maxVUs: 1000,
    stages: [
      { target: Math.round(RATE / 4), duration: "30s" },
      { target: RATE * 2, duration: "10s" }, // sudden jump to 8x the base rate
      { target: RATE * 2, duration: "30s" },
      { target: Math.round(RATE / 4), duration: "10s" },
      { target: Math.round(RATE / 4), duration: "30s" },
    ],
  },
  // Many transfers out of the same few accounts: every request needs the same row locks.
  hot: {
    executor: "constant-arrival-rate",
    rate: RATE,
    timeUnit: "1s",
    duration: DURATION,
    preAllocatedVUs: 50,
    maxVUs: 500,
  },
};

export const options = {
  scenarios: { [SCENARIO]: scenarios[SCENARIO] },
  thresholds: {
    http_req_failed: ["rate<0.01"],
    http_req_duration: ["p(95)<250"],
  },
  summaryTrendStats: ["avg", "min", "med", "p(90)", "p(95)", "p(99)", "max"],
};

function pick(list) {
  return list[Math.floor(Math.random() * list.length)];
}

export default function () {
  const source = SCENARIO === "hot" ? pick(hot) : pick(accounts);
  let destination = pick(accounts);
  while (destination === source) destination = pick(accounts);

  const response = http.post(
    `${BASE_URL}/v1/transfers`,
    JSON.stringify({
      from_account_id: source,
      to_account_id: destination,
      amount_minor: 100 + Math.floor(Math.random() * 4900),
      currency: "USD",
    }),
    {
      headers: {
        "Content-Type": "application/json",
        "X-API-Key": __ENV.API_KEY,
        "Idempotency-Key": `k6-${exec.scenario.iterationInTest}-${exec.vu.idInTest}-${Date.now()}`,
      },
      tags: { name: "POST /v1/transfers" },
    },
  );
  check(response, { "business answer (201/202/422)": (r) => [201, 202, 422].includes(r.status) });
  if (response.status === 201) completed.add(1);
  else if (response.status === 202) held.add(1);
  else if (response.status === 422) refused.add(1);
}

export function handleSummary(data) {
  const name = `${SCENARIO}-${LABEL}`;
  return {
    [`../docs/perf-results/${name}.json`]: JSON.stringify(data, null, 2),
    stdout: textSummary(data, { indent: " ", enableColors: false }),
  };
}
