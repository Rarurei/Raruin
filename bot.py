import os
import discord
from discord import app_commands, ui
from discord.ext import commands
from dotenv import load_dotenv
from datetime import datetime
import json
from flask import Flask
import threading
from google.cloud import firestore
from google.cloud.firestore_v1 import Transaction
from typing import Union, List
import random
from datetime import date
import asyncio

# === 環境設定 ===
load_dotenv()

# Discordトークンのチェック
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    print("【致命的エラー】DISCORD_TOKEN が設定されていません。")

# ADMIN_ID (空なら空リスト)
admin_raw = os.getenv("ADMIN_ID", "")
ADMIN_IDS = [int(x.strip()) for x in admin_raw.split(",") if x.strip().isdigit()]

# 各種チャンネルID (設定がなければ0にする)
BACKUP_CHANNEL_ID = int(os.getenv("BACKUP_CHANNEL_ID") or 0)
ITEM_USED_CHANNEL_ID = int(os.getenv("ITEM_USED_CHANNEL_ID") or 0)

# Google認証設定
google_creds = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")

if google_creds:
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = google_creds
else:
    # RenderのSecret Fileの標準的な場所を指定
    secret_path = "/etc/secrets/google-key.json"
    if os.path.exists(secret_path):
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = secret_path
    else:
        # どちらも見つからない場合
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "google-key.json"

print(f"Final Credentials Path: {os.environ['GOOGLE_APPLICATION_CREDENTIALS']}")
CURRENCY_NAME = "Raruin"

# === Firestore ===
db = firestore.Client()

def user_doc(user_id):
    return db.collection("users").document(str(user_id))
def shop_doc(shop_name):
    return db.collection("shops").document(shop_name)
def product_doc(shop_name, product_name):
    return shop_doc(shop_name).collection("products").document(product_name)
def user_item_doc(user_id, shop_name, product_name):
    return user_doc(user_id).collection("items").document(f"{shop_name}:{product_name}")
def is_admin(user):
    return user.id in ADMIN_IDS

def get_user_balance(user_id):
    doc = user_doc(user_id).get()
    if doc.exists:
        val = doc.to_dict()
        return int(val.get("balance",1000)), int(val.get("earned",0)), int(val.get("spent",0))
    else:
        user_doc(user_id).set({"balance":1000, "earned":0, "spent":0})
        return 1000,0,0
def change_balance(user_id, amount, is_add=True):
    doc = user_doc(user_id)
    if is_add:
        doc.set({
            "balance":firestore.Increment(amount),
            "earned":firestore.Increment(amount)
        }, merge=True)
    else:
        doc.set({
            "balance":firestore.Increment(-amount),
            "spent":firestore.Increment(amount)
        }, merge=True)
def shop_exists(shop_name):
    return shop_doc(shop_name).get().exists

# discord.py intents
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.guilds = True
intents.members = True
bot = commands.Bot(command_prefix="/", intents=intents)
tree = bot.tree

# --- async autocomplete ---
async def user_autocomplete(interaction: discord.Interaction, current: str):
    return [
        app_commands.Choice(name=m.display_name, value=str(m.id))
        for m in interaction.guild.members if current.lower() in m.display_name.lower()
    ][:25]

async def shop_autocomplete(interaction: discord.Interaction, current: str):
    shops = [doc.id for doc in db.collection("shops").stream()]
    return [
        app_commands.Choice(name=s, value=s)
        for s in shops if current.lower() in s.lower()
    ][:25]

async def myitem_key_autocomplete(interaction: discord.Interaction, current: str):
    items = []
    for doc in user_doc(interaction.user.id).collection("items").stream():
        pname = doc.id.split(":",1)[1]
        sname = doc.id.split(":",1)[0]
        display = f"{pname}（{sname}）"
        items.append((display, doc.id))
    return [
        app_commands.Choice(name=disp, value=key)
        for disp, key in items if current.lower() in disp.lower()
    ][:25]

async def product_autocomplete(interaction: discord.Interaction, current: str):
    # すでにショップ名が入力されているか確認
    shop_name = interaction.namespace.shop_name
    if not shop_name or not shop_exists(shop_name):
        return []

    # そのショップの商品一覧を取得
    prods = []
    for doc in shop_doc(shop_name).collection("products").stream():
        p_data = doc.to_dict()
        p_name = doc.id
        price = p_data.get("price", 0)
        # 候補に「商品名 (価格 Raruin)」と表示
        display_name = f"{p_name} ({price} {CURRENCY_NAME})"
        
        if current.lower() in p_name.lower():
            prods.append(app_commands.Choice(name=display_name, value=p_name))
    
    return prods[:25]


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user.name}")
    try:
        # スラッシュコマンドをDiscordに送信して登録する
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} command(s)")
    except Exception as e:
        print(f"Sync error: {e}")
        
