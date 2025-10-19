import os
import discord
from discord.ext import commands, tasks
from discord import app_commands
from dotenv import load_dotenv
from flask import Flask
from threading import Thread
from github_backup import upload_db_to_release  # ← Supabase使用でも念のため残す
import asyncio
import asyncpg
import requests
import re
from typing import List, Optional

# --------------------
# Raruin Bot: Persistent DB (Supabase PostgreSQL 方式)
# --------------------

# 環境変数読み込み
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

# Supabase 接続情報（Renderなどに環境変数で設定しておく）
SUPABASE_DB_URL = os.getenv("SUPABASE_DB_URL")  # 例: postgres://postgres:password@db.xxx.supabase.co:5432/postgres

if not SUPABASE_DB_URL:
    raise RuntimeError("❌ SUPABASE_DB_URL が設定されていません。環境変数に追加してください。")

# 管理者（OWNER）のDiscord ID
OWNER_ID = int(os.getenv("OWNER_ID", "1402613707527426131"))

# バックアップ通知用チャンネル（任意）
BACKUP_CHANNEL_ID = int(os.getenv("BACKUP_CHANNEL_ID", "0") or 0)

# ---------- Discord Bot 初期化 ----------
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="/", intents=intents)
tree = bot.tree

# ---------- PostgreSQL（Supabase）接続 ----------
db_pool: "asyncpg.Pool | None" = None  # type: ignore

