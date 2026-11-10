# AgentPay
Demo video: https://youtu.be/wgbWQl4NXtM
An AI shopping agent that finds a deal, enforces user-defined spending rules on the server, and pays through a
permission-controlled **PayPal** workflow (authorize first, capture only after the user releases the payment).

> AgentPay is an AI-powered purchasing agent that finds the right deal, enforces user-defined rules, and uses PayPal
> to execute payments through a permission-controlled workflow.

## How it works
1. **Ask** in plain language ("Samsung phone under 15000", "men running shoes under 3000").
2. **AI (Gemini)** turns the request into structured intent, then picks the best option, but only among products that already passed every rule.
3. **Rule engine (server-side, the AI cannot override it)**: budget, minimum rating and review count, per-order limit, daily cap.
4. **Human approval screen** summarises the item, amount and every rule check.
5. **PayPal Orders API v2**: create order with `intent=AUTHORIZE`, buyer approves in PayPal, backend authorizes (funds on hold), then captures when the user releases the payment.
6. **Audit log** records every step. Limits are editable in the Rules tab.

PayPal authorization is a hold on funds, **not an escrow service**.

## Stack
FastAPI, httpx, PayPal Orders API v2 (sandbox), Google Gemini API (free tier; any OpenAI-compatible API or Claude also works),
vanilla JS mobile-first PWA (installable).

## Run locally
```
pip install -r requirements.txt
copy .env.example .env      # then fill in your values
python -m uvicorn main:app --reload
```
Open http://127.0.0.1:8000. Terminal should print `PayPal client id loaded: True | secret loaded: True | AI: gemini`.

| Variable | Purpose |
|---|---|
| `PAYPAL_CLIENT_ID`, `PAYPAL_SECRET` | Sandbox REST app credentials (use a US Business sandbox account as the app's merchant) |
| `GEMINI_API_KEY` | Free key from https://aistudio.google.com/apikey (optional: without it a rule-based parser is used) |
| `APP_URL` | Public base URL (PayPal return URL). Defaults to http://localhost:8000 |
| `LLM_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL` | Optional: any OpenAI-compatible provider instead of Gemini |

Pay with a **US Personal sandbox buyer account** (developer.paypal.com > Testing Tools > Sandbox Accounts). Never use real credentials.

## Deploy (Render)
Push this repo to GitHub, then Render > New > Blueprint > select the repo. Set the env vars above, deploy, then set
`APP_URL` to the service URL and redeploy. Free instances sleep after inactivity, so the first request can be slow.

## License
MIT (see `LICENSE`). The product data in `data/catalogue.csv` is derived from third-party public datasets and keeps the license of its sources.

## Data and limitations (honest notes)
- **Data source:** the product catalogue (about 14,000 listings) is built from publicly shared datasets of Flipkart product listings (mobiles, laptops, smartwatches, refrigerators, washing machines) and a Myntra product catalogue, downloaded from Kaggle. These datasets belong to their original authors and keep **their own licenses** (the MIT license above covers the AgentPay code only). The data is used here for a non-commercial hackathon demo. If you are an owner and want a file removed, please open an issue and it will be taken down.
- Exact dataset links: _to be added_.
- The data is a past snapshot, so prices are **not live**, and there are no product photos (the UI shows illustrated placeholders). Fashion listings have no rating data, so the rating rule shows "not available" for them.
- Payments run in the PayPal **sandbox** at a fixed demo rate (INR 85 = USD 1). No real money moves, and nothing ships.
- Orders and the audit log live in memory and a local JSON file (demo scope).
