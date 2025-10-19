import os
import sqlite3
import asyncio
import discord
from discord.ext import commands
from discord import app_commands
from flask import Flask
from threading import Thread
from dotenv import load_dotenv
from datetime import datetime
from typing import List, Optional
import re

# --------------------
# .env読み込みと初期設定
# --------------------
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID", "1402613707527426131"))

if not TOKEN:
    raise ValueError("❌ 環境変数 DISCORD_TOKEN が設定されていません。")

# --------------------
# Flaskサーバー (Render/uptimerobot維持用)
# --------------------
app = Flask(__name__)

@app.route('/')
def home():
    return "Raruin BOT is running!"

def run_flask():
    try:
        port = int(os.getenv("PORT", "8080"))
    except:
        port = 8080
    try:
        app.run(host="0.0.0.0", port=port)
    except Exception as e:
        # Flaskの起動に失敗してもBot自体は動くようにする
        print(f"[Flask] 起動エラー: {e}")

Thread(target=run_flask, daemon=True).start()

# --------------------
# Discord Bot設定
# --------------------
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# --------------------
# PersistentDB クラス（Render永続化対応版）
# --------------------
class PersistentDB:
    def __init__(self, db_path: str):
        env_db_path = os.getenv("DB_PATH")
        if env_db_path:
            db_path = env_db_path

        if os.path.exists("/data"):
            if not db_path or db_path == "main.db":
                db_path = "/data/main.db"
        else:
            if db_path.startswith("/data"):
                db_path = "main.db"
            print("⚠️ /data が存在しないため、DB は作業ディレクトリに保存されます。")

        self.db_path = db_path
        db_dir = os.path.dirname(os.path.abspath(self.db_path))
        if db_dir and not os.path.exists(db_dir):
            try:
                os.makedirs(db_dir, exist_ok=True)
                print(f"✅ DBディレクトリ作成: {db_dir}")
            except Exception as e:
                print(f"⚠️ DBディレクトリ作成失敗: {db_dir} ({e})")

        self._warn_if_non_persistent_path()
        try:
            self._ensure_tables()
        except Exception as e:
            print(f"❌ DB初期化エラー: {e}")

    def _warn_if_non_persistent_path(self):
        path = os.path.abspath(self.db_path)
        if not path.startswith("/data"):
            print(f"⚠️ 注意: {path} は永続化されない可能性があります。")

    def _connect(self):
        try:
            conn = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA foreign_keys = ON;")
            return conn
        except Exception as e:
            print(f"❌ DB接続エラー: {e}")
            raise

    def _ensure_tables(self):
        conn = None
        try:
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

            CREATE TABLE IF NOT EXISTS daily_earnings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                date TEXT,
                earnings INTEGER
            );
            ''')
            c.execute("INSERT OR IGNORE INTO gamble_settings (id, probability_level) VALUES (1,3)")
            conn.commit()
        finally:
            if conn:
                conn.close()

    async def execute(self, func, *args, **kwargs):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, func, *args, **kwargs)

# --------------------
# DB初期化
# --------------------
DB_PATH = os.getenv("DB_PATH") or ("/data/main.db" if os.path.exists("/data") else "main.db")
db = PersistentDB(DB_PATH)

# --------------------
# ユーザー関連関数
# --------------------
def add_user_if_not_exists_sync(user_id: int):
    try:
        conn = db._connect()
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO users (user_id, balance, total_received, total_spent) VALUES (?, 0, 0, 0)", (user_id,))
        conn.commit()
    except Exception as e:
        print(f"[add_user_if_not_exists_sync] エラー: {e}")
    finally:
        if conn:
            conn.close()

async def add_user_if_not_exists(user_id: int):
    await asyncio.to_thread(add_user_if_not_exists_sync, user_id)

def get_balance_sync(user_id: int) -> int:
    try:
        conn = db._connect()
        c = conn.cursor()
        c.execute("SELECT balance FROM users WHERE user_id=?", (user_id,))
        row = c.fetchone()
        return int(row[0]) if row else 0
    except Exception as e:
        print(f"[get_balance_sync] エラー: {e}")
        return 0
    finally:
        if conn:
            conn.close()

async def get_balance(user_id: int) -> int:
    return await asyncio.to_thread(get_balance_sync, user_id)

def update_balance_sync(user_id: int, amount: int):
    try:
        conn = db._connect()
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO users (user_id, balance, total_received, total_spent) VALUES (?, 0, 0, 0)", (user_id,))
        if amount >= 0:
            c.execute("UPDATE users SET balance = balance + ?, total_received = total_received + ? WHERE user_id=?", (amount, amount, user_id))
        else:
            c.execute("UPDATE users SET balance = balance + ?, total_spent = total_spent + ? WHERE user_id=?", (amount, -amount, user_id))
        conn.commit()
    except Exception as e:
        print(f"[update_balance_sync] エラー: {e}")
    finally:
        if conn:
            conn.close()

async def update_balance(user_id: int, amount: int):
    await asyncio.to_thread(update_balance_sync, user_id, amount)

# --------------------
# 起動時イベント
# --------------------
@bot.event
async def on_ready():
    try:
        print("===================================================")
        print("✅ ログイン完了:", bot.user)
        print("ID:", bot.user.id)
        print("日時:", datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S (UTC)"))
        print("DB path:", DB_PATH)
        print("Owner ID:", OWNER_ID)
        print("===================================================")
    except Exception:
        pass

    try:
        await tree.sync()
        print("✅ スラッシュコマンドを同期しました。")
    except Exception as e:
        print(f"⚠️ スラッシュコマンド同期エラー: {e}")

    print("Bot is ready.")

# --------------------
# ユーティリティ: ユーザー存在確認関数
# --------------------
def _add_user_if_not_exists_sync(user_id: int, db_path: str):
    """同期版ユーザー登録関数"""
    conn = sqlite3.connect(db_path, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL;")
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            balance INTEGER DEFAULT 0,
            total_received INTEGER DEFAULT 0,
            total_spent INTEGER DEFAULT 0,
            daily_streak INTEGER DEFAULT 0,
            last_daily TEXT
        )
    """)
    cur.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
    conn.commit()
    conn.close()

def add_user_if_not_exists(user_id: int):
    """上位互換のシンプル版ラッパー"""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL;")
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            balance INTEGER DEFAULT 0,
            total_received INTEGER DEFAULT 0,
            total_spent INTEGER DEFAULT 0,
            daily_streak INTEGER DEFAULT 0,
            last_daily TEXT
        )
    """)
    cur.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
    conn.commit()
    conn.close()
# ---------- Part 2: 管理者コマンドと通貨操作 (PersistentDB対応) ----------

import re
from typing import List, Optional

# ---------- ユーティリティ: ユーザー追加（同期関数、db.executeで呼ぶ） ----------
def _add_user_if_not_exists_sync(user_id: int, db_path: str):
    """sync function for db.execute"""
    conn = sqlite3.connect(db_path, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL;")
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            balance INTEGER DEFAULT 0,
            total_received INTEGER DEFAULT 0,
            total_spent INTEGER DEFAULT 0,
            daily_streak INTEGER DEFAULT 0,
            last_daily TEXT
        )
    """)
    cur.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
    conn.commit()
    conn.close()

# wrapper so existing code can call add_user_if_not_exists synchronously if needed:
def add_user_if_not_exists(user_id: int):
    # call via db.execute from async code: await db.execute(_add_user_if_not_exists_sync, user_id, DB_PATH)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL;")
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            balance INTEGER DEFAULT 0,
            total_received INTEGER DEFAULT 0,
            total_spent INTEGER DEFAULT 0,
            daily_streak INTEGER DEFAULT 0,
            last_daily TEXT
        )
    """)
    cur.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
    conn.commit()
    conn.close()


# ---------- /addr - 管理者追加・削除・一覧 ----------
@tree.command(name="addr", description="管理者を追加、削除、一覧表示")
@app_commands.describe(action="add, remove, list", target="ユーザー名またはID")
async def addr(interaction: discord.Interaction, action: str, target: str = None):
    try:
        # ensure caller exists in users table
        await db.execute(_add_user_if_not_exists_sync, interaction.user.id, DB_PATH)

        if not await is_admin(interaction.user.id):
            await interaction.response.send_message("⚠️ 管理者のみ使用可能です。", ephemeral=True)
            return

        action = action.lower()
        if action == "list":
            def _fetch_admins():
                conn = sqlite3.connect(DB_PATH)
                cur = conn.cursor()
                cur.execute("CREATE TABLE IF NOT EXISTS admins (user_id INTEGER PRIMARY KEY)")
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

        # 解決: ギルドメンバー名 or ID で探す
        user = None
        if interaction.guild:
            user = discord.utils.find(lambda m: m.name == target or (m.nick and m.nick == target), interaction.guild.members)
        if not user:
            try:
                user_id = int(target)
                user = await bot.fetch_user(user_id)
            except:
                await interaction.response.send_message("❌ ユーザーが見つかりません。", ephemeral=True)
                return

        async def _modify_admins(action_inner, uid):
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            cur.execute("CREATE TABLE IF NOT EXISTS admins (user_id INTEGER PRIMARY KEY)")
            if action_inner == "add":
                cur.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (uid,))
            elif action_inner == "remove":
                cur.execute("DELETE FROM admins WHERE user_id=?", (uid,))
            conn.commit()
            conn.close()

        await db.execute(_modify_admins, action, user.id)
        msg = f"✅ {user.mention} を管理者に追加しました。" if action == "add" else f"🗑️ {user.mention} を管理者から削除しました。"
        await interaction.response.send_message(msg)
    except Exception as e:
        print(f"[addr] error: {e}")
        await interaction.response.send_message(f"⚠️ エラーが発生しました: {e}", ephemeral=True)


# ---------- 管理者チェック（非同期ラッパー） ----------
async def is_admin(user_id: int) -> bool:
    def _check():
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS admins (user_id INTEGER PRIMARY KEY)")
        cur.execute("SELECT 1 FROM admins WHERE user_id=?", (user_id,))
        r = cur.fetchone()
        conn.close()
        return r is not None
    try:
        is_admin_db = await db.execute(_check)
        return is_admin_db or (user_id == OWNER_ID)
    except Exception as e:
        print(f"[is_admin] DB error: {e}")
        return user_id == OWNER_ID


# ---------- ターゲット解決（より堅牢） ----------
async def resolve_target_members(target: str, interaction: discord.Interaction) -> List[discord.Member]:
    guild = interaction.guild
    if not guild:
        return []

    raw = target.strip()

    # role mention <@&id>
    m = re.match(r'^<@&(\d+)>$', raw)
    if m:
        role = guild.get_role(int(m.group(1)))
        return list(role.members) if role else []

    # user mention <@!id> or <@id>
    m = re.match(r'^<@!?(?P<id>\d+)>$', raw)
    if m:
        uid = int(m.group("id"))
        member = guild.get_member(uid)
        if not member:
            try:
                member = await guild.fetch_member(uid)
            except:
                member = None
        return [member] if member else []

    # if starts with @name (fuzzy role)
    if raw.startswith("@") or raw.startswith("＠"):
        name = raw.lstrip("@\uFF20").strip()
        role = discord.utils.find(lambda r: r.name.lower() == name.lower(), guild.roles)
        if role:
            return list(role.members)
        starts = [r for r in guild.roles if r.name.lower().startswith(name.lower())]
        if starts:
            return list(starts[0].members)
        contains = [r for r in guild.roles if name.lower() in r.name.lower()]
        if contains:
            return list(contains[0].members)
        return []

    # digits -> id (member or role)
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
            return list(role.members)

    # direct member name / nickname
    found = []
    for mbr in guild.members:
        if mbr.name == raw or (mbr.nick and mbr.nick == raw) or f"{mbr.name}#{mbr.discriminator}" == raw:
            found.append(mbr)
    if found:
        return found[:50]

    # partial match
    partial = [mbr for mbr in guild.members if raw.lower() in mbr.name.lower() or (mbr.nick and raw.lower() in mbr.nick.lower())]
    return partial[:50]


def _find_role_from_input(raw: str, guild: discord.Guild) -> Optional[discord.Role]:
    if not guild:
        return None
    s = raw.strip()
    m = re.match(r'^<@&(\d+)>$', s)
    if m:
        return guild.get_role(int(m.group(1)))
    if s.isdigit():
        r = guild.get_role(int(s))
        if r:
            return r
    name = s.lstrip("@\uFF20").strip()
    if name:
        role = discord.utils.find(lambda r: r.name.lower() == name.lower(), guild.roles)
        if role:
            return role
        starts = [r for r in guild.roles if r.name.lower().startswith(name.lower())]
        if starts:
            return starts[0]
        contains = [r for r in guild.roles if name.lower() in r.name.lower()]
        if contains:
            return contains[0]
    role = discord.utils.find(lambda r: r.name.lower() == s.lower(), guild.roles)
    return role


# ---------- /配布 ----------
@tree.command(name="配布", description="指定したユーザーまたはロールにRaruinを付与（管理者専用）")
@app_commands.describe(target="ユーザーまたはロール（@メンション可）", amount="付与する金額")
async def distribute(interaction: discord.Interaction, target: str, amount: int):
    try:
        if not await is_admin(interaction.user.id):
            await interaction.response.send_message("⚠️ 管理者のみ使用可能です。", ephemeral=True)
            return
        if amount <= 0:
            await interaction.response.send_message("⚠️ 付与額は1以上にしてください。", ephemeral=True)
            return

        # Defer early to avoid "application did not respond"
        await interaction.response.defer(ephemeral=True)

        members = await resolve_target_members(target, interaction)
        if not members:
            role_obj = _find_role_from_input(target, interaction.guild)
            if role_obj:
                members = list(role_obj.members)
        if not members:
            await interaction.followup.send("❌ 対象ユーザーまたはロールが見つかりません。", ephemeral=True)
            return

        def _add_balance_sync(member_ids, amt, db_path):
            conn = sqlite3.connect(db_path, timeout=30)
            cur = conn.cursor()
            cur.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, balance INTEGER DEFAULT 0, total_received INTEGER DEFAULT 0, total_spent INTEGER DEFAULT 0)")
            cur.executemany("INSERT OR IGNORE INTO users (user_id) VALUES (?)", [(uid,) for uid in member_ids])
            cur.executemany("UPDATE users SET balance = balance + ?, total_received = total_received + ? WHERE user_id=?", [(amt, amt, uid) for uid in member_ids])
            conn.commit()
            conn.close()

        member_ids = [m.id for m in members]
        await db.execute(_add_balance_sync, member_ids, amount, DB_PATH)

        target_name = members[0].display_name if len(members) == 1 else f"{len(members)}人のメンバー"
        await interaction.followup.send(f"🎁 {target_name} に **{amount:,} Raruin** を付与しました。")
    except Exception as e:
        print(f"[distribute] error: {e}")
        try:
            await interaction.followup.send(f"⚠️ エラーが発生しました: {e}", ephemeral=True)
        except:
            pass


# ---------- /支払い ----------
@tree.command(name="支払い", description="指定したユーザーまたはロールのRaruinを減らす（管理者専用）")
@app_commands.describe(target="ユーザーまたはロール（@メンション可）", amount="減算する金額")
async def payment(interaction: discord.Interaction, target: str, amount: int):
    try:
        if not await is_admin(interaction.user.id):
            await interaction.response.send_message("⚠️ 管理者のみ使用可能です。", ephemeral=True)
            return
        if amount <= 0:
            await interaction.response.send_message("⚠️ 減算額は1以上にしてください。", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        members = await resolve_target_members(target, interaction)
        if not members:
            role_obj = _find_role_from_input(target, interaction.guild)
            if role_obj:
                members = list(role_obj.members)
        if not members:
            await interaction.followup.send("❌ 対象ユーザーまたはロールが見つかりません。", ephemeral=True)
            return

        def _subtract_balance_sync(member_ids, amt, db_path):
            conn = sqlite3.connect(db_path, timeout=30)
            cur = conn.cursor()
            cur.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, balance INTEGER DEFAULT 0, total_received INTEGER DEFAULT 0, total_spent INTEGER DEFAULT 0)")
            cur.executemany("INSERT OR IGNORE INTO users (user_id) VALUES (?)", [(uid,) for uid in member_ids])
            cur.executemany("UPDATE users SET balance = balance - ?, total_spent = total_spent + ? WHERE user_id=?", [(amt, amt, uid) for uid in member_ids])
            conn.commit()
            conn.close()

        member_ids = [m.id for m in members]
        await db.execute(_subtract_balance_sync, member_ids, amount, DB_PATH)

        target_name = members[0].display_name if len(members) == 1 else f"{len(members)}人のメンバー"
        await interaction.followup.send(f"💸 {target_name} から **{amount:,} Raruin** を減算しました。")
    except Exception as e:
        print(f"[payment] error: {e}")
        try:
            await interaction.followup.send(f"⚠️ エラーが発生しました: {e}", ephemeral=True)
        except:
            pass


# ---------- /ギャンブル確率設定 ----------
@tree.command(name="ギャンブル確率設定", description="管理者専用: ギャンブル確率レベル変更")
@app_commands.describe(probability="1=当たりやすい, 6=当たりにくい")
async def gamble_prob_set(interaction: discord.Interaction, probability: int):
    try:
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("❌ 管理者専用です。", ephemeral=True)
            return
        if probability < 1 or probability > 6:
            await interaction.response.send_message("⚠️ 1〜6で指定してください。", ephemeral=True)
            return

        def _set_prob_sync(p, db_path):
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            cur.execute("CREATE TABLE IF NOT EXISTS gamble_settings (id INTEGER PRIMARY KEY, probability_level INTEGER DEFAULT 3)")
            cur.execute("INSERT OR IGNORE INTO gamble_settings (id, probability_level) VALUES (1,3)")
            cur.execute("UPDATE gamble_settings SET probability_level=? WHERE id=1", (p,))
            conn.commit()
            conn.close()

        await db.execute(_set_prob_sync, probability, DB_PATH)
        await interaction.response.send_message(f"🎯 ギャンブル確率レベルを `{probability}` に設定しました。")
    except Exception as e:
        print(f"[gamble_prob_set] error: {e}")
        await interaction.response.send_message("⚠️ エラーが発生しました。", ephemeral=True)


# ---------- /shopadd ----------
@tree.command(name="shopadd", description="管理者専用: ショップを追加または削除")
@app_commands.describe(action="追加 or 削除", shop_name="ショップの名前")
async def shopadd(interaction: discord.Interaction, action: str, shop_name: str):
    try:
        if not await is_admin(interaction.user.id):
            await interaction.response.send_message("⚠️ 管理者のみ使用可能です。", ephemeral=True)
            return

        action_norm = action.lower()

        def _modify_shop_sync(act, sname, db_path):
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            cur.execute("CREATE TABLE IF NOT EXISTS shops (shop_id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE)")
            if act in ("追加", "add"):
                cur.execute("INSERT OR IGNORE INTO shops (name) VALUES (?)", (sname,))
            elif act in ("削除", "remove"):
                cur.execute("DELETE FROM shops WHERE name=?", (sname,))
            conn.commit()
            conn.close()

        await db.execute(_modify_shop_sync, action_norm, shop_name, DB_PATH)
        msg = f"✅ ショップ {shop_name} を追加しました。" if action_norm in ("追加", "add") else f"🗑️ ショップ {shop_name} を削除しました。"
        await interaction.response.send_message(msg)
    except Exception as e:
        print(f"[shopadd] error: {e}")
        await interaction.response.send_message("⚠️ エラーが発生しました。", ephemeral=True)


# ---------- /shop (商品追加/削除) ----------
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
    try:
        if not await is_admin(interaction.user.id):
            await interaction.response.send_message("⚠️ 管理者のみ使用可能です。", ephemeral=True)
            return

        def _shop_action_sync(act, sname, iname, desc, pr, st, role_id, role_dur, db_path):
            conn = sqlite3.connect(db_path)
            cur = conn.cursor()
            cur.execute("CREATE TABLE IF NOT EXISTS shops (shop_id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE)")
            cur.execute("CREATE TABLE IF NOT EXISTS shop_items (item_id INTEGER PRIMARY KEY AUTOINCREMENT, shop_id INTEGER, name TEXT, description TEXT, price INTEGER, stock TEXT, role_id INTEGER, role_duration INTEGER)")
            # check shop exists
            cur.execute("SELECT shop_id FROM shops WHERE name=?", (sname,))
            shop_row = cur.fetchone()
            if not shop_row:
                conn.close()
                return ("no_shop", None)
            shop_id = shop_row[0]

            stock_norm = st if isinstance(st, str) else str(st)
            if stock_norm.lower() in ("無限", "mugen"):
                stock_to_store = "無限"
            else:
                try:
                    stock_int = int(stock_norm)
                    if stock_int < 0:
                        conn.close()
                        return ("invalid_stock", None)
                    stock_to_store = str(stock_int)
                except:
                    conn.close()
                    return ("invalid_stock", None)

            if act in ("追加", "add"):
                role_id_val = role_id
                role_duration_seconds = role_dur * 60 if role_dur else None
                cur.execute('''
                    INSERT INTO shop_items (shop_id, name, description, price, stock, role_id, role_duration)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (shop_id, iname, desc, pr, stock_to_store, role_id_val, role_duration_seconds))
                conn.commit()
                conn.close()
                return ("added", stock_to_store)
            elif act in ("削除", "remove"):
                cur.execute("DELETE FROM shop_items WHERE shop_id=? AND name=?", (shop_id, iname))
                conn.commit()
                conn.close()
                return ("removed", None)
            else:
                conn.close()
                return ("invalid_action", None)

        result, extra = await db.execute(_shop_action_sync, action, shop_name, item_name, description, price, stock, (role.id if role else None), role_duration_min, DB_PATH)

        if result == "no_shop":
            await interaction.response.send_message(f"⚠️ {shop_name} というショップは存在しません。", ephemeral=True)
        elif result == "invalid_stock":
            await interaction.response.send_message("⚠️ 在庫は0以上の整数か'無限'で指定してください。", ephemeral=True)
        elif result == "invalid_action":
            await interaction.response.send_message("⚠️ actionは「追加」または「削除」（add/remove）を指定してください。", ephemeral=True)
        elif result == "added":
            extra_msg = ""
            if role:
                extra_msg = f" ロール付与: {role.name}"
                if role_duration_min:
                    extra_msg += f"（{role_duration_min}分）"
            extra_msg += f" 在庫: {extra}"
            await interaction.response.send_message(f"✅ {item_name} を {shop_name} に追加しました。{extra_msg}")
        elif result == "removed":
            await interaction.response.send_message(f"🗑️ {item_name} を {shop_name} から削除しました。")
        else:
            await interaction.response.send_message("⚠️ 不明なエラーが発生しました。", ephemeral=True)
    except Exception as e:
        print(f"[shop] error: {e}")
        await interaction.response.send_message("⚠️ エラーが発生しました。", ephemeral=True)


