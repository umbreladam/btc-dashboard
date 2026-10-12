"""Screenshot the locally running app: python screenshot.py [base_url]"""
import sys, asyncio
from playwright.async_api import async_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8765"


async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--no-sandbox"])
        for name, vp, pages in (("desktop", {"width": 1440, "height": 900}, ["dash", "tx", "import", "settings"]),
                                ("mobile", {"width": 390, "height": 844}, ["dash"])):
            pg = await b.new_page(viewport=vp, device_scale_factor=2 if name == "mobile" else 1)
            for page in pages:
                await pg.goto(f"{BASE}/#{page}")
                await pg.wait_for_timeout(2500)
                await pg.screenshot(path=f"screenshots/{name}-{page}.png", full_page=True)
        await b.close()

asyncio.run(main())
