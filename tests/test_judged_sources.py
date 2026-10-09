"""票 01：m4 维护判断来源清单——经 Gitee API 生成并发布可信 `judged-*` 来源快照。

接缝（都走公开口）：

- **来源清单的刷新与发布**：`judged_sources.refresh`——真 git（本地裸库当远端、`plan`
  克隆当交换区那份）、替身 provider（给一页仓库清单或抛 `SourceQueryError`），世界每测试
  进程只搭一次模板、用例复制取独立副本（同 `test_exchange` / `test_judgment_set`）。
- **Gitee API 客户端**：`judged_sources.GiteeApi.list_repos`——替身 HTTP 传输（`fetch`）
  给按页的仓库 JSON 或抛错，不碰真实凭据与网络（票面口径）。
- **整趟接线**：`exchange.run_once` 里注一次 provider——只在实际交换任务启动时刷新，
  打开交换窗口 / 启动分析都不刷新。

过滤器、分页、空库、无变化、缩减与改址保护都在这一层验；不加真实凭据。
"""
from __future__ import annotations

import datetime as dt
import json
import pathlib
import unittest

import bestseller_monitor
import exchange
from bestseller_monitor import judged_sources
from bestseller_monitor.config import ROLE_MERGE_ONLY
from bestseller_monitor.db import CST
from bestseller_monitor.git_channel import GitChannel
from tests.git_repos import GitSandbox, WorldTemplate
from test_exchange import ConsoleWorld, todo_of

# 固定生成时刻：它进快照正文（生成记录），但不参与「无变化」判定。
STAMP = dt.datetime(2026, 10, 9, 21, 0, tzinfo=CST)
STAMP_ISO = STAMP.isoformat(timespec="seconds")
OWNER = "yeshuzhen"

_PLAN_PATHS = {"remote": "plan.git", "exchange": "exchange/plan"}


def _plan_template() -> WorldTemplate:
    """计划库那一件世界模板：空裸库 + 未出生的克隆（上机清单里的形状）。"""
    names = _PLAN_PATHS
    return WorldTemplate.obtain("judged-sources-plan",
                                lambda site: site.world(names["remote"], names["exchange"]))


def _gitee(name, *, private=True, owner=OWNER, remote="") -> judged_sources.GiteeRepo:
    return judged_sources.GiteeRepo(name=name, owner=owner, private=private, remote=remote)


class FakeProvider:
    """替身 provider：给一页仓库清单，或在查询时抛错（模拟 API 失败 / 分页不完整）。"""

    def __init__(self, repos=(), *, error: str | None = None):
        self.repos = tuple(repos)
        self.error = error
        self.calls = 0

    def list_repos(self):
        self.calls += 1
        if self.error is not None:
            raise judged_sources.SourceQueryError(self.error)
        return self.repos


class RefreshCase(unittest.TestCase):
    """共用夹具：交换区里一个 `plan` 克隆（裸库当远端）+ 各用例自己的替身 provider。"""

    def setUp(self):
        self.box = GitSandbox(self, prefix="bestseller-sources-")
        self.exchange = self.box.tmp / "exchange"
        self.exchange.mkdir()
        _plan_template().take(self.box)
        self.plan = self.box.tmp / _PLAN_PATHS["exchange"]

    def refresh(self, provider, *, owner=OWNER, generator="m4", confirm_shrink=False, now=STAMP):
        return judged_sources.refresh(
            self.exchange, owner=owner, generator=generator, provider=provider,
            now=now, confirm_shrink=confirm_shrink)

    # ---- 读口 ----

    def committed(self) -> dict | None:
        """plan 库 HEAD 里的快照（远端的事实）；还没发布过就是 None。"""
        raw = GitChannel(self.plan).read_path(judged_sources.SNAPSHOT_REL)
        return json.loads(raw.decode("utf-8")) if raw is not None else None

    def machines(self) -> list[str]:
        snapshot = self.committed() or {}
        return [source["machine_id"] for source in snapshot.get("sources", ())]

    def commits(self) -> int:
        return int(self.box.must("rev-list", "--count", "HEAD", cwd=self.plan).strip())


