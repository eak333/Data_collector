"""
skills/rib_api.py
=================
rib.gg 専用APIクライアント。

主要機能:
  1. rib.gg の内部APIまたは __NEXT_DATA__ からJSON を取得
  2. 武器ID → 武器名へのマッピング (WEAPON_MAP)
  3. Killイベント（タイムスタンプ・武器・座標）の抽出

rib.gg は Next.js 製アプリケーションのため、以下の優先順位でデータ取得を試みます:
  Priority 1: 内部REST APIエンドポイント (/api/series/{id}, /api/match/{id})
  Priority 2: ページHTML内の <script id="__NEXT_DATA__"> JSONブロブのパース
"""

import json
import logging
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# ロガー設定
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------
BASE_URL = "https://www.rib.gg"
API_BASE = "https://www.rib.gg/api"

# HTTPセッション設定
DEFAULT_HEADERS: Dict[str, str] = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/html, */*",
    "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8",
    "Referer": "https://www.rib.gg/",
}

REQUEST_TIMEOUT: int = 30       # 秒
MAX_RETRIES: int = 3            # 最大リトライ回数
RETRY_BACKOFF: float = 2.0      # 指数バックオフの基数（秒）
RATE_LIMIT_DELAY: float = 1.5   # リクエスト間の待機時間（秒）

# ---------------------------------------------------------------------------
# 武器IDマッピング辞書 (WEAPON_MAP)
# rib.gg が返す武器UUID → 表示名
# VALORANT公式の武器IDに基づく
# ---------------------------------------------------------------------------
WEAPON_MAP: Dict[str, str] = {
    # ───── サイドアーム（Sidearms）─────
    "Classic":          "Classic",
    "Shorty":           "Shorty",
    "Frenzy":           "Frenzy",
    "Ghost":            "Ghost",
    "Sheriff":          "Sheriff",

    # ───── SMG ─────
    "Stinger":          "Stinger",
    "Spectre":          "Spectre",

    # ───── ショットガン（Shotguns）─────
    "Bucky":            "Bucky",
    "Judge":            "Judge",

    # ───── ライフル（Rifles）─────
    "Bulldog":          "Bulldog",
    "Guardian":         "Guardian",
    "Phantom":          "Phantom",
    "Vandal":           "Vandal",

    # ───── スナイパー（Snipers）─────
    "Marshal":          "Marshal",
    "Outlaw":           "Outlaw",
    "Operator":         "Operator",

    # ───── マシンガン（Machine Guns）─────
    "Ares":             "Ares",
    "Odin":             "Odin",

    # ───── 近接（Melee）─────
    "Melee":            "Melee",

    # ───── アビリティ / その他 ─────
    "Ability1":         "Ability (Q)",
    "Ability2":         "Ability (E)",
    "GrenadeAbility":   "Ability (Grenade)",
    "Ultimate":         "Ultimate",
    "Primary":          "Primary Ability",

    # ───── rib.gg 固有の数値IDマッピング（フォールバック用）─────
    # rib.gg の内部APIでは整数IDが使われる場合があります
    "0":  "Classic",
    "1":  "Shorty",
    "2":  "Frenzy",
    "3":  "Ghost",
    "4":  "Sheriff",
    "5":  "Stinger",
    "6":  "Spectre",
    "7":  "Bucky",
    "8":  "Judge",
    "9":  "Bulldog",
    "10": "Guardian",
    "11": "Phantom",
    "12": "Vandal",
    "13": "Marshal",
    "14": "Outlaw",
    "15": "Operator",
    "16": "Ares",
    "17": "Odin",
    "18": "Melee",
}


def resolve_weapon_name(weapon_raw: Any) -> str:
    """
    武器IDまたは武器名文字列を表示名に変換します。

    rib.gg は武器を以下の形式で返す場合があります:
      - 整数ID (例: 11 → "Phantom")
      - 文字列名 (例: "Phantom")
      - None / 不明

    Args:
        weapon_raw: APIから取得した武器値（int, str, None）

    Returns:
        str: 正規化された武器名。不明な場合は "Unknown"
    """
    if weapon_raw is None:
        return "Unknown"

    key = str(weapon_raw).strip()

    # 直接マッチ
    if key in WEAPON_MAP:
        return WEAPON_MAP[key]

    # 大文字小文字を無視したマッチ
    for map_key, map_val in WEAPON_MAP.items():
        if map_key.lower() == key.lower():
            return map_val

    logger.warning("未知の武器ID/名: %s — 'Unknown' として記録します", weapon_raw)
    return f"Unknown({key})"


# ---------------------------------------------------------------------------
# HTTPセッション管理
# ---------------------------------------------------------------------------

class RibGGClient:
    """
    rib.gg 専用HTTPクライアント。

    セッション管理・レート制限・リトライロジックをカプセル化します。
    """

    def __init__(self, cache_dir: Optional[Path] = None) -> None:
        """
        Args:
            cache_dir: 生JSONをキャッシュするディレクトリ。
                       None の場合はキャッシュを行いません。
        """
        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)
        self.cache_dir = cache_dir
        if cache_dir:
            cache_dir.mkdir(parents=True, exist_ok=True)
        self._last_request_time: float = 0.0

    def _enforce_rate_limit(self) -> None:
        """リクエスト間のレート制限を強制します。"""
        elapsed = time.time() - self._last_request_time
        if elapsed < RATE_LIMIT_DELAY:
            sleep_time = RATE_LIMIT_DELAY - elapsed
            logger.debug("レート制限: %.2f秒待機", sleep_time)
            time.sleep(sleep_time)

    def _cache_key(self, url: str) -> Path:
        """URLからキャッシュファイルパスを生成します。"""
        safe_name = re.sub(r"[^\w\-_.]", "_", url.replace(BASE_URL, ""))
        return self.cache_dir / f"{safe_name[:200]}.json"

    def get_json(self, url: str, force_refresh: bool = False) -> Optional[Dict[str, Any]]:
        """
        指定URLからJSONデータを取得します（キャッシュ対応）。

        Args:
            url: 取得先URL
            force_refresh: Trueの場合はキャッシュを無視して再取得

        Returns:
            パース済みJSONデータ。失敗時はNone
        """
        # キャッシュチェック
        if self.cache_dir and not force_refresh:
            cache_path = self._cache_key(url)
            if cache_path.exists():
                logger.debug("キャッシュヒット: %s", cache_path)
                try:
                    return json.loads(cache_path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    logger.warning("キャッシュファイルが壊れています: %s", cache_path)

        self._enforce_rate_limit()

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                logger.info("[%d/%d] GET %s", attempt, MAX_RETRIES, url)
                response = self.session.get(url, timeout=REQUEST_TIMEOUT)
                self._last_request_time = time.time()

                if response.status_code == 429:
                    retry_after = int(response.headers.get("Retry-After", 60))
                    logger.warning("レート制限 (429): %d秒後にリトライ", retry_after)
                    time.sleep(retry_after)
                    continue

                response.raise_for_status()

                # Content-Type に応じてパース
                content_type = response.headers.get("Content-Type", "")
                if "application/json" in content_type:
                    data = response.json()
                else:
                    # HTMLから __NEXT_DATA__ を試みる
                    data = self._extract_next_data(response.text)
                    if data is None:
                        # プレーンテキストとしてJSON解析を試みる
                        try:
                            data = response.json()
                        except Exception:
                            logger.error("JSONパース失敗 (Content-Type: %s)", content_type)
                            return None

                # キャッシュ保存
                if self.cache_dir and data:
                    cache_path = self._cache_key(url)
                    cache_path.write_text(
                        json.dumps(data, ensure_ascii=False, indent=2),
                        encoding="utf-8"
                    )
                    logger.debug("キャッシュ保存: %s", cache_path)

                return data

            except requests.exceptions.ConnectionError as e:
                logger.error("接続エラー (試行 %d/%d): %s", attempt, MAX_RETRIES, e)
            except requests.exceptions.Timeout:
                logger.error("タイムアウト (試行 %d/%d): %s", attempt, MAX_RETRIES, url)
            except requests.exceptions.HTTPError as e:
                logger.error("HTTPエラー: %s", e)
                # 4xxエラーはリトライしない
                if response.status_code < 500:
                    return None
            except requests.exceptions.RequestException as e:
                logger.error("リクエストエラー (試行 %d/%d): %s", attempt, MAX_RETRIES, e)

            if attempt < MAX_RETRIES:
                backoff = RETRY_BACKOFF ** attempt
                logger.info("%.1f秒後にリトライ...", backoff)
                time.sleep(backoff)

        logger.error("最大リトライ回数に達しました: %s", url)
        return None

    def get_html(self, url: str) -> Optional[str]:
        """
        指定URLからHTMLテキストを取得します。

        Args:
            url: 取得先URL

        Returns:
            HTMLテキスト文字列。失敗時はNone
        """
        self._enforce_rate_limit()

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                logger.info("[%d/%d] GET HTML %s", attempt, MAX_RETRIES, url)
                response = self.session.get(url, timeout=REQUEST_TIMEOUT)
                self._last_request_time = time.time()
                response.raise_for_status()
                return response.text

            except requests.exceptions.RequestException as e:
                logger.error("HTMLフェッチエラー (試行 %d/%d): %s", attempt, MAX_RETRIES, e)
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_BACKOFF ** attempt)

        return None

    @staticmethod
    def _extract_next_data(html: str) -> Optional[Dict[str, Any]]:
        """
        HTMLページから Next.js の __NEXT_DATA__ JSONブロブを抽出します。

        rib.gg は Next.js を使用しているため、サーバーサイドレンダリングされた
        データが <script id="__NEXT_DATA__"> タグ内にJSON形式で埋め込まれています。

        Args:
            html: HTMLテキスト

        Returns:
            パース済みの __NEXT_DATA__ dict。見つからない場合はNone
        """
        try:
            soup = BeautifulSoup(html, "lxml")
            script_tag = soup.find("script", {"id": "__NEXT_DATA__"})
            if script_tag and script_tag.string:
                data = json.loads(script_tag.string)
                logger.debug("__NEXT_DATA__ の抽出に成功しました")
                return data
        except (json.JSONDecodeError, AttributeError) as e:
            logger.warning("__NEXT_DATA__ のパースに失敗: %s", e)

        return None


# ---------------------------------------------------------------------------
# シリーズデータ取得
# ---------------------------------------------------------------------------

def fetch_series_data(
    client: RibGGClient,
    series_id: str,
) -> Optional[Dict[str, Any]]:
    """
    シリーズ（トーナメントの1セット）のメタデータを取得します。

    取得内容:
      - 大会名、日付
      - 参加チーム（A/B）
      - マップBan/Pickの履歴
      - 含まれるMatch（マップ）のIDリスト

    Priority 1: /api/series/{series_id} エンドポイント
    Priority 2: /series/{series_id} ページの __NEXT_DATA__

    Args:
        client: RibGGClient インスタンス
        series_id: シリーズID（URLから抽出）

    Returns:
        シリーズデータのdict。失敗時はNone
    """
    # Priority 1: API直接取得を試みる
    api_url = f"{API_BASE}/series/{series_id}"
    data = client.get_json(api_url)

    if data:
        logger.info("シリーズデータをAPIから取得しました: series_id=%s", series_id)
        return data

    # Priority 2: __NEXT_DATA__ からフォールバック
    logger.info("APIが失敗。__NEXT_DATA__ からフォールバックします...")
    page_url = f"{BASE_URL}/series/{series_id}"
    html = client.get_html(page_url)
    if html:
        next_data = client._extract_next_data(html)
        if next_data:
            # __NEXT_DATA__ の構造: props.pageProps.series
            series = (
                next_data
                .get("props", {})
                .get("pageProps", {})
                .get("series")
            )
            if series:
                logger.info("__NEXT_DATA__ からシリーズデータを取得しました")
                return series

    logger.error("シリーズデータの取得に失敗しました: series_id=%s", series_id)
    return None


def parse_url_info(url: str) -> Optional[Tuple[str, str]]:
    """
    rib.gg の各種URLからIDとURLタイプを抽出します。

    対応URLパターン:
      /series/{id}                          → (id, "series")
      /events/{slug}/matches/{id}           → (id, "match")
      /matches/{id}                         → (id, "match")
      /events/{slug}/series/{id}            → (id, "series")

    例:
      "https://www.rib.gg/series/12345"                            → ("12345", "series")
      "https://www.rib.gg/events/vct-2026-pacific-kickoff/matches/6244"
                                                                   → ("6244", "match")
      "https://www.rib.gg/matches/6244"                            → ("6244", "match")

    Args:
        url: rib.gg のURL（www あり・なし両対応）

    Returns:
        (id文字列, タイプ文字列) のタプル。パース失敗時はNone
    """
    parsed = urlparse(url)
    path = parsed.path

    # /series/{id}  または  /events/{slug}/series/{id}
    m = re.search(r"/series/(\d+)", path)
    if m:
        return m.group(1), "series"

    # /events/{slug}/matches/{id}  または  /matches/{id}
    m = re.search(r"/matches/(\d+)", path)
    if m:
        return m.group(1), "match"

    logger.error(
        "URLからIDを抽出できませんでした: %s\n"
        "対応パターン: /series/{id}, /events/{slug}/matches/{id}, /matches/{id}",
        url,
    )
    return None


def parse_series_id_from_url(url: str) -> Optional[str]:
    """
    後方互換ラッパー。parse_url_info() を呼び出してIDのみを返します。

    Args:
        url: rib.gg のURL

    Returns:
        ID文字列（タイプを問わず）。パース失敗時はNone
    """
    result = parse_url_info(url)
    return result[0] if result else None


# ---------------------------------------------------------------------------
# マッチ（マップ）データ取得
# ---------------------------------------------------------------------------

def fetch_match_data(
    client: RibGGClient,
    match_id: str,
) -> Optional[Dict[str, Any]]:
    """
    個別マップ（マッチ）の詳細データを取得します。

    取得内容:
      - マップ名、最終スコア
      - ラウンドごとの詳細（勝敗・終了種別）
      - 全Killイベント（タイムスタンプ・武器・座標）
      - プレイヤーとエージェントの情報

    Priority 1: /api/match/{match_id}
    Priority 2: /api/matches/{match_id}/events（イベント専用エンドポイント）

    Args:
        client: RibGGClient インスタンス
        match_id: マッチID

    Returns:
        マッチデータのdict。失敗時はNone
    """
    # Priority 1: /api/match/{match_id}
    for api_url in [
        f"{API_BASE}/match/{match_id}",
        f"{API_BASE}/matches/{match_id}",
        f"{API_BASE}/matches/{match_id}/events",
    ]:
        data = client.get_json(api_url)
        if data:
            logger.info("マッチデータを取得しました: match_id=%s (endpoint: %s)", match_id, api_url)
            return data

    # Priority 2: ページHTMLの __NEXT_DATA__ からフォールバック
    logger.info("APIが失敗。__NEXT_DATA__ からフォールバックします...")
    page_url = f"{BASE_URL}/matches/{match_id}"
    html = client.get_html(page_url)
    if html:
        next_data = client._extract_next_data(html)
        if next_data:
            match_data = (
                next_data.get("props", {}).get("pageProps", {}).get("match")
                or next_data.get("props", {}).get("pageProps", {}).get("matchData")
                or next_data.get("props", {}).get("pageProps")
            )
            if match_data:
                logger.info("__NEXT_DATA__ からマッチデータを取得しました: match_id=%s", match_id)
                return match_data

    logger.error("マッチデータの取得に失敗しました: match_id=%s", match_id)
    return None


def fetch_match_as_series_data(
    client: RibGGClient,
    match_id: str,
    original_url: str,
) -> Optional[Dict[str, Any]]:
    """
    matchタイプのURLを起点として、シリーズ相当のデータを構築します。

    /events/{slug}/matches/{id} 形式のURLが渡された場合:
      1. /api/match/{id} からマッチデータを取得
      2. マッチデータに含まれるシリーズ情報（series_id）を探す
      3. シリーズ情報が見つかれば fetch_series_data() に委譲
      4. 見つからなければマッチデータから合成したシリーズdictを返す

    また、イベントページの __NEXT_DATA__ からシリーズIDを発見することも試みます。

    Args:
        client: RibGGClient インスタンス
        match_id: URLから抽出したマッチID
        original_url: ユーザーが渡した元URL（イベントページのパス解析に使用）

    Returns:
        シリーズデータ相当のdict（fetch_series_data と同形式）。失敗時はNone
    """
    logger.info("matchタイプURL → シリーズデータ構築開始: match_id=%s", match_id)

    # ── Step 1: マッチデータを取得 ──
    match_data = fetch_match_data(client, match_id)
    if not match_data:
        # __NEXT_DATA__ をイベントページから直接試みる
        html = client.get_html(original_url)
        if html:
            next_data = client._extract_next_data(html)
            if next_data:
                match_data = (
                    next_data.get("props", {}).get("pageProps", {}).get("match")
                    or next_data.get("props", {}).get("pageProps", {}).get("matchData")
                    or next_data.get("props", {}).get("pageProps")
                )
                if match_data:
                    logger.info("元URLの __NEXT_DATA__ からマッチデータを取得しました")

    if not match_data:
        logger.error("マッチデータを取得できませんでした: match_id=%s", match_id)
        return None

    # ── Step 2: マッチデータ内のシリーズIDを探す ──
    series_id_from_match = (
        match_data.get("seriesId")
        or match_data.get("series_id")
        or match_data.get("series", {}).get("id") if isinstance(match_data.get("series"), dict) else None
    )

    if series_id_from_match:
        logger.info("マッチデータからシリーズID=%s を発見。シリーズAPIに委譲します", series_id_from_match)
        series_data = fetch_series_data(client, str(series_id_from_match))
        if series_data:
            return series_data

    # ── Step 3: イベントページから __NEXT_DATA__ を取得してシリーズ情報を探す ──
    parsed_url = urlparse(original_url)
    # /events/{slug} の部分だけ取り出してページを取得
    event_path_match = re.match(r"(/events/[^/]+)", parsed_url.path)
    if event_path_match:
        event_page_url = f"{BASE_URL}{event_path_match.group(1)}"
        logger.info("イベントページから __NEXT_DATA__ を探します: %s", event_page_url)
        html = client.get_html(event_page_url)
        if html:
            next_data = client._extract_next_data(html)
            if next_data:
                # イベントページにシリーズリストが含まれている場合
                event_data = (
                    next_data.get("props", {}).get("pageProps", {}).get("event")
                    or next_data.get("props", {}).get("pageProps", {}).get("eventData")
                )
                if event_data:
                    series_list = event_data.get("series") or event_data.get("matches") or []
                    for s in series_list:
                        if str(s.get("id")) == match_id:
                            logger.info("イベントページからシリーズデータを発見しました")
                            return s

    # ── Step 4: 合成シリーズdictを作成（フォールバック）──
    # マッチデータから直接読み取れる情報でシリーズ相当のdictを構築
    logger.info("シリーズデータが見つからないため、マッチデータから合成します")

    teams = match_data.get("teams") or []
    event_info = match_data.get("event") or match_data.get("tournament") or {}
    event_name = (
        event_info.get("name")
        or event_info.get("shortName")
        or _slug_to_name(parsed_url.path)  # URLスラグから大会名を推定
    )

    # マッチデータをシリーズ形式にラップ
    synthetic_series: Dict[str, Any] = {
        "id":             match_id,
        "tournamentName": event_name,
        "date":           match_data.get("date") or match_data.get("createdAt") or "",
        "teams":          teams,
        "matches":        [{"id": match_id}],
        "mapBans":        match_data.get("mapBans") or [],
        # 元のマッチデータも保持しておく（scraper側で活用できるよう）
        "_raw_match_data": match_data,
    }

    logger.info(
        "合成シリーズdictを作成: event=%s, teams=%d",
        event_name, len(teams)
    )
    return synthetic_series


def _slug_to_name(url_path: str) -> str:
    """
    URLスラグから人間が読める大会名を生成するヘルパー。

    例: "/events/vct-2026-pacific-kickoff/matches/6244" → "Vct 2026 Pacific Kickoff"
    """
    m = re.search(r"/events/([^/]+)", url_path)
    if m:
        slug = m.group(1)
        return " ".join(word.capitalize() for word in slug.split("-"))
    return "Unknown Event"


# ---------------------------------------------------------------------------
# Killイベント抽出
# ---------------------------------------------------------------------------

def extract_kill_events(
    match_data: Dict[str, Any],
    round_number: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """
    マッチデータからKillイベントを抽出し、正規化します。

    抽出するフィールド:
      - round_number: ラウンド番号
      - timestamp: ラウンド開始からの経過秒数（float）
      - killer_id: キラーのプレイヤーID
      - killer_name: キラーの名前
      - victim_id: ビクティムのプレイヤーID
      - victim_name: ビクティムの名前
      - weapon_name: 武器名（resolve_weapon_name() で正規化済み）
      - location_x: X座標（任意）
      - location_y: Y座標（任意）
      - is_headshot: ヘッドショットフラグ（任意）

    rib.gg のデータ構造例:
    ```json
    {
      "rounds": [
        {
          "roundNumber": 1,
          "events": [
            {
              "type": "kill",
              "roundTimeMillis": 12500,
              "killer": {"playerId": "p1", "name": "Player1"},
              "victim": {"playerId": "p2", "name": "Player2"},
              "weapon": {"name": "Phantom"},
              "location": {"x": 3840, "y": 5120}
            }
          ]
        }
      ]
    }
    ```

    Args:
        match_data: fetch_match_data() が返すマッチデータ
        round_number: 特定ラウンドのみ抽出する場合に指定。Noneで全ラウンド

    Returns:
        正規化されたKillイベントのリスト
    """
    kill_events: List[Dict[str, Any]] = []

    # データ構造のパスをいくつか試みる（rib.gg はAPIバージョンで変わる可能性あり）
    rounds_data = (
        match_data.get("rounds")
        or match_data.get("data", {}).get("rounds")
        or match_data.get("matchData", {}).get("rounds")
        or []
    )

    if not rounds_data:
        logger.warning("ラウンドデータが見つかりませんでした")
        return kill_events

    for round_info in rounds_data:
        r_num = round_info.get("roundNumber") or round_info.get("round_number") or 0

        # 特定ラウンドのフィルタリング
        if round_number is not None and r_num != round_number:
            continue

        events = (
            round_info.get("events")
            or round_info.get("killEvents")
            or round_info.get("kills")
            or []
        )

        for event in events:
            # イベントタイプがkillでないものはスキップ
            event_type = event.get("type", "kill").lower()
            if event_type not in ("kill", "death", ""):
                continue

            # ─── タイムスタンプの取得と秒変換 ───
            # rib.gg はミリ秒で返す場合がある
            timestamp_raw = (
                event.get("roundTimeMillis")
                or event.get("roundTimeSecs")
                or event.get("timestamp")
                or event.get("time")
                or 0
            )
            # ミリ秒→秒変換（値が1000以上なら確実にミリ秒）
            timestamp_sec: float = (
                timestamp_raw / 1000.0
                if isinstance(timestamp_raw, (int, float)) and timestamp_raw > 1000
                else float(timestamp_raw)
            )

            # ─── キラー情報 ───
            killer_info = event.get("killer") or event.get("attacker") or {}
            killer_id = (
                str(killer_info.get("playerId") or killer_info.get("id") or "")
            )
            killer_name = killer_info.get("name") or killer_info.get("playerName") or ""

            # ─── ビクティム情報 ───
            victim_info = event.get("victim") or event.get("player") or {}
            victim_id = str(victim_info.get("playerId") or victim_info.get("id") or "")
            victim_name = victim_info.get("name") or victim_info.get("playerName") or ""

            # ─── 武器情報 ───
            weapon_info = event.get("weapon") or event.get("weaponId") or {}
            if isinstance(weapon_info, dict):
                weapon_raw = weapon_info.get("name") or weapon_info.get("id")
            else:
                weapon_raw = weapon_info  # 文字列またはintID

            weapon_name = resolve_weapon_name(weapon_raw)

            # ─── 位置情報 ───
            location = event.get("location") or event.get("position") or {}
            if isinstance(location, dict):
                loc_x: Optional[float] = location.get("x")
                loc_y: Optional[float] = location.get("y")
            else:
                loc_x, loc_y = None, None

            kill_events.append({
                "round_number":  r_num,
                "timestamp":     timestamp_sec,
                "killer_id":     killer_id,
                "killer_name":   killer_name,
                "victim_id":     victim_id,
                "victim_name":   victim_name,
                "weapon_name":   weapon_name,
                "location_x":   loc_x,
                "location_y":   loc_y,
                "is_headshot":   event.get("isHeadshot") or event.get("headshot") or False,
            })

    logger.info(
        "%d件のKillイベントを抽出しました (ラウンド絞り込み: %s)",
        len(kill_events),
        round_number if round_number is not None else "全ラウンド"
    )
    return kill_events


# ---------------------------------------------------------------------------
# ロースター（プレイヤー）情報抽出
# ---------------------------------------------------------------------------

def extract_roster(
    series_data: Dict[str, Any],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    シリーズデータからチームAとチームBのロースターを抽出します。

    Args:
        series_data: fetch_series_data() が返すシリーズデータ

    Returns:
        (team_a_players, team_b_players) のタプル
        各要素は {"id", "name", "agent", "team"} を含むdictのリスト
    """
    def _parse_team(team_data: Dict[str, Any], team_label: str) -> List[Dict[str, Any]]:
        players = []
        player_list = (
            team_data.get("players")
            or team_data.get("roster")
            or []
        )
        for p in player_list:
            players.append({
                "id":    str(p.get("id") or p.get("playerId") or ""),
                "name":  p.get("name") or p.get("ign") or p.get("playerName") or "Unknown",
                "agent": p.get("agent") or p.get("agentName") or "Unknown",
                "team":  team_label,
            })
        return players

    teams = series_data.get("teams") or []
    if len(teams) >= 2:
        team_a_name = teams[0].get("name", "Team A")
        team_b_name = teams[1].get("name", "Team B")
        team_a = _parse_team(teams[0], team_a_name)
        team_b = _parse_team(teams[1], team_b_name)
    else:
        team_a_data = series_data.get("teamA") or series_data.get("team1") or {}
        team_b_data = series_data.get("teamB") or series_data.get("team2") or {}
        team_a = _parse_team(team_a_data, team_a_data.get("name", "Team A"))
        team_b = _parse_team(team_b_data, team_b_data.get("name", "Team B"))

    logger.info(
        "ロースター抽出: チームA=%d名, チームB=%d名",
        len(team_a), len(team_b)
    )
    return team_a, team_b


# ---------------------------------------------------------------------------
# マップBan/Pick情報抽出
# ---------------------------------------------------------------------------

def extract_map_picks(series_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    シリーズデータからマップBan/Pick履歴を抽出します。

    Args:
        series_data: fetch_series_data() が返すシリーズデータ

    Returns:
        マップ選択履歴のリスト。各要素:
        {"order": int, "action": "ban"|"pick", "team": str, "map": str}
    """
    picks_raw = (
        series_data.get("mapBans")
        or series_data.get("mapPicks")
        or series_data.get("vod_data", {}).get("mapBanPick")
        or []
    )

    picks: List[Dict[str, Any]] = []
    for i, pick in enumerate(picks_raw):
        picks.append({
            "order":  i + 1,
            "action": pick.get("action") or pick.get("type") or "unknown",
            "team":   pick.get("team") or pick.get("teamName") or "Unknown",
            "map":    pick.get("map") or pick.get("mapName") or "Unknown",
        })

    logger.info("マップBan/Pick %d件を抽出しました", len(picks))
    return picks
