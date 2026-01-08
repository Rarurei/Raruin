import os
import discord
from discord import app_commands, ui
from discord.ext import commands, tasks
from dotenv import load_dotenv
from datetime import datetime
import json

# ========== 環境変数とFireStore初期化 ==========
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
ADMIN_IDS = [int(x.strip()) for x in os.getenv("ADMIN_ID", "").split(",")]
BACKUP_CHANNEL_ID = int(os.getenv("BACKUP_CHANNEL_ID"))
ITEM_USED_CHANNEL_ID = int(os.getenv("ITEM_USED_CHANNEL_ID"))
os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
CURRENCY_NAME = "Raruin"

from google.cloud import firestore
db = firestore.Client()

# ========== Firestoreデータモデル ==========
def user_doc(user_id):
    return db.collection("users").document(str(user_id))

def shop_doc(shop_name):
    return db.collection("shops").document(shop_name)

def product_doc(shop_name, product_name):
    # 商品はサブコレクション（shops/{shop_name}/products/{product_name}）
    return shop_doc(shop_name).collection("products").document(product_name)

def user_item_doc(user_id, shop_name, product_name):
    return user_doc(user_id).collection("items").document(f"{shop_name}:{product_name}")

# ========== 管理者判定 ==========
def is_admin(user):
    return user.id in ADMIN_IDS

# ========== ユーザー残高操作 ==========
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

# ========== ショップ・商品操作 ==========
def shop_exists(shop_name):
    return shop_doc(shop_name).get().exists

def get_shop_list():
    return [d.id for d in db.collection("shops").list_documents()]

def get_product_list(shop_name=None):
    if shop_name and shop_exists(shop_name):
        return [
            doc.to_dict() | {"product_name":doc.id}
            for doc in shop_doc(shop_name).collection("products").list_documents()
        ]
    else:
        # すべて
        prods = []
        for s in get_shop_list():
            prods.extend([
                doc.to_dict() | {"product_name":doc.id,"shop_name":s}
                for doc in shop_doc(s).collection("products").list_documents()
            ])
        return prods

# ========== ユーザーアイテム ==========
def get_user_items(user_id):
    return [
        doc.to_dict() | {"shop_name": doc.id.split(":")[0], "product_name":doc.id.split(":")[1]}
        for doc in user_doc(user_id).collection("items").list_documents()
    ]

def add_user_item(user_id, shop_name, product_name, count):
    doc = user_item_doc(user_id, shop_name, product_name)
    snap = doc.get()
    if snap.exists:
        doc.set({"amount": firestore.Increment(count)}, merge=True)
    else:
        doc.set({"amount":count, "shop_name":shop_name, "product_name":product_name})

def remove_user_item(user_id, shop_name, product_name, count):
    doc = user_item_doc(user_id, shop_name, product_name)
    snap = doc.get()
    if snap.exists and snap.get("amount",0) >= count:
        remain = snap.get("amount",0) - count
        if remain > 0:
            doc.update({"amount":remain})
        else:
            doc.delete()
        return True
    return False

# ========== Discord Bot初期化 ==========
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.guilds = True
intents.members = True
bot = commands.Bot(command_prefix="/", intents=intents)
tree = bot.tree

# ========== コマンド群 ==========
@tree.command(name="付与", description=f"ユーザー/ロールに {CURRENCY_NAME} 付与（���理者）")
@app_commands.describe(target="ユーザー", amount=f"{CURRENCY_NAME}額")
@app_commands.autocomplete(target=lambda i,c:[
    app_commands.Choice(name=m.display_name,value=str(m.id))
    for m in i.guild.members
])
async def add_raurin(interaction, target: str, amount: int):
    if not is_admin(interaction.user):
        await interaction.response.send_message("管理者限定", ephemeral=True)
        return
    mem = interaction.guild.get_member(int(target))
    if not mem or amount <= 0:
        await interaction.response.send_message("不正な指定", ephemeral=True)
        return
    change_balance(mem.id, amount, is_add=True)
    try: await mem.send(f"あなたに{amount}{CURRENCY_NAME}が付与されました。")
    except: pass
    await interaction.response.send_message(f"{mem.display_name}に{amount}{CURRENCY_NAME}付与", ephemeral=True)

