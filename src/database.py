"""
src/database.py
===============
SQLiteデータベースの初期化・スキーマ定義・データ挿入モジュール。

テーブル構成:
  - matches:     シリーズメタデータ（大会名・チーム・マップBan/Pick）
  - rounds:      ラウンド結果（勝利チーム・終了種別・ラウンド時間）
  - players:     プレイヤーメタデータ（名前・チーム・エージェント）
  - kill_events: Killイベント詳細（タイムスタンプ・武器・Kill Delta）

設計方針:
  - SQLAlchemy Core を使用（ORM不使用 — シンプルさを優先）
  - pandas.DataFrame を経由して一括挿入（chunksize=500）
  - INSERT OR REPLACE でべき等性を確保（再実行しても重複しない）
"""

import json
import logging
import sqlite3
from pathlib import Path
from typing import Any, Optional

import pandas as pd
from sqlalchemy import (
    Column,
    Float,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    text,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

# ---------------------------------------------------------------------------
# ロガー設定
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# デフォルトのDBパス
# ---------------------------------------------------------------------------
DEFAULT_DB_PATH = Path("data/processed/valorant_matches.db")

# ---------------------------------------------------------------------------
# スキーマ定義
# ---------------------------------------------------------------------------

metadata = MetaData()

#
# matches テーブル: シリーズ（対戦セット）のメタデータ
#
matches_table = Table(
    "matches",
    metadata,
    Column("id",               String,  primary_key=True, comment="シリーズID"),
    Column("tournament_name",  String,  nullable=True,    comment="大会名"),
    Column("date",             String,  nullable=True,    comment="試合日 (ISO8601)"),
    Column("team_a",           String,  nullable=True,    comment="チームA名"),
    Column("team_b",           String,  nullable=True,    comment="チームB名"),
    Column("map_picks_json",   Text,    nullable=True,    comment="マップBan/Pick履歴 (JSON文字列)"),
)

#
# rounds テーブル: ラウンド結果
#
rounds_table = Table(
    "rounds",
    metadata,
    Column("id",               Integer, primary_key=True, autoincrement=True, comment="ラウンドDB ID"),
    Column("match_id",         String,  nullable=False,   comment="matchesテーブル参照"),
    Column("map_name",         String,  nullable=True,    comment="マップ名"),
    Column("round_number",     Integer, nullable=False,   comment="ラウンド番号 (1始まり)"),
    Column("winning_team",     String,  nullable=True,    comment="勝利チーム名"),
    Column("end_type",         String,  nullable=True,    comment="終了種別 (Elimination/Defuse/Time等)"),
    Column("duration_seconds", Float,   nullable=True,    comment="ラウンド時間 (秒)"),
)

#
# players テーブル: プレイヤーメタデータ
#
players_table = Table(
    "players",
    metadata,
    Column("id",    String, primary_key=True, comment="プレイヤーID"),
    Column("name",  String, nullable=True,    comment="プレイヤー名 (IGN)"),
    Column("team",  String, nullable=True,    comment="所属チーム名"),
    Column("agent", String, nullable=True,    comment="使用エージェント名"),
)

#
# kill_events テーブル: Killイベント詳細（メインテーブル）
#
kill_events_table = Table(
    "kill_events",
    metadata,
    Column("id",          Integer, primary_key=True, autoincrement=True, comment="イベントDB ID"),
    Column("round_id",    Integer, nullable=True,    comment="roundsテーブル参照 (FK)"),
    Column("match_id",    String,  nullable=True,    comment="matchesテーブル参照 (FK)"),
    Column("round_number",Integer, nullable=True,    comment="ラウンド番号"),
    Column("timestamp",   Float,   nullable=True,    comment="ラウンド開始からの経過秒数"),
    Column("killer_id",   String,  nullable=True,    comment="キラーのプレイヤーID"),
    Column("killer_name", String,  nullable=True,    comment="キラーの名前"),
    Column("victim_id",   String,  nullable=True,    comment="ビクティムのプレイヤーID"),
    Column("victim_name", String,  nullable=True,    comment="ビクティムの名前"),
    Column("weapon_name", String,  nullable=True,    comment="武器名 (正規化済み)"),
    Column("time_delta",  Float,   nullable=True,    comment="前Killからの経過秒数 (Kill Pace)"),
    Column("location_x",  Float,   nullable=True,    comment="X座標"),
    Column("location_y",  Float,   nullable=True,    comment="Y座標"),
    Column("is_headshot", Integer, nullable=True,    comment="ヘッドショット (1=Yes, 0=No)"),
)


# ---------------------------------------------------------------------------
# データベース初期化
# ---------------------------------------------------------------------------

def init_database(db_path: Optional[Path] = None) -> Engine:
    """
    SQLiteデータベースを初期化し、テーブルを作成します。

    既存テーブルは変更しません（CREATE TABLE IF NOT EXISTS 相当）。
    べき等性があるため、何度実行しても安全です。

    Args:
        db_path: SQLiteファイルのパス。Noneの場合はデフォルトパスを使用

    Returns:
        初期化済みのSQLAlchemyエンジン
    """
    path = db_path or DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)

    engine = create_engine(
        f"sqlite:///{path}",
        echo=False,
        connect_args={
            "check_same_thread": False,
            "timeout": 30,
        },
    )

    # テーブル作成（存在しない場合のみ）
    metadata.create_all(engine)
    logger.info("データベースを初期化しました: %s", path)

    # WALモードを有効化（並行アクセスのパフォーマンス向上）
    with engine.connect() as conn:
        conn.execute(text("PRAGMA journal_mode=WAL"))
        conn.execute(text("PRAGMA foreign_keys=ON"))

    return engine


