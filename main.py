# main.py
import os
import json
import logging
import aiohttp
import asyncio
import fcntl
from datetime import datetime, timedelta
from typing import Dict, Optional, List
from zoneinfo import ZoneInfo
import aiofiles
from fastapi import FastAPI, Request, HTTPException
from contextlib import asynccontextmanager
from linebot.v3.messaging import (
    Configuration, MessagingApi, ApiClient,
    ReplyMessageRequest, TextMessage
)
from linebot.v3.webhook import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.webhooks import MessageEvent, TextMessageContent

# --- 環境変数・定数の設定 ---
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "TMnn0HcemcDK42n9vG0WGD72KsmbMlQMEfkY3A7ypiDw0Hjl6vq5KnZaHyV18DmewHIWaaa3r67BsNM1l6V0lbbw48GjkCgUZ+ITajXHthCnrmBuYE56IaTnZXKK8Px2HcXNctTVm6MK6Jgc3BVD1AdB04t89/1O/w1cDnyilFU=")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "4b38a30fe40e6eb0c405a6f9bb233172")
TOGGL_USERS_FILE = "toggl_users.json"
USAGE_LOG_FILE = "usage_log.json"
MAX_REPORT_DAYS = 30
REMIND_INTERVAL = 30  # 1時間ごとにチェック

if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_CHANNEL_SECRET:
    raise ValueError("LINE_CHANNEL_ACCESS_TOKENとLINE_CHANNEL_SECRETの環境変数が設定されていません")

# --- ログ設定 ---
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler("bot.log"), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# --- タイムゾーン設定 ---
JST = ZoneInfo("Asia/Tokyo")
UTC = ZoneInfo("UTC")

# --- LINE設定 ---
configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# --- 非同期ファイルロッククラス ---
class AsyncFileLock:
    """非同期でファイルロックを管理するクラス"""
    def __init__(self, filename: str):
        self.filename = filename
        self.lockfile = f"{filename}.lock"
        self.file = None

    async def __aenter__(self):
        self.file = await aiofiles.open(self.lockfile, "w")
        while True:
            try:
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except BlockingIOError:
                await asyncio.sleep(0.1)

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        fcntl.flock(self.file, fcntl.LOCK_UN)
        await self.file.close()
        try:
            await aiofiles.os.remove(self.lockfile)
        except FileNotFoundError:
            pass

# --- 非同期JSON入出力関数 ---
async def async_load_json(filename: str) -> Dict:
    try:
        async with AsyncFileLock(filename):
            async with aiofiles.open(filename, "r") as f:
                content = await f.read()
                return json.loads(content) if content else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

async def async_save_json(filename: str, data: Dict):
    async with AsyncFileLock(filename):
        dir_name = os.path.dirname(filename)
        if dir_name:
            await aiofiles.os.makedirs(dir_name, exist_ok=True)
        async with aiofiles.open(filename, "w") as f:
            await f.write(json.dumps(data, indent=2, ensure_ascii=False))