@tree.command(name="減額", description=f"ユーザー/ロールから{CURRENCY_NAME}減額（管理者）")
@app_commands.describe(target="ユーザー", amount=f"{CURRENCY_NAME}額")
@app_commands.autocomplete(target=lambda i,c:[
    app_commands.Choice(name=m.display_name,value=str(m.id))
    for m in i.guild.members
])
async def remove_raurin(interaction, target:str, amount:int):
    if not is_admin(interaction.user):
        await interaction.response.send_message("管理者限定", ephemeral=True)
        return
    mem = interaction.guild.get_member(int(target))
    if not mem or amount <= 0:
        await interaction.response.send_message("不正な指定", ephemeral=True)
        return
    b,_,_ = get_user_balance(mem.id)
    if b < amount:
        await interaction.response.send_message("残高不足", ephemeral=True)
        return
    change_balance(mem.id, amount, is_add=False)
    try: await mem.send(f"{amount}{CURRENCY_NAME}が管理者操作で減額されました。")
    except: pass
    await interaction.response.send_message(f"{mem.display_name}から{amount}{CURRENCY_NAME}減額", ephemeral=True)

@tree.command(name="Shop", description="ショップ追加/削除（管理者）")
@app_commands.describe(action="追加or削除", shop_name="ショップ名")
@app_commands.choices(action=[
    app_commands.Choice(name="追加", value="add"),
    app_commands.Choice(name="削除", value="remove")
])
async def shop_command(interaction, action:str, shop_name:str):
    if not is_admin(interaction.user):
        await interaction.response.send_message("管理者限定", ephemeral=True)
        return
    if action=="add":
        shop_doc(shop_name).set({})
        await interaction.response.send_message(f"ショップ「{shop_name}」追加", ephemeral=True)
    elif action=="remove":
        shop_doc(shop_name).delete()
        await interaction.response.send_message(f"ショップ「{shop_name}」削除", ephemeral=True)

@tree.command(name="Shop商品", description="商品の追加/削除（管理者）")
@app_commands.describe(
    action="追加or削除", product_name="商品名", shop_name="ショップ名",
    description="商品の説明", price="金額", stock="在庫", buy_role="購入可能ロールID"
)
@app_commands.choices(action=[
    app_commands.Choice(name="追加", value="add"),
    app_commands.Choice(name="削除", value="remove")
])
async def shopitem_command(
    interaction, action:str, product_name:str, shop_name:str,
    description:str="", price:int=0, stock:int=0, buy_role:int=0
):
    if not is_admin(interaction.user):
        await interaction.response.send_message("管理者限定", ephemeral=True)
        return
    if not shop_exists(shop_name):
        await interaction.response.send_message("ショップがありません", ephemeral=True)
        return
    if action=="add":
        product_doc(shop_name,product_name).set({
            "description":description, "price":price, "stock":stock, "buy_role":buy_role
        })
        await interaction.response.send_message(f"{shop_name}に商品「{product_name}」追加", ephemeral=True)
    else:
        product_doc(shop_name,product_name).delete()
        await interaction.response.send_message(f"{shop_name}の商品「{product_name}」削除", ephemeral=True)

# ---残高復元（バックアップチャンネルの最新json）---
@tree.command(name="残高復元", description="バックアップからデータリストア（管理者専用）")
async def restore_balance(interaction: discord.Interaction):
    if not is_admin(interaction.user):
        await interaction.response.send_message("管理者限定", ephemeral=True)
        return
    channel = bot.get_channel(BACKUP_CHANNEL_ID)
    async for msg in channel.history(limit=10):
        if msg.content.startswith("【Raruin Backup】"):
            try:
                js = msg.content[msg.content.index("```json")+7: msg.content.rindex("```")]
                dump = json.loads(js)
            except: continue
            # users
            for row in dump.get("users",[]):
                user_doc(row['user_id']).set({
                    "balance":row['balance'],
                    "earned":row['earned'],
                    "spent":row['spent']
                })
            # shops
            for s in dump.get("shops",[]):
                shop_doc(s).set({})
            # products
            for p in dump.get("products",[]):
                product_doc(p['shop_name'],p['product_name']).set({
                    "description":p.get("description",""),
                    "price":p.get("price",0),
                    "stock":p.get("stock",0),
                    "buy_role":p.get("buy_role",0),
                })
            # user_items
            for ui in dump.get("user_items",[]):
                user_item_doc(ui['user_id'],ui['shop_name'],ui['product_name']).set({"amount":ui["amount"]})
            await interaction.response.send_message("復元完了！", ephemeral=True)
            return
    await interaction.response.send_message("バックアップメッセージが見つかりません", ephemeral=True)