class RefreshTests(RefreshCase):
    """生成与发布：过滤、分页完整、空库入列、无变化不提交、缩减/改址保护。"""

    def test_a_complete_query_publishes_a_credential_free_snapshot(self):
        provider = FakeProvider([_gitee("judged-m1"), _gitee("judged-m4")])

        result = self.refresh(provider)

        self.assertTrue(result.published, result.failure)
        self.assertFalse(result.failed)
        self.assertFalse(result.unchanged)
        self.assertEqual(result.owner, OWNER)
        self.assertEqual(result.generator, "m4")
        snapshot = self.committed()
        self.assertEqual(snapshot["format_version"], judged_sources.FORMAT_VERSION)
        self.assertEqual(snapshot["owner"], OWNER)
        self.assertEqual(snapshot["generated_by"], "m4")
        self.assertEqual(snapshot["generated_at"], STAMP_ISO)
        self.assertEqual([s["machine_id"] for s in snapshot["sources"]], ["m1", "m4"])
        self.assertEqual(snapshot["sources"][0]["repo"], "judged-m1")
        self.assertEqual(snapshot["sources"][0]["remote"], f"git@gitee.com:{OWNER}/judged-m1.git")
        self.assertEqual(list(snapshot), ["format_version", "generated_at", "generated_by",
                                          "owner", "sources"], "快照只有这几个键（无凭据、无包内容）")
        self.assertEqual(list(snapshot["sources"][0]), ["machine_id", "remote", "repo"])
        self.assertEqual(self.commits(), 1)

    def test_only_private_judged_repos_with_a_valid_number_are_included(self):
        provider = FakeProvider([
            _gitee("judged-m1"),                       # 合规：私有 + judged-m<正整数>
            _gitee("judged-m4"),
            _gitee("judged-m2", private=False),        # 公开库不入列
            _gitee("judged-test"),                     # 测试/演示/旧名不入列
            _gitee("judged-old"),
            _gitee("judged-demo"),
            _gitee("judged-m01"),                      # 机器编号不合规（前导零）
            _gitee("judged-m0"),                       # 编号从 1 起
            _gitee("judged-worker"),                   # 编号不是数字
            _gitee("raw-m3"),                          # 不是 judged- 前缀
            _gitee("judged-m9", owner="someoneelse"),  # 别人 owner 下的库
        ])

        result = self.refresh(provider)

        self.assertTrue(result.published, result.failure)
        self.assertEqual(self.machines(), ["m1", "m4"])

    def test_an_empty_remote_repo_is_still_a_source(self):
        """库存在即入列：远端还没有判断集包（空库）也代表该来源应接入。"""
        provider = FakeProvider([_gitee("judged-m2")])

        result = self.refresh(provider)

        self.assertFalse(result.failed, result.failure)
        self.assertEqual(self.machines(), ["m2"])

    def test_a_no_change_refresh_makes_no_commit(self):
        provider = FakeProvider([_gitee("judged-m1"), _gitee("judged-m2")])
        self.refresh(provider)
        published = self.committed()

        second = self.refresh(provider, now=STAMP + dt.timedelta(hours=3))

        self.assertTrue(second.unchanged, "同来源同地址：无变化")
        self.assertTrue(second.published)
        self.assertFalse(second.failed)
        self.assertEqual(self.commits(), 1, "无变化不产生提交")
        self.assertEqual(self.committed()["generated_at"], published["generated_at"],
                         "无变化时文件一个字不动（生成时刻也不刷新）")

    def test_a_shrinking_source_list_is_held_for_confirmation(self):
        self.refresh(FakeProvider([_gitee("judged-m1"), _gitee("judged-m2")]))

        result = self.refresh(FakeProvider([_gitee("judged-m1")]))

        self.assertTrue(result.held, "来源减少：保留上一版、等人确认")
        self.assertFalse(result.published)
        self.assertFalse(result.failed)
        self.assertEqual(self.machines(), ["m1", "m2"], "上一版有效快照原样留着")
        self.assertEqual(self.commits(), 1)
        self.assertIn("m2", "；".join(result.warnings))
        self.assertIn("确认", "；".join(result.warnings))

    def test_a_changed_remote_address_is_held_for_confirmation(self):
        self.refresh(FakeProvider([_gitee("judged-m1", remote="git@gitee.com:alice/judged-m1.git")]))

        result = self.refresh(FakeProvider([_gitee("judged-m1", remote="git@gitee.com:bob/judged-m1.git")]))

        self.assertTrue(result.held, "远端地址变化：保留上一版、等人确认")
        self.assertFalse(result.published)
        self.assertEqual(self.commits(), 1)
        self.assertIn("m1", "；".join(result.warnings))

    def test_a_source_added_later_publishes_a_new_commit(self):
        self.refresh(FakeProvider([_gitee("judged-m1")]))

        result = self.refresh(FakeProvider([_gitee("judged-m1"), _gitee("judged-m4")]))

        self.assertTrue(result.published, result.failure)
        self.assertFalse(result.held)
        self.assertEqual(self.machines(), ["m1", "m4"])
        self.assertEqual(self.commits(), 2)

    def test_an_api_failure_keeps_the_previous_snapshot(self):
        self.refresh(FakeProvider([_gitee("judged-m1"), _gitee("judged-m2")]))

        result = self.refresh(FakeProvider(error="连接超时"))

        self.assertTrue(result.failed)
        self.assertFalse(result.published)
        self.assertIn("连接超时", result.failure)
        self.assertEqual(self.machines(), ["m1", "m2"], "查不全就不覆盖上一版")
        self.assertEqual(self.commits(), 1)

    def test_a_machine_without_the_owner_does_not_touch_the_network(self):
        provider = FakeProvider([_gitee("judged-m1")])

        result = self.refresh(provider, owner="")

        self.assertFalse(result.failed, "没配 owner 不是失败：本机不是来源维护机")
        self.assertFalse(result.published)
        self.assertIsNotNone(result.note)
        self.assertEqual(provider.calls, 0, "不是维护机就不查 Gitee")
        self.assertIsNone(self.committed())

    def test_a_missing_plan_clone_is_a_readable_failure(self):
        import shutil
        shutil.rmtree(self.plan)

        result = self.refresh(FakeProvider([_gitee("judged-m1")]))

        self.assertTrue(result.failed)
        self.assertIn("plan", result.failure)

    def test_a_configured_owner_without_a_token_is_a_readable_failure(self):
        result = self.refresh(None)      # provider 为空 = 有 owner 但读不到 GITEE_TOKEN

        self.assertTrue(result.failed)
        self.assertIn(judged_sources.TOKEN_ENV, result.failure)

    def test_a_confirmed_shrink_is_published(self):
        """维护人确认后（`confirm_shrink`）才按新的发：上一版被替换成减少后的清单。"""
        self.refresh(FakeProvider([_gitee("judged-m1"), _gitee("judged-m2")]))

        result = self.refresh(FakeProvider([_gitee("judged-m1")]), confirm_shrink=True)

        self.assertTrue(result.published, result.failure)
        self.assertFalse(result.held)
        self.assertEqual(self.machines(), ["m1"])
        self.assertEqual(self.commits(), 2)

    def test_a_broken_previous_snapshot_is_not_overwritten(self):
        """上一版读不动 = 不覆盖、不猜：坏文件绝不能静默关掉缩减保护。"""
        self.box.commit_push(self.plan, {judged_sources.SNAPSHOT_REL: b"not a manifest at all"})

        result = self.refresh(FakeProvider([_gitee("judged-m1")]))

        self.assertTrue(result.failed)
        self.assertIn(judged_sources.SNAPSHOT_REL, result.failure)
        self.assertEqual(self.commits(), 1, "坏文件原样留着，没有拿候选盖掉它")


