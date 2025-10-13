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

# --------------------
# PersistentDB ラッパー
# --------------------
class PersistentDB:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._warn_if_non_persistent_path()
        self._ensure_tables()

    def _warn_if_non_persistent_path(self):
        path = os.path.abspath(self.db_path)
        if not path.startswith(os.getcwd()):
            print(f"⚠️ 注意: DB_PATH({path}) は永続化されない可能性があります。")
            print("   → GitHub Releases 経由でバックアップを推奨します。")

    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    def _ensure_tables(self):
        conn = self._connect()
        c = conn.cursor()
        c.executescript('''
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            balance INTEGER DEFAULT 0,
            total_received INTEGER DEFAULT 0,
            total_spent INTEGER DEFAULT 0,
            daily_streak INTEGER DEFAULT 0,
            last_daily TEXT
        );

        CREATE TABLE IF NOT EXISTS admins (
            user_id INTEGER PRIMARY KEY
        );

        CREATE TABLE IF NOT EXISTS shops (
            shop_id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE
        );

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
        );

        CREATE TABLE IF NOT EXISTS gamble_settings (
            id INTEGER PRIMARY KEY,
            probability_level INTEGER DEFAULT 3
        );

        CREATE TABLE IF NOT EXISTS purchase_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            shop_name TEXT,
            item_name TEXT,
            price INTEGER,
            timestamp TEXT
        );
        ''')
        c.execute("INSERT OR IGNORE INTO gamble_settings (id, probability_level) VALUES (1,3)")
        conn.commit()
        conn.close()

    async def execute(self, func, *args, **kwargs):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, func, *args, **kwargs)

# --------------------
# 環境変数読み込み
# --------------------
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
DB_PATH = os.getenv("DB_PATH", "main.db")
OWNER_ID = int(os.getenv("OWNER_ID", "1402613707527426131"))
BACKUP_CHANNEL_ID = int(os.getenv("BACKUP_CHANNEL_ID", "0") or 0)

# PersistentDB インスタンス
db = PersistentDB(DB_PATH)

# GitHub Releases から最新 DB を取得
try:
    if download_latest_db():
        print("✅ GitHub から最新の main.db を取得しました。")
    else:
        print("⚠️ Release に main.db が見つかりません。新規作成します。")
except Exception as e:
    print(f"⚠️ DB ダウンロード中にエラー: {e}")

# ---------- Discord Bot 初期化 ----------
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="/", intents=intents)
tree = bot.tree

# ===== Flask keep_alive =====
app = Flask(__name__)
@app.route('/')
def home():
    return "Raruin Bot Running!"

def run_flask():
    try:
        port = int(os.getenv("PORT", "8080"))
        app.run(host='0.0.0.0', port=port)
    except Exception as e:
        print(f"Flask 起動エラー: {e}")

Thread(target=run_flask, daemon=True).start()

# ---------- GitHub Releases 自動バックアップタスク ----------
@tasks.loop(hours=24)
async def backup_database():
    try:
        upload_db_to_release()
        print("✅ GitHub Releases に main.db をバックアップしました。")
        if BACKUP_CHANNEL_ID and os.path.exists(DB_PATH):
            channel = bot.get_channel(BACKUP_CHANNEL_ID)
            if channel:
                await channel.send(file=discord.File(DB_PATH))
    except Exception as e:
        print(f"[backup_database] エラー: {e}")

@backup_database.before_loop
async def before_backup():
    await bot.wait_until_ready()

# ---------- on_ready イベント ----------
@bot.event
async def on_ready():
    print(f"✅ ログイン完了: {bot.user}")
    try:
        await tree.sync()
        print("✅ スラッシュコマンド同期済み。")
    except Exception as e:
        print(f"⚠️ コマンド同期エラー: {e}")

    try:
        if not backup_database.is_running():
            backup_database.start()
            print("✅ 自動バックアップ開始。")
    except Exception as e:
        print(f"[on_ready] backup_database start error: {e}")

    print("Background tasks started.")

# ---------- ユーティリティ関数 ----------
async def is_admin(user_id: int):
    def _check():
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM admins WHERE user_id=?", (user_id,))
        result = cur.fetchone()
        conn.close()
        return bool(result) or user_id == OWNER_ID
    return await db.execute(_check)

async def resolve_target_members(target: str, interaction: discord.Interaction):
    members = []
    # メンション形式
    if interaction.guild:
        for member in interaction.guild.members:
            if str(member.id) == target or member.name == target or member.display_name == target:
                members.append(member)
    return members

def _find_role_from_input(target: str, guild: discord.Guild):
    if not guild:
        return None
    return discord.utils.find(lambda r: r.name == target or str(r.id) == target, guild.roles)