# ---------- /残高 ----------
@tree.command(name="残高", description="自分の残高を確認します")
async def balance_cmd(interaction: discord.Interaction):
    try:
        await interaction.response.defer(ephemeral=True)
        # ensure user exists
        await db.execute(_add_user_if_not_exists_sync, interaction.user.id, DB_PATH)

        def _get_balance_sync(uid, db_path):
            conn = sqlite3.connect(db_path, timeout=10)
            conn.execute("PRAGMA journal_mode=WAL;")
            cur = conn.cursor()
            cur.execute("SELECT balance FROM users WHERE user_id=?", (uid,))
            r = cur.fetchone()
            conn.close()
            return r[0] if r else 0

        balance = await db.execute(_get_balance_sync, interaction.user.id, DB_PATH)
        await interaction.followup.send(f"💰 あなたの残高は {balance} Raruin です。")
    except Exception as e:
        print(f"[balance_cmd] error: {e}")
        try:
            await interaction.followup.send(f"⚠️ エラーが発生しました: {e}", ephemeral=True)
        except:
            pass


# ---------- /ランキング ----------
@tree.command(name="ランキング", description="Raruin残高ランキング上位15名")
async def ranking(interaction: discord.Interaction):
    try:
        def _fetch_top_sync(db_path):
            conn = sqlite3.connect(db_path, timeout=10)
            cur = conn.cursor()
            cur.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, balance INTEGER DEFAULT 0, total_received INTEGER DEFAULT 0, total_spent INTEGER DEFAULT 0)")
            cur.execute("SELECT user_id, balance FROM users ORDER BY balance DESC LIMIT 15")
            rows = cur.fetchall()
            conn.close()
            return rows

        rows = await db.execute(_fetch_top_sync, DB_PATH)
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
        def _stats_sync(db_path):
            conn = sqlite3.connect(db_path, timeout=10)
            cur = conn.cursor()
            cur.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, balance INTEGER DEFAULT 0, total_received INTEGER DEFAULT 0, total_spent INTEGER DEFAULT 0)")
            cur.execute("SELECT SUM(balance), SUM(total_spent) FROM users")
            tot = cur.fetchone()
            cur.execute("SELECT user_id, balance FROM users ORDER BY balance DESC LIMIT 1")
            richest = cur.fetchone()
            cur.execute("SELECT user_id, balance FROM users ORDER BY balance ASC LIMIT 1")
            poorest = cur.fetchone()
            conn.close()
            return tot, richest, poorest

        (total_balance, total_spent), richest_row, poorest_row = await db.execute(_stats_sync, DB_PATH)
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
        def _get_prob_sync(db_path):
            conn = sqlite3.connect(db_path, timeout=10)
            cur = conn.cursor()
            cur.execute("CREATE TABLE IF NOT EXISTS gamble_settings (id INTEGER PRIMARY KEY, probability_level INTEGER DEFAULT 3)")
            cur.execute("INSERT OR IGNORE INTO gamble_settings (id, probability_level) VALUES (1,3)")
            cur.execute("SELECT probability_level FROM gamble_settings WHERE id=1")
            r = cur.fetchone()
            conn.close()
            return r[0] if r else 3

        prob = await db.execute(_get_prob_sync, DB_PATH)
        await interaction.response.send_message(f"🎰 現在のギャンブル確率設定: {prob} (1が最も当たりやすい, 6が最も難しい)")
    except Exception as e:
        print(f"[gamble_prob_check] error: {e}")
        await interaction.response.send_message("⚠️ エラーが発生しました。")