class GiteeApiTests(unittest.TestCase):
    """Gitee API 客户端的公开口：翻页、认证在头里、失败语义——都经替身传输。"""

    TOKEN = "secret-gitee-token"

    def client(self, fetch, **kwargs):
        return judged_sources.GiteeApi(OWNER, self.TOKEN, per_page=2, fetch=fetch, **kwargs)

    def recording_fetch(self, pages: dict[int, list[dict]], *, error_on: int | None = None):
        """一个替身传输：按 URL 里的 page 参数回对应的一页；要哪一页报错就报错。"""
        seen: list[tuple[str, dict]] = []

        def fetch(url: str, headers: dict):
            seen.append((url, dict(headers)))
            page = int(url.rsplit("page=", 1)[-1].split("&")[0])
            if error_on is not None and page == error_on:
                raise OSError("HTTP 500")
            return pages.get(page, [])

        return fetch, seen

    def test_the_client_pages_through_every_page(self):
        fetch, seen = self.recording_fetch({
            1: [{"name": "judged-m1", "private": True, "namespace": {"path": OWNER}},
                {"name": "judged-m2", "private": True, "namespace": {"path": OWNER}}],
            2: [{"name": "judged-m4", "private": True, "namespace": {"path": OWNER}}],
        })

        repos = self.client(fetch).list_repos()

        self.assertEqual([repo.name for repo in repos], ["judged-m1", "judged-m2", "judged-m4"])
        self.assertEqual(len(seen), 2, "第二页不满就到底了，不再多问一页")
        self.assertIn("per_page=2", seen[0][0])
        self.assertIn("page=1", seen[0][0])
        self.assertIn("type=all", seen[0][0], "私有库要一起列出来")

    def test_the_client_stops_on_an_empty_last_page(self):
        fetch, seen = self.recording_fetch({
            1: [{"name": "judged-m1", "private": True, "namespace": {"path": OWNER}},
                {"name": "judged-m2", "private": True, "namespace": {"path": OWNER}}],
            2: [],
        })

        repos = self.client(fetch).list_repos()

        self.assertEqual([repo.name for repo in repos], ["judged-m1", "judged-m2"])
        self.assertEqual(len(seen), 2)

    def test_the_token_rides_in_the_header_never_in_the_url(self):
        fetch, seen = self.recording_fetch({1: []})

        self.client(fetch).list_repos()

        url, headers = seen[0]
        self.assertNotIn(self.TOKEN, url, "token 不进 URL（会漏进日志）")
        self.assertEqual(headers.get("Authorization"), f"token {self.TOKEN}")

    def test_a_failure_on_a_later_page_raises_without_leaking_the_token(self):
        fetch, _ = self.recording_fetch(
            {1: [{"name": "judged-m1", "private": True, "namespace": {"path": OWNER}},
                 {"name": "judged-m2", "private": True, "namespace": {"path": OWNER}}]},
            error_on=2)

        with self.assertRaises(judged_sources.SourceQueryError) as caught:
            self.client(fetch).list_repos()

        self.assertNotIn(self.TOKEN, str(caught.exception))
        self.assertIn("2", str(caught.exception), "点名翻到哪一页没成")

    def test_an_endless_full_pages_run_is_treated_as_incomplete(self):
        fetch, _ = self.recording_fetch({})   # 每页都满（默认回全满的一页）：翻不到底

        def endless(url: str, headers: dict):
            return [{"name": f"judged-m{n}", "private": True, "namespace": {"path": OWNER}}
                    for n in (1, 2)]

        with self.assertRaises(judged_sources.SourceQueryError):
            self.client(endless).list_repos()

    def test_the_client_reads_the_documented_repo_fields(self):
        fetch, _ = self.recording_fetch({1: [
            {"name": "judged-m1", "private": True, "namespace": {"path": OWNER},
             "ssh_url": "git@gitee.com:somebody/judged-m1.git"}]})

        repo = self.client(fetch).list_repos()[0]

        self.assertEqual(repo.name, "judged-m1")
        self.assertEqual(repo.owner, OWNER)
        self.assertTrue(repo.private)
        self.assertEqual(repo.remote, "git@gitee.com:somebody/judged-m1.git",
                         "远端地址取 API 给的 ssh_url")


