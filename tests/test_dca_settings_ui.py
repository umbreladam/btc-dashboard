"""Strike DCA settings: frequency-first layout, conditional fields, clear status text."""
import json, os, re, shutil, subprocess
import pytest
from app import main

STATIC = os.path.join(os.path.dirname(main.__file__), "static")
html = open(os.path.join(STATIC, "index.html")).read()
js = open(os.path.join(STATIC, "app.js")).read()
dca = html[html.index("Strike DCA"):html.index('id="btn-strike-test"')]


def test_order_amount_currency_then_frequency_then_schedule():
    pos = [dca.index(s) for s in ('name="strike_daily_gbp"', 'name="strike_dca_currency"', 'id="dca-frequency"',
                                  'name="strike_weekday"', 'name="strike_monthday"', 'name="strike_job_time"', 'name="strike_check_delay_min"')]
    assert pos == sorted(pos)
    assert re.search(r'<option value="off">Off</option>', dca) and "Buy time (UK)<input" in dca
    assert 'type="checkbox" value="true"> Enable scheduled' not in dca


def test_schedule_fields_hidden_until_settings_load():
    for name, freqs in (("strike_weekday", "weekly"), ("strike_monthday", "monthly"),
                        ("strike_job_time", "daily weekly monthly"), ("strike_check_delay_min", "daily weekly monthly")):
        label = re.search(r'<label([^>]*)>[^<]*<(?:input|select) name="%s"' % name, dca).group(1)
        assert f'data-freq="{freqs}"' in label and 'class="hidden"' in label, name


@pytest.mark.skipif(not shutil.which("node"), reason="node not installed")
def test_status_and_explanation_text():
    src = js[js.index("const WEEKDAYS"):js.index("function applyFreq")]
    prog = ("const ukTime = t => 'T(' + t + ')';\n" + src + """
const f = {strike_job_time: {value: "15:25"}, strike_check_delay_min: {value: "10"}, strike_weekday: {value: "2"}, strike_monthday: {value: "22"}, strike_daily_gbp: {value: "20"}, strike_dca_currency: {value: "GBP"}};
console.log(JSON.stringify([
  dcaStatus("daily", {strike_key_set: false, strike_job_enabled: false, strike_frequency: "daily"}),
  dcaStatus("off", {strike_key_set: true}),
  dcaStatus("weekly", {strike_key_set: true, strike_job_enabled: true, strike_frequency: "daily"}),
  dcaStatus("daily", {strike_key_set: true, strike_job_enabled: true, strike_frequency: "daily", strike_next_run: "X"}),
  dcaExplain("daily", f), dcaExplain("weekly", f), dcaExplain("monthly", f), dcaExplain("off", f)]));""")
    s = json.loads(subprocess.check_output(["node", "-e", prog], text=True))
    assert s[0].startswith("Add your Strike API key")
    assert "is off" in s[1] and "Save" in s[2] and s[3] == "Next balance check: T(X)"
    assert s[4].startswith("Every day at 15:25 (UK time") and "10 minutes later" in s[4] and "£20 buy" in s[4]
    assert "Every Wednesday at 15:25" in s[5] and s[6].startswith("On the 22nd of each month") and "shorter months" in s[6] and "shorter" not in s[4]
    assert "won't check Strike automatically" in s[7]