# --- Toggl APIクライアント ---
class TogglClient:
    """Toggl APIとの通信を非同期で行うクライアントクラス"""
    BASE_URL = "https://api.track.toggl.com/api/v9"
    REPORTS_URL = "https://api.track.toggl.com/reports/api/v2/details"

    def __init__(self, api_key: str, workspace_id: str):
        self.api_key = api_key
        self.workspace_id = str(workspace_id)
        self._session = None

    async def __aenter__(self):
        self._session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, *exc):
        if self._session:
            await self._session.close()
        self._session = None

    async def _request(self, method: str, path: str, **kwargs):
        """Toggl APIへの共通リクエスト処理"""
        if not self._session:
            raise RuntimeError("Session not initialized")
        if path.startswith("http"):
            url = path
        else:
            url = f"{self.BASE_URL}{path}"
        try:
            async with self._session.request(
                method,
                url,
                auth=aiohttp.BasicAuth(self.api_key, "api_token"),
                **kwargs
            ) as resp:
                if resp.status != 200:
                    logger.error(f"Toggl API Error: {resp.status} {await resp.text()}")
                    return None
                return await resp.json()
        except aiohttp.ClientError as e:
            logger.error(f"Request failed: {str(e)}")
            return None

    async def get_current_entry(self) -> Optional[Dict]:
        """現在の計測エントリを取得"""
        return await self._request("GET", "/me/time_entries/current")

    async def get_projects(self) -> List[Dict]:
        """ワークスペース内のプロジェクト一覧を取得"""
        return await self._request("GET", f"/workspaces/{self.workspace_id}/projects") or []

    async def start_time_entry(self, project_name: str, description: str = "") -> Optional[Dict]:
        """指定したプロジェクトで計測を開始"""
        projects = await self.get_projects()
        project = next((p for p in projects if p.get("name", "").lower() == project_name.lower()), None)
        if not project:
            logger.error(f"Project not found: {project_name}")
            return None
        payload = {
            "created_with": "LINE Bot",
            "workspace_id": int(self.workspace_id),
            "description": description[:255],
            "project_id": project["id"],
            "start": datetime.now(UTC).isoformat(),
            "duration": -1
        }
        return await self._request("POST", f"/workspaces/{self.workspace_id}/time_entries", json=payload)

    async def stop_current_entry(self) -> Optional[Dict]:
        """現在進行中の計測エントリを停止"""
        current = await self.get_current_entry()
        if not current:
            return None
        return await self._request("PATCH", f"/workspaces/{self.workspace_id}/time_entries/{current['id']}/stop")

    async def get_report(self, start: datetime, end: datetime) -> List[Dict]:
        """指定期間の詳細レポートを取得"""
        params = {
            "workspace_id": self.workspace_id,
            "since": start.date().isoformat(),
            "until": end.date().isoformat(),
            "user_agent": "LINE-Toggl-Bot/1.1"
        }
        data = await self._request("GET", self.REPORTS_URL, params=params)
        return data.get("data", []) if data else []

# --- 同期的ファイル入出力関数 ---
def safe_load_json(filename: str) -> Dict:
    try:
        with open(filename, "r+", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_SH)
            try:
                content = f.read()
                return json.loads(content) if content else {}
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def safe_save_json(filename: str, data: Dict):
    with open(filename, "w+", encoding="utf-8") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            json.dump(data, f, indent=2, ensure_ascii=False)
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)

def get_toggl_credentials(user_id: str) -> Optional[Dict]:
    return safe_load_json(TOGGL_USERS_FILE).get(user_id)

def save_user_credentials(user_id: str, data: Dict):
    users = safe_load_json(TOGGL_USERS_FILE)
    users[user_id] = data
    safe_save_json(TOGGL_USERS_FILE, users)

def record_usage(user_id: str):
    usage = safe_load_json(USAGE_LOG_FILE)
    now = datetime.now().isoformat()
    usage[user_id] = usage.get(user_id, {"count": 0, "last_used": now})
    usage[user_id]["count"] += 1
    usage[user_id]["last_used"] = now
    safe_save_json(USAGE_LOG_FILE, usage)

# --- コマンド処理 ---
async def handle_register(user_id: str, args: list) -> str:
    if len(args) < 3:
        return "登録形式: register [ユーザー名] [APIキー] [ワークスペースID]"
    try:
        int(args[2])
    except ValueError:
        return "ワークスペースIDは数値で入力してください"
    save_user_credentials(user_id, {
        "user_name": args[0][:50],
        "api_key": args[1],
        "workspace_id": args[2]
    })
    return "✅ 登録完了"

