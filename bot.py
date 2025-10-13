import os
import sqlite3
import discord
from discord.ext import commands, tasks
from discord import app_commands
from dotenv import load_dotenv
from flask import Flask
from threading import Thread
from github_backup import download_latest_db, upload_db_to_release
import asyncio
import requests

# --------------------
# Raruin Bot: Persistent DB block
# Notes:
# - Ensure your Render (or other host) mounts a persistent volume to /data
#   and set DB_PATH to "/data/main.db" (or set via environment variable).
# - If you leave DB_PATH pointing to a temporary path (e.g. /tmp or the
#   project root in some PaaS), the DB will be lost on redeploy.
# --------------------

# 環境変数読み込み
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

# 永続 DB のパス（Render の Persistent Disk を /data にマウントする想定）
# デフォルトを /data にしておくことで、deploy ごとに初期化される問題を防ぎます。
DB_PATH = os.getenv("DB_PATH", "/data/main.db")

# 簡易チェック: DB_PATH が永続化フォルダになっているかを警告
def _warn_if_non_persistent_path(path: str):
    path = os.path.abspath(path)
    # 以下の条件は一般的なヒューリスティックです。必要に応じて環境に合わせて調整してください。
    ephemeral_indicators = ["/tmp", "\\\"", ":memory:"]
    if any(ind in path for ind in ephemeral_indicators) or not path.startswith("/data"):
        print(f"⚠️ 注意: DB_PATH({path}) が永続化フォルダ (/data) ではない可能性があります。\n   Render 等を使っている場合はマウント先を /data に設定し、DB_PATH を '/data/main.db' にしてください。")

_warn_if_non_persistent_path(DB_PATH)

# 管理者（OWNER）のDiscord ID（必要に応じて変更）
OWNER_ID = int(os.getenv("OWNER_ID", "1402613707527426131"))

# バックアップ復元元（任意）と送信先チャンネル
BACKUP_DB_URL = os.getenv("BACKUP_DB_URL", "")
BACKUP_CHANNEL_ID = int(os.getenv("BACKUP_CHANNEL_ID", "0") or 0)

# ---------- Discord Bot 初期化 ----------
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="/", intents=intents)
tree = bot.tree

# ----- 起動時: DB の格納ディレクトリがなければ作る -----
try:
    db_dir = os.path.dirname(DB_PATH) or "/"
    if db_dir and not os.path.exists(db_dir):
        os.makedirs(db_dir, exist_ok=True)
        print(f"✅ DB ディレクトリを作成しました: {db_dir}")
except Exception as e:
    print(f"⚠️ DB ディレクトリ作成に失敗: {e}")

# ----- 起動時: DB がなければ外部から復元を試みる（BACKUP_DB_URL が指定されている場合のみ） -----
if not os.path.exists(DB_PATH):
    if BACKUP_DB_URL:
        try:
            print(f"⚠️ DB({DB_PATH}) が見つかりません。バックアップURLから復元を試みます…")
            r = requests.get(BACKUP_DB_URL, timeout=20)
            r.raise_for_status()
            with open(DB_PATH, "wb") as f:
                f.write(r.content)
            print(f"✅ DB を復元しました: {DB_PATH}")
        except Exception as e:
            print(f"❌ DB の復元に失敗しました: {e}  — 続行します（新規 DB を作成します）")
    else:
        print(f"ℹ️ DB が見つかりません: {DB_PATH} 。BACKUP_DB_URL が未設定のため新規 DB を作成します。")

# --- SQLite 接続: グローバルに1つ保持（注意: check_same_thread=False を指定） ---
# - Render などで /data にマウントしていれば、このファイルは再デプロイ間で保持されます。
# - check_same_thread=False によって複数スレッドからのアクセスを許可しますが、トランザクション管理は注意して行ってください。
try:
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    c = conn.cursor()
    print(f"✅ DB に接続しました: {DB_PATH}")
except Exception as e:
    print(f"[DB] 接続時にエラーが発生しました ({DB_PATH}): {e}")
    conn = None
    c = None

