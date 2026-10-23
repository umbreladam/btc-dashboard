"""Screenshot the locally running app: python screenshot.py [base_url]
Demo app (DEMO_MODE=1, starts at the wizard): python screenshot.py http://localhost:8766 --demo /workspace/screens"""
import sys, asyncio, os
from playwright.async_api import async_playwright

ARGS = [a for a in sys.argv[1:] if not a.startswith("--")]
BASE = ARGS[0] if ARGS else "http://localhost:8765"
DEMO_OUT = sys.argv[sys.argv.index("--demo") + 1] if "--demo" in sys.argv else None


async def demo(b):
    """wizard -> generate -> dashboard, transactions, wallets, settings (integrations disabled), privacy mode."""
    os.makedirs(DEMO_OUT, exist_ok=True)
    shot = lambda pg, n, **k: pg.screenshot(path=f"{DEMO_OUT}/demo-{n}.png", **k)
    pg = await b.new_page(viewport={"width": 1440, "height": 900})
    await pg.goto(f"{BASE}/#dash"); await pg.wait_for_timeout(1500)
    await pg.evaluate("""() => fetch('api/demo/reset', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Satoshi-Request': '1'}, body: '{}'})""")
    await pg.reload(); await pg.wait_for_timeout(1500)
    f = "#f-wizard"
    await pg.fill(f"{f} [name=start]", "2021-01-15"); await pg.fill(f"{f} [name=amount]", "50")
    await pg.select_option(f"{f} [name=frequency]", "weekly"); await pg.fill(f"{f} [name=salary]", "55000")
    await pg.fill(f"{f} [name=lump_sum]", "2000"); await pg.select_option(f"{f} [name=cold_every]", "quarterly")
    await pg.select_option(f"{f} [name=exchanges]", "3")
    await shot(pg, "wizard", full_page=True)
    await pg.click("#btn-wizard"); await pg.wait_for_selector("#page-dash:not(.hidden)", timeout=180000)
    await pg.set_viewport_size({"width": 1440, "height": 1640})   # tall viewport, not full_page: a resize re-animates charts
    await pg.wait_for_timeout(3500)
    await shot(pg, "dashboard")
    await pg.add_style_tag(content="header{position:static!important}")
    await (await pg.query_selector(".wgrid")).screenshot(path=f"{DEMO_OUT}/demo-wallets.png")
    await pg.set_viewport_size({"width": 1440, "height": 900})
    await pg.goto(f"{BASE}/#tx"); await pg.wait_for_timeout(2500)
    await shot(pg, "transactions")
    await pg.goto(f"{BASE}/#settings"); await pg.wait_for_timeout(2500)
    await shot(pg, "settings", full_page=True)
    await pg.goto(f"{BASE}/#dash"); await pg.wait_for_timeout(2500)
    await pg.click("#btn-priv"); await pg.wait_for_timeout(2500)
    await shot(pg, "privacy")
    await pg.click("#btn-priv")


async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--no-sandbox"])
        if DEMO_OUT:
            await demo(b); await b.close(); return
        for name, vp, pages in (("desktop", {"width": 1440, "height": 900}, ["dash", "tx", "import", "settings"]),
                                ("mobile", {"width": 390, "height": 844}, ["dash"])):
            pg = await b.new_page(viewport=vp, device_scale_factor=2 if name == "mobile" else 1)
            for page in pages:
                await pg.goto(f"{BASE}/#{page}")
                await pg.wait_for_timeout(2500)
                await pg.screenshot(path=f"screenshots/{name}-{page}.png", full_page=True)
                if page == "dash":
                    await pg.add_style_tag(content="header{position:static!important}")
                    el = await pg.query_selector(".wgrid")
                    await el.screenshot(path=f"screenshots/{name}-wallets.png")
        await b.close()

asyncio.run(main())