# --- コマンド群 ---
@tree.command(name="リセット", description=f"ユーザーまたはロールの残高・統計をリセット（管理者）")
@app_commands.describe(target="対象（ユーザーまたはロール）")
async def reset_balance_cmd(interaction: discord.Interaction, target: Union[discord.Member, discord.Role]):
    if not is_admin(interaction.user):
        await interaction.response.send_message("管理者限定", ephemeral=True); return

    # タイムアウト対策
    await interaction.response.defer(ephemeral=True)

    def reset_user(uid):
        user_doc(uid).set({"balance": 1000, "earned": 0, "spent": 0}, merge=True)

    if isinstance(target, discord.Role):
        for member in target.members:
            if not member.bot:
                reset_user(member.id)
        await interaction.followup.send(f"ロール「{target.name}」の全員の残高・統計をリセットしました。")
    else:
        reset_user(target.id)
        await interaction.followup.send(f"{target.display_name} の残高・統計をリセットしました。")
        
@tree.command(name="付与", description=f"ユーザーまたはロールに {CURRENCY_NAME} 付与")
@app_commands.describe(target="対象（ユーザーまたはロール）", amount=f"{CURRENCY_NAME}額")
async def add_raurin(interaction: discord.Interaction, target: Union[discord.Member, discord.Role], amount: int):
    if not is_admin(interaction.user):
        await interaction.response.send_message("管理者限定", ephemeral=True); return
    if amount <= 0:
        await interaction.response.send_message("金額が不正です", ephemeral=True); return

    # 処理が長引く可能性があるので「考え中」にする
    await interaction.response.defer(ephemeral=True)

    if isinstance(target, discord.Role):
        for member in target.members:
            if not member.bot:
                change_balance(member.id, amount, is_add=True)
        await interaction.followup.send(f"ロール「{target.name}」の全員に {amount}{CURRENCY_NAME} を付与しました。")
    else:
        change_balance(target.id, amount, is_add=True)
        try: await target.send(f"あなたに {amount}{CURRENCY_NAME} が付与されました。")
        except: pass
        await interaction.followup.send(f"{target.display_name} に {amount}{CURRENCY_NAME} 付与しました。")

@tree.command(name="減額", description=f"ユーザーまたはロールから {CURRENCY_NAME} 減額")
@app_commands.describe(target="対象（ユーザーまたはロール）", amount=f"{CURRENCY_NAME}額")
async def remove_raurin(interaction: discord.Interaction, target: Union[discord.Member, discord.Role], amount: int):
    if not is_admin(interaction.user):
        await interaction.response.send_message("管理者限定", ephemeral=True); return
    if amount <= 0:
        await interaction.response.send_message("金額が不正です", ephemeral=True); return

    await interaction.response.defer(ephemeral=True)

    if isinstance(target, discord.Role):
        for member in target.members:
            if not member.bot:
                change_balance(member.id, amount, is_add=False)
        await interaction.followup.send(f"ロール「{target.name}」の全員から {amount}{CURRENCY_NAME} を減額しました。")
    else:
        change_balance(target.id, amount, is_add=False)
        await interaction.followup.send(f"{target.display_name} から {amount}{CURRENCY_NAME} 減額しました。")

@tree.command(name="shop", description="ショップ追加/削除（管理者）")
@app_commands.describe(action="追加or削除", shop_name="ショップ名")
@app_commands.choices(action=[
    app_commands.Choice(name="追加", value="add"),
    app_commands.Choice(name="削除", value="remove")
])
async def shop_command(interaction, action:str, shop_name:str):
    if not is_admin(interaction.user):
        await interaction.response.send_message("管理者限定", ephemeral=True);return
    if action=="add":
        shop_doc(shop_name).set({})
        await interaction.response.send_message(f"ショップ「{shop_name}」追加", ephemeral=True)
    elif action=="remove":
        shop_doc(shop_name).delete()
        await interaction.response.send_message(f"ショップ「{shop_name}」削除", ephemeral=True)

@tree.command(name="shop商品", description="商品の追加/削除（管理者）")
@app_commands.describe(
    action="追加or削除", product_name="商品名", shop_name="ショップ名",
    description="商品の説明", price="金額", stock="在庫", buy_role="購入可能ロールID"
)
@app_commands.choices(action=[
    app_commands.Choice(name="追加", value="add"),
    app_commands.Choice(name="削除", value="remove")
])
@app_commands.autocomplete(shop_name=shop_autocomplete)
async def shopitem_command(
    interaction, action:str, product_name:str, shop_name:str,
    description:str="", price:int=0, stock:int=0, buy_role:int=0
):
    if not is_admin(interaction.user):
        await interaction.response.send_message("管理者限定", ephemeral=True);return
    if not shop_exists(shop_name):
        await interaction.response.send_message("ショップがありません", ephemeral=True);return
    if action=="add":
        product_doc(shop_name,product_name).set({
            "description":description, "price":price, "stock":stock, "buy_role":buy_role
        })
        await interaction.response.send_message(f"{shop_name}に商品「{product_name}」追加", ephemeral=True)
    else:
        product_doc(shop_name,product_name).delete()
        await interaction.response.send_message(f"{shop_name}の商品「{product_name}」削除", ephemeral=True)

