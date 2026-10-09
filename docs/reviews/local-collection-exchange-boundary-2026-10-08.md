# 汇总机试采数据外发边界：只读调查

任务：grill_local_collection_exchange_20261008；日期：2026-10-08。仅静态读代码、文档与测试，未跑测试、采集、交换或联网；未修改代码或执行 git 写操作。

## 1. 结构化数据选取与“只禁外发、保留收取”

- raw 周包固定七表：shops/products/skus/inventory/product_information_versions/sku_image_versions/product_image_assets。库存按 date、商品/SKU 图观测按 observed_date 取北京时间 ISO 周；身份表按周内关联商品/店铺取，资产仅取已入包观测引用的哈希，包不含图片字节。证据：`F:\AI\projects\bestseller\bestseller_monitor\export.py:144–220`。
- “本机采的”不是逐行来源标签：观测组按 (店铺, 日期) 的 merge_claims 与最新商品观测时刻认归属；外机 claim 与当前时刻一致才排除，未记账或本机后来重采会照发；身份表按 merge_seen 与 last_seen_at 类似过滤。**不是周计划内才发，也没有在这些选取条件里区分“试采”与“正式采集”。**证据：`F:\AI\projects\bestseller\bestseller_monitor\export.py:112–141,180–219`。
- 从编排上可只停 raw 导出而保留接收：导出、raw 汇总、判断集收取分别独立执行；`--only merge` 只做 raw 汇总，`--only collect` 只收判断集。默认 merge_only 也保留两种接收。证据：`F:\AI\projects\bestseller\bestseller_monitor\exchange.py:463–501`。
- 但现有角色**没有“允许本机试采但永不外发”的档位**：merge_only 同时禁采集；collector 正常整趟会导出本机库。靠人工每次选 merge 并非持久禁止。底层 `export.export` 本身不判断 role（闸门在交换台），不能把入口跳过称作所有调用路径的安全边界。证据：`F:\AI\projects\bestseller\run.py:88–96`；`F:\AI\projects\bestseller\bestseller_monitor\export.py:735–783`。

## 2. 图片外发路径

- 当前采集主图/SKU 图是从商品 URL 下载并留证，字节写本地 product_image_assets；未找到采集时自动上传 COS 的调用。证据：`F:\AI\projects\bestseller\bestseller_monitor\detail.py:249–250,286`；`F:\AI\projects\bestseller\bestseller_monitor\product_images.py:11–24`；`F:\AI\projects\bestseller\bestseller_monitor\db.py:1485–1495`。
- **交换导出会上传**：先推 raw 包与图片清单，成功后按包引用哈希检查 COS key，只上传缺失 key 的本地字节；同内容包重跑仍会补传图片。`store.upload` 的生产调用定位于 export.upload_new_images；真实实现走 coscli cp。因此只挡 git push 不应被等同于设计上的图片禁止，需连图片外发动作一起定义。证据：`F:\AI\projects\bestseller\bestseller_monitor\export.py:610–644,771–783`；`F:\AI\projects\bestseller\bestseller_monitor\cos_store.py:109–113`。

## 3. 判断集独立外发

- **有独立通道**：judged-<机器> 发布与角色无关，merge_only 默认整趟照发，也可 `--only publish` 单独发。证据：`F:\AI\projects\bestseller\bestseller_monitor\exchange.py:476–483`；`F:\AI\projects\bestseller\bestseller_monitor\judgment_set.py:319–378`。
- 包不含库存观测表或 evidence.image_data 图片字节，也没有调用 COS 上传；但会发 **商品证据索引 identity/version/name/image_hash/origin、视觉描述、判断结果及人工决定**。缓存表读全体行，不按本机试采来源排除；所以“raw 不导出”不等于“试采商品的名称/哈希/描述等绝不外发”。证据：`F:\AI\projects\bestseller\bestseller_monitor\judgment_set.py:103–139,190–210`；`F:\AI\projects\bestseller\bestseller_monitor\matching.py:904–910`。
- 旁路提示（不属于判断包交换）：若边界指“任何外部服务都不得收到图片”，分析模型请求也是外发点，direct 模式放图片，描述模式将图片发 vision endpoint；本任务没有运行或核验当前分析配置。证据：`F:\AI\projects\bestseller\bestseller_monitor\matching.py:382–395`。

## 4. 角色与实际边界 / 设计冲突

- 配置只有 collector（采集机）与 merge_only（纯汇总机），不存在 aggregator 字符串。collector 也能汇总；汇总没有固定机器。merge_only = 不采、不导出 raw，但能汇总/展示及发布/收取判断集。证据：`F:\AI\projects\bestseller\bestseller_monitor\config.py:69–85,112–117`；`F:\AI\projects\bestseller\CONTEXT.md:131,141–151`。
- git_access/cos_access 只是记录档位，实际限制需要真实凭据；Gitee 凭据因判断发布保留可写，COS 应换只读 AK。不能只把字段改成 read_only 就证明无外发。证据：`F:\AI\projects\bestseller\bestseller_monitor\config.py:88–93,196–197,254–255`；`F:\AI\projects\bestseller\docs\adr\0034-machine-identity-and-role-change.md:3,28–35`。
- 现有 ADR-0034 明定过渡期试采是真观测、留库并导出；终局纯汇总机不采集。若改成“汇总机可试采、只留本机”，两处决策均需重开，不能靠换名字自洽。证据：`F:\AI\projects\bestseller\docs\adr\0034-machine-identity-and-role-change.md:24–33`；`F:\AI\projects\bestseller\CONTEXT.md:149–151`。
- ADR-0031 明确不给观测加行级来源，归属取包/claim；其头部仍写“实现未落地”，与目前实现/测试不一致，引用实现状态应以代码为准。证据：`F:\AI\projects\bestseller\docs\adr\0031-cross-machine-merge-decided-by-package-claim.md:3,23–28,32–49`；前述 export 来源过滤和下列回归测试。

## 已有测试（只读核验，未执行）

- `F:\AI\projects\bestseller\tests\test_export.py:125,160,205`：周切片、SKU 图片流水、资产并集；`:515,532,598`：新增图片上传、同包补传、图片失败不撤 raw 包。
- `F:\AI\projects\bestseller\tests\test_exchange.py:793`：merge_only 仍发布判断；`:896,915,946`：跳过导出、汇总仍运行、raw/plan 通道不写；**946 不能解读为判断库也禁写**；`:1095,1108`：外机行不换名重发、本机后来重采重新导出。
- `F:\AI\projects\bestseller\tests\test_judgment_set.py:153,178–205,640,672`：判断包表集与图片列排除、两种交换集互不干扰。
- `F:\AI\projects\bestseller\tests\test_gui.py:223,246`；`F:\AI\projects\bestseller\tests\test_plan_step.py:907`：merge_only 拒绝开始/续跑、跳过周计划检查生成发布。

## 需父 agent 向用户确认 / 未查明

1. 禁止范围：只禁 raw 库与 COS，还是也禁 judged 商品元数据/视觉描述，以及分析模型请求？
2. 禁止对象：该机器所有本机采集（含旧历史），还是仅新增试采轮次？若以后转回 collector，旧试采是否永久不得补发？现有周窗口/claim 过滤不表达后者。
3. 试采是否仍写正式库并影响本机分析/合并胜负，还是隔离存储？单纯禁导出不隔离本机胜负与统计。
4. 当前本机角色、真实凭据权限、计划名册及曾否外发未核验；禁止今后外发不会撤回远端已有 raw 包、git 历史或 COS 对象。