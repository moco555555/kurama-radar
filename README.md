# KURAMA MARKET RADAR v3

市場の歪みを1枚のマップに可視化する監視ボット。対象10銘柄: XAUUSD / USDJPY / EURUSD / GBPUSD / GBPJPY / N225 / NAS100 / US30 / BTCUSD / ETHUSD。

## 何が見えるか

- **歪みマップ**: 縦=歪みスコア(0-100)、横=トレンド方向、バブル大きさ=ボラレジーム、残像=過去5日の軌跡。相関断裂ペアは赤点線で結ばれる
- **歪みの指紋**: バブルをタップすると、全根拠(MA乖離・長期RSI・マクロ残差・COT・ボラ・相関)を過去2年パーセンタイルで表示
- **類似局面の照合**: 現在の指紋と類似度86%以上の過去局面を検索し、その後5営業日の中期MA回帰率・平均逆行幅を統計表示

## セットアップ

1. このフォルダを新規GitHubリポジトリにpush
2. リポジトリ Settings → Pages → Source を `main` ブランチの `/docs` に設定
3. (任意) Settings → Secrets → Actions に `DISCORD_WEBHOOK_URL` を追加
   → 歪みスコアが70を超えた瞬間だけ根拠付きでDiscord通知が飛ぶ
4. Actions タブから `KURAMA MARKET RADAR` を手動実行して初回生成
5. 以後、平日毎時自動更新。ダッシュボードURLは `https://<user>.github.io/<repo>/`

## ローカル実行

```
pip install -r requirements.txt
python kurama_radar.py --out docs/index.html   # 本番データ
python kurama_radar.py --demo                  # 合成データで動作確認
```

## データ出典(すべて無料)

- 価格/ボラ/金利: Yahoo Finance (yfinance)
- 実質金利 DFII10: FRED (APIキー不要)
- COT投機筋ポジション: CFTC 週次レポート

## 注意

- 統計は過去実績であり将来の結果を保証するものではない
- COT/FREDの取得に失敗した場合、その根拠は自動でスコアから除外され動作は継続する
- GitHub Pagesは公開URLになる。ダッシュボード上のMA表記は「中期MA/長期MA」に抽象化済み

## v3の追加機能

- スコアカード列(タップで指紋切替) / 発光バブル+警戒ゾーンのパルスアニメ
- 推薦ポジションカード: 方向・エントリーゾーン・TP(中期MA)・SL(類似局面90%tile逆行)・R/R・ランク(A/B/C)を機械生成。RSI過熱や類似局面サンプル不足で自動降格し、降格理由も表示
- 根拠チップ: 極端値(80%tile超/20%tile未満)の根拠を最大4つ+類似局面統計+減点を並べる
