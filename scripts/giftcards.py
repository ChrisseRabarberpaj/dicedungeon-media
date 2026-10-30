#!/usr/bin/env python3
"""Dice Dungeon gift cards: Stripe payment -> Amelia coupon -> card PDF/PNG -> email.

  python scripts/giftcards.py check          # how many paid gift cards are not handled yet (read-only)
  python scripts/giftcards.py run            # handle them
  python scripts/giftcards.py demo --out DIR # render sample cards locally, touches nothing
  python scripts/giftcards.py test --product gm3 --delivery post --to me@x   # full email to one address,
                                                 # dummy code, no coupon, no Stripe

Secrets (env): STRIPE_API_KEY (read only), WP_APP_PASSWORD, GMAIL_APP_PASSWORD.
Nothing personal is written to this public repo; the sales log lives in WordPress (option dd_gift_sales)
and in the copies emailed to talecraftmanagement.
"""
import argparse, base64, datetime as dt, hashlib, hmac, html, json, os, smtplib, ssl, sys, tempfile
import urllib.parse, urllib.request
from email.message import EmailMessage
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GC = ROOT / "giftcards"
ASSETS = GC / "assets"
PRODUCTS = json.loads((GC / "products.json").read_text())
LINKS = json.loads((GC / "links.json").read_text()) if (GC / "links.json").exists() else {}

WP = "https://dicedungeon.fi/wp-json/dd/v1"
WP_USER = "Talecraftgames"
GMAIL_USER = "talecraftmanagement@gmail.com"
FROM = "Dice Dungeon <booking@talecraftgames.com>"
REPLY_TO = "booking@talecraftgames.com"
OWNER = "talecraftmanagement@gmail.com"
VALID_DAYS = 365
LOOKBACK_DAYS = 30
ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"


def secret(name):
    v = os.environ.get(name, "")
    v = "".join(v.split())          # app passwords get pasted with spaces/newlines
    if not v:
        sys.exit(f"missing secret {name}")
    return v


# ---------------------------------------------------------------- HTTP helpers
def http(method, url, data=None, headers=None):
    body = None
    headers = dict(headers or {})
    if data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        txt = e.read().decode(errors="replace")
        try:
            return e.code, json.loads(txt)
        except ValueError:
            return e.code, txt


def wp(method, path, data=None, params=None):
    auth = base64.b64encode(f"{WP_USER}:{secret('WP_APP_PASSWORD')}".encode()).decode()
    url = WP + path + ("?" + urllib.parse.urlencode(params) if params else "")
    return http(method, url, data, {"Authorization": "Basic " + auth, "User-Agent": "dicedungeon-giftcards"})


def stripe_sessions(link_id):
    key = secret("STRIPE_API_KEY")
    since = int((dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=LOOKBACK_DAYS)).timestamp())
    out, after = [], None
    while True:
        q = {"payment_link": link_id, "status": "complete", "limit": 100, "created[gte]": since}
        if after:
            q["starting_after"] = after
        code, res = http("GET", "https://api.stripe.com/v1/checkout/sessions?" + urllib.parse.urlencode(q),
                         headers={"Authorization": "Bearer " + key})
        if code != 200:
            sys.exit(f"Stripe error {code}: {res}")
        out += res["data"]
        if not res.get("has_more"):
            return out
        after = res["data"][-1]["id"]


# ---------------------------------------------------------------- sale model
def code_for(session_id):
    """Same Stripe payment always gives the same code, so a retry never makes a second coupon."""
    d = hmac.new(secret("WP_APP_PASSWORD").encode(), session_id.encode(), hashlib.sha256).digest()
    n = int.from_bytes(d[:8], "big")
    s = ""
    for _ in range(6):
        n, r = divmod(n, len(ALPHABET))
        s += ALPHABET[r]
    return s


FIELD_LABELS = {"kenelle": ("kenelle", "recipient", "to"), "viesti": ("tervehdys", "message", "greeting"),
                "kenelta": ("kenelt", "from")}


def field(sess, key):
    """Stripe dashboard picks the field keys itself, so match on key or on the label's start."""
    want = FIELD_LABELS[key]
    for f in sess.get("custom_fields") or []:
        k = (f.get("key") or "").lower()
        label = ((f.get("label") or {}).get("custom") or "").lower()
        if k.startswith(want) or label.startswith(want):  # want is a tuple of prefixes
            return ((f.get("text") or {}).get("value") or "").strip()
    return ""