# ---残高---
@tree.command(name="残高", description=f"{CURRENCY_NAME}残高・獲得/消費表示")
async def balance_cmd(interaction):
    b,e,s = get_user_balance(interaction.user.id)
    await interaction.response.send_message(
        f"あなたの残高:\n**{b} {CURRENCY_NAME}**\n獲得:{e} 消費:{s}",
        ephemeral=True
    )

# ---ランキング---
@tree.command(name="ランキング", description=f"{CURRENCY_NAME}ランキングTop10")
async def ranking_cmd(interaction):
    # Firestoreは一括クエリが重いので100件でsort
    users = [d.to_dict()|{"user_id":int(d.id)} for d in db.collection("users").list_documents()]
    users.sort(key=lambda x: x.get('balance',0), reverse=True)
    embed = discord.Embed(title=f"{CURRENCY_NAME}ランキング")
    for idx, u in enumerate(users[:10]):
        member = interaction.guild.get_member(u["user_id"])
        n = member.display_name if member else str(u["user_id"])
        embed.add_field(
            name=f"{idx+1}位 {n}",
            value=f"残高:{u.get('balance',0)} / 獲得:{u.get('earned',0)} / 消費:{u.get('spent',0)}",
            inline=False
        )
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ---渡す---
@tree.command(name="渡す", description=f"ユーザーに{CURRENCY_NAME}を渡す")
@app_commands.describe(target="渡す相手", amount=f"{CURRENCY_NAME}額")
@app_commands.autocomplete(target=lambda i,c:[
    app_commands.Choice(name=m.display_name,value=str(m.id)) for m in i.guild.members if m.id!=i.user.id
])
async def transfer_cmd(interaction, target:str, amount:int):
    tid = int(target)
    if tid == interaction.user.id or amount<=0:
        await interaction.response.send_message("不正な指定", ephemeral=True)
        return
    b,_,_ = get_user_balance(interaction.user.id)
    if b < amount:
        await interaction.response.send_message("残高不足", ephemeral=True)
        return
    change_balance(interaction.user.id, amount, is_add=False)
    change_balance(tid, amount, is_add=True)
    user2 = interaction.guild.get_member(tid)
    now = datetime.now()
    try:
        await user2.send(f"{interaction.user.display_name} さんから {amount}{CURRENCY_NAME} を受け取りました\n日時:{now.month}月{now.day}日{now.hour}時{now.minute}分")
    except: pass
    await interaction.response.send_message(f"{user2.display_name}に{amount}{CURRENCY_NAME}渡しました", ephemeral=True)

