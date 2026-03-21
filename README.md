# tgport

Telegram bot wrapper for [Claude Code](https://docs.anthropic.com/en/docs/claude-code) CLI.
Telegramのボットを通じて、Claude Codeとリモートでやり取りできます。

## 機能

- TelegramメッセージからClaude CLIを実行
- ストリーミング出力（リアルタイムでメッセージを更新）
- セッション管理（チャットごとに会話コンテキストを維持）
- TelegramユーザーIDによるアクセス制御
- リクエストごとの予算・ターン数制限
- 画像・ドキュメントの送信対応
- JSONL形式の会話ログ（自動ローテーション・自動削除）

## 必要なもの

- Python 3.11+
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code)（インストール・認証済み）
- Telegram Bot Token（[@BotFather](https://t.me/BotFather) から取得）

## セットアップ

```bash
git clone https://github.com/delta-bonsoliel/tgport.git
cd tgport
python3 -m venv .venv
.venv/bin/pip install -e .
```

`.env.example` を `.env` にコピーして設定：

```bash
cp .env.example .env
```

| 変数 | 必須 | デフォルト | 説明 |
|------|------|-----------|------|
| `TELEGRAM_BOT_TOKEN` | Yes | - | BotFatherから取得したトークン |
| `ALLOWED_USER_IDS` | Yes | - | 許可するTelegramユーザーID（カンマ区切り） |
| `CLAUDE_WORK_DIR` | No | `~` | Claude CLIの作業ディレクトリ |
| `CLAUDE_MAX_TURNS` | No | `3` | リクエストあたりの最大ツール使用ターン数 |
| `CLAUDE_MAX_BUDGET_USD` | No | `1.0` | リクエストあたりの最大コスト（USD） |
| `CLAUDE_SKIP_PERMISSIONS` | No | `false` | `--dangerously-skip-permissions` を有効化 |
| `EDIT_INTERVAL` | No | `1.5` | メッセージ更新の間隔（秒） |
| `RESPONSE_TIMEOUT` | No | `300` | レスポンス待ちの最大時間（秒） |
| `LOG_DIR` | No | `~/workspace/projects/tgport/logs` | 会話ログの保存先ディレクトリ |
| `LOG_RETENTION_DAYS` | No | `14` | ログバックアップの保持日数 |
| `COST_DISPLAY` | No | `dollar` | コスト表示形式（`none` / `dollar` / `yen`） |

## 使い方

```bash
.venv/bin/python -m tgport
```

### ボットコマンド

- `/start` - ヘルプを表示
- `/new` - 新しい会話を開始（セッションをリセット）
- テキストメッセージ - Claudeに送信
- 画像・ドキュメント - ファイルを保存してClaudeに送信

### ログ

会話ログは `LOG_DIR` に `chat_{チャットID}.jsonl` 形式で保存されます。

- 日付が変わると、前日のログは `chat_{ID}_bk-YYYYMMDD.jsonl` に自動ローテーション
- `LOG_RETENTION_DAYS` 日以上前のバックアップは自動削除

## セキュリティ

- `ALLOWED_USER_IDS` に登録されたユーザーのみ操作可能。未許可のアクセスはログに記録されます。
- `CLAUDE_SKIP_PERMISSIONS` はデフォルトで**無効**です。有効にするとClaudeが確認なしにコマンドを実行します。サンドボックス環境でのみ使用してください。
- `CLAUDE_WORK_DIR` でファイルアクセスの範囲を定義します。必要最小限のディレクトリに設定してください。

## ライセンス

MIT
