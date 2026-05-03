# 翻訳済みRSSフィードを Reeder で受け取るセットアップ手順

完全無料・APIキー不要で、英語の公式RSS/Atomフィードを毎時自動翻訳して Reeder に配信する仕組みです。

## 仕組み

```
[公式RSS]
  ├─ anthropics/claude-code releases.atom
  ├─ openai/codex releases.atom
  ├─ Anthropic News
  └─ OpenAI News
        │
        ▼
  GitHub Actions（毎時15分）
        │  python translate.py
        │  - feedparser で取得
        │  - deep-translator (Google翻訳の無料エンドポイント) で日本語化
        │  - cache/<name>.json で再翻訳を回避
        ▼
  output/<name>.xml + index.html
        │
        ▼
  GitHub Pages（公開URL）
        │
        ▼
  Reeder に登録
```

## ステップ 1: GitHubに新規リポジトリを作る

1. <https://github.com/new> で新規リポジトリを作成
2. 名前: `rss-jp-translator`（任意）
3. **Public**（GitHub Actionsの実行時間が無制限になります）
4. 何もチェックせず Create

## ステップ 2: ファイルをアップロード

このフォルダ（`rss-jp-translator/`）の中身をリポジトリに push します。

```bash
cd rss-jp-translator

git init
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/<your-username>/rss-jp-translator.git
git push -u origin main
```

含まれているファイル:

```
rss-jp-translator/
├── .github/workflows/translate.yml   # 毎時実行する Actions
├── feeds.yaml                        # 翻訳したい元フィード一覧
├── translate.py                      # 翻訳スクリプト本体
├── requirements.txt                  # Python依存
├── .gitignore
└── SETUP.md                          # このファイル
```

## ステップ 3: GitHub Pages を有効化

1. リポジトリの **Settings → Pages** を開く
2. **Source**: 「GitHub Actions」 を選択（「Branch」 ではない）
3. 保存

## ステップ 4: Actions を初回実行

1. リポジトリの **Actions** タブを開く
2. 左の `Translate RSS feeds` を選択
3. 右上の **Run workflow** → **Run workflow**（main ブランチで）
4. 完了まで2〜5分待つ（フィード数と翻訳量による）

完了すると `output/` ディレクトリに翻訳済みXML、index.html が生成されコミットされます。

## ステップ 5: 配信URLを確認

1. 完了後、**Settings → Pages** で公開URLが出ます
   例: `https://<your-username>.github.io/rss-jp-translator/`
2. そこを開くと feed 一覧が表示されます
3. 各フィードのURL（例: `https://<user>.github.io/rss-jp-translator/claude-code.xml`）をコピー

## ステップ 6: Reeder に登録

1. Reeder で「フィードを追加」
2. URL欄にコピーしたXMLのURLを貼り付け
3. 完了！毎時自動で日本語化された記事が流れてきます

各記事には「日本語訳」と「Original (English)」の両方が含まれているので、原文も確認できます。

## カスタマイズ

### フィードを追加・削除

`feeds.yaml` を編集して push するだけ。次回のActions実行で反映されます。

```yaml
feeds:
  - name: my-blog
    url: https://example.com/feed.xml
    title: ブログ名（日本語）
```

### 実行頻度を変える

`.github/workflows/translate.yml` の `cron` 行:

```yaml
schedule:
  - cron: "15 * * * *"   # 毎時
  - cron: "0 */6 * * *"  # 6時間おき
  - cron: "0 0 * * *"    # 毎日0時 UTC
```

### 翻訳エンジン

`translate.py` の `_try_translate()` を編集すると別エンジンに切り替えられます:
- 既定: GoogleTranslator（無料・無認証）→ MyMemoryTranslator（フォールバック）
- 別案: DeepL Free API キーを取得して `DeeplTranslator` に置き換え（月50万文字無料）

## トラブルシューティング

### ソースフィードについて（事前確認済みの状況）

検証結果（2026-05時点）：

- **Claude Code GitHub Releases** (`https://github.com/anthropics/claude-code/releases.atom`) — 公式、確実に動作
- **OpenAI Codex GitHub Releases** (`https://github.com/openai/codex/releases.atom`) — 公式、確実に動作
- **OpenAI News** (`https://openai.com/news/rss.xml`) — OpenAIが提供する公式RSS（過去に一度消えたが復活）
- **Anthropic News** — **公式RSSは存在しません**（`/rss.xml`, `/news/rss.xml`, `/feed.xml`, `/atom.xml` すべて404を直接確認済み）

Anthropicについては feeds.yaml で `https://rsshub.app/anthropic/news`（RSSHubの公式コミュニティルート）を使うように設定してあります。

### RSSHub のレート制限に当たった場合

公開 `rsshub.app` インスタンスは混雑時にレート制限がかかることがあります。Anthropicフィードが空になり続ける場合、`feeds.yaml` で代替URLに切り替えてください：

```yaml
- name: anthropic-news
  url: https://raw.githubusercontent.com/taobojlen/anthropic-rss-feed/main/anthropic_news_rss.xml
  title: Anthropic News（日本語・非公式ミラー経由）
```

これは個人が GitHub Actions で定期生成しているXMLで、GitHubのCDN経由なので非常に安定しています（更新頻度は数日に1回程度）。

### 自分で RSSHub をホスティングする選択肢

公開インスタンスを避けたい場合、Railway等で無料/低額でRSSHubを自前デプロイ可能：<https://railway.com/deploy/deploy-rsshub>。デプロイ後 `https://<your-app>.up.railway.app/anthropic/news` を feeds.yaml に指定してください。

### Google翻訳が突然失敗する

deep-translator は Google翻訳のWebエンドポイントを使うため、稀にレート制限や仕様変更で動かなくなることがあります。フォールバックの MyMemory が呼ばれて多少品質が落ちます。長期的に安定させたい場合は DeepL Free API キーを取得して切り替えてください（月50万文字まで無料）。

### Actions が動かない

Public リポジトリであることを確認。Settings → Actions → General → 「Workflow permissions」で「Read and write permissions」が有効か確認。

## コスト

- GitHub Public Repo の Actions: 無制限・無料
- GitHub Pages: 無料（Soft limit 100GB/月のbandwidth）
- 翻訳エンジン: deep-translator は無料（無認証）
- → **完全無料で運用可能**
