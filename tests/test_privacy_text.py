"""Synthetic data only. Privacy mode also masks numbers inside free text (notes, descriptions, messages) - display only (1.0.11)."""
import json, os, re, shutil, subprocess
import pytest
from app import main

STATIC = os.path.join(os.path.dirname(main.__file__), "static")
JS = open(os.path.join(STATIC, "app.js")).read()
HTML = open(os.path.join(STATIC, "index.html")).read()
CSS = open(os.path.join(STATIC, "style.css")).read()

CASES = [
    ("Converted 1.23456789 CGLD to 0.00001234 BTC [buy]", "Converted **** CGLD to **** BTC [buy]"),
    ("Bought 0.00123456 BTC for 25 GBP using Visa debit ********1234", "Bought **** BTC for **** GBP using Visa debit ************"),
    ("Bought 0.01111111 BTC for 12.34 GBP using EUR Wallet", "Bought **** BTC for **** GBP using EUR Wallet"),
    ("£1,234.50, $99, €5, 20GBP, 100sats, 1.5k, 0.5ETH", "£****, $****, €****, ****GBP, ****sats, ****k, ****ETH"),
    ("2.5e-07 BTC for 0.001234567 GBP on BTC-GBP", "**** BTC for **** GBP on BTC-GBP"),
    # dates, times, percentages, "line N" and hex identifiers stay readable
    ("on 2023-11-05 at 14:32, fee 1.5% id 5abc0def1234567890abcdef", "on 2023-11-05 at 14:32, fee 1.5% id 5abc0def1234567890abcdef"),
    ("2023-11-05T10:00:00Z withdrawal 05/11/2023", "2023-11-05T10:00:00Z withdrawal 05/11/2023"),
    ("line 12: could not convert string to float: '0.123'", "line 12: could not convert string to float: '****'"),
    ("Disposal on 2024-01-01 exceeds holdings by 0.0001 BTC", "Disposal on 2024-01-01 exceeds holdings by **** BTC"),
    ("An increase is recorded as a £20 buy.", "An increase is recorded as a £**** buy."),
    # Lightning invoices encode the amount in the prefix; invoices without an amount are left alone
    ("paid lnbc1234560n1pexample and lnbc2000u1pexmpl to lnbc4...xyzab", "paid lnbc****n1pexample and lnbc****u1pexmpl to lnbc****...xyzab"),
    ("invoice lnbc1pexampleqqq", "invoice lnbc1pexampleqqq"),
]


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_mask_text_hides_every_amount_but_keeps_words_dates_percentages():
    fmt = JS[JS.index("const MASK"):JS.index("function setPrivacy")]
    prog = ("const document={documentElement:{classList:{contains:()=>false}}};const esc=t=>String(t).replace(/</g,'&lt;');\n"
            + fmt.replace("let PRIV", "var PRIV") + "\nconst C=" + json.dumps([c[0] for c in CASES]) +
            ";\nconst off=C.map(maskText); PRIV=true; const on=C.map(maskText);"
            "\nconsole.log(JSON.stringify([off, on, mesc('<b>1 BTC</b>'), maskText(null), maskText(42)]));")
    off, on, escaped, none, num = json.loads(subprocess.check_output(["node", "-e", prog], text=True))
    assert off == [c[0] for c in CASES]          # privacy off: text untouched
    assert on == [c[1] for c in CASES]
    assert escaped == "&lt;b>**** BTC&lt;/b>" and none == "" and num == "****"


def test_free_text_is_rendered_through_mask():
    assert "${mesc(t.note)}" in JS and "t.note || \"\").replace" not in JS
    assert "${mesc(r.description)}" in JS and "${mesc(w.description)}" in JS       # wallet panel + settings list
    assert "p.errors.slice(0, 30).map(mesc)" in JS                                  # import preview issues
    assert "${mesc(l.msg)}" in JS                                                   # job log (Strike / scan balances)
    assert "maskText(t.error)" in JS and "maskText(r.message || r.error)" in JS
    assert "maskText(dcaExplain(freq, f))" in JS
    # no other raw interpolation of free-text fields (the wallet edit box keeps the real value, blurred via .pmask)
    for field in ("note", "description", "msg", "message", "error"):
        for m in re.finditer(r"\$\{([^}]*\.%s\b[^}]*)\}" % field, JS):
            if m.group(1) == 'esc(w.description || "")':
                continue
            assert re.search(r"\b(mesc|maskText)\(", m.group(1)), m.group(0)
    assert not re.search(r"alert\((e|err)\.message\)", JS)


def test_toggle_rerenders_preview_settings_text_and_log():
    tog = JS[JS.index("// ---------- privacy toggle"):]
    assert "renderPreview()" in tog and "applyFreq()" in tog and "loadLog()" in tog


def test_amount_inputs_blurred_in_privacy_mode_without_changing_values():
    for n in ("other_income", "exempt_used", "strike_daily_gbp"):
        assert re.search(r'<input name="%s" class="pmask"' % n, HTML), n
    assert "html.priv .pmask:not(:focus){filter:blur(5px)}" in CSS
    assert 'data-f="description" class="pmask"' in JS