@tree.command(name="残高", description=f"{CURRENCY_NAME}残高・獲得/消費表示")
async def balance_cmd(interaction):
    b,e,s = get_user_balance(interaction.user.id)
    await interaction.response.send_message(
        f"あなたの残高:\n**{b} {CURRENCY_NAME}**\n獲得:{e} 消費:{s}", ephemeral=True
    )

class RankingPagination(discord.ui.View):
    def __init__(self, users, guild):
        super().__init__(timeout=60)
        self.users = users
        self.guild = guild
        self.page = 0
        self.max_page = (len(users) - 1) // 10

    def create_embed(self):
        start = self.page * 10
        end = start + 10
        current_users = self.users[start:end]
        
        embed = discord.Embed(title=f"{CURRENCY_NAME}ランキング ({self.page + 1}/{self.max_page + 1}ページ)")
        for idx, u in enumerate(current_users):
            member = self.guild.get_member(u["user_id"])
            name = member.display_name if member else f"不明({u['user_id']})"
            embed.add_field(
                name=f"{start + idx + 1}位 {name}", 
                value=f"残高: {u.get('balance',0)} / 累計獲得: {u.get('earned',0)}", 
                inline=False
            )
        return embed

    @discord.ui.button(label="前へ", style=discord.ButtonStyle.gray)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page > 0:
            self.page -= 1
            await interaction.response.edit_message(embed=self.create_embed(), view=self)
        else:
            await interaction.response.send_message("最初のページです", ephemeral=True)

    @discord.ui.button(label="次へ", style=discord.ButtonStyle.gray)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page < self.max_page:
            self.page += 1
            await interaction.response.edit_message(embed=self.create_embed(), view=self)
        else:
            await interaction.response.send_message("最後のページです", ephemeral=True)