def shipping_of(sess):
    sd = sess.get("shipping_details") or (sess.get("collected_information") or {}).get("shipping_details")
    if not sd:
        return ""
    a = sd.get("address") or {}
    lines = [sd.get("name") or "", a.get("line1") or "", a.get("line2") or "",
             f"{a.get('postal_code') or ''} {a.get('city') or ''}".strip(), a.get("state") or "", a.get("country") or ""]
    return "\n".join(l for l in lines if l)


def sale_from_session(sess, link):
    cd = sess.get("customer_details") or {}
    paid = dt.datetime.fromtimestamp(sess["created"], dt.timezone.utc)
    return {
        "session": sess["id"],
        "code": ("LAHJA-" if (sess.get("client_reference_id") or link.get("lang")) == "fi" else "GIFT-") + code_for(sess["id"]),
        "product": link["product"],
        "delivery": link["delivery"],
        "amount": f"{(sess.get('amount_total') or 0) / 100:.2f} {str(sess.get('currency', 'eur')).upper()}",
        "buyer_name": cd.get("name") or "",
        "buyer_email": cd.get("email") or "",
        "recipient": field(sess, "kenelle"),
        "message": field(sess, "viesti"),
        "sender": field(sess, "kenelta"),
        "shipping": shipping_of(sess),
        "paid_at": paid.isoformat(),
        "expires": (paid + dt.timedelta(days=VALID_DAYS)).date().isoformat(),
        # the Finnish site can reuse the same links with ?client_reference_id=fi
        "lang": sess.get("client_reference_id") if sess.get("client_reference_id") in ("fi", "en") else link.get("lang", "en"),
    }


# ---------------------------------------------------------------- card rendering
FONTS = """<style>@font-face{font-family:'Almendra SC';src:url(AlmendraSC-Regular.ttf)}
@font-face{font-family:'Alegreya';src:url(Alegreya.ttf);font-weight:100 900}
html,body{margin:0;padding:0;background:#0A0808}@page{size:91mm 61.1mm;margin:0}
.card{width:344px;height:231px;position:relative;overflow:hidden;box-sizing:border-box;background:#0A0808;
font-family:'Alegreya',Georgia,serif;color:#ECE8E2;page-break-after:always}</style>"""


T = {
    "fi": {"label": "LAHJAKORTTI", "for_you": "SINULLE", "code": "Koodi", "valid": "Voimassa {d} asti",
           "or_mail": "tai lähetä koodi: booking@talecraftgames.com",
           "how": "Varaa: dicedungeon.fi/fi/booking → <i>{s}</i> → syötä koodi maksuvaiheessa.",
           "gm": "Varaa vähintään 5 päivää etukäteen, vahvistamme ajan."},
    "en": {"label": "GIFT CARD", "for_you": "FOR YOU", "code": "Code", "valid": "Valid until {d}",
           "or_mail": "or email the code to booking@talecraftgames.com",
           "how": "Book at dicedungeon.fi/booking → pick <i>{s}</i> → enter the code at checkout.",
           "gm": "Book at least 5 days ahead, we confirm the date."},
}


def fmt_date(iso, lang="fi"):
    y, m, d = (int(x) for x in iso.split("-"))
    if lang == "en":
        return f"{d} {['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'][m-1]} {y}"
    return f"{d}.{m}.{y}"


def front_html(p, lang):
    t, c = T[lang], p[lang]
    return f"""<div class="card" style="padding:22px">
<img src="{p['image']}" style="position:absolute;left:0;top:0;width:344px;height:231px;object-fit:cover;object-position:{p['pos']}">
<div style="position:absolute;inset:0;background:linear-gradient(180deg,rgba(10,8,8,.55) 0%,rgba(10,8,8,.15) 38%,rgba(10,8,8,.6) 62%,rgba(10,8,8,.92) 100%)"></div>
<div style="position:relative;width:100%;height:100%;box-sizing:border-box;border:1.5px solid #F5CB5C;border-radius:4px;padding:14px 16px;display:flex;flex-direction:column;justify-content:space-between">
<div style="display:flex;justify-content:space-between;align-items:flex-start;gap:12px"><div style="font-size:12px;letter-spacing:3px;font-weight:700;color:#F5CB5C">{t['label']}</div><img src="logo2.svg" style="width:74px;height:auto"></div>
<div style="display:flex;flex-direction:column;gap:4px"><div style="font-family:'Almendra SC',Georgia,serif;font-size:34px;line-height:1;color:#fff">{c['head']}</div><div style="font-size:13px">{c['sub']}</div></div>
</div></div>"""