# ---------- /ショップリスト ----------
@tree.command(name="ショップリスト", description="ショップ一覧を表示")
async def shop_list(interaction: discord.Interaction):
    try:
        def _list_shops_sync(db_path):
            conn = sqlite3.connect(db_path, timeout=10)
            cur = conn.cursor()
            cur.execute("CREATE TABLE IF NOT EXISTS shops (shop_id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE)")
            cur.execute("SELECT name FROM shops")
            rows = cur.fetchall()
            conn.close()
            return rows

        rows = await db.execute(_list_shops_sync, DB_PATH)
        if not rows:
            await interaction.response.send_message("ショップが存在しません。")
            return
        msg = "🛒 ショップ一覧:\n" + "\n".join([row[0] for row in rows])
        await interaction.response.send_message(msg)
    except Exception as e:
        print(f"[shop_list] error: {e}")
        await interaction.response.send_message("⚠️ エラーが発生しました。")


# ---------- /ショップ（表示） ----------
@tree.command(name="ショップ", description="指定したショップの商品を表示")
@app_commands.describe(shop_name="ショップの名前")
async def show_shop(interaction: discord.Interaction, shop_name: str):
    try:
        def _show_shop_sync(sname, db_path):
            conn = sqlite3.connect(db_path, timeout=10)
            cur = conn.cursor()
            cur.execute("CREATE TABLE IF NOT EXISTS shops (shop_id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE)")
            cur.execute("CREATE TABLE IF NOT EXISTS shop_items (item_id INTEGER PRIMARY KEY AUTOINCREMENT, shop_id INTEGER, name TEXT, description TEXT, price INTEGER, stock TEXT, role_id INTEGER, role_duration INTEGER)")
            cur.execute("SELECT shop_id FROM shops WHERE name=?", (sname,))
            row = cur.fetchone()
            if not row:
                conn.close()
                return None
            shop_id = row[0]
            cur.execute("SELECT name, description, price FROM shop_items WHERE shop_id=?", (shop_id,))
            items = cur.fetchall()
            conn.close()
            return items

        items = await db.execute(_show_shop_sync, shop_name, DB_PATH)
        if items is None:
            await interaction.response.send_message(f"{shop_name} というショップは存在しません。")
            return
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