# ---ショップ一覧---
@tree.command(name="ショップ一覧", description="ショップ一覧（10件/ページ）")
@app_commands.describe(page="ページ(デフォルト1)")
async def shop_list_cmd(interaction, page:int=1):
    shops = get_shop_list()
    max_page = max(1,(len(shops)-1)//10+1)
    page = max(1,min(page,max_page))
    embed = discord.Embed(title="ショップ一覧", description=f"{page}/{max_page}")
    start = (page-1)*10
    for s in shops[start:start+10]:
        embed.add_field(name=s, value=s, inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ---ショップ詳細---
@tree.command(name="ショップ", description="指定ショップの商品一覧（ページあり）")
@app_commands.describe(shop_name="ショップ名", page="ページ(デフォルト1)")
@app_commands.autocomplete(shop_name=lambda i,c:[
    app_commands.Choice(name=s,value=s) for s in get_shop_list()
])
async def shop_detail_cmd(interaction, shop_name:str, page:int=1):
    if not shop_exists(shop_name):
        await interaction.response.send_message("ショップがありません", ephemeral=True)
        return
    prods = [
        doc.to_dict() | {"product_name":doc.id}
        for doc in shop_doc(shop_name).collection("products").list_documents()
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

# ---購入---
@tree.command(name="買う", description="商品購入")
@app_commands.describe(shop_name="ショップ名", product_name="商品名")
@app_commands.autocomplete(shop_name=lambda i,c:[app_commands.Choice(name=s,value=s) for s in get_shop_list()])
@app_commands.autocomplete(product_name=lambda i,c:[
    app_commands.Choice(name=doc.id,value=doc.id)
    for doc in shop_doc(i.namespace.get("shop_name","")).collection("products").list_documents()
])
async def buy_cmd(interaction, shop_name:str, product_name:str):
    if not shop_exists(shop_name):
        await interaction.response.send_message("ショップがありません", ephemeral=True)
        return
    doc = product_doc(shop_name, product_name).get()
    if not doc.exists:
        await interaction.response.send_message("商品がありません", ephemeral=True)
        return
    val = doc.to_dict()
    b,_,_ = get_user_balance(interaction.user.id)
    if b < val.get("price",0):
        await interaction.response.send_message("残高不足", ephemeral=True)
        return
    st = val.get("stock",0)
    if st!=0 and st<1:
        await interaction.response.send_message("在庫切れ", ephemeral=True)
        return
    change_balance(interaction.user.id, val.get("price",0), is_add=False)
    if st!=0:
        product_doc(shop_name, product_name).update({"stock":st-1})
    add_user_item(interaction.user.id, shop_name, product_name, 1)
    await interaction.response.send_message(
        f"{product_name}購入！説明:{val.get('description','')}", ephemeral=True
    )

# ---アイテム表示（所持品ページング）---
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
    items = get_user_items(interaction.user.id)
    if not items:
        await interaction.response.send_message("所持アイテムはありません", ephemeral=True)
        return
    await send_item_list(interaction, interaction.user.id, items, page)

# ---アイテム渡す---
@tree.command(name="アイテム渡す", description="所持アイテムを他人に渡す")
@app_commands.describe(target="渡す相手", product_name="商品名", shop_name="ショップ名", amount="渡す個数")
@app_commands.autocomplete(target=lambda i,c:[
    app_commands.Choice(name=m.display_name,value=str(m.id)) for m in i.guild.members if m.id!=i.user.id
])
@app_commands.autocomplete(product_name=lambda i,c:[
    app_commands.Choice(name=itm["product_name"],value=itm["product_name"])
    for itm in get_user_items(i.user.id)
])
@app_commands.autocomplete(shop_name=lambda i,c:[
    app_commands.Choice(name=itm["shop_name"],value=itm["shop_name"])
    for itm in get_user_items(i.user.id)
])
async def item_transfer_cmd(interaction, target:str, product_name:str, shop_name:str, amount:int):
    tid = int(target)
    if tid==interaction.user.id or amount<=0:
        await interaction.response.send_message("不正な指定", ephemeral=True)
        return
    items = get_user_items(interaction.user.id)
    amt = next((itm["amount"] for itm in items if itm["product_name"]==product_name and itm["shop_name"]==shop_name),0)
    if amt<amount:
        await interaction.response.send_message("所持数不足", ephemeral=True)
        return
    remove_user_item(interaction.user.id, shop_name, product_name, amount)
    add_user_item(tid, shop_name, product_name, amount)
    user2 = interaction.guild.get_member(tid)
    now = datetime.now()
    try:
        await user2.send(f"{interaction.user.display_name}から{product_name}（{shop_name}）{amount}個受け取り\n日時:{now.month}月{now.day}日{now.hour}時{now.minute}分")
    except: pass
    await interaction.response.send_message(f"{user2.display_name}に{product_name}（{shop_name}）{amount}個渡した", ephemeral=True)

# ---アイテム使う---
@tree.command(name="アイテム使う", description="所持アイテムを使用・消費")
@app_commands.describe(product_name="商品名", shop_name="ショップ名")
@app_commands.autocomplete(product_name=lambda i,c:[
    app_commands.Choice(name=itm["product_name"], value=itm["product_name"]) for itm in get_user_items(i.user.id)
])
@app_commands.autocomplete(shop_name=lambda i,c:[
    app_commands.Choice(name=itm["shop_name"], value=itm["shop_name"]) for itm in get_user_items(i.user.id)
])
async def use_item_cmd(interaction, product_name:str, shop_name:str):
    items = get_user_items(interaction.user.id)
    amt = next((itm["amount"] for itm in items if itm["product_name"]==product_name and itm["shop_name"]==shop_name),0)
    if amt<1:
        await interaction.response.send_message("所持していません", ephemeral=True)
        return
    remove_user_item(interaction.user.id, shop_name, product_name, 1)
    msg = f"{interaction.user.display_name}が{product_name}（{shop_name}）を使用！({datetime.now().strftime('%Y/%m/%d %H:%M:%S')})"
    used_ch = bot.get_channel(ITEM_USED_CHANNEL_ID)
    if used_ch: await used_ch.send(msg)
    await interaction.response.send_message(msg, ephemeral=True)
    # バックアップにも（簡易json）
    backup_ch = bot.get_channel(BACKUP_CHANNEL_ID)
    if backup_ch:
        backup = {
            "user_id":interaction.user.id,
            "product_name":product_name,
            "shop_name":shop_name,
            "date":datetime.now().isoformat()
        }
        await backup_ch.send(f"【Raruin Item Used Log】\n```json\n{json.dumps(backup, ensure_ascii=False, indent=2)}\n```")

# ---チャット: 1Raruin付与---
@bot.event
async def on_message(message):
    if message.guild and not message.author.bot:
        change_balance(message.author.id, 1, is_add=True)
    await bot.process_commands(message)

# ---通話: 1分ごとに30Raruin---
voice_times = {}
@bot.event
async def on_voice_state_update(member, before, after):
    if after.channel and not before.channel:
        voice_times[member.id] = datetime.now()
    elif before.channel and not after.channel:
        join_time = voice_times.pop(member.id, None)
        if join_time:
            minutes = max(1,int((datetime.now()-join_time).total_seconds()//60))
            reward = minutes * 30
            change_balance(member.id, reward, is_add=True)
            try:
                await member.send(f"通話報酬:{minutes}分で{reward}{CURRENCY_NAME}獲得！")
            except: pass

# ---毎日0時: Firestore内容をjsonバックアップ---
@tasks.loop(hours=24)
async def daily_backup():
    await bot.wait_until_ready()
    users = [d.to_dict()|{"user_id":int(d.id)} for d in db.collection("users").list_documents()]
    shops = get_shop_list()
    products = []
    for s in shops:
        for prod in shop_doc(s).collection("products").list_documents():
            products.append(prod.to_dict()|{"shop_name":s,"product_name":prod.id})
    user_items = []
    for u in users:
        for itm in user_doc(u["user_id"]).collection("items").list_documents():
            snap = itm.get()
            if snap.exists:
                val = snap.to_dict()
                user_items.append({
                    "user_id":u["user_id"], "shop_name":val.get("shop_name",""), "product_name":val.get("product_name",""), "amount":val.get("amount",0),
                })
    dump = {"users":users,"shops":shops,"products":products,"user_items":user_items,"date":datetime.now().isoformat()}
    channel = bot.get_channel(BACKUP_CHANNEL_ID)
    if channel:
        await channel.send(f"【Raruin Backup】\n```json\n{json.dumps(dump, ensure_ascii=False, indent=2)}\n```")

@bot.event
async def on_ready():
    print(f"Bot activated: {bot.user} ({bot.user.id})")
    try:
        synced = await tree.sync()
        print(f"Slashコマンドを {len(synced)} 個同期")
    except Exception as e:
        print(f"コマンド同期エラー: {e}")
    daily_backup.start()

bot.run(TOKEN)
