# VALORANT Match Data Collector - Project Guidelines

## 概要 (Overview)

このプロジェクトは **rib.gg** からVALORANTの試合データをスクレイピングし、
「Kill Pace（キルペース）」や「武器使用率」を分析するデータパイプラインです。

rib.gg は Next.js 製アプリケーションのため、`__NEXT_DATA__` JSONブロブや
内部APIエンドポイントを優先的にリバースエンジニアリングします。

---

## プロジェクト構造 (Project Structure)

```
Data_collector/
├── CLAUDE.md              # このファイル（プロジェクトガイドライン）
├── requirements.txt       # Python依存関係
├── main.py                # エントリポイント
├── skills/
│   └── rib_api.py         # rib.gg 専用APIクライアント・データ取得ロジック
├── src/
│   ├── scraper.py         # ラウンドデータの反復収集
│   ├── analyzer.py        # Kill Delta / 武器統計の計算
│   └── database.py        # SQLite スキーマ定義・データ挿入
└── data/
    ├── raw/               # APIから取得した生JSONデータ
    └── processed/         # 処理済みCSV / SQLiteファイル
```

---

## 開発コマンド (Development Commands)

```bash
# 依存関係のインストール
pip install -r requirements.txt

# スクレイパーの実行（特定シリーズURLを引数で渡す）
python main.py --url "https://rib.gg/series/<series_id>"

# データベースのみ初期化
python -m src.database --init

# 分析レポートの出力
python -m src.analyzer --report
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

### ネットワーク
- 全HTTPリクエストに `requests.Session` を使用すること
- リトライロジック（最大3回、指数バックオフ）を実装すること
- User-Agent ヘッダーを適切に設定し、レート制限を遵守すること
- タイムアウトは常に明示的に設定すること（デフォルト: 30秒）

### データ処理
- DB挿入前に必ず `pandas.DataFrame` を経由して中間処理を行うこと
- 武器IDは `skills/rib_api.py` の `WEAPON_MAP` 辞書で名前に変換すること
- タイムスタンプは秒単位（float）で統一すること

### エラー処理
- ネットワークエラー: `requests.exceptions.RequestException` をキャッチしてリトライ
- パースエラー: `KeyError`, `TypeError` をキャッチして警告ログを出力し、スキップ
- DB エラー: `sqlalchemy.exc.SQLAlchemyError` をキャッチしてロールバック

---

## アーキテクチャ概要 (Architecture)

```
[rib.gg API / __NEXT_DATA__]
          |
          v
  skills/rib_api.py         ← APIフェッチ + 武器IDマッピング + Killイベント抽出
          |
          v
    src/scraper.py           ← ラウンド反復・生データ収集・data/raw/ に保存
          |
          v
    src/analyzer.py          ← Kill Delta計算・武器統計集計・DataFrame生成
          |
          v
    src/database.py          ← SQLite スキーマ初期化・DataFrame から一括挿入
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
| killer_id | TEXT (FK) | killersプレイヤーID |
| victim_id | TEXT (FK) | victimプレイヤーID |
| weapon_name | TEXT | 武器名（日本語変換済み） |
| time_delta | REAL | 前killからの経過秒数（Kill Pace） |
| location_x | REAL | X座標（任意） |
| location_y | REAL | Y座標（任意） |

---

## rib.gg APIエンドポイント（リバースエンジニアリング済み）

```
# シリーズ詳細（マップ一覧・チーム情報）
GET https://rib.gg/api/series/{series_id}

# 特定マップの試合詳細（ラウンド・Killイベント）
GET https://rib.gg/api/match/{match_id}

# プレイヤー情報
GET https://rib.gg/api/players/{player_id}
```

> **注意:** エンドポイントはリバースエンジニアリングに基づくため、
> サイト更新により変更される可能性があります。
> `__NEXT_DATA__` フォールバックも実装すること。

---

## 注意事項 (Important Notes)

1. **レート制限:** rib.gg のTOS（利用規約）を遵守し、リクエスト間に適切な遅延を設けること（最低1秒）
2. **データキャッシュ:** 同一URLへの重複リクエストを避けるため、`data/raw/` にJSONをキャッシュすること
3. **武器マッピング:** 武器IDは整数値で返されるため、必ず `WEAPON_MAP` で名前に変換すること
4. **タイムスタンプ精度:** Kill タイムスタンプはミリ秒で取得される場合があるため、秒変換時は `/ 1000` を使用すること