# ---------- /渡す ----------
@tree.command(name="渡す", description="指定したユーザーにRaruinを渡す")
@app_commands.describe(target="渡したいユーザー（@で指定）", amount="渡す額")
async def transfer(interaction: discord.Interaction, target: discord.User, amount: int):
    if amount <= 0:
        await interaction.response.send_message("⚠️ 額は1以上にしてください。", ephemeral=True)
        return
    try:
        await db.execute(_add_user_if_not_exists_sync, interaction.user.id, DB_PATH)
        await db.execute(_add_user_if_not_exists_sync, target.id, DB_PATH)

        def _transfer_sync(from_id, to_id, amt, db_path):
            conn = sqlite3.connect(db_path, timeout=10)
            conn.execute("PRAGMA journal_mode=WAL;")
            cur = conn.cursor()
            cur.execute("SELECT balance FROM users WHERE user_id=?", (from_id,))
            r = cur.fetchone()
            if not r or r[0] < amt:
                conn.close()
                return False, (r[0] if r else 0)
            cur.execute("UPDATE users SET balance = balance - ?, total_spent = total_spent + ? WHERE user_id=?", (amt, amt, from_id))
            cur.execute("UPDATE users SET balance = balance + ?, total_received = total_received + ? WHERE user_id=?", (amt, amt, to_id))
            conn.commit()
            conn.close()
            return True, None

        ok, short = await db.execute(_transfer_sync, interaction.user.id, target.id, amount, DB_PATH)
        if not ok:
            await interaction.response.send_message(f"💰 残高が足りません（現在: {short}）", ephemeral=True)
            return

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
        "（略）"  # 長い文はそのまま元のヘルプ文を入れてください
    )
    await interaction.response.send_message(msg, ephemeral=True)


