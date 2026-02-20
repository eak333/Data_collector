"""
src/analyzer.py
===============
Kill Pace（キルペース）分析エンジン。

このモジュールは src/scraper.py が収集した生データを受け取り、
以下の分析を行います:

  1. **Kill Delta（キルデルタ）計算**
     - 同一ラウンド内で前のKillから現在のKillまでの経過秒数
     - ラウンド先頭のKillは「First Blood Time」（ラウンド開始からの秒数）

  2. **First Blood Time（ファーストブラッド時間）**
     - 各ラウンドの最初のKillが発生するまでの時間

  3. **Weapon Performance（武器パフォーマンス）統計**
     - 武器ごとのKill数・平均Kill Delta・ヘッドショット率

  4. **Kill Pace サマリー**
     - マッチ全体のテンポ指標（平均・中央値・標準偏差）
"""

import logging
from typing import Any, Dict, Optional

import pandas as pd

# ---------------------------------------------------------------------------
# ロガー設定
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Kill Delta（キルデルタ）の計算
# ---------------------------------------------------------------------------

def compute_kill_delta(kill_events_df: pd.DataFrame) -> pd.DataFrame:
    """
    各Killイベントに対して `time_delta`（前Killからの経過秒数）を計算します。

    計算ロジック:
      - データを (match_id, round_number, timestamp) でソート
      - 同一 (match_id, round_number) グループ内で `timestamp.diff()` を計算
      - 各ラウンドの最初のKillは `time_delta = timestamp`（ラウンド開始からの秒数）

    Args:
        kill_events_df: scraper.py が返すKillイベントDataFrame

    Returns:
        `time_delta` 列が追加されたDataFrame（コピー）
    """
    if kill_events_df.empty:
        logger.warning("Killイベントが空です。Kill Delta計算をスキップします")
        return kill_events_df.copy()

    required_cols = {"match_id", "round_number", "timestamp"}
    missing = required_cols - set(kill_events_df.columns)
    if missing:
        raise ValueError(f"必須カラムが不足しています: {missing}")

    df = kill_events_df.copy()

    # timestamp を数値型に強制変換
    df["timestamp"] = pd.to_numeric(df["timestamp"], errors="coerce").fillna(0.0)

    # ソート: マッチ → ラウンド → タイムスタンプ順
    df = df.sort_values(
        ["match_id", "round_number", "timestamp"],
        ascending=[True, True, True]
    ).reset_index(drop=True)

    # グループ内でのdiff計算
    # 最初の行は前のKillがないので NaN → その行のtimestampで置き換える（First Blood Time）
    df["time_delta"] = df.groupby(
        ["match_id", "round_number"]
    )["timestamp"].diff()

    # ラウンド先頭のKill: NaN → timestamp そのものを使用（First Blood）
    first_kill_mask = df["time_delta"].isna()
    df.loc[first_kill_mask, "time_delta"] = df.loc[first_kill_mask, "timestamp"]

    # 負値が出た場合は0に補正（タイムスタンプ誤差対応）
    df["time_delta"] = df["time_delta"].clip(lower=0.0)

    logger.info(
        "Kill Delta計算完了: %d件 (平均: %.2f秒, 中央値: %.2f秒)",
        len(df),
        df["time_delta"].mean(),
        df["time_delta"].median(),
    )
    return df


# ---------------------------------------------------------------------------
# First Blood Time（ファーストブラッド時間）の抽出
# ---------------------------------------------------------------------------

def compute_first_blood_times(kill_events_df: pd.DataFrame) -> pd.DataFrame:
    """
    各ラウンドのFirst Blood Time（最初のKillまでの時間）を計算します。

    Args:
        kill_events_df: `time_delta` 列を含むKillイベントDataFrame
                        （compute_kill_delta() 後のデータを推奨）

    Returns:
        以下のカラムを持つDataFrame:
          - match_id
          - round_number
          - first_blood_time: ラウンド開始から最初のKillまでの秒数
          - first_blood_killer: 最初のKillerの名前
          - first_blood_weapon: 最初のKillに使われた武器
    """
    if kill_events_df.empty:
        logger.warning("Killイベントが空です")
        return pd.DataFrame(columns=[
            "match_id", "round_number",
            "first_blood_time", "first_blood_killer", "first_blood_weapon"
        ])

    df_sorted = kill_events_df.sort_values(
        ["match_id", "round_number", "timestamp"]
    )

    first_kills = df_sorted.groupby(
        ["match_id", "round_number"], sort=False
    ).first().reset_index()

    result = pd.DataFrame({
        "match_id":           first_kills["match_id"],
        "round_number":       first_kills["round_number"],
        "first_blood_time":   first_kills["timestamp"],
        "first_blood_killer": first_kills.get("killer_name", pd.Series(dtype=str)),
        "first_blood_weapon": first_kills.get("weapon_name", pd.Series(dtype=str)),
    })

    logger.info(
        "First Blood Time計算完了: %d ラウンド (平均: %.2f秒)",
        len(result),
        result["first_blood_time"].mean() if not result.empty else 0.0,
    )
    return result


