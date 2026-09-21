# 0007. 打包 exe 只做启动壳，项目代码一律从源码目录加载

- 状态：已接受
- 日期：2026-09-12

## 背景

`dist/bestseller_gui.exe` 原来是混合形态：PyInstaller 把 `gui.py` 和自己打包的那份 `bestseller_monitor` 一起冻进 exe，而 `gui.py` 在冻结态又把项目根插到 `sys.path` 最前面（`sys.path.insert(0, str(PROJECT_ROOT))`），于是 `bestseller_monitor` 实际来自源码目录，入口脚本却来自 exe。

两边版本一旦漂移就带着半套代码跑。2026-09-12 的事故是 9 月 10 日打包的 exe 带着旧入口脚本，调用已被轮次模块取代的 `Database.start_or_resume()`，抛出 `AttributeError`；界面没有错误反馈（那一半见 IS-37），用户看到的是「点了没反应」，运行库里连轮次都没留下。

漂移至今仍在，只是暂时无害：exe 打包于 03:16（`c880e0d`），HEAD 是 03:20（`aab1dde`），中间那个提交只动了 `bestseller_monitor` 里的翻页逻辑——它被源码目录那份盖住，所以 exe 行为等于 HEAD。由此得到现状的**不对称**：改包立刻生效，改 `gui.py` 或 `docs/ui_live.html` 必须重新打包，而最危险的组合恰是「界面层改了没重打包，它调用的包已经换了」。

两条事实限定了取舍空间：抓取子进程本来就是源码目录的代码（`gui.py` 拉的是 `python run.py`，cwd 为项目根），exe 从来没有内嵌抓取；`gui.py` 的文档串自己也写着「exe 不内嵌抓取与浏览器」。

## 决策

- **exe 是壳**：exe 里不放项目代码，只负责推导项目根、找到本机 python、拉起 `<项目根>\gui.py`。`gui.py`、`docs/ui_live.html`、`bestseller_monitor`、`run.py` 一律来自源码目录，运行时只有一份代码，漂移不可表达。
- 壳的正文是新的 `gui_launcher.py`（项目根），`bestseller_gui.spec` 的 `Analysis` 入口改为它；`gui.py` 不再被冻结，其冻结分支（`_is_frozen()` / `_project_root()` 的冻结路径）随之删除。
- **项目根由 exe 自身位置推导**（`dist` 的上一级），并校验该目录同时存在 `gui.py` 与 `bestseller_monitor`，不成立就给可读错误；`BESTSELLER_PROJECT` 仍可覆盖。exe 必须待在 `<项目根>\dist\`。
- 壳优先用 `pythonw.exe` 拉起界面（不带控制台窗口），退到 `python.exe` + `CREATE_NO_WINDOW`，`BESTSELLER_PYTHON` 可覆盖；选解释器的责任归壳。`gui.py` 自己只认 `sys.executable`，只在它是 `pythonw` 时把采集子进程换成同目录的 `python.exe`，保持采集既有的调用方式。
- 壳常驻等待子进程；stderr 收管道写进 `<项目根>\logs\gui_launcher.log`；**非零退出弹 MessageBox**（中文原因 + 报错尾部几行 + 日志路径），用户正常关窗（退出码 0）不弹。
- 找不到 python、缺 `pywebview`、项目根校验不过、其余未预期异常，四类失败各给一句人话并落日志。
- 重建固定为 `tools/build_exe.py --target gui`（原 `tools/build_gui_exe.py`，票 14 泛化，见文末落地实证），构建后读 `build/bestseller_gui/Analysis-00.toc` 断言产物不含 `bestseller_monitor` 与 `gui`。
- 壳支持 `--check [--check-report <路径>]`：不开界面，只做推导与校验并写出 JSON 报告（项目根、解释器、冻结态、是否夹带项目代码），供构建自检与自动化验证使用。
- `pywebview>=6.2` 进 `requirements.txt`（pythonnet / clr_loader 由它自带，不单列）；README 安装段补 WebView2 运行时前置。

## 结果

改 `gui.py` 或页面文案不再需要重新打包，重新打包也不再需要「记得在改包前后各做一次」。界面与采集从此共用源码目录这一份代码，`config.toml`、`shops.csv`、数据库与运行库路径也不再有第二份来源。

代价有两处。其一，GUI 依赖本机 python 具备 pywebview——已声明进 `requirements.txt`，但它是新的硬前置。其二，启动失败的表达从 PyInstaller 的异常框转由壳负责；壳不弹窗就等于什么都不说，所以失败可见性是这条决策的必要部分，不是附加项。

被否掉的三个方向：

- **让 exe 自洽**（冻结态不插项目根，界面用 exe 内打包的包）：抓取子进程仍走源码目录，漂移从「界面自相矛盾」变成「界面与采集各用一套代码读同一个库」；要真自洽得把 playwright 和浏览器一起打包，是另一个量级的工程。
- **保留混合形态、加一致性校验**（启动时比对两处版本，不一致就在首页提示）：改动最小，但把「随时可能半套代码」永久留着，只是在事后加了个体温计。
- **取消 exe**（`pythonw` + 快捷方式）：零打包、零漂移，代价是丢掉「双击一个文件就能起来」的手感，而壳能用同样的手感拿到同样的好处。

相关工单为 IS-52；实现见 `gui_launcher.py`、`bestseller_gui.spec`、`tools/build_exe.py --target gui`。运行形态的操作口径见 README 的「打包与运行形态」一节。

**落地实证（2026-09-21，票 14）**：壳正文抽芯为 `launcher_core.py`，两只薄入口
（`gui_launcher.py` / `exchange_launcher.py`）各自填目标差异：拉起的脚本、项目根标记、日志名、
弹窗政策（采集壳「非零都弹」；交换台壳「0/1 静默、2 与启动失败才弹」——退出码 1 是正常结局）。
重建入口泛化为 `python tools/build_exe.py --target gui|exchange`（`tools/build_gui_exe.py`
随之退役、不再存在），新增第二只壳：交换台壳（`bestseller_exchange.spec` →
`dist/bestseller_exchange.exe`，拉 `<项目根>\exchange.py --window`，不转发参数）。本 ADR 的
壳形态（项目代码一律从源码目录加载、项目根按 exe 位置推导、exe 待在 `<项目根>\dist\`）
对两只壳一视同仁；演练取证在 `.scratch/multi-machine-collection-impl/ticket14/`。

**落地实证（2026-09-21，分析线票 14）**：加入第三只壳——分析壳，与上述两只同形：
`analysis_launcher.py` + `bestseller_analysis.spec` → `dist/bestseller_analysis.exe`，拉
`<项目根>\analyze.py`（不带参数，缺省即开窗）、项目根标记 `analyze.py` + `bestseller_monitor`、
弹窗政策同采集壳「非零都弹」；重建入口随之泛化为
`python tools/build_exe.py --target gui|exchange|analysis`。名实分裂是有意的：目标键、spec、
薄入口、日志与 `--target` 都叫 `analysis`，被拉的脚本仍叫 `analyze.py`（写错会当场失败）。
演练取证在 `.scratch/bestseller-analysis/ticket14/`。