def back_html(p, sale, lang):
    e, t = html.escape, T[lang]
    to = sale.get("recipient") or ""
    head = (("FOR " if lang == "en" else "") + e(to.upper())) if to else t["for_you"]
    msg, frm = sale.get("message") or "", sale.get("sender") or ""
    greet = ""
    if msg or frm:
        greet = f"""<div style="font-size:12.5px;line-height:1.3;font-style:italic;padding-right:80px">{e(msg)}{' – ' + e(frm) if frm else ''}</div>"""
    how = t["how"].format(s=e(p[lang].get("service", p["service"])))
    if p["gm"]:
        how += f"<br><span style=\"color:#B9B2A8\">{t['gm']}</span>"
    return f"""<div class="card" style="padding:18px 22px;display:flex;flex-direction:column;gap:5px">
<div style="font-size:11px;letter-spacing:3px;font-weight:700;color:#F5CB5C;padding-right:80px">{head}</div>
{greet}
<div style="font-size:11px;line-height:1.32;padding-right:80px;margin-top:2px">{how}</div>
<div style="display:flex;align-items:baseline;gap:10px;margin-top:auto"><span style="font-size:12px">{t['code']}</span><span style="font-size:21px;font-weight:700;letter-spacing:2.5px;color:#F5CB5C">{e(sale['code'])}</span></div>
<div style="font-size:9.5px;color:#B9B2A8">{t['valid'].format(d=fmt_date(sale['expires'], lang))} · {t['or_mail']}</div>
<img src="{'qr_booking_fi.png' if lang == 'fi' else 'qr_booking.png'}" style="position:absolute;right:22px;top:18px;width:64px;height:64px;background:#fff;padding:3px;border-radius:3px">
</div>"""


def render(sale, outdir):
    """Writes <code>-print.pdf (front + back, card size) and <code>-WhatsApp.png (front over back)."""
    from playwright.sync_api import sync_playwright
    p, lang = PRODUCTS[sale["product"]], sale.get("lang", "en")
    outdir = Path(outdir)
    pdf, png = outdir / f"{sale['code']}-print.pdf", outdir / f"{sale['code']}-WhatsApp.png"
    page_html = f"<!doctype html><html><head><meta charset='utf-8'>{FONTS}</head><body>{front_html(p, lang)}{back_html(p, sale, lang)}</body></html>"
    stack_html = (f"<!doctype html><html><head><meta charset='utf-8'>{FONTS}</head><body style='padding:12px;background:#1a1414'>"
                  f"<div style='display:flex;flex-direction:column;gap:12px'>{front_html(p, lang)}{back_html(p, sale, lang)}</div></body></html>")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        for f in ASSETS.iterdir():
            (tmp / f.name).symlink_to(f)
        (tmp / "card.html").write_text(page_html)
        (tmp / "stack.html").write_text(stack_html)
        with sync_playwright() as pw:
            b = pw.chromium.launch()
            pg = b.new_page(viewport={"width": 368, "height": 486}, device_scale_factor=3)
            pg.goto((tmp / "stack.html").as_uri()); pg.wait_for_timeout(500)
            pg.screenshot(path=str(png), full_page=True)
            pg.goto((tmp / "card.html").as_uri()); pg.wait_for_timeout(500)
            pg.pdf(path=str(pdf), width="91mm", height="61.1mm", print_background=True,
                   margin={"top": "0", "right": "0", "bottom": "0", "left": "0"})
            b.close()
    return pdf, png