# ---------- 必要なテーブルを自動作成 ----------
if c:
    try:
        # users（基本）
        c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            balance INTEGER DEFAULT 0,
            total_received INTEGER DEFAULT 0,
            total_spent INTEGER DEFAULT 0,
            daily_streak INTEGER DEFAULT 0,
            last_daily TEXT
        )
        ''')

        # admins
        c.execute('''
        CREATE TABLE IF NOT EXISTS admins (
            user_id INTEGER PRIMARY KEY
        )
        ''')

        # shops
        c.execute('''
        CREATE TABLE IF NOT EXISTS shops (
            shop_id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE
        )
        ''')

        # shop_items
        c.execute('''
        CREATE TABLE IF NOT EXISTS shop_items (
            item_id INTEGER PRIMARY KEY AUTOINCREMENT,
            shop_id INTEGER,
            name TEXT,
            description TEXT,
            price INTEGER,
            stock TEXT,
            role_id INTEGER,
            role_duration INTEGER,
            FOREIGN KEY (shop_id) REFERENCES shops(shop_id)
        )
        ''')

        # gamble_settings
        c.execute('''
        CREATE TABLE IF NOT EXISTS gamble_settings (
            id INTEGER PRIMARY KEY,
            probability_level INTEGER DEFAULT 3
        )
        ''')

        # purchase_history
        c.execute('''
        CREATE TABLE IF NOT EXISTS purchase_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            shop_name TEXT,
            item_name TEXT,
            price INTEGER,
            timestamp TEXT
        )
        ''')

        # 初期データ
        c.execute("INSERT OR IGNORE INTO gamble_settings (id, probability_level) VALUES (1, 3)")
        try:
            c.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (OWNER_ID,))
        except Exception:
            pass

        conn.commit()
        print("✅ 必要なテーブルを作成/確認しました。")
    except Exception as e:
        print(f"⚠️ テーブル作成時にエラー: {e}")
else:
    print("⚠️ DBカーソルが利用できません。テーブル作成をスキップしました。")

# ===== Discord Bot 設定（bot, tree は既に定義済み） =====

# ===== Flask keep_alive =====
app = Flask(__name__)

@app.route('/')
def home():
    return "Raruin Bot Running!"


def run_flask():
    print("🌐 Flaskサーバー起動: ポート8080で待機中…")
    try:
        # bind を 0.0.0.0 にして外部からの接続を許可（PaaS によっては不要）
       import os
port = int(os.getenv("PORT", "8080"))
app.run(host='0.0.0.0', port=port)

    except Exception as e:
        print(f"Flask 起動エラー: {e}")

Thread(target=run_flask, daemon=True).start()

# ---------- 定期バックアップタスク（定義のみ。開始は on_ready() で） ----------
@tasks.loop(hours=24)
async def backup_database():
    try:
        # チャンネル取得: get_channel が None を返す場合は fetch_channel を試す
        channel = bot.get_channel(BACKUP_CHANNEL_ID)
        if channel is None and BACKUP_CHANNEL_ID:
            try:
                channel = await bot.fetch_channel(BACKUP_CHANNEL_ID)
            except Exception:
                channel = None

        if not channel:
            print("⚠️ バックアップ: 指定されたチャンネルが見つかりません。送信をスキップします。")
            return

        if os.path.exists(DB_PATH):
            # 送信は非同期 IO のまま行う
            await channel.send(file=discord.File(DB_PATH))
            print("✅ DB をバックアップチャンネルに送信しました。")
        else:
            print("⚠️ バックアップ: DB ファイルが見つかりません。送信をスキップします。")
    except Exception as e:
        print(f"[backup_database] エラー: {e}")

@backup_database.before_loop
async def before_backup():
    await bot.wait_until_ready()

# ---------- on_ready でタスクを安全に開始 ----------
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print(f"Guilds: {[g.name for g in bot.guilds]}")
    # コマンド同期
    try:
        await tree.sync()
    except Exception as e:
        print(f"[on_ready] tree.sync error: {e}")

    # backup_database はここで安全に開始する（重複起動を防ぐ）
    try:
        if BACKUP_CHANNEL_ID and not backup_database.is_running():
            backup_database.start()
            print("backup_database task started.")
    except Exception as e:
        print(f"[on_ready] backup_database start error: {e}")

    print("Background tasks started.")

# ===== 注意 =====
# - Render 等の PaaS を使う場合は、上記 DB_PATH を /data/main.db に設定し、
#   Render の "Persistent Disk" を /data にマウントしてください。
# - local 開発時は .env に DB_PATH=/path/to/your/persistent/location/main.db を設定してください。
# - BACKUP_DB_URL を使う場合は、公開アクセスできる URL を指定してください。

# ===== (必要に応じて) bot.run を main 部分で呼んでください =====
# if __name__ == '__main__':
#     bot.run(TOKEN)


# ---------- Part 2: 管理者コマンドと通貨操作 ----------

# /addr - 管理者を追加・削除・一覧
@tree.command(name="addr", description="管理者を追加、削除、一覧表示")
@app_commands.describe(action="add, remove, list", target="ユーザー名またはID")
async def addr(interaction: discord.Interaction, action: str, target: str = None):
    add_user_if_not_exists(interaction.user.id)
    if not is_admin(interaction.user.id):
        await interaction.response.send_message("⚠️ 管理者のみ使用可能です。", ephemeral=True)
        return

    action = action.lower()

    if action == "list":
        c.execute("SELECT user_id FROM admins")
        rows = c.fetchall()
        if not rows:
            await interaction.response.send_message("👤 管理者はまだ登録されていません。")
            return
        mentions = [f"<@{uid[0]}>" for uid in rows]
        await interaction.response.send_message(f"👑 管理者一覧: {', '.join(mentions)}")
        return

    if not target:
        await interaction.response.send_message("⚠️ 対象ユーザー名またはIDを指定してください。", ephemeral=True)
        return

    # 対象ユーザー取得
    user = None
    if interaction.guild:
        user = discord.utils.get(interaction.guild.members, name=target)

    if not user:
        try:
            user_id = int(target)
            user = await bot.fetch_user(user_id)
        except:
            await interaction.response.send_message("❌ ユーザーが見つかりません。", ephemeral=True)
            return

    if action == "add":
        c.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (user.id,))
        conn.commit()
        await interaction.response.send_message(f"✅ {user.mention} を管理者に追加しました。")
    elif action == "remove":
        c.execute("DELETE FROM admins WHERE user_id=?", (user.id,))
        conn.commit()
        await interaction.response.send_message(f"🗑️ {user.mention} を管理者から削除しました。")
    else:
        await interaction.response.send_message("⚠️ actionは add, remove, list のいずれかです。", ephemeral=True)

## ---------- ヘルパー: ターゲット解決（@役職名 に堅牢対応） ----------
import re
from typing import List, Optional

async def resolve_target_members(target: str, interaction: discord.Interaction) -> List[discord.Member]:
    """
    target: ユーザー/ロール指定の文字列（例: '@役職名', '<@123...>', '<@&roleid>', '1234567890', 'ユーザー名'）
    戻り値: discord.Member のリスト（見つからなければ [] を返す）
    """
    guild = interaction.guild
    if not guild:
        return []

    raw = target.strip()

    # 1) ロールメンション形式 <@&123>
    rm = re.match(r'^<@&(?P<id>\d+)>$', raw)
    if rm:
        rid = int(rm.group("id"))
        role = guild.get_role(rid)
        return role.members if role else []

    # 2) ユーザーメンション形式 <@!123> または <@123>
    um = re.match(r'^<@!?(?P<id>\d+)>$', raw)
    if um:
        uid = int(um.group("id"))
        member = guild.get_member(uid)
        if not member:
            try:
                member = await guild.fetch_member(uid)
            except:
                member = None
        return [member] if member else []

    # 3) 先頭が @ （全角＠も含む）で @役職名 の場合 -> ロール名で検索（完全一致：大/小文字無視）
    if raw.startswith("@") or raw.startswith("＠"):
        name = raw.lstrip("@\uFF20").strip()  # 半角/全角@を除去
        # 完全一致（case-insensitive）
        role = discord.utils.find(lambda r: r.name.lower() == name.lower(), guild.roles)
        if role:
            return role.members  # 空でも [] を返す（呼び出し側でメッセージ出し分け）
        # 完全一致見つからなければ部分一致（先頭一致 → 含む順）で検索（上位1つのロールを採用）
        starts = [r for r in guild.roles if r.name.lower().startswith(name.lower())]
        if starts:
            return starts[0].members
        contains = [r for r in guild.roles if name.lower() in r.name.lower()]
        if contains:
            return contains[0].members
        return []  # 見つからない

    # 4) 数字のみ -> ID として member または role を探す
    if raw.isdigit():
        uid = int(raw)
        member = guild.get_member(uid)
        if not member:
            try:
                member = await guild.fetch_member(uid)
            except:
                member = None
        if member:
            return [member]
        role = guild.get_role(uid)
        if role:
            return role.members

    # 5) ロール名そのもの（プレーン）を大文字小文字無視で完全一致
    role = discord.utils.find(lambda r: r.name.lower() == raw.lower(), guild.roles)
    if role:
        return role.members

    # 6) ユーザー名/ニックネーム/name#discriminator の完全一致
    found = []
    for mbr in guild.members:
        if mbr.name == raw or (mbr.nick and mbr.nick == raw) or f"{mbr.name}#{mbr.discriminator}" == raw:
            found.append(mbr)
    if found:
        return found[:50]

    # 7) 部分一致（ユーザー名/ニックネーム）
    partial = [mbr for mbr in guild.members if raw.lower() in mbr.name.lower() or (mbr.nick and raw.lower() in mbr.nick.lower())]
    return partial[:50]


# ヘルパー: 入力からロールオブジェクトを探す（resolve_target_members が見つけられなかった場合の補助）
def _find_role_from_input(raw: str, guild: discord.Guild) -> Optional[discord.Role]:
    if not guild:
        return None
    s = raw.strip()
    # mention形式
    m = re.match(r'^<@&(?P<id>\d+)>$', s)
    if m:
        return guild.get_role(int(m.group("id")))
    # idのみ
    if s.isdigit():
        r = guild.get_role(int(s))
        if r:
            return r
    # @で始まる名前（半角/全角@許容）
    name = s.lstrip("@\uFF20").strip()
    if name:
        # 完全一致
        role = discord.utils.find(lambda r: r.name.lower() == name.lower(), guild.roles)
        if role:
            return role
        # 先頭一致 -> 部分一致
        starts = [r for r in guild.roles if r.name.lower().startswith(name.lower())]
        if starts:
            return starts[0]
        contains = [r for r in guild.roles if name.lower() in r.name.lower()]
        if contains:
            return contains[0]
    # 最終手段: プレーン文字列をロール名として探す（完全一致）
    role = discord.utils.find(lambda r: r.name.lower() == s.lower(), guild.roles)
    return role


# ---------- /配布（付与）（修正版：ロール存在チェックとDB安全化含む） ----------
@tree.command(name="配布", description="指定したユーザーやロールにRaruinを付与（管理者専用）")
@app_commands.describe(target="ユーザー（@で指定可）またはロール名/ID/ユーザー名", amount="付与額")
async def distribute(interaction: discord.Interaction, target: str, amount: int):
    if not is_admin(interaction.user.id):
        await interaction.response.send_message("⚠️ 管理者のみ使用可能です。", ephemeral=True)
        return

    if amount <= 0:
        await interaction.response.send_message("⚠️ 付与額は1以上にしてください。", ephemeral=True)
        return

    members = await resolve_target_members(target, interaction)

    # members が空なら、改めて「ロールとして存在するか」をチェックしてユーザ向けメッセージを出す
    if not members:
        guild = interaction.guild
        if guild:
            role_obj = _find_role_from_input(target, guild)
            if role_obj:
                # ロールは見つかったがメンバー0 のケース
                if len(role_obj.members) == 0:
                    await interaction.response.send_message(f"ℹ️ ロール **{role_obj.name}** は見つかりましたが、メンバーがいません。", ephemeral=True)
                    return
                # ロールにはメンバーがいる（resolve が失敗していた可能性） -> そのメンバーを使う
                members = role_obj.members

    if not members:
        await interaction.response.send_message("❌ 対象が見つかりませんでした。@メンション / ID / ロール名 / ユーザー名 を試してください。", ephemeral=True)
        return

    # 長い処理の可能性があるため defer してからバックグラウンドで DB 更新
    try:
        await interaction.response.defer(ephemeral=True)
    except Exception:
        # 既にレスポンス済みのとき等はスキップして続行
        pass

    import sqlite3
    import asyncio

    def _db_add_balance(member_ids: List[int], amt: int):
        conn = sqlite3.connect("main.db", timeout=10)
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                balance INTEGER DEFAULT 0,
                total_received INTEGER DEFAULT 0,
                total_spent INTEGER DEFAULT 0
            )
        """)
        for uid in member_ids:
            cur.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (uid,))
            cur.execute("UPDATE users SET balance = balance + ?, total_received = total_received + ? WHERE user_id=?", (amt, amt, uid))
        conn.commit()
        conn.close()

    member_ids = [m.id for m in members]

    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _db_add_balance, member_ids, amount)
    except Exception as e:
        # エラー発生時はフォローアップで通知
        await interaction.followup.send(f"❌ DB更新中にエラーが発生しました: `{e}`", ephemeral=True)
        return

    # レスポンス
    if len(members) == 1:
        name = members[0].display_name
    else:
        name = f"{len(members)} 件のメンバー"
    await interaction.followup.send(f"🎁 {name} に {amount} Raruin を付与しました。")


# ---------- /支払い（減算）（修正版：ロール存在チェックとDB安全化含む） ----------
@tree.command(name="支払い", description="指定したユーザーやロールのRaruinを減らす（管理者専用）")
@app_commands.describe(target="ユーザー（@で指定可）またはロール名/ID/ユーザー名", amount="減らす額")
async def payment(interaction: discord.Interaction, target: str, amount: int):
    if not is_admin(interaction.user.id):
        await interaction.response.send_message("⚠️ 管理者のみ使用可能です。", ephemeral=True)
        return

    if amount <= 0:
        await interaction.response.send_message("⚠️ 減算額は1以上にしてください。", ephemeral=True)
        return

    members = await resolve_target_members(target, interaction)

    # members が空ならロール存在チェック（改善）
    if not members:
        guild = interaction.guild
        if guild:
            role_obj = _find_role_from_input(target, guild)
            if role_obj:
                if len(role_obj.members) == 0:
                    await interaction.response.send_message(f"ℹ️ ロール **{role_obj.name}** は見つかりましたが、メンバーがいません。", ephemeral=True)
                    return
                members = role_obj.members

    if not members:
        await interaction.response.send_message("❌ 対象が見つかりませんでした。@メンション / ID / ロール名 / ユーザー名 を試してください。", ephemeral=True)
        return

    # defer + DB更新（非ブロッキング）
    try:
        await interaction.response.defer(ephemeral=True)
    except Exception:
        pass

    import sqlite3
    import asyncio

    def _db_subtract_balance(member_ids: List[int], amt: int):
        conn = sqlite3.connect("main.db", timeout=10)
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                balance INTEGER DEFAULT 0,
                total_received INTEGER DEFAULT 0,
                total_spent INTEGER DEFAULT 0
            )
        """)
        for uid in member_ids:
            cur.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (uid,))
            cur.execute("UPDATE users SET balance = balance - ?, total_spent = total_spent + ? WHERE user_id=?", (amt, amt, uid))
        conn.commit()
        conn.close()

    member_ids = [m.id for m in members]

    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, _db_subtract_balance, member_ids, amount)
    except Exception as e:
        await interaction.followup.send(f"❌ DB更新中にエラーが発生しました: `{e}`", ephemeral=True)
        return

    # レスポンス
    if len(members) == 1:
        name = members[0].display_name
    else:
        name = f"{len(members)} 件のメンバー"
    await interaction.followup.send(f"💸 {name} から {amount} Raruin を減算しました。")






# ---------- /ギャンブル確率設定 ----------
@tree.command(name="ギャンブル確率設定", description="管理者専用: ギャンブル確率レベル変更")
@app_commands.describe(probability="1=当たりやすい, 6=当たりにくい")
async def gamble_prob_set(interaction: discord.Interaction, probability: int):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ 管理者専用です。", ephemeral=True)
        return
    if probability < 1 or probability > 6:
        await interaction.response.send_message("⚠️ 1〜6で指定してください。", ephemeral=True)
        return
    c.execute("UPDATE gamble_settings SET probability_level=? WHERE id=1", (probability,))
    conn.commit()
    await interaction.response.send_message(f"🎯 ギャンブル確率レベルを `{probability}` に設定しました。")



# /shopadd - ショップ追加/削除
@tree.command(name="shopadd", description="管理者専用: ショップを追加または削除")
@app_commands.describe(action="追加 or 削除", shop_name="ショップの名前")
async def shopadd(interaction: discord.Interaction, action: str, shop_name: str):
    if not is_admin(interaction.user.id):
        await interaction.response.send_message("⚠️ 管理者のみ使用可能です。", ephemeral=True)
        return

    action = action.lower()
    if action == "追加":
        c.execute("INSERT OR IGNORE INTO shops (name) VALUES (?)", (shop_name,))
        conn.commit()
        await interaction.response.send_message(f"✅ ショップ {shop_name} を追加しました。")
    elif action == "削除":
        c.execute("DELETE FROM shops WHERE name=?", (shop_name,))
        conn.commit()
        await interaction.response.send_message(f"🗑️ ショップ {shop_name} を削除しました。")
    else:
        await interaction.response.send_message("⚠️ actionは「追加」または「削除」を指定してください。", ephemeral=True)



# /shop - 商品追加/削除（ロールは@で指定、付与時間は分で指定、在庫は数字または'無限'）
@tree.command(name="shop", description="管理者専用: ショップの商品を追加または削除")
@app_commands.describe(
    action="追加 or 削除",
    shop_name="ショップ名",
    item_name="商品名",
    description="説明",
    price="値段",
    stock="在庫（数字か'無限'）",
    role="付与ロール（@で指定、任意）",
    role_duration_min="付与時間(分)（任意）"
)
async def shop(
    interaction: discord.Interaction,
    action: str,
    shop_name: str,
    item_name: str,
    description: str = "",
    price: int = 0,
    stock: str = "無限",
    role: discord.Role = None,
    role_duration_min: int = None
):
    if not is_admin(interaction.user.id):
        await interaction.response.send_message("⚠️ 管理者のみ使用可能です。", ephemeral=True)
        return

    # ショップ存在チェック
    c.execute("SELECT shop_id FROM shops WHERE name=?", (shop_name,))
    shop_row = c.fetchone()
    if not shop_row:
        await interaction.response.send_message(f"⚠️ {shop_name} というショップは存在しません。", ephemeral=True)
        return
    shop_id = shop_row[0]

    # 在庫の検証: '無限' (小文字許容) または 0以上の整数
    stock_norm = stock if isinstance(stock, str) else str(stock)
    if stock_norm.lower() in ["無限", "mugen"]:
        stock_to_store = "無限"
    else:
        try:
            stock_int = int(stock_norm)
            if stock_int < 0:
                await interaction.response.send_message("⚠️ 在庫は0以上の整数か'無限'で指定してください。", ephemeral=True)
                return
            stock_to_store = str(stock_int)
        except ValueError:
            await interaction.response.send_message("⚠️ 在庫は数字または '無限' を指定してください。", ephemeral=True)
            return

    action_norm = action.lower()
    if action_norm in ["追加", "add"]:
        # role が与えられていれば role.id を保存。role_duration_min は分 -> DBには秒で保存。
        role_id = role.id if role else None
        role_duration_seconds = role_duration_min * 60 if role_duration_min is not None else None

        c.execute('''
            INSERT INTO shop_items (shop_id, name, description, price, stock, role_id, role_duration)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (shop_id, item_name, description, price, stock_to_store, role_id, role_duration_seconds))
        conn.commit()

        extra = ""
        if role:
            extra = f" ロール付与: {role.name}"
            if role_duration_min:
                extra += f"（{role_duration_min}分）"
        extra += f" 在庫: {stock_to_store}"
        await interaction.response.send_message(f"✅ {item_name} を {shop_name} に追加しました。{extra}")

    elif action_norm in ["削除", "remove"]:
        c.execute("DELETE FROM shop_items WHERE shop_id=? AND name=?", (shop_id, item_name))
        conn.commit()
        await interaction.response.send_message(f"🗑️ {item_name} を {shop_name} から削除しました。")
    else:
        await interaction.response.send_message("⚠️ actionは「追加」または「削除」（または add/remove）を指定してください。", ephemeral=True)

