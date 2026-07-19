import json
import os
import hmac
import base64
import hashlib
import datetime
import urllib.request
import urllib.error
import boto3
from html import escape
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from fpdf import FPDF

SIG_FONT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "GreatVibes-Regular.ttf")

# ── Config ────────────────────────────────────────────────────────────────
# This repo is public — these come from Lambda environment variables, never hardcoded.
LOI_PAGE_KEY     = os.environ.get("LOI_PAGE_KEY", "")
LOI_TOKEN_SECRET = os.environ.get("LOI_TOKEN_SECRET", "")
PIPELINE_BASE = "https://api.pipelinecrm.com/api/v3"
SES_SENDER    = "agent@agent.graciagroup.com"
CHAD_EMAIL    = "cgracia@rainmakersecurities.com"
REDIRECT_URL  = "https://www.graciagroup.com"

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

# ── Company custom-field IDs (last-round data, for the implied valuation) ──
COMPANY_PPS_FIELD = "custom_label_3064363"   # LR PPS
COMPANY_VAL_FIELD = "custom_label_3790429"   # LR Val ($Bn)

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
TRANSACTOR_TYPE_FIELD = "custom_label_3759163"
# Transactor types that sign in their own name and use their home address.
# ── IQF gate (buyers only) ────────────────────────────────────────────────
IQF_FIELD        = "custom_label_3763008"
IQF_YES_ID       = 6496840
IQF_UNNEEDED_ID  = 6596073   # "Unnecessary" — no form required, treat as cleared
IQF_URL_NATURAL  = "https://www.rainmakersecurities.com/investor-qualification-form-for-natural-persons"
IQF_URL_ENTITY   = "https://www.rainmakersecurities.com/investor-qualification-form-for-entity-persons"

INDIVIDUAL_TYPE_IDS = {
    6484810,  # Natural Person
    6716196,  # Employee Holder
    6892622,  # Employee Holder - VIP
    6484809,  # Ex-Employee Holder
}
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

