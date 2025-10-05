import discord
from discord.ext import commands
from discord import app_commands
import sqlite3
import os
from datetime import datetime, timezone
import random
import time
from flask import Flask
from threading import Thread

# --- Replit用：.envからトークン取得 ---
from dotenv import load_dotenv
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

if not TOKEN:
    print("エラー: DISCORD_TOKENが設定されていません。Secretsツールで設定してください。")
    exit(1)

# --- Flask HTTPサーバー（Uptime Robot用） ---
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is alive!", 200

@app.route('/health')
def health():
    return "OK", 200

def run_flask():
    # 開発環境ではFlask開発サーバーを使用
    try:
        app.run(host='0.0.0.0', port=5000, use_reloader=False)
    except Exception as e:
        print(f"Flaskサーバーエラー: {e}")

def keep_alive():
    server = Thread(target=run_flask)
    server.daemon = True
    server.start()

DB_PATH = "raruin.db"
ADMIN_ROLE_NAME = "RaruinAdmin"

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# --- DB 初期化 ---
conn = sqlite3.connect(DB_PATH)
c = conn.cursor()
c.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    balance INTEGER DEFAULT 0,
    voice_join INTEGER DEFAULT NULL
)
""")
conn.commit()

# --- 通貨管理関数 ---
def get_balance(user_id: int) -> int:
    c.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    if row:
        return row[0]
    c.execute("INSERT INTO users(user_id, balance) VALUES(?,0)", (user_id,))
    conn.commit()
    return 0

def add_balance(user_id: int, amount: int):
    bal = get_balance(user_id)
    bal += amount
    c.execute("UPDATE users SET balance = ? WHERE user_id = ?", (bal, user_id))
    conn.commit()
    return bal

def ensure_user(user_id: int):
    c.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,))
    if not c.fetchone():
        c.execute("INSERT INTO users(user_id,balance) VALUES(?,0)", (user_id,))
        conn.commit()

def is_admin_member(member: discord.Member):
    if member.guild_permissions.administrator:
        return True
    role = discord.utils.get(member.roles, name=ADMIN_ROLE_NAME)
    return role is not None

# --- メッセージ報酬（1文字1Raruin） ---
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    ensure_user(message.author.id)
    amount = len(message.content)
    if amount > 0:
        add_balance(message.author.id, amount)
    await bot.process_commands(message)

# --- ボイス報酬（1分5Raruin） ---
@bot.event
async def on_voice_state_update(member, before, after):
    ensure_user(member.id)
    now_ts = int(datetime.now(timezone.utc).timestamp())
    if before.channel is None and after.channel is not None:
        c.execute("UPDATE users SET voice_join = ? WHERE user_id = ?", (now_ts, member.id))
        conn.commit()
    elif before.channel is not None and after.channel is None:
        c.execute("SELECT voice_join FROM users WHERE user_id = ?", (member.id,))
        row = c.fetchone()
        if row and row[0]:
            seconds = now_ts - row[0]
            minutes = seconds // 60
            reward = minutes * 5
            if reward > 0:
                add_balance(member.id, reward)
        c.execute("UPDATE users SET voice_join = NULL WHERE user_id = ?", (member.id,))
        conn.commit()

# --- スラッシュコマンド：残高確認 ---
@tree.command(name="残高", description="自分のRaruin残高を表示します")
async def bal(interaction: discord.Interaction):
    ensure_user(interaction.user.id)
    bal = get_balance(interaction.user.id)
    await interaction.response.send_message(f"あなたの残高: **{bal} Raruin**")

# --- スラッシュコマンド：ランキング ---
@tree.command(name="ランキング", description="Raruin残高のランキングを表示します")
@app_commands.describe(表示数="表示する順位の数（デフォルト: 10）")
async def ranking(interaction: discord.Interaction, 表示数: int = 10):
    if 表示数 < 1:
        await interaction.response.send_message("表示数は1以上で指定してください。")
        return
    if 表示数 > 50:
        表示数 = 50  # 最大50位まで

    c.execute("SELECT user_id, balance FROM users ORDER BY balance DESC LIMIT ?", (表示数,))
    rows = c.fetchall()

    if not rows:
        await interaction.response.send_message("まだランキングデータがありません。")
        return

    msg_lines = ["**💰 Raruinランキング 💰**\n"]
    for idx, (user_id, balance) in enumerate(rows, 1):
        member = interaction.guild.get_member(user_id)
        if member:
            name = member.display_name
        else:
            name = f"ユーザーID: {user_id}"

        # 上位3位にメダルを付ける
        if idx == 1:
            medal = "🥇"
        elif idx == 2:
            medal = "🥈"
        elif idx == 3:
            medal = "🥉"
        else:
            medal = f"**{idx}位**"

        msg_lines.append(f"{medal} {name}: **{balance:,} Raruin**")

    await interaction.response.send_message("\n".join(msg_lines))

# --- スラッシュコマンド：渡す ---
@tree.command(name="渡す", description="他のユーザーにRaruinを渡します")
@app_commands.describe(対象="渡す相手", 金額="渡す量")
async def give(interaction: discord.Interaction, 対象: discord.Member, 金額: int):
    if 金額 <= 0:
        await interaction.response.send_message("金額は正の数で指定してください。")
        return
    ensure_user(interaction.user.id)
    ensure_user(対象.id)
    bal = get_balance(interaction.user.id)
    if bal < 金額:
        await interaction.response.send_message("残高が不足しています。")
        return
    add_balance(interaction.user.id, -金額)
    add_balance(対象.id, 金額)
    await interaction.response.send_message(f"{対象.mention} に **{金額} Raruin** を渡しました。")

# --- スラッシュコマンド：money add（管理者） ---
@tree.command(name="money_add", description="管理者: 任意のユーザーにRaruinを付与します")
@app_commands.describe(対象="付与する相手", 金額="付与する額")
async def money_add(interaction: discord.Interaction, 対象: discord.Member, 金額: int):
    if not is_admin_member(interaction.user):
        await interaction.response.send_message("管理者のみ使用可能です。")
        return
    ensure_user(対象.id)
    add_balance(対象.id, 金額)
    await interaction.response.send_message(f"{対象.mention} に **{金額} Raruin** を付与しました。")

# --- スラッシュコマンド：addr（管理者ロール付与） ---
@tree.command(name="addr", description="管理者: 指定ユーザーに管理者ロールを付与します")
@app_commands.describe(対象="ロールを付与する相手")
async def addr(interaction: discord.Interaction, 対象: discord.Member):
    if not is_admin_member(interaction.user):
        await interaction.response.send_message("管理者のみ使用可能です。")
        return
    guild = interaction.guild
    role = discord.utils.get(guild.roles, name=ADMIN_ROLE_NAME)
    if role is None:
        role = await guild.create_role(name=ADMIN_ROLE_NAME)
    await 対象.add_roles(role)
    await interaction.response.send_message(f"{対象.mention} に **{ADMIN_ROLE_NAME}** ロールを付与しました。")

# --- 起動時処理 ---
@bot.event
async def on_ready():
    await tree.sync()
    print(f"Bot 起動: {bot.user}")

class GambleView(discord.ui.View):
    def __init__(self, user_id, amount):
        super().__init__(timeout=30)
        self.user_id = user_id
        self.amount = amount

    @discord.ui.button(label="スロット", style=discord.ButtonStyle.primary)
    async def slot(self, interaction: discord.Interaction, button: discord.ui.Button):
        await run_slot(interaction, self.user_id, self.amount)

    @discord.ui.button(label="コイントス", style=discord.ButtonStyle.primary)
    async def coin(self, interaction: discord.Interaction, button: discord.ui.Button):
        await run_coin(interaction, self.user_id, self.amount)

    @discord.ui.button(label="ハイアンドロー", style=discord.ButtonStyle.primary)
    async def highlow(self, interaction: discord.Interaction, button: discord.ui.Button):
        await run_highlow(interaction, self.user_id, self.amount)

    @discord.ui.button(label="ルーレット", style=discord.ButtonStyle.primary)
    async def roulette(self, interaction: discord.Interaction, button: discord.ui.Button):
        await run_roulette(interaction, self.user_id, self.amount)

    @discord.ui.button(label="ポーカー", style=discord.ButtonStyle.primary)
    async def poker(self, interaction: discord.Interaction, button: discord.ui.Button):
        await run_poker(interaction, self.user_id, self.amount)

# --- 各ゲーム処理関数 ---
async def run_slot(interaction, user_id, amount):
    symbols = ["🍒","🔔","⭐","🍋","7️⃣"]
    res = [random.choice(symbols) for _ in range(3)]
    if res.count(res[0]) == 3:
        win = int(amount * 4)
        result_text = "大当たり！3つ一致！"
    elif len(set(res)) == 2:
        win = int(amount * 1.5)
        result_text = "2つ一致！"
    else:
        win = -amount
        result_text = "はずれ..."
    add_balance(user_id, win)
    await interaction.response.send_message(f"スロット: {' '.join(res)}\n{result_text}\n変化: **{win} Raruin** 残高: **{get_balance(user_id)} Raruin**")

async def run_coin(interaction, user_id, amount):
    outcome = random.choice(["表", "裏"])
    win = amount if random.choice([True, False]) else -amount
    add_balance(user_id, win)
    result = "勝ち！" if win > 0 else "負け..."
    await interaction.response.send_message(f"コイントス: {outcome} — {result}\n変化: **{win} Raruin** 残高: **{get_balance(user_id)} Raruin**")

async def run_highlow(interaction, user_id, amount):
    current = random.randint(1,13)
    next_card = random.randint(1,13)
    if next_card == current or next_card < current:
        win = -amount
        result = "負け..."
    else:
        win = amount
        result = "勝ち！"
    add_balance(user_id, win)
    await interaction.response.send_message(f"カード: {current} → {next_card} — {result}\n変化: **{win} Raruin** 残高: **{get_balance(user_id)} Raruin**")

async def run_roulette(interaction, user_id, amount):
    outcome = random.randint(0,36)
    if outcome == random.randint(0,36):
        win = amount * 35
        result = "直撃！"
    elif outcome % 2 == 0:
        win = amount
        result = "偶数で勝ち！"
    else:
        win = -amount
        result = "奇数で負け..."
    add_balance(user_id, win)
    await interaction.response.send_message(f"ルーレット: {outcome} — {result}\n変化: **{win} Raruin** 残高: **{get_balance(user_id)} Raruin**")

async def run_poker(interaction, user_id, amount):
    ranks = list(range(2,15))
    suits = ["♠","♥","♦","♣"]
    def deal_hand():
        deck = [(r,s) for r in ranks for s in suits]
        random.shuffle(deck)
        return deck[:5]
    def hand_score(hand):
        rs = sorted([r for r,s in hand])
        ss = [s for r,s in hand]
        counts = {r: rs.count(r) for r in set(rs)}
        is_flush = len(set(ss)) == 1
        is_straight = all(rs[i]+1==rs[i+1] for i in range(len(rs)-1))
        if is_straight and is_flush: return (8, max(rs))
        if 4 in counts.values(): return (7, max(k for k,v in counts.items() if v==4))
        if 3 in counts.values() and 2 in counts.values(): return (6, max(k for k,v in counts.items() if v==3))
        if is_flush: return (5, max(rs))
        if is_straight: return (4, max(rs))
        if 3 in counts.values(): return (3, max(k for k,v in counts.items() if v==3))
        if list(counts.values()).count(2) == 2: return (2, max(k for k,v in counts.items() if v==2))
        if 2 in counts.values(): return (1, max(k for k,v in counts.items() if v==2))
        return (0, max(rs))
    user_hand = deal_hand()
    bot_hand = deal_hand()
    us = hand_score(user_hand)
    bs = hand_score(bot_hand)
    if us > bs:
        win = amount * 2
        result = "あなたの勝ち！"
    elif us == bs:
        win = 0
        result = "引き分け"
    else:
        win = -amount
        result = "あなたの負け..."
    add_balance(user_id, win)
    await interaction.response.send_message(
        f"あなたの手: {user_hand}\n敵の手: {bot_hand}\n{result}\n変化: **{win} Raruin** 残高: **{get_balance(user_id)} Raruin**"
    )

@tree.command(name="ギャンブル", description="ギャンブルを開始します")
@app_commands.describe(掛け金="掛け金 (整数)")
async def gamble(interaction: discord.Interaction, 掛け金: int):
    ensure_user(interaction.user.id)
    bal = get_balance(interaction.user.id)
    if 掛け金 <= 0:
        await interaction.response.send_message("掛け金は正の数で指定してください。")
        return
    if bal < 掛け金:
        await interaction.response.send_message("残高が不足しています。")
        return
    await interaction.response.send_message("ゲームを選んでください：", view=GambleView(interaction.user.id, 掛け金))

# --- DB拡張：ショップテーブル（在庫・期限付き） ---
c.execute("""
CREATE TABLE IF NOT EXISTS shop (
    name TEXT PRIMARY KEY,
    description TEXT,
    price INTEGER,
    role_id INTEGER,
    stock INTEGER DEFAULT -1,
    expires_at INTEGER DEFAULT NULL
)
""")

# --- DB拡張：デイリーボーナステーブル ---
c.execute("""
CREATE TABLE IF NOT EXISTS daily_claims (
    user_id INTEGER PRIMARY KEY,
    last_claim INTEGER
)
""")
conn.commit()

# --- デイリーボーナス関数 ---
def can_claim_daily(user_id: int) -> tuple[bool, int]:
    """デイリーボーナスを受け取れるかチェック。戻り値: (受け取れるか, 次回まで秒数)"""
    c.execute("SELECT last_claim FROM daily_claims WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    now = int(time.time())
    if not row:
        return True, 0
    last = row[0]
    elapsed = now - last
    if elapsed >= 86400:  # 24時間 = 86400秒
        return True, 0
    else:
        return False, 86400 - elapsed

def claim_daily(user_id: int, amount: int):
    """デイリーボーナスを付与"""
    now = int(time.time())
    c.execute("INSERT OR REPLACE INTO daily_claims(user_id, last_claim) VALUES(?,?)", (user_id, now))
    conn.commit()
    add_balance(user_id, amount)

# --- スラッシュコマンド：デイリーボーナス ---
@tree.command(name="デイリー", description="24時間ごとにRaruinを受け取れます")
async def daily(interaction: discord.Interaction):
    ensure_user(interaction.user.id)
    can_claim, wait_time = can_claim_daily(interaction.user.id)

    if can_claim:
        bonus_amount = 100  # デイリーボーナス額
        claim_daily(interaction.user.id, bonus_amount)
        new_balance = get_balance(interaction.user.id)
        await interaction.response.send_message(
            f"🎁 デイリーボーナス **{bonus_amount} Raruin** を受け取りました！\n"
            f"現在の残高: **{new_balance} Raruin**"
        )
    else:
        hours = wait_time // 3600
        minutes = (wait_time % 3600) // 60
        await interaction.response.send_message(
            f"⏰ 次のデイリーボーナスまで: **{hours}時間 {minutes}分**"
        )

# --- スラッシュコマンド：統計表示 ---
@tree.command(name="統計", description="サーバー全体のRaruin統計を表示します")
async def stats(interaction: discord.Interaction):
    c.execute("SELECT COUNT(*), SUM(balance), AVG(balance), MAX(balance), MIN(balance) FROM users")
    row = c.fetchone()

    if not row or row[0] == 0:
        await interaction.response.send_message("まだ統計データがありません。")
        return

    count, total, avg, max_bal, min_bal = row
    total = total or 0
    avg = avg or 0
    max_bal = max_bal or 0
    min_bal = min_bal or 0

    msg = (
        f"**📊 Raruin統計 📊**\n\n"
        f"👥 登録ユーザー数: **{count}人**\n"
        f"💰 総流通量: **{total:,} Raruin**\n"
        f"📈 平均残高: **{avg:,.0f} Raruin**\n"
        f"🔝 最高残高: **{max_bal:,} Raruin**\n"
        f"🔻 最低残高: **{min_bal:,} Raruin**"
    )

    await interaction.response.send_message(msg)

# --- スラッシュコマンド：全員にRaruin配布（管理者専用） ---
@tree.command(name="配布", description="管理者: サーバー全員にRaruinを配布します")
@app_commands.describe(金額="配布する金額")
async def distribute(interaction: discord.Interaction, 金額: int):
    if not is_admin_member(interaction.user):
        await interaction.response.send_message("管理者のみ使用可能です。")
        return

    if 金額 <= 0:
        await interaction.response.send_message("金額は正の数で指定してください。")
        return

    count = 0
    for member in interaction.guild.members:
        if not member.bot:
            ensure_user(member.id)
            add_balance(member.id, 金額)
            count += 1

    await interaction.response.send_message(
        f"✅ サーバーメンバー **{count}人** に **{金額} Raruin** ずつ配布しました！"
    )

def add_shop_item(name: str, desc: str, price: int, role_id: int | None, stock: int = -1, expires_at: int | None = None):
    c.execute("INSERT OR REPLACE INTO shop(name,description,price,role_id,stock,expires_at) VALUES(?,?,?,?,?,?)",
              (name, desc, price, role_id, stock, expires_at))
    conn.commit()

def list_shop_items():
    c.execute("SELECT name,description,price,role_id,stock,expires_at FROM shop")
    return c.fetchall()

def get_shop_item(name: str):
    c.execute("SELECT name,description,price,role_id,stock,expires_at FROM shop WHERE name = ?", (name,))
    return c.fetchone()

def reduce_stock(name: str):
    c.execute("UPDATE shop SET stock = stock - 1 WHERE name = ? AND stock > 0", (name,))
    conn.commit()

# --- スラッシュコマンド：商品追加（管理者のみ） ---
@tree.command(name="shop", description="管理者: ショップに商品を追加します")
@app_commands.describe(
    商品名="商品の名前",
    説明="商品の説明",
    値段="商品の価格",
    ロール付与="購入時に付与するロール（省略可）",
    在庫="在庫数（-1で無制限）",
    販売期限="販売期限（UNIX時間、省略可）"
)
async def shop_add(
    interaction: discord.Interaction,
    商品名: str,
    説明: str,
    値段: int,
    ロール付与: discord.Role | None = None,
    在庫: int = -1,
    販売期限: int | None = None
):
    if not is_admin_member(interaction.user):
        await interaction.response.send_message("管理者のみ使用可能です。")
        return
    role_id = ロール付与.id if ロール付与 else None
    add_shop_item(商品名, 説明, 値段, role_id, 在庫, 販売期限)
    await interaction.response.send_message(f"商品 **{商品名}** を追加しました。価格: **{値段} Raruin** 在庫: {在庫} 期限: {販売期限 if 販売期限 else 'なし'}")

# --- スラッシュコマンド：商品一覧表示 ---
@tree.command(name="ショップ", description="ショップの商品一覧を表示します")
async def shop_list(interaction: discord.Interaction):
    items = list_shop_items()
    if not items:
        await interaction.response.send_message("ショップは空です。")
        return
    msg_lines = []
    now = int(time.time())
    for name, desc, price, role_id, stock, expires_at in items:
        if expires_at and expires_at < now:
            continue  # 販売期限切れは非表示
        stock_text = f"在庫: {stock}" if stock >= 0 else "在庫: 無制限"
        expire_text = f"期限: <t:{expires_at}:D>" if expires_at else "期限: なし"
        role_text = f"<@&{role_id}>" if role_id else "ロールなし"
        msg_lines.append(f"**{name}** — {desc} — **{price} Raruin** — {stock_text} — {expire_text} — {role_text}")
    await interaction.response.send_message("\n".join(msg_lines))

# --- スラッシュコマンド：商品購入 ---
@tree.command(name="買う", description="ショップで商品を購入します")
@app_commands.describe(商品名="購入する商品の名前")
async def buy(interaction: discord.Interaction, 商品名: str):
    ensure_user(interaction.user.id)
    item = get_shop_item(商品名)
    if not item:
        await interaction.response.send_message("その商品は存在しません。")
        return
    name, desc, price, role_id, stock, expires_at = item
    now = int(time.time())
    if expires_at and expires_at < now:
        await interaction.response.send_message("この商品の販売期限は終了しています。")
        return
    if stock == 0:
        await interaction.response.send_message("この商品は在庫切れです。")
        return
    bal = get_balance(interaction.user.id)
    if bal < price:
        await interaction.response.send_message("残高が不足しています。")
        return
    add_balance(interaction.user.id, -price)
    if stock > 0:
        reduce_stock(name)
    if role_id:
        role = interaction.guild.get_role(role_id)
        if role:
            await interaction.user.add_roles(role)
    await interaction.response.send_message(f"**{name}** を購入しました！支払: **{price} Raruin** 残高: **{get_balance(interaction.user.id)} Raruin**")
poker_lobbies = {}  # {guild_id: [user_id1, user_id2, ...]}

# --- ロビー参加コマンド ---
@tree.command(name="ポーカーロビー", description="ポーカーのロビーに参加します")
async def poker_lobby(interaction: discord.Interaction):
    gid = interaction.guild.id
    poker_lobbies.setdefault(gid, [])
    if interaction.user.id not in poker_lobbies[gid]:
        poker_lobbies[gid].append(interaction.user.id)
        await interaction.response.send_message(f"{interaction.user.display_name} がロビーに参加しました。現在の人数: {len(poker_lobbies[gid])}")
    else:
        await interaction.response.send_message("すでにロビーに参加しています。")

# --- ポーカー開始コマンド（1ラウンド） ---
@tree.command(name="ポーカー開始", description="ロビー内のメンバーでポーカーを開始します")
async def poker_start(interaction: discord.Interaction):
    gid = interaction.guild.id
    members = poker_lobbies.get(gid, [])
    if len(members) < 2:
        await interaction.response.send_message("最低2人以上必要です。")
        return
    ranks = list(range(2,15))
    suits = ["♠","♥","♦","♣"]
    def deal_hand():
        deck = [(r,s) for r in ranks for s in suits]
        random.shuffle(deck)
        return deck[:5]
    def hand_score(hand):
        rs = sorted([r for r,s in hand])
        ss = [s for r,s in hand]
        counts = {r: rs.count(r) for r in set(rs)}
        is_flush = len(set(ss)) == 1
        is_straight = all(rs[i]+1==rs[i+1] for i in range(len(rs)-1))
        if is_straight and is_flush: return (8, max(rs))
        if 4 in counts.values(): return (7, max(k for k,v in counts.items() if v==4))
        if 3 in counts.values() and 2 in counts.values(): return (6, max(k for k,v in counts.items() if v==3))
        if is_flush: return (5, max(rs))
        if is_straight: return (4, max(rs))
        if 3 in counts.values(): return (3, max(k for k,v in counts.items() if v==3))
        if list(counts.values()).count(2) == 2: return (2, max(k for k,v in counts.items() if v==2))
        if 2 in counts.values(): return (1, max(k for k,v in counts.items() if v==2))
        return (0, max(rs))
    hands = {uid: deal_hand() for uid in members}
    scores = {uid: hand_score(hands[uid]) for uid in members}
    winner_id = max(scores, key=lambda uid: scores[uid])
    winner_score = scores[winner_id]
    prize = 100
    add_balance(winner_id, prize)
    msg = f"🏆 ポーカー勝者: <@{winner_id}>（役ランク: {winner_score[0]}）\n報酬: **{prize} Raruin**\n\n"
    for uid in members:
        hand = hands[uid]
        score = scores[uid]
        msg += f"<@{uid}> の手札: {hand}（役ランク: {score[0]}）\n"
    poker_lobbies[gid] = []  # ロビーリセット
    await interaction.response.send_message(msg)

# --- ポイントシステムの追加 ---
points_db = {}

def add_points(user_id: int, points: int):
    if user_id not in points_db:
        points_db[user_id] = 0
    points_db[user_id] += points

def get_points(user_id: int) -> int:
    return points_db.get(user_id, 0)

# --- ランク制度の追加 ---
def get_user_rank(user_id: int) -> str:
    bal = get_balance(user_id)
    if bal >= 1000:
        return "ゴールド"
    elif bal >= 500:
        return "シルバー"
    else:
        return "ブロンズ"

# --- スラッシュコマンド：ポイント確認 ---
@tree.command(name="ポイント", description="自分のポイントを表示します")
async def points(interaction: discord.Interaction):
    user_id = interaction.user.id
    ensure_user(user_id)
    pts = get_points(user_id)
    await interaction.response.send_message(f"あなたのポイント: **{pts}**")

# --- スラッシュコマンド：ランク確認 ---
@tree.command(name="ランク", description="自分のランクを表示します")
async def rank(interaction: discord.Interaction):
    user_id = interaction.user.id
    ensure_user(user_id)
    rank = get_user_rank(user_id)
    await interaction.response.send_message(f"あなたのランク: **{rank}**")

# --- ミニゲーム：サイコロ ---
@tree.command(name="サイコロ", description="サイコロを振ってRaruinを獲得しよう！")
async def dice(interaction: discord.Interaction):
    ensure_user(interaction.user.id)
    roll = random.randint(1, 6)
    award = roll * 10  # 1から6の値に10を掛けたRaruinを獲得
    add_balance(interaction.user.id, award)
    add_points(interaction.user.id, roll)  # サイコロの目に基づくポイント獲得
    await interaction.response.send_message(f"🎲 サイコロの目: {roll} - **{award} Raruin** 獲得！")

# --- 起動時処理の変更 --
@bot.event
async def on_ready():
    await tree.sync()
    print(f"Bot 起動: {bot.user} - 準備完了！")

keep_alive()  # HTTPサーバーを起動

try:
    print("Discord Botを起動しています...")
    print("Discord Developer Portalで以下の権限が有効になっていることを確認してください:")
    print("  ✅ PRESENCE INTENT")
    print("  ✅ SERVER MEMBERS INTENT")
    print("  ✅ MESSAGE CONTENT INTENT")
    bot.run(TOKEN)
except discord.errors.PrivilegedIntentsRequired:
    print("\n❌ エラー: 特権インテントが有効になっていません。")
    print("\n以下の手順で設定してください:")
    print("1. https://discord.com/developers/applications/ にアクセス")
    print("2. あなたのアプリケーションを選択")
    print("3. 左側メニューから「Bot」を選択")
    print("4. 「Privileged Gateway Intents」セクションで以下をすべてONにする:")
    print("   - PRESENCE INTENT")
    print("   - SERVER MEMBERS INTENT")
    print("   - MESSAGE CONTENT INTENT")
    print("5. 「Save Changes」をクリック")
    print("6. 数分待ってから再度実行してください")
except discord.errors.LoginFailure:
    print("\n❌ エラー: トークンが無効です。")
    print("SecretツールでDISCORD_TOKENを確認してください。")
except Exception as e:
    print(f"\n❌ 予期しないエラー: {e}")