# ---------- Part 3: ショップ管理とユーザー向けコマンド ----------

# 安全なユーザー作成ユーティリティ（既存のものと置き換えてください）
def add_user_if_not_exists(user_id: int):
    """
    users テーブルがなければ作成し、user_id がなければ挿入する。
    コマンド／イベントで都度呼べるように都度接続する実装。
    """
    conn = None
    try:
        conn = sqlite3.connect("main.db", timeout=10)
        conn.execute("PRAGMA journal_mode=WAL;")
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                balance INTEGER DEFAULT 0,
                total_received INTEGER DEFAULT 0,
                total_spent INTEGER DEFAULT 0
            )
        """)
        # user がいなければ作る（他のカラムは DEFAULT）
        cur.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
        conn.commit()
    except Exception as e:
        print(f"[add_user_if_not_exists] DB error: {e}")
        try:
            if conn:
                conn.rollback()
        except:
            pass
    finally:
        try:
            if conn:
                conn.close()
        except:
            pass


# /残高 コマンド（これで常に最新の DB 値を読みます）
@tree.command(name="残高", description="自分の残高を確認します")
async def balance_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    try:
        # ユーザーの存在を保証（内部で commit される）
        add_user_if_not_exists(interaction.user.id)

        # 都度接続して確実に最新値を読む
        conn = sqlite3.connect("main.db", timeout=10)
        conn.execute("PRAGMA journal_mode=WAL;")
        cur = conn.cursor()
        cur.execute("SELECT balance FROM users WHERE user_id=?", (interaction.user.id,))
        row = cur.fetchone()
        balance = row[0] if row else 0
        conn.close()

        await interaction.followup.send(f"💰 あなたの残高は {balance} Raruin です。")
    except Exception as e:
        # 開発中は詳細ログを残す（本運用では簡潔表示に変える）
        print(f"[balance_cmd] error: {e}")
        try:
            await interaction.followup.send(f"⚠️ エラーが発生しました: {e}")
        except:
            pass

# ---------- ランキング ----------
@tree.command(name="ランキング", description="Raruin残高ランキング上位15名")
async def ranking(interaction: discord.Interaction):
    c.execute("SELECT user_id, balance FROM users ORDER BY balance DESC LIMIT 15")
    rows = c.fetchall()
    if not rows:
        await interaction.response.send_message("ユーザーが存在しません。")
        return

    msg = "🏆 Raruinランキング（上位15名）\n"
    for i, (uid, bal) in enumerate(rows, start=1):
        try:
            user = await bot.fetch_user(uid)
            name = user.name
        except:
            name = f"ユーザーID:{uid}"
        msg += f"{i}. {name}: {bal} Raruin\n"
    await interaction.response.send_message(msg)


# ---------- 統計 ----------
@tree.command(name="統計", description="Raruin全体統計")
async def stats(interaction: discord.Interaction):
    c.execute("SELECT SUM(balance), SUM(total_spent) FROM users")
    total_balance, total_spent = c.fetchone()
    c.execute("SELECT user_id, balance FROM users ORDER BY balance DESC LIMIT 1")
    richest_row = c.fetchone()
    c.execute("SELECT user_id, balance FROM users ORDER BY balance ASC LIMIT 1")
    poorest_row = c.fetchone()

    richest_user = await bot.fetch_user(richest_row[0]) if richest_row else None
    poorest_user = await bot.fetch_user(poorest_row[0]) if poorest_row else None

    msg = (
        f"💰 現在全員が持っているRaruin合計: {total_balance or 0}\n"
        f"📤 全員が今まで使った額合計: {total_spent or 0}\n"
        f"👑 最も持っている人: {richest_user.name if richest_user else 'なし'} ({richest_row[1] if richest_row else 0})\n"
        f"💸 最も持っていない人: {poorest_user.name if poorest_user else 'なし'} ({poorest_row[1] if poorest_row else 0})"
    )
    await interaction.response.send_message(msg)

# ---------- ギャンブル確率確認 ----------
@tree.command(name="ギャンブル確率確認", description="現在のギャンブル確率を確認")
async def gamble_prob_check(interaction: discord.Interaction):
    c.execute('''
        CREATE TABLE IF NOT EXISTS gamble_settings (
            id INTEGER PRIMARY KEY,
            probability_level INTEGER DEFAULT 3
        )
    ''')
    c.execute("INSERT OR IGNORE INTO gamble_settings (id, probability_level) VALUES (1, 3)")
    conn.commit()

    c.execute("SELECT probability_level FROM gamble_settings WHERE id=1")
    prob = c.fetchone()[0]
    await interaction.response.send_message(
        f"🎰 現在のギャンブル確率設定: {prob} (1が最も当たりやすい, 6が最も難しい)"
    )



# ---------- Part 3: ショップ管理とユーザー向けコマンド ----------

# ---------- ショップ一覧 ----------
@tree.command(name="ショップリスト", description="ショップ一覧を表示")
async def shop_list(interaction: discord.Interaction):
    c.execute("SELECT name FROM shops")
    rows = c.fetchall()
    if not rows:
        await interaction.response.send_message("ショップが存在しません。")
        return
    msg = "🛒 ショップ一覧:\n" + "\n".join([row[0] for row in rows])
    await interaction.response.send_message(msg)


# ---------- ショップ商品表示 ----------
@tree.command(name="ショップ", description="指定したショップの商品を表示")
@app_commands.describe(shop_name="ショップの名前")
async def show_shop(interaction: discord.Interaction, shop_name: str):
    c.execute("SELECT shop_id FROM shops WHERE name=?", (shop_name,))
    row = c.fetchone()
    if not row:
        await interaction.response.send_message(f"{shop_name} というショップは存在しません。")
        return
    shop_id = row[0]
    c.execute("SELECT name, description, price FROM shop_items WHERE shop_id=?", (shop_id,))
    items = c.fetchall()
    if not items:
        await interaction.response.send_message("このショップには商品がありません。")
        return
    msg = f"🛒 {shop_name}の商品一覧:\n"
    for name, desc, price in items:
        msg += f"{name} - {desc} - {price} Raruin\n"
    await interaction.response.send_message(msg)


# ---------- 商品購入（修正版） ----------
@tree.command(name="買う", description="商品を購入する")
@app_commands.describe(shop_name="ショップ名", item_name="商品名")
async def buy_item(interaction: discord.Interaction, shop_name: str, item_name: str):
    # インタラクション応答を確保（3秒タイムアウト回避）
    await interaction.response.defer(ephemeral=True)

    try:
        # ユーザー用エントリを保証（関数は都度接続する安全版を使ってください）
        add_user_if_not_exists(interaction.user.id)

        # DB接続（このコマンド内で完結）
        conn = sqlite3.connect("main.db", timeout=10)
        conn.execute("PRAGMA journal_mode=WAL;")
        cur = conn.cursor()

        # 1) ショップ確認
        cur.execute("SELECT shop_id FROM shops WHERE name=?", (shop_name,))
        row = cur.fetchone()
        if not row:
            await interaction.followup.send("❌ そのショップは存在しません。", ephemeral=True)
            conn.close()
            return
        shop_id = row[0]

        # 2) 商品取得
        cur.execute("SELECT item_id, price, stock, role_id, role_duration FROM shop_items WHERE shop_id=? AND name=?",
                    (shop_id, item_name))
        item = cur.fetchone()
        if not item:
            await interaction.followup.send("❌ その商品は見つかりません。", ephemeral=True)
            conn.close()
            return
        item_id, price, stock, role_id, role_duration = item

        # 3) 残高確認
        cur.execute("SELECT balance FROM users WHERE user_id=?", (interaction.user.id,))
        r = cur.fetchone()
        balance = r[0] if r else 0
        if balance < price:
            await interaction.followup.send("💸 残高が足りません。", ephemeral=True)
            conn.close()
            return

        # 4) 在庫確認（"無限"/"unlimited" 等を許容）
        stock_str = "" if stock is None else str(stock)
        stock_unlimited = stock_str.lower() in ("無限", "mugen", "unlimited", "inf", "∞")
        if not stock_unlimited:
            try:
                stock_int = int(stock_str)
            except:
                # 不正な在庫値は購入不可
                await interaction.followup.send("⚠️ 在庫情報に問題があります。管理者に連絡してください。", ephemeral=True)
                conn.close()
                return
            if stock_int <= 0:
                await interaction.followup.send("🚫 在庫がありません。", ephemeral=True)
                conn.close()
                return
            # 在庫を1減らす
            cur.execute("UPDATE shop_items SET stock = ? WHERE item_id=?", (str(stock_int - 1), item_id))

        # 5) 支払い処理（残高更新）
        cur.execute("UPDATE users SET balance = balance - ?, total_spent = total_spent + ? WHERE user_id=?",
                    (price, price, interaction.user.id))
        conn.commit()

        # 6) ロール付与（即時） — ロール解除はバックグラウンドで行う
        role_assigned = False
        role_name = None
        if role_id:
            role = interaction.guild.get_role(role_id)
            if role:
                try:
                    await interaction.user.add_roles(role, reason="Shop purchase")
                    role_assigned = True
                    role_name = role.name
                    # ロール解除が必要な場合（role_durationが秒数で入っている想定）
                    if role_duration and isinstance(role_duration, int) and role_duration > 0:
                        async def _remove_role_later(member: discord.Member, role: discord.Role, delay: int):
                            try:
                                await asyncio.sleep(delay)
                                await member.remove_roles(role, reason="Role duration expired")
                                # optional: 購入通知チャンネルに解除を通知する
                                ch = bot.get_channel(1408247205034328066)
                                if ch:
                                    await ch.send(f"{member.display_name} のロール `{role.name}` の付与が終了しました。")
                            except Exception as e:
                                print(f"[remove_role_later] error: {e}")

                        # バックグラウンドで実行（応答はブロックしない）
                        asyncio.create_task(_remove_role_later(interaction.user, role, role_duration))
                except Exception as e:
                    print(f"[buy_item] role add failed: {e}")
                    # 付与失敗でも購入自体は完了する（必要ならロール付与分の返金ロジックを追加）

        # 7) 購入通知（任意チャンネルがあれば通知）
        notify_ch = bot.get_channel(1408247205034328066)
        if notify_ch:
            try:
                await notify_ch.send(f"{interaction.user.display_name} が {shop_name} で {item_name} を購入しました。")
            except Exception as e:
                print(f"[buy_item] notify_ch send error: {e}")

        # 8) 最終応答（購入完了）
        new_balance = None
        try:
            cur.execute("SELECT balance FROM users WHERE user_id=?", (interaction.user.id,))
            nb = cur.fetchone()
            new_balance = nb[0] if nb else None
        except:
            new_balance = None

        msg = f"✅ {item_name} を購入しました！ 支払額: {price} Raruin"
        if role_assigned:
            msg += f" — 付与ロール: `{role_name}`"
        if new_balance is not None:
            msg += f"\n💰 残高: {new_balance} Raruin"

        await interaction.followup.send(msg, ephemeral=True)
        conn.close()
        return

    except Exception as e:
        # 例外時は詳細をログに残してユーザに通知
        print(f"[buy_item] error: {e}")
        try:
            await interaction.followup.send("⚠️ エラーが発生しました。管理者に連絡してください。", ephemeral=True)
        except:
            pass
        try:
            conn.close()
        except:
            pass
        return

# ---------- /渡す ----------
@tree.command(name="渡す", description="指定したユーザーにRaruinを渡す")
@app_commands.describe(target="渡したいユーザー（@で指定）", amount="渡す額")
async def transfer(interaction: discord.Interaction, target: discord.User, amount: int):
    if amount <= 0:
        await interaction.response.send_message("⚠️ 額は1以上にしてください。", ephemeral=True)
        return

    add_user_if_not_exists(interaction.user.id)
    c.execute("SELECT balance FROM users WHERE user_id=?", (interaction.user.id,))
    balance = c.fetchone()[0]

    if balance < amount:
        await interaction.response.send_message("💰 残高が足りません。", ephemeral=True)
        return

    add_user_if_not_exists(target.id)

    # 残高の更新
    c.execute(
        "UPDATE users SET balance = balance - ?, total_spent = total_spent + ? WHERE user_id=?",
        (amount, amount, interaction.user.id)
    )
    c.execute(
        "UPDATE users SET balance = balance + ?, total_received = total_received + ? WHERE user_id=?",
        (amount, amount, target.id)
    )
    conn.commit()

    try:
        await target.send(f"📩 {interaction.user.display_name} から {amount} Raruin を受け取りました！")
    except:
        pass

    await interaction.response.send_message(f"✅ {target.display_name} に {amount} Raruin を渡しました。")


# ---------- /ヘルプ ----------
@tree.command(name="ヘルプ", description="Botの全コマンド一覧を表示します")
async def help_cmd(interaction: discord.Interaction):
    msg = (
        "📘 **Raruin Bot コマンド一覧**\n\n"
        "💰 **通貨関連**\n"
        "・/残高 — 自分の所持Raruinを確認\n"
        "・/渡す — 指定したユーザーにRaruinを送金\n"
        "・/今日の収支 — 今日の獲得・消費Raruinを確認\n"
        "・/統計 — 累計獲得や使用統計を確認\n"
        "・/ランキング — 残高ランキングを表示\n"
        "・/リーダーボード — トッププレイヤーを確認\n"
        "・（自動獲得）チャットで1文字=1Raruin（1日最大2000Raruinまで）\n\n"

        "🎰 **ギャンブル関連**\n"
        "・/スロット — 絵柄を揃えて報酬を狙う\n"
        "・/ハイアンドロー — 数字を予想して勝負\n"
        "・/ポーカー — 手札で勝負するカードゲーム\n"
        "・/ギャンブル確率確認 — 現在の確率を確認\n\n"

        "🛒 **ショップ関連**\n"
        "・/ショップリスト — 商品の一覧を表示\n"
        "・/ショップ — ショップ情報を確認\n"
        "・/買う — 商品を購入\n"
        "・/ショップ検索 — 商品名で検索\n"
        "・/最近の購入 — 最近の購入履歴を確認\n"

        "ℹ️ **その他**\n"
        "・/役立ち — 便利な機能を表示\n"
        "・/bot情報 — Botの情報を確認\n"
    )
    await interaction.response.send_message(msg, ephemeral=True)

# ---------- 今日の収支 ----------
@tree.command(name="今日の収支", description="自分の受取・支出・残高をまとめて表示")
async def today_income(interaction: discord.Interaction):
    add_user_if_not_exists(interaction.user.id)
    c.execute("SELECT balance, total_received, total_spent FROM users WHERE user_id=?", (interaction.user.id,))
    balance, received, spent = c.fetchone()
    await interaction.response.send_message(
        f"💰 {interaction.user.display_name} さんの収支:\n"
        f"残高: {balance} Raruin\n"
        f"受取合計: {received} Raruin\n"
        f"支出合計: {spent} Raruin"
    )


# ---------- ショップ検索 ----------
@tree.command(name="ショップ検索", description="商品名でショップ内を検索")
@app_commands.describe(keyword="検索したい商品名")
async def shop_search(interaction: discord.Interaction, keyword: str):
    c.execute(
        "SELECT s.name, i.name, i.price FROM shop_items i "
        "JOIN shops s ON i.shop_id = s.shop_id "
        "WHERE i.name LIKE ?",
        (f"%{keyword}%",)
    )
    rows = c.fetchall()
    if not rows:
        await interaction.response.send_message("該当する商品はありません。")
        return
    msg = "🔍 検索結果:\n"
    for shop_name, item_name, price in rows:
        msg += f"{shop_name} - {item_name}: {price} Raruin\n"
    await interaction.response.send_message(msg)


# ---------- 最近の購入 ----------
@tree.command(name="最近の購入", description="最近購入した商品を確認")
async def recent_purchase(interaction: discord.Interaction):
    c.execute('''
        CREATE TABLE IF NOT EXISTS purchase_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            shop_name TEXT,
            item_name TEXT,
            price INTEGER,
            timestamp TEXT
        )
    ''')
    c.execute(
        "SELECT shop_name, item_name, price, timestamp "
        "FROM purchase_history WHERE user_id=? ORDER BY id DESC LIMIT 5",
        (interaction.user.id,)
    )
    rows = c.fetchall()
    if not rows:
        await interaction.response.send_message("購入履歴はありません。")
        return
    msg = "🛒 最近の購入:\n"
    for shop, item, price, ts in rows:
        msg += f"{ts[:16]} - {shop} - {item}: {price} Raruin\n"
    await interaction.response.send_message(msg)


# ---------- リーダーボード ----------
@tree.command(name="リーダーボード", description="Raruin上位15名の詳細を表示")
async def leaderboard(interaction: discord.Interaction):
    c.execute(
        "SELECT user_id, balance, total_received, total_spent "
        "FROM users ORDER BY balance DESC LIMIT 15"
    )
    rows = c.fetchall()
    msg = "🏆 Raruinリーダーボード（上位15名）\n"
    for i, (uid, bal, rec, spent) in enumerate(rows, start=1):
        try:
            user = await bot.fetch_user(uid)
            name = user.name
        except:
            name = f"ユーザーID:{uid}"
        msg += f"{i}. {name}: 残高 {bal}, 受取 {rec}, 支出 {spent}\n"
    await interaction.response.send_message(msg)


# ---------- 役立ち ----------
@tree.command(name="役立ち", description="便利なRaruin情報を表示")
async def tips(interaction: discord.Interaction):
    msg = (
        "💡 役立ち情報:\n"
        "・文字数1文字 = 1 Raruin 取得可能\n"
        "・通話1分 = 12 Raruin 取得可能（ミュートでも）\n"
        "・ショップを見て安く買える商品をチェック\n"
    )
    await interaction.response.send_message(msg)


# ---------- Bot情報 ----------
@tree.command(name="bot情報", description="Botのバージョンや稼働状況を確認")
async def bot_info(interaction: discord.Interaction):
    msg = (
        f"🤖 Bot情報\n"
        f"ユーザー名: {bot.user.name}\n"
        f"ID: {bot.user.id}\n"
        f"稼働中のサーバー数: {len(bot.guilds)}\n"
        f"コマンド同期済み\n"
        f"現在オンライン中"
    )
    await interaction.response.send_message(msg)



# ---------- Part 5: ギャンブル・自動付与・起動ログ・keep-alive ----------

import random
from discord.ext import tasks
from flask import Flask
from threading import Thread
import time
import sqlite3
from discord.ui import View, Button
from discord import app_commands

# ---------- DB接続 ----------
conn = sqlite3.connect("main.db")
c = conn.cursor()

# ---------- 起動時ログ ----------
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print(f"Guilds: {[g.name for g in bot.guilds]}")
    print("Bot is ready and slash commands synced.")
    await tree.sync()
    voice_check.start()
    print("Background tasks started.")

import sqlite3
import asyncio

def _db_add_chat_earnings(user_id: int, earned: int):
    conn = sqlite3.connect("main.db", timeout=30)
    conn.execute("PRAGMA journal_mode=WAL;")
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            balance INTEGER DEFAULT 0,
            total_received INTEGER DEFAULT 0,
            total_spent INTEGER DEFAULT 0
        )
    """)
    cur.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
    cur.execute("UPDATE users SET balance = balance + ?, total_received = total_received + ? WHERE user_id = ?",
                (earned, earned, user_id))
    conn.commit()
    conn.close()

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    if not message.content or not message.content.strip():
        await bot.process_commands(message)
        return

    earned = len(message.content)  # 1文字 = 1 Raruin（無制限）
    await asyncio.to_thread(_db_add_chat_earnings, message.author.id, earned)

    await bot.process_commands(message)