def address_block(person, is_individual=False):
    if is_individual:
        line1   = person.get("home_address_1")
        city    = person.get("home_city")
        state   = person.get("home_state")
        postal  = person.get("home_postal_code")
        country = person.get("home_country")
        # If no home address is on file, fall back to work rather than render blank.
        if not any([line1, city, postal, country]):
            line1   = person.get("work_address_1")   or person.get("company_address_1")
            city    = person.get("work_city")        or person.get("company_city")
            state   = person.get("work_state")       or person.get("company_state")
            postal  = person.get("work_postal_code") or person.get("company_postal_code")
            country = person.get("work_country")     or person.get("company_country")
    else:
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
.reline { font-size:15px; font-weight:600; color:#1a1a1a; margin:0 0 20px; }
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
.feebox h3 { margin:0 0 12px 0; font-size:13px; text-transform:uppercase;
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
.dayrow { display:flex; align-items:center; gap:8px; }
.dayrow input[type=text] { width:56px; text-align:center; padding:8px 6px; }
.dayunit { font-size:14px; color:#5b6472; }
.valbox { margin:6px 0 14px; }
#valtext { font-size:14px; font-weight:600; color:#0f1e35; }
.valnote { font-size:12px; color:#8a929d; margin-top:2px; }
.sig { margin-top:26px; }
.sig .name { font-size:15px; font-weight:400; color:#1a1a1a; line-height:1.65; }
.sig .meta { font-size:15px; color:#1a1a1a; margin-top:0; line-height:1.65; }
.signwrap { margin-top:30px; }
.sigrule { width:220px; border-top:1px solid #1a1a1a; margin-bottom:4px; }
.signbtn { display:inline-block; background:none; border:none; padding:0 4px; cursor:pointer;
  font-family:'Snell Roundhand','Segoe Script','Brush Script MT',cursive;
  font-size:28px; line-height:1; color:#16307a; }
.foot { padding:14px 28px; font-size:12px; color:#9aa1ab; border-top:1px solid #eef1f5; }
.modal { display:none; position:fixed; inset:0; background:rgba(0,0,0,.45); z-index:100;
  align-items:center; justify-content:center; }
.modal.open { display:flex; }
.modalbox { background:#fff; border-radius:12px; padding:36px 32px; max-width:380px;
  text-align:center; box-shadow:0 8px 32px rgba(0,0,0,.18); }
.modalbox h2 { margin:0 0 12px; font-size:18px; color:#0f1e35; }
.modalbox p { margin:0; font-size:14px; color:#5b6472; line-height:1.6; }
.signbtn:disabled { opacity:.45; cursor:not-allowed; }
"""

def render_loi(deal, person, role="", company=None):
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
    # Party display: a Natural Person signs in their own name and shows their home address;
    # any entity type signs in the company's name and shows the work address.
    transactor    = cf_first(pcf, TRANSACTOR_TYPE_FIELD)
    try:
        is_individual = int(float(str(transactor))) in INDIVIDUAL_TYPE_IDS
    except (TypeError, ValueError):
        is_individual = False
    company_nm    = escape(person.get("company_name") or "")
    party_display = signer_name if is_individual else (company_nm or signer_name)
    entity        = "" if is_individual else company_nm
    addr_lines    = address_block(person, is_individual)
    _now         = datetime.datetime.utcnow()
    today        = _now.strftime("%B %-d, %Y")


    # investor-level party label (used later on the seller notice; shown here for reference)
    inv = cf_first(pcf, INVESTOR_LEVEL_FIELD)
    party_label = "Qualified Purchaser" if str(inv) == str(QP_ID) else ("Seller" if is_sell else "Buyer")

    # ── letter recital (side-aware) ──
    entity_or_name = party_display or signer_name or "_____"
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
      This letter shall be superseded in its entirety by any transfer notice, share purchase
      agreement, subscription document or other agreement subsequently executed between
      the parties, the terms of which shall control.</p>
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
        _spv_flag = "1" if is_spv else "0"
        if price:
            _inner = f"at a {price_word} price up to ${price} per share"
        elif is_spv:
            _inner = ("at a net per-share price to be determined by the results of the "
                      "Company&rsquo;s current financing round")
        else:
            _inner = f"at a {price_word} price up to $&mdash; per share"
        price_clause = (f'<span id="tp-priceclause" data-word="{price_word}" '
                        f'data-spv="{_spv_flag}">{_inner}</span>')
    fee_clause = ""
    if is_spv:
        fee_clause = (f" If via fund, this price is exclusive of a management fee of "
                      f"<span id=\"tp-mgmt\">{escape(str(mgmt))}</span>%, carried interest of "
                      f"<span id=\"tp-carry\">{escape(str(carry))}</span>%, and a one-time fee of "
                      f"<span id=\"tp-sfee\">{escape(str(sfee))}</span>%, as well as "
                      f"Rainmaker Securities&rsquo; commission.")
    terms_para = f'''
      <p>Accordingly, the {signer_party} proposes to {action_verb} shares of the Company for an
      {consideration} of up to $<span id="tp-size">{target or '&mdash;'}</span>,
      {price_clause}.{fee_clause}<span id="tp-expclause"></span></p>
    '''

    # ── price / size entry ──
    FIRM_MIN  = 100000
    buyer_req = '' if is_sell else ' data-required="1"'
    req_star  = '' if is_sell else ' <span class="req">*</span>'
    # Price is required for Direct/Forward (both sides). For an SPV the signer may not
    # know it yet, so it stays optional and the letter falls back to round-based wording.
    price_req  = '' if is_spv else ' data-required="1"'
    price_star = '' if is_spv else ' <span class="req">*</span>'
    price_note = " &mdash; current price shown; confirm or adjust" if price else ""

    # IQF gate: buyers only. Yes / Unnecessary clear it; Pending, No or blank do not.
    # A signer we treat as an individual gets the natural-persons form, everyone else
    # the entity form.
    _iqf_raw = cf_first(pcf, IQF_FIELD)
    try:
        _iqf_id = int(float(str(_iqf_raw)))
    except (TypeError, ValueError):
        _iqf_id = 0
    iqf_cleared = is_sell or _iqf_id in (IQF_YES_ID, IQF_UNNEEDED_ID)
    iqf_url     = IQF_URL_NATURAL if is_individual else IQF_URL_ENTITY
    fee_req   = ' data-required="1"'
    fee_star  = ' <span class="req">*</span>'
    fee_heading = "SPV terms" if is_sell else "Max fees you&rsquo;d pay if via SPV"

    # Implied valuation from the last public round: val = lr_val * (price / lr_pps).
    # Same formula the public trades page uses. Hidden entirely if either field is missing.
    ccf = (company or {}).get("custom_fields", {}) or {}
    try:
        lr_pps_f = float(str(cf_first(ccf, COMPANY_PPS_FIELD)).replace(",", ""))
    except (TypeError, ValueError):
        lr_pps_f = 0.0
    try:
        lr_val_f = float(str(cf_first(ccf, COMPANY_VAL_FIELD)).replace(",", ""))
    except (TypeError, ValueError):
        lr_val_f = 0.0
    val_html = ""
    if lr_pps_f > 0 and lr_val_f > 0:
        val_html = (f'<div class="valbox" id="valbox" data-pps="{lr_pps_f}" '
                    f'data-val="{lr_val_f}" style="display:none;">'
                    f'<span id="valtext"></span>'
                    f'<div class="valnote">Valuation based on last public round.</div></div>')
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
    exp_input = ('<label>This letter remains open for</label>'
                 '<div class="dayrow"><input type="text" name="expdays" value="" maxlength="3" '
                 'oninput="syncTerms()"><span class="dayunit">days</span></div>'
                 '<div class="hint" style="margin-bottom:14px;">If empty, good till filled.</div>')
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
      {exp_input}
        '''
    else:
        price_section = f'''
      <label>{price_word.capitalize()} price per share (USD){price_star}{price_note}</label>
      <input type="text" name="gross" class="prefill" value="{price or ''}"{price_req} oninput="this.classList.remove('prefill');syncTerms()">
      {val_html}
      <label>Size you intend to {action_verb} (USD){req_star}</label>
      {size_input}
      {size_hint}
      {exp_input}
        '''

    # ── SPV fee stack (shown for SPV, firm or tied) ──
    fee_section = ""
    if is_spv:
        fee_section = f'''
      <div class="feebox">
        <h3>{fee_heading}</h3>
        <div class="frow"><span>Management fee{fee_star}</span>
          <input type="text" name="mgmt_fee" class="feein prefill" value="{escape(str(mgmt))}"{fee_req} oninput="this.classList.remove('prefill');syncTerms()"></div>
        <div class="frow"><span>Carry{fee_star}</span>
          <input type="text" name="carry" class="feein prefill" value="{escape(str(carry))}"{fee_req} oninput="this.classList.remove('prefill');syncTerms()"></div>
        <div class="frow"><span>One-time fee{fee_star}</span>
          <input type="text" name="seller_fee" class="feein prefill" value="{escape(str(sfee))}"{fee_req} oninput="this.classList.remove('prefill');syncTerms()"></div>
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
  <div class="wrap">
    <div class="head">
      <h1>Letter of Intent &mdash; {side_label}</h1>
      <div class="rule"></div>
    </div>
    <div class="body">
      <div class="reline">Re: {escape(security)}</div>
      <div class="terms-h">The Terms</div>
      <div class="termsbox">
      {price_section}
      {fee_section}
      </div>
      <div class="letter">{recital}{terms_para}</div>
      {sig}
      <div class="signwrap">
        <button id="signbtn" class="signbtn" data-side="{side_label}" data-iqf="{'1' if iqf_cleared else '0'}" data-iqfurl="{iqf_url}" onclick="return doSign();">Click to sign</button>
        <div class="sigrule"></div>
      </div>
    </div>
  </div>
  <div class="modal" id="thankyou">
    <div class="modalbox">
      <h2>Thank you</h2>
      <p id="tymsg">Your letter of intent has been received.<br>We&rsquo;ll be in touch shortly.</p>
      <p style="margin-top:18px;"><a id="tylink" href="{REDIRECT_URL}" style="color:#1652f0;font-weight:600;text-decoration:none;">Continue &rarr;</a></p>
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
  function grp(s) {{
    var raw = s.replace(/,/g, '').trim();
    var n = parseFloat(raw);
    if (isNaN(n)) return s;
    if (n === Math.floor(n)) return n.toLocaleString('en-US');
    return n.toLocaleString('en-US', {{minimumFractionDigits: 2, maximumFractionDigits: 2}});
  }}
  function syncTerms() {{
    function put(id, name, numeric) {{
      var span = document.getElementById(id);
      var input = document.querySelector('[name="' + name + '"]');
      if (span && input && input.value.trim() !== '') {{
        span.textContent = numeric ? grp(input.value) : input.value.trim();
      }}
    }}
    put('tp-size', 'size', true);
    put('tp-mgmt', 'mgmt_fee', false);
    put('tp-carry', 'carry', false);
    put('tp-sfee', 'seller_fee', false);
    syncPrice();
    syncExpiry();
  }}
  function syncPrice() {{
    var span = document.getElementById('tp-priceclause');
    var input = document.querySelector('[name="gross"]');
    if (!span || !input) return;
    var word = span.getAttribute('data-word') || 'gross';
    var spv = span.getAttribute('data-spv') === '1';
    var raw = (input.value || '').trim();
    if (raw === '') {{
      span.innerHTML = spv
        ? 'at a net per-share price to be determined by the results of the Company&rsquo;s current financing round'
        : 'at a ' + word + ' price up to $&mdash; per share';
    }} else {{
      span.innerHTML = 'at a ' + word + ' price up to $' + grp(raw) + ' per share';
    }}
    syncVal();
  }}
  function syncExpiry() {{
    var span = document.getElementById('tp-expclause');
    var input = document.querySelector('[name="expdays"]');
    if (!span || !input) return;
    var raw = (input.value || '').trim();
    var n = parseInt(raw, 10);
    if (raw === '' || isNaN(n) || n <= 0) {{
      span.textContent = '';
      return;
    }}
    var d = new Date();
    d.setDate(d.getDate() + n);
    var txt = d.toLocaleDateString('en-US', {{year: 'numeric', month: 'long', day: 'numeric'}});
    span.textContent = ' The terms set forth herein shall remain open for acceptance until ' +
      txt + ', after which this letter expires automatically without further action.';
  }}
  function syncVal() {{
    var box = document.getElementById('valbox');
    var input = document.querySelector('[name="gross"]');
    if (!box || !input) return;
    var pps = parseFloat(box.getAttribute('data-pps'));
    var val = parseFloat(box.getAttribute('data-val'));
    var raw = parseFloat((input.value || '').replace(/,/g, '').trim());
    if (isNaN(raw) || raw <= 0 || !pps || !val) {{
      box.style.display = 'none';
      return;
    }}
    box.style.display = 'block';
    document.getElementById('valtext').innerHTML =
      '&asymp; $' + (val * raw / pps).toFixed(1) + 'Bn valuation';
  }}
  function fieldVal(name) {{
    var el = document.querySelector('[name="' + name + '"]');
    return el ? el.value.trim() : '';
  }}
  function doSign() {{
    if (!validateReq()) return false;
    var btn = document.getElementById('signbtn');
    btn.disabled = true;
    var body = new URLSearchParams({{
      key: new URLSearchParams(location.search).get('key') || '',
      t: new URLSearchParams(location.search).get('t') || '',
      deal_id: new URLSearchParams(location.search).get('deal_id') || '',
      role: new URLSearchParams(location.search).get('role') || '',
      side: btn.getAttribute('data-side') || '',
      gross: fieldVal('gross'),
      size: fieldVal('size'),
      expdays: fieldVal('expdays'),
      mgmt_fee: fieldVal('mgmt_fee'),
      carry: fieldVal('carry'),
      seller_fee: fieldVal('seller_fee'),
      letter_text: (document.querySelector('.letter') || {{}}).innerText || '',
      reline: (document.querySelector('.reline') || {{}}).innerText || '',
      signer_name: (document.querySelector('.sig .name') || {{}}).innerText || '',
      sig_meta: (document.querySelector('.sig .meta') || {{}}).innerText || '',
    }});
    fetch(location.href, {{method: 'POST', body: body,
      headers: {{'Content-Type': 'application/x-www-form-urlencoded'}}}})
      .then(function(r) {{
        var needIqf = btn.getAttribute('data-iqf') === '0';
        var dest = needIqf ? btn.getAttribute('data-iqfurl') : '{REDIRECT_URL}';
        if (needIqf) {{
          document.getElementById('tymsg').innerHTML =
            'Your letter of intent has been received.<br>One more step — please complete ' +
            'your investor qualification form so we can proceed.';
          var lk = document.getElementById('tylink');
          lk.href = dest;
          lk.textContent = 'Continue to the form →';
        }}
        document.getElementById('thankyou').classList.add('open');
        setTimeout(function() {{ location.href = dest; }}, 4000);
      }})
      .catch(function() {{ btn.disabled = false; alert('Submission failed — please try again.'); }});
    return false;
  }}
  syncTerms();
  </script>
</body></html>'''

def _resp(code, body, ctype):
    return {"statusCode": code, "headers": {"Content-Type": ctype}, "body": body}

def _json(code, obj):
    return {"statusCode": code, "headers": {"Content-Type": "application/json"},
            "body": json.dumps(obj)}

def send_email(subject, text_body):
    ses = boto3.client("ses", region_name="us-east-1")
    ses.send_email(
        Source=SES_SENDER,
        Destination={"ToAddresses": [CHAD_EMAIL]},
        Message={
            "Subject": {"Data": subject},
            "Body": {"Text": {"Data": text_body}},
        },
    )

def make_token(deal_id) -> str:
    sig = hmac.new(LOI_TOKEN_SECRET.encode(), str(deal_id).encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(sig).decode().rstrip("=")


def verify_token(deal_id, token: str) -> bool:
    if not token or not LOI_TOKEN_SECRET:
        return False
    return hmac.compare_digest(make_token(deal_id), token)


def send_email_to(to_address, subject, text_body):
    """Send to a recipient, with replies going to Chad rather than the agent mailbox."""
    ses = boto3.client("ses", region_name="us-east-1")
    ses.send_email(
        Source=SES_SENDER,
        Destination={"ToAddresses": [to_address]},
        ReplyToAddresses=[CHAD_EMAIL],
        Message={
            "Subject": {"Data": subject},
            "Body": {"Text": {"Data": text_body}},
        },
    )


def _send_confirm_page(deal_id, key, name, to_addr, security):
    action = f"/?send=1&deal_id={escape(str(deal_id))}&key={escape(key)}"
    sec = f" regarding {escape(security)}" if security else ""
    return (
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<meta name="robots" content="noindex,nofollow">'
        '<title>Send LOI link</title></head>'
        "<body style=\"font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;"
        'background:#f4f5f7;margin:0;padding:40px;">'
        '<div style="max-width:460px;margin:0 auto;background:#fff;border:1px solid #e3e5e8;'
        'border-radius:10px;padding:30px 32px;">'
        '<h2 style="margin:0 0 10px;font-size:19px;color:#0f1e35;">Send an LOI link?</h2>'
        f'<p style="margin:0 0 4px;font-size:15px;color:#1a1a1a;">To {escape(name)} '
        f'&lt;{escape(to_addr)}&gt;{sec}.</p>'
        '<p style="margin:0 0 20px;font-size:13px;color:#8a929d;">Nothing is sent until you '
        'press the button.</p>'
        f'<form method="POST" action="{action}">'
        '<button type="submit" style="background:#1652f0;color:#fff;padding:12px 26px;border:none;'
        'border-radius:7px;font-size:15px;font-weight:600;cursor:pointer;">Send the LOI link</button>'
        '</form></div></body></html>'
    )


def handle_send(event, params, method):
    """A GET only renders a confirmation page — nothing is sent. The email goes out only
    on the POST from the Send button, so link-scanners and prefetchers (which GET and
    never submit forms) cannot trigger a send. Mirrors deal-nudge."""
    deal_id = params.get("deal_id", "")
    if not deal_id:
        return _resp(400, "missing deal_id", "text/plain")
    jwt = get_jwt()
    dr = call_pipeline_api("GET", f"/deals/{deal_id}.json", jwt)
    if dr["status"] != 200:
        return _resp(404, f"deal not found ({deal_id})", "text/plain")
    deal = dr["data"]
    contact = deal.get("primary_contact") or {}
    pid = contact.get("id")
    person = {}
    if pid:
        pr = call_pipeline_api("GET", f"/people/{pid}.json", jwt)
        if pr["status"] == 200:
            person = pr["data"]
    to_addr = (person.get("email") or contact.get("email") or "").strip()
    if not to_addr:
        return _resp(200, f"No email on file for the contact on deal {deal_id}. Nothing sent.", "text/plain")
    first    = (person.get("first_name") or contact.get("first_name") or "").strip()
    name     = (person.get("full_name") or contact.get("full_name") or first or "the contact").strip()
    security = (deal.get("company") or {}).get("name") or ""

    # Safe GET: render the confirmation page only. Nothing is sent until the POST.
    if method != "POST":
        return _resp(200, _send_confirm_page(deal_id, params.get("key", ""), name, to_addr, security), "text/html")

    domain   = (event.get("requestContext") or {}).get("domainName") or ""
    link     = f"https://{domain}/?deal_id={deal_id}&t={make_token(deal_id)}"
    body = (f"{first} -\n\n"
            f"Thank you for your indication. The counterparty is collecting Letters of "
            f"Intent, and you can submit one here:\n\n"
            f"{link}\n")
    subject = f"Letter of Intent — {security}" if security else "Letter of Intent"
    try:
        send_email_to(to_addr, subject, body)
    except Exception as e:
        return _resp(500, f"send failed: {e}", "text/plain")
    return _resp(200,
                 f"<div style=\"font-family:-apple-system,sans-serif;padding:28px;\">"
                 f"<h3 style=\"margin:0 0 6px;\">LOI link sent</h3>"
                 f"<p style=\"margin:0;color:#5b6472;\">To {escape(first)} "
                 f"&lt;{escape(to_addr)}&gt; — {escape(security)}</p></div>",
                 "text/html")


def _plain(s):
    """Times is a latin-1 core font, so fold the typographic characters down."""
    if s is None:
        return ""
    s = str(s)
    for a, b in (("—", "-"), ("–", "-"), ("‘", "'"), ("’", "'"),
                 ("“", '"'), ("”", '"'), ("≈", "~"), (" ", " "),
                 ("…", "..."), ("→", "->")):
        s = s.replace(a, b)
    return s.encode("latin-1", "replace").decode("latin-1")


def build_loi_pdf(reline, side_label, letter_text, sig_meta, signer_name, audit_line):
    pdf = FPDF(format="Letter", unit="mm")
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.set_margins(25, 22, 25)
    pdf.add_page()

    sig_font = None
    try:
        pdf.add_font("sig", "", SIG_FONT_PATH)
        sig_font = "sig"
    except Exception:
        sig_font = None   # fall back to Times italic rather than fail the whole PDF

    pdf.set_font("Times", "B", 13)
    pdf.cell(0, 7, "LETTER OF INTENT", align="C", new_x="LMARGIN", new_y="NEXT")
    if side_label:
        pdf.set_font("Times", "", 10)
        pdf.cell(0, 5, _plain(side_label), align="C", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(7)

    if reline:
        pdf.set_font("Times", "B", 11)
        pdf.cell(0, 6, _plain(reline), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(3)

    pdf.set_font("Times", "", 11)
    for para in [p.strip() for p in (letter_text or "").split("\n") if p.strip()]:
        pdf.multi_cell(0, 5.6, _plain(para))
        pdf.ln(2.5)

    pdf.ln(9)
    if sig_font:
        pdf.set_font(sig_font, "", 26)
        pdf.cell(0, 11, signer_name or "", new_x="LMARGIN", new_y="NEXT")
    else:
        pdf.set_font("Times", "I", 17)
        pdf.cell(0, 11, _plain(signer_name or ""), new_x="LMARGIN", new_y="NEXT")
    y = pdf.get_y()
    pdf.line(25, y, 100, y)
    pdf.ln(2)

    pdf.set_font("Times", "", 10)
    for line in [l.strip() for l in (sig_meta or "").split("\n") if l.strip()]:
        pdf.cell(0, 5, _plain(line), new_x="LMARGIN", new_y="NEXT")

    pdf.ln(7)
    pdf.set_font("Times", "", 8)
    pdf.set_text_color(120, 120, 120)
    pdf.multi_cell(0, 4, _plain(audit_line))
    return bytes(pdf.output())


def send_email_with_pdf(subject, text_body, pdf_bytes, filename):
    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"]    = SES_SENDER
    msg["To"]      = CHAD_EMAIL
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    part = MIMEApplication(pdf_bytes, _subtype="pdf")
    part.add_header("Content-Disposition", "attachment", filename=filename)
    msg.attach(part)
    ses = boto3.client("ses", region_name="us-east-1")
    ses.send_raw_email(Source=SES_SENDER, Destinations=[CHAD_EMAIL],
                       RawMessage={"Data": msg.as_string()})


def handle_sign(event):
    import urllib.parse
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        import base64
        raw = base64.b64decode(raw).decode()
    fields = urllib.parse.parse_qs(raw, keep_blank_values=True)
    def fv(k):
        return (fields.get(k) or [""])[0].strip()
    deal_id  = fv("deal_id")
    role     = fv("role")
    side     = fv("side")
    gross    = fv("gross")
    size     = fv("size")
    expdays  = fv("expdays")
    mgmt_fee = fv("mgmt_fee")
    carry    = fv("carry")
    sfee     = fv("seller_fee")
    lines = [
        f"LOI signed — {side}",
        f"Deal ID : {deal_id}",
        f"Role    : {role or side}",
        f"Price   : ${gross}/sh" if gross else "Price   : (not specified)",
        f"Size    : ${size}" if size else "Size    : (not specified)",
        f"Expiry  : {expdays} days" if expdays else "Expiry  : open",
    ]
    if mgmt_fee or carry or sfee:
        lines += [
            f"Mgmt fee: {mgmt_fee}%",
            f"Carry   : {carry}%",
            f"One-time: {sfee}%",
        ]
    letter_text = fv("letter_text")
    reline      = fv("reline")
    signer_name = fv("signer_name")
    sig_meta    = fv("sig_meta")
    security    = reline[3:].strip() if reline.lower().startswith("re:") else reline

    ip = ((event.get("requestContext") or {}).get("http") or {}).get("sourceIp", "unknown")
    ts = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    lines += ["", f"Signed  : {ts}", f"IP      : {ip}"]

    audit   = (f"Executed electronically on {ts} from IP {ip}. "
               f"Deal reference {deal_id}.")
    subject = f"LOI signed — {side} — deal {deal_id}"
    try:
        pdf_bytes = build_loi_pdf(reline, side, letter_text, sig_meta, signer_name, audit)
        fname = _plain(f"LOI - {security or deal_id}.pdf")
        send_email_with_pdf(subject, "\n".join(lines), pdf_bytes, fname)
    except Exception as e:
        # Never lose a submission because the PDF failed — fall back to text only.
        send_email(subject + " (PDF failed)", "\n".join(lines) + f"\n\nPDF ERROR: {e}")
    return _json(200, {"ok": True})

def _authed(params) -> bool:
    """A tokenized link is scoped to one deal; the page key is admin-only preview."""
    deal_id = params.get("deal_id", "")
    if deal_id and verify_token(deal_id, params.get("t", "")):
        return True
    return bool(LOI_PAGE_KEY) and params.get("key", "") == LOI_PAGE_KEY


def lambda_handler(event, context):
    params = (event.get("queryStringParameters") or {})
    if params.get("send"):
        if not LOI_PAGE_KEY or params.get("key", "") != LOI_PAGE_KEY:
            return _resp(403, "forbidden", "text/plain")
        _m = (event.get("requestContext", {}).get("http", {}).get("method", "")
              or event.get("httpMethod", "")).upper()
        return handle_send(event, params, _m)
    if not _authed(params):
        return _resp(403, "forbidden", "text/plain")
    if event.get("requestContext", {}).get("http", {}).get("method", "").upper() == "POST" or \
       event.get("httpMethod", "").upper() == "POST":
        return handle_sign(event)
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
    company = {}
    comp_id = (deal.get("company") or {}).get("id")
    if comp_id:
        cr = call_pipeline_api("GET", f"/companies/{comp_id}.json", jwt)
        if cr["status"] == 200:
            company = cr["data"]
    role = params.get("role", "").strip().lower()
    if role not in ("buyer", "seller"):
        role = ""   # empty = follow the deal's own side
    return _resp(200, render_loi(deal, person, role, company), "text/html")
