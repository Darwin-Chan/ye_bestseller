import pathlib
from playwright.sync_api import sync_playwright

html = (pathlib.Path("docs/ui_mockup.html")).resolve().as_uri()
out = pathlib.Path("output")
out.mkdir(parents=True, exist_ok=True)
with sync_playwright() as p:
    b = p.chromium.launch(channel="msedge", headless=True)
    pg = b.new_page(viewport={"width": 1100, "height": 900}, device_scale_factor=2)
    pg.goto(html)
    pg.wait_for_timeout(400)
    for tab, name in [("start", "ui_1_start"), ("run", "ui_2_run"), ("result", "ui_3_result")]:
        pg.click(f'.tab[data-tab="{tab}"]')
        pg.wait_for_timeout(150)
        pg.screenshot(path=str(out / f"{name}.png"))
    b.close()
print("shots done")