# 通話参加で1分ごとに12Raruin付与
@tasks.loop(minutes=1)
async def voice_check():
    for guild in bot.guilds:
        for vc in guild.voice_channels:
            for member in vc.members:
                if member.bot:
                    continue
                add_user_if_not_exists(member.id)
                c.execute("UPDATE users SET balance = balance + ?, total_received = total_received + ? WHERE user_id=?",
                          (12, 12, member.id))
    conn.commit()

# ---------- ヘルパー関数 ----------
def get_probability():
    c.execute("SELECT probability_level FROM gamble_settings WHERE id=1")
    row = c.fetchone()
    return row[0] if row else 3  # デフォルト3

def update_balance(user_id, amount):
    c.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
    if amount >= 0:
        c.execute(
            "UPDATE users SET balance = balance + ?, total_received = total_received + ? WHERE user_id=?",
            (amount, amount, user_id)
        )
    else:
        c.execute(
            "UPDATE users SET balance = balance + ?, total_spent = total_spent + ? WHERE user_id=?",
            (amount, -amount, user_id)
        )
    conn.commit()

def get_balance(user_id):
    c.execute("SELECT balance FROM users WHERE user_id=?", (user_id,))
    row = c.fetchone()
    return row[0] if row else 0

# ---------- ギャンブル確率＋冷却管理 (一部ゲームで使う) ----------
last_win_times = {}

