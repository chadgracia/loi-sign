import json
import datetime
import urllib.request
import urllib.error
import boto3
from html import escape

# ── Config ────────────────────────────────────────────────────────────────
LOI_PAGE_KEY  = "loi7q-3f9a-k28p"        # access key for this page (?key=...)
PIPELINE_BASE = "https://api.pipelinecrm.com/api/v3"

# ── Deal custom-field IDs (authoritative — from deal-update-form) ──────────
GROSS_FIELD        = "custom_label_3064339"
NET_FIELD          = "custom_label_3064369"
MIN_SIZE_FIELD     = "custom_label_3065488"
MAX_SIZE_FIELD     = "custom_label_3064645"
MGMT_FEE_FIELD     = "custom_label_3940558"
CARRY_FIELD        = "custom_label_3940559"
SELLER_FEE_FIELD   = "custom_label_3940560"
SHARE_COUNT_FIELD  = "custom_label_3070843"
DEAL_TYPE_FIELD    = "custom_label_1958"
STRUCTURE_FIELD    = "custom_label_3064360"
PRICE_STATUS_FIELD = "custom_label_3938750"
BUYER_ENTITY_FIELD = "custom_label_3805785"

SELL_TYPE_ID        = 5011675
SPV_STRUCTURE_ID    = 5077906   # labeled "Fund" in the CRM
DIRECT_STRUCTURE_ID = 6250090
FORWARD_STRUCTURE_ID = 5077903
PRICE_FIRM_ID       = 7000238
PRICE_TIED_ID       = 7000239

# Structure entry ID -> phrase used in the letter. Unknown (6361933) and None (5077909)
# are intentionally absent: they add no clause.
STRUCT_PHRASES = {
    6250090: "direct transfer",
    5077906: "fund interest",
    5077903: "forward",
}

# ── Person custom-field IDs ────────────────────────────────────────────────
INVESTOR_LEVEL_FIELD = "custom_label_3923758"
QP_ID                = 6950564

# ── Auth / API ─────────────────────────────────────────────────────────────
def get_jwt():
    s3 = boto3.client("s3")
    obj = s3.get_object(Bucket="pipeline-token", Key="pipeline-jwt.json")
    return json.loads(obj["Body"].read())["jwt"]