# ---------------------------------------------------------------------------
# データ挿入関数
# ---------------------------------------------------------------------------

def insert_match(engine: Engine, series_meta: Any) -> None:
    """
    シリーズメタデータをmatchesテーブルに挿入します。

    Args:
        engine: SQLAlchemyエンジン
        series_meta: scraper.SeriesMeta インスタンス
    """
    row = {
        "id":              series_meta.series_id,
        "tournament_name": series_meta.tournament_name,
        "date":            series_meta.date,
        "team_a":          series_meta.team_a,
        "team_b":          series_meta.team_b,
        "map_picks_json":  json.dumps(series_meta.map_picks, ensure_ascii=False),
    }

    df = pd.DataFrame([row])
    _upsert_dataframe(engine, df, "matches", unique_key="id")
    logger.info("matchesテーブルに挿入: series_id=%s", series_meta.series_id)


def insert_players(engine: Engine, players_df: pd.DataFrame) -> None:
    """
    プレイヤー情報をplayersテーブルに挿入します。

    Args:
        engine: SQLAlchemyエンジン
        players_df: scraper.py が返すプレイヤーDataFrame
    """
    if players_df.empty:
        logger.warning("プレイヤーデータが空です。挿入をスキップします")
        return

    _upsert_dataframe(engine, players_df, "players", unique_key="id")
    logger.info("playersテーブルに %d件挿入しました", len(players_df))


def insert_rounds(engine: Engine, rounds_df: pd.DataFrame) -> pd.DataFrame:
    """
    ラウンドデータをroundsテーブルに挿入し、自動生成されたIDを付加して返します。

    Returns:
        round_id 列が付加されたDataFrame（kill_events挿入時のFK参照に使用）
    """
    if rounds_df.empty:
        logger.warning("ラウンドデータが空です。挿入をスキップします")
        return rounds_df.copy()

    df = rounds_df.copy()

    try:
        with engine.begin() as conn:
            for _, row in df.iterrows():
                conn.execute(
                    rounds_table.insert().values(
                        match_id=str(row.get("match_id", "")),
                        map_name=str(row.get("map_name", "")),
                        round_number=int(row.get("round_number", 0)),
                        winning_team=str(row.get("winning_team", "")),
                        end_type=str(row.get("end_type", "")),
                        duration_seconds=float(row.get("duration_seconds", 0.0)),
                    )
                )
        logger.info("roundsテーブルに %d件挿入しました", len(df))

    except SQLAlchemyError as e:
        logger.error("rounds挿入エラー: %s", e)
        raise

    # 挿入後にIDを取得してDataFrameに紐付け
    try:
        rounds_with_ids = pd.read_sql(
            "SELECT id AS round_id, match_id, round_number FROM rounds",
            engine
        )
        df = df.merge(
            rounds_with_ids,
            on=["match_id", "round_number"],
            how="left"
        )
    except Exception as e:
        logger.warning("round_IDの取得に失敗: %s — FKなしで続行します", e)
        df["round_id"] = None

    return df