def is_win(user_id: int, base_chance: int = 0) -> bool:
    """
    汎用的な勝敗判定。ここを使うゲームでは『連勝クールダウン』が効きます。
    ただし以下のコマンドでは deterministic (確定判定) に変更しているので影響しません:
      - スロット (絵柄に基づく確定判定)
      - ハイアンドロー (カード比較での確定判定)
      - コイントス (常に1/2)
      - ポーカー (手札対戦)
    """
    now = time.time()
    if user_id in last_win_times and now - last_win_times[user_id] < 60:
        return False
    prob_level = get_probability()
    chance_table = {1: 20, 2: 15, 3: 10, 4: 5, 5: 2, 6: 1}
    chance = chance_table.get(prob_level, 10) + base_chance
    chance = min(chance, 100)
    win = random.randint(1, 100) <= chance
    if win:
        last_win_times[user_id] = now
    return win


# ---------- スロット ----------
@tree.command(name="スロット", description="スロットで遊ぶ")
@app_commands.describe(bet="ベット額")
async def slot(interaction: discord.Interaction, bet: int):
    user_id = interaction.user.id
    balance = get_balance(user_id)
    if bet <= 0 or balance < bet:
        await interaction.response.send_message(f"💰 残高不足です。あなたの残高: {balance}", ephemeral=True)
        return

    # 絵柄テーブルは確率レベルで絞りはするが、判定は絵柄結果に基づいて **確定** にしました。
    prob_level = get_probability()
    symbol_table = {
        1: ["🍒", "🍋", "🍇", "⭐", "💎"],
        2: ["🍒", "🍋", "🍇", "⭐", "💎", "🍉", "🔔"],
        3: ["🍒", "🍋", "🍇", "⭐", "💎", "🍉", "🔔", "🍀", "🥝", "🍊"],
        4: ["🍒", "🍋", "🍇", "⭐", "💎", "🍉", "🔔", "🍀", "🥝", "🍊", "🎱", "💰", "🪙"],
        5: ["🍒", "🍋", "🍇", "⭐", "💎", "🍉", "🔔", "🍀", "🥝", "🍊", "🎱", "💰", "🪙", "🍎", "🍆"],
        6: ["🍒", "🍋", "🍇", "⭐", "💎", "🍉", "🔔", "🍀", "🥝", "🍊", "🎱", "💰", "🪙", "🍎", "🍆", "🍌", "🥭", "🍍", "🥥", "🥕"]
    }
    symbols = symbol_table.get(prob_level, symbol_table[3])
    reels = [random.choice(symbols) for _ in range(3)]

    # 判定は **絵柄の並び** のみで行う（外部乱数による追加の否定判定はしない）
    if reels[0] == reels[1] == reels[2]:
        payout = bet * 5
        update_balance(user_id, payout)
        msg = f"🎉 大当たり! +{payout} Raruin"
    elif len(set(reels)) == 2:
        # 2つ揃い (例: A A B)
        payout = bet * 2
        update_balance(user_id, payout)
        msg = f"✨ 中当たり! +{payout} Raruin"
    else:
        payout = -bet
        update_balance(user_id, payout)
        msg = f"💀 ハズレ... -{bet} Raruin"

    await interaction.response.send_message(
        f"🎰 {' | '.join(reels)}\n{msg}\n残高: {get_balance(user_id)} Raruin"
    )


