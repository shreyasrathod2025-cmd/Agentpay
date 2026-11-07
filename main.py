import os, re, uuid, json, csv, math
import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pathlib import Path
from datetime import datetime, timezone



def load_env():
    """Read .env next to main.py. Handles BOM / UTF-16 (Notepad), spaces and quotes."""
    f = Path(__file__).parent / ".env"
    if not f.exists():
        print("[AgentPay] .env NOT FOUND at", f)
        return
    raw = f.read_bytes()
    text = raw.decode("utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else raw.decode("utf-8-sig", errors="ignore")
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ[k.strip().lstrip("\ufeff")] = v.strip().strip('"').strip("'")


load_env()
CID, SECRET = os.getenv("PAYPAL_CLIENT_ID"), os.getenv("PAYPAL_SECRET")
ANTHROPIC_KEY = os.getenv("ANTHROPIC_API_KEY")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")   # free key from aistudio.google.com/apikey
LLM_KEY, LLM_URL, LLM_MODEL = os.getenv("LLM_API_KEY"), os.getenv("LLM_BASE_URL"), os.getenv("LLM_MODEL")   # any OpenAI-compatible API
MODEL = os.getenv("ANTHROPIC_MODEL", "claude-sonnet-5-5")
APP_URL = os.getenv("APP_URL") or os.getenv("RENDER_EXTERNAL_URL") or "http://localhost:8000"
BASE = "https://api-m.sandbox.paypal.com"
print(f"[AgentPay] PayPal client id loaded: {bool(CID)} | secret loaded: {bool(SECRET)} | AI: {'gemini' if GEMINI_KEY else 'openai-compatible' if (LLM_KEY and LLM_URL) else 'claude' if ANTHROPIC_KEY else 'none (rule-based fallback)'}")
MIN_RATING = 4.0

BASE_DIR = Path(__file__).parent
AUDIT_FILE = BASE_DIR / "audit.json"
INR_PER_USD = 85.0   # fixed DEMO conversion rate, PayPal sandbox is charged in USD
RULES = {"per_order": 50000.0, "daily_cap": 100000.0, "min_rating": 4.0, "min_reviews": 50.0}   # INR / rating / count
TOK = r"[a-z0-9][a-z0-9\-]*"


def to_usd(inr):
    return max(0.01, round(inr / INR_PER_USD, 2))


def norm(s):
    return re.sub(r"(\d)\s*(gb|tb)", r"\1\2", s.lower())


def load_catalogue():
    f, items = BASE_DIR / "data" / "catalogue.csv", []
    if not f.exists():
        print("[AgentPay] data/catalogue.csv NOT FOUND")
        return items
    num = lambda r, k: float(r[k]) if r.get(k) not in (None, "", "nan") else None
    for r in csv.DictReader(f.open(encoding="utf-8")):
        rc = num(r, "rating_count")
        items.append({"id": r["id"], "title": r["name"], "brand": r["brand"], "category": r["category"], "group": r["group"],
                      "gender": r["gender"], "price": int(float(r["price_inr"])), "mrp": num(r, "mrp_inr"), "rating": num(r, "rating"),
                      "rating_count": int(rc) if rc is not None else None, "specs": r["specs"], "seller": r["source"]})
    return items


CATALOGUE = load_catalogue()
BY_ID = {p["id"]: p for p in CATALOGUE}
HAY = {p["id"]: norm(f"{p['title']} {p['brand']} {p['specs']}") for p in CATALOGUE}
BRANDS = {p["brand"].lower() for p in CATALOGUE if re.fullmatch(TOK, p["brand"].lower())}
SYN = {"mobiles": "mobile mobiles phone phones smartphone smartphones", "laptops": "laptop laptops notebook",
       "smartwatches": "smartwatch smartwatches", "watches": "watch watches wristwatch", "refrigerators": "refrigerator refrigerators fridge",
       "washing_machines": "washing washer", "shoes": "shoes shoe sneakers sneaker sandals sandal footwear boots heels slippers",
       "bags": "bag bags backpack backpacks handbag handbags trolley luggage", "t-shirts": "t-shirt t-shirts tshirt tshirts tee",
       "shirts": "shirt shirts", "jeans & trousers": "jeans trousers pants chinos joggers shorts", "ethnic wear": "kurta kurti saree lehenga ethnic salwar",
       "dresses & tops": "dress dresses top tops skirt jumpsuit", "jackets & sweaters": "jacket jackets hoodie sweater sweatshirt coat blazer",
       "sunglasses": "sunglasses goggles", "jewellery": "jewellery jewelry necklace earrings bracelet", "fragrance": "perfume fragrance deodorant",
       "home decor": "lamp decor candle cushion", "speakers": "speaker speakers soundbar", "kitchen appliances": "mixer grinder kettle cooker toaster",
       "cooling & heating": "cooler fan heater geyser"}
WORD2CAT = {}
for _c, _ws in SYN.items():
    for _w in _ws.split():
        WORD2CAT.setdefault(_w, set()).add(_c)
WORD2CAT["watch"] = WORD2CAT["watches"] = {"watches", "smartwatches"}
WORD2CAT["shirt"] = WORD2CAT["shirts"] = {"shirts", "t-shirts"}
GENDER = {"men": "Men", "man": "Men", "mens": "Men", "male": "Men", "women": "Women", "woman": "Women", "womens": "Women", "ladies": "Women",
          "girls": "Girls", "boys": "Boys", "kids": "Kids"}
GENDER_OK = {"Men": {"Men", "Unisex"}, "Women": {"Women", "Unisex"}, "Girls": {"Girls", "Women", "Unisex Kids"}, "Boys": {"Boys", "Men", "Unisex Kids"},
             "Kids": {"Boys", "Girls", "Unisex Kids"}}
STOP = set("find best a an the for me under below max within upto up to less than from of with and in on rs inr verified sellers seller good cheap buy get want need show please new".split())

try:
    AUDIT = json.loads(AUDIT_FILE.read_text())
except Exception:
    AUDIT = []


def log(event, detail, order=None, amount=0.0):
    AUDIT.append({"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "event": event,
                  "detail": detail, "order": order, "amount": amount})
    try:
        AUDIT_FILE.write_text(json.dumps(AUDIT[-200:], indent=1))
    except Exception:
        pass


def spent_today():
    d = datetime.now(timezone.utc).date().isoformat()
    return sum(a["amount"] for a in AUDIT if a["event"] == "authorized" and a["ts"].startswith(d))


ORDERS = {}  # order_id -> dict (in-memory, demo only)
app = FastAPI()


async def pp(method, path, body=None, form=False):
    if not CID or not SECRET:
        raise HTTPException(500, "PAYPAL_CLIENT_ID / PAYPAL_SECRET missing. Check your .env file and restart the server.")
    async with httpx.AsyncClient(timeout=30) as c:
        if form:
            r = await c.post(BASE + path, auth=(CID, SECRET), data={"grant_type": "client_credentials"})
        else:
            t = await pp("POST", "/v1/oauth2/token", form=True)
            r = await c.request(method, BASE + path, json=body, headers={
                "Authorization": f"Bearer {t['access_token']}",
                "Content-Type": "application/json", "PayPal-Request-Id": str(uuid.uuid4())})
    if r.status_code >= 400:
        raise HTTPException(r.status_code, r.text)
    return r.json() if r.text else {}


def evaluate(item, budget, min_rating=None, spent=None):
    R = RULES
    mr = min_rating if min_rating is not None else R["min_rating"]
    spent = spent_today() if spent is None else spent
    chk = [{"label": f"Price \u20b9{item['price']:,}" + (f" (budget \u20b9{budget:,.0f})" if budget is not None else " (no budget set)"),
            "ok": budget is None or item["price"] <= budget}]
    if item["rating"] is None:
        chk.append({"label": "Rating not available in this dataset", "ok": True, "na": True})
    else:
        chk.append({"label": f"Rating {item['rating']} (minimum {mr})", "ok": item["rating"] >= mr})
        chk.append({"label": f"{item['rating_count']:,} reviews (minimum {R['min_reviews']:.0f})", "ok": item["rating_count"] >= R["min_reviews"]})
    chk.append({"label": f"Per-order limit (\u20b9{R['per_order']:,.0f})", "ok": item["price"] <= R["per_order"]})
    chk.append({"label": f"Daily cap (\u20b9{spent:,.0f} of \u20b9{R['daily_cap']:,.0f} used)", "ok": spent + item["price"] <= R["daily_cap"]})
    return chk


def check_rules(item, budget, min_rating=None):
    return [c["label"] for c in evaluate(item, budget, min_rating) if not c["ok"]]


def parse_budget(prompt):
    m = re.search(r"(?:under|below|max|within|upto|up to|less than|<)\s*(?:rs\.?|inr|\u20b9)?\s*(\d[\d,]*(?:\.\d+)?)\s*(k|thousand|lakh|lac)?", prompt, re.I)
    if not m:
        return None
    u = (m.group(2) or "").lower()
    return float(m.group(1).replace(",", "")) * (1000 if u in ("k", "thousand") else 100000 if u in ("lakh", "lac") else 1)


SYSTEM = """You extract shopping intent. Reply with ONLY a JSON object:
{"keywords": [lowercase tokens: product type, brand, specs like "128gb", color, gender], "budget": number in INR or null, "min_rating": number or null}
No explanation, no markdown."""


def llm_name():
    return "gemini" if GEMINI_KEY else "openai-compatible" if (LLM_KEY and LLM_URL) else "claude" if ANTHROPIC_KEY else None


GEMINI_MODELS = []


async def gemini_models(c):
    """Ask Google which Gemini flash models this key can use (model names change over time)."""
    if GEMINI_MODELS:
        return GEMINI_MODELS
    if LLM_MODEL:
        GEMINI_MODELS.append(LLM_MODEL)
        return GEMINI_MODELS
    try:
        r = await c.get("https://generativelanguage.googleapis.com/v1beta/models?pageSize=200", headers={"x-goog-api-key": GEMINI_KEY})
        r.raise_for_status()
        names = [m["name"].split("/")[-1] for m in r.json().get("models", []) if "generateContent" in m.get("supportedGenerationMethods", [])]
        flash = [n for n in names if re.fullmatch(r"gemini-[\d.]+-flash(-lite)?", n)]
        flash.sort(key=lambda n: (-("lite" in n), -float(re.search(r"gemini-([\d.]+)", n).group(1))))   # lite first (higher free limits), newest first
        GEMINI_MODELS.extend(flash[:3])
    except Exception:
        pass
    if not GEMINI_MODELS:
        GEMINI_MODELS.extend(["gemini-2.5-flash-lite", "gemini-2.5-flash"])
    return GEMINI_MODELS


async def llm(system, user, max_tokens=300):
    """One call to whichever AI provider has a key: Gemini (free tier), any OpenAI-compatible API, or Claude."""
    async with httpx.AsyncClient(timeout=25) as c:
        if GEMINI_KEY:
            last = None
            for model in list(await gemini_models(c)):
                r = await c.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                    headers={"x-goog-api-key": GEMINI_KEY},
                    json={"systemInstruction": {"parts": [{"text": system}]}, "contents": [{"role": "user", "parts": [{"text": user}]}],
                          "generationConfig": {"maxOutputTokens": max(max_tokens, 600), "temperature": 0.2}})
                if r.status_code in (404, 429):   # model missing or out of quota: try the next one
                    last = r
                    continue
                r.raise_for_status()
                GEMINI_MODELS.insert(0, GEMINI_MODELS.pop(GEMINI_MODELS.index(model)))   # remember the model that works
                return r.json()["candidates"][0]["content"]["parts"][0]["text"]
            last.raise_for_status()
        if LLM_KEY and LLM_URL:
            r = await c.post(LLM_URL.rstrip("/") + "/chat/completions", headers={"Authorization": f"Bearer {LLM_KEY}"},
                json={"model": LLM_MODEL, "max_tokens": max_tokens, "temperature": 0.2,
                      "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]})
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
        r = await c.post("https://api.anthropic.com/v1/messages", headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01"},
            json={"model": MODEL, "max_tokens": max_tokens, "system": system, "messages": [{"role": "user", "content": user}]})
        r.raise_for_status()
        return r.json()["content"][0]["text"]


INTENT_CACHE = {}


async def parse_intent(prompt):
    intent = {"budget": parse_budget(prompt), "min_rating": None, "tokens": set(re.findall(TOK, prompt.lower())), "mode": "regex", "note": None}
    if not llm_name():
        intent["note"] = "No AI key in .env (set GEMINI_API_KEY, free from aistudio.google.com/apikey)"
        return intent
    try:
        if prompt not in INTENT_CACHE:   # cache saves free-tier quota
            text = await llm(SYSTEM, prompt, 300)
            INTENT_CACHE[prompt] = json.loads(re.search(r"\{.*\}", text, re.S).group(0))
        data = INTENT_CACHE[prompt]
        kw = [str(k).lower() for k in data.get("keywords", [])]
        if kw:
            intent["tokens"] = set(kw)
        if isinstance(data.get("budget"), (int, float)):
            intent["budget"] = float(data["budget"])
        if isinstance(data.get("min_rating"), (int, float)):
            intent["min_rating"] = float(data["min_rating"])
        intent["mode"] = "llm"
    except Exception as e:
        intent["note"] = f"AI call failed: {str(e)[:140]}"
        print("AI parse failed, regex fallback:", e)
    return intent


class SearchReq(BaseModel):
    prompt: str


class OrderReq(BaseModel):
    product_id: str
    budget: float | None = None
    min_rating: float | None = None


async def choose(deals, n_blocked, prompt):
    """The AI picks the best option, but only among deals that already passed every rule."""
    fb = f"{deals[0]['title'][:70]} ranked best on price, rating and discount among {len(deals)} rule-compliant option(s); {n_blocked} were blocked."
    if len(deals) < 2 or not llm_name():
        return deals, fb
    lines = "\n".join(f"{i}: {d['title'][:80]} | Rs {d['price']} | rating {d['rating'] or 'n/a'} ({d['rating_count'] or 0} reviews) | {d['discount'] or 0}% off" for i, d in enumerate(deals))
    try:
        out = await llm('You are a shopping agent. Pick the best option for the buyer request. Reply ONLY with JSON: {"pick": <index>, "reason": "<one short sentence>"}',
                        f"Request: {prompt}\nOptions:\n{lines}", 200)
        j = json.loads(re.search(r"\{.*\}", out, re.S).group(0))
        i = int(j["pick"])
        if 0 <= i < len(deals):
            deals = [deals[i]] + deals[:i] + deals[i + 1:]
        return deals, str(j.get("reason") or fb)[:300]
    except Exception:
        return deals, fb


KEYS = ("id", "title", "brand", "category", "group", "gender", "price", "mrp", "rating", "rating_count", "seller", "specs")


def view(p, chk):
    d = {k: p[k] for k in KEYS}
    d["checks"], d["usd"] = chk, to_usd(p["price"])
    d["discount"] = round((p["mrp"] - p["price"]) / p["mrp"] * 100) if p["mrp"] and p["mrp"] > p["price"] else None
    return d


@app.post("/api/agent/search")
async def search(req: SearchReq):
    it = await parse_intent(req.prompt)
    budget, min_rating = it["budget"], max(RULES["min_rating"], it["min_rating"] or 0)  # LLM can only make rules stricter
    tokens = set(re.findall(TOK, " ".join(it["tokens"])))
    cats = set().union(*[WORD2CAT.get(t, set()) for t in tokens])
    base = {"budget": budget, "min_rating": min_rating, "mode": it["mode"], "llm": llm_name(), "note": it["note"], "scanned": len(CATALOGUE),
            "matched": 0, "best": None, "deals": [], "rejected": [], "reasoning": None, "unsupported": not cats, "supported": sorted(SYN)}
    if not cats:
        log("request", f'"{req.prompt[:80]}" - category not supported')
        return base
    gf = next((GENDER_OK[GENDER[t]] for t in tokens if t in GENDER), None)
    req_t = [t for t in tokens if t in BRANDS or re.fullmatch(r"\d+(gb|tb)|5g", t)]
    soft = [t for t in tokens if t not in STOP and t not in GENDER and t not in req_t and not re.fullmatch(r"\d+k?", t)]
    spent, ok, bad = spent_today(), [], []
    for p in CATALOGUE:
        if p["category"] not in cats or (gf and p["gender"] and p["gender"] not in gf):
            continue
        h = HAY[p["id"]]
        if any(t not in h for t in req_t):
            continue
        rel = sum(1 for t in soft if t in h)
        chk = evaluate(p, budget, min_rating, spent)
        val = (p["rating"] * 2 + math.log10((p["rating_count"] or 0) + 1) * 2 + (p["mrp"] and (p["mrp"] - p["price"]) / p["mrp"] * 10 or 0)) if p["rating"] else 0
        (ok if all(c["ok"] for c in chk) else bad).append((rel, val, p, chk))
    ok.sort(key=lambda x: (-x[0], -x[1], x[2]["price"]))
    bad.sort(key=lambda x: (-x[0], x[2]["price"]))
    deals, rejected = [view(p, c) for _, _, p, c in ok[:5]], [view(p, c) for _, _, p, c in bad[:4]]
    reasoning = None
    if deals:
        deals, reasoning = await choose(deals, len(bad), req.prompt)
    best = deals[0] if deals else None
    log("request", f'"{req.prompt[:80]}" parsed by {it["mode"]}: budget {budget}, min rating {min_rating}')
    log("rules", f"{len(ok) + len(bad)} matched, {len(ok)} passed, {len(bad)} blocked" + (f"; recommended {best['title'][:60]}" if best else "; purchase blocked"))
    return {**base, "matched": len(ok) + len(bad), "passed": len(ok), "best": best, "deals": deals, "rejected": rejected,
            "reasoning": reasoning}


@app.get("/api/audit")
def audit():
    return {"events": AUDIT[-30:][::-1], "spent_today": spent_today(), "daily_cap": RULES["daily_cap"]}


@app.get("/api/rules")
def get_rules():
    return {**RULES, "inr_per_usd": INR_PER_USD, "products": len(CATALOGUE), "categories": sorted(SYN)}


class RulesReq(BaseModel):
    per_order: float
    daily_cap: float
    min_rating: float
    min_reviews: float


@app.post("/api/rules")
def set_rules(r: RulesReq):
    if r.per_order <= 0 or r.daily_cap <= 0 or not 0 <= r.min_rating <= 5 or r.min_reviews < 0:
        raise HTTPException(400, "Invalid rule values")
    RULES.update(per_order=r.per_order, daily_cap=r.daily_cap, min_rating=r.min_rating, min_reviews=r.min_reviews)
    log("settings", f"Rules updated: per-order \u20b9{r.per_order:,.0f}, daily cap \u20b9{r.daily_cap:,.0f}, min rating {r.min_rating}, min reviews {r.min_reviews:.0f}")
    return RULES


@app.post("/api/reset")
def reset():
    AUDIT.clear()
    try:
        AUDIT_FILE.write_text("[]")
    except Exception:
        pass
    return {"ok": True}


@app.post("/api/orders/create")
async def create_order(req: OrderReq):
    item = BY_ID.get(req.product_id)
    if not item:
        raise HTTPException(404, "Product not found")
    fails = check_rules(item, req.budget, max(RULES["min_rating"], req.min_rating or 0))  # server-side re-check: never trust the client/LLM
    if fails:
        log("blocked", f"{item['title']}: " + "; ".join(fails))
        raise HTTPException(400, "Blocked: " + "; ".join(fails))
    usd = to_usd(item["price"])
    d = await pp("POST", "/v2/checkout/orders", {
        "intent": "AUTHORIZE",
        "purchase_units": [{"reference_id": item["id"], "description": item["title"][:120],
                            "amount": {"currency_code": "USD", "value": f"{usd:.2f}"}}],
        "payment_source": {"paypal": {"experience_context": {
            "return_url": f"{APP_URL}/paypal/return", "cancel_url": f"{APP_URL}/",
            "user_action": "PAY_NOW", "shipping_preference": "NO_SHIPPING"}}}})
    link = next((l["href"] for l in d.get("links", []) if l["rel"] in ("payer-action", "approve")), None)
    if not link:
        raise HTTPException(502, f"PayPal returned no approval link: {str(d)[:300]}")
    ORDERS[d["id"]] = {"item": item, "usd": usd, "status": "Pending user approval", "auth_id": None}
    log("approved", f"User approved {item['title']} (\u20b9{item['price']:,} = ${usd:.2f}); PayPal order created", d["id"])
    return {"order_id": d["id"], "approve_url": link}


RECEIPT = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>AgentPay - Receipt</title>
<link rel=stylesheet href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600&family=Playfair+Display:wght@400;500&display=swap">
<style>
*{box-sizing:border-box}body{margin:0;background:#faf8f5;color:#111;font:14.5px/1.55 Inter,system-ui,-apple-system,"Segoe UI",sans-serif}
header{background:#000;color:#fff;padding:18px 24px;font:500 20px "Playfair Display",Georgia,serif;letter-spacing:4px}header span{font:500 10px Inter,sans-serif;letter-spacing:2px;color:#d9b07a;margin-left:12px}
.wrap{max-width:540px;margin:40px auto;padding:0 16px}.card{background:#fff;border:1px solid #e8e2da;padding:34px}
.ok{width:46px;height:46px;border-radius:50%;background:#e1efe3;color:#2f6b3a;display:grid;place-items:center;font-size:22px;margin-bottom:14px}
h1{font:500 28px "Playfair Display",Georgia,serif;margin:0 0 4px}p{margin:0;color:#6b6b6b}
dl{margin:24px 0;display:grid;grid-template-columns:120px 1fr;gap:11px 12px;font-size:14px}dt{color:#6b6b6b}dd{margin:0;word-break:break-word}
.pill{display:inline-block;padding:3px 11px;background:#fbf1dc;color:#7a5a12;font-size:12px;font-weight:600;letter-spacing:.4px}.pill.done{background:#e1efe3;color:#2f6b3a}
button{width:100%;padding:14px;border:1px solid #000;background:#000;color:#fff;font:600 12px Inter,sans-serif;letter-spacing:1.4px;text-transform:uppercase;cursor:pointer}button:disabled{opacity:.5;cursor:default}
a{display:block;text-align:center;margin-top:16px;color:#111;font-size:13px}.note{margin-top:12px;font-size:12px;color:#6b6b6b;text-align:center}
</style>
<header>AGENTPAY<span>SANDBOX</span></header>
<div class=wrap><div class=card>
<div class=ok>&#10003;</div><h1 id=h>__HEAD__</h1><p id=sub>__SUB__</p>
<dl><dt>Item</dt><dd>__TITLE__</dd><dt>Seller</dt><dd>__SELLER__</dd><dt>Amount</dt><dd>\u20b9__PRICE__ <span style=\"color:#64748b\">(sandbox charge: $__USD__ USD, demo rate)</span></dd>
<dt>Status</dt><dd><span id=s class="pill __PCLS__">__STATUS__</span></dd><dt>Order ID</dt><dd>__ORDER__</dd><dt>Authorization</dt><dd>__AUTH__</dd></dl>
<button id=b onclick="cap()" __DIS__>Delivery confirmed - release payment to seller</button>
<div class=note>Simulates the seller shipping and the buyer releasing the held payment (PayPal capture).</div>
<a href="/">&larr; Back to AgentPay</a></div></div>
<script>
function cap(){b.disabled=true;b.textContent='Releasing...';
fetch('/api/orders/__ORDER__/capture',{method:'POST'}).then(r=>r.json()).then(j=>{
s.textContent=j.status||'Error';s.className='pill done';b.textContent='Payment released';h.textContent='Payment released';sub.textContent='The held amount has been captured and sent to the seller.'}).catch(()=>{b.disabled=false;b.textContent='Retry'})}
</script>"""


@app.get("/paypal/return", response_class=HTMLResponse)
async def paypal_return(token: str):
    o = ORDERS.get(token)
    if not o:
        raise HTTPException(404, "Unknown order")
    if not o["auth_id"]:  # authorize only once, so a page refresh is safe
        d = await pp("POST", f"/v2/checkout/orders/{token}/authorize", {})
        o["auth_id"] = d["purchase_units"][0]["payments"]["authorizations"][0]["id"]
        o["status"] = "Authorized (on hold)"
        log("authorized", "PayPal authorization created; funds on hold", token, o["item"]["price"])
    done = o["status"].startswith("Captured")
    it = o["item"]
    return (RECEIPT.replace("__TITLE__", it["title"]).replace("__SELLER__", it["seller"])
            .replace("__PRICE__", f"{it['price']:,}").replace("__USD__", f"{o['usd']:.2f}").replace("__STATUS__", o["status"])
            .replace("__ORDER__", token).replace("__AUTH__", o["auth_id"])
            .replace("__HEAD__", "Payment released" if done else "Payment authorized")
            .replace("__SUB__", "The held amount has been captured and sent to the seller." if done else "Funds are on hold. Nothing has been charged yet.")
            .replace("__PCLS__", "done" if done else "").replace("__DIS__", "disabled" if done else ""))


@app.post("/api/orders/{order_id}/capture")
async def capture(order_id: str):
    o = ORDERS.get(order_id)
    if not o or not o["auth_id"]:
        raise HTTPException(400, "Order not authorized")
    await pp("POST", f"/v2/payments/authorizations/{o['auth_id']}/capture", {})
    o["status"] = "Captured - payment complete"
    log("captured", "Payment released to seller", order_id)
    return {"status": o["status"]}


@app.get("/sw.js")
def sw():
    return FileResponse(BASE_DIR / "static" / "sw.js", media_type="application/javascript")


@app.get("/")
def home():
    for f in (BASE_DIR / "static" / "index.html", BASE_DIR / "index.html"):
        if f.exists():
            return FileResponse(f)
    return HTMLResponse(f"index.html not found. Put it in the 'static' folder next to main.py. Looked in: {BASE_DIR}", status_code=500)


app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
