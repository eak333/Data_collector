"""
main.py
=======
VALORANT Match Data Collector — エントリポイント。

使用例:
  # シリーズURLを指定してフルパイプラインを実行
  python main.py --url "https://rib.gg/series/12345"

  # DBパスを指定
  python main.py --url "https://rib.gg/series/12345" --db data/my_match.db

  # 詳細ログを有効化
  python main.py --url "https://rib.gg/series/12345" --verbose

  # 分析レポートのみ出力（スクレイピングなし）
  python main.py --report --db data/processed/valorant_matches.db

パイプライン全体の処理フロー:
  1. [scraper]   rib.gg からシリーズ・マッチ・ラウンド・Killイベントを収集
  2. [analyzer]  Kill Delta（キルペース）・ファーストブラッド・武器統計を計算
  3. [database]  SQLiteへの保存（matches/rounds/players/kill_eventsテーブル）
  4. [report]    分析結果をコンソールに出力
"""

import argparse
import logging
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# ロギング設定（最初に設定しないとモジュールログが拾えない）
# ---------------------------------------------------------------------------

def setup_logging(verbose: bool = False) -> None:
    """アプリケーション全体のロギングを設定します。"""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(sys.stdout),
        ],
    )
    # サードパーティライブラリのログを抑制
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("requests").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CLIアーギュメント定義
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    """コマンドライン引数をパースします。"""
    parser = argparse.ArgumentParser(
        prog="valorant-collector",
        description="VALORANT match data collector for rib.gg",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
例:
  python main.py --url "https://rib.gg/series/12345"
  python main.py --url "https://rib.gg/series/12345" --verbose
  python main.py --report --db data/processed/valorant_matches.db
        """,
    )

    parser.add_argument(
        "--url",
        type=str,
        help="rib.gg のシリーズURL (例: https://rib.gg/series/12345)",
    )
    parser.add_argument(
        "--db",
        type=str,
        default="data/processed/valorant_matches.db",
        help="SQLiteデータベースのファイルパス (デフォルト: data/processed/valorant_matches.db)",
    )
    parser.add_argument(
        "--cache-dir",
        type=str,
        default="data/raw",
        help="APIレスポンスのキャッシュディレクトリ (デフォルト: data/raw)",
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help="既存DBから分析レポートのみを出力（スクレイピングなし）",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="DEBUGレベルの詳細ログを有効化",
    )
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="キャッシュを無視して常にAPIを再取得",
    )

    return parser.parse_args()


# ---------------------------------------------------------------------------
# レポートのみ出力モード
# ---------------------------------------------------------------------------

def run_report_only(db_path: Path) -> None:
    """
    既存のSQLiteデータベースから分析レポートを出力します。

    Args:
        db_path: 既存のSQLiteファイルパス
    """
    from src.analyzer import compute_kill_pace_summary, compute_weapon_stats, print_analysis_report
    from src.database import get_summary, init_database

    if not db_path.exists():
        logger.error("DBファイルが見つかりません: %s", db_path)
        sys.exit(1)

    import pandas as pd

    engine = init_database(db_path)
    db_summary = get_summary(engine)
    logger.info("DB状態: %s", db_summary)

    # kill_eventsテーブルから読み込み
    try:
        kill_events_df = pd.read_sql("SELECT * FROM kill_events", engine)
        if kill_events_df.empty:
            logger.warning("kill_eventsテーブルが空です")
            return
    except Exception as e:
        logger.error("kill_events読み込みエラー: %s", e)
        return

    # 分析・レポート出力
    weapon_stats = compute_weapon_stats(kill_events_df)
    pace_summary = compute_kill_pace_summary(kill_events_df)

    print_analysis_report({
        "kill_events":       kill_events_df,
        "weapon_stats":      weapon_stats,
        "kill_pace_summary": pace_summary,
        "first_blood":       pd.DataFrame(),
    })


# ---------------------------------------------------------------------------
# フルパイプライン実行
# ---------------------------------------------------------------------------

def run_pipeline(
    series_url: str,
    db_path: Path,
    cache_dir: Path,
    force_refresh: bool = False,
) -> int:
    """
    スクレイピング → 分析 → DB保存 のフルパイプラインを実行します。

    Args:
        series_url: rib.gg のシリーズURL
        db_path: SQLiteファイルパス
        cache_dir: APIキャッシュディレクトリ
        force_refresh: Trueでキャッシュを無視

    Returns:
        終了コード（0: 成功, 1: エラー）
    """
    from src.analyzer import run_full_analysis, print_analysis_report
    from src.database import (
        get_summary,
        init_database,
        insert_kill_events,
        insert_match,
        insert_players,
        insert_rounds,
    )
    from src.scraper import VALORANTScraper

    logger.info("=" * 60)
    logger.info("VALORANT Match Data Collector 開始")
    logger.info("対象URL: %s", series_url)
    logger.info("DB パス: %s", db_path)
    logger.info("=" * 60)

    # ── Phase 1: スクレイピング ──
    logger.info("[Phase 1/3] データ収集を開始します...")
    scraper = VALORANTScraper(cache_dir=cache_dir, raw_dir=cache_dir)
    result = scraper.scrape_series(series_url)

    if result is None:
        logger.error("スクレイピングに失敗しました。処理を中止します")
        return 1

    logger.info(
        "収集完了: ラウンド=%d, Kill=%d, プレイヤー=%d",
        len(result.rounds_df),
        len(result.kill_events_df),
        len(result.players_df),
    )

    # ── Phase 2: 分析 ──
    logger.info("[Phase 2/3] Kill Pace分析を開始します...")
    if result.kill_events_df.empty:
        logger.warning("Killイベントが取得できませんでした。分析をスキップします")
        analysis = {
            "kill_events":       result.kill_events_df,
            "weapon_stats":      None,
            "kill_pace_summary": {},
            "first_blood":       None,
        }
    else:
        analysis = run_full_analysis(result.kill_events_df)
        print_analysis_report(analysis)

    # ── Phase 3: DB保存 ──
    logger.info("[Phase 3/3] データベースへの保存を開始します...")
    engine = init_database(db_path)

    try:
        # matchesテーブル
        insert_match(engine, result.series_meta)

        # playersテーブル
        insert_players(engine, result.players_df)

        # roundsテーブル（round_id付きのDFを返す）
        rounds_with_id = insert_rounds(engine, result.rounds_df)

        # kill_eventsテーブル
        if not analysis["kill_events"].empty:
            insert_kill_events(engine, analysis["kill_events"], rounds_with_id)

    except Exception as e:
        logger.error("DB保存中にエラーが発生しました: %s", e)
        return 1

    # 最終確認
    db_summary = get_summary(engine)
    logger.info("=" * 60)
    logger.info("処理完了! DB状態:")
    for table, count in db_summary.items():
        logger.info("  %-20s: %d 件", table, count)
    logger.info("DB保存先: %s", db_path.resolve())
    logger.info("=" * 60)

    return 0


# ---------------------------------------------------------------------------
# エントリポイント
# ---------------------------------------------------------------------------

def main() -> None:
    """メインエントリポイント。"""
    args = parse_args()
    setup_logging(verbose=args.verbose)

    db_path = Path(args.db)
    cache_dir = Path(args.cache_dir)

    # ── レポートのみモード ──
    if args.report:
        run_report_only(db_path)
        return

    # ── URLが必要なモード ──
    if not args.url:
        logger.error(
            "--url オプションが必要です。\n"
            "例: python main.py --url 'https://rib.gg/series/12345'"
        )
        sys.exit(1)

    exit_code = run_pipeline(
        series_url=args.url,
        db_path=db_path,
        cache_dir=cache_dir,
        force_refresh=args.no_cache,
    )
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