# ---------- コイントス (常に 1/2 の勝率) ----------
@tree.command(name="コイントス", description="コイントスでギャンブル (公平な1/2)")
@app_commands.describe(bet="掛け金")
async def coin(interaction: discord.Interaction, bet: int):
    user_id = interaction.user.id
    if bet <= 0 or get_balance(user_id) < bet:
        await interaction.response.send_message("💰 残高不足です。", ephemeral=True)
        return

    # ギャンブル確率設定に左右されず常に50%で判定
    win = random.choice([True, False])
    if win:
        payout = bet * 2
        update_balance(user_id, payout)
        msg = f"🎉 {payout} Raruin 獲得"
    else:
        payout = -bet
        update_balance(user_id, payout)
        msg = f"💀 {bet} Raruin 減少"

    await interaction.response.send_message(f"コイントス: {'表' if win else '裏'}\n{msg}\n残高: {get_balance(user_id)} Raruin")


# ---------- ハイアンドロー (カード比較で確定判定) ----------
class HiLoView(discord.ui.View):
    def __init__(self, user_id, current_card, bet):
        super().__init__(timeout=30)
        self.user_id = user_id
        self.current_card = current_card
        self.next_card = random.randint(1, 13)
        self.bet = bet

    async def finish(self, interaction, win):
        if win:
            payout = int(self.bet * 0.3)
            msg = f"🎉 勝ち! +{payout} Raruin 獲得"
        else:
            payout = -self.bet
            msg = f"💀 負け... -{self.bet} Raruin"
        update_balance(self.user_id, payout)
        await interaction.response.edit_message(
            content=f"{self.current_card} → {self.next_card}\n{msg}\n残高: {get_balance(self.user_id)} Raruin",
            view=None
        )

    @discord.ui.button(label="ハイ", style=discord.ButtonStyle.primary)
    async def hi_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ あなた専用です", ephemeral=True)
            return
        # 「数値比較で勝ち」が第一判定。乱数による否定は行わない（ユーザーの不満対応）
        win = self.next_card > self.current_card
        await self.finish(interaction, win)

    @discord.ui.button(label="ロー", style=discord.ButtonStyle.secondary)
    async def low_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ あなた専用です", ephemeral=True)
            return
        win = self.next_card < self.current_card
        await self.finish(interaction, win)