# ---------- /今日の収支 ----------
@tree.command(name="今日の収支", description="自分の受取・支出・残高をまとめて表示")
async def today_income(interaction: discord.Interaction):
    try:
        await db.execute(_add_user_if_not_exists_sync, interaction.user.id, DB_PATH)
        def _today_sync(uid, db_path):
            conn = sqlite3.connect(db_path, timeout=10)
            cur = conn.cursor()
            cur.execute("SELECT balance, total_received, total_spent FROM users WHERE user_id=?", (uid,))
            r = cur.fetchone()
            conn.close()
            return r if r else (0, 0, 0)
        balance, received, spent = await db.execute(_today_sync, interaction.user.id, DB_PATH)
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
        def _search_sync(kw, db_path):
            conn = sqlite3.connect(db_path, timeout=10)
            cur = conn.cursor()
            cur.execute("CREATE TABLE IF NOT EXISTS shop_items (item_id INTEGER PRIMARY KEY AUTOINCREMENT, shop_id INTEGER, name TEXT, description TEXT, price INTEGER, stock TEXT, role_id INTEGER, role_duration INTEGER)")
            cur.execute("CREATE TABLE IF NOT EXISTS shops (shop_id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE)")
            cur.execute(
                "SELECT s.name, i.name, i.price FROM shop_items i JOIN shops s ON i.shop_id = s.shop_id WHERE i.name LIKE ?",
                (f"%{kw}%",)
            )
            rows = cur.fetchall()
            conn.close()
            return rows
        rows = await db.execute(_search_sync, keyword, DB_PATH)
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
        def _recent_sync(uid, db_path):
            conn = sqlite3.connect(db_path, timeout=10)
            cur = conn.cursor()
            cur.execute('CREATE TABLE IF NOT EXISTS purchase_history (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, shop_name TEXT, item_name TEXT, price INTEGER, timestamp TEXT)')
            cur.execute("SELECT shop_name, item_name, price, timestamp FROM purchase_history WHERE user_id=? ORDER BY id DESC LIMIT 5", (uid,))
            rows = cur.fetchall()
            conn.close()
            return rows
        rows = await db.execute(_recent_sync, interaction.user.id, DB_PATH)
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
        def _leader_sync(db_path):
            conn = sqlite3.connect(db_path, timeout=10)
            cur = conn.cursor()
            cur.execute("CREATE TABLE IF NOT EXISTS users (user_id INTEGER PRIMARY KEY, balance INTEGER DEFAULT 0, total_received INTEGER DEFAULT 0, total_spent INTEGER DEFAULT 0)")
            cur.execute("SELECT user_id, balance, total_received, total_spent FROM users ORDER BY balance DESC LIMIT 15")
            rows = cur.fetchall()
            conn.close()
            return rows
        rows = await db.execute(_leader_sync, DB_PATH)
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