# ---------------------------------------------------------------- email
def buyer_email(sale):
    p, lang = PRODUCTS[sale["product"]], sale.get("lang", "en")
    first = (sale["buyer_name"].split() or [""])[0]
    exp = fmt_date(sale["expires"], lang)
    post = sale["delivery"] == "post"
    if lang == "fi":
        gm = ("\nSeikkailuissa pyyntö kannattaa tehdä vähintään 5 päivää etukäteen. Varmistamme pelinjohtajan ja "
              "vahvistamme ajan sähköpostilla.\n") if p["gm"] else ""
        deliv = ("\nPostitamme kortin 1-2 arkipäivän sisällä osoitteeseen:\n" + sale["shipping"] +
                 "\n\nLiitteenä on myös digiversio, jos haluat antaa lahjan jo ennen kuin posti tuo sen.\n") if post else \
                "\nKortti on liitteenä PDF:nä tulostettavaksi ja kuvana, jonka voi lähettää esim. WhatsAppissa.\n"
        body = f"""Hei {first}!

Kiitos ostosta! Tässä lahjakortti: {p['fi']['long']}.
{deliv}
Koodi: {sale['code']}
Voimassa: {exp} asti

Näin lahjan saaja käyttää sen:
1. Varaa aika osoitteessa https://dicedungeon.fi/fi/booking/
2. Valitse "{p['fi'].get('service', p['service'])}"
3. Syötä koodi maksuvaiheen alennuskenttään, hinta menee nollaan.
{gm}
Jos jokin mietityttää, vastaa tähän viestiin.

Christoffer
Dice Dungeon, Caloniuksenkatu 9 D 65, Töölö
"""
        return "Dice Dungeon -lahjakortti: " + p["fi"]["head"], body
    gm = ("\nFor adventures, send the booking request at least 5 days ahead. We line up the Game Master and "
          "confirm the date by email.\n") if p["gm"] else ""
    deliv = ("\nWe'll post the card within 1-2 working days to:\n" + sale["shipping"] +
             "\n\nThe digital version is attached too, in case you want to give it before the post arrives.\n") if post else \
            "\nThe card is attached twice: a PDF to print, and an image that's easy to send on WhatsApp.\n"
    body = f"""Hi {first}!

Thanks for your order! Here's your gift card: {p['en']['long']}.
{deliv}
Code: {sale['code']}
Valid until: {exp}

How the recipient uses it:
1. Book at https://dicedungeon.fi/booking/
2. Pick "{p['service']}"
3. Enter the code in the coupon field at checkout. The price goes to zero.
{gm}
Questions? Just reply to this email.

Christoffer
Dice Dungeon, Caloniuksenkatu 9 D 65, Helsinki
"""
    return "Your Dice Dungeon gift card: " + p["en"]["head"], body


def owner_email(sale):
    p = PRODUCTS[sale["product"]]
    post = sale["delivery"] == "post"
    lines = [
        ("POSTITETTAVA, tulosta liite ja postita:\n\n" + sale["shipping"] + "\n") if post else "Digitaalinen, lähti ostajalle automaattisesti.\n",
        f"Tuote: {p['fi']['head']} ({p['service']})",
        f"Koodi: {sale['code']} (Amelia-kuponki luotu, 100 %, 1 käyttö)",
        f"Voimassa: {fmt_date(sale['expires'])}",
        f"Maksettu: {sale['amount']}",
        f"Ostaja: {sale['buyer_name']} <{sale['buyer_email']}>",
        f"Kenelle: {sale['recipient'] or '-'}",
        f"Tervehdys: {sale['message'] or '-'}",
        f"Keneltä: {sale['sender'] or '-'}",
        f"Stripe: {sale['session']}",
    ]
    subj = ("POSTITA: " if post else "") + f"Lahjakortti myyty: {p['fi']['head']} ({sale['code']})"
    return subj, "\n".join(lines) + "\n"


def send(to, subj, body, attachments, reply_to=REPLY_TO):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"], m["Reply-To"] = FROM, to, subj, reply_to
    m.set_content(body)
    for path in attachments:
        path = Path(path)
        sub = "pdf" if path.suffix == ".pdf" else "png"
        mt = "application" if sub == "pdf" else "image"
        m.add_attachment(path.read_bytes(), maintype=mt, subtype=sub, filename="Dice-Dungeon-gift-card-" + path.name.split("-", 2)[-1])
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as s:
        s.login(GMAIL_USER, secret("GMAIL_APP_PASSWORD"))
        s.send_message(m)


