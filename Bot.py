import os
import discord
from discord import app_commands
from discord.ext import commands, tasks
import sqlite3
import json
from dotenv import load_dotenv
from datetime import datetime, timedelta

# ---- 環境設定 ----
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_ID", "").split(",")]
BACKUP_CHANNEL_ID = int(os.getenv("BACKUP_CHANNEL_ID"))
CURRENCY_NAME = "Raruin"   # 通貨名

DB_PATH = "currency.db"

# ---- DB初期化 ----
con = sqlite3.connect(DB_PATH)
cur = con.cursor()
# ユーザー: 残高、獲得額、消費額
cur.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    balance INTEGER DEFAULT 1000,
    earned INTEGER DEFAULT 0,
    spent INTEGER DEFAULT 0
);
""")
# ショップ
cur.execute("""
CREATE TABLE IF NOT EXISTS shops (
    shop_name TEXT PRIMARY KEY
);
""")
# 商品: 商品名,ショップ名,説明,金額,在庫(0=無限),許可ロール
cur.execute("""
CREATE TABLE IF NOT EXISTS products (
    product_name TEXT,
    shop_name TEXT,
    description TEXT,
    price INTEGER,
    stock INTEGER,       -- 0で無限
    buy_role INTEGER,    -- 0で全員
    PRIMARY KEY(product_name, shop_name)
);
""")
con.commit()

# ---- Botインスタンス ----
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="/", intents=intents)
tree = bot.tree

# ---- 管理者判定 ----
def is_admin(user: discord.User):
    return user.id in ADMIN_IDS

# ---- DB操作ヘルパ ----
def get_user_balance(user_id):
    cur.execute("SELECT balance, earned, spent FROM users WHERE user_id=?", (user_id,))
    data = cur.fetchone()
    if data:
        return data
    else:
        return (1000, 0, 0)

def add_user_ifnot(user_id):
    b, e, s = get_user_balance(user_id)
    cur.execute("INSERT OR IGNORE INTO users (user_id, balance, earned, spent) VALUES (?, ?, ?, ?)", (user_id, b, e, s))
    con.commit()

def change_balance(user_id, amount, is_add=True):
    add_user_ifnot(user_id)
    if is_add:
        cur.execute("UPDATE users SET balance = balance + ?, earned = earned + ? WHERE user_id=?", (amount, amount, user_id))
    else:
        cur.execute("UPDATE users SET balance = balance - ?, spent = spent + ? WHERE user_id=?", (amount, amount, user_id))
    con.commit()

def shop_exists(shop_name):
    cur.execute("SELECT shop_name FROM shops WHERE shop_name=?", (shop_name,))
    return cur.fetchone() is not None

def product_exists(product_name, shop_name):
    cur.execute("SELECT product_name FROM products WHERE product_name=? AND shop_name=?", (product_name, shop_name))
    return cur.fetchone() is not None

def get_shop_list():
    cur.execute("SELECT shop_name FROM shops")
    return [row[0] for row in cur.fetchall()]

def get_product_list(shop_name=None, role_id=None):
    q = "SELECT product_name, description, price, stock, buy_role FROM products"
    args = []
    conds = []
    if shop_name:
        conds.append("shop_name=?")
        args.append(shop_name)
    if role_id is not None:
        conds.append("(buy_role=0 OR buy_role=?)")
        args.append(role_id)
    if conds:
        q += " WHERE " + " AND ".join(conds)
    return cur.execute(q, tuple(args)).fetchall()

# ---- ��マンド: /付与 ----
@tree.command(name="付与", description=f"ユーザー/ロールに {CURRENCY_NAME} を付与します（管理者のみ）")
@app_commands.describe(target="付与するユーザーまたはロール", amount=f"付与する{CURRENCY_NAME}額")
@app_commands.autocomplete(target=lambda i, c: [
    app_commands.Choice(name=m.display_name, value=str(m.id))
    for m in i.guild.members
] + [
    app_commands.Choice(name=r.name, value=f"role:{r.id}")
    for r in i.guild.roles if not r.is_default()
])
async def add_raurin(interaction: discord.Interaction, target: str, amount: int):
    if not is_admin(interaction.user):
        await interaction.response.send_message("権限がありません", ephemeral=True)
        return
    if amount <= 0:
        await interaction.response.send_message("付与額は1以上にしてください", ephemeral=True)
        return
    # ユーザー or ロール判定
    if target.startswith("role:"):
        role_id = int(target.split(":",1)[1])
        role = discord.utils.get(interaction.guild.roles, id=role_id)
        targets = [m for m in interaction.guild.members if role in m.roles]
    else:
        uid = int(target)
        member = interaction.guild.get_member(uid)
        if not member:
            await interaction.response.send_message("ユーザーが見つかりません", ephemeral=True)
            return
        targets = [member]
    for mem in targets:
        change_balance(mem.id, amount, is_add=True)
        # DM通知
        try:
            await mem.send(f"あなたに{amount} {CURRENCY_NAME}が付与されました。（管理者操作）")
        except Exception:
            pass
    await interaction.response.send_message(
        f"{', '.join(m.display_name for m in targets)}に{amount} {CURRENCY_NAME}を付与しました",
        ephemeral=True
    )

# ---- コマンド: /減額 ----
@tree.command(name="減額", description=f"ユーザー/ロールから{CURRENCY_NAME}を減額します（管理者のみ）")
@app_commands.describe(target="減額するユーザーまたはロール", amount=f"減額する{CURRENCY_NAME}額")
@app_commands.autocomplete(target=lambda i, c: [
    app_commands.Choice(name=m.display_name, value=str(m.id))
    for m in i.guild.members
] + [
    app_commands.Choice(name=r.name, value=f"role:{r.id}")
    for r in i.guild.roles if not r.is_default()
])
async def remove_raurin(interaction: discord.Interaction, target: str, amount: int):
    if not is_admin(interaction.user):
        await interaction.response.send_message("権限がありません", ephemeral=True)
        return
    if amount <= 0:
        await interaction.response.send_message("減額は1以上を指定してください", ephemeral=True)
        return
    # ユーザー or ロール判定
    if target.startswith("role:"):
        role_id = int(target.split(":",1)[1])
        role = discord.utils.get(interaction.guild.roles, id=role_id)
        targets = [m for m in interaction.guild.members if role in m.roles]
    else:
        uid = int(target)
        member = interaction.guild.get_member(uid)
        if not member:
            await interaction.response.send_message("ユーザーが見つかりません", ephemeral=True)
            return
        targets = [member]
    for mem in targets:
        b,_,_ = get_user_balance(mem.id)
        if b < amount:
            continue
        change_balance(mem.id, amount, is_add=False)
        # DM通知
        try:
            await mem.send(f"{amount}{CURRENCY_NAME}が管��者操作で減額されました。")
        except Exception:
            pass
    await interaction.response.send_message(
        f"{', '.join(m.display_name for m in targets)}から{amount}{CURRENCY_NAME}減額しました（残高不足はスキップ）",
        ephemeral=True
    )

# ---- コマンド: /Shop ----
@tree.command(name="Shop", description="ショップ追加/削除（管理者のみ）")
@app_commands.describe(action="追加 or 削除", shop_name="ショップ名")
@app_commands.choices(action=[
    app_commands.Choice(name="追加", value="add"),
    app_commands.Choice(name="削除", value="remove")
])
async def shop_command(interaction: discord.Interaction, action: str, shop_name: str):
    if not is_admin(interaction.user):
        await interaction.response.send_message("権限がありません", ephemeral=True)
        return
    if action == "add":
        cur.execute("INSERT OR IGNORE INTO shops (shop_name) VALUES (?)", (shop_name,))
        con.commit()
        await interaction.response.send_message(f"ショップ「{shop_name}」を追加しました", ephemeral=True)
    elif action == "remove":
        cur.execute("DELETE FROM shops WHERE shop_name=?", (shop_name,))
        cur.execute("DELETE FROM products WHERE shop_name=?", (shop_name,))
        con.commit()
        await interaction.response.send_message(f"ショップ「{shop_name}」と関連商品を削除しました", ephemeral=True)

# ---- コマンド: /Shop商品 ----
@tree.command(name="Shop商品", description="ショップの商品追加/削除（管理者のみ）")
@app_commands.describe(
    action="追加/削除",
    product_name="商品の名前",
    shop_name="ショップ名",
    description="商品の説明(追加時のみ)",
    price="金額(追加時のみ)",
    stock="在庫数(0で無限、追加時のみ)",
    buy_role="誰が買える(0で全員、追加時のみ/ロールID)"
)
@app_commands.choices(action=[
    app_commands.Choice(name="追加", value="add"),
    app_commands.Choice(name="削除", value="remove")
])
async def shopitem_command(
    interaction: discord.Interaction,
    action: str,
    product_name: str,
    shop_name: str,
    description: str = "",
    price: int = 0,
    stock: int = 0,
    buy_role: int = 0
):
    if not is_admin(interaction.user):
        await interaction.response.send_message("権限がありません", ephemeral=True)
        return
    if not shop_exists(shop_name):
        await interaction.response.send_message("ショップが存在しません", ephemeral=True)
        return
    if action == "add":
        if price <= 0:
            await interaction.response.send_message("価格は1以上で指定してください", ephemeral=True)
            return
        cur.execute("""
            INSERT OR REPLACE INTO products
            (product_name, shop_name, description, price, stock, buy_role)
            VALUES (?, ?, ?, ?, ?, ?)""",
            (product_name, shop_name, description, price, stock, buy_role)
        )
        con.commit()
        await interaction.response.send_message(f"{shop_name}に商品「{product_name}」を追加しました", ephemeral=True)
    elif action == "remove":
        cur.execute("DELETE FROM products WHERE product_name=? AND shop_name=?", (product_name, shop_name))
        con.commit()
        await interaction.response.send_message(f"{shop_name}の商品「{product_name}」を削除しました", ephemeral=True)

# ---- コマンド: /残高復元（管理者のみ） ----
@tree.command(name="残高復元", description="バックアップチャンネルからデータ復元（最新のみ）")
async def restore_balance(interaction: discord.Interaction):
    if not is_admin(interaction.user):
        await interaction.response.send_message("権限がありません", ephemeral=True)
        return
    channel = bot.get_channel(BACKUP_CHANNEL_ID)
    if not channel:
        await interaction.response.send_message("バックアップチャンネルが見つかりません", ephemeral=True)
        return
    # 最新のバックアップメッセージ取得
    async for msg in channel.history(limit=10):
        if msg.content.startswith("【Raruin Backup】"):
            try:
                start = msg.content.index("```json") + 7
                end = msg.content.index("```", start)
                dump = json.loads(msg.content[start:end])
            except Exception:
                continue
            # 復元処理
            cur.execute("DELETE FROM users")
            for row in dump["users"]:
                cur.execute("INSERT INTO users (user_id, balance, earned, spent) VALUES (?, ?, ?, ?)", tuple(row))
            cur.execute("DELETE FROM shops")
            for row in dump["shops"]:
                cur.execute("INSERT INTO shops (shop_name) VALUES (?)", tuple(row))
            cur.execute("DELETE FROM products")
            for row in dump["products"]:
                cur.execute("""INSERT INTO products 
                    (product_name, shop_name, description, price, stock, buy_role)
                    VALUES (?, ?, ?, ?, ?, ?)""", tuple(row))
            con.commit()
            await interaction.response.send_message("復元完了！", ephemeral=True)
            return
    await interaction.response.send_message("復元用のバックアップが見つかりません", ephemeral=True)

# ---- コマンド: /残高 ----
@tree.command(name="残高", description=f"あなたの{CURRENCY_NAME}残高・獲得/消費を確認")
async def balance_cmd(interaction: discord.Interaction):
    b, e, s = get_user_balance(interaction.user.id)
    await interaction.response.send_message(
        f"あなたの残高:\n**{b} {CURRENCY_NAME}**\n獲得:{e} 消費:{s}",
        ephemeral=True
    )

# ---- コマンド: /ランキング ----
@tree.command(name="ランキング", description=f"{CURRENCY_NAME}ラン���ングTop10")
async def ranking_cmd(interaction: discord.Interaction):
    cur.execute("SELECT user_id, balance, earned, spent FROM users ORDER BY balance DESC LIMIT 10")
    rows = cur.fetchall()
    embed = discord.Embed(title=f"{CURRENCY_NAME}ランキング")
    for idx, (uid, b, e, s) in enumerate(rows):
        member = interaction.guild.get_member(uid)
        name = member.display_name if member else str(uid)
        embed.add_field(
            name=f"{idx+1}位 {name}",
            value=f"残高: {b} / 獲得: {e} / 消費: {s}",
            inline=False
        )
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ---- コマンド: /渡す ----
@tree.command(name="渡す", description=f"特定ユーザーに{CURRENCY_NAME}を渡す")
@app_commands.describe(target="渡す相手", amount=f"渡す{CURRENCY_NAME}額")
@app_commands.autocomplete(target=lambda i, c: [
    app_commands.Choice(name=m.display_name, value=str(m.id))
    for m in i.guild.members if m.id != i.user.id
])
async def transfer_cmd(interaction: discord.Interaction, target: str, amount: int):
    target_id = int(target)
    if target_id == interaction.user.id:
        await interaction.response.send_message("自分へは送れません", ephemeral=True)
        return
    if amount <= 0:
        await interaction.response.send_message("1以上の金額を指定してください", ephemeral=True)
        return
    b,_,_ = get_user_balance(interaction.user.id)
    if b < amount:
        await interaction.response.send_message("残高不足です", ephemeral=True)
        return
    change_balance(interaction.user.id, amount, is_add=False)
    change_balance(target_id, amount, is_add=True)
    user2 = interaction.guild.get_member(target_id)
    now = datetime.now()
    try:
        await user2.send(
            f"{interaction.user.display_name} さんから {amount}{CURRENCY_NAME} を受け取りました\n"
            f"日時:{now.month}月{now.day}日{now.hour}時{now.minute}分"
        )
    except Exception:
        pass
    await interaction.response.send_message(
        f"{user2.display_name}に{amount}{CURRENCY_NAME}を渡しました", ephemeral=True
    )

# ---- コマンド: /ショップ一覧（ページング） ----
@tree.command(name="ショップ一覧", description="現在存在するショップの一覧（10件ずつ表示）")
@app_commands.describe(page="ページ番号(デフォルト1)")
async def shop_list_cmd(interaction: discord.Interaction, page: int = 1):
    shops = get_shop_list()
    max_page = max(1, (len(shops)-1)//10+1)
    page = max(1,min(page,max_page))
    embed = discord.Embed(title="ショップ一覧", description=f"ページ:{page}/{max_page}")
    start = (page-1)*10
    for s in shops[start:start+10]:
        embed.add_field(name=s, value=f"ショップ名:{s}", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ---- コマンド: /ショップ ----
@tree.command(name="ショップ", description="指定ショップの商品一覧（10件ずつ表示）")
@app_commands.describe(shop_name="ショップ名", page="ページ番号(デフォルト1)")
@app_commands.autocomplete(shop_name=lambda i, c: [
    app_commands.Choice(name=s, value=s)
    for s in get_shop_list()
])
async def shop_detail_cmd(interaction: discord.Interaction, shop_name: str, page: int = 1):
    if not shop_exists(shop_name):
        await interaction.response.send_message("ショップが存在しません", ephemeral=True)
        return
    user_roles = [r.id for r in interaction.user.roles]
    # 購入可能商品
    items = []
    for (pn, desc, price, stock, buy_role) in get_product_list(shop_name=shop_name):
        if buy_role == 0 or buy_role in user_roles:
            items.append((pn, desc, price, stock, buy_role))
    max_page = max(1, (len(items)-1)//10+1)
    page = max(1,min(page,max_page))
    embed = discord.Embed(title=f"{shop_name}の商品一覧", description=f"ページ:{page}/{max_page}")
    start = (page-1)*10
    for (pn, desc, price, stock, buy_role) in items[start:start+10]:
        stock_str = "無限" if stock==0 else f"{stock}個"
        role_name = "全員" if buy_role==0 else (
            discord.utils.get(interaction.guild.roles, id=buy_role).name if discord.utils.get(interaction.guild.roles, id=buy_role) else f"ID:{buy_role}"
        )
        embed.add_field(
            name=pn,
            value=f"{desc}\n価格:{price} {CURRENCY_NAME}\n在庫:{stock_str}\n購入可能:{role_name}",
            inline=False
        )
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ---- コマンド: /買う ----
@tree.command(name="買う", description="指定ショップの商品を購入します")
@app_commands.describe(shop_name="ショップ名", product_name="商品名")
@app_commands.autocomplete(shop_name=lambda i, c: [app_commands.Choice(name=s, value=s) for s in get_shop_list()])
@app_commands.autocomplete(product_name=lambda i, c: [
     app_commands.Choice(name=pn, value=pn)
     for (pn,desc,price,stock,br) in get_product_list(shop_name=i.namespace.get("shop_name", None))
])
async def buy_cmd(interaction: discord.Interaction, shop_name: str, product_name: str):
    if not shop_exists(shop_name):
        await interaction.response.send_message("ショップが存在しません", ephemeral=True)
        return
    found = cur.execute(
        "SELECT description, price, stock, buy_role FROM products WHERE product_name=? AND shop_name=?",
        (product_name, shop_name)
    ).fetchone()
    if not found:
        await interaction.response.send_message("商品が見つかりません", ephemeral=True)
        return
    desc, price, stock, buy_role = found
    user_roles = [r.id for r in interaction.user.roles]
    if buy_role != 0 and buy_role not in user_roles:
        await interaction.response.send_message("この商品は指定ロールのみ買えます", ephemeral=True)
        return
    b,_,_ = get_user_balance(interaction.user.id)
    if b < price:
        await interaction.response.send_message("残高不足です", ephemeral=True)
        return
    if stock != 0 and stock < 1:
        await interaction.response.send_message("在庫切れです", ephemeral=True)
        return
    # 購入処理
    change_balance(interaction.user.id, price, is_add=False)
    if stock != 0:
        cur.execute("UPDATE products SET stock = stock - 1 WHERE product_name=? AND shop_name=?", (product_name, shop_name))
        con.commit()
    await interaction.response.send_message(
        f"{product_name}を{price}{CURRENCY_NAME}で購入しました！\n{desc}",
        ephemeral=True
    )

# ---- チャットごとに1Raruin付与 ----
@bot.event
async def on_message(message):
    if not message.guild or message.author.bot: return
    change_balance(message.author.id, 1, is_add=True)
    await bot.process_commands(message)

# ---- 通話1分ごとに30Raruin ----
voice_times = {} # {user_id: join_time}
@bot.event
async def on_voice_state_update(member, before, after):
    # 通話参加
    if after.channel and not before.channel:
        voice_times[member.id] = datetime.now()
    # 通話退出
    elif before.channel and not after.channel:
        join_time = voice_times.pop(member.id, None)
        if join_time:
            minutes = max(1, int((datetime.now()-join_time).total_seconds() // 60))
            reward = minutes * 30
            change_balance(member.id, reward, is_add=True)
            try:
                await member.send(f"通話報酬: {minutes}分で{reward} {CURRENCY_NAME}を獲得しました！")
            except Exception: pass

# ---- 毎日0時に自動バックアップ ----
@tasks.loop(hours=24)
async def daily_backup():
    await bot.wait_until_ready()
    channel = bot.get_channel(BACKUP_CHANNEL_ID)
    # DBの内容取得
    cur.execute("SELECT * FROM users")
    users = cur.fetchall()
    cur.execute("SELECT * FROM shops")
    shops = cur.fetchall()
    cur.execute("SELECT * FROM products")
    products = cur.fetchall()
    dump = {
        "users": users,
        "shops": shops,
        "products": products,
        "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }
    await channel.send(f"【Raruin Backup】\n```json\n{json.dumps(dump, ensure_ascii=False, indent=2)}\n```")

@bot.event
async def on_ready():
    print(f"Bot activated: {bot.user} ({bot.user.id})")
    try:
        synced = await tree.sync()
        print(f"Slashコマンドを {len(synced)}個同期しました")
    except Exception as e:
        print(f"コマンド同期エラー: {e}")
    # バックアップ起動
    now = datetime.now()
    run_delay = ((24-now.hour)%24)*3600 - now.minute*60 - now.second
    daily_backup.change_interval(seconds=run_delay if run_delay>0 else 60)
    daily_backup.start()

# ---- main起動 ----
bot.run(TOKEN)