# ---------------------------------------------------------------------------
# 武器パフォーマンス統計
# ---------------------------------------------------------------------------

def compute_weapon_stats(kill_events_df: pd.DataFrame) -> pd.DataFrame:
    """
    武器ごとのKill統計を集計します。

    集計項目:
      - kills: Kill数
      - avg_time_delta: 平均Kill Delta（秒）— 武器の「テンポへの寄与」指標
      - median_time_delta: 中央値Kill Delta
      - headshot_rate: ヘッドショット率（0.0〜1.0）
      - avg_timestamp: 平均Kill発生時刻（ラウンド内での発生タイミング）

    Args:
        kill_events_df: `time_delta` 列を含むKillイベントDataFrame

    Returns:
        武器別統計のDataFrame（Kill数降順でソート済み）
    """
    if kill_events_df.empty:
        logger.warning("Killイベントが空のため武器統計を計算できません")
        return pd.DataFrame(columns=[
            "weapon_name", "kills", "avg_time_delta",
            "median_time_delta", "headshot_rate", "avg_timestamp"
        ])

    if "weapon_name" not in kill_events_df.columns:
        raise ValueError("'weapon_name' カラムがありません")

    agg_dict: Dict[str, Any] = {
        "kills":              ("timestamp", "count"),
        "avg_time_delta":     ("time_delta", "mean"),
        "median_time_delta":  ("time_delta", "median"),
        "avg_timestamp":      ("timestamp", "mean"),
    }

    # ヘッドショット率（カラムが存在する場合のみ）
    if "is_headshot" in kill_events_df.columns:
        df = kill_events_df.copy()
        df["is_headshot_num"] = df["is_headshot"].astype(float)
        grouped = df.groupby("weapon_name").agg(
            kills=("timestamp", "count"),
            avg_time_delta=("time_delta", "mean") if "time_delta" in df.columns else ("timestamp", "count"),
            median_time_delta=("time_delta", "median") if "time_delta" in df.columns else ("timestamp", "count"),
            avg_timestamp=("timestamp", "mean"),
            headshot_rate=("is_headshot_num", "mean"),
        ).reset_index()
    else:
        df = kill_events_df.copy()
        if "time_delta" not in df.columns:
            df["time_delta"] = 0.0
        grouped = df.groupby("weapon_name").agg(
            kills=("timestamp", "count"),
            avg_time_delta=("time_delta", "mean"),
            median_time_delta=("time_delta", "median"),
            avg_timestamp=("timestamp", "mean"),
        ).reset_index()
        grouped["headshot_rate"] = None

    # Kill数降順でソート
    result = grouped.sort_values("kills", ascending=False).reset_index(drop=True)

    # 数値を小数点2桁に丸める
    for col in ["avg_time_delta", "median_time_delta", "avg_timestamp"]:
        if col in result.columns:
            result[col] = result[col].round(2)
    if "headshot_rate" in result.columns and result["headshot_rate"].notna().any():
        result["headshot_rate"] = result["headshot_rate"].round(4)

    logger.info("武器統計計算完了: %d種類の武器", len(result))
    return result


# ---------------------------------------------------------------------------
# Kill Paceサマリー
# ---------------------------------------------------------------------------