def insert_kill_events(
    engine: Engine,
    kill_events_df: pd.DataFrame,
    rounds_df: Optional[pd.DataFrame] = None,
) -> None:
    """
    KillイベントをKill_eventsテーブルに一括挿入します。

    round_id の解決:
      rounds_df が提供された場合、(match_id, round_number) をキーに
      round_id を結合してFKを設定します。

    Args:
        engine: SQLAlchemyエンジン
        kill_events_df: analyzer.py が返すKill Deltaが付加されたDataFrame
        rounds_df: round_id 列を含むラウンドDataFrame（任意）
    """
    if kill_events_df.empty:
        logger.warning("Killイベントデータが空です。挿入をスキップします")
        return

    df = kill_events_df.copy()

    # round_id の解決
    if rounds_df is not None and "round_id" in rounds_df.columns:
        round_id_map = rounds_df[["match_id", "round_number", "round_id"]].drop_duplicates()
        df = df.merge(round_id_map, on=["match_id", "round_number"], how="left")
    else:
        df["round_id"] = None

    # is_headshot を int に変換
    if "is_headshot" in df.columns:
        df["is_headshot"] = df["is_headshot"].astype(int)

    # 挿入に必要なカラムのみ選択
    insert_cols = [
        "round_id", "match_id", "round_number", "timestamp",
        "killer_id", "killer_name", "victim_id", "victim_name",
        "weapon_name", "time_delta", "location_x", "location_y", "is_headshot",
    ]
    # 存在しないカラムはNaNで補完
    for col in insert_cols:
        if col not in df.columns:
            df[col] = None

    df_insert = df[insert_cols]

    try:
        # chunksize=500 で一括挿入
        df_insert.to_sql(
            "kill_events",
            engine,
            if_exists="append",
            index=False,
            chunksize=500,
            method="multi",
        )
        logger.info("kill_eventsテーブルに %d件挿入しました", len(df_insert))

    except SQLAlchemyError as e:
        logger.error("kill_events挿入エラー: %s", e)
        raise


# ---------------------------------------------------------------------------
# べき等的アップサート（INSERT OR REPLACE）ヘルパー
# ---------------------------------------------------------------------------

def _upsert_dataframe(
    engine: Engine,
    df: pd.DataFrame,
    table_name: str,
    unique_key: str,
) -> None:
    """
    DataFrameをINSERT OR REPLACEでテーブルに挿入します。

    SQLAlchemy の to_sql は REPLACE INTO を直接サポートしないため、
    sqlite3 の低レベルAPIを使用してべき等性を実現します。

    Args:
        engine: SQLAlchemyエンジン
        df: 挿入するDataFrame
        table_name: ターゲットテーブル名
        unique_key: 重複判定に使うカラム名（PRIMARY KEY）
    """
    if df.empty:
        return

    conn_raw = engine.raw_connection()
    try:
        cursor = conn_raw.cursor()

        # カラムリストと値プレースホルダーを動的生成
        cols = list(df.columns)
        placeholders = ", ".join(["?" for _ in cols])
        col_names = ", ".join(cols)
        sql = f"INSERT OR REPLACE INTO {table_name} ({col_names}) VALUES ({placeholders})"

        # NaN → None 変換（SQLiteはNULLとして記録）
        records = [
            tuple(None if pd.isna(v) else v for v in row)
            for row in df.itertuples(index=False, name=None)
        ]

        cursor.executemany(sql, records)
        conn_raw.commit()
        logger.debug(
            "INSERT OR REPLACE: %s テーブルに %d件", table_name, len(records)
        )
    except sqlite3.Error as e:
        conn_raw.rollback()
        logger.error("DB挿入エラー (%s): %s", table_name, e)
        raise
    finally:
        conn_raw.close()


# ---------------------------------------------------------------------------
# データ確認クエリ（デバッグ用）
# ---------------------------------------------------------------------------

def get_summary(engine: Engine) -> dict:
    """
    各テーブルのレコード数を返します（動作確認用）。

    Args:
        engine: SQLAlchemyエンジン

    Returns:
        {"matches": int, "rounds": int, "players": int, "kill_events": int}
    """
    summary = {}
    with engine.connect() as conn:
        for table in ["matches", "rounds", "players", "kill_events"]:
            try:
                result = conn.execute(text(f"SELECT COUNT(*) FROM {table}"))
                summary[table] = result.scalar()
            except Exception:
                summary[table] = -1  # テーブルが存在しない場合

    logger.info("DB サマリー: %s", summary)
    return summary
