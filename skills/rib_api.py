"""
skills/rib_api.py
=================
rib.gg 専用Webクローラー兼データ抽出クライアント。

主要機能:
  1. Playwright Chromium ブラウザによる実ブラウザクローリング（ボット検出回避）
  2. __NEXT_DATA__ JSONブロブからのデータ抽出（Next.js SSR）
  3. 武器ID → 武器名へのマッピング (WEAPON_MAP)
  4. Killイベント（タイムスタンプ・武器・座標）の抽出

データ取得戦略（優先順位順）:
  Priority 1: Playwright Chromium ブラウザ（__NEXT_DATA__ JS評価）
              → TLSフィンガープリント偽装によりボット検出を回避
  Priority 2: Playwright APIインターセプト
              → ページロード時のAPIレスポンスをネットワーク傍受
  Fallback:   requests（Playwright未インストール時のみ）
              → rib.gg のEnvoyプロキシによりブロックされる可能性が高い
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
# ---------------------------------------------------------------------------
# 定数
# ---------------------------------------------------------------------------
BASE_URL = "https://www.rib.gg"
API_BASE = "https://www.rib.gg/api"

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/122.0.0.0 Safari/537.36"
)

# ─── ページナビゲーション用ヘッダー（HTML取得・ブラウザの「タブを開く」相当）───
_PAGE_HEADERS: Dict[str, str] = {
    "User-Agent":                _UA,
    "Accept":                    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "Accept-Language":           "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding":           "gzip, deflate",
    "Cache-Control":             "max-age=0",
    "Upgrade-Insecure-Requests": "1",
    "sec-ch-ua":                 '"Chromium";v="122", "Not(A:Brand";v="24", "Google Chrome";v="122"',
    "sec-ch-ua-mobile":          "?0",
    "sec-ch-ua-platform":        '"Windows"',
    "Sec-Fetch-Dest":            "document",
    "Sec-Fetch-Mode":            "navigate",
    "Sec-Fetch-Site":            "none",   # 直接入力 or 外部リンク遷移
    "Sec-Fetch-User":            "?1",
}

# ─── 同一オリジンAPIリクエスト用ヘッダー（ページ内 fetch() 相当）───
_API_HEADERS: Dict[str, str] = {
    "User-Agent":       _UA,
    "Accept":           "application/json, text/plain, */*",
    "Accept-Language":  "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding":  "gzip, deflate",
    "Origin":           BASE_URL,
    "Referer":          f"{BASE_URL}/",
    "sec-ch-ua":        '"Chromium";v="122", "Not(A:Brand";v="24", "Google Chrome";v="122"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "Sec-Fetch-Dest":   "empty",
    "Sec-Fetch-Mode":   "cors",
    "Sec-Fetch-Site":   "same-origin",
}

# 後方互換のため残す（外部からアクセスされている場合に備えて）
DEFAULT_HEADERS = _PAGE_HEADERS

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

    def __init__(
        self,
        cache_dir: Optional[Path] = None,
        use_browser: bool = True,
    ) -> None:
        """
        Args:
            cache_dir: 生JSONをキャッシュするディレクトリ。
                       None の場合はキャッシュを行いません。
            use_browser: True の場合、Playwright Chromium を起動して
                         TLSフィンガープリント偽装によるボット検出回避を試みます。
                         playwright 未インストール時は requests にフォールバックします。
        """
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent":         _UA,
            "Accept-Language":    "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding":    "gzip, deflate",
            "sec-ch-ua":          '"Chromium";v="122", "Not(A:Brand";v="24", "Google Chrome";v="122"',
            "sec-ch-ua-mobile":   "?0",
            "sec-ch-ua-platform": '"Windows"',
        })
        self.cache_dir = cache_dir
        if cache_dir:
            cache_dir.mkdir(parents=True, exist_ok=True)
        self._last_request_time: float = 0.0
        self._browser: Optional["PlaywrightBrowser"] = None

        if use_browser:
            try:
                self._browser = PlaywrightBrowser()
                logger.info("Playwrightブラウザモード: 有効 (TLSフィンガープリント偽装)")
            except ImportError as e:
                logger.warning("%s", e)
                logger.warning("requestsモードにフォールバック（ボット検出される可能性あり）")
                self._warmup()
            except Exception as e:
                logger.warning("ブラウザ起動失敗: %s — requestsモードにフォールバック", e)
                self._warmup()
        else:
            self._warmup()

    def __del__(self) -> None:
        """ブラウザリソースを解放する"""
        if getattr(self, "_browser", None):
            try:
                self._browser.close()
            except Exception:
                pass

    def _warmup(self) -> None:
        """
        ホームページを訪問してセッションクッキーを取得します。

        rib.gg はボット検出のため、最初のリクエストでクッキーが
        セットされていることを確認する場合があります。ホームページを
        先に訪問することで、以降のリクエストが本物のブラウザに見えます。
        """
        try:
            logger.info("セッション初期化: %s を訪問中...", BASE_URL)
            resp = self.session.get(
                BASE_URL,
                headers=_PAGE_HEADERS,
                timeout=REQUEST_TIMEOUT,
                allow_redirects=True,
            )
            self._last_request_time = time.time()
            cookie_count = len(self.session.cookies)
            logger.info(
                "セッション初期化完了: status=%d, cookies=%d件",
                resp.status_code, cookie_count,
            )
        except requests.exceptions.RequestException as e:
            # ウォームアップ失敗は致命的ではないため警告のみ
            logger.warning("セッション初期化に失敗（続行します）: %s", e)

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

        # API リクエストヘッダー: Referer を呼び出し元URLのページに設定
        # （ページ内の fetch() が送るヘッダーを再現）
        api_headers = {
            **_API_HEADERS,
            "Referer": url.rsplit("/api/", 1)[0] + "/" if "/api/" in url else f"{BASE_URL}/",
        }

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                logger.info("[%d/%d] GET %s", attempt, MAX_RETRIES, url)
                response = self.session.get(url, headers=api_headers, timeout=REQUEST_TIMEOUT)
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

        Playwright ブラウザが利用可能な場合は本物の Chromium でアクセスし、
        TLSフィンガープリント検出によるボット対策を回避します。

        Args:
            url: 取得先URL

        Returns:
            HTMLテキスト文字列。失敗時はNone
        """
        # Playwright が利用可能な場合は優先して使う
        if self._browser:
            return self._browser.get_html(url)

        self._enforce_rate_limit()

        # HTML ページ取得ヘッダー: Sec-Fetch-Site を same-origin/cross-site で切り替える
        parsed_target = urlparse(url)
        is_same_origin = parsed_target.netloc in ("www.rib.gg", "rib.gg")
        page_headers = {
            **_PAGE_HEADERS,
            "Sec-Fetch-Site": "same-origin" if is_same_origin else "cross-site",
            "Referer": f"{BASE_URL}/",
        }

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                logger.info("[%d/%d] GET HTML %s", attempt, MAX_RETRIES, url)
                response = self.session.get(url, headers=page_headers, timeout=REQUEST_TIMEOUT)
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
# Playwright ブラウザクライアント（TLSフィンガープリント偽装・ボット検出回避）
# ---------------------------------------------------------------------------

