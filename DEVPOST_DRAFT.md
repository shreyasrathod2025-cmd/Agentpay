# Devpost write-up (draft, edit before submitting)

**Tagline:** AI proposes, rules decide, PayPal executes.

## Inspiration
AI agents are starting to shop for us, but giving an agent your payment power is risky: what if it misreads a request or overspends? We wanted an agent that is useful but cannot exceed limits the user set.

## What it does
You ask for a product in plain language. AgentPay understands the request with Gemini, searches a catalogue of about 14,000 products, and picks the best option among the ones that pass your rules (budget, rating, review count, per-order limit, daily cap). You review a summary and approve; PayPal authorizes the payment (funds held), and you release it (capture) after delivery. Every step is logged, and the rules are editable.

## How we built it
- **PayPal:** Orders API v2 with `intent=AUTHORIZE`: create order, buyer approval, authorize, then capture. Sandbox.
- **AI:** Gemini turns free text into structured intent and chooses the best deal among rule-compliant options, with a short explanation. Falls back to a rule-based parser if the AI is unavailable.
- **Guardrails:** a server-side rule engine re-checks everything at order creation, so neither the AI nor the browser can bypass limits.
- **App:** FastAPI backend, mobile-first installable PWA.

## Challenges
PayPal sandbox currency and buyer/seller account rules, authorize versus capture semantics, free-tier AI rate limits, and cleaning messy public product datasets.

## Accomplishments
A complete working flow from natural-language request to PayPal authorization and capture, with real safety limits rather than prompts alone.

## What we learned
Treat AI output as a proposal and enforce policy in code. Authorization is a hold, not an escrow, so we describe it accurately.

## What's next
Live retailer connectors, merchant checkout integration, per-category budgets, multi-user accounts, and webhooks for delivery confirmation.

## Built with
Python, FastAPI, PayPal Orders API, Google Gemini API, JavaScript (PWA), public Flipkart and Myntra product datasets.

## Testing instructions (fill in)
- App: `<your public URL>` (a free server may take about a minute to wake up on the first visit)
- Try: "Samsung phone under 15000", then Approve.
- PayPal sandbox buyer: email `<test buyer email>` / password `<test buyer password>` (a dedicated sandbox test account, not a real one)
- After PayPal approval you return to a receipt; click "Delivery confirmed" to capture. Orders tab shows the audit log; Rules tab edits limits.