async def handle_start(user_id: str, args: list) -> str:
    creds = get_toggl_credentials(user_id)
    if not creds:
        return "⚠️ まずregisterコマンドで登録してください"
    if not args:
        return "プロジェクト名を入力してください"
    try:
        async with TogglClient(creds["api_key"], creds["workspace_id"]) as toggl:
            await toggl.start_time_entry(args[0], " ".join(args[1:]))
            return f"⏱️ {args[0]} の計測を開始しました"
    except Exception as e:
        logger.error(f"Start error: {str(e)}")
        return f"🚨 開始エラー: {str(e)}"

async def handle_stop(user_id: str) -> str:
    creds = get_toggl_credentials(user_id)
    if not creds:
        return "⚠️ まずregisterコマンドで登録してください"
    try:
        async with TogglClient(creds["api_key"], creds["workspace_id"]) as toggl:
            stopped = await toggl.stop_current_entry()
            if stopped:
                dur = stopped.get("duration", 0)
                hours, mins = dur // 3600, (dur % 3600) // 60
                return f"⏹️ 計測停止 (時間: {hours}時間{mins}分)"
            return "ℹ️ 実行中の計測はありません"
    except Exception as e:
        logger.error(f"Stop error: {str(e)}")
        return f"🚨 停止エラー: {str(e)}"

async def handle_status(user_id: str) -> str:
    creds = get_toggl_credentials(user_id)
    if not creds:
        return "⚠️ まずregisterコマンドで登録してください"
    try:
        async with TogglClient(creds["api_key"], creds["workspace_id"]) as toggl:
            entry = await toggl.get_current_entry()
            if entry and entry.get("duration", 0) < 0:
                start = datetime.fromisoformat(entry["start"]).astimezone(JST)
                elapsed = datetime.now(JST) - start
                return (
                    "🔄 計測中\n"
                    f"プロジェクト: {entry.get('description', '未設定')}\n"
                    f"経過時間: {elapsed.seconds // 3600}時間{elapsed.seconds % 3600 // 60}分"
                )
            return "ℹ️ 現在計測中のプロジェクトはありません"
    except Exception as e:
        logger.error(f"Status error: {str(e)}")
        return f"🚨 ステータス取得エラー: {str(e)}"

def format_report(entries: List[Dict]) -> str:
    report = []
    daily_total = {}
    for entry in entries:
        try:
            start_str = entry.get('start')
            if not start_str:
                continue
            start = datetime.fromisoformat(start_str).astimezone(JST)
            date_str = start.date().isoformat()
            dur = int(entry.get('dur', 0))
            daily_total[date_str] = daily_total.get(date_str, 0) + dur
            report.append(
                f"{start.strftime('%m/%d %H:%M')} | "
                f"{entry.get('project', '未設定')[:20]} | "
                f"{entry.get('description', '未設定')[:30]} | "
                f"{dur//3600000:02d}:{(dur%3600000)//60000:02d}"
            )
        except Exception as e:
            logger.warning(f"Invalid entry: {str(e)}")
    summary = [
        f"📅 {date}: {total//3600000:02d}:{(total%3600000)//60000:02d}" 
        for date, total in sorted(daily_total.items())
    ]
    return (
        "📊 稼働レポート\n" +
        "\n".join(summary)[:2000] +
        "\n\n詳細:\n" +
        "\n".join(report[-20:])[:3000]
    )

async def handle_report(user_id: str, args: list) -> str:
    creds = get_toggl_credentials(user_id)
    if not creds:
        return "⚠️ まずregisterコマンドで登録してください"
    days = 1
    if args:
        try:
            days = min(int(args[0]), MAX_REPORT_DAYS)
        except ValueError:
            return "日数は数値で入力してください"
    try:
        async with TogglClient(creds["api_key"], creds["workspace_id"]) as toggl:
            end = datetime.now(JST)
            start = end - timedelta(days=days)
            entries = await toggl.get_report(start, end)
            return format_report(entries)
    except Exception as e:
        logger.error(f"Report error: {str(e)}")
        return f"🚨 レポート取得エラー: {str(e)}"

# --- バックグラウンドタスク：長時間稼働中のエントリをチェック ---
BACKGROUND_TASK_STARTED = False