def call_pipeline_api(method, path, jwt):
    req = urllib.request.Request(PIPELINE_BASE + path, method=method)
    req.add_header("Authorization", f"Bearer {jwt}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return {"status": r.status, "data": json.loads(r.read())}
    except urllib.error.HTTPError as e:
        return {"status": e.code, "data": e.read().decode()}
    except Exception as e:
        return {"status": 0, "data": str(e)}

# ── CF helpers ─────────────────────────────────────────────────────────────
def cf_first(cf, key):
    v = cf.get(key)
    if isinstance(v, list):
        return v[0] if v else None
    return v

def cf_list(cf, key):
    v = cf.get(key)
    if isinstance(v, list):
        return v
    return [v] if v not in (None, "") else []

def fmt_money(v):
    try:
        f = float(str(v).replace(",", ""))
        return f"{int(f):,}" if f == int(f) else f"{f:,.2f}"
    except (TypeError, ValueError):
        return None

def address_block(person):
    line1   = person.get("work_address_1")   or person.get("company_address_1")
    city    = person.get("work_city")        or person.get("company_city")
    state   = person.get("work_state")       or person.get("company_state")
    postal  = person.get("work_postal_code") or person.get("company_postal_code")
    country = person.get("work_country")     or person.get("company_country")
    lines = []
    if line1:
        lines.append(line1)
    citystate = ", ".join([p for p in [city, state] if p])
    line2 = " ".join([p for p in [citystate, postal] if p]).strip()
    if line2:
        lines.append(line2)
    if country:
        lines.append(country)
    return [escape(x) for x in lines]

# ── Static CSS (plain string — single braces are fine here) ────────────────
PAGE_CSS = """
* { box-sizing: border-box; }
body { margin:0; background:#f4f5f7; color:#1a1a1a;
  font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif; }
.wrap { max-width:640px; margin:32px auto; background:#fff; border:1px solid #e3e5e8;
  border-radius:10px; overflow:hidden; }
.head { padding:30px 40px 0; text-align:center; }
.head h1 { margin:0; font-size:16px; font-weight:700; letter-spacing:.18em;
  text-transform:uppercase; color:#0f1e35; }
.head .rule { height:2px; background:#0f1e35; width:48px; margin:12px auto 0; }
.body { padding:22px 40px 30px; }
.effdate { text-align:right; font-size:14px; color:#3a4350; margin:0 0 22px; }
.letter { font-size:15px; line-height:1.65; color:#1a1a1a; }
.letter p { margin:0 0 18px; }
.terms-h { font-size:13px; text-transform:uppercase; letter-spacing:.09em;
  color:#5b6472; margin:26px 0 8px; font-weight:700; }
.termsbox { border:1px solid #e6ebf2; border-radius:8px; padding:16px 18px 6px;
  background:#fafbfc; margin-bottom:20px; }
.termsbox label:first-child { margin-top:0; }
.roundnote { background:#fff8e6; border:1px solid #f0e2b6; border-radius:8px;
  padding:14px 16px; font-size:14px; line-height:1.5; margin-bottom:22px; color:#5a4a12; }
.feebox { border:1px solid #e6ebf2; border-radius:8px; padding:14px 18px; margin-bottom:22px; }
.feebox h3 { margin:0 0 4px 0; font-size:13px; text-transform:uppercase;
  letter-spacing:.08em; color:#5b6472; }
.feebox .note { font-size:12px; color:#8a929d; margin-bottom:10px; }
.feebox .frow { display:flex; justify-content:space-between; padding:4px 0; font-size:14px; }
label { display:block; font-size:13px; color:#5b6472; margin:14px 0 6px; }
input[type=text] { width:100%; padding:11px 13px; font-size:16px; border:1px solid #cfd5dd;
  border-radius:7px; }
input.feein { width:130px; padding:7px 9px; font-size:14px; text-align:right; }
.prefill { color:#9aa1ab; }
.hint { font-size:12px; color:#8a929d; margin-top:6px; }
.req { color:#c0392b; font-weight:700; }
input.invalid { border-color:#c0392b; background:#fdecef; }
.sig { margin-top:26px; border-top:1px dashed #cfd5dd; padding-top:20px; }
.sig .name { font-size:14px; font-weight:400; color:#1a1a1a; }
.sig .meta { font-size:14px; color:#5b6472; margin-top:0; line-height:1.45; }
.signwrap { margin-top:30px; }
.sigrule { width:220px; border-top:1px solid #1a1a1a; margin-bottom:4px; }
.signbtn { display:inline-block; background:none; border:none; padding:0 4px; cursor:pointer;
  font-family:'Snell Roundhand','Segoe Script','Brush Script MT',cursive;
  font-size:28px; line-height:1; color:#16307a; }
.foot { padding:14px 28px; font-size:12px; color:#9aa1ab; border-top:1px solid #eef1f5; }
.preview { background:#fdecef; color:#a01329; font-size:12px; text-align:center;
  padding:7px; font-weight:600; letter-spacing:.03em; }
"""

def render_loi(deal, person, role=""):
    cf   = deal.get("custom_fields", {}) or {}
    pcf  = person.get("custom_fields", {}) or {}

    # Side follows the deal's own listing (Sell Order -> sale letter, Buy Order -> purchase
    # letter). An explicit role= overrides it (e.g. the Bid button will pass role=buyer).
    type_ids     = [int(x) for x in cf_list(cf, DEAL_TYPE_FIELD) if x]
    deal_is_sell = SELL_TYPE_ID in type_ids
    if role == "seller":
        is_sell = True
    elif role == "buyer":
        is_sell = False
    else:
        is_sell = deal_is_sell
    action_verb = "sell" if is_sell else "purchase"
    action_noun = "Sale" if is_sell else "Purchase"

    # structure
    struct_ids = [int(x) for x in cf_list(cf, STRUCTURE_FIELD) if x]
    is_spv     = SPV_STRUCTURE_ID in struct_ids
    is_direct  = DIRECT_STRUCTURE_ID in struct_ids
    structure_label = "SPV" if is_spv else ("Direct" if is_direct else "—")

    # price status
    ps = cf_first(cf, PRICE_STATUS_FIELD)
    try:
        ps = int(ps) if ps is not None else None
    except (TypeError, ValueError):
        ps = None
    tied = (ps == PRICE_TIED_ID)

    security   = (deal.get("company") or {}).get("name") or deal.get("company_name") or "—"
    side_label = "Sellside" if is_sell else "Buyside"
    price      = fmt_money(cf_first(cf, NET_FIELD if is_sell else GROSS_FIELD))
    price_word = "net" if is_sell else "gross"
    target   = fmt_money(cf_first(cf, MAX_SIZE_FIELD))

    # signer block
    signer_name  = escape(person.get("full_name") or "")
    signer_title = escape(person.get("position") or "")
    _entity_raw  = person.get("company_name") if is_sell else (cf_first(cf, BUYER_ENTITY_FIELD) or person.get("company_name"))
    if _entity_raw and security and _entity_raw.strip().lower() == security.strip().lower():
        _entity_raw = ""   # a signer's entity should never be the company being traded
    entity       = escape(_entity_raw or "")
    addr_lines   = address_block(person)
    _now         = datetime.datetime.utcnow()
    today        = _now.strftime("%B %-d, %Y")
    expiry       = (_now + datetime.timedelta(days=30)).strftime("%B %-d, %Y")

    # investor-level party label (used later on the seller notice; shown here for reference)
    inv = cf_first(pcf, INVESTOR_LEVEL_FIELD)
    party_label = "Qualified Purchaser" if str(inv) == str(QP_ID) else ("Seller" if is_sell else "Buyer")

    # ── letter recital (side-aware) ──
    entity_or_name = entity or signer_name or "_____"
    if is_sell:
        txn_lc       = "sale"
        signer_party = "Seller"
    else:
        txn_lc       = "purchase"
        signer_party = "Purchaser"

    # Structure clause: "via direct transfer", "via fund interest or forward", etc.
    _phrases = [STRUCT_PHRASES[i] for i in struct_ids if i in STRUCT_PHRASES]
    struct_clause = (" via " + " or ".join(_phrases)) if _phrases else ""

    recital = f'''
      <p>This Letter of Intent, dated as of {escape(today)} (the &ldquo;Effective Date&rdquo;),
      sets forth the principal terms of the {txn_lc} of shares in {escape(security)}
      (the &ldquo;Company&rdquo;) by {entity_or_name} (&ldquo;{signer_party}&rdquo;){struct_clause}.
      The {signer_party} intends to be, and is, bound by the terms set forth herein.
      This letter shall be superseded in its entirety by any transfer notice or share purchase
      agreement subsequently executed between the parties, the terms of which shall control.</p>
    '''

    # fee values (used by both the editable box and the closing terms paragraph)
    mgmt  = cf_first(cf, MGMT_FEE_FIELD) or "0"
    carry = cf_first(cf, CARRY_FIELD) or "0"
    sfee  = cf_first(cf, SELLER_FEE_FIELD) or "0"

    # ── closing terms paragraph (prose restatement, updates live from the inputs) ──
    consideration = "aggregate consideration" if is_sell else "aggregate purchase price"
    if tied:
        price_clause = ("at a per-share price to be established at the net per-share price "
                        "determined upon the closing of the Company&rsquo;s current financing "
                        "round or tender")
    else:
        price_clause = (f"at a {price_word} price of $<span id=\"tp-price\">{price or '&mdash;'}</span> "
                        f"per share")
    fee_clause = ""
    if is_spv:
        fee_clause = (f" This price is exclusive of a management fee of "
                      f"<span id=\"tp-mgmt\">{escape(str(mgmt))}</span>%, carried interest of "
                      f"<span id=\"tp-carry\">{escape(str(carry))}</span>%, and a seller fee of "
                      f"<span id=\"tp-sfee\">{escape(str(sfee))}</span>%, as well as "
                      f"Rainmaker Securities&rsquo; commission.")
    terms_para = f'''
      <p>Accordingly, the {signer_party} proposes to {action_verb} shares of the Company for an
      {consideration} of up to $<span id="tp-size">{target or '&mdash;'}</span>,
      {price_clause}.{fee_clause} The terms set forth herein shall remain open for acceptance
      until {escape(expiry)}, after which this letter expires automatically without further
      action by either party.</p>
    '''

    # ── price / size entry ──
    FIRM_MIN  = 100000
    buyer_req = '' if is_sell else ' data-required="1"'
    req_star  = '' if is_sell else ' <span class="req">*</span>'
    if is_sell:
        min_raw  = FIRM_MIN
        min_disp = "100,000"
    else:
        try:
            min_raw = int(float(str(cf_first(cf, MIN_SIZE_FIELD)).replace(",", "")))
        except (TypeError, ValueError):
            min_raw = 0
        min_disp = fmt_money(cf_first(cf, MIN_SIZE_FIELD)) or ""
    size_hint = (f'<div class="hint" style="margin-bottom:14px;">Minimum: ${min_disp}.</div>'
                 if min_disp else '')
    size_input = (f'<input type="text" name="size" class="prefill" value="{min_disp}"{buyer_req} '
                  f'data-min="{min_raw}" oninput="this.classList.remove(\'prefill\');syncTerms()" '
                  f'onblur="clampMin(this)">')
    if tied:
        price_section = f'''
      <div class="roundnote">Purchase price: to be established at the net per-share price
      determined upon closing of {escape(security)}'s current financing round or tender.</div>
      <label>Size you intend to {action_verb} (USD){req_star}</label>
      {size_input}
      {size_hint}
        '''
    else:
        price_section = f'''
      <label>{price_word.capitalize()} price per share (USD) — current price shown; confirm or adjust</label>
      <input type="text" name="gross" class="prefill" value="{price or ''}" oninput="this.classList.remove('prefill');syncTerms()">
      <label>Size you intend to {action_verb} (USD){req_star}</label>
      {size_input}
      {size_hint}
        '''

    # ── SPV fee stack (shown for SPV, firm or tied) ──
    fee_section = ""
    if is_spv:
        fee_section = f'''
      <div class="feebox">
        <h3>SPV terms to confirm</h3>
        <div class="frow"><span>Management fee{req_star}</span>
          <input type="text" name="mgmt_fee" class="feein prefill" value="{escape(str(mgmt))}"{buyer_req} oninput="this.classList.remove('prefill');syncTerms()"></div>
        <div class="frow"><span>Carry{req_star}</span>
          <input type="text" name="carry" class="feein prefill" value="{escape(str(carry))}"{buyer_req} oninput="this.classList.remove('prefill');syncTerms()"></div>
        <div class="frow"><span>Seller fee</span>
          <input type="text" name="seller_fee" class="feein prefill" value="{escape(str(sfee))}" oninput="this.classList.remove('prefill');syncTerms()"></div>
      </div>
        '''

    # ── signer block ──
    meta_bits = []
    if signer_title:
        meta_bits.append(signer_title)
    if entity:
        meta_bits.append(entity)
    meta_line = " &middot; ".join(meta_bits)
    addr_html = "<br>".join(addr_lines)
    sig = f'''
      <div class="sig">
        <div class="name">{signer_name}</div>
        <div class="meta">{meta_line}{('<br>' + addr_html) if addr_html else ''}</div>
      </div>
    '''

    return f'''<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Letter of Intent</title><style>{PAGE_CSS}</style></head>
<body>
  <div class="preview">PREVIEW — layout only, nothing is saved or sent</div>
  <div class="wrap">
    <div class="head">
      <h1>Letter of Intent &mdash; {side_label}</h1>
      <div class="rule"></div>
    </div>
    <div class="body">
      <div class="effdate">{escape(today)}</div>
      <div class="terms-h">The Terms</div>
      <div class="termsbox">
      {price_section}
      {fee_section}
      </div>
      <div class="letter">{recital}{terms_para}</div>
      {sig}
      <div class="signwrap">
        <button class="signbtn" onclick="if(!validateReq())return false;alert('Preview only — signing is not wired up yet.');return false;">Click to sign</button>
        <div class="sigrule"></div>
      </div>
    </div>
  </div>
  <script>
  function clampMin(el) {{
    var min = parseFloat(el.getAttribute('data-min')) || 0;
    var val = parseFloat((el.value || '').replace(/,/g, ''));
    if (!isNaN(val) && val < min) {{ el.value = min.toLocaleString('en-US'); }}
    syncTerms();
  }}
  function validateReq() {{
    var ok = true;
    document.querySelectorAll('[data-required]').forEach(function(el) {{
      if (el.value.trim() === '') {{ el.classList.add('invalid'); ok = false; }}
      else {{ el.classList.remove('invalid'); }}
    }});
    if (!ok) {{ alert('Please complete the required fields.'); }}
    return ok;
  }}
  function syncTerms() {{
    function grp(s) {{
      var raw = s.replace(/,/g, '').trim();
      var n = parseFloat(raw);
      if (isNaN(n)) return s;
      if (n === Math.floor(n)) return n.toLocaleString('en-US');
      return n.toLocaleString('en-US', {{minimumFractionDigits: 2, maximumFractionDigits: 2}});
    }}
    function put(id, name, numeric) {{
      var span = document.getElementById(id);
      var input = document.querySelector('[name="' + name + '"]');
      if (span && input && input.value.trim() !== '') {{
        span.textContent = numeric ? grp(input.value) : input.value.trim();
      }}
    }}
    put('tp-size', 'size', true);
    put('tp-price', 'gross', true);
    put('tp-mgmt', 'mgmt_fee', false);
    put('tp-carry', 'carry', false);
    put('tp-sfee', 'seller_fee', false);
  }}
  </script>
</body></html>'''

def _resp(code, body, ctype):
    return {"statusCode": code, "headers": {"Content-Type": ctype}, "body": body}

def lambda_handler(event, context):
    params = (event.get("queryStringParameters") or {})
    if params.get("key", "") != LOI_PAGE_KEY:
        return _resp(403, "forbidden", "text/plain")
    deal_id = params.get("deal_id", "")
    if not deal_id:
        return _resp(400, "missing deal_id", "text/plain")
    jwt = get_jwt()
    dr = call_pipeline_api("GET", f"/deals/{deal_id}.json", jwt)
    if dr["status"] != 200:
        return _resp(404, f"deal not found ({deal_id}) — {dr['status']}", "text/plain")
    deal = dr["data"]
    person = {}
    contact_id = (deal.get("primary_contact") or {}).get("id")
    if contact_id:
        pr = call_pipeline_api("GET", f"/people/{contact_id}.json", jwt)
        if pr["status"] == 200:
            person = pr["data"]
    role = params.get("role", "").strip().lower()
    if role not in ("buyer", "seller"):
        role = ""   # empty = follow the deal's own side
    return _resp(200, render_loi(deal, person, role), "text/html")
