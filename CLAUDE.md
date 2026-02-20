# VALORANT Match Data Collector - Project Guidelines

## 概要 (Overview)

このプロジェクトは **rib.gg** からVALORANTの試合データをWebクローリングで収集し、
「Kill Pace（キルペース）」や「武器使用率」を分析するデータパイプラインです。

rib.gg は Next.js 製アプリケーションであり、Envoy プロキシによる TLS フィンガープリント
検査でボット検出を行います。そのため **Playwright Chromium ブラウザ** を使った
実ブラウザクローリングを採用し、`__NEXT_DATA__` JSONブロブからデータを抽出します。

---

## プロジェクト構造 (Project Structure)

```
Data_collector/
├── CLAUDE.md              # このファイル（プロジェクトガイドライン）
├── requirements.txt       # Python依存関係
├── main.py                # エントリポイント
├── skills/
│   └── rib_api.py         # rib.gg 専用ブラウザクローラー・データ抽出ロジック
├── src/
│   ├── scraper.py         # ラウンドデータの反復収集
│   ├── analyzer.py        # Kill Delta / 武器統計の計算
│   └── database.py        # SQLite スキーマ定義・データ挿入
└── data/
    ├── raw/               # クローリングで取得した生JSONデータ
    └── processed/         # 処理済みCSV / SQLiteファイル
```

---

## 開発コマンド (Development Commands)

```bash
# 依存関係のインストール
pip install -r requirements.txt

# Playwright Chromium ブラウザのインストール（初回必須）
playwright install chromium

# スクレイパーの実行（シリーズURLまたはマッチURLを指定）
python main.py --url "https://www.rib.gg/series/<series_id>"
python main.py --url "https://www.rib.gg/events/<event-slug>/matches/<match_id>"

# 詳細ログを有効化
python main.py --url "https://www.rib.gg/series/<series_id>" --verbose

# 分析レポートの出力（既存DBから）
python main.py --report --db data/processed/valorant_matches.db
```

---

## コーディング規約 (Coding Standards)

### 言語・バージョン
- **Python 3.9+** を使用すること
- 型ヒント（`typing` モジュール）を全関数に付与すること

### ロギング
- `print()` は使用禁止。必ず `logging` モジュールを使用すること
- ログレベル: `DEBUG` (詳細), `INFO` (進捗), `WARNING` (軽微な問題), `ERROR` (致命的エラー)
- フォーマット: `%(asctime)s [%(levelname)s] %(name)s: %(message)s`

### Webクローリング（Playwright優先）
- **データ取得は必ず Playwright Chromium ブラウザを経由すること**
- `RibGGClient(use_browser=True)` がデフォルト。`PlaywrightBrowser` が自動起動する
- Playwright が未インストールの場合のみ `requests` にフォールバックする
  （ただしrib.ggのボット検出によりブロックされる可能性が高い）
- ページ訪問後は `__NEXT_DATA__` を JS 評価で直接取得すること（`get_next_data()`）
- `__NEXT_DATA__` にデータが含まれない場合は APIインターセプト（`intercept_api()`）を使用

### データ処理
- DB挿入前に必ず `pandas.DataFrame` を経由して中間処理を行うこと
- 武器IDは `skills/rib_api.py` の `WEAPON_MAP` 辞書で名前に変換すること
- タイムスタンプは秒単位（float）で統一すること

### エラー処理
- ブラウザエラー: Playwright 例外をキャッチしてログ出力し、`None` を返す
- パースエラー: `KeyError`, `TypeError` をキャッチして警告ログを出力し、スキップ
- DB エラー: `sqlalchemy.exc.SQLAlchemyError` をキャッチしてロールバック

---

## アーキテクチャ概要 (Architecture)