async def check_long_entries():
    logger.info("【自動切り忘れ機能】バックグラウンドタスク開始")
    while True:
        try:
            users = safe_load_json(TOGGL_USERS_FILE)
            for user_id, creds in users.items():
                try:
                    async with TogglClient(creds["api_key"], creds["workspace_id"]) as toggl:
                        entry = await toggl.get_current_entry()
                        if entry and entry.get("duration", 0) < 0:
                            start_time = datetime.fromisoformat(entry["start"]).replace(tzinfo=UTC)
                            elapsed = (datetime.now(UTC) - start_time).total_seconds()
                            if elapsed > 10:  # 3時間以上稼働している場合
                                message = (
                                    f"⚠️ 長時間稼働中！\n"
                                    f"プロジェクト: {entry.get('description', '未設定')}\n"
                                    f"経過時間: {elapsed // 3600:.0f}時間{elapsed % 3600 // 60:.0f}分"
                                )
                                with ApiClient(configuration) as api_client:
                                    MessagingApi(api_client).push_message(
                                        user_id,
                                        [TextMessage(text=message)]
                                    )
                except Exception as e:
                    logger.error(f"User {user_id} check error: {str(e)}")
            await asyncio.sleep(REMIND_INTERVAL)
        except Exception as e:
            logger.error(f"Background task error: {str(e)}")
            await asyncio.sleep(60)

# --- FastAPIアプリ設定 ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    global BACKGROUND_TASK_STARTED
    if not BACKGROUND_TASK_STARTED:
        # バックグラウンドタスクを起動して切り忘れチェック機能を有効化
        asyncio.create_task(check_long_entries())
        BACKGROUND_TASK_STARTED = True
        logger.info("【自動切り忘れ機能】有効")
    yield

app = FastAPI(lifespan=lifespan)

@app.post("/webhook")
async def webhook(request: Request):
    signature = request.headers.get("X-Line-Signature", "")
    body = await request.body()
    try:
        handler.handle(body.decode(), signature)
    except InvalidSignatureError:
        raise HTTPException(status_code=400, detail="Invalid signature")
    return "OK"

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event: MessageEvent):
    user_id = event.source.user_id
    message = event.message.text.strip()
    record_usage(user_id)
    asyncio.create_task(process_command(
        user_id=user_id,
        message=message,
        reply_token=event.reply_token
    ))

async def process_command(user_id: str, message: str, reply_token: str):
    commands = message.lower().split()
    response = ""
    if commands:
        cmd = commands[0]
        try:
            if cmd == "register":
                response = await handle_register(user_id, commands[1:])
            elif cmd == "start":
                response = await handle_start(user_id, commands[1:])
            elif cmd == "stop":
                response = await handle_stop(user_id)
            elif cmd == "status":
                response = await handle_status(user_id)
            elif cmd == "report":
                response = await handle_report(user_id, commands[1:])
            elif cmd == "help":
                response = help_message()
        except Exception as e:
            logger.error(f"Command error: {str(e)}")
            response = f"⚠️ エラー: {str(e)}"
    with ApiClient(configuration) as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text=response)]
            )
        )

def help_message() -> str:
    return (
        "📚 使い方\n"
        "・登録: register [ユーザー名] [APIキー] [ワークスペースID]\n"
        "・開始: start <プロジェクト名> [説明]\n"
        " ===指定できる、プロジェクト名一覧 === \n"
        " < システム開発、LLM開発、バックエンド開発、フロントエンド開発、リサーチ、先方MT、資料作成、営業 > \n"
        "・停止: stop\n"
        "・状態: status\n"
        "・レポート: report [日数]\n"
        "・ヘルプ: help"
    )

    # 直接実行時に uvicorn でアプリを起動する（Azure Web App の Windows 環境での起動用）
if __name__ == '__main__':
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("main:app", host="0.0.0.0", port=port)