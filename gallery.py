"""Build App Store gallery images (1440x900 JPG) from the locally running app with DEMO data:
python gallery.py [base_url] -> satoshi-btc-dashboard/gallery/{1..4}.jpg"""
import sys, asyncio, io, os
from PIL import Image, ImageDraw, ImageFont
from playwright.async_api import async_playwright

ARGS = [a for a in sys.argv[1:] if not a.startswith("--")]
BASE = ARGS[0] if ARGS else "http://localhost:8765"
DEMO = "--demo" in sys.argv   # Satoshi Demo app (run with DEMO_MODE=1, wizard already completed)
OUT = "satoshi-demo/gallery" if DEMO else "satoshi-btc-dashboard/gallery"
W, H, BAR = 1440, 900, 90
SHOTS = [("dash", None, "Your whole Bitcoin stack at a glance"),
         ("dash", "#wallets-panel", "Every wallet: Strike, exchanges, multisig and single-sig cold storage"),
         ("dash", "#cgt-table", "Estimate what you keep after UK Capital Gains Tax"),
         ("settings", "#cold-wallets", "Import CSVs, auto-record Strike DCA, watch-only wallets via your Electrs")]
if DEMO:
    SHOTS = [("dash", None, "Demo: a made-up stack priced at real BTC/GBP history"),
             ("dash", "#wallets-panel", "Fake exchanges, a 2-of-3 multisig vault and a single-sig wallet"),
             ("dash", "#cgt-table", "Every feature of the real app, including UK CGT")]
F = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def compose(png, caption):
    img = Image.new("RGB", (W, H), "#1a1d23")
    img.paste(Image.open(io.BytesIO(png)).convert("RGB"), (0, BAR))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, BAR], fill="#111318"); d.rectangle([0, BAR - 3, W, BAR], fill="#F7931A")
    icon = Image.open("branding/icon-64.png").convert("RGBA").resize((52, 52))
    img.paste(icon, (28, (BAR - 52) // 2), icon)
    f = ImageFont.truetype(F, 30)
    d.text((100, BAR // 2), caption, font=f, fill="white", anchor="lm")
    return img


async def main():
    os.makedirs(OUT, exist_ok=True)
    async with async_playwright() as p:
        b = await p.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--no-sandbox"])
        pg = await b.new_page(viewport={"width": W, "height": H - BAR})
        for i, (page, sel, cap) in enumerate(SHOTS, 1):
            await pg.goto(f"{BASE}/#{page}"); await pg.reload()
            await pg.wait_for_timeout(2500)
            if sel:
                await pg.evaluate("""s => { const e = document.querySelector(s).closest('.panel') || document.querySelector(s);
                    const hh = (document.querySelector('header')||{offsetHeight:0}).offsetHeight;
                    window.scrollTo(0, e.getBoundingClientRect().top + scrollY - hh - 16); }""", sel)
                await pg.wait_for_timeout(800)
            compose(await pg.screenshot(), cap).save(f"{OUT}/{i}.jpg", quality=90)
        await b.close()

asyncio.run(main())