@tree.command(name="ランキング", description=f"{CURRENCY_NAME}ランキング")
async def ranking_cmd(interaction: discord.Interaction):
    target_role_id = 1408273149199650867
    
    # 【自動削除】ロールを持っていない場合、Firestoreからその人のデータを消す
    if not any(role.id == target_role_id for role in interaction.user.roles):
        user_doc(interaction.user.id).delete() # データを削除
        await interaction.response.send_message("❌ 認証ロールがないため、データをリセットしました。実行できません。", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    users = []
    for doc in db.collection("users").stream():
        data = doc.to_dict()
        users.append({**data, "user_id": int(doc.id)})
    
    users.sort(key=lambda x: x.get('balance', 0), reverse=True)
    if not users:
        await interaction.followup.send("データがありません。")
        return

    view = RankingPagination(users, interaction.guild)
    await interaction.followup.send(embed=view.create_embed(), view=view)
    
@tree.command(name="渡す", description=f"ユーザーに {CURRENCY_NAME} を渡す")
@app_commands.describe(target="渡す相手", amount=f"{CURRENCY_NAME}額")
async def transfer_cmd(interaction: discord.Interaction, target: discord.Member, amount: int):
    target_role_id = 1408273149199650867
    
    # 【自動削除】
    if not any(role.id == target_role_id for role in interaction.user.roles):
        user_doc(interaction.user.id).delete()
        await interaction.response.send_message("❌ 認証ロールがないため、データをリセットしました。", ephemeral=True)
        return

    if target.id == interaction.user.id or amount <= 0:
        await interaction.response.send_message("不正な指定です", ephemeral=True); return
    
    b, _, _ = get_user_balance(interaction.user.id)
    if b < amount:
        await interaction.response.send_message("残高不足です", ephemeral=True); return

    change_balance(interaction.user.id, amount, is_add=False)
    change_balance(target.id, amount, is_add=True)
    
    await interaction.response.send_message(f"{target.display_name} に {amount}{CURRENCY_NAME} 渡しました", ephemeral=True)

@tree.command(name="ショップ一覧", description="ショップ一覧（10件/ページ）")
@app_commands.describe(page="ページ(デフォルト1)")
async def shop_list_cmd(interaction, page:int=1):
    shops = [doc.id for doc in db.collection("shops").stream()]
    max_page = max(1,(len(shops)-1)//10+1)
    page = max(1,min(page,max_page))
    embed = discord.Embed(title="ショップ一覧", description=f"{page}/{max_page}")
    start = (page-1)*10
    for s in shops[start:start+10]:
        embed.add_field(name=s, value=s, inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@tree.command(name="ショップ", description="指定ショップの商品一覧（ページあり）")
@app_commands.describe(shop_name="ショップ名", page="ページ(デフォルト1)")
@app_commands.autocomplete(shop_name=shop_autocomplete)
async def shop_detail_cmd(interaction, shop_name:str, page:int=1):
    if not shop_exists(shop_name):
        await interaction.response.send_message("ショップがありません", ephemeral=True);return
    prods = [
        doc.to_dict() | {"product_name":doc.id}
        for doc in shop_doc(shop_name).collection("products").stream()
    ]
    max_page = max(1,(len(prods)-1)//10+1)
    page = max(1,min(page,max_page))
    embed = discord.Embed(title=f"{shop_name}商品一覧", description=f"{page}/{max_page}")
    start = (page-1)*10
    for p in prods[start:start+10]:
        embed.add_field(
            name=p["product_name"],
            value=f'{p.get("description","")}\n価格:{p.get("price",0)}{CURRENCY_NAME}\n在庫:{p.get("stock",0) if p.get("stock",0)!=0 else "無限"}',
            inline=False,
        )
    await interaction.response.send_message(embed=embed, ephemeral=True)

@tree.command(name="買う", description="商品購入")
@app_commands.describe(shop_name="ショップ名", product_name="商品名")
@app_commands.autocomplete(shop_name=shop_autocomplete, product_name=product_autocomplete)
async def buy_cmd(interaction: discord.Interaction, shop_name: str, product_name: str):
    doc = product_doc(shop_name, product_name).get()
    if not doc.exists:
        await interaction.response.send_message("その商品は存在しません", ephemeral=True)
        return

    val = doc.to_dict()
    price = val.get("price", 0)
    stock = val.get("stock", 0)
    
    b, _, _ = get_user_balance(interaction.user.id)
    if b < price:
        await interaction.response.send_message(f"残高が足りません（必要: {price} {CURRENCY_NAME}）", ephemeral=True)
        return
    
    if stock != 0 and stock < 1:
        await interaction.response.send_message("在庫切れです", ephemeral=True)
        return
    
    # 購入処理
    change_balance(interaction.user.id, price, is_add=False)
    if stock != 0:
        product_doc(shop_name, product_name).update({"stock": stock - 1})
    
    user_item_doc(interaction.user.id, shop_name, product_name).set({
        "amount": firestore.Increment(1),
        "shop_name": shop_name,
        "product_name": product_name
    }, merge=True)
    
    await interaction.response.send_message(f"「{product_name}」を {price} {CURRENCY_NAME} で購入しました！", ephemeral=True)

class ItemListView(ui.View):
    def __init__(self, user_id, items, page=1):
        super().__init__(timeout=120)
        self.user_id = user_id
        self.items = items
        self.page = page
        self.max_page = max(1,(len(items)-1)//10+1)
        if self.page > 1:
            self.add_item(ui.Button(label="前のページ", style=discord.ButtonStyle.secondary, custom_id="prev"))
        if self.page < self.max_page:
            self.add_item(ui.Button(label="次のページへ", style=discord.ButtonStyle.success, custom_id="next"))
    async def interaction_check(self, interaction):
        return interaction.user.id == self.user_id
    @ui.button(label="前のページ", style=discord.ButtonStyle.secondary, custom_id="prev", row=0)
    async def prev_page(self, interaction:discord.Interaction, button:ui.Button):
        self.page -= 1
        await send_item_list(interaction, self.user_id, self.items, self.page)
        self.stop()
    @ui.button(label="次のページへ", style=discord.ButtonStyle.success, custom_id="next", row=0)
    async def next_page(self, interaction:discord.Interaction, button:ui.Button):
        self.page += 1
        await send_item_list(interaction, self.user_id, self.items, self.page)
        self.stop()

async def send_item_list(interaction, user_id, items, page):
    max_page = max(1,(len(items)-1)//10+1)
    page = max(1,min(page,max_page))
    embed = discord.Embed(title="所持アイテム一覧", description=f"{page}/{max_page}")
    start = (page-1)*10
    for itm in items[start:start+10]:
        embed.add_field(
            name=f"{itm['product_name']}（{itm['shop_name']}）",
            value=f"個数: {itm.get('amount',0)}",
            inline=False
        )
    view = ItemListView(user_id, items, page)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

@tree.command(name="アイテム表示", description="所持アイテム一覧（ページング）")
@app_commands.describe(page="ページ(デフォルト1)")
async def item_list_cmd(interaction, page:int=1):
    items = [
        {**doc.to_dict(), "shop_name": doc.id.split(":")[0], "product_name":doc.id.split(":")[1]}
        for doc in user_doc(interaction.user.id).collection("items").stream()
    ]
    if not items:
        await interaction.response.send_message("所持アイテムはありません", ephemeral=True);return
    await send_item_list(interaction, interaction.user.id, items, page)

@tree.command(name="アイテム渡す", description="所持アイテムを他人に渡す")
@app_commands.describe(target="渡す相手", item="渡すアイテム")
@app_commands.autocomplete(item=myitem_key_autocomplete)
async def item_transfer_cmd(interaction: discord.Interaction, target: discord.Member, item: str):
    target_role_id = 1408273149199650867
    
    # 【自動削除】
    if not any(role.id == target_role_id for role in interaction.user.roles):
        user_doc(interaction.user.id).delete()
        # アイテムコレクションも消す場合は以下を追加
        for sub_doc in user_doc(interaction.user.id).collection("items").stream():
            sub_doc.reference.delete()
            
        await interaction.response.send_message("❌ 認証ロールがないため、全アイテムとデータを削除しました。", ephemeral=True)
        return

    if target.id == interaction.user.id or ":" not in item:
        await interaction.response.send_message("不正な指定です", ephemeral=True); return
    
    # (以下、元々のアイテム転送処理)
    shop_name, product_name = item.split(":", 1)
    from_ref = user_item_doc(interaction.user.id, shop_name, product_name)
    to_ref = user_item_doc(target.id, shop_name, product_name)

    @firestore.transactional
    def do_transfer(transaction):
        from_snap = from_ref.get(transaction=transaction)
        to_snap = to_ref.get(transaction=transaction)
        if not from_snap.exists: return False
        data = from_snap.to_dict()
        now_amt = data.get("amount", 0)
        if now_amt < 1: return False
        if now_amt == 1: transaction.delete(from_ref)
        else: transaction.update(from_ref, {"amount": now_amt - 1})
        if to_snap.exists: transaction.update(to_ref, {"amount": to_snap.to_dict().get("amount", 0) + 1})
        else: transaction.set(to_ref, {"amount": 1, "shop_name": shop_name, "product_name": product_name})
        return True

    if do_transfer(db.transaction()):
        await interaction.response.send_message(f"{target.display_name}に{product_name}を1個渡しました", ephemeral=True)
    else:
        await interaction.response.send_message("アイテムを持っていません", ephemeral=True)

@tree.command(name="ログイン", description="1日1回限定！ランダムで Raruin を獲得します")
async def login_bonus_cmd(interaction: discord.Interaction):
    user_id = interaction.user.id
    today = str(date.today())  # "2023-10-27" のような形式
    
    # ユーザーデータを取得
    doc_ref = user_doc(user_id)
    doc = doc_ref.get()
    
    last_login = ""
    if doc.exists:
        last_login = doc.to_dict().get("last_login", "")

    # 日付チェック
    if last_login == today:
        await interaction.response.send_message(
            "今日のログインボーナスは既に受け取っています。また明日来てくださいね！", 
            ephemeral=True
        )
        return

    # 1〜10000のランダムな金額を決定
    reward = random.randint(1, 10000)
    
    # Firestoreの更新（残高加算 + 統計更新 + ログイン日記録）
    doc_ref.set({
        "balance": firestore.Increment(reward),
        "earned": firestore.Increment(reward),
        "last_login": today
    }, merge=True)

    # 演出用のメッセージ（高額当選時に少し変えるなど）
    msg = f"ログインボーナス！ **{reward} {CURRENCY_NAME}** を獲得しました！"
    if reward >= 9000:
        msg = f"✨ **超ラッキー！** ✨\n最高級のログインボーナス！ **{reward} {CURRENCY_NAME}** を獲得しました！"
    elif reward <= 100:
        msg = f"ログインボーナス！ **{reward} {CURRENCY_NAME}** を獲得しました。明日はもっと当たるといいですね！"

    await interaction.response.send_message(msg, ephemeral=True)

@tree.command(name="データ整理", description="認証ロールがないユーザーのデータをFirestoreから削除します（管理者用）")
async def cleanup_data(interaction: discord.Interaction):
    if not is_admin(interaction.user):
        await interaction.response.send_message("管理者限定です", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    
    target_role_id = 1408273149199650867
    guild = interaction.guild
    users_ref = db.collection("users")
    
    deleted_count = 0
    total_count = 0

    # Firestoreから全ユーザーを取得
    docs = users_ref.stream()

    for doc in docs:
        total_count += 1
        user_id_str = doc.id
        try:
            user_id = int(user_id_str)
            member = guild.get_member(user_id)

            # メンバーがサーバーにいない、または特定のロールを持っていない場合
            if member is None or not any(role.id == target_role_id for role in member.roles):
                # Firestoreから削除
                users_ref.document(user_id_str).delete()
                deleted_count += 1
        except Exception as e:
            print(f"Error processing {user_id_str}: {e}")

    await interaction.followup.send(
        f"データ整理が完了しました。\n"
        f"チェック対象: {total_count}件\n"
        f"削除された非認証ユーザー: {deleted_count}件", 
        ephemeral=True
    )

# ==============================
# 宝くじシステム（ユニット方式・Firestore版）
# ==============================

# === 宝くじ用 Firestore ヘルパー ===
def lottery_doc(name):
    return db.collection("lottery_settings").document(name)

# === 共通関数 ===
def today_yyyymmdd():
    return int(datetime.now().strftime("%Y%m%d"))

# === オートコンプリート関数（コマンドより上に配置） ===
async def lottery_name_autocomplete(interaction: discord.Interaction, current: str):
    # 販売期限内かつ在庫あり
    today = today_yyyymmdd()
    docs = db.collection("lottery_settings").stream()
    choices = []
    for doc in docs:
        d = doc.to_dict()
        # 期限内かつ残数が1以上
        if int(d.get("end_date", 0)) >= today and d.get("remaining", 0) > 0:
            if current.lower() in doc.id.lower():
                choices.append(app_commands.Choice(name=f"{doc.id} (残り{d['remaining']}枚)", value=doc.id))
    return choices[:25]

async def lottery_name_all_autocomplete(interaction: discord.Interaction, current: str):
    # 管理用：削除などは期限切れも含めて表示
    docs = db.collection("lottery_settings").stream()
    return [app_commands.Choice(name=doc.id, value=doc.id) for doc in docs if current.lower() in doc.id.lower()][:25]

# === 抽選ロジック ===
def draw_unit_lottery(setting: dict, count: int):
    """
    ユニット（残り本数）方式の抽選
    """
    results = {1:0, 2:0, 3:0, 4:0, 5:0, 6:0, "lose":0}
    reward = 0
    
    # くじ箱の中身をシミュレート
    pool = []
    for grade in range(1, 7):
        # DBに保存されている「各等級の残り本数」をプールに入れる
        count_in_box = setting.get(f"count{grade}", 0)
        pool.extend([grade] * count_in_box)
    
    # はずれの数を計算 (現在の総在庫 - 当たり合計)
    current_remaining = setting.get("remaining", 0)
    loses = max(0, current_remaining - len(pool))
    pool.extend(["lose"] * loses)

    # 購入枚数分、ランダムに重複なしで取り出す
    my_draws = random.sample(pool, min(count, len(pool)))

    for res in my_draws:
        results[res] += 1
        if res != "lose":
            reward += setting.get(f"prize{res}", 0)

    return results, reward

# === スラッシュコマンド ===

@tree.command(name="宝くじ設定", description="宝くじの追加・削除（管理者専用）")
@app_commands.describe(mode="追加 または 削除", name="宝くじ名", price="1枚の価格", total="総枚数", end_date="期限 YYYYMMDD")
@app_commands.choices(mode=[
    app_commands.Choice(name="追加", value="add"), 
    app_commands.Choice(name="削除", value="remove")
])
@app_commands.autocomplete(name=lottery_name_all_autocomplete)
async def lottery_setting(
    interaction: discord.Interaction, mode: str, name: str, 
    price: int=0, total: int=0, end_date: str="",
    count1: int=0, prize1: int=0, count2: int=0, prize2: int=0,
    count3: int=0, prize3: int=0, count4: int=0, prize4: int=0,
    count5: int=0, prize5: int=0, count6: int=0, prize6: int=0
):
    if not is_admin(interaction.user):
        await interaction.response.send_message("管理者限定です", ephemeral=True); return
    await interaction.response.defer(ephemeral=True)

    if mode == "remove":
        lottery_doc(name).delete()
        await interaction.followup.send(f"宝くじ「{name}」を削除しました。")
    else:
        # 当たりの合計が総枚数を超えていないかチェック
        hit_sum = count1 + count2 + count3 + count4 + count5 + count6
        if hit_sum > total:
            await interaction.followup.send(f"⚠️ エラー：当たりの合計（{hit_sum}本）が総枚数（{total}枚）を超えています。", ephemeral=True)
            return

        data = {
            "price": price, "total": total, "remaining": total, "end_date": end_date,
            "count1": count1, "prize1": prize1, "count2": count2, "prize2": prize2,
            "count3": count3, "prize3": prize3, "count4": count4, "prize4": prize4,
            "count5": count5, "prize5": prize5, "count6": count6, "prize6": prize6
        }
        lottery_doc(name).set(data)
        await interaction.followup.send(f"宝くじ「{name}」を設定しました。\n総数: {total}枚 (1等: {count1}本) | 価格: {price}")

@tree.command(name="宝くじ", description="宝くじを購入して抽選します")
@app_commands.describe(name="宝くじの種類", count="購入枚数")
@app_commands.autocomplete(name=lottery_name_autocomplete)
async def lottery_buy(interaction: discord.Interaction, name: str, count: int):
    if count <= 0:
        await interaction.response.send_message("1枚以上指定してください", ephemeral=True); return
    
    await interaction.response.defer(ephemeral=True)
    
    l_doc_ref = lottery_doc(name)
    l_doc = l_doc_ref.get()
    if not l_doc.exists:
        await interaction.followup.send("指定された宝くじが見つかりません。"); return
    
    setting = l_doc.to_dict()
    
    # 日付チェック
    try:
        if int(setting.get("end_date", 0)) < today_yyyymmdd():
            await interaction.followup.send("この宝くじは販売期限切れです。"); return
    except ValueError:
        pass # 日付が空などの場合
    
    rem = setting.get("remaining", 0)
    if rem <= 0:
        await interaction.followup.send("完売しました！"); return
    
    buy_count = min(count, rem)
    total_cost = buy_count * setting.get("price", 0)
    
    # 残高チェック
    balance, _, _ = get_user_balance(interaction.user.id)
    if balance < total_cost:
        await interaction.followup.send(f"残高不足です。 (必要: {total_cost} {CURRENCY_NAME})"); return

    # 抽選実行
    results, reward = draw_unit_lottery(setting, buy_count)
    
    # DB更新：支払い
    change_balance(interaction.user.id, total_cost, is_add=False)
    # DB更新：当選金
    if reward > 0:
        change_balance(interaction.user.id, reward, is_add=True)
    
    # DB更新：在庫と当たり本数の更新
    updates = {"remaining": firestore.Increment(-buy_count)}
    for k in range(1, 7):
        if results[k] > 0:
            updates[f"count{k}"] = firestore.Increment(-results[k])
    l_doc_ref.update(updates)

    # 結果表示
    msg = f"🛒 **{name}** を {buy_count} 枚購入しました！ (合計 {total_cost} {CURRENCY_NAME})\n\n"
    msg += "📊 **抽選結果**\n"
    for k in range(1, 7):
        if results[k] > 0:
            msg += f"・{k}等: {results[k]}本\n"
    
    if results['lose'] > 0:
        msg += f"・はずれ: {results['lose']}本\n"
    
    msg += f"\n💰 **合計獲得:** {reward} {CURRENCY_NAME}\n"
    msg += f"📦 **残り在庫:** {rem - buy_count}枚"
    
    await interaction.followup.send(msg)

    
    # バックアップ送信
    backup_ch = bot.get_channel(BACKUP_CHANNEL_ID)
    if backup_ch:
        backup = {
            "user_id":interaction.user.id,
            "product_name":product_name,
            "shop_name":shop_name,
            "date":datetime.now().isoformat()
        }
        await backup_ch.send(f"【Raruin Item Used Log】\n```json\n{json.dumps(backup, ensure_ascii=False, indent=2)}\n```")

# 通知を送るチャンネルID
NOTIFICATION_CHANNEL_ID = 1458775432726839464

# --- メッセージ報酬の処理 ---
@bot.event
async def on_message(message):
    # サーバー内での発言かつ、Bot以外のユーザーの場合
    if message.guild and not message.author.bot:
        # 文字数(len)を取得して 1文字 = 1 Raruin 付与
        msg_reward = len(message.content)
        if msg_reward > 0:
            change_balance(message.author.id, msg_reward, is_add=True)
    
    # スラッシュコマンドを正常に動作させるために必須
    await bot.process_commands(message)

# --- 通話報酬の処理（通話通知のみスパム対策版） ---
voice_times = {}
voice_notification_queue = []  # 通知を溜めるリスト
is_voice_queue_running = False # タイマーが動いているかどうかのフラグ

async def send_voice_notifications(channel):
    """15秒後にまとめて通知を送る関数"""
    global voice_notification_queue, is_voice_queue_running
    
    # 15秒待機（この間に他の人が抜けてもキューに溜まる）
    await asyncio.sleep(15)
    
    if voice_notification_queue:
        # メッセージを改行で結合して1つのメッセージにする
        content = "\n".join(voice_notification_queue)
        
        # 文字数制限対策 (念のため1900文字でカット)
        if len(content) > 1900:
            content = content[:1900] + "\n...(他多数)"
            
        try:
            await channel.send(content)
        except Exception as e:
            print(f"通話通知の送信に失敗: {e}")
            
        # 送信したらリストを空にする
        voice_notification_queue = []
        
    # フラグを下ろす（次の通知待ちを受け付けられるようにする）
    is_voice_queue_running = False

@bot.event
async def on_voice_state_update(member, before, after):
    global is_voice_queue_running

    # --- 入室時の処理 ---
    if not before.channel and after.channel:
        voice_times[member.id] = datetime.now()
        print(f"[DEBUG] {member.display_name} が入室しました")

    # --- 退出時の処理 ---
    elif before.channel and not after.channel:
        join_time = voice_times.pop(member.id, None)
        if join_time:
            leave_time = datetime.now()
            diff = leave_time - join_time
            seconds = diff.total_seconds()
            minutes = int(seconds // 60)
            
            print(f"[DEBUG] {member.display_name}: 通話時間 {seconds:.1f}秒 -> {minutes}分と判定")

            if minutes >= 1:
                reward = minutes * 60
                change_balance(member.id, reward, is_add=True)
                
                # --- 即送信せずリストに入れる ---
                msg = f"🎙️ {member.mention} が {minutes}分間の通話で {reward} {CURRENCY_NAME} を獲得しました！"
                voice_notification_queue.append(msg)
                
                # もしタイマーが動いていなければ、タイマーを起動する（最初の1人が抜けた時だけ動く）
                if not is_voice_queue_running:
                    channel = bot.get_channel(NOTIFICATION_CHANNEL_ID)
                    if channel:
                        is_voice_queue_running = True
                        asyncio.create_task(send_voice_notifications(channel))
            else:
                print(f"[DEBUG] 1分未満のため報酬なし")

# === リアクション報酬設定 ===
TARGET_CHANNEL_ID = 1452296570295816253  # 指定されたチャンネルID
TARGET_EMOJI = "😎"  # 判定する絵文字

@bot.event
async def on_raw_reaction_add(payload):
    # 指定のチャンネル以外は無視
    if payload.channel_id != TARGET_CHANNEL_ID:
        return

    # 😎 以外のリアクションは無視
    if str(payload.emoji) != TARGET_EMOJI:
        return

    # リアクションしたユーザーを取得
    guild = bot.get_guild(payload.guild_id)
    if not guild: return
    member = guild.get_member(payload.user_id)
    
    # ボット自身のリアクションやメンバー取得失敗時は無視
    if not member or member.bot:
        return

    # メッセージを取得
    channel = bot.get_channel(payload.channel_id)
    try:
        message = await channel.fetch_message(payload.message_id)
    except:
        return # メッセージが見つからない場合

    # メッセージの送信者が管理者（is_admin）かチェック
    if not is_admin(message.author):
        return

    # 重複付与の防止（Firestoreで管理）
    reward_id = f"{payload.message_id}_{payload.user_id}"
    reward_ref = db.collection("reaction_rewards").document(reward_id)

    if reward_ref.get().exists:
        return

    # 1〜100,000 Raruinをランダムに決定
    reward_amount = random.randint(1, 100000)

    # 報酬を付与
    change_balance(payload.user_id, reward_amount, is_add=True)

    # 付与済みフラグをDBに保存
    reward_ref.set({
        "user_id": payload.user_id,
        "message_id": payload.message_id,
        "amount": reward_amount,
        "timestamp": datetime.now()
    })

    # 【修正】DMをやめて指定チャンネルに通知
    notify_channel = bot.get_channel(NOTIFICATION_CHANNEL_ID)
    if notify_channel:
        try:
            await notify_channel.send(f"📸 {member.mention} が撮影に参加して {reward_amount} {CURRENCY_NAME} を獲得しました！")
        except Exception as e:
            print(f"通知送信エラー: {e}")

import os
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler

# --- 最軽量のWebサーバー設定 ---
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        """ブラウザや通常のアクセス用"""
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is active")

    def do_HEAD(self):
        """UptimeRobotの生存確認（HEADリクエスト）用。これがないと501エラーになります"""
        self.send_response(200)
        self.end_headers()

    def log_message(self, format, *args):
        """ログをスッキリさせるためアクセスログを非表示にする"""
        return

def run_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(('0.0.0.0', port), HealthCheckHandler)
    server.serve_forever()

def keep_alive():
    t = threading.Thread(target=run_server, daemon=True)
    t.start()

# --- ここが修正箇所です ---
if __name__ == "__main__":
    # 1. まず「keep_alive()」を実行して、Webサーバーを裏で動かす
    keep_alive()
    
    # 2. その後にBotをログインさせる
    try:
        # ※注意: 上の方で bot = commands.Bot(...) と書いているなら bot.run
        # もし client = ... と書いているなら client.run にしてください
        bot.run(TOKEN) 
    except Exception as e:
        print(f"Bot起動エラー: {e}")
