# Agent Studio — Tool Catalog

> **Scope:** Drop-in tool definitions for the **Autonomous ReAct Agent v2** node.  
> Each JSON block can be pasted directly into the `tools` input-parameter array.  
> Every `node_type` maps 1-to-1 to a registered executor in `app/nodes/`.  
> All `{{UPPER_CASE}}` tokens are flow-level environment variables (never sent to the LLM); lower-case `{{tokens}}` are LLM-supplied arguments.

---

## Table of Contents

| # | Category | Tools |
|---|----------|-------|
| 1 | [Communication & Notifications](#1-communication--notifications) | Slack, Teams, SMS, WhatsApp |
| 2 | [Productivity & Scheduling](#2-productivity--scheduling) | Google Calendar, Google Sheets, Jira, ServiceNow |
| 3 | [Data & File Processing](#3-data--file-processing) | PDF Extractor, CSV Analyzer, URL Scraper, JSON Transformer |
| 4 | [AI & Intelligence](#4-ai--intelligence) | Vision Analyzer, Translator, PII Redactor, Sentiment Scorer |
| 5 | [Finance & Commerce](#5-finance--commerce) | Currency Converter, Invoice Builder, Stock Quote |
| 6 | [Developer & System](#6-developer--system) | GitHub Issue, Webhook Trigger, DateTime Calculator, Code Reviewer |
| 7 | [Knowledge & Research](#7-knowledge--research) | Wikipedia, News Digest, Patent Search |
| 8 | [Security & Compliance](#8-security--compliance) | Password Strength Checker, URL Safety Scanner |

---

## 1. Communication & Notifications

### 1.1 Slack Message Sender

**Use-case:** After completing a complex analysis or detecting an anomaly, the agent posts a structured summary to a Slack channel so the team is notified in real-time — without any human having to open a dashboard.

> *Example:* An IT ops agent detects a server CPU spike above 90 %, composes a concise alert with server name and metric, and fires it to `#infra-alerts`.

```json
{
  "_pattern": "REST POST — Slack Incoming Webhook",
  "name": "Post To Slack",
  "description": "Post a message to a Slack channel. Use this to notify the team of important findings, alerts, or summaries. Only call once per conversation unless the user explicitly asks for another notification.",
  "node_type": "API caller",
  "config": {
    "url": "{{SLACK_WEBHOOK_URL}}",
    "method": "POST",
    "headers": { "Content-Type": "application/json" },
    "data": {
      "text": "{{message}}",
      "channel": "{{channel}}"
    },
    "timeout": "10"
  },
  "parameters": [
    {
      "name": "message",
      "type": "string",
      "required": true,
      "description": "The message to post. Keep it under 300 characters. Use plain text only.",
      "example": "🚨 CPU spike detected on prod-api-01: 94 % for 5 min."
    },
    {
      "name": "channel",
      "type": "string",
      "required": true,
      "description": "Slack channel name including the # prefix.",
      "example": "#infra-alerts"
    }
  ]
}
```

---

### 1.2 Microsoft Teams Notification

**Use-case:** Enterprise workflows where the company uses Teams instead of Slack. The agent sends an adaptive-card style message to a Teams channel via an Incoming Webhook, enabling managers to receive real-time updates without leaving Teams.

> *Example:* A sales agent closes a deal summary and pushes a Teams card to `#sales-wins` with customer name, deal value, and owner.

```json
{
  "_pattern": "REST POST — Teams Incoming Webhook",
  "name": "Send Teams Notification",
  "description": "Send a notification card to a Microsoft Teams channel. Use after completing a task that requires human awareness — deal closed, ticket escalated, report ready.",
  "node_type": "API caller",
  "config": {
    "url": "{{TEAMS_WEBHOOK_URL}}",
    "method": "POST",
    "headers": { "Content-Type": "application/json" },
    "data": {
      "@type": "MessageCard",
      "@context": "http://schema.org/extensions",
      "themeColor": "0076D7",
      "summary": "{{title}}",
      "sections": [
        {
          "activityTitle": "{{title}}",
          "activityText": "{{body}}"
        }
      ]
    },
    "timeout": "10"
  },
  "parameters": [
    {
      "name": "title",
      "type": "string",
      "required": true,
      "description": "Short headline for the card (max 80 chars).",
      "example": "New Deal Closed — Acme Corp $45,000"
    },
    {
      "name": "body",
      "type": "string",
      "required": true,
      "description": "Card body with supporting details.",
      "example": "Owner: Jane Smith | Product: Enterprise Plan | Close date: 2026-08-16"
    }
  ]
}
```

---

### 1.3 SMS Sender (Twilio)

**Use-case:** High-urgency alerts or OTP/confirmation flows where email is too slow. The agent triggers an SMS to a verified phone number — for example, notifying an on-call engineer of a critical incident.

> *Example:* A monitoring agent detects a payment gateway timeout, looks up the on-call number from the database, and fires an SMS: *"ALERT: Payment gateway down. Check dashboard immediately."*

```json
{
  "_pattern": "REST POST — Twilio SMS API (form-encoded)",
  "name": "Send SMS",
  "description": "Send a short text message to a phone number via Twilio. Use only for urgent alerts or confirmations that the user has explicitly requested via SMS. Never include sensitive data in the body.",
  "node_type": "API caller",
  "config": {
    "url": "https://api.twilio.com/2010-04-01/Accounts/{{TWILIO_ACCOUNT_SID}}/Messages.json",
    "method": "POST",
    "headers": {
      "Content-Type": "application/x-www-form-urlencoded",
      "Authorization": "Basic {{TWILIO_AUTH_TOKEN_B64}}"
    },
    "data": "To={{to_number}}&From={{TWILIO_FROM_NUMBER}}&Body={{message}}",
    "timeout": "15"
  },
  "parameters": [
    {
      "name": "to_number",
      "type": "string",
      "required": true,
      "description": "Recipient phone number in E.164 format.",
      "example": "+14155552671"
    },
    {
      "name": "message",
      "type": "string",
      "required": true,
      "description": "SMS body. Max 160 characters. No PII, passwords, or links to internal systems.",
      "example": "Your verification code is 482910. Expires in 10 minutes."
    }
  ]
}
```

---

### 1.4 WhatsApp Message (360Dialog)

**Use-case:** Customer-facing assistants in markets where WhatsApp is the primary communication channel (India, LatAm, MEA). The agent sends a template or free-form message back to the customer's WhatsApp number after completing a task.

> *Example:* An e-commerce agent confirms an order placement and immediately sends the order confirmation to the customer's WhatsApp.

```json
{
  "_pattern": "REST POST — 360Dialog / Meta WhatsApp Cloud API",
  "name": "Send WhatsApp Message",
  "description": "Send a WhatsApp text message to a customer. Use only when the workflow explicitly includes WhatsApp delivery. Ensure the message is concise and professional.",
  "node_type": "API caller",
  "config": {
    "url": "https://waba.360dialog.io/v1/messages",
    "method": "POST",
    "headers": {
      "Content-Type": "application/json",
      "D360-API-KEY": "{{WHATSAPP_API_KEY}}"
    },
    "data": {
      "messaging_product": "whatsapp",
      "recipient_type": "individual",
      "to": "{{to_number}}",
      "type": "text",
      "text": { "body": "{{message}}" }
    },
    "timeout": "15"
  },
  "parameters": [
    {
      "name": "to_number",
      "type": "string",
      "required": true,
      "description": "Customer WhatsApp number in E.164 format.",
      "example": "+919876543210"
    },
    {
      "name": "message",
      "type": "string",
      "required": true,
      "description": "Message body. Max 4096 characters. Plain text only.",
      "example": "Hi Priya! Your order #ORD-8821 has been confirmed. Estimated delivery: Aug 18."
    }
  ]
}
```

---

## 2. Productivity & Scheduling

### 2.1 Google Calendar Event Creator

**Use-case:** After discussing meeting logistics with a user, the agent books the meeting directly on Google Calendar — eliminating the copy-paste between chat and calendar.

> *Example:* An executive assistant agent hears "Schedule a 30-min sync with the product team next Tuesday at 3 PM" and creates the event with attendees via the Calendar API.

```json
{
  "_pattern": "REST POST — Google Calendar Events API",
  "name": "Create Calendar Event",
  "description": "Create an event on Google Calendar. Requires the event title, start time, end time (ISO 8601), and optionally attendee emails. Confirm the time and attendees with the user before calling.",
  "node_type": "API caller",
  "config": {
    "url": "https://www.googleapis.com/calendar/v3/calendars/primary/events",
    "method": "POST",
    "headers": {
      "Authorization": "Bearer {{GOOGLE_ACCESS_TOKEN}}",
      "Content-Type": "application/json"
    },
    "data": {
      "summary": "{{title}}",
      "description": "{{description}}",
      "start": { "dateTime": "{{start_time}}", "timeZone": "{{timezone}}" },
      "end":   { "dateTime": "{{end_time}}",   "timeZone": "{{timezone}}" },
      "attendees": "{{attendees}}"
    },
    "timeout": "15"
  },
  "parameters": [
    {
      "name": "title",
      "type": "string",
      "required": true,
      "description": "Short event title.",
      "example": "Product Team Sync"
    },
    {
      "name": "start_time",
      "type": "string",
      "required": true,
      "description": "Event start in ISO 8601 with timezone offset.",
      "example": "2026-08-19T15:00:00+05:30"
    },
    {
      "name": "end_time",
      "type": "string",
      "required": true,
      "description": "Event end in ISO 8601 with timezone offset.",
      "example": "2026-08-19T15:30:00+05:30"
    },
    {
      "name": "timezone",
      "type": "string",
      "required": true,
      "description": "IANA timezone string.",
      "example": "Asia/Kolkata"
    },
    {
      "name": "description",
      "type": "string",
      "required": false,
      "description": "Optional agenda or notes for the event.",
      "example": "Agenda: Q3 roadmap review and sprint planning."
    },
    {
      "name": "attendees",
      "type": "array",
      "required": false,
      "description": "List of attendee objects [{\"email\": \"...\"}].",
      "example": [{"email": "raj@company.com"}, {"email": "meera@company.com"}]
    }
  ]
}
```

---

### 2.2 Google Sheets Read Row

**Use-case:** Agents that need live tabular data (pricing tables, employee rosters, configuration parameters) stored in a shared spreadsheet — no database required.

> *Example:* An HR agent fetches an employee's leave balance from a Google Sheet using their email as the lookup key.

```json
{
  "_pattern": "REST GET — Google Sheets Values API",
  "name": "Read Google Sheet",
  "description": "Read a range of cells from a Google Spreadsheet. Use when live tabular data is needed (pricing, rosters, configs). Returns the raw cell values as a 2-D array.",
  "node_type": "API caller",
  "config": {
    "url": "https://sheets.googleapis.com/v4/spreadsheets/{{SHEET_ID}}/values/{{range}}",
    "method": "GET",
    "headers": { "Authorization": "Bearer {{GOOGLE_ACCESS_TOKEN}}" },
    "timeout": "15"
  },
  "parameters": [
    {
      "name": "range",
      "type": "string",
      "required": true,
      "description": "A1 notation range, e.g. Sheet1!A1:E50 or a named range.",
      "example": "Employees!A2:F100"
    }
  ]
}
```

---

### 2.3 Google Sheets Append Row

**Use-case:** Write structured results (form submissions, extracted entities, audit logs) back to a shared sheet — giving non-technical stakeholders a real-time view of what the agent is doing.

> *Example:* A lead-capture agent appends a new row with contact details every time a visitor fills in the chat widget.

```json
{
  "_pattern": "REST POST — Google Sheets Append API",
  "name": "Append To Google Sheet",
  "description": "Append a new row of data to a Google Spreadsheet. Use to log extracted data, form fills, or audit events that non-technical users track in Sheets.",
  "node_type": "API caller",
  "config": {
    "url": "https://sheets.googleapis.com/v4/spreadsheets/{{SHEET_ID}}/values/{{SHEET_NAME}}!A1:append?valueInputOption=USER_ENTERED",
    "method": "POST",
    "headers": {
      "Authorization": "Bearer {{GOOGLE_ACCESS_TOKEN}}",
      "Content-Type": "application/json"
    },
    "data": { "values": "{{row_values}}" },
    "timeout": "15"
  },
  "parameters": [
    {
      "name": "row_values",
      "type": "array",
      "required": true,
      "description": "A 2-D array representing one or more rows. Each inner array is a row of cell values.",
      "example": [["2026-08-16", "John Doe", "john@example.com", "Enterprise"]]
    }
  ]
}
```

---

### 2.4 Jira Ticket Creator

**Use-case:** DevOps, IT support, or customer service agents that need to create a trackable work item from a conversation — bridging natural language intent with the project management system.

> *Example:* A support agent classifies a bug report, extracts the reproduction steps from the user's message, and creates a Jira issue with correct priority and component.

```json
{
  "_pattern": "REST POST — Jira REST API v3",
  "name": "Create Jira Ticket",
  "description": "Create a new Jira issue. Use when a bug, task, or change-request is identified and needs to be tracked. Always confirm the project key and priority with the user before calling.",
  "node_type": "API caller",
  "config": {
    "url": "{{JIRA_BASE_URL}}/rest/api/3/issue",
    "method": "POST",
    "headers": {
      "Authorization": "Basic {{JIRA_AUTH_B64}}",
      "Content-Type": "application/json",
      "Accept": "application/json"
    },
    "data": {
      "fields": {
        "project": { "key": "{{project_key}}" },
        "summary": "{{summary}}",
        "description": {
          "type": "doc", "version": 1,
          "content": [{ "type": "paragraph", "content": [{ "type": "text", "text": "{{description}}" }] }]
        },
        "issuetype": { "name": "{{issue_type}}" },
        "priority": { "name": "{{priority}}" }
      }
    },
    "timeout": "20"
  },
  "parameters": [
    {
      "name": "project_key",
      "type": "string",
      "required": true,
      "description": "Jira project key (all caps).",
      "example": "INFRA"
    },
    {
      "name": "summary",
      "type": "string",
      "required": true,
      "description": "One-line issue title.",
      "example": "Login page throws 500 on Safari 17"
    },
    {
      "name": "description",
      "type": "string",
      "required": true,
      "description": "Full issue description including steps to reproduce."
    },
    {
      "name": "issue_type",
      "type": "string",
      "required": true,
      "enum": ["Bug", "Task", "Story", "Epic", "Improvement"],
      "description": "Jira issue type.",
      "example": "Bug"
    },
    {
      "name": "priority",
      "type": "string",
      "required": true,
      "enum": ["Highest", "High", "Medium", "Low", "Lowest"],
      "description": "Issue priority.",
      "example": "High"
    }
  ]
}
```

---

### 2.5 ServiceNow Incident Creator

**Use-case:** Enterprise IT service management. When the agent detects or receives a production issue, it raises an incident automatically so SLA timers start immediately — without waiting for a human to file the ticket.

> *Example:* A cloud monitoring agent sees disk usage > 95 % on a node, creates a P2 incident in ServiceNow, and assigns it to the storage team.

```json
{
  "_pattern": "REST POST — ServiceNow Table API",
  "name": "Create ServiceNow Incident",
  "description": "Create an IT incident in ServiceNow. Use when a production system issue is detected or reported and must be tracked under ITSM SLA. Provide accurate urgency and impact.",
  "node_type": "API caller",
  "config": {
    "url": "{{SNOW_BASE_URL}}/api/now/table/incident",
    "method": "POST",
    "headers": {
      "Authorization": "Basic {{SNOW_AUTH_B64}}",
      "Content-Type": "application/json",
      "Accept": "application/json"
    },
    "data": {
      "short_description": "{{short_description}}",
      "description": "{{description}}",
      "urgency": "{{urgency}}",
      "impact": "{{impact}}",
      "assignment_group": "{{assignment_group}}"
    },
    "timeout": "20"
  },
  "parameters": [
    {
      "name": "short_description",
      "type": "string",
      "required": true,
      "description": "One-line incident title (max 160 chars).",
      "example": "Prod disk usage > 95% on storage-node-07"
    },
    {
      "name": "description",
      "type": "string",
      "required": true,
      "description": "Full incident details including affected system, observed behavior, and any diagnostics."
    },
    {
      "name": "urgency",
      "type": "string",
      "required": true,
      "enum": ["1", "2", "3"],
      "description": "1 = High, 2 = Medium, 3 = Low.",
      "example": "2"
    },
    {
      "name": "impact",
      "type": "string",
      "required": true,
      "enum": ["1", "2", "3"],
      "description": "1 = High (business-wide), 2 = Medium (department), 3 = Low (individual).",
      "example": "2"
    },
    {
      "name": "assignment_group",
      "type": "string",
      "required": false,
      "description": "ServiceNow assignment group name.",
      "example": "Storage Operations"
    }
  ]
}
```

---

## 3. Data & File Processing

### 3.1 PDF Text Extractor

**Use-case:** Agents that process uploaded contracts, invoices, reports, or policy documents. Instead of the user copy-pasting content, the agent fetches the PDF URL and extracts readable text for further reasoning.

> *Example:* A legal review agent extracts clause text from a contract PDF URL and identifies any non-standard liability terms.

```json
{
  "_pattern": "REST POST — PDF extraction microservice",
  "name": "Extract Text From PDF",
  "description": "Download a PDF from a URL and extract its plain text content. Use this before analyzing contracts, invoices, or any document in PDF format. Returns extracted text as a string.",
  "node_type": "API caller",
  "config": {
    "url": "{{PDF_EXTRACTOR_URL}}/extract",
    "method": "POST",
    "headers": { "Content-Type": "application/json" },
    "data": {
      "pdf_url": "{{pdf_url}}",
      "pages": "{{pages}}"
    },
    "timeout": "30"
  },
  "parameters": [
    {
      "name": "pdf_url",
      "type": "string",
      "required": true,
      "description": "Public HTTPS URL of the PDF file.",
      "example": "https://storage.example.com/contracts/vendor-agreement-2026.pdf"
    },
    {
      "name": "pages",
      "type": "string",
      "required": false,
      "description": "Page range to extract, e.g. '1-5'. Leave empty for the whole document.",
      "example": "1-10"
    }
  ]
}
```

---

### 3.2 Web Page Scraper

**Use-case:** When the Web Search tool returns URLs but the agent needs the actual page content — competitor pricing pages, job listings, real-time news articles — instead of relying on a search snippet.

> *Example:* A competitive intelligence agent fetches a competitor's pricing page and extracts plan names and prices for comparison.

```json
{
  "_pattern": "REST POST — Jina Reader / Firecrawl-compatible scraper",
  "name": "Fetch Web Page Content",
  "description": "Fetch and extract clean readable text from any public web page URL. Use when you have a specific URL and need its full content — not just a search snippet. Do NOT use for login-protected pages.",
  "node_type": "API caller",
  "config": {
    "url": "https://r.jina.ai/{{page_url}}",
    "method": "GET",
    "headers": {
      "Accept": "text/plain",
      "Authorization": "Bearer {{JINA_API_KEY}}"
    },
    "timeout": "25"
  },
  "parameters": [
    {
      "name": "page_url",
      "type": "string",
      "required": true,
      "description": "The full HTTPS URL of the page to read.",
      "example": "https://stripe.com/pricing"
    }
  ]
}
```

---

### 3.3 CSV / Table Analyzer

**Use-case:** When the user uploads or provides CSV data and wants insights, trends, or anomalies — the agent summarizes the table without needing a dedicated analytics backend.

> *Example:* A business analyst agent receives monthly sales CSV rows and produces a bullet-point summary of top products, worst performers, and MoM trend.

```json
{
  "_pattern": "LLM as a data analyst — table-in, insights-out",
  "name": "Analyze CSV Data",
  "description": "Analyze tabular CSV data and return key insights: totals, averages, top/bottom items, trends, and anomalies. Use when the user shares a CSV or table and asks 'what does this tell me?'.",
  "node_type": "LLM invoker",
  "config": {
    "Model": "llama3",
    "Prompt": "You are a data analyst. The user has provided CSV data. Analyze it and return:\n1. A one-sentence summary of what the data represents.\n2. Key statistics (totals, averages, counts).\n3. Top 3 and bottom 3 items by the primary metric.\n4. Any notable trends or anomalies.\nReturn as structured bullet points. Be concise.",
    "user_message_key": "csv_data",
    "use_memory": "false",
    "Response Format": "text"
  },
  "parameters": [
    {
      "name": "csv_data",
      "type": "string",
      "required": true,
      "description": "Raw CSV text or a JSON array of row objects to analyze.",
      "example": "product,revenue,units\nWidget A,45000,900\nWidget B,12000,400\nWidget C,78000,1560"
    }
  ]
}
```

---

### 3.4 JSON Transformer

**Use-case:** When an upstream tool returns a complex nested JSON and the agent needs to reshape it to a simpler format before passing it to a downstream API or displaying it to the user.

> *Example:* An integration agent receives a verbose Salesforce opportunity object and reshapes it to `{name, value, stage, close_date}` for a lightweight dashboard update.

```json
{
  "_pattern": "LLM as a JSON reshape engine",
  "name": "Transform JSON",
  "description": "Reshape, filter, or flatten a JSON object into a simpler target structure. Use when an upstream tool returns a verbose payload and downstream needs only specific fields.",
  "node_type": "LLM invoker",
  "config": {
    "Model": "llama3",
    "Prompt": "You are a JSON transformer. The user will provide a SOURCE JSON and a TARGET SCHEMA description. Output ONLY the transformed JSON — no explanation, no markdown fences, just the raw JSON object.",
    "user_message_key": "transform_request",
    "use_memory": "false",
    "Response Format": "json"
  },
  "parameters": [
    {
      "name": "transform_request",
      "type": "string",
      "required": true,
      "description": "Describe the transformation in this format: 'SOURCE: <json>\\nTARGET SCHEMA: <field descriptions>'.",
      "example": "SOURCE: {\"Id\":\"006X\",\"Name\":\"Acme Deal\",\"Amount\":45000,\"StageName\":\"Closed Won\",\"CloseDate\":\"2026-08-30\",\"AccountId\":\"001X\"}\nTARGET SCHEMA: {name, value, stage, close_date}"
    }
  ]
}
```

---

## 4. AI & Intelligence

### 4.1 Image Analyzer (Vision)

**Use-case:** Agents that work with receipts, product photos, ID documents, diagrams, or screenshots. The agent extracts structured information from an image URL using a vision-capable model.

> *Example:* An expense management agent receives a photo of a restaurant receipt and extracts vendor, date, total, and individual line items.

```json
{
  "_pattern": "LLM invoker — vision-capable model, image URL in the prompt",
  "name": "Analyze Image",
  "description": "Analyze the content of an image at a given URL. Use for receipts, product photos, screenshots, charts, ID documents, or any visual that contains information you need to extract.",
  "node_type": "LLM invoker",
  "config": {
    "Model": "gemini-pro-vision",
    "Prompt": "Carefully examine the image provided by the user. Extract all relevant information as a structured JSON object. For receipts: {vendor, date, line_items, subtotal, tax, total}. For products: {name, brand, price, condition}. For charts: {title, axes, key_values}. Output ONLY the JSON with no explanation.",
    "user_message_key": "image_url",
    "use_memory": "false",
    "Response Format": "json"
  },
  "parameters": [
    {
      "name": "image_url",
      "type": "string",
      "required": true,
      "description": "Public HTTPS URL of the image to analyze.",
      "example": "https://storage.example.com/receipts/starbucks-2026-08-16.jpg"
    }
  ]
}
```

---

### 4.2 Text Translator

**Use-case:** Multilingual customer support, content localization, or processing user inputs in any language. The agent translates text on-the-fly without a separate translation API subscription.

> *Example:* A global support agent detects a French message, translates it to English for internal processing, then translates the response back to French before replying.

```json
{
  "_pattern": "LLM invoker — translation with language detection",
  "name": "Translate Text",
  "description": "Translate text from one language to another. Detects source language automatically if not specified. Use for multilingual support, content localization, or cross-language data processing.",
  "node_type": "LLM invoker",
  "config": {
    "Model": "llama3",
    "Prompt": "You are a professional translator. Translate the user's text to the target language specified. Preserve the original tone and formatting. Return ONLY the translated text with no explanation or preamble.",
    "user_message_key": "translation_request",
    "use_memory": "false",
    "Response Format": "text"
  },
  "parameters": [
    {
      "name": "translation_request",
      "type": "string",
      "required": true,
      "description": "Format: 'Translate to <language>: <text to translate>'.",
      "example": "Translate to English: Bonjour, je voudrais annuler ma commande numéro 4892."
    }
  ]
}
```

---

### 4.3 PII Redactor

**Use-case:** Data governance and compliance workflows where user messages or documents need to be stored or shared with third-party systems, but must first have personal data stripped out.

> *Example:* A customer feedback pipeline redacts names, emails, phone numbers, and account numbers from raw survey responses before writing them to an analytics database.

```json
{
  "_pattern": "LLM invoker — PII removal with replacement tokens",
  "name": "Redact PII",
  "description": "Remove or replace personally identifiable information (PII) from text before storing or sharing it. Replaces names, emails, phone numbers, addresses, and account numbers with [REDACTED] tokens. Use before logging user messages to external systems.",
  "node_type": "LLM invoker",
  "config": {
    "Model": "llama3",
    "Prompt": "You are a data-privacy assistant. Detect and replace all PII in the user's text with the placeholder [REDACTED]. PII includes: full names, email addresses, phone numbers, physical addresses, national ID numbers, credit card numbers, account numbers, and date of birth. Preserve the full sentence structure and all non-PII content. Return ONLY the redacted text.",
    "user_message_key": "text_to_redact",
    "use_memory": "false",
    "Response Format": "text"
  },
  "parameters": [
    {
      "name": "text_to_redact",
      "type": "string",
      "required": true,
      "description": "The raw text that may contain PII.",
      "example": "Hi, I'm John Smith, my email is john.smith@email.com and my account number is ACC-884712."
    }
  ]
}
```

---

### 4.4 Sentiment & Emotion Scorer

**Use-case:** CX analytics, escalation routing, and social media monitoring. The agent scores the emotional tone of a message and returns structured output that can trigger downstream logic — e.g., escalate to a human if CSAT score is very negative.

> *Example:* A customer service agent scores every incoming message; if sentiment is `very_negative` with emotion `angry`, it escalates to a live agent and flags the session.

```json
{
  "_pattern": "LLM invoker — structured sentiment output",
  "name": "Score Sentiment",
  "description": "Score the sentiment and dominant emotion of a text. Returns a JSON with sentiment (positive/neutral/negative/very_negative), confidence (0.0–1.0), dominant_emotion, and a one-sentence reason. Use to route escalations or tag conversations.",
  "node_type": "LLM invoker",
  "config": {
    "Model": "llama3",
    "Prompt": "Analyze the sentiment and emotional tone of the user's text. Return ONLY a JSON object in this exact schema: {\"sentiment\": \"positive|neutral|negative|very_negative\", \"confidence\": 0.0, \"dominant_emotion\": \"happy|frustrated|angry|confused|satisfied|neutral\", \"reason\": \"one sentence\"}. No other text.",
    "user_message_key": "text_to_score",
    "use_memory": "false",
    "Response Format": "json"
  },
  "parameters": [
    {
      "name": "text_to_score",
      "type": "string",
      "required": true,
      "description": "The customer or user message to analyze.",
      "example": "This is absolutely ridiculous. I've been waiting 3 weeks for a refund and nobody is helping me!"
    }
  ]
}
```

---

### 4.5 Document Summarizer

**Use-case:** Research, due-diligence, and knowledge-management workflows where long documents (reports, legal text, meeting transcripts) need to be distilled into executive summaries or action items.

> *Example:* A board meeting assistant ingests a 40-page quarterly report and returns a 5-bullet executive summary plus a list of decisions made.

```json
{
  "_pattern": "LLM invoker — structured long-document summarizer",
  "name": "Summarize Document",
  "description": "Summarize a long document into key points, decisions, and action items. Use for reports, meeting transcripts, legal documents, or any text exceeding 500 words that needs condensing.",
  "node_type": "LLM invoker",
  "config": {
    "Model": "llama3",
    "Prompt": "You are an executive assistant. Summarize the document provided by the user using this structure:\n**TL;DR** (1 sentence)\n**Key Points** (3–5 bullets)\n**Decisions Made** (bullet list, or 'None' if not applicable)\n**Action Items** (bullet list with owner if mentioned, or 'None')\nBe concise. Use plain language.",
    "user_message_key": "document_text",
    "use_memory": "false",
    "Response Format": "text"
  },
  "parameters": [
    {
      "name": "document_text",
      "type": "string",
      "required": true,
      "description": "The full document text to summarize. Paste or pipe in the extracted text.",
      "example": "Q2 2026 Financial Results — Revenue grew 18 % YoY to $4.2M..."
    }
  ]
}
```

---

## 5. Finance & Commerce

### 5.1 Real-Time Currency Converter

**Use-case:** E-commerce, invoicing, and financial reporting agents that need to present prices or totals in the user's preferred currency using live exchange rates.

> *Example:* A procurement agent converts a USD vendor quote to INR using the live exchange rate before presenting the approval request to a finance manager.

```json
{
  "_pattern": "REST GET — exchangerate.host (free tier)",
  "name": "Convert Currency",
  "description": "Convert an amount from one currency to another using the live exchange rate. Use for invoices, quotes, or any monetary value that needs multi-currency display.",
  "node_type": "API caller",
  "config": {
    "url": "https://api.exchangerate.host/convert?from={{from_currency}}&to={{to_currency}}&amount={{amount}}&access_key={{EXCHANGERATE_API_KEY}}",
    "method": "GET",
    "headers": { "Accept": "application/json" },
    "timeout": "10"
  },
  "parameters": [
    {
      "name": "from_currency",
      "type": "string",
      "required": true,
      "description": "ISO 4217 source currency code.",
      "example": "USD"
    },
    {
      "name": "to_currency",
      "type": "string",
      "required": true,
      "description": "ISO 4217 target currency code.",
      "example": "INR"
    },
    {
      "name": "amount",
      "type": "number",
      "required": true,
      "description": "The numeric amount to convert.",
      "example": 1500.00
    }
  ]
}
```

---

### 5.2 Stock / Crypto Quote

**Use-case:** Investment assistants, financial report generators, and market-monitoring agents that need live or delayed price data to provide context-aware answers.

> *Example:* A portfolio agent is asked "Is now a good time to add more AAPL?" — it fetches the current price and 52-week range before giving a data-grounded response.

```json
{
  "_pattern": "REST GET — Alpha Vantage / Yahoo Finance proxy",
  "name": "Get Stock Quote",
  "description": "Fetch the latest stock or crypto price, change percentage, and 52-week high/low for a given ticker symbol. Use before any financial advice or portfolio discussion.",
  "node_type": "API caller",
  "config": {
    "url": "https://query1.finance.yahoo.com/v8/finance/chart/{{ticker}}?interval=1d&range=1d",
    "method": "GET",
    "headers": { "Accept": "application/json", "User-Agent": "Mozilla/5.0" },
    "timeout": "10"
  },
  "parameters": [
    {
      "name": "ticker",
      "type": "string",
      "required": true,
      "description": "Stock or crypto ticker symbol. Use standard exchange suffix if needed.",
      "example": "AAPL"
    }
  ]
}
```

---

### 5.3 Invoice Builder

**Use-case:** Freelancer tools, ERP integrations, and billing agents that compose structured invoice JSON from natural language descriptions — ready to send to an accounting system or PDF renderer.

> *Example:* A sales ops agent hears "Create an invoice for Acme Corp: 5 days consulting at $1,200/day plus $400 travel expenses" and returns a ready-to-submit JSON invoice.

```json
{
  "_pattern": "LLM invoker — structured invoice generation",
  "name": "Build Invoice",
  "description": "Generate a structured invoice JSON from a natural-language description of services, quantities, and prices. Use when the user describes what to bill and needs a formatted, calculation-correct invoice object.",
  "node_type": "LLM invoker",
  "config": {
    "Model": "llama3",
    "Prompt": "You are an invoicing assistant. From the user's description, generate a JSON invoice with this schema: {\"invoice_number\": \"INV-YYYYMMDD-001\", \"date\": \"YYYY-MM-DD\", \"client_name\": \"\", \"line_items\": [{\"description\": \"\", \"quantity\": 0, \"unit_price\": 0, \"total\": 0}], \"subtotal\": 0, \"tax_rate\": 0, \"tax_amount\": 0, \"total_due\": 0, \"currency\": \"USD\"}. Calculate all totals accurately. Use today's date if not specified. Output ONLY the JSON.",
    "user_message_key": "invoice_description",
    "use_memory": "false",
    "Response Format": "json"
  },
  "parameters": [
    {
      "name": "invoice_description",
      "type": "string",
      "required": true,
      "description": "Natural language description of what to invoice: client, line items, quantities, unit prices, and any applicable tax.",
      "example": "Invoice for Acme Corp: 5 days consulting at $1,200/day, $400 travel expenses, 10% tax."
    }
  ]
}
```

---

## 6. Developer & System

### 6.1 GitHub Issue Creator

**Use-case:** Developer productivity agents that turn bug reports, feature requests, or meeting action items into tracked GitHub issues without the developer switching context.

> *Example:* A code review agent identifies a security vulnerability in a PR review and immediately creates a high-priority GitHub issue with the affected file and line number.

```json
{
  "_pattern": "REST POST — GitHub Issues API v3",
  "name": "Create GitHub Issue",
  "description": "Create a GitHub issue in a repository. Use when a bug, security vulnerability, or feature gap is identified and needs to be tracked. Confirm the repository and label before calling.",
  "node_type": "API caller",
  "config": {
    "url": "https://api.github.com/repos/{{GITHUB_OWNER}}/{{GITHUB_REPO}}/issues",
    "method": "POST",
    "headers": {
      "Authorization": "Bearer {{GITHUB_TOKEN}}",
      "Accept": "application/vnd.github+json",
      "Content-Type": "application/json"
    },
    "data": {
      "title": "{{title}}",
      "body": "{{body}}",
      "labels": "{{labels}}",
      "assignees": "{{assignees}}"
    },
    "timeout": "15"
  },
  "parameters": [
    {
      "name": "title",
      "type": "string",
      "required": true,
      "description": "Concise issue title.",
      "example": "[Security] SQL injection possible in user search endpoint"
    },
    {
      "name": "body",
      "type": "string",
      "required": true,
      "description": "Detailed issue description in GitHub-flavored Markdown."
    },
    {
      "name": "labels",
      "type": "array",
      "required": false,
      "description": "List of label strings to apply.",
      "example": ["bug", "security", "P1"]
    },
    {
      "name": "assignees",
      "type": "array",
      "required": false,
      "description": "List of GitHub usernames to assign.",
      "example": ["octocat", "torvalds"]
    }
  ]
}
```

---

### 6.2 Outbound Webhook Trigger

**Use-case:** The universal integration glue. When no dedicated tool exists for a target system (Make, Zapier, n8n, custom microservice), the agent fires a webhook payload and the external platform handles the rest.

> *Example:* An HR agent completes an onboarding checklist and triggers a Zapier webhook that automatically provisions accounts in 5 downstream SaaS tools.

```json
{
  "_pattern": "REST POST — generic webhook, arbitrary JSON payload",
  "name": "Trigger Webhook",
  "description": "Send a JSON payload to an external webhook URL. Use as a universal integration when a dedicated tool does not exist — fires events to Make, Zapier, n8n, or any custom endpoint.",
  "node_type": "API caller",
  "config": {
    "url": "{{WEBHOOK_URL}}",
    "method": "POST",
    "headers": {
      "Content-Type": "application/json",
      "X-Agent-Source": "agent-studio"
    },
    "data": {
      "event": "{{event_name}}",
      "payload": "{{payload}}"
    },
    "timeout": "15"
  },
  "parameters": [
    {
      "name": "event_name",
      "type": "string",
      "required": true,
      "description": "A short snake_case name identifying the event type.",
      "example": "onboarding_completed"
    },
    {
      "name": "payload",
      "type": "object",
      "required": true,
      "description": "Arbitrary JSON object with the event data. Include all fields the receiving system needs.",
      "example": { "employee_id": "E-4821", "email": "new.hire@company.com", "start_date": "2026-09-01" }
    }
  ]
}
```

---

### 6.3 DateTime Calculator

**Use-case:** Any agent that needs date arithmetic — calculating deadlines, SLA expiry, age from DOB, business days between two dates, or converting timestamps between timezones.

> *Example:* A contract management agent calculates the renewal deadline as "contract start date + 12 months - 30 days notice period" and returns the exact date.

```json
{
  "_pattern": "LLM invoker — date/time arithmetic",
  "name": "Calculate DateTime",
  "description": "Perform date and time calculations: add/subtract durations, find the difference between two dates, convert timezones, or find the next occurrence of a weekday. Returns exact dates and durations.",
  "node_type": "LLM invoker",
  "config": {
    "Model": "llama3",
    "Prompt": "You are a precision date calculator. Today's date is injected via the system. Perform the requested date/time calculation and return ONLY a JSON: {\"result\": \"<ISO date or duration>\", \"explanation\": \"<one-line working>\"}. Use ISO 8601 for all dates. For durations use 'X days', 'X months' format.",
    "user_message_key": "calculation_request",
    "use_memory": "false",
    "Response Format": "json"
  },
  "parameters": [
    {
      "name": "calculation_request",
      "type": "string",
      "required": true,
      "description": "Plain-English description of the date calculation needed.",
      "example": "Contract signed 2025-03-15. Term is 18 months. 30-day notice required. What is the last day to send notice to avoid auto-renewal?"
    }
  ]
}
```

---

### 6.4 Code Reviewer

**Use-case:** Developer tools and CI-adjacent agents that provide instant code quality, security, and best-practice feedback without full IDE tooling — particularly useful in chat-based pair programming.

> *Example:* A developer pastes a Python function handling user auth. The agent reviews it and flags missing input validation, a hardcoded secret, and an unhandled exception path.

```json
{
  "_pattern": "LLM invoker — structured code review output",
  "name": "Review Code",
  "description": "Review a code snippet for bugs, security issues, performance problems, and best-practice violations. Returns structured findings with severity and fix suggestions. Use before merging or deploying code.",
  "node_type": "LLM invoker",
  "config": {
    "Model": "llama3",
    "Prompt": "You are a senior software engineer conducting a code review. Analyze the provided code snippet and return a JSON review: {\"language\": \"\", \"overall_rating\": \"pass|needs_work|fail\", \"findings\": [{\"severity\": \"critical|high|medium|low\", \"category\": \"security|bug|performance|style|maintainability\", \"line\": \"approximate\", \"issue\": \"\", \"suggestion\": \"\"}], \"summary\": \"\"}. Be specific and actionable. Output ONLY the JSON.",
    "user_message_key": "code_to_review",
    "use_memory": "false",
    "Response Format": "json"
  },
  "parameters": [
    {
      "name": "code_to_review",
      "type": "string",
      "required": true,
      "description": "The code snippet to review, optionally prefixed with the language name.",
      "example": "python\ndef get_user(user_id):\n    query = f\"SELECT * FROM users WHERE id = {user_id}\"\n    return db.execute(query)"
    }
  ]
}
```

---

### 6.5 Environment Variable / Secret Lookup

**Use-case:** Dynamic configuration agents that need to retrieve runtime config or non-sensitive metadata without hardcoding values — e.g., fetching a feature flag or the current API base URL for an environment.

> *Example:* A deployment agent checks a config service to determine which environment (staging/prod) to deploy to based on the current branch name.

```json
{
  "_pattern": "REST GET — internal config / feature-flag service",
  "name": "Get Config Value",
  "description": "Retrieve a configuration value or feature flag by key from the internal config service. Use when workflow behavior depends on dynamic settings not known at design time.",
  "node_type": "API caller",
  "config": {
    "url": "{{CONFIG_SERVICE_URL}}/config/{{config_key}}",
    "method": "GET",
    "headers": {
      "Authorization": "Bearer {{CONFIG_SERVICE_TOKEN}}",
      "Accept": "application/json"
    },
    "timeout": "10"
  },
  "parameters": [
    {
      "name": "config_key",
      "type": "string",
      "required": true,
      "description": "The configuration key to retrieve.",
      "example": "payment_gateway.timeout_seconds"
    }
  ]
}
```

---

## 7. Knowledge & Research

### 7.1 Wikipedia Article Lookup

**Use-case:** General knowledge agents, educational assistants, and research tools that need authoritative, structured background information — faster and cheaper than a full web search for well-known topics.

> *Example:* A travel planning agent looks up the Wikipedia summary for "Machu Picchu" to provide authoritative historical context alongside hotel recommendations.

```json
{
  "_pattern": "REST GET — Wikipedia REST API v1 (no auth)",
  "name": "Wikipedia Lookup",
  "description": "Fetch a concise Wikipedia summary and key facts for any topic. Use for background information on well-known people, places, events, or concepts. Free, no auth required.",
  "node_type": "API caller",
  "config": {
    "url": "https://en.wikipedia.org/api/rest_v1/page/summary/{{topic}}",
    "method": "GET",
    "headers": { "Accept": "application/json" },
    "timeout": "10"
  },
  "parameters": [
    {
      "name": "topic",
      "type": "string",
      "required": true,
      "description": "Wikipedia article title (URL-encoded if it contains spaces — use underscores).",
      "example": "Machu_Picchu"
    }
  ]
}
```

---

### 7.2 Real-Time News Digest

**Use-case:** Market intelligence, executive briefing, and trend-monitoring agents that need the latest news on a topic — injecting current awareness into an otherwise static LLM knowledge base.

> *Example:* A financial research agent searches for the latest news on "Tesla Q2 2026 earnings" before providing investment commentary.

```json
{
  "_pattern": "REST GET — NewsAPI.org (free tier)",
  "name": "Search News",
  "description": "Fetch the latest news articles on a topic from across the web. Use when the user asks about current events, recent developments, or breaking news. Returns title, source, date, and snippet.",
  "node_type": "API caller",
  "config": {
    "url": "https://newsapi.org/v2/everything?q={{query}}&sortBy=publishedAt&pageSize=5&language=en&apiKey={{NEWSAPI_KEY}}",
    "method": "GET",
    "headers": { "Accept": "application/json" },
    "timeout": "15"
  },
  "parameters": [
    {
      "name": "query",
      "type": "string",
      "required": true,
      "description": "News search query. Use specific entity names and dates for best results.",
      "example": "Tesla Q2 2026 earnings results"
    }
  ]
}
```

---

### 7.3 Weather Lookup

**Use-case:** Travel, logistics, event-planning, and field-service agents that need current or forecast weather conditions to tailor recommendations or flag risks.

> *Example:* A delivery routing agent checks the 24-hour forecast for 5 delivery cities before generating the optimal route order, flagging locations with severe weather.

```json
{
  "_pattern": "REST GET — OpenWeatherMap Current Weather API",
  "name": "Get Weather",
  "description": "Get the current weather conditions and 24-hour forecast for a city. Use for travel planning, logistics, or outdoor event decisions. Returns temperature, condition, humidity, and wind speed.",
  "node_type": "API caller",
  "config": {
    "url": "https://api.openweathermap.org/data/2.5/forecast?q={{city}}&units=metric&cnt=8&appid={{OPENWEATHER_API_KEY}}",
    "method": "GET",
    "headers": { "Accept": "application/json" },
    "timeout": "10"
  },
  "parameters": [
    {
      "name": "city",
      "type": "string",
      "required": true,
      "description": "City name, optionally with country code for disambiguation.",
      "example": "Mumbai,IN"
    }
  ]
}
```

---

### 7.4 arXiv / Research Paper Search

**Use-case:** Academic research, R&D intelligence, and technology scouting agents that need to surface recent peer-reviewed papers on a topic.

> *Example:* A technology radar agent searches arXiv weekly for new papers on "LLM agent memory" and summarizes the top 3 abstracts for an engineering newsletter.

```json
{
  "_pattern": "REST GET — arXiv API (no auth)",
  "name": "Search Research Papers",
  "description": "Search arXiv for recent academic papers on a topic. Returns titles, authors, abstracts, and links. Use for technology scouting, literature review, or staying current with a research field.",
  "node_type": "API caller",
  "config": {
    "url": "http://export.arxiv.org/api/query?search_query=all:{{query}}&sortBy=submittedDate&sortOrder=descending&max_results={{max_results}}",
    "method": "GET",
    "headers": { "Accept": "application/atom+xml" },
    "timeout": "20"
  },
  "parameters": [
    {
      "name": "query",
      "type": "string",
      "required": true,
      "description": "Research topic or keywords. Wrap multi-word terms in quotes for exact matching.",
      "example": "\"LLM agent\" memory retrieval augmented"
    },
    {
      "name": "max_results",
      "type": "integer",
      "required": false,
      "description": "Number of papers to return (default 5, max 20).",
      "example": 5
    }
  ]
}
```

---

## 8. Security & Compliance

### 8.1 URL Safety Scanner (VirusTotal)

**Use-case:** Security-aware agents in phishing detection, SOC automation, or content moderation that need to validate whether a URL or domain is known-malicious before acting on it or sharing it.

> *Example:* A phishing triage agent receives a suspicious link reported by an employee, scans it against VirusTotal, and returns the verdict with a vendor breakdown before escalating to the security team.

```json
{
  "_pattern": "REST GET — VirusTotal URL scan (v3)",
  "name": "Scan URL Safety",
  "description": "Check whether a URL or domain is flagged as malicious by any security vendor via VirusTotal. Use BEFORE opening, sharing, or acting on any unverified URL. Returns a verdict and vendor breakdown.",
  "node_type": "API caller",
  "config": {
    "url": "https://www.virustotal.com/api/v3/urls/{{url_id}}",
    "method": "GET",
    "headers": {
      "x-apikey": "{{VIRUSTOTAL_API_KEY}}",
      "Accept": "application/json"
    },
    "timeout": "20"
  },
  "parameters": [
    {
      "name": "url_id",
      "type": "string",
      "required": true,
      "description": "Base64url-encoded URL (without padding) to look up. Compute as base64url(url) with trailing '=' removed.",
      "example": "aHR0cHM6Ly9leGFtcGxlLmNvbQ"
    }
  ]
}
```

---

### 8.2 Password / Secret Strength Checker

**Use-case:** Security onboarding agents, IT compliance bots, and developer tools that validate password policies or identify weak secrets before they reach production.

> *Example:* A DevSecOps agent reviews a newly proposed service-account password policy spec and scores each proposed password against NIST 800-63B criteria.

```json
{
  "_pattern": "LLM invoker — policy-grounded security assessment",
  "name": "Check Password Strength",
  "description": "Evaluate the strength of a password or secret string against NIST 800-63B and OWASP guidelines. Returns a structured risk assessment. NEVER log or store the evaluated password after this call.",
  "node_type": "LLM invoker",
  "config": {
    "Model": "llama3",
    "Prompt": "You are a security auditor. Evaluate the provided password against NIST 800-63B and OWASP password guidelines. Return ONLY a JSON: {\"strength\": \"weak|moderate|strong|very_strong\", \"score\": 0, \"issues\": [\"list of problems\"], \"recommendations\": [\"list of improvements\"]}. Score is 0–100. Do NOT repeat or quote the password in your response.",
    "user_message_key": "password_to_check",
    "use_memory": "false",
    "Response Format": "json"
  },
  "parameters": [
    {
      "name": "password_to_check",
      "type": "string",
      "required": true,
      "description": "The password string to evaluate. This is analyzed in-memory and must not be logged or stored.",
      "example": "P@ssw0rd123"
    }
  ]
}
```

---

### 8.3 Compliance Policy Checker

**Use-case:** GRC (Governance, Risk & Compliance) agents that validate text, configurations, or data handling decisions against a referenced policy document — without a human reviewer having to manually cross-reference.

> *Example:* A data governance agent checks a proposed new data retention period against the company GDPR policy and flags whether it is compliant.

```json
{
  "_pattern": "KB retrieval + LLM — grounded compliance check",
  "name": "Check Policy Compliance",
  "description": "Check whether a described action, configuration, or data handling practice complies with a named internal or regulatory policy. Uses the knowledge base to ground the answer. Returns compliant/non-compliant with cited policy sections.",
  "node_type": "Knowledge Retrieval Node",
  "config": {
    "knowledge_base_name": "{{COMPLIANCE_KB_NAME}}",
    "user_prompt": "{{compliance_query}}",
    "limit": "5",
    "max_distance": "0.65"
  },
  "parameters": [
    {
      "name": "compliance_query",
      "type": "string",
      "required": true,
      "description": "Describe the action or configuration to validate against policy.",
      "example": "We plan to retain EU customer email addresses for 7 years after account closure. Is this compliant with GDPR?"
    }
  ]
}
```

---

## Quick Reference — Tool × Node Type Matrix

| Tool | Node Type | Auth Required |
|------|-----------|---------------|
| Post To Slack | `API caller` | `SLACK_WEBHOOK_URL` env |
| Send Teams Notification | `API caller` | `TEAMS_WEBHOOK_URL` env |
| Send SMS | `API caller` | `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN_B64` |
| Send WhatsApp Message | `API caller` | `WHATSAPP_API_KEY` |
| Create Calendar Event | `API caller` | `GOOGLE_ACCESS_TOKEN` |
| Read Google Sheet | `API caller` | `GOOGLE_ACCESS_TOKEN`, `SHEET_ID` |
| Append To Google Sheet | `API caller` | `GOOGLE_ACCESS_TOKEN`, `SHEET_ID` |
| Create Jira Ticket | `API caller` | `JIRA_BASE_URL`, `JIRA_AUTH_B64` |
| Create ServiceNow Incident | `API caller` | `SNOW_BASE_URL`, `SNOW_AUTH_B64` |
| Extract Text From PDF | `API caller` | `PDF_EXTRACTOR_URL` env |
| Fetch Web Page Content | `API caller` | `JINA_API_KEY` |
| Analyze CSV Data | `LLM invoker` | none |
| Transform JSON | `LLM invoker` | none |
| Analyze Image | `LLM invoker` | vision-capable model |
| Translate Text | `LLM invoker` | none |
| Redact PII | `LLM invoker` | none |
| Score Sentiment | `LLM invoker` | none |
| Summarize Document | `LLM invoker` | none |
| Convert Currency | `API caller` | `EXCHANGERATE_API_KEY` |
| Get Stock Quote | `API caller` | none (public endpoint) |
| Build Invoice | `LLM invoker` | none |
| Create GitHub Issue | `API caller` | `GITHUB_TOKEN`, `GITHUB_OWNER`, `GITHUB_REPO` |
| Trigger Webhook | `API caller` | `WEBHOOK_URL` env |
| Calculate DateTime | `LLM invoker` | none |
| Review Code | `LLM invoker` | none |
| Get Config Value | `API caller` | `CONFIG_SERVICE_URL`, `CONFIG_SERVICE_TOKEN` |
| Wikipedia Lookup | `API caller` | none (public API) |
| Search News | `API caller` | `NEWSAPI_KEY` |
| Get Weather | `API caller` | `OPENWEATHER_API_KEY` |
| Search Research Papers | `API caller` | none (public API) |
| Scan URL Safety | `API caller` | `VIRUSTOTAL_API_KEY` |
| Check Password Strength | `LLM invoker` | none |
| Check Policy Compliance | `Knowledge Retrieval Node` | `COMPLIANCE_KB_NAME` env |

---

## Environment Variables Checklist

Add these to your `.env` / flow-level secrets before using the corresponding tools:

```bash
# Communication
SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
TEAMS_WEBHOOK_URL=https://outlook.office.com/webhook/...
TWILIO_ACCOUNT_SID=ACxxxxxxxxx
TWILIO_AUTH_TOKEN_B64=<base64(accountSid:authToken)>
TWILIO_FROM_NUMBER=+1xxxxxxxxxx
WHATSAPP_API_KEY=xxxxx

# Productivity
GOOGLE_ACCESS_TOKEN=<OAuth2 access token>
SHEET_ID=<Google Sheets spreadsheet ID>
JIRA_BASE_URL=https://yourorg.atlassian.net
JIRA_AUTH_B64=<base64(email:api_token)>
SNOW_BASE_URL=https://yourinstance.service-now.com
SNOW_AUTH_B64=<base64(user:password)>

# Developer
GITHUB_TOKEN=ghp_xxxx
GITHUB_OWNER=your-org
GITHUB_REPO=your-repo
WEBHOOK_URL=https://hook.make.com/...
CONFIG_SERVICE_URL=https://config.internal.company.com
CONFIG_SERVICE_TOKEN=xxxx

# Data / File
PDF_EXTRACTOR_URL=https://pdf-service.internal.company.com
JINA_API_KEY=jina_xxxx

# Finance
EXCHANGERATE_API_KEY=xxxx

# Research
NEWSAPI_KEY=xxxx
OPENWEATHER_API_KEY=xxxx

# Security
VIRUSTOTAL_API_KEY=xxxx
COMPLIANCE_KB_NAME=gdpr-policy
```

---

*Generated for Agent Studio · ReAct Agent v2 compatible · 2026-08-16*