def compute_kill_pace_summary(kill_events_df: pd.DataFrame) -> Dict[str, float]:
    """
    マッチ全体のKill Pace（キルペース）サマリー統計を計算します。

    返す指標:
      - total_kills: 総Kill数
      - avg_time_delta: 平均Kill間隔（秒）
      - median_time_delta: 中央値Kill間隔
      - std_time_delta: 標準偏差Kill間隔
      - min_time_delta: 最小Kill間隔（最速のKill連続）
      - max_time_delta: 最大Kill間隔（最も間延びした場面）
      - avg_first_blood_time: 平均ファーストブラッド時間

    Args:
        kill_events_df: `time_delta` 列を含むKillイベントDataFrame

    Returns:
        統計値のdict
    """
    if kill_events_df.empty:
        return {
            "total_kills": 0,
            "avg_time_delta": 0.0,
            "median_time_delta": 0.0,
            "std_time_delta": 0.0,
            "min_time_delta": 0.0,
            "max_time_delta": 0.0,
            "avg_first_blood_time": 0.0,
        }

    td = kill_events_df.get("time_delta", pd.Series(dtype=float)).dropna()

    # ファーストブラッド（各ラウンドの最初のKill）だけを抽出
    fb_times = kill_events_df.sort_values(
        ["match_id", "round_number", "timestamp"]
    ).groupby(["match_id", "round_number"])["timestamp"].first()

    summary = {
        "total_kills":          len(kill_events_df),
        "avg_time_delta":       round(float(td.mean()), 2) if len(td) > 0 else 0.0,
        "median_time_delta":    round(float(td.median()), 2) if len(td) > 0 else 0.0,
        "std_time_delta":       round(float(td.std()), 2) if len(td) > 1 else 0.0,
        "min_time_delta":       round(float(td.min()), 2) if len(td) > 0 else 0.0,
        "max_time_delta":       round(float(td.max()), 2) if len(td) > 0 else 0.0,
        "avg_first_blood_time": round(float(fb_times.mean()), 2) if len(fb_times) > 0 else 0.0,
    }

    logger.info(
        "Kill Paceサマリー: 総Kill=%d, 平均Delta=%.2f秒, 平均FB=%.2f秒",
        summary["total_kills"],
        summary["avg_time_delta"],
        summary["avg_first_blood_time"],
    )
    return summary


# ---------------------------------------------------------------------------
# 全分析をまとめて実行するヘルパー
# ---------------------------------------------------------------------------

def run_full_analysis(kill_events_df: pd.DataFrame) -> Dict[str, Any]:
    """
    Kill Deltaの計算から各種統計まで、すべての分析を順番に実行します。

    Args:
        kill_events_df: scraper.py が返す生のKillイベントDataFrame

    Returns:
        以下のキーを持つdict:
          - "kill_events":      Kill Deltaが付加されたDataFrame
          - "first_blood":      ファーストブラッド時間DataFrame
          - "weapon_stats":     武器パフォーマンス統計DataFrame
          - "kill_pace_summary": Kill Paceサマリー dict
    """
    logger.info("全分析を開始します...")

    # Step 1: Kill Delta計算
    kill_events_with_delta = compute_kill_delta(kill_events_df)

    # Step 2: First Blood時間
    first_blood_df = compute_first_blood_times(kill_events_with_delta)

    # Step 3: 武器統計
    weapon_stats_df = compute_weapon_stats(kill_events_with_delta)

    # Step 4: Kill Paceサマリー
    pace_summary = compute_kill_pace_summary(kill_events_with_delta)

    logger.info("全分析完了")
    return {
        "kill_events":       kill_events_with_delta,
        "first_blood":       first_blood_df,
        "weapon_stats":      weapon_stats_df,
        "kill_pace_summary": pace_summary,
    }


# ---------------------------------------------------------------------------
# レポート出力（デバッグ・確認用）
# ---------------------------------------------------------------------------

def print_analysis_report(analysis_result: Dict[str, Any]) -> None:
    """
    分析結果をコンソールに出力します（確認・デバッグ用）。

    Args:
        analysis_result: run_full_analysis() の返り値
    """
    summary = analysis_result.get("kill_pace_summary", {})
    weapon_stats = analysis_result.get("weapon_stats", pd.DataFrame())
    first_blood = analysis_result.get("first_blood", pd.DataFrame())

    print("\n" + "="*60)
    print("  VALORANT Kill Pace 分析レポート")
    print("="*60)

    print("\n【Kill Pace サマリー】")
    print(f"  総Kill数:             {summary.get('total_kills', 0):,}")
    print(f"  平均Kill間隔:          {summary.get('avg_time_delta', 0):.2f} 秒")
    print(f"  中央値Kill間隔:        {summary.get('median_time_delta', 0):.2f} 秒")
    print(f"  Kill間隔 標準偏差:     {summary.get('std_time_delta', 0):.2f} 秒")
    print(f"  最小Kill間隔:          {summary.get('min_time_delta', 0):.2f} 秒")
    print(f"  最大Kill間隔:          {summary.get('max_time_delta', 0):.2f} 秒")
    print(f"  平均ファーストブラッド: {summary.get('avg_first_blood_time', 0):.2f} 秒")

    if not weapon_stats.empty:
        print("\n【武器パフォーマンス Top 10】")
        top10 = weapon_stats.head(10)[
            ["weapon_name", "kills", "avg_time_delta", "headshot_rate"]
        ].to_string(index=False)
        print(top10)

    if not first_blood.empty:
        print("\n【ファーストブラッド 先頭10ラウンド】")
        print(first_blood.head(10)[
            ["match_id", "round_number", "first_blood_time",
             "first_blood_killer", "first_blood_weapon"]
        ].to_string(index=False))

    print("\n" + "="*60)
