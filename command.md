## 環境設定
```Bash
export LINE_CHANNEL_SECRET="your_channel_secret"
export LINE_CHANNEL_ACCESS_TOKEN="your_access_token"
export TEST_MODE="true"
```
## 依存関係インストール
pip install -r requirements.txt
### Webhook設定（ngrok使用）
```Bash
ngrok http 8000
```
### ローカルサーバーの起動

```python
python main.py
```

## サーバー起動
```
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

### ポート指定起動（例：8000番）
```
python main.py --port 8000
```
### プロセス確認コマンド（Mac/Linux）
```
sudo lsof -i :7000
```
### プロセス強制終了（例：PIDが1234の場合）
```
kill -9 1234
```

## 基本機能テスト

| テストケース | コマンド | 期待結果 |
|--------------|----------|----------|
| ヘルプ表示 | `help` | ヘルプメッセージ表示 |
| ユーザー登録 | `register user1 api_key123 456` | 登録成功メッセージ |
| 時間計測開始 | `start ProjectX` | 開始確認メッセージ |
| ステータス確認 | `status` | 現在の状態表示 |
| 計測停止 | `stop` | 停止確認メッセージ |

### Nginx設定例（本番環境）
```NGINX
server {
    listen 443 ssl;
    server_name yourdomain.com;

    ssl_certificate /path/to/cert.pem;
    ssl_certificate_key /path/to/privkey.pem;

    location /webhook {
        proxy_pass http://localhost:7000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
```

### セキュリティの確認コード該当箇所
```python
# 署名検証の実装箇所（既存コード）
@app.post("/webhook")
async def webhook(request: Request):
    signature = request.headers.get("X-Line-Signature", "")
    body = await request.body()
    
    try:
        handler.handle(body.decode(), signature)  # 自動で署名検証
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature")
```