# ---------------------------------------------------------------- commands
def pending_sales():
    out = []
    for link_id, link in LINKS.items():
        for sess in stripe_sessions(link_id):
            if sess.get("payment_status") not in ("paid", "no_payment_required"):
                continue
            code, _ = wp("GET", "/gift-sale", params={"session": sess["id"]})
            if code == 404:
                out.append(sale_from_session(sess, link))
            elif code != 200:
                sys.exit(f"WordPress check failed ({code}) for {sess['id']}")
    return out


def cmd_check(_):
    if not LINKS:
        print("no payment links configured yet"); n = 0
    else:
        n = len(pending_sales())
        print(f"{n} gift card(s) waiting")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write(f"pending={n}\n")


def handle(sale, workdir):
    p = PRODUCTS[sale["product"]]
    code, res = wp("POST", "/gift-coupon", {"code": sale["code"], "service_id": p["service_id"], "expires": sale["expires"]})
    if code != 200:
        raise RuntimeError(f"coupon failed {code}: {res}")
    pdf, png = render(sale, workdir)
    if sale["buyer_email"]:
        send(sale["buyer_email"], *buyer_email(sale), [pdf, png])
    send(OWNER, *owner_email(sale), [pdf, png], reply_to=sale["buyer_email"] or REPLY_TO)
    rec = {k: v for k, v in sale.items() if k not in ("expires", "lang")}
    rec["handled_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
    code, res = wp("POST", "/gift-sale", rec)
    if code != 200:
        raise RuntimeError(f"emails sent but marking handled failed {code}: {res}")


def cmd_run(_):
    sales = pending_sales()
    failed = 0
    with tempfile.TemporaryDirectory() as work:
        for s in sales:
            try:
                handle(s, work)
                print(f"handled {s['product']} {s['delivery']} {s['code']}")
            except Exception as ex:          # keep going, fail the run at the end so GitHub emails an alert
                failed += 1
                print(f"FAILED {s['session']}: {ex}")
    print(f"{len(sales) - failed} handled, {failed} failed")
    sys.exit(1 if failed else 0)


def demo_sale(product, delivery="digital", lang="en"):
    today = dt.date.today()
    return {"session": "cs_test_demo", "code": ("LAHJA-" if lang == "fi" else "GIFT-") + "7K3QX2", "product": product, "delivery": delivery,
            "amount": f"{PRODUCTS[product]['price'] + (5 if delivery == 'post' else 0)}.00 EUR",
            "buyer_name": "Mikko Testinen", "buyer_email": "", "recipient": "Anna",
            "message": "Merry Christmas! Time to finally roll some dice.", "sender": "Mikko",
            "shipping": "Anna Testinen\nTestikatu 1 A 2\n00100 Helsinki\nFI" if delivery == "post" else "",
            "paid_at": dt.datetime.now(dt.timezone.utc).isoformat(),
            "expires": (today + dt.timedelta(days=VALID_DAYS)).isoformat(), "lang": lang}


def cmd_demo(a):
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    for k in PRODUCTS:
        s = demo_sale(k); s["code"] = "GIFT-" + k.upper().ljust(6, "X")[:6]
        if k == "room12":
            s["recipient"] = s["message"] = s["sender"] = ""
        pdf, png = render(s, out)
        print(pdf, png)


def cmd_test(a):
    s = demo_sale(a.product, a.delivery, a.lang); s["buyer_email"] = a.to
    if a.lang == "fi":
        s["message"] = "Hyvää joulua! Nyt heitetään noppaa."
    with tempfile.TemporaryDirectory() as work:
        pdf, png = render(s, work)
        send(a.to, *buyer_email(s), [pdf, png])
        subj, body = owner_email(s)
        send(a.to, "[TESTI] " + subj, body, [pdf, png])
    print("sent test to", a.to)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sp = ap.add_subparsers(dest="cmd", required=True)
    sp.add_parser("check").set_defaults(f=cmd_check)
    sp.add_parser("run").set_defaults(f=cmd_run)
    d = sp.add_parser("demo"); d.add_argument("--out", default="giftcard_demo"); d.set_defaults(f=cmd_demo)
    t = sp.add_parser("test"); t.add_argument("--product", default="room6"); t.add_argument("--delivery", default="digital")
    t.add_argument("--to", default=OWNER); t.add_argument("--lang", default="en", choices=["en", "fi"])
    t.set_defaults(f=cmd_test)
    a = ap.parse_args()
    a.f(a)
