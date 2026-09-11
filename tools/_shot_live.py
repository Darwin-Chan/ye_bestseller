"""给 docs/ui_live.html 注入 mock pywebview API，渲染三屏截图（前端 QA 用）。"""
import pathlib
import textwrap
from playwright.sync_api import sync_playwright

here = pathlib.Path(__file__).resolve().parent
html = (here.parent / "docs" / "ui_live.html").resolve().as_uri()
out = here.parent / "output"
out.mkdir(parents=True, exist_ok=True)

MOCK = r"""
let paused = false;
const shops = (() => {
  const names = ["义乌市中茂箱包有限公司","义乌市奔此日用品有限公司","义乌市中茂专业设计有限公司","义乌市快茂日用品有限公司",
    "义乌市中蓝箱包有限公司","义乌市快勤日用品有限公司","义乌市暖宏纺织品有限公司","义乌市创柔文化用品有限公司",
    "义乌市启弘纺织品有限公司","义乌市中腾箱包有限公司","义乌市通伟塑料制品有限公司","义乌市远强不锈钢制品有限公司"];
  const prod = [92,95,90,87,90,81,30,89,62,90,90,49];
  const sku = [614,530,542,432,515,460,118,549,285,490,441,273];
  return names.map((n,i)=>({key:"A"+(i+1).toString().padStart(2,"0"), name:n, products:prod[i], skus:sku[i],
    pages:3, default_checked: prod[i] < 3*30}));
})();

const done = [
  {key:"A01",name:shops[0].name,products:33,skus:174,duration:"34.5 分",deny:55},
  {key:"A02",name:shops[1].name,products:40,skus:192,duration:"33.0 分",deny:52},
  {key:"A03",name:shops[2].name,products:33,skus:181,duration:"34.6 分",deny:56},
];
const todo = shops.slice(3).map(s=>({key:s.key,name:s.name}));

window.pywebview = { api: {
  get_start: async () => ({
    ov:{products:945, skus:5249}, summary:{started:true, rounds:1, text:"09-06 00:21 开始 · 跑约 127 分钟 · 人工放弃"},
    shops, total_shops:12, active_round_id:null,
  }),
  get_run: async () => ({
    running:!paused, manually_paused:paused, has_round:true, round_id:12, started_hhmm:"00:21",
    elapsed_sec:2530, deny:203, done_count:3, total_count:12, progress:0.25, current_shop:"A04",
    phase:"detail", done, todo,
  }),
  get_result: async () => ({
    has_round:true, round_id:12, status:"已放弃", started_hhmm:"00:21", finished_hhmm:"02:27",
    duration_text:"2 时 6 分", deny:203, done_count:3, total_count:12, products_total:106, skus_total:691,
    done, todo, note:"本轮由人工中止（放弃），已抓取数据已保留、不再续跑；未抓取店铺见下方。", tag:"人工中止",
  }),
  start_run: async () => ({ok:true, round_id:12}),
  pause_run: async () => { paused = true; return {ok:true}; },
  resume_run: async () => { paused = false; return {ok:true}; },
  abort_run: async () => ({ok:true}),
}};
"""

with sync_playwright() as p:
    b = p.chromium.launch(channel="msedge", headless=True)
    pg = b.new_page(viewport={"width": 1100, "height": 900}, device_scale_factor=2)
    pg.add_init_script(MOCK)
    pg.goto(html)
    pg.wait_for_timeout(500)
    pg.screenshot(path=str(out / "live_1_start.png"))
    # 开始 -> 过程
    pg.click("#startBtn")
    pg.wait_for_timeout(700)
    pg.screenshot(path=str(out / "live_2_run.png"))
    # 暂停 -> 显示“继续”
    pg.click("#pauseBtn")
    pg.wait_for_timeout(500)
    pg.screenshot(path=str(out / "live_2b_paused.png"))
    # 继续 -> 回到运行
    pg.click("#resumeBtn")
    pg.wait_for_timeout(500)
    # 中止 -> 弹确认框
    pg.click("#abortBtn")
    pg.wait_for_timeout(300)
    pg.screenshot(path=str(out / "live_2c_confirm.png"))
    # 确定 -> 结果
    pg.click("#confirmOk")
    pg.wait_for_timeout(500)
    pg.screenshot(path=str(out / "live_3_result.png"))
    b.close()
print("live shots done")