class PlaywrightBrowser:
    """
    Playwright Chromium ブラウザセッション。

    Python requests ライブラリは TLS フィンガープリントが urllib3 独自実装のため、
    Envoy / Cloudflare などの高度なボット検出（JA3/JA4 フィンガープリント検査）を
    通過できません。Playwright は本物の Chromium を起動するため TLS が一致し、
    ボット検出を回避できます。

    取得フロー:
      1. get_next_data()  … JS 経由で __NEXT_DATA__ を直接取得（最速・最優先）
      2. intercept_api()  … ページロード時のAPIレスポンスをネットワーク傍受
      3. get_html()       … 生HTMLを返す（BeautifulSoup でパース）

    使い方:
        browser = PlaywrightBrowser()
        data = browser.get_next_data("https://www.rib.gg/events/.../matches/6244")
        browser.close()
    """

    def __init__(self, headless: bool = True) -> None:
        try:
            from playwright.sync_api import sync_playwright  # noqa: F401
        except ImportError:
            raise ImportError(
                "playwright がインストールされていません。\n"
                "インストール方法:\n"
                "  pip install playwright\n"
                "  playwright install chromium"
            )
        from playwright.sync_api import sync_playwright

        logger.info("Playwrightブラウザを起動中 (headless=%s) ...", headless)
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=headless)
        self._context = self._browser.new_context(
            user_agent=_UA,
            locale="ja-JP",
            timezone_id="Asia/Tokyo",
            viewport={"width": 1920, "height": 1080},
            extra_http_headers={
                "Accept-Language": "ja-JP,ja;q=0.9,en-US;q=0.8,en;q=0.7",
            },
        )
        logger.info("Playwrightブラウザ起動完了")
        self._warmup()

    def _warmup(self) -> None:
        """ホームページを訪問してセッションクッキーを確立する"""
        page = self._context.new_page()
        try:
            logger.info("ブラウザセッション初期化: %s を訪問中...", BASE_URL)
            page.goto(BASE_URL, wait_until="domcontentloaded", timeout=30_000)
            cookies = len(self._context.cookies())
            logger.info("ブラウザセッション初期化完了: cookies=%d件", cookies)
        except Exception as e:
            logger.warning("ブラウザウォームアップ失敗（続行します）: %s", e)
        finally:
            page.close()

    def get_html(self, url: str) -> Optional[str]:
        """指定URLのページHTMLを取得する（JavaScript実行後の完全なDOM）"""
        page = self._context.new_page()
        try:
            logger.info("ブラウザ GET HTML: %s", url)
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)
            time.sleep(0.5)  # 動的コンテンツの追加レンダリング待機
            return page.content()
        except Exception as e:
            logger.error("ブラウザ HTMLフェッチエラー: %s", e)
            return None
        finally:
            page.close()

    def get_next_data(self, url: str) -> Optional[Dict[str, Any]]:
        """
        指定URLの Next.js __NEXT_DATA__ を JavaScript 経由で直接取得する。

        Next.js SSR では <script id="__NEXT_DATA__"> にサーバーサイドレンダリング済み
        データが埋め込まれます。JS で直接パースすることで高速・確実に取得できます。

        Args:
            url: rib.gg のページURL

        Returns:
            __NEXT_DATA__ dict。取得失敗時はNone
        """
        page = self._context.new_page()
        try:
            logger.info("ブラウザ GET __NEXT_DATA__: %s", url)
            page.goto(url, wait_until="domcontentloaded", timeout=30_000)

            # JavaScript で直接 __NEXT_DATA__ を取得（最速）
            data = page.evaluate("""() => {
                try {
                    const el = document.getElementById('__NEXT_DATA__');
                    return el ? JSON.parse(el.textContent) : null;
                } catch(e) { return null; }
            }""")

            if data:
                logger.info("__NEXT_DATA__ を取得しました: %s", url)
                return data

            # JS取得失敗 → HTML全体を BeautifulSoup でパース（フォールバック）
            logger.debug("JS取得失敗。HTMLをBeautifulSoupでパースします")
            return RibGGClient._extract_next_data(page.content())

        except Exception as e:
            logger.error("__NEXT_DATA__ 取得エラー: %s", e)
            return None
        finally:
            page.close()

    def intercept_api(
        self,
        page_url: str,
        api_pattern: str = "/api/",
    ) -> Optional[Dict[str, Any]]:
        """
        ページ読み込み時のAPIレスポンスをネットワーク傍受してJSONを返す。

        rib.gg は Next.js アプリのため、クライアントサイドで内部APIを呼び出します。
        Playwright のネットワークインターセプト機能でそのAPIレスポンスをキャプチャし、
        __NEXT_DATA__ に含まれないデータも取得できます。

        Args:
            page_url: 読み込むページURL
            api_pattern: キャプチャするAPIのURLパターン（部分一致）

        Returns:
            キャプチャしたJSONデータ（最もデータ量の多いもの）。失敗時はNone
        """
        captured: List[Dict[str, Any]] = []

        def on_response(response: Any) -> None:
            if api_pattern in response.url and response.status == 200:
                try:
                    ct = response.headers.get("content-type", "")
                    if "json" in ct:
                        captured.append({
                            "url":  response.url,
                            "data": response.json(),
                        })
                        logger.debug("APIレスポンスキャプチャ: %s", response.url)
                except Exception:
                    pass

        page = self._context.new_page()
        page.on("response", on_response)
        try:
            logger.info(
                "ブラウザ APIインターセプト: %s (pattern=%s)", page_url, api_pattern
            )
            page.goto(page_url, wait_until="networkidle", timeout=45_000)

            if not captured:
                logger.warning("APIレスポンスをキャプチャできませんでした: %s", page_url)
                return None

            logger.info("APIレスポンス %d件キャプチャ完了", len(captured))
            # 最もデータ量の多いレスポンスを採用
            best = max(captured, key=lambda x: len(str(x["data"])))
            logger.info("採用エンドポイント: %s", best["url"])
            return best["data"]

        except Exception as e:
            logger.error("APIインターセプトエラー: %s", e)
            return None
        finally:
            page.close()

    def close(self) -> None:
        """ブラウザリソースを解放する"""
        try:
            self._context.close()
            self._browser.close()
            self._pw.stop()
            logger.debug("Playwrightブラウザを終了しました")
        except Exception:
            pass

    def __enter__(self) -> "PlaywrightBrowser":
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


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

    取得戦略（Playwright優先）:
      Primary:   Playwright ブラウザで /series/{id} を訪問 → __NEXT_DATA__ を取得
      Secondary: Playwright APIインターセプト（__NEXT_DATA__ にシリーズなし時）
      Fallback:  requests（Playwright未インストール時）

    Args:
        client: RibGGClient インスタンス
        series_id: シリーズID（URLから抽出）

    Returns:
        シリーズデータのdict。失敗時はNone
    """
    page_url = f"{BASE_URL}/series/{series_id}"

    if client._browser:
        # PRIMARY: Playwright ブラウザで __NEXT_DATA__ を取得
        logger.info("ブラウザでシリーズページをクローリングします: %s", page_url)
        next_data = client._browser.get_next_data(page_url)
        if next_data:
            series = _dig_series_from_next_data(next_data)
            if series:
                logger.info("ブラウザ __NEXT_DATA__ からシリーズデータを取得しました")
                return series

        # SECONDARY: __NEXT_DATA__ にシリーズデータなし → APIインターセプト
        logger.info("__NEXT_DATA__ にシリーズデータなし。APIインターセプトを試みます...")
        api_data = client._browser.intercept_api(page_url)
        if api_data:
            logger.info("APIインターセプトでシリーズデータを取得しました")
            return api_data

    else:
        # FALLBACK (Playwright 未インストール): requests による取得
        logger.warning(
            "Playwrightブラウザが利用できません。requestsでフォールバックします"
            "（rib.gg のボット検出によりブロックされる可能性があります）"
        )
        api_url = f"{API_BASE}/series/{series_id}"
        data = client.get_json(api_url)
        if data:
            logger.info("requestsでAPIからシリーズデータを取得しました: series_id=%s", series_id)
            return data

        html = client.get_html(page_url)
        if html:
            next_data = client._extract_next_data(html)
            if next_data:
                series = _dig_series_from_next_data(next_data)
                if series:
                    logger.info("requests __NEXT_DATA__ からシリーズデータを取得しました")
                    return series

    logger.error("シリーズデータの取得に失敗しました: series_id=%s", series_id)
    return None


# ---------------------------------------------------------------------------
# __NEXT_DATA__ 掘り出しヘルパー
# ---------------------------------------------------------------------------

def _dig_match_from_next_data(next_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    __NEXT_DATA__ 構造からマッチデータを掘り出すヘルパー。

    rib.gg のページ構造バリエーションに対応:
      props.pageProps.match           (通常の試合ページ)
      props.pageProps.matchData       (旧バージョン)
      props.pageProps.data.match      (ネスト構造)
      props.pageProps                 (rounds/events/players が直下にある場合)
    """
    page_props = next_data.get("props", {}).get("pageProps", {})
    logger.debug("__NEXT_DATA__ pageProps keys: %s", list(page_props.keys()))
    match = (
        page_props.get("match")
        or page_props.get("matchData")
        or (page_props.get("data") or {}).get("match")
    )
    if match:
        return match
    # pageProps 直下にラウンド等が含まれる場合
    if any(k in page_props for k in ("rounds", "events", "players", "killEvents")):
        return page_props
    return None


