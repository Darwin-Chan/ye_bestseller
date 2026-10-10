"""Select collection storage once, before any collection side effects.

Formal tools keep using the original Config.  Trial collection has a stable,
local-only namespace and does not impersonate a collector machine.
"""
import os
from pathlib import Path
import tempfile

from .config import Config, Shop, ROLE_GLOSS, is_merge_only, load_shops
from .db import Database, connect, cst_date
from . import rounds


class TrialUnavailable(ValueError):
    """No safe local trial can be started; never fall back to formal storage."""


class TrialRoleRefused(TrialUnavailable):
    """This machine's role does not have local trial: only a pure aggregator does."""


def _walk_error(exc: OSError) -> None:
    raise exc


def _overlaps(left: Path, right: Path) -> bool:
    left, right = left.resolve(), right.resolve()
    return left == right or left in right.parents or right in left.parents


def _protected_paths(cfg: Config, config_file: Path | None) -> list[Path]:
    paths = [cfg.data_dir, cfg.db_file, Path(str(cfg.db_file) + "-wal"),
             Path(str(cfg.db_file) + "-shm"), cfg.logs_dir, cfg.screenshot_dir,
             cfg.raw_page_dir, cfg.exchange_root, cfg.user_data_path, cfg.shop_csv]
    if config_file is not None:
        paths.append(config_file)
    if cfg.analysis_config is not None:
        paths.extend([cfg.analysis_config, cfg.analysis_config.with_name("config.toml")])
        if cfg.analysis_config.exists():
            # Read configuration only; never open/migrate analysis or judgment stores.
            from .analysis import AnalysisConfig
            analysis = AnalysisConfig.from_file(cfg.analysis_config)
            paths.extend([analysis.database, analysis.store, analysis.output])
            if analysis.matching is not None:
                paths.append(analysis.matching.cache)
    return paths


def for_collection(cfg: Config, *, local_trial: bool = False,
                   config_file: Path | None = None) -> Config:
    if not local_trial:
        return cfg
    # 试采是纯汇总机的补偿手段（ADR-0049 及其修订段）：角色判据在这里落下，
    # 界面与命令行都从这一处过，别在入口各判一次。
    if not is_merge_only(cfg):
        raise TrialRoleRefused(
            f"本机试采只在纯汇总机提供（本机角色：{ROLE_GLOSS.get(cfg.role, cfg.role)}）。")
    home = cfg.data_dir.parent / "local-trial"
    try:
        protected = _protected_paths(cfg, config_file)
        for path in protected:
            if _overlaps(home, path):
                raise TrialUnavailable(f"试采空间与正式目标重合：{path}")
        # Resolve junctions/symlinks throughout existing storage, including assets.
        # Hard-linked files cannot be proven isolated; refuse rather than modifying them.
        if home.exists():
            for directory, dirs, files in os.walk(home, followlinks=False, onerror=_walk_error):
                for name in [*dirs, *files]:
                    path = Path(directory) / name
                    if path.is_symlink() or path.is_junction() or not path.resolve().is_relative_to(home.resolve()):
                        raise TrialUnavailable(f"试采空间包含外部链接：{path}")
                    if path.is_file() and path.stat().st_nlink > 1:
                        raise TrialUnavailable(f"试采文件存在硬链接，无法保证隔离：{path}")
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        raise TrialUnavailable(str(exc)) from exc
    return cfg.replace(
        local_trial=True, plan_pages=None,
        data_dir=home / "data", db_file=home / "data/bestseller.db",
        logs_dir=home / "logs", screenshot_dir=home / "data/screenshots",
        raw_page_dir=home / "data/raw_pages",
    )


def prepare_trial_storage(cfg: Config) -> None:
    """Fail early on unusable trial directories, without touching formal targets."""
    for path in [cfg.data_dir, cfg.logs_dir, cfg.screenshot_dir, cfg.raw_page_dir]:
        path.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=path):
            pass


def trial_shops(cfg: Config, limit_keys: set[str] | None = None) -> list[Shop]:
    """Read only the local shop list and trial round scope; no plan preparation."""
    from .pipeline import round_shops

    configured = load_shops(cfg.shop_csv, allow_empty=True)
    enabled = [shop for shop in configured if shop.active]
    if limit_keys is not None:
        unknown = limit_keys - {shop.key for shop in enabled}
        if unknown or not limit_keys:
            raise TrialUnavailable("试采范围为空或包含未知/停用店铺：" + "、".join(sorted(unknown)))
        selected = [shop for shop in enabled if shop.key in limit_keys]
    else:
        selected = enabled
    conn = connect(cfg.db_file)
    try:
        db = Database(conn)
        active = rounds.active_round(db, cst_date())
        if active is not None and limit_keys is None:
            return round_shops(db, active.id, cfg)
        if not selected:
            raise TrialUnavailable("本机清单里没有有效店铺（active=1）。")
        db.upsert_shops(configured)
        return selected
    finally:
        conn.close()