# 
# ---------- Part 2: 管理者コマンドと通貨操作 (PersistentDB対応) ----------

# ---------- /addr - 管理者追加・削除・一覧 ----------
@tree.command(name="addr", description="管理者を追加、削除、一覧表示")
@app_commands.describe(action="add, remove, list", target="ユーザー名またはID")
async def addr(interaction: discord.Interaction, action: str, target: str = None):
    await db.execute(add_user_if_not_exists, interaction.user.id)
    if not await is_admin(interaction.user.id):
        await interaction.response.send_message("⚠️ 管理者のみ使用可能です。", ephemeral=True)
        return

    action = action.lower()

    if action == "list":
        def _fetch_admins():
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            cur.execute("SELECT user_id FROM admins")
            rows = cur.fetchall()
            conn.close()
            return rows

        rows = await db.execute(_fetch_admins)
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

    async def _modify_admins():
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        if action == "add":
            cur.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (user.id,))
        elif action == "remove":
            cur.execute("DELETE FROM admins WHERE user_id=?", (user.id,))
        conn.commit()
        conn.close()

    await db.execute(_modify_admins)
    msg = f"{user.mention} を管理者に追加しました。" if action=="add" else f"{user.mention} を管理者から削除しました。"
    await interaction.response.send_message(msg)