class ExchangeWiringTests(unittest.TestCase):
    """整趟接线（票 01）：只在实际交换任务启动时刷新来源清单；窗口打开 / 启动分析都不触发。"""

    def setUp(self):
        self.world = ConsoleWorld(self, machine_id="m4", role=ROLE_MERGE_ONLY,
                                  gitee_owner=OWNER)
        _plan_template().take(self.world.box)

    def snapshot(self) -> dict | None:
        path = self.world.root / "plan" / judged_sources.SNAPSHOT_REL
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None

    def provider(self, *names) -> FakeProvider:
        return FakeProvider([_gitee(name) for name in names])

    def test_a_full_run_refreshes_and_publishes_the_source_snapshot(self):
        outcome = self.world.run(source_provider=self.provider("judged-m1", "judged-m4"))

        self.assertEqual(outcome.exit_code, 0, outcome.failures)
        self.assertEqual([s["machine_id"] for s in self.snapshot()["sources"]], ["m1", "m4"])
        self.assertIn("- 来源清单：2 个来源 → plan @ ", self.world.report())

    def test_a_collect_only_task_refreshes_too(self):
        outcome = self.world.run(only="collect", source_provider=self.provider("judged-m2"))

        self.assertEqual(outcome.exit_code, 0, outcome.failures)
        self.assertEqual([s["machine_id"] for s in self.snapshot()["sources"]], ["m2"])

    def test_opening_the_exchange_window_does_not_refresh(self):
        api = exchange.Api(self.world.cfg())

        state = api.state()

        self.assertEqual(state["machine_id"], "m4")
        self.assertIsNone(self.snapshot(), "开窗口不刷新来源清单（只有 run_once 才刷）")

    def test_a_held_source_list_asks_for_a_look(self):
        first = self.world.run(source_provider=self.provider("judged-m1", "judged-m2"))
        self.assertEqual(first.exit_code, 0, first.failures)

        second = self.world.run(source_provider=self.provider("judged-m1"))

        self.assertEqual(second.exit_code, 1, "来源减少：有需要人看一眼的")
        self.assertIn("保留了上一版有效快照", self.world.report())
        self.assertEqual([s["machine_id"] for s in self.snapshot()["sources"]], ["m1", "m2"])

    def test_a_refresh_failure_is_a_hard_failure(self):
        outcome = self.world.run(source_provider=FakeProvider(error="连接超时"))

        self.assertEqual(outcome.exit_code, 2, "维护机没刷成 = 本机没做成事")
        report = self.world.report()
        self.assertIn("- 来源清单：没刷新（来源查询没成（连接超时）", report)
        self.assertIn("判断来源清单没刷新", todo_of(report))

    def test_a_non_maintainer_report_carries_no_source_row(self):
        world = ConsoleWorld(self)                  # m1：没配 owner，不是来源维护机
        outcome = world.run()

        self.assertEqual(outcome.exit_code, 0, outcome.failures)
        self.assertNotIn("来源清单", world.report(), "恒定事实不进周报：非维护机不记这行")
        self.assertIsNotNone(outcome.source_refresh.note)

    def test_a_maintainer_without_the_plan_clone_is_named_in_the_check(self):
        import shutil
        shutil.rmtree(self.world.root / "plan")

        outcome = self.world.run(source_provider=self.provider("judged-m1"))

        self.assertEqual(outcome.exit_code, 2, "维护机发不出清单 = 本机没做成事")
        self.assertIn("来源维护机", "\n".join(outcome.check.notes))
        self.assertIn("- 来源清单：没刷新", self.world.report())

    def test_a_confirmed_shrink_publishes_through_the_task(self):
        """确认出口真的通到刷新：同一件事，不加确认保留、加了确认按新的发。"""
        first = self.world.run(source_provider=self.provider("judged-m1", "judged-m2"))
        self.assertEqual(first.exit_code, 0, first.failures)

        second = self.world.run(source_provider=self.provider("judged-m1"),
                                confirm_shrink=True)

        self.assertEqual(second.exit_code, 0, second.failures)
        self.assertEqual([s["machine_id"] for s in self.snapshot()["sources"]], ["m1"])

    def test_the_cli_exposes_the_confirmation_flag(self):
        self.assertTrue(exchange.parse_args(["--confirm-source-shrink"]).confirm_source_shrink)
        self.assertFalse(exchange.parse_args([]).confirm_source_shrink)

    def test_the_snapshot_refresh_is_wired_only_into_the_exchange_task(self):
        """来源刷新只接在交换任务里：包内别的模块一个字都不引用它（结构护栏）。"""
        package = pathlib.Path(bestseller_monitor.__file__).parent
        users = sorted(path.name for path in package.glob("*.py")
                       if "judged_sources" in path.read_text(encoding="utf-8"))
        self.assertEqual(users, ["exchange.py"])


if __name__ == "__main__":
    unittest.main()