# ---------- 役立ち / bot情報 / tips ----------
@tree.command(name="役立ち", description="便利なRaruin情報を表示")
async def tips(interaction: discord.Interaction):
    msg = (
        "💡 役立ち情報:\n"
        "・文字数1文字 = 1 Raruin 取得可能\n"
        "・通話1分 = 12 Raruin 取得可能（ミュートでも）\n"
        "・ショップを見て安く買える商品をチェック\n"
    )
    await interaction.response.send_message(msg)

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
# ---------- Part 5: ギャンブル・自動付与・起動ログ・keep-alive (修正版) ----------
import os
import random
import time
import sqlite3
import asyncio
from datetime import datetime
from discord.ext import tasks
from discord import app_commands
from discord.ui import View, Button
import discord

# Flask only once
from flask import Flask
from threading import Thread

# DB path from env (fallback to main.db)
DB_PATH = os.getenv("DB_PATH", "main.db")
PORT = int(os.getenv("PORT", "8080"))

# ---------- Flask keep-alive (safe single start) ----------
_flask_started = globals().get("_flask_started", False)
if not _flask_started:
    app = Flask("RaruinKeepAlive")

    @app.route("/")
    def _home():
        return "Raruin Bot is alive!"

    def _run_flask():
        try:
            # Use PORT env var; if port already in use, catch and continue
            app.run(host="0.0.0.0", port=PORT)
        except OSError as e:
            print(f"⚠️ Flask: port {PORT} is unavailable: {e}")
        except Exception as e:
            print(f"⚠️ Flask unexpected error: {e}")

    Thread(target=_run_flask, daemon=True).start()
    globals()["_flask_started"] = True
    print(f"✅ Flask keep-alive started on port {PORT} (or attempted).")

# ---------- DB helpers (each operation opens a connection) ----------
def _ensure_db_dir(path):
    db_dir = os.path.dirname(path)
    if db_dir:
        try:
            os.makedirs(db_dir, exist_ok=True)
            print(f"✅ DB ディレクトリ作成: {db_dir}")
        except Exception as e:
            print(f"⚠️ DB ディレクトリ作成エラー ({db_dir}): {e}")

# attempt to create dir (if permission denied, we'll fall back later)
try:
    _ensure_db_dir(DB_PATH)
except Exception:
    pass

def get_conn():
    # If DB_PATH directory is not writable, fall back to local main.db
    path = DB_PATH
    try:
        conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    except Exception as e:
        print(f"⚠️ DB open error for {path}: {e} — falling back to main.db")
        path = "main.db"
        conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn

def init_tables():
    conn = get_conn()
    c = conn.cursor()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        balance INTEGER DEFAULT 0,
        total_received INTEGER DEFAULT 0,
        total_spent INTEGER DEFAULT 0,
        daily_streak INTEGER DEFAULT 0,
        last_daily TEXT
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
    """)
    c.execute("INSERT OR IGNORE INTO gamble_settings (id, probability_level) VALUES (1,3)")
    conn.commit()
    conn.close()
    print("✅ DB テーブル確認・作成済み")

# initialize at import time
init_tables()

# ---------- helper functions ----------
def add_user_if_not_exists(user_id: int):
    conn = get_conn()
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
    conn.commit()
    conn.close()

def update_balance(user_id: int, amount: int):
    """amount が正なら受取、負なら支出として記録"""
    conn = get_conn()
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
    if amount >= 0:
        c.execute("UPDATE users SET balance = balance + ?, total_received = total_received + ? WHERE user_id=?",
                  (amount, amount, user_id))
    else:
        c.execute("UPDATE users SET balance = balance + ?, total_spent = total_spent + ? WHERE user_id=?",
                  (amount, -amount, user_id))
    conn.commit()
    conn.close()

def get_balance(user_id: int) -> int:
    conn = get_conn()
    c = conn.cursor()
    c.execute("SELECT balance FROM users WHERE user_id=?", (user_id,))
    row = c.fetchone()
    conn.close()
    return row[0] if row else 0

def get_probability() -> int:
    conn = get_conn()
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO gamble_settings (id, probability_level) VALUES (1,3)")
    c.execute("SELECT probability_level FROM gamble_settings WHERE id=1")
    row = c.fetchone()
    conn.commit()
    conn.close()
    return row[0] if row else 3

# ---------- startup log ----------
async def startup_log():
    print("===================================================")
    print("🚀 Bot 起動開始")
    print("日時:", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print(f"DB: {DB_PATH}")
    print("Flask keep-alive: running (if port was free).")
    print("テーブル: users, gamble_settings, purchase_history, shops, shop_items")
    print("===================================================")
    await asyncio.sleep(0.05)

# ---------- chat earnings (register listener safely) ----------
async def _on_message_for_earnings(message):
    # avoid interfering with other on_message handlers: this is registered as an extra listener
    if message.author.bot:
        return
    content = message.content or ""
    if content.strip():
        earned = len(content)
        # run sync DB write in thread so we don't block
        def _write():
            add_user_if_not_exists(message.author.id)
            conn = get_conn()
            c = conn.cursor()
            c.execute("UPDATE users SET balance = balance + ?, total_received = total_received + ? WHERE user_id=?",
                      (earned, earned, message.author.id))
            conn.commit()
            conn.close()
        await asyncio.to_thread(_write)

# register the listener only if bot exists (avoid NameError when loading standalone)
if "bot" in globals():
    try:
        bot.add_listener(_on_message_for_earnings, "on_message")
        print("✅ on_message earnings listener registered")
    except Exception as e:
        print(f"⚠️ failed to add on_message listener: {e}")

# ---------- voice_check (1 minute loop) ----------
@tasks.loop(minutes=1)
async def voice_check():
    if "bot" not in globals():
        return
    for guild in bot.guilds:
        for vc in guild.voice_channels:
            for member in vc.members:
                if member.bot:
                    continue
                # use update_balance (thread-safe per-call)
                update_balance(member.id, 12)

# ---------- 汎用勝敗判定 ----------
last_win_times = {}

def is_win(user_id: int, base_chance: int = 0) -> bool:
    now = time.time()
    if user_id in last_win_times and now - last_win_times[user_id] < 60:
        return False
    prob = get_probability()
    chance_table = {1:20,2:15,3:10,4:5,5:2,6:1}
    chance = min(chance_table.get(prob, 10) + base_chance, 100)
    win = random.randint(1,100) <= chance
    if win:
        last_win_times[user_id] = now
    return win

# ---------- スロット ----------
@tree.command(name="スロット", description="スロットで遊ぶ")
@app_commands.describe(bet="ベット額")
async def slot(interaction: discord.Interaction, bet: int):
    user_id = interaction.user.id
    add_user_if_not_exists(user_id)
    balance = get_balance(user_id)
    if bet <= 0 or balance < bet:
        await interaction.response.send_message(f"💰 残高不足です。あなたの残高: {balance}", ephemeral=True)
        return

    prob_level = get_probability()
    symbol_table = {
        1:["🍒","🍋","🍇","⭐","💎"],
        2:["🍒","🍋","🍇","⭐","💎","🍉","🔔"],
        3:["🍒","🍋","🍇","⭐","💎","🍉","🔔","🍀","🥝","🍊"],
        4:["🍒","🍋","🍇","⭐","💎","🍉","🔔","🍀","🥝","🍊","🎱","💰","🪙"],
        5:["🍒","🍋","🍇","⭐","💎","🍉","🔔","🍀","🥝","🍊","🎱","💰","🪙","🍎","🍆"],
        6:["🍒","🍋","🍇","⭐","💎","🍉","🔔","🍀","🥝","🍊","🎱","💰","🪙","🍎","🍆","🍌","🥭","🍍","🥥","🥕"]
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

    await interaction.response.send_message(f"🎰 {' | '.join(reels)}\n{msg}\n残高: {get_balance(user_id)} Raruin")

# ---------- コイントス ----------
@tree.command(name="コイントス", description="コイントスで勝負")
@app_commands.describe(bet="掛け金")
async def coin(interaction: discord.Interaction, bet: int):
    user_id = interaction.user.id
    add_user_if_not_exists(user_id)
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
        self.next_card = random.randint(1,13)
        self.bet = bet

    async def finish(self, interaction: discord.Interaction, win: bool):
        if win:
            payout = int(self.bet * 0.3)
            msg = f"🎉 勝ち! +{payout} Raruin"
        else:
            payout = -self.bet
            msg = f"💀 負け... -{self.bet} Raruin"
        update_balance(self.user_id, payout)
        await interaction.response.edit_message(content=f"{self.current_card} → {self.next_card}\n{msg}\n残高: {get_balance(self.user_id)} Raruin", view=None)

    @discord.ui.button(label="ハイ", style=discord.ButtonStyle.primary)
    async def hi_button(self, interaction: discord.Interaction, button: Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ あなた専用です", ephemeral=True)
            return
        await self.finish(interaction, self.next_card > self.current_card)

    @discord.ui.button(label="ロー", style=discord.ButtonStyle.secondary)
    async def low_button(self, interaction: discord.Interaction, button: Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ あなた専用です", ephemeral=True)
            return
        await self.finish(interaction, self.next_card < self.current_card)

@tree.command(name="ハイアンドロー", description="ハイアンドローで勝負")
@app_commands.describe(bet="掛け金")
async def hilo(interaction: discord.Interaction, bet: int):
    user_id = interaction.user.id
    add_user_if_not_exists(user_id)
    if bet <= 0 or get_balance(user_id) < bet:
        await interaction.response.send_message("💰 残高不足です。", ephemeral=True)
        return
    view = HiLoView(user_id, random.randint(1,13), bet)
    await interaction.response.send_message(f"現在のカード: {view.current_card}\nハイかローを選んでください。", view=view, ephemeral=True)

# ---------- ルーレット ----------
@tree.command(name="ルーレット", description="ルーレットで勝負")
@app_commands.describe(bet="掛け金", choice="赤/黒/偶数/奇数/番号(0-36)")
async def roulette(interaction: discord.Interaction, bet: int, choice: str):
    user_id = interaction.user.id
    add_user_if_not_exists(user_id)
    if bet <= 0 or get_balance(user_id) < bet:
        await interaction.response.send_message("💰 残高不足です。", ephemeral=True)
        return

    choice_str = (choice or "").strip()
    number = random.randint(0,36)
    color = "赤" if number % 2 == 0 else "黒"
    win=False; payout=0

    if choice_str.isdigit():
        chosen_num = int(choice_str)
        if 0 <= chosen_num <= 36 and chosen_num == number:
            win=True; payout=bet*35
    if not win and choice_str in ("赤","黒"):
        if choice_str == color:
            win=True; payout=bet*2
    if not win and choice_str in ("偶数","奇数"):
        if (number % 2 == 0 and choice_str=="偶数") or (number % 2 == 1 and choice_str=="奇数"):
            win=True; payout=bet*2

    if win:
        update_balance(user_id, payout)
        msg=f"🎉 勝ち! {payout} Raruin 獲得"
    else:
        update_balance(user_id, -bet)
        msg=f"💀 負け... -{bet} Raruin"

    await interaction.response.send_message(f"ルーレット: 出目 {number} ({color})\n{msg}\n残高: {get_balance(user_id)} Raruin")

# ---------- ポーカー ----------
def hand_rank_by_counts(rank_list):
    order = {r:i for i,r in enumerate(['2','3','4','5','6','7','8','9','10','J','Q','K','A'], start=2)}
    counts={}
    for r in rank_list:
        counts[r]=counts.get(r,0)+1
    items = sorted([(cnt, order[r], r) for r,cnt in counts.items()], key=lambda x:(-x[0], -x[1]))
    cnts = sorted(counts.values(), reverse=True)
    if cnts[0]==4: category=6
    elif cnts[0]==3 and len(cnts)>1 and cnts[1]==2: category=5
    elif cnts[0]==3: category=4
    elif cnts[0]==2 and len(cnts)>1 and cnts[1]==2: category=3
    elif cnts[0]==2: category=2
    else: category=1
    tiebreaker=[]
    for cnt, rv, r in items:
        tiebreaker.append(cnt); tiebreaker.append(rv)
    return (category, tiebreaker)

@tree.command(name="ポーカー", description="5枚カードで勝負（対ディーラー）")
@app_commands.describe(bet="掛け金")
async def poker(interaction: discord.Interaction, bet: int):
    user_id = interaction.user.id
    add_user_if_not_exists(user_id)
    if bet <= 0 or get_balance(user_id) < bet:
        await interaction.response.send_message("💰 残高不足です。", ephemeral=True)
        return

    ranks = [str(n) for n in range(2,11)] + ["J","Q","K","A"]
    suits = ["♠","♥","♦","♣"]
    deck = [r+s for r in ranks for s in suits]
    cards = random.sample(deck, 10)
    player_cards = cards[:5]; dealer_cards = cards[5:]
    player_ranks = [c[:-1] for c in player_cards]; dealer_ranks = [c[:-1] for c in dealer_cards]
    player_rank = hand_rank_by_counts(player_ranks); dealer_rank = hand_rank_by_counts(dealer_ranks)

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

    mult_map={6:10,5:7,4:3,3:2,2:1.5,1:0}
    mult = mult_map.get(player_rank[0], 0)

    if winner=="player" and mult>0:
        payout=int(bet*mult); update_balance(user_id, payout); msg=f"🎉 あなたの勝ち! ({payout} Raruin 獲得)"
    elif winner=="player" and mult==0:
        payout=bet; update_balance(user_id, payout); msg=f"🎉 あなたの勝ち! ハイカードで勝利: +{payout} Raruin"
    elif winner=="tie":
        msg="🤝 引き分けです。ベットは返却されます。"
    else:
        payout=-bet; update_balance(user_id, payout); msg=f"💀 あなたの負け... -{bet} Raruin"

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

# ---------- Bot 起動 ----------
async def main():
    # 起動時のログ関数があれば呼ぶ（存在チェック）
    if "startup_log" in globals() and callable(globals()["startup_log"]):
        try:
            await globals()["startup_log"]()
        except Exception as e:
            print(f"[main] startup_log error: {e}")

    print("🟢 Discord Bot を接続中…")
    try:
        # bot.start はキャンセル可能なので例外処理で安全にログを残す
        await bot.start(TOKEN)
    except KeyboardInterrupt:
        print("[main] KeyboardInterrupt - shutting down")
        try:
            await bot.close()
        except Exception:
            pass
    except Exception as e:
        # 起動時の致命的エラーを記録（Render のログで確認しやすくする）
        print(f"[main] bot.start error: {e}")
        try:
            await bot.close()
        except Exception:
            pass

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        print(f"[__main__] fatal error: {e}")