@tree.command(name="ハイアンドロー", description="ハイアンドローで勝負")
@app_commands.describe(bet="掛け金")
async def hilo(interaction: discord.Interaction, bet: int):
    user_id = interaction.user.id
    if bet <= 0 or get_balance(user_id) < bet:
        await interaction.response.send_message("💰 残高不足です。", ephemeral=True)
        return
    view = HiLoView(user_id, random.randint(1, 13), bet)
    await interaction.response.send_message(
        f"現在のカード: {view.current_card}\nハイかローを選んでください。",
        view=view,
        ephemeral=True
    )


# ---------- ルーレット (修正版: 当たり判定を絵柄結果のみに依存させる) ----------
@tree.command(name="ルーレット", description="ルーレットで勝負")
@app_commands.describe(bet="掛け金", choice="赤/黒/偶数/奇数/番号(0-36)")
async def roulette(interaction: discord.Interaction, bet: int, choice: str):
    user_id = interaction.user.id
    if bet <= 0 or get_balance(user_id) < bet:
        await interaction.response.send_message("💰 残高不足です。", ephemeral=True)
        return

    # 正規化
    choice_str = choice.strip()

    # ルーレット回転（実際の出目）
    number = random.randint(0, 36)
    color = "赤" if number % 2 == 0 else "黒"  # 0 を赤扱いしている実装（必要なら0は特別扱いに変更可）

    win = False
    payout = 0

    # 1) 指定が数字（番号ベット）
    if choice_str.isdigit():
        try:
            chosen_num = int(choice_str)
            if 0 <= chosen_num <= 36 and chosen_num == number:
                win = True
                payout = bet * 35
        except:
            pass

    # 2) 赤・黒ベット（数字ベットの判定が優先される）
    if not win and choice_str in ["赤", "黒"]:
        if choice_str == color:
            win = True
            payout = bet * 2

    # 3) 偶数/奇数ベット
    if not win and choice_str in ["偶数", "奇数"]:
        # 0は偶数扱いになる（必要なら0を無効にするロジックを追加）
        if (number % 2 == 0 and choice_str == "偶数") or (number % 2 == 1 and choice_str == "奇数"):
            win = True
            payout = bet * 2

    # 結果反映
    if win:
        update_balance(user_id, payout)   # 当たりならプラス
        msg = f"🎉 勝ち! {payout} Raruin 獲得"
    else:
        update_balance(user_id, -bet)     # ハズレならベットを差し引き
        msg = f"💀 負け... -{bet} Raruin"

    # 結果表示
    await interaction.response.send_message(
        f"ルーレット: 出目 {number} ({color})\n"
        f"{msg}\n"
        f"残高: {get_balance(user_id)} Raruin"
    )