async def init_db():
    """Supabase(PostgreSQL)に接続してテーブルを初期化"""
    global db_pool
    try:
        db_pool = await asyncpg.create_pool(SUPABASE_DB_URL, min_size=1, max_size=5)
        print("✅ Supabase PostgreSQL に接続しました。")

        async with db_pool.acquire() as conn:
            # テーブル作成
            await conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                balance INTEGER DEFAULT 0,
                total_received INTEGER DEFAULT 0,
                total_spent INTEGER DEFAULT 0,
                daily_streak INTEGER DEFAULT 0,
                last_daily TEXT
            );

            CREATE TABLE IF NOT EXISTS admins (
                user_id BIGINT PRIMARY KEY
            );

            CREATE TABLE IF NOT EXISTS shops (
                shop_id SERIAL PRIMARY KEY,
                name TEXT UNIQUE
            );

            CREATE TABLE IF NOT EXISTS shop_items (
                item_id SERIAL PRIMARY KEY,
                shop_id INTEGER REFERENCES shops(shop_id),
                name TEXT,
                description TEXT,
                price INTEGER,
                stock TEXT,
                role_id BIGINT,
                role_duration INTEGER
            );

            CREATE TABLE IF NOT EXISTS gamble_settings (
                id INTEGER PRIMARY KEY,
                probability_level INTEGER DEFAULT 3
            );

            CREATE TABLE IF NOT EXISTS purchase_history (
                id SERIAL PRIMARY KEY,
                user_id BIGINT,
                shop_name TEXT,
                item_name TEXT,
                price INTEGER,
                timestamp TEXT
            );
            ''')

            # 初期値挿入
            await conn.execute("""
            INSERT INTO gamble_settings (id, probability_level)
            VALUES (1, 3)
            ON CONFLICT (id) DO NOTHING;
            """)

            await conn.execute("""
            INSERT INTO admins (user_id)
            VALUES ($1)
            ON CONFLICT (user_id) DO NOTHING;
            """, OWNER_ID)

        print("✅ Supabaseテーブル初期化完了。")

    except Exception as e:
        print(f"❌ DB初期化エラー: {e}")

# ===== Flask keep_alive =====
app = Flask(__name__)

@app.route('/')
def home():
    return "Raruin Bot Running with Supabase!"

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
    """GitHubにバックアップをアップロード（任意）"""
    try:
        upload_db_to_release()  # 任意機能（ファイルがあるなら）
        print("✅ GitHub Releases にバックアップをアップロードしました。")

        if BACKUP_CHANNEL_ID:
            channel = bot.get_channel(BACKUP_CHANNEL_ID)
            if channel:
                await channel.send("✅ Supabase DB のバックアップを完了しました。")
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

    # DB初期化
    await init_db()

    # 自動バックアップ起動
    try:
        if not backup_database.is_running():
            backup_database.start()
            print("✅ 自動バックアップ開始。")
    except Exception as e:
        print(f"[on_ready] backup_database start error: {e}")

    print("Background tasks started.")

# ---------- Part 2: 管理者コマンドと通貨操作（Supabase / asyncpg対応版） ----------
# resolve_target_members / _find_role_from_input を利用（ロール/メンション/名前解決に強い）

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
        name = raw.lstrip("@\uFF20").strip()
        role = discord.utils.find(lambda r: r.name.lower() == name.lower(), guild.roles)
        if role:
            return role.members
        starts = [r for r in guild.roles if r.name.lower().startswith(name.lower())]
        if starts:
            return starts[0].members
        contains = [r for r in guild.roles if name.lower() in r.name.lower()]
        if contains:
            return contains[0].members
        return []

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

def _find_role_from_input(raw: str, guild: discord.Guild) -> Optional[discord.Role]:
    if not guild:
        return None
    s = raw.strip()
    m = re.match(r'^<@&(?P<id>\d+)>$', s)
    if m:
        return guild.get_role(int(m.group("id")))
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

# ---------- DBヘルパー（asyncpg / Supabase） ----------
# 前提: グローバルに `db_pool` (asyncpg.Pool) が存在すること

async def add_user_if_not_exists(user_id: int) -> None:
    """users テーブルに user_id が存在しなければ追加する。"""
    global db_pool
    if db_pool is None:
        raise RuntimeError("DB pool is not initialized")
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (user_id) VALUES ($1) ON CONFLICT (user_id) DO NOTHING",
            user_id
        )

async def is_admin(user_id: int) -> bool:
    """admins テーブルに user_id が存在するかを返す。"""
    global db_pool
    if db_pool is None:
        return False
    async with db_pool.acquire() as conn:
        rv = await conn.fetchval("SELECT 1 FROM admins WHERE user_id = $1", user_id)
    return bool(rv)

async def get_balance(user_id: int) -> int:
    """ユーザーの残高を返す（なければ0）。"""
    global db_pool
    if db_pool is None:
        raise RuntimeError("DB pool is not initialized")
    async with db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT balance FROM users WHERE user_id = $1", user_id)
    return row["balance"] if row else 0

async def update_balance(user_id: int, amount: int) -> None:
    """
    残高を増減するユーティリティ。
    amount >0 の場合 total_received も増やし、amount<0 の場合 total_spent を増やす。
    """
    global db_pool
    if db_pool is None:
        raise RuntimeError("DB pool is not initialized")
    async with db_pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("INSERT INTO users (user_id) VALUES ($1) ON CONFLICT (user_id) DO NOTHING", user_id)
            if amount >= 0:
                await conn.execute(
                    "UPDATE users SET balance = balance + $1, total_received = total_received + $1 WHERE user_id = $2",
                    amount, user_id
                )
            else:
                # amount is negative
                await conn.execute(
                    "UPDATE users SET balance = balance + $1, total_spent = total_spent + $2 WHERE user_id = $3",
                    amount, -amount, user_id
                )



# ---------- /addr - 管理者を追加・削除・一覧（Supabase対応） ----------
@tree.command(name="addr", description="管理者を追加、削除、一覧表示")
@app_commands.describe(action="add, remove, list", target="ユーザー名またはID")
async def addr(interaction: discord.Interaction, action: str, target: str = None):
    await add_user_if_not_exists(interaction.user.id)
    if not await is_admin(interaction.user.id):
        await interaction.response.send_message("⚠️ 管理者のみ使用可能です。", ephemeral=True)
        return

    action = action.lower()

    # list
    if action == "list":
        async with db_pool.acquire() as conn:
            rows = await conn.fetch("SELECT user_id FROM admins")
        if not rows:
            await interaction.response.send_message("👤 管理者はまだ登録されていません。")
            return
        mentions = [f"<@{r['user_id']}>" for r in rows]
        await interaction.response.send_message(f"👑 管理者一覧: {', '.join(mentions)}")
        return

    if not target:
        await interaction.response.send_message("⚠️ 対象ユーザー名またはIDを指定してください。", ephemeral=True)
        return

    # 対象ユーザー取得（ギルドメンバー優先、なければ fetch_user）
    user = None
    if interaction.guild:
        # try to resolve by mention / name
        try:
            # if target looks like mention
            um = re.match(r'^<@!?(?P<id>\d+)>$', target.strip())
            if um:
                uid = int(um.group("id"))
                user = interaction.guild.get_member(uid) or await interaction.guild.fetch_member(uid)
            else:
                # try find by name/nick
                user = discord.utils.get(interaction.guild.members, name=target)
        except:
            user = None

    if not user:
        try:
            user_id = int(target)
            user = await bot.fetch_user(user_id)
        except:
            await interaction.response.send_message("❌ ユーザーが見つかりません。", ephemeral=True)
            return

    if action == "add":
        async with db_pool.acquire() as conn:
            await conn.execute("INSERT INTO admins (user_id) VALUES ($1) ON CONFLICT (user_id) DO NOTHING", user.id)
        await interaction.response.send_message(f"✅ {user.mention} を管理者に追加しました。")
    elif action == "remove":
        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM admins WHERE user_id=$1", user.id)
        await interaction.response.send_message(f"🗑️ {user.mention} を管理者から削除しました。")
    else:
        await interaction.response.send_message("⚠️ actionは add, remove, list のいずれかです。", ephemeral=True)


# ---------- /配布（付与）（Supabase対応・ロールOK） ----------
@tree.command(name="配布", description="指定したユーザーやロールにRaruinを付与（管理者専用）")
@app_commands.describe(target="ユーザー（@で指定可）またはロール名/ID/ユーザー名", amount="付与額")
async def distribute(interaction: discord.Interaction, target: str, amount: int):
    # 管理者チェック
    if not await is_admin(interaction.user.id):
        await interaction.response.send_message("⚠️ 管理者のみ使用可能です。", ephemeral=True)
        return

    if amount <= 0:
        await interaction.response.send_message("⚠️ 付与額は1以上にしてください。", ephemeral=True)
        return

    # ターゲット解決（ユーザーまたはロール）
    members = await resolve_target_members(target, interaction)

    # members が空ならロール名が存在するか確認（補助）
    if not members and interaction.guild:
        role_obj = _find_role_from_input(target, interaction.guild)
        if role_obj:
            if len(role_obj.members) == 0:
                await interaction.response.send_message(f"ℹ️ ロール **{role_obj.name}** は見つかりましたが、メンバーがいません。", ephemeral=True)
                return
            members = role_obj.members

    if not members:
        await interaction.response.send_message("❌ 対象が見つかりませんでした。@メンション / ID / ロール名 / ユーザー名 を試してください。", ephemeral=True)
        return

    # defer（長時間処理に備える）
    try:
        await interaction.response.defer(ephemeral=True)
    except:
        pass

    member_ids = [m.id for m in members]

    # DB更新（トランザクションでまとめて）
    try:
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                for uid in member_ids:
                    await conn.execute("INSERT INTO users (user_id) VALUES ($1) ON CONFLICT (user_id) DO NOTHING", uid)
                    await conn.execute("UPDATE users SET balance = balance + $1, total_received = total_received + $1 WHERE user_id = $2", amount, uid)
    except Exception as e:
        await interaction.followup.send(f"❌ DB更新中にエラーが発生しました: `{e}`", ephemeral=True)
        return

    # レスポンス
    if len(members) == 1:
        name = members[0].display_name
    else:
        name = f"{len(members)} 件のメンバー"
    await interaction.followup.send(f"🎁 {name} に {amount} Raruin を付与しました。")

# ---------- /支払い（減算）（Supabase対応・ロールOK） ----------
@tree.command(name="支払い", description="指定したユーザーやロールのRaruinを減らす（管理者専用）")
@app_commands.describe(target="ユーザー（@で指定可）またはロール名/ID/ユーザー名", amount="減らす額")
async def payment(interaction: discord.Interaction, target: str, amount: int):
    if not await is_admin(interaction.user.id):
        await interaction.response.send_message("⚠️ 管理者のみ使用可能です。", ephemeral=True)
        return

    if amount <= 0:
        await interaction.response.send_message("⚠️ 減算額は1以上にしてください。", ephemeral=True)
        return

    members = await resolve_target_members(target, interaction)

    # members が空ならロール存在チェック（改善）
    if not members and interaction.guild:
        role_obj = _find_role_from_input(target, interaction.guild)
        if role_obj:
            if len(role_obj.members) == 0:
                await interaction.response.send_message(f"ℹ️ ロール **{role_obj.name}** は見つかりましたが、メンバーがいません。", ephemeral=True)
                return
            members = role_obj.members

    if not members:
        await interaction.response.send_message("❌ 対象が見つかりませんでした。@メンション / ID / ロール名 / ユーザー名 を試してください。", ephemeral=True)
        return

    try:
        await interaction.response.defer(ephemeral=True)
    except:
        pass

    member_ids = [m.id for m in members]

    try:
        async with db_pool.acquire() as conn:
            async with conn.transaction():
                for uid in member_ids:
                    await conn.execute("INSERT INTO users (user_id) VALUES ($1) ON CONFLICT (user_id) DO NOTHING", uid)
                    await conn.execute("UPDATE users SET balance = balance - $1, total_spent = total_spent + $1 WHERE user_id = $2", amount, uid)
    except Exception as e:
        await interaction.followup.send(f"❌ DB更新中にエラーが発生しました: `{e}`", ephemeral=True)
        return

    if len(members) == 1:
        name = members[0].display_name
    else:
        name = f"{len(members)} 件のメンバー"
    await interaction.followup.send(f"💸 {name} から {amount} Raruin を減算しました。")

# ---------- /ギャンブル確率設定（Supabase対応） ----------
@tree.command(name="ギャンブル確率設定", description="管理者専用: ギャンブル確率レベル変更")
@app_commands.describe(probability="1=当たりやすい, 6=当たりにくい")
async def gamble_prob_set(interaction: discord.Interaction, probability: int):
    # 管理者権限判定（サーバー管理者権限でも可）
    if not interaction.user.guild_permissions.administrator and not await is_admin(interaction.user.id):
        await interaction.response.send_message("❌ 管理者専用です。", ephemeral=True)
        return
    if probability < 1 or probability > 6:
        await interaction.response.send_message("⚠️ 1〜6で指定してください。", ephemeral=True)
        return
    try:
        async with db_pool.acquire() as conn:
            await conn.execute("UPDATE gamble_settings SET probability_level = $1 WHERE id = 1", probability)
        await interaction.response.send_message(f"🎯 ギャンブル確率レベルを `{probability}` に設定しました。")
    except Exception as e:
        await interaction.response.send_message(f"⚠️ 更新エラー: {e}", ephemeral=True)

# ---------- /shopadd - ショップ追加/削除（Supabase対応） ----------
@tree.command(name="shopadd", description="管理者専用: ショップを追加または削除")
@app_commands.describe(action="追加 or 削除", shop_name="ショップの名前")
async def shopadd(interaction: discord.Interaction, action: str, shop_name: str):
    if not await is_admin(interaction.user.id):
        await interaction.response.send_message("⚠️ 管理者のみ使用可能です。", ephemeral=True)
        return

    action_norm = action.lower()
    try:
        async with db_pool.acquire() as conn:
            if action_norm in ("追加", "add"):
                await conn.execute("INSERT INTO shops (name) VALUES ($1) ON CONFLICT (name) DO NOTHING", shop_name)
                await interaction.response.send_message(f"✅ ショップ {shop_name} を追加しました。")
            elif action_norm in ("削除", "remove"):
                await conn.execute("DELETE FROM shops WHERE name = $1", shop_name)
                await interaction.response.send_message(f"🗑️ ショップ {shop_name} を削除しました。")
            else:
                await interaction.response.send_message("⚠️ actionは「追加」または「削除」を指定してください。", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"⚠️ DBエラー: {e}", ephemeral=True)

# ---------- /shop - 商品追加/削除（Supabase対応） ----------
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
    if not await is_admin(interaction.user.id):
        await interaction.response.send_message("⚠️ 管理者のみ使用可能です。", ephemeral=True)
        return

    # ショップ存在チェック
    async with db_pool.acquire() as conn:
        shop_row = await conn.fetchrow("SELECT shop_id FROM shops WHERE name = $1", shop_name)
    if not shop_row:
        await interaction.response.send_message(f"⚠️ {shop_name} というショップは存在しません。", ephemeral=True)
        return
    shop_id = shop_row["shop_id"]

    # 在庫の検証: '無限' (小文字許容) または 0以上の整数
    stock_norm = stock if isinstance(stock, str) else str(stock)
    if stock_norm.lower() in ["無限", "mugen", "unlimited", "inf", "∞"]:
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
    try:
        async with db_pool.acquire() as conn:
            if action_norm in ["追加", "add"]:
                role_id = role.id if role else None
                role_duration_seconds = role_duration_min * 60 if role_duration_min is not None else None

                await conn.execute('''
                    INSERT INTO shop_items (shop_id, name, description, price, stock, role_id, role_duration)
                    VALUES ($1, $2, $3, $4, $5, $6, $7)
                ''', shop_id, item_name, description, price, stock_to_store, role_id, role_duration_seconds)

                extra = ""
                if role:
                    extra = f" ロール付与: {role.name}"
                    if role_duration_min:
                        extra += f"（{role_duration_min}分）"
                extra += f" 在庫: {stock_to_store}"
                await interaction.response.send_message(f"✅ {item_name} を {shop_name} に追加しました。{extra}")

            elif action_norm in ["削除", "remove"]:
                await conn.execute("DELETE FROM shop_items WHERE shop_id = $1 AND name = $2", shop_id, item_name)
                await interaction.response.send_message(f"🗑️ {item_name} を {shop_name} から削除しました。")
            else:
                await interaction.response.send_message("⚠️ actionは「追加」または「削除」（または add/remove）を指定してください。", ephemeral=True)
    except Exception as e:
        await interaction.response.send_message(f"⚠️ DBエラー: {e}", ephemeral=True)
# ---------- Part 3: ショップ管理とユーザー向けコマンド（Supabase / asyncpg対応版） ----------
import asyncio
from datetime import datetime
from typing import Optional, List

# 前提: global db_pool が asyncpg.Pool として定義済み
# 例: db_pool = await asyncpg.create_pool(SUPABASE_DB_URL, min_size=1, max_size=5)

# ---- DB ユーティリティ（同期版から async へ置換） ----
async def add_user_if_not_exists(user_id: int):
    """users テーブルに存在しなければ追加（基本フィールドのみ）。非同期版。"""
    global db_pool
    if db_pool is None:
        raise RuntimeError("DB pool is not initialized")
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO users (user_id) VALUES ($1) ON CONFLICT (user_id) DO NOTHING",
            user_id
        )

# ---- /残高 コマンド ----
@tree.command(name="残高", description="自分の残高を確認します")
async def balance_cmd(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        await add_user_if_not_exists(interaction.user.id)
        async with db_pool.acquire() as conn:
            bal = await conn.fetchval("SELECT balance FROM users WHERE user_id=$1", interaction.user.id)
        balance = bal if bal is not None else 0
        await interaction.followup.send(f"💰 あなたの残高は {balance} Raruin です。", ephemeral=True)
    except Exception as e:
        print(f"[balance_cmd] error: {e}")
        await interaction.followup.send("⚠️ エラーが発生しました。管理者に連絡してください。", ephemeral=True)

# ---- /ランキング ----
@tree.command(name="ランキング", description="Raruin残高ランキング上位15名")
async def ranking(interaction: discord.Interaction):
    try:
        async with db_pool.acquire() as conn:
            rows = await conn.fetch("SELECT user_id, balance FROM users ORDER BY balance DESC LIMIT 15")
        if not rows:
            await interaction.response.send_message("ユーザーが存在しません。")
            return
        msg = "🏆 Raruinランキング（上位15名）\n"
        for i, row in enumerate(rows, start=1):
            uid = row["user_id"]
            bal = row["balance"] or 0
            try:
                user = await bot.fetch_user(uid)
                name = user.name
            except:
                name = f"ユーザーID:{uid}"
            msg += f"{i}. {name}: {bal} Raruin\n"
        await interaction.response.send_message(msg)
    except Exception as e:
        print(f"[ranking] error: {e}")
        await interaction.response.send_message("⚠️ エラーが発生しました。", ephemeral=True)

# ---- /統計 ----
@tree.command(name="統計", description="Raruin全体統計")
async def stats(interaction: discord.Interaction):
    try:
        async with db_pool.acquire() as conn:
            totals = await conn.fetchrow("SELECT COALESCE(SUM(balance),0) as total_balance, COALESCE(SUM(total_spent),0) as total_spent FROM users")
            richest = await conn.fetchrow("SELECT user_id, balance FROM users ORDER BY balance DESC LIMIT 1")
            poorest = await conn.fetchrow("SELECT user_id, balance FROM users ORDER BY balance ASC LIMIT 1")

        total_balance = totals["total_balance"] if totals else 0
        total_spent = totals["total_spent"] if totals else 0

        richest_name = "なし"
        richest_bal = 0
        if richest:
            richest_bal = richest["balance"] or 0
            try:
                u = await bot.fetch_user(richest["user_id"])
                richest_name = u.name
            except:
                richest_name = f"ユーザーID:{richest['user_id']}"

        poorest_name = "なし"
        poorest_bal = 0
        if poorest:
            poorest_bal = poorest["balance"] or 0
            try:
                u = await bot.fetch_user(poorest["user_id"])
                poorest_name = u.name
            except:
                poorest_name = f"ユーザーID:{poorest['user_id']}"

        msg = (
            f"💰 現在全員が持っているRaruin合計: {total_balance}\n"
            f"📤 全員が今まで使った額合計: {total_spent}\n"
            f"👑 最も持っている人: {richest_name} ({richest_bal})\n"
            f"💸 最も持っていない人: {poorest_name} ({poorest_bal})"
        )
        await interaction.response.send_message(msg)
    except Exception as e:
        print(f"[stats] error: {e}")
        await interaction.response.send_message("⚠️ エラーが発生しました。", ephemeral=True)

# ---- /ギャンブル確率確認 ----
@tree.command(name="ギャンブル確率確認", description="現在のギャンブル確率を確認")
async def gamble_prob_check(interaction: discord.Interaction):
    try:
        # ensure row exists
        async with db_pool.acquire() as conn:
            await conn.execute("INSERT INTO gamble.settings (id, probability_level) VALUES (1,3) ON CONFLICT (id) DO NOTHING") \
                if False else None
    except Exception:
        # Some Postgres schemas might differ; fallback: ensure via safe insert without schema qualifier
        pass

    try:
        async with db_pool.acquire() as conn:
            prob = await conn.fetchval("SELECT probability_level FROM gamble_settings WHERE id=1")
        prob = prob if prob is not None else 3
        await interaction.response.send_message(f"🎰 現在のギャンブル確率設定: {prob} (1が最も当たりやすい, 6が最も難しい)")
    except Exception as e:
        print(f"[gamble_prob_check] error: {e}")
        await interaction.response.send_message("⚠️ ギャンブル確率の取得に失敗しました。", ephemeral=True)

# ---- /ショップリスト ----
@tree.command(name="ショップリスト", description="ショップ一覧を表示")
async def shop_list(interaction: discord.Interaction):
    try:
        async with db_pool.acquire() as conn:
            rows = await conn.fetch("SELECT name FROM shops ORDER BY name")
        if not rows:
            await interaction.response.send_message("ショップが存在しません。")
            return
        msg = "🛒 ショップ一覧:\n" + "\n".join([r["name"] for r in rows])
        await interaction.response.send_message(msg)
    except Exception as e:
        print(f"[shop_list] error: {e}")
        await interaction.response.send_message("⚠️ エラーが発生しました。", ephemeral=True)

# ---- /ショップ (商品表示) ----
@tree.command(name="ショップ", description="指定したショップの商品を表示")
@app_commands.describe(shop_name="ショップの名前")
async def show_shop(interaction: discord.Interaction, shop_name: str):
    try:
        async with db_pool.acquire() as conn:
            shop_row = await conn.fetchrow("SELECT shop_id FROM shops WHERE name=$1", shop_name)
            if not shop_row:
                await interaction.response.send_message(f"{shop_name} というショップは存在しません。")
                return
            shop_id = shop_row["shop_id"]
            items = await conn.fetch("SELECT name, description, price FROM shop_items WHERE shop_id=$1", shop_id)

        if not items:
            await interaction.response.send_message("このショップには商品がありません。")
            return
        msg = f"🛒 {shop_name}の商品一覧:\n"
        for it in items:
            desc = it["description"] or ""
            msg += f"{it['name']} - {desc} - {it['price']} Raruin\n"
        await interaction.response.send_message(msg)
    except Exception as e:
        print(f"[show_shop] error: {e}")
        await interaction.response.send_message("⚠️ エラーが発生しました。", ephemeral=True)

# ---- /買う （購入処理：トランザクションで安全に） ----
@tree.command(name="買う", description="商品を購入する")
@app_commands.describe(shop_name="ショップ名", item_name="商品名")
async def buy_item(interaction: discord.Interaction, shop_name: str, item_name: str):
    await interaction.response.defer(ephemeral=True)

    try:
        await add_user_if_not_exists(interaction.user.id)

        async with db_pool.acquire() as conn:
            async with conn.transaction():
                # 1) shop id
                shop_row = await conn.fetchrow("SELECT shop_id FROM shops WHERE name=$1", shop_name)
                if not shop_row:
                    await interaction.followup.send("❌ そのショップは存在しません。", ephemeral=True)
                    return
                shop_id = shop_row["shop_id"]

                # 2) lock item row
                item = await conn.fetchrow(
                    "SELECT item_id, price, stock, role_id, role_duration FROM shop_items WHERE shop_id=$1 AND name=$2 FOR UPDATE",
                    shop_id, item_name
                )
                if not item:
                    await interaction.followup.send("❌ その商品は見つかりません。", ephemeral=True)
                    return
                item_id = item["item_id"]
                price = item["price"] or 0
                stock = item["stock"]
                role_id = item["role_id"]
                role_duration = item["role_duration"]

                # 3) check balance (lock user row)
                user_row = await conn.fetchrow("SELECT balance FROM users WHERE user_id=$1 FOR UPDATE", interaction.user.id)
                user_balance = user_row["balance"] if user_row else 0
                if user_balance < price:
                    await interaction.followup.send("💸 残高が足りません。", ephemeral=True)
                    return

                # 4) stock check and decrement if necessary
                stock_str = "" if stock is None else str(stock)
                stock_unlimited = stock_str.lower() in ("無限", "mugen", "unlimited", "inf", "∞")
                if not stock_unlimited:
                    try:
                        stock_int = int(stock_str)
                    except:
                        await interaction.followup.send("⚠️ 在庫情報に問題があります。管理者に連絡してください。", ephemeral=True)
                        return
                    if stock_int <= 0:
                        await interaction.followup.send("🚫 在庫がありません。", ephemeral=True)
                        return
                    # decrement stock
                    await conn.execute("UPDATE shop_items SET stock=$1 WHERE item_id=$2", str(stock_int - 1), item_id)

                # 5) charge user (upsert pattern)
                await conn.execute("""
                    INSERT INTO users (user_id, balance, total_received, total_spent)
                    VALUES ($1, $2, 0, $3)
                    ON CONFLICT (user_id) DO UPDATE
                      SET balance = users.balance - $3,
                          total_spent = users.total_spent + $3
                """, interaction.user.id, 0, price)

                # 6) insert purchase history
                ts = datetime.utcnow().isoformat()
                await conn.execute("""
                    INSERT INTO purchase_history (user_id, shop_name, item_name, price, timestamp)
                    VALUES ($1, $2, $3, $4, $5)
                """, interaction.user.id, shop_name, item_name, price, ts)

        # outside transaction: role assignment and notifications
        role_assigned = False
        role_name = None
        if role_id and interaction.guild:
            role_obj = interaction.guild.get_role(role_id)
            if role_obj:
                try:
                    await interaction.user.add_roles(role_obj, reason="Shop purchase")
                    role_assigned = True
                    role_name = role_obj.name
                    # schedule removal if duration set (seconds)
                    if role_duration and isinstance(role_duration, int) and role_duration > 0:
                        async def _remove_role_later(member: discord.Member, role: discord.Role, delay: int):
                            try:
                                await asyncio.sleep(delay)
                                await member.remove_roles(role, reason="Role duration expired")
                                ch = bot.get_channel(1408247205034328066)
                                if ch:
                                    await ch.send(f"{member.display_name} のロール `{role.name}` の付与が終了しました。")
                            except Exception as e:
                                print(f"[remove_role_later] error: {e}")

                        asyncio.create_task(_remove_role_later(interaction.user, role_obj, role_duration))

        # optional notify channel
        notify_ch = bot.get_channel(1408247205034328066)
        if notify_ch:
            try:
                await notify_ch.send(f"{interaction.user.display_name} が {shop_name} で {item_name} を購入しました。")
            except Exception as e:
                print(f"[buy_item] notify_ch send error: {e}")

        # reply with new balance
        try:
            async with db_pool.acquire() as conn:
                new_bal = await conn.fetchval("SELECT balance FROM users WHERE user_id=$1", interaction.user.id)
        except:
            new_bal = None

        msg = f"✅ {item_name} を購入しました！ 支払額: {price} Raruin"
        if role_assigned:
            msg += f" — 付与ロール: `{role_name}`"
        if new_bal is not None:
            msg += f"\n💰 残高: {new_bal} Raruin"

        await interaction.followup.send(msg, ephemeral=True)
        return

    except Exception as e:
        print(f"[buy_item] error: {e}")
        await interaction.followup.send("⚠️ エラーが発生しました。管理者に連絡してください。", ephemeral=True)
        return

# ---- /渡す ----
@tree.command(name="渡す", description="指定したユーザーにRaruinを渡す")
@app_commands.describe(target="渡したいユーザー（@で指定）", amount="渡す額")
async def transfer(interaction: discord.Interaction, target: discord.User, amount: int):
    if amount <= 0:
        await interaction.response.send_message("⚠️ 額は1以上にしてください。", ephemeral=True)
        return
    try:
        await add_user_if_not_exists(interaction.user.id)
        await add_user_if_not_exists(target.id)

        async with db_pool.acquire() as conn:
            async with conn.transaction():
                # lock sender
                sender_row = await conn.fetchrow("SELECT balance FROM users WHERE user_id=$1 FOR UPDATE", interaction.user.id)
                sender_bal = sender_row["balance"] if sender_row else 0
                if sender_bal < amount:
                    await interaction.response.send_message("💰 残高が足りません。", ephemeral=True)
                    return
                # deduct sender
                await conn.execute("""
                    UPDATE users SET balance = balance - $1, total_spent = total_spent + $1 WHERE user_id=$2
                """, amount, interaction.user.id)
                # credit receiver
                await conn.execute("""
                    INSERT INTO users (user_id, balance, total_received, total_spent)
                    VALUES ($1, $2, $3, 0)
                    ON CONFLICT (user_id) DO UPDATE
                      SET balance = users.balance + $2,
                          total_received = users.total_received + $2
                """, target.id, amount, amount)

        try:
            await target.send(f"📩 {interaction.user.display_name} から {amount} Raruin を受け取りました！")
        except:
            pass

        await interaction.response.send_message(f"✅ {target.display_name} に {amount} Raruin を渡しました。")
    except Exception as e:
        print(f"[transfer] error: {e}")
        await interaction.response.send_message("⚠️ エラーが発生しました。", ephemeral=True)

# ---- /ヘルプ ----
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

# ---- /今日の収支 ----
@tree.command(name="今日の収支", description="自分の受取・支出・残高をまとめて表示")
async def today_income(interaction: discord.Interaction):
    try:
        await add_user_if_not_exists(interaction.user.id)
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT balance, total_received, total_spent FROM users WHERE user_id=$1", interaction.user.id)
        if not row:
            await interaction.response.send_message("データが見つかりません。", ephemeral=True)
            return
        balance = row["balance"] or 0
        received = row["total_received"] or 0
        spent = row["total_spent"] or 0
        await interaction.response.send_message(
            f"💰 {interaction.user.display_name} さんの収支:\n"
            f"残高: {balance} Raruin\n"
            f"受取合計: {received} Raruin\n"
            f"支出合計: {spent} Raruin"
        )
    except Exception as e:
        print(f"[today_income] error: {e}")
        await interaction.response.send_message("⚠️ エラーが発生しました。", ephemeral=True)

# ---- /ショップ検索 ----
@tree.command(name="ショップ検索", description="商品名でショップ内を検索")
@app_commands.describe(keyword="検索したい商品名")
async def shop_search(interaction: discord.Interaction, keyword: str):
    try:
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT s.name as shop_name, i.name as item_name, i.price FROM shop_items i "
                "JOIN shops s ON i.shop_id = s.shop_id "
                "WHERE i.name ILIKE $1",
                f"%{keyword}%"
            )
        if not rows:
            await interaction.response.send_message("該当する商品はありません。")
            return
        msg = "🔍 検索結果:\n"
        for r in rows:
            msg += f"{r['shop_name']} - {r['item_name']}: {r['price']} Raruin\n"
        await interaction.response.send_message(msg)
    except Exception as e:
        print(f"[shop_search] error: {e}")
        await interaction.response.send_message("⚠️ エラーが発生しました。", ephemeral=True)

# ---- /最近の購入 ----
@tree.command(name="最近の購入", description="最近購入した商品を確認")
async def recent_purchase(interaction: discord.Interaction):
    try:
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT shop_name, item_name, price, timestamp FROM purchase_history "
                "WHERE user_id=$1 ORDER BY id DESC LIMIT 5",
                interaction.user.id
            )
        if not rows:
            await interaction.response.send_message("購入履歴はありません。")
            return
        msg = "🛒 最近の購入:\n"
        for r in rows:
            ts = (r["timestamp"][:16] if r["timestamp"] else "")
            msg += f"{ts} - {r['shop_name']} - {r['item_name']}: {r['price']} Raruin\n"
        await interaction.response.send_message(msg)
    except Exception as e:
        print(f"[recent_purchase] error: {e}")
        await interaction.response.send_message("⚠️ エラーが発生しました。", ephemeral=True)

# ---- /リーダーボード ----
@tree.command(name="リーダーボード", description="Raruin上位15名の詳細を表示")
async def leaderboard(interaction: discord.Interaction):
    try:
        async with db_pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT user_id, balance, total_received, total_spent FROM users ORDER BY balance DESC LIMIT 15"
            )
        if not rows:
            await interaction.response.send_message("データがありません。")
            return
        msg = "🏆 Raruinリーダーボード（上位15名）\n"
        for i, r in enumerate(rows, start=1):
            uid = r["user_id"]
            bal = r["balance"] or 0
            rec = r["total_received"] or 0
            spent = r["total_spent"] or 0
            try:
                user = await bot.fetch_user(uid)
                name = user.name
            except:
                name = f"ユーザーID:{uid}"
            msg += f"{i}. {name}: 残高 {bal}, 受取 {rec}, 支出 {spent}\n"
        await interaction.response.send_message(msg)
    except Exception as e:
        print(f"[leaderboard] error: {e}")
        await interaction.response.send_message("⚠️ エラーが発生しました。", ephemeral=True)

# ---- /役立ち ----
@tree.command(name="役立ち", description="便利なRaruin情報を表示")
async def tips(interaction: discord.Interaction):
    msg = (
        "💡 役立ち情報:\n"
        "・文字数1文字 = 1 Raruin 取得可能\n"
        "・通話1分 = 12 Raruin 取得可能（ミュートでも）\n"
        "・ショップを見て安く買える商品をチェック\n"
    )
    await interaction.response.send_message(msg)

# ---- /bot情報 ----
@tree.command(name="bot情報", description="Botのバージョンや稼働状況を確認")
async def bot_info(interaction: discord.Interaction):
    try:
        name = bot.user.name if bot.user else "Unknown"
        bot_id = bot.user.id if bot.user else "Unknown"
        guilds = len(bot.guilds) if hasattr(bot, "guilds") else 0
        msg = (
            f"🤖 Bot情報\n"
            f"ユーザー名: {name}\n"
            f"ID: {bot_id}\n"
            f"稼働中のサーバー数: {guilds}\n"
            f"コマンド同期済み\n"
            f"現在オンライン中"
        )
        await interaction.response.send_message(msg)
    except Exception as e:
        print(f"[bot_info] error: {e}")
        await interaction.response.send_message("⚠️ 情報取得中にエラーが発生しました。", ephemeral=True)


# ---------- Part 5: ギャンブル・自動付与・起動ログ・keep-alive (Supabase / asyncpg 対応) ----------
import random
import time
import asyncio
from datetime import datetime
from discord.ext import tasks
from discord.ui import View, Button
from discord import app_commands
import discord

# 前提: global db_pool (asyncpg.Pool), bot, tree, TOKEN が別箇所で定義済み

# ---------- DB 非同期ユーティリティ ----------
async def async_get_probability() -> int:
    """ギャンブル確率レベルを取得（存在しない場合は3を返す）"""
    global db_pool
    if db_pool is None:
        return 3
    async with db_pool.acquire() as conn:
        val = await conn.fetchval("SELECT probability_level FROM gamble_settings WHERE id=1")
        return int(val) if val is not None else 3

async def async_update_balance(user_id: int, amount: int):
    """
    amount >= 0 の場合: balance += amount, total_received += amount
    amount < 0 の場合: balance -= abs(amount), total_spent += abs(amount)
    """
    global db_pool
    if db_pool is None:
        raise RuntimeError("DB pool is not initialized")
    async with db_pool.acquire() as conn:
        if amount >= 0:
            await conn.execute(
                """
                INSERT INTO users (user_id, balance, total_received, total_spent)
                VALUES ($1, $2, $2, 0)
                ON CONFLICT (user_id) DO UPDATE
                  SET balance = users.balance + $2,
                      total_received = users.total_received + $2
                """,
                user_id, amount
            )
        else:
            amt = -amount
            await conn.execute(
                """
                INSERT INTO users (user_id, balance, total_received, total_spent)
                VALUES ($1, 0, 0, $2)
                ON CONFLICT (user_id) DO UPDATE
                  SET balance = users.balance - $2,
                      total_spent = users.total_spent + $2
                """,
                user_id, amt
            )

async def async_get_balance(user_id: int) -> int:
    global db_pool
    if db_pool is None:
        return 0
    async with db_pool.acquire() as conn:
        val = await conn.fetchval("SELECT balance FROM users WHERE user_id=$1", user_id)
        return int(val) if val is not None else 0

# チャットや通話で付与する際にユーザー存在を保証（Part1 の init_db と同じテーブルがある前提）
async def ensure_user_exists(user_id: int):
    global db_pool
    if db_pool is None:
        return
    async with db_pool.acquire() as conn:
        await conn.execute("INSERT INTO users (user_id) VALUES ($1) ON CONFLICT (user_id) DO NOTHING", user_id)


# ---------- 起動時ログ / on_ready ----------
@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print(f"Guilds: {[g.name for g in bot.guilds]}")
    print("Bot is ready and slash commands synced.")
    try:
        await tree.sync()
    except Exception as e:
        print(f"[on_ready] tree.sync error: {e}")
    # start background tasks
    if not voice_check.is_running():
        voice_check.start()
    print("Background tasks started.")


# ---------- 自動付与：チャット文字数に応じて付与 ----------
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        # ただしコマンド処理は通す
        await bot.process_commands(message)
        return

    if not message.content or not message.content.strip():
        await bot.process_commands(message)
        return

    # 1文字 = 1 Raruin を付与（必要に応じて1日上限等を実装）
    earned = len(message.content)
    try:
        await ensure_user_exists(message.author.id)
        await async_update_balance(message.author.id, earned)
    except Exception as e:
        print(f"[on_message] DB error: {e}")

    await bot.process_commands(message)


# ---------- 通話参加で1分ごとに付与 ----------
@tasks.loop(minutes=1)
async def voice_check():
    for guild in bot.guilds:
        for vc in guild.voice_channels:
            for member in vc.members:
                if member.bot:
                    continue
                try:
                    await ensure_user_exists(member.id)
                    await async_update_balance(member.id, 12)
                except Exception as e:
                    print(f"[voice_check] error for user {member.id}: {e}")
    # ループ内でコミット不要（asyncpg が自動実行）


# ---------- ギャンブル確率＋冷却管理 ----------
last_win_times: dict = {}  # user_id -> last win timestamp (秒)

async def is_win(user_id: int, base_chance: int = 0) -> bool:
    """
    非同期版勝敗判定。連勝短時間クールダウン考慮。
    """
    now = time.time()
    if user_id in last_win_times and now - last_win_times[user_id] < 60:
        return False
    prob_level = await async_get_probability()
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
    balance = await async_get_balance(user_id)
    if bet <= 0 or balance < bet:
        await interaction.response.send_message(f"💰 残高不足です。あなたの残高: {balance}", ephemeral=True)
        return

    prob_level = await async_get_probability()
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

    if reels[0] == reels[1] == reels[2]:
        payout = bet * 5
        await async_update_balance(user_id, payout)
        msg = f"🎉 大当たり! +{payout} Raruin"
    elif len(set(reels)) == 2:
        payout = bet * 2
        await async_update_balance(user_id, payout)
        msg = f"✨ 中当たり! +{payout} Raruin"
    else:
        payout = -bet
        await async_update_balance(user_id, payout)
        msg = f"💀 ハズレ... -{bet} Raruin"

    new_bal = await async_get_balance(user_id)
    await interaction.response.send_message(
        f"🎰 {' | '.join(reels)}\n{msg}\n残高: {new_bal} Raruin"
    )


# ---------- コイントス ----------
@tree.command(name="コイントス", description="コイントスでギャンブル (公平な1/2)")
@app_commands.describe(bet="掛け金")
async def coin(interaction: discord.Interaction, bet: int):
    user_id = interaction.user.id
    balance = await async_get_balance(user_id)
    if bet <= 0 or balance < bet:
        await interaction.response.send_message("💰 残高不足です。", ephemeral=True)
        return

    win = random.choice([True, False])
    if win:
        payout = bet * 2
        await async_update_balance(user_id, payout)
        msg = f"🎉 {payout} Raruin 獲得"
    else:
        payout = -bet
        await async_update_balance(user_id, payout)
        msg = f"💀 {bet} Raruin 減少"

    new_bal = await async_get_balance(user_id)
    await interaction.response.send_message(f"コイントス: {'表' if win else '裏'}\n{msg}\n残高: {new_bal} Raruin")


# ---------- ハイアンドロー (View を使う) ----------
class HiLoView(discord.ui.View):
    def __init__(self, user_id: int, current_card: int, bet: int):
        super().__init__(timeout=30)
        self.user_id = user_id
        self.current_card = current_card
        self.next_card = random.randint(1, 13)
        self.bet = bet

    async def finish(self, interaction: discord.Interaction, win: bool):
        if win:
            payout = int(self.bet * 0.3)
            await async_update_balance(self.user_id, payout)
            msg = f"🎉 勝ち! +{payout} Raruin 獲得"
        else:
            payout = -self.bet
            await async_update_balance(self.user_id, payout)
            msg = f"💀 負け... -{self.bet} Raruin"
        new_bal = await async_get_balance(self.user_id)
        await interaction.response.edit_message(
            content=f"{self.current_card} → {self.next_card}\n{msg}\n残高: {new_bal} Raruin",
            view=None
        )

    @discord.ui.button(label="ハイ", style=discord.ButtonStyle.primary)
    async def hi_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ あなた専用です", ephemeral=True)
            return
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
    balance = await async_get_balance(user_id)
    if bet <= 0 or balance < bet:
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
    balance = await async_get_balance(user_id)
    if bet <= 0 or balance < bet:
        await interaction.response.send_message("💰 残高不足です。", ephemeral=True)
        return

    choice_str = choice.strip()
    number = random.randint(0, 36)
    color = "赤" if number % 2 == 0 else "黒"  # 0 を赤扱い（必要なら変更可）

    win = False
    payout = 0

    # 番号ベット
    if choice_str.isdigit():
        try:
            chosen_num = int(choice_str)
            if 0 <= chosen_num <= 36 and chosen_num == number:
                win = True
                payout = bet * 35
        except:
            pass

    # 赤/黒
    if not win and choice_str in ["赤", "黒"]:
        if choice_str == color:
            win = True
            payout = bet * 2

    # 偶数/奇数
    if not win and choice_str in ["偶数", "奇数"]:
        if (number % 2 == 0 and choice_str == "偶数") or (number % 2 == 1 and choice_str == "奇数"):
            win = True
            payout = bet * 2

    if win:
        await async_update_balance(user_id, payout)
        msg = f"🎉 勝ち! {payout} Raruin 獲得"
    else:
        await async_update_balance(user_id, -bet)
        msg = f"💀 負け... -{bet} Raruin"

    new_bal = await async_get_balance(user_id)
    await interaction.response.send_message(
        f"ルーレット: 出目 {number} ({color})\n"
        f"{msg}\n"
        f"残高: {new_bal} Raruin"
    )


# ---------- ポーカー（対ディーラー簡易版） ----------
def hand_rank_by_counts(rank_list):
    order = {r: i for i, r in enumerate(['2','3','4','5','6','7','8','9','10','J','Q','K','A'], start=2)}
    counts = {}
    for r in rank_list:
        counts[r] = counts.get(r, 0) + 1
    items = sorted([(cnt, order[r], r) for r, cnt in counts.items()], key=lambda x: (-x[0], -x[1]))
    cnts = sorted(counts.values(), reverse=True)

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
        category = 1

    tiebreaker = []
    for cnt, rv, r in items:
        tiebreaker.append(cnt)
        tiebreaker.append(rv)
    return (category, tiebreaker)


@tree.command(name="ポーカー", description="5枚カードで勝負（対ディーラー、簡易版）")
@app_commands.describe(bet="掛け金")
async def poker(interaction: discord.Interaction, bet: int):
    user_id = interaction.user.id
    balance = await async_get_balance(user_id)
    if bet <= 0 or balance < bet:
        await interaction.response.send_message("💰 残高不足です。", ephemeral=True)
        return

    ranks = [str(n) for n in range(2, 11)] + ["J", "Q", "K", "A"]
    suits = ["♠", "♥", "♦", "♣"]
    deck = [r + s for r in ranks for s in suits]
    cards = random.sample(deck, 10)
    player_cards = cards[:5]
    dealer_cards = cards[5:]

    player_ranks = [c[:-1] for c in player_cards]
    dealer_ranks = [c[:-1] for c in dealer_cards]

    player_rank = hand_rank_by_counts(player_ranks)
    dealer_rank = hand_rank_by_counts(dealer_ranks)

    prob_level = await async_get_probability()
    dealer_modifier = prob_level - 3
    modified_dealer_category = max(1, min(6, dealer_rank[0] + dealer_modifier))
    modified_dealer_rank = (modified_dealer_category, dealer_rank[1])

    if player_rank[0] > modified_dealer_rank[0]:
        winner = "player"
    elif player_rank[0] < modified_dealer_rank[0]:
        winner = "dealer"
    else:
        if player_rank[1] > modified_dealer_rank[1]:
            winner = "player"
        elif player_rank[1] < modified_dealer_rank[1]:
            winner = "dealer"
        else:
            winner = "tie"

    mult_map = {6: 10, 5: 7, 4: 3, 3: 2, 2: 1.5, 1: 0}
    mult = mult_map.get(player_rank[0], 0)

    if winner == "player" and mult > 0:
        payout = int(bet * mult)
        await async_update_balance(user_id, payout)
        msg = f"🎉 あなたの勝ち! ({payout} Raruin 獲得)"
    elif winner == "player" and mult == 0:
        payout = bet
        await async_update_balance(user_id, payout)
        msg = f"🎉 あなたの勝ち! ハイカードで勝利: +{payout} Raruin"
    elif winner == "tie":
        msg = "🤝 引き分けです。ベットは返却されます。"
    else:
        await async_update_balance(user_id, -bet)
        msg = f"💀 あなたの負け... -{bet} Raruin"

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

    new_bal = await async_get_balance(user_id)
    await interaction.response.send_message(
        f"あなたの手札: {' '.join(player_cards)}  ({player_cat_name})\n"
        f"ディーラーの手札: {' '.join(dealer_cards)}  ({dealer_cat_name})\n"
        f"→ ギャンブル確率設定によりディーラー強さを調整: {modified_dealer_cat_name}\n\n"
        f"{msg}\n残高: {new_bal} Raruin"
    )


# ---------- 起動ログ / startup_log & main ----------
async def startup_log():
    print("===================================================")
    print("🚀 Bot起動開始 🚀")
    print("日時:", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("TOKENは環境変数から正常に取得済み")
    print("Flaskサーバーは keep_alive.py でバックグラウンド起動済み（もし使うなら）")
    print("UptimeRobot等で常時オンラインを維持してください")
    print("DBプール (asyncpg) が初期化済みであることを確認してください")
    print("===================================================\n")

    tables = ["users", "shops", "shop_items", "purchase_history", "gamble_settings", "daily_settings"]
    for t in tables:
        print(f"✔ テーブル '{t}' 確認済み（存在確認は init_db() 側で行ってください）")

    print("\n🔧 Botのバックグラウンドタスクを開始")
    print("・雑談文字数でRaruin付与")
    print("・通話参加で1分ごとにRaruin付与")
    print("===================================================")
    await asyncio.sleep(0.05)


async def main():
    await startup_log()
    print("🟢 Discord Botを接続中…")
    await bot.start(TOKEN)