```
[rib.gg ページ（Next.js SSR）]
          |
          | Playwright Chromium ブラウザ（実TLSフィンガープリント）
          v
  skills/rib_api.py
    ├── PlaywrightBrowser       ← Chromium起動・セッション管理
    │     ├── get_next_data()   ← __NEXT_DATA__ JS評価（PRIMARY）
    │     └── intercept_api()   ← APIレスポンスネットワーク傍受（SECONDARY）
    ├── fetch_series_data()     ← シリーズメタデータ取得
    ├── fetch_match_data()      ← マッチ詳細取得（ラウンド・Kill）
    ├── fetch_match_as_series_data() ← matchURL → シリーズ構造変換
    ├── extract_kill_events()   ← Killイベント正規化
    └── WEAPON_MAP              ← 武器ID → 武器名マッピング
          |
          v
    src/scraper.py              ← ラウンド反復・生データ収集・data/raw/ に保存
          |
          v
    src/analyzer.py             ← Kill Delta計算・武器統計集計・DataFrame生成
          |
          v
    src/database.py             ← SQLite スキーマ初期化・DataFrame から一括挿入
```

---

## データ取得戦略 (Data Retrieval Strategy)

rib.gg は Envoy プロキシで **TLS フィンガープリント（JA3/JA4）** を検査しており、
`requests` / `urllib3` などの Python HTTPクライアントは 403 (`x-deny-reason: host_not_allowed`)
でブロックされます。

### 採用戦略: Playwright 実ブラウザクローリング

| 優先度 | 手段 | 説明 |
|---|---|---|
| PRIMARY | `get_next_data()` | Playwright で該当URLを訪問し、`__NEXT_DATA__` を JS で評価取得 |
| SECONDARY | `intercept_api()` | ページロード時のAPIレスポンスをネットワーク傍受 |
| FALLBACK | `requests` | Playwright未インストール時のみ（ブロックされる可能性大） |

### 対応URLパターン

```
https://www.rib.gg/series/{series_id}
https://www.rib.gg/events/{event-slug}/matches/{match_id}
https://www.rib.gg/matches/{match_id}
```

---

## データスキーマ (Database Schema)

### `matches` テーブル
| カラム名 | 型 | 説明 |
|---|---|---|
| id | TEXT (PK) | シリーズID |
| tournament_name | TEXT | 大会名 |
| date | TEXT | 試合日 |
| team_a | TEXT | チームA名 |
| team_b | TEXT | チームB名 |
| map_picks_json | TEXT | マップBan/Pick履歴（JSON文字列） |

### `rounds` テーブル
| カラム名 | 型 | 説明 |
|---|---|---|
| id | INTEGER (PK) | ラウンドDB ID |
| match_id | TEXT (FK) | matchesテーブル参照 |
| map_name | TEXT | マップ名 |
| round_number | INTEGER | ラウンド番号 |
| winning_team | TEXT | 勝利チーム |
| end_type | TEXT | 終了種別（Elimination/Defuse等） |
| duration_seconds | REAL | ラウンド時間（秒） |

### `players` テーブル
| カラム名 | 型 | 説明 |
|---|---|---|
| id | TEXT (PK) | プレイヤーID |
| name | TEXT | プレイヤー名 |
| team | TEXT | 所属チーム |
| agent | TEXT | エージェント名 |

### `kill_events` テーブル
| カラム名 | 型 | 説明 |
|---|---|---|
| id | INTEGER (PK) | イベントDB ID |
| round_id | INTEGER (FK) | roundsテーブル参照 |
| timestamp | REAL | ラウンド開始からの経過秒数 |
| killer_id | TEXT (FK) | killerプレイヤーID |
| victim_id | TEXT (FK) | victimプレイヤーID |
| weapon_name | TEXT | 武器名（WEAPON_MAP変換済み） |
| time_delta | REAL | 前killからの経過秒数（Kill Pace） |
| location_x | REAL | X座標（任意） |
| location_y | REAL | Y座標（任意） |

---

## 注意事項 (Important Notes)

1. **初回セットアップ:** `playwright install chromium` を必ず実行すること
2. **レート制限:** rib.gg のTOS（利用規約）を遵守し、リクエスト間に適切な遅延を設けること（最低1.5秒）
3. **データキャッシュ:** 同一URLへの重複リクエストを避けるため、`data/raw/` にJSONをキャッシュすること
4. **武器マッピング:** 武器IDは整数値で返されるため、必ず `WEAPON_MAP` で名前に変換すること
5. **タイムスタンプ精度:** Kill タイムスタンプはミリ秒で取得される場合があるため、秒変換時は `/ 1000` を使用すること