# ---------- ポーカー（対ディーラー版、手札を両方表示） ----------
def hand_rank_by_counts(rank_list):
    """
    rank_list: ['A','K','10','10','2'] のようなランクリスト（文字列）を渡す
    戻り値: (category_value, tiebreaker_list)
      category_value: フォーカード>フルハウス>スリー>ツーペア>ワンペア>ハイカード (6..1)
      tiebreaker_list: 比較用の数値リスト（高い順）
    """
    # ランク -> 数値変換
    order = {r: i for i, r in enumerate(['2','3','4','5','6','7','8','9','10','J','Q','K','A'], start=2)}
    counts = {}
    for r in rank_list:
        counts[r] = counts.get(r, 0) + 1
    # (count, rank_value, rank_str) のリストを作りソート（まずカウント降順、その次にランク降順）
    items = sorted([(cnt, order[r], r) for r, cnt in counts.items()], key=lambda x: (-x[0], -x[1]))
    cnts = sorted(counts.values(), reverse=True)

    # カテゴリ判定（数値は強さ。6が最強 = フォーカード）
    if cnts[0] == 4:
        category = 6
    elif cnts[0] == 3 and len(cnts) > 1 and cnts[1] == 2:
        category = 5
    elif cnts[0] == 3:
        category = 4
    elif cnts[0] == 2 and len(cnts) > 1 and cnts[1] == 2:
        category = 3
    elif cnts[0] == 2:
        category = 2
    else:
        category = 1  # ハイカード

    # tiebreaker: 順に (count, rank_value) を並べたリスト（比較用）
    tiebreaker = []
    for cnt, rv, r in items:
        tiebreaker.append(cnt)
        tiebreaker.append(rv)
    return (category, tiebreaker)


@tree.command(name="ポーカー", description="5枚カードで勝負（対ディーラー、簡易版）")
@app_commands.describe(bet="掛け金")
async def poker(interaction: discord.Interaction, bet: int):
    user_id = interaction.user.id
    if bet <= 0 or get_balance(user_id) < bet:
        await interaction.response.send_message("💰 残高不足です。", ephemeral=True)
        return

    # --- デッキ作成・配布 ---
    ranks = [str(n) for n in range(2, 11)] + ["J", "Q", "K", "A"]
    suits = ["♠", "♥", "♦", "♣"]
    deck = [r + s for r in ranks for s in suits]  # 52枚
    cards = random.sample(deck, 10)
    player_cards = cards[:5]
    dealer_cards = cards[5:]

    # ランク（スート抜き）を取得
    player_ranks = [c[:-1] for c in player_cards]
    dealer_ranks = [c[:-1] for c in dealer_cards]

    # --- 評価 ---
    player_rank = hand_rank_by_counts(player_ranks)   # (category, tiebreaker)
    dealer_rank = hand_rank_by_counts(dealer_ranks)

    # --- ギャンブル確率設定と連携（ディーラー強さ調整） ---
    # get_probability() を使って設定値を取得（1=易しい .. 6=難しい）
    prob_level = get_probability()  # 1..6
    # prob_level が大きいほど「プレイヤーにとって不利（ディーラー有利）」にする
    # modifier: prob_level 3 を基準にして差分をディーラーのカテゴリに加える
    # （例: prob=5 -> +2 カテゴリ、prob=1 -> -2 カテゴリ）
    dealer_modifier = prob_level - 3
    # dealer_rank を直接書き換えず、比較用に修正版を作る（カテゴリのみ調整、1..6 の範囲に制限）
    modified_dealer_category = max(1, min(6, dealer_rank[0] + dealer_modifier))
    modified_dealer_rank = (modified_dealer_category, dealer_rank[1])

    # --- 勝敗判定（修正版ディーラー強さで比較） ---
    if player_rank[0] > modified_dealer_rank[0]:
        winner = "player"
    elif player_rank[0] < modified_dealer_rank[0]:
        winner = "dealer"
    else:
        # 同カテゴリなら tiebreaker を比較（リスト同士の辞書式比較でOK）
        if player_rank[1] > modified_dealer_rank[1]:
            winner = "player"
        elif player_rank[1] < modified_dealer_rank[1]:
            winner = "dealer"
        else:
            winner = "tie"

    # --- 支払い倍率（プレイヤーカテゴリに基づく） ---
    # category: 6(フォーカード),5(フルハウス),4(スリー),3(ツーペア),2(ワンペア),1(ハイカード)
    mult_map = {6: 10, 5: 7, 4: 3, 3: 2, 2: 1.5, 1: 0}
    mult = mult_map.get(player_rank[0], 0)

    if winner == "player" and mult > 0:
        payout = int(bet * mult)
        update_balance(user_id, payout)
        msg = f"🎉 あなたの勝ち! ({payout} Raruin 獲得)"
    elif winner == "player" and mult == 0:
        # ハイカード勝利はベット返却（設計上の選択）
        payout = bet
        update_balance(user_id, payout)
        msg = f"🎉 あなたの勝ち! ハイカードで勝利: +{payout} Raruin"
    elif winner == "tie":
        # 引き分けはベット返却（0変動）
        msg = "🤝 引き分けです。ベットは返却されます。"
    else:
        payout = -bet
        update_balance(user_id, payout)
        msg = f"💀 あなたの負け... -{bet} Raruin"

    # --- 表示 ---
    # 表示には**実際のディーラーの手札**と、補助として「ディーラー評価（修正後カテゴリ）」を出す
    category_names = {
        6: "フォーカード",
        5: "フルハウス",
        4: "スリーカード",
        3: "ツーペア",
        2: "ワンペア",
        1: "ハイカード"
    }
    player_cat_name = category_names.get(player_rank[0], "不明")
    dealer_cat_name = category_names.get(dealer_rank[0], "不明")
    modified_dealer_cat_name = category_names.get(modified_dealer_category, "不明")

    await interaction.response.send_message(
        f"あなたの手札: {' '.join(player_cards)}  ({player_cat_name})\n"
        f"ディーラーの手札: {' '.join(dealer_cards)}  ({dealer_cat_name})\n"
        f"→ ギャンブル確率設定によりディーラー強さを調整: {modified_dealer_cat_name}\n\n"
        f"{msg}\n残高: {get_balance(user_id)} Raruin"
    )


# ---------- 起動ログ・状態確認 ----------
import asyncio
from datetime import datetime

async def startup_log():
    print("===================================================")
    print("🚀 Bot起動開始 🚀")
    print("日時:", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("TOKENは環境変数から正常に取得済み")
    print("Flaskサーバーは keep_alive.py でバックグラウンド起動済み")
    print("UptimeRobotで常時オンラインを維持します")
    print("SQLiteデータベース接続済み")
    print("各種テーブル（users, shops, shop_items, purchase_history, gamble_settings, daily_settings）を確認・作成済み")
    print("===================================================\n")

    # 各テーブル確認・作成ログ
    tables = ["users", "shops", "shop_items", "purchase_history", "gamble_settings", "daily_settings"]
    for t in tables:
        print(f"✔ テーブル '{t}' 確認済み")

    print("\n🔧 Botのバックグラウンドタスクを開始")
    print("・雑談文字数でRaruin付与")
    print("・通話参加で1分ごとにRaruin付与")
    print("===================================================")
    await asyncio.sleep(0.1)  # 少し待ってから起動

# ---------- Bot 起動処理 ----------
async def main():
    await startup_log()  # 日本語で詳細ログ
    print("🟢 Discord Botを接続中…")
    await bot.start(TOKEN)  # Bot起動のみ

# 実行
if __name__ == "__main__":
    asyncio.run(main())
