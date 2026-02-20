"""
src/scraper.py
==============
VALORANTマッチデータのスクレイパー。

rib.gg のシリーズURLを起点に、以下の順序でデータを収集します:
  1. シリーズメタデータ（大会名・チーム・マップBan/Pick）
  2. 各マップ（マッチ）の詳細データ（ラウンド・Killイベント）
  3. 収集した生データを data/raw/ にJSONとして保存

設計方針:
  - `skills/rib_api.py` のAPIクライアントとデータ抽出関数を利用
  - 全ラウンドを反復処理してKillイベントを収集
  - 処理済みデータは pandas.DataFrame として返す（DB挿入の前段）
"""

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

from skills.rib_api import (
    RibGGClient,
    extract_kill_events,
    extract_map_picks,
    extract_roster,
    fetch_match_data,
    fetch_series_data,
    parse_series_id_from_url,
)

# ---------------------------------------------------------------------------
# ロガー設定
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# データクラス定義
# ---------------------------------------------------------------------------

@dataclass
class SeriesMeta:
    """シリーズ（対戦セット）のメタデータ。"""
    series_id: str
    tournament_name: str
    date: str
    team_a: str
    team_b: str
    map_picks: List[Dict[str, Any]] = field(default_factory=list)
    match_ids: List[str] = field(default_factory=list)


@dataclass
class RoundResult:
    """ラウンドの結果データ。"""
    match_id: str
    map_name: str
    round_number: int
    winning_team: str
    end_type: str
    duration_seconds: float


@dataclass
class ScraperResult:
    """スクレイパーが返す全体の結果データ。"""
    series_meta: SeriesMeta
    players_df: pd.DataFrame
    rounds_df: pd.DataFrame
    kill_events_df: pd.DataFrame


# ---------------------------------------------------------------------------
# シリーズメタデータのパース
# ---------------------------------------------------------------------------

def _parse_series_meta(series_data: Dict[str, Any], series_id: str) -> SeriesMeta:
    """
    APIレスポンスからSeriesMetaデータクラスを構築します。

    Args:
        series_data: fetch_series_data() の返り値
        series_id: シリーズID文字列

    Returns:
        SeriesMeta インスタンス
    """
    # 大会名の取得（複数キー候補を試みる）
    tournament_name = (
        series_data.get("tournament", {}).get("name")
        or series_data.get("tournamentName")
        or series_data.get("event", {}).get("name")
        or "Unknown Tournament"
    )

    # 日付の取得
    date = (
        series_data.get("date")
        or series_data.get("createdAt")
        or series_data.get("startedAt")
        or ""
    )

    # チーム名の取得
    teams = series_data.get("teams") or []
    if len(teams) >= 2:
        team_a = teams[0].get("name", "Team A")
        team_b = teams[1].get("name", "Team B")
    else:
        team_a = (
            series_data.get("teamA", {}).get("name")
            or series_data.get("team1", {}).get("name")
            or "Team A"
        )
        team_b = (
            series_data.get("teamB", {}).get("name")
            or series_data.get("team2", {}).get("name")
            or "Team B"
        )

    # 含まれるマッチIDのリスト
    matches_raw = (
        series_data.get("matches")
        or series_data.get("games")
        or series_data.get("maps")
        or []
    )
    match_ids = [
        str(m.get("id") or m.get("matchId") or m.get("gameId") or "")
        for m in matches_raw
        if m.get("id") or m.get("matchId") or m.get("gameId")
    ]

    map_picks = extract_map_picks(series_data)

    logger.info(
        "シリーズメタデータ解析完了: %s vs %s (%s) — マッチ数: %d",
        team_a, team_b, tournament_name, len(match_ids)
    )

    return SeriesMeta(
        series_id=series_id,
        tournament_name=tournament_name,
        date=date,
        team_a=team_a,
        team_b=team_b,
        map_picks=map_picks,
        match_ids=match_ids,
    )


# ---------------------------------------------------------------------------
# ラウンドデータのパース
# ---------------------------------------------------------------------------

def _parse_rounds(
    match_data: Dict[str, Any],
    match_id: str,
) -> List[RoundResult]:
    """
    マッチデータからRoundResultリストを構築します。

    Args:
        match_data: fetch_match_data() の返り値
        match_id: マッチID文字列

    Returns:
        RoundResult のリスト
    """
    map_name = (
        match_data.get("map", {}).get("name")
        or match_data.get("mapName")
        or match_data.get("map")
        or "Unknown Map"
    )
    # mapがstr型の場合はそのまま使う
    if isinstance(map_name, dict):
        map_name = map_name.get("name", "Unknown Map")

    rounds_raw = (
        match_data.get("rounds")
        or match_data.get("data", {}).get("rounds")
        or []
    )

    results: List[RoundResult] = []
    for round_info in rounds_raw:
        r_num = round_info.get("roundNumber") or round_info.get("round_number") or 0

        winning_team = (
            round_info.get("winner")
            or round_info.get("winningTeam")
            or round_info.get("winningSide")
            or "Unknown"
        )

        end_type = (
            round_info.get("endType")
            or round_info.get("roundEndType")
            or round_info.get("outcome")
            or "Unknown"
        )

        # ラウンド時間（秒）を取得
        duration_ms = (
            round_info.get("durationMillis")
            or round_info.get("duration")
            or 0
        )
        # ミリ秒判定
        duration_sec: float = (
            duration_ms / 1000.0
            if isinstance(duration_ms, (int, float)) and duration_ms > 1000
            else float(duration_ms)
        )

        results.append(RoundResult(
            match_id=match_id,
            map_name=map_name,
            round_number=r_num,
            winning_team=str(winning_team),
            end_type=str(end_type),
            duration_seconds=duration_sec,
        ))

    logger.debug(
        "マッチ %s のラウンド %d件を解析しました (マップ: %s)",
        match_id, len(results), map_name
    )
    return results