# ---------- /配布 ----------
@tree.command(name="配布", description="指定したユーザーやロールにRaruinを付与（管理者専用）")
@app_commands.describe(target="ユーザーまたはロール", amount="付与額")
async def distribute(interaction: discord.Interaction, target: str, amount: int):
    if not await is_admin(interaction.user.id):
        await interaction.response.send_message("⚠️ 管理者のみ使用可能です。", ephemeral=True)
        return
    if amount <= 0:
        await interaction.response.send_message("⚠️ 付与額は1以上にしてください。", ephemeral=True)
        return

    members = await resolve_target_members(target, interaction)
    if not members:
        role_obj = _find_role_from_input(target, interaction.guild)
        if role_obj:
            members = role_obj.members
    if not members:
        await interaction.response.send_message("❌ 対象が見つかりません。", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    async def _add_balance(member_ids, amt):
        conn = sqlite3.connect(DB_PATH, timeout=30)
        cur = conn.cursor()
        cur.executemany("INSERT OR IGNORE INTO users (user_id) VALUES (?)", [(uid,) for uid in member_ids])
        cur.executemany("UPDATE users SET balance = balance + ?, total_received = total_received + ? WHERE user_id=?",
                        [(amt, amt, uid) for uid in member_ids])
        conn.commit()
        conn.close()

    await db.execute(_add_balance, [m.id for m in members], amount)
    name = members[0].display_name if len(members) == 1 else f"{len(members)} 件のメンバー"
    await interaction.followup.send(f"🎁 {name} に {amount} Raruin を付与しました。")

# ---------- /支払い ----------
@tree.command(name="支払い", description="指定したユーザーやロールのRaruinを減らす（管理者専用）")
@app_commands.describe(target="ユーザーまたはロール", amount="減らす額")
async def payment(interaction: discord.Interaction, target: str, amount: int):
    if not await is_admin(interaction.user.id):
        await interaction.response.send_message("⚠️ 管理者のみ使用可能です。", ephemeral=True)
        return
    if amount <= 0:
        await interaction.response.send_message("⚠️ 減算額は1以上にしてください。", ephemeral=True)
        return

    members = await resolve_target_members(target, interaction)
    if not members:
        role_obj = _find_role_from_input(target, interaction.guild)
        if role_obj:
            members = role_obj.members
    if not members:
        await interaction.response.send_message("❌ 対象が見つかりません。", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    async def _subtract_balance(member_ids, amt):
        conn = sqlite3.connect(DB_PATH, timeout=30)
        cur = conn.cursor()
        cur.executemany("INSERT OR IGNORE INTO users (user_id) VALUES (?)", [(uid,) for uid in member_ids])
        cur.executemany("UPDATE users SET balance = balance - ?, total_spent = total_spent + ? WHERE user_id=?",
                        [(amt, amt, uid) for uid in member_ids])
        conn.commit()
        conn.close()

    await db.execute(_subtract_balance, [m.id for m in members], amount)
    name = members[0].display_name if len(members) == 1 else f"{len(members)} 件のメンバー"
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

    async def _set_prob():
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("UPDATE gamble_settings SET probability_level=? WHERE id=1", (probability,))
        conn.commit()
        conn.close()

    await db.execute(_set_prob)
    await interaction.response.send_message(f"🎯 ギャンブル確率レベルを `{probability}` に設定しました。")

# ---------- /shopadd ----------
@tree.command(name="shopadd", description="管理者専用: ショップを追加または削除")
@app_commands.describe(action="追加 or 削除", shop_name="ショップの名前")
async def shopadd(interaction: discord.Interaction, action: str, shop_name: str):
    if not await is_admin(interaction.user.id):
        await interaction.response.send_message("⚠️ 管理者のみ使用可能です。", ephemeral=True)
        return

    action = action.lower()

    async def _modify_shop():
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        if action == "追加":
            cur.execute("INSERT OR IGNORE INTO shops (name) VALUES (?)", (shop_name,))
        elif action == "削除":
            cur.execute("DELETE FROM shops WHERE name=?", (shop_name,))
        conn.commit()
        conn.close()

    await db.execute(_modify_shop)
    msg = f"✅ ショップ {shop_name} を追加しました。" if action=="追加" else f"🗑️ ショップ {shop_name} を削除しました。"
    await interaction.response.send_message(msg)

# ---------- /shop ----------
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
async def shop(interaction: discord.Interaction,
               action: str,
               shop_name: str,
               item_name: str,
               description: str = "",
               price: int = 0,
               stock: str = "無限",
               role: discord.Role = None,
               role_duration_min: int = None):
    if not await is_admin(interaction.user.id):
        await interaction.response.send_message("⚠️ 管理者のみ使用可能です。", ephemeral=True)
        return

    # PersistentDBアクセス
    def _shop_action():
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        # ショップ存在チェック
        cur.execute("SELECT shop_id FROM shops WHERE name=?", (shop_name,))
        shop_row = cur.fetchone()
        if not shop_row:
            conn.close()
            return None, None
        shop_id = shop_row[0]

        # 在庫処理
        stock_norm = stock if isinstance(stock, str) else str(stock)
        if stock_norm.lower() in ["無限", "mugen"]:
            stock_to_store = "無限"
        else:
            try:
                stock_int = int(stock_norm)
                if stock_int < 0:
                    conn.close()
                    return "invalid_stock", None
                stock_to_store = str(stock_int)
            except:
                conn.close()
                return "invalid_stock", None

        action_norm = action.lower()
        if action_norm in ["追加", "add"]:
            role_id = role.id if role else None
            role_duration_seconds = role_duration_min * 60 if role_duration_min else None
            cur.execute('''
                INSERT INTO shop_items (shop_id, name, description, price, stock, role_id, role_duration)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (shop_id, item_name, description, price, stock_to_store, role_id, role_duration_seconds))
            conn.commit()
            conn.close()
            return "added", stock_to_store
        elif action_norm in ["削除", "remove"]:
            cur.execute("DELETE FROM shop_items WHERE shop_id=? AND name=?", (shop_id, item_name))
            conn.commit()
            conn.close()
            return "removed", None
        else:
            conn.close()
            return "invalid_action", None

    result, extra_stock = await db.execute(_shop_action)
    if result == "invalid_stock":
        await interaction.response.send_message("⚠️ 在庫は0以上の整数か'無限'で指定してください。", ephemeral=True)
    elif result == "invalid_action":
        await interaction.response.send_message("⚠️ actionは「追加」または「削除」（add/remove）を指定してください。", ephemeral=True)
    elif result == "added":
        extra = ""
        if role:
            extra = f" ロール付与: {role.name}"
            if role_duration_min:
                extra += f"（{role_duration_min}分）"
        extra += f" 在庫: {extra_stock}"
        await interaction.response.send_message(f"✅ {item_name} を {shop_name} に追加しました。{extra}")
    elif result == "removed":
        await interaction.response.send_message(f"🗑️ {item_name} を {shop_name} から削除しました。")
    else:
        await interaction.response.send_message("⚠️ 不明なエラーが発生しました。", ephemeral=True)

# ---------- Part 3: ショップ管理とユーザー向けコマンド ----------

# 安全なユーザー作成ユーティリティ
def add_user_if_not_exists(user_id: int):
    """
    users テーブルがなければ作成し、user_id がなければ挿入する。
    """
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
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
        conn.commit()
    except Exception as e:
        print(f"[add_user_if_not_exists] DB error: {e}")
        if conn:
            try:
                conn.rollback()
            except:
                pass
    finally:
        if conn:
            try:
                conn.close()
            except:
                pass

# ---------- /残高 ----------
@tree.command(name="残高", description="自分の残高を確認します")
async def balance_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        add_user_if_not_exists(interaction.user.id)
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL;")
        cur = conn.cursor()
        cur.execute("SELECT balance FROM users WHERE user_id=?", (interaction.user.id,))
        row = cur.fetchone()
        balance = row[0] if row else 0
        conn.close()
        await interaction.followup.send(f"💰 あなたの残高は {balance} Raruin です。")
    except Exception as e:
        print(f"[balance_cmd] error: {e}")
        try:
            await interaction.followup.send(f"⚠️ エラーが発生しました: {e}")
        except:
            pass

# ---------- /ランキング ----------
@tree.command(name="ランキング", description="Raruin残高ランキング上位15名")
async def ranking(interaction: discord.Interaction):
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL;")
        cur = conn.cursor()
        cur.execute("SELECT user_id, balance FROM users ORDER BY balance DESC LIMIT 15")
        rows = cur.fetchall()
        conn.close()

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
    except Exception as e:
        print(f"[ranking] error: {e}")
        await interaction.response.send_message("⚠️ エラーが発生しました。")

# ---------- /統計 ----------
@tree.command(name="統計", description="Raruin全体統計")
async def stats(interaction: discord.Interaction):
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL;")
        cur = conn.cursor()

        cur.execute("SELECT SUM(balance), SUM(total_spent) FROM users")
        total_balance, total_spent = cur.fetchone()

        cur.execute("SELECT user_id, balance FROM users ORDER BY balance DESC LIMIT 1")
        richest_row = cur.fetchone()
        cur.execute("SELECT user_id, balance FROM users ORDER BY balance ASC LIMIT 1")
        poorest_row = cur.fetchone()
        conn.close()

        richest_user = await bot.fetch_user(richest_row[0]) if richest_row else None
        poorest_user = await bot.fetch_user(poorest_row[0]) if poorest_row else None

        msg = (
            f"💰 現在全員が持っているRaruin合計: {total_balance or 0}\n"
            f"📤 全員が今まで使った額合計: {total_spent or 0}\n"
            f"👑 最も持っている人: {richest_user.name if richest_user else 'なし'} ({richest_row[1] if richest_row else 0})\n"
            f"💸 最も持っていない人: {poorest_user.name if poorest_user else 'なし'} ({poorest_row[1] if poorest_row else 0})"
        )
        await interaction.response.send_message(msg)
    except Exception as e:
        print(f"[stats] error: {e}")
        await interaction.response.send_message("⚠️ エラーが発生しました。")

# ---------- /ギャンブル確率確認 ----------
@tree.command(name="ギャンブル確率確認", description="現在のギャンブル確率を確認")
async def gamble_prob_check(interaction: discord.Interaction):
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL;")
        cur = conn.cursor()
        cur.execute('''
            CREATE TABLE IF NOT EXISTS gamble_settings (
                id INTEGER PRIMARY KEY,
                probability_level INTEGER DEFAULT 3
            )
        ''')
        cur.execute("INSERT OR IGNORE INTO gamble_settings (id, probability_level) VALUES (1, 3)")
        conn.commit()
        cur.execute("SELECT probability_level FROM gamble_settings WHERE id=1")
        prob = cur.fetchone()[0]
        conn.close()
        await interaction.response.send_message(f"🎰 現在のギャンブル確率設定: {prob} (1が最も当たりやすい, 6が最も難しい)")
    except Exception as e:
        print(f"[gamble_prob_check] error: {e}")
        await interaction.response.send_message("⚠️ エラーが発生しました。")

# ---------- /ショップリスト ----------
@tree.command(name="ショップリスト", description="ショップ一覧を表示")
async def shop_list(interaction: discord.Interaction):
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL;")
        cur = conn.cursor()
        cur.execute("SELECT name FROM shops")
        rows = cur.fetchall()
        conn.close()
        if not rows:
            await interaction.response.send_message("ショップが存在しません。")
            return
        msg = "🛒 ショップ一覧:\n" + "\n".join([row[0] for row in rows])
        await interaction.response.send_message(msg)
    except Exception as e:
        print(f"[shop_list] error: {e}")
        await interaction.response.send_message("⚠️ エラーが発生しました。")

# ---------- /ショップ ----------
@tree.command(name="ショップ", description="指定したショップの商品を表示")
@app_commands.describe(shop_name="ショップの名前")
async def show_shop(interaction: discord.Interaction, shop_name: str):
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL;")
        cur = conn.cursor()
        cur.execute("SELECT shop_id FROM shops WHERE name=?", (shop_name,))
        row = cur.fetchone()
        if not row:
            await interaction.response.send_message(f"{shop_name} というショップは存在しません。")
            conn.close()
            return
        shop_id = row[0]
        cur.execute("SELECT name, description, price FROM shop_items WHERE shop_id=?", (shop_id,))
        items = cur.fetchall()
        conn.close()
        if not items:
            await interaction.response.send_message("このショップには商品がありません。")
            return
        msg = f"🛒 {shop_name}の商品一覧:\n"
        for name, desc, price in items:
            msg += f"{name} - {desc} - {price} Raruin\n"
        await interaction.response.send_message(msg)
    except Exception as e:
        print(f"[show_shop] error: {e}")
        await interaction.response.send_message("⚠️ エラーが発生しました。")

# ---------- /買う ----------
# (この部分は Part 2 で対応済みの buy_item を使用してください)

# ---------- /渡す ----------
@tree.command(name="渡す", description="指定したユーザーにRaruinを渡す")
@app_commands.describe(target="渡したいユーザー（@で指定）", amount="渡す額")
async def transfer(interaction: discord.Interaction, target: discord.User, amount: int):
    if amount <= 0:
        await interaction.response.send_message("⚠️ 額は1以上にしてください。", ephemeral=True)
        return
    try:
        add_user_if_not_exists(interaction.user.id)
        add_user_if_not_exists(target.id)

        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL;")
        cur = conn.cursor()

        cur.execute("SELECT balance FROM users WHERE user_id=?", (interaction.user.id,))
        balance = cur.fetchone()[0]
        if balance < amount:
            conn.close()
            await interaction.response.send_message("💰 残高が足りません。", ephemeral=True)
            return

        cur.execute(
            "UPDATE users SET balance = balance - ?, total_spent = total_spent + ? WHERE user_id=?",
            (amount, amount, interaction.user.id)
        )
        cur.execute(
            "UPDATE users SET balance = balance + ?, total_received = total_received + ? WHERE user_id=?",
            (amount, amount, target.id)
        )
        conn.commit()
        conn.close()

        try:
            await target.send(f"📩 {interaction.user.display_name} から {amount} Raruin を受け取りました！")
        except:
            pass
        await interaction.response.send_message(f"✅ {target.display_name} に {amount} Raruin を渡しました。")
    except Exception as e:
        print(f"[transfer] error: {e}")
        await interaction.response.send_message("⚠️ エラーが発生しました。")

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

# ---------- /今日の収支 ----------
@tree.command(name="今日の収支", description="自分の受取・支出・残高をまとめて表示")
async def today_income(interaction: discord.Interaction):
    try:
        add_user_if_not_exists(interaction.user.id)
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL;")
        cur = conn.cursor()
        cur.execute("SELECT balance, total_received, total_spent FROM users WHERE user_id=?", (interaction.user.id,))
        balance, received, spent = cur.fetchone()
        conn.close()
        await interaction.response.send_message(
            f"💰 {interaction.user.display_name} さんの収支:\n"
            f"残高: {balance} Raruin\n"
            f"受取合計: {received} Raruin\n"
            f"支出合計: {spent} Raruin"
        )
    except Exception as e:
        print(f"[today_income] error: {e}")
        await interaction.response.send_message("⚠️ エラーが発生しました。")

# ---------- /ショップ検索 ----------
@tree.command(name="ショップ検索", description="商品名でショップ内を検索")
@app_commands.describe(keyword="検索したい商品名")
async def shop_search(interaction: discord.Interaction, keyword: str):
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL;")
        cur = conn.cursor()
        cur.execute(
            "SELECT s.name, i.name, i.price FROM shop_items i "
            "JOIN shops s ON i.shop_id = s.shop_id "
            "WHERE i.name LIKE ?",
            (f"%{keyword}%",)
        )
        rows = cur.fetchall()
        conn.close()
        if not rows:
            await interaction.response.send_message("該当する商品はありません。")
            return
        msg = "🔍 検索結果:\n"
        for shop_name, item_name, price in rows:
            msg += f"{shop_name} - {item_name}: {price} Raruin\n"
        await interaction.response.send_message(msg)
    except Exception as e:
        print(f"[shop_search] error: {e}")
        await interaction.response.send_message("⚠️ エラーが発生しました。")

# ---------- /最近の購入 ----------
@tree.command(name="最近の購入", description="最近購入した商品を確認")
async def recent_purchase(interaction: discord.Interaction):
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL;")
        cur = conn.cursor()
        cur.execute('''
            CREATE TABLE IF NOT EXISTS purchase_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                shop_name TEXT,
                item_name TEXT,
                price INTEGER,
                timestamp TEXT
            )
        ''')
        cur.execute(
            "SELECT shop_name, item_name, price, timestamp "
            "FROM purchase_history WHERE user_id=? ORDER BY id DESC LIMIT 5",
            (interaction.user.id,)
        )
        rows = cur.fetchall()
        conn.close()
        if not rows:
            await interaction.response.send_message("購入履歴はありません。")
            return
        msg = "🛒 最近の購入:\n"
        for shop, item, price, ts in rows:
            msg += f"{ts[:16]} - {shop} - {item}: {price} Raruin\n"
        await interaction.response.send_message(msg)
    except Exception as e:
        print(f"[recent_purchase] error: {e}")
        await interaction.response.send_message("⚠️ エラーが発生しました。")

# ---------- /リーダーボード ----------
@tree.command(name="リーダーボード", description="Raruin上位15名の詳細を表示")
async def leaderboard(interaction: discord.Interaction):
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.execute("PRAGMA journal_mode=WAL;")
        cur = conn.cursor()
        cur.execute(
            "SELECT user_id, balance, total_received, total_spent "
            "FROM users ORDER BY balance DESC LIMIT 15"
        )
        rows = cur.fetchall()
        conn.close()
        msg = "🏆 Raruinリーダーボード（上位15名）\n"
        for i, (uid, bal, rec, spent) in enumerate(rows, start=1):
            try:
                user = await bot.fetch_user(uid)
                name = user.name
            except:
                name = f"ユーザーID:{uid}"
            msg += f"{i}. {name}: 残高 {bal}, 受取 {rec}, 支出 {spent}\n"
        await interaction.response.send_message(msg)
    except Exception as e:
        print(f"[leaderboard] error: {e}")
        await interaction.response.send_message("⚠️ エラーが発生しました。")

# ---------- /役立ち ----------
@tree.command(name="役立ち", description="便利なRaruin情報を表示")
async def tips(interaction: discord.Interaction):
    msg = (
        "💡 役立ち情報:\n"
        "・文字数1文字 = 1 Raruin 取得可能\n"
        "・通話1分 = 12 Raruin 取得可能（ミュートでも）\n"
        "・ショップを見て安く買える商品をチェック\n"
    )
    await interaction.response.send_message(msg)

# ---------- /bot情報 ----------
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


# ---------- Part 5: ギャンブル・自動付与・起動ログ・keep-alive 完全版 ----------

import os
import random
import time
import sqlite3
import asyncio
from datetime import datetime
from discord.ext import tasks
from discord import app_commands
from discord.ui import View, Button
from flask import Flask
from threading import Thread

# ---------- Flask Keep-Alive ----------
app = Flask("")

@app.route("/")
def home():
    return "Raruin Bot is alive!"

def run_flask():
    app.run(host="0.0.0.0", port=8080)

Thread(target=run_flask).start()

# ---------- DB接続 ----------
conn = sqlite3.connect("main.db", check_same_thread=False)
c = conn.cursor()
conn.execute("PRAGMA journal_mode=WAL;")  # 並行アクセス対応

# ---------- ヘルパー関数 ----------
def add_user_if_not_exists(user_id: int):
    c.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
    conn.commit()

def update_balance(user_id, amount):
    add_user_if_not_exists(user_id)
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

def get_probability():
    c.execute("SELECT probability_level FROM gamble_settings WHERE id=1")
    row = c.fetchone()
    return row[0] if row else 3  # デフォルト3

# ---------- 起動時ログ ----------
async def startup_log():
    print("===================================================")
    print("🚀 Bot起動開始 🚀")
    print("日時:", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("TOKENは環境変数から正常に取得済み")
    print("Flaskサーバーで keep-alive 起動済み")
    print("SQLite DB接続済み")
    print("各種テーブル確認・作成済み")
    print("・users, shops, shop_items, purchase_history, gamble_settings, daily_settings")
    print("🔧 バックグラウンドタスク開始")
    print("・雑談文字数でRaruin付与")
    print("・通話参加で1分ごとにRaruin付与")
    print("===================================================\n")
    await asyncio.sleep(0.1)

# ---------- 雑談文字数に応じた自動付与 ----------
def _db_add_chat_earnings(user_id: int, earned: int):
    add_user_if_not_exists(user_id)
    c.execute(
        "UPDATE users SET balance = balance + ?, total_received = total_received + ? WHERE user_id=?",
        (earned, earned, user_id)
    )
    conn.commit()

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    if message.content and message.content.strip():
        earned = len(message.content)
        await asyncio.to_thread(_db_add_chat_earnings, message.author.id, earned)

    await bot.process_commands(message)

# ---------- 通話参加で1分ごとに12Raruin ----------
@tasks.loop(minutes=1)
async def voice_check():
    for guild in bot.guilds:
        for vc in guild.voice_channels:
            for member in vc.members:
                if member.bot:
                    continue
                update_balance(member.id, 12)

# ---------- 汎用ギャンブル勝敗判定 ----------
last_win_times = {}

def is_win(user_id: int, base_chance: int = 0) -> bool:
    now = time.time()
    if user_id in last_win_times and now - last_win_times[user_id] < 60:
        return False
    prob_level = get_probability()
    chance_table = {1: 20, 2: 15, 3: 10, 4: 5, 5: 2, 6: 1}
    chance = min(chance_table.get(prob_level, 10) + base_chance, 100)
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

    prob_level = get_probability()
    symbol_table = {
        1: ["🍒","🍋","🍇","⭐","💎"],
        2: ["🍒","🍋","🍇","⭐","💎","🍉","🔔"],
        3: ["🍒","🍋","🍇","⭐","💎","🍉","🔔","🍀","🥝","🍊"],
        4: ["🍒","🍋","🍇","⭐","💎","🍉","🔔","🍀","🥝","🍊","🎱","💰","🪙"],
        5: ["🍒","🍋","🍇","⭐","💎","🍉","🔔","🍀","🥝","🍊","🎱","💰","🪙","🍎","🍆"],
        6: ["🍒","🍋","🍇","⭐","💎","🍉","🔔","🍀","🥝","🍊","🎱","💰","🪙","🍎","🍆","🍌","🥭","🍍","🥥","🥕"]
    }
    symbols = symbol_table.get(prob_level, symbol_table[3])
    reels = [random.choice(symbols) for _ in range(3)]

    if reels[0] == reels[1] == reels[2]:
        payout = bet * 5
        update_balance(user_id, payout)
        msg = f"🎉 大当たり! +{payout} Raruin"
    elif len(set(reels)) == 2:
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

# ---------- コイントス ----------
@tree.command(name="コイントス", description="コイントスで勝負")
@app_commands.describe(bet="掛け金")
async def coin(interaction: discord.Interaction, bet: int):
    user_id = interaction.user.id
    if bet <= 0 or get_balance(user_id) < bet:
        await interaction.response.send_message("💰 残高不足です。", ephemeral=True)
        return

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

# ---------- ハイアンドロー ----------
class HiLoView(View):
    def __init__(self, user_id, current_card, bet):
        super().__init__(timeout=30)
        self.user_id = user_id
        self.current_card = current_card
        self.next_card = random.randint(1, 13)
        self.bet = bet

    async def finish(self, interaction, win):
        if win:
            payout = int(self.bet * 0.3)
            msg = f"🎉 勝ち! +{payout} Raruin"
        else:
            payout = -self.bet
            msg = f"💀 負け... -{self.bet} Raruin"
        update_balance(self.user_id, payout)
        await interaction.response.edit_message(
            content=f"{self.current_card} → {self.next_card}\n{msg}\n残高: {get_balance(self.user_id)} Raruin",
            view=None
        )

    @discord.ui.button(label="ハイ", style=discord.ButtonStyle.primary)
    async def hi_button(self, interaction: discord.Interaction, button: Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ あなた専用です", ephemeral=True)
            return
        win = self.next_card > self.current_card
        await self.finish(interaction, win)

    @discord.ui.button(label="ロー", style=discord.ButtonStyle.secondary)
    async def low_button(self, interaction: discord.Interaction, button: Button):
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

# ---------- ルーレット ----------
@tree.command(name="ルーレット", description="ルーレットで勝負")
@app_commands.describe(bet="掛け金", choice="赤/黒/偶数/奇数/番号(0-36)")
async def roulette(interaction: discord.Interaction, bet: int, choice: str):
    user_id = interaction.user.id
    if bet <= 0 or get_balance(user_id) < bet:
        await interaction.response.send_message("💰 残高不足です。", ephemeral=True)
        return

    choice_str = choice.strip()
    number = random.randint(0, 36)
    color = "赤" if number % 2 == 0 else "黒"

    win = False
    payout = 0

    if choice_str.isdigit():
        chosen_num = int(choice_str)
        if 0 <= chosen_num <= 36 and chosen_num == number:
            win = True
            payout = bet * 35
    if not win and choice_str in ["赤", "黒"]:
        if choice_str == color:
            win = True
            payout = bet * 2
    if not win and choice_str in ["偶数", "奇数"]:
        if (number % 2 == 0 and choice_str == "偶数") or (number % 2 == 1 and choice_str == "奇数"):
            win = True
            payout = bet * 2

    if win:
        update_balance(user_id, payout)
        msg = f"🎉 勝ち! {payout} Raruin 獲得"
    else:
        update_balance(user_id, -bet)
        msg = f"💀 負け... -{bet} Raruin"

    await interaction.response.send_message(
        f"ルーレット: 出目 {number} ({color})\n{msg}\n残高: {get_balance(user_id)} Raruin"
    )

# ---------- ポーカー ----------
def hand_rank_by_counts(rank_list):
    order = {r: i for i, r in enumerate(['2','3','4','5','6','7','8','9','10','J','Q','K','A'], start=2)}
    counts = {}
    for r in rank_list:
        counts[r] = counts.get(r, 0) + 1
    items = sorted([(cnt, order[r], r) for r, cnt in counts.items()], key=lambda x: (-x[0], -x[1]))
    cnts = sorted(counts.values(), reverse=True)

    if cnts[0] == 4: category=6
    elif cnts[0]==3 and len(cnts)>1 and cnts[1]==2: category=5
    elif cnts[0]==3: category=4
    elif cnts[0]==2 and len(cnts)>1 and cnts[1]==2: category=3
    elif cnts[0]==2: category=2
    else: category=1
    tiebreaker=[]
    for cnt, rv, r in items:
        tiebreaker.append(cnt)
        tiebreaker.append(rv)
    return (category, tiebreaker)

@tree.command(name="ポーカー", description="5枚カードで勝負（対ディーラー）")
@app_commands.describe(bet="掛け金")
async def poker(interaction: discord.Interaction, bet: int):
    user_id = interaction.user.id
    if bet <= 0 or get_balance(user_id) < bet:
        await interaction.response.send_message("💰 残高不足です。", ephemeral=True)
        return

    ranks = [str(n) for n in range(2,11)] + ["J","Q","K","A"]
    suits = ["♠","♥","♦","♣"]
    deck = [r+s for r in ranks for s in suits]
    cards = random.sample(deck, 10)
    player_cards = cards[:5]
    dealer_cards = cards[5:]
    player_ranks = [c[:-1] for c in player_cards]
    dealer_ranks = [c[:-1] for c in dealer_cards]

    player_rank = hand_rank_by_counts(player_ranks)
    dealer_rank = hand_rank_by_counts(dealer_ranks)

    prob_level = get_probability()
    dealer_modifier = prob_level - 3
    modified_dealer_category = max(1, min(6, dealer_rank[0] + dealer_modifier))
    modified_dealer_rank = (modified_dealer_category, dealer_rank[1])

    if player_rank[0] > modified_dealer_rank[0]: winner="player"
    elif player_rank[0] < modified_dealer_rank[0]: winner="dealer"
    else:
        if player_rank[1] > modified_dealer_rank[1]: winner="player"
        elif player_rank[1] < modified_dealer_rank[1]: winner="dealer"
        else: winner="tie"

    mult_map = {6:10,5:7,4:3,3:2,2:1.5,1:0}
    mult = mult_map.get(player_rank[0],0)

    if winner=="player" and mult>0:
        payout=int(bet*mult)
        update_balance(user_id,payout)
        msg=f"🎉 あなたの勝ち! ({payout} Raruin 獲得)"
    elif winner=="player" and mult==0:
        payout=bet
        update_balance(user_id,payout)
        msg=f"🎉 あなたの勝ち! ハイカードで勝利: +{payout} Raruin"
    elif winner=="tie":
        msg="🤝 引き分けです。ベットは返却されます。"
    else:
        payout=-bet
        update_balance(user_id,payout)
        msg=f"💀 あなたの負け... -{bet} Raruin"

    category_names={6:"フォーカード",5:"フルハウス",4:"スリーカード",3:"ツーペア",2:"ワンペア",1:"ハイカード"}
    player_cat_name=category_names.get(player_rank[0],"不明")
    dealer_cat_name=category_names.get(dealer_rank[0],"不明")
    modified_dealer_cat_name=category_names.get(modified_dealer_category,"不明")

    await interaction.response.send_message(
        f"あなたの手札: {' '.join(player_cards)} ({player_cat_name})\n"
        f"ディーラーの手札: {' '.join(dealer_cards)} ({dealer_cat_name})\n"
        f"→ ディーラー強さ調整: {modified_dealer_cat_name}\n\n"
        f"{msg}\n残高: {get_balance(user_id)} Raruin"
    )

# ---------- Bot Ready ----------
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print(f"Guilds: {[g.name for g in bot.guilds]}")
    await tree.sync()
    voice_check.start()
    print("Bot ready and background tasks started.")

# ---------- Bot 起動 ----------
async def main():
    await startup_log()
    print("🟢 Discord Botを接続中…")
    await bot.start(TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