def _dig_series_from_next_data(next_data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    __NEXT_DATA__ 構造からシリーズデータを掘り出すヘルパー。

    rib.gg の /events/{slug}/matches/{id} ページでは、
    pageProps.series にシリーズ全体（チーム・マッチ一覧・マップBan/Pick等）が
    格納されることが多い。

    対応バリエーション:
      props.pageProps.series          (通常のシリーズ・イベント試合ページ)
      props.pageProps.seriesData      (旧バージョン)
      props.pageProps.data.series     (ネスト構造)
    """
    page_props = next_data.get("props", {}).get("pageProps", {})
    return (
        page_props.get("series")
        or page_props.get("seriesData")
        or (page_props.get("data") or {}).get("series")
    )


def _build_synthetic_series(
    match_data: Dict[str, Any],
    match_id: str,
    original_url: str,
) -> Dict[str, Any]:
    """
    マッチデータからシリーズ相当のdictを合成するヘルパー。

    シリーズAPIが利用できない場合に、マッチデータに含まれる
    チーム・イベント情報からシリーズ構造を再現します。
    """
    parsed_path = urlparse(original_url).path
    teams = match_data.get("teams") or []
    event_info = match_data.get("event") or match_data.get("tournament") or {}
    event_name = (
        event_info.get("name")
        or event_info.get("shortName")
        or _slug_to_name(parsed_path)
    )
    logger.info(
        "合成シリーズdictを作成: event=%s, teams=%d", event_name, len(teams)
    )
    return {
        "id":              match_id,
        "tournamentName":  event_name,
        "date":            match_data.get("date") or match_data.get("createdAt") or "",
        "teams":           teams,
        "matches":         [{"id": match_id}],
        "mapBans":         match_data.get("mapBans") or [],
        "_raw_match_data": match_data,
    }


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
    original_url: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """
    個別マップ（マッチ）の詳細データを取得します。

    取得内容:
      - マップ名、最終スコア
      - ラウンドごとの詳細（勝敗・終了種別）
      - 全Killイベント（タイムスタンプ・武器・座標）
      - プレイヤーとエージェントの情報

    取得戦略（Playwright優先）:
      Primary:   Playwright ブラウザで original_url（または /matches/{id}）を訪問
                 → __NEXT_DATA__ を JS 評価で直接取得（_dig_match_from_next_data）
      Secondary: Playwright APIインターセプト（__NEXT_DATA__ にマッチデータなし時）
      Fallback:  requests（Playwright未インストール時）

    Args:
        client: RibGGClient インスタンス
        match_id: マッチID
        original_url: ユーザーが渡した元URL。指定時はそちらを優先して訪問。

    Returns:
        マッチデータのdict。失敗時はNone
    """
    # original_url が指定されていればそちらを使う（イベントページの正確なパスを保証）
    page_url = original_url or f"{BASE_URL}/matches/{match_id}"

    if client._browser:
        # PRIMARY: Playwright ブラウザで __NEXT_DATA__ を取得
        logger.info("ブラウザでマッチページをクローリングします: %s", page_url)
        next_data = client._browser.get_next_data(page_url)
        if next_data:
            match_data = _dig_match_from_next_data(next_data)
            if match_data:
                logger.info("ブラウザ __NEXT_DATA__ からマッチデータを取得しました")
                return match_data

        # SECONDARY: __NEXT_DATA__ にマッチデータなし → APIインターセプト
        logger.info("__NEXT_DATA__ にマッチデータなし。APIインターセプトを試みます...")
        api_data = client._browser.intercept_api(page_url)
        if api_data:
            logger.info("APIインターセプトでマッチデータを取得しました")
            return api_data

    else:
        # FALLBACK (Playwright 未インストール): requests による取得
        logger.warning(
            "Playwrightブラウザが利用できません。requestsでフォールバックします"
            "（rib.gg のボット検出によりブロックされる可能性があります）"
        )
        for api_url in [
            f"{API_BASE}/match/{match_id}",
            f"{API_BASE}/matches/{match_id}",
        ]:
            data = client.get_json(api_url)
            if data:
                logger.info("requestsでAPIからマッチデータを取得しました: %s", api_url)
                return data

        html = client.get_html(page_url)
        if html:
            next_data = client._extract_next_data(html)
            if next_data:
                match_data = _dig_match_from_next_data(next_data)
                if match_data:
                    logger.info("requests __NEXT_DATA__ からマッチデータを取得しました")
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

    /events/{slug}/matches/{id} 形式のURLが渡された場合、ブラウザで
    そのページを訪問して __NEXT_DATA__ からマッチデータを一括取得し、
    シリーズ相当のdictを合成して返します。

    処理フロー:
      1. fetch_match_data() でマッチデータを取得（Playwright PRIMARY）
      2. マッチデータ内にシリーズIDが含まれる場合は fetch_series_data() に委譲
      3. 見つからなければ _build_synthetic_series() でシリーズdictを合成

    Args:
        client: RibGGClient インスタンス
        match_id: URLから抽出したマッチID
        original_url: ユーザーが渡した元URL（ブラウザクローリングに使用）

    Returns:
        シリーズデータ相当のdict（fetch_series_data と同形式）。失敗時はNone
    """
    logger.info("matchタイプURL → シリーズデータ構築開始: match_id=%s", match_id)

    if client._browser:
        # ── ブラウザで original_url を1回だけ訪問 ──
        logger.info("ブラウザで %s をクローリングします", original_url)
        next_data = client._browser.get_next_data(original_url)

        if next_data:
            page_props = next_data.get("props", {}).get("pageProps", {})
            logger.debug("pageProps keys: %s", list(page_props.keys()))

            # Step 1: series キーを最初に確認
            # /events/{slug}/matches/{id} ページは pageProps.series にシリーズデータを持つことが多い
            series = _dig_series_from_next_data(next_data)
            if series:
                logger.info("__NEXT_DATA__ からシリーズデータを直接取得しました")
                return series

            # Step 2: match キーを確認
            match_data = _dig_match_from_next_data(next_data)
            if match_data:
                series_obj = match_data.get("series")
                series_id_from_match = (
                    match_data.get("seriesId")
                    or match_data.get("series_id")
                    or (series_obj.get("id") if isinstance(series_obj, dict) else None)
                )
                if series_id_from_match:
                    logger.info(
                        "マッチデータからシリーズID=%s を発見。fetch_series_data に委譲します",
                        series_id_from_match,
                    )
                    series_data = fetch_series_data(client, str(series_id_from_match))
                    if series_data:
                        return series_data
                return _build_synthetic_series(match_data, match_id, original_url)

        # __NEXT_DATA__ に有効なデータなし → APIインターセプト
        logger.info(
            "__NEXT_DATA__ に有効なデータなし。APIインターセプトを試みます: %s", original_url
        )
        api_data = client._browser.intercept_api(original_url)
        if api_data:
            logger.info("APIインターセプトからデータを取得しました")
            # シリーズ構造らしければそのまま返す
            if "matches" in api_data or "mapBans" in api_data or "teams" in api_data:
                return api_data
            return _build_synthetic_series(api_data, match_id, original_url)

    else:
        # FALLBACK (Playwright 未インストール): requests による取得
        logger.warning(
            "Playwrightブラウザが利用できません。requestsでフォールバックします"
            "（rib.gg のボット検出によりブロックされる可能性があります）"
        )
        match_data = fetch_match_data(client, match_id, original_url=original_url)
        if match_data:
            series_obj = match_data.get("series")
            series_id_from_match = (
                match_data.get("seriesId")
                or match_data.get("series_id")
                or (series_obj.get("id") if isinstance(series_obj, dict) else None)
            )
            if series_id_from_match:
                series_data = fetch_series_data(client, str(series_id_from_match))
                if series_data:
                    return series_data
            return _build_synthetic_series(match_data, match_id, original_url)

    logger.error("シリーズデータの取得に失敗しました: match_id=%s", match_id)
    return None


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
