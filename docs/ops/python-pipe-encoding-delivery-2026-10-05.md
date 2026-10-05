# Python 管道编码契约：交付与接收（2026-10-05）

决策见 [ADR-0046](../adr/0046-python-pipe-encoding-owned-by-launcher.md)。契约的家是
`shells/launcher_core.py`：启动解释器前覆盖子环境 PYTHONIOENCODING=utf-8，
读方按共同常量 UTF-8 / replace 解码，不改变退出政策、cwd 或管道拓扑。

## 本机交付边界

- m4 的 config/config.toml 仍是 machine_id=m4、role=merge_only；不改变真实配置与数据。
- 三只 exe 跑隔离临时入口，不启动业务界面、浏览器、采集与交换任务；
  BESTSELLER_NO_DIALOG=1，父环境故意给 PYTHONIOENCODING=gbk。
- 中文在入口导入前输出，分别验证 python / pythonw、退出码 0/1/2（18 个用例）。
  日志只检查本次新增字节；旧日志里的 U+FFFD 不清理，也不当作新回归。
- m1/m2/m3 尚未部署；本页是接收说明，不是远端验收完成证明。

## 验证记录

实现采用真子进程红绿测试。定向回归 9 个文件、166 项全部通过：

| 文件 | 用例数 |
| --- | ---: |
| test_python_text_contract | 10 |
| test_launcher_core | 29 |
| test_analyze_launcher / test_exchange_launcher | 6 / 6 |
| test_issue_bundle | 39 |
| test_build_exe | 13 |
| test_release_shells | 5 |
| test_acceptance_check | 33 |
| test_replay_groups | 25 |

- 定向日志：.scratch/logs/dev/python-text-contract/test_*.py.log。
- 三壳构建、TOC 与 --check 全通过；构建日志：
  .scratch/logs/dev/python-text-contract/build-{gui,exchange,analysis}.log。
- 最终真壳实跑：18/18；证据 .scratch/tool-output/encoding/contract-20261005-delivery/summary.json，
  每目标各有 python / pythonw 的自检 JSON 与 0/1/2 退出日志。
- 分段全量仅在收尾跑一次（隔离副本、未 fail-fast）：
  合计 1383 项（预期 1383，计数对账一致），总耗时 677.7s，段结果 7/7 段 OK，结论 OK
  证据 .scratch/logs/full-suite/python-text-contract-20261005/summary.txt；逐段日志同目录。
  全量快照建立后仅有格式、探针证据路径规范化和文档收口；这些工具已重跑定向 / 真壳验证。
- [Standards / Spec 两轴审阅](../reviews/python-text-contract-2026-10-05.md)：本会话审阅，
  两轴均无未解决项；不是独立双 agent 审查。为遵守共享脏工作区的归属约束，未派子 agent，
  未暂存或清理其它会话改动。
- 全部 18 个改动 Python 文件语法编译通过，git diff --check 通过；未配置静态类型检查器，
  不将语法编译称为 typechecking。

最终真壳 SHA256（发布包应逐个相同）：

| 产物 | SHA256 |
| --- | --- |
| inventory_fetch.exe | 0604e35c8946e0fb0d331038d6bde555b27ccd8990b7ca277a74784b8f9eea37 |
| inventory_exchange.exe | 5b66a44b412a95c50fe0aa24a3cd7a68ca38a4a348b55213b51bc6f88256635e |
| bestseller_analysis.exe | 17dbbbbfe644f542b0c222a5aa997690f4ad9873eb1e5638bc99484137fe9720 |

## 重建、提交与出包次序

1. 定向测试通过后，串行执行：

   ```powershell
   python tools/build_exe.py --target gui
   python tools/build_exe.py --target exchange
   python tools/build_exe.py --target analysis
   ```

   各目标执行 TOC 检查与冻结态 --check，禁止夹带 bestseller_monitor 或运行时入口。
2. python tools/verify_shell_encoding.py 在新的 .scratch/tool-output/encoding/ 目录留存真壳
   编码事实、通知政策和 SHA256；不读取真实配置。现有 evidence 目录禁止覆盖。
3. 在隔离副本（根目录名 bestseller，补齐 config/*.toml 与 shops.csv）运行
   python tools/run_full_suite.py，分段串行、不 fail-fast，并核对加载计数。
4. 双轴审查后，按路径将本次源码、测试、文档和三只 dist exe 一起提交；不收其它会话改动。
5. 使用全新的输出目录运行 python tools/release_shells.py --no-build --out <新目录>。
   默认不带 --no-build 会重建，不能用来打包已经验收的同一批产物；现有 release 工具会
   清理给定目录，因此必须选新目录。核对 MANIFEST.json 的 git_commit、shells_clean，
   三只 exe 的 SHA256 必须与验收和提交内容一致。

提交哈希、发布包目录与打包后比对证据在最终交付回复中给出（不在待提交文件里预填自身哈希）。

## m1/m2/m3 接收清单（人工，各机分别记录）

1. 先停止使用旧壳；按该机既有安全更新流程取得包含本次提交的源码。
   不覆盖 config/*.toml、shops.csv、数据库和该机运行目录；保留本机 machine_id / role。
2. 读取发布包 MANIFEST.json 的来源，核对三项文件名和 SHA256。
   把三只 exe 一起替换到 <项目根>\dist\，不能只更新源码而继续用旧壳。
   本次目标：inventory_fetch.exe、inventory_exchange.exe、bestseller_analysis.exe。
3. 用 certutil -hashfile <项目根>\dist\<文件名> SHA256 对照 MANIFEST，逐个一致。
4. 先在 .scratch 下创建本次自检证据目录，再在项目根为每只 exe 执行
   --check --check-report <该目录下的新报告路径>，
   同时设置 BESTSELLER_NO_DIALOG=1。检查 ok=true、frozen=true、bundle_has_project_code=false，
   project_root 为本机项目根，python 为本机可用解释器。此步不启动业务入口。
5. 运行 python tools/verify_shell_encoding.py --out-dir <.scratch 下不存在的新目录>。
   默认验证本机 python 及同目录 pythonw；正常配备两者时应为 18 个用例。
   若只有 python，应明确记录 9 个用例及 pythonw 未验收，不能按 18 项全覆盖报告。
   探针复制壳到隔离根并放临时入口，不依赖业务配置、不起浏览器。
6. 留存每机：源码提交、清单提交、文件 SHA256、自检 JSON、summary.json、探针解释器列表、
   接收日期和操作者。实际业务启用依该机原操作单安排，不由本编码探针代替。

非 Python .cmd/.sh/.bat 引导层保留现有适配；Git/tasklist 解码不纳入本契约。
