# 票04 Standards 最终兼容复审

范围：802ee29f993b0fe3e642ef5b6145315cb3296c3a → b861876a6b41d45c71c0e4dabf82affe953248f7；重点 faa2b0c → b861876。实施仓 HEAD 符合固定候选，读取时工作区干净。本报告仅 Standards 轴。

## 硬性违反
**无发现。**

- `F:\AI\projects\bestseller\.scratch\sandboxes\issues-08-11\01\20261008-postmerge\bestseller\tests\helpers.py:91` 增加 `gui_browser_settings=None`，与真实 Config 的中性默认一致，不另建配置替身。适用标准 `F:\AI\projects\bestseller\.scratch\sandboxes\issues-08-11\01\20261008-postmerge\bestseller\docs\adr\0022-crawler-cfg-in-one-fixture.md` 原句：“其余 `Config` 字段给中性默认，要用的用例自己传”；“替身与真 `Config` 同形”。
- `F:\AI\projects\bestseller\.scratch\sandboxes\issues-08-11\01\20261008-postmerge\bestseller\tools\bench_refresh.py:55–59` 只补 Api 初始化必需的浏览器配置字段；`start_browser=False`、空 executable 与独立 unused-profile 不授予启动能力。DedicatedBrowser 初始化只冻结 Settings，刷新基准仍测真实 `get_run()`，未加入创建浏览器入口。ADR-0022 原句：“`tools/bench_refresh.py` 是**工具**、不依赖 `tests/`”；本补丁没有引入 tests 依赖。
- 已核对前轮 faa2b0c 双轴报告与浏览器事实边界；本次两文件增量不改变0007源码部署、0008采集互斥、0028停止副作用归属及0048关窗政策，也未扩展票05–07。

## 判断性异味
固定12项 Fowler 启发式无新增可操作发现。工具的局部 Api 入参不是违反集中采集夹具规范的重复；新增字段有已发生兼容失败的具体需求，不属 Speculative Generality。未发现应新建抽象或扩大重构的理由。

## 证据边界
只读核对 `F:\AI\projects\bestseller\.scratch\logs\tickets\issues-08-11\04\20261009-ticket04-01\30-full-compat-green.log`：2、7、19项均 OK。本代理未重跑测试、未启动浏览器、未修改仓库。最终分段全量仍由父代理完成；既有 matching 用例首轮失败和独立复跑通过不等同最终全量通过，部署机人工验收也未被替代。