# ---------------------------------------------------------------------------
# メインスクレイパークラス
# ---------------------------------------------------------------------------

class VALORANTScraper:
    """
    rib.gg からVALORANTマッチデータを収集するスクレイパー。

    使用例:
        scraper = VALORANTScraper(cache_dir=Path("data/raw"))
        result = scraper.scrape_series("https://rib.gg/series/12345")
        print(result.kill_events_df.head())
    """

    def __init__(
        self,
        cache_dir: Optional[Path] = None,
        raw_dir: Optional[Path] = None,
    ) -> None:
        """
        Args:
            cache_dir: HTTPレスポンスキャッシュディレクトリ
            raw_dir: 生JSONを保存するディレクトリ
        """
        self.cache_dir = cache_dir or Path("data/raw")
        self.raw_dir = raw_dir or Path("data/raw")
        self.raw_dir.mkdir(parents=True, exist_ok=True)

        self.client = RibGGClient(cache_dir=self.cache_dir)

    def _save_raw_json(self, data: Any, filename: str) -> None:
        """生データをJSONファイルとして保存します。"""
        path = self.raw_dir / filename
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        logger.debug("生データを保存しました: %s", path)

    def scrape_series(self, series_url: str) -> Optional[ScraperResult]:
        """
        指定したシリーズURLからすべてのデータを収集します。

        処理フロー:
          1. URLからシリーズIDを抽出
          2. シリーズメタデータ取得（チーム・大会名・マップBan/Pick）
          3. ロースター（プレイヤー情報）を取得
          4. 各マップ（マッチ）についてラウンドとKillイベントを収集
          5. pandasDataFrameに変換して返す

        Args:
            series_url: rib.gg のシリーズURL

        Returns:
            ScraperResult。失敗時はNone
        """
        logger.info("スクレイピング開始: %s", series_url)

        # ── Step 1: シリーズIDの抽出 ──
        series_id = parse_series_id_from_url(series_url)
        if not series_id:
            logger.error("シリーズIDの抽出に失敗しました")
            return None

        # ── Step 2: シリーズメタデータ取得 ──
        series_data = fetch_series_data(self.client, series_id)
        if not series_data:
            logger.error("シリーズデータの取得に失敗しました")
            return None

        self._save_raw_json(series_data, f"series_{series_id}.json")
        series_meta = _parse_series_meta(series_data, series_id)

        # ── Step 3: ロースター取得 ──
        team_a_players, team_b_players = extract_roster(series_data)
        all_players = team_a_players + team_b_players

        players_df = pd.DataFrame(all_players) if all_players else pd.DataFrame(
            columns=["id", "name", "agent", "team"]
        )
        logger.info("プレイヤー合計: %d名", len(players_df))

        # ── Step 4: 各マッチのデータ収集 ──
        all_rounds: List[Dict[str, Any]] = []
        all_kills: List[Dict[str, Any]] = []

        for match_id in series_meta.match_ids:
            logger.info("マッチデータ取得中: match_id=%s", match_id)

            match_data = fetch_match_data(self.client, match_id)
            if not match_data:
                logger.warning("マッチ %s のデータ取得に失敗。スキップします", match_id)
                continue

            self._save_raw_json(match_data, f"match_{match_id}.json")

            # ラウンドデータのパース
            rounds = _parse_rounds(match_data, match_id)
            for r in rounds:
                all_rounds.append({
                    "match_id":        r.match_id,
                    "map_name":        r.map_name,
                    "round_number":    r.round_number,
                    "winning_team":    r.winning_team,
                    "end_type":        r.end_type,
                    "duration_seconds": r.duration_seconds,
                })

            # Killイベントの抽出
            kills = extract_kill_events(match_data)
            for k in kills:
                k["match_id"] = match_id
                all_kills.append(k)

            logger.info(
                "マッチ %s: ラウンド %d件, Kill %d件",
                match_id, len(rounds), len(kills)
            )

        # ── Step 5: DataFrame変換 ──
        rounds_df = pd.DataFrame(all_rounds) if all_rounds else pd.DataFrame(
            columns=["match_id", "map_name", "round_number",
                     "winning_team", "end_type", "duration_seconds"]
        )

        kill_events_df = pd.DataFrame(all_kills) if all_kills else pd.DataFrame(
            columns=["match_id", "round_number", "timestamp",
                     "killer_id", "killer_name", "victim_id", "victim_name",
                     "weapon_name", "location_x", "location_y", "is_headshot"]
        )

        logger.info(
            "スクレイピング完了: ラウンド=%d, Kill=%d",
            len(rounds_df), len(kill_events_df)
        )

        return ScraperResult(
            series_meta=series_meta,
            players_df=players_df,
            rounds_df=rounds_df,
            kill_events_df=kill_events_df,
        )
