# ReAct Agent demo assets

This directory contains three ready-to-use assets:

| File | Purpose |
|---|---|
| [`autonomous-react-agent.node.json`](autonomous-react-agent.node.json) | Frontend node-catalog descriptor with all 18 implemented input keys, dummy values, four outputs, tool schemas, guardrails, approval rules, and reliability settings |
| [`ecommerce-react-agent-full-flow.json`](ecommerce-react-agent-full-flow.json) | Complete Agent Studio flow testing outer HITL, policy context, ReAct tools, approvals, public demo APIs, status routing, and structured output |
| [`AutonomousReactAgentDemo.jsx`](AutonomousReactAgentDemo.jsx) | Single client-side JSX component for a Next.js app using `@xyflow/react`; includes palette drag/drop, custom node rendering, click inspector, editors, copy, and download |
| [`ecommerce-react-agent-sample-output.json`](ecommerce-react-agent-sample-output.json) | Illustrative PAUSED payload, resume decision, and final structured API response |

## 1. Use the node JSON in a node library

List `autonomous-react-agent.node.json` with the other draggable nodes. Preserve these backend keys when a node is dropped:

```text
node_id
name = Autonomous ReAct Agent
displayName
type = agent
description
interrupt
next
inputParameters
outputParameters
```

The backend registry lookup uses `name`, so it must remain exactly:

```text
Autonomous ReAct Agent
```

Generate a unique `node_id` for each dropped instance. Users may change `displayName`.

## 2. Use the single JSX file in Next.js

Install React Flow:

```bash
npm install @xyflow/react
```

Copy:

```text
examples/AutonomousReactAgentDemo.jsx
```

into the Next.js project, for example:

```text
components/AutonomousReactAgentDemo.jsx
```

Then render it from an App Router page:

```jsx
import AutonomousReactAgentDemo from "@/components/AutonomousReactAgentDemo";

export default function Page() {
  return <AutonomousReactAgentDemo />;
}
```

The file already contains `"use client"`, React Flow styles, the full node schema, the custom node component, palette, canvas, inspector, and inline CSS. It has no Tailwind or icon-library dependency.

## 3. Run the full-power flow

Upload/save `ecommerce-react-agent-full-flow.json` as an Agent Studio flow.

The flow uses public demonstration services:

- `https://dummyjson.com` for cart read/delete simulation;
- `https://jsonplaceholder.typicode.com` for mock refund, support-case, and notification records.

These services do not perform real cancellation, payment, ticket, or communication actions.

### Required authentication scopes

The v2 node hides privileged tools unless runtime scopes contain all required scopes. For the API-key demo boundary:

```env
AUTH_REQUIRED=true
AGENT_API_KEY="replace-with-a-long-random-secret"
AGENT_API_SCOPES="agent:access,agent:invoke,cart:read,cart:cancel,refund:issue,case:create,notification:send"
AGENT_API_ROLES="service,commerce"
AGENT_API_TENANT_ID="demo-tenant"
```

Send:

```http
X-API-Key: replace-with-a-long-random-secret
X-User-ID: 1
```

The sample `resource_policy` requires each tool's `user_id` argument to equal the trusted runtime user ID, so use user `1` in the request and test prompt.

### Test sequence

1. Invoke the flow with a stable session/thread. The outer `Question Node` pauses.
2. Resume with:

```text
For user 1, inspect cart 1, cancel it, issue a mock refund of 25.50 because of a duplicate order, create a support case, and notify customer@example.com.
```

3. The read tool can execute automatically.
4. Cancellation pauses for approval.
5. Mock refund pauses for approval and supports argument editing.
6. Support-case creation pauses for approval.
7. Mock customer notification pauses for approval.
8. Resume each approval with the same outer thread ID and returned tool request ID.
9. The final Decision Node routes by `react_status`.
10. The End Node returns the complete `agent_result` object.

The model may choose a different safe ordering, but writes remain approval-gated and idempotency-journaled.

## 4. Easy use cases with the existing nodes

### A. Fashion search assistant

Reuse the attached ontology flow:

```text
Question Node
  -> Vocabulary Extractor
  -> Canonical Resolver
  -> Cypher Query Builder
  -> API caller
  -> ReAct Agent
  -> End Node
```

Pass `{{ontology_filters}}` and `{{neo4j_result}}` into the ReAct system prompt. Let ReAct compare products, explain trade-offs, and ask one refinement question. Keep Cypher construction/execution deterministic in the outer graph.

### B. Shopping cart resolution

Use the supplied full flow. It tests read, ownership, approval, cancellation, refund, support case, notification, retry, idempotency, and structured output.

### C. Ticket-handling chatbot

Expose:

- KB search as `read`;
- ticket read as `read_sensitive`;
- ticket create/update as `reversible_write`;
- ticket close as `high_impact_write`;
- customer email as `external_communication`.

Use the `support` profile and route `react_status` to success, escalation, failure, or cancellation End Nodes.

### D. Database analyst

Keep `Chat with DB` or query-generation nodes outside ReAct when SQL policy must be deterministic. Pass the result to an `analyst` ReAct node for explanation, comparison, and report generation. Use read-only database credentials.

### E. CMDB/ontology operations assistant

Keep Vocabulary Extractor, Canonical Resolver, and Cypher Query Builder as deterministic outer nodes. Give ReAct only approved read tools plus a separately approval-gated change-plan tool. Never give a model unrestricted production Cypher execution.

### F. Approval-enabled notification assistant

Use `Send Email` or an allowlisted notification API as an `external_communication` tool. The Tool Gateway pauses automatically and resumes the same inner agent after approve/edit/reject.

## 5. Production changes before using real systems

- replace public demo URLs with approved service endpoints;
- use HTTPS and `execution.allowed_hosts`;
- use OIDC/JWT or a trusted gateway for end-user identity;
- set narrow server-owned scopes;
- enforce resource ownership again in downstream services;
- define real input/output schemas;
- pass stable idempotency keys to all side-effect APIs;
- replace simulated refund/cancellation/notification endpoints;
- test approve, edit, reject, timeout, provider failure, retry, and replay;
- rotate all previously exposed credentials and keep secrets outside node JSON